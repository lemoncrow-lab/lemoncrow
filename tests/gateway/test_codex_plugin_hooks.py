from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from atelier.infra.runtime.run_ledger import RunLedger

pytestmark = pytest.mark.slow  # Each test spawns a real Python subprocess (~2s each)

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "integrations" / "codex" / "hooks"


def _run_hook(
    script: str, root: Path, payload: dict[str, Any], version: str = "1.0.0"
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"ATELIER_ROOT": str(root), "ATELIER_VERSION": version})
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )


def test_codex_savings_reporter_updates_session_stats(tmp_path: Path) -> None:
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

    output = json.loads(result.stdout)
    stats = json.loads((root / "session_stats" / "c1.json").read_text(encoding="utf-8"))
    assert "Atelier saved" in output["systemMessage"]
    assert stats["total_tool_calls"] == 1
    assert stats["savings"]["calls_saved"] > 0


def test_codex_savings_reporter_emits_no_edit_progress_nudge_once(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _run_hook(
        "savings_reporter.py",
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "mcp__plugin_atelier_atelier__Search",
            "tool_input": {},
            "now_ms": 1_000,
        },
    )

    first = _run_hook(
        "savings_reporter.py",
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "mcp__plugin_atelier_atelier__Search",
            "tool_input": {},
            "now_ms": 601_001,
        },
    )
    second = _run_hook(
        "savings_reporter.py",
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "mcp__plugin_atelier_atelier__Search",
            "tool_input": {},
            "now_ms": 601_002,
        },
    )

    first_output = json.loads(first.stdout)
    second_output = json.loads(second.stdout)

    assert "10 minutes" in first_output["additionalContext"]
    assert "additionalContext" not in second_output


def test_codex_savings_reporter_emits_loop_rescue_nudge(tmp_path: Path) -> None:
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

    output = json.loads(result.stdout)
    assert "loop detector" in output["additionalContext"].lower()
    assert "change approach" in output["message"].lower()


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


def test_codex_stop_hook_emits_session_summary(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _run_hook(
        "savings_reporter.py",
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "mcp__plugin_atelier_atelier__Edit",
            "tool_input": {"edits": [{"file_path": "a.py"}, {"file_path": "b.py"}]},
        },
    )

    result = _run_hook("stop.py", root, {"hook_event_name": "Stop", "session_id": "c1"})

    output = json.loads(result.stdout)
    assert "Atelier session complete." in output["systemMessage"]
    assert "calls avoided" in output["systemMessage"]
    assert "Atelier tool calls: 1" in output["systemMessage"]


def test_codex_stop_hook_is_quiet_without_session_activity(tmp_path: Path) -> None:
    result = _run_hook("stop.py", tmp_path / ".atelier", {"hook_event_name": "Stop", "session_id": "c1"})

    assert result.stdout == ""


def test_codex_update_notification_outputs_sessionstart_message(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    root.mkdir()
    (root / "update.json").write_text(
        json.dumps({"fromVersion": "1.0.0", "toVersion": "1.1.0"}),
        encoding="utf-8",
    )

    result = _run_hook("update_notification.py", root, {"hook_event_name": "SessionStart"}, version="1.0.0")

    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Atelier v1.1.0" in output["message"]
    assert "Atelier budget optimizer" in output["additionalContext"]


def test_codex_update_notification_outputs_optimizer_without_update(tmp_path: Path) -> None:
    result = _run_hook("update_notification.py", tmp_path / ".atelier", {"hook_event_name": "SessionStart"})

    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "smallest viable plan" in output["additionalContext"]


def test_codex_hooks_manifest_wires_reporter_and_update() -> None:
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    assert "SessionStart" in data["hooks"]
    assert "PostToolUse" in data["hooks"]
    assert "Stop" in data["hooks"]
    rendered = json.dumps(data)
    assert "update_notification.py" in rendered
    assert "savings_reporter.py" in rendered
    assert "stop.py" in rendered
