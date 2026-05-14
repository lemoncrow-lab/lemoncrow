"""ContextBudget recorder — per-turn token savings measurement.

Implements a recorder that captures token usage, cache effects, and
per-lever savings attribution for each tool call in an agent run.
Emits Prometheus metrics and persists records to the SQLite store.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from atelier.core.foundation.store import ContextStore


class RunSavings:
    """Aggregated savings for an entire run."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.total_tokens_saved: int = 0
        self.lever_totals: dict[str, int] = {}
        self.compact_method_totals: dict[str, int] = {}
        self.turn_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "total_tokens_saved": self.total_tokens_saved,
            "lever_totals": self.lever_totals,
            "compact_method_totals": self.compact_method_totals,
            "turn_count": self.turn_count,
        }


class ContextBudgetRecorder:
    """Records per-turn context budget and token savings.

    Attributes:
        store: The ContextStore instance for persistence.
    """

    def __init__(self, store: ContextStore) -> None:
        self.store = store
        self._prometheus_enabled = self._check_prometheus()

    def _check_prometheus(self) -> bool:
        """Check if prometheus_client is available."""
        try:
            import prometheus_client  # noqa: F401

            return True
        except ImportError:
            return False

    def record(
        self,
        *,
        session_id: str,
        turn_index: int,
        model: str,
        input_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        naive_input_tokens: int,
        lever_savings: dict[str, int],
        tool_calls: int,
    ) -> None:
        """Record a single turn's context budget and token savings.

        Args:
            session_id: The run identifier.
            turn_index: Zero-based turn index.
            model: The LLM model used (e.g., "claude-3-opus").
            input_tokens: Actual input tokens to the model.
            cache_read_tokens: Cache read tokens (Claude-specific).
            cache_write_tokens: Cache write tokens (Claude-specific).
            output_tokens: Output tokens from the model.
            naive_input_tokens: Baseline input tokens without optimizations.
            lever_savings: Per-lever token savings attribution.
            tool_calls: Total tool calls in this turn.
        """
        from atelier.core.foundation.savings_models import ContextBudget

        # Create and persist the ContextBudget record
        record = ContextBudget(
            session_id=session_id,
            turn_index=turn_index,
            model=model,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            naive_input_tokens=naive_input_tokens,
            lever_savings=lever_savings,
            tool_calls=tool_calls,
        )

        # Persist to the store
        self.store.persist_context_budget(record)

        # Emit Prometheus metrics if available
        if self._prometheus_enabled:
            self._emit_metrics(record)

    def record_compact_tool_output(
        self,
        *,
        session_id: str,
        turn_index: int,
        model: str,
        method: str,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        """Record net savings for one compact-tool-output call."""
        saved = max(0, tokens_in - tokens_out)
        self.record(
            session_id=session_id,
            turn_index=turn_index,
            model=model,
            input_tokens=tokens_out,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=0,
            naive_input_tokens=tokens_in,
            lever_savings={f"compact_tool_output:{method}": saved} if saved else {},
            tool_calls=1,
        )

    def _emit_metrics(self, record: Any) -> None:
        """Emit Prometheus metrics for the recorded turn."""
        try:
            from prometheus_client import Counter

            # Total tokens saved across all levers for this turn
            total_saved = sum(record.lever_savings.values())

            # Create or get the counter
            if not hasattr(self, "_tokens_saved_counter"):
                self._tokens_saved_counter = Counter(
                    "atelier_tokens_saved_total",
                    "Total tokens saved by optimization lever",
                    ["lever", "model"],
                )

            # Emit one metric per lever, plus a total
            for lever, saved in record.lever_savings.items():
                method = ""
                metric_lever = lever
                if lever.startswith("compact_tool_output:"):
                    metric_lever, method = lever.split(":", 1)
                self._tokens_saved_counter.labels(lever=metric_lever, model=record.model).inc(saved)
                if method:
                    self._emit_compact_method_metric(method, record.model, saved)

            # Also emit a total across all levers
            if total_saved > 0:
                self._tokens_saved_counter.labels(lever="total", model=record.model).inc(total_saved)

        except Exception:
            # Silently fail if Prometheus is not available or metric emission fails
            logger.warning(
                "Suppressed exception at context_budget.py:162",
                exc_info=True,
            )

    def _emit_compact_method_metric(self, method: str, model: str, saved: int) -> None:
        try:
            from prometheus_client import Counter

            if not hasattr(self, "_compact_tokens_saved_counter"):
                self._compact_tokens_saved_counter = Counter(
                    "atelier_compact_output_tokens_saved_total",
                    "Total tokens saved by compact output method",
                    ["method", "model"],
                )

            self._compact_tokens_saved_counter.labels(method=method, model=model).inc(saved)
        except Exception:
            logger.warning(
                "Suppressed exception at context_budget.py:178",
                exc_info=True,
            )

    def aggregate_run(self, session_id: str) -> RunSavings:
        """Aggregate all context budgets for a run.

        Args:
            session_id: The run identifier.

        Returns:
            A RunSavings object with totals and per-lever breakdowns.
        """
        records = self.store.list_context_budgets(session_id)

        result = RunSavings(session_id)
        result.turn_count = len(records)

        for record in records:
            for lever, saved in record.lever_savings.items():
                metric_lever = lever
                if lever.startswith("compact_tool_output:"):
                    metric_lever, method = lever.split(":", 1)
                    result.compact_method_totals[method] = result.compact_method_totals.get(method, 0) + saved
                result.lever_totals[metric_lever] = result.lever_totals.get(metric_lever, 0) + saved
                result.total_tokens_saved += saved

        return result


__all__ = ["ContextBudgetRecorder", "RunSavings"]
