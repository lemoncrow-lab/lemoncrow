"""Backend interfaces for encrypted cross-machine sync."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol


class SyncBackendError(RuntimeError):
    """Raised when a sync backend operation fails."""


@dataclass(slots=True)
class RemoteEntityMeta:
    key: str
    kind: str
    updated_at: str
    content_hash: str
    blob_name: str
    machine_id: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RemoteEntityMeta:
        return cls(
            key=str(payload["key"]),
            kind=str(payload["kind"]),
            updated_at=str(payload["updated_at"]),
            content_hash=str(payload["content_hash"]),
            blob_name=str(payload["blob_name"]),
            machine_id=str(payload.get("machine_id") or "unknown"),
        )


class SyncBackend(Protocol):
    backend_name: str

    def descriptor(self) -> str: ...

    def load_index(self) -> dict[str, RemoteEntityMeta]: ...

    def save_index(self, index: dict[str, RemoteEntityMeta]) -> None: ...

    def upload_blob(self, blob_name: str, payload: bytes) -> None: ...

    def download_blob(self, blob_name: str) -> bytes: ...


class DirectorySyncBackend:
    """Filesystem-backed backend used by the self-host adapters and local cloud tests."""

    backend_name = "filesystem"

    def __init__(self, remote_root: Path, *, descriptor_text: str, backend_name: str) -> None:
        self._remote_root = remote_root
        self._descriptor_text = descriptor_text
        self.backend_name = backend_name

    def descriptor(self) -> str:
        return self._descriptor_text

    def load_index(self) -> dict[str, RemoteEntityMeta]:
        path = self._remote_root / "index.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SyncBackendError(f"Invalid sync index at {path}")
        return {key: RemoteEntityMeta.from_dict(value) for key, value in data.items() if isinstance(value, dict)}

    def save_index(self, index: dict[str, RemoteEntityMeta]) -> None:
        self._remote_root.mkdir(parents=True, exist_ok=True)
        path = self._remote_root / "index.json"
        payload = {key: asdict(meta) for key, meta in index.items()}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def upload_blob(self, blob_name: str, payload: bytes) -> None:
        path = self._remote_root / "blobs" / blob_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def download_blob(self, blob_name: str) -> bytes:
        path = self._remote_root / "blobs" / blob_name
        if not path.exists():
            raise SyncBackendError(f"Remote blob not found: {blob_name}")
        return path.read_bytes()


__all__ = ["DirectorySyncBackend", "RemoteEntityMeta", "SyncBackend", "SyncBackendError"]
