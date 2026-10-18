"""Review API routes for trusted-base check recipes (Phase 9R items 1, 2, 5-8, 27, 29).

``GET  /reviews/{id}/check-recipes``                 recipes, plan status, latest results, claims
``POST /reviews/{id}/check-recipes/run``             execute one admitted check entry (planned only);
                                                     ``"fresh": true`` reuses nothing (no replay, carry, shared base or CI)
``POST /reviews/{id}/check-recipes/requests``        proof request / autofix hand-off as reviewer feedback

The routes are composed into ``register_review_api`` with the seams it
already owns (sessions, the run planner, the runner registry, Snapshot
readers), so nothing here reaches around the Review's trust boundary.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

# Module level: FastAPI resolves the (postponed) ``Request`` annotation from these globals.
from fastapi import HTTPException, Request, status

from lemoncrow.pro.capabilities.review.check_runs import (
    CHECK_ENTRIES,
    CheckRunInputs,
    CheckRunSuperseded,
    is_check_entry,
    run_check_entry,
)
from lemoncrow.pro.capabilities.review.differential import (
    CHECK_DIFFERENTIAL_MEDIA_TYPE,
    AgentClaim,
    claim_status,
    reconcile_claims,
)
from lemoncrow.pro.capabilities.review.execution import ExecutorClass
from lemoncrow.pro.capabilities.review.recipes import (
    RECIPE_ENTRY_PREFIX,
    entry_recipe_digests,
    future_recipe_policy,
)
from lemoncrow.pro.capabilities.review.test_impact import selections_from_document

if TYPE_CHECKING:
    from fastapi import APIRouter

_MAX_CLAIMS = 200


@dataclass(frozen=True)
class CheckRecipeDeps:
    """The Review API seams these routes compose; every one is owned by ``register_review_api``."""

    store: Any
    run_plans: Any
    development_store: Any
    session: Callable[[str], Any]
    mutable_session: Callable[[str], Any]
    latest: Callable[[Any], Any]
    requested_revision: Callable[[Any, str], Any]
    runners: Callable[[], Any]
    surface_runtime: Callable[..., tuple[Any, Any, Any, Any]]
    read_side: Callable[[Any, str, str], str | None]
    review_config: Callable[[Any], Any]
    ai_candidates: Callable[[Any, Any], int]
    enforce_armed_landing: Callable[[Any], Awaitable[Any]]
    annotate_feedback: Callable[..., Any]
    annotation_payload: Callable[[Any], dict[str, Any]]
    changed_paths: Callable[[Any], Sequence[str]]
    json_object: Callable[[Any], Awaitable[dict[str, Any]]]
    #: Phase 9R item 42: the other change-set members' heads of one Change (``None``: not in a set).
    change_set_heads: Callable[[Any], Any] = lambda _session: None
    #: Gate H for one Snapshot: ``{"summary": projection summary | None, "rows": stable key -> annotation}``.
    qualification: Callable[[Any, Any], Mapping[str, Any]] | None = None


def agent_claims(development_store: Any, store: Any, session: Any, revision: Any) -> tuple[AgentClaim, ...]:
    """Checks the authoring side says it ran, as of this Snapshot (latest claim per check).

    Development events (``classify_check`` test/validation) count only up to
    the Snapshot's watermark; caller-attested results (``lc review --check
    --run``) only when recorded on this exact Snapshot. Without a watermark
    no Development claim is attributed to the Snapshot (fail closed).
    """

    from lemoncrow.pro.capabilities.review.development import DevelopmentUnavailable, check_key

    claims: dict[str, AgentClaim] = {}
    try:
        watermark = development_store.snapshot_watermark(revision.id) if development_store.available() else None
        events = (
            development_store.list_events(session.id, upto=watermark, limit=_MAX_CLAIMS, kinds=("test", "validation"))
            if watermark is not None
            else []
        )
    except (DevelopmentUnavailable, OSError, RuntimeError, ValueError):
        events = []
    for event in sorted(events, key=lambda item: int(item.sequence)):
        command = str(event.summary or "").lstrip("✓✗✔✖ ").strip()
        if not command:
            continue
        key = f"development:{check_key(event)}"
        claims[key] = AgentClaim(
            key=key,
            command=command,
            status=claim_status(event.status),
            origin="development",
            ref=str(event.event_id),
            at=str(event.at),
        )
    for item in store.list_evidence(revision.review_id):
        if (
            item.revision_id != revision.id
            or item.attestation_class == "lemoncrow_verification"
            or item.source not in {"test", "agent"}
            or str(item.verification_status or "").upper() not in {"PASS", "FAIL"}
        ):
            continue
        command = (item.source_ref if item.source == "test" else "") or item.title
        key = f"attested:{item.title or item.id}"
        claims.setdefault(
            key,
            AgentClaim(
                key=key,
                command=command,
                status=claim_status("pass" if item.verification_status.upper() == "PASS" else "fail"),
                origin="caller_attested",
                ref=item.id,
                at=item.created_at,
            ),
        )
    return tuple(list(claims.values())[-_MAX_CLAIMS:])


def _read_json(store: Any, item: Any) -> dict[str, Any] | None:
    payload = store.read_evidence_artifact(item)
    if payload is None or not item.content_hash or hashlib.sha256(payload).hexdigest() != item.content_hash:
        return None
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def latest_results(store: Any, revision: Any) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Newest differential per recipe and newest verdict per check key on the exact Snapshot."""

    differentials: dict[str, dict[str, Any]] = {}
    verdicts: dict[str, dict[str, Any]] = {}
    rows = sorted(
        (item for item in store.list_evidence(revision.review_id) if item.revision_id == revision.id),
        key=lambda item: (item.created_at, item.id),
        reverse=True,
    )
    for item in rows:
        if item.execution_intent == "explicit_historical":
            continue
        if item.mime_type == CHECK_DIFFERENTIAL_MEDIA_TYPE:
            record = _read_json(store, item)
            recipe = record.get("recipe") if isinstance(record, Mapping) else None
            recipe_id = str(recipe.get("id") or "") if isinstance(recipe, Mapping) else ""
            if record is not None and recipe_id and recipe_id not in differentials:
                differentials[recipe_id] = {**record, "evidence_id": item.id, "observed_at": item.created_at}
        elif (
            item.attestation_class == "lemoncrow_verification"
            and item.verification_status
            and (item.stable_key in CHECK_ENTRIES or item.stable_key.startswith(RECIPE_ENTRY_PREFIX))
            and item.stable_key not in verdicts
        ):
            verdicts[item.stable_key] = {
                "stable_key": item.stable_key,
                "status": item.verification_status,
                "detail": item.detail,
                "evidence_id": item.id,
                "executor_class": item.executor_class,
                "observed_at": item.created_at,
            }
    return differentials, verdicts


