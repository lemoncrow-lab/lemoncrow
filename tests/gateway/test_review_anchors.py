"""The relocation ladder: find the comment again, or say plainly that you cannot.

One test per rung, and then the three tests that are the actual product:

* two locations fit equally well -> **orphaned**, with the count named
  (:func:`test_two_identical_locations_orphan_rather_than_guess`);
* the fuzzy locator itself reports a tie -> **orphaned**, never its ``best``
  (:func:`test_a_fuzzy_tie_orphans_rather_than_taking_the_best_guess`);
* the new-side text could not be read -> **unresolved**, and explicitly *not*
  orphaned (:func:`test_unreadable_new_side_is_unresolved_not_orphaned`).

The last one is the distinction the whole module turns on. "This comment moved"
and "we did not read the file" are different facts, and a reviewer shown the
first when the truth is the second goes hunting for a change nobody made.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from lemoncrow.pro.capabilities.review import anchors
from lemoncrow.pro.capabilities.review.anchors import build_anchor, resolve_anchor
from lemoncrow.pro.capabilities.review.session_models import _ANCHOR_TEXT_CAP, ANCHOR_METHOD_LABELS
from lemoncrow.pro.capabilities.tool_supervision.fuzzy_match import (
    FuzzyAmbiguousMatchError,
    FuzzyCandidate,
)

_PATH = "src/m.py"

# 1 """Module."""   2        3        4 def alpha   5 total   6 logged
# 7 return logged   8        9        10 def beta   11 scaled   12 return
_ORIG = '''"""Module."""


def alpha(value):
    total = value + 1
    logged = total
    return logged


def beta(value):
    scaled = value * 2
    return scaled
'''

_SELECTED_LINE = 6


def _anchor(symbol: str = "alpha", text: str = _ORIG) -> anchors.AnnotationAnchor:
    """An anchor on ``    logged = total`` inside ``alpha``."""

    return build_anchor(
        path=_PATH,
        text=text,
        start_line=_SELECTED_LINE,
        end_line=_SELECTED_LINE,
        unit_key="sym:abc",
        symbol_qualified_name=symbol,
    )


# --------------------------------------------------------------------------- #
# rung 1 -- identical blob
# --------------------------------------------------------------------------- #


def test_rung_1_identical_blob_keeps_the_line_range_untouched() -> None:
    resolution = resolve_anchor(_anchor(), new_text=_ORIG)
    assert resolution.method == "identical_blob"
    assert resolution.status == "unchanged"
    assert (resolution.anchor.start_line, resolution.anchor.end_line) == (_SELECTED_LINE, _SELECTED_LINE)


# --------------------------------------------------------------------------- #
# rung 2 -- exact text with matching context
# --------------------------------------------------------------------------- #


def test_rung_2_relocates_when_the_text_and_both_context_blocks_still_match() -> None:
    shifted = "import os\nimport sys\n\n\n" + _ORIG
    resolution = resolve_anchor(_anchor(), new_text=shifted)
    assert resolution.method == "exact_text_context"
    assert resolution.status == "relocated"
    assert resolution.anchor.start_line == _SELECTED_LINE + 4
    assert shifted.splitlines()[resolution.anchor.start_line - 1] == "    logged = total"


# --------------------------------------------------------------------------- #
# rung 3 -- same symbol, text inside its body
# --------------------------------------------------------------------------- #


_TWO_COPIES = '''"""Module."""

import os


def alpha(value, scale=1):
    total = value + 1
    logged = total
    return logged * scale


def beta(value):
    total = value * 2
    logged = total
    return logged
'''


def test_rung_3_looks_inside_the_named_symbol_when_the_context_moved() -> None:
    """Both context blocks changed and the text now occurs twice; the symbol decides.

    Without rung 3 this is a two-candidate orphan. With it, the owning
    definition narrows the search to one body and the comment survives an agent
    rewriting the signature above it.
    """

    resolution = resolve_anchor(_anchor(), new_text=_TWO_COPIES)
    assert resolution.method == "symbol_text"
    assert resolution.status == "relocated"
    assert resolution.anchor.start_line == 8
    assert _TWO_COPIES.splitlines()[7] == "    logged = total"
    assert "alpha" in resolution.detail


def test_rung_3_orphans_when_two_definitions_carry_the_same_name() -> None:
    twins = _TWO_COPIES.replace("def beta(value):", "def alpha(value):")
    resolution = resolve_anchor(_anchor(), new_text=twins)
    assert resolution.status == "orphaned"
    assert "2 definitions named alpha" in resolution.detail


# --------------------------------------------------------------------------- #
# rung 4 -- unique occurrence in the file
# --------------------------------------------------------------------------- #


def test_rung_4_takes_a_single_occurrence_when_no_symbol_is_recorded() -> None:
    """No symbol name, changed context, one occurrence -- that is a location."""

    anchor = replace(_anchor(), symbol_qualified_name="")
    moved = _ORIG.replace("def alpha(value):", "def alpha(value, scale=1):").replace(
        '"""Module."""', '"""Module."""\n\nimport os'
    )
    resolution = resolve_anchor(anchor, new_text=moved)
    assert resolution.method == "unique_text"
    assert resolution.status == "relocated"
    assert moved.splitlines()[resolution.anchor.start_line - 1] == "    logged = total"


# --------------------------------------------------------------------------- #
# rung 5 -- the text is gone; anchor to the seam it left
# --------------------------------------------------------------------------- #


def test_rung_5_collapses_onto_the_context_join_when_the_text_was_deleted() -> None:
    deleted = _ORIG.replace("    logged = total\n", "")
    resolution = resolve_anchor(_anchor(), new_text=deleted)
    assert resolution.method == "unique_context"
    assert resolution.status == "relocated"
    assert resolution.anchor.start_line == resolution.anchor.end_line == _SELECTED_LINE
    assert "content removed" in resolution.detail


def test_rung_5_orphans_when_the_context_is_nowhere_to_be_found() -> None:
    resolution = resolve_anchor(_anchor(), new_text="totally = 'unrelated'\nprint(totally)\n")
    assert resolution.status == "orphaned"
    assert resolution.method == "orphaned"


def test_rung_5_does_not_stamp_this_revisions_blob_onto_a_seam_guess() -> None:
    """A seam guess must not be relabelled ``unchanged`` by the next revision.

    Rung 5 keeps the stored ``selected_text`` -- correctly, because writing
    whatever line now occupies the seam into it would launder a guess into a
    verbatim record. Stamping the *current* blob's sha on top of that record was
    the other half of the same decision, and it was wrong: ``blob_sha`` is
    exactly what rung 1 compares. One further revision that left this file alone
    therefore answered ``unchanged`` / ``identical_blob`` -- "file unchanged in
    this revision", ``anchor_exact`` true -- pointing at ``    return logged``,
    a line the reviewer never wrote a word about, with the honest "content
    removed" explanation gone.
    """

    deleted = _ORIG.replace("    logged = total\n", "")
    seam = resolve_anchor(_anchor(), new_text=deleted)
    assert seam.method == "unique_context"
    assert seam.anchor.selected_text == "    logged = total", "the stored text is kept, and it is gone from `deleted`"
    assert seam.anchor.blob_sha != anchors.blob_sha(deleted)

    # One more revision, recorded for an unrelated file: this file is untouched.
    again = resolve_anchor(seam.anchor, new_text=deleted)

    assert again.status == "relocated"
    assert again.method == "unique_context"
    assert "content removed" in again.detail
    assert again.anchor.start_line == seam.anchor.start_line


def test_rung_5_keeps_relocating_a_comment_that_selected_no_text() -> None:
    """A comment on a blank line is anchored by its context, and must not freeze.

    Rung 5 clears ``blob_sha`` so the next revision re-derives the seam rather
    than reading the guess as an untouched file. A blank-line comment has an
    empty ``selected_text`` from the moment it is written, so the revision
    *after* a rung-5 relocation used to find both fields empty and take the
    never-anchored branch: ``unresolved``, frozen at a stale line, and told
    "this comment was never anchored to text" while its two context blocks sat
    there intact and rung 5 would still have found the seam.
    """

    blank_line = 3
    anchor = build_anchor(path=_PATH, text=_ORIG, start_line=blank_line, end_line=blank_line)
    assert anchor.selected_text == "", "a blank line selects no text, which is not the same as no anchor"
    assert anchor.before_context and anchor.after_context

    edited = _ORIG.replace("    scaled = value * 2", "    scaled = value * 3")
    seam = resolve_anchor(anchor, new_text=edited)
    assert seam.status == "relocated"
    assert seam.method == "unique_context"
    assert seam.anchor.blob_sha == ""

    # One more revision, recorded for an unrelated file: this file is untouched.
    again = resolve_anchor(seam.anchor, new_text=edited)

    assert again.status == "relocated"
    assert again.method == "unique_context"
    assert again.anchor.start_line == seam.anchor.start_line


# --------------------------------------------------------------------------- #
# rung 6 -- the rule the other five rest on
# --------------------------------------------------------------------------- #


_TIE_BEFORE = """MARK = 1


