"""Where a comment lives after the code moved -- the relocation ladder.

A line number is not an anchor. It is a guess that survives until the first
insertion above it, and agents insert above things constantly. So an annotation
stores what it was actually attached to -- the selected text, the three lines
either side, the owning symbol, the whole-blob hash -- and this module's job is
to find that thing again in a later revision, or to say plainly that it could
not.

The ladder has six rungs, tried strongest first, and the first rung that yields
**exactly one** location wins:

1. ``identical_blob``     -- the file did not change; keep the line range.
2. ``exact_text_context`` -- the selected text is present with its neighbours.
3. ``symbol_text``        -- the owning definition still exists; look inside it.
4. ``unique_text``        -- the selected text occurs exactly once in the file.
5. ``unique_context``     -- the text is gone; anchor to where it used to sit.
6. ``orphaned``           -- admit it.

The rule that makes the other five trustworthy is rung 6:

    **If more than one location fits, do not guess.**

An annotation quietly redrawn onto a plausible-looking line is worse than one
that admits it lost its footing, because the reader has no way to tell the two
apart. Every rung that finds two candidates falls straight to ``orphaned`` and
says how many it found.

The counterpart rule, and it is not optional either: **a candidate has to be a
real one**. "Occurs once" is counted in the units the selection was made in --
:func:`build_anchor` captures whole lines, so ``"    return x"`` means *that
line*, and a line reading ``"    return x + 0"`` elsewhere in the file is a
different line, not a second home. Counting raw substrings made every comment
whose line is a prefix of some other line orphan itself on the next revision,
which is refusing to answer a question nobody asked.

And it cuts the other way just as hard: when the stored line is **gone**, a
longer line that happens to start with it is not a quiet second-best, it is a
confident line number in a function the reviewer never wrote a word about. A
line that is gone is rung 5's business, not rung 4's. See
:func:`_candidate_offsets` and :func:`_tail_may_be_cut`.

``diff-match-patch`` (through
:func:`~lemoncrow.pro.capabilities.tool_supervision.fuzzy_match.find_best_fuzzy_window`)
appears at rung 5 only, and only as a **candidate locator**. It proposes; this
module disposes. It already raises
:class:`FuzzyAmbiguousMatchError` on a genuine tie, and that exception is caught
and turned into an orphan -- never unwrapped to take ``best`` anyway.

Three states are deliberately distinct and must never be collapsed into one:

``orphaned``
    We looked, and the answer was ambiguous or absent. The annotation needs a
    human.
``removed``
    The file is gone from this revision. The annotation is obsolete, and that
    is a fact rather than a failure.
``unresolved``
    We could not look at all -- binary, oversize, unreadable -- or there was
    nothing on the anchor to look *for*: no selected text and no context that
    survives a blank-line trim (:func:`_has_a_locator`). **Never orphan because
    we could not look, and never orphan for failing to find a locator that was
    never recorded.** That is the difference between "this moved" and "we did
    not read it", and a reviewer who is shown the first when the truth is the
    second will go hunting for a change nobody made.

    This holds whether or not the file changed underneath the comment. A file
    that changed while the anchor recorded no locator is still "we had nothing
    to look with", not "we looked and it is gone": ``orphaned`` asserts that a
    search ran and came back empty or ambiguous, and no search runs without a
    query. What the changed file does alter is the *sentence* the reader gets.
    "Never anchored" is only true when nothing was ever recorded; when the
    anchor carries a real ``blob_sha`` the file was read at capture time and
    has since moved on, so :func:`resolve_anchor` says so instead. Same status,
    two different explanations, both of them true.

Pure and I/O-free: every input is text the caller already has.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Literal

from lemoncrow.pro.capabilities.review.impact import symbol_windows
from lemoncrow.pro.capabilities.review.session_models import (
    _ANCHOR_CONTEXT_LINES,
    _ANCHOR_TEXT_CAP,
    AnchorMethod,
    AnchorSide,
    AnnotationAnchor,
)
from lemoncrow.pro.capabilities.review.units import (
    normalize_body,
    qualified_window_names,
    symbol_fingerprint,
)

AnchorStatus = Literal["unchanged", "relocated", "orphaned", "removed", "unresolved", "stale"]
"""What happened to an anchor on this revision, and it keys on the rung, not the line.

