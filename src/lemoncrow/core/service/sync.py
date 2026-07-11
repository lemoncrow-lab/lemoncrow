"""Sync mechanism for lemoncrow.beseam.com."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow import __version__ as lemoncrow_version
from lemoncrow.core.foundation.identity import get_anon_id, platform_payload

logger = logging.getLogger(__name__)

_logger = logging.getLogger("lemoncrow.sync")


def sync_usage(
    root: str | Path,
    sessions: list[dict[str, Any]] | None = None,
    session_ids: list[str] | None = None,
    chunk_size: int = 50,
) -> bool:
    """Report session-level usage metrics to lemoncrow.beseam.com in chunks.

    This is used to track individual session performance against the machine ID.
    Large session lists are automatically chunked into multiple requests.
    Only successfully acknowledged chunks are marked as synced in the local DB.
    """
    from lemoncrow.core.service.telemetry.config import remote_enabled

    if not remote_enabled():
        # User opted out of remote telemetry -- nothing leaves the machine.
        _logger.debug("sync_usage skipped: remote telemetry disabled")
        return False

    url = os.environ.get("LEMONCROW_SYNC_URL", "https://lemoncrow.beseam.com/api/sync")
    root_path = Path(root)

    from lemoncrow.core.capabilities.plugin_runtime import (
        get_session_stats_from_trace,
    )
    from lemoncrow.core.foundation.store import ContextStore

    store = ContextStore(root_path)

    # 1. Handle pre-loaded sessions (explicitly provided)
    if sessions:
        for i in range(0, len(sessions), chunk_size):
            chunk = sessions[i : i + chunk_size]
            if _send_chunk(url, chunk):
                for s in chunk:
                    sid = s.get("id") or s.get("session_id")
                    if sid:
                        store.mark_synced(sid, _hash_dict(s))

    # 2. Handle session IDs (lazy fetch to avoid memory bloat)
    # If session_ids is None, fetch all unsynced IDs from the DB
    if session_ids is None and sessions is None:
        session_ids = store.list_unsynced_trace_ids(limit=1000)

    if session_ids:
        for i in range(0, len(session_ids), chunk_size):
            chunk_ids = session_ids[i : i + chunk_size]
            chunk_sessions = []
            for sid in chunk_ids:
                # Try live stats first
                from lemoncrow.core.foundation.paths import find_session_dir

                _existing = find_session_dir(root_path, sid)
                stats_path = (
                    (_existing / "stats.json")
                    if _existing is not None
                    else (root_path / "sessions" / sid / "stats.json")
                )
                if stats_path.exists():
                    try:
                        chunk_sessions.append(json.loads(stats_path.read_text(encoding="utf-8")))
                        continue
                    except (json.JSONDecodeError, OSError):
                        logger.warning(
                            "Suppressed exception at sync.py:69",
                            exc_info=True,
                        )
                # Fallback to Trace reconstruction
                trace = store.get_trace(sid)
                if trace:
                    with contextlib.suppress(json.JSONDecodeError, OSError):
                        chunk_sessions.append(get_session_stats_from_trace(trace))

            if chunk_sessions and _send_chunk(url, chunk_sessions):
                for s in chunk_sessions:
                    sid = s.get("id") or s.get("session_id")
                    if sid:
                        store.mark_synced(sid, _hash_dict(s))

    return True


def _sanitize_session(session: dict[str, Any]) -> dict[str, Any]:
    """Strip user-content fields so only metrics/ids leave the machine.

    ``task`` carries prompt-derived text (see ``get_session_stats_from_trace``
    and per-session ``stats.json``) and must never be sent off-machine.
    """
    cleaned = dict(session)
    cleaned.pop("task", None)
    return cleaned


def _send_chunk(url: str, sessions: list[dict[str, Any]]) -> bool:
    """Send a single chunk of sessions to the sync endpoint."""
    payload = {
        "machine_id": get_anon_id(),
        "timestamp": datetime.now(UTC).isoformat(),
        "lemoncrow_version": lemoncrow_version,
        "sessions": [_sanitize_session(s) for s in sessions],
        "metadata": platform_payload(),
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            try:
                status = int(getattr(resp, "status", 0))
            except (ValueError, TypeError):
                return False
            return status < 400
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _logger.debug(f"sync chunk to {url} failed: {e}")
        return False


def _hash_dict(d: dict[str, Any]) -> str:
    """Generate a stable hash for a dictionary."""
    s = json.dumps(d, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
