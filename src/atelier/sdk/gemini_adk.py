"""Gemini ADK-style middleware hooks for Atelier.

This adapter mirrors the existing SDK surfaces and provides a minimal hook set
that can be wired into Google Gemini ADK agent lifecycles:

- ``on_agent_start`` / ``on_agent_end``
- ``on_model_start`` / ``on_model_end``
- ``on_tool_start`` / ``on_tool_end``

The implementation avoids importing Gemini SDK modules at import time, so
Atelier keeps Gemini integration optional.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from atelier.infra.runtime.run_ledger import RunLedger

logger = logging.getLogger(__name__)


class GeminiADKMiddleware:
    """Gemini ADK-compatible lifecycle hooks backed by ``RunLedger``."""

    def __init__(self, ledger: RunLedger, *, mode: str = "shadow") -> None:
        self._ledger = ledger
        self.mode = mode
        self._agent_start: float = 0.0
        self._model_start: dict[str, float] = {}
        self._prior_prefix_hash: str = ""
        self._tool_call_counts: dict[str, int] = {}

    def on_agent_start(self, agent_name: str = "agent", **_: Any) -> None:
        self._agent_start = time.monotonic()
        self._ledger.record(
            "agent_message",
            f"agent_start: {agent_name}",
            {"event": "start", "agent": agent_name},
        )

    def on_agent_end(self, agent_name: str = "agent", output: Any = None, **_: Any) -> None:
        elapsed = round(time.monotonic() - self._agent_start, 2)
        self._ledger.record(
            "agent_message",
            f"agent_end: {agent_name} in {elapsed}s",
            {
                "event": "end",
                "agent": agent_name,
                "elapsed_s": elapsed,
                "output_type": type(output).__name__,
            },
        )

    def on_model_start(self, run_id: str = "", **_: Any) -> None:
        self._model_start[run_id] = time.monotonic()

    def on_model_end(self, response: Any, run_id: str = "", model: str = "gemini", **_: Any) -> None:
        self._model_start.pop(run_id, None)

        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        used_model = model
        prefix_hash = ""
        prefix_invalidated_reason = ""

        usage_meta = getattr(response, "usage_metadata", None)
        if isinstance(usage_meta, dict):
            input_tokens = int(usage_meta.get("prompt_token_count") or usage_meta.get("input_tokens") or 0)
            output_tokens = int(usage_meta.get("candidates_token_count") or usage_meta.get("output_tokens") or 0)
            cache_read_tokens = int(
                usage_meta.get("cached_content_token_count") or usage_meta.get("cache_read_input_tokens") or 0
            )
            used_model = str(usage_meta.get("model") or used_model)
        else:
            usage = getattr(response, "usage", None)
            if usage is not None:
                input_tokens = int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0)
                cache_read_tokens = int(
                    getattr(usage, "cached_tokens", 0) or getattr(usage, "cache_read_input_tokens", 0) or 0
                )
            used_model = str(getattr(response, "model", used_model) or used_model)

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
                prefix_invalidated_reason = plan.invalidated_reason
                self._prior_prefix_hash = prefix_hash
        except Exception:
            logging.exception("Recovered from broad exception handler")
            logger.debug("gemini prefix-cache capture failed", exc_info=True)

        self._ledger.record_call(
            operation="gemini.generate_content",
            model=used_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            stable_prefix_hash=prefix_hash,
            prefix_invalidated_reason=prefix_invalidated_reason,
        )

        call_events = [e for e in self._ledger.events if e.payload.get("kind") == "llm_call"]
        if len(call_events) >= 3:
            last3 = call_events[-3:]
            if all(int(e.payload.get("cache_read_tokens", 0)) == 0 for e in last3):
                self._ledger.record(
                    "watchdog_alert",
                    "prefix_cache_miss: 3 consecutive turns with no cache_read_tokens",
                    {
                        "event_type": "PREFIX_CACHE_MISS",
                        "turns": 3,
                        "hint": "Keep ADK system/tool context stable to improve provider caching.",
                    },
                )

    def on_tool_start(self, tool_name: str, **_: Any) -> None:
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
                    "hint": f"Agent is looping on '{tool_name}'. Consider redirecting.",
                },
            )

    def on_tool_end(self, tool_name: str, result: Any = None, **_: Any) -> None:
        self._ledger.record(
            "tool_result",
            f"tool_end: {tool_name}",
            {"tool": tool_name, "result_type": type(result).__name__},
        )

    def __enter__(self) -> GeminiADKMiddleware:
        return self

    def __exit__(self, *_: Any) -> None:
        self._ledger.close()
