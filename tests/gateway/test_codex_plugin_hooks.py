from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

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


def test_codex_hooks_manifest_wires_reporter_and_update() -> None:
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    assert "SessionStart" in data["hooks"]
    assert "PostToolUse" in data["hooks"]
    rendered = json.dumps(data)
    assert "update_notification.py" in rendered
    assert "savings_reporter.py" in rendered
