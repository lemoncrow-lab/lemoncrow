"""Tool-call normalization and lookup independent of MCP execution/finalization."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import Any

from lemoncrow.gateway.tools.registry import registered_tools
from lemoncrow.gateway.tools.surface import mcp_tool_profile


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    """Normalized call metadata consumed by the execution pipeline."""

    name: str
    args: dict[str, Any]
    spec: dict[str, Any]
    remote_routed: bool


REMOTE_TOOLS = frozenset({"context", "memory", "rescue", "trace", "verify"})


class ToolCallPreparationError(ValueError):
    """Protocol-level error found before a handler is invoked."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _normalized_arguments(params: Any) -> dict[str, Any]:
    args = params.get("arguments") or {}
    # Some MCP clients send the entire arguments object as a JSON string.
    # Recover it before typed/mypyc handlers see the call boundary.
    if isinstance(args, str):
        with contextlib.suppress(json.JSONDecodeError, ValueError, RecursionError):
            args = json.loads(args)
    return args if isinstance(args, dict) else {}


def _validate_memory_arguments(args: dict[str, Any], spec: dict[str, Any]) -> None:
    properties = spec.get("inputSchema", {}).get("properties", {})
    allowed_args = set(properties) if isinstance(properties, dict) else set()
    # Declared compatibility aliases (for example agent_id -> agent) remain valid;
    # the memory handler performs the actual remap before its own validation.
    allowed_args |= set(spec.get("param_aliases", {}) or {})
    unknown_args = sorted(set(args) - allowed_args)
    if unknown_args:
        raise ToolCallPreparationError(
            -32602,
            f"unknown arguments for memory tool: {', '.join(unknown_args)}",
        )


def prepare_tool_call(
    params: Any,
    *,
    broker_spec: dict[str, Any],
    remote_tools: frozenset[str],
) -> PreparedToolCall:
    """Normalize one ``tools/call`` payload and resolve its handler specification."""
    name = params.get("name") or ""
    if name == "run":
        name = "bash"
    args = _normalized_arguments(params)

    tools = registered_tools()
    spec = broker_spec if name == "tool" and mcp_tool_profile() == "core" else tools.get(name)
    if spec is None:
        raise ToolCallPreparationError(-32601, f"unknown tool: {name}")
    if name == "memory":
        _validate_memory_arguments(args, spec)

    remote_routed = name in remote_tools
    # Symbol context is served by the local code-intel engine even when the
    # broader context tool is service-routable.
    if name == "context" and args.get("mode") == "symbols":
        remote_routed = False

    return PreparedToolCall(
        name=name,
        args=args,
        spec=spec,
        remote_routed=remote_routed,
    )


__all__ = ["REMOTE_TOOLS", "PreparedToolCall", "ToolCallPreparationError", "prepare_tool_call"]
