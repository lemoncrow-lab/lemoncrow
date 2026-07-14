from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.pro.capabilities.source_projection import build_compact_projection
from lemoncrow.pro.capabilities.tool_supervision.rich_edit import apply_rich_edits


def test_rich_edit_sequential_same_file_and_line_range(tmp_path: Path) -> None:
    path = tmp_path / "code.py"
    path.write_text("first\nsecond\nthird\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "code.py:L2", "old_string": "second", "new_string": "middle"},
            {"file_path": "code.py", "old_string": "middle", "new_string": "SECOND"},
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert path.read_text(encoding="utf-8") == "first\nSECOND\nthird\n"


def test_rich_edit_multiline_replacement_preserves_terminal_newline(tmp_path: Path) -> None:
    path = tmp_path / "guide.md"
    path.write_text("before\nold one\nold two\nafter\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {
                "file_path": "guide.md",
                "old_string": "old one\nold two\n",
                "new_string": "new one\nnew two\n",
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert path.read_text(encoding="utf-8") == "before\nnew one\nnew two\nafter\n"


def test_rich_edit_typography_placeholder_fuzzy_and_indent(tmp_path: Path) -> None:
    path = tmp_path / "code.py"
    path.write_text("def f():\n    value = “old”\n    keep = 1\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "code.py", "old_string": 'value = "old"', "new_string": 'value = "new"'},
            {
                "file_path": "code.py",
                "old_string": "value = ...\n    keep = 1",
                "new_string": "value = 2\nkeep = 3",
            },
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert "    keep = 3" in path.read_text(encoding="utf-8")


def test_rich_edit_atomic_rollback_and_protected_paths(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("original\n", encoding="utf-8")
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "state.txt").write_text("do not touch\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "file.txt", "old_string": "original", "new_string": "changed"},
            {"file_path": ".lemoncrow/state.txt", "old_string": "do", "new_string": "DO"},
        ],
        repo_root=tmp_path,
        atomic=True,
    )

    assert result["rolled_back"] is True
    assert path.read_text(encoding="utf-8") == "original\n"


def test_rich_edit_notebook_cell_operations_clear_outputs(tmp_path: Path) -> None:
    path = tmp_path / "nb.ipynb"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "metadata": {},
                        "source": "print(1)",
                        "outputs": [{"name": "stdout"}],
                        "execution_count": 3,
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )

    result = apply_rich_edits(
        [
            {"file_path": "nb.ipynb#cell=0", "overwrite": True, "new_string": "print(2)"},
            {
                "file_path": "nb.ipynb#cell=0",
                "cell_action": "insert_after",
                "cell_type": "markdown",
                "new_string": "# note",
            },
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    notebook = json.loads(path.read_text(encoding="utf-8"))
    assert notebook["cells"][0]["source"] == "print(2)"
    assert notebook["cells"][0]["outputs"] == []
    assert notebook["cells"][1]["cell_type"] == "markdown"


def test_rich_edit_peer_level_def_not_indented(tmp_path: Path) -> None:
    path = tmp_path / "code.py"
    path.write_text(
        "def test_foo() -> None:\n    resp = call()\n    assert resp\n",
        encoding="utf-8",
    )

    result = apply_rich_edits(
        [
            {
                "file_path": "code.py",
                "old_string": "def test_foo() -> None:\n    resp = call()\n    assert resp",
                "new_string": (
                    "def test_foo(mp) -> None:\n"
                    "    mp.setenv('X', '1')\n"
                    "    resp = call()\n"
                    "    assert resp\n"
                    "\n"
                    "\n"
                    "def test_bar(mp) -> None:\n"
                    "    mp.delenv('X', raising=False)"
                ),
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    text = path.read_text(encoding="utf-8")
    # peer-level def must stay at column 0
    assert "\ndef test_bar(mp) -> None:\n" in text
    # body lines must be indented
    assert "    mp.setenv" in text


def test_rich_edit_refuses_near_miss_over_duplicate_tail_constants(tmp_path: Path) -> None:
    """Benchmark incident: a near-miss old_string that matches two identical
    blocks equally must be refused, not silently anchored onto the first.
    Exact/normalized/minified all miss (the body has a typo); the fuzzy rung now
    raises on the tie and apply_rich_edits rolls back, leaving the file intact.
    """
    path = tmp_path / "mod.py"
    body = (
        '    "alpha shared rule line",\n'
        '    "beta shared rule line",\n'
        '    "gamma shared rule line",\n'
        '    "delta shared rule line",\n'
    )
    original = "FIRST = [\n" + body + "]\nSECOND = [\n" + body + "]\n"
    path.write_text(original, encoding="utf-8")

    # near-miss body (one typo) with no distinguishing name matches both blocks
    near_miss = (
        '    "alpha shared rule line",\n'
        '    "beta shared rule line",\n'
        '    "gamma shared rule lineX",\n'
        '    "delta shared rule line",\n'
    )
    result = apply_rich_edits(
        [{"file_path": "mod.py", "old_string": near_miss, "new_string": ""}],
        repo_root=tmp_path,
    )

    assert result["failed"], "ambiguous near-miss must fail, not silently corrupt"
    # the whole file must be left untouched (neither block clobbered)
    assert path.read_text(encoding="utf-8") == original


def test_rich_edit_exact_match_indented_anchor_keeps_dedented_constant(tmp_path: Path) -> None:
    """rich_edit incident: an exact match whose anchor starts inside an indented
    block must not re-indent replacement lines that legitimately dedent. Inserting
    a module-level constant after a list literal previously got every column-0 line
    prefixed with the anchor's indent, producing a SyntaxError that the parse gate
    rolled back — silently rejecting a valid edit.
    """
    path = tmp_path / "mod.py"
    original = '_A: list[str] = [\n    "x",\n    "y",\n]\n\n\ndef f() -> None:\n    return None\n'
    path.write_text(original, encoding="utf-8")

    result = apply_rich_edits(
        [
            {
                "file_path": "mod.py",
                "old_string": '    "y",\n]\n\n\ndef f() -> None:',
                "new_string": ('    "y",\n]\n\n_B: list[str] = [\n    "z",\n]\n\n\ndef f() -> None:'),
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert result["applied"][0]["match_mode"] == "exact"
    text = path.read_text(encoding="utf-8")
    # the inserted module-level constant must land at column 0, not re-indented
    assert "\n_B: list[str] = [\n" in text
    assert "    _B: list[str]" not in text


def test_rich_edit_projection_descriptor_applies_exact_span(tmp_path: Path) -> None:
    path = tmp_path / "code.go"
    source = 'package   main\n\nfunc   main()   {\n    println("hi")\n}\n'
    path.write_text(source, encoding="utf-8")
    projection = build_compact_projection(source, "go", path=str(path), include_mapping=True)

    assert projection.mapping is not None
    projected_start = projection.content.index("println")
    projected_end = projected_start + len("println")

    result = apply_rich_edits(
        [
            {
                "kind": "projection",
                "file_path": str(path),
                "projection_kind": "compact",
                "projection_mapping": projection.mapping.to_dict(),
                "projected_start": projected_start,
                "projected_end": projected_end,
                "new_string": "fmt.Println",
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert "fmt.Println" in path.read_text(encoding="utf-8")


def test_rich_edit_projection_descriptor_rejects_stale_mapping(tmp_path: Path) -> None:
    path = tmp_path / "code.go"
    source = 'package   main\n\nfunc   main()   {\n    println("hi")\n}\n'
    path.write_text(source, encoding="utf-8")
    projection = build_compact_projection(source, "go", path=str(path), include_mapping=True)
    path.write_text(source.replace("println", "panic"), encoding="utf-8")

    assert projection.mapping is not None
    projected_start = projection.content.index("println")
    projected_end = projected_start + len("println")

    result = apply_rich_edits(
        [
            {
                "kind": "projection",
                "file_path": str(path),
                "projection_kind": "compact",
                "projection_mapping": projection.mapping.to_dict(),
                "projected_start": projected_start,
                "projected_end": projected_end,
                "new_string": "fmt.Println",
            }
        ],
        repo_root=tmp_path,
    )

    assert result["rolled_back"] is True
    assert result["failed"][0]["code"] == "stale_projection_mapping"
    assert "re-read" in result["failed"][0]["hint"].lower()
    assert "stale" in result["failed"][0]["error"]
    assert result["failed"][0]["retry_with"] == {
        "tool": "read",
        "path": str(path),
        "full": True,
        "include_meta": True,
    }


def test_rich_edit_projection_descriptor_supports_exact_cursor_insertion(tmp_path: Path) -> None:
    path = tmp_path / "code.go"
    source = 'package   main\n\nfunc   main()   {\n    println("hi")\n}\n'
    path.write_text(source, encoding="utf-8")
    projection = build_compact_projection(source, "go", path=str(path), include_mapping=True)

    assert projection.mapping is not None
    projected_start = projection.content.index("println")

    result = apply_rich_edits(
        [
            {
                "kind": "projection",
                "file_path": str(path),
                "projection_kind": "compact",
                "projection_mapping": projection.mapping.to_dict(),
                "projected_start": projected_start,
                "projected_end": projected_start,
                "new_string": "log.",
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert "log.println" in path.read_text(encoding="utf-8")


def test_rich_edit_fuzzy_similarity_floor_rejects_bad_match(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    original = "def target():\n    return ACTUAL_DISK_VALUE\n"
    path.write_text(original, encoding="utf-8")

    result = apply_rich_edits(
        [
            {
                "file_path": "mod.py",
                "old_string": "def target():\n    return OLD\n",
                "new_string": "def target():\n    return NEW\n",
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"], "low-similarity fuzzy match must be rejected"
    assert "not found" in result["failed"][0]["error"]
    assert path.read_text(encoding="utf-8") == original


def test_rich_edit_noop_when_edit_already_applied(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text("x = 1\n", encoding="utf-8")
    edit = {"file_path": "mod.py", "old_string": "x = 1", "new_string": "x = 2"}

    first = apply_rich_edits([dict(edit)], repo_root=tmp_path)
    assert first["failed"] == []
    assert first["applied"][0]["match_mode"] == "exact"

    second = apply_rich_edits([dict(edit)], repo_root=tmp_path)
    assert second["failed"] == []
    assert second["applied"][0]["match_mode"] == "noop"
    assert second["applied"][0]["already_applied"] is True
    assert path.read_text(encoding="utf-8") == "x = 2\n"


def test_rich_edit_noop_when_formatter_rewrapped_new_string(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    formatted = "result = compute(\n    alpha_value,\n    beta_value,\n    gamma_value,\n    delta_value,\n)\n"
    path.write_text(formatted, encoding="utf-8")

    result = apply_rich_edits(
        [
            {
                "file_path": "mod.py",
                "old_string": "result = build(alpha_value, beta_value, gamma_value, delta_value)",
                "new_string": "result = compute(alpha_value, beta_value, gamma_value, delta_value)",
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert result["applied"][0]["match_mode"] == "noop"
    assert result["applied"][0]["already_applied"] is True
    assert path.read_text(encoding="utf-8") == formatted


def test_rich_edit_atomic_failure_reports_already_applied(tmp_path: Path) -> None:
    path_a = tmp_path / "a.py"
    path_a.write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def target():\n    return ACTUAL_DISK_VALUE\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
            {
                "file_path": "b.py",
                "old_string": "def missing():\n    return OLD\n",
                "new_string": "def missing():\n    return NEW\n",
            },
        ],
        repo_root=tmp_path,
    )

    assert result["rolled_back"] is True
    assert result["failed"]
    assert result["already_applied"] == ["a.py"]
    assert path_a.read_text(encoding="utf-8") == "x = 2\n"


def test_rich_edit_scoped_not_found_reports_already_applied(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    content = (
        "import os\n\n\ndef helper():\n    return os.environ\n\n\n"
        "result = compute(\n    alpha_value,\n    beta_value,\n    gamma_value,\n    delta_value,\n)\n"
    )
    path.write_text(content, encoding="utf-8")

    result = apply_rich_edits(
        [
            {
                "file_path": "mod.py:L1-L2",
                "old_string": "result = build(alpha_value, beta_value, gamma_value, delta_value)",
                "new_string": "result = compute(alpha_value, beta_value, gamma_value, delta_value)",
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"]
    assert result["failed"][0]["already_applied"] is True
    assert "do not retry" in result["failed"][0]["hint"]
    assert "retry_with" not in result["failed"][0]
    assert path.read_text(encoding="utf-8") == content


def test_rich_edit_parse_gate_rolls_back_corrupt_python(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    original = "value = 1\n"
    path.write_text(original, encoding="utf-8")

    result = apply_rich_edits(
        [{"file_path": "mod.py", "old_string": "value = 1", "new_string": "def broken(:"}],
        repo_root=tmp_path,
    )

    assert result["failed"]
    assert "parse error" in result["failed"][0]["error"]
    assert path.read_text(encoding="utf-8") == original


def test_rich_edit_retry_hint_targets_failing_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha = 1\n", encoding="utf-8")
    path_b = tmp_path / "b.py"
    path_b.write_text("def target():\n    return ACTUAL_DISK_VALUE\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "a.py", "old_string": "alpha = 1", "new_string": "alpha = 2"},
            {
                "file_path": "b.py",
                "old_string": "def target():\n    return OLD\n",
                "new_string": "def target():\n    return NEW\n",
            },
        ],
        repo_root=tmp_path,
    )

    assert result["failed"]
    hint = result["failed"][0].get("retry_with")
    assert hint is not None, "not-found failure must ship a retry_with hint"
    assert hint["path"].startswith("b.py:L1-")
    assert "ACTUAL_DISK_VALUE" in hint["old_string"]


def test_rich_edit_fuzzy_line_snap_preserves_trailing_newline(tmp_path: Path) -> None:
    """Session replay (daemon.py incident): fuzzy window ends at a def signature.

    new_string has no trailing newline; the line-snapped replacement must not
    glue the following body line onto the signature (which parses and would
    slip past the parse gate).
    """
    path = tmp_path / "mod.py"
    original = "# ---- helpers ----\ndef alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    path.write_text(original, encoding="utf-8")

    result = apply_rich_edits(
        [
            {
                "file_path": "mod.py",
                # comment drift (dash count) forces the fuzzy rung
                "old_string": "# --- helpers ---\ndef alpha():\n    return 1\n\ndef beta():",
                "new_string": "# --- helpers ---\ndef alpha():\n    return 99\n\ndef beta():",
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert result["applied"][0]["match_mode"] == "fuzzy"
    text = path.read_text(encoding="utf-8")
    assert "return 99" in text
    assert "def beta():\n    return 2\n" in text, "body must stay on its own line"
    assert "def beta():    return 2" not in text


def test_rich_edit_parse_gate_catches_misindented_insertion(tmp_path: Path) -> None:
    """Session replay (runtime.py incident): constants landing inside a tuple.

    A replacement whose new_string carries wrong indentation must be rolled
    back by the parse gate instead of being written silently.
    """
    path = tmp_path / "mod.py"
    original = 'NAMES = (\n    "read",\n    "search",\n)\n'
    path.write_text(original, encoding="utf-8")

    result = apply_rich_edits(
        [
            {
                "file_path": "mod.py",
                "old_string": '    "search",\n)',
                "new_string": '    "search",\n)\n\n    SAFE = frozenset(NAMES) - {"edit"}',
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"]
    assert "parse error" in result["failed"][0]["error"]
    assert path.read_text(encoding="utf-8") == original


def test_rich_edit_range_delete_no_old_string(tmp_path: Path) -> None:
    """file_path#start-end with new_string='' deletes those lines without old_string."""
    path = tmp_path / "mod.py"
    path.write_text("keep_before\ndelete_me_1\ndelete_me_2\nkeep_after\n", encoding="utf-8")

    result = apply_rich_edits(
        [{"file_path": "mod.py:L2-L3", "new_string": ""}],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert path.read_text(encoding="utf-8") == "keep_before\nkeep_after\n"


def test_rich_edit_range_replace_no_old_string(tmp_path: Path) -> None:
    """file_path#start-end with new_string replaces those lines without old_string."""
    path = tmp_path / "mod.py"
    path.write_text("a\nb\nc\nd\n", encoding="utf-8")

    result = apply_rich_edits(
        [{"file_path": "mod.py:L2-L3", "new_string": "X\nY"}],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert path.read_text(encoding="utf-8") == "a\nX\nY\nd\n"


def test_rich_edit_range_old_string_still_uses_scoped_search(tmp_path: Path) -> None:
    """When old_string is given alongside a range, the normal scoped search fires."""
    path = tmp_path / "mod.py"
    path.write_text("x = 1\nx = 2\nx = 3\n", encoding="utf-8")

    # Range restricts to line 2 so only the second 'x' is replaced
    result = apply_rich_edits(
        [{"file_path": "mod.py:L2", "old_string": "x = 2", "new_string": "x = 99"}],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert path.read_text(encoding="utf-8") == "x = 1\nx = 99\nx = 3\n"


def test_rich_edit_ambiguous_normalized_match_fails_with_candidates(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    original = "def a():\n    x  = 1\n\ndef b():\n    x =  1\n"
    path.write_text(original, encoding="utf-8")

    result = apply_rich_edits(
        [{"file_path": "mod.py", "old_string": "x   =   1", "new_string": "x = 2"}],
        repo_root=tmp_path,
    )

    assert result["failed"]
    assert "ambiguous" in result["failed"][0]["error"]
    assert path.read_text(encoding="utf-8") == original


def test_rich_edit_trailing_newline_old_string_reports_inclusive_line_end(tmp_path: Path) -> None:
    """An old_string ending in \\n must not push the reported line_end past the last changed line."""
    path = tmp_path / "code.py"
    path.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {
                "file_path": "code.py",
                "old_string": "line2\nline3\n",
                "new_string": "REPLACED\n",
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert path.read_text(encoding="utf-8") == "line1\nREPLACED\nline4\n"
    hunk = result["applied"][0]["hunks"][0]
    # Lines 2-3 were replaced; line_end must be 3 (inclusive), not 4.
    assert (hunk["line_start"], hunk["line_end"]) == (2, 3)


def test_rich_edit_content_edit_then_range_edit_same_file(tmp_path: Path) -> None:
    """A content (old/new) edit and a range (:Lx) edit to the same file in one
    batch now co-apply -- the exact case that used to roll the batch back."""
    path = tmp_path / "mod.py"
    path.write_text("line1\nlemoncrow_label\nline3\nline4\nline5\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "mod.py", "old_string": "lemoncrow_label", "new_string": "lc_label"},
            {"file_path": "mod.py:L4", "new_string": "REPLACED"},
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert path.read_text(encoding="utf-8") == "line1\nlc_label\nline3\nREPLACED\nline5\n"


def test_rich_edit_range_edit_translates_across_earlier_line_growth(tmp_path: Path) -> None:
    """A content edit that ADDS lines shifts a later range edit's pre-batch line
    number; the range must still land on the row the caller pointed at."""
    path = tmp_path / "mod.py"
    path.write_text("a\nb\nc\nd\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "mod.py", "old_string": "b", "new_string": "b1\nb2"},
            {"file_path": "mod.py:L4", "new_string": "D"},  # pre-batch L4 == 'd'
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert path.read_text(encoding="utf-8") == "a\nb1\nb2\nc\nD\n"
    # The range hunk is echoed at the line it LANDED on (pre-batch L4 + 1 line of
    # growth above it), not the raw :L4 the caller passed.
    range_hunk = next(e for e in result["applied"] if e.get("match_mode") == "range")["hunks"][0]
    assert (range_hunk["line_start"], range_hunk["line_end"]) == (5, 5)


def test_rich_edit_range_edit_after_range_then_content_same_file(tmp_path: Path) -> None:
    """Range, then content, then range on one file: every splice is tracked in
    the ledger so the trailing range edit still resolves."""
    path = tmp_path / "mod.py"
    path.write_text("a\nb\nc\nd\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "mod.py:L1", "new_string": "A"},
            {"file_path": "mod.py", "old_string": "c", "new_string": "C"},
            {"file_path": "mod.py:L4", "new_string": "D"},
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert path.read_text(encoding="utf-8") == "A\nb\nC\nD\n"


def test_rich_edit_range_edit_overlapping_earlier_content_edit_fails(tmp_path: Path) -> None:
    """A range edit that targets the same line an earlier content edit changed is
    ambiguous and must fail the batch rather than clobber the wrong region."""
    path = tmp_path / "mod.py"
    original = "a\nb\nc\n"
    path.write_text(original, encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "mod.py", "old_string": "b", "new_string": "B"},
            {"file_path": "mod.py:L2", "new_string": "X"},
        ],
        repo_root=tmp_path,
    )

    assert result["failed"]
    assert "overlaps" in result["failed"][0]["error"]
    assert result["rolled_back"] is True
    assert path.read_text(encoding="utf-8") == original


def test_rich_edit_range_edit_translates_across_earlier_line_shrink(tmp_path: Path) -> None:
    """A content edit that REMOVES lines shifts a later range edit up (negative
    delta); the range must still land on the row the caller pointed at."""
    path = tmp_path / "mod.py"
    path.write_text("a\nb\nc\nd\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "mod.py", "old_string": "b\nc", "new_string": "B"},  # 2 lines -> 1
            {"file_path": "mod.py:L4", "new_string": "D"},  # pre-batch L4 == 'd'
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert path.read_text(encoding="utf-8") == "a\nB\nD\n"


def test_rich_edit_chained_content_match_poisons_later_range_edit(tmp_path: Path) -> None:
    """When a content edit matches text an EARLIER edit inserted (a chained match),
    that splice has no pre-batch coordinate, so a following range edit to the same
    file must still be rejected loudly rather than silently hit the wrong line."""
    path = tmp_path / "mod.py"
    original = "a\nb\nc\nd\n"
    path.write_text(original, encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "mod.py", "old_string": "b", "new_string": "MID"},
            {"file_path": "mod.py", "old_string": "MID", "new_string": "X\nY"},  # chained
            {"file_path": "mod.py:L3", "new_string": "Z"},
        ],
        repo_root=tmp_path,
    )

    assert result["rolled_back"] is True
    assert "untrackable edit" in result["failed"][0]["error"]
    assert path.read_text(encoding="utf-8") == original
