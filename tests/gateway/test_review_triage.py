"""Phase 9 items 3-5, 9-12, 14: priority, criticality, severity, threshold and canonical order."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast, get_args

import pytest

from lemoncrow.pro.capabilities.review.behavior import (
    BehaviorDelta,
    BehaviorEvidenceRef,
    BehaviorObservation,
    behavior_queue_actions,
    behavior_targets,
    merge_queue_actions,
)
from lemoncrow.pro.capabilities.review.criticality import (
    BUILT_IN_ORIGIN,
    CRITICALITY_MIN_EFFORT,
    REQUIRED_FOCUS_CATEGORIES,
    normalize_finding_severity,
    operator_triage_layers,
    parse_trusted_base_triage,
    surfaced,
    target_policy_reasons,
    triage_policy_for,
)
from lemoncrow.pro.capabilities.review.review_priority import (
    TIER_DONE,
    TIER_POLICY_BLOCKER,
    TIER_REVIEW_FIRST,
    TIER_UNDERSTAND,
    assign_review_priority,
)
from lemoncrow.pro.capabilities.review.review_triage import (
    FINDING_EVIDENCE_KIND,
    TRIAGE_KIND,
    TRIAGE_SCOPE,
    VULNERABILITY_PRODUCER,
    CurrentFinding,
    PriorityOverrideRefused,
    ReviewTriage,
    SeveritySignal,
    active_override,
    ai_finding_evidence,
    behavior_signals,
    current_findings,
    dependency_package,
    derive_review_triage,
    finding_evidence_state,
    finding_queue_reasons,
    inbox_order_key,
    load_review_triage,
    order_for_inbox,
    persist_review_triage,
    priority_override_clear_event,
    priority_override_event,
    record_finding_evidence,
    stale_severity_signals,
    vulnerability_signals,
    with_override,
)
from lemoncrow.pro.capabilities.review.run_plan import (
    PlanInputs,
    SettingOrigin,
    build_run_plan,
    parse_review_config_layer,
    parse_run_settings,
    resolve_run_settings,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent
from lemoncrow.pro.capabilities.review.supply_chain import VulnerabilityFinding
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetAnnotationCounts, TargetVerification
from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

COMMIT = "c0ffee" * 6 + "abcd"
CONFIG = 'review:\n  effort: quick\n  focus:\n    - paths: ["src/auth/**"]\n      criticality: critical\n    - paths: ["src/payments/*.py"]\n      priority: first\n    - paths: ["docs/**"]\n      priority: last\n      criticality: low\n    - paths: ["src/auth/legacy/**"]\n      priority: last\n  finding_threshold: high\n'


def _policy(config: str = CONFIG, *, effective: Any = None, state: str = "present") -> Any:
    document = TrustedBaseDocument(".lemoncrow/review.yaml", config, cast(Any, state), "base .lemoncrow/review.yaml")
    return triage_policy_for(effective, document, commit=COMMIT)


def _target(key: str, path: str, *, state: str = "unreviewed", rank: int = 0) -> ReviewTarget:
    return ReviewTarget(
        target_id=f"target:{key}",
        unit_key=key,
        kind="symbol",
        path=path,
        label=f"{path}::{key}",
        symbol=key,
        start_line=1,
        end_line=1,
        hunk_ordinals=(0,),
        spans=(),
        state=state,
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=rank,
        attention_level="normal",
        structural_level="normal",
        reasons=(),
        additions=1,
        deletions=0,
        fingerprint_method="symbol_body_sha256",
        verification=TargetVerification(),
        annotation_counts=TargetAnnotationCounts(),
    )


def _delta(**overrides: Any) -> BehaviorDelta:
    base = BehaviorDelta(
        kind="api_response",
        producer="test.producer",
        subject="GET /health",
        title="Health endpoint",
        summary="HTTP 200 -> 500",
        base=BehaviorObservation("base", "", "digest-base", "HTTP 200", "artifact:ev-base"),
        head=BehaviorObservation("head", "", "digest-head", "HTTP 500", "artifact:ev-head"),
        evidence=(
            BehaviorEvidenceRef(ref="artifact:ev-base", kind="artifact", label="base record", digest="b1"),
            BehaviorEvidenceRef(ref="artifact:ev-head", kind="artifact", label="head record", digest="h1"),
        ),
    )
    return replace(base, **overrides)


class _DerivedStore:
    """Only ``review_derived`` rows: anything else a triage read touches fails the test."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}
        self.writes = 0

    def derived_results(self, kind: str, revision_ids: list[str]) -> dict[tuple[str, str], tuple[str, dict[str, Any]]]:
        return {
            (revision_id, scope): value
            for (revision_id, row_kind, scope), value in self.rows.items()
            if row_kind == kind and revision_id in revision_ids
        }

    def put_derived_results(self, results: Any) -> None:
        self.writes += 1
        for item in results:
            self.rows[item.revision_id, item.kind, item.scope_key] = (item.stamp, json.loads(json.dumps(item.payload)))

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"triage touched store.{name}")


