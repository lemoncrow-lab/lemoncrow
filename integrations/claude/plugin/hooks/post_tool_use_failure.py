#!/usr/bin/env python3
"""PostToolUseFailure hook for Bash.

Tracks command failures keyed by (command, error_signature). On the second
identical failure, returns a decision that tells Claude to call
`rescue` before retrying.

Opt-in via hooks.json.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

REPEAT_THRESHOLD = 2  # block on the second identical failure


def _session_state_path() -> Path:
    import hashlib

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]
    root = Path(os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or Path.home() / ".atelier")
    return root / "workspaces" / h / "session_state.json"


def _read_session_state() -> dict:  # type: ignore[type-arg]
    sp = _session_state_path()
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text("utf-8"))  # type: ignore[no-any-return]
    except Exception:
        return {}


def _save_state(state: dict) -> None:  # type: ignore[type-arg]
    sp = _session_state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# RunLedger helpers (fail-open, same pattern as post_tool_use.py)
# ---------------------------------------------------------------------------


def _atelier_root() -> Path:
    root = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
    if root:
        return Path(root)
    state = _read_session_state()
    if state.get("atelier_root"):
        return Path(state["atelier_root"])
    return Path.home() / ".atelier"


def _active_session_id() -> str | None:
    state = _read_session_state()
    return state.get("session_id") or state.get("active_session_id")


def _append_failure_event(session_id: str, command: str, error: str, repeat: int) -> None:
    """Append a note event for the command failure to runs/<session_id>.json."""
    runs_dir = _atelier_root() / "runs"
    run_file = runs_dir / f"{session_id}.json"
    if not run_file.exists():
        return
    try:
        data = json.loads(run_file.read_text("utf-8"))
    except Exception:
        return

    events: list[dict[str, Any]] = data.setdefault("events", [])
    short_cmd = command.strip()[:80] + ("…" if len(command.strip()) > 80 else "")
    events.append(
        {
            "kind": "note",
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "summary": f"bash failure (*{repeat}): {short_cmd}",
            "payload": {
                "command": command,
                "error": error[:2000],
                "repeat_count": repeat,
                "event": "PostToolUseFailure",
            },
        }
    )
    data["events"] = events

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=run_file.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        Path(tmp_path).replace(run_file)
    except Exception:
        if tmp_path:
            with contextlib.suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)


def _signature(command: str, error: str) -> str:
    # collapse paths, line numbers, hex, hashes
    norm = re.sub(r"0x[0-9a-fA-F]+", "0xX", error)
    norm = re.sub(r"\b\d+\b", "N", norm)
    norm = re.sub(r"/[^\s:]+", "<path>", norm)
    key = f"{command.strip()[:80]}::{norm.strip()[:200]}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    tool_response = payload.get("tool_response", {}) or {}
    command = tool_input.get("command", "")
    error = (tool_response.get("stderr") or tool_response.get("error") or "")[:1000]
    if not command:
        return 0

    sig = _signature(command, error)
    state = _read_session_state()
    failures = state.setdefault("failures", {})
    failures[sig] = failures.get(sig, 0) + 1
    state["failures"] = failures
    _save_state(state)

    # Always write the failure to the RunLedger (fail-open)
    try:
        session_id = _active_session_id()
        if session_id:
            _append_failure_event(session_id, command, error, failures[sig])
    except Exception:
        pass

    if failures[sig] >= REPEAT_THRESHOLD:
        print(
            json.dumps(
                {
                    "decision": "ask",
                    "reason": (
                        "Atelier: this command has now failed twice with the "
                        "same error signature. Call `rescue` "
                        "with the task, error, files, and recent_actions "
                        "before running it again; do not retry the same fix a third time."
                    ),
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
