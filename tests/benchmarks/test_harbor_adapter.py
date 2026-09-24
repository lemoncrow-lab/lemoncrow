"""Tests for the Harbor Terminal-Bench adapter (benchmarks/harbor/lemoncrow_agent.py).

The harbor framework is installed as a uv tool, not a project dependency, so
these tests skip unless harbor is importable. To run them locally:

    PYTHONPATH=$HOME/.local/share/uv/tools/harbor/lib/python3.13/site-packages \
        uv run pytest tests/benchmarks/test_harbor_adapter.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# NOT plain "harbor": with the repo root on sys.path that name can resolve to
# the local benchmarks/harbor package; require the real framework module.
pytest.importorskip("harbor.agents.installed.base")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.harbor import lemoncrow_agent  # noqa: E402
from benchmarks.harbor.lemoncrow_agent import LemonCrowClaudeCodeHarborAgent  # noqa: E402

FAKE_TOKEN = "sk-ant-oat01-FAKETESTTOKEN123456"


class _Ctx:
    """Minimal AgentContext stand-in."""

    n_input_tokens = 0
    n_cache_tokens = 0
    n_output_tokens = 0
    cost_usd = 0.0


@pytest.fixture()
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LemonCrowClaudeCodeHarborAgent:
    # Single-token fallback path: no _1/_2 pool, plain env token.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_1", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_2", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(lemoncrow_agent, "_TOKEN_QUEUE", None)
    monkeypatch.setattr(lemoncrow_agent, "_TOKEN_QUEUE_INIT", False)
    return LemonCrowClaudeCodeHarborAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-4-8")


class _ExecResult:
    """Stand-in for exec_as_root's return value: a completed run always ends
    its stream-json log with a terminal type="result" line (see
    TruncatedAgentOutputError) -- the fake must supply one or run() raises."""

    stdout = json.dumps({"type": "result", "total_cost_usd": 0.01, "usage": {}}, separators=(",", ":"))


def _run_and_capture(agent: LemonCrowClaudeCodeHarborAgent) -> tuple[str, dict[str, str]]:
    captured: dict[str, Any] = {}

    async def fake_exec(environment: Any, command: str, env: dict[str, str] | None = None, **kw: Any) -> _ExecResult:
        captured["command"] = command
        captured["env"] = env
        return _ExecResult()

    agent.exec_as_root = fake_exec  # type: ignore[method-assign]
    # __wrapped__ bypasses the prompt-template decorator (needs no template file).
    asyncio.run(LemonCrowClaudeCodeHarborAgent.run.__wrapped__(agent, "solve the task", None, _Ctx()))
    return captured["command"], captured["env"]


def test_harbor_uses_shorter_bash_soft_timeout(agent: LemonCrowClaudeCodeHarborAgent) -> None:
    assert agent._agent_env["LEMONCROW_BASH_SOFT_TIMEOUT"] == "60"


def test_context_arms_resolve_exact_surfaces(tmp_path: Path) -> None:
    expected = {
        "raw": (False, False, False, False),
        "rtk": (False, True, True, False),
        "lemoncrow": (True, False, True, False),
        "lemoncrow-headroom": (True, False, True, True),
    }
    for arm, flags in expected.items():
        current = LemonCrowClaudeCodeHarborAgent(logs_dir=tmp_path / arm, context_arm=arm)
        assert (
            current._lemoncrow_enabled,
            current._rtk_hook_enabled,
            current._uses_rtk,
            current._headroom_tail_enabled,
        ) == flags
        assert current._agent_env["LEMONCROW_BENCH_CONTEXT_ARM"] == arm

    legacy_raw = LemonCrowClaudeCodeHarborAgent(logs_dir=tmp_path / "legacy-raw", bench_mode="off")
    assert legacy_raw._context_arm == "raw"
    legacy_lc = LemonCrowClaudeCodeHarborAgent(logs_dir=tmp_path / "legacy-lc", bench_mode="on")
    assert legacy_lc._context_arm == "lemoncrow"
    with pytest.raises(ValueError, match="unknown context_arm"):
        LemonCrowClaudeCodeHarborAgent(logs_dir=tmp_path / "bad", context_arm="mystery")


def test_context_arm_install_wires_rtk_lc_and_headroom_independently(tmp_path: Path) -> None:
    async def capture(arm: str) -> list[str]:
        current = LemonCrowClaudeCodeHarborAgent(logs_dir=tmp_path / arm, context_arm=arm)
        calls: list[str] = []

        async def fake_exec(environment: Any, command: str, env: dict[str, str] | None = None, **kw: Any) -> None:
            calls.append(command)

        current.exec_as_root = fake_exec  # type: ignore[method-assign]
        await current.install(None)  # type: ignore[arg-type]
        return calls

    raw = asyncio.run(capture("raw"))
    rtk = asyncio.run(capture("rtk"))
    lc = asyncio.run(capture("lemoncrow"))
    headroom = asyncio.run(capture("lemoncrow-headroom"))

    assert any("snapshot.debian.org/archive/debian/20260831T235959Z" in command for command in raw)
    assert not any("rtk-ai/rtk" in command for command in raw)
    assert not any("rtk hook claude" in command for command in raw)
    assert not any("lemoncrow mcp --host claude check" in command for command in raw)

    assert any("rtk-ai/rtk" in command for command in rtk)
    assert any("rtk hook claude" in command for command in rtk)
    assert not any("lemoncrow mcp --host claude check" in command for command in rtk)

    assert any("rtk-ai/rtk" in command for command in lc)
    assert not any("rtk hook claude" in command for command in lc)
    assert any("lemoncrow-server up" in command for command in lc)
    assert any("/healthz" in command for command in lc)
    assert any("lemoncrow mcp --host claude check --timeout 300" in command for command in lc)
    assert not any("from headroom.transforms.log_compressor import LogCompressor" in command for command in lc)

    assert any("lemoncrow-server up" in command for command in headroom)
    assert any("lemoncrow mcp --host claude check --timeout 300" in command for command in headroom)
    assert any("from headroom.transforms.log_compressor import LogCompressor" in command for command in headroom)


def test_headroom_context_arm_uses_mcp_tail_without_provider_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)
    current = LemonCrowClaudeCodeHarborAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-opus-4-8",
        context_arm="lemoncrow-headroom",
    )
    command, env = _run_and_capture(current)
    assert "headroom proxy" not in command
    assert "ANTHROPIC_BASE_URL" not in command
    assert "headroom-proxy.log" not in command
    assert env["LEMONCROW_BENCH_CONTEXT_ARM"] == "lemoncrow-headroom"
    assert env["LEMONCROW_HEADROOM_MCP_TAIL_MODE"] == "apply"
    assert env["LEMONCROW_HEADROOM_SITE_PACKAGES"] == "/opt/headroom-venv/lib/python3.13/site-packages"
    assert env["LEMONCROW_HEADROOM_TAIL_STATS"] == "/logs/agent/headroom-tail-stats.jsonl"
    assert env["LEMONCROW_HEADROOM_MODEL"] == "claude-opus-4-8"


def test_lemoncrow_context_arm_registers_non_git_workspace_before_prewarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)
    current = LemonCrowClaudeCodeHarborAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-opus-4-8",
        context_arm="lemoncrow",
    )
    command, _ = _run_and_capture(current)
    assert "git rev-parse --show-toplevel" in command
    assert "lemoncrow init --no-login --no-index >/logs/agent/lemoncrow-workspace-init.log" in command
    assert "lemoncrow mcp --host claude check --timeout 300" in command
    assert command.index("lemoncrow init --no-login") < command.index("lemoncrow mcp --host claude check --timeout 300")


def test_raw_context_arm_has_no_lc_prewarm_plugin_or_headroom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)
    current = LemonCrowClaudeCodeHarborAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-opus-4-8",
        context_arm="raw",
    )
    command, _ = _run_and_capture(current)
    assert "lemoncrow code index" not in command
    assert "--plugin-dir /opt/lemoncrow-plugin-lean" not in command
    assert "headroom proxy" not in command


def test_solve_persona_protects_mechanical_deliverable_gates() -> None:
    solve_prompt = (REPO_ROOT / "integrations/claude/plugin/agents/solve.md").read_text(encoding="utf-8")
    assert "runnable candidate at the required location" in solve_prompt
    assert "never sleep-loop polls" in solve_prompt
    assert "A threshold is the deliverable" in solve_prompt


def test_run_keeps_token_out_of_logged_command(agent: LemonCrowClaudeCodeHarborAgent) -> None:
    """harbor logs the command verbatim into trial.log; the token must only
    travel via the exec env= dict (dropped by harbor's log formatter)."""
    command, env = _run_and_capture(agent)
    assert FAKE_TOKEN not in command
    assert "sk-ant" not in command
    assert env is not None and env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN


def test_run_raises_on_truncated_output(agent: LemonCrowClaudeCodeHarborAgent) -> None:
    """No terminal type="result" line in the stream-json log (process killed
    mid-run, e.g. OOM) must surface as a retryable error, not a silent
    reward-0 -- see TruncatedAgentOutputError."""

    class _TruncatedResult:
        stdout = '{"type":"assistant","message":{"content":[]}}\n'  # no result line

    async def fake_exec(
        environment: Any, command: str, env: dict[str, str] | None = None, **kw: Any
    ) -> _TruncatedResult:
        return _TruncatedResult()

    agent.exec_as_root = fake_exec  # type: ignore[method-assign]
    with pytest.raises(lemoncrow_agent.TruncatedAgentOutputError):
        asyncio.run(LemonCrowClaudeCodeHarborAgent.run.__wrapped__(agent, "solve the task", None, _Ctx()))


def test_quota_error_class_never_falls_through_on_is_error() -> None:
    """_quota_error_class must return SOME ApiError subclass for every
    is_error result, never None -- caffe-cifar-10 scored a silent reward-0
    because a 403 (org-disabled-access) matched neither the usage-limit text
    nor the 429 check and fell through to None, undetected."""
    from harbor.agents.installed.base import ApiRateLimitError, ApiUsageLimitError, UnknownApiError

    assert lemoncrow_agent._quota_error_class(None) is None
    assert lemoncrow_agent._quota_error_class({"is_error": False, "result": "ok"}) is None
    assert (
        lemoncrow_agent._quota_error_class({"is_error": True, "result": "You have hit your usage limit."})
        is ApiUsageLimitError
    )
    assert (
        lemoncrow_agent._quota_error_class({"is_error": True, "api_error_status": 429, "result": "rate limited"})
        is ApiRateLimitError
    )
    # The exact caffe-cifar-10 shape: is_error true, an unrecognized status,
    # text that matches neither known pattern -- must not be None.
    assert (
        lemoncrow_agent._quota_error_class(
            {
                "is_error": True,
                "api_error_status": 403,
                "result": "Your organization has disabled Claude subscription access",
            }
        )
        is UnknownApiError
    )


def test_run_raises_on_unclassified_is_error_result(agent: LemonCrowClaudeCodeHarborAgent) -> None:
    """End-to-end: an is_error result line with a status/text run() has never
    seen before must still raise (UnknownApiError), not return normally and
    let the trial score a clean-looking reward-0."""

    class _UnclassifiedErrorResult:
        stdout = json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 403,
                "result": "Your organization has disabled Claude subscription access",
            },
            separators=(",", ":"),
        )

    async def fake_exec(
        environment: Any, command: str, env: dict[str, str] | None = None, **kw: Any
    ) -> _UnclassifiedErrorResult:
        return _UnclassifiedErrorResult()

    agent.exec_as_root = fake_exec  # type: ignore[method-assign]
    with pytest.raises(lemoncrow_agent.UnknownApiError):
        asyncio.run(LemonCrowClaudeCodeHarborAgent.run.__wrapped__(agent, "solve the task", None, _Ctx()))