``unchanged`` belongs to rung 1 alone. Rung 1 compares whole-blob hashes: the
file is byte-identical, so the stored line range was never re-derived and the
line the reviewer picked is still literally that line.

``relocated`` is every other success, **including the ones that landed on the
same line number**. Rungs 2-5 searched for the text again; that a re-find
happened to agree with the old number is a coincidence of the edit, not
evidence that nothing moved. Keying the status on line movement instead made a
heuristic re-find indistinguishable from an untouched file -- a comment sitting
inside a function that no longer has the name, signature or docstring it was
written against, drawn with the same weight as one whose blob never changed.
That is exactly the silent move plan SS4.4 forbids.
"""
ANCHOR_STATUSES: tuple[str, ...] = ("unchanged", "relocated", "orphaned", "removed", "unresolved", "stale")


@dataclass(frozen=True)
class AnchorResolution:
    """One attempt to find an annotation again, and its full explanation.

    *anchor* is always populated. On a failure it is the anchor as it stood
    going in (with a rewritten ``path`` when the file was renamed), so a surface
    can still show the reader where the comment *used to* point while labelling
    it orphaned -- the spec's "keep the previous lines, for display only".

    *detail* is written to be read by a human in a list of orphans, so it names
    the count that defeated the rung (``"3 candidate locations"``), never just
    ``"ambiguous"``.
    """

    status: AnchorStatus
    method: AnchorMethod
    anchor: AnnotationAnchor
    detail: str = ""

    @property
    def located(self) -> bool:
        """True when the annotation has a position this revision can be trusted with."""

        return self.status in ("unchanged", "relocated")


def blob_sha(text: str) -> str:
    """The whole-file identity rung 1 compares.

    Raw bytes, deliberately **not** :func:`normalize_body`: rung 1's promise is
    "nothing in this file moved, so the stored line numbers are still exact",
    and a normalization that forgave a trailing space would also forgive the
    inserted blank line that shifted every line below it.
    """

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def context_hash(text: str) -> str:
    """Hash of one context block, normalized so reformatting does not break it.

    Shares :func:`~lemoncrow.pro.capabilities.review.units.normalize_body` with
    the fingerprints, so "the code around this comment changed" means the same
    thing to the anchor ladder and to the review frontier.
    """

    return hashlib.sha256(normalize_body(text).encode("utf-8")).hexdigest()


def _line_slice(lines: list[str], start: int, end: int) -> str:
    """Lines ``[start, end]`` 1-based inclusive, clamped, joined with ``\\n``."""

    lo = max(1, start)
    hi = min(len(lines), end)
    if hi < lo:
        return ""
    return "\n".join(lines[lo - 1 : hi])


def build_anchor(
    *,
    path: str,
    text: str,
    start_line: int,
    end_line: int,
    side: AnchorSide = "new",
    unit_key: str = "",
    symbol_qualified_name: str = "",
    symbol_fingerprint: str = "",
) -> AnnotationAnchor:
    """Capture everything the ladder will need, from the text on screen right now.

    Called at annotation time, and again by :func:`resolve_anchor` after a
    successful relocation -- refreshing the neighbourhood so the *next* revision
    is compared against what the reviewer would see today rather than against a
    context that has drifted three revisions out of date. The append-only
    ``annotation_anchor_events`` rows keep the superseded anchors, so refreshing
    loses no history.

    The verbatim text is capped: rungs 2, 4 and 5 *search* for it, so it cannot
    be replaced by its hash, but one pathological selection must not turn one
    comment into a megabyte row.
    """

    lines = text.splitlines()
    lo = max(1, start_line)
    hi = max(lo, end_line)
    selected = _line_slice(lines, lo, hi)[:_ANCHOR_TEXT_CAP]
    before = _line_slice(lines, lo - _ANCHOR_CONTEXT_LINES, lo - 1)
    after = _line_slice(lines, hi + 1, hi + _ANCHOR_CONTEXT_LINES)
    return AnnotationAnchor(
        path=path,
        side=side,
        start_line=lo,
        end_line=hi,
        selected_text_hash=context_hash(selected),
        selected_text=selected,
        before_context_hash=context_hash(before),
        after_context_hash=context_hash(after),
        before_context=before,
        after_context=after,
        symbol_qualified_name=symbol_qualified_name,
        symbol_fingerprint=symbol_fingerprint,
        blob_sha=blob_sha(text),
        unit_key=unit_key,
    )


def _occurrences(haystack: str, needle: str) -> list[int]:
    """Every character offset at which *needle* occurs, overlaps included.

    Overlapping hits are counted because they are still distinct locations a
    reader could mean, and rung 6 exists precisely to refuse to choose between
    distinct locations.
    """

    if not needle:
        return []
    out: list[int] = []
    cursor = haystack.find(needle)
    while cursor != -1:
        out.append(cursor)
        cursor = haystack.find(needle, cursor + 1)
    return out


def _starts_line(haystack: str, position: int) -> bool:
    """Whether *position* is the first character of a line of *haystack*."""

    return position == 0 or haystack[position - 1] == "\n"


def _occupies_whole_lines(haystack: str, position: int, needle: str) -> bool:
    """Whether *needle* at *position* takes up complete lines of *haystack*.

    True when the hit starts a line and ends one. ``"    return x"`` found at
    the start of ``"    return x\\n"`` qualifies; the same characters found
    inside ``"    return x + 0\\n"`` do not, because they are the beginning of a
    different line rather than a copy of this one.
    """

    if not _starts_line(haystack, position):
        return False
    end = position + len(needle)
    return end >= len(haystack) or haystack[end] in "\n\r"


def _reads_as_a_fragment(haystack: str, position: int) -> bool:
    """Whether a hit can only be read as a piece of a line, never as a line.

    True when the hit begins mid-line with real code before it on that line --
    ``"value + 1"`` inside ``"    total = value + 1"``. Such a hit is a
    fragment location and nothing else, because the characters to its left rule
    out its being a copy of any line.

    False for a hit whose line holds only whitespace before it:
    ``"    return x"`` inside ``"        return x"`` is the stored line under
    four more spaces of indentation -- a *different line*, not a piece of one.
    Re-indentation is the commonest edit there is, and taking the re-indented
    twin of a line that vanished as a confident location is the same
    fabrication as taking a longer line that merely starts with it.
    """

    line_start = haystack.rfind("\n", 0, position) + 1
    return haystack[line_start:position].strip() != ""


def _tail_may_be_cut(anchor: AnnotationAnchor) -> bool:
    """Whether the stored selection may stop in the middle of a line.

    Asked of the **record**, not of the text it is being hunted in, because the
    record is the only thing that knows how the selection was made.
    :func:`build_anchor` is the only producer of ``selected_text`` and it slices
    whole lines, so a stored selection is a line range -- with exactly one
    exception, and the record names it: ``_ANCHOR_TEXT_CAP`` trims the tail of a
    pathological selection, and a stored text that long is a text that may have
    been cut. The cap only ever cuts the *tail*; the head is a line start by
    construction, truncated or not.

    That is the distinction :func:`_candidate_offsets` turns on. "The selection
    was cut mid-line" and "the selection was a line and that line is gone" leave
    identically-shaped hits behind -- a longer line beginning with the stored
    text -- and only this answer separates them.
    """

    return len(anchor.selected_text) >= _ANCHOR_TEXT_CAP


def _candidate_offsets(haystack: str, needle: str, *, tail_may_be_cut: bool) -> list[int]:
    """The offsets a rung is allowed to choose between.

    :func:`build_anchor` slices whole lines, so a stored selection of
    ``"    return x"`` is a **line range**, and rung 4's promise -- "the selected
    text occurs exactly once" -- has to be counted in line ranges too: unique
    means *no other line range in the file equals this one*. A raw substring
    scan also hits inside ``"    return x + 0"``, so a comment on an untouched,
    byte-identical line was orphaned as "2 candidate locations" because an
    unrelated line elsewhere happened to start the same way. Nothing was
    ambiguous; the counting was.

    So when any occurrence occupies whole lines, only those are candidates. Two
    genuinely identical lines are two whole-line occurrences, stay two
    candidates, and still orphan.

    When **none** does, the counting is not the only thing at stake any more --
    the line the comment was written on is not in this file, and falling back to
    raw substrings answers that with the wrong question's answer. A comment on
    ``"    return x"`` inside a deleted ``beta`` was re-found inside
    ``"    return x + 0"`` in an unrelated ``gamma``: one candidate, "selected
    text occurs exactly once", a confident line number in a function the
    reviewer never commented on. The honest answer to a vanished line is rung 5,
    the seam it used to sit in, or rung 6.

    So the fallback keeps only the hits that a *whole-line* reading cannot
    explain away:

    * ``tail_may_be_cut`` -- :func:`_tail_may_be_cut` says the cap may have
      trimmed the stored text mid-line, so its tail cannot be checked against a
      line end. Its head still can: candidates are the hits that start a line.
    * otherwise the stored text is a whole line, and a hit that starts a line
      without ending one is a longer line that merely begins with it, while a
      hit under nothing but indentation is that line re-indented. Both are
      *other lines*; neither is a home. What survives is
      :func:`_reads_as_a_fragment` -- hits that only a fragment reading can
      account for, which is what keeps a genuine intra-line selection findable.

    Nothing that was ever ambiguous is narrowed here, and nothing that was ever
    a real location is dropped: every rejected hit is a hit on a line that is
    not the stored one.
    """

    hits = _occurrences(haystack, needle)
    whole_lines = [pos for pos in hits if _occupies_whole_lines(haystack, pos, needle)]
    if whole_lines:
        return whole_lines
    if tail_may_be_cut:
        return [pos for pos in hits if _starts_line(haystack, pos)]
    return [pos for pos in hits if _reads_as_a_fragment(haystack, pos)]


def _line_offsets(text: str) -> list[int]:
    """Character offset at which each 1-based line starts, plus a terminator."""

    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _line_of(offsets: list[int], position: int) -> int:
    """1-based line containing character *position*."""

    lo, hi = 0, len(offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if offsets[mid] <= position:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _span_for(text: str, offsets: list[int], position: int, needle: str) -> tuple[int, int]:
    """The 1-based inclusive line span occupied by *needle* found at *position*."""

    start = _line_of(offsets, position)
    end = _line_of(offsets, max(position, position + len(needle) - 1))
    return start, max(start, end)


def _context_matches(anchor: AnnotationAnchor, lines: list[str], start: int, end: int) -> bool:
    """Whether the neighbourhood of ``[start, end]`` is the one the anchor recorded.

    Both sides must agree. Accepting a one-sided match would promote "the same
    three lines happen to sit above two different copies of this text" into a
    unique location, which is exactly the guess rung 6 exists to refuse.
    """

    before = _line_slice(lines, start - _ANCHOR_CONTEXT_LINES, start - 1)
    after = _line_slice(lines, end + 1, end + _ANCHOR_CONTEXT_LINES)
    return context_hash(before) == anchor.before_context_hash and context_hash(after) == anchor.after_context_hash


def _owning_symbol(text: str, path: str, start: int, end: int) -> tuple[str, str]:
    """The innermost definition containing ``[start, end]``, as ``(name, fingerprint)``.

    Re-derived rather than carried forward, because the stored name describes
    the revision the comment was *written* on. A reviewer renames the function
    and rewrites its docstring; the selected line survives verbatim; carrying
    the old name over prints ``in compute_total`` under a comment that now sits
    inside ``total_price``. That is a fabricated fact about the current
    revision, and a reader has no way to catch it.

    ``("", "")`` when no definition contains the range -- module level, an
    unparseable file, a language with no span support. Empty is the honest
    answer there; a nearest-neighbour guess would not be.
    """

    windows = symbol_windows(text, path)
    names = qualified_window_names(windows)
    best: tuple[int, str, str] | None = None
    for window, qualified in zip(windows, names, strict=True):
        if window.start_line > start or end > window.end_line:
            continue
        span = window.end_line - window.start_line
        if best is None or span < best[0]:
            best = (span, qualified, symbol_fingerprint(window.kind, qualified, window.body))
    if best is None:
        return "", ""
    return best[1], best[2]


def _relocated(
    anchor: AnnotationAnchor,
    *,
    text: str,
    start: int,
    end: int,
    method: AnchorMethod,
    detail: str,
) -> AnchorResolution:
    """A successful move: refresh the neighbourhood and the owning definition.

    Always ``relocated``, never ``unchanged``: see :data:`AnchorStatus`. Only
    rung 1 may claim the line was never re-derived, and rung 1 does not come
    through here.
    """

    name, fingerprint = _owning_symbol(text, anchor.path, start, end)
    moved = build_anchor(
        path=anchor.path,
        text=text,
        start_line=start,
        end_line=end,
        side=anchor.side,
        unit_key=anchor.unit_key,
        symbol_qualified_name=name,
        symbol_fingerprint=fingerprint,
    )
    return AnchorResolution(status="relocated", method=method, anchor=moved, detail=detail)


def _with_owning_symbol(anchor: AnnotationAnchor, text: str) -> AnnotationAnchor:
    """Rung 1's anchor, with the definition that owns it on this revision.

    Rung 1 keeps the stored line range untouched -- and used to keep the stored
    symbol fields untouched with it, which are empty for every comment whose
    creation-time unit lookup found none (a line in a definition this revision
    did not change has no symbol unit to be owned by). The reviewer then reads
    ``in consumer`` under one correctly-placed comment and nothing at all under
    the one three lines below it, with no way to see that the difference is
    bookkeeping rather than a fact about their code.

    Naming it here is a lookup, not a guess: the blob is byte-identical, so
    ``[start_line, end_line]`` points into exactly this text and the containing
    definition is read straight off it -- the same call every other rung makes
    through :func:`_relocated`.

    An empty derivation (module level, unparseable, a language with no span
    support) leaves the stored fields alone rather than blanking them: on an
    identical blob the stored name still describes this very text, so replacing
    a fact with a silence would be a loss, not an honesty.
    """

    name, fingerprint = _owning_symbol(text, anchor.path, anchor.start_line, anchor.end_line)
    if not name:
        return anchor
    return replace(anchor, symbol_qualified_name=name, symbol_fingerprint=fingerprint)


def _orphan(anchor: AnnotationAnchor, detail: str) -> AnchorResolution:
    """Rung 6. The previous line range is kept for display and claims nothing."""

    return AnchorResolution(status="orphaned", method="orphaned", anchor=anchor, detail=detail)


def _has_a_locator(anchor: AnnotationAnchor) -> bool:
    """Is there anything here a rung could actually search for?

    "Usable" is stricter than "non-empty string", and it is **rung 5** that sets
    the bar, because rung 5 is the rung this question stands in front of: it is
    the only one that runs on an empty ``selected_text``. :func:`_context_rung`
    trims blank edges off both context blocks before probing, so a context of
    two newlines is a truthy string that collapses to nothing the moment it is
    used -- and an anchor let past on the strength of it reaches rung 6 and is
    orphaned for failing to find a locator it never had. An empty context and a
    context of nothing but blank lines are the same amount of evidence, and it
    is none.

    ``selected_text`` is taken as-is, without stripping: rungs 2-4 search for it
    verbatim in whole-line units, so a line of nothing but spaces is a line they
    can genuinely match.
    """

    return bool(anchor.selected_text) or bool(anchor.before_context.strip()) or bool(anchor.after_context.strip())


def resolve_anchor(
    anchor: AnnotationAnchor,
    *,
    new_text: str | None,
    path: str = "",
    file_status: str = "modified",
    new_blob_sha: str = "",
) -> AnchorResolution:
    """Find *anchor* in the new revision, or refuse to.

    *new_text* is the new-side text of the file, or ``None`` when there is none
    to read (binary, ``large_file_skipped``, ``blob_unreadable``). ``None`` is
    ``unresolved``, never ``orphaned``.

    *path* is the file's path in the **new** revision. When it differs from
    ``anchor.path`` the file was renamed: the anchor's path is rewritten, the
    rename is named in ``detail``, and the ladder then runs normally -- a rename
    is not a reason to lose a comment.

    *new_blob_sha* is computed from *new_text* when the caller does not have it.

    Never raises.
    """

    renamed = bool(path) and path != anchor.path
    working = replace(anchor, path=path) if renamed else anchor
    prefix = f"file renamed {anchor.path} -> {path}; " if renamed else ""

    if file_status == "deleted":
        return AnchorResolution(
            status="removed",
            method="removed",
            anchor=working,
            detail=f"{prefix}file deleted in this revision",
        )
    if new_text is None:
        return AnchorResolution(
            status="unresolved",
            method="unresolved",
            anchor=working,
            detail=f"{prefix}new-side text unavailable; not looked at, so not orphaned",
        )

    current_sha = new_blob_sha or blob_sha(new_text)
    lines = new_text.splitlines()

    # Rung 1 -- the file is byte-identical, so the stored lines are still exact.
    # This runs *before* the never-anchored guard below, and the order matters:
    # a comment can have a real `blob_sha` and still carry no locator (line 1 of
    # a file whose whole content is one blank line selects no text and has no
    # neighbours). The blob proves that file is untouched, which is a sighting,
    # not a derivation -- rung 1 has to be given the chance to say so.
    if working.blob_sha and current_sha == working.blob_sha:
        return AnchorResolution(
            status="unchanged",
            method="identical_blob",
            anchor=_with_owning_symbol(working, new_text),
            detail=f"{prefix}file unchanged in this revision",
        )

    if not _has_a_locator(working):
        # There is nothing here a rung could search with: no selected text, and
        # no context that survives the blank-line trim rung 5 applies. Nothing
        # to relocate and nothing to be ambiguous about, so this is
        # `unresolved`, never `orphaned`: we did not fail to find it, we never
        # had anything to look for. Orphaning is not cosmetic either -- it
        # writes `state='orphaned'` onto a live comment and moves it into the
        # bucket the reviewer is told to ignore, for missing a locator that was
        # never recorded.
        #
        # The test is "no usable locator", not "no selected text": a comment on
        # a blank line selects no text and is still anchored, by its two context
        # blocks, and rung 5 relocates it perfectly well. Keying this branch on
        # an empty `selected_text` beside an empty `blob_sha` froze exactly
        # those comments, because rung 5 clears `blob_sha` on the way past.
        #
        # Two different stories reach this line and the reader is owed the one
        # that is true of their comment. With no `blob_sha` the comment really
        # was never anchored to anything -- the file was binary or unreadable
        # when it was written, or it sits on the old side of the diff, where
        # there is no new-side text to follow. With one, rung 1 has just
        # declined it, so the file *was* read when the comment was written and
        # its bytes have changed since; telling that reader "never anchored"
        # would be a second untruth stacked on the one this branch exists to
        # stop.
        detail = (
            "the file changed; this comment recorded no text or context to relocate it by"
            if working.blob_sha
            else "this comment was never anchored to text or context; nothing to relocate it by"
        )
        return AnchorResolution(
            status="unresolved",
            method="unresolved",
            anchor=working,
            detail=f"{prefix}{detail}",
        )

    selected = working.selected_text
    if not selected:
        # Nothing to search for. Rung 5 is the only rung that does not need it,
        # and the guard above has already established it has something to work
        # with, so the seam probe below cannot come up empty here.
        return _context_rung(working, new_text=new_text, lines=lines, prefix=prefix)

    offsets = _line_offsets(new_text)
    tail_may_be_cut = _tail_may_be_cut(working)
    hits = _candidate_offsets(new_text, selected, tail_may_be_cut=tail_may_be_cut)

    # Rung 2 -- exact text whose neighbours also match.
    with_context = [
        pos for pos in hits if _context_matches(working, lines, *_span_for(new_text, offsets, pos, selected))
    ]
    if len(with_context) == 1:
        start, end = _span_for(new_text, offsets, with_context[0], selected)
        return _relocated(
            working,
            text=new_text,
            start=start,
            end=end,
            method="exact_text_context",
            detail=f"{prefix}selected text and both context blocks matched",
        )
    if len(with_context) > 1:
        return _orphan(working, f"{prefix}{len(with_context)} candidate locations with identical context")

    # Rung 3 -- the owning definition still exists; look only inside it.
    resolution = _symbol_rung(working, new_text=new_text, prefix=prefix)
    if resolution is not None:
        return resolution

    # Rung 4 -- one occurrence in the whole file is a location; two is a guess.
    if len(hits) == 1:
        start, end = _span_for(new_text, offsets, hits[0], selected)
        return _relocated(
            working,
            text=new_text,
            start=start,
            end=end,
            method="unique_text",
            detail=f"{prefix}selected text occurs exactly once",
        )
    if len(hits) > 1:
        return _orphan(working, f"{prefix}{len(hits)} candidate locations for the selected text")

    # Rung 5 -- the text itself is gone; anchor to where it used to sit.
    return _context_rung(working, new_text=new_text, lines=lines, prefix=prefix)


def _symbol_rung(anchor: AnnotationAnchor, *, new_text: str, prefix: str) -> AnchorResolution | None:
    """Rung 3, or ``None`` to fall through to rung 4.

    ``None`` means "this rung had nothing to say" -- no recorded symbol, no
    window by that name, or no occurrence inside it. A rung that *did* have
    something to say and found it ambiguous returns an orphan instead of
    letting the wider search downstairs pick a winner it already rejected.
    """

    name = anchor.symbol_qualified_name
    if not name:
        return None
    # One parse, then a few passes over its result. Parsing twice to try a second
    # name would double the cost of every rung-3 miss on a large file.
    candidates = symbol_windows(new_text, anchor.path)
    qualified = qualified_window_names(candidates)
    windows = [window for window, dotted in zip(candidates, qualified, strict=True) if dotted == name]
    if not windows:
        windows = [window for window in candidates if window.name == name]
    if not windows:
        # A qualified name the index knew (``Class.method``) may not match the
        # containment name, so fall back to the leaf before concluding it is
        # gone. This is the weakest match and the one most likely to be
        # ambiguous, which is exactly what the count check below is for.
        leaf = name.rpartition(".")[2]
        windows = [window for window in candidates if window.name == leaf] if leaf else []
    if not windows:
        return None
    if len(windows) > 1:
        return _orphan(anchor, f"{prefix}{len(windows)} definitions named {name}; not guessing between them")

    window = windows[0]
    inner = _candidate_offsets(window.body, anchor.selected_text, tail_may_be_cut=_tail_may_be_cut(anchor))
    if not inner:
        return None
    if len(inner) > 1:
        return _orphan(anchor, f"{prefix}{len(inner)} occurrences inside {name}")

    body_offsets = _line_offsets(window.body)
    start, end = _span_for(window.body, body_offsets, inner[0], anchor.selected_text)
    shift = window.start_line - 1
    return _relocated(
        anchor,
        text=new_text,
        start=start + shift,
        end=end + shift,
        method="symbol_text",
        detail=f"{prefix}located inside {name}, which moved to line {window.start_line}",
    )


def _context_rung(
    anchor: AnnotationAnchor,
    *,
    new_text: str,
    lines: list[str],
    prefix: str,
) -> AnchorResolution:
    """Rung 5, falling to rung 6.

    The commented-on text is gone. The only honest thing left to locate is the
    seam it used to sit in, so the anchor collapses onto the join between the
    two context blocks and says so in ``anchor_detail``. ``diff-match-patch``
    proposes that seam; a tie is an orphan, and the exception that reports one
    is caught here rather than unwrapped for its best guess.
    """

    from lemoncrow.pro.capabilities.tool_supervision.fuzzy_match import (
        FuzzyAmbiguousMatchError,
        find_best_fuzzy_window,
    )

    # Blank edges are trimmed off both ends of the probe because the locator
    # anchors its window on the first *non-blank* line. Leaving them in would
    # make the reported window start later than the probe's own first line, and
    # the join point below -- which counts forward from that start -- would land
    # one line low for every blank line that was dropped.
    before_lines = anchor.before_context.splitlines()
    while before_lines and not before_lines[0].strip():
        before_lines.pop(0)
    after_lines = anchor.after_context.splitlines()
    while after_lines and not after_lines[-1].strip():
        after_lines.pop()

    probe = "\n".join(before_lines + after_lines)
    if not probe.strip():
        return _orphan(anchor, f"{prefix}selected text gone and no context was recorded")
    try:
        candidate = find_best_fuzzy_window(new_text, probe)
    except FuzzyAmbiguousMatchError:
        return _orphan(anchor, f"{prefix}surrounding context matches more than one place")
    except (ValueError, RecursionError):  # a locator failure is not a location
        return _orphan(anchor, f"{prefix}surrounding context could not be located")
    if candidate is None:
        return _orphan(anchor, f"{prefix}selected text gone and its context was not found")

    join = min(max(1, candidate.start_line + len(before_lines)), max(1, len(lines)))
    # The commented-on text is gone, so the definition the comment named is the
    # least trustworthy field on the anchor. Re-derive it from where the join
    # actually landed, or say nothing.
    name, fingerprint = _owning_symbol(new_text, anchor.path, join, join)
    # `blob_sha` is cleared, and `selected_text` is deliberately *not*
    # restamped. This rung does not re-capture the neighbourhood the way
    # :func:`_relocated` does, because there is nothing here to capture: the
    # text the reviewer picked is gone, and writing whatever line now occupies
    # the seam into `selected_text` would launder a heuristic guess into a
    # verbatim record.
    #
    # `blob_sha` is what rung 1 compares. Stamping the current blob onto a
    # record that still describes an older one made the *next* revision answer
    # `unchanged` / `identical_blob` / `anchor_exact` -- "file unchanged in this
    # revision" over a seam guess, with the honest explanation gone. Keeping the
    # *stored* sha is no better: on an undo back to that exact content (which
    # sources/local.py models explicitly) rung 1 fires again over the guess.
    # Empty means "this line number is a derivation, not a sighting", so rung 1
    # is skipped and the seam is re-derived -- or, after an undo, rung 2 finds
    # the reviewer's actual text again. The context blocks are left untouched,
    # and reaching this line proved at least one of them survives the trim, so
    # :func:`_has_a_locator` still answers yes on the next revision and the
    # never-anchored branch stays out of reach even when `selected_text` was
    # empty to begin with, as it is for a comment written on a blank line.
    moved = replace(
        anchor,
        start_line=join,
        end_line=join,
        symbol_qualified_name=name,
        symbol_fingerprint=fingerprint,
        blob_sha="",
    )
    return AnchorResolution(
        status="relocated",
        method="unique_context",
        anchor=moved,
        detail=f"{prefix}content removed; anchored to surrounding context",
    )


__all__ = [
    "ANCHOR_STATUSES",
    "AnchorResolution",
    "AnchorStatus",
    "blob_sha",
    "build_anchor",
    "context_hash",
    "resolve_anchor",
]
