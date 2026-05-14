#!/usr/bin/env python3
"""Audit and auto-fix silent exception handlers in Python source files.

Finds every handler whose body is only `pass` (or `...`) and rewrites it
to log at WARNING level so nothing is silently swallowed.

If the handler has no `as <name>` clause the script also adds one so the
exception object is available to the logger.

Usage:
    # Report only — make no changes
    python scripts/fix_silent_exceptions.py --dry-run src/

    # Apply fixes
    python scripts/fix_silent_exceptions.py src/

    # Single file
    python scripts/fix_silent_exceptions.py src/atelier/core/service/api.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# ── logger-name detection ────────────────────────────────────────────────────

logger_PATTERNS = [
    r"\b(logger)\s*=\s*logging\.getLogger",
    r"\b(logger)\s*=\s*logging\.getLogger",
    r"\b(log)\s*=\s*logging\.getLogger",
    r"\b(LOG)\s*=\s*logging\.getLogger",
]


def _find_logger_name(source: str) -> str:
    for pat in logger_PATTERNS:
        m = re.search(pat, source)
        if m:
            return m.group(1)
    return "logger"


# ── body classification ───────────────────────────────────────────────────────


def _is_silent(handler: ast.ExceptHandler) -> bool:
    """True when the handler body contains only pass / ellipsis (Expr(...))."""
    for stmt in handler.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
            continue
        return False
    return True


# ── except-clause rewriter ───────────────────────────────────────────────────

_EXCEPT_RE = re.compile(
    r"^(\s*)except\b([^:]*?)(\s*as\s+\w+)?(\s*):\s*$",
)


def _add_as_clause(line: str, varname: str) -> str:
    """Insert `as <varname>` into an except line that is missing it."""
    m = _EXCEPT_RE.match(line)
    if not m:
        return line  # can't parse — leave untouched
    indent, exc_types, as_clause, trailing, *_ = m.groups()
    if as_clause:
        return line  # already has one
    return f"{indent}except{exc_types} as {varname}{trailing}:\n"


# ── per-file fix ─────────────────────────────────────────────────────────────


def _fix_source(source: str, path: Path) -> tuple[str, list[str]]:
    """Return (new_source, list_of_change_descriptions).

    new_source == source when nothing changed.
    """
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return source, [f"  SKIP (syntax error): {exc}"]

    logger_name = _find_logger_name(source)
    lines = source.splitlines(keepends=True)
    changes: list[str] = []

    # Collect patches as (line_index_0based, new_text) sorted descending so
    # we can apply them without invalidating earlier indices.
    patches: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _is_silent(node):
            continue

        exc_varname = node.name or "exc"
        handler_lineno = node.lineno - 1  # 0-indexed line of `except ...:`

        # Determine the pass/ellipsis line(s) to replace
        pass_line_indices = [s.lineno - 1 for s in node.body]
        first_pass_idx = pass_line_indices[0]

        # Indentation of the pass statement
        pass_line = lines[first_pass_idx]
        indent_str = " " * (len(pass_line) - len(pass_line.lstrip()))

        # Build replacement for all body lines (collapse into one log call)
        if node.name:
            log_line = (
                f"{indent_str}{logger_name}.warning(\n"
                f'{indent_str}    "Suppressed %s at {path.name}:{node.lineno}: %s",\n'
                f"{indent_str}    type({exc_varname}).__name__,\n"
                f"{indent_str}    {exc_varname},\n"
                f"{indent_str}    exc_info=True,\n"
                f"{indent_str})\n"
            )
        else:
            # No `as` clause yet — add one to the except line and use it
            exc_varname = "exc"
            log_line = (
                f"{indent_str}{logger_name}.warning(\n"
                f'{indent_str}    "Suppressed exception at {path.name}:{node.lineno}",\n'
                f"{indent_str}    exc_info=True,\n"
                f"{indent_str})\n"
            )
            # Also patch the except line itself to add `as exc`
            old_except = lines[handler_lineno]
            new_except = _add_as_clause(old_except, "exc")
            if new_except != old_except:
                patches.append((handler_lineno, new_except))

        # Replace all body lines with the single log call on the first,
        # and blank out the rest.
        patches.append((first_pass_idx, log_line))
        for idx in pass_line_indices[1:]:
            patches.append((idx, ""))

        changes.append(f"  {path}:{node.lineno}: added {logger_name}.warning() (exc var: {exc_varname!r})")

    if not patches:
        return source, []

    # Apply patches — sort descending by line index so earlier lines stay valid
    for line_idx, new_text in sorted(set(patches), key=lambda p: -p[0]):
        lines[line_idx] = new_text

    return "".join(lines), changes


# ── CLI ──────────────────────────────────────────────────────────────────────


def _iter_py_files(roots: list[Path]) -> list[Path]:
    result: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            result.append(root)
        elif root.is_dir():
            result.extend(sorted(root.rglob("*.py")))
    return result


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    dry_run = "--dry-run" in args
    paths_raw = [a for a in args if not a.startswith("-")]

    if not paths_raw:
        print("Usage: fix_silent_exceptions.py [--dry-run] <path|dir> ...")
        return 1

    roots = [Path(p) for p in paths_raw]
    py_files = _iter_py_files(roots)

    total_fixed = 0
    for path in py_files:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue

        new_source, changes = _fix_source(source, path)
        if not changes:
            continue

        total_fixed += len(changes)
        print(f"\n{path}  ({len(changes)} handler(s))")
        for c in changes:
            print(c)

        if not dry_run and new_source != source:
            path.write_text(new_source, encoding="utf-8")
            print("  → written")
        elif dry_run:
            print("  → dry-run, not written")

    print(f"\n{'Would fix' if dry_run else 'Fixed'} {total_fixed} silent handler(s) across {len(py_files)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
