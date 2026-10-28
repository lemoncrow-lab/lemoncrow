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


def test_real_snapshot_sensitive_paths_lead_reading_list_without_finding_or_extra_model_depth(tmp_path: Path) -> None:
    """Capture two actual commits, serve the Reader API, then inspect its canonical queue.

    The auth/payment edit warrants human attention, not a synthetic bug, policy
    blocker, extra provider spend or fabricated verification.
    """
    review = _review(
        tmp_path,
        base_files={
            "src/payments/charge.py": "def charge(amount):\n    return amount\n",
            "src/catalog/search.py": "def search():\n    return ['a']\n",
        },
        head_files={
            "src/payments/charge.py": "def charge(amount):\n    return max(0, amount)\n",
            "src/catalog/search.py": "def search():\n    return ['a', 'b']\n",
        },
    )
    rows = _get(review, "targets")["targets"]
    by_path = {}
    for row in rows:
        by_path.setdefault(row["path"], []).append(row)
    for path in ("src/auth.py", "src/payments/charge.py"):
        assert path in by_path, f"Missing actually changed source: {path}"
        assert any(
            row["priority"]["attention"] == "review_first"
            and any(
                reason["code"] == "sensitive_path_attention" and "not a verified finding" in reason["detail"]
                for reason in row["priority"]["reasons"]
            )
            for row in by_path[path]
        )
    assert all(row["priority"]["attention"] != "review_first" for row in by_path["src/catalog/search.py"])
    assert _get(review, "projection")["counts"]["review_first"] > 0
    assert _investigators(review)["depth"] != "thorough"
    overview_response = review.client.get(f"/api/reviews/{review.review_id}", headers=HEADERS)
    assert overview_response.status_code == 200, overview_response.text
    overview = overview_response.json()
    assert not any(row["status"] == "PASS" for row in overview["evidence"])


def test_actual_command_outcomes_are_snapshot_pinned_in_reader_overview(tmp_path: Path) -> None:
    """Execute real commands, persist their observed results, then advance the code Snapshot."""
    import subprocess
    import sys

    review = _review(tmp_path)
    revision = review.store.latest_revision(review.review_id)
    assert revision is not None
    commands = [
        ("python verify-valid", [sys.executable, "-c", "print('works')"]),
        ("python verify-failing", [sys.executable, "-c", "raise SystemExit(7)"]),
    ]
    observed = []
    for name, command in commands:
        run = subprocess.run(command, text=True, capture_output=True, check=False)
        observed.append((name, run.returncode))
        review.store.add_evidence(
            ReviewEvidence(
                id="",
                review_id=review.review_id,
                revision_id=revision.id,
                kind="verification",
                title=name,
                source="test",
                source_ref="manual-integration:" + name,
                verification_status="PASS" if run.returncode == 0 else "FAIL",
                detail=f"command={' '.join(command)}; exit={run.returncode}; output={run.stdout.strip() or run.stderr.strip()}",
            ),
        )
    assert observed == [("python verify-valid", 0), ("python verify-failing", 7)]
    before_response = review.client.get(f"/api/reviews/{review.review_id}", headers=HEADERS)
    assert before_response.status_code == 200
    before = before_response.json()
    evidence = {row["name"]: row for row in before["evidence"]}
    assert evidence["python verify-valid"]["status"] == "PASS"
    assert "exit=0" in evidence["python verify-valid"]["detail"]
    assert evidence["python verify-failing"]["status"] == "FAIL"
    assert "exit=7" in evidence["python verify-failing"]["detail"]
    assert before["brief"]["verification"]["pass"] >= 1
    assert before["brief"]["verification"]["fail"] >= 1

    _next_snapshot(review)
    after_response = review.client.get(f"/api/reviews/{review.review_id}", headers=HEADERS)
    assert after_response.status_code == 200
    after = after_response.json()
    assert not {"python verify-valid", "python verify-failing"}.intersection(
        {row["name"] for row in after["evidence"]}
    ), "A command run for the earlier Snapshot must not prove the new one"


