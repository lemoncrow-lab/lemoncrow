from __future__ import annotations

import asyncio
import base64
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from aiohttp import ClientSession
from lemoncrow_server_core.app import CoreServerRoutes
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.errors import ErrorCode, ServerError
from lemoncrow_server_core.index.analysis import analyze, analyze_foreground
from lemoncrow_server_core.index.contracts import (
    AnalysisArtifact,
    IndexBackend,
    ManifestEntry,
    RepoIdentityClaim,
    sha256_hex,
)
from lemoncrow_server_core.index.lifecycle import Maintenance
from lemoncrow_server_core.index.manifest import chunk_manifest, manifest_root
from lemoncrow_server_core.index.memory import build_memory_backend
from lemoncrow_server_core.index.migration import migrate_index
from lemoncrow_server_core.index.query import IndexQuery
from lemoncrow_server_core.index.repositories import RepositoryIndex, build_repository_backend, repository_index_root
from lemoncrow_server_core.index.sqlite import SqliteIndex
from lemoncrow_server_core.limits import Limits
from lemoncrow_server_core.protocol import PROTOCOL_VERSION, REQUIRED_CLIENT_CAPABILITIES

CAP = 1024**2


@pytest.mark.parametrize("kind", ("memory", "sqlite"))
def test_content_provenance_scope_is_an_exact_opaque_key(tmp_path: Path, kind: str) -> None:
    if kind == "memory":
        backend = build_memory_backend(content_size_cap=CAP)
        close = backend.close_backend
    else:
        index = SqliteIndex(tmp_path / "scope.sqlite3", content_size_cap=CAP)
        backend = index.backend
        close = index.close
    digest = sha256_hex(b"scope-boundary")
    try:
        assert backend.provenance.record("org_a", "repo_a", "alice", (digest,)) == 1
        assert backend.provenance.held("org_a", "repo_a", "alice", (digest,)) == frozenset({digest})
        assert backend.provenance.held("org_a", "repo_a", "", (digest,)) == frozenset()
        assert backend.provenance.record("org_a", "repo_a", "", (digest,)) == 1
        assert backend.provenance.held("org_a", "repo_a", "", (digest,)) == frozenset({digest})
    finally:
        close()


def seed(
    backend: IndexBackend,
    files: dict[str, bytes],
    *,
    org: str = "org_a",
    fingerprint: str = "repo-a",
    subject: str = "alice",
) -> tuple[str, str]:
    repository = backend.repos.canonical_repo_id(org, RepoIdentityClaim("", "", fingerprint))
    entries = tuple(
        ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data)) for path, data in sorted(files.items())
    )
    chunks = chunk_manifest(entries)
    view = backend.views.open(
        org_id=org,
        repo_id=repository,
        base_revision="",
        manifest_root=manifest_root(entries),
        chunk_ids=[chunk.chunk_id for chunk in chunks],
        lease_s=100_000,
        content_subject=subject,
        owner_subject=subject,
    ).state.view_id
    for chunk in chunks:
        backend.views.put_manifest_chunk(org_id=org, view_id=view, chunk_id=chunk.chunk_id, entries=chunk.entries)
    scoped = backend.for_view(org, view)
    for data in files.values():
        digest = sha256_hex(data)
        scoped.content.put(org, digest, data)
        scoped.analysis.ensure(org, digest, "python-1", data)
        scoped.provenance.record(org, repository, subject, (digest,))
    return repository, view


def matches(backend: IndexBackend, org: str, view: str, query: str) -> tuple[str, ...]:
    return tuple(
        hit.path
        for hit in IndexQuery(backend, content_size_cap=CAP)
        .run(org_id=org, view_id=view, kind="lexical", query=query)
        .hits
    )


