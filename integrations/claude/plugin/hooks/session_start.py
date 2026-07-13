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
import re
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# State helpers
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


def _read_session_state() -> dict[str, Any]:
    p = _session_state_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_session_state(updates: dict[str, Any]) -> None:
    p = _session_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    state = _read_session_state()
    state.update(updates)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=p.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(state, tmp, indent=2)
            tmp_path = tmp.name
        Path(tmp_path).replace(p)
    except OSError:
        if tmp_path:
            with suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
    state = _read_session_state()
    if state.get("lemoncrow_root"):
        return Path(state["lemoncrow_root"])
    return Path.home() / ".lemoncrow"


def _active_session_id() -> str | None:
    state = _read_session_state()
    return state.get("session_id") or state.get("active_session_id")


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
        from lemoncrow.core.capabilities.plugin_runtime import apply_session_start_files
    except (ImportError, AttributeError):
        return False
    with suppress(Exception):
        apply_session_start_files(
            _lemoncrow_root(),
            plugin_root,
            config_dir=_claude_settings_path().parent,
            payload=payload,
            current_version=os.environ.get("LEMONCROW_VERSION", "0.0.0"),
        )
        return True
    return False


def _initialize_session_stats(payload: dict[str, Any]) -> None:
    try:
        from lemoncrow.core.capabilities.plugin_runtime import update_session_stats

        update_session_stats(_lemoncrow_root(), payload)
    except (ImportError, OSError, json.JSONDecodeError, TypeError):
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
    except OSError:
        if tmp_path:
            with suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, TypeError):
        return 0

    session_id_raw: str = payload.get("session_id", "") or ""
    source: str = payload.get("source", "startup") or "startup"
    model: str = payload.get("model", "") or ""
    cwd: str = payload.get("cwd", "") or ""
    transcript_path: str = payload.get("transcript_path", "") or ""

    try:
        # Write session_id + transcript_path to session_state so other hooks
        # and the MCP server can read a one-shot session-id/model bridge.
        if session_id_raw:
            state_update: dict[str, Any] = {
                "session_id": session_id_raw,
                # Host stamp keeps the workspace-shared slot honest: the MCP
                # bridge fallback (non-claude hosts only) rejects a sid whose
                # stamp doesn't match, so a Claude sid written here is never
                # adopted by an OpenCode/Codex server sharing the repo.
                "host": "claude",
                "lemoncrow_root": str(_lemoncrow_root()),
            }
            if model:
                state_update["model"] = model
            if transcript_path:
                state_update["transcript_path"] = transcript_path
            _write_session_state(state_update)

            # Window-anchored identity: write THIS window's own file so the
            # long-lived MCP server recovers the live id across /clear without
            # racing a shared workspace slot. Best-effort; uses the same
            # workspace key the MCP server resolves with.
            # Skipped for source=="clear": Claude Code fires SessionStart(clear)
            # with the PRE-clear session id — anchoring it would point the MCP
            # server's savings attribution at a dead session. The
            # UserPromptSubmit hook re-anchors with the live post-clear id on
            # the first prompt instead.
            if source != "clear":
                with suppress(Exception):
                    from lemoncrow.core.foundation.session_window import (
                        register_window_session,
                        workspace_hash,
                    )

                    _ws = os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd()
                    register_window_session(
                        _lemoncrow_root(),
                        workspace_hash(_ws),
                        session_id=session_id_raw,
                        source=source,
                        transcript_path=transcript_path,
                        model=model,
                    )

        # On /clear, drop a marker so the statusline snapshots the current
        # cumulative live cost as a baseline and shows only post-clear spend.
        # Claude's cost.total_cost_usd is process-cumulative and does NOT reset
        # on /clear; the hook can't see it, but the statusline can. We do this
        # for clear only — /compact continues the same task, so its cost stands.
        if source == "clear" and session_id_raw:
            with suppress(Exception):
                reset_dir = _lemoncrow_root() / "statusline_cost_reset"
                reset_dir.mkdir(parents=True, exist_ok=True)
                (reset_dir / session_id_raw).write_text("", encoding="utf-8")
                # Also write a workspace-keyed marker so the statusline can
                # find it even when /clear assigns a new session_id. Claude
                # Code fires SessionStart(clear) with the pre-clear session_id,
                # but the statusline renders with the post-clear session_id, so
                # the session-keyed marker above is never matched.
                # The workspace key uses the same encoding Claude Code applies
                # to project dirs: replace every non-alphanumeric character
                # with "-" in the cwd.
                if cwd:
                    ws_key = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
                    (reset_dir / f"ws_{ws_key}").write_text("", encoding="utf-8")
        if not _apply_session_bootstrap(payload):
            _initialize_session_stats(payload)

        session_id: str | None = session_id_raw or _active_session_id()
        if not session_id:
            return 0

        _append_session_start_event(session_id, source, model, cwd, transcript_path)
    except (OSError, TypeError, ValueError):
        pass  # fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
