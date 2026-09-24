from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from lemoncrow_client.config import ClientConfig
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.hosted import (
    capture_server_review,
    server_review_detail,
    server_review_request,
    server_review_rows,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


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
        return captured, rows, detail, overview, surfaces

    try:
        captured, rows, detail, overview, surfaces = await asyncio.to_thread(client_flow)
        assert len(rows) == 1
        assert rows[0]["id"] == captured.review_id
        assert rows[0]["repo_id"] == captured.repo_id
        assert rows[0]["progress"]["target_count"] >= 1
        assert rows[0]["progress"]["reviewed"] == 0
        assert rows[0]["progress"]["changed_since_review"] == 0
        assert detail["review"]["id"] == captured.review_id
        assert detail["revision_count"] == 1
        assert state.review_repository.checkout_root(captured.repo_id) == repo.resolve()
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
        assert refreshed["revision"]["revision_number"] == 2
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
