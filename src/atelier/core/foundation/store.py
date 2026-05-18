"""Persistent storage for ReasonBlocks, traces, and rubrics.

Backend: SQLite + FTS5 (no external services).

Design:
- One table per entity, JSON column for the full payload.
- A contentless FTS5 mirror table for ReasonBlocks for fast lookup by
  title / triggers / situation / dead_ends / procedure.
- Markdown copies of blocks live under <root>/blocks/ for human review
  and version control. Traces are mirrored under <root>/traces/.
- Redacted raw artifacts live under <root>/raw/ and are linked from traces
  when a host import preserves more detail than the curated Trace schema.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from atelier.core.foundation.lesson_models import LessonCandidate, LessonPromotion
from atelier.core.foundation.models import (
    BlockStatus,
    ConsolidationCandidate,
    RawArtifact,
    ReasonBlock,
    Rubric,
    Trace,
    coerce_trace_json,
    to_jsonable,
)
from atelier.core.foundation.paths import resolve_knowledge_root

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
    (9, "Run"),
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
    meta,
    tokenize = 'porter'
)
"""

# --------------------------------------------------------------------------- #
# Schema                                                                      #
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS reasonblocks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    domain TEXT NOT NULL,
    status TEXT NOT NULL,
    usage_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reasonblocks_domain ON reasonblocks(domain);
CREATE INDEX IF NOT EXISTS idx_reasonblocks_status ON reasonblocks(status);

CREATE VIRTUAL TABLE IF NOT EXISTS reasonblocks_fts USING fts5(
    id UNINDEXED,
    title,
    triggers,
    situation,
    dead_ends,
    procedure,
    failure_signals,
    tokenize = 'porter'
);

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

