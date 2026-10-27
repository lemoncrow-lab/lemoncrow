"""Check suggestions preserve the planner's authority; they never schedule execution."""

from __future__ import annotations

from types import SimpleNamespace

from lemoncrow.pro.capabilities.review.check_recommendations import recommend_checks
from lemoncrow.pro.capabilities.review.recipes import CheckRecipe, RecipeCatalog


def _plan(*entries: object) -> SimpleNamespace:
    owned = {entry.id: entry for entry in entries}
    return SimpleNamespace(entry=lambda key: owned.get(key))


def _entry(key: str, *, status: str = "run", required: bool = False, selection: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=key,
        status=status,
        available=True,
        external=False,
        required=required,
        selection=selection,
        executor="local",
    )


def test_recommends_impacted_tests_from_existing_plan_only() -> None:
    recipes = RecipeCatalog(
        recipes=(
            CheckRecipe(id="unit", kind="test", command=("pytest", "{tests}"), origin="config"),
            CheckRecipe(id="lint", kind="lint", command=("ruff", "check", "."), origin="config"),
        )
    )
    selection = {"unit": {"mode": "impact", "tests": ["tests/test_checkout.py"], "coverage": "known"}}
    result = recommend_checks(_plan(_entry("checks.test", selection=selection)), recipes, [])
    assert result["schema_version"] == 1
    assert len(result["recommendations"]) == 1
    row = result["recommendations"][0]
    assert row["entry_id"] == "checks.test"
    assert row["recipe_id"] == "unit"
    assert row["selected_tests_sample"] == ["tests/test_checkout.py"]
    assert row["manual_authorization_required"] is True
    assert row["can_run_now"] is True
    assert "command" not in row and "argv" not in row


def test_failures_first_but_never_override_denied_execution() -> None:
    catalog = RecipeCatalog(
        recipes=(
            CheckRecipe(id="failed", kind="test", command=("pytest",), origin="config"),
            CheckRecipe(id="required", kind="test", command=("pytest",), origin="config"),
        )
    )
    plan = _plan(
        _entry("checks.recipe:failed", status="not_available"),
        _entry("checks.recipe:required", required=True),
    )
    actual = [{"recipe": {"id": "failed"}, "head_verdict": "FAIL"}]
    rows = recommend_checks(plan, catalog, actual)["recommendations"]
    assert [row["recipe_id"] for row in rows] == ["failed", "required"]
    assert rows[0]["can_run_now"] is False
    assert rows[0]["run_plan_status"] == "not_available"
    assert rows[1]["required"] is True


def test_unknown_impact_is_explicit_and_no_unplanned_or_provisional_recipe_can_run() -> None:
    recipe = CheckRecipe(id="unit", kind="test", command=("pytest",), origin="config")
    catalog = RecipeCatalog(recipes=(recipe,))
    assert recommend_checks(_plan(), catalog, [])["recommendations"] == []
    assert (
        recommend_checks(
            _plan(_entry(recipe.entry_id)), RecipeCatalog(recipes=(recipe,), base_state="provisional"), []
        )["recommendations"]
        == []
    )
    row = recommend_checks(
        _plan(_entry(recipe.entry_id, selection={"unit": {"mode": "all", "coverage": "unknown"}})), catalog, []
    )["recommendations"][0]
    assert row["coverage"] == "unknown"
    assert row["selected_test_count"] == 0
    assert row["selection_mode"] == "all"


def test_only_same_recipe_digest_can_suggest_reverification_or_claim_progress() -> None:
    recipe = CheckRecipe(id="unit", kind="test", command=("pytest", "-q"), origin="config")
    catalog = RecipeCatalog(recipes=(recipe,))
    plan = _plan(_entry(recipe.entry_id))
    mismatched = [{"recipe": {"id": "unit", "digest": "recipe:sha256:different"}, "head_verdict": "FAIL"}]
    not_comparable = recommend_checks(plan, catalog, [], previous_results=mismatched)["recommendations"][0]
    assert not_comparable["prior_snapshot_verdict"] == ""
    assert "Previous Snapshot" not in not_comparable["reason"]

    previous = [{"recipe": {"id": "unit", "digest": recipe.digest}, "head_verdict": "FAIL"}]
    candidate = recommend_checks(plan, catalog, [], previous_results=previous)["recommendations"][0]
    assert candidate["prior_snapshot_verdict"] == "FAIL"
    assert "Previous Snapshot had FAIL" in candidate["reason"]
    current_pass = [{"recipe": {"id": "unit", "digest": recipe.digest}, "head_verdict": "PASS"}]
    progress = recommend_checks(plan, catalog, current_pass, previous_results=previous)["recommendations"][0]
    assert "Current Snapshot's check passed" in progress["reason"]
    assert progress["previous_verdict"] == "PASS"


def test_prior_formatter_failure_is_recommended_only_for_comparable_recipe_without_automatic_execution() -> None:
    fmt = CheckRecipe(id="fmt", kind="format", command=("ruff", "format", "--check", "."), origin="config")
    lint = CheckRecipe(id="lint", kind="lint", command=("ruff", "check", "."), origin="config")
    catalog = RecipeCatalog(recipes=(fmt, lint))
    plan = _plan(_entry(fmt.entry_id), _entry(lint.entry_id))
    # A non-test check is never recommended merely because it exists.
    assert recommend_checks(plan, catalog, [])["recommendations"] == []
    old = [{"recipe": {"id": "fmt", "digest": fmt.digest}, "head_verdict": "FAIL"}]
    after_fix = recommend_checks(plan, catalog, [], previous_results=old)["recommendations"]
    assert len(after_fix) == 1
    suggestion = after_fix[0]
    assert suggestion["recipe_id"] == "fmt"
    assert suggestion["prior_snapshot_verdict"] == "FAIL"
    assert suggestion["coverage"] == "not_applicable"
    assert suggestion["can_run_now"] is True
    assert suggestion["manual_authorization_required"] is True
    assert "command" not in suggestion
    changed = [{"recipe": {"id": "fmt", "digest": "not-same"}, "head_verdict": "FAIL"}]
    assert recommend_checks(plan, catalog, [], previous_results=changed)["recommendations"] == []
    # A read-only recommendation cannot override the effective plan.
    denied = recommend_checks(
        _plan(_entry(fmt.entry_id, status="not_available")),
        catalog,
        [],
        previous_results=old,
    )[
        "recommendations"
    ][0]
    assert denied["can_run_now"] is False
