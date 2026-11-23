"""Review API route for the experimental data path (P10-7).

``POST /reviews/{id}/data-path/run``   run ``data_path.diff`` on one exact Snapshot (planned or picked only)

The stored report reaches the Review's ``/map`` projection as its ``data_path`` field
(:func:`data_path.projection`). Composed into ``register_review_api`` with the same seams as the
check-recipe and artifact-analysis routes (:class:`check_api.CheckRecipeDeps`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

# Module level: FastAPI resolves the (postponed) ``Request`` annotation from these globals.
from fastapi import HTTPException, Request, status

from lemoncrow.pro.capabilities.review.artifact_analysis_api import analysis_budget_seconds
from lemoncrow.pro.capabilities.review.check_api import CheckRecipeDeps
from lemoncrow.pro.capabilities.review.data_path import (
    DEFAULT_BOUNDS,
    ENTRY,
    not_available,
    run_data_path,
    setup_hint,
    store_report,
)

if TYPE_CHECKING:
    from fastapi import APIRouter


def register_data_path_routes(
    router: APIRouter, deps: CheckRecipeDeps, *, side_tree: Callable[[Any, str], Path]
) -> None:
    """*side_tree* materializes one Snapshot side (``old``/``new``) from the Review store, never the working tree."""

    from starlette.concurrency import run_in_threadpool

    store = deps.store
    run_plans = deps.run_plans

    @router.post("/reviews/{review_id}/data-path/run")
    async def _post_data_path_run(review_id: str, request: Request) -> dict[str, Any]:
        """Run the data path on one exact Snapshot, only as the effective run plan schedules it."""

        import platform

        from lemoncrow.pro.capabilities.review.planned_execution import (
            PlannedExecutionRefused,
            admit_planned_execution,
            record_execution_compute,
        )
        from lemoncrow.pro.capabilities.review.run_plan import ManualPick
        from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable

        session = deps.mutable_session(review_id)
        body = await deps.json_object(request)
        requested = str(body.get("revision_id") or "").strip()
        if not requested:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="the data path runs on an exact revision_id (revision)",
            )
        revision = deps.requested_revision(session, requested)
        current = revision.id == deps.latest(session).id
        pick = body.get("pick") is True

        def run() -> dict[str, Any]:
            def candidates() -> int:
                return deps.ai_candidates(session, revision)

            if pick and session.status != "archived" and current:
                # A reviewer's pick is an add-only amendment; it never removes required work.
                plan = run_plans.amend(session, revision, [ManualPick(ENTRY)], ai_candidates=candidates)
            else:
                plan = run_plans.current(session, revision, ai_candidates=candidates, persist=current)
            runners = deps.runners()
            run_on = str(getattr(runners, "run_on", "") or "local")
            try:
                entry = admit_planned_execution(plan, ENTRY, run_on=run_on)
            except PlannedExecutionRefused as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"{ENTRY} is not in the effective run plan: {exc}",
                ) from exc
            bounds = DEFAULT_BOUNDS
            budget = analysis_budget_seconds(plan, entry)
            if budget is not None:
                # The per-Review ceiling: whatever it leaves is the producer's total time cap (named when hit).
                total = max(0, min(bounds.total_seconds, int(budget)))
                bounds = replace(bounds, total_seconds=total)
            started = time.monotonic()
            try:
                old_tree = side_tree(revision, "old")
                new_tree = side_tree(revision, "new")
            except ReviewSnapshotUnavailable:
                # Its message can quote a repository path: the stored report names only that it happened.
                report = not_available(
                    "the Snapshot's source trees are not readable",
                    setup=setup_hint(platform.system(), platform.machine()),
                    bounds=bounds,
                )
            else:
                report = run_data_path(
                    old_tree, new_tree, changed_paths=tuple(deps.changed_paths(revision)), bounds=bounds
                )
            wall = time.monotonic() - started
            store_report(store, revision, report)
            record_execution_compute(
                run_plans.compute.recorder(session.id, revision.id),
                entry_id=ENTRY,
                run_on=run_on,
                execution=None,
                wall_seconds=wall,
                fallback_stage="data_path",
            )
            return {
                **report,
                "run_plan_digest": plan.digest,
                "plan_entry": entry.to_wire(),
                "run_on": run_on,
                "wall_seconds": round(wall, 3),
            }

        result = cast(dict[str, Any], await run_in_threadpool(run))
        await deps.enforce_armed_landing(session)
        return result


__all__ = ["register_data_path_routes"]
