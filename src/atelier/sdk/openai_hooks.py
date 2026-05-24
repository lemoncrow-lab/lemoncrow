"""OpenAI Agents SDK hooks for Atelier.

Implements the ``AgentHooks`` interface from the OpenAI Agents SDK
(``agents.lifecycle.AgentHooks``) to inject Atelier's watchdogs,
loop detection, cost tracking, and prefix-cache diagnostics into
OpenAI-hosted agent runs.

Usage::

    from atelier.sdk import AtelierMiddleware

    mw = AtelierMiddleware(agent_name="bugfixer", task="Refactor the auth module")

    agent = Agent(
        name="bugfixer",
        instructions="You refactor code.",
        tools=your_tools,
    )

    with mw:
        Runner.run_sync(agent, input="Refactor the auth module", hooks=mw.openai_hooks())
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from atelier.infra.runtime.run_ledger import RunLedger

if TYPE_CHECKING:
    pass


class OpenAIAgentsHooks:
    """``AgentHooks``-compatible Atelier middleware for the OpenAI Agents SDK.

    Hook methods follow the ``agents.lifecycle.AgentHooks`` protocol.
    They are called by the OpenAI Agents SDK ``Runner`` at each lifecycle
    boundary.

    This class does NOT import from ``agents`` at module level so Atelier
    does not hard-require the OpenAI Agents SDK as a dependency.
    """

    def __init__(self, ledger: RunLedger, *, mode: str = "shadow") -> None:
        self._ledger = ledger
        self.mode = mode
        self._tool_call_counts: dict[str, int] = {}
        self._agent_start: float = 0.0
        self._turn_input_tokens: int = 0
        self._turn_output_tokens: int = 0

    # ---------------------------------------------------------------------- #
    # AgentHooks protocol                                                      #
    # ---------------------------------------------------------------------- #

    async def on_agent_start(self, context: Any, agent: Any) -> None:
        """Called when the agent run begins."""
        self._agent_start = time.monotonic()
        self._ledger.record(
            "agent_message",
            f"agent_start: {getattr(agent, 'name', 'agent')}",
            {"event": "start", "agent": getattr(agent, "name", "agent")},
        )

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        """Called when the agent run ends."""
        elapsed = round(time.monotonic() - self._agent_start, 2)
        self._ledger.record(
            "agent_message",
            f"agent_end: {getattr(agent, 'name', 'agent')} in {elapsed}s",
            {
                "event": "end",
                "agent": getattr(agent, "name", "agent"),
                "elapsed_s": elapsed,
                "output_type": type(output).__name__,
            },
        )

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        """Called before each tool call. Fires loop detection."""
        tool_name = getattr(tool, "name", str(tool))
        self._tool_call_counts[tool_name] = self._tool_call_counts.get(tool_name, 0) + 1
        count = self._tool_call_counts[tool_name]

        self._ledger.record_tool_call(tool_name, {"count": count})

        if count >= 3:
            self._ledger.record(
                "watchdog_alert",
                f"loop_detection: tool '{tool_name}' called {count} times",
                {
                    "event_type": "REPEATED_TOOL_CALL",
                    "tool": tool_name,
                    "count": count,
                    "hint": f"Agent is looping on '{tool_name}'. Consider redirecting or adding a guard.",
                },
            )

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        """Called after each tool call."""
        tool_name = getattr(tool, "name", str(tool))
        self._ledger.record(
            "tool_result",
            f"tool_end: {tool_name}",
            {"tool": tool_name, "result_type": type(result).__name__},
        )

    async def on_handoff(self, context: Any, from_agent: Any, to_agent: Any) -> None:
        """Called on agent handoff."""
        self._ledger.record(
            "agent_message",
            f"handoff: {getattr(from_agent, 'name', '?')} → {getattr(to_agent, 'name', '?')}",
            {
                "event": "handoff",
                "from": getattr(from_agent, "name", "?"),
                "to": getattr(to_agent, "name", "?"),
            },
        )

    # ---------------------------------------------------------------------- #
    # Sync wrappers (RunSync compatibility)                                    #
    # ---------------------------------------------------------------------- #

    def on_agent_start_sync(self, context: Any, agent: Any) -> None:
        self._agent_start = time.monotonic()
        self._ledger.record(
            "agent_message",
            f"agent_start: {getattr(agent, 'name', 'agent')}",
            {"event": "start", "agent": getattr(agent, "name", "agent")},
        )

    def on_agent_end_sync(self, context: Any, agent: Any, output: Any) -> None:
        elapsed = round(time.monotonic() - self._agent_start, 2)
        self._ledger.record(
            "agent_message",
            f"agent_end: {getattr(agent, 'name', 'agent')} in {elapsed}s",
            {"event": "end", "elapsed_s": elapsed},
        )

    def on_tool_start_sync(self, context: Any, agent: Any, tool: Any) -> None:
        tool_name = getattr(tool, "name", str(tool))
        self._tool_call_counts[tool_name] = self._tool_call_counts.get(tool_name, 0) + 1
        count = self._tool_call_counts[tool_name]
        self._ledger.record_tool_call(tool_name, {"count": count})
        if count >= 3:
            self._ledger.record(
                "watchdog_alert",
                f"loop_detection: tool '{tool_name}' called {count} times",
                {"event_type": "REPEATED_TOOL_CALL", "tool": tool_name, "count": count},
            )

    def on_tool_end_sync(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        pass

    # ---------------------------------------------------------------------- #
    # Context manager support                                                  #
    # ---------------------------------------------------------------------- #

    def __enter__(self) -> OpenAIAgentsHooks:
        return self

    def __exit__(self, *_: Any) -> None:
        self._ledger.close()
