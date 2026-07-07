from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SessionRecord:
    session_id: str
    started_at: str  # ISO8601
    ended_at: str | None
    model: str
    provider: str
    mode: str
    total_cost_usd: float
    total_savings_usd: float
    cache_efficiency_pct: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    turns: int
    tool_calls: int


class AnalyticsStore:
    def __init__(self, path: Path | None = None) -> None:
        from atelier.core.foundation.paths import default_store_root

        db_path = path or (default_store_root() / "analytics.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                started_at TEXT,
                ended_at TEXT,
                model TEXT,
                provider TEXT,
                mode TEXT,
                total_cost_usd REAL DEFAULT 0,
                total_savings_usd REAL DEFAULT 0,
                cache_efficiency_pct REAL DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                turns INTEGER DEFAULT 0,
                tool_calls INTEGER DEFAULT 0
            )
            """)
        self._conn.commit()

    def upsert_session(self, record: SessionRecord) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.session_id,
                record.started_at,
                record.ended_at,
                record.model,
                record.provider,
                record.mode,
                record.total_cost_usd,
                record.total_savings_usd,
                record.cache_efficiency_pct,
                record.input_tokens,
                record.output_tokens,
                record.cache_read_tokens,
                record.cache_write_tokens,
                record.turns,
                record.tool_calls,
            ),
        )
        self._conn.commit()

    def recent_sessions(self, limit: int = 20) -> list[SessionRecord]:
        cur = self._conn.execute(
            """
            SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [SessionRecord(**dict(zip(cols, row, strict=False))) for row in rows]

    def per_host_stats(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("""
            SELECT
                provider,
                SUM(input_tokens) as total_input_tokens,
                SUM(output_tokens) as total_output_tokens,
                SUM(cache_read_tokens) as total_cache_read_tokens,
                SUM(cache_write_tokens) as total_cache_write_tokens
            FROM sessions
            GROUP BY provider
            """).fetchall()

        stats = []
        for row in rows:
            stats.append(
                {
                    "provider": row[0],
                    "in": row[1] or 0,
                    "out": row[2] or 0,
                    "cR": row[3] or 0,
                    "cW": row[4] or 0,
                }
            )
        return stats

    def summary_stats(self) -> dict[str, Any]:
        row = self._conn.execute("""
            SELECT
                COUNT(*) as total_sessions,
                SUM(total_cost_usd) as total_cost,
                SUM(total_savings_usd) as total_savings,
                AVG(cache_efficiency_pct) as avg_cache_efficiency,
                SUM(turns) as total_turns,
                SUM(tool_calls) as total_tool_calls
            FROM sessions
            """).fetchone()
        if row:
            summary = {
                "total_sessions": row[0],
                "total_cost_usd": round(row[1] or 0, 4),
                "total_savings_usd": round(row[2] or 0, 4),
                "avg_cache_efficiency_pct": round(row[3] or 0, 1),
                "total_turns": row[4] or 0,
                "total_tool_calls": row[5] or 0,
            }
            summary["per_host_token_counts"] = self.per_host_stats()
            return summary
        return {}

    def close(self) -> None:
        self._conn.close()


__all__ = ["AnalyticsStore", "SessionRecord"]
