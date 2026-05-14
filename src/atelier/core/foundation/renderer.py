"""Render ReasonBlocks and runtime artifacts to human-readable formats.

Markdown is the canonical reviewable format. The injection format
(used by `get_context`) is a compact text block tuned for LLM
context windows.
"""

from __future__ import annotations

from atelier.core.foundation.models import ReasonBlock, RubricResult


def render_block_markdown(block: ReasonBlock) -> str:
    """Render a ReasonBlock as a self-contained markdown document."""
    lines: list[str] = []
    lines.append(f"# {block.title}")
    lines.append("")
    lines.append(f"- **id:** `{block.id}`")
    lines.append(f"- **domain:** `{block.domain}`")
    lines.append(f"- **status:** `{block.status}`")
    if block.task_types:
        lines.append(f"- **task_types:** {', '.join(block.task_types)}")
    lines.append("")
    lines.append("## Situation")
    lines.append(block.situation)
    lines.append("")
    if block.triggers:
        lines.append("## Triggers")
        for t in block.triggers:
            lines.append(f"- {t}")
        lines.append("")
    if block.dead_ends:
        lines.append("## Dead ends")
        for d in block.dead_ends:
            lines.append(f"- {d}")
        lines.append("")
    lines.append("## Procedure")
    for i, step in enumerate(block.procedure, 1):
        lines.append(f"{i}. {step}")
    lines.append("")
    if block.verification:
        lines.append("## Verification")
        for v in block.verification:
            lines.append(f"- {v}")
        lines.append("")
    if block.failure_signals:
        lines.append("## Failure signals")
        for f in block.failure_signals:
            lines.append(f"- {f}")
        lines.append("")
    if block.when_not_to_apply:
        lines.append("## When not to apply")
        lines.append(block.when_not_to_apply)
        lines.append("")
    if block.file_patterns or block.tool_patterns:
        lines.append("## Scope")
        if block.file_patterns:
            lines.append(f"- file_patterns: {', '.join(block.file_patterns)}")
        if block.tool_patterns:
            lines.append(f"- tool_patterns: {', '.join(block.tool_patterns)}")
    return "\n".join(lines).rstrip() + "\n"


def render_block_for_agent(block: ReasonBlock) -> str:
    """Render one compact ReasonBlock for agent-context injection."""
    out: list[str] = []
    out.append(f"Procedure: {block.title}  [{block.id}]")
    out.append(f"Use when: {block.situation}")
    if block.dead_ends:
        out.append("Avoid:")
        for d in block.dead_ends:
            out.append(f"  - {d}")
    out.append("Do:")
    for step in block.procedure:
        out.append(f"  - {step}")
    if block.verification:
        out.append("Validate:")
        for v in block.verification:
            out.append(f"  - {v}")
    if block.when_not_to_apply:
        out.append(f"Skip when: {block.when_not_to_apply}")
    return "\n".join(out)


def render_context_for_agent(blocks: list[ReasonBlock], *, max_blocks: int = 5) -> str:
    """Compact context block for injection into agent prompts.

    Format is deliberately small: title, situation, dead-ends, procedure,
    verification. Skips usage stats and creation metadata to save tokens.
    """
    if not blocks:
        return "<context_procedures>\n(no relevant procedures found)\n</context_procedures>\n"

    out = ["<context_procedures>"]
    for block in blocks[:max_blocks]:
        out.append("")
        out.append(render_block_for_agent(block))
    out.append("</context_procedures>")
    return "\n".join(out) + "\n"


def render_rubric_result(result: RubricResult) -> str:
    lines = [f"Rubric {result.rubric_id}: {result.status.upper()}"]
    for o in result.outcomes:
        lines.append(f"  [{o.status}] {o.name}{(': ' + o.detail) if o.detail else ''}")
    for esc in result.escalations:
        lines.append(f"  ESCALATE: {esc}")
    return "\n".join(lines)
