"""Spec §5, §7, §8: the deterministic PlanArtifact behind the Brief."""

from __future__ import annotations

from typing import Any

from lemoncrow.pro.capabilities.review.guidance import (
    AuthorGuidanceRecord,
    GuidanceView,
    build_guidance_view,
    parse_guidance,
    referenced_paths,
)
from lemoncrow.pro.capabilities.review.review_plan import (
    INTERNAL_ONLY_TEXT,
    SUMMARY_TEXT,
    apply_agent_context,
    build_review_plan,
    narrative_positions,
    projection_payload,
    question_reasons,
    review_questions,
)
from lemoncrow.pro.capabilities.review.review_priority import assign_review_priority
from lemoncrow.pro.capabilities.review.routine import RoutineProof
from lemoncrow.pro.capabilities.review.session_models import ReviewUnit
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetSpan

SESSION = "src/review/session.py"
STORE = "src/review/store.py"
DOCS = "docs/notes.md"
PRINTS = {SESSION: "fp-session", STORE: "fp-store", DOCS: "fp-docs"}


def _target(target_id: str, path: str, symbol: str = "", start: int = 1) -> ReviewTarget:
    return ReviewTarget(
        target_id=target_id,
        unit_key=f"symbol:{path}:{symbol or target_id}",
        kind="symbol",
        path=path,
        label=symbol or path,
        symbol=symbol,
        start_line=start,
        end_line=start + 1,
        hunk_ordinals=(0,),
        spans=(TargetSpan("new", start, start + 1, 0),),
        state="unreviewed",
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=0,
        attention_level="normal",
        reasons=(),
        additions=1,
        deletions=0,
        fingerprint_method="symbol_body_sha256",
    )


TARGETS = (
    _target("t-resolve", SESSION, "resolve_review", 1),
    _target("t-helper", SESSION, "_helper", 10),
    _target("t-prune", STORE, "prune_checkpoints", 1),
    _target("t-docs", DOCS, "", 1),
)
UNITS = tuple(
    (
        ReviewUnit(revision_id="rev", unit_key=f"file:{path}", kind="file", path=path, content_fingerprint=fingerprint)
        for path, fingerprint in PRINTS.items()
    )
)
PACKET: dict[str, Any] = {"files": [{"path": path, "old_path": None, "status": "modified"} for path in PRINTS]}
CHAPTERS = (
    {"key": "intent:review", "label": "Review", "targets": [{"target_id": "t-resolve"}, {"target_id": "t-helper"}]},
    {"key": "intent:storage", "label": "Storage", "targets": [{"target_id": "t-prune"}]},
    {"key": "intent:docs", "label": "Docs", "targets": [{"target_id": "t-docs"}]},
)


def _guidance(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "schema_version": 1,
        "intent": "Keep review history isolated by branch.",
        "items": [
            {
                "key": "branch-identity",
                "title": "Branch identity",
                "targets": [{"path": SESSION, "symbol": "resolve_review"}],
                "behavior": {"before": "Branches shared a review.", "after": "Each branch resolves its own review."},
                "rationale": "Preserve a continuous review across commits.",
                "evidence_refs": ["pytest review"],
            }
        ],
        "human_decisions": [
            {
                "key": "checkpoint-retention",
                "question": "How long should committed checkpoints be retained?",
                "options": [{"key": "indefinite", "label": "Forever"}, {"key": "ninety-days", "label": "90 days"}],
                "selected_option": "indefinite",
                "reason_human_required": "The requirement does not specify retention.",
                "targets": [{"path": STORE, "symbol": "prune_checkpoints"}],
            }
        ],
        "suggested_order": ["branch-identity"],
    }
    raw.update(overrides)
    return raw


def _record(
    raw: dict[str, Any], *, host: str = "claude", session: str = "cl-1", at: str = "2026-09-28T10:00:00+00:00"
) -> AuthorGuidanceRecord:
    payload = parse_guidance(raw)
    return AuthorGuidanceRecord(
        id=f"gdn-{host}-{at[11:19]}",
        producer_host=host,
        producer_session_id=session,
        producer_model="",
        attribution="exact",
        repo_root="/repo",
        source_identity=tuple((path, PRINTS.get(path, "")) for path in referenced_paths(payload)),
        payload=payload,
        created_at=at,
    )


def _view(*records: AuthorGuidanceRecord) -> GuidanceView:
    return build_guidance_view(records, targets=TARGETS, units=UNITS, packet=PACKET)


