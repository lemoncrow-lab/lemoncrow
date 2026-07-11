from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOOK = Path("integrations/claude/plugin/hooks/live_review.py")

_STUB = (
    "import os, sys\n"
    "with open(os.environ['REVIEW_MARKER'], 'a', encoding='utf-8') as f:\n"
    "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
)


def _setup(tmp_path: Path, settings: dict, n_edits: int) -> tuple[Path, Path]:
    root = tmp_path / ".lemoncrow"
    ws = tmp_path / "ws"
    ws.mkdir()
    from lemoncrow.core.foundation.paths import workspace_key

    state_dir = root / "workspaces" / workspace_key(ws)
    state_dir.mkdir(parents=True)
    (state_dir / "session_state.json").write_text(json.dumps({"session_id": "sid1"}), encoding="utf-8")
    (root / "plugin_settings.json").write_text(json.dumps(settings), encoding="utf-8")
    from lemoncrow.core.foundation.paths import session_dir

    runs = session_dir(root, "claude", "sid1")
    runs.mkdir(parents=True)
    (runs / "run.json").write_text(json.dumps({"events": [{"kind": "file_edit"}] * n_edits}), encoding="utf-8")
    return root, ws


def _env(root: Path, ws: Path, marker: Path, stub: Path, **extra: str) -> dict:
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(root)
    env["CLAUDE_WORKSPACE_ROOT"] = str(ws)
    env["PYTHONPATH"] = "src"
    env["REVIEW_MARKER"] = str(marker)
    env["LEMONCROW_REVIEWER_CHILD_CMD"] = f"{sys.executable} {stub}"
    env.update(extra)
    return env


def _run(env: dict) -> subprocess.CompletedProcess[str]:
    payload = {"session_id": "sid1", "tool_name": "Edit", "tool_input": {"file_path": "x.py"}}
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    return result


def _wait_marker(marker: Path, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker.exists():
            return marker.read_text(encoding="utf-8")
        time.sleep(0.1)
    return ""


def test_disabled_spawns_nothing(tmp_path: Path) -> None:
    root, ws = _setup(tmp_path, {"liveReviewer": False, "deepEditCountReviewer": False}, 1)
    stub = tmp_path / "stub.py"
    stub.write_text(_STUB, encoding="utf-8")
    marker = tmp_path / "marker.txt"
    _run(_env(root, ws, marker, stub))
    time.sleep(0.6)
    assert not marker.exists()


def test_live_spawns_live(tmp_path: Path) -> None:
    root, ws = _setup(tmp_path, {"liveReviewer": True}, 1)
    stub = tmp_path / "stub.py"
    stub.write_text(_STUB, encoding="utf-8")
    marker = tmp_path / "marker.txt"
    _run(_env(root, ws, marker, stub))
    out = _wait_marker(marker)
    assert "--mode live" in out
    assert "--path x.py" in out


def test_deep_spawns_at_interval(tmp_path: Path) -> None:
    root, ws = _setup(tmp_path, {"deepEditCountReviewer": True, "deepEditCountInterval": 5}, 5)
    stub = tmp_path / "stub.py"
    stub.write_text(_STUB, encoding="utf-8")
    marker = tmp_path / "marker.txt"
    _run(_env(root, ws, marker, stub))
    out = _wait_marker(marker)
    assert "--mode deep" in out


def test_in_review_env_guard_spawns_nothing(tmp_path: Path) -> None:
    root, ws = _setup(tmp_path, {"liveReviewer": True}, 1)
    stub = tmp_path / "stub.py"
    stub.write_text(_STUB, encoding="utf-8")
    marker = tmp_path / "marker.txt"
    _run(_env(root, ws, marker, stub, LEMONCROW_IN_REVIEW="1"))
    time.sleep(0.6)
    assert not marker.exists()
