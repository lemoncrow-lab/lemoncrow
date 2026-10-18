"""Review triage through the Reader API: projection, policy order, audited overrides (Phase 9)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.review_triage import ai_finding_evidence, record_finding_evidence
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence
from lemoncrow.pro.capabilities.review.sources.local import (
    annotate_machine_finding,
    open_or_create_session,
    snapshot_revision,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "t" * 32
PORT = 45997
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

_SOURCE = "def authorize(user):\n    return user.is_admin\n"
_CHANGED = "def authorize(user):\n    if user is None:\n        return True\n    return user.is_admin\n"
_CRITICAL = "review:\n  effort: quick\n  focus:\n    - paths: ['src/**']\n      criticality: critical\n"


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str, offset: int) -> None:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000 + offset, 0)
    repo.create_commit("HEAD", signature, signature, message, tree, parents)


def _review(
    tmp_path: Path,
    *,
    base_config: str = "",
    head_config: str | None = None,
    base_files: dict[str, str] | None = None,
    head_files: dict[str, str] | None = None,
    intelligence_policy: Any = None,
) -> SimpleNamespace:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    repo = pygit2.init_repository(str(repo_root), initial_head="main")
    _write(repo_root, "src/auth.py", _SOURCE)
    _write(repo_root, "pyproject.toml", "[project]\nname = 'fixture'\n")
    for rel, text in (base_files or {}).items():
        _write(repo_root, rel, text)
    if base_config:
        _write(repo_root, ".lemoncrow/review.yaml", base_config)
    _commit(repo, "base", 0)
    base_sha = str(repo.head.target)
    _write(repo_root, "src/auth.py", _CHANGED)
    for rel, text in (head_files or {}).items():
        _write(repo_root, rel, text)
    if head_config is not None:
        _write(repo_root, ".lemoncrow/review.yaml", head_config)
    _commit(repo, "change", 1)

    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, base="HEAD~1", head="HEAD")
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    store.publish_snapshot(session.id, revision.id)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=repo_root,
        port=PORT,
        intelligence_policy=intelligence_policy,
    )
    client = TestClient(app, base_url=BASE, raise_server_exceptions=False)
    return SimpleNamespace(
        client=client,
        store=store,
        store_root=store_root,
        repo=repo,
        repo_root=repo_root,
        review_id=session.id,
        revision_id=revision.id,
        base_sha=base_sha,
    )


def _next_snapshot(review: SimpleNamespace) -> str:
    """Commit one more change and capture it as the Review's next Snapshot (no checks re-run)."""

    _write(review.repo_root, "src/auth.py", _CHANGED + "\n# follow-up\n")
    _commit(review.repo, "follow-up", 2)
    rng = resolve_rev_range(review.repo_root, base=review.base_sha, head="HEAD")
    build = build_review_packet_with_blobs(review.repo_root, rng, store_root=review.store_root, with_patch_text=True)
    session = review.store.get_session(review.review_id)
    revision = snapshot_revision(
        review.store, session, review.repo_root, rng, store_root=review.store_root, build=build
    )
    review.store.publish_snapshot(review.review_id, revision.id)
    return str(revision.id)


