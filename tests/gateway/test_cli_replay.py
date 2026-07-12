"""CLI tests for ``lc replay`` (session reconstruction, no model run).

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

from lemoncrow.gateway.cli import cli  # noqa: E402


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
    result = CliRunner().invoke(cli, ["session", "replay", "--help"])
    assert result.exit_code == 0, result.output
    assert "no model is re-run" in result.output.lower()
    assert "--html" in result.output


def test_replay_file_text(tmp_path: Path) -> None:
    f = _write(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "session",
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
    result = CliRunner().invoke(cli, ["session", "replay", "--file", str(f), "--json", "--no-live"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    replay = data["replays"][0]
    assert replay["summary"]["episode_count"] == 1
    assert replay["summary"]["calls_saved"] == 2
    assert replay["episodes"][0]["query"] == "TokenRefresh"


def test_replay_html_output(tmp_path: Path) -> None:
    f = _write(tmp_path)
    out = tmp_path / "replay.html"
    result = CliRunner().invoke(
        cli, ["session", "replay", "--file", str(f), "--html", str(out), "--no-live", "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "code_search" in html
    assert "turn cut" in html


def _codex_transcript() -> str:
    events: list[dict[str, object]] = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "find the bug"}]},
        {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "grep -rn foo ."})},
        {"type": "function_call", "name": "read_file", "arguments": json.dumps({"path": "a.py"})},
        {
            "type": "message",
            "role": "assistant",
            "model": "gpt-5",
            "usage": {"input_tokens": 900, "output_tokens": 40},
            "content": [{"type": "text", "text": "found it"}],
        },
    ]
    return "\n".join(json.dumps(e) for e in events)


def test_replay_file_autodetects_codex(tmp_path: Path) -> None:
    # A codex rollout passed via --file WITHOUT --host must parse as codex,
    # not render an empty "claude · unknown model" replay.
    f = tmp_path / "rollout.jsonl"
    f.write_text(_codex_transcript(), encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "session",
            "replay",
            "--file",
            str(f),
            "--no-color",
            "--no-live",
            "--no-open",
            "--html",
            str(tmp_path / "o.html"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "codex" in result.output  # detection note + session header
    assert "find the bug" in result.output  # turns actually parsed


def test_replay_file_unparseable_exits_1(tmp_path: Path) -> None:
    f = tmp_path / "junk.jsonl"
    f.write_text("this is not a transcript\nnope\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["session", "replay", "--file", str(f), "--no-live", "--no-open"])
    assert result.exit_code == 1
    assert "could not parse" in result.output.lower()


def test_replay_file_explicit_host_bypasses_detection(tmp_path: Path) -> None:
    # Explicit --host wins; a mismatched format renders with a 0-turn warning.
    f = tmp_path / "rollout.jsonl"
    f.write_text(_codex_transcript(), encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "session",
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
    assert "no turns parsed" in result.output


def _make_opencode_db(db: Path) -> None:
    import sqlite3

    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, time_created INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT, data TEXT, time_created INTEGER);
        """)
    conn.execute("INSERT INTO session VALUES ('ses_abc123', 200)")
    conn.execute(
        "INSERT INTO message VALUES ('m1', 'ses_abc123', ?, 201)",
        (json.dumps({"role": "user", "text": "find the parser bug"}),),
    )
    conn.execute("INSERT INTO message VALUES ('m2', 'ses_abc123', ?, 202)", (json.dumps({"role": "assistant"}),))
    for pid, tool, inp, ts in [
        ("p1", "grep", {"pattern": "parse_session"}, 203),
        ("p2", "read", {"filePath": "parser.py"}, 204),
        ("p3", "edit", {"filePath": "parser.py", "old_string": "x", "new_string": "y"}, 205),
    ]:
        conn.execute(
            "INSERT INTO part VALUES (?, 'ses_abc123', 'm2', ?, ?)",
            (pid, json.dumps({"type": "tool", "tool": tool, "state": {"input": inp}}), ts),
        )
    conn.commit()
    conn.close()


def test_replay_opencode_from_db(tmp_path: Path) -> None:
    # opencode stores sessions in opencode.db (SQLite), not *.jsonl files.
    xdg = tmp_path / "xdg"
    _make_opencode_db(xdg / "opencode" / "opencode.db")
    result = CliRunner().invoke(
        cli,
        [
            "session",
            "replay",
            "--host",
            "opencode",
            "--last",
            "1",
            "--no-color",
            "--no-live",
            "--no-open",
            "--html",
            str(tmp_path / "o.html"),
        ],
        env={"XDG_DATA_HOME": str(xdg)},
    )
    assert result.exit_code == 0, result.output
    assert "ses_abc123" in result.output
    assert "find the parser bug" in result.output


def test_replay_host_choices_cover_all_hosts() -> None:
    result = CliRunner().invoke(cli, ["session", "replay", "--help"])
    assert result.exit_code == 0
    for host in ("claude", "codex", "opencode", "copilot", "hermes", "cursor", "antigravity"):
        assert host in result.output


def test_replay_missing_session_exits_1() -> None:
    result = CliRunner().invoke(cli, ["session", "replay", "--session-id", "no-such-session-xyz", "--host", "claude"])
    assert result.exit_code == 1
    assert "no transcript found" in result.output.lower()
