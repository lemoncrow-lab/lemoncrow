"""Stable human approval identity for generated Review correction proposals.

Ordinary review comments, authored bot text, and arbitrary evidence references
never confer the ability to dispatch an agent correction. A correction approval
is recorded exclusively through the gated check-recipe approval endpoint.
"""

from __future__ import annotations

import hashlib
from typing import Any


def autofix_approval_id(review_id: str, proposal_id: str) -> str:
    return "ann-" + hashlib.sha256(f"approved-autofix:{review_id}:{proposal_id}".encode()).hexdigest()[:32]


def is_autofix_approval(
    annotation: Any, *, review_id: str, proposal_id: str, recipe_id: str, revision_id: str, reviewer_id: str
) -> bool:
    """Verify both server-minted identity and immutable human/Snapshot provenance."""
    return (
        annotation.id == autofix_approval_id(review_id, proposal_id)
        and annotation.review_id == review_id
        and annotation.revision_id == revision_id
        and annotation.source == "human"
        and annotation.created_by_actor == "human"
        and annotation.created_by == (reviewer_id or "local")
        and annotation.title == f"Autofix proposed · {recipe_id}"
        and f"proposal:{proposal_id}" in annotation.evidence
    )
