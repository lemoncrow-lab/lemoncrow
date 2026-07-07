"""AtelierMiddleware — unified drop-in middleware entry point.

Wraps Atelier's production runtime (watchdogs, loop detection, context
compression, cost tracking, prefix-cache planning) behind three integration
surfaces:

- ``.langchain()``       → LangChain ``BaseCallbackHandler``
- ``.openai_hooks()``    → OpenAI Agents SDK ``AgentHooks``
- ``.anthropic_tools()`` → ``(tool_specs, dispatch)`` for raw Anthropic API
- ``.gemini_adk()``      → Gemini ADK-style lifecycle hooks

All three surfaces share a single ``RunLedger`` so cost metrics, loop
detection events, and prefix-cache diagnostics are unified across the session.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from atelier.infra.runtime.run_ledger import RunLedger

if TYPE_CHECKING:
    from collections.abc import Callable

    from atelier.sdk.gemini_adk import GeminiADKMiddleware
    from atelier.sdk.langchain_middleware import LangChainMiddleware
    from atelier.sdk.openai_hooks import OpenAIAgentsHooks


class AtelierMiddleware:
    """Unified entry point for Atelier SDK middleware.

    Args:
        agent_name: Identifier for this agent/session (used in ledger + logging).
        task:       Human-readable description of the agent's current task.
        mode:       ``"shadow"`` (observe only) | ``"suggest"`` | ``"enforce"``.
        session_id: Optional stable session ID; auto-generated if omitted.
    """

    def __init__(
        self,
        *,
        agent_name: str = "agent",
        task: str = "",
        mode: str = "shadow",
        session_id: str | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.task = task
        self.mode = mode
        self._ledger = RunLedger(
            session_id=session_id,
            agent=agent_name,
            task=task,
        )
        self._lock = threading.Lock()
        self._start_time = time.monotonic()

    # ---------------------------------------------------------------------- #
    # Integration surfaces                                                     #
    # ---------------------------------------------------------------------- #

    def langchain(self) -> LangChainMiddleware:
        """Return a LangChain ``BaseCallbackHandler`` backed by this middleware."""
        from atelier.sdk.langchain_middleware import LangChainMiddleware

        return LangChainMiddleware(self._ledger, mode=self.mode)

    def openai_hooks(self) -> OpenAIAgentsHooks:
        """Return an OpenAI Agents SDK ``AgentHooks`` backed by this middleware."""
        from atelier.sdk.openai_hooks import OpenAIAgentsHooks

        return OpenAIAgentsHooks(self._ledger, mode=self.mode)

    def anthropic_tools(
        self,
        *,
        include_telemetry_tool: bool = True,
    ) -> tuple[list[dict[str, Any]], Callable[[Any], None]]:
        """Return ``(tool_specs, dispatch)`` for raw Anthropic API integration.

        Pass ``tool_specs`` to ``Anthropic().messages.create(tools=tool_specs, ...)``
        and call ``dispatch(response)`` after each API response to record
        token usage and fire watchdog checks.
        """
        from atelier.sdk.anthropic_tools import make_atelier_tools

        return make_atelier_tools(self._ledger, include_telemetry_tool=include_telemetry_tool)

    def gemini_adk(self) -> GeminiADKMiddleware:
        """Return Gemini ADK-style hooks backed by this middleware."""
        from atelier.sdk.gemini_adk import GeminiADKMiddleware

        return GeminiADKMiddleware(self._ledger, mode=self.mode)

    # ---------------------------------------------------------------------- #
    # Context manager support                                                  #
    # ---------------------------------------------------------------------- #

    def __enter__(self) -> AtelierMiddleware:
        return self

    def __exit__(self, *_: Any) -> None:
        self._ledger.close()

    # ---------------------------------------------------------------------- #
    # Diagnostics                                                              #
    # ---------------------------------------------------------------------- #

    def cost_summary(self) -> dict[str, Any]:
        """Return current session cost and token metrics from the ledger."""
        call_events = [e for e in self._ledger.events if e.payload.get("kind") == "llm_call"]
        total_input = sum(int(e.payload.get("input_tokens", 0)) for e in call_events)
        total_output = sum(int(e.payload.get("output_tokens", 0)) for e in call_events)
        total_cache_read = sum(int(e.payload.get("cache_read_tokens", 0)) for e in call_events)
        total_cost = sum(float(e.payload.get("cost_usd") or 0.0) for e in call_events)
        hits = sum(1 for e in call_events[1:] if int(e.payload.get("cache_read_tokens", 0)) > 0)
        eligible = max(len(call_events) - 1, 1)
        return {
            "turns": len(call_events),
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_hit_ratio": round(hits / eligible, 4) if call_events else 0.0,
            "cost_usd": round(total_cost, 6),
            "elapsed_s": round(time.monotonic() - self._start_time, 2),
        }

    def watchdog_events(self) -> list[dict[str, Any]]:
        """Return all watchdog alert events recorded in this session."""
        return [
            {"kind": e.kind, "summary": e.summary, **e.payload}
            for e in self._ledger.events
            if e.kind == "watchdog_alert"
        ]

    def loop_detected(self) -> bool:
        """True if a REPEATED_TOOL_CALL or REPEATED_COMMAND watchdog fired."""
        for e in self._ledger.events:
            if e.kind == "watchdog_alert" and e.payload.get("event_type") in {
                "REPEATED_TOOL_CALL",
                "REPEATED_COMMAND",
                "KNOWN_DEAD_END",
            }:
                return True
        return False
