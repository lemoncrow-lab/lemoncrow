#!/usr/bin/env python3
"""agentStop hook for GitHub Copilot CLI.

Reads events.jsonl for token/tool stats and delegates to the shared Atelier
savings computation (the same one Claude Code's stop hook uses) for the
per-session savings breakdown.

Payload from Copilot CLI: {sessionId, transcriptPath, stopReason, timestamp, cwd}
"""

import collections
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from atelier.core.capabilities.savings_summary import _fmt_tok, _fmt_usd
except ImportError:
    # This hook has no PYTHONPATH guarantee the way the Claude/Codex plugin
    # hooks do (their installers wire it up; copilot-cli's does not) -- fall
    # back to the same two formatting rules rather than crash the recap.
    def _fmt_tok(n: int) -> str:
        n = int(n or 0)
        if n >= 1_000_000:
            return f"{n / 1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n)

    def _fmt_usd(v: float) -> str:
        v = float(v or 0.0)
        if abs(v) < 1:
            return f"${v:.4f}"
        return f"${v:,.2f}"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _atelier_root() -> Path:
    return Path(os.environ.get("ATELIER_ROOT", "") or Path.home() / ".atelier")


def _session_id(payload: dict[str, Any]) -> str:
    """Resolve the copilot-cli session id for the savings-sidecar lookup.

    ``GITHUB_COPILOT_SESSION_ID`` is what the MCP writer keys the per-session
    ``savings.jsonl`` sidecar on (see
    ``mcp_server.py:_get_host_session_sidecar_path``); the payload's own
    ``sessionId`` (present on every agentStop call, mirrored by the sibling
    ``post_tool_use_failure.py`` hook) is the fallback for when the env var
    isn't propagated to this hook's process.
    """
    return (
        os.environ.get("GITHUB_COPILOT_SESSION_ID", "").strip()
        or str(payload.get("sessionId") or payload.get("session_id") or "").strip()
    )


# ---------------------------------------------------------------------------
# events.jsonl parsing
# ---------------------------------------------------------------------------


