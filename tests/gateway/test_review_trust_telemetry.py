"""Trust plan §39.8 item 10: content-free observations of the trust projection.

``trust_telemetry`` turns one built trust section, or one run plan's
re-verification summary, into counts of codes that a composition may export as
operator metrics. These tests pin what an observation may hold (codes and
counts, never a Review, a person or text), that every built or failed section
and every run plan read is observed exactly once, and that observing never
changes what the read answers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review import readiness as readiness_module
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.readiness import TRUST_FLAG, trust_mismatches, trust_section
from lemoncrow.pro.capabilities.review.run_plan_reverification import BUCKETS
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.trust_telemetry import (
    RUN_PLAN,
    TRUST_SECTION,
    code,
    run_plan_observation,
    section_failure,
    section_observation,
)
from tests.gateway.test_review_api import (
    BASE,
    PORT,
    TOKEN,
    Workspace,
    _no_astgrep_download,
    draft_workspace,
    workspace,
)
from tests.gateway.test_review_run_plan_api import _get as _plan_get
from tests.gateway.test_review_run_plan_api import _hosted_policy
from tests.gateway.test_review_run_plan_api import _review as _plan_review
from tests.gateway.test_review_trust_section import _EXPIRED_CI, _NEXT, _POLICY, _REVIEWED, _WORKFLOW, _result

__all__ = ["_no_astgrep_download", "draft_workspace", "workspace"]

Observed = list[tuple[str, dict[str, Any]]]


# --------------------------------------------------------------------------- #
# what an observation holds
# --------------------------------------------------------------------------- #


def test_a_section_is_observed_as_items_and_validity_statements_by_state_and_reason() -> None:
    section = trust_section(_result(_REVIEWED, _EXPIRED_CI, _POLICY), workflow=_WORKFLOW, next_action=_NEXT)

    assert section_observation(section, mode="shadow", mismatches=trust_mismatches(section), build_ms=1.5) == {
        "mode": "shadow",
        "outcomes": ["ok"],
        "build_ms": 1.5,
        "items": [
            {"state": "pass", "reason": "satisfied", "count": 1},
            {"state": "pending", "reason": "policy_changed", "count": 1},
            {"state": "unknown", "reason": "evidence_expired", "count": 1},
        ],
        "validity": [
            {"state": "expired", "reason": "condition_evidence_expired", "count": 1},
            {"state": "expired", "reason": "observation_expired", "count": 1},
            {"state": "reverify", "reason": "policy_generation_changed", "count": 1},
        ],
    }


def test_equal_items_are_counted_together() -> None:
    section = {
        "items": [
            {
                "state": "unknown",
                "reason": "evaluation_error",
                "validity": [{"state": "unknown", "reason_code": "a_b"}],
            },
            {
                "state": "unknown",
                "reason": "evaluation_error",
                "validity": [{"state": "unknown", "reason_code": "a_b"}],
            },
        ]
    }

    observed = section_observation(section, mode="on", mismatches=(), build_ms=0.0)

    assert observed["items"] == [{"state": "unknown", "reason": "evaluation_error", "count": 2}]
    assert observed["validity"] == [{"state": "unknown", "reason": "a_b", "count": 2}]


def test_each_failed_consistency_check_is_an_outcome_and_a_failed_build_is_an_error() -> None:
    section = trust_section(_result(_REVIEWED), workflow=None, next_action=None)
    unavailable = section_observation(section, mode="on", mismatches=trust_mismatches(section), build_ms=2.0)
    both = section_observation(
        section, mode="on", mismatches=["readiness_record_not_current", "ready_without_readiness"], build_ms=2.0
    )

    assert unavailable["outcomes"] == ["workflow_unavailable"]
    assert both["outcomes"] == ["readiness_record_not_current", "ready_without_readiness"]
    assert section_failure(mode="shadow", build_ms=3.0) == {
        "mode": "shadow",
        "outcomes": ["error"],
        "build_ms": 3.0,
        "items": [],
        "validity": [],
    }


def test_an_observation_never_carries_an_identifier_a_label_or_free_text() -> None:
    result = _result(_REVIEWED, _EXPIRED_CI, _POLICY)
    section = trust_section(result, workflow=_WORKFLOW, next_action=_NEXT)

    wire = json.dumps(section_observation(section, mode="on", mismatches=(), build_ms=1.0))

    for forbidden in (
        result.review_id,
        result.snapshot_id,
        "ev_ci",
        "user_b",
        section["digest"],
        result.policy.digest,
        "Required checks",
        "validity window",
        "observed 3 hours ago",
        "condition:verification",
    ):
        assert forbidden not in wire, forbidden


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ("carry_exact_key", "carry_exact_key"),
        ("x" * 64, "x" * 64),  # the longest code that is kept
        ("x" * 65, "other"),  # one past it
        ("the cited evidence is not on this revision", "other"),
        ("rev_01HZX-4", "other"),
        ("Carry", "other"),
        ("9lives", "other"),
        ("", "none"),
        (None, "none"),
        (7, "other"),
        ({"reason": "x"}, "other"),
    ],
)
def test_only_a_code_is_kept_as_a_label(value: object, label: str) -> None:
    assert code(value) == label


def test_a_state_or_mode_outside_its_vocabulary_is_other_never_passed_through() -> None:
    section = {
        "items": [
            {
                "state": "rev_1",
                "reason": "free text here",
                "validity": [{"state": "user_b", "reason_code": "Bad Code"}],
            },
            "not a row",
            {"state": "pass", "reason": "", "validity": "not rows"},
        ]
    }

    observed = section_observation(section, mode="rev_1", mismatches=["Some Text"], build_ms=-4.0)

    assert observed == {
        "mode": "other",
        "outcomes": ["other"],
        "build_ms": 0.0,
        "items": [{"state": "other", "reason": "other", "count": 1}, {"state": "pass", "reason": "none", "count": 1}],
        "validity": [{"state": "other", "reason": "other", "count": 1}],
    }


def _reverification(**rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {**{name: [] for name in BUCKETS}, **rows}


def test_a_run_plan_is_observed_as_buckets_check_dispositions_and_unknown_statuses() -> None:
    reverification = _reverification(
        run=[
            {"entry_id": "checks.tests", "recipe_id": "pytest", "state": "ran", "reason_code": "carry_inputs_changed"},
            {"entry_id": "checks.lint", "recipe_id": "ruff", "state": "ran"},
            {"entry_id": "checks.types", "recipe_id": "mypy", "state": "planned"},
            {"entry_id": "ai.investigators", "reason": "runs on every revision"},
        ],
        reuse=[
            {
                "entry_id": "checks.build",
                "kind": "carried",
                "reason_code": "carry_exact_key",
                "origin_revision_id": "r1",
            },
            {
                "entry_id": "checks.docs",
                "kind": "carried",
                "reason_code": "carry_exact_key",
                "origin_revision_id": "r1",
            },
            {"entry_id": "check:build", "kind": "ci_equivalent", "satisfied_by": {"run": "gh_991"}},
        ],
        unknown_broadened=[{"entry_id": "checks.e2e", "recipe_id": "e2e", "state": "ran", "coverage": "x"}],
        skip_not_affected=[{"entry_id": "checks.go", "reason": "no changed path is in its scope"}],
        not_run=[
            # A due check whose head never executed is its own disposition, never ``ran``.
            {
                "entry_id": "checks.tests",
                "recipe_id": "unit",
                "state": "not_run",
                "reason": "the Change edits Makefile, which defines this recipe",
                "reason_code": "head_definition_guard",
            },
            {"entry_id": "visual.screenshot", "status": "on_request", "reason": "runs only when requested"},
        ],
        unknown_not_run=[
            {"entry_id": "ai.hypotheses", "status": "limited_by_budget", "reason": "2 of 6 model calls left"},
            {"entry_id": "check:security:sast", "status": "not_available", "reason": "required · no executor"},
        ],
    )

    observed = run_plan_observation(reverification)

    assert observed == {
        "buckets": [
            {"bucket": "not_run", "count": 2},
            {"bucket": "reuse", "count": 3},
            {"bucket": "run", "count": 4},
            {"bucket": "skip_not_affected", "count": 1},
            {"bucket": "unknown_broadened", "count": 1},
            {"bucket": "unknown_not_run", "count": 2},
        ],
        "checks": [
            {"disposition": "carried", "reason": "carry_exact_key", "count": 2},
            {"disposition": "ci_equivalent", "reason": "none", "count": 1},
            {"disposition": "not_run", "reason": "head_definition_guard", "count": 1},
            {"disposition": "ran", "reason": "carry_inputs_changed", "count": 1},
            {"disposition": "ran", "reason": "none", "count": 2},
        ],
        "unknown": [{"status": "limited_by_budget", "count": 1}, {"status": "not_available", "count": 1}],
        "budget_limited": True,
    }
    wire = json.dumps(observed)
    for forbidden in ("checks.tests", "pytest", "r1", "gh_991", "model calls left", "no executor", "Makefile"):
        assert forbidden not in wire, forbidden


def test_a_run_plan_within_its_budget_is_not_budget_limited_and_an_unknown_bucket_is_ignored() -> None:
    observed = run_plan_observation(
        {
            "unknown_not_run": [{"entry_id": "check:sast", "status": "not_available"}],
            "rev_1": [{"entry_id": "x"}],
            "reuse": "not rows",
        }
    )

    assert observed == {
        "buckets": [{"bucket": "unknown_not_run", "count": 1}],
        "checks": [],
        "unknown": [{"status": "not_available", "count": 1}],
        "budget_limited": False,
    }


# --------------------------------------------------------------------------- #
# Review Status reports each section it builds
# --------------------------------------------------------------------------- #


def _reader(workspace: Workspace, observed: Observed | None, provider: Any = None, observer: Any = None) -> Workspace:
    def review_workflow(session: Any, _as_of: datetime, _progress: Any) -> Mapping[str, Any] | None:
        return provider(session) if provider is not None else {"workflow": _WORKFLOW, "next_action": _NEXT}

    def record(kind: str, observation: Mapping[str, Any]) -> None:
        assert observed is not None
        observed.append((kind, json.loads(json.dumps(observation))))

    store = ReviewStore(workspace.store_root)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        review_workflow=review_workflow,
        trust_observer=observer if observer is not None else (record if observed is not None else None),
    )
    return Workspace(
        TestClient(app, base_url=BASE), store, workspace.store_root, workspace.repo_root, workspace.review_id
    )


def _readiness(reader: Workspace) -> dict[str, Any]:
    response = reader.get(f"/api/reviews/{reader.review_id}/readiness")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _stable(body: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "evaluated_at"}


def test_each_built_section_is_reported_once_and_none_while_the_flag_is_off(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: Observed = []
    reader = _reader(workspace, observed)

    monkeypatch.delenv(TRUST_FLAG, raising=False)
    off = _readiness(reader)
    assert observed == []

    monkeypatch.setenv(TRUST_FLAG, "shadow")
    shadow = _readiness(reader)
    monkeypatch.setenv(TRUST_FLAG, "on")
    shown = _readiness(reader)

    assert _stable(shadow) == _stable(off)
    [(first_kind, first), (second_kind, second)] = observed
    assert (first_kind, second_kind) == (TRUST_SECTION, TRUST_SECTION)
    assert (first["mode"], second["mode"]) == ("shadow", "on")
    # What was reported is the section the read built, and nothing else.
    expected = section_observation(shown["trust"], mode="on", mismatches=(), build_ms=second["build_ms"])
    assert second == expected
    assert {key: value for key, value in first.items() if key not in ("mode", "build_ms")} == {
        key: value for key, value in expected.items() if key not in ("mode", "build_ms")
    }
    assert sum(row["count"] for row in second["items"]) == len(shown["trust"]["items"]) > 0
    assert second["build_ms"] > 0.0


def test_a_workflow_the_composition_cannot_supply_is_reported_and_the_read_still_answers(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_session: Any) -> Mapping[str, Any]:
        raise RuntimeError("inbox down")

    observed: Observed = []
    monkeypatch.setenv(TRUST_FLAG, "on")

    body = _readiness(_reader(workspace, observed, broken))

    assert body["trust"]["summary"]["state"] == "unknown"
    [(kind, observation)] = observed
    assert (kind, observation["outcomes"]) == (TRUST_SECTION, ["workflow_unavailable"])


def test_a_section_that_cannot_be_built_is_reported_as_an_error_and_fails_the_read_as_before(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("section defect")

    monkeypatch.setenv(TRUST_FLAG, "shadow")
    monkeypatch.setattr(readiness_module, "trust_section", broken)
    observed: Observed = []

    with pytest.raises(RuntimeError, match="section defect"):
        _readiness(_reader(workspace, None))  # without an observer: the behaviour being kept
    with pytest.raises(RuntimeError, match="section defect"):
        _readiness(_reader(workspace, observed))

    [(kind, observation)] = observed
    assert kind == TRUST_SECTION
    assert {key: value for key, value in observation.items() if key != "build_ms"} == {
        "mode": "shadow",
        "outcomes": ["error"],
        "items": [],
        "validity": [],
    }


def test_an_observer_that_raises_never_changes_the_read(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def failing(kind: str, _observation: Mapping[str, Any]) -> None:
        calls.append(kind)
        raise RuntimeError("metrics backend down")

    monkeypatch.setenv(TRUST_FLAG, "on")
    plain = _readiness(_reader(workspace, None))

    answered = _readiness(_reader(workspace, None, observer=failing))

    assert calls == [TRUST_SECTION]
    assert _stable(answered) == _stable(plain)


# --------------------------------------------------------------------------- #
# the run plan reports its re-verification summary
# --------------------------------------------------------------------------- #


def test_each_run_plan_read_reports_the_summary_it_returned(tmp_path: Path) -> None:
    observed: Observed = []
    review = _plan_review(
        tmp_path, trust_observer=lambda kind, row: observed.append((kind, json.loads(json.dumps(row))))
    )

    shown = _plan_get(review, "run-plan")
    assert shown.status_code == 200, shown.text

    [(kind, observation)] = observed
    assert kind == RUN_PLAN
    reverification = shown.json()["reverification"]
    assert observation == run_plan_observation(reverification)
    assert {row["bucket"]: row["count"] for row in observation["buckets"]} == {
        name: len(rows) for name, rows in reverification.items() if rows
    }
    assert observation["budget_limited"] is False


def test_a_budget_limited_run_plan_is_reported_as_budget_limited(tmp_path: Path) -> None:
    observed: Observed = []
    review = _plan_review(
        tmp_path,
        intelligence_policy=_hosted_policy(allowed=("managed",)),
        run_plan_usage=lambda _session, _revision: {"monthly_managed_usd": 5.0},
        trust_observer=lambda kind, row: observed.append((kind, dict(row))),
    )

    shown = _plan_get(review, "run-plan")
    assert shown.status_code == 200, shown.text

    [(_kind, observation)] = observed
    assert observation["budget_limited"] is True
    assert {"status": "limited_by_budget", "count": 1} in observation["unknown"]


def test_a_run_plan_read_answers_the_same_with_an_observer_that_raises(tmp_path: Path) -> None:
    calls: list[str] = []
    raising = False

    def observer(kind: str, _observation: Mapping[str, Any]) -> None:
        calls.append(kind)
        if raising:
            raise RuntimeError("metrics backend down")

    review = _plan_review(tmp_path, trust_observer=observer)
    plain = _plan_get(review, "run-plan")
    raising = True
    watched = _plan_get(review, "run-plan")

    assert calls == [RUN_PLAN, RUN_PLAN]
    assert plain.status_code == watched.status_code == 200
    assert watched.json() == plain.json()
