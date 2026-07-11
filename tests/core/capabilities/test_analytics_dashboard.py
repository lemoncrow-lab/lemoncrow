"""Tests for the analytics session store (dashboard now served via FastAPI + React)."""

from __future__ import annotations


def test_store_records_and_summarizes_sessions(tmp_path) -> None:
    from lemoncrow.core.capabilities.analytics.store import AnalyticsStore, SessionRecord

    store = AnalyticsStore(path=tmp_path / "analytics.db")
    store.upsert_session(
        SessionRecord(
            session_id="sess-test-123",
            started_at="2024-01-01T00:00:00",
            ended_at=None,
            model="anthropic/claude",
            provider="anthropic",
            mode="code",
            total_cost_usd=0.1234,
            total_savings_usd=0.5678,
            cache_efficiency_pct=72.5,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=80,
            cache_write_tokens=20,
            turns=3,
            tool_calls=5,
        )
    )

    summary = store.summary_stats()
    sessions = store.recent_sessions(50)
    store.close()

    assert summary["total_sessions"] == 1
    assert sessions[0].session_id == "sess-test-123"
    assert sessions[0].mode == "code"
