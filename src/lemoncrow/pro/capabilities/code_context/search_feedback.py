"""Transport-neutral, bounded search-session feedback policy."""

from __future__ import annotations

import logging
import threading
from typing import Any

from lemoncrow.core.foundation.runtime_decisions import (
    RuntimeDecisionEvent,
    RuntimeDecisionSink,
)
from lemoncrow.pro.capabilities.code_context.search_verdict import (
    BREAKER_NOTE,
    ChannelHealth,
    SearchHistory,
    compute_verdict,
)

_LOG = logging.getLogger(__name__)
_DEFAULT_MAX_SESSIONS = 64


class SearchFeedbackPolicy:
    """Own per-session search memory independently of any transport."""

    def __init__(self, *, max_sessions: int = _DEFAULT_MAX_SESSIONS) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self.histories: dict[str, SearchHistory] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self.histories.clear()

    def _history(self, session_id: str) -> SearchHistory:
        history = self.histories.get(session_id)
        if history is not None:
            return history
        history = SearchHistory()
        self.histories[session_id] = history
        if len(self.histories) > self.max_sessions:
            overflow = len(self.histories) - self.max_sessions
            for stale in list(self.histories)[:overflow]:
                if stale != session_id:
                    self.histories.pop(stale, None)
        return history

    @staticmethod
    def _emit_decision(
        sink: RuntimeDecisionSink | None,
        *,
        session_id: str,
        verdict: str,
        hit_count: int,
        channels: ChannelHealth,
        breaker_tripped: bool,
    ) -> None:
        if sink is None:
            return
        reason_codes = [verdict]
        if breaker_tripped:
            reason_codes.append("breaker_tripped")
        reason_codes.extend(f"channel_dark:{name}" for name in channels.dark())
        event = RuntimeDecisionEvent(
            kind="retrieval.verdict",
            phase="retrieve",
            policy="search-feedback",
            policy_version="1",
            mode="observe",
            session_id=session_id,
            reason_codes=tuple(reason_codes),
            proposed={"verdict": verdict, "breaker": breaker_tripped},
            actual={"verdict": verdict, "breaker": breaker_tripped},
            metrics={
                "hit_count": max(0, int(hit_count)),
                "found": hit_count > 0,
                "dark_channels": channels.dark(),
            },
        )
        try:
            sink.record_runtime_decision(event)
        except Exception:
            _LOG.debug("runtime decision sink failed for search feedback", exc_info=True)

    def apply(
        self,
        result: dict[str, Any],
        *,
        session_id: str,
        query: str,
        hit_count: int,
        channels: ChannelHealth | None = None,
        decision_sink: RuntimeDecisionSink | None = None,
    ) -> dict[str, Any]:
        """Stamp verdict, next hint and breaker feedback onto result in place."""

        if not isinstance(result, dict) or not (query or "").strip():
            return result

        channel_health = channels if isinstance(channels, ChannelHealth) else ChannelHealth()
        found = hit_count > 0
        stable_session_id = (session_id or "").strip() or "_global"

        with self._lock:
            history = self._history(stable_session_id)
            prior = history.prior_empties()

        verdict = compute_verdict(
            hit_count=hit_count,
            query=query,
            channels=channel_health,
            prior_empties=prior,
        )

        with self._lock:
            history.record(query, found=found)
            tripped = history.breaker_tripped()

        result["verdict"] = verdict.verdict
        if verdict.next:
            result["next"] = verdict.next
        if tripped and not found:
            result["breaker_note"] = BREAKER_NOTE

        self._emit_decision(
            decision_sink,
            session_id=stable_session_id,
            verdict=verdict.verdict,
            hit_count=hit_count,
            channels=channel_health,
            breaker_tripped=tripped and not found,
        )
        return result


__all__ = ["SearchFeedbackPolicy"]
