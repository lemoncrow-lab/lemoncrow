"""Pre-merge check and intent routes (Phase 10 items 1-6).

Routes reuse the Review API's gates (repository-scoped session, mutability,
expected Snapshot) and add no authority of their own: a waiver is granted only
to a principal the effective policy names, is audited whether granted or
refused, and is recorded on the existing ``human_waiver`` evidence lane.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, status

from lemoncrow.pro.capabilities.review.intent import (
    INTENT_KINDS,
    IntentContent,
    IntentObservation,
    IntentReference,
    normalize_reference,
    rebind_intent,
    record_intent,
)
from lemoncrow.pro.capabilities.review.premerge_checks import (
    WAIVER_SCOPES,
    PremergeFacts,
    evaluate_premerge,
    premerge_projection,
    waiver_evidence,
    waiver_expiry,
)
from lemoncrow.pro.capabilities.review.session_models import (
    ReviewActivityEvent,
    ReviewRevision,
    ReviewSession,
)

_MAX_BODY_TEXT = 65536
_TRIAL_DEFAULT = 25
_TRIAL_MAX = 100

# lc-debt: scheduled evaluations run on one small per-process pool and a fixed
# set of lock stripes; a crashed process loses its queued runs (the next
# capture, push or explicit Evaluate re-plans them) and two processes may both
# evaluate one input digest once (the later row supersedes the earlier).
# Upgrade: a durable job like hosted AI review, keyed by the input digest.
_POOL_WORKERS = 2
_LOCK_STRIPES = tuple(threading.Lock() for _ in range(32))
_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def premerge_background(job: Callable[[], None]) -> None:
    """Run one scheduled evaluation off the request path, in the caller's context (bound principal)."""

    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_POOL_WORKERS, thread_name_prefix="lc-premerge")
        pool = _pool
    pool.submit(contextvars.copy_context().run, job)


def premerge_lock(revision_id: str, check_id: str) -> threading.Lock:
    """The lock serializing evaluations of one check on one Snapshot in this process."""

    digest = hashlib.sha256(f"{revision_id}\0{check_id}".encode()).digest()
    return _LOCK_STRIPES[digest[0] % len(_LOCK_STRIPES)]


@dataclass(frozen=True, slots=True)
class PremergeDeps:
    """What the routes need from the Review API (all already gated there)."""

    store: Any
    session: Callable[[str], ReviewSession]
    mutable_session: Callable[[str], ReviewSession]
    latest: Callable[[ReviewSession], ReviewRevision]
    require_expected_revision: Callable[[Mapping[str, Any], ReviewRevision, str], None]
    #: Pre-merge facts of one Snapshot, or ``None`` when no check is configured (raises on read failure).
    facts: Callable[[ReviewSession, ReviewRevision], PremergeFacts | None]
    #: The readiness policy generation digest of one Snapshot.
    generation: Callable[[ReviewSession, ReviewRevision], str]
    #: Explicit re-run of the checks named (all when empty): amends the run plan
    #: with a manual pick of ``ai.premerge_checks``, then executes it (raises
    #: ``HTTPException`` 409 when the plan does not run it).
    run_checks: Callable[[ReviewSession, ReviewRevision, frozenset[str]], Mapping[str, Any]]
    #: Plan-gated origination after an event that changes what checks read.
    schedule_checks: Callable[[ReviewSession, ReviewRevision], Mapping[str, Any]]
    #: Whether the acting principal may change what this Review is judged against.
    may_manage: Callable[[ReviewSession], bool]
    enforce_armed_landing: Callable[[ReviewSession], Awaitable[Any]]
    #: ``owner/repo`` this Review's provider subject names ("" when none) for ``#123`` references.
    repository: Callable[[ReviewSession], str]
    #: Open Reviews in this repository scope, newest first (trial report).
    open_sessions: Callable[[int], Sequence[ReviewSession]]
    now: Callable[[], str]


def _text(value: object, *, limit: int = _MAX_BODY_TEXT) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _activity(
    deps: PremergeDeps,
    session: ReviewSession,
    revision: ReviewRevision,
    *,
    kind: str,
    subject_type: str,
    subject_id: str,
    summary: str,
    detail: Mapping[str, Any],
) -> None:
    deps.store.record_activity(
        ReviewActivityEvent(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            kind=cast(Any, kind),
            actor_id=session.reviewer_id,
            actor_type="human",
            subject_type=subject_type,
            subject_id=subject_id,
            summary=summary[:500],
            detail_json=json.dumps(dict(detail), ensure_ascii=False, sort_keys=True),
        )
    )


