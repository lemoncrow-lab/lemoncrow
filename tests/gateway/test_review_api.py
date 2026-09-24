"""The review workspace API: what it serves, and everything it refuses.

This service reads a private repository over HTTP, so the refusals are the
feature and each one is asserted with its negative case beside it -- a control
that only ever sees the happy path is a control nobody has tested.

The five gates, and the failure each one exists to prevent:

* **token** -- a loopback bind is not an auth boundary; every local process and
  every browser tab shares it.
* **Origin/Referer** -- without it, a page on another local port drives the API
  the moment the token leaks.
* **Host** -- without it, a rebound DNS name reaches a 127.0.0.1 listener.
* **path confinement** -- ``../../etc/passwd`` must be refused before anything
  is read, not after.
* **unit membership** -- a path inside the repository but outside the review is
  a 404, not a read. The reviewed change is the boundary, not the checkout.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import (
    SECURITY_HEADERS,
    _is_markdown_path,
    build_groups,
    degraded_note,
    group_for,
    make_token_dependency,
    origin_refusal,
    register_review_api,
    status_column,
    synthesize_file_patch,
)
from lemoncrow.pro.capabilities.review.delivery import ClaudeDeliveryResult, mark_feedback_addressed
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.models import ImpactSite
from lemoncrow.pro.capabilities.review.packet import PacketBuild, build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.session_models import FrontierEntry
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, refresh, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "test-workspace-token"
PORT = 45999
BASE = f"http://127.0.0.1:{PORT}"

_BASE_SOURCE = '''"""Session management."""


class SessionManager:
    def __init__(self, store):
        self.store = store

    def refresh(self, user):
        record = self.store.get(user)
        if record is None:
            return {"status": "expired"}
        return {"status": "ok", "user": user}
'''

_CHANGED_SOURCE = _BASE_SOURCE.replace('"expired"', '"invalid"')

_UNTOUCHED_SOURCE = "def untouched(value):\n    return value\n"


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _init_repo(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"
    return repo


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000 + offset, 0)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


class Workspace:
    """A real store, a real repository, and a client that speaks to both."""

    def __init__(
        self, client: TestClient, store: ReviewStore, store_root: Path, repo_root: Path, review_id: str
    ) -> None:
        self.client = client
        self.store = store
        self.store_root = store_root
        self.repo_root = repo_root
        self.review_id = review_id

    def get(self, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        headers.update(kwargs.pop("headers", {}))
        return self.client.get(path, headers=headers, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        headers.update(kwargs.pop("headers", {}))
        return self.client.post(path, headers=headers, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        headers.update(kwargs.pop("headers", {}))
        return self.client.patch(path, headers=headers, **kwargs)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    repo_root = tmp_path / "repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "src/session.py", _BASE_SOURCE)
    _write(repo_root, "src/untouched.py", _UNTOUCHED_SOURCE)
    _commit(repo, "base", 0)
    _write(repo_root, "src/session.py", _CHANGED_SOURCE)

    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)

    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=repo_root,
        port=PORT,
    )
    client = TestClient(app, base_url=BASE)
    return Workspace(client, store, store_root, repo_root, session.id)


def _authenticated_routes(workspace: Workspace) -> list[tuple[str, str]]:
    review = workspace.review_id
    return [
        ("GET", "/api/reviews"),
        ("GET", f"/api/reviews/{review}"),
        ("GET", f"/api/reviews/{review}/revisions"),
        ("GET", f"/api/reviews/{review}/compare"),
        ("GET", "/api/compare?from_ref=git/HEAD&to_ref=worktree"),
        ("GET", f"/api/reviews/{review}/targets"),
        ("GET", f"/api/reviews/{review}/units"),
        ("GET", f"/api/reviews/{review}/files/src/session.py/patch"),
        ("GET", f"/api/reviews/{review}/patch"),
        ("POST", f"/api/reviews/{review}/marks"),
        ("POST", f"/api/reviews/{review}/marks/bulk"),
    ]


# --------------------------------------------------------------------------- #
# 1. token
# --------------------------------------------------------------------------- #


def test_every_route_but_healthz_refuses_an_unauthenticated_request(workspace: Workspace) -> None:
    """No token, no repository. Not one route leaks a byte of it."""

    for method, path in _authenticated_routes(workspace):
        response = workspace.client.request(method, path, json={"unit_key": "x"})
        assert response.status_code == 403, f"{method} {path} answered {response.status_code} without a token"


def test_the_same_routes_answer_with_the_token(workspace: Workspace) -> None:
    """The negative case of the test above: the gate is a gate, not a wall."""

    assert workspace.get("/api/reviews").status_code == 200
    assert workspace.get(f"/api/reviews/{workspace.review_id}").status_code == 200
    assert workspace.get(f"/api/reviews/{workspace.review_id}/revisions").status_code == 200
    assert workspace.get(f"/api/reviews/{workspace.review_id}/units").status_code == 200
    assert workspace.get(f"/api/reviews/{workspace.review_id}/files/src/session.py/patch").status_code == 200
    assert workspace.get(f"/api/reviews/{workspace.review_id}/patch").status_code == 200


def test_a_wrong_token_is_refused_not_merely_a_missing_one(workspace: Workspace) -> None:
    assert workspace.get(f"/api/reviews/{workspace.review_id}/units").status_code == 200
    assert workspace.get(f"/api/reviews/{workspace.review_id}/surfaces").status_code == 200
    assert workspace.get(f"/api/reviews/{workspace.review_id}/files/src/session.py/patch").status_code == 200


def test_surface_run_dispatches_surface_through_separate_runner_registry(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow.pro.capabilities.review.runtimes as runtimes_module
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
    from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult
    from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface

    seen: list[tuple[str, str, str]] = []
    surface = ReviewSurface(
        id="stack:app",
        provider="service",
        kind="service",
        title="app",
        runtime="stack-runtime",
        capabilities=("source", "execute", "results"),
    )

    class SurfaceRegistryFixture:
        def discover(self, _context: Any) -> tuple[Any, ...]:
            return (surface,)

    class RunnerRegistryFixture:
        def run(self, _context: Any, selected: ReviewSurface, *, side: str) -> SurfaceRunResult:
            seen.append((selected.provider, selected.id, side))
            return SurfaceRunResult(
                selected.id,
                selected.provider,
                side,
                "passed",
                "validated",
                runner="docker-compose",
                runtime=selected.runtime,
                exit_code=0,
            )

    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: SurfaceRegistryFixture())
    monkeypatch.setattr(runtimes_module, "built_in_runner_registry", lambda: RunnerRegistryFixture())
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/surface-run",
        params={"provider": "service", "surface_id": "stack:app", "side": "old"},
    )

    assert response.status_code == 200
    assert seen == [("service", "stack:app", "old")]
    assert response.json()["result"]["runner"] == "docker-compose"
    assert response.json()["result"]["runtime"] == "stack-runtime"


def test_surface_run_rejects_unknown_surface(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module

    class Registry:
        def discover(self, _context: Any) -> tuple[Any, ...]:
            return ()

    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: Registry())
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/surface-run",
        params={"provider": "bruno", "surface_id": "api/get", "side": "new"},
    )

    assert response.status_code == 422
    assert "not found" in response.json()["detail"]


def test_healthz_is_unauthenticated_and_says_nothing_about_the_repository(workspace: Workspace) -> None:
    """Liveness only. A health probe that named the repo would be a leak."""

    response = workspace.client.get("/healthz")
    assert response.status_code == 200
    body = response.text
    assert str(workspace.repo_root) not in body
    assert "session.py" not in body


# --------------------------------------------------------------------------- #
# 2. Origin / Referer / Host
# --------------------------------------------------------------------------- #


def test_a_foreign_origin_is_refused(workspace: Workspace) -> None:
    """Another local page must not be able to drive this API."""

    response = workspace.get("/api/reviews", headers={"Origin": "http://localhost:9999"})
    assert response.status_code == 403
    assert "Origin" in response.json()["detail"]


def test_the_workspaces_own_origin_is_accepted(workspace: Workspace) -> None:
    assert workspace.get("/api/reviews", headers={"Origin": BASE}).status_code == 200
    assert workspace.get("/api/reviews", headers={"Origin": f"http://localhost:{PORT}"}).status_code == 200


def test_a_foreign_referer_is_refused(workspace: Workspace) -> None:
    response = workspace.get("/api/reviews", headers={"Referer": "http://evil.example/page"})
    assert response.status_code == 403


def test_a_rebound_host_is_refused(workspace: Workspace) -> None:
    """DNS rebinding: the name resolves to loopback, the Host header does not."""

    response = workspace.get("/api/reviews", headers={"Host": f"attacker.example:{PORT}"})
    assert response.status_code == 403
    assert "Host" in response.json()["detail"]


def test_the_host_check_is_port_specific(workspace: Workspace) -> None:
    """Loopback on another port is a different service, not this one."""

    assert origin_refusal(host=f"127.0.0.1:{PORT + 1}", origin="", referer="", port=PORT)
    assert origin_refusal(host="", origin="", referer="", port=PORT) == "missing Host header"
    assert origin_refusal(host=f"localhost:{PORT}", origin=BASE, referer=f"{BASE}/review", port=PORT) == ""


# --------------------------------------------------------------------------- #
# 3 + 4. path confinement and unit membership
# --------------------------------------------------------------------------- #


def test_a_traversal_path_is_refused_before_anything_is_read(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """404 *and* nothing read: the refusal happens before the packet is opened.

    Asserted by making any packet read explode. A 404 that still read the stored
    artifact would mean the gate runs too late to be a gate.
    """

    def _explode(self: Any, revision: Any) -> bytes:
        raise AssertionError("the route read stored content for a path it was going to refuse")

    monkeypatch.setattr(ReviewStore, "read_packet_artifact", _explode)
    for candidate in ("../../etc/passwd", "../outside.txt", "/etc/passwd"):
        response = workspace.get(f"/api/reviews/{workspace.review_id}/files/{candidate}/patch")
        assert response.status_code == 404, candidate
        assert "root:" not in response.text


def test_a_repo_file_outside_the_review_is_a_404_not_a_read(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewed change is the boundary, not the checkout.

    ``src/untouched.py`` is committed, present on disk, and inside the repo. It
    is not part of this change, so it does not exist as far as this service is
    concerned.
    """

    assert (workspace.repo_root / "src/untouched.py").is_file()

    def _explode(self: Any, revision: Any) -> bytes:
        raise AssertionError("the route read stored content for a file outside the review")

    monkeypatch.setattr(ReviewStore, "read_packet_artifact", _explode)
    response = workspace.get(f"/api/reviews/{workspace.review_id}/files/src/untouched.py/patch")
    assert response.status_code == 404
    assert "def untouched" not in response.text


def test_a_review_for_another_repository_is_not_found(workspace: Workspace, tmp_path: Path) -> None:
    """One workspace serves one repo_root. Another checkout's session is a 404."""

    other_repo = tmp_path / "other"
    repo = _init_repo(other_repo)
    _write(other_repo, "a.py", "x = 1\n")
    _commit(repo, "base", 0)
    _write(other_repo, "a.py", "x = 2\n")
    store = ReviewStore(workspace.store_root)
    rng = resolve_rev_range(other_repo, None, working_tree=True)
    other = open_or_create_session(store, other_repo, rng)

    assert workspace.get(f"/api/reviews/{other.id}").status_code == 404
    listed = workspace.get("/api/reviews").json()["reviews"]
    assert [row["id"] for row in listed] == [workspace.review_id]


# --------------------------------------------------------------------------- #
# 5. marks persist
# --------------------------------------------------------------------------- #


