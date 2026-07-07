"""Pydantic request/response schemas for the service API.

Kept separate from core models to allow the API to evolve independently.
All response models use ``extra="forbid"`` for forward-compat serialization.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---- shared ----------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- /v1/reasoning/context -------------------------------------------------


class ContextRequest(_Strict):
    task: str
    domain: str | None = None
    files: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    max_blocks: int = 5
    token_budget: int | None = 2000
    dedup: bool = True
    include_telemetry: bool = False
    agent_id: str | None = None
    recall: bool = True


class ContextResponse(_Strict):
    context: str
    tokens_used: int | None = None
    tokens_saved_vs_naive: int | None = None
    recalled_passages: list[dict[str, str | float]] = Field(default_factory=list)
    tokens_breakdown: dict[str, int] | None = None
    bootstrap: dict[str, Any] | None = None