def test_real_review_brief_surfaces_sourced_decision_questions_and_links_to_code(tmp_path: Path) -> None:
    """A real Git Snapshot produces the same prompts in the Brief and canonical queue.

    This must not manufacture human decisions, findings, tests, or required checks.
    """
    review = _review(
        tmp_path,
        base_files={
            "db/migrations/004_billing.sql": "ALTER TABLE invoices ADD COLUMN memo TEXT;\n",
            "package.json": '{"dependencies":{"example":"1"}}\n',
            "schema/service.proto": "message Customer { string name = 1; }\n",
            ".github/workflows/deploy.yml": "name: deploy\non: [push]\n",
            "src/catalog/items.py": "def catalog():\n    return [1]\n",
        },
        head_files={
            "db/migrations/004_billing.sql": "ALTER TABLE invoices ADD COLUMN memo VARCHAR(100);\n",
            "package.json": '{"dependencies":{"example":"2"}}\n',
            "schema/service.proto": "message Customer { string name = 1; int32 age = 2; }\n",
            ".github/workflows/deploy.yml": "name: deploy\non: [workflow_dispatch]\n",
            "src/catalog/items.py": "def catalog():\n    return [1, 2]\n",
        },
    )
    projection = _get(review, "projection")
    questions = {row["id"]: row for row in projection["questions"] if row["id"].startswith("q:structural:")}
    assert set(questions) == {
        "q:structural:migration",
        "q:structural:dependencies",
        "q:structural:contract",
        "q:structural:release",
    }, questions
    assert all(row["origin"] == "lemoncrow" and row["target_ids"] for row in questions.values())
    assert all("not a verified defect" in row["detail"] for row in questions.values())
    targets = _get(review, "targets")["targets"]
    owners = {row["target_id"]: row for row in targets}
    for question in questions.values():
        assert all(owners[target_id]["priority"]["attention"] == "review_first" for target_id in question["target_ids"])
    assert all(
        row["priority"]["attention"] != "review_first" for row in targets if row["path"] == "src/catalog/items.py"
    )
    assert projection["decisions"] == [], "Only a human/author may propose an actual decision"
    # Migration/release changes can independently require deep evaluation under existing policy.
    overview = review.client.get(f"/api/reviews/{review.review_id}", headers=HEADERS).json()
    assert not any(row["status"] == "PASS" for row in overview["evidence"])


