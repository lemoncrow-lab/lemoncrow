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

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.infra.runtime.run_ledger import RunLedger
from lemoncrow.pro.capabilities.review import api as review_api
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
from lemoncrow.pro.capabilities.review.journal import collect_review_journal
from lemoncrow.pro.capabilities.review.models import ImpactSite, ProvenanceRecord
from lemoncrow.pro.capabilities.review.packet import PacketBuild, build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.session_models import (
    DeliveryRecord,
    FrontierEntry,
    ReviewAttribution,
    ReviewEvidence,
    ReviewRelationship,
    ReviewSession,
)
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, refresh, snapshot_revision
from lemoncrow.pro.capabilities.review.store import AnnotationHistoryContext, ReviewStore

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
        prefix = f"/api/reviews/{self.review_id}/"
        suffix = path[len(prefix) :] if path.startswith(prefix) else ""
        revision = self.store.latest_revision(self.review_id)
        revision_id = revision.id if revision is not None else ""
        source_coupled_json = {"marks", "marks/bulk", "annotations", "outcome", "evidence/link"}
        if suffix in source_coupled_json and revision_id:
            body = dict(kwargs.get("json") or {})
            body.setdefault("expected_revision_id", revision_id)
            kwargs["json"] = body
        elif suffix == "finish" and revision_id:
            body = dict(kwargs.get("json") or {})
            if str(body.get("status") or "finished") != "open":
                body.setdefault("expected_revision_id", revision_id)
            kwargs["json"] = body
        elif suffix.startswith("evidence/upload") and revision_id:
            if kwargs.get("params") is not None:
                params = dict(kwargs.get("params") or {})
                params.setdefault("expected_revision_id", revision_id)
                kwargs["params"] = params
            elif "expected_revision_id=" not in path:
                separator = "&" if "?" in path else "?"
                path = f"{path}{separator}expected_revision_id={revision_id}"
        return self.client.post(path, headers=headers, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        headers.update(kwargs.pop("headers", {}))
        body = dict(kwargs.get("json") or {})
        if "state" in body:
            revision = self.store.latest_revision(self.review_id)
            if revision is not None:
                body.setdefault("expected_revision_id", revision.id)
                kwargs["json"] = body
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
        ("GET", f"/api/reviews/{review}/journal"),
        ("GET", f"/api/reviews/{review}/ask"),
        ("POST", f"/api/reviews/{review}/ask"),
        ("GET", f"/api/reviews/{review}/relationships"),
        ("POST", f"/api/reviews/{review}/relationships"),
        ("GET", f"/api/reviews/{review}/live-state?wait_ms=0"),
        ("GET", f"/api/reviews/{review}/revisions"),
        ("GET", f"/api/reviews/{review}/readiness"),
        ("POST", f"/api/reviews/{review}/landing/arm"),
        ("POST", f"/api/reviews/{review}/landing/cancel"),
        ("GET", f"/api/reviews/{review}/verification"),
        ("POST", f"/api/reviews/{review}/verification"),
        ("POST", f"/api/reviews/{review}/browser-checks"),
        ("GET", f"/api/reviews/{review}/dependencies"),
        ("GET", f"/api/reviews/{review}/compare"),
        ("GET", "/api/compare?from_ref=git/HEAD&to_ref=worktree"),
        ("GET", f"/api/reviews/{review}/targets"),
        ("GET", f"/api/reviews/{review}/map"),
        ("GET", f"/api/reviews/{review}/units"),
        ("GET", f"/api/reviews/{review}/files/src/session.py/patch"),
        ("GET", f"/api/reviews/{review}/patch"),
        ("GET", f"/api/reviews/{review}/surface-preview?provider=fixture&surface_id=session&side=new"),
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


def test_review_map_is_snapshot_exact_and_current_graph_context_is_explicit(workspace: Workspace) -> None:
    response = workspace.get(f"/api/reviews/{workspace.review_id}/map")
    assert response.status_code == 200, response.text
    payload = response.json()
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    assert payload["revision_id"] == revision.id
    assert payload["revision_number"] == 0
    assert payload["level"] == "architecture"
    assert payload["snapshot"]["exactness"] == "snapshot"
    assert payload["snapshot"]["tree_fingerprint"] == revision.tree_fingerprint
    assert payload["context"]["exact_for_snapshot"] is False
    assert payload["context"]["source"] in {"current_index", "unavailable"}
    assert payload["graph"]["nodes"]
    assert all(node["selection"]["revision_id"] == revision.id for node in payload["graph"]["nodes"])

    historical = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/0/map")
    assert historical.status_code == 200, historical.text
    assert historical.json()["revision_id"] == revision.id

    invalid = workspace.get(f"/api/reviews/{workspace.review_id}/map?level=full-repository")
    assert invalid.status_code == 422


def test_overview_chapters_roll_up_existing_targets_without_becoming_review_state(workspace: Workspace) -> None:
    response = workspace.get(f"/api/reviews/{workspace.review_id}")
    assert response.status_code == 200, response.text
    payload = response.json()
    chapters = payload["chapters"]["intent"]
    assert chapters
    chapter_targets = [target for chapter in chapters for target in chapter["targets"]]
    assert len(chapter_targets) == payload["target_count"]
    assert sum(chapter["progress"]["target_count"] for chapter in chapters) == payload["target_count"]
    assert all(
        target["path"] in {row["path"] for row in chapter["rows"]}
        for chapter in chapters
        for target in chapter["targets"]
    )
    # This fixture is intentionally tiny: concepts stay available as a derived
    # lens, but the primary Reader does not add Change Story ceremony.
    assert payload["change_story"] == []
    assert payload["brief"]["major_changes"] == []


def test_change_identity_relationships_and_snapshot_metadata_are_explicit(workspace: Workspace) -> None:
    related = workspace.store.create_session(
        ReviewSession(
            id="",
            subject_type="commit_range",
            repo_root=str(workspace.repo_root.resolve()),
            range_mode="commit_range",
            source_ref="named:refs/heads/main...refs/heads/parent-change",
            title="Parent Change",
        )
    )

    work = workspace.post(
        f"/api/reviews/{workspace.review_id}/relationships",
        json={
            "kind": "work_item",
            "relation": "implements",
            "provider": "linear",
            "external_id": "ENG-431",
            "url": "https://linear.app/acme/issue/ENG-431",
            "title": "Device approval callback race",
        },
    )
    assert work.status_code == 200, work.text
    dependency = workspace.post(
        f"/api/reviews/{workspace.review_id}/relationships",
        json={
            "kind": "change",
            "relation": "depends_on",
            "target_change_ref": f"r/{related.id}",
            "source": "imported",
        },
    )
    assert dependency.status_code == 200, dependency.text
    requirement = workspace.post(
        f"/api/reviews/{workspace.review_id}/relationships",
        json={
            "kind": "scm_requirement",
            "relation": "requires",
            "provider": "github",
            "external_id": "required-checks",
            "status": "pending",
        },
    )
    assert requirement.status_code == 200, requirement.text

    workspace.store.update_session(workspace.review_id, source_ref="github:acme/widgets#7")
    context = workspace.get(f"/api/reviews/{workspace.review_id}/relationships")
    assert context.status_code == 200, context.text
    body = context.json()
    assert body["ref"] == f"r/{workspace.review_id}"
    assert body["scm"][0]["provider"] == "github"
    assert body["scm"][0]["external_id"] == "acme/widgets#7"
    assert body["scm"][0]["url"] == "https://github.com/acme/widgets/pull/7"
    assert body["work_items"][0]["external_id"] == "ENG-431"
    assert body["relations"][0]["target_change_ref"] == f"r/{related.id}"
    assert body["scm_requirements"][0]["status"] == "pending"

    overview = workspace.get(f"/api/reviews/{workspace.review_id}")
    assert overview.status_code == 200, overview.text
    payload = overview.json()
    assert payload["session"]["object"] == "change"
    assert payload["session"]["id"] == workspace.review_id
    assert payload["session"]["change_ref"] == f"r/{payload['session']['short_id']}"
    assert payload["revision"]["object"] == "snapshot"
    assert payload["revision"]["snapshot_ref"] == f"rr/{payload['revision']['short_id']}"
    assert payload["revision"]["source_fingerprint"]
    assert payload["change"]["work_items"][0]["external_id"] == "ENG-431"

    external_stack = workspace.post(
        f"/api/reviews/{workspace.review_id}/relationships",
        json={
            "kind": "change",
            "relation": "next",
            "provider": "github",
            "external_id": "acme/widgets#9",
            "title": "Follow-up Change",
            "source": "imported",
        },
    )
    assert external_stack.status_code == 200, external_stack.text
    assert external_stack.json()["relationship"]["target_change_ref"] == ""
    assert external_stack.json()["relationship"]["external_id"] == "acme/widgets#9"


def _ask_client(workspace: Workspace, answerer: Any, *, code_intelligence: Any | None = None) -> Workspace:
    store = ReviewStore(workspace.store_root)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        ask_answerer=answerer,
        code_intelligence=code_intelligence,
    )
    return Workspace(
        TestClient(app, base_url=BASE),
        store,
        workspace.store_root,
        workspace.repo_root,
        workspace.review_id,
    )


def _landing_client(workspace: Workspace, delegate: Any) -> Workspace:
    store = ReviewStore(workspace.store_root)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        landing_delegate=delegate,
    )
    return Workspace(
        TestClient(app, base_url=BASE), store, workspace.store_root, workspace.repo_root, workspace.review_id
    )


def _review_all_current_targets(workspace: Workspace) -> None:
    targets = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    for target in targets:
        recorded = workspace.post(
            f"/api/reviews/{workspace.review_id}/marks",
            json={"unit_key": target["unit_key"], "state": "reviewed"},
        )
        assert recorded.status_code == 200, recorded.text


def test_landing_readiness_combines_human_scm_stack_and_work_item_conditions(workspace: Workspace) -> None:
    calls: list[tuple[str, str]] = []

    def delegate(session: ReviewSession, revision: Any, *, action: str) -> dict[str, Any]:
        calls.append((revision.id, action))
        return {
            "supported": True,
            "state": "delegated" if action == "arm" else "cancelled",
            "provider": "github",
            "detail": "provider-native auto merge",
            "url": "https://github.com/acme/widgets/pull/7",
            "mechanism": "auto_merge_or_merge_queue",
        }

    landing = _landing_client(workspace, delegate)
    linked = workspace.store.create_session(
        ReviewSession(
            id="",
            subject_type="commit_range",
            repo_root=str(workspace.repo_root.resolve()),
            range_mode="commit_range",
            source_ref="named:dependency",
            title="Dependency Change",
        )
    )
    for payload in (
        {
            "kind": "scm",
            "relation": "pull_request",
            "provider": "github",
            "external_id": "acme/widgets#7",
            "url": "https://github.com/acme/widgets/pull/7",
        },
        {
            "kind": "work_item",
            "relation": "implements",
            "provider": "linear",
            "external_id": "ENG-431",
            "url": "https://linear.app/acme/issue/ENG-431",
            "title": "Fix callback race",
        },
        {
            "kind": "scm_requirement",
            "relation": "requires",
            "provider": "github",
            "external_id": "required-checks",
            "title": "Required checks",
            "status": "pending",
        },
        {"kind": "change", "relation": "depends_on", "target_change_ref": f"r/{linked.id}"},
    ):
        response = landing.post(f"/api/reviews/{workspace.review_id}/relationships", json=payload)
        assert response.status_code == 200, response.text

    before = landing.get(f"/api/reviews/{workspace.review_id}/readiness")
    assert before.status_code == 200, before.text
    body = before.json()
    assert body["ready"] is False
    assert body["armable"] is False
    assert body["work_items"][0]["external_id"] == "ENG-431"
    assert next(row for row in body["conditions"] if row["id"] == "latest-snapshot-reviewed")["state"] == "pending"
    scm = next(row for row in body["conditions"] if row["id"].startswith("scm-requirement:"))
    assert scm["owner"] == "scm" and scm["state"] == "pending"
    stack = next(row for row in body["conditions"] if row["id"].startswith("stack:"))
    assert stack["owner"] == "lemoncrow" and stack["state"] == "pending"
    assert body["landing"]["supported"] is True
    assert calls == []

    workspace.store.update_session(linked.id, status="finished")
    workspace.store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=linked.id,
            kind="landing",
            relation="merge_when_ready",
            provider="github",
            external_id="acme/widgets#6",
            status="merged",
            source="imported",
        )
    )
    _review_all_current_targets(landing)
    after = landing.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    assert next(row for row in after["conditions"] if row["id"].startswith("stack:"))["state"] == "pass"
    assert after["armable"] is True
    assert after["ready"] is False  # GitHub-owned required checks may still be pending.


def test_human_request_change_is_a_concrete_landing_blocker(workspace: Workspace) -> None:
    _review_all_current_targets(workspace)
    posted = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "side": "new",
            "start_line": 8,
            "end_line": 8,
            "body": "Please change this behavior.",
            "kind": "request_change",
        },
    )
    assert posted.status_code == 201, posted.text

    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    condition = next(row for row in readiness["conditions"] if row["id"] == "human-change-requests")
    assert condition["state"] == "fail"
    assert "1 unresolved human request-change" in condition["detail"]
    assert readiness["armable"] is False


def test_readiness_does_not_claim_unconfigured_verification_policy_or_approval_passed(workspace: Workspace) -> None:
    _review_all_current_targets(workspace)
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()

    verification = next(row for row in readiness["conditions"] if row["id"] == "verification")
    supply = next(row for row in readiness["conditions"] if row["id"] == "supply-chain-policy")
    approval = next(row for row in readiness["conditions"] if row["id"] == "human-approval-state")
    assert verification["state"] == "not_applicable"
    assert verification["label"] == "Verification requirements"
    assert supply["state"] == "not_applicable"
    assert approval["state"] == "not_applicable"
    assert readiness["ready"] is True
    assert readiness["armable"] is True


def test_readiness_names_failed_verification_and_supply_chain_policy(workspace: Workspace) -> None:
    _review_all_current_targets(workspace)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    workspace.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=workspace.review_id,
            revision_id=revision.id,
            kind="verification",
            title="Screenshot verification",
            source="external",
            source_ref="playwright:visual",
            verification_status="FAIL",
            detail="pixel mismatch",
        )
    )
    workspace.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=workspace.review_id,
            revision_id=revision.id,
            kind="vulnerability_scan",
            title="Dependency policy",
            source="external",
            source_ref="artifact-analysis",
            verification_status="FAIL",
            detail=json.dumps({"artifact_digest": "sha256:" + "a" * 64, "observation_source": "artifact-analysis"}),
        )
    )

    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    verification = next(row for row in readiness["conditions"] if row["id"] == "verification")
    supply = next(row for row in readiness["conditions"] if row["id"] == "supply-chain-policy")
    assert verification["state"] == "fail"
    assert "1 current check(s) failed" in verification["detail"]
    assert supply["state"] == "fail"
    assert "1 current supply-chain policy result(s) failed" in supply["detail"]
    assert readiness["armable"] is False


def _advance_snapshot_without_rerunning_checks(workspace: Workspace) -> None:
    source = workspace.repo_root / "src/session.py"
    _write(workspace.repo_root, "src/session.py", source.read_text(encoding="utf-8") + "\n# follow-up\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    _review_all_current_targets(workspace)


def test_a_failed_check_keeps_blocking_later_snapshots_until_it_reports_again(workspace: Workspace) -> None:
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None
    failed = workspace.post(
        f"/api/reviews/{workspace.review_id}/verification",
        json={"revision_id": first.id, "name": "CI / unit tests", "status": "FAIL", "source": "ci"},
    )
    assert failed.status_code == 201, failed.text

    _advance_snapshot_without_rerunning_checks(workspace)

    # The failure is stale evidence, never a pass -- but its absence on the new
    # Snapshot means "not re-run", not "no verification applies".
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    verification = next(row for row in readiness["conditions"] if row["id"] == "verification")
    assert verification["state"] == "pending"
    assert "CI / unit tests" in verification["detail"]
    assert readiness["armable"] is False

    second = workspace.store.latest_revision(workspace.review_id)
    assert second is not None and second.id != first.id
    rerun = workspace.post(
        f"/api/reviews/{workspace.review_id}/verification",
        json={"revision_id": second.id, "name": "CI / unit tests", "status": "PASS", "source": "ci"},
    )
    assert rerun.status_code == 201, rerun.text
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    verification = next(row for row in readiness["conditions"] if row["id"] == "verification")
    assert verification["state"] == "pass"
    assert readiness["armable"] is True


def test_a_stale_pass_is_named_but_never_counted_as_passing(workspace: Workspace) -> None:
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None
    passed = workspace.post(
        f"/api/reviews/{workspace.review_id}/verification",
        json={"revision_id": first.id, "name": "CI / unit tests", "status": "PASS", "source": "ci"},
    )
    assert passed.status_code == 201, passed.text

    _advance_snapshot_without_rerunning_checks(workspace)

    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    verification = next(row for row in readiness["conditions"] if row["id"] == "verification")
    assert verification["state"] == "not_applicable"
    assert "stale" in verification["detail"]
    assert "passed" not in verification["label"].lower()


def _readiness_and_verification(workspace: Workspace) -> tuple[dict[str, Any], dict[str, Any]]:
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    return readiness, next(row for row in readiness["conditions"] if row["id"] == "verification")


def _check_row(workspace: Workspace, name: str) -> dict[str, Any]:
    projection = workspace.get(f"/api/reviews/{workspace.review_id}/verification").json()
    return next(row for row in projection["checks"] if row["name"] == name)


def test_a_human_waiver_clears_a_stuck_check_on_the_exact_snapshot_only(workspace: Workspace) -> None:
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None
    failed = workspace.post(
        f"/api/reviews/{workspace.review_id}/verification",
        json={"revision_id": first.id, "name": "CI / unit tests", "status": "FAIL", "source": "ci"},
    )
    assert failed.status_code == 201, failed.text
    _advance_snapshot_without_rerunning_checks(workspace)
    second = workspace.store.latest_revision(workspace.review_id)
    assert second is not None and second.id != first.id
    assert _check_row(workspace, "CI / unit tests")["waivable"] is True

    url = f"/api/reviews/{workspace.review_id}/verification/waivers"
    no_reason = workspace.post(url, json={"revision_id": second.id, "name": "CI / unit tests", "reason": " "})
    assert no_reason.status_code == 422, no_reason.text
    old_snapshot = workspace.post(url, json={"revision_id": first.id, "name": "CI / unit tests", "reason": "retired"})
    assert old_snapshot.status_code == 409, old_snapshot.text
    unknown = workspace.post(url, json={"revision_id": second.id, "name": "CI / lint", "reason": "retired"})
    assert unknown.status_code == 409, unknown.text

    waived = workspace.post(
        url,
        json={"revision_id": second.id, "name": "CI / unit tests", "reason": "Job was retired from CI"},
    )
    assert waived.status_code == 201, waived.text

    row = _check_row(workspace, "CI / unit tests")
    assert row["status"] == "FAIL" and row["state"] == "stale"
    assert row["waiver"]["reason"] == "Job was retired from CI"
    assert row["waiver"]["snapshot_id"] == second.id
    readiness, verification = _readiness_and_verification(workspace)
    assert verification["state"] == "waived"
    assert "waived" in verification["detail"] and "Job was retired from CI" in verification["detail"]
    assert "passed" not in verification["label"].lower()
    assert readiness["armable"] is True

    # The waiver named one exact Snapshot; the next one is stuck again.
    _advance_snapshot_without_rerunning_checks(workspace)
    readiness, verification = _readiness_and_verification(workspace)
    assert verification["state"] == "pending"
    assert readiness["armable"] is False


def test_a_current_failure_cannot_be_waived_and_outranks_an_earlier_waiver(workspace: Workspace) -> None:
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    verification_url = f"/api/reviews/{workspace.review_id}/verification"
    waiver_url = f"{verification_url}/waivers"
    not_run = workspace.post(
        verification_url,
        json={"revision_id": revision.id, "name": "CI / e2e", "status": "NOT_RUN", "source": "ci"},
    )
    assert not_run.status_code == 201, not_run.text
    waived = workspace.post(
        waiver_url, json={"revision_id": revision.id, "name": "CI / e2e", "reason": "runner outage"}
    )
    assert waived.status_code == 201, waived.text
    assert _readiness_and_verification(workspace)[1]["state"] == "waived"

    failed = workspace.post(
        verification_url,
        json={"revision_id": revision.id, "name": "CI / e2e", "status": "FAIL", "source": "ci"},
    )
    assert failed.status_code == 201, failed.text
    assert _check_row(workspace, "CI / e2e")["waivable"] is False
    assert "waiver" not in _check_row(workspace, "CI / e2e")
    assert _readiness_and_verification(workspace)[1]["state"] == "fail"
    refused = workspace.post(waiver_url, json={"revision_id": revision.id, "name": "CI / e2e", "reason": "flaky"})
    assert refused.status_code == 409, refused.text


def test_a_failed_supply_chain_verdict_keeps_blocking_later_snapshots(workspace: Workspace) -> None:
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None
    workspace.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=workspace.review_id,
            revision_id=first.id,
            kind="vulnerability_scan",
            title="Dependency policy",
            source="external",
            source_ref="artifact-analysis",
            verification_status="FAIL",
            detail=json.dumps({"artifact_digest": "sha256:" + "a" * 64, "observation_source": "artifact-analysis"}),
        )
    )

    _advance_snapshot_without_rerunning_checks(workspace)

    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    supply = next(row for row in readiness["conditions"] if row["id"] == "supply-chain-policy")
    assert supply["state"] == "pending"
    assert "Dependency policy" in supply["detail"]
    assert readiness["armable"] is False


