"""Harbor agents for the cursor-agent CLI, with and without LemonCrow.

Harbor shipped only Claude Code arms (``lemoncrow_agent.py``), so terminal-bench
numbers said nothing about Cursor. These two agents make the CLI host the
variable-under-test instead:

    baseline   cursor-agent alone, no MCP server, no rules
    lemoncrow  the same binary + the LemonCrow MCP server and its always-on rule

Run both arms over the same tasks::

    harbor run -d terminal-bench/terminal-bench-2-1 \\
        --agent benchmarks.harbor.cursor_agent:CursorHarborAgent \\
        --mounts '[{"type":"bind","source":"/home/<you>/.config/cursor/auth.json",
                    "target":"/root/.config/cursor/auth.json","read_only":true}]' \\
        -k 1 -o jobs/tb-cursor-baseline

    ... --agent benchmarks.harbor.cursor_agent:LemonCrowCursorHarborAgent

Auth: cursor-agent reads ``~/.config/cursor/auth.json`` (accessToken +
refreshToken written by ``cursor-agent login``). It is bind-mounted read-only
rather than passed as an env var, mirroring codebench's cursor driver -- there is
no token env var for this CLI.

Egress: model inference and auth live on ``*.cursor.sh`` / ``*.cursor.com``; a
hermetic egress guard must allow both or every trial fails at the first call.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

# Self-contained binary from the official installer -- no Node needed, unlike
# the claude CLI. Symlinked onto PATH so it resolves regardless of the image's
# default user (task images vary between root and an `agent` user).
_CURSOR_INSTALL = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
i=0; while :; do apt-get update -qq && apt-get install -y -qq curl ca-certificates git && break;
  i=$((i+1)); [ $i -ge 4 ] && { echo apt_install_failed_after_$i; exit 1; }; sleep $((i*5)); done
i=0; while :; do curl https://cursor.com/install -fsS | bash && break;
  i=$((i+1)); [ $i -ge 4 ] && { echo cursor_install_failed_after_$i; exit 1; }; sleep $((i*5)); done
ln -sf "$HOME/.local/bin/cursor-agent" /usr/local/bin/cursor-agent
cursor-agent --version
"""

# Container path for the mounted credential file.
_CURSOR_AUTH_CONTAINER = "/root/.config/cursor/auth.json"

# cursor-agent only accepts its own model ids -- list them with `cursor-agent
# models`. Vendor-neutral aliases ('sonnet', 'opus') are NOT accepted: they used
# to be dropped here so the server picked for us, which silently ran the whole
# benchmark on composer while the results claimed the requested model. An
# unrecognised id is now a hard error -- a wrong-model run is unrecoverable,
# whereas a startup failure costs seconds.
_CURSOR_MODEL_PREFIXES = ("claude-", "gpt-", "composer", "cursor-grok", "kimi-", "auto")


class UnknownCursorModelError(ValueError):
    """Raised for a model id cursor-agent would not recognise."""


# cursor-agent reports raw token counts but never a price, so cost has to be
# derived. These are Cursor's own published API-pricing rates (USD per token),
# which is what the usage dashboard bills -- not the underlying model vendor's
# list price. Cache reads are 5x cheaper than fresh input, so a run's hit rate
# dominates its cost far more than its total token count does.
_USD_PER_INPUT_TOKEN = 1.25 / 1_000_000
_USD_PER_OUTPUT_TOKEN = 6.00 / 1_000_000
_USD_PER_CACHE_READ_TOKEN = 0.25 / 1_000_000
_USD_PER_CACHE_WRITE_TOKEN = 1.25 / 1_000_000


def _cursor_model(model: str | None) -> str | None:
    m = (model or "").strip()
    if not m:
        return None
    if not m.startswith(_CURSOR_MODEL_PREFIXES):
        raise UnknownCursorModelError(
            f"{m!r} is not a cursor-agent model id, so --model would be dropped and the "
            f"run would silently fall back to the server default (composer). Pass a full "
            f"id such as 'claude-sonnet-5-medium' -- run `cursor-agent models` to list them."
        )
    return m


