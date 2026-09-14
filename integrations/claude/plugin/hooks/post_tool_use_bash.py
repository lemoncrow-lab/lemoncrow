#!/usr/bin/env python3
"""PostToolUse hook — capture Bash command + output into the active RunLedger.

Fires after every Bash tool call. Records the command, stdout, stderr, and
return code as a ``command_result`` event in ``runs/<session_id>.json``.

Stdout/stderr are truncated to 4 KB each to cap ledger file size.
Fail-open: any error exits silently (code 0) — never blocks the agent.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any

_MAX_OUTPUT_BYTES = 4096  # 4 KB per stream


# ---------------------------------------------------------------------------
# State helpers (mirrors post_tool_use.py)
# ---------------------------------------------------------------------------


def _session_state_path() -> Path:
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    return Path(workspace).expanduser().resolve() / ".lemoncrow" / "workspace" / "session_state.json"


def _read_session_state() -> dict:  # type: ignore[type-arg]
    p = _session_state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError):
        return {}


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
    state = _read_session_state()
    if state.get("lemoncrow_root"):
        return Path(state["lemoncrow_root"])
    return Path.home() / ".lemoncrow"


def _cache_bash_invocation(
    command: str,
    stdout: str,
    stderr: str,
    return_code: int | None,
) -> None:
    """Record Bash output in the shared tool-supervision cache."""
    if os.environ.get("LEMONCROW_CACHE_DISABLED") == "1":
        return
    try:
        from lemoncrow.pro.capabilities.tool_supervision import ToolSupervisionCapability

        cap = ToolSupervisionCapability(_lemoncrow_root())
        key = f"Bash:{json.dumps({'command': command}, sort_keys=True)[:100]}"
        cap.observe(
            key,
            {
                "command": command,
                "stdout": stdout[:_MAX_OUTPUT_BYTES] if stdout else "",
                "stderr": stderr[:_MAX_OUTPUT_BYTES] if stderr else "",
                "return_code": return_code,
            },
            cache_hit=False,
        )
    except (OSError, ImportError, ValueError, AttributeError, TypeError):
        pass


# ---------------------------------------------------------------------------
# RunLedger event writer
# ---------------------------------------------------------------------------


def _append_command_result_event(
    session_id: str,
    command: str,
    stdout: str,
    stderr: str,
    return_code: int | None,
) -> None:
    """Append a command_result event without losing concurrent writers."""
    try:
        from lemoncrow.core.foundation.paths import session_dir
        from lemoncrow.core.foundation.run_file_io import RunFileLock, atomic_write_json
    except ImportError:
        return
    run_file = session_dir(_lemoncrow_root(), "claude", session_id) / "run.json"

    try:
        with RunFileLock(run_file):
            if not run_file.exists():
                return
            data = json.loads(run_file.read_text("utf-8"))
            if not isinstance(data, dict):
                return
            events = data.setdefault("events", [])
            if not isinstance(events, list):
                return

            short_cmd = command.strip()[:80] + ("…" if len(command.strip()) > 80 else "")
            ok = return_code == 0 if return_code is not None else True
            events.append(
                {
                    "kind": "command_result",
                    "at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "summary": f"{'✓' if ok else '✗'} {short_cmd}",
                    "payload": {
                        "event": "PostToolUseBash",
                        "command": command,
                        "stdout": stdout[:_MAX_OUTPUT_BYTES] if stdout else "",
                        "stderr": stderr[:_MAX_OUTPUT_BYTES] if stderr else "",
                        "return_code": return_code,
                        "truncated": len(stdout or "") > _MAX_OUTPUT_BYTES or len(stderr or "") > _MAX_OUTPUT_BYTES,
                    },
                }
            )
            atomic_write_json(run_file, data)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return 0  # fail-open

    tool_name: str = payload.get("tool_name", "") or ""
    if tool_name != "Bash":
        return 0

    tool_input: dict[str, Any] = payload.get("tool_input", {}) or {}
    tool_response: dict[str, Any] = payload.get("tool_response", {}) or {}

    command: str = tool_input.get("command", "") or ""
    if not command:
        return 0

    stdout: str = tool_response.get("stdout", "") or ""
    stderr: str = tool_response.get("stderr", "") or ""
    # Claude Code may return exit code in different fields; use explicit None
    # check so that 0 (success) is not treated as falsy and discarded.
    _rc = tool_response.get("returnCode")
    if _rc is None:
        _rc = tool_response.get("return_code")
    if _rc is None:
        _rc = tool_response.get("exitCode")
    return_code: int | None = int(_rc) if _rc is not None else None

    try:
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            _cache_bash_invocation(command, stdout, stderr, return_code)
            return 0
        _append_command_result_event(session_id, command, stdout, stderr, return_code)
        _cache_bash_invocation(command, stdout, stderr, return_code)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass  # fail-open: never block the agent

    return 0


if __name__ == "__main__":
    sys.exit(main())
