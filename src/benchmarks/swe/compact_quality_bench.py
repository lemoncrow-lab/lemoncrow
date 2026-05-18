"""Compact QUALITY benchmark - real export replay.

Answers the question: after Atelier's compactor fires, does the session
continue healthily or does it regress (more errors, more re-reads)?

Two complementary quality signals
----------------------------------
1. Error rate drift
   Count failed tool_results (is_error=True or error-pattern text) in the
   N turns immediately before and after each compaction event.
   Positive drift = compaction introduced regressions.
   Negative drift = session was already recovering when compact fired.

2. Re-read cost (proxy for lost context)
   Count Read/search tool calls in the first M turns after each compact.
   A model that has to re-read files it already edited lost that context.
   Baseline subtracted: we only count *extra* re-reads vs the pre-compact rate.

Metrics per compaction event
-----------------------------
  pre_error_rate    failed results / total results in last 15 turns before
  post_error_rate   failed results / total results in first 15 turns after
  error_drift       post - pre  (negative = good)
  pre_read_rate     Read+search calls / turns in last 8 turns before
  post_read_rate    same for first 8 turns after
  extra_read_rate   max(0, post_read_rate - pre_read_rate)
  retention_score   1.0 - clamp(error_drift x 2 + extra_read_rate x 0.5, 0, 1)
  session_continued bool - any real assistant turns after compact

Overall quality score = mean(retention_score) across all compaction events.

Compaction detection
--------------------
Same as compact_bench.py: consecutive real assistant events where
  (context_before - context_after) / context_before >= 0.40
  AND context_after >= 5 000 tokens  (excludes sub-agent context resets)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COMPACT_DROP_THRESHOLD = 0.40  # >=40% drop in effective context
_MIN_CONTEXT_AFTER = 5_000  # below this = sub-agent reset, not compact
_PRE_POST_WINDOW = 15  # turns to look at before/after compact
_READ_WINDOW = 8  # turns to count re-reads in
_READ_TOOLS = frozenset({"read", "grep", "glob", "websearch", "webfetch", "search"})

_ERROR_PATTERNS = re.compile(
    r"\b(error|exception|traceback|syntaxerror|typeerror|valueerror|"
    r"nameerror|command\s+failed|exit\s+code\s+[1-9]|cannot\s+find|"
    r"no\s+such\s+file|permission\s+denied)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Parsed events
# ---------------------------------------------------------------------------


@dataclass
class _Turn:
    """One real (non-synthetic) assistant -> user round trip."""

    turn_idx: int
    total_context: int  # inp + cache_create + cache_read (for compaction detection)
    fresh_tokens: int  # inp + cache_create (for cost)
    output_tokens: int
    tool_names: list[str]
    # Outcome from the following user event
    had_tool_results: bool
    failed_tool_count: int
    total_tool_count: int


def _is_error_content(content: Any) -> bool:
    if isinstance(content, list):
        text = " ".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
    else:
        text = str(content or "")
    return bool(_ERROR_PATTERNS.search(text))


def _parse_turns(path: Path) -> list[_Turn]:
    """Parse session into a list of round-trip turns."""
    turns: list[_Turn] = []
    last_fp: tuple[int, int, int, int] | None = None

    # We need to pair assistant events with their following user events.
    # Strategy: collect all raw events first, then pair them.
    raw_events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    raw_events.append(json.loads(raw))
                except Exception:
                    continue
    except Exception:
        return []

    i = 0
    turn_idx = 0
    while i < len(raw_events):
        ev = raw_events[i]
        if ev.get("type") != "assistant":
            i += 1
            continue

        msg = ev.get("message") or {}
        usage = msg.get("usage") or {}
        inp = int(usage.get("input_tokens", 0))
        cache_c = int(usage.get("cache_creation_input_tokens", 0))
        cache_r = int(usage.get("cache_read_input_tokens", 0))
        out = int(usage.get("output_tokens", 0))
        fp = (inp, cache_c, cache_r, out)

        # Skip duplicate sub-agent context flush
        if fp == last_fp:
            i += 1
            continue
        last_fp = fp

        model = str(msg.get("model") or "")
        synthetic = model == "<synthetic>" or not model
        if synthetic or (inp == 0 and cache_c == 0 and cache_r == 0 and out == 0):
            i += 1
            continue

        tool_names = [
            str(b.get("name", ""))
            for b in (msg.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]

        # Find the immediately following user event
        failed = 0
        total_results = 0
        j = i + 1
        while j < len(raw_events) and raw_events[j].get("type") != "user":
            j += 1

        if j < len(raw_events) and raw_events[j].get("type") == "user":
            last_fp = None  # reset dedup at user event
            user_msg = raw_events[j].get("message") or {}
            user_content = user_msg.get("content") or []
            if isinstance(user_content, list):
                for b in user_content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        total_results += 1
                        if b.get("is_error") or _is_error_content(b.get("content", "")):
                            failed += 1

        turns.append(
            _Turn(
                turn_idx=turn_idx,
                total_context=inp + cache_c + cache_r,
                fresh_tokens=inp + cache_c,
                output_tokens=out,
                tool_names=tool_names,
                had_tool_results=total_results > 0,
                failed_tool_count=failed,
                total_tool_count=total_results,
            )
        )
        turn_idx += 1
        i = j + 1 if j < len(raw_events) else i + 1

    return turns


# ---------------------------------------------------------------------------
# Compaction event analysis
# ---------------------------------------------------------------------------


@dataclass
class CompactionQuality:
    event_idx: int  # index in turns list
    context_before: int
    context_after: int
    freed_pct: float

    # Error rate window
    pre_error_rate: float  # failed / total in last _PRE_POST_WINDOW turns
    post_error_rate: float
    error_drift: float  # post - pre

    # Re-read window
    pre_read_rate: float  # read tools / turns in last _READ_WINDOW turns
    post_read_rate: float
    extra_read_rate: float  # max(0, post - pre)

    # Continuation
    session_continued: bool
    turns_after_compact: int

    # Composite
    retention_score: float  # 0.0-1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_before": self.context_before,
            "context_after": self.context_after,
            "freed_pct": round(self.freed_pct, 1),
            "pre_error_rate": round(self.pre_error_rate, 3),
            "post_error_rate": round(self.post_error_rate, 3),
            "error_drift": round(self.error_drift, 3),
            "pre_read_rate": round(self.pre_read_rate, 3),
            "post_read_rate": round(self.post_read_rate, 3),
            "extra_read_rate": round(self.extra_read_rate, 3),
            "session_continued": self.session_continued,
            "turns_after_compact": self.turns_after_compact,
            "retention_score": round(self.retention_score, 3),
        }


def _error_rate(turns: list[_Turn]) -> float:
    total = sum(t.total_tool_count for t in turns)
    failed = sum(t.failed_tool_count for t in turns)
    return failed / total if total > 0 else 0.0


def _read_rate(turns: list[_Turn]) -> float:
    """Fraction of turns that had at least one read-only tool call."""
    if not turns:
        return 0.0
    read_turns = sum(1 for t in turns if any(name.lower() in _READ_TOOLS for name in t.tool_names))
    return read_turns / len(turns)


def _retention_score(error_drift: float, extra_read_rate: float) -> float:
    """Composite quality score for one compaction event.

    error_drift: post_error_rate - pre_error_rate
      positive = more errors after compact (bad)
      negative = fewer errors after compact (good, compact helped)
    extra_read_rate: extra Read calls post compact vs pre compact baseline
      higher = more context was lost and had to be re-acquired
    """
    penalty = max(0.0, error_drift) * 2.0 + extra_read_rate * 0.5
    return max(0.0, min(1.0, 1.0 - penalty))


def _analyze_compactions(turns: list[_Turn]) -> list[CompactionQuality]:
    """Find all real compaction events and score their quality."""
    results: list[CompactionQuality] = []

    for i in range(1, len(turns)):
        prev = turns[i - 1]
        curr = turns[i]
        if prev.total_context == 0:
            continue
        drop = (prev.total_context - curr.total_context) / prev.total_context
        if drop < _COMPACT_DROP_THRESHOLD:
            continue
        if curr.total_context < _MIN_CONTEXT_AFTER:
            continue  # sub-agent context reset, not real compact

        # Pre window
        pre_start = max(0, i - _PRE_POST_WINDOW)
        pre_turns = turns[pre_start:i]
        pre_read = turns[max(0, i - _READ_WINDOW) : i]

        # Post window
        post_end = min(len(turns), i + _PRE_POST_WINDOW)
        post_turns = turns[i:post_end]
        post_read = turns[i : min(len(turns), i + _READ_WINDOW)]

        pre_err = _error_rate(pre_turns)
        post_err = _error_rate(post_turns)
        drift = post_err - pre_err

        pre_rr = _read_rate(pre_read)
        post_rr = _read_rate(post_read)
        extra_rr = max(0.0, post_rr - pre_rr)

        continues = any(t.total_context > 0 for t in turns[i + 1 :])

        results.append(
            CompactionQuality(
                event_idx=i,
                context_before=prev.total_context,
                context_after=curr.total_context,
                freed_pct=round(drop * 100, 1),
                pre_error_rate=pre_err,
                post_error_rate=post_err,
                error_drift=drift,
                pre_read_rate=pre_rr,
                post_read_rate=post_rr,
                extra_read_rate=extra_rr,
                session_continued=continues,
                turns_after_compact=len(turns) - i,
                retention_score=_retention_score(drift, extra_rr),
            )
        )

    return results


# ---------------------------------------------------------------------------
# Session result
# ---------------------------------------------------------------------------


@dataclass
class SessionCompactQuality:
    session_id: str
    total_turns: int
    compaction_events: int
    avg_retention_score: float
    avg_error_drift: float
    avg_extra_read_rate: float
    sessions_continued_pct: float
    events: list[CompactionQuality] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "total_turns": self.total_turns,
            "compaction_events": self.compaction_events,
            "avg_retention_score": round(self.avg_retention_score, 3),
            "avg_error_drift": round(self.avg_error_drift, 3),
            "avg_extra_read_rate": round(self.avg_extra_read_rate, 3),
            "sessions_continued_pct": round(self.sessions_continued_pct, 1),
            "events": [e.to_dict() for e in self.events],
        }


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run_compact_quality_bench(
    corpus_dir: Path,
    *,
    max_sessions: int | None = None,
) -> dict[str, Any]:
    """Run compact quality benchmark over *corpus_dir*."""
    search_dir = corpus_dir / "claude" if (corpus_dir / "claude").is_dir() else corpus_dir

    candidates = sorted(search_dir.glob("*.jsonl"), key=lambda p: -p.stat().st_size)

    session_results: list[SessionCompactQuality] = []
    sessions_skipped = 0

    for path in candidates:
        if max_sessions is not None and len(session_results) >= max_sessions:
            break

        turns = _parse_turns(path)
        if not turns:
            sessions_skipped += 1
            continue

        compactions = _analyze_compactions(turns)
        if not compactions:
            sessions_skipped += 1
            continue

        n = len(compactions)
        avg_ret = sum(c.retention_score for c in compactions) / n
        avg_drift = sum(c.error_drift for c in compactions) / n
        avg_rr = sum(c.extra_read_rate for c in compactions) / n
        cont_pct = sum(1 for c in compactions if c.session_continued) / n * 100

        session_results.append(
            SessionCompactQuality(
                session_id=path.stem,
                total_turns=len(turns),
                compaction_events=n,
                avg_retention_score=avg_ret,
                avg_error_drift=avg_drift,
                avg_extra_read_rate=avg_rr,
                sessions_continued_pct=cont_pct,
                events=compactions,
            )
        )

    if not session_results:
        return {
            "benchmark": "quality-compact",
            "note": (
                "retention_score = 1 - clamp(error_drift x 2 + extra_read_rate x 0.5, 0, 1). "
                "error_drift = post_error_rate - pre_error_rate (negative=good). "
                "extra_read_rate = extra Read/search calls post-compact vs pre-compact baseline "
                "(proxy for context re-acquisition after loss)."
            ),
            "sessions_benchmarked": 0,
            "sessions_skipped": sessions_skipped,
            "total_compaction_events": 0,
            "avg_retention_score": 0.0,
            "avg_error_drift": 0.0,
            "avg_extra_read_rate": 0.0,
            "sessions_continued_pct": 0.0,
            "sessions": [],
            "generated_at": datetime.now(UTC).isoformat(),
        }

    n_sess = len(session_results)
    total_events = sum(r.compaction_events for r in session_results)
    avg_ret_global = sum(r.avg_retention_score for r in session_results) / n_sess
    avg_drift_global = sum(r.avg_error_drift for r in session_results) / n_sess
    avg_rr_global = sum(r.avg_extra_read_rate for r in session_results) / n_sess
    cont_global = sum(r.sessions_continued_pct for r in session_results) / n_sess

    return {
        "benchmark": "quality-compact",
        "note": (
            "retention_score = 1 - clamp(error_drift x 2 + extra_read_rate x 0.5, 0, 1). "
            "error_drift = post_error_rate - pre_error_rate (negative=good, compact helped). "
            "extra_read_rate = extra Read/search calls post-compact vs pre-compact baseline "
            "(proxy for context re-acquisition after loss). "
            "All signals measured from real export data - no LLM replay."
        ),
        "sessions_benchmarked": n_sess,
        "sessions_skipped": sessions_skipped,
        "total_compaction_events": total_events,
        "avg_retention_score": round(avg_ret_global, 3),
        "avg_error_drift": round(avg_drift_global, 3),
        "avg_extra_read_rate": round(avg_rr_global, 3),
        "sessions_continued_pct": round(cont_global, 1),
        "sessions": [r.to_dict() for r in session_results],
        "generated_at": datetime.now(UTC).isoformat(),
    }
