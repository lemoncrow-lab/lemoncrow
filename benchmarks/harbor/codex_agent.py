"""Harbor agents for the Codex CLI, with and without LemonCrow.

Harbor shipped only Claude Code arms (``lemoncrow_agent.py``) and later a Cursor
pair (``cursor_agent.py``), so terminal-bench numbers said nothing about Codex.
These two agents make the CLI host the variable-under-test instead:

    baseline   codex alone, its own native tools, its own prompt
    lemoncrow  the same binary with LemonCrow's toolset substituted for the
               natives it duplicates, plus the ``lemoncrow:auto`` persona

Run both arms over the same tasks::

    harbor run -d terminal-bench/terminal-bench-2-1 \\
        --agent benchmarks.harbor.codex_agent:CodexHarborAgent \\
        --mounts '[{"type":"bind","source":"/home/<you>/.codex/auth.json",
                    "target":"/codex-auth.json","read_only":true}]' \\
        -i write-compressor -k 1 -o jobs/tb-codex-baseline

    ... --agent benchmarks.harbor.codex_agent:LemonCrowCodexHarborAgent
        (also mount /tmp/avbuild/lemoncrow-bundle.tar.gz -> /lemoncrow-bundle.tar.gz)

Auth: codex reads ``$CODEX_HOME/auth.json`` (ChatGPT OAuth tokens written by
``codex login``). The host file is bind-mounted read-only to ``/codex-auth.json``
and **copied** into the container's ``$CODEX_HOME`` at install time -- Codex
refreshes that token in place, and a read-only mount would turn a mid-run
refresh into a failed trial.

Egress: model inference and auth live on ``chatgpt.com`` (plus
``*.oaiusercontent.com`` for attachments); a hermetic egress guard must allow
them or every trial fails at the first call.
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

from benchmarks.codex_native_tools import codex_config_toml, codex_native_calls

# Codex ships as an npm package wrapping a native binary, so Node is a hard
# prerequisite -- and task images are all over the place: some have no Node,
# some ship Node 18, at least one (make-mips-interpreter) ships Node 18 with no
# npm at all. So instead of probing and patching the image's toolchain, install
# a private pinned Node under /opt and reach Codex through a wrapper. The task's
# own `node`/`npm` are left exactly as the task author built them -- swapping
# them out could change what the task under test is even doing.
# Every step prints a marker: harbor truncates a failed command's output, and
# without them a failure 300 lines into apt noise is unattributable.
_CODEX_INSTALL = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
NODE_V=v22.20.0
i=0; while :; do apt-get update -qq && apt-get install -y -qq curl ca-certificates xz-utils git && break;
  i=$((i+1)); [ $i -ge 4 ] && { echo 'lc:FAIL apt'; exit 1; }; sleep $((i*5)); done
echo 'lc:ok apt'
i=0; while :; do curl -fsSL "https://nodejs.org/dist/$NODE_V/node-$NODE_V-linux-x64.tar.xz" -o /tmp/node.txz && break;
  i=$((i+1)); [ $i -ge 4 ] && { echo 'lc:FAIL node-download'; exit 1; }; sleep $((i*5)); done
mkdir -p /opt/node22 && tar -xJf /tmp/node.txz -C /opt/node22 --strip-components=1
/opt/node22/bin/node --version || { echo 'lc:FAIL node-extract'; exit 1; }
echo 'lc:ok node'
# PATH, not --prefix: `npm i -g --prefix` silently resolves elsewhere and leaves
# lib/node_modules empty, which then fails later as a bare MODULE_NOT_FOUND.
export PATH=/opt/node22/bin:$PATH
i=0; while :; do npm i -g @openai/codex && break;
  i=$((i+1)); [ $i -ge 4 ] && { echo 'lc:FAIL codex-npm'; exit 1; }; sleep $((i*5)); done
test -f /opt/node22/lib/node_modules/@openai/codex/bin/codex.js || { echo 'lc:FAIL codex-missing'; exit 1; }
printf '#!/bin/sh\nexec /opt/node22/bin/node /opt/node22/lib/node_modules/@openai/codex/bin/codex.js "$@"\n' \
  > /usr/local/bin/codex
chmod +x /usr/local/bin/codex
codex --version || { echo 'lc:FAIL codex-run'; exit 1; }
echo 'lc:ok codex'
"""

# Read-only mount of the host credential file, and the writable home Codex uses.
_CODEX_AUTH_MOUNT = "/codex-auth.json"
_CODEX_HOME = "/root/.codex"