def _finding(annotation_id: str, revision_id: str, *, state: str = "open", disposition: str = "open") -> Any:
    return SimpleNamespace(
        id=annotation_id,
        source="ai_review",
        parent_id="",
        state=state,
        finding_disposition=disposition,
        revision_id=revision_id,
        title=f"finding {annotation_id}",
        evidence=("target:target:auth",),
    )


def _event(kind: str, detail: dict[str, Any], *, revision_id: str = "r1", at: str, actor_type: str = "human") -> Any:
    return ReviewActivityEvent(
        id=f"rae-{at}",
        review_id="review-1",
        revision_id=revision_id,
        kind=kind,
        actor_id="alice",
        actor_type=actor_type,
        detail_json=json.dumps(detail),
        created_at=at,
    )


def test_triage_policy_contract() -> None:
    """case: test_path_policy_layers_strictest_criticality_and_last_review_first_override"""
    operator = {
        "triage": {
            "layers": [
                {
                    "origin": {"layer": "organization", "source": "operator_review_policy"},
                    "settings": {
                        "focus": [{"paths": ["src/auth/**"], "criticality": "high", "priority": "first"}],
                        "finding_threshold": "critical",
                    },
                }
            ]
        }
    }
    policy = _policy(effective=operator)
    auth = policy.classify("src/auth/login.py")
    assert auth.criticality == "critical"
    assert auth.criticality_origin.layer == "trusted_base_repository"
    assert auth.criticality_origin.commit == COMMIT
    assert auth.review_first == "first" and auth.review_first_origin.layer == "organization"
    legacy = policy.classify("src/auth/legacy/old.py")
    assert legacy.criticality == "critical" and legacy.review_first == "normal" and legacy.last_ignored
    assert policy.finding_threshold == "high"
    assert policy.threshold_origin.layer == "trusted_base_repository"
    assert policy.policy_state == "final"
    assert policy.classify("src/other.py").criticality == "normal"
    "case: test_invalid_or_unreadable_policy_is_visible_and_fails_closed"
    broken = _policy("review:\n  focus:\n    - paths: ['x/**']\n      criticality: extreme\n")
    assert broken.policy_state == "invalid"
    assert any("criticality must be" in error for error in broken.errors)
    provisional = _policy("", state="provisional")
    assert provisional.policy_state == "provisional"
    assert operator_triage_layers({"triage": "nonsense"})[0].errors
    for policy in (broken, provisional):
        triage = derive_review_triage(
            review_id="r",
            revision_id="s",
            policy=policy,
            changed_paths=("docs/a.md",),
            findings=(),
            evidence_state="current",
        )
        assert triage.priority == "normal"
    "case: test_run_settings_parser_leaves_triage_keys_to_their_owner"
    layer = parse_review_config_layer(CONFIG, source="base")
    assert layer is not None and (not layer.errors)
    assert parse_trusted_base_triage("review:\n  effort: quick\n", source="base") is None
    "case: test_criticality_raises_the_depth_floor_of_its_paths_but_is_not_a_severity"
    policy = _policy()
    assert CRITICALITY_MIN_EFFORT["critical"] == "thorough"
    critical = _plan(policy, ("src/auth/login.py",))
    investigators = critical.entry("ai.investigators")
    assert investigators is not None and investigators.depth == "thorough" and investigators.required
    assert investigators.floor_kind == "min_effort"
    assert "src/auth/**" in investigators.reason and COMMIT[:12] in investigators.reason
    ordinary = _plan(policy, ("src/util.py",))
    assert ordinary.entry("ai.investigators").depth == "quick"
    triage = derive_review_triage(
        review_id="r",
        revision_id="s",
        policy=policy,
        changed_paths=("src/auth/login.py",),
        findings=(),
        evidence_state="current",
    )
    assert triage.criticality == "critical"
    assert triage.severity_summary.highest is None
    assert triage.severity_summary.to_wire()["p0"] == 0
    assert triage.priority == "high"
    entry = next(item for item in triage.explanation if item.field == "criticality")
    assert entry.origin.layer == "trusted_base_repository" and entry.origin.commit == COMMIT
    "case: test_severity_is_normalized_by_policy_and_the_threshold_only_changes_surfacing"
    assert normalize_finding_severity("ai_review", category="critical_rule") == "p0"
    assert normalize_finding_severity("ai_review", category="security") == "p1"
    assert normalize_finding_severity("ai_review", category="performance") == "p2"
    assert normalize_finding_severity("ai_review", category="made_up") is None
    assert normalize_finding_severity("behavior", raw="low") == "suggestion"
    assert surfaced("p0", "critical") and surfaced(None, "critical")
    assert not surfaced("p1", "critical") and surfaced("p1", "high")
    assert not surfaced("suggestion", "medium") and surfaced("suggestion", "suggestions")
    loud = _policy(CONFIG.replace("finding_threshold: high", "finding_threshold: suggestions"))
    quiet = _policy(CONFIG.replace("finding_threshold: high", "finding_threshold: critical"))
    assert loud.finding_threshold != quiet.finding_threshold
    assert loud.effort_floors() == quiet.effort_floors()
    assert _plan(loud, ("src/auth/a.py",)).digest == _plan(quiet, ("src/auth/a.py",)).digest
    assert parse_review_config_layer(
        CONFIG.replace("finding_threshold: high", "finding_threshold: critical"), source="base"
    ) == parse_review_config_layer(CONFIG, source="base")
    findings = (
        CurrentFinding("a", "p1", surfaced=surfaced("p1", "critical")),
        CurrentFinding("b", "p0", surfaced=surfaced("p0", "critical")),
    )
    triage = derive_review_triage(
        review_id="r", revision_id="s", policy=quiet, changed_paths=("src/x.py",), findings=findings
    )
    assert triage.hidden_finding_ids == ("a",)
    assert triage.severity_summary.below_threshold == 1
    assert triage.priority == "urgent"


