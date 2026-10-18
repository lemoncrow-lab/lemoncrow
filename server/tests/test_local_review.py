from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp import ClientSession
from lemoncrow_client.config import ClientConfig, load_config
from lemoncrow_client.errors import ClientError
from lemoncrow_client.session import RemoteSession
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.local_app import _automatic_review_title, _review_location_metadata
from lemoncrow_server_core.local_reviews import LocalReviewRepository, _prime_asgi_route_caches

from lemoncrow.core.capabilities.session_comment_capture import capture_session_comment
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.hosted import (
    capture_server_review,
    server_review_detail,
    server_review_request,
    server_review_rows,
)
from lemoncrow.pro.capabilities.review.models import ReviewOrderEntry
from lemoncrow.pro.capabilities.review.session_models import ReviewMark, ReviewRevision, ReviewSession


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_reader_primes_lazy_fastapi_route_tables_before_parallel_dispatch() -> None:
    from fastapi import APIRouter, FastAPI

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter(prefix="/api")

    @router.post("/reviews/{review_id}/revisions/{revision_number}/publish")
    def publish(review_id: str, revision_number: int) -> dict[str, object]:
        return {"review_id": review_id, "revision_number": revision_number}

    app.include_router(router)
    included = [route for route in app.routes if callable(getattr(route, "effective_candidates", None))]
    assert included, "FastAPI included-router cache shape changed; revisit the Reader compatibility guard"

    _prime_asgi_route_caches(app)

    candidates = included[0].effective_candidates()
    paths = {str(getattr(getattr(candidate, "original_route", None), "path", "")) for candidate in candidates}
    assert "/api/reviews/{review_id}/revisions/{revision_number}/publish" in paths
    version = getattr(included[0], "_effective_candidates_version", None)
    router_version = included[0].original_router._get_routes_version()
    assert version == router_version


def test_review_location_metadata_identifies_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "initial")

    metadata = _review_location_metadata(repo)

    assert metadata["directory"] == str(tmp_path)
    assert metadata["project"] == "repo"
    assert metadata["worktree"] == "repo"
    assert metadata["worktree_root"] == str(repo.resolve())
    assert metadata["branch"]
    assert metadata["revision"]


def test_review_list_reuses_checkout_metadata_without_leaking_branch_hints(tmp_path: Path, monkeypatch) -> None:
    import lemoncrow_server_core.local_review_service as review_service
    from lemoncrow_server_core.local_review_service import LocalReviewService

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    calls: list[Path] = []

    def metadata(root: Path) -> dict[str, object]:
        calls.append(root)
        return {
            "directory": str(tmp_path),
            "project": "repo",
            "project_root": str(checkout),
            "worktree": "checkout",
            "worktree_root": str(checkout),
            "branch": "current",
            "revision": "abcdef",
            "detached": False,
        }

    monkeypatch.setattr(review_service, "_review_location_metadata", metadata)
    service = LocalReviewService.__new__(LocalReviewService)
    service.review_repository = SimpleNamespace(
        workspace_root_hint=lambda _repo, _id: str(checkout),
        workspace_branch_hint=lambda _repo, review_id: "old-branch" if review_id == "older" else "current",
        checkout_root=lambda _repo: checkout,
    )
    cache: dict[Path, dict[str, object]] = {}
    older = service._review_location_wire("repo", SimpleNamespace(id="older"), metadata_cache=cache)
    current = service._review_location_wire("repo", SimpleNamespace(id="newer"), metadata_cache=cache)
    assert calls == [checkout]
    assert older["branch"] == "old-branch" and older["revision"] == ""
    assert current["branch"] == "current" and current["revision"] == "abcdef"


def test_commit_range_is_reviewable_before_impact_and_provenance(tmp_path: Path, monkeypatch) -> None:
    """Immutable commit ranges publish source before optional intelligence work."""
    import lemoncrow.pro.capabilities.review.hosted as capture_module

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.test")
    _git(repo, "config", "user.name", "Reviewer")
    (repo / "a.py").write_text("answer = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "first")
    (repo / "a.py").write_text("answer = 2\n")
    _git(repo, "commit", "-qam", "second")
    stages: list[str] = []

    def impact(_repo, _range, build, **_kwargs):
        stages.append("impact")
        return build

    def provenance(_store, _repo, _range, build, **_kwargs):
        stages.append("provenance")
        return build

    monkeypatch.setattr(capture_module, "attach_impact", impact)
    monkeypatch.setattr(capture_module, "attach_provenance", provenance)

    async def exercise() -> None:
        server, _state = build_local_server(
            LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
        )
        url = await server.start()
        config = ClientConfig(
            url=url,
            token="",
            repo_root=repo,
            state_dir=tmp_path / "client",
            install_mode="local",
            startup_budget_s=10.0,
            request_timeout_s=10.0,
            tool_timeout_s=30.0,
            resolution_cache_bytes=0,
            offer_local_fs=True,
            token_source="test",
        )

        def capture() -> None:
            def ready(captured):
                stages.append("ready")
                overview = server_review_request(config, "GET", f"/api/reviews/{captured.review_id}")
                assert overview["revision"]["id"] == captured.response["revision"]["id"]
                assert captured.preparation_id

            review = capture_server_review(
                config,
                repo,
                resolve_rev_range(repo, base="HEAD~1", head="HEAD"),
                store_root=tmp_path / "client-store",
                session_id=None,
                limit=20,
                with_impact=True,
                with_provenance=True,
                ready=ready,
            )
            assert review.build.packet.files

        try:
            await asyncio.to_thread(capture)
        finally:
            await server.stop()

    asyncio.run(exercise())
    assert stages == ["ready", "impact", "provenance"]


def test_a_review_keeps_its_own_branch_after_the_checkout_switches(tmp_path: Path) -> None:
    asyncio.run(_exercise_branch_switch(tmp_path))


async def _exercise_branch_switch(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.hosted import active_local_review_base

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "initial")
    (repo / "README.md").write_text("two\n", encoding="utf-8")

    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
    )
    url = await server.start()
    config = ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=30.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )

    def flow() -> tuple[str, list[dict[str, object]], tuple[str, str]]:
        captured = capture_server_review(
            config,
            repo,
            resolve_rev_range(repo, working_tree=True),
            store_root=tmp_path / "client-store",
            session_id=None,
            limit=200,
            with_impact=False,
            with_provenance=False,
        )
        _git(repo, "checkout", "-q", "-b", "feature")
        rows = server_review_rows(config)
        adopted = active_local_review_base(
            config, repo_root=repo, store_root=tmp_path / "client-store", range_mode="working_tree"
        )
        return captured.review_id, rows, adopted

    try:
        review_id, rows, adopted = await asyncio.to_thread(flow)
        [row] = [row for row in rows if row["id"] == review_id]
        assert row["branch"] == "main"
        # A new branch never adopts (and freezes the base of) another branch's Change.
        assert adopted == ("", "")
    finally:
        await server.stop()


