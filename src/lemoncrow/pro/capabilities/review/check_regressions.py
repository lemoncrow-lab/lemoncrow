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
    seen: set[tuple[str, str, str, str]] = set()
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
            subject_value = finding.get("subject")
            if not isinstance(subject_value, str) or not subject_value.strip():
                continue
            subject = subject_value[:250]
            path_value = finding.get("file")
            test_path = path_value[:240] if isinstance(path_value, str) else ""
            identity = (recipe["id"], evidence_id, test_path, subject)
            if identity in seen:
                # One recorded failing test in one immutable differential is
                # one observation, even when a test runner repeats the unit
                # in a noisy report. Never render or score it twice.
                continue
            seen.add(identity)
            rows.append(
                {
                    "recipe_id": recipe["id"][:90],
                    "subject": subject,
                    "test_path": test_path,
                    "differential_evidence_id": evidence_id,
                    "executor_class": str(result.get("executor_class") or ""),
                    # A typed runner, valid differential, signed transport MAC
                    # or even a durable queue receipt does not independently
                    # prove execution of this process. The paired verifier's
                    # Ed25519 attestation is qualified separately. Never borrow
                    # that trust for generic check observations, even if a
                    # worker report self-labels itself signed.
                    "independent_execution": False,
                    "scope": "exact_snapshot_base_vs_head",
                    "observation": "passed_on_base_failed_on_head",
                    "limitation": "An observed test regression, not a confirmed code defect.",
                }
            )
    rows.sort(key=lambda row: (row["recipe_id"], row["subject"], row["differential_evidence_id"]))
    return {"schema_version": 1, "rows": rows[:_MAX_ROWS], "omitted": max(0, len(rows) - _MAX_ROWS)}


def cited_regressions(annotations: Sequence[Any], observed: Mapping[str, Any], *, revision_id: str) -> dict[str, Any]:
    """Read-only exact-Snapshot citation join. A citation is never defect proof.

    A reviewer/AI annotation has to cite BOTH a trusted recipe and the exact
    test id. The backend then joins those refs only to already-observed
    comparable base-PASS/head-FAIL rows from this Snapshot; no model-chosen
    path, quoted prose or unchecked previous Snapshot can manufacture a row.
    Nothing upgrades execution attestation or mutates a finding disposition.
    """
    if not revision_id or observed.get("schema_version") != 1 or not isinstance(observed.get("rows"), list):
        return {"schema_version": 1, "rows": [], "omitted": 0}
    links: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    rows = observed["rows"][:_MAX_ROWS]
    for annotation in annotations:
        if (
            getattr(annotation, "revision_id", None) != revision_id
            or getattr(annotation, "source", None) not in {"ai_review", "human"}
            or getattr(annotation, "parent_id", None)
        ):
            continue
        ident = getattr(annotation, "id", None)
        evidence = getattr(annotation, "evidence", ())
        if not isinstance(ident, str) or not ident or not isinstance(evidence, (tuple, list)):
            continue
        refs = set(value for value in evidence if isinstance(value, str) and len(value) <= 500)
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            recipe, subject, receipt = (
                item.get("recipe_id"),
                item.get("subject"),
                item.get("differential_evidence_id"),
            )
            if (
                not isinstance(recipe, str)
                or not recipe
                or not isinstance(subject, str)
                or not subject
                or not isinstance(receipt, str)
                or not receipt
            ):
                continue
            # Model evidence citations cannot masquerade as executed proof: the
            # separate observed row is the authority for the test result.
            if f"check-recipe:{recipe}" not in refs or f"test:{subject}" not in refs:
                continue
            identity = (ident, recipe, subject, receipt)
            if identity in seen:
                continue
            seen.add(identity)
            links.append(
                {
                    "annotation_id": ident[:100],
                    "source": annotation.source,
                    "recipe_id": recipe,
                    "subject": subject,
                    "differential_evidence_id": receipt,
                    "link_class": "explicit_current_snapshot_test_citation",
                    "independent_execution": False,
                    "confirmed_defect": False,
                    "limitation": "A linked observed test regression does not independently confirm the annotated defect.",
                }
            )
    links.sort(
        key=lambda row: (row["annotation_id"], row["recipe_id"], row["subject"], row["differential_evidence_id"])
    )
    return {"schema_version": 1, "rows": links[:_MAX_ROWS], "omitted": max(0, len(links) - _MAX_ROWS)}
