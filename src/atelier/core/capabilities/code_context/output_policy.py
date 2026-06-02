"""Shared output policy profiles for code-context response shaping."""

from __future__ import annotations

from dataclasses import dataclass

TRUNCATION_MARKER = "... [truncated]"


@dataclass(frozen=True)
class OutputPolicy:
    include_code: bool
    include_docstring: bool
    include_snippet: bool
    include_edges: bool
    max_results: int
    max_related_symbols: int
    max_code_blocks: int
    max_code_block_chars: int
    max_total_tokens: int
    max_symbols_per_file: int
    container_outline_only: bool


SEARCH_COMPACT = OutputPolicy(
    include_code=False,
    include_docstring=False,
    include_snippet=False,
    include_edges=False,
    max_results=8,
    max_related_symbols=0,
    max_code_blocks=0,
    max_code_block_chars=0,
    max_total_tokens=1400,
    max_symbols_per_file=2,
    container_outline_only=True,
)

RELATION_COMPACT = OutputPolicy(
    include_code=False,
    include_docstring=False,
    include_snippet=False,
    include_edges=True,
    max_results=12,
    max_related_symbols=12,
    max_code_blocks=0,
    max_code_block_chars=0,
    max_total_tokens=1700,
    max_symbols_per_file=3,
    container_outline_only=True,
)

CONTEXT_COMPACT = OutputPolicy(
    include_code=True,
    include_docstring=False,
    include_snippet=True,
    include_edges=True,
    max_results=16,
    max_related_symbols=8,
    max_code_blocks=3,
    max_code_block_chars=750,
    max_total_tokens=5000,
    max_symbols_per_file=4,
    container_outline_only=True,
)

NODE_OUTLINE_COMPACT = OutputPolicy(
    include_code=False,
    include_docstring=False,
    include_snippet=False,
    include_edges=False,
    max_results=1,
    max_related_symbols=24,
    max_code_blocks=0,
    max_code_block_chars=0,
    max_total_tokens=2400,
    max_symbols_per_file=24,
    container_outline_only=True,
)

NODE_CODE_COMPACT = OutputPolicy(
    include_code=True,
    include_docstring=False,
    include_snippet=True,
    include_edges=False,
    max_results=1,
    max_related_symbols=0,
    max_code_blocks=1,
    max_code_block_chars=1800,
    max_total_tokens=1800,
    max_symbols_per_file=0,
    container_outline_only=False,
)

_POLICY_BY_OPERATION = {
    "search": SEARCH_COMPACT,
    "relation": RELATION_COMPACT,
    "context": CONTEXT_COMPACT,
    "outline": NODE_OUTLINE_COMPACT,
    "node": NODE_CODE_COMPACT,
}


def resolve_output_policy(operation: str) -> OutputPolicy:
    return _POLICY_BY_OPERATION.get(operation, SEARCH_COMPACT)


def hard_cap_chars(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return TRUNCATION_MARKER
    if len(text) <= max_chars:
        return text
    marker = TRUNCATION_MARKER
    if max_chars <= len(marker):
        return marker
    available_chars = max_chars - len(marker)
    cut = text[:available_chars]
    newline_floor = int(available_chars * 0.8)
    last_newline = cut.rfind("\n")
    if last_newline >= newline_floor:
        cut = cut[:last_newline]
    cut = cut.rstrip()
    if not cut:
        return marker
    return f"{cut}\n{marker}"


__all__ = [
    "CONTEXT_COMPACT",
    "NODE_CODE_COMPACT",
    "NODE_OUTLINE_COMPACT",
    "RELATION_COMPACT",
    "SEARCH_COMPACT",
    "TRUNCATION_MARKER",
    "OutputPolicy",
    "hard_cap_chars",
    "resolve_output_policy",
]