def alpha(value):
    logged = total
    return logged
"""

_TIE_AFTER = """MARK = 1


def alpha(value):
    logged = total
    return logged


def alpha(value):
    logged = total
    return logged
"""


def test_two_identical_locations_orphan_rather_than_guess() -> None:
    """Case 7: identical text with identical context in two places.

    Both candidates clear rung 2 outright. Picking either one would be a coin
    flip presented to the reader as a fact, so the annotation is orphaned and
    the detail names how many places fit.
    """

    anchor = build_anchor(path=_PATH, text=_TIE_BEFORE, start_line=5, end_line=5)
    resolution = resolve_anchor(anchor, new_text=_TIE_AFTER)
    assert resolution.status == "orphaned"
    assert resolution.method == "orphaned"
    assert "2 candidate locations" in resolution.detail
    # The previous position is kept for display and claims nothing.
    assert resolution.anchor.start_line == 5


def test_a_fuzzy_tie_orphans_rather_than_taking_the_best_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 8: ``diff-match-patch`` proposes, LemonCrow disposes.

    ``find_best_fuzzy_window`` raising is the locator saying "two places fit".
    Catching it and reading ``best`` off the exception would turn a refusal
    into an answer, which is the single thing this module exists to prevent.
    """

    def _tie(_content: str, _needle: str) -> FuzzyCandidate:
        raise FuzzyAmbiguousMatchError(
            [
                FuzzyCandidate(1, 2, 0, 10, 0, 1.0),
                FuzzyCandidate(9, 10, 40, 50, 0, 1.0),
            ]
        )

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.tool_supervision.fuzzy_match.find_best_fuzzy_window",
        _tie,
    )
    deleted = _ORIG.replace("    logged = total\n", "")
    resolution = resolve_anchor(_anchor(), new_text=deleted)
    assert resolution.status == "orphaned"
    assert "more than one place" in resolution.detail