def test_an_engine_snapshot_from_before_the_tree_id_identity_takes_no_new_snapshot(tmp_path: Path) -> None:
    """The source identity names the base by its tree (owner decision, plan 2026-10-10).

    A Snapshot recorded before stores the commit-id form. The engine sends that
    form beside the new one, so the same source is unchanged after the upgrade;
    a real content change still takes a Snapshot.
    """

    asyncio.run(_exercise_identity_upgrade(tmp_path))


def test_snapshot_file_progress_reaches_the_capture_client(tmp_path: Path, monkeypatch) -> None:
    import time

    from lemoncrow.pro.capabilities.review.sources import local

    original = local.derive_units

    def slow_derivation(packet, blobs, **kwargs):
        result = original(packet, blobs, **kwargs)
        # Keep the completed count visible for one real background-job poll.
        if kwargs.get("progress") is not None:
            time.sleep(1.2)
        return result

    monkeypatch.setattr(local, "derive_units", slow_derivation)
    seen = []
    asyncio.run(_exercise_identity_upgrade(tmp_path, progress=lambda stage, detail: seen.append((stage, detail))))
    assert any(stage == "publish" and "Building review targets · 1/1 files" in detail for stage, detail in seen)
    assert any(stage == "publish_done" for stage, _ in seen)


async def _exercise_identity_upgrade(tmp_path: Path, progress=None) -> None:
    from lemoncrow.pro.capabilities.review.gitdiff import source_state

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "initial")
    (repo / "README.md").write_text("two\n", encoding="utf-8")

    server, state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
    )
    url = await server.start()
    config = ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=30.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )

    def capture() -> tuple[str, str, str, int]:
        captured = capture_server_review(
            config,
            repo,
            resolve_rev_range(repo, working_tree=True),
            store_root=tmp_path / "client-store",
            session_id=None,
            limit=200,
            with_impact=False,
            with_provenance=False,
            progress=progress,
        )
        revision = captured.response["revision"]
        return captured.repo_id, captured.review_id, str(revision["id"]), int(revision["revision_number"])

    def flow() -> None:
        repo_id, review_id, first_id, first_number = capture()
        current = source_state(repo, resolve_rev_range(repo, working_tree=True))
        assert current.legacy_fingerprint and current.legacy_fingerprint != current.fingerprint
        # The Snapshot as the engine recorded it before the upgrade.
        store = state.review_repository.store(repo_id)
        store.set_revision_source_fingerprint(first_id, current.legacy_fingerprint)

        assert capture() == (repo_id, review_id, first_id, first_number)
        assert store.latest_revision(review_id).source_fingerprint == current.legacy_fingerprint  # type: ignore[union-attr]

        (repo / "README.md").write_text("three\n", encoding="utf-8")
        changed = capture()
        assert changed[:2] == (repo_id, review_id)
        assert (changed[2] != first_id, changed[3]) == (True, first_number + 1)

    try:
        await asyncio.to_thread(flow)
    finally:
        await server.stop()


