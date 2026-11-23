"""Capability catalog + run planner (Phase 9R items 40/41)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review import capability_catalog as catalog
from lemoncrow.pro.capabilities.review.ai_review import _EFFORT_PROFILES
from lemoncrow.pro.capabilities.review.capability_catalog import (
    AI_INVESTIGATOR_CALLS,
    DETECTION_FACTS,
    FAMILIES,
    PRESET_SELECTIONS,
    PRESETS,
    STATIC_CATALOG,
    DetectedCapabilities,
    DynamicTarget,
    detect_capabilities,
    valid_entry_id,
)
from lemoncrow.pro.capabilities.review.run_plan import (
    PROPOSED_BUDGET_DEFAULTS,
    BudgetUsage,
    EffectiveRunSettings,
    ManualPick,
    PlanInputs,
    RunPlan,
    SettingOrigin,
    ai_stage_plan,
    amend_run_plan,
    build_run_plan,
    catalog_wire,
    dependents_of,
    depth_preview,
    load_run_plan,
    parse_review_config_layer,
    parse_run_settings,
    persist_run_plan,
    preview_picks,
    resolve_run_settings,
)
from lemoncrow.pro.capabilities.review.run_plan_reverification import BUCKETS, reverification_summary
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id
from lemoncrow.pro.capabilities.review.test_impact import RecipeSelection

ORG = SettingOrigin("organization", "organization policy")
REPO = SettingOrigin("trusted_base_repository", "base revision .lemoncrow/review.yaml")
SEED_IDS = {
    "checks.test",
    "checks.lint",
    "checks.format",
    "checks.typecheck",
    "checks.precommit",
    "behavior.test_diff",
    "behavior.api_diff",
    "visual.screenshot",
    "visual.interactive_preview",
    "visual.dom_a11y",
    "visual.matrix",
    "side_effects.static",
    "side_effects.runtime",
    "migrations.execute",
    "artifacts.regenerate",
    "artifacts.shipped",
    "infra.render",
    "compat.mixed_version",
    "behavior.flag_matrix",
    "data_path.diff",
    "ai.brief_synthesis",
    "ai.hypotheses",
    "ai.investigators",
    "ai.premerge_checks",
    "ai.rule_checks",
}


def _detected(facts: frozenset[str] | set[str] | None = None, **kwargs: Any) -> DetectedCapabilities:
    return DetectedCapabilities(facts=frozenset(DETECTION_FACTS if facts is None else facts), **kwargs)


def _settings(org: dict[str, Any] | None = None, repo: dict[str, Any] | None = None) -> EffectiveRunSettings:
    layers = []
    if org is not None:
        layers.append(parse_run_settings(org, origin=ORG, allow_profiles=True))
    if repo is not None:
        layers.append(parse_run_settings(repo, origin=REPO))
    return resolve_run_settings(layers)


def _inputs(
    settings: EffectiveRunSettings,
    detected: DetectedCapabilities | None = None,
    *,
    trigger: str = "open",
    changed: tuple[str, ...] = ("src/app.py",),
    **kwargs: Any,
) -> PlanInputs:
    return PlanInputs(
        review_id="review-1",
        revision_id="revision-1",
        trigger=trigger,
        settings=settings,
        detected=detected if detected is not None else _detected(),
        changed_paths=changed,
        **kwargs,
    )


def _plan(settings: EffectiveRunSettings, detected: DetectedCapabilities | None = None, **kwargs: Any) -> RunPlan:
    usage = kwargs.pop("usage", None)
    return build_run_plan(_inputs(settings, detected, **kwargs), usage=usage)


def _status(plan: RunPlan) -> dict[str, str]:
    return {row.id: row.status for row in plan.entries}


@pytest.fixture
def executors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate executors for every static entry (R4/R5 land the real ones)."""
    for entry_id, entry in list(STATIC_CATALOG.items()):
        if not entry.executor:
            monkeypatch.setitem(STATIC_CATALOG, entry_id, replace(entry, executor="simulated"))


def test_run_plan_catalog_contract() -> None:
    """case: test_catalog_has_seed_ids_families_costs_and_dynamic_ids"""
    assert set(STATIC_CATALOG) == SEED_IDS
    assert {entry.family for entry in STATIC_CATALOG.values()} <= set(FAMILIES)
    assert STATIC_CATALOG["behavior.test_diff"].dependencies == ("checks.test",)
    assert STATIC_CATALOG["behavior.test_diff"].dependency_sides == "base+head"
    for dynamic in ("checks.recipe:pytest", "browser:login", "surface:web", "browser:*"):
        assert valid_entry_id(dynamic)
    for invalid in ("checks.bogus", "browser:", "surface:bad id", "ai"):
        assert not valid_entry_id(invalid)
    "case: test_ai_investigator_calls_match_effort_profiles"
    for effort, profile in _EFFORT_PROFILES.items():
        assert AI_INVESTIGATOR_CALLS[effort] == profile.max_investigations + int(profile.semantic_dedupe)
    "case: test_entries_without_an_executor_never_run"
    plan = _plan(_settings(repo={"effort": "deep"}), ai_candidates=5, risk_signal=True)
    for row in plan.entries:
        if not row.available:
            assert row.status in {"not_available", "inapplicable"}, row
    assert _status(plan)["behavior.api_diff"] == "not_available"
    assert _status(plan)["behavior.test_diff"] == "run"
    assert _status(plan)["ai.investigators"] == "run"
    "case: test_catalog_module_exports_are_stable"
    assert set(catalog.__all__) >= {"STATIC_CATALOG", "detect_capabilities", "resolve_entry", "PRESET_SELECTIONS"}
    "case: test_check_catalog_id_maps_lemoncrow_stable_keys_onto_catalog_entries"
    from lemoncrow.pro.capabilities.review.run_plan import check_catalog_id

    detected = _detected(browser_scenarios=(DynamicTarget("login"),), surfaces=(DynamicTarget("checks"),))
    assert check_catalog_id("login", detected) == "browser:login"
    assert check_catalog_id("browser:login", detected) == "browser:login"
    assert check_catalog_id("surface:service:checks:tests", detected) == "surface:checks"
    assert check_catalog_id("surface:service:checks", detected) == "surface:checks"
    assert check_catalog_id("surface:checks", detected) == "surface:checks"
    assert check_catalog_id("checks.test", detected) == "checks.test"
    for unmapped in ("", "browser:*", "surface:service:other", "browser:missing", "security:sast"):
        assert check_catalog_id(unmapped, detected) is None


def test_run_plan_detection_contract() -> None:
    """case: test_detection_reads_trusted_base_facts_only"""
    files = {
        "package.json": '{"private": true, "scripts": {"test": "vitest"}, "dependencies": {"react": "18"}}',
        "pyproject.toml": "[tool.mypy]\nstrict = true\n",
    }
    detected = detect_capabilities([*files, "db/migrate/001.sql"], files.get)
    assert {"test_command", "typecheck", "web_app", "migrations"} <= detected.facts
    assert "infrastructure" not in detected.facts
    assert detect_capabilities(None).base_state == "provisional"
    # A static site the trusted review config declares is a web app wherever it lives.
    declared = {".lemoncrow/review.yaml": "render:\n  static_root: site\n"}
    assert "web_app" in detect_capabilities([*declared, "site/home.html"], declared.get).facts
    undeclared = {".lemoncrow/review.yaml": "# static_root: site\nrender: {}\n"}
    assert "web_app" not in detect_capabilities([*undeclared, "site/home.html"], undeclared.get).facts
    "case: test_identical_selections_yield_identical_plans"
    settings = _settings(org={"effort": "thorough"}, repo={"capabilities": {"visual": "off"}})
    first = _plan(settings, ai_candidates=1)
    second = _plan(_settings(org={"effort": "thorough"}, repo={"capabilities": {"visual": "off"}}), ai_candidates=1)
    assert first.to_wire() == second.to_wire()
    assert first.digest == second.digest
    "case: test_deselecting_a_dependency_reports_what_stops"
    assert dependents_of("checks.test", ["behavior.test_diff", "side_effects.runtime", "checks.lint"]) == (
        "behavior.test_diff",
        "side_effects.runtime",
    )
    assert dependents_of("visual.screenshot", ["visual.matrix"]) == ("visual.matrix",)
    "case: test_unknown_and_inapplicable_ids_are_rejected_or_shown_inapplicable"
    layer = parse_run_settings({"effort": "custom", "custom": ["checks.bogus", "browser:missing"]}, origin=REPO)
    assert any("unknown capability id 'checks.bogus'" in error for error in layer.errors)
    plan = _plan(resolve_run_settings([layer]), _detected(set()))
    assert any("checks.bogus" in error for error in plan.errors)
    assert any("browser:missing" in error for error in plan.errors)
    assert plan.entry("browser:missing") is None
    inapplicable = _plan(_settings(repo={"effort": "custom", "custom": ["checks.test"]}), _detected(set()))
    row = inapplicable.entry("checks.test")
    assert row is not None and row.status == "inapplicable" and ("test command" in row.setup_hint)
    bad = parse_run_settings(
        {"effort": "medium", "capabilities": {"gpu": "always", "visual": "sometimes"}, "surprise": 1}, origin=REPO
    )
    assert len(bad.errors) == 4
    "case: test_provisional_trusted_base_marks_detection_dependent_entries_inapplicable"
    plan = _plan(_settings(), detect_capabilities(None))
    row = plan.entry("checks.test")
    assert row is not None and row.status == "inapplicable" and ("not readable yet" in row.reason)
    assert plan.runs("ai.investigators")


