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


def _extract_session_title(transcript_path: str) -> str | None:
    """Return the first real user message from the transcript as session title.

    Skips:
    - ``local-command-caveat`` system injections
    - Slash-command entries (text starts with ``/`` or wrapped in XML tags)
    - Entries with ``parentUuid`` set (continuations, not root turns)

    Caps the title at 500 characters.
    """
    if not transcript_path:
        return None
    p = Path(transcript_path)
    if not p.exists():
        return None

    import re

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

                if entry.get("type") != "user":
                    continue
                # Only root turns (parentUuid is null)
                if entry.get("parentUuid") is not None:
                    continue

                msg = entry.get("message", {}) or {}
                content = msg.get("content", "")

                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            break

                # Skip system injections and empty content
                if "local-command-caveat" in text:
                    continue

                # Strip XML command tags (e.g. <command-name>…</command-name>)
                clean = re.sub(r"<[^>]+>.*?</[^>]+>", "", text, flags=re.DOTALL).strip()

                # Skip pure slash-command inputs
                if not clean or clean.startswith("/"):
                    continue

                return clean[:500]
    except Exception:
        return None
    return None


def _extract_user_prompts(transcript_path: str) -> list[str]:
    """Return all real user prompts from the transcript (capped at 2 KB each)."""
    _MAX_PROMPT = 2048
    if not transcript_path:
        return []
    p = Path(transcript_path)
    if not p.exists():
        return []

    import re

    prompts: list[str] = []
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

                if entry.get("type") != "user":
                    continue
                if entry.get("isSidechain"):
                    continue

                msg = entry.get("message", {}) or {}
                content = msg.get("content", "")

                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            break

                if "local-command-caveat" in text:
                    continue

                clean = re.sub(r"<[^>]+>.*?</[^>]+>", "", text, flags=re.DOTALL).strip()
                if not clean or clean.startswith("/"):
                    continue

                prompts.append(clean[:_MAX_PROMPT])
    except Exception:
        pass
    return prompts


