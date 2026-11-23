"""Review API routes for runtime observation and deep rendering diffs (Phase 9R items 10, 13, 14).

``GET  /reviews/{id}/runtime-observation``                    plan status and latest results
``POST /reviews/{id}/runtime-observation/run``                execute one admitted entry (planned only)
``GET  /reviews/{id}/runtime-observation/artifacts/{digest}`` one referenced run artifact (screenshot, snapshot)

Entries: ``side_effects.runtime`` (observed base/head test runs through the
R4 runner registry), ``visual.dom_a11y`` and ``visual.matrix`` (sandboxed
headless-browser renders: on the local executor, or on an executor whose
registry carries a ``render_executor``). Every run is admitted by the
effective run plan (``planned_execution``), meters its compute, honors the
entry budget, and publishes against the exact Snapshot. The routes compose
the seams ``register_review_api`` already owns (:class:`CheckRecipeDeps`).
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

# Module level: FastAPI resolves the (postponed) ``Request`` annotation from these globals.
from fastapi import HTTPException, Request, status

from lemoncrow.pro.capabilities.review import request_reads
from lemoncrow.pro.capabilities.review.check_api import CheckRecipeDeps, rerun_budget_seconds
from lemoncrow.pro.capabilities.review.render_diff import (
    RENDER_DIFF_MEDIA_TYPE,
    RENDER_ENTRIES,
    RenderRunInputs,
    RenderRunSuperseded,
    RenderUnavailable,
    latest_render_diffs,
    plan_cells,
    render_config_from_review,
    run_render_entry,
    select_routes,
)
from lemoncrow.pro.capabilities.review.render_driver import (
    ChromeCdpDriver,
    RenderDriver,
    RenderRequest,
    RenderSource,
)
from lemoncrow.pro.capabilities.review.runtime_observation import (
    LIMITATIONS,
    RUNTIME_OBSERVATION_ENTRY,
    RUNTIME_OBSERVATION_MEDIA_TYPE,
)
from lemoncrow.pro.capabilities.review.runtime_observation_runs import (
    ObservationRunInputs,
    ObservationRunSuperseded,
    latest_observations,
    run_observation_entry,
)
from lemoncrow.pro.environment.execution import ExecutorClass, cache_namespace

if TYPE_CHECKING:
    from fastapi import APIRouter

OBSERVATION_ENTRIES: Final[tuple[str, ...]] = (RUNTIME_OBSERVATION_ENTRY, *RENDER_ENTRIES)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def default_render_driver() -> RenderDriver:
    """The render driver seam (tests replace it with a deterministic fake)."""

    return ChromeCdpDriver()


def _entry_budget(plan: Any, entry: Any) -> float | None:
    """Seconds the admitted entry may spend: its estimate plus the plan's remaining slack."""

    slack = rerun_budget_seconds(plan)
    if slack is None:
        return None
    return max(0.0, float(getattr(entry, "estimated_minutes", 0.0) or 0.0) * 60.0) + slack


def referenced_artifact(store: Any, revision: Any, digest: str) -> bool:
    """True when a current record of this Snapshot references the run artifact."""

    needle = f"run-artifact:{revision.review_id}:{digest}".encode()
    # lc-debt: linear scan of this Snapshot's observation/render records per artifact read;
    # index artifact refs per record if Snapshots accumulate many re-runs.
    for item in store.list_evidence(revision.review_id):
        if item.revision_id != revision.id or item.mime_type not in {
            RENDER_DIFF_MEDIA_TYPE,
            RUNTIME_OBSERVATION_MEDIA_TYPE,
        }:
            continue
        payload = store.read_evidence_artifact(item)
        if payload is not None and needle in payload:
            return True
    return False


