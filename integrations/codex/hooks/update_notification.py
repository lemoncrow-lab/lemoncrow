#!/usr/bin/env python3
"""Codex SessionStart update notifier backed by LemonCrow runtime state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
    return Path.home() / ".lemoncrow"


def _session_state_path(cwd: str | None = None) -> Path:
    # Canonical hashing lives in lemoncrow.core.foundation.paths.workspace_key --
    # import it rather than keeping a local copy in sync by hand.
    from lemoncrow.core.foundation.paths import workspace_key

    workspace = cwd or os.environ.get("CODEX_WORKSPACE_ROOT") or os.getcwd()
    h = workspace_key(workspace)
    return _lemoncrow_root() / "workspaces" / h / "session_state.json"


def _write_session_state(session_id: str, cwd: str | None = None, model: str = "") -> None:
    p = _session_state_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        state: dict = json.loads(p.read_text("utf-8")) if p.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    state["session_id"] = session_id
    if model:
        state["model"] = model
    # Stamp the writing host: the MCP server only trusts this workspace-shared
    # slot when the stamp matches its own host, so a sid written here can never
    # be adopted by an OpenCode/other-host server sharing the repo.
    state["host"] = "codex"
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if payload and payload.get("hook_event_name") not in {None, "SessionStart"}:
            return 0

        # Bridge host session_id into session_state.json so MCP server live
        # savings events use the same ID as the stop/savings hooks.
        session_id = str(payload.get("session_id") or "")
        cwd = str(payload.get("cwd") or "")
        if session_id:
            _write_session_state(session_id, cwd or None, str(payload.get("model") or ""))
        # Layer 2 (codex): when the savings cap is exhausted, stash our agent
        # files so Codex falls back to its builtin agent (parity with Claude).
        try:
            from lemoncrow.core.capabilities.plugin_runtime import (
                cap_exhausted,
                reset_host_agents_for_dormancy,
                reset_lemoncrow_global_dormancy,
            )

            _dormant = cap_exhausted(_lemoncrow_root())
            reset_host_agents_for_dormancy("codex", cwd or os.getcwd(), dormant=_dormant)
            reset_lemoncrow_global_dormancy("codex", dormant=_dormant)
        except Exception:  # noqa: BLE001 — best-effort; never break the hook
            pass

        # Check for update notification from daemon/MCP auto-update
        state_path = _lemoncrow_root() / "update_state.json"
        if state_path.exists():
            update_data = json.loads(state_path.read_text("utf-8"))
            if (
                isinstance(update_data, dict)
                and update_data.get("current_version")
                and update_data.get("previous_version")
                and update_data["current_version"] != update_data["previous_version"]
                and not update_data.get("notified")
            ):
                prev_ver = update_data["previous_version"]
                cur_ver = update_data["current_version"]
                method = update_data.get("method", "auto")
                msg = (
                    f"lc updated from {prev_ver} → {cur_ver} (via {method}). "
                    "Release notes: https://github.com/lemoncrow-lab/lemoncrow/releases"
                )
                sys.stdout.write(json.dumps({"systemMessage": msg}) + "\n")
                sys.stdout.flush()
                # Mark as notified
                update_data["notified"] = True
                state_path.write_text(json.dumps(update_data, indent=2), encoding="utf-8")
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
