from __future__ import annotations

import asyncio
import base64
import threading
import time
from pathlib import Path

import pytest
from aiohttp import ClientSession
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.index.contracts import IndexBackend, ManifestEntry, RepoIdentityClaim, sha256_hex
from lemoncrow_server_core.index.deferred import (
    ACTIVE_PRIORITY,
    RECOVERY_PRIORITY,
    DeferredIndexWorker,
    MemoryDeferredJobStore,
    SqliteDeferredJobStore,
    cancel_deferred_view,
    enqueue_deferred_layer,
    enqueue_deferred_view,
)
from lemoncrow_server_core.index.layers import LinkLayer
from lemoncrow_server_core.index.manifest import chunk_manifest, manifest_root
from lemoncrow_server_core.index.repositories import RepositoryIndex
from lemoncrow_server_core.index.sqlite import _Db
from lemoncrow_server_core.limits import Limits
from lemoncrow_server_core.protocol import PROTOCOL_VERSION, REQUIRED_CLIENT_CAPABILITIES

CAP = 1024**2


def _seed(backend: IndexBackend) -> tuple[str, str]:
    org = "org_deferred"
    repo = backend.repos.canonical_repo_id(org, RepoIdentityClaim("", "", "deferred-repo"))
    files = {
        "a.py": b"from b import beta\n\ndef alpha():\n    return beta()\n",
        "b.py": b"def beta():\n    return 1\n",
    }
    entries = tuple(
        ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data)) for path, data in sorted(files.items())
    )
    chunks = chunk_manifest(entries)
    opened = backend.views.open(
        org_id=org,
        repo_id=repo,
        base_revision="base",
        manifest_root=manifest_root(entries),
        chunk_ids=[chunk.chunk_id for chunk in chunks],
        lease_s=100_000,
        content_subject="alice",
        owner_subject="alice",
    )
    view_id = opened.state.view_id
    for chunk in chunks:
        backend.views.put_manifest_chunk(org_id=org, view_id=view_id, chunk_id=chunk.chunk_id, entries=chunk.entries)
    leaf = backend.for_view(org, view_id)
    for data in files.values():
        digest = sha256_hex(data)
        leaf.content.put(org, digest, data)
        leaf.analysis.ensure(org, digest, "python-1", data)
        leaf.provenance.record(org, repo, "alice", (digest,))
    return repo, view_id


def _eventually(predicate, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_job_store_deduplicates_supersedes_and_prioritizes_active_work() -> None:
    store = MemoryDeferredJobStore()
    first = store.enqueue(
        org_id="org",
        repo_id="repo",
        view_id="view",
        target_kind="view",
        target_key="view",
        layer="link",
        revision=0,
        priority=RECOVERY_PRIORITY,
        now=1.0,
    )
    same = store.enqueue(
        org_id="org",
        repo_id="repo",
        view_id="view",
        target_kind="view",
        target_key="view",
        layer="link",
        revision=0,
        priority=ACTIVE_PRIORITY,
        now=2.0,
    )
    assert same.job_id == first.job_id
    assert same.priority == ACTIVE_PRIORITY

    newer = store.enqueue(
        org_id="org",
        repo_id="repo",
        view_id="view",
        target_kind="view",
        target_key="view",
        layer="link",
        revision=1,
        priority=RECOVERY_PRIORITY,
        now=3.0,
    )
    assert store.get(first.job_id).state == "obsolete"  # type: ignore[union-attr]
    assert store.claim(3.0).job_id == newer.job_id  # type: ignore[union-attr]


def test_sqlite_job_store_upgrades_pre_generation_queue_in_place(tmp_path: Path) -> None:
    db = _Db(str(tmp_path / "jobs.sqlite3"))
    try:
        with db._lock:
            db._conn.executescript("""
                CREATE TABLE deferred_index_jobs (
                    job_id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    repo_id TEXT NOT NULL,
                    view_id TEXT NOT NULL,
                    layer TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    priority INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    requested_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    not_before REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );
                """)
            db._conn.execute(
                "INSERT INTO deferred_index_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("idx_old", "org", "repo", "view-old", "link", 3, 0, "pending", 0, 1.0, 1.0, 0.0, ""),
            )
        store = SqliteDeferredJobStore(db)
        recovered = store.get("idx_old")
        assert recovered is not None
        assert recovered.target_kind == "view"
        assert recovered.target_key == "view-old"
        assert recovered.revision == 3
    finally:
        db.close()


def test_local_server_production_lifecycle_consumes_deferred_jobs(tmp_path: Path) -> None:
    asyncio.run(_local_server_deferred_flow(tmp_path))


async def _local_server_deferred_flow(tmp_path: Path) -> None:
    server, state = build_local_server(
        LocalServerConfig(
            state_root=tmp_path / "server",
            listen_port=0,
            local_no_auth=True,
            allow_local_fs=False,
            limits=Limits(inline_rebuild_budget=1, max_deferred_index_files=100),
        )
    )
    files = {
        "a.py": b"from b import beta\n\ndef alpha():\n    return beta()\n",
        "b.py": b"def beta():\n    return 1\n",
    }
    entries = tuple(
        ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data)) for path, data in sorted(files.items())
    )
    chunk = chunk_manifest(entries)[0]
    url = await server.start()
    try:
        assert state.index_worker.running
        async with ClientSession() as client:
            response = await client.post(
                url + "/v1/sessions",
                json={
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": sorted(REQUIRED_CLIENT_CAPABILITIES),
                    "client_name": "deferred-test",
                    "client_version": "1",
                },
            )
            assert response.status == 200, await response.text()
            session = await response.json()
            headers = {"X-LemonCrow-Session": session["session_id"]}
            response = await client.post(
                url + "/v1/views/open",
                headers=headers,
                json={
                    "repo_identity": {"fingerprint": "deferred-http"},
                    "base_revision": "0" * 40,
                    "manifest_root": manifest_root(entries),
                    "manifest_chunks": [chunk.chunk_id],
                },
            )
            assert response.status == 200, await response.text()
            view_id = (await response.json())["view_id"]
            headers["X-LemonCrow-View"] = view_id
            headers["X-LemonCrow-View-Revision"] = "0"
            response = await client.post(
                url + f"/v1/views/{view_id}/manifest-chunks",
                headers=headers,
                json={"chunk_id": chunk.chunk_id, "entries": [entry.to_wire() for entry in entries]},
            )
            assert response.status == 200, await response.text()
            response = await client.post(
                url + "/v1/blobs",
                headers=headers,
                json={
                    "blobs": [
                        {
                            "content_digest": entry.content_digest,
                            "parser_profile": entry.parser_profile,
                            "encoding": "base64",
                            "data": base64.b64encode(files[entry.path]).decode(),
                        }
                        for entry in entries
                    ]
                },
            )
            assert response.status == 200, await response.text()
            leaf = state.backend.for_view("org_local", view_id)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if leaf.links.status("org_local", view_id).ready and leaf.semantic.status("org_local", view_id).ready:
                    break
                await asyncio.sleep(0.01)
            assert leaf.links.status("org_local", view_id).ready
            assert leaf.semantic.status("org_local", view_id).ready
    finally:
        await server.stop()
    assert not state.index_worker.running


