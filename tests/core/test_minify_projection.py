"""Tests for the tree-sitter minified source projection (read view + mapped edits)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.source_projection import (
    MinifiedEditError,
    apply_minified_edit,
    build_minified_projection,
    language_for_minify,
)
from lemoncrow.pro.capabilities.tool_supervision.rich_edit import apply_rich_edits

PY_SAMPLE = '''"""Module docstring."""
import os


def add(a, b):
    """Return the sum."""
    # inline note
    label = "keep   inner   spaces"
    return a + b


class Worker:
    """Worker doc."""

    value = 1

    def run(self):
        return add(self.value, 2)
'''


def _reconstructs(result: Any) -> bool:
    rebuilt = "".join(result.content[s.projected_start : s.projected_end] for s in result.mapping.segments)
    return bool(rebuilt == result.content)


def test_strips_comments_and_blanks_keeps_strings_and_docstrings() -> None:
    r = build_minified_projection(PY_SAMPLE, "python", path="m.py", include_mapping=True)
    assert r.applied
    assert "Module docstring" in r.content  # docstrings preserved (semantic content)
    assert "Return the sum" in r.content
    assert "Worker doc" in r.content
    assert "# inline note" not in r.content  # comment stripped
    assert "keep   inner   spaces" in r.content  # string interior preserved verbatim
    assert "\n\n\n" not in r.content  # blank-line runs collapsed
    assert r.saved_tokens > 0


def test_mapping_reconstructs_and_is_idempotent() -> None:
    r = build_minified_projection(PY_SAMPLE, "python", path="m.py", include_mapping=True)
    assert r.mapping is not None and r.mapping.projection_kind == "minified"
    assert _reconstructs(r)
    again = build_minified_projection(r.content, "python")
    assert (not again.applied) or again.content == r.content


def test_docstrings_preserved_when_minifying() -> None:
    src = 'def stub():\n    """Only docstring."""\n\n\ndef real():\n    # x\n    return 1\n'
    r = build_minified_projection(src, "python")
    assert r.applied  # blank lines + comment still yield savings
    assert "Only docstring" in r.content  # docstring preserved verbatim
    assert "# x" not in r.content


def test_unicode_is_byte_safe() -> None:
    src = 'def f():\n    # café ☕\n    return "naïve"\n'
    r = build_minified_projection(src, "python", include_mapping=True)
    assert r.applied and _reconstructs(r)
    assert "café" not in r.content  # comment dropped
    assert 'return "naïve"' in r.content


def test_go_minifies_without_indentation() -> None:
    src = 'package   main\n\n// greet\nfunc   Greet()   {\n\tprintln(   "hi"   )\n}\n'
    r = build_minified_projection(src, "go", path="g.go", include_mapping=True)
    assert r.applied and _reconstructs(r)
    assert "// greet" not in r.content
    assert 'println( "hi" )' in r.content


def test_excluded_language_skips() -> None:
    r = build_minified_projection("# Title\n\nsome   prose\n", "markdown")
    assert not r.applied


def test_language_for_minify_resolution() -> None:
    assert language_for_minify("a.py") == "python"
    assert language_for_minify("a.go") == "go"
    assert language_for_minify("a.md") is None
    assert language_for_minify("a.unknownext") is None


def test_edit_round_trip_python_preserves_disk_comment_and_docstring() -> None:
    updated, line_start, line_end = apply_minified_edit(PY_SAMPLE, "python", "return a + b", "return a + b + 1")
    assert "return a + b + 1" in updated
    assert "# inline note" in updated  # comment intact on disk
    assert "Return the sum" in updated  # docstring intact on disk
    assert line_start == line_end


def test_edit_round_trip_go() -> None:
    src = 'package main\n\nfunc Greet(name string) {\n\tprintln("hi", name)  // x\n}\n'
    updated, _, _ = apply_minified_edit(src, "go", 'println("hi", name)', 'println("hello", name)')
    assert 'println("hello", name)' in updated
    assert "// x" in updated  # trailing comment intact on disk


def test_edit_ambiguous_raises() -> None:
    # The comment makes minification apply; the duplicated statement is ambiguous.
    src = "def f():\n    # note\n    x = 1\n    x = 1\n    return x\n"
    with pytest.raises(MinifiedEditError) as exc:
        apply_minified_edit(src, "python", "x = 1", "x = 2")
    assert exc.value.code == "ambiguous"


def test_edit_no_match_raises() -> None:
    with pytest.raises(MinifiedEditError) as exc:
        apply_minified_edit(PY_SAMPLE, "python", "nonexistent_token_xyz", "z")
    assert exc.value.code == "no_match"


def test_edit_dropped_interior_fails_closed() -> None:
    # old_string as seen in the minified view spans a comment dropped on disk.
    src = "def f():\n    a = 1\n    # critical\n    b = 2\n    return a + b\n"
    with pytest.raises(MinifiedEditError) as exc:
        apply_minified_edit(src, "python", "a = 1\n    b = 2", "a = 10\n    b = 20")
    assert exc.value.code == "comment_inside_span"


def test_rich_edit_minified_fallback_applies() -> None:
    disk = "def compute():\n    total     =     0\n    return total\n"
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "m.py"
        f.write_text(disk, encoding="utf-8")
        res = apply_rich_edits(
            [{"file_path": str(f), "old_string": "total = 0", "new_string": "total = 42"}],
            repo_root=d,
        )
        assert not res["rolled_back"]
        assert res["applied"][0]["match_mode"] == "minified"
        after = f.read_text(encoding="utf-8")
        assert "total = 42" in after
        assert "total     =" not in after


def test_rich_edit_minified_fails_closed_preserves_comment() -> None:
    disk = "def f():\n    x = 1\n    # critical\n    y = 2\n    return x + y\n"
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "n.py"
        f.write_text(disk, encoding="utf-8")
        res = apply_rich_edits(
            [{"file_path": str(f), "old_string": "x = 1\n    y = 2", "new_string": "x = 10\n    y = 20"}],
            repo_root=d,
        )
        # minified fails closed (comment inside span) -> fuzzy -> rejected -> rollback
        assert res["rolled_back"]
        assert "# critical" in f.read_text(encoding="utf-8")
