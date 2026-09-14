"""A review's comments, in the one shape every recipient can read.

Plan SS10.1: *review -> annotate -> send feedback*, where "send" is somebody
else's problem. This module builds the bundle and renders it as Markdown. It
delivers nothing, opens no socket, and knows about no host: a delivery target is
:class:`~lemoncrow.pro.capabilities.review.session_models.DeliveryRecord`'s
business (PR-R6), and keeping the two apart is what lets the same bundle go to
an agent session, a clipboard and a pull request without three renderers that
drift.

Two properties this module owes its callers, and the reasons:

**Deterministic.** Two runs over the same annotations produce byte-identical
Markdown -- ordering is ``attention_rank``, then path, then start line, then id,
and nothing here stamps a time. A bundle that changes when nothing changed
cannot be diffed, cannot be cached, and cannot be checked into a review trail.

**Honest about position.** An orphaned comment is printed in its own section
with the reason it lost its anchor, never silently emitted next to a line number
that no longer means anything. Plannotator's approach -- a prose disclaimer that
the whole file was "anchored to commit abc1234" -- is exactly the failure this
project exists to fix: it converts every comment into a footnote instead of
relocating the ones that can be relocated and admitting the ones that cannot.

No Markdown *library* is used, and none is wanted. This is not general Markdown
rendering; it is a fixed six-section document whose shape is the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lemoncrow.pro.capabilities.review.session_models import ANCHOR_METHOD_LABELS, anchor_symbol_claim

# How many out-of-patch call sites a bundle names before it stops. A recipient
# who is handed forty is handed none.
_CONTEXT_SITES = 8

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from lemoncrow.pro.capabilities.review.session_models import (
        AnchorMethod,
        Annotation,
        AnnotationKind,
        AnnotationState,
        ReviewRevision,
        ReviewSession,
        ReviewUnit,
    )

# How each disposition is written for a human. Plan SS5.5 caps the set at four
# and this mapping is total over it, so an unknown kind is impossible rather
# than rendered as a raw identifier.
KIND_LABELS: dict[str, str] = {
    "comment": "comment",
    "request_change": "request change",
    "suggestion": "suggestion",
    "looks_good": "looks good",
}

# The state that still wants the reader's attention. `resolved` is carried in
# the bundle but rendered separately, because "already dealt with" and "still
# open" are different asks and merging them makes the second invisible.
_OPEN_STATES = ("open",)


@dataclass(frozen=True)
class FeedbackItem:
    """One comment, flattened into the fields a recipient needs.

    ``replies`` is the thread under this comment, already ordered by creation.
    They are carried as plain bodies rather than nested items because a
    recipient reads a thread, and a two-level structure that only ever renders
    as an indented list is a structure nobody needs.
    """

    annotation_id: str
    kind: AnnotationKind
    state: AnnotationState
    path: str
    start_line: int
    end_line: int
    body: str
    symbol: str = ""
    """The definition this comment sits in **now**; empty when no rung found it."""
    origin_symbol: str = ""
    """The definition it was *written* against, when nothing located it since.

    Separate from :attr:`symbol` because the recipient of a bundle is usually an
    agent about to edit code. "in ``gone_forever``" is an address it will act
    on; "originally in ``gone_forever``" is history it cannot mistake for one.
    """
    attention_rank: int = 0
    anchor_method: AnchorMethod = "identical_blob"
    anchor_detail: str = ""
    replies: tuple[str, ...] = ()

    @property
    def location(self) -> str:
        """``path:L10`` for one line, ``path:L10-L20`` for a range."""

        if self.end_line > self.start_line:
            return f"{self.path}:L{self.start_line}-L{self.end_line}"
        return f"{self.path}:L{self.start_line}"


@dataclass(frozen=True)
class FeedbackBundle:
    """Everything one review has to say, ready to hand to something else."""

    review_id: str
    revision_id: str
    revision_number: int
    title: str = ""
    range_mode: str = ""
    items: tuple[FeedbackItem, ...] = ()
    """Comments that still point at a line, in attention order."""
    orphaned: tuple[FeedbackItem, ...] = ()
    """Comments whose anchor could not be re-found. Reported, never dropped."""
    resolved: tuple[FeedbackItem, ...] = ()
    context: tuple[str, ...] = ()
    """Review context lines (impacted callers, verification), verbatim."""
    degraded: tuple[str, ...] = ()
    """Every reason this revision knows less than a perfect one would."""


def packet_context(packet: Mapping[str, object] | None) -> tuple[str, ...]:
    """The *Review context* bullets of plan SS10.1, drawn from a packet dict.

    Impacted call sites outside the patch first, because they are the part a
    recipient cannot see in the diff, then verification exactly as it was
    recorded -- ``NOT_RUN`` printed as ``NOT_RUN``. Nothing here turns silence
    into a pass.

    Lives beside the renderer rather than in either surface, so the terminal's
    ``--feedback`` and the workspace's *Send feedback* produce the same document
    rather than two that drifted.
    """

    if packet is None:
        return ()
    out: list[str] = []
    sites = packet.get("impact")
    for site in sites if isinstance(sites, (list, tuple)) else ():
        if not isinstance(site, dict) or site.get("in_patch"):
            continue
        out.append(f"impacted caller: {site.get('path') or ''}")
        if len(out) >= _CONTEXT_SITES:
            break
    evidence = packet.get("evidence")
    for item in evidence if isinstance(evidence, (list, tuple)) else ():
        if isinstance(item, dict):
            out.append(f"{item.get('name') or 'verification'}: {item.get('status') or 'UNKNOWN'}")
    return tuple(out)


def _sort_key(item: FeedbackItem) -> tuple[int, int, str, int, str]:
    """Attention order, then a total tiebreak so the output cannot wobble.

    ``attention_rank == 0`` means *unranked*, which sorts last rather than
    first -- rank 0 is the absence of a rank, not the best one.
    """

    return (
        1 if item.attention_rank == 0 else 0,
        item.attention_rank,
        item.path,
        item.start_line,
        item.annotation_id,
    )


def _item_for(
    annotation: Annotation,
    ranks: Mapping[str, int],
    symbols: Mapping[str, str],
    replies: Sequence[Annotation],
) -> FeedbackItem:
    anchor = annotation.anchor
    current_symbol, origin_symbol = anchor_symbol_claim(
        annotation.anchor_method,
        anchor.symbol_qualified_name or symbols.get(anchor.unit_key, ""),
    )
    return FeedbackItem(
        annotation_id=annotation.id,
        kind=annotation.kind,
        state=annotation.state,
        path=anchor.path,
        start_line=anchor.start_line,
        end_line=anchor.end_line,
        body=annotation.body,
        symbol=current_symbol,
        origin_symbol=origin_symbol,
        attention_rank=ranks.get(anchor.unit_key, 0) or ranks.get(anchor.path, 0),
        anchor_method=annotation.anchor_method,
        anchor_detail=annotation.anchor_detail,
        replies=tuple(reply.body for reply in replies),
    )


def build_bundle(
    session: ReviewSession,
    revision: ReviewRevision,
    annotations: Sequence[Annotation],
    units: Sequence[ReviewUnit] = (),
    *,
    context: Sequence[str] = (),
    title: str = "",
) -> FeedbackBundle:
    """Assemble the bundle. Pure: no store, no repository, no clock.

    *units* supply the attention ranks, so a comment on the file the ranking put
    first is the first comment the recipient reads. Without them every item is
    unranked and the order falls back to path and line, which is still total and
    still deterministic.

    Replies are attached to their parents and never appear as top-level items:
    a thread read out of order is a thread misread.
    """

    ranks: dict[str, int] = {}
    symbols: dict[str, str] = {}
    for unit in units:
        ranks[unit.unit_key] = unit.attention_rank
        if unit.kind == "file":
            ranks.setdefault(unit.path, unit.attention_rank)
        if unit.symbol:
            symbols[unit.unit_key] = unit.symbol

    # A feedback bundle is the human reviewer's judgment being sent back to an
    # author. Author rationale, deterministic LemonCrow evidence and AI hints are
    # context for the reviewer; feeding them back as if they were reviewer
    # comments would blur the trust boundary the unified annotation model adds.
    human_annotations = tuple(annotation for annotation in annotations if annotation.source == "human")

    children: dict[str, list[Annotation]] = {}
    for annotation in human_annotations:
        if annotation.parent_id:
            children.setdefault(annotation.parent_id, []).append(annotation)

    open_items: list[FeedbackItem] = []
    orphaned: list[FeedbackItem] = []
    resolved: list[FeedbackItem] = []
    for annotation in human_annotations:
        if annotation.parent_id:
            continue
        item = _item_for(annotation, ranks, symbols, children.get(annotation.id, ()))
        if annotation.state in ("orphaned", "obsolete"):
            orphaned.append(item)
        elif annotation.state in _OPEN_STATES:
            open_items.append(item)
        else:
            resolved.append(item)

    return FeedbackBundle(
        review_id=session.id,
        revision_id=revision.id,
        revision_number=revision.revision_number,
        title=title or session.title,
        range_mode=revision.range_mode,
        items=tuple(sorted(open_items, key=_sort_key)),
        orphaned=tuple(sorted(orphaned, key=_sort_key)),
        resolved=tuple(sorted(resolved, key=_sort_key)),
        context=tuple(context),
        degraded=tuple(sorted(set(revision.degraded))),
    )


def _section(item: FeedbackItem) -> list[str]:
    lines = [
        f"### {item.location} — {KIND_LABELS.get(item.kind, item.kind)}",
        f"<!-- lemoncrow-annotation-id: {item.annotation_id} -->",
    ]
    if item.symbol:
        where = f"in `{item.symbol}` · "
    elif item.origin_symbol:
        where = f"originally in `{item.origin_symbol}` · "
    else:
        where = ""
    # Every surviving comment says which rung put it here. The recipient is
    # usually an agent about to edit that line, and "we re-found this by its
    # text alone, in a file whose function has since been renamed" is exactly
    # the caveat that stops a confident edit to the wrong place.
    lines.append(f"*{where}{ANCHOR_METHOD_LABELS.get(item.anchor_method, item.anchor_method)}*")
    lines.append("")
    lines.append(item.body.strip() or "_(no text)_")
    for reply in item.replies:
        lines.append("")
        lines.append(f"> {reply.strip()}")
    lines.append("")
    return lines


def render_markdown(bundle: FeedbackBundle) -> str:
    """Render the bundle exactly as plan SS10.1 shows it, plus what it omits.

    The plan's example has three sections. Two more are added here because
    leaving them out would be a claim rather than a saving: comments that lost
    their anchor, and the reasons this revision knows less than a perfect one
    would. A feedback bundle that silently drops an orphan tells the recipient
    the reviewer had nothing more to say.

    Byte-stable for a given bundle: no timestamps, no set iteration, and a total
    sort order.
    """

    lines: list[str] = ["## Review feedback", ""]
    header = f"Revision {bundle.revision_number} · {bundle.range_mode}"
    if bundle.title:
        header = f"{bundle.title} — {header}"
    lines.append(header)
    lines.append("")

    if not bundle.items and not bundle.orphaned:
        lines.append("No open comments on this revision.")
        lines.append("")
    for item in bundle.items:
        lines.extend(_section(item))

    if bundle.orphaned:
        lines.append("### Comments that lost their anchor")
        lines.append("")
        lines.append("These were not relocated, because more than one place fit or none did.")
        lines.append("They are listed at the position they last held, which no longer means anything.")
        lines.append("")
        for item in bundle.orphaned:
            reason = item.anchor_detail or "no reason recorded"
            lines.append(f"- {item.location} — {KIND_LABELS.get(item.kind, item.kind)}: {reason}")
        lines.append("")

    if bundle.context:
        lines.append("### Review context")
        lines.append("")
        for entry in bundle.context:
            lines.append(f"- {entry}")
        lines.append("")

    if bundle.degraded:
        lines.append("### What is not known")
        lines.append("")
        for name in bundle.degraded:
            lines.append(f"- {name}")
        lines.append("")

    lines.append("Human review REQUIRED. Nothing here is a verdict.")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "KIND_LABELS",
    "FeedbackBundle",
    "FeedbackItem",
    "build_bundle",
    "packet_context",
    "render_markdown",
]
