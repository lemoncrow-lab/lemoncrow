"""P12-AUTO: automation rules, post-merge actions, published-feedback sync, discard/delete safety.

Every acceptance property is asserted next to its refusal.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.automation_rules import (
    ActingIdentity,
    AutomationDecision,
    AutomationFacts,
    BranchProtectionFacts,
    decide_automation,
    evaluate_rule,
    load_local_automation_rules,
    parse_automation_rules,
)
from lemoncrow.pro.capabilities.review.feedback_sync import (
    FeedbackSyncError,
    GhFeedbackSyncProvider,
    ProviderRequest,
    durable_github_host,
    durable_github_target,
    parse_durable_github_target,
)
from lemoncrow.pro.capabilities.review.handoff import feedback_version
from lemoncrow.pro.capabilities.review.landing import landing_metadata, landing_relationship
from lemoncrow.pro.capabilities.review.post_merge_actions import (
    PostMergeOutcome,
    PostMergeRefused,
    ProviderMergeFact,
    observed_merge,
    readiness_record_digest,
    run_post_merge_action,
)
from lemoncrow.pro.capabilities.review.session_models import DeliveryRecord, ReviewSession
from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError
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

# A delivery names its pull request by the repository's durable id and host (publish writes only that).
DURABLE_PR = durable_github_target("4242", "7", "github.com")
# The retired by-name form: an older delivery may still carry it, and it is never sent anywhere.
LEGACY_PR = "github:acme/widgets#7"


@pytest.fixture(autouse=True)
def _isolated_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    return root


def _client(workspace: Workspace, **register: Any) -> Workspace:
    store = ReviewStore(workspace.store_root)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        **register,
    )
    return Workspace(
        TestClient(app, base_url=BASE), store, workspace.store_root, workspace.repo_root, workspace.review_id
    )


def _delegate(arm_state: str, calls: list[str] | None = None, *, cancel: str = "cancelled") -> Any:
    def delegate(session: ReviewSession, revision: Any, *, action: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(action)
        if action == "cancel" and cancel == "raise":
            raise RuntimeError("GitHub is unavailable")
        return {
            "supported": True,
            "state": arm_state if action == "arm" else cancel,
            "provider": "github",
            "detail": f"provider said {arm_state if action == 'arm' else cancel}",
            "url": "https://github.com/acme/widgets/pull/7",
        }

    return delegate


def _arm(ws: Workspace) -> dict[str, Any]:
    linked = ws.post(
        f"/api/reviews/{ws.review_id}/relationships",
        json={
            "kind": "scm",
            "relation": "pull_request",
            "provider": "github",
            "external_id": "acme/widgets#7",
            "url": "https://github.com/acme/widgets/pull/7",
        },
    )
    assert linked.status_code == 200, linked.text
    _review_all_current_targets(ws)
    inspected = ws.get(f"/api/reviews/{ws.review_id}/readiness").json()
    armed = ws.post(
        f"/api/reviews/{ws.review_id}/landing/arm",
        json={
            "armed": True,
            "expected_revision_id": inspected["snapshot_id"],
            "expected_tree_fingerprint": inspected["tree_fingerprint"],
            "expected_conditions_hash": inspected["conditions_hash"],
        },
    )
    assert armed.status_code == 200, armed.text
    return dict(armed.json())


def _rules_file(root: Path, rules: Sequence[dict[str, Any]]) -> Path:
    path = root / "review-automation.json"
    path.write_text(json.dumps({"version": 1, "rules": list(rules)}), encoding="utf-8")
    return path


def _rule(rule_id: str, action: str, **extra: Any) -> dict[str, Any]:
    trigger = "merge_observed" if action.startswith("post_merge.") else "readiness_ready"
    return {
        "id": rule_id,
        "trigger": trigger,
        "action": action,
        "enabled": True,
        "enabled_by": "ops-admin@acme",
        "acts_as": {"subject": "owner", "role": "owner"},
        **extra,
    }


def _activity(ws: Workspace, kind: str) -> list[dict[str, Any]]:
    events = ws.get(f"/api/reviews/{ws.review_id}/activity").json()["events"]
    return [item for item in events if item["kind"] == kind]


# --------------------------------------------------------------------------- #
# Rule model (items 8, 9)
# --------------------------------------------------------------------------- #


def test_rules_parse_from_operator_policy_and_invalid_rules_never_run(tmp_path: Path) -> None:
    rules, errors = parse_automation_rules(
        [
            _rule("notify", "post_merge.notification"),
            _rule(
                "bypass", "landing.native_merge", conditions=[{"fact": "branch_protection.requirements", "op": "eq"}]
            ),
            {**_rule("skip-reviews", "landing.native_merge"), "bypass_required_reviews": True},
            {**_rule("as-admin", "post_merge.notification"), "acts_as": {"subject": "root", "role": "admin"}},
            {**_rule("nobody", "post_merge.notification"), "enabled_by": ""},
            _rule("wrong-trigger", "post_merge.notification", trigger="readiness_ready"),
            _rule(
                "ready-after-merge",
                "post_merge.notification",
                conditions=[{"fact": "readiness.ready", "op": "is_true"}],
            ),
            _rule("notify", "post_merge.ticket_update"),
        ],
        layer="local_operator",
        source="test",
    )

    assert [rule.id for rule in rules] == ["notify"]
    assert len(errors) == 7
    joined = "\n".join(errors)
    assert "never conditions a rule can relax or bypass" in joined
    assert "can never bypass branch protection or required reviews" in joined
    assert "never acts with administrator authority" in joined
    assert "enabled_by must name who deliberately enabled" in joined
    assert "only runs on the merge_observed trigger" in joined
    assert "readiness.ready is not a post-merge fact" in joined
    assert "used by more than one rule" in joined

    # The local file is the operator's (LEMONCROW_ROOT), never the repository's.
    assert load_local_automation_rules(tmp_path).rules == ()
    (tmp_path / "review-automation.json").write_text("{not json", encoding="utf-8")
    broken = load_local_automation_rules(tmp_path)
    assert broken.rules == () and "could not be read" in broken.errors[0]


def _facts(protection: BranchProtectionFacts, **values: object) -> AutomationFacts:
    return AutomationFacts(review_id="r1", repository="repo", values=values, branch_protection=protection)


def test_landing_rules_are_guarded_by_branch_protection_and_required_reviews() -> None:
    rules, errors = parse_automation_rules(
        [_rule("merge-docs", "landing.native_merge", conditions=[{"fact": "change.title", "op": "ne", "value": ""}])],
        layer="local_operator",
        source="test",
    )
    assert errors == ()
    rule = rules[0]
    unknown = evaluate_rule(rule, _facts(BranchProtectionFacts(), **{"readiness.ready": True, "change.title": "x"}))
    assert unknown.would_fire is False and unknown.branch_protection_enforced is True
    guard = {item.name: item for item in unknown.guards}
    assert guard["provider branch protection satisfied"].passed is False
    assert guard["provider branch protection satisfied"].actual == "unknown"
    assert "unknown is never satisfied" in guard["provider branch protection satisfied"].detail

    changes = BranchProtectionFacts("satisfied", "unsatisfied", changes_requested=True, source="provider")
    blocked = evaluate_rule(rule, _facts(changes, **{"readiness.ready": True, "change.title": "x"}))
    assert blocked.would_fire is False
    assert (
        "requested changes"
        in {item.name: item for item in blocked.guards}["provider required reviews satisfied"].detail
    )

    ok = BranchProtectionFacts("satisfied", "satisfied", source="provider")
    fires = evaluate_rule(rule, _facts(ok, **{"readiness.ready": True, "change.title": "x"}))
    assert fires.would_fire is True
    # Every condition is inspectable with the value it saw.
    assert fires.conditions[0].actual == "x" and fires.conditions[0].passed is True


def test_automation_needs_an_enabling_rule_and_never_exceeds_its_acting_identity() -> None:
    facts = _facts(BranchProtectionFacts(), **{"merge.observed": True, "merge.reviewed_head": True})
    allowed = lambda identity, permission: True  # noqa: E731

    none = decide_automation("post_merge.changelog_entry", rules=(), facts=facts, permission_check=allowed)
    assert none.mode == "needs_human" and "needs an explicit human action" in none.reason

    disabled, _errors = parse_automation_rules(
        [{**_rule("cl", "post_merge.changelog_entry"), "enabled": False}], layer="local_operator", source="t"
    )
    assert decide_automation(
        "post_merge.changelog_entry", rules=disabled, facts=facts, permission_check=allowed
    ).mode == ("needs_human")

    # An administrator enabled it; it acts as a service identity that lacks review:manage.
    hosted, errors = parse_automation_rules(
        [{**_rule("cl", "post_merge.changelog_entry"), "acts_as": {"subject": "bot-1", "role": "service"}}],
        layer="organization",
        source="t",
    )
    assert errors == ()
    roles = {"service": {"review:read", "review:comment"}, "developer": {"review:read", "review:manage"}}

    def by_role(identity: ActingIdentity, permission: str) -> bool:
        return permission in roles.get(identity.role, set())

    refused = decide_automation("post_merge.changelog_entry", rules=hosted, facts=facts, permission_check=by_role)
    assert refused.mode == "refused" and refused.rule_id == "cl"
    assert "lacks review:manage" in refused.reason

    developer, _ = parse_automation_rules(
        [{**_rule("cl", "post_merge.changelog_entry"), "acts_as": {"subject": "dev-1", "role": "developer"}}],
        layer="organization",
        source="t",
    )
    ran = decide_automation("post_merge.changelog_entry", rules=developer, facts=facts, permission_check=by_role)
    assert ran.mode == "rule" and ran.acts_as == ActingIdentity("dev-1", "developer")
    assert ran.permission == "review:manage"


# --------------------------------------------------------------------------- #
# Post-merge actions (item 7)
# --------------------------------------------------------------------------- #


def test_post_merge_runs_only_after_a_provider_observed_merge_and_never_moves_readiness(
    workspace: Workspace,
) -> None:
    merged = _client(workspace, landing_delegate=_delegate("delegated", cancel="merged"))
    armed = _arm(merged)
    assert armed["landing"]["state"] == "delegated"
    view = merged.get(f"/api/reviews/{merged.review_id}/automation").json()
    assert view["merge"] is None and view["post_merge"]["available"] is False
    assert view["post_merge"]["in_readiness"] is False
    refused = merged.post(f"/api/reviews/{merged.review_id}/post-merge/changelog_entry", json={"confirm": True})
    assert refused.status_code == 409
    assert "no provider-observed merge" in refused.json()["detail"]

    # The provider answers the cancel: it had already merged exactly the reviewed head.
    settled = merged.post(
        f"/api/reviews/{merged.review_id}/landing/cancel", json={"expected_revision_id": armed["snapshot_id"]}
    )
    assert settled.status_code == 200 and settled.json()["landing"]["state"] == "merged"
    readiness_before = merged.get(f"/api/reviews/{merged.review_id}/readiness").json()
    digest_before = readiness_record_digest(merged.store, merged.review_id, readiness_before)
    assert all("post_merge" not in json.dumps(item) for item in readiness_before["conditions"])

    missing = merged.post(f"/api/reviews/{merged.review_id}/post-merge/changelog_entry", json={})
    assert missing.status_code == 400  # an explicit human action, never implied
    ran = merged.post(f"/api/reviews/{merged.review_id}/post-merge/changelog_entry", json={"confirm": True})
    assert ran.status_code == 200, ran.text
    record = ran.json()["record"]
    assert record["status"] == "completed" and record["initiated_by"] == "human"
    assert record["output"]["written"] is False and record["output"]["entry"].startswith("- ")
    assert record["readiness_digest_before"] == record["readiness_digest_after"] == digest_before
    assert record["readiness_unchanged"] is True

    readiness_after = merged.get(f"/api/reviews/{merged.review_id}/readiness").json()
    assert readiness_record_digest(merged.store, merged.review_id, readiness_after) == digest_before
    assert readiness_after["conditions_hash"] == readiness_before["conditions_hash"]

    again = merged.post(f"/api/reviews/{merged.review_id}/post-merge/changelog_entry", json={"confirm": True})
    assert again.json()["ran"] is False  # once per merge
    ticket = merged.post(f"/api/reviews/{merged.review_id}/post-merge/ticket_update", json={"confirm": True})
    assert ticket.json()["record"]["status"] == "unavailable"
    assert _activity(merged, "post_merge.action")


def test_a_blocked_or_unreviewed_merge_is_not_a_merge_for_post_merge(workspace: Workspace) -> None:
    blocked = _client(workspace, landing_delegate=_delegate("blocked"))
    _arm(blocked)
    session = blocked.store.get_session(blocked.review_id)
    assert session is not None
    assert observed_merge(blocked.store, session) is None
    with pytest.raises(PostMergeRefused, match="no provider-observed merge"):
        run_post_merge_action(
            blocked.store,
            session,
            "notification",
            merge=None,
            initiated_by="human",
            actor_id="local",
            readiness_digest=lambda: "",
        )


def test_only_a_provider_reported_merged_state_is_a_merge(workspace: Workspace) -> None:
    session = workspace.store.get_session(workspace.review_id)
    assert session is not None
    for state in ("open", "closed", "unknown", ""):
        fact = ProviderMergeFact(provider="github", state=state, head_sha="c" * 40, external_id="acme/widgets#7")
        assert observed_merge(workspace.store, session, provider_fact=fact) is None, state
    merged = ProviderMergeFact(provider="github", state="merged", head_sha="c" * 40, external_id="acme/widgets#7")
    observation = observed_merge(workspace.store, session, provider_fact=merged)
    assert observation is not None and observation.source == "provider_subject"
    assert observation.merged_head == "c" * 40


def test_without_a_rule_a_merge_runs_nothing_automatically(workspace: Workspace) -> None:
    ws = _client(workspace, landing_delegate=_delegate("merged"))
    readiness = _arm(ws)
    assert readiness["landing"]["state"] == "merged"
    view = ws.get(f"/api/reviews/{ws.review_id}/automation").json()
    assert view["merge"]["reviewed_head_merged"] is True or view["merge"]["source"] == "provider_landing"
    assert view["post_merge"]["records"] == []
    modes = {item["action"]: item["mode"] for item in view["decisions"]}
    assert modes["post_merge.changelog_entry"] == "needs_human"
    assert modes["post_merge.notification"] == "needs_human"
    with pytest.raises(PostMergeRefused, match="needs a firing automation rule"):
        session = ws.store.get_session(ws.review_id)
        assert session is not None
        run_post_merge_action(
            ws.store,
            session,
            "changelog_entry",
            merge=observed_merge(ws.store, session),
            initiated_by="rule",
            actor_id="owner",
            readiness_digest=lambda: "",
        )


def test_an_enabled_rule_runs_once_per_merge_and_is_audited_with_its_rule_id(
    workspace: Workspace, _isolated_root: Path
) -> None:
    _rules_file(
        _isolated_root,
        [
            _rule(
                "changelog-on-merge",
                "post_merge.changelog_entry",
                conditions=[{"fact": "change.paths", "op": "any_match", "value": ["src/*"]}],
                params={"section": "Fixed"},
            ),
            _rule(
                "notify-docs-only",
                "post_merge.notification",
                conditions=[{"fact": "change.paths", "op": "all_match", "value": ["docs/*"]}],
            ),
        ],
    )
    # The default rule source is the operator file under LEMONCROW_ROOT.
    ws = _client(workspace, landing_delegate=_delegate("merged"))
    _arm(ws)

    view = ws.get(f"/api/reviews/{ws.review_id}/automation").json()
    records = view["post_merge"]["records"]
    assert [(item["kind"], item["initiated_by"], item["rule_id"]) for item in records] == [
        ("changelog_entry", "rule", "changelog-on-merge")
    ]
    assert records[0]["output"]["section"] == "Fixed"
    assert records[0]["acts_as"] == {"subject": "owner", "role": "owner"}
    assert records[0]["readiness_unchanged"] is True
    evaluations = {item["rule_id"]: item for item in view["evaluations"]}
    assert evaluations["notify-docs-only"]["would_fire"] is False
    assert "change.paths all_match" in evaluations["notify-docs-only"]["reason"]
    events = _activity(ws, "post_merge.action")
    assert len(events) == 1 and events[0]["detail"]["rule_id"] == "changelog-on-merge"
    assert events[0]["actor_type"] == "agent"

    # Replaying the trigger (the provider reports the merge again) never runs it twice.
    rerun = _client(workspace, landing_delegate=_delegate("merged", cancel="merged"))
    revision = rerun.store.latest_revision(rerun.review_id)
    assert revision is not None
    replay = rerun.post(f"/api/reviews/{rerun.review_id}/landing/cancel", json={"expected_revision_id": revision.id})
    assert replay.status_code == 200 and replay.json()["landing"]["state"] == "merged"
    assert len(_activity(rerun, "post_merge.action")) == 1
    assert len(rerun.get(f"/api/reviews/{rerun.review_id}/automation").json()["post_merge"]["records"]) == 1


def test_a_failed_rule_run_never_retries_itself_but_a_human_may_retry(workspace: Workspace) -> None:
    ws = _client(workspace, landing_delegate=_delegate("merged"))
    _arm(ws)
    session = ws.store.get_session(ws.review_id)
    assert session is not None
    merge = observed_merge(ws.store, session)
    assert merge is not None
    calls: list[str] = []

    def failing(context: Any) -> PostMergeOutcome:
        calls.append(context.actor_id)
        return PostMergeOutcome("failed", "webhook unreachable")

    decision = AutomationDecision(
        "post_merge.notification",
        "rule",
        "fires",
        rule_id="notify",
        acts_as=ActingIdentity(subject="owner", role="owner"),
        permission="review:manage",
    )

    def run(initiated_by: Any, actor_id: str) -> tuple[Any, bool]:
        return run_post_merge_action(
            ws.store,
            session,
            "notification",
            merge=merge,
            initiated_by=initiated_by,
            actor_id=actor_id,
            readiness_digest=lambda: "d",
            executors={"notification": failing},
            decision=decision if initiated_by == "rule" else None,
        )

    first, ran = run("rule", "owner")
    assert ran and first.status == "failed"
    again, ran_again = run("rule", "owner")
    assert ran_again is False and again.status == "failed" and again.attempts == first.attempts
    assert calls == ["owner"]
    human, _ = run("human", "local")
    retried, ran_retry = run("human", "local")
    assert human.status == "failed" and ran_retry is True and retried.attempts == human.attempts + 1
    assert calls == ["owner", "local", "local"]


def test_a_rule_whose_identity_lacks_the_permission_is_refused_and_audited(
    workspace: Workspace, _isolated_root: Path
) -> None:
    _rules_file(_isolated_root, [_rule("cl", "post_merge.changelog_entry")])
    checked: list[tuple[str, str]] = []

    def deny(identity: ActingIdentity, permission: str) -> bool:
        checked.append((identity.subject, permission))
        return False

    ws = _client(workspace, landing_delegate=_delegate("merged"), automation_permission_check=deny)
    _arm(ws)
    assert ("owner", "review:manage") in checked
    records = ws.get(f"/api/reviews/{ws.review_id}/automation").json()["post_merge"]["records"]
    assert [(item["status"], item["rule_id"]) for item in records] == [("refused", "cl")]
    refused = _activity(ws, "automation.refused")
    assert len(refused) == 1 and refused[0]["detail"]["rule_id"] == "cl"
    assert not _activity(ws, "post_merge.action")


def test_rule_inspection_shows_branch_protection_and_policy_errors(workspace: Workspace, _isolated_root: Path) -> None:
    _rules_file(
        _isolated_root,
        [
            _rule("auto-merge", "landing.native_merge"),
            {**_rule("sneaky", "landing.native_merge"), "override_branch_protection": True},
        ],
    )
    protection = {"facts": BranchProtectionFacts()}
    ws = _client(
        workspace,
        landing_delegate=_delegate("delegated"),
        branch_protection=lambda session, readiness: protection["facts"],
    )
    view = ws.get(f"/api/reviews/{ws.review_id}/automation").json()
    assert [item["id"] for item in view["policy"]["rules"]] == ["auto-merge"]
    assert view["policy"]["rules"][0]["reserved"] is True
    assert any("can never bypass" in item for item in view["policy"]["errors"])
    evaluation = view["evaluations"][0]
    guards = {item["name"]: item for item in evaluation["guards"]}
    assert evaluation["would_fire"] is False and evaluation["branch_protection_enforced"] is True
    assert guards["provider branch protection satisfied"]["actual"] == "unknown"
    assert guards["readiness is ready"]["passed"] is False

    _review_all_current_targets(ws)
    protection["facts"] = BranchProtectionFacts("satisfied", "unsatisfied", changes_requested=True, source="github")
    view = ws.get(f"/api/reviews/{ws.review_id}/automation").json()
    evaluation = view["evaluations"][0]
    assert evaluation["would_fire"] is False
    assert evaluation["branch_protection"]["changes_requested"] is True

    protection["facts"] = BranchProtectionFacts("satisfied", "satisfied", source="github")
    view = ws.get(f"/api/reviews/{ws.review_id}/automation").json()
    assert view["facts"]["values"]["readiness.ready"] is True
    assert view["evaluations"][0]["would_fire"] is True
    # Inspectable only: nothing in this package executes a landing action.
    assert landing_relationship(ws.store, ws.review_id) is None


# --------------------------------------------------------------------------- #
# Published feedback (item 11)
# --------------------------------------------------------------------------- #


class FakeGh:
    """The ``gh`` adapter seam: records every request shape, can fail like GitHub."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Any]] = []
        self.fail = ""

    def __call__(self, args: Sequence[str], stdin: str | None) -> str:
        self.calls.append((list(args), json.loads(stdin) if stdin else None))
        if self.fail:
            raise GitHubCliError(self.fail)
        return json.dumps({"id": 9001, "html_url": "https://github.com/acme/widgets/pull/7#issuecomment-9001"})


