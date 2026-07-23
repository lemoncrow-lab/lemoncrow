#!/usr/bin/env python3
"""Cursor sessionStart hook: bridge the live session id (and model) to LemonCrow.

Cursor never sets a session env var for MCP server subprocesses, so without
this bridge every LemonCrow MCP tool call's savings row is diverted to the
unattributed quarantine ledger and the session always shows Saved $0 even
though Cost displays correctly. This hook writes the live session id into
``<workspace>/.lemoncrow/workspace/session_state.json``, which
``mcp_server._resolved_host_session()`` reads as its fallback.

It ALSO records the active model into ``sessions/YYYY/MM/DD/cursor/<sid>/
stats.json`` (the same per-session stats file the MCP server's
``_bridge_context_state`` reads for non-Claude hosts). Without it the savings
rows get an empty model and price at a Sonnet-fallback rate instead of the
model actually in use; with it token savings price honestly. Live per-turn
context-token counts aren't exposed to Cursor hooks under privacy/ghost mode,
so the avoided-round-trip dollar component stays conservative -- that's a
Cursor platform limit, not something we synthesize.

Payload (cursor.com/docs/agent/hooks, sessionStart): base fields include
``conversation_id``, ``workspace_roots``, ``model``/``model_id``; sessionStart
adds ``session_id`` (documented as "same as conversation_id" -- the composer
id, which is also the session id LemonCrow's Cursor importer keys Traces on).

Self-contained on purpose: hooks run under Cursor's environment with no
PYTHONPATH guarantee, so no lemoncrow import -- the ``sessions/`` layout is
replicated here to match ``lemoncrow.core.foundation.paths.session_dir``.
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


def _session_dir(root: Path, host: str, sid: str, search_days: int = 3) -> Path:
    """Replicate ``paths.session_dir``: sessions/YYYY/MM/DD/<host>/<sid>/.

    Reuse an existing dir within the last ``search_days`` days (so a session
    spanning midnight keeps one folder) else mint today's -- identical to the
    MCP server's resolver so both sides agree on where stats.json lives.
    """
    base = root / "sessions"
    today = _dt.date.today()
    for off in range(search_days):
        d = today - _dt.timedelta(days=off)
        cand = base / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / host / sid
        if cand.exists():
            return cand
    return base / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}" / host / sid


# Cursor reports a placeholder instead of a concrete id when the user runs
# "auto"/default model selection (server-chosen). Those never resolve to a
# rate card, so we don't bridge them -- pricing then falls back cleanly to the
# Sonnet default instead of stamping a bogus "default" model on every row.
_PLACEHOLDER_MODELS = {"", "default", "auto", "composer", "composer-2", "unknown"}


def _model(payload: dict) -> str:
    # ``model_id`` is the clean canonical id (e.g. "claude-opus-4-7"); ``model``
    # is the display slug ("...-thinking-max"). Prefer the id for pricing.
    raw = str(payload.get("model_id") or payload.get("model") or "").strip()
    return "" if raw.lower() in _PLACEHOLDER_MODELS else raw


def _write_session_state(workspace: str, session_id: str) -> None:
    if not session_id:
        return
    state_path = Path(workspace).expanduser().resolve() / ".lemoncrow" / "workspace" / "session_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("session_id") != session_id or state.get("host") != "cursor":
        state["session_id"] = session_id
        # Host stamp: the MCP bridge fallback only trusts this shared slot
        # when the stamp matches the reading server's host.
        state["host"] = "cursor"
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except OSError:
            pass


def _write_stats(session_id: str, model: str) -> None:
    """Seed sessions/.../cursor/<sid>/stats.json with the live model."""
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


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    session_id = str(payload.get("session_id") or payload.get("conversation_id") or "").strip()
    roots = payload.get("workspace_roots")
    workspace = ""
    if isinstance(roots, list) and roots:
        workspace = str(roots[0] or "").strip()
    workspace = workspace or os.getcwd()

    _write_session_state(workspace, session_id)
    _write_stats(session_id, _model(payload))

    # sessionStart is fire-and-forget; emit an empty object for cleanliness.
    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