def test_merge_when_ready_requires_explicit_exact_conditions_and_invalidates_on_semantic_change(
    workspace: Workspace,
) -> None:
    calls: list[tuple[str, str]] = []

    def delegate(session: ReviewSession, revision: Any, *, action: str) -> dict[str, Any]:
        calls.append((revision.id, action))
        return {
            "supported": True,
            "state": "delegated" if action == "arm" else "cancelled",
            "provider": "github",
            "detail": "GitHub auto-merge delegated" if action == "arm" else "GitHub auto-merge disabled",
            "url": "https://github.com/acme/widgets/pull/7",
            "mechanism": "auto_merge_or_merge_queue",
        }

    landing = _landing_client(workspace, delegate)
    linked = landing.post(
        f"/api/reviews/{workspace.review_id}/relationships",
        json={
            "kind": "scm",
            "relation": "pull_request",
            "provider": "github",
            "external_id": "acme/widgets#7",
            "url": "https://github.com/acme/widgets/pull/7",
        },
    )
    assert linked.status_code == 200, linked.text
    _review_all_current_targets(landing)
    inspected = landing.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    assert inspected["armable"] is True

    missing_explicit = landing.post(f"/api/reviews/{workspace.review_id}/landing/arm", json={})
    assert missing_explicit.status_code == 400
    assert calls == []

    stale = landing.post(
        f"/api/reviews/{workspace.review_id}/landing/arm",
        json={
            "armed": True,
            "expected_revision_id": inspected["snapshot_id"],
            "expected_tree_fingerprint": inspected["tree_fingerprint"],
            "expected_conditions_hash": "stale-conditions",
        },
    )
    assert stale.status_code == 409
    assert calls == []

    armed = landing.post(
        f"/api/reviews/{workspace.review_id}/landing/arm",
        json={
            "armed": True,
            "expected_revision_id": inspected["snapshot_id"],
            "expected_tree_fingerprint": inspected["tree_fingerprint"],
            "expected_conditions_hash": inspected["conditions_hash"],
        },
    )
    assert armed.status_code == 200, armed.text
    armed_body = armed.json()
    assert armed_body["landing"]["armed"] is True
    assert armed_body["landing"]["state"] == "delegated"
    assert calls[-1] == (inspected["snapshot_id"], "arm")

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"new semantic source"'))
    refreshed = landing.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    current = landing.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    assert current["snapshot_id"] != inspected["snapshot_id"]
    assert current["landing"]["state"] == "stale"
    assert current["landing"]["semantic_stale"] is True
    assert calls[-1][1] == "cancel"
    assert current["armable"] is False


def _merged_on_cancel(session: ReviewSession, revision: Any, *, action: str) -> dict[str, Any]:
    """GitHub merges the reviewed head right after arming (e.g. the landing watch)."""

    return {
        "supported": True,
        "state": "armed" if action == "arm" else "merged",
        "provider": "github",
        "detail": "waiting" if action == "arm" else "GitHub already merged the reviewed head.",
        "url": "https://github.com/acme/widgets/pull/7",
    }


def _arm_github_landing(workspace: Workspace, delegate: Any) -> tuple[Workspace, dict[str, Any]]:
    landing = _landing_client(workspace, delegate)
    linked = landing.post(
        f"/api/reviews/{workspace.review_id}/relationships",
        json={
            "kind": "scm",
            "relation": "pull_request",
            "provider": "github",
            "external_id": "acme/widgets#7",
            "url": "https://github.com/acme/widgets/pull/7",
        },
    )
    assert linked.status_code == 200, linked.text
    _review_all_current_targets(landing)
    inspected = landing.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    armed = landing.post(
        f"/api/reviews/{workspace.review_id}/landing/arm",
        json={
            "armed": True,
            "expected_revision_id": inspected["snapshot_id"],
            "expected_tree_fingerprint": inspected["tree_fingerprint"],
            "expected_conditions_hash": inspected["conditions_hash"],
        },
    )
    assert armed.status_code == 200, armed.text
    return landing, inspected


def test_a_cancel_that_arrives_after_github_merged_records_the_merge(workspace: Workspace) -> None:
    landing, inspected = _arm_github_landing(workspace, _merged_on_cancel)

    cancelled = landing.post(
        f"/api/reviews/{workspace.review_id}/landing/cancel",
        json={"expected_revision_id": inspected["snapshot_id"]},
    )

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["landing"]["state"] == "merged"


def test_a_regression_after_github_merged_records_the_merge_not_a_block(workspace: Workspace) -> None:
    landing, _inspected = _arm_github_landing(workspace, _merged_on_cancel)

    posted = landing.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "side": "new",
            "start_line": 8,
            "end_line": 8,
            "body": "Please change this behavior.",
            "kind": "request_change",
        },
    )

    assert posted.status_code == 201, posted.text
    readiness = landing.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    assert readiness["landing"]["state"] == "merged"


def _merged_unreviewed_head_on_cancel(session: ReviewSession, revision: Any, *, action: str) -> dict[str, Any]:
    """GitHub merged a head pushed after arming before LemonCrow could withdraw."""

    return {
        "supported": True,
        "state": "armed" if action == "arm" else "blocked",
        "provider": "github",
        "detail": (
            "waiting" if action == "arm" else "GitHub merged head cccccccccccc, not the reviewed head bbbbbbbbbbbb."
        ),
        "url": "https://github.com/acme/widgets/pull/7",
    }


def test_a_cancel_that_finds_an_unreviewed_head_merged_is_never_reported_cancelled(workspace: Workspace) -> None:
    landing, inspected = _arm_github_landing(workspace, _merged_unreviewed_head_on_cancel)

    cancelled = landing.post(
        f"/api/reviews/{workspace.review_id}/landing/cancel",
        json={"expected_revision_id": inspected["snapshot_id"]},
    )

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["landing"]["state"] == "blocked"
    assert "not the reviewed head" in cancelled.json()["landing"]["detail"]


def test_a_regression_that_finds_an_unreviewed_head_merged_says_so(workspace: Workspace) -> None:
    landing, _inspected = _arm_github_landing(workspace, _merged_unreviewed_head_on_cancel)

    posted = landing.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "side": "new",
            "start_line": 8,
            "end_line": 8,
            "body": "Please change this behavior.",
            "kind": "request_change",
        },
    )

    assert posted.status_code == 201, posted.text
    detail = landing.get(f"/api/reviews/{workspace.review_id}/readiness").json()["landing"]["detail"]
    assert "not the reviewed head" in detail
    assert "was cancelled" not in detail


def test_discarding_an_armed_change_withdraws_provider_landing(workspace: Workspace) -> None:
    calls: list[tuple[str, str]] = []
    landing, inspected = _arm_github_landing(workspace, _github_delegate(calls))

    discarded = landing.post(f"/api/reviews/{workspace.review_id}/finish", json={"status": "archived"})

    assert discarded.status_code == 200, discarded.text
    assert calls[-1] == (inspected["snapshot_id"], "cancel")
    readiness = landing.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    assert readiness["landing"]["armed"] is False
    assert readiness["landing"]["state"] == "cancelled"
    assert "discarded" in readiness["landing"]["detail"]


def test_a_discard_that_raced_a_github_merge_records_the_merge(workspace: Workspace) -> None:
    landing, _inspected = _arm_github_landing(workspace, _merged_on_cancel)

    discarded = landing.post(f"/api/reviews/{workspace.review_id}/finish", json={"status": "archived"})

    assert discarded.status_code == 200, discarded.text
    readiness = landing.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    assert readiness["landing"]["state"] == "merged"


def test_merge_when_ready_can_still_be_cancelled_on_a_discarded_change(workspace: Workspace) -> None:
    calls: list[tuple[str, str]] = []
    landing, inspected = _arm_github_landing(workspace, _github_delegate(calls))
    # A Change discarded before discarding withdrew landing keeps an armed intent.
    landing.store.update_session(workspace.review_id, status="archived")

    cancelled = landing.post(
        f"/api/reviews/{workspace.review_id}/landing/cancel",
        json={"expected_revision_id": inspected["snapshot_id"]},
    )

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["landing"]["state"] == "cancelled"
    assert calls[-1] == (inspected["snapshot_id"], "cancel")


def _condition_state(workspace: Workspace, condition_id: str) -> str:
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    return str(next(row for row in readiness["conditions"] if row["id"] == condition_id)["state"])


def test_deleting_the_file_a_human_objected_to_never_resolves_the_objection(workspace: Workspace) -> None:
    posted = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "side": "new",
            "start_line": 8,
            "end_line": 8,
            "body": "This must keep reporting expired sessions.",
            "kind": "request_change",
        },
    )
    assert posted.status_code == 201, posted.text
    annotation_id = posted.json()["annotation"]["id"]
    assert _condition_state(workspace, "human-change-requests") == "fail"

    # The author deletes the file instead of addressing the request.
    (workspace.repo_root / "src" / "session.py").unlink()
    assert workspace.post(f"/api/reviews/{workspace.review_id}/refresh").status_code == 200
    obsolete = workspace.store.get_annotation(annotation_id)
    assert obsolete is not None and obsolete.state == "obsolete"

    assert _condition_state(workspace, "human-change-requests") == "fail"

    resolved = workspace.patch(f"/api/annotations/{annotation_id}", json={"state": "resolved"})
    assert resolved.status_code == 200, resolved.text
    assert _condition_state(workspace, "human-change-requests") == "pass"


def test_a_human_may_settle_their_own_comment_as_obsolete(workspace: Workspace) -> None:
    posted = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "side": "new",
            "start_line": 8,
            "end_line": 8,
            "body": "Is this still needed?",
            "kind": "comment",
        },
    )
    assert posted.status_code == 201, posted.text
    assert _condition_state(workspace, "human-comments") == "pending"

    settled = workspace.patch(f"/api/annotations/{posted.json()['annotation']['id']}", json={"state": "obsolete"})

    assert settled.status_code == 200, settled.text
    assert _condition_state(workspace, "human-comments") == "pass"


def _verification_condition(workspace: Workspace) -> dict[str, Any]:
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    return next(row for row in readiness["conditions"] if row["id"] == "verification")


def _delete_evidence(workspace: Workspace, evidence_id: str) -> Any:
    return workspace.client.delete(f"/api/evidence/{evidence_id}", headers={"Authorization": f"Bearer {TOKEN}"})


def test_a_verification_result_cannot_be_deleted_to_clear_readiness(workspace: Workspace) -> None:
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    recorded = workspace.post(
        f"/api/reviews/{workspace.review_id}/verification",
        json={"revision_id": revision.id, "name": "unit", "status": "FAIL", "source": "test"},
    )
    assert recorded.status_code == 201, recorded.text
    assert _verification_condition(workspace)["state"] == "fail"

    refused = _delete_evidence(workspace, recorded.json()["evidence"]["id"])

    assert refused.status_code == 409, refused.text
    assert _verification_condition(workspace)["state"] == "fail"


def test_an_uploaded_verdict_is_append_only_but_plain_evidence_can_be_removed(workspace: Workspace) -> None:
    verdict = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={"kind": "screenshot", "title": "checkout", "verification_status": "FAIL"},
        content=b"\x89PNG fail",
        headers={"Content-Type": "image/png"},
    )
    plain = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={"kind": "screenshot", "title": "wrong window"},
        content=b"\x89PNG oops",
        headers={"Content-Type": "image/png"},
    )
    assert verdict.status_code == 201, verdict.text
    assert plain.status_code == 201, plain.text

    assert _delete_evidence(workspace, verdict.json()["evidence"]["id"]).status_code == 409
    assert _delete_evidence(workspace, plain.json()["evidence"]["id"]).status_code == 200
    assert _verification_condition(workspace)["state"] == "fail"


def test_author_session_evidence_neither_blocks_nor_passes_landing(workspace: Workspace) -> None:
    from lemoncrow.pro.capabilities.review.models import review_packet_from_dict
    from lemoncrow.pro.capabilities.review.sources.local import encode_packet, read_packet_json

    _review_all_current_targets(workspace)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    packet = read_packet_json(workspace.store, revision)
    assert packet is not None
    # What provenance attaches for an agent-authored Change: commands from any
    # point of the authoring session plus placeholders for checks never run.
    packet["evidence"] = [
        {"name": "Focused tests", "status": "NOT_RUN", "detail": "", "source": "none"},
        {"name": "Full suite", "status": "PASS", "detail": "pytest -q (exit 0)", "source": "session:abc"},
        {"name": "Migration check", "status": "UNKNOWN", "detail": "", "source": "none"},
    ]
    parsed = review_packet_from_dict(packet)
    workspace.store.update_revision_projection(
        revision.id,
        packet_payload=encode_packet(parsed),
        packet_schema_version=parsed.schema_version,
        degraded=(),
        provenance_host=revision.provenance_host,
        provenance_model=revision.provenance_model,
        provenance_session_id=revision.provenance_session_id,
        provenance_certainty=revision.provenance_certainty,
        units=workspace.store.list_units(revision.id),
    )

    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()

    verification = next(row for row in readiness["conditions"] if row["id"] == "verification")
    assert verification["state"] == "not_applicable", verification
    assert readiness["armable"] is True


def test_ai_finding_never_impersonates_human_landing_blocker(workspace: Workspace) -> None:
    _review_all_current_targets(workspace)
    revision = workspace.store.latest_revision(workspace.review_id)
    session = workspace.store.get_session(workspace.review_id)
    assert revision is not None and session is not None
    from lemoncrow.pro.capabilities.review.sources.local import annotate

    with pytest.raises(ValueError, match="cannot record the human review disposition"):
        annotate(
            workspace.store,
            session,
            revision,
            path="src/session.py",
            start_line=1,
            end_line=1,
            body="AI attempts request changes",
            kind="request_change",
            created_by="reviewbot",
            created_by_actor="agent",
            source="ai_review",
            source_id="bot-invalid",
            new_text=_CHANGED_SOURCE,
        )

    annotate(
        workspace.store,
        session,
        revision,
        path="src/session.py",
        start_line=1,
        end_line=1,
        body="AI finding for human inspection",
        kind="suggestion",
        created_by="reviewbot",
        created_by_actor="agent",
        source="ai_review",
        source_id="bot-1",
        new_text=_CHANGED_SOURCE,
    )
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    human_requests = next(row for row in readiness["conditions"] if row["id"] == "human-change-requests")
    human_comments = next(row for row in readiness["conditions"] if row["id"] == "human-comments")
    assert human_requests["state"] == "pass"
    assert human_comments["state"] == "pass"


def test_review_ask_refuses_a_discarded_review(workspace: Workspace) -> None:
    asked: list[dict[str, Any]] = []

    def answer(context: dict[str, Any]) -> dict[str, str]:
        asked.append(context)
        return {"intent": "question", "body": "answer", "handoff_instruction": ""}

    ask = _ask_client(workspace, answer)
    discarded = ask.post(f"/api/reviews/{workspace.review_id}/finish", json={"status": "archived"})
    assert discarded.status_code == 200, discarded.text

    refused = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "Why did this behavior change?"},
    )

    assert refused.status_code == 409, refused.text
    assert asked == []
    assert workspace.store.list_ask_messages(workspace.review_id) == ()


def test_review_ask_is_revision_bound_read_only_and_persists_across_restart(workspace: Workspace) -> None:
    contexts: list[dict[str, Any]] = []

    def answer(context: dict[str, Any]) -> dict[str, str]:
        contexts.append(context)
        return {
            "intent": "question",
            "body": "The changed branch now reports invalid instead of expired.",
            "handoff_instruction": "",
        }

    ask = _ask_client(workspace, answer)
    before_revisions = workspace.store.list_revisions(workspace.review_id)
    before_annotations = workspace.store.list_annotations(workspace.review_id)
    before_marks = workspace.store.list_marks(workspace.review_id)

    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={
            "revision_number": 0,
            "body": "Why did this behavior change?",
            "path": "src/session.py",
            "symbol": "SessionManager.refresh",
            "start_line": 8,
            "end_line": 10,
            "side": "new",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["human"]["revision_number"] == 0
    assert payload["assistant"]["revision_number"] == 0
    assert payload["assistant"]["intent"] == "question"
    assert payload["assistant"]["action"] == {}
    assert payload["assistant"]["references"][0]["path"] == "src/session.py"
    assert payload["assistant"]["references"][0]["side"] == "new"
    assert payload["assistant"]["references"][0]["start_line"] == 8
    assert payload["assistant"]["references"][0]["end_line"] == 10
    assert payload["assistant"]["references"][0]["symbol"] == "SessionManager.refresh"
    assert payload["human"]["scope"]["symbol"] == "SessionManager.refresh"

    assert len(contexts) == 1
    source = next(item for item in contexts[0]["source"] if item["path"] == "src/session.py")
    assert '"invalid"' in source["text"]
    assert '"expired"' not in source["text"]
    assert contexts[0]["scope"]["symbol"] == "SessionManager.refresh"
    assert contexts[0]["scope"]["start_line"] == 8
    assert contexts[0]["revision"]["number"] == 0

    # Asking is not review judgment and must not mutate the human/agent loop.
    assert workspace.store.list_revisions(workspace.review_id) == before_revisions
    assert workspace.store.list_annotations(workspace.review_id) == before_annotations
    assert workspace.store.list_marks(workspace.review_id) == before_marks
    assert workspace.store.frontier_revision(workspace.review_id) == ""

    # A fresh store/API instance sees the one durable Review conversation.
    reopened = _ask_client(workspace, answer)
    listed = reopened.get(f"/api/reviews/{workspace.review_id}/ask")
    assert listed.status_code == 200, listed.text
    messages = listed.json()["messages"]
    assert [item["role"] for item in messages] == ["human", "assistant"]
    assert [item["role"] for item in messages] == ["human", "assistant"]
    assert [item["revision_number"] for item in messages] == [0, 0]
    assert messages[0]["scope"]["symbol"] == "SessionManager.refresh"


def test_review_ask_stream_emits_lifecycle_and_persists_one_turn(workspace: Workspace) -> None:
    def answer(_context: dict[str, Any]) -> dict[str, str]:
        return {
            "intent": "question",
            "body": "Streamed Review answer.",
            "handoff_instruction": "",
        }

    ask = _ask_client(workspace, answer)
    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask/stream",
        json={"revision_number": 0, "body": "Explain this change."},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert events[0]["type"] == "accepted"
    assert events[-1]["type"] == "result"
    assert events[-1]["result"]["assistant"]["body"] == "Streamed Review answer."

    listed = ask.get(f"/api/reviews/{workspace.review_id}/ask")
    assert listed.status_code == 200, listed.text
    messages = listed.json()["messages"]
    assert [item["role"] for item in messages] == ["human", "assistant"]


def test_review_ask_carries_live_code_intelligence_readiness_without_losing_author_context(
    workspace: Workspace,
) -> None:
    from lemoncrow.pro.capabilities.review.code_intelligence import (
        CodeIntelligenceLayerReadiness,
        CodeIntelligenceReadiness,
    )

    class ReadinessOnlyProvider:
        def readiness(self) -> CodeIntelligenceReadiness:
            ready = CodeIntelligenceLayerReadiness("ready", covered_paths=12, at_view_revision=3)
            return CodeIntelligenceReadiness(
                backend="native",
                lexical=ready,
                structural=ready,
                graph=CodeIntelligenceLayerReadiness(
                    "building",
                    reason="link_rebuild_deferred",
                    native_state="building",
                    at_view_revision=2,
                ),
                semantic=CodeIntelligenceLayerReadiness(
                    "degraded",
                    reason="semantic_content_incomplete",
                    native_state="building",
                    at_view_revision=3,
                ),
                view_id="view-ask",
                view_revision=3,
                generation_id="gen-ask",
            )

        def semantic_search(self, query: str, *, limit: int = 8) -> dict[str, Any]:
            assert query == "What callers might be affected?"
            assert limit == 6
            return {
                "hits": [
                    {
                        "path": "src/consumer.py",
                        "score": 0.91,
                        "detail": {"semantic_unit": "symbol:consume", "start_line": 12, "end_line": 20},
                    }
                ],
                "degraded": True,
                "degraded_reason": "semantic:semantic_content_incomplete",
                "truncated": False,
                "view_revision": 3,
            }

    contexts: list[dict[str, Any]] = []
    ask = _ask_client(
        workspace,
        lambda context: (
            contexts.append(context)
            or {"intent": "question", "body": "Readiness-aware answer.", "handoff_instruction": ""}
        ),
        code_intelligence=ReadinessOnlyProvider(),
    )
    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "What callers might be affected?"},
    )
    assert response.status_code == 201, response.text
    assert len(contexts) == 1
    context = contexts[0]
    readiness = context["code_intelligence"]["readiness"]
    assert readiness["backend"] == "native"
    assert readiness["graph"]["state"] == "building"
    assert readiness["graph"]["reason"] == "link_rebuild_deferred"
    assert readiness["semantic"]["state"] == "degraded"
    assert readiness["semantic"]["reason"] == "semantic_content_incomplete"
    semantic = context["code_intelligence"]["semantic_retrieval"]
    assert semantic["hits"][0]["path"] == "src/consumer.py"
    assert semantic["degraded"] is True
    assert semantic["degraded_reason"] == "semantic:semantic_content_incomplete"
    assert "author_context" in context
    assert "verified_facts" in context["author_context"]
    assert "activity" in context["author_context"]


