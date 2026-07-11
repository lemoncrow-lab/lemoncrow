from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.analytics.store import AnalyticsStore, SessionRecord


def _record(session_id: str, **overrides: object) -> SessionRecord:
    base = dict(
        session_id=session_id,
        started_at="2024-01-01T00:00:00",
        ended_at="2024-01-01T01:00:00",
        model="anthropic/claude",
        provider="anthropic",
        mode="code",
        total_cost_usd=1.5,
        total_savings_usd=0.5,
        cache_efficiency_pct=80.0,
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=50,
        cache_write_tokens=10,
        turns=3,
        tool_calls=4,
    )
    base.update(overrides)
    return SessionRecord(**base)  # type: ignore[arg-type]


def test_store_creates_db(tmp_path: Path) -> None:
    db = tmp_path / "analytics.db"
    store = AnalyticsStore(path=db)
    assert db.exists()
    store.close()


def test_upsert_and_recent_round_trip(tmp_path: Path) -> None:
    store = AnalyticsStore(path=tmp_path / "a.db")
    rec = _record("sess-1")
    store.upsert_session(rec)
    recent = store.recent_sessions()
    assert len(recent) == 1
    assert recent[0] == rec
    store.close()


def test_summary_stats_accumulates(tmp_path: Path) -> None:
    store = AnalyticsStore(path=tmp_path / "a.db")
    store.upsert_session(_record("sess-1", started_at="2024-01-01T00:00:00", cache_efficiency_pct=60.0))
    store.upsert_session(_record("sess-2", started_at="2024-01-02T00:00:00", cache_efficiency_pct=80.0))
    stats = store.summary_stats()
    assert stats["total_sessions"] == 2
    assert stats["total_cost_usd"] == 3.0
    assert stats["total_savings_usd"] == 1.0
    assert stats["avg_cache_efficiency_pct"] == 70.0
    assert stats["total_turns"] == 6
    assert stats["total_tool_calls"] == 8

    # Recent ordering is newest-first by started_at.
    recent = store.recent_sessions(5)
    assert [r.session_id for r in recent] == ["sess-2", "sess-1"]
    store.close()