def test_sqlite_view_content_reachability_is_scoped_and_overlay_exact(tmp_path: Path) -> None:
    index = SqliteIndex(tmp_path / "reachability.sqlite3", content_size_cap=CAP, compact=True)
    backend = index.backend
    shared = b"def shared():\n    return 1\n"
    missing = b"def missing():\n    return 2\n"
    big = b"x" * (CAP + 1)
    shared_digest = sha256_hex(shared)
    missing_digest = sha256_hex(missing)
    try:
        repo = backend.repos.canonical_repo_id("org_a", RepoIdentityClaim("", "", "reachability"))
        entries = (
            ManifestEntry("a.py", shared_digest, "python-1", 0o100644, len(shared)),
            ManifestEntry("b.py", shared_digest, "python-1", 0o100644, len(shared)),
            ManifestEntry("c.py", missing_digest, "python-1", 0o100644, len(missing)),
            ManifestEntry("vendor.bin", sha256_hex(big), "lexical-1", 0o100644, len(big)),
        )
        chunks = chunk_manifest(entries)
        view = backend.views.open(
            org_id="org_a",
            repo_id=repo,
            base_revision="base",
            manifest_root=manifest_root(entries),
            chunk_ids=[chunk.chunk_id for chunk in chunks],
            lease_s=100_000,
            content_subject="alice",
            owner_subject="alice",
        ).state.view_id
        for chunk in chunks:
            backend.views.put_manifest_chunk(
                org_id="org_a", view_id=view, chunk_id=chunk.chunk_id, entries=chunk.entries
            )

        backend.content.put("org_a", shared_digest, shared)
        backend.provenance.record("org_a", repo, "alice", (shared_digest,))
        backend.content.put("org_a", missing_digest, missing)
        backend.provenance.record("org_a", repo, "bob", (missing_digest,))

        assert backend.view_content_reachability is not None
        status = backend.view_content_reachability("org_a", view, CAP)
        assert status.missing_digests == (missing_digest,)
        assert status.required_paths == 3
        assert status.missing_paths == 1

        updated = backend.views.apply_overlay(
            org_id="org_a",
            view_id=view,
            expected_view_revision=0,
            client_seq=1,
            entries=(ManifestEntry("c.py", shared_digest, "python-1", 0o100644, len(shared)),),
            removed_paths=("b.py",),
        )
        assert updated.view_revision == 1
        status = backend.view_content_reachability("org_a", view, CAP)
        assert status.missing_digests == ()
        assert status.required_paths == 2
        assert status.missing_paths == 0
    finally:
        index.close()


def test_repositories_are_separate_and_worktrees_share_content(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP)
    try:
        a, first = seed(owner.backend, {"one.py": b"def alpha():\n    return 1\n"})
        again, second = seed(owner.backend, {"one.py": b"def alpha():\n    return 1\n"})
        b, other = seed(owner.backend, {"two.py": b"def beta():\n    return 2\n"}, fingerprint="repo-b")
        assert a == again and a != b
        assert owner.repository_path("org_a", a) != owner.repository_path("org_a", b)
        assert owner.backend.for_view("org_a", first) is owner.backend.for_view("org_a", second)
        assert owner.backend.for_view("org_a", first).analysis.count("org_a") == 1
        assert matches(owner.backend, "org_a", first, "alpha") == ("one.py",)
        assert matches(owner.backend, "org_a", second, "beta") == ()
        assert matches(owner.backend, "org_a", other, "beta") == ("two.py",)
        assert owner._catalog.scalar_int("SELECT COUNT(*) FROM analysis") == 0
        assert len(owner.keys()) == 2
        paths_before = tuple(owner.root.rglob("index.sqlite3"))
        with pytest.raises(ServerError) as refusal:
            owner.backend.for_view("org_intruder", first)
        assert refusal.value.code == ErrorCode.VIEW_UNKNOWN
        assert tuple(owner.root.rglob("index.sqlite3")) == paths_before
    finally:
        owner.close()
    reopened = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP)
    try:
        assert matches(reopened.backend, "org_a", first, "alpha") == ("one.py",)
        assert matches(reopened.backend, "org_a", other, "beta") == ("two.py",)
    finally:
        reopened.close()


def test_layer_status_of_a_view_with_no_route_is_a_cold_flag(tmp_path: Path) -> None:
    # A single backend reports cold for a View it does not hold; the repository
    # router raised view_unknown instead, so a degradation check run after the
    # View's route was reclaimed failed the call it was only meant to flag.
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP)
    try:
        _repo, view = seed(owner.backend, {"one.py": b"def alpha():\n    return 1\n"})
        invented = "view_" + "0" * 32
        for layer in (owner.backend.links, owner.backend.semantic):
            foreign = layer.status("org_intruder", view)
            assert foreign == layer.status("org_a", invented)
            assert not foreign.ready
        assert owner.backend.links.status("org_a", invented).reason == "link_index_not_built"
        assert owner.backend.semantic.status("org_a", invented).reason == "semantic_index_not_built"
        with pytest.raises(ServerError) as refusal:
            owner.backend.links.edges("org_a", invented)
        assert refusal.value.code == ErrorCode.VIEW_UNKNOWN
    finally:
        owner.close()


