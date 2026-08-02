#!/usr/bin/env python3
"""Cursor preToolUse hook: route natives through LemonCrow MCP tools.

Unscoped (no matcher): fires for every tool, including Glob — Cursor's
Claude-compat matcher map sets Glob:null, so a matcher never covers it.

Modes via LEMONCROW_CURSOR_ENFORCE:
  unset/0  — allow all (default; inert for interactive IDE)
  1/true   — soft: deny natives with cooloff (legacy nudge)
  hard     — deny every non-LemonCrow-MCP tool, no cooloff (bench)

Allowed under hard: MCP tools whose bare name is one of LemonCrow's
code tools (code_search, read, edit, bash, web_fetch), including
server-qualified forms like MCP:lemoncrow:code_search.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

IMMEDIATE_RETRY_SECONDS = 10
COOLOFF_SECONDS = int(os.environ.get("LEMONCROW_CURSOR_TOOL_COOLOFF_SECONDS", "300"))

_ALLOWED_MCP_TOOLS = frozenset({"code_search", "read", "edit", "bash", "web_fetch"})
# Cursor meta MCP callers: deny under hard — schemas are already injected once
# `cursor-agent mcp enable lemoncrow` runs; GetMcpTools/CallMcpTool burn ~9k
# chars and confuse the model into invalid CallMcpTool loops.
_ALLOWED_META_TOOLS = frozenset()

_REPLACEMENT = {
    "Grep": "code_search",
    "Glob": "code_search",
    "Write": "edit",
    "StrReplace": "edit",
    "Delete": "bash",
    "Shell": "bash",
    "Read": "read",
    "Task": "bash",
    "WebFetch": "web_fetch",
    "WebSearch": "web_fetch",
    "GetMcpTools": "lemoncrow-code_search",
    "CallMcpTool": "lemoncrow-code_search",
}


def _lemoncrow_root() -> Path:
    return Path(
        os.environ.get("LEMONCROW_ROOT", "") or os.environ.get("LEMONCROW_STORE_ROOT", "") or Path.home() / ".lemoncrow"
    )


def _state_path() -> Path:
    return _lemoncrow_root() / "cursor-hooks" / "pretooluse_deny_state.json"


def _enforce_mode() -> str:
    """Return '', 'soft', or 'hard'."""
    raw = os.environ.get("LEMONCROW_CURSOR_ENFORCE", "0").strip().lower()
    if raw in {"hard", "strict", "bench"}:
        return "hard"
    if raw in {"1", "true", "yes", "on", "soft"}:
        return "soft"
    return ""


def _mcp_bare_name(tool_name: str) -> str | None:
    """Extract bare MCP tool name, or None if not an MCP tool."""
    name = tool_name.strip()
    if not name:
        return None
    lower = name.lower()
    # Cursor docs: MCP:<tool_name>. Also seen: MCP:server:tool, mcp__server__tool.
    if lower.startswith("mcp:"):
        parts = name.split(":")
        return parts[-1].strip().lower() or None
    if lower.startswith("mcp__"):
        parts = name.split("__")
        return parts[-1].strip().lower() or None
    return None


def _is_allowed_mcp(tool_name: str) -> bool:
    bare = _mcp_bare_name(tool_name)
    return bare is not None and bare in _ALLOWED_MCP_TOOLS


def _decide_soft() -> str:
    """Soft mode cooloff: deny then allow retries so the agent is not stuck."""
    now = time.time()
    path = _state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}

    last_at = state.get("last_event_at")
    last_action = state.get("last_action")
    action = "deny"
    if isinstance(last_at, (int, float)):
        age = now - last_at
        if last_action == "deny" and age < IMMEDIATE_RETRY_SECONDS:
            action = "allow"
        elif last_action == "allow" and age < COOLOFF_SECONDS:
            action = "allow"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_event_at": now, "last_action": action}), encoding="utf-8")
    except OSError:
        pass
    return action


def _deny(tool_name: str) -> dict:
    replacement = _REPLACEMENT.get(tool_name, "code_search")
    return {
        "permission": "deny",
        "user_message": (
            f"Native {tool_name or 'tool'} blocked — LemonCrow routes this through its own " f"'{replacement}' tool."
        ),
        "agent_message": (
            f"Native '{tool_name}' is blocked. Use the '{replacement}' tool from the lemoncrow MCP "
            "server (MCP:code_search / MCP:read / MCP:edit / MCP:bash). Do not retry the native tool."
        ),
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    tool_name = str(payload.get("tool_name") or "")
    mode = _enforce_mode()

    if not mode:
        sys.stdout.write(json.dumps({"permission": "allow"}) + "\n")
        return 0

    if _is_allowed_mcp(tool_name):
        sys.stdout.write(json.dumps({"permission": "allow"}) + "\n")
        return 0

    if tool_name.strip().lower() in _ALLOWED_META_TOOLS:
        sys.stdout.write(json.dumps({"permission": "allow"}) + "\n")
        return 0

    if mode == "hard":
        # Stick deny — no cooloff. MCP tools already allowed above.
        decision = _deny(tool_name)
        with contextlib.suppress(OSError):
            log = Path("/tmp/lc-cursor-enforce.log")
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"mode": mode, "tool": tool_name, "decision": "deny"}) + "\n")
        sys.stdout.write(json.dumps(decision) + "\n")
        return 0

    # Soft: cooloff may allow natives through (legacy IDE nudge).
    if _decide_soft() == "allow":
        sys.stdout.write(json.dumps({"permission": "allow"}) + "\n")
        return 0

    sys.stdout.write(json.dumps(_deny(tool_name)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
