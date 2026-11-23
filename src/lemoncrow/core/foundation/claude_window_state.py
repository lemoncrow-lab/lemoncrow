"""Isolated, cross-process-safe Claude hook state.

Workspace metadata (session_state.json) is a shared discovery bridge, NOT a
safe place for mutable per-window warning counters or compaction bookkeeping.
The launch session id is stable for the life of a Claude window even across
/clear and /compact. On hosts without it, use the window process identity, then
the live session id as a conservative fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.run_file_io import RunFileLock, atomic_write_json
from lemoncrow.core.foundation.session_window import host_window_id


def claude_window_state_path(workspace: str | Path, session_id: str = "") -> Path:
    launch = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if launch:
        identity = "launch:" + launch
    else:
        window = host_window_id()
        if window is not None:
            identity = f"window:{window[0]}:{window[1]}"
        else:
            identity = "session:" + (session_id.strip() or "unbound")
    # Hash IDs from external hooks; they must never become path components.
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return Path(workspace).expanduser().resolve() / ".lemoncrow" / "workspace" / "claude_window_state" / f"{key}.json"


def read_claude_window_state(workspace: str | Path, session_id: str = "") -> dict[str, Any]:
    path = claude_window_state_path(workspace, session_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return state if isinstance(state, dict) else {}


def mutate_claude_window_state[T](workspace: str | Path, session_id: str, update: Callable[[dict[str, Any]], T]) -> T:
    """Apply an update under one cross-process lock; no lost read/modify/write."""
    path = claude_window_state_path(workspace, session_id)
    with RunFileLock(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            data = {}
        state: dict[str, Any] = data if isinstance(data, dict) else {}
        before = dict(state)
        outcome = update(state)
        if state != before:
            atomic_write_json(path, state)
        return outcome
