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

import bisect
import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    FrontierEntry,
    MarkState,
    ReviewFrontier,
    ReviewMark,
    ReviewRevision,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.units import (
    ANCHOR_MOVED,
    bare_fingerprint,
    orders_parts,
    owns_deleted_text,
    place_reason,
    split_anchor,
    unit_key,
    with_anchor,
)


@dataclass(frozen=True)
class SymbolMove:
    """One symbol that left a file and appeared, unambiguously, somewhere else.

    ``moved``: same qualified name, same body (identical fingerprint) in another
    file; the mark is carried. ``renamed``: same body, different name; the mark
    is **not** carried -- it reopens as ``changed_since_review`` at the new key
    with the ``symbol_renamed`` reason so a later surface can route it.
    """

    kind: Literal["moved", "renamed"]
    old_key: str
    new_key: str
    previous_path: str
    previous_symbol: str
    path: str
    symbol: str
    # The reviewer whose mark followed this move. Detection is about units and leaves it empty; ``reconcile``
    # stamps it, so one reviewer's followed move is never read as another reviewer's.
    reviewer_id: str = ""


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
    rebased: tuple[ReviewMark, ...] = ()
    """Judgments carried across *base-only* movement (Phase 9 item 34).

    The unit's bytes moved only because the diff base moved underneath an
    authored patch that is patch-equivalent on both sides. The state is kept and
    the fingerprint is re-stamped to the current unit, so the caller persists
    each one with an explicit, audited reconciliation reason. Never produced
    without a deterministic base-only classification from the caller.
    """
    restamped: tuple[ReviewMark, ...] = ()
    """Judgments kept on a part that now shows *less* deleted text than the verdict covered.

    The part's fingerprint covers the deleted base lines it shows. When the same body
    is shown again with none of them (the replaced neighbour no longer shares its
    block), every line on screen is one the reviewer judged, so the state is kept and
    the fingerprint re-stamped. Not a reopening, and not a base-only carry: it has its
    own reason code (``deleted_text_no_longer_shown``) and its own count.
    """
    reasons: tuple[tuple[str, str, str], ...] = ()
    """``(reviewer_id, unit_key, reason code)`` for every carried, rebased and reopened mark.

    Keyed by the mark's unit key on the new revision; the code is the rule that
    decided it, in the :mod:`validity` vocabulary. Explanatory only.
    """
    moves: tuple[SymbolMove, ...] = ()
    """Symbol moves and renames a mark actually followed (one per old key).

    A ``moved`` entry is also in :attr:`aliases`; a ``renamed`` one is not, because
    the mark reopened rather than carried.
    """

    @property
    def marks(self) -> tuple[ReviewMark, ...]:
        """Every mark that still applies to the new revision: carried, rebased, restamped and reopened."""

        return self.carried + self.rebased + self.restamped + self.reopened


@dataclass(frozen=True)
class FileRevisionDelta:
    """File-level transition between two frozen ReviewRevisions.

    This is deliberately smaller than a Git diff. It answers the Reader's
    cache question: which already-loaded file details still describe the exact
    bytes on the new revision? A file with unknown identity is never preserved,
    even when its placeholder fingerprint happens to compare equal.
    """

    preserved: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RevisionUnitDelta:
    """One semantic review unit that differs between two frozen revisions."""

    unit_key: str
    path: str
    kind: str
    symbol: str
    status: str
    from_state: str = "unreviewed"
    to_state: str = "unreviewed"
    from_start_line: int = 0
    to_start_line: int = 0


@dataclass(frozen=True)
class RevisionFileComparison:
    """Roll-up of semantic unit and reviewer-state changes for one file."""

    path: str
    status: str
    added: int = 0
    changed: int = 0
    removed: int = 0
    reviewed_before: int = 0
    reviewed_after: int = 0
    changed_since_review_after: int = 0
    needs_changes_after: int = 0


@dataclass(frozen=True)
class RevisionComparison:
    """Provable semantic delta between two immutable ReviewRevisions.

    This intentionally does not fabricate an A/B source patch. Some hosted and
    dirty-worktree revisions cannot prove both full file bodies after a file
    leaves the reviewed change. Review-unit fingerprints *are* durable for every
    revision, so this comparison works identically for local and hosted review.
    """

    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    units: tuple[RevisionUnitDelta, ...] = ()
    files: tuple[RevisionFileComparison, ...] = ()