def _comment(ws: Workspace, body: str = "Rename this.") -> dict[str, Any]:
    posted = ws.post(
        f"/api/reviews/{ws.review_id}/annotations",
        json={"path": "src/session.py", "side": "new", "start_line": 4, "end_line": 4, "body": body},
    )
    assert posted.status_code == 201, posted.text
    return dict(posted.json()["annotation"])


def _publish(ws: Workspace, annotation_id: str, remote_ref: str, target_ref: str = DURABLE_PR) -> None:
    revision = ws.store.latest_revision(ws.review_id)
    assert revision is not None
    ws.store.record_delivery(
        DeliveryRecord(
            id="",
            annotation_id=annotation_id,
            target_type="github_review",
            target_ref=target_ref,
            state="sent",
            remote_ref=remote_ref,
            operation_id="gop-1",
            revision_id=revision.id,
            annotation_version=feedback_version(ws.store.list_annotation_versions(annotation_id)),
        )
    )


def _sync_client(workspace: Workspace, gh: FakeGh) -> Workspace:
    return _client(workspace, feedback_sync_provider=lambda session: GhFeedbackSyncProvider(gh))


def test_pending_feedback_edits_and_withdraws_locally(workspace: Workspace) -> None:
    gh = FakeGh()
    ws = _sync_client(workspace, gh)
    comment = _comment(ws)
    edited = ws.patch(f"/api/annotations/{comment['id']}", json={"body": "Rename this, please."})
    assert edited.status_code == 200 and edited.json()["annotation"]["body"] == "Rename this, please."
    withdrawn = ws.patch(f"/api/annotations/{comment['id']}", json={"state": "obsolete"})
    assert withdrawn.status_code == 200, withdrawn.text
    assert ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "edit", "body": "x"}).status_code == 409
    assert gh.calls == []  # nothing reached the provider