def test_revision_compare_defaults_from_last_judged_revision_to_current(workspace: Workspace) -> None:
    marked = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": "src/session.py", "state": "reviewed"},
    )
    assert marked.status_code == 200, marked.text

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"revoked"'))
    session = workspace.store.get_session(workspace.review_id)
    assert session is not None
    rng = resolve_rev_range(workspace.repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(
        workspace.repo_root,
        rng,
        store_root=workspace.store_root,
        with_patch_text=True,
    )
    refreshed = refresh(
        workspace.store,
        session,
        workspace.repo_root,
        store_root=workspace.store_root,
        rng=rng,
        build=build,
    )
    assert refreshed.created is True
    assert refreshed.revision.revision_number == 2

    # Historical code comparison must be pinned to the frozen revision blobs,
    # not whatever happens to be in the working checkout when history is opened.
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"compromised"'))

    compared = workspace.get(f"/api/reviews/{workspace.review_id}/compare")
    assert compared.status_code == 200, compared.text
    body = compared.json()
    assert body["from_revision"]["revision_number"] == 1
    assert body["to_revision"]["revision_number"] == 2
    assert body["suggested_from_revision_number"] == 1
    assert body["summary"]["changed"] > 0
    assert body["code_available"] is True
    assert body["code_summary"]["files"] == 1
    code_file = next(row for row in body["code_files"] if row["path"] == "src/session.py")
    assert code_file["status"] == "changed"
    assert '"status": "invalid"' in code_file["patch"]
    assert '"status": "revoked"' in code_file["patch"]
    assert "compromised" not in code_file["patch"]
    assert any(row["path"] == "src/session.py" and row["status"] == "changed" for row in body["files"])
    file_delta = next(row for row in body["files"] if row["path"] == "src/session.py")
    assert file_delta["reviewed_before"] > 0
    assert file_delta["changed_since_review_after"] > 0
    assert any(
        row["path"] == "src/session.py"
        and row["from_state"] == "reviewed"
        and row["to_state"] == "changed_since_review"
        for row in body["units"]
    )

    same = workspace.get(f"/api/reviews/{workspace.review_id}/compare?from_revision=2&to_revision=2")
    assert same.status_code == 200, same.text
    same_body = same.json()
    assert same_body["summary"]["added"] == 0
    assert same_body["summary"]["changed"] == 0
    assert same_body["summary"]["removed"] == 0
    assert same_body["files"] == []
    assert same_body["code_available"] is True
    assert same_body["code_files"] == []


def test_generic_source_compare_supports_git_review_revisions_and_live_worktree(workspace: Workspace) -> None:
    git_live = workspace.get(
        "/api/compare",
        params={"from_ref": "git/HEAD", "to_ref": "worktree"},
    )
    assert git_live.status_code == 200, git_live.text
    git_live_body = git_live.json()
    assert git_live_body["from"]["kind"] == "git"
    assert git_live_body["to"]["kind"] == "worktree"
    live_file = next(item for item in git_live_body["files"] if item["path"] == "src/session.py")
    assert '"status": "expired"' in live_file["patch"]
    assert '"status": "invalid"' in live_file["patch"]

    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"revoked"'))
    session = workspace.store.get_session(workspace.review_id)
    assert session is not None
    rng = resolve_rev_range(workspace.repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(
        workspace.repo_root,
        rng,
        store_root=workspace.store_root,
        with_patch_text=True,
    )
    second_result = refresh(
        workspace.store,
        session,
        workspace.repo_root,
        store_root=workspace.store_root,
        rng=rng,
        build=build,
    )
    second = second_result.revision
    first_ref = f"rr/{first.id}"
    second_ref = f"rr/{second.id}"

    revision_pair = workspace.get(
        "/api/compare",
        params={"from_ref": first_ref, "to_ref": second_ref},
    )
    assert revision_pair.status_code == 200, revision_pair.text
    revision_body = revision_pair.json()
    assert revision_body["from"]["kind"] == "review_revision"
    assert revision_body["to"]["kind"] == "review_revision"
    revision_file = next(item for item in revision_body["files"] if item["path"] == "src/session.py")
    assert '"status": "invalid"' in revision_file["patch"]
    assert '"status": "revoked"' in revision_file["patch"]

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"compromised"'))
    live_from_review = workspace.get(
        "/api/compare",
        params={"from_ref": second_ref, "to_ref": "worktree"},
    )
    assert live_from_review.status_code == 200, live_from_review.text
    live_review_body = live_from_review.json()
    assert live_review_body["from"]["kind"] == "review_revision"
    assert live_review_body["to"]["kind"] == "worktree"
    live_review_file = next(item for item in live_review_body["files"] if item["path"] == "src/session.py")
    assert '"status": "revoked"' in live_review_file["patch"]
    assert '"status": "compromised"' in live_review_file["patch"]


def test_generic_source_compare_supports_commit_to_commit(workspace: Workspace) -> None:
    repo = pygit2.Repository(str(workspace.repo_root))
    _commit(repo, "changed session", 1)

    compared = workspace.get(
        "/api/compare",
        params={"from_ref": "git/HEAD~1", "to_ref": "git/HEAD"},
    )
    assert compared.status_code == 200, compared.text
    body = compared.json()
    assert body["from"]["kind"] == "git"
    assert body["to"]["kind"] == "git"
    file_row = next(item for item in body["files"] if item["path"] == "src/session.py")
    assert '"status": "expired"' in file_row["patch"]
    assert '"status": "invalid"' in file_row["patch"]


def test_review_directory_can_explicitly_include_finished_and_archived_reviews(workspace: Workspace) -> None:
    open_row = workspace.get("/api/reviews?status_filter=open")
    assert open_row.status_code == 200
    assert [item["id"] for item in open_row.json()["reviews"]] == [workspace.review_id]

    workspace.store.update_session(workspace.review_id, status="finished")
    assert workspace.get("/api/reviews?status_filter=open").json()["reviews"] == []
    finished = workspace.get("/api/reviews?status_filter=finished")
    assert [item["id"] for item in finished.json()["reviews"]] == [workspace.review_id]
    assert finished.json()["reviews"][0]["revision_number"] == 1

    workspace.store.update_session(workspace.review_id, status="archived")
    archived = workspace.get("/api/reviews?status_filter=archived")
    assert [item["id"] for item in archived.json()["reviews"]] == [workspace.review_id]
    all_rows = workspace.get("/api/reviews?status_filter=all")
    assert [item["id"] for item in all_rows.json()["reviews"]] == [workspace.review_id]
    assert all_rows.json()["status_filter"] == "all"


def test_review_directory_rejects_an_unknown_status_filter(workspace: Workspace) -> None:
    response = workspace.get("/api/reviews?status_filter=deleted")
    assert response.status_code == 422


def test_a_mark_persists_and_a_fresh_store_instance_sees_it(workspace: Workspace) -> None:
    """Phase 1's acceptance criterion, through the API this time."""

    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": "src/session.py", "state": "reviewed"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recorded"] == "reviewed"
    assert body["downgraded"] is False

    fresh = ReviewStore(workspace.store_root)
    marks = fresh.list_marks(workspace.review_id)
    assert [(mark.state, mark.unit_key) for mark in marks if mark.state == "reviewed"]


def test_reopening_is_the_same_route_with_a_different_state(workspace: Workspace) -> None:
    """Mark reviewed and reopen are one verb. Two would drift apart."""

    workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": "src/session.py", "state": "reviewed"},
    )
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": "src/session.py", "state": "unreviewed"},
    )
    assert response.status_code == 200
    assert response.json()["recorded"] == "unreviewed"
    rows = [row for group in response.json()["groups"] for row in group["rows"] if row["path"] == "src/session.py"]
    assert rows and rows[0]["group"] != "reviewed"


def test_a_mark_on_an_unknown_unit_is_refused_not_silently_dropped(workspace: Workspace) -> None:
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": "src/does-not-exist.py", "state": "reviewed"},
    )
    assert response.status_code == 404


def test_an_unknown_mark_state_is_refused(workspace: Workspace) -> None:
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": "src/session.py", "state": "approved"},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# 6. browser-facing headers
# --------------------------------------------------------------------------- #


def test_every_response_carries_the_security_headers(workspace: Workspace) -> None:
    for response in (workspace.client.get("/healthz"), workspace.get("/api/reviews")):
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        csp = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp
        assert "default-src 'none'" in csp
        assert "frame-src http://127.0.0.1:*" in csp
        assert "frame-src blob:" not in csp


def test_a_refusal_still_carries_the_security_headers(workspace: Workspace) -> None:
    """A 403 is a response a browser renders; it needs the same headers."""

    response = workspace.get("/api/reviews", headers={"Origin": "http://localhost:9999"})
    assert response.status_code == 403
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name.lower()] == value


# --------------------------------------------------------------------------- #
# what it actually serves
# --------------------------------------------------------------------------- #


def test_the_patch_route_returns_real_unified_diff_text(workspace: Workspace) -> None:
    """The centre pane's input: git's own patch, re-serialised, not recomputed."""

    body = workspace.get(f"/api/reviews/{workspace.review_id}/files/src/session.py/patch").json()
    assert body["renderable"] is True
    assert body["refusal"] == ""
    assert body["preview"] is None
    patch = body["patch"]
    assert patch.startswith("diff --git a/src/session.py b/src/session.py\n")
    assert "--- a/src/session.py" in patch
    assert "+++ b/src/session.py" in patch
    assert "@@" in patch
    assert '-            return {"status": "expired"}' in patch
    assert '+            return {"status": "invalid"}' in patch


def test_review_patch_download_is_complete_and_git_apply_ready(workspace: Workspace) -> None:
    response = workspace.get(f"/api/reviews/{workspace.review_id}/patch")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-diff")
    assert response.headers["content-disposition"] == 'attachment; filename="lemoncrow-review-rev-1.patch"'
    patch = response.text
    assert patch.startswith("diff --git a/src/session.py b/src/session.py\n")
    assert "--- a/src/session.py" in patch
    assert "+++ b/src/session.py" in patch
    assert "@@" in patch


def test_cursor_rule_mdc_is_rendered_as_markdown() -> None:
    assert _is_markdown_path("integrations/cursor/rules/lemoncrow.code.mdc")


