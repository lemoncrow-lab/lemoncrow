from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_server_core.index.contracts import (
    LinkBuildMode,
    ManifestEntry,
    RepoIdentityClaim,
    sha256_hex,
)
from lemoncrow_server_core.index.deferred import (
    DeferredIndexWorker,
    cancel_deferred_view,
    enqueue_deferred_layer,
)
from lemoncrow_server_core.index.manifest import chunk_manifest, manifest_root
from lemoncrow_server_core.index.repositories import RepositoryIndex

CAP = 1024**2
ORG = "org_generation"


def _entries(files: dict[str, bytes]) -> tuple[ManifestEntry, ...]:
    return tuple(
        ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data)) for path, data in sorted(files.items())
    )


def _open(
    owner: RepositoryIndex,
    files: dict[str, bytes],
    *,
    subject: str = "alice",
    fingerprint: str = "generation-repo",
    base_revision: str = "a" * 40,
) -> tuple[str, str]:
    repository = owner.backend.repos.canonical_repo_id(ORG, RepoIdentityClaim("", "", fingerprint))
    entries = _entries(files)
    chunks = chunk_manifest(entries)
    opened = owner.backend.views.open(
        org_id=ORG,
        repo_id=repository,
        base_revision=base_revision,
        manifest_root=manifest_root(entries),
        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
        lease_s=100_000,
        content_subject=subject,
        owner_subject=subject,
    )
    view_id = opened.state.view_id
    for chunk_id in opened.state.pending_chunks:
        chunk = next(item for item in chunks if item.chunk_id == chunk_id)
        owner.backend.views.put_manifest_chunk(
            org_id=ORG,
            view_id=view_id,
            chunk_id=chunk.chunk_id,
            entries=chunk.entries,
        )
    leaf = owner.backend.for_view(ORG, view_id)
    for data in files.values():
        digest = sha256_hex(data)
        leaf.content.put(ORG, digest, data)
        leaf.analysis.ensure(ORG, digest, "python-1", data)
        leaf.provenance.record(ORG, repository, subject, (digest,))
    return repository, view_id