def test_triage_evidence_contract() -> None:
    """case: test_an_unreviewed_pr_never_looks_safe_because_finding_evidence_is_absent"""
    policy = _policy()
    state, detail = finding_evidence_state("none", stale_findings=0)
    unreviewed = derive_review_triage(
        review_id="r",
        revision_id="s",
        policy=policy,
        changed_paths=("docs/guide.md",),
        findings=(),
        evidence_state=state,
        evidence_detail=detail,
        review_state="not_reviewed",
    )
    assert unreviewed.evidence_state == "missing"
    assert unreviewed.priority == "normal"
    assert unreviewed.evidence_label == "Not reviewed · current evidence missing"
    priority = unreviewed.explanation[0]
    assert priority.field == "priority" and "current evidence missing" in priority.reason
    from lemoncrow.pro.capabilities.review.review_triage import missing_triage

    assert missing_triage("r", "s").priority == "normal" and missing_triage("r", "s").evidence_state == "missing"
    current = derive_review_triage(
        review_id="r",
        revision_id="s",
        policy=policy,
        changed_paths=("docs/guide.md",),
        findings=(),
        evidence_state="current",
    )
    assert current.priority == "low"
    assert (
        derive_review_triage(
            review_id="r", revision_id="s", policy=policy, changed_paths=(), findings=(), evidence_state="current"
        ).priority
        == "normal"
    )
    "case: test_stale_severity_stops_contributing_after_a_new_snapshot_until_revalidated"
    store = _DerivedStore()
    record_finding_evidence(
        store,
        review_id="review-1",
        revision_id="r1",
        rows=[
            ai_finding_evidence(
                "ann-1", finding_id="f1", category="security", target_ids=("target:auth",), title="bypass"
            )
        ],
    )
    annotations = [_finding("ann-1", "r1")]
    policy = _policy()

    def triage(revision_id: str, ai_status: str) -> ReviewTriage:
        findings, stale = current_findings(store, revision_id=revision_id, annotations=annotations)
        state, detail = finding_evidence_state(ai_status, stale_findings=stale)
        return derive_review_triage(
            review_id="review-1",
            revision_id=revision_id,
            policy=policy,
            changed_paths=("src/util.py",),
            findings=findings,
            stale_findings=stale,
            evidence_state=state,
            evidence_detail=detail,
        )

    first = triage("r1", "ready")
    assert first.severity_summary.p1 == 1 and first.priority == "high"
    assert finding_queue_reasons(current_findings(store, revision_id="r1", annotations=annotations)[0])
    second = triage("r2", "none")
    assert second.severity_summary.p1 == 0 and second.severity_summary.stale == 1
    assert second.evidence_state == "stale"
    assert second.priority == "normal"
    assert any(item.value == "stale" for item in second.explanation if item.field == "severity")
    assert not finding_queue_reasons(current_findings(store, revision_id="r2", annotations=annotations)[0])
    record_finding_evidence(
        store,
        review_id="review-1",
        revision_id="r2",
        rows=[ai_finding_evidence("ann-1", finding_id="f1", category="security", target_ids=(), title="bypass")],
    )
    third = triage("r2", "ready")
    assert third.severity_summary.p1 == 1 and third.severity_summary.stale == 0 and (third.priority == "high")
    dismissed = [_finding("ann-1", "r1", disposition="dismissed")]
    findings, _stale = current_findings(store, revision_id="r2", annotations=dismissed)
    assert [item.unresolved for item in findings] == [False]
    "case: test_overrides_are_audited_attributed_and_refused_for_non_humans"
    event = priority_override_event(
        review_id="review-1",
        revision_id="r1",
        level="urgent",
        reason="  release   blocker ",
        scope="snapshot",
        actor_id="alice",
        actor_type="human",
    )
    assert event.kind == "priority.override_set" and event.actor_id == "alice" and (event.actor_type == "human")
    assert json.loads(event.detail_json) == {"level": "urgent", "reason": "release blocker", "scope": "snapshot"}
    for actor_type in ("agent", "mixed", "unknown"):
        with pytest.raises(PriorityOverrideRefused) as refused:
            priority_override_event(
                review_id="review-1",
                revision_id="r1",
                level="low",
                reason="model ranked it low",
                scope="review",
                actor_id="bot",
                actor_type=actor_type,
            )
        assert refused.value.code == "not_human"
    for bad in ({"reason": ""}, {"level": "whenever"}, {"scope": "forever"}, {"actor_id": " "}):
        kwargs: dict[str, Any] = {
            "review_id": "review-1",
            "revision_id": "r1",
            "level": "low",
            "reason": "why",
            "scope": "review",
            "actor_id": "alice",
            "actor_type": "human",
            **bad,
        }
        with pytest.raises(PriorityOverrideRefused):
            priority_override_event(**kwargs)
    snapshot = _event("priority.override_set", {"level": "urgent", "reason": "x", "scope": "snapshot"}, at="1")
    active = active_override([snapshot], current_revision_id="r1")
    assert active is not None
    assert (active.actor_id, active.reason, active.created_at, active.scope) == ("alice", "x", "1", "snapshot")
    assert active_override([snapshot], current_revision_id="r2") is None
    review_scoped = _event("priority.override_set", {"level": "low", "reason": "y", "scope": "review"}, at="2")
    assert active_override([snapshot, review_scoped], current_revision_id="r9").level == "low"
    cleared = _event("priority.override_cleared", {"reason": "done"}, at="3")
    assert active_override([review_scoped, cleared, snapshot], current_revision_id="r1") is None
    clear = priority_override_clear_event(
        review_id="review-1", revision_id="r1", reason="done", actor_id="alice", actor_type="human"
    )
    assert clear.kind == "priority.override_cleared"
    forged = _event(
        "priority.override_set", {"level": "low", "reason": "z", "scope": "review"}, at="4", actor_type="agent"
    )
    assert active_override([review_scoped, forged], current_revision_id="r1") is None
    "case: test_priority_override_changes_order_but_never_criticality_or_required_checks"
    policy = _policy()
    triage = derive_review_triage(
        review_id="r",
        revision_id="s",
        policy=policy,
        changed_paths=("src/auth/login.py",),
        findings=(),
        evidence_state="current",
        required_failures=2,
    )
    override = active_override(
        [_event("priority.override_set", {"level": "low", "reason": "cosmetic", "scope": "review"}, at="1")],
        current_revision_id="s",
    )
    lowered = with_override(triage, override)
    assert lowered.priority == "low" and lowered.computed_priority == triage.priority == "high"
    assert lowered.criticality == "critical" and lowered.required_failures == 2
    assert lowered.explanation[0].field == "override"
    assert lowered.explanation[0].origin.layer == "override" and "alice" in lowered.explanation[0].origin.source
    assert policy.effort_floors() == _policy().effort_floors()
    other = replace(triage, review_id="q")
    assert inbox_order_key(lowered) > inbox_order_key(other)
    assert with_override(lowered, None).priority == "high"


