from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pygit2

from lemoncrow.pro.capabilities.review import live_runtime


def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Live Review"
    repo.config["user.email"] = "review.local"
    (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Live Review", "review.local", 1700000000, 0)
    repo.create_commit("HEAD", sig, sig, "base", tree, [])
    (root / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    return root


def _draft_only(monkeypatch, prepare: Any) -> None:
    """Route the worker's draft step to *prepare*; publishing a Snapshot fails the test."""
    from lemoncrow.pro.capabilities.review import hosted

    def forbidden_publish(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("background prewarm must draft locally, never publish a Review revision")

    monkeypatch.setattr(hosted, "capture_server_review", forbidden_publish)
    monkeypatch.setattr(hosted, "prepare_local_source_build", prepare)


def _fingerprint(repo: Path) -> str:
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, source_state

    return source_state(repo, resolve_rev_range(repo, working_tree=True)).fingerprint


def test_live_review_binding_round_trip_is_workspace_and_server_scoped(tmp_path: Path) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    binding = live_runtime.LiveReviewBinding(
        server_url="http://127.0.0.1:7420",
        repo_id="repo-live",
        review_id="review-live",
        revision_id="revision-live",
        revision_number=3,
        source_fingerprint="source-fp",
        author_host="claude",
        author_session_id="session-live",
        branch_ref="refs/heads/main",
        worktree_key="worktree-1",
    )
    live_runtime.write_live_review_binding(root, repo, binding)

    loaded = live_runtime.load_live_review_binding(root, repo, server_url=binding.server_url)
    assert loaded is not None
    assert loaded.repo_id == "repo-live"
    assert loaded.review_id == "review-live"
    assert loaded.revision_id == "revision-live"
    assert loaded.revision_number == 3
    assert loaded.source_fingerprint == "source-fp"
    assert loaded.author_host == "claude"
    assert loaded.author_session_id == "session-live"
    assert loaded.branch_ref == "refs/heads/main"
    assert loaded.worktree_key == "worktree-1"
    assert loaded.updated_at > 0
    assert live_runtime.load_live_review_binding(root, repo, server_url="http://127.0.0.1:9") is None
    assert live_runtime.load_live_review_binding(root, _repo(tmp_path / "other"), server_url=binding.server_url) is None


def test_prepare_worker_drafts_the_snapshot_without_publishing(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
    request = live_runtime._write_request(root, repo, host="claude", session_id="session-1")
    drafted: list[tuple[str, str | None]] = []

    def prepare(
        _config: Any, repo_root: Path, _rng: Any, *, store_root: Path, session_id: str | None, **_kwargs: Any
    ) -> str:
        assert store_root == root.resolve()
        drafted.append((_fingerprint(repo_root), session_id))
        return drafted[-1][0]

    _draft_only(monkeypatch, prepare)
    assert live_runtime.prepare_live_review_worker(root, repo, debounce_s=0) == 0

    assert drafted == [(_fingerprint(repo), "session-1")]
    # No Review exists until `lc review`: nothing for `lcr` to open or claim.
    assert live_runtime.load_live_review_binding(root, repo, server_url="http://127.0.0.1:7420") is None
    assert live_runtime._read_request(root, repo) == request


def test_drafted_build_is_the_one_lc_review_reuses(tmp_path: Path, monkeypatch) -> None:
    from lemoncrow.pro.capabilities.review import hosted
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
    from lemoncrow.pro.capabilities.review.precompute import load_source_build

    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    rng = resolve_rev_range(repo, working_tree=True)
    config = SimpleNamespace(hosted=False, url="http://127.0.0.1:9")
    monkeypatch.setattr(hosted, "active_local_review_base", lambda *_args, **_kwargs: ("", ""))

    fingerprint = hosted.prepare_local_source_build(config, repo, rng, store_root=root, session_id=None, limit=40)

    assert fingerprint == _fingerprint(repo)
    reused = load_source_build(root, repo, rng, fingerprint)
    assert reused is not None
    assert [changed.path for changed in reused.packet.files] == ["app.py"]
    # Already drafted: a heartbeat on unchanged source rebuilds nothing.
    assert (
        hosted.prepare_local_source_build(config, repo, rng, store_root=root, session_id=None, limit=40) == fingerprint
    )


def test_prepare_worker_coalesces_an_edit_that_arrives_during_capture(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
    live_runtime._write_request(root, repo, host="codex", session_id="session-old")
    drafted: list[str] = []

    def prepare(_config: Any, repo_root: Path, _rng: Any, **_kwargs: Any) -> str:
        drafted.append(_fingerprint(repo_root))
        if len(drafted) == 1:
            (repo / "app.py").write_text("def value():\n    return 3\n", encoding="utf-8")
            live_runtime._write_request(root, repo, host="codex", session_id="session-new")
        return drafted[-1]

    _draft_only(monkeypatch, prepare)
    assert live_runtime.prepare_live_review_worker(root, repo, debounce_s=0) == 0
    assert len(drafted) == 2
    assert drafted[-1] == _fingerprint(repo)


def test_edit_signal_is_nonblocking_and_spawns_detached_worker(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class _Process:
        pass

    def popen(command: list[str], **kwargs: Any) -> _Process:
        calls.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(live_runtime.subprocess, "Popen", popen)
    request = live_runtime.request_live_review_prepare(root, repo, host="claude", session_id="session-1")

    assert request is not None
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert "lemoncrow.pro.capabilities.review.live_runtime" in command
    assert "--worker" in command
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == str(repo.resolve())


def test_claimed_live_review_keeps_drafting_without_moving_the_binding(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")

    binding = live_runtime.LiveReviewBinding(
        server_url="http://127.0.0.1:7420",
        repo_id="repo-live",
        review_id="review-live",
        revision_id="revision-live",
        revision_number=4,
        source_fingerprint="source-before-claim",
        author_host="claude",
        author_session_id="session-live",
    )
    live_runtime.write_live_review_binding(root, repo, binding)
    claimed = live_runtime.claim_live_review_binding(
        root,
        repo,
        server_url=binding.server_url,
        expected_source_fingerprint="source-before-claim",
    )
    assert claimed is not None
    assert claimed.claimed_at > 0

    live_runtime._write_request(root, repo, host="claude", session_id="session-live")
    drafted: list[str] = []

    def prepare(_config: Any, repo_root: Path, _rng: Any, **_kwargs: Any) -> str:
        drafted.append(_fingerprint(repo_root))
        return drafted[-1]

    _draft_only(monkeypatch, prepare)
    assert live_runtime.prepare_live_review_worker(root, repo, debounce_s=0) == 0

    # The next explicit Snapshot is prepared; the human's Snapshot is untouched.
    assert drafted == [_fingerprint(repo)]

    after = live_runtime.load_live_review_binding(root, repo, server_url=binding.server_url)
    assert after is not None
    assert after.review_id == "review-live"
    assert after.revision_id == "revision-live"
    assert after.revision_number == 4
    assert after.claimed_at == claimed.claimed_at


def test_claim_live_review_requires_the_exact_current_source(tmp_path: Path) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    binding = live_runtime.LiveReviewBinding(
        server_url="http://127.0.0.1:7420",
        repo_id="repo-live",
        review_id="review-live",
        revision_id="revision-live",
        revision_number=2,
        source_fingerprint="source-generation-a",
    )
    live_runtime.write_live_review_binding(root, repo, binding)

    assert (
        live_runtime.claim_live_review_binding(
            root, repo, server_url=binding.server_url, expected_source_fingerprint="source-generation-b"
        )
        is None
    )
    untouched = live_runtime.load_live_review_binding(root, repo, server_url=binding.server_url)
    assert untouched is not None
    assert untouched.claimed_at == 0

    claimed = live_runtime.claim_live_review_binding(
        root, repo, server_url=binding.server_url, expected_source_fingerprint="source-generation-a"
    )
    assert claimed is not None
    assert claimed.claimed_at > 0


def test_unchanged_heartbeat_reuses_binding_without_new_revision(tmp_path: Path, monkeypatch) -> None:
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, source_state

    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
    fingerprint = source_state(repo, resolve_rev_range(repo, working_tree=True)).fingerprint
    live_runtime.write_live_review_binding(
        root,
        repo,
        live_runtime.LiveReviewBinding(
            server_url="http://127.0.0.1:7420",
            repo_id="repo-live",
            review_id="review-live",
            revision_id="revision-live",
            revision_number=7,
            source_fingerprint=fingerprint,
            author_host="claude",
            author_session_id="session-old",
        ),
    )
    live_runtime._write_request(root, repo, host="claude", session_id="session-new")

    def forbidden_prepare(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an unchanged watcher heartbeat must not draft another revision")

    _draft_only(monkeypatch, forbidden_prepare)
    assert live_runtime.prepare_live_review_worker(root, repo, debounce_s=0, watch_idle_s=0) == 0

    binding = live_runtime.load_live_review_binding(root, repo, server_url="http://127.0.0.1:7420")
    assert binding is not None
    assert binding.revision_id == "revision-live"
    assert binding.revision_number == 7
    # A prompt heartbeat proves liveness, not authorship. The exact author of
    # unchanged source must remain the session that produced the revision.
    assert binding.author_session_id == "session-old"


def test_out_of_band_watch_signal_recaptures_one_new_generation(tmp_path: Path, monkeypatch) -> None:
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, source_state

    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
    first_fingerprint = source_state(repo, resolve_rev_range(repo, working_tree=True)).fingerprint
    live_runtime.write_live_review_binding(
        root,
        repo,
        live_runtime.LiveReviewBinding(
            server_url="http://127.0.0.1:7420",
            repo_id="repo-live",
            review_id="review-live",
            revision_id="revision-1",
            revision_number=1,
            source_fingerprint=first_fingerprint,
            author_host="claude",
            author_session_id="session-live",
        ),
    )
    live_runtime._write_request(root, repo, host="claude", session_id="session-live")

    watch_calls = 0

    def wait_for_change(*_args: Any, **_kwargs: Any) -> bool:
        nonlocal watch_calls
        watch_calls += 1
        if watch_calls == 1:
            (repo / "app.py").write_text("def value():\n    return 99\n", encoding="utf-8")
            return True
        return False

    monkeypatch.setattr(live_runtime, "_wait_for_out_of_band_change", wait_for_change)
    drafted: list[str] = []

    def prepare(_config: Any, repo_root: Path, _rng: Any, **_kwargs: Any) -> str:
        drafted.append(_fingerprint(repo_root))
        return drafted[-1]

    _draft_only(monkeypatch, prepare)
    assert live_runtime.prepare_live_review_worker(root, repo, debounce_s=0, watch_idle_s=10) == 0

    assert len(drafted) == 1
    assert drafted[0] != first_fingerprint
    # The published Snapshot stays exactly what the human last created.
    binding = live_runtime.load_live_review_binding(root, repo, server_url="http://127.0.0.1:7420")
    assert binding is not None
    assert binding.revision_id == "revision-1"
    assert binding.source_fingerprint == first_fingerprint


def test_watchdog_heartbeat_request_wakes_existing_worker_without_overwriting_identity(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    first = live_runtime._write_request(root, repo, host="codex", session_id="session-old")

    class _Observer:
        def schedule(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def start(self) -> None:
            live_runtime._write_request(root, repo, host="codex", session_id="session-new")

        def stop(self) -> None:
            return None

        def join(self, **_kwargs: Any) -> None:
            return None

    monkeypatch.setitem(__import__("sys").modules, "watchdog.events", SimpleNamespace(FileSystemEventHandler=object))
    monkeypatch.setitem(__import__("sys").modules, "watchdog.observers", SimpleNamespace(Observer=_Observer))

    result = live_runtime._wait_for_out_of_band_change(
        root,
        repo,
        baseline_fingerprint=live_runtime._source_fingerprint(repo),
        baseline_request_id=first.request_id,
        watch_idle_s=1.0,
        debounce_s=0,
        poll_interval_s=0.01,
    )

    assert result == "request"
    latest = live_runtime._read_request(root, repo)
    assert latest is not None
    assert latest.host == "codex"
    assert latest.session_id == "session-new"


def test_watchdog_path_still_detects_a_semantic_change_when_no_event_arrives(tmp_path: Path, monkeypatch) -> None:
    import sys

    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    baseline = live_runtime._source_fingerprint(repo)

    class _Observer:
        def schedule(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return True

        def stop(self) -> None:
            return None

        def join(self, **_kwargs: Any) -> None:
            return None

    monkeypatch.setitem(sys.modules, "watchdog.events", SimpleNamespace(FileSystemEventHandler=object))
    monkeypatch.setitem(sys.modules, "watchdog.observers", SimpleNamespace(Observer=_Observer))
    # The heartbeat that catches a dropped event is 60s in production.
    monkeypatch.setattr(live_runtime, "_WATCHDOG_RECHECK_S", 0.0)
    timer = threading.Timer(
        0.03,
        lambda: (repo / "app.py").write_text("def value():\n    return 777\n", encoding="utf-8"),
    )
    timer.start()
    try:
        assert (
            live_runtime._wait_with_watchdog(
                root,
                repo,
                baseline_fingerprint=baseline,
                timeout_s=0.2,
                debounce_s=0,
            )
            == "source"
        )
    finally:
        timer.cancel()


def test_dead_watchdog_observer_falls_back_to_semantic_polling(tmp_path: Path, monkeypatch) -> None:
    import sys

    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    baseline = live_runtime._source_fingerprint(repo)

    class _DeadObserver:
        def schedule(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def stop(self) -> None:
            return None

        def join(self, **_kwargs: Any) -> None:
            return None

    monkeypatch.setitem(sys.modules, "watchdog.events", SimpleNamespace(FileSystemEventHandler=object))
    monkeypatch.setitem(sys.modules, "watchdog.observers", SimpleNamespace(Observer=_DeadObserver))
    timer = threading.Timer(
        0.03,
        lambda: (repo / "app.py").write_text("def value():\n    return 888\n", encoding="utf-8"),
    )
    timer.start()
    try:
        assert (
            live_runtime._wait_for_out_of_band_change(
                root,
                repo,
                baseline_fingerprint=baseline,
                watch_idle_s=0.5,
                debounce_s=0,
                poll_interval_s=0.01,
            )
            == "source"
        )
    finally:
        timer.cancel()


def test_poll_fallback_detects_real_out_of_band_edit(tmp_path: Path, monkeypatch) -> None:
    import threading

    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, source_state

    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    baseline = source_state(repo, resolve_rev_range(repo, working_tree=True)).fingerprint
    monkeypatch.setattr(live_runtime, "_wait_with_watchdog", lambda *_args, **_kwargs: None)

    timer = threading.Timer(
        0.03,
        lambda: (repo / "app.py").write_text("def value():\n    return 123\n", encoding="utf-8"),
    )
    timer.start()
    try:
        assert live_runtime._wait_for_out_of_band_change(
            root,
            repo,
            baseline_fingerprint=baseline,
            watch_idle_s=0.5,
            debounce_s=0,
            poll_interval_s=0.01,
        )
    finally:
        timer.cancel()


def test_claim_is_compare_and_swap_and_cannot_roll_binding_backward(tmp_path: Path) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    server = "http://127.0.0.1:7420"
    live_runtime.write_live_review_binding(
        root,
        repo,
        live_runtime.LiveReviewBinding(
            server_url=server,
            repo_id="repo-live",
            review_id="review-live",
            revision_id="revision-1",
            revision_number=1,
            source_fingerprint="source-1",
        ),
    )
    stale = live_runtime.load_live_review_binding(root, repo, server_url=server)
    assert stale is not None
    live_runtime.write_live_review_binding(
        root,
        repo,
        live_runtime.LiveReviewBinding(
            server_url=server,
            repo_id="repo-live",
            review_id="review-live",
            revision_id="revision-2",
            revision_number=2,
            source_fingerprint="source-2",
        ),
    )

    assert (
        live_runtime.claim_live_review_binding(
            root,
            repo,
            server_url=server,
            expected_source_fingerprint=stale.source_fingerprint,
            expected_revision_id=stale.revision_id,
        )
        is None
    )
    current = live_runtime.load_live_review_binding(root, repo, server_url=server)
    assert current is not None
    assert current.revision_id == "revision-2"
    assert current.source_fingerprint == "source-2"
    assert current.claimed_at == 0


def test_claimed_binding_refuses_background_advance_but_allows_explicit_human_advance(tmp_path: Path) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    server = "http://127.0.0.1:7420"
    first = live_runtime.LiveReviewBinding(
        server_url=server,
        repo_id="repo-live",
        review_id="review-live",
        revision_id="revision-1",
        revision_number=1,
        source_fingerprint="source-1",
        author_host="codex",
        author_session_id="session-1",
    )
    live_runtime.write_live_review_binding(root, repo, first)
    claimed = live_runtime.claim_live_review_binding(root, repo, server_url=server, expected_revision_id="revision-1")
    assert claimed is not None and claimed.claimed_at > 0

    second = live_runtime.LiveReviewBinding(
        server_url=server,
        repo_id="repo-live",
        review_id="review-live",
        revision_id="revision-2",
        revision_number=2,
        source_fingerprint="source-2",
        author_host="codex",
        author_session_id="session-1",
    )
    assert live_runtime.write_live_review_binding(root, repo, second) is False
    still_claimed = live_runtime.load_live_review_binding(root, repo, server_url=server)
    assert still_claimed is not None
    assert still_claimed.revision_id == "revision-1"
    assert still_claimed.claimed_at == claimed.claimed_at

    assert live_runtime.write_live_review_binding(root, repo, second, allow_advance_claimed=True) is True
    advanced = live_runtime.load_live_review_binding(root, repo, server_url=server)
    assert advanced is not None
    assert advanced.revision_id == "revision-2"
    assert advanced.claimed_at == 0


def test_hundred_edit_burst_coalesces_to_one_final_semantic_capture(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")

    for generation in range(1, 101):
        (repo / "app.py").write_text(f"def value():\n    return {generation}\n", encoding="utf-8")
        live_runtime._write_request(root, repo, host="codex", session_id=f"session-{generation}")

    calls: list[str] = []

    def prepare(_config: Any, repo_root: Path, _rng: Any, **_kwargs: Any) -> str:
        calls.append((repo / "app.py").read_text(encoding="utf-8"))
        return _fingerprint(repo_root)

    _draft_only(monkeypatch, prepare)
    assert live_runtime.prepare_live_review_worker(root, repo, debounce_s=0, watch_idle_s=0) == 0
    assert calls == ["def value():\n    return 100\n"]


def test_prepare_worker_does_not_drop_generation_after_eight_edit_bursts(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
    live_runtime._write_request(root, repo, host="codex", session_id="session-0")
    drafted: list[str] = []

    def prepare(_config: Any, repo_root: Path, _rng: Any, **_kwargs: Any) -> str:
        drafted.append(_fingerprint(repo_root))
        if len(drafted) < 10:
            (repo / "app.py").write_text(f"def value():\n    return {len(drafted) + 2}\n", encoding="utf-8")
            live_runtime._write_request(root, repo, host="codex", session_id=f"session-{len(drafted)}")
        return drafted[-1]

    _draft_only(monkeypatch, prepare)
    assert live_runtime.prepare_live_review_worker(root, repo, debounce_s=0, watch_idle_s=0) == 0
    assert len(drafted) == 10
    assert drafted[-1] == _fingerprint(repo)


def test_worker_shutdown_handoff_cannot_strand_a_new_prepare_request(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "state"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:7420")
    live_runtime._write_request(root, repo, host="codex", session_id="session-old")
    _draft_only(monkeypatch, lambda _config, repo_root, _rng, **_kwargs: _fingerprint(repo_root))
    release_entered = threading.Event()
    allow_release = threading.Event()
    worker_done = threading.Event()
    writer_done = threading.Event()
    original_release = live_runtime._release_worker_lock

    def delayed_release(handle: Any) -> None:
        release_entered.set()
        assert allow_release.wait(2.0)
        original_release(handle)

    monkeypatch.setattr(live_runtime, "_release_worker_lock", delayed_release)

    def run_worker() -> None:
        assert live_runtime.prepare_live_review_worker(root, repo, debounce_s=0, watch_idle_s=0) == 0
        worker_done.set()

    worker = threading.Thread(target=run_worker)
    worker.start()
    assert release_entered.wait(2.0)

    def publish_next_request() -> None:
        live_runtime._write_request(root, repo, host="codex", session_id="session-new")
        writer_done.set()

    writer = threading.Thread(target=publish_next_request)
    writer.start()
    # The request publication must wait until worker.lock is actually released.
    # Before the handoff lock this completed here, while the replacement worker
    # would still lose worker.lock and exit, stranding request.json.
    assert not writer_done.wait(0.1)

    allow_release.set()
    assert worker_done.wait(2.0)
    assert writer_done.wait(2.0)
    worker.join(timeout=2.0)
    writer.join(timeout=2.0)
    latest = live_runtime._read_request(root, repo)
    assert latest is not None
    assert latest.session_id == "session-new"


def test_source_state_cache_is_reused_only_while_the_tree_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    import os

    from lemoncrow.pro.capabilities.review import gitdiff

    repo = _repo(tmp_path / "repo")
    old = 1_600_000_000
    os.utime(repo / "app.py", (old, old))  # settled: old enough to trust from cache
    rng = gitdiff.resolve_rev_range(repo, working_tree=True)
    calls = 0
    real = gitdiff._source_state_uncached

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(gitdiff, "_source_state_uncached", counting)
    first = gitdiff.source_state(repo, rng)
    assert gitdiff.source_state(repo, rng) == first
    assert calls == 1  # the second call was served from cache

    (repo / "app.py").write_text("def value():\n    return 3\n", encoding="utf-8")
    os.utime(repo / "app.py", (old + 5, old + 5))  # different mtime, still settled
    changed = gitdiff.source_state(repo, rng)
    assert changed.fingerprint != first.fingerprint
    assert calls == 2


def test_prepare_request_skips_temp_checkouts_unless_always(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path / "repo")  # tmp_path lives under the system temp root
    monkeypatch.setattr(live_runtime.subprocess, "Popen", lambda *_args, **_kwargs: None)

    monkeypatch.delenv("LEMONCROW_LIVE_REVIEW_PREWARM")
    assert live_runtime.request_live_review_prepare(tmp_path / "state", repo, host="claude", session_id="s") is None
    assert live_runtime._read_request(tmp_path / "state", repo) is None

    monkeypatch.setenv("LEMONCROW_LIVE_REVIEW_PREWARM", "always")
    assert live_runtime.request_live_review_prepare(tmp_path / "state", repo, host="claude", session_id="s") is not None


def test_sparse_checkout_ignore_rules_read_from_the_index_hide_what_git_hides(tmp_path: Path) -> None:
    import subprocess

    from lemoncrow.pro.capabilities.review import gitdiff

    root = tmp_path / "sparse"
    root.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (root / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / ".gitignore").write_text("build/\n", encoding="utf-8")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    # Sparse: both ignore files are skip-worktree and absent from disk; git still honors them.
    git("config", "core.sparseCheckout", "true")
    git("update-index", "--skip-worktree", ".gitignore", "pkg/.gitignore")
    (root / ".gitignore").unlink()
    (root / "pkg" / ".gitignore").unlink()
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "index.js").write_text("x\n", encoding="utf-8")
    (root / "pkg" / "build").mkdir()
    (root / "pkg" / "build" / "out.js").write_text("x\n", encoding="utf-8")
    (root / "new.py").write_text("y = 2\n", encoding="utf-8")

    git_untracked = {line[3:] for line in git("status", "--porcelain", "-uall").splitlines() if line.startswith("??")}
    status = gitdiff._open_repo(root).status(untracked_files="all", ignored=False)
    ours = {path for path, flags in status.items() if int(flags) & 128}  # WT_NEW
    assert git_untracked == ours == {"new.py"}
