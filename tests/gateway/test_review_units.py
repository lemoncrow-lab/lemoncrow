"""Review units: identity that survives a move, fingerprints that do not survive a rewrite.

These two properties are the product, so they are asserted as properties rather
than as snapshots. The load-bearing one is
:func:`test_inserting_ten_lines_above_a_function_changes_neither_key_nor_fingerprint`:
agents rewrite files constantly, and a review frontier whose keys noticed a line
number would evaporate every mark in a file for an edit made to none of them.

One test here pins a *limitation* rather than a feature
(:func:`test_a_decorator_only_edit_changes_the_file_fingerprint_but_not_the_symbol`).
The symbol window starts at the ``def`` keyword, so a decorator edit is charged
to the file and not to the definition below it. That is deliberate -- the
prelude is what separates two neighbouring definitions -- and it is asserted so
the day someone changes it, they change it on purpose.
"""

from __future__ import annotations

from lemoncrow.pro.capabilities.review.impact import SymbolWindow, symbol_windows
from lemoncrow.pro.capabilities.review.models import (
    ChangedFile,
    ChangedSymbol,
    DiffHunk,
    ReviewOrderEntry,
    ReviewPacket,
    empty_stats,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewUnit
from lemoncrow.pro.capabilities.review.units import (
    derive_units,
    file_fingerprint,
    normalize_body,
    symbol_fingerprint,
    tree_fingerprint,
    unit_key,
)

_MODULE = '''import functools


@functools.lru_cache
def alpha(value):
    """Doc."""
    inner = value + 1

    def helper(x):
        return x * 2

    return helper(inner)


class Beta:
    def gamma(self):
        return 3
'''

_TYPESCRIPT = """export function alpha(a: number): number {
  return a + 1;
}

export class Beta {
  gamma(): string {
    return "x";
  }
}
"""


def _windows(text: str, path: str = "pkg/mod.py") -> dict[str, SymbolWindow]:
    return {window.name: window for window in symbol_windows(text, path)}


def _packet(
    *,
    files: tuple[ChangedFile, ...] = (),
    symbols: tuple[ChangedSymbol, ...] = (),
    order: tuple[ReviewOrderEntry, ...] = (),
) -> ReviewPacket:
    """A minimal packet: only the four slices ``derive_units`` actually reads."""

    return ReviewPacket(
        schema_version=2,
        generated_at="2026-09-08T00:00:00+00:00",
        repo_root="/repo",
        range_mode="working_tree",
        base_rev="HEAD",
        head_rev="WORKDIR",
        base_sha="0" * 40,
        head_sha="",
        files=files,
        symbols=symbols,
        order=order,
        stats=empty_stats(),
    )


def _hunk(new_start: int, new_lines: int, *, ranges: tuple[tuple[int, int], ...], patch: str = "") -> DiffHunk:
    return DiffHunk(
        old_start=new_start,
        old_lines=new_lines,
        new_start=new_start,
        new_lines=new_lines,
        header=f"@@ -{new_start},{new_lines} +{new_start},{new_lines} @@",
        added=new_lines,
        removed=0,
        new_ranges=ranges,
        patch=patch,
    )


# --------------------------------------------------------------------------- #
# symbol_windows -- the public seam
# --------------------------------------------------------------------------- #


def test_symbol_windows_returns_every_definition_including_nested_ones() -> None:
    windows = symbol_windows(_MODULE, "pkg/mod.py")
    assert [window.name for window in windows] == ["alpha", "helper", "Beta", "gamma"]


def test_symbol_window_body_is_exactly_the_window_lines() -> None:
    """``body`` must be ``_symbol_text``'s output, not a re-derived slice."""

    lines = _MODULE.splitlines()
    for window in symbol_windows(_MODULE, "pkg/mod.py"):
        expected = "\n".join(lines[window.start_line - 1 : window.end_line]).strip()
        assert window.body == expected


def test_the_window_starts_at_the_def_line_so_the_decorator_is_outside_it() -> None:
    window = _windows(_MODULE)["alpha"]
    assert _MODULE.splitlines()[window.start_line - 1].startswith("def alpha")
    assert "lru_cache" not in window.body


def test_an_unparseable_python_file_yields_no_windows_and_does_not_raise() -> None:
    assert symbol_windows("def broken(:\n    pass\n", "pkg/bad.py") == ()


def test_an_empty_file_yields_no_windows() -> None:
    assert symbol_windows("", "pkg/empty.py") == ()


def test_a_typescript_file_falls_back_to_indentation_and_still_yields_windows() -> None:
    """Non-Python has no ``ast.end_lineno``; the indentation fallback still works."""

    windows = symbol_windows(_TYPESCRIPT, "src/app.ts")
    names = [window.name for window in windows]
    assert names == ["alpha", "Beta", "gamma"]
    alpha = windows[0]
    assert alpha.start_line == 1
    assert alpha.end_line >= 3
    assert "return a + 1;" in alpha.body


# --------------------------------------------------------------------------- #
# fingerprints
# --------------------------------------------------------------------------- #


def test_a_decorator_only_edit_changes_the_file_fingerprint_but_not_the_symbol() -> None:
    """The documented limitation of §4.2, asserted so it cannot regress silently."""

    after = _MODULE.replace("@functools.lru_cache\n", "@functools.lru_cache(maxsize=8)\n")
    assert after != _MODULE

    before_window = _windows(_MODULE)["alpha"]
    after_window = _windows(after)["alpha"]
    assert symbol_fingerprint(
        before_window.kind,
        "alpha",
        before_window.body,
    ) == symbol_fingerprint(
        after_window.kind,
        "alpha",
        after_window.body,
    )
    assert file_fingerprint("pkg/mod.py", _MODULE) != file_fingerprint("pkg/mod.py", after)


def test_a_trailing_whitespace_only_edit_changes_no_fingerprint() -> None:
    noisy = "\n".join(f"{line}   " for line in _MODULE.splitlines()) + "\n\n\n"
    assert noisy != _MODULE
    assert normalize_body(noisy) == normalize_body(_MODULE)
    assert file_fingerprint("pkg/mod.py", noisy) == file_fingerprint("pkg/mod.py", _MODULE)

    before = _windows(_MODULE)["gamma"]
    after = _windows(noisy)["gamma"]
    assert symbol_fingerprint("method", "Beta.gamma", before.body) == symbol_fingerprint(
        "method", "Beta.gamma", after.body
    )


def test_crlf_line_endings_change_no_fingerprint() -> None:
    assert file_fingerprint("pkg/mod.py", _MODULE.replace("\n", "\r\n")) == file_fingerprint("pkg/mod.py", _MODULE)


def test_inserting_ten_lines_above_a_function_changes_neither_key_nor_fingerprint() -> None:
    """The whole point of the design: identity and content are separate questions."""

    shifted = ("# padding\n" * 10) + _MODULE
    before = _windows(_MODULE)["gamma"]
    after = _windows(shifted)["gamma"]

    assert after.start_line == before.start_line + 10
    assert unit_key("symbol", "pkg/mod.py", "Beta.gamma", 0) == unit_key("symbol", "pkg/mod.py", "Beta.gamma", 0)
    assert symbol_fingerprint("method", "Beta.gamma", before.body) == symbol_fingerprint(
        "method", "Beta.gamma", after.body
    )


def test_renaming_a_function_changes_its_unit_key_not_only_its_fingerprint() -> None:
    old = unit_key("symbol", "pkg/mod.py", "alpha", 0)
    new = unit_key("symbol", "pkg/mod.py", "alpha_renamed", 0)
    assert old != new


def test_moving_a_file_changes_the_file_key_but_no_symbol_fingerprint() -> None:
    assert unit_key("file", "pkg/mod.py", "", 0) != unit_key("file", "pkg/moved.py", "", 0)
    window = _windows(_MODULE)["gamma"]
    moved = _windows(_MODULE, "pkg/moved.py")["gamma"]
    assert symbol_fingerprint("method", "Beta.gamma", window.body) == symbol_fingerprint(
        "method", "Beta.gamma", moved.body
    )


def test_unit_key_carries_no_line_number_and_no_content() -> None:
    """Same inputs, different file content and position -- one key."""

    assert unit_key("symbol", "pkg/mod.py", "alpha", 0) == unit_key("symbol", "pkg/mod.py", "alpha", 0)
    assert unit_key("symbol", "pkg/mod.py", "alpha", 0) != unit_key("symbol", "pkg/mod.py", "alpha", 1)
    assert unit_key("file", "pkg/mod.py", "", 0).startswith("fil:")
    assert unit_key("hunk", "pkg/mod.py", "", 3).startswith("hun:")
    assert unit_key("symbol", "pkg/mod.py", "alpha", 0).startswith("sym:")


def test_the_digest_separator_stops_adjacent_parts_from_colliding() -> None:
    assert unit_key("file", "ab", "c", 0) != unit_key("file", "a", "bc", 0)


def _unit(key: str, fingerprint: str) -> ReviewUnit:
    return ReviewUnit(
        revision_id="",
        unit_key=key,
        kind="file",
        path="pkg/mod.py",
        content_fingerprint=fingerprint,
    )


def test_tree_fingerprint_is_order_independent_and_stable() -> None:
    units = [_unit("fil:a", "one"), _unit("fil:b", "two"), _unit("fil:c", "three")]
    assert tree_fingerprint(units) == tree_fingerprint(list(reversed(units)))
    assert tree_fingerprint(units) == tree_fingerprint(units)


def test_tree_fingerprint_ignores_parser_and_symbol_projection_drift() -> None:
    """Revision identity is reviewed file bytes, not today's analysis vocabulary."""

    file_unit = _unit("fil:a", "file-bytes")
    old_symbol = ReviewUnit(
        revision_id="",
        unit_key="sym:old",
        kind="symbol",
        path="pkg/mod.py",
        symbol="evidence",
        content_fingerprint="symbol-body",
    )
    renamed_projection = ReviewUnit(
        revision_id="",
        unit_key="sym:new",
        kind="symbol",
        path="pkg/mod.py",
        symbol="detail.evidence",
        content_fingerprint="symbol-body",
    )

    assert tree_fingerprint([file_unit, old_symbol]) == tree_fingerprint([file_unit, renamed_projection])


def test_tree_fingerprint_changes_when_any_unit_content_changes() -> None:
    units = [_unit("fil:a", "one"), _unit("fil:b", "two")]
    changed = [_unit("fil:a", "one"), _unit("fil:b", "two-prime")]
    assert tree_fingerprint(units) != tree_fingerprint(changed)


def test_an_empty_unit_set_still_fingerprints() -> None:
    assert tree_fingerprint(()) == tree_fingerprint(())
    assert tree_fingerprint(()) != ""


# --------------------------------------------------------------------------- #
# derive_units
# --------------------------------------------------------------------------- #


def test_derive_units_emits_one_file_unit_one_hunk_unit_and_the_touched_symbols() -> None:
    hunk = _hunk(15, 3, ranges=((16, 16),), patch=" class Beta:\n-        return 2\n+        return 3\n")
    packet = _packet(
        files=(ChangedFile(path="pkg/mod.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="gamma",
                qualified_name="Beta.gamma",
                kind="method",
                file_path="pkg/mod.py",
                start_line=16,
                end_line=17,
                change="modified",
                caller_count=4,
                centrality_rank=2,
            ),
        ),
        order=(ReviewOrderEntry(path="pkg/mod.py", rank=1, score=9.0, reasons=("4 known callers",)),),
    )
    units = derive_units(packet, {"pkg/mod.py": _MODULE})
    kinds = [unit.kind for unit in units]
    assert kinds.count("file") == 1
    assert kinds.count("hunk") == 1
    assert "symbol" in kinds

    # ``Beta`` is a unit too: the changed line is inside its body, and a class
    # whose method changed is a thing a reviewer reads.
    symbols = {unit.symbol: unit for unit in units if unit.kind == "symbol"}
    assert set(symbols) == {"Beta", "Beta.gamma"}
    assert symbols["Beta.gamma"].fingerprint_method == "symbol_body_sha256"
    assert symbols["Beta.gamma"].reasons == ("4 known callers", "symbol modified", "centrality rank 2")


def test_only_definitions_a_hunk_actually_touched_become_symbol_units() -> None:
    """``alpha`` is in the file and in ``packet.symbols``; no hunk reaches it."""

    hunk = _hunk(15, 3, ranges=((16, 16),))
    packet = _packet(
        files=(ChangedFile(path="pkg/mod.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="gamma",
                qualified_name="Beta.gamma",
                kind="method",
                file_path="pkg/mod.py",
                start_line=16,
                end_line=17,
                change="modified",
            ),
        ),
    )
    names = {unit.symbol for unit in derive_units(packet, {"pkg/mod.py": _MODULE}) if unit.kind == "symbol"}
    assert "alpha" not in names


def test_a_hunk_with_no_ranges_widens_rather_than_dropping_every_symbol() -> None:
    """``new_ranges == ()`` means "unreadable patch body", never "nothing changed"."""

    hunk = _hunk(1, 20, ranges=())
    packet = _packet(
        files=(ChangedFile(path="pkg/mod.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="alpha",
                qualified_name="alpha",
                kind="function",
                file_path="pkg/mod.py",
                start_line=5,
                end_line=12,
                change="modified",
            ),
        ),
    )
    names = {unit.symbol for unit in derive_units(packet, {"pkg/mod.py": _MODULE}) if unit.kind == "symbol"}
    assert "alpha" in names


def test_a_file_with_no_impact_symbols_still_gets_its_file_and_hunk_units() -> None:
    hunk = _hunk(1, 2, ranges=((1, 2),), patch="+a\n+b\n")
    packet = _packet(files=(ChangedFile(path="docs/readme.md", old_path=None, status="modified", hunks=(hunk,)),))
    units = derive_units(packet, {"docs/readme.md": "a\nb\n"})
    assert [unit.kind for unit in units] == ["file", "hunk"]


def test_a_binary_file_is_unknown_and_can_never_read_as_reviewed() -> None:
    packet = _packet(files=(ChangedFile(path="assets/logo.png", old_path=None, status="modified", is_binary=True),))
    units = derive_units(packet, {})
    assert len(units) == 1
    assert units[0].fingerprint_method == "unknown"
    assert units[0].content_fingerprint != ""


def test_a_deleted_file_is_unknown_but_still_a_unit() -> None:
    packet = _packet(files=(ChangedFile(path="pkg/gone.py", old_path="pkg/gone.py", status="deleted"),))
    units = derive_units(packet, {"pkg/gone.py": ""})
    assert units[0].fingerprint_method == "unknown"
    assert units[0].kind == "file"


def test_a_file_whose_blob_never_loaded_is_unknown_rather_than_missing() -> None:
    packet = _packet(files=(ChangedFile(path="pkg/huge.py", old_path=None, status="modified"),))
    units = derive_units(packet, {})
    assert units[0].fingerprint_method == "unknown"


def test_an_absent_blob_is_unknown_where_an_empty_one_is_content() -> None:
    """Absence is the signal, and it has to survive all the way to the mark.

    ``load_blobs`` used to store ``""`` for a file it could not read (over the
    blob cap, or an unmerged path with no stage-0 entry). The key was therefore
    present, this branch never fired, and the file was fingerprinted over empty
    text under ``blob_sha256`` -- a per-path constant that does not move however
    the file is rewritten, so a ``reviewed`` mark survived a full regeneration of
    content nobody had seen. Both downgrade doors key on
    ``fingerprint_method == "unknown"``, so the absent key must reach them, and
    an *actually* empty file must not be dragged along with it: an empty file is
    a file we looked at.
    """

    from lemoncrow.pro.capabilities.review.revisions import _next_state
    from lemoncrow.pro.capabilities.review.session_models import ReviewMark
    from lemoncrow.pro.capabilities.review.sources.local import effective_mark_state

    packet = _packet(files=(ChangedFile(path="uv.lock", old_path=None, status="modified"),))

    unread = derive_units(packet, {})[0]
    empty = derive_units(packet, {"uv.lock": ""})[0]

    assert unread.fingerprint_method == "unknown"
    assert empty.fingerprint_method == "blob_sha256"
    assert unread.content_fingerprint != empty.content_fingerprint
    assert unread.unit_key == empty.unit_key, "identity does not depend on whether we could read it"

    assert effective_mark_state(unread, "reviewed") == "unknown"
    assert effective_mark_state(empty, "reviewed") == "reviewed"

    mark = ReviewMark(
        review_id="r1",
        unit_key=unread.unit_key,
        state="reviewed",
        reviewed_revision_id="rev1",
        content_fingerprint=unread.content_fingerprint,
    )
    assert _next_state(mark, unread, None, {}) == "unknown"
    assert _next_state(mark, empty, None, {}) == "changed_since_review"


def test_a_hunk_without_a_captured_body_falls_back_to_geometry_with_unknown_method() -> None:
    captured = _hunk(1, 2, ranges=((1, 2),), patch="+a\n+b\n")
    bare = _hunk(1, 2, ranges=((1, 2),))
    with_body = derive_units(
        _packet(files=(ChangedFile(path="pkg/x.py", old_path=None, status="modified", hunks=(captured,)),)),
        {"pkg/x.py": "a\nb\n"},
    )
    without = derive_units(
        _packet(files=(ChangedFile(path="pkg/x.py", old_path=None, status="modified", hunks=(bare,)),)),
        {"pkg/x.py": "a\nb\n"},
    )
    assert with_body[1].fingerprint_method == "hunk_patch_sha256"
    assert without[1].fingerprint_method == "unknown"
    assert without[1].content_fingerprint != ""
    assert with_body[1].unit_key == without[1].unit_key


def test_a_deleted_symbol_gets_a_synthetic_unknown_fingerprint() -> None:
    hunk = _hunk(1, 2, ranges=((1, 2),))
    packet = _packet(
        files=(ChangedFile(path="pkg/mod.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="vanished",
                qualified_name="vanished",
                kind="function",
                file_path="pkg/mod.py",
                start_line=40,
                end_line=40,
                change="deleted",
            ),
        ),
    )
    deleted = [unit for unit in derive_units(packet, {"pkg/mod.py": _MODULE}) if unit.symbol == "vanished"]
    assert len(deleted) == 1
    assert deleted[0].fingerprint_method == "unknown"
    assert deleted[0].content_fingerprint != ""


def test_same_named_methods_in_one_file_are_named_by_their_class_not_by_an_ordinal() -> None:
    """Two ``run`` methods must not both be called ``pkg/two.py::run``.

    Distinct ``unit_key``s were never the problem -- nothing false survived in
    the store. The *rows* were the problem: a reviewer sent to ``run`` arrives
    at one of two methods and has no way to tell which, and in a real repository
    the same name showed up in UNCHANGED REVIEWED and NEW SINCE MY LAST REVIEW
    at the same time. Nesting is the disambiguator the code already carries, and
    it survives the insertion that an ordinal or a line number would not.
    """

    text = "class One:\n    def run(self):\n        return 1\n\n\nclass Two:\n    def run(self):\n        return 2\n"
    hunk = _hunk(1, 8, ranges=((1, 8),))
    packet = _packet(
        files=(ChangedFile(path="pkg/two.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="run",
                qualified_name=None,
                kind="method",
                file_path="pkg/two.py",
                start_line=2,
                end_line=3,
                change="modified",
            ),
        ),
    )
    runs = [unit for unit in derive_units(packet, {"pkg/two.py": text}) if unit.symbol.endswith("run")]
    assert [unit.symbol for unit in runs] == ["One.run", "Two.run"]
    assert runs[0].unit_key != runs[1].unit_key
    # One ``ChangedSymbol`` is claimed by one window. Handing the same impact
    # record to both would give both methods one qualified name -- and one of
    # them would be wrong in the way that sends a reviewer to the other method.
    assert len({unit.content_fingerprint for unit in runs}) == 2


def test_stale_index_parent_cannot_collapse_sibling_methods_onto_one_unit_key() -> None:
    text = (
        "class CommandRunner:\n"
        "    def run(self):\n"
        "        return 1\n\n"
        "class HttpServiceRunner:\n"
        "    def run(self):\n"
        "        return 2\n"
    )
    hunk = _hunk(1, 7, ranges=((1, 7),))
    packet = _packet(
        files=(ChangedFile(path="runtimes.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="run",
                qualified_name="ReviewRunner.run",
                kind="method",
                file_path="runtimes.py",
                start_line=2,
                end_line=3,
                change="modified",
            ),
            ChangedSymbol(
                symbol_name="run",
                qualified_name="ReviewRunner.run",
                kind="method",
                file_path="runtimes.py",
                start_line=6,
                end_line=7,
                change="modified",
            ),
        ),
    )

    runs = [unit for unit in derive_units(packet, {"runtimes.py": text}) if unit.symbol.endswith("run")]
    assert [unit.symbol for unit in runs] == ["CommandRunner.run", "HttpServiceRunner.run"]
    assert len({unit.unit_key for unit in runs}) == 2


def test_index_bare_name_cannot_collapse_scoped_shell_assignments_onto_one_unit_key() -> None:
    """A lossy index name must not erase scope recovered from containment.

    Shell indexing can report both a module assignment and an assignment inside
    a function as the same bare qualified name. That used to produce two
    ``ReviewUnit`` rows with the same ``(revision_id, unit_key)`` and made a bare
    ``lc review`` die in ``ReviewStore.add_revision``.
    """

    text = (
        'LEMONCROW_TELEGRAPHIC="${LEMONCROW_TELEGRAPHIC:-}"\n'
        "prompt_telegraphic_selection() {\n"
        '    LEMONCROW_TELEGRAPHIC="lite"\n'
        "}\n"
    )
    hunk = _hunk(1, 4, ranges=((1, 4),))
    packet = _packet(
        files=(ChangedFile(path="scripts/lib/common.sh", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="LEMONCROW_TELEGRAPHIC",
                qualified_name="LEMONCROW_TELEGRAPHIC",
                kind="variable",
                file_path="scripts/lib/common.sh",
                start_line=1,
                end_line=1,
                change="modified",
            ),
            ChangedSymbol(
                symbol_name="LEMONCROW_TELEGRAPHIC",
                qualified_name="LEMONCROW_TELEGRAPHIC",
                kind="variable",
                file_path="scripts/lib/common.sh",
                start_line=3,
                end_line=3,
                change="modified",
            ),
        ),
    )

    assignments = [
        unit
        for unit in derive_units(packet, {"scripts/lib/common.sh": text})
        if unit.symbol.endswith("LEMONCROW_TELEGRAPHIC")
    ]
    assert [unit.symbol for unit in assignments] == [
        "LEMONCROW_TELEGRAPHIC",
        "prompt_telegraphic_selection.LEMONCROW_TELEGRAPHIC",
    ]
    assert len({unit.unit_key for unit in assignments}) == 2


def test_two_definitions_nesting_cannot_separate_still_fall_back_to_the_ordinal() -> None:
    """Containment is not always enough, and the ordinal is still the backstop.

    Two module-level ``run``s qualify to the same name, so ``unit_key`` has
    nothing but the ordinal left to keep them apart. The keys must still differ;
    naming them apart on screen is then the label's job (``@L2`` / ``@L6``).
    """

    text = "def run():\n    return 1\n\n\ndef run():\n    return 2\n"
    hunk = _hunk(1, 6, ranges=((1, 6),))
    packet = _packet(
        files=(ChangedFile(path="pkg/flat.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="run",
                qualified_name=None,
                kind="function",
                file_path="pkg/flat.py",
                start_line=1,
                end_line=2,
                change="modified",
            ),
        ),
    )
    runs = [unit for unit in derive_units(packet, {"pkg/flat.py": text}) if unit.symbol == "run"]
    assert [unit.ordinal for unit in runs] == [0, 1]
    assert [unit.start_line for unit in runs] == [1, 5]
    assert runs[0].unit_key != runs[1].unit_key


def test_attention_rank_group_and_reasons_are_inherited_by_every_unit_of_the_file() -> None:
    hunk = _hunk(1, 2, ranges=((1, 2),), patch="+a\n")
    packet = _packet(
        files=(ChangedFile(path="pkg/x.py", old_path=None, status="modified", hunks=(hunk,)),),
        order=(
            ReviewOrderEntry(path="pkg/x.py", rank=3, score=1.0, reasons=("public contract changed",), group="test"),
        ),
    )
    for unit in derive_units(packet, {"pkg/x.py": "a\n"}):
        assert unit.attention_rank == 3
        assert unit.attention_group == "test"
        assert unit.reasons == ("public contract changed",)


def test_an_unranked_file_gets_rank_zero_rather_than_going_missing() -> None:
    """``--limit`` truncates the order, never the change."""

    packet = _packet(
        files=(
            ChangedFile(path="pkg/ranked.py", old_path=None, status="modified"),
            ChangedFile(path="pkg/unranked.py", old_path=None, status="modified", category="docs"),
        ),
        order=(ReviewOrderEntry(path="pkg/ranked.py", rank=1, score=1.0),),
    )
    units = {unit.path: unit for unit in derive_units(packet, {"pkg/ranked.py": "a", "pkg/unranked.py": "b"})}
    assert units["pkg/ranked.py"].attention_rank == 1
    assert units["pkg/unranked.py"].attention_rank == 0
    assert units["pkg/unranked.py"].attention_group == "docs"


def test_derive_units_leaves_revision_id_blank_for_the_store_to_stamp() -> None:
    packet = _packet(files=(ChangedFile(path="pkg/x.py", old_path=None, status="modified"),))
    assert all(unit.revision_id == "" for unit in derive_units(packet, {"pkg/x.py": "a"}))


def test_derive_units_over_an_empty_packet_returns_nothing_and_does_not_raise() -> None:
    assert derive_units(_packet(), {}) == ()


def test_no_unit_ever_carries_an_empty_content_fingerprint() -> None:
    hunk = _hunk(15, 3, ranges=((16, 16),))
    packet = _packet(
        files=(
            ChangedFile(path="pkg/mod.py", old_path=None, status="modified", hunks=(hunk,)),
            ChangedFile(path="assets/logo.png", old_path=None, status="modified", is_binary=True),
            ChangedFile(path="pkg/gone.py", old_path="pkg/gone.py", status="deleted"),
        ),
        symbols=(
            ChangedSymbol(
                symbol_name="gamma",
                qualified_name="Beta.gamma",
                kind="method",
                file_path="pkg/mod.py",
                start_line=16,
                end_line=17,
                change="modified",
            ),
        ),
    )
    for unit in derive_units(packet, {"pkg/mod.py": _MODULE}):
        assert unit.content_fingerprint != ""


# --------------------------------------------------------------------------- #
# nothing a reviewer cannot attest to may become a unit
# --------------------------------------------------------------------------- #


_VITEST_SUITE = """import { afterEach, describe } from "vitest";

export function navigateTo(url: string): void {
  window.history.replaceState({}, "", url);
}

afterEach(() => {
  navigateTo("/");
});
"""

_TSX_TERNARY = """export function Shell({ bare }: { bare: boolean }) {
  return (
    <div className={bare ? "min-h-full font-mono" : "min-h-full bg-tint"}>
      {bare}
    </div>
  );
}
"""

_PACKAGE_JSON = '{\n  "name": "app",\n  "dependencies": {\n    "react": "19.0.0"\n  }\n}\n'


def test_an_import_binding_is_not_a_window_and_so_never_a_unit() -> None:
    """`afterEach` is imported on line 1 and called on line 7 -- defined nowhere."""

    names = set(_windows(_VITEST_SUITE, "src/reviewApi.test.ts"))

    assert "afterEach" not in names
    assert "describe" not in names
    assert "navigateTo" in names


def test_a_string_literal_is_not_a_window_and_so_never_a_unit() -> None:
    for name in _windows(_TSX_TERNARY, "src/App.tsx"):
        assert '"' not in name, f"a reviewer cannot attest to {name!r}"
        assert " " not in name


def test_a_json_key_is_not_a_window_and_so_never_a_unit() -> None:
    assert _windows(_PACKAGE_JSON, "package.json") == {}


def test_a_toml_key_is_not_a_window_and_so_never_a_unit() -> None:
    assert _windows('[project]\nname = "app"\nversion = "1.0"\n', "pyproject.toml") == {}


def test_a_yaml_key_is_not_a_window_and_so_never_a_unit() -> None:
    assert _windows("jobs:\n  build:\n    runs-on: ubuntu\n", ".github/workflows/ci.yml") == {}


def test_derive_units_mints_no_unit_for_an_imported_binding() -> None:
    """The gate is in the window seam, so it holds for units without a second copy."""

    hunk = _hunk(1, 9, ranges=((1, 9),), patch='+import { afterEach } from "vitest";\n')
    packet = _packet(
        files=(ChangedFile(path="src/reviewApi.test.ts", old_path=None, status="added", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="navigateTo",
                qualified_name="navigateTo",
                kind="function",
                file_path="src/reviewApi.test.ts",
                start_line=3,
                end_line=5,
                change="added",
            ),
        ),
    )
    units = derive_units(packet, {"src/reviewApi.test.ts": _VITEST_SUITE})
    names = {unit.symbol for unit in units if unit.kind == "symbol"}

    assert "afterEach" not in names
    assert names == {"navigateTo"}
