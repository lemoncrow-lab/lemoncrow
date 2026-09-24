"""Transport-neutral helpers for extracting tool-call savings metadata."""

from __future__ import annotations

from typing import Any

from lemoncrow.gateway.tools.state import tool_call_tokens_saved


def coerce_saved_tokens(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return int(max(0.0, value))
    if isinstance(value, dict):
        return sum(
            int(max(0.0, float(item_value)))
            for item_value in value.values()
            if isinstance(item_value, (int, float)) and not isinstance(item_value, bool)
        )
    return 0


def extract_compact_output_tokens_saved(result: dict[str, Any]) -> int:
    return coerce_saved_tokens(result.get("tokens_saved_vs_naive"))


def extract_tokens_saved(result: dict[str, Any]) -> int:
    direct = coerce_saved_tokens(result.get("tokens_saved"))
    if direct > 0:
        return direct
    thread_local = getattr(tool_call_tokens_saved, "value", 0)
    if thread_local > 0:
        return thread_local
    return extract_compact_output_tokens_saved(result)


__all__ = ["coerce_saved_tokens", "extract_compact_output_tokens_saved", "extract_tokens_saved"]