def file_revision_delta(
    previous_units: Sequence[ReviewUnit],
    next_units: Sequence[ReviewUnit],
    *,
    renames: Sequence[tuple[str, str]] = (),
    same_diff_base: bool = True,
) -> FileRevisionDelta:
    """Classify file cache validity across review revisions.

    File-unit fingerprints are the authoritative frozen-content identity used
    by review itself, so the browser does not have to infer invalidation from
    target shape or line ranges. ``same_diff_base`` is a separate precondition:
    identical new-side bytes can still produce a different patch when ``HEAD``
    (or another base) moves underneath a mutable review. Renames are always
    invalidated on both names: even byte-identical content moved to a new path
    changes the file-level unit identity and path-sensitive context around it.
    """

    before = {unit.path: unit for unit in previous_units if unit.kind == "file"}
    after = {unit.path: unit for unit in next_units if unit.kind == "file"}

    moved: list[tuple[str, str]] = []
    renamed_from: set[str] = set()
    renamed_to: set[str] = set()
    for old_path, new_path in renames:
        if not old_path or not new_path or old_path == new_path:
            continue
        if old_path not in before or new_path not in after:
            continue
        pair = (old_path, new_path)
        if pair in moved:
            continue
        moved.append(pair)
        renamed_from.add(old_path)
        renamed_to.add(new_path)

    preserved: list[str] = []
    changed: list[str] = []
    for path in sorted(set(before) & set(after)):
        if path in renamed_from or path in renamed_to:
            continue
        left = before[path]
        right = after[path]
        if (
            same_diff_base
            and left.fingerprint_method != "unknown"
            and right.fingerprint_method != "unknown"
            and left.content_fingerprint == right.content_fingerprint
        ):
            preserved.append(path)
        else:
            changed.append(path)

    added = sorted(path for path in set(after) - set(before) if path not in renamed_to)
    removed = sorted(path for path in set(before) - set(after) if path not in renamed_from)
    return FileRevisionDelta(
        preserved=tuple(preserved),
        changed=tuple(changed),
        added=tuple(added),
        removed=tuple(removed),
        renamed=tuple(sorted(moved)),
    )


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
) -> tuple[MarkState, str]:
    """The state *mark* has on the revision *unit* belongs to, and the rule that decided it.

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

    The second value is the :mod:`validity` reason code of the branch taken.
    """

    # Human requests are durable input, not content attestations. A re-hunk or
    # temporary fingerprint failure has no standing to withdraw them.
    if mark.state == "needs_changes":
        return mark.state, "human_request_kept"
    # `unknown` can be a temporary downgrade of a prior reviewed attestation.
    # The downgrade deliberately preserves the fingerprint the human actually
    # saw. Once identity becomes knowable again, compare against that preserved
    # fingerprint so UNKNOWN is not a one-way ratchet.
    if mark.state == "unknown" and previous is not None and previous.fingerprint_method == "unknown":
        if unit.fingerprint_method == "unknown":
            return "unknown", "fingerprint_unknown"
        if unit.content_fingerprint == mark.content_fingerprint:
            return "reviewed", "fingerprint_restored"
        if (place := place_reason(mark.content_fingerprint, unit.content_fingerprint)) is not None:
            return "changed_since_review", place
        if split_anchor(unit.content_fingerprint)[0] == split_anchor(mark.content_fingerprint)[0]:
            return "reviewed", "fingerprint_restored"  # the unit has no anchor: nothing to tell apart
        return "changed_since_review", "content_changed"
    # A hunk key is its ordinal, so a re-hunked file's key now names different content. A part left for later
    # follows the key like any hunk mark, so it resets rather than pointing at a hunk the reviewer never set aside.
    if mark.state == "later" and unit.kind == "hunk" and unit.path in rehunked:
        return "unreviewed", "hunks_renumbered"
    if mark.state not in ("reviewed", "changed_since_review"):
        return mark.state, "fingerprint_unknown" if mark.state == "unknown" else "no_attestation"
    if unit.kind == "hunk" and unit.path in rehunked:
        return "unreviewed", "hunks_renumbered"
    if unit.fingerprint_method == "unknown" or (previous is not None and previous.fingerprint_method == "unknown"):
        return "unknown", "fingerprint_unknown"
    if unit.content_fingerprint == mark.content_fingerprint:
        # Includes the revert case: content that moved away and came back is
        # content this reviewer did look at, and saying otherwise would make
        # "changed since review" a one-way ratchet nobody can clear.
        return "reviewed", "content_unchanged"
    if (place := place_reason(mark.content_fingerprint, unit.content_fingerprint)) is not None:
        return "changed_since_review", place
    if split_anchor(unit.content_fingerprint)[0] == split_anchor(mark.content_fingerprint)[0]:
        # The unit has no anchor (its file is not ordered now): there is no place to compare, and the content is what it was.
        return "reviewed", "content_unchanged"
    return "changed_since_review", "content_changed"


# Parts that are spans rather than definitions: they have no order of their own, only a place relative to definitions.
_SPAN_KINDS = frozenset({"hunk", "document_section"})


def _orders_parts(unit: ReviewUnit) -> bool:
    """Whether *unit* is a part of a file whose order is its meaning: by name, or by the anchor derive gave it."""

    return unit.kind != "file" and (orders_parts(unit.path) or bool(split_anchor(unit.content_fingerprint)[1]))