def test_run_uses_harbor_model_and_stages_sessions(agent: LemonCrowClaudeCodeHarborAgent) -> None:
    command, _ = _run_and_capture(agent)
    # -m anthropic/claude-opus-4-8 -> operational --model claude-opus-4-8
    assert "--model claude-opus-4-8" in command
    # Session JSONLs staged where the ATIF converter expects them, preserving
    # claude's exit code.
    assert "cp -r /root/.claude-bench/projects/. /logs/agent/sessions/projects/" in command
    assert command.rstrip("'").rstrip().endswith("exit $rc")


def test_disallowed_tools_strips_dead_builtin_tools_never_used_in_a_real_run(
    agent: LemonCrowClaudeCodeHarborAgent,
) -> None:
    """A full scrape of every tool_use call across all 445 trials of the
    2026-07-14 Harbor run (results/baseline/tbench_opus48_claudecode_
    2.1.205_turns.csv + scrape_baseline_trajectories.py) showed these
    built-in Claude Code tools were invoked zero times, yet still cost real
    cache-read tokens every turn just by being registered -- the largest
    measured driver of LemonCrow's higher per-turn cache-read vs. baseline.
    Regression-guards the --disallowedTools default so a future edit can't
    silently drop one back in.
    """
    dead_tools = {
        "CronCreate",
        "CronDelete",
        "CronList",
        "DesignSync",
        "EnterWorktree",
        "ExitWorktree",
        "Monitor",
        "NotebookEdit",
        "PushNotification",
        "RemoteTrigger",
        "ReportFindings",
        "SendMessage",
        "Skill",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
    }
    disallowed = set(lemoncrow_agent._DISALLOWED_TOOLS.split())
    missing = dead_tools - disallowed
    assert not missing, f"dead tools missing from _DISALLOWED_TOOLS default: {missing}"

    # Tools LemonCrow's arm actually uses must stay allowed.
    live_tools = {"mcp__lc__bash", "mcp__lc__edit", "mcp__lc__read", "mcp__lc__code_search", "mcp__lc__web_fetch"}
    assert not (live_tools & disallowed), f"live tools wrongly disallowed: {live_tools & disallowed}"

    # End-to-end: the same dead tool names must reach the actual constructed
    # claude invocation via --disallowedTools, not just the module constant.
    command, _ = _run_and_capture(agent)
    assert "--disallowedTools" in command
    disallowed_segment = command.split("--disallowedTools", 1)[1].split("2>&1", 1)[0]
    segment_tools = set(disallowed_segment.split())
    assert dead_tools <= segment_tools, f"missing from constructed command: {dead_tools - segment_tools}"