def test_run_plan_profile_contract() -> None:
    """case: test_custom_entries_inherit_unset_fields_from_their_preset"""
    settings = _settings(
        repo={
            "effort": "custom",
            "custom_from": "auto",
            "custom": [
                "behavior.test_diff",
                {"id": "behavior.api_diff", "paths": ["api/**"]},
                {"id": "visual.screenshot", "when": "manual"},
            ],
        }
    )
    plan = _plan(settings)
    inherited = plan.entry("behavior.test_diff")
    assert inherited is not None and inherited.sides == "base+head" and inherited.escalate
    scoped = plan.entry("behavior.api_diff")
    assert scoped is not None and scoped.paths == ("api/**",)
    manual = plan.entry("visual.screenshot")
    assert manual is not None and manual.when == "manual"
    "case: test_custom_profile_family_off_and_manual_picks_cannot_drop_a_policy_floor"
    org = {
        "required": ["ai.investigators", "checks.lint"],
        "profiles": {"docs": {"from": "quick", "custom": ["side_effects.static"]}},
        "profile_rules": [{"profile": "docs", "paths": ["docs/**"]}],
    }
    custom = _settings(
        org=org, repo={"effort": "custom", "custom": ["side_effects.static"], "capabilities": {"ai": "off"}}
    )
    for plan in (
        _plan(custom),
        _plan(custom, changed=("docs/index.md",)),
        amend_run_plan(_plan(custom), _inputs(custom), usage=BudgetUsage(), manual=[ManualPick("side_effects.static")]),
        _plan(replace(custom, budget={"per_review_model_calls": 0})),
    ):
        floor = plan.entry("ai.investigators")
        assert floor is not None
        assert floor.status == "run"
        assert floor.required and floor.origin == "policy_floor"
        lint = plan.entry("checks.lint")
        assert lint is not None and lint.required and (lint.status == "run")
    assert _plan(custom, changed=("docs/index.md",)).selection["source"] == "profile"
    over = _plan(replace(custom, budget={"per_review_model_calls": 0}))
    assert any("policy-required or already-planned work alone exceeds the budget" in note for note in over.budget_notes)
    "case: test_path_min_effort_floor_raises_depth_for_sensitive_paths_only"
    settings = _settings(
        org={"min_effort": [{"effort": "thorough", "paths": ["src/payments/**"]}]}, repo={"effort": "quick"}
    )
    sensitive = _plan(settings, changed=("src/payments/charge.py",))
    investigators = sensitive.entry("ai.investigators")
    assert investigators is not None and investigators.depth == "thorough" and investigators.required
    hypotheses = sensitive.entry("ai.hypotheses")
    assert hypotheses is not None and hypotheses.status == "run" and (hypotheses.origin == "policy_floor")
    ordinary = _plan(settings, changed=("src/readme.py",))
    assert ordinary.entry("ai.hypotheses") is None
    assert ordinary.entry("ai.investigators").depth == "quick"
    "case: test_named_profiles_are_organization_only"
    layer = parse_review_config_layer(
        "review:\n  profiles:\n    x:\n      custom: [checks.lint]\n", source=".lemoncrow/review.yaml"
    )
    assert layer is not None
    assert "named profiles may only be defined by organization policy" in layer.errors
    "case: test_inherit_override_and_reset_to_preset"
    org_layer = parse_run_settings({"effort": "thorough", "run_on": "worker"}, origin=ORG)
    repo_layer = parse_run_settings({"effort": "custom", "custom": ["checks.lint"]}, origin=REPO)
    resolved = resolve_run_settings([org_layer, repo_layer])
    assert resolved.effort == "custom"
    assert resolved.custom_from == "thorough"
    assert resolved.origin("effort") == REPO
    assert resolved.origin("run_on") == ORG
    wire = resolved.to_wire()
    assert wire["effort"]["mode"] == "override"
    assert wire["budget"]["per_review_minutes"] == {
        "effective_value": None,
        "mode": "inherit",
        "origin": {"layer": "built_in_default", "source": "built_in_default"},
    }
    reset = resolve_run_settings([org_layer, repo_layer.reset_to_preset()])
    assert reset.effort == "thorough"
    assert reset.custom == ()
    assert reset.origin("effort") == ORG
    assert resolve_run_settings([]).effort == "auto"
    assert resolve_run_settings([]).origin("effort").layer == "built_in_default"
    roundtrip = parse_run_settings(repo_layer.to_document(), origin=REPO)
    assert roundtrip.custom == repo_layer.custom and roundtrip.effort == repo_layer.effort
    "case: test_legacy_review_ai_keys_map_to_ai_family_overrides"
    assert _settings(repo={"ai": {"mode": "off"}}).capabilities["ai"] == "off"
    assert _settings(repo={"ai": {"enabled": False}}).capabilities["ai"] == "off"
    legacy = _settings(repo={"ai": {"effort": "deep", "max_calls": 3, "max_cost_usd": 0.5}})
    assert legacy.ai_depth == "deep"
    assert legacy.budget == {"per_review_model_calls": 3, "per_review_model_cost_usd": 0.5}
    plan = _plan(legacy)
    assert plan.entry("ai.investigators").depth == "auto"
    assert plan.entry("ai.investigators").budget_reduced_from == "deep"
    off = _status(_plan(_settings(repo={"ai": {"mode": "off"}}), ai_candidates=3))
    assert off["ai.investigators"] == "skipped" and off["ai.hypotheses"] == "skipped"
    "case: test_plan_yaml_examples_parse_exactly"
    first = "\nreview:\n  effort: auto # quick | auto | thorough | deep\n  run_on: local # local | worker | managed\n  budget:\n    per_review_minutes: 15\n    per_review_model_calls: 6\n    monthly_managed_usd: 200\n  capabilities: # advanced, optional\n    visual: off\n"
    second = '\nreview:\n  effort: custom # quick | auto | thorough | deep | custom\n  run_on: worker\n  custom:\n    - checks.test\n    - checks.lint\n    - id: behavior.api_diff\n      paths: ["api/**"]\n    - id: visual.screenshot\n      when: manual\n'
    one = parse_review_config_layer(first, source="base")
    two = parse_review_config_layer(second, source="base")
    assert one is not None and one.errors == ()
    assert two is not None and two.errors == ()
    assert one.effort == "auto" and one.run_on == "local"
    assert dict(one.budget) == {"per_review_minutes": 15.0, "per_review_model_calls": 6, "monthly_managed_usd": 200.0}
    assert dict(one.capabilities) == {"visual": "off"}
    assert two.effort == "custom" and two.run_on == "worker"
    assert [row.id for row in two.custom or ()] == [
        "checks.test",
        "checks.lint",
        "behavior.api_diff",
        "visual.screenshot",
    ]
    assert yaml.safe_load(second)["review"]["custom"][2] == (two.custom or ())[2].to_document()
    assert parse_review_config_layer("other: 1\n", source="base") is None


