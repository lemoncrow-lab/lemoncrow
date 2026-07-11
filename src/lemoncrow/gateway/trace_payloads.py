"""Helpers for trimming default-heavy trace payloads before transport."""

from __future__ import annotations

from typing import Any

from lemoncrow.core.foundation.models import TraceLearning, ValidationResult


def serialize_trace_learnings(
    learnings: list[str | dict[str, Any] | TraceLearning] | None,
) -> list[str | dict[str, Any]]:
    return [
        (
            item.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
            if isinstance(item, TraceLearning)
            else item
        )
        for item in (learnings or [])
    ]


def serialize_validation_results(
    validation_results: list[ValidationResult] | None,
) -> list[dict[str, Any]]:
    return [
        result.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
        for result in (validation_results or [])
    ]


__all__ = ["serialize_trace_learnings", "serialize_validation_results"]
