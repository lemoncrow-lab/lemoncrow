"""A local Review stores every file the blob transport can carry, whatever the index cap."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from lemoncrow_client.config import ClientConfig
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.hosted import capture_server_review
from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, materialize_review_side


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _content(name: str, size: int) -> bytes:
    return name.encode().ljust(size, b"z")


def test_a_captured_review_keeps_large_files_and_names_only_what_cannot_travel(tmp_path: Path) -> None:
    asyncio.run(_exercise(tmp_path))


async def _exercise(tmp_path: Path) -> None:
    server, state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
    )
    limits = state.config.limits
    ladder = {
        "at-index-cap.bin": limits.max_indexed_content_bytes,
        "over-index-cap.bin": limits.max_indexed_content_bytes + 1,
        "at-blob-limit.bin": limits.max_blob_bytes,
        "over-blob-limit.bin": limits.max_blob_bytes + 1,
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "review@example.test")
    _git(repo, "config", "user.name", "Reviewer")
    (repo / "app.py").write_text("answer = 1\n", encoding="utf-8")
    for name, size in ladder.items():
        (repo / name).write_bytes(_content(name, size))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "first")
    (repo / "app.py").write_text("answer = 2\n", encoding="utf-8")

    url = await server.start()
    config = ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=30.0,
        tool_timeout_s=120.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )

    def flow() -> None:
        captured = capture_server_review(
            config,
            repo,
            resolve_rev_range(repo, working_tree=True),
            store_root=tmp_path / "client-store",
            session_id=None,
            limit=50,
            with_impact=False,
            with_provenance=False,
        )
        store = state.review_repository.store(captured.repo_id)
        revision = store.latest_revision(captured.review_id)
        assert revision is not None

        for side in ("new", "old"):
            target = tmp_path / f"ask-{side}"
            unavailable: list[str] = []
            materialize_review_side(
                store.root, repo, revision, side=side, target=target, store=store, unavailable=unavailable
            )
            assert unavailable == ["over-blob-limit.bin"], side
            for name in ("at-index-cap.bin", "over-index-cap.bin", "at-blob-limit.bin"):
                assert (target / name).read_bytes() == _content(name, ladder[name]), (side, name)
            assert not (target / "over-blob-limit.bin").exists()

            # Whatever runs the tree still needs all of it.
            with pytest.raises(ReviewSnapshotUnavailable, match=r"over-blob-limit\.bin"):
                materialize_review_side(store.root, repo, revision, side=side, target=tmp_path / "exec", store=store)

    try:
        await asyncio.to_thread(flow)
    finally:
        await server.stop()
