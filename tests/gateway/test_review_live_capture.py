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
    ) -> int:
        self.pinned_snapshots.append(
            {
                "new_blobs": dict(new_blobs),
                "binary_new_blobs": dict(binary_new_blobs or {}),
                "deleted_paths": tuple(deleted_paths),
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