def test_run_plan_execution_contract() -> None:
    """case: test_manual_pick_with_unknown_id_is_rejected_visibly"""
    settings = _settings()
    plan = build_run_plan(_inputs(settings), manual=[ManualPick("nope.x"), ManualPick("surface:ghost")])
    assert any("'nope.x'" in error for error in plan.errors)
    assert any("surface:ghost" in error for error in plan.errors)
    "case: test_run_on_falls_back_visibly_when_not_enabled"
    plan = _plan(_settings(repo={"run_on": "managed"}), allowed_run_on=("local",))
    assert plan.run_on == "local"
    assert "managed execution is not enabled here" in plan.run_on_note
    "case: test_no_default_ceiling_gates_anything"
    settings = resolve_run_settings([])
    assert dict(settings.budget) == {}
    plan = _plan(_settings(repo={"effort": "deep"}), ai_candidates=5, risk_signal=True)
    assert not plan.limited_by_budget
    assert dict(plan.ceilings) == {}
    assert PROPOSED_BUDGET_DEFAULTS == {
        "per_review_minutes": 15,
        "per_review_model_calls": 6,
        "monthly_managed_usd": 200,
    }
    "case: test_display_metadata_is_not_execution_identity"
    plan = _plan(_settings())
    rewired = plan.to_wire()
    for row in rewired["entries"]:
        row["reason"] = "edited for display"
        row["title"] = "renamed"
    assert RunPlan.from_wire(rewired).digest == plan.digest
    assert DetectedCapabilities.from_wire(plan.detected) == _detected()
    rewired["detected"] = {}
    assert RunPlan.from_wire(rewired).digest == plan.digest
    "case: test_run_specific_only_adds_and_previews_incremental_cost"
    settings = _settings(repo={"effort": "quick"})
    inputs = _inputs(settings)
    base = build_run_plan(inputs)
    preview = preview_picks(base, inputs, usage=BudgetUsage(), manual=[ManualPick("ai.hypotheses")])
    assert preview["incremental"] == {"minutes": 0.0, "model_calls": 1}
    assert [row["id"] for row in preview["changes"]] == ["ai.hypotheses"]
    amended = amend_run_plan(base, inputs, usage=BudgetUsage(), manual=[ManualPick("ai.hypotheses")])
    assert amended.parent_digest == base.digest
    for row in base.entries:
        if row.status == "run":
            assert amended.runs(row.id)
    added = amended.entry("ai.hypotheses")
    assert added is not None and added.status == "run" and (added.origin == "manual")
    deeper = amend_run_plan(amended, inputs, usage=BudgetUsage(), manual=[ManualPick("ai.investigators", "deep")])
    assert deeper.entry("ai.investigators").depth == "deep"
    assert amend_run_plan(deeper, inputs, usage=BudgetUsage(), manual=[ManualPick("ai.investigators")]) is deeper
    "case: test_manual_pick_cannot_reenable_a_family_that_is_off"
    settings = _settings(repo={"capabilities": {"ai": "off"}})
    plan = build_run_plan(_inputs(settings), manual=[ManualPick("ai.hypotheses")])
    assert plan.entry("ai.hypotheses").status == "skipped"
    "case: test_ai_stage_plan_mirrors_the_plan"
    auto = _plan(_settings(), ai_candidates=3)
    stage = ai_stage_plan(auto)
    assert (stage.effort, stage.investigators, stage.hypotheses) == ("auto", True, True)
    assert stage.plan_digest == auto.digest
    quiet = ai_stage_plan(_plan(_settings(), ai_candidates=1))
    assert (quiet.investigators, quiet.hypotheses) == (True, False)
    quick = ai_stage_plan(_plan(_settings(repo={"effort": "quick"}), ai_candidates=5))
    assert (quick.effort, quick.hypotheses) == ("quick", False)
    off = ai_stage_plan(_plan(_settings(repo={"capabilities": {"ai": "off"}})))
    assert not off.investigators and off.status == "skipped"
    capped = ai_stage_plan(_plan(_settings(repo={"budget": {"per_review_model_calls": 4}})))
    assert capped.max_review_calls == 4