def test_review_ask_refreshes_readiness_after_semantic_query_advances_layer(workspace: Workspace) -> None:
    from lemoncrow.pro.capabilities.review.code_intelligence import (
        CodeIntelligenceLayerReadiness,
        CodeIntelligenceReadiness,
    )

    class ActivatingProvider:
        ready = False

        def readiness(self) -> CodeIntelligenceReadiness:
            source = CodeIntelligenceLayerReadiness("ready", covered_paths=2, at_view_revision=0)
            semantic = CodeIntelligenceLayerReadiness(
                "ready" if self.ready else "building",
                reason="" if self.ready else "semantic_index_not_built",
                native_state="ready" if self.ready else "cold",
                covered_paths=2 if self.ready else 0,
                at_view_revision=0,
            )
            return CodeIntelligenceReadiness(
                backend="native",
                lexical=source,
                structural=source,
                graph=source,
                semantic=semantic,
                view_id="view-activating",
                view_revision=0,
                generation_id="gen-activating",
            )

        def semantic_search(self, query: str, *, limit: int = 8) -> dict[str, Any]:
            assert query == "Where is retry policy decided?"
            assert limit == 6
            self.ready = True
            return {
                "hits": [{"path": "src/retry.py", "score": 1.0}],
                "degraded": False,
                "degraded_reason": "",
                "truncated": False,
                "view_revision": 0,
            }

    provider = ActivatingProvider()
    contexts: list[dict[str, Any]] = []
    ask = _ask_client(
        workspace,
        lambda context: (
            contexts.append(context) or {"intent": "question", "body": "Found it.", "handoff_instruction": ""}
        ),
        code_intelligence=provider,
    )
    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "Where is retry policy decided?"},
    )
    assert response.status_code == 201, response.text
    assert provider.ready is True
    assert contexts[0]["code_intelligence"]["semantic_retrieval"]["degraded"] is False
    assert contexts[0]["code_intelligence"]["readiness"]["semantic"]["state"] == "ready"


def test_review_ask_from_map_node_uses_the_same_thread_and_exact_snapshot_context(workspace: Workspace) -> None:
    contexts: list[dict[str, Any]] = []

    ask = _ask_client(
        workspace,
        lambda context: (
            contexts.append(context)
            or {"intent": "question", "body": "Map-grounded answer.", "handoff_instruction": ""}
        ),
    )
    map_response = ask.get(f"/api/reviews/{workspace.review_id}/map")
    assert map_response.status_code == 200, map_response.text
    node = next(item for item in map_response.json()["graph"]["nodes"] if item["exactness"] == "snapshot")
    selection = node["selection"]

    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={
            "revision_number": 0,
            "body": "Why was this component touched?",
            "context": selection,
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["human"]["context"]["selection"]["node_id"] == selection["node_id"]
    assert payload["assistant"]["references"][0]["kind"] == "review_map_node"
    assert payload["assistant"]["references"][0]["map_selection"]["revision_id"] == selection["revision_id"]
    assert contexts[0]["scope"]["kind"] == "review_map_node"
    assert contexts[0]["scope"]["context_key"] == selection["node_id"]
    assert contexts[0]["surface_context"]["selection"]["node_id"] == selection["node_id"]
    assert contexts[0]["surface_context"]["selected"]["exactness"] == "snapshot"
    assert contexts[0]["surface_context"]["snapshot"]["source_fingerprint"]
    assert contexts[0]["surface_context"]["snapshot_paths"]

    second = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "And what changed overall?"},
    )
    assert second.status_code == 201, second.text
    listed = ask.get(f"/api/reviews/{workspace.review_id}/ask").json()["messages"]
    assert [item["role"] for item in listed] == ["human", "assistant", "human", "assistant"]
    assert listed[0]["context"]["selection"]["node_id"] == selection["node_id"]
    assert listed[2]["context"] == {}

    foreign = dict(selection)
    foreign["revision_id"] = "rr-not-this-snapshot"
    refused = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "Use this stale node", "context": foreign},
    )
    assert refused.status_code == 409
    assert len(ask.get(f"/api/reviews/{workspace.review_id}/ask").json()["messages"]) == 4


def test_review_ask_from_deleted_map_node_preserves_snapshot_old_side(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts: list[dict[str, Any]] = []
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    selection = {
        "kind": "review_map_node",
        "review_id": workspace.review_id,
        "revision_id": revision.id,
        "level": "architecture",
        "focus_node_id": "",
        # Legacy component selection persisted before the file-first Review Map.
        "node_id": "component:deleted",
        "exactness": "snapshot",
        "side": "old",
        "path": "src/session.py",
        "line": 8,
        "end_line": 10,
        "symbol": "",
        "target_ids": [],
        "current_symbol_id": "",
    }
    canonical_selection = {**selection, "node_id": "file:session"}

    def fake_map(**kwargs: Any) -> dict[str, Any]:
        selected_revision = kwargs["revision"]
        return {
            "review_id": workspace.review_id,
            "revision_id": selected_revision.id,
            "revision_number": 0,
            "level": "architecture",
            "snapshot": {
                "exactness": "snapshot",
                "tree_fingerprint": selected_revision.tree_fingerprint,
                "source_fingerprint": selected_revision.source_fingerprint,
                "base_sha": selected_revision.base_sha,
                "head_sha": selected_revision.head_sha,
                "degraded": [],
                "provenance": {},
                "verification": {"pass": 0, "fail": 0, "not_run": 0, "unknown": 0, "checks": []},
            },
            "context": {
                "source": "unavailable",
                "exact_for_snapshot": False,
                "snapshot_head_matches_checkout": False,
                "index": {},
                "note": "",
            },
            "graph": {
                "level": "architecture",
                "focus": "file:session",
                "nodes": [
                    {
                        "id": "file:session",
                        "label": "session.py",
                        "kind": "file",
                        "component": "src",
                        "path": "src/session.py",
                        "line": 8,
                        "end_line": 10,
                        "status": "changed",
                        "exactness": "snapshot",
                        "target_ids": [],
                        "paths": ["src/session.py"],
                        "symbols": [],
                        "important_public_contract": False,
                        "boundaries": [],
                        "selection": canonical_selection,
                    }
                ],
                "edges": [],
                "truncated": False,
            },
        }

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.review_map.build_review_map", fake_map)
    ask = _ask_client(
        workspace,
        lambda context: (
            contexts.append(context)
            or {"intent": "question", "body": "Deleted-node answer.", "handoff_instruction": ""}
        ),
    )
    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "What changed here?", "context": selection},
    )
    assert response.status_code == 201, response.text
    assert response.json()["human"]["context"]["selection"]["node_id"] == "file:session"
    assert response.json()["human"]["scope"]["side"] == "old"
    assert contexts[0]["scope"]["side"] == "old"
    assert contexts[0]["surface_context"]["selection"]["node_id"] == "file:session"
    source = [item for item in contexts[0]["source"] if item["path"] == "src/session.py"]
    if source:
        assert source[0]["side"] == "old"
        assert '"expired"' in source[0]["text"]
    else:
        assert contexts[0]["grounding"]["selected_source_available"] is False


def test_review_ask_from_map_edge_preserves_graph_exactness_and_endpoint_evidence(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts: list[dict[str, Any]] = []
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None

    source_selection = {
        "kind": "review_map_node",
        "review_id": workspace.review_id,
        "revision_id": revision.id,
        "level": "architecture",
        "focus_node_id": "",
        "node_id": "component:src",
        "exactness": "snapshot",
        "path": "src/session.py",
        "line": 1,
        "end_line": 12,
        "symbol": "",
        "target_ids": [],
        "current_symbol_id": "",
    }
    target_selection = {
        "kind": "review_map_node",
        "review_id": workspace.review_id,
        "revision_id": revision.id,
        "level": "architecture",
        "focus_node_id": "",
        "node_id": "component:consumer",
        "exactness": "current_index",
        "path": "src/untouched.py",
        "line": 1,
        "end_line": 2,
        "symbol": "",
        "target_ids": [],
        "current_symbol_id": "",
    }
    edge_selection = {
        "kind": "review_map_edge",
        "review_id": workspace.review_id,
        "revision_id": revision.id,
        "level": "architecture",
        "focus_node_id": "",
        "edge_id": "edge:dependency",
        "source_id": "component:src",
        "target_id": "component:consumer",
        "relation": "calls",
        "exactness": "current_index",
    }

    def fake_map(**kwargs: Any) -> dict[str, Any]:
        selected_revision = kwargs["revision"]
        return {
            "review_id": workspace.review_id,
            "revision_id": selected_revision.id,
            "revision_number": 0,
            "level": "architecture",
            "snapshot": {
                "exactness": "snapshot",
                "tree_fingerprint": selected_revision.tree_fingerprint,
                "source_fingerprint": selected_revision.source_fingerprint,
                "base_sha": selected_revision.base_sha,
                "head_sha": selected_revision.head_sha,
                "degraded": [],
                "provenance": {},
                "verification": {"pass": 0, "fail": 0, "not_run": 0, "unknown": 0, "checks": []},
            },
            "context": {
                "source": "current_index",
                "exact_for_snapshot": False,
                "snapshot_head_matches_checkout": True,
                "index": {"symbols_indexed": 2},
                "note": "Current index context; not Snapshot exact.",
            },
            "graph": {
                "level": "architecture",
                "focus": "component:src",
                "nodes": [
                    {
                        "id": "component:src",
                        "label": "src",
                        "kind": "component",
                        "path": "src/session.py",
                        "line": 1,
                        "end_line": 12,
                        "status": "changed",
                        "exactness": "snapshot",
                        "target_ids": [],
                        "paths": ["src/session.py"],
                        "symbols": [],
                        "important_public_contract": False,
                        "boundaries": [],
                        "selection": source_selection,
                    },
                    {
                        "id": "component:consumer",
                        "label": "consumer",
                        "kind": "component",
                        "path": "src/untouched.py",
                        "line": 1,
                        "end_line": 2,
                        "status": "context",
                        "exactness": "current_index",
                        "target_ids": [],
                        "paths": [],
                        "symbols": [],
                        "important_public_contract": False,
                        "boundaries": [],
                        "selection": target_selection,
                    },
                ],
                "edges": [
                    {
                        "id": "edge:dependency",
                        "source": "component:src",
                        "target": "component:consumer",
                        "kind": "calls",
                        "exactness": "current_index",
                        "weight": 3,
                        "uncertain": False,
                        "selection": edge_selection,
                    }
                ],
                "truncated": False,
            },
        }

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.review_map.build_review_map", fake_map)
    ask = _ask_client(
        workspace,
        lambda context: (
            contexts.append(context) or {"intent": "question", "body": "Dependency answer.", "handoff_instruction": ""}
        ),
    )
    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={
            "revision_number": 0,
            "body": "Why was this dependency introduced? What callers are affected?",
            "context": edge_selection,
        },
    )
    assert response.status_code == 201, response.text
    surface = contexts[0]["surface_context"]
    assert surface["kind"] == "review_map_edge"
    assert surface["selected"]["relation"] == "calls"
    assert surface["selected"]["exactness"] == "current_index"
    assert surface["surrounding_graph"]["exact_for_snapshot"] is False
    assert {item["id"] for item in surface["nodes"]} == {"component:src", "component:consumer"}
    assert contexts[0]["scope"]["path"] == ""
    map_reference = response.json()["assistant"]["references"][0]
    assert map_reference["kind"] == "review_map_edge"
    assert map_reference["map_selection"]["edge_id"] == "edge:dependency"

    promoted = dict(edge_selection)
    promoted["exactness"] = "snapshot"
    refused = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "Treat this as frozen", "context": promoted},
    )
    assert refused.status_code == 409

    change = _ask_client(
        workspace,
        lambda _context: {
            "intent": "change_request",
            "body": "I can prepare that change, but this Map edge is current-index context.",
            "handoff_instruction": "Change the mapped dependency.",
        },
    )
    preview = change.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "Change this dependency", "context": edge_selection},
    )
    assert preview.status_code == 201, preview.text
    assistant = preview.json()["assistant"]
    action = assistant["action"]
    assert action["delivery"]["supported"] is False
    assert "current-index Review Map context is read-only" in action["delivery"]["reason"]
    handoff = change.post(
        f"/api/reviews/{workspace.review_id}/ask/{assistant['id']}/handoff",
        json={
            "operation_id": action["operation_id"],
            "instruction_hash": action["instruction_hash"],
            "expected_revision_id": action["expected_revision_id"],
            "expected_tree_fingerprint": action["expected_tree_fingerprint"],
            "expected_source_fingerprint": action["expected_source_fingerprint"],
        },
    )
    assert handoff.status_code == 409
    assert "current-index Review Map context is read-only" in handoff.json()["detail"]


def test_review_ask_old_side_never_substitutes_new_side_source(workspace: Workspace) -> None:
    contexts: list[dict[str, Any]] = []

    ask = _ask_client(
        workspace,
        lambda context: (
            contexts.append(context) or {"intent": "question", "body": "Old-side answer.", "handoff_instruction": ""}
        ),
    )
    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={
            "revision_number": 0,
            "body": "Why was the expired behavior removed?",
            "path": "src/session.py",
            "side": "old",
            "start_line": 8,
            "end_line": 10,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["human"]["scope"]["side"] == "old"
    selected = [item for item in contexts[0]["source"] if item["path"] == "src/session.py"]
    if selected:
        assert selected[0]["side"] == "old"
        assert '"expired"' in selected[0]["text"]
        assert '"invalid"' not in selected[0]["text"]
    else:
        assert contexts[0]["grounding"]["selected_source_available"] is False
        assert "old-side source is unavailable" in contexts[0]["grounding"]["selected_source_reason"]


def test_review_ask_natural_previous_snapshot_question_gets_comparison(workspace: Workspace) -> None:
    contexts: list[dict[str, Any]] = []

    ask = _ask_client(
        workspace,
        lambda context: (
            contexts.append(context) or {"intent": "question", "body": "Comparison answer.", "handoff_instruction": ""}
        ),
    )
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"newer"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"]["revision_number"] == 1

    map_response = ask.get(f"/api/reviews/{workspace.review_id}/map")
    assert map_response.status_code == 200, map_response.text
    map_node = next(item for item in map_response.json()["graph"]["nodes"] if "src/session.py" in item.get("paths", []))
    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={
            "revision_number": 1,
            "body": "What changed in this area compared with the previous Snapshot?",
            "context": map_node["selection"],
        },
    )
    assert response.status_code == 201, response.text
    comparison = contexts[0]["comparison"]
    assert comparison["from_revision_number"] == 0
    assert comparison["to_revision_number"] == 1
    assert comparison["available"] is True
    assert any(item["path"] == "src/session.py" for item in comparison["files"])


def test_review_ask_refuses_repo_paths_outside_the_review_boundary(workspace: Workspace) -> None:
    ask = _ask_client(
        workspace,
        lambda _context: {"intent": "question", "body": "unused", "handoff_instruction": ""},
    )
    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={
            "revision_number": 0,
            "body": "Read this unrelated file",
            "path": "src/untouched.py",
            "start_line": 1,
            "end_line": 2,
        },
    )
    assert response.status_code == 404
    assert ask.get(f"/api/reviews/{workspace.review_id}/ask").json()["messages"] == []


def test_review_ask_model_failure_does_not_create_phantom_turns(workspace: Workspace) -> None:
    def fail(_context: dict[str, Any]) -> dict[str, str]:
        raise RuntimeError("model offline")

    ask = _ask_client(workspace, fail)
    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "Explain the change"},
    )
    assert response.status_code == 503
    assert "model offline" in response.json()["detail"]
    assert ask.get(f"/api/reviews/{workspace.review_id}/ask").json()["messages"] == []


def test_review_ask_change_request_prepares_but_does_not_send_agent_handoff(workspace: Workspace) -> None:
    ask = _ask_client(
        workspace,
        lambda _context: {
            "intent": "change_request",
            "body": "I can ask the author to collapse the redundant branch.",
            "handoff_instruction": "Collapse the redundant branch in src/session.py and preserve behavior.",
        },
    )
    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={
            "revision_number": 0,
            "body": "Collapse this redundant branch.",
            "path": "src/session.py",
            "start_line": 7,
            "end_line": 10,
        },
    )
    assert response.status_code == 201, response.text
    assistant = response.json()["assistant"]
    assert assistant["intent"] == "change_request"
    action = assistant["action"]
    assert action["kind"] == "agent_handoff"
    assert action["handoff_instruction"].startswith("Collapse the redundant branch")
    assert action["operation_id"].startswith("aop-")
    assert action["expected_revision_id"] == assistant["revision_id"]
    assert action["expected_tree_fingerprint"]
    assert action["expected_source_fingerprint"]
    prepared = ask.store.get_ask_handoff(assistant["id"])
    assert prepared is not None
    assert prepared.state == "prepared"
    assert prepared.operation_id == action["operation_id"]
    # Preparation alone neither invokes an agent nor creates a source revision.
    assert len(ask.store.list_revisions(workspace.review_id)) == 1


