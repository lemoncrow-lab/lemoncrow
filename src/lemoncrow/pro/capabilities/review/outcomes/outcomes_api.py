"""Production outcome routes (P15-OBS): what production said about a merged Change.

``GET /reviews/{id}/production``
    predicted vs observed per production deployment, linked incidents
    (advisory), the production problems Revert / Fix forward act on, the
    Changes that reverted or fixed it forward, and the automatic-rollback
    decision. ``{"configured": false}`` is the whole answer when neither a
    deployment nor an observability connector is configured.
``POST /reviews/{id}/production/links``
    ``{action_id, target_review_id}``: a human names the new Change a copied
    Revert / Fix forward prompt produced; it is linked both ways.

Registered by ``register_review_api`` through one call; the routes reuse the
Review API's session gates and grant no new authority (hosted maps the POST to
``REVIEW_MANAGE``, the permission that starts the action).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from lemoncrow.pro.capabilities.review import viewer_lens
from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import observation_projection
from lemoncrow.pro.capabilities.review.outcomes.outcome_actions import (
    RELATIONS,
    change_links,
    link_outcome_change,
    production_outcomes,
    rollback_decision,
)

OutcomeSettings = Callable[[Any], tuple[Any, Any]]
"""``session -> (DeploymentSettings, ObservabilitySettings)`` of the Review's repository."""


def outcomes_configured(settings: OutcomeSettings, session: Any) -> bool:
    deployment, observability = settings(session)
    return bool(deployment.configured or observability.configured)


def register_production_routes(
    router: APIRouter,
    *,
    store: Any,
    session: Callable[[str], Any],
    mutable_session: Callable[[str], Any],
    settings: OutcomeSettings,
    rules: Callable[[Any], Any],
    repository: Callable[[Any], str],
    permission_check: Callable[[Any, str], bool],
    hidden: Callable[[Any], frozenset[str]] | None = None,
) -> None:
    def projection(current: Any) -> dict[str, Any]:
        deployment, observability = settings(current)
        if not (deployment.configured or observability.configured):
            return {"configured": False}
        problems = production_outcomes(store, current)
        repo = repository(current)
        rollback = rollback_decision(
            rules(current).for_repository(repo),
            review_id=current.id,
            repository=repo,
            problems=problems,
            permission_check=permission_check,
        )
        return {
            "configured": True,
            "observability": observation_projection(store, current, observability),
            "problems": [item.to_wire() for item in problems],
            "links": change_links(store, current.id),
            "rollback": rollback.to_wire(),
            "freshness_class": "external_observation",
            # Post-merge observations never enter readiness.
            "in_readiness": False,
        }

    @router.get("/reviews/{review_id}/production")
    def review_production(review_id: str) -> dict[str, Any]:
        return projection(session(review_id))

    @router.post("/reviews/{review_id}/production/links")
    async def review_production_link(review_id: str, request: Request) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.api import _json_object
        from lemoncrow.pro.capabilities.review.review_actions import ReviewActionLedger

        current = mutable_session(review_id)
        body = await _json_object(request)
        unknown = sorted(set(body) - {"action_id", "target_review_id"})
        if unknown:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown field {unknown[0]!r}")
        action_id = str(body.get("action_id") or "").strip()
        target = str(body.get("target_review_id") or "").strip()
        if not action_id or not target:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "action_id and target_review_id are required")
        if not outcomes_configured(settings, current):
            raise HTTPException(status.HTTP_409_CONFLICT, "no deployment or observability connector is configured")

        def run() -> dict[str, Any]:
            revision_ids = [item.id for item in store.list_revisions(current.id)]
            # An action of someone this person may not read yet is as unknown to her as an id nobody holds.
            action = viewer_lens.reachable(
                frozenset() if hidden is None else hidden(current),
                ReviewActionLedger(store).get(revision_ids, action_id),
                lambda found: found.created_by,
            )
            if action is None or action.review_id != current.id or action.kind not in RELATIONS:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such Revert or Fix forward action on this Review")
            resolved = store.resolve_session(target)
            target_id = resolved.id if resolved is not None else target
            if not action.in_flight:
                linked = any(
                    item.relation == RELATIONS[action.kind][0] and item.target_review_id == target_id
                    for item in store.list_relationships(current.id, kind="change")
                )
                if not linked:
                    raise HTTPException(status.HTTP_409_CONFLICT, f"this action is already {action.status}")
            try:
                link = link_outcome_change(
                    store,
                    action,
                    target_id,
                    certainty="unattributed",
                    actor_id=str(current.reviewer_id),
                )
            except ValueError as exc:
                raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
            return {"link": link.to_wire(), "links": change_links(store, current.id)}

        return await run_in_threadpool(run)


__all__ = ["OutcomeSettings", "outcomes_configured", "register_production_routes"]
