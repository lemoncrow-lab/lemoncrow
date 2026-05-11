#!/usr/bin/env python3
"""SessionStart hook — capture session metadata into the RunLedger.

Fires once when a Claude Code session starts (or resumes / clears / compacts).
Records session_id, model, cwd, source, and timestamp as a ``note`` event in
the active RunLedger.  Also writes ``session_id`` into session_state.json so
other hooks and the Stop hook can correlate events to the session.

Fail-open: any error exits silently (code 0) — never blocks the agent.

Payload received on stdin:
  {
    "session_id": "abc123",
    "transcript_path": "/path/to/session.jsonl",
    "cwd": "/path/to/workspace",
    "hook_event_name": "SessionStart",
    "source": "startup" | "resume" | "clear" | "compact",
    "model": "claude-sonnet-4-6"
  }
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from contextlib import suppress
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
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_session_state(updates: dict[str, Any]) -> None:
    p = _session_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    state = _read_session_state()
    state.update(updates)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


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


def _claude_settings_path() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def _apply_session_bootstrap(payload: dict[str, Any]) -> bool:
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        return False
    try:
        from atelier.core.capabilities.plugin_runtime import apply_session_start_files
    except Exception:
        return False
    with suppress(Exception):
        result = apply_session_start_files(
            _atelier_root(),
            plugin_root,
            config_dir=_claude_settings_path().parent,
            payload=payload,
            current_version=os.environ.get("ATELIER_VERSION", "0.0.0"),
        )
        stdout = result.get("stdout") if isinstance(result, dict) else None
        if stdout:
            print(json.dumps(stdout) if isinstance(stdout, dict) else str(stdout))
        return True
    return False


def _initialize_session_stats(payload: dict[str, Any]) -> None:
    try:
        from atelier.core.capabilities.plugin_runtime import update_session_stats

        update_session_stats(_atelier_root(), payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# RunLedger event writer
# ---------------------------------------------------------------------------


def _append_session_start_event(
    session_id: str,
    source: str,
    model: str,
    cwd: str,
    transcript_path: str,
) -> None:
    runs_dir = _atelier_root() / "runs"
    run_file = runs_dir / f"{session_id}.json"
    if not run_file.exists():
        return

    try:
        data = json.loads(run_file.read_text("utf-8"))
    except Exception:
        return

    events: list[dict[str, Any]] = data.setdefault("events", [])
    events.append(
        {
            "kind": "note",
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "summary": f"session {source} — {model or 'unknown model'}",
            "payload": {
                "session_id": session_id,
                "source": source,
                "model": model,
                "cwd": cwd,
                "transcript_path": transcript_path,
                "event": "SessionStart",
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
            with suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    session_id: str = payload.get("session_id", "") or ""
    source: str = payload.get("source", "startup") or "startup"
    model: str = payload.get("model", "") or ""
    cwd: str = payload.get("cwd", "") or ""
    transcript_path: str = payload.get("transcript_path", "") or ""

    try:
        # Write session_id to session_state so other hooks/Stop can use it
        if session_id:
            _write_session_state({"session_id": session_id, "atelier_root": str(_atelier_root())})

        if not _apply_session_bootstrap(payload):
            _initialize_session_stats(payload)

        session_id = _active_session_id()
        if not session_id:
            return 0

        _append_session_start_event(session_id, source, model, cwd, transcript_path)
    except Exception:
        pass  # fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
