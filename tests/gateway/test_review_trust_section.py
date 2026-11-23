"""Trust T2: the trust section on Review Status (``readiness.trust_section``).

The section normalizes the one readiness answer into items that carry their T1
validity reasons, and copies the composition's own Inbox workflow state and
next action. These tests pin what may never happen: a section that computes
its own state or owner, one that reads as ready without the workflow, and a
flag that changes the readiness answer when it is off.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.readiness import (
    TRUST_FLAG,
    EvidenceRef,
    ReadinessCondition,
    ReadinessResult,
    SnapshotProgress,
    built_in_policy_layer,
    policy_generation,
    trust_items,
    trust_mismatches,
    trust_mode,
    trust_section,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore
from tests.gateway.test_review_api import (
    BASE,
    PORT,
    TOKEN,
    Workspace,
    _no_astgrep_download,
    draft_workspace,
    workspace,
)

__all__ = ["_no_astgrep_download", "draft_workspace", "workspace"]

_WORKFLOW = {
    "state": "waiting_on_reviewer",
    "label": "Waiting on reviewer",
    "rule": "reviewer_turn",
    "explanation": "A reviewer holds the next action (1 reason(s))",
}
_NEXT = {
    "owner_ids": ["user_b"],
    "for_me": False,
    "my_reasons": [],
    "reasons": [{"principal_id": "user_b", "kind": "review_requested", "fact": "requested"}],
}


def _result(*conditions: ReadinessCondition) -> ReadinessResult:
    return ReadinessResult(
        review_id="rev_1",
        snapshot_id="snap_2",
        evaluated_at="2026-10-08T10:00:00Z",
        policy=policy_generation([built_in_policy_layer()]),
        conditions=conditions,
    )


_REVIEWED = ReadinessCondition(
    id="latest-snapshot-reviewed",
    label="Latest revision reviewed",
    state="pass",
    detail="Every part has current human judgment.",
    category="human_review",
    reason="satisfied",
)
_EXPIRED_CI = ReadinessCondition(
    id="verification",
    label="Required checks",
    state="unknown",
    detail="The CI observation passed its validity window.",
    owner="scm",
    category="verification",
    reason="evidence_expired",
    evidence_refs=(
        EvidenceRef(
            evidence_id="ev_ci",
            freshness_class="external",
            state="expired",
            source_revision="snap_2",
            expires_at="2026-10-08T09:00:00Z",
            reason="observed 3 hours ago",
            reason_code="observation_expired",
        ),
    ),
)
_POLICY = ReadinessCondition(
    id="policy-generation",
    label="Review policy",
    state="pending",
    detail="Approvals predate the current policy.",
    category="policy",
    reason="policy_changed",
)


# --------------------------------------------------------------------------- #
# the flag
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "mode"),
    [(None, "off"), ("", "off"), ("shadow", "shadow"), (" ON ", "on"), ("true", "off"), ("1", "off")],
)
def test_the_flag_defaults_off_and_an_unknown_value_is_off(
    monkeypatch: pytest.MonkeyPatch, value: str | None, mode: str
) -> None:
    if value is None:
        monkeypatch.delenv(TRUST_FLAG, raising=False)
    else:
        monkeypatch.setenv(TRUST_FLAG, value)
    assert trust_mode() == mode


# --------------------------------------------------------------------------- #
# items
# --------------------------------------------------------------------------- #


def test_items_keep_each_condition_its_action_and_its_validity_reasons() -> None:
    reviewed, ci, policy = trust_items(_result(_REVIEWED, _EXPIRED_CI, _POLICY))

    assert reviewed == {
        "key": "latest-snapshot-reviewed",
        "category": "human_review",
        "state": "pass",
        "satisfied": True,
        "label": "Latest revision reviewed",
        "reason": "satisfied",
        "owner": "lemoncrow",
        "evidence_refs": [],
        "action": "",
        "validity": [],
    }
    # The owner is the condition's authority, never a person; the action is the blocking-reason phrase.
    assert (ci["owner"], ci["action"], ci["evidence_refs"]) == ("scm", "Waiting for checks", ["ev_ci"])
    assert [(row["reason_code"], row["chain"], row["subject_ref"]) for row in ci["validity"]] == [
        ("condition_evidence_expired", ["condition_evidence_expired", "observation_expired"], "condition:verification"),
        ("observation_expired", ["observation_expired"], "evidence:ev_ci"),
    ]
    assert all(row["current_revision"] == "snap_2" for row in ci["validity"])
    assert policy["action"] == "Review policy changed"
    assert [row["reason_code"] for row in policy["validity"]] == ["policy_generation_changed"]


# --------------------------------------------------------------------------- #
# the section
# --------------------------------------------------------------------------- #


def test_the_section_copies_the_workflow_and_next_action_as_given() -> None:
    result = _result(_REVIEWED, _EXPIRED_CI)
    section = trust_section(result, workflow=_WORKFLOW, next_action=_NEXT)

    assert section["summary"] == {**_WORKFLOW, "source": "workflow", "ready": False, "blocker_count": 1}
    assert section["next"] == _NEXT
    assert section["next"] is not _NEXT  # a copy, so a caller cannot mutate the Inbox row through it
    assert (section["revision_id"], section["promotion_target"]) == ("snap_2", "merge")
    assert section["policy_generation"] == result.policy.digest
    assert section["valid_until"] == result.valid_until
    assert [item["key"] for item in section["items"]] == ["latest-snapshot-reviewed", "verification"]
    assert trust_mismatches(section) == []


@pytest.mark.parametrize(
    ("workflow", "next_action"),
    [
        (None, None),
        (_WORKFLOW, None),
        (None, _NEXT),
        ({**_WORKFLOW, "state": "ready"}, _NEXT),
        ({**_WORKFLOW, "state": ""}, _NEXT),
    ],
)
def test_without_a_known_workflow_the_section_is_unknown_never_ready(
    workflow: Mapping[str, Any] | None, next_action: Mapping[str, Any] | None
) -> None:
    section = trust_section(_result(_REVIEWED), workflow=workflow, next_action=next_action)

    assert section["summary"]["state"] == "unknown"
    assert section["summary"]["source"] == "unavailable"
    assert section["summary"]["ready"] is True  # readiness itself is reported, the workflow is not invented
    assert section["next"] is None
    assert trust_mismatches(section) == ["workflow_unavailable"]


@pytest.mark.parametrize(
    ("workflow", "conditions", "expected"),
    [
        ({**_WORKFLOW, "state": "ready_to_merge", "rule": "readiness_ready"}, (_REVIEWED,), []),
        (
            {**_WORKFLOW, "state": "ready_to_merge", "rule": "readiness_ready"},
            (_REVIEWED, _EXPIRED_CI),
            ["ready_without_readiness"],
        ),
        (
            {**_WORKFLOW, "state": "needs_decision", "rule": "readiness_stale"},
            (_REVIEWED,),
            ["readiness_record_not_current"],
        ),
        (
            {**_WORKFLOW, "state": "needs_decision", "rule": "readiness_unknown"},
            (_REVIEWED,),
            ["readiness_record_not_current"],
        ),
        ({**_WORKFLOW, "state": "needs_decision", "rule": "readiness_not_ready"}, (_EXPIRED_CI,), []),
    ],
)
def test_shadow_checks_name_each_inconsistency(
    workflow: Mapping[str, Any], conditions: tuple[ReadinessCondition, ...], expected: list[str]
) -> None:
    assert trust_mismatches(trust_section(_result(*conditions), workflow=workflow, next_action=_NEXT)) == expected


def test_the_digest_is_deterministic_and_covers_the_section() -> None:
    result = _result(_REVIEWED, _EXPIRED_CI, _POLICY)
    first = trust_section(result, workflow=_WORKFLOW, next_action=_NEXT)

    assert trust_section(result, workflow=dict(_WORKFLOW), next_action=dict(_NEXT)) == first
    other_owner = trust_section(result, workflow=_WORKFLOW, next_action={**_NEXT, "owner_ids": ["user_c"]})
    other_state = trust_section(result, workflow={**_WORKFLOW, "state": "needs_review"}, next_action=_NEXT)
    fewer_items = trust_section(_result(_REVIEWED, _EXPIRED_CI), workflow=_WORKFLOW, next_action=_NEXT)
    assert len({first["digest"], other_owner["digest"], other_state["digest"], fewer_items["digest"]}) == 4


# --------------------------------------------------------------------------- #
# Review Status
# --------------------------------------------------------------------------- #


def _status(
    workspace: Workspace, provider: Any = None, *, wired: bool = True, received: list[Any] | None = None
) -> tuple[Workspace, list[str]]:
    """A Reader whose composition supplies ``provider``'s workflow, recording each read.

    ``received`` collects the target progress each read hands the composition.
    """

    calls: list[str] = []

    def review_workflow(session: Any, as_of: datetime, progress: SnapshotProgress | None) -> Mapping[str, Any] | None:
        assert as_of.tzinfo is not None
        calls.append(str(session.id))
        if received is not None:
            received.append(progress)
        return provider(session) if provider is not None else {"workflow": _WORKFLOW, "next_action": _NEXT}

    store = ReviewStore(workspace.store_root)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        review_workflow=review_workflow if wired else None,
    )
    client = Workspace(
        TestClient(app, base_url=BASE), store, workspace.store_root, workspace.repo_root, workspace.review_id
    )
    return client, calls


def _readiness(workspace: Workspace) -> dict[str, Any]:
    response = workspace.get(f"/api/reviews/{workspace.review_id}/readiness")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


_UNSTABLE = ("evaluated_at",)


def _stable(body: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key not in _UNSTABLE}


def test_off_and_shadow_return_the_earlier_answer_and_shadow_only_logs(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, calls = _status(workspace)
    monkeypatch.delenv(TRUST_FLAG, raising=False)
    off = _readiness(client)
    assert "trust" not in off and calls == []

    monkeypatch.setenv(TRUST_FLAG, "shadow")
    with caplog.at_level(logging.INFO, logger="lemoncrow.review.trust"):
        shadow = _readiness(client)
    assert "trust" not in shadow
    assert _stable(shadow) == _stable(off)
    assert calls == [workspace.review_id]
    [line] = [record.getMessage() for record in caplog.records if record.name == "lemoncrow.review.trust"]
    assert line.startswith(f"review trust shadow review={workspace.review_id} revision={off['snapshot_id']} ")
    assert "state=waiting_on_reviewer rule=reviewer_turn" in line and "mismatches=none" in line


def test_the_trust_line_times_the_whole_section_including_the_workflow_read(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``build_ms`` is what the trust plan's 150 ms p95 budget is measured on (G9-SLO trust_section_overhead)."""

    def slow(_session: Any) -> Mapping[str, Any]:
        time.sleep(0.05)
        return {"workflow": _WORKFLOW, "next_action": _NEXT}

    client, _calls = _status(workspace, slow)
    monkeypatch.setenv(TRUST_FLAG, "shadow")
    with caplog.at_level(logging.INFO, logger="lemoncrow.review.trust"):
        _readiness(client)
    [line] = [record.getMessage() for record in caplog.records if record.name == "lemoncrow.review.trust"]
    built = re.search(r" mismatches=none digest=[0-9a-f]{64} build_ms=([0-9]+\.[0-9])$", line)
    assert built is not None, line
    assert float(built.group(1)) >= 50.0


