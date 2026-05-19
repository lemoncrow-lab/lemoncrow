"""Merge rules for encrypted cross-machine sync."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .serializer import SyncEntity


@dataclass(slots=True)
class MergeResult:
    entities: dict[str, SyncEntity]
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def merge_entities(local: dict[str, SyncEntity], remote: dict[str, SyncEntity]) -> MergeResult:
    entities: dict[str, SyncEntity] = {}
    conflicts: list[dict[str, Any]] = []
    for key in sorted(set(local) | set(remote)):
        left = local.get(key)
        right = remote.get(key)
        if left is None:
            entities[key] = right  # type: ignore[assignment]
            continue
        if right is None or left.content_hash == right.content_hash:
            entities[key] = left
            continue
        if left.kind != right.kind:
            entities[key] = right
            conflicts.append({"key": key, "winner": "remote", "reason": "kind_mismatch"})
            continue
        if left.kind in {"audit_log", "live_savings"}:
            entities[key] = _merge_line_union(left, right)
            continue
        if left.kind == "memory_block":
            winner = _pick_newer(left, right)
            entities[key] = winner
            conflicts.append(
                {
                    "key": key,
                    "winner": "local" if winner is left else "remote",
                    "reason": "last_write_wins",
                }
            )
            continue
        winner = _pick_newer(left, right)
        entities[key] = winner
        conflicts.append(
            {
                "key": key,
                "winner": "local" if winner is left else "remote",
                "reason": "last_write_wins",
            }
        )
    return MergeResult(entities=entities, conflicts=conflicts)


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _pick_newer(left: SyncEntity, right: SyncEntity) -> SyncEntity:
    if _parse_ts(left.updated_at) > _parse_ts(right.updated_at):
        return left
    if _parse_ts(left.updated_at) < _parse_ts(right.updated_at):
        return right
    return right if right.content_hash >= left.content_hash else left


def _merge_line_union(left: SyncEntity, right: SyncEntity) -> SyncEntity:
    left_lines = list(left.payload.get("lines") or [])
    right_lines = list(right.payload.get("lines") or [])
    seen: set[str] = set()
    merged_lines: list[str] = []
    for line in [*left_lines, *right_lines]:
        if not isinstance(line, str) or line in seen:
            continue
        seen.add(line)
        merged_lines.append(line)
    payload = dict(right.payload)
    payload["lines"] = merged_lines
    return SyncEntity(
        key=right.key,
        kind=right.kind,
        updated_at=max(left.updated_at, right.updated_at),
        content_hash=_hash_payload(payload),
        payload=payload,
    )


def _hash_payload(payload: Any) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


__all__ = ["MergeResult", "merge_entities"]