def test_published_inline_comment_edits_and_deletes_through_github(workspace: Workspace) -> None:
    gh = FakeGh()
    ws = _sync_client(workspace, gh)
    comment = _comment(ws)
    _publish(ws, comment["id"], "github-comment:555")

    local = ws.patch(f"/api/annotations/{comment['id']}", json={"body": "Silently changed"})
    assert local.status_code == 409 and local.json()["detail"]["code"] == "published_feedback"
    assert ws.patch(f"/api/annotations/{comment['id']}", json={"state": "obsolete"}).status_code == 409
    assert ws.store.get_annotation(comment["id"]).body == "Rename this."  # type: ignore[union-attr]

    synced = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "edit", "body": "Rename to refresh_user."})
    assert synced.status_code == 200, synced.text
    assert synced.json()["sync"]["state"] == "synced" and synced.json()["local_changed"] is True
    assert gh.calls[-1] == (
        [
            "api",
            "--hostname",
            "github.com",
            "--method",
            "PATCH",
            "repositories/4242/pulls/comments/555",
            "--input",
            "-",
        ],
        {"body": "Rename to refresh_user."},
    )
    assert ws.store.get_annotation(comment["id"]).body == "Rename to refresh_user."  # type: ignore[union-attr]
    # The edited version is recorded as on GitHub: never offered for a second publish.
    deliveries = ws.store.list_deliveries(comment["id"])
    latest = feedback_version(ws.store.list_annotation_versions(comment["id"]))
    assert any(item.annotation_version == latest and item.state == "sent" for item in deliveries)

    withdrawn = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "withdraw"})
    assert withdrawn.status_code == 200, withdrawn.text
    assert gh.calls[-1] == (
        ["api", "--hostname", "github.com", "--method", "DELETE", "repositories/4242/pulls/comments/555"],
        None,
    )
    assert ws.store.get_annotation(comment["id"]).state == "obsolete"  # type: ignore[union-attr]
    assert _activity(ws, "annotation.provider_synced")


