"""HistoryStore -- traces, raw artifacts, sync status.

High-volume, append-only. Written by every session import (Session parsers)
and every live run (RuntimeSession.record_trace). Read by CLI sessions, MCP
queries, lesson promotion, and failure analysis. ``batch_mode()`` imports
write traces + raw_artifacts atomically -- both live in this one file, so
that atomicity is unaffected by the split. ``list_unsynced_trace_ids`` JOINs
traces <-> sync_status, also both in this file.

Backed by ``lemoncrow_history.db``, physically separate from knowledge,
lessons, jobs, memory, and telemetry so import/live-run writes never
contend with those stores' locks.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.models import RawArtifact, Trace, coerce_trace_json, to_jsonable
from lemoncrow.core.foundation.sqlite_base import SqliteTableStore

logger = logging.getLogger(__name__)

TRACE_FTS_COLUMNS = [
    "id",
    "task",
    "reasoning",
    "tools",
    "commands",
    "errors",
    "output",
    "files",
    "validations",
    "learnings",
    "meta",
]

TRACE_FTS_SNIPPETS = [
    (1, "Task"),
    (2, "Reasoning"),
    (3, "Tools"),
    (4, "Commands"),
    (5, "Errors"),
    (6, "Summary"),
    (7, "Files"),
    (8, "Validations"),
    (9, "Learnings"),
    (10, "Run"),
]

TRACE_FTS_DDL = """
CREATE VIRTUAL TABLE traces_fts USING fts5(
    id UNINDEXED,
    task,
    reasoning,
    tools,
    commands,
    errors,
    output,
    files,
    validations,
    learnings,
    meta,
    tokenize = 'porter'
)
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    host TEXT,
    domain TEXT,
    status TEXT NOT NULL,
    task TEXT NOT NULL,
    workspace_path TEXT,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_domain ON traces(domain);
CREATE INDEX IF NOT EXISTS idx_traces_status ON traces(status);
-- get_trace()'s session_id fallback does json_extract(payload, '$.session_id')
-- per miss; without this expression index that's a full-table scan per call.
CREATE INDEX IF NOT EXISTS idx_traces_session_id ON traces(json_extract(payload, '$.session_id'));

CREATE VIRTUAL TABLE IF NOT EXISTS traces_fts USING fts5(
    id UNINDEXED,
    task,
    reasoning,
    tools,
    commands,
    errors,
    output,
    files,
    validations,
    learnings,
    meta,
    tokenize = 'porter'
);

