from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "integrations" / "copilot-cli" / "hooks"


def _run_failure(root: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ATELIER_ROOT"] = str(root)
    return subprocess.run(
        [sys.executable, str(HOOKS / "post_tool_use_failure.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_copilot_repeated_failure_injects_rescue_on_second_match(tmp_path: Path) -> None:
    payload = {
        "sessionId": "s1",
        "toolName": "bash",
        "toolArgs": {"command": "make test"},
        "error": "same failure",
    }

    first = _run_failure(tmp_path / ".atelier", payload)
    second = _run_failure(tmp_path / ".atelier", payload)

    assert first.returncode == 0
    assert first.stdout == ""
    assert second.returncode == 2
    assert "Call 'rescue' before any retry" in second.stdout


def test_copilot_different_failure_does_not_trigger_rescue(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    base = {
        "sessionId": "s1",
        "toolName": "bash",
        "toolArgs": {"command": "make test"},
    }

    first = _run_failure(root, {**base, "error": "failure one"})
    second = _run_failure(root, {**base, "error": "failure two"})

    assert first.returncode == 0
    assert second.returncode == 0
    assert second.stdout == ""


def test_copilot_hooks_manifest_wires_failure_hook() -> None:
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    rendered = json.dumps(data)
    assert "postToolUseFailure" in data["hooks"]
    assert "post_tool_use_failure.py" in rendered
