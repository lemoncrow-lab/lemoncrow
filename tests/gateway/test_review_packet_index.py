"""The packet index: one file of a Snapshot's packet costs that file, and the index never serves another packet.

The Reader's per-file patch and contents reads used to gunzip and decode the whole packet (10 MiB of JSON for a
10,000-file change) per request. ``ReviewStore.packet_index`` decodes it once per ``(revision id, packet digest,
packet path)``; these tests hold the index to the legacy readers' answers and to its cache rules.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import api as review_api
from lemoncrow.pro.capabilities.review.packet_index import PacketIndex, build_packet_index, decode_packet_bytes
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession, ReviewUnit
from lemoncrow.pro.capabilities.review.sources.local import read_packet_json
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id


def _packet(*paths: str, tag: str = "a") -> dict[str, Any]:
    return {
        "files": [{"path": path, "status": "modified", "additions": 1, "deletions": 0, "tag": tag} for path in paths],
        "symbols": [{"file_path": path, "symbol_name": f"fn_{tag}", "change": "modified"} for path in paths],
        "provenance": {"status": "recorded", "files_inspected": list(paths[:1])},
        "evidence": [{"name": "tests", "status": "PASS"}],
    }


def _gz(packet: Any) -> bytes:
    return gzip.compress(json.dumps(packet).encode("utf-8"), mtime=0)


def _store(root: Path) -> ReviewStore:
    store = ReviewStore(root)
    store.init()
    return store


def _session() -> ReviewSession:
    return ReviewSession(
        id=new_session_id(),
        subject_type="local_change",
        repo_root="/repo/alpha",
        range_mode="working_tree",
        title="uncommitted changes",
        source_ref="",
    )


def _unit(path: str, *, kind: str = "file", key: str = "") -> ReviewUnit:
    return ReviewUnit(
        revision_id="",
        unit_key=key or f"{kind}:{path}",
        kind=kind,  # type: ignore[arg-type]
        path=path,
        content_fingerprint="fp-1",
    )


def _publish(
    store: ReviewStore,
    session: ReviewSession,
    packet: Any,
    *,
    units: tuple[ReviewUnit, ...] = (),
    fingerprint: str = "tree-a",
) -> ReviewRevision:
    revision_id = new_revision_id()
    path, digest, size = store.write_packet_artifact(session.id, revision_id, _gz(packet))
    revision = ReviewRevision(
        id=revision_id,
        review_id=session.id,
        revision_number=0,
        range_mode="working_tree",
        tree_fingerprint=fingerprint,
        packet_schema_version=1,
        packet_path=path,
        packet_sha256=digest,
        packet_bytes=size,
    )
    return store.add_revision(revision, units or (_unit("src/a.py"),))


@pytest.fixture
def store(tmp_path: Path) -> ReviewStore:
    return _store(tmp_path)


# ----- the index answers what the legacy readers answered --------------------------------------------------------- #


def test_decode_packet_bytes_refuses_anything_that_is_not_a_packet() -> None:
    assert decode_packet_bytes(None) is None
    assert decode_packet_bytes(b"not gzip") is None
    assert decode_packet_bytes(gzip.compress(b"{broken")) is None
    assert decode_packet_bytes(gzip.compress(b"[1, 2]")) is None
    assert decode_packet_bytes(gzip.compress(b"\xff\xfe")) is None
    assert decode_packet_bytes(_gz({"files": []})) == {"files": []}
    assert build_packet_index(b"not gzip") is None


def test_the_index_answers_exactly_what_the_legacy_helpers_answer() -> None:
    odd_path = "src/ünicode 😀/☃.py"
    packet: dict[str, Any] = {
        "files": [
            {"path": "src/a.py", "status": "added", "n": 1},
            "not-a-dict",
            None,
            {"path": "src/a.py", "status": "modified", "n": 2},  # a later entry for a path wins
            {"status": "modified"},  # no path: reachable only as the empty path
            {"path": odd_path, "old_path": "src/old.py", "status": "renamed", "nested": {"k": [1, 2.5, None, True]}},
            {"path": "src/b.py", "is_binary": True},
        ],
        "symbols": [
            {"file_path": "src/a.py", "symbol_name": "one", "start_line": 3},
            "junk",
            {"file_path": "src/a.py", "symbol_name": "two"},
            {"file_path": odd_path, "symbol_name": "three", "caller_count": 4},
            {"symbol_name": "orphan"},
        ],
        "provenance": {"status": "recorded", "files_inspected": ["src/a.py", odd_path], "commands_run": ["pytest"]},
        "evidence": [{"name": "tests", "status": "PASS"}, "junk", {"name": "lint", "status": "FAIL"}],
    }
    index = PacketIndex(packet)
    legacy = review_api._files_by_path(packet)
    assert set(legacy) == {"src/a.py", "", odd_path, "src/b.py"}
    for path in (*legacy, "src/missing.py"):
        entry = index.file(path)
        assert entry == legacy.get(path), path
        if entry is None:
            continue
        sliced = index.slice_for(path, entry)
        assert review_api._symbols_for(sliced, path) == review_api._symbols_for(packet, path), path
        assert review_api._provenance_for(sliced, path) == review_api._provenance_for(packet, path), path
        assert review_api._evidence(sliced, (), path=path, revision_id="r") == review_api._evidence(
            packet, (), path=path, revision_id="r"
        ), path
    assert index.file("src/a.py") == {"path": "src/a.py", "status": "modified", "n": 2}


def test_a_packet_without_provenance_or_evidence_indexes_like_the_legacy_helpers() -> None:
    for packet in ({}, {"provenance": "x", "evidence": None, "files": None, "symbols": None}):
        index = PacketIndex(packet)
        assert index.file("src/a.py") is None
        sliced = index.slice_for("src/a.py", {"path": "src/a.py"})
        assert review_api._provenance_for(sliced, "src/a.py") == review_api._provenance_for(packet, "src/a.py")
        assert review_api._evidence(sliced, (), path="src/a.py") == review_api._evidence(packet, (), path="src/a.py")


def test_every_read_is_a_fresh_copy_the_caller_may_change() -> None:
    index = PacketIndex(_packet("src/a.py"))
    entry = index.file("src/a.py")
    assert entry is not None
    entry["status"] = "changed by a caller"
    index.symbols("src/a.py").clear()
    assert index.file("src/a.py") == _packet("src/a.py")["files"][0]
    assert len(index.symbols("src/a.py")) == 1


# ----- membership ------------------------------------------------------------------------------------------------- #


def test_has_file_unit_is_an_exact_file_unit_of_that_revision(store: ReviewStore) -> None:
    session = store.create_session(_session())
    first = _publish(
        store,
        session,
        _packet("src/a.py"),
        units=(_unit("src/a.py"), _unit("src/sym.py", kind="symbol", key="symbol:src/sym.py:f")),
    )
    second = _publish(store, session, _packet("src/z.py"), units=(_unit("src/z.py"),), fingerprint="tree-b")
    assert store.has_file_unit(first.id, "src/a.py")
    assert not store.has_file_unit(first.id, "src/sym.py"), "a symbol unit is not a file of the review"
    assert not store.has_file_unit(first.id, "src/z.py"), "another revision's file is not this revision's"
    assert not store.has_file_unit(first.id, "src/a.p")
    assert not store.has_file_unit(first.id, "SRC/A.PY")
    assert not store.has_file_unit(first.id, "src")
    assert not store.has_file_unit(first.id, "")
    assert not store.has_file_unit("no-such-revision", "src/a.py")
    assert store.has_file_unit(second.id, "src/z.py")
    for unit in store.list_units(first.id):
        assert store.has_file_unit(first.id, unit.path) == (unit.kind == "file")


# ----- the memo ---------------------------------------------------------------------------------------------------- #


def _count_reads(monkeypatch: pytest.MonkeyPatch, delay: float = 0.0) -> list[str]:
    reads: list[str] = []
    real = ReviewStore.read_packet_artifact

    def counted(self: ReviewStore, revision: ReviewRevision) -> bytes | None:
        reads.append(revision.id)
        if delay:
            time.sleep(delay)
        return real(self, revision)

    monkeypatch.setattr(ReviewStore, "read_packet_artifact", counted)
    return reads


def test_a_snapshots_packet_is_read_once_however_many_files_are_read(
    store: ReviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = store.create_session(_session())
    revision = _publish(store, session, _packet("src/a.py", "src/b.py", "src/c.py"))
    reads = _count_reads(monkeypatch)
    first = store.packet_index(revision)
    assert first is not None
    for path in ("src/a.py", "src/b.py", "src/c.py", "src/a.py"):
        assert store.packet_index(revision) is first
        assert first.file(path) is not None
    assert reads == [revision.id]


def test_a_second_snapshot_never_reads_the_first_ones_index(store: ReviewStore) -> None:
    session = store.create_session(_session())
    first = _publish(store, session, _packet("src/a.py", tag="first"))
    second = _publish(store, session, _packet("src/a.py", "src/b.py", tag="second"), fingerprint="tree-b")
    one, two = store.packet_index(first), store.packet_index(second)
    assert one is not None and two is not None and one is not two
    assert (one.file("src/a.py") or {})["tag"] == "first"
    assert (two.file("src/a.py") or {})["tag"] == "second"
    assert one.file("src/b.py") is None and two.file("src/b.py") is not None
    assert store.packet_index(first) is one and store.packet_index(second) is two


def test_identical_packets_of_two_snapshots_are_two_entries(
    store: ReviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The digest is not the whole key: revision id and path are in it, so equal bytes never alias two revisions."""

    session = store.create_session(_session())
    first = _publish(store, session, _packet("src/a.py"))
    second = _publish(store, session, _packet("src/a.py"), fingerprint="tree-b")
    assert first.packet_sha256 == second.packet_sha256 and first.packet_path != second.packet_path
    reads = _count_reads(monkeypatch)
    one, two = store.packet_index(first), store.packet_index(second)
    assert one is not two
    assert sorted(reads) == sorted([first.id, second.id])


