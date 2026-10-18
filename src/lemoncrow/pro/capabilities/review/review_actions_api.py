"""HTTP routes for controlled Review actions (Phase 12, P12-FIX).

Registered by ``register_review_api`` through one small call, so the hotspot
only wires its existing gates (session scope, mutability, expected revision,
exact-author routing, local-checkout policy, proposal apply) into this module.
Every code-changing action reuses the exact delivery adapters, command lines
and permission gates that human feedback delivery and proposal apply use: an
action gets no permission a human handoff would not get.

Routes (all under the Review API prefix, all authenticated by the router):

``GET  /reviews/{id}/actions``                   actions, provenance, catalog
``POST /reviews/{id}/actions/preview``           route, included/excluded, exact handoff
``POST /reviews/{id}/actions``                   start one previewed action, once
``POST /reviews/{id}/actions/{action_id}/cancel`` close an in-flight action
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, status

from lemoncrow.pro.capabilities.review.fix_agent import FIX_AGENT_UNSET_REASON, FixAgentPreference
from lemoncrow.pro.capabilities.review.review_actions import (
    CODE_CHANGING_KINDS,
    CROSS_CHANGE_KINDS,
    FIX_ALL_LIMIT,
    IN_FLIGHT_STATUSES,
    KIND_LABELS,
    PROMPT_FALLBACK,
    REVIEW_ACTION_KINDS,
    ROUTE_TIERS,
    UNSUPPORTED_KINDS,
    ActionConflict,
    ActionEvent,
    ActionRoute,
    ActionSubject,
    CatalogInputs,
    DataOrigin,
    HandoffDocument,
    HandoffItem,
    ReviewAction,
    ReviewActionKind,
    ReviewActionLedger,
    ReviewActionStatus,
    RouteDecision,
    RouteTier,
    RouteUnavailable,
    SubjectCandidate,
    SubjectExclusion,
    SubjectPartition,
    action_catalog,
    action_id_for,
    action_identity,
    correction_changed_paths,
    exactly_attributed,
    finding_exclusion,
    now_iso,
    partition_subjects,
    proposal_fingerprints,
    render_handoff,
    resolve_results,
    resolve_route,
    sanitize_directive,
    saved_snapshots,
    snapshot_refs,
    text_digest,
)
from lemoncrow.pro.capabilities.review.stack_awareness import OwnershipResolver, OwningChange, ownership_resolver

_PREVIEW_KEYS = frozenset({"kind", "subject_ids", "expected_revision_id", "route", "directive"})
_START_KEYS = _PREVIEW_KEYS | {"operation_id", "expected_handoff_digest", "copied"}
_CANCEL_KEYS = frozenset({"expected_revision_id", "reason"})
_MAX_SUBJECT_IDS = 200
_DISPATCH_IN_DOUBT_AFTER = timedelta(minutes=10)
_RERUN_LIMIT = 5
_ACCEPTED = frozenset({"sent", "consumed", "working", "queued", "leased"})


@dataclass(frozen=True)
class ReviewActionDeps:
    """The Review API gates and seams an action reuses; nothing here is new authority."""

    store: Any
    development_store: Any
    session: Callable[[str], Any]
    mutable_session: Callable[[str], Any]
    latest: Callable[[Any], Any]
    require_expected_revision: Callable[[Mapping[str, Any], Any, str], None]
    exact_routes: Callable[[Any, Sequence[str], Mapping[str, Any]], tuple[dict[str, dict[str, str]], tuple[str, ...]]]
    """Per-annotation exact responsible session (feedback's ``_delivery_routes``)."""
    author_route: Callable[[Any, str], Mapping[str, Any]]
    """Exact author of one path, or of the revision when the path is empty (``_delivery_capability``)."""
    behavior_targets: Callable[[Any, Any], Sequence[Any]]
    run_plan: Callable[[Any, Any], Mapping[str, Any]]
    """The effective R1 plan wire for a Snapshot: the same plan GET run-plan shows."""
    apply_proposal: Callable[[str], Awaitable[Mapping[str, Any]]]
    """The existing proposal apply route: its source-mutation policy, unchanged."""
    display_number: Callable[[Any], int]
    host_label: Callable[[str], str]
    source_repo: Path
    log_dir: Path
    no_local_checkout: str = ""
    """Why agent sessions are unreachable from this server; empty on a local checkout."""
    source_mutation_reason: str = ""
    """Why reviewed source cannot be written by this server; empty when it can."""
    preferred_agent: Callable[[Any], FixAgentPreference] | None = None
    """The reviewer's personal preferred fixing agent (route step 2); ``None`` where none can be read."""
    production_outcomes: Callable[[Any], Sequence[Any] | None] | None = None
    """A merged Review's production problems (P15-OBS Revert / Fix forward subjects); ``None`` -- or a
    callable returning ``None`` -- when no deployment or observability connector is configured."""


@dataclass(frozen=True)
class _Prepared:
    kind: ReviewActionKind
    partition: SubjectPartition
    decision: RouteDecision | None
    items: tuple[HandoffItem, ...]
    directive: str
    identity: str
    action_id: str
    handoff: str
    failures: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return bool(self.partition.included) and self.decision is not None and self.decision.route.available


def _refuse(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _strict_body(body: Mapping[str, Any], allowed: frozenset[str]) -> None:
    """Unknown fields fail closed: no tool, policy or prompt override rides along."""

    unknown = sorted(set(body) - allowed)
    if unknown:
        raise _refuse(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unsupported Review action field(s): {', '.join(unknown)}",
        )


def _one_line(text: str, limit: int = 160) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def _preferred_agent_preference(deps: ReviewActionDeps, session: Any) -> FixAgentPreference:
    if deps.preferred_agent is None:
        return FixAgentPreference("", "unset", FIX_AGENT_UNSET_REASON)
    return deps.preferred_agent(session)


def preferred_agent_route(deps: ReviewActionDeps, session: Any, seed: str) -> ActionRoute:
    """Routing step 2: this Change's chosen destination, else the reviewer's preferred agent.

    Shared by Review actions and split requests (P12-AGENT), so both route the
    same way under the same policy.
    """

    from lemoncrow.pro.capabilities.review import delivery as delivery_module

    if deps.no_local_checkout:
        return ActionRoute("preferred_agent", "new_session", False, reason=deps.no_local_checkout)
    destination = deps.store.get_destination(session.id)
    if destination.kind == "session" and destination.host and destination.session_id:
        host = str(destination.host)
        supported = delivery_module.direct_delivery_supported(host)
        return ActionRoute(
            "preferred_agent",
            "resume",
            supported,
            host=host,
            session_id=str(destination.session_id),
            label=f"{deps.host_label(host)} {str(destination.session_id)[:4]} · chosen for this Change",
            reason="" if supported else f"direct delivery is not configured for {host}",
        )
    # A destination pinned for this Change is more specific than the
    # reviewer's personal default; otherwise the personal setting decides.
    host = str(destination.host) if destination.kind == "new_session" else ""
    origin = "chosen for this Change"
    if not host:
        preference = _preferred_agent_preference(deps, session)
        if not preference.host:
            return ActionRoute("preferred_agent", "new_session", False, reason=preference.detail)
        host, origin = preference.host, preference.detail
    supported, reason = delivery_module.new_session_supported(host)
    new_id = delivery_module.new_session_id(host, seed)
    return ActionRoute(
        "preferred_agent",
        "new_session",
        supported,
        host=host,
        session_id="" if new_id == delivery_module.PENDING_SESSION_ID else new_id,
        label=f"new {deps.host_label(host)} session · {origin}",
        reason=reason,
    )


def register_review_action_routes(router: APIRouter, deps: ReviewActionDeps) -> None:
    """Add the Review action routes to *router*."""

    from lemoncrow.pro.capabilities.review import delivery as delivery_module

    store = deps.store
    ledger = ReviewActionLedger(store)

    def revision_ids(review_id: str) -> list[str]:
        return [item.id for item in store.list_revisions(review_id)]

    def actions_for(review_id: str) -> tuple[ReviewAction, ...]:
        return ledger.list(revision_ids(review_id))

    # ------------------------------------------------------------------ #
    # subjects
    # ------------------------------------------------------------------ #

    def in_flight_subjects(actions: Sequence[ReviewAction]) -> set[str]:
        return {subject.id for action in actions if action.in_flight for subject in action.subjects}

    def annotation_subject(annotation: Any, revision: Any) -> ActionSubject:
        source = str(annotation.source)
        kind = {
            "ai_review": "ai_finding",
            "lemoncrow": "deterministic_finding",
            "human": "human_comment",
            "author": "author_comment",
        }.get(source, "unknown")
        anchor = annotation.anchor
        return ActionSubject(
            kind=cast(Any, kind),
            id=annotation.id,
            title=_one_line(annotation.title or annotation.body),
            path=str(anchor.path or ""),
            start_line=int(anchor.start_line or 0),
            end_line=int(anchor.end_line or 0),
            revision_id=revision.id,
            state=str(annotation.finding_disposition or annotation.state),
        )

    def anchored(annotation: Any) -> bool:
        return annotation.state not in ("orphaned", "obsolete") and annotation.anchor_method not in (
            "orphaned",
            "removed",
            "unresolved",
        )

    def fixer_for_route(route: Mapping[str, Any] | None) -> tuple[str, str]:
        if route is None:
            return "nonexact", "your preferred agent or a copied prompt"
        host = str(route.get("host") or "")
        session_id = str(route.get("session_id") or "")
        if not host or not session_id:
            return "nonexact", "your preferred agent or a copied prompt"
        return f"exact:{host}:{session_id}", f"{deps.host_label(host)} {session_id[:4]}".strip()

    def candidates_for(
        session: Any,
        revision: Any,
        kind: ReviewActionKind,
        subject_ids: Sequence[str],
        requested: RouteTier | None,
        actions: Sequence[ReviewAction],
    ) -> tuple[list[SubjectCandidate], dict[str, HandoffItem], dict[str, tuple[str, str, str]], list[str]]:
        """Resolve requested ids to subjects, their own exclusions, fixers and handoff data."""

        busy = in_flight_subjects(actions)
        candidates: list[SubjectCandidate] = []
        items: dict[str, HandoffItem] = {}
        exact: dict[str, tuple[str, str, str]] = {}
        failures: list[str] = []
        behavior: dict[str, Any] | None = None
        evidence: dict[str, Any] | None = None
        tiered = requested in {"preferred_agent", "prompt_fallback"}
        ownership: list[OwnershipResolver] = []
        outcomes: dict[str, Any] | None = None

        def owner_of(annotation: Any) -> OwningChange | None:
            """Item 24: the lower stack layer that introduced the anchored code, if any."""

            if annotation.comment_scope == "change" or not anchored(annotation):
                return None
            if not ownership:
                ownership.append(ownership_resolver(store, session, revision))
            anchor = annotation.anchor
            return ownership[0].resolve(
                path=str(anchor.path or ""), side=str(anchor.side), start_line=int(anchor.start_line or 0)
            )

        def fixer(route: Mapping[str, Any] | None, subject_id: str) -> tuple[str, str]:
            if tiered:
                return str(requested), "the chosen fixer"
            key, label = fixer_for_route(route)
            if route is not None and key.startswith("exact:"):
                exact[subject_id] = (str(route["host"]), str(route["session_id"]), label)
            return key, label

        for subject_id in subject_ids:
            if subject_id.startswith("outcome:"):
                if outcomes is None:
                    outcomes = {item.id: item for item in production_outcomes_of(session) or ()}
                found = outcomes.get(subject_id)
                if found is None:
                    candidates.append(
                        SubjectCandidate(
                            ActionSubject("production_outcome", subject_id),
                            SubjectExclusion(
                                subject_id, "production_outcome", "unknown_subject", "not a current production outcome"
                            ),
                        )
                    )
                    continue
                subject = ActionSubject(
                    kind="production_outcome",
                    id=subject_id,
                    title=_one_line(found.title),
                    revision_id=found.revision_id,
                    state=found.kind,
                )
                outcome_route = deps.author_route(revision, "")
                if not outcome_route.get("supported") and outcome_route.get("reason"):
                    failures.append(str(outcome_route["reason"]))
                key, label = fixer(outcome_route if outcome_route.get("supported") else None, subject_id)
                items[subject_id] = HandoffItem(subject, "behavior_observation", str(found.detail))
                busy_outcome = (
                    SubjectExclusion(
                        subject_id, "production_outcome", "action_in_flight", "an action for it is already in flight"
                    )
                    if subject_id in busy
                    else None
                )
                candidates.append(SubjectCandidate(subject, busy_outcome, key, label))
                continue
            if subject_id.startswith("change:"):
                subject = ActionSubject(
                    kind="change",
                    id=f"change:{revision.id}",
                    title=_one_line(session.title) or "this Change",
                    revision_id=revision.id,
                )
                change_route = deps.author_route(revision, "")
                key, label = fixer(change_route if change_route.get("supported") else None, subject.id)
                if not change_route.get("supported") and change_route.get("reason"):
                    failures.append(str(change_route["reason"]))
                changed = sorted({unit.path for unit in store.list_units(revision.id) if unit.kind == "file"})
                items[subject.id] = HandoffItem(
                    subject,
                    "repository_text",
                    "changed files:\n" + "\n".join(changed[:200]),
                )
                busy_change = (
                    SubjectExclusion(
                        subject.id, "change", "action_in_flight", "a test update for it is already in flight"
                    )
                    if subject.id in busy
                    else None
                )
                candidates.append(SubjectCandidate(subject, busy_change, key, label))
                continue
            if subject_id.startswith("beh:"):
                if behavior is None:
                    behavior = {item.behavior_key: item for item in deps.behavior_targets(session, revision)}
                target = behavior.get(subject_id)
                if target is None:
                    candidates.append(
                        SubjectCandidate(
                            ActionSubject("behavior_target", subject_id),
                            SubjectExclusion(
                                subject_id,
                                "behavior_target",
                                "unknown_subject",
                                "no such behavior change on the current Snapshot",
                            ),
                        )
                    )
                    continue
                delta = target.delta
                paths = tuple(str(path) for path in delta.paths)
                subject = ActionSubject(
                    kind="behavior_target",
                    id=subject_id,
                    title=_one_line(delta.title),
                    path=paths[0] if len(paths) == 1 else "",
                    revision_id=revision.id,
                    state=str(target.state),
                )
                exclusion = None
                if target.state == "reviewed":
                    exclusion = SubjectExclusion(
                        subject_id, "behavior_target", "not_unresolved", "accepted by the reviewer", subject.title
                    )
                elif subject_id in busy:
                    exclusion = SubjectExclusion(
                        subject_id, "behavior_target", "action_in_flight", "a fix for it is already in flight"
                    )
                path_routes = [deps.author_route(revision, path) for path in paths] or [deps.author_route(revision, "")]
                owners = {(str(item.get("host") or ""), str(item.get("session_id") or "")) for item in path_routes}
                owner = (
                    path_routes[0] if len(owners) == 1 and all(item.get("supported") for item in path_routes) else None
                )
                if owner is None:
                    failures.extend(str(item.get("reason") or "") for item in path_routes if item.get("reason"))
                key, label = fixer(owner, subject_id)
                facts = [f"{name}: {value}" for name, value in delta.facts]
                items[subject_id] = HandoffItem(
                    subject,
                    "behavior_observation",
                    "\n".join(
                        [str(delta.summary), *(f"path: {path}" for path in paths), *facts],
                    ),
                )
                candidates.append(SubjectCandidate(subject, exclusion, key, label))
                continue
            annotation = store.get_annotation(subject_id)
            if annotation is not None and annotation.review_id == session.id:
                subject = annotation_subject(annotation, revision)
                exclusion = finding_exclusion(
                    subject=subject,
                    source=str(annotation.source),
                    parent_id=str(annotation.parent_id or ""),
                    state=str(annotation.state),
                    anchored=anchored(annotation),
                    disposition=str(annotation.finding_disposition or ""),
                    in_flight=subject_id in busy,
                )
                lower_owner = owner_of(annotation)
                body = str(annotation.body or "")
                if lower_owner is not None:
                    # The fix belongs to the lower Change: route to *its* exact author.
                    lower_revision = store.latest_revision(lower_owner.review_id)
                    owner_route = deps.author_route(lower_revision, subject.path) if lower_revision is not None else {}
                    if not owner_route.get("supported") and owner_route.get("reason"):
                        failures.append(str(owner_route["reason"]))
                    key, label = fixer(owner_route if owner_route.get("supported") else None, subject_id)
                    label = f"{label} (owning Change {lower_owner.change_ref})"
                    if subject_id in exact:
                        host, session_id, _ = exact[subject_id]
                        exact[subject_id] = (host, session_id, label)
                    body = (
                        f"Owning Change: {lower_owner.change_ref} ({lower_owner.reason}). "
                        "Fix this in that lower-layer Change, not in the Change under review.\n\n" + body
                    )
                else:
                    exact_by_id, problems = deps.exact_routes(revision, (subject_id,), {subject_id: annotation})
                    failures.extend(problems)
                    key, label = fixer(exact_by_id.get(subject_id), subject_id)
                origin: DataOrigin = "ai_finding" if annotation.source == "ai_review" else "deterministic_finding"
                if annotation.source == "author":
                    origin = "agent_comment"
                items[subject_id] = HandoffItem(
                    subject,
                    origin,
                    body,
                    (
                        *(str(item) for item in annotation.evidence),
                        *(f"rule: {item}" for item in annotation.rule_refs),
                    ),
                )
                candidates.append(SubjectCandidate(subject, exclusion, key, label))
                continue
            if evidence is None:
                evidence = {item.id: item for item in store.list_evidence(session.id)}
            check = evidence.get(subject_id)
            if check is not None:
                subject = ActionSubject(
                    kind="check",
                    id=subject_id,
                    title=_one_line(check.title),
                    path=str(check.path or ""),
                    revision_id=revision.id,
                    state=str(check.verification_status or ""),
                )
                exclusion = None
                if check.revision_id != revision.id:
                    exclusion = SubjectExclusion(
                        subject_id, "check", "stale_anchor", "recorded on an older Snapshot", subject.title
                    )
                elif str(check.verification_status or "").upper() not in {"FAIL", "FAILED", "ERROR"}:
                    exclusion = SubjectExclusion(
                        subject_id, "check", "not_unresolved", "this check is not failing", subject.title
                    )
                elif subject_id in busy:
                    exclusion = SubjectExclusion(
                        subject_id, "check", "action_in_flight", "a fix for it is already in flight", subject.title
                    )
                route = deps.author_route(revision, "")
                if not route.get("supported") and route.get("reason"):
                    failures.append(str(route["reason"]))
                key, label = fixer(route if route.get("supported") else None, subject_id)
                items[subject_id] = HandoffItem(
                    subject,
                    "check_output",
                    "\n".join(
                        part
                        for part in (
                            f"status: {check.verification_status}",
                            f"source: {check.source}",
                            str(check.detail or ""),
                        )
                        if part
                    ),
                    tuple(item for item in (check.url, check.source_ref) if item),
                )
                candidates.append(SubjectCandidate(subject, exclusion, key, label))
                continue
            proposal = store.get_change_proposal(subject_id)
            if proposal is not None and proposal.review_id == session.id:
                subject = ActionSubject(
                    kind="proposal",
                    id=subject_id,
                    title=f"Suggested edit in {proposal.path}",
                    path=proposal.path,
                    start_line=proposal.start_line,
                    end_line=proposal.end_line,
                    revision_id=proposal.base_revision_id,
                    state=proposal.state,
                )
                exclusion = None
                if proposal.state != "proposed":
                    exclusion = SubjectExclusion(
                        subject_id, "proposal", "not_unresolved", f"suggestion is {proposal.state}", subject.title
                    )
                elif proposal.base_revision_id != revision.id:
                    exclusion = SubjectExclusion(
                        subject_id,
                        "proposal",
                        "stale_anchor",
                        "made on an older Snapshot; recreate it on this one",
                        subject.title,
                    )
                elif subject_id in busy:
                    exclusion = SubjectExclusion(
                        subject_id, "proposal", "action_in_flight", "already being applied", subject.title
                    )
                candidates.append(SubjectCandidate(subject, exclusion, "reviewer_apply", "your checkout"))
                continue
            candidates.append(
                SubjectCandidate(
                    ActionSubject("unknown", subject_id),
                    SubjectExclusion(subject_id, "unknown", "unknown_subject", "not part of this Review"),
                )
            )
        return candidates, items, exact, failures

    # ------------------------------------------------------------------ #
    # routing
    # ------------------------------------------------------------------ #

    def preferred_route(session: Any, revision: Any, seed: str) -> ActionRoute:
        return preferred_agent_route(deps, session, seed)

    def preferred_agent_preference(session: Any) -> FixAgentPreference:
        return _preferred_agent_preference(deps, session)

    def exact_route(
        partition: SubjectPartition, exact: Mapping[str, tuple[str, str, str]], failures: Sequence[str]
    ) -> ActionRoute:
        owners = {exact[item.id] for item in partition.included if item.id in exact}
        if partition.fixer_key.startswith("exact:") and len(owners) == 1:
            host, session_id, label = next(iter(owners))
            if deps.no_local_checkout:
                return ActionRoute(
                    "exact_session", "resume", False, host, session_id, label, reason=deps.no_local_checkout
                )
            return ActionRoute("exact_session", "resume", True, host, session_id, f"{label} · wrote this code")
        reason = next((item for item in failures if item), "no exact authoring session is recorded for this code")
        return ActionRoute("exact_session", "resume", False, reason=reason)

    # ------------------------------------------------------------------ #
    # preparation (shared by preview and start)
    # ------------------------------------------------------------------ #

    def parse_request(body: Mapping[str, Any]) -> tuple[ReviewActionKind, list[str], RouteTier | None, str]:
        kind = str(body.get("kind") or "")
        if kind not in REVIEW_ACTION_KINDS:
            raise _refuse(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"kind must be one of {', '.join(REVIEW_ACTION_KINDS)}",
            )
        if kind in UNSUPPORTED_KINDS:
            # Typed, but honestly unavailable in this build: never faked.
            raise _refuse(status.HTTP_409_CONFLICT, f"{KIND_LABELS[kind]} is unavailable: {UNSUPPORTED_KINDS[kind]}")
        raw_ids = body.get("subject_ids", [])
        if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "subject_ids must be a list of ids")
        ids = [item.strip() for item in raw_ids if item.strip()]
        if len(ids) > _MAX_SUBJECT_IDS:
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, f"at most {_MAX_SUBJECT_IDS} subjects at once")
        if kind in {"fix", "apply_suggestion"} and len(ids) != 1:
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{KIND_LABELS[kind]} takes exactly one subject")
        if kind == "fix_all" and not ids:
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "Fix all needs the findings to fix")
        raw_route = body.get("route")
        if raw_route not in (None, "", *ROUTE_TIERS):
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, f"route must be one of {', '.join(ROUTE_TIERS)}")
        requested = cast("RouteTier | None", raw_route or None)
        try:
            directive = sanitize_directive(body.get("directive") or "")
        except ValueError as exc:
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        if kind == "apply_suggestion" and directive:
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "an applied suggestion carries no directive")
        return kind, ids, requested, directive

    def production_outcomes_of(session: Any) -> Sequence[Any] | None:
        return deps.production_outcomes(session) if deps.production_outcomes is not None else None

    def default_subjects(session: Any, revision: Any, kind: ReviewActionKind, ids: list[str]) -> list[str]:
        if ids:
            return ids
        if kind in CROSS_CHANGE_KINDS:
            found = tuple(production_outcomes_of(session) or ())
            concrete = [item.id for item in found if not item.advisory]
            return concrete or [item.id for item in found]
        if kind == "fix_ci_failure":
            return [
                item.id
                for item in store.list_evidence(session.id)
                if item.revision_id == revision.id
                and str(item.verification_status or "").upper() in {"FAIL", "FAILED", "ERROR"}
            ]
        if kind == "update_tests":
            return [f"change:{revision.id}"]
        return ids

    def prepare(session: Any, revision: Any, body: Mapping[str, Any]) -> _Prepared:
        kind, ids, requested, directive = parse_request(body)
        ids = default_subjects(session, revision, kind, ids)
        if not ids:
            raise _refuse(status.HTTP_409_CONFLICT, f"there is nothing for {KIND_LABELS[kind]} on this Snapshot")
        actions = actions_for(session.id)
        candidates, items, exact, failures = candidates_for(session, revision, kind, ids, requested, actions)
        limit = 1 if kind in {"fix", "apply_suggestion"} else FIX_ALL_LIMIT
        partition = partition_subjects(candidates, kind, limit=limit)
        decision: RouteDecision | None = None
        if kind == "apply_suggestion":
            apply_route = ActionRoute(
                "reviewer_apply",
                "apply",
                not deps.source_mutation_reason,
                label="apply in your checkout",
                reason=deps.source_mutation_reason,
            )
            if requested not in (None, "reviewer_apply"):
                raise _refuse(status.HTTP_409_CONFLICT, "a suggestion is applied in your checkout or not at all")
            decision = RouteDecision(apply_route, (apply_route,))
        elif partition.included:
            if requested == "reviewer_apply":
                raise _refuse(status.HTTP_409_CONFLICT, "only a suggestion is applied in your checkout")
            seed = "\0".join((session.id, revision.id, kind, *(item.id for item in partition.included)))
            try:
                decision = resolve_route(
                    exact_route(partition, exact, failures),
                    preferred_route(session, revision, seed),
                    requested=requested,
                )
            except RouteUnavailable as exc:
                raise _refuse(status.HTTP_409_CONFLICT, str(exc)) from exc
        included_items = tuple(items[item.id] for item in partition.included if item.id in items)
        route = decision.route if decision is not None else PROMPT_FALLBACK
        identity = action_identity(
            review_id=session.id,
            revision_id=revision.id,
            kind=kind,
            route=route,
            items=included_items or partition.included,
            directive=directive,
            change_description=str(session.title or ""),
        )
        attempt = sum(1 for item in actions if item.identity == identity)
        action_id = action_id_for(identity, attempt)
        handoff = ""
        if kind != "apply_suggestion" and partition.included:
            handoff = render_handoff(
                HandoffDocument(
                    action_kind=kind,
                    operation_id=action_id,
                    reviewer_id=str(session.reviewer_id),
                    snapshot_label=f"Snapshot {deps.display_number(revision)}",
                    items=included_items,
                    directive=directive,
                    change_description=str(session.title or ""),
                )
            )
        return _Prepared(
            kind=kind,
            partition=partition,
            decision=decision,
            items=included_items,
            directive=directive,
            identity=identity,
            action_id=action_id,
            handoff=handoff,
            failures=tuple(failures),
        )

    def preview_payload(revision: Any, prepared: _Prepared) -> dict[str, Any]:
        decision = prepared.decision
        return {
            "revision_id": revision.id,
            "kind": prepared.kind,
            "label": KIND_LABELS[prepared.kind],
            "operation_id": prepared.action_id,
            "ready": prepared.ready,
            "included": [item.to_wire() for item in prepared.partition.included],
            "excluded": [item.to_wire() for item in prepared.partition.excluded],
            "route": decision.route.to_wire() if decision is not None else None,
            "candidates": [item.to_wire() for item in decision.candidates] if decision is not None else [],
            "handoff": prepared.handoff,
            "handoff_digest": text_digest(prepared.handoff) if prepared.handoff else "",
            "limit": 1 if prepared.kind in {"fix", "apply_suggestion"} else FIX_ALL_LIMIT,
            "code_changing": prepared.kind in CODE_CHANGING_KINDS,
        }

    # ------------------------------------------------------------------ #
    # provenance projection
    # ------------------------------------------------------------------ #

    def reconcile(session: Any, actions: Sequence[ReviewAction]) -> tuple[ReviewAction, ...]:
        """Persist resulting Snapshots and in-doubt dispatches. Never touches a finding."""

        if session.status == "archived":
            return tuple(actions)
        if any(item.kind in CROSS_CHANGE_KINDS and item.in_flight for item in actions):
            from lemoncrow.pro.capabilities.review.outcomes.outcome_actions import reconcile_outcome_actions

            if reconcile_outcome_actions(store, session, actions):
                actions = actions_for(session.id)
        snapshots = saved_snapshots(store, session.id)
        links = resolve_results(
            actions,
            snapshot_refs(snapshots),
            exactly_attributed(store),
            proposal_fingerprints=proposal_fingerprints(store, session.id),
        )
        cutoff = datetime.now(UTC) - _DISPATCH_IN_DOUBT_AFTER
        updated: list[ReviewAction] = []
        for action in actions:
            link = links.get(action.id)
            try:
                if action.status in IN_FLIGHT_STATUSES and link is not None:
                    action = ledger.transition(
                        action.source_revision_id,
                        action.id,
                        status="snapshot_recorded",
                        allowed_from=IN_FLIGHT_STATUSES,
                        event=ActionEvent(
                            "snapshot_recorded",
                            now_iso(),
                            "system",
                            detail=f"result proven by {link.certainty.replace('_', ' ')}",
                            revision_id=link.revision_id,
                        ),
                        resulting_revision_id=link.revision_id,
                        result_certainty=link.certainty,
                    )
                elif action.status == "dispatching":
                    started = datetime.fromisoformat(action.updated_at) if action.updated_at else None
                    if started is not None and started.tzinfo is None:
                        started = started.replace(tzinfo=UTC)
                    if started is not None and started < cutoff:
                        action = ledger.transition(
                            action.source_revision_id,
                            action.id,
                            status="uncertain",
                            allowed_from=frozenset({"dispatching"}),
                            event=ActionEvent(
                                "delivery_uncertain",
                                now_iso(),
                                "system",
                                detail="dispatch never reported back; LemonCrow will not send it again",
                            ),
                            message="delivery is in doubt; it may have reached the fixer",
                        )
            except (ActionConflict, LookupError, ValueError):
                pass
            updated.append(action)
        return tuple(updated)

    def subject_states(session: Any, revision: Any, actions: Sequence[ReviewAction]) -> dict[str, dict[str, Any]]:
        """Each subject's current disposition: finding -> action -> Snapshot -> disposition."""

        states: dict[str, dict[str, Any]] = {}
        behavior: dict[str, Any] | None = None
        for action in actions:
            for subject in action.subjects:
                if subject.id in states:
                    continue
                if subject.kind in {"ai_finding", "deterministic_finding"}:
                    annotation = store.get_annotation(subject.id)
                    if annotation is not None:
                        states[subject.id] = {
                            "state": annotation.state,
                            "disposition": annotation.finding_disposition,
                            "disposition_revision_id": annotation.finding_disposition_revision_id,
                            "disposition_by": annotation.finding_disposition_reviewer_id,
                        }
                elif subject.kind == "behavior_target":
                    if behavior is None:
                        behavior = {item.behavior_key: item for item in deps.behavior_targets(session, revision)}
                    target = behavior.get(subject.id)
                    states[subject.id] = {"state": str(target.state) if target is not None else "gone"}
                elif subject.kind == "proposal":
                    proposal = store.get_change_proposal(subject.id)
                    states[subject.id] = {"state": proposal.state if proposal is not None else "gone"}
        return states

    def rerun_payload(session: Any, revision: Any) -> dict[str, Any]:
        """The R1 plan the resulting Snapshot executes: exactly what GET run-plan shows."""

        affected = correction_changed_paths(store, revision)
        try:
            plan = deps.run_plan(session, revision)
        except (RuntimeError, ValueError) as exc:
            return {"revision_id": revision.id, "available": False, "reason": str(exc)}
        entries = [row for row in plan.get("entries") or () if isinstance(row, Mapping)]
        return {
            "revision_id": revision.id,
            "available": True,
            "incremental": affected is not None,
            "affected_paths": list(affected) if affected is not None else None,
            "plan_digest": str(plan.get("digest") or ""),
            "entries": [
                {
                    "id": str(row.get("id") or ""),
                    "title": str(row.get("title") or ""),
                    "status": str(row.get("status") or ""),
                    "reason": str(row.get("reason") or ""),
                    "required": row.get("required") is True,
                }
                for row in entries
            ],
        }

    def action_payload(
        action: ReviewAction,
        *,
        numbers: Mapping[str, int],
        states: Mapping[str, Mapping[str, Any]],
        rerun: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        wire = action.to_wire()
        wire["source_revision_number"] = numbers.get(action.source_revision_id)
        wire["resulting_revision_number"] = (
            numbers.get(action.resulting_revision_id) if action.resulting_revision_id else None
        )
        wire["subjects"] = [{**item, "current": dict(states.get(item["id"], {}))} for item in wire["subjects"]]
        wire["rerun"] = dict(rerun) if rerun is not None else None
        return wire

    def listing(session: Any, revision: Any) -> dict[str, Any]:
        actions = reconcile(session, actions_for(session.id))
        revisions = store.list_revisions(session.id)
        by_id = {item.id: item for item in revisions}
        numbers = {item.id: deps.display_number(item) for item in revisions}
        states = subject_states(session, revision, actions)
        recorded = [item for item in actions if item.status == "snapshot_recorded" and item.resulting_revision_id]
        reruns: dict[str, dict[str, Any]] = {}
        for action in recorded[-_RERUN_LIMIT:]:
            result = by_id.get(action.resulting_revision_id)
            if result is not None and result.id not in reruns:
                reruns[result.id] = rerun_payload(session, result)
        busy = in_flight_subjects(actions)
        open_findings = tuple(
            item.id
            for item in store.list_annotations(session.id)
            if item.source in {"ai_review", "lemoncrow"}
            and not item.parent_id
            and item.state == "open"
            and anchored(item)
            and item.finding_disposition in {"", "open", "confirmed_by_reviewer"}
            and item.id not in busy
        )
        failing = tuple(
            item.id
            for item in store.list_evidence(session.id)
            if item.revision_id == revision.id
            and str(item.verification_status or "").upper() in {"FAIL", "FAILED", "ERROR"}
            and item.id not in busy
        )
        proposals = tuple(
            item.id
            for item in store.list_change_proposals(session.id)
            if item.state == "proposed" and item.base_revision_id == revision.id and item.id not in busy
        )
        outcomes = production_outcomes_of(session)
        catalog = action_catalog(
            CatalogInputs(
                open_findings=open_findings,
                failing_checks=failing,
                proposals=proposals,
                source_mutation_reason=deps.source_mutation_reason,
                read_only_reason=("this Review is discarded and read-only" if session.status == "archived" else ""),
                production_outcomes=(
                    None if outcomes is None else tuple(item.id for item in outcomes if item.id not in busy)
                ),
            )
        )
        return {
            "revision_id": revision.id,
            "actions": [
                action_payload(
                    item,
                    numbers=numbers,
                    states=states,
                    rerun=reruns.get(item.resulting_revision_id) if item.resulting_revision_id else None,
                )
                for item in actions
            ],
            "catalog": [item.to_wire() for item in catalog],
            "fix_all_limit": FIX_ALL_LIMIT,
            "preferred_agent": (
                FixAgentPreference("", "unset", deps.no_local_checkout)
                if deps.no_local_checkout
                else preferred_agent_preference(session)
            ).to_wire(),
        }

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #

    async def dispatch(session: Any, action: ReviewAction, prepared: _Prepared) -> ReviewAction:
        route = action.route
        if route.mode == "apply":
            proposal_id = action.subjects[0].id
            try:
                await deps.apply_proposal(proposal_id)
            except HTTPException as exc:
                return ledger.transition(
                    action.source_revision_id,
                    action.id,
                    status="failed",
                    allowed_from=frozenset({"dispatching"}),
                    event=ActionEvent("delivery_failed", now_iso(), "system", detail=str(exc.detail)),
                    message=str(exc.detail),
                )
            return ledger.transition(
                action.source_revision_id,
                action.id,
                status="awaiting_snapshot",
                allowed_from=frozenset({"dispatching"}),
                event=ActionEvent(
                    "applied",
                    now_iso(),
                    "human",
                    str(session.reviewer_id),
                    detail="applied in the trusted checkout; capture a Snapshot to review it",
                ),
                message="applied in your checkout; the next Snapshot carries it",
            )
        if route.mode == "new_session":
            result = await asyncio.to_thread(
                delivery_module.launch_agent_session,
                route.host,
                deps.source_repo,
                prepared.handoff,
                session_id=route.session_id,
                delivery_id=action.id,
                log_dir=deps.log_dir,
                wrap_review_feedback=False,
            )
            if result.session_id:
                route = replace(route, session_id=result.session_id)
        else:
            result = await asyncio.to_thread(
                delivery_module.deliver_prompt_to_agent_session,
                route.host,
                route.session_id,
                deps.source_repo,
                prepared.handoff,
            )
        state = str(result.state)
        next_status: ReviewActionStatus
        if state in _ACCEPTED:
            next_status, event_kind = "awaiting_snapshot", "dispatched"
        elif state == "uncertain":
            next_status, event_kind = "uncertain", "delivery_uncertain"
        else:
            next_status, event_kind = "failed", "delivery_failed"
        updated = ledger.transition(
            action.source_revision_id,
            action.id,
            status=next_status,
            allowed_from=frozenset({"dispatching"}),
            event=ActionEvent(
                cast(Any, event_kind),
                now_iso(),
                "system",
                detail=f"{state}: {result.message}".strip(": "),
            ),
            route=route,
            message=str(result.message or ""),
            remote_ref=str(result.remote_ref or ""),
        )
        if state in _ACCEPTED and route.host and route.session_id:
            # Causal link only: binds the session so its later work is ingested
            # and attributed; it never creates or changes a Review comment.
            from lemoncrow.pro.capabilities.review.development import record_feedback_handoff

            with contextlib.suppress(Exception):
                record_feedback_handoff(
                    deps.development_store,
                    session.id,
                    host=route.host,
                    session_id=route.session_id,
                    operation_id=action.id,
                    annotations=[("", item.title) for item in action.subjects],
                    state=state,
                    causation_id=f"action:{action.id}",
                    label=f"Review {KIND_LABELS[action.kind]} sent",
                )
        return updated

    # ------------------------------------------------------------------ #
    # routes
    # ------------------------------------------------------------------ #

    @router.get("/reviews/{review_id}/actions")
    def _get_review_actions(review_id: str) -> dict[str, Any]:
        session = deps.session(review_id)
        return listing(session, deps.latest(session))

    @router.post("/reviews/{review_id}/actions/preview")
    async def _post_review_action_preview(review_id: str, request: Request) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.api import _json_object

        session = deps.mutable_session(review_id)
        revision = deps.latest(session)
        body = await _json_object(request)
        _strict_body(body, _PREVIEW_KEYS)
        deps.require_expected_revision(body, revision, "preparing this action")
        prepared = await run_in_threadpool(prepare, session, revision, body)
        return preview_payload(revision, prepared)

    @router.post("/reviews/{review_id}/actions")
    async def _post_review_action(review_id: str, request: Request) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.api import _json_object

        session = deps.mutable_session(review_id)
        revision = deps.latest(session)
        body = await _json_object(request)
        _strict_body(body, _START_KEYS)
        operation_id = str(body.get("operation_id") or "").strip()
        expected_digest = str(body.get("expected_handoff_digest") or "").strip()
        if not operation_id:
            raise _refuse(status.HTTP_400_BAD_REQUEST, "starting an action requires operation_id from its preview")
        existing = ledger.get(revision_ids(session.id), operation_id)
        if existing is not None:
            # Idempotent replay: one prepared action dispatches once, ever.
            numbers = {item.id: deps.display_number(item) for item in store.list_revisions(session.id)}
            return {"action": action_payload(existing, numbers=numbers, states={}), "replayed": True}
        deps.require_expected_revision(body, revision, "starting this action")
        prepared = await run_in_threadpool(prepare, session, revision, body)
        if prepared.action_id != operation_id or (
            prepared.handoff and text_digest(prepared.handoff) != expected_digest
        ):
            raise _refuse(
                status.HTTP_409_CONFLICT,
                "this action changed since its preview; review the updated preview before starting it",
            )
        if not prepared.partition.included:
            reasons = "; ".join(item.reason for item in prepared.partition.excluded[:3])
            raise _refuse(status.HTTP_409_CONFLICT, f"nothing to act on: {reasons}" if reasons else "nothing to act on")
        decision = prepared.decision
        if decision is None or not decision.route.available:
            raise _refuse(
                status.HTTP_409_CONFLICT,
                decision.route.reason if decision is not None else "no route can take this action",
            )
        route = decision.route
        if route.mode == "copy" and body.get("copied") is not True:
            raise _refuse(
                status.HTTP_409_CONFLICT, "copy the fix prompt for the fixer, then confirm the handoff (copied: true)"
            )
        at = now_iso()
        actor = str(session.reviewer_id)
        events = [
            ActionEvent("created", at, "human", actor, detail=KIND_LABELS[prepared.kind], revision_id=revision.id)
        ]
        initial: ReviewActionStatus = "dispatching"
        if route.mode == "copy":
            initial = "awaiting_snapshot"
            events.append(ActionEvent("handed_off", at, "human", actor, detail="copied for the fixer"))
        action = ReviewAction(
            id=prepared.action_id,
            review_id=session.id,
            kind=prepared.kind,
            source_revision_id=revision.id,
            route=route,
            status=initial,
            created_by=actor,
            created_at=at,
            updated_at=at,
            subjects=prepared.partition.included,
            excluded=prepared.partition.excluded,
            directive=prepared.directive,
            handoff_digest=text_digest(prepared.handoff) if prepared.handoff else "",
            identity=prepared.identity,
            message=(
                "copied for the fixer; the next Snapshot returns this Review for re-review"
                if route.mode == "copy"
                else ""
            ),
            events=tuple(events),
        )
        action, created = ledger.create(action)
        if created and action.status == "dispatching":
            action = await dispatch(session, action, prepared)
        numbers = {item.id: deps.display_number(item) for item in store.list_revisions(session.id)}
        return {"action": action_payload(action, numbers=numbers, states={}), "replayed": not created}

    @router.post("/reviews/{review_id}/actions/{action_id}/cancel")
    async def _post_review_action_cancel(review_id: str, action_id: str, request: Request) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.api import _json_object

        session = deps.mutable_session(review_id)
        raw = await request.body()
        body = await _json_object(request) if raw.strip() else {}
        _strict_body(body, _CANCEL_KEYS)
        action = ledger.get(revision_ids(session.id), action_id)
        if action is None or action.review_id != session.id:
            raise _refuse(status.HTTP_404_NOT_FOUND, "no such Review action")
        reason = str(body.get("reason") or "").strip()[:500]
        try:
            action = ledger.transition(
                action.source_revision_id,
                action.id,
                status="cancelled",
                allowed_from=IN_FLIGHT_STATUSES,
                event=ActionEvent("cancelled", now_iso(), "human", str(session.reviewer_id), detail=reason),
                message=(
                    "cancelled in LemonCrow; a running agent is not stopped, and a later Snapshot "
                    "is not linked to this action"
                ),
            )
        except ActionConflict as exc:
            raise _refuse(status.HTTP_409_CONFLICT, f"cannot cancel: {exc}") from exc
        numbers = {item.id: deps.display_number(item) for item in store.list_revisions(session.id)}
        return {"action": action_payload(action, numbers=numbers, states={})}


__all__ = ["ReviewActionDeps", "preferred_agent_route", "register_review_action_routes"]