def test_a_durable_target_is_sent_by_repository_id_to_its_recorded_host(workspace: Workspace) -> None:
    gh = FakeGh()
    ws = _sync_client(workspace, gh)
    comment = _comment(ws)
    # Published on a GitHub Enterprise host; acme/widgets was renamed since and
    # another repository took the old name. Repository 4242 is unique only there.
    _publish(ws, comment["id"], "github-comment:555", target_ref="github-repo:4242#7@ghe.example.com")

    synced = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "edit", "body": "Renamed."})
    assert synced.status_code == 200 and synced.json()["sync"]["state"] == "synced", synced.text
    patch = ["--method", "PATCH", "repositories/4242/pulls/comments/555", "--input", "-"]
    assert gh.calls == [(["api", "--hostname", "ghe.example.com", *patch], {"body": "Renamed."})]
    assert synced.json()["sync"]["request"]["host"] == "ghe.example.com"

    withdrawn = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "withdraw"})
    assert withdrawn.status_code == 200 and withdrawn.json()["sync"]["state"] == "synced", withdrawn.text
    delete = ["--method", "DELETE", "repositories/4242/pulls/comments/555"]
    assert gh.calls[-1] == (["api", "--hostname", "ghe.example.com", *delete], None)
    assert not any("acme/" in arg for args, _body in gh.calls for arg in args)


