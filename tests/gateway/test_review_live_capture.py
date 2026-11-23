from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest
from lemoncrow_client.config import load_config
from lemoncrow_client.liveview import LiveViewBinding, write_live_view_binding
from lemoncrow_client.session import workspace_state_fingerprint

from lemoncrow.pro.capabilities.review import hosted
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, source_state
from lemoncrow.pro.capabilities.review.live_runtime import (
    LiveReviewBinding,
    LiveReviewClaimed,
    claim_live_review_binding,
    load_live_review_binding,
    write_live_review_binding,
)
from lemoncrow.pro.capabilities.review.models import ImpactSite, ProvenanceRecord
from lemoncrow.pro.capabilities.review.packet import PacketBuild


def _repo(root: Path) -> None:
    root.mkdir(parents=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Reviewer"
    repo.config["user.email"] = "reviewer@example.test"
    (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Reviewer", "reviewer@example.test", 1700000000, 0)
    repo.create_commit("HEAD", sig, sig, "base", tree, [])


def _config(repo: Path, state: Path):
    return load_config(
        {
            "LEMONCROW_URL": "http://127.0.0.1:7420",
            "LEMONCROW_INSTALL_MODE": "local",
            "LEMONCROW_HOME": str(state),
            "HOME": str(state.parent),
        },
        cwd=repo,
    )


class _FakeRemote:
    last: _FakeRemote | None = None

    def __init__(self, config: Any, *, publish_live_view: bool = True) -> None:
        self.config = config
        self.publish_live_view = publish_live_view
        self.adopted: LiveViewBinding | None = None
        self.open_view_calls = 0
        self.capture_repo_ids: list[str] = []
        self.captured_packets: list[dict[str, Any]] = []
        self.enriched_packets: list[dict[str, Any]] = []
        self.preparation_states: list[str] = []
        self.events: list[str] = []
        self.pinned_snapshots: list[dict[str, Any]] = []
        self.closed = False
        _FakeRemote.last = self

    def handshake(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def review_reader_request(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        # No durable branch Review to resume: capture keeps the resolved base.
        return {}

    def open_session(self, **_kwargs: Any) -> dict[str, Any]:
        return {"session_id": "review-http-session"}

    def adopt_live_view(self, binding: LiveViewBinding) -> None:
        self.adopted = binding

    def open_view(self, **_kwargs: Any) -> dict[str, Any]:
        self.open_view_calls += 1
        return {"repo_id": "repo-synced", "view_id": "view-synced", "view_revision": 1}

    def pin_review_snapshot(
        self,
        new_blobs: dict[str, str],
        *,
        binary_new_blobs: dict[str, bytes] | None = None,
        deleted_paths: tuple[str, ...] = (),
        timeout_s: float | None = None,
    ) -> int:
        self.pinned_snapshots.append(
            {
                "new_blobs": dict(new_blobs),
                "binary_new_blobs": dict(binary_new_blobs or {}),
                "deleted_paths": tuple(deleted_paths),
                "timeout_s": timeout_s,
            }
        )
        self.events.append("pin")
        return 2

    def capture_review(self, *, repo_id: str, **kwargs: Any) -> dict[str, Any]:
        self.capture_repo_ids.append(repo_id)
        self.events.append("capture")
        packet = kwargs.get("packet")
        if isinstance(packet, dict):
            self.captured_packets.append(packet)
        return {
            "review": {
                "id": "review-live",
                "ref": "r/review-live",
                "review_path": "/r/review-live",
            },
            "revision": {"id": "revision-live", "revision_number": 1},
        }

    def open_review_snapshot(self, _root: Path, **_kwargs: Any) -> dict[str, Any]:
        return {"view_id": "base-view", "view_revision": 1, "repo_id": "repo-synced"}

    def upload_large_review_source(self, **_kwargs: Any) -> tuple[int, int]:
        return 0, 0

    def update_review_preparation(self, *, state: str, **_kwargs: Any) -> dict[str, Any]:
        self.preparation_states.append(state)
        self.events.append(f"preparation:{state}")
        return {"state": state}

    def enrich_review_projection(self, *, packet: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.enriched_packets.append(packet)
        self.events.append("projection")
        return {"enriched": True}

    def attach_review_base(self, **_kwargs: Any) -> dict[str, Any]:
        return {"attached": True}

    def review_url(self, review_id: str) -> str:
        return f"http://127.0.0.1:7420/{review_id}"

    def close(self) -> None:
        self.closed = True


def test_working_tree_capture_reuses_matching_live_authoring_view(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    config = _config(repo, state)
    binding = LiveViewBinding(
        server_url=config.url,
        repo_id="repo-live",
        view_id="view-live",
        view_revision=9,
        workspace_fingerprint=workspace_state_fingerprint(repo),
    )
    write_live_view_binding(state, repo, binding)
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)

    result = hosted.capture_server_review(
        config,
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=state,
        session_id=None,
        limit=20,
        with_impact=False,
        with_provenance=False,
    )

    remote = _FakeRemote.last
    assert remote is not None
    assert remote.publish_live_view is False
    assert remote.adopted == binding
    assert remote.open_view_calls == 0
    assert remote.capture_repo_ids == ["repo-live"]
    assert remote.pinned_snapshots == []
    assert result.review_id == "review-live"
    prepared = load_live_review_binding(state, repo, server_url=config.url)
    assert prepared is not None
    assert prepared.review_id == "review-live"
    assert prepared.revision_id == "revision-live"
    assert prepared.source_fingerprint == source_state(repo, resolve_rev_range(repo, working_tree=True)).fingerprint
    assert remote.closed is True


def test_working_tree_capture_resyncs_if_checkout_moves_during_reused_view_build(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    config = _config(repo, state)
    write_live_view_binding(
        state,
        repo,
        LiveViewBinding(
            server_url=config.url,
            repo_id="repo-live",
            view_id="view-live",
            view_revision=9,
            workspace_fingerprint=workspace_state_fingerprint(repo),
        ),
    )
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    original = hosted.build_review_packet_with_blobs

    def moving_build(*args: Any, **kwargs: Any):
        build = original(*args, **kwargs)
        (repo / "app.py").write_text("def value():\n    return 99\n", encoding="utf-8")
        return build

    monkeypatch.setattr(hosted, "build_review_packet_with_blobs", moving_build)
    hosted.capture_server_review(
        config,
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=state,
        session_id=None,
        limit=20,
        with_impact=False,
        with_provenance=False,
    )

    remote = _FakeRemote.last
    assert remote is not None
    assert remote.adopted is None
    assert remote.open_view_calls == 1
    assert remote.capture_repo_ids == ["repo-synced"]
    assert remote.pinned_snapshots[0]["new_blobs"]["app.py"] == "def value():\n    return 2\n"
    assert remote.events.index("pin") < remote.events.index("capture")


def test_working_tree_capture_refuses_stale_live_view_fingerprint(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    config = _config(repo, state)
    write_live_view_binding(
        state,
        repo,
        LiveViewBinding(
            server_url=config.url,
            repo_id="repo-live",
            view_id="view-live",
            view_revision=9,
            workspace_fingerprint="stale-fingerprint",
        ),
    )
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)

    hosted.capture_server_review(
        config,
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=state,
        session_id=None,
        limit=20,
        with_impact=False,
        with_provenance=False,
    )

    remote = _FakeRemote.last
    assert remote is not None
    assert remote.adopted is None
    assert remote.open_view_calls == 1
    assert remote.capture_repo_ids == ["repo-synced"]
    assert len(remote.pinned_snapshots) == 1


def test_working_tree_capture_publishes_captured_snapshot_even_if_checkout_moves_after_build(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    config = _config(repo, state)
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)

    real_build = hosted.build_review_packet_with_blobs

    def moving_build(*args: Any, **kwargs: Any):
        build = real_build(*args, **kwargs)
        (repo / "app.py").write_text("def value():\n    return 99\n", encoding="utf-8")
        return build

    monkeypatch.setattr(hosted, "build_review_packet_with_blobs", moving_build)

    result = hosted.capture_server_review(
        config,
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=state,
        session_id=None,
        limit=20,
        with_impact=False,
        with_provenance=False,
    )

    remote = _FakeRemote.last
    assert remote is not None
    assert result.review_id == "review-live"
    assert remote.pinned_snapshots[0]["new_blobs"]["app.py"] == "def value():\n    return 2\n"
    assert (repo / "app.py").read_text(encoding="utf-8") == "def value():\n    return 99\n"
    assert remote.events.index("pin") < remote.events.index("capture")


def test_background_capture_refuses_server_publication_after_human_claim(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    config = _config(repo, state)
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    fingerprint = source_state(repo, resolve_rev_range(repo, working_tree=True)).fingerprint
    write_live_review_binding(
        state,
        repo,
        LiveReviewBinding(
            server_url=config.url,
            repo_id="repo-live",
            review_id="review-live",
            revision_id="revision-old",
            revision_number=1,
            source_fingerprint=fingerprint,
        ),
    )
    assert (
        claim_live_review_binding(state, repo, server_url=config.url, expected_revision_id="revision-old") is not None
    )

    with pytest.raises(LiveReviewClaimed):
        hosted.capture_server_review(
            config,
            repo,
            resolve_rev_range(repo, working_tree=True),
            store_root=state,
            session_id="session-author",
            limit=20,
            with_impact=False,
            with_provenance=False,
            respect_human_claim=True,
        )
    remote = _FakeRemote.last
    assert remote is not None
    assert remote.captured_packets == []
    binding = load_live_review_binding(state, repo, server_url=config.url)
    assert binding is not None
    assert binding.revision_id == "revision-old"


def test_human_claim_waits_for_inflight_background_publication_and_claims_its_revision(
    tmp_path: Path, monkeypatch
) -> None:
    import threading
    import time

    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    config = _config(repo, state)
    fingerprint = source_state(repo, resolve_rev_range(repo, working_tree=True)).fingerprint
    write_live_review_binding(
        state,
        repo,
        LiveReviewBinding(
            server_url=config.url,
            repo_id="repo-live",
            review_id="review-live",
            revision_id="revision-old",
            revision_number=0,
            source_fingerprint=fingerprint,
        ),
    )

    entered = threading.Event()
    release = threading.Event()

    class BlockingRemote(_FakeRemote):
        def capture_review(self, *, repo_id: str, **kwargs: Any) -> dict[str, Any]:
            entered.set()
            assert release.wait(timeout=10)
            return super().capture_review(repo_id=repo_id, **kwargs)

    monkeypatch.setattr(hosted, "RemoteSession", BlockingRemote)
    publish_error: list[BaseException] = []

    def publish() -> None:
        try:
            hosted.capture_server_review(
                config,
                repo,
                resolve_rev_range(repo, working_tree=True),
                store_root=state,
                session_id="session-author",
                limit=20,
                with_impact=False,
                with_provenance=False,
                respect_human_claim=True,
            )
        except BaseException as exc:
            publish_error.append(exc)

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert entered.wait(timeout=10)

    claimed: list[LiveReviewBinding | None] = []
    claimer = threading.Thread(
        target=lambda: claimed.append(claim_live_review_binding(state, repo, server_url=config.url))
    )
    claimer.start()
    time.sleep(0.05)
    assert claimer.is_alive(), "claim must serialize behind the in-flight server publication"
    release.set()
    publisher.join(timeout=10)
    claimer.join(timeout=10)
    assert publish_error == []
    assert claimed and claimed[0] is not None
    assert claimed[0].revision_id == "revision-live"
    assert claimed[0].claimed_at > 0


def test_working_tree_capture_pins_packet_when_checkout_moves_after_build(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    config = _config(repo, state)
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    original = hosted.build_review_packet_with_blobs

    def racing_build(*args: Any, **kwargs: Any):
        build = original(*args, **kwargs)
        (repo / "app.py").write_text("def value():\n    return 99\n", encoding="utf-8")
        return build

    monkeypatch.setattr(hosted, "build_review_packet_with_blobs", racing_build)
    captured = hosted.capture_server_review(
        config,
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=state,
        session_id=None,
        limit=20,
        with_impact=False,
        with_provenance=False,
    )

    remote = _FakeRemote.last
    assert remote is not None
    assert captured.review_id == "review-live"
    assert remote.pinned_snapshots[0]["new_blobs"]["app.py"] == "def value():\n    return 2\n"
    assert remote.events.index("pin") < remote.events.index("capture")


def test_progressive_working_tree_opens_source_before_impact_and_enriches_same_revision(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    config = _config(repo, state)
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)

    def impact(_repo: Path, _rng: Any, build: PacketBuild, **_kwargs: Any) -> PacketBuild:
        assert _FakeRemote.last is not None
        _FakeRemote.last.events.append("impact")
        return PacketBuild(
            packet=replace(
                build.packet,
                impact=(
                    ImpactSite(
                        kind="untouched_caller",
                        path="caller.py:L10",
                        old="value()",
                        new=None,
                        snippet="value()",
                        source_path="app.py",
                    ),
                ),
            ),
            blobs=build.blobs,
        )

    def provenance(_store: Path, _repo: Path, _rng: Any, build: PacketBuild, **_kwargs: Any) -> PacketBuild:
        assert _FakeRemote.last is not None
        _FakeRemote.last.events.append("provenance")
        return PacketBuild(
            packet=replace(
                build.packet,
                provenance=ProvenanceRecord(
                    status="matched",
                    host="claude",
                    session_id="session-1",
                    certainty="exact",
                ),
            ),
            blobs=build.blobs,
        )

    monkeypatch.setattr(hosted, "attach_impact", impact)
    monkeypatch.setattr(hosted, "attach_provenance", provenance)
    ready_rows: list[tuple[int, str, str]] = []

    result = hosted.capture_server_review(
        config,
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=state,
        session_id="session-1",
        limit=20,
        with_impact=True,
        with_provenance=True,
        ready=lambda capture: (
            _FakeRemote.last.events.append("ready") if _FakeRemote.last is not None else None,
            ready_rows.append(
                (len(capture.build.packet.impact), capture.review_url, capture.response["revision"]["id"])
            ),
        ),
    )

    remote = _FakeRemote.last
    assert remote is not None
    assert ready_rows == [(0, ready_rows[0][1], "revision-live")]
    assert "#p=prep-" in ready_rows[0][1]
    assert "&sr=1" in ready_rows[0][1]
    assert result.response["revision"]["id"] == "revision-live"
    assert len(result.build.packet.impact) == 1
    assert result.build.packet.provenance.session_id == "session-1"
    assert len(remote.captured_packets) == 1
    assert not remote.captured_packets[0]["impact"]
    assert len(remote.enriched_packets) == 1
    assert len(remote.enriched_packets[0]["impact"]) == 1
    assert remote.preparation_states == ["impact_background", "finalizing_background", "ready"]
    assert remote.events == [
        "pin",
        "capture",
        "preparation:impact_background",
        "ready",
        "impact",
        "provenance",
        "preparation:finalizing_background",
        "projection",
        "preparation:ready",
    ]


def test_progressive_enrichment_stops_when_a_newer_live_generation_supersedes_it(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    config = _config(repo, state)
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)

    impact_calls = 0

    def impact(_repo: Path, _rng: Any, build: PacketBuild, **_kwargs: Any) -> PacketBuild:
        nonlocal impact_calls
        impact_calls += 1
        assert _FakeRemote.last is not None
        _FakeRemote.last.events.append("impact")
        return build

    def forbidden_provenance(*_args: Any, **_kwargs: Any) -> PacketBuild:
        raise AssertionError("a superseded generation must not start provenance enrichment")

    checks = 0

    def superseded() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    monkeypatch.setattr(hosted, "attach_impact", impact)
    monkeypatch.setattr(hosted, "attach_provenance", forbidden_provenance)

    result = hosted.capture_server_review(
        config,
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=state,
        session_id="session-1",
        limit=20,
        with_impact=True,
        with_provenance=True,
        superseded=superseded,
    )

    remote = _FakeRemote.last
    assert remote is not None
    assert result.response["revision"]["id"] == "revision-live"
    assert impact_calls == 1
    assert remote.enriched_packets == []
    assert remote.preparation_states == ["impact_background", "ready"]
    assert remote.events == [
        "pin",
        "capture",
        "preparation:impact_background",
        "impact",
        "preparation:ready",
    ]


def test_prepared_source_reuses_diff_symbols_and_binary_bytes(tmp_path: Path, monkeypatch) -> None:
    repo, state = tmp_path / "repo", tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n")
    (repo / "image.png").write_bytes(b"\x89PNG\x00\xff")
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    config = _config(repo, state)
    rng = resolve_rev_range(repo, working_tree=True)
    options = dict(store_root=state, session_id=None, limit=20, with_impact=False, with_provenance=False)
    first = hosted.capture_server_review(config, repo, rng, **options)

    def forbidden(*args, **kwargs):
        pytest.fail("prepared source must not rebuild the diff, symbols, or base tree")

    monkeypatch.setattr(hosted, "build_review_packet_with_blobs", forbidden)
    monkeypatch.setattr(hosted, "attach_changed_symbols", forbidden)
    monkeypatch.setattr(hosted, "_materialize_capture_old_side", forbidden)
    second = hosted.capture_server_review(config, repo, rng, **options)
    assert second.build == first.build
    assert second.build.blobs.binary_new["image.png"] == b"\x89PNG\x00\xff"
    assert _FakeRemote.last.pinned_snapshots[0]["binary_new_blobs"] == second.build.blobs.binary_new


def test_prepared_source_invalidates_on_edit_and_refreshes_evidence(tmp_path: Path, monkeypatch) -> None:
    repo, state = tmp_path / "repo", tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n")
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    config = _config(repo, state)
    options = dict(store_root=state, session_id=None, limit=20, with_impact=False, with_provenance=False)
    hosted.capture_server_review(config, repo, resolve_rev_range(repo, working_tree=True), **options)
    builder = hosted.build_review_packet_with_blobs
    builds = []
    evidence = []

    def counted(*args, **kwargs):
        builds.append(True)
        return builder(*args, **kwargs)

    def provenance(_store, _repo, _rng, build, **kwargs):
        evidence.append(kwargs["session_id"])
        return build

    monkeypatch.setattr(hosted, "build_review_packet_with_blobs", counted)
    monkeypatch.setattr(hosted, "attach_provenance", provenance)
    hosted.capture_server_review(
        config,
        repo,
        resolve_rev_range(repo, working_tree=True),
        **{**options, "with_provenance": True, "session_id": "new-evidence"},
    )
    assert builds == []
    assert evidence == ["new-evidence"]
    (repo / "app.py").write_text("def value():\n    return 3\n")
    changed = hosted.capture_server_review(config, repo, resolve_rev_range(repo, working_tree=True), **options)
    assert builds == [True]
    assert changed.build.blobs.new["app.py"].endswith("return 3\n")


@pytest.mark.parametrize("invalid", ["corrupt", "expired", "oversize"])
def test_prepared_source_cache_failure_preserves_capture(tmp_path: Path, monkeypatch, invalid: str) -> None:
    from lemoncrow.pro.capabilities.review import precompute

    repo, state = tmp_path / "repo", tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n")
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    config = _config(repo, state)
    options = dict(store_root=state, session_id=None, limit=20, with_impact=False, with_provenance=False)
    rng = resolve_rev_range(repo, working_tree=True)
    first = hosted.capture_server_review(config, repo, rng, **options)
    path = precompute._root(state, repo) / "source.json.gz"
    if invalid == "corrupt":
        path.write_bytes(b"not a gzip stream")
    elif invalid == "expired":
        monkeypatch.setattr(precompute, "_MAX_AGE_S", -1)
    else:
        monkeypatch.setattr(precompute, "_MAX_PACKET_BYTES", 1)
    rebuilt = []
    builder = hosted.build_review_packet_with_blobs

    def counted(*args, **kwargs):
        rebuilt.append(True)
        return builder(*args, **kwargs)

    monkeypatch.setattr(hosted, "build_review_packet_with_blobs", counted)
    result = hosted.capture_server_review(config, repo, rng, **options)
    assert rebuilt == [True]
    assert result.build.blobs == first.build.blobs
    assert result.build.packet.files == first.build.packet.files


def test_base_snapshot_replaces_old_commit_and_retries_failed_materialization(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.precompute import base_snapshot

    repo, state = tmp_path / "repo", tmp_path / "state"
    _repo(repo)
    rng = resolve_rev_range(repo, working_tree=True)
    calls = []

    def materialize(_repo, revision, target):
        calls.append(revision.base_sha)
        target.mkdir(parents=True)
        (target / "source").write_text(revision.base_sha)

    with base_snapshot(state, repo, rng, materialize) as first:
        assert (first / "source").read_text() == rng.base_sha
    with base_snapshot(state, repo, rng, materialize):
        pass
    assert calls == [rng.base_sha]
    moved = replace(rng, base_sha="new-base")

    def failed(_repo, revision, target):
        target.mkdir(parents=True)
        (target / "partial").write_text("incomplete")
        raise ValueError("interrupted")

    with pytest.raises(ValueError, match="interrupted"), base_snapshot(state, repo, moved, failed):
        pass
    with base_snapshot(state, repo, moved, materialize) as current:
        assert (current / "source").read_text() == "new-base"
        assert not (current / "partial").exists()
    assert calls == [rng.base_sha, "new-base"]


def test_definition_analysis_cache_is_content_sensitive_and_bounded(monkeypatch) -> None:
    from lemoncrow.pro.capabilities.review import impact

    monkeypatch.setattr(impact, "_definition_cache", type(impact._definition_cache)())
    monkeypatch.setattr(impact, "_DEFINITION_CACHE_MAX", 64)
    parser = impact._uncached_definition_analysis
    calls = []

    def counted(text, path):
        calls.append((text, path))
        return parser(text, path)

    monkeypatch.setattr(impact, "_uncached_definition_analysis", counted)
    text = "def value():\n    return 1\n"
    first = impact._definition_analysis(text, "a.py")
    assert impact._definition_analysis(text, "a.py") == first
    impact._definition_analysis(text.replace("1", "2"), "a.py")
    impact._definition_analysis(text, "b.py")
    assert len(calls) == 3
    for i in range(70):
        impact._definition_analysis(f"value = {i}\n", "a.py")
    assert len(impact._definition_cache) == 64
    # Large files are the expensive parses and are cached too: the key is a
    # digest, so the cache never retains the text itself.
    large = "#" + "x" * (64 * 1024)
    before = len(calls)
    impact._definition_analysis(large, "a.py")
    impact._definition_analysis(large, "a.py")
    assert len(calls) == before + 1
    assert all(isinstance(key[1], bytes) and len(key[1]) == 20 for key in impact._definition_cache)


def test_prepared_source_rechecks_source_after_loading_cache(tmp_path: Path, monkeypatch) -> None:
    repo, state = tmp_path / "repo", tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n")
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    config = _config(repo, state)
    rng = resolve_rev_range(repo, working_tree=True)
    options = dict(store_root=state, session_id=None, limit=20, with_impact=False, with_provenance=False)
    hosted.capture_server_review(config, repo, rng, **options)
    loader = hosted.load_source_build

    def moving_source(*args):
        build = loader(*args)
        assert build is not None
        (repo / "app.py").write_text("def value():\n    return 3\n")
        return build

    monkeypatch.setattr(hosted, "load_source_build", moving_source)
    result = hosted.capture_server_review(config, repo, rng, **options)
    assert result.build.blobs.new["app.py"].endswith("return 3\n")
    assert result.build.source_fingerprint == source_state(repo, rng).fingerprint


def test_source_cache_invalidates_when_base_moves_with_same_worktree_bytes(tmp_path: Path, monkeypatch) -> None:
    repo, state = tmp_path / "repo", tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n")
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    config = _config(repo, state)
    options = dict(store_root=state, session_id=None, limit=20, with_impact=False, with_provenance=False)
    first = hosted.capture_server_review(config, repo, resolve_rev_range(repo, working_tree=True), **options)

    (repo / "app.py").write_text("def value():\n    return 0\n")
    git = pygit2.Repository(str(repo))
    git.index.add_all()
    git.index.write()
    sig = pygit2.Signature("Reviewer", "reviewer@example.test", 1700000001, 0)
    git.create_commit("HEAD", sig, sig, "new base", git.index.write_tree(), [git.head.target])
    (repo / "app.py").write_text("def value():\n    return 2\n")
    second = hosted.capture_server_review(config, repo, resolve_rev_range(repo, working_tree=True), **options)
    assert second.build.blobs.new == first.build.blobs.new
    assert second.build.blobs.old["app.py"].endswith("return 0\n")
    assert second.build.source_fingerprint != first.build.source_fingerprint


def test_source_cache_size_boundary_and_workspace_isolation(tmp_path: Path, monkeypatch) -> None:
    import gzip

    from lemoncrow.pro.capabilities.review import precompute

    repo, state = tmp_path / "repo", tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n")
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    rng = resolve_rev_range(repo, working_tree=True)
    captured = hosted.capture_server_review(
        _config(repo, state),
        repo,
        rng,
        store_root=state,
        session_id=None,
        limit=20,
        with_impact=False,
        with_provenance=False,
    )
    path = precompute._root(state, repo) / "source.json.gz"
    size = len(gzip.decompress(path.read_bytes()))
    fingerprint = captured.build.source_fingerprint
    monkeypatch.setattr(precompute, "_MAX_PACKET_BYTES", size)
    assert precompute.load_source_build(state, repo, rng, fingerprint) == captured.build
    assert precompute.load_source_build(state, tmp_path / "other-repo", rng, fingerprint) is None
    monkeypatch.setattr(precompute, "_MAX_PACKET_BYTES", size - 1)
    assert precompute.load_source_build(state, repo, rng, fingerprint) is None
    precompute.save_source_build(state, repo, rng, captured.build)
    assert not path.exists()
    assert captured.build.packet.files


def test_base_snapshot_replacement_waits_for_active_reader(tmp_path: Path) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from lemoncrow.pro.capabilities.review.precompute import base_snapshot

    repo, state = tmp_path / "repo", tmp_path / "state"
    _repo(repo)
    rng = resolve_rev_range(repo, working_tree=True)
    waiting = threading.Event()
    entered = threading.Event()

    def materialize(_repo, revision, target):
        target.mkdir(parents=True)
        (target / "source").write_text(revision.base_sha)

    def replace_base():
        waiting.set()
        with base_snapshot(state, repo, replace(rng, base_sha="new-base"), materialize) as tree:
            entered.set()
            return (tree / "source").read_text()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with base_snapshot(state, repo, rng, materialize) as tree:
            future = pool.submit(replace_base)
            assert waiting.wait(timeout=5)
            assert not entered.wait(timeout=0.05)
            assert (tree / "source").read_text() == rng.base_sha
        assert future.result(timeout=5) == "new-base"


def test_captures_sweep_slots_no_capture_has_written_for_a_day(tmp_path: Path, monkeypatch) -> None:
    import os
    import threading
    import time

    from lemoncrow.pro.capabilities.review import precompute
    from lemoncrow.pro.capabilities.review.live_runtime import _cross_process_lock

    repo, state = tmp_path / "repo", tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n")
    rng = resolve_rev_range(repo, working_tree=True)

    def materialize(_repo, revision, target):
        target.mkdir(parents=True)
        (target / "source").write_text(revision.base_sha)

    slots = []
    for name in ("deleted-worktree", "busy-worktree", "recent-worktree"):
        with precompute.base_snapshot(state, tmp_path / name, rng, materialize):
            pass
        slots.append(precompute._root(state, tmp_path / name))
    deleted, busy, recent = slots
    for slot, ago_s in ((deleted, 60), (busy, 60), (recent, -60)):
        written = time.time() - precompute._MAX_AGE_S - ago_s
        for path in (slot / "base" / "identity.json", slot / "base", slot):
            os.utime(path, (written, written))
    stray = precompute._slots(state) / "notes.txt"
    stray.write_text("not a slot")

    # A new base tree sweeps once its reader is done; a slot another process
    # (its own thread lock) holds is skipped.
    with _cross_process_lock(busy / "base.lock", threading.RLock()):
        with precompute.base_snapshot(state, repo, rng, hosted._materialize_capture_old_side) as tree:
            assert (tree / "app.py").read_text() == "def value():\n    return 1\n"
            assert deleted.is_dir()
        assert not deleted.exists()
        assert busy.is_dir() and recent.is_dir() and stray.exists()

    # A saved source packet sweeps too, once the slot is free.
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    options = dict(store_root=state, session_id=None, limit=20, with_impact=False, with_provenance=False)
    hosted.capture_server_review(_config(repo, state), repo, rng, **options)
    assert (precompute._root(state, repo) / "source.json.gz").exists()
    assert not busy.exists()
    assert recent.is_dir() and stray.exists()


def test_a_waiter_that_wins_a_deleted_lock_file_locks_the_current_one(tmp_path: Path, monkeypatch) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from lemoncrow.pro.capabilities.review.live_runtime import _cross_process_lock

    # Separate thread locks stand in for separate processes: only the file is shared.
    path = tmp_path / "slot" / "base.lock"
    opened, won, release = threading.Event(), threading.Event(), threading.Event()
    holder = threading.current_thread()
    real_open = Path.open

    def tracking_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        if self == path and threading.current_thread() is not holder:
            opened.set()
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)

    def waiter() -> None:
        with _cross_process_lock(path, threading.RLock()):
            won.set()
            assert release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with _cross_process_lock(path, threading.RLock()):
            future = pool.submit(waiter)
            assert opened.wait(timeout=5)
            # The holder reclaims the slot, lock file and all, as the sweep does.
            path.unlink()
            path.parent.rmdir()
        assert won.wait(timeout=5)
        # The waiter holds the path's current file, so the next holder is refused.
        with pytest.raises(BlockingIOError), _cross_process_lock(path, threading.RLock(), blocking=False):
            pass
        release.set()
        future.result(timeout=5)


def test_cached_base_inside_repository_remains_uploadable(tmp_path: Path) -> None:
    from lemoncrow_client.session import walk_worktree

    from lemoncrow.pro.capabilities.review.precompute import base_snapshot

    repo = tmp_path / "repo"
    _repo(repo)
    state = repo / ".lemoncrow"
    rng = resolve_rev_range(repo, working_tree=True)
    with base_snapshot(state, repo, rng, hosted._materialize_capture_old_side) as tree:
        walk = walk_worktree(tree, size_cap=1024 * 1024)
        assert not walk.truncated
        assert not walk.unreadable
        assert [entry.path for entry in walk.entries()] == ["app.py"]
        assert (tree / "app.py").read_text() == "def value():\n    return 1\n"


def test_unwritable_cache_falls_back_to_normal_capture(tmp_path: Path, monkeypatch) -> None:
    from lemoncrow.pro.capabilities.review import precompute

    repo, state = tmp_path / "repo", tmp_path / "state"
    _repo(repo)
    (repo / "app.py").write_text("def value():\n    return 2\n")
    root = precompute._root(state, repo)
    root.parent.mkdir(parents=True)
    root.write_text("cannot create a cache directory here")
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    result = hosted.capture_server_review(
        _config(repo, state),
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=state,
        session_id=None,
        limit=20,
        with_impact=False,
        with_provenance=False,
    )
    assert result.build.blobs.new["app.py"].endswith("return 2\n")
    assert result.review_id == "review-live"


def test_resumed_branch_review_uses_frozen_review_base_before_packet_build(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    state = tmp_path / "state"
    _repo(repo_root)
    repo = pygit2.Repository(str(repo_root))
    frozen_base = str(repo.head.target)

    (repo_root / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Reviewer", "fixture@example.test", 1700000010, 0)
    repo.create_commit("HEAD", sig, sig, "checkpoint", tree, [repo.head.target])
    moved_head = str(repo.head.target)
    assert moved_head != frozen_base
    (repo_root / "app.py").write_text("def value():\n    return 3\n", encoding="utf-8")

    config = _config(repo_root, state)
    current = resolve_rev_range(repo_root, working_tree=True)
    write_live_review_binding(
        state,
        repo_root,
        LiveReviewBinding(
            server_url=config.url,
            repo_id="repo-live",
            review_id="review-live",
            revision_id="revision-old",
            revision_number=1,
            source_fingerprint=source_state(repo_root, current).fingerprint,
            branch_ref="refs/heads/main",
            worktree_key=str(repo_root.resolve()),
        ),
    )
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)
    monkeypatch.setattr(
        hosted,
        "server_review_request",
        lambda *_args, **_kwargs: {
            "session": {"status": "open", "range_mode": "working_tree", "review_base_sha": frozen_base}
        },
    )

    hosted.capture_server_review(
        config,
        repo_root,
        current,
        store_root=state,
        session_id=None,
        limit=20,
        with_impact=False,
        with_provenance=False,
    )

    remote = _FakeRemote.last
    assert remote is not None
    assert remote.captured_packets
    assert remote.captured_packets[0]["base_sha"] == frozen_base
    prepared = load_live_review_binding(state, repo_root, server_url=config.url)
    assert prepared is not None
    assert prepared.branch_ref == "refs/heads/main"


def test_a_capture_without_the_impact_pass_still_names_its_changed_symbols(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    state = tmp_path / "state"
    _repo(repo_root)
    repo = pygit2.Repository(str(repo_root))
    (repo_root / "app.py").write_text("def value():\n    return 2\n\n\ndef other():\n    return 0\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    sig = pygit2.Signature("Reviewer", "reviewer@example.test", 1700000010, 0)
    repo.create_commit("HEAD", sig, sig, "change", repo.index.write_tree(), [repo.head.target])
    monkeypatch.setattr(hosted, "RemoteSession", _FakeRemote)

    # A commit range takes the one-pass build, which names the changed symbols even without the impact pass.
    hosted.capture_server_review(
        _config(repo_root, state),
        repo_root,
        resolve_rev_range(repo_root, "HEAD~1..HEAD"),
        store_root=state,
        session_id=None,
        limit=20,
        with_impact=False,
        with_provenance=False,
    )

    remote = _FakeRemote.last
    assert remote is not None and len(remote.captured_packets) == 1
    packet = remote.captured_packets[0]
    assert sorted(symbol["symbol_name"] for symbol in packet["symbols"]) == ["other", "value"]
    assert {symbol["file_path"] for symbol in packet["symbols"]} == {"app.py"}
    assert not packet["impact"] and "impact_disabled" in packet["degraded"]
