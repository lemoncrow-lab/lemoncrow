#!/usr/bin/env python3
"""Inject Copilot CLI recovery guidance after a repeated identical tool failure."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_GUIDANCE = "This command failed twice with the same error. Call 'rescue' before any retry; do not repeat the same fix."


def _atelier_root() -> Path:
    return Path(os.environ.get("ATELIER_ROOT", "") or Path.home() / ".atelier")


def _state_path(payload: dict[str, Any]) -> Path:
    """Per-session failure-signature counts.

    Folded into the canonical `session_dir()` layout (`copilot` host,
    hardcoded since this hook is only ever invoked by copilot-cli) instead of
    the old bespoke `copilot-cli/failure-state/<sha256(session_id)>.json`
    scheme, which was the only place in the codebase that already namespaced
    by host -- just via its own incompatible convention. session_id is a
    host-issued high-entropy id, so using it directly as the directory name
    (rather than hashing it) is safe and consistent with every other writer.
    """
    session_id = str(payload.get("sessionId") or payload.get("session_id") or "default")
    try:
        from atelier.core.foundation.paths import session_dir
    except ImportError:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return _atelier_root() / "copilot-cli" / "failure-state" / f"{digest}.json"
    return session_dir(_atelier_root(), "copilot", session_id) / "failure_state.json"


def _signature(payload: dict[str, Any]) -> str:
    tool = str(payload.get("toolName") or payload.get("tool_name") or "")
    args = payload.get("toolArgs", payload.get("tool_input"))
    error = str(payload.get("error") or "").strip()
    raw = json.dumps({"tool": tool, "args": args, "error": error}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        path = _state_path(payload)
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        signature = _signature(payload)
        count = int(state.get(signature, 0) or 0) + 1
        state[signature] = count
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        if count >= 2:
            sys.stdout.write(_GUIDANCE + "\n")
            return 2
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
