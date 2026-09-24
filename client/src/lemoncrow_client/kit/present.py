"""How a tool's structured payload becomes the text a model reads.

The main package's MCP presentation and the thin client's executors both use
these, so a payload no renderer claims reads the same wherever it ran.
"""

from __future__ import annotations

import json
from typing import Any

STRIP_NULLS_MAX_DEPTH = 200


def strip_empty_values(value: Any, _depth: int = 0) -> Any:
    """Recursively remove ``None`` and empty-string values.

    Empty containers, zero, and ``False`` remain because they carry semantic
    information. Recursion is capped so adversarially deep tool data cannot
    turn normalization into a recursion failure.
    """
    if _depth >= STRIP_NULLS_MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: strip_empty_values(item, _depth + 1) for key, item in value.items() if item is not None and item != ""
        }
    if isinstance(value, list):
        return [strip_empty_values(item, _depth + 1) for item in value]
    return value


def payload_text(payload: Any, rendered: str | None) -> str:
    """The rendered text when a renderer produced some, else compact key-sorted JSON."""
    if rendered:
        return rendered
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


__all__ = ["STRIP_NULLS_MAX_DEPTH", "payload_text", "strip_empty_values"]
