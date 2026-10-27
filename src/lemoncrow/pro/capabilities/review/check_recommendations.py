"""Explain which existing trusted-base checks deserve attention for a Snapshot.

This module never runs code or changes the effective run plan. Recommendations
reference *only* admitted recipe entry IDs, the planner's bounded impact map,
and observed verdicts. A candidate is neither a verified finding nor authority
to execute head-authored commands.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_MAX_CANDIDATES = 12
_MAX_TEST_LABELS = 3


def recommend_checks(
    plan: Any, catalog: Any, results: Sequence[Mapping[str, Any]], *, previous_results: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """Read-only, deterministic queue; unknown impact must never claim coverage."""
    result_by_recipe = {
        str(result.get("recipe", {}).get("id") or ""): result
        for result in results
        if isinstance(result.get("recipe"), Mapping)
    }
    previous_by_recipe = {
        str(result.get("recipe", {}).get("id") or ""): result
        for result in previous_results
        if isinstance(result.get("recipe"), Mapping)
    }
    suggested: list[dict[str, Any]] = []
    for recipe in catalog.recipes:
        if recipe.kind != "test":
            continue
        entry = plan.entry(recipe.entry_id) or plan.entry(recipe.kind_entry)
        if entry is None:
            # A recipe that cannot be named in the effective plan is not a
            # recommendation, even when it exists in the trusted base.
            continue
        if getattr(catalog, "base_state", "present") == "provisional" or bool(catalog.errors):
            continue
        selected = entry.selection.get(recipe.id) if isinstance(entry.selection, Mapping) else None
        if not isinstance(selected, Mapping):
            selected = {}
        selection_mode = str(selected.get("mode") or "all")
        raw_tests = selected.get("tests")
        tests: list[str] = [item for item in raw_tests if isinstance(item, str)] if isinstance(raw_tests, list) else []
        chosen = tests[:_MAX_TEST_LABELS]
        coverage = str(selected.get("coverage") or "known")
        observed = result_by_recipe.get(recipe.id)
        last_verdict = str(observed.get("head_verdict") or "") if observed else ""
        runnable = entry.status == "run" and entry.available and not entry.external
        failure = last_verdict in {"FAIL", "UNKNOWN"}
        previous = previous_by_recipe.get(recipe.id)
        prior_verdict = str(previous.get("head_verdict") or "") if previous else ""
        previous_failure = not last_verdict and prior_verdict in {"FAIL", "UNKNOWN"}
        # This is a suggestion to *inspect* first, not a new execution grant.
        if not runnable and not failure and not previous_failure:
            continue
        if failure:
            reason = f"Previous execution {last_verdict}; inspect its failure and rerun only with explicit approval"
            order = 0
        elif previous_failure:
            reason = f"Previous Snapshot had {prior_verdict}; this Snapshot has no corresponding check result"
            order = 1
        elif entry.required:
            reason = "Required by the trusted Review policy"
            order = 2
        elif selection_mode == "impact" and chosen:
            reason = f"Planner selected impacted tests ({len(tests)} recorded)"
            order = 3
        elif selection_mode == "none":
            continue
        else:
            reason = "Available trusted-base test; scope is broad or unknown"
            order = 4
        suggested.append(
            {
                "entry_id": entry.id,
                "recipe_id": recipe.id,
                "title": (recipe.title or recipe.id)[:140],
                "reason": reason,
                "run_plan_status": entry.status,
                "previous_verdict": last_verdict,
                "prior_snapshot_verdict": prior_verdict,
                "required": bool(entry.required),
                "can_run_now": runnable,
                "manual_authorization_required": True,
                "selection_mode": selection_mode if selection_mode in {"impact", "all", "none"} else "all",
                "selected_tests_sample": chosen,
                "selected_test_count": len(tests) if selection_mode == "impact" else 0,
                "coverage": "unknown" if coverage == "unknown" else "known",
                "executor": entry.executor,
                "recipe_digest": recipe.digest,
                "_rank": order,
            }
        )
    suggested.sort(key=lambda item: (item["_rank"], item["entry_id"]))
    for item in suggested:
        item.pop("_rank", None)
    return {
        "schema_version": 1,
        "recommendations": suggested[:_MAX_CANDIDATES],
        "omitted": max(0, len(suggested) - _MAX_CANDIDATES),
        "policy": "Suggestions are read-only and cannot bypass run-plan, sandbox, approvals or budgets.",
    }