def test_review_title_tracks_snapshot_and_custom_name_can_be_cleared(tmp_path: Path) -> None:
    session = SimpleNamespace(title="working tree")
    first = SimpleNamespace(revision_number=1)
    second = SimpleNamespace(revision_number=2)
    assert _automatic_review_title(session, None, None) == "working tree"
    assert (
        _automatic_review_title(session, first, {"title": "working tree"}, author_title="Fix review labels")
        == "Fix review labels"
    )
    assert (
        _automatic_review_title(
            session,
            first,
            {
                "title": "working tree",
                "files": [
                    {"path": "frontend/src/App.tsx", "status": "modified"},
                ],
            },
        )
        == "Snapshot 0: Update frontend/src/App.tsx"
    )
    assert (
        _automatic_review_title(
            session,
            second,
            {
                "title": "working tree",
                "files": [
                    {"path": "server/app.py", "status": "modified"},
                ],
            },
        )
        == "Snapshot 1: Update server/app.py"
    )

    reviews = LocalReviewRepository(tmp_path / "reviews")
    repo_id = "repo_0123456789abcdef0123456789abcdef"
    review_id = "review_0123456789abcdef0123456789abcdef"
    revision_id = "rev_0123456789abcdef0123456789abcdef"
    store = reviews.store(repo_id)
    stored_session = store.create_session(
        ReviewSession(
            id=review_id,
            subject_type="local_change",
            repo_root=str(tmp_path / "repo"),
            range_mode="working_tree",
        )
    )
    store.add_revision(
        ReviewRevision(
            id=revision_id,
            review_id=stored_session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree-title",
            packet_schema_version=1,
        ),
        (),
    )
    reviews.set_author_snapshot_title(repo_id, revision_id, "Fix review labels")
    assert reviews.author_snapshot_title(repo_id, revision_id) == "Fix review labels"
    assert store.get_revision(revision_id).title == "Fix review labels"
    reviews.set_manual_review_title(repo_id, review_id, "My review")
    assert reviews.manual_review_title(repo_id, review_id) == "My review"
    reviews.set_manual_review_title(repo_id, review_id, "")
    assert reviews.manual_review_title(repo_id, review_id) == ""


def test_review_title_endpoint_saves_and_restores_auto(tmp_path: Path) -> None:
    async def exercise() -> None:
        server, state = build_local_server(
            LocalServerConfig(
                listen_port=0,
                state_root=tmp_path / "state",
                local_no_auth=True,
                allow_local_fs=False,
            )
        )
        repo_id = "repo_0123456789abcdef0123456789abcdef"
        session = state.review_repository.store(repo_id).create_session(
            ReviewSession(
                id="",
                subject_type="local_change",
                repo_root=str(tmp_path),
                range_mode="working_tree",
                title="working tree",
            )
        )
        url = await server.start()
        try:
            async with ClientSession() as client:
                path = f"{url}/api/repos/{repo_id}/reviews/{session.id}/title"
                named = await client.put(path, json={"title": "My change"}, headers={"Origin": url})
                assert named.status == 200
                payload = await named.json()
                assert payload["title"] == "My change"
                assert payload["auto_title"] == "working tree"
                assert payload["title_is_custom"] is True
                inbox = await client.get(f"{url}/v1/reviews")
                assert (await inbox.json())["reviews"][0]["title"] == "My change"
                restored = await client.put(path, json={"title": ""}, headers={"Origin": url})
                assert restored.status == 200
                assert (await restored.json())["title_is_custom"] is False
        finally:
            await server.stop()

    asyncio.run(exercise())


def test_local_review_uses_the_same_server_backed_flow(tmp_path: Path, monkeypatch) -> None:
    import sys

    from lemoncrow_server_core.index.repositories import _ContentRouter

    get = _ContentRouter.get
    get_many = _ContentRouter.get_many
    capture_batches: list[int] = []

    def checked_get(self, org_id, digest):
        assert sys._getframe(1).f_code.co_name != "_review_capture_sync", "capture read a file individually"
        return get(self, org_id, digest)

    def checked_get_many(self, org_id, digests):
        if sys._getframe(1).f_code.co_name == "_review_capture_sync":
            capture_batches.append(len(digests))
        return get_many(self, org_id, digests)

    monkeypatch.setattr(_ContentRouter, "get", checked_get)
    monkeypatch.setattr(_ContentRouter, "get_many", checked_get_many)
    asyncio.run(_exercise_review(tmp_path))
    assert capture_batches and any(capture_batches)


