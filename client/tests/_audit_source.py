"""Reading the client's source the way an auditor would: code, not prose.

A naive ``"cloudflared" in source`` search fails on this package for a funny
reason -- its documentation talks at length about the things it does *not* do,
so the forbidden words are all over the docstrings. Scanning raw text would
either flag every honest explanation or force the explanations out of the code,
and the explanations are the point.

So the scan reads code only: comments are dropped, and so are docstrings --
precisely the string literals ``ast`` identifies as docstrings, and no others,
so a string that is actually *used* (a program name, a URL, a path) is still
scanned. Everything the audit asserts is therefore asserted about executable
code.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Iterator
from pathlib import Path

__all__ = ["PACKAGE_ROOT", "code_only", "iter_modules"]

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "lemoncrow_client"


def iter_modules() -> Iterator[Path]:
    """Every Python module the distribution ships, in a stable order."""
    yield from sorted(PACKAGE_ROOT.rglob("*.py"))


def _docstring_positions(tree: ast.AST) -> set[tuple[int, int]]:
    positions: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            positions.add((first.value.lineno, first.value.col_offset))
    return positions


def code_only(path: Path) -> str:
    """The module's source with comments and docstrings blanked out.

    Blanked, not deleted: the characters are replaced with spaces so every
    other token keeps its exact line, column and spelling. A scan for a dotted
    call like ``atexit.register`` has to see the source as it is written, and a
    reconstruction that re-spaced the tokens would make such a scan silently
    match nothing -- a test that cannot fail.
    """
    source = path.read_text(encoding="utf-8")
    docstrings = _docstring_positions(ast.parse(source))
    lines = [list(line) for line in source.splitlines(keepends=True)]

    def blank(start: tuple[int, int], end: tuple[int, int]) -> None:
        start_row, start_col = start
        end_row, end_col = end
        for row in range(start_row, end_row + 1):
            if row - 1 >= len(lines):
                break
            line = lines[row - 1]
            first = start_col if row == start_row else 0
            last = end_col if row == end_row else len(line)
            for column in range(first, min(last, len(line))):
                if line[column] != "\n":
                    line[column] = " "

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT or (token.type == tokenize.STRING and token.start in docstrings):
            blank(token.start, token.end)
    return "".join("".join(line) for line in lines)
