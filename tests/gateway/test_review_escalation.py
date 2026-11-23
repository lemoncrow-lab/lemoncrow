"""Deep-review escalation inside the depth ladder (Phase 11 P11-ESC items 6 and 12).

These tests pin: each deterministic trigger class escalates with a visible
reason and origin; no trigger, no escalation; escalation never touches anything
but the two AI entries' depth/status (no severity, presentation or priority
change); a budget limit is an explicit state; deep review disabled for a path
never escalates there and says why; the ceiling is enforced at and past its
bound.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from lemoncrow.pro.capabilities.review.capability_catalog import DETECTION_FACTS, STATIC_CATALOG, DetectedCapabilities
from lemoncrow.pro.capabilities.review.escalation import (
    ESCALATION_DEFAULTS,
    MAX_INVENTORY_PATHS,
    TRIGGER_CLASSES,
    EscalationFacts,
    EscalationPolicy,
    collect_escalation_facts,
    decide_escalation,
)
from lemoncrow.pro.capabilities.review.fast_lane import FastLaneSignals
from lemoncrow.pro.capabilities.review.run_plan import (
    BudgetUsage,
    EffectiveRunSettings,
    ManualPick,
    PlanInputs,
    RunPlan,
    SettingOrigin,
    amend_run_plan,
    build_run_plan,
    parse_run_settings,
    resolve_run_settings,
)

ORG = SettingOrigin("organization", "organization policy")
REPO = SettingOrigin("trusted_base_repository", "base revision .lemoncrow/review.yaml")
AI = ("ai.investigators", "ai.hypotheses")
NO_FACTS = EscalationFacts()
INVESTIGATOR_EXTRA = STATIC_CATALOG["ai.investigators"].model_calls("thorough") - STATIC_CATALOG[
    "ai.investigators"
].model_calls("auto")
HYPOTHESIS_CALLS = STATIC_CATALOG["ai.hypotheses"].model_calls("auto")


def _settings(org: dict[str, Any] | None = None, repo: dict[str, Any] | None = None) -> EffectiveRunSettings:
    layers = []
    if org is not None:
        layers.append(parse_run_settings(org, origin=ORG, allow_profiles=True))
    if repo is not None:
        layers.append(parse_run_settings(repo, origin=REPO))
    for layer in layers:
        assert layer.errors == (), layer.errors
    return resolve_run_settings(layers)


def _inputs(
    settings: EffectiveRunSettings,
    *,
    facts: EscalationFacts | None = NO_FACTS,
    changed: tuple[str, ...] = ("src/app.py",),
    **kwargs: Any,
) -> PlanInputs:
    return PlanInputs(
        review_id="review-1",
        revision_id="revision-1",
        trigger=kwargs.pop("trigger", "open"),
        settings=settings,
        detected=DetectedCapabilities(facts=frozenset(DETECTION_FACTS)),
        changed_paths=changed,
        escalation=facts,
        **kwargs,
    )


def _plan(settings: EffectiveRunSettings, **kwargs: Any) -> RunPlan:
    return build_run_plan(_inputs(settings, **kwargs))


def _entry(plan: RunPlan, entry_id: str) -> Any:
    row = plan.entry(entry_id)
    assert row is not None, entry_id
    return row


TRIGGERS: dict[str, tuple[EscalationFacts, dict[str, Any], tuple[str, ...], str]] = {
    "criticality": (
        EscalationFacts(criticality=(("src/auth/session.py", "high", "organization triage focus"),)),
        {},
        ("src/auth/session.py",),
        "path criticality high or higher: src/auth/session.py",
    ),
    "static_inventory": (
        EscalationFacts(side_effects=(("db_drop", "db/migrations/0002.sql", "drop_table:users"),)),
        {},
        ("db/migrations/0002.sql",),
        "side-effect inventory changed (db drop) in db/migrations/0002.sql",
    ),
    "changed_contracts": (
        EscalationFacts(side_effects=(("route", "src/api.py", "GET /users"),)),
        {},
        ("src/api.py",),
        "cross-service contract: route changed",
    ),
    "security_chains": (
        EscalationFacts(security_chains=(("p1", "src/report.py", "py.sqli"),)),
        {},
        ("src/report.py",),
        "security chain at p1 or higher (py.sqli) in src/report.py",
    ),
    "explicit_policy": (
        EscalationFacts(),
        {"escalation": {"paths": ["src/billing/**"]}},
        ("src/billing/charge.py",),
        "policy escalates src/billing/charge.py",
    ),
    "author_track_record": (
        EscalationFacts(
            author_track_record=(
                ("claude/claude-sonnet-4-5", "revert_rate", "revert rate 2 of 5 deployed Changes rolled back"),
            )
        ),
        {},
        ("src/app.py",),
        "author track record of claude/claude-sonnet-4-5: revert rate 2 of 5 deployed Changes rolled back",
    ),
}


def test_escalation_trigger_contract() -> None:
    """case: test_every_trigger_class_has_a_fixture"""
    assert set(TRIGGERS) == set(TRIGGER_CLASSES)
    "case: test_each_trigger_class_escalates_auto_with_a_visible_reason_and_origin"
    for trigger_class in TRIGGER_CLASSES:
        facts, org, changed, label = TRIGGERS[trigger_class]
        plan = _plan(_settings(org=org or None), facts=facts, changed=changed)
        assert plan.escalation["state"] == "escalated"
        assert [row["class"] for row in plan.escalation["triggers"]] == [trigger_class]
        assert plan.escalation["triggers"][0]["label"].startswith(label)
        assert plan.escalation["reason"].startswith("escalated to thorough: ")
        investigators = _entry(plan, "ai.investigators")
        assert (investigators.depth, investigators.origin, investigators.status) == ("thorough", "escalation", "run")
        assert label in investigators.reason
        assert investigators.escalation["state"] == "escalated"
        assert (investigators.escalation["from"], investigators.escalation["to"]) == ("auto", "thorough")
        hypotheses = _entry(plan, "ai.hypotheses")
        assert (hypotheses.status, hypotheses.origin) == ("run", "escalation")
        origin = plan.escalation["triggers"][0]["origin"]
        assert origin == ("organization policy" if trigger_class == "explicit_policy" else "built-in default") or (
            trigger_class == "criticality" and origin == "built-in default; criticality from organization triage focus"
        )
        wire = next(row for row in plan.to_wire()["entries"] if row["id"] == "ai.investigators")
        assert wire["origin"] == "escalation" and wire["escalation"]["triggers"][0]["class"] == trigger_class
        assert RunPlan.from_wire(plan.to_wire()).escalation == plan.escalation
    "case: test_contract_schema_paths_escalate_without_inventory_facts"
    plan = _plan(_settings(), changed=("api/billing.proto",))
    assert plan.escalation["state"] == "escalated"
    assert (
        plan.escalation["triggers"][0]["label"] == "cross-service contract: contract schema changed (api/billing.proto)"
    )
    "case: test_without_a_trigger_nothing_escalates"
    for facts, org in [
        (EscalationFacts(), None),
        (EscalationFacts(security_chains=(("p2", "src/a.py", "r"),)), None),
        (EscalationFacts(criticality=(("src/a.py", "high", ""),)), {"escalation": {"criticality": "critical"}}),
        (EscalationFacts(side_effects=(("env_var", "src/a.py", "HOME"),)), None),
        (EscalationFacts(side_effects=(("db_drop", "src/a.py", "x"),)), {"escalation": {"static_inventory": "off"}}),
    ]:
        plan = _plan(_settings(org=org), facts=facts)
        assert plan.escalation["state"] == "none"
        assert plan.escalation["reason"] == "no deterministic escalation signal"
        assert _entry(plan, "ai.investigators").depth == "auto"
        assert _entry(plan, "ai.hypotheses").status == "skipped"
        assert not any(row.origin == "escalation" or row.escalation for row in plan.entries)
    "case: test_unobserved_facts_never_escalate_and_facts_are_not_plan_identity"
    settings = _settings()
    facts = TRIGGERS["security_chains"][0]
    unobserved = _plan(settings, facts=None)
    assert unobserved.escalation == {}
    assert "escalation" not in unobserved.signals
    assert _inputs(settings, facts=facts).digest() == _inputs(settings, facts=None).digest()
    failed = _plan(settings, facts=facts, policy_error="base .lemoncrow/review.yaml is invalid")
    assert failed.escalation == {}


def test_escalation_depth_and_budget_contract() -> None:
    """case: test_escalation_changes_only_the_ai_entries_depth_and_status"""
    "Never severity, presentation or priority: every other entry and field is identical."
    settings = _settings()
    plain = _plan(settings, facts=EscalationFacts(), changed=("src/report.py",))
    escalated = _plan(settings, facts=TRIGGERS["security_chains"][0], changed=("src/report.py",))
    before = {row.id: row for row in plain.entries}
    after = {row.id: row for row in escalated.entries}
    assert set(before) == set(after)
    for entry_id in set(before) - set(AI):
        assert before[entry_id] == after[entry_id], entry_id
    changed_fields = {
        name
        for entry_id in AI
        for name in (
            "status",
            "reason",
            "origin",
            "depth",
            "escalation",
            "estimated_model_calls",
            "estimated_minutes",
            "required",
            "floor_kind",
            "cost_class",
            "degrade_tier",
            "escalate",
            "when",
        )
        if getattr(before[entry_id], name) != getattr(after[entry_id], name)
    }
    assert changed_fields <= {
        "status",
        "reason",
        "origin",
        "depth",
        "escalation",
        "estimated_model_calls",
        "estimated_minutes",
    }
    assert not any(after[entry_id].required for entry_id in AI)
    assert escalated.selection == plain.selection
    assert "severity" not in str(escalated.escalation)
    "case: test_escalation_ceiling_at_and_past_the_bound"
    facts = TRIGGERS["criticality"][0]
    changed = TRIGGERS["criticality"][2]
    exact = INVESTIGATOR_EXTRA + HYPOTHESIS_CALLS
    at = _plan(_settings(org={"escalation": {"max_model_calls": exact}}), facts=facts, changed=changed)
    assert at.escalation["state"] == "escalated"
    assert at.escalation["ceiling"] == {"max_model_calls": exact, "origin": "organization policy"}
    assert _entry(at, "ai.investigators").depth == "thorough"
    assert _entry(at, "ai.hypotheses").status == "run"
    past = _plan(_settings(org={"escalation": {"max_model_calls": exact - 1}}), facts=facts, changed=changed)
    assert past.escalation["state"] == "limited_by_budget"
    assert past.escalation["reason"].startswith("Limited by budget: escalated to thorough: ")
    assert _entry(past, "ai.investigators").depth == "thorough"
    hypotheses = _entry(past, "ai.hypotheses")
    assert hypotheses.status == "limited_by_budget"
    assert hypotheses.reason.startswith("Limited by budget: escalation does not fit")
    assert "organization policy" in hypotheses.reason
    assert hypotheses.escalation["state"] == "limited_by_budget"
    assert past.limited_by_budget
    none = _plan(_settings(org={"escalation": {"max_model_calls": 0}}), facts=facts, changed=changed)
    investigators = _entry(none, "ai.investigators")
    assert (investigators.depth, investigators.budget_reduced_from) == ("auto", "thorough")
    assert investigators.reason.startswith("Limited by budget: escalation to thorough does not fit")
    assert investigators.escalation["state"] == "limited_by_budget"
    assert _entry(none, "ai.hypotheses").status == "limited_by_budget"
    assert none.escalation["state"] == "limited_by_budget"
    assert none.escalation["reason"].startswith("Limited by budget: ")
    "case: test_a_ceiling_below_deep_escalates_as_far_as_it_fits"
    deep_extra = STATIC_CATALOG["ai.investigators"].model_calls("deep") - STATIC_CATALOG[
        "ai.investigators"
    ].model_calls("auto")
    facts, _org, changed, _label = TRIGGERS["security_chains"]
    settings = _settings(org={"escalation": {"to": "deep", "max_model_calls": deep_extra - 1}})
    plan = _plan(settings, facts=facts, changed=changed)
    investigators = _entry(plan, "ai.investigators")
    assert (investigators.depth, investigators.budget_reduced_from) == ("thorough", "deep")
    assert investigators.reason.startswith("Limited by budget: escalated to thorough, not deep")
    assert plan.escalation["state"] == "limited_by_budget"
    full = _plan(_settings(org={"escalation": {"to": "deep", "max_model_calls": deep_extra + 1}}), facts=facts)
    assert _entry(full, "ai.investigators").depth == "deep"
    "case: test_the_per_review_budget_limits_escalation_visibly"
    facts, _org, changed, _label = TRIGGERS["static_inventory"]
    plain = _plan(_settings(), facts=EscalationFacts(), changed=changed)
    budget = sum(row.estimated_model_calls for row in plain.entries if row.status == "run")
    plan = _plan(_settings(org={"budget": {"per_review_model_calls": budget}}), facts=facts, changed=changed)
    assert plan.escalation["state"] == "limited_by_budget"
    limited = [row for row in plan.entries if row.escalation and row.escalation["state"] == "limited_by_budget"]
    assert limited, plan.to_wire()["entries"]
    assert all(row.reason.startswith("Limited by budget") for row in limited)
    "case: test_deep_review_disabled_for_a_path_never_escalates_there_and_says_why"
    facts = EscalationFacts(criticality=(("src/legacy/billing.py", "critical", ""),))
    settings = _settings(org={"escalation": {"disabled_paths": ["src/legacy/**"]}})
    plan = _plan(settings, facts=facts, changed=("src/legacy/billing.py",))
    assert plan.escalation["state"] == "suppressed"
    assert (
        plan.escalation["reason"]
        == "not escalated: deep review is disabled for src/legacy/billing.py (organization policy)"
    )
    assert plan.escalation["suppressed"][0]["class"] == "criticality"
    assert _entry(plan, "ai.investigators").depth == "auto"
    assert _entry(plan, "ai.hypotheses").status == "skipped"
    mixed = EscalationFacts(criticality=facts.criticality, security_chains=(("p0", "src/api/report.py", "py.sqli"),))
    both = _plan(settings, facts=mixed, changed=("src/legacy/billing.py", "src/api/report.py"))
    assert both.escalation["state"] == "escalated"
    assert [row["class"] for row in both.escalation["triggers"]] == ["security_chains"]
    assert [row["class"] for row in both.escalation["suppressed"]] == ["criticality"]
    overflow = _plan(settings, facts=EscalationFacts(not_inventoried=3), changed=("src/legacy/a.py",))
    assert overflow.escalation["state"] == "suppressed"
    "case: test_disabled_escalation_and_floors_win"
    facts, _org, changed, _label = TRIGGERS["security_chains"]
    off = _plan(_settings(repo={"escalation": False}), facts=facts, changed=changed)
    assert off.escalation["state"] == "disabled"
    assert off.escalation["reason"] == "deep review escalation is turned off (base revision .lemoncrow/review.yaml)"
    assert _entry(off, "ai.investigators").depth == "auto"
    floored = _plan(
        _settings(
            org={"min_effort": [{"effort": "thorough", "paths": ["src/**"]}]},
            repo={"escalation": {"disabled_paths": ["src/**"]}},
        ),
        facts=facts,
        changed=changed,
    )
    investigators = _entry(floored, "ai.investigators")
    assert (investigators.depth, investigators.origin, investigators.required) == ("thorough", "policy_floor", True)
    assert floored.escalation["state"] == "suppressed"
    "case: test_quick_never_escalates_and_thorough_escalates_only_to_deep"
    facts, _org, changed, _label = TRIGGERS["security_chains"]
    quick = _plan(_settings(repo={"effort": "quick"}), facts=facts, changed=changed)
    assert quick.escalation["state"] == "not_applicable"
    assert quick.escalation["reason"].startswith("Quick does not escalate; ")
    assert _entry(quick, "ai.investigators").depth == "quick"
    thorough = _plan(_settings(repo={"effort": "thorough"}), facts=facts, changed=changed)
    assert thorough.escalation["state"] == "not_applicable"
    deep = _plan(_settings(repo={"effort": "thorough", "escalation": {"to": "deep"}}), facts=facts, changed=changed)
    assert deep.escalation["state"] == "escalated"
    assert _entry(deep, "ai.investigators").depth == "deep"


def test_an_unknown_track_record_is_a_note_never_a_trigger() -> None:
    note = "author track record of claude/claude-sonnet-4-5 is unknown beyond the 200 most recent Reviews of the last 90 days"
    facts = EscalationFacts(track_record_unknown=note)
    assert EscalationFacts.from_wire(facts.to_wire()) == facts
    assert "track_record_unknown" not in EscalationFacts().to_wire()  # a plan without it keeps its digest
    plan = _plan(_settings(), facts=facts)
    assert plan.escalation["state"] == "none" and plan.escalation["triggers"] == []
    assert plan.escalation["notes"] == [note]
    assert _entry(plan, "ai.investigators").depth == "auto"
    # The note names a check the policy ran: turning the track record off or escalation off drops it.
    off = _plan(_settings(org={"escalation": {"author_track_record": "off"}}), facts=facts)
    assert "notes" not in off.escalation
    disabled = _plan(_settings(repo={"escalation": False}), facts=facts)
    assert "notes" not in disabled.escalation
    # A poor record keeps escalating and still carries the note.
    poor = _plan(
        _settings(),
        facts=EscalationFacts(
            track_record_unknown=note, author_track_record=(("claude/claude-sonnet-4-5", "escaped_defects", "x"),)
        ),
    )
    assert poor.escalation["state"] == "escalated" and poor.escalation["notes"] == [note]


def test_escalation_origin_contract() -> None:
    """case: test_manual_picks_keep_their_origin_and_amendments_keep_the_frozen_facts"""
    settings = _settings()
    facts, _org, changed, _label = TRIGGERS["security_chains"]
    first = _plan(settings, facts=facts, changed=changed)
    assert first.signals["escalation"] == facts.to_wire()
    amended = amend_run_plan(
        first, _inputs(settings, facts=None, changed=changed), usage=BudgetUsage(), manual=[ManualPick("checks.lint")]
    )
    assert amended.escalation["state"] == "escalated"
    assert _entry(amended, "ai.investigators").depth == "thorough"
    "case: test_escalation_blocks_the_push_fast_lane_with_the_trigger"
    settings = _settings()
    facts, _org, changed, label = TRIGGERS["security_chains"]
    signals = FastLaneSignals("revision-0", True)
    plain = _plan(settings, facts=EscalationFacts(), changed=changed, trigger="push", fast_lane=signals)
    assert plain.fast_lane is not None and plain.fast_lane["eligible"] is True
    escalated = _plan(settings, facts=facts, changed=changed, trigger="push", fast_lane=signals)
    assert escalated.fast_lane is not None and escalated.fast_lane["eligible"] is False
    assert escalated.fast_lane["reasons"] == ["deep review escalated: " + label]
    assert _entry(escalated, "ai.investigators").depth == "thorough"
    "case: test_inventory_facts_diff_base_and_head_and_overflow_escalates_conservatively"
    base: dict[str, str] = {"db/migrations/0001.sql": "CREATE TABLE users (id int);\n"}
    head = {**base, "db/migrations/0001.sql": "DROP TABLE users;\n", "db/migrations/0002.sql": "SELECT 1;\n"}
    facts = collect_escalation_facts(
        ("db/migrations/0001.sql", "db/migrations/0002.sql"),
        read_base=lambda path: base.get(path, ""),
        read_head=head.get,
        classify=lambda path: ("high", "triage focus") if path.endswith("0001.sql") else ("normal", ""),
    )
    assert [(row[0], row[1]) for row in facts.side_effects if row[0] in {"db_drop", "migration"}] == [
        ("db_drop", "db/migrations/0001.sql"),
        ("migration", "db/migrations/0001.sql"),
        ("migration", "db/migrations/0002.sql"),
    ]
    assert facts.criticality == (("db/migrations/0001.sql", "high", "triage focus"),)
    unchanged = collect_escalation_facts(("db/migrations/0001.sql",), read_base=head.get, read_head=head.get)
    assert unchanged.side_effects == ()
    reads: list[str] = []

    def read(path: str) -> str:
        reads.append(path)
        return ""

    at = collect_escalation_facts([f"src/m{i}.py" for i in range(MAX_INVENTORY_PATHS)], read_base=read, read_head=read)
    assert at.not_inventoried == 0 and len(reads) == 2 * MAX_INVENTORY_PATHS
    reads.clear()
    past = collect_escalation_facts(
        [f"src/m{i}.py" for i in range(MAX_INVENTORY_PATHS + 1)], read_base=read, read_head=read
    )
    assert past.not_inventoried == 1 and len(reads) == 2 * MAX_INVENTORY_PATHS
    decision = decide_escalation(past, EscalationPolicy(), ["src/m0.py"])
    assert decision.state == "escalated"
    assert decision.triggers[0].label.startswith("1 changed file(s) past the 200-file inventory bound")
    assert EscalationFacts.from_wire(past.to_wire()) == past


def test_escalation_settings_contract() -> None:
    """case: test_escalation_settings_round_trip_with_origins_and_reject_invalid_values"""
    document = {
        "escalation": {
            "to": "deep",
            "max_model_calls": 6,
            "criticality": "critical",
            "security_chains": "off",
            "static_inventory": ["db_drop"],
            "paths": ["src/billing/**"],
            "disabled_paths": ["vendor/**"],
        }
    }
    layer = parse_run_settings(document, origin=ORG)
    assert layer.errors == ()
    assert parse_run_settings(layer.to_document(), origin=ORG) == layer
    settings = resolve_run_settings([layer, parse_run_settings({"escalation": {"to": "thorough"}}, origin=REPO)])
    wire = settings.to_wire()["escalation"]
    assert set(wire) == set(ESCALATION_DEFAULTS)
    assert wire["to"] == {"effective_value": "thorough", "mode": "override", "origin": REPO.to_wire()}
    assert wire["max_model_calls"]["effective_value"] == 6
    assert wire["max_model_calls"]["origin"] == ORG.to_wire()
    assert wire["enabled"]["mode"] == "inherit"
    assert wire["enabled"]["origin"]["layer"] == "built_in_default"
    assert settings.digest_document()["escalation"]["to"] == "thorough"
    assert "escalation" not in _settings().digest_document()
    for bad, message in (
        ({"to": "quick"}, "escalation.to must be thorough or deep"),
        ({"max_model_calls": -1}, "escalation.max_model_calls"),
        ({"criticality": "medium"}, "escalation.criticality"),
        ({"static_inventory": ["nope"]}, "unknown side-effect category"),
        ({"surprise": 1}, "unknown escalation setting"),
        ({"disabled_paths": [""]}, "escalation.disabled_paths"),
    ):
        errors = parse_run_settings({"escalation": bad}, origin=ORG).errors
        assert any(message in error for error in errors), (bad, errors)
    "case: test_policy_value_falls_back_to_the_built_in_default_with_its_origin"
    policy = EscalationPolicy.from_settings(_settings())
    assert policy.value("to") == "thorough"
    assert policy.origin_label("to") == "built-in default"
    overridden = EscalationPolicy.from_settings(_settings(repo={"escalation": {"to": "deep"}}))
    assert overridden.origin_label("to") == REPO.source
    assert replace(overridden, explicit={}).value("to") == "thorough"