def test_harbor_uses_release_mcp_surface_without_visibility_overrides(
    agent: LemonCrowClaudeCodeHarborAgent,
) -> None:
    """Harbor may disable Claude built-ins, but never mutates LemonCrow tools/list."""
    disallowed = set(lemoncrow_agent._DISALLOWED_TOOLS.split())
    assert "WebSearch" in disallowed
    assert "ToolSearch" in disallowed

    line = lemoncrow_agent._web_access_line()
    assert "URL-fetch tool (web_fetch)" in line
    assert "Terminal-Bench integrity policy" in line
    assert "'" not in line

    command, _env = _run_and_capture(agent)
    disallowed_segment = command.split("--disallowedTools", 1)[1].split("2>&1", 1)[0]
    segment_tools = set(disallowed_segment.split())
    assert "WebSearch" in segment_tools
    assert "ToolSearch" in segment_tools
    assert "mcp__lc__web_fetch" not in segment_tools


def test_run_requests_dynamic_system_prompt_section_exclusion(agent: LemonCrowClaudeCodeHarborAgent) -> None:
    """claude's --exclude-dynamic-system-prompt-sections flag moves
    per-machine sections (cwd, env info, memory paths, git status) out of the
    cached system-prompt block into the first user message. Every trial in
    this benchmark runs a different task/repo (different git status at
    minimum), so leaving those sections in the system prompt can break
    prompt-cache reuse of the otherwise byte-identical static prefix (tool
    schemas + persona) shared by every trial on the same OAuth token --
    turning this on should convert more of that shared prefix from cache
    writes into cache reads. Must default on (not just be available via env
    override).
    """
    assert lemoncrow_agent._EXCLUDE_DYNAMIC_SYSTEM_PROMPT_SECTIONS is True
    command, _ = _run_and_capture(agent)
    assert "--exclude-dynamic-system-prompt-sections" in command