def test_triage_projection_contract() -> None:
    """case: test_triage_writes_only_its_own_projection_rows"""
    "AI ranking cannot reach readiness, merge eligibility, assignment or permissions."
    store = _DerivedStore()
    triage = derive_review_triage(
        review_id="review-1", revision_id="r1", policy=_policy(), changed_paths=("src/auth/a.py",), findings=()
    )
    persist_review_triage(store, triage)
    persist_review_triage(store, triage)
    assert store.writes == 1
    assert {kind for _rev, kind, _scope in store.rows} == {TRIAGE_KIND}
    record_finding_evidence(
        store,
        review_id="review-1",
        revision_id="r1",
        rows=[ai_finding_evidence("a", finding_id="f", category="security", target_ids=(), title="t")],
    )
    assert {kind for _rev, kind, _scope in store.rows} == {TRIAGE_KIND, FINDING_EVIDENCE_KIND}
    "case: test_lightweight_projection_renders_without_loading_review_bodies"
    store = _DerivedStore()
    missing = load_review_triage(store, review_id="review-1", revision_id="r1")
    assert missing.computed is False and missing.evidence_state == "missing" and (missing.priority == "normal")
    triage = derive_review_triage(
        review_id="review-1",
        revision_id="r1",
        policy=_policy(),
        changed_paths=("src/auth/a.py",),
        findings=(CurrentFinding("x", "p1"),),
        evidence_state="current",
        review_state="in_progress",
    )
    persist_review_triage(store, triage)
    events = [_event("priority.override_set", {"level": "urgent", "reason": "ship", "scope": "review"}, at="1")]
    loaded = load_review_triage(store, review_id="review-1", revision_id="r1", events=events)
    assert loaded.computed and loaded.criticality == "critical" and (loaded.severity_summary.p1 == 1)
    assert loaded.priority == "urgent" and loaded.computed_priority == "high"
    assert loaded.review_state == "unknown"
    wire = loaded.to_wire()
    for field in ("priority", "criticality", "severity_summary", "evidence_state", "explanation"):
        assert field in wire
    assert all("origin" in entry for entry in wire["explanation"])
    store.rows["r1", TRIAGE_KIND, TRIAGE_SCOPE] = ("x", {**wire, "priority": "whatever"})
    assert load_review_triage(store, review_id="review-1", revision_id="r1").computed is False
    "case: test_inbox_order_is_deterministic_for_equal_evidence"
    base = ReviewTriage(review_id="", revision_id="s", evidence_state="current")
    rows = [replace(base, review_id=f"review-{index:02d}") for index in range(12)]
    rows += [
        replace(base, review_id="urgent", priority="urgent"),
        replace(base, review_id="missing", evidence_state="missing"),
        replace(base, review_id="critical", criticality="critical"),
    ]
    expected = [item.review_id for item in order_for_inbox(rows)]
    assert expected[:3] == ["urgent", "critical", "missing"]
    assert expected[3:] == sorted(expected[3:])
    for seed in range(5):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        assert [item.review_id for item in order_for_inbox(shuffled)] == expected
    "case: test_review_first_paths_come_earlier_with_a_visible_policy_reason"
    policy = _policy()
    targets = (
        _target("util", "src/util.py", rank=0),
        _target("docs", "docs/guide.md", rank=1),
        _target("auth", "src/auth/login.py", rank=2),
        _target("pay", "src/payments/charge.py", rank=3),
        _target("blocked", "src/zzz.py", state="needs_changes", rank=4),
        _target("done", "src/auth/done.py", state="reviewed", rank=5),
        _target("legacy", "src/auth/legacy/old.py", rank=6),
        _target("finding", "src/util2.py", rank=7),
    )
    reasons = target_policy_reasons(targets, policy)
    for target_id, rows in finding_queue_reasons(
        [CurrentFinding("ann", "p1", title="token bypass", target_ids=("target:finding",))]
    ).items():
        reasons.setdefault(target_id, []).extend(rows)
    ordered = assign_review_priority(targets, policy_reasons=reasons)
    order = [target.unit_key for target in ordered]
    assert order[:5] == ["blocked", "pay", "auth", "legacy", "finding"]
    assert order[5:] == ["util", "docs", "done"]
    by_key = {target.unit_key: target for target in ordered}
    assert by_key["pay"].priority.tier == TIER_REVIEW_FIRST
    assert by_key["pay"].priority.reasons[0].detail == "Review first · src/payments/*.py policy"
    assert by_key["auth"].priority.reasons[0].detail == "Critical · required by src/auth/** policy"
    assert by_key["finding"].priority.reasons[0].detail == "AI finding · P1 High: token bypass"
    assert by_key["docs"].priority.tier == TIER_UNDERSTAND
    assert "review_last_policy" in [reason.code for reason in by_key["docs"].priority.reasons]
    assert "review_last_policy" not in [reason.code for reason in by_key["legacy"].priority.reasons]
    assert by_key["blocked"].priority.tier == TIER_POLICY_BLOCKER
    assert by_key["done"].priority.tier == TIER_DONE
    assert all(reason.code != "criticality_policy" for reason in by_key["done"].priority.reasons)
    "case: test_behavior_targets_follow_the_same_policy_order"
    policy = _policy()
    plain = _delta(subject="GET /a", title="Plain", paths=("src/util.py",))
    auth = _delta(subject="GET /b", title="Auth", paths=("src/util.py", "src/auth/session.py"))
    docs = _delta(subject="GET /c", title="Docs", paths=("docs/api.md",))
    targets = behavior_targets([docs, plain, auth], [])
    actions = behavior_queue_actions(targets, policy=policy)
    by_title = {row["title"]: row for row in actions}
    assert by_title["Auth"]["tier"] == TIER_REVIEW_FIRST
    assert by_title["Auth"]["detail"] == "Critical · required by src/auth/** policy"
    assert by_title["Docs"]["demoted"] is True and by_title["Docs"]["tier"] == TIER_UNDERSTAND
    merged = merge_queue_actions(
        [
            {"id": "q:blocker", "tier": TIER_POLICY_BLOCKER},
            {"id": "decision", "tier": TIER_REVIEW_FIRST, "reason": "decision_open"},
            {"id": "q:understand", "tier": TIER_UNDERSTAND},
        ],
        actions,
    )
    titles = [row.get("title", row["id"]) for row in merged]
    assert titles == ["q:blocker", "Auth", "decision", "q:understand", "Plain", "Docs"]
    assert all("policy_class" not in row for row in behavior_queue_actions(targets))
    "case: test_explanation_names_the_layer_and_file_or_commit_for_each_value"
    triage = derive_review_triage(
        review_id="r",
        revision_id="s",
        policy=_policy(),
        changed_paths=("src/auth/login.py",),
        findings=(CurrentFinding("a", "p0"),),
        evidence_state="current",
        dependency_changes=1,
    )
    fields = {entry.field: entry for entry in triage.explanation}
    assert set(fields) >= {
        "priority",
        "criticality",
        "severity",
        "dependency",
        "release_urgency",
        "evidence",
        "finding_threshold",
    }
    assert fields["release_urgency"].value == "unknown" and fields["release_urgency"].origin == BUILT_IN_ORIGIN
    assert fields["finding_threshold"].origin.layer == "trusted_base_repository"
    assert fields["finding_threshold"].origin.commit == COMMIT
    assert fields["priority"].value == "urgent" and fields["priority"].origin.layer == "evidence"
    assert ReviewTriage.from_wire(triage.to_wire()) == triage


