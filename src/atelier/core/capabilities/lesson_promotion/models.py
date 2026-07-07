"""Typed adaptive lesson models."""

from __future__ import annotations

from datetime import UTC, datetime
from math import pow
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atelier.core.foundation.models import _utcnow
from atelier.infra.storage.ids import make_uuid7

TypedLessonKind = Literal["route-preference", "cost-cap"]
LessonScope = Literal["user", "team", "workspace"]
CostCapBreachMode = Literal["downgrade-one-tier", "warn", "block"]


def _lesson_id() -> str:
    return f"tl-{make_uuid7()}"


class TypedLesson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_lesson_id)
    kind: TypedLessonKind
    scope: LessonScope = "user"
    enabled: bool = True
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_session_id: str | None = None
    captured_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime | None = None
    decay_half_life_days: int | None = 30
    last_applied_at: datetime | None = None
    match: dict[str, str] = Field(default_factory=dict)
    prefer: dict[str, str] = Field(default_factory=dict)
    limit_usd_per_session: float | None = None
    on_breach: CostCapBreachMode | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> TypedLesson:
        if self.kind == "route-preference":
            if not self.match:
                raise ValueError("route-preference lessons require match")
            if not self.prefer:
                raise ValueError("route-preference lessons require prefer")
        if self.kind == "cost-cap":
            if self.limit_usd_per_session is None:
                raise ValueError("cost-cap lessons require limit_usd_per_session")
            if self.on_breach is None:
                raise ValueError("cost-cap lessons require on_breach")
        if self.decay_half_life_days is not None and self.decay_half_life_days <= 0:
            raise ValueError("decay_half_life_days must be positive")
        return self

    def effective_confidence_at(self, at: datetime | None = None) -> float:
        current = at or datetime.now(UTC)
        if self.expires_at is not None and current >= self.expires_at:
            return 0.0
        if self.decay_half_life_days is None:
            return self.confidence
        elapsed_days = max(0.0, (current - self.captured_at).total_seconds() / 86_400.0)
        factor = pow(0.5, elapsed_days / float(self.decay_half_life_days))
        return max(0.0, min(1.0, self.confidence * factor))

    def is_active_at(self, at: datetime | None = None, *, scope: LessonScope = "user") -> bool:
        if not self.enabled:
            return False
        if self.scope != scope:
            return False
        return self.effective_confidence_at(at) >= 0.4

    def applies_without_tiebreaker_at(self, at: datetime | None = None) -> bool:
        return self.effective_confidence_at(at) >= 0.7


__all__ = ["CostCapBreachMode", "LessonScope", "TypedLesson", "TypedLessonKind"]