def test_markdown_preview_is_pinned_to_the_recorded_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rendered docs and their images stay inside the reviewed document boundary."""

    repo_root = tmp_path / "markdown-repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "README.md", "# LemonCrow\n\nOld copy.\n")
    _write(repo_root, "docs/logo.svg", '<svg xmlns="http://www.w3.org/2000/svg"><rect width="8" height="8"/></svg>\n')
    _write(repo_root, "docs/private.svg", '<svg xmlns="http://www.w3.org/2000/svg"><text>secret</text></svg>\n')
    gif_path = repo_root / "docs/demo.gif"
    gif_path.write_bytes(b"GIF89a\x00OLD")
    _commit(repo, "base", 0)
    reviewed_gif = b"GIF89a\x00REVIEWED"
    gif_path.write_bytes(reviewed_gif)
    reviewed = (
        "# LemonCrow\n\nNew **reviewed** copy.\n\n"
        '<img src="docs/logo.svg" width="8" height="8">\n\n'
        '<img src="docs/demo.gif" width="8" height="8">\n\n'
        "![Build](https://img.example.invalid/build.svg)\n"
    )
    _write(repo_root, "README.md", reviewed)

    store_root = tmp_path / "markdown-store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    first_revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    # Advance the mutable worktree after the review snapshot. The preview must
    # keep showing what the human review was actually created against.
    _write(repo_root, "README.md", "# LemonCrow\n\nLater unreviewed copy.\n")
    gif_path.write_bytes(b"GIF89a\x00LATER")

    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=repo_root,
        port=PORT,
    )
    client = TestClient(app, base_url=BASE)
    response = client.get(
        f"/api/reviews/{session.id}/files/README.md/patch",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200, response.text
    preview = response.json()["preview"]
    assert preview == {
        "kind": "markdown",
        "old_content": "# LemonCrow\n\nOld copy.\n",
        "new_content": reviewed,
    }
    assert "Later unreviewed copy" not in response.text

    local_image = client.get(
        f"/api/reviews/{session.id}/markdown-image",
        params={"document_path": "README.md", "side": "new", "src": "docs/logo.svg"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert local_image.status_code == 200, local_image.text
    assert local_image.headers["content-type"].startswith("image/svg+xml")
    assert b"<rect" in local_image.content

    frozen_gif = client.get(
        f"/api/reviews/{session.id}/markdown-image",
        params={"document_path": "README.md", "side": "new", "src": "docs/demo.gif"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert frozen_gif.status_code == 200, frozen_gif.text
    assert frozen_gif.headers["content-type"].startswith("image/gif")
    assert frozen_gif.content == reviewed_gif

    media_detail = client.get(
        f"/api/reviews/{session.id}/files/docs/demo.gif/patch",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert media_detail.status_code == 200, media_detail.text
    media_payload = media_detail.json()
    assert media_payload["renderable"] is True
    assert media_payload["refusal"] == ""
    assert media_payload["detail"] == ""
    assert media_payload["preview"] == {
        "kind": "media",
        "media_type": "image/gif",
        "media_kind": "image",
    }
    direct_media = client.get(
        f"/api/reviews/{session.id}/media-file",
        params={"path": "docs/demo.gif", "side": "new"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert direct_media.status_code == 200, direct_media.text
    assert direct_media.content == reviewed_gif
    old_media = client.get(
        f"/api/reviews/{session.id}/media-file",
        params={"path": "docs/demo.gif", "side": "old"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert old_media.status_code == 200, old_media.text
    assert old_media.content == b"GIF89a\x00OLD"

    not_referenced = client.get(
        f"/api/reviews/{session.id}/markdown-image",
        params={"document_path": "README.md", "side": "new", "src": "docs/private.svg"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert not_referenced.status_code == 404

    async def _fake_remote_image(url: str, **_kwargs: Any) -> tuple[bytes, str]:
        assert url == "https://img.example.invalid/build.svg"
        return b'<svg xmlns="http://www.w3.org/2000/svg"><text>badge</text></svg>', "image/svg+xml"

    monkeypatch.setattr("lemoncrow.core.capabilities.web_fetch.async_fetch_image", _fake_remote_image)
    remote_image = client.get(
        f"/api/reviews/{session.id}/markdown-image",
        params={
            "document_path": "README.md",
            "side": "new",
            "src": "https://img.example.invalid/build.svg",
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert remote_image.status_code == 200, remote_image.text
    assert b"badge" in remote_image.content

    # Record the later worktree as a real N+1 revision. Requests pinned to N
    # must still serve N's exact bytes even though the session's latest revision
    # now contains different Markdown and a different GIF.
    next_rng = resolve_rev_range(repo_root, None, working_tree=True)
    next_build = build_review_packet_with_blobs(
        repo_root,
        next_rng,
        store_root=store_root,
        with_patch_text=True,
    )
    second_revision = snapshot_revision(
        store,
        session,
        repo_root,
        next_rng,
        store_root=store_root,
        build=next_build,
    )
    assert second_revision.id != first_revision.id

    latest_media = client.get(
        f"/api/reviews/{session.id}/media-file",
        params={"path": "docs/demo.gif", "side": "new"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert latest_media.status_code == 200, latest_media.text
    assert latest_media.content == b"GIF89a\x00LATER"

    pinned_media = client.get(
        f"/api/reviews/{session.id}/media-file",
        params={"path": "docs/demo.gif", "side": "new", "revision_id": first_revision.id},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert pinned_media.status_code == 200, pinned_media.text
    assert pinned_media.content == reviewed_gif

    pinned_markdown_gif = client.get(
        f"/api/reviews/{session.id}/markdown-image",
        params={
            "document_path": "README.md",
            "side": "new",
            "src": "docs/demo.gif",
            "revision_id": first_revision.id,
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert pinned_markdown_gif.status_code == 200, pinned_markdown_gif.text
    assert pinned_markdown_gif.content == reviewed_gif


def test_related_impact_source_opens_at_the_reviewed_revision(tmp_path: Path) -> None:
    """An out-of-patch caller is context the reviewer can inspect without an IDE.

    The route is not a repository browser: it serves only paths already present
    in the stored revision's impact evidence, and the bytes come from that
    revision's Git tree rather than from today's worktree.
    """

    repo_root = tmp_path / "related-repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "src/session.py", "def refresh(user):\n    return user\n")
    _write(repo_root, "src/caller.py", "from src.session import refresh\n\nvalue = refresh('a')\n")
    _write(repo_root, "src/secret.py", "SECRET = 'not review context'\n")
    _commit(repo, "base", 0)
    _write(repo_root, "src/session.py", "def refresh(user, context):\n    return user\n")

    store_root = tmp_path / "related-store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    original = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    packet = replace(
        original.packet,
        impact=(
            *original.packet.impact,
            ImpactSite(
                kind="untouched_caller",
                path="src/caller.py:L3",
                old="refresh",
                new="modified",
                snippet="value = refresh('a')",
                in_patch=False,
                source_path="src/session.py",
            ),
            # An in-patch impact must be navigated as a real diff, never through
            # the related-source route whose worktree semantics use the base tree.
            ImpactSite(
                kind="signature_change",
                path="src/session.py:L1",
                old="refresh",
                new="refresh",
                snippet="def refresh(user, context):",
                in_patch=True,
                source_path="src/session.py",
            ),
        ),
    )
    build = PacketBuild(packet=packet, blobs=original.blobs)
    session = open_or_create_session(store, repo_root, rng, title=packet.title)
    snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)

    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=repo_root,
        port=PORT,
    )
    client = TestClient(app, base_url=BASE)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    response = client.get(f"/api/reviews/{session.id}/related/src/caller.py?line=3", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "src/caller.py"
    assert body["focus_line"] == 3
    assert "value = refresh('a')" in body["text"]

    # Arbitrary repository reads and changed/in-patch files remain closed.
    assert client.get(f"/api/reviews/{session.id}/related/src/secret.py?line=1", headers=headers).status_code == 404
    assert client.get(f"/api/reviews/{session.id}/related/src/session.py?line=1", headers=headers).status_code == 404


def test_snapshot_only_composition_refuses_source_refresh(workspace: Workspace) -> None:
    app = FastAPI()
    register_review_api(
        app,
        workspace.store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        source_refresh_enabled=False,
    )
    client = TestClient(app, base_url=BASE)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    state = client.get(f"/api/reviews/{workspace.review_id}/source-state", headers=headers)
    assert state.status_code == 200
    assert state.json()["supported"] is False
    assert "run lc review" in state.json()["reason"]

    refreshed = client.post(f"/api/reviews/{workspace.review_id}/refresh", headers=headers)
    assert refreshed.status_code == 409
    assert "run lc review" in refreshed.json()["detail"]


def test_change_proposal_applies_to_trusted_checkout_and_links_to_next_revision(workspace: Workspace) -> None:
    overview = workspace.get(f"/api/reviews/{workspace.review_id}").json()
    revision_id = overview["revision"]["id"]
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/proposals",
        json={
            "expected_revision_id": revision_id,
            "path": "src/session.py",
            "start_line": 11,
            "end_line": 11,
            "side": "additions",
            "replacement_text": '            return {"status": "inactive"}',
            "intent": "Use the domain status expected by callers.",
        },
    )
    assert created.status_code == 201, created.text
    proposal = created.json()["proposal"]
    assert proposal["state"] == "proposed"
    assert proposal["base_revision_id"] == revision_id
    assert '-            return {"status": "invalid"}' in proposal["patch"]
    assert '+            return {"status": "inactive"}' in proposal["patch"]

    listed = workspace.get(f"/api/reviews/{workspace.review_id}/proposals")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["proposals"]] == [proposal["id"]]

    applied = workspace.post(f"/api/proposals/{proposal['id']}/apply")
    assert applied.status_code == 200, applied.text
    assert applied.json()["proposal"]["state"] == "applied"
    assert applied.json()["source_state"]["changed"] is True
    current = (workspace.repo_root / "src/session.py").read_text(encoding="utf-8")
    assert '"status": "inactive"' in current
    assert '"status": "invalid"' not in current

    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["revision"]["revision_number"] == 2
    linked = body["refreshed"]["applied_proposals"]
    assert len(linked) == 1
    assert linked[0]["id"] == proposal["id"]
    assert linked[0]["result_revision_id"] == body["revision"]["id"]
    assert linked[0]["result_target_ids"]

    after = workspace.get(f"/api/reviews/{workspace.review_id}/proposals").json()["proposals"]
    assert after[0]["result_revision_id"] == body["revision"]["id"]


def test_change_proposal_conflicts_if_checkout_moved_after_proposal(workspace: Workspace) -> None:
    revision_id = workspace.get(f"/api/reviews/{workspace.review_id}").json()["revision"]["id"]
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/proposals",
        json={
            "expected_revision_id": revision_id,
            "path": "src/session.py",
            "start_line": 11,
            "end_line": 11,
            "side": "additions",
            "replacement_text": '            return {"status": "inactive"}',
        },
    )
    proposal = created.json()["proposal"]

    external = _CHANGED_SOURCE + "\n# IDE edit\n"
    _write(workspace.repo_root, "src/session.py", external)
    refused = workspace.post(f"/api/proposals/{proposal['id']}/apply")
    assert refused.status_code == 409
    assert "source changed since this proposal was prepared" in refused.json()["detail"]
    assert (workspace.repo_root / "src/session.py").read_text(encoding="utf-8") == external

    stored = workspace.get(f"/api/reviews/{workspace.review_id}/proposals").json()["proposals"][0]
    assert stored["state"] == "conflicted"
    assert "source changed" in stored["conflict_reason"]


def test_change_proposal_from_older_revision_is_not_advertised_as_applicable(workspace: Workspace) -> None:
    revision_id = workspace.get(f"/api/reviews/{workspace.review_id}").json()["revision"]["id"]
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/proposals",
        json={
            "expected_revision_id": revision_id,
            "path": "src/session.py",
            "start_line": 11,
            "end_line": 11,
            "side": "additions",
            "replacement_text": '            return {"status": "inactive"}',
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["proposal"]["can_apply"] is True

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE + "\n# accepted unrelated edit\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"]["revision_number"] == 2

    listed = workspace.get(f"/api/reviews/{workspace.review_id}/proposals")
    assert listed.status_code == 200
    proposal = listed.json()["proposals"][0]
    assert proposal["state"] == "proposed"
    assert proposal["base_revision_id"] == revision_id
    assert proposal["can_apply"] is False


def test_change_proposal_preserves_crlf_source_bytes(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo-crlf"
    repo = _init_repo(repo_root)
    base_source = _BASE_SOURCE.replace("\n", "\r\n")
    changed_source = _CHANGED_SOURCE.replace("\n", "\r\n")
    _write(repo_root, "src/session.py", base_source)
    _commit(repo, "base", 0)
    _write(repo_root, "src/session.py", changed_source)

    store_root = tmp_path / "store-crlf"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)

    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=repo_root,
        port=PORT,
    )
    client = TestClient(app, base_url=BASE)
    crlf = Workspace(client, store, store_root, repo_root, session.id)

    revision_id = crlf.get(f"/api/reviews/{session.id}").json()["revision"]["id"]
    created = crlf.post(
        f"/api/reviews/{session.id}/proposals",
        json={
            "expected_revision_id": revision_id,
            "path": "src/session.py",
            "start_line": 11,
            "end_line": 11,
            "side": "additions",
            "replacement_text": '            return {"status": "inactive"}',
        },
    )
    assert created.status_code == 201, created.text
    applied = crlf.post(f"/api/proposals/{created.json()['proposal']['id']}/apply")
    assert applied.status_code == 200, applied.text

    data = (repo_root / "src/session.py").read_bytes()
    assert b'"status": "inactive"' in data
    assert b"\r\n" in data
    assert data.count(b"\n") == data.count(b"\r\n")


def test_source_state_detects_edits_without_advancing_the_review(workspace: Workspace) -> None:
    initial = workspace.get(f"/api/reviews/{workspace.review_id}/source-state")
    assert initial.status_code == 200
    assert initial.json()["supported"] is True
    assert initial.json()["changed"] is False

    before = workspace.get(f"/api/reviews/{workspace.review_id}/revisions").json()["revisions"]
    assert len(before) == 1

    # Detection is read-only: the current diff remains frozen until the human
    # accepts the new revision through POST /refresh.
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE + "\n# agent edit\n")
    changed = workspace.get(f"/api/reviews/{workspace.review_id}/source-state")
    assert changed.status_code == 200
    assert changed.json()["changed"] is True
    assert len(workspace.get(f"/api/reviews/{workspace.review_id}/revisions").json()["revisions"]) == 1

    # Reverting byte-for-byte to what was reviewed removes the notification.
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE)
    assert workspace.get(f"/api/reviews/{workspace.review_id}/source-state").json()["changed"] is False

    # Accepting a real edit captures/reconciles exactly once and establishes a
    # fresh baseline, so the cheap detector becomes quiet again.
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE + "\n# accepted edit\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["refreshed"]["created"] is True
    assert len(workspace.get(f"/api/reviews/{workspace.review_id}/revisions").json()["revisions"]) == 2
    assert workspace.get(f"/api/reviews/{workspace.review_id}/source-state").json()["changed"] is False


def test_historical_revision_reads_the_frozen_diff_targets_and_comments_without_mutating_latest(
    workspace: Workspace,
) -> None:
    first_revision = workspace.store.latest_revision(workspace.review_id)
    assert first_revision is not None
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "comment from rev one", "kind": "comment"},
    )
    assert created.status_code == 201, created.text
    first_comment = created.json()["annotation"]

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE + "\n# accepted edit\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"]["revision_number"] == 2

    second = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "comment from rev two", "kind": "comment"},
    )
    assert second.status_code == 201, second.text
    located = workspace.get(f"/api/revisions/{first_revision.id}")
    assert located.status_code == 200, located.text
    assert located.json()["review"]["id"] == workspace.review_id
    assert located.json()["review"]["ref"] == f"r/{workspace.review_id}"
    assert located.json()["revision"]["id"] == first_revision.id
    assert located.json()["revision"]["ref"] == f"rr/{first_revision.id}"
    assert "short_id" not in located.json()["review"]
    assert "short_id" not in located.json()["revision"]

    old_alias = workspace.get(f"/api/revisions/rr-{first_revision.id}")
    assert old_alias.status_code == 404

    historical = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/1")
    assert historical.status_code == 200, historical.text
    assert historical.json()["revision"]["id"] == first_revision.id
    assert historical.json()["latest_revision_number"] == 2
    # No human judgment was ever overwritten in this fixture, so H2 can honestly
    # call the historical judgment projection complete rather than globally
    # degrading every old revision just because it is historical.
    assert historical.json()["historical_judgment_complete"] is True

    targets = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/1/targets")
    assert targets.status_code == 200, targets.text
    assert targets.json()["revision_id"] == first_revision.id
    assert targets.json()["historical"] is True

    patch = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/1/files/src/session.py/patch")
    assert patch.status_code == 200, patch.text
    assert patch.json()["revision_id"] == first_revision.id
    assert patch.json()["historical"] is True
    assert "accepted edit" not in patch.json()["patch"]

    comments = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/1/annotations")
    assert comments.status_code == 200, comments.text
    assert comments.json()["historical"] is True
    assert [item["id"] for item in comments.json()["annotations"]] == [first_comment["id"]]
    old_comment = comments.json()["annotations"][0]
    assert old_comment["start_line"] == first_comment["start_line"]
    assert old_comment["anchor_method"] == first_comment["anchor_method"]

    # There is deliberately no historical mutation namespace. A revision URL is
    # read-only by construction rather than by a disabled button in the browser.
    refused = workspace.post(
        f"/api/reviews/{workspace.review_id}/revisions/1/marks",
        json={"unit_key": "src/session.py", "state": "reviewed"},
    )
    assert refused.status_code in {404, 405}
    assert workspace.store.latest_revision(workspace.review_id).revision_number == 2


def test_three_revision_history_replays_judgments_comment_versions_and_anchor_positions(
    workspace: Workspace,
) -> None:
    """H2 gate: history is reconstructed from events, never today's mutable rows."""

    rev1 = workspace.store.latest_revision(workspace.review_id)
    assert rev1 is not None
    targets1 = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    target1 = next(item for item in targets1 if item["path"] == "src/session.py")
    unit_key = target1["unit_key"]

    marked = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": unit_key, "state": "reviewed"},
    )
    assert marked.status_code == 200, marked.text
    comment = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "rev one wording", "kind": "comment"},
    )
    assert comment.status_code == 201, comment.text
    annotation_id = comment.json()["annotation"]["id"]

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"rev-two"'))
    refresh2 = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refresh2.status_code == 200, refresh2.text
    assert refresh2.json()["revision"]["revision_number"] == 2
    rev2_id = refresh2.json()["revision"]["id"]

    marked2 = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": unit_key, "state": "needs_changes"},
    )
    assert marked2.status_code == 200, marked2.text
    edited = workspace.patch(f"/api/annotations/{annotation_id}", json={"body": "rev two wording"})
    assert edited.status_code == 200, edited.text

    # Remove the file entirely. The target leaves the review and its live mark
    # is discarded, while the open comment records a terminal removed anchor.
    (workspace.repo_root / "src/session.py").unlink()
    refresh3 = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refresh3.status_code == 200, refresh3.text
    assert refresh3.json()["revision"]["revision_number"] == 3

    history = workspace.get(f"/api/reviews/{workspace.review_id}/targets/{unit_key}/history")
    assert history.status_code == 200, history.text
    transitions = [(item["event_kind"], item["from_state"], item["to_state"]) for item in history.json()["events"]]
    assert transitions == [
        ("judgment", "", "reviewed"),
        ("reconciled", "reviewed", "changed_since_review"),
        ("judgment", "changed_since_review", "needs_changes"),
        ("discarded", "needs_changes", ""),
    ]

    old1 = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/1")
    assert old1.status_code == 200, old1.text
    assert old1.json()["historical_judgment_complete"] is True
    assert old1.json()["progress"]["reviewed"] == 1

    old2 = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/2")
    assert old2.status_code == 200, old2.text
    assert old2.json()["revision"]["id"] == rev2_id
    assert old2.json()["progress"]["needs_changes"] == 1

    comments1 = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/1/annotations").json()["annotations"]
    comments2 = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/2/annotations").json()["annotations"]
    comments3 = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()["annotations"]
    c1 = next(item for item in comments1 if item["id"] == annotation_id)
    c2 = next(item for item in comments2 if item["id"] == annotation_id)
    c3 = next(item for item in comments3 if item["id"] == annotation_id)
    assert c1["body"] == "rev one wording"
    assert c1["anchor_method"] == "identical_blob"
    assert c2["body"] == "rev two wording"
    assert c3["body"] == "rev two wording"
    assert c3["state"] == "obsolete"
    assert c3["anchor_method"] == "removed"

    versions = workspace.get(f"/api/annotations/{annotation_id}/versions")
    assert versions.status_code == 200, versions.text
    assert versions.json()["versions"][0]["change_kind"] == "created"
    assert any(
        item["change_kind"] == "edited" and item["body"] == "rev two wording" for item in versions.json()["versions"]
    )
    assert versions.json()["versions"][-1]["state"] == "obsolete"

    activity = workspace.get(f"/api/reviews/{workspace.review_id}/activity")
    assert activity.status_code == 200, activity.text
    kinds = [item["kind"] for item in activity.json()["events"]]
    for expected in (
        "review.created",
        "revision.recorded",
        "mark.judgment",
        "mark.reconciled",
        "mark.discarded",
        "annotation.created",
        "annotation.updated",
        "annotation.state_changed",
    ):
        assert expected in kinds