# --------------------------------------------------------------------------- #
# terminal states -- three different facts, three different words
# --------------------------------------------------------------------------- #


def test_unreadable_new_side_is_unresolved_not_orphaned() -> None:
    """Case 9: never orphan because we could not look.

    A binary or oversize file yields no text. That is a fact about our reading,
    not about the comment's footing, and conflating the two would send a
    reviewer hunting for a change nobody made.
    """

    resolution = resolve_anchor(_anchor(), new_text=None)
    assert resolution.status == "unresolved"
    assert resolution.method == "unresolved"
    assert resolution.status != "orphaned"
    assert "not orphaned" in resolution.detail


def test_a_deleted_file_makes_the_annotation_obsolete_not_orphaned() -> None:
    resolution = resolve_anchor(_anchor(), new_text=None, file_status="deleted")
    assert resolution.status == "removed"
    assert resolution.method == "removed"


def test_a_rename_rewrites_the_path_and_then_runs_the_ladder_normally() -> None:
    resolution = resolve_anchor(_anchor(), new_text=_ORIG, path="src/renamed.py")
    assert resolution.anchor.path == "src/renamed.py"
    assert resolution.method == "identical_blob"
    assert "renamed" in resolution.detail


# --------------------------------------------------------------------------- #
# the never-anchored guard -- what counts as a locator, and where it sits
# --------------------------------------------------------------------------- #

_BLANK_LINE = 3  # the empty line between the module docstring and `def alpha`
_ONE_BLANK_LINE_FILE = "\n"  # an `__init__.py` holding nothing but a newline


def _blank_line_anchor() -> anchors.AnnotationAnchor:
    """A comment on a blank line: no selected text, two real context blocks."""

    return build_anchor(path=_PATH, text=_ORIG, start_line=_BLANK_LINE, end_line=_BLANK_LINE)


def _gate(resolution: anchors.AnchorResolution) -> str:
    """Which door at the top of the ladder this anchor went through.

    The locator-less door answers one status, ``unresolved``, in two different
    sentences, and which sentence a reader gets is as much a fact about the
    anchor as the status is -- so the gate names them apart.
    """

    if resolution.method == "identical_blob":
        return "rung 1"
    if resolution.status == "unresolved":
        return "changed, nothing recorded" if "the file changed" in resolution.detail else "never anchored"
    return "ladder"  # rungs 2-6 -- we looked, and either found it or said we did not


def test_a_locator_less_comment_on_an_untouched_file_still_reaches_rung_1() -> None:
    """A real blob sha is a sighting, and the guard must not stand in front of it.

    Line 1 of a file whose entire content is one newline selects no text and has
    no neighbours either side, so all three locators come out empty -- while
    ``blob_sha`` is perfectly genuine. Asking "is there a locator?" *before* rung
    1 answered "this comment was never anchored to text" about a file the blob
    proves is byte-identical, which is the one thing ``unresolved`` must never
    mean: we could look, and we did not.
    """

    anchor = build_anchor(path=_PATH, text=_ONE_BLANK_LINE_FILE, start_line=1, end_line=1)
    assert (anchor.selected_text, anchor.before_context, anchor.after_context) == ("", "", "")
    assert anchor.blob_sha, "the file was read; the blob is a fact about it"

    resolution = resolve_anchor(anchor, new_text=_ONE_BLANK_LINE_FILE)

    assert (resolution.status, resolution.method) == ("unchanged", "identical_blob")
    assert "unchanged" in resolution.detail


def test_a_locator_less_comment_on_a_changed_file_is_unresolved_not_orphaned() -> None:
    """Nothing to look for is not the same as looked and lost.

    Same anchor as above, but the file changed, so rung 1 declines. There is
    still nothing to search with, and rung 5 used to be handed the empty probe
    and answer ``orphaned`` -- "selected text gone and no context was recorded",
    which says in one breath that it orphaned the comment for missing the very
    thing it admits was never there. Orphaning is not cosmetic: it writes
    ``state='orphaned'`` onto the annotation and moves a live comment into the
    bucket the reviewer is told to ignore.

    ``unresolved`` is the status, and the sentence under it is *not* "never
    anchored": this anchor carries a real blob, so the file was read when the
    comment was written and its bytes have changed since. What the comment
    never had is a locator, and that is what the reader is told.
    """

    anchor = build_anchor(path=_PATH, text=_ONE_BLANK_LINE_FILE, start_line=1, end_line=1)
    assert anchor.blob_sha, "the file was read; the blob is a fact about it"

    resolution = resolve_anchor(anchor, new_text="x = 1\n")

    assert (resolution.status, resolution.method) == ("unresolved", "unresolved")
    assert resolution.detail == "the file changed; this comment recorded no text or context to relocate it by"
    assert "never anchored" not in resolution.detail


def test_only_a_comment_that_never_had_a_blob_is_told_it_was_never_anchored() -> None:
    """The other sentence behind the same status, and the only one that may say "never".

    No ``blob_sha`` and no locator means nothing about this file was ever
    recorded: it was binary or unreadable when the comment was written, or the
    comment sits on the old side of the diff, where there is no new-side text
    to follow. "Never anchored" is literally true here and nowhere else, which
    is why the two cases do not share one sentence.
    """

    anchor = replace(
        build_anchor(path=_PATH, text=_ONE_BLANK_LINE_FILE, start_line=1, end_line=1),
        blob_sha="",
    )

    resolution = resolve_anchor(anchor, new_text="x = 1\n")

    assert (resolution.status, resolution.method) == ("unresolved", "unresolved")
    assert resolution.detail == "this comment was never anchored to text or context; nothing to relocate it by"


