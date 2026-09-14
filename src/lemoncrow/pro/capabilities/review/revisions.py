"""What changed since *you* reviewed it -- reconciliation and the frontier.

Every diff viewer answers "what changed since main?". The question a reviewer
of agent output actually has is different and much harder:

    **what changed since I last looked at this?**

Answering it means carrying a human's verdicts across a rewrite of the thing
they were verdicts about. Two functions do that, both pure:

:func:`reconcile`
    Given the units of the revision the marks were made against, the units of
    the new revision, and the marks -- decide which marks survive intact, which
    must reopen, which unit is new, and which is gone.
:func:`compute_frontier`
    Project the surviving marks onto the new revision's units. **Derived on
    every read, never stored**: computed from units plus marks it cannot
    disagree with the marks, whereas a persisted frontier becomes a second
    opinion that drifts and then has to be reconciled with the first.

The rule the whole module exists to enforce:

    **``reviewed`` is never silently preserved across changed content.**

A stale ``reviewed`` is worse than no review state at all. No review state makes
a reader look; a stale one tells them not to. So carry-forward is decided by
``ReviewMark.content_fingerprint`` -- what was actually on screen -- and never
by a revision id or a line range. A unit whose content moved off the fingerprint
its mark was made against reopens as ``changed_since_review``, which is the one
group the reviewer must see first.

The symmetric failure is just as bad and is guarded just as explicitly: a
reconciliation that reopens *everything* on every revision is not conservative,
it is useless. A reviewer who is asked to re-read nineteen untouched files
because an agent edited a twentieth stops reading any of them. Both directions
are pinned by tests.

No store, no git, no I/O. The caller persists the decisions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    FrontierEntry,
    MarkState,
    ReviewFrontier,
    ReviewMark,
    ReviewRevision,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.units import unit_key


@dataclass(frozen=True)
class Reconciliation:
    """What a new revision does to a reviewer's existing marks.

    Four disjoint outcomes, and the caller persists each one differently:
    *carried* needs no write at all, *reopened* carries already-rewritten marks
    to upsert, *dropped* names marks whose unit left the review and whose rows
    must go, and *added*/*removed* are the unit-level deltas a surface uses to
    say "new since your last revision".

    *notes* exists because one of the rules is genuinely surprising -- hunk
    identity is ordinal-derived and cannot survive a re-hunking -- and a rule a
    reviewer cannot see the effect of is a rule they will read as a bug.
    """

    carried: tuple[ReviewMark, ...] = ()
    reopened: tuple[ReviewMark, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    dropped: tuple[ReviewMark, ...] = ()
    notes: tuple[str, ...] = ()
    aliases: tuple[tuple[str, str], ...] = ()
    """Old unit key -> current unit key for rename-preserved identities."""

    @property
    def marks(self) -> tuple[ReviewMark, ...]:
        """Every mark that still applies to the new revision, carried and reopened."""

        return self.carried + self.reopened


@dataclass(frozen=True)
class FrontierGroups:
    """The frontier in the four buckets a reviewer actually asks for.

    Ordered by how much they cost a human, not alphabetically: what you already
    approved and an agent then rewrote is the only group nobody else's tool
    shows you, so it comes first. Every entry lands in exactly one bucket.
    """

    changed_since_review: tuple[FrontierEntry, ...] = ()
    new: tuple[FrontierEntry, ...] = ()
    unresolved: tuple[FrontierEntry, ...] = ()
    unchanged_reviewed: tuple[FrontierEntry, ...] = ()
    not_yet_reviewed: tuple[FrontierEntry, ...] = ()


def _hunk_counts(units: Sequence[ReviewUnit]) -> dict[str, int]:
    """How many hunks each path had, which is the only thing hunk keys encode."""

    counts: dict[str, int] = {}
    for unit in units:
        if unit.kind == "hunk":
            counts[unit.path] = counts.get(unit.path, 0) + 1
    return counts


def _rehunked(previous: Sequence[ReviewUnit], nxt: Sequence[ReviewUnit]) -> dict[str, tuple[int, int]]:
    """Paths whose hunk count moved, with both counts, so it can be explained.

    A hunk's ``unit_key`` is derived from its ordinal within the file -- the one
    unit key in the system that *is* position-dependent. Insert a hunk at the
    top and every ordinal below shifts, so hunk 2's mark would silently become
    hunk 3's. There is no fingerprint trick that recovers the intent, so the
    marks are reset rather than reassigned, and file and symbol marks -- which
    have position-independent keys -- reconcile normally underneath.
    """

    before = _hunk_counts(previous)
    after = _hunk_counts(nxt)
    # Only paths the baseline actually had. A path that was not in the revision
    # the reviewer last saw was not *re*-hunked -- it is new, it can hold no
    # mark of theirs, and reporting it as a reset would put a note about lost
    # verdicts next to a file nobody had a verdict on.
    return {path: (before[path], after[path]) for path in after if path in before and before[path] != after[path]}


def _next_state(
    mark: ReviewMark,
    unit: ReviewUnit,
    previous: ReviewUnit | None,
    rehunked: Mapping[str, tuple[int, int]],
) -> MarkState:
    """The state *mark* has on the revision *unit* belongs to.

    Only the two states that make a claim about *content having been seen* are
    re-derived. ``needs_changes`` is a request a human made and this function
    has no standing to withdraw it: downgrading it to ``unknown`` because a file
    turned binary would delete the reviewer's own input, which is a strictly
    worse failure than the one the ``unknown`` rule guards against.
    ``unreviewed`` and ``unknown`` never attested anything, so there is nothing
    to invalidate.

    The ``unknown`` downgrade mirrors ``sources.local.effective_mark_state``
    exactly: what cannot be fingerprinted cannot be recorded as reviewed, at
    mark time or at reconcile time. One rule, two doors.
    """

    # Human requests are durable input, not content attestations. A re-hunk or
    # temporary fingerprint failure has no standing to withdraw them.
    if mark.state == "needs_changes":
        return mark.state
    # `unknown` can be a temporary downgrade of a prior reviewed attestation.
    # The downgrade deliberately preserves the fingerprint the human actually
    # saw. Once identity becomes knowable again, compare against that preserved
    # fingerprint so UNKNOWN is not a one-way ratchet.
    if mark.state == "unknown" and previous is not None and previous.fingerprint_method == "unknown":
        if unit.fingerprint_method == "unknown":
            return "unknown"
        return "reviewed" if unit.content_fingerprint == mark.content_fingerprint else "changed_since_review"
    if mark.state not in ("reviewed", "changed_since_review"):
        return mark.state
    if unit.kind == "hunk" and unit.path in rehunked:
        return "unreviewed"
    if unit.fingerprint_method == "unknown" or (previous is not None and previous.fingerprint_method == "unknown"):
        return "unknown"
    if unit.content_fingerprint == mark.content_fingerprint:
        # Includes the revert case: content that moved away and came back is
        # content this reviewer did look at, and saying otherwise would make
        # "changed since review" a one-way ratchet nobody can clear.
        return "reviewed"
    return "changed_since_review"


def _rename_aliases(
    previous_units: Sequence[ReviewUnit],
    renames: Sequence[tuple[str, str]],
) -> dict[str, str]:
    """Old ``unit_key`` -> new ``unit_key`` for every unit of a renamed file.

    A ``unit_key`` is composed from the path, so ``git mv`` renames every key in
    the file and would strand every mark in it. Recomputing the key under the
    new path is safe precisely because the *fingerprints* are unaffected for
    symbols (path is excluded from a symbol digest on purpose) and correctly
    affected for the file itself (path is included), so a rename carries the
    symbol verdicts forward and still reopens the file for one look at where it
    now lives.

    Renames only. A *copy* leaves the original in place, so migrating its marks
    would move a verdict off a file that still exists.
    """

    moved = {old: new for old, new in renames if old and new and old != new}
    if not moved:
        return {}
    aliases: dict[str, str] = {}
    for unit in previous_units:
        destination = moved.get(unit.path)
        if destination is None:
            continue
        # Hunk identity is ordinal-derived. A rename combined with an inserted
        # hunk can shift every ordinal, so aliasing hunk N -> hunk N on the new
        # path silently moves a verdict onto different code. Hunk marks are
        # conservatively dropped on rename; file/symbol identities remain safe.
        if unit.kind == "hunk":
            continue
        aliases[unit.unit_key] = unit_key(unit.kind, destination, unit.symbol, unit.ordinal)
    return aliases


def reconcile(
    previous_units: Sequence[ReviewUnit],
    next_units: Sequence[ReviewUnit],
    marks: Sequence[ReviewMark],
    *,
    renames: Sequence[tuple[str, str]] = (),
) -> Reconciliation:
    """Carry *marks* from *previous_units* onto *next_units*, or refuse to.

    Keyed on ``unit_key`` throughout -- position-independent, so ten lines
    inserted above a function move nothing -- and decided on
    ``content_fingerprint`` -- so a rewrite of that function moves exactly it.

    *renames* is ``(old_path, new_path)`` for each file the diff reports as
    renamed. Without it a ``git mv`` looks exactly like "twenty units deleted,
    twenty units created" and silently discards every verdict in the file.

    Never raises, and never mutates its inputs: reopened marks are new frozen
    records with the same ``reviewed_revision_id``, because that field records
    *when a human actually looked* and advancing it would erase the only
    evidence of how stale the verdict is.
    """

    prev_by_key = {unit.unit_key: unit for unit in previous_units}
    next_by_key = {unit.unit_key: unit for unit in next_units}
    rehunked = _rehunked(previous_units, next_units)
    # Only aliases that actually land on a unit of the new revision count: a
    # symbol that was deleted *during* the rename genuinely left the review, and
    # pretending it merely moved would keep a verdict alive on nothing.
    aliases = {old: new for old, new in _rename_aliases(previous_units, renames).items() if new in next_by_key}
    arrivals = set(aliases.values())

    carried: list[ReviewMark] = []
    reopened: list[ReviewMark] = []
    dropped: list[ReviewMark] = []
    for mark in marks:
        key = aliases.get(mark.unit_key, mark.unit_key)
        unit = next_by_key.get(key)
        if unit is None:
            # The unit left the review. Its mark is not "unreviewed" -- there is
            # nothing left to review -- so the row goes rather than lingering as
            # a verdict on something that no longer exists.
            dropped.append(mark)
            continue
        state = _next_state(mark, unit, prev_by_key.get(mark.unit_key), rehunked)
        if state == mark.state and key == mark.unit_key:
            carried.append(mark)
            continue
        reopened.append(replace(mark, state=state, unit_key=key))
        if key != mark.unit_key:
            # The mark moved to a new primary key, so the old row is a duplicate
            # verdict on a path that no longer exists and has to go.
            dropped.append(mark)

    notes = tuple(
        f"{path}: hunk count changed {before} -> {after}; hunk marks reset "
        "(hunk keys are ordinal-derived and cannot survive a re-hunking)"
        for path, (before, after) in sorted(rehunked.items())
    )
    return Reconciliation(
        carried=tuple(carried),
        reopened=tuple(reopened),
        added=tuple(key for key in next_by_key if key not in prev_by_key and key not in arrivals),
        removed=tuple(key for key in prev_by_key if key not in next_by_key and key not in aliases),
        dropped=tuple(dropped),
        notes=notes,
        aliases=tuple(sorted(aliases.items())),
    )


def compute_frontier(
    review_id: str,
    reviewer_id: str,
    revision: ReviewRevision,
    units: Sequence[ReviewUnit],
    marks: Sequence[ReviewMark],
    *,
    annotations: Sequence[Annotation] = (),
) -> ReviewFrontier:
    """What is left to look at on *revision*, for one reviewer.

    Derived, never stored. A unit with no mark is ``unreviewed`` because that is
    what it is, not because a row says so, and ``changed_since_mark`` is a live
    comparison rather than a remembered boolean -- so the frontier cannot report
    a unit as settled while its fingerprint says otherwise.

    Annotations are optional so the frontier can be computed before any exist;
    when supplied, open and orphaned ones are listed separately, because
    "nobody has answered this yet" and "this comment lost its footing" need
    different actions from the reviewer.
    """

    by_key = {mark.unit_key: mark for mark in marks if mark.reviewer_id == reviewer_id}
    entries: list[FrontierEntry] = []
    for unit in units:
        mark = by_key.get(unit.unit_key)
        entries.append(
            FrontierEntry(
                unit_key=unit.unit_key,
                kind=unit.kind,
                path=unit.path,
                symbol=unit.symbol,
                state=mark.state if mark is not None else "unreviewed",
                attention_rank=unit.attention_rank,
                reasons=unit.reasons,
                changed_since_mark=mark is not None and mark.content_fingerprint != unit.content_fingerprint,
                reviewed_revision_id=mark.reviewed_revision_id if mark is not None else "",
                ordinal=unit.ordinal,
                start_line=unit.start_line,
            )
        )
    return ReviewFrontier(
        review_id=review_id,
        reviewer_id=reviewer_id,
        last_seen_revision=revision.id,
        entries=tuple(entries),
        unresolved_annotation_ids=tuple(item.id for item in annotations if item.state == "open"),
        orphaned_annotation_ids=tuple(item.id for item in annotations if item.state == "orphaned"),
        reviewed_unit_fingerprints=tuple(
            sorted((mark.unit_key, mark.content_fingerprint) for mark in by_key.values() if mark.state == "reviewed")
        ),
    )


_KIND_ORDER: dict[str, int] = {"file": 0, "document_section": 1, "symbol": 2, "hunk": 3}


def _attention_key(entry: FrontierEntry) -> tuple[int, int, str, int, str]:
    """Rank first, unranked last, then the file and its parts in reading order.

    ``attention_rank == 0`` means *unranked* rather than *rank zero* -- the
    ``--limit`` truncation leaves survivors at ``1..limit`` and everything else
    at 0 -- so it is sorted to the bottom rather than to the top, where a plain
    ascending sort would put it and where it would read as the most urgent thing
    in the review.
    """

    return (
        1 if entry.attention_rank == 0 else 0,
        entry.attention_rank,
        entry.path,
        _KIND_ORDER.get(entry.kind, 9),
        entry.symbol,
    )


def group_frontier(frontier: ReviewFrontier, added: Sequence[str] = ()) -> FrontierGroups:
    """Sort the frontier into the four buckets a reviewer asks for, plus the rest.

    *added* is ``Reconciliation.added`` -- the unit keys that were not in the
    revision the reviewer last saw. Without it "new" and "never got to it" are
    the same bucket, and they are not the same problem.

    Precedence matters and is fixed: a unit that both appeared this revision and
    carries a reviewed mark from an older one is *reviewed*, not new. Claiming
    otherwise would ask a human to re-read something they demonstrably read.
    """

    new_keys = set(added)
    changed: list[FrontierEntry] = []
    fresh: list[FrontierEntry] = []
    unresolved: list[FrontierEntry] = []
    reviewed: list[FrontierEntry] = []
    pending: list[FrontierEntry] = []
    for entry in frontier.entries:
        if entry.state == "changed_since_review":
            changed.append(entry)
        elif entry.state == "needs_changes":
            unresolved.append(entry)
        elif entry.state == "reviewed":
            reviewed.append(entry)
        elif entry.unit_key in new_keys:
            fresh.append(entry)
        else:
            pending.append(entry)
    return FrontierGroups(
        changed_since_review=tuple(sorted(changed, key=_attention_key)),
        new=tuple(sorted(fresh, key=_attention_key)),
        unresolved=tuple(sorted(unresolved, key=_attention_key)),
        unchanged_reviewed=tuple(sorted(reviewed, key=_attention_key)),
        not_yet_reviewed=tuple(sorted(pending, key=_attention_key)),
    )


__all__ = [
    "FrontierGroups",
    "Reconciliation",
    "compute_frontier",
    "group_frontier",
    "reconcile",
]
