"""Focused per-rule model checks (Phase 11 P11-ESC item 5).

One bounded question per authoritative rule, as sub-entries of the
``ai.rule_checks`` run-plan entry with its own ceiling (tested at and past the
bound); answers cached by (rule digest, Snapshot input digest, model route);
deterministic findings are context and never re-asked; an unavailable model
route or exhausted budget is an explicit per-question state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import ai_review as ai_review_module
from lemoncrow.pro.capabilities.review import static_findings as static_findings_module
from lemoncrow.pro.capabilities.review.ai_review import INVESTIGATION_SCHEMA, run_independent_review
from lemoncrow.pro.capabilities.review.capability_catalog import DETECTION_FACTS, DetectedCapabilities
from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallBudget, StructuredExecution
from lemoncrow.pro.capabilities.review.rule_checks import (
    MAX_RULE_TARGETS,
    RULE_CHECK_DEFAULTS,
    RULE_CHECK_KIND,
    RULE_CHECK_SCHEMA,
    RuleQuestion,
    execute_rule_checks,
    rule_check_cache_key,
    rule_questions,
)
from lemoncrow.pro.capabilities.review.rules import ReviewRule
from lemoncrow.pro.capabilities.review.run_plan import (
    AiStagePlan,
    EffectiveRunSettings,
    PlanInputs,
    RunPlan,
    SettingOrigin,
    ai_stage_plan,
    build_run_plan,
    parse_run_settings,
    resolve_run_settings,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id
from tests.gateway.test_review_ai_review import _execution, _store, _target

ORG = SettingOrigin("organization", "organization policy")
CEILING = int(RULE_CHECK_DEFAULTS["max_model_calls"])


def _rule(stable_key: str = "auth-required", *, paths: tuple[str, ...] = ("src/auth.py",), **kwargs: Any) -> ReviewRule:
    return ReviewRule(
        stable_key=stable_key,
        instruction=kwargs.pop("instruction", "Authentication must be enforced before privileged behavior."),
        path_scope=paths,
        origin="base revision .lemoncrow/review.yaml",
        trust_class=kwargs.pop("trust_class", "trusted_base_repository"),
        source_identity="base:review.yaml",
        source_location=".lemoncrow/review.yaml",
    )


def _questions(count: int) -> tuple[RuleQuestion, ...]:
    return rule_questions([_rule(f"rule-{index:02d}", paths=("src/**",)) for index in range(count)], ["src/a.py"])


def _settings(org: dict[str, Any] | None = None) -> EffectiveRunSettings:
    layers = [] if org is None else [parse_run_settings(org, origin=ORG)]
    for layer in layers:
        assert layer.errors == (), layer.errors
    return resolve_run_settings(layers)


def _plan(settings: EffectiveRunSettings, questions: tuple[RuleQuestion, ...]) -> RunPlan:
    return build_run_plan(
        PlanInputs(
            review_id="review-1",
            revision_id="revision-1",
            trigger="open",
            settings=settings,
            detected=DetectedCapabilities(facts=frozenset(DETECTION_FACTS), rule_checks=len(questions)),
            changed_paths=("src/a.py",),
            rule_questions=questions,
        )
    )


def _entry(plan: RunPlan) -> Any:
    row = plan.entry("ai.rule_checks")
    assert row is not None
    return row


# --------------------------------------------------------------------------- #
# planning: one question per rule, its own ceiling
# --------------------------------------------------------------------------- #


def test_one_question_per_authoritative_rule_that_applies_to_a_changed_path() -> None:
    auth = _rule("auth-required")
    billing = _rule("billing-idempotent", paths=("src/billing/**",))
    head = _rule("head-proposal", trust_class="head_change_candidate")
    questions = rule_questions([auth, auth, billing, head], ["src/auth.py", "docs/x.md"])
    assert [(row.ref, row.paths) for row in questions] == [(auth.ref, ("src/auth.py",))]
    assert questions[0].digest == auth.digest


def test_rule_checks_are_a_plan_entry_with_one_sub_entry_per_rule_question() -> None:
    questions = _questions(2)
    plan = _plan(_settings(), questions)
    entry = _entry(plan)
    assert (entry.status, entry.estimated_model_calls) == ("run", 2)
    assert entry.rule_refs == tuple(row.ref for row in questions)
    assert entry.reason == "2 rule question(s), one bounded model call each"
    assert entry.rule_checks["ceiling"] == CEILING
    assert entry.rule_checks["ceiling_origin"] == "built-in default"
    assert [row["state"] for row in entry.rule_checks["questions"]] == ["planned", "planned"]
    stage = ai_stage_plan(plan)
    assert stage.rule_checks == entry.rule_refs and stage.runs_ai
    # The questions are execution identity: another rule set is another plan.
    assert _plan(_settings(), _questions(1)).inputs_digest != plan.inputs_digest
    assert RunPlan.from_wire(plan.to_wire()).entry("ai.rule_checks") == entry


def test_rule_check_ceiling_at_and_past_the_bound() -> None:
    at = _entry(_plan(_settings(), _questions(CEILING)))
    assert (at.status, at.estimated_model_calls, at.rule_checks["limited"]) == ("run", CEILING, 0)
    assert not at.reason.startswith("Limited by budget")

    plan = _plan(_settings(), _questions(CEILING + 1))
    past = _entry(plan)
    assert (past.status, past.estimated_model_calls) == ("run", CEILING)
    assert len(past.rule_refs) == CEILING
    assert past.rule_checks["limited"] == 1
    assert past.rule_checks["questions"][-1]["state"] == "limited_by_budget"
    assert past.reason == (
        f"Limited by budget: {CEILING} of {CEILING + 1} rule questions fit; the rule-check ceiling is "
        f"{CEILING} model call(s) (built-in default); 1 not asked"
    )
    assert plan.limited_by_budget

    lowered = _entry(_plan(_settings({"rule_checks": {"max_model_calls": 1}}), _questions(2)))
    assert lowered.rule_checks["ceiling_origin"] == "organization policy"
    assert len(lowered.rule_refs) == 1

    none = _entry(_plan(_settings({"rule_checks": {"max_model_calls": 0}}), _questions(1)))
    assert none.status == "limited_by_budget" and none.rule_refs == ()
    assert none.reason.startswith("Limited by budget: the rule-check ceiling is 0 model call(s) (organization policy)")


def test_rule_checks_turned_off_or_without_rules_never_run() -> None:
    off = _entry(_plan(_settings({"rule_checks": False}), _questions(1)))
    assert off.status == "skipped" and off.reason == "rule checks are turned off (organization policy)"
    assert off.rule_refs == ()
    assert _plan(_settings(), ()).entry("ai.rule_checks") is None
    quick = _plan(_settings({"effort": "quick"}), _questions(1)).entry("ai.rule_checks")
    assert quick is None or quick.status != "run"


def test_rule_check_settings_round_trip_with_origin() -> None:
    layer = parse_run_settings({"rule_checks": {"enabled": True, "max_model_calls": 3}}, origin=ORG)
    assert parse_run_settings(layer.to_document(), origin=ORG) == layer
    wire = resolve_run_settings([layer]).to_wire()["rule_checks"]
    assert wire["max_model_calls"] == {"effective_value": 3, "mode": "override", "origin": ORG.to_wire()}
    assert _settings().to_wire()["rule_checks"]["max_model_calls"]["mode"] == "inherit"
    for bad in ({"max_model_calls": -1}, {"max_model_calls": True}, {"surprise": 1}, []):
        assert parse_run_settings({"rule_checks": bad}, origin=ORG).errors, bad


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #


def _violated(rule: ReviewRule, refs: list[str]) -> StructuredExecution:
    return _execution(
        {
            "status": "violated",
            "target_ids": ["target-auth"],
            "title": "Privileged path skips authentication",
            "explanation": f"The changed branch returns before authentication, contrary to {rule.stable_key}.",
            "consequence": "An unauthenticated caller reaches privileged behavior.",
            "evidence_refs": refs,
        }
    )


class _Model:
    """Records every rule-check call; answers with *answer(rule_ref)*."""

    def __init__(self, answer: Any = None) -> None:
        self.prompts: list[dict[str, Any]] = []
        self.answer = answer

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        assert kwargs["output_schema"] is RULE_CHECK_SCHEMA
        blocks = json.loads(kwargs["prompt"])
        self.prompts.append(blocks)
        if self.answer is not None:
            return self.answer(blocks)  # type: ignore[no-any-return]
        return _execution(
            {
                "status": "satisfied",
                "target_ids": [],
                "title": "",
                "explanation": "",
                "consequence": "",
                "evidence_refs": [],
            }
        )


def _run(store: ReviewStore, revision: ReviewRevision, rules: list[ReviewRule], executor: _Model, **kwargs: Any) -> Any:
    return execute_rule_checks(
        store=store,
        revision=revision,
        targets=kwargs.pop("targets", (_target(),)),
        repo_root=Path(str(store.root)),
        rules=rules,
        planned_refs=[rule.ref for rule in rules],
        driver="openai",
        model=kwargs.pop("model", "review-model"),
        budget=kwargs.pop("budget", ModelCallBudget()),
        structured_executor=executor,
        **kwargs,
    )


def _next_revision(store: ReviewStore, revision: ReviewRevision, source: str) -> ReviewRevision:
    later = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=revision.review_id,
            revision_number=revision.revision_number + 1,
            range_mode="working_tree",
            tree_fingerprint=f"tree-{revision.revision_number + 1}",
            source_fingerprint=f"source-{revision.revision_number + 1}",
            packet_schema_version=1,
            provenance_host="claude",
            provenance_model="sonnet",
        ),
        [],
    )
    store.write_blob_artifact(later.review_id, later.id, {"src/auth.py": source, "src/plain.py": "x = 1\n"})
    return later


def test_each_rule_is_one_bounded_question(tmp_path: Path) -> None:
    store, revision = _store(tmp_path)
    auth, other = _rule("auth-required"), _rule("audit-log", instruction="Privileged calls are audited.")
    model = _Model()
    run = _run(store, revision, [auth, other], model)
    assert len(model.prompts) == 2
    for blocks, rule, other_rule in zip(model.prompts, (auth, other), (other, auth), strict=True):
        # Exactly one rule per question, as an instruction-authority block.
        block = blocks["trusted_repository_rule"]
        assert block["authority"].startswith("instruction:")
        assert (block["provenance"], block["value"]["ref"]) == (rule.ref, rule.ref)
        assert other_rule.ref not in json.dumps(blocks)
    assert [(row["ref"], row["state"], row["answer"]) for row in run.states] == [
        (auth.ref, "answered", "satisfied"),
        (other.ref, "answered", "satisfied"),
    ]
    assert run.findings == []


def test_question_context_is_bounded_at_and_past_the_target_bound(tmp_path: Path) -> None:
    store, revision = _store(tmp_path)
    rule = _rule(paths=("src/**",))
    targets = [_target(target_id=f"target-{index}", path="src/auth.py") for index in range(MAX_RULE_TARGETS + 1)]
    at = _run(store, revision, [rule], _Model(), targets=targets[:MAX_RULE_TARGETS])
    assert at.states[0]["targets"] == MAX_RULE_TARGETS
    past = _run(store, revision, [rule], _Model(), targets=targets)
    assert past.states[0]["targets"] == MAX_RULE_TARGETS


def test_answers_are_cached_by_rule_input_and_route(tmp_path: Path) -> None:
    store, revision = _store(tmp_path)
    rule = _rule()
    first_model = _Model(lambda blocks: _violated(rule, ["target:target-auth"]))
    first = _run(store, revision, [rule], first_model)
    assert first.states[0]["state"] == "answered" and len(first.findings) == 1
    finding = first.findings[0]
    assert (finding.category, finding.rule_refs) == ("critical_rule", (rule.ref,))
    key = first.states[0]["cache_key"]
    assert key.startswith("rulecheck_")
    assert (
        store.derived_results(RULE_CHECK_KIND, [revision.id])[(revision.id, key)][1]["result"]["status"] == "violated"
    )

    # Identical rule, identical input on the next Snapshot, same route: a hit, no model call.
    same = _next_revision(store, revision, store.read_blob_artifact(revision.review_id, revision.id)["src/auth.py"])  # type: ignore[index]
    model = _Model()
    hit = _run(store, same, [rule], model)
    assert model.prompts == []
    assert (hit.states[0]["state"], hit.states[0]["cache_key"], hit.cache_hits) == ("cached", key, 1)
    assert [row.id for row in hit.findings] == [finding.id]

    # A changed rule, a changed input or another route asks again.
    changed_rule = _rule(instruction="Authentication and authorization must both run first.")
    assert len(_record(store, same, [changed_rule]).prompts) == 1
    edited = _next_revision(store, same, "def authorize(user):\n    return True\n")
    assert len(_record(store, edited, [rule]).prompts) == 1
    assert len(_record(store, same, [rule], model="other-model").prompts) == 1


def _record(store: ReviewStore, revision: ReviewRevision, rules: list[ReviewRule], **kwargs: Any) -> _Model:
    model = _Model()
    _run(store, revision, rules, model, **kwargs)
    return model


def test_cache_key_is_rule_input_and_route() -> None:
    key = rule_check_cache_key("rule-digest", "input-digest", "openai", "review-model")
    assert key == rule_check_cache_key("rule-digest", "input-digest", "openai", "review-model")
    assert key.startswith("rulecheck_")
    for changed in (
        ("rule-digest-2", "input-digest", "openai", "review-model"),
        ("rule-digest", "input-digest-2", "openai", "review-model"),
        ("rule-digest", "input-digest", "anthropic", "review-model"),
        ("rule-digest", "input-digest", "openai", "other-model"),
    ):
        assert rule_check_cache_key(*changed) != key, changed


def test_deterministic_findings_are_context_and_never_re_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, revision = _store(tmp_path)
    rule = _rule()
    static_ref = "static_finding:semgrep-missing-authz"
    monkeypatch.setattr(
        static_findings_module,
        "investigator_static_context",
        lambda _store, _revision, paths: (
            [(static_ref, "Static finding (deterministic) semgrep missing-authz", "src/auth.py")],
            [],
        ),
    )
    restated = _Model(lambda blocks: _violated(rule, [static_ref]))
    run = _run(store, revision, [rule], restated)
    registry = json.dumps(restated.prompts[0]["evidence_registry"])
    assert static_ref in registry and restated.prompts[0]["evidence_registry"]["authority"].startswith("data:")
    # Re-reporting the deterministic finding is not a new finding.
    assert run.findings == []
    assert (run.states[0]["answer"], run.states[0]["reason"]) == (
        "violated",
        "the answer only re-reports deterministic static findings",
    )
    # Cited alongside the exact source that shows the violation, it is evidence.
    store2, revision2 = _store(tmp_path / "second")
    added = _run(store2, revision2, [rule], _Model(lambda blocks: _violated(rule, [static_ref, "target:target-auth"])))
    assert len(added.findings) == 1 and static_ref in added.findings[0].evidence_refs


def _investigated_hypotheses(tmp_path: Path, rule: ReviewRule, covered: tuple[str, ...]) -> tuple[list[Any], Any]:
    store, revision = _store(tmp_path)
    hypotheses: list[Any] = []

    def execute(**kwargs: Any) -> StructuredExecution:
        if kwargs["output_schema"] is RULE_CHECK_SCHEMA:
            return _Model()(**kwargs)
        assert kwargs["output_schema"] is INVESTIGATION_SCHEMA
        hypotheses.append(json.loads(kwargs["prompt"])["hypothesis"])
        return _execution(
            {
                "status": "not_confirmed",
                "category": "security",
                "target_ids": ["target-auth"],
                "title": "",
                "explanation": "",
                "consequence": "",
                "evidence_refs": [],
            }
        )

    stage = AiStagePlan(
        plan_digest="d" * 64,
        effort="quick",
        investigators=True,
        hypotheses=False,
        status="run",
        reason="",
        rule_checks=covered,
    )
    result = run_independent_review(
        store=store,
        revision=revision,
        targets=(_target(),),
        repo_root=tmp_path,
        effort="quick",
        driver="openai",
        model="review-model",
        rules=(rule,),
        structured_executor=execute,
        stage_plan=stage,
    )
    return hypotheses, result


def test_investigators_do_not_re_ask_a_rule_the_rule_checks_ask(tmp_path: Path) -> None:
    rule = _rule()
    uncovered, _ = _investigated_hypotheses(tmp_path / "a", rule, ())
    # Without a rule check, the rule is one of the investigated hypotheses.
    assert any(rule.ref in json.dumps(row) for row in uncovered)

    covered, result = _investigated_hypotheses(tmp_path / "b", rule, (rule.ref,))
    # With one, the rule is asked once by its rule check and no investigator asks it again
    # (it stays context in trusted_repository_rules, as for every investigation).
    assert covered and not any(rule.ref in json.dumps(row) for row in covered)
    assert "ai.rule_checks" in result.executed_entries
    assert [row["state"] for row in result.rule_checks] == ["answered"]


# --------------------------------------------------------------------------- #
# unavailable route and exhausted budget
# --------------------------------------------------------------------------- #


def test_no_model_route_marks_every_question_unavailable_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, revision = _store(tmp_path)
    rules = [_rule("auth-required"), _rule("audit-log", instruction="Privileged calls are audited.")]

    def no_route(**_: Any) -> Any:
        raise RuntimeError("no provider is configured")

    monkeypatch.setattr(ai_review_module, "select_reviewer_route", no_route)
    model = _Model()
    stage = AiStagePlan(
        plan_digest="d" * 64,
        effort="auto",
        investigators=False,
        hypotheses=False,
        status="run",
        reason="",
        rule_checks=tuple(rule.ref for rule in rules),
    )
    result = run_independent_review(
        store=store,
        revision=revision,
        targets=(_target(),),
        repo_root=tmp_path,
        effort="auto",
        rules=rules,
        structured_executor=model,
        stage_plan=stage,
    )
    assert model.prompts == []
    assert result.status == "degraded"
    assert [row["state"] for row in result.rule_checks] == ["unavailable", "unavailable"]
    assert result.rule_checks[0]["reason"] == "no model route is available: RuntimeError: no provider is configured"
    assert "ai.rule_checks" not in result.executed_entries
    assert result.to_wire()["rule_checks"][0]["state"] == "unavailable"


def test_an_exhausted_review_budget_limits_the_remaining_questions(tmp_path: Path) -> None:
    store, revision = _store(tmp_path)
    rules = [_rule("auth-required"), _rule("audit-log", instruction="Privileged calls are audited.")]
    model = _Model()
    run = _run(store, revision, rules, model, budget=ModelCallBudget(max_review_calls=1))
    assert len(model.prompts) == 1
    assert [row["state"] for row in run.states] == ["answered", "limited_by_budget"]
    assert run.states[1]["reason"].startswith("Limited by budget: ")


def test_all_providers_unavailable_keeps_deterministic_checks_planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import intelligence_runtime
    from tests.gateway.test_review_run_plan_api import _get, _review

    def no_provider(*_: Any, **__: Any) -> Any:
        raise RuntimeError("no API key or CLI driver found")

    monkeypatch.setattr(intelligence_runtime, "resolve_driver_model", no_provider)
    review = _review(tmp_path)
    payload = _get(review, "run-plan").json()
    assert payload["ai_availability"] == {
        "available": False,
        "reason": "no AI provider is configured: no API key or CLI driver found",
        "executor": "local",
    }
    statuses = {row["id"]: row["status"] for row in payload["plan"]["entries"]}
    assert statuses["side_effects.static"] == "run"
    # The plan does not depend on provider availability: deterministic work is planned as always.
    monkeypatch.undo()
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    assert _get(review, "run-plan").json()["plan"]["digest"] == payload["plan"]["digest"]
