"""Bounded serializer for Atelier-owned sync state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.core.capabilities.cross_vendor_memory.audit_log import (
    audit_overrides_path,
    audit_store_root,
)
from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore
from atelier.core.foundation.memory_models import MemoryBlock
from atelier.infra.storage.factory import make_memory_store


@dataclass(slots=True)
class SyncEntity:
    key: str
    kind: str
    updated_at: str
    content_hash: str
    payload: dict[str, Any]


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_payload(payload: Any) -> str:
    import hashlib

    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def _read_json_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def collect_sync_entities(root: Path) -> dict[str, SyncEntity]:
    entities: dict[str, SyncEntity] = {}
    _collect_memory_entities(root, entities)
    _collect_audit_logs(root, entities)
    _collect_run_files(root, entities)
    _collect_typed_lessons(root, entities)
    _collect_optional_file(root / "live_savings_events.jsonl", "live_savings", "live_savings_events", entities)
    _collect_optional_file(root / "route.yaml", "config_file", "route.yaml", entities)
    _collect_optional_file(root / "team_workspace.json", "config_file", "team_workspace.json", entities)
    _collect_optional_file(root / "team_audit.jsonl", "config_file", "team_audit.jsonl", entities)
    _collect_optional_file(root / "governance.yaml", "config_file", "governance.yaml", entities)
    _collect_optional_file(audit_overrides_path(root), "config_file", "cross_vendor_memory.yaml", entities)
    return entities


def _collect_memory_entities(root: Path, entities: dict[str, SyncEntity]) -> None:
    store = make_memory_store(root)
    blocks = store.list_blocks(None, include_tombstoned=True, limit=5000)
    for block in blocks:
        if not isinstance(block, MemoryBlock):
            continue
        history = [item.model_dump(mode="json") for item in store.list_block_history(block.id, limit=200)]
        payload = {"block": block.model_dump(mode="json"), "history": history}
        key = f"memory_block:{block.agent_id}:{block.label}"
        entities[key] = SyncEntity(
            key=key,
            kind="memory_block",
            updated_at=block.updated_at.astimezone(UTC).isoformat(),
            content_hash=_hash_payload(payload),
            payload=payload,
        )


def _collect_audit_logs(root: Path, entities: dict[str, SyncEntity]) -> None:
    audit_root = audit_store_root(root)
    for path in sorted(audit_root.glob("*.jsonl")):
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        payload = {"path": path.name, "lines": lines}
        key = f"audit_log:{path.stem}"
        entities[key] = SyncEntity(
            key=key,
            kind="audit_log",
            updated_at=_mtime_iso(path),
            content_hash=_hash_payload(payload),
            payload=payload,
        )


def _collect_run_files(root: Path, entities: dict[str, SyncEntity]) -> None:
    runs_dir = root / "runs"
    if not runs_dir.exists():
        return
    run_state: dict[str, str] = {}
    for path in sorted(runs_dir.glob("*.json")):
        if path.name.endswith("_outcomes.json"):
            continue
        snapshot = _read_json_file(path)
        session_id = str(snapshot.get("session_id") or path.stem)
        if str(snapshot.get("status") or "running") == "running":
            run_state[session_id] = "running"
            continue
        run_state[session_id] = "done"
        payload = {"path": path.name, "snapshot": snapshot}
        key = f"run_snapshot:{session_id}"
        updated_at = str(snapshot.get("updated_at") or snapshot.get("created_at") or _mtime_iso(path))
        entities[key] = SyncEntity(
            key=key,
            kind="run_snapshot",
            updated_at=updated_at,
            content_hash=_hash_payload(payload),
            payload=payload,
        )
    for path in sorted(runs_dir.glob("*_outcomes.json")):
        session_id = path.stem.removesuffix("_outcomes")
        if run_state.get(session_id) == "running":
            continue
        payload = {"path": path.name, "data": _read_json_file(path)}
        key = f"outcome_file:{session_id}"
        entities[key] = SyncEntity(
            key=key,
            kind="outcome_file",
            updated_at=_mtime_iso(path),
            content_hash=_hash_payload(payload),
            payload=payload,
        )


def _collect_typed_lessons(root: Path, entities: dict[str, SyncEntity]) -> None:
    store = TypedLessonStore(root, create=False)
    for lesson in store.list_lessons():
        payload = {"lesson": lesson.model_dump(mode="json")}
        key = f"typed_lesson:{lesson.id}"
        updated_at = (
            lesson.last_applied_at.astimezone(UTC).isoformat()
            if lesson.last_applied_at is not None
            else lesson.captured_at.astimezone(UTC).isoformat()
        )
        entities[key] = SyncEntity(
            key=key,
            kind="typed_lesson",
            updated_at=updated_at,
            content_hash=_hash_payload(payload),
            payload=payload,
        )


def _collect_optional_file(path: Path, kind: str, name: str, entities: dict[str, SyncEntity]) -> None:
    if not path.exists():
        return
    payload = {"path": name, "content": path.read_text(encoding="utf-8")}
    key = f"{kind}:{name}"
    entities[key] = SyncEntity(
        key=key,
        kind=kind,
        updated_at=_mtime_iso(path),
        content_hash=_hash_payload(payload),
        payload=payload,
    )


def entity_from_record(record: dict[str, Any]) -> SyncEntity:
    return SyncEntity(
        key=str(record["key"]),
        kind=str(record["kind"]),
        updated_at=str(record["updated_at"]),
        content_hash=str(record["content_hash"]),
        payload=record["payload"],
    )


def entity_to_record(entity: SyncEntity) -> dict[str, Any]:
    return {
        "key": entity.key,
        "kind": entity.kind,
        "updated_at": entity.updated_at,
        "content_hash": entity.content_hash,
        "payload": entity.payload,
    }


__all__ = [
    "SyncEntity",
    "collect_sync_entities",
    "entity_from_record",
    "entity_to_record",
]
