"""V2 memory subsystem models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from atelier.core.foundation.models import _utcnow
from atelier.core.foundation.redaction import is_prompt_injection
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
    # N13 bi-temporal validity window, DISTINCT from ``created_at`` (ingestion
    # time). ``valid_at`` is when the captured knowledge became true (defaults to
    # ingestion); ``invalid_at`` marks when it stopped being true. Both additive
    # with open-ended defaults (valid since ingestion, never invalidated), so
    # records persisted before this field -- which the store reconstructs without
    # passing them -- load unchanged and every existing read is unaffected.
    valid_at: datetime | None = None
    invalid_at: datetime | None = None

    @field_validator("dedup_hash")
    @classmethod
    def _dedup_hash_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dedup_hash must be non-empty")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def injection_flagged(self) -> bool:
        """Index-time trust label (N15): True when the passage text matches a
        prompt-injection needle.

        Inbound dual of live output redaction. Derived deterministically from
        ``text`` so the flag rides along in every retrieval result (and
        ``model_dump``) without a schema migration, and survives store/bridge
        round-trips. Additive only: callers that ignore it are unaffected; the
        passage text itself is never altered or dropped.
        """
        return is_prompt_injection(self.text)

    def is_valid_at(self, when: datetime | None = None) -> bool:
        """N13: True when this passage's validity window covers ``when``.

        ``when`` defaults to now. A passage is valid when ``when`` is at or after
        ``valid_at`` (open-ended start when unset -- valid since ingestion) and
        strictly before ``invalid_at`` (open-ended end when unset -- never
        invalidated). Pure: it reads only the record's own fields.
        """
        moment = when if when is not None else _utcnow()
        if self.valid_at is not None and moment < self.valid_at:
            return False
        if self.invalid_at is not None and moment >= self.invalid_at:
            return False
        return True


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
    compaction_strategy: Literal["none", "tfidf", "llm_summarizer", "letta_summarizer"]
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