def _in_every_longest_run(sequence: Sequence[int]) -> set[int]:
    """Indexes of the elements of a permutation that belong to *every* longest increasing subsequence.

    An element is in some longest run when the longest run ending at it and the longest run starting at it add up to
    the whole; it is in every one when it is the only such element at its place in the run. A swap of two neighbours
    has two longest runs (keep either), so neither is in every one and both are reported as moved, which is the
    fail-safe reading: the reviewer re-reads both rather than one being silently trusted.
    """

    def longest_ending(values: Sequence[int]) -> list[int]:
        tails: list[int] = []
        out: list[int] = []
        for value in values:
            slot = bisect.bisect_left(tails, value)
            if slot == len(tails):
                tails.append(value)
            else:
                tails[slot] = value
            out.append(slot + 1)
        return out

    ending = longest_ending(sequence)
    # Longest run *starting* at i is the longest run ending at i of the reversed, negated sequence.
    starting = longest_ending([-value for value in reversed(sequence)])[::-1]
    total = max(ending, default=0)
    by_place: dict[int, list[int]] = {}
    for index in range(len(sequence)):
        if ending[index] + starting[index] - 1 == total:
            by_place.setdefault(ending[index], []).append(index)
    return {members[0] for members in by_place.values() if len(members) == 1}


def _crossed_spans(
    spans: Sequence[tuple[ReviewUnit, ReviewUnit]], fixed: Sequence[tuple[ReviewUnit, ReviewUnit]]
) -> set[str]:
    """New keys of the spans a fixed definition has crossed: wholly before it then wholly after, or the reverse.

    The definition test per (span, fixed) pair is ``fixed.before_end < span.before_start and fixed.after_start >
    span.after_end`` (it was above, it is below) or ``fixed.before_start > span.before_end and fixed.after_end <
    span.after_start`` (it was below, it is above). Both are a range test on one side and a threshold on the other, so
    each is answered for every span at once: the first against the fixed definitions sorted by their old end (the
    largest new start among those ending before the span began), the second against them sorted by their old start (the
    smallest new end among those starting after the span ended).
    """

    if not spans or not fixed:
        return set()
    by_end = sorted(fixed, key=lambda pair: pair[0].end_line)
    ends = [pair[0].end_line for pair in by_end]
    best_start = list(itertools.accumulate((pair[1].start_line for pair in by_end), max))
    by_start = sorted(fixed, key=lambda pair: pair[0].start_line)
    starts = [pair[0].start_line for pair in by_start]
    best_end = list(itertools.accumulate((pair[1].end_line for pair in reversed(by_start)), min))[::-1]
    crossed: set[str] = set()
    for span_before, span_after in spans:
        above = bisect.bisect_left(ends, span_before.start_line)  # fixed definitions that ended before the span began
        if above and best_start[above - 1] > span_after.end_line:
            crossed.add(span_after.unit_key)
            continue
        below = bisect.bisect_right(starts, span_before.end_line)  # ... and that began after the span ended
        if below < len(starts) and best_end[below] < span_after.start_line:
            crossed.add(span_after.unit_key)
    return crossed


def _order_moved(
    previous_units: Sequence[ReviewUnit],
    next_units: Sequence[ReviewUnit],
    aliases: Mapping[str, str],
) -> frozenset[str]:
    """Keys (in the new revision) of parts whose place among their file's parts changed, content aside.

    Definitions are ordered by where they start. Parts present on both sides form two sequences; the ones that are in
    every longest run common to both are unmoved and everything else moved. A part that merely shifted because lines
    came or went elsewhere keeps its place in both sequences, so it is never reported: only relative order is read,
    never a line number.

    A hunk or a document section has no order of its own, so it moved only when a definition that did not move has
    crossed it (it lay wholly before the span and now lies wholly after, or the reverse). Overlap says nothing either
    way and is never counted, so spans that merely grew or shrank do not move.

    Parts whose fingerprint is unknown on either side are left out: nothing about them is known to compare.
    """

    previous_by_key = {aliases.get(unit.unit_key, unit.unit_key): unit for unit in previous_units}
    by_path: dict[str, list[ReviewUnit]] = {}
    for unit in next_units:
        if unit.fingerprint_method != "unknown" and _orders_parts(unit):
            by_path.setdefault(unit.path, []).append(unit)
    moved: set[str] = set()
    for units in by_path.values():
        pairs = [
            (before, after)
            for after in units
            if (before := previous_by_key.get(after.unit_key)) is not None
            and before.kind == after.kind
            and before.fingerprint_method != "unknown"
        ]
        definitions = [pair for pair in pairs if pair[1].kind not in _SPAN_KINDS and pair[1].kind != "dependency"]
        spans = [pair for pair in pairs if pair[1].kind in _SPAN_KINDS]
        if not definitions:
            continue
        # A nested definition sorts after the one that holds it: same start, wider first.
        before_order = sorted(definitions, key=lambda pair: (pair[0].start_line, -pair[0].end_line, pair[1].unit_key))
        after_rank = {
            pair[1].unit_key: rank
            for rank, pair in enumerate(
                sorted(definitions, key=lambda pair: (pair[1].start_line, -pair[1].end_line, pair[1].unit_key))
            )
        }
        stable = _in_every_longest_run([after_rank[pair[1].unit_key] for pair in before_order])
        fixed = [pair for index, pair in enumerate(before_order) if index in stable]
        moved.update(pair[1].unit_key for index, pair in enumerate(before_order) if index not in stable)
        moved.update(_crossed_spans(spans, fixed))
    return frozenset(moved)


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
        if unit.kind in {"hunk", "dependency"}:
            # Dependency identity is ecosystem + component, not anchor path. A
            # manifest/lock rename therefore leaves its semantic unit key valid
            # as-is and needs no path-derived alias.
            continue
        aliases[unit.unit_key] = unit_key(unit.kind, destination, unit.symbol, unit.ordinal)
    return aliases


