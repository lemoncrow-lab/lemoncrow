"""Where a Review's feedback stands in its handoff, from stored rows alone.

One classification serves the Reader's feedback status and the Reviews list, so
the comment the Reader counts as sent is the one the list says it waits on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from lemoncrow.pro.capabilities.review.session_models import Annotation, AnnotationVersion, DeliveryRecord

FeedbackState = Literal["addressed", "published", "in_flight", "unpublished"]

# Attempts that are active or may have crossed the host boundary: never resendable.
_UNSAFE_TO_RESEND = frozenset({"prepared", "leased", "dispatching", "working", "queued", "uncertain"})


def is_open_root_feedback(annotation: Annotation) -> bool:
    """A reviewer's own open comment thread: what a handoff carries."""

    return (
        annotation.source == "human" and not annotation.parent_id and annotation.state not in {"resolved", "obsolete"}
    )


def feedback_version(versions: Sequence[AnnotationVersion]) -> int:
    """Latest reviewer-owned version of a comment, *versions* in version order.

    Author ``addressed`` claims are versions too, but must not make text that
    was already sent look new again.
    """

    for version in reversed(versions):
        if version.change_kind != "author_response":
            return int(version.version_number)
    return 0


def feedback_state(
    annotation: Annotation,
    *,
    revision_id: str,
    version: int,
    deliveries: Sequence[DeliveryRecord],
) -> tuple[FeedbackState, tuple[DeliveryRecord, ...]]:
    """One comment's handoff state on *revision_id*, and the attempts that decide it.

    A GitHub review is a separate audience: publishing to the pull request does
    not hand a comment to its fixer, and vice versa. Only a proven
    not-dispatched failure becomes unsent again.
    """

    if annotation.author_response == "addressed":
        return "addressed", ()
    matching = tuple(
        item
        for item in deliveries
        if item.revision_id == revision_id
        and item.annotation_version == version
        and item.target_type != "github_review"
    )
    if any(item.state in {"sent", "consumed"} for item in matching):
        return "published", matching
    if any(
        item.state in _UNSAFE_TO_RESEND or (item.state in {"blocked", "failed"} and bool(item.remote_ref))
        for item in matching
    ):
        return "in_flight", matching
    return "unpublished", matching


def host_label(host: str) -> str:
    """``claude`` -> ``Claude Code``, ``opencode`` -> ``OpenCode``."""

    return {
        "claude": "Claude Code",
        "opencode": "OpenCode",
    }.get(host, host.capitalize())


@dataclass(frozen=True)
class ReviewHandoff:
    """A Review's open comments by where they stand, for one Snapshot."""

    returned: int = 0
    """Marked addressed by their fixer: back with the reviewer for re-review."""
    unsent: int = 0
    with_agent: int = 0
    """Sent to, or still being handed to, a coding-agent session."""
    with_author: int = 0
    """Copied for the author, or published on the pull request."""
    agent_hosts: tuple[str, ...] = ()
    on_pull_request: bool = False


def review_handoff(
    revision_id: str,
    annotations: Sequence[Annotation],
    versions: Mapping[str, Sequence[AnnotationVersion]],
    deliveries: Mapping[str, Sequence[DeliveryRecord]],
    *,
    pull_request: bool = False,
) -> ReviewHandoff:
    """Classify a Review's open comments on its current Snapshot.

    *pull_request*: the Review answers on a pull request, so a comment already
    published there is with its author.
    """

    returned = unsent = with_agent = with_author = 0
    hosts: set[str] = set()
    on_pull_request = False
    for annotation in annotations:
        if not is_open_root_feedback(annotation):
            continue
        rows = deliveries.get(annotation.id, ())
        version = feedback_version(versions.get(annotation.id, ()))
        state, matching = feedback_state(annotation, revision_id=revision_id, version=version, deliveries=rows)
        if state == "addressed":
            returned += 1
        elif state == "unpublished":
            published_on_pr = pull_request and any(
                item.target_type == "github_review" and item.annotation_version == version and item.state == "sent"
                for item in rows
            )
            if published_on_pr:
                with_author += 1
                on_pull_request = True
            else:
                unsent += 1
        elif all(item.target_type == "clipboard" for item in matching):
            with_author += 1
        else:
            with_agent += 1
            hosts.update(item.target_ref.partition(":")[0] for item in matching if item.target_type == "agent_session")
    return ReviewHandoff(
        returned=returned,
        unsent=unsent,
        with_agent=with_agent,
        with_author=with_author,
        agent_hosts=tuple(sorted(host for host in hosts if host)),
        on_pull_request=on_pull_request,
    )


def approved_on(facts: Mapping[str, str], revision_id: str) -> bool:
    """Whether the latest verdict (``inbox_side_facts``) approves exactly this revision."""

    return bool(revision_id) and facts.get("verdict") == "lgtm" and facts.get("verdict_revision") == revision_id


def settled_by_approval(handoff: ReviewHandoff, approved: bool) -> ReviewHandoff:
    """An approval on the current revision answers the comments that came back: none is back with the reviewer."""

    return replace(handoff, returned=0) if approved and handoff.returned else handoff


def handoff_row_fields(handoff: ReviewHandoff, *, status: str, actionable_targets: int) -> dict[str, Any]:
    """The Reviews-list fields for one row: whether it is back with the reviewer or waiting.

    Waiting holds only when nothing is left to send or judge: an open Review
    with feedback out and no addressed comment back yet.
    """

    fields: dict[str, Any] = {
        "returned_thread_count": handoff.returned,
        "waiting_on_agent_count": handoff.with_agent,
        "waiting_on_author_count": handoff.with_author,
    }
    if status != "open":
        return fields
    if handoff.returned > 0:
        return {**fields, "primary_bucket": "returned_to_me", "reason": f"{handoff.returned} addressed"}
    if handoff.unsent or actionable_targets or not (handoff.with_agent or handoff.with_author):
        return fields
    if handoff.with_agent:
        label = host_label(handoff.agent_hosts[0]) if len(handoff.agent_hosts) == 1 else "coding agent"
        return {**fields, "primary_bucket": "waiting_on_agent", "handoff_label": label}
    label = "pull request" if handoff.on_pull_request else "author"
    return {**fields, "primary_bucket": "waiting_on_author", "handoff_label": label}


__all__ = [
    "FeedbackState",
    "ReviewHandoff",
    "approved_on",
    "feedback_state",
    "feedback_version",
    "handoff_row_fields",
    "host_label",
    "is_open_root_feedback",
    "review_handoff",
    "settled_by_approval",
]