CREATE TABLE IF NOT EXISTS raw_artifacts (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    content_path TEXT NOT NULL,
    sha256_original TEXT NOT NULL,
    sha256_redacted TEXT NOT NULL,
    byte_count_original INTEGER NOT NULL,
    byte_count_redacted INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    source_file_mtime TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_raw_artifacts_source_session
    ON raw_artifacts(source, source_session_id);

CREATE TABLE IF NOT EXISTS sync_status (
    session_id TEXT PRIMARY KEY,
    synced_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
"""


class HistoryStore(SqliteTableStore):
    """SQLite-backed store for traces, raw artifacts, and sync status."""

    SCHEMA = SCHEMA
    REQUIRED_TABLES = ("traces", "traces_fts", "raw_artifacts", "sync_status")

    def __init__(self, root: Path | str, *, db_name: str = "lemoncrow_history.db") -> None:
        super().__init__(root, db_name=db_name)
        self.traces_dir = self.root / "traces"
        self.raw_dir = self.root / "raw"

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        super().init()
        with self._transaction() as conn:
            recreated_trace_fts = self._ensure_trace_search_schema(conn)
            self._reindex_traces_fts_if_needed(conn, force=recreated_trace_fts)

    def _ensure_trace_search_schema(self, conn: sqlite3.Connection) -> bool:
        rows = conn.execute("PRAGMA table_info(traces_fts)").fetchall()
        actual_columns = [row[1] for row in rows]
        if actual_columns == TRACE_FTS_COLUMNS:
            return False
        conn.execute("DROP TABLE IF EXISTS traces_fts")
        conn.execute(TRACE_FTS_DDL)
        return True

    def _reindex_traces_fts_if_needed(self, conn: sqlite3.Connection, *, force: bool = False) -> None:
        trace_count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM traces_fts").fetchone()[0]
        if not force and trace_count == fts_count:
            return
        self._reindex_traces_fts(conn)

    def _reindex_traces_fts(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT payload FROM traces").fetchall()
        with closing(conn.cursor()) as cur:
            cur.execute("DELETE FROM traces_fts")
            for row in rows:
                self._update_trace_fts(cur, Trace.model_validate_json(coerce_trace_json(row["payload"])))

    def _build_trace_search_document(self, trace: Trace) -> tuple[str, ...]:
        reasoning = "\n".join(trace.reasoning)

        tools_parts = []
        for tool in trace.tools_called:
            sections = [tool.name]
            if tool.result_summary:
                sections.append(tool.result_summary)
            if tool.args:
                sections.append(json.dumps(tool.args, ensure_ascii=False, sort_keys=True))
            tools_parts.append("\n".join(part for part in sections if part))
        tools = "\n\n".join(tools_parts)

        command_parts = []
        for command in trace.commands_run:
            if isinstance(command, str):
                command_parts.append(command)
                continue
            command_parts.append(
                "\n".join(part for part in [command.command, command.stdout or "", command.stderr or ""] if part)
            )
        commands = "\n\n".join(command_parts)

        errors = "\n".join(trace.errors_seen)
        output = "\n\n".join(part for part in [trace.diff_summary, trace.output_summary] if part)

        file_parts = []
        for file_record in trace.files_touched:
            if isinstance(file_record, str):
                file_parts.append(file_record)
                continue
            sections = [file_record.path]
            if file_record.event:
                sections.append(file_record.event)
            if file_record.diff:
                sections.append(file_record.diff)
            file_parts.append("\n".join(part for part in sections if part))
        files = "\n\n".join(file_parts)

        validation_parts = []
        for validation in trace.validation_results:
            status = "passed" if validation.passed else "failed"
            validation_parts.append(
                " ".join(part for part in [validation.name, status, validation.detail or ""] if part)
            )
        validations = "\n".join(validation_parts)

        learning_parts = []
        for learning in trace.learnings:
            learning_parts.append(
                "\n".join(
                    part
                    for part in [
                        learning.kind,
                        learning.text,
                        learning.evidence,
                        learning.promote_to or "",
                    ]
                    if part
                )
            )
        learnings = "\n\n".join(learning_parts)

        meta = "\n".join(
            part
            for part in [
                trace.id,
                trace.session_id or "",
                trace.agent,
                trace.host or "",
                trace.domain or "",
                trace.status,
                trace.model,
            ]
            if part
        )

        return (
            trace.task,
            reasoning,
            tools,
            commands,
            errors,
            output,
            files,
            validations,
            learnings,
            meta,
        )

    def _trace_search_snippets(self, row: sqlite3.Row) -> list[str]:
        snippets: list[str] = []
        for _, label in TRACE_FTS_SNIPPETS:
            value = row[f"s_{label.lower()}"]
            if not value or "[[" not in value:
                continue
            cleaned_value = re.sub(r"\s+", " ", value).strip()
            snippets.append(f"{label}: {cleaned_value}")
        return snippets

    # ----- Traces ---------------------------------------------------------- #

    def record_trace(self, trace: Trace, *, write_json: bool = True) -> None:
        payload = json.dumps(to_jsonable(trace), ensure_ascii=False)
        with self._transaction() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                """
                INSERT INTO traces (id, agent, host, domain, status, task, workspace_path, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    agent = excluded.agent,
                    host = excluded.host,
                    domain = excluded.domain,
                    status = excluded.status,
                    task = excluded.task,
                    workspace_path = excluded.workspace_path,
                    created_at = excluded.created_at,
                    payload = excluded.payload
                """,
                (
                    trace.id,
                    trace.agent,
                    trace.host,
                    trace.domain,
                    trace.status,
                    trace.task,
                    trace.workspace_path,
                    trace.created_at.isoformat(),
                    payload,
                ),
            )
            self._update_trace_fts(cur, trace)

        if write_json:
            self._write_trace_json(trace)

    def _update_trace_fts(self, cur: sqlite3.Cursor, trace: Trace) -> None:
        """Update the FTS5 index for a single trace."""
        task, reasoning, tools, commands, errors, output, files, validations, learnings, meta = (
            self._build_trace_search_document(trace)
        )

        cur.execute("DELETE FROM traces_fts WHERE id = ?", (trace.id,))
        cur.execute(
            """
            INSERT INTO traces_fts (
                id,
                task,
                reasoning,
                tools,
                commands,
                errors,
                output,
                files,
                validations,
                learnings,
                meta
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace.id,
                task,
                reasoning,
                tools,
                commands,
                errors,
                output,
                files,
                validations,
                learnings,
                meta,
            ),
        )

    def delete_trace(self, trace_id: str) -> None:
        with self._transaction() as conn, closing(conn.cursor()) as cur:
            cur.execute("DELETE FROM traces WHERE id = ?", (trace_id,))
            cur.execute("DELETE FROM traces_fts WHERE id = ?", (trace_id,))

        trace_json_path = self.traces_dir / f"{trace_id}.json"
        with contextlib.suppress(OSError):
            trace_json_path.unlink()

    def trace_exists(self, trace_id: str) -> bool:
        """Lightweight existence check -- no deserialization."""
        with self._transaction() as conn:
            row = conn.execute("SELECT 1 FROM traces WHERE id = ?", (trace_id,)).fetchone()
        return row is not None

    def get_trace(self, trace_id: str) -> Trace | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT payload FROM traces WHERE id = ?", (trace_id,)).fetchone()
            if row is None:
                # Fallback: check if trace_id was actually a session_id
                row = conn.execute(
                    "SELECT payload FROM traces WHERE json_extract(payload, '$.session_id') = ?",
                    (trace_id,),
                ).fetchone()

        if row is None:
            return None
        return Trace.model_validate_json(coerce_trace_json(row["payload"]))

    def list_unsynced_trace_ids(self, limit: int = 500) -> list[str]:
        """Return IDs of traces that have not been successfully synced."""
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT t.id FROM traces t
                LEFT JOIN sync_status s ON t.id = s.session_id
                WHERE s.session_id IS NULL
                ORDER BY t.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row[0] for row in rows]

    def mark_synced(self, session_id: str, payload_hash: str) -> None:
        """Mark a session as successfully synced."""
        synced_at = datetime.now(UTC).isoformat()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO sync_status (session_id, synced_at, payload_hash)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    synced_at = excluded.synced_at,
                    payload_hash = excluded.payload_hash
                """,
                (session_id, synced_at, payload_hash),
            )

    def list_traces(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        agent: str | None = None,
        host: str | None = None,
        query: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Trace]:
        if query and query.strip():
            search_query = self._build_fts_prefix_query(query)
            sql = (
                "SELECT t.payload, "
                "snippet(traces_fts, 1, '[[', ']]', '...', 12) as s_task, "
                "snippet(traces_fts, 2, '[[', ']]', '...', 12) as s_reasoning, "
                "snippet(traces_fts, 3, '[[', ']]', '...', 12) as s_tools, "
                "snippet(traces_fts, 4, '[[', ']]', '...', 12) as s_commands, "
                "snippet(traces_fts, 5, '[[', ']]', '...', 12) as s_errors, "
                "snippet(traces_fts, 6, '[[', ']]', '...', 12) as s_summary, "
                "snippet(traces_fts, 7, '[[', ']]', '...', 12) as s_files, "
                "snippet(traces_fts, 8, '[[', ']]', '...', 12) as s_validations, "
                "snippet(traces_fts, 9, '[[', ']]', '...', 12) as s_learnings, "
                "snippet(traces_fts, 10, '[[', ']]', '...', 12) as s_run "
                "FROM traces_fts "
                "JOIN traces t ON t.id = traces_fts.id "
                "WHERE traces_fts MATCH ? "
                "AND t.task != 'session-auto-record' "
            )
            params: list[Any] = [search_query]
            if domain:
                sql += " AND t.domain = ?"
                params.append(domain)
            if status:
                sql += " AND t.status = ?"
                params.append(status)
            if agent:
                sql += " AND t.agent = ?"
                params.append(agent)
            if host:
                sql += " AND t.host = ?"
                params.append(host)
            if since:
                sql += " AND t.created_at >= ?"
                params.append(since.isoformat())

            sql += " ORDER BY bm25(traces_fts), t.created_at DESC LIMIT ? OFFSET ?"
            params.append(limit)
            params.append(offset)

            with self._transaction() as conn:
                rows = conn.execute(sql, params).fetchall()

            results = []
            for row in rows:
                trace = Trace.model_validate_json(coerce_trace_json(row["payload"]))
                trace.snippets = self._trace_search_snippets(row)
                results.append(trace)
            return results

        # Standard filter path
        sql = "SELECT payload FROM traces WHERE task != 'session-auto-record' AND 1=1"
        params = []
        if domain:
            sql += " AND domain = ?"
            params.append(domain)
        if status:
            sql += " AND status = ?"
            params.append(status)
        if agent:
            sql += " AND agent = ?"
            params.append(agent)
        if host:
            sql += " AND host = ?"
            params.append(host)
        if since:
            sql += " AND created_at >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Trace.model_validate_json(coerce_trace_json(r["payload"])) for r in rows]

    def get_traces_metrics(
        self,
        *,
        domain: str | None = None,
        agent: str | None = None,
        host: str | None = None,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        """Return aggregate metrics for traces matching the filters."""
        base_sql = "FROM traces WHERE task != 'session-auto-record' AND 1=1"
        params: list[Any] = []
        if domain:
            base_sql += " AND domain = ?"
            params.append(domain)
        if agent:
            base_sql += " AND agent = ?"
            params.append(agent)
        if host:
            base_sql += " AND host = ?"
            params.append(host)
        if since:
            base_sql += " AND created_at >= ?"
            params.append(since.isoformat())

        with self._transaction() as conn:
            # 1. Total and status breakdown
            status_sql = f"SELECT status, COUNT(*) {base_sql} GROUP BY status"
            status_rows = conn.execute(status_sql, params).fetchall()

            # 2. Distinct hosts, agents, and domains
            host_sql = f"SELECT DISTINCT host {base_sql}"
            host_rows = conn.execute(host_sql, params).fetchall()

            agent_sql = f"SELECT DISTINCT agent {base_sql}"
            agent_rows = conn.execute(agent_sql, params).fetchall()

            domain_sql = f"SELECT DISTINCT domain {base_sql}"
            domain_rows = conn.execute(domain_sql, params).fetchall()

        stats = {"total": 0, "success": 0, "failed": 0, "partial": 0}
        for row in status_rows:
            s = row["status"]
            c = row["COUNT(*)"]
            stats["total"] += c
            if s in stats:
                stats[s] = c

        return {
            "stats": stats,
            "hosts": [r["host"] for r in host_rows if r["host"]],
            "agents": [r["agent"] for r in agent_rows if r["agent"]],
            "domains": [r["domain"] for r in domain_rows if r["domain"]],
        }

    def token_rows(self, *, since: datetime | None = None) -> list[dict[str, Any]]:
        """Lightweight per-trace token/host/model rows for cost aggregates."""
        sql = (
            "SELECT id, host, "
            "json_extract(payload, '$.session_id') AS session_id, "
            "json_extract(payload, '$.model') AS model, "
            "json_extract(payload, '$.input_tokens') AS input_tokens, "
            "json_extract(payload, '$.output_tokens') AS output_tokens, "
            "json_extract(payload, '$.cached_input_tokens') AS cached_input_tokens, "
            "json_extract(payload, '$.thinking_tokens') AS thinking_tokens "
            "FROM traces WHERE task != 'session-auto-record'"
        )
        params: list[Any] = []
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since.isoformat())
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_trace_payloads(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return raw trace payload dicts (not parsed ``Trace`` models), newest first."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT payload FROM traces WHERE task != 'session-auto-record' ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    # ----- Raw artifacts -------------------------------------------------- #

    def record_raw_artifact(self, artifact: RawArtifact, content: str) -> None:
        payload = json.dumps(to_jsonable(artifact), ensure_ascii=False)
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO raw_artifacts (
                    id, source, source_session_id, kind, relative_path,
                    content_path, sha256_original, sha256_redacted,
                    byte_count_original, byte_count_redacted,
                    created_at, source_file_mtime, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    source_session_id = excluded.source_session_id,
                    kind = excluded.kind,
                    relative_path = excluded.relative_path,
                    content_path = excluded.content_path,
                    sha256_original = excluded.sha256_original,
                    sha256_redacted = excluded.sha256_redacted,
                    byte_count_original = excluded.byte_count_original,
                    byte_count_redacted = excluded.byte_count_redacted,
                    source_file_mtime = excluded.source_file_mtime,
                    payload = excluded.payload
                """,
                (
                    artifact.id,
                    artifact.source,
                    artifact.source_session_id,
                    artifact.kind,
                    artifact.relative_path,
                    artifact.content_path,
                    artifact.sha256_original,
                    artifact.sha256_redacted,
                    artifact.byte_count_original,
                    artifact.byte_count_redacted,
                    artifact.created_at.isoformat(),
                    artifact.source_file_mtime.isoformat() if artifact.source_file_mtime else None,
                    payload,
                ),
            )
        self._write_raw_artifact(artifact, content)

    def get_raw_artifact(self, artifact_id: str) -> RawArtifact | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT payload FROM raw_artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            return None
        return RawArtifact.model_validate_json(row["payload"])

    def list_raw_artifacts(
        self,
        *,
        source: str | None = None,
        source_session_id: str | None = None,
        limit: int = 100,
    ) -> list[RawArtifact]:
        sql = "SELECT payload FROM raw_artifacts WHERE 1=1"
        params: list[Any] = []
        if source:
            sql += " AND source = ?"
            params.append(source)
        if source_session_id:
            sql += " AND source_session_id = ?"
            params.append(source_session_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [RawArtifact.model_validate_json(r["payload"]) for r in rows]

    def read_raw_artifact_content(self, artifact: RawArtifact) -> str:
        return self._artifact_path(artifact).read_text(encoding="utf-8")

    # ----- File mirrors ----------------------------------------------------- #

    def _write_trace_json(self, trace: Trace) -> None:
        path = self.traces_dir / f"{trace.id}.json"
        path.write_text(
            json.dumps(to_jsonable(trace), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_raw_artifact(self, artifact: RawArtifact, content: str) -> None:
        path = self._artifact_path(artifact)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _artifact_path(self, artifact: RawArtifact) -> Path:
        path = (self.root / artifact.content_path).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise ValueError(f"raw artifact path escapes store root: {artifact.content_path}")
        return path


__all__ = ["HistoryStore"]