def _session_event(uuid: str, typ: str, message: dict[str, Any], ts: str) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "sessionId": "s1",
        "type": typ,
        "timestamp": ts,
        "isSidechain": False,
        "cwd": "/app",
        "version": "2.0.0",
        "message": message,
    }


def test_populate_context_writes_atif_trajectory(agent: LemonCrowClaudeCodeHarborAgent, tmp_path: Path) -> None:
    """populate_context_post_run must emit agent/trajectory.json (ATIF) with the
    cost patched in from claude-run.json, and still fill the context totals."""
    events = [
        _session_event(
            "u1",
            "user",
            {"role": "user", "content": [{"type": "text", "text": "solve the task"}]},
            "2026-07-01T00:00:00Z",
        ),
        _session_event(
            "a1",
            "assistant",
            {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2},
            },
            "2026-07-01T00:00:01Z",
        ),
        _session_event(
            "u2",
            "user",
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}],
            },
            "2026-07-01T00:00:02Z",
        ),
        _session_event(
            "a2",
            "assistant",
            {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 20, "output_tokens": 7},
            },
            "2026-07-01T00:00:03Z",
        ),
    ]
    proj = tmp_path / "sessions" / "projects" / "-app"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    (tmp_path / "claude-run.json").write_text(
        json.dumps(
            {
                "type": "result",
                "total_cost_usd": 1.23,
                "usage": {"input_tokens": 30, "output_tokens": 12, "cache_read_input_tokens": 2},
            }
        )
    )

    ctx = _Ctx()
    agent.populate_context_post_run(ctx)

    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    assert trajectory["schema_version"].startswith("ATIF")
    assert len(trajectory["steps"]) == 3
    assert trajectory["final_metrics"]["total_cost_usd"] == 1.23
    assert ctx.n_input_tokens == 30
    assert ctx.n_output_tokens == 12
    assert ctx.cost_usd == 1.23


