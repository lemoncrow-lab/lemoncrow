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

REPEAT_THRESHOLD = 3  # block on the third identical failure


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


def _session_state_path() -> Path:
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = _workspace_key(workspace)
    root = Path(os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT") or Path.home() / ".lemoncrow")
    return root / "workspaces" / h / "session_state.json"


def _read_session_state() -> dict:  # type: ignore[type-arg]
    sp = _session_state_path()
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text("utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:  # type: ignore[type-arg]
    sp = _session_state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=sp.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(state, tmp, indent=2)
            tmp_path = tmp.name
        Path(tmp_path).replace(sp)
    except OSError:
        if tmp_path:
            with contextlib.suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# RunLedger helpers (fail-open, same pattern as post_tool_use.py)
# ---------------------------------------------------------------------------


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
    state = _read_session_state()
    if state.get("lemoncrow_root"):
        return Path(state["lemoncrow_root"])
    return Path.home() / ".lemoncrow"


def _append_failure_event(session_id: str, command: str, error: str, repeat: int) -> None:
    """Append a note event for the command failure to the session's run.json."""
    try:
        from lemoncrow.core.foundation.paths import session_dir
    except ImportError:
        return
    run_file = session_dir(_lemoncrow_root(), "claude", session_id) / "run.json"
    if not run_file.exists():
        return
    try:
        data = json.loads(run_file.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
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
    except OSError:
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
    except (json.JSONDecodeError, TypeError):
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
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            _append_failure_event(session_id, command, error, failures[sig])
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    if failures[sig] >= REPEAT_THRESHOLD:
        print(
            json.dumps(
                {
                    "decision": "ask",
                    "reason": (
                        "This command failed 3 times with the same error. "
                        "Call `rescue` before any retry; do not repeat the same fix."
                    ),
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
