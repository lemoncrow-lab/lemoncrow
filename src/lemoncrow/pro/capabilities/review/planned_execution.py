"""Run-plan admission and compute reporting for hermetic executions.

The effective run plan governs what executes. A hermetic executor (author
machine, customer worker, managed sandbox) runs a catalog entry only when the
Snapshot's persisted plan schedules it (``run``) on that executor's own locus:
``author_machine`` runs ``local`` entries, ``customer_worker`` ``worker``
entries and ``managed_sandbox`` ``managed`` entries. A hosted dispatcher states
its locus explicitly (``RunnerRegistry.run_on``), because a customer-operated
deployment's ``managed`` plane is customer compute. Anything else -- no plan, a
tampered plan, an entry the plan skips or places elsewhere -- is refused.

In-process executions report per-stage compute (fetch/build/run from the
execution contract) through the planner's ``ComputeRecorder``. Hosted jobs are
metered from their durable leases (``compute_usage``) and are never reported
here, so no execution is counted twice.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import Any, Final

from lemoncrow.pro.capabilities.review.capability_catalog import DetectedCapabilities, DynamicTarget, valid_entry_id
from lemoncrow.pro.capabilities.review.compute_ledger import ComputeRecorder
from lemoncrow.pro.capabilities.review.run_plan import (
    ManualPick,
    PlanEntry,
    RunPlan,
    check_catalog_id,
    load_run_plan,
)
from lemoncrow.pro.environment.execution import ExecutionResult

# Wall time outside the contract's stages (Snapshot materialization, dispatch).
SETUP_STAGE: Final[str] = "setup"
# Reviewer-opened web previews render base-vs-head screenshots.
WEB_PREVIEW_ENTRY: Final[str] = "visual.screenshot"


class PlannedExecutionRefused(ValueError):
    """The effective run plan does not schedule this execution on this executor."""


def surface_entry_id(surface: Any) -> str:
    """The run-plan catalog entry of a trusted-base runtime surface."""

    metadata = getattr(surface, "metadata", None) or {}
    return f"surface:{metadata.get('surface_id') or surface.id!s}"


def discovered_surface_entry_id(surface: Any) -> str:
    """``surface:<provider>:<id>`` for a surface the trusted base does not configure.

    An identifier outside the catalog's ID alphabet is replaced by a stable
    digest of the provider and surface ID.
    """

    provider, surface_id = str(surface.provider), str(surface.id)
    entry_id = f"surface:{provider}:{surface_id}"
    if valid_entry_id(entry_id):
        return entry_id
    digest = hashlib.sha256(f"{provider}\0{surface_id}".encode()).hexdigest()[:24]
    return f"surface:discovered-{digest}"


def surface_pick(surface: Any, detected: DetectedCapabilities, *, executable: bool) -> ManualPick:
    """The manual run-plan pick a reviewer's request for *surface* adds.

    A surface the trusted base configures (directly, or as the parent of a
    discovered ID) picks that catalog entry. Any other surface discovered on
    the Snapshot is added as its own ``surface:<provider>:<id>`` pick.
    """

    configured = surface_entry_id(surface)
    if detected.dynamic(configured) is not None:
        return ManualPick(configured)
    mapped = check_catalog_id(f"surface:{surface.provider}:{surface.id}", detected)
    if mapped is not None and mapped.startswith("surface:") and detected.dynamic(mapped) is not None:
        return ManualPick(mapped)
    entry_id = discovered_surface_entry_id(surface)
    title = str(getattr(surface, "title", "") or "") or f"{surface.provider}:{surface.id}"
    return ManualPick(
        entry_id,
        target=DynamicTarget(id=entry_id[len("surface:") :], title=title[:200], executable=executable),
    )


def persisted_plan(store: Any, revision_id: str) -> RunPlan | None:
    """The Snapshot's persisted effective plan; unreadable or tampered reads as none."""

    try:
        return load_run_plan(store, revision_id)
    except (ValueError, KeyError, TypeError):
        return None


def admit_planned_execution(plan: RunPlan | None, entry_id: str, *, run_on: str) -> PlanEntry:
    """The plan entry that authorizes this execution; refuses anything else."""

    if plan is None:
        raise PlannedExecutionRefused("the revision has no persisted effective run plan")
    entry = plan.entry(entry_id)
    if entry is None:
        raise PlannedExecutionRefused("not addressable in the effective run plan")
    if not plan.runs(entry_id):
        raise PlannedExecutionRefused(entry.reason or f"planned as {entry.status}")
    if entry.executor != run_on:
        raise PlannedExecutionRefused(f"planned on the {entry.executor} executor, not {run_on}")
    return entry


def stage_compute(
    execution: Mapping[str, Any] | None, *, wall_seconds: float, fallback_stage: str
) -> tuple[tuple[str, float], ...]:
    """Seconds per stage of one execution, summing to its wall time.

    Typed results report their fetch/build/run stages; the rest of the wall
    time is ``setup``. An untyped run is one *fallback_stage* row.
    """

    wall = float(wall_seconds) if math.isfinite(float(wall_seconds)) else 0.0
    wall = max(0.0, wall)
    parsed: ExecutionResult | None = None
    if isinstance(execution, Mapping):
        try:
            parsed = ExecutionResult.from_wire(execution)
        except (TypeError, ValueError):
            parsed = None
    if parsed is None or not parsed.stages:
        return ((fallback_stage, wall),)
    totals: dict[str, float] = {}
    for stage in parsed.stages:
        totals[stage.stage] = totals.get(stage.stage, 0.0) + max(0, int(stage.duration_ms)) / 1000.0
    remainder = wall - sum(totals.values())
    if remainder > 0:
        totals[SETUP_STAGE] = remainder
    return tuple(totals.items())


def record_execution_compute(
    recorder: ComputeRecorder,
    *,
    entry_id: str,
    run_on: str,
    execution: Mapping[str, Any] | None,
    wall_seconds: float,
    fallback_stage: str,
) -> None:
    """Report one in-process execution's compute per stage; zero-time stages are not runs."""

    for stage, seconds in stage_compute(execution, wall_seconds=wall_seconds, fallback_stage=fallback_stage):
        if seconds > 0:
            recorder.record_compute(entry_id, run_on, stage, seconds)


def execution_wire(record: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The typed execution result embedded in a run/validation record, if any."""

    data = record.get("data") if isinstance(record, Mapping) else None
    execution = data.get("execution") if isinstance(data, Mapping) else None
    return execution if isinstance(execution, Mapping) else None


__all__ = [
    "SETUP_STAGE",
    "WEB_PREVIEW_ENTRY",
    "PlannedExecutionRefused",
    "admit_planned_execution",
    "discovered_surface_entry_id",
    "execution_wire",
    "persisted_plan",
    "record_execution_compute",
    "stage_compute",
    "surface_entry_id",
    "surface_pick",
]
