#!/usr/bin/env python3
"""agentStart hook for GitHub Copilot CLI.

Clears the workspace-scoped savings side log so each session starts fresh.
Payload: {sessionId, transcriptPath, timestamp, cwd}
"""

import json
import os
import sys
from pathlib import Path


def _lemoncrow_root() -> Path:
    return Path(os.environ.get("LEMONCROW_ROOT", "") or Path.home() / ".lemoncrow")


def _workspace_key(path: str) -> str:
    import re
    from hashlib import sha256
    from pathlib import Path as _Path

    resolved = _Path(path).expanduser().resolve()
    home = _Path.home().resolve()
    try:
        parts = resolved.relative_to(home).parts
    except ValueError:
        parts = [p for p in resolved.parts if p and p != "/"]
    sanitized = [re.sub(r"[^a-zA-Z0-9.\-_]", "-", p) for p in parts if p]
    label = re.sub(r"-{2,}", "-", "-".join(sanitized)).strip("-")
    if len(label) > 120:
        label = label[:110].rstrip("-") + "--" + sha256(str(resolved).encode()).hexdigest()[:6]
    return label or sha256(str(resolved).encode()).hexdigest()[:12]


def _session_savings_path(workspace: str) -> Path:
    """Resolve the per-session savings path, mirroring the MCP writer.

    Delegates host segregation to the canonical `session_dir()` helper
    (`lemoncrow.core.foundation.paths`) instead of re-deriving it here. The old
    fallback to CLAUDE_CODE_SESSION_ID "for parity" was itself a real
    cross-host collision bug: a copilot-cli session and a Claude Code session
    that happened to share an id (or a stale CLAUDE_CODE_SESSION_ID left over
    in the environment) would silently corrupt each other's savings.jsonl.
    Only GITHUB_COPILOT_SESSION_ID identifies a copilot-cli session. The host
    is hardcoded to "copilot" (not `detect_host()`) since this file is only
    ever invoked by copilot-cli.

    1. If GITHUB_COPILOT_SESSION_ID is set ->
       session_dir(root, "copilot", sid) / "savings.jsonl".
    2. Else workspaces/<_workspace_key(LEMONCROW_WORKSPACE_ROOT or cwd)>/
       session_savings.jsonl (human-readable key, matching paths.workspace_key).
    """
    sid = os.environ.get("GITHUB_COPILOT_SESSION_ID", "").strip()
    if sid:
        try:
            from lemoncrow.core.foundation.paths import session_dir
        except ImportError:
            pass
        else:
            return session_dir(_lemoncrow_root(), "copilot", sid) / "savings.jsonl"
    workspace = str(Path(os.environ.get("LEMONCROW_WORKSPACE_ROOT") or workspace).resolve())
    h = _workspace_key(workspace)
    return _lemoncrow_root() / "workspaces" / h / "session_savings.jsonl"


def _write_session_state_bridge(workspace: str, session_id: str) -> None:
    """Refresh workspaces/<hash>/session_state.json with the live session id.

    Copilot CLI does not set GITHUB_COPILOT_SESSION_ID for the MCP server
    subprocess, so without this bridge every MCP tool call's savings row is
    diverted to the unattributed quarantine ledger and the session recap
    always shows $0 saved (mcp_server._resolved_host_session falls back to
    _workspace_bridge_session_id, which reads this file). Mirrors the codex
    hooks' _write_codex_session_state — same file, same workspace-hash scheme.
    Local implementation (no lemoncrow import): this hook has no PYTHONPATH
    guarantee, and _workspace_key above already matches paths.workspace_key.
    """
    if not session_id:
        return
    state_path = _lemoncrow_root() / "workspaces" / _workspace_key(workspace) / "session_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("session_id") == session_id and state.get("host") == "copilot":
        return
    state["session_id"] = session_id
    # Host stamp: the MCP bridge fallback only trusts this shared slot when the
    # stamp matches the reading server's host.
    state["host"] = "copilot"
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    workspace = (
        payload.get("cwd")
        or os.environ.get("COPILOT_PROJECT_DIR")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )

    session_id = str(payload.get("sessionId") or os.environ.get("GITHUB_COPILOT_SESSION_ID", "") or "").strip()
    _write_session_state_bridge(workspace, session_id)

    path = _session_savings_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    except OSError:
        pass


if __name__ == "__main__":
    main()