async def _exercise_review(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    frontend = repo / "frontend"
    review_dir = frontend / "src" / "review"
    review_dir.mkdir(parents=True)
    (frontend / "package.json").write_text(
        '{"dependencies":{"vite":"6","react":"19","react-router-dom":"7"}}\n',
        encoding="utf-8",
    )
    (frontend / "src" / "main.tsx").write_text(
        'import App from "./App"; render(<App/>);\n',
        encoding="utf-8",
    )
    (frontend / "src" / "App.tsx").write_text(
        'import { Route, Routes } from "react-router-dom";\n'
        'import ReviewReader from "./review/ReviewReader";\n'
        'export default function App(){ return <Routes><Route path="/review" element={<ReviewReader/>}/></Routes> }\n',
        encoding="utf-8",
    )
    reader = review_dir / "ReviewReader.tsx"
    reader.write_text(
        "export default function ReviewReader(){ return <main>before</main> }\n",
        encoding="utf-8",
    )
    _git(repo, "add", "frontend")
    _git(repo, "commit", "-qm", "initial")
    reader.write_text(
        "export default function ReviewReader(){ return <main>after</main> }\n",
        encoding="utf-8",
    )

    server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=True,
        )
    )
    url = await server.start()
    config = ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=30.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )

    def client_flow() -> tuple[
        object,
        list[dict[str, object]],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        revision = resolve_rev_range(repo, working_tree=True)
        captured = capture_server_review(
            config,
            repo,
            revision,
            store_root=tmp_path / "client-store",
            session_id=None,
            limit=200,
            with_impact=False,
            with_provenance=False,
        )
        revision_row = captured.response.get("revision")
        assert isinstance(revision_row, dict)
        revision_id = str(revision_row.get("id") or "")
        revision_number = revision_row.get("revision_number")
        assert isinstance(revision_number, int)
        # Capture returning is the durability boundary for this exact Snapshot.
        # Saving it immediately must not depend on a dynamically-built Reader app.
        published = server_review_request(
            config,
            "POST",
            f"/api/reviews/{captured.review_id}/revisions/{revision_number}/publish",
            body={},
        )
        assert published == {"revision_id": revision_id, "revision_number": revision_number}

        rows = server_review_rows(config)
        detail = server_review_detail(config, captured.repo_id, captured.review_id)
        overview = server_review_request(config, "GET", f"/api/reviews/{captured.review_id}")
        surfaces = server_review_request(config, "GET", f"/api/reviews/{captured.review_id}/surfaces")

        captured_map = server_review_request(config, "GET", f"/api/reviews/{captured.review_id}/map")
        captured_view_id = str(captured_map["context"]["index"]["view_id"])
        assert captured_view_id

        transport = RemoteSession(config, publish_live_view=False)
        transport.handshake(timeout_s=config.request_timeout_s)
        transport.open_session(timeout_s=config.request_timeout_s)
        newer_view = transport.open_view(timeout_s=config.tool_timeout_s)
        native_map = server_review_request(config, "GET", f"/api/reviews/{captured.review_id}/map")
        assert native_map["context"]["source"] == "current_index"
        assert native_map["context"]["exact_for_snapshot"] is False
        assert native_map["context"]["index"]["backend"] == "native"
        assert native_map["context"]["index"]["view_id"] == captured_view_id
        assert native_map["context"]["index"]["view_id"] != newer_view["view_id"]
        enriched_packet = replace(
            captured.build.packet,
            order=tuple(
                ReviewOrderEntry(
                    path=item.path,
                    rank=index,
                    score=99.0 - index,
                    reasons=("progressive enrichment test",),
                    group=item.category,
                )
                for index, item in enumerate(captured.build.packet.files, start=1)
            ),
        )
        enriched = transport.enrich_review_projection(
            repo_id=captured.repo_id,
            review_id=captured.review_id,
            revision_id=revision_id,
            packet=enriched_packet.to_dict(),
        )
        preparation_id = "prep-progressive-local"
        transport.update_review_preparation(
            repo_id=captured.repo_id,
            review_id=captured.review_id,
            preparation_id=preparation_id,
            state="impact_background",
            detail="source is already readable",
        )
        preparation = server_review_request(
            config,
            "GET",
            f"/api/reviews/{captured.review_id}/preparation?preparation_id={preparation_id}",
        )
        identity_rejected = False
        try:
            transport.enrich_review_projection(
                repo_id=captured.repo_id,
                review_id=captured.review_id,
                revision_id=revision_id,
                packet=replace(captured.build.packet, files=()).to_dict(),
            )
        except ClientError as exc:
            identity_rejected = "target identity" in exc.message
        transport.close()
        return captured, rows, detail, overview, surfaces, enriched, preparation, identity_rejected

    try:
        captured, rows, detail, overview, surfaces, enriched, preparation, identity_rejected = await asyncio.to_thread(
            client_flow
        )
        assert len(rows) == 1
        assert rows[0]["id"] == captured.review_id
        assert rows[0]["repo_id"] == captured.repo_id
        assert rows[0]["revision_number"] == 0
        assert rows[0]["revision_number"] == 0
        assert rows[0]["title"].startswith("Snapshot 0: Update frontend/src/review/ReviewReader.tsx")
        assert rows[0]["auto_title"] == rows[0]["title"]
        assert rows[0]["title_is_custom"] is False
        assert rows[0]["project"] == "repo"
        assert rows[0]["worktree"] == "repo"
        assert rows[0]["worktree_root"] == str(repo.resolve())
        assert rows[0]["branch"] == _review_location_metadata(repo)["branch"]
        assert rows[0]["progress"]["target_count"] >= 1
        assert rows[0]["progress"]["reviewed"] == 0
        assert rows[0]["progress"]["changed_since_review"] == 0
        assert detail["review"]["id"] == captured.review_id
        assert detail["revision"]["revision_number"] == 0
        assert detail["revision_count"] == 1
        assert captured.response["revision"]["revision_number"] == 0
        assert state.review_repository.checkout_root(captured.repo_id) == repo.resolve()
        assert state.review_repository.workspace_root_hint(captured.repo_id, captured.review_id) == str(repo.resolve())
        assert state.review_repository.workspace_root_hint(captured.repo_id, captured.review_id) == str(repo.resolve())
        named = await asyncio.to_thread(
            server_review_request,
            config,
            "PUT",
            f"/api/repos/{captured.repo_id}/reviews/{captured.review_id}/title",
            body={"title": "Review reader update"},
        )
        assert named["title"] == "Review reader update"
        assert named["auto_title"].startswith("Snapshot 0: Update")
        assert named["title_is_custom"] is True
        renamed_rows = await asyncio.to_thread(server_review_rows, config)
        assert renamed_rows[0]["title"] == "Review reader update"
        restored = await asyncio.to_thread(
            server_review_request,
            config,
            "PUT",
            f"/api/repos/{captured.repo_id}/reviews/{captured.review_id}/title",
            body={"title": ""},
        )
        assert restored["title"] == restored["auto_title"]
        assert restored["title_is_custom"] is False
        # Display identity must survive without turning the packet path into a
        # trusted source-read grant.  The hint is per Change, while source access
        # remains gated by the negotiated checkout marker.
        state.review_repository.set_checkout_root(captured.repo_id, None)
        fallback_rows = await asyncio.to_thread(server_review_rows, config)
        assert fallback_rows[0]["project"] == "repo"
        assert fallback_rows[0]["worktree"] == "repo"
        assert fallback_rows[0]["worktree_root"] == str(repo.resolve())
        assert fallback_rows[0]["branch"] == _review_location_metadata(repo)["branch"]
        assert state.review_repository.checkout_root(captured.repo_id) is None
        state.review_repository.set_checkout_root(captured.repo_id, repo)
        # Inbox progress is cached per session; a new mark must replace it.
        assert fallback_rows[0]["progress"]["reviewed"] == 0
        review_store = state.review_repository.store(captured.repo_id)
        latest = review_store.latest_revision(captured.review_id)
        review_session = review_store.get_session(captured.review_id)
        assert latest is not None and review_session is not None
        for unit in review_store.list_units(latest.id):
            review_store.set_mark(
                ReviewMark(
                    review_id=captured.review_id,
                    unit_key=unit.unit_key,
                    state="reviewed",
                    reviewed_revision_id=latest.id,
                    content_fingerprint=unit.content_fingerprint,
                    reviewer_id=review_session.reviewer_id,
                )
            )
        marked_rows = await asyncio.to_thread(server_review_rows, config)
        marked_progress = marked_rows[0]["progress"]
        assert marked_progress["reviewed"] == marked_progress["target_count"] >= 1
        overview_id = str(
            overview.get("id") or (overview.get("session") or {}).get("id") or (overview.get("review") or {}).get("id")
        )
        assert overview_id == captured.review_id
        assert overview["session"]["ref"].startswith("r/")
        assert overview["revision"]["ref"].startswith("rr/")
        revision_ref = str(overview["revision"]["ref"])
        revision_payload = revision_ref.removeprefix("rr/")
        located_revision = await asyncio.to_thread(
            server_review_request,
            config,
            "GET",
            f"/api/revisions/{revision_payload}",
        )
        assert located_revision["review"]["id"] == captured.review_id
        assert located_revision["revision"]["id"] == detail["revision"]["id"]
        assert surfaces["revision_id"] == detail["revision"]["id"]
        assert enriched["enriched"] is True
        assert enriched["revision_id"] == detail["revision"]["id"]
        assert preparation["state"] == "impact_background"
        assert preparation["detail"] == "source is already readable"
        assert identity_rejected is True
        assert surfaces["surfaces"]
        assert any(
            item["provider"] == "web" and item["kind"] == "web.route" and item["locator"] == "/review"
            for item in surfaces["surfaces"]
        )

        initial_source = await asyncio.to_thread(
            server_review_request,
            config,
            "GET",
            f"/api/reviews/{captured.review_id}/source-state",
        )
        assert initial_source["supported"] is True
        assert initial_source["changed"] is False

        reader.write_text(
            "export default function ReviewReader(){ return <main>after again</main> }\n",
            encoding="utf-8",
        )
        changed_source = await asyncio.to_thread(
            server_review_request,
            config,
            "GET",
            f"/api/reviews/{captured.review_id}/source-state",
        )
        assert changed_source["changed"] is True

        review_ref = str(overview["session"]["ref"])
        review_compare = await asyncio.to_thread(
            server_review_request,
            config,
            "GET",
            "/api/compare?" + urlencode({"from_ref": revision_ref, "to_ref": "worktree"}),
        )
        assert review_compare["from"]["kind"] == "review_revision"
        assert review_compare["to"]["kind"] == "worktree"
        assert any(item["path"] == "frontend/src/review/ReviewReader.tsx" for item in review_compare["files"])

        git_compare = await asyncio.to_thread(
            server_review_request,
            config,
            "GET",
            "/api/compare?" + urlencode({"from_ref": "git/HEAD", "to_ref": "worktree", "scope": review_ref}),
        )
        assert git_compare["from"]["kind"] == "git"
        assert git_compare["to"]["kind"] == "worktree"
        assert any(item["path"] == "frontend/src/review/ReviewReader.tsx" for item in git_compare["files"])

        git_only_compare = await asyncio.to_thread(
            server_review_request,
            config,
            "GET",
            "/api/compare?" + urlencode({"from_ref": "git/HEAD", "to_ref": "git/HEAD"}),
        )
        assert git_only_compare["from"]["kind"] == "git"
        assert git_only_compare["to"]["kind"] == "git"
        assert git_only_compare["files"] == []

        refreshed = await asyncio.to_thread(
            server_review_request,
            config,
            "POST",
            f"/api/reviews/{captured.review_id}/refresh",
        )
        assert refreshed["session"]["id"] == captured.review_id
        assert refreshed["revision"]["revision_number"] == 1
        assert refreshed["refreshed"]["created"] is True

        settled_source = await asyncio.to_thread(
            server_review_request,
            config,
            "GET",
            f"/api/reviews/{captured.review_id}/source-state",
        )
        assert settled_source["changed"] is False
    finally:
        await server.stop()


def test_force_new_capture_imports_pre_review_agent_comment_after_binding(tmp_path: Path, monkeypatch) -> None:
    asyncio.run(_exercise_force_new_pre_review_comment(tmp_path, monkeypatch))


async def _exercise_force_new_pre_review_comment(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "initial")
    (repo / "app.py").write_text("value = 2\n", encoding="utf-8")

    agent_root = tmp_path / "agent-root"
    monkeypatch.setenv("LEMONCROW_ROOT", str(agent_root))
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        '{"type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer",'
        '"id":"pre-review","content":[{"type":"output_text","text":"Updated app.py:1 for the new value."}]}}\n',
        encoding="utf-8",
    )
    assert (
        capture_session_comment(
            agent_root,
            {
                "session_id": "pre-review-code",
                "transcript_path": str(transcript),
                "cwd": str(repo),
                "agent_role": "code",
            },
            host="codex",
        )
        == 1
    )

    server, state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
    )
    url = await server.start()
    config = ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=30.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )
    try:
        captured = await asyncio.to_thread(
            capture_server_review,
            config,
            repo,
            resolve_rev_range(repo, working_tree=True),
            store_root=tmp_path / "client-store",
            session_id=None,
            limit=200,
            with_impact=False,
            with_provenance=False,
            force_new_review=True,
        )
        store = state.review_repository.store(captured.repo_id)
        session = store.get_session(captured.review_id)
        assert session is not None
        revision = store.latest_revision(session.id)
        assert revision is not None
        annotations = store.list_annotations_at_revision(session.id, revision.id)
        assert [item.body for item in annotations] == ["Updated app.py:1 for the new value."]
        assert annotations[0].source == "author"
        assert annotations[0].agent_role == "code"
    finally:
        await server.stop()


