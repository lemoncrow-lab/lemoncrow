"""Canonical MCP host/session identity and model resolution.

This leaf module owns workspace-bridge fallback and host-session resolution so
handlers can depend on session identity without importing the legacy
``mcp_server`` composition module.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from lemoncrow.core.foundation.paths import resolve_workspace_store_dir
from lemoncrow.gateway.adapters.mcp import ledger
from lemoncrow.gateway.adapters.mcp.session_state import _mcp_session_file

_log = logging.getLogger("lemoncrow.mcp")

HOST_SESSION_ENVS: tuple[tuple[str, str], ...] = (
    ("CODEX_SESSION_ID", "codex"),
    ("OPENCODE_SESSION_ID", "opencode"),
    ("GITHUB_COPILOT_SESSION_ID", "copilot"),
    ("CURSOR_SESSION_ID", "cursor"),
    ("CURSOR_TRACE_ID", "cursor"),
    ("HERMES_SESSION_ID", "hermes"),
    ("ANTIGRAVITY_SESSION_ID", "antigravity"),
    ("AGY_SESSION_ID", "antigravity"),
)


def workspace_bridge_file() -> Path:
    """Workspace-shared identity relay containing session id, host and model."""
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd()
    return resolve_workspace_store_dir(workspace_root=Path(workspace)) / "session_state.json"


def read_workspace_session_bridge() -> tuple[str, str]:
    """Return ``(session_id, model)`` from the workspace identity relay."""
    try:
        path = workspace_bridge_file()
        if not path.is_file():
            return "", ""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return "", ""
        return str(data.get("session_id") or "").strip(), str(data.get("model") or "").strip()
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return "", ""


def workspace_bridge_session_id() -> str:
    """Resolve a non-Claude host session id from the workspace bridge."""
    try:
        host = ledger._detect_agent()
        if not host or host == "claude":
            return ""
        path = workspace_bridge_file()
        if not path.is_file():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ""
        if str(data.get("host") or "").strip() != host:
            return ""
        return str(data.get("session_id") or "").strip()
    except Exception:
        return ""


def resolved_host_session() -> tuple[str, str]:
    """Resolved ``(session_id, host)`` for the current host, or empty strings."""
    req_sid, req_host = ledger._request_session_identity()
    if req_sid:
        return req_sid, req_host or ledger._detect_agent()
    sid = ledger._resolve_live_session_id()
    if sid:
        return sid, "claude"
    claude_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if claude_sid:
        return claude_sid, "claude"
    for env_var, host in HOST_SESSION_ENVS:
        env_sid = os.environ.get(env_var, "").strip()
        if env_sid:
            return env_sid, host
    bridge_sid = workspace_bridge_session_id()
    if bridge_sid:
        return bridge_sid, ledger._detect_agent()
    return "", ""


def resolved_host_session_id() -> str:
    """Resolved per-session id for the current host, or ``""`` when unknown."""
    return resolved_host_session()[0]


def get_mcp_model() -> str:
    """Return the request-scoped or last host-reported model, or empty string."""
    request_model = ledger._request_session_model()
    if request_model:
        return request_model
    if not ledger._cached_claude_session_id:
        ledger._get_claude_session_id()

    sid, model = read_workspace_session_bridge()
    if sid and model and sid == resolved_host_session_id():
        ledger._cached_mcp_model = model
        return ledger._cached_mcp_model

    try:
        path = _mcp_session_file()
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                ledger._cached_mcp_model = str(data.get("model") or "").strip()
    except (OSError, json.JSONDecodeError):
        _log.debug("MCP model read failed", exc_info=True)
    return ledger._cached_mcp_model


__all__ = [
    "HOST_SESSION_ENVS",
    "get_mcp_model",
    "read_workspace_session_bridge",
    "resolved_host_session",
    "resolved_host_session_id",
    "workspace_bridge_file",
    "workspace_bridge_session_id",
]
