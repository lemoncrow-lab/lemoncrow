"""HTTP routes and Reader hooks for automation, post-merge, feedback sync and discard safety (P12-AUTO).

Registered by ``register_review_api`` through one call; the hotspot keeps its
own gates (session scope, mutability, acting reviewer) and only asks the
returned :class:`ReviewAutomationHooks` at the points it owns (Finish/discard,
hard delete, comment edit, landing settling ``merged``).

Routes (all under the Review API prefix):

``GET  /reviews/{id}/automation``            rules, why each would or would not fire, post-merge state
``POST /reviews/{id}/post-merge/{kind}``     run one post-merge action explicitly (after a provider merge)
``GET  /reviews/{id}/feedback-sync``         provider identity and sync state per published comment
``POST /annotations/{id}/sync``              edit / withdraw / retry / discard a published comment
``POST /reviews/{id}/provider-review/dismiss`` dismiss a published review verdict through the provider
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, status

from lemoncrow.pro.capabilities.review import archive_safety
from lemoncrow.pro.capabilities.review.automation_rules import (
    ACTION_LABELS,
    ACTION_PERMISSIONS,
    AUTOMATION_ACTIONS,
    STATE_CHANGING_ACTIONS,
    AutomationRuleSet,
    BranchProtectionFacts,
    PermissionCheck,
    branch_protection_from_conditions,
    decide_automation,
    evaluate_rule,
)
from lemoncrow.pro.capabilities.review.feedback_sync import (
    FeedbackSyncConflict,
    FeedbackSyncProvider,
    discard_failed_sync,
    dismiss_published_review,
    published_ref,
    retry_sync,
    sync_published_comment,
    sync_view,
)
from lemoncrow.pro.capabilities.review.post_merge_actions import (
    POST_MERGE_KINDS,
    MergeAutomationResult,
    PostMergeConflict,
    PostMergeExecutor,
    PostMergeLedger,
    PostMergeRefused,
    ProviderMergeFact,
    action_for,
    automation_facts,
    observed_merge,
    readiness_record_digest,
    run_merge_automation,
    run_post_merge_action,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewDeletionTombstone

_SYNC_KEYS = frozenset({"op", "body", "expected_revision_id"})
_DISMISS_KEYS = frozenset({"remote_ref", "message", "expected_revision_id"})
_POST_MERGE_KEYS = frozenset({"confirm", "expected_revision_id"})
_PUBLISHED_GUARDED_FIELDS = frozenset({"body", "kind"})


@dataclass(frozen=True)
class ReviewAutomationDeps:
    """The Review API gates and composition seams these routes reuse; no new authority."""

    store: Any
    session: Callable[[str], Any]
    mutable_session: Callable[[str], Any]
    latest: Callable[[Any], Any]
    readiness: Callable[[Any], Mapping[str, Any]]
    """The Reader's readiness projection (the one readiness core)."""
    repository: Callable[[Any], str]
    rules: Callable[[Any], AutomationRuleSet]
    """Effective organization/operator automation rules for one Review (never repository text)."""
    permission_check: PermissionCheck
    feedback_version: Callable[[str], int]
    update_comment: Callable[[Any, Any, str, str], Any] | None = None
    """``(session, annotation, op, body)``: the local comment write after the provider confirmed."""
    branch_protection: Callable[[Any, Mapping[str, Any]], BranchProtectionFacts] | None = None
    provider_fact: Callable[[Any], ProviderMergeFact | None] | None = None
    executors: Mapping[str, PostMergeExecutor] | None = None
    feedback_sync_provider: Callable[[Any], FeedbackSyncProvider | None] | None = None
    hard_delete_allowed: Callable[[Any], bool] | None = None
    deletion_audit: Callable[[Mapping[str, Any]], None] | None = None
    deletion_context: Callable[[Any], archive_safety.DeletionContext] | None = None
    """Who is deleting, in which repository, under which request id (default: the local owner)."""
    landing_delegate_available: bool = True
    hosted_owned: Callable[[Any, str], None] | None = None
    """``(session, what)``: refuses (409) a write hosted owns while the Review is attached (RC5)."""