def test_a_repository_keeps_one_review_store_until_its_database_is_removed(tmp_path: Path) -> None:
    # Every Reader request resolves its Review across repositories. A fresh
    # store per call re-ran schema setup each time, on the request's thread.
    reviews = LocalReviewRepository(tmp_path / "reviews")
    repo_id = "repo_0123456789abcdef0123456789abcdef"
    first = reviews.store(repo_id)
    assert first.list_sessions(status="", limit=None) == ()
    assert reviews.store(repo_id) is first

    shutil.rmtree(reviews.store_root(repo_id))
    again = reviews.store(repo_id)
    assert again is not first
    assert again.list_sessions(status="", limit=None) == ()


def test_referenced_digests_read_each_stored_tree_once_and_hold_each_digest_once(tmp_path: Path, monkeypatch) -> None:
    # Local maintenance asks on every run. Held per revision, the answer grew by
    # a whole source tree for every revision ever stored.
    class Trees:
        def __init__(self) -> None:
            self.revisions = {"s1": ["r1", "r2"]}
            self.trees = {("r1", "new"): "ab", ("r1", "old"): "a", ("r2", "new"): "ac"}
            self.reads: list[tuple[str, str]] = []

        def list_sessions(self, *, status: str, limit: int | None) -> tuple[SimpleNamespace, ...]:
            return tuple(SimpleNamespace(id=session_id) for session_id in self.revisions)

        def list_revisions(self, session_id: str) -> tuple[SimpleNamespace, ...]:
            return tuple(SimpleNamespace(id=revision_id) for revision_id in self.revisions[session_id])

        def read_source_tree_artifact(self, session_id: str, revision_id: str, *, side: str):
            self.reads.append((revision_id, side))
            digests = self.trees.get((revision_id, side))
            return None if digests is None else {f"{d}.py": {"content_digest": d} for d in digests}

    reviews = LocalReviewRepository(tmp_path / "reviews")
    repo_id = "repo_0123456789abcdef0123456789abcdef"
    reviews.store_root(repo_id).mkdir(parents=True)
    trees = Trees()
    monkeypatch.setattr(LocalReviewRepository, "store", lambda _self, _repo_id: trees)
    key = ("org", repo_id)

    assert reviews.referenced_digests("org") == {key: frozenset("abc")}
    assert sorted(trees.reads) == [("r1", "new"), ("r1", "old"), ("r2", "new"), ("r2", "old")]

    # Only a tree not read yet is read again: the old side attached since.
    trees.reads.clear()
    trees.trees[("r2", "old")] = "d"
    assert reviews.referenced_digests("org") == {key: frozenset("abcd")}
    assert trees.reads == [("r2", "old")]

    # Content is held once per repository, not once per revision naming it.
    for number in range(3, 50):
        trees.revisions["s1"].append(f"r{number}")
        trees.trees[(f"r{number}", "new")] = trees.trees[(f"r{number}", "old")] = "abcd"
    assert reviews.referenced_digests("org") == {key: frozenset("abcd")}
    assert sum(len(held) for _read, held in reviews._referenced.values()) == 4

    # A removed revision re-derives the union: what only it named is released.
    trees.revisions["s1"] = ["r2"]
    assert reviews.referenced_digests("org") == {key: frozenset("acd")}
    shutil.rmtree(reviews.store_root(repo_id))
    assert reviews.referenced_digests("org") == {}
    assert reviews._referenced == {}