def test_review_evaluation_timeline_is_snapshot_scoped_and_reports_untrusted_results(tmp_path: Path) -> None:
    """Read real Review history: trusted check, untrusted claim, next Snapshot, no phantom passes."""
    review = _review(tmp_path)
    original = review.revision_id
    review.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=review.review_id,
            revision_id=original,
            kind="verification",
            title="Unit tests",
            source="test",
            source_ref="eval-trusted-1",
            verification_status="PASS",
            attestation_class="lemoncrow_verification",
            stable_key="pytest tests/unit",
            detail="Observed on first Snapshot",
        )
    )
    review.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=review.review_id,
            revision_id=original,
            kind="verification",
            title="Agent says tests passed",
            source="human",
            source_ref="eval-untrusted-1",
            verification_status="PASS",
            stable_key="untrusted-check",
            detail="self-reported",
        )
    )
    unauthenticated = review.client.get(f"/api/reviews/{review.review_id}/evaluations")
    assert unauthenticated.status_code in {401, 403}, unauthenticated.text
    before = _get(review, "evaluations")
    assert before["selected_revision_id"] == original
    assert before["current"]["counts"] == {"pass": 1, "fail": 0, "not_run": 0, "unknown": 0}
    assert before["current"]["state"] == "observed"
    assert any(row["status"] == "REPORTED" for row in before["current"]["receipts"])
    assert before["current"]["configuration_comparable"] is False
    assert not any("score" in row for row in before["snapshots"])

    second = _next_snapshot(review)
    after = _get(review, "evaluations")
    assert after["selected_revision_id"] == second
    assert [row["revision_id"] for row in after["snapshots"]] == [second, original]
    assert after["current"]["state"] == "unverified"
    assert after["current"]["counts"]["pass"] == 0
    assert after["snapshots"][1]["counts"]["pass"] == 1
    response = review.client.get(f"/api/reviews/{review.review_id}/evaluations?revision_id={original}", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["selected_revision_id"] == original
    assert len(response.json()["snapshots"]) == 1
    projected = _get(review, "projection")
    assert projected["evaluation_timeline"]["current"]["revision_id"] == second


def test_executed_commands_have_snapshot_receipts_and_comparable_regression(tmp_path: Path) -> None:
    """Real subprocess exit codes -> existing evidence artifact -> 2-Snapshot API regression."""
    import subprocess
    import sys

    from lemoncrow.pro.capabilities.review.runtime_validation import (
        ValidationRequest,
        ValidationScenario,
        persist_validation_record,
    )

    review = _review(tmp_path)
    scenario = ValidationScenario(
        id="critical-path-test", title="Critical path tests", provider="command", surface_id="test", runtime_id="python"
    )

    def execute(revision_id: str, exit_code: int) -> None:
        # The observed command is real, never browser fixture injection.
        process = subprocess.run(
            [sys.executable, "-c", f"raise SystemExit({exit_code})"], capture_output=True, check=False
        )
        assert process.returncode == exit_code
        request = ValidationRequest(
            review_id=review.review_id,
            revision_id=revision_id,
            snapshot_identity="d" * 64,
            scenario_kind="recipe",
            scenario_id=scenario.id,
            side="new",
            viewport_key="",
            runtime_id="python",
            runtime_config_digest="a" * 64,
            trusted_policy_digest="b" * 64,
            scope_digest="c" * 64,
        )
        record = {
            "identity": request.identity,
            "request": request.to_wire(),
            "snapshot_id": revision_id,
            "scenario": scenario.to_wire(),
            "status": "PASS" if exit_code == 0 else "FAIL",
            "deterministic_checks": [{"name": "process exit code", "expected": 0, "actual": exit_code}],
            "duration_ms": 10,
        }
        revision = next(item for item in review.store.list_revisions(review.review_id) if item.id == revision_id)
        persist_validation_record(
            review.store,
            revision,
            request,
            scenario,
            record,
            executor_class="author_machine",
            expected_current_revision_id=revision_id,
        )

    first = review.revision_id
    execute(first, 0)
    second = _next_snapshot(review)
    execute(second, 7)
    timeline = _get(review, "evaluations")
    assert timeline["current"]["counts"]["fail"] == 1
    assert timeline["snapshots"][1]["counts"]["pass"] == 1
    assert timeline["current"]["configuration_comparable"] is True
    assert timeline["current"]["comparable_results"] == [
        {
            "scenario": scenario.id,
            "from": "PASS",
            "to": "FAIL",
            "change": "regression",
            "evidence_ids": [timeline["snapshots"][1]["receipts"][0]["id"], timeline["current"]["receipts"][0]["id"]],
        }
    ]
    receipt = timeline["current"]["receipts"][0]
    assert receipt["exit_code"] == 7
    assert receipt["executor_class"] == "author_machine"
    assert receipt["raw_attempt"] is False
    assert receipt["trust"] == "typed_observation"
    assert receipt["artifact_digest"] and len(receipt["artifact_digest"]) == 64
    assert "output" not in receipt and "env" not in receipt and "argv" not in receipt
    # An untrusted historical read cannot promote a prior passing result.
    assert all(row["revision_id"] == second for row in timeline["current"]["receipts"])


def test_evaluation_configuration_change_prevents_false_comparison(tmp_path: Path) -> None:
    """Two actual Snapshots with differently configured evaluations never form a comparable trend."""
    from lemoncrow.pro.capabilities.review.runtime_validation import (
        ValidationRequest,
        ValidationScenario,
        persist_validation_record,
    )

    review = _review(tmp_path)
    scenario = ValidationScenario(
        id="checkout", title="Checkout check", provider="command", surface_id="test", runtime_id="python"
    )

    def publish(revision_id: str, status: str, policy_digest: str) -> None:
        request = ValidationRequest(
            review_id=review.review_id,
            revision_id=revision_id,
            snapshot_identity="f" * 64,
            scenario_kind="recipe",
            scenario_id=scenario.id,
            side="new",
            viewport_key="",
            runtime_id="python",
            runtime_config_digest="a" * 64,
            trusted_policy_digest=policy_digest,
            scope_digest="c" * 64,
        )
        revision = next(row for row in review.store.list_revisions(review.review_id) if row.id == revision_id)
        persist_validation_record(
            review.store,
            revision,
            request,
            scenario,
            {
                "identity": request.identity,
                "request": request.to_wire(),
                "snapshot_id": revision.id,
                "status": status,
                "deterministic_checks": [{"name": "process exit code", "actual": 0 if status == "PASS" else 5}],
            },
            executor_class="author_machine",
            expected_current_revision_id=revision.id,
        )

    publish(review.revision_id, "PASS", "b" * 64)
    newer = _next_snapshot(review)
    publish(newer, "FAIL", "e" * 64)
    history = _get(review, "evaluations")
    assert history["current"]["counts"]["fail"] == 1
    assert history["snapshots"][1]["counts"]["pass"] == 1
    assert history["current"]["configuration_comparable"] is False
    assert history["current"]["comparable_results"] == []
    assert (
        "no confidence score" in history["limitations"][0].lower()
        or "not a new execution" in history["limitations"][0].lower()
    )


def test_missing_or_corrupt_execution_artifact_cannot_count_as_pass(tmp_path: Path) -> None:
    """A trusted row's PASS metadata alone never proves an unreadable execution receipt."""
    from lemoncrow.pro.capabilities.review.runtime_validation import RUNTIME_VALIDATION_MEDIA_TYPE

    review = _review(tmp_path)
    review.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=review.review_id,
            revision_id=review.revision_id,
            kind="verification",
            title="Build verification",
            source="test",
            source_ref="unreadable-exec",
            verification_status="PASS",
            attestation_class="lemoncrow_verification",
            stable_key="build",
            mime_type=RUNTIME_VALIDATION_MEDIA_TYPE,
            content_hash="a" * 64,
            artifact_path="",
            executor_class="author_machine",
        )
    )
    timeline = _get(review, "evaluations")
    assert timeline["current"]["counts"]["pass"] == 0
    assert timeline["current"]["state"] == "unverified"
    assert timeline["current"]["receipts"][0]["status"] == "UNVERIFIED"
    assert timeline["current"]["receipts"][0]["trust"] == "reported_only"


