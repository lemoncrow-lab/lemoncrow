"""Shared types for memory interoperability."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MemorySyncResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    source: str
    skipped: bool = False
    context: str = ""
    accepted_playbooks: list[str] = Field(default_factory=list)
    detail: str = ""