def test_deferred_worker_completes_large_link_and_semantic_layers(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=1)
    worker = DeferredIndexWorker(owner.backend, retry_delay_s=0.01)
    try:
        _repo, view_id = _seed(owner.backend)
        leaf = owner.backend.for_view("org_deferred", view_id)
        assert leaf.links.refresh("org_deferred", view_id).reason == "link_rebuild_deferred"
        assert leaf.semantic.refresh("org_deferred", view_id).reason == "semantic_rebuild_deferred"
        jobs = enqueue_deferred_view(owner.backend, "org_deferred", view_id)
        assert len(jobs) == 2

        worker.start()
        _eventually(
            lambda: leaf.links.status("org_deferred", view_id).ready
            and leaf.semantic.status("org_deferred", view_id).ready
        )
        assert leaf.links.status("org_deferred", view_id).at_view_revision == 0
        assert leaf.semantic.status("org_deferred", view_id).at_view_revision == 0
        assert all(owner.deferred_jobs.get(job.job_id).state == "completed" for job in jobs)  # type: ignore[union-attr]
        assert owner.ops.snapshot().value("lemoncrow_index_worker_jobs_total", layer="link", outcome="completed") >= 1
    finally:
        worker.close()
        owner.close()


def test_deferred_worker_recovers_readiness_after_restart(tmp_path: Path) -> None:
    root = tmp_path / "indexes"
    owner = RepositoryIndex(root, content_size_cap=CAP, inline_rebuild_budget=1)
    worker = DeferredIndexWorker(owner.backend, retry_delay_s=0.01)
    _repo, view_id = _seed(owner.backend)
    worker.start()
    _eventually(lambda: owner.backend.for_view("org_deferred", view_id).links.status("org_deferred", view_id).ready)
    worker.close()
    owner.close()

    reopened = RepositoryIndex(root, content_size_cap=CAP, inline_rebuild_budget=1)
    restarted = DeferredIndexWorker(reopened.backend, retry_delay_s=0.01)
    try:
        leaf = reopened.backend.for_view("org_deferred", view_id)
        assert not leaf.links.status("org_deferred", view_id).ready
        assert not leaf.semantic.status("org_deferred", view_id).ready
        restarted.start()
        _eventually(
            lambda: leaf.links.status("org_deferred", view_id).ready
            and leaf.semantic.status("org_deferred", view_id).ready
        )
    finally:
        restarted.close()
        reopened.close()


