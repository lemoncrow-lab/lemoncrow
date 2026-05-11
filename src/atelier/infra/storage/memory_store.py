"""Memory storage protocol and errors."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from atelier.core.foundation.memory_models import (
    ArchivalPassage,
    MemoryBlock,
    MemoryBlockHistory,
    MemoryRecall,
    RunMemoryFrame,
)


class MemoryConcurrencyError(RuntimeError):
    """Raised when an optimistic-locking version check fails."""


class MemorySidecarUnavailable(RuntimeError):
    """Raised when an optional external memory sidecar cannot serve a request."""


class MemoryStore(Protocol):
    def upsert_block(self, block: MemoryBlock, *, actor: str, reason: str = "") -> MemoryBlock: ...
    def get_block(self, agent_id: str, label: str, *, include_tombstoned: bool = False) -> MemoryBlock | None: ...
    def list_blocks(
        self, agent_id: str, *, include_tombstoned: bool = False, limit: int = 500
    ) -> list[MemoryBlock]: ...
    def list_pinned_blocks(self, agent_id: str) -> list[MemoryBlock]: ...
    def list_block_history(self, block_id: str, *, limit: int = 50) -> list[MemoryBlockHistory]: ...
    def delete_block(self, block_id: str) -> None: ...
    def tombstone_block(
        self, block_id: str, *, deprecated_by_block_id: str | None = None, reason: str = ""
    ) -> None: ...

    def insert_passage(self, passage: ArchivalPassage) -> ArchivalPassage: ...
    def search_passages(
        self,
        agent_id: str,
        query: str,
        *,
        top_k: int = 5,
        tags: list[str] | None = None,
        since: datetime | None = None,
    ) -> list[ArchivalPassage]: ...
    def list_passages(
        self,
        agent_id: str,
        *,
        tags: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[ArchivalPassage]: ...
    def record_recall(self, recall: MemoryRecall) -> MemoryRecall: ...
    def list_recalls(self, agent_id: str, *, limit: int = 50) -> list[MemoryRecall]: ...

    def write_run_frame(self, frame: RunMemoryFrame) -> None: ...
    def get_run_frame(self, session_id: str) -> RunMemoryFrame | None: ...


__all__ = ["MemoryConcurrencyError", "MemorySidecarUnavailable", "MemoryStore"]
