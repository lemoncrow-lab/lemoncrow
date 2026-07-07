# ruff: noqa: RUF001

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from atelier.core.capabilities import plugin_runtime
from atelier.infra.runtime.run_ledger import RunLedger

pytestmark = pytest.mark.slow  # Each test spawns a real Python subprocess (~2s each)

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "integrations" / "codex" / "hooks"
STATUSLINE = ROOT / "integrations" / "codex" / "plugin" / "scripts" / "statusline.sh"


def _run_hook(
    script: str, root: Path, payload: dict[str, Any], version: str = "1.0.0"
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "ATELIER_ROOT": str(root),
            "ATELIER_VERSION": version,
            "ATELIER_CTX_NUDGE_TOKENS": "999999999",
            # These are Codex hooks: pin detect_host()'s explicit override so
            # every per-session path lands under host "codex" regardless of
            # the ambient test-runner environment (e.g. a stray CLAUDE_CODE
            # or ATELIER_AGENT=claude leaking in from the outer harness).
            "ATELIER_AGENT": "codex",
        }
    )
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )


def _run_statusline(
    root: Path, payload: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"ATELIER_ROOT": str(root), "ATELIER_NO_COLOR": "1"})
    env.update(env_extra or {})
    return subprocess.run(
        [str(STATUSLINE)],
        input=payload,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )


def test_codex_statusline_renders_native_footer_in_claude_format(tmp_path: Path) -> None:
    native = "gpt-5.5 xhigh · ~/Projects/leanchain/atelier · 1.11M used · 19.4M in · 61.1K out"

    result = _run_statusline(tmp_path / ".atelier", native)

    assert result.stdout.strip() == ("❯ atelier | gpt-5.5 xhigh ctx 1.1M $0.000(I:19.4M C:0 O:61k) $0.000(R:0)")


def test_codex_statusline_renders_json_token_fields_in_claude_format(tmp_path: Path) -> None:
    payload = {
        "model": {"name": "gpt-5.5"},
        "effort": "xhigh",
        "session_id": "c1",
        "context": {"used_tokens": 1_110_000, "used_percent": 12.3},
        "usage": {"input_tokens": 19_400_000, "output_tokens": 61_100},
        "cost": {"total_usd": 1.23456},
    }

    result = _run_statusline(tmp_path / ".atelier", json.dumps(payload))

    assert result.stdout.strip() == ("❯ atelier | gpt-5.5 xhigh ctx 1.1M 12% $1.235(I:19.4M C:0 O:61k) $0.000(R:0)")


def test_codex_multi_file_prompt_emits_no_runtime_context(tmp_path: Path) -> None:
    result = _run_hook(
        "user_prompt.py",
        tmp_path / ".atelier",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "c1",
            "prompt": "Update auth.py and billing.py to share token parsing",
        },
    )

    assert result.stdout == ""


def test_codex_user_prompt_emits_high_context_nudge_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".atelier"
    monkeypatch.setattr(
        "atelier.gateway.hosts.context_state.host_context_state",
        lambda host, session_id: (200_000, "gpt-5.5"),
    )
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "c1",
        "prompt": "Continue the implementation",
    }

    first = plugin_runtime.build_codex_user_prompt_output(root, payload)
    second = plugin_runtime.build_codex_user_prompt_output(root, payload)

    assert "high context" in first["uiMessage"]
    assert "additionalContext" not in first
    assert second.get("no_output") is True


def test_codex_pre_tool_use_blocks_full_reread_after_edit(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    session_id = "c1"
    runs = root / "runs"
    runs.mkdir(parents=True)
    (runs / f"{session_id}.json").write_text(
        json.dumps({"session_id": session_id, "events": [], "files_touched": ["src/a.py"]}),
        encoding="utf-8",
    )

    result = _run_hook(
        "pre_tool_use.py",
        root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "tool_name": "mcp__atelier__read",
            "tool_input": {"files": [{"path": "src/a.py", "full": True}]},
            "cwd": str(tmp_path),
        },
    )

    output = json.loads(result.stdout)
    hook = output["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse"
    assert hook["permissionDecision"] == "deny"
    assert "range" in hook["permissionDecisionReason"]


def test_codex_savings_reporter_updates_session_stats(tmp_path: Path) -> None:
    from atelier.core.foundation.paths import session_dir

    root = tmp_path / ".atelier"
    result = _run_hook(
        "savings_reporter.py",
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "mcp__plugin_atelier_atelier__Edit",
            "tool_input": {"edits": [{"file_path": "a.py"}, {"file_path": "b.py"}]},
        },
    )

    stats = json.loads((session_dir(root, "codex", "c1") / "stats.json").read_text(encoding="utf-8"))
    assert result.stdout == ""
    assert stats["total_tool_calls"] == 1
    assert stats["tools_used"]["mcp__plugin_atelier_atelier__Edit"] == 1
    assert stats["event_counts"]["PostToolUse"] == 1


