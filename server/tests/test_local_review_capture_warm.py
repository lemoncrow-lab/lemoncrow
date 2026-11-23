"""A capture derives the Reader's products in the background, before anyone opens the Review URL.

``lc review`` captures (``derive_packet``) and prints the Reader URL. The capture itself is the
durable event that starts the warm-up: the startup overview and targets, every changed file's
frozen code and the Brief's projection are computed then, so the Reader's first reads are served
from prepared results, and the server keeps answering while that work runs.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.review import _capture, resolve_review_range
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

from lemoncrow.pro.capabilities.review.hosted import server_review_request
from lemoncrow.pro.capabilities.review.store import ReviewStore


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    for index in range(3):
        (repo / f"mod{index}.py").write_text(f"def f{index}():\n    return {index}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    for index in range(3):
        (repo / f"mod{index}.py").write_text(f"def f{index}():\n    return {index + 10}\n", encoding="utf-8")
    return repo


def _config(url: str, repo: Path, tmp_path: Path) -> ClientConfig:
    return ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=60.0,
        resolution_cache_bytes=0,
        offer_local_fs=False,
        token_source="test",
    )


def _serve(tmp_path: Path, flow: Callable[[str, Any, Path], Any]) -> Any:
    async def run() -> Any:
        server, state = build_local_server(
            LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=False)
        )
        url = await server.start()
        try:
            return await asyncio.to_thread(flow, url, state, _repo(tmp_path))
        finally:
            await server.stop()

    return asyncio.run(run())


def _wait_for(predicate: Callable[[], bool], timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the capture warm-up")
        time.sleep(0.05)


def _get(url: str, path: str, timeout_s: float) -> dict[str, Any]:
    with urllib.request.urlopen(url + path, timeout=timeout_s) as response:
        return dict(json.loads(response.read()))


def _count_blob_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    original = ReviewStore.read_blob_artifact

    def counted(self: ReviewStore, review_id: str, revision_id: str) -> Any:
        reads.append(revision_id)
        return original(self, review_id, revision_id)

    monkeypatch.setattr(ReviewStore, "read_blob_artifact", counted)
    return reads


def test_a_capture_prepares_the_readers_first_screens_before_any_reader_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob_reads = _count_blob_reads(monkeypatch)

    def flow(url: str, state: Any, repo: Path) -> None:
        config = _config(url, repo, tmp_path)
        response, _link = _capture(
            config, resolve_review_range(repo, None, working_tree=True), with_impact=False, restore_archived=False
        )
        review_id = str(response["review"]["id"])
        service = state.review_service
        # Started by the capture itself: nobody has read the Review yet.
        _wait_for(lambda: bool(service._warmed))
        _wait_for(lambda: not service._warm_tasks)

        cached = {(key[1], key[2]) for key in state.review_reader._responses}
        base = f"/api/reviews/{review_id}"
        # The startup reads exactly as the Reader sends them.
        assert (base, "startup=true") in cached
        assert (f"{base}/targets", "order=recommended&startup=true") in cached
        # The Brief was built by the warm-up (its progress is the one the Reader polls).
        preparation = server_review_request(config, "GET", f"{base}/projection/preparation")
        assert preparation["state"] == "ready"
        # Every changed file's frozen code was read in one pass of the blob artifact.
        warmed_reads = len(blob_reads)
        assert warmed_reads >= 1

        for index in range(3):
            contents = server_review_request(config, "GET", f"{base}/files/mod{index}.py/contents")
            assert contents["new_content"] == f"def f{index}():\n    return {index + 10}\n"
            assert contents["old_content"] == f"def f{index}():\n    return {index}\n"
        # The Reader's card reads started no computation: served from the prepared contents.
        assert len(blob_reads) == warmed_reads

    _serve(tmp_path, flow)


def test_the_server_answers_while_the_capture_warm_up_derives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    original = ReviewStore.read_blob_artifact

    def held(self: ReviewStore, review_id: str, revision_id: str) -> Any:
        # Only the background warm-up's frozen-code pass is held; capture itself never reads it.
        if threading.current_thread().name.startswith("asyncio_"):
            entered.set()
            release.wait(30)
        return original(self, review_id, revision_id)

    monkeypatch.setattr(ReviewStore, "read_blob_artifact", held)

    def flow(url: str, state: Any, repo: Path) -> None:
        config = _config(url, repo, tmp_path)
        try:
            response, _link = _capture(
                config, resolve_review_range(repo, None, working_tree=True), with_impact=False, restore_archived=False
            )
            review_id = str(response["review"]["id"])
            assert entered.wait(30), "the warm-up never reached its frozen-code pass"
            assert state.review_service._warm_tasks, "the warm-up should still be running"
            started = time.monotonic()
            system = _get(url, "/api/local/system", timeout_s=5.0)
            assert system["status"] == "ok" and system["review_count"] == 1
            # A Reader read that `lc review status` makes, answered while the derive runs.
            annotations = server_review_request(config, "GET", f"/api/reviews/{review_id}/annotations")
            assert "counts" in annotations
            assert time.monotonic() - started < 5.0
        finally:
            release.set()
        _wait_for(lambda: not state.review_service._warm_tasks)

    _serve(tmp_path, flow)


def test_an_artifact_write_misses_the_reads_cached_before_it(tmp_path: Path) -> None:
    """Source trees land as files after the revision is readable; the Reader cache must not serve a read from before."""

    from lemoncrow_server_core.local_reviews import _store_stamp, note_artifact_write

    store = ReviewStore(tmp_path / "store")
    store.list_sessions()  # ready: the schema exists
    note_artifact_write(store)  # nothing cached yet: a no-op, and nothing installed
    before = _store_stamp(store)
    assert before is not None
    assert _store_stamp(store) == before
    note_artifact_write(store)
    after = _store_stamp(store)
    assert after is not None and after != before
