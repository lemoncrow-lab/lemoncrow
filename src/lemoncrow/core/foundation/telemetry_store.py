"""TelemetryStore -- context budget and routing instrumentation.

``context_budget`` records per-turn token accounting (written by
``ContextBudgetRecorder``, read by the savings/optimization reporting).
``route_decision`` / ``verification_envelope`` are provisioned for the
cross-vendor routing feature; nothing writes them yet, but they're grouped
here since they're the same kind of ephemeral, high-volume instrumentation
as context_budget, not knowledge or history.

Backed by ``lemoncrow_telemetry.db``, physically separate from history,
knowledge, lessons, jobs, and memory.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.sqlite_base import SqliteTableStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS context_budget (
  id                   TEXT PRIMARY KEY,
  session_id           TEXT NOT NULL,
  turn_index           INTEGER NOT NULL,
  model                TEXT NOT NULL,
  input_tokens         INTEGER NOT NULL,
  cache_read_tokens    INTEGER NOT NULL,
  cache_write_tokens   INTEGER NOT NULL,
  output_tokens        INTEGER NOT NULL,
  naive_input_tokens   INTEGER NOT NULL,
  lever_savings_json   TEXT NOT NULL,
  tool_calls           INTEGER NOT NULL,
  created_at           TEXT NOT NULL,
  UNIQUE (session_id, turn_index)
);
CREATE INDEX IF NOT EXISTS ix_context_budget_run ON context_budget(session_id);

CREATE TABLE IF NOT EXISTS route_decision (
  id                    TEXT PRIMARY KEY,
  session_id            TEXT NOT NULL,
  request_id            TEXT NOT NULL DEFAULT '',
  step_index            INTEGER NOT NULL,
  step_type             TEXT NOT NULL,
  risk_level            TEXT NOT NULL,
  tier                  TEXT NOT NULL,
  selected_model        TEXT NOT NULL DEFAULT '',
  confidence            REAL NOT NULL,
  reason                TEXT NOT NULL,
  protected_file_match  INTEGER NOT NULL DEFAULT 0,
  verifier_required     TEXT NOT NULL DEFAULT '[]',
  escalation_trigger    TEXT,
  evidence_refs         TEXT NOT NULL DEFAULT '[]',
  created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_route_decision_run_step ON route_decision(session_id, step_index);

CREATE TABLE IF NOT EXISTS verification_envelope (
  id                    TEXT PRIMARY KEY,
  route_decision_id     TEXT NOT NULL REFERENCES route_decision(id) ON DELETE CASCADE,
  session_id            TEXT NOT NULL,
  changed_files         TEXT NOT NULL DEFAULT '[]',
  validation_results    TEXT NOT NULL DEFAULT '[]',
  rubric_status         TEXT NOT NULL DEFAULT 'not_run',
  outcome               TEXT NOT NULL,
  compressed_evidence   TEXT NOT NULL DEFAULT '',
  human_accepted        INTEGER,
  created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_verification_envelope_route ON verification_envelope(route_decision_id);
"""


class TelemetryStore(SqliteTableStore):
    """SQLite-backed store for context budget and routing telemetry."""

    SCHEMA = SCHEMA
    REQUIRED_TABLES = ("context_budget", "route_decision", "verification_envelope")

    def __init__(self, root: Path | str, *, db_name: str = "lemoncrow_telemetry.db") -> None:
        super().__init__(root, db_name=db_name)

    def persist_context_budget(self, record: Any) -> None:
        """Persist a ContextBudget record to the store."""
        with self._transaction() as conn:
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

    def list_context_budgets(self, session_id: str) -> list[Any]:
        """List all ContextBudget records for a run, ordered by turn_index."""
        from lemoncrow.core.foundation.savings_models import ContextBudget

        with self._transaction() as conn:
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
        """Get a single ContextBudget record by ID, or None."""
        from lemoncrow.core.foundation.savings_models import ContextBudget

        with self._transaction() as conn:
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


__all__ = ["TelemetryStore"]