def test_run_plan_capability_and_budget_contract() -> None:
    """case: test_capabilities_with_nothing_to_act_on_neither_run_nor_appear_in_settings"""
    detected = _detected({"test_command"})
    preview = depth_preview(_settings(), detected, monthly_reviews=10)
    shown = {row["id"] for depth in preview["depths"].values() for row in depth["entries"]}
    assert not shown & {"visual.screenshot", "visual.matrix", "migrations.execute", "infra.render", "checks.lint"}
    assert "checks.test" in shown
    assert (
        preview["depths"]["auto"]["monthly"]["model_calls"]
        == preview["depths"]["auto"]["per_review"]["model_calls"] * 10
    )
    listed = {row["id"] for row in catalog_wire(detected)}
    assert "visual.screenshot" not in listed and "checks.test" in listed
    plan = _plan(_settings(repo={"effort": "deep"}), detected)
    assert all(row.status != "run" for row in plan.entries if row.id.startswith("visual."))
    "case: test_amendment_keeps_frozen_signals_and_limits_a_pick_beyond_budget"
    settings = _settings(repo={"budget": {"per_review_model_calls": 4}})
    first = build_run_plan(_inputs(settings, ai_candidates=3))
    assert first.runs("ai.hypotheses")
    later = _inputs(settings, ai_candidates=0)
    assert later.digest() == _inputs(settings, ai_candidates=3).digest()
    amended = amend_run_plan(first, later, usage=BudgetUsage(), manual=[ManualPick("ai.investigators", "deep")])
    assert amended.runs("ai.hypotheses")
    row = amended.entry("ai.investigators")
    assert row is not None and row.status == "run"
    assert (row.depth, row.budget_reduced_from) == ("auto", "deep")
    assert row.reason.startswith("Limited by budget")
    "case: test_managed_money_cap_routes_required_work_to_an_allowed_non_managed_executor"
    plan = _plan(
        _managed_floor_settings(), usage=BudgetUsage(monthly_managed_usd=50.0), allowed_run_on=("managed", "local")
    )
    assert plan.run_on == "managed"
    investigators = plan.entry("ai.investigators")
    assert investigators is not None and investigators.required
    assert (investigators.status, investigators.executor) == ("run", "local")
    assert investigators.reason.startswith("routed to local: monthly managed spend cap reached")
    assert not [
        row.id for row in plan.entries if row.status == "run" and row.executor == "managed" and (not row.external)
    ]
    assert "monthly managed spend cap reached: managed work routed to local" in plan.budget_notes
    assert any(note.startswith("policy floors ignore the model calls ceilings") for note in plan.budget_notes)
    assert ai_stage_plan(plan).investigators
    assert not plan.required_blockers()
    "case: test_floors_ignore_family_off_with_an_explicit_plan_note"
    plan = _plan(_managed_floor_settings(capabilities={"ai": "off"}), usage=BudgetUsage(monthly_managed_usd=0.0))
    assert plan.runs("ai.investigators")
    assert any(
        "ai.investigators is policy-required and runs although the ai family is off" in note
        for note in plan.budget_notes
    )
    "case: test_only_lemoncrow_verification_requirements_become_check_floors"
    from lemoncrow.pro.capabilities.review.run_plan_service import required_check_floors

    def effective(rows: Any) -> dict[str, Any]:
        return {
            "requirements": {
                "settings": {"required_checks": {"effective_value": rows, "origin": {"layer": "repository"}}}
            }
        }

    floors = required_check_floors(
        effective(
            [
                {"stable_key": "login", "attestation_class": "lemoncrow_verification"},
                {"stable_key": "ci/build", "attestation_class": "provider_ci"},
                {"stable_key": "login", "attestation_class": "lemoncrow_verification", "branch": "main"},
            ]
        )
    )
    assert [(floor.kind, floor.value, floor.origin.layer) for floor in floors] == [("check", "login", "repository")]
    assert required_check_floors(None) == ()
    assert required_check_floors({"requirements": {"critical_rules": []}}) == ()
    for malformed in ("login", ["login"], [{"attestation_class": "lemoncrow_verification"}]):
        with pytest.raises(RuntimeError):
            required_check_floors(effective(malformed))
    "case: test_label_rule_profiles_are_shown_inactive_without_a_label_source"
    from lemoncrow.pro.capabilities.review.run_plan import LABEL_RULE_INACTIVE, LABEL_SOURCE_AVAILABLE

    assert LABEL_SOURCE_AVAILABLE is False
    settings = _settings(
        org={
            "profiles": {
                "docs": {"from": "quick", "custom": ["side_effects.static"]},
                "web": {"from": "thorough", "custom": ["side_effects.static"]},
            },
            "profile_rules": [
                {"profile": "docs", "labels": ["docs"]},
                {"profile": "web", "paths": ["web/**"], "labels": ["frontend"]},
            ],
        }
    )
    labelled = _plan(settings, labels=("docs", "frontend"))
    assert labelled.selection["source"] == "preset"
    assert labelled.notes == (
        f"profile docs: {LABEL_RULE_INACTIVE}",
        f"profile web: {LABEL_RULE_INACTIVE}; its path rule still applies",
    )
    assert RunPlan.from_wire(labelled.to_wire()).notes == labelled.notes
    assert _plan(settings, changed=("web/app.tsx",)).selection["name"] == "web"
    rules = settings.to_wire()["profile_rules"]
    assert rules[0] == {"profile": "docs", "labels": ["docs"], "label_rule": "inactive", "note": LABEL_RULE_INACTIVE}
    assert rules[1]["label_rule"] == "inactive" and rules[1]["paths"] == ["web/**"]
    "case: test_an_invalid_trusted_base_policy_runs_nothing_and_says_why"
    error = "trusted-base .lemoncrow/review.yaml is invalid: effort must be quick, auto, thorough, deep or custom"
    settings = _settings(org={"required": ["ai.investigators"]}, repo={"effort": "deep"})
    valid = _inputs(settings, ai_candidates=5, risk_signal=True)
    invalid = replace(valid, policy_error=error)
    assert invalid.digest() != valid.digest()
    plan = build_run_plan(invalid)
    assert plan.policy_error == error
    assert not [row.id for row in plan.entries if row.status in {"run", "on_request"}]
    investigators = plan.entry("ai.investigators")
    assert investigators is not None and investigators.reason == f"Review policy invalid: {error}"
    assert not ai_stage_plan(plan).investigators
    wire = plan.to_wire()
    assert (wire["policy_state"], wire["policy_error"]) == ("invalid", error)
    assert RunPlan.from_wire(wire).policy_error == error
    amended = amend_run_plan(plan, invalid, usage=BudgetUsage(), manual=[ManualPick("checks.lint")])
    assert not [row.id for row in amended.entries if row.status == "run"]
    assert build_run_plan(valid).to_wire()["policy_state"] == "valid"
    "case: test_effective_policy_withholds_behavior_only_for_an_invalid_trusted_base"
    from lemoncrow.pro.capabilities.review.run_plan_service import effective_policy_layers, policy_invalid_reason

    run_plan = {
        "layers": [
            {"origin": {"layer": "organization", "source": "operator"}, "settings": {"required": ["checks.test"]}}
        ],
        "allowed_run_on": ["managed"],
    }
    invalid = {
        "behavior": None,
        "trusted_base": {"policy_state": "invalid", "path": ".lemoncrow/review.yaml", "detail": "effort is bogus"},
        "run_plan": run_plan,
    }
    layers, execution = effective_policy_layers(invalid)
    assert [layer.origin.layer for layer in layers] == ["organization"]
    assert execution["allowed_run_on"] == ["managed"]
    assert policy_invalid_reason(invalid) == "trusted-base .lemoncrow/review.yaml is invalid: effort is bogus"
    provisional = {
        "behavior": {"effort": {"effective_value": "auto", "origin": {"layer": "built_in_default"}}},
        "trusted_base": {"policy_state": "provisional", "detail": "unreadable"},
    }
    assert policy_invalid_reason(provisional) == ""
    assert effective_policy_layers(provisional) == ([], {})
    with pytest.raises(RuntimeError, match="missing behavior"):
        effective_policy_layers({"behavior": None})
    "case: test_reviewer_surface_and_preview_amendments_take_the_runtime_locus"
    from lemoncrow.pro.capabilities.review.planned_execution import PlannedExecutionRefused, admit_planned_execution

    layers = [parse_run_settings({"effort": "balanced"}, origin=REPO)]
    inputs = _loci_inputs(resolve_run_settings(layers, default_run_on="managed"))
    picks = [
        ManualPick("surface:fixture:api/status", target=DynamicTarget(id="fixture:api/status", title="GET /status")),
        ManualPick("visual.screenshot", target=DynamicTarget(id="visual.screenshot", title="Web preview")),
        ManualPick("ai.hypotheses"),
    ]
    plan = amend_run_plan(build_run_plan(inputs), inputs, usage=BudgetUsage(), manual=picks)
    for entry_id in ("surface:fixture:api/status", "visual.screenshot"):
        entry = plan.entry(entry_id)
        assert entry is not None and (entry.status, entry.executor) == ("run", "worker"), entry
        assert admit_planned_execution(plan, entry_id, run_on="worker") == entry
        with pytest.raises(PlannedExecutionRefused, match="planned on the worker executor, not managed"):
            admit_planned_execution(plan, entry_id, run_on="managed")
    assert admit_planned_execution(plan, "ai.hypotheses", run_on="managed").executor == "managed"
    assert plan.run_on_note == ""


@pytest.mark.usefixtures("executors")
def test_presets_are_pure_selections_and_equal_custom_yields_identical_plan() -> None:
    for preset in PRESETS:
        detected = _detected(
            browser_scenarios=(DynamicTarget("login"),),
            surfaces=(DynamicTarget("web"),),
            recipes=(DynamicTarget("pytest"),),
        )
        preset_plan = _plan(_settings(repo={"effort": preset}), detected, ai_candidates=3, risk_signal=True)
        custom_plan = _plan(
            _settings(
                repo={
                    "effort": "custom",
                    "custom_from": preset,
                    "custom": [row.id for row in PRESET_SELECTIONS[preset]],
                }
            ),
            detected,
            ai_candidates=3,
            risk_signal=True,
        )
        assert preset_plan.digest == custom_plan.digest
        assert preset_plan.execution_document() == custom_plan.execution_document()
        assert preset_plan.selection["source"] == "preset"
        assert custom_plan.selection["source"] == "custom"
        assert {row.origin for row in custom_plan.entries} == {"custom"}


@pytest.mark.usefixtures("executors")
def test_custom_runs_nothing_outside_selection_dependencies_and_floors() -> None:
    settings = _settings(
        org={"required": ["ai.investigators"]},
        repo={"effort": "custom", "custom": ["checks.lint", "behavior.test_diff"]},
    )
    plan = _plan(settings, risk_signal=True)
    assert {row.id for row in plan.entries} == {"checks.lint", "behavior.test_diff", "checks.test", "ai.investigators"}
    pulled = plan.entry("checks.test")
    assert pulled is not None
    assert pulled.sides == "base+head"
    assert pulled.pulled_in_by == ("behavior.test_diff",)
    assert pulled.status == "run"
    floor = plan.entry("ai.investigators")
    assert floor is not None and floor.origin == "policy_floor" and floor.required


@pytest.mark.usefixtures("executors")
def test_when_paths_and_escalation_shape_status() -> None:
    settings = _settings(
        repo={
            "effort": "custom",
            "custom_from": "auto",
            "custom": [
                {"id": "checks.lint", "when": "open"},
                {"id": "checks.typecheck", "paths": ["api/**"]},
                {"id": "visual.screenshot", "when": "manual"},
                "behavior.test_diff",
                "ai.investigators",
                "ai.hypotheses",
            ],
        }
    )
    pushed = _status(_plan(settings, trigger="push", ai_candidates=1))
    assert pushed["checks.lint"] == "skipped"
    assert pushed["checks.typecheck"] == "skipped"
    assert pushed["visual.screenshot"] == "on_request"
    assert pushed["behavior.test_diff"] == "skipped"
    assert pushed["ai.hypotheses"] == "skipped"
    risky = _status(_plan(settings, ai_candidates=3, risk_signal=True, changed=("api/v1.py",)))
    assert risky["checks.lint"] == "run"
    assert risky["checks.typecheck"] == "run"
    assert risky["behavior.test_diff"] == "run"
    assert risky["ai.hypotheses"] == "run"


