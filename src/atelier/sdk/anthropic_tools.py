"""Raw Anthropic API integration for Atelier.

``make_atelier_tools()`` returns ``(tool_specs, dispatch)`` so you can
inject Atelier's watchdog + cost-tracking capabilities as Anthropic tool
calls without changing your agent architecture.

Usage::

    from anthropic import Anthropic
    from atelier.sdk import AtelierMiddleware

    mw = AtelierMiddleware(agent_name="bugfixer", task="Refactor the auth module")
    tool_specs, dispatch = mw.anthropic_tools()

    client = Anthropic()
    messages = [{"role": "user", "content": "Refactor the auth module"}]

    while True:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=[{
                "type": "text",
                "text": "You are a helpful assistant.",
                "cache_control": {"type": "ephemeral"},   # injected by Atelier
            }],
            tools=tool_specs,
            messages=messages,
        )
        dispatch(resp)  # records tokens, fires watchdogs
        if resp.stop_reason != "tool_use":
            break
        # ... handle tool calls ...
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from atelier.infra.runtime.run_ledger import RunLedger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# Atelier's own Anthropic-compatible tool spec for session telemetry
_ATELIER_TELEMETRY_TOOL: dict[str, Any] = {
    "name": "atelier_session_status",
    "description": (
        "Get current session status from Atelier: loop detection events, "
        "cache hit ratio, cost so far, and watchdog alerts.  "
        "Call this if you detect you may be looping or want to check session health."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def make_atelier_tools(
    ledger: RunLedger,
    *,
    include_telemetry_tool: bool = True,
) -> tuple[list[dict[str, Any]], Callable[[Any], None]]:
    """Return ``(tool_specs, dispatch)`` for raw Anthropic API integration.

    Args:
        ledger:                 Shared ``RunLedger`` for this session.
        include_telemetry_tool: If True, prepends the ``atelier_session_status``
                                tool spec so the LLM can query session health.

    Returns:
        A 2-tuple of:
        - ``tool_specs``: list of Anthropic ``ToolParam``-compatible dicts.
          Pass directly to ``messages.create(tools=...)``.
        - ``dispatch``: Callable that accepts an Anthropic ``Message`` response
          and records token usage + fires watchdog checks.
    """
    tool_specs: list[dict[str, Any]] = []
    if include_telemetry_tool:
        tool_specs.append(_ATELIER_TELEMETRY_TOOL)

    tool_call_counts: dict[str, int] = {}
    prior_prefix_hash: list[str] = [""]  # mutable container for closure

    def dispatch(response: Any) -> None:
        """Record token usage from an Anthropic API response and fire watchdogs.

        Args:
            response: An ``anthropic.types.Message`` instance (or any object with
                      ``.usage``, ``.model``, ``.content``, and ``.stop_reason``).
        """
        # Extract usage from Anthropic Message
        usage = getattr(response, "usage", None)
        input_tokens: int = 0
        output_tokens: int = 0
        cache_read_tokens: int = 0

        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or 0
            # Anthropic extended usage fields
            cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0

        model = getattr(response, "model", "unknown") or "unknown"
        _stop_reason = getattr(response, "stop_reason", "")

        # Compute prefix hash from ledger task context
        prefix_hash = ""
        prefix_invalidated_reason = ""
        try:
            from atelier.core.capabilities.prefix_cache.planner import PrefixCachePlanner
            from atelier.core.capabilities.prompt_compilation.models import (
                BlockKind,
                PromptBlock,
                Stability,
            )

            if ledger.task:
                task_block = PromptBlock(
                    id="task",
                    kind=BlockKind.USER_TASK,
                    stability=Stability.TURN,
                    content=ledger.task,
                )
                planner = PrefixCachePlanner()
                plan = planner.plan_with_history([task_block], prior_prefix_hash[0] or None)
                prefix_hash = plan.prefix_hash
                prefix_invalidated_reason = plan.invalidated_reason
                prior_prefix_hash[0] = prefix_hash
        except Exception:
            logging.exception("Recovered from broad exception handler")
            logger.debug("anthropic prefix-cache capture failed", exc_info=True)

        ledger.record_call(
            operation="messages.create",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            stable_prefix_hash=prefix_hash,
            prefix_invalidated_reason=prefix_invalidated_reason,
        )

        # Loop detection: scan tool_use blocks in response
        content = getattr(response, "content", []) or []
        for block in content:
            if getattr(block, "type", "") == "tool_use":
                tool_name = getattr(block, "name", "unknown")

                # Handle Atelier's own telemetry tool call
                if tool_name == "atelier_session_status":
                    _handle_telemetry_call(ledger)
                    continue

                tool_call_counts[tool_name] = tool_call_counts.get(tool_name, 0) + 1
                count = tool_call_counts[tool_name]
                if count >= 3:
                    ledger.record(
                        "watchdog_alert",
                        f"loop_detection: tool '{tool_name}' called {count} times",
                        {
                            "event_type": "REPEATED_TOOL_CALL",
                            "tool": tool_name,
                            "count": count,
                            "hint": f"Agent is looping on '{tool_name}'. Consider redirecting.",
                        },
                    )

        # Budget watchdog: warn if token count is high
        total_tokens = ledger.token_count
        if total_tokens > 100_000:
            ledger.record(
                "watchdog_alert",
                f"budget_watchdog: session token count {total_tokens:,} exceeds 100k",
                {
                    "event_type": "BUDGET_EXHAUSTION",
                    "token_count": total_tokens,
                    "hint": "Consider compressing context or starting a new session.",
                },
            )

    return tool_specs, dispatch


def _handle_telemetry_call(ledger: RunLedger) -> dict[str, Any]:
    """Produce a telemetry response for the atelier_session_status tool."""
    call_events = [e for e in ledger.events if e.payload.get("kind") == "llm_call"]
    watchdog_events = [e for e in ledger.events if e.kind == "watchdog_alert"]
    total_cache_read = sum(int(e.payload.get("cache_read_tokens", 0)) for e in call_events)
    total_input = sum(int(e.payload.get("input_tokens", 0)) for e in call_events)
    hits = sum(1 for e in call_events[1:] if int(e.payload.get("cache_read_tokens", 0)) > 0)
    eligible = max(len(call_events) - 1, 1)
    return {
        "turns": len(call_events),
        "cache_hit_ratio": round(hits / eligible, 4) if call_events else 0.0,
        "cache_read_tokens_saved": total_cache_read,
        "total_input_tokens": total_input,
        "watchdog_alerts": len(watchdog_events),
        "loop_detected": any(
            e.payload.get("event_type") in {"REPEATED_TOOL_CALL", "REPEATED_COMMAND"} for e in watchdog_events
        ),
    }
