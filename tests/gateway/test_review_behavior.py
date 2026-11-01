"""Behavior deltas as canonical Review targets: the v1 contract and its built-in producers.

Phase 9R items 11 (behavior targets), 12 (diff execution coverage), 18 (behavior
narrative), 38 (evidence before judgment) and 43 (manual verification checklist).
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import behavior as behavior_module
from lemoncrow.pro.capabilities.review.behavior import (
    BEHAVIOR_CONTRACT_VERSION,
    BEHAVIOR_KEY_PREFIX,
    BEHAVIOR_KINDS,
    BehaviorContext,
    BehaviorDelta,
    BehaviorEvidenceRef,
    BehaviorJudgmentRefused,
    BehaviorObservation,
    BehaviorProduction,
    CoverageSubject,
    ExecutedScope,
    ManualVerificationPolicy,
    behavior_evidence_digest,
    behavior_fingerprint,
    behavior_key,
    behavior_mark,
    behavior_progress,
    behavior_queue_actions,
    behavior_target_payload,
    behavior_targets,
    build_manual_checklist,
    check_behavior_judgment,
    collect_behavior,
    coverage_payload,
    deterministic_behavior_lines,
    execution_coverage,
    is_manual_check_evidence,
    manual_check_evidence,
    manual_checklist_payload,
    merge_queue_actions,
    narrative_payload,
    parse_manual_verification_policy,
    register_behavior_producer,
    registered_behavior_producers,
    unregister_behavior_producer,
    validate_behavior_narrative,
)
from lemoncrow.pro.capabilities.review.behavior_runtime import (
    BROWSER_CHECK_PRODUCER_ID,
    RUNTIME_VALIDATION_PRODUCER_ID,
    BrowserCheckBehaviorProducer,
    RuntimeValidationBehaviorProducer,
)
from lemoncrow.pro.capabilities.review.browser_checks import (
    BROWSER_CHECK_MEDIA_TYPE,
    BrowserPolicy,
    BrowserScenario,
    browser_source_ref,
    normalize_browser_run,
)
from lemoncrow.pro.capabilities.review.review_priority import (
    TIER_DONE,
    TIER_POLICY_BLOCKER,
    TIER_RE_REVIEW,
    TIER_REVIEW_FIRST,
    TIER_UNDERSTAND,
)
from lemoncrow.pro.capabilities.review.revisions import _BEHAVIOR_MARK_PREFIX, reconcile
from lemoncrow.pro.capabilities.review.runtime_validation import (
    build_surface_validation_request,
    normalize_surface_run,
    persist_validation_record,
    surface_validation_scenario,
    trusted_policy_digest,
)
from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewRevision, ReviewSession, ReviewUnit
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_evidence_id, new_revision_id, new_session_id
from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface, RuntimeConfigEntry
from lemoncrow.pro.capabilities.review.verification import build_verification_projection


def _delta(**overrides: Any) -> BehaviorDelta:
    base = BehaviorDelta(
        kind="api_response",
        producer="test.producer",
        subject="GET /health",
        title="Health endpoint",
        summary="HTTP 200 → 500",
        base=BehaviorObservation("base", "", "digest-base", "HTTP 200", "artifact:ev-base"),
        head=BehaviorObservation("head", "", "digest-head", "HTTP 500", "artifact:ev-head"),
        evidence=(
            BehaviorEvidenceRef(ref="artifact:ev-base", kind="artifact", label="base record", digest="b1"),
            BehaviorEvidenceRef(ref="artifact:ev-head", kind="artifact", label="head record", digest="h1"),
        ),
    )
    return replace(base, **overrides)


def _revision(revision_id: str = "rev-1") -> ReviewRevision:
    return ReviewRevision(
        id=revision_id,
        review_id="review-1",
        revision_number=1,
        range_mode="commit_range",
        tree_fingerprint="tree",
        source_fingerprint="source",
        packet_schema_version=1,
        base_sha="base",
        head_sha="head",
    )


def _artifact(evidence_id: str, *, revision_id: str = "rev-1", kind: str = "document") -> ReviewEvidence:
    return ReviewEvidence(
        id=evidence_id,
        review_id="review-1",
        revision_id=revision_id,
        kind=kind,
        title=evidence_id,
        content_hash="hash-" + evidence_id,
    )


def _context(evidence: tuple[ReviewEvidence, ...] = ()) -> BehaviorContext:
    return BehaviorContext(
        review_id="review-1",
        revision=_revision(),
        evidence=evidence or (_artifact("ev-base"), _artifact("ev-head")),
        read_artifact=lambda _item: None,
    )


class _Producer:
    contract_version = BEHAVIOR_CONTRACT_VERSION

    def __init__(self, producer_id: str, production: BehaviorProduction | None = None, error: Exception | None = None):
        self.producer_id = producer_id
        self._production = production or BehaviorProduction()
        self._error = error

    def produce(self, context: BehaviorContext) -> BehaviorProduction:
        del context
        if self._error is not None:
            raise self._error
        return self._production


def test_behavior_contract_identity() -> None:
    """case: test_contract_declares_every_required_behavior_kind_and_opaque_executor_classes"""
    for kind in (
        "test_outcome",
        "api_response",
        "visual",
        "side_effect",
        "migration",
        "verification_weakening",
        "spec_delta",
        "compatibility",
        "agent_instruction",
    ):
        assert kind in BEHAVIOR_KINDS
    assert set(behavior_module.EXECUTOR_CLASSES) == {"", "author_machine", "customer_worker", "managed_sandbox"}
    assert BEHAVIOR_CONTRACT_VERSION == 2
    "case: test_identity_is_stable_but_fingerprint_follows_observations_environment_and_executor"
    delta = _delta()
    key = behavior_key(delta)
    assert key.startswith(BEHAVIOR_KEY_PREFIX + "api_response:")
    reworded = _delta(title="Health", summary="other words", evidence=())
    assert behavior_key(reworded) == key
    assert behavior_fingerprint(reworded) == behavior_fingerprint(delta)
    moved = _delta(head=BehaviorObservation("head", "", "digest-head-2", "HTTP 500", "artifact:ev-head"))
    assert behavior_key(moved) == key
    assert behavior_fingerprint(moved) != behavior_fingerprint(delta)
    assert behavior_fingerprint(_delta(environment_fingerprint="env-2")) != behavior_fingerprint(delta)
    assert behavior_fingerprint(_delta(executor_class="managed_sandbox")) != behavior_fingerprint(delta)
    assert behavior_key(_delta(subject="GET /other")) != key
    "case: test_evidence_digest_covers_exactly_what_the_client_renders"
    delta = _delta()
    digest = behavior_evidence_digest(delta)
    assert behavior_evidence_digest(_delta()) == digest
    assert behavior_evidence_digest(_delta(summary="HTTP 200 → 502")) != digest
    swapped = (
        delta.evidence[0],
        BehaviorEvidenceRef(ref="artifact:ev-head", kind="artifact", label="head record", digest="h2"),
    )
    assert behavior_evidence_digest(_delta(evidence=swapped)) != digest
    payload = behavior_target_payload(behavior_targets([delta], [])[0])
    assert payload["evidence"]["digest"] == digest
    assert "evidence" not in behavior_target_payload(behavior_targets([delta], [])[0], include_evidence=False)
    "case: test_single_registration_point_refuses_duplicates_builtins_and_other_contract_versions"
    builtin_ids = [producer.producer_id for producer in registered_behavior_producers()]
    assert builtin_ids[:2] == [RUNTIME_VALIDATION_PRODUCER_ID, BROWSER_CHECK_PRODUCER_ID]
    producer = _Producer("r3.static_side_effects")
    register_behavior_producer(producer)
    try:
        assert registered_behavior_producers()[-1] is producer
        with pytest.raises(ValueError, match="duplicate"):
            register_behavior_producer(_Producer("r3.static_side_effects"))
        register_behavior_producer(_Producer("r3.static_side_effects"), replace_existing=True)
        with pytest.raises(ValueError, match="duplicate"):
            register_behavior_producer(_Producer(RUNTIME_VALIDATION_PRODUCER_ID), replace_existing=True)
        stale = _Producer("r5.differential")
        stale.contract_version = BEHAVIOR_CONTRACT_VERSION + 1
        with pytest.raises(ValueError, match="contract version"):
            register_behavior_producer(stale)
        with pytest.raises(ValueError, match="non-empty"):
            register_behavior_producer(_Producer("  "))
    finally:
        unregister_behavior_producer("r3.static_side_effects")
    assert [item.producer_id for item in registered_behavior_producers()] == builtin_ids


def test_behavior_v2_reopens_legacy_marks_on_risk_escalation_and_rejects_old_approval_digest() -> None:
    """A v1 judgment is preserved for audit, not silently accepted under v2."""
    import hashlib

    delta = _delta()
    [target] = behavior_targets([delta], [])
    mark = behavior_mark(target, review_id="review-1", revision_id="rev-1", reviewer_id="me", state="reviewed")
    old_payload = {
        "v": 1,
        "producer": delta.producer,
        "kind": delta.kind,
        "subject": delta.subject,
        "change": delta.change,
        "base": {"side": delta.base.side, "status": delta.base.status, "digest": delta.base.digest},
        "head": {"side": delta.head.side, "status": delta.head.status, "digest": delta.head.digest},
        "environment": delta.environment_fingerprint,
        "executor": delta.executor_class,
    }
    old_hash = hashlib.sha256(json.dumps(old_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    historical = replace(mark, content_fingerprint=old_hash)
    [reopened] = behavior_targets([delta], [historical])
    assert reopened.state == "changed_since_review" and reopened.priority.outstanding
    assert historical.content_fingerprint != target.fingerprint
    # Even a plausible digest from the earlier contract is no longer authority.
    old_evidence = {
        "fingerprint": old_hash,
        "title": delta.title,
        "summary": delta.summary,
        "base": delta.base.to_wire(),
        "head": delta.head.to_wire(),
        "evidence": sorted((ref.ref, ref.digest) for ref in delta.evidence),
        "facts": [list(item) for item in delta.facts],
    }
    old_digest = hashlib.sha256(json.dumps(old_evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(BehaviorJudgmentRefused) as refused:
        check_behavior_judgment(target, state="reviewed", evidence_digest=old_digest)
    assert refused.value.code == "evidence_mismatch"
    assert check_behavior_judgment(target, state="reviewed", evidence_digest=target.evidence_digest) == "reviewed"
    assert behavior_key(delta) == behavior_key(replace(delta, critical=True, severity="high"))
    for changed in (
        replace(delta, critical=True),
        replace(delta, severity="high"),
        replace(delta, unverified_integrations=("stripe",)),
    ):
        assert behavior_fingerprint(changed) != behavior_fingerprint(delta)
        [stale] = behavior_targets([changed], [mark])
        assert stale.state == "changed_since_review" and stale.priority.outstanding


def test_behavior_v2_approval_digest_covers_all_displayed_reference_metadata() -> None:
    base = _delta()
    ref = base.evidence[1]
    for changed in (
        replace(ref, label="Different evidence description"),
        replace(ref, kind="check"),
        replace(ref, side="base"),
        replace(ref, role="verification"),
        replace(ref, media_type="image/png"),
        replace(ref, evidence_id="ev-substituted"),
    ):
        updated = replace(base, evidence=(base.evidence[0], changed))
        assert behavior_evidence_digest(updated) != behavior_evidence_digest(base)
        assert behavior_key(updated) == behavior_key(base)
    # Evidence ordering is not a change when refs are otherwise identical.
    assert behavior_evidence_digest(replace(base, evidence=tuple(reversed(base.evidence)))) == behavior_evidence_digest(
        base
    )


def test_incompatible_behavior_contract_fails_closed_when_no_targets_survive() -> None:
    from lemoncrow.pro.capabilities.review.readiness import _behavior_condition

    outdated = _Producer("test.producer", BehaviorProduction(deltas=(_delta(contract_version=1),)))
    collected = collect_behavior(_context(), [outdated])
    assert collected.deltas == ()
    assert any("contract version 1 is not 2" in note for note in collected.degraded)
    # Missing targets from an obsolete producer must not silently look green.
    condition = _behavior_condition(0, 0, False, 0, incompatible_contracts=1)
    assert condition.state == "unknown"
    assert condition.reason == "incompatible_behavior_contract"
    assert "regenerated" in condition.detail


def test_behavior_collection_and_judgment_contract() -> None:
    """case: test_collection_validates_isolates_failures_drops_unresolved_refs_and_collapses_unchanged"""
    unchanged = _delta(
        subject="GET /same", head=BehaviorObservation("head", "", "digest-base", "HTTP 200", "artifact:ev-head")
    )
    forged_ref = _delta(
        subject="GET /forged",
        evidence=(
            BehaviorEvidenceRef(ref="artifact:ev-head", kind="artifact", label="head", digest="h1"),
            BehaviorEvidenceRef(ref="artifact:not-on-this-snapshot", kind="artifact", label="x"),
        ),
    )
    good = _Producer(
        "test.producer",
        BehaviorProduction(
            deltas=(
                _delta(),
                _delta(),
                unchanged,
                forged_ref,
                _delta(subject="bad kind", kind="telepathy"),
                _delta(subject="claims another producer", producer="someone.else"),
                _delta(subject="no sides", base=None, head=None),
            ),
            executed=(
                ExecutedScope(producer="test.producer", run_ref="artifact:ev-head", side="head", paths=("src/a.py",)),
                ExecutedScope(producer="test.producer", run_ref="check:never-recorded", side="head"),
            ),
            unchanged=2,
        ),
    )
    broken = _Producer("test.broken", error=RuntimeError("boom"))
    collection = collect_behavior(_context(), [broken, good])
    keys = [behavior_key(item) for item in collection.deltas]
    assert keys == [behavior_key(_delta()), behavior_key(forged_ref)]
    forged = collection.deltas[1]
    assert [ref.ref for ref in forged.evidence] == ["artifact:ev-head"]
    assert collection.unchanged == 3
    assert collection.producers == ("test.producer",)
    assert [scope.run_ref for scope in collection.executed] == ["artifact:ev-head"]
    notes = " | ".join(collection.degraded)
    for expected in (
        "test.broken: RuntimeError: boom",
        "did not resolve on this Snapshot and were dropped",
        "duplicate behavior",
        "unknown behavior kind",
        "claims producer",
        "neither a base nor a head",
        "run reference did not resolve",
    ):
        assert expected in notes
    "case: test_judgment_carries_forward_only_on_an_identical_fingerprint"
    delta = _delta()
    [target] = behavior_targets([delta], [])
    assert target.state == "unreviewed"
    assert target.priority.tier == TIER_UNDERSTAND and target.priority.outstanding
    mark = behavior_mark(target, review_id="review-1", revision_id="rev-1", reviewer_id="me", state="reviewed")
    assert mark.unit_key == target.behavior_key and mark.content_fingerprint == target.fingerprint
    assert mark.actor_type == "human"
    same = _delta(evidence=(BehaviorEvidenceRef(ref="artifact:ev-head-2", kind="artifact", label="head"),))
    [carried] = behavior_targets([same], [mark])
    assert carried.state == "reviewed" and (not carried.changed_since_mark)
    assert carried.priority.tier == TIER_DONE and (not carried.priority.outstanding)
    for moved in (
        _delta(head=BehaviorObservation("head", "", "digest-head-2", "HTTP 503", "artifact:ev-head")),
        _delta(environment_fingerprint="node-22"),
        _delta(executor_class="customer_worker"),
    ):
        [reopened] = behavior_targets([moved], [mark])
        assert reopened.state == "changed_since_review" and reopened.changed_since_mark
        assert reopened.priority.tier == TIER_RE_REVIEW and reopened.priority.outstanding
    blocked = replace(mark, state="needs_changes")
    [open_request] = behavior_targets([same], [blocked])
    assert open_request.state == "needs_changes" and open_request.priority.tier == TIER_POLICY_BLOCKER
    "case: test_reconciliation_carries_behavior_marks_instead_of_dropping_them"
    assert _BEHAVIOR_MARK_PREFIX == BEHAVIOR_KEY_PREFIX
    [target] = behavior_targets([_delta()], [])
    mark = behavior_mark(target, review_id="review-1", revision_id="rev-1", reviewer_id="me", state="reviewed")
    unit = ReviewUnit(
        revision_id="rev-1", unit_key="file:src/a.py", kind="file", path="src/a.py", content_fingerprint="c1"
    )
    result = reconcile((unit,), (), (mark,))
    assert mark in result.carried
    assert mark not in result.dropped and mark not in result.reopened
    "case: test_canonical_queue_orders_behavior_targets_and_merges_them_by_tier"
    critical = _delta(subject="GET /pay", title="Payments", critical=True, severity="high")
    routine = _delta(subject="GET /docs", title="Docs", severity="low")
    targets = behavior_targets([routine, critical], [])
    assert [item.delta.title for item in targets] == ["Payments", "Docs"]
    assert [item.priority.position for item in targets] == [0, 1]
    assert targets[0].priority.tier == TIER_REVIEW_FIRST
    actions = behavior_queue_actions(targets)
    assert [row["id"] for row in actions] == [f"behavior:{item.behavior_key}" for item in targets]
    merged = merge_queue_actions(
        [{"id": "q:blocker", "tier": TIER_POLICY_BLOCKER}, {"id": "q:understand", "tier": TIER_UNDERSTAND}], actions
    )
    assert [row["id"] for row in merged] == ["q:blocker", actions[0]["id"], "q:understand", actions[1]["id"]]
    reviewed = behavior_mark(targets[1], review_id="r", revision_id="rev-1", reviewer_id="me", state="reviewed")
    after = behavior_targets([routine, critical], [reviewed])
    assert [row["id"] for row in behavior_queue_actions(after)] == [actions[0]["id"]]
    assert behavior_progress(after) == {
        "total": 2,
        "reviewed": 1,
        "unreviewed": 1,
        "changed_since_review": 0,
        "needs_changes": 0,
        "unknown": 0,
        "critical": 1,
        "outstanding": 1,
    }
    "case: test_judgment_requires_the_displayed_evidence_digest_and_critical_is_never_bulk"
    [target] = behavior_targets([_delta()], [])
    [critical] = behavior_targets([_delta(subject="GET /pay", critical=True)], [])

    def refused(*args: Any, **kwargs: Any) -> str:
        with pytest.raises(BehaviorJudgmentRefused) as caught:
            check_behavior_judgment(*args, **kwargs)
        return caught.value.code

    assert refused(None, state="reviewed", evidence_digest="x") == "unknown_target"
    assert refused(target, state="approved", evidence_digest=target.evidence_digest) == "invalid_state"
    assert refused(target, state="reviewed", evidence_digest="") == "evidence_not_displayed"
    assert refused(target, state="needs_changes", evidence_digest=" ") == "evidence_not_displayed"
    assert refused(target, state="reviewed", evidence_digest="0" * 64) == "evidence_mismatch"
    assert check_behavior_judgment(target, state="reviewed", evidence_digest=target.evidence_digest) == "reviewed"
    assert check_behavior_judgment(target, state="reviewed", evidence_digest=target.evidence_digest, bulk=True)
    assert check_behavior_judgment(target, state="unreviewed", evidence_digest="") == "unreviewed"
    assert refused(critical, state="reviewed", evidence_digest=critical.evidence_digest, bulk=True) == "critical_bulk"
    assert check_behavior_judgment(critical, state="reviewed", evidence_digest=critical.evidence_digest)
    assert behavior_target_payload(critical)["bulk_eligible"] is False
    "case: test_contract_stores_no_viewing_telemetry"
    [target] = behavior_targets([_delta()], [])
    mark = behavior_mark(target, review_id="review-1", revision_id="rev-1", reviewer_id="me", state="reviewed")
    assert target.evidence_digest not in json.dumps(asdict(mark))
    assert not {"viewed", "viewed_at", "displayed", "displayed_at", "evidence_digest"} & set(asdict(mark))


def test_behavior_narrative_contract() -> None:
    """case: test_execution_coverage_only_speaks_when_something_ran"""
    subjects = (
        CoverageSubject("t:a", "src/a.py", "a"),
        CoverageSubject("t:b", "src/b.py", "b"),
        CoverageSubject("t:c", "web/c.tsx", "c"),
        CoverageSubject("t:d", "src/d.py", "d"),
    )
    assert execution_coverage(subjects, ()) is None
    assert coverage_payload(None) is None
    coverage = execution_coverage(
        subjects,
        (
            ExecutedScope(producer="p", run_ref="check:1", side="head", paths=("src/a.py",)),
            ExecutedScope(producer="p", run_ref="check:2", side="base", path_globs=("web/*.tsx",)),
            ExecutedScope(producer="p", run_ref="check:2", side="head", target_ids=("t:d",)),
        ),
    )
    assert coverage is not None
    payload = coverage_payload(coverage)
    assert payload is not None
    assert payload["runs"] == 2 and payload["changed_targets"] == 4 and (payload["exercised"] == 3)
    assert payload["never_executed"] == [{"target_id": "t:b", "path": "src/b.py", "label": "b"}]
    unmapped = execution_coverage(subjects, (ExecutedScope(producer="p", run_ref="check:9", side="head"),))
    assert unmapped is not None and len(unmapped.never_executed) == 4
    "case: test_narrative_keeps_only_statements_citing_typed_refs_of_the_deltas_they_name"
    targets = behavior_targets([_delta(), _delta(subject="GET /pay", title="Pay")], [])
    first, second = targets
    result = {
        "statements": [
            {
                "text": "Health now returns 500.",
                "behavior_keys": [first.behavior_key],
                "evidence_refs": ["artifact:ev-head"],
            },
            {"text": "Invented ref.", "behavior_keys": [first.behavior_key], "evidence_refs": ["artifact:made-up"]},
            {
                "text": "Invented key.",
                "behavior_keys": ["beh:api_response:nope"],
                "evidence_refs": ["artifact:ev-head"],
            },
            {"text": "No citation.", "behavior_keys": [first.behavior_key], "evidence_refs": []},
            {
                "text": "All tests pass now.",
                "behavior_keys": [second.behavior_key],
                "evidence_refs": ["artifact:ev-head"],
            },
            {
                "text": "Verified safe to merge.",
                "behavior_keys": [second.behavior_key],
                "evidence_refs": ["artifact:ev-base"],
            },
        ]
    }
    kept = validate_behavior_narrative(result, targets)
    assert [item.text for item in kept] == ["Health now returns 500."]
    assert kept[0].origin == "inferred" and kept[0].evidence_refs == ("artifact:ev-head",)
    inferred = narrative_payload(targets, synthesis={"status": "ready"}, model_result=result)
    assert inferred["mode"] == "inferred"
    shown_only_second = narrative_payload(
        targets, synthesis={"status": "ready"}, model_result=result, eligible=(second,)
    )
    assert shown_only_second["mode"] == "deterministic"
    "case: test_narrative_falls_back_to_deterministic_delta_lines_that_never_claim_a_pass"
    regression = _delta(
        kind="test_outcome",
        subject="checkout",
        title="Checkout",
        summary="Head run fails where the base run did not: assertion failed",
        base=BehaviorObservation("base", "PASS", "b", "3 deterministic check(s) passed", "artifact:ev-base"),
        head=BehaviorObservation("head", "FAIL", "h", "assertion failed", "artifact:ev-head"),
    )
    targets = behavior_targets([regression, _delta()], [])
    for model_result in (None, {"statements": []}, {"statements": "garbage"}):
        payload = narrative_payload(targets, synthesis={"status": "disabled"}, model_result=model_result)
        assert payload["mode"] == "deterministic"
        assert payload["statements"] == payload["deterministic"]
        assert len(payload["statements"]) == 2
        for row in payload["statements"]:
            assert row["origin"] == "deterministic"
            assert row["evidence_refs"] and all(ref.startswith("artifact:") for ref in row["evidence_refs"])
            assert behavior_module._VERDICT_WORDS.search(row["text"]) is None
            assert "PASS" not in row["text"]
    assert [line.behavior_keys for line in deterministic_behavior_lines(targets)] == [
        (item.behavior_key,) for item in targets
    ]


def test_behavior_manual_policy_contract() -> None:
    """case: test_manual_policy_parses_and_fails_visibly"""
    policy = parse_manual_verification_policy(_MANUAL_POLICY, source="base")
    assert policy.required is True and [item.id for item in policy.integrations] == ["stripe", "sso"]
    assert parse_manual_verification_policy("", source="base") == ManualVerificationPolicy(source="base")
    assert parse_manual_verification_policy("manual_verification: []", source="b").error
    duplicate = "manual_verification:\n  integrations:\n    - id: a\n    - id: a\n"
    assert "unique id" in parse_manual_verification_policy(duplicate, source="b").error
    assert parse_manual_verification_policy("manual_verification: {required: yes}", source="b").required is True
    assert parse_manual_verification_policy("manual_verification: {required: 'true'}", source="b").required is False
    "case: test_checklist_is_deterministic_and_required_only_when_policy_says_so"
    policy = parse_manual_verification_policy(_MANUAL_POLICY, source="base")
    webhook = _delta(subject="POST /hooks", title="Webhook", unverified_integrations=("Slack", "stripe"))
    targets = behavior_targets([webhook], [])
    items = build_manual_checklist(targets, policy, ("src/payments/charge.py", "README.md"))
    assert [(item.item_id, item.origin, item.required) for item in items] == [
        ("manual:stripe", "policy", True),
        ("manual:slack", "behavior", True),
    ]
    assert items[0].paths == ("src/payments/charge.py",)
    assert items[0].steps == ("Pay with a test card in staging",)
    assert items[0].behavior_keys == (targets[0].behavior_key,)
    assert build_manual_checklist(targets, policy, ("src/payments/charge.py",)) == items
    optional = replace(policy, required=False)
    assert not any(item.required for item in build_manual_checklist(targets, optional, ("src/payments/x.py",)))
    assert build_manual_checklist((), optional, ("README.md",)) == ()
    assert manual_checklist_payload((), optional, (), _revision()) is None
    "case: test_manual_result_is_a_human_observation_that_cannot_spoof_a_sandbox_check"
    "Phase 14 item 38: a human observation never stands in for a LemonCrow-executed result."
    policy = parse_manual_verification_policy(_MANUAL_POLICY, source="base")
    [item, *_rest] = build_manual_checklist((), policy, ("src/payments/charge.py",))
    revision = _revision()
    observed = manual_check_evidence(
        item, review_id="review-1", revision_id=revision.id, reviewer_id="alice", status="pass", note="looked ok"
    )
    assert observed.attestation_class == "human_observation"
    assert observed.source == "human" and observed.kind == "verification"
    assert observed.stable_key == item.item_id and observed.attested_by == "alice"
    assert is_manual_check_evidence(observed)
    with pytest.raises(ValueError):
        manual_check_evidence(item, review_id="r", revision_id=revision.id, reviewer_id="a", status="NOT_RUN")
    sandbox = ReviewEvidence(
        id="ev-sandbox",
        review_id="review-1",
        revision_id=revision.id,
        kind="verification",
        title=observed.title,
        source="test",
        source_ref="runtime-validation:x",
        verification_status="FAIL",
        attestation_class="lemoncrow_verification",
        stable_key="surface:fixture:stripe",
        created_at="2026-10-01T00:00:00Z",
    )
    human = replace(observed, id="ev-human", created_at="2026-10-02T00:00:00Z")
    projection = build_verification_projection(
        packet=None, evidence=(sandbox, human), revision=revision, revisions=(revision,)
    )
    rows = {(row["name"], row["attestation_class"]): row for row in projection["checks"] if row["state"] == "current"}
    assert rows[observed.title, "lemoncrow_verification"]["status"] == "FAIL"
    assert rows[observed.title, "human_observation"]["status"] == "PASS"
    payload = manual_checklist_payload((item,), policy, (sandbox, human), revision)
    assert payload is not None and payload["attestation_class"] == "human_observation"
    result = payload["items"][0]["result"]
    assert result["label"] == "Human observation" and result["attestation_class"] == "human_observation"
    forged = replace(human, id="ev-forged", attestation_class="lemoncrow_verification", created_at="2026-10-03")
    assert manual_checklist_payload((item,), policy, (forged,), revision)["items"][0]["result"] is None


_MANUAL_POLICY = '\nmanual_verification:\n  required: true\n  integrations:\n    - id: stripe\n      title: Stripe checkout\n      paths: ["src/payments/*"]\n      steps: ["Pay with a test card in staging"]\n    - id: sso\n      title: Single sign-on\n      paths: ["src/auth/*"]\n'


def _store_revision(tmp_path: Path) -> tuple[ReviewStore, ReviewRevision]:
    store = ReviewStore(tmp_path / "store")
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root=str(tmp_path.resolve()),
            range_mode="commit_range",
            title="behavior fixture",
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=1,
            range_mode="commit_range",
            tree_fingerprint="tree-behavior",
            source_fingerprint="source-behavior",
            packet_schema_version=1,
            base_sha="base",
            head_sha="head",
        ),
        (
            ReviewUnit(
                revision_id="",
                unit_key="file:src/api.py",
                kind="file",
                path="src/api.py",
                content_fingerprint="content-one",
            ),
        ),
    )
    return (store, revision)


def _persist_api_run(
    store: ReviewStore,
    revision: ReviewRevision,
    *,
    surface_id: str,
    side: str,
    status_code: int,
    body: Any,
    execution: Any = None,
    executor_class: str = "author_machine",
) -> ReviewEvidence:
    runtime = RuntimeConfigEntry(id="api", runner="http-service", values={"command": ["python", "app.py"]})
    surface = ReviewSurface(
        id=surface_id,
        provider="fixture",
        kind="api.request",
        title=f"GET /{surface_id}",
        runtime="api",
        affected_paths=("src/api.py",),
        capabilities=("source", "execute"),
        metadata={"expected_status": 200},
    )
    request = build_surface_validation_request(
        revision,
        surface,
        runtime,
        side=side,
        policy_digest=trusted_policy_digest("version: 1"),
        target_ids=("target:api",),
    )
    scenario = surface_validation_scenario(surface, runtime, target_ids=("target:api",))
    record = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            surface.id,
            surface.provider,
            side,
            "passed",
            f"GET /{surface_id} returned HTTP {status_code}",
            "http-service",
            runtime.id,
            exit_code=0,
            data={
                "status_code": status_code,
                "body": body,
                "headers": {"content-type": "application/json", "date": f"volatile-{side}"},
                "assertions": [],
                "assertions_executed": False,
                **({"execution": execution} if execution is not None else {}),
            },
        ),
    )
    return persist_validation_record(
        store, revision, request, scenario, record, path="src/api.py", source="test", executor_class=executor_class
    )


def test_runtime_validation_producer_turns_persisted_base_head_runs_into_behavior_targets(tmp_path: Path) -> None:
    store, revision = _store_revision(tmp_path)
    _persist_api_run(store, revision, surface_id="orders", side="old", status_code=200, body={"items": [1, 2]})
    _persist_api_run(store, revision, surface_id="orders", side="new", status_code=500, body={"error": "boom"})
    _persist_api_run(store, revision, surface_id="health", side="old", status_code=200, body={"ok": True})
    _persist_api_run(store, revision, surface_id="health", side="new", status_code=200, body={"ok": True})
    evidence = tuple(store.list_evidence(revision.review_id))
    context = BehaviorContext(
        review_id=revision.review_id,
        revision=revision,
        evidence=evidence,
        read_artifact=store.read_evidence_artifact,
        changed_paths=("src/api.py",),
    )
    collection = collect_behavior(context, [RuntimeValidationBehaviorProducer()])
    assert collection.degraded == ()
    assert collection.unchanged == 1
    assert {delta.kind for delta in collection.deltas} == {"test_outcome", "api_response"}
    for delta in collection.deltas:
        assert delta.producer == RUNTIME_VALIDATION_PRODUCER_ID
        assert delta.subject == "surface:fixture:orders"
        assert delta.critical is True and delta.severity == "high"
        assert delta.target_ids == ("target:api",) and delta.paths == ("src/api.py",)
        assert {ref.side for ref in delta.evidence} == {"base", "head"}
        ids = {item.id for item in evidence}
        assert all(ref.evidence_id in ids for ref in delta.evidence)
        assert delta.executor_class == "author_machine" and delta.environment_fingerprint == ""
    api = next(delta for delta in collection.deltas if delta.kind == "api_response")
    assert ("HTTP status", "200 → 500") in api.facts
    outcome = next(delta for delta in collection.deltas if delta.kind == "test_outcome")
    assert outcome.base is not None and outcome.base.status == "PASS"
    assert outcome.head is not None and outcome.head.status == "FAIL"
    assert collection.execution_ran and len(collection.executed) == 4
    assert {scope.paths for scope in collection.executed} == {("src/api.py",)}
    targets = behavior_targets(collection.deltas, ())
    assert all(target.priority.tier == TIER_REVIEW_FIRST for target in targets)


def _executed_api_deltas(
    root: Path,
    *,
    image: str,
    executors: tuple[str, str] = ("author_machine", "author_machine"),
    not_verified: tuple[str, ...] = (),
) -> tuple[BehaviorDelta, ...]:
    from lemoncrow.pro.capabilities.review.execution import (
        ExecutionRecipe,
        ExecutionSubject,
        ExecutorClass,
        StageOutcome,
        UnverifiedIntegration,
    )
    from lemoncrow.pro.capabilities.review.hermetic import HermeticPolicy, build_execution_result

    store, revision = _store_revision(root)
    policy = HermeticPolicy(not_verified=tuple(UnverifiedIntegration(name, "declared") for name in not_verified))
    for (side, code), executor in zip((("old", 200), ("new", 500)), executors, strict=True):
        execution = build_execution_result(
            subject=ExecutionSubject(
                review_id=revision.review_id, revision_id=revision.id, side=side, snapshot_identity="tree"
            ),
            executor_class=ExecutorClass.AUTHOR_MACHINE,
            policy=policy,
            recipe=ExecutionRecipe(command=("python", "app.py")),
            stages=(StageOutcome("run", "passed", exit_code=0),),
            namespace="head_untrusted",
            content_access="ai_denied",
            image_digest=image,
            toolchain=(("python3", "Python 3.13"),),
        ).to_wire()
        _persist_api_run(
            store,
            revision,
            surface_id="orders",
            side=side,
            status_code=code,
            body={"code": code},
            execution=execution,
            executor_class=executor,
        )
    context = BehaviorContext(
        review_id=revision.review_id,
        revision=revision,
        evidence=tuple(store.list_evidence(revision.review_id)),
        read_artifact=store.read_evidence_artifact,
        changed_paths=("src/api.py",),
    )
    collection = collect_behavior(context, [RuntimeValidationBehaviorProducer()])
    assert collection.degraded == ()
    return collection.deltas


def test_runtime_deltas_carry_the_execution_environment_and_executor_and_reopen_on_change(tmp_path: Path) -> None:
    deltas = _executed_api_deltas(tmp_path / "a", image="sha256:image-a")
    assert {delta.kind for delta in deltas} == {"test_outcome", "api_response"}
    for delta in deltas:
        assert delta.executor_class == "author_machine"
        assert delta.environment_fingerprint.startswith("envfp:sha256:")
        assert delta.unverified_integrations == ()
    marks = [
        behavior_mark(target, review_id="review-1", revision_id="rev-1", reviewer_id="me", state="reviewed")
        for target in behavior_targets(deltas, [])
    ]
    same = _executed_api_deltas(tmp_path / "b", image="sha256:image-a")
    assert {target.state for target in behavior_targets(same, marks)} == {"reviewed"}
    for moved in (
        _executed_api_deltas(tmp_path / "c", image="sha256:image-b"),
        _executed_api_deltas(tmp_path / "d", image="sha256:image-a", executors=("managed_sandbox", "managed_sandbox")),
    ):
        reopened = behavior_targets(moved, marks)
        assert {target.state for target in reopened} == {"changed_since_review"}
        assert all(target.changed_since_mark for target in reopened)


def test_runtime_delta_executor_is_the_least_trusted_side_and_reports_declared_integrations(tmp_path: Path) -> None:
    mixed = _executed_api_deltas(
        tmp_path / "mixed",
        image="sha256:image-a",
        executors=("author_machine", "managed_sandbox"),
        not_verified=("Stripe webhooks",),
    )
    sandboxed = _executed_api_deltas(
        tmp_path / "sandboxed", image="sha256:image-a", executors=("managed_sandbox", "managed_sandbox")
    )
    for delta in mixed:
        assert delta.executor_class == "author_machine"
        assert delta.unverified_integrations == ("Stripe webhooks",)
    assert {delta.executor_class for delta in sandboxed} == {"managed_sandbox"}


def test_runtime_validation_producer_refuses_records_whose_bytes_do_not_match(tmp_path: Path) -> None:
    store, revision = _store_revision(tmp_path)
    _persist_api_run(store, revision, surface_id="orders", side="old", status_code=200, body={"a": 1})
    _persist_api_run(store, revision, surface_id="orders", side="new", status_code=500, body={"a": 2})
    context = BehaviorContext(
        review_id=revision.review_id,
        revision=revision,
        evidence=tuple(store.list_evidence(revision.review_id)),
        read_artifact=lambda _item: b'{"side": "new", "status": "PASS"}',
    )
    collection = collect_behavior(context, [RuntimeValidationBehaviorProducer()])
    assert collection.deltas == () and collection.executed == ()
    assert len(collection.degraded) == 2 and "does not match its digest" in collection.degraded[0]


def _persist_browser_run(
    store: ReviewStore,
    revision: ReviewRevision,
    policy: BrowserPolicy,
    *,
    side: str,
    screenshot: bytes,
    diff_ratio: float | None,
) -> ReviewEvidence:
    shot_id = new_evidence_id()
    rel, digest, size = store.write_evidence_artifact(revision.review_id, revision.id, shot_id, screenshot)
    shot = store.add_evidence(
        ReviewEvidence(
            id=shot_id,
            review_id=revision.review_id,
            revision_id=revision.id,
            kind="screenshot",
            title=f"{side} screenshot",
            artifact_path=rel,
            content_hash=digest,
            mime_type="image/png",
            bytes=size,
            source="ci",
        )
    )
    record = normalize_browser_run(
        {
            "scenario": "checkout",
            "route": "/checkout",
            "side": side,
            "viewport": {"width": 1280, "height": 800},
            "target_url": "http://127.0.0.1:4173/checkout",
            "completed": True,
            "navigation": {"status": 200},
            "assertions": [{"name": "total shown", "status": "PASS"}],
            "console": [],
            "visual": {"diff_ratio": diff_ratio} if diff_ratio is not None else None,
            "artifacts": {"screenshot": shot.id},
        },
        policy=policy,
        snapshot=revision,
        resolve_artifact=store.get_evidence,
    )
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    record_id = new_evidence_id()
    rel, digest, size = store.write_evidence_artifact(revision.review_id, revision.id, record_id, payload)
    return store.add_evidence(
        ReviewEvidence(
            id=record_id,
            review_id=revision.review_id,
            revision_id=revision.id,
            kind="verification",
            title=str(record["name"]),
            artifact_path=rel,
            content_hash=digest,
            mime_type=BROWSER_CHECK_MEDIA_TYPE,
            bytes=size,
            source="ci",
            source_ref=browser_source_ref(record),
            verification_status=str(record["status"]) if side == "new" else "",
        )
    )


@pytest.mark.parametrize(("ratio", "critical"), [(0.002, False), (0.2, True)])
def test_browser_producer_turns_screenshot_comparisons_into_visual_targets(
    tmp_path: Path, ratio: float, critical: bool
) -> None:
    store, revision = _store_revision(tmp_path)
    policy = BrowserPolicy(
        scenarios=(
            BrowserScenario(
                id="checkout", title="Checkout", route="/checkout", paths=("src/*.py",), visual_threshold=0.05
            ),
        )
    )
    _persist_browser_run(store, revision, policy, side="old", screenshot=b"\x89PNG-base", diff_ratio=None)
    _persist_browser_run(store, revision, policy, side="new", screenshot=b"\x89PNG-head", diff_ratio=ratio)
    context = BehaviorContext(
        review_id=revision.review_id,
        revision=revision,
        evidence=tuple(store.list_evidence(revision.review_id)),
        read_artifact=store.read_evidence_artifact,
        changed_paths=("src/api.py", "README.md"),
        scenario_paths={"checkout": ("src/*.py",)},
    )
    collection = collect_behavior(context, [BrowserCheckBehaviorProducer()])
    assert collection.degraded == ()
    kinds = {delta.kind: delta for delta in collection.deltas}
    assert set(kinds) == ({"visual", "test_outcome"} if critical else {"visual"})
    visual = kinds["visual"]
    assert visual.producer == BROWSER_CHECK_PRODUCER_ID
    assert visual.subject == "checkout@1280x800"
    assert visual.critical is critical
    assert visual.paths == ("src/api.py",)
    roles = {(ref.side, ref.role) for ref in visual.evidence}
    assert {("head", "screenshot"), ("base", "screenshot"), ("head", "record")} <= roles
    coverage = execution_coverage(
        (CoverageSubject("t:api", "src/api.py", "api"), CoverageSubject("t:readme", "README.md", "readme")),
        collection.executed,
    )
    assert coverage is not None and [item.path for item in coverage.never_executed] == ["README.md"]


def test_browser_producer_collapses_identical_screenshots(tmp_path: Path) -> None:
    store, revision = _store_revision(tmp_path)
    policy = BrowserPolicy(scenarios=(BrowserScenario(id="checkout", title="Checkout", route="/checkout"),))
    _persist_browser_run(store, revision, policy, side="old", screenshot=b"\x89PNG-same", diff_ratio=None)
    _persist_browser_run(store, revision, policy, side="new", screenshot=b"\x89PNG-same", diff_ratio=0.0)
    context = BehaviorContext(
        review_id=revision.review_id,
        revision=revision,
        evidence=tuple(store.list_evidence(revision.review_id)),
        read_artifact=store.read_evidence_artifact,
    )
    collection = collect_behavior(context, [BrowserCheckBehaviorProducer()])
    assert collection.deltas == () and collection.unchanged == 1 and collection.execution_ran