def _check(name: str, status: str = "PASS", *, state: str = "current", path: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "verification",
        "status": status,
        "state": state,
        "snapshot_number": 3 if state == "current" else 2,
        "source_ref": extra.pop("source_ref", ""),
        "path": path,
        "artifact": extra.pop("artifact", None),
        "required": extra.pop("required", False),
        **extra,
    }


def _plan(view: GuidanceView | None = None, checks: Any = (), proofs: Any = None, targets: Any = TARGETS) -> Any:
    return build_review_plan(
        targets=targets,
        guidance=view or _view(),
        checks=checks,
        chapters=CHAPTERS,
        routine_proofs=proofs or {},
        snapshot_number=3,
    )


def test_review_plan_author_context_contract() -> None:
    """case: test_captured_author_output_augments_brief_without_becoming_guidance"""
    base = _plan()
    enriched = apply_agent_context(
        base,
        (
            {
                "id": "dsc-plan",
                "message_id": "msg-plan",
                "producer": "codex:plan-2",
                "role": "plan",
                "source": "author",
                "kind": "intent",
                "title": "Keep branch review identity stable",
                "body": "Keep branch review identity stable across refreshed Snapshots.",
            },
            {
                "id": "ann-code",
                "message_id": "msg-code",
                "producer": "codex:code-2",
                "role": "code",
                "source": "author",
                "kind": "context",
                "title": "Implemented binding reuse",
                "body": "Implemented binding reuse and kept the existing branch base fixed.",
            },
            {
                "id": "ann-review",
                "message_id": "msg-review",
                "producer": "codex:review-2",
                "role": "review",
                "source": "ai_review",
                "kind": "context",
                "title": "Possible regression",
                "body": "This remains a reviewer finding, not author context.",
            },
        ),
    )
    assert ("codex:plan-2", "Keep branch review identity stable") in enriched.intents
    assert enriched.captured_intent_producers == ("codex:plan-2",)
    [context] = [item for item in enriched.items if item.id == "agent:ann-code"]
    assert context.kind == "behavior"
    assert context.rationale_origin == "author"
    assert context.source_label == "Captured author · Code"
    assert context.primary_target_ids == ()
    assert context.rationale == "Implemented binding reuse and kept the existing branch base fixed."
    assert not any(item.id == "agent:ann-review" for item in enriched.items)
    assert enriched.input_digest != base.input_digest
    "case: test_captured_plan_does_not_override_explicit_guidance_intent_for_same_producer"
    base = _plan(_view(_record(_guidance())))
    enriched = apply_agent_context(
        base,
        (
            {
                "id": "dsc-plan",
                "message_id": "msg-plan",
                "producer": "claude:cl-1",
                "role": "plan",
                "source": "author",
                "kind": "intent",
                "title": "A later captured plan headline",
                "body": "A later captured plan headline.",
            },
        ),
    )
    assert dict(enriched.intents)["claude:cl-1"] == "Keep review history isolated by branch."
    assert "claude:cl-1" not in enriched.captured_intent_producers