def rerun_budget_seconds(plan: Any) -> float | None:
    """Seconds left for flake reruns under ``per_review_minutes``; ``None`` without that ceiling."""

    ceiling = plan.ceilings.get("per_review_minutes") if isinstance(plan.ceilings, Mapping) else None
    if ceiling is None:
        return None
    used = float(getattr(plan.usage, "review_minutes", 0.0) or 0.0)
    planned = float(plan.totals.get("minutes") or 0.0)
    return max(0.0, (float(ceiling) - used - planned) * 60.0)


_FRESH_MINUTE_CEILINGS: tuple[tuple[str, str, str], ...] = (
    ("per_review_minutes", "review_minutes", "on this Review"),
    ("monthly_minutes", "monthly_minutes", "this month"),
)


def fresh_budget_refusal(plan: Any, entry_id: str, usage: Any) -> str:
    """Why an explicit fresh run does not fit the minute ceilings, ``""`` when it does.

    A fresh run executes again on a Snapshot whose plan already counted that
    work, so it is checked against *usage* read from the live ledger now, not
    the usage frozen into the plan when it was built: once spent minutes reach
    a ceiling (or this run's estimate would cross it) no further fresh run starts.
    """

    ceilings = plan.ceilings if isinstance(plan.ceilings, Mapping) else {}
    entry = plan.entry(entry_id)
    estimate = float(getattr(entry, "estimated_minutes", 0.0) or 0.0)
    for ceiling_key, usage_key, scope in _FRESH_MINUTE_CEILINGS:
        cap = ceilings.get(ceiling_key)
        spent = getattr(usage, usage_key, None)
        if cap is None or spent is None:
            continue  # no ceiling, or this deployment does not meter that spend
        cap, spent = float(cap), float(spent)
        if spent >= cap or spent + estimate > cap:
            return (
                f"a fresh run (about {estimate:g} min) does not fit {ceiling_key}: "
                f"{spent:.1f} of {cap:g} min already spent {scope}"
            )
    return ""


