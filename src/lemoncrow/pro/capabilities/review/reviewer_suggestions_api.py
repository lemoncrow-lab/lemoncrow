"""Reviewer suggestions route for the Review Reader (Phase 9 item 7).

``GET /api/reviews/{id}/reviewer-suggestions`` returns the advisory
:class:`~lemoncrow.pro.capabilities.review.reviewer_suggestions.SuggestionSet`
for one exact Snapshot. It is read-only: nothing here requests, assigns or
changes readiness. A hosted composition passes ``hosted`` and serves its own
projection (provider identities, directory access, provider-requested
reviewers); the local composition mines the trusted base, local LemonCrow
ownership and bounded git history at the base commit.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from lemoncrow.pro.capabilities.review import request_reads
from lemoncrow.pro.capabilities.review.reviewer_history import change_authors, history_paths, mine_path_history
from lemoncrow.pro.capabilities.review.reviewer_suggestion_sources import (
    LocalIdentityResolver,
    compose_suggestions,
    ownership_evidence,
)
from lemoncrow.pro.capabilities.review.reviewer_suggestions import (
    AccessState,
    ExclusionReason,
    ReviewerIdentity,
    SuggestionSet,
    operator_routing_layers,
    resolve_codeowners,
    resolve_routing,
    trusted_base_routing,
)


@dataclass(frozen=True)
class ReviewerSuggestionDeps:
    """Seams owned by ``register_review_api``."""

    store: Any
    session: Callable[[str], Any]
    requested_revision: Callable[[Any, str], Any]
    read_base_document: Callable[[Any, str], Any]
    review_config: Callable[[Any], Any]
    triage_policy: Callable[[Any, Any], Any]
    effective_review: Callable[[Any, Any], object]
    packet: Callable[[Any], Mapping[str, Any] | None]
    history_root: Callable[[], Path | None]
    same_repository: Callable[[Any], bool]
    hosted: Callable[[Any, Any], Mapping[str, Any]] | None = None


def local_reviewer_suggestions(deps: ReviewerSuggestionDeps, session: Any, revision: Any) -> SuggestionSet:
    """The local Reader's suggestions: trusted base, local ownership, git history at the base."""

    packet = deps.packet(revision) or {}
    raw_files = packet.get("files", ()) if isinstance(packet, Mapping) else ()
    files = [item for item in raw_files if isinstance(item, Mapping) and item.get("path")]
    policy = deps.triage_policy(session, revision)

    def criticality(path: str) -> str:
        return str(policy.classify(path).criticality)

    config = deps.review_config(revision)
    trusted = (
        trusted_base_routing(str(config.content), source=str(config.source or ""), commit=str(revision.base_sha or ""))
        if getattr(config, "state", "") == "present"
        else None
    )
    routing = resolve_routing([*operator_routing_layers(deps.effective_review(session, revision)), trusted])
    root = deps.history_root()
    base_sha = str(revision.base_sha or "")
    history = mine_path_history(root, base_sha, history_paths(files)) if root is not None and base_sha else None
    authors = (
        frozenset(
            f"email:{email}"
            for email in change_authors(
                root, base_sha, str(revision.head_sha or ""), working_tree=bool(packet.get("dirty"))
            )
        )
        if root is not None
        else frozenset()
    )

    def exclude(identity: ReviewerIdentity) -> ExclusionReason | None:
        if identity.key in authors:
            return "author"
        if "[bot]" in f"{identity.label} {identity.handle}".lower():
            return "authoring_agent"
        return None

    def access(_identity: ReviewerIdentity) -> AccessState:
        return "not_applicable"

    resolver = LocalIdentityResolver()
    return compose_suggestions(
        revision_id=str(revision.id),
        changed_files=files,
        criticality=criticality,
        codeowners=resolve_codeowners(lambda path: deps.read_base_document(revision, path)),
        routing=routing,
        resolver=resolver,
        exclude=exclude,
        access=access,
        ownership=ownership_evidence(
            deps.store,
            str(session.id),
            [str(item["path"]) for item in files],
            resolver,
            same_repository=deps.same_repository,
        ),
        history=history,
        errors=tuple(getattr(policy, "errors", ()) or ()),
    )


def register_reviewer_suggestion_routes(router: APIRouter, deps: ReviewerSuggestionDeps) -> None:
    from starlette.concurrency import run_in_threadpool

    @router.get("/reviews/{review_id}/reviewer-suggestions")
    async def _get_reviewer_suggestions(review_id: str, revision_id: str = "") -> dict[str, Any]:
        """Advisory reviewer suggestions with their reasons; never a request or assignment."""

        session = deps.session(review_id)
        revision = deps.requested_revision(session, revision_id)

        def build() -> dict[str, Any]:
            with request_reads.reads_once():
                if deps.hosted is not None:
                    return dict(deps.hosted(session, revision))
                return local_reviewer_suggestions(deps, session, revision).to_wire()

        return await run_in_threadpool(build)


__all__ = ["ReviewerSuggestionDeps", "local_reviewer_suggestions", "register_reviewer_suggestion_routes"]
