from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

from lemoncrow_client.config import ClientConfig
from lemoncrow_client.errors import ClientError
from lemoncrow_client.session import RemoteSession
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.local_app import _review_location_metadata
from lemoncrow_server_core.local_reviews import LocalReviewRepository

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.hosted import (
    capture_server_review,
    server_review_detail,
    server_review_request,
    server_review_rows,
)
from lemoncrow.pro.capabilities.review.models import ReviewOrderEntry


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


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


def test_local_review_uses_the_same_server_backed_flow(tmp_path: Path) -> None:
    asyncio.run(_exercise_review(tmp_path))


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
        rows = server_review_rows(config)
        detail = server_review_detail(config, captured.repo_id, captured.review_id)
        overview = server_review_request(config, "GET", f"/api/reviews/{captured.review_id}")
        surfaces = server_review_request(config, "GET", f"/api/reviews/{captured.review_id}/surfaces")

        revision_row = captured.response.get("revision")
        assert isinstance(revision_row, dict)
        revision_id = str(revision_row.get("id") or "")
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
