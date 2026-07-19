#!/usr/bin/env python3
"""PostToolUse hook — capture file diffs into the active RunLedger.

Fires after Edit, Write, or MultiEdit. Computes the diff and appends a
``file_edit`` event to the session's ``run.json`` (see
``lemoncrow.core.foundation.paths.session_dir``) so it shows up in the LemonCrow
traces dashboard.

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
from typing import Any

# ---------------------------------------------------------------------------
# State helpers (mirrors pre_tool_use.py / stop.py)
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
    root = Path(
        os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT") or Path.home() / ".lemoncrow"
    )
    return root / "workspaces" / h / "session_state.json"


def _read_session_state() -> dict:  # type: ignore[type-arg]
    p = _session_state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return {}


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
    state = _read_session_state()
    if state.get("lemoncrow_root"):
        return Path(state["lemoncrow_root"])
    return Path.home() / ".lemoncrow"


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
    except (subprocess.SubprocessError, OSError):
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
        old = tool_input.get("old") or tool_input.get("old_string", "")
        new = tool_input.get("new") or tool_input.get("new_string", "")
        if old or new:
            diff = _unified_diff(old, new, file_path)
        if not diff:
            diff = _git_diff(file_path)

    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        parts: list[str] = []
        for edit in edits:
            old = edit.get("old") or edit.get("old_string", "")
            new = edit.get("new") or edit.get("new_string", "")
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


# Cap the stored diff: run.json is rewritten in full on every edit (O(n^2) over a
# session), so an unbounded diff from a large Write/refactor would bloat it badly.
_MAX_DIFF_CHARS = 4000


def _append_file_edit_event(session_id: str, file_path: str, diff: str) -> None:
    """Append a file_edit event to the session's run.json atomically."""
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
    short_path = Path(file_path).name
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + f"\n...[diff truncated, {len(diff)} chars total]"
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
    except (OSError, TypeError, ValueError):
        if tmp_path:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _jj_snapshot_nudge() -> None:
    """jj lazily snapshots the working copy on the *next* jj invocation, not on
    the file write itself -- so an edit sits unprotected in jj's history until
    something else happens to call jj. Fire a cheap, silent `jj status` right
    after each edit to close that gap. Fail-open: no .jj dir, no jj binary, or
    any error -> silently skipped, never blocks the agent."""
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    if not (Path(workspace) / ".jj").exists():
        return
    try:
        subprocess.run(
            ["jj", "status"],
            cwd=workspace,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return 0
    tool_name: str = payload.get("tool_name", "") or ""
    if tool_name not in ("Edit", "Write", "MultiEdit"):
        return 0
    _jj_snapshot_nudge()
    tool_input: dict[str, Any] = payload.get("tool_input", {}) or {}
    try:
        file_path, diff = _compute_diff(tool_name, tool_input)
        if not file_path or not diff:
            return 0

        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return 0

        _append_file_edit_event(session_id, file_path, diff)
    except (OSError, TypeError, ValueError):
        pass  # fail-open: never block the agent

    return 0


if __name__ == "__main__":
    sys.exit(main())
