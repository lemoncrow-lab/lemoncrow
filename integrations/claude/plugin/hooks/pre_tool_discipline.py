#!/usr/bin/env python3
"""PreToolUse read-after-edit guard.

Blocks the one wasteful case: a whole-file re-read (``full=true`` with no range) of a
file already edited this session. The edit response already returned the changed
region, and a full re-read re-injects the whole file -- which is then re-cached
on every later turn. Targeted range reads and reads of un-edited files pass
through untouched.

Edited files are recorded by loop_discipline_post.py (shared session state).
Fail-open; opt-out via ATELIER_READ_AFTER_EDIT_GUARD=0.

Note: this hook deliberately does NOT block grep/rg over source. Steering toward
explore/search lives in the agent instructions + the strength of the indexed
tools, not a hard PreToolUse deny (which mis-fired on legitimate searches).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def _root() -> Path:
    raw = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
    return Path(raw) if raw else Path.home() / ".atelier"


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


def _edited_paths() -> set[str]:
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = _workspace_key(workspace)
    sp = _root() / "workspaces" / h / "loop_discipline.json"
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(sp.read_text("utf-8"))
        if isinstance(data, dict):
            return {str(p) for p in (data.get("edited_paths") or [])}
    return set()


def _is_read(name: str, ti: dict[str, Any]) -> bool:
    if name.endswith("__read") or name == "read":
        return True
    return "path" in ti and "edits" not in ti and "command" not in ti


def _deny(reason: str) -> None:
    """Emit a current-schema PreToolUse 'deny' (Claude Code v2.1.x).

    The legacy top-level {"decision": "block"} form is deprecated for PreToolUse
    and is silently ignored -- denial must go through hookSpecificOutput so the
    tool call is actually blocked and the reason is shown to the agent.
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, TypeError, OSError):
        return 0
    name = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        return 0

    # Read-after-edit guard.
    if os.environ.get("ATELIER_READ_AFTER_EDIT_GUARD", "1") == "0":
        return 0
    if not _is_read(name, ti):
        return 0
    raw_path = str(ti.get("path") or "")
    has_range = bool(ti.get("range")) or "#" in raw_path
    if not bool(ti.get("full")) or has_range:
        return 0
    base = Path(raw_path.split("#")[0]).name
    if not base or base not in _edited_paths():
        return 0
    reason = (
        f'Edited {base} already -- read a range (range="L1-L120"), not the whole file; :full re-caches it every turn.'
    )
    _deny(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