def fresh_rerun_budget_seconds(plan: Any, entry_id: str, usage: Any) -> float | None:
    """Flake-rerun seconds left for a fresh run: the live spend and this run's estimate."""

    ceiling = plan.ceilings.get("per_review_minutes") if isinstance(plan.ceilings, Mapping) else None
    if ceiling is None:
        return None
    entry = plan.entry(entry_id)
    estimate = float(getattr(entry, "estimated_minutes", 0.0) or 0.0)
    used = float(getattr(usage, "review_minutes", 0.0) or 0.0)
    return max(0.0, (float(ceiling) - used - estimate) * 60.0)


def register_check_recipe_routes(router: APIRouter, deps: CheckRecipeDeps) -> None:
    from starlette.concurrency import run_in_threadpool

    from lemoncrow.pro.capabilities.review.surfaces import ReviewSurfaceConfig

    store = deps.store
    run_plans = deps.run_plans

    def _entries(plan: Any, catalog: Any) -> list[dict[str, Any]]:
        ids = [*CHECK_ENTRIES, *(recipe.entry_id for recipe in catalog.configured())]
        out: list[dict[str, Any]] = []
        for entry_id in dict.fromkeys(ids):
            entry = plan.entry(entry_id)
            out.append(
                {
                    "id": entry_id,
                    "status": entry.status if entry is not None else "not_planned",
                    "reason": entry.reason if entry is not None else "not in the effective run plan",
                    "sides": entry.sides if entry is not None else "",
                    "executor": entry.executor if entry is not None else "",
                    "required": bool(entry.required) if entry is not None else False,
                    "recipes": [recipe.id for recipe in catalog.for_entry(entry_id)],
                    "setup_hint": entry.setup_hint if entry is not None else "",
                    # R6: why each recipe runs selected tests or the full suite, and
                    # whether the base runs only on a head failure, and the CI result
                    # an entry is satisfied by.
                    "selection": dict(entry.selection) if entry is not None and entry.selection else None,
                    "base_on_demand": bool(entry.base_on_demand) if entry is not None else False,
                    "satisfied_by": dict(entry.satisfied_by) if entry is not None and entry.satisfied_by else None,
                }
            )
        return out

    @router.get("/reviews/{review_id}/check-recipes")
    async def _get_check_recipes(review_id: str, revision_id: str = "") -> dict[str, Any]:
        """Trusted-base recipes, their plan status and the latest results on one exact Snapshot."""

        session = deps.session(review_id)
        latest = deps.latest(session)
        revision = deps.requested_revision(session, revision_id) if revision_id else latest

        def build() -> dict[str, Any]:
            catalog = run_plans.recipe_catalog(revision)
            plan = run_plans.peek(session, revision)
            try:
                future = future_recipe_policy(catalog, run_plans.head_recipe_catalog(revision))
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                future = {"changed": False, "errors": [f"head recipes could not be read: {exc}"[:300]]}
            differentials, verdicts = latest_results(store, revision)
            # Gate H: each LemonCrow-executed result says whether it may gate, or is advisory and why.
            gate_h = deps.qualification(session, revision) if deps.qualification is not None else {}
            marks = gate_h.get("rows") if isinstance(gate_h.get("rows"), Mapping) else {}
            assert isinstance(marks, Mapping)
            for recipe_id, row in differentials.items():
                mark = marks.get(f"{RECIPE_ENTRY_PREFIX}{recipe_id}")
                if mark is not None:
                    row["qualification"] = mark
            for stable_key, row in verdicts.items():
                mark = marks.get(stable_key)
                if mark is not None:
                    row["qualification"] = mark
            head_verdicts = {recipe_id: str(row.get("head_verdict") or "") for recipe_id, row in differentials.items()}
            claims = reconcile_claims(
                agent_claims(deps.development_store, store, session, revision), catalog.recipes, head_verdicts
            )
            proposals = [
                {
                    "id": item.id,
                    "path": item.path,
                    "state": item.state,
                    "intent": item.intent,
                    "created_by": item.created_by,
                    "patch_text": item.patch_text[:20_000],
                    "annotation_id": item.annotation_id,
                }
                for item in store.list_change_proposals(session.id)
                if item.base_revision_id == revision.id and item.intent.startswith("autofix:")
            ]
            return {
                "revision_id": revision.id,
                "historical": revision.id != latest.id,
                "run_plan_digest": plan.digest,
                **catalog.to_wire(),
                "entries": _entries(plan, catalog),
                # entry id -> recipe digest: the slot CI-equivalence pins (``recipe_digests``).
                "entry_digests": entry_recipe_digests(catalog),
                "future_policy": future,
                "results": [differentials[key] for key in sorted(differentials)],
                "verdicts": [verdicts[key] for key in sorted(verdicts)],
                "claims": [row.to_wire() for row in claims],
                "proposals": proposals,
                **({"qualification": gate_h["summary"]} if gate_h.get("summary") else {}),
            }

        return cast(dict[str, Any], await run_in_threadpool(build))

    @router.get("/reviews/{review_id}/evidence-qualification")
    async def _get_evidence_qualification(review_id: str, revision_id: str = "") -> dict[str, Any]:
        """Gate H on one exact Snapshot: mode, effective thresholds and each executed result's qualification."""

        from lemoncrow.pro.capabilities.review.evidence_qualification import runtime_gate_h_config

        session = deps.session(review_id)
        latest = deps.latest(session)
        revision = deps.requested_revision(session, revision_id) if revision_id else latest

        def build() -> dict[str, Any]:
            gate_h = deps.qualification(session, revision) if deps.qualification is not None else {}
            rows = gate_h.get("rows") if isinstance(gate_h.get("rows"), Mapping) else {}
            assert isinstance(rows, Mapping)
            config = runtime_gate_h_config()
            return {
                "revision_id": revision.id,
                "gate": "H",
                "mode": config.mode,
                "source": config.source,
                "summary": gate_h.get("summary"),
                "subjects": [{"stable_key": key, **dict(rows[key])} for key in sorted(rows)],
            }

        return cast(dict[str, Any], await run_in_threadpool(build))

    @router.post("/reviews/{review_id}/check-recipes/run")
    async def _post_check_recipes_run(review_id: str, request: Request) -> dict[str, Any]:
        """Execute one check entry on one exact Snapshot, only as the effective run plan schedules it."""

        from lemoncrow.pro.capabilities.review.planned_execution import (
            PlannedExecutionRefused,
            admit_planned_execution,
            record_execution_compute,
        )
        from lemoncrow.pro.capabilities.review.run_plan import ManualPick
        from lemoncrow.pro.capabilities.review.runtime_validation import trusted_policy_digest
        from lemoncrow.pro.capabilities.review.runtimes import revision_snapshot_identity

        session = deps.mutable_session(review_id)
        body = await deps.json_object(request)
        requested = str(body.get("revision_id") or "").strip()
        if not requested:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="check recipes run on an exact revision_id (Snapshot)",
            )
        revision = deps.requested_revision(session, requested)
        current = revision.id == deps.latest(session).id
        entry_id = str(body.get("entry") or "").strip()
        if not is_check_entry(entry_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"entry must be one of {', '.join(CHECK_ENTRIES)} or {RECIPE_ENTRY_PREFIX}<id>",
            )
        pick = body.get("pick") is True
        # An explicit fresh run: nothing replayed, carried forward, shared or satisfied by CI.
        fresh = body.get("fresh") is True
        fresh_run_id = uuid.uuid4().hex if fresh else ""

        def run() -> dict[str, Any]:
            def candidates() -> int:
                return deps.ai_candidates(session, revision)

            live = None
            if fresh:
                # Repeated fresh runs are new compute on an already-planned Snapshot:
                # admit each against the live ledger before the pick amends the plan.
                live = run_plans.usage(session, revision)
                preview = run_plans.current(session, revision, ai_candidates=candidates, persist=current)
                refusal = fresh_budget_refusal(preview, entry_id, live)
                if refusal:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refusal)
            if (pick or fresh) and session.status != "archived" and current:
                # A reviewer's pick is an add-only amendment; it never removes required work.
                plan = run_plans.amend(session, revision, [ManualPick(entry_id, fresh=fresh)], ai_candidates=candidates)
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
            catalog = run_plans.recipe_catalog(revision)
            if catalog.base_state == "provisional":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="the trusted base is not readable yet, so no recipe can be resolved",
                )
            authority = deps.review_config(revision)
            executor_class = getattr(runners, "executor_class", ExecutorClass.AUTHOR_MACHINE)
            recorder = run_plans.compute.recorder(session.id, revision.id)

            def run_side(runtime: Any, surface: Any, side: str) -> Any:
                _revision, _registry, side_runners, context = deps.surface_runtime(
                    session.id, revision.id, side=side, immutable_execution=True
                )
                # The synthetic recipe runtime is the only one: head config can add none.
                context = replace(context, config=ReviewSurfaceConfig(runtimes=(runtime,)))
                return side_runners.run(context, surface, side=side, cleanup_workspace=True)

            def meter(execution: Mapping[str, Any] | None, wall: float) -> None:
                record_execution_compute(
                    recorder,
                    entry_id=entry_id,
                    run_on=run_on,
                    execution=execution,
                    wall_seconds=wall,
                    fallback_stage="check_recipe",
                )

            def saved(reason: str, seconds: float) -> None:
                run_plans.savings.record(
                    review_id=session.id, revision_id=revision.id, entry_id=entry_id, reason=reason, seconds=seconds
                )

            inputs = CheckRunInputs(
                store=store,
                revision=revision,
                catalog=catalog,
                entry_id=entry_id,
                sides=("old", "new") if entry.sides == "base+head" else ("new",),
                executor_class=executor_class,
                policy_digest=trusted_policy_digest(authority.content, base_known=authority.base_known),
                changed_paths=tuple(deps.changed_paths(revision)),
                read_base=lambda path: deps.read_side(revision, path, "old"),
                read_head=lambda path: deps.read_side(revision, path, "new"),
                head_identity=revision_snapshot_identity(revision, "new"),
                claims=agent_claims(deps.development_store, store, session, revision),
                rerun_budget_seconds=(
                    rerun_budget_seconds(plan) if live is None else fresh_rerun_budget_seconds(plan, entry_id, live)
                ),
                expected_current_revision_id=revision.id if current else "",
                execution_intent="current" if current else "explicit_historical",
                selection=selections_from_document(entry.selection),
                base_on_demand=entry.base_on_demand,
                saved=saved,
                fresh_run_id=fresh_run_id,
                environment=getattr(runners, "environment_probe", None),
            )
            started = time.monotonic()
            try:
                report = run_check_entry(inputs, run_side, meter)
            except CheckRunSuperseded as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"the Review advanced during the check run; results were not published as current: {exc}",
                ) from exc
            if entry.equivalence:
                # Why an equivalent CI result did not stand in (kill switch, fresh pick).
                equivalence = dict(entry.equivalence)
                report.validity[str(equivalence.get("subject_ref") or f"entry:{entry_id}")] = equivalence
            return {
                **report.to_wire(),
                "run_plan_digest": plan.digest,
                "plan_entry": entry.to_wire(),
                "executor_class": executor_class.value,
                "run_on": run_on,
                "fresh_run_id": fresh_run_id,
                "wall_seconds": round(time.monotonic() - started, 3),
            }

        result = cast(dict[str, Any], await run_in_threadpool(run))
        await deps.enforce_armed_landing(session)
        return result

    @router.post("/reviews/{review_id}/check-recipes/requests", status_code=status.HTTP_201_CREATED)
    async def _post_check_request(review_id: str, request: Request) -> dict[str, Any]:
        """A reviewer's proof request or autofix hand-off, as feedback through the normal handoff.

        It is a human comment (never a code-change demand); sending it to the
        authoring agent uses the existing delivery. The agent's new Snapshot
        is re-executed by the planned runner like any other Snapshot.
        """

        session = deps.mutable_session(review_id)
        revision = deps.latest(session)
        body = await deps.json_object(request)
        expected = str(body.get("expected_revision_id") or "").strip()
        if expected and expected != revision.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the Review advanced; reload the current Snapshot before requesting proof",
            )
        kind = str(body.get("kind") or "proof").strip()
        if kind not in {"proof", "autofix"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="kind must be proof or autofix"
            )
        note = str(body.get("body") or "").strip()[:4000]
        units = {unit.path for unit in store.list_units(revision.id) if unit.kind == "file"}
        evidence: list[str] = []
        if kind == "autofix":
            proposal = store.get_change_proposal(str(body.get("proposal_id") or ""))
            if (
                proposal is None
                or proposal.review_id != session.id
                or proposal.base_revision_id != revision.id
                or not proposal.intent.startswith("autofix:")
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="no autofix proposal on the current Snapshot"
                )
            recipe_id = proposal.intent.removeprefix("autofix:")
            path = proposal.path
            title = f"Autofix proposed · {recipe_id}"
            text = (
                note
                or f"The trusted-base `{recipe_id}` check fails; its autofix proposes this change. "
                "Please apply it (or explain why not) in a new Snapshot."
            )
            text += f"\n\n```diff\n{proposal.patch_text[:12_000]}\n```"
            evidence = [f"proposal:{proposal.id}", f"check-recipe:{recipe_id}"]
        else:
            recipe_id = str(body.get("recipe_id") or "").strip()
            subject = str(body.get("subject") or "").strip()[:400]
            catalog = run_plans.recipe_catalog(revision)
            if catalog.get(recipe_id) is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such trusted-base check recipe")
            path = str(body.get("path") or "").strip()
            if not path:
                differentials, _verdicts = latest_results(store, revision)
                tests = differentials.get(recipe_id, {}).get("tests") or []
                path = next(
                    (str(row.get("file")) for row in tests if row.get("subject") == subject and row.get("file")),
                    "",
                )
            if path not in units:
                path = sorted(units)[0] if units else ""
            title = f"Proof requested · {subject or recipe_id}"[:200]
            text = note or (
                f"Please show this change is proven by `{recipe_id}`"
                + (f" for `{subject}`" if subject else "")
                + ": add or adjust a test that fails on the base and passes on your change. "
                "LemonCrow re-runs the check on your next Snapshot."
            )
            evidence = [f"check-recipe:{recipe_id}", *([f"test:{subject}"] if subject else [])]
            behavior_key = str(body.get("behavior_key") or "").strip()
            if behavior_key:
                evidence.append(behavior_key[:300])
        if not path or path not in units:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="the current Snapshot has no file to anchor the request"
            )

        def create() -> Any:
            return deps.annotate_feedback(
                session, revision, path=path, body=text, title=title, evidence=tuple(evidence)
            )

        try:
            annotation = await run_in_threadpool(create)
        except ValueError as exc:
            message = str(exc)
            code = status.HTTP_409_CONFLICT if "advanced" in message else status.HTTP_422_UNPROCESSABLE_ENTITY
            raise HTTPException(status_code=code, detail=message) from exc
        await deps.enforce_armed_landing(session)
        return {"annotation": deps.annotation_payload(annotation), "kind": kind, "revision_id": revision.id}


__all__ = [
    "CheckRecipeDeps",
    "agent_claims",
    "latest_results",
    "register_check_recipe_routes",
    "rerun_budget_seconds",
]
