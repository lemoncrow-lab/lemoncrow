"""V2 memory subsystem models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from atelier.core.foundation.models import _utcnow
from atelier.infra.storage.ids import make_uuid7

ArchivalSource = Literal["trace", "block_evict", "user", "tool_output", "file_chunk"]


def _id(prefix: str) -> str:
    return f"{prefix}-{make_uuid7()}"


class MemoryBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: _id("mem"))
    agent_id: str
    label: str
    value: str
    limit_chars: int = 8000
    description: str = ""
    read_only: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    pinned: bool = False
    version: int = 1
    current_history_id: str | None = None
    deprecated_at: datetime | None = None
    deprecated_by_block_id: str | None = None
    deprecation_reason: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _value_within_limit(self) -> MemoryBlock:
        if len(self.value) > self.limit_chars:
            raise ValueError("value length must be less than or equal to limit_chars")
        return self


class MemoryBlockHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: _id("memh"))
    block_id: str
    prev_value: str
    new_value: str
    actor: str
    reason: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class ArchivalPassage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: _id("pas"))
    agent_id: str
    text: str
    embedding: list[float] | None = None
    embedding_model: str = ""
    embedding_provenance: str = "legacy_stub"
    tags: list[str] = Field(default_factory=list)
    source: ArchivalSource
    source_ref: str = ""
    dedup_hash: str
    dedup_hit: bool = False
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("dedup_hash")
    @classmethod
    def _dedup_hash_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dedup_hash must be non-empty")
        return value


class MemoryRecall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: _id("rec"))
    agent_id: str
    query: str
    top_passages: list[str]
    selected_passage_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class RunMemoryFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    pinned_blocks: list[str]
    recalled_passages: list[str]
    summarized_events: list[str]
    tokens_pre_summary: int
    tokens_post_summary: int
    compaction_strategy: Literal["none", "tfidf", "ollama_summarizer", "letta_summarizer"]
    workspace_path: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


__all__ = [
    "ArchivalPassage",
    "ArchivalSource",
    "MemoryBlock",
    "MemoryBlockHistory",
    "MemoryRecall",
    "RunMemoryFrame",
]
