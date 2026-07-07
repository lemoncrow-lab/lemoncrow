"""Append-only audit log for cross-vendor memory facts.

See docs/specs/day30/08-memory-audit-viewer.md for the full spec.

Layout on disk:
  <root>/memory_audit.jsonl          — main event log (one JSON object per line)
  <root>/cross_vendor_memory.yaml    — user overrides / allow-list config
"""

from __future__ import annotations

import hashlib
import json
import platform
import socket
from datetime import UTC, datetime
from pathlib import Path

from atelier.core.capabilities.cross_vendor_memory.models import AuditEvent

# ---------------------------------------------------------------------------
# Path helpers (used by sync_engine.py and serializer.py)
# ---------------------------------------------------------------------------


def audit_store_root(root: Path | str) -> Path:
    """Return the directory that contains audit JSONL files.

    Currently the same as ``root`` — audit files live at the top level of the
    Atelier store directory alongside other data files.
    """
    return Path(root).expanduser().resolve()


def audit_overrides_path(root: Path | str) -> Path:
    """Path to the cross-vendor memory user-override YAML file."""
    return audit_store_root(root) / "cross_vendor_memory.yaml"


def local_machine_id() -> str:
    """Return a stable, opaque identifier for the current machine.

    Priority:
    1. ``/etc/machine-id`` (Linux systemd)
    2. ``/var/lib/dbus/machine-id`` (older Linux)
    3. Fallback: SHA-256 of ``platform.node()`` (hostname)
    """
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            raw = Path(candidate).read_text(encoding="utf-8").strip()
            if raw:
                return raw
        except OSError:
            pass
    # Hostname-based fallback — not stable across renames but good enough
    hostname = socket.gethostname() or platform.node() or "unknown"
    return hashlib.sha256(hostname.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# MemoryAuditLog
# ---------------------------------------------------------------------------

_LOG_FILENAME = "memory_audit.jsonl"


class MemoryAuditLog:
    """Append-only log of ``AuditEvent`` records.

    Usage::

        log = MemoryAuditLog(root)
        log.append(AuditEvent(vendor="claude", event="added", ...))
        for event in log.read(since=yesterday):
            print(event.fact_id, event.content)
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).expanduser().resolve()
        self._path = self._root / _LOG_FILENAME

    # ------------------------------------------------------------------ #
    # Write                                                                #
    # ------------------------------------------------------------------ #

    def append(self, event: AuditEvent) -> None:
        """Append *event* to the log (creates the file if absent)."""
        self._root.mkdir(parents=True, exist_ok=True)
        record = event.to_public_record()
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    def read(self, *, since: datetime | None = None) -> list[AuditEvent]:
        """Return all events, optionally filtered to those at or after *since*."""
        if not self._path.exists():
            return []

        since_utc = since.astimezone(UTC) if since is not None else None
        results: list[AuditEvent] = []

        for raw_line in self._path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            event = AuditEvent.model_validate(record)

            if since_utc is not None:
                event_time = event.at.astimezone(UTC)
                if event_time < since_utc:
                    continue

            results.append(event)

        return results

    # ------------------------------------------------------------------ #
    # Convenience                                                          #
    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self.read())

    def clear(self) -> None:
        """Delete the log file (useful in tests)."""
        if self._path.exists():
            self._path.unlink()


__all__ = [
    "AuditEvent",
    "MemoryAuditLog",
    "audit_overrides_path",
    "audit_store_root",
    "local_machine_id",
]
