"""Shared telemetry substrate — all capabilities publish signals here."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

_MAX_EVENTS = 10_000


@dataclass
class TelemetryEvent:
    """A single metric emission from a capability.

    Attributes:
        capability:  Name of the emitting capability.
        metric:      Signal name, e.g. 'token_cost', 'latency_ms',
                     'hit_quality', 'loop_probability', 'retry_count', 'success'.
        value:       Numeric value.
        context:     Free-form key/value metadata from the emitter.
        timestamp:   Unix time of emission (auto-set).
    """

    capability: str
    metric: str
    value: float
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "metric": self.metric,
            "value": self.value,
            "context": self.context,
            "timestamp": self.timestamp,
        }


class TelemetrySubstrate:
    """Thread-safe in-memory event bus for capability metrics.

    All capabilities should emit signals here so that adaptive
    components (adaptive priors, rerankers, budget optimizer) can
    learn from one unified signal space.

    Usage::

        bus = TelemetrySubstrate()
        bus.emit("tool_supervision", "redundancy_score", 0.8, session_id="r1")
        events = bus.query(capability="tool_supervision")
        stats  = bus.aggregates(metric="redundancy_score")
    """

    def __init__(self, max_events: int = _MAX_EVENTS) -> None:
        self._events: deque[TelemetryEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Emit

    def emit(
        self,
        capability: str,
        metric: str,
        value: float,
        **context: Any,
    ) -> None:
        """Publish a metric signal.

        Args:
            capability:  Source capability name.
            metric:      Signal name.
            value:       Numeric measurement.
            **context:   Extra key/value pairs attached to the event.
        """
        event = TelemetryEvent(
            capability=capability,
            metric=metric,
            value=value,
            context=dict(context),
        )
        with self._lock:
            self._events.append(event)

    # ------------------------------------------------------------------
    # Query

    def query(
        self,
        *,
        capability: str | None = None,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[TelemetryEvent]:
        """Return recent events, optionally filtered by capability/metric."""
        with self._lock:
            events = list(self._events)
        if capability:
            events = [e for e in events if e.capability == capability]
        if metric:
            events = [e for e in events if e.metric == metric]
        return events[-limit:]

    def aggregates(
        self,
        *,
        capability: str | None = None,
        metric: str | None = None,
    ) -> dict[str, float]:
        """Return summary statistics over matching events.

        Returns a dict with keys: ``count``, ``mean``, ``p95``, ``total``.
        All zero when no events match.
        """
        events = self.query(capability=capability, metric=metric, limit=_MAX_EVENTS)
        if not events:
            return {"count": 0.0, "mean": 0.0, "p95": 0.0, "total": 0.0}
        values = sorted(e.value for e in events)
        n = len(values)
        mean = sum(values) / n
        p95_idx = min(n - 1, int(0.95 * n))
        return {
            "count": float(n),
            "mean": round(mean, 4),
            "p95": round(values[p95_idx], 4),
            "total": round(sum(values), 4),
        }

    # ------------------------------------------------------------------
    # Management

    def clear(self) -> None:
        """Discard all buffered events."""
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)