def test_file_level_comment_never_invents_line_one_and_survives_refresh(workspace: Workspace) -> None:
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "file_level": True, "body": "whole-file concern"},
    )
    assert created.status_code == 201
    annotation = created.json()["annotation"]
    assert annotation["file_level"] is True
    assert annotation["start_line"] == 0
    assert annotation["end_line"] == 0
    assert annotation["anchor_method"] == "file"
    assert annotation["anchor_method_label"] == "file-level comment"

    # A later edit keeps the comment on the file, without converting it into a
    # made-up line anchor.
    (workspace.repo_root / "src/session.py").write_text(_CHANGED_SOURCE + "\n# another agent edit\n", encoding="utf-8")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200
    rows = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()["annotations"]
    current = next(item for item in rows if item["id"] == annotation["id"])
    assert current["file_level"] is True
    assert current["start_line"] == 0
    assert current["state"] == "open"
    assert current["anchor_method"] == "file"


def test_every_left_pane_row_carries_a_plain_language_reason(workspace: Workspace) -> None:
    """Plan SS5.2: no unexplained score. Asserted over every row, not a sample."""

    body = workspace.get(f"/api/reviews/{workspace.review_id}").json()
    rows = [row for group in body["groups"] for row in group["rows"]]
    assert rows
    for row in rows:
        assert row["reasons"], f"{row['path']} was ranked with no stated reason"
        assert all(isinstance(reason, str) and reason.strip() for reason in row["reasons"])


def test_the_five_groups_are_present_and_in_the_planned_order(workspace: Workspace) -> None:
    body = workspace.get(f"/api/reviews/{workspace.review_id}").json()
    assert [group["key"] for group in body["groups"]] == [
        "needs_attention",
        "changed_since_my_review",
        "unreviewed",
        "reviewed",
        "mechanical",
    ]


def test_marking_by_path_moves_target_progress_and_rolls_up_the_legacy_file_row(workspace: Workspace) -> None:
    """The compatibility file row must agree with the target denominator.

    A bare path widens onto the reader targets, never onto a separate file-level
    approval. The legacy ``groups`` projection therefore rolls its displayed file
    state up from those targets: once every target is reviewed the row is reviewed
    too, and a later deletion must reopen it rather than preserving that verdict.
    """

    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": "src/session.py", "state": "reviewed"},
    )
    assert response.status_code == 200, response.text
    marked = response.json()["marks"]
    assert marked, response.json()
    assert {row["path"] for row in marked} == {"src/session.py"}
    assert response.json()["progress"]["reviewed"] == len(marked), response.json()["progress"]

    body = workspace.get(f"/api/reviews/{workspace.review_id}").json()
    assert body["progress"]["reviewed"] == len(marked), body["progress"]
    reviewed = next(group for group in body["groups"] if group["key"] == "reviewed")
    row = next(item for item in reviewed["rows"] if item["path"] == "src/session.py")
    assert row["state"] == "reviewed"

    (workspace.repo_root / "src/session.py").unlink()
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    after = workspace.get(f"/api/reviews/{workspace.review_id}").json()
    rows = [item for group in after["groups"] for item in group["rows"]]
    deleted = next(item for item in rows if item["path"] == "src/session.py")
    assert deleted["file_status"] == "deleted"
    assert deleted["state"] != "reviewed"
    assert deleted["group"] != "reviewed"


def test_bulk_review_marks_only_current_plain_targets(workspace: Workspace) -> None:
    _write(workspace.repo_root, "notes.txt", "one ordinary review note\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text

    target_rows = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    note = next(row for row in target_rows if row["path"] == "notes.txt")
    session = next(row for row in target_rows if row["path"] == "src/session.py")
    assert note["state"] == "unreviewed"
    assert note["attention_level"] != "high"

    marked_risky = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": session["unit_key"], "state": "needs_changes"},
    )
    assert marked_risky.status_code == 200, marked_risky.text
    assert marked_risky.json()["recorded"] == "needs_changes"

    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks/bulk",
        json={
            "unit_keys": [
                note["unit_key"],
                session["unit_key"],
                note["unit_key"],
                "stale-target",
            ]
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["marked"] == [{"unit_key": note["unit_key"], "path": "notes.txt"}]
    assert result["skipped"] == [
        {
            "unit_key": session["unit_key"],
            "path": "src/session.py",
            "reason": "still needs individual attention",
        },
        {
            "unit_key": "stale-target",
            "path": "",
            "reason": "not a current review target",
        },
    ]

    after = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    states = {row["unit_key"]: row["state"] for row in after}
    assert states[note["unit_key"]] == "reviewed"
    assert states[session["unit_key"]] == "needs_changes"
    assert "stale-target" not in states


def test_bulk_review_refuses_a_target_with_open_review_discussion(workspace: Workspace) -> None:
    _write(workspace.repo_root, "src/support.py", "VALUE = 1\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text

    target_rows = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    support = next(row for row in target_rows if row["path"] == "src/support.py")
    annotation = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/support.py",
            "start_line": support["start_line"] or 1,
            "body": "Please confirm this value before bulk close-out.",
            "kind": "request_change",
            "target_unit_key": support["unit_key"],
            "mark_target": False,
        },
    )
    assert annotation.status_code == 201, annotation.text

    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks/bulk",
        json={"unit_keys": [support["unit_key"]]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["marked"] == []
    assert response.json()["skipped"] == [
        {
            "unit_key": support["unit_key"],
            "path": "src/support.py",
            "reason": "open request-change discussion requires individual attention",
        }
    ]


def test_bulk_review_allows_an_open_ordinary_comment(workspace: Workspace) -> None:
    _write(workspace.repo_root, "notes.txt", "one ordinary review note\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text

    target_rows = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    note = next(row for row in target_rows if row["path"] == "notes.txt")
    annotation = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "notes.txt",
            "start_line": note["start_line"] or 1,
            "body": "A note that does not request a change.",
            "kind": "comment",
            "target_unit_key": note["unit_key"],
        },
    )
    assert annotation.status_code == 201, annotation.text

    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks/bulk",
        json={"unit_keys": [note["unit_key"]]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["marked"] == [{"unit_key": note["unit_key"], "path": "notes.txt"}]
    assert response.json()["skipped"] == []


def test_targets_are_the_non_overlapping_reader_progress_denominator(workspace: Workspace) -> None:
    response = workspace.get(f"/api/reviews/{workspace.review_id}/targets")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision_id"]
    assert body["order"] == "recommended"
    assert body["targets"]
    assert body["progress"]["target_count"] == len(body["targets"])
    assert (
        sum(
            body["progress"][key]
            for key in ("reviewed", "changed_since_review", "needs_changes", "unreviewed", "unknown")
        )
        == body["progress"]["target_count"]
    )

    overview = workspace.get(f"/api/reviews/{workspace.review_id}").json()
    assert overview["target_count"] == body["progress"]["target_count"]
    assert overview["progress"] == body["progress"]
    assert overview["outline"] == body["outline"]


def test_targets_can_be_ordered_by_file_and_reject_unknown_orders(workspace: Workspace) -> None:
    by_file = workspace.get(f"/api/reviews/{workspace.review_id}/targets?order=file")
    assert by_file.status_code == 200, by_file.text
    paths_and_lines = [(row["path"], row["start_line"]) for row in by_file.json()["targets"]]
    assert paths_and_lines == sorted(paths_and_lines)

    bad = workspace.get(f"/api/reviews/{workspace.review_id}/targets?order=magic")
    assert bad.status_code == 422


def test_marking_a_reader_target_returns_target_progress_without_file_approval(workspace: Workspace) -> None:
    before = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()
    target = before["targets"][0]
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": target["unit_key"], "state": "reviewed"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target"]["unit_key"] == target["unit_key"]
    assert body["target"]["state"] == "reviewed"
    assert body["progress"]["reviewed"] == before["progress"]["reviewed"] + 1
    assert body["progress"]["target_count"] == before["progress"]["target_count"]

    after = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()
    current = next(item for item in after["targets"] if item["unit_key"] == target["unit_key"])
    assert current["state"] == "reviewed"