def test_a_rewritten_packet_is_read_again_and_never_served_from_the_old_index(
    tmp_path: Path, store: ReviewStore
) -> None:
    session = store.create_session(_session())
    revision = _publish(store, session, _packet("src/a.py", tag="before"))
    before = store.packet_index(revision)
    assert before is not None and (before.file("src/a.py") or {})["tag"] == "before"

    store.update_revision_projection(
        revision.id,
        packet_payload=_gz(_packet("src/a.py", "src/b.py", tag="after")),
        packet_schema_version=revision.packet_schema_version,
        degraded=(),
        provenance_host="",
        provenance_model="",
        provenance_session_id="",
        provenance_certainty=revision.provenance_certainty,
        units=store.list_units(revision.id),
    )
    rewritten = store.get_revision(revision.id)
    assert rewritten is not None and rewritten.packet_sha256 != revision.packet_sha256
    after = store.packet_index(rewritten)
    assert after is not None and after is not before
    assert (after.file("src/a.py") or {})["tag"] == "after"
    assert after.file("src/b.py") is not None
    # A second process reads the same rewrite the same way.
    again = ReviewStore(tmp_path).packet_index(rewritten)
    assert again is not None and (again.file("src/a.py") or {})["tag"] == "after"


def test_a_packet_without_a_digest_is_never_kept(store: ReviewStore, monkeypatch: pytest.MonkeyPatch) -> None:
    session = store.create_session(_session())
    revision = replace(_publish(store, session, _packet("src/a.py")), packet_sha256="")
    reads = _count_reads(monkeypatch)
    one, two = store.packet_index(revision), store.packet_index(revision)
    assert one is not None and two is not None and one is not two
    assert len(reads) == 2
    assert store._packet_index_memo == {}