def _refuse(code: int, detail: Any) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _strict(body: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unsupported field(s): {', '.join(unknown)}")


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        payload = await request.json()
    except ValueError as exc:
        raise _refuse(status.HTTP_400_BAD_REQUEST, "request body must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise _refuse(status.HTTP_400_BAD_REQUEST, "request body must be a JSON object")
    return payload


class ReviewAutomationHooks:
    """What the Reader's own routes ask at the points they own."""

    def __init__(self, deps: ReviewAutomationDeps) -> None:
        self._deps = deps

    # ----- discard ------------------------------------------------------- #

    def archive_hazards(self, session: Any) -> archive_safety.ArchiveHazards:
        return archive_safety.archive_hazards(
            self._deps.store, session, landing_delegate_available=self._deps.landing_delegate_available
        )

    def require_discard_confirmation(self, session: Any, body: Mapping[str, Any]) -> archive_safety.ArchiveHazards:
        """409 with the hazards unless the request names each one (and the digest shown)."""

        hazards = self.archive_hazards(session)
        try:
            archive_safety.require_acknowledged(hazards, body)
        except archive_safety.ArchiveConfirmationRequired as exc:
            raise _refuse(status.HTTP_409_CONFLICT, exc.detail()) from exc
        return hazards

    def record_discard(self, session: Any, hazards: archive_safety.ArchiveHazards) -> dict[str, Any]:
        return archive_safety.record_discard(self._deps.store, session, hazards, actor_id=session.reviewer_id)

    def discard_preview(self, session: Any) -> dict[str, Any]:
        if session.status == "archived":
            return {**archive_safety.ArchiveHazards(session.id).to_wire(), "owed_cancellation": self.owed(session.id)}
        return {**self.archive_hazards(session).to_wire(), "owed_cancellation": self.owed(session.id)}

    def owed(self, review_id: str) -> dict[str, Any] | None:
        return archive_safety.owed_cancellation(self._deps.store, review_id)

    # ----- hard delete --------------------------------------------------- #

    def _deletion_context(self, session: Any) -> archive_safety.DeletionContext:
        if self._deps.deletion_context is not None:
            return self._deps.deletion_context(session)
        # The local server has one principal; the request id correlates the
        # tombstone with the local audit file.
        return archive_safety.DeletionContext(
            actor_id=str(session.reviewer_id or "local"),
            repo_id=str(self._deps.repository(session)),
            request_id=f"local-{uuid.uuid4().hex}",
        )

    def hard_delete(
        self,
        session: Any,
        body: Mapping[str, Any],
        delete: Callable[[str, ReviewDeletionTombstone], bool],
    ) -> bool:
        """Privilege, exact identity, audit first; then the store delete with its tombstone.

        *delete* writes the tombstone and removes the Review in one store
        transaction; any failure there (the tombstone included) leaves the
        Review in place and refuses the request.
        """

        allowed = self._deps.hard_delete_allowed(session) if self._deps.hard_delete_allowed is not None else False
        audit = self._deps.deletion_audit
        try:
            confirmation = archive_safety.authorize_hard_delete(session, body, allowed=allowed, audit=audit)
        except archive_safety.HardDeleteRefused as exc:
            raise _refuse(exc.status_code, exc.message) from exc
        assert audit is not None
        try:
            context = self._deletion_context(session)
        except Exception as exc:  # no attributable actor: never an anonymous permanent delete
            raise _refuse(status.HTTP_503_SERVICE_UNAVAILABLE, f"the deleting principal is unknown: {exc}") from exc
        tombstone = archive_safety.deletion_tombstone(session, context, confirmation)

        def _audit(outcome: str) -> None:
            audit(
                archive_safety.deletion_record(
                    session,
                    actor_id=context.actor_id,
                    confirmation=confirmation,
                    outcome=outcome,
                    request_id=context.request_id,
                )
            )

        try:
            _audit("deleting")
        except Exception as exc:  # an unaudited permanent delete never happens
            raise _refuse(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"the deletion audit could not be written: {exc}"
            ) from exc
        try:
            deleted = delete(session.id, tombstone)
        except ValueError as exc:
            _audit(f"refused: {exc}")
            raise _refuse(status.HTTP_409_CONFLICT, str(exc)) from exc
        except Exception as exc:  # the tombstone (or the delete) failed: the transaction rolled back
            _audit("refused: the deletion tombstone could not be written")
            raise _refuse(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"the deletion record could not be written, so nothing was deleted: {exc.__class__.__name__}",
            ) from exc
        _audit("deleted" if deleted else "not_found")
        return deleted

    # ----- published comments --------------------------------------------- #

    def guard_published_edit(self, annotation: Any, fields: Mapping[str, Any]) -> None:
        """A published comment's text (or withdrawal) changes only through provider sync."""

        changes_text = any(
            name in fields and fields[name] != getattr(annotation, name) for name in _PUBLISHED_GUARDED_FIELDS
        )
        withdraws = fields.get("state") == "obsolete" and annotation.state != "obsolete"
        if not changes_text and not withdraws:
            return
        ref = published_ref(self._deps.store, annotation)
        if ref is None:
            return
        raise _refuse(
            status.HTTP_409_CONFLICT,
            {
                "code": "published_feedback",
                "message": (
                    "This comment is published on the provider; edit or withdraw it through provider sync so "
                    "the provider and LemonCrow never disagree."
                ),
                "published": ref.to_wire(),
            },
        )

    # ----- automation ------------------------------------------------------ #

    def facts(self, session: Any, readiness: Mapping[str, Any] | None) -> Any:
        deps = self._deps
        merge = observed_merge(
            deps.store, session, provider_fact=deps.provider_fact(session) if deps.provider_fact else None
        )
        conditions = readiness.get("conditions") if readiness is not None else None
        if deps.branch_protection is not None and readiness is not None:
            protection = deps.branch_protection(session, readiness)
        else:
            protection = branch_protection_from_conditions(conditions if isinstance(conditions, list) else [])
        landing = readiness.get("landing") if readiness is not None else None
        return merge, automation_facts(
            deps.store,
            session,
            repository=deps.repository(session),
            merge=merge,
            provider=str(landing.get("provider") or "") if isinstance(landing, Mapping) else "",
            readiness_ready=bool(readiness.get("ready")) if readiness is not None else None,
            branch_protection=protection,
        )

    def run_merge_automation(self, session: Any) -> MergeAutomationResult:
        """Idempotent: run every post-merge action an enabled rule fires for this merge."""

        deps = self._deps
        ruleset = deps.rules(session)
        rules = ruleset.for_repository(deps.repository(session))
        if not rules:
            return MergeAutomationResult(merge=None)
        readiness = deps.readiness(session)
        merge, facts = self.facts(session, readiness)
        return run_merge_automation(
            deps.store,
            session,
            rules=rules,
            facts=facts,
            merge=merge,
            permission_check=deps.permission_check,
            readiness_digest=lambda: readiness_record_digest(deps.store, session.id, deps.readiness(session)),
            executors=deps.executors,
        )


def _write_local_comment(store: Any, revision: Any, session: Any, annotation: Any, op: str, body: str) -> Any:
    """The Reader's own comment write (a new annotation version), after the provider confirmed."""

    from lemoncrow.pro.capabilities.review.store import AnnotationHistoryContext

    history = AnnotationHistoryContext(
        revision_id=revision.id, changed_by=session.reviewer_id, changed_by_actor="human"
    )
    if op == "edit":
        fields: dict[str, object] = {"body": body}
        if annotation.author_response != "none":
            # As the Reader's edit: an author response belongs to the text the author saw.
            fields.update(author_response="none", author_response_source_id="", author_response_at="")
        return store.update_annotation(annotation.id, history, **fields)
    return store.update_annotation(annotation.id, history, state="obsolete")


def register_review_automation_routes(router: APIRouter, deps: ReviewAutomationDeps) -> ReviewAutomationHooks:
    """Add the routes to *router* and return the hooks the Reader routes call."""

    from starlette.concurrency import run_in_threadpool

    store = deps.store
    hooks = ReviewAutomationHooks(deps)

    def _revision_ids(review_id: str) -> list[str]:
        return [item.id for item in store.list_revisions(review_id)]

    def _automation_view(session: Any) -> dict[str, Any]:
        readiness = deps.readiness(session)
        merge, facts = hooks.facts(session, readiness)
        repository = deps.repository(session)
        ruleset = deps.rules(session)
        rules = ruleset.for_repository(repository)
        records = PostMergeLedger(store).list(_revision_ids(session.id))
        decisions = [
            decide_automation(action, rules=rules, facts=facts, permission_check=deps.permission_check)
            for action in AUTOMATION_ACTIONS
        ]
        kinds = []
        for kind in POST_MERGE_KINDS:
            action = action_for(kind)
            latest = next((item for item in reversed(records) if item.kind == kind), None)
            kinds.append(
                {
                    "kind": kind,
                    "action": action,
                    "label": ACTION_LABELS[action],
                    "permission": ACTION_PERMISSIONS[action],
                    "state_changing": action in STATE_CHANGING_ACTIONS,
                    "latest": None if latest is None else latest.to_wire(),
                    "runnable": merge is not None,
                }
            )
        return {
            "review_id": session.id,
            "repository": repository,
            "policy": ruleset.to_wire(repository),
            "facts": facts.to_wire(),
            "evaluations": [evaluate_rule(rule, facts).to_wire() for rule in rules],
            "decisions": [item.to_wire() for item in decisions],
            "merge": None if merge is None else merge.to_wire(),
            "post_merge": {
                "available": merge is not None,
                "reason": (
                    "The provider reported this Change merged."
                    if merge is not None
                    else "Post-merge actions run only after the connected provider reports this Change merged."
                ),
                "in_readiness": False,
                "kinds": kinds,
                "records": [item.to_wire() for item in records],
            },
            "readiness_digest": readiness_record_digest(store, session.id, readiness),
        }

    @router.get("/reviews/{review_id}/automation")
    async def _get_automation(review_id: str) -> dict[str, Any]:
        session = deps.session(review_id)
        return cast(dict[str, Any], await run_in_threadpool(_automation_view, session))

    @router.post("/reviews/{review_id}/post-merge/{kind}")
    async def _post_post_merge(review_id: str, kind: str, request: Request) -> dict[str, Any]:
        """A human runs one optional post-merge action; only after a provider-observed merge."""

        session = deps.mutable_session(review_id)
        if deps.hosted_owned is not None:
            deps.hosted_owned(session, "provider operations")
        body = await _body(request)
        _strict(body, _POST_MERGE_KEYS)
        if body.get("confirm") is not True:
            raise _refuse(status.HTTP_400_BAD_REQUEST, "a post-merge action needs confirm=true as an explicit action")
        if kind not in POST_MERGE_KINDS:
            raise _refuse(status.HTTP_404_NOT_FOUND, f"unknown post-merge action {kind!r}")
        provider_fact = deps.provider_fact(session) if deps.provider_fact else None
        merge = observed_merge(store, session, provider_fact=provider_fact)

        def _run() -> tuple[Any, bool]:
            return run_post_merge_action(
                store,
                session,
                kind,
                merge=merge,
                initiated_by="human",
                actor_id=session.reviewer_id,
                readiness_digest=lambda: readiness_record_digest(store, session.id, deps.readiness(session)),
                executors=deps.executors,
            )

        try:
            record, ran = await run_in_threadpool(_run)
        except PostMergeRefused as exc:
            raise _refuse(status.HTTP_409_CONFLICT, str(exc)) from exc
        except PostMergeConflict as exc:
            raise _refuse(status.HTTP_409_CONFLICT, str(exc)) from exc
        return {"record": record.to_wire(), "ran": ran}

    @router.get("/reviews/{review_id}/feedback-sync")
    async def _get_feedback_sync(review_id: str) -> dict[str, Any]:
        session = deps.session(review_id)

        def _view() -> dict[str, Any]:
            annotations = [item for item in store.list_annotations(session.id) if item.source == "human"]
            return {"review_id": session.id, **sync_view(store, session.id, annotations)}

        return cast(dict[str, Any], await run_in_threadpool(_view))

    @router.post("/annotations/{annotation_id}/sync")
    async def _post_annotation_sync(annotation_id: str, request: Request) -> dict[str, Any]:
        """Change a published comment through the provider; the local copy follows only on success."""

        annotation = store.get_annotation(annotation_id)
        if annotation is None:
            raise _refuse(status.HTTP_404_NOT_FOUND, "no such comment")
        session = deps.mutable_session(annotation.review_id)
        if deps.hosted_owned is not None:
            deps.hosted_owned(session, "comments and provider operations")
        body = await _body(request)
        _strict(body, _SYNC_KEYS)
        op = str(body.get("op") or "")
        if op not in {"edit", "withdraw", "retry", "discard"}:
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "op must be edit, withdraw, retry or discard")
        if annotation.source != "human" or annotation.parent_id:
            raise _refuse(status.HTTP_409_CONFLICT, "only a reviewer's own root comment is published feedback")
        if annotation.created_by != session.reviewer_id:
            raise _refuse(status.HTTP_403_FORBIDDEN, "only the comment's author can change it on the provider")
        if op == "discard":
            try:
                record = await run_in_threadpool(discard_failed_sync, store, annotation, actor_id=session.reviewer_id)
            except FeedbackSyncConflict as exc:
                raise _refuse(status.HTTP_409_CONFLICT, str(exc)) from exc
            return {"sync": record.to_wire(), "annotation_id": annotation.id}
        if published_ref(store, annotation) is None:
            raise _refuse(
                status.HTTP_409_CONFLICT,
                "this comment was never published to the provider; edit or withdraw it locally",
            )
        provider = deps.feedback_sync_provider(session) if deps.feedback_sync_provider else None
        if provider is None:
            raise _refuse(
                status.HTTP_409_CONFLICT,
                "this server has no provider connection that can change published feedback; nothing was changed",
            )

        def _apply_local(current: Any, sync_op: str, text: str) -> Any:
            if deps.update_comment is not None:
                return deps.update_comment(session, current, sync_op, text)
            return _write_local_comment(store, deps.latest(session), session, current, sync_op, text)

        try:
            if op == "retry":
                outcome = await run_in_threadpool(
                    lambda: retry_sync(
                        store,
                        annotation,
                        provider=provider,
                        actor_id=session.reviewer_id,
                        apply_local=_apply_local,
                        current_version=deps.feedback_version,
                    )
                )
            else:
                outcome = await run_in_threadpool(
                    lambda: sync_published_comment(
                        store,
                        annotation,
                        op=cast(Any, op),
                        body=str(body.get("body") or ""),
                        provider=provider,
                        actor_id=session.reviewer_id,
                        apply_local=_apply_local,
                        current_version=deps.feedback_version,
                    )
                )
        except FeedbackSyncConflict as exc:
            raise _refuse(status.HTTP_409_CONFLICT, str(exc)) from exc
        return {
            "sync": outcome.record.to_wire(),
            "annotation_id": annotation.id,
            "local_changed": outcome.record.state in {"synced", "corrected"},
        }

    @router.post("/reviews/{review_id}/provider-review/dismiss")
    async def _post_dismiss(review_id: str, request: Request) -> dict[str, Any]:
        """Dismiss a review verdict this Review published, through the provider."""

        session = deps.mutable_session(review_id)
        if deps.hosted_owned is not None:
            deps.hosted_owned(session, "outcomes and provider operations")
        body = await _body(request)
        _strict(body, _DISMISS_KEYS)
        remote_ref = str(body.get("remote_ref") or "")
        published = None
        for annotation in store.list_annotations(session.id):
            for item in store.list_deliveries(annotation.id):
                if item.target_type == "github_review" and item.state == "sent" and item.remote_ref == remote_ref:
                    published = item
                    break
            if published is not None:
                break
        if published is None or not remote_ref.startswith("github-review:"):
            raise _refuse(status.HTTP_404_NOT_FOUND, "no review with that identity was published from this Review")
        provider = deps.feedback_sync_provider(session) if deps.feedback_sync_provider else None
        if provider is None:
            raise _refuse(
                status.HTTP_409_CONFLICT,
                "this server has no provider connection that can dismiss a published review; nothing was changed",
            )
        try:
            record = await run_in_threadpool(
                lambda: dismiss_published_review(
                    store,
                    review_id=session.id,
                    revision_id=published.revision_id or deps.latest(session).id,
                    remote_ref=remote_ref,
                    target_ref=published.target_ref,
                    message=str(body.get("message") or ""),
                    provider=provider,
                    actor_id=session.reviewer_id,
                )
            )
        except FeedbackSyncConflict as exc:
            raise _refuse(status.HTTP_409_CONFLICT, str(exc)) from exc
        return {"sync": record.to_wire()}

    return hooks


__all__ = ["ReviewAutomationDeps", "ReviewAutomationHooks", "register_review_automation_routes"]
