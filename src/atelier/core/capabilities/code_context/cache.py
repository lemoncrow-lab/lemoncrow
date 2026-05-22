"""SQLite-backed retrieval cache for code-context payloads."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class RetrievalCache:
    """Content-addressed retrieval cache stored in the code-context SQLite DB."""

    def __init__(self, db_path: str | Path, *, max_bytes: int = 64 * 1024 * 1024) -> None:
        self.db_path = Path(db_path)
        self.max_bytes = max_bytes

    def get(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        index_version: int,
        repo_id: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        query_hash = self.make_key(tool_name=tool_name, args=args, index_version=index_version, repo_id=repo_id)
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute(
                """
                SELECT payload_json
                FROM retrieval_cache
                WHERE query_hash = ? AND tool_name = ? AND index_version = ?
                """,
                (query_hash, tool_name, index_version),
            ).fetchone()
            if row is None:
                return False, None
            conn.execute(
                """
                UPDATE retrieval_cache
                SET hit_count = hit_count + 1,
                    last_hit_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE query_hash = ?
                """,
                (query_hash,),
            )
        return True, json.loads(str(row["payload_json"]))

    def set(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        index_version: int,
        repo_id: str,
        payload: dict[str, Any],
    ) -> None:
        query_hash = self.make_key(tool_name=tool_name, args=args, index_version=index_version, repo_id=repo_id)
        payload_json = _canonical_json(payload)
        with self._connect() as conn:
            self._init_schema(conn)
            conn.execute(
                """
                INSERT INTO retrieval_cache(query_hash, repo_id, tool_name, index_version, payload_json, hit_count, last_hit_at)
                VALUES (?, ?, ?, ?, ?, 0, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(query_hash) DO UPDATE SET
                    repo_id = excluded.repo_id,
                    tool_name = excluded.tool_name,
                    index_version = excluded.index_version,
                    payload_json = excluded.payload_json,
                    last_hit_at = excluded.last_hit_at
                """,
                (query_hash, repo_id, tool_name, index_version, payload_json),
            )
            self._evict_lru(conn)

    def stats(
        self,
        *,
        repo_id: str,
        index_version: int,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            self._init_schema(conn)
            where = ["index_version = ?", "(repo_id = ? OR repo_id IS NULL)"]
            params: list[Any] = [index_version, repo_id]
            if tool_name:
                where.append("tool_name = ?")
                params.append(tool_name)
            where_sql = " AND ".join(where)
            totals = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS entry_count,
                    COALESCE(SUM(LENGTH(payload_json)), 0) AS total_bytes,
                    COALESCE(MAX(last_hit_at), '') AS last_hit_at
                FROM retrieval_cache
                WHERE {where_sql}
                """,
                params,
            ).fetchone()
            tool_rows = conn.execute(
                f"""
                SELECT tool_name, COUNT(*) AS entry_count
                FROM retrieval_cache
                WHERE {where_sql}
                GROUP BY tool_name
                ORDER BY tool_name ASC
                """,
                params,
            ).fetchall()
        return {
            "entry_count": int(totals["entry_count"]) if totals is not None else 0,
            "entries_by_tool": {str(row["tool_name"]): int(row["entry_count"]) for row in tool_rows},
            "total_bytes": int(totals["total_bytes"]) if totals is not None else 0,
            "last_hit_at": str(totals["last_hit_at"]) if totals is not None else "",
            "max_bytes": self.max_bytes,
        }

    def invalidate(
        self,
        *,
        repo_id: str,
        index_version: int,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            self._init_schema(conn)
            where = ["index_version = ?", "(repo_id = ? OR repo_id IS NULL)"]
            params: list[Any] = [index_version, repo_id]
            if tool_name:
                where.append("tool_name = ?")
                params.append(tool_name)
            where_sql = " AND ".join(where)
            rows = conn.execute(
                f"""
                SELECT tool_name, COUNT(*) AS entry_count
                FROM retrieval_cache
                WHERE {where_sql}
                GROUP BY tool_name
                ORDER BY tool_name ASC
                """,
                params,
            ).fetchall()
            conn.execute(f"DELETE FROM retrieval_cache WHERE {where_sql}", params)
        entries_by_tool = {str(row["tool_name"]): int(row["entry_count"]) for row in rows}
        return {
            "invalidated_entries": sum(entries_by_tool.values()),
            "entries_by_tool": entries_by_tool,
        }

    def make_key(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        index_version: int,
        repo_id: str,
    ) -> str:
        """Freeze the M12 key shape to args + index version + repo + tool name."""
        payload = f"{_canonical_json(args)}|{index_version}|{repo_id}|{tool_name}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS retrieval_cache (
                query_hash TEXT PRIMARY KEY,
                repo_id TEXT,
                tool_name TEXT NOT NULL,
                index_version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                last_hit_at TEXT NOT NULL
            )
            """)
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(retrieval_cache)")}
        if "repo_id" not in columns:
            conn.execute("ALTER TABLE retrieval_cache ADD COLUMN repo_id TEXT")

    def _evict_lru(self, conn: sqlite3.Connection) -> None:
        while True:
            row = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(payload_json)), 0) AS total_bytes FROM retrieval_cache"
            ).fetchone()
            total_bytes = int(row["total_bytes"]) if row is not None else 0
            if total_bytes <= self.max_bytes:
                return
            oldest = conn.execute("""
                SELECT query_hash
                FROM retrieval_cache
                ORDER BY last_hit_at ASC, hit_count ASC, query_hash ASC
                LIMIT 1
                """).fetchone()
            if oldest is None:
                return
            conn.execute("DELETE FROM retrieval_cache WHERE query_hash = ?", (str(oldest["query_hash"]),))


__all__ = ["RetrievalCache"]