def _follow_move(
    mark: ReviewMark,
    move: SymbolMove,
    prev_by_key: Mapping[str, ReviewUnit],
    next_by_key: Mapping[str, ReviewUnit],
    rehunked: Mapping[str, tuple[int, int]],
) -> tuple[ReviewMark, str] | None:
    """The mark as it stands on the destination of *move* and the reason code, or ``None`` to drop it.

    A moved symbol is judged exactly as it would be in place (so a mark already stale
    stays stale); only the reasons that mean "kept" are renamed ``symbol_moved``. A
    renamed symbol never keeps a verdict: an attestation reopens, anything else is dropped
    as before (``lc-debt: a renamed needs_changes/later mark is dropped, not followed;
    follow it once a product decision says a name change keeps a request``).
    """

    unit = next_by_key[move.new_key]
    if move.kind == "moved":
        previous = prev_by_key.get(mark.unit_key)
        mark = _restamped_mark(mark, unit, previous)
        state, code = _next_state(mark, unit, previous, rehunked)
        if code in ("content_unchanged", "fingerprint_restored") and _orders_parts(unit):
            # Its place in a file whose order is its meaning is a new one, so the verdict is read again.
            state, code = "changed_since_review", "part_order_changed"
            mark = replace(mark, content_fingerprint=with_anchor(mark.content_fingerprint, ANCHOR_MOVED))
        elif code in ("content_unchanged", "human_request_kept", "no_attestation"):
            code = "symbol_moved"
        return replace(mark, state=state, unit_key=move.new_key), code
    if mark.state in ("reviewed", "changed_since_review"):
        return replace(mark, state="changed_since_review", unit_key=move.new_key), "symbol_renamed"
    return None


def _restamped_mark(mark: ReviewMark, unit: ReviewUnit, previous: ReviewUnit | None) -> ReviewMark:
    """*mark* stamped for the unit it now sits on, when the unit shows nothing the verdict did not cover.

    A fingerprint that covers deleted text (``units.symbol_fingerprint``) describes the place the definition sat as
    well as the definition. When a verdict that was current on its unit is looked at again with the same body and
    *no* deleted text beside it (the definition moved to another file, its file was renamed, or the revision no longer
    puts a replaced neighbour's lines in its block), every line shown is a line the verdict covered, so it is
    restamped to the new fingerprint instead of reopening. A unit that shows deleted text the verdict never saw has a
    different fingerprint and is never restamped.
    """

    if (
        previous is not None
        and mark.content_fingerprint == previous.content_fingerprint
        and previous.fingerprint_method != "unknown"
        and unit.fingerprint_method != "unknown"
        and owns_deleted_text(mark.content_fingerprint)
        and not owns_deleted_text(unit.content_fingerprint)
        and bare_fingerprint(mark.content_fingerprint) == split_anchor(unit.content_fingerprint)[0]
        # The verdict's place in an ordered file is not what this restamp may rewrite, and a verdict that recorded none
        # is not given the one the part has now (it is read, ``units.place_reason``).
        and split_anchor(mark.content_fingerprint)[1] == split_anchor(unit.content_fingerprint)[1]
    ):
        return replace(mark, content_fingerprint=unit.content_fingerprint)
    return mark


def symbol_move_candidates(
    previous_units: Sequence[ReviewUnit], next_units: Sequence[ReviewUnit]
) -> tuple[tuple[ReviewUnit, ...], tuple[ReviewUnit, ...]]:
    """Symbol units that left the previous revision, and symbol units that are new in the next one."""

    previous_keys = {unit.unit_key for unit in previous_units}
    next_keys = {unit.unit_key for unit in next_units}
    return (
        tuple(u for u in previous_units if u.kind == "symbol" and u.unit_key not in next_keys),
        tuple(u for u in next_units if u.kind == "symbol" and u.unit_key not in previous_keys),
    )