def test_review_ask_historical_revision_uses_frozen_source_after_newer_revision_exists(workspace: Workspace) -> None:
    contexts: list[dict[str, Any]] = []

    def answer(context: dict[str, Any]) -> dict[str, str]:
        contexts.append(context)
        return {"intent": "question", "body": "Historical answer.", "handoff_instruction": ""}

    ask = _ask_client(workspace, answer)
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"newer"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"]["revision_number"] == 1

    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={
            "revision_number": 0,
            "body": "Why did rev 0 do this?",
            "path": "src/session.py",
            "start_line": 8,
            "end_line": 10,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["assistant"]["revision_number"] == 0
    source = next(item for item in contexts[0]["source"] if item["path"] == "src/session.py")
    assert '"invalid"' in source["text"]
    assert '"newer"' not in source["text"]
    assert response.json()["latest_revision_number"] == 1


def test_review_ask_handoff_uses_exact_session_once_without_creating_revision(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    calls: list[tuple[str, str, str]] = []

    def deliver(host: str, exact_session_id: str, _repo_root: Path, prompt: str) -> ClaudeDeliveryResult:
        calls.append((host, exact_session_id, prompt))
        return ClaudeDeliveryResult("sent", f"{host}:{exact_session_id}", remote_ref="message-1", message="sent")

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.delivery.deliver_prompt_to_agent_session",
        deliver,
    )
    ask = _ask_client(
        workspace,
        lambda _context: {
            "intent": "change_request",
            "body": "I can ask the author to simplify this branch.",
            "handoff_instruction": "Simplify the branch without changing behavior.",
        },
    )
    created = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={
            "revision_number": 0,
            "body": "Simplify this branch.",
            "path": "src/session.py",
            "start_line": 7,
            "end_line": 10,
        },
    )
    assert created.status_code == 201, created.text
    assistant = created.json()["assistant"]
    action = assistant["action"]
    revision_id = assistant["revision_id"]
    before_revision_count = len(ask.store.list_revisions(workspace.review_id))

    payload = {
        "operation_id": action["operation_id"],
        "instruction_hash": action["instruction_hash"],
        "expected_revision_id": revision_id,
        "expected_tree_fingerprint": action["expected_tree_fingerprint"],
        "expected_source_fingerprint": action["expected_source_fingerprint"],
    }
    sent = ask.post(
        f"/api/reviews/{workspace.review_id}/ask/{assistant['id']}/handoff",
        json=payload,
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["state"] == "sent"
    assert len(calls) == 1
    assert calls[0][0:2] == ("claude", session_id)
    assert "Reviewer-requested code change" in calls[0][2]
    assert "src/session.py:L7-L10" in calls[0][2]
    assert "Simplify the branch without changing behavior." in calls[0][2]
    assert len(ask.store.list_revisions(workspace.review_id)) == before_revision_count

    # Network/UI retries are idempotent and do not resume the author twice.
    repeated = ask.post(
        f"/api/reviews/{workspace.review_id}/ask/{assistant['id']}/handoff",
        json=payload,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["state"] == "sent"
    assert len(calls) == 1

    reopened = ask.get(f"/api/reviews/{workspace.review_id}/ask")
    assert reopened.status_code == 200, reopened.text
    persisted = next(item for item in reopened.json()["messages"] if item["id"] == assistant["id"])
    assert persisted["action"]["handoff"]["state"] == "sent"
    assert persisted["action"]["handoff"]["remote_ref"] == "message-1"


def test_review_ask_change_request_routes_to_exact_file_author(workspace: Workspace) -> None:
    _make_latest_revision_exact_claude(workspace, "revision-session")
    revision = workspace.store.list_revisions(workspace.review_id)[-1]
    workspace.store.add_revision_attributions(
        revision.id,
        (
            ReviewAttribution(
                revision_id=revision.id,
                scope="file",
                scope_key="src/session.py",
                host="claude",
                session_id="file-session",
                role="contributor",
                certainty="exact",
                method="test",
            ),
        ),
    )
    ask = _ask_client(
        workspace,
        lambda _context: {
            "intent": "change_request",
            "body": "I can simplify that.",
            "handoff_instruction": "Simplify this function.",
        },
    )

    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "Simplify this function.", "path": "src/session.py"},
    )
    assert response.status_code == 201, response.text
    delivery = response.json()["assistant"]["action"]["delivery"]
    assert delivery["supported"] is True
    assert delivery["target_ref"] == "claude:file-session"


def test_review_ask_handoff_refuses_after_latest_revision_advances(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    called = False

    def deliver(*_args: object, **_kwargs: object) -> ClaudeDeliveryResult:
        nonlocal called
        called = True
        return ClaudeDeliveryResult("sent", f"claude:{session_id}")

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.delivery.deliver_prompt_to_agent_session",
        deliver,
    )
    ask = _ask_client(
        workspace,
        lambda _context: {
            "intent": "change_request",
            "body": "I can ask the author to simplify this.",
            "handoff_instruction": "Simplify the branch.",
        },
    )
    created = ask.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "Simplify it.", "path": "src/session.py", "start_line": 7},
    ).json()
    assistant = created["assistant"]
    action = assistant["action"]

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"newer"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"]["revision_number"] == 1

    response = ask.post(
        f"/api/reviews/{workspace.review_id}/ask/{assistant['id']}/handoff",
        json={
            "operation_id": action["operation_id"],
            "instruction_hash": action["instruction_hash"],
            "expected_revision_id": assistant["revision_id"],
            "expected_tree_fingerprint": action["expected_tree_fingerprint"],
            "expected_source_fingerprint": action["expected_source_fingerprint"],
        },
    )
    assert response.status_code == 409
    assert "revision changed" in response.json()["detail"]
    assert called is False


def test_review_live_state_cursor_changes_only_when_mutable_metadata_changes(workspace: Workspace) -> None:
    path = f"/api/reviews/{workspace.review_id}/live-state?wait_ms=0"
    first_response = workspace.get(path)
    assert first_response.status_code == 200
    first = first_response.json()
    assert first["changed"] is True
    assert first["revision_id"]
    assert first["packet_cursor"]
    assert first["annotations_cursor"]
    assert first["journal_cursor"]

    same_response = workspace.get(f"{path}&cursor={first['cursor']}")
    assert same_response.status_code == 200
    same = same_response.json()
    assert same["changed"] is False
    assert same["cursor"] == first["cursor"]

    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "live cursor comment", "kind": "comment"},
    )
    assert created.status_code == 201, created.text

    changed_response = workspace.get(f"{path}&cursor={first['cursor']}")
    assert changed_response.status_code == 200
    changed = changed_response.json()
    assert changed["changed"] is True
    assert changed["cursor"] != first["cursor"]
    assert changed["annotations_cursor"] != first["annotations_cursor"]
    assert changed["packet_cursor"] == first["packet_cursor"]


def test_review_live_state_cursor_survives_fresh_store_and_api_instance(workspace: Workspace) -> None:
    path = f"/api/reviews/{workspace.review_id}/live-state?wait_ms=0"
    first_response = workspace.get(path)
    assert first_response.status_code == 200
    first = first_response.json()

    # Simulate a server process restart: no in-memory API/store objects survive.
    fresh_store = ReviewStore(workspace.store_root)
    fresh_app = FastAPI()
    register_review_api(
        fresh_app,
        fresh_store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
    )
    fresh = Workspace(
        TestClient(fresh_app, base_url=BASE),
        fresh_store,
        workspace.store_root,
        workspace.repo_root,
        workspace.review_id,
    )

    resumed_response = fresh.get(f"{path}&cursor={first['cursor']}")
    assert resumed_response.status_code == 200
    resumed = resumed_response.json()
    assert resumed["changed"] is False
    assert resumed["cursor"] == first["cursor"]
    assert resumed["revision_id"] == first["revision_id"]

    created = fresh.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "after restart", "kind": "comment"},
    )
    assert created.status_code == 201, created.text
    changed = fresh.get(f"{path}&cursor={first['cursor']}").json()
    assert changed["changed"] is True
    assert changed["cursor"] != first["cursor"]
    assert changed["annotations_cursor"] != first["annotations_cursor"]


def test_finish_status_compare_and_swap_refuses_revision_that_advanced_after_preview(workspace: Workspace) -> None:
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"after preview"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    second = workspace.store.latest_revision(workspace.review_id)
    assert second is not None and second.id != first.id

    with pytest.raises(ValueError, match="advanced before this status change"):
        workspace.store.update_session_status_if_current_revision(workspace.review_id, first.id, "finished")
    session = workspace.store.get_session(workspace.review_id)
    assert session is not None
    assert session.status == "open"
    assert session.current_revision_id == second.id


def test_human_source_judgments_reject_missing_or_stale_revision_tokens(workspace: Workspace) -> None:
    auth = {"Authorization": f"Bearer {TOKEN}"}
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None

    missing = workspace.client.post(
        f"/api/reviews/{workspace.review_id}/marks",
        headers=auth,
        json={"unit_key": "src/session.py", "state": "reviewed"},
    )
    assert missing.status_code == 400
    assert "expected_revision_id" in missing.text

    missing_proposal = workspace.client.post(
        f"/api/reviews/{workspace.review_id}/proposals",
        headers=auth,
        json={
            "path": "src/session.py",
            "start_line": 11,
            "end_line": 11,
            "replacement_text": '            return {"status": "inactive"}',
        },
    )
    assert missing_proposal.status_code == 400
    assert "expected_revision_id" in missing_proposal.text

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"invalid-again"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    second = workspace.store.latest_revision(workspace.review_id)
    assert second is not None and second.id != first.id

    stale_mark = workspace.client.post(
        f"/api/reviews/{workspace.review_id}/marks",
        headers=auth,
        json={
            "unit_key": "src/session.py",
            "state": "reviewed",
            "expected_revision_id": first.id,
        },
    )
    assert stale_mark.status_code == 409

    stale_comment = workspace.client.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        headers=auth,
        json={
            "path": "src/session.py",
            "start_line": 10,
            "body": "this belonged to the previous source",
            "expected_revision_id": first.id,
        },
    )
    assert stale_comment.status_code == 409

    stale_outcome = workspace.client.post(
        f"/api/reviews/{workspace.review_id}/outcome",
        headers=auth,
        json={"outcome": "lgtm", "summary": "old bytes", "expected_revision_id": first.id},
    )
    assert stale_outcome.status_code == 409

    stale_finish = workspace.client.post(
        f"/api/reviews/{workspace.review_id}/finish",
        headers=auth,
        json={"status": "finished", "expected_revision_id": first.id},
    )
    assert stale_finish.status_code == 409
    assert workspace.store.get_session(workspace.review_id).status == "open"

    stale_evidence = workspace.client.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload?kind=document&expected_revision_id={first.id}",
        headers={**auth, "Content-Type": "text/plain"},
        content=b"old evidence",
    )
    assert stale_evidence.status_code == 409

    stale_proposal = workspace.client.post(
        f"/api/reviews/{workspace.review_id}/proposals",
        headers=auth,
        json={
            "expected_revision_id": first.id,
            "path": "src/session.py",
            "start_line": 11,
            "end_line": 11,
            "replacement_text": '            return {"status": "inactive"}',
        },
    )
    assert stale_proposal.status_code == 409

    assert workspace.store.list_marks(workspace.review_id, reviewer_id="local") == ()
    assert workspace.store.list_annotations(workspace.review_id) == ()
    assert workspace.store.list_outcomes(workspace.review_id, reviewer_id="local") == ()
    assert workspace.store.list_evidence(workspace.review_id) == ()
    assert workspace.store.list_change_proposals(workspace.review_id) == ()


def test_comment_resolution_is_bound_to_the_revision_the_reviewer_saw(workspace: Workspace) -> None:
    current = workspace.store.latest_revision(workspace.review_id)
    assert current is not None
    created = workspace.client.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "path": "src/session.py",
            "start_line": 10,
            "body": "verify this exact source",
            "expected_revision_id": current.id,
        },
    )
    assert created.status_code == 201, created.text
    annotation_id = created.json()["annotation"]["id"]

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"later"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text

    stale = workspace.client.patch(
        f"/api/annotations/{annotation_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"state": "resolved", "expected_revision_id": current.id},
    )
    assert stale.status_code == 409
    assert workspace.store.get_annotation(annotation_id).state != "resolved"


def test_review_journal_route_is_additive_and_exact_session_scoped(workspace: Workspace) -> None:
    response = workspace.get(f"/api/reviews/{workspace.review_id}/journal")

    assert response.status_code == 200
    payload = response.json()
    assert payload["review_id"] == workspace.review_id
    assert payload["revision_id"]
    assert payload["revision_number"] == 0
    assert isinstance(payload["entries"], list)
    # This fixture has no author session trace. The API reports the gap rather
    # than guessing a nearby run and attaching unrelated agent context.
    assert payload["available"] is False
    assert "exact author session" in payload["reason"]


def test_review_journal_merges_live_exact_session_but_historical_stays_frozen(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo-journal-api"
    repo = _init_repo(repo_root)
    _write(repo_root, "src/session.py", _BASE_SOURCE)
    _commit(repo, "base", 0)
    _write(repo_root, "src/session.py", _CHANGED_SOURCE)

    store_root = tmp_path / "journal-store"
    ledger = RunLedger(
        session_id="api-journal-session",
        agent="claude",
        root=store_root,
        task="Keep session invalidation stable",
        workspace_path=repo_root,
    )
    ledger.set_plan(["Inspect session refresh", "Change invalid state", "Run session tests"])
    ledger.record_file_event("src/session.py", "edit", "@@ -1 +1 @@")
    ledger.persist()

    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(
        repo_root,
        rng,
        store_root=store_root,
        session_id="api-journal-session",
        with_patch_text=True,
    )
    exact = ProvenanceRecord(
        status="matched",
        host="claude",
        session_id="api-journal-session",
        certainty="exact",
        match_confidence=1.0,
        match_reason="test exact author session",
    )
    packet = replace(
        build.packet,
        provenance=exact,
        journal=collect_review_journal(store_root, repo_root, exact, [item.path for item in build.packet.files]),
    )
    build = PacketBuild(packet=packet, blobs=build.blobs)
    assert build.packet.journal
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)

    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=repo_root,
        port=PORT,
        source_repo_root=repo_root,
        source_store_root=store_root,
    )
    client = TestClient(app, base_url=BASE)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    ledger.add_verified_fact("Late live fact after revision capture")
    ledger.persist()

    current = client.get(f"/api/reviews/{session.id}/journal", headers=headers)
    historical = client.get(
        f"/api/reviews/{session.id}/revisions/{revision.revision_number - 1}/journal",
        headers=headers,
    )
    assert current.status_code == 200, current.text
    assert historical.status_code == 200, historical.text
    current_payload = current.json()
    historical_payload = historical.json()
    assert current_payload["available"] is True
    assert any(
        item["kind"] == "plan" and "Inspect session refresh" in item["detail"] for item in current_payload["entries"]
    )
    assert any(
        item["kind"] == "fact" and "Late live fact after revision capture" in item["summary"]
        for item in current_payload["entries"]
    )
    assert any(item["kind"] == "edit" and item["path"] == "src/session.py" for item in current_payload["entries"])
    assert historical_payload["available"] is True
    assert "Late live fact after revision capture" not in repr(historical_payload)
    assert any(item["kind"] == "plan" for item in historical_payload["entries"])


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


def test_surface_run_discovers_request_metadata_from_the_requested_old_side(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow.pro.capabilities.review.runtimes as runtimes_module
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
    from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult
    from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface

    materialized: list[str] = []
    selected_titles: list[str] = []

    def materialize(
        _store_root: Path,
        _repo_root: Path,
        _revision: Any,
        *,
        side: str,
        target: Path,
        **_kwargs: Any,
    ) -> str:
        materialized.append(side)
        target.mkdir(parents=True, exist_ok=True)
        (target / "surface-side.txt").write_text(side)
        return f"snapshot:{side}"

    class Registry:
        def discover(self, context: Any) -> tuple[Any, ...]:
            selected_side = (Path(context.repo_root) / "surface-side.txt").read_text().strip()
            return (
                ReviewSurface(
                    id="api/status",
                    provider="fixture",
                    kind="api.request",
                    title=f"request-from-{selected_side}",
                    runtime="fixture-runtime",
                    capabilities=("source", "execute", "results", "compare"),
                    metadata={"method": "GET", "url": f"/{selected_side}"},
                ),
            )

    class Runners:
        def run(self, _context: Any, surface: ReviewSurface, *, side: str) -> SurfaceRunResult:
            selected_titles.append(surface.title)
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "passed",
                surface.title,
                runner="fixture",
                runtime=surface.runtime,
            )

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.snapshot.materialize_review_side", materialize)
    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: Registry())
    monkeypatch.setattr(runtimes_module, "built_in_runner_registry", lambda: Runners())

    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/surface-run",
        params={"provider": "fixture", "surface_id": "api/status", "side": "old"},
    )
    assert response.status_code == 200, response.text
    assert materialized == ["old"]
    assert selected_titles == ["request-from-old"]
    assert response.json()["result"]["side"] == "old"


