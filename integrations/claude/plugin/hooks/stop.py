#!/usr/bin/env python3
"""Stop hook — session summary + trace reminder.

Reads the hook payload (stdin: JSON with session_id, transcript_path).

Decision tree:
1. If this was a discussion-only session (no code-editing tools used in the
   transcript) → silent exit.  No trace required.
2. If code work happened AND trace was already called for
   this session → show stats and exit silently.
3. If code work happened but no trace was recorded → surface a system
   message asking Claude to call trace.

Token and tool-call counts are read directly from the Claude Code
transcript JSONL at `transcript_path`.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

# Tools that indicate real code work (not just discussion / exploration).
# Sessions that only used Read, Bash (read-only), Glob, WebFetch, etc. are
# classified as "discussion" and do not require a trace.
CODE_EDITING_TOOLS: frozenset[str] = frozenset(
    {
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
        "TodoWrite",
    }
)

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    import hashlib

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]
    root = Path(os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or Path.home() / ".atelier")
    return root / "workspaces" / h / "session_state.json"


def _load_state() -> dict:  # type: ignore[type-arg]
    sp = _state_path()
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text("utf-8"))  # type: ignore[no-any-return]
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# RunLedger token-count writer (fail-open)
# ---------------------------------------------------------------------------


def _atelier_root() -> Path:
    state = _load_state()
    root = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
    if root:
        return Path(root)
    if state.get("atelier_root"):
        return Path(state["atelier_root"])
    return Path.home() / ".atelier"


def _write_token_event(stats: dict) -> None:  # type: ignore[type-arg]
    """Append a session_stats note event to the active run file."""
    state = _load_state()
    session_id: str | None = state.get("active_session_id")
    if not session_id:
        return
    run_file = _atelier_root() / "runs" / f"{session_id}.json"
    if not run_file.exists():
        return
    try:
        data = json.loads(run_file.read_text("utf-8"))
    except Exception:
        return

    events: list[dict] = data.setdefault("events", [])  # type: ignore[assignment]
    events.append(
        {
            "kind": "note",
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "summary": (
                f"session end — {stats['total_tokens']:,} tokens "
                f"(+{stats['output_tokens']:,} out), "
                f"~${stats['est_cost_usd']:.4f}"
            ),
            "payload": {
                "input_tokens": stats["input_tokens"],
                "output_tokens": stats["output_tokens"],
                "total_tokens": stats["total_tokens"],
                "est_cost_usd": stats["est_cost_usd"],
                "tool_calls": stats["tool_calls"],
                "top_tools": dict(sorted(stats["tools_used"].items(), key=lambda x: -x[1])[:8]),
                "event": "Stop",
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


def _trace_recorded(session_id: str) -> bool:
    """Return True if trace was called in this session.

    Checks session-scoped state first (keyed by *session_id*), then falls
    back to the legacy global ``trace_recorded`` flag for older MCP versions
    that do not write per-session state.
    """
    state = _load_state()

    if session_id:
        sessions: dict[str, dict] = state.get("sessions", {})  # type: ignore[assignment]
        session_data = sessions.get(session_id, {})
        if "trace_recorded" in session_data:
            return bool(session_data["trace_recorded"])

    # Legacy fallback — mcp_server.py < 2.x wrote a flat `trace_recorded` key
    return bool(state.get("trace_recorded"))


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------


def _read_transcript_stats(transcript_path: str) -> dict | None:  # type: ignore[type-arg]
    """Parse the Claude Code transcript JSONL and return session stats."""
    if not transcript_path:
        return None
    p = Path(transcript_path)
    if not p.exists():
        return None

    tool_calls = 0
    input_tokens = 0
    output_tokens = 0
    tools_used: dict[str, int] = {}

    try:
        with p.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue

                msg = entry.get("message", {}) or {}

                # Accumulate token counts from assistant turns
                usage = msg.get("usage", {}) or {}
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)

                # Count tool-use blocks
                for block in msg.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name") or "unknown"
                        tools_used[name] = tools_used.get(name, 0) + 1
                        tool_calls += 1
    except Exception:
        return None

    # Approximate cost (Claude Sonnet 3.7 pricing as baseline)
    # $3/M input, $15/M output — rough indicator, not billed amount
    est_cost_usd = (input_tokens * 3 + output_tokens * 15) / 1_000_000

    return {
        "tool_calls": tool_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "est_cost_usd": est_cost_usd,
        "tools_used": tools_used,
    }


def _is_task_session(stats: dict | None) -> bool:  # type: ignore[type-arg]
    """Return True only if code-editing tools were used this session.

    A session that only called Read, Bash (read-only), Glob, WebFetch,
    WebSearch, or had zero tool calls is classified as a "discussion" session
    and does not require an Atelier trace.
    """
    if stats is None or stats.get("tool_calls", 0) == 0:
        return False
    tools_used: set[str] = set(stats.get("tools_used", {}).keys())
    return bool(CODE_EDITING_TOOLS & tools_used)


def _format_stats(stats: dict) -> str:  # type: ignore[type-arg]
    total = stats["total_tokens"]
    inp = stats["input_tokens"]
    out = stats["output_tokens"]
    calls = stats["tool_calls"]
    cost = stats["est_cost_usd"]

    # Top tools (up to 4)
    top = sorted(stats["tools_used"].items(), key=lambda x: -x[1])[:4]
    tools_str = " · ".join(f"{n}×{c}" for n, c in top) if top else "none"  # noqa: RUF001

    lines = [
        f"tool calls: {calls}",
        f"tokens: {inp:,} in / {out:,} out  ({total:,} total)",
        f"est. cost: ~${cost:.4f}",
        f"top tools: {tools_str}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}

    session_id: str = payload.get("session_id", "") or ""
    transcript_path: str = payload.get("transcript_path", "") or ""
    stats = _read_transcript_stats(transcript_path)

    # ── Always write token/cost summary to RunLedger (fail-open) ─────────────
    if stats and stats.get("total_tokens", 0) > 0:
        with contextlib.suppress(Exception):
            _write_token_event(stats)

    # ── Smart detection: discussion vs task session ──────────────────────────
    # If no code-editing tools were used, this was a discussion or exploration
    # session. Do not require a trace — exit silently.
    if not _is_task_session(stats):
        return 0

    # ── Code work happened: check if trace was recorded ──────────────────────
    if _trace_recorded(session_id):
        # Trace already recorded — show stats via systemMessage and allow exit.
        # Note: hookSpecificOutput is NOT valid for Stop hooks (only PreToolUse,
        # PostToolUse, UserPromptSubmit, PostToolBatch support it).
        if stats and stats["total_tokens"] > 0:
            summary = _format_stats(stats)
            print(json.dumps({"systemMessage": f"Atelier session complete.\n{summary}"}))
        return 0

    # ── Code work done but no trace — surface a non-blocking system message ──
    # Stop hooks only accept "block" as a `decision` value; "ask" is not valid
    # and would silently no-op. Use `systemMessage` for a visible warning.
    msg = (
        "Atelier: no trace was recorded this session. "
        "Call `trace` with the observable summary "
        "(files_touched, commands_run, errors_seen, diff_summary, "
        "validation_results, status) before stopping."
    )

    if stats and stats["total_tokens"] > 0:
        msg += f"\n\nSession stats:\n{_format_stats(stats)}"

    print(json.dumps({"systemMessage": msg}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