def detect_symbol_moves(
    previous_units: Sequence[ReviewUnit],
    next_units: Sequence[ReviewUnit],
    *,
    body_digests: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    claimed_old: frozenset[str] = frozenset(),
    claimed_new: frozenset[str] = frozenset(),
) -> tuple[SymbolMove, ...]:
    """One-to-one symbol moves and renames between two revisions.

    A **move** needs an identical, knowable ``content_fingerprint`` (same method)
    on exactly one symbol that disappeared and exactly one that appeared, in a
    different file, and on no other symbol of either revision. Any duplicate on
    either side -- a copy that stays, a second removed twin, a second added twin
    -- makes it ambiguous and nothing is detected: a verdict may only follow
    content to a place when there is exactly one place it could have gone.

    A **rename** is the same one-to-one test over *body_digests* (the body with
    its own name masked, ``unit_key -> digest``, supplied by the caller for
    :func:`symbol_move_candidates` only) for symbols that did not move. A
    fingerprint-identical pair that failed the move test is ambiguous, not
    renamed.

    *headers* (``unit_key -> normalised decorator/annotation lines above the window``, see
    :func:`units.symbol_header`) is the one thing a fingerprint does not cover: the window of a Python function
    starts at ``def``. When it is given, a move needs both sides' header present and equal, so a function that lost
    or gained ``@login_required`` on the way is not carried (its judgment reopens, as before this rule existed).
    ``None`` means the caller read no source text, so nothing can prove the decorators agree: no move or rename is
    reported (fail-closed; a caller that omits it keeps every verdict reopened rather than carried on a guess).

    *claimed_old*/*claimed_new* are keys a file rename already accounts for.
    """

    if headers is None:
        return ()
    removed, added = symbol_move_candidates(previous_units, next_units)
    removed = tuple(u for u in removed if u.unit_key not in claimed_old)
    added = tuple(u for u in added if u.unit_key not in claimed_new)
    prev_count: dict[str, int] = {}
    for unit in previous_units:
        if unit.kind == "symbol":
            bare = bare_fingerprint(unit.content_fingerprint)
            prev_count[bare] = prev_count.get(bare, 0) + 1
    next_count: dict[str, int] = {}
    for unit in next_units:
        if unit.kind == "symbol":
            bare = bare_fingerprint(unit.content_fingerprint)
            next_count[bare] = next_count.get(bare, 0) + 1
    removed_by_fp: dict[str, list[ReviewUnit]] = {}
    for unit in removed:
        removed_by_fp.setdefault(bare_fingerprint(unit.content_fingerprint), []).append(unit)
    added_by_fp: dict[str, list[ReviewUnit]] = {}
    for unit in added:
        added_by_fp.setdefault(bare_fingerprint(unit.content_fingerprint), []).append(unit)

    moves: list[SymbolMove] = []
    taken_old: set[str] = set()
    taken_new: set[str] = set()
    for fingerprint, olds in removed_by_fp.items():
        news = added_by_fp.get(fingerprint, [])
        if len(olds) != 1 or len(news) != 1 or prev_count[fingerprint] != 1 or next_count[fingerprint] != 1:
            continue
        old, new = olds[0], news[0]
        if (
            old.fingerprint_method == "unknown"
            or old.fingerprint_method != new.fingerprint_method
            or old.path == new.path
        ):
            continue
        if owns_deleted_text(new.content_fingerprint) and new.content_fingerprint != old.content_fingerprint:
            continue  # the destination shows deleted text the verdict never covered: not a move, it reopens
        if old.unit_key not in headers or new.unit_key not in headers or headers[old.unit_key] != headers[new.unit_key]:
            continue  # decorators or annotations differ, or could not be read: not provably the same definition
        moves.append(_symbol_move("moved", old, new))
        taken_old.add(old.unit_key)
        taken_new.add(new.unit_key)

    digests = body_digests or {}
    old_by_digest: dict[str, list[ReviewUnit]] = {}
    for unit in removed:
        digest = digests.get(unit.unit_key, "")
        if digest and unit.unit_key not in taken_old and unit.fingerprint_method != "unknown":
            old_by_digest.setdefault(digest, []).append(unit)
    new_by_digest: dict[str, list[ReviewUnit]] = {}
    for unit in added:
        digest = digests.get(unit.unit_key, "")
        if digest and unit.unit_key not in taken_new and unit.fingerprint_method != "unknown":
            new_by_digest.setdefault(digest, []).append(unit)
    for digest, olds in old_by_digest.items():
        news = new_by_digest.get(digest, [])
        if len(olds) != 1 or len(news) != 1:
            continue
        old, new = olds[0], news[0]
        if (
            bare_fingerprint(old.content_fingerprint) == bare_fingerprint(new.content_fingerprint)
            or old.fingerprint_method != new.fingerprint_method
        ):
            continue
        moves.append(_symbol_move("renamed", old, new))
    return tuple(moves)


