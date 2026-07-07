"""LangChain ``BaseCallbackHandler`` middleware for Atelier.

Intercepts LLM calls to:
- Inject ``cache_control: {"type": "ephemeral"}`` on the system prompt
  and tool-schema messages (provider-side KV-cache pinning)
- Record each call in RunLedger with real cache_read_tokens
- Fire watchdog loop-detection on tool calls
- Track prefix hash changes across turns

Usage::

    from atelier.sdk import AtelierMiddleware

    mw = AtelierMiddleware(agent_name="bugfixer", task="Refactor the auth module")

    agent = create_agent(
        model=ChatAnthropic(model="claude-sonnet-4-5"),
        tools=your_tools,
        callbacks=[mw.langchain()],
    )
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from atelier.infra.runtime.run_ledger import RunLedger

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _inject_cache_control(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inject ``cache_control: {"type": "ephemeral"}`` on eligible message parts.

    Eligible: system messages and the last human message (which typically
    carries the compiled tool schema / instructions block).  This pins the
    stable prefix in Anthropic's prompt KV cache.
    """
    out: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            if isinstance(content, str):
                out.append(
                    {
                        **msg,
                        "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}],
                    }
                )
            else:
                out.append(msg)
            continue

        # Pin the last tool-schema-bearing human turn (index 0 in most agents)
        if role == "human" and i == 0 and isinstance(content, str):
            out.append(
                {
                    **msg,
                    "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}],
                }
            )
            continue

        out.append(msg)
    return out