def test_an_unreadable_packet_is_none_and_is_read_again_next_time(store: ReviewStore) -> None:
    session = store.create_session(_session())
    revision = _publish(store, session, _packet("src/a.py"))
    artifact = store._confined(revision.packet_path)
    good = artifact.read_bytes()
    artifact.write_bytes(b"corrupt")
    assert store.packet_index(revision) is None
    assert store.packet_index(replace(revision, packet_path="review/artifacts/none/none/packet.json.gz")) is None
    artifact.write_bytes(good)
    index = store.packet_index(revision)
    assert index is not None and index.file("src/a.py") is not None
    artifact.unlink()
    assert store.packet_index(replace(revision, packet_path="")) is None


def test_the_memo_is_bounded_by_entries_and_by_size(store: ReviewStore, monkeypatch: pytest.MonkeyPatch) -> None:
    session = store.create_session(_session())
    revisions = [
        _publish(store, session, _packet(f"src/f{n}.py", tag=str(n)), fingerprint=f"tree-{n}") for n in range(4)
    ]
    monkeypatch.setattr(ReviewStore, "PACKET_INDEX_MEMO_MAX_ENTRIES", 2)
    for revision in revisions:
        store.packet_index(revision)
    kept = [key[0] for key in store._packet_index_memo]
    assert kept == [revisions[2].id, revisions[3].id], "least recently used goes first"
    store.packet_index(revisions[2])  # touching an entry makes it the newest
    store.packet_index(revisions[0])
    assert [key[0] for key in store._packet_index_memo] == [revisions[2].id, revisions[0].id]
    assert (store.packet_index(revisions[1]) or PacketIndex({})).file(
        "src/f1.py"
    ) is not None, "an evicted one rebuilds"

    monkeypatch.setattr(ReviewStore, "PACKET_INDEX_MEMO_MAX_ENTRIES", 99)
    monkeypatch.setattr(ReviewStore, "PACKET_INDEX_MEMO_MAX_BYTES", 1)
    newest = store.packet_index(revisions[3])
    assert newest is not None
    assert [key[0] for key in store._packet_index_memo] == [revisions[3].id], "the newest stays even past the bound"
    assert store.packet_index(revisions[3]) is newest


