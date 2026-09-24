"""Benchmark-only capture of bounded server runtime-policy diagnostics.

The thin client never persists these facts in production.  When an explicit
benchmark path is provided, this module appends a small allow-listed JSONL row
so the benchmark can prove which policy actually executed.  Query text, tool
arguments, repository paths, source and tenant/session identifiers are never
accepted into the row.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from typing import Any, Final

_ATTRIBUTION_ENV: Final[str] = "LEMONCROW_RUNTIME_ATTRIBUTION_PATH"
_WRITE_LOCK = threading.Lock()
_MAX_REASON_CODES: Final[int] = 32
_MAX_TEXT: Final[int] = 96

_TEXT_FIELDS = (
    "policy",
    "policy_version",
    "mode",
    "evidence_status",
    "proposed_action",
    "actual_action",
)
_INT_FIELDS = (
    "rounds",
    "candidate_count_before",
    "candidate_count_after",
    "elapsed_ms",
)


def _bounded_policy_row(value: Mapping[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key in _TEXT_FIELDS:
        raw = value.get(key)
        if raw is not None:
            row[key] = str(raw)[:_MAX_TEXT]
    for key in _INT_FIELDS:
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            continue
        try:
            row[key] = max(0, int(raw))
        except ValueError:
            continue
    confidence = value.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        row["confidence"] = max(0.0, min(1.0, float(confidence)))
    reasons = value.get("reason_codes")
    if isinstance(reasons, (list, tuple)):
        row["reason_codes"] = [str(item)[:_MAX_TEXT] for item in reasons[:_MAX_REASON_CODES]]
    return row


def record_runtime_policy_attribution(diagnostics: Mapping[str, Any] | None) -> None:
    """Append one allow-listed policy diagnostic when benchmark capture is armed."""

    target = os.environ.get(_ATTRIBUTION_ENV, "").strip()
    if not target or not isinstance(diagnostics, Mapping):
        return
    raw = diagnostics.get("runtime_policy")
    if not isinstance(raw, Mapping):
        return
    row = _bounded_policy_row(raw)
    if not row.get("policy"):
        return
    data = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with _WRITE_LOCK:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)


__all__ = ["record_runtime_policy_attribution"]