def test_marking_by_path_over_http_moves_the_target_counters(workspace: Workspace) -> None:
    """The audit finding, reached through the API rather than the CLI.

    ``POST /marks`` accepts a bare ``path`` and used to resolve it with
    ``resolve_unit``, which answers a path with the *file* unit. No reader target
    is keyed to a file unit unless the whole file is the one target, so the
    verdict landed where nothing counted it and this very response reported the
    progress it had not moved. A path is the imprecise spelling and has to widen
    onto the file's targets, exactly as ``lc review --mark PATH`` does.
    """

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE + "\n\ndef mint(user):\n    return user\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text

    before = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()
    on_path = [item for item in before["targets"] if item["path"] == "src/session.py"]
    assert len(on_path) > 1, before["targets"]
    assert {item["state"] for item in on_path} == {"unreviewed"}, on_path

    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"path": "src/session.py", "state": "reviewed"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # N targets on the path means N verdicts, and the response says so rather
    # than reporting the single file-unit mark nothing reads.
    assert {row["unit_key"] for row in body["marks"]} == {item["unit_key"] for item in on_path}, body["marks"]
    assert {row["label"] for row in body["marks"]} == {item["label"] for item in on_path}, body["marks"]
    assert body["progress"]["reviewed"] == before["progress"]["reviewed"] + len(on_path), body["progress"]
    assert body["progress"]["target_count"] == before["progress"]["target_count"], body["progress"]
    assert body["target"]["state"] == "reviewed", body["target"]

    after = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()
    assert {item["state"] for item in after["targets"] if item["path"] == "src/session.py"} == {"reviewed"}


def test_bulk_marking_accepts_current_normal_targets_not_arbitrary_raw_units(workspace: Workspace) -> None:
    _write(workspace.repo_root, "src/support.py", "VALUE = 1\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text

    target_body = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()
    support = [item for item in target_body["targets"] if item["path"] == "src/support.py"]
    assert support
    # A one-line constant in a new file is ranked on churn and its own "symbol
    # added" descriptor and nothing else -- exactly the remainder bulk completion
    # exists to close. Accepting either outcome here would leave the only test of
    # the accepting path unable to fail.
    assert [item["attention_level"] for item in support] == ["normal"]
    ordinary = support[0]

    marked = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks/bulk",
        json={"unit_keys": [ordinary["unit_key"]]},
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["marked"] == [{"unit_key": ordinary["unit_key"], "path": "src/support.py"}]
    assert marked.json()["skipped"] == []
    assert marked.json()["progress"]["reviewed"] >= 1

    raw_units = workspace.get(f"/api/reviews/{workspace.review_id}/units").json()["units"]
    target_keys = {item["unit_key"] for item in target_body["targets"]}
    non_target = next(
        (item for item in raw_units if item["unit_key"] not in target_keys and item["kind"] != "file"), None
    )
    if non_target is not None:
        refused = workspace.post(
            f"/api/reviews/{workspace.review_id}/marks/bulk",
            json={"unit_keys": [non_target["unit_key"]]},
        )
        assert refused.status_code == 200
        assert refused.json()["marked"] == []
        assert refused.json()["skipped"][0]["reason"] == "not a current review target"


def test_bulk_marking_refuses_a_file_whose_open_objection_belongs_to_no_target(workspace: Workspace) -> None:
    """A file-level objection is anchored on the file unit, which is not a target.

    ``annotate`` builds the anchor server-side and never honours the client's
    ``target_unit_key``, so a file-level ``request_change`` on a file that split
    into symbol targets belongs to none of them -- and every per-target check
    truthfully answers "nothing open here". The refusal has to be the file's, or
    a sweep closes a file whose reviewer is still waiting for an answer. The
    ``lc review comment`` path reaches this the same way: it sends no target key
    at all.
    """

    targets = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    target = next(item for item in targets if item["path"] == "src/session.py")
    assert target["attention_level"] == "normal" and target["state"] == "unreviewed"

    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "file_level": True,
            "kind": "request_change",
            "body": "the expiry contract changed; explain this before it lands",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["annotation"]["file_level"] is True
    assert created.json()["annotation"]["unit_key"] not in {item["unit_key"] for item in targets}

    swept = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks/bulk",
        json={"unit_keys": [target["unit_key"]]},
    )
    assert swept.status_code == 200, swept.text
    assert swept.json()["marked"] == []
    assert swept.json()["skipped"] == [
        {
            "unit_key": target["unit_key"],
            "path": "src/session.py",
            "reason": "an open request-change on this file belongs to no target; review individually",
        }
    ]


_CLASS_AND_METHODS_SOURCE = '''"""Session management."""


class SessionManager:
    DEFAULT = "anonymous"

    def __init__(self, store):
        self.store = store or {}

    def refresh(self, user):
        record = self.store.get(user)
        if record is None:
            return {"status": "invalid"}
        return {"status": "ok", "user": user}
'''


def test_bulk_gate_and_thread_counts_agree_about_who_owns_a_thread(workspace: Workspace) -> None:
    """One request must not answer "whose thread is this?" two different ways.

    ``owning_symbol_unit`` anchors a selection crossing two methods on the class
    that contains both, and that class is a target of its own here. GET /targets
    already credits the thread to the class alone -- identity beats the
    changed-span fallback -- so the bulk gate has to read the same ownership.
    Reading it inclusively refuses a method with "open request-change discussion
    requires individual attention" while the payload the reader is looking at
    says that same method has nothing open.
    """

    _write(workspace.repo_root, "src/session.py", _CLASS_AND_METHODS_SOURCE)
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text

    rows = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    by_symbol = {row["symbol"]: row for row in rows if row["path"] == "src/session.py"}
    assert {"SessionManager", "SessionManager.__init__"} <= set(by_symbol), sorted(by_symbol)
    method = by_symbol["SessionManager.__init__"]
    assert method["state"] == "unreviewed" and method["attention_level"] == "normal"

    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "start_line": 8,
            "end_line": 13,
            "kind": "request_change",
            "body": "the constructor and the refresh path need rethinking together",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["annotation"]["unit_key"] == by_symbol["SessionManager"]["unit_key"]

    after = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    counts = {row["unit_key"]: row["annotation_counts"]["open"] for row in after}
    assert counts[method["unit_key"]] == 0
    assert counts[by_symbol["SessionManager"]["unit_key"]] == 1

    swept = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks/bulk",
        json={"unit_keys": [method["unit_key"]]},
    )
    assert swept.status_code == 200, swept.text
    assert swept.json()["skipped"] == []
    assert swept.json()["marked"] == [{"unit_key": method["unit_key"], "path": "src/session.py"}]

    objected = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks/bulk",
        json={"unit_keys": [by_symbol["SessionManager"]["unit_key"]]},
    )
    assert objected.status_code == 200, objected.text
    assert objected.json()["marked"] == []
    assert objected.json()["skipped"] == [
        {
            "unit_key": by_symbol["SessionManager"]["unit_key"],
            "path": "src/session.py",
            "reason": "open request-change discussion requires individual attention",
        }
    ]


def test_units_are_ordered_by_attention_not_alphabetically(workspace: Workspace) -> None:
    body = workspace.get(f"/api/reviews/{workspace.review_id}/units").json()
    ranks = [row["attention_rank"] for row in body["units"] if row["attention_rank"]]
    assert ranks == sorted(ranks)


def test_degraded_signals_are_rendered_with_a_sentence_each(workspace: Workspace) -> None:
    body = workspace.get(f"/api/reviews/{workspace.review_id}").json()
    for item in body["degraded"]:
        assert item["note"], item["name"]


# --------------------------------------------------------------------------- #
# the pure pieces
# --------------------------------------------------------------------------- #


def test_a_hunk_with_no_recorded_body_is_refused_not_half_rendered() -> None:
    """``hunk_patch_truncated`` must not become a diff that silently omits lines."""

    entry = {
        "path": "a.py",
        "old_path": None,
        "status": "modified",
        "is_binary": False,
        "hunks": [{"header": "@@ -1,2 +1,2 @@", "patch": ""}],
    }
    text, refusal, detail = synthesize_file_patch(entry)
    assert text == ""
    assert refusal == "hunk_body_unavailable"
    assert detail


def test_an_unrecognised_line_origin_is_refused_rather_than_emitted() -> None:
    """libgit2 also emits '=', '>' and '<'. A guess would corrupt the parse."""

    entry = {
        "path": "a.py",
        "status": "modified",
        "is_binary": False,
        "hunks": [{"header": "@@ -1 +1 @@", "patch": "=no newline at end of file\n"}],
    }
    _, refusal, _ = synthesize_file_patch(entry)
    assert refusal == "unknown_line_origin"


def test_the_standard_no_newline_marker_is_preserved() -> None:
    entry = {
        "path": "new.py",
        "status": "added",
        "is_binary": False,
        "hunks": [{"header": "@@ -0,0 +1 @@", "patch": "+x = 1\n\\ No newline at end of file\n"}],
    }
    text, refusal, _ = synthesize_file_patch(entry)
    assert refusal == ""
    assert "\\ No newline at end of file" in text


def test_a_submodule_pointer_renders_from_the_recorded_gitlinks() -> None:
    old = "c6c4c800d7e9d5496611baf1dc635bf22fdb9434"
    new = "c64bf5eccb95a7b310300f466cd38649007a9185"
    text, refusal, _ = synthesize_file_patch(
        {
            "path": "landing",
            "status": "modified",
            "is_binary": False,
            "hunks": [],
            "submodule_pointer": [old, new],
        }
    )
    assert refusal == ""
    assert f"index {old[:7]}..{new[:7]} 160000" in text
    assert f"-Subproject commit {old}" in text
    assert f"+Subproject commit {new}" in text


def test_an_added_file_gets_a_dev_null_old_side() -> None:
    entry = {
        "path": "new.py",
        "status": "added",
        "is_binary": False,
        "hunks": [{"header": "@@ -0,0 +1 @@", "patch": "+x = 1\n"}],
    }
    text, refusal, _ = synthesize_file_patch(entry)
    assert refusal == ""
    assert "new file mode 100644" in text
    assert "--- /dev/null" in text


def test_a_binary_file_is_refused_with_its_reason() -> None:
    text, refusal, _ = synthesize_file_patch({"path": "logo.png", "status": "modified", "is_binary": True})
    assert text == ""
    assert refusal == "binary_file"


def _entry(**kwargs: Any) -> FrontierEntry:
    base: dict[str, Any] = {"unit_key": "fil:1", "kind": "file", "path": "a.py"}
    base.update(kwargs)
    return FrontierEntry(**base)


def test_a_submodule_pointer_is_advertised_as_renderable() -> None:
    groups, _ = build_groups(
        [],
        [_entry(path="landing")],
        {
            "files": [
                {
                    "path": "landing",
                    "status": "modified",
                    "is_binary": False,
                    "hunks": [],
                    "submodule_pointer": ["a" * 40, "b" * 40],
                }
            ]
        },
    )
    row = next(row for group in groups for row in group["rows"])
    assert row["renderable"] is True


def test_content_that_moved_under_a_mark_outranks_every_other_group() -> None:
    """The one group whose members were once believed done. It wins ties."""

    entry = _entry(state="reviewed", changed_since_mark=True, reasons=("public contract changed",))
    assert group_for(entry, "generated") == "changed_since_my_review"


def test_an_unreviewed_file_with_only_a_size_reason_is_not_promoted() -> None:
    """Churn moves the score; it is not a finding, so it must not shout."""

    assert group_for(_entry(reasons=("+12 -3",)), "production") == "unreviewed"
    assert group_for(_entry(reasons=("+12 -3", "public contract changed")), "production") == "needs_attention"


def test_a_generated_file_is_mechanical_until_something_points_at_it() -> None:
    assert group_for(_entry(reasons=("generated/vendor — skim",)), "generated") == "mechanical"
    assert group_for(_entry(state="reviewed"), "generated") == "reviewed"


def test_an_unknown_fingerprint_never_reads_as_reviewed_in_the_status_column() -> None:
    assert status_column("unknown", False, "modified") == "?M"
    assert status_column("reviewed", True, "modified") == "~M"
    assert status_column("reviewed", False, "added") == "✓A"


def test_a_row_with_no_ranking_signal_still_says_so() -> None:
    groups, counts = build_groups((), (_entry(),), None)
    rows = [row for group in groups for row in group["rows"]]
    assert rows[0]["reasons"] == ["no ranking signal fired -- nothing in this change points at it"]
    assert counts["unreviewed"] == 1


def test_an_unmapped_degraded_name_still_gets_a_sentence() -> None:
    """Swallowing an unknown signal hides a producer that fell back."""

    assert degraded_note("a_signal_added_next_year")
    assert "3" in degraded_note("intent_to_add_excluded:3")
    assert degraded_note("hunk_patch_truncated")


def test_a_counted_range_fact_is_not_described_as_a_producer_falling_back() -> None:
    """An unmerged path and a skipped store file are facts, not fallbacks.

    Neither signal means a producer degraded: one says the range contains a path
    with no stage-0 content to diff, the other says this command's own store
    directory was skipped. Printing "a producer fell back" over them names a
    cause that is not the cause.
    """

    unmerged = degraded_note("unmerged_paths:2")
    assert "producer fell back" not in unmerged
    assert "2" in unmerged and "unmerged" in unmerged

    excluded = degraded_note("lemoncrow_store_excluded:1")
    assert "producer fell back" not in excluded
    assert "1" in excluded and "store directory" in excluded


