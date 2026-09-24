"""Deterministic rare-tool broker independent of MCP transport framing."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, cast

from lemoncrow.gateway.tools.errors import ToolArgumentError as _ToolArgumentError
from lemoncrow.gateway.tools.registry import registered_tools
from lemoncrow.gateway.tools.state import tool_call_rendered_text
from lemoncrow.gateway.tools.surface import CORE_MCP_TOOLS, tool_description, tool_visible_to_llm

BROKER_HIDDEN_CALLABLE = frozenset({"statusline_segment"})


def render_tool_broker_search(result: dict[str, Any]) -> str | None:
    matches = result.get("matches")
    if not isinstance(matches, list):
        return None
    if not matches:
        return "tools\n- no matches"
    lines = ["tools"]
    for match in matches:
        if not isinstance(match, dict):
            continue
        name = str(match.get("name") or "?").strip() or "?"
        description = " ".join(str(match.get("description") or "").split())
        lines.append(f"→ {name}" + (f" · {description}" if description else ""))
    return "\n".join(lines)


def invoke_tool_broker(
    args: dict[str, Any],
    *,
    renderer: Callable[[str, Any], str | None],
    visibility: Callable[[str, dict[str, Any]], bool] | None = None,
    description: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any] | Any:
    """Search or invoke rare registered tools without exposing them globally."""
    visible = visibility or (lambda name, _spec: tool_visible_to_llm(name))
    describe = description or tool_description
    tools = registered_tools()
    action = str(args.get("action") or "")

    if action == "search":
        normalized = " ".join(str(args.get("query") or "").lower().split())
        terms = tuple(term for term in re.split(r"\W+", normalized) if term)
        matches: list[tuple[int, str, str]] = []
        for tool_name, spec in tools.items():
            if tool_name in CORE_MCP_TOOLS:
                continue
            if not visible(tool_name, spec):
                continue
            rendered_description = describe(spec)
            haystack = f"{tool_name} {rendered_description}".lower()
            score = (100 if normalized == tool_name.lower() else 0) + (
                40 if normalized and normalized in haystack else 0
            )
            score += 5 * sum(1 for term in terms if term in haystack)
            if not normalized or score > 0:
                matches.append((score, tool_name, rendered_description[:240]))
        matches.sort(key=lambda item: (-item[0], item[1]))
        result = {"matches": [{"name": tool_name, "description": desc} for _, tool_name, desc in matches[:12]]}
        tool_call_rendered_text.value = render_tool_broker_search(result)
        return result

    if action == "call":
        target = str(args.get("name") or "").strip()
        if not target:
            raise _ToolArgumentError("tool call requires an exact name")
        if target in CORE_MCP_TOOLS:
            raise _ToolArgumentError(f"{target!r} is already exposed; call it directly")
        call_spec = tools.get(target)
        if call_spec is None or not (visible(target, call_spec) or target in BROKER_HIDDEN_CALLABLE):
            raise _ToolArgumentError(f"unknown or unavailable tool: {target}")
        arguments = args.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _ToolArgumentError("tool arguments must be an object")
        handler = cast(Callable[[dict[str, Any]], Any], call_spec["handler"])
        result = handler(arguments)
        rendered = renderer(target, result)
        if rendered:
            tool_call_rendered_text.value = rendered
        return result

    raise _ToolArgumentError(f"unknown broker action: {action}")


__all__ = ["BROKER_HIDDEN_CALLABLE", "invoke_tool_broker", "render_tool_broker_search"]