class CursorHarborAgent(BaseInstalledAgent):
    """Baseline: cursor-agent with its own built-in tools only."""

    # Written by run(); harbor collects /logs/agent into the trial dir.
    # stream-json (JSONL), not json: the plain `json` format emits ONLY a final
    # summary object, which is enough to price a run but says nothing about how
    # it got there. Comparing two arms means explaining *why* one took more
    # turns, so every tool_call event has to be on disk.
    _RUN_LOG = "/logs/agent/cursor-run.jsonl"
    _TRAJECTORY = "trajectory.json"

    def __init__(self, model: str | None = None, logs_dir: Path | None = None, **kwargs: Any) -> None:
        if logs_dir is None:
            logs_dir = Path("/tmp/cursor-harbor-logs")
        super().__init__(logs_dir=logs_dir, **kwargs)
        self._model = model or self._parsed_model_name

    @classmethod
    def name(cls) -> str:
        # Must equal the literal --agent import path: harbor records this as
        # config.agents[].name and external tooling compares the two verbatim.
        return cls.import_path()

    def version(self) -> str | None:
        return os.environ.get("LEMONCROW_BENCH_COMMIT")

    @property
    def _agent_env(self) -> dict[str, str]:
        return {"PYTHONUNBUFFERED": "1"}

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(environment, command=_CURSOR_INSTALL, timeout_sec=900)
        await self._verify_auth(environment)

    async def _verify_auth(self, environment: BaseEnvironment) -> None:
        """Fail loudly at install time rather than as N confusing trial errors.

        Without the bind mount every task fails at its first model call, which
        reads as a benchmark result rather than a harness misconfiguration.
        """
        await self.exec_as_root(
            environment,
            command=(
                f"test -s {shlex.quote(_CURSOR_AUTH_CONTAINER)} || "
                f"{{ echo 'FATAL: {_CURSOR_AUTH_CONTAINER} missing -- pass it via --mounts'; exit 1; }}"
            ),
        )

    async def _setup_arm(self, environment: BaseEnvironment) -> None:
        """Baseline: guarantee no MCP server is registered (cursor-native only)."""
        await self.exec_as_root(environment, command="rm -f /app/.cursor/mcp.json 2>/dev/null || true")

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        await self._setup_arm(environment)
        cmd = [
            "cursor-agent",
            "-p",
            shlex.quote(instruction),
            "--force",
            "--output-format",
            "stream-json",
        ]
        model = _cursor_model(self._model)
        if model:
            cmd += ["--model", shlex.quote(model)]
        log = shlex.quote(self._RUN_LOG)
        await self.exec_as_root(
            environment,
            command=f"mkdir -p /logs/agent && {' '.join(cmd)} > {log} 2>&1; echo exit=$?",
            env=self._agent_env,
            cwd="/app",
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Fill token/cost totals from the run log harbor collected to the host.

        cursor-agent's terminal ``result`` object carries a ``usage`` block
        (inputTokens/outputTokens/cacheReadTokens/cacheWriteTokens) but no
        price, so cost is computed here. Without this every trial reports
        cost_usd=null and the arms can only be compared on pass/fail -- which
        hides the entire point of the comparison.
        """
        self._write_trajectory()
        usage = self._parse_usage()
        if usage is None:
            return
        n_in = int(usage.get("inputTokens", 0) or 0)
        n_out = int(usage.get("outputTokens", 0) or 0)
        n_cache_read = int(usage.get("cacheReadTokens", 0) or 0)
        n_cache_write = int(usage.get("cacheWriteTokens", 0) or 0)
        # harbor's n_input_tokens is documented as "input tokens including
        # cache", so cache reads belong in the total as well as their own field.
        context.n_input_tokens = n_in + n_cache_read
        context.n_cache_tokens = n_cache_read
        context.n_output_tokens = n_out
        context.cost_usd = (
            n_in * _USD_PER_INPUT_TOKEN
            + n_out * _USD_PER_OUTPUT_TOKEN
            + n_cache_read * _USD_PER_CACHE_READ_TOKEN
            + n_cache_write * _USD_PER_CACHE_WRITE_TOKEN
        )

    def _iter_events(self) -> list[dict[str, Any]]:
        """Every parsed JSON object from the host-collected stream-json log."""
        host_log = os.path.join(str(self.logs_dir), os.path.basename(self._RUN_LOG))
        if not os.path.exists(host_log):
            return []
        events: list[dict[str, Any]] = []
        try:
            with open(host_log, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        # cursor-agent writes stderr into the same file, so a
                        # non-JSON line is expected noise rather than corruption.
                        continue
                    if isinstance(obj, dict):
                        events.append(obj)
        except OSError:
            return []
        return events

    def _write_trajectory(self) -> None:
        """Summarise the tool-call trace into agent/trajectory.json.

        Token totals price a run but cannot explain it. When one arm bills more
        than the other the question is always *which tools it called and how
        many times*, so that has to be a first-class artifact rather than
        something re-derived by hand from a raw log after the fact.

        Each event names its tool via a ``<kind>ToolCall`` key (editToolCall,
        shellToolCall, mcpToolCall, ...), which is also how a LemonCrow MCP call
        is told apart from a cursor-native one.
        """
        events = self._iter_events()
        if not events:
            return
        calls: list[dict[str, Any]] = []
        for ev in events:
            if ev.get("type") != "tool_call" or ev.get("subtype") != "completed":
                continue
            payload = ev.get("tool_call")
            if not isinstance(payload, dict):
                continue
            kind = next((k for k in payload if k.endswith("ToolCall")), "unknown")
            body = payload.get(kind) if isinstance(payload.get(kind), dict) else {}
            args = body.get("args") if isinstance(body, dict) else {}
            args = args if isinstance(args, dict) else {}
            started, completed = payload.get("startedAtMs"), payload.get("completedAtMs")
            duration_ms: int | None = None
            if isinstance(started, (str, int)) and isinstance(completed, (str, int)):
                try:
                    duration_ms = int(completed) - int(started)
                except ValueError:
                    duration_ms = None
            calls.append(
                {
                    "tool": kind[: -len("ToolCall")] or kind,
                    "name": args.get("name") or args.get("toolName"),
                    "target": args.get("path") or args.get("command") or args.get("query"),
                    "duration_ms": duration_ms,
                }
            )
        by_tool: dict[str, int] = {}
        for c in calls:
            key = c["tool"] if not c["name"] else f"{c['tool']}:{c['name']}"
            by_tool[key] = by_tool.get(key, 0) + 1
        # `auto` resolves server-side per run, so the id requested is not
        # necessarily the one billed. The init event reports what actually
        # served the request -- without it an auto-vs-auto comparison cannot be
        # shown to have run the same model on both arms.
        init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
        summary = {
            "model": init.get("model"),
            "requested_model": self._model,
            "n_assistant_turns": sum(1 for e in events if e.get("type") == "assistant"),
            "n_tool_calls": len(calls),
            "calls_by_tool": dict(sorted(by_tool.items(), key=lambda kv: -kv[1])),
            "calls": calls,
        }
        try:
            with open(os.path.join(str(self.logs_dir), self._TRAJECTORY), "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=1)
        except OSError:
            self.logger.debug("Could not write trajectory summary")

    def _parse_usage(self) -> dict[str, Any] | None:
        """``usage`` from the last JSON object in the host-collected run log.

        The log is a single JSON object today, but it is scanned from the end
        line-wise so a future switch to stream-json keeps working. A killed or
        timed-out agent leaves the file empty -- that is a real outcome, not an
        error, so it yields None and the trial simply reports no usage.
        """
        host_log = os.path.join(str(self.logs_dir), os.path.basename(self._RUN_LOG))
        if not os.path.exists(host_log):
            return None
        try:
            with open(host_log, encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            return None
        if not text:
            return None
        candidates = [text, *reversed(text.splitlines())]
        for chunk in candidates:
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
                return obj["usage"]
        return None


class LemonCrowCursorHarborAgent(CursorHarborAgent):
    """Same binary, plus the LemonCrow MCP server and its always-on rule.

    Everything except the MCP registration and the rule file is identical to the
    baseline, so the plugin is the only variable between the two arms.
    """

    # Mounted read-only by the driver (same bundle the Claude Code arm uses).
    _BUNDLE = "/lemoncrow-bundle.tar.gz"
    # The bundle bakes absolute paths -- the console script's shebang is
    # `#!/opt/lemoncrow-venv/bin/python` and pyvenv.cfg points at /opt/uvpy --
    # so its two top-level entries must land directly in /opt. Extracting one
    # level deeper leaves an executable whose interpreter does not exist, which
    # fails as a bare ENOENT that reads like a missing binary.
    _EXTRACT_DIR = "/opt"
    _LC_BIN = "/opt/lemoncrow-venv/bin/lemoncrow"
    # The `code` role is Cursor's always-on rule (CURSOR_ALWAYS_ON_ROLE).
    _RULE_SRC = "/lemoncrow/integrations/cursor/rules/lemoncrow.code.mdc"

    @property
    def _agent_env(self) -> dict[str, str]:
        env = dict(super()._agent_env)
        env.update(
            {
                "LEMONCROW_ROOT": "/root/.lemoncrow",
                # Pin the workspace explicitly: an unpinned run resolves via git
                # toplevel/cwd and can index a different tree than the task's.
                "LEMONCROW_WORKSPACE_ROOT": "/app",
                "CURSOR_WORKSPACE_ROOT": "/app",
                "CLAUDE_WORKSPACE_ROOT": "/app",
                "LEMONCROW_BASH_SOFT_TIMEOUT": "60",
            }
        )
        return env

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await self.exec_as_root(
            environment,
            command=(
                f"test -s {self._BUNDLE} || {{ echo 'FATAL: {self._BUNDLE} missing -- mount the bundle'; exit 1; }}; "
                f"mkdir -p {self._EXTRACT_DIR} && tar -xzf {self._BUNDLE} -C {self._EXTRACT_DIR} && "
                # Hard-fail: a missing or unrunnable binary leaves mcp.json
                # pointing at nothing and the arm silently degrades into a
                # second baseline run. `test -x` is not enough -- it passes for
                # a script whose shebang interpreter is absent -- so actually
                # execute it.
                f"{{ {self._LC_BIN} --version >/dev/null 2>&1 || "
                f"{{ echo 'FATAL: {self._LC_BIN} missing or unrunnable after extract'; "
                f"ls -la {self._EXTRACT_DIR}; exit 1; }}; }}"
            ),
            timeout_sec=900,
        )

    async def _setup_arm(self, environment: BaseEnvironment) -> None:
        """Register the MCP server for /app and drop in the always-on rule."""
        mcp = json.dumps(
            {
                "mcpServers": {
                    "lemoncrow": {"type": "stdio", "command": self._LC_BIN, "args": ["mcp", "--host", "cursor"]}
                }
            }
        )
        # MUST be the always-on rule (`alwaysApply: true`). A description-only
        # rule such as lemoncrow.auto.mdc is "Agent Requested": Cursor injects
        # it only if the model asks, so it arrives after the first tool call --
        # by which point the agent has already committed to cursor-native tools
        # and never touches the MCP server. That silently reduces this arm to a
        # baseline run carrying LemonCrow's prompt overhead. Only this rule
        # carries the get_mcp_tools discovery preamble.
        await self.exec_as_root(
            environment,
            command=(
                "mkdir -p /app/.cursor/rules && "
                f"printf '%s' {shlex.quote(mcp)} > /app/.cursor/mcp.json && "
                f"{{ cp {self._RULE_SRC} /app/.cursor/rules/ || "
                f"{{ echo 'FATAL: {self._RULE_SRC} not mounted -- the arm would run ruleless'; "
                "exit 1; }; } && "
                f"{{ grep -q '^alwaysApply: true' /app/.cursor/rules/{os.path.basename(self._RULE_SRC)} || "
                "{ echo 'FATAL: rule lacks alwaysApply:true -- Cursor would not auto-apply it'; "
                "exit 1; }; }"
            ),
            env=self._agent_env,
        )
        # Build the index before the agent starts: the first code_search must hit
        # a ready FTS index rather than race a lazy build (the empty-first-search
        # bug). Non-fatal -- an empty/non-git workdir legitimately has nothing.
        await self.exec_as_root(
            environment,
            command=f"{self._LC_BIN} code index --repo-root /app || echo 'WARN: prewarm index failed'",
            env=self._agent_env,
            cwd="/app",
            timeout_sec=600,
        )
        await self.exec_as_root(
            environment,
            command=(
                "cursor-agent mcp enable lemoncrow || "
                "{ echo 'FATAL: cursor-agent mcp enable lemoncrow failed'; exit 1; }"
            ),
            env=self._agent_env,
            cwd="/app",
            timeout_sec=120,
        )
        await self._verify_mcp_live(environment)

    async def _verify_mcp_live(self, environment: BaseEnvironment) -> None:
        """Abort unless the server actually answers a tools/list handshake.

        Registration succeeding proves only that the config parsed. If the
        server then fails to start, cursor-agent quietly falls back to its
        native tools and the trial reads as a LemonCrow result while measuring
        plain Cursor -- the failure mode this whole arm exists to rule out.
        """
        handshake = (
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
            '{"protocolVersion":"2024-11-05","capabilities":{},'
            '"clientInfo":{"name":"harbor-probe","version":"0"}}}\n'
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        )
        await self.exec_as_root(
            environment,
            command=(
                f"printf '%s' {shlex.quote(handshake)} | "
                f"timeout 60 {self._LC_BIN} mcp --host cursor 2>/dev/null "
                "| grep -q '\"code_search\"' || "
                "{ echo 'FATAL: lemoncrow MCP server did not answer tools/list'; exit 1; }"
            ),
            env=self._agent_env,
            cwd="/app",
            timeout_sec=120,
        )
