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
