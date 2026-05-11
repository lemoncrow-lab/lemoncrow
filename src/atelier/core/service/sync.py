"""Sync mechanism for atelier.beseam.com."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier import __version__ as atelier_version
from atelier.core.foundation.identity import get_anon_id, platform_payload

logger = logging.getLogger(__name__)

_logger = logging.getLogger("atelier.sync")


def sync_usage(
    root: str | Path,
    sessions: list[dict[str, Any]] | None = None,
    session_ids: list[str] | None = None,
    chunk_size: int = 50,
) -> bool:
    """Report session-level usage metrics to atelier.beseam.com in chunks.

    This is used to track individual session performance against the machine ID.
    Large session lists are automatically chunked into multiple requests.
    Only successfully acknowledged chunks are marked as synced in the local DB.
    """
    url = os.environ.get("ATELIER_SYNC_URL", "https://atelier.beseam.com/api/sync")
    root_path = Path(root)

    from atelier.core.capabilities.plugin_runtime import (
        get_session_stats_from_trace,
    )
    from atelier.core.foundation.store import ReasoningStore

    store = ReasoningStore(root_path)

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
                stats_path = root_path / "session_stats" / f"{sid}.json"
                if stats_path.exists():
                    try:
                        chunk_sessions.append(json.loads(stats_path.read_text(encoding="utf-8")))
                        continue
                    except Exception:
                        logger.warning(
                            "Suppressed exception at sync.py:69",
                            exc_info=True,
                        )
                # Fallback to Trace reconstruction
                trace = store.get_trace(sid)
                if trace:
                    with contextlib.suppress(Exception):
                        chunk_sessions.append(get_session_stats_from_trace(trace))

            if chunk_sessions and _send_chunk(url, chunk_sessions):
                for s in chunk_sessions:
                    sid = s.get("id") or s.get("session_id")
                    if sid:
                        store.mark_synced(sid, _hash_dict(s))

    return True


def _send_chunk(url: str, sessions: list[dict[str, Any]]) -> bool:
    """Send a single chunk of sessions to the sync endpoint."""
    payload = {
        "machine_id": get_anon_id(),
        "timestamp": datetime.now(UTC).isoformat() + "Z",
        "atelier_version": atelier_version,
        "sessions": sessions,
        "metadata": platform_payload(),
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status < 400
    except Exception as e:
        _logger.debug(f"sync chunk to {url} failed: {e}")
        return False


def _hash_dict(d: dict[str, Any]) -> str:
    """Generate a stable hash for a dictionary."""
    s = json.dumps(d, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
