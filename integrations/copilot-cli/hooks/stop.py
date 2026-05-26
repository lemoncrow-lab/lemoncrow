#!/usr/bin/env python3
"""agentStop hook for GitHub Copilot CLI.

Reads events.jsonl for token/tool stats and a workspace-scoped side log for
Atelier savings, then prints a formatted session summary.

Payload from Copilot CLI: {sessionId, transcriptPath, stopReason, timestamp, cwd}
"""

import collections
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _atelier_root() -> Path:
    return Path(os.environ.get("ATELIER_ROOT", "") or Path.home() / ".atelier")


def _workspace_savings_path(workspace: str) -> Path:
    h = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:12]
    return _atelier_root() / "workspaces" / h / "session_savings.jsonl"


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
            except Exception:
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
# Savings side log
# ---------------------------------------------------------------------------


def _read_workspace_savings(workspace: str) -> dict[str, int]:
    path = _workspace_savings_path(workspace)
    tokens_saved = 0
    calls_saved = 0
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
                tokens_saved += int(entry.get("tokens_saved") or 0)
                calls_saved += int(entry.get("calls_saved") or 0)
            except Exception:
                pass
    return {"tokens_saved": tokens_saved, "calls_saved": calls_saved}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _fmt_tok(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _format_summary(stats: dict[str, Any], savings: dict[str, int]) -> str:
    out = stats["output_tokens"]
    calls = stats["tool_calls"]
    tools_used = stats["tools_used"]
    c = stats["compaction"]

    top = sorted(tools_used.items(), key=lambda x: -x[1])[:4]
    tools_str = " · ".join(f"{n}×{cnt}" for n, cnt in top) if top else "none"  # noqa: RUF001

    tokens_saved = int(savings.get("tokens_saved") or 0)
    calls_saved = int(savings.get("calls_saved") or 0)

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

    lines.append(f"savings: $0.0000 · {tokens_saved:,} tokens saved · {calls_saved} calls avoided")
    lines.append(f"top tools: {tools_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}

    transcript_path: str = payload.get("transcriptPath") or ""
    workspace: str = (
        payload.get("cwd")
        or os.environ.get("COPILOT_PROJECT_DIR")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )

    stats = _read_events_stats(transcript_path)
    savings = _read_workspace_savings(workspace)

    if stats["tool_calls"] == 0 and stats["output_tokens"] == 0:
        return 0

    summary = _format_summary(stats, savings)
    print(json.dumps({"systemMessage": f"Atelier: session complete.\n{summary}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
