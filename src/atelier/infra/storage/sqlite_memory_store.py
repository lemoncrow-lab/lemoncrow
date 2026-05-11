"""SQLite implementation of the V2 MemoryStore contract."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from atelier.core.foundation.memory_models import (
    ArchivalPassage,
    MemoryBlock,
    MemoryBlockHistory,
    MemoryRecall,
    RunMemoryFrame,
)
from atelier.infra.storage.memory_store import MemoryConcurrencyError
from atelier.infra.storage.sqlite_store import SQLiteStore


def _iso(dt: datetime) -> str:
    value = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _loads_list(text: str) -> list[str]:
    data = json.loads(text or "[]")
    return [str(item) for item in data] if isinstance(data, list) else []


def _fts_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]+", query)
    return " OR ".join(terms)


def _validate_embedding(vector: list[float] | None) -> None:
    if vector is None:
        return
    if len(vector) == 32:
        raise ValueError("32-dimensional legacy stub embeddings are not accepted")


class SqliteMemoryStore:
    """SQLite memory store backed by the existing Atelier database file."""

    def __init__(self, root: str | Path) -> None:
        self._store = SQLiteStore(Path(root))
        self._store.init()

    @property
    def root(self) -> Path:
        return self._store.root

    @property
    def db_path(self) -> Path:
        return self._store.db_path

    def upsert_block(self, block: MemoryBlock, *, actor: str, reason: str = "") -> MemoryBlock:
        now = datetime.now(UTC)
        with self._store._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM memory_block WHERE agent_id = ? AND label = ?",
                (block.agent_id, block.label),
            ).fetchone()
            if existing is not None and int(existing["version"]) != block.version:
                raise MemoryConcurrencyError(
                    f"stale memory block version for {block.agent_id}:{block.label}: "
                    f"expected {existing['version']} got {block.version}"
                )

            block_id = str(existing["id"]) if existing is not None else block.id
            previous_value = str(existing["value"]) if existing is not None else ""
            next_version = int(existing["version"]) + 1 if existing is not None else block.version
            created_at = str(existing["created_at"]) if existing is not None else _iso(block.created_at)

            history = MemoryBlockHistory(
                block_id=block_id,
                prev_value=previous_value,
                new_value=block.value,
                actor=actor,
                reason=reason,
                created_at=now,
            )

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO memory_block (
                      id, agent_id, label, value, limit_chars, description, read_only,
                                            metadata, pinned, version, current_history_id,
                                            deprecated_at, deprecated_by_block_id, deprecation_reason,
                                            created_at, updated_at
                    )
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        block_id,
                        block.agent_id,
                        block.label,
                        block.value,
                        block.limit_chars,
                        block.description,
                        int(block.read_only),
                        _json(block.metadata),
                        int(block.pinned),
                        next_version,
                        block.deprecated_at.isoformat() if block.deprecated_at else None,
                        block.deprecated_by_block_id,
                        block.deprecation_reason,
                        created_at,
                        _iso(now),
                    ),
                )
            conn.execute(
                """
                INSERT INTO memory_block_history
                  (id, block_id, prev_value, new_value, actor, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    history.id,
                    history.block_id,
                    history.prev_value,
                    history.new_value,
                    history.actor,
                    history.reason,
                    _iso(history.created_at),
                ),
            )
            if existing is None:
                conn.execute(
                    "UPDATE memory_block SET current_history_id = ? WHERE id = ?",
                    (history.id, block_id),
                )
            else:
                updated = conn.execute(
                    """
                    UPDATE memory_block SET
                      value = ?,
                      limit_chars = ?,
                      description = ?,
                      read_only = ?,
                      metadata = ?,
                      pinned = ?,
                      version = ?,
                      current_history_id = ?,
                      deprecated_at = ?,
                      deprecated_by_block_id = ?,
                      deprecation_reason = ?,
                      updated_at = ?
                                        WHERE id = ? AND version = ?
                    """,
                    (
                        block.value,
                        block.limit_chars,
                        block.description,
                        int(block.read_only),
                        _json(block.metadata),
                        int(block.pinned),
                        next_version,
                        history.id,
                        block.deprecated_at.isoformat() if block.deprecated_at else None,
                        block.deprecated_by_block_id,
                        block.deprecation_reason,
                        _iso(now),
                        block_id,
                        block.version,
                    ),
                )
                if updated.rowcount != 1:
                    raise MemoryConcurrencyError(
                        f"stale memory block version for {block.agent_id}:{block.label}: "
                        f"expected current version {block.version}"
                    )

        stored = self.get_block(block.agent_id, block.label, include_tombstoned=True)
        if stored is None:  # pragma: no cover - defensive
            raise RuntimeError("memory block upsert did not persist")
        return stored

    def get_block(self, agent_id: str, label: str, *, include_tombstoned: bool = False) -> MemoryBlock | None:
        tombstone_sql = "" if include_tombstoned else " AND deprecated_at IS NULL"
        with self._store._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM memory_block WHERE agent_id = ? AND label = ?{tombstone_sql}",
                (agent_id, label),
            ).fetchone()
        return self._block_from_row(row) if row is not None else None

    def list_blocks(self, agent_id: str, *, include_tombstoned: bool = False, limit: int = 500) -> list[MemoryBlock]:
        tombstone_sql = "" if include_tombstoned else " AND deprecated_at IS NULL"
        with self._store._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memory_block
                WHERE agent_id = ?{tombstone_sql}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        return [self._block_from_row(row) for row in rows]

    def list_pinned_blocks(self, agent_id: str) -> list[MemoryBlock]:
        with self._store._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_block
                WHERE agent_id = ? AND pinned = 1 AND deprecated_at IS NULL
                ORDER BY updated_at DESC
                """,
                (agent_id,),
            ).fetchall()
        return [self._block_from_row(row) for row in rows]

    def list_block_history(self, block_id: str, *, limit: int = 50) -> list[MemoryBlockHistory]:
        with self._store._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_block_history
                WHERE block_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (block_id, limit),
            ).fetchall()
        return [self._history_from_row(row) for row in rows]

    def delete_block(self, block_id: str) -> None:
        with self._store._connect() as conn:
            conn.execute("DELETE FROM memory_block WHERE id = ?", (block_id,))

    def tombstone_block(
        self,
        block_id: str,
        *,
        deprecated_by_block_id: str | None = None,
        reason: str = "",
    ) -> None:
        with self._store._connect() as conn:
            conn.execute(
                """
                UPDATE memory_block SET
                  deprecated_at = ?,
                  deprecated_by_block_id = ?,
                  deprecation_reason = ?,
                  updated_at = ?
                WHERE id = ?
                """,
                (
                    _iso(datetime.now(UTC)),
                    deprecated_by_block_id,
                    reason,
                    _iso(datetime.now(UTC)),
                    block_id,
                ),
            )

    def insert_passage(self, passage: ArchivalPassage) -> ArchivalPassage:
        _validate_embedding(passage.embedding)
        with self._store._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM archival_passage WHERE agent_id = ? AND dedup_hash = ?",
                (passage.agent_id, passage.dedup_hash),
            ).fetchone()
            if existing is not None:
                return self._passage_from_row(existing).model_copy(update={"dedup_hit": True})

            cursor = conn.execute(
                """
                INSERT INTO archival_passage (
                                    id, agent_id, text, embedding, embedding_model, embedding_provenance, tags,
                  source, source_ref, dedup_hash, created_at
                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    passage.id,
                    passage.agent_id,
                    passage.text,
                    (_json(passage.embedding).encode("utf-8") if passage.embedding is not None else None),
                    passage.embedding_model,
                    passage.embedding_provenance,
                    _json(passage.tags),
                    passage.source,
                    passage.source_ref,
                    passage.dedup_hash,
                    _iso(passage.created_at),
                ),
            )
            conn.execute(
                "INSERT INTO archival_passage_fts(rowid, text, tags) VALUES (?, ?, ?)",
                (cursor.lastrowid, passage.text, " ".join(passage.tags)),
            )
        return passage.model_copy(update={"dedup_hit": False})

    def search_passages(
        self,
        agent_id: str,
        query: str,
        *,
        top_k: int = 5,
        tags: list[str] | None = None,
        since: datetime | None = None,
    ) -> list[ArchivalPassage]:
        rows = self._search_passage_rows(agent_id, query, top_k=max(top_k * 5, top_k), since=since)
        passages = [self._passage_from_row(row) for row in rows]
        if tags:
            required = set(tags)
            passages = [p for p in passages if required.issubset(set(p.tags))]
        return passages[:top_k]

    def list_passages(
        self,
        agent_id: str,
        *,
        tags: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[ArchivalPassage]:
        params: list[Any] = [agent_id]
        since_sql = ""
        if since is not None:
            since_sql = " AND created_at >= ?"
            params.append(_iso(since))
        with self._store._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM archival_passage
                WHERE agent_id = ?{since_sql}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        passages = [self._passage_from_row(row) for row in rows]
        if tags:
            required = set(tags)
            passages = [p for p in passages if required.issubset(set(p.tags))]
        return passages

    def record_recall(self, recall: MemoryRecall) -> MemoryRecall:
        with self._store._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_recall
                  (id, agent_id, query, top_passages, selected_passage_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    recall.id,
                    recall.agent_id,
                    recall.query,
                    _json(recall.top_passages),
                    recall.selected_passage_id,
                    _iso(recall.created_at),
                ),
            )
        return recall

    def list_recalls(self, agent_id: str, *, limit: int = 50) -> list[MemoryRecall]:
        with self._store._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_recall
                WHERE agent_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        return [
            MemoryRecall(
                id=str(row["id"]),
                agent_id=str(row["agent_id"]),
                query=str(row["query"]),
                top_passages=_loads_list(str(row["top_passages"])),
                selected_passage_id=(str(row["selected_passage_id"]) if row["selected_passage_id"] else None),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        ]

    def write_run_frame(self, frame: RunMemoryFrame) -> None:
        with self._store._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_memory_frame (
                  session_id, pinned_blocks, recalled_passages, summarized_events,
                  tokens_pre_summary, tokens_post_summary, compaction_strategy, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  pinned_blocks = excluded.pinned_blocks,
                  recalled_passages = excluded.recalled_passages,
                  summarized_events = excluded.summarized_events,
                  tokens_pre_summary = excluded.tokens_pre_summary,
                  tokens_post_summary = excluded.tokens_post_summary,
                  compaction_strategy = excluded.compaction_strategy,
                  created_at = excluded.created_at
                """,
                (
                    frame.session_id,
                    _json(frame.pinned_blocks),
                    _json(frame.recalled_passages),
                    _json(frame.summarized_events),
                    frame.tokens_pre_summary,
                    frame.tokens_post_summary,
                    frame.compaction_strategy,
                    _iso(frame.created_at),
                ),
            )

    def get_run_frame(self, session_id: str) -> RunMemoryFrame | None:
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_memory_frame WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return RunMemoryFrame(
            session_id=str(row["session_id"]),
            pinned_blocks=_loads_list(str(row["pinned_blocks"])),
            recalled_passages=_loads_list(str(row["recalled_passages"])),
            summarized_events=_loads_list(str(row["summarized_events"])),
            tokens_pre_summary=int(row["tokens_pre_summary"]),
            tokens_post_summary=int(row["tokens_post_summary"]),
            compaction_strategy=cast(Any, row["compaction_strategy"]),
            workspace_path=((row.get("workspace_path") and str(row["workspace_path"])) or None),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def _search_passage_rows(
        self,
        agent_id: str,
        query: str,
        *,
        top_k: int,
        since: datetime | None,
    ) -> list[sqlite3.Row]:
        params: list[Any] = [agent_id]
        since_sql = ""
        if since is not None:
            since_sql = " AND p.created_at >= ?"
            params.append(_iso(since))

        match_query = _fts_query(query)
        with self._store._connect() as conn:
            if match_query:
                return conn.execute(
                    f"""
                    SELECT p.* FROM archival_passage_fts f
                    JOIN archival_passage p ON p.rowid = f.rowid
                    WHERE p.agent_id = ?{since_sql} AND archival_passage_fts MATCH ?
                    ORDER BY bm25(archival_passage_fts), p.created_at DESC
                    LIMIT ?
                    """,
                    (*params, match_query, top_k),
                ).fetchall()
            return conn.execute(
                f"""
                SELECT p.* FROM archival_passage p
                WHERE p.agent_id = ?{since_sql}
                ORDER BY p.created_at DESC
                LIMIT ?
                """,
                (*params, top_k),
            ).fetchall()

    def _block_from_row(self, row: sqlite3.Row) -> MemoryBlock:
        row_keys = set(row.keys())
        metadata = json.loads(str(row["metadata"] or "{}"))
        return MemoryBlock(
            id=str(row["id"]),
            agent_id=str(row["agent_id"]),
            label=str(row["label"]),
            value=str(row["value"]),
            limit_chars=int(row["limit_chars"]),
            description=str(row["description"]),
            read_only=bool(row["read_only"]),
            metadata=metadata if isinstance(metadata, dict) else {},
            pinned=bool(row["pinned"]),
            version=int(row["version"]),
            current_history_id=(str(row["current_history_id"]) if row["current_history_id"] else None),
            deprecated_at=(
                datetime.fromisoformat(str(row["deprecated_at"]))
                if "deprecated_at" in row_keys and row["deprecated_at"]
                else None
            ),
            deprecated_by_block_id=(
                str(row["deprecated_by_block_id"])
                if "deprecated_by_block_id" in row_keys and row["deprecated_by_block_id"]
                else None
            ),
            deprecation_reason=(
                str(row["deprecation_reason"]) if "deprecation_reason" in row_keys and row["deprecation_reason"] else ""
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def _history_from_row(self, row: sqlite3.Row) -> MemoryBlockHistory:
        return MemoryBlockHistory(
            id=str(row["id"]),
            block_id=str(row["block_id"]),
            prev_value=str(row["prev_value"]),
            new_value=str(row["new_value"]),
            actor=str(row["actor"]),
            reason=str(row["reason"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def _passage_from_row(self, row: sqlite3.Row) -> ArchivalPassage:
        row_keys = set(row.keys())
        embedding_blob = row["embedding"]
        embedding: list[float] | None = None
        if embedding_blob is not None:
            data = json.loads(bytes(embedding_blob).decode("utf-8"))
            if isinstance(data, list):
                embedding = [float(item) for item in data]
        return ArchivalPassage(
            id=str(row["id"]),
            agent_id=str(row["agent_id"]),
            text=str(row["text"]),
            embedding=embedding,
            embedding_model=str(row["embedding_model"]),
            embedding_provenance=(
                str(row["embedding_provenance"]) if "embedding_provenance" in row_keys else "legacy_stub"
            ),
            tags=_loads_list(str(row["tags"])),
            source=str(row["source"]),  # type: ignore[arg-type]
            source_ref=str(row["source_ref"]),
            dedup_hash=str(row["dedup_hash"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )


__all__ = ["SqliteMemoryStore"]