def test_a_review_reopens_its_lapsed_snapshot_view_and_keeps_its_content(tmp_path: Path) -> None:
    asyncio.run(_exercise_restored_snapshot_view(tmp_path))


async def _exercise_restored_snapshot_view(tmp_path: Path) -> None:
    # A Snapshot's View lapses with its lease (a day), and with it the Reader's
    # code intelligence. The revision stores the exact tree it named, so a
    # Review opened later reopens that View instead of degrading, and cleanup
    # keeps the content that tree names.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "restore@example.com")
    _git(repo, "config", "user.name", "Restore")
    (repo / "lib.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (repo / "app.py").write_text("from lib import helper\n\ndef run():\n    return helper()\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    (repo / "app.py").write_text("from lib import helper\n\ndef run():\n    return helper() + 1\n", encoding="utf-8")

    server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=True,
        )
    )
    url = await server.start()
    config = ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=30.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )
    try:

        def flow() -> tuple[str, str, str, str, str]:
            captured = capture_server_review(
                config,
                repo,
                resolve_rev_range(repo, working_tree=True),
                store_root=tmp_path / "client-store",
                session_id=None,
                limit=200,
                with_impact=False,
                with_provenance=False,
            )
            before = server_review_request(config, "GET", f"/api/reviews/{captured.review_id}/map")
            original = str(before["context"]["index"]["view_id"])
            state.backend.views.close(state.local_principal.org_id, original)  # the lease lapsed
            after = server_review_request(config, "GET", f"/api/reviews/{captured.review_id}/map")
            restored = str(after["context"]["index"]["view_id"])

            # A revision captured before Views were bound to it, whose repository
            # has no live View left, opens its own stored tree as well.
            org_id, subject = state.local_principal.org_id, state.local_principal.subject
            store = state.review_repository.store(captured.repo_id)
            latest = store.latest_revision(captured.review_id)
            assert latest is not None
            store.set_revision_source_view(latest.id, view_id="", view_revision=0)
            for view_id in state.backend.views.view_ids_for_user(org_id, subject):
                state.backend.views.close(org_id, view_id)
            unbound = server_review_request(config, "GET", f"/api/reviews/{captured.review_id}/map")
            return captured.repo_id, captured.review_id, original, restored, str(unbound["context"]["index"]["view_id"])

        repo_id, review_id, original, restored, reopened = await asyncio.to_thread(flow)
        assert original
        assert restored and restored != original
        assert reopened and reopened not in {original, restored}
        store = state.review_repository.store(repo_id)
        revision = store.latest_revision(review_id)
        assert revision is not None and revision.source_view_id == reopened
        tree = store.read_source_tree_artifact(review_id, revision.id, side="new")
        assert tree
        kept = state.review_repository.referenced_digests(state.local_principal.org_id)
        assert {str(row["content_digest"]) for row in tree.values()} <= kept[(state.local_principal.org_id, repo_id)]
    finally:
        await server.stop()