def test_codex_savings_reporter_is_quiet_after_repeated_searches(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    for now_ms in (1_000, 601_001, 601_002):
        result = _run_hook(
            "savings_reporter.py",
            root,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "c1",
                "tool_name": "mcp__plugin_atelier_atelier__Search",
                "tool_input": {},
                "now_ms": now_ms,
            },
        )
        assert result.stdout == ""


def test_codex_savings_reporter_records_loop_state_without_output(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    root.mkdir()
    session_id = "loop-run"
    ledger = RunLedger(session_id=session_id, agent="codex", root=root, task="debug repeated read loop")
    for index in range(3):
        ledger.record_tool_call("Search", {"query": "why is this looping"})
        ledger.record_tool_call("Read", {"path": f"src/module_{index}.py"})
    ledger.persist(root)
    (root / "session_state.json").write_text(
        json.dumps({"active_session_id": session_id, "atelier_root": str(root)}),
        encoding="utf-8",
    )

    result = _run_hook(
        "savings_reporter.py",
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "mcp__plugin_atelier_atelier__Search",
            "tool_input": {},
            "now_ms": 2_000,
        },
    )

    assert result.stdout == ""
    from atelier.core.foundation.paths import session_dir

    stats = json.loads((session_dir(root, "codex", "c1") / "stats.json").read_text(encoding="utf-8"))
    assert stats["total_tool_calls"] == 1


def test_codex_savings_reporter_ignores_non_atelier_tools(tmp_path: Path) -> None:
    result = _run_hook(
        "savings_reporter.py",
        tmp_path / ".atelier",
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "Read",
            "tool_input": {},
        },
    )

    assert result.stdout == ""


def test_codex_subagent_hook_tracks_start_and_stop(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    start_payload = {
        "hook_event_name": "SubagentStart",
        "session_id": "c1",
        "agent_id": "agent-1",
        "agent_type": "atelier:explore",
    }
    stop_payload = {
        "hook_event_name": "SubagentStop",
        "session_id": "c1",
        "agent_id": "agent-1",
        "agent_type": "atelier:explore",
    }

    start = _run_hook("subagent.py", root, start_payload)
    stop = _run_hook("subagent.py", root, stop_payload)

    from atelier.core.foundation.paths import session_dir

    stats = json.loads((session_dir(root, "codex", "c1") / "stats.json").read_text(encoding="utf-8"))
    assert start.stdout == ""
    assert stop.stdout == ""
    assert stats["subagents_started"] == 1
    assert stats["subagents_completed"] == 1
    assert stats["pending_subagents"] == 0
    assert stats["active_subagents"] == {}
    assert stats["event_counts"]["SubagentStart"] == 1
    assert stats["event_counts"]["SubagentStop"] == 1


def test_codex_stop_hook_emits_session_summary(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _run_hook(
        "user_prompt.py",
        root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "c1",
            "turn_id": "turn-1",
            "model": "gpt-5",
            "usage": {
                "input_tokens": 1000,
                "cache_write_tokens": 200,
                "cache_read_tokens": 3000,
                "output_tokens": 400,
            },
        },
    )
    _run_hook(
        "savings_reporter.py",
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "mcp__atelier__edit",
            "tool_input": {"edits": [{"file_path": "a.py"}, {"file_path": "b.py"}]},
        },
    )
    from atelier.core.foundation.paths import session_dir

    sess_dir = session_dir(root, "codex", "c1")
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "savings.jsonl").write_text(
        json.dumps({"tokens": 500, "calls": 2, "model": "gpt-5"}) + "\n",
        encoding="utf-8",
    )

    result = _run_hook("stop.py", root, {"hook_event_name": "Stop", "session_id": "c1"})

    output = json.loads(result.stdout)
    assert set(output) == {"systemMessage"}
    message = output["systemMessage"]
    assert "Atelier session complete." in message
    assert "0 LLM turns · 1 prompt turn · 1 tool call (hooks)" in message
    assert "est. cost: ~$" in message
    assert (
        "savings: $0.0006 · 500 tokens saved · 2 calls avoided · routing $0.0000 · carry $0.0000 / 0 tokens" in message
    )
    assert "tools: mcp__atelier__edit×1" in message


