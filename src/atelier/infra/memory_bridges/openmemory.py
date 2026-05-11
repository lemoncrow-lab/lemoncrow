"""OpenMemory interoperability wrapper."""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path

from atelier.core.foundation.memory_models import (
    ArchivalPassage,
    MemoryBlock,
    MemoryBlockHistory,
    MemoryRecall,
    RunMemoryFrame,
)
from atelier.gateway.integrations import openmemory as openmemory_bridge
from atelier.infra.memory_bridges.base import MemorySyncResult
from atelier.infra.storage.sqlite_memory_store import SqliteMemoryStore


class OpenMemoryAdapter:
    source = "openmemory"

    def fetch_context(self, *, task: str, project_id: str | None = None) -> MemorySyncResult:
        result = openmemory_bridge.maybe_fetch_memory_context_for_task(task, project_id)
        data = result.get("data", {})
        context = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
        return MemorySyncResult(
            ok=bool(result.get("ok", False)),
            skipped=bool(result.get("skipped", False)),
            source=self.source,
            context=context,
            detail=str(result.get("reason", "")),
        )

    def push_procedural_lesson(self, *, trace_id: str, memory_id: str) -> MemorySyncResult:
        result = openmemory_bridge.maybe_store_memory_pointer(trace_id, memory_id)
        return MemorySyncResult(
            ok=bool(result.get("ok", False)),
            skipped=bool(result.get("skipped", False)),
            source=self.source,
            detail=str(result.get("reason", "")),
        )


class OpenMemoryMemoryStore:
    """MemoryStore adapter for OpenMemory with local SQLite durability.

    The existing OpenMemory bridge is pointer/context oriented and remote sync
    is optional.  This adapter keeps the full Atelier MemoryStore contract by
    storing canonical data in SQLite, then mirrors archival pointers to the
    OpenMemory bridge on a best-effort basis.
    """

    def __init__(self, root: str | Path, *, adapter: OpenMemoryAdapter | None = None) -> None:
        self._store = SqliteMemoryStore(root)
        self._adapter = adapter or OpenMemoryAdapter()

    @property
    def root(self) -> Path:
        return self._store.root

    @property
    def db_path(self) -> Path:
        return self._store.db_path

    def upsert_block(self, block: MemoryBlock, *, actor: str, reason: str = "") -> MemoryBlock:
        return self._store.upsert_block(block, actor=actor, reason=reason)

    def get_block(self, agent_id: str, label: str, *, include_tombstoned: bool = False) -> MemoryBlock | None:
        return self._store.get_block(agent_id, label, include_tombstoned=include_tombstoned)

    def list_blocks(self, agent_id: str, *, include_tombstoned: bool = False, limit: int = 500) -> list[MemoryBlock]:
        return self._store.list_blocks(agent_id, include_tombstoned=include_tombstoned, limit=limit)

    def list_pinned_blocks(self, agent_id: str) -> list[MemoryBlock]:
        return self._store.list_pinned_blocks(agent_id)

    def list_block_history(self, block_id: str, *, limit: int = 50) -> list[MemoryBlockHistory]:
        return self._store.list_block_history(block_id, limit=limit)

    def delete_block(self, block_id: str) -> None:
        self._store.delete_block(block_id)

    def tombstone_block(
        self,
        block_id: str,
        *,
        deprecated_by_block_id: str | None = None,
        reason: str = "",
    ) -> None:
        self._store.tombstone_block(block_id, deprecated_by_block_id=deprecated_by_block_id, reason=reason)

    def insert_passage(self, passage: ArchivalPassage) -> ArchivalPassage:
        stored = self._store.insert_passage(passage)
        with contextlib.suppress(Exception):
            self._adapter.push_procedural_lesson(trace_id=stored.source_ref or stored.id, memory_id=stored.id)
        return stored

    def search_passages(
        self,
        agent_id: str,
        query: str,
        *,
        top_k: int = 5,
        tags: list[str] | None = None,
        since: datetime | None = None,
    ) -> list[ArchivalPassage]:
        with contextlib.suppress(Exception):
            self._adapter.fetch_context(task=query, project_id=agent_id)
        return self._store.search_passages(agent_id, query, top_k=top_k, tags=tags, since=since)

    def list_passages(
        self,
        agent_id: str,
        *,
        tags: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[ArchivalPassage]:
        return self._store.list_passages(agent_id, tags=tags, since=since, limit=limit)

    def record_recall(self, recall: MemoryRecall) -> MemoryRecall:
        return self._store.record_recall(recall)

    def list_recalls(self, agent_id: str, *, limit: int = 50) -> list[MemoryRecall]:
        return self._store.list_recalls(agent_id, limit=limit)

    def write_run_frame(self, frame: RunMemoryFrame) -> None:
        self._store.write_run_frame(frame)

    def get_run_frame(self, session_id: str) -> RunMemoryFrame | None:
        return self._store.get_run_frame(session_id)
