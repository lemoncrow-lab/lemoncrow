"""Depth ladder defaults per path and min_effort floors on per-Review requests (P11-ESC items 10, 11)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.capability_catalog import DETECTION_FACTS, DetectedCapabilities
from lemoncrow.pro.capabilities.review.depth_policy import explain_depth_request, path_default_depth
from lemoncrow.pro.capabilities.review.escalation import EscalationFacts
from lemoncrow.pro.capabilities.review.run_plan import (
    BudgetUsage,
    EffectiveRunSettings,
    ManualPick,
    PlanInputs,
    RunPlan,
    SettingOrigin,
    amend_run_plan,
    build_run_plan,
    load_run_plan,
    parse_run_settings,
    resolve_run_settings,
)
from tests.gateway.test_review_run_plan_api import _fake_ai, _post, _review

ORG = SettingOrigin("organization", "organization policy")
REPO = SettingOrigin("trusted_base_repository", "base revision .lemoncrow/review.yaml")


def _settings(org: dict[str, Any] | None = None, repo: dict[str, Any] | None = None) -> EffectiveRunSettings:
    layers = []
    if org is not None:
        layers.append(parse_run_settings(org, origin=ORG, allow_profiles=True))
    if repo is not None:
        layers.append(parse_run_settings(repo, origin=REPO))
    for layer in layers:
        assert layer.errors == (), layer.errors
    return resolve_run_settings(layers)


def _inputs(settings: EffectiveRunSettings, changed: tuple[str, ...], **kwargs: Any) -> PlanInputs:
    return PlanInputs(
        review_id="review-1",
        revision_id="revision-1",
        trigger="open",
        settings=settings,
        detected=DetectedCapabilities(facts=frozenset(DETECTION_FACTS)),
        changed_paths=changed,
        **kwargs,
    )


def _plan(settings: EffectiveRunSettings, *changed: str, **kwargs: Any) -> RunPlan:
    return build_run_plan(_inputs(settings, tuple(changed), **kwargs))


def _depth(plan: RunPlan) -> str:
    entry = plan.entry("ai.investigators")
    assert entry is not None
    return str(entry.depth)


# --------------------------------------------------------------------------- #
# item 10: org/repo/path defaults with explicit origin
# --------------------------------------------------------------------------- #

_PATHS = {
    "path_effort": [
        {"effort": "quick", "paths": ["docs/**"]},
        {"effort": "thorough", "paths": ["src/payments/**"]},
    ]
}


def test_path_defaults_set_the_starting_depth_with_their_origin() -> None:
    settings = _settings(org={"effort": "auto"}, repo=_PATHS)

    docs = _plan(settings, "docs/index.md")
    assert _depth(docs) == "quick"
    assert docs.selection == {
        "source": "preset",
        "name": "quick",
        "base": "quick",
        "path_default": "quick",
        "path_default_paths": "docs/**",
        "path_default_origin": REPO.source,
    }
    payments = _plan(settings, "src/payments/charge.py", "docs/index.md")
    assert _depth(payments) == "thorough"
    assert payments.selection["path_default_origin"] == REPO.source
    # Unlike a floor, a path default is not required work.
    assert not payments.entry("ai.investigators").required  # type: ignore[union-attr]
    # A path no rule matches runs at the layer depth: the layer decided, no path default is shown.
    mixed = _plan(settings, "docs/index.md", "src/app.py")
    assert _depth(mixed) == "auto"
    assert mixed.selection == {"source": "preset", "name": "auto", "base": "auto"}
    assert _plan(_settings(org={"effort": "auto"}), "docs/index.md").selection == mixed.selection


def test_a_later_layer_path_rule_wins_and_floors_still_apply_on_top() -> None:
    settings = _settings(
        org={"path_effort": [{"effort": "deep", "paths": ["src/**"]}]},
        repo={"path_effort": [{"effort": "quick", "paths": ["src/generated/**"]}]},
    )
    generated = _plan(settings, "src/generated/models.py")
    assert (_depth(generated), generated.selection["path_default_origin"]) == ("quick", REPO.source)
    other = _plan(settings, "src/app.py")
    assert (_depth(other), other.selection["path_default_origin"]) == ("deep", ORG.source)

    floored = _settings(
        org={"min_effort": [{"effort": "thorough", "paths": ["src/generated/**"]}]},
        repo={"path_effort": [{"effort": "quick", "paths": ["src/generated/**"]}]},
    )
    row = _plan(floored, "src/generated/models.py").entry("ai.investigators")
    assert row is not None and (row.depth, row.origin, row.required) == ("thorough", "policy_floor", True)


def test_path_defaults_apply_to_presets_only_and_round_trip() -> None:
    custom = _settings(repo={**_PATHS, "effort": "custom", "custom": ["ai.investigators"], "custom_from": "auto"})
    assert _plan(custom, "docs/index.md").selection["source"] == "custom"
    layer = parse_run_settings(_PATHS, origin=REPO)
    assert parse_run_settings(layer.to_document(), origin=REPO) == layer
    wire = _settings(repo=_PATHS).to_wire()["path_effort"]
    assert wire == [
        {"effort": "quick", "paths": ["docs/**"], "origin": REPO.to_wire()},
        {"effort": "thorough", "paths": ["src/payments/**"], "origin": REPO.to_wire()},
    ]
    # Explicit path rules are plan identity.
    assert _settings(repo=_PATHS).digest_document() != _settings().digest_document()
    for bad in (
        [{"effort": "maximum", "paths": ["a/**"]}],
        [{"effort": "quick", "paths": []}],
        [{"effort": "quick", "paths": ["a/**"], "extra": 1}],
        "docs/**",
    ):
        assert parse_run_settings({"path_effort": bad}, origin=REPO).errors, bad


def test_path_default_rule_bounds() -> None:
    from lemoncrow.pro.capabilities.review.depth_policy import parse_path_effort

    rules = [{"effort": "quick", "paths": [f"p{i}/**"]} for i in range(64)]
    errors: list[str] = []
    assert len(parse_path_effort(rules, REPO, errors)) == 64 and errors == []
    assert len(parse_path_effort([*rules, rules[0]], REPO, errors)) == 64
    assert errors == ["path_effort exceeds 64 rules"]
    assert path_default_depth((), "auto", ("a.py",)).depth == "auto"


# --------------------------------------------------------------------------- #
# item 11: a per-Review request raises, never lowers a floor
# --------------------------------------------------------------------------- #

_FLOOR = {"min_effort": [{"effort": "thorough", "paths": ["src/payments/**"]}]}


def _requested(
    settings: EffectiveRunSettings, depth: str, *changed: str, **kwargs: Any
) -> tuple[RunPlan, dict[str, Any]]:
    inputs = _inputs(settings, tuple(changed), **kwargs)
    plan = amend_run_plan(
        build_run_plan(inputs),
        inputs,
        usage=BudgetUsage(),
        manual=[ManualPick("ai.investigators", depth)],  # type: ignore[arg-type]
    )
    return plan, explain_depth_request(depth, plan, settings.floors, inputs.required_scope)


def test_a_request_below_a_floor_is_raised_with_the_floor_and_its_origin() -> None:
    settings = _settings(org=_FLOOR)
    plan, answer = _requested(settings, "quick", "src/payments/charge.py")
    assert _depth(plan) == "thorough"
    assert answer == {
        "entry": "ai.investigators",
        "requested": "quick",
        "effective": "thorough",
        "state": "raised",
        "reason": (
            "raised to thorough: a per-Review request cannot go below the min_effort floor thorough "
            "for src/payments/** (organization policy)"
        ),
    }
    # Raising is allowed and honored.
    deeper, honored = _requested(settings, "deep", "src/payments/charge.py")
    assert _depth(deeper) == "deep" and honored["state"] == "honored"
    # Off the floor's paths the request is the plan's business: no floor reason.
    _plan_other, other = _requested(settings, "quick", "src/app.py")
    assert other["state"] == "raised" and "min_effort floor" not in other["reason"]


def test_a_lower_pick_never_lowers_a_floor_with_or_without_a_persisted_plan() -> None:
    inputs = _inputs(_settings(org=_FLOOR), ("src/payments/charge.py",))
    lower = [ManualPick("ai.investigators", "quick")]
    # First build (no persisted plan yet): the planner's own guard keeps the floor.
    assert _depth(build_run_plan(inputs, manual=lower)) == "thorough"
    # Against a persisted plan: a lower request adds nothing at all.
    persisted = build_run_plan(inputs)
    assert amend_run_plan(persisted, inputs, usage=BudgetUsage(), manual=lower) is persisted


def test_a_request_raised_by_escalation_or_limited_by_budget_says_so() -> None:
    facts = EscalationFacts(security_chains=(("p1", "src/report.py", "py.sqli"),))
    _plan_escalated, escalated = _requested(_settings(), "auto", "src/report.py", escalation=facts)
    assert escalated["state"] == "raised"
    assert escalated["reason"].startswith("raised to thorough: escalated to thorough: security chain")

    budget = _settings(org={"budget": {"per_review_model_calls": 2}})
    _plan_limited, limited = _requested(budget, "deep", "src/app.py")
    assert limited["state"] == "limited_by_budget"
    assert limited["reason"].startswith("Limited by budget")

    off = _settings(org={"capabilities": {"ai": "off"}})
    refused = explain_depth_request("deep", _plan(off, "src/app.py"), off.floors, ("src/app.py",))
    assert refused["state"] == "refused" and refused["effective"] is None


# --------------------------------------------------------------------------- #
# every entry point: local and hosted /ai-review, run-specific preview and confirm
# --------------------------------------------------------------------------- #

_BASE_FLOOR = "review:\n  min_effort:\n    - effort: thorough\n      paths: ['src/**']\n"


def _api_review(tmp_path: Path, **register: Any) -> SimpleNamespace:
    return _review(tmp_path, base_config=_BASE_FLOOR, **register)


def test_local_ai_review_request_below_the_floor_is_raised_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _api_review(tmp_path)
    calls = _fake_ai(monkeypatch)
    response = _post(review, "ai-review", {"trigger": "manual", "effort": "quick"})
    assert response.status_code == 200, response.text
    answer = response.json()["ai_review"]["depth_request"]
    assert (answer["requested"], answer["effective"], answer["state"]) == ("quick", "thorough", "raised")
    assert "min_effort floor thorough for src/** (base revision .lemoncrow/review.yaml)" in answer["reason"]
    # What executes is the raised depth, never the request.
    assert calls[0]["effort"] == calls[0]["stage_plan"].effort == "thorough"
    persisted = load_run_plan(review.store, review.revision_id)
    assert persisted is not None and _depth(persisted) == "thorough"


def test_hosted_ai_review_request_below_the_floor_is_raised_with_the_reason(tmp_path: Path) -> None:
    submitted: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def submitter(*args: Any, **kwargs: Any) -> dict[str, Any]:
        submitted.append((args, kwargs))
        return {"status": "queued"}

    review = _api_review(tmp_path, ai_review_submitter=submitter)
    response = _post(review, "ai-review", {"trigger": "manual", "effort": "quick"})
    assert response.status_code == 200, response.text
    answer = response.json()["ai_review"]["depth_request"]
    assert (answer["effective"], answer["state"]) == ("thorough", "raised")
    assert "min_effort floor thorough" in answer["reason"]
    args, kwargs = submitted[0]
    assert args[2] == "thorough"
    assert RunPlan.from_wire(kwargs["run_plan"]).entry("ai.investigators").depth == "thorough"  # type: ignore[union-attr]


def test_run_specific_preview_and_confirm_explain_a_raised_depth(tmp_path: Path) -> None:
    review = _api_review(tmp_path)
    entries = [{"id": "ai.investigators", "depth": "quick"}]
    preview = _post(review, "run-plan/run-specific", {"entries": entries})
    assert preview.status_code == 200, preview.text
    [answer] = preview.json()["depth_requests"]
    assert (answer["requested"], answer["effective"], answer["state"]) == ("quick", "thorough", "raised")
    assert "min_effort floor" in answer["reason"]
    confirmed = _post(review, "run-plan/run-specific", {"entries": entries, "confirm": True})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["depth_requests"] == [answer]
    assert confirmed.json()["next"] == [{"entry": "ai.investigators", "action": "ai_review", "depth": "thorough"}]