def test_codex_tool_summary_merges_atelier_mcp_aliases(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    plugin_runtime.update_session_stats(
        root,
        {"hook_event_name": "PostToolUse", "session_id": "c-tools", "tool_name": "bash"},
    )
    plugin_runtime.update_session_stats(
        root,
        {
            "hook_event_name": "StatuslineUpdate",
            "session_id": "c-tools",
            "tools_used": {
                "atelier.bash": 26,
                "bash": 26,
                "atelier.read": 23,
                "read": 23,
                "atelier.code_search": 15,
                "code_search": 15,
                "atelier.edit": 14,
                "edit": 15,
            },
            "total_tool_calls": 157,
            "tool_call_source": "transcript",
            "context_window": {"current_usage": {"input_tokens": 1}},
        },
    )

    result = plugin_runtime.build_codex_stop_output(root, {"hook_event_name": "Stop", "session_id": "c-tools"})

    message = result["systemMessage"]
    assert "tools: atelier.bash×26 · atelier.read×23 · atelier.code_search×15 · atelier.edit×14 · edit×1" in message
    assert " · bash×26" not in message
    assert " · read×23" not in message
    assert " · code_search×15" not in message


def test_codex_stop_hook_reads_status_style_token_fields(tmp_path: Path) -> None:
    from atelier.core.capabilities.pricing import override_pricing

    root = tmp_path / ".atelier"
    override_pricing(
        "codex-test-model",
        input_usd=1.0,
        output_usd=10.0,
        cache_read_usd=0.1,
        cache_write_usd=2.0,
    )

    result = plugin_runtime.build_codex_stop_output(
        root,
        {
            "hook_event_name": "Stop",
            "session_id": "c-status",
            "model": "codex-test-model",
            "tokens": {
                "input": "19.4M",
                "output": "61.1K",
                "cache": {"read": "2.5M", "write": "100k"},
            },
        },
    )

    message = result["systemMessage"]
    assert "tokens: 19.5M input (19.4M new + 100.0k cW) / 2.5M cR / 61.1k out  (22.1M total)" in message
    assert "est. cost: ~$20.4610" in message


def test_codex_stop_hook_uses_native_statusline_snapshot_without_session_id(tmp_path: Path) -> None:
    from atelier.core.capabilities.pricing import override_pricing

    root = tmp_path / ".atelier"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    override_pricing("codex-test-model", input_usd=1.0, output_usd=10.0)

    statusline = _run_statusline(
        root,
        "codex-test-model high · ~/workspace · 1.11M used · 19.4M in · 61.1K out",
        env_extra={"CODEX_WORKSPACE_ROOT": str(workspace)},
    )
    assert "codex-test-model high" in statusline.stdout

    result = plugin_runtime.build_codex_stop_output(
        root,
        {"hook_event_name": "Stop", "session_id": "c-native", "cwd": str(workspace)},
    )

    message = result["systemMessage"]
    assert "tokens: 19.4M input (19.4M new + 0 cW) / 0 cR / 61.1k out  (19.5M total)" in message
    assert "est. cost: ~$20.0110" in message


def test_codex_stop_hook_recovers_usage_from_local_codex_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atelier.core.capabilities.pricing import override_pricing

    root = tmp_path / ".atelier"
    codex_home = tmp_path / ".codex"
    workspace = tmp_path / "workspace"
    transcript_dir = codex_home / "sessions" / "2026" / "06" / "16"
    transcript_dir.mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    override_pricing("codex-test-model", input_usd=1.0, output_usd=10.0, cache_read_usd=0.1)

    transcript = transcript_dir / "rollout-2026-06-16T21-42-38-session-abc.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "session-abc", "cwd": str(workspace)},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {"model": "codex-test-model", "cwd": str(workspace)},
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": '{"cmd": "rg foo"}',
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 1000,
                                    "cached_input_tokens": 700,
                                    "output_tokens": 50,
                                },
                                "total_token_usage": {
                                    "input_tokens": 1000,
                                    "cached_input_tokens": 700,
                                    "output_tokens": 50,
                                    "reasoning_output_tokens": 30,
                                    "total_tokens": 1050,
                                },
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "apply_patch",
                            "arguments": '{"patch": "*** Begin Patch\\n*** End Patch"}',
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 300,
                                    "cached_input_tokens": 100,
                                    "output_tokens": 20,
                                },
                                "total_token_usage": {
                                    "input_tokens": 1300,
                                    "cached_input_tokens": 800,
                                    "output_tokens": 70,
                                    "reasoning_output_tokens": 40,
                                    "total_tokens": 1370,
                                },
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plugin_runtime.update_session_stats(
        root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "atelier-session",
            "turn_id": "human-prompt-1",
        },
    )
    plugin_runtime.update_session_stats(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "atelier-session",
            "tool_name": "Bash",
        },
    )

    result = plugin_runtime.build_codex_stop_output(
        root,
        {"hook_event_name": "Stop", "session_id": "atelier-session", "cwd": str(workspace)},
    )

    message = result["systemMessage"]
    assert "2 LLM turns · 2 tool calls (transcript)" in message
    assert "tokens: 500 input (500 new + 0 cW) / 800 cR / 70 out  (1.4k total)" in message
    assert (
        "token breakdown: new input 500 · cache read 800 · cache write 0 · output 70 (40 reasoning, 30 visible)"
        in message
    )
    assert "est. cost: ~$0.0013" in message
    assert "tools: apply_patch×1 · exec_command×1" in message


