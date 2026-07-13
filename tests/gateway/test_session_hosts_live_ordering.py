from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lemoncrow.core.foundation.models import Trace
from lemoncrow.gateway.cli.commands import sessions as sessions_cmd
from lemoncrow.gateway.cli.commands.sessions import _pick_live_sessions
from lemoncrow.infra.storage.bundle import StoreBundle, build_sqlite_store_bundle


def _touch_with_mtime(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_pick_live_sessions_sorts_by_mtime_desc_and_stops_at_limit(tmp_path: Path) -> None:
    oldest = tmp_path / "old.jsonl"
    newest = tmp_path / "new.jsonl"
    middle = tmp_path / "mid.jsonl"
    _touch_with_mtime(oldest, 1000.0)
    _touch_with_mtime(middle, 2000.0)
    _touch_with_mtime(newest, 3000.0)

    selected = _pick_live_sessions(
        [oldest, newest, middle],
        path_of=lambda p: p,
        limit=2,
        scan=100,
    )

    assert selected == [newest, middle]


def test_pick_live_sessions_supports_tuple_items(tmp_path: Path) -> None:
    a = tmp_path / "workspace-a" / "a.jsonl"
    b = tmp_path / "workspace-b" / "b.jsonl"
    c = tmp_path / "workspace-c" / "c.jsonl"
    _touch_with_mtime(a, 1100.0)
    _touch_with_mtime(b, 3300.0)
    _touch_with_mtime(c, 2200.0)

    sessions = [("workspace-a", a), ("workspace-b", b), ("workspace-c", c)]
    selected = _pick_live_sessions(
        sessions,
        path_of=lambda item: item[1],
        limit=2,
        scan=100,
    )

    assert selected == [("workspace-b", b), ("workspace-c", c)]


def test_pick_live_sessions_filters_by_stem_before_limit(tmp_path: Path) -> None:
    """--id filtering happens pre-import so older matches are still reached."""
    match_old = tmp_path / "target-abc.jsonl"
    other_new = tmp_path / "other-1.jsonl"
    other_newer = tmp_path / "other-2.jsonl"
    _touch_with_mtime(match_old, 1000.0)
    _touch_with_mtime(other_new, 2000.0)
    _touch_with_mtime(other_newer, 3000.0)

    selected = _pick_live_sessions(
        [match_old, other_new, other_newer],
        path_of=lambda p: p,
        limit=1,
        scan=100,
        session_filter="target",
    )

    assert selected == [match_old]


def test_pick_live_sessions_stops_at_since_cutoff(tmp_path: Path) -> None:
    recent = tmp_path / "recent.jsonl"
    ancient = tmp_path / "ancient.jsonl"
    now = datetime.now(UTC).timestamp()
    _touch_with_mtime(recent, now)
    _touch_with_mtime(ancient, 1000.0)

    selected = _pick_live_sessions(
        [recent, ancient],
        path_of=lambda p: p,
        limit=10,
        scan=100,
        cutoff=datetime.fromtimestamp(now - 3600, tz=UTC),
    )

    assert selected == [recent]


def test_pick_live_sessions_scan_bounds_candidates(tmp_path: Path) -> None:
    """A match older than the --scan window is not reached."""
    match_old = tmp_path / "target-abc.jsonl"
    other_new = tmp_path / "other-1.jsonl"
    _touch_with_mtime(match_old, 1000.0)
    _touch_with_mtime(other_new, 2000.0)

    selected = _pick_live_sessions(
        [match_old, other_new],
        path_of=lambda p: p,
        limit=1,
        scan=1,
        session_filter="target",
    )

    assert selected == []


def test_file_backed_live_import_filters_since_by_trace_created_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from lemoncrow.gateway.hosts.session_parsers import codex as codex_module

    now = datetime.now(UTC)
    old_path = tmp_path / "old-session.jsonl"
    new_path = tmp_path / "new-session.jsonl"
    _touch_with_mtime(old_path, now.timestamp())
    _touch_with_mtime(new_path, now.timestamp() - 1)

    class FakeCodexImporter:
        def __init__(self, store: StoreBundle) -> None:
            self.store = store

        def import_session(self, session_path: Path, *, force: bool = False) -> str:
            created_at = now - timedelta(days=30) if session_path == old_path else now - timedelta(hours=1)
            trace = Trace(
                id=session_path.stem,
                session_id=session_path.stem,
                agent="codex",
                domain="coding",
                task="fake session",
                status="success",
                host="codex",
                created_at=created_at,
            )
            self.store.history.record_trace(trace, write_json=False)
            return session_path.stem

    monkeypatch.setattr(codex_module, "CodexImporter", FakeCodexImporter)
    monkeypatch.setattr(codex_module, "find_codex_sessions", lambda path=None: [old_path, new_path])

    store = build_sqlite_store_bundle(tmp_path / "store")
    store.init()
    imported = sessions_cmd._import_live_host_sessions(
        host_name="codex",
        importer_cls=object,
        store=store,
        path=None,
        force=False,
        max_per_host=10,
        limit=5,
        cutoff=now - timedelta(days=7),
    )

    assert imported == ["new-session"]
