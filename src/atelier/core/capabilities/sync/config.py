"""Config and status helpers for encrypted cross-machine sync."""

from __future__ import annotations

import json
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

SYNC_CONFIG_VERSION = 1
SYNC_STATUS_VERSION = 1
SUPPORTED_SYNC_BACKENDS = ("cloud", "s3", "ssh")


@dataclass(slots=True)
class SyncConfig:
    backend: Literal["cloud", "s3", "ssh"]
    machine_id: str
    account_id: str
    backend_config: dict[str, Any] = field(default_factory=dict)
    version: int = SYNC_CONFIG_VERSION


def default_machine_id() -> str:
    return socket.gethostname().strip() or "atelier-machine"


def sync_config_path(root: Path) -> Path:
    return Path(root) / "sync.yaml"


def sync_status_path(root: Path) -> Path:
    return Path(root) / "sync_status.json"


def load_sync_config(root: Path) -> SyncConfig:
    path = sync_config_path(root)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Sync config at {path} is invalid")
    backend = str(data.get("backend") or "").strip().lower()
    if backend not in SUPPORTED_SYNC_BACKENDS:
        raise ValueError(f"Unsupported sync backend {backend!r}")
    machine_id = str(data.get("machine_id") or "").strip()
    account_id = str(data.get("account_id") or "default").strip() or "default"
    backend_config = data.get("backend_config") or {}
    if not machine_id:
        raise ValueError("sync config is missing machine_id")
    if not isinstance(backend_config, dict):
        raise ValueError("sync config backend_config must be a mapping")
    return SyncConfig(
        backend=backend,  # type: ignore[arg-type]
        machine_id=machine_id,
        account_id=account_id,
        backend_config=backend_config,
        version=int(data.get("version") or SYNC_CONFIG_VERSION),
    )


def save_sync_config(root: Path, config: SyncConfig) -> Path:
    path = sync_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(config)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def load_sync_status(root: Path) -> dict[str, Any]:
    path = sync_status_path(root)
    if not path.exists():
        return {
            "version": SYNC_STATUS_VERSION,
            "configured": False,
            "backend": None,
            "machine_id": None,
            "account_id": None,
            "last_push_at": None,
            "last_pull_at": None,
            "last_error": None,
            "last_push_counts": {},
            "last_pull_counts": {},
            "pending_uploads": 0,
            "machines": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "version": SYNC_STATUS_VERSION,
            "configured": False,
            "backend": None,
            "machine_id": None,
            "account_id": None,
            "last_push_at": None,
            "last_pull_at": None,
            "last_error": "sync status file is corrupted",
            "last_push_counts": {},
            "last_pull_counts": {},
            "pending_uploads": 0,
            "machines": [],
        }
    if not isinstance(data, dict):
        raise ValueError("sync status file must contain a JSON object")
    return data


def save_sync_status(root: Path, status: dict[str, Any]) -> Path:
    path = sync_status_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(status)
    payload.setdefault("version", SYNC_STATUS_VERSION)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


__all__ = [
    "SUPPORTED_SYNC_BACKENDS",
    "SyncConfig",
    "default_machine_id",
    "load_sync_config",
    "load_sync_status",
    "save_sync_config",
    "save_sync_status",
    "sync_config_path",
    "sync_status_path",
]