def test_concurrent_first_reads_build_the_index_once(store: ReviewStore, monkeypatch: pytest.MonkeyPatch) -> None:
    session = store.create_session(_session())
    revision = _publish(store, session, _packet("src/a.py", "src/b.py"))
    reads = _count_reads(monkeypatch, delay=0.15)
    results: list[PacketIndex | None] = []
    gate = threading.Barrier(8)

    def worker() -> None:
        gate.wait()
        results.append(store.packet_index(revision))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == 8 and all(item is results[0] and item is not None for item in results)
    assert reads == [revision.id]
    assert store._packet_index_building == {}


def test_a_failed_build_does_not_wedge_later_reads(store: ReviewStore, monkeypatch: pytest.MonkeyPatch) -> None:
    session = store.create_session(_session())
    revision = _publish(store, session, _packet("src/a.py"))
    real = ReviewStore.read_packet_artifact
    state = {"fail": True}

    def flaky(self: ReviewStore, target: ReviewRevision) -> bytes | None:
        if state["fail"]:
            raise OSError("disk went away")
        return real(self, target)

    monkeypatch.setattr(ReviewStore, "read_packet_artifact", flaky)
    with pytest.raises(OSError):
        store.packet_index(revision)
    assert store._packet_index_building == {}
    state["fail"] = False
    assert store.packet_index(revision) is not None


def test_deleting_a_review_drops_its_packets_from_the_memo(store: ReviewStore) -> None:
    session = store.create_session(_session())
    revision = _publish(store, session, _packet("src/a.py"))
    assert store.packet_index(revision) is not None
    assert store._packet_index_memo
    store._remove_artifacts(session.id)
    assert store._packet_index_memo == {}


def test_the_index_matches_read_packet_json_for_every_file_of_a_large_packet(store: ReviewStore) -> None:
    paths = [f"src/pkg_{n % 7}/module_{n:05d}.py" for n in range(2_000)]
    session = store.create_session(_session())
    revision = _publish(store, session, _packet(*paths), units=tuple(_unit(path) for path in paths))
    index = store.packet_index(revision)
    legacy = review_api._files_by_path(read_packet_json(store, revision))
    assert index is not None and len(legacy) == len(paths)
    assert all(index.file(path) == legacy[path] for path in paths)
    assert all(store.has_file_unit(revision.id, path) for path in paths[::97])
