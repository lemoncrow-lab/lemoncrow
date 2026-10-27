"""Read-only, evidence-backed test failure deltas from executed check recipes.

Do not conflate an observed failing test with a confirmed code defect. This
projection requires comparable base/head observations on the exact Snapshot;
flaky, quarantined, unrun, and non-comparable cases are never promoted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_MAX_ROWS = 20


def observed_test_regressions(differentials: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for result in differentials:
        if result.get("comparable") is not True or result.get("head_verdict") != "FAIL":
            continue
        recipe = result.get("recipe")
        if not isinstance(recipe, Mapping) or not isinstance(recipe.get("id"), str):
            continue
        evidence_id = result.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            continue
        attention = result.get("attention")
        if not isinstance(attention, list):
            continue
        for finding in attention:
            if not isinstance(finding, Mapping):
                continue
            if (
                finding.get("class") != "introduced_failure"
                or finding.get("base") != "passed"
                or finding.get("head") != "failed"
                or finding.get("quarantine")
            ):
                continue
            subject = str(finding.get("subject") or "")[:250]
            if not subject:
                continue
            rows.append(
                {
                    "recipe_id": recipe["id"][:90],
                    "subject": subject,
                    "test_path": str(finding.get("file") or "")[:240],
                    "differential_evidence_id": evidence_id,
                    "executor_class": str(result.get("executor_class") or ""),
                    "independent_execution": result.get("executor_class") in {"managed_sandbox", "customer_worker"},
                    "scope": "exact_snapshot_base_vs_head",
                    "observation": "passed_on_base_failed_on_head",
                    "limitation": "An observed test regression, not a confirmed code defect.",
                }
            )
    rows.sort(key=lambda row: (row["recipe_id"], row["subject"], row["differential_evidence_id"]))
    return {"schema_version": 1, "rows": rows[:_MAX_ROWS], "omitted": max(0, len(rows) - _MAX_ROWS)}