def test_repository_writers_do_not_share_a_lock(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP)
    try:
        a = owner.for_repository("org_a", "repo_a")
        b = owner.for_repository("org_a", "repo_b")
        data = b"independent writer"
        with a.analysis.batch(), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(b.content.put, "org_a", sha256_hex(data), data)
            assert future.result(timeout=2).digest == sha256_hex(data)
    finally:
        owner.close()


def test_external_retention_reference_is_scoped_to_one_repository(tmp_path: Path) -> None:
    now = [1000.0]
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP, clock=lambda: now[0])
    data = b"def shared():\n    return 42\n"
    digest = sha256_hex(data)
    try:
        a, first = seed(owner.backend, {"shared.py": data})
        b, second = seed(owner.backend, {"shared.py": data}, fingerprint="repo-b")
        owner.backend.views.close("org_a", first)
        owner.backend.views.close("org_a", second)
        maintenance = Maintenance(
            owner.backend,
            retention_s=10,
            clock=lambda: now[0],
            reference_providers=(lambda: {("org_a", a): frozenset({digest})},),
        )
        now[0] = 1011
        maintenance.run_once()
        now[0] = 1022
        report = maintenance.run_once()
        assert report.blobs_swept == 1
        assert owner.for_repository("org_a", a).content.get("org_a", digest) == data
        assert owner.for_repository("org_a", b).content.get("org_a", digest) is None
    finally:
        owner.close()


def test_a_failed_reference_provider_keeps_all_content_and_lets_maintenance_run(tmp_path: Path) -> None:
    now = [1000.0]
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP, clock=lambda: now[0])
    data = b"def reviewed():\n    return 42\n"
    digest = sha256_hex(data)
    failing = [False]

    def reviews() -> dict[tuple[str, str], frozenset[str]]:
        if failing[0]:
            raise sqlite3.OperationalError("database is locked")
        return {}

    try:
        a, view = seed(owner.backend, {"reviewed.py": data})
        owner.backend.views.close("org_a", view)
        maintenance = Maintenance(owner.backend, retention_s=10, clock=lambda: now[0], reference_providers=(reviews,))
        now[0] = 1011
        maintenance.run_once()
        # The Reviews that may name the marked blob cannot be read this pass:
        # past its retention window it is kept, and the pass still completes.
        failing[0] = True
        now[0] = 1022
        report = maintenance.run_once()
        assert maintenance.last_report is report
        assert report.blobs_swept == 0
        assert owner.for_repository("org_a", a).content.get("org_a", digest) == data
        failing[0] = False
        assert maintenance.run_once().blobs_swept == 1
    finally:
        owner.close()


def test_retention_does_not_pin_one_repository_with_another(tmp_path: Path) -> None:
    now = [1000.0]
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP, clock=lambda: now[0])
    data = b"def shared():\n    return 42\n"
    try:
        a, first = seed(owner.backend, {"shared.py": data})
        b, second = seed(owner.backend, {"shared.py": data}, fingerprint="repo-b")
        owner.backend.views.close("org_a", first)
        maintenance = Maintenance(owner.backend, retention_s=10, clock=lambda: now[0])
        now[0] = 1011
        maintenance.run_once()
        now[0] = 1022
        report = maintenance.run_once()
        assert report.blobs_swept == 1
        assert owner.for_repository("org_a", a).content.get("org_a", sha256_hex(data)) is None
        assert owner.for_repository("org_a", b).content.get("org_a", sha256_hex(data)) == data
        assert matches(owner.backend, "org_a", second, "shared") == ("shared.py",)
        assert owner._catalog.row("SELECT 1 FROM repository_views WHERE view_id=?", (first,)) is None
        assert maintenance.storage_report("org_a").physical_index_bytes > 0
    finally:
        owner.close()