@pytest.mark.usefixtures("executors")
def test_family_overrides_off_and_always() -> None:
    off = _status(_plan(_settings(repo={"effort": "deep", "capabilities": {"visual": "off"}}), ai_candidates=3))
    assert {off[key] for key in ("visual.screenshot", "visual.dom_a11y", "visual.matrix")} == {"skipped"}
    always = _plan(_settings(repo={"effort": "quick", "capabilities": {"behavior_diff": "always"}}))
    forced = always.entry("behavior.test_diff")
    assert forced is not None and forced.status == "run" and (forced.origin == "family_override")


@pytest.mark.usefixtures("executors")
def test_budget_exhaustion_degrades_in_documented_order() -> None:
    detected = _detected()
    full = _plan(_settings(repo={"effort": "deep"}), detected, ai_candidates=3, risk_signal=True)
    running = [row for row in full.entries if row.status == "run"]
    tier_minutes: dict[str, float] = {}
    for row in running:
        tier_minutes[row.degrade_tier] = tier_minutes.get(row.degrade_tier, 0.0) + row.estimated_minutes
    total = full.totals["minutes"]
    assert tier_minutes["deep_extra"] > 0 and tier_minutes["thorough_behavior"] > 0

    def limited(budget: dict[str, float | int]) -> dict[str, set[str]]:
        plan = _plan(_settings(repo={"effort": "deep", "budget": budget}), detected, ai_candidates=3, risk_signal=True)
        out: dict[str, set[str]] = {}
        for row in plan.entries:
            if row.status == "limited_by_budget":
                assert row.reason.startswith("Limited by budget")
                out.setdefault(row.degrade_tier, set()).add(row.id)
        if out:
            assert plan.to_wire()["budget"]["limited"] is True
        return out

    deep_cut = limited({"per_review_minutes": total - tier_minutes["deep_extra"]})
    assert set(deep_cut) == {"deep_extra"}
    behavior_cut = limited({"per_review_minutes": total - tier_minutes["deep_extra"] - 1})
    assert set(behavior_cut) == {"deep_extra", "thorough_behavior"}
    assert "base" not in behavior_cut
    calls = full.totals["model_calls"]
    ai_cut = limited({"per_review_minutes": total, "per_review_model_calls": calls - 1})
    assert ai_cut == {"ai_escalation": {"ai.hypotheses"}}
    reduced = _plan(
        _settings(repo={"effort": "deep", "budget": {"per_review_model_calls": 3}}),
        detected,
        ai_candidates=3,
        risk_signal=True,
    ).entry("ai.investigators")
    assert reduced is not None and reduced.status == "run" and (reduced.depth == "auto")
    assert reduced.budget_reduced_from == "deep"
    nothing = limited({"per_review_model_calls": 0})
    assert nothing["base"] == {"ai.investigators"}


@pytest.mark.usefixtures("executors")
def test_monthly_ceilings_use_observed_usage_and_unmetered_months_are_noted() -> None:
    settings = _settings(repo={"effort": "auto", "budget": {"monthly_minutes": 10, "monthly_managed_usd": 5}})
    unmetered = _plan(settings)
    assert not unmetered.limited_by_budget
    assert any("monthly usage is not metered" in note for note in unmetered.budget_notes)
    spent = _plan(settings, usage=BudgetUsage(monthly_minutes=10))
    assert spent.limited_by_budget
    assert all(row.status != "run" or row.estimated_minutes == 0 for row in spent.entries)


def _store(tmp_path: Path) -> tuple[ReviewStore, ReviewRevision]:
    store = ReviewStore(tmp_path)
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root=str(tmp_path),
            range_mode="working_tree",
            title="plan",
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree",
            source_fingerprint="source",
            packet_schema_version=1,
            provenance_host="claude",
            provenance_model="sonnet",
        ),
        [],
    )
    return (store, revision)


def test_persisted_plan_round_trips_with_its_digest_and_tampering_fails_closed(tmp_path: Path) -> None:
    store, revision = _store(tmp_path)
    plan = build_run_plan(
        PlanInputs(
            review_id=revision.review_id,
            revision_id=revision.id,
            trigger="open",
            settings=_settings(repo={"effort": "thorough"}),
            detected=_detected(),
        )
    )
    persist_run_plan(store, plan)
    loaded = load_run_plan(store, revision.id)
    assert loaded is not None
    assert loaded.digest == plan.digest
    assert loaded.to_wire() == plan.to_wire()
    tampered = plan.to_wire()
    tampered["entries"][0]["status"] = "skipped" if tampered["entries"][0]["status"] == "run" else "run"
    with pytest.raises(ValueError, match="digest"):
        RunPlan.from_wire(tampered)
    assert load_run_plan(store, "missing") is None


def test_bounded_detection_is_coverage_unknown_at_and_past_the_bound_never_absence() -> None:
    """RC12-B item 5: past the scan bound a missing fact is unknown, and a required check blocks."""

    paths = [f"src/m{index}.py" for index in range(5)]
    beyond = [*paths, "tests/test_hidden.py"]
    at = detect_capabilities(paths, max_paths=5)
    past = detect_capabilities(beyond, max_paths=5)
    assert (at.truncated, past.truncated) == (False, True)
    assert "test_command" not in past.facts  # the test file sat past the bound
    settings = _settings(org={"required": ["checks.test"]})
    complete = build_run_plan(_inputs(settings, at))
    row = complete.entry("checks.test")
    assert row is not None and row.status == "inapplicable" and row.reason == "no discoverable test command"
    assert "checks.test" not in {entry.id for entry in complete.required_blockers()}
    bounded = build_run_plan(_inputs(settings, past))
    row = bounded.entry("checks.test")
    assert row is not None and row.status == "not_available"
    assert row.reason == (
        f"{catalog.COVERAGE_UNKNOWN}: trusted-base detection stopped at its path bound, "
        "so 'no discoverable test command' is not established"
    )
    assert "checks.test" in {entry.id for entry in bounded.required_blockers()}
    # Optional entries are shown as unknown rather than omitted as absent.
    optional = build_run_plan(_inputs(_settings(repo={"effort": "deep"}), past)).entry("migrations.execute")
    assert optional is not None and optional.status == "not_available"
    assert optional.reason.startswith(catalog.COVERAGE_UNKNOWN)
    # Truncation is part of the plan's input identity.
    assert _inputs(settings, at).digest() != _inputs(settings, past).digest()
    assert "truncated" not in at.digest_document()
    # Not offered in "Run specific": a pick of a coverage-unknown entry can only plan not_available.
    assert "migrations.execute" not in {row["id"] for row in catalog_wire(past)}
    picked = amend_run_plan(
        bounded, _inputs(settings, past), usage=BudgetUsage(), manual=[ManualPick("migrations.execute")]
    )
    row = picked.entry("migrations.execute")
    assert row is not None and row.status == "not_available" and row.reason.startswith(catalog.COVERAGE_UNKNOWN)


def test_manual_picks_survive_a_plan_input_change_while_their_capability_resolves() -> None:
    settings = _settings(repo={"effort": "quick"})
    recipes = _detected(browser_scenarios=(DynamicTarget("login"),))
    first = build_run_plan(_inputs(settings, recipes))
    surface = ManualPick("surface:fixture:api/status", target=DynamicTarget(id="fixture:api/status", title="GET"))
    picked = amend_run_plan(
        first,
        _inputs(settings, recipes),
        usage=BudgetUsage(),
        manual=[ManualPick("ai.hypotheses"), ManualPick("browser:login"), surface],
    )
    assert {pick.id for pick in picked.manual} == {"ai.hypotheses", "browser:login", surface.id}
    # The trusted-base policy changes (a new label set) but every pick still resolves.
    changed = _inputs(settings, recipes, labels=("risky",))
    assert changed.digest() != picked.inputs_digest
    rebuilt = amend_run_plan(picked, changed, usage=BudgetUsage())
    assert rebuilt.inputs_digest == changed.digest()
    assert {pick.id for pick in rebuilt.manual} == {"ai.hypotheses", "browser:login", surface.id}
    assert next(p for p in rebuilt.manual if p.id == surface.id).target == surface.target
    for entry_id in ("ai.hypotheses", "browser:login", surface.id):
        row = rebuilt.entry(entry_id)
        assert row is not None and row.status == "run" and row.origin == "manual", entry_id
    assert rebuilt.manual_dropped == () and rebuilt.to_wire()["manual_dropped"] == []
    # A plan of another Snapshot carries nothing.
    other = replace(changed, revision_id="revision-2")
    assert amend_run_plan(picked, other, usage=BudgetUsage()).manual == ()


