"""Markdown structural extraction — heading-based symbols for the code index.

Uses ``markdown-it-py`` (CommonMark compliant, pure Python) to parse markdown
files into a token stream, then extracts heading hierarchy as symbols with
section content for FTS5 coverage.

Usage::

    symbols = extract_markdown_symbols(source)
    # Each symbol is a dict matching _ExtractedSymbol fields:
    #   name, qualified_name, kind (="heading"), signature,
    #   start_byte, end_byte, start_line, end_line, parent_symbol
"""

from __future__ import annotations

from typing import Any


def _line_offsets(text: str) -> list[int]:
    """Character-offset of the start of each line (0-indexed)."""
    offsets = [0]
    total = 0
    for line in text.splitlines(keepends=True):
        total += len(line.encode("utf-8"))
        offsets.append(total)
    return offsets


def extract_markdown_symbols(source: str) -> list[dict[str, Any]]:
    """Extract heading-based symbols from markdown source text.

    Returns a list of dicts matching ``_ExtractedSymbol`` field names so the
    caller can construct dataclass instances directly::

        from atelier.infra.code_intel.markdown import extract_markdown_symbols
        extracted = [
            _ExtractedSymbol(**s) for s in extract_markdown_symbols(source)
        ]

    Returns an empty list when no headings are found.
    """
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"maxNesting": 100})
    tokens = md.parse(source)
    offsets = _line_offsets(source)
    lines = source.splitlines()

    # --- collect heading positions ---
    heading_positions: list[tuple[int, int, str, str]] = []
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.map is not None:
            level = int(token.tag[1])  # "h1" -> 1, "h2" -> 2, …
            inline_tok = tokens[i + 1] if i + 1 < len(tokens) else None
            text = (inline_tok.content if inline_tok and inline_tok.type == "inline" else "").strip()
            heading_positions.append((token.map[0], level, text, token.tag))

    if not heading_positions:
        return []

    # --- build heading tree into symbols with section boundaries ---
    symbols: list[dict[str, Any]] = []
    heading_stack: list[tuple[int, str]] = []  # [(level, name), …]

    for idx, (heading_line, level, text, _tag) in enumerate(heading_positions):
        # 1-indexed line numbers
        start_line = heading_line + 1
        start_byte = offsets[heading_line] if heading_line < len(offsets) else 0

        # Section extends to the next heading (or EOF)
        if idx + 1 < len(heading_positions):
            next_line = heading_positions[idx + 1][0]
            end_byte = offsets[next_line] if next_line < len(offsets) else offsets[-1]
            end_line = next_line + 1  # exclusive 1-indexed line
        else:
            end_line = len(lines) + 1
            end_byte = offsets[-1] if offsets else 0

        # Manage heading stack for qualified_name (breadcrumb)
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, text))
        qualified_name = " > ".join(h[1] for h in heading_stack)

        parent_symbol: str | None = heading_stack[-2][1] if len(heading_stack) >= 2 else None
        signature = f"{'#' * level} {text}"

        symbols.append(
            {
                "name": text,
                "qualified_name": qualified_name,
                "kind": "heading",
                "signature": signature,
                "start_byte": start_byte,
                "end_byte": max(start_byte, end_byte),
                "start_line": start_line,
                "end_line": max(start_line, end_line),
                "parent_symbol": parent_symbol,
            }
        )

    return symbols


__all__ = ["extract_markdown_symbols"]
