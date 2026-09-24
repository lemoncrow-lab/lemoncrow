"""Model-facing tool-result presentation stages independent of MCP telemetry."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from lemoncrow_client.kit.present import payload_text

from lemoncrow.gateway.tools.output import (
    SPILL_CHAR_CAP_TOOLS,
    auto_compact_result_text,
    compact_result_text,
    effective_spill_tool,
    max_result_bytes,
    spill_oversized_result_text,
    spill_result_chars,
    trimmed_tokens_saved,
    truncate_result_text,
)


def assemble_response_text(result: Any, rendered_text: str | None, loop_note: str | None = None) -> str:
    """Choose rendered/raw/JSON text and append a loop note exactly once."""
    response_text = payload_text(result, rendered_text)
    if loop_note and loop_note not in response_text:
        response_text = f"{response_text}\n{loop_note}"
    return response_text


def apply_requested_output_format(
    args: dict[str, Any],
    result: Any,
    response_text: str,
) -> tuple[str, int]:
    """Apply explicit compact/json formatting, returning text and credited savings."""
    fmt = args.get("format")
    if not isinstance(fmt, str) or fmt.strip().lower() not in {"compact", "json"}:
        return response_text, 0
    original = response_text
    with contextlib.suppress(Exception):
        from lemoncrow.pro.capabilities.tool_supervision.output_format import apply_output_format

        response_text, _ = apply_output_format(
            fmt=fmt,
            result=result,
            rendered_text=response_text,
        )
        return response_text, trimmed_tokens_saved(len(original), len(response_text))
    return original, 0


def bound_tool_output(
    name: str,
    args: dict[str, Any],
    response_text: str,
    *,
    spill: Callable[..., str] = spill_oversized_result_text,
) -> tuple[str, int]:
    """Apply reversible compaction/spill/wire bounds and return trimming credit."""
    pre_trim_chars = len(response_text)
    response_text = auto_compact_result_text(response_text, name, args)
    effective_tool = effective_spill_tool(name, args)
    response_text = spill(
        response_text,
        effective_tool,
        args,
        spill_result_chars(effective_tool),
        unit="chars",
        tools=SPILL_CHAR_CAP_TOOLS,
    )
    response_text = compact_result_text(response_text, name)
    response_text = spill(response_text, name, args, max_result_bytes())
    response_text = truncate_result_text(response_text, max_result_bytes(), name)
    return response_text, trimmed_tokens_saved(pre_trim_chars, len(response_text))


__all__ = ["apply_requested_output_format", "assemble_response_text", "bound_tool_output"]
