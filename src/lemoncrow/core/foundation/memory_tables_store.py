"""MemoryTablesStore -- schema for LemonCrow's V2 curated-memory tables.

Owns ``memory_block``, ``memory_block_history``, ``archival_passage``,
``archival_passage_fts``, ``memory_recall``, and ``run_memory_frame``. This
class only defines the schema; the query/mutation methods live in
``lemoncrow.infra.storage.sqlite_memory_store.SqliteMemoryStore``, which
composes an instance of this store for its connection.

Backed by ``lemoncrow_memory.db``, physically separate from history,
knowledge, lessons, jobs, and telemetry -- curated memory writes never
contend with the (much larger) trace history.
"""

from __future__ import annotations

from pathlib import Path

from lemoncrow.core.foundation.sqlite_base import SqliteTableStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_block (
  id                  TEXT PRIMARY KEY,
  agent_id            TEXT NOT NULL,
  label               TEXT NOT NULL,
  value               TEXT NOT NULL,
  limit_chars         INTEGER NOT NULL DEFAULT 8000,
  description         TEXT NOT NULL DEFAULT '',
  read_only           INTEGER NOT NULL DEFAULT 0,
  metadata            TEXT NOT NULL DEFAULT '{}',
  pinned              INTEGER NOT NULL DEFAULT 0,
  version             INTEGER NOT NULL DEFAULT 1,
  current_history_id  TEXT,
  deprecated_at       TEXT,
  deprecated_by_block_id TEXT,
  deprecation_reason  TEXT NOT NULL DEFAULT '',
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  UNIQUE (agent_id, label)
);
CREATE INDEX IF NOT EXISTS ix_memory_block_agent_pinned ON memory_block(agent_id, pinned);
CREATE INDEX IF NOT EXISTS ix_memory_block_updated_at  ON memory_block(updated_at DESC);

CREATE TABLE IF NOT EXISTS memory_block_history (
  id          TEXT PRIMARY KEY,
  block_id    TEXT NOT NULL REFERENCES memory_block(id) ON DELETE CASCADE,
  prev_value  TEXT NOT NULL,
  new_value   TEXT NOT NULL,
  actor       TEXT NOT NULL,
  reason      TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memory_block_history_block_at
  ON memory_block_history(block_id, created_at DESC);

CREATE TABLE IF NOT EXISTS archival_passage (
  id              TEXT PRIMARY KEY,
  agent_id        TEXT NOT NULL,
  text            TEXT NOT NULL,
  embedding       BLOB,
  embedding_model TEXT NOT NULL DEFAULT '',
  embedding_provenance TEXT NOT NULL DEFAULT 'legacy_stub',
  tags            TEXT NOT NULL DEFAULT '[]',
  source          TEXT NOT NULL,
  source_ref      TEXT NOT NULL DEFAULT '',
  dedup_hash      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  UNIQUE (agent_id, dedup_hash)
);
CREATE INDEX IF NOT EXISTS ix_archival_passage_agent_at ON archival_passage(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_archival_passage_source   ON archival_passage(source, source_ref);
CREATE VIRTUAL TABLE IF NOT EXISTS archival_passage_fts USING fts5(
  text, tags, content='archival_passage', content_rowid='rowid'
);

CREATE TABLE IF NOT EXISTS memory_recall (
  id                   TEXT PRIMARY KEY,
  agent_id             TEXT NOT NULL,
  query                TEXT NOT NULL,
  top_passages         TEXT NOT NULL,
  selected_passage_id  TEXT,
  created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_memory_frame (
  session_id          TEXT PRIMARY KEY,
  pinned_blocks       TEXT NOT NULL,
  recalled_passages   TEXT NOT NULL,
  summarized_events   TEXT NOT NULL,
  tokens_pre_summary  INTEGER NOT NULL,
  tokens_post_summary INTEGER NOT NULL,
  compaction_strategy TEXT NOT NULL,
  workspace_path      TEXT,
  created_at          TEXT NOT NULL
);
"""


class MemoryTablesStore(SqliteTableStore):
    """SQLite-backed schema owner for curated-memory tables."""

    SCHEMA = SCHEMA
    REQUIRED_TABLES = (
        "memory_block",
        "memory_block_history",
        "archival_passage",
        "archival_passage_fts",
        "memory_recall",
        "run_memory_frame",
    )

    def __init__(self, root: Path | str, *, db_name: str = "lemoncrow_memory.db") -> None:
        super().__init__(root, db_name=db_name)


__all__ = ["MemoryTablesStore"]
