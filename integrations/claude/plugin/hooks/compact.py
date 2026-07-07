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


def _claude_stats_path(session_id: str) -> Path:
    """Path to the session-scoped stats.json for a Claude Code session."""
    from atelier.core.foundation.paths import session_dir

    root = _atelier_root()
    return session_dir(root, "claude", session_id) / "stats.json"


def _carry_optimizer_notices_precompact(session_id: str, state: dict[str, Any]) -> None:
    """Read optimizer_notices from the old session's stats and stage into session_state.

    Ensures one-shot nudges (ctx_high) survive the session_id change that
    /compact performs.
    """
    try:
        stats_path = _claude_stats_path(session_id)
        if stats_path.exists():
            stats = json.loads(stats_path.read_text("utf-8"))
            notices = stats.get("optimizer_notices") if isinstance(stats, dict) else None
            if isinstance(notices, dict) and notices:
                state["precompact_optimizer_notices"] = notices
    except (ImportError, OSError, json.JSONDecodeError, TypeError, ValueError):
        pass  # Fail-open


def _restore_optimizer_notices_postcompact(session_id: str, state: dict[str, Any]) -> None:
    """Write staged optimizer_notices into the post-compact session's stats."""
    notices = state.pop("precompact_optimizer_notices", None)
    if not isinstance(notices, dict) or not notices:
        return
    try:
        stats_path = _claude_stats_path(session_id)
        existing = json.loads(stats_path.read_text("utf-8")) if stats_path.exists() else {}
        if not isinstance(existing, dict):
            existing = {}
        merged = {
            **notices,
            **(existing.get("optimizer_notices") or {} if isinstance(existing.get("optimizer_notices"), dict) else {}),
        }
        existing["optimizer_notices"] = merged
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except (ImportError, OSError, json.JSONDecodeError, TypeError, ValueError):
        pass  # Fail-open


def _read_session_state() -> dict[str, Any]:
    p = _session_state_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_session_state(state: dict[str, Any]) -> None:
    path = _session_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(state, tmp, indent=2)
            tmp_path = tmp.name
        Path(tmp_path).replace(path)
    except (OSError, TypeError, ValueError):
        if tmp_path:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)


def _context_occupancy(transcript_path: str) -> tuple[int, str | None]:
    """Return ``(live_window_tokens, model)`` from the transcript's last usage block."""
    try:
        occ = 0
        model: str | None = None
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                message = entry.get("message") or {}
                usage = message.get("usage") or {}
                turn = sum(
                    int(usage.get(k, 0) or 0)
                    for k in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
                if turn > 0:
                    occ = turn
                    model = message.get("model") or model
        return occ, model
    except OSError:
        return 0, None


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
    try:
        from atelier.core.foundation.paths import session_dir

        run_dir = session_dir(atelier_root, "claude", session_id)
    except ImportError:
        return atelier_root / "sessions" / session_id / "compact_manifest.json"
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
            "preserve_playbooks": [],
            "pin_memory": [],
            "open_files": [],
            "recent_turns": [],
            "claude_md_hash": None,
            "active_errors": [],
            "handover_file": None,
            "suggested_prompt": "Compact this conversation.",
        }
        with contextlib.suppress(OSError, TypeError):
            manifest_path.write_text(json.dumps(initial, indent=2), encoding="utf-8")

    return manifest_path


def _read_compact_manifest(session_id: str) -> dict[str, Any] | None:
    """Read compact_manifest.json from the run directory."""
    try:
        from atelier.core.foundation.paths import session_dir

        atelier_root = _atelier_root()
        manifest_path = session_dir(atelier_root, "claude", session_id) / "compact_manifest.json"
        if manifest_path.exists():
            data = json.loads(manifest_path.read_text("utf-8"))
            if isinstance(data, dict):
                return data
    except (ImportError, OSError, json.JSONDecodeError):
        pass
    return None


# ---------------------------------------------------------------------------
# RunLedger event writer
# ---------------------------------------------------------------------------


