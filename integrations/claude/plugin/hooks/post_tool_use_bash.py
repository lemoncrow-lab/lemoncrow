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

_MAX_OUTPUT_BYTES = 4096  # 4 KB per stream


# ---------------------------------------------------------------------------
# State helpers (mirrors post_tool_use.py)
# ---------------------------------------------------------------------------


def _session_state_path() -> Path:
    import hashlib

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]
    root = Path(os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or Path.home() / ".atelier")
    return root / "workspaces" / h / "session_state.json"


def _read_session_state() -> dict:  # type: ignore[type-arg]
    p = _session_state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))  # type: ignore[no-any-return]
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


def _active_session_id() -> str | None:
    return _read_session_state().get("active_session_id")


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
            tool_name="Bash",
        )
    except Exception:
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
    """Append a command_result event to runs/<session_id>.json atomically."""
    runs_dir = _atelier_root() / "runs"
    run_file = runs_dir / f"{session_id}.json"
    if not run_file.exists():
        return

    try:
        data = json.loads(run_file.read_text("utf-8"))
    except Exception:
        return

    events: list[dict] = data.setdefault("events", [])  # type: ignore[assignment]

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
    except Exception:
        if tmp_path:
            with contextlib.suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0  # fail-open

    tool_name: str = payload.get("tool_name", "") or ""
    if tool_name != "Bash":
        return 0

    tool_input: dict = payload.get("tool_input", {}) or {}  # type: ignore[assignment]
    tool_response: dict = payload.get("tool_response", {}) or {}  # type: ignore[assignment]

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
        session_id = _active_session_id()
        if not session_id:
            _cache_bash_invocation(command, stdout, stderr, return_code)
            return 0
        _append_command_result_event(session_id, command, stdout, stderr, return_code)
        _cache_bash_invocation(command, stdout, stderr, return_code)
    except Exception:
        pass  # fail-open: never block the agent

    return 0


if __name__ == "__main__":
    sys.exit(main())