def test_triage_focus_contract() -> None:
    """case: test_behavior_and_vulnerability_severities_count_in_the_summary_and_priority"""
    behavior = behavior_signals(
        [
            _delta(producer=RUNTIME, severity="medium", critical=True, target_ids=("target:auth",)),
            _delta(producer=RUNTIME, subject="GET /docs", title="Docs", severity="low"),
        ],
        revision_id="r1",
    )
    assert [signal.severity for signal in behavior] == ["p1", "suggestion"]
    assert behavior[0].target_ids == ("target:auth",) and behavior[0].key.startswith("beh:")
    assert normalize_finding_severity("behavior", raw="high", critical=True) == "p1"
    assert normalize_finding_severity("behavior", raw="bogus", critical=True) == "p1"
    assert normalize_finding_severity("behavior", raw="bogus") is None
    assert dependency_package("npm:Lodash (web)") == "lodash"
    assert dependency_package("pypi:cryptography") == "cryptography"
    assert dependency_package("npm:lockfile metadata (web)") == ""
    vulnerable = _vulnerable({"lodash": ["dep-lodash"]})
    assert [(signal.key, signal.severity, signal.target_ids) for signal in vulnerable] == [
        ("vulnerability:lodash:GHSA-crit", "p0", ("target:dep-lodash",))
    ]
    triage = derive_review_triage(
        review_id="review-1",
        revision_id="r1",
        policy=_policy(),
        changed_paths=("src/util.py",),
        findings=(),
        signals=(*behavior, *vulnerable),
        evidence_state="current",
    )
    summary = triage.severity_summary
    assert (summary.p0, summary.p1, summary.p2, summary.suggestion, summary.stale) == (1, 1, 0, 1, 0)
    wire = summary.to_wire()["by_source"]
    assert wire["vulnerability"]["p0"] == 1 and wire["behavior"]["p1"] == 1 and (wire["ai_review"]["p0"] == 0)
    assert triage.priority == "urgent" and summary.highest == "p0"
    reasons = {entry.value: entry.reason for entry in triage.explanation if entry.field == "severity"}
    assert reasons["p0"] == "1 unresolved P0 Critical finding(s) with current evidence (1 vulnerability advisory)"
    assert reasons["p1"] == "1 unresolved P1 High finding(s) with current evidence (1 behavior change)"
    assert ReviewTriage.from_wire(triage.to_wire()) == triage
    "case: test_behavior_and_vulnerability_signals_go_stale_on_a_snapshot_that_did_not_rerun_them"
    first = (
        *behavior_signals([_delta(producer=RUNTIME, severity="high", critical=True)], revision_id="r1"),
        *_vulnerable({"lodash": []}),
    )
    ran_first = {RUNTIME, VULNERABILITY_PRODUCER}
    policy = _policy()

    def triage(signals: tuple[SeveritySignal, ...], ai_status: str = "ready") -> ReviewTriage:
        stale = sum(signal.stale for signal in signals)
        state, detail = finding_evidence_state(ai_status, stale_findings=0, stale_signals=stale)
        return derive_review_triage(
            review_id="review-1",
            revision_id="r2",
            policy=policy,
            changed_paths=("docs/guide.md",),
            findings=(),
            signals=signals,
            evidence_state=state,
            evidence_detail=detail,
        )

    assert triage(first).priority == "urgent"
    carried = stale_severity_signals((), (), [(first, ran_first)])
    assert {signal.key for signal in carried} == {signal.key for signal in first}
    assert all(signal.stale for signal in carried)
    second = triage(carried)
    assert (second.severity_summary.p0, second.severity_summary.p1, second.severity_summary.stale) == (0, 0, 2)
    evidence = next(entry for entry in second.explanation if entry.field == "evidence")
    assert second.evidence_state == "stale" and "not re-run" in evidence.reason
    assert second.priority == "normal"
    assert "stale findings not revalidated" in second.explanation[0].reason
    stale_entry = next(e for e in second.explanation if e.field == "severity" and e.value == "stale")
    assert "1 behavior change, 1 vulnerability advisory" in stale_entry.reason
    third = stale_severity_signals((), {RUNTIME}, [((), ()), (first, ran_first)])
    assert [signal.source for signal in third] == ["vulnerability"]
    assert stale_severity_signals(first[:1], {RUNTIME}, [(first, ran_first)]) == (replace(first[1], stale=True),)
    assert stale_severity_signals((), (), [((), {RUNTIME, VULNERABILITY_PRODUCER}), (first, ran_first)]) == ()

    def earlier() -> Any:
        yield ((), (RUNTIME, VULNERABILITY_PRODUCER))
        raise AssertionError("an older revision was computed after every producer had run")

    assert stale_severity_signals((), (), earlier(), producers=(RUNTIME, VULNERABILITY_PRODUCER)) == ()
    assert triage(()).priority == "low"
    "case: test_focus_require_is_a_typed_required_focus_fact"
    config = 'review:\n  focus:\n    - paths: ["src/db/**"]\n      require: [persistence, security]\n    - paths: ["docs/**"]\n      criticality: low\n      require: [backwards_compatibility]\n'
    policy = _policy(config)
    assert policy.policy_state == "final" and (not policy.errors)
    required = policy.required_focus(("src/db/models.py", "src/ui.py", "src/db/migrate.py"))
    assert [(item.category, item.paths) for item in required] == [
        ("security", ("src/db/models.py", "src/db/migrate.py")),
        ("persistence_migration", ("src/db/models.py", "src/db/migrate.py")),
    ]
    assert required[0].origins[0].layer == "trusted_base_repository" and required[0].origins[0].commit == COMMIT
    assert policy.required_focus(("src/ui.py",)) == ()
    triage = derive_review_triage(
        review_id="review-1",
        revision_id="r1",
        policy=policy,
        changed_paths=("src/db/models.py",),
        findings=(),
        evidence_state="current",
    )
    assert [item["category"] for item in triage.to_wire()["required_focus"]] == ["security", "persistence_migration"]
    explained = [entry for entry in triage.explanation if entry.field == "required_focus"]
    assert [entry.value for entry in explained] == ["security", "persistence_migration"]
    assert explained[0].origin.commit == COMMIT and "focus.require" in explained[0].reason
    assert ReviewTriage.from_wire(triage.to_wire()) == triage
    docs = derive_review_triage(
        review_id="review-1",
        revision_id="r1",
        policy=policy,
        changed_paths=("docs/guide.md",),
        findings=(),
        evidence_state="current",
    )
    assert docs.criticality == "low" and docs.priority == "normal"
    assert "review focus required" in docs.explanation[0].reason
    "case: test_focus_require_rejects_an_unsupported_category_visibly"
    policy = _policy('review:\n  focus:\n    - paths: ["src/**"]\n      require: [security, vibes]\n')
    assert policy.policy_state == "invalid"
    assert any("unsupported category 'vibes'" in error for error in policy.errors)
    assert [item.category for item in policy.required_focus(("src/a.py",))] == ["security"]
    only_invalid = _policy('review:\n  focus:\n    - paths: ["src/**"]\n      require: [vibes]\n')
    assert only_invalid.policy_state == "invalid" and only_invalid.required_focus(("src/a.py",)) == ()
    assert any("sets no priority, criticality, min_effort or require" in error for error in only_invalid.errors)
    "case: test_focus_require_vocabulary_is_the_ai_finding_categories"
    from lemoncrow.pro.capabilities.review.ai_review import FindingCategory

    assert set(REQUIRED_FOCUS_CATEGORIES) == set(get_args(FindingCategory)) - {"critical_rule"}


def _plan(policy: Any, changed: tuple[str, ...]) -> Any:
    settings = resolve_run_settings(
        [parse_run_settings({"effort": "quick"}, origin=SettingOrigin("trusted_base_repository", "base"))]
    )
    return build_run_plan(
        PlanInputs(
            review_id="review-1",
            revision_id="revision-1",
            trigger="open",
            settings=settings,
            detected=__import__(
                "lemoncrow.pro.capabilities.review.capability_catalog", fromlist=["DetectedCapabilities"]
            ).DetectedCapabilities(facts=frozenset()),
            changed_paths=changed,
            extra_floors=policy.effort_floors(),
        )
    )


RUNTIME = "lemoncrow.runtime_validation"


def _vulnerable(changed: dict[str, list[str]]) -> tuple[SeveritySignal, ...]:
    return vulnerability_signals(
        [
            VulnerabilityFinding("Lodash", "GHSA-crit", "CRITICAL"),
            VulnerabilityFinding("lodash", "GHSA-crit", ""),
            VulnerabilityFinding("left-pad", "GHSA-old", "HIGH"),
        ],
        revision_id="r1",
        changed_packages=changed,
    )