class CodexHarborAgent(BaseInstalledAgent):
    """Baseline: the Codex CLI with its own built-in tools only."""

    # Written by run(); harbor collects /logs/agent into the trial dir.
    _RUN_LOG = "/logs/agent/codex-run.jsonl"
    # Rollouts are copied here so the host can audit which tools were called.
    _SESSIONS_LOG_DIR = "/logs/agent/codex-sessions"

    def __init__(
        self,
        model: str | None = None,
        logs_dir: Path | None = None,
        reasoning_effort: str | None = None,
        **kwargs: Any,
    ) -> None:
        if logs_dir is None:
            logs_dir = Path("/tmp/codex-harbor-logs")
        super().__init__(logs_dir=logs_dir, **kwargs)
        self._model = model or self._parsed_model_name
        # Set on the command line, never in config.toml: only the LemonCrow arm
        # writes a config.toml, so putting effort there would silently hand the
        # two arms different reasoning budgets and invalidate the comparison.
        self._reasoning_effort = reasoning_effort or os.environ.get("LEMONCROW_BENCH_EFFORT") or "high"

    @classmethod
    def name(cls) -> str:
        # Must equal the literal --agent import path: harbor records this as
        # config.agents[].name and external tooling compares the two verbatim.
        return cls.import_path()

    def version(self) -> str | None:
        return os.environ.get("LEMONCROW_BENCH_COMMIT")

    @property
    def _agent_env(self) -> dict[str, str]:
        return {"PYTHONUNBUFFERED": "1", "CODEX_HOME": _CODEX_HOME}

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(environment, command=_CODEX_INSTALL, timeout_sec=900)
        await self._install_auth(environment)

    async def _install_auth(self, environment: BaseEnvironment) -> None:
        """Copy the mounted credentials into a writable CODEX_HOME.

        Fails loudly at install time rather than as N confusing trial errors:
        without the mount every task dies at its first model call, which reads
        as a benchmark result rather than a harness misconfiguration.
        """
        await self.exec_as_root(
            environment,
            command=(
                f"test -s {shlex.quote(_CODEX_AUTH_MOUNT)} || "
                f"{{ echo 'FATAL: {_CODEX_AUTH_MOUNT} missing -- pass it via --mounts'; exit 1; }}; "
                f"mkdir -p {_CODEX_HOME} && cp {shlex.quote(_CODEX_AUTH_MOUNT)} {_CODEX_HOME}/auth.json && "
                f"chmod 600 {_CODEX_HOME}/auth.json"
            ),
        )

    async def _setup_arm(self, environment: BaseEnvironment) -> None:
        """Baseline: guarantee no LemonCrow config is present (codex-native only)."""
        await self.exec_as_root(
            environment,
            command=f"rm -f {_CODEX_HOME}/config.toml /app/AGENTS.md 2>/dev/null || true",
        )

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        await self._setup_arm(environment)
        cmd = [
            "codex",
            "exec",
            "--json",
            # Non-interactive exec auto-DENIES every approval prompt, which
            # starves tool calls and sandboxes writes -- the trial would fail
            # for reasons unrelated to the toolset. The task container is
            # already the sandbox.
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
        if self._model:
            cmd += ["--model", shlex.quote(self._model)]
        if self._reasoning_effort:
            cmd += ["-c", f"model_reasoning_effort={shlex.quote(self._reasoning_effort)}"]
        cmd += ["-C", "/app", "--", shlex.quote(instruction)]
        log = shlex.quote(self._RUN_LOG)
        await self.exec_as_root(
            environment,
            command=f"mkdir -p /logs/agent && {' '.join(cmd)} > {log} 2>&1; echo exit=$?",
            env=self._agent_env,
            cwd="/app",
        )
        # Collect the rollouts so the host can prove which tools were used.
        await self.exec_as_root(
            environment,
            command=(
                f"mkdir -p {self._SESSIONS_LOG_DIR} && "
                f"find {_CODEX_HOME}/sessions -name '*.jsonl' -exec cp {{}} {self._SESSIONS_LOG_DIR}/ \\; "
                "2>/dev/null || true"
            ),
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Fill token totals from the run log harbor collected to the host.

        ``codex exec --json`` emits one ``turn.completed`` event per turn, each
        carrying a usage block. Without this every trial reports null tokens and
        the arms can only be compared on pass/fail -- which hides the entire
        point of the comparison.
        """
        n_in = n_out = n_cached = 0
        model = ""
        found = False
        for event in self._run_events():
            if event.get("type") != "turn.completed":
                continue
            usage = event.get("usage") or {}
            found = True
            n_in += int(usage.get("input_tokens", 0) or 0)
            n_out += int(usage.get("output_tokens", 0) or 0)
            n_cached += int(usage.get("cached_input_tokens", 0) or 0)
            model = str(event.get("model") or model)
        if not found:
            return
        # harbor's n_input_tokens is documented as "input tokens including
        # cache"; Codex already reports it that way, so it is not re-added.
        context.n_input_tokens = n_in
        context.n_cache_tokens = n_cached
        context.n_output_tokens = n_out
        context.cost_usd = self._cost_usd(model or (self._model or ""), n_in, n_out, n_cached)

    @staticmethod
    def _cost_usd(model: str, n_in: int, n_out: int, n_cached: int) -> float | None:
        """Price the run with the same table codebench uses, or None if unknown.

        Codex reports tokens but never a price, so without this every trial
        lands with cost_usd=null and the arms can only be compared on pass/fail.
        ``input_tokens`` already includes the cached portion, so the uncached
        remainder is what gets charged at full rate.
        """
        try:
            from lemoncrow.core.capabilities.pricing import usage_cost_usd

            cost = float(
                usage_cost_usd(
                    model,
                    input_tokens=max(n_in - n_cached, 0),
                    output_tokens=n_out,
                    cache_read_tokens=n_cached,
                    cache_write_tokens=0,
                )
            )
            # An unpriced model id returns 0.0 rather than raising, and a
            # reported $0.00 for a run that clearly burned tokens is worse than
            # an honest null -- it would quietly win every cost comparison.
            return cost if cost > 0 or not (n_in or n_out) else None
        except Exception:
            # An unpriced model is a reporting gap, never a reason to fail a
            # trial that otherwise produced a real result.
            return None

    def _run_events(self) -> list[dict[str, Any]]:
        """JSONL events from the host-collected run log.

        A killed or timed-out agent leaves the file short or empty -- that is a
        real outcome, not an error, so it simply yields fewer events.
        """
        host_log = Path(self.logs_dir) / Path(self._RUN_LOG).name
        if not host_log.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in host_log.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
        return events

    def native_tool_calls(self) -> dict[str, int]:
        """Native (non-LemonCrow) tools this trial actually called.

        For the baseline this is expected to be non-empty -- that is the arm
        working as designed. For the LemonCrow arm it must be empty, or the
        trial did not measure what it claims to.
        """
        sessions = Path(self.logs_dir) / Path(self._SESSIONS_LOG_DIR).name
        return dict(codex_native_calls(sessions)) if sessions.exists() else {}


class LemonCrowCodexHarborAgent(CodexHarborAgent):
    """Same binary, with LemonCrow's toolset substituted for Codex's natives.

    Everything except ``$CODEX_HOME/config.toml`` is identical to the baseline,
    so that file -- the MCP registration, the persona, and the disabled natives
    -- is the only variable between the two arms.
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
    _PERSONA = "lemoncrow:auto"

    @property
    def _agent_env(self) -> dict[str, str]:
        env = dict(super()._agent_env)
        env.update(
            {
                "LEMONCROW_ROOT": "/root/.lemoncrow",
                # Pin the workspace explicitly: an unpinned run resolves via git
                # toplevel/cwd and can index a different tree than the task's.
                "LEMONCROW_WORKSPACE_ROOT": "/app",
                "CLAUDE_WORKSPACE_ROOT": "/app",
                # No LEMONCROW_BASH_SOFT_TIMEOUT override: the arm should behave
                # like a real install. Halving the default wait only buys extra
                # round-trips, and any harness-only deviation is a thumb on the
                # scale in a comparison whose whole point is the toolset.
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
                # Hard-fail: a missing or unrunnable binary leaves config.toml
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
        """Write the config that makes LemonCrow the only toolset on offer."""
        config = codex_config_toml(self._PERSONA, self._LC_BIN)
        await self.exec_as_root(
            environment,
            command=(
                f"mkdir -p {_CODEX_HOME} && printf '%s' {shlex.quote(config)} > {_CODEX_HOME}/config.toml && "
                # The persona rides in developer_instructions; a stale AGENTS.md
                # would double it up.
                "rm -f /app/AGENTS.md 2>/dev/null || true"
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
        await self._verify_mcp_live(environment)

    async def _verify_mcp_live(self, environment: BaseEnvironment) -> None:
        """Abort unless the server actually answers a tools/list handshake.

        A parseable config proves only that the config parsed. If the server
        then fails to start, Codex quietly carries on with whatever tools remain
        and the trial reads as a LemonCrow result while measuring a crippled
        Codex -- the failure mode this whole arm exists to rule out.
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
                f"timeout 60 {self._LC_BIN} mcp --host codex 2>/dev/null "
                "| grep -q '\"code_search\"' || "
                "{ echo 'FATAL: lemoncrow MCP server did not answer tools/list'; exit 1; }"
            ),
            env=self._agent_env,
            cwd="/app",
            timeout_sec=120,
        )