def test_clean_capture_after_commit_reuses_commit_checkpoint_without_fake_draft(tmp_path: Path, monkeypatch) -> None:
    asyncio.run(_exercise_clean_commit_checkpoint(tmp_path, monkeypatch))


async def _exercise_clean_commit_checkpoint(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.test")
    _git(repo, "config", "user.name", "Checkpoint")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "initial")
    (repo / "app.py").write_text("value = 2\n", encoding="utf-8")

    server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=True,
        )
    )
    url = await server.start()
    config = load_config(
        {
            "LEMONCROW_URL": url,
            "LEMONCROW_INSTALL_MODE": "local",
            "LEMONCROW_HOME": str(tmp_path / "client"),
            "LEMONCROW_LOCAL_FS": "1",
            "HOME": str(tmp_path),
        },
        cwd=repo,
    )
    try:

        def flow() -> tuple[str, str, str]:
            first = capture_server_review(
                config,
                repo,
                resolve_rev_range(repo, working_tree=True),
                store_root=tmp_path / "client-store",
                session_id=None,
                limit=200,
                with_impact=False,
                with_provenance=False,
            )

            # A failed explicit --new capture must not steal the branch binding
            # from the Review that was successfully published before it.
            from lemoncrow_server_core import local_reviews as local_review_module

            original_record_revision = local_review_module.record_revision

            def fail_record(*_args, **_kwargs):
                raise RuntimeError("synthetic capture failure")

            monkeypatch.setattr(local_review_module, "record_revision", fail_record)
            try:
                try:
                    capture_server_review(
                        config,
                        repo,
                        resolve_rev_range(repo, working_tree=True),
                        store_root=tmp_path / "client-store",
                        session_id=None,
                        limit=200,
                        with_impact=False,
                        with_provenance=False,
                        force_new_review=True,
                    )
                except (ClientError, RuntimeError):
                    pass
                else:
                    raise AssertionError("synthetic force-new capture unexpectedly succeeded")
            finally:
                monkeypatch.setattr(local_review_module, "record_revision", original_record_revision)

            first_store = state.review_repository.store(first.repo_id)
            active_bindings = first_store.list_bindings(first.review_id, active_only=True)
            assert active_bindings, "failed --new capture must preserve the previous active branch binding"

            _git(repo, "add", "app.py")
            staged = capture_server_review(
                config,
                repo,
                resolve_rev_range(repo, staged=True),
                store_root=tmp_path / "client-store",
                session_id=None,
                limit=200,
                with_impact=False,
                with_provenance=False,
            )
            assert staged.review_id != first.review_id
            working_again = capture_server_review(
                config,
                repo,
                resolve_rev_range(repo, working_tree=True),
                store_root=tmp_path / "client-store",
                session_id=None,
                limit=200,
                with_impact=False,
                with_provenance=False,
            )
            assert working_again.review_id == first.review_id
            _git(repo, "commit", "-qm", "checkpoint")
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            second = capture_server_review(
                config,
                repo,
                resolve_rev_range(repo, working_tree=True),
                store_root=tmp_path / "client-store",
                session_id=None,
                limit=200,
                with_impact=False,
                with_provenance=False,
            )
            assert second.review_id == first.review_id
            store = state.review_repository.store(second.repo_id)
            revisions = store.list_revisions(second.review_id)
            assert [revision.capture_kind for revision in revisions] == ["draft", "commit"]
            latest = revisions[-1]
            assert latest.commit_sha == head
            assert latest.range_mode == "commit_range"
            return first.review_id, second.review_id, latest.id

        first_id, second_id, latest_id = await asyncio.to_thread(flow)
        assert first_id == second_id
        assert latest_id
    finally:
        await server.stop()