def test_review_plan_ownership_contract() -> None:
    """case: test_every_target_has_exactly_one_primary_owner"""
    plan = _plan(_view(_record(_guidance())))
    author = _item(plan, "item:branch-identity")
    assert (author.kind, author.primary_target_ids, author.rationale_origin) == ("behavior", ("t-resolve",), "author")
    assert author.source_label == "Author's agent note"
    primaries = [target for item in plan.items for target in item.primary_target_ids]
    assert sorted(primaries) == sorted(target.target_id for target in TARGETS)
    review = _item(plan, "area:intent:review")
    assert (review.kind, review.primary_target_ids, review.rationale_origin) == ("area", ("t-helper",), "none")
    assert (review.before, review.after, review.rationale) == ("", "", "")
    assert review.source_label == "LemonCrow observation"
    coverage = plan.coverage
    assert (coverage.target_count, coverage.author_explained, coverage.fallback_grouped, coverage.other) == (4, 1, 3, 0)
    "case: test_a_target_claimed_by_two_items_is_primary_once_and_related_elsewhere"
    raw = _guidance()
    raw["items"].append(
        {
            "key": "api-compat",
            "title": "API compatibility",
            "targets": [{"path": SESSION, "symbol": "resolve_review"}, {"path": SESSION, "symbol": "_helper"}],
            "behavior": {"after": "Old clients keep working."},
        }
    )
    raw["suggested_order"] = ["branch-identity", "api-compat"]
    plan = _plan(_view(_record(raw)))
    assert _item(plan, "item:branch-identity").primary_target_ids == ("t-resolve",)
    compat = _item(plan, "item:api-compat")
    assert (compat.primary_target_ids, compat.related_target_ids) == (("t-helper",), ("t-resolve",))
    assert plan.coverage.author_explained == 2
    "case: test_narrative_positions_follow_suggested_order_and_primary_ownership"
    raw = _guidance()
    raw["items"].append(
        {
            "key": "api-compat",
            "title": "API compatibility",
            "targets": [{"path": SESSION, "symbol": "_helper"}, {"path": SESSION, "symbol": "resolve_review"}],
            "behavior": {"after": "Old clients keep working."},
        }
    )
    raw["suggested_order"] = ["branch-identity", "api-compat"]
    view = _view(_record(raw))
    assert narrative_positions(TARGETS, view) == {"t-resolve": 0, "t-helper": 1}
    plan = _plan(view)
    assert _item(plan, "item:branch-identity").primary_target_ids == ("t-resolve",)
    assert _item(plan, "item:api-compat").primary_target_ids == ("t-helper",)
    raw["suggested_order"] = ["api-compat", "branch-identity"]
    assert narrative_positions(TARGETS, _view(_record(raw))) == {"t-helper": 0, "t-resolve": 0}
    assert narrative_positions(TARGETS, _view()) == {}


def test_review_plan_evidence_contract() -> None:
    """case: test_a_fabricated_evidence_ref_is_not_evidence"""
    raw = _guidance()
    raw["items"][0]["evidence_refs"] = ["made-up-check"]
    claim = _claim(_plan(_view(_record(raw)), checks=[_check("pytest review")]), "item:branch-identity")
    assert claim.evidence == ()
    assert claim.summary == "none"
    assert any("made-up-check" in text and "not recorded" in text for text in claim.limitations)
    "case: test_evidence_summary_language_follows_the_mapping_source"
    view = _view(_record(_guidance()))
    cited = _claim(_plan(view, checks=[_check("pytest review")]), "item:branch-identity")
    assert (cited.summary, SUMMARY_TEXT[cited.summary]) == ("author_cited", "Author cites these recorded checks")
    assert cited.evidence[0].mapping == "explicit"
    assert cited.result_line == "1 linked check passed on Snapshot 3"
    failing = _claim(_plan(view, checks=[_check("pytest review", "FAIL")]), "item:branch-identity")
    assert (failing.summary, SUMMARY_TEXT["contradictory"]) == ("contradictory", "Contradictory evidence")
    raw = _guidance()
    raw["items"][0]["evidence_refs"] = []
    inferred = _claim(_plan(_view(_record(raw)), checks=[_check("lint", path=SESSION)]), "item:branch-identity")
    assert (inferred.summary, SUMMARY_TEXT["inferred"]) == ("inferred", "Related checks passed · relevance inferred")
    assert inferred.evidence[0].mapping == "inferred"
    nothing = _claim(_plan(_view(_record(raw))), "item:branch-identity")
    assert (nothing.summary, SUMMARY_TEXT["none"]) == ("none", "No linked evidence")
    raw = _guidance()
    raw["items"][0]["concerns"] = [{"scope": "legacy migration", "reason": "Existing reviews were not exercised."}]
    partial = _claim(_plan(_view(_record(raw)), checks=[_check("pytest review")]), "item:branch-identity")
    assert (partial.summary, SUMMARY_TEXT["partial"]) == ("partial", "Partial evidence")
    assert "observed" not in {claim.summary for claim in (cited, failing, inferred, nothing, partial)}
    "case: test_stale_only_evidence_is_not_current_support"
    claim = _claim(
        _plan(_view(_record(_guidance())), checks=[_check("pytest review", state="stale")]), "item:branch-identity"
    )
    assert claim.summary == "none"
    assert any("earlier Snapshot" in text for text in claim.limitations)
    assert claim.evidence[0].state == "stale"
    "case: test_agent_reported_session_evidence_is_labelled_as_such"
    claim = _claim(
        _plan(_view(_record(_guidance())), checks=[_check("pytest review", state="session")]), "item:branch-identity"
    )
    assert claim.summary == "none"
    assert not any("earlier Snapshot" in text for text in claim.limitations)
    assert any("Agent-reported during authoring" in text for text in claim.limitations)
    assert "agent-reported" in claim.evidence[0].relevance
    "case: test_an_inferred_only_failure_is_not_contradictory"
    raw = _guidance()
    raw["items"][0]["evidence_refs"] = []
    claim = _claim(_plan(_view(_record(raw)), checks=[_check("lint", "FAIL", path=SESSION)]), "item:branch-identity")
    assert (claim.summary, SUMMARY_TEXT[claim.summary]) == (
        "inferred_failing",
        "Related checks failed · relevance inferred",
    )
    "case: test_evidence_links_say_when_and_where_each_check_ran"
    "Spec §8: execution time, recorder and environment when recorded, beside the result."
    raw = _guidance()
    raw["items"][0]["evidence_refs"] = ["pytest review", "browser:login"]
    checks = (
        _check("pytest review", observed_at="2026-09-28T10:00:00+00:00", source="ci"),
        _check(
            "browser:login",
            kind="browser",
            observed_at="2026-09-28T11:00:00+00:00",
            source="agent",
            browser={"run": {"environment": {"browser": "chromium", "os": "linux"}}},
        ),
    )
    plan = _plan(_view(_record(raw)), checks=checks)
    links = {link.key: link for link in _claim(plan, "item:branch-identity").evidence}
    assert (links["pytest review"].observed_at, links["pytest review"].source) == ("2026-09-28T10:00:00+00:00", "ci")
    assert links["pytest review"].environment == ()
    assert links["browser:login"].environment == (("browser", "chromium"), ("os", "linux"))
    (claim,) = [row for row in _projection(plan)["claims"] if row["id"] == "claim:branch-identity"]
    rows = {row["key"]: row for row in claim["evidence"]}
    assert (rows["pytest review"]["observed_at"], rows["pytest review"]["source"]) == (
        "2026-09-28T10:00:00+00:00",
        "ci",
    )
    assert rows["pytest review"]["environment"] == {}
    assert rows["browser:login"]["environment"] == {"browser": "chromium", "os": "linux"}