def test_rejected_nested_execution_cannot_be_counted_as_verified(tmp_path: Path) -> None:
    """Even typed metadata cannot rescue a malformed or forged execution record."""
    from lemoncrow.pro.capabilities.review.runtime_validation import (
        ValidationRequest,
        ValidationScenario,
        persist_validation_record,
    )

    review = _review(tmp_path)
    request = ValidationRequest(
        review_id=review.review_id,
        revision_id=review.revision_id,
        snapshot_identity="1" * 64,
        scenario_kind="recipe",
        scenario_id="critical-check",
        side="new",
        viewport_key="",
        runtime_id="python",
        runtime_config_digest="2" * 64,
        trusted_policy_digest="3" * 64,
        scope_digest="4" * 64,
    )
    scenario = ValidationScenario(
        id="critical-check", title="Critical check", provider="command", surface_id="test", runtime_id="python"
    )
    revision = review.store.latest_revision(review.review_id)
    assert revision is not None
    persist_validation_record(
        review.store,
        revision,
        request,
        scenario,
        {
            "identity": request.identity,
            "request": request.to_wire(),
            "snapshot_id": review.revision_id,
            "status": "PASS",
            "data": {"execution": {"executor_class": "managed_sandbox", "fake": "hello"}},
        },
        executor_class="author_machine",
        expected_current_revision_id=review.revision_id,
    )
    timeline = _get(review, "evaluations")
    assert timeline["current"]["counts"]["pass"] == 0
    (receipt,) = timeline["current"]["receipts"]
    assert receipt["execution_provenance"] == "rejected"
    assert receipt["trust"] == "reported_only"
    assert receipt["status"] == "UNVERIFIED"
    assert not receipt["environment_fingerprint"]