def test_the_unresolved_label_is_true_of_both_roads_that_reach_it() -> None:
    """The four surfaces render the label, not the detail, so it has to hold alone.

    ``ANCHOR_METHOD_LABELS['unresolved']`` is the whole sentence a reviewer gets
    in the annotation list, the CLI panes and the finish sheet. It read "not
    looked at", which was true while ``unresolved`` meant only "the new side
    could not be read". It also covers an anchor with no locator now -- and that
    anchor's file may very well have been read, and changed. Telling that
    reviewer their file was not looked at is a plain falsehood about the one
    thing the label is there to report, so the label has to be true of both
    roads or it is worse than none.
    """

    unreadable = resolve_anchor(_anchor(), new_text=None)
    no_locator = resolve_anchor(
        build_anchor(path=_PATH, text=_ONE_BLANK_LINE_FILE, start_line=1, end_line=1),
        new_text="x = 1\n",
    )
    assert unreadable.method == no_locator.method == "unresolved"
    assert "the file changed" in no_locator.detail, "this road runs over a file that was read"

    label = ANCHOR_METHOD_LABELS["unresolved"]

    assert label == "could not be checked"
    assert "not looked at" not in label


def test_a_context_of_nothing_but_blank_lines_is_not_a_locator() -> None:
    """``"\\n\\n"`` is a truthy string and no evidence at all.

    Rung 5 trims blank edges off both context blocks before probing, so a
    context made only of blank lines collapses to nothing the moment it is used.
    A guard that tested the raw strings let such an anchor through and rung 6
    orphaned it for failing to find a locator it never had -- and this is not a
    hypothetical shape: it is exactly what a comment on a blank line inside a
    run of blank lines carries, with ``blob_sha`` cleared by a previous rung 5.
    """

    anchor = anchors.AnnotationAnchor(
        path=_PATH,
        side="new",
        start_line=5,
        end_line=5,
        selected_text="",
        before_context="\n\n",
        after_context="\n\n",
        blob_sha="",
    )

    resolution = resolve_anchor(anchor, new_text=_ORIG)

    assert (resolution.status, resolution.method) == ("unresolved", "unresolved")
    assert "never anchored" in resolution.detail


@pytest.mark.parametrize(
    ("blob", "selected", "context", "identical", "changed"),
    [
        # blob        selected  context     blob matches       blob does not
        ("none", "none", "none", "never anchored", "never anchored"),
        ("none", "none", "blank", "never anchored", "never anchored"),
        ("none", "none", "real", "ladder", "ladder"),
        ("none", "real", "none", "ladder", "ladder"),
        ("none", "real", "blank", "ladder", "ladder"),
        ("none", "real", "real", "ladder", "ladder"),
        ("origin", "none", "none", "rung 1", "changed, nothing recorded"),
        ("origin", "none", "blank", "rung 1", "changed, nothing recorded"),
        ("origin", "none", "real", "rung 1", "ladder"),
        ("origin", "real", "none", "rung 1", "ladder"),
        ("origin", "real", "blank", "rung 1", "ladder"),
        ("origin", "real", "real", "rung 1", "ladder"),
        ("stale", "none", "none", "changed, nothing recorded", "changed, nothing recorded"),
        ("stale", "none", "blank", "changed, nothing recorded", "changed, nothing recorded"),
        ("stale", "none", "real", "ladder", "ladder"),
        ("stale", "real", "none", "ladder", "ladder"),
        ("stale", "real", "blank", "ladder", "ladder"),
        ("stale", "real", "real", "ladder", "ladder"),
    ],
)
def test_which_door_each_anchor_shape_goes_through(
    blob: str, selected: str, context: str, identical: str, changed: str
) -> None:
    """The whole blast radius of the guard, pinned one row at a time.

    Three fields decide which door at the top of the ladder an anchor takes, and
    the table is small enough to write out in full, so it is written out in
    full. The two last columns are the same anchor against a file whose bytes
    match its recorded blob and one whose bytes do not -- which, for the two
    rows carrying a ``stale`` blob, is both times. Three rules it encodes, the
    first two of which were broken at some point by keying the guard on empty
    strings alone:

    * a matching ``blob_sha`` wins over everything -- rung 1 is a sighting, and
      it is asked first even when nothing else on the anchor is usable;
    * "usable" means what rung 5 can probe with, so a blank-only context counts
      as no context, exactly as an empty one does;
    * an anchor with no usable locator is ``unresolved`` whether or not the file
      changed under it. ``orphaned`` claims a search ran and lost, and no search
      can run without a query -- so the file changing moves the *sentence*, not
      the status, from "never anchored" to "the file changed; this comment
      recorded no text or context". Both are true of their own row; neither is
      true of the other's, which is why one string could not cover both.

    ``ladder`` is deliberately coarse: rungs 2-6 all mean "we looked", and which
    of them answers is the business of the rung tests above, not of this gate.
    """

    edited = _ORIG.replace("    scaled = value * 2", "    scaled = value * 3")
    shas = {"none": "", "origin": anchors.blob_sha(_ORIG), "stale": anchors.blob_sha("unrelated\n")}
    texts = {"none": "", "real": "    logged = total"}
    contexts = {
        "none": ("", ""),
        "blank": ("\n\n", "\n\n"),
        "real": (_blank_line_anchor().before_context, _blank_line_anchor().after_context),
    }
    before, after = contexts[context]
    anchor = replace(
        _blank_line_anchor(),
        blob_sha=shas[blob],
        selected_text=texts[selected],
        before_context=before,
        after_context=after,
    )

    assert _gate(resolve_anchor(anchor, new_text=_ORIG)) == identical
    assert _gate(resolve_anchor(anchor, new_text=edited)) == changed