CREATE TABLE IF NOT EXISTS rubrics (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lesson_candidate (
    id                     TEXT PRIMARY KEY,
    domain                 TEXT NOT NULL,
    cluster_fingerprint    TEXT NOT NULL DEFAULT '',
    kind                   TEXT NOT NULL,
    target_id              TEXT,
    proposed_block_json    TEXT,
    proposed_rubric_check  TEXT,
    evidence_trace_ids     TEXT NOT NULL,
    body                   TEXT NOT NULL DEFAULT '',
    evidence_json          TEXT NOT NULL DEFAULT '{}',
    embedding              BLOB,
    embedding_provenance   TEXT NOT NULL DEFAULT 'legacy_stub',
    confidence             REAL NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'inbox',
    reviewer               TEXT,
    decision_at            TEXT,
    decision_reason        TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lesson_candidate_domain_status_at
    ON lesson_candidate(domain, status, created_at DESC);

CREATE TABLE IF NOT EXISTS lesson_promotion (
    id                  TEXT PRIMARY KEY,
    lesson_id           TEXT NOT NULL REFERENCES lesson_candidate(id),
    published_block_id  TEXT,
    edited_block_id     TEXT,
    pr_url              TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consolidation_candidate (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,
    affected_block_ids  TEXT NOT NULL,
    proposed_action     TEXT NOT NULL,
    proposed_body       TEXT,
    evidence_json       TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    decided_at          TEXT,
    decided_by          TEXT,
    decision            TEXT
);
CREATE INDEX IF NOT EXISTS ix_consolidation_candidate_pending
    ON consolidation_candidate(decided_at, created_at DESC);

CREATE TABLE IF NOT EXISTS sync_status (
    session_id TEXT PRIMARY KEY,
    synced_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmark_run (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    suite TEXT NOT NULL,
    git_sha TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    n_prompts INTEGER NOT NULL DEFAULT 0,
    median_input_tokens_baseline INTEGER,
    median_input_tokens_optimized INTEGER,
    reduction_pct REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS benchmark_prompt_result (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES benchmark_run(id) ON DELETE CASCADE,
    prompt_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    input_tokens_baseline INTEGER NOT NULL,
    input_tokens_optimized INTEGER NOT NULL,
    reduction_pct REAL NOT NULL,
    duration_ms INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    baseline_input_tokens INTEGER NOT NULL,
    optimized_input_tokens INTEGER NOT NULL,
    lever_attribution_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    locked_by TEXT,
    locked_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_type_status ON jobs(job_type, status, created_at);
"""


# --------------------------------------------------------------------------- #
# Store                                                                       #
# --------------------------------------------------------------------------- #


class ContextStore:
    """SQLite-backed store. Single-process, single-writer.

    The store is also responsible for mirroring blocks/traces to the filesystem
    so they can be reviewed in PRs without running tools.
    """

    def __init__(self, root: Path | str, knowledge_root: Path | str | None = None) -> None:
        self.root = Path(root).resolve()
        self.db_path = self.root / "atelier.db"

        # Knowledge (blocks/rubrics) is project-local by default for Git tracking.
        # History (traces/raw) stays in the primary root.
        _k_root = resolve_knowledge_root(self.root, knowledge_root)
        self.blocks_dir = _k_root / "blocks"
        self.rubrics_dir = _k_root / "rubrics"

        self.traces_dir = self.root / "traces"
        self.raw_dir = self.root / "raw"

        self._initialized = False
        self._connection: sqlite3.Connection | None = None

    @contextlib.contextmanager
    def batch_mode(self) -> Iterator[sqlite3.Connection]:
        """Wrap multiple operations in a single connection and transaction.

        Optimized for bulk imports with high-performance PRAGMAs.
        """
        conn = self._connect()
        # High-performance settings for bulk import (must be outside transaction)
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute(f"PRAGMA cache_size = -{512 * 1024}")  # 512MB cache

        conn.execute("BEGIN TRANSACTION")
        old_conn = self._connection
        self._connection = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._connection = old_conn
            with contextlib.suppress(sqlite3.Error):
                conn.execute("PRAGMA synchronous = NORMAL")
            conn.close()

    # ----- lifecycle ------------------------------------------------------- #

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.blocks_dir.mkdir(parents=True, exist_ok=True)
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        self.rubrics_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Ensure source_file_mtime column exists (migration for existing DBs)
            import contextlib

            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE raw_artifacts ADD COLUMN source_file_mtime TEXT")
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE raw_artifacts ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")

            # Data recovery: Infer host from ID prefix for existing imported runs

            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE traces ADD COLUMN host TEXT")

            from atelier.gateway.hosts.session_parsers.registry import (
                SUPPORTED_SESSION_IMPORT_HOSTS,
            )

            for h in SUPPORTED_SESSION_IMPORT_HOSTS:
                conn.execute("UPDATE traces SET host = ? WHERE id LIKE ? AND host IS NULL", (h, f"{h}-%"))

            # Strip legacy fields (e.g. run_id) from stored trace payloads so old
            # data doesn't crash Trace.model_validate_json() during FTS reindex.
            conn.execute(
                "UPDATE traces SET payload = json_remove(payload, '$.run_id')"
                " WHERE json_extract(payload, '$.run_id') IS NOT NULL"
            )

            recreated_trace_fts = self._ensure_trace_search_schema(conn)
            self._reindex_traces_fts_if_needed(conn, force=recreated_trace_fts)

            for ddl in (
                "ALTER TABLE lesson_candidate ADD COLUMN body TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE lesson_candidate ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'",
                "ALTER TABLE lesson_candidate ADD COLUMN embedding_provenance TEXT NOT NULL DEFAULT 'legacy_stub'",
                "ALTER TABLE archival_passage ADD COLUMN embedding_provenance TEXT NOT NULL DEFAULT 'legacy_stub'",
                "ALTER TABLE memory_block ADD COLUMN deprecated_at TEXT",
                "ALTER TABLE memory_block ADD COLUMN deprecated_by_block_id TEXT",
                "ALTER TABLE memory_block ADD COLUMN deprecation_reason TEXT NOT NULL DEFAULT ''",
            ):
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute(ddl)
            self._apply_v2_migrations(conn)
            self.verify_v2_schema(conn)
            self.sync_knowledge()
        self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        if self._connection:
            return self._connection

        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _apply_v2_migrations(self, conn: sqlite3.Connection) -> None:
        from atelier.infra.storage.migrations import SQLITE_MIGRATIONS, read_migration

        conn.executescript(
            "CREATE TABLE IF NOT EXISTS _schema_migrations" " (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL);"
        )
        applied = {row[0] for row in conn.execute("SELECT name FROM _schema_migrations").fetchall()}
        for name in SQLITE_MIGRATIONS:
            if name in applied:
                continue
            for stmt in (s.strip() for s in read_migration(name).split(";")):
                if not stmt:
                    continue
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    msg = str(exc).lower()
                    if "duplicate column name" not in msg and "already exists" not in msg:
                        raise
            conn.execute(
                "INSERT OR IGNORE INTO _schema_migrations (name, applied_at)" " VALUES (?, datetime('now'))",
                (name,),
            )
            conn.commit()

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
            meta,
        )

    def _build_trace_search_query(self, query: str) -> str:
        clauses: list[str] = []
        for phrase, token in re.findall(r'"([^"]+)"|(\S+)', query):
            term = (phrase or token).strip().lower()
            if not term:
                continue
            if phrase:
                escaped_term = term.replace('"', '""')
                clauses.append(f'"{escaped_term}"')
                continue
            pieces = [piece for piece in re.split(r"[^0-9a-z_]+", term) if piece]
            clauses.extend(f"{piece}*" for piece in pieces)
        if clauses:
            return " AND ".join(clauses)
        escaped = query.strip().replace('"', '""')
        return f'"{escaped}"'

    def _trace_search_snippets(self, row: sqlite3.Row) -> list[str]:
        snippets: list[str] = []
        for _, label in TRACE_FTS_SNIPPETS:
            value = row[f"s_{label.lower()}"]
            if not value or "[[" not in value:
                continue
            cleaned_value = re.sub(r"\s+", " ", value).strip()
            snippets.append(f"{label}: {cleaned_value}")
        return snippets

    def verify_v2_schema(self, conn: sqlite3.Connection | None = None) -> bool:
        """Return True when every V2 table exists in SQLite."""

        from atelier.infra.storage.migrations import V2_REQUIRED_TABLES

        owns_connection = conn is None
        active_conn = conn or self._connect()
        try:
            rows = active_conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type IN ('table', 'virtual table') AND name IN ({})
                """.format(",".join("?" for _ in V2_REQUIRED_TABLES)),
                V2_REQUIRED_TABLES,
            ).fetchall()
            found = {row["name"] for row in rows}
            missing = set(V2_REQUIRED_TABLES) - found
            if missing:
                raise RuntimeError(f"missing V2 tables: {', '.join(sorted(missing))}")
            return True
        finally:
            if owns_connection:
                active_conn.close()

    # ----- ReasonBlocks ---------------------------------------------------- #

    def upsert_block(self, block: ReasonBlock, *, write_markdown: bool = True) -> None:
        payload = json.dumps(to_jsonable(block), ensure_ascii=False)
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                """
                INSERT INTO reasonblocks (
                    id, title, domain, status,
                    usage_count, success_count, failure_count,
                    created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    domain=excluded.domain,
                    status=excluded.status,
                    usage_count=excluded.usage_count,
                    success_count=excluded.success_count,
                    failure_count=excluded.failure_count,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (
                    block.id,
                    block.title,
                    block.domain,
                    block.status,
                    block.usage_count,
                    block.success_count,
                    block.failure_count,
                    block.created_at.isoformat(),
                    block.updated_at.isoformat(),
                    payload,
                ),
            )
            cur.execute("DELETE FROM reasonblocks_fts WHERE id = ?", (block.id,))
            cur.execute(
                """
                INSERT INTO reasonblocks_fts (
                    id, title, triggers, situation, dead_ends, procedure, failure_signals
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    block.id,
                    block.title,
                    " ; ".join(block.triggers),
                    block.situation,
                    " ; ".join(block.dead_ends),
                    " ; ".join(block.procedure),
                    " ; ".join(block.failure_signals),
                ),
            )
        if write_markdown:
            self._write_block_markdown(block)

    def get_block(self, block_id: str) -> ReasonBlock | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM reasonblocks WHERE id = ?", (block_id,)).fetchone()
        if row is None:
            return None
        return ReasonBlock.model_validate_json(row["payload"])

    def list_blocks(
        self,
        *,
        domain: str | None = None,
        status: BlockStatus | None = "active",
        include_deprecated: bool = False,
    ) -> list[ReasonBlock]:
        sql = "SELECT payload FROM reasonblocks WHERE 1=1"
        params: list[Any] = []
        if domain:
            sql += " AND domain = ?"
            params.append(domain)
        if status and not include_deprecated:
            sql += " AND status = ?"
            params.append(status)
        elif not include_deprecated:
            sql += " AND status != 'quarantined'"
        sql += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ReasonBlock.model_validate_json(r["payload"]) for r in rows]

    def search_blocks(self, query: str, *, limit: int = 20) -> list[ReasonBlock]:
        if not query.strip():
            return self.list_blocks()[:limit]
        # Use FTS5 MATCH with safe quoting (escape internal double quotes).
        safe = query.replace('"', '""')
        sql = (
            "SELECT r.payload FROM reasonblocks_fts f "
            "JOIN reasonblocks r ON r.id = f.id "
            "WHERE reasonblocks_fts MATCH ? "
            "AND r.status != 'quarantined' "
            "ORDER BY rank LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (f'"{safe}"', limit)).fetchall()
        return [ReasonBlock.model_validate_json(r["payload"]) for r in rows]

    def update_block_status(self, block_id: str, status: BlockStatus) -> bool:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                "UPDATE reasonblocks SET status = ?, updated_at = ? WHERE id = ?",
                (status, datetime.now(UTC).isoformat(), block_id),
            )
            changed = cur.rowcount > 0
        if changed:
            block = self.get_block(block_id)
            if block:
                self._write_block_markdown(block)
        return changed

    def increment_usage(
        self,
        block_id: str,
        *,
        success: bool | None = None,
    ) -> None:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                "UPDATE reasonblocks SET usage_count = usage_count + 1 WHERE id = ?",
                (block_id,),
            )
            if success is True:
                cur.execute(
                    "UPDATE reasonblocks SET success_count = success_count + 1 WHERE id = ?",
                    (block_id,),
                )
            elif success is False:
                cur.execute(
                    "UPDATE reasonblocks SET failure_count = failure_count + 1 WHERE id = ?",
                    (block_id,),
                )

    def sync_knowledge(self) -> dict[str, int]:
        """Sync blocks and rubrics from the filesystem to the database.

        Uses a file-mtime manifest stored alongside the SQLite DB so that
        unchanged files are skipped on subsequent calls — safe to call
        repeatedly.
        """
        results = {"blocks": 0, "rubrics": 0}

        if self.blocks_dir.exists():
            from atelier.core.foundation.parser import parse_block_markdown

            prev = self._load_sync_manifest("blocks")
            fresh: dict[str, int] = {}

            for path in sorted(self.blocks_dir.rglob("*.md")):
                key = str(path)
                mtime = path.stat().st_mtime_ns
                fresh[key] = mtime
                if prev.get(key) == mtime:
                    continue  # unchanged — skip read/parse/upsert

                try:
                    content = path.read_text(encoding="utf-8")
                    block = parse_block_markdown(content)
                    self.upsert_block(block, write_markdown=False)
                    results["blocks"] += 1
                except Exception as exc:
                    logger.warning("failed to sync knowledge block from %s: %s", path, exc)
                    continue

            self._save_sync_manifest("blocks", fresh)

        if self.rubrics_dir.exists():
            rubric_paths = sorted(self.rubrics_dir.rglob("*.yaml")) + sorted(self.rubrics_dir.rglob("*.yml"))
            prev = self._load_sync_manifest("rubrics")
            fresh_rubrics: dict[str, int] = {}

            for path in rubric_paths:
                key = str(path)
                mtime = path.stat().st_mtime_ns
                fresh_rubrics[key] = mtime
                if prev.get(key) == mtime:
                    continue  # unchanged

                try:
                    content = path.read_text(encoding="utf-8")
                    data = yaml.safe_load(content) or {}
                    rubric = Rubric.model_validate(data)
                    self.upsert_rubric(rubric, write_yaml=False)
                    results["rubrics"] += 1
                except Exception as exc:
                    logger.warning("failed to sync knowledge rubric from %s: %s", path, exc)
                    continue

            self._save_sync_manifest("rubrics", fresh_rubrics)

        return results

    def _sync_manifest_path(self, kind: str) -> Path:
        """Return path to the incremental-sync manifest for *kind*."""
        return self.root / f".knowledge_sync_{kind}.json"

    def _load_sync_manifest(self, kind: str) -> dict[str, int]:
        path = self._sync_manifest_path(kind)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                return {k: int(v) for k, v in raw.items() if isinstance(v, int)}
            except Exception:
                return {}
        return {}

    def _save_sync_manifest(self, kind: str, manifest: dict[str, int]) -> None:
        path = self._sync_manifest_path(kind)
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    # ----- Traces ---------------------------------------------------------- #

    def record_trace(self, trace: Trace, *, write_json: bool = True) -> None:
        payload = json.dumps(to_jsonable(trace), ensure_ascii=False)
        with self._connect() as conn, closing(conn.cursor()) as cur:
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
            # Update FTS index
            self._update_trace_fts(cur, trace)

        if write_json:
            self._write_trace_json(trace)

    def _update_trace_fts(self, cur: sqlite3.Cursor, trace: Trace) -> None:
        """Update the FTS5 index for a single trace."""
        task, reasoning, tools, commands, errors, output, files, validations, meta = self._build_trace_search_document(
            trace
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
                meta
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (trace.id, task, reasoning, tools, commands, errors, output, files, validations, meta),
        )

    def delete_trace(self, trace_id: str) -> None:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute("DELETE FROM traces WHERE id = ?", (trace_id,))
            cur.execute("DELETE FROM traces_fts WHERE id = ?", (trace_id,))

        trace_json_path = self.traces_dir / f"{trace_id}.json"
        with contextlib.suppress(OSError):
            trace_json_path.unlink()

    def trace_exists(self, trace_id: str) -> bool:
        """Lightweight existence check — no deserialization."""
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM traces WHERE id = ?", (trace_id,)).fetchone()
        return row is not None

    def get_trace(self, trace_id: str) -> Trace | None:
        with self._connect() as conn:
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
        with self._connect() as conn:
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
        with self._connect() as conn:
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
            search_query = self._build_trace_search_query(query)
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
                "snippet(traces_fts, 9, '[[', ']]', '...', 12) as s_run "
                "FROM traces_fts "
                "JOIN traces t ON t.id = traces_fts.id "
                "WHERE traces_fts MATCH ? "
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

            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()

            results = []
            for row in rows:
                trace = Trace.model_validate_json(coerce_trace_json(row["payload"]))
                trace.snippets = self._trace_search_snippets(row)
                results.append(trace)
            return results

        # Standard filter path
        sql = "SELECT payload FROM traces WHERE 1=1"
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
        with self._connect() as conn:
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
        base_sql = "FROM traces WHERE 1=1"
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

        with self._connect() as conn:
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

    # ----- Raw artifacts -------------------------------------------------- #

    def record_raw_artifact(self, artifact: RawArtifact, content: str) -> None:
        payload = json.dumps(to_jsonable(artifact), ensure_ascii=False)
        with self._connect() as conn:
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
        with self._connect() as conn:
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
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [RawArtifact.model_validate_json(r["payload"]) for r in rows]

    def read_raw_artifact_content(self, artifact: RawArtifact) -> str:
        return self._artifact_path(artifact).read_text(encoding="utf-8")

    # ----- Rubrics --------------------------------------------------------- #

    def upsert_rubric(self, rubric: Rubric, *, write_yaml: bool = True) -> None:
        payload = json.dumps(to_jsonable(rubric), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rubrics (id, domain, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    domain = excluded.domain,
                    payload = excluded.payload
                """,
                (rubric.id, rubric.domain, payload),
            )
        if write_yaml:
            self._write_rubric_yaml(rubric)

    def get_rubric(self, rubric_id: str) -> Rubric | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM rubrics WHERE id = ?", (rubric_id,)).fetchone()
        if row is None:
            return None
        return Rubric.model_validate_json(row["payload"])

    def list_rubrics(self, *, domain: str | None = None) -> list[Rubric]:
        sql = "SELECT payload FROM rubrics"
        params: list[Any] = []
        if domain:
            sql += " WHERE domain = ?"
            params.append(domain)
        sql += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Rubric.model_validate_json(r["payload"]) for r in rows]

    # ----- Jobs ------------------------------------------------------------ #

    def enqueue_job(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        *,
        max_attempts: int = 3,
    ) -> str:
        job_id = uuid4().hex
        now = datetime.now(UTC).isoformat()
        payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, job_type, payload, status, attempts, max_attempts,
                    locked_by, locked_at, error, created_at, updated_at
                )
                VALUES (?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL, ?, ?)
                """,
                (job_id, job_type, payload_json, max_attempts, now, now),
            )
        return job_id

    def claim_job(self, worker_id: str | None = None) -> dict[str, Any] | None:
        claimed_by = worker_id or f"sqlite-{os.getpid()}"
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""
                SELECT *
                FROM jobs
                WHERE status IN ('pending', 'failed')
                  AND attempts < max_attempts
                ORDER BY created_at ASC
                LIMIT 1
                """).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    locked_by = ?,
                    locked_at = ?,
                    updated_at = ?,
                    error = NULL
                WHERE id = ?
                """,
                (claimed_by, now, now, row["id"]),
            )
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            conn.commit()
        return self._row_to_job(claimed) if claimed is not None else None

    def complete_job(self, job_id: str, result: dict[str, Any] | None = None) -> bool:
        _ = result
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            res = conn.execute(
                """
                UPDATE jobs
                SET status = 'succeeded',
                    locked_by = NULL,
                    locked_at = NULL,
                    error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )
        return (res.rowcount or 0) > 0

    def fail_job(self, job_id: str, error: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            res = conn.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'failed' END,
                    locked_by = NULL,
                    locked_at = NULL,
                    error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, now, job_id),
            )
        return (res.rowcount or 0) > 0

    def list_jobs(
        self,
        *,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if job_type:
            sql += " AND job_type = ?"
            params.append(job_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_job(row) for row in rows]

    # ----- external analytics -------------------------------------------- #

    def record_external_analytics_run(
        self,
        *,
        tool: str,
        period: str,
        source: str,
        ok: bool,
        command_display: str = "",
        returncode: int | None = None,
        summary: dict[str, Any] | None = None,
        payload: Any | None = None,
        stdout: str = "",
        stderr: str = "",
        collected_at: str | None = None,
        replace_period_snapshot: bool = False,
    ) -> str:
        session_id = uuid4().hex
        created_at = datetime.now(UTC).isoformat()
        collected = collected_at or created_at
        with self._connect() as conn:
            if replace_period_snapshot:
                conn.execute(
                    "DELETE FROM external_analytics_runs WHERE tool = ? AND period = ?",
                    (tool, period),
                )
            conn.execute(
                """
                INSERT INTO external_analytics_runs (
                    id, tool, period, source, command_display,
                    ok, returncode, summary_json, payload_json,
                    stdout, stderr, collected_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    tool,
                    period,
                    source,
                    command_display,
                    1 if ok else 0,
                    returncode,
                    json.dumps(summary or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(payload if payload is not None else {}, ensure_ascii=False),
                    stdout,
                    stderr,
                    collected,
                    created_at,
                ),
            )
        return session_id

    def list_external_analytics_runs(
        self,
        *,
        tool: str | None = None,
        period: str | None = None,
        ok: bool | None = None,
        days: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM external_analytics_runs WHERE 1=1"
        params: list[Any] = []
        if tool:
            sql += " AND tool = ?"
            params.append(tool)
        if period:
            sql += " AND period = ?"
            params.append(period)
        if ok is not None:
            sql += " AND ok = ?"
            params.append(1 if ok else 0)
        if days is not None:
            cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            sql += " AND collected_at >= ?"
            params.append(cutoff)
        sql += " ORDER BY collected_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_external_analytics_run(row) for row in rows]

    # ----- Lessons --------------------------------------------------------- #

    def upsert_lesson_candidate(self, candidate: LessonCandidate) -> None:
        proposed_block_json = (
            json.dumps(to_jsonable(candidate.proposed_block), ensure_ascii=False)
            if candidate.proposed_block is not None
            else None
        )
        embedding_json = json.dumps(candidate.embedding, ensure_ascii=False) if candidate.embedding else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lesson_candidate (
                    id, domain, cluster_fingerprint, kind, target_id,
                    proposed_block_json, proposed_rubric_check, evidence_trace_ids,
                    body, evidence_json, embedding, embedding_provenance,
                    confidence, status, reviewer, decision_at,
                    decision_reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    domain = excluded.domain,
                    cluster_fingerprint = excluded.cluster_fingerprint,
                    kind = excluded.kind,
                    target_id = excluded.target_id,
                    proposed_block_json = excluded.proposed_block_json,
                    proposed_rubric_check = excluded.proposed_rubric_check,
                    evidence_trace_ids = excluded.evidence_trace_ids,
                    body = excluded.body,
                    evidence_json = excluded.evidence_json,
                    embedding = excluded.embedding,
                    embedding_provenance = excluded.embedding_provenance,
                    confidence = excluded.confidence,
                    status = excluded.status,
                    reviewer = excluded.reviewer,
                    decision_at = excluded.decision_at,
                    decision_reason = excluded.decision_reason
                """,
                (
                    candidate.id,
                    candidate.domain,
                    candidate.cluster_fingerprint,
                    candidate.kind,
                    candidate.target_id,
                    proposed_block_json,
                    candidate.proposed_rubric_check,
                    json.dumps(candidate.evidence_trace_ids, ensure_ascii=False),
                    candidate.body,
                    json.dumps(candidate.evidence, ensure_ascii=False, sort_keys=True),
                    embedding_json,
                    candidate.embedding_provenance,
                    candidate.confidence,
                    candidate.status,
                    candidate.reviewer,
                    candidate.decision_at.isoformat() if candidate.decision_at else None,
                    candidate.decision_reason,
                    candidate.created_at.isoformat(),
                ),
            )

    def get_lesson_candidate(self, lesson_id: str) -> LessonCandidate | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM lesson_candidate WHERE id = ?", (lesson_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_lesson_candidate(row)

    def list_lesson_candidates(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[LessonCandidate]:
        sql = "SELECT * FROM lesson_candidate WHERE 1=1"
        params: list[Any] = []
        if domain:
            sql += " AND domain = ?"
            params.append(domain)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_lesson_candidate(r) for r in rows]

    def upsert_lesson_promotion(self, promotion: LessonPromotion) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lesson_promotion (
                    id, lesson_id, published_block_id, edited_block_id, pr_url, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    lesson_id = excluded.lesson_id,
                    published_block_id = excluded.published_block_id,
                    edited_block_id = excluded.edited_block_id,
                    pr_url = excluded.pr_url
                """,
                (
                    promotion.id,
                    promotion.lesson_id,
                    promotion.published_block_id,
                    promotion.edited_block_id,
                    promotion.pr_url,
                    promotion.created_at.isoformat(),
                ),
            )

    def list_lesson_promotions(self, *, limit: int = 100) -> list[LessonPromotion]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lesson_promotion ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            LessonPromotion(
                id=r["id"],
                lesson_id=r["lesson_id"],
                published_block_id=r["published_block_id"],
                edited_block_id=r["edited_block_id"],
                pr_url=r["pr_url"] or "",
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ----- Consolidation candidates -------------------------------------- #

    def upsert_consolidation_candidate(self, candidate: ConsolidationCandidate) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO consolidation_candidate (
                    id, kind, affected_block_ids, proposed_action, proposed_body,
                    evidence_json, created_at, decided_at, decided_by, decision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    affected_block_ids = excluded.affected_block_ids,
                    proposed_action = excluded.proposed_action,
                    proposed_body = excluded.proposed_body,
                    evidence_json = excluded.evidence_json,
                    decided_at = excluded.decided_at,
                    decided_by = excluded.decided_by,
                    decision = excluded.decision
                """,
                (
                    candidate.id,
                    candidate.kind,
                    json.dumps(candidate.affected_block_ids, ensure_ascii=False),
                    candidate.proposed_action,
                    candidate.proposed_body,
                    json.dumps(candidate.evidence, ensure_ascii=False, sort_keys=True),
                    candidate.created_at.isoformat(),
                    candidate.decided_at.isoformat() if candidate.decided_at else None,
                    candidate.decided_by,
                    candidate.decision,
                ),
            )

    def list_consolidation_candidates(
        self, *, pending_only: bool = True, limit: int = 100
    ) -> list[ConsolidationCandidate]:
        sql = "SELECT * FROM consolidation_candidate"
        if pending_only:
            sql += " WHERE decided_at IS NULL"
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [self._row_to_consolidation_candidate(row) for row in rows]

    def get_consolidation_candidate(self, candidate_id: str) -> ConsolidationCandidate | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM consolidation_candidate WHERE id = ?", (candidate_id,)).fetchone()
        return self._row_to_consolidation_candidate(row) if row is not None else None

    def _row_to_job(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = row["payload"]
        return {
            "id": row["id"],
            "job_type": row["job_type"],
            "payload": json.loads(payload) if isinstance(payload, str) else (payload or {}),
            "status": row["status"],
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "locked_by": row["locked_by"],
            "locked_at": row["locked_at"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_external_analytics_run(self, row: sqlite3.Row) -> dict[str, Any]:
        def _load_json(raw: Any, fallback: Any) -> Any:
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return fallback
            return raw if raw is not None else fallback

        return {
            "id": row["id"],
            "tool": row["tool"],
            "period": row["period"],
            "source": row["source"],
            "command_display": row["command_display"],
            "ok": bool(row["ok"]),
            "returncode": row["returncode"],
            "summary": _load_json(row["summary_json"], {}),
            "payload": _load_json(row["payload_json"], {}),
            "stdout": row["stdout"] or "",
            "stderr": row["stderr"] or "",
            "collected_at": row["collected_at"],
            "created_at": row["created_at"],
        }

    def _row_to_consolidation_candidate(self, row: sqlite3.Row) -> ConsolidationCandidate:
        return ConsolidationCandidate(
            id=row["id"],
            kind=row["kind"],
            affected_block_ids=json.loads(row["affected_block_ids"] or "[]"),
            proposed_action=row["proposed_action"],
            proposed_body=row["proposed_body"],
            evidence=json.loads(row["evidence_json"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]),
            decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
            decided_by=row["decided_by"],
            decision=row["decision"],
        )

    def _row_to_lesson_candidate(self, row: sqlite3.Row) -> LessonCandidate:
        row_keys = set(row.keys())
        proposed_block = None
        if row["proposed_block_json"]:
            proposed_block = ReasonBlock.model_validate_json(row["proposed_block_json"])
        embedding = None
        if row["embedding"]:
            raw_embedding = row["embedding"]
            if isinstance(raw_embedding, bytes):
                raw_embedding = raw_embedding.decode("utf-8", errors="replace")
            embedding = json.loads(raw_embedding)
        decision_at = datetime.fromisoformat(row["decision_at"]) if row["decision_at"] else None
        return LessonCandidate(
            id=row["id"],
            domain=row["domain"],
            cluster_fingerprint=row["cluster_fingerprint"] or "",
            kind=row["kind"],
            target_id=row["target_id"],
            proposed_block=proposed_block,
            proposed_rubric_check=row["proposed_rubric_check"],
            evidence_trace_ids=json.loads(row["evidence_trace_ids"]),
            body=row["body"] if "body" in row_keys else "",
            evidence=(json.loads(row["evidence_json"] or "{}") if "evidence_json" in row_keys else {}),
            embedding=embedding,
            embedding_provenance=(row["embedding_provenance"] if "embedding_provenance" in row_keys else "legacy_stub"),
            confidence=float(row["confidence"]),
            status=row["status"],
            reviewer=row["reviewer"],
            decision_at=decision_at,
            decision_reason=row["decision_reason"] or "",
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ----- File mirrors ---------------------------------------------------- #

    def _write_block_markdown(self, block: ReasonBlock) -> None:
        path = self.blocks_dir / f"{block.id}.md"
        from atelier.core.foundation.renderer import render_block_markdown

        path.write_text(render_block_markdown(block), encoding="utf-8")

    def _write_trace_json(self, trace: Trace) -> None:
        path = self.traces_dir / f"{trace.id}.json"
        path.write_text(
            json.dumps(to_jsonable(trace), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_rubric_yaml(self, rubric: Rubric) -> None:
        path = self.rubrics_dir / f"{rubric.id}.yaml"
        path.write_text(
            yaml.safe_dump(to_jsonable(rubric), sort_keys=False),
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

    # ----- Bulk import ---------------------------------------------------- #

    def import_blocks(self, blocks: Iterable[ReasonBlock]) -> int:
        n = 0
        for b in blocks:
            self.upsert_block(b)
            n += 1
        return n

    def import_rubrics(self, rubrics: Iterable[Rubric]) -> int:
        n = 0
        for r in rubrics:
            self.upsert_rubric(r)
            n += 1
        return n

    # ----- Context Budget -------------------------------------------------- #

    def persist_context_budget(self, record: Any) -> None:
        """Persist a ContextBudget record to the store.

        Args:
            record: A ContextBudget instance with session_id, turn_index, model,
                    token counts, lever_savings dict, and tool_calls count.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO context_budget (
                    id, session_id, turn_index, model, input_tokens,
                    cache_read_tokens, cache_write_tokens, output_tokens,
                    naive_input_tokens, lever_savings_json, tool_calls, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.session_id,
                    record.turn_index,
                    record.model,
                    record.input_tokens,
                    record.cache_read_tokens,
                    record.cache_write_tokens,
                    record.output_tokens,
                    record.naive_input_tokens,
                    json.dumps(record.lever_savings),
                    record.tool_calls,
                    record.created_at.isoformat(),
                ),
            )
            conn.commit()

    def list_context_budgets(self, session_id: str) -> list[Any]:
        """List all ContextBudget records for a run.

        Args:
            session_id: The run identifier.

        Returns:
            A list of ContextBudget records (as dicts), ordered by turn_index.
        """
        from atelier.core.foundation.savings_models import ContextBudget

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, turn_index, model, input_tokens,
                       cache_read_tokens, cache_write_tokens, output_tokens,
                       naive_input_tokens, lever_savings_json, tool_calls, created_at
                FROM context_budget
                WHERE session_id = ?
                ORDER BY turn_index ASC
                """,
                (session_id,),
            ).fetchall()

        results = []
        for row in rows:
            results.append(
                ContextBudget(
                    id=row[0],
                    session_id=row[1],
                    turn_index=row[2],
                    model=row[3],
                    input_tokens=row[4],
                    cache_read_tokens=row[5],
                    cache_write_tokens=row[6],
                    output_tokens=row[7],
                    naive_input_tokens=row[8],
                    lever_savings=json.loads(row[9]),
                    tool_calls=row[10],
                    created_at=datetime.fromisoformat(row[11]),
                )
            )

        return results

    def get_context_budget(self, cb_id: str) -> Any | None:
        """Get a single ContextBudget record by ID.

        Args:
            cb_id: The ContextBudget ID.

        Returns:
            A ContextBudget instance or None if not found.
        """
        from atelier.core.foundation.savings_models import ContextBudget

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, session_id, turn_index, model, input_tokens,
                       cache_read_tokens, cache_write_tokens, output_tokens,
                       naive_input_tokens, lever_savings_json, tool_calls, created_at
                FROM context_budget
                WHERE id = ?
                """,
                (cb_id,),
            ).fetchone()

        if row is None:
            return None

        return ContextBudget(
            id=row[0],
            session_id=row[1],
            turn_index=row[2],
            model=row[3],
            input_tokens=row[4],
            cache_read_tokens=row[5],
            cache_write_tokens=row[6],
            output_tokens=row[7],
            naive_input_tokens=row[8],
            lever_savings=json.loads(row[9]),
            tool_calls=row[10],
            created_at=datetime.fromisoformat(row[11]),
        )