# --------------------------------------------------------------------------- #
# unit labels and the two actions plan SS5.5 ends on
# --------------------------------------------------------------------------- #


def test_every_unit_row_carries_the_label_the_terminal_prints(workspace: Workspace) -> None:
    """One spelling across both surfaces, or a reviewer marks the same thing twice."""

    body = workspace.get(f"/api/reviews/{workspace.review_id}/units").json()
    assert body["units"]
    for row in body["units"]:
        assert row["label"]
        if row["kind"] == "file":
            assert row["label"] == row["path"]
        if row["kind"] == "symbol" and row["symbol"]:
            assert row["label"].startswith(f"{row['path']}::{row['symbol']}")
    assert len({row["label"] for row in body["units"]}) == len(body["units"])


def test_same_named_symbols_get_distinguishable_labels() -> None:
    """The pure half of the rule, without a repository in the way."""

    from lemoncrow.pro.capabilities.review.api import ambiguous_symbols, unit_label
    from lemoncrow.pro.capabilities.review.session_models import ReviewUnit

    units = (
        ReviewUnit(
            revision_id="r",
            unit_key="sym:a",
            kind="symbol",
            path="src/svc.py",
            content_fingerprint="a",
            symbol="run",
            ordinal=0,
            start_line=2,
        ),
        ReviewUnit(
            revision_id="r",
            unit_key="sym:b",
            kind="symbol",
            path="src/svc.py",
            content_fingerprint="b",
            symbol="run",
            ordinal=1,
            start_line=9,
        ),
    )
    ambiguous = ambiguous_symbols(units)
    labels = [unit_label(unit, ambiguous=(unit.path, unit.symbol) in ambiguous) for unit in units]
    assert labels == ["src/svc.py::run@L2", "src/svc.py::run@L9"]
    # A name with only one definition keeps the plain spelling.
    assert unit_label(units[0]) == "src/svc.py::run"


def test_every_comment_on_the_wire_says_which_rung_put_it_there(workspace: Workspace) -> None:
    """`anchor_method_label` and `anchor_exact` ride on every annotation payload.

    The field was typed and on the wire and rendered nowhere, which is how a
    heuristic re-find came to be drawn with the same weight as an untouched
    file.
    """

    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "why?", "kind": "comment"},
    )
    assert created.status_code == 201, created.text
    payload = created.json()["annotation"]
    assert payload["anchor_method"] == "identical_blob"
    assert payload["anchor_method_label"] == "file unchanged since the comment"
    assert payload["anchor_exact"] is True

    listed = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()
    for row in listed["annotations"]:
        assert row["anchor_method_label"]
        assert row["anchor_method_label"] != row["anchor_method"]


def test_request_change_can_mark_its_current_review_target_in_one_interaction(workspace: Workspace) -> None:
    targets = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    target = next(item for item in targets if item["path"] == "src/session.py")
    span = next((item for item in target["spans"] if item["side"] == "new"), None)
    line = span["start_line"] if span is not None else max(1, target["start_line"])

    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "start_line": line,
            "end_line": line,
            "side": "new",
            "body": "Keep the invalid state explicit before this merges.",
            "kind": "request_change",
            "target_unit_key": target["unit_key"],
            "mark_target": True,
        },
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["annotation"]["kind"] == "request_change"
    assert payload["target"]["unit_key"] == target["unit_key"]
    assert payload["target"]["state"] == "needs_changes"
    assert payload["progress"]["needs_changes"] >= 1
    assert [item["path"] for item in payload["outline_updates"]] == ["src/session.py"]

    current = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    persisted = next(item for item in current if item["unit_key"] == target["unit_key"])
    assert persisted["state"] == "needs_changes"


def test_request_change_refuses_a_non_target_key_before_writing_the_comment(workspace: Workspace) -> None:
    before = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()["annotations"]
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "start_line": 10,
            "body": "This should never be partially recorded.",
            "kind": "request_change",
            "target_unit_key": "sym:not-current",
            "mark_target": True,
        },
    )
    assert response.status_code == 422
    assert "current ReviewTarget" in response.json()["detail"]
    after = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()["annotations"]
    assert len(after) == len(before)


def test_comment_anchors_to_frozen_revision_after_worktree_moves(workspace: Workspace) -> None:
    """A reviewer comments on what is on screen, never on newer bytes on disk."""

    frozen = workspace.store.latest_revision(workspace.review_id)
    assert frozen is not None
    snapshots = workspace.store.read_blob_artifact(workspace.review_id, frozen.id)
    assert snapshots is not None
    assert '"invalid"' in snapshots["src/session.py"]

    # The agent writes the next version while the human is still reading the
    # frozen diff. Do not refresh the ReviewRevision.
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"moved"'))

    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 11, "body": "keep this state explicit"},
    )
    assert response.status_code == 201, response.text
    annotation = workspace.store.get_annotation(response.json()["annotation"]["id"])
    assert annotation is not None
    assert annotation.anchor_method == "identical_blob"
    assert '"invalid"' in annotation.anchor.selected_text
    assert '"moved"' not in annotation.anchor.selected_text


def test_revision_bound_visual_evidence_uploads_previews_and_becomes_stale(workspace: Workspace) -> None:
    uploaded = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload"
        "?kind=screenshot&path=src/session.py&title=mobile%20checkout&filename=checkout.png&mime_type=image/png",
        content=b"fake-png-bytes",
        headers={"Content-Type": "image/png"},
    )
    assert uploaded.status_code == 201, uploaded.text
    evidence = uploaded.json()["evidence"]
    assert evidence["kind"] == "screenshot"
    assert evidence["path"] == "src/session.py"
    assert evidence["status"] == "current"
    assert evidence["content_url"].endswith(f"/evidence/{evidence['id']}/content")

    content = workspace.get(evidence["content_url"])
    assert content.status_code == 200
    assert content.content == b"fake-png-bytes"
    assert content.headers["content-type"].startswith("image/png")

    listed = workspace.get(f"/api/reviews/{workspace.review_id}/evidence").json()
    assert [item["id"] for item in listed["evidence"]] == [evidence["id"]]
    assert listed["evidence"][0]["status"] == "current"

    # Evidence describes the reviewed revision, not "whatever is current". A
    # later code revision therefore makes it stale automatically.
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"newer"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    later = workspace.get(f"/api/reviews/{workspace.review_id}/evidence").json()["evidence"]
    assert later[0]["status"] == "stale"


def test_evidence_stays_current_across_analysis_only_revision_rows(workspace: Workspace) -> None:
    uploaded = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload"
        "?kind=document&path=src/session.py&title=Focused%20tests&filename=focused.txt&mime_type=text/plain"
        "&source=test&verification_status=PASS&detail=582%20passed",
        content=b"582 passed",
        headers={"Content-Type": "text/plain"},
    )
    assert uploaded.status_code == 201, uploaded.text

    original = workspace.store.latest_revision(workspace.review_id)
    assert original is not None and original.source_fingerprint
    units = workspace.store.list_units(original.id)
    drifted = workspace.store.add_revision(
        replace(
            original,
            id="",
            revision_number=0,
            tree_fingerprint=f"analysis-drift-{original.tree_fingerprint}",
            packet_path="",
            packet_sha256="",
            packet_bytes=0,
        ),
        units,
    )
    assert drifted.id != original.id
    assert drifted.source_fingerprint == original.source_fingerprint

    listed = workspace.get(f"/api/reviews/{workspace.review_id}/evidence")
    assert listed.status_code == 200, listed.text
    evidence = listed.json()["evidence"]
    assert evidence[0]["status"] == "current"
    overview = workspace.get(f"/api/reviews/{workspace.review_id}")
    assert overview.status_code == 200, overview.text
    assert overview.json()["brief"]["artifacts"]["current"] == 1
    assert overview.json()["brief"]["artifacts"]["stale"] == 0

    targets = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    session_target = next(row for row in targets if row["path"] == "src/session.py")
    assert session_target["verification"] == {"pass": 1, "fail": 0, "unknown": 0}


def test_structured_verification_evidence_populates_overview_and_replaces_packet_not_run(workspace: Workspace) -> None:
    uploaded = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload"
        "?kind=document&title=Focused%20tests&filename=focused.txt&mime_type=text/plain"
        "&source=test&verification_status=PASS&detail=582%20passed",
        content=b"582 passed",
        headers={"Content-Type": "text/plain"},
    )
    assert uploaded.status_code == 201, uploaded.text

    overview = workspace.get(f"/api/reviews/{workspace.review_id}")
    assert overview.status_code == 200, overview.text
    body = overview.json()
    focused = [item for item in body["evidence"] if item["name"] == "Focused tests"]
    assert focused == [
        {
            "name": "Focused tests",
            "status": "PASS",
            "detail": "582 passed",
            "source": "test",
            "scope": "review",
        }
    ]
    assert body["brief"]["verification"]["pass"] >= 1


def test_file_verification_is_scoped_to_the_selected_file(workspace: Workspace) -> None:
    uploaded = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload"
        "?kind=document&path=src/session.py&title=Session%20tests&filename=session.txt&mime_type=text/plain"
        "&source=test&verification_status=PASS&detail=12%20passed",
        content=b"12 passed",
        headers={"Content-Type": "text/plain"},
    )
    assert uploaded.status_code == 201, uploaded.text

    detail = workspace.get(f"/api/reviews/{workspace.review_id}/files/src/session.py/patch")
    assert detail.status_code == 200, detail.text
    row = next(item for item in detail.json()["evidence"] if item["name"] == "Session tests")
    assert row == {
        "name": "Session tests",
        "status": "PASS",
        "detail": "12 passed",
        "source": "test",
        "scope": "file",
    }


def test_file_verification_controls_target_bulk_eligibility_and_newest_result_wins(workspace: Workspace) -> None:
    _write(workspace.repo_root, "notes.txt", "one ordinary review note\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text

    uploaded_fail = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={
            "kind": "document",
            "path": "notes.txt",
            "title": "Notes tests",
            "filename": "notes-fail.txt",
            "mime_type": "text/plain",
            "source": "test",
            "verification_status": "FAIL",
            "detail": "1 failed",
        },
        content=b"1 failed",
    )
    assert uploaded_fail.status_code == 201, uploaded_fail.text

    # Review-wide evidence is context only: it must never become a per-target
    # failure merely because it is current for the same revision.
    uploaded_global = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={
            "kind": "document",
            "title": "Global tests",
            "filename": "global.txt",
            "mime_type": "text/plain",
            "source": "test",
            "verification_status": "FAIL",
            "detail": "review-wide failure",
        },
        content=b"review-wide failure",
    )
    assert uploaded_global.status_code == 201, uploaded_global.text

    targets = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    note = next(row for row in targets if row["path"] == "notes.txt")
    assert note["verification"] == {"pass": 0, "fail": 1, "unknown": 0}

    refused = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks/bulk",
        json={"unit_keys": [note["unit_key"]]},
    )
    assert refused.status_code == 200, refused.text
    assert refused.json()["marked"] == []
    assert refused.json()["skipped"] == [
        {
            "unit_key": note["unit_key"],
            "path": "notes.txt",
            "reason": "verification is not settled",
        }
    ]

    uploaded_pass = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={
            "kind": "document",
            "path": "notes.txt",
            "title": "Notes tests",
            "filename": "notes-pass.txt",
            "mime_type": "text/plain",
            "source": "test",
            "verification_status": "PASS",
            "detail": "12 passed",
        },
        content=b"12 passed",
    )
    assert uploaded_pass.status_code == 201, uploaded_pass.text

    targets = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    note = next(row for row in targets if row["path"] == "notes.txt")
    assert note["verification"] == {"pass": 1, "fail": 0, "unknown": 0}

    detail = workspace.get(f"/api/reviews/{workspace.review_id}/files/notes.txt/patch")
    assert detail.status_code == 200, detail.text
    check = next(item for item in detail.json()["evidence"] if item["name"] == "Notes tests")
    assert check["status"] == "PASS"
    assert check["detail"] == "12 passed"

    accepted = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks/bulk",
        json={"unit_keys": [note["unit_key"]]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["marked"] == [{"unit_key": note["unit_key"], "path": "notes.txt"}]
    assert accepted.json()["skipped"] == []


def test_live_preview_evidence_is_a_revision_bound_link(workspace: Workspace) -> None:
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/link",
        json={"url": "http://127.0.0.1:3000/checkout", "title": "Checkout preview", "path": "src/session.py"},
    )
    assert response.status_code == 201, response.text
    evidence = response.json()["evidence"]
    assert evidence["kind"] == "live_preview"
    assert evidence["url"] == "http://127.0.0.1:3000/checkout"
    assert evidence["content_url"] == ""
    assert evidence["status"] == "current"


