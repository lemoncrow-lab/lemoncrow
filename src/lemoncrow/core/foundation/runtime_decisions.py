"""Provider- and transport-neutral runtime decision facts.

The runtime has several existing evidence stores (run ledger, optimization
traces, benchmark evidence). This module defines the one structured fact they
can all consume without making any of those stores the owner of runtime policy.

Decision events contain observable facts and bounded policy outputs, never
hidden model reasoning. Mapping fields are sanitized at construction time so
new sinks cannot accidentally become a second prompt/source/secret store.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

RuntimeDecisionMode = Literal["observe", "shadow", "enforce"]

_SCHEMA_VERSION = 1
_MAX_COLLECTION_ITEMS = 64
_MAX_DEPTH = 6
_MAX_KEY_CHARS = 64
_MAX_VALUE_CHARS = 256
_MAX_REF_CHARS = 512
_SENSITIVE_KEYS = frozenset(
    {
        "args",
        "arguments",
        "command",
        "content",
        "context",
        "diff",
        "file",
        "files",
        "path",
        "paths",
        "prompt",
        "query",
        "result",
        "source",
        "sources",
        "task",
        "task_text",
        "text",
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:20]


def _sanitize_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return a bounded JSON-compatible value with sensitive leaves hashed."""
    if key.lower() in _SENSITIVE_KEYS:
        return {"redacted_sha256": _fingerprint(str(value))}
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:_MAX_VALUE_CHARS]
    if depth >= _MAX_DEPTH:
        return {"truncated": "max_depth"}
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (item_key, item_value) in enumerate(value.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                break
            safe_key = str(item_key)[:_MAX_KEY_CHARS]
            result[safe_key] = _sanitize_value(item_value, key=safe_key, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_value(item, depth=depth + 1) for item in list(value)[:_MAX_COLLECTION_ITEMS]]
    return str(value)[:_MAX_VALUE_CHARS]


def _sanitize_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("runtime decision mapping fields must be mappings")
    sanitized = _sanitize_value(value)
    return dict(sanitized) if isinstance(sanitized, Mapping) else {}


class RuntimeDecisionEvent(BaseModel):
    """One versioned, bounded runtime policy decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=_SCHEMA_VERSION, ge=1)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=64)
    at: datetime = Field(default_factory=_utcnow)

    kind: str = Field(min_length=1, max_length=64)
    phase: str = Field(min_length=1, max_length=32)
    policy: str = Field(min_length=1, max_length=96)
    policy_version: str = Field(default="1", min_length=1, max_length=64)
    mode: RuntimeDecisionMode = "observe"

    session_id: str | None = Field(default=None, max_length=256)
    workspace_revision: str | int | None = None

    evidence_refs: tuple[str, ...] = ()
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_codes: tuple[str, ...] = ()

    proposed: dict[str, Any] = Field(default_factory=dict)
    actual: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @field_validator("proposed", "actual", "budget", "metrics", mode="before")
    @classmethod
    def _sanitize_mapping_fields(cls, value: Any) -> dict[str, Any]:
        return _sanitize_mapping(value)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _bound_evidence_refs(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        items = (value,) if isinstance(value, str) else value
        return tuple(str(item)[:_MAX_REF_CHARS] for item in list(items)[:_MAX_COLLECTION_ITEMS])

    @field_validator("reason_codes", mode="before")
    @classmethod
    def _bound_reason_codes(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        items = (value,) if isinstance(value, str) else value
        return tuple(str(item)[:_MAX_KEY_CHARS] for item in list(items)[:_MAX_COLLECTION_ITEMS])

    @field_validator("workspace_revision", mode="before")
    @classmethod
    def _bound_workspace_revision(cls, value: Any) -> str | int | None:
        if value is None or isinstance(value, int):
            return value
        return str(value)[:_MAX_VALUE_CHARS]

    def to_payload(self) -> dict[str, Any]:
        """JSON-compatible representation suitable for an existing sink."""
        return self.model_dump(mode="json")


@runtime_checkable
class RuntimeDecisionSink(Protocol):
    """Structural sink contract used by runtime subsystems."""

    def record_runtime_decision(self, event: RuntimeDecisionEvent) -> Any:
        """Record event in the sink's existing storage contract."""


class NullRuntimeDecisionSink:
    """Explicit no-op sink for call sites where instrumentation is disabled."""

    def record_runtime_decision(self, event: RuntimeDecisionEvent) -> None:
        del event


__all__ = [
    "NullRuntimeDecisionSink",
    "RuntimeDecisionEvent",
    "RuntimeDecisionMode",
    "RuntimeDecisionSink",
]
