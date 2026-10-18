"""Landing decisions through the Review API (P10-LAND): auto-approval, break-glass, merge result, records."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review import auto_approval as aa
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.landing import landing_metadata, landing_relationship
from lemoncrow.pro.capabilities.review.landing_api import LandingAuthority, LandingDeps, gather_facts
from lemoncrow.pro.capabilities.review.landing_policy import AutoApprovePolicy, LandingPolicy
from lemoncrow.pro.capabilities.review.merge_result import MergeResultFacts, side_results
from lemoncrow.pro.capabilities.review.store import ReviewStore
from tests.gateway.test_review_api import (
    BASE,
    PORT,
    TOKEN,
    Workspace,
    _no_astgrep_download,
    _review_all_current_targets,
    draft_workspace,
    workspace,
)

__all__ = ["_no_astgrep_download", "draft_workspace", "workspace"]


@dataclass
class _Provider:
    """A connected SCM: provider truth, approval delivery, statuses, merge result and control-plane signing."""

    sync: str = "synced"
    head: str | None = None
    trusted_base: str | None = "current"  # None: the provider returns no L4 fact
    raw_base: Any = None  # a value that is not a TrustedBaseFact
    labels: tuple[str, ...] = ()
    open_requests: tuple[str, ...] = ()
    merge: MergeResultFacts | None = None
    approvals: list[str] = field(default_factory=list)
    withdrawals: list[str] = field(default_factory=list)
    statuses: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    signed: list[Any] = field(default_factory=list)
    reads: list[bool] = field(default_factory=list)
    base_reads: int = 0

    def facts(self, session: Any, revision: Any, decision_time: bool) -> Mapping[str, Any]:
        self.reads.append(decision_time)
        out: dict[str, Any] = {
            "provider_sync": aa.ProviderSyncFact(self.sync, self.head if self.head is not None else revision.head_sha),
            "requests": aa.RequestProgressFact(open_requests=self.open_requests),
            "labels": list(self.labels),
            "target_branch": "main",
            "provider_blockers": [],
        }
        if decision_time and self.raw_base is not None:
            out["trusted_base"] = self.raw_base
        elif decision_time and self.trusted_base is not None:
            out["trusted_base"] = aa.TrustedBaseFact(self.trusted_base)
        return out

    def approve(self, session: Any, revision: Any, metadata: Mapping[str, Any]) -> Mapping[str, Any]:
        self.approvals.append(str(metadata["decision_digest"]))
        return {"state": "sent", "remote_ref": f"review-{len(self.approvals)}"}

    def withdraw(self, session: Any, metadata: Mapping[str, Any]) -> Mapping[str, Any]:
        self.withdrawals.append(str(metadata.get("remote_ref") or ""))
        return {"state": "done"}

    def publish_status(self, session: Any, sha: str, status: Mapping[str, str]) -> Mapping[str, Any]:
        self.statuses.append((sha, dict(status)))
        return {"state": "sent"}

    def merge_result(self, session: Any, revision: Any) -> MergeResultFacts | None:
        return self.merge

    def sign_decision(self, review_id: str, decision: Any) -> Mapping[str, Any]:
        self.signed.append(decision)
        return {"status": "recorded", "record_id": f"rec-{len(self.signed)}"}

    def trusted_base_now(self, session: Any, revision: Any) -> aa.TrustedBaseFact:
        self.base_reads += 1
        return aa.TrustedBaseFact(self.trusted_base or "unknown")

    def authority(self) -> LandingAuthority:
        return LandingAuthority(
            facts=self.facts,
            approve=self.approve,
            withdraw=self.withdraw,
            publish_status=self.publish_status,
            merge_result=self.merge_result,
            sign_decision=self.sign_decision,
            trusted_base=self.trusted_base_now,
        )


_POLICY = {
    "auto_approve": {"enabled": True, "max_criticality": "high"},
    "break_glass": {"responders": ["alice"], "follow_up_hours": 48},
}


def _delegate(calls: list[str]) -> Any:
    def delegate(session: Any, revision: Any, *, action: str) -> dict[str, Any]:
        calls.append(action)
        return {
            "supported": True,
            "state": "delegated" if action == "arm" else "cancelled",
            "provider": "github",
            "detail": "GitHub auto-merge",
            "url": "https://github.com/acme/widgets/pull/7",
            "mechanism": "auto_merge_or_merge_queue",
        }

    return delegate


def _client(
    workspace: Workspace,
    provider: _Provider,
    *,
    policy: Mapping[str, Any] = _POLICY,
    principal: Any = None,
    calls: list[str] | None = None,
) -> Workspace:
    store = ReviewStore(workspace.store_root)
    app = FastAPI()

    def extension(session: Any, revision: Any) -> Mapping[str, Any]:
        return {
            "landing": {"supported": True, "provider": "github", "auto_merge": "enabled", "queue_state": ""},
            "landing_policy": [{"source": "organization policy", "document": dict(policy)}],
            "policy": [{"layer": "operator_review_policy", "source": "org", "document": {"landing": dict(policy)}}],
        }

    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        landing_delegate=_delegate(calls if calls is not None else []),
        readiness_extension=extension,
        landing_authority=provider.authority(),
        approval_authority=(lambda: principal) if principal is not None else None,
    )
    landing = Workspace(
        TestClient(app, base_url=BASE), store, workspace.store_root, workspace.repo_root, workspace.review_id
    )
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
    return landing


def _evaluate(landing: Workspace) -> dict[str, Any]:
    evaluator = landing.client.app.state.review_landing_evaluator  # type: ignore[attr-defined]
    result: dict[str, Any] = evaluator(landing.review_id, origin="provider")
    return result


def _gates(result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {gate["id"]: gate for gate in result["decision"]["gates"]}


def _readiness(landing: Workspace) -> dict[str, Any]:
    response = landing.get(f"/api/reviews/{landing.review_id}/readiness")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


HEAD = "c" * 40


def _set_head(landing: Workspace, sha: str = HEAD) -> None:
    """Give the working-tree fixture Snapshot a provider head (as a pull request capture would)."""

    revision = landing.store.latest_revision(landing.review_id)
    assert revision is not None
    with sqlite3.connect(landing.store.db_path) as conn:
        conn.execute("UPDATE review_revisions SET head_sha = ? WHERE id = ?", (sha, revision.id))


def _activity(landing: Workspace, kind: str) -> list[Any]:
    return [row for row in landing.store.list_activity_events(landing.review_id) if row.kind == kind]


def _landed(workspace: Workspace, provider: _Provider, **client: Any) -> Workspace:
    landing = _client(workspace, provider, **client)
    _set_head(landing)
    return landing


# --------------------------------------------------------------------------- #
# items 8-10: auto-approval decided, delivered, signed and shown
# --------------------------------------------------------------------------- #


def test_an_eligible_change_is_auto_approved_once_signed_and_its_status_published(workspace: Workspace) -> None:
    provider = _Provider()
    landing = _landed(workspace, provider)

    result = _evaluate(landing)
    assert result["outcome"] == "approved", _gates(result)
    digest = result["decision"]["decision_digest"]
    assert provider.approvals == [digest]
    assert provider.reads == [True]  # L4 is read at decision time
    [signed] = provider.signed
    assert (signed.kind, signed.outcome, signed.actor_id, signed.actor_type) == (
        "landing",
        "auto_approved",
        aa.AUTO_APPROVAL_ACTOR,
        "system",
    )
    assert signed.detail["decision_digest"] == digest
    assert provider.statuses == [
        (
            HEAD,
            {
                "state": "success",
                "description": "Approved by LemonCrow policy",
                "context": "LemonCrow / Merge readiness",
            },
        )
    ]
    assert len(_activity(landing, "landing.auto_approved")) == 1

    # Deciding again goes through the one revalidation path: no second approval, status or record.
    again = _evaluate(landing)
    assert again["outcome"] == "unchanged"
    assert provider.approvals == [digest] and len(provider.signed) == 1 and len(provider.statuses) == 1

    body = _readiness(landing)
    approval = body["landing"]["auto_approval"]
    assert approval["state"] == "approved" and approval["eligible"] is True and approval["blocking"] == []
    assert body["landing"]["scm"] == {
        "managed_by": "github",
        "mechanism": "",
        "auto_merge": "enabled",
        "queue_state": "",
    }
    assert all(set(row) == {"id", "owner", "state", "action"} for row in body["blocking_reasons"])
    record = landing.get(f"/api/reviews/{landing.review_id}/record").json()
    assert record["decision"]["kind"] == "landing" and record["decision"]["outcome"] == "auto_approved"


@pytest.mark.parametrize(
    ("change", "gate"),
    [
        ({"sync": "provider_delayed"}, "provider_sync"),
        ({"head": "d" * 40}, "provider_sync"),
        ({"open_requests": ("bob",)}, "requests"),
        ({"trusted_base": "stale"}, "trusted_base"),
        # A decision-time L4 the provider did not return is unknown, never "not re-checked".
        ({"trusted_base": None}, "trusted_base"),
        ({"raw_base": {"state": "current"}}, "trusted_base"),
    ],
)
def test_gate_f_preconditions_block_approval_through_the_api(
    workspace: Workspace, change: dict[str, Any], gate: str
) -> None:
    provider = _Provider(**change)
    landing = _landed(workspace, provider)
    result = _evaluate(landing)
    assert result["outcome"] == "not_eligible"
    assert _gates(result)[gate]["passed"] is False
    assert provider.approvals == [] and provider.signed == []


def test_a_human_blocker_after_approval_withdraws_it_through_the_one_path(workspace: Workspace) -> None:
    provider = _Provider()
    landing = _landed(workspace, provider)
    assert _evaluate(landing)["outcome"] == "approved"
    posted = landing.post(
        f"/api/reviews/{landing.review_id}/annotations",
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
    # A Reader read revalidates the standing approval (no viewer-specific progress involved).
    body = _readiness(landing)
    assert body["landing"]["auto_approval"]["state"] == "withdrawn"
    assert provider.withdrawals == ["review-1"]
    [withdrawn] = _activity(landing, "landing.auto_approval_withdrawn")
    assert json.loads(withdrawn.detail_json)["blocked_by"][0]["id"] == "readiness"
    # The next decision never re-approves the Snapshot it just withdrew while the blocker stands.
    assert _evaluate(landing)["outcome"] == "not_eligible"
    assert len(provider.approvals) == 1


def test_a_policy_change_is_revalidated_by_the_request_free_revalidator(workspace: Workspace) -> None:
    provider = _Provider()
    policy: dict[str, Any] = json.loads(json.dumps(_POLICY))
    landing = _landed(workspace, provider, policy=policy)
    assert _evaluate(landing)["outcome"] == "approved"
    revalidate = landing.client.app.state.review_landing_revalidator  # type: ignore[attr-defined]
    assert revalidate(landing.review_id, origin="sweep") == "unchanged"
    policy["auto_approve"]["exclude"] = {"paths": ["src/**"]}
    assert revalidate(landing.review_id, origin="sweep") == "withdrawn"
    [withdrawn] = _activity(landing, "landing.auto_approval_withdrawn")
    detail = json.loads(withdrawn.detail_json)
    assert detail["origin"] == "sweep" and detail["previous_generation"] != detail["generation"]
    assert provider.withdrawals == ["review-1"]


def test_revalidation_re_reads_l4_and_withdraws_an_approval_once_the_trusted_base_moved(
    workspace: Workspace,
) -> None:
    """RC11: L4 is re-read on revalidation, not only when deciding (a plain read never reads it)."""

    provider = _Provider()
    landing = _landed(workspace, provider)
    assert _evaluate(landing)["outcome"] == "approved"
    revalidate = landing.client.app.state.review_landing_revalidator  # type: ignore[attr-defined]
    assert revalidate(landing.review_id, origin="sweep") == "unchanged"
    assert provider.reads[-1] is True  # the revalidation re-read the default branch
    provider.trusted_base = "unknown"  # unreadable: kept, never withdrawn
    assert revalidate(landing.review_id, origin="sweep") == "deferred"
    assert provider.withdrawals == []
    provider.trusted_base = "stale"
    reads = len(provider.reads)
    _readiness(landing)  # a plain read does not reach the provider's default branch
    assert provider.reads[reads:] == [False] * (len(provider.reads) - reads)
    assert provider.withdrawals == []
    assert revalidate(landing.review_id, origin="sweep") == "withdrawn"
    assert provider.withdrawals == ["review-1"]
    [withdrawn] = _activity(landing, "landing.auto_approval_withdrawn")
    assert "trusted_base" in json.dumps(json.loads(withdrawn.detail_json)["blocked_by"])


def _arm_by_hand(landing: Workspace) -> None:
    _review_all_current_targets(landing)
    inspected = _readiness(landing)
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


def test_an_armed_landing_is_withdrawn_once_the_trusted_base_moved(workspace: Workspace) -> None:
    """RC11: the human-armed Merge-when-ready intent re-reads L4 on revalidation."""

    calls: list[str] = []
    provider = _Provider()
    landing = _landed(workspace, provider, policy={"auto_approve": {"enabled": False}}, calls=calls)
    _arm_by_hand(landing)
    assert calls == ["arm"]
    revalidate = landing.client.app.state.review_landing_revalidator  # type: ignore[attr-defined]
    assert revalidate(landing.review_id, origin="sweep") in {"unchanged", "revalidated"}
    reads = provider.base_reads
    assert reads >= 1
    provider.trusted_base = "unknown"  # an unreadable base never cancels a human's decision
    assert revalidate(landing.review_id, origin="sweep") in {"unchanged", "revalidated"}
    provider.trusted_base = "stale"
    reads = provider.base_reads
    _readiness(landing)  # a plain read never reads the default branch
    assert provider.base_reads == reads and calls == ["arm"]
    assert revalidate(landing.review_id, origin="sweep") == "withdrawn"
    assert calls == ["arm", "cancel"]
    [withdrawn] = _activity(landing, "landing.withdrawn")
    assert [row["id"] for row in json.loads(withdrawn.detail_json)["blocked_by"]] == ["trusted-base"]


def test_break_glass_cannot_bypass_a_moved_trusted_base(workspace: Workspace) -> None:
    calls: list[str] = []
    provider = _Provider()
    alice = SimpleNamespace(kind="human", principal_id="alice", reason="")
    landing = _landed(workspace, provider, principal=alice, calls=calls)
    inspected = _readiness(landing)
    assert _break_glass(landing, inspected, _local_blockers(inspected)).status_code == 200
    provider.trusted_base = "stale"
    revalidate = landing.client.app.state.review_landing_revalidator  # type: ignore[attr-defined]
    assert revalidate(landing.review_id, origin="sweep") == "withdrawn"
    assert calls == ["arm", "cancel"]


def test_hard_denied_paths_are_never_auto_approved_through_the_api(workspace: Workspace) -> None:
    provider = _Provider()
    policy = {"auto_approve": {"enabled": True, "max_criticality": "high", "sensitive_paths": ["src/session.py"]}}
    landing = _landed(workspace, provider, policy=policy)
    result = _evaluate(landing)
    assert _gates(result)["hard_deny"]["reason"] == "configured_sensitive"
    assert provider.approvals == []


def test_deciding_on_request_needs_a_human_manager(workspace: Workspace) -> None:
    path = f"/api/reviews/{workspace.review_id}/landing/auto-approval"
    agent = SimpleNamespace(kind="agent", principal_id="bot", reason="not a human sign-in")
    landing = _landed(workspace, _Provider(), principal=agent)
    assert landing.post(path, json={}).status_code == 403
    unbound = _landed(workspace, _Provider())
    assert unbound.post(path, json={}).status_code == 403
    provider = _Provider()
    human = _landed(workspace, provider, principal=SimpleNamespace(kind="human", principal_id="alice", reason=""))
    preview = human.get(path).json()
    assert preview["decision"]["eligible"] is True and preview["delivers"] is True
    assert provider.reads == [False] and provider.approvals == []  # a preview never decides
    decided = human.post(path, json={})
    assert decided.status_code == 200 and decided.json()["outcome"] == "approved"


# --------------------------------------------------------------------------- #
# item 18: what lands is re-validated
# --------------------------------------------------------------------------- #


def _merge_group(*, merge: str) -> MergeResultFacts:
    def side(status: str) -> Any:
        return side_results([{"identity": "ci:unit", "name": "unit", "status": status}])

    return MergeResultFacts(
        target_kind="merge_group",
        target_sha="e" * 40,
        base_sha="b" * 40,
        head_sha=HEAD,
        queue_state="checks_requested",
        head=side("success"),
        base=side("success"),
        merge=side(merge),
    )


def test_two_changes_that_break_only_once_merged_are_blocked_at_landing(workspace: Workspace) -> None:
    calls: list[str] = []
    provider = _Provider()
    landing = _landed(workspace, provider, calls=calls)
    _review_all_current_targets(landing)
    inspected = _readiness(landing)
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
    assert calls == ["arm"]

    # The merge queue builds the group with another Change merged since review: unit fails.
    provider.merge = _merge_group(merge="failure")
    body = _readiness(landing)
    condition = next(row for row in body["conditions"] if row["id"] == "merge-result")
    assert (condition["state"], condition["owner"]) == ("fail", "lemoncrow")
    assert condition["new_failures"] == ["ci:unit"]
    assert calls == ["arm", "cancel"] and body["landing"]["state"] == "blocked"
    assert {
        "id": "merge-result",
        "owner": "lemoncrow",
        "state": "fail",
        "action": "Merged result fails checks",
    } in body["blocking_reasons"]
    # Arming again is refused while the merge result fails; the merge commit gets a failing status.
    again = _readiness(landing)
    refused = landing.post(
        f"/api/reviews/{landing.review_id}/landing/arm",
        json={
            "armed": True,
            "expected_revision_id": again["snapshot_id"],
            "expected_tree_fingerprint": again["tree_fingerprint"],
            "expected_conditions_hash": again["conditions_hash"],
        },
    )
    assert refused.status_code == 409 and "Merge result" in refused.json()["detail"]
    _evaluate(landing)
    merge_status = [status for sha, status in provider.statuses if sha == "e" * 40]
    assert merge_status and merge_status[-1]["state"] == "failure"


def test_a_passing_merge_result_is_published_as_success(workspace: Workspace) -> None:
    provider = _Provider(merge=_merge_group(merge="success"))
    landing = _landed(workspace, provider)
    assert _evaluate(landing)["outcome"] == "approved"
    assert (
        "e" * 40,
        {"state": "success", "description": "Merge result: no new failures", "context": "LemonCrow / Merge readiness"},
    ) in provider.statuses


# --------------------------------------------------------------------------- #
# item 20: break-glass
# --------------------------------------------------------------------------- #


def _break_glass(landing: Workspace, inspected: Mapping[str, Any], bypass: list[str], **overrides: Any) -> Any:
    return landing.post(
        f"/api/reviews/{landing.review_id}/landing/break-glass",
        json={
            "reason": "Checkout outage: ship the retry fix now",
            "incident_url": "https://status.example.com/incidents/42",
            "bypass": bypass,
            "expected_revision_id": inspected["snapshot_id"],
            "expected_conditions_hash": inspected["conditions_hash"],
            **overrides,
        },
    )


def _local_blockers(body: Mapping[str, Any]) -> list[str]:
    return [
        row["id"]
        for row in body["conditions"]
        if row["owner"] != "scm"
        and row["state"] not in {"pass", "not_applicable", "waived"}
        and row["id"] != "policy-generation"
    ]


@pytest.mark.parametrize(
    "principal",
    [
        SimpleNamespace(kind="agent", principal_id="alice", reason="this credential is not a human sign-in"),
        SimpleNamespace(kind="service", principal_id="alice", reason="service credentials"),
        SimpleNamespace(kind="human", principal_id="bob", reason=""),
    ],
)
def test_break_glass_is_refused_to_agents_services_and_unnamed_people(workspace: Workspace, principal: Any) -> None:
    calls: list[str] = []
    landing = _landed(workspace, _Provider(), principal=principal, calls=calls)
    inspected = _readiness(landing)
    refused = _break_glass(landing, inspected, _local_blockers(inspected))
    assert refused.status_code == 403, refused.text
    assert calls == []
    [audit] = _activity(landing, "landing.break_glass_refused")
    assert json.loads(audit.detail_json)["actor"] == principal.principal_id
    assert landing_relationship(landing.store, landing.review_id) is None


def test_break_glass_lands_through_the_scm_records_everything_and_opens_a_follow_up(workspace: Workspace) -> None:
    calls: list[str] = []
    provider = _Provider()
    alice = SimpleNamespace(kind="human", principal_id="alice", reason="")
    landing = _landed(workspace, provider, principal=alice, calls=calls)
    inspected = _readiness(landing)
    blockers = _local_blockers(inspected)
    assert "latest-snapshot-reviewed" in blockers

    # Every LemonCrow blocker must be named; a stale view is refused.
    assert _break_glass(landing, inspected, blockers[:-1] or ["nope"]).status_code == 409
    assert _break_glass(landing, inspected, blockers, expected_conditions_hash="conditions:old").status_code == 409
    assert calls == []

    landed = _break_glass(landing, inspected, blockers)
    assert landed.status_code == 200, landed.text
    assert calls == ["arm"]  # the SCM's own native landing, nothing else
    record = landing_relationship(landing.store, landing.review_id)
    metadata = landing_metadata(record)
    assert metadata["basis"] == "break_glass" and metadata["bypassed"] == blockers
    [event] = _activity(landing, "landing.break_glass")
    detail = json.loads(event.detail_json)
    assert detail["actor"] == "alice" and detail["incident_url"].endswith("/42") and detail["bypassed"] == blockers
    assert event.actor_type == "human"
    [signed] = provider.signed
    assert (signed.kind, signed.outcome, signed.actor_id, signed.actor_type) == (
        "landing",
        "break_glass",
        "alice",
        "human",
    )
    assert signed.detail["bypassed"] == ",".join(blockers)
    assert signed.detail["reason"] == "Checkout outage: ship the retry fix now"

    body = _readiness(landing)
    assert calls == ["arm"]  # the named blockers are bypassed: reading never withdraws it
    [shown] = body["landing"]["break_glass"]
    assert shown["actor_id"] == "alice" and shown["follow_up"]["state"] == "open"
    assert body["landing"]["basis"] == "break_glass"

    # The follow-up Review completes with another person's decision on the landed Snapshot.
    revision = landing.store.latest_revision(landing.review_id)
    assert revision is not None
    approved = landing.post(
        f"/api/reviews/{landing.review_id}/outcome",
        json={"outcome": "lgtm", "summary": "follow-up done", "expected_revision_id": revision.id},
    )
    assert approved.status_code == 200, approved.text
    assert _readiness(landing)["landing"]["break_glass"][0]["follow_up"]["state"] == "completed"


def test_a_regression_beyond_the_bypassed_blockers_still_withdraws_break_glass(workspace: Workspace) -> None:
    calls: list[str] = []
    alice = SimpleNamespace(kind="human", principal_id="alice", reason="")
    landing = _landed(workspace, _Provider(), principal=alice, calls=calls)
    inspected = _readiness(landing)
    assert _break_glass(landing, inspected, _local_blockers(inspected)).status_code == 200
    posted = landing.post(
        f"/api/reviews/{landing.review_id}/annotations",
        json={
            "path": "src/session.py",
            "side": "new",
            "start_line": 8,
            "end_line": 8,
            "body": "Wait: this breaks refresh.",
            "kind": "request_change",
        },
    )
    assert posted.status_code == 201, posted.text
    assert calls == ["arm", "cancel"]


def test_break_glass_is_off_without_responders_in_policy(workspace: Workspace) -> None:
    alice = SimpleNamespace(kind="human", principal_id="alice", reason="")
    landing = _landed(workspace, _Provider(), principal=alice, policy={"auto_approve": {"enabled": False}})
    inspected = _readiness(landing)
    refused = _break_glass(landing, inspected, _local_blockers(inspected))
    assert refused.status_code == 403 and "names no break-glass responders" in refused.json()["detail"]


# --------------------------------------------------------------------------- #
# carried P10-SIGN call site
# --------------------------------------------------------------------------- #


def test_a_landing_record_never_suppresses_the_merge_record() -> None:
    from lemoncrow.pro.capabilities.review.review_record_api import LANDING_OBSERVER, MERGE_OBSERVER, _merge_emitted

    def record(kind: str, exporter: str = MERGE_OBSERVER, kind_of_actor: str = "system") -> Any:
        predicate = {"exported_by": {"id": exporter, "type": kind_of_actor}, "decision": {"kind": kind}}
        return SimpleNamespace(statement={"predicate": predicate})

    assert _merge_emitted(record("provider_merge")) is True
    assert _merge_emitted(record("human_outcome")) is True
    # A merge that rests on an auto-approval or break-glass records that landing decision: still the merge's.
    assert _merge_emitted(record("landing")) is True
    # The landing decision's own record never stands in for the merge record.
    assert _merge_emitted(record("landing", exporter=LANDING_OBSERVER)) is False
    assert _merge_emitted(record("human_outcome", exporter="alice", kind_of_actor="human")) is False


def test_the_merge_record_names_the_auto_approval_and_break_glass_it_landed_on(workspace: Workspace) -> None:
    from lemoncrow.pro.capabilities.review.landing import record_landing_state
    from lemoncrow.pro.capabilities.review.review_record import current_decision

    alice = SimpleNamespace(kind="human", principal_id="alice", reason="")
    provider = _Provider()
    landing = _landed(workspace, provider, principal=alice)
    assert _evaluate(landing)["outcome"] == "approved"
    inspected = _readiness(landing)
    assert _break_glass(landing, inspected, _local_blockers(inspected)).status_code == 200
    item = landing_relationship(landing.store, landing.review_id)
    assert item is not None
    record_landing_state(landing.store, item, status="merged", metadata_updates={})
    session = landing.store.get_session(landing.review_id)
    revision = landing.store.latest_revision(landing.review_id)
    assert session is not None and revision is not None
    decision = current_decision(landing.store, session, revision)
    assert decision is not None and decision.kind == "provider_merge"
    assert decision.detail["basis"] == "break_glass"
    assert decision.detail["break_glass_id"].startswith("bg_")
    assert decision.detail["auto_approval"].startswith("decision:")


def test_without_a_control_plane_signer_the_local_key_path_is_used_and_never_fails_the_decision(
    workspace: Workspace,
) -> None:
    provider = _Provider()
    _set_head(_client(workspace, provider))
    unsigned = replace(provider.authority(), sign_decision=None)
    store = ReviewStore(workspace.store_root)
    local = FastAPI()
    register_review_api(
        local,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        landing_delegate=_delegate([]),
        readiness_extension=lambda session, revision: {
            "landing": {"supported": True, "provider": "github"},
            "landing_policy": [{"source": "organization policy", "document": _POLICY}],
        },
        landing_authority=unsigned,
    )
    result = local.state.review_landing_evaluator(workspace.review_id, origin="provider")
    assert result["outcome"] == "approved" and provider.signed == []
    # No opted-in personal key here: the record is not emitted, and the decision stands.
    assert aa.auto_approval_relationship(store, workspace.review_id).status == "approved"


# --------------------------------------------------------------------------- #
# what one decision reads: off by default, renames, Gate H rows
# --------------------------------------------------------------------------- #


def test_auto_approval_is_off_without_any_landing_configuration(workspace: Workspace) -> None:
    assert AutoApprovePolicy().enabled is False and LandingPolicy().auto_approve_enabled is False
    provider = _Provider()
    _set_head(_client(workspace, provider))  # the SCM link only
    store = ReviewStore(workspace.store_root)
    bare = FastAPI()
    register_review_api(
        bare,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        landing_delegate=_delegate([]),
        readiness_extension=lambda session, revision: {"landing": {"supported": True, "provider": "github"}},
        landing_authority=provider.authority(),
    )
    result = bare.state.review_landing_evaluator(workspace.review_id, origin="provider")
    assert result["outcome"] == "not_eligible" and result["decision"]["eligible"] is False
    assert _gates(result)["policy"]["reason"] == "not_enabled"
    assert provider.reads == [False]  # the default branch is never read for a policy that cannot approve
    assert provider.approvals == [] and provider.signed == []
    assert aa.auto_approval_relationship(store, workspace.review_id) is None
    assert all("Approved by LemonCrow policy" not in status["description"] for _sha, status in provider.statuses)


_SESSION = SimpleNamespace(id="rev_facts")
_REVISION = SimpleNamespace(id="snap_facts", tree_fingerprint="tree:facts", head_sha=HEAD)


def _not_read(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("gather_facts never reads this")


def _facts_deps(
    *,
    files: tuple[tuple[str, str, str], ...] = (("pkg/app.py", "", "modified"),),
    rows: tuple[Mapping[str, Any], ...] = (),
) -> LandingDeps:
    store = SimpleNamespace(
        list_units=lambda _revision_id: [],
        list_review_mark_events=lambda _review_id: [],
        list_marks=lambda _review_id, reviewer_id: [],
    )
    return LandingDeps(
        store=store,
        session=_not_read,
        latest=_not_read,
        display_number=_not_read,
        readiness=_not_read,
        extension=lambda _session, _revision: {"landing_policy": [{"source": "org", "document": dict(_POLICY)}]},
        review_config=lambda _revision: None,
        changed_files=lambda _revision: files,
        criticality=lambda _session, _revision, _paths: "low",
        behavior_blockers=lambda _session, _revision: (),
        verification_rows=lambda _session, _revision: rows,
        findings=lambda _session, _revision: aa.FindingsFact(),
        arm=_not_read,
        activity=_not_read,
        approval_principal=None,
        may_manage=_not_read,
        scm_link=_not_read,
        revalidate=_not_read,
        authority=_Provider().authority(),
        now=lambda: "2026-10-05T00:00:00Z",
    )


def _decided_gates(deps: LandingDeps) -> dict[str, aa.Gate]:
    facts = gather_facts(deps, _SESSION, _REVISION, {}, decision_time=True)
    decision = aa.evaluate_auto_approval(facts, evaluated_at="2026-10-05T00:00:00Z")
    return {gate.id: gate for gate in decision.gates}


def test_a_rename_out_of_a_sensitive_directory_is_hard_denied_by_its_old_path() -> None:
    assert _decided_gates(_facts_deps(files=(("src/misc/helpers.py", "", "modified"),)))["hard_deny"].passed
    moved = _facts_deps(files=(("src/misc/helpers.py", "src/auth/helpers.py", "renamed"),))
    facts = gather_facts(moved, _SESSION, _REVISION, {}, decision_time=True)
    assert facts.changed_paths == ("src/misc/helpers.py", "src/auth/helpers.py")
    gate = _decided_gates(moved)["hard_deny"]
    assert gate.passed is False and gate.reason == "auth" and "src/auth/helpers.py" in gate.detail


def _lemoncrow_row(name: str, qualification: Any = None) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name, "attestation_class": "lemoncrow_verification", "status": "passed"}
    if qualification is not None:
        row["qualification"] = qualification
    return row


def test_gate_h_a_lemoncrow_result_without_a_qualified_stamp_blocks_through_gather_facts() -> None:
    for qualification in [None, {"state": "advisory"}, {"state": "unmeasured"}, "qualified"]:
        rows = (_lemoncrow_row("pytest (LemonCrow)", qualification), {"name": "ci", "attestation_class": "ci"})
        facts = gather_facts(_facts_deps(rows=rows), _SESSION, _REVISION, {}, decision_time=True)
        assert facts.unqualified_evidence == ("pytest (LemonCrow)",)
        gate = _decided_gates(_facts_deps(rows=rows))["evidence_qualification"]
        assert gate.passed is False and gate.reason == "unqualified" and ("pytest (LemonCrow)" in gate.detail)


def test_gate_h_a_qualified_lemoncrow_result_and_non_lemoncrow_rows_pass() -> None:
    rows = (_lemoncrow_row("pytest (LemonCrow)", {"state": "qualified"}), {"name": "ci", "attestation_class": "ci"})
    assert gather_facts(_facts_deps(rows=rows), _SESSION, _REVISION, {}, decision_time=True).unqualified_evidence == ()
    assert _decided_gates(_facts_deps(rows=rows))["evidence_qualification"].passed is True