def test_populate_context_without_session_still_fills_totals(
    agent: LemonCrowClaudeCodeHarborAgent, tmp_path: Path
) -> None:
    """No staged session (e.g. claude crashed early): no trajectory, but the
    claude-run.json totals must still populate the context."""
    (tmp_path / "claude-run.json").write_text(
        json.dumps({"type": "result", "total_cost_usd": 0.5, "usage": {"output_tokens": 3}})
    )
    ctx = _Ctx()
    agent.populate_context_post_run(ctx)
    assert not (tmp_path / "trajectory.json").exists()
    assert ctx.cost_usd == 0.5
    assert ctx.n_output_tokens == 3


def test_supports_atif_and_commit_version(
    agent: LemonCrowClaudeCodeHarborAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert LemonCrowClaudeCodeHarborAgent.SUPPORTS_ATIF is True
    monkeypatch.setenv("LEMONCROW_BENCH_COMMIT", "abc1234")
    assert agent.version() == "abc1234"


def test_agent_env_contains_no_lemoncrow_account_credentials(agent: LemonCrowClaudeCodeHarborAgent) -> None:
    env = agent._agent_env
    assert "LEMONCROW_AUTH_TOKEN" not in env
    assert "LEMONCROW_DEVICE_ID" not in env
    assert env["LEMONCROW_ROOT"] == "/root/.lemoncrow"


def test_install_uses_plain_local_init(agent: LemonCrowClaudeCodeHarborAgent) -> None:
    calls: list[tuple[str, dict[str, str] | None]] = []

    async def fake_exec(environment: Any, command: str, env: dict[str, str] | None = None, **kw: Any) -> None:
        calls.append((command, env))

    agent.exec_as_root = fake_exec  # type: ignore[method-assign]
    asyncio.run(agent.install(None))  # type: ignore[arg-type]

    init_calls = [(command, env) for command, env in calls if "lemoncrow init" in command]
    assert len(init_calls) == 1
    command, env = init_calls[0]
    assert "--login" not in command
    assert "--no-login" not in command
    assert "auth_token" not in command
    assert not env or "LEMONCROW_AUTH_TOKEN" not in env


def test_install_configures_and_probes_lemoncrow_mcp(
    agent: LemonCrowClaudeCodeHarborAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_exec(environment: Any, command: str, env: dict[str, str] | None = None, **kw: Any) -> None:
        calls.append(command)

    agent.exec_as_root = fake_exec  # type: ignore[method-assign]
    asyncio.run(agent.install(None))  # type: ignore[arg-type]

    config_call = next(command for command in calls if "/root/.claude-bench/.claude.json" in command)
    assert '"mcpServers"' in config_call
    assert '"command": "lemoncrow"' in config_call
    assert '"alwaysLoad": true' in config_call
    assert "lemoncrow mcp --host claude check --timeout 300" in calls