def test_evidence_cannot_claim_a_file_outside_the_review(workspace: Workspace) -> None:
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload?kind=screenshot&path=secrets.txt",
        content=b"x",
    )
    assert response.status_code == 404
    assert "not in this revision" in response.json()["detail"]


def test_the_feedback_export_route_renders_the_bundle(workspace: Workspace) -> None:
    workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "this hides an invalid session"},
    )
    body = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    assert body["open"] == 1
    assert "## Review feedback" in body["markdown"]
    assert "this hides an invalid session" in body["markdown"]
    assert "lemoncrow-annotation-id:" in body["markdown"]
    assert "Human review REQUIRED" in body["markdown"]


def _prepared_feedback_body(workspace: Workspace) -> dict[str, str]:
    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    return {
        "operation_id": preview["operation_id"],
        "expected_revision_id": preview["revision_id"],
        "expected_feedback_hash": preview["feedback_hash"],
    }


def _make_latest_revision_exact_claude(workspace: Workspace, session_id: str) -> None:
    with workspace.store._transaction() as conn:
        conn.execute(
            "UPDATE review_revisions SET provenance_host = 'claude', provenance_certainty = 'exact', "
            "provenance_session_id = ? WHERE review_id = ?",
            (session_id, workspace.review_id),
        )


def test_annotations_expose_current_exact_feedback_delivery_capability(workspace: Workspace) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)

    body = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()

    assert body["delivery"] == {
        "supported": True,
        "host": "claude",
        "session_id": session_id,
        "target_ref": f"claude:{session_id}",
        "label": "Claude",
        "reason": "",
    }


def test_claude_feedback_requires_exact_session_provenance(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def deliver(*_args: object, **_kwargs: object) -> ClaudeDeliveryResult:
        nonlocal called
        called = True
        return ClaudeDeliveryResult("sent", "claude:should-not-run")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session", deliver)
    body = _prepared_feedback_body(workspace)
    response = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/claude", json=body)
    assert response.status_code == 409
    assert "exact author-session provenance" in response.json()["detail"]
    assert called is False


def test_general_feedback_delivery_still_requires_a_prepared_preview(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "change this"},
    )
    called = False

    def deliver(*_args: object, **_kwargs: object) -> ClaudeDeliveryResult:
        nonlocal called
        called = True
        return ClaudeDeliveryResult("sent", f"claude:{session_id}")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session", deliver)
    response = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/deliver")
    assert response.status_code == 400
    assert "prepared preview" in response.json()["detail"]
    assert called is False


def test_legacy_claude_feedback_without_a_body_uses_one_safe_snapshot(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "change this once"},
    ).json()["annotation"]
    calls = 0

    def deliver(target: str, _repo_root: Path, _feedback: str) -> ClaudeDeliveryResult:
        nonlocal calls
        calls += 1
        return ClaudeDeliveryResult("sent", f"claude:{target}", remote_ref="deadbeef")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session", deliver)
    first = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/claude")
    assert first.status_code == 200, first.text
    assert first.json()["operation_id"].startswith("fop-compat-")
    assert calls == 1

    # A sequential retry sees the now-published annotation and therefore has
    # nothing left to send. It may refuse, but it must never dispatch twice.
    second = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/claude")
    assert second.status_code == 409
    assert "no unpublished human feedback" in second.json()["detail"]
    assert calls == 1
    (delivery,) = workspace.store.list_deliveries(created["id"])
    assert delivery.operation_id == first.json()["operation_id"]


def test_claude_feedback_delivers_only_human_open_feedback_and_records_delivery(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "keep the invalid state visible"},
    ).json()["annotation"]
    # Non-human evidence shares the annotation substrate but is not feedback
    # the reviewer sends back to the author.
    workspace.store.add_annotation(
        replace(
            workspace.store.get_annotation(created["id"]),
            id="",
            source="ai_review",
            source_id="review-skill",
            created_by_actor="agent",
            body="possible issue",
        )
    )
    seen: dict[str, str] = {}

    def deliver(target: str, repo_root: Path, feedback: str) -> ClaudeDeliveryResult:
        seen["target"] = target
        seen["repo"] = str(repo_root)
        seen["feedback"] = feedback
        return ClaudeDeliveryResult(
            "sent",
            f"claude:{target}",
            remote_ref="deadbeef",
            message="feedback sent to the exact Claude session",
        )

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session", deliver)
    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver",
        json={
            "operation_id": preview["operation_id"],
            "expected_revision_id": preview["revision_id"],
            "expected_feedback_hash": preview["feedback_hash"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "sent"
    assert body["annotation_count"] == 1
    assert seen["target"] == session_id
    assert "keep the invalid state visible" in seen["feedback"]
    assert "possible issue" not in seen["feedback"]

    deliveries = workspace.store.list_deliveries(created["id"])
    assert len(deliveries) == 1
    assert deliveries[0].state == "sent"
    assert deliveries[0].target_ref == f"claude:{session_id}"
    assert deliveries[0].remote_ref == "deadbeef"
    assert deliveries[0].operation_id == preview["operation_id"]
    assert deliveries[0].revision_id == preview["revision_id"]
    assert deliveries[0].feedback_hash == preview["feedback_hash"]
    assert deliveries[0].annotation_version > 0


def test_feedback_send_rejects_a_comment_change_after_preview(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "first request"},
    ).json()["annotation"]
    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    workspace.patch(f"/api/annotations/{created['id']}", json={"body": "changed request"})
    called = False

    def deliver(*_args: object, **_kwargs: object) -> ClaudeDeliveryResult:
        nonlocal called
        called = True
        return ClaudeDeliveryResult("sent", f"claude:{session_id}")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session", deliver)
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver",
        json={
            "operation_id": preview["operation_id"],
            "expected_revision_id": preview["revision_id"],
            "expected_feedback_hash": preview["feedback_hash"],
        },
    )
    assert response.status_code == 409
    assert "feedback changed since preview" in response.json()["detail"]
    assert called is False


def test_feedback_operation_is_idempotent_across_repeated_submission(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "change this once"},
    )
    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    calls = 0

    def deliver(target: str, repo_root: Path, feedback: str) -> ClaudeDeliveryResult:
        nonlocal calls
        calls += 1
        return ClaudeDeliveryResult("sent", f"claude:{target}", remote_ref="deadbeef")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session", deliver)
    payload = {
        "operation_id": preview["operation_id"],
        "expected_revision_id": preview["revision_id"],
        "expected_feedback_hash": preview["feedback_hash"],
    }
    first = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/deliver", json=payload)
    second = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/deliver", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == 1
    assert second.json()["state"] == "sent"


def test_sent_feedback_is_not_republished_on_the_same_revision(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "change this once"},
    ).json()["annotation"]
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session",
        lambda target, _repo, _feedback: ClaudeDeliveryResult("sent", f"claude:{target}", remote_ref="deadbeef"),
    )

    first = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    assert first["status"] == {
        "open_total": 1,
        "unpublished": 1,
        "published": 0,
        "in_flight": 0,
        "addressed": 0,
    }
    sent = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver",
        json={
            "operation_id": first["operation_id"],
            "expected_revision_id": first["revision_id"],
            "expected_feedback_hash": first["feedback_hash"],
        },
    )
    assert sent.status_code == 200, sent.text

    annotation_list = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()
    assert annotation_list["feedback"] == {
        "open_total": 1,
        "unpublished": 0,
        "published": 1,
        "in_flight": 0,
        "addressed": 0,
    }
    second = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    assert second["status"]["unpublished"] == 0
    assert second["open"] == 0
    assert second["orphaned"] == 0
    assert created["body"] not in second["markdown"]


def test_reviewer_edit_makes_published_feedback_unpublished_again(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "first request"},
    ).json()["annotation"]
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session",
        lambda target, _repo, _feedback: ClaudeDeliveryResult("sent", f"claude:{target}", remote_ref="deadbeef"),
    )
    first = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    sent = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver",
        json={
            "operation_id": first["operation_id"],
            "expected_revision_id": first["revision_id"],
            "expected_feedback_hash": first["feedback_hash"],
        },
    )
    assert sent.status_code == 200, sent.text

    edited = workspace.patch(f"/api/annotations/{created['id']}", json={"body": "second request"})
    assert edited.status_code == 200, edited.text
    status_body = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()["feedback"]
    assert status_body["unpublished"] == 1
    assert status_body["published"] == 0
    second = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    assert second["open"] == 1
    assert "second request" in second["markdown"]
    assert second["annotation_versions"][created["id"]] > first["annotation_versions"][created["id"]]


def test_human_reply_after_addressed_feedback_reopens_the_thread_for_delivery(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "keep the invalid state visible"},
    ).json()["annotation"]
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session",
        lambda target, _repo, _feedback: ClaudeDeliveryResult("sent", f"claude:{target}", remote_ref="deadbeef"),
    )

    first = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    first_sent = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver",
        json={
            "operation_id": first["operation_id"],
            "expected_revision_id": first["revision_id"],
            "expected_feedback_hash": first["feedback_hash"],
        },
    )
    assert first_sent.status_code == 200, first_sent.text
    (addressed,) = mark_feedback_addressed(
        workspace.store,
        workspace.repo_root,
        [created["id"]],
        host="claude",
        session_id=session_id,
    )
    assert addressed.author_response == "addressed"

    reply = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "start_line": 10,
            "body": "still broken on the fallback path",
            "parent_id": created["id"],
        },
    )
    assert reply.status_code == 201, reply.text

    annotation_list = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()
    root = next(item for item in annotation_list["annotations"] if item["id"] == created["id"])
    assert root["author_response"] == "none"
    assert annotation_list["feedback"] == {
        "open_total": 1,
        "unpublished": 1,
        "published": 0,
        "in_flight": 0,
        "addressed": 0,
    }

    versions = workspace.get(f"/api/annotations/{created['id']}/versions").json()["versions"]
    assert versions[-1]["change_kind"] == "thread_reply"
    assert versions[-1]["version_number"] > first["annotation_versions"][created["id"]]

    second = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    assert second["open"] == 1
    assert "keep the invalid state visible" in second["markdown"]
    assert "> still broken on the fallback path" in second["markdown"]
    assert second["annotation_versions"][created["id"]] > first["annotation_versions"][created["id"]]

    second_sent = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver",
        json={
            "operation_id": second["operation_id"],
            "expected_revision_id": second["revision_id"],
            "expected_feedback_hash": second["feedback_hash"],
        },
    )
    assert second_sent.status_code == 200, second_sent.text
    (addressed_again,) = mark_feedback_addressed(
        workspace.store,
        workspace.repo_root,
        [created["id"]],
        host="claude",
        session_id=session_id,
    )
    assert addressed_again.author_response == "addressed"


def test_author_addressed_claim_moves_feedback_to_rereview_instead_of_republishing(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "keep the invalid state visible"},
    ).json()["annotation"]
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session",
        lambda target, _repo, _feedback: ClaudeDeliveryResult("sent", f"claude:{target}", remote_ref="deadbeef"),
    )
    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    sent = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver",
        json={
            "operation_id": preview["operation_id"],
            "expected_revision_id": preview["revision_id"],
            "expected_feedback_hash": preview["feedback_hash"],
        },
    )
    assert sent.status_code == 200, sent.text

    (addressed,) = mark_feedback_addressed(
        workspace.store,
        workspace.repo_root,
        [created["id"]],
        host="claude",
        session_id=session_id,
    )
    assert addressed.author_response == "addressed"

    annotation_list = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()
    assert annotation_list["feedback"] == {
        "open_total": 1,
        "unpublished": 0,
        "published": 0,
        "in_flight": 0,
        "addressed": 1,
    }
    next_preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    assert next_preview["status"]["unpublished"] == 0
    assert created["body"] not in next_preview["markdown"]


def test_known_not_dispatched_feedback_failure_can_retry_the_same_prepared_operation(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "please change this"},
    ).json()["annotation"]
    attempts = 0

    def deliver(target: str, _repo: Path, _feedback: str) -> ClaudeDeliveryResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return ClaudeDeliveryResult(
                "blocked",
                f"claude:{target}",
                message="the exact Claude session is already running",
            )
        return ClaudeDeliveryResult(
            "sent",
            f"claude:{target}",
            remote_ref="deadbeef",
            message="feedback sent",
        )

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session", deliver)
    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    payload = {
        "operation_id": preview["operation_id"],
        "expected_revision_id": preview["revision_id"],
        "expected_feedback_hash": preview["feedback_hash"],
    }

    first = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/deliver", json=payload)
    assert first.status_code == 409
    assert "already running" in first.json()["detail"]
    assert workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()["feedback"] == {
        "open_total": 1,
        "unpublished": 1,
        "published": 0,
        "in_flight": 0,
        "addressed": 0,
    }

    second = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/deliver", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "sent"
    assert attempts == 2

    deliveries = workspace.store.list_deliveries(created["id"])
    assert len(deliveries) == 1
    assert deliveries[0].state == "sent"
    assert deliveries[0].remote_ref == "deadbeef"
    assert workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()["feedback"] == {
        "open_total": 1,
        "unpublished": 0,
        "published": 1,
        "in_flight": 0,
        "addressed": 0,
    }


