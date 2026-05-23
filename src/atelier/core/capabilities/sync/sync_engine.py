"""Sync engine for encrypted cross-machine Atelier state."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from atelier.core.capabilities.cross_vendor_memory.audit_log import audit_store_root
from atelier.core.capabilities.lesson_promotion.models import TypedLesson
from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore
from atelier.core.foundation.memory_models import MemoryBlock
from atelier.infra.storage.factory import make_memory_store

from .backends import RemoteEntityMeta, SyncBackend
from .backends.cloud import CloudAccessError, CloudSyncBackend
from .backends.s3 import S3SyncBackend
from .backends.ssh import SSHSyncBackend
from .config import (
    SyncConfig,
    default_machine_id,
    load_sync_config,
    load_sync_status,
    save_sync_config,
    save_sync_status,
)
from .encryption import decrypt_json, encrypt_json, validate_passphrase_strength
from .merge import merge_entities
from .serializer import SyncEntity, collect_sync_entities, entity_from_record, entity_to_record


class SyncError(RuntimeError):
    """Base class for sync failures."""


class SyncNotConfiguredError(SyncError):
    """Raised when sync commands are used before `sync init`."""


class SyncApplyError(SyncError):
    """Raised when a sync down operation cannot safely apply merged state."""


def init_sync(
    root: Path,
    *,
    backend: str,
    passphrase: str,
    backend_config: dict[str, Any] | None = None,
    machine_id: str | None = None,
    account_id: str = "default",
    allow_weak: bool = False,
) -> dict[str, Any]:
    validate_passphrase_strength(passphrase, allow_weak=allow_weak)
    resolved_machine_id = machine_id or default_machine_id()
    config = SyncConfig(
        backend=backend,  # type: ignore[arg-type]
        machine_id=resolved_machine_id,
        account_id=account_id,
        backend_config=backend_config or {},
    )
    backend_impl = _backend_from_config(config)
    save_sync_config(root, config)
    status = load_sync_status(root)
    status.update(
        {
            "configured": True,
            "backend": config.backend,
            "machine_id": config.machine_id,
            "account_id": config.account_id,
            "descriptor": backend_impl.descriptor(),
            "last_error": None,
            "pending_uploads": _pending_uploads(root, backend_impl),
        }
    )
    save_sync_status(root, status)
    return {
        "configured": True,
        "backend": config.backend,
        "machine_id": config.machine_id,
        "account_id": config.account_id,
        "descriptor": backend_impl.descriptor(),
        "keyring_cached": False,
        "warning": (
            "Passphrase caching is unavailable because keyring is not installed." if _keyring_missing() else None
        ),
    }


def sync_up(root: Path, *, passphrase: str | None = None) -> dict[str, Any]:
    config = _require_config(root)
    secret = _require_passphrase(config, passphrase)
    backend = _backend_from_config(config)
    local_entities = collect_sync_entities(root)
    remote_index = backend.load_index()
    uploaded: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    for key, entity in local_entities.items():
        remote_meta = remote_index.get(key)
        if remote_meta is not None and remote_meta.content_hash == entity.content_hash:
            skipped.append(key)
            continue
        blob_name = _blob_name(entity)
        payload = encrypt_json(
            {
                "entity": entity_to_record(entity),
                "machine_id": config.machine_id,
            },
            secret,
            aad=key.encode("utf-8"),
        )
        try:
            backend.upload_blob(blob_name, payload)
            remote_index[key] = RemoteEntityMeta(
                key=entity.key,
                kind=entity.kind,
                updated_at=entity.updated_at,
                content_hash=entity.content_hash,
                blob_name=blob_name,
                machine_id=config.machine_id,
            )
            backend.save_index(remote_index)
            uploaded.append(key)
        except Exception as exc:
            failed.append({"key": key, "error": str(exc)})
    status = load_sync_status(root)
    status.update(
        {
            "configured": True,
            "backend": config.backend,
            "machine_id": config.machine_id,
            "account_id": config.account_id,
            "descriptor": backend.descriptor(),
            "last_push_at": datetime.now(UTC).isoformat(),
            "last_error": failed[0]["error"] if failed else None,
            "last_push_counts": _count_kinds(local_entities, uploaded),
            "pending_uploads": _pending_uploads(root, backend),
            "machines": sorted({meta.machine_id for meta in remote_index.values()}),
        }
    )
    save_sync_status(root, status)
    return {
        "backend": config.backend,
        "uploaded": uploaded,
        "skipped": skipped,
        "failed": failed,
        "counts": _count_kinds(local_entities, uploaded),
    }


def sync_down(root: Path, *, passphrase: str | None = None) -> dict[str, Any]:
    config = _require_config(root)
    secret = _require_passphrase(config, passphrase)
    backend = _backend_from_config(config)
    local_entities = collect_sync_entities(root)
    remote_index = backend.load_index()
    remote_entities: dict[str, SyncEntity] = {}
    downloaded: list[str] = []
    for key, meta in remote_index.items():
        local_entity = local_entities.get(key)
        if local_entity is not None and local_entity.content_hash == meta.content_hash:
            remote_entities[key] = local_entity
            continue
        blob = backend.download_blob(meta.blob_name)
        record = decrypt_json(blob, secret, aad=key.encode("utf-8"))
        remote_entities[key] = entity_from_record(record["entity"])
        downloaded.append(key)
    merged = merge_entities(local_entities, remote_entities)
    _apply_merged_entities(root, local_entities, merged.entities, config=config)
    status = load_sync_status(root)
    status.update(
        {
            "configured": True,
            "backend": config.backend,
            "machine_id": config.machine_id,
            "account_id": config.account_id,
            "descriptor": backend.descriptor(),
            "last_pull_at": datetime.now(UTC).isoformat(),
            "last_error": None,
            "last_pull_counts": _count_kinds(merged.entities, downloaded),
            "pending_uploads": _pending_uploads(root, backend),
            "machines": sorted({meta.machine_id for meta in remote_index.values()}),
            "last_conflicts": merged.conflicts,
        }
    )
    save_sync_status(root, status)
    return {
        "backend": config.backend,
        "downloaded": downloaded,
        "counts": _count_kinds(merged.entities, downloaded),
        "conflicts": merged.conflicts,
    }


def sync_bidirectional(root: Path, *, passphrase: str | None = None) -> dict[str, Any]:
    pulled = sync_down(root, passphrase=passphrase)
    pushed = sync_up(root, passphrase=passphrase)
    return {"down": pulled, "up": pushed}


def sync_status(root: Path) -> dict[str, Any]:
    status = load_sync_status(root)
    try:
        config = load_sync_config(root)
    except Exception:
        status.setdefault("configured", False)
        return status
    try:
        backend = _backend_from_config(config)
        remote_index = backend.load_index()
        status.update(
            {
                "configured": True,
                "backend": config.backend,
                "machine_id": config.machine_id,
                "account_id": config.account_id,
                "descriptor": backend.descriptor(),
                "pending_uploads": _pending_uploads(root, backend),
                "machines": sorted({meta.machine_id for meta in remote_index.values()}),
                "backend_state": "ok",
            }
        )
    except CloudAccessError as exc:
        status.update(
            {
                "configured": True,
                "backend": config.backend,
                "machine_id": config.machine_id,
                "account_id": config.account_id,
                "pending_uploads": 0,
                "machines": [],
                "backend_state": "unavailable",
                "last_error": str(exc),
            }
        )
    return status


def render_sync_status_text(status: dict[str, Any]) -> str:
    if not status.get("configured"):
        return "Sync is not configured. Run `atelier sync init` first."
    lines = [
        f"Backend: {status.get('backend')}",
        f"Machine ID: {status.get('machine_id')}",
        f"Descriptor: {status.get('descriptor')}",
        f"Backend state: {status.get('backend_state', 'ok')}",
        f"Pending uploads: {status.get('pending_uploads', 0)}",
        f"Last push: {status.get('last_push_at') or '-'}",
        f"Last pull: {status.get('last_pull_at') or '-'}",
        f"Machines: {', '.join(status.get('machines') or []) or '-'}",
    ]
    if status.get("last_error"):
        lines.append(f"Last error: {status['last_error']}")
    return "\n".join(lines)


def _require_config(root: Path) -> SyncConfig:
    try:
        return load_sync_config(root)
    except FileNotFoundError as exc:
        raise SyncNotConfiguredError("Sync is not configured. Run `atelier sync init` first.") from exc


def _backend_from_config(config: SyncConfig) -> SyncBackend:
    backend_config = dict(config.backend_config)
    if config.backend == "cloud":
        remote_root = backend_config.get("root_dir")
        api_url = backend_config.get("api_url")
        return CloudSyncBackend(
            remote_root=Path(str(remote_root)).expanduser().resolve() if remote_root else None,
            api_url=str(api_url) if api_url else None,
            account_id=config.account_id,
        )
    if config.backend == "s3":
        remote_root = backend_config.get("root_dir")
        if not remote_root:
            raise SyncError("S3 sync requires backend_config.root_dir for this repo's local self-host adapter.")
        return S3SyncBackend(
            remote_root=Path(str(remote_root)).expanduser().resolve(),
            account_id=config.account_id,
            bucket=str(backend_config.get("bucket") or "atelier-sync"),
            prefix=str(backend_config.get("prefix") or "default"),
        )
    if config.backend == "ssh":
        remote_root = backend_config.get("root_dir")
        if not remote_root:
            raise SyncError("SSH sync requires backend_config.root_dir for this repo's local self-host adapter.")
        return SSHSyncBackend(
            remote_root=Path(str(remote_root)).expanduser().resolve(),
            account_id=config.account_id,
            host=str(backend_config.get("host") or "localhost"),
            remote_path=str(backend_config.get("remote_path") or "atelier-sync"),
        )
    raise SyncError(f"Unsupported sync backend: {config.backend}")


def _blob_name(entity: SyncEntity) -> str:
    return f"{hashlib.sha256(entity.key.encode('utf-8')).hexdigest()}-{entity.content_hash[:16]}.json"


def _require_passphrase(config: SyncConfig, provided: str | None) -> str:
    if provided:
        return provided
    try:
        import importlib

        keyring_module = importlib.import_module("keyring")
    except ImportError:
        raise SyncError("Passphrase required. Re-run with --passphrase because keyring is not installed.") from None
    get_password = getattr(keyring_module, "get_password", None)
    if not callable(get_password):
        raise SyncError("Passphrase required. Re-run with --passphrase because keyring support is unavailable.")
    secret = get_password("atelier-sync", f"{config.account_id}:{config.machine_id}:{config.backend}")
    if not isinstance(secret, str) or not secret:
        raise SyncError("Passphrase required. Re-run with --passphrase.")
    return secret


def _keyring_missing() -> bool:
    return find_spec("keyring") is None


def _pending_uploads(root: Path, backend: SyncBackend) -> int:
    local = collect_sync_entities(root)
    remote = backend.load_index()
    return sum(
        1 for key, entity in local.items() if remote.get(key) is None or remote[key].content_hash != entity.content_hash
    )


def _count_kinds(entities: dict[str, SyncEntity], keys: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in keys:
        entity = entities.get(key)
        if entity is None:
            continue
        counts[entity.kind] = counts.get(entity.kind, 0) + 1
    return counts


def _apply_merged_entities(
    root: Path,
    local_entities: dict[str, SyncEntity],
    merged_entities: dict[str, SyncEntity],
    *,
    config: SyncConfig,
) -> None:
    changed = [
        entity
        for key, entity in merged_entities.items()
        if local_entities.get(key) is None or local_entities[key].content_hash != entity.content_hash
    ]
    if not changed:
        return
    mem_store = make_memory_store(root)
    db_path = getattr(mem_store, "db_path", None)
    db_backup: Path | None = None
    lessons_path = root / "lessons.sqlite"
    lessons_backup: Path | None = None
    file_backups: dict[Path, bytes | None] = {}
    try:
        if any(entity.kind == "memory_block" for entity in changed) and db_path is not None and Path(db_path).exists():
            db_backup = Path(tempfile.mkdtemp(prefix="atelier-sync-db-")) / "store.sqlite"
            shutil.copy2(Path(db_path), db_backup)
        if any(entity.kind == "typed_lesson" for entity in changed) and lessons_path.exists():
            lessons_backup = Path(tempfile.mkdtemp(prefix="atelier-sync-lessons-")) / "lessons.sqlite"
            shutil.copy2(lessons_path, lessons_backup)
        for entity in changed:
            target = _target_path(root, entity)
            if target is not None and target not in file_backups:
                file_backups[target] = target.read_bytes() if target.exists() else None
        for entity in changed:
            _apply_entity(root, mem_store, entity, config=config)
    except Exception as exc:
        if db_backup is not None and db_path is not None:
            shutil.copy2(db_backup, Path(db_path))
        if lessons_backup is not None:
            shutil.copy2(lessons_backup, lessons_path)
        for target, content in file_backups.items():
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        raise SyncApplyError(f"Sync down failed before local state could be applied safely: {exc}") from exc


def _target_path(root: Path, entity: SyncEntity) -> Path | None:
    if entity.kind == "audit_log":
        return audit_store_root(root) / str(entity.payload["path"])
    if entity.kind in {"run_snapshot", "outcome_file"}:
        return root / "runs" / str(entity.payload["path"])
    if entity.kind == "live_savings":
        return root / str(entity.payload["path"])
    if entity.kind == "config_file":
        return root / str(entity.payload["path"])
    return None


def _apply_entity(root: Path, mem_store: Any, entity: SyncEntity, *, config: SyncConfig) -> None:
    if entity.kind == "memory_block":
        incoming = MemoryBlock.model_validate(entity.payload["block"])
        existing = mem_store.get_block(incoming.agent_id, incoming.label, include_tombstoned=True)
        actor = f"sync:{config.machine_id}"
        if existing is None:
            mem_store.upsert_block(incoming, actor=actor, reason="sync down merge")
            return
        update = incoming.model_dump(mode="python", exclude={"id", "version", "current_history_id"})
        merged = existing.model_copy(update=update)
        mem_store.upsert_block(merged, actor=actor, reason="sync down merge")
        return
    if entity.kind == "typed_lesson":
        lesson = TypedLesson.model_validate(entity.payload["lesson"])
        TypedLessonStore(root).upsert_lesson(lesson)
        return
    target = _target_path(root, entity)
    if target is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if entity.kind in {"run_snapshot", "outcome_file"}:
        body = entity.payload.get("snapshot") if entity.kind == "run_snapshot" else entity.payload.get("data")
        target.write_text(json.dumps(body, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return
    if entity.kind in {"audit_log", "live_savings"}:
        lines = [line for line in entity.payload.get("lines") or [] if isinstance(line, str) and line.strip()]
        target.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        return
    if entity.kind == "config_file":
        target.write_text(str(entity.payload.get("content") or ""), encoding="utf-8")


__all__ = [
    "CloudAccessError",
    "SyncApplyError",
    "SyncError",
    "SyncNotConfiguredError",
    "init_sync",
    "render_sync_status_text",
    "sync_bidirectional",
    "sync_down",
    "sync_status",
    "sync_up",
]