def test_a_durable_target_records_a_well_formed_host_or_nothing() -> None:
    assert durable_github_target("4242", "7", "GHE.Example.com") == "github-repo:4242#7@ghe.example.com"
    assert durable_github_target("4242", "7", "ghe.example.com:8443") == "github-repo:4242#7@ghe.example.com:8443"
    assert durable_github_target("4242", "7") == "github-repo:4242#7"  # hosted: its installation names the host
    for bad in ("evil.example/x", "user@evil.example", "-oops", "a b", "evil.example#9"):
        assert durable_github_target("4242", "7", bad) == "", bad
    for ref, host in (
        ("github-repo:4242#7@ghe.example.com", "ghe.example.com"),
        ("github-repo:4242#7", ""),
        ("github-repo:4242#7@user@evil.example", ""),
        ("github-repo:4242#7@", ""),
    ):
        assert parse_durable_github_target(ref) == ("4242", "7"), ref
        assert durable_github_host(ref) == host, ref
    assert parse_durable_github_target("github-repo:4242#x@ghe.example.com") is None


@pytest.mark.parametrize("host", ["", "--hostname=evil.example", "-oops", "evil.example/x", "GHE.example.com"])
def test_the_gh_adapter_never_sends_a_durable_request_to_an_unchecked_host(host: str) -> None:
    gh = FakeGh()
    request = ProviderRequest("DELETE", "repositories/4242/pulls/comments/555", {}, host)
    with pytest.raises(FeedbackSyncError, match="nothing was sent"):
        GhFeedbackSyncProvider(gh)._gh(request)
    assert gh.calls == []


@pytest.mark.parametrize(
    "target_ref",
    [
        "github-repo:4242#7",
        "github-repo:4242#7@",
        "github-repo:4242#7@-oops",
        "github-repo:4242#7@--hostname=evil.example",
        "github-repo:4242#7@evil.example/x",
        "github-repo:4242#7@user@evil.example",
        "github-repo:4242#7@GHE.example.com",
        "github-repo:4242#7@a b",
    ],
)
def test_a_durable_target_without_a_valid_recorded_host_sends_nothing(workspace: Workspace, target_ref: str) -> None:
    gh = FakeGh()
    ws = _sync_client(workspace, gh)
    comment = _comment(ws)
    _publish(ws, comment["id"], "github-comment:555", target_ref=target_ref)

    failed = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "edit", "body": "New text"})
    assert failed.status_code == 200 and failed.json()["sync"]["state"] == "sync_failed", failed.text
    assert "GitHub host" in failed.json()["sync"]["error"] and "nothing was sent" in failed.json()["sync"]["error"]
    assert gh.calls == []
    assert ws.store.get_annotation(comment["id"]).body == "Rename this."  # type: ignore[union-attr]