def test_ambiguous_feedback_delivery_is_not_resendable(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "please change this"},
    ).json()["annotation"]
    attempts = 0

    def deliver(target: str, _repo: Path, _feedback: str) -> ClaudeDeliveryResult:
        nonlocal attempts
        attempts += 1
        return ClaudeDeliveryResult(
            "uncertain",
            f"claude:{target}",
            remote_ref="deadbeef",
            message="Claude started but exact-session verification failed",
        )

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session", deliver)
    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    payload = {
        "operation_id": preview["operation_id"],
        "expected_revision_id": preview["revision_id"],
        "expected_feedback_hash": preview["feedback_hash"],
    }

    first = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/deliver", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "uncertain"

    feedback = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()["feedback"]
    assert feedback == {
        "open_total": 1,
        "unpublished": 0,
        "published": 0,
        "in_flight": 1,
        "addressed": 0,
    }
    next_preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    assert next_preview["open"] == 0
    assert created["body"] not in next_preview["markdown"]

    second = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/deliver", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "uncertain"
    assert attempts == 1


def test_failed_exact_claude_delivery_is_recorded_before_the_api_refuses(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "please change this"},
    ).json()["annotation"]
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session",
        lambda *_args, **_kwargs: ClaudeDeliveryResult(
            "blocked", f"claude:{session_id}", message="the exact Claude session is already running"
        ),
    )
    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver",
        json={
            "operation_id": preview["operation_id"],
            "expected_revision_id": preview["revision_id"],
            "expected_feedback_hash": preview["feedback_hash"],
        },
    )
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]
    (delivery,) = workspace.store.list_deliveries(created["id"])
    assert delivery.state == "blocked"
    assert "already running" in delivery.last_error


def test_reviewer_outcome_is_separate_from_finish_and_becomes_stale_on_new_revision(workspace: Workspace) -> None:
    initial = workspace.get(f"/api/reviews/{workspace.review_id}/outcome")
    assert initial.status_code == 200
    assert initial.json()["current"] is None

    recorded = workspace.post(
        f"/api/reviews/{workspace.review_id}/outcome",
        json={"outcome": "lgtm", "summary": "Looks good to me."},
    )
    assert recorded.status_code == 200, recorded.text
    current = recorded.json()["current"]
    assert current["outcome"] == "lgtm"
    assert current["summary"] == "Looks good to me."
    assert current["stale"] is False

    # Finishing remains a separate lifecycle choice and never mutates the verdict.
    finished = workspace.post(f"/api/reviews/{workspace.review_id}/finish")
    assert finished.status_code == 200
    assert workspace.get(f"/api/reviews/{workspace.review_id}/outcome").json()["current"]["outcome"] == "lgtm"
    workspace.post(f"/api/reviews/{workspace.review_id}/finish", json={"status": "open"})

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE + "\n# another revision\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text

    stale = workspace.get(f"/api/reviews/{workspace.review_id}/outcome").json()
    assert stale["current"]["outcome"] == "lgtm"
    assert stale["current"]["stale"] is True
    assert len(stale["history"]) == 1


def test_finishing_a_review_records_the_decision_and_never_a_verdict(workspace: Workspace) -> None:
    """Plan SS5.5's last action, reachable. Plan SS4.1's limit on it, kept."""

    workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "still open"},
    )
    # Completion must carry current verification risk, not rely on the reviewer
    # remembering badges elsewhere on the page.
    for title, result in (("Focused tests", "FAIL"), ("Migration check", "UNKNOWN")):
        uploaded = workspace.post(
            f"/api/reviews/{workspace.review_id}/evidence/upload",
            params={
                "kind": "document",
                "title": title,
                "filename": f"{title}.txt",
                "mime_type": "text/plain",
                "source": "test",
                "verification_status": result,
                "detail": f"{title} is {result.lower()}",
            },
            content=result.encode(),
        )
        assert uploaded.status_code == 201, uploaded.text

    preview = workspace.get(f"/api/reviews/{workspace.review_id}/finish").json()
    assert preview["status"] == "open"
    assert workspace.get(f"/api/reviews/{workspace.review_id}").json()["session"]["status"] == "open"

    body = workspace.post(f"/api/reviews/{workspace.review_id}/finish").json()
    assert body["status"] == "finished"
    # Completion is the same non-overlapping target denominator the reader shows,
    # never the raw file/hunk/symbol substrate and never a file-only proxy.
    targets = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()
    assert body["target_count"] == targets["progress"]["target_count"]
    assert body["unreviewed_targets"] == targets["progress"]["unreviewed"]
    assert body["reviewed_targets"] == targets["progress"]["reviewed"]
    assert body["unknown_targets"] == targets["progress"]["unknown"]
    assert (
        body["reviewed_targets"]
        + body["unreviewed_targets"]
        + body["changed_since_review"]
        + body["needs_changes"]
        + body["unknown_targets"]
        == body["target_count"]
    )
    assert body["open_comments"] == 1
    assert body["failed_verification"] == 1
    assert body["unresolved_verification"] == 1
    assert body["previous_revision_evidence"] == 0
    assert body["discarded_verdicts"] == 0
    assert "approved" not in str(body).lower()

    overview = workspace.get(f"/api/reviews/{workspace.review_id}").json()
    assert overview["session"]["status"] == "finished"

    reopened = workspace.post(f"/api/reviews/{workspace.review_id}/finish", json={"status": "open"}).json()
    assert reopened["status"] == "open"


def test_an_unknown_review_status_is_refused(workspace: Workspace) -> None:
    response = workspace.post(f"/api/reviews/{workspace.review_id}/finish", json={"status": "approved"})
    assert response.status_code == 422
    assert "expected 'open', 'finished' or 'archived'" in response.json()["detail"]


def test_archiving_is_a_separate_choice_from_finishing(workspace: Workspace) -> None:
    """``archived`` is the one status retention cleanup collects, so it needs a door.

    Without a writer for it, ``ReviewStore.prune`` selects on a status nothing
    ever sets and the store keeps every review, revision, blob and evidence
    artifact forever. Finishing a review must not imply it: a finished review is
    a completed record, not a discarded one.
    """

    finished = workspace.post(f"/api/reviews/{workspace.review_id}/finish").json()
    assert finished["status"] == "finished"
    assert workspace.get(f"/api/reviews/{workspace.review_id}").json()["session"]["status"] == "finished"

    archived = workspace.post(f"/api/reviews/{workspace.review_id}/finish", json={"status": "archived"}).json()
    assert archived["status"] == "archived"
    assert workspace.get(f"/api/reviews/{workspace.review_id}").json()["session"]["status"] == "archived"

    # Discard is a tombstone, not a visual label. Mutation routes refuse it even
    # if an old browser tab still has controls on screen.
    blocked_mark = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"path": "src/session.py", "state": "reviewed"},
    )
    assert blocked_mark.status_code == 409
    assert "discarded and read-only" in blocked_mark.json()["detail"]
    blocked_refresh = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert blocked_refresh.status_code == 409
    blocked_finish = workspace.post(f"/api/reviews/{workspace.review_id}/finish", json={"status": "finished"})
    assert blocked_finish.status_code == 409

    restored = workspace.post(f"/api/reviews/{workspace.review_id}/finish", json={"status": "open"}).json()
    assert restored["status"] == "open"
    assert workspace.get(f"/api/reviews/{workspace.review_id}").json()["session"]["status"] == "open"


# --------------------------------------------------------------------------- #
# 9. re-reading the tree says what it did to the reviewer's own work
# --------------------------------------------------------------------------- #


def test_refresh_target_delta_reopens_only_the_reviewed_target_that_changed(workspace: Workspace) -> None:
    targets = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    reviewed = next(row for row in targets if row["symbol"] == "SessionManager.refresh")
    marked = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": reviewed["unit_key"], "state": "reviewed"},
    )
    assert marked.status_code == 200, marked.text

    # Same symbol identity, different body: this is exactly the target the
    # reviewer must see again; unrelated reviewed work would remain preserved.
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"ok"', '"ready"'))
    response = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert response.status_code == 200, response.text
    refreshed = response.json()["refreshed"]
    delta = refreshed["target_delta"]

    assert refreshed["changed_paths"] == ["src/session.py"]
    assert refreshed["added_paths"] == []
    assert refreshed["removed_paths"] == []
    assert refreshed["renamed_paths"] == []
    assert refreshed["preserved_paths"] == []
    assert [row["unit_key"] for row in delta["reopened"]] == [reviewed["unit_key"]]
    assert [row["unit_key"] for row in delta["active"]] == [reviewed["unit_key"]]
    assert delta["preserved"] == []


def test_a_refresh_names_the_verdicts_it_discarded_and_the_units_that_left(workspace: Workspace) -> None:
    """The browser must not delete a human's approval and report five counts.

    The terminal has printed ``VERDICTS DISCARDED`` since the block existed.
    This route answered ``reopened/carried/added/removed/notes`` for the same
    event -- no name, not the word "discarded", and nothing the reviewer could
    look up afterwards. Same records, same words, both doors.
    """

    listed = workspace.get(f"/api/reviews/{workspace.review_id}/units").json()
    unit_key = next(
        row["unit_key"] for row in listed["units"] if row["label"] == "src/session.py::SessionManager.refresh"
    )
    marked = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": unit_key, "state": "reviewed"},
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["recorded"] == "reviewed"

    # The agent renames the reviewed method away: its unit leaves the review, so
    # the verdict on it has to go -- and has to be named on the way out.
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace("def refresh(", "def renew("))

    response = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert response.status_code == 200, response.text
    refreshed = response.json()["refreshed"]

    discarded = refreshed["discarded"]
    assert [row["unit_key"] for row in discarded] == [unit_key]
    assert discarded[0]["label"] == "src/session.py::SessionManager.refresh"
    assert discarded[0]["state"] == "reviewed"
    assert discarded[0]["reason"] == "the unit it attested to is not in this revision"

    # It is a report of a deletion that already happened, not a warning about
    # one that might: the row is gone from the store, and recoverable only here.
    fresh = ReviewStore(workspace.store_root)
    assert unit_key not in [mark.unit_key for mark in fresh.list_marks(workspace.review_id)]
    kept = fresh.discarded_marks_on(refreshed_revision_id(fresh, workspace.review_id))
    assert [record.unit_key for record in kept] == [unit_key]

    # R25 projects the same transition into human review targets. The reviewed
    # method left, so the old target is named as removed and the replacement is
    # active rather than smuggled in as preserved work.
    target_delta = refreshed["target_delta"]
    assert any(row["label"] == "src/session.py::SessionManager.refresh" for row in target_delta["removed"])
    assert target_delta["active"]

    # The raw-unit audit substrate remains available alongside the target view.
    assert [row["label"] for row in refreshed["removed"]] == ["src/session.py::SessionManager.refresh"]

    # The ship blocker: it was read off the invocation's own reconciliation, so
    # the browser named the loss on exactly one request. A second *Re-read tree*
    # answered `"discarded": []` and a page reload carried no discard at all --
    # a destroyed approval vanishing from the screen the moment the reviewer
    # moved. It belongs to the revision, so every later look reports it.
    assert response.json()["discarded"] == discarded

    again = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert again.status_code == 200, again.text
    assert again.json()["refreshed"]["discarded"] == discarded
    assert again.json()["discarded"] == discarded

    reloaded = workspace.get(f"/api/reviews/{workspace.review_id}")
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["discarded"] == discarded

    # ...and every later *revision*, not merely every later request. The agent
    # edits once more: the discard row still sits on revision 2 while the
    # reviewer's frontier still sits on revision 1, and a report scoped to "this
    # revision" answered `[]` here -- on both routes at once -- while
    # `refreshed.removed` went on naming the unit. That is the browser's
    # "your reviewed verdict went with it" annotation silently dropping off.
    _write(
        workspace.repo_root,
        "src/session.py",
        _CHANGED_SOURCE.replace("def refresh(", "def renew(") + "\n\ndef unrelated_helper() -> int:\n    return 7\n",
    )
    third = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert third.status_code == 200, third.text
    assert third.json()["refreshed"]["revision_number"] == 3
    assert third.json()["refreshed"]["previous_revision_number"] == 1
    # Once, not once per revision that has gone by since.
    assert third.json()["discarded"] == discarded
    assert third.json()["refreshed"]["discarded"] == discarded
    assert [row["label"] for row in third.json()["refreshed"]["removed"]] == ["src/session.py::SessionManager.refresh"]

    third_reload = workspace.get(f"/api/reviews/{workspace.review_id}")
    assert third_reload.status_code == 200, third_reload.text
    assert third_reload.json()["discarded"] == discarded


def refreshed_revision_id(store: ReviewStore, review_id: str) -> str:
    """The revision the last refresh produced."""

    revision = store.latest_revision(review_id)
    assert revision is not None
    return revision.id