class LangChainMiddleware:
    """LangChain ``BaseCallbackHandler``-compatible Atelier middleware.

    Designed as a drop-in callback for LangChain / LangGraph agents.
    Imports from ``langchain_core`` are deferred so Atelier does not
    hard-require LangChain as a dependency.
    """

    def __init__(self, ledger: RunLedger, *, mode: str = "shadow") -> None:
        self._ledger = ledger
        self.mode = mode
        self._call_start: dict[str, float] = {}
        self._prior_prefix_hash: str = ""
        self._tool_call_counts: dict[str, int] = {}

    # ---------------------------------------------------------------------- #
    # LangChain callback interface                                             #
    # ---------------------------------------------------------------------- #

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        """Called before each LLM invocation.

        Injects cache_control headers if the underlying invocation_params
        are accessible, and records the call start time.
        """
        run_id = str(kwargs.get("run_id", ""))
        self._call_start[run_id] = time.monotonic()

        # Attempt to inject cache_control via invocation_params mutation.
        # This works when the LangChain model supports param forwarding.
        invocation_params = kwargs.get("invocation_params") or {}
        if "messages" in invocation_params:
            invocation_params["messages"] = _inject_cache_control(invocation_params["messages"])

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        """Called before each chat model invocation with message objects."""
        run_id = str(kwargs.get("run_id", ""))
        self._call_start[run_id] = time.monotonic()

    def on_llm_end(
        self,
        response: Any,
        **kwargs: Any,
    ) -> None:
        """Called after each LLM invocation.

        Reads real token counts from provider response metadata and records
        them in the shared RunLedger.  Fires loop-detection watchdog checks.
        """
        run_id = str(kwargs.get("run_id", ""))
        _elapsed = time.monotonic() - self._call_start.pop(run_id, time.monotonic())

        # Extract token usage from LangChain response
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        model = ""
        prefix_hash = ""

        try:
            gen = response.generations[0][0] if response.generations else None
            if gen is not None:
                usage = getattr(gen, "generation_info", {}) or {}
                # Anthropic-style usage (via langchain_anthropic)
                usage_meta = usage.get("usage", {}) or usage.get("token_count", {}) or {}
                input_tokens = (
                    usage_meta.get("input_tokens") or usage_meta.get("prompt_tokens") or usage.get("input_tokens") or 0
                )
                output_tokens = (
                    usage_meta.get("output_tokens")
                    or usage_meta.get("completion_tokens")
                    or usage.get("output_tokens")
                    or 0
                )
                cache_read_tokens = (
                    usage_meta.get("cache_read_input_tokens") or usage.get("cache_read_input_tokens") or 0
                )
                model = usage.get("model") or ""

            # LLM output from response.llm_output
            llm_output = response.llm_output or {}
            if not model:
                model = llm_output.get("model_name") or llm_output.get("model") or "unknown"
            token_usage = llm_output.get("token_usage") or {}
            if not input_tokens:
                input_tokens = token_usage.get("prompt_tokens", 0)
            if not output_tokens:
                output_tokens = token_usage.get("completion_tokens", 0)
            if not cache_read_tokens:
                cache_read_tokens = token_usage.get("cache_read_input_tokens", 0)
        except Exception:
            logging.exception("Recovered from broad exception handler")
            logger.debug("langchain token capture failed", exc_info=True)

        # Compute prefix hash from current prompt blocks
        try:
            from atelier.core.capabilities.prefix_cache.planner import PrefixCachePlanner
            from atelier.core.capabilities.prompt_compilation.models import (
                BlockKind,
                PromptBlock,
                Stability,
            )

            if self._ledger.task:
                task_block = PromptBlock(
                    id="task",
                    kind=BlockKind.USER_TASK,
                    stability=Stability.TURN,
                    content=self._ledger.task,
                )
                planner = PrefixCachePlanner()
                plan = planner.plan_with_history([task_block], self._prior_prefix_hash or None)
                prefix_hash = plan.prefix_hash
                self._prior_prefix_hash = prefix_hash
        except Exception:
            logging.exception("Recovered from broad exception handler")
            logger.debug("langchain prefix-cache capture failed", exc_info=True)

        self._ledger.record_call(
            operation="chat",
            model=model or "unknown",
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cache_read_tokens=int(cache_read_tokens),
            stable_prefix_hash=prefix_hash,
        )

        # Loop detection: flag if cache_read_tokens consistently 0
        call_events = [e for e in self._ledger.events if e.payload.get("kind") == "llm_call"]
        if len(call_events) >= 3:
            last3 = call_events[-3:]
            if all(int(e.payload.get("cache_read_tokens", 0)) == 0 for e in last3):
                self._ledger.record(
                    "watchdog_alert",
                    "prefix_cache_miss: 3 consecutive turns with no cache_read_tokens",
                    {
                        "event_type": "PREFIX_CACHE_MISS",
                        "turns": len(last3),
                        "hint": "Ensure system prompt and tool schemas are stable across turns.",
                    },
                )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """Detect repeated tool calls (loop detection)."""
        tool_name = serialized.get("name") or serialized.get("id", ["unknown"])[-1]
        self._tool_call_counts[tool_name] = self._tool_call_counts.get(tool_name, 0) + 1

        if self._tool_call_counts.get(tool_name, 0) >= 3:
            self._ledger.record(
                "watchdog_alert",
                f"loop_detection: tool '{tool_name}' called {self._tool_call_counts[tool_name]} times",
                {
                    "event_type": "REPEATED_TOOL_CALL",
                    "tool": tool_name,
                    "count": self._tool_call_counts[tool_name],
                    "hint": f"Agent is looping on '{tool_name}'. Consider redirecting.",
                },
            )

    def on_tool_end(self, output: str, **kwargs: Any) -> None:
        pass

    def on_llm_error(self, error: Exception, **kwargs: Any) -> None:
        run_id = str(kwargs.get("run_id", ""))
        self._call_start.pop(run_id, None)
        self._ledger.record(
            "note",
            f"llm_error: {type(error).__name__}: {error}",
            {"error_type": type(error).__name__, "error_msg": str(error)},
        )

    # ---------------------------------------------------------------------- #
    # Utility                                                                  #
    # ---------------------------------------------------------------------- #

    @property
    def raise_error(self) -> bool:
        return False

    def ignore_agent(self) -> bool:
        return False

    def ignore_chat_model(self) -> bool:
        return False

    def ignore_llm(self) -> bool:
        return False

    def ignore_retriever(self) -> bool:
        return True

    def ignore_chain(self) -> bool:
        return False
