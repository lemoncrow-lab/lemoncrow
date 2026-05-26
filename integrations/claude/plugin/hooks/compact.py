#!/usr/bin/env python3
"""PreCompact / PostCompact hook — manage compact manifest for context preservation.

A single script handles both events — the ``hook_event_name`` field in the
payload determines which path runs.

PreCompact:
    1. Creates a placeholder manifest file for compact op=advise to populate
  2. Writes a note event to the ledger indicating pre-compact
  3. Does NOT block (exit 0 always).

PostCompact:
  1. Reads the manifest (if it exists)
  2. Records that compaction completed with preservation details
  3. Writes a note event to the ledger

The compact MCP tool with op=advise populates the manifest on PreCompact.

Fail-open: any error exits silently (code 0) — never blocks the agent.

Payload shapes:
  PreCompact:  { session_id, transcript_path, cwd, hook_event_name: "PreCompact" }
  PostCompact: { session_id, transcript_path, cwd, hook_event_name: "PostCompact" }
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

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _session_state_path() -> Path:
    import hashlib

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]
    root = Path(os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or Path.home() / ".atelier")
    return root / "workspaces" / h / "session_state.json"


def _read_session_state() -> dict[str, Any]:
    p = _session_state_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
        if isinstance(data, dict):
            return data
        return {}
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
    state = _read_session_state()
    return state.get("session_id") or state.get("active_session_id")


# ---------------------------------------------------------------------------
# Compact manifest management
# ---------------------------------------------------------------------------


def _ensure_compact_manifest(session_id: str) -> Path:
    """Ensure manifest file exists. Return the path."""
    atelier_root = _atelier_root()
    run_dir = atelier_root / "runs" / session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "compact_manifest.json"

    if not manifest_path.exists():
        # Create an empty manifest; compact op=advise will populate it
        initial = {
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "session_id": session_id,
            "trigger": "pre_compact_hook",
            "should_compact": False,
            "should_advise": False,
            "should_auto_compact": False,
            "should_handover": False,
            "utilisation_pct": 0.0,
            "turn_count": 0,
            "task_boundary_detected": False,
            "preserve_blocks": [],
            "pin_memory": [],
            "open_files": [],
            "recent_turns": [],
            "claude_md_hash": None,
            "active_errors": [],
            "handover_file": None,
            "suggested_prompt": "Compact this conversation.",
        }
        with contextlib.suppress(Exception):
            manifest_path.write_text(json.dumps(initial, indent=2), encoding="utf-8")

    return manifest_path


def _read_compact_manifest(session_id: str) -> dict[str, Any] | None:
    """Read compact_manifest.json from the run directory."""
    try:
        atelier_root = _atelier_root()
        manifest_path = atelier_root / "runs" / session_id / "compact_manifest.json"
        if manifest_path.exists():
            data = json.loads(manifest_path.read_text("utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# RunLedger event writer
# ---------------------------------------------------------------------------


def _append_compact_event(
    session_id: str, hook_event: str, trigger: str, payload: dict[str, Any] | None = None
) -> None:
    atelier_root = _atelier_root()
    runs_dir = atelier_root / "runs"
    run_file = runs_dir / f"{session_id}.json"
    if not run_file.exists():
        return

    try:
        data = json.loads(run_file.read_text("utf-8"))
    except Exception:
        return

    events: list[dict[str, Any]] = data.setdefault("events", [])

    phase = "starting" if hook_event == "PreCompact" else "completed"
    events.append(
        {
            "kind": "note",
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "summary": f"context compaction {phase} ({trigger})",
            "payload": {
                "hook_event": hook_event,
                "trigger": trigger,
                "event": hook_event,
                **(payload or {}),
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


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------


def _handle_pre_compact(session_id: str, trigger: str) -> None:
    """Handle PreCompact: create/ensure manifest file exists."""
    _ensure_compact_manifest(session_id)
    _append_compact_event(session_id, "PreCompact", trigger)


def _handle_post_compact(session_id: str, trigger: str) -> None:
    """Handle PostCompact: read manifest and record preservation."""
    manifest = _read_compact_manifest(session_id)

    # Record post-compact event
    payload: dict[str, Any] = {}
    if manifest:
        payload = {
            "preserve_blocks": manifest.get("preserve_blocks", []),
            "pin_memory": manifest.get("pin_memory", []),
            "utilisation_pct": manifest.get("utilisation_pct", 0.0),
            "should_handover": manifest.get("should_handover", False),
            "handover_file": manifest.get("handover_file"),
            "manifest_found": True,
        }

    _append_compact_event(session_id, "PostCompact", trigger, payload)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    hook_event: str = payload.get("hook_event_name", "") or ""
    trigger: str = payload.get("trigger", payload.get("matcher", "auto")) or "auto"

    if hook_event not in ("PreCompact", "PostCompact"):
        return 0

    try:
        session_id = _active_session_id()
        if not session_id:
            return 0

        if hook_event == "PreCompact":
            _handle_pre_compact(session_id, trigger)
        elif hook_event == "PostCompact":
            _handle_post_compact(session_id, trigger)
    except Exception:
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