def test_review_plan_question_contract() -> None:
    """case: test_author_concerns_and_linked_failures_become_review_first_questions"""
    raw = _guidance()
    raw["items"][0]["concerns"] = [{"scope": "legacy migration", "reason": "Existing reviews were not exercised."}]
    view = _view(_record(raw))
    questions = review_questions(TARGETS, view, [_check("pytest review", "FAIL")])
    assert sorted(question.reason for question in questions) == [
        "check_failed_linked",
        "claim_evidence_scope_mismatch",
    ]
    assert all(question.target_ids == ("t-resolve",) and question.tier == 3 for question in questions)
    reasons = question_reasons(questions)
    assert sorted(reason.code for reason in reasons["t-resolve"]) == [
        "check_failed_linked",
        "claim_evidence_scope_mismatch",
    ]
    "case: test_a_conflict_is_one_question_and_no_decision"
    other = _guidance()
    other["human_decisions"][0]["selected_option"] = "ninety-days"
    view = _view(_record(_guidance()), _record(other, host="codex", session="cx-1", at="2026-09-28T11:00:00+00:00"))
    plan = _plan(view)
    assert plan.decisions == ()
    (question,) = [question for question in plan.questions if question.reason == "author_guidance_conflict"]
    assert question.target_ids == ("t-prune",)
    "case: test_proposed_decisions_are_read_only_with_resolved_targets"
    (decision,) = _plan(_view(_record(_guidance()))).decisions
    assert (decision.key, decision.read_only, decision.target_ids, decision.producer) == (
        "checkpoint-retention",
        True,
        ("t-prune",),
        "claude:cl-1",
    )
    "case: test_a_required_browser_failure_is_a_review_level_tier_one_question"
    failing = _check("Checkout", "FAIL", source_ref="browser:checkout", required=True, kind="browser")
    (question,) = review_questions(TARGETS, _view(), [failing])
    assert (question.reason, question.tier, question.review_level, question.target_ids) == (
        "required_browser_scenario_failed",
        1,
        True,
        (),
    )
    assert "browser:checkout failed on Snapshot 3" in question.detail
    assert question_reasons([question]) == {}
    optional = _check("Checkout", "FAIL", source_ref="browser:checkout", required=False, kind="browser")
    assert review_questions(TARGETS, _view(), [optional]) == ()