def _write_session_enrichment(
    session_id: str,
    session_title: str | None,
    user_prompts: list[str],
    transcript_path: str,
) -> None:
    """Append session_metadata note to the active run file.

    Written by the Stop hook so the run file always contains the real session
    title (first user message) and the full prompt history, regardless of what
    the agent reported via ``record``.
    """
    if not session_id:
        return
    run_file = _atelier_root() / "runs" / f"{session_id}.json"
    if not run_file.exists():
        return
    try:
        data = json.loads(run_file.read_text("utf-8"))
    except Exception:
        return

    # Update top-level task with session_title when the agent left it blank
    if session_title and not (data.get("task") or "").strip():
        data["task"] = session_title

    events: list[dict] = data.setdefault("events", [])
    events.append(
        {
            "kind": "note",
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "summary": f"session_title: {(session_title or '')[:80]}",
            "payload": {
                "session_title": session_title,
                "transcript_path": transcript_path,
                "user_prompts": user_prompts[:50],  # cap at 50 turns
                "prompt_count": len(user_prompts),
                "event": "SessionEnrichment",
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


def _load_session_savings(session_id: str) -> dict:  # type: ignore[type-arg]
    """Return compaction and routing cost savings for this session from JSONL."""
    if not session_id:
        return {"compact": 0.0, "routing": 0.0, "total": 0.0}
    compact_usd = 0.0
    routing_usd = 0.0
    try:
        events_path = _atelier_root() / "live_savings_events.jsonl"
        if events_path.exists():
            with events_path.open(encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        continue
                    if ev.get("session_id") != session_id:
                        continue
                    cost = float(ev.get("cost_saved_usd", 0.0) or 0.0)
                    lever = str(ev.get("lever") or ev.get("tool_name") or "")
                    if "routing" in lever:
                        routing_usd += cost
                    elif "compact" in lever:
                        compact_usd += cost
    except Exception:
        pass
    return {
        "compact": compact_usd,
        "routing": routing_usd,
        "total": compact_usd + routing_usd,
    }


def _format_stats(
    stats: dict,  # type: ignore[type-arg]
    savings: dict | None = None,  # type: ignore[type-arg]
    real_cost: bool = False,
) -> str:
    total = stats["total_tokens"]
    inp = stats["input_tokens"]
    out = stats["output_tokens"]
    calls = stats["tool_calls"]
    cost = stats["est_cost_usd"]

    # Top tools (up to 4)
    top = sorted(stats["tools_used"].items(), key=lambda x: -x[1])[:4]
    tools_str = " · ".join(f"{n}×{c}" for n, c in top) if top else "none"  # noqa: RUF001

    cost_prefix = "cost: " if real_cost else "est. cost: ~"
    lines = [
        f"tool calls: {calls}",
        f"tokens: {inp:,} in / {out:,} out  ({total:,} total)",
        f"{cost_prefix}${cost:.4f}",
        f"top tools: {tools_str}",
    ]

    if savings and savings.get("total", 0.0) > 0:
        parts = []
        if savings["compact"] > 0:
            parts.append(f"compact=${savings['compact']:.4f}")
        if savings["routing"] > 0:
            parts.append(f"routing=${savings['routing']:.4f}")
        lines.append(f"savings: {' · '.join(parts)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-record helper
# ---------------------------------------------------------------------------


def _auto_record(session_id: str, stats: dict | None) -> None:  # type: ignore[type-arg]
    """Call `atelier runs record` silently so the ledger stays complete."""
    import subprocess

    if not session_id:
        return
    total_in = (stats or {}).get("input_tokens", 0)
    total_out = (stats or {}).get("output_tokens", 0)
    cost = (stats or {}).get("est_cost_usd", 0.0)
    tool_calls = (stats or {}).get("tool_calls", 0)
    trace = {
        "agent": "claude-code",
        "domain": "session",
        "task": "session-auto-record",
        "status": "success",
        "session_id": session_id,
        "output_summary": f"tokens: {total_in}in/{total_out}out  cost: ~${cost:.4f}  tools: {tool_calls}",
    }

    atelier_bin = os.environ.get("ATELIER_BIN") or str(Path.home() / ".local" / "bin" / "atelier")
    with contextlib.suppress(Exception):
        subprocess.run(
            [atelier_bin, "runs", "record", "--input", "-"],
            input=json.dumps(trace),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )


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
    payload_cost: float = float(payload.get("total_cost_usd") or payload.get("total_cost") or 0.0)
    if not payload_cost and session_id:
        with contextlib.suppress(Exception):
            cost_file = _atelier_root() / "session_costs" / f"{session_id}.txt"
            if cost_file.is_file():
                payload_cost = float(cost_file.read_text("utf-8").strip())
    real_cost = False
    if stats is not None and payload_cost > 0:
        stats["est_cost_usd"] = payload_cost
        real_cost = True

    # ── Always write token/cost summary to RunLedger (fail-open) ─────────────
    if stats and stats.get("total_tokens", 0) > 0:
        with contextlib.suppress(Exception):
            _write_token_event(stats)

    # ── Enrich run file with session title + full prompt history ──────────────
    with contextlib.suppress(Exception):
        session_title = _extract_session_title(transcript_path)
        user_prompts = _extract_user_prompts(transcript_path)
        if session_title or user_prompts:
            _write_session_enrichment(session_id, session_title, user_prompts, transcript_path)

    # ── Load per-session savings breakdown ────────────────────────────────────
    savings: dict | None = None  # type: ignore[type-arg]
    with contextlib.suppress(Exception):
        savings = _load_session_savings(session_id)
        if savings and savings.get("total", 0.0) <= 0:
            savings = None

    # ── Always show stats (discussion and task sessions alike) ───────────────
    # If no code-editing tools were used, show stats but skip the trace reminder.
    if not _is_task_session(stats):
        if stats and stats["total_tokens"] > 0:
            summary = _format_stats(stats, savings, real_cost=real_cost)
            print(json.dumps({"systemMessage": f"Session stats:\n{summary}"}))
        return 0

    # ── Code work happened: check if trace was recorded ──────────────────────
    if _trace_recorded(session_id):
        # Trace already recorded — show stats via systemMessage and allow exit.
        # Note: hookSpecificOutput is NOT valid for Stop hooks (only PreToolUse,
        # PostToolUse, UserPromptSubmit, PostToolBatch support it).
        if stats and stats["total_tokens"] > 0:
            summary = _format_stats(stats, savings, real_cost=real_cost)
            print(json.dumps({"systemMessage": f"Atelier session complete.\n{summary}"}))
        return 0

    # ── Code work done but no trace — auto-record and show stats ─────────────
    _auto_record(session_id, stats)
    if stats and stats["total_tokens"] > 0:
        summary = _format_stats(stats, savings, real_cost=real_cost)
        print(json.dumps({"systemMessage": f"Atelier: session auto-recorded.\n{summary}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
