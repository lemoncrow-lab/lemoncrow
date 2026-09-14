"""What the Claude PostToolUse hook writes down at the moment of an edit.

This is the write half of "exact provenance at authoring time". Nothing
downstream can reconstruct these four facts -- HEAD moves, the model id is
never repeated after SessionStart, and the host is only knowable from inside
the host -- so if the hook does not stamp them here they are gone.

Two behaviours matter as much as the payload itself: the hook must CREATE
run.json when the MCP server has not written one yet (the bug that left 57 of
61 recent claude sessions with no events at all), and it must never raise into
the agent whatever it finds on disk.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, cast

import pytest

from integrations.claude.plugin.hooks import post_tool_use
from lemoncrow.core.foundation import session_window as sw
from lemoncrow.core.foundation.paths import session_dir

HOOK = cast(Any, post_tool_use)

_SESSION = "sid-exact-1"
_HEAD = "0123456789abcdef0123456789abcdef01234567"

_PAYLOAD_KEYS = {"path", "diff", "event", "session_id", "host", "model", "at_head"}


def _setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, model: str | None = "claude-opus-5"
) -> tuple[Path, Path]:
    """Isolated store root + workspace, with a fake checkout and session state."""

    root = tmp_path / "lemoncrow"
    workspace = tmp_path / "ws"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / "HEAD").write_text(f"{_HEAD}\n", encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    # No window identity file in these tests: the session_state read is the
    # documented primary source and the window file is the fallback.
    monkeypatch.setattr(sw, "host_window_id", lambda: None)
    if model is not None:
        state = workspace / ".lemoncrow" / "workspace" / "session_state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"session_id": _SESSION, "model": model}), encoding="utf-8")
    return root, workspace


def _run_json(root: Path, session_id: str = _SESSION) -> dict[str, Any]:
    path = session_dir(root, "claude", session_id) / "run.json"
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _file_edits(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for event in data["events"] if event["kind"] == "file_edit"]


# ----- the payload --------------------------------------------------------- #


def test_hook_creates_run_json_when_the_mcp_server_has_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace = _setup(tmp_path, monkeypatch)
    assert not (root / "sessions").exists()

    HOOK._append_file_edit_event(_SESSION, str(workspace / "src/app.py"), "@@ -1 +1 @@\n-a\n+b\n")

    data = _run_json(root)
    assert data["session_id"] == _SESSION
    assert data["agent"] == "claude"
    assert len(_file_edits(data)) == 1


def test_payload_carries_every_authoring_fact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace = _setup(tmp_path, monkeypatch)
    target = str(workspace / "src/app.py")

    HOOK._append_file_edit_event(_SESSION, target, "@@ -1 +1 @@\n-a\n+b\n")

    event = _file_edits(_run_json(root))[0]
    assert event["kind"] == "file_edit"
    assert event["at"]  # the timestamp is what makes this a record, not a guess
    assert event["summary"] == "edited app.py"
    payload = event["payload"]
    assert set(payload) == _PAYLOAD_KEYS
    assert payload["path"] == target
    assert payload["event"] == "PostToolUse"
    assert payload["session_id"] == _SESSION
    assert payload["host"] == "claude"
    assert payload["model"] == "claude-opus-5"
    assert payload["at_head"] == _HEAD
    assert _run_json(root)["files_touched"] == [target]


def test_a_second_edit_appends_rather_than_replaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace = _setup(tmp_path, monkeypatch)
    HOOK._append_file_edit_event(_SESSION, str(workspace / "a.py"), "diff-a")
    HOOK._append_file_edit_event(_SESSION, str(workspace / "b.py"), "diff-b")

    data = _run_json(root)
    assert [event["payload"]["path"] for event in _file_edits(data)] == [
        str(workspace / "a.py"),
        str(workspace / "b.py"),
    ]
    assert len(data["files_touched"]) == 2


def test_an_existing_run_json_keeps_the_events_it_already_had(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace = _setup(tmp_path, monkeypatch)
    run_file = session_dir(root, "claude", _SESSION) / "run.json"
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text(
        json.dumps({"session_id": _SESSION, "events": [{"kind": "note", "summary": "prior", "payload": {}}]}),
        encoding="utf-8",
    )

    HOOK._append_file_edit_event(_SESSION, str(workspace / "a.py"), "diff-a")

    kinds = [event["kind"] for event in _run_json(root)["events"]]
    assert kinds == ["note", "file_edit"]


def test_an_oversized_diff_is_truncated_not_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace = _setup(tmp_path, monkeypatch)
    HOOK._append_file_edit_event(_SESSION, str(workspace / "a.py"), "x" * 9000)

    diff = _file_edits(_run_json(root))[0]["payload"]["diff"]
    assert "diff truncated" in diff
    assert len(diff) < 9000


# ----- the model is read, never guessed ------------------------------------ #


def test_model_is_blank_when_the_session_state_names_another_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # session_state.json is workspace-shared: a sibling window's model is not
    # this session's model, and writing it would be a guess dressed as a fact.
    root, workspace = _setup(tmp_path, monkeypatch, model=None)
    state = workspace / ".lemoncrow" / "workspace" / "session_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"session_id": "some-other-window", "model": "claude-haiku-4"}), encoding="utf-8")

    HOOK._append_file_edit_event(_SESSION, str(workspace / "a.py"), "diff-a")

    assert _file_edits(_run_json(root))[0]["payload"]["model"] == ""


def test_model_is_blank_when_nothing_recorded_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace = _setup(tmp_path, monkeypatch, model=None)

    HOOK._append_file_edit_event(_SESSION, str(workspace / "a.py"), "diff-a")

    payload = _file_edits(_run_json(root))[0]["payload"]
    assert payload["model"] == ""
    # Everything the hook *can* know is still recorded: a missing model does
    # not cost the rest of the anchor.
    assert payload["host"] == "claude"
    assert payload["at_head"] == _HEAD


def test_model_falls_back_to_this_window_identity_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace = _setup(tmp_path, monkeypatch, model=None)
    monkeypatch.setattr(sw, "host_window_id", lambda: (43210, 987654))
    sw.register_window_session(
        root,
        sw.workspace_hash(str(workspace)),
        session_id=_SESSION,
        source="startup",
        model="claude-sonnet-4-5",
    )

    HOOK._append_file_edit_event(_SESSION, str(workspace / "a.py"), "diff-a")

    assert _file_edits(_run_json(root))[0]["payload"]["model"] == "claude-sonnet-4-5"


# ----- at_head ------------------------------------------------------------- #


def test_at_head_is_blank_outside_a_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "lemoncrow"
    workspace = tmp_path / "plain"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(sw, "host_window_id", lambda: None)

    HOOK._append_file_edit_event(_SESSION, str(workspace / "a.py"), "diff-a")

    assert _file_edits(_run_json(root))[0]["payload"]["at_head"] == ""


# ----- fail-open ----------------------------------------------------------- #


def test_a_corrupt_run_json_is_left_alone_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace = _setup(tmp_path, monkeypatch)
    run_file = session_dir(root, "claude", _SESSION) / "run.json"
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text("{not json", encoding="utf-8")

    HOOK._append_file_edit_event(_SESSION, str(workspace / "a.py"), "diff-a")

    assert run_file.read_text(encoding="utf-8") == "{not json"


def test_main_returns_zero_for_a_non_edit_tool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Read", "session_id": _SESSION})))
    assert HOOK.main() == 0
    assert capsys.readouterr().out == ""


def test_main_returns_zero_on_garbage_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert HOOK.main() == 0
