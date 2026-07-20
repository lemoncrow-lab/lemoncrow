#!/usr/bin/env python3
"""Cursor sessionStart hook: bridge the live session id to LemonCrow.

Cursor never sets a session env var for MCP server subprocesses, so without
this bridge every LemonCrow MCP tool call's savings row is diverted to the
unattributed quarantine ledger and the session always shows Saved $0 even
though Cost displays correctly. This hook writes the live session id into
``<workspace>/.lemoncrow/workspace/session_state.json``, which
``mcp_server._resolved_host_session()`` reads as its fallback.

Payload (cursor.com/docs/hooks, sessionStart): base fields include
``conversation_id`` and ``workspace_roots``; sessionStart adds ``session_id``
(documented as \"same as conversation_id\" -- the composer id, which is also
the session id LemonCrow's Cursor importer keys Traces on).

Self-contained on purpose: hooks run under Cursor's environment with no
PYTHONPATH guarantee, so no lemoncrow import.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    session_id = str(payload.get("session_id") or payload.get("conversation_id") or "").strip()
    roots = payload.get("workspace_roots")
    workspace = ""
    if isinstance(roots, list) and roots:
        workspace = str(roots[0] or "").strip()
    workspace = workspace or os.getcwd()

    if session_id:
        state_path = Path(workspace).expanduser().resolve() / ".lemoncrow" / "workspace" / "session_state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if not isinstance(state, dict):
                state = {}
        except (OSError, json.JSONDecodeError):
            state = {}
        if state.get("session_id") != session_id or state.get("host") != "cursor":
            state["session_id"] = session_id
            # Host stamp: the MCP bridge fallback only trusts this shared slot
            # when the stamp matches the reading server's host.
            state["host"] = "cursor"
            try:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            except OSError:
                pass

    # sessionStart is fire-and-forget; emit an empty object for cleanliness.
    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