# --------------------------------------------------------------------------- #
# anchor capture
# --------------------------------------------------------------------------- #


def test_build_anchor_captures_the_text_and_both_context_blocks_verbatim() -> None:
    anchor = _anchor()
    assert anchor.selected_text == "    logged = total"
    assert anchor.before_context.splitlines()[-1] == "    total = value + 1"
    assert anchor.after_context.splitlines()[0] == "    return logged"
    assert anchor.blob_sha == anchors.blob_sha(_ORIG)


def test_a_relocation_refreshes_the_neighbourhood_so_the_next_revision_can_use_it() -> None:
    """The ladder must not compare revision 5 against revision 1's neighbourhood.

    Refreshing loses no history: every attempt, with its anchor, is appended to
    ``annotation_anchor_events`` by the caller.
    """

    shifted = "import os\n\n\n" + _ORIG
    resolution = resolve_anchor(_anchor(), new_text=shifted)
    assert resolution.anchor.blob_sha == anchors.blob_sha(shifted)
    assert resolution.anchor.unit_key == "sym:abc"
    assert resolution.anchor.selected_text == "    logged = total"


# --------------------------------------------------------------------------- #
# anchor honesty: which rung ran, not whether the line moved
# --------------------------------------------------------------------------- #


# The judge's case, exactly: the function is renamed and its docstring rewritten,
# so *both* context blocks change and rung 2 finds nothing. The commented line is
# untouched and unique, so rung 4 re-finds it -- at the same line number it
# started on. Keying the status on line movement reported that as `unchanged`,
# which is the word for "the file is byte-identical" and is a lie here.
_RENAMED_AND_REDOCUMENTED = '''"""Module."""


def renamed_alpha(value):
    """A completely different sentence about what this does."""
    logged = value + 1
    return logged


def beta(value):
    scaled = value * 2
    return scaled
'''


def test_a_rung_4_refind_that_lands_on_the_same_line_is_still_relocated() -> None:
    """Only rung 1 may say ``unchanged``. Every other rung is a re-find.

    The reviewer's comment now sits inside a function with a different name and
    a different docstring. Reported as ``unchanged`` it is drawn with the same
    weight as a comment whose blob never moved -- plan SS4.4's "never silently
    move a comment to a different line after a revision", where the silence is
    the part that matters and not the line number.
    """

    anchor = build_anchor(
        path=_PATH,
        text=_ORIG,
        start_line=5,
        end_line=5,
        unit_key="sym:abc",
        symbol_qualified_name="alpha",
    )
    assert anchor.selected_text == "    total = value + 1"

    moved = build_anchor(
        path=_PATH,
        text=_RENAMED_AND_REDOCUMENTED,
        start_line=6,
        end_line=6,
    )
    assert moved.selected_text == "    logged = value + 1"

    # Anchor the *unchanged* line so the re-find lands where it started.
    original = build_anchor(
        path=_PATH,
        text=_ORIG,
        start_line=6,
        end_line=6,
        unit_key="sym:abc",
        symbol_qualified_name="alpha",
    )
    assert original.selected_text == "    logged = total"

    text = _RENAMED_AND_REDOCUMENTED.replace("    logged = value + 1", "    logged = total")
    resolution = resolve_anchor(original, new_text=text)

    assert resolution.method == "unique_text"
    assert resolution.status == "relocated"
    assert (resolution.anchor.start_line, resolution.anchor.end_line) == (6, 6)
    assert resolution.located is True


def test_only_rung_1_reports_unchanged() -> None:
    """The invariant, stated once so a future rung cannot quietly opt in."""

    unchanged = resolve_anchor(_anchor(), new_text=_ORIG)
    assert (unchanged.method, unchanged.status) == ("identical_blob", "unchanged")

    for text in (
        "import os\nimport sys\n\n\n" + _ORIG,  # rung 2
        _TWO_COPIES,  # rung 3
        _RENAMED_AND_REDOCUMENTED.replace("    logged = value + 1", "    logged = total"),  # rung 4
    ):
        resolution = resolve_anchor(_anchor(), new_text=text)
        assert resolution.located, resolution.detail
        assert resolution.status == "relocated", f"{resolution.method} claimed {resolution.status}"


def test_a_relocation_re_derives_the_owning_definition_instead_of_carrying_the_old_name() -> None:
    """``in compute_total`` under a comment that now sits in ``total_price`` is a fabrication.

    The stored name describes the revision the comment was *written* on. Carried
    forward unexamined it becomes a confident claim about the current revision
    that nothing checked, and the reader has no way to catch it.
    """

    text = _RENAMED_AND_REDOCUMENTED.replace("    logged = value + 1", "    logged = total")
    resolution = resolve_anchor(_anchor(), new_text=text)

    assert resolution.method == "unique_text"
    assert resolution.anchor.symbol_qualified_name == "renamed_alpha"
    assert resolution.anchor.symbol_fingerprint != _anchor().symbol_fingerprint


