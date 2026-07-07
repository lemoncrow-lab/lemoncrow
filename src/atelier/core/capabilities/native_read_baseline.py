"""Canonical \"native Claude Read\" baseline for savings accounting.

Both the live savings numbers embedded in MCP tool responses and the
``benchmarks/mcp_tools`` suite model the no-Atelier counterfactual as Claude
Code's built-in Read, which truncates at a fixed line cap. Defining that cap
and the truncation in one place keeps the runtime savings figure and the
benchmark baseline from silently diverging.

This is intentionally a worst-case baseline (a naive agent reading the whole
file up to the native cap), not a model of what a careful agent would read.
"""

from __future__ import annotations

CLAUDE_NATIVE_READ_LINE_LIMIT = 2000
"""Line cap of Claude Code's built-in Read tool."""


def claude_read_baseline_text(source: str, *, line_limit: int = CLAUDE_NATIVE_READ_LINE_LIMIT) -> str:
    """Return the text Claude Code's built-in Read would surface for *source*.

    Read truncates at ``line_limit`` lines, so a file longer than that only ever
    exposes its first ``line_limit`` lines to a naive agent. Shorter files are
    returned unchanged.
    """
    lines = source.splitlines()
    if len(lines) <= line_limit:
        return source
    return "\n".join(lines[:line_limit])