def register_premerge_routes(router: APIRouter, deps: PremergeDeps) -> None:
    from lemoncrow.pro.capabilities.review.api import _json_object

    def status_payload(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        facts = deps.facts(session, revision)
        if facts is None:
            return {"configured": False, "snapshot_id": revision.id, "checks": [], "intents": []}
        return {
            "configured": True,
            **premerge_projection(facts, generation=deps.generation(session, revision), now=deps.now()),
        }

    def require_manager(
        session: ReviewSession, revision: ReviewRevision, action: str, *, kind: str, subject_id: str
    ) -> None:
        """Refuse (and audit, naming the acting principal) a non-manager changing what the checks judge."""

        if deps.may_manage(session):
            return
        _activity(
            deps,
            session,
            revision,
            kind=kind,
            subject_type="intent",
            subject_id=subject_id,
            summary=f"{session.reviewer_id} was refused {action.lower()} {subject_id}: not a Review manager",
            detail={"ref": subject_id, "actor": session.reviewer_id},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{action} changes what this Review's pre-merge checks judge; it needs Review manage permission",
        )

    def require_facts(session: ReviewSession, revision: ReviewRevision) -> PremergeFacts:
        try:
            facts = deps.facts(session, revision)
        except Exception as exc:  # unreadable facts never grant anything
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"pre-merge check facts are unavailable: {str(exc)[:200]}",
            ) from exc
        if facts is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="no pre-merge check is configured for this Review"
            )
        return facts

    @router.get("/reviews/{review_id}/premerge")
    async def _get_premerge(review_id: str) -> dict[str, Any]:
        """Every configured check on the latest Snapshot (warning-mode trial included) and linked intent."""

        session = deps.session(review_id)
        revision = deps.latest(session)
        return await asyncio.to_thread(status_payload, session, revision)

    @router.post("/reviews/{review_id}/intent", status_code=status.HTTP_201_CREATED)
    async def _post_intent(review_id: str, request: Request) -> dict[str, Any]:
        """Record linked intent sources and/or the Change description (local Reviews, ``lc review --intent``).

        Recorded text is untrusted data. The first readable capture of a
        reference binds it; later different text is an observation shown as
        "intent changed", never a silent re-bind.
        """

        session = deps.mutable_session(review_id)
        body = await _json_object(request)
        revision = deps.latest(session)
        deps.require_expected_revision(body, revision, "recording intent")
        require_manager(
            session, revision, "Recording intent", kind="premerge.intent_record_refused", subject_id="intent"
        )
        repository = deps.repository(session)
        observations: list[IntentObservation] = []
        raw_refs = body.get("references") or []
        if not isinstance(raw_refs, list) or len(raw_refs) > 8:
            raise HTTPException(status_code=422, detail="references must be a list of at most 8 intent sources")
        for index, raw in enumerate(raw_refs):
            if not isinstance(raw, Mapping):
                raise HTTPException(status_code=422, detail=f"references[{index}] must be an object")
            kind = str(raw.get("kind") or "").strip().lower()
            if kind not in INTENT_KINDS:
                raise HTTPException(
                    status_code=422, detail=f"references[{index}].kind must be one of {', '.join(INTENT_KINDS)}"
                )
            normalized = normalize_reference(str(raw.get("ref") or ""), repository=repository)
            if normalized is None:
                raise HTTPException(status_code=422, detail=f"references[{index}].ref is not a recognized reference")
            ref, url = normalized
            title = _text(raw.get("title"), limit=300)
            text = _text(raw.get("body"))
            content = (
                IntentContent(
                    title=title,
                    body=text,
                    source_revision=_text(raw.get("source_revision"), limit=200),
                    fetched_at=deps.now(),
                    url=url,
                )
                if raw.get("body") is not None or raw.get("title") is not None
                else None
            )
            observations.append(
                IntentObservation(
                    reference=IntentReference(kind=kind, ref=ref, origin="cli", url=url, title=title),
                    content=content,
                    note="" if content is not None else "reference only: no text was supplied",
                )
            )
        raw_description = body.get("description")
        description: IntentContent | None = None
        if raw_description is not None:
            if not isinstance(raw_description, Mapping):
                raise HTTPException(status_code=422, detail="description must be an object with title and body")
            description = IntentContent(
                title=_text(raw_description.get("title"), limit=300),
                body=_text(raw_description.get("body")),
                fetched_at=deps.now(),
            )
        result = await asyncio.to_thread(
            lambda: record_intent(
                deps.store,
                review_id=session.id,
                revision_id=revision.id,
                observations=observations,
                description=description,
                description_origin="cli",
                actor=session.reviewer_id,
                source="human",
            )
        )
        await deps.enforce_armed_landing(session)
        scheduled = await asyncio.to_thread(deps.schedule_checks, session, revision)
        return {
            "recorded": result.to_wire(),
            "scheduled": dict(scheduled),
            "premerge": await asyncio.to_thread(status_payload, session, revision),
        }

    @router.post("/reviews/{review_id}/intent/rebind")
    async def _post_intent_rebind(review_id: str, request: Request) -> dict[str, Any]:
        """The Review owner/manager explicitly binds the newest captured text of one changed intent source.

        Re-binding changes what the checks judge, so it needs manage authority
        (hosted ``REVIEW_MANAGE``; locally the Review owner). Audited.
        """

        session = deps.mutable_session(review_id)
        body = await _json_object(request)
        revision = deps.latest(session)
        deps.require_expected_revision(body, revision, "re-binding intent")
        ref = str(body.get("ref") or "").strip()
        if not ref:
            raise HTTPException(status_code=422, detail="ref is required")
        require_manager(session, revision, "Re-binding intent", kind="premerge.intent_rebind_refused", subject_id=ref)
        try:
            snapshot = await asyncio.to_thread(
                lambda: rebind_intent(
                    deps.store, review_id=session.id, revision_id=revision.id, ref=ref, actor=session.reviewer_id
                )
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        _activity(
            deps,
            session,
            revision,
            kind="premerge.intent_rebound",
            subject_type="intent",
            subject_id=ref,
            summary=f"{session.reviewer_id} re-bound {snapshot.title or ref} to its current text",
            detail={"ref": ref, "evidence_id": snapshot.evidence_id, "digest": snapshot.digest},
        )
        await deps.enforce_armed_landing(session)
        scheduled = await asyncio.to_thread(deps.schedule_checks, session, revision)
        return {
            "intent": snapshot.to_wire(),
            "scheduled": dict(scheduled),
            "premerge": await asyncio.to_thread(status_payload, session, revision),
        }

    @router.post("/reviews/{review_id}/premerge/evaluate")
    async def _post_premerge_evaluate(review_id: str, request: Request) -> dict[str, Any]:
        """Explicitly re-run the model-evaluated checks on the latest Snapshot (a manual run-plan pick).

        The run plan normally schedules them on capture/push; this amends the
        plan with a manual pick of ``ai.premerge_checks`` (409 when the plan
        still does not run it: AI family off, budget, invalid policy). Each
        check is one budgeted Review Intelligence call, idempotent per input
        digest. Denied content or an exhausted budget records
        ``NOT_RUN``/``UNKNOWN``, never ``PASS``; a verdict without verifiable
        citations is ``UNKNOWN``.
        """

        session = deps.mutable_session(review_id)
        body = await _json_object(request)
        revision = deps.latest(session)
        deps.require_expected_revision(body, revision, "evaluating pre-merge checks")
        facts = require_facts(session, revision)
        requested = body.get("checks")
        if requested is not None and (
            not isinstance(requested, list) or not all(isinstance(item, str) for item in requested)
        ):
            raise HTTPException(status_code=422, detail="checks must be a list of check ids")
        wanted = set(requested or ())
        checks = [
            check for check in facts.policy.checks if check.model_evaluated and (not wanted or check.id in wanted)
        ]
        unknown = sorted(wanted - {check.id for check in facts.policy.checks if check.model_evaluated})
        if unknown:
            raise HTTPException(status_code=422, detail=f"{unknown[0]!r} is not a model-evaluated check")
        if not checks:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="no model-evaluated pre-merge check is configured here"
            )
        result = await asyncio.to_thread(deps.run_checks, session, revision, frozenset(wanted))
        await deps.enforce_armed_landing(session)
        return {**dict(result), "premerge": await asyncio.to_thread(status_payload, session, revision)}

    @router.post("/reviews/{review_id}/premerge/waivers", status_code=status.HTTP_201_CREATED)
    async def _post_premerge_waiver(review_id: str, request: Request) -> dict[str, Any]:
        """Waive one blocking error-mode check: permissioned, reasoned, scoped, expiring and audited.

        The waiver binds to the current policy generation; it lapses when the
        policy changes, at expiry, or when a new Snapshot falls outside its
        scope (``snapshot``: this Snapshot only; ``review``: later Snapshots
        that change no path beyond the ones changed now). The check keeps its
        observed status; readiness shows it as waived, never passed.
        """

        session = deps.mutable_session(review_id)
        body = await _json_object(request)
        revision = deps.latest(session)
        deps.require_expected_revision(body, revision, "waiving a pre-merge check")
        check_id = str(body.get("check_id") or "").strip()
        reason = str(body.get("reason") or "").strip()
        scope = str(body.get("scope") or "snapshot").strip()
        if not check_id:
            raise HTTPException(status_code=422, detail="check_id is required")
        if scope not in WAIVER_SCOPES:
            raise HTTPException(status_code=422, detail="scope must be snapshot or review")
        facts = require_facts(session, revision)
        check = facts.policy.check(check_id)
        if check is None or check.mode != "error":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"{check_id!r} is not a blocking pre-merge check here"
            )
        actor = session.reviewer_id
        if not facts.policy.may_waive(check, actor):
            _activity(
                deps,
                session,
                revision,
                kind="premerge.waiver_refused",
                subject_type="premerge_check",
                subject_id=check.requirement_key,
                summary=f"{actor} was refused a waiver of {check.title}: not permitted by policy",
                detail={"check_id": check.id, "reason": reason[:500], "waivable": check.waivable},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"{check.title} is not waivable by policy"
                    if not check.waivable
                    else f"policy does not allow {actor} to waive {check.title}"
                ),
            )
        if not reason:
            raise HTTPException(status_code=422, detail="a waiver needs a reason")
        generation = deps.generation(session, revision)
        now = deps.now()
        result = next(
            item
            for item in evaluate_premerge(facts, generation=generation, now=now).results
            if item.check.id == check.id
        )
        if not result.applicable or result.status == "PASS":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"{check.title} is not blocking on this revision"
            )
        raw_hours = body.get("hours")
        if raw_hours is not None and (not isinstance(raw_hours, (int, float)) or isinstance(raw_hours, bool)):
            raise HTTPException(status_code=422, detail="hours must be a number")
        try:
            expires_at = waiver_expiry(
                now=now,
                hours=None if raw_hours is None else float(raw_hours),
                expires_at=str(body.get("expires_at") or "").strip(),
                max_hours=facts.policy.waivers.max_hours,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            record = deps.store.add_evidence(
                waiver_evidence(
                    review_id=session.id,
                    revision_id=revision.id,
                    check=check,
                    actor=actor,
                    reason=reason,
                    scope=scope,
                    expires_at=expires_at,
                    policy_generation=generation,
                    paths=facts.changed_paths,
                ),
                expected_current_revision_id=revision.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        _activity(
            deps,
            session,
            revision,
            kind="premerge.waiver_granted",
            subject_type="premerge_check",
            subject_id=check.requirement_key,
            summary=f"{actor} waived {check.title} until {expires_at} ({scope} scope): {reason[:200]}",
            detail={
                "check_id": check.id,
                "evidence_id": record.id,
                "reason": reason[:2000],
                "scope": scope,
                "expires_at": expires_at,
                "policy_generation": generation,
                "status_waived": result.status,
            },
        )
        await deps.enforce_armed_landing(session)
        return {"evidence_id": record.id, "premerge": await asyncio.to_thread(status_payload, session, revision)}

    @router.get("/premerge/trial")
    async def _get_premerge_trial(limit: int = _TRIAL_DEFAULT) -> dict[str, Any]:
        """What warning-mode checks would block across open Reviews if they were enforced.

        Read-only and deterministic: stored results only, no model call and no
        static analysis is started.
        """

        bounded = max(1, min(_TRIAL_MAX, int(limit)))

        def build() -> dict[str, Any]:
            # lc-debt: per-Review fact gathering over at most 100 open Reviews per
            # request; upgrade to an incremental per-Snapshot result cache if
            # trial reports are needed across larger fleets.
            reviews: list[dict[str, Any]] = []
            counts: dict[str, int] = {}
            unavailable = 0
            sessions = list(deps.open_sessions(bounded))
            for session in sessions:
                try:
                    revision = deps.latest(session)
                    facts = deps.facts(session, revision)
                except Exception:
                    unavailable += 1
                    continue
                if facts is None:
                    continue
                evaluation = evaluate_premerge(facts, generation=deps.generation(session, revision), now=deps.now())
                blocked = [item for item in evaluation.results if item.would_block]
                for item in blocked:
                    counts[item.check.id] = counts.get(item.check.id, 0) + 1
                reviews.append(
                    {
                        "review_id": session.id,
                        "title": session.title,
                        "snapshot_id": revision.id,
                        "snapshot_number": facts.snapshot_number,
                        "would_block": [
                            {
                                "id": item.check.id,
                                "title": item.check.title,
                                "status": item.status,
                                "detail": item.detail,
                            }
                            for item in blocked
                        ],
                    }
                )
            return {
                "limit": bounded,
                "examined": len(sessions),
                "unavailable": unavailable,
                "would_block": counts,
                "reviews": reviews,
            }

        return await asyncio.to_thread(build)


__all__ = ["PremergeDeps", "register_premerge_routes"]
