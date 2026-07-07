#!/usr/bin/env python3
"""PostToolUse hook — capture Bash command + output into the active RunLedger.

Fires after every Bash tool call. Records the command, stdout, stderr, and
return code as a ``command_result`` event in ``runs/<session_id>.json``.

Stdout/stderr are truncated to 4 KB each to cap ledger file size.
Fail-open: any error exits silently (code 0) — never blocks the agent.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

_MAX_OUTPUT_BYTES = 4096  # 4 KB per stream


# ---------------------------------------------------------------------------
# State helpers (mirrors post_tool_use.py)
# ---------------------------------------------------------------------------


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
    root = Path(os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or Path.home() / ".atelier")
    return root / "workspaces" / h / "session_state.json"


def _read_session_state() -> dict:  # type: ignore[type-arg]
    p = _session_state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError):
        return {}


def _atelier_root() -> Path:
    root = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
    if root:
        return Path(root)
    state = _read_session_state()
    if state.get("atelier_root"):
        return Path(state["atelier_root"])
    return Path.home() / ".atelier"


def _cache_bash_invocation(
    command: str,
    stdout: str,
    stderr: str,
    return_code: int | None,
) -> None:
    """Record Bash output in the shared tool-supervision cache."""
    if os.environ.get("ATELIER_CACHE_DISABLED") == "1":
        return
    try:
        from atelier.core.capabilities.tool_supervision import ToolSupervisionCapability

        cap = ToolSupervisionCapability(_atelier_root())
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
    """Append a command_result event to the session's run.json atomically."""
    try:
        from atelier.core.foundation.paths import session_dir
    except ImportError:
        return
    run_file = session_dir(_atelier_root(), "claude", session_id) / "run.json"
    if not run_file.exists():
        return

    try:
        data = json.loads(run_file.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    events: list[dict[str, Any]] = data.setdefault("events", [])

    # Build a short summary line
    short_cmd = command.strip()[:80] + ("…" if len(command.strip()) > 80 else "")
    ok = return_code == 0 if return_code is not None else True
    summary = f"{'✓' if ok else '✗'} {short_cmd}"

    events.append(
        {
            "kind": "command_result",
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "summary": summary,
            "payload": {
                "command": command,
                "stdout": stdout[:_MAX_OUTPUT_BYTES] if stdout else "",
                "stderr": stderr[:_MAX_OUTPUT_BYTES] if stderr else "",
                "return_code": return_code,
                "truncated": len(stdout or "") > _MAX_OUTPUT_BYTES or len(stderr or "") > _MAX_OUTPUT_BYTES,
            },
        }
    )
    data["events"] = events

    # Atomic write via temp file + rename
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
    except (OSError, json.JSONDecodeError):
        if tmp_path:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)


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