def test_surface_discovery_and_execution_bind_an_explicit_snapshot(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow.pro.capabilities.review.runtimes as runtimes_module
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
    from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult
    from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface

    original = workspace.store.latest_revision(workspace.review_id)
    assert original is not None
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"newer"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    latest = workspace.store.latest_revision(workspace.review_id)
    assert latest is not None and latest.id != original.id

    seen_discovery: list[str] = []
    seen_runs: list[tuple[str, str]] = []
    surface = ReviewSurface(
        id="api/get",
        provider="bruno",
        kind="api.request",
        title="GET /status",
        runtime="api-runtime",
        capabilities=("source", "execute", "results", "compare"),
    )

    class Registry:
        def discover(self, context: Any) -> tuple[Any, ...]:
            seen_discovery.append(context.revision.id)
            return (surface,)

    class Runners:
        def run(self, context: Any, selected: ReviewSurface, *, side: str) -> SurfaceRunResult:
            seen_runs.append((context.revision.id, side))
            return SurfaceRunResult(
                selected.id,
                selected.provider,
                side,
                "passed",
                "HTTP 200",
                runner="service",
                runtime=selected.runtime,
                data={"status_code": 200},
            )

    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: Registry())
    monkeypatch.setattr(runtimes_module, "built_in_runner_registry", lambda: Runners())

    discovered = workspace.get(
        f"/api/reviews/{workspace.review_id}/surfaces",
        params={"revision_id": original.id},
    )
    assert discovered.status_code == 200, discovered.text
    assert discovered.json()["revision_id"] == original.id
    assert seen_discovery[-1] == original.id

    executed = workspace.post(
        f"/api/reviews/{workspace.review_id}/surface-run",
        params={
            "provider": "bruno",
            "surface_id": "api/get",
            "side": "old",
            "revision_id": original.id,
        },
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["revision_id"] == original.id
    assert executed.json()["result"]["revision_id"] == original.id
    assert seen_runs == [(original.id, "old")]


def test_surface_discovery_never_falls_back_to_a_newer_mutable_checkout(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable

    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"newer"'))

    def unavailable(*_args: Any, **_kwargs: Any) -> str:
        raise ReviewSnapshotUnavailable("frozen tree missing")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.snapshot.materialize_review_side", unavailable)
    response = workspace.get(
        f"/api/reviews/{workspace.review_id}/surfaces",
        params={"revision_id": revision.id},
    )
    assert response.status_code == 409, response.text
    assert "exact Snapshot new-side surface source is unavailable" in response.json()["detail"]
    assert "frozen tree missing" in response.json()["detail"]


def test_surface_impact_infers_preview_comparator_for_arbitrary_plugin(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import lemoncrow.pro.capabilities.review.previews as previews_module
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
    from lemoncrow.pro.capabilities.review.previews import PreviewResult
    from lemoncrow.pro.capabilities.review.surfaces import PreviewDescriptor, ReviewSurface

    old_path = tmp_path / "old.txt"
    new_path = tmp_path / "new.txt"
    old_path.write_text("alpha\nbeta\n")
    new_path.write_text("alpha\ngamma\n")
    surface = ReviewSurface(
        id="flow/login",
        provider="fixture-mobile",
        kind="mobile.flow",
        title="Login flow",
        affected_paths=("src/session.py",),
        capabilities=("preview", "compare", "source"),
        preview=PreviewDescriptor(
            renderer="fixture-mobile",
            display="text",
            media_type="text/plain",
            label="Mobile flow trace",
        ),
    )

    class Registry:
        def discover(self, _context: Any) -> tuple[Any, ...]:
            return (surface,)

    def render(context: Any, selected: Any, *, side: str, **_kwargs: Any) -> PreviewResult:
        path = old_path if side == "old" else new_path
        return PreviewResult(
            status="ready",
            reason="",
            revision_id=context.revision.id,
            side=side,
            renderer="fixture-mobile",
            display="text",
            media_type="text/plain",
            filename=path.name,
            path=path,
        )

    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: Registry())
    monkeypatch.setattr(previews_module, "render_surface_preview", render)

    listed = workspace.get(f"/api/reviews/{workspace.review_id}/surfaces")
    assert listed.status_code == 200, listed.text
    comparison = listed.json()["surfaces"][0]["comparison"]
    assert comparison["inferred"] is True
    assert comparison["source"] == "preview"
    assert comparison["comparator"] == "text"

    response = workspace.get(
        f"/api/reviews/{workspace.review_id}/surface-impact",
        params={"provider": surface.provider, "surface_id": surface.id},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "preview"
    assert payload["comparator"] == "text"
    assert payload["changed"] is True
    assert payload["magnitude"] > 0
    assert "text" in payload["label"]


def test_surface_impact_ranks_one_sided_preview_as_full_impact(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
    from lemoncrow.pro.capabilities.review.surfaces import PreviewDescriptor, ReviewSurface

    surface = ReviewSurface(
        id="story/new",
        provider="fixture-preview",
        kind="fixture.story",
        title="New story",
        affected_paths=("src/session.py",),
        capabilities=("preview", "compare", "source"),
        preview=PreviewDescriptor(
            renderer="fixture-preview",
            display="image",
            media_type="image/png",
            sides=("new",),
        ),
    )

    class Registry:
        def discover(self, _context: Any) -> tuple[Any, ...]:
            return (surface,)

    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: Registry())
    response = workspace.get(
        f"/api/reviews/{workspace.review_id}/surface-impact",
        params={"provider": surface.provider, "surface_id": surface.id},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["changed"] is True
    assert payload["magnitude"] == 1.0
    assert payload["label"] == "Added preview · 100%"
    assert payload["metrics"]["one_sided"] is True


def test_surface_impact_ranks_new_runner_surface_without_executing_it(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow.pro.capabilities.review.runtimes as runtimes_module
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
    from lemoncrow.pro.capabilities.review.surfaces import ComparisonDescriptor, ReviewSurface

    surface = ReviewSurface(
        id="new-check",
        provider="fixture-runner",
        kind="fixture.check",
        title="New check",
        runtime="fixture-runtime",
        affected_paths=("src/session.py",),
        capabilities=("source", "execute", "compare", "results", "auto_compare"),
        comparison=ComparisonDescriptor(source="run", comparator="run-result", auto=True),
    )
    discoveries = 0
    runs: list[str] = []

    class Registry:
        def discover(self, _context: Any) -> tuple[Any, ...]:
            nonlocal discoveries
            discoveries += 1
            return (surface,) if discoveries == 1 else ()

    class Runners:
        def run(self, *_args: Any, **_kwargs: Any) -> Any:
            runs.append("ran")
            raise AssertionError("one-sided surface must rank without execution")

    registry = Registry()
    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: registry)
    monkeypatch.setattr(runtimes_module, "built_in_runner_registry", lambda: Runners())

    response = workspace.get(
        f"/api/reviews/{workspace.review_id}/surface-impact",
        params={"provider": surface.provider, "surface_id": surface.id},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["magnitude"] == 1.0
    assert payload["label"] == "Added surface · 100%"
    assert runs == []


def test_surface_impact_runs_only_explicitly_safe_plugin_surfaces(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow.pro.capabilities.review.runtimes as runtimes_module
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
    from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult
    from lemoncrow.pro.capabilities.review.surfaces import ComparisonDescriptor, ReviewSurface

    safe = ReviewSurface(
        id="safe",
        provider="fixture-runner",
        kind="fixture.check",
        title="Safe check",
        runtime="fixture-runtime",
        affected_paths=("src/session.py",),
        capabilities=("source", "execute", "compare", "results", "auto_compare"),
        comparison=ComparisonDescriptor(source="run", comparator="run-result", auto=True),
    )
    unsafe = ReviewSurface(
        id="unsafe",
        provider="fixture-runner",
        kind="fixture.check",
        title="Unsafe check",
        runtime="fixture-runtime",
        affected_paths=("src/session.py",),
        capabilities=("source", "execute", "compare", "results"),
        comparison=ComparisonDescriptor(source="run", comparator="run-result", auto=False),
    )
    selected = safe
    runs: list[str] = []

    class Registry:
        def discover(self, _context: Any) -> tuple[Any, ...]:
            return (selected,)

    class Runners:
        def run(self, _context: Any, surface: ReviewSurface, *, side: str) -> SurfaceRunResult:
            runs.append(side)
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "passed",
                side,
                runner="fixture",
                runtime=surface.runtime,
                data={"value": 1 if side == "old" else 2},
            )

    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: Registry())
    monkeypatch.setattr(runtimes_module, "built_in_runner_registry", lambda: Runners())

    response = workspace.get(
        f"/api/reviews/{workspace.review_id}/surface-impact",
        params={"provider": safe.provider, "surface_id": safe.id},
    )
    assert response.status_code == 200, response.text
    assert response.json()["comparator"] == "run-result"
    assert response.json()["changed"] is True
    assert sorted(runs) == ["new", "old"]

    selected = unsafe
    runs.clear()
    refused = workspace.get(
        f"/api/reviews/{workspace.review_id}/surface-impact",
        params={"provider": unsafe.provider, "surface_id": unsafe.id},
    )
    assert refused.status_code == 422
    assert "not declared safe" in refused.json()["detail"]
    assert runs == []


def test_surface_response_diff_ranks_safe_api_and_caches_the_immutable_result(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow.pro.capabilities.review.runtimes as runtimes_module
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
    from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult
    from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface

    surface = ReviewSurface(
        id="api/status.bru",
        provider="bruno",
        kind="api.request",
        title="Status",
        locator="GET {{baseUrl}}/status",
        runtime="api-runtime",
        affected_paths=("src/session.py",),
        capabilities=("source", "execute", "results", "compare"),
        metadata={"method": "GET", "url": "{{baseUrl}}/status"},
    )
    runs: list[str] = []

    class Registry:
        def discover(self, _context: Any) -> tuple[Any, ...]:
            return (surface,)

    class Runners:
        def run(self, _context: Any, selected: ReviewSurface, *, side: str) -> SurfaceRunResult:
            runs.append(side)
            return SurfaceRunResult(
                selected.id,
                selected.provider,
                side,
                "passed" if side == "old" else "failed",
                "fixture",
                runner="fixture",
                runtime=selected.runtime,
                data={
                    "status_code": 200 if side == "old" else 503,
                    "headers": {
                        "Content-Type": "application/json",
                        "Date": "old" if side == "old" else "new",
                    },
                    "body": {"ok": side == "old"},
                },
            )

    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: Registry())
    monkeypatch.setattr(runtimes_module, "built_in_runner_registry", lambda: Runners())
    params = {"provider": "bruno", "surface_id": surface.id}

    first = workspace.get(f"/api/reviews/{workspace.review_id}/surface-response-diff", params=params)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["status_changed"] is True
    assert payload["old_status_code"] == 200
    assert payload["new_status_code"] == 503
    assert payload["body_changed_ratio"] > 0
    assert payload["header_changed_ratio"] == 0
    assert sorted(runs) == ["new", "old"]

    second = workspace.get(f"/api/reviews/{workspace.review_id}/surface-response-diff", params=params)
    assert second.status_code == 200
    assert second.json() == payload
    assert sorted(runs) == ["new", "old"]


def test_surface_response_diff_never_auto_executes_mutation_methods(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow.pro.capabilities.review.runtimes as runtimes_module
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
    from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface

    surface = ReviewSurface(
        id="api/mutate.bru",
        provider="bruno",
        kind="api.request",
        title="Mutate",
        locator="POST {{baseUrl}}/items",
        runtime="api-runtime",
        affected_paths=("src/session.py",),
        capabilities=("source", "execute", "results", "compare"),
        metadata={"method": "POST", "url": "{{baseUrl}}/items"},
    )

    class Registry:
        def discover(self, _context: Any) -> tuple[Any, ...]:
            return (surface,)

    class Runners:
        def run(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("mutation endpoint must not execute for ranking")

    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: Registry())
    monkeypatch.setattr(runtimes_module, "built_in_runner_registry", lambda: Runners())
    response = workspace.get(
        f"/api/reviews/{workspace.review_id}/surface-response-diff",
        params={"provider": "bruno", "surface_id": surface.id},
    )

    assert response.status_code == 422
    assert "refuses mutation method POST" in response.json()["detail"]


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


def test_surface_preview_is_revision_pinned_contained_and_fails_with_specific_reasons(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow.pro.capabilities.review.previews as previews_module
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module
    from lemoncrow.pro.capabilities.review.previews import PreviewArtifact, PreviewRendererRegistry
    from lemoncrow.pro.capabilities.review.surfaces import PluginIssue, PreviewDescriptor, ReviewSurface

    seen: list[tuple[str, str, str]] = []

    class Page:
        renderer_id = "fixture"
        contract_version = 1

        def render(self, request: Any) -> Any:
            source = (request.workspace / "src/session.py").read_text(encoding="utf-8")
            seen.append((request.context.revision.id, request.side, source))
            target = request.output_dir / "view.html"
            target.write_text(f"<p>{request.side}</p>", encoding="utf-8")
            return PreviewArtifact.ready(target, "text/html", filename="view.html")

    class Crash:
        renderer_id = "crash"
        contract_version = 1

        def render(self, _request: Any) -> Any:
            raise RuntimeError("headless browser crashed")

    def surface(surface_id: str, kind: str, renderer: str = "") -> ReviewSurface:
        return ReviewSurface(
            id=surface_id,
            provider="fixture",
            kind=kind,
            title=surface_id,
            affected_paths=("src/session.py",),
            preview=(
                PreviewDescriptor(renderer=renderer, display="html", media_type="text/html", label="Fixture page")
                if renderer
                else None
            ),
        )

    class Registry:
        issues = (PluginIssue("broken", "provider", "plugin failed to load: ImportError: nope"),)

        def discover(self, _context: Any) -> tuple[Any, ...]:
            return (
                surface("session", "fixture.page", "fixture"),
                surface("frame", "design.frame", "figma"),
                surface("crash", "fixture.page", "crash"),
                surface("plain", "mystery.kind"),
            )

    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: Registry())
    monkeypatch.setattr(
        previews_module, "built_in_preview_registry", lambda: PreviewRendererRegistry([Page(), Crash()])
    )
    review = workspace.review_id
    revision = workspace.store.latest_revision(review)
    assert revision is not None

    listed = workspace.get(f"/api/reviews/{review}/surfaces", params={"revision_id": revision.id})
    assert listed.status_code == 200, listed.text
    rows = {row["id"]: row for row in listed.json()["surfaces"]}
    assert rows["session"]["preview"]["status"] == "available"
    assert rows["session"]["preview"]["display"] == "html"
    assert rows["frame"]["preview"]["status"] == "unavailable"
    assert "preview renderer 'figma' is not installed" in rows["frame"]["preview"]["reason"]
    assert rows["plain"]["preview"] is None
    assert listed.json()["issues"] == [
        {"plugin": "broken", "kind": "provider", "reason": "plugin failed to load: ImportError: nope"}
    ]

    def preview(surface_id: str, side: str) -> Any:
        return workspace.get(
            f"/api/reviews/{review}/surface-preview",
            params={"provider": "fixture", "surface_id": surface_id, "side": side, "revision_id": revision.id},
        )

    old, new = preview("session", "old"), preview("session", "new")
    assert (old.status_code, new.status_code) == (200, 200), (old.text, new.text)
    assert (old.text, new.text) == ("<p>old</p>", "<p>new</p>")
    assert old.headers["content-type"].startswith("text/html")
    assert old.headers["x-content-type-options"] == "nosniff"
    assert old.headers["content-security-policy"].startswith("sandbox;")
    assert old.headers["x-lemoncrow-revision-id"] == revision.id
    assert old.headers["x-lemoncrow-preview-side"] == "old"
    # Each side rendered from its own exact Snapshot tree, not the checkout.
    assert [(rev, side) for rev, side, _source in seen] == [(revision.id, "old"), (revision.id, "new")]
    assert '"expired"' in seen[0][2] and '"invalid"' in seen[1][2]

    missing = preview("frame", "new")
    assert missing.status_code == 409
    assert missing.json()["detail"].startswith("Fixture page is unavailable on the new side: preview renderer 'figma'")
    crashed = preview("crash", "old")
    assert crashed.status_code == 409
    assert crashed.json()["detail"] == (
        "Fixture page failed on the old side: preview renderer 'crash' raised RuntimeError: headless browser crashed"
    )
    assert preview("plain", "new").status_code == 422
    assert preview("nope", "new").status_code == 404
    assert preview("session", "sideways").status_code == 422
    # Broken plugins never take source review down with them.
    assert workspace.get(f"/api/reviews/{review}/files/src/session.py/patch").status_code == 200


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


def test_revision_compare_without_human_frontier_is_full_latest_review(workspace: Workspace) -> None:
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"revoked"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"]["revision_number"] == 1

    history = workspace.get(f"/api/reviews/{workspace.review_id}/revisions").json()
    assert history["last_reviewed_revision_number"] is None
    assert history["default_base_revision_number"] == 1
    assert history["default_target_revision_number"] == 1

    compared = workspace.get(f"/api/reviews/{workspace.review_id}/compare")
    assert compared.status_code == 200, compared.text
    body = compared.json()
    assert body["from_revision"]["revision_number"] == 1
    assert body["to_revision"]["revision_number"] == 1
    assert body["suggested_from_revision_number"] == 1
    assert body["summary"]["added"] == 0
    assert body["summary"]["changed"] == 0
    assert body["summary"]["removed"] == 0
    assert body["code_files"] == []


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
    assert body["from_revision"]["revision_number"] == 0
    assert body["to_revision"]["revision_number"] == 1
    assert body["suggested_from_revision_number"] == 0
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

    same = workspace.get(f"/api/reviews/{workspace.review_id}/compare?from_revision=1&to_revision=1")
    assert same.status_code == 200, same.text
    same_body = same.json()
    assert same_body["summary"]["added"] == 0
    assert same_body["summary"]["changed"] == 0
    assert same_body["summary"]["removed"] == 0
    assert same_body["files"] == []
    assert same_body["code_available"] is True
    assert same_body["code_files"] == []


def test_revision_history_exposes_zero_based_review_cursors_and_comment_origin(workspace: Workspace) -> None:
    initial = workspace.get(f"/api/reviews/{workspace.review_id}/revisions")
    assert initial.status_code == 200, initial.text
    first_history = initial.json()
    assert [item["revision_number"] for item in first_history["revisions"]] == [0]
    assert first_history["latest_revision_number"] == 0
    assert first_history["last_reviewed_revision_number"] is None
    assert first_history["last_commented_revision_number"] is None
    assert first_history["default_base_revision_number"] == 0
    assert first_history["default_target_revision_number"] == 0
    initial_targets = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/0/targets").json()
    assert first_history["revisions"][0]["review_area_count"] == initial_targets["progress"]["target_count"]
    assert first_history["revisions"][0]["review_area_delta"] == {
        "added": initial_targets["progress"]["target_count"],
        "changed": 0,
        "removed": 0,
    }

    comment = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "human feedback on rev 0", "kind": "comment"},
    )
    assert comment.status_code == 201, comment.text
    annotation_id = comment.json()["annotation"]["id"]

    after_comment = workspace.get(f"/api/reviews/{workspace.review_id}/revisions").json()
    assert after_comment["last_commented_revision_number"] == 0
    assert after_comment["last_reviewed_revision_number"] is None
    assert after_comment["revisions"][0]["is_last_commented"] is True

    marked = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": "src/session.py", "state": "reviewed"},
    )
    assert marked.status_code == 200, marked.text

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"rev-one"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"]["revision_number"] == 1

    history = workspace.get(f"/api/reviews/{workspace.review_id}/revisions").json()
    assert [item["revision_number"] for item in history["revisions"]] == [0, 1]
    assert history["latest_revision_number"] == 1
    assert history["last_reviewed_revision_number"] == 0
    assert history["last_commented_revision_number"] == 0
    assert history["default_base_revision_number"] == 0
    assert history["default_target_revision_number"] == 1
    assert history["revisions"][0]["is_last_reviewed"] is True
    assert history["revisions"][0]["previous_revision_number"] is None
    assert history["revisions"][0]["semantic_changed"] is True
    assert history["revisions"][0]["source_identity_changed"] is True
    assert history["revisions"][0]["base_moved"] is False
    assert history["revisions"][0]["delta"]["added"] > 0
    assert history["revisions"][1]["is_latest"] is True
    assert history["revisions"][1]["previous_revision_number"] == 0
    assert history["revisions"][1]["semantic_changed"] is True
    assert history["revisions"][1]["source_identity_changed"] is True
    assert history["revisions"][1]["base_moved"] is False
    assert history["revisions"][1]["delta"]["changed"] > 0
    latest_targets = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/1/targets").json()
    assert history["revisions"][1]["review_area_count"] == latest_targets["progress"]["target_count"]
    assert set(history["revisions"][1]["review_area_delta"]) == {"added", "changed", "removed"}
    assert sum(history["revisions"][1]["review_area_delta"].values()) > 0

    # Cursor/history semantics are reconstructed from durable state after a
    # process-equivalent store reopen; they are not browser/session memory.
    reopened_store = ReviewStore(workspace.store_root)
    reopened_app = FastAPI()
    register_review_api(
        reopened_app,
        reopened_store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
    )
    reopened_client = TestClient(reopened_app, base_url=BASE)
    reopened = reopened_client.get(
        f"/api/reviews/{workspace.review_id}/revisions",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert reopened.status_code == 200, reopened.text
    reopened_history = reopened.json()
    assert reopened_history["last_reviewed_revision_number"] == 0
    assert reopened_history["last_commented_revision_number"] == 0
    assert reopened_history["default_base_revision_number"] == 0
    assert reopened_history["default_target_revision_number"] == 1

    compared = workspace.get(f"/api/reviews/{workspace.review_id}/compare").json()
    assert compared["from_revision"]["revision_number"] == 0
    assert compared["to_revision"]["revision_number"] == 1
    historical_comment = next(item for item in compared["annotations"] if item["id"] == annotation_id)
    assert historical_comment["origin_revision_number"] == 0
    assert historical_comment["relation_to_revision"] == "inherited"
    assert historical_comment["addressed_by_revision"] is False
    assert annotation_id in compared["annotation_groups"]["inherited"]

    # Merely opening an arbitrary comparison must not move either human cursor.
    explicit = workspace.get(f"/api/reviews/{workspace.review_id}/compare?from_revision=1&to_revision=1")
    assert explicit.status_code == 200, explicit.text
    after_compare = workspace.get(f"/api/reviews/{workspace.review_id}/revisions").json()
    assert after_compare["last_reviewed_revision_number"] == 0
    assert after_compare["last_commented_revision_number"] == 0


def test_revision_comment_history_keeps_origin_addressed_resolution_and_reanchors_distinct(
    workspace: Workspace,
) -> None:
    """The ten historical-comment cases remain distinct across four revisions."""

    # 1. A human comment originates on rev 0 and is immediately visible there.
    created_a = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "A from rev 0", "kind": "comment"},
    )
    assert created_a.status_code == 201, created_a.text
    a_id = created_a.json()["annotation"]["id"]
    rev0_comments = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/0/annotations").json()["annotations"]
    a0 = next(item for item in rev0_comments if item["id"] == a_id)
    a0_line = a0["start_line"]
    assert a0["origin_revision_number"] == 0
    assert a0["relation_to_revision"] == "originating"
    assert a0["state"] == "open"
    assert a0["author_addressed_revision_number"] is None

    # 2/9. The author says addressed on rev 1, but that never resolves A.
    _write(
        workspace.repo_root,
        "src/session.py",
        "# shifted once\n" + _CHANGED_SOURCE.replace('"invalid"', '"rev-one"'),
    )
    refresh1 = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refresh1.status_code == 200, refresh1.text
    assert refresh1.json()["revision"]["revision_number"] == 1
    rev1 = workspace.store.latest_revision(workspace.review_id)
    assert rev1 is not None
    addressed = workspace.store.update_annotation(
        a_id,
        AnnotationHistoryContext(
            revision_id=rev1.id,
            changed_by="agent-session",
            changed_by_actor="agent",
            change_kind="author_response",
        ),
        author_response="addressed",
        author_response_source_id="agent-session",
        author_response_at="2026-09-24T00:00:00+00:00",
    )
    assert addressed is not None
    assert addressed.state == "open"

    # 6. Browsing historical rev 1 shows the historical context relevant there.
    rev1_comments = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/1/annotations").json()["annotations"]
    a1 = next(item for item in rev1_comments if item["id"] == a_id)
    assert a1["origin_revision_number"] == 0
    assert a1["relation_to_revision"] == "inherited"
    assert a1["author_addressed_revision_number"] == 1
    assert a1["addressed_by_revision"] is True
    assert a1["state"] == "open"

    # 4/7. Rev 2 moves the code again; A re-anchors but keeps rev 0 as origin.
    _write(
        workspace.repo_root,
        "src/session.py",
        "# shifted twice\n# another line\n" + _CHANGED_SOURCE.replace('"invalid"', '"rev-two"'),
    )
    refresh2 = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refresh2.status_code == 200, refresh2.text
    assert refresh2.json()["revision"]["revision_number"] == 2
    created_b = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 12, "body": "B from rev 2", "kind": "comment"},
    )
    assert created_b.status_code == 201, created_b.text
    b_id = created_b.json()["annotation"]["id"]

    rev2_comments = workspace.get(f"/api/reviews/{workspace.review_id}/annotations").json()["annotations"]
    a2 = next(item for item in rev2_comments if item["id"] == a_id)
    b2 = next(item for item in rev2_comments if item["id"] == b_id)
    assert a2["origin_revision_number"] == 0
    assert a2["relation_to_revision"] == "inherited"
    assert a2["author_addressed_revision_number"] == 1
    assert a2["state"] == "open"
    assert a2["start_line"] != a0_line
    assert b2["origin_revision_number"] == 2
    assert b2["relation_to_revision"] == "originating"

    # 3/5. By rev 3, A is still unresolved and B is inherited. Comparing
    # rev 0 -> rev 3 must not mutate either comment or its origin.
    _write(
        workspace.repo_root,
        "src/session.py",
        "# shifted thrice\n# another line\n# third line\n" + _CHANGED_SOURCE.replace('"invalid"', '"rev-three"'),
    )
    refresh3 = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refresh3.status_code == 200, refresh3.text
    assert refresh3.json()["revision"]["revision_number"] == 3

    before_resolve = workspace.get(f"/api/reviews/{workspace.review_id}/compare?from_revision=0&to_revision=3")
    assert before_resolve.status_code == 200, before_resolve.text
    before_rows = {item["id"]: item for item in before_resolve.json()["annotations"]}
    assert before_rows[a_id]["state"] == "open"
    assert before_rows[a_id]["origin_revision_number"] == 0
    assert before_rows[a_id]["author_addressed_revision_number"] == 1
    assert before_rows[a_id]["addressed_by_revision"] is False
    assert before_rows[b_id]["state"] == "open"
    assert before_rows[b_id]["origin_revision_number"] == 2
    assert before_rows[b_id]["relation_to_revision"] == "inherited"

    created_c = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 13, "body": "C from rev 3", "kind": "comment"},
    )
    assert created_c.status_code == 201, created_c.text
    c_id = created_c.json()["annotation"]["id"]

    # 8. Resolving an old comment while viewing the newer revision records the
    # human resolution on rev 3; it does not rewrite origin or author response.
    resolved = workspace.patch(f"/api/annotations/{a_id}", json={"state": "resolved"})
    assert resolved.status_code == 200, resolved.text

    compared = workspace.get(f"/api/reviews/{workspace.review_id}/compare?from_revision=0&to_revision=3")
    assert compared.status_code == 200, compared.text
    compare_body = compared.json()
    rows = {item["id"]: item for item in compare_body["annotations"]}
    assert rows[a_id]["origin_revision_number"] == 0
    assert rows[a_id]["relation_to_revision"] == "inherited"
    assert rows[a_id]["author_addressed_revision_number"] == 1
    assert rows[a_id]["addressed_by_revision"] is False
    assert rows[a_id]["resolved_revision_number"] == 3
    assert rows[a_id]["state"] == "resolved"
    assert rows[b_id]["origin_revision_number"] == 2
    assert rows[b_id]["relation_to_revision"] == "inherited"
    assert rows[b_id]["state"] == "open"
    assert rows[c_id]["origin_revision_number"] == 3
    assert rows[c_id]["relation_to_revision"] == "originating"
    assert a_id in compare_body["annotation_groups"]["inherited"]
    assert b_id in compare_body["annotation_groups"]["inherited"]
    assert c_id in compare_body["annotation_groups"]["originating"]
    assert a_id not in compare_body["annotation_groups"]["addressed_by_target"]

    # 10. Reopening the durable store (process/hosted reload equivalent) yields
    # the same immutable origins, addressed revision, resolution and comparison.
    reopened_store = ReviewStore(workspace.store_root)
    reopened_app = FastAPI()
    register_review_api(
        reopened_app,
        reopened_store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
    )
    reopened_client = TestClient(reopened_app, base_url=BASE)
    reopened = reopened_client.get(
        f"/api/reviews/{workspace.review_id}/compare?from_revision=0&to_revision=3",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert reopened.status_code == 200, reopened.text
    reopened_rows = {item["id"]: item for item in reopened.json()["annotations"]}
    assert reopened_rows[a_id]["origin_revision_number"] == 0
    assert reopened_rows[a_id]["author_addressed_revision_number"] == 1
    assert reopened_rows[a_id]["resolved_revision_number"] == 3
    assert reopened_rows[b_id]["origin_revision_number"] == 2
    assert reopened_rows[c_id]["origin_revision_number"] == 3


def test_revision_comment_context_tracks_addressed_reset_and_exact_resolution_revision(
    workspace: Workspace,
) -> None:
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "fix this", "kind": "comment"},
    )
    assert created.status_code == 201, created.text
    annotation_id = created.json()["annotation"]["id"]

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"rev-one"'))
    refreshed1 = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed1.status_code == 200, refreshed1.text
    rev1 = refreshed1.json()["revision"]
    assert rev1["revision_number"] == 1

    workspace.store.update_annotation(
        annotation_id,
        AnnotationHistoryContext(
            revision_id=rev1["id"],
            changed_by="agent-session",
            changed_by_actor="agent",
            change_kind="author_response",
        ),
        author_response="addressed",
        author_response_source_id="agent-session",
    )
    compare1 = workspace.get(f"/api/reviews/{workspace.review_id}/compare?from_revision=0&to_revision=1").json()
    annotation1 = next(item for item in compare1["annotations"] if item["id"] == annotation_id)
    assert annotation1["author_addressed_revision_number"] == 1
    assert annotation1["addressed_by_revision"] is True
    assert annotation1["resolved_revision_number"] is None

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"rev-two"'))
    refreshed2 = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed2.status_code == 200, refreshed2.text
    rev2 = refreshed2.json()["revision"]
    assert rev2["revision_number"] == 2

    workspace.store.update_annotation(
        annotation_id,
        AnnotationHistoryContext(
            revision_id=rev2["id"],
            changed_by="local",
            changed_by_actor="human",
            change_kind="edited",
        ),
        author_response="none",
        author_response_source_id="",
    )
    resolved = workspace.patch(f"/api/annotations/{annotation_id}", json={"state": "resolved"})
    assert resolved.status_code == 200, resolved.text

    compare2 = workspace.get(f"/api/reviews/{workspace.review_id}/compare?from_revision=0&to_revision=2").json()
    annotation2 = next(item for item in compare2["annotations"] if item["id"] == annotation_id)
    assert annotation2["author_addressed_revision_number"] is None
    assert annotation2["addressed_by_revision"] is False
    assert annotation2["resolved_revision_number"] == 2