def test_a_provider_failure_is_visible_sync_failed_and_leaves_the_local_copy(workspace: Workspace) -> None:
    gh = FakeGh()
    ws = _sync_client(workspace, gh)
    comment = _comment(ws)
    _publish(ws, comment["id"], "github-comment:555")
    gh.fail = "HTTP 502: Bad Gateway (https://api.github.com/repos/acme/widgets/pulls/comments/555)"

    failed = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "edit", "body": "New text"})
    assert failed.status_code == 200, failed.text
    assert failed.json()["sync"]["state"] == "sync_failed" and failed.json()["local_changed"] is False
    assert "HTTP 502" in failed.json()["sync"]["error"]
    assert ws.store.get_annotation(comment["id"]).body == "Rename this."  # type: ignore[union-attr]
    view = ws.get(f"/api/reviews/{ws.review_id}/feedback-sync").json()
    row = view["comments"][comment["id"]]
    assert row["sync"]["state"] == "sync_failed" and row["sync"]["requested_body"] == "New text"
    assert row["sync"]["request"]["method"] == "PATCH"
    assert _activity(ws, "annotation.provider_sync_failed")

    gh.fail = ""
    retried = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "retry"})
    assert retried.json()["sync"]["state"] == "synced" and retried.json()["sync"]["attempts"] == 2
    assert ws.store.get_annotation(comment["id"]).body == "New text"  # type: ignore[union-attr]

    gh.fail = "HTTP 404"
    ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "withdraw"})
    discarded = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "discard"})
    assert discarded.json()["sync"]["state"] == "abandoned"
    assert ws.store.get_annotation(comment["id"]).state == "open"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("target_ref", "route"),
    [
        (DURABLE_PR, (["--hostname", "github.com"], "repositories/4242")),
        ("github-repo:4242#7@ghe.example.com", (["--hostname", "ghe.example.com"], "repositories/4242")),
    ],
)
def test_a_folded_comment_is_corrected_by_an_auditable_follow_up(
    workspace: Workspace, target_ref: str, route: tuple[list[str], str]
) -> None:
    gh = FakeGh()
    ws = _sync_client(workspace, gh)
    comment = _comment(ws)
    _publish(ws, comment["id"], "github-review:77", target_ref=target_ref)
    host, repo = route

    corrected = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "edit", "body": "Actually fine."})
    assert corrected.json()["sync"]["state"] == "corrected"
    assert corrected.json()["sync"]["correction_ref"] == "github-issue-comment:9001"
    args, payload = gh.calls[-1]
    assert args == ["api", *host, "--method", "POST", f"{repo}/issues/7/comments", "--input", "-"]
    assert "Correction from LemonCrow review" in payload["body"] and "Actually fine." in payload["body"]

    dismissed = ws.post(
        f"/api/reviews/{ws.review_id}/provider-review/dismiss",
        json={"remote_ref": "github-review:77", "message": "Superseded by the corrected review."},
    )
    assert dismissed.status_code == 200, dismissed.text
    assert gh.calls[-1] == (
        ["api", *host, "--method", "PUT", f"{repo}/pulls/7/reviews/77/dismissals", "--input", "-"],
        {"message": "Superseded by the corrected review.", "event": "DISMISS"},
    )
    unknown = ws.post(
        f"/api/reviews/{ws.review_id}/provider-review/dismiss",
        json={"remote_ref": "github-review:999", "message": "x"},
    )
    assert unknown.status_code == 404


@pytest.mark.parametrize(
    ("remote_ref", "op"),
    [("github-comment:555", "edit"), ("github-comment:555", "withdraw"), ("github-review:77", "edit")],
)
def test_a_legacy_by_name_target_is_never_sent_and_fails_visibly(
    workspace: Workspace, remote_ref: str, op: str
) -> None:
    """Nothing writes ``github:owner/repo#N`` any more; an old delivery that still has it is read, never trusted.

    A repository renamed or transferred since would make the name reach another
    repository, so the sync stops, says why, and leaves the local copy alone.
    """

    gh = FakeGh()
    ws = _sync_client(workspace, gh)
    comment = _comment(ws)
    _publish(ws, comment["id"], remote_ref, target_ref=LEGACY_PR)

    refused = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": op, "body": "New text"})
    assert refused.status_code == 409 and "durable GitHub id" in refused.json()["detail"], refused.text
    assert gh.calls == []
    assert ws.store.get_annotation(comment["id"]).body == "Rename this."  # type: ignore[union-attr]
    # Refused before anything was recorded: there is no failed request to retry or discard.
    assert ws.get(f"/api/reviews/{ws.review_id}/feedback-sync").json()["comments"][comment["id"]].get("sync") is None


def test_retrying_a_failed_sync_recorded_against_a_legacy_by_name_target_is_refused_unrecorded(
    workspace: Workspace,
) -> None:
    """A ``sync_failed`` row that predates durable identity cannot be revived: nothing is recorded or sent."""

    from lemoncrow.pro.capabilities.review.feedback_sync import (
        FeedbackSyncLedger,
        FeedbackSyncRecord,
        SyncEvent,
        sync_record_id,
    )

    gh = FakeGh()
    ws = _sync_client(workspace, gh)
    comment = _comment(ws)
    _publish(ws, comment["id"], "github-comment:555", target_ref=LEGACY_PR)
    revision = ws.store.latest_revision(ws.review_id)
    assert revision is not None
    ledger = FeedbackSyncLedger(ws.store)
    record = ledger.begin(
        FeedbackSyncRecord(
            id=sync_record_id(comment["id"]),
            review_id=ws.review_id,
            revision_id=revision.id,
            subject_id=comment["id"],
            remote_ref="github-comment:555",
            remote_kind="comment",
            target_ref=LEGACY_PR,
            provider="github",
            op="edit",
            state="sync_pending",
            actor_id="local",
            requested_body="New text",
            local_body="Rename this.",
            request={"method": "PATCH", "path": "repos/acme/widgets/pulls/comments/555", "body": {"body": "New text"}},
            events=(SyncEvent("2026-10-01T00:00:00+00:00", "sync_pending", "local"),),
            created_at="2026-10-01T00:00:00+00:00",
            updated_at="2026-10-01T00:00:00+00:00",
        )
    )
    failed = ledger.settle(record, state="sync_failed", error="HTTP 502")
    before = ledger.read(revision.id, sync_record_id(comment["id"]))
    assert before is not None and before[1].state == "sync_failed"

    retried = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "retry"})
    assert retried.status_code == 409 and "durable GitHub id" in retried.json()["detail"], retried.text
    assert gh.calls == []
    after = ledger.read(revision.id, sync_record_id(comment["id"]))
    assert after is not None and after[1] == before[1] == failed  # no sync_pending written, no attempt counted
    assert ws.store.get_annotation(comment["id"]).body == "Rename this."  # type: ignore[union-attr]
    discarded = ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "discard"})
    assert discarded.json()["sync"]["state"] == "abandoned"