def test_unborn_local_review_records_first_commit_checkpoint(tmp_path: Path) -> None:
    asyncio.run(_exercise_unborn_first_commit(tmp_path))


async def _exercise_unborn_first_commit(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.gitdiff import EMPTY_TREE_SHA

    repo = tmp_path / "unborn"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.test")
    _git(repo, "config", "user.name", "Unborn")
    (repo / "first.py").write_text("FIRST = 1\n", encoding="utf-8")

    server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=True,
        )
    )
    url = await server.start()
    config = load_config(
        {
            "LEMONCROW_URL": url,
            "LEMONCROW_INSTALL_MODE": "local",
            "LEMONCROW_HOME": str(tmp_path / "client"),
            "LEMONCROW_LOCAL_FS": "1",
            "HOME": str(tmp_path),
        },
        cwd=repo,
    )
    try:

        def flow() -> None:
            first = capture_server_review(
                config,
                repo,
                resolve_rev_range(repo, working_tree=True),
                store_root=tmp_path / "client-store",
                session_id=None,
                limit=200,
                with_impact=False,
                with_provenance=False,
            )
            store = state.review_repository.store(first.repo_id)
            session = store.get_session(first.review_id)
            assert session is not None and session.review_base_sha == EMPTY_TREE_SHA
            assert state.review_repository.checkout_root(first.repo_id) == repo.resolve()

            _git(repo, "add", "first.py")
            _git(repo, "commit", "-qm", "first")
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
            second = capture_server_review(
                config,
                repo,
                resolve_rev_range(repo, working_tree=True),
                store_root=tmp_path / "client-store",
                session_id=None,
                limit=200,
                with_impact=False,
                with_provenance=False,
            )
            assert second.review_id == first.review_id
            revisions = store.list_revisions(first.review_id)
            assert [item.capture_kind for item in revisions] == ["draft", "commit"]
            assert revisions[-1].commit_sha == head
            assert revisions[-1].base_sha == EMPTY_TREE_SHA
            assert revisions[-1].dirty is False

        await asyncio.to_thread(flow)
    finally:
        await server.stop()


def test_review_only_capture_records_original_checkout_for_preview_dependencies(tmp_path: Path) -> None:
    asyncio.run(_exercise_review_only_checkout(tmp_path))


async def _exercise_review_only_checkout(tmp_path: Path) -> None:
    from lemoncrow_client.review_session import ReviewSession as CaptureSession

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    base = tmp_path / "base"
    head = tmp_path / "head"
    for root, text in ((base, "before"), (head, "frozen after"), (repo, "live differs")):
        (root / "frontend").mkdir(parents=True, exist_ok=True)
        (root / "frontend" / "package.json").write_text('{"devDependencies":{"vite":"6"}}')
        (root / "frontend" / "app.js").write_text(text)
    server, state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
    )
    url = await server.start()
    config = ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=30.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )

    def flow() -> None:
        session = CaptureSession(config)
        try:
            session.open_session()
            base_view = session.open_snapshot(base, source_revision="base")
            session.open_snapshot(head, source_revision="head")
            result = session.capture_from_views(
                source_ref="review:test",
                base_view_id=str(base_view["view_id"]),
                base_view_revision=int(base_view["view_revision"]),
                with_impact=False,
                range_spec={
                    "mode": "working_tree",
                    "base_rev": "HEAD",
                    "head_rev": "WORKDIR",
                    "base_sha": "a" * 40,
                    "head_sha": "",
                    "merge_base_sha": "",
                    "dirty": True,
                },
            )
            review_id = str(result["review"]["id"])
            assert state.review_repository.source_root_for(session.state.repo_id, review_id) == repo.resolve()
            store = state.review_repository.store(session.state.repo_id)
            revision = store.latest_revision(review_id)
            assert revision is not None
            from lemoncrow.pro.capabilities.review.web_preview import _materialize_review_side

            target = tmp_path / "render-source"
            _materialize_review_side(
                store.root, repo, revision, side="new", target=target, scope="frontend", store=store
            )
            assert (target / "frontend" / "app.js").read_text() == "frozen after"
        finally:
            session.close()

    try:
        await asyncio.to_thread(flow)
    finally:
        await server.stop()
