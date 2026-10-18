"""Phase 11 items 14-16: independent reviewer, observed identity, fresh re-run, author track record.

Pins the assurance contract end to end on the local runtime:

* observed model identity is persisted separately from the requested one,
  never guessed for a CLI default, bounded, and round-trips store -> API;
* ``independent_ai_reviewer`` is satisfied only by exact author provenance
  and an observed or pinned reviewer of a different model family; ``require``
  without that is a visible pending readiness condition, ``prefer`` labels;
* a fresh re-run executes again on the unchanged Snapshot under the Review
  budget, is distinguishable from reuse, replays idempotently, and ordinary
  requests and reloads keep reusing cache;
* the author track record is scored per agent/model only (never a human),
  over a bounded window, and a poor record escalates Auto within budget with
  a reason naming the agent/model and the metric.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import intelligence_drivers
from lemoncrow.pro.capabilities.review import readiness as readiness_module
from lemoncrow.pro.capabilities.review import track_record as track_record_module
from lemoncrow.pro.capabilities.review.ai_review import select_reviewer_route
from lemoncrow.pro.capabilities.review.capability_catalog import DETECTION_FACTS, STATIC_CATALOG, DetectedCapabilities
from lemoncrow.pro.capabilities.review.differential import CHECK_DIFFERENTIAL_MEDIA_TYPE
from lemoncrow.pro.capabilities.review.escalation import EscalationFacts
from lemoncrow.pro.capabilities.review.fresh_rerun import (
    execution_summary,
    fresh_rerun_preview,
    run_execution,
    valid_action_id,
)
from lemoncrow.pro.capabilities.review.independence import (
    CONDITION_ID,
    MAX_REVIEWER_ROUTES,
    REVIEWER_ROUTES_ENV,
    AgentAuthor,
    IndependenceFacts,
    assess_independence,
    assess_revision,
    authorship,
    configured_reviewer_routes,
    exact_agent_author,
    parse_independence_policy,
    provably_distinct,
    requirement_state,
    resolve_independence,
    reviewer_identities,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import (
    IntelligenceRunRequest,
    ModelCallBudget,
    ModelCallUsage,
    StructuredExecution,
    execute_structured,
    run_identity,
    run_intelligence,
)
from lemoncrow.pro.capabilities.review.model_identity import (
    MAX_OBSERVED_MODELS,
    UNREPORTED_DEFAULT_LABEL,
    ModelIdentity,
    clean_model_name,
    model_family,
    observed_from,
    reported_models_from_event,
    reported_models_from_output,
)
from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import apply_observations
from lemoncrow.pro.capabilities.review.readiness import (
    ConditionRegistry,
    ReadinessInputs,
    RegisteredCondition,
    built_in_policy_layer,
    fact_unavailable,
    policy_generation,
)
from lemoncrow.pro.capabilities.review.run_plan import (
    PlanInputs,
    SettingOrigin,
    build_run_plan,
    parse_run_settings,
    resolve_run_settings,
)
from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    AnnotationAnchor,
    ReviewEvidence,
    ReviewIntelligenceRun,
    ReviewRevision,
    ReviewSession,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_evidence_id, new_revision_id, new_session_id
from lemoncrow.pro.capabilities.review.track_record import (
    ChangeOutcome,
    TrackRecord,
    change_agent,
    change_outcome,
    scored_author,
    track_record,
    track_record_assessment,
    track_record_facts,
)
from tests.gateway.test_review_deployments import CONFIGURED, MERGED, OLD_SHA, _at, _obs
from tests.gateway.test_review_run_plan_api import _get, _post, _review

ORG = SettingOrigin("organization", "organization policy")
REPO = SettingOrigin("trusted_base_repository", "base Snapshot .lemoncrow/review.yaml")
AGENT = AgentAuthor("claude", "claude-sonnet-4-5")
_SCORE_TEXT = ("track record of", "revert rate", "escaped defects", "contradicted claims", "confirmed findings")


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.delenv(REVIEWER_ROUTES_ENV, raising=False)


def test_assurance_identity_contract() -> None:
    """case: test_model_family_is_conservative"""
    for model, family in [
        ("claude-sonnet-4-5", "anthropic"),
        ("claude-opus-4-1-20250805", "anthropic"),
        ("sonnet", "anthropic"),
        ("anthropic/claude-3-5-haiku", "anthropic"),
        ("gpt-4.1", "openai"),
        ("gpt-4.1-2025-04-14", "openai"),
        ("o3-mini", "openai"),
        ("gpt-5-codex", "openai"),
        ("gemini-2.5-pro", "google"),
        ("qwen3-coder", "alibaba"),
        ("review-model", ""),
        ("my-finetune", ""),
        ("deepseek-r1-distill-llama-70b", ""),
        ("claude-gpt-merge", ""),
        ("", ""),
    ]:
        assert model_family(model) == family
    "case: test_identity_source_label_and_provability"
    default = ModelIdentity("claude")
    assert (default.source, default.label, default.provable) == ("unreported_default", UNREPORTED_DEFAULT_LABEL, False)
    assert default.effective_models == ()
    pinned = ModelIdentity("openai", "gpt-4.1")
    assert (pinned.source, pinned.label, pinned.provable) == ("pinned", "gpt-4.1", True)
    observed = ModelIdentity("openai", "gpt-4.1", ("gpt-4.1-2025-04-14",))
    assert (observed.source, observed.label, observed.provable) == ("observed", "gpt-4.1-2025-04-14", True)
    assert observed.effective_models == ("gpt-4.1-2025-04-14",)
    wire = observed.to_wire()
    assert wire["requested_model"] == "gpt-4.1" and wire["observed_models"] == ["gpt-4.1-2025-04-14"]
    unverified = ModelIdentity("worker", unverified_model="gpt-4.1")
    assert (unverified.source, unverified.provable) == ("unverified", False)
    assert unverified.label == "gpt-4.1 (worker-reported)" and unverified.effective_models == ()
    truncated = ModelIdentity("openai", "gpt-4.1", ("a1", "a2", "a3", "a4"), observed_truncated=True)
    assert truncated.source == "observed" and (not truncated.provable) and truncated.label.endswith("(+more)")
    "case: test_observed_models_bound_at_and_past_the_limit"
    at = [f"gpt-4.1-{index}" for index in range(MAX_OBSERVED_MODELS)]
    assert observed_from([*at, *at, "", "bad name!", None, 7]) == (tuple(sorted(at)), False)
    past = [*at, "gpt-4.1-z"]
    kept, truncated = observed_from(past)
    assert (
        truncated is True and len(kept) == MAX_OBSERVED_MODELS and (kept == tuple(sorted(past))[:MAX_OBSERVED_MODELS])
    )
    assert clean_model_name("x" * 128) and (not clean_model_name("x" * 129))
    "case: test_persisted_identity_never_upgrades_itself"
    assert ModelIdentity.from_wire(None, driver="codex", requested_model="").source == "unreported_default"
    assert ModelIdentity.from_wire("junk", driver="openai", requested_model="gpt-4.1").source == "pinned"
    forged = ModelIdentity.from_wire({"driver": "claude", "source": "observed", "observed_models": []}, driver="claude")
    assert forged.source == "unreported_default"
    dirty = ModelIdentity.from_wire({"driver": "openai", "observed_models": ["gpt-4.1", "no spaces allowed"]})
    assert dirty.observed_models == ("gpt-4.1",)
    flagged = ModelIdentity.from_wire({"driver": "openai", "observed_models": ["gpt-4.1"], "observed_truncated": True})
    assert flagged.observed_truncated and (not flagged.provable)
    "case: test_reported_models_are_parsed_only_from_explicit_driver_events"
    claude_init = {"type": "system", "subtype": "init", "model": "claude-sonnet-4-5-20250929"}
    claude_message = {"type": "assistant", "message": {"model": "claude-sonnet-4-5-20250929", "content": []}}
    claude_result = {"type": "result", "modelUsage": {"claude-sonnet-4-5-20250929": {}, "claude-3-5-haiku": {}}}
    assert reported_models_from_event("claude", claude_init) == ("claude-sonnet-4-5-20250929",)
    assert reported_models_from_event("claude", claude_message) == ("claude-sonnet-4-5-20250929",)
    assert reported_models_from_event("claude", claude_result) == ("claude-sonnet-4-5-20250929", "claude-3-5-haiku")
    assert reported_models_from_event("claude", {"type": "result", "result": "{}"}) == ()
    codex_proto = {"msg": {"type": "session_configured", "model": "gpt-5-codex"}}
    assert reported_models_from_event("codex", codex_proto) == ("gpt-5-codex",)
    assert reported_models_from_event("codex", {"type": "thread.started", "thread_id": "t"}) == ()
    assert reported_models_from_event("codex", claude_init) == ()
    assert reported_models_from_event("opencode", claude_init) == ()
    lines = "\n".join(["not json", json.dumps(claude_init), "{broken", json.dumps(claude_result)])
    assert reported_models_from_output("claude", lines) == ("claude-sonnet-4-5-20250929", "claude-3-5-haiku")
    assert reported_models_from_output("claude", json.dumps(claude_result)) == (
        "claude-sonnet-4-5-20250929",
        "claude-3-5-haiku",
    )
    assert reported_models_from_output("claude", json.dumps({"ok": True})) == ()
    "case: test_action_ids_are_bounded_opaque_tokens"
    assert valid_action_id("act-1:retry.2_x") == "act-1:retry.2_x"
    assert valid_action_id("a" * 128) and (not valid_action_id("a" * 129))
    for bad in ("", "   ", "has space", "semi;colon", None, 7, ["a"]):
        assert valid_action_id(bad) == ""
    "case: test_authorship_needs_exact_provenance_and_humans_are_not_agents"
    agent = SimpleNamespace(actor_type="agent")
    assert authorship(agent, _rev()) == ("agent", AGENT)
    for certainty in ("probable", "possible", "none", ""):
        assert authorship(agent, _rev(provenance_certainty=certainty)) == ("unknown", None)
    assert authorship(agent, _rev(provenance_model="")) == ("unknown", None)
    assert authorship(SimpleNamespace(actor_type="human"), _rev(provenance_host="", provenance_certainty="none")) == (
        "human",
        None,
    )
    human_with_agent = authorship(SimpleNamespace(actor_type="human"), _rev(provenance_certainty="probable"))
    assert human_with_agent == ("unknown", None)
    assert exact_agent_author("Claude", "claude-sonnet-4-5", "exact") == AGENT
    assert exact_agent_author("bad host!", "claude-sonnet-4-5", "exact") is None
    "case: test_independence_is_never_labeled_without_proof"
    for kind, author, reviewers, status in [
        ("human", None, (PINNED_OPENAI,), "not_applicable"),
        ("unknown", None, (PINNED_OPENAI,), "unknown_author"),
        ("agent", AgentAuthor("claude", "my-finetune"), (PINNED_OPENAI,), "unknown_author"),
        ("agent", AGENT, (), "not_run"),
        ("agent", AGENT, (ModelIdentity("claude", "claude-sonnet-4-5"),), "not_independent"),
        ("agent", AGENT, (ModelIdentity("claude", "claude-opus-4-1"),), "not_independent"),
        ("agent", AGENT, (ModelIdentity("codex"),), "unreported_reviewer"),
        ("agent", AGENT, (ModelIdentity("worker", unverified_model="gpt-4.1"),), "unverified_reviewer"),
        ("agent", AGENT, (ModelIdentity("openai", "review-model"),), "unknown_reviewer"),
        (
            "agent",
            AGENT,
            (ModelIdentity("openai", "gpt-4.1", tuple(f"gpt-4.1-{i}" for i in range(4)), True),),
            "unknown_reviewer",
        ),
        ("agent", AGENT, (PINNED_OPENAI,), "independent"),
        ("agent", AGENT, (OBSERVED_OPENAI,), "independent"),
        ("agent", AGENT, (ModelIdentity("openai", "gpt-4.1", ("claude-sonnet-4-5",)),), "not_independent"),
        ("agent", AGENT, (PINNED_OPENAI, ModelIdentity("claude", "sonnet")), "not_independent"),
        ("agent", AGENT, (PINNED_OPENAI, ModelIdentity("claude")), "unreported_reviewer"),
    ]:
        assessment = assess_independence(kind, author, reviewers)
        assert assessment.status == status, assessment.reason
        assert assessment.independent is (status == "independent")
        assert requirement_state("require", assessment) == (
            "satisfied"
            if status == "independent"
            else "not_applicable" if status == "not_applicable" else "unsatisfied"
        )
        assert requirement_state("prefer", assessment) == (
            "preferred_met"
            if status == "independent"
            else "not_applicable" if status == "not_applicable" else "preferred_unmet"
        )
        assert requirement_state("off", assessment) == "not_required"
    "case: test_the_distinctness_rule_directly"
    assert provably_distinct("anthropic", PINNED_OPENAI)
    assert not provably_distinct("", PINNED_OPENAI)
    assert not provably_distinct("openai", PINNED_OPENAI)
    assert not provably_distinct("anthropic", ModelIdentity("codex"))
    assert not provably_distinct("anthropic", ModelIdentity("openai", "gpt-4.1", ("gpt-4.1", "claude-sonnet-4-5")))


def test_assurance_reviewer_route_contract() -> None:
    """case: test_every_reviewer_identity_on_a_snapshot_is_judged_in_either_order"""
    for weak_first in [True, False]:
        for weak, status in _WEAK_REVIEWERS:
            for distinct in (PINNED_OPENAI, OBSERVED_OPENAI):
                reviewers = (weak, distinct) if weak_first else (distinct, weak)
                assessment = assess_independence("agent", AGENT, reviewers)
                assert (assessment.status, assessment.independent) == (status, False), assessment.reason
                assert assessment.reviewers == reviewers
                assert requirement_state("require", assessment) == "unsatisfied"
                assert requirement_state("prefer", assessment) == "preferred_unmet"
                sandwiched = assess_independence("agent", AGENT, (distinct, weak, PINNED_OPENAI))
                assert (sandwiched.status, requirement_state("require", sandwiched)) == (status, "unsatisfied")
    "case: test_route_selection_prefers_a_pinned_distinct_route_and_never_a_default"
    kwargs: dict[str, Any] = {
        "author_driver": "claude",
        "author_model": "claude-sonnet-4-5",
        "author_certainty": "exact",
    }
    same = select_reviewer_route(driver="claude", model="claude-opus-4-1", **kwargs)
    assert (same.driver, same.model, same.independent) == ("claude", "claude-opus-4-1", False)
    default = select_reviewer_route(driver="claude", model=None, alternatives=(("codex", None),), **kwargs)
    assert default.independent is False and default.driver == "claude"
    chosen = select_reviewer_route(
        driver="claude", model=None, alternatives=(("codex", None), ("openai", "gpt-4.1")), **kwargs
    )
    assert (chosen.driver, chosen.model, chosen.independent) == ("openai", "gpt-4.1", True)
    unknown = select_reviewer_route(driver="openai", model="gpt-4.1", **{**kwargs, "author_certainty": "probable"})
    assert unknown.independent is False
    "case: test_reviewer_identities_use_settled_rows_else_an_unverified_worker_claim"
    ready = SimpleNamespace(status="ready", driver="openai", model="gpt-4.1", usage_json="{}")
    failed = SimpleNamespace(status="failed", driver="claude", model="", usage_json="{}")
    assert reviewer_identities([ready, failed, ready]) == (PINNED_OPENAI,)
    worker = reviewer_identities([failed], {"provider": "worker", "model": "gpt-4.1"})
    assert worker == (ModelIdentity("worker", unverified_model="gpt-4.1"),)
    assert assess_independence("agent", AGENT, worker).status == "unverified_reviewer"
    assert reviewer_identities([failed]) == ()


def test_assurance_policy_contract() -> None:
    """case: test_policy_parses_at_each_layer_and_is_a_floor_with_its_origin"""
    errors: list[str] = []
    assert [rule.mode for rule in parse_independence_policy("require", ORG, errors)] == ["require"]
    assert [rule.mode for rule in parse_independence_policy(False, ORG, errors)] == ["off"]
    scoped = parse_independence_policy(
        {"mode": "prefer", "paths": [{"paths": ["src/auth/**"], "mode": "require"}]}, REPO, errors
    )
    assert errors == []
    assert [(rule.mode, rule.paths) for rule in scoped] == [("prefer", ()), ("require", ("src/auth/**",))]
    org = parse_run_settings({"independent_ai_reviewer": "require"}, origin=ORG, allow_profiles=True)
    repo = parse_run_settings({"independent_ai_reviewer": "off"}, origin=REPO)
    floor = resolve_independence(resolve_run_settings([org, repo]).independence, ("src/app.py",))
    assert (floor.mode, floor.origin_label) == ("require", "organization policy")
    repo_scoped = parse_run_settings(
        {"independent_ai_reviewer": {"mode": "prefer", "paths": [{"paths": "src/auth/**", "mode": "require"}]}},
        origin=REPO,
    )
    settings = resolve_run_settings([repo_scoped])
    outside = resolve_independence(settings.independence, ("docs/readme.md",))
    assert (outside.mode, outside.matched_paths) == ("prefer", ())
    inside = resolve_independence(settings.independence, ("docs/readme.md", "src/auth/login.py"))
    assert (inside.mode, inside.matched_paths, inside.origin_label) == (
        "require",
        ("src/auth/login.py",),
        "base Snapshot .lemoncrow/review.yaml",
    )
    assert inside.to_wire()["paths"] == ["src/auth/**"]
    assert resolve_independence((), ("a.py",)).to_wire()["origin"]["layer"] == "built_in_default"
    document = repo_scoped.to_document()["independent_ai_reviewer"]
    reparsed = parse_run_settings({"independent_ai_reviewer": document}, origin=REPO)
    assert reparsed.independence == repo_scoped.independence
    assert "independent_ai_reviewer" in settings.to_wire()
    assert "independent_ai_reviewer" not in resolve_run_settings([]).to_wire()
    "case: test_policy_errors_are_reported_and_bounds_hold_at_and_past_each_limit"
    for raw, needle in (
        ("sometimes", "must be off, prefer, require"),
        ({"mode": "always"}, ".mode must be"),
        ({"surprise": 1}, "unknown field 'surprise'"),
        ({"paths": "src/**"}, ".paths must be a list"),
        ({"paths": [{"paths": [], "mode": "require"}]}, "non-empty list"),
        ({"paths": [{"paths": ["a"], "mode": "nope"}]}, "paths[0].mode"),
    ):
        errors: list[str] = []
        parse_independence_policy(raw, ORG, errors)
        assert any(needle in error for error in errors), (raw, errors)
    at = [{"paths": [f"p{i}/**"], "mode": "require"} for i in range(64)]
    errors = []
    assert len(parse_independence_policy({"paths": at}, ORG, errors)) == 64 and errors == []
    errors = []
    parsed = parse_independence_policy({"paths": [*at, {"paths": ["x/**"], "mode": "require"}]}, ORG, errors)
    assert len(parsed) == 64 and any("exceeds 64 rules" in error for error in errors)
    globs = [f"g{i}/**" for i in range(65)]
    errors = []
    [rule] = parse_independence_policy({"paths": [{"paths": globs, "mode": "require"}]}, ORG, errors)
    assert len(rule.paths) == 64 and any("at most 64" in error for error in errors)
    invalid = parse_run_settings({"independent_ai_reviewer": "sometimes"}, origin=REPO)
    assert invalid.errors and invalid.independence == ()
    "case: test_require_is_a_visible_readiness_condition_never_silently_satisfied"
    assert _readiness({}) == []
    [pending] = _readiness({"independent_ai_review": _facts("require", "unreported_reviewer")})
    assert (pending.id, pending.state, pending.reason) == (CONDITION_ID, "pending", "unreported_reviewer")
    assert pending.detail.startswith("Required by org: not satisfied;")
    [passing] = _readiness({"independent_ai_review": _facts("require", "independent")})
    assert (passing.state, passing.reason) == ("pass", "independent")
    assert _readiness({"independent_ai_review": _facts("prefer", "unreported_reviewer")}) == []
    assert _readiness({"independent_ai_review": _facts("require", "not_applicable")}) == []
    [broken] = _readiness({"independent_ai_review": fact_unavailable("store offline")})
    assert broken.state == "unknown"
    [wrong] = _readiness({"independent_ai_review": {"mode": "require"}})
    assert wrong.state == "unknown"
    "case: test_configured_reviewer_routes_are_bounded_and_report_what_they_ignore"
    at = ",".join(f"openai=gpt-4.1-{i}" for i in range(MAX_REVIEWER_ROUTES))
    routes, ignored = configured_reviewer_routes({REVIEWER_ROUTES_ENV: at})
    assert len(routes) == MAX_REVIEWER_ROUTES and ignored == 0
    routes, ignored = configured_reviewer_routes({REVIEWER_ROUTES_ENV: at + ",openai=gpt-4.1-z,=x,codex=,bad host=m"})
    assert len(routes) == MAX_REVIEWER_ROUTES and ignored == 4
    assert configured_reviewer_routes({}) == ((), 0)
    "case: test_fresh_rerun_preview_names_execution_and_cost_class"
    plan = build_run_plan(
        PlanInputs(
            review_id="review-1",
            revision_id="revision-1",
            trigger="manual",
            settings=resolve_run_settings([parse_run_settings({"effort": "thorough"}, origin=ORG)]),
            detected=DetectedCapabilities(facts=frozenset(DETECTION_FACTS)),
            changed_paths=("src/app.py",),
            ai_candidates=3,
            risk_signal=True,
        )
    )
    running = [row for row in plan.entries if row.id.startswith("ai.") and row.status == "run"]
    preview = fresh_rerun_preview(
        plan,
        calls_used=3,
        max_calls=4,
        provider="openai",
        model_label="gpt-4.1",
        model_data_locus="external",
        available=True,
    )
    expected = sum(
        row.estimated_model_calls
        for row in running
        if row.id in {"ai.investigators", "ai.hypotheses", "ai.rule_checks"}
    )
    assert preview["estimated_model_calls"] == expected > 1
    assert preview["budget"] == {"calls_used": 3, "max_calls": 4, "remaining": 1}
    assert preview["within_budget"] is False
    assert preview["cost_class"] in {row.cost_class for row in running}
    assert preview["effort"] == "thorough" and preview["reuses_cache"] is False
    blocked = fresh_rerun_preview(
        plan,
        calls_used=0,
        max_calls=40,
        provider="openai",
        model_label="gpt-4.1",
        model_data_locus="external",
        available=False,
        reason="AI review has not settled on this Snapshot yet",
    )
    assert blocked["available"] is False and blocked["reason"] == "AI review has not settled on this Snapshot yet"
    assert STATIC_CATALOG["ai.investigators"].model_calls("thorough") > 0


def test_assurance_quality_threshold_contract() -> None:
    """case: test_revert_rate_threshold_and_minimum_sample"""
    at = _outcomes(1, deployed=True, reverted=True) + _outcomes(4, deployed=True)
    assert _metrics(at)["revert_rate"].startswith("revert rate 1 of 5 deployed Changes rolled back (20%)")
    below = _outcomes(1, deployed=True, reverted=True) + _outcomes(5, deployed=True)
    assert "revert_rate" not in _metrics(below)
    small = _outcomes(1, deployed=True, reverted=True) + _outcomes(3, deployed=True)
    assert "revert_rate" not in _metrics(small)
    undeployed = _outcomes(1, deployed=True, reverted=True) + _outcomes(4)
    assert "revert_rate" not in _metrics(undeployed)
    "case: test_escaped_defects_threshold"
    assert "escaped_defects" not in _metrics(_outcomes(1, deployed=True, escaped=True))
    assert _metrics(_outcomes(2, deployed=True, escaped=True))["escaped_defects"].startswith(
        "escaped defects: 2 Changes failed or were rolled back in production"
    )
    "case: test_contradicted_claims_threshold_and_minimum_sample"
    at = [ChangeOutcome("a", claims_confirmed=7, claims_contradicted=3)]
    assert _metrics(at)["contradicted_claims"].startswith("contradicted claims: 3 of 10 verified claims (30%)")
    below = [ChangeOutcome("a", claims_confirmed=8, claims_contradicted=3)]
    assert "contradicted_claims" not in _metrics(below)
    small = [ChangeOutcome("a", claims_contradicted=4)]
    assert "contradicted_claims" not in _metrics(small)
    "case: test_confirmed_findings_threshold_and_minimum_sample"
    at = _outcomes(5, confirmed_findings=1)
    assert _metrics(at)["confirmed_findings"].startswith(
        "confirmed findings: 5 reviewer-confirmed AI findings across 5 Changes (1.0 per Change)"
    )
    below = _outcomes(4, confirmed_findings=1) + _outcomes(1)
    assert "confirmed_findings" not in _metrics(below)
    small = _outcomes(4, confirmed_findings=9)
    assert "confirmed_findings" not in _metrics(small)
    assert _metrics([]) == {}


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _fake_cli(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    monkeypatch.setattr(intelligence_drivers.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(command: Any, *, cwd: Any, env: Any = None, capture_output: Any, text: Any, check: Any, timeout: Any):
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(intelligence_drivers.subprocess, "run", fake_run)


def test_cli_reported_model_is_observed_and_the_default_stays_unreported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reported = json.dumps(
        {"type": "result", "structured_output": {"ok": True}, "modelUsage": {"claude-sonnet-4-5-20250929": {}}}
    )
    _fake_cli(monkeypatch, reported)
    execution = execute_structured(
        system="s",
        prompt="p",
        output_schema=_SCHEMA,
        driver="claude",
        model=None,
        project_root=tmp_path,
        origin="machine",
    )
    assert execution.result == {"ok": True}
    assert execution.model == ""
    assert execution.observed_models == ("claude-sonnet-4-5-20250929",)
    _fake_cli(monkeypatch, json.dumps({"type": "result", "structured_output": {"ok": True}}))
    silent = execute_structured(
        system="s",
        prompt="p",
        output_schema=_SCHEMA,
        driver="claude",
        model=None,
        project_root=tmp_path,
        origin="machine",
    )
    assert (silent.model, silent.observed_models) == ("", ())


def test_streaming_cli_reports_each_observed_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intelligence_drivers.shutil, "which", lambda name: f"/usr/bin/{name}")
    events = [
        {"msg": {"type": "session_configured", "model": "gpt-5-codex"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"ok": True})}},
    ]

    def runner(command: Any, *, cwd: Any, env: Any, on_stdout: Any, timeout: Any) -> tuple[int, str, str]:
        for event in events:
            on_stdout(json.dumps(event))
        return (0, "\n".join(json.dumps(event) for event in events), "")

    seen: list[str] = []
    result = intelligence_drivers._execute_cli_structured(
        driver="codex",
        model=None,
        system="s",
        prompt="p",
        schema=_SCHEMA,
        project_root=tmp_path,
        on_raw_delta=lambda _fragment: None,
        process_runner=runner,
        on_model_reported=seen.append,
    )
    assert result == {"ok": True} and seen == ["gpt-5-codex"]


def test_openai_compatible_response_model_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.infra.internal_llm import openai_client

    def create(**_kwargs: Any) -> Any:
        message = SimpleNamespace(content=json.dumps({"ok": True}))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], model="gpt-4.1-2025-04-14", id="r1")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai_client, "_resolve_client", lambda: client)
    result = openai_client.chat_with_result([{"role": "user", "content": "x"}], model="gpt-4.1", json_schema=_SCHEMA)
    assert (result.model, result.reported_model) == ("gpt-4.1", "gpt-4.1-2025-04-14")

    def unreported(**_kwargs: Any) -> Any:
        message = SimpleNamespace(content=json.dumps({"ok": True}))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], id="r2")

    client.chat.completions.create = unreported
    silent = openai_client.chat_with_result([{"role": "user", "content": "x"}], model="gpt-4.1", json_schema=_SCHEMA)
    assert (silent.model, silent.reported_model) == ("gpt-4.1", "")


def _runtime_store(tmp_path: Path) -> tuple[ReviewStore, ReviewSession, ReviewRevision]:
    store = ReviewStore(tmp_path / "runtime")
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root="/not/durable",
            range_mode="working_tree",
            title="assurance",
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree-a",
            source_fingerprint="source-a",
            packet_schema_version=1,
        ),
        [],
    )
    return (store, session, revision)


def _request(
    session: ReviewSession,
    revision: ReviewRevision,
    *,
    driver: str = "openai",
    model: str | None = "gpt-4.1",
    fresh: str = "",
    review_calls: int = 20,
) -> IntelligenceRunRequest:
    return IntelligenceRunRequest(
        review_id=session.id,
        revision_id=revision.id,
        snapshot_identity="source-a",
        capability="finding_investigation",
        scope={"path": "src/a.py"},
        context={"q": 1},
        policy={"read_only": True},
        driver=driver,
        model=model,
        output_schema=_SCHEMA,
        prompt_version=1,
        schema_version=1,
        budget=ModelCallBudget(max_calls=1, max_review_calls=review_calls),
        fresh_action_id=fresh,
    )


def _executor(
    calls: list[int], *, driver: str = "openai", model: str = "gpt-4.1", observed: tuple[str, ...] = ()
) -> Any:

    def run() -> StructuredExecution:
        calls.append(1)
        return StructuredExecution(
            result={"ok": True}, driver=driver, model=model, usage=ModelCallUsage(calls=1), observed_models=observed
        )

    return run


def test_observed_identity_is_persisted_apart_from_the_requested_one(tmp_path: Path) -> None:
    store, session, revision = _runtime_store(tmp_path)
    calls: list[int] = []
    outcome = run_intelligence(
        store=store, request=_request(session, revision), executor=_executor(calls, observed=("gpt-4.1-2025-04-14",))
    )
    assert outcome.status == "ready" and calls == [1]
    [row] = store.list_intelligence_runs(session.id)
    assert row.model == "gpt-4.1"
    persisted = json.loads(row.usage_json)["identity"]
    assert persisted["requested_model"] == "gpt-4.1"
    assert persisted["observed_models"] == ["gpt-4.1-2025-04-14"] and persisted["source"] == "observed"
    assert run_identity(row) == ModelIdentity("openai", "gpt-4.1", ("gpt-4.1-2025-04-14",))
    assert outcome.to_wire()["identity"] == run_identity(row).to_wire()
    reused = run_intelligence(store=store, request=_request(session, revision), executor=_executor(calls))
    assert reused.cache_hit and reused.identity == run_identity(row) and (calls == [1])


def test_a_cli_default_run_is_labeled_unreported_never_guessed(tmp_path: Path) -> None:
    store, session, revision = _runtime_store(tmp_path)
    calls: list[int] = []
    outcome = run_intelligence(
        store=store,
        request=_request(session, revision, driver="claude", model=None),
        executor=_executor(calls, driver="claude", model=""),
    )
    assert outcome.status == "ready"
    identity = run_identity(store.list_intelligence_runs(session.id)[0])
    assert identity.requested_model == "" and identity.observed_models == ()
    assert (identity.source, identity.label, identity.provable) == (
        "unreported_default",
        UNREPORTED_DEFAULT_LABEL,
        False,
    )


def test_observed_identity_past_the_bound_is_marked_and_cannot_prove(tmp_path: Path) -> None:
    store, session, revision = _runtime_store(tmp_path)
    many = tuple(f"gpt-4.1-{index}" for index in range(MAX_OBSERVED_MODELS + 1))
    run_intelligence(store=store, request=_request(session, revision), executor=_executor([], observed=many))
    identity = run_identity(store.list_intelligence_runs(session.id)[0])
    assert identity.observed_truncated and len(identity.observed_models) == MAX_OBSERVED_MODELS
    assert not identity.provable


def test_a_legacy_row_reads_as_its_requested_identity_never_observed(tmp_path: Path) -> None:
    store, session, revision = _runtime_store(tmp_path)
    for driver, model, source in (("openai", "gpt-4.1", "pinned"), ("codex", "", "unreported_default")):
        row, claimed = store.claim_intelligence_run(
            ReviewIntelligenceRun(
                review_id=session.id,
                id="",
                revision_id=revision.id,
                snapshot_identity="source-a",
                capability="finding_investigation",
                run_key=f"legacy-{driver}",
                scope_digest="s",
                context_digest="c",
                policy_digest="p",
                schema_digest="x",
                driver=driver,
                model=model,
                prompt_version=1,
                schema_version=1,
                status="pending",
            )
        )
        assert claimed
        store.complete_intelligence_run(session.id, row.id, status="ready", usage_json='{"calls":1}', model_calls=1)
        settled = store.get_intelligence_run(session.id, f"legacy-{driver}")
        assert settled is not None
        assert run_identity(settled).source == source
        assert run_execution(settled) == {"mode": "standard", "action_id": ""}


def test_fresh_rerun_executes_again_on_the_unchanged_snapshot_and_ordinary_requests_reuse(tmp_path: Path) -> None:
    store, session, revision = _runtime_store(tmp_path)
    calls: list[int] = []
    first = run_intelligence(store=store, request=_request(session, revision), executor=_executor(calls))
    again = run_intelligence(store=store, request=_request(session, revision), executor=_executor(calls))
    assert (first.cache_hit, again.cache_hit, again.run_id, len(calls)) == (False, True, first.run_id, 1)
    assert store.intelligence_calls(session.id) == 1
    fresh = run_intelligence(store=store, request=_request(session, revision, fresh="act-1"), executor=_executor(calls))
    assert fresh.status == "ready" and (not fresh.cache_hit) and (fresh.run_id != first.run_id)
    assert fresh.execution_mode == "fresh_rerun" and fresh.provider_calls_this_request == 1
    assert len(calls) == 2 and store.intelligence_calls(session.id) == 2
    rows = store.list_intelligence_runs(session.id)
    assert len(rows) == 2 and {row.run_key for row in rows} == {first_row_key(store, session.id, first.run_id)}
    assert {row.revision_id for row in rows} == {revision.id}
    replay = run_intelligence(
        store=store, request=_request(session, revision, fresh="act-1"), executor=_executor(calls)
    )
    assert (replay.cache_hit, replay.run_id, len(calls)) == (True, fresh.run_id, 2)
    reload = run_intelligence(store=store, request=_request(session, revision), executor=_executor(calls))
    assert (reload.cache_hit, reload.run_id, reload.execution_mode, len(calls)) == (
        True,
        fresh.run_id,
        "fresh_rerun",
        2,
    )
    second = run_intelligence(
        store=store, request=_request(session, revision, fresh="act-2"), executor=_executor(calls)
    )
    assert second.run_id not in {first.run_id, fresh.run_id} and len(calls) == 3
    summary = execution_summary(store.list_intelligence_runs(session.id))
    assert summary == {
        "latest_mode": "fresh_rerun",
        "fresh_reruns": 2,
        "fresh_rerun_runs": 2,
        "latest_fresh_at": summary["latest_fresh_at"],
        "runs": 3,
    }
    assert execution_summary(rows[-1:])["latest_mode"] == "executed"


def first_row_key(store: ReviewStore, review_id: str, run_id: str) -> str:
    return next(row.run_key for row in store.list_intelligence_runs(review_id) if row.id == run_id)


def test_fresh_rerun_spends_the_normal_review_budget_and_a_failure_keeps_the_ready_result(tmp_path: Path) -> None:
    store, session, revision = _runtime_store(tmp_path)
    calls: list[int] = []
    first = run_intelligence(
        store=store, request=_request(session, revision, review_calls=1), executor=_executor(calls)
    )
    refused = run_intelligence(
        store=store, request=_request(session, revision, fresh="act-1", review_calls=1), executor=_executor(calls)
    )
    assert refused.status == "failed" and "Review-wide model-call budget exhausted" in refused.error
    assert len(calls) == 1 and store.intelligence_calls(session.id) == 1
    reused = run_intelligence(
        store=store, request=_request(session, revision, review_calls=1), executor=_executor(calls)
    )
    assert (reused.cache_hit, reused.run_id, reused.status, len(calls)) == (True, first.run_id, "ready", 1)


def test_a_fresh_claim_still_converges_on_a_live_identical_run(tmp_path: Path) -> None:
    store, session, revision = _runtime_store(tmp_path)
    pending = ReviewIntelligenceRun(
        review_id=session.id,
        id="",
        revision_id=revision.id,
        snapshot_identity="source-a",
        capability="finding_investigation",
        run_key="live",
        scope_digest="s",
        context_digest="c",
        policy_digest="p",
        schema_digest="x",
        driver="openai",
        model="gpt-4.1",
        prompt_version=1,
        schema_version=1,
        status="pending",
    )
    live, claimed = store.claim_intelligence_run(pending)
    assert claimed
    joined, claimed_again = store.claim_intelligence_run(pending, bypass_cache=True)
    assert (joined.id, claimed_again) == (live.id, False)
    replaced, claimed_stale = store.claim_intelligence_run(pending, stale_after_s=0.0, bypass_cache=True)
    assert claimed_stale and replaced.id != live.id


def _rev(**fields: Any) -> SimpleNamespace:
    base = {"provenance_host": "claude", "provenance_model": "claude-sonnet-4-5", "provenance_certainty": "exact"}
    return SimpleNamespace(**{**base, **fields})


PINNED_OPENAI = ModelIdentity("openai", "gpt-4.1")
OBSERVED_OPENAI = ModelIdentity("codex", "", ("gpt-5-codex",))
_WEAK_REVIEWERS = [
    (ModelIdentity("claude", "claude-opus-4-1"), "not_independent"),
    (ModelIdentity("worker", unverified_model="gpt-4.1"), "unverified_reviewer"),
    (ModelIdentity("codex"), "unreported_reviewer"),
    (ModelIdentity("openai", "review-model"), "unknown_reviewer"),
]


@pytest.mark.parametrize("same_first", [True, False], ids=["same-family-first", "distinct-first"])
def test_several_ready_runs_on_one_snapshot_are_all_judged(same_first: bool) -> None:
    same = SimpleNamespace(status="ready", driver="claude", model="claude-opus-4-1", usage_json="{}")
    distinct = SimpleNamespace(status="ready", driver="openai", model="gpt-4.1", usage_json="{}")
    rows = [same, distinct] if same_first else [distinct, same]
    assessment = assess_revision(SimpleNamespace(actor_type="agent"), _rev(), rows)
    assert len(assessment.reviewers) == 2
    assert (assessment.status, requirement_state("require", assessment)) == ("not_independent", "unsatisfied")
    alone = assess_revision(SimpleNamespace(actor_type="agent"), _rev(), [distinct])
    assert (alone.status, requirement_state("require", alone)) == ("independent", "satisfied")


def _readiness(facts: dict[str, object]) -> list[Any]:
    registry = ConditionRegistry(
        (RegisteredCondition("independent_ai_review", 108, readiness_module._independent_ai_review),)
    )
    inputs = ReadinessInputs(
        review_id="review-1",
        snapshot_id="rr-1",
        snapshot_number=1,
        evaluated_at="2026-10-01T12:00:00Z",
        policy=policy_generation([built_in_policy_layer()]),
        facts=facts,
    )
    return [row for row in registry.evaluate(inputs).conditions if row.id != "policy-generation"]


def _facts(mode: Any, status: Any) -> IndependenceFacts:
    assessment = assess_independence(
        "human" if status == "not_applicable" else "agent",
        None if status == "not_applicable" else AGENT,
        (PINNED_OPENAI,) if status == "independent" else (ModelIdentity("claude"),),
    )
    return IndependenceFacts(mode, requirement_state(mode, assessment), assessment.status, assessment.reason, "org")


def _respond(json_schema: Any) -> dict[str, Any]:
    properties = (json_schema or {}).get("properties", {})
    if "hypotheses" in properties:
        return {"hypotheses": []}
    if "decisions" in properties:
        return {"decisions": []}
    return {
        "status": "not_confirmed",
        "category": "security",
        "target_ids": [],
        "title": "",
        "explanation": "",
        "consequence": "",
        "evidence_refs": [],
        "rule_refs": [],
    }


def _enable_openai(monkeypatch: pytest.MonkeyPatch, *, requested: str, reported: str) -> list[str]:
    from lemoncrow.infra.internal_llm import openai_client
    from lemoncrow.infra.internal_llm.result import InternalLLMChatResult

    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", requested)
    calls: list[str] = []

    def structured(messages: Any, *, model: str | None = None, json_schema: Any = None, **_kwargs: Any) -> Any:
        calls.append(str(model))
        parsed = _respond(json_schema)
        return InternalLLMChatResult(
            content=json.dumps(parsed),
            parsed_json=parsed,
            model=model or requested,
            reported_model=reported,
            request_id=f"req-{len(calls)}",
            input_tokens=7,
            output_tokens=3,
        )

    monkeypatch.setattr(openai_client, "chat_with_result", structured)
    return calls


def _set_author(
    store: ReviewStore,
    review_id: str,
    revision_id: str,
    *,
    actor: str = "agent",
    host: str = "claude",
    model: str = "claude-sonnet-4-5",
    certainty: str = "exact",
) -> None:
    store.update_session(review_id, actor_type=actor)
    with store._transaction() as conn:
        conn.execute(
            "UPDATE review_revisions SET provenance_host = ?, provenance_model = ?, provenance_certainty = ? WHERE id = ?",
            (host, model, certainty, revision_id),
        )


def _condition(review: SimpleNamespace) -> dict[str, Any] | None:
    body = _get(review, "readiness").json()
    return next((row for row in body["conditions"] if row["id"] == CONDITION_ID), None)


_REQUIRE = "review:\n  effort: thorough\n  independent_ai_reviewer: require\n"


def test_require_is_pending_before_review_and_satisfied_by_an_observed_distinct_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _enable_openai(monkeypatch, requested="gpt-4.1", reported="gpt-4.1-2025-04-14")
    review = _review(tmp_path, base_config=_REQUIRE)
    _set_author(review.store, review.review_id, review.revision_id)
    before = _get(review, "projection").json()["ai_review"]["assurance"]
    assert before["identity"] is None
    assert before["independence"]["status"] == "not_run"
    assert before["independence"]["requirement"] == "unsatisfied"
    assert before["independence"]["policy"]["mode"] == "require"
    assert before["independence"]["policy"]["origin"]["layer"] == "trusted_base_repository"
    pending = _condition(review)
    assert pending is not None and (pending["state"], pending["reason"]) == ("pending", "not_run")
    ran = _post(review, "ai-review", {"trigger": "manual"})
    assert ran.status_code == 200, ran.text
    assert calls, "the fixture must exercise the provider"
    wire = ran.json()["ai_review"]
    assert wire["execution"] == {"mode": "standard"}
    assert {row["source"] for row in wire["reviewer"]["identities"]} == {"observed"}
    assurance = _get(review, "projection").json()["ai_review"]["assurance"]
    identity = assurance["identity"]
    assert identity["requested_model"] == "gpt-4.1"
    assert identity["observed_models"] == ["gpt-4.1-2025-04-14"]
    assert (identity["source"], identity["label"]) == ("observed", "gpt-4.1-2025-04-14")
    independence = assurance["independence"]
    assert (independence["status"], independence["requirement"]) == ("independent", "satisfied")
    assert independence["author"]["label"] == "claude/claude-sonnet-4-5"
    assert independence["reviewers"][0]["families"] == ["openai"]
    assert assurance["execution"]["latest_mode"] == "executed"
    satisfied = _condition(review)
    assert satisfied is not None and satisfied["state"] == "pass"


def test_require_without_a_distinct_route_stays_visibly_unsatisfied_and_prefer_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_openai(monkeypatch, requested="claude-sonnet-4-5", reported="claude-sonnet-4-5-20250929")
    review = _review(tmp_path / "require", base_config=_REQUIRE)
    _set_author(review.store, review.review_id, review.revision_id)
    assert _post(review, "ai-review", {"trigger": "manual"}).status_code == 200
    independence = _get(review, "projection").json()["ai_review"]["assurance"]["independence"]
    assert (independence["status"], independence["requirement"]) == ("not_independent", "unsatisfied")
    condition = _condition(review)
    assert condition is not None and (condition["state"], condition["reason"]) == ("pending", "not_independent")
    prefer = _review(
        tmp_path / "prefer", base_config="review:\n  effort: thorough\n  independent_ai_reviewer: prefer\n"
    )
    _set_author(prefer.store, prefer.review_id, prefer.revision_id)
    assert _post(prefer, "ai-review", {"trigger": "manual"}).status_code == 200
    labeled = _get(prefer, "projection").json()["ai_review"]["assurance"]["independence"]
    assert (labeled["independent"], labeled["requirement"]) == (False, "preferred_unmet")
    assert _condition(prefer) is None


def test_require_with_an_unknown_author_is_never_satisfied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_openai(monkeypatch, requested="gpt-4.1", reported="gpt-4.1")
    review = _review(tmp_path, base_config=_REQUIRE)
    _set_author(review.store, review.review_id, review.revision_id, certainty="probable")
    assert _post(review, "ai-review", {"trigger": "manual"}).status_code == 200
    independence = _get(review, "projection").json()["ai_review"]["assurance"]["independence"]
    assert (independence["status"], independence["requirement"]) == ("unknown_author", "unsatisfied")
    condition = _condition(review)
    assert condition is not None and condition["state"] == "pending"


@pytest.mark.parametrize("distinct_first", [False, True], ids=["authors-family-first", "distinct-first"])
def test_require_stays_unsatisfied_when_any_reviewer_run_on_the_snapshot_shares_the_authors_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, distinct_first: bool
) -> None:
    same = ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929")
    distinct = ("gpt-4.1", "gpt-4.1-2025-04-14")
    review = _review(tmp_path, base_config=_REQUIRE)
    _set_author(review.store, review.review_id, review.revision_id)
    for index, (requested, reported) in enumerate((distinct, same) if distinct_first else (same, distinct)):
        calls = _enable_openai(monkeypatch, requested=requested, reported=reported)
        ran = _post(review, "ai-review/fresh", {"action_id": f"several-{index}"})
        assert ran.status_code == 200, ran.text
        assert calls, "each reviewer must actually run on the Snapshot"
    ready = [row for row in review.store.list_intelligence_runs(review.review_id) if row.status == "ready"]
    assert {model for row in ready for model in run_identity(row).observed_models} == {same[1], distinct[1]}
    independence = _get(review, "projection").json()["ai_review"]["assurance"]["independence"]
    assert (independence["status"], independence["independent"], independence["requirement"]) == (
        "not_independent",
        False,
        "unsatisfied",
    )
    assert sorted(family for row in independence["reviewers"] for family in row["families"]) == [
        "anthropic",
        "openai",
    ]
    condition = _condition(review)
    assert condition is not None and (condition["state"], condition["reason"]) == ("pending", "not_independent")


def test_fresh_rerun_endpoint_executes_again_and_ordinary_requests_and_reloads_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _enable_openai(monkeypatch, requested="gpt-4.1", reported="gpt-4.1-2025-04-14")
    review = _review(tmp_path, base_config="review:\n  effort: thorough\n")
    first = _post(review, "ai-review", {"trigger": "manual"})
    assert first.status_code == 200, first.text
    spent = len(calls)
    assert spent > 0
    rows_before = len(review.store.list_intelligence_runs(review.review_id))
    preview = _get(review, "projection").json()["ai_review"]["assurance"]["fresh_rerun"]
    assert preview["available"] is True and preview["reuses_cache"] is False
    assert preview["estimated_model_calls"] > 0 and preview["cost_class"]
    assert preview["execution_locus"] == "local" and preview["metering_owner"] == "customer_provider"
    assert preview["budget"]["calls_used"] == spent and preview["budget"]["remaining"] >= 0
    assert preview["model"] == "gpt-4.1" and preview["provider"] == "openai"
    _get(review, "projection")
    again = _post(review, "ai-review", {"trigger": "manual"})
    assert again.status_code == 200 and len(calls) == spent
    assert len(review.store.list_intelligence_runs(review.review_id)) == rows_before
    missing = _post(review, "ai-review/fresh", {})
    assert missing.status_code == 422, missing.text
    malformed = _post(review, "ai-review/fresh", {"action_id": "has space"})
    assert malformed.status_code == 422
    stale = review.client.post(
        f"/api/reviews/{review.review_id}/ai-review/fresh",
        headers={"Authorization": f"Bearer {_token()}"},
        json={"expected_revision_id": "rr-other", "action_id": "act-1"},
    )
    assert stale.status_code in {409, 412}, stale.text
    assert len(calls) == spent
    fresh = _post(review, "ai-review/fresh", {"action_id": "act-1"})
    assert fresh.status_code == 200, fresh.text
    body = fresh.json()
    assert body["fresh_rerun"] == {"action_id": "act-1"}
    assert body["ai_review"]["execution"] == {"mode": "fresh_rerun"}
    assert len(calls) == 2 * spent
    rows = review.store.list_intelligence_runs(review.review_id)
    assert len(rows) == 2 * rows_before and {row.revision_id for row in rows} == {review.revision_id}
    assert review.store.intelligence_calls(review.review_id) == 2 * spent
    replay = _post(review, "ai-review/fresh", {"action_id": "act-1"})
    assert replay.status_code == 200 and len(calls) == 2 * spent
    after = _post(review, "ai-review", {"trigger": "manual"})
    assert after.status_code == 200 and len(calls) == 2 * spent
    execution = _get(review, "projection").json()["ai_review"]["assurance"]["execution"]
    assert execution["latest_mode"] == "fresh_rerun" and execution["fresh_reruns"] == 1
    assert execution["runs"] == 2 * rows_before and execution["latest_fresh_at"]
    assert len(calls) == 2 * spent


def _token() -> str:
    from tests.gateway.test_review_run_plan_api import TOKEN

    return TOKEN


def test_fresh_rerun_refuses_when_the_plan_runs_no_ai(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _enable_openai(monkeypatch, requested="gpt-4.1", reported="gpt-4.1")
    review = _review(tmp_path, base_config="review:\n  capabilities:\n    ai: off\n")
    refused = _post(review, "ai-review/fresh", {"action_id": "act-1"})
    assert refused.status_code == 409 and "not in the effective run plan" in refused.json()["detail"]
    assert calls == []
    preview = _get(review, "projection").json()["ai_review"]["assurance"]["fresh_rerun"]
    assert preview["available"] is False and preview["estimated_model_calls"] == 0


def _outcomes(count: int, **flags: Any) -> list[ChangeOutcome]:
    return [ChangeOutcome(review_id=f"r{index}", **flags) for index in range(count)]


def _metrics(changes: list[ChangeOutcome]) -> dict[str, str]:
    return dict(TrackRecord(AGENT, tuple(changes), examined=len(changes)).poor())


def _past_change(
    store: ReviewStore,
    *,
    actor: str = "agent",
    agents: tuple[tuple[str, str, str], ...] = (("claude", "claude-sonnet-4-5", "exact"),),
    deployment: str = "",
    environment: str = "production",
) -> ReviewSession:
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="pull_request",
            repo_root="/repo/deploy",
            range_mode="commit_range",
            source_ref="github:acme/widgets#7",
            actor_type=actor,
        )
    )
    for number, (host, model, certainty) in enumerate(agents):
        store.add_revision(
            ReviewRevision(
                id=new_revision_id(),
                review_id=session.id,
                revision_number=0,
                range_mode="commit_range",
                tree_fingerprint=f"tree-{number}",
                packet_schema_version=1,
                head_sha="f" * 40,
                provenance_host=host,
                provenance_model=model,
                provenance_certainty=certainty,
            ),
            [],
        )
    current = store.get_session(session.id)
    assert current is not None
    if deployment == "rolled_back":
        live, back = (
            _obs("2", environment=environment),
            _obs("3", commit=OLD_SHA, created=5.0, environment=environment),
        )
        relations: dict[str, Any] = {live.key: "exact", back.key: "not_contained"}
        apply_observations(store, current, MERGED, [live, back], relations, CONFIGURED, actor_id="p", now=_at(5.1))
    elif deployment in {"deployed", "failed"}:
        item = _obs("1", state="success" if deployment == "deployed" else "failure", environment=environment)
        apply_observations(store, current, MERGED, [item], {item.key: "exact"}, CONFIGURED, actor_id="p", now=_at(1.1))
    refreshed = store.get_session(session.id)
    assert refreshed is not None
    return refreshed


def _claims(store: ReviewStore, session: ReviewSession, verdicts: list[str]) -> None:
    revision = store.latest_revision(session.id)
    assert revision is not None
    payload = json.dumps(
        {"recipe": {"id": "pytest"}, "claims": [{"verdict": verdict} for verdict in verdicts]}, sort_keys=True
    ).encode("utf-8")
    evidence_id = new_evidence_id()
    rel, content_hash, size = store.write_evidence_artifact(session.id, revision.id, evidence_id, payload)
    store.add_evidence(
        ReviewEvidence(
            id=evidence_id,
            review_id=session.id,
            revision_id=revision.id,
            kind="verification",
            title="pytest base vs head",
            artifact_path=rel,
            content_hash=content_hash,
            mime_type=CHECK_DIFFERENTIAL_MEDIA_TYPE,
            bytes=size,
            source="check_runs",
            source_ref=f"check-differential:pytest:{content_hash}",
        )
    )


def _finding(store: ReviewStore, session: ReviewSession, disposition: str, *, parent: str = "", **extra: Any) -> Any:
    revision = store.latest_revision(session.id)
    assert revision is not None
    return store.add_annotation(
        Annotation(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            anchor=AnnotationAnchor(path="src/a.py", side="new", start_line=1, end_line=1),
            body="finding",
            title="finding",
            created_by="LemonCrow AI Review",
            created_by_actor="agent",
            source="ai_review",
            source_id=f"finding-{disposition}-{parent}",
            parent_id=parent,
            finding_disposition=disposition,
            **extra,
        )
    )


def test_change_outcome_reads_deployments_claims_and_confirmed_findings(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "outcomes")
    rolled = _past_change(store, deployment="rolled_back")
    _claims(store, rolled, ["confirmed", "contradicted", "contradicted", "not_reproduced"])
    root = _finding(store, rolled, "confirmed_by_reviewer")
    _finding(store, rolled, "fixed", finding_disposition_evidence=("ev-1",))
    _finding(store, rolled, "dismissed", finding_disposition_reason="incorrect")
    _finding(store, rolled, "open")
    _finding(store, rolled, "confirmed_by_reviewer", parent=root.id)
    outcome = change_outcome(store, rolled)
    assert outcome == ChangeOutcome(
        review_id=rolled.id,
        deployed=True,
        reverted=True,
        escaped=True,
        claims_confirmed=1,
        claims_contradicted=2,
        confirmed_findings=2,
    )
    staging = change_outcome(store, _past_change(store, deployment="rolled_back", environment="staging"))
    assert (staging.deployed, staging.reverted, staging.escaped) == (True, True, False)
    failed = change_outcome(store, _past_change(store, deployment="failed"))
    assert (failed.deployed, failed.reverted, failed.escaped) == (True, False, True)
    healthy = change_outcome(store, _past_change(store, deployment="deployed"))
    assert (healthy.deployed, healthy.reverted, healthy.escaped) == (True, False, False)
    assert change_outcome(store, _past_change(store)) == ChangeOutcome(review_id=store.list_sessions(status="")[0].id)


def test_only_agent_only_changes_with_exact_same_agent_provenance_are_attributed(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "attribution")
    assert change_agent(store, _past_change(store)) == AGENT
    two = (("claude", "claude-sonnet-4-5", "exact"), ("claude", "claude-sonnet-4-5", "exact"))
    assert change_agent(store, _past_change(store, agents=two)) == AGENT
    for actor in ("human", "mixed", "unknown"):
        assert change_agent(store, _past_change(store, actor=actor)) is None
    probable = (("claude", "claude-sonnet-4-5", "exact"), ("claude", "claude-sonnet-4-5", "probable"))
    assert change_agent(store, _past_change(store, agents=probable)) is None
    mixed_agents = (("claude", "claude-sonnet-4-5", "exact"), ("codex", "gpt-5-codex", "exact"))
    assert change_agent(store, _past_change(store, agents=mixed_agents)) is None
    assert scored_author(SimpleNamespace(actor_type="agent"), _rev()) == AGENT
    for actor in ("human", "mixed", "unknown"):
        assert scored_author(SimpleNamespace(actor_type=actor), _rev()) is None
    assert scored_author(SimpleNamespace(actor_type="agent"), _rev(provenance_certainty="probable")) is None


def _age(store: ReviewStore, review_id: str, days: float) -> None:
    stamp = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with store._transaction() as conn:
        conn.execute("UPDATE review_sessions SET updated_at = ? WHERE id = ?", (stamp, review_id))


def test_the_window_excludes_old_reviews_and_the_current_one(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "window")
    recent = [_past_change(store, deployment="failed") for _ in range(2)]
    old = _past_change(store, deployment="failed")
    for index, session in enumerate(recent):
        _age(store, session.id, 1 + index)
    _age(store, old.id, track_record_module.WINDOW_DAYS + 1)
    current = _past_change(store)
    record = track_record(store, AGENT, exclude_review_id=current.id)
    assert {row.review_id for row in record.changes} == {session.id for session in recent}
    assert record.examined == 2 and (not record.truncated)
    assert [metric for metric, _ in record.poor()] == ["escaped_defects"]
    _age(store, old.id, track_record_module.WINDOW_DAYS - 0.01)
    assert len(track_record(store, AGENT, exclude_review_id=current.id).changes) == 3
    assert track_record(store, AgentAuthor("codex", "gpt-5-codex"), exclude_review_id=current.id).changes == ()


def test_the_examined_bound_holds_at_and_past_its_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(track_record_module, "MAX_REVIEWS_EXAMINED", 3)
    monkeypatch.setattr(track_record_module, "_PAGE", 2)
    store = ReviewStore(tmp_path / "bound")
    sessions = [_past_change(store, deployment="failed") for _ in range(4)]
    for index, session in enumerate(sessions):
        _age(store, session.id, 1 + index)
    current = sessions[0]
    at = track_record(store, AGENT, exclude_review_id=current.id)
    assert (at.examined, at.truncated, len(at.changes)) == (3, False, 3)
    assert at.scope == f"Reviews of the last {track_record_module.WINDOW_DAYS} days"
    extra = _past_change(store, deployment="failed")
    _age(store, extra.id, 10)
    past = track_record(store, AGENT, exclude_review_id=current.id)
    assert (past.examined, past.truncated, len(past.changes)) == (3, True, 3)
    assert past.scope.startswith("the 3 most recent Reviews")
    assert "the 3 most recent Reviews" in dict(past.poor())["escaped_defects"]


def test_the_default_bound_is_the_documented_200_reviews_over_90_days() -> None:
    assert (track_record_module.MAX_REVIEWS_EXAMINED, track_record_module.WINDOW_DAYS) == (200, 90)


def test_a_truncated_record_is_unknown_even_when_no_metric_is_poor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(track_record_module, "MAX_REVIEWS_EXAMINED", 3)
    monkeypatch.setattr(track_record_module, "_PAGE", 2)
    # A plan freezes its escalation facts when first built, so each case gets its own Review.
    (tmp_path / "at").mkdir()
    (tmp_path / "past").mkdir()
    at_review = _review(tmp_path / "at")
    _set_author(at_review.store, at_review.review_id, at_review.revision_id)
    for _ in range(3):  # exactly at the bound: complete, nothing to report
        _past_change(at_review.store)
    session = at_review.store.get_session(at_review.review_id)
    revision = at_review.store.latest_revision(at_review.review_id)
    at = track_record_assessment(at_review.store, session, revision)
    assert at is not None and at.rows == () and at.unknown == ""
    at_plan = _get(at_review, "run-plan").json()["plan"]["escalation"]
    assert at_plan["state"] == "none" and "notes" not in at_plan

    review = _review(tmp_path / "past")
    _set_author(review.store, review.review_id, review.revision_id)
    for _ in range(4):  # past the bound: still not poor, but no longer known
        _past_change(review.store)
    session = review.store.get_session(review.review_id)
    revision = review.store.latest_revision(review.review_id)
    past = track_record_assessment(review.store, session, revision)
    assert past is not None and past.rows == ()
    assert past.unknown == (
        "author track record of claude/claude-sonnet-4-5 is unknown beyond the 3 most recent Reviews "
        f"of the last {track_record_module.WINDOW_DAYS} days"
    )
    assert track_record_facts(review.store, session, revision) == ()  # the escalation rows never carry it
    escalation = _get(review, "run-plan").json()["plan"]["escalation"]
    assert escalation["state"] == "none" and escalation["notes"] == [past.unknown]
    assert escalation["triggers"] == []  # unknown never escalates by itself


def test_a_human_author_never_gets_an_unknown_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(track_record_module, "MAX_REVIEWS_EXAMINED", 1)
    review = _review(tmp_path)
    _set_author(review.store, review.review_id, review.revision_id, actor="human", certainty="none")
    for _ in range(3):
        _past_change(review.store)
    session = review.store.get_session(review.review_id)
    revision = review.store.latest_revision(review.review_id)
    assert track_record_assessment(review.store, session, revision) is None
    assert "notes" not in _get(review, "run-plan").json()["plan"]["escalation"]


def _poor_history(store: ReviewStore) -> None:
    for _ in range(2):
        _past_change(store, deployment="rolled_back")


def test_a_poor_record_escalates_auto_naming_the_agent_and_metric(tmp_path: Path) -> None:
    review = _review(tmp_path)
    _poor_history(review.store)
    _set_author(review.store, review.review_id, review.revision_id)
    session = review.store.get_session(review.review_id)
    revision = review.store.latest_revision(review.review_id)
    [(agent, metric, detail)] = track_record_facts(review.store, session, revision)
    assert (agent, metric) == ("claude/claude-sonnet-4-5", "escaped_defects")
    assert detail.startswith("escaped defects: 2 Changes failed or were rolled back in production")
    plan = _get(review, "run-plan").json()["plan"]
    assert plan["escalation"]["state"] == "escalated", plan["escalation"]
    [trigger] = [row for row in plan["escalation"]["triggers"] if row["class"] == "author_track_record"]
    assert trigger["label"].startswith("author track record of claude/claude-sonnet-4-5: escaped defects: 2 Changes")
    assert trigger["origin"] == "built-in default"
    investigators = next(row for row in plan["entries"] if row["id"] == "ai.investigators")
    assert investigators["origin"] == "escalation" and investigators["depth"] == "thorough"
    assert "claude/claude-sonnet-4-5" in investigators["reason"] and "escaped defects" in investigators["reason"]


def test_track_record_escalation_stays_within_the_ceiling_and_can_be_disabled(tmp_path: Path) -> None:
    facts = EscalationFacts(
        author_track_record=(("claude/claude-sonnet-4-5", "escaped_defects", "escaped defects: 2"),)
    )

    def plan(org: dict[str, Any]) -> Any:
        return build_run_plan(
            PlanInputs(
                review_id="review-1",
                revision_id="revision-1",
                trigger="open",
                settings=resolve_run_settings([parse_run_settings(org, origin=ORG, allow_profiles=True)]),
                detected=DetectedCapabilities(facts=frozenset(DETECTION_FACTS)),
                changed_paths=("src/app.py",),
                escalation=facts,
            )
        )

    limited = plan({"escalation": {"max_model_calls": 0}})
    assert limited.escalation["state"] == "limited_by_budget"
    investigators = limited.entry("ai.investigators")
    assert investigators is not None and investigators.depth == "auto"
    assert investigators.reason.startswith("Limited by budget: escalation to thorough does not fit")
    assert "claude/claude-sonnet-4-5" in str(limited.escalation["triggers"])
    disabled = plan({"escalation": {"author_track_record": "off"}})
    assert disabled.escalation["state"] == "none"
    assert parse_run_settings({"escalation": {"author_track_record": "sometimes"}}, origin=ORG).errors


def test_a_human_author_yields_no_score_anywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for case_index, (actor, certainty) in enumerate(
        [("human", "none"), ("human", "exact"), ("mixed", "exact"), ("unknown", "exact"), ("agent", "probable")]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        with monkeypatch.context() as case_monkeypatch:
            "Adversarial: the same poor history is in the store; only an exact agent author is ever scored."
            _enable_openai(case_monkeypatch, requested="gpt-4.1", reported="gpt-4.1")
            review = _review(case_tmp_path)
            _poor_history(review.store)
            for _ in range(2):
                _past_change(review.store, actor="human", agents=(("", "", "none"),), deployment="rolled_back")
            _set_author(review.store, review.review_id, review.revision_id, actor=actor, certainty=certainty)
            session = review.store.get_session(review.review_id)
            revision = review.store.latest_revision(review.review_id)
            assert track_record_facts(review.store, session, revision) == ()
            plan = _get(review, "run-plan")
            projection = _get(review, "projection")
            readiness = _get(review, "readiness")
            assert _post(review, "ai-review", {"trigger": "manual"}).status_code == 200
            after = _get(review, "projection")
            for response in (plan, projection, readiness, after):
                assert response.status_code == 200, response.text
                text = response.text
                assert not any(needle in text for needle in _SCORE_TEXT), needle_in(text)
                assert '"author_track_record":[' not in text.replace(" ", "")
            triggers = plan.json()["plan"]["escalation"].get("triggers", [])
            assert all(row["class"] != "author_track_record" for row in triggers)
            with review.store._transaction() as conn:
                tables = [
                    row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                ]
                for table in tables:
                    for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                        assert "track record" not in json.dumps([str(value) for value in tuple(row)])


def needle_in(text: str) -> list[str]:
    return [needle for needle in _SCORE_TEXT if needle in text]
