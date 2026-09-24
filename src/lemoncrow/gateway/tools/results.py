"""Transport-independent normalization for structured tool results."""

from __future__ import annotations

from typing import Any, cast

from lemoncrow_client.kit.present import strip_empty_values


def clean_tool_result(result: dict[str, Any], tool_name: str) -> dict[str, Any]:
    """Apply final structured-result normalization before serialization."""
    _ = tool_name  # Reserved for tool-specific normalization without changing callers.
    return cast(dict[str, Any], strip_empty_values(result))


__all__ = ["clean_tool_result"]
