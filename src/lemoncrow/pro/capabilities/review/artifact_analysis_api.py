"""Review API routes for the artifact and infrastructure analyses (Phase 9R items 15, 31, 32, 33).

``GET  /reviews/{id}/artifact-analyses``       applicable analyses, plan status, latest results
``POST /reviews/{id}/artifact-analyses/run``   execute one admitted analysis entry (planned only)

Composed into ``register_review_api`` with the same seams as the check-recipe
routes (:class:`check_api.CheckRecipeDeps`); nothing here reaches around the
Review's trust boundary or the effective run plan.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

# Module level: FastAPI resolves the (postponed) ``Request`` annotation from these globals.
from fastapi import HTTPException, Request, status

from lemoncrow.pro.capabilities.review import request_reads
from lemoncrow.pro.capabilities.review.analysis_common import (
    ANALYSIS_ENTRIES,
    ANALYSIS_MEDIA_TYPE,
    CONFIG_KEYS,
    LOCAL_REPORT_LIMIT,
    REPORT_LIMIT,
)
from lemoncrow.pro.capabilities.review.artifact_analyses import (
    AnalysisRunInputs,
    AnalysisRunSuperseded,
    entry_title,
    is_analysis_entry,
    latest_analyses,
    read_analysis_record,
    run_analysis_entry,
)
from lemoncrow.pro.capabilities.review.check_api import CheckRecipeDeps, rerun_budget_seconds
from lemoncrow.pro.environment.execution import ExecutorClass

if TYPE_CHECKING:
    from fastapi import APIRouter

_ARTIFACT_VIEW_BYTES = 256 * 1024


def analysis_budget_seconds(plan: Any, entry: Any) -> float | None:
    """Seconds this entry may spend: its planned estimate plus what the per-Review ceiling has left.

    ``None`` without a ``per_review_minutes`` ceiling (no budget to exhaust).
    """

    slack = rerun_budget_seconds(plan)
    if slack is None:
        return None
    return float(getattr(entry, "estimated_minutes", 0.0) or 0.0) * 60.0 + slack


def register_artifact_analysis_routes(router: APIRouter, deps: CheckRecipeDeps) -> None:
    from starlette.concurrency import run_in_threadpool

    from lemoncrow.pro.capabilities.review.surfaces import ReviewSurfaceConfig

    store = deps.store
    run_plans = deps.run_plans

    def _entries(plan: Any, catalog: Any) -> list[dict[str, Any]]:
        """Analyses with something to act on; an inapplicable entry is not shown at all."""

        out: list[dict[str, Any]] = []
        for entry_id in ANALYSIS_ENTRIES:
            specs = catalog.for_entry(entry_id)
            entry = plan.entry(entry_id)
            if not specs or (entry is not None and entry.status == "inapplicable"):
                continue
            out.append(
                {
                    "id": entry_id,
                    "title": entry_title(entry_id),
                    "config_key": CONFIG_KEYS[entry_id],
                    "status": entry.status if entry is not None else "not_planned",
                    "reason": entry.reason if entry is not None else "not in the effective run plan",
                    "sides": entry.sides if entry is not None else "",
                    "required": bool(entry.required) if entry is not None else False,
                    "setup_hint": entry.setup_hint if entry is not None else "",
                    "configured": entry_id in catalog.configured_entries,
                    "specs": [spec.to_wire() for spec in specs],
                }
            )
        return out

    @router.get("/reviews/{review_id}/artifact-analyses")
    async def _get_artifact_analyses(review_id: str, revision_id: str = "") -> dict[str, Any]:
        """Trusted-base analyses, their plan status and the latest results on one exact Snapshot."""

        session = deps.session(review_id)
        latest = deps.latest(session)
        revision = deps.requested_revision(session, revision_id) if revision_id else latest

        def build() -> dict[str, Any]:
            with request_reads.reads_once():
                catalog = run_plans.analysis_catalog(revision)
                plan = run_plans.peek(session, revision)
            entries = _entries(plan, catalog)
            shown = {row["id"] for row in entries}
            records, verdicts = latest_analyses(store, revision)
            return {
                "revision_id": revision.id,
                "historical": revision.id != latest.id,
                "run_plan_digest": plan.digest,
                "base_state": catalog.base_state,
                "errors": list(catalog.errors),
                "entries": entries,
                "results": [records[key] for key in sorted(records) if key[0] in shown],
                "verdicts": [verdicts[key] for key in sorted(verdicts) if key in shown],
            }

        return cast(dict[str, Any], await run_in_threadpool(build))

    @router.get("/reviews/{review_id}/artifact-analyses/artifact")
    async def _get_artifact_analysis_artifact(
        review_id: str, evidence_id: str, name: str, revision_id: str = ""
    ) -> dict[str, Any]:
        """One redacted heavy artifact (rendered manifest, plan, schema diff) an analysis record cites."""

        session = deps.session(review_id)
        revision = deps.requested_revision(session, revision_id) if revision_id else deps.latest(session)

        def read() -> dict[str, Any]:
            item = store.get_evidence(evidence_id)
            if (
                item is None
                or item.review_id != session.id
                or item.revision_id != revision.id
                or item.mime_type != ANALYSIS_MEDIA_TYPE
            ):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no analysis record on this revision")
            record = read_analysis_record(store, item)
            rows = record.get("artifacts") if isinstance(record, Mapping) else None
            row = next(
                (row for row in rows or () if isinstance(row, Mapping) and row.get("name") == name),
                None,
            )
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="the analysis cites no such artifact")
            opener = getattr(store, "run_artifacts", None)
            artifacts = opener() if callable(opener) else None
            payload = artifacts.read(session.id, str(row.get("digest") or "")) if artifacts is not None else None
            if payload is None:
                return {"name": name, "expired": True, "text": "", "truncated": False, "bytes": 0}
            # Stored already redacted (R4); the view is bounded.
            text = payload[:_ARTIFACT_VIEW_BYTES].decode("utf-8", "replace")
            return {
                "name": name,
                "expired": False,
                "text": text,
                "truncated": len(payload) > _ARTIFACT_VIEW_BYTES,
                "bytes": len(payload),
                "digest": row.get("digest"),
            }

        return cast(dict[str, Any], await run_in_threadpool(read))

    @router.post("/reviews/{review_id}/artifact-analyses/run")
    async def _post_artifact_analyses_run(review_id: str, request: Request) -> dict[str, Any]:
        """Execute one analysis entry on one exact Snapshot, only as the effective run plan schedules it."""

        from lemoncrow.pro.capabilities.review.hermetic import load_hermetic_policy
        from lemoncrow.pro.capabilities.review.planned_execution import (
            PlannedExecutionRefused,
            admit_planned_execution,
            record_execution_compute,
        )
        from lemoncrow.pro.capabilities.review.run_evidence import DEFAULT_RAW_RETENTION_SECONDS, RedactionPolicy
        from lemoncrow.pro.capabilities.review.run_plan import ManualPick
        from lemoncrow.pro.capabilities.review.runner_host import revision_snapshot_identity
        from lemoncrow.pro.capabilities.review.runtime_validation import trusted_policy_digest

        session = deps.mutable_session(review_id)
        body = await deps.json_object(request)
        requested = str(body.get("revision_id") or "").strip()
        if not requested:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="artifact analyses run on an exact revision_id (revision)",
            )
        revision = deps.requested_revision(session, requested)
        current = revision.id == deps.latest(session).id
        entry_id = str(body.get("entry") or "").strip()
        if not is_analysis_entry(entry_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"entry must be one of {', '.join(ANALYSIS_ENTRIES)}",
            )
        raw_specs = body.get("specs")
        spec_ids = tuple(str(item) for item in raw_specs[:16]) if isinstance(raw_specs, list) else ()
        pick = body.get("pick") is True

        def run() -> dict[str, Any]:
            def candidates() -> int:
                return deps.ai_candidates(session, revision)

            if pick and session.status != "archived" and current:
                # A reviewer's pick is an add-only amendment; it never removes required work.
                plan = run_plans.amend(session, revision, [ManualPick(entry_id)], ai_candidates=candidates)
            else:
                plan = run_plans.current(session, revision, ai_candidates=candidates, persist=current)
            runners = deps.runners()
            run_on = str(getattr(runners, "run_on", "") or "local")
            try:
                entry = admit_planned_execution(plan, entry_id, run_on=run_on)
            except PlannedExecutionRefused as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"{entry_id} is not in the effective run plan: {exc}",
                ) from exc
            catalog = run_plans.analysis_catalog(revision)
            if catalog.base_state == "provisional":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="the trusted base is not readable yet, so no analysis can be resolved",
                )
            authority = deps.review_config(revision)
            try:
                policy = load_hermetic_policy(str(getattr(authority, "content", "") or ""))
                redaction, retention = policy.redaction, policy.retention_seconds
            except ValueError:
                # An unreadable execution block still redacts with the built-in rules.
                redaction, retention = RedactionPolicy(), DEFAULT_RAW_RETENTION_SECONDS
            executor_class = getattr(runners, "executor_class", ExecutorClass.AUTHOR_MACHINE)
            recorder = run_plans.compute.recorder(session.id, revision.id)

            def run_side(runtime: Any, surface: Any, side: str) -> Any:
                _revision, _registry, side_runners, context = deps.surface_runtime(
                    session.id, revision.id, side=side, immutable_execution=True
                )
                # The synthetic analysis runtime is the only one: head config can add none.
                context = replace(context, config=ReviewSurfaceConfig(runtimes=(runtime,)))
                return side_runners.run(context, surface, side=side, cleanup_workspace=True)

            def meter(execution: Mapping[str, Any] | None, wall: float) -> None:
                record_execution_compute(
                    recorder,
                    entry_id=entry_id,
                    run_on=run_on,
                    execution=execution,
                    wall_seconds=wall,
                    fallback_stage="artifact_analysis",
                )

            inputs = AnalysisRunInputs(
                store=store,
                revision=revision,
                catalog=catalog,
                entry_id=entry_id,
                sides=("old", "new") if entry.sides == "base+head" else ("new",),
                executor_class=executor_class,
                policy_digest=trusted_policy_digest(authority.content, base_known=authority.base_known),
                changed_paths=tuple(deps.changed_paths(revision)),
                read_base=lambda path: deps.read_side(revision, path, "old"),
                read_side=lambda path, side: deps.read_side(revision, path, side),
                base_paths=lambda: run_plans.base_paths(revision),
                depth=str(getattr(entry, "depth", "") or ""),
                # Read as the requester: an unreadable member makes the pairing not_available.
                set_heads=lambda: deps.change_set_heads(session),
                base_identity=revision_snapshot_identity(revision, "old"),
                # The local runner reads 4 MiB reports; hosted executors cap them lower.
                report_limit=LOCAL_REPORT_LIMIT if run_on == "local" else REPORT_LIMIT,
                redaction=redaction,
                retention_seconds=retention,
                budget_seconds=analysis_budget_seconds(plan, entry),
                expected_current_revision_id=revision.id if current else "",
                execution_intent="current" if current else "explicit_historical",
                spec_ids=spec_ids,
            )
            started = time.monotonic()
            try:
                report = run_analysis_entry(inputs, run_side, meter)
            except AnalysisRunSuperseded as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"the Review advanced during the analysis; results were not published as current: {exc}",
                ) from exc
            return {
                **report.to_wire(),
                "run_plan_digest": plan.digest,
                "plan_entry": entry.to_wire(),
                "executor_class": executor_class.value,
                "run_on": run_on,
                "wall_seconds": round(time.monotonic() - started, 3),
            }

        result = cast(dict[str, Any], await run_in_threadpool(run))
        await deps.enforce_armed_landing(session)
        return result


__all__ = ["analysis_budget_seconds", "register_artifact_analysis_routes"]