def test_migration_preserves_views_profiles_provenance_and_unbound_content(tmp_path: Path) -> None:
    source = tmp_path / "index.sqlite3"
    legacy = SqliteIndex(source, content_size_cap=CAP)
    a, view_a = seed(legacy.backend, {"a.py": b"def alpha():\n    return 1\n"})
    b, view_b = seed(legacy.backend, {"b.py": b"def beta():\n    return 2\n"}, fingerprint="repo-b")
    _other, view_other = seed(legacy.backend, {"c.py": b"def gamma():\n    return 3\n"}, org="org_b")
    loose = b"unbound upload"
    legacy.backend.content.put("org_a", sha256_hex(loose), loose)
    legacy.backend.analysis.ensure("org_a", sha256_hex(loose), "lexical-1", loose)
    expected = matches(legacy.backend, "org_a", view_a, "alpha")
    legacy.close()
    with pytest.raises(RuntimeError, match="requires migration"):
        build_repository_backend(source, content_size_cap=CAP)
    report = migrate_index(source)
    assert report["repositories"] == 3 and report["views"] == 3
    assert report["source_retained"] and source.exists()
    backend = build_repository_backend(source, content_size_cap=CAP)
    try:
        assert matches(backend, "org_a", view_a, "alpha") == expected
        scoped = backend.for_view("org_a", view_a)
        paths, truncated = scoped.views.path_candidates("org_a", view_a, "a.py", limit=10)
        assert tuple(paths) == ("a.py",) and not truncated
        assert matches(backend, "org_a", view_b, "beta") == ("b.py",)
        assert matches(backend, "org_b", view_other, "gamma") == ("c.py",)
        assert backend.repos.canonical_repo_id("org_a", RepoIdentityClaim("", "", "repo-a")) == a
        assert backend.repos.canonical_repo_id("org_a", RepoIdentityClaim("", "", "repo-b")) == b
        assert backend.content.get("org_a", sha256_hex(loose)) == loose
        with pytest.raises(ServerError):
            backend.for_view("org_b", view_a)
        digest = sha256_hex(b"def alpha():\n    return 1\n")
        assert backend.provenance.held("org_a", a, "alice", (digest,)) == frozenset({digest})
        assert backend.provenance.held("org_a", b, "alice", (digest,)) == frozenset()
    finally:
        backend.close_backend()
    with pytest.raises(RuntimeError, match="already exists"):
        migrate_index(source)


def test_migration_resumes_only_its_unchanged_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_server_core.index import migration

    source = tmp_path / "index.sqlite3"
    legacy = SqliteIndex(source, content_size_cap=CAP)
    seed(legacy.backend, {"a.py": b"def alpha():\n    return 1\n"})
    seed(legacy.backend, {"b.py": b"def beta():\n    return 2\n"}, fingerprint="repo-b")
    legacy.close()
    copy = migration._copy_repository
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(args[3])
        if len(calls) == 2:
            raise RuntimeError("injected migration interruption")
        return copy(*args, **kwargs)

    monkeypatch.setattr(migration, "_copy_repository", interrupted)
    with pytest.raises(RuntimeError, match="injected"):
        migrate_index(source)
    assert source.exists() and not repository_index_root(source).exists()
    monkeypatch.setattr(migration, "_copy_repository", copy)
    messages = []
    report = migrate_index(source, progress=messages.append)
    assert report["repositories"] == 2
    assert any("reusing verified" in message for message in messages)


def test_changed_legacy_source_is_not_silently_ignored(tmp_path: Path) -> None:
    source = tmp_path / "index.sqlite3"
    legacy = SqliteIndex(source, content_size_cap=CAP)
    seed(legacy.backend, {"a.py": b"alpha"})
    legacy.close()
    migrate_index(source)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE views SET revision=revision+1")
    with pytest.raises(RuntimeError, match=r"requires migration|changed after migration"):
        build_repository_backend(source, content_size_cap=CAP)


def test_repository_disk_limit_rolls_back_and_other_repository_keeps_working(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP, max_repository_bytes=2 * 1024**2, max_wal_bytes=256 * 1024)
    try:
        a = owner.for_repository("org_a", "repo_a")
        data = b"x" * (3 * 1024**2)
        with pytest.raises(ServerError) as refusal:
            a.content.put("org_a", sha256_hex(data), data)
        assert refusal.value.code == ErrorCode.STORAGE_LIMIT
        assert a.content.get("org_a", sha256_hex(data)) is None
        b = owner.for_repository("org_a", "repo_b")
        small = b"still writable"
        b.content.put("org_a", sha256_hex(small), small)
        assert b.content.get("org_a", sha256_hex(small)) == small
        # A SQLITE_FULL auto-rollback must not leave the writer unusable.
        a.content.put("org_a", sha256_hex(small), small)
    finally:
        owner.close()