def _eventually(predicate, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def _base_files() -> dict[str, bytes]:
    return {
        "a.py": b"from b import beta\n\ndef alpha():\n    return beta()\n",
        "b.py": b"def beta():\n    return 1\n",
        "c.py": b"def charlie():\n    return 3\n",
        "d.py": b"def delta():\n    return 4\n",
        "e.py": b"def echo():\n    return 5\n",
    }


def test_ten_clean_views_share_one_generation_graph(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        views = [_open(owner, _base_files())[1] for _ in range(10)]
        generations = {owner.backend.views.get(ORG, view).generation_id for view in views}
        assert len(generations) == 1
        assert next(iter(generations)).startswith("gen_")

        leaf = owner.backend.for_view(ORG, views[0])
        first = leaf.links.refresh(ORG, views[0], budget=100, wait=True)
        assert first.ready
        assert leaf.links.last_build(ORG, views[0]).mode is LinkBuildMode.FULL  # type: ignore[union-attr]

        for view in views[1:]:
            ready = leaf.links.refresh(ORG, view, budget=100, wait=True)
            assert ready.ready
            report = leaf.links.last_build(ORG, view)
            assert report is not None
            assert report.mode is LinkBuildMode.UNCHANGED
            assert report.resolved_paths == 0
            assert report.reason == "generation_reuse"

        index = owner.index(ORG, owner.backend.views.get(ORG, views[0]).repo_id, create=False)
        assert index is not None
        assert index._db.scalar_int("SELECT COUNT(*) FROM link_graphs") == 1
    finally:
        owner.close()


def test_overlay_graph_is_incremental_and_private_to_view(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, clean = _open(owner, _base_files())
        _again, dirty = _open(owner, _base_files())
        leaf = owner.backend.for_view(ORG, clean)
        leaf.links.refresh(ORG, clean, budget=100, wait=True)

        replacement = b"def echo():\n    return 55\n"
        entry = ManifestEntry("e.py", sha256_hex(replacement), "python-1", 0o100644, len(replacement))
        leaf.content.put(ORG, entry.content_digest, replacement)
        leaf.analysis.ensure(ORG, entry.content_digest, entry.parser_profile, replacement)
        leaf.provenance.record(ORG, repo, "alice", (entry.content_digest,))
        state = leaf.views.apply_overlay(
            org_id=ORG,
            view_id=dirty,
            expected_view_revision=0,
            client_seq=1,
            entries=(entry,),
            removed_paths=(),
        )
        leaf.links.note_revision(ORG, dirty, state.view_revision)
        ready = leaf.links.refresh(ORG, dirty, budget=100, wait=True)
        assert ready.ready
        report = leaf.links.last_build(ORG, dirty)
        assert report is not None
        assert report.mode is LinkBuildMode.INCREMENTAL
        assert report.changed_paths == 1
        assert report.resolved_paths < len(_base_files())

        index = owner.index(ORG, repo, create=False)
        assert index is not None
        assert index._db.scalar_int("SELECT COUNT(*) FROM link_graphs") == 2
        leaf.links.forget(ORG, dirty)
        leaf.views.close(ORG, dirty)
        assert index._db.scalar_int("SELECT COUNT(*) FROM link_graphs") == 1
    finally:
        owner.close()


def test_generation_graphs_do_not_cross_possession_scopes(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, alice = _open(owner, _base_files(), subject="alice")
        _repo, bob = _open(owner, _base_files(), subject="bob")
        assert owner.backend.views.get(ORG, alice).generation_id == owner.backend.views.get(ORG, bob).generation_id
        leaf = owner.backend.for_view(ORG, alice)
        leaf.links.refresh(ORG, alice, budget=100, wait=True)
        leaf.links.refresh(ORG, bob, budget=100, wait=True)
        index = owner.index(ORG, repo, create=False)
        assert index is not None
        keys = tuple(str(row["view_id"]) for row in index._db.rows("SELECT view_id FROM link_graphs ORDER BY view_id"))
        assert len(keys) == 2
        assert all(key.startswith("gscope_") for key in keys)
        assert keys[0] != keys[1]
    finally:
        owner.close()


def test_generation_retention_reclaims_scoped_graph(tmp_path: Path) -> None:
    now = [1000.0]
    owner = RepositoryIndex(
        tmp_path / "indexes",
        content_size_cap=CAP,
        inline_rebuild_budget=100,
        clock=lambda: now[0],
    )
    try:
        repo, view = _open(owner, _base_files())
        leaf = owner.backend.for_view(ORG, view)
        leaf.links.refresh(ORG, view, budget=100, wait=True)
        index = owner.index(ORG, repo, create=False)
        assert index is not None
        assert index._db.scalar_int("SELECT COUNT(*) FROM link_graphs") == 1
        assert index._db.scalar_int("SELECT COUNT(*) FROM repository_generations") == 1
        assert index._db.scalar_int("SELECT COUNT(*) FROM manifest_path_search") > 0

        leaf.views.close(ORG, view)
        now[0] = 1100.0
        assert leaf.views.sweep_manifest_roots(1050.0) == 1
        assert index._db.scalar_int("SELECT COUNT(*) FROM link_graphs") == 0
        assert index._db.scalar_int("SELECT COUNT(*) FROM repository_generations") == 0
        assert index._db.scalar_int("SELECT COUNT(*) FROM manifest_path_search") == 0
    finally:
        owner.close()


def test_deferred_clean_views_deduplicate_by_generation_scope(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=1)
    try:
        _repo, first = _open(owner, _base_files())
        _repo, second = _open(owner, _base_files())
        one = enqueue_deferred_layer(owner.backend, ORG, first, "link")
        two = enqueue_deferred_layer(owner.backend, ORG, second, "link")
        assert one is not None and two is not None
        assert one.job_id == two.job_id
        assert two.target_kind == "generation"
        assert two.target_key.startswith("gscope_")
        assert two.view_id == second
    finally:
        owner.close()


def test_generation_job_survives_original_view_closing(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=1)
    worker = DeferredIndexWorker(owner.backend, max_files=100, retry_delay_s=0.01)
    try:
        _repo, first = _open(owner, _base_files())
        _repo, second = _open(owner, _base_files())
        job = enqueue_deferred_layer(owner.backend, ORG, first, "link")
        assert job is not None and job.target_kind == "generation"
        cancel_deferred_view(owner.backend, first)
        leaf = owner.backend.for_view(ORG, first)
        leaf.views.close(ORG, first)
        leaf.links.forget(ORG, first)
        assert owner.deferred_jobs.get(job.job_id).state == "pending"  # type: ignore[union-attr]

        worker.start()
        _eventually(lambda: leaf.links.status(ORG, second).ready)
        assert owner.deferred_jobs.get(job.job_id).state == "completed"  # type: ignore[union-attr]
        report = leaf.links.last_build(ORG, second)
        assert report is not None and report.mode is LinkBuildMode.FULL
    finally:
        worker.close()
        owner.close()


def test_an_unchanged_generation_graph_is_not_saved_again(tmp_path: Path) -> None:
    # Every dirty View's link refresh re-saved its unchanged generation graph,
    # diffing all of its edges (~146k for a busy repository) against storage.
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        _repo, clean = _open(owner, _base_files())
        _again, dirty = _open(owner, _base_files())
        leaf = owner.backend.for_view(ORG, clean)
        leaf.links.refresh(ORG, clean, budget=100, wait=True)
        stored = leaf.links._graphs
        saved: list[str] = []

        class CountingStore:
            def __getattr__(self, name: str) -> Any:
                return getattr(stored, name)

            def save(self, org_id: str, view_id: str, graph: Any, base: str = "", base_graph: Any = None) -> None:
                saved.append(view_id)
                stored.save(org_id, view_id, graph, base=base, base_graph=base_graph)

        leaf.links._graphs = CountingStore()
        replacement = b"def echo():\n    return 55\n"
        entry = ManifestEntry("e.py", sha256_hex(replacement), "python-1", 0o100644, len(replacement))
        leaf.content.put(ORG, entry.content_digest, replacement)
        leaf.analysis.ensure(ORG, entry.content_digest, entry.parser_profile, replacement)
        leaf.provenance.record(ORG, _repo, "alice", (entry.content_digest,))
        state = leaf.views.apply_overlay(
            org_id=ORG,
            view_id=dirty,
            expected_view_revision=0,
            client_seq=1,
            entries=(entry,),
            removed_paths=(),
        )
        leaf.links.note_revision(ORG, dirty, state.view_revision)
        assert leaf.links.refresh(ORG, dirty, budget=100, wait=True).ready
        assert saved == [dirty]
        graph = stored.load(ORG, dirty)
        assert graph is not None
        assert any(edge.src_path == "a.py" and edge.dst_path == "b.py" for edge in graph.edges)
    finally:
        owner.close()


def test_a_generation_no_live_view_is_on_releases_its_semantic_index(tmp_path: Path) -> None:
    # Every new generation built an in-memory ANN of all its embeddings and
    # none was released, so the server grew with each commit Views moved onto.
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        _repo, first = _open(owner, _base_files())
        leaf = owner.backend.for_view(ORG, first)
        assert leaf.semantic.refresh(ORG, first, budget=100, wait=True).ready
        later_files = {**_base_files(), "e.py": b"def echo():\n    return 6\n"}
        _same, second = _open(owner, later_files, base_revision="b" * 40)
        assert owner.backend.views.get(ORG, second).generation_id != owner.backend.views.get(ORG, first).generation_id
        assert owner.backend.for_view(ORG, second).semantic is leaf.semantic

        # Both generations are live: both indexes stay.
        assert leaf.semantic.refresh(ORG, second, budget=100, wait=True).ready
        assert len(leaf.semantic._generation_indexes) == 2

        # The first generation's last View closes; the next build releases it.
        leaf.views.close(ORG, first)
        _third_repo, third = _open(
            owner, {**later_files, "f.py": b"def foxtrot():\n    return 7\n"}, base_revision="c" * 40
        )
        assert leaf.semantic.refresh(ORG, third, budget=100, wait=True).ready
        kept = {key[1] for key in leaf.semantic._generation_indexes}
        assert len(kept) == 2
        assert all(_generation_key(owner, view) in kept for view in (second, third))
    finally:
        owner.close()


def _generation_key(owner: RepositoryIndex, view_id: str) -> str:
    from lemoncrow_server_core.index.contracts import repository_generation_scope_id

    state = owner.backend.views.get(ORG, view_id)
    return repository_generation_scope_id(state.generation_id, state.content_subject)


def test_generations_share_the_units_and_signatures_of_unchanged_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each generation re-read every file's units and recomputed every LSH
    # signature in pure Python, holding its own copy: minutes of CPU and
    # hundreds of MB per commit for files that did not change.
    from lemoncrow_server_core.index import layers

    computed: list[object] = []
    real_signatures = layers.signatures

    def counting(vector: Any) -> tuple[int, ...]:
        computed.append(vector)
        return real_signatures(vector)

    monkeypatch.setattr(layers, "signatures", counting)
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        _repo, first = _open(owner, _base_files())
        semantic = owner.backend.for_view(ORG, first).semantic
        assert semantic.refresh(ORG, first, budget=100, wait=True).ready
        first_count = len(computed)
        assert first_count

        later_files = {**_base_files(), "e.py": b"def golf_hotel():\n    return india_juliet\n"}
        _same, second = _open(owner, later_files, base_revision="b" * 40)
        assert semantic.refresh(ORG, second, budget=100, wait=True).ready
        assert 0 < len(computed) - first_count < first_count

        old, new = (semantic._generation_indexes[(ORG, _generation_key(owner, view))] for view in (first, second))
        old_units = {item.path: item.unit for item in old.items}
        for item in new.items:
            if item.path != "e.py":
                assert item.unit is old_units[item.path]

        # The first generation's last View closes; its only-file versions go.
        owner.backend.for_view(ORG, first).views.close(ORG, first)
        _third_repo, third = _open(owner, later_files, base_revision="c" * 40)
        assert semantic.refresh(ORG, third, budget=100, wait=True).ready
        old_echo = sha256_hex(_base_files()["e.py"])
        assert all(key[1] != old_echo for key in semantic._unit_cache)
    finally:
        owner.close()
