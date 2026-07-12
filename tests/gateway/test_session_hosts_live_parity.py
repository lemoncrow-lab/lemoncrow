"""Regression test: JSON and text live-scan presenters must pick the same
session set for the same host/filters.

Verified finding (MAJOR, sessions.py `_scan_hosts_live` vs `_stream_hosts_live`):
before both presenters were unified onto a single `_import_live_host_sessions`
routine, generic hosts (antigravity, cursor) were imported directly by each
presenter with different pre-filtering -- the JSON path applied --since only
as a post-filter external to the scan function while the text path never
applied --since to generic hosts at all, and neither respected --id
consistently. `lc session list` vs `lc session list --json` could
therefore show different sessions for the identical query.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from lemoncrow.core.foundation.models import Trace
from lemoncrow.core.foundation.store import ContextStore
from lemoncrow.gateway.cli.commands import sessions as sessions_cmd
from lemoncrow.gateway.hosts.session_parsers import registry as registry_module

_NOW = datetime.now(UTC)


class _FakeGenericImporter:
    """Stand-in for a generic host importer (antigravity/cursor-shaped
    contract): ``import_all(path, force=..., limit=N)`` returns the N newest
    session ids, newest-first -- the contract ``_import_live_host_sessions``
    relies on for hosts with no per-file discovery of their own. Routing a
    fake host name through this class exercises exactly that (previously
    divergent) generic branch without depending on any real host's on-disk
    file format.
    """

    # 5 sessions, newest first: s0 is today, s4 is 4 days old.
    _SESSIONS: ClassVar[list[tuple[str, datetime]]] = [(f"s{i}", _NOW - timedelta(days=i)) for i in range(5)]

    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, path: Path | None = None, *, force: bool = False, limit: int | None = None) -> list[str]:
        picked = self._SESSIONS[:limit] if limit is not None else self._SESSIONS
        ids: list[str] = []
        for sid, created_at in picked:
            trace = Trace(
                id=sid,
                session_id=sid,
                agent="test-agent",
                domain="coding",
                task="fake session",
                status="success",
                host="fakehost",
                created_at=created_at,
            )
            self.store.record_trace(trace, write_json=False)
            ids.append(sid)
        return ids


@pytest.fixture(autouse=True)
def _fake_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route a fake host through the generic (non-special-cased) branch of
    _import_live_host_sessions -- the branch that previously diverged between
    _scan_hosts_live and _stream_hosts_live."""

    def fake_iter_importer_classes() -> list[tuple[str, type[Any]]]:
        return [("fakehost", _FakeGenericImporter)]

    monkeypatch.setattr(registry_module, "iter_importer_classes", fake_iter_importer_classes)


def _scan_session_ids(**kwargs: Any) -> list[str]:
    _counts, imported_by_host, _store, tmp = sessions_cmd._scan_hosts_live(
        selected_hosts=["fakehost"], force=False, path=None, **kwargs
    )
    try:
        return list(imported_by_host.get("fakehost", []))
    finally:
        tmp.cleanup()


def _stream_session_ids(root: Path, **kwargs: Any) -> list[str]:
    rows = sessions_cmd._stream_hosts_live(selected_hosts=["fakehost"], force=False, path=None, root=root, **kwargs)
    return [str(r["session_id"]) for r in rows]


def test_scan_and_stream_agree_with_no_filters(tmp_path: Path) -> None:
    scan_ids = _scan_session_ids(max_per_host=10, limit=3)
    stream_ids = _stream_session_ids(tmp_path, max_per_host=10, limit=3)

    assert scan_ids == ["s0", "s1", "s2"]
    assert stream_ids == scan_ids


def test_scan_and_stream_agree_with_since_cutoff(tmp_path: Path) -> None:
    """Regression: --since was previously ignored entirely by the text path's
    generic-host branch, so text showed sessions JSON correctly excluded."""
    cutoff = _NOW - timedelta(days=2, hours=12)  # excludes s3 (3d old), s4 (4d old)

    scan_ids = _scan_session_ids(max_per_host=10, limit=10, cutoff=cutoff)
    stream_ids = _stream_session_ids(tmp_path, max_per_host=10, limit=10, cutoff=cutoff)

    assert scan_ids == ["s0", "s1", "s2"]
    assert stream_ids == scan_ids


def test_scan_and_stream_agree_with_id_filter(tmp_path: Path) -> None:
    """Regression: --id (session_filter) was applied inconsistently across
    hosts and presenters before the unification."""
    scan_ids = _scan_session_ids(max_per_host=10, limit=10, session_filter="s3")
    stream_ids = _stream_session_ids(tmp_path, max_per_host=10, limit=10, session_filter="s3")

    assert scan_ids == ["s3"]
    assert stream_ids == scan_ids
