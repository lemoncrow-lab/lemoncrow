"""System prompt templates per benchmark mode.

Kept tiny on purpose — the prompts themselves are part of the benchmark
contract and changing them changes the published numbers.
"""

from __future__ import annotations

from benchmarks.swe.modes import Mode, get_spec

_BASE = (
    "You are a software engineer fixing a bug in a real GitHub repository. "
    "Read the problem statement, propose a minimal patch, and emit the patch "
    "as a unified diff (``--- a/...`` / ``+++ b/...``). Do not modify tests."
)

_TOOLS_HINT = (
    "\n\nThe Atelier MCP server is available. You may call tools such as "
    "`reasoning`, `lint`, "
    "`rescue`, `verify`, and "
    "`trace`. Use them when they help."
)

_FORCED_HINT = (
    "\n\nYou MUST follow this workflow:\n"
    "  1. Call `reasoning` before drafting your plan.\n"
    "  2. Call `lint` before producing the patch.\n"
    "  3. If you fail twice on the same step, call `rescue`.\n"
    "  4. Call `verify` before submitting.\n"
    "  5. Call `trace` once you finish."
)


def system_prompt(mode: Mode) -> str:
    spec = get_spec(mode)
    parts = [_BASE]
    if spec.mcp_available and not spec.forced_steps:
        parts.append(_TOOLS_HINT)
    if spec.forced_steps:
        parts.append(_TOOLS_HINT)
        parts.append(_FORCED_HINT)
    return "".join(parts)


def task_prompt(problem_statement: str, hints: str = "") -> str:
    block = f"# Problem\n{problem_statement.strip()}\n"
    if hints.strip():
        block += f"\n# Hints\n{hints.strip()}\n"
    block += "\n# Output\nReturn only the unified diff."
    return block
