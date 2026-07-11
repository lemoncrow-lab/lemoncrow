"""V2 lesson pipeline models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lemoncrow.core.foundation.models import Playbook, _utcnow
from lemoncrow.infra.storage.ids import make_uuid7

LessonCandidateKind = Literal["new_block", "edit_block", "new_rubric_check", "route-preference", "cost-cap"]
LessonCandidateStatus = Literal["inbox", "approved", "rejected", "superseded"]


def _id(prefix: str) -> str:
    return f"{prefix}-{make_uuid7()}"


class LessonCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: _id("lc"))
    domain: str
    cluster_fingerprint: str
    kind: LessonCandidateKind
    target_id: str | None = None
    proposed_block: Playbook | None = None
    proposed_rubric_check: str | None = None
    evidence_trace_ids: list[str]
    body: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None
    embedding_provenance: str = "legacy_stub"
    confidence: float = Field(ge=0, le=1)
    status: LessonCandidateStatus = "inbox"
    reviewer: str | None = None
    decision_at: datetime | None = None
    decision_reason: str = ""
    created_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _edit_block_requires_target(self) -> LessonCandidate:
        if self.kind == "edit_block" and not self.target_id:
            raise ValueError("target_id is required when kind is edit_block")
        return self


class LessonPromotion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: _id("lp"))
    lesson_id: str
    published_block_id: str | None = None
    edited_block_id: str | None = None
    pr_url: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


__all__ = [
    "LessonCandidate",
    "LessonCandidateKind",
    "LessonCandidateStatus",
    "LessonPromotion",
]