def test_a_relocation_out_of_every_definition_names_no_symbol_rather_than_the_old_one() -> None:
    """No definition contains the new position, so the honest answer is nothing."""

    text = '"""Module."""\n\nif True:\n    logged = total\n'
    resolution = resolve_anchor(_anchor(), new_text=text)

    assert resolution.status == "relocated"
    assert resolution.anchor.symbol_qualified_name == ""
    assert resolution.anchor.symbol_fingerprint == ""


def test_rung_3_finds_the_right_twin_when_the_anchor_carries_a_qualified_name() -> None:
    """Two ``run`` methods, one qualified anchor: rung 3 must not orphan.

    The leaf-name fallback matches both windows and correctly refuses to choose.
    The containment name matches exactly one, which is the whole reason units are
    keyed on it.
    """

    text = (
        "class Reader:\n"
        "    def run(self):\n"
        "        payload = self.load()\n"
        "        return payload\n"
        "\n"
        "\n"
        "class Writer:\n"
        "    def run(self):\n"
        "        payload = self.dump()\n"
        "        return payload\n"
    )
    anchor = build_anchor(
        path=_PATH,
        text=text,
        start_line=4,
        end_line=4,
        symbol_qualified_name="Writer.run",
    )
    assert anchor.selected_text == "        return payload"

    # Push both classes down and change the neighbourhood so rung 2 cannot fire.
    moved = "import os\nimport sys\n\n\n" + text.replace("payload = self.load()", "payload = self.load(1)")
    resolution = resolve_anchor(anchor, new_text=moved)

    assert resolution.method == "symbol_text"
    assert resolution.status == "relocated"
    assert resolution.anchor.start_line == 14
    assert moved.splitlines()[13] == "        return payload"


# --------------------------------------------------------------------------- #
# a candidate has to be a real one: a line is not "found" inside a longer line
# --------------------------------------------------------------------------- #


# The judge's control pair. Both files hold a comment on the body line of
# ``newly``; in both the agent renames only the enclosing def, so the commented
# line is byte-identical and at the same number. They differ in one thing: in
# ``_PREFIXED`` another line -- ``    return x + 0``, in an unrelated function --
# has the commented line as a prefix. Nothing about the comment is ambiguous in
# either file, so both must survive.
_PREFIXED = """def alpha_0(x):
    return x + 0


def newly(x):
    return x
"""

_UNPREFIXED = """def alpha_0(q):
    return q + 0


def newly(x):
    return x
"""

_NEWLY_LINE = 6


@pytest.mark.parametrize("original", [_PREFIXED, _UNPREFIXED], ids=["prefixed", "unprefixed"])
def test_rung_4_counts_lines_not_substrings_when_the_selection_is_a_line(original: str) -> None:
    """A line is not a second home just because a longer line starts with it.

    The two parameters are the same scenario twice over: only the enclosing
    ``def`` is renamed, so rung 2's context is gone and rung 3's name is gone,
    and rung 4 re-finds the untouched line by its text. ``_PREFIXED`` used to
    orphan it -- ``2 candidate locations for the selected text`` -- because a
    raw substring scan also hit inside ``    return x + 0`` nine lines away.
    The reviewer's comment was thrown away over a line nobody had touched, and
    the printed count was of substrings, not of places a reader could mean.
    """

    anchor = build_anchor(
        path=_PATH,
        text=original,
        start_line=_NEWLY_LINE,
        end_line=_NEWLY_LINE,
        symbol_qualified_name="newly",
    )
    assert anchor.selected_text == "    return x"

    renamed = original.replace("def newly(x):", "def renamed_newly(x):")
    resolution = resolve_anchor(anchor, new_text=renamed)

    assert resolution.status == "relocated", resolution.detail
    assert resolution.method == "unique_text"
    assert (resolution.anchor.start_line, resolution.anchor.end_line) == (_NEWLY_LINE, _NEWLY_LINE)
    assert renamed.splitlines()[_NEWLY_LINE - 1] == "    return x"
    assert resolution.anchor.symbol_qualified_name == "renamed_newly"


def test_rung_4_still_orphans_when_two_lines_are_genuinely_identical() -> None:
    """The other half of the pair: narrowing the count must not soften the rule.

    Two whole lines that really are the same text really are two locations, and
    rung 6 still refuses to choose. If the fix above had bought its rescue by
    dropping duplicates, this is where it would show.
    """

    original = "def alpha(value):\n    total = value\n    return total\n"
    anchor = replace(
        build_anchor(path=_PATH, text=original, start_line=3, end_line=3),
        symbol_qualified_name="",
    )
    assert anchor.selected_text == "    return total"

    twins = (
        "import os\n"
        "\n"
        "\n"
        "def alpha(value):\n"
        "    total = value + 1\n"
        "    return total\n"
        "\n"
        "\n"
        "def beta(value):\n"
        "    total = value * 2\n"
        "    return total\n"
    )
    resolution = resolve_anchor(anchor, new_text=twins)

    assert resolution.status == "orphaned"
    assert resolution.method == "orphaned"
    assert "2 candidate locations for the selected text" in resolution.detail