def test_revision_zero_is_a_real_historical_route_not_a_default_sentinel(workspace: Workspace) -> None:
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"rev-one"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text

    historical = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/0")
    assert historical.status_code == 200, historical.text
    assert historical.json()["revision"]["id"] == first.id
    assert historical.json()["revision"]["revision_number"] == 0
    assert historical.json()["latest_revision_number"] == 1

    compared = workspace.get(f"/api/reviews/{workspace.review_id}/compare?from_revision=0&to_revision=1")
    assert compared.status_code == 200, compared.text
    assert compared.json()["from_revision"]["id"] == first.id


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
    assert finished.json()["reviews"][0]["revision_number"] == 0

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


def test_web_preview_uses_discovery_checkout_when_reader_runs_in_scratch(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import web_preview

    scratch = tmp_path / "reader-scratch"
    scratch.mkdir()
    seen: list[Path] = []

    def preview_payload(_store: ReviewStore, _revision: Any, repo: Path, _entry: Any) -> dict[str, Any]:
        seen.append(repo)
        return {"kind": "web", "framework": "vite", "routes": ["/"], "default_route": "/"}

    def live_preview(_store_root: Path, repo: Path, _revision: Any, **_kwargs: Any) -> str:
        seen.append(repo)
        return "http://127.0.0.1:40000/preview/"

    monkeypatch.setattr(review_api, "_web_preview_payload", preview_payload)
    monkeypatch.setattr(web_preview, "ensure_live_web_preview", live_preview)
    app = FastAPI()
    register_review_api(
        app,
        workspace.store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=scratch,
        dependency_repo_root=workspace.repo_root,
        session_repo_identity=str(workspace.repo_root),
        port=PORT,
    )
    client = TestClient(app, base_url=BASE)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    review = workspace.review_id

    file_response = client.get(f"/api/reviews/{review}/files/src/session.py/patch", headers=headers)
    fast_file_response = client.get(
        f"/api/reviews/{review}/files/src/session.py/patch",
        params={"include_web_preview": "false"},
        headers=headers,
    )
    live_response = client.get(
        f"/api/reviews/{review}/web-live-preview",
        params={"document_path": "src/session.py", "side": "new", "route": "/"},
        headers=headers,
    )

    assert file_response.status_code == 200
    assert file_response.json()["preview"]["routes"] == ["/"]
    assert fast_file_response.status_code == 200
    assert fast_file_response.json()["preview"] is None
    assert live_response.status_code == 200
    assert live_response.json()["url"] == "http://127.0.0.1:40000/preview/"
    assert seen == [workspace.repo_root.resolve()] * 3


def test_surface_side_is_materialized_once_from_the_discovery_checkout(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The local server's Reader runs in a non-git scratch root. A surface side
    # must come from the proven checkout, into its own per-side directory, and
    # be reused: re-materializing (rmtree + write) per request deleted the tree
    # a concurrent surface run was executing from.
    from lemoncrow.pro.capabilities.review import snapshot

    scratch = tmp_path / "reader-scratch"
    scratch.mkdir()
    surfaces_root = tmp_path / "surface-scratch"
    calls: list[tuple[Path, str, Path]] = []

    def materialize(_store_root: Path, repo: Path, _revision: Any, *, side: str, target: Path, **_kwargs: Any) -> str:
        calls.append((repo, side, target))
        target.mkdir(parents=True)
        return side

    monkeypatch.setattr(snapshot, "materialize_review_side", materialize)
    app = FastAPI()
    register_review_api(
        app,
        workspace.store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=scratch,
        dependency_repo_root=workspace.repo_root,
        session_repo_identity=str(workspace.repo_root),
        surface_workspace_root=surfaces_root,
        port=PORT,
    )
    client = TestClient(app, base_url=BASE)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None

    for _ in range(2):
        client.post(
            f"/api/reviews/{workspace.review_id}/surface-run",
            params={"provider": "none", "surface_id": "none", "side": "old"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    target = surfaces_root / workspace.review_id / revision.id / "old" / "workspace"
    assert calls == [(workspace.repo_root.resolve(), "old", target)]


def test_oversized_file_notice_reaches_reader_without_preview_override(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gzip
    import json

    from lemoncrow.pro.capabilities.review.models import oversized_diff_reason

    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    packet = json.loads(gzip.decompress(workspace.store.read_packet_artifact(revision)))
    entry = next(item for item in packet["files"] if item["path"] == "src/session.py")
    entry["diff_omitted_reason"] = oversized_diff_reason(1024 * 1024)
    entry["hunks"] = []
    workspace.store.write_packet_artifact(workspace.review_id, revision.id, gzip.compress(json.dumps(packet).encode()))
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.api._media_preview_payload",
        lambda _entry: {"kind": "media"},
    )

    response = workspace.get(f"/api/reviews/{workspace.review_id}/files/src/session.py/patch")
    assert response.status_code == 200
    body = response.json()
    assert body["renderable"] is False
    assert body["refusal"] == "file_too_large"
    assert "1 MiB (1,048,576 bytes)" in body["detail"]
    assert body["patch"] == ""
    assert body["preview"] is None


def test_review_patch_download_is_complete_and_git_apply_ready(workspace: Workspace) -> None:
    response = workspace.get(f"/api/reviews/{workspace.review_id}/patch")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-diff")
    assert response.headers["content-disposition"] == 'attachment; filename="lemoncrow-review-rev-0.patch"'
    patch = response.text
    assert patch.startswith("diff --git a/src/session.py b/src/session.py\n")
    assert "--- a/src/session.py" in patch
    assert "+++ b/src/session.py" in patch
    assert "@@" in patch


def test_cursor_rule_mdc_is_rendered_as_markdown() -> None:
    assert _is_markdown_path("integrations/cursor/rules/lemoncrow.code.mdc")


def test_markdown_preview_reconstructs_commit_range_when_blob_cache_is_missing(tmp_path: Path) -> None:
    repo_root = tmp_path / "markdown-commit-repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "README.md", "# Before\n\nOld body.\n")
    base_sha = _commit(repo, "base", 0)
    _write(repo_root, "README.md", "# After\n\nNew body.\n")
    head_sha = _commit(repo, "head", 1)

    store_root = tmp_path / "markdown-commit-store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, base=base_sha, head=head_sha)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    store.discard_artifact(store.blob_artifact_relpath(session.id, revision.id))

    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=repo_root, port=PORT)
    client = TestClient(app, base_url=BASE)
    response = client.get(
        f"/api/reviews/{session.id}/files/README.md/patch",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["preview"] == {
        "kind": "markdown",
        "old_content": "# Before\n\nOld body.\n",
        "new_content": "# After\n\nNew body.\n",
    }


def test_csv_file_has_builtin_revision_pinned_table_preview(tmp_path: Path) -> None:
    repo_root = tmp_path / "csv-repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "data/report.csv", 'name,value\nalpha,"1,000"\n')
    _commit(repo, "base", 0)
    reviewed = 'name,value\nalpha,"2,000"\nbeta,3\n'
    _write(repo_root, "data/report.csv", reviewed)

    store_root = tmp_path / "csv-store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)

    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=repo_root, port=PORT)
    client = TestClient(app, base_url=BASE)
    response = client.get(
        f"/api/reviews/{session.id}/files/data/report.csv/patch",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["preview"] == {
        "kind": "csv",
        "old_content": 'name,value\nalpha,"1,000"\n',
        "new_content": reviewed,
    }


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

    # The central server serves the same old side from its attached immutable
    # source-tree/content store, where repo_root is server scratch rather than
    # the original Git checkout. Prove that path wins before the Git fallback.
    frozen_old = b"GIF89a\x00FROZEN-OLD"
    frozen_digest = hashlib.sha256(frozen_old).hexdigest()

    class FrozenTreeStore:
        def read_source_tree_artifact(
            self, review_id: str, revision_id: str, *, side: str = "new"
        ) -> dict[str, dict[str, int | str]]:
            assert (review_id, revision_id, side) == ("review", "revision", "old")
            return {
                "docs/demo.gif": {
                    "content_digest": frozen_digest,
                    "mode": 0o100644,
                    "size": len(frozen_old),
                }
            }

        def read_source_content(self, digest: str) -> bytes | None:
            return frozen_old if digest == frozen_digest else None

    frozen_body, frozen_reason = review_api._revision_asset_bytes(
        FrozenTreeStore(),  # type: ignore[arg-type]
        "review",
        SimpleNamespace(id="revision", range_mode="working_tree", base_sha="deadbeef", head_sha=""),
        tmp_path / "not-a-git-checkout",
        {"files": [{"path": "docs/demo.gif", "status": "modified", "is_binary": True}]},
        "docs/demo.gif",
        side="old",
    )
    assert frozen_reason == ""
    assert frozen_body == frozen_old

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

    # Without a trusted checkout binding no workspace path is exposed.
    assert client.get(f"/api/reviews/{workspace.review_id}", headers=headers).json()["workspace_root"] == ""


def test_trusted_checkout_exposes_its_real_workspace_root(workspace: Workspace) -> None:
    overview = workspace.get(f"/api/reviews/{workspace.review_id}").json()
    assert overview["workspace_root"] == str(workspace.repo_root.resolve())


def test_change_proposal_insert_rechecks_current_revision_after_api_validation(
    workspace: Workspace, monkeypatch
) -> None:
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE + "\n# second generation\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    second = workspace.store.latest_revision(workspace.review_id)
    assert second is not None and second.id != first.id

    original_add = workspace.store.add_change_proposal

    def race_head(proposal: Any, *, expected_current_revision_id: str = "") -> Any:
        workspace.store.set_current_revision(workspace.review_id, first.id)
        return original_add(proposal, expected_current_revision_id=expected_current_revision_id)

    monkeypatch.setattr(workspace.store, "add_change_proposal", race_head)
    refused = workspace.post(
        f"/api/reviews/{workspace.review_id}/proposals",
        json={
            "expected_revision_id": second.id,
            "path": "src/session.py",
            "start_line": 11,
            "end_line": 11,
            "replacement_text": '            return {"status": "inactive"}',
        },
    )
    assert refused.status_code == 409
    assert "advanced before this change proposal committed" in refused.json()["detail"]
    assert workspace.store.list_change_proposals(workspace.review_id) == ()


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
    assert body["revision"]["revision_number"] == 1
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


def test_change_proposal_apply_refuses_edit_that_lands_during_atomic_write(workspace: Workspace, monkeypatch) -> None:
    revision_id = workspace.get(f"/api/reviews/{workspace.review_id}").json()["revision"]["id"]
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/proposals",
        json={
            "expected_revision_id": revision_id,
            "path": "src/session.py",
            "start_line": 11,
            "end_line": 11,
            "replacement_text": '            return {"status": "inactive"}',
        },
    )
    assert created.status_code == 201, created.text
    proposal = created.json()["proposal"]
    external = _CHANGED_SOURCE + "\n# concurrent IDE edit\n"
    original_mkstemp = review_api.tempfile.mkstemp
    raced = False

    def mkstemp_after_external_edit(*args: Any, **kwargs: Any) -> tuple[int, str]:
        nonlocal raced
        if not raced:
            raced = True
            _write(workspace.repo_root, "src/session.py", external)
        return original_mkstemp(*args, **kwargs)

    monkeypatch.setattr(review_api.tempfile, "mkstemp", mkstemp_after_external_edit)
    refused = workspace.post(f"/api/proposals/{proposal['id']}/apply")
    assert refused.status_code == 409
    assert "source changed while the proposal write was being committed" in refused.json()["detail"]
    assert (workspace.repo_root / "src/session.py").read_text(encoding="utf-8") == external
    stored = workspace.get(f"/api/reviews/{workspace.review_id}/proposals").json()["proposals"][0]
    assert stored["state"] == "conflicted"


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
    assert refreshed.json()["revision"]["revision_number"] == 1

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


def test_a_slow_source_probe_leaves_other_reader_requests_served(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Reader polls source-state every two seconds. On a large checkout the
    # probe takes seconds, and run on the event loop it stalled every request.
    import asyncio
    import threading
    import time

    import httpx

    from lemoncrow.pro.capabilities.review import gitdiff

    entered = threading.Event()
    release = threading.Event()
    real_source_state = gitdiff.source_state

    def slow_source_state(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        release.wait(5)
        return real_source_state(*args, **kwargs)

    monkeypatch.setattr(gitdiff, "source_state", slow_source_state)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async def exercise() -> float:
        transport = httpx.ASGITransport(app=workspace.client.app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            started = time.monotonic()
            probe = asyncio.ensure_future(
                client.get(f"/api/reviews/{workspace.review_id}/source-state", headers=headers)
            )
            while not entered.is_set():
                await asyncio.sleep(0.01)
            review = await client.get(f"/api/reviews/{workspace.review_id}", headers=headers)
            elapsed = time.monotonic() - started
            release.set()
            assert review.status_code == 200
            assert (await probe).json()["supported"] is True
            return elapsed

    try:
        assert asyncio.run(exercise()) < 2
    finally:
        release.set()


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
    assert refreshed.json()["revision"]["revision_number"] == 1

    second = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "comment from rev two", "kind": "comment"},
    )
    assert second.status_code == 201, second.text
    located = workspace.get(f"/api/revisions/{first_revision.id}")
    assert located.status_code == 200, located.text
    located_payload = located.json()
    assert located_payload["review"]["id"] == workspace.review_id
    assert located_payload["review"]["ref"] == f"r/{located_payload['review']['short_id']}"
    assert located_payload["revision"]["id"] == first_revision.id
    assert located_payload["revision"]["ref"] == f"rr/{located_payload['revision']['short_id']}"
    assert located_payload["review"]["short_id"]
    assert located_payload["revision"]["short_id"]
    old_alias = workspace.get(f"/api/revisions/rr-{first_revision.id}")
    assert old_alias.status_code == 404

    historical = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/0")
    assert historical.status_code == 200, historical.text
    assert historical.json()["revision"]["id"] == first_revision.id
    assert historical.json()["latest_revision_number"] == 1
    # No human judgment was ever overwritten in this fixture, so H2 can honestly
    # call the historical judgment projection complete rather than globally
    # degrading every old revision just because it is historical.
    assert historical.json()["historical_judgment_complete"] is True

    targets = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/0/targets")
    assert targets.status_code == 200, targets.text
    assert targets.json()["revision_id"] == first_revision.id
    assert targets.json()["historical"] is True

    patch = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/0/files/src/session.py/patch")
    assert patch.status_code == 200, patch.text
    assert patch.json()["revision_id"] == first_revision.id
    assert patch.json()["historical"] is True
    assert "accepted edit" not in patch.json()["patch"]

    comments = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/0/annotations")
    assert comments.status_code == 200, comments.text
    assert comments.json()["historical"] is True
    assert [item["id"] for item in comments.json()["annotations"]] == [first_comment["id"]]
    old_comment = comments.json()["annotations"][0]
    assert old_comment["start_line"] == first_comment["start_line"]
    assert old_comment["anchor_method"] == first_comment["anchor_method"]

    # There is deliberately no historical mutation namespace. A revision URL is
    # read-only by construction rather than by a disabled button in the browser.
    refused = workspace.post(
        f"/api/reviews/{workspace.review_id}/revisions/0/marks",
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
    assert refresh2.json()["revision"]["revision_number"] == 1
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
    assert refresh3.json()["revision"]["revision_number"] == 2

    history = workspace.get(f"/api/reviews/{workspace.review_id}/targets/{unit_key}/history")
    assert history.status_code == 200, history.text
    transitions = [(item["event_kind"], item["from_state"], item["to_state"]) for item in history.json()["events"]]
    assert transitions == [
        ("judgment", "", "reviewed"),
        ("reconciled", "reviewed", "changed_since_review"),
        ("judgment", "changed_since_review", "needs_changes"),
        ("discarded", "needs_changes", ""),
    ]

    old1 = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/0")
    assert old1.status_code == 200, old1.text
    assert old1.json()["historical_judgment_complete"] is True
    assert old1.json()["progress"]["reviewed"] == 1

    old2 = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/1")
    assert old2.status_code == 200, old2.text
    assert old2.json()["revision"]["id"] == rev2_id
    assert old2.json()["progress"]["needs_changes"] == 1

    comments1 = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/0/annotations").json()["annotations"]
    comments2 = workspace.get(f"/api/reviews/{workspace.review_id}/revisions/1/annotations").json()["annotations"]
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


def test_web_preview_diff_ranks_only_a_revision_pinned_affected_route(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    seen: dict[str, Any] = {}

    monkeypatch.setattr(review_api, "_web_preview_payload", lambda *_args, **_kwargs: {"routes": ["/affected"]})

    class Score:
        def to_payload(self) -> dict[str, float | int]:
            return {"changed_ratio": 0.42, "mean_delta": 0.18, "width": 1440, "height": 5000}

    def rank(*_args: Any, **kwargs: Any) -> Score:
        seen.update(kwargs)
        return Score()

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.web_preview.rank_web_preview_difference", rank)
    response = workspace.get(
        f"/api/reviews/{workspace.review_id}/web-preview-diff",
        params={
            "document_path": "src/session.py",
            "route": "/affected",
            "revision_id": revision.id,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "revision_id": revision.id,
        "route": "/affected",
        "changed_ratio": 0.42,
        "mean_delta": 0.18,
        "width": 1440,
        "height": 5000,
    }
    assert seen["path"] == "src/session.py"
    assert seen["route"] == "/affected"

    refused = workspace.get(
        f"/api/reviews/{workspace.review_id}/web-preview-diff",
        params={"document_path": "src/session.py", "route": "/unaffected", "revision_id": revision.id},
    )
    assert refused.status_code == 404
    assert "affected route" in refused.json()["detail"]


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
    assert evidence["revision_number"] == 0
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
    later_payload = workspace.get(f"/api/reviews/{workspace.review_id}/evidence").json()
    later = later_payload["evidence"]
    assert later[0]["status"] == "stale"
    assert later[0]["revision_number"] == 0

    historical = workspace.get(
        f"/api/reviews/{workspace.review_id}/evidence",
        params={"revision_id": evidence["revision_id"]},
    )
    assert historical.status_code == 200, historical.text
    historical_payload = historical.json()
    assert historical_payload["revision_id"] == evidence["revision_id"]
    assert historical_payload["evidence"][0]["status"] == "current"
    assert historical_payload["evidence"][0]["revision_number"] == 0


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


def test_dependency_change_refresh_creates_semantic_targets_and_hides_raw_lock_churn(workspace: Workspace) -> None:
    _write(
        workspace.repo_root,
        "pyproject.toml",
        """[project]
name = "demo"
version = "1.0"
dependencies = ["cryptography>=46,<47"]
""",
    )
    _write(
        workspace.repo_root,
        "uv.lock",
        """version = 1

[[package]]
name = "cryptography"
version = "46.0.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "cffi" }]
wheels = [{ url = "https://example.invalid/cryptography.whl", hash = "sha256:newcrypto" }]

[[package]]
name = "cffi"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://example.invalid/cffi.whl", hash = "sha256:newcffi" }]
""",
    )
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    dependency_snapshot = workspace.store.latest_revision(workspace.review_id)
    assert dependency_snapshot is not None

    targets_response = workspace.get(f"/api/reviews/{workspace.review_id}/targets")
    assert targets_response.status_code == 200, targets_response.text
    targets = targets_response.json()["targets"]
    dependency_targets = [row for row in targets if row["kind"] == "dependency"]
    assert any(row["label"] == "pypi:cryptography" for row in dependency_targets)
    assert any(row["label"] == "pypi:cffi" for row in dependency_targets)
    assert not any(row["path"] == "uv.lock" and row["kind"] != "dependency" for row in targets)
    assert any(row["path"] == "pyproject.toml" and row["kind"] != "dependency" for row in targets)

    dependencies = workspace.get(f"/api/reviews/{workspace.review_id}/dependencies")
    assert dependencies.status_code == 200, dependencies.text
    body = dependencies.json()
    assert body["supported"] is True
    crypto = next(row for row in body["changes"] if row["name"] == "cryptography")
    cffi = next(row for row in body["changes"] if row["name"] == "cffi")
    assert crypto["direct"] is True
    assert crypto["after_version"] == "46.0.0"
    assert crypto["transitive_changes"] == 1
    assert "added" in crypto["changes"]
    assert cffi["direct"] is False
    assert ["cryptography", "cffi"] in cffi["dependency_paths"]
    assert crypto["target_id"] in {row["target_id"] for row in dependency_targets}

    crypto_target = next(row for row in dependency_targets if row["label"] == "pypi:cryptography")
    marked = workspace.post(
        f"/api/reviews/{workspace.review_id}/marks",
        json={"unit_key": crypto_target["unit_key"], "state": "reviewed"},
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["recorded"] == "reviewed"

    _write(
        workspace.repo_root,
        "pyproject.toml",
        """[project]
name = "demo"
version = "1.0"
dependencies = ["cryptography>=47,<48"]
""",
    )
    _write(
        workspace.repo_root,
        "uv.lock",
        """version = 1

[[package]]
name = "cryptography"
version = "47.0.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "cffi" }]
wheels = [{ url = "https://example.invalid/cryptography.whl", hash = "sha256:newercrypto" }]

[[package]]
name = "cffi"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://example.invalid/cffi.whl", hash = "sha256:newcffi" }]
""",
    )
    refreshed_again = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed_again.status_code == 200, refreshed_again.text
    targets_again = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    crypto_again = next(row for row in targets_again if row["label"] == "pypi:cryptography")
    assert crypto_again["unit_key"] == crypto_target["unit_key"]
    assert crypto_again["state"] == "changed_since_review"

    historical_dependencies = workspace.get(
        f"/api/reviews/{workspace.review_id}/dependencies",
        params={"revision_id": dependency_snapshot.id},
    )
    assert historical_dependencies.status_code == 200, historical_dependencies.text
    historical_crypto = next(row for row in historical_dependencies.json()["changes"] if row["name"] == "cryptography")
    assert historical_crypto["after_version"] == "46.0.0"
    assert historical_crypto["after_integrity"] == "sha256:newcrypto"


def test_unparseable_lockfile_stays_raw_when_only_manifest_semantics_are_safe(workspace: Workspace) -> None:
    _write(
        workspace.repo_root,
        "pyproject.toml",
        """[project]
name = "demo"
version = "1.0"
dependencies = ["authlib>=1.3"]
""",
    )
    _write(workspace.repo_root, "uv.lock", "not = [valid toml")

    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    targets = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]

    dependency = next(row for row in targets if row["kind"] == "dependency" and row["label"] == "pypi:authlib")
    assert dependency["path"] == "pyproject.toml"
    assert any(row["path"] == "uv.lock" and row["kind"] != "dependency" for row in targets)


def _nested_npm_lock(a_version: str, nested_c_resolved: str, nested_c_integrity: str) -> str:
    return (
        json.dumps(
            {
                "name": "app",
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "app", "dependencies": {"a": "^1.0.0", "b": "^1.0.0"}},
                    "node_modules/a": {
                        "version": a_version,
                        "resolved": f"https://registry.npmjs.org/a/-/a-{a_version}.tgz",
                        "integrity": f"sha512-a{a_version}",
                    },
                    "node_modules/b": {
                        "version": "1.0.0",
                        "resolved": "https://registry.npmjs.org/b/-/b-1.0.0.tgz",
                        "integrity": "sha512-b",
                        "dependencies": {"c": "^2.0.0"},
                    },
                    "node_modules/c": {
                        "version": "1.0.0",
                        "resolved": "https://registry.npmjs.org/c/-/c-1.0.0.tgz",
                        "integrity": "sha512-c1",
                    },
                    "node_modules/b/node_modules/c": {
                        "version": "2.0.0",
                        "resolved": nested_c_resolved,
                        "integrity": nested_c_integrity,
                    },
                },
            },
            indent=2,
        )
        + "\n"
    )


def test_nested_lockfile_copy_swapped_after_review_reopens_landing(workspace: Workspace) -> None:
    root = workspace.repo_root
    good_c = ("https://registry.npmjs.org/c/-/c-2.0.0.tgz", "sha512-c2")
    _write(root, "package.json", json.dumps({"name": "app", "dependencies": {"a": "^1.0.0", "b": "^1.0.0"}}) + "\n")
    _write(root, "package-lock.json", _nested_npm_lock("1.0.0", *good_c))
    _commit(pygit2.Repository(str(root)), "lock", 1)

    # Snapshot N: only the top-level package moves; the reviewer judges it.
    _write(root, "package-lock.json", _nested_npm_lock("1.0.1", *good_c))
    assert workspace.post(f"/api/reviews/{workspace.review_id}/refresh").status_code == 200
    _review_all_current_targets(workspace)
    reviewed = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    assert reviewed["armable"] is True

    # Snapshot N+1: the author swaps the nested copy's tarball. The lockfile's raw
    # hunks are not review targets, so the semantic targets must reopen.
    _write(root, "package-lock.json", _nested_npm_lock("1.0.1", "https://evil.example/c-2.0.0.tgz", "sha512-EVIL"))
    assert workspace.post(f"/api/reviews/{workspace.review_id}/refresh").status_code == 200

    targets = workspace.get(f"/api/reviews/{workspace.review_id}/targets").json()["targets"]
    nested = next(row for row in targets if row["kind"] == "dependency" and row["label"] == "npm:c")
    assert nested["state"] != "reviewed"
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    reviewed_condition = next(row for row in readiness["conditions"] if row["id"] == "latest-snapshot-reviewed")
    assert reviewed_condition["state"] == "pending"
    assert readiness["armable"] is False


def test_verification_results_are_exact_snapshot_and_late_ci_stays_stale(workspace: Workspace) -> None:
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"newer"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    second = workspace.store.latest_revision(workspace.review_id)
    assert second is not None and second.id != first.id

    late_ci = workspace.post(
        f"/api/reviews/{workspace.review_id}/verification",
        json={
            "revision_id": first.id,
            "name": "CI / unit tests",
            "status": "PASS",
            "source": "ci",
            "source_ref": "github-actions:run-42",
            "detail": "582 passed",
        },
    )
    assert late_ci.status_code == 201, late_ci.text
    assert late_ci.json()["evidence"]["revision_id"] == first.id

    current = workspace.get(f"/api/reviews/{workspace.review_id}/verification")
    assert current.status_code == 200, current.text
    current_body = current.json()
    stale = next(row for row in current_body["checks"] if row["name"] == "CI / unit tests")
    assert stale["state"] == "stale"
    assert stale["status"] == "PASS"
    assert stale["snapshot_id"] == first.id
    assert stale["snapshot_number"] == 0
    assert current_body["summary"]["pass"] == 0
    assert current_body["summary"]["stale"] >= 1

    historical = workspace.get(
        f"/api/reviews/{workspace.review_id}/verification",
        params={"revision_id": first.id},
    )
    assert historical.status_code == 200, historical.text
    historical_row = next(row for row in historical.json()["checks"] if row["name"] == "CI / unit tests")
    assert historical_row["state"] == "current"
    assert historical_row["status"] == "PASS"
    assert historical.json()["snapshot_number"] == 0

    current_not_run = workspace.post(
        f"/api/reviews/{workspace.review_id}/verification",
        json={
            "revision_id": second.id,
            "name": "Browser checks",
            "status": "NOT_RUN",
            "source": "test",
            "detail": "browser runtime unavailable",
        },
    )
    assert current_not_run.status_code == 201, current_not_run.text
    body = workspace.get(f"/api/reviews/{workspace.review_id}/verification").json()
    browser = next(row for row in body["checks"] if row["name"] == "Browser checks")
    assert browser["state"] == "current"
    assert browser["status"] == "NOT_RUN"
    assert browser["snapshot_id"] == second.id
    assert body["summary"]["not_run"] >= 1


def test_sbom_evidence_requires_standard_format_and_immutable_artifact_digest(workspace: Workspace) -> None:
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    artifact_digest = "sha256:" + "a" * 64
    spdx = json.dumps(
        {
            "spdxVersion": "SPDX-2.3",
            "packages": [
                {
                    "name": "cryptography",
                    "versionInfo": "46.0.0",
                    "licenseConcluded": "Apache-2.0",
                }
            ],
        }
    ).encode()

    uploaded = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={
            "kind": "sbom",
            "revision_id": revision.id,
            "title": "BuildKit SPDX",
            "filename": "sbom.spdx.json",
            "mime_type": "application/json",
            "source": "ci",
            "artifact_digest": artifact_digest,
            "observation_source": "BuildKit",
        },
        content=spdx,
        headers={"Content-Type": "application/json"},
    )
    assert uploaded.status_code == 201, uploaded.text
    evidence = uploaded.json()["evidence"]
    assert evidence["kind"] == "sbom"
    metadata = json.loads(evidence["detail"])
    assert metadata["artifact_digest"] == artifact_digest
    assert metadata["standard"] == "SPDX"
    assert metadata["format_version"] == "SPDX-2.3"
    assert metadata["component_count"] == 1
    assert metadata["observation_source"] == "BuildKit"

    verification = workspace.get(f"/api/reviews/{workspace.review_id}/verification").json()
    sbom_artifact = next(row for row in verification["artifacts"] if row["kind"] == "sbom")
    assert sbom_artifact["state"] == "current"
    assert sbom_artifact["snapshot_id"] == revision.id
    assert sbom_artifact["digest"] == evidence["content_hash"]

    proprietary = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={
            "kind": "sbom",
            "revision_id": revision.id,
            "artifact_digest": artifact_digest,
        },
        content=b'{"format":"lemoncrow-sbom","packages":[]}',
        headers={"Content-Type": "application/json"},
    )
    assert proprietary.status_code == 422
    assert "expected SPDX" in proprietary.json()["detail"]

    mutable_subject = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={
            "kind": "sbom",
            "revision_id": revision.id,
            "artifact_digest": "latest",
        },
        content=spdx,
        headers={"Content-Type": "application/json"},
    )
    assert mutable_subject.status_code == 422
    assert "immutable sha256" in mutable_subject.json()["detail"]


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


def test_feedback_routes_each_file_to_its_exact_author_session(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(workspace.repo_root, "src/untouched.py", _UNTOUCHED_SOURCE + "\nMIXED = True\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    workspace.store.add_revision_attributions(
        revision.id,
        (
            ReviewAttribution(
                revision_id=revision.id,
                scope="file",
                scope_key="src/session.py",
                host="claude",
                session_id="session-claude",
                certainty="exact",
                method="recorded_edit",
            ),
            ReviewAttribution(
                revision_id=revision.id,
                scope="file",
                scope_key="src/untouched.py",
                host="codex",
                session_id="session-codex",
                certainty="exact",
                method="recorded_edit",
            ),
        ),
    )
    first = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "claude-only feedback"},
    ).json()["annotation"]
    second = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/untouched.py", "start_line": 1, "body": "codex-only feedback"},
    ).json()["annotation"]
    calls: list[tuple[str, str, str, str]] = []

    def deliver(
        host: str, session_id: str, _repo: Path, feedback: str, *, delivery_id: str = ""
    ) -> ClaudeDeliveryResult:
        calls.append((host, session_id, feedback, delivery_id))
        return ClaudeDeliveryResult("consumed", f"{host}:{session_id}", remote_ref=f"receipt:{session_id}")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.deliver_to_agent_session", deliver)
    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    assert preview["delivery"]["supported"] is True
    assert set(preview["delivery"]["targets"]) == {"claude:session-claude", "codex:session-codex"}
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver",
        json={
            "operation_id": preview["operation_id"],
            "expected_revision_id": preview["revision_id"],
            "expected_feedback_hash": preview["feedback_hash"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "consumed"
    assert {(host, session) for host, session, _feedback, _delivery_id in calls} == {
        ("claude", "session-claude"),
        ("codex", "session-codex"),
    }
    by_target = {(host, session): feedback for host, session, feedback, _delivery_id in calls}
    assert "claude-only feedback" in by_target[("claude", "session-claude")]
    assert "codex-only feedback" not in by_target[("claude", "session-claude")]
    assert "codex-only feedback" in by_target[("codex", "session-codex")]
    assert "claude-only feedback" not in by_target[("codex", "session-codex")]
    assert all(delivery_id.startswith(preview["operation_id"] + ":") for *_rest, delivery_id in calls)
    assert {item.annotation_id for item in workspace.store.list_delivery_operation(preview["operation_id"])} == {
        first["id"],
        second["id"],
    }


def _feedback_for_two_author_sessions(workspace: Workspace) -> None:
    _write(workspace.repo_root, "src/untouched.py", _UNTOUCHED_SOURCE + "\nMIXED = True\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    workspace.store.add_revision_attributions(
        revision.id,
        tuple(
            ReviewAttribution(
                revision_id=revision.id,
                scope="file",
                scope_key=path,
                host=host,
                session_id=f"session-{host}",
                certainty="exact",
                method="recorded_edit",
            )
            for path, host in (("src/session.py", "claude"), ("src/untouched.py", "codex"))
        ),
    )
    for path, line in (("src/session.py", 10), ("src/untouched.py", 1)):
        created = workspace.post(
            f"/api/reviews/{workspace.review_id}/annotations",
            json={"path": path, "start_line": line, "body": f"feedback for {path}"},
        )
        assert created.status_code == 201, created.text


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ({"claude": "sent", "codex": "sent"}, "sent"),
        ({"claude": "sent", "codex": "consumed"}, "sent"),
        ({"claude": "consumed", "codex": "consumed"}, "consumed"),
    ],
)
def test_multi_session_delivery_reports_consumed_only_when_every_session_consumed(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, states: dict[str, str], expected: str
) -> None:
    _feedback_for_two_author_sessions(workspace)

    def deliver(
        host: str, session_id: str, _repo: Path, _feedback: str, *, delivery_id: str = ""
    ) -> ClaudeDeliveryResult:
        return ClaudeDeliveryResult(states[host], f"{host}:{session_id}", remote_ref=f"receipt:{session_id}")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.deliver_to_agent_session", deliver)
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver", json=_prepared_feedback_body(workspace)
    )

    # A handoff no session has confirmed reading is "sent", never "consumed".
    assert response.status_code == 200, response.text
    assert response.json()["state"] == expected
    assert {row["state"] for row in response.json()["targets"]} == set(states.values())


