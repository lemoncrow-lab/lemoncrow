#!/usr/bin/env python3
"""stop hook for Cursor (IDE + cursor-agent CLI).

Runs when the Cursor agent finishes a turn. Two jobs:

1. Refresh the workspace session-attribution bridge so any savings row the
   LemonCrow MCP server wrote is keyed to this session (else it lands in the
   unattributed quarantine ledger and per-session readers under-report it).
2. Emit a one-line savings recap for the session to stderr. Cursor's ``stop``
   hook output contract only supports ``followup_message`` (cursor.com/docs/
   agent/hooks), so -- unlike Claude's in-chat recap -- the user-facing savings
   surface for Cursor is ``lc savings`` / ``lc dashboard``; this recap is a
   diagnostic breadcrumb only.

Self-contained: Cursor runs hooks with no PYTHONPATH guarantee, so LemonCrow
imports are best-effort (the bridge refresh works without them).

Cursor stop payload: base fields include ``conversation_id``,
``workspace_roots``, ``transcript_path``, ``model``; stop adds ``status`` and
``loop_count``. ``session_id`` mirrors ``conversation_id`` -- the same id the
sessionStart bridge and the Cursor importer key on.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path


def _lemoncrow_root() -> Path:
    return Path(
        os.environ.get("LEMONCROW_ROOT", "") or os.environ.get("LEMONCROW_STORE_ROOT", "") or Path.home() / ".lemoncrow"
    )


def _session_id(payload: dict) -> str:
    return (
        os.environ.get("CURSOR_SESSION_ID", "").strip()
        or os.environ.get("CURSOR_TRACE_ID", "").strip()
        or str(payload.get("session_id") or payload.get("conversation_id") or "").strip()
    )


def _session_dir(root: Path, host: str, sid: str, search_days: int = 3) -> Path:
    """Replicate ``paths.session_dir`` so the MCP server reads the same file."""
    base = root / "sessions"
    today = _dt.date.today()
    for off in range(search_days):
        d = today - _dt.timedelta(days=off)
        cand = base / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / host / sid
        if cand.exists():
            return cand
    return base / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}" / host / sid


# Auto/default model selection reports a placeholder, not a concrete id -- skip
# those so pricing falls back cleanly to the Sonnet default (see session_start).
_PLACEHOLDER_MODELS = {"", "default", "auto", "composer", "composer-2", "unknown"}


def _write_stats(session_id: str, payload: dict) -> None:
    """Refresh sessions/.../cursor/<sid>/stats.json with the live model."""
    raw = str(payload.get("model_id") or payload.get("model") or "").strip()
    model = "" if raw.lower() in _PLACEHOLDER_MODELS else raw
    if not session_id or not model:
        return
    d = _session_dir(_lemoncrow_root(), "cursor", session_id)
    stats_path = d / "stats.json"
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {}
        if not isinstance(stats, dict):
            stats = {}
    except (OSError, json.JSONDecodeError):
        stats = {}
    stats["model"] = model
    stats["last_model"] = model
    try:
        d.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(json.dumps(stats), encoding="utf-8")
    except OSError:
        pass


def _refresh_bridge(workspace: str, session_id: str) -> None:
    """Re-stamp <workspace>/.lemoncrow/workspace/session_state.json (host=cursor).

    Mirrors integrations/cursor/hooks/session_start.py so the MCP server's
    ``_workspace_bridge_session_id`` fallback keeps resolving this session.
    """
    if not session_id:
        return
    state_path = Path(workspace).expanduser().resolve() / ".lemoncrow" / "workspace" / "session_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("session_id") == session_id and state.get("host") == "cursor":
        return
    state["session_id"] = session_id
    # Host stamp: the MCP bridge fallback only trusts this shared slot when the
    # stamp matches the reading server's host.
    state["host"] = "cursor"
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _recap(session_id: str, workspace: str) -> str:
    """One-line savings recap via the shared computation (same as Claude/Copilot)."""
    if not session_id:
        return ""
    try:
        from lemoncrow.core.capabilities.savings_summary import (
            _fmt_tok,
            _fmt_usd,
            compute_savings_summary,
            fmt_duration,
        )
    except ImportError:
        return ""
    try:
        s = compute_savings_summary(session_id, lemoncrow_root=_lemoncrow_root(), workspace=workspace)
    except Exception:
        return ""
    if s.saved_usd <= 0 and s.smart_calls <= 0 and s.ctx_saved <= 0:
        return ""
    line = f"savings: {_fmt_usd(s.saved_usd)} · {_fmt_tok(s.ctx_saved)} tok · {s.smart_calls} calls avoided"
    faster = s.time_saved_seconds
    if faster >= 60:
        line += f" · ~{fmt_duration(faster)} faster"
    return line


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    roots = payload.get("workspace_roots")
    workspace = ""
    if isinstance(roots, list) and roots:
        workspace = str(roots[0] or "").strip()
    workspace = workspace or str(payload.get("cwd") or "").strip() or os.getcwd()

    session_id = _session_id(payload)
    _refresh_bridge(workspace, session_id)
    _write_stats(session_id, payload)

    recap = _recap(session_id, workspace)
    if recap:
        sys.stderr.write(f"[lemoncrow:cursor] session complete. {recap}\n")

    # Cursor `stop` output only supports `followup_message`; emit an empty
    # object so we never accidentally re-trigger the agent loop.
    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