def test_pinned_wal_applies_backpressure_and_recovers(tmp_path: Path) -> None:
    index = SqliteIndex(
        tmp_path / "index.sqlite3", content_size_cap=CAP, compact=True, max_bytes=8 * 1024**2, max_wal_bytes=256 * 1024
    )
    reader = sqlite3.connect(index.path, isolation_level=None)
    try:
        index._db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM blobs").fetchone()
        data = b"x" * (384 * 1024)
        index.backend.content.put("org_a", sha256_hex(data), data)
        small = b"next write"
        with pytest.raises(ServerError) as refusal:
            index.backend.content.put("org_a", sha256_hex(small), small)
        assert refusal.value.code == ErrorCode.STORAGE_LIMIT and refusal.value.retryable
        reader.execute("ROLLBACK")
        index.backend.content.put("org_a", sha256_hex(small), small)
        assert index.backend.content.get("org_a", sha256_hex(small)) == small
    finally:
        reader.close()
        index.close()


@pytest.mark.parametrize("count", [63, 64, 65, 129])
def test_local_fill_commits_bounded_batches_without_losing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    import os

    from lemoncrow_server_core.index.localfs import LocalFsGrant, LocalFsSource

    index = SqliteIndex(tmp_path / "fill.sqlite3", content_size_cap=CAP, compact=True)
    files = {f"file_{number}.py": f"def item_{number}(): return {number}".encode() for number in range(count)}
    entries = tuple(
        ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data)) for path, data in files.items()
    )
    source = LocalFsSource(allow=True, size_cap=CAP)
    monkeypatch.setattr(LocalFsSource, "read", lambda _self, _grant, path, _digest: files[path])
    grant = LocalFsGrant(root=tmp_path, proof_path="proof", granted_at=0, owner_uid=os.getuid())
    transactions = []
    index._db._conn.set_trace_callback(lambda sql: transactions.append(sql) if sql == "BEGIN IMMEDIATE" else None)
    try:
        result = source.fill(
            grant, org_id="org", entries=entries, content=index.backend.content, analysis=index.backend.analysis
        )
        assert len(result.filled) == count
        assert index.backend.content.blob_count("org") == count
        assert index.backend.analysis.count("org") == count
        assert len(transactions) == (count + 63) // 64
    finally:
        index.close()


def test_local_fill_derives_outside_bounded_write_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from lemoncrow_server_core.index.localfs import LocalFsGrant, LocalFsSource

    count = 257
    index = SqliteIndex(tmp_path / "parallel-fill.sqlite3", content_size_cap=CAP, compact=True)
    files = {f"file_{number}.py": f"def item_{number}(): return {number}".encode() for number in range(count)}
    entries = tuple(
        ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data)) for path, data in files.items()
    )
    source = LocalFsSource(allow=True, size_cap=CAP)
    monkeypatch.setattr(LocalFsSource, "read", lambda _self, _grant, path, _digest: files[path])
    grant = LocalFsGrant(root=tmp_path, proof_path="proof", granted_at=0, owner_uid=os.getuid())
    analysis_batches: list[int] = []

    def derive(
        items: list[tuple[str, str, bytes]] | tuple[tuple[str, str, bytes], ...],
    ) -> tuple[AnalysisArtifact, ...]:
        analysis_batches.append(len(items))
        return tuple(analyze_foreground(digest, profile, data) for digest, profile, data in items)

    try:
        result = source.fill(
            grant,
            org_id="org",
            entries=entries,
            content=index.backend.content,
            analysis=index.backend.analysis,
            analyze_batch=derive,
        )
        assert analysis_batches == [256, 1]
        assert len(result.filled) == count
        assert index.backend.content.blob_count("org") == count
        assert index.backend.analysis.count("org") == count
    finally:
        index.close()


def test_foreground_analysis_defers_semantic_chunk_units() -> None:
    routes = object.__new__(CoreServerRoutes)
    routes._analysis_executor = None
    data = b"def authenticate_user(token):\n    return load_account(token)\n"
    artifact = asyncio.run(routes._derive_analysis_artifacts(((sha256_hex(data), "python-3", data),)))[0]
    assert artifact.lexical_terms
    assert artifact.definitions
    assert artifact.embedding
    assert artifact.semantic_units == ()


