"""PostgresStore — optional Postgres backend for Atelier.

Import guard: psycopg (v3) is imported lazily inside __init__ so that
importing this module at type-check time or when psycopg is not installed
does not raise ImportError.

Usage:
    from atelier.infra.storage.postgres_store import PostgresStore
    store = PostgresStore(database_url="postgresql://user:pass@host/db")
    store.init()   # creates tables if they don't exist

All 15 production tables are created by init().  Existing tests continue
to run against SQLite; Postgres tests are skipped when ATELIER_DATABASE_URL
is not set.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from atelier.core.foundation.models import (
    CommandRecord,
    FileEditRecord,
    Playbook,
    PlaybookStatus,
    Rubric,
    ToolCall,
    Trace,
    to_jsonable,
)
from atelier.infra.storage.migrations import (
    V2_REQUIRED_TABLES,
    postgres_migration_scripts,
    postgres_vector_script,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Optional import guard                                                       #
# --------------------------------------------------------------------------- #

_psycopg: Any = None  # will be set to the psycopg module on successful import

try:
    import psycopg as _psycopg_module

    _psycopg = _psycopg_module
except ImportError:
    logger.warning(
        "Suppressed exception at postgres_store.py:53",
        exc_info=True,
    )

# --------------------------------------------------------------------------- #
# Production DDL (15 tables)                                                  #
# --------------------------------------------------------------------------- #

SCHEMA_DDL = """
-- 1. projects
CREATE TABLE IF NOT EXISTS projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    root_path   TEXT,
    repo_url    TEXT,
    default_branch TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. playbooks
CREATE TABLE IF NOT EXISTS playbooks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID REFERENCES projects(id) ON DELETE SET NULL,
    slug            TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    domain          TEXT NOT NULL,
    scope           TEXT,
    task_types      JSONB NOT NULL DEFAULT '[]',
    triggers        JSONB NOT NULL DEFAULT '[]',
    file_patterns   JSONB NOT NULL DEFAULT '[]',
    tool_patterns   JSONB NOT NULL DEFAULT '[]',
    situation       TEXT NOT NULL,
    dead_ends       JSONB NOT NULL DEFAULT '[]',
    procedure       JSONB NOT NULL DEFAULT '[]',
    verification    JSONB NOT NULL DEFAULT '[]',
    failure_signals JSONB NOT NULL DEFAULT '[]',
    when_not_to_apply TEXT,
    severity        TEXT NOT NULL DEFAULT 'medium',
    status          TEXT NOT NULL DEFAULT 'active',
    source_trace_ids UUID[] NOT NULL DEFAULT '{}',
    usage_count     INT NOT NULL DEFAULT 0,
    success_count   INT NOT NULL DEFAULT 0,
    failure_count   INT NOT NULL DEFAULT 0,
    embedding       vector,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_playbook_domain_status ON playbooks(project_id, domain, status);
CREATE INDEX IF NOT EXISTS idx_playbook_slug ON playbooks(slug);
CREATE INDEX IF NOT EXISTS idx_playbook_metadata ON playbooks USING gin(metadata);

