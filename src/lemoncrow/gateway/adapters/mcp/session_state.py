"""MCP process identity + session-registration file + managed-bash ownership.

Foundational session-state substrate shared by the dispatch loop and the tool
handlers. No ``mcp_server`` import, so any tool module can depend on it.

Extracted verbatim from ``mcp_server.py`` (behaviour-preserving); ``mcp_server``
re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid as _uuid_mod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Per-process unique id; the SessionStart hook writes the Claude session UUID +
# model into the registration file named by it.
_MCP_ID: str = f"lemoncrow-{_uuid_mod.uuid4().hex[:16]}"


def _lemoncrow_root() -> Path:
    from lemoncrow.core.foundation.paths import default_store_root

    return Path(os.environ.get("LEMONCROW_ROOT", str(default_store_root())))


_MCP_SESSION_FILE_LOCK = threading.Lock()


def _mcp_session_file() -> Path:
    """Path to this MCP process's registration file.

    Written at startup; SessionStart hook writes claude_session_id + model into it.
    """
    return _lemoncrow_root() / "mcp_sessions" / f"{_MCP_ID}.json"


def _mutate_mcp_managed_bash(*, record: dict[str, Any] | None = None, remove_id: str = "") -> None:
    """Atomically update live Bash ownership in this MCP registration."""
    path = _mcp_session_file()
    with _MCP_SESSION_FILE_LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            commands = [row for row in data.get("managed_bash", []) if isinstance(row, dict)]
            target_id = remove_id or str((record or {}).get("session_id") or "")
            if target_id:
                commands = [row for row in commands if str(row.get("session_id") or "") != target_id]
            if record is not None:
                commands.append(record)
            data["managed_bash"] = commands
            tmp = path.with_name(f".{path.name}.{_uuid_mod.uuid4().hex}.tmp")
            try:
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            logger.debug("MCP managed Bash registration update failed", exc_info=True)


def _record_mcp_managed_bash(started: dict[str, Any]) -> None:
    session_id = str(started.get("session_id") or "")
    pid = started.get("pid")
    if not session_id or not isinstance(pid, int):
        return
    record: dict[str, Any] = {
        "session_id": session_id,
        "pid": pid,
        "explicit_background": bool(started.get("explicit_background")),
        "started_at": time.time(),
    }
    for key in ("log_file", "log_file_stderr"):
        value = started.get(key)
        if isinstance(value, str) and value:
            record[key] = value
    _mutate_mcp_managed_bash(record=record)


def _forget_mcp_managed_bash(session_id: str) -> None:
    if session_id:
        _mutate_mcp_managed_bash(remove_id=session_id)