def test_rung_2_does_not_count_a_longer_line_as_a_rival_with_identical_context() -> None:
    """Rung 2 read the same substrings, so it orphaned on the same phantom.

    The appended block gives the prefix hit the anchor's exact neighbourhood, so
    the two "candidates" cleared rung 2 together and the comment was orphaned as
    ``2 candidate locations with identical context``. Only one of the two is the
    selected line; the other merely begins with it.
    """

    block = "alpha = 1\nbeta = 2\ngamma = 3\nvalue = x\ndelta = 4\nepsilon = 5\nzeta = 6\n"
    anchor = build_anchor(path=_PATH, text=block, start_line=4, end_line=4)
    assert anchor.selected_text == "value = x"

    with_prefix_twin = block + block.replace("value = x", "value = x + 0")
    resolution = resolve_anchor(anchor, new_text=with_prefix_twin)

    assert resolution.status == "relocated", resolution.detail
    assert resolution.method == "exact_text_context"
    assert (resolution.anchor.start_line, resolution.anchor.end_line) == (4, 4)


def test_rung_3_does_not_count_a_longer_line_as_a_rival_inside_the_symbol() -> None:
    """Rung 3 searches a definition body, and counted substrings there too.

    Both lines live in the same function, so this one never reached rung 4: it
    orphaned as ``2 occurrences inside alpha`` while the file held exactly one
    line equal to the selection.
    """

    original = 'def alpha(x):\n    """Original sentence."""\n    if x:\n        return x + 0\n    return x\n'
    anchor = build_anchor(
        path=_PATH,
        text=original,
        start_line=5,
        end_line=5,
        symbol_qualified_name="alpha",
    )
    assert anchor.selected_text == "    return x"

    redocumented = original.replace('"""Original sentence."""', '"""A different sentence."""')
    resolution = resolve_anchor(anchor, new_text=redocumented)

    assert resolution.status == "relocated", resolution.detail
    assert resolution.method == "symbol_text"
    assert (resolution.anchor.start_line, resolution.anchor.end_line) == (5, 5)


def _fragment_anchor(text: str, line: int, fragment: str) -> anchors.AnnotationAnchor:
    """An anchor on part of one line -- what a character selection stores."""

    return replace(
        build_anchor(path=_PATH, text=text, start_line=line, end_line=line),
        selected_text=fragment,
        selected_text_hash=anchors.context_hash(fragment),
        symbol_qualified_name="",
    )


def test_an_intra_line_selection_keeps_substring_semantics() -> None:
    """No line equals the selection, so substrings are the only reading there is.

    Line counting is what a whole-line selection means; it is not a new rule
    imposed on selections that were never lines. A fragment picked out of the
    middle of a line must still be findable.
    """

    anchor = _fragment_anchor(_ORIG, 5, "value + 1")
    widened = _ORIG.replace("def alpha(value):", "def alpha(value, scale=1):")
    resolution = resolve_anchor(anchor, new_text=widened)

    assert resolution.status == "relocated", resolution.detail
    assert resolution.method == "unique_text"
    assert widened.splitlines()[resolution.anchor.start_line - 1] == "    total = value + 1"


def test_an_ambiguous_intra_line_selection_still_orphans() -> None:
    """And the refusal survives there too: two lines contain it, so neither wins."""

    anchor = _fragment_anchor(_ORIG, 5, "value + 1")
    twice = _ORIG.replace("    scaled = value * 2", "    scaled = value + 1").replace(
        "def alpha(value):", "def alpha(value, scale=1):"
    )
    resolution = resolve_anchor(anchor, new_text=twice)

    assert resolution.status == "orphaned"
    assert "2 candidate locations for the selected text" in resolution.detail


# --------------------------------------------------------------------------- #
# ... and the other half of the same rule: a line that is gone is not "found"
# inside a longer line either
# --------------------------------------------------------------------------- #


_DELETED_BETA_ORIGINAL = """def alpha(value):
    return value * 2


def beta(x):
    return x


def gamma(x):
    return x + 0
"""

_BETA_BODY_LINE = 6

_BETA_DELETED = """def alpha(value):
    return value * 2


def gamma(x):
    return x + 0
"""


def test_a_vanished_line_is_not_re_found_inside_a_longer_line() -> None:
    """The ship blocker: a confident line number in a function nobody commented on.

    The reviewer commented on ``    return x`` in ``beta``. The agent deleted
    ``beta`` outright. The only remaining occurrence of those characters is the
    *beginning of a different line*, ``    return x + 0``, in an unrelated
    ``gamma`` -- and rung 4 took it, printing ``selected text occurs exactly
    once`` and ``in gamma`` with no hedge at all. Wrong line, wrong function,
    full confidence, and the reader had nothing to tell it apart from a comment
    that really had followed its code.

    ``_occupies_whole_lines`` already knew ``    return x + 0`` is a different
    line; ``_candidate_offsets`` then fell back to raw substrings and used it
    anyway. The stored selection is a line range -- the anchor record says so,
    because :func:`build_anchor` slices whole lines and only the text cap can
    cut one -- so a line that is gone belongs to rung 5 or rung 6, which are the
    two rungs that say so out loud.
    """

    anchor = build_anchor(
        path=_PATH,
        text=_DELETED_BETA_ORIGINAL,
        start_line=_BETA_BODY_LINE,
        end_line=_BETA_BODY_LINE,
        symbol_qualified_name="beta",
    )
    assert anchor.selected_text == "    return x"
    assert _BETA_DELETED.splitlines()[5] == "    return x + 0"

    resolution = resolve_anchor(anchor, new_text=_BETA_DELETED)

    # Never rung 4, and never any rung that claims to have found the text.
    assert resolution.method not in ("unique_text", "exact_text_context", "symbol_text")
    assert resolution.status == "orphaned"
    assert resolution.method == "orphaned"
    assert "selected text gone" in resolution.detail
    # The kept line range is for display only, and the stored name is rendered
    # as "originally in" by anchor_symbol_claim -- neither claims gamma.
    assert resolution.anchor.symbol_qualified_name != "gamma"


