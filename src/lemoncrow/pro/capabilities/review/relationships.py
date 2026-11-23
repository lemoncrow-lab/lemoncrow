"""Durable Change relationship projection.

External SCM/work-item systems remain authoritative. LemonCrow stores only the
relationship identity/context needed to keep one r/<id> coherent over time.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from lemoncrow.pro.capabilities.review import viewer_lens
from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship, ReviewSession

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.store import ReviewStore


def relationship_payload(item: ReviewRelationship) -> dict[str, Any]:
    try:
        metadata = json.loads(item.metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    return {
        "id": item.id,
        "kind": item.kind,
        "relation": item.relation,
        "provider": item.provider,
        "external_id": item.external_id,
        "target_change_ref": f"r/{item.target_review_id}" if item.target_review_id else "",
        "url": item.url,
        "title": item.title,
        "status": item.status,
        "source": item.source,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def scm_identity_from_source_ref(
    source_ref: str,
    *,
    range_mode: str,
) -> tuple[str, str, str] | None:
    ref = source_ref.strip()
    if not ref:
        return None

    explicit_prefixes = ("github:", "gitlab:", "gerrit:", "jj:", "gitbutler:", "provider:")
    if range_mode != "commit_range" and not ref.startswith(explicit_prefixes):
        return None

    provider = "git"
    external_id = ref
    if ref.startswith("provider:"):
        _, _, remainder = ref.partition(":")
        provider, separator, external_id = remainder.partition(":")
        if not separator or not provider or not external_id:
            provider, external_id = "git", ref
    else:
        for candidate in ("github", "gitlab", "gerrit", "jj", "gitbutler"):
            prefix = candidate + ":"
            if ref.startswith(prefix):
                provider = candidate
                external_id = ref[len(prefix) :]
                break

    url = ""
    if provider == "github":
        owner_repo, separator, number = external_id.partition("#")
        if separator and owner_repo and number:
            url = f"https://github.com/{owner_repo}/pull/{number}"
    return provider, external_id, url


def _derived_scm_requirements(store: ReviewStore, session: ReviewSession) -> list[dict[str, Any]]:
    """Provider-owned requirements a store derives from provider facts (hosted only).

    These are SCM requirements (e.g. GitHub dismissing approvals on a new
    push), never changed targets. An unreadable hook contributes nothing; the
    provider still enforces its own rule.
    """

    hook = getattr(store, "derived_scm_requirements", None)
    if not callable(hook):
        return []
    try:
        rows = hook(session)
    except Exception:
        logging.getLogger(__name__).warning("derived SCM requirements unavailable for %s", session.id, exc_info=True)
        return []
    return [dict(row) for row in rows or () if isinstance(row, Mapping)]


def stack_payload(store: ReviewStore, session: ReviewSession) -> dict[str, Any] | None:
    """Read-only stack awareness (item 24); ``None`` when the Change is not stacked."""

    from lemoncrow.pro.capabilities.review.stack_awareness import stack_for

    try:
        projection = stack_for(store, session)
    except Exception:
        logging.getLogger(__name__).warning("stack awareness unavailable for %s", session.id, exc_info=True)
        return None
    if projection is None or (not projection.stacked and projection.complete):
        return None
    return projection.to_wire()


def change_context_payload(
    store: ReviewStore, session: ReviewSession, *, include_stack: bool = False
) -> dict[str, Any]:
    """Relationships of one Change; ``include_stack`` adds the read-only stack projection."""

    rows = [relationship_payload(item) for item in store.list_relationships(session.id)]
    scm = [item for item in rows if item["kind"] == "scm"]
    source_identity = scm_identity_from_source_ref(session.source_ref, range_mode=session.range_mode)
    if source_identity is not None:
        provider, external_id, url = source_identity
        if not any(item["provider"] == provider and item["external_id"] == external_id for item in scm):
            scm.insert(
                0,
                {
                    "id": "session-source-ref",
                    "kind": "scm",
                    "relation": "tracks",
                    "provider": provider,
                    "external_id": external_id,
                    "target_change_ref": "",
                    "url": url,
                    "title": "",
                    "status": "",
                    "source": "session",
                    "metadata": {},
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                },
            )
    payload: dict[str, Any] = {
        "ref": f"r/{session.id}",
        "scm": scm,
        "relations": [item for item in rows if item["kind"] == "change"],
        "work_items": [item for item in rows if item["kind"] == "work_item"],
        "scm_requirements": [
            *(item for item in rows if item["kind"] == "scm_requirement"),
            *_derived_scm_requirements(store, session),
        ],
        # A reader who may not read the Review's full rows yet gets a landing's state, not the words its withdrawal
        # was made from (its blockers) or who armed it (P7-1 F).
        "landing": [
            {**item, "metadata": viewer_lens.landing_metadata_wire(session.id, item["metadata"])}
            for item in rows
            if item["kind"] == "landing"
        ],
    }
    if include_stack:
        payload["stack"] = stack_payload(store, session)
    return payload


__all__ = ["change_context_payload", "relationship_payload", "scm_identity_from_source_ref", "stack_payload"]
