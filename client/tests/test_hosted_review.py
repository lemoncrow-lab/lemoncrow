"""Hosted Review requests stay inside the short-lived thin client."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from lemoncrow_client.session import RemoteSession
from lemoncrow_client.transport import Response


class _RecordingTransport:
    def __init__(self) -> None:
        self.posts: list[tuple[str, Mapping[str, Any] | None, Mapping[str, str] | None]] = []
        self.gets: list[tuple[str, Mapping[str, str] | None]] = []

    def post(
        self,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> Response:
        self.posts.append((path, body, headers))
        return Response(
            status=201,
            payload={
                "review": {"id": "01abc23401ab723481abc23401abc234", "ref": "r/01abc23401ab723481abc23401abc234"},
                "revision": {"revision_number": 1},
                "revision_created": True,
            },
        )

    def get(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> Response:
        self.gets.append((path, headers))
        return Response(status=200, payload={"reviews": []})

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> Response:
        if method.upper() == "GET":
            return self.get(path, headers=headers, timeout_s=timeout_s)
        if method.upper() == "POST":
            return self.post(path, body=body, headers=headers, timeout_s=timeout_s)
        raise AssertionError(f"unexpected method {method}")


def test_capture_review_posts_the_diff_to_the_bound_hosted_view(config: Any) -> None:
    transport = _RecordingTransport()
    session = RemoteSession(config, transport=transport)  # type: ignore[arg-type]
    session.state.session_id = "session-1"
    session.state.view_id = "view-1"
    session.state.view_revision = 7

    response = session.capture_review(
        repo_id="repo-1",
        source_ref="client:working_tree",
        title="Working tree",
        packet={"schema_version": 1, "files": []},
        new_blobs={"pkg/a.py": "print('new')\n"},
        workspace_branch="main",
    )

    assert response["review"]["id"] == "01abc23401ab723481abc23401abc234"
    assert len(transport.posts) == 1
    path, body, headers = transport.posts[0]
    assert path == "/v1/repos/repo-1/reviews"
    assert body == {
        "view_id": "view-1",
        "view_revision": 7,
        "source_ref": "client:working_tree",
        "title": "Working tree",
        "restore_archived": False,
        "packet": {"schema_version": 1, "files": []},
        "new_blobs": {"pkg/a.py": "print('new')\n"},
        "base_view_id": "",
        "base_view_revision": 0,
        "workspace_branch": "main",
    }
    assert headers is not None
    assert headers["Authorization"].startswith("Bearer ")
    assert session.review_url("01abc23401ab723481abc23401abc234").endswith("/r/01abc23401ab723481abc23401abc234")
    assert session.review_url("r/01abc23401ab723481abc23401abc234").endswith("/r/01abc23401ab723481abc23401abc234")


def test_thin_capture_posts_only_view_and_range_identity(config: Any) -> None:
    transport = _RecordingTransport()
    session = RemoteSession(config, transport=transport)  # type: ignore[arg-type]
    session.state.session_id = "session-1"
    session.state.view_id = "view-head"
    session.state.repo_id = "repo-1"
    session.state.view_revision = 9

    response = session.capture_review_from_views(
        repo_id="repo-1",
        source_ref="thin:working_tree",
        title="Thin review",
        base_view_id="view-base",
        base_view_revision=4,
        with_impact=False,
        range_spec={
            "mode": "working_tree",
            "base_rev": "HEAD",
            "head_rev": "WORKDIR",
            "base_sha": "a" * 40,
            "head_sha": "",
            "merge_base_sha": "",
            "dirty": True,
            "title": "Thin review",
        },
    )

    assert response["review"]["id"] == "01abc23401ab723481abc23401abc234"
    path, body, headers = transport.posts[-1]
    assert path == "/v1/repos/repo-1/reviews"
    assert body is not None
    assert body["derive_packet"] is True
    assert body["view_id"] == "view-head"
    assert body["base_view_id"] == "view-base"
    assert body["range"]["base_sha"] == "a" * 40
    assert body["gitlinks"] == {}
    assert "packet" not in body
    assert "new_blobs" not in body
    assert headers is not None and headers["Authorization"].startswith("Bearer ")


def test_review_inbox_uses_bearer_auth_without_opening_a_listener(config: Any) -> None:
    transport = _RecordingTransport()
    session = RemoteSession(config, transport=transport)  # type: ignore[arg-type]

    assert session.review_inbox() == {"reviews": []}
    assert transport.gets == [("/v1/reviews", {"Authorization": f"Bearer {config.token}"})]


def test_local_review_browser_pairing_uses_machine_bearer(config: Any) -> None:
    local = replace(config, install_mode="local")
    transport = _RecordingTransport()
    session = RemoteSession(local, transport=transport)  # type: ignore[arg-type]

    session.pair_local_review_browser("/reviews/r-12345678")

    path, body, headers = transport.posts[-1]
    assert path == "/v1/auth/local-browser/pair"
    assert body == {"path": "/reviews/r-12345678"}
    assert headers == {"Authorization": f"Bearer {local.token}"}


def test_live_view_binding_round_trip_is_server_scoped(config: Any, worktree, state_dir) -> None:
    from lemoncrow_client.liveview import (
        LiveViewBinding,
        clear_live_view_binding,
        load_live_view_binding,
        write_live_view_binding,
    )

    binding = LiveViewBinding(
        server_url=config.url,
        repo_id="repo-live",
        view_id="view-live",
        view_revision=7,
        workspace_fingerprint="fp-live",
    )
    write_live_view_binding(state_dir, worktree, binding)

    assert load_live_view_binding(state_dir, worktree, server_url=config.url) == binding
    assert load_live_view_binding(state_dir, worktree, server_url="http://127.0.0.1:9") is None

    clear_live_view_binding(state_dir, worktree, expected_view_id="another-view")
    assert load_live_view_binding(state_dir, worktree, server_url=config.url) == binding
    clear_live_view_binding(state_dir, worktree, expected_view_id="view-live")
    assert load_live_view_binding(state_dir, worktree, server_url=config.url) is None


def test_remote_session_publishes_and_advances_live_view_binding(config: Any, worktree, state_dir) -> None:
    from lemoncrow_client.liveview import load_live_view_binding
    from lemoncrow_client.session import RemoteSession, workspace_state_fingerprint

    session = RemoteSession(config)
    report = session.bootstrap(deadline_s=10.0)
    assert report.ok, report.reason

    binding = load_live_view_binding(state_dir, worktree, server_url=config.url)
    assert binding is not None
    assert binding.repo_id == session.state.repo_id
    assert binding.view_id == session.state.view_id
    assert binding.view_revision == session.state.view_revision
    assert binding.workspace_fingerprint == workspace_state_fingerprint(worktree)

    previous_revision = binding.view_revision
    (worktree / "pkg" / "alpha.py").write_text("def alpha():\n    return 'changed'\n", encoding="utf-8")
    session.push_paths(("pkg/alpha.py",))

    advanced = load_live_view_binding(state_dir, worktree, server_url=config.url)
    assert advanced is not None
    assert advanced.view_id == binding.view_id
    assert advanced.view_revision > previous_revision
    assert advanced.workspace_fingerprint == workspace_state_fingerprint(worktree)

    session.close()
    assert load_live_view_binding(state_dir, worktree, server_url=config.url) is None


def test_fresh_remote_session_can_adopt_live_view_without_opening_it(config: Any) -> None:
    from lemoncrow_client.liveview import LiveViewBinding

    session = RemoteSession(config, transport=_RecordingTransport())  # type: ignore[arg-type]
    session.state.session_id = "new-http-session"
    binding = LiveViewBinding(
        server_url=config.url,
        repo_id="repo-author",
        view_id="view-author",
        view_revision=11,
        workspace_fingerprint="fingerprint-author",
    )

    session.adopt_live_view(binding)

    assert session.state.repo_id == "repo-author"
    assert session.state.view_id == "view-author"
    assert session.state.view_revision == 11