def test_a_pick_whose_capability_left_the_trusted_base_is_dropped_with_its_reason() -> None:
    settings = _settings(repo={"effort": "quick"})
    recipes = _detected(browser_scenarios=(DynamicTarget("login"),))
    picked = amend_run_plan(
        build_run_plan(_inputs(settings, recipes)),
        _inputs(settings, recipes),
        usage=BudgetUsage(),
        manual=[ManualPick("browser:login"), ManualPick("ai.hypotheses")],
    )
    # The trusted base no longer lists the scenario.
    rebuilt = amend_run_plan(picked, _inputs(settings, _detected()), usage=BudgetUsage())
    assert [pick.id for pick in rebuilt.manual] == ["ai.hypotheses"]
    assert rebuilt.entry("browser:login") is None
    reason = "capability 'browser:login' is no longer in the catalog or trusted base after the plan inputs changed"
    assert rebuilt.to_wire()["manual_dropped"] == [{"id": "browser:login", "reason": reason}]
    assert f"manual pick browser:login dropped: {reason}" in rebuilt.notes
    # The reason is persisted with the plan and survives a further rebuild.
    assert RunPlan.from_wire(rebuilt.to_wire()).manual_dropped == rebuilt.manual_dropped
    again = amend_run_plan(rebuilt, _inputs(settings, _detected(), labels=("x",)), usage=BudgetUsage())
    assert [pick.id for pick in again.manual_dropped] == ["browser:login"]
    # The trusted base lists the scenario again: the stale drop notice clears.
    restored = amend_run_plan(again, _inputs(settings, recipes, labels=("y",)), usage=BudgetUsage())
    assert restored.manual_dropped == () and not any("browser:login" in note for note in restored.notes)
    assert restored.to_wire()["manual_dropped"] == []
    # Picking it again once the base configures it clears the notice.
    back = amend_run_plan(again, _inputs(settings, recipes), usage=BudgetUsage(), manual=[ManualPick("browser:login")])
    assert back.manual_dropped == () and back.runs("browser:login")


def _managed_floor_settings(**repo: Any) -> EffectiveRunSettings:
    return _settings(
        org={"required": ["ai.investigators"], "budget": {"monthly_managed_usd": 50, "per_review_model_calls": 0}},
        repo={"run_on": "managed", **repo},
    )


@pytest.mark.parametrize("spent", [50.0, None])
def test_managed_money_cap_limits_required_work_when_no_other_executor_is_allowed(spent: float | None) -> None:
    plan = _plan(_managed_floor_settings(), usage=BudgetUsage(monthly_managed_usd=spent), allowed_run_on=("managed",))
    investigators = plan.entry("ai.investigators")
    assert investigators is not None and investigators.required
    assert investigators.status == "limited_by_budget"
    assert investigators.reason.startswith("Limited by budget: ")
    assert ("cap reached" if spent is not None else "cannot be verified") in investigators.reason
    assert not [
        row.id for row in plan.entries if row.status == "run" and row.executor == "managed" and (not row.external)
    ]
    assert "ai.investigators" in [row.id for row in plan.required_blockers()]
    assert "ai.investigators" in plan.to_wire()["required_blockers"]
    assert not ai_stage_plan(plan).investigators
    within = _plan(_managed_floor_settings(), usage=BudgetUsage(monthly_managed_usd=10.0), allowed_run_on=("managed",))
    assert within.entry("ai.investigators").status == "run"
    assert within.entry("ai.investigators").executor == "managed"
    assert not within.required_blockers()


@pytest.mark.usefixtures("executors")
def test_required_lemoncrow_checks_are_always_planned_and_unmapped_ones_are_visible() -> None:
    from lemoncrow.pro.capabilities.review.run_plan import REQUIRED_NO_EXECUTOR, FloorRule

    detected = _detected(browser_scenarios=(DynamicTarget("login"),), surfaces=(DynamicTarget("checks"),))
    origin = SettingOrigin("organization", "organization Review policy required_checks")
    floors = tuple(
        FloorRule("check", key, origin)
        for key in ("login", "surface:service:checks:tests", "checks.lint", "security:sast")
    )
    settings = _settings(
        repo={
            "effort": "quick",
            "capabilities": dict.fromkeys(FAMILIES, "off"),
            "budget": {"per_review_minutes": 0, "per_review_model_calls": 0},
        }
    )
    plan = _plan(settings, detected, extra_floors=floors)
    for entry_id in ("browser:login", "surface:checks", "checks.lint"):
        row = plan.entry(entry_id)
        assert row is not None, entry_id
        assert (row.status, row.required, row.floor_kind) == ("run", True, "check"), row
    missing = plan.entry("check:security:sast")
    assert missing is not None and missing.required and (missing.status == "not_available")
    assert missing.reason.startswith(REQUIRED_NO_EXECUTOR)
    assert [row.id for row in plan.required_blockers()] == ["check:security:sast"]
    assert RunPlan.from_wire(plan.to_wire()).entry("check:security:sast") == missing


def test_a_required_check_on_an_entry_without_an_executor_is_required_no_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemoncrow.pro.capabilities.review.run_plan import REQUIRED_NO_EXECUTOR, FloorRule

    monkeypatch.setitem(STATIC_CATALOG, "visual.matrix", replace(STATIC_CATALOG["visual.matrix"], executor=""))
    floor = FloorRule("check", "visual.matrix", SettingOrigin("repository", "repository Review policy required_checks"))
    plan = _plan(_settings(), extra_floors=(floor,))
    row = plan.entry("visual.matrix")
    assert row is not None and row.status == "not_available" and row.reason.startswith(REQUIRED_NO_EXECUTOR)
    assert [item.id for item in plan.required_blockers()] == ["visual.matrix"]
    assert plan.entry("visual.screenshot").pulled_in_by == ("visual.matrix",)
    assert plan.entry("visual.screenshot").required
    deep = _plan(_settings(org={"min_effort": [{"effort": "deep"}]}))
    assert not deep.required_blockers()


