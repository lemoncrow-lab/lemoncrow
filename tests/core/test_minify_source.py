"""Tests for minify_source — LINEAR-03 / D-10.

Wave-0 RED scaffolds for the three minifier transform-safety cases
(13-02-01..03). minify_source must be a pure whitespace transform that
preserves Python and YAML semantics while collapsing trailing whitespace
and runs of 3+ consecutive newlines.
"""

from __future__ import annotations

import ast

import yaml
from atelier.core.capabilities.context_compression.minify import minify_source


def test_collapses_blank_runs() -> None:
    """13-02-01: trailing WS stripped; ≥3-newline runs collapse to two."""
    src = "line one   \nline two\t\n\n\n\nline three\n"
    out, original_tokens, minified_tokens = minify_source(src, "text")
    # Four consecutive \n become exactly two.
    assert "\n\n\n" not in out
    assert out == "line one\nline two\n\nline three\n"
    assert original_tokens >= minified_tokens


def test_python_semantics_preserved() -> None:
    """13-02-02: Python source remains parseable + AST-equivalent; leading
    indentation on every non-blank line is byte-preserved (D-10)."""
    original = "def f(x):   \n" "    if x:\t\n" "        return x   \n" "\n" "\n" "\n" "    return 0\n"
    out, _o, _m = minify_source(original, "python")
    # Compiles
    compile(out, "<test>", "exec")
    # Semantic equality via AST
    assert ast.dump(ast.parse(original)) == ast.dump(ast.parse(out))
    # Leading whitespace preserved byte-for-byte on every non-blank line.
    for orig_line, out_line in zip(original.splitlines(), out.splitlines(), strict=False):
        if orig_line.strip() == "" and out_line.strip() == "":
            continue
        if orig_line.strip() == "":
            continue
        # Compare leading whitespace prefix.
        orig_prefix = orig_line[: len(orig_line) - len(orig_line.lstrip())]
        out_prefix = out_line[: len(out_line) - len(out_line.lstrip())]
        assert orig_prefix == out_prefix, (orig_line, out_line)


def test_yaml_semantics_preserved() -> None:
    """13-02-03: YAML structural equality after minify (D-10)."""
    original = (
        "root:   \n" "  child_a: 1\t\n" "  child_b:\n" "    - one\n" "    - two   \n" "\n" "\n" "\n" "other: value\n"
    )
    out, _o, _m = minify_source(original, "yaml")
    assert yaml.safe_load(original) == yaml.safe_load(out)
