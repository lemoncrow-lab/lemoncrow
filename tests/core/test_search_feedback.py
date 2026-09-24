from __future__ import annotations

from lemoncrow.core.foundation.runtime_decisions import RuntimeDecisionEvent
from lemoncrow.pro.capabilities.code_context.search_feedback import SearchFeedbackPolicy
from lemoncrow.pro.capabilities.code_context.search_verdict import BREAKER_NOTE, ChannelHealth


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[RuntimeDecisionEvent] = []

    def record_runtime_decision(self, event: RuntimeDecisionEvent) -> None:
        self.events.append(event)


def test_search_feedback_tracks_reformulations_per_session() -> None:
    policy = SearchFeedbackPolicy()

    first = policy.apply({}, session_id="a", query="payment parser", hit_count=0)
    second = policy.apply({}, session_id="a", query="payment parsing", hit_count=0)
    other = policy.apply({}, session_id="b", query="payment parsing", hit_count=0)

    assert first["verdict"] == "missed"
    assert second["verdict"] == "absent"
    assert other["verdict"] == "missed"


def test_search_feedback_preserves_dark_and_breaker_contract(monkeypatch) -> None:
    monkeypatch.setenv("LEMONCROW_SEARCH_BREAKER_THRESHOLD", "2")
    policy = SearchFeedbackPolicy()

    dark = policy.apply(
        {},
        session_id="s",
        query="semantic concept",
        hit_count=0,
        channels=ChannelHealth(semantic=False),
    )
    assert dark["verdict"] == "dark"
    assert "semantic off" in dark["next"]

    first = policy.apply({}, session_id="breaker", query="alpha one", hit_count=0)
    second = policy.apply({}, session_id="breaker", query="beta two", hit_count=0)
    assert "breaker_note" not in first
    assert second["breaker_note"] == BREAKER_NOTE


def test_search_feedback_bounds_session_registry() -> None:
    policy = SearchFeedbackPolicy(max_sessions=2)

    policy.apply({}, session_id="a", query="a", hit_count=0)
    policy.apply({}, session_id="b", query="b", hit_count=0)
    policy.apply({}, session_id="c", query="c", hit_count=0)

    assert len(policy.histories) == 2
    assert "c" in policy.histories
    assert "a" not in policy.histories


def test_search_feedback_emits_runtime_decision_facts() -> None:
    policy = SearchFeedbackPolicy()
    sink = _CollectingSink()

    result = policy.apply(
        {},
        session_id="session-1",
        query="authorization middleware",
        hit_count=0,
        channels=ChannelHealth(zoekt=False),
        decision_sink=sink,
    )

    assert result["verdict"] == "dark"
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.kind == "retrieval.verdict"
    assert event.policy == "search-feedback"
    assert event.reason_codes == ("dark", "channel_dark:zoekt")
    assert event.metrics["hit_count"] == 0
    assert event.metrics["dark_channels"] == ["zoekt"]