def test_a_vanished_line_is_not_re_found_under_extra_indentation() -> None:
    """Same fabrication, mirrored: the twin is the stored line plus indentation.

    ``    return x`` occurs inside ``        return x`` too -- starting at
    column five rather than column one. A hit whose line holds nothing but
    whitespace before it is the stored line re-indented, which is a different
    line, so it is no more a home than a longer line that starts with it.
    Re-indenting a block is the commonest edit an agent makes.
    """

    nested = """def alpha(value):
    return value * 2


def gamma(x):
    if x:
        return x
"""
    anchor = build_anchor(
        path=_PATH,
        text=_DELETED_BETA_ORIGINAL,
        start_line=_BETA_BODY_LINE,
        end_line=_BETA_BODY_LINE,
        symbol_qualified_name="beta",
    )
    assert anchor.selected_text == "    return x"
    assert nested.splitlines()[6] == "        return x"

    resolution = resolve_anchor(anchor, new_text=nested)

    assert resolution.method not in ("unique_text", "exact_text_context", "symbol_text")
    assert resolution.status == "orphaned"
    assert resolution.anchor.symbol_qualified_name != "gamma"


def test_a_cap_truncated_selection_still_anchors_by_its_surviving_head() -> None:
    """The one selection the cap really does leave ending mid-line still works.

    ``_ANCHOR_TEXT_CAP`` trims the tail of a pathological selection, so its
    stored text genuinely stops in the middle of a line and no whole-line copy
    of it can ever exist. That is the case the substring fallback was written
    for, and it is the case the record can name -- a stored text that long is a
    stored text that may have been cut. Its head is still a line start, so the
    hit that starts a line is a candidate here, where for an intact line it
    would be a phantom.
    """

    huge = "    payload = " + "x" * (_ANCHOR_TEXT_CAP + 500)
    original = f"def alpha(value):\n{huge}\n    return payload\n"
    anchor = build_anchor(path=_PATH, text=original, start_line=2, end_line=2)
    assert len(anchor.selected_text) == _ANCHOR_TEXT_CAP
    assert huge.startswith(anchor.selected_text) and anchor.selected_text != huge

    # Change the neighbourhood so rung 2 cannot fire and rung 4 has to decide.
    widened = original.replace("def alpha(value):", "def alpha(value, scale=1):")
    resolution = resolve_anchor(anchor, new_text=widened)

    assert resolution.status == "relocated", resolution.detail
    assert resolution.method == "unique_text"
    assert (resolution.anchor.start_line, resolution.anchor.end_line) == (2, 2)


# --------------------------------------------------------------------------- #
# rung 1 says as much about where the comment sits as every other rung
# --------------------------------------------------------------------------- #


def test_rung_1_names_the_definition_the_comment_sits_in() -> None:
    """Two correctly-placed comments must not carry different amounts of fact.

    A comment created on a line inside a definition this revision did not change
    has no symbol unit to be owned by, so it is stored with no name. Every other
    rung re-derives one on the way past; rung 1 did not, so the terminal printed
    ``anchor: file unchanged since the comment`` bare while the comment three
    lines above it read ``... · in alpha``. The blob is byte-identical here, so
    the owning definition is a lookup against the very text the range points
    into -- not a guess, and not something the reader should have to supply.
    """

    anchor = _anchor(symbol="")
    assert anchor.symbol_qualified_name == ""

    resolution = resolve_anchor(anchor, new_text=_ORIG)

    assert (resolution.method, resolution.status) == ("identical_blob", "unchanged")
    assert (resolution.anchor.start_line, resolution.anchor.end_line) == (_SELECTED_LINE, _SELECTED_LINE)
    assert resolution.anchor.symbol_qualified_name == "alpha"
    assert resolution.anchor.symbol_fingerprint != ""


def test_rung_1_and_a_re_find_name_the_same_definition_for_the_same_line() -> None:
    """The point of the previous test, stated as the parity it is meant to buy."""

    unchanged = resolve_anchor(_anchor(symbol=""), new_text=_ORIG)
    shifted = "import os\nimport sys\n\n\n" + _ORIG
    refound = resolve_anchor(_anchor(symbol=""), new_text=shifted)

    assert unchanged.method == "identical_blob"
    assert refound.method == "exact_text_context"
    assert unchanged.anchor.symbol_qualified_name == refound.anchor.symbol_qualified_name == "alpha"


def test_rung_1_keeps_the_stored_name_when_no_definition_owns_the_line() -> None:
    """An identical blob cannot have made the stored name stale, so keep it.

    Module level, an unparseable file, a language with no span support: the
    lookup has nothing to say. Blanking the field there would trade a fact the
    caller recorded for a silence, which is the opposite of the trade rung 1 is
    making.
    """

    text = "hello\nworld\n"
    anchor = build_anchor(path="notes.txt", text=text, start_line=1, end_line=1, symbol_qualified_name="recorded")
    resolution = resolve_anchor(anchor, new_text=text)

    assert resolution.method == "identical_blob"
    assert resolution.anchor.symbol_qualified_name == "recorded"