def _symbol_move(kind: Literal["moved", "renamed"], old: ReviewUnit, new: ReviewUnit) -> SymbolMove:
    return SymbolMove(
        kind=kind,
        old_key=old.unit_key,
        new_key=new.unit_key,
        previous_path=old.path,
        previous_symbol=old.symbol,
        path=new.path,
        symbol=new.symbol,
    )


# ``behavior.BEHAVIOR_KEY_PREFIX``, spelled here so this derivation root does not
# import the behavior contract (a test pins the two equal).
_BEHAVIOR_MARK_PREFIX = "beh:"


def reconcile(
    previous_units: Sequence[ReviewUnit],
    next_units: Sequence[ReviewUnit],
    marks: Sequence[ReviewMark],
    *,
    renames: Sequence[tuple[str, str]] = (),
    base_only: frozenset[str] = frozenset(),
    body_digests: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Reconciliation:
    """Carry *marks* from *previous_units* onto *next_units*, or refuse to.

    A symbol that left one file and appeared in another with an identical
    fingerprint, one-to-one, carries its mark (``symbol_moved``); see
    :func:`detect_symbol_moves`. *body_digests* (name-masked bodies of the
    :func:`symbol_move_candidates`) lets a same-body rename be recognised: its
    mark reopens as ``changed_since_review`` under ``symbol_renamed`` and is
    never carried. *headers* is the decorator/annotation text above each
    candidate (see :func:`detect_symbol_moves`); a move needs it equal on both
    sides, and without it no symbol move is followed.

    *base_only* names next-revision unit keys a deterministic classifier
    (:mod:`rebase_classification`) proved moved only with the diff base. A
    ``reviewed``/``needs_changes`` mark on such a unit is carried with a
    re-stamped fingerprint instead of reopening -- but only when the mark was
    current on *previous_units* (it attested exactly the bytes being compared);
    a mark already stale there keeps its normal reconciliation.

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
    order_moved = _order_moved(previous_units, next_units, aliases)
    moves_by_old = {
        move.old_key: move
        for move in detect_symbol_moves(
            previous_units,
            next_units,
            body_digests=body_digests,
            headers=headers,
            claimed_old=frozenset(aliases),
            claimed_new=frozenset(arrivals),
        )
    }
    applied: dict[tuple[str, str], SymbolMove] = {}
    moved_aliases: dict[str, str] = {}
    # A reviewer's own decision on the destination is newer than a verdict on the
    # source and is never overwritten by following the move: the old mark is
    # dropped as before, and the destination keeps exactly one outcome and reason.
    judged = {(mark.reviewer_id, mark.unit_key) for mark in marks}

    carried: list[ReviewMark] = []
    rebased: list[ReviewMark] = []
    restamped: list[ReviewMark] = []
    reopened: list[ReviewMark] = []
    dropped: list[ReviewMark] = []
    reasons: list[tuple[str, str, str]] = []
    for mark in marks:
        if mark.unit_key.startswith(_BEHAVIOR_MARK_PREFIX):
            # Behavior judgments are not unit verdicts: the behavior target is
            # re-observed after capture, and its fingerprint decides on read
            # (``behavior.behavior_targets``) whether the judgment still holds.
            carried.append(mark)
            reasons.append((mark.reviewer_id, mark.unit_key, "behavior_mark_kept"))
            continue
        key = aliases.get(mark.unit_key, mark.unit_key)
        unit = next_by_key.get(key)
        if unit is None:
            move = moves_by_old.get(mark.unit_key)
            followed = (
                _follow_move(mark, move, prev_by_key, next_by_key, rehunked)
                if move is not None and (mark.reviewer_id, move.new_key) not in judged
                else None
            )
            if move is not None and followed is not None:
                moved_mark, code = followed
                reopened.append(moved_mark)
                reasons.append((mark.reviewer_id, move.new_key, code))
                dropped.append(mark)
                applied[(mark.reviewer_id, move.old_key)] = replace(move, reviewer_id=mark.reviewer_id)
                if move.kind == "moved":
                    # lc-debt: keyed by old key across reviewers, so one reviewer's followed move marks the unit as moved for the
                    # unit-level added/removed lists even when another reviewer's own destination mark stopped theirs; the lists
                    # are about units, not verdicts. Key it by (reviewer, old_key) if a per-reviewer delta is ever surfaced.
                    moved_aliases[move.old_key] = move.new_key
                continue
            # The unit left the review. Its mark is not "unreviewed" -- there is
            # nothing left to review -- so the row goes rather than lingering as
            # a verdict on something that no longer exists.
            dropped.append(mark)
            continue
        previous = prev_by_key.get(mark.unit_key)
        if (
            key == mark.unit_key
            and key in base_only
            and previous is not None
            and mark.state in ("reviewed", "needs_changes")
            and previous.fingerprint_method != "unknown"
            and unit.fingerprint_method != "unknown"
            and mark.content_fingerprint == previous.content_fingerprint
            and unit.content_fingerprint != mark.content_fingerprint
            # An anchored part needs an anchored verdict: one from before places were kept is read once (``place_reason``)
            # and not given the place the part has now. An anchored verdict whose anchor moved only with the base passes.
            and (split_anchor(mark.content_fingerprint)[1] or not split_anchor(unit.content_fingerprint)[1])
        ):
            # The judged bytes moved only with the base under a patch-equivalent
            # authored change: same verdict, re-stamped to what is now on screen.
            rebased.append(replace(mark, content_fingerprint=unit.content_fingerprint))
            reasons.append((mark.reviewer_id, key, "base_only_equivalent"))
            continue
        stamped = _restamped_mark(mark, unit, previous)
        state, reason = _next_state(stamped, unit, previous, rehunked)
        if reason in ("content_unchanged", "fingerprint_restored") and key in order_moved:
            # Same bytes, different place among the file's parts: in a shell or Make file that can change what runs. The reopened mark is stamped so that it stays reopened
            # (``units.ANCHOR_MOVED``): a later revision that moves nothing must not find it equal and clear it.
            state, reason = "changed_since_review", "part_order_changed"
            stamped = replace(stamped, content_fingerprint=with_anchor(stamped.content_fingerprint, ANCHOR_MOVED))
        if stamped is not mark and state == mark.state and key == mark.unit_key:
            restamped.append(stamped)
            reasons.append((mark.reviewer_id, key, "deleted_text_no_longer_shown"))
            continue
        reasons.append((mark.reviewer_id, key, reason))
        if state == mark.state and key == mark.unit_key and stamped is mark:
            carried.append(mark)
            continue
        # A re-stamped verdict is written back like a moved one: its stored fingerprint changes.
        reopened.append(replace(stamped, state=state, unit_key=key))
        if key != mark.unit_key:
            # The mark moved to a new primary key, so the old row is a duplicate
            # verdict on a path that no longer exists and has to go.
            dropped.append(mark)

    notes = tuple(
        f"{path}: hunk count changed {before} -> {after}; hunk marks reset "
        "(hunk keys are ordinal-derived and cannot survive a re-hunking)"
        for path, (before, after) in sorted(rehunked.items())
    )
    arrivals |= set(moved_aliases.values())
    aliases = {**aliases, **moved_aliases}
    # A renamed symbol is the same part under a new name that reopened a judgment (the target delta reads it so), so its
    # old key is not "removed" either; it stays out of ``aliases``/``arrivals`` because nothing was carried.
    renamed_old = {move.old_key for move in applied.values() if move.kind == "renamed"}
    return Reconciliation(
        carried=tuple(carried),
        reopened=tuple(reopened),
        restamped=tuple(restamped),
        added=tuple(key for key in next_by_key if key not in prev_by_key and key not in arrivals),
        removed=tuple(
            key for key in prev_by_key if key not in next_by_key and key not in aliases and key not in renamed_old
        ),
        dropped=tuple(dropped),
        notes=notes,
        aliases=tuple(sorted(aliases.items())),
        rebased=tuple(rebased),
        reasons=tuple(reasons),
        moves=tuple(applied.values()),
    )


def compare_revision_units(
    previous_units: Sequence[ReviewUnit],
    next_units: Sequence[ReviewUnit],
    *,
    previous_marks: Sequence[ReviewMark] = (),
    next_marks: Sequence[ReviewMark] = (),
) -> RevisionComparison:
    """Compare exactly what Review knew at two immutable snapshots.

    Stable ``unit_key`` identifies the same semantic target. Content equality is
    accepted only when both fingerprint methods are knowable; two ``unknown``
    placeholders never prove that code stayed the same. Returned ``units`` omit
    unchanged targets so large reviews stay navigable, while ``unchanged`` keeps
    the complete denominator visible.
    """

    before = {unit.unit_key: unit for unit in previous_units}
    after = {unit.unit_key: unit for unit in next_units}
    before_marks = {mark.unit_key: mark for mark in previous_marks}
    after_marks = {mark.unit_key: mark for mark in next_marks}
    changed_units: list[RevisionUnitDelta] = []
    counts = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0}
    per_path: dict[str, dict[str, int]] = {}

    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old is None:
            status = "added"
            unit = new
        elif new is None:
            status = "removed"
            unit = old
        else:
            known_identity = old.fingerprint_method != "unknown" and new.fingerprint_method != "unknown"
            status = "unchanged" if known_identity and old.content_fingerprint == new.content_fingerprint else "changed"
            unit = new
        assert unit is not None  # union key guarantees one side exists
        counts[status] += 1
        if status == "unchanged":
            continue
        path_counts = per_path.setdefault(
            unit.path,
            {
                "added": 0,
                "changed": 0,
                "removed": 0,
                "reviewed_before": 0,
                "reviewed_after": 0,
                "changed_since_review_after": 0,
                "needs_changes_after": 0,
            },
        )
        path_counts[status] += 1
        before_mark = before_marks.get(key)
        after_mark = after_marks.get(key)
        from_state = before_mark.state if before_mark is not None else "unreviewed"
        to_state = after_mark.state if after_mark is not None else "unreviewed"
        if from_state == "reviewed":
            path_counts["reviewed_before"] += 1
        if to_state == "reviewed":
            path_counts["reviewed_after"] += 1
        if to_state == "changed_since_review":
            path_counts["changed_since_review_after"] += 1
        if to_state == "needs_changes":
            path_counts["needs_changes_after"] += 1
        changed_units.append(
            RevisionUnitDelta(
                unit_key=key,
                path=unit.path,
                kind=unit.kind,
                symbol=unit.symbol,
                status=status,
                from_state=from_state,
                to_state=to_state,
                from_start_line=0 if old is None else old.start_line,
                to_start_line=0 if new is None else new.start_line,
            )
        )

    files: list[RevisionFileComparison] = []
    for path, row in sorted(per_path.items()):
        if row["changed"] or (row["added"] and row["removed"]):
            status = "changed"
        elif row["added"]:
            status = "added"
        else:
            status = "removed"
        files.append(
            RevisionFileComparison(
                path=path,
                status=status,
                added=row["added"],
                changed=row["changed"],
                removed=row["removed"],
                reviewed_before=row["reviewed_before"],
                reviewed_after=row["reviewed_after"],
                changed_since_review_after=row["changed_since_review_after"],
                needs_changes_after=row["needs_changes_after"],
            )
        )
    return RevisionComparison(
        added=counts["added"],
        changed=counts["changed"],
        removed=counts["removed"],
        unchanged=counts["unchanged"],
        units=tuple(changed_units),
        files=tuple(files),
    )


def snapshot_history_numbers(
    previous: tuple[Sequence[ReviewUnit], Sequence[Any]] | None,
    current: tuple[Sequence[ReviewUnit], Sequence[Any]],
) -> dict[str, Any]:
    """One Snapshot history row's numbers, from each Snapshot's units and Review targets.

    ``review_area_*`` count the non-overlapping Review areas the Reader shows
    (targets, by ``unit_key``); ``delta`` counts raw units. Kept here, beside
    ``compare_revision_units``, so the stored-result stamp covers this code.
    """

    units, targets = current
    if previous is None:
        return {
            "review_area_count": len(targets),
            "review_area_delta": {"added": len(targets), "changed": 0, "removed": 0},
            "delta": {"added": len(units), "changed": 0, "removed": 0},
        }
    previous_units, previous_targets = previous
    before_units = {unit.unit_key: unit for unit in previous_units}
    after_units = {unit.unit_key: unit for unit in units}
    before_keys = {target.unit_key for target in previous_targets}
    after_keys = {target.unit_key for target in targets}
    changed = 0
    for key in before_keys & after_keys:
        before = before_units.get(key)
        after = after_units.get(key)
        if before is None or after is None:
            changed += 1
            continue
        known_identity = before.fingerprint_method != "unknown" and after.fingerprint_method != "unknown"
        if not known_identity or before.content_fingerprint != after.content_fingerprint:
            changed += 1
    comparison = compare_revision_units(previous_units, units)
    return {
        "review_area_count": len(targets),
        "review_area_delta": {
            "added": len(after_keys - before_keys),
            "changed": changed,
            "removed": len(before_keys - after_keys),
        },
        "delta": {"added": comparison.added, "changed": comparison.changed, "removed": comparison.removed},
    }


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
                # "Decide later" attests nothing about content, so content moving under it re-opens nothing.
                changed_since_mark=(
                    mark is not None and mark.state != "later" and mark.content_fingerprint != unit.content_fingerprint
                ),
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


_KIND_ORDER: dict[str, int] = {"file": 0, "document_section": 1, "dependency": 2, "symbol": 3, "hunk": 4}


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
    "FileRevisionDelta",
    "FrontierGroups",
    "Reconciliation",
    "RevisionComparison",
    "RevisionFileComparison",
    "RevisionUnitDelta",
    "SymbolMove",
    "compare_revision_units",
    "compute_frontier",
    "detect_symbol_moves",
    "file_revision_delta",
    "group_frontier",
    "reconcile",
    "symbol_move_candidates",
]
