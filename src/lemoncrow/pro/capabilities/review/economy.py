"""Compute saved versus spent per Review (Phase 9R R6).

What the execution economy avoided, next to what the Review spent:

* ``base_shared`` -- a base run reused from another Review on the same base
  commit, environment and recipe (item 21); measured: the original run's time.
* ``carried_forward`` -- a head run reused from an earlier Snapshot whose inputs
  did not change (item 22); measured: the original run's time.
* ``ci_reuse`` -- an entry an equivalent current-Snapshot CI or LemonCrow result
  satisfies (item 23); estimated: the entry's planned minutes.
* ``impact_selection`` -- test files impact selection left out (item 19);
  estimated: the entry's planned minutes times the share of test files not run.

Measured rows live in ``review_derived`` (kind ``compute_saved``, one row per
entry and reason) and are deleted with their Snapshot. Estimated rows are
derived from the persisted run plan on read. A base run left out because the
head passed (item 20) is not counted: nothing measured what it would cost.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.store import DerivedResult

SAVED_KIND: Final[str] = "compute_saved"
_STAMP: Final[str] = "v1"
SavedReason = Literal["base_shared", "carried_forward", "ci_reuse", "impact_selection"]
MEASURED_REASONS: Final[tuple[str, ...]] = ("base_shared", "carried_forward")
_LABELS: Final[Mapping[str, str]] = {
    "base_shared": "Base result shared from another Review",
    "carried_forward": "Unchanged since an earlier revision",
    "ci_reuse": "Satisfied by an equivalent CI result",
    "impact_selection": "Tests the change cannot affect",
}


class SavingsLedger:
    """Measured compute avoided, per Snapshot, entry and reason."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def record(self, *, review_id: str, revision_id: str, entry_id: str, reason: str, seconds: float) -> None:
        if reason not in MEASURED_REASONS:
            raise ValueError(f"{reason!r} is not a measured saving")
        value = float(seconds)
        if not math.isfinite(value) or value < 0:
            raise ValueError("saved seconds must be a finite, non-negative number")
        # One additive store-side write (``ReviewStore.accumulate_derived_result``):
        # hosted replicas sharing one PostgreSQL authority neither lose nor
        # double-count a saving, with no per-process lock.
        self._store.accumulate_derived_result(
            DerivedResult(
                review_id=review_id,
                revision_id=revision_id,
                kind=SAVED_KIND,
                scope_key=f"{entry_id}|{reason}",
                stamp=_STAMP,
                payload={"entry_id": entry_id, "reason": reason},
            ),
            increments={"seconds": value, "runs": 1},
        )

    def rows(self, revision_ids: Iterable[str]) -> dict[tuple[str, str], tuple[float, int]]:
        """(entry, reason) -> (seconds, runs) summed over these Snapshots."""

        out: dict[tuple[str, str], tuple[float, int]] = {}
        for (_revision, _scope), (stamp, payload) in self._store.derived_results(
            SAVED_KIND, list(revision_ids)
        ).items():
            if stamp != _STAMP:
                continue
            key = (str(payload.get("entry_id") or ""), str(payload.get("reason") or ""))
            seconds, runs = out.get(key, (0.0, 0))
            out[key] = (seconds + float(payload.get("seconds") or 0.0), runs + int(payload.get("runs") or 0))
        return out


def _plan_estimates(plan: Any) -> dict[tuple[str, str], tuple[float, int]]:
    out: dict[tuple[str, str], tuple[float, int]] = {}
    for entry in getattr(plan, "entries", ()):
        minutes = float(getattr(entry, "estimated_minutes", 0.0) or 0.0)
        if entry.status == "satisfied":
            out[(entry.id, "ci_reuse")] = (minutes * 60.0, 1)
            continue
        selection = getattr(entry, "selection", None)
        recipes = selection.get("recipes") if isinstance(selection, Mapping) else None
        if not isinstance(recipes, Mapping) or not recipes:
            continue
        shares: list[float] = []
        for row in recipes.values():
            if not isinstance(row, Mapping):
                continue
            mode = row.get("mode")
            total = int(row.get("total") or 0)
            if mode == "none":
                shares.append(1.0)
            elif mode == "impact" and total > 0:
                shares.append(max(0.0, 1.0 - len(row.get("tests") or ()) / total))
            else:
                shares.append(0.0)
        share = sum(shares) / len(shares) if shares else 0.0
        if share > 0 and entry.status in {"run", "skipped"}:
            out[(entry.id, "impact_selection")] = (minutes * share * 60.0, 1)
    return out


def economy_projection(
    plan: Any, measured: Mapping[tuple[str, str], tuple[float, int]], *, spent_minutes: float
) -> dict[str, Any]:
    """Saved versus spent for one Review: measured rows plus the current plan's estimates."""

    rows: list[dict[str, Any]] = []
    totals = {"measured": 0.0, "estimated": 0.0}
    estimated = _plan_estimates(plan)
    for (entry_id, reason), (seconds, runs) in sorted({**measured, **estimated}.items()):
        basis = "measured" if reason in MEASURED_REASONS else "estimated"
        totals[basis] += seconds
        rows.append(
            {
                "entry_id": entry_id,
                "reason": reason,
                "label": _LABELS.get(reason, reason),
                "basis": basis,
                "minutes": round(seconds / 60.0, 3),
                "runs": runs,
            }
        )
    return {
        "spent_minutes": round(max(0.0, spent_minutes), 3),
        "saved_minutes": round((totals["measured"] + totals["estimated"]) / 60.0, 3),
        "saved_measured_minutes": round(totals["measured"] / 60.0, 3),
        "saved_estimated_minutes": round(totals["estimated"] / 60.0, 3),
        "rows": rows,
    }


__all__ = ["MEASURED_REASONS", "SAVED_KIND", "SavedReason", "SavingsLedger", "economy_projection"]
