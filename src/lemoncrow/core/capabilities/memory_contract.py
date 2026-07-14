"""Public contract models for the memory service.

These pydantic result shapes are the caller-facing API (data contract, not IP).
They live here (open) because pydantic cannot be mypyc-compiled, so the pro
memory service compiles to native ``.so`` while callers import the same types.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FactScope = Literal["repository", "user"]
VoteDirection = Literal["upvote", "downvote"]


class MemoryFactResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    subject: str
    fact: str
    scope: FactScope
    citations: str = ""
    reason: str = ""


class MemoryVoteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    fact: str
    scope: FactScope
    direction: VoteDirection
    reason: str


class MemoryRecallPassage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    source_ref: str = ""
    tags: list[str] = Field(default_factory=list)


class MemoryRecallResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passages: list[MemoryRecallPassage]
