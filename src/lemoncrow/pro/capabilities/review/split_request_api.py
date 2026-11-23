"""Reader routes for split requests (Phase 12 item 13).

A split request reuses the Review action gates unchanged: the same mutable
session check, expected-revision check, routing tiers (exact authoring session,
then the preferred agent, then a copied prompt), delivery and Development
handoff link -- so it carries exactly the permissions of Fix, locally and
hosted. Collecting the agent's branches is the only new step, and it runs only
where this server holds the trusted checkout.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request, status
from lemoncrow_client.review_words import revision_name

from lemoncrow.pro.capabilities.review import viewer_lens
from lemoncrow.pro.capabilities.review.derived_records import DerivedRecordLedger, RecordConflict
from lemoncrow.pro.capabilities.review.review_actions import (
    ROUTE_TIERS,
    ActionRoute,
    RouteUnavailable,
    now_iso,
    resolve_route,
    sanitize_directive,
    text_digest,
)
from lemoncrow.pro.capabilities.review.review_actions_api import ReviewActionDeps, preferred_agent_route
from lemoncrow.pro.capabilities.review.split_request import (
    CANCELLABLE,
    COLLECTABLE,
    MAX_REPORTED_ITEMS,
    SPLIT_KIND,
    SPLIT_PROVIDER,
    SPLIT_RELATION_KIND,
    TERMINAL,
    SplitLayer,
    SplitPlan,
    SplitRefused,
    carry_decisions,
    plan_from_cohorts,
    render_split_handoff,
    split_id_for,
    split_identity,
)

_PREVIEW_KEYS = frozenset({"expected_revision_id", "route", "directive"})
_START_KEYS = _PREVIEW_KEYS | {"operation_id", "expected_handoff_digest", "copied"}
_COLLECT_KEYS = frozenset({"expected_plan_digest"})
_CANCEL_KEYS = frozenset({"reason"})
_ACCEPTED = frozenset({"sent", "consumed", "working", "queued", "leased"})
_STALE_COLLECT: Final[timedelta] = timedelta(minutes=10)
_MAX_EVENTS: Final[int] = 40


@dataclass(frozen=True)
class SplitDeps:
    actions: ReviewActionDeps
    """The Review action gates and seams, reused unchanged (same authority as Fix)."""
    cohorts: Callable[[Any, Any], tuple[Sequence[Mapping[str, Any]], Sequence[str]]]
    """``(cohorts in canonical Brief order, changed file paths)`` for one revision."""
    source_store_root: Path
    collect_reason: str = ""
    """Why produced branches cannot be collected here; empty with a trusted checkout."""


class SplitCollectError(RuntimeError):
    """The produced branches cannot become Reviews; nothing was kept."""


@dataclass(frozen=True)
class _Prepared:
    plan: SplitPlan | None
    refusal: str
    route: ActionRoute | None
    candidates: tuple[ActionRoute, ...]
    split_id: str
    identity: str
    handoff: str
    directive: str


def _refuse(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _strict(body: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unsupported split field(s): {', '.join(unknown)}")


def _git(repo: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, timeout=60)
    return result.returncode, result.stdout.strip()


def _event(kind: str, actor_type: str, actor_id: str = "", detail: str = "") -> dict[str, str]:
    return {"kind": kind, "at": now_iso(), "actor_type": actor_type, "actor_id": actor_id, "detail": detail[:500]}


def _with_event(record: dict[str, Any], event: Mapping[str, str], **fields: Any) -> dict[str, Any]:
    events = [*list(record.get("events") or ()), dict(event)][-_MAX_EVENTS:]
    return {**record, **fields, "events": events, "updated_at": now_iso()}


def validate_branches(repo: Path, plan: SplitPlan) -> list[dict[str, Any]]:
    """Each layer's ``base``/``head`` commit and changed paths, or :class:`SplitCollectError`.

    The layers must exist, each must add commits on top of the one below (the
    first on the original base), and the last must reproduce the original head
    tree exactly -- the produced Changes together are the reviewed Change.
    """

    heads: list[str] = []
    missing: list[str] = []
    for layer in plan.layers:
        code, sha = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{layer.branch}^{{commit}}")
        if code != 0 or not sha:
            missing.append(layer.branch)
        heads.append(sha)
    if missing:
        raise SplitCollectError(f"the agent has not created {', '.join(missing)} yet")
    out: list[dict[str, Any]] = []
    previous = plan.base_sha
    for layer, head in zip(plan.layers, heads, strict=True):
        if head == previous:
            raise SplitCollectError(f"layer {layer.index} ({layer.branch}) adds no commits")
        code, _ = _git(repo, "merge-base", "--is-ancestor", previous, head)
        if code != 0:
            below = "the original base" if layer.index == 1 else f"layer {layer.index - 1}"
            raise SplitCollectError(f"layer {layer.index} ({layer.branch}) is not stacked on {below}")
        code, changed = _git(repo, "diff", "--name-only", "--no-renames", previous, head)
        paths = sorted(item for item in changed.splitlines() if item)
        planned = set(layer.paths)
        out.append(
            {
                "layer": layer,
                "base": previous,
                "head": head,
                "extra_paths": [path for path in paths if path not in planned][:50],
                "missing_paths": [path for path in layer.paths if path not in set(paths)][:50],
            }
        )
        previous = head
    _code, final_tree = _git(repo, "rev-parse", f"{previous}^{{tree}}")
    _code, head_tree = _git(repo, "rev-parse", f"{plan.head_sha}^{{tree}}")
    if not final_tree or final_tree != head_tree:
        raise SplitCollectError(
            f"the last layer does not reproduce the reviewed commit {plan.head_sha[:12]} exactly; "
            "the stacked Changes must add up to the original"
        )
    return out


def register_split_request_routes(router: APIRouter, deps: SplitDeps) -> None:
    """Add the split request routes to *router*."""

    from lemoncrow.pro.capabilities.review import delivery as delivery_module

    actions = deps.actions
    store = actions.store
    ledger = DerivedRecordLedger(store, SPLIT_KIND)

    def revision_ids(review_id: str) -> list[str]:
        return [item.id for item in store.list_revisions(review_id)]

    def records(review_id: str) -> list[tuple[str, dict[str, Any]]]:
        rows = [(rev, payload) for rev, _stamp, payload in ledger.rows(revision_ids(review_id))]
        rows.sort(key=lambda item: (str(item[1].get("created_at") or ""), str(item[1].get("id") or "")))
        return rows

    def hidden_of(session: Any) -> frozenset[str]:
        return frozenset() if actions.hidden is None else actions.hidden(session)

    def find(session: Any, split_id: str) -> tuple[str, dict[str, Any]]:
        """A split by its id; one a hidden person started is missing, as an id that never existed (R15)."""

        found = viewer_lens.reachable(
            hidden_of(session),
            ledger.get(revision_ids(session.id), split_id),
            lambda found: str(found[1].get("created_by") or ""),
        )
        if found is None or found[1].get("review_id") != session.id:
            raise _refuse(status.HTTP_404_NOT_FOUND, "no such split request")
        return found

    def busy_words(busy: Mapping[str, Any], session: Any) -> str:
        """Which split is in flight, unless a hidden person started it: a refusal that names no one."""

        if str(busy.get("created_by") or "") in hidden_of(session):
            return "another split of this Review is still in progress"
        return f"split {busy.get('id')} is still {busy.get('status')}"

    def transition(
        review_id: str, revision_id: str, split_id: str, allowed: frozenset[str], **fields: Any
    ) -> dict[str, Any]:
        event = fields.pop("event")

        def change(current: dict[str, Any]) -> dict[str, Any]:
            if str(current.get("status")) not in allowed:
                raise RecordConflict(f"this split is {current.get('status')}")
            return _with_event(current, event, **fields)

        return ledger.update(review_id, revision_id, split_id, change)

    def exact_route(revision: Any) -> ActionRoute:
        author = actions.author_route(revision, "")
        if not author.get("supported") or not author.get("host") or not author.get("session_id"):
            reason = str(author.get("reason") or "") or "no exact authoring session is recorded for this Change"
            return ActionRoute("exact_session", "resume", False, reason=reason)
        host, session_id = str(author["host"]), str(author["session_id"])
        label = f"{actions.host_label(host)} {session_id[:4]}".strip()
        if actions.no_local_checkout:
            return ActionRoute(
                "exact_session", "resume", False, host, session_id, label, reason=actions.no_local_checkout
            )
        return ActionRoute("exact_session", "resume", True, host, session_id, f"{label} · wrote this code")

    def prepare(session: Any, revision: Any, body: Mapping[str, Any]) -> _Prepared:
        requested_raw = str(body.get("route") or "")
        if (requested_raw and requested_raw not in ROUTE_TIERS) or requested_raw == "reviewer_apply":
            raise _refuse(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "route is exact_session, preferred_agent or prompt_fallback"
            )
        try:
            directive = sanitize_directive(body.get("directive"))
        except ValueError as exc:
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        existing = records(session.id)
        short = hashlib.sha256(f"{session.id}\0{len(existing)}".encode()).hexdigest()[:10]
        cohorts, changed = deps.cohorts(session, revision)
        try:
            plan = plan_from_cohorts(
                cohorts,
                changed,
                review_short=short,
                base_sha=str(revision.base_sha or ""),
                head_sha=str(revision.head_sha or ""),
            )
        except SplitRefused as exc:
            return _Prepared(None, str(exc), None, (), "", "", "", directive)
        seed = "\0".join((session.id, revision.id, "split", plan.digest))
        try:
            decision = resolve_route(
                exact_route(revision),
                preferred_agent_route(actions, session, seed),
                requested=requested_raw or None,  # type: ignore[arg-type]
            )
        except RouteUnavailable as exc:
            raise _refuse(status.HTTP_409_CONFLICT, str(exc)) from exc
        identity = split_identity(
            review_id=session.id,
            revision_id=revision.id,
            plan=plan,
            route=decision.route.to_wire(),
            directive=directive,
        )
        attempt = sum(1 for _rev, item in existing if item.get("identity") == identity)
        split_id = split_id_for(identity, attempt)
        handoff = render_split_handoff(
            plan,
            split_id=split_id,
            reviewer_id=str(session.reviewer_id),
            snapshot_label=revision_name(actions.display_number(revision)),
            change_description=str(session.title or ""),
            directive=directive,
        )
        return _Prepared(plan, "", decision.route, decision.candidates, split_id, identity, handoff, directive)

    def in_flight(review_id: str) -> dict[str, Any] | None:
        return next(
            (item for _rev, item in reversed(records(review_id)) if str(item.get("status")) not in TERMINAL), None
        )

    def preview_payload(
        session: Any, revision: Any, prepared: _Prepared, busy: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        reason = prepared.refusal or (busy_words(busy, session) if busy else "")
        return {
            "revision_id": revision.id,
            "operation_id": prepared.split_id,
            "ready": prepared.plan is not None and prepared.route is not None and not busy,
            "reason": reason,
            "plan": prepared.plan.to_wire() if prepared.plan is not None else None,
            "route": prepared.route.to_wire() if prepared.route is not None else None,
            "candidates": [item.to_wire() for item in prepared.candidates],
            "handoff": prepared.handoff,
            "handoff_digest": text_digest(prepared.handoff) if prepared.handoff else "",
            "collect_available": not deps.collect_reason,
            "collect_reason": deps.collect_reason,
        }

    def child_ref(review_id: str) -> dict[str, Any]:
        child = store.get_session(review_id)
        return {
            "review_id": review_id,
            "title": str(child.title) if child is not None else "",
            "status": str(child.status) if child is not None else "gone",
        }

    def wire(revision_id: str, record: Mapping[str, Any], numbers: Mapping[str, int]) -> dict[str, Any]:
        children = [
            {**dict(item), **child_ref(str(item.get("review_id") or ""))}
            for item in record.get("children") or ()
            if isinstance(item, Mapping)
        ]
        return {
            **{key: value for key, value in record.items() if key not in {"identity", "children"}},
            "children": children,
            "source_revision_number": numbers.get(str(record.get("source_revision_id") or "")),
            "stored_on_revision_id": revision_id,
        }

    def split_from(session: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in store.list_relationships(session.id, kind=SPLIT_RELATION_KIND):
            if item.relation != "split_from" or item.provider != SPLIT_PROVIDER:
                continue
            try:
                meta = json.loads(item.metadata_json or "{}")
            except ValueError:
                meta = {}
            out.append(
                {
                    "review_id": item.target_review_id,
                    "split_id": item.external_id,
                    "layer": meta.get("layer"),
                    "layer_count": meta.get("layer_count"),
                    "branch": meta.get("branch"),
                }
            )
        return out

    async def dispatch(session: Any, revision_id: str, record: dict[str, Any], handoff: str) -> dict[str, Any]:
        route = ActionRoute.from_wire(record["route"])
        if route.mode == "new_session":
            result = await asyncio.to_thread(
                delivery_module.launch_agent_session,
                route.host,
                actions.source_repo,
                handoff,
                session_id=route.session_id,
                delivery_id=str(record["id"]),
                log_dir=actions.log_dir,
                wrap_review_feedback=False,
            )
            if result.session_id:
                route = replace(route, session_id=result.session_id)
        else:
            result = await asyncio.to_thread(
                delivery_module.deliver_prompt_to_agent_session,
                route.host,
                route.session_id,
                actions.source_repo,
                handoff,
            )
        state = str(result.state)
        if state in _ACCEPTED:
            next_status, kind = "awaiting_branches", "dispatched"
        elif state == "uncertain":
            next_status, kind = "uncertain", "delivery_uncertain"
        else:
            next_status, kind = "failed", "delivery_failed"
        updated = transition(
            session.id,
            revision_id,
            str(record["id"]),
            frozenset({"dispatching"}),
            event=_event(kind, "system", detail=f"{state}: {result.message}".strip(": ")),
            status=next_status,
            route=route.to_wire(),
            message=str(result.message or ""),
        )
        if state in _ACCEPTED and route.host and route.session_id:
            from lemoncrow.pro.capabilities.review.development import record_feedback_handoff

            with contextlib.suppress(Exception):
                record_feedback_handoff(
                    actions.development_store,
                    session.id,
                    host=route.host,
                    session_id=route.session_id,
                    operation_id=str(record["id"]),
                    annotations=[("", f"split into {len(record['plan']['layers'])} stacked Changes")],
                    state=state,
                    causation_id=f"split:{record['id']}",
                    label="Review split request sent",
                )
        return updated

    # ------------------------------------------------------------------ #
    # collect: branches -> linked Reviews, exact judgments carried
    # ------------------------------------------------------------------ #

    def capture_layers(original: Any, record: Mapping[str, Any]) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
        from lemoncrow.pro.capabilities.review.session_models import ReviewMark, ReviewRelationship, ReviewSession
        from lemoncrow.pro.capabilities.review.sources.local import refresh

        plan = SplitPlan.from_wire(record["plan"])
        source_revision = store.get_revision(str(record["source_revision_id"]))
        if source_revision is None:
            raise SplitCollectError("the revision this split was planned from no longer exists")
        layers = validate_branches(actions.source_repo, plan)
        count = len(layers)
        created: list[str] = []
        written: list[Any] = []
        children: list[dict[str, Any]] = []
        try:
            for item in layers:
                layer: SplitLayer = item["layer"]
                rng = resolve_rev_range(actions.source_repo, None, base=item["base"], head=item["head"])
                title = f"Split {layer.index}/{count} · {layer.title}"[:120]
                child, made = store.find_or_create_session(
                    ReviewSession(
                        id="",
                        subject_type="commit_range",
                        repo_root=original.repo_root,
                        range_mode="commit_range",
                        title=title,
                        source_ref=f"{item['base']}..{item['head']}",
                        review_base_sha=item["base"],
                        reviewer_id=original.reviewer_id,
                    ),
                    reuse=("open",),
                )
                if not made:
                    raise SplitCollectError(f"layer {layer.index} is already an open Review ({child.id})")
                created.append(child.id)
                result = refresh(
                    store,
                    child,
                    actions.source_repo,
                    store_root=deps.source_store_root,
                    rng=rng,
                    limit=5000,
                    reviewer_id=original.reviewer_id,
                )
                store.publish_snapshot(child.id, result.revision.id)
                children.append(
                    {
                        "layer": layer.index,
                        "cohort_id": layer.cohort_id,
                        "branch": layer.branch,
                        "base_sha": item["base"],
                        "head_sha": item["head"],
                        "review_id": child.id,
                        "revision_id": result.revision.id,
                        "extra_paths": item["extra_paths"],
                        "missing_paths": item["missing_paths"],
                    }
                )
            for index, child in enumerate(children):
                meta = {
                    "split_id": record["id"],
                    "layer": child["layer"],
                    "layer_count": count,
                    "cohort_id": child["cohort_id"],
                    "branch": child["branch"],
                    "source_revision_id": source_revision.id,
                }
                store.upsert_relationship(
                    ReviewRelationship(
                        id="",
                        review_id=child["review_id"],
                        kind=SPLIT_RELATION_KIND,
                        relation="split_from",
                        provider=SPLIT_PROVIDER,
                        external_id=str(record["id"]),
                        target_review_id=original.id,
                        title=f"layer {child['layer']} of {count}",
                        status="linked",
                        source="split",
                        metadata_json=json.dumps(meta, sort_keys=True),
                    )
                )
                if index:
                    store.upsert_relationship(
                        ReviewRelationship(
                            id="",
                            review_id=child["review_id"],
                            kind=SPLIT_RELATION_KIND,
                            relation="depends_on",
                            provider=SPLIT_PROVIDER,
                            external_id=str(record["id"]),
                            target_review_id=children[index - 1]["review_id"],
                            title=f"stacked on layer {children[index - 1]['layer']}",
                            status="linked",
                            source="split",
                            metadata_json=json.dumps(meta, sort_keys=True),
                        )
                    )
            migration = migrate_marks(original, source_revision, children, ReviewMark)
            for child in children:
                written.append(
                    store.upsert_relationship(
                        ReviewRelationship(
                            id="",
                            review_id=original.id,
                            kind=SPLIT_RELATION_KIND,
                            relation="split_into",
                            provider=SPLIT_PROVIDER,
                            external_id=str(record["id"]),
                            target_review_id=child["review_id"],
                            title=f"layer {child['layer']} of {count}",
                            status="linked",
                            source="split",
                            metadata_json=json.dumps(
                                {"split_id": record["id"], "layer": child["layer"], "layer_count": count},
                                sort_keys=True,
                            ),
                        )
                    )
                )
        except BaseException:
            rollback(created, written)
            raise
        return {"children": children, "migration": migration}

    def rollback(created: Sequence[str], written: Sequence[Any]) -> None:
        """Remove only what this collect created; the original keeps nothing of it."""

        for relationship in written:
            with contextlib.suppress(Exception):
                store.update_relationship_if_unchanged(
                    relationship, status="abandoned", metadata_json=relationship.metadata_json
                )
        for review_id in created:
            with contextlib.suppress(Exception):
                store.update_session(review_id, status="archived")
                store.delete_archived_session(review_id)

    def migrate_marks(
        original: Any, source_revision: Any, children: Sequence[Mapping[str, Any]], mark_type: Any
    ) -> dict[str, Any]:
        original_units = store.list_units(source_revision.id)
        carried = 0
        per_child: dict[str, int] = {}
        outcome: dict[tuple[str, str], str] = {}
        for reviewer_id in store.list_reviewer_ids(original.id):
            marks = store.list_marks(original.id, reviewer_id=reviewer_id)
            for child in children:
                child_units = store.list_units(str(child["revision_id"]))
                present = {mark.unit_key for mark in store.list_marks(str(child["review_id"]), reviewer_id=reviewer_id)}
                for decision in carry_decisions(
                    original_units, child_units, marks, child_review_id=str(child["review_id"])
                ):
                    key = (reviewer_id, str(decision.mark.unit_key))
                    if not decision.carried:
                        if outcome.get(key) != "carried" and (
                            key not in outcome or decision.reason == "the target changed"
                        ):
                            outcome[key] = decision.reason
                        continue
                    outcome[key] = "carried"
                    if decision.mark.unit_key in present:
                        continue  # never overwrite a judgment already on the produced Change
                    mark = decision.mark
                    store.set_mark(
                        mark_type(
                            review_id=str(child["review_id"]),
                            unit_key=mark.unit_key,
                            state=mark.state,
                            reviewed_revision_id=str(child["revision_id"]),
                            content_fingerprint=mark.content_fingerprint,
                            reviewer_id=reviewer_id,
                            actor_type="human",
                            note=mark.note,
                        ),
                        event_kind="migrated",
                        event_revision_id=str(child["revision_id"]),
                        event_reason=(
                            f"carried by split from {original.id} {revision_name(source_revision.revision_number - 1)}: "
                            "identical part and content fingerprint"
                        ),
                    )
                    carried += 1
                    per_child[str(child["review_id"])] = per_child.get(str(child["review_id"]), 0) + 1
        not_carried = [
            {"reviewer_id": reviewer, "unit_key": unit_key, "reason": reason}
            for (reviewer, unit_key), reason in sorted(outcome.items())
            if reason != "carried"
        ]
        return {
            "carried": carried,
            "per_child": per_child,
            "not_carried_total": len(not_carried),
            "not_carried": not_carried[:MAX_REPORTED_ITEMS],
        }

    # ------------------------------------------------------------------ #
    # routes
    # ------------------------------------------------------------------ #

    @router.get("/reviews/{review_id}/split-requests")
    def _get_split_requests(review_id: str) -> dict[str, Any]:
        session = actions.session(review_id)
        numbers = {item.id: actions.display_number(item) for item in store.list_revisions(session.id)}
        return {
            "splits": [
                wire(rev, item, numbers)
                for rev, item in viewer_lens.visible(
                    session.id, records(session.id), lambda row: str(row[1].get("created_by") or "")
                )
            ][::-1],
            "split_from": split_from(session),
            "collect_available": not deps.collect_reason,
            "collect_reason": deps.collect_reason,
        }

    @router.post("/reviews/{review_id}/split-requests/preview")
    async def _post_split_preview(review_id: str, request: Request) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.api import _json_object

        session = actions.mutable_session(review_id)
        revision = actions.latest(session)
        body = await _json_object(request)
        _strict(body, _PREVIEW_KEYS)
        actions.require_expected_revision(body, revision, "preparing this split")
        prepared = await run_in_threadpool(prepare, session, revision, body)
        return preview_payload(session, revision, prepared, in_flight(session.id))

    @router.post("/reviews/{review_id}/split-requests")
    async def _post_split_request(review_id: str, request: Request) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.api import _json_object

        session = actions.mutable_session(review_id)
        revision = actions.latest(session)
        body = await _json_object(request)
        _strict(body, _START_KEYS)
        operation_id = str(body.get("operation_id") or "").strip()
        if not operation_id:
            raise _refuse(status.HTTP_400_BAD_REQUEST, "starting a split requires operation_id from its preview")
        numbers = {item.id: actions.display_number(item) for item in store.list_revisions(session.id)}
        existing = viewer_lens.reachable(
            hidden_of(session),
            ledger.get(revision_ids(session.id), operation_id),
            lambda found: str(found[1].get("created_by") or ""),
        )
        if existing is not None:
            return {"split": wire(existing[0], existing[1], numbers), "replayed": True}
        actions.require_expected_revision(body, revision, "starting this split")
        prepared = await run_in_threadpool(prepare, session, revision, body)
        if prepared.plan is None or prepared.route is None:
            raise _refuse(status.HTTP_409_CONFLICT, prepared.refusal or "this Change cannot be split")
        if prepared.split_id != operation_id or text_digest(prepared.handoff) != str(
            body.get("expected_handoff_digest") or ""
        ):
            raise _refuse(
                status.HTTP_409_CONFLICT,
                "this split changed since its preview; review the updated preview before starting it",
            )
        busy = in_flight(session.id)
        if busy is not None:
            raise _refuse(status.HTTP_409_CONFLICT, busy_words(busy, session))
        route = prepared.route
        if route.mode == "copy" and body.get("copied") is not True:
            raise _refuse(
                status.HTTP_409_CONFLICT, "copy the split prompt for the agent, then confirm the handoff (copied: true)"
            )
        actor = str(session.reviewer_id)
        at = now_iso()
        events = [_event("created", "human", actor, f"{len(prepared.plan.layers)} stacked layers")]
        initial = "dispatching"
        if route.mode == "copy":
            initial = "awaiting_branches"
            events.append(_event("handed_off", "human", actor, "copied for the agent"))
        record = {
            "id": prepared.split_id,
            "review_id": session.id,
            "source_revision_id": revision.id,
            "status": initial,
            "identity": prepared.identity,
            "route": route.to_wire(),
            "plan": prepared.plan.to_wire(),
            "handoff_digest": text_digest(prepared.handoff),
            "directive": prepared.directive,
            "created_by": actor,
            "created_at": at,
            "updated_at": at,
            "message": "copied for the agent; collect its branches when they are ready" if route.mode == "copy" else "",
            "events": events,
            "children": [],
            "migration": None,
        }
        created, stored = ledger.create(session.id, revision.id, record)
        if created and stored.get("status") == "dispatching":
            stored = await dispatch(session, revision.id, stored, prepared.handoff)
        return {"split": wire(revision.id, stored, numbers), "replayed": not created}

    @router.post("/reviews/{review_id}/split-requests/{split_id}/collect")
    async def _post_split_collect(review_id: str, split_id: str, request: Request) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.api import _json_object

        session = actions.mutable_session(review_id)
        raw = await request.body()
        body = await _json_object(request) if raw.strip() else {}
        _strict(body, _COLLECT_KEYS)
        if deps.collect_reason:
            raise _refuse(status.HTTP_409_CONFLICT, deps.collect_reason)
        revision_id, record = find(session, split_id)
        expected = str(body.get("expected_plan_digest") or "")
        if expected and expected != str((record.get("plan") or {}).get("digest") or ""):
            raise _refuse(status.HTTP_409_CONFLICT, "this split's plan is not the one you reviewed")
        if actions.latest(session).id != record.get("source_revision_id"):
            raise _refuse(
                status.HTTP_409_CONFLICT,
                "this Review has a newer revision than the split was planned from; request a new split",
            )
        allowed = COLLECTABLE
        updated_at = str(record.get("updated_at") or "")
        with contextlib.suppress(ValueError):
            stamp = datetime.fromisoformat(updated_at)
            stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
            if record.get("status") == "collecting" and datetime.now(UTC) - stamp > _STALE_COLLECT:
                allowed = COLLECTABLE | {"collecting"}  # a collect that died mid-way may be retried
        previous = str(record.get("status"))
        try:
            claimed = transition(
                session.id,
                revision_id,
                split_id,
                frozenset(allowed),
                event=_event("collecting", "human", str(session.reviewer_id)),
                status="collecting",
            )
        except RecordConflict as exc:
            raise _refuse(status.HTTP_409_CONFLICT, f"cannot collect: {exc}") from exc
        try:
            outcome = await run_in_threadpool(capture_layers, session, claimed)
        except (SplitCollectError, ValueError, OSError, subprocess.SubprocessError) as exc:
            message = str(exc)
            back = previous if previous in COLLECTABLE else "awaiting_branches"
            transition(
                session.id,
                revision_id,
                split_id,
                frozenset({"collecting"}),
                event=_event("collect_failed", "system", detail=message),
                status=back,
                message=f"not collected: {message}",
            )
            raise _refuse(status.HTTP_409_CONFLICT, f"not collected: {message}") from exc
        done = transition(
            session.id,
            revision_id,
            split_id,
            frozenset({"collecting"}),
            event=_event("completed", "system", detail=f"{len(outcome['children'])} linked Reviews"),
            status="completed",
            children=outcome["children"],
            migration=outcome["migration"],
            message=(
                f"{len(outcome['children'])} stacked Reviews linked to this one; "
                f"{outcome['migration']['carried']} judgments carried on exactly matching targets"
            ),
        )
        numbers = {item.id: actions.display_number(item) for item in store.list_revisions(session.id)}
        return {"split": wire(revision_id, done, numbers)}

    @router.post("/reviews/{review_id}/split-requests/{split_id}/cancel")
    async def _post_split_cancel(review_id: str, split_id: str, request: Request) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.api import _json_object

        session = actions.mutable_session(review_id)
        raw = await request.body()
        body = await _json_object(request) if raw.strip() else {}
        _strict(body, _CANCEL_KEYS)
        revision_id, _record = find(session, split_id)
        reason = str(body.get("reason") or "").strip()[:500]
        try:
            updated = transition(
                session.id,
                revision_id,
                split_id,
                CANCELLABLE,
                event=_event("cancelled", "human", str(session.reviewer_id), reason),
                status="cancelled",
                message="cancelled in LemonCrow; a running agent is not stopped and its branches are not collected",
            )
        except RecordConflict as exc:
            raise _refuse(status.HTTP_409_CONFLICT, f"cannot cancel: {exc}") from exc
        numbers = {item.id: actions.display_number(item) for item in store.list_revisions(session.id)}
        return {"split": wire(revision_id, updated, numbers)}


__all__ = ["SplitCollectError", "SplitDeps", "register_split_request_routes", "validate_branches"]
