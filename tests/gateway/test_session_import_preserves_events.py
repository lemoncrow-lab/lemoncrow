"""Session import must merge into run.json, never overwrite it.

``persist_imported_run_snapshot`` and the PostToolUse hooks write the same
file from opposite ends. The importer knows the cost roll-up and the task; only
the hooks know who edited what, when, and against which HEAD -- and those are
unreconstructable, because HEAD moves and the model id is never repeated.

A bare ``write_text`` of an importer payload carrying ``"events": []`` erased
every one of them on the next ``lc usage``, which is why exact provenance was
worthless before this landed: the write side worked and the next import undid
it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from lemoncrow.core.foundation.models import Trace
from lemoncrow.core.foundation.paths import session_dir
from lemoncrow.gateway.hosts.session_parsers._common import persist_imported_run_snapshot

_STARTED = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
_ENDED = datetime(2026, 9, 7, 11, 0, tzinfo=UTC)
_SESSION = "sid-import-1"
_HEAD = "a" * 40


def _store(root: Path) -> Any:
    """``persist_imported_run_snapshot`` reads exactly one attribute off the bundle."""

    return cast(Any, SimpleNamespace(history=SimpleNamespace(root=root)))


def _trace() -> Trace:
    return Trace(
        id="t1",
        session_id=_SESSION,
        agent="claude",
        host="claude",
        domain="code",
        task="import me",
        status="success",
        created_at=_STARTED,
    )


def _hook_written_run_json() -> dict[str, Any]:
    """What the PostToolUse hook leaves behind: three edits and a git anchor."""

    return {
        "session_id": _SESSION,
        "agent": "claude",
        "git": {"head": _HEAD, "branch": "feat/x", "repo_root": "/repo"},
        "files_touched": ["/repo/src/a.py", "/repo/src/b.py", "/repo/src/c.py"],
        "events": [
            {
                "kind": "file_edit",
                "at": f"2026-09-07T10:3{index}:00+00:00",
                "summary": f"edited {name}",
                "payload": {
                    "path": f"/repo/src/{name}",
                    "diff": "@@\n",
                    "event": "PostToolUse",
                    "session_id": _SESSION,
                    "host": "claude",
                    "model": "claude-opus-5",
                    "at_head": _HEAD,
                },
            }
            for index, name in enumerate(("a.py", "b.py", "c.py"))
        ],
    }


def _seed(root: Path) -> Path:
    run_dir = session_dir(root, "claude", _SESSION)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "run.json"
    path.write_text(json.dumps(_hook_written_run_json()), encoding="utf-8")
    return path


def test_import_preserves_every_hook_written_event(tmp_path: Path) -> None:
    root = tmp_path / "store"
    _seed(root)

    path = persist_imported_run_snapshot(_store(root), _trace(), started_at=_STARTED, ended_at=_ENDED)

    data = json.loads(path.read_text(encoding="utf-8"))
    edits = [event for event in data["events"] if event["kind"] == "file_edit"]
    assert len(edits) == 3
    assert [event["payload"]["path"] for event in edits] == [
        "/repo/src/a.py",
        "/repo/src/b.py",
        "/repo/src/c.py",
    ]
    # The authoring facts survive intact, key for key.
    assert edits[0]["payload"]["model"] == "claude-opus-5"
    assert edits[0]["payload"]["at_head"] == _HEAD
    assert edits[0]["at"] == "2026-09-07T10:30:00+00:00"


def test_import_preserves_the_git_anchor_and_files_touched(tmp_path: Path) -> None:
    root = tmp_path / "store"
    _seed(root)

    path = persist_imported_run_snapshot(_store(root), _trace(), started_at=_STARTED, ended_at=_ENDED)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["git"] == {"head": _HEAD, "branch": "feat/x", "repo_root": "/repo"}
    assert data["files_touched"] == ["/repo/src/a.py", "/repo/src/b.py", "/repo/src/c.py"]


def test_import_still_writes_the_keys_only_it_knows(tmp_path: Path) -> None:
    root = tmp_path / "store"
    _seed(root)

    path = persist_imported_run_snapshot(_store(root), _trace(), started_at=_STARTED, ended_at=_ENDED)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["task"] == "import me"
    assert data["status"] == "done"
    assert data["created_at"] == _STARTED.isoformat()
    assert data["updated_at"] == _ENDED.isoformat()
    assert "cost" in data


def test_import_with_no_existing_file_still_writes_an_events_key(tmp_path: Path) -> None:
    root = tmp_path / "store"

    path = persist_imported_run_snapshot(_store(root), _trace(), started_at=_STARTED, ended_at=_ENDED)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["events"] == []
    assert data["session_id"] == _SESSION


def test_a_corrupt_run_json_does_not_stop_the_import(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_dir = session_dir(root, "claude", _SESSION)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text("{not json", encoding="utf-8")

    path = persist_imported_run_snapshot(_store(root), _trace(), started_at=_STARTED, ended_at=_ENDED)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["events"] == []


def test_two_imports_in_a_row_do_not_erode_the_events(tmp_path: Path) -> None:
    # `lc usage` runs the importer repeatedly; the merge has to be idempotent
    # rather than merely surviving the first pass.
    root = tmp_path / "store"
    _seed(root)

    persist_imported_run_snapshot(_store(root), _trace(), started_at=_STARTED, ended_at=_ENDED)
    path = persist_imported_run_snapshot(_store(root), _trace(), started_at=_STARTED, ended_at=_ENDED)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert len([event for event in data["events"] if event["kind"] == "file_edit"]) == 3
