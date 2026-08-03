"""Parsing helpers for the Graft retrieval-eval provider.

Graft's MCP server intentionally returns compact human-readable context packs
rather than structured JSON.  The benchmark only needs the ranked file surface,
so these parsers consume the documented output shapes without scraping source
blocks or incidental paths from snippets.
"""

from __future__ import annotations

import re

_LEXICAL_HIT_RE = re.compile(r"^\d+\.\s")
_SPAN_SUFFIX_RE = re.compile(r":L\d+-L\d+$")
_STRUCTURAL_HIT_RE = re.compile(r"^-\s+.*?\s{2}(.+?):L\d+-L\d+\s{2}\([^)]*\)\s*$")
_GREP_SYMBOL_RE = re.compile(r"^.* · [^·]+ · (.+):L\d+-L\d+ · \d+ in-edges\s*$")
_GREP_MODULE_RE = re.compile(r"^(.+) \(module level\) · \d+ in-edges\s*$")


def _dedupe(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in paths:
        path = raw.strip().replace("\\", "/")
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def parse_find_code_paths(text: str) -> list[str]:
    """Return ranked file paths from ``graft_find_code`` markdown.

    Lexical packs render each numbered hit followed immediately by its pointer.
    Structural packs render one bullet containing the pointer inline.  Reading
    only those positions avoids treating paths inside inlined source or snippets
    as additional ranked results.
    """

    lines = text.splitlines()
    paths: list[str] = []
    for index, line in enumerate(lines):
        structural = _STRUCTURAL_HIT_RE.match(line)
        if structural:
            paths.append(structural.group(1))
            continue
        if not _LEXICAL_HIT_RE.match(line):
            continue
        pointer_index = index + 1
        while pointer_index < len(lines) and not lines[pointer_index].strip():
            pointer_index += 1
        if pointer_index >= len(lines):
            continue
        pointer = lines[pointer_index].strip()
        # A lexical pointer is either ``path:Lx-Ly`` or a whole-file path.  It
        # never contains spaces; snippets/source lines do, which gives us a
        # conservative guard if the formatter ever changes.
        if not pointer or any(ch.isspace() for ch in pointer):
            continue
        paths.append(_SPAN_SUFFIX_RE.sub("", pointer))
    return _dedupe(paths)


def parse_find_all_paths(text: str) -> list[str]:
    """Return ranked file paths from ``graft_find_all`` group headers."""

    paths: list[str] = []
    for line in text.splitlines():
        symbol = _GREP_SYMBOL_RE.match(line)
        if symbol:
            paths.append(symbol.group(1))
            continue
        module = _GREP_MODULE_RE.match(line)
        if module:
            paths.append(module.group(1))
    return _dedupe(paths)