_WORKFLOW_COUNTS = ("target_count", "reviewed", "unreviewed", "changed_since_review", "unknown")


def test_the_workflow_read_is_handed_this_reads_own_target_progress(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composition gets the progress this Status read derived, for the Snapshot it evaluated.

    A local Inbox row counts targets; handing it these counts is what keeps the
    section inside its budget on a 2,000-target Review (it would otherwise derive
    every target again after each mark).
    """

    received: list[Any] = []
    client, _calls = _status(workspace, received=received)
    monkeypatch.setenv(TRUST_FLAG, "on")
    body = _readiness(client)
    reader = client.get(f"/api/reviews/{workspace.review_id}/targets").json()["progress"]
    [handed] = received
    assert isinstance(handed, SnapshotProgress)
    assert handed.revision_id == body["snapshot_id"] == body["trust"]["revision_id"]
    assert handed.counts["target_count"] > 0
    assert {key: handed.counts[key] for key in _WORKFLOW_COUNTS} == {key: reader[key] for key in _WORKFLOW_COUNTS}


def test_an_unreadable_packet_hands_no_progress(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Coarser targets re-derived without the packet are not the Inbox's counts: the composition derives its own."""

    received: list[Any] = []
    client, _calls = _status(workspace, received=received)
    monkeypatch.setenv(TRUST_FLAG, "on")
    monkeypatch.setattr(ReviewStore, "read_packet_artifact", lambda _self, _revision: None)
    body = _readiness(client)
    assert ("readiness-error:review_progress", "unknown") in [(row["id"], row["state"]) for row in body["conditions"]]
    assert received == [None]


def test_on_adds_the_section_built_from_the_same_answer(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _status(workspace)
    monkeypatch.setenv(TRUST_FLAG, "on")
    body = _readiness(client)

    trust = body["trust"]
    assert calls == [workspace.review_id]
    assert trust["summary"]["state"] == "waiting_on_reviewer" and trust["next"] == _NEXT
    assert trust["revision_id"] == body["snapshot_id"]
    assert trust["policy_generation"] == body["policy_generation"]["digest"]
    assert trust["valid_until"] == body["valid_until"]
    assert trust["summary"]["ready"] is body["ready"]
    assert trust["summary"]["blocker_count"] == body["blocker_count"]
    assert [(item["key"], item["state"]) for item in trust["items"]] == [
        (row["id"], row["state"]) for row in body["conditions"]
    ]
    actions = {row["id"]: row["action"] for row in body["blocking_reasons"]}
    assert {item["key"]: item["action"] for item in trust["items"] if not item["satisfied"]} == actions
    # The section is not part of what a person arms against.
    monkeypatch.setenv(TRUST_FLAG, "off")
    assert _readiness(client)["conditions_hash"] == body["conditions_hash"]


def test_only_review_status_carries_the_section(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

    client, calls = _status(workspace)
    monkeypatch.setenv(TRUST_FLAG, "on")
    snapshot_id = _readiness(client)["snapshot_id"]
    calls.clear()
    client.store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=workspace.review_id,
            kind="landing",
            relation="merge_when_ready",
            title="Merge when ready",
            status="armed",
            source="human",
        )
    )

    # Cancelling answers with the same live readiness, without the section.
    cancelled = client.post(
        f"/api/reviews/{workspace.review_id}/landing/cancel", json={"expected_revision_id": snapshot_id}
    )

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["landing"]["state"] == "cancelled"
    assert "trust" not in cancelled.json() and calls == []


def test_a_failed_or_missing_workflow_read_is_unknown_and_readiness_still_answers(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_session: Any) -> Mapping[str, Any]:
        raise RuntimeError("inbox read failed")

    monkeypatch.setenv(TRUST_FLAG, "on")
    for client in (
        _status(workspace, broken)[0],
        _status(workspace, lambda _s: None)[0],
        _status(workspace, wired=False)[0],
    ):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="lemoncrow.review.trust"):
            trust = _readiness(client)["trust"]
        assert trust["summary"]["state"] == "unknown" and trust["next"] is None
        [line] = [record for record in caplog.records if record.name == "lemoncrow.review.trust"]
        assert line.levelno == logging.WARNING and "mismatches=workflow_unavailable" in line.getMessage()