def test_a_legacy_by_name_review_is_never_dismissed_by_name(workspace: Workspace) -> None:
    gh = FakeGh()
    ws = _sync_client(workspace, gh)
    comment = _comment(ws)
    _publish(ws, comment["id"], "github-review:77", target_ref=LEGACY_PR)
    dismissed = ws.post(
        f"/api/reviews/{ws.review_id}/provider-review/dismiss",
        json={"remote_ref": "github-review:77", "message": "Superseded."},
    )
    assert dismissed.status_code == 409 and "durable GitHub id" in dismissed.json()["detail"], dismissed.text
    assert gh.calls == []


def test_only_the_comment_author_syncs_and_no_provider_means_nothing_changes(workspace: Workspace) -> None:
    gh = FakeGh()
    ws = _sync_client(workspace, gh)
    comment = _comment(ws)
    _publish(ws, comment["id"], "github-comment:555")
    ws.store.update_annotation(comment["id"], created_by="someone-else")
    assert ws.post(f"/api/annotations/{comment['id']}/sync", json={"op": "withdraw"}).status_code == 403
    ws.store.update_annotation(comment["id"], created_by="local")
    plain = _client(workspace)
    refused = plain.post(f"/api/annotations/{comment['id']}/sync", json={"op": "withdraw"})
    assert refused.status_code == 409 and "nothing was changed" in refused.json()["detail"]
    assert gh.calls == []


# --------------------------------------------------------------------------- #
# Discard and delete safety (item 12)
# --------------------------------------------------------------------------- #


def _finish(ws: Workspace, **body: Any) -> Any:
    return ws.post(f"/api/reviews/{ws.review_id}/finish", json={"status": "archived", **body})


def test_discard_with_pending_feedback_and_an_armed_landing_needs_confirmation_naming_both(
    workspace: Workspace,
) -> None:
    calls: list[str] = []
    ws = _client(workspace, landing_delegate=_delegate("delegated", calls, cancel="raise"))
    _arm(ws)
    _comment(ws, "Unsent thought.")
    provider_calls = len(calls)

    preview = ws.get(f"/api/reviews/{ws.review_id}/finish").json()["discard"]
    assert preview["hazard_ids"] == ["pending_feedback", "armed_landing"]
    assert preview["deletes_now"] is False
    outcomes = {item["id"]: item["outcome"] for item in preview["hazards"]}
    assert "Nothing is sent" in outcomes["pending_feedback"]
    assert "until github confirms it" in outcomes["armed_landing"]

    bare = _finish(ws)
    assert bare.status_code == 409
    assert bare.json()["detail"]["code"] == "archive_confirmation_required"
    assert "pending feedback and armed landing" in bare.json()["detail"]["message"]
    one = _finish(ws, acknowledged_hazards=["armed_landing"], hazard_digest=preview["hazard_digest"])
    assert one.status_code == 409
    stale = _finish(ws, acknowledged_hazards=preview["hazard_ids"], hazard_digest="0" * 64)
    assert stale.status_code == 409 and "changed since it was confirmed" in stale.json()["detail"]["message"]
    assert ws.store.get_session(ws.review_id).status == "open"  # type: ignore[union-attr]
    assert len(calls) == provider_calls  # a refused discard asks the provider nothing

    done = _finish(ws, acknowledged_hazards=preview["hazard_ids"], hazard_digest=preview["hazard_digest"])
    assert done.status_code == 200, done.text
    discard = done.json()["discard"]
    assert discard["acknowledged"] == ["pending_feedback", "armed_landing"]
    assert discard["cancellation_owed"] is True
    assert discard["outcomes"]["armed_landing"]["provider_cancel"] == "pending"
    events = _activity(ws, "review.discard_acknowledged")
    assert len(events) == 1 and events[0]["detail"]["acknowledged"] == ["pending_feedback", "armed_landing"]

    # Finish/discard never deletes: the Review and its comments remain.
    assert ws.store.get_session(ws.review_id).status == "archived"  # type: ignore[union-attr]
    assert ws.store.list_annotations(ws.review_id)
    # The owed provider cancellation stays visible until the provider confirms it.
    readiness = ws.get(f"/api/reviews/{ws.review_id}/readiness").json()
    assert readiness["landing"]["provider_cancel"] == "pending"
    listed = ws.get("/api/reviews", params={"status_filter": "archived"}).json()["reviews"]
    row = next(item for item in listed if item["id"] == ws.review_id)
    assert row["owed_cancellation"]["provider"] == "github"

    recovered = _client(workspace, landing_delegate=_delegate("delegated"))
    cancelled = recovered.post(
        f"/api/reviews/{ws.review_id}/landing/cancel", json={"expected_revision_id": readiness["snapshot_id"]}
    )
    assert cancelled.status_code == 200, cancelled.text
    settled = landing_relationship(recovered.store, ws.review_id)
    assert settled is not None and landing_metadata(settled)["provider_cancel"] == "done"
    listed = recovered.get("/api/reviews", params={"status_filter": "archived"}).json()["reviews"]
    assert next(item for item in listed if item["id"] == ws.review_id)["owed_cancellation"] is None


