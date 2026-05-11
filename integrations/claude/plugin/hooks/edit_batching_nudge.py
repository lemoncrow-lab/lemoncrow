#!/usr/bin/env python3
"""PostToolUse hook that nudges repeated single edits into batches."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _session_state_path() -> Path:
    import hashlib

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]
    root = Path(os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or Path.home() / ".atelier")
    return root / "workspaces" / h / "session_state.json"


def _read_session_state() -> dict[str, object]:
    p = _session_state_path()
    if not p.exists():
        return {}
    try:
        import json

        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {}


def _atelier_root() -> Path:
    root = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
    if root:
        return Path(root)
    state = _read_session_state()
    if state.get("atelier_root"):
        return Path(state["atelier_root"])
    return Path.home() / ".atelier"


def _state_path(payload: dict[str, object]) -> Path:
    session_id = str(payload.get("session_id") or "default")
    safe_session_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in session_id)
    return _atelier_root() / "hook_state" / f"edit-nudge-{safe_session_id}.json"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        from atelier.core.capabilities.plugin_runtime import edit_nudge

        state_path = _state_path(payload)
        try:
            state_before = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        except Exception:
            state_before = {}
        result = edit_nudge(state_before=state_before, payload=payload)
        if result.get("state_after") is not None:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(result["state_after"], indent=2), encoding="utf-8")
        stdout = result.get("stdout")
        if stdout:
            print(json.dumps(stdout))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
