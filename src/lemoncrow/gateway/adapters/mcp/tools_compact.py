"""Context compaction MCP handler with late-bound compressor composition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast

from pydantic import Field

from lemoncrow.gateway.adapters.mcp.framework import mcp_tool


@dataclass(frozen=True, slots=True)
class CompactHandlerHooks:
    compress_context: Callable[..., Any]


_HooksFactory = Callable[[], CompactHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_compact_handler_hooks(factory: _HooksFactory) -> None:
    """Install the process composition used by the compact handler."""
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> CompactHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("compact handler hooks are not configured")
    return factory()


@mcp_tool(name="compact")
def tool_compact(
    op: Annotated[
        str,
        Field(
            description=(
                '"compact" (default) = compress the run ledger into a compact session-state '
                'block. "consolidate" (T6) = distill recent findings + prune stale history, '
                "same compaction entrypoint — autonomous lever when context is heavy."
            )
        ),
    ] = "compact",
    session_id: Annotated[
        str | None,
        Field(description="Optional run-ledger session ID override. Usually omit."),
    ] = None,
) -> dict[str, Any]:
    """Compress the full run ledger into a compact session-state block."""
    normalized = (op or "compact").strip().lower()
    result = cast(dict[str, Any], _hooks().compress_context(session_id=session_id))
    if normalized == "consolidate":
        result["op"] = "consolidate"
    return result


__all__ = ["CompactHandlerHooks", "configure_compact_handler_hooks", "tool_compact"]