def _get(review: SimpleNamespace, path: str) -> Any:
    response = review.client.get(f"/api/reviews/{review.review_id}/{path}", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _readiness(review: SimpleNamespace) -> Any:
    """Readiness without its read-time stamp, so two reads compare by decision."""

    readiness = _get(review, "readiness")
    readiness.pop("evaluated_at", None)
    return readiness


def _override(review: SimpleNamespace, body: dict[str, Any], *, clear: bool = False) -> Any:
    path = "priority-override/clear" if clear else "priority-override"
    method = review.client.post if clear else review.client.put
    return method(f"/api/reviews/{review.review_id}/{path}", headers=HEADERS, json=body)


def _investigators(review: SimpleNamespace) -> dict[str, Any]:
    plan = _get(review, "run-plan")["plan"]
    return next(row for row in plan["entries"] if row["id"] == "ai.investigators")


def test_projection_carries_triage_policy_order_and_the_criticality_depth_floor(tmp_path: Path) -> None:
    review = _review(tmp_path, base_config=_CRITICAL)
    fresh = _get(review, "triage")["triage"]
    # Before any projection exists: missing evidence, never low.
    assert fresh["computed"] is False and fresh["evidence_state"] == "missing" and fresh["priority"] == "normal"

    triage = _get(review, "projection")["triage"]
    assert triage["criticality"] == "critical"
    assert triage["priority"] == "high"
    assert triage["severity_summary"]["highest"] is None
    assert triage["evidence_state"] in {"missing", "current"}
    criticality = next(row for row in triage["explanation"] if row["field"] == "criticality")
    assert criticality["origin"]["layer"] == "trusted_base_repository"
    assert criticality["origin"]["commit"] == review.base_sha
    stored = _get(review, "triage")["triage"]
    assert stored["computed"] is True and stored["criticality"] == "critical" and stored["priority"] == "high"

    targets = _get(review, "targets")["targets"]
    first = targets[0]
    assert first["priority"]["reasons"][0]["detail"] == "Critical · required by src/** policy"
    investigators = _investigators(review)
    assert investigators["depth"] == "thorough" and investigators["required"] is True


def test_a_head_side_criticality_change_does_not_apply_to_the_current_review(tmp_path: Path) -> None:
    review = _review(tmp_path, head_config=_CRITICAL)
    triage = _get(review, "projection")["triage"]
    assert triage["criticality"] == "normal"
    assert all(
        reason["code"] != "criticality_policy"
        for target in _get(review, "targets")["targets"]
        for reason in target["priority"]["reasons"]
    )
    assert _investigators(review)["depth"] != "thorough"
    future = _get(review, "run-plan")["future_settings"]
    assert future["changed"] is True
    assert future["proposed"]["focus"] == [{"paths": ["src/**"], "criticality": "critical"}]


def test_overrides_are_audited_human_only_and_never_reach_readiness_or_the_plan(tmp_path: Path) -> None:
    review = _review(tmp_path, base_config=_CRITICAL)
    computed = _get(review, "projection")["triage"]
    readiness = _readiness(review)
    digest = _get(review, "run-plan")["plan"]["digest"]

    refused = _override(
        review,
        {"expected_revision_id": review.revision_id, "level": "low", "reason": "ranked", "actor_type": "agent"},
    )
    assert refused.status_code == 403, refused.text
    assert _override(review, {"expected_revision_id": review.revision_id, "level": "low"}).status_code == 422
    assert _override(review, {"level": "low", "reason": "x"}).status_code == 400
    assert not [e for e in _get(review, "activity")["events"] if e["kind"].startswith("priority.")]

    response = _override(
        review,
        {"expected_revision_id": review.revision_id, "level": "low", "reason": "cosmetic only", "scope": "review"},
    )
    assert response.status_code == 200, response.text
    event = response.json()["event"]
    session = review.store.get_session(review.review_id)
    assert event["kind"] == "priority.override_set"
    assert event["actor_type"] == "human" and event["actor_id"] == session.reviewer_id
    assert event["created_at"] and event["detail"]["reason"] == "cosmetic only"
    overridden = response.json()["triage"]
    assert overridden["priority"] == "low" and overridden["computed_priority"] == computed["priority"]
    assert overridden["criticality"] == "critical"
    assert overridden["override"]["actor_id"] == session.reviewer_id
    assert _get(review, "projection")["triage"]["priority"] == "low"

    # Readiness, merge eligibility and the run plan are untouched by priority.
    assert _readiness(review) == readiness
    assert _get(review, "run-plan")["plan"]["digest"] == digest
    assert _investigators(review)["depth"] == "thorough"

    cleared = _override(review, {"expected_revision_id": review.revision_id, "reason": "back to computed"}, clear=True)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["triage"]["priority"] == computed["priority"]
    kinds = [e["kind"] for e in _get(review, "activity")["events"] if e["kind"].startswith("priority.")]
    assert kinds == ["priority.override_set", "priority.override_cleared"]


# --------------------------------------------------------------------------- #
# item 11: the finding threshold changes surfacing only
# --------------------------------------------------------------------------- #


def test_the_finding_threshold_changes_surfacing_only_never_readiness_counts_or_the_plan(tmp_path: Path) -> None:
    threshold = {"value": "suggestions"}
    review = _review(
        tmp_path,
        intelligence_policy=lambda _session, _revision: {
            "behavior": {"effort": {"effective_value": "auto", "origin": {"layer": "built_in_default"}}},
            "triage": {
                "layers": [
                    {
                        "origin": {"layer": "organization", "source": "org review policy"},
                        "settings": {"finding_threshold": threshold["value"]},
                    }
                ]
            },
        },
    )
    session = review.store.get_session(review.review_id)
    revision = review.store.latest_revision(review.review_id)
    ids: dict[str, str] = {}
    for line, category in ((2, "performance"), (3, "security"), (4, "")):
        annotation, _created = annotate_machine_finding(
            review.store,
            session,
            revision,
            path="src/auth.py",
            start_line=line,
            end_line=line,
            body=f"finding on line {line}",
            title=f"finding {category or 'unrated'}",
            created_by="investigator",
            created_by_actor="agent",
            source="ai_review",
            source_id=f"investigator-{line}",
            new_text=_CHANGED,
        )
        ids[category or "unrated"] = annotation.id
        if category:
            record_finding_evidence(
                review.store,
                review_id=review.review_id,
                revision_id=revision.id,
                rows=[
                    ai_finding_evidence(
                        annotation.id, finding_id=f"f{line}", category=category, target_ids=(), title=category
                    )
                ],
            )

    def read(value: str) -> tuple[dict[str, Any], Any, Any, int]:
        threshold["value"] = value
        triage = _get(review, "projection")["triage"]
        annotations = _get(review, "annotations")["annotations"]
        return triage, _readiness(review), _get(review, "run-plan")["plan"]["digest"], len(annotations)

    base_triage, readiness, digest, count = read("suggestions")
    assert base_triage["hidden_finding_ids"] == []
    expected_hidden = {
        "medium": [],
        "high": [ids["performance"]],
        "critical": sorted([ids["performance"], ids["security"]]),
    }
    for value, hidden in expected_hidden.items():
        triage, at_threshold, plan_digest, annotation_count = read(value)
        assert triage["finding_threshold"]["value"] == value
        assert sorted(triage["hidden_finding_ids"]) == hidden
        assert ids["unrated"] not in triage["hidden_finding_ids"]  # unrated is always shown
        # Readiness, the run plan, the annotation list and priority are identical at every threshold.
        assert at_threshold == readiness
        assert plan_digest == digest
        assert annotation_count == count
        summary = triage["severity_summary"]
        assert (summary["p1"], summary["p2"], summary["unrated"]) == (1, 1, 1)
        assert summary["below_threshold"] == len(hidden)
        assert triage["priority"] == base_triage["priority"] == "high"


# --------------------------------------------------------------------------- #
# item 3: behavior and vulnerability severities through the projection
# --------------------------------------------------------------------------- #


def _uv_lock(cryptography: str) -> str:
    return f"""version = 1

[[package]]
name = "cryptography"
version = "{cryptography}"
source = {{ registry = "https://pypi.org/simple" }}
"""


def _scan(review: SimpleNamespace, revision_id: str, rows: list[dict[str, Any]]) -> None:
    evidence_id = f"ev-scan-{revision_id[-8:]}"
    key, digest, size = review.store.write_evidence_artifact(
        review.review_id, revision_id, evidence_id, json.dumps(rows).encode("utf-8")
    )
    review.store.add_evidence(
        ReviewEvidence(
            id=evidence_id,
            review_id=review.review_id,
            revision_id=revision_id,
            kind="vulnerability_scan",
            title="image scan",
            artifact_path=key,
            content_hash=digest,
            bytes=size,
            mime_type="application/json",
            source="external",
            source_ref="Artifact Analysis",
            detail=json.dumps({"artifact_digest": "sha256:" + "a" * 64, "observation_source": "Artifact Analysis"}),
        )
    )


def _advisory(name: str, package: str, severity: str) -> dict[str, Any]:
    return {
        "noteName": name,
        "vulnerability": {
            "effectiveSeverity": severity,
            "packageIssue": [{"affectedPackage": {"packageName": package}}],
        },
    }


def test_vulnerability_advisories_on_changed_dependencies_count_until_a_snapshot_is_not_rescanned(
    tmp_path: Path,
) -> None:
    review = _review(
        tmp_path,
        base_files={"uv.lock": _uv_lock("45.0.4")},
        head_files={"uv.lock": _uv_lock("46.0.1")},
    )
    dependencies = [target for target in _get(review, "targets")["targets"] if target["kind"] == "dependency"]
    assert any("cryptography" in target["label"] for target in dependencies)
    _scan(
        review,
        review.revision_id,
        [
            _advisory("CVE-2099-0001", "cryptography", "CRITICAL"),
            _advisory("CVE-2099-0002", "requests", "HIGH"),  # not a dependency this change touches
        ],
    )
    triage = _get(review, "projection")["triage"]
    summary = triage["severity_summary"]
    assert summary["by_source"]["vulnerability"]["p0"] == 1 and summary["p1"] == 0
    assert triage["priority"] == "urgent"
    reason = next(row["reason"] for row in triage["explanation"] if row["field"] == "severity")
    assert "1 vulnerability advisory" in reason

    # The next Snapshot was not rescanned: the advisory is stale, counted, and no longer drives priority.
    _next_snapshot(review)
    stale = _get(review, "projection")["triage"]
    assert stale["severity_summary"]["p0"] == 0
    assert stale["severity_summary"]["by_source"]["vulnerability"]["stale"] == 1
    assert stale["evidence_state"] == "stale" and stale["priority"] == "normal"


def test_behavior_regressions_count_until_a_rerun_no_longer_observes_them(tmp_path: Path) -> None:
    from tests.gateway.test_review_behavior_api import _CHANGED_SOURCE, _api_run, _workspace

    workspace = _workspace(tmp_path)
    _api_run(workspace, side="old", status_code=200, body={"orders": [1]})
    _api_run(workspace, side="new", status_code=500, body={"error": "boom"})
    targets = workspace.get("/behavior").json()["behavior"]["targets"]
    assert sorted((target["kind"], target["severity"], target["critical"]) for target in targets) == [
        ("api_response", "high", True),
        ("test_outcome", "high", True),
    ]
    triage = workspace.get("/projection").json()["triage"]
    behavior = triage["severity_summary"]["by_source"]["behavior"]
    assert behavior["p0"] == 0 and behavior["p1"] == 2  # critical/high is P1, never P0
    assert triage["priority"] == "high"
    assert any("2 behavior changes" in row["reason"] for row in triage["explanation"] if row["field"] == "severity")

    # Snapshot 2 re-ran nothing: stale, counted apart, no longer a priority input.
    workspace.snapshot(_CHANGED_SOURCE + "\n# comment\n")
    second = workspace.get("/projection").json()["triage"]
    assert second["severity_summary"]["p1"] == 0
    assert second["severity_summary"]["by_source"]["behavior"]["stale"] == 2
    assert second["evidence_state"] == "stale" and second["priority"] == "normal"

    # Re-run on Snapshot 2 with the regression fixed: resolved, neither current nor stale.
    _api_run(workspace, side="old", status_code=200, body={"orders": [1]})
    _api_run(workspace, side="new", status_code=200, body={"orders": [1]})
    fixed = workspace.get("/projection").json()["triage"]
    assert fixed["severity_summary"]["by_source"]["behavior"] == {
        "p0": 0,
        "p1": 0,
        "p2": 0,
        "suggestion": 0,
        "unrated": 0,
        "stale": 0,
    }
    assert fixed["evidence_state"] != "stale"
