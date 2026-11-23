"""``units.symbol_header`` against Python's own parser, over real source.

The header is the decorator text that a symbol move compares (the fingerprint window starts at ``def``). A scanner that
drops a decorator line while still returning text reads a decorated definition as an undecorated one and carries a
judgment it must reopen. ``ast`` knows exactly where every decorator is, so it is the oracle:

* **fail-open**: a decorated definition whose header is not ``None`` yet lacks any line of any decorator. Must be 0.
* **contamination**: a header that holds a line that is no decorator line. Reported, never an unsafe carry by itself
  (two sides contaminated alike still compare equal; one side contaminated reopens).
* **None**: refused, so never carried. A bound keeps the check from passing by refusing everything.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.units import symbol_header

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ("src/lemoncrow/pro/capabilities/review", "tests/gateway")
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _norm(line: str) -> str:
    return " ".join(line.split())


def measure(files: list[Path], *, stop_at_fail_open: bool = False) -> Counter[str]:
    counts: Counter[str] = Counter()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, SyntaxError, ValueError):
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, _DEFS):
                continue
            wanted: list[str] = []
            for decorator in node.decorator_list:
                for number in range(decorator.lineno, (decorator.end_lineno or decorator.lineno) + 1):
                    line = _norm(lines[number - 1])
                    if line and not line.startswith("#"):  # blank and comment lines are not part of the header
                        wanted.append(line)
            kind = "decorated" if node.decorator_list else "plain"
            counts[kind] += 1
            header = symbol_header(lines, node.lineno)
            if header is None:
                counts[f"{kind}_none"] += 1
                continue
            got = header.split("\n") if header else []
            if any(line not in got for line in wanted):
                counts["fail_open"] += 1
                if stop_at_fail_open:
                    return counts
            if any(line not in wanted for line in got):
                counts[f"{kind}_contaminated"] += 1
    return counts


def _files() -> list[Path]:
    return sorted(path for base in CORPUS for path in (ROOT / base).rglob("*.py"))


def test_the_header_never_drops_a_decorator_line_the_parser_sees() -> None:
    counts = measure(_files())
    print(
        f"symbol_header vs ast: {dict(counts)} "
        f"none(decorated)={counts['decorated_none'] / max(counts['decorated'], 1):.2%} "
        f"none(plain)={counts['plain_none'] / max(counts['plain'], 1):.2%} "
        f"contaminated={(counts['decorated_contaminated'] + counts['plain_contaminated']) / sum([counts['decorated'], counts['plain']]):.2%}"
    )
    assert counts["fail_open"] == 0, dict(counts)
    # Not vacuous: real decorators, mostly read, and the refusal is the exception. The bounds are well above what
    # the live tree measures (about 0.3% refused, no contamination), so ordinary edits to the corpus do not trip them.
    assert counts["decorated"] >= 300 and counts["plain"] >= 1000, dict(counts)
    assert counts["decorated_none"] <= 0.02 * counts["decorated"], dict(counts)
    assert counts["plain_none"] <= 0.02 * counts["plain"], dict(counts)
    assert counts["plain_contaminated"] <= 0.01 * counts["plain"], dict(counts)


def test_the_oracle_does_see_a_scanner_that_drops_a_decorator(monkeypatch: pytest.MonkeyPatch) -> None:
    """The differential bites: a scanner that reads every definition as undecorated must light it up."""

    monkeypatch.setattr(sys.modules[__name__], "symbol_header", lambda lines, start_line: "")
    assert measure(_files(), stop_at_fail_open=True)["fail_open"] > 0