def _append_compact_event(
    session_id: str, hook_event: str, trigger: str, payload: dict[str, Any] | None = None
) -> None:
    try:
        from atelier.core.foundation.paths import session_dir
    except ImportError:
        return
    run_file = session_dir(_atelier_root(), "claude", session_id) / "run.json"
    if not run_file.exists():
        return

    try:
        data = json.loads(run_file.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
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
    except (OSError, TypeError, ValueError):
        if tmp_path:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------


def _checkpoint_pre_compact_usage(session_id: str, transcript_path: str) -> None:
    """Snapshot cumulative session usage into stats.json before compact rewrites the transcript.

    After /compact, Claude Code replaces the transcript JSONL with a compact summary.
    The pre-compact token history is then invisible to read_transcript_stats at stop time.
    By saving the running totals here (before the overwrite), the stop hook can add them
    back on top of whatever post-compact usage the transcript shows.

    Stores the HIGH-WATER MARK across compacts (max per field, not sum).
    Fail-open: any error is silently swallowed.
    """
    try:
        from atelier.core.capabilities.savings_summary import read_transcript_stats

        stats = read_transcript_stats(transcript_path)
        if stats is None:
            return
        # Only checkpoint if there's real usage to preserve.
        if not (stats.input_tokens or stats.output_tokens or stats.cache_read_tokens or stats.cache_write_tokens):
            return

        from atelier.core.foundation.paths import session_dir

        atelier_root = _atelier_root()
        stats_path = session_dir(atelier_root, "claude", session_id) / "stats.json"
        try:
            existing: dict[str, Any] = json.loads(stats_path.read_text("utf-8")) if stats_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            existing = {}

        # High-water mark — keep the largest snapshot seen across compacts.
        #
        # Why max, not sum: Claude Code preserves the full transcript after each
        # /compact (old turns remain below compact_boundary markers), so
        # read_transcript_stats() already counts every token once via msg_id
        # dedup.  Summing N growing snapshots inflated cost by up to Nx in
        # heavy-compact sessions.  With max, pre_compact holds the full session
        # state at the most recent compact; stop.py then adds only the delta
        # above what the post-compact transcript already shows (≈ 0 when the
        # transcript is complete, correct recovery when entries were erased).
        prev = existing.get("pre_compact_usage")
        if not isinstance(prev, dict):
            prev = {}
        existing["pre_compact_usage"] = {
            "input_tokens": max(int(prev.get("input_tokens", 0) or 0), stats.input_tokens),
            "output_tokens": max(int(prev.get("output_tokens", 0) or 0), stats.output_tokens),
            "cache_read_tokens": max(int(prev.get("cache_read_tokens", 0) or 0), stats.cache_read_tokens),
            "cache_write_tokens": max(int(prev.get("cache_write_tokens", 0) or 0), stats.cache_write_tokens),
            "est_cost_usd": max(float(prev.get("est_cost_usd", 0.0) or 0.0), stats.est_cost_usd),
        }
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(json.dumps(existing, indent=2), "utf-8")
    except Exception:  # noqa: BLE001
        pass  # Fail-open — never block the compact


def _handle_pre_compact(session_id: str, trigger: str, transcript_path: str = "") -> None:
    """Handle PreCompact: create manifest and capture pre-compaction occupancy.

    The live window size is recorded into session state so the next user prompt
    can credit the realized cache-read reduction once the compacted window size
    is known (it isn't yet — no model turn has run on the summary).
    """
    _ensure_compact_manifest(session_id)
    _append_compact_event(session_id, "PreCompact", trigger)
    if transcript_path:
        # Snapshot cumulative usage BEFORE Claude Code overwrites the transcript.
        # stop.py will add these pre-compact totals on top of post-compact transcript stats.
        _checkpoint_pre_compact_usage(session_id, transcript_path)
        occ, model = _context_occupancy(transcript_path)
        state = _read_session_state()
        if occ > 0:
            state["precompact_occupancy"] = occ
            state["precompact_model"] = model or ""
            state["precompact_pending"] = True
            state["precompact_attempts"] = 0
        # Carry optimizer_notices across the session boundary so that one-shot
        # nudges (ctx_high) survive compaction.
        _carry_optimizer_notices_precompact(session_id, state)
        _write_session_state(state)


def _handle_post_compact(session_id: str, trigger: str) -> None:
    """Handle PostCompact: read manifest and record preservation."""
    manifest = _read_compact_manifest(session_id)

    # Record post-compact event
    payload: dict[str, Any] = {}
    if manifest:
        payload = {
            "preserve_playbooks": manifest.get("preserve_playbooks", []),
            "pin_memory": manifest.get("pin_memory", []),
            "utilisation_pct": manifest.get("utilisation_pct", 0.0),
            "should_handover": manifest.get("should_handover", False),
            "handover_file": manifest.get("handover_file"),
            "manifest_found": True,
        }

    _append_compact_event(session_id, "PostCompact", trigger, payload)

    # Bump the compaction epoch so the MCP server's within-session content dedup
    # resets — the compacted summary may no longer hold previously-returned bytes.
    with contextlib.suppress(OSError, ValueError, TypeError):
        state = _read_session_state()
        state["compaction_epoch"] = int(state.get("compaction_epoch", 0) or 0) + 1
        # Restore optimizer_notices from pre-compact session so the one-shot
        # nudge guard survives the session_id change.
        _restore_optimizer_notices_postcompact(session_id, state)
        _write_session_state(state)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    hook_event: str = payload.get("hook_event_name", "") or ""
    trigger: str = payload.get("trigger", payload.get("matcher", "auto")) or "auto"

    if hook_event not in ("PreCompact", "PostCompact"):
        return 0

    try:
        # Prefer the session_id Claude Code sends in the payload — it matches
        # what the Stop hook will receive for the same session.  Falling back
        # to _active_session_id() risks a mismatch when SessionStart (which
        # updates session_state.json) fires *before* PreCompact: the state
        # file already holds the new post-compact session_id while the payload
        # and transcript still belong to the old session.  That mismatch causes
        # the old session's pre_compact_usage to be written under the new
        # session_id, inflating the new session's stop-hook cost.
        session_id = str(payload.get("session_id") or "") or _active_session_id()
        if not session_id:
            return 0

        if hook_event == "PreCompact":
            _handle_pre_compact(session_id, trigger, payload.get("transcript_path", "") or "")
        elif hook_event == "PostCompact":
            _handle_post_compact(session_id, trigger)
    except (OSError, ValueError, TypeError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