def test_compute_ledger_meters_per_entry_executor_and_stage(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.compute_ledger import ComputeLedger

    store, revision = _store(tmp_path)
    ledger = ComputeLedger(store)
    recorder = ledger.recorder(revision.review_id, revision.id)
    recorder.record_compute("surface:checks", "local", "runtime_validation", 90.0)
    recorder.record_compute("surface:checks", "local", "runtime_validation", 30.0)
    recorder.record_compute("surface:checks", "local", "surface_run", 6.0)
    projection = ledger.projection([revision.id])
    assert projection["rows"] == [
        {
            "entry_id": "surface:checks",
            "executor": "local",
            "stage": "runtime_validation",
            "compute_minutes": 2.0,
            "runs": 2,
        },
        {"entry_id": "surface:checks", "executor": "local", "stage": "surface_run", "compute_minutes": 0.1, "runs": 1},
    ]
    assert projection["totals"] == {"compute_minutes": 2.1, "runs": 3}
    assert ledger.minutes([revision.id]) == pytest.approx(2.1)
    assert ledger.projection(["other-revision"])["totals"] == {"compute_minutes": 0.0, "runs": 0}
    for bad in (("surface:checks", "local", "x", -1.0), ("", "local", "x", 1.0), ("a", "local", "x", float("nan"))):
        with pytest.raises(ValueError):
            recorder.record_compute(*bad)


def _loci_inputs(settings: EffectiveRunSettings, **kwargs: Any) -> PlanInputs:
    detected = _detected(browser_scenarios=(DynamicTarget("login"),), surfaces=(DynamicTarget("checks"),))
    return _inputs(
        settings,
        detected,
        ai_candidates=5,
        risk_signal=True,
        allowed_run_on=("managed",),
        allowed_run_on_by_class={"ai": ("managed",), "runtime": ("worker",)},
        **kwargs,
    )


def test_the_default_execution_class_holds_only_required_checks_with_no_executor() -> None:
    """``execution_block.execution`` stops ``default``: it must hold nothing that is not code execution."""

    from lemoncrow.pro.capabilities.review.capability_catalog import DYNAMIC_PREFIXES, STATIC_CATALOG
    from lemoncrow.pro.capabilities.review.run_plan import execution_class
    from lemoncrow.pro.capabilities.review.run_plan_service import BLOCKED_CLASSES

    catalog = [*STATIC_CATALOG, *(f"{prefix}web" for prefix in DYNAMIC_PREFIXES)]
    assert [entry_id for entry_id in catalog if execution_class(entry_id) == "default"] == []
    # The only ``default`` entry: a required LemonCrow-run verification check
    # (``lemoncrow_verification``) no catalog executor maps, planned as ``check:<key>``.
    assert execution_class("check:tests") == "default"
    assert dict(BLOCKED_CLASSES) == {"ai": ("ai",), "execution": ("runtime", "default")}


@pytest.mark.usefixtures("executors")
def test_ai_and_runtime_entries_follow_their_own_deployment_locus() -> None:
    from lemoncrow.pro.capabilities.review.run_plan import execution_class

    assert [execution_class(entry_id) for entry_id in ("ai.hypotheses", "checks.test", "surface:web")] == [
        "ai",
        "runtime",
        "runtime",
    ]
    assert execution_class("side_effects.runtime") == "runtime"
    assert execution_class("side_effects.static") == "static"
    layers = [parse_run_settings({"effort": "deep"}, origin=REPO)]
    plan = build_run_plan(_loci_inputs(resolve_run_settings(layers, default_run_on="managed")))
    executors = {row.id: row.executor for row in plan.entries}
    for entry_id in ("ai.investigators", "ai.hypotheses"):
        assert executors[entry_id] == "managed", entry_id
    assert executors["side_effects.static"] == "review_host" and plan.runs("side_effects.static")
    for entry_id in (
        "checks.test",
        "behavior.test_diff",
        "visual.screenshot",
        "side_effects.runtime",
        "surface:checks",
    ):
        assert executors[entry_id] == "worker", entry_id
    assert plan.runs("ai.investigators") and plan.runs("checks.test")
    assert plan.run_on == "managed" and plan.run_on_note == ""


@pytest.mark.usefixtures("executors")
def test_runtime_work_placed_on_a_locus_with_no_wired_executor_plans_not_available() -> None:
    from lemoncrow.pro.capabilities.review.run_plan import REQUIRED_NO_EXECUTOR, FloorRule
    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    settings = resolve_run_settings([parse_run_settings({"effort": "deep"}, origin=REPO)], default_run_on="managed")
    floors = (FloorRule("capability", "checks.test", REPO),)
    wired = _loci_inputs(settings, extra_floors=floors)
    unwired = replace(wired, unavailable_run_on_by_class={"runtime": {"worker": "no customer worker connected"}})
    assert replace(wired, unavailable_run_on_by_class={"runtime": {}}).digest() == wired.digest()
    assert unwired.digest() != wired.digest()
    assert build_run_plan(wired).runs("checks.test") and build_run_plan(wired).required_blockers() == ()
    plan = build_run_plan(unwired)
    rows = {row.id: row for row in plan.entries}
    required = rows["checks.test"]
    assert (required.status, required.required, required.executor) == ("not_available", True, "worker")
    assert required.reason == f"{REQUIRED_NO_EXECUTOR} (no customer worker connected)"
    optional = rows["behavior.test_diff"]
    assert (optional.status, optional.required) == ("not_available", False)
    assert optional.reason == "no customer worker connected"
    assert [row.id for row in plan.required_blockers()] == ["checks.test"]
    assert plan.runs("ai.investigators") and plan.runs("side_effects.static")
    assert not [row.id for row in plan.entries if row.executor == "worker" and row.status == "run"]
    assert RunPlanService._unavailable(
        {
            "runtime": {"worker": " no customer worker connected ", "review_host": "x", "managed": ""},
            "static": {"review_host": "x"},
            "ai": "bogus",
        }
    ) == {"runtime": {"worker": "no customer worker connected"}}
    assert RunPlanService._unavailable(None) == {}


@pytest.mark.usefixtures("executors")
def test_a_requested_locus_the_deployment_does_not_allow_for_a_class_shows_the_not_enabled_note() -> None:
    layers = [
        parse_run_settings(
            {
                "effort": "custom",
                "run_on": "managed",
                "custom": [{"id": "checks.lint", "executor": "local"}, "ai.investigators"],
            },
            origin=REPO,
        )
    ]
    plan = build_run_plan(_loci_inputs(resolve_run_settings(layers, default_run_on="managed")))
    assert plan.entry("ai.investigators").executor == "managed"
    assert plan.entry("checks.lint").executor == "worker"
    assert plan.run_on_note == "local execution is not enabled here for runtime work; worker is used"


# --------------------------------------------------------------------------- #
# Trust T3 items 9, 10 and 12: the re-verification summary
# --------------------------------------------------------------------------- #


def _placed(summary: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    return {row["entry_id"]: bucket for bucket, rows in summary.items() for row in rows}


def test_the_reverification_summary_places_every_entry_once_with_its_reason() -> None:
    settings = _settings(
        repo={
            "effort": "custom",
            "custom_from": "auto",
            "custom": [
                {"id": "checks.lint", "when": "open"},
                {"id": "checks.typecheck", "paths": ["api/**"]},
                {"id": "visual.screenshot", "when": "manual"},
                "ai.hypotheses",
            ],
        }
    )
    plan = _plan(settings, ai_candidates=1)
    summary = reverification_summary(plan, {}, recipes_for=lambda _entry: (), is_check=lambda _entry: False)
    assert tuple(summary) == BUCKETS
    placed = [row["entry_id"] for rows in summary.values() for row in rows]
    assert sorted(placed) == sorted(row.id for row in plan.entries)
    where = _placed(summary)
    reasons = {row["entry_id"]: row["reason"] for rows in summary.values() for row in rows}
    assert (where["checks.typecheck"], reasons["checks.typecheck"]) == (
        "skip_not_affected",
        "no changed path matches api/**",
    )
    assert (where["visual.screenshot"], reasons["visual.screenshot"]) == ("not_run", "runs only when requested")
    assert where["ai.hypotheses"] == "not_run" and "risk signal" in reasons["ai.hypotheses"]
    assert where["checks.lint"] == "run"


def test_budget_limited_or_unrunnable_required_work_stays_unknown_never_skipped() -> None:
    plan = _plan(_settings())
    template = plan.entries[0]
    rows = (
        replace(template, id="x.budget", status="limited_by_budget", reason="limited by per_review_minutes"),
        replace(template, id="x.required_budget", status="limited_by_budget", reason="limited", required=True),
        replace(
            template, id="x.floor", status="not_available", reason="no executor", required=True, floor_kind="check"
        ),
        replace(
            template, id="x.depth", status="not_available", reason="no executor", required=True, floor_kind="min_effort"
        ),
        replace(template, id="x.optional", status="not_available", reason="no executor", required=False),
        replace(
            template,
            id="x.unknown",
            status="not_available",
            reason=f"{catalog.COVERAGE_UNKNOWN}: trusted-base detection stopped at its path bound",
            required=False,
        ),
        replace(template, id="x.none", status="inapplicable", reason="no migrations in this repository"),
        replace(template, id="x.ci", status="satisfied", reason="not re-run", satisfied_by={"evidence_id": "ev-ci"}),
        replace(template, id="x.dependent", status="skipped", reason="depends on x.none (inapplicable)"),
    )
    summary = reverification_summary(
        replace(plan, entries=rows), {}, recipes_for=lambda _entry: (), is_check=lambda _entry: False
    )
    assert _placed(summary) == {
        "x.budget": "unknown_not_run",
        "x.required_budget": "unknown_not_run",
        "x.floor": "unknown_not_run",
        "x.depth": "not_run",
        "x.optional": "not_run",
        "x.unknown": "unknown_not_run",
        "x.none": "inapplicable",
        "x.ci": "reuse",
        "x.dependent": "not_run",
    }
    assert summary["unknown_not_run"][0] == {
        "entry_id": "x.budget",
        "title": template.title,
        "required": False,
        "status": "limited_by_budget",
        "reason": "limited by per_review_minutes",
    }
    [ci] = summary["reuse"]
    assert (ci["kind"], ci["satisfied_by"]) == ("ci_equivalent", {"evidence_id": "ev-ci"})


def _differential(recipe_id: str, reason_code: str = "", **head: Any) -> dict[str, Any]:
    attempts = head.pop("attempts", None) or [{"side": "new", "attempt": 1, "variant": "run", **head}]
    validity = {f"check:{recipe_id}#1": {"reason_code": reason_code, "detail": f"{reason_code}!"}}
    return {"recipe": {"id": recipe_id}, "attempts": attempts, "economy": {"validity": validity} if reason_code else {}}


def test_check_recipes_say_whether_they_ran_were_carried_were_not_affected_or_broadened() -> None:
    plan = _plan(_settings())
    selections = (
        RecipeSelection("unit", "impact", "test impact: 1 of 3", tests=("tests/test_a.py",), total=3),
        RecipeSelection("docs", "none", "test impact: only documentation", total=2),
        RecipeSelection("wide", "all", "full suite: no map", total=4, coverage="unknown"),
        RecipeSelection("carried", "impact", "test impact: 2 of 2", tests=("a", "b"), total=2),
    )
    entry = replace(
        plan.entries[0],
        id="checks.test",
        status="run",
        reason="entry reason",
        selection={"map": "m", "recipes": {row.recipe_id: row.to_wire() for row in selections}},
    )
    origin = {"kind": "snapshot", "snapshot": 3, "revision_id": "rev-3", "evidence_id": "ev-3"}
    differentials = {
        "unit": _differential("unit", "carry_inputs_changed"),
        "carried": _differential("carried", "carry_exact_key", reused_from=origin),
        # The deciding head attempt (the last plain head run, which the verdict rests on) is
        # the carry decision: a base or fix copy is not, but a carried rerun is.
        "rerun": _differential(
            "rerun",
            attempts=[
                {"side": "old", "attempt": 1, "variant": "run", "reused_from": origin},
                {"side": "new", "attempt": 2, "variant": "run", "reused_from": origin},
                {"side": "new", "attempt": 1, "variant": "fix", "reused_from": origin},
                {"side": "new", "attempt": 1, "variant": "run"},
            ],
        ),
        # Only an earlier Snapshot's run is a carry; any other provenance on the head ran here.
        "shared": _differential("shared", reused_from={**origin, "kind": "shared_base"}),
    }
    summary = reverification_summary(
        replace(plan, entries=(entry,)),
        differentials,
        recipes_for=lambda _entry: ("unit", "lint", "rerun", "shared"),
        is_check=lambda entry_id: entry_id == "checks.test",
    )

    def shown(bucket: str) -> list[tuple[Any, ...]]:
        keys = ("recipe_id", "state", "reason", "reason_code", "tests_skipped", "origin_snapshot", "coverage")
        return [tuple(row.get(key) for key in keys) for row in summary[bucket]]

    assert shown("run") == [
        ("unit", "ran", "test impact: 1 of 3", "carry_inputs_changed", None, None, None),
        ("lint", "planned", "entry reason", None, None, None, None),
        ("shared", "ran", "entry reason", None, None, None, None),
    ]
    assert summary["run"][0]["detail"] == "carry_inputs_changed!"
    assert shown("skip_not_affected") == [
        ("unit", None, "test impact: 1 of 3", None, 2, None, None),
        ("docs", None, "test impact: only documentation", None, 2, None, None),
    ]
    assert shown("unknown_broadened") == [
        ("wide", "planned", "full suite: no map", None, None, None, "coverage_unknown")
    ]
    rerun, carried = summary["reuse"]
    assert (rerun["recipe_id"], rerun["kind"], rerun["origin_snapshot"]) == ("rerun", "carried", 3)
    assert carried == {
        "entry_id": "checks.test",
        "title": entry.title,
        "required": entry.required,
        "recipe_id": "carried",
        "kind": "carried",
        "origin_snapshot": 3,
        "origin_revision_id": "rev-3",
        "origin_evidence_id": "ev-3",
        "reason_code": "carry_exact_key",
        "detail": "carry_exact_key!",
    }
    # A check entry with no recipe and no selection still shows as planned work.
    bare = reverification_summary(
        replace(plan, entries=(replace(entry, selection=None),)),
        {},
        recipes_for=lambda _entry: (),
        is_check=lambda _entry: True,
    )
    assert [(row["entry_id"], row["state"]) for row in bare["run"]] == [("checks.test", "planned")]


def test_a_check_whose_head_did_not_run_or_whose_verdict_rests_on_a_carried_rerun_says_so() -> None:
    """The summary and its telemetry name the deciding head attempt: never ``ran`` for a head that did not run."""

    from lemoncrow.pro.capabilities.review.trust_telemetry import run_plan_observation

    plan = _plan(_settings())
    entry = replace(plan.entries[0], id="checks.test", status="run", reason="entry reason")
    origin = {"kind": "snapshot", "snapshot": 3, "revision_id": "rev-3", "evidence_id": "ev-3"}
    guard = "the Change edits Makefile, which defines this recipe; the edit is not executed on the head side"
    differentials = {
        # check_runs: a guarded head records one not_run attempt and the differential's head_guard.
        "guarded": {
            **_differential("guarded", status="not_run", reason=guard),
            "head_guard": guard,
        },
        "offline": _differential("offline", "carry_no_prior_run", status="not_run", reason="runner was unavailable"),
        # Attempt 1 ran here and failed; the passing rerun the verdict rests on was carried.
        "flaky": {
            "recipe": {"id": "flaky"},
            "attempts": [
                {"side": "new", "attempt": 1, "variant": "run", "status": "failed"},
                {"side": "new", "attempt": 2, "variant": "run", "status": "passed", "reused_from": origin},
            ],
            "economy": {
                "validity": {
                    "check:flaky#1": {"reason_code": "carry_environment_changed"},
                    "check:flaky#2": {"reason_code": "carry_exact_key", "detail": "same key"},
                }
            },
        },
        # Attempt 1 was carried and failed; the rerun that decides ran here, for its own reason.
        "retried": {
            "recipe": {"id": "retried"},
            "attempts": [
                {"side": "new", "attempt": 2, "variant": "run", "status": "passed"},
                {"side": "new", "attempt": 1, "variant": "run", "status": "failed", "reused_from": origin},
            ],
            "economy": {
                "validity": {
                    "check:retried#1": {"reason_code": "carry_exact_key"},
                    "check:retried#2": {"reason_code": "carry_attempt_differs"},
                }
            },
        },
    }
    summary = reverification_summary(
        replace(plan, entries=(entry,)),
        differentials,
        recipes_for=lambda _entry: tuple(differentials),
        is_check=lambda entry_id: entry_id == "checks.test",
    )

    assert [(row["recipe_id"], row["state"], row["reason_code"], row["reason"]) for row in summary["not_run"]] == [
        ("guarded", "not_run", "head_definition_guard", guard),
        ("offline", "not_run", "executor_unavailable", "runner was unavailable"),
    ]
    [flaky] = summary["reuse"]
    assert (flaky["recipe_id"], flaky["kind"], flaky["reason_code"], flaky["detail"]) == (
        "flaky",
        "carried",
        "carry_exact_key",
        "same key",
    )
    assert flaky["origin_evidence_id"] == "ev-3"
    assert [(row["recipe_id"], row["state"], row["reason_code"]) for row in summary["run"]] == [
        ("retried", "ran", "carry_attempt_differs")
    ]
    assert run_plan_observation(summary)["checks"] == [
        {"disposition": "carried", "reason": "carry_exact_key", "count": 1},
        {"disposition": "not_run", "reason": "executor_unavailable", "count": 1},
        {"disposition": "not_run", "reason": "head_definition_guard", "count": 1},
        {"disposition": "ran", "reason": "carry_attempt_differs", "count": 1},
    ]