def test_parallel_analysis_derivation_preserves_order() -> None:
    routes = object.__new__(CoreServerRoutes)
    executor = ThreadPoolExecutor(max_workers=2)
    routes._analysis_executor = executor
    try:
        pending = []
        for number in range(193):
            data = f"def item_{number}(): return {number}\n".encode()
            pending.append((sha256_hex(data), "python-1", data))
        synchronous = routes._derive_analysis_artifacts_sync(pending)
        assert tuple(artifact.digest for artifact in synchronous) == tuple(item[0] for item in pending)
        assert tuple(artifact.parser_profile for artifact in synchronous) == tuple(item[1] for item in pending)
        artifacts = asyncio.run(routes._derive_analysis_artifacts(pending))
        assert tuple(artifact.digest for artifact in artifacts) == tuple(item[0] for item in pending)
        assert tuple(artifact.parser_profile for artifact in artifacts) == tuple(item[1] for item in pending)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_remote_blob_batch_uses_one_sqlite_transaction(tmp_path: Path) -> None:
    index = SqliteIndex(tmp_path / "remote-upload.sqlite3", content_size_cap=CAP, compact=True)
    routes = object.__new__(CoreServerRoutes)
    routes.config = SimpleNamespace(limits=Limits())
    prepared = []
    artifacts = {}
    transactions = []
    index._db._conn.set_trace_callback(lambda sql: transactions.append(sql) if sql == "BEGIN IMMEDIATE" else None)
    try:
        for number in range(17):
            data = f"def item_{number}(): return {number}\n".encode()
            digest = sha256_hex(data)
            profile = "python-1"
            prepared.append(
                (
                    digest,
                    {
                        "content_digest": digest,
                        "parser_profile": profile,
                        "encoding": "base64",
                        "data": base64.b64encode(data).decode(),
                    },
                    profile,
                )
            )
            artifacts[(digest, profile)] = analyze(digest, profile, data)

        stored = routes._commit_prepared_blobs("org", prepared, artifacts, nullcontext(), index.backend)
        assert len(stored) == len(prepared)
        assert index.backend.content.blob_count("org") == len(prepared)
        assert index.backend.analysis.count("org") == len(prepared)
        assert len(transactions) == 1
    finally:
        index.close()


def test_viewless_upload_accounts_against_unbound_partition(tmp_path: Path) -> None:
    asyncio.run(_viewless_upload_accounting_flow(tmp_path))


async def _viewless_upload_accounting_flow(tmp_path: Path) -> None:
    server, state = build_local_server(
        LocalServerConfig(state_root=tmp_path, listen_port=0, local_no_auth=True, allow_local_fs=False)
    )
    data = b"def already_in_repo():\n    return 1\n"
    digest = sha256_hex(data)
    seed(state.backend, {"same.py": data}, org="org_local", fingerprint="repo-existing")
    captured: dict[str, int] = {}

    def capture_budget(self, request, principal, view_id, *, content_bytes: int, indexed_bytes: int):
        del self, request, principal, view_id
        captured["content_bytes"] = content_bytes
        captured["indexed_bytes"] = indexed_bytes
        return nullcontext()

    state._storage_budget = MethodType(capture_budget, state)
    url = await server.start()
    try:
        async with ClientSession() as client:
            response = await client.post(
                url + "/v1/sessions",
                json={
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": sorted(REQUIRED_CLIENT_CAPABILITIES),
                    "client_name": "test",
                    "client_version": "1",
                },
            )
            assert response.status == 200, await response.text()
            session = await response.json()
            response = await client.post(
                url + "/v1/blobs",
                headers={"X-LemonCrow-Session": session["session_id"]},
                json={
                    "blobs": [
                        {
                            "content_digest": digest,
                            "parser_profile": "python-1",
                            "encoding": "base64",
                            "data": base64.b64encode(data).decode(),
                        }
                    ]
                },
            )
            assert response.status == 200, await response.text()
            assert captured["content_bytes"] == len(data)
            assert captured["indexed_bytes"] > 0
            unbound = state.backend.for_unbound("org_local")
            assert unbound.content.get("org_local", digest) == data
            assert unbound.analysis.get("org_local", digest, "python-1") is not None
    finally:
        await server.stop()


