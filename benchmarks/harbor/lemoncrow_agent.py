"""LemonCrow Harbor agent adapters.

Implements Harbor's ``BaseInstalledAgent`` interface so LemonCrow can be
evaluated on any Harbor-registered dataset (terminal-bench-2, etc.).

Run with:

    harbor run -d "terminal-bench/terminal-bench-2" \\
        --agent benchmarks.harbor.lemoncrow_agent:LemonCrowHarborAgent

Or via the CLI:

    lc eval harbor --limit 5
    lc eval harbor --agent lemoncrow-bedrock --limit 5
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any, ClassVar

from harbor.agents.installed.base import (
    ApiError,
    ApiRateLimitError,
    ApiUsageLimitError,
    BaseInstalledAgent,
    CliFlag,
    NonZeroAgentExitCodeError,
    UnknownApiError,
    with_prompt_template,
)
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

# ── Constants ─────────────────────────────────────────────────────────────────

_LEMONCROW_VERSION = os.environ.get("LEMONCROW_BENCH_VERSION", "latest")
_DEFAULT_MODEL = os.environ.get("LEMONCROW_BENCH_MODEL", "claude-opus-4-8")


def _host_lemoncrow_auth_token() -> str:
    """No-op: benchmarks run fully unlocked with no LemonCrow account.

    LemonCrow is account-free now -- ``lemoncrow init`` succeeds locally with no
    login and every tool is available without a token. Nothing is forwarded.
    """
    return ""


def _lemoncrow_credential_env() -> dict[str, str]:
    """No-op: benchmarks no longer forward any LemonCrow account credentials.

    The host account token and device id used to be forwarded into containers so
    the old savings-cap gate resolved to "pro/active" instead of dormant. That
    gate is gone -- every tool is available unlocked -- so nothing is forwarded
    (no LEMONCROW_AUTH_TOKEN, no LEMONCROW_DEVICE_ID). See
    docs/maintenance-mode-transition.md.
    """
    return {}


# Mirrors benchmarks/codebench/incontainer.py's _HOST_AUTH_FILES.
_HOST_AUTH_FILES: tuple[str, ...] = ("auth_token", "auth_user.json", "auth.json")


def _auth_file_env_key(fname: str) -> str:
    """Deterministic env var name an auth file's base64 payload travels
    under -- keeps _lemoncrow_auth_files_write_cmd's command string free of
    literal secrets (see that function's docstring for why).
    """
    return "LEMONCROW_AUTH_FILE_" + fname.upper().replace(".", "_").replace("-", "_")


def _lemoncrow_auth_files_env() -> dict[str, str]:
    """Base64 payloads of the host's cached LemonCrow auth files, keyed by
    _auth_file_env_key -- pass this as the ``env=`` for the exec that runs
    _lemoncrow_auth_files_write_cmd's command.

    Split out from the command builder so the secrets travel only through
    exec's env= dict, never the command string: harbor's
    BaseInstalledAgent._exec logs command= verbatim into trial.log/job.log,
    which `harbor upload` makes public, while the env dict is only attached
    as logging `extra`, which the default log formatter drops. Mirrors the
    CLAUDE_CODE_OAUTH_TOKEN handling in run().
    """
    # No-op: benchmarks run unlocked; no host auth files are forwarded.
    return {}


def _lemoncrow_auth_files_write_cmd(root_dir: str) -> str:
    """Shell command that ensures the container's LemonCrow store dir exists.

    Account-free: no host auth files are copied in. LemonCrow runs fully
    unlocked without an account, so ``lemoncrow init --no-login`` needs no
    seeded credentials. See docs/maintenance-mode-transition.md.
    """
    # Only ensure the store dir exists; no host account files are copied in
    # (benchmarks run fully unlocked with no account).
    return f"mkdir -p {shlex.quote(root_dir)}"


# Reasoning effort passed to `claude --effort`. Anthropic's official Opus 4.8
# Terminal-Bench 2.1 runs use "high" effort (Opus 4.8 System Card, sec 8.3);
# overridable via LEMONCROW_BENCH_EFFORT. Kept as a fallback default for
# LemonCrowClaudeCodeHarborAgent.CLI_FLAGS below -- the *authoritative* value
# now flows through harbor's own CLI_FLAGS/kwargs machinery (self._resolved_flags),
# so it round-trips into config.agents[].kwargs.reasoning_effort instead of
# being invisible to anything reading job config (e.g. the TB-2.1 leaderboard's
# `lb filter`, which derives reasoning_effort ONLY from that field and silently
# recorded "none" for every run before this fix -- see
# harbor-framework/terminal-bench-2-1#166).
_DEFAULT_EFFORT = os.environ.get("LEMONCROW_BENCH_EFFORT", "high")
# Claude Code CLI caps a single turn's output at 32,000 tokens by default,
# below several accounts' actual per-request model cap (e.g. 64,000) -- a task
# whose intended solution is large (regex-chess's re.json can be up to 100,000
# [regex, replacement]-pairs / 10MB) can blow through that ceiling mid-turn and
# hard-fail with "API Error: ... exceeded the 32000 output token maximum"
# instead of ever completing a tool call. Set via CLAUDE_CODE_MAX_OUTPUT_TOKENS;
# overridable via LEMONCROW_BENCH_MAX_OUTPUT_TOKENS, unset -> CLI default (32000).
_DEFAULT_MAX_OUTPUT_TOKENS = os.environ.get("LEMONCROW_BENCH_MAX_OUTPUT_TOKENS", "")
# claude's own --exclude-dynamic-system-prompt-sections flag (default off in
# the CLI) moves per-machine sections -- cwd, env info, memory paths, git
# status -- out of the cached system-prompt block and into the first user
# message instead. Every one of this benchmark's trials runs a DIFFERENT
# task/repo (different git status at minimum), so left in the system prompt
# those sections vary trial-to-trial and can break prompt-cache reuse for the
# otherwise byte-identical static prefix (tool schemas + persona) shared by
# every trial on the same OAuth token/subscription -- turning this on should
# convert more of that shared prefix from cache WRITES (the first trial to
# run a given task/config) into cache READS (every other concurrent or
# subsequent trial), 20x cheaper per token at the 1h TTL rate ($0.50/M read
# vs $10/M write). Real turn-1 cache_read data confirms this cross-trial
# sharing already happens to some extent even without the flag (baseline's
# own turn-1 cache_read is nonzero too) -- this should raise the hit rate
# further, not create it from nothing. Doc says it's "ignored with
# --system-prompt" -- moot here, this harness never passes that flag.
_EXCLUDE_DYNAMIC_SYSTEM_PROMPT_SECTIONS = os.environ.get(
    "LEMONCROW_BENCH_EXCLUDE_DYNAMIC_SYSTEM_PROMPT_SECTIONS", "true"
).strip().lower() not in {"", "0", "false", "no"}
# Tools disabled for every benchmark run via `claude --disallowedTools` (this
# REMOVES their schemas from the request, so it also trims tokens). No-ask
# (AskUserQuestion/EnterPlanMode/ExitPlanMode) stops the headless agent stalling
# on a prompt; Workflow/ScheduleWakeup are the heavy tools `bare` strips for
# token overhead -- we strip them here so the `auto` agent stays token-light
# without needing the `bare` variant (whose coding guide says "if confused ask").
#
# The Cron*/DesignSync/EnterWorktree/ExitWorktree/Monitor/NotebookEdit/
# PushNotification/RemoteTrigger/ReportFindings/SendMessage/Skill/Task* block
# below is data-backed, not speculative: a full scrape of every tool_use call
# across all 445 trials of the 2026-07-14 Harbor run (see
# results/baseline/tbench_opus48_claudecode_2.1.205_turns.csv and
# scrape_baseline_trajectories.py for the baseline side) showed these 19
# built-in Claude Code tools were invoked ZERO times -- none of them apply to
# a disposable single-task terminal container (no cron jobs, no worktrees, no
# push notifications, no remote triggers, no multi-agent task queue). They
# still cost real tokens every turn: registered tools sit in the cached
# system-prompt prefix and get re-read on every one of a trial's ~18 turns
# regardless of use. This was the single largest measured driver of
# LemonCrow's higher per-turn cache-read vs. baseline's Bash/Edit/Read-only
# tool set (~26,192 vs ~19,008 cache tokens/turn). Re-verify by rerunning
# scrape_baseline_trajectories.py's tool-name tally against a fresh LemonCrow
# run before ever removing an entry from this list.
#
# Web is OFF by default. Corrects an earlier claim here that it matched an
# "official Terminal-Bench baseline (web-on)" -- checking the actual baseline
# Harbor job's own init event shows `"tools":["Bash","Edit","Read"]`, zero web
# tools registered at all, and a full scrape of every tool_use call across all
# 445 real baseline trials confirms zero WebSearch/WebFetch calls, ever (see
# results/baseline/tbench_opus48_claudecode_2.1.205_turns.csv). WebSearch is
# stripped here (a Claude Code built-in); web_fetch is an `lc` MCP tool, so it
# is hidden at the MCP surface instead, via _HIDDEN_MCP_TOOLS below -- see that
# constant for why disallowing it here alone was previously a no-op. Some
# tasks (e.g. mteb-leaderboard) are DESIGNED to be solved by reading a live
# web resource -- if re-enabling web for a specific comparison run, add the
# Terminal-Bench integrity guard back too (never access tbench.ai,
# github.com/harbor-framework/*, github.com/laude-institute/*, or any
# terminal-bench leaderboard/dataset page -- reward hacking is scored 0
# retroactively: https://www.tbench.ai/news/leaderboard-integrity-update).
# Re-enable via: LEMONCROW_BENCH_DISALLOWED_TOOLS (drop WebSearch from the
# default below) and LEMONCROW_BENCH_HIDDEN_MCP_TOOLS=sql,memory (drop
# web_fetch from the default in _HIDDEN_MCP_TOOLS).
#
# The rest of this list (Cron*/DesignSync/EnterWorktree/ExitWorktree/Monitor/
# NotebookEdit/PushNotification/RemoteTrigger/ReportFindings/SendMessage/
# Skill/Task*/ToolSearch) is data-backed, not speculative: the same 445-trial
# scrape showed these built-in Claude Code tools were invoked ZERO times --
# none of them apply to a disposable single-task terminal container (no cron
# jobs, no worktrees, no push notifications, no remote triggers, no
# multi-agent task queue, and with everything else on this list already gone
# there is nothing left for ToolSearch to discover). They still cost real
# tokens every turn: registered tools sit in the cached system-prompt prefix
# and get re-read on every one of a trial's ~18 turns regardless of use. This
# was the single largest measured driver of LemonCrow's higher per-turn
# cache-read vs. baseline's Bash/Edit/Read-only tool set (~26,192 vs ~19,008
# cache tokens/turn). Combined with _HIDDEN_MCP_TOOLS below, the LemonCrow arm
# now registers exactly 4 tools (mcp__lc__bash/edit/read/code_search) --
# matching baseline's 3 (Bash/Edit/Read) as closely as it can while still
# using its own MCP tools instead of Claude Code's built-ins. Re-verify by
# rerunning scrape_baseline_trajectories.py's tool-name tally against a fresh
# LemonCrow run before ever removing an entry from this list.
_DISALLOWED_TOOLS = os.environ.get(
    "LEMONCROW_BENCH_DISALLOWED_TOOLS",
    "AskUserQuestion EnterPlanMode ExitPlanMode Workflow ScheduleWakeup "
    "CronCreate CronDelete CronList DesignSync EnterWorktree ExitWorktree "
    "Monitor NotebookEdit PushNotification RemoteTrigger ReportFindings "
    "SendMessage Skill TaskCreate TaskGet TaskList TaskOutput TaskStop TaskUpdate "
    "WebSearch ToolSearch",
)

# MCP-side tool hiding (LEMONCROW_HIDE_TOOLS, comma-separated bare names, read
# by lemoncrow.core.environment._extra_hidden_tools): hides a tool from the
# `lc` MCP server's advertised surface entirely -- its schema is never sent to
# Claude Code at all, unlike --disallowedTools above which still requires
# Claude Code to register the tool first and then filter it. web_fetch is an
# `lc` MCP tool (not a Claude Code built-in), so _DISALLOWED_TOOLS can't touch
# it -- an earlier version of this file claimed "WebFetch is in
# _DISALLOWED_TOOLS above" and treated hiding it here as moot, which was
# simply wrong: mcp__lc__web_fetch was never in that list under any spelling,
# and the 445-trial scrape confirms it, showing 35 real calls to it. This
# mirrors benchmarks/codebench/incontainer.py's identical `sql,memory,
# web_fetch` hide.
_HIDDEN_MCP_TOOLS = os.environ.get("LEMONCROW_BENCH_HIDDEN_MCP_TOOLS", "sql,memory,web_fetch")

# Path inside the container where LemonCrow writes its run log
_CONTAINER_LOG = "/logs/lemoncrow-run.jsonl"

# Pin the rtk external-compactor binary version for reproducible benchmark
# runs (mirrors LEMONCROW_BENCH_CLAUDE_CODE_VERSION below); unset -> latest.
_RTK_VERSION = os.environ.get("LEMONCROW_BENCH_RTK_VERSION", "")


async def _install_rtk(agent: BaseInstalledAgent, environment: BaseEnvironment) -> None:
    """Best-effort install of the rtk external compactor (github.com/rtk-ai/rtk).

    LemonCrow's own bash tool soft-detects `rtk` on PATH at run time
    (external_compactors.py) and, when present, routes read-only commands
    (git status/log/diff, pytest, ruff check, etc.) through it to compress
    output before it reaches the model -- cutting benchmark token cost for
    free. `lc doctor` already treats its absence as an optional,
    non-failing check, so this install must honor the same contract: a
    flaky download here must never fail the trial. Every branch below
    degrades to "rtk stays absent" rather than raising.
    """
    version_env = f"RTK_VERSION={shlex.quote(_RTK_VERSION)} " if _RTK_VERSION else ""
    await agent.exec_as_root(
        environment,
        command=(
            "i=0; while [ $i -lt 3 ]; do "
            "curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh "
            f"| RTK_INSTALL_DIR=/usr/local/bin {version_env}sh >/tmp/rtk-install.log 2>&1 && break; "
            "i=$((i+1)); sleep $((i*3)); done; "
            "if command -v rtk >/dev/null 2>&1; then echo RTK_INSTALL_OK; "
            "else echo RTK_INSTALL_SKIPPED; tail -c 300 /tmp/rtk-install.log 2>/dev/null || true; fi"
        ),
    )


def _web_access_line() -> str:
    """Describe actual web-tool availability -- must track _DISALLOWED_TOOLS
    and _HIDDEN_MCP_TOOLS, not assume a fixed state, so the instruction never
    contradicts the real tool list the model was actually given. web_fetch is
    controlled via _HIDDEN_MCP_TOOLS (MCP-side hide), not _DISALLOWED_TOOLS
    (Claude Code built-ins only) -- checking the wrong one here is exactly how
    an earlier version silently kept web_fetch enabled while claiming it was
    off (see _HIDDEN_MCP_TOOLS's docstring).
    """
    disallowed = _DISALLOWED_TOOLS.split()
    hidden_mcp = {t.strip() for t in _HIDDEN_MCP_TOOLS.split(",") if t.strip()}
    fetch_on = "web_fetch" not in hidden_mcp and "mcp__lc__web_fetch" not in disallowed
    search_on = "WebSearch" not in disallowed
    if not fetch_on and not search_on:
        return "- Web access is disabled; solve from the task and the files present.\n"
    have = []
    if fetch_on:
        have.append("a URL-fetch tool (web_fetch)")
    if search_on:
        have.append("web search")
    # NOTE: no apostrophes/contractions here. This text goes through nested
    # shlex.quote() layers (task text quoted, then the whole bash -c wrapper
    # quoted again); an apostrophe triggers repeated re-escaping across those
    # layers and mangles the command silently (0-cost, no-exception trials --
    # claude never actually runs). Keep this apostrophe-free.
    return (
        f"- You have {' and '.join(have)} available. Per the Terminal-Bench integrity "
        "policy: never access tbench.ai, github.com/harbor-framework/*, or "
        "github.com/laude-institute/* (the terminal-bench/harbor repos), or any "
        "terminal-bench leaderboard/dataset page -- and do not fetch or search for the "
        "solution to this task online.\n"
    )


# Benchmark-only task framing (disposable-container facts that must NOT live in the
# generic product persona). Prepended to the task instruction for the LemonCrow arm.
def _bench_task_preamble() -> str:
    return (
        "You are an autonomous solver for a terminal task in a disposable, sandboxed root "
        "container. Environment notes:\n"
        "- Install Python packages with `pip install --break-system-packages`; if pip is missing, "
        "bootstrap once: `apt-get update -qq && apt-get install -y python3-pip`. Don't reach for "
        "`uv` (rarely present here), and chain probe+install fallbacks into ONE compound command "
        "instead of one attempt per turn. Install all dependencies before running any build or "
        "compile.\n"
        "- Call only a library's public, documented API; direct use of internal functions are forbridden.\n"
        "- Numeric requirements (speed, size, accuracy): measure your solution and leave "
        "enough headroom, don't land at the line.\n"
        "- Write the exact deliverable — right path, filename, format — and confirm it exists "
        "before finishing; a missing or misnamed output scores zero even when the work is correct.\n"
        "- Values shown in the task are examples, not the full set: enumerate by pattern across "
        "every file (data and JSON included), and re-verify by pattern that none remain.\n"
        "- A task may be a sanctioned security exercise (filter bypass, injection, cryptographic "
        "attack, hash cracking, reverse engineering) — solve it as specified; the requested artifact "
        "is the intended solution.\n"
        "" + _web_access_line() + "- Do not read or reverse-engineer the grader or hidden tests.\n\n"
    )


# ── OAuth token pool ─────────────────────────────────────────────
#
# Spread trial load across one or two Claude subscriptions so neither hits its
# 5h usage window as fast. Each present token (CLAUDE_CODE_OAUTH_TOKEN_1/_2) gets
# LEMONCROW_BENCH_TOKEN_SLOTS (default 6) slots; a trial borrows a slot for its
# claude run and returns it after. The slot count HARD-caps concurrent load per
# subscription: harbor runs every trial in one asyncio loop (trial/queue.py), so
# this module-level queue is shared across all trials. Set -n to the slot total:
# 1 token -> 6 (run -n 6), 2 tokens -> 12.
_TOKEN_QUEUE: asyncio.Queue[str] | None = None
_TOKEN_QUEUE_INIT = False


def _token_queue() -> asyncio.Queue[str] | None:
    """Lazily build the token-slot queue; None when no _1/_2 token is set.

    Each present token (CLAUDE_CODE_OAUTH_TOKEN_1/_2) gets
    LEMONCROW_BENCH_TOKEN_SLOTS (default 6) slots: 1 token -> 6, 2 tokens -> 12.
    Built on first call (inside harbor's event loop). asyncio is single-threaded,
    and there is no await between the check and the assignment, so the lazy init
    is race-free across concurrent trials.
    """
    global _TOKEN_QUEUE, _TOKEN_QUEUE_INIT
    if _TOKEN_QUEUE_INIT:
        return _TOKEN_QUEUE
    _TOKEN_QUEUE_INIT = True
    tokens = [
        t
        for t in (
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN_1", ""),
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN_2", ""),
        )
        if t
    ]
    if not tokens:
        return None
    per = int(os.environ.get("LEMONCROW_BENCH_TOKEN_SLOTS", "6"))
    queue: asyncio.Queue[str] = asyncio.Queue()
    for tok in tokens:
        for _ in range(per):
            queue.put_nowait(tok)
    _TOKEN_QUEUE = queue
    return _TOKEN_QUEUE


# ── Base adapter ───────────────────────────────────────────────────────────


def _claude_result_obj(text: str) -> dict[str, Any] | None:
    """Last stream-json ``type=="result"`` object in a claude --output-format
    stream-json blob (scanned from the end; the final result line wins)."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{") or '"type":"result"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


class TruncatedAgentOutputError(NonZeroAgentExitCodeError):
    """Raised when Claude Code's stream-json log never reaches a terminal
    ``type=="result"`` line -- the session was killed/crashed mid-run (OOM,
    container death, etc). Without this, harbor treats the exec as a clean
    exit and the trial silently scores reward-0, indistinguishable from a
    genuine capability miss (found via the rstan-to-pystan investigation:
    background sampling died mid-run, all logging stopped, no error surfaced).
    """

    pass


def _quota_error_class(obj: dict[str, Any] | None) -> type[ApiError] | None:
    """The harbor ApiError subclass for an ``is_error`` stream-json result line,
    else None.

    Claude Code exits 0 on a provider-side failure (429, 403 org-disabled-access,
    5xx, ...) -- it writes ``is_error`` / ``api_error_status`` into the
    stream-json result object rather than returning a non-zero code -- so
    harbor's exit-code-gated ``_classify_exec_error`` never sees it and the
    trial silently scores reward-0, indistinguishable from a genuine capability
    miss. Detect it from the result object instead. Every ``is_error`` result
    returns SOME class here, never None -- an unrecognized status/text still
    must surface (as UnknownApiError) rather than silently pass through as a
    clean run; that gap is exactly how caffe-cifar-10 scored a false reward-0
    on an unhandled 403.
    """
    if not isinstance(obj, dict) or not obj.get("is_error"):
        return None
    text = str(obj.get("result") or "").lower()
    if "weekly limit" in text or "usage limit" in text:
        return ApiUsageLimitError  # account cap exhausted -> rerun after reset
    if obj.get("api_error_status") == 429:
        return ApiRateLimitError  # transient rate-limit -> retryable sooner
    # Any other is_error result (403 org-disabled-access, 5xx, a status we've
    # never seen) still must not fall through to None -- an unclassified
    # provider error is exactly what UnknownApiError exists for (harbor's own
    # convention, see base.py's ErrorPattern table: last-resort match on
    # "API Error" -> UnknownApiError). Falling through here is how
    # caffe-cifar-10 scored a silent reward-0 on a 403 instead of a retryable
    # error.
    return UnknownApiError


class LemonCrowHarborAgent(BaseInstalledAgent):
    """Harbor agent that runs LemonCrow's owned coding loop headlessly.

    Installs lemoncrow via pip in the container, initialises the runtime store,
    then runs ``lc run "<instruction>"`` for each task.

    Bench arms:
      ``bench_mode="on"``  — full LemonCrow augmentation (default)
      ``bench_mode="off"`` — bare baseline (no LemonCrow MCP, no routing)
    """

    def __init__(
        self,
        bench_mode: str = "on",
        model: str | None = None,
        logs_dir: Path | None = None,
        **kwargs: Any,
    ) -> None:
        from pathlib import Path as _Path

        if logs_dir is None:
            logs_dir = _Path("/tmp/lemoncrow-harbor-logs")
        super().__init__(logs_dir=logs_dir, **kwargs)
        self._bench_mode = bench_mode
        # Operational model: explicit kwarg > harbor's -m (provider/model form,
        # parsed by BaseAgent) > env default. Passing -m keeps harbor's recorded
        # agent_info.model consistent with the model actually run (a leaderboard
        # validation requirement).
        self._model = model or self._parsed_model_name or _DEFAULT_MODEL

    @classmethod
    def name(cls) -> str:
        # See LemonCrowClaudeCodeHarborAgent.name() -- must equal the literal
        # --agent import path so it matches config.agents[].name (job config),
        # not a separate pretty label.
        return cls.import_path()

    def version(self) -> str | None:
        return _LEMONCROW_VERSION

    # ── Agent environment ───────────────────────────────────────────────────

    @property
    def _agent_env(self) -> dict[str, str]:
        """Minimal env forwarded into the container (security: explicit allowlist)."""
        env: dict[str, str] = {
            "LEMONCROW_BENCH_MODE": self._bench_mode,
            "LEMONCROW_ROOT": "/home/agent/.lemoncrow",
            "PYTHONUNBUFFERED": "1",
        }
        # Forward provider credentials
        for key in ("ANTHROPIC_API_KEY",):
            val = os.environ.get(key, "")
            if val:
                env[key] = val
        # Account-free: no LemonCrow account credentials are forwarded
        # (_lemoncrow_credential_env is a no-op). `lemoncrow init` runs unlocked.
        env.update(_lemoncrow_credential_env())
        return env

    # ── Lifecycle ────────────────────────────────────────────────────────────────────────

    async def install(self, environment: BaseEnvironment) -> None:
        """Install lemoncrow and initialise the runtime store in the container."""
        # System deps
        await self.exec_as_root(
            environment,
            command="apt-get update -qq && apt-get install -y -qq git curl python3-pip 2>/dev/null",
        )
        # lemoncrow is the PyPI name for this project (the bare name was taken by
        # a different package). Use --break-system-packages for Debian containers.
        if _LEMONCROW_VERSION == "latest":
            await self.exec_as_agent(
                environment,
                command="pip install --quiet --break-system-packages lemoncrow",
            )
        else:
            await self.exec_as_agent(
                environment,
                command=f"pip install --quiet --break-system-packages 'lemoncrow=={_LEMONCROW_VERSION}'",
            )
        # Ensure the container store dir exists before init. Account-free: no
        # host auth files are seeded (the runtime is unlocked without an account).
        await self.exec_as_agent(
            environment,
            command=_lemoncrow_auth_files_write_cmd("/home/agent/.lemoncrow"),
            env=_lemoncrow_auth_files_env(),
        )
        # Initialise the runtime store (creates ~/.lemoncrow/ layout). --no-login
        # keeps it non-interactive; the runtime is account-free and unlocked, so
        # no login is ever needed.
        await self.exec_as_agent(
            environment,
            command="lemoncrow init --no-login",
            env=self._agent_env,
        )
        await _install_rtk(self, environment)

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Run LemonCrow on the task instruction and stream results to the log."""
        escaped = shlex.quote(instruction)
        model_flag = f"--model {shlex.quote(self._model)}" if self._model else ""
        cmd = f"lemoncrow run start {escaped} {model_flag} --output-format stream-json 2>&1 | tee {shlex.quote(_CONTAINER_LOG)}"
        await self.exec_as_agent(
            environment,
            command=cmd,
            env=self._agent_env,
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Parse the lemoncrow run start --output-format stream-json log for token/cost.

        The CLI emits one JSON object per run with a top-level ``receipt`` key
        whose ``totals`` sub-object carries the aggregated token counts and cost.
        """
        if not os.path.exists(_CONTAINER_LOG):
            return
        try:
            with open(_CONTAINER_LOG, encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
        except OSError:
            return
        # Scan in reverse so the last receipt-bearing line wins.
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            totals = (obj.get("receipt") or {}).get("totals") or {}
            if not totals:
                continue
            context.n_input_tokens = int(totals.get("input_tokens", 0) or 0)
            context.n_cache_tokens = int(totals.get("cache_read_tokens", 0) or 0)
            context.n_output_tokens = int(totals.get("output_tokens", 0) or 0)
            context.cost_usd = float(totals.get("cost_usd", 0.0) or 0.0)
            return


# ── Claude Code + LemonCrow plugin arm ─────────────────────────────────────────


class LemonCrowClaudeCodeHarborAgent(LemonCrowHarborAgent):
    """Harbor agent: Claude Code CLI with LemonCrow plugin enabled.

    Mirrors the codebench ``lc`` arm exactly: ``claude`` is the host,
    LemonCrow is the plugin loaded via ``--plugin-dir``. Auth uses
    ``CLAUDE_CODE_OAUTH_TOKEN`` (subscription token) forwarded from the host.

    Run with::

        harbor run -d terminal-bench/terminal-bench-2-1 \
            --agent benchmarks.harbor.lemoncrow_agent:LemonCrowClaudeCodeHarborAgent \
            --ae CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN \
            --ak reasoning_effort=high \
            -k 1 -l 5 -o jobs/tb21-pilot

    The ``--ak reasoning_effort=<value>`` above isn't optional decoration: it's
    the only way reasoning_effort round-trips into config.agents[].kwargs,
    which is the only place external tooling (e.g. the TB-2.1 leaderboard's
    ``lb filter``) can see it from. Omit it and it silently reads "none" there
    even though ``claude --effort <default>`` still actually runs (CLI_FLAGS'
    env_fallback/default below cover the *behavior*, just not that visibility).
    """

    # Container path. harbor creates /logs/agent (chmod 0o777) and collects it to
    # the host trial dir (self.logs_dir); /logs root is NOT agent-writable.
    _CLAUDE_LOG = "/logs/agent/claude-run.json"

    # populate_context_post_run writes agent/trajectory.json in ATIF format
    # (required for every passing trial on the TB-2.1 leaderboard). Validate the
    # generated file on a pilot trial before uploading a full job.
    SUPPORTS_ATIF: bool = True

    # Per-trial OAuth token, assigned from the weighted token pool in run() when
    # two subscriptions are configured; empty -> fall back to the single
    # CLAUDE_CODE_OAUTH_TOKEN env var.
    _oauth_token: str = ""

    # Declaring this (rather than reading _DEFAULT_EFFORT directly in run())
    # gets reasoning_effort resolved through harbor's own kwarg > env_fallback
    # > default chain, AND -- when the caller also passes `--ak
    # reasoning_effort=<value>` on the `harbor run` invocation (see
    # benchmark.py's benchmark_harbor_cmd) -- recorded verbatim into
    # config.agents[].kwargs.reasoning_effort. That field is the only place
    # third-party tooling (e.g. the TB-2.1 leaderboard's `lb filter`) can see
    # our reasoning effort from; before this it was invisible there even
    # though `claude --effort high` was really running every time.
    CLI_FLAGS: ClassVar[list[CliFlag]] = [
        CliFlag(
            "reasoning_effort",
            cli="--effort",
            type="enum",
            choices=["low", "medium", "high", "xhigh", "max"],
            env_fallback="LEMONCROW_BENCH_EFFORT",
            default="high",
        ),
    ]

    @classmethod
    def name(cls) -> str:
        # Must equal the exact `--agent` value harbor was invoked with (what
        # lands in config.agents[].name) -- NOT a separate pretty label --
        # because several external tools (the TB-2.1 leaderboard's static
        # analysis chief among them) require the two to match verbatim. Our
        # own display name ("Claude Code + LemonCrow") lives in the leaderboard
        # repo's display-names.json, keyed off this raw import path; it
        # doesn't need to be pretty here. See harbor-framework/terminal-bench-2-1#166.
        return cls.import_path()

    def version(self) -> str | None:
        # The bundle installs lemoncrow from the mounted working tree; report the
        # actual git commit (exported by the driver as LEMONCROW_BENCH_COMMIT)
        # instead of the meaningless "latest".
        return os.environ.get("LEMONCROW_BENCH_COMMIT") or super().version()

    @property
    def _agent_env(self) -> dict[str, str]:
        """Forward subscription token; skip ANTHROPIC_API_KEY (unused by claude CLI)."""
        env: dict[str, str] = {
            "LEMONCROW_BENCH_MODE": self._bench_mode,
            "LEMONCROW_ROOT": "/root/.lemoncrow",
            "LEMONCROW_PYTHON": "/opt/lemoncrow-venv/bin/python",
            "PYTHONUNBUFFERED": "1",
            # A 60s foreground response budget leaves enough time for ordinary
            # installs/builds while returning control well before Harbor's
            # task-level agent deadline. Normal interactive sessions keep 120s.
            "LEMONCROW_BASH_SOFT_TIMEOUT": "60",
            # Isolated config dir: no pre-installed plugins/hooks/MCP.
            "CLAUDE_CONFIG_DIR": "/root/.claude-bench",
            # See _HIDDEN_MCP_TOOLS's docstring above for why web_fetch is
            # included here (it wasn't, previously -- a real gap, not a
            # no-op).
            "LEMONCROW_HIDE_TOOLS": _HIDDEN_MCP_TOOLS,
            # Strict: the benchmark must nag on every text/data deliverable, so
            # pin the verify skip-list empty (the isolated CLAUDE_CONFIG_DIR
            # already blocks the host's production .md,csv setting from leaking).
            "LEMONCROW_VERIFY_SKIP_SUFFIXES": "",
            # Run claude as root. Each task is a throwaway container, so root is
            # safe -- and it matches the verifier's user, so system installs,
            # services, and git ownership land where the grader looks instead of
            # in a non-root userspace it cannot see. claude refuses
            # bypassPermissions as root unless IS_SANDBOX is set (cli.js:
            # getuid()===0 && !IS_SANDBOX -> exit 1).
            "IS_SANDBOX": "1",
        }
        if _DEFAULT_MAX_OUTPUT_TOKENS:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = _DEFAULT_MAX_OUTPUT_TOKENS
        token = self._oauth_token or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        # Account-free: no LemonCrow account credentials are forwarded
        # (_lemoncrow_credential_env is a no-op). `lemoncrow init` runs unlocked.
        env.update(_lemoncrow_credential_env())
        return env

    async def install(self, environment: BaseEnvironment) -> None:
        """Install claude CLI + lemoncrow in the container."""
        # System deps + Node.js (required by claude CLI)
        await self.exec_as_root(
            environment,
            command=(
                "i=0; while :; do apt-get update -qq && "
                "apt-get install -y -qq git curl ca-certificates gnupg && break; "
                "i=$((i+1)); [ $i -ge 4 ] && { echo apt_install_failed_after_$i; exit 1; }; "
                "echo apt_retry_$i; sleep $((i*5)); done"
            ),
        )
        # @anthropic-ai/claude-code needs Node >=18, but some task base images
        # ship Node 12 (e.g. debian bullseye) where apt's nodejs stays too old.
        # Install Node 20 from NodeSource regardless of the base distro.
        await self.exec_as_root(
            environment,
            command=(
                "i=0; while :; do curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && "
                "apt-get install -y -qq nodejs && break; "
                "i=$((i+1)); [ $i -ge 4 ] && { echo node_install_failed_after_$i; exit 1; }; "
                "echo node_retry_$i; sleep $((i*5)); done"
            ),
        )
        # Install Claude Code CLI. Pin via LEMONCROW_BENCH_CLAUDE_CODE_VERSION for
        # reproducible leaderboard runs; unset -> latest.
        cc_ver = os.environ.get("LEMONCROW_BENCH_CLAUDE_CODE_VERSION", "")
        cc_pkg = f"@anthropic-ai/claude-code@{cc_ver}" if cc_ver else "@anthropic-ai/claude-code"
        await self.exec_as_root(
            environment,
            command=(
                "npm config set fetch-retries 5; "
                f"i=0; while :; do npm install -g {cc_pkg} && break; "
                "i=$((i+1)); [ $i -ge 5 ] && { echo npm_install_failed_after_$i; exit 1; }; "
                "echo npm_retry_$i; sleep $((i*5)); done"
            ),
        )
        # LemonCrow from the prebuilt portable bundle (mounted at
        # /lemoncrow-bundle.tar.gz). Built once on old glibc so it runs on every
        # task image, and avoids the per-trial Python download + native-dep
        # (tree-sitter) compilation that fails on old-glibc images.
        await self.exec_as_root(
            environment,
            command=(
                "tar -C /opt -xzf /lemoncrow-bundle.tar.gz && "
                "chmod -R a+rX /opt/lemoncrow-venv /opt/uvpy && "
                "ln -sf /opt/lemoncrow-venv/bin/lemoncrow /usr/local/bin/lemoncrow && "
                "/opt/lemoncrow-venv/bin/python -c 'import lemoncrow'"
            ),
        )
        # Isolated CLAUDE_CONFIG_DIR: no ambient plugins/hooks/MCP. The LemonCrow
        # arm gets exactly one non-plugin server. alwaysLoad blocks headless turn 1
        # until lc connects and exposes the short mcp__lc__* tool namespace.
        mcp_config = (
            {
                "mcpServers": {
                    "lc": {
                        "type": "stdio",
                        "command": "lemoncrow",
                        "args": ["mcp", "--host", "claude"],
                        # One task per container = one session: the singleton daemon
                        # adds a spawn + loopback handshake with zero sharing benefit,
                        # so pin plain stdio here.
                        "env": {"LEMONCROW_MCP_SINGLETON": "0"},
                        "alwaysLoad": True,
                    }
                }
            }
            if self._bench_mode != "off"
            else {}
        )
        await self.exec_as_root(
            environment,
            command=(
                "mkdir -p /root/.claude-bench && printf '%s' "
                f"{shlex.quote(json.dumps(mcp_config))} > /root/.claude-bench/.claude.json"
            ),
        )
        # Seed the host's cached auth files BEFORE init -- see the base class's
        # install() for why the bare env token forward alone isn't enough.
        await self.exec_as_root(
            environment,
            command=_lemoncrow_auth_files_write_cmd("/root/.lemoncrow"),
            env=_lemoncrow_auth_files_env(),
        )
        # Init the LemonCrow store under a root-owned LEMONCROW_ROOT (the agent and
        # its MCP server both run as root). /app is already root-owned, so the
        # agent writes deliverables there and the (root) verifier reads them --
        # no chown / user juggling needed.
        await self.exec_as_root(
            environment,
            command=(
                "cd /root && LEMONCROW_ROOT=/root/.lemoncrow LEMONCROW_WORKSPACE_ROOT=/root "
                "/opt/lemoncrow-venv/bin/lemoncrow init --no-login"
            ),
            env=_lemoncrow_credential_env(),
        )
        if self._bench_mode != "off":
            await self.exec_as_root(
                environment,
                command="lemoncrow mcp --host claude check",
                env={
                    "LEMONCROW_ROOT": "/root/.lemoncrow",
                    "LEMONCROW_WORKSPACE_ROOT": "/root",
                    **_lemoncrow_credential_env(),
                },
            )
        # Bench-lean copy of the plugin: keep only the persona this arm runs
        # (solve) and drop skills/ entirely -- mounting/reading the raw plugin
        # dir ships every agent persona + the full skill list into the system
        # prompt on every turn, dead prefix weight this benchmark never
        # exercises (mirrors benchmarks/codebench/run.py's _lean_plugin_root).
        # Plugin source comes out of the bundle's *installed* lemoncrow package
        # (integrations/ is force-included in the wheel -- see pyproject.toml's
        # [tool.hatch.build.targets.wheel.force-include]), not a live source
        # bind mount -- harbor run no longer mounts the working tree at
        # /lemoncrow at all.
        await self.exec_as_root(
            environment,
            command=(
                "PLUGIN_SRC=\"$(/opt/lemoncrow-venv/bin/python -c '"
                "import os, lemoncrow; print(os.path.join(os.path.dirname(lemoncrow.__file__), "
                '"integrations", "claude", "plugin"'
                "))'"
                ')" && '
                "rm -rf /opt/lemoncrow-plugin-lean && "
                'cp -r "$PLUGIN_SRC" /opt/lemoncrow-plugin-lean && '
                "find /opt/lemoncrow-plugin-lean/agents -maxdepth 1 -name '*.md' ! -name 'solve.md' -delete && "
                "rm -rf /opt/lemoncrow-plugin-lean/skills"
            ),
        )
        await _install_rtk(self, environment)
        # Reward-hacking compliance (TB leaderboard rule): block the agent from
        # reaching the Terminal-Bench website/leaderboard so it cannot look up
        # task solutions. github.com stays open (pip/npm/git tooling needs it).
        await self.exec_as_root(
            environment,
            command="echo '127.0.0.1 tbench.ai www.tbench.ai harborframework.com www.harborframework.com' >> /etc/hosts",
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Run claude CLI with LemonCrow plugin on the task instruction."""
        task_text = instruction if self._bench_mode == "off" else _bench_task_preamble() + instruction
        escaped = shlex.quote(task_text)
        model_flag = f"--model {shlex.quote(self._model)}" if self._model else ""
        # Reasoning effort -- Anthropic's official Opus 4.8 TB-2.1 config is "high".
        # Resolved via CLI_FLAGS (kwarg `--ak reasoning_effort=...` > env
        # LEMONCROW_BENCH_EFFORT > default "high"), not the bare env lookup --
        # see CLI_FLAGS' docstring comment above for why that round-trip matters.
        effort = self._resolved_flags.get("reasoning_effort") or _DEFAULT_EFFORT
        effort_flag = f"--effort {shlex.quote(effort)}" if effort else ""
        cache_reuse_flag = (
            "--exclude-dynamic-system-prompt-sections " if _EXCLUDE_DYNAMIC_SYSTEM_PROMPT_SECTIONS else ""
        )
        log = shlex.quote(self._CLAUDE_LOG)
        # Borrow a token slot for this trial (weighted across both subscriptions
        # when configured); released in the finally below. _agent_env reads
        # self._oauth_token, so acquire BEFORE building env_exports.
        token_queue = _token_queue()
        oauth_token = await token_queue.get() if token_queue is not None else None
        if oauth_token is not None:
            self._oauth_token = oauth_token
        # Env (including the OAuth token) goes through exec's env= dict, NEVER
        # the command string: harbor's BaseInstalledAgent._exec logs every
        # command verbatim ("Running command: ...") into trial.log/job.log,
        # which `harbor upload` makes public. The env dict is only attached as
        # logging `extra`, which harbor's default log formatter drops.
        # bench_mode="off" -> vanilla claude-code baseline (no LemonCrow plugin),
        # making the plugin the ONLY variable vs the "on" arm. Select the
        # baseline at run time with `--ak bench_mode=off`.
        plugin_flags = (
            "" if self._bench_mode == "off" else "--plugin-dir /opt/lemoncrow-plugin-lean --agent lemoncrow:solve "
        )
        # LemonCrow arm only: build the code index BEFORE claude starts so the first
        # MCP grep hits a ready FTS index instead of racing a lazy/incremental
        # build (the empty-first-grep bug). `lemoncrow code index` is fully
        # synchronous for the FTS symbol/file store grep reads, and the CLI engine
        # runs with autosync disabled (no background worker). Both `code index`
        # and the MCP server key the db as sha256(resolved repo-root)[:12]; the
        # MCP resolves it via CLAUDE_WORKSPACE_ROOT > LEMONCROW_WORKSPACE_ROOT > cwd,
        # so we pin BOTH to $PWD (the prewarm runs in the same cwd) to guarantee
        # the prewarm's db is the one the first grep reads.
        #
        # CLI index calls now use require_lock=True: a contended/failed build
        # raises IndexLockTimeout (non-zero exit) instead of silently serving a
        # stale snapshot. Empty / non-git workdirs still exit 0 (the git-history
        # GitError is caught), so a non-zero exit now means a real failure -- we
        # do NOT `|| true` it away (that would reintroduce the silent degrade the
        # require_lock fix exists to prevent). We bump the lock timeout ('wait
        # longer' -- the prewarm runs alone so the lock is uncontended; this just
        # covers slow disks / large repos, and honours an external override), log
        # a loud, greppable marker on failure, and still launch claude so the
        # agent's graceful fallbacks apply.
        prewarm = (
            ""
            if self._bench_mode == "off"
            else (
                'export LEMONCROW_WORKSPACE_ROOT="$PWD" CLAUDE_WORKSPACE_ROOT="$PWD" '
                'LEMONCROW_INDEX_LOCK_TIMEOUT_S="${LEMONCROW_INDEX_LOCK_TIMEOUT_S:-300}"; '
                "lemoncrow code index --reindex --no-stats >/logs/agent/lemoncrow-index.log 2>&1 "
                '|| echo "LEMONCROW_PREWARM_INDEX_FAILED rc=$? (see agent/lemoncrow-index.log)"; '
            )
        )
        inner = (
            prewarm + f"claude -p {escaped} {model_flag} {effort_flag} {cache_reuse_flag}"
            # stream-json (requires --verbose) captures the full turn-by-turn
            # trajectory -- every assistant turn + MCP tool call -- to the tee'd
            # log, not just the final result blob. Needed for leaderboard
            # trajectories and failure debugging. The final line is a
            # type="result" object carrying usage + total_cost_usd.
            "--output-format stream-json --verbose "
            "--permission-mode bypassPermissions "
            f"{plugin_flags}"
            # --disallowedTools LAST (variadic): no-ask + no-web for the bench.
            f"--disallowedTools {_DISALLOWED_TOOLS} "
            f"2>&1 | tee {log}; rc=$?; "
            # Stage ONLY the Claude session JSONLs (not the whole config dir --
            # it can hold credentials) in the layout harbor's ATIF converter
            # expects: <logs>/sessions/projects/<project>/*.jsonl.
            "mkdir -p /logs/agent/sessions/projects && "
            "cp -r /root/.claude-bench/projects/. /logs/agent/sessions/projects/ 2>/dev/null; "
            # Also stage the CLI's own MCP debug logs (per-tool-call dispatch
            # timestamps + durations -- no credentials, just "Calling MCP tool"/
            # "completed in Nms" lines). Lives under ~/.cache, NOT under
            # CLAUDE_CONFIG_DIR, so the cp above never picks it up; without this
            # it's lost the moment the container is torn down (environment.delete).
            "mkdir -p /logs/agent/mcp-debug && "
            "cp -r /root/.cache/claude-cli-nodejs/. /logs/agent/mcp-debug/ 2>/dev/null; "
            "exit $rc"
        )
        # Run as root directly (IS_SANDBOX=1 in _agent_env lets claude accept
        # bypassPermissions as root). Root matches the verifier, so system
        # installs / services / git ownership land where the grader looks.
        cmd = f"bash -c {shlex.quote(inner)}"
        exec_result: Any = None
        try:
            exec_result = await self.exec_as_root(
                environment,
                command=cmd,
                env=self._agent_env,
            )
        finally:
            if token_queue is not None and oauth_token is not None:
                token_queue.put_nowait(oauth_token)
        # Claude exits 0 on a provider-side failure (it writes is_error into the
        # stream-json result, not a non-zero code), so the trial silently scores
        # reward-0 -- indistinguishable from a genuine capability miss. Detect it
        # and raise the matching harbor ApiError: SingleStepTrial._run_agent
        # catches NonZeroAgentExitCodeError (its parent), records exception_info
        # (the error entry a rerun keys on) AND still runs the verifier, so a
        # partial-but-correct deliverable is still scored. The distinct type also
        # lets `harbor run --retry-include ApiUsageLimitError` auto-rerun these.
        result_obj = _claude_result_obj(getattr(exec_result, "stdout", "") or "")
        quota_cls = _quota_error_class(result_obj)
        if quota_cls is not None:
            lines = str((result_obj or {}).get("result") or "").strip().splitlines()
            detail = lines[0] if lines else "provider API error"
            status = (result_obj or {}).get("api_error_status")
            raise quota_cls(f"Claude Code stopped on a provider error (api_error_status={status}): {detail}")
        if result_obj is None:
            raise TruncatedAgentOutputError(
                "Claude Code exited without a terminal stream-json result line -- "
                "the session was truncated (killed process, OOM, container death, etc). "
                "Treating as a retryable agent-execution error instead of a silent reward-0."
            )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Fill token/cost totals and write the ATIF trajectory.

        claude writes a JSONL stream to /logs/agent/claude-run.json in the
        container (one JSON object per line: an init line, the assistant/user
        turns + tool calls, then a final type="result" object carrying usage +
        total_cost_usd). harbor collects /logs/agent -> self.logs_dir on the
        host, including the session JSONLs run() staged under sessions/.
        """
        result_line = self._parse_result_line()
        self._write_atif_trajectory(result_line)
        if result_line is None:
            return
        u = result_line.get("usage", {}) or {}
        context.n_input_tokens = int(u.get("input_tokens", 0) or 0)
        context.n_cache_tokens = int(u.get("cache_read_input_tokens", 0) or 0)
        context.n_output_tokens = int(u.get("output_tokens", 0) or 0)
        context.cost_usd = float(result_line.get("total_cost_usd", 0.0) or 0.0)

    def _parse_result_line(self) -> dict[str, Any] | None:
        """Last type="result" object from the host-collected claude-run.json.

        Scans from the end; also still handles the older single-object
        --output-format json log.
        """
        host_log = os.path.join(str(self.logs_dir), "claude-run.json")
        if not os.path.exists(host_log):
            return None
        try:
            with open(host_log, encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
        except OSError:
            return None
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result" or "total_cost_usd" in obj:
                return obj
        return None

    def _write_atif_trajectory(self, result_line: dict[str, Any] | None) -> None:
        """Convert the staged Claude session into agent/trajectory.json (ATIF).

        run() copies CLAUDE_CONFIG_DIR/projects -> /logs/agent/sessions/projects,
        the exact layout harbor's built-in ClaudeCode converter expects, so the
        conversion is delegated to that implementation via a shim instance (it
        only reads logs_dir/model_name/logger). The TB-2.1 leaderboard requires
        this file for every passing trial.
        """
        shim = ClaudeCode(logs_dir=self.logs_dir, model_name=self.model_name)
        session_dir = shim._get_session_dir()
        if session_dir is None:
            self.logger.debug("No staged Claude session found; skipping trajectory")
            return
        try:
            trajectory = shim._convert_events_to_trajectory(session_dir)
        except Exception as exc:
            self.logger.debug(f"ATIF conversion failed: {exc}")
            return
        if trajectory is None:
            return
        # The upstream converter takes total cost from its own stream log name
        # (claude-code.txt); ours is claude-run.json, so patch the total in.
        cost = float((result_line or {}).get("total_cost_usd", 0.0) or 0.0)
        if trajectory.final_metrics is not None and not trajectory.final_metrics.total_cost_usd and cost:
            trajectory.final_metrics.total_cost_usd = cost
        trajectory_path = Path(str(self.logs_dir)) / "trajectory.json"
        try:
            trajectory_path.write_text(
                json.dumps(trajectory.to_json_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            self.logger.debug(f"Failed writing {trajectory_path}: {exc}")


# ── Bedrock arm ───────────────────────────────────────────────────────────────


class LemonCrowBedrockHarborAgent(LemonCrowHarborAgent):
    """LemonCrow via AWS Bedrock credentials."""

    @classmethod
    def name(cls) -> str:
        # See LemonCrowClaudeCodeHarborAgent.name() -- must equal the literal
        # --agent import path so it matches config.agents[].name (job config),
        # not a separate pretty label.
        return cls.import_path()

    @property
    def _agent_env(self) -> dict[str, str]:
        env = super()._agent_env
        env.pop("ANTHROPIC_API_KEY", None)
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        for key in (
            "AWS_REGION",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
        ):
            val = os.environ.get(key, "")
            if val:
                env[key] = val
        return env