@pytest.mark.parametrize(
    ("outer_status", "stage_status", "exit_code", "expected_status", "expected_provenance", "variant"),
    [
        ("PASS", "failed", 7, "UNVERIFIED", "rejected", "normal"),
        ("PASS", "refused", None, "UNVERIFIED", "rejected", "normal"),
        ("PASS", "skipped", None, "UNVERIFIED", "rejected", "normal"),
        ("PASS", "passed", 7, "UNVERIFIED", "rejected", "normal"),
        ("PASS", "passed", 0, "PASS", "validated", "normal"),
        ("FAIL", "failed", 7, "FAIL", "validated", "normal"),
        ("FAIL", "passed", 0, "FAIL", "validated", "normal"),
        ("PASS", "passed", 0, "UNVERIFIED", "rejected", "fetch_only"),
        ("PASS", "passed", 0, "PASS", "validated", "skipped_fetch"),
        ("PASS", "passed", 0, "UNVERIFIED", "rejected", "duplicate_run"),
        ("FAIL", "failed", 7, "UNVERIFIED", "rejected", "duplicate_run"),
        ("PASS", "passed", 0, "UNVERIFIED", "rejected", "inner_status"),
        ("PASS", "passed", 0, "UNVERIFIED", "rejected", "malformed_stage"),
        ("PASS", "passed", 0, "PASS", "validated", "repeated_fetch"),
        ("FAIL", "failed", 7, "FAIL", "validated", "repeated_fetch"),
    ],
)
def test_execution_stage_cannot_be_laundered_into_outer_pass(
    tmp_path: Path,
    outer_status: str,
    stage_status: str,
    exit_code: int | None,
    expected_status: str,
    expected_provenance: str,
    variant: str,
) -> None:
    """Even a hash-valid inner receipt cannot certify a contradictory PASS."""
    from lemoncrow.pro.capabilities.review.execution import (
        EnvironmentFingerprint,
        ExecutionResult,
        ExecutionSubject,
        ExecutorClass,
        StageOutcome,
        VerificationEnvironment,
        canonical_digest,
    )
    from lemoncrow.pro.capabilities.review.runtime_validation import (
        ValidationRequest,
        ValidationScenario,
        persist_validation_record,
    )

    review = _review(tmp_path)
    request = ValidationRequest(
        review_id=review.review_id,
        revision_id=review.revision_id,
        snapshot_identity="1" * 64,
        scenario_kind="recipe",
        scenario_id="stage-proof",
        side="new",
        viewport_key="",
        runtime_id="python",
        runtime_config_digest="2" * 64,
        trusted_policy_digest="3" * 64,
        scope_digest="4" * 64,
    )
    scenario = ValidationScenario(
        id="stage-proof", title="Observed process", provider="command", surface_id="test", runtime_id="python"
    )
    run_stage = StageOutcome("run", stage_status, exit_code=exit_code)
    stages = (run_stage,)
    if variant == "fetch_only":
        stages = (StageOutcome("fetch", "passed", exit_code=0),)
    elif variant == "skipped_fetch":
        stages = (StageOutcome("fetch", "skipped"), run_stage)
    elif variant == "duplicate_run":
        stages = (run_stage, run_stage)
    elif variant == "repeated_fetch":
        stages = (StageOutcome("fetch", "passed", exit_code=0), StageOutcome("fetch", "skipped"), run_stage)
    execution = ExecutionResult(
        subject=ExecutionSubject(review_id=review.review_id, revision_id=review.revision_id, side="new"),
        executor_class=ExecutorClass.AUTHOR_MACHINE,
        fingerprint=EnvironmentFingerprint(image_digest="host:test"),
        verification=VerificationEnvironment(executor_class=ExecutorClass.AUTHOR_MACHINE, image_digest="host:test"),
        stages=stages,
    )
    wire = execution.to_wire()
    if variant == "inner_status":
        wire["status"] = "failed"
    elif variant == "malformed_stage":
        wire["stages"].append({"stage": "invented", "status": "passed"})
    if variant in {"inner_status", "malformed_stage"}:
        wire["result_digest"] = canonical_digest({key: value for key, value in wire.items() if key != "result_digest"})
    revision = review.store.latest_revision(review.review_id)
    assert revision is not None
    persist_validation_record(
        review.store,
        revision,
        request,
        scenario,
        {
            "identity": request.identity,
            "request": request.to_wire(),
            "snapshot_id": review.revision_id,
            "status": outer_status,
            "duration_ms": 1234,
            "deterministic_checks": [{"name": "process exit code", "actual": 0}],
            "data": {"execution": wire},
        },
        executor_class=ExecutorClass.AUTHOR_MACHINE,
        expected_current_revision_id=review.revision_id,
    )
    result = _get(review, "evaluations")["current"]
    assert len(result["receipts"]) == 1
    assert result["receipts"][0]["status"] == expected_status
    assert result["receipts"][0]["execution_provenance"] == expected_provenance
    assert result["counts"]["pass"] == (1 if expected_status == "PASS" else 0)
    receipt = result["receipts"][0]
    if expected_status == "UNVERIFIED":
        assert receipt["exit_code"] is None
        assert receipt["duration_ms"] is None
    else:
        assert receipt["duration_ms"] == 1234
    if expected_status == "UNVERIFIED" and outer_status == "PASS":
        from lemoncrow.pro.capabilities.review.verification import build_verification_projection

        main = build_verification_projection(
            packet=None,
            evidence=review.store.list_evidence(review.review_id),
            revision=revision,
            revisions=review.store.list_revisions(review.review_id),
        )
        assert main["checks"][0]["status"] == "UNKNOWN"
        assert review.store.list_evidence(review.review_id)[0].verification_status == "UNKNOWN"
    assert "command" not in receipt
    assert "output" not in receipt