-- 3. rubrics
CREATE TABLE IF NOT EXISTS rubrics (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id           UUID REFERENCES projects(id) ON DELETE SET NULL,
    slug                 TEXT UNIQUE NOT NULL,
    title                TEXT NOT NULL,
    domain               TEXT NOT NULL,
    required_checks      JSONB NOT NULL DEFAULT '[]',
    block_if_missing     JSONB NOT NULL DEFAULT '[]',
    warning_checks       JSONB NOT NULL DEFAULT '[]',
    escalation_conditions JSONB NOT NULL DEFAULT '[]',
    check_definitions    JSONB NOT NULL DEFAULT '{}',
    status               TEXT NOT NULL DEFAULT 'active',
    metadata             JSONB NOT NULL DEFAULT '{}',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4. environments
CREATE TABLE IF NOT EXISTS environments (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id             UUID REFERENCES projects(id) ON DELETE SET NULL,
    slug                   TEXT UNIQUE NOT NULL,
    domain                 TEXT NOT NULL,
    description            TEXT NOT NULL DEFAULT '',
    required_playbooks  JSONB NOT NULL DEFAULT '[]',
    default_rubrics        JSONB NOT NULL DEFAULT '[]',
    tool_policy            JSONB NOT NULL DEFAULT '{}',
    escalation_rules       JSONB NOT NULL DEFAULT '[]',
    eval_cases             JSONB NOT NULL DEFAULT '[]',
    reference_trace_ids    UUID[] NOT NULL DEFAULT '{}',
    status                 TEXT NOT NULL DEFAULT 'active',
    metadata               JSONB NOT NULL DEFAULT '{}',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. traces
CREATE TABLE IF NOT EXISTS traces (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        UUID REFERENCES projects(id) ON DELETE SET NULL,
    session_id            TEXT,
    agent             TEXT NOT NULL,
    adapter           TEXT NOT NULL DEFAULT '',
    domain            TEXT NOT NULL,
    task              TEXT NOT NULL,
    status            TEXT NOT NULL,
    files_touched     JSONB NOT NULL DEFAULT '[]',
    tools_called      JSONB NOT NULL DEFAULT '[]',
    commands_run      JSONB NOT NULL DEFAULT '[]',
    errors_seen       JSONB NOT NULL DEFAULT '[]',
    repeated_failures JSONB NOT NULL DEFAULT '[]',
    diff_summary      TEXT,
    output_summary    TEXT,
    validation_results JSONB NOT NULL DEFAULT '[]',
    token_usage       JSONB NOT NULL DEFAULT '{}',
    cost_estimate     NUMERIC,
    latency_ms        INT,
    redaction_status  TEXT NOT NULL DEFAULT 'unredacted',
    metadata          JSONB NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_traces_project_domain ON traces(project_id, domain, status, created_at);

-- 6. trace_events
CREATE TABLE IF NOT EXISTS trace_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id        UUID NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    event_index     INT NOT NULL,
    event_type      TEXT NOT NULL,
    payload_redacted JSONB NOT NULL DEFAULT '{}',
    error_signature TEXT,
    token_estimate  INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_te_trace_idx ON trace_events(trace_id, event_index);
CREATE INDEX IF NOT EXISTS idx_te_error_sig ON trace_events(error_signature);

-- 7. block_applications
CREATE TABLE IF NOT EXISTS block_applications (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    playbook_id   UUID NOT NULL,
    trace_id         UUID NOT NULL,
    project_id       UUID REFERENCES projects(id) ON DELETE SET NULL,
    injection_point  TEXT NOT NULL DEFAULT '',
    was_injected     BOOLEAN NOT NULL DEFAULT false,
    outcome          TEXT NOT NULL DEFAULT 'unknown',
    evidence         JSONB NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 8. monitor_events
CREATE TABLE IF NOT EXISTS monitor_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id        UUID,
    project_id      UUID REFERENCES projects(id) ON DELETE SET NULL,
    monitor_name    TEXT NOT NULL,
    severity        TEXT NOT NULL,
    event_payload   JSONB NOT NULL DEFAULT '{}',
    action_taken    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_me_project_name ON monitor_events(project_id, monitor_name, created_at);

-- 9. failure_clusters
CREATE TABLE IF NOT EXISTS failure_clusters (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id              UUID REFERENCES projects(id) ON DELETE SET NULL,
    domain                  TEXT NOT NULL,
    symptom                 TEXT NOT NULL,
    root_cause              TEXT NOT NULL DEFAULT '',
    evidence_trace_ids      UUID[] NOT NULL DEFAULT '{}',
    affected_files          JSONB NOT NULL DEFAULT '[]',
    affected_tools          JSONB NOT NULL DEFAULT '[]',
    suggested_playbook   JSONB,
    suggested_rubric        JSONB,
    suggested_eval_cases    JSONB,
    suggested_prompt_patch  TEXT,
    severity                TEXT NOT NULL DEFAULT 'medium',
    confidence              NUMERIC NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'open',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fc_project_domain ON failure_clusters(project_id, domain, status);

-- 10. eval_cases
CREATE TABLE IF NOT EXISTS eval_cases (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        UUID REFERENCES projects(id) ON DELETE SET NULL,
    domain            TEXT NOT NULL,
    title             TEXT NOT NULL,
    task              TEXT NOT NULL,
    setup             JSONB NOT NULL DEFAULT '{}',
    expected_behavior TEXT NOT NULL DEFAULT '',
    forbidden_behavior TEXT NOT NULL DEFAULT '',
    required_evidence JSONB NOT NULL DEFAULT '[]',
    grader_type       TEXT NOT NULL DEFAULT 'manual',
    pass_criteria     JSONB NOT NULL DEFAULT '{}',
    source_trace_ids  UUID[] NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'active',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ec_project_domain ON eval_cases(project_id, domain, status);

-- 11. eval_runs
CREATE TABLE IF NOT EXISTS eval_runs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    UUID REFERENCES projects(id) ON DELETE SET NULL,
    eval_case_id  UUID REFERENCES eval_cases(id) ON DELETE SET NULL,
    suite         TEXT,
    mode          TEXT NOT NULL DEFAULT 'manual',
    status        TEXT NOT NULL DEFAULT 'pending',
    result        JSONB NOT NULL DEFAULT '{}',
    metrics       JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 12. savings_events
CREATE TABLE IF NOT EXISTS savings_events (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id             UUID REFERENCES projects(id) ON DELETE SET NULL,
    trace_id               UUID,
    event_type             TEXT NOT NULL,
    raw_tool_calls         INT,
    avoided_tool_calls     INT,
    estimated_tokens_saved INT,
    estimated_cost_saved   NUMERIC,
    metadata               JSONB NOT NULL DEFAULT '{}',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 13. run_ledgers
CREATE TABLE IF NOT EXISTS run_ledgers (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
    session_id     TEXT UNIQUE NOT NULL,
    task       TEXT NOT NULL,
    domain     TEXT,
    state      JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 14. audit_log
CREATE TABLE IF NOT EXISTS audit_log (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id       UUID REFERENCES projects(id) ON DELETE SET NULL,
    actor            TEXT NOT NULL,
    action           TEXT NOT NULL,
    resource_type    TEXT NOT NULL,
    resource_id      TEXT,
    payload_redacted JSONB NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 15. jobs
CREATE TABLE IF NOT EXISTS jobs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type    TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    locked_by   TEXT,
    locked_at   TIMESTAMPTZ,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
"""

# DDL to enable pgvector (applied only when ATELIER_VECTOR_SEARCH_ENABLED=true)
VECTOR_EXTENSION_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE playbooks
    ALTER COLUMN embedding TYPE vector({dim});
CREATE INDEX IF NOT EXISTS idx_playbook_embedding ON playbooks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
"""


# --------------------------------------------------------------------------- #
# PostgresStore                                                               #
# --------------------------------------------------------------------------- #


class PostgresStore:
    """Postgres-backed store.

    Requires psycopg (v3) to be installed::

        uv add psycopg[binary]

    When psycopg is not available, instantiating this class raises
    ``RuntimeError`` with a helpful message.  Importing the module is safe
    and won't raise at import time.
    """

    def __init__(
        self,
        *,
        database_url: str | None = None,
        vector_search_enabled: bool | None = None,
        embedding_dim: int | None = None,
    ) -> None:
        if _psycopg is None:
            raise RuntimeError(
                "psycopg (v3) is required for Postgres storage. Install it with: uv add 'psycopg[binary]'"
            )

        self._url = database_url or os.environ.get("ATELIER_DATABASE_URL", "")
        if not self._url:
            raise ValueError("database_url or ATELIER_DATABASE_URL must be set for PostgresStore")

        _vs_env = os.environ.get("ATELIER_VECTOR_SEARCH_ENABLED", "false").lower()
        self._vector_search = (
            vector_search_enabled if vector_search_enabled is not None else _vs_env in ("1", "true", "yes")
        )
        self._embedding_dim = embedding_dim or int(os.environ.get("ATELIER_EMBEDDING_DIM", "1536"))

        # Filesystem mirrors (optional, off by default for Postgres)
        from atelier.core.foundation.paths import default_store_root

        self.root = Path(os.environ.get("ATELIER_ROOT", str(default_store_root()))).resolve()
        self.blocks_dir = self.root / "blocks"
        self.traces_dir = self.root / "traces"
        self.rubrics_dir = self.root / "rubrics"

    # ----- lifecycle ------------------------------------------------------- #

    def _connect(self) -> Any:
        """Return a new psycopg connection (autocommit=False)."""
        return _psycopg.connect(self._url)

    def init(self) -> None:
        """Create tables and (optionally) enable pgvector."""
        with self._connect() as conn:
            conn.execute(SCHEMA_DDL)
            for sql in postgres_migration_scripts():
                conn.execute(sql)
            if self._vector_search:
                self.ensure_vector_index(conn)
            self.verify_v2_schema(conn)
            conn.commit()

    def ensure_vector_index(self, conn: Any | None = None) -> bool:
        """Best-effort pgvector setup for archival passage embeddings."""

        owns_connection = conn is None
        active_conn = conn or self._connect()
        try:
            try:
                active_conn.execute(postgres_vector_script(dim=self._embedding_dim))
                if owns_connection:
                    active_conn.commit()
                return True
            except Exception:
                logging.exception("Recovered from broad exception handler")
                if owns_connection:
                    active_conn.rollback()
                return False
        finally:
            if owns_connection:
                active_conn.close()

    def verify_v2_schema(self, conn: Any | None = None) -> bool:
        """Return True when every V2 base table exists in Postgres."""

        owns_connection = conn is None
        active_conn = conn or self._connect()
        try:
            rows = active_conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = ANY(%s)
                """,
                (list(V2_REQUIRED_TABLES),),
            ).fetchall()
            found = {row[0] for row in rows}
            missing = set(V2_REQUIRED_TABLES) - found
            if missing:
                raise RuntimeError(f"missing V2 tables: {', '.join(sorted(missing))}")
            return True
        finally:
            if owns_connection:
                active_conn.close()

    def health_check(self) -> dict[str, Any]:
        """Return basic health information."""
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*) FROM playbooks").fetchone()
                block_count = row[0] if row else 0
            return {
                "ok": True,
                "backend": "postgres",
                "block_count": block_count,
                "vector_search": self._vector_search,
            }
        except Exception as exc:
            logging.exception("Recovered from broad exception handler")
            return {"ok": False, "backend": "postgres", "error": str(exc)}

    # ----- playbooks ------------------------------------------------------ #

    def upsert_block(self, block: Playbook, *, write_markdown: bool = False) -> None:
        payload = to_jsonable(block)
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO playbooks (
                    slug, title, domain, situation, status,
                    task_types, triggers, file_patterns, tool_patterns,
                    dead_ends, procedure, verification, failure_signals,
                    when_not_to_apply, usage_count, success_count, failure_count,
                    metadata, created_at, updated_at
                )
                VALUES (
                    %(slug)s, %(title)s, %(domain)s, %(situation)s, %(status)s,
                    %(task_types)s, %(triggers)s, %(file_patterns)s, %(tool_patterns)s,
                    %(dead_ends)s, %(procedure)s, %(verification)s, %(failure_signals)s,
                    %(when_not_to_apply)s, %(usage_count)s, %(success_count)s,
                    %(failure_count)s, %(metadata)s, %(created_at)s, %(updated_at)s
                )
                ON CONFLICT(slug) DO UPDATE SET
                    title = EXCLUDED.title,
                    domain = EXCLUDED.domain,
                    situation = EXCLUDED.situation,
                    status = EXCLUDED.status,
                    task_types = EXCLUDED.task_types,
                    triggers = EXCLUDED.triggers,
                    file_patterns = EXCLUDED.file_patterns,
                    tool_patterns = EXCLUDED.tool_patterns,
                    dead_ends = EXCLUDED.dead_ends,
                    procedure = EXCLUDED.procedure,
                    verification = EXCLUDED.verification,
                    failure_signals = EXCLUDED.failure_signals,
                    when_not_to_apply = EXCLUDED.when_not_to_apply,
                    usage_count = EXCLUDED.usage_count,
                    success_count = EXCLUDED.success_count,
                    failure_count = EXCLUDED.failure_count,
                    updated_at = EXCLUDED.updated_at
                """,
                {
                    "slug": block.id,
                    "title": block.title,
                    "domain": block.domain,
                    "situation": block.situation,
                    "status": block.status,
                    "task_types": json.dumps(payload.get("task_types", [])),
                    "triggers": json.dumps(payload.get("triggers", [])),
                    "file_patterns": json.dumps(payload.get("file_patterns", [])),
                    "tool_patterns": json.dumps(payload.get("tool_patterns", [])),
                    "dead_ends": json.dumps(payload.get("dead_ends", [])),
                    "procedure": json.dumps(payload.get("procedure", [])),
                    "verification": json.dumps(payload.get("verification", [])),
                    "failure_signals": json.dumps(payload.get("failure_signals", [])),
                    "when_not_to_apply": block.when_not_to_apply or None,
                    "usage_count": block.usage_count,
                    "success_count": block.success_count,
                    "failure_count": block.failure_count,
                    "metadata": json.dumps({"tier": block.tier}),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            conn.commit()

    def get_block(self, block_id: str) -> Playbook | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM playbooks WHERE slug = %s", (block_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_block(row)

    def list_blocks(
        self,
        *,
        domain: str | None = None,
        status: PlaybookStatus | None = "active",
        include_deprecated: bool = False,
    ) -> list[Playbook]:
        sql = "SELECT * FROM playbooks WHERE 1=1"
        params: list[Any] = []
        if domain:
            sql += " AND domain = %s"
            params.append(domain)
        if status and not include_deprecated:
            sql += " AND status = %s"
            params.append(status)
        elif not include_deprecated:
            sql += " AND status != 'quarantined'"
        sql += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_block(r) for r in rows]

    def search_blocks(self, query: str, *, limit: int = 20) -> list[Playbook]:
        """Full-text search via Postgres tsvector."""
        if not query.strip():
            return self.list_blocks()[:limit]
        sql = """
            SELECT * FROM playbooks
            WHERE to_tsvector('english', title || ' ' || situation) @@ plainto_tsquery(%s)
              AND status != 'quarantined'
            LIMIT %s
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (query, limit)).fetchall()
        return [self._row_to_block(r) for r in rows]

    def update_block_status(self, block_id: str, status: PlaybookStatus) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE playbooks SET status = %s, updated_at = %s WHERE slug = %s",
                (status, now, block_id),
            )
            conn.commit()
        return (result.rowcount or 0) > 0

    def delete_block(self, block_id: str) -> bool:
        """Hard-delete a Playbook; return True if a row was removed."""
        with self._connect() as conn:
            result = conn.execute("DELETE FROM playbooks WHERE slug = %s", (block_id,))
            conn.commit()
        return (result.rowcount or 0) > 0

    def increment_usage(self, block_id: str, *, success: bool | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE playbooks SET usage_count = usage_count + 1 WHERE slug = %s",
                (block_id,),
            )
            if success is True:
                conn.execute(
                    "UPDATE playbooks SET success_count = success_count + 1 WHERE slug = %s",
                    (block_id,),
                )
            elif success is False:
                conn.execute(
                    "UPDATE playbooks SET failure_count = failure_count + 1 WHERE slug = %s",
                    (block_id,),
                )
            conn.commit()

    # ----- traces ---------------------------------------------------------- #

    def record_trace(self, trace: Trace, *, write_json: bool = False) -> None:
        payload = to_jsonable(trace)
        metadata = {"learnings": payload.get("learnings", [])}
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO traces (
                    session_id, agent, adapter, domain, task, status,
                    files_touched, tools_called, commands_run, errors_seen,
                    repeated_failures, diff_summary, output_summary,
                    validation_results, metadata, created_at, updated_at
                )
                VALUES (
                    %(session_id)s, %(agent)s, %(adapter)s, %(domain)s, %(task)s, %(status)s,
                    %(files_touched)s, %(tools_called)s, %(commands_run)s, %(errors_seen)s,
                    %(repeated_failures)s, %(diff_summary)s, %(output_summary)s,
                    %(validation_results)s, %(metadata)s, %(created_at)s, %(updated_at)s
                )
                ON CONFLICT DO NOTHING
                """,
                {
                    "session_id": trace.id,
                    "agent": trace.agent,
                    "adapter": "",
                    "domain": trace.domain,
                    "task": trace.task,
                    "status": trace.status,
                    "files_touched": json.dumps(payload.get("files_touched", [])),
                    "tools_called": json.dumps(payload.get("tools_called", [])),
                    "commands_run": json.dumps(payload.get("commands_run", [])),
                    "errors_seen": json.dumps(payload.get("errors_seen", [])),
                    "repeated_failures": json.dumps(payload.get("repeated_failures", [])),
                    "diff_summary": trace.diff_summary or None,
                    "output_summary": trace.output_summary or None,
                    "validation_results": json.dumps(payload.get("validation_results", [])),
                    "metadata": json.dumps(metadata),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            conn.commit()

    def get_trace(self, trace_id: str) -> Trace | None:
        with self._connect() as conn:
            # Try session_id first (most common for routing)
            row = conn.execute("SELECT * FROM traces WHERE session_id = %s", (trace_id,)).fetchone()
            if row is None:
                # Fallback to direct ID match
                row = conn.execute("SELECT * FROM traces WHERE id = %s", (trace_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_trace(row)

    def list_traces(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        sql = "SELECT * FROM traces WHERE task != 'session-auto-record' AND 1=1"
        params: list[Any] = []
        if domain:
            sql += " AND domain = %s"
            params.append(domain)
        if status:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_trace(r) for r in rows]

    # ----- rubrics --------------------------------------------------------- #

    def upsert_rubric(self, rubric: Rubric, *, write_yaml: bool = False) -> None:
        payload = to_jsonable(rubric)
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rubrics (
                    slug, title, domain,
                    required_checks, block_if_missing, warning_checks,
                    escalation_conditions, check_definitions,
                    metadata, created_at, updated_at
                )
                VALUES (
                    %(slug)s, %(title)s, %(domain)s,
                    %(required_checks)s, %(block_if_missing)s, %(warning_checks)s,
                    %(escalation_conditions)s, %(check_definitions)s,
                    %(metadata)s, %(created_at)s, %(updated_at)s
                )
                ON CONFLICT(slug) DO UPDATE SET
                    domain = EXCLUDED.domain,
                    required_checks = EXCLUDED.required_checks,
                    block_if_missing = EXCLUDED.block_if_missing,
                    warning_checks = EXCLUDED.warning_checks,
                    escalation_conditions = EXCLUDED.escalation_conditions,
                    updated_at = EXCLUDED.updated_at
                """,
                {
                    "slug": rubric.id,
                    "title": rubric.id,
                    "domain": rubric.domain,
                    "required_checks": json.dumps(payload.get("required_checks", [])),
                    "block_if_missing": json.dumps(payload.get("block_if_missing", [])),
                    "warning_checks": json.dumps(payload.get("warning_checks", [])),
                    "escalation_conditions": json.dumps(payload.get("escalation_conditions", [])),
                    "check_definitions": "{}",
                    "metadata": "{}",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            conn.commit()

    def get_rubric(self, rubric_id: str) -> Rubric | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM rubrics WHERE slug = %s", (rubric_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_rubric(row)

    def list_rubrics(self, *, domain: str | None = None) -> list[Rubric]:
        sql = "SELECT * FROM rubrics"
        params: list[Any] = []
        if domain:
            sql += " WHERE domain = %s"
            params.append(domain)
        sql += " ORDER BY slug"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_rubric(r) for r in rows]

    # ----- jobs ----------------------------------------------------------- #

    def enqueue_job(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        *,
        max_attempts: int = 3,
    ) -> str:
        job_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, job_type, payload, status, attempts, max_attempts,
                    locked_by, locked_at, error, created_at, updated_at
                )
                VALUES (%s, %s, %s::jsonb, 'pending', 0, %s, NULL, NULL, NULL, %s, %s)
                """,
                (job_id, job_type, json.dumps(payload or {}), max_attempts, now, now),
            )
            conn.commit()
        return job_id

    def claim_job(self, worker_id: str | None = None) -> dict[str, Any] | None:
        claimed_by = worker_id or f"postgres-{os.getpid()}"
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            row = conn.execute("""
                SELECT *
                FROM jobs
                WHERE status IN ('pending', 'failed')
                  AND attempts < max_attempts
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    locked_by = %s,
                    locked_at = %s,
                    updated_at = %s,
                    error = NULL
                WHERE id = %s
                """,
                (claimed_by, now, now, row["id"]),
            )
            claimed = conn.execute("SELECT * FROM jobs WHERE id = %s", (row["id"],)).fetchone()
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
                    updated_at = %s
                WHERE id = %s
                """,
                (now, job_id),
            )
            conn.commit()
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
                    error = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (error, now, job_id),
            )
            conn.commit()
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
            sql += " AND status = %s"
            params.append(status)
        if job_type:
            sql += " AND job_type = %s"
            params.append(job_type)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_job(row) for row in rows]

    def prune_jobs(self, *, older_than_days: int = 14) -> int:
        """Delete terminal jobs (succeeded/failed/dead) older than the cutoff."""
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, older_than_days))).isoformat()
        with self._connect() as conn:
            res = conn.execute(
                "DELETE FROM jobs WHERE status IN ('succeeded', 'failed', 'dead') AND updated_at < %s",
                (cutoff,),
            )
            conn.commit()
        return int(res.rowcount or 0)

    def job_queue_health(self) -> dict[str, int]:
        lease_raw = os.environ.get("ATELIER_JOB_LEASE_SECONDS", "")
        lease_seconds = int(lease_raw) if lease_raw.isdigit() and int(lease_raw) > 0 else 900
        lease_cutoff = (datetime.now(UTC) - timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
                    COALESCE(SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END), 0) AS running,
                    COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed,
                    COALESCE(SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END), 0) AS dead,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN status = 'running' AND locked_at IS NOT NULL AND locked_at < %s THEN 1
                                ELSE 0
                            END
                        ),
                        0
                    ) AS stuck_running
                FROM jobs
                """,
                (lease_cutoff,),
            ).fetchone()
            conn.commit()
        pending = int(row["pending"]) if row is not None else 0
        running = int(row["running"]) if row is not None else 0
        failed = int(row["failed"]) if row is not None else 0
        dead = int(row["dead"]) if row is not None else 0
        stuck_running = int(row["stuck_running"]) if row is not None else 0
        return {
            "pending": pending,
            "running": running,
            "failed": failed,
            "dead": dead,
            "stuck_running": stuck_running,
            "active": pending + running + failed,
        }

    def import_blocks(self, blocks: Iterable[Playbook]) -> int:
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

    def store_embedding(self, block_id: str, embedding: list[float]) -> None:
        """Store a vector embedding for a Playbook (requires pgvector)."""
        vector_str = "[" + ",".join(str(x) for x in embedding) + "]"
        with self._connect() as conn:
            conn.execute(
                "UPDATE playbooks SET embedding = %s WHERE slug = %s",
                (vector_str, block_id),
            )
            conn.commit()

    # ----- bulk import ----------------------------------------------------- #

    # ----- vector helpers -------------------------------------------------- #

    def vector_search(
        self,
        embedding: list[float],
        *,
        domain: str | None = None,
        limit: int = 10,
    ) -> list[tuple[Playbook, float]]:
        """Cosine-similarity search (requires pgvector)."""
        if not self._vector_search:
            return []
        vector_str = "[" + ",".join(str(x) for x in embedding) + "]"
        sql = """
            SELECT *, 1 - (embedding <=> %(vec)s::vector) AS similarity
            FROM playbooks
            WHERE embedding IS NOT NULL
              AND status != 'quarantined'
        """
        params: dict[str, Any] = {"vec": vector_str}
        if domain:
            sql += " AND domain = %(domain)s"
            params["domain"] = domain
        sql += " ORDER BY embedding <=> %(vec)s::vector LIMIT %(limit)s"
        params["limit"] = limit
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(self._row_to_block(r), float(r["similarity"])) for r in rows]

    # ----- row mappers ----------------------------------------------------- #

    @staticmethod
    def _parse_metadata(raw: Any) -> dict[str, Any]:
        """Parse a metadata column value (str JSON or dict) into a plain dict."""
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return cast(dict[str, Any], json.loads(raw))
            except (json.JSONDecodeError, ValueError):
                return {}
        return {}

    def _row_to_block(self, row: Any) -> Playbook:
        """Convert a Postgres row dict/RealDictRow to a Playbook."""
        d = dict(row)
        return Playbook(
            id=d["slug"],
            title=d["title"],
            domain=d["domain"],
            situation=d["situation"],
            status=d["status"],
            task_types=(json.loads(d["task_types"]) if isinstance(d["task_types"], str) else d["task_types"]),
            triggers=json.loads(d["triggers"]) if isinstance(d["triggers"], str) else d["triggers"],
            file_patterns=(
                json.loads(d["file_patterns"]) if isinstance(d["file_patterns"], str) else d["file_patterns"]
            ),
            tool_patterns=(
                json.loads(d["tool_patterns"]) if isinstance(d["tool_patterns"], str) else d["tool_patterns"]
            ),
            dead_ends=(json.loads(d["dead_ends"]) if isinstance(d["dead_ends"], str) else d["dead_ends"]),
            procedure=(json.loads(d["procedure"]) if isinstance(d["procedure"], str) else d["procedure"]),
            verification=(json.loads(d["verification"]) if isinstance(d["verification"], str) else d["verification"]),
            failure_signals=(
                json.loads(d["failure_signals"]) if isinstance(d["failure_signals"], str) else d["failure_signals"]
            ),
            when_not_to_apply=d.get("when_not_to_apply") or "",
            usage_count=d.get("usage_count", 0),
            success_count=d.get("success_count", 0),
            failure_count=d.get("failure_count", 0),
            tier=self._parse_metadata(d.get("metadata")).get("tier", "e2"),
        )

    def _row_to_trace(self, row: Any) -> Trace:
        """Convert a Postgres row to a Trace."""
        d = dict(row)

        # Deserialize JSONB fields
        raw_tools = d.get("tools_called")
        if isinstance(raw_tools, str):
            raw_tools = json.loads(raw_tools)
        raw_tools = raw_tools or []

        raw_files = d.get("files_touched")
        if isinstance(raw_files, str):
            raw_files = json.loads(raw_files)
        raw_files = raw_files or []

        raw_commands = d.get("commands_run")
        if isinstance(raw_commands, str):
            raw_commands = json.loads(raw_commands)
        raw_commands = raw_commands or []

        raw_errors = d.get("errors_seen")
        if isinstance(raw_errors, str):
            raw_errors = json.loads(raw_errors)
        raw_errors = raw_errors or []

        raw_repeated = d.get("repeated_failures")
        if isinstance(raw_repeated, str):
            raw_repeated = json.loads(raw_repeated)
        raw_repeated = raw_repeated or []

        raw_validation = d.get("validation_results")
        if isinstance(raw_validation, str):
            raw_validation = json.loads(raw_validation)
        raw_validation = raw_validation or []

        raw_metadata = d.get("metadata")
        if isinstance(raw_metadata, str):
            raw_metadata = json.loads(raw_metadata)
        raw_metadata = raw_metadata or {}
        raw_learnings = raw_metadata.get("learnings", [])

        # Build typed tool calls
        tools_called: list[ToolCall] = []
        for item in raw_tools:
            if isinstance(item, dict):
                tools_called.append(
                    ToolCall(
                        name=item.get("name", ""),
                        args_hash=item.get("args_hash", ""),
                        count=item.get("count", 1),
                        args=item.get("args"),
                        result_summary=item.get("result_summary", ""),
                    )
                )

        # Build typed file records (backward compat: plain strings or dicts)
        files_touched: list[str | FileEditRecord] = []
        for item in raw_files:
            if isinstance(item, dict):
                files_touched.append(
                    FileEditRecord(
                        path=item.get("path", ""),
                        diff=item.get("diff", ""),
                        event=item.get("event", "edit"),
                    )
                )
            else:
                files_touched.append(str(item))

        # Build typed command records (backward compat: plain strings or dicts)
        commands_run: list[str | CommandRecord] = []
        for item in raw_commands:
            if isinstance(item, dict):
                commands_run.append(
                    CommandRecord(
                        command=item.get("command", ""),
                        exit_code=item.get("exit_code"),
                        stdout=item.get("stdout", ""),
                        stderr=item.get("stderr", ""),
                    )
                )
            else:
                commands_run.append(str(item))

        return Trace(
            id=d.get("session_id") or str(d.get("id", "")),
            agent=d["agent"],
            domain=d["domain"],
            task=d["task"],
            status=d["status"],
            files_touched=files_touched,
            tools_called=tools_called,
            commands_run=commands_run,
            errors_seen=raw_errors,
            repeated_failures=raw_repeated,
            diff_summary=d.get("diff_summary") or "",
            output_summary=d.get("output_summary") or "",
            validation_results=raw_validation,
            learnings=raw_learnings,
        )

    def _row_to_rubric(self, row: Any) -> Rubric:
        """Convert a Postgres row to a Rubric."""
        d = dict(row)
        return Rubric(
            id=d["slug"],
            domain=d["domain"],
            required_checks=(
                json.loads(d["required_checks"])
                if isinstance(d["required_checks"], str)
                else d.get("required_checks", [])
            ),
            block_if_missing=(
                json.loads(d["block_if_missing"])
                if isinstance(d["block_if_missing"], str)
                else d.get("block_if_missing", [])
            ),
            warning_checks=(
                json.loads(d["warning_checks"]) if isinstance(d["warning_checks"], str) else d.get("warning_checks", [])
            ),
            escalation_conditions=(
                json.loads(d["escalation_conditions"])
                if isinstance(d["escalation_conditions"], str)
                else d.get("escalation_conditions", [])
            ),
        )

    def _row_to_job(self, row: Any) -> dict[str, Any]:
        d = dict(row)
        payload = d.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        return {
            "id": str(d["id"]),
            "job_type": d["job_type"],
            "payload": payload,
            "status": d["status"],
            "attempts": d["attempts"],
            "max_attempts": d["max_attempts"],
            "locked_by": d.get("locked_by"),
            "locked_at": d.get("locked_at"),
            "error": d.get("error"),
            "created_at": str(d.get("created_at") or ""),
            "updated_at": str(d.get("updated_at") or ""),
        }

    def upsert_run_ledger(self, session_id: str, task: str, state: dict[str, Any], domain: str | None = None) -> None:
        """Upsert a run_ledger row."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_ledgers (session_id, task, domain, state, created_at, updated_at)
                VALUES (%(session_id)s, %(task)s, %(domain)s, %(state)s, %(now)s, %(now)s)
                ON CONFLICT(session_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    updated_at = EXCLUDED.updated_at
                """,
                {
                    "session_id": session_id,
                    "task": task,
                    "domain": domain,
                    "state": json.dumps(state),
                    "now": now,
                },
            )
            conn.commit()

    def get_run_ledger(self, session_id: str) -> dict[str, Any] | None:
        """Return a run_ledger row as a dict, or None."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM run_ledgers WHERE session_id = %s", (session_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("state"), str):
            d["state"] = json.loads(d["state"])
        return d

    # ----- run_ledger convenience ------------------------------------------ #


__all__ = ["SCHEMA_DDL", "VECTOR_EXTENSION_DDL", "PostgresStore"]