def test_viewless_upload_rolls_back_blob_when_analysis_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_viewless_upload_rollback_flow(tmp_path, monkeypatch))


async def _viewless_upload_rollback_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, state = build_local_server(
        LocalServerConfig(state_root=tmp_path, listen_port=0, local_no_auth=True, allow_local_fs=False)
    )
    data = b"def rollback_me():\n    return 1\n"
    digest = sha256_hex(data)
    unbound = state.backend.for_unbound("org_local")

    def fail_analysis(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected analysis failure")

    monkeypatch.setattr(unbound.analysis, "ensure", fail_analysis)
    url = await server.start()
    try:
        async with ClientSession() as client:
            response = await client.post(
                url + "/v1/sessions",
                json={
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": sorted(REQUIRED_CLIENT_CAPABILITIES),
                    "client_name": "test",
                    "client_version": "1",
                },
            )
            assert response.status == 200, await response.text()
            session = await response.json()
            response = await client.post(
                url + "/v1/blobs",
                headers={"X-LemonCrow-Session": session["session_id"]},
                json={
                    "blobs": [
                        {
                            "content_digest": digest,
                            "parser_profile": "python-1",
                            "encoding": "base64",
                            "data": base64.b64encode(data).decode(),
                        }
                    ]
                },
            )
            assert response.status == 500
            assert unbound.content.get("org_local", digest) is None
            assert unbound.analysis.get("org_local", digest, "python-1") is None
    finally:
        await server.stop()


def test_upload_and_query_use_repository_database_over_http(tmp_path: Path) -> None:
    asyncio.run(_http_repository_flow(tmp_path))


async def _http_repository_flow(tmp_path: Path) -> None:
    server, state = build_local_server(
        LocalServerConfig(
            state_root=tmp_path,
            listen_port=0,
            local_no_auth=True,
            allow_local_fs=False,
        )
    )
    url = await server.start()
    try:
        async with ClientSession() as client:
            for fingerprint, name in (("repo-alpha", "alpha"), ("repo-beta", "beta")):
                response = await client.post(
                    url + "/v1/sessions",
                    json={
                        "protocol_version": PROTOCOL_VERSION,
                        "capabilities": sorted(REQUIRED_CLIENT_CAPABILITIES),
                        "client_name": "test",
                        "client_version": "1",
                    },
                )
                assert response.status == 200, await response.text()
                session = await response.json()
                headers = {"X-LemonCrow-Session": session["session_id"]}
                data = f"def {name}():\n    return 1\n".encode()
                entry = ManifestEntry(f"{name}.py", sha256_hex(data), "python-1", 0o100644, len(data))
                chunk = chunk_manifest((entry,))[0]
                response = await client.post(
                    url + "/v1/views/open",
                    headers=headers,
                    json={
                        "repo_identity": {"fingerprint": fingerprint},
                        "base_revision": "0" * 40,
                        "manifest_root": manifest_root((entry,)),
                        "manifest_chunks": [chunk.chunk_id],
                    },
                )
                assert response.status == 200, await response.text()
                opened = await response.json()
                view = opened["view_id"]
                headers["X-LemonCrow-View"] = view
                headers["X-LemonCrow-View-Revision"] = "0"
                response = await client.post(
                    url + f"/v1/views/{view}/manifest-chunks",
                    headers=headers,
                    json={
                        "chunk_id": chunk.chunk_id,
                        "entries": [entry.to_wire()],
                    },
                )
                assert response.status == 200, await response.text()
                response = await client.post(
                    url + "/v1/blobs",
                    headers=headers,
                    json={
                        "blobs": [
                            {
                                "content_digest": entry.content_digest,
                                "parser_profile": "python-1",
                                "encoding": "base64",
                                "data": base64.b64encode(data).decode(),
                            }
                        ]
                    },
                )
                assert response.status == 200, await response.text()
                response = await client.post(
                    url + f"/v1/views/{view}/query", headers=headers, json={"kind": "lexical", "query": name}
                )
                assert response.status == 200, await response.text()
                result = await response.json()
                assert [hit["path"] for hit in result["hits"]] == [f"{name}.py"]
                scoped = state.backend.for_view("org_local", view)
                assert scoped.analysis.count("org_local") == 1
            assert len(state.backend.partitions()) == 2
    finally:
        await server.stop()
