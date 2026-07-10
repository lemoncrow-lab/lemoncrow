"""CLI tests for ``atelier replay`` (session reconstruction, no model run).

Builds a synthetic Claude transcript with a grep->read loop and drives the CLI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from pathlib import Path as _Path

from click.testing import CliRunner

_REPO_ROOT = _Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from atelier.gateway.cli import cli  # noqa: E402


def _assistant(msg_id: str, name: str, tool_id: str, inp: dict[str, object]) -> dict[str, object]:
    block = {"type": "tool_use", "id": tool_id, "name": name, "input": inp}
    return {"type": "assistant", "message": {"id": msg_id, "content": [block], "usage": {}}}


def _tool_result(tool_id: str, text: str) -> dict[str, object]:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": text}
    return {"type": "user", "message": {"content": [block]}}


def _transcript() -> str:
    events: list[dict[str, object]] = [
        {"type": "user", "sessionId": "s1", "message": {"content": "find TokenRefresh"}},
        _assistant("m1", "Grep", "t1", {"pattern": "TokenRefresh"}),
        _tool_result("t1", "18 files matched"),
        _assistant("m2", "Read", "t2", {"file_path": "auth/token.py"}),
        _assistant("m3", "Read", "t3", {"file_path": "auth/mid.py"}),
        _assistant("m4", "Edit", "t4", {"file_path": "auth/token.py", "old_string": "a", "new_string": "b"}),
    ]
    return "\n".join(json.dumps(e) for e in events)


def _write(tmp_path: Path) -> Path:
    f = tmp_path / "session.jsonl"
    f.write_text(_transcript(), encoding="utf-8")
    return f


def test_replay_help_exits_zero() -> None:
    result = CliRunner().invoke(cli, ["replay", "--help"])
    assert result.exit_code == 0, result.output
    assert "no model is re-run" in result.output.lower()
    assert "--html" in result.output


def test_replay_file_text(tmp_path: Path) -> None:
    f = _write(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "replay",
            "--file",
            str(f),
            "--host",
            "claude",
            "--no-color",
            "--no-live",
            "--no-open",
            "--html",
            str(tmp_path / "o.html"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "code_search(" in result.output
    assert "collapses 3 calls" in result.output


def test_replay_file_json(tmp_path: Path) -> None:
    f = _write(tmp_path)
    result = CliRunner().invoke(cli, ["replay", "--file", str(f), "--json", "--no-live"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    replay = data["replays"][0]
    assert replay["summary"]["episode_count"] == 1
    assert replay["summary"]["calls_saved"] == 2
    assert replay["episodes"][0]["query"] == "TokenRefresh"


def test_replay_html_output(tmp_path: Path) -> None:
    f = _write(tmp_path)
    out = tmp_path / "replay.html"
    result = CliRunner().invoke(cli, ["replay", "--file", str(f), "--html", str(out), "--no-live", "--no-open"])
    assert result.exit_code == 0, result.output
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "code_search" in html
    assert "turn cut" in html


def test_replay_missing_session_exits_1() -> None:
    result = CliRunner().invoke(cli, ["replay", "--session-id", "no-such-session-xyz", "--host", "claude"])
    assert result.exit_code == 1
    assert "no transcript found" in result.output.lower()
