#!/usr/bin/env python3
"""PostToolUse hook — capture file diffs into the active RunLedger.

Fires after Edit, Write, or MultiEdit. Computes the diff and appends a
``file_edit`` event to ``runs/<session_id>.json`` so it shows up in the
Atelier traces dashboard.

Fail-open: any error exits silently (code 0) — never blocks the agent.
"""

from __future__ import annotations

import contextlib
import datetime
import difflib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# State helpers (mirrors pre_tool_use.py / stop.py)
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


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


def _git_diff(file_path: str) -> str:
    """Try git diff HEAD for a file. Returns empty string on any failure."""
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--", file_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _unified_diff(old: str, new: str, path: str) -> str:
    """Compute a unified diff between old and new content."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    return "\n".join(diff)


def _compute_diff(tool_name: str, tool_input: dict) -> tuple[str, str]:  # type: ignore[type-arg]
    """Return (file_path, diff_string). diff_string may be empty on failure."""
    file_path: str = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("filename") or ""
    if not file_path:
        return "", ""

    diff = ""

    if tool_name == "Edit":
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if old or new:
            diff = _unified_diff(old, new, file_path)
        if not diff:
            diff = _git_diff(file_path)

    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        parts: list[str] = []
        for edit in edits:
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            if old or new:
                parts.append(_unified_diff(old, new, file_path))
        diff = "\n".join(p for p in parts if p)
        if not diff:
            diff = _git_diff(file_path)

    elif tool_name == "Write":
        # For a full-file write, git diff is the most reliable source.
        diff = _git_diff(file_path)

    return file_path, diff


# ---------------------------------------------------------------------------
# RunLedger event writer
# ---------------------------------------------------------------------------


def _append_file_edit_event(session_id: str, file_path: str, diff: str) -> None:
    """Append a file_edit event to runs/<session_id>.json atomically."""
    runs_dir = _atelier_root() / "runs"
    run_file = runs_dir / f"{session_id}.json"
    if not run_file.exists():
        return

    try:
        data = json.loads(run_file.read_text("utf-8"))
    except Exception:
        return

    events: list[dict] = data.setdefault("events", [])  # type: ignore[assignment]
    short_path = Path(file_path).name
    events.append(
        {
            "kind": "file_edit",
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "summary": f"edited {short_path}",
            "payload": {
                "path": file_path,
                "diff": diff,
                "event": "PostToolUse",
            },
        }
    )
    data["events"] = events

    # Atomic write via temp file + rename
    tmp_path: str | None = None
    try:
        dir_ = run_file.parent
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=dir_,
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
    if tool_name not in ("Edit", "Write", "MultiEdit"):
        return 0

    tool_input: dict = payload.get("tool_input", {}) or {}  # type: ignore[assignment]

    try:
        file_path, diff = _compute_diff(tool_name, tool_input)
        if not file_path or not diff:
            return 0

        session_id = _active_session_id()
        if not session_id:
            return 0

        _append_file_edit_event(session_id, file_path, diff)
    except Exception:
        pass  # fail-open: never block the agent

    return 0


if __name__ == "__main__":
    sys.exit(main())