def test_evaluation_timeline_counts_only_latest_verdict_per_check(tmp_path: Path) -> None:
    """Fresh reruns preserve history without counting the same check twice."""
    review = _review(tmp_path)
    for value in ("PASS", "FAIL", "PASS"):
        review.store.add_evidence(
            ReviewEvidence(
                id="",
                review_id=review.review_id,
                revision_id=review.revision_id,
                kind="verification",
                title="Unit",
                source="test",
                source_ref=f"retake-{value}-{len(review.store.list_evidence(review.review_id))}",
                stable_key="checks.recipe:unit",
                verification_status=value,
                attestation_class="lemoncrow_verification",
                executor_class="author_machine",
            )
        )
    history = _get(review, "evaluations")
    latest = history["current"]
    assert latest["counts"] == {"pass": 1, "fail": 0, "not_run": 0, "unknown": 0}
    receipts = [item for item in latest["receipts"] if item["scenario"] == "checks.recipe:unit"]
    assert len(receipts) == 3
    assert [item["status"] for item in receipts] == ["PASS", "FAIL", "PASS"]
    assert [item.get("superseded_by_newer_result", False) for item in receipts] == [False, True, True]
    assert "effective_outcomes" not in latest


def test_invalid_latest_verdict_cannot_resurrect_older_pass(tmp_path: Path) -> None:
    """A corrupt final result keeps the check unverified, not passing via an older retry."""
    from lemoncrow.pro.capabilities.review.runtime_validation import RUNTIME_VALIDATION_MEDIA_TYPE

    review = _review(tmp_path)
    review.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=review.review_id,
            revision_id=review.revision_id,
            kind="verification",
            title="Unit older",
            source="test",
            source_ref="old-verified",
            stable_key="checks.recipe:unit",
            verification_status="PASS",
            attestation_class="lemoncrow_verification",
            executor_class="author_machine",
        )
    )
    review.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=review.review_id,
            revision_id=review.revision_id,
            kind="verification",
            title="Unit newer unreadable",
            source="test",
            source_ref="new-corrupt",
            stable_key="checks.recipe:unit",
            verification_status="FAIL",
            mime_type=RUNTIME_VALIDATION_MEDIA_TYPE,
            content_hash="a" * 64,
            attestation_class="lemoncrow_verification",
            executor_class="author_machine",
        )
    )
    history = _get(review, "evaluations")["current"]
    assert history["counts"] == {"pass": 0, "fail": 0, "not_run": 0, "unknown": 0}
    assert history["state"] == "unverified"
    assert history["receipts"][0]["status"] == "UNVERIFIED"
    assert history["receipts"][1]["superseded_by_newer_result"] is True