def register_runtime_observation_routes(router: APIRouter, deps: CheckRecipeDeps) -> None:
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import Response

    from lemoncrow.pro.capabilities.review.surfaces import ReviewSurfaceConfig

    store = deps.store
    run_plans = deps.run_plans

    @router.get("/reviews/{review_id}/runtime-observation")
    async def _get_runtime_observation(review_id: str, revision_id: str = "") -> dict[str, Any]:
        """Plan status of the observation entries and the latest results on one exact Snapshot."""

        session = deps.session(review_id)
        latest = deps.latest(session)
        revision = deps.requested_revision(session, revision_id) if revision_id else latest

        def build() -> dict[str, Any]:
            with request_reads.reads_once():
                plan = run_plans.peek(session, revision)
            entries = []
            for entry_id in OBSERVATION_ENTRIES:
                entry = plan.entry(entry_id)
                entries.append(
                    {
                        "id": entry_id,
                        "status": entry.status if entry is not None else "not_planned",
                        "reason": entry.reason if entry is not None else "not in the effective run plan",
                        "sides": entry.sides if entry is not None else "",
                        "executor": entry.executor if entry is not None else "",
                        "required": bool(entry.required) if entry is not None else False,
                        "estimated_minutes": float(entry.estimated_minutes) if entry is not None else 0.0,
                    }
                )
            observations = latest_observations(store, revision)
            renders = latest_render_diffs(store, revision)
            return {
                "revision_id": revision.id,
                "historical": revision.id != latest.id,
                "run_plan_digest": plan.digest,
                "entries": entries,
                "side_effects": [observations[key] for key in sorted(observations)],
                "render": [renders[key] for key in sorted(renders)],
                "limitations": list(LIMITATIONS),
            }

        return cast(dict[str, Any], await run_in_threadpool(build))

    @router.get("/reviews/{review_id}/runtime-observation/artifacts/{digest}")
    async def _get_runtime_observation_artifact(review_id: str, digest: str, revision_id: str = "") -> Response:
        """One raw run artifact a current record references (expired: 410, never re-created)."""

        if not _DIGEST_RE.match(digest):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such run artifact")
        session = deps.session(review_id)
        revision = deps.requested_revision(session, revision_id) if revision_id else deps.latest(session)

        def read() -> tuple[bytes | None, str, bool]:
            if not referenced_artifact(store, revision, digest):
                return None, "", False
            artifacts = store.run_artifacts()
            if artifacts is None:
                return None, "", True
            meta = artifacts.meta(revision.review_id, digest) or {}
            return artifacts.read(revision.review_id, digest), str(meta.get("media_type") or ""), True

        payload, media_type, referenced = await run_in_threadpool(read)
        if not referenced:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such run artifact on this revision")
        if payload is None:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="the run artifact expired under raw retention; re-run the entry to observe it again",
            )
        media = media_type if media_type in {"image/png", "text/plain"} else "application/octet-stream"
        return Response(
            content=payload,
            media_type=media if media != "text/plain" else "text/plain; charset=utf-8",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @router.post("/reviews/{review_id}/runtime-observation/run")
    async def _post_runtime_observation_run(review_id: str, request: Request) -> dict[str, Any]:
        """Execute one observation entry on one exact Snapshot, only as the effective run plan schedules it."""

        from lemoncrow.pro.capabilities.review.hermetic import HermeticPolicy
        from lemoncrow.pro.capabilities.review.planned_execution import (
            PlannedExecutionRefused,
            admit_planned_execution,
            record_execution_compute,
        )
        from lemoncrow.pro.capabilities.review.run_plan import ManualPick
        from lemoncrow.pro.capabilities.review.runtime_validation import trusted_policy_digest

        session = deps.mutable_session(review_id)
        body = await deps.json_object(request)
        requested = str(body.get("revision_id") or "").strip()
        if not requested:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="observation runs on an exact revision_id (revision)"
            )
        revision = deps.requested_revision(session, requested)
        current = revision.id == deps.latest(session).id
        entry_id = str(body.get("entry") or "").strip()
        if entry_id not in OBSERVATION_ENTRIES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"entry must be one of {', '.join(OBSERVATION_ENTRIES)}",
            )
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
            authority = deps.review_config(revision)
            executor_class = getattr(runners, "executor_class", ExecutorClass.AUTHOR_MACHINE)
            recorder = run_plans.compute.recorder(session.id, revision.id)
            budget = _entry_budget(plan, entry)
            policy_digest = trusted_policy_digest(authority.content, base_known=authority.base_known)
            intent = "current" if current else "explicit_historical"

            def meter(execution: Mapping[str, Any] | None, wall: float) -> None:
                record_execution_compute(
                    recorder,
                    entry_id=entry_id,
                    run_on=run_on,
                    execution=execution,
                    wall_seconds=wall,
                    fallback_stage="render" if entry_id in RENDER_ENTRIES else "observation",
                )

            started = time.monotonic()
            if entry_id == RUNTIME_OBSERVATION_ENTRY:
                catalog = run_plans.recipe_catalog(revision)
                if catalog.base_state == "provisional":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="the trusted base is not readable yet, so no recipe can be resolved",
                    )

                def run_side(runtime: Any, surface: Any, side: str) -> Any:
                    _revision, _registry, side_runners, context = deps.surface_runtime(
                        session.id, revision.id, side=side, immutable_execution=True
                    )
                    # The synthetic observed runtime is the only one: head config can add none.
                    context = replace(context, config=ReviewSurfaceConfig(runtimes=(runtime,)))
                    return side_runners.run(context, surface, side=side, cleanup_workspace=True)

                try:
                    observed = run_observation_entry(
                        ObservationRunInputs(
                            store=store,
                            revision=revision,
                            recipes=tuple(catalog.for_entry("checks.test")),
                            executor_class=executor_class,
                            policy_digest=policy_digest,
                            changed_paths=tuple(deps.changed_paths(revision)),
                            sides=("old", "new") if entry.sides == "base+head" else ("new",),
                            budget_seconds=budget,
                            expected_current_revision_id=revision.id if current else "",
                            execution_intent=intent,
                        ),
                        run_side,
                        meter,
                    )
                except ObservationRunSuperseded as exc:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"the Review advanced during the run; results were not published as current: {exc}",
                    ) from exc
                report = observed.to_wire()
            else:
                # Off the local executor only a deployment that wired a render path runs these:
                # the control plane never builds or renders reviewed code itself.
                remote = getattr(runners, "render_executor", None) if run_on != "local" else None
                if run_on != "local" and remote is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="rendering diffs run only on the local executor",
                    )
                report = _render(
                    entry_id, entry, budget, meter, policy_digest, intent, current, authority, executor_class, remote
                )
            return {
                **report,
                "run_plan_digest": plan.digest,
                "plan_entry": entry.to_wire(),
                "executor_class": executor_class.value,
                "run_on": run_on,
                "wall_seconds": round(time.monotonic() - started, 3),
            }

        def _render(
            entry_id: str,
            entry: Any,
            budget: float | None,
            meter: Any,
            policy_digest: str,
            intent: str,
            current: bool,
            authority: Any,
            executor_class: ExecutorClass,
            remote: Any,
        ) -> dict[str, Any]:
            from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, materialize_review_side
            from lemoncrow.pro.capabilities.review.web_preview import (
                WebPreviewUnavailable,
                infer_web_preview_descriptor,
            )

            config = render_config_from_review(authority.content)
            _revision, _registry, _runners, context = deps.surface_runtime(
                session.id, revision.id, side="new", immutable_execution=True
            )
            source = context.source_root or context.repo_root
            hermetic = context.execution.for_side("new") if context.execution is not None else None
            policy = hermetic.policy if hermetic is not None else HermeticPolicy()

            def infer(path: str) -> tuple[str, ...] | None:
                try:
                    descriptor = infer_web_preview_descriptor(
                        context.repo_root, revision, path, store_root=context.store_root, store=store
                    )
                except (OSError, ValueError, WebPreviewUnavailable):
                    return None
                return tuple(descriptor.routes) if descriptor is not None and descriptor.routes else None

            changed = tuple(deps.changed_paths(revision))
            selection = select_routes(config, changed, infer=infer)
            cells = plan_cells(config, selection, entry=cast(Any, entry_id), budget_seconds=budget)

            def site(side: str, work: Path) -> tuple[Path, bool, str]:
                if config.static_root:
                    target = work / "tree"
                    try:
                        materialize_review_side(
                            context.store_root,
                            source,
                            revision,
                            side=side,
                            target=target,
                            scope=config.static_root,
                            store=store,
                        )
                    except ReviewSnapshotUnavailable as exc:
                        raise RenderUnavailable(str(exc)) from exc
                    root = (target / config.static_root).resolve()
                    confined = target.resolve()
                    if not root.is_dir() or (root != confined and confined not in root.parents):
                        raise RenderUnavailable(f"render.static_root {config.static_root} is missing on this side")
                    return root, config.spa, config.static_root
                return _built_site(side, changed, context)

            def materialize(side: str, target: Path, scope: str) -> None:
                try:
                    materialize_review_side(
                        context.store_root, source, revision, side=side, target=target, scope=scope, store=store
                    )
                except ReviewSnapshotUnavailable as exc:
                    raise RenderUnavailable(str(exc)) from exc

            def remote_source() -> RenderSource:
                if config.static_root:
                    return RenderSource(config.static_root, config.spa)
                descriptor = _descriptor(changed, context)
                return RenderSource(descriptor.root, descriptor.framework == "vite", descriptor.framework)

            def side_policy(side: str) -> tuple[Mapping[str, Any], str]:
                if context.execution is None:
                    return policy.to_wire(), cache_namespace(side, "unknown")
                scoped = context.execution.for_side(side)
                if scoped.policy_error:
                    raise RenderUnavailable(f"trusted-base execution policy is invalid: {scoped.policy_error}")
                return scoped.policy.to_wire(), scoped.namespace

            driver: RenderDriver
            resolver = site
            if remote is None:
                driver = default_render_driver()
            else:
                bound = remote.bind(RenderRequest(remote_source, materialize, side_policy))
                driver, resolver = bound, bound.site

            try:
                rendered = run_render_entry(
                    RenderRunInputs(
                        store=store,
                        revision=revision,
                        entry_id=cast(Any, entry_id),
                        config=config,
                        selection=selection,
                        plan=cells,
                        driver=driver,
                        site=resolver,
                        artifacts=store.run_artifacts(retention_seconds=policy.retention_seconds),
                        executor_class=executor_class,
                        policy=policy,
                        policy_digest=policy_digest,
                        expected_current_revision_id=revision.id if current else "",
                        execution_intent=intent,
                    ),
                    meter,
                )
            except RenderRunSuperseded as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"the Review advanced during the render; results were not published as current: {exc}",
                ) from exc
            return rendered.to_wire()

        def _descriptor(changed: tuple[str, ...], context: Any) -> Any:
            """The buildable web app this change touches; raises ``RenderUnavailable`` without one."""

            from lemoncrow.pro.capabilities.review import web_preview

            descriptor = None
            for path in changed:
                try:
                    descriptor = web_preview.infer_web_preview_descriptor(
                        context.repo_root, revision, path, store_root=context.store_root, store=store
                    )
                except (OSError, ValueError, web_preview.WebPreviewUnavailable):
                    descriptor = None
                if descriptor is not None:
                    break
            if descriptor is None:
                raise RenderUnavailable(
                    "no static render source: set render.static_root in the trusted-base review config, "
                    "or change a file the web preview can build"
                )
            return descriptor

        def _built_site(side: str, changed: tuple[str, ...], context: Any) -> tuple[Path, bool, str]:
            """A framework app's revision-pinned static build, through the web preview's sandboxed builder."""

            from lemoncrow.pro.capabilities.review import web_preview
            from lemoncrow.pro.capabilities.review.scratch_cache import touch_cache_entry

            descriptor = _descriptor(changed, context)
            try:
                # The same revision-pinned, sandboxed build the web preview serves (content-keyed cache).
                cache_root = web_preview._build_cache_root(
                    context.store_root,
                    revision,
                    side=side,
                    framework=descriptor.framework,
                    project_root=descriptor.root,
                    dependency_repo=context.dependency_root or context.repo_root,
                )
                touch_cache_entry(cache_root)
                output = web_preview._ensure_built(
                    cache_root,
                    context.repo_root,
                    revision,
                    store_root=context.store_root,
                    side=side,
                    framework=descriptor.framework,
                    project_root=descriptor.root,
                    store=store,
                    dependency_root=context.dependency_root,
                )
            except (OSError, web_preview.WebPreviewUnavailable) as exc:
                raise RenderUnavailable(str(exc)[:500]) from exc
            if not output.is_dir():
                raise RenderUnavailable("the web preview build produced no output")
            # The driver mounts the build read-only, so the shared cache entry is never written.
            return output, descriptor.framework == "vite", descriptor.root

        result = cast(dict[str, Any], await run_in_threadpool(run))
        await deps.enforce_armed_landing(session)
        return result


__all__ = [
    "OBSERVATION_ENTRIES",
    "default_render_driver",
    "referenced_artifact",
    "register_runtime_observation_routes",
]
