"""Local SQLite product telemetry store."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

RETENTION_DAYS = 30

# Retention pruning is housekeeping, not a per-event obligation: running the
# DELETE inside every write_event() put a table scan + extra WAL churn on the
# tool-call hot path. Once per interval per DB path keeps the table bounded.
_PRUNE_INTERVAL_S = 6 * 60 * 60
_last_prune_ts: dict[str, float] = {}


def default_db_path() -> Path:
    from lemoncrow.core.foundation.paths import default_store_root

    return Path(os.environ.get("LEMONCROW_TELEMETRY_DB", default_store_root() / "telemetry.db"))


class LocalTelemetryStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_db_path()

    def write_event(
        self,
        *,
        event: str,
        props: dict[str, Any],
        exported: bool,
        ts: float | None = None,
    ) -> int:
        timestamp = time.time() if ts is None else ts
        session_id = props.get("session_id") if isinstance(props.get("session_id"), str) else None
        with self._connect() as conn:
            self._init(conn)
            if timestamp - _last_prune_ts.get(str(self.db_path), 0.0) >= _PRUNE_INTERVAL_S:
                _last_prune_ts[str(self.db_path)] = timestamp
                self._prune(conn, timestamp)
            cur = conn.execute(
                """
                INSERT INTO events (ts, event, session_id, props_json, exported)
                VALUES (?, ?, ?, ?, ?)
                """,
                (timestamp, event, session_id, json.dumps(props, sort_keys=True), int(exported)),
            )
            return int(cur.lastrowid or 0)

    def write_events(self, events: list[dict[str, Any]]) -> None:
        """Persist a batch of already-validated+scrubbed events in one transaction.

        Used by the async telemetry worker: the whole batch shares one
        connection (no per-event connect + DDL), and prune/VACUUM runs here —
        off the hot path — never inside an emit call.
        """
        if not events:
            return
        now = time.time()
        rows: list[tuple[float, str, str | None, str, int]] = []
        for item in events:
            props = item.get("props") or {}
            session_id = props.get("session_id") if isinstance(props.get("session_id"), str) else None
            rows.append(
                (
                    float(item.get("ts", now)),
                    str(item["event"]),
                    session_id,
                    json.dumps(props, sort_keys=True),
                    int(bool(item.get("exported", False))),
                )
            )
        with self._connect() as conn:
            self._init(conn)
            if now - _last_prune_ts.get(str(self.db_path), 0.0) >= _PRUNE_INTERVAL_S:
                _last_prune_ts[str(self.db_path)] = now
                self._prune(conn, now)
            conn.executemany(
                "INSERT INTO events (ts, event, session_id, props_json, exported) VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    def _where_and_params(
        self,
        *,
        since: float | None,
        event: str | None,
        host: str | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if event:
            clauses.append("event = ?")
            params.append(event)
        if host:
            clauses.append("""
                (
                  (event = 'session_start' AND json_extract(props_json, '$.agent_host') = ?)
                  OR session_id IN (
                    SELECT session_id
                    FROM events
                    WHERE event = 'session_start'
                      AND json_extract(props_json, '$.agent_host') = ?
                      AND session_id IS NOT NULL
                  )
                )
                """)
            params.extend([host, host])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return where, params

    def list_events(
        self,
        *,
        since: float | None = None,
        event: str | None = None,
        host: str | None = None,
        limit: int | None = 500,
    ) -> list[dict[str, Any]]:
        if limit is not None:
            limit = max(1, min(limit, 5000))
        where, params = self._where_and_params(since=since, event=event, host=host)
        with self._connect() as conn:
            self._init(conn)
            query = f"""
                SELECT id, ts, event, session_id, props_json, exported
                FROM events{where}
                ORDER BY ts DESC
                """
            query_params = list(params)
            if limit is not None:
                query += "\n                LIMIT ?"
                query_params.append(limit)
            rows = conn.execute(query, query_params).fetchall()
        return [_row_to_event(row) for row in rows]

    def _iter_events(
        self,
        *,
        since: float | None = None,
        event: str | None = None,
        host: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream matching events one row at a time via a lazy cursor.

        ``summary`` aggregates over the whole retention window; iterating keeps
        peak memory at O(1) rows instead of materialising every event as a dict
        list (telemetry must never take too much RAM).
        """
        where, params = self._where_and_params(since=since, event=event, host=host)
        with self._connect() as conn:
            self._init(conn)
            query = f"""
                SELECT id, ts, event, session_id, props_json, exported
                FROM events{where}
                ORDER BY ts DESC
                """
            for row in conn.execute(query, params):
                yield _row_to_event(row)

    def summary(
        self,
        *,
        since: float | None = None,
        event: str | None = None,
        host: str | None = None,
    ) -> dict[str, Any]:
        events = self._iter_events(since=since, event=event, host=host)
        session_hosts = self._session_hosts()
        commands_by_day: Counter[str] = Counter()
        top_commands: Counter[str] = Counter()
        agent_hosts: Counter[str] = Counter()
        top_playbooks: Counter[str] = Counter()
        playbook_domains: dict[str, str] = {}
        retrieval_scores: Counter[str] = Counter()
        plan_checks: Counter[str] = Counter()
        frustration_behavioral: Counter[str] = Counter()
        frustration_lexical: Counter[str] = Counter()
        value = {
            "tokens_saved_estimate": 0,
            "cache_hits": 0,
            "total_tool_calls": 0,
            "cache_hit_rate": 0.0,
            "blocks_applied": 0,
        }
        event_counts: Counter[str] = Counter()
        session_ids: set[str] = set()
        first_event_ts: float | None = None
        last_event_ts: float | None = None

        for item in events:
            props = item["props"]
            event_counts[item["event"]] += 1
            session_id = item.get("session_id")
            if isinstance(session_id, str) and session_id:
                session_ids.add(session_id)
            event_ts = float(item["ts"])
            if first_event_ts is None or event_ts < first_event_ts:
                first_event_ts = event_ts
            if last_event_ts is None or event_ts > last_event_ts:
                last_event_ts = event_ts
            day = time.strftime("%Y-%m-%d", time.localtime(float(item["ts"])))
            if item["event"] in {"cli_command_invoked", "cli_command_completed"}:
                commands_by_day[day] += 1
                command = props.get("command_name")
                if isinstance(command, str):
                    top_commands[command] += 1
            host_name = props.get("agent_host")
            if not isinstance(host_name, str):
                host_name = session_hosts.get(session_id or "")
            if isinstance(host_name, str) and host_name:
                agent_hosts[host_name] += 1
            if item["event"] == "playbook_applied":
                block_hash = props.get("block_id_hash")
                if isinstance(block_hash, str):
                    top_playbooks[block_hash] += 1
                    domain = props.get("domain")
                    if isinstance(domain, str):
                        playbook_domains[block_hash] = domain
            if item["event"] in {"playbook_applied", "playbook_retrieved"}:
                score = props.get("retrieval_score")
                if isinstance(score, (int, float)):
                    retrieval_scores[_score_bucket(float(score))] += 1
            if item["event"].startswith("plan_check_"):
                plan_checks[item["event"]] += 1
            if item["event"] == "frustration_signal_behavioral":
                signal = props.get("signal_type")
                if isinstance(signal, str):
                    frustration_behavioral[signal] += 1
            if item["event"] == "frustration_signal_lexical":
                category = props.get("category")
                if isinstance(category, str):
                    frustration_lexical[category] += 1
            if item["event"] == "value_estimate":
                for key in ("tokens_saved_estimate", "cache_hits", "total_tool_calls", "blocks_applied"):
                    raw = props.get(key)
                    if isinstance(raw, int) and not isinstance(raw, bool):
                        value[key] += raw

        total_tool_calls = int(value["total_tool_calls"])
        cache_hits = int(value["cache_hits"])
        value["cache_hit_rate"] = round(cache_hits / total_tool_calls, 3) if total_tool_calls > 0 else 0.0

        return {
            "events_total": sum(event_counts.values()),
            "unique_event_types": len(event_counts),
            "active_sessions": len(session_ids),
            "first_event_ts": first_event_ts,
            "last_event_ts": last_event_ts,
            "event_counts": dict(event_counts),
            "commands_by_day": _counter_series(commands_by_day),
            "top_commands": _counter_items(top_commands),
            "agent_hosts": _counter_items(agent_hosts),
            "top_playbooks": [
                {"block_id_hash": key, "count": count, "domain": playbook_domains.get(key, "")}
                for key, count in top_playbooks.most_common(10)
            ],
            "retrieval_score_distribution": _counter_items(retrieval_scores),
            "plan_checks": dict(plan_checks),
            "frustration_behavioral": _counter_items(frustration_behavioral),
            "frustration_lexical": _counter_items(frustration_lexical),
            "value_estimate": value,
        }

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent readers + one writer without locking conflicts
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:  # noqa: BLE001
            pass  # ignore if already set or fails
        return conn

    def _init(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL,
              event TEXT NOT NULL,
              session_id TEXT,
              props_json TEXT NOT NULL,
              exported INTEGER NOT NULL DEFAULT 0
            )
            """)
        conn.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS events_event_ts ON events(event, ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS events_session ON events(session_id)")

    def _session_hosts(self) -> dict[str, str]:
        with self._connect() as conn:
            self._init(conn)
            rows = conn.execute("""
                SELECT session_id, props_json
                FROM events
                WHERE event = 'session_start' AND session_id IS NOT NULL
                ORDER BY ts DESC
                """).fetchall()
        hosts: dict[str, str] = {}
        for row in rows:
            try:
                props = json.loads(row["props_json"])
            except json.JSONDecodeError:
                continue
            host = props.get("agent_host")
            session_id = row["session_id"]
            if isinstance(session_id, str) and isinstance(host, str):
                hosts.setdefault(session_id, host)
        return hosts

    def _prune(self, conn: sqlite3.Connection, now: float) -> None:
        cutoff = now - RETENTION_DAYS * 24 * 60 * 60
        deleted = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
        if deleted and deleted > 0:
            # Reclaim pages freed by the delete instead of letting the file (and
            # its WAL) ratchet up forever. VACUUM must run outside a transaction,
            # so commit first; this runs at most once per prune window (6h).
            conn.commit()
            try:
                conn.execute("VACUUM")
            except sqlite3.Error:
                pass


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    try:
        props = json.loads(row["props_json"])
    except json.JSONDecodeError:
        props = {}
    return {
        "id": row["id"],
        "ts": row["ts"],
        "event": row["event"],
        "session_id": row["session_id"],
        "props": props,
        "exported": bool(row["exported"]),
    }


def _counter_items(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"name": key, "count": count} for key, count in counter.most_common(20)]


def _counter_series(counter: Counter[str]) -> list[dict[str, Any]]:
    by_day: defaultdict[str, int] = defaultdict(int)
    by_day.update(counter)
    return [{"day": key, "count": by_day[key]} for key in sorted(by_day)]


def _score_bucket(value: float) -> str:
    if value < 0.25:
        return "0-0.25"
    if value < 0.5:
        return "0.25-0.5"
    if value < 0.75:
        return "0.5-0.75"
    return "0.75-1.0"