def test_evaluation_comparison_uses_latest_repeated_verdict_not_stale_retry(tmp_path: Path) -> None:
    """Only the final verdict of a repeated evaluator may produce cross-Snapshot trends."""
    from lemoncrow.pro.capabilities.review.runtime_validation import (
        ValidationRequest,
        ValidationScenario,
        persist_validation_record,
    )

    review = _review(tmp_path)
    scenario = ValidationScenario(
        id="checkout", title="Checkout", provider="command", surface_id="test", runtime_id="python"
    )

    def publish(revision_id: str, status: str, token: str) -> None:
        request = ValidationRequest(
            review_id=review.review_id,
            revision_id=revision_id,
            snapshot_identity="f" * 64,
            scenario_kind="recipe",
            scenario_id=scenario.id,
            side="new",
            viewport_key="",
            runtime_id="python",
            runtime_config_digest="a" * 64,
            trusted_policy_digest="b" * 64,
            scope_digest="c" * 64,
        )
        revision = next(row for row in review.store.list_revisions(review.review_id) if row.id == revision_id)
        persist_validation_record(
            review.store,
            revision,
            request,
            scenario,
            {
                "identity": request.identity,
                "request": request.to_wire(),
                "snapshot_id": revision.id,
                "status": status,
                "deterministic_checks": [{"name": "process exit code", "actual": 0 if status == "PASS" else 5}],
            },
            executor_class="author_machine",
            expected_current_revision_id=revision_id,
            fresh_ref=token,
        )

    original = review.revision_id
    for i, status in enumerate(("PASS", "FAIL", "PASS"), 1):
        publish(original, status, f"run-{i}")
    first = _get(review, "evaluations")["current"]
    assert first["counts"] == {"pass": 1, "fail": 0, "not_run": 0, "unknown": 0}
    assert len([row for row in first["receipts"] if row["status"] in {"PASS", "FAIL"}]) == 3
    next_revision = _next_snapshot(review)
    publish(next_revision, "FAIL", "next-run")
    latest = _get(review, "evaluations")
    assert latest["current"]["counts"]["fail"] == 1
    assert latest["current"]["configuration_comparable"] is True
    result = latest["current"]["comparable_results"]
    assert len(result) == 1
    assert result[0]["from"] == "PASS" and result[0]["to"] == "FAIL"
    assert result[0]["change"] == "regression"