def test_codex_stop_hook_is_quiet_without_session_activity(tmp_path: Path) -> None:
    result = _run_hook("stop.py", tmp_path / ".atelier", {"hook_event_name": "Stop", "session_id": "c1"})

    assert result.stdout == ""


def test_codex_session_start_is_quiet_and_records_session(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    result = _run_hook(
        "update_notification.py",
        root,
        {"hook_event_name": "SessionStart", "session_id": "c1", "cwd": str(cwd)},
    )

    assert result.stdout == ""
    state_files = list((root / "workspaces").glob("*/session_state.json"))
    assert len(state_files) == 1
    assert json.loads(state_files[0].read_text(encoding="utf-8"))["session_id"] == "c1"


def test_codex_hooks_manifest_wires_reporter_and_update() -> None:
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    assert "SessionStart" in data["hooks"]
    assert "UserPromptSubmit" in data["hooks"]
    assert "PreToolUse" in data["hooks"]
    assert "PostToolUse" in data["hooks"]
    assert "SubagentStart" in data["hooks"]
    assert "SubagentStop" in data["hooks"]
    assert "Stop" in data["hooks"]
    rendered = json.dumps(data)
    assert "update_notification.py" in rendered
    assert "user_prompt.py" in rendered
    assert "pre_tool_use.py" in rendered
    assert "savings_reporter.py" in rendered
    assert "subagent.py" in rendered
    assert "stop.py" in rendered
    assert "compact.py" in rendered
    assert "${PLUGIN_ROOT}/hooks/" in rendered
    assert "__ATELIER_PYTHON__" in rendered
    assert "__ATELIER_REPO_SRC__" in rendered
    assert "ATELIER_CODEX_PLUGIN_ROOT" not in rendered
    for event in ("PreCompact", "PostCompact", "SubagentStart", "SubagentStop"):
        assert event in data["hooks"]


def test_codex_compact_hook_bumps_epoch(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    cwd = tmp_path / "ws"
    cwd.mkdir()

    result = _run_hook(
        "compact.py",
        root,
        {"hook_event_name": "PostCompact", "session_id": "c1", "cwd": str(cwd), "trigger": "auto"},
    )

    assert result.stdout == ""
    state_files = list((root / "workspaces").glob("*/session_state.json"))
    assert len(state_files) == 1
    assert json.loads(state_files[0].read_text(encoding="utf-8"))["compaction_epoch"] == 1


def test_codex_savings_reporter_is_fail_open_on_unwritable_root(tmp_path: Path) -> None:
    # ATELIER_ROOT points at a regular file, so session_stats writes raise OSError.
    # The hook MUST still exit 0 (fail-open) rather than crash with a traceback.
    bad_root = tmp_path / "rootfile"
    bad_root.write_text("not a directory", encoding="utf-8")
    env = os.environ.copy()
    env.update({"ATELIER_ROOT": str(bad_root), "ATELIER_CTX_NUDGE_TOKENS": "999999999"})
    result = subprocess.run(
        [sys.executable, str(HOOKS / "savings_reporter.py")],
        input=json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "c1",
                "tool_name": "mcp__atelier__edit",
                "tool_input": {"file_path": "a.py"},
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