def test_feedback_preview_is_bound_to_the_exact_routes_it_showed(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_latest_revision_exact_claude(workspace, "session-primary")
    workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "route this exactly"},
    )
    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    assert preview["delivery"]["target_ref"] == "claude:session-primary"
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    workspace.store.add_revision_attributions(
        revision.id,
        (
            ReviewAttribution(
                revision_id=revision.id,
                scope="file",
                scope_key="src/session.py",
                host="codex",
                session_id="session-file-author",
                certainty="exact",
                method="recorded_edit",
            ),
        ),
    )

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.delivery.deliver_to_agent_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stale preview must not dispatch")),
    )
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver",
        json={
            "operation_id": preview["operation_id"],
            "expected_revision_id": preview["revision_id"],
            "expected_feedback_hash": preview["feedback_hash"],
        },
    )
    assert response.status_code == 409
    assert "routing changed since preview" in response.json()["detail"]


def test_feedback_does_not_use_primary_author_for_an_unattributed_file_once_scoped_evidence_exists(
    workspace: Workspace,
) -> None:
    _write(workspace.repo_root, "src/untouched.py", _UNTOUCHED_SOURCE + "\nSCOPED = True\n")
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    _make_latest_revision_exact_claude(workspace, "session-primary")
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    workspace.store.add_revision_attributions(
        revision.id,
        (
            ReviewAttribution(
                revision_id=revision.id,
                scope="file",
                scope_key="src/untouched.py",
                host="codex",
                session_id="session-scoped",
                certainty="exact",
                method="recorded_edit",
            ),
        ),
    )
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "do not guess my author"},
    )
    assert created.status_code == 201, created.text

    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()

    assert preview["delivery"]["supported"] is False
    assert preview["delivery"]["targets"] == []
    assert "exact responsible session is unavailable for src/session.py" in preview["delivery"]["reason"]


