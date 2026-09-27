"""`lc review --pr` end to end on the real local server, with GitHub faked at `gh`."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
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
from lemoncrow.pro.capabilities.review.sources import github as github_source

SOURCE_REF = "github:acme/widgets#7"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class _GitHub:
    def __init__(self) -> None:
        self.head = ""
        self.posts: list[dict[str, Any]] = []

    def __call__(self, args: Any, stdin: str | None = None, *, cwd: Path | None = None) -> str:
        call = list(args)
        api = "repos/acme/widgets/pulls/7"
        if call == ["api", "user"]:
            return json.dumps({"login": "bob"})
        if call == ["api", api]:
            return json.dumps({"head": {"sha": self.head}, "user": {"login": "alice"}, "state": "open"})
        if call[:3] == ["api", "--paginate", "--slurp"] and call[3].startswith(f"{api}/files"):
            return json.dumps([[{"filename": "app.py", "patch": "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c"}]])
        if call[:3] == ["api", "--paginate", "--slurp"] and "/comments" in call[3]:
            return json.dumps([[{"id": 5000, "path": "app.py", "body": "why B?", "line": 2}]])
        if call[:3] == ["api", "--paginate", "--slurp"] and call[3].startswith(f"{api}/reviews"):
            return json.dumps([[]])
        if call[:4] == ["api", "--method", "POST", f"{api}/reviews"]:
            self.posts.append(json.loads(stdin or "{}"))
            return json.dumps({"id": 500, "html_url": "https://github.com/acme/widgets/pull/7#pullrequestreview-500"})
        raise AssertionError(f"unexpected gh call: {call}")


def test_a_pull_request_review_captures_refreshes_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = _GitHub()
    monkeypatch.setattr(github_source, "run_gh", github)
    asyncio.run(_exercise(tmp_path, github))


async def _exercise(tmp_path: Path, github: _GitHub) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("a\nb\nc\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("a\nB\nc\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "first push")
    first = _git(repo, "rev-parse", "HEAD")

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

    def capture(head: str) -> Any:
        return capture_server_review(
            config,
            repo,
            resolve_rev_range(repo, base=base, head=head),
            store_root=tmp_path / "client-store",
            session_id=None,
            limit=200,
            with_impact=False,
            with_provenance=False,
            source_ref_override=SOURCE_REF,
            title_override="PR #7: Widgets",
        )

    def flow() -> dict[str, Any]:
        captured = capture(first)
        review_path = f"/api/reviews/{captured.review_id}"
        server_review_request(
            config,
            "POST",
            f"{review_path}/annotations",
            body={
                "path": "app.py",
                "start_line": 2,
                "body": "why B?",
                "expected_revision_id": str(captured.response["revision"]["id"]),
            },
        )
        github.head = first
        preview = server_review_request(config, "POST", f"{review_path}/github/preview", body={})
        published = server_review_request(
            config,
            "POST",
            f"{review_path}/github/publish",
            body={
                "operation_id": preview["operation_id"],
                "expected_revision_id": preview["revision_id"],
                "expected_plan_hash": preview["plan_hash"],
            },
        )

        # The author pushes again: same Review, new revision.
        (repo / "app.py").write_text("a\nB\nc\nd\n", encoding="utf-8")
        _git(repo, "commit", "-qam", "second push")
        second = capture(_git(repo, "rev-parse", "HEAD"))
        return {
            "first": captured,
            "second": second,
            "rows": server_review_rows(config),
            "detail": server_review_detail(config, second.repo_id, second.review_id),
            "relationships": server_review_request(config, "GET", f"{review_path}/relationships"),
            "preview": preview,
            "published": published,
        }

    try:
        result = await asyncio.to_thread(flow)
    finally:
        await server.stop()

    assert result["second"].review_id == result["first"].review_id
    assert result["detail"]["revision_count"] == 2
    row = next(item for item in result["rows"] if item["id"] == result["first"].review_id)
    assert row["source_ref"] == SOURCE_REF
    assert row["title"] == "PR #7: Widgets"
    scm = result["relationships"]["scm"]
    assert len(scm) == 1
    assert scm[0]["provider"] == "github"
    assert scm[0]["relation"] == "tracks"
    assert scm[0]["external_id"] == "acme/widgets#7"
    assert scm[0]["url"] == "https://github.com/acme/widgets/pull/7"
    assert scm[0]["source"] == "imported"

    assert [item["line"] for item in result["preview"]["inline"]] == [2]
    assert result["published"]["state"] == "sent"
    assert github.posts[0]["commit_id"] == first
    assert github.posts[0]["comments"] == [{"path": "app.py", "side": "RIGHT", "line": 2, "body": "why B?"}]
