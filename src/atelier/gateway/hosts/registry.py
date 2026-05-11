"""Host registry — registration, tracking, persistence."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from uuid import UUID

from atelier.gateway.hosts.models import HostFingerprint, HostRegistration, HostStatus


class HostRegistry:
    """In-memory host registry with filesystem persistence."""

    def __init__(self, storage_dir: Path | None = None):
        """Initialize registry.

        Args:
            storage_dir: Where to store host registrations (.atelier/hosts/)
        """
        if storage_dir is None:
            from atelier.core.foundation.paths import default_store_root

            storage_dir = default_store_root() / "hosts"
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # In-memory registry
        self._hosts: dict[str, HostRegistration] = {}
        self._lock = threading.RLock()

        # Load from disk
        self._load()

    def register(self, atelier_version: str) -> HostRegistration:
        """Register a new host or get existing registration.

        If host already registered (same fingerprint), return existing.
        Otherwise create new registration.
        """
        with self._lock:
            fingerprint = HostFingerprint.generate()
            existing = self._find_by_fingerprint(fingerprint.fingerprint_hash)

            if existing:
                # Update last_seen
                existing.last_seen = datetime.utcnow()
                existing.atelier_version = atelier_version
                self._persist(existing)
                return existing

            # Create new registration
            registration = HostRegistration(
                fingerprint=fingerprint,
                atelier_version=atelier_version,
            )
            self._hosts[str(registration.host_id)] = registration
            self._persist(registration)
            return registration

    def get(self, host_id: str | UUID) -> HostRegistration | None:
        """Get host registration by ID."""
        host_id_str = str(host_id)
        with self._lock:
            return self._hosts.get(host_id_str)

    def get_or_register(self, atelier_version: str) -> HostRegistration:
        """Get existing registration or register new."""
        fingerprint = HostFingerprint.generate()
        existing = self._find_by_fingerprint(fingerprint.fingerprint_hash)
        if existing:
            return existing
        return self.register(atelier_version)

    def list_all(self) -> list[HostRegistration]:
        """List all registered hosts."""
        with self._lock:
            return list(self._hosts.values())

    def update_status(self, host_id: str | UUID, status: HostStatus) -> HostRegistration | None:
        """Update host status (packs, tools, last_seen)."""
        registration = self.get(host_id)
        if not registration:
            return None

        with self._lock:
            registration.last_seen = status.last_seen
            registration.metadata["installed_packs"] = status.installed_packs
            registration.metadata["mcp_tools"] = status.available_mcp_tools
            self._persist(registration)

        return registration

    def unregister(self, host_id: str | UUID) -> bool:
        """Unregister a host."""
        host_id_str = str(host_id)
        with self._lock:
            if host_id_str in self._hosts:
                del self._hosts[host_id_str]
                # Delete file
                (self.storage_dir / f"{host_id_str}.json").unlink(missing_ok=True)
                return True
        return False

    def verify_fingerprint(self, host_id: str | UUID, fingerprint_hash: str) -> bool:
        """Verify that host_id matches expected fingerprint."""
        registration = self.get(host_id)
        if not registration:
            return False
        return registration.fingerprint.fingerprint_hash == fingerprint_hash

    # Private methods

    def _find_by_fingerprint(self, fingerprint_hash: str) -> HostRegistration | None:
        """Find host by fingerprint hash."""
        for host in self._hosts.values():
            if host.fingerprint.fingerprint_hash == fingerprint_hash:
                return host
        return None

    def _persist(self, registration: HostRegistration) -> None:
        """Save registration to disk."""
        file = self.storage_dir / f"{registration.host_id}.json"
        with open(file, "w") as f:
            data = registration.model_dump(mode="json")
            json.dump(data, f, indent=2, default=str)

    def _load(self) -> None:
        """Load all registrations from disk."""
        with self._lock:
            for file in self.storage_dir.glob("*.json"):
                try:
                    with open(file) as f:
                        data = json.load(f)
                    registration = HostRegistration(**data)
                    self._hosts[str(registration.host_id)] = registration
                except Exception as e:
                    # Log warning but continue
                    print(f"Warning: Failed to load {file}: {e}")


__all__ = ["HostRegistry", "HostStatus"]
