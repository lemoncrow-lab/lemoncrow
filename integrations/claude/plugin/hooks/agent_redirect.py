"""PreToolUse agent-redirect guard.

Claude Code's built-in ``general-purpose`` and ``Explore`` subagents are
compiled into the CLI -- no plugin/settings API removes them from the Task
tool's subagent_type list, so the model can always see and pick them. This
hook denies a Task call that targets either one and points the model at the
LemonCrow-authored equivalent instead: lemoncrow:general has the same
catch-all remit, lemoncrow:explore the same read-only search remit, both
run under this plugin's coding-guidelines discipline.

Fail-open; opt-out via LEMONCROW_AGENT_REDIRECT_GUARD=0. Quiet while dormant
(cap exhausted) -- same rule as pre_tool_discipline.py: degrade to the host
defaults, never block.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_REDIRECTS = {
    "general-purpose": "lemoncrow:general",
    "Explore": "lemoncrow:explore",
}


def _dormant() -> bool:
    try:
        from lemoncrow.core.capabilities.plugin_runtime import cap_exhausted

        root = (
            os.environ.get("LEMONCROW_ROOT")
            or os.environ.get("LEMONCROW_STORE_ROOT")
            or str(Path.home() / ".lemoncrow")
        )
        return bool(cap_exhausted(root))
    except Exception:
        return False


def _deny(reason: str) -> None:
    """Emit a current-schema PreToolUse 'deny' (Claude Code v2.1.x).

    Mirrors pre_tool_discipline.py's _deny -- the legacy top-level
    {"decision": "block"} form is deprecated for PreToolUse and silently
    ignored; denial must go through hookSpecificOutput.
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
        payload: dict[str, Any] = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, TypeError, OSError):
        return 0
    if os.environ.get("LEMONCROW_AGENT_REDIRECT_GUARD", "1") == "0":
        return 0
    if str(payload.get("tool_name") or "") != "Task":
        return 0
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return 0
    subagent_type = str(ti.get("subagent_type") or "")
    target = _REDIRECTS.get(subagent_type)
    if not target:
        return 0
    if _dormant():
        return 0
    _deny(f"Use subagent_type={target!r} instead of {subagent_type!r} -- same remit, under LemonCrow discipline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