def _read_events_stats(transcript_path: str) -> dict[str, Any]:
    """Parse events.jsonl and return token/tool stats available at hook time.

    Only `assistant.message.outputTokens` is reliable per-message.
    Full 4-field billing breakdown only exists in `session.compaction_complete`
    (for compaction calls) and `session.shutdown` (arrives ~30s after stop).
    """
    stats: dict[str, Any] = {
        "output_tokens": 0,
        "tool_calls": 0,
        "tools_used": collections.Counter(),
        "compaction": {
            "count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
    }
    if not transcript_path:
        return stats
    p = Path(transcript_path)
    if not p.exists():
        return stats

    with p.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type") or ""
            data = ev.get("data") or {}

            if etype == "assistant.message":
                stats["output_tokens"] += int(data.get("outputTokens") or 0)

            elif etype == "tool.execution_start":
                name = data.get("toolName") or "unknown"
                stats["tool_calls"] += 1
                stats["tools_used"][name] += 1

            elif etype == "session.compaction_complete":
                used = data.get("compactionTokensUsed") or {}
                c = stats["compaction"]
                c["count"] += 1
                c["input_tokens"] += int(used.get("inputTokens") or 0)
                c["output_tokens"] += int(used.get("outputTokens") or 0)
                c["cache_read_tokens"] += int(used.get("cacheReadTokens") or 0)
                c["cache_write_tokens"] += int(used.get("cacheWriteTokens") or 0)

    stats["tools_used"] = dict(stats["tools_used"])
    return stats


# ---------------------------------------------------------------------------
# Savings summary
# ---------------------------------------------------------------------------

_ZERO_SAVINGS: dict[str, Any] = {
    "saved_usd": 0.0,
    "tokens_saved": 0,
    "calls_avoided": 0,
    "carry_usd": 0.0,
    "carry_tokens": 0,
    "output_usd": 0.0,
    "output_tokens": 0,
    "routing_usd": 0.0,
}


def _read_workspace_savings(session_id: str, workspace: str) -> dict[str, Any]:
    """Session savings breakdown via the shared computation.

    Delegates to ``compute_savings_summary`` -- the same function Claude
    Code's stop hook uses -- instead of hand-summing raw ``savings.jsonl``
    rows itself. The old per-row reader here could only total tokens and
    pre-priced dollars, so it structurally could never show carry/output/
    routing; this gets the same components Claude Code's recap has.
    """
    if not session_id:
        return dict(_ZERO_SAVINGS)
    try:
        from atelier.core.capabilities.savings_summary import compute_savings_summary
    except ImportError:
        return dict(_ZERO_SAVINGS)
    summary = compute_savings_summary(session_id, atelier_root=_atelier_root(), workspace=workspace)
    return {
        "saved_usd": float(summary.saved_usd),
        "tokens_saved": int(summary.ctx_saved),
        "calls_avoided": int(summary.smart_calls),
        "carry_usd": float(summary.carry_usd),
        "carry_tokens": int(summary.carry_tokens),
        "output_usd": float(summary.output_saved_usd),
        "output_tokens": int(summary.output_saved_tokens),
        "routing_usd": float(summary.routing_saved_usd),
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_summary(stats: dict[str, Any], savings: dict[str, Any]) -> str:
    out = stats["output_tokens"]
    calls = stats["tool_calls"]
    tools_used = stats["tools_used"]
    c = stats["compaction"]

    top = sorted(tools_used.items(), key=lambda x: -x[1])[:4]
    tools_str = " · ".join(f"{n}×{cnt}" for n, cnt in top) if top else "none"  # noqa: RUF001

    saved_usd = float(savings.get("saved_usd") or 0.0)
    tokens_saved = int(savings.get("tokens_saved") or 0)
    calls_avoided = int(savings.get("calls_avoided") or 0)
    routing_usd = float(savings.get("routing_usd") or 0.0)
    carry_usd = float(savings.get("carry_usd") or 0.0)
    carry_tokens = int(savings.get("carry_tokens") or 0)
    output_usd = float(savings.get("output_usd") or 0.0)
    output_tokens = int(savings.get("output_tokens") or 0)

    lines = [f"tool calls: {calls}"]

    # Output tokens from message events (overestimates slightly due to thinking tokens)
    if c["count"] > 0:
        comp_detail = (
            f"{_fmt_tok(c['input_tokens'])} in / "
            f"{_fmt_tok(c['cache_write_tokens'])} cW / "
            f"{_fmt_tok(c['cache_read_tokens'])} cR / "
            f"{_fmt_tok(c['output_tokens'])} out"
        )
        lines.append(f"tokens out: {_fmt_tok(out)}  (compaction x{c['count']}: {comp_detail})")
    else:
        lines.append(f"tokens out: {_fmt_tok(out)}")

    # Component set/suppression mirrors Claude Code's stop hook exactly:
    # Output/Carry/Routing lines are omitted when exactly 0; the headline
    # total and calls-avoided always show.
    savings_line = f"savings: {_fmt_usd(saved_usd)} · {_fmt_tok(tokens_saved)} tok · {calls_avoided} calls avoided"
    if output_usd > 0:
        out_tokens_str = f"/{_fmt_tok(output_tokens)} tok" if output_tokens > 0 else ""
        savings_line += f" · O {_fmt_usd(output_usd)}{out_tokens_str}"
    if carry_usd > 0:
        carry_tokens_str = f"/{_fmt_tok(carry_tokens)} tok" if carry_tokens > 0 else ""
        savings_line += f" · carry {_fmt_usd(carry_usd)}{carry_tokens_str}"
    if routing_usd > 0:
        savings_line += f" · routing {_fmt_usd(routing_usd)}"
    try:
        from atelier.core.capabilities.savings_summary import estimate_time_saved_seconds, fmt_duration

        _faster_s = estimate_time_saved_seconds(calls_avoided=calls_avoided, output_saved_tokens=output_tokens)
        if _faster_s >= 60:
            savings_line += f" · ~{fmt_duration(_faster_s)} faster"
    except ImportError:
        pass
    lines.append(savings_line)

    lines.append(f"top tools: {tools_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    transcript_path: str = payload.get("transcriptPath") or ""
    workspace: str = (
        payload.get("cwd")
        or os.environ.get("COPILOT_PROJECT_DIR")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )

    stats = _read_events_stats(transcript_path)
    savings = _read_workspace_savings(_session_id(payload), workspace)

    if stats["tool_calls"] == 0 and stats["output_tokens"] == 0:
        return 0

    summary = _format_summary(stats, savings)
    sys.stdout.write(json.dumps({"systemMessage": f"Atelier: session complete.\n{summary}"}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