def test_review_plan_grouping_contract() -> None:
    """case: test_proven_routine_targets_share_one_collapsed_group"""
    proof = RoutineProof(DOCS, "comment_only", "python", "Comment-only change; code is byte-identical (Python)")
    plan = _plan(proofs={"t-docs": proof})
    routine = _item(plan, "group:routine")
    assert (routine.kind, routine.primary_target_ids) == ("mechanical", ("t-docs",))
    assert plan.coverage.routine == 1
    "case: test_without_guidance_only_deterministic_groups_exist"
    plan = _plan()
    assert {item.kind for item in plan.items} == {"area"}
    assert all(item.rationale_origin == "none" and (not item.after) for item in plan.items)
    assert plan.decisions == () and plan.questions == () and (plan.claims == ())
    "case: test_targets_outside_every_group_land_in_other_implementation_changes"
    extra = _target("t-orphan", "scripts/x.py", "main", 1)
    plan = _plan(targets=(*TARGETS, extra))
    other = _item(plan, "group:other")
    assert (other.title, other.primary_target_ids) == ("Other implementation changes", ("t-orphan",))
    assert plan.coverage.other == 1
    "case: test_a_tiny_change_produces_a_small_plan"
    tiny = (_target("t-only", SESSION, "resolve_review"),)
    plan = build_review_plan(
        targets=tiny,
        guidance=_view(),
        checks=(),
        chapters=({"key": "intent:review", "label": "Review", "targets": [{"target_id": "t-only"}]},),
        routine_proofs={},
        snapshot_number=0,
    )
    assert [item.id for item in plan.items] == ["area:intent:review"]
    assert plan.questions == ()
    "case: test_the_input_digest_is_stable_and_tracks_evidence"
    view = _view(_record(_guidance()))
    first = _plan(view, checks=[_check("pytest review")]).input_digest
    assert first == _plan(view, checks=[_check("pytest review")]).input_digest
    assert first != _plan(view, checks=[_check("pytest review", "FAIL")]).input_digest
    "case: test_the_delta_names_the_behaviors_and_areas_it_touches"
    "Spec §9 step 5: changed behavior and newly affected items, not only target counts."
    plan = _plan(_view(_record(_guidance())))
    delta = {
        "since_snapshot": 2,
        "added": ["t-prune"],
        "changed": ["t-resolve"],
        "removed": 0,
        "stale_evidence": [],
        "reopened_decisions": [],
    }
    assert _projection(plan, delta=delta)["delta"]["items"] == [
        {
            "id": "item:branch-identity",
            "title": "Branch identity",
            "kind": "behavior",
            "changed": ["t-resolve"],
            "added": [],
        },
        {"id": "area:intent:storage", "title": "Storage", "kind": "area", "changed": [], "added": ["t-prune"]},
    ]
    assert _projection(plan)["delta"] is None
    "case: test_an_internal_only_item_claims_unchanged_external_behavior"
    "Spec §3: an attributed claim with inspectable scope and evidence, not a guarantee."
    raw = _guidance()
    raw["items"][0]["behavior"] = {"unchanged": True}
    plan = _plan(_view(_record(raw)), checks=[_check("pytest review")])
    item = _item(plan, "item:branch-identity")
    assert item.internal_only and (item.before, item.after) == ("", "")
    claim = _claim(plan, "item:branch-identity")
    assert claim.statement == INTERNAL_ONLY_TEXT
    assert INTERNAL_ONLY_TEXT == "Internal implementation only — external behavior intended to remain unchanged."
    assert claim.summary == "author_cited", "its evidence is weighed like any other claim"
    (row,) = [row for row in _projection(plan)["items"] if row["id"] == "item:branch-identity"]
    assert row["internal_only"] is True
    assert all(row["internal_only"] is False for row in _projection(_plan())["items"])


def _item(plan: Any, item_id: str) -> Any:
    (item,) = [item for item in plan.items if item.id == item_id]
    return item


def _claim(plan: Any, item_id: str) -> Any:
    item = _item(plan, item_id)
    (claim,) = [claim for claim in plan.claims if claim.id in item.claim_ids]
    return claim


def _projection(plan: Any, **kwargs: Any) -> dict[str, Any]:
    return projection_payload(
        plan,
        assign_review_priority(TARGETS),
        review_id="review",
        revision_id="rev",
        snapshot_number=3,
        historical=False,
        **kwargs,
    )