def test_discard_with_nothing_at_stake_needs_no_confirmation(workspace: Workspace) -> None:
    done = _finish(workspace)
    assert done.status_code == 200, done.text
    assert "discard" not in done.json()


def _delete(ws: Workspace, **body: Any) -> Any:
    return ws.client.request(
        "DELETE", f"/api/reviews/{ws.review_id}", headers={"Authorization": f"Bearer {TOKEN}"}, json=body
    )


def test_hard_delete_needs_privilege_exact_identity_and_an_audit(workspace: Workspace) -> None:
    workspace.store.update_session(workspace.review_id, status="archived", title="Session refresh fix")
    audit: list[dict[str, Any]] = []

    unprivileged = _client(workspace, hard_delete_allowed=lambda session: False, deletion_audit=audit.append)
    assert _delete(unprivileged, confirm_identity=workspace.review_id).status_code == 403
    assert audit == [] and workspace.store.get_session(workspace.review_id) is not None

    ws = _client(workspace, deletion_audit=audit.append)
    for wrong in ("", workspace.review_id[:12], "session refresh fix", "Session refresh fix "):
        refused = _delete(ws, confirm_identity=wrong)
        assert refused.status_code == 400, wrong
        assert "exact Review id or its exact title" in refused.json()["detail"]
    assert audit == [] and workspace.store.get_session(workspace.review_id) is not None

    deleted = _delete(ws, confirm_identity="Session refresh fix")
    assert deleted.status_code == 200, deleted.text
    assert [item["outcome"] for item in audit] == ["deleting", "deleted"]
    assert audit[0]["event"] == "review.hard_delete" and audit[0]["review_id"] == workspace.review_id
    assert audit[0]["confirmed_by"] == "title"
    assert workspace.store.get_session(workspace.review_id) is None


def test_hard_delete_audits_to_the_local_file_and_refuses_without_an_audit(workspace: Workspace) -> None:
    workspace.store.update_session(workspace.review_id, status="archived")

    def broken(record: Any) -> None:
        raise OSError("disk full")

    failing = _client(workspace, deletion_audit=broken)
    refused = _delete(failing, confirm_identity=workspace.review_id)
    assert refused.status_code == 503 and "disk full" in refused.json()["detail"]
    assert workspace.store.get_session(workspace.review_id) is not None

    ws = _client(workspace)
    assert _delete(ws, confirm_identity=f"r/{workspace.review_id}").status_code == 200
    lines = (workspace.store_root / "audit" / "review-deletions.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["outcome"] for line in lines] == ["deleting", "deleted"]


def test_a_local_hard_delete_leaves_a_durable_tombstone_correlated_with_its_audit(workspace: Workspace) -> None:
    workspace.store.update_session(workspace.review_id, status="archived", title="Session refresh fix")
    audit: list[dict[str, Any]] = []
    ws = _client(workspace, deletion_audit=audit.append)
    deleted = _delete(ws, confirm_identity="Session refresh fix")
    assert deleted.status_code == 200, deleted.text
    wire = deleted.json()["tombstone"]

    # The Review and its own activity are gone; the tombstone in the store is not.
    assert workspace.store.get_session(workspace.review_id) is None
    assert workspace.store.list_activity_events(workspace.review_id) == ()
    [stone] = workspace.store.deletion_tombstones(workspace.review_id)
    assert wire["id"] == stone.id and wire["record_sha256"] == stone.record_sha256
    assert stone.actor_id == audit[0]["actor_id"]
    assert stone.request_id == audit[0]["request_id"] == audit[1]["request_id"]
    assert stone.request_id.startswith("local-")
    assert stone.repo_id == str(workspace.repo_root)
    assert stone.confirmation_sha256 == hashlib.sha256(b"Session refresh fix").hexdigest()
    assert stone.confirmed_by == "title"
    assert dict(stone.counts)["review_sessions"] == 1 and dict(stone.counts)["review_revisions"] >= 1
    assert "Session refresh fix" not in json.dumps(stone.to_wire())  # names the Review, never its content
    assert workspace.store.verify_deletion_tombstones() == ""


def test_a_hard_delete_whose_tombstone_cannot_be_written_deletes_nothing(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace.store.update_session(workspace.review_id, status="archived")

    def _unwritable(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(ReviewStore, "_write_deletion_tombstone", _unwritable)
    audit: list[dict[str, Any]] = []
    ws = _client(workspace, deletion_audit=audit.append)
    refused = _delete(ws, confirm_identity=workspace.review_id)
    assert refused.status_code == 503, refused.text
    assert "nothing was deleted" in refused.json()["detail"]
    assert workspace.store.get_session(workspace.review_id) is not None
    assert [item["outcome"] for item in audit] == ["deleting", "refused: the deletion tombstone could not be written"]
    monkeypatch.undo()
    assert workspace.store.deletion_tombstones() == ()


def test_finish_and_open_reviews_are_never_hard_deleted(workspace: Workspace) -> None:
    finished = workspace.post(f"/api/reviews/{workspace.review_id}/finish", json={"status": "finished"})
    assert finished.status_code == 200, finished.text
    assert workspace.store.get_session(workspace.review_id) is not None
    refused = _delete(workspace, confirm_identity=workspace.review_id)
    assert refused.status_code == 409
    assert workspace.store.get_session(workspace.review_id) is not None


def test_rule_set_wire_never_reads_repository_text(workspace: Workspace) -> None:
    """A rule file committed to the reviewed repository is ignored."""

    (workspace.repo_root / "review-automation.json").write_text(
        json.dumps({"version": 1, "rules": [_rule("repo", "post_merge.notification")]}), encoding="utf-8"
    )
    (workspace.repo_root / ".lemoncrow").mkdir(exist_ok=True)
    (workspace.repo_root / ".lemoncrow" / "review-automation.json").write_text(
        json.dumps({"version": 1, "rules": [_rule("repo", "post_merge.notification")]}), encoding="utf-8"
    )
    view = workspace.get(f"/api/reviews/{workspace.review_id}/automation").json()
    assert view["policy"]["rules"] == []
    assert "never repository text" in view["policy"]["edit_source"]