def test_running_job_is_cancelled_when_view_becomes_obsolete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=1)
    worker = DeferredIndexWorker(owner.backend, retry_delay_s=0.01)
    try:
        _repo, view_id = _seed(owner.backend)
        leaf = owner.backend.for_view("org_deferred", view_id)
        state = leaf.views.apply_overlay(
            org_id="org_deferred",
            view_id=view_id,
            expected_view_revision=0,
            client_seq=1,
            entries=(),
            removed_paths=("b.py",),
        )
        leaf.links.note_revision("org_deferred", view_id, state.view_revision)
        leaf.semantic.note_revision("org_deferred", view_id, state.view_revision)
        entered = threading.Event()
        original = LinkLayer._all_facts

        def until_cancelled(self, org_id, members, cancel=None):  # type: ignore[no-untyped-def]
            entered.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and cancel is not None and not cancel():
                time.sleep(0.01)
            return original(self, org_id, members, cancel)

        monkeypatch.setattr(LinkLayer, "_all_facts", until_cancelled)
        job = enqueue_deferred_layer(owner.backend, "org_deferred", view_id, "link")
        assert job is not None
        worker.start()
        assert entered.wait(2.0)
        cancel_deferred_view(owner.backend, view_id)
        _eventually(lambda: owner.deferred_jobs.get(job.job_id).state == "obsolete")  # type: ignore[union-attr]
        _eventually(
            lambda: owner.ops.snapshot().value("lemoncrow_index_worker_jobs_total", layer="link", outcome="obsolete")
            >= 1
        )
        assert not leaf.links.status("org_deferred", view_id).ready
    finally:
        worker.close()
        owner.close()


def test_worker_shutdown_interrupts_and_restart_recovers_running_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=1)
    worker = DeferredIndexWorker(owner.backend, retry_delay_s=0.01)
    try:
        _repo, view_id = _seed(owner.backend)
        entered = threading.Event()
        original = LinkLayer._all_facts

        def until_cancelled(self, org_id, members, cancel=None):  # type: ignore[no-untyped-def]
            entered.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and cancel is not None and not cancel():
                time.sleep(0.01)
            return original(self, org_id, members, cancel)

        monkeypatch.setattr(LinkLayer, "_all_facts", until_cancelled)
        job = enqueue_deferred_layer(owner.backend, "org_deferred", view_id, "link")
        assert job is not None
        worker.start()
        assert entered.wait(2.0)
        started = time.monotonic()
        worker.close()
        assert time.monotonic() - started < 1.0
        assert owner.deferred_jobs.get(job.job_id).state == "running"  # type: ignore[union-attr]

        monkeypatch.setattr(LinkLayer, "_all_facts", original)
        restarted = DeferredIndexWorker(owner.backend, retry_delay_s=0.01)
        restarted.start()
        try:
            leaf = owner.backend.for_view("org_deferred", view_id)
            _eventually(lambda: leaf.links.status("org_deferred", view_id).ready)
            assert owner.deferred_jobs.get(job.job_id).state == "completed"  # type: ignore[union-attr]
        finally:
            restarted.close()
    finally:
        worker.close()
        owner.close()


def test_link_background_build_never_publishes_stale_revision_and_foreground_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=1)
    try:
        _repo, view_id = _seed(owner.backend)
        leaf = owner.backend.for_view("org_deferred", view_id)
        entered = threading.Event()
        release = threading.Event()
        original = LinkLayer._all_facts

        def blocked(self, org_id, members, cancel=None):  # type: ignore[no-untyped-def]
            entered.set()
            assert release.wait(2.0)
            return original(self, org_id, members, cancel)

        monkeypatch.setattr(LinkLayer, "_all_facts", blocked)
        result: list[object] = []
        thread = threading.Thread(
            target=lambda: result.append(leaf.links.refresh("org_deferred", view_id, budget=100, wait=True)),
            daemon=True,
        )
        thread.start()
        assert entered.wait(2.0)

        started = time.monotonic()
        foreground = leaf.links.refresh("org_deferred", view_id)
        assert time.monotonic() - started < 0.2
        assert foreground.reason == "link_rebuild_in_progress"

        state = leaf.views.apply_overlay(
            org_id="org_deferred",
            view_id=view_id,
            expected_view_revision=0,
            client_seq=1,
            entries=(),
            removed_paths=("b.py",),
        )
        leaf.links.note_revision("org_deferred", view_id, state.view_revision)
        release.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert result and result[0].reason == "view_revision_advanced"  # type: ignore[union-attr]
        assert leaf.links.edges("org_deferred", view_id) == ()

        monkeypatch.setattr(LinkLayer, "_all_facts", original)
        ready = leaf.links.refresh("org_deferred", view_id, budget=100, wait=True)
        assert ready.ready
        assert ready.at_view_revision == 1
        assert leaf.links.last_build("org_deferred", view_id).at_view_revision == 1  # type: ignore[union-attr]
    finally:
        owner.close()