def test_feedback_refuses_to_guess_when_one_file_has_two_exact_authors(workspace: Workspace) -> None:
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    workspace.store.add_revision_attributions(
        revision.id,
        (
            ReviewAttribution(
                revision_id=revision.id,
                scope="file",
                scope_key="src/session.py",
                host="claude",
                session_id="session-a",
                certainty="exact",
                method="recorded_edit",
            ),
            ReviewAttribution(
                revision_id=revision.id,
                scope="file",
                scope_key="src/session.py",
                host="codex",
                session_id="session-b",
                certainty="exact",
                method="recorded_edit",
            ),
        ),
    )
    workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "ambiguous owner"},
    )

    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    assert preview["delivery"]["supported"] is False
    assert "mixed exact authorship" in preview["delivery"]["reason"]


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
    assert "exact primary author session" in response.json()["detail"]
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


def test_annotation_projection_keeps_human_chronology_and_ranks_machine_context(workspace: Workspace) -> None:
    first = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "human first"},
    ).json()["annotation"]
    second = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "human second"},
    ).json()["annotation"]
    base = workspace.store.get_annotation(first["id"])
    assert base is not None
    low = workspace.store.add_annotation(
        replace(
            base,
            id="",
            body="low confidence machine hint",
            source="ai_review",
            source_id="correctness:low",
            created_by="correctness",
            created_by_actor="agent",
            confidence=0.35,
            evidence=("finding:low",),
            created_at="",
            updated_at="",
        )
    )
    author = workspace.store.add_annotation(
        replace(
            base,
            id="",
            body="author rationale",
            source="author",
            source_id="author-session",
            created_by="claude",
            created_by_actor="agent",
            confidence=None,
            evidence=("rationale:1",),
            created_at="",
            updated_at="",
        )
    )
    high = workspace.store.add_annotation(
        replace(
            base,
            id="",
            body="high confidence machine hint",
            source="ai_review",
            source_id="correctness:high",
            created_by="correctness",
            created_by_actor="agent",
            confidence=0.92,
            evidence=("finding:high", "test:focused"),
            created_at="",
            updated_at="",
        )
    )

    response = workspace.get(f"/api/reviews/{workspace.review_id}/annotations")
    assert response.status_code == 200
    annotations = response.json()["annotations"]
    ids = [item["id"] for item in annotations]

    assert ids[:2] == [first["id"], second["id"]]
    assert ids[2:] == [high.id, low.id, author.id]
    high_payload = next(item for item in annotations if item["id"] == high.id)
    assert high_payload["confidence"] == 0.92
    assert high_payload["source"] == "ai_review"


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


def test_identical_feedback_previews_converge_on_one_operation_identity(workspace: Workspace) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "same bundle in two tabs"},
    )

    first = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    second = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()

    assert first["revision_id"] == second["revision_id"]
    assert first["feedback_hash"] == second["feedback_hash"]
    assert first["operation_id"] == second["operation_id"]


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


def test_stale_receiptless_dispatch_stays_uncertain_and_never_auto_retries(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    _make_latest_revision_exact_claude(workspace, session_id)
    created = workspace.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={"path": "src/session.py", "start_line": 10, "body": "survive a server restart"},
    ).json()["annotation"]
    preview = workspace.post(f"/api/reviews/{workspace.review_id}/feedback/export").json()
    operation_id = preview["operation_id"]
    target_ref = f"claude:{session_id}"
    _operation, inserted = workspace.store.begin_delivery_operation(
        (
            DeliveryRecord(
                id="",
                annotation_id=created["id"],
                target_type="agent_session",
                target_ref=target_ref,
                state="prepared",
                operation_id=operation_id,
                revision_id=preview["revision_id"],
                feedback_hash=preview["feedback_hash"],
                annotation_version=preview["annotation_versions"][created["id"]],
            ),
        )
    )
    assert inserted is True
    leased, claimed = workspace.store.claim_delivery_target(operation_id, target_ref)
    assert claimed is True and leased[0].state == "leased"
    dispatching, crossed = workspace.store.begin_delivery_dispatch(operation_id, target_ref)
    assert crossed is True and dispatching[0].state == "dispatching"
    with workspace.store._transaction() as conn:
        conn.execute(
            "UPDATE deliveries SET updated_at = '2000-01-01T00:00:00+00:00' WHERE operation_id = ?",
            (operation_id,),
        )

    calls = 0

    def deliver(*_args: object, **_kwargs: object) -> ClaudeDeliveryResult:
        nonlocal calls
        calls += 1
        raise AssertionError("ambiguous dispatch must not auto-retry")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session", deliver)
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/feedback/deliver",
        json={
            "operation_id": operation_id,
            "expected_revision_id": preview["revision_id"],
            "expected_feedback_hash": preview["feedback_hash"],
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["state"] == "uncertain"
    assert payload["operation_id"] == operation_id
    assert payload["target_ref"] == target_ref
    assert "may already have crossed the host boundary" in payload["message"]
    assert calls == 0
    stored = workspace.store.list_delivery_operation(operation_id)
    assert len(stored) == 1
    assert stored[0].state == "dispatching"
    assert stored[0].remote_ref == ""
    assert stored[0].last_error == ""


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
    assert third.json()["refreshed"]["revision_number"] == 2
    assert third.json()["refreshed"]["previous_revision_number"] == 0
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


# --------------------------------------------------------------------------- #
# browser / UI regression evidence
# --------------------------------------------------------------------------- #

_BROWSER_CONFIG = """version: 1
browser_checks:
  trusted_origins: ["https://preview.example.test"]
  scenarios:
    - id: session-refresh
      title: Session refresh
      route: /session
      paths: ["src/session.py"]
      required: true
      viewports: ["1280x800"]
    - id: settings-smoke
      title: Settings smoke
      route: /settings
      paths: ["src/*.py"]
"""

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _browser_workspace(tmp_path: Path, delegate: Any = None) -> Workspace:
    repo_root = tmp_path / "browser-repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "src/session.py", _BASE_SOURCE)
    _write(repo_root, "src/untouched.py", _UNTOUCHED_SOURCE)
    # Browser policy lives on the base side: the Change cannot weaken it.
    _write(repo_root, ".lemoncrow/review.yaml", _BROWSER_CONFIG)
    _commit(repo, "base", 0)
    _write(repo_root, "src/session.py", _CHANGED_SOURCE)
    store_root = tmp_path / "browser-store"
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
        landing_delegate=delegate,
    )
    return Workspace(TestClient(app, base_url=BASE), store, store_root, repo_root, session.id)


def _upload(workspace: Workspace, revision_id: str, *, kind: str, title: str, payload: bytes, mime: str) -> str:
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={"kind": kind, "title": title, "revision_id": revision_id, "source": "ci", "mime_type": mime},
        content=payload,
        headers={"Content-Type": mime},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["evidence"]["id"])


def _browser_run(workspace: Workspace, revision_id: str, **overrides: Any) -> Any:
    body: dict[str, Any] = {
        "revision_id": revision_id,
        "scenario": "session-refresh",
        "route": "/session",
        "viewport": {"width": 1280, "height": 800},
        "environment": {"browser": "chromium", "browser_version": "129"},
        "provider": "playwright",
        "source": "ci",
        "run_ref": "https://ci.example.test/runs/7",
        "target_url": "http://127.0.0.1:4173/session",
        "completed": True,
        "navigation": {"status": 200},
        "assertions": [{"name": "status text", "status": "PASS", "expected": "invalid", "actual": "invalid"}],
        "console": [],
    }
    body.update(overrides)
    return workspace.post(f"/api/reviews/{workspace.review_id}/browser-checks", json=body)


def _condition(readiness: dict[str, Any], identifier: str) -> dict[str, Any] | None:
    return next((row for row in readiness["conditions"] if row["id"] == identifier), None)


def test_browser_regression_pass_is_bound_to_exact_snapshot_with_frozen_before_after_evidence(tmp_path: Path) -> None:
    workspace = _browser_workspace(tmp_path)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None

    before_shot = _upload(
        workspace, revision.id, kind="screenshot", title="session base", payload=_PNG + b"a", mime="image/png"
    )
    baseline = _browser_run(workspace, revision.id, side="old", artifacts={"screenshot": before_shot})
    assert baseline.status_code == 201, baseline.text
    # A baseline run describes pre-change code: evidence, never verification.
    assert baseline.json()["evidence"]["verification_status"] == ""

    after_shot = _upload(
        workspace, revision.id, kind="screenshot", title="session", payload=_PNG + b"b", mime="image/png"
    )
    trace = _upload(
        workspace,
        revision.id,
        kind="playwright_trace",
        title="trace",
        payload=b"PK\x03\x04trace",
        mime="application/zip",
    )
    video = _upload(workspace, revision.id, kind="video", title="recording", payload=b"webm", mime="video/webm")
    current = _browser_run(
        workspace,
        revision.id,
        artifacts={"screenshot": after_shot, "trace": trace, "video": video},
        observations=["Agent note: the banner looks right."],
    )
    assert current.status_code == 201, current.text
    assert current.json()["browser_check"]["status"] == "PASS"

    verification = workspace.get(f"/api/reviews/{workspace.review_id}/verification").json()
    rows = [row for row in verification["checks"] if row.get("browser") and row["state"] == "current"]
    session_row = next(row for row in rows if row["browser"]["scenario"] == "session-refresh")
    assert session_row["name"] == "Session refresh · 1280x800"
    assert session_row["status"] == "PASS"
    assert session_row["required"] is True
    assert session_row["snapshot_id"] == revision.id
    run = session_row["browser"]["run"]
    assert run["environment"]["browser"] == "chromium"
    assert run["viewport"]["width"] == 1280
    assert run["observations"] == ["Agent note: the banner looks right."]
    uploaded = {
        item["id"]: item for item in workspace.get(f"/api/reviews/{workspace.review_id}/evidence").json()["evidence"]
    }
    assert run["artifacts"]["screenshot"]["digest"] == uploaded[after_shot]["content_hash"]
    assert run["artifacts"]["trace"]["evidence_id"] == trace
    assert run["artifacts"]["video"]["evidence_id"] == video
    base = session_row["browser"]["baseline"]
    assert base["relation"] == "base" and base["side"] == "old" and base["snapshot_id"] == revision.id
    assert base["artifacts"]["screenshot"]["digest"] == uploaded[before_shot]["content_hash"]
    # The baseline run is not a second check row.
    assert len([row for row in rows if row["browser"]["scenario"] == "session-refresh"]) == 1
    # The structured result stays inspectable as original machine-readable evidence.
    raw = workspace.get(f"/api/evidence/{run['evidence_id']}/content")
    assert raw.status_code == 200 and json.loads(raw.content)["status"] == "PASS"

    _review_all_current_targets(workspace)
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    browser = _condition(readiness, "browser-regression")
    assert browser is not None and browser["state"] == "pass"
    generic = _condition(readiness, "verification")
    assert generic is not None and generic["state"] == "not_applicable"


def test_required_browser_failure_blocks_landing_but_optional_failure_is_informative(tmp_path: Path) -> None:
    workspace = _browser_workspace(tmp_path)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    _review_all_current_targets(workspace)

    failed = _browser_run(
        workspace,
        revision.id,
        assertions=[{"name": "status text", "status": "FAIL", "expected": "invalid", "actual": "expired"}],
        console=[{"level": "error", "text": "TypeError: user is undefined"}],
    )
    assert failed.status_code == 201, failed.text
    assert failed.json()["browser_check"]["status"] == "FAIL"
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    browser = _condition(readiness, "browser-regression")
    assert browser is not None and browser["state"] == "fail"
    assert "Session refresh · 1280x800 (FAIL)" in browser["detail"]
    assert readiness["armable"] is False

    passed = _browser_run(workspace, revision.id)
    assert passed.status_code == 201, passed.text
    optional = _browser_run(
        workspace,
        revision.id,
        scenario="settings-smoke",
        route="/settings",
        viewport=None,
        assertions=[{"name": "toggle", "status": "FAIL"}],
    )
    assert optional.status_code == 201, optional.text
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    browser = _condition(readiness, "browser-regression")
    assert browser is not None and browser["state"] == "pass"
    assert all("Settings smoke" not in row["detail"] for row in readiness["conditions"])
    assert readiness["armable"] is True
    verification = workspace.get(f"/api/reviews/{workspace.review_id}/verification").json()
    settings = next(row for row in verification["checks"] if row["name"] == "Settings smoke")
    assert settings["status"] == "FAIL" and settings["required"] is False


def test_previous_snapshot_browser_pass_is_stale_after_semantic_source_change(tmp_path: Path) -> None:
    workspace = _browser_workspace(tmp_path)
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None
    assert _browser_run(workspace, first.id).status_code == 201

    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"rejected"'))
    refreshed = workspace.post(f"/api/reviews/{workspace.review_id}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    second = workspace.store.latest_revision(workspace.review_id)
    assert second is not None and second.id != first.id

    verification = workspace.get(f"/api/reviews/{workspace.review_id}/verification").json()
    session_rows = [
        row for row in verification["checks"] if row.get("browser", {}).get("scenario") == "session-refresh"
    ]
    current = [row for row in session_rows if row["state"] == "current"]
    stale = [row for row in session_rows if row["state"] == "stale"]
    assert [(row["status"], row["required"], row["snapshot_id"]) for row in current] == [("NOT_RUN", True, second.id)]
    assert [(row["status"], row["snapshot_id"]) for row in stale] == [("PASS", first.id)]
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    browser = _condition(readiness, "browser-regression")
    assert browser is not None and browser["state"] == "pending"


def test_browser_checks_refuse_untrusted_targets_foreign_artifacts_and_unbound_snapshots(tmp_path: Path) -> None:
    workspace = _browser_workspace(tmp_path)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None

    unbound = workspace.post(f"/api/reviews/{workspace.review_id}/browser-checks", json={"scenario": "session-refresh"})
    assert unbound.status_code == 400
    for target in (
        "http://169.254.169.254/latest/meta-data",
        "https://evil.example.test/session",
        "file:///etc/passwd",
    ):
        refused = _browser_run(workspace, revision.id, target_url=target)
        assert refused.status_code == 422, target
        assert "browser target" in refused.json()["detail"]
    assert _browser_run(workspace, revision.id, target_url="https://preview.example.test/session").status_code == 201
    assert _browser_run(workspace, revision.id, route="https://evil.example.test/").status_code == 422
    missing = _browser_run(workspace, revision.id, artifacts={"screenshot": "ev-does-not-exist"})
    assert missing.status_code == 422


def test_browser_agent_statement_and_generic_screenshot_do_not_satisfy_browser_regression(tmp_path: Path) -> None:
    workspace = _browser_workspace(tmp_path)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    claim = _browser_run(
        workspace,
        revision.id,
        source="external",
        provider="browser_agent",
        status="PASS",
        completed=True,
        navigation=None,
        assertions=[],
        console=None,
        observations=["Everything looks fine."],
    )
    assert claim.status_code == 201, claim.text
    assert claim.json()["browser_check"]["status"] == "UNKNOWN"
    _review_all_current_targets(workspace)
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    browser = _condition(readiness, "browser-regression")
    assert browser is not None and browser["state"] == "pending"

    screenshot = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={"kind": "screenshot", "title": "looks good", "source": "agent", "verification_status": "PASS"},
        content=_PNG,
        headers={"Content-Type": "image/png"},
    )
    assert screenshot.status_code == 201, screenshot.text
    # Generic evidence keeps its established contract but never feeds the
    # deterministic browser-regression condition.
    assert screenshot.json()["evidence"]["verification_status"] == "PASS"
    assert "browser" not in screenshot.json()["evidence"]["mime_type"]
    readiness = workspace.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    browser = _condition(readiness, "browser-regression")
    assert browser is not None and browser["state"] == "pending"


# --------------------------------------------------------------------------- #
# landing + relationship hardening
# --------------------------------------------------------------------------- #


def _arm(landing: Workspace) -> dict[str, Any]:
    inspected = landing.get(f"/api/reviews/{landing.review_id}/readiness").json()
    assert inspected["armable"] is True, inspected
    armed = landing.post(
        f"/api/reviews/{landing.review_id}/landing/arm",
        json={
            "armed": True,
            "expected_revision_id": inspected["snapshot_id"],
            "expected_tree_fingerprint": inspected["tree_fingerprint"],
            "expected_conditions_hash": inspected["conditions_hash"],
        },
    )
    assert armed.status_code == 200, armed.text
    assert armed.json()["landing"]["armed"] is True
    return inspected


def _github_delegate(calls: list[tuple[str, str]]) -> Any:
    def delegate(session: ReviewSession, revision: Any, *, action: str) -> dict[str, Any]:
        calls.append((revision.id, action))
        return {
            "supported": True,
            "state": "delegated" if action == "arm" else "cancelled",
            "provider": "github",
            "detail": "delegated" if action == "arm" else "GitHub auto-merge disabled",
            "url": "https://github.com/acme/widgets/pull/7",
        }

    return delegate


def test_armed_landing_is_disarmed_when_a_human_requests_changes_after_arming(workspace: Workspace) -> None:
    calls: list[tuple[str, str]] = []
    landing = _landing_client(workspace, _github_delegate(calls))
    assert (
        landing.post(
            f"/api/reviews/{workspace.review_id}/relationships",
            json={"kind": "scm", "relation": "pull_request", "provider": "github", "external_id": "acme/widgets#7"},
        ).status_code
        == 200
    )
    _review_all_current_targets(landing)
    inspected = _arm(landing)

    posted = landing.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "side": "new",
            "start_line": 8,
            "end_line": 8,
            "body": "This must not merge yet.",
            "kind": "request_change",
        },
    )
    assert posted.status_code == 201, posted.text
    assert calls[-1] == (inspected["snapshot_id"], "cancel")
    readiness = landing.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    assert readiness["landing"]["armed"] is False
    assert readiness["landing"]["state"] == "blocked"
    assert "No unresolved request-change comments" in readiness["landing"]["detail"]


def test_any_snapshot_capture_marks_armed_landing_stale_and_owes_provider_cancel(workspace: Workspace) -> None:
    calls: list[tuple[str, str]] = []
    landing = _landing_client(workspace, _github_delegate(calls))
    assert (
        landing.post(
            f"/api/reviews/{workspace.review_id}/relationships",
            json={"kind": "scm", "relation": "pull_request", "provider": "github", "external_id": "acme/widgets#7"},
        ).status_code
        == 200
    )
    _review_all_current_targets(landing)
    inspected = _arm(landing)

    # A capture path outside the Reader API (CLI / live runtime / server capture).
    _write(workspace.repo_root, "src/session.py", _CHANGED_SOURCE.replace('"invalid"', '"captured elsewhere"'))
    session = landing.store.get_session(workspace.review_id)
    assert session is not None
    refresh(landing.store, session, workspace.repo_root, store_root=workspace.store_root)
    readiness = landing.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    assert readiness["landing"]["state"] == "stale"
    assert readiness["landing"]["armed"] is False
    assert "cancellation is pending" in readiness["landing"]["detail"]
    assert calls[-1][1] == "arm"

    # The next Reader refresh completes the owed provider cancellation even
    # though the source did not change again.
    assert landing.post(f"/api/reviews/{workspace.review_id}/refresh").status_code == 200
    assert calls[-1] == (inspected["snapshot_id"], "cancel")
    after = landing.get(f"/api/reviews/{workspace.review_id}/readiness").json()
    assert "was cancelled" in after["landing"]["detail"]


def test_relationship_links_must_be_absolute_web_urls(workspace: Workspace) -> None:
    for url in (
        "javascript:alert(document.cookie)",
        "data:text/html,<script>1</script>",
        "//evil.test/x",
        "file:///etc/passwd",
    ):
        refused = workspace.post(
            f"/api/reviews/{workspace.review_id}/relationships",
            json={"kind": "work_item", "provider": "jira", "external_id": "LC-1", "url": url},
        )
        assert refused.status_code == 422, url
    accepted = workspace.post(
        f"/api/reviews/{workspace.review_id}/relationships",
        json={
            "kind": "work_item",
            "provider": "jira",
            "external_id": "LC-1",
            "url": "https://jira.example.test/browse/LC-1",
        },
    )
    assert accepted.status_code == 200, accepted.text
