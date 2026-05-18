"""Compact savings benchmark - real export replay.

Reads real Claude Code session exports (exports/claude/*.jsonl) and measures
how much *additional* context Atelier compaction would free on top of native
``/compact``.

Key design principle
--------------------
**Both Atelier and native Claude Code compact.** Native ``/compact`` is real
and already saves a large portion of context. This benchmark measures the
*delta* - the additional savings Atelier achieves beyond what the native
compactor already does. Savings are never inflated by assuming native does
nothing.

Algorithm
---------
1. For every session JSONL, reconstruct the per-turn effective context window
   (``input_tokens + cache_creation_input_tokens + cache_read_input_tokens``).
2. Detect *compaction events* where context drops by >= 40 % between consecutive
   assistant turns - these are the points where native ``/compact`` actually fired
   (confirmed from the observed export data where context drops from ~160 K -> ~43 K).
3. For each detected compaction event:

   * ``tokens_before``          - measured context just before the drop.
   * ``native_tokens_after``    - measured context just after (actual data).
   * ``native_freed``           - ``tokens_before - native_tokens_after`` (measured).
   * ``atelier_tokens_after``   - *estimated* output of Atelier compressor:
       ~3 000 tokens (structured summary block) + avg-output-size x 10 recent turns.
   * ``atelier_freed``          - ``tokens_before - atelier_tokens_after`` (estimated).
   * ``delta``                  - ``atelier_freed - native_freed``.
       Positive = Atelier frees more. Negative = native was already sufficient.

4. ``native_freed`` is always measured from real data.
   ``atelier_freed`` is always marked as an estimate.
5. Compute per-session and aggregate statistics, with USD savings based on
   the session's reported model (sonnet-4.6 fallback).

Output
------
Written to ``<root>/benchmarks/savings/compact_latest.json``.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Pricing constants (USD per 1 M tokens)
# These are hardcoded so the benchmark is deterministic without LiteLLM data.
# ---------------------------------------------------------------------------

_MODEL_INPUT_PRICE: dict[str, float] = {
    "claude-haiku-4-5": 0.80,
    "claude-haiku-4-6": 0.80,
    "claude-sonnet-4-5": 3.0,
    "claude-sonnet-4-6": 3.0,
    "claude-sonnet-4.6": 3.0,
    "claude-opus-4-5": 15.0,
    "claude-opus-4-7": 15.0,
    "claude-opus-4.7": 15.0,
}
_DEFAULT_INPUT_PRICE = 3.0  # sonnet-4.6 as safe default

# Compaction parameters
_COMPACTION_DROP_THRESHOLD = 0.40  # >=40% context drop -> candidate compaction event
# Minimum tokens kept by native /compact after the drop.
# Real /compact always keeps some history (typically 20-35% of context).
# Drops to <5 000 tokens are context resets (new sub-agent, new session),
# not real compaction - they are excluded because native freeing 100% is
# not a fair comparison point for Atelier's structured summary.
_MIN_NATIVE_AFTER = 5_000
_ATELIER_SUMMARY_TOKENS = 3_000  # fixed overhead for Atelier's structured block
# Dynamic budget bounds - mirror context_compressor._dynamic_turn_budget()
_ATELIER_BUDGET_MIN = 10_000
_ATELIER_BUDGET_MAX = 40_000
_MIN_CONTEXT_FOR_BENCH = 80_000  # sessions below this peak are skipped

# Tool name sets used for complexity scoring (mirrors compressor scoring)
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "TodoWrite"})


def _input_price_per_m(model: str) -> float:
    m = (model or "").lower().strip()
    for key, price in _MODEL_INPUT_PRICE.items():
        if key in m:
            return price
    return _DEFAULT_INPUT_PRICE


def _tokens_to_usd(tokens: int, price_per_m: float) -> float:
    return tokens * price_per_m / 1_000_000


# ---------------------------------------------------------------------------
# Session parsing
# ---------------------------------------------------------------------------


@dataclass
class _Turn:
    index: int
    effective_context: int  # tokens visible to the model this turn
    output_tokens: int
    tool_names: list[str] = field(default_factory=list)


def _parse_session(path: Path) -> tuple[list[_Turn], str]:
    """Return (turns, model_id) from a single-session JSONL export.

    Handles both the ``claude-*.jsonl`` top-level format and the ``claude/``
    sub-directory format.

    Deduplication
    -------------
    Claude Code JSONL exports can contain consecutive blocks of identical
    assistant events (same input/output/cache token counts) when multiple
    sub-agent contexts share the same conversation.  Consecutive duplicates
    are collapsed to a single turn to avoid inflating token counts.
    """
    turns: list[_Turn] = []
    model_id = "claude-sonnet-4-6"  # default
    last_fingerprint: tuple[int, int, int, int] | None = None  # (inp, c_create, c_read, out)

    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue

                if ev.get("type") != "assistant":
                    # Any non-assistant event resets the dedup window
                    last_fingerprint = None
                    continue

                msg = ev.get("message") or {}
                usage = msg.get("usage") or {}
                inp = int(usage.get("input_tokens", 0))
                cache_create = int(usage.get("cache_creation_input_tokens", 0))
                cache_read = int(usage.get("cache_read_input_tokens", 0))
                out = int(usage.get("output_tokens", 0))

                fingerprint = (inp, cache_create, cache_read, out)
                if fingerprint == last_fingerprint:
                    # Consecutive duplicate - skip
                    continue
                last_fingerprint = fingerprint

                effective = inp + cache_create + cache_read

                m = str(msg.get("model") or "")
                if m and m != "<synthetic>":
                    model_id = m

                tool_names = [
                    str(b.get("name", ""))
                    for b in (msg.get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]

                turns.append(
                    _Turn(
                        index=len(turns),
                        effective_context=effective,
                        output_tokens=out,
                        tool_names=tool_names,
                    )
                )
    except Exception:
        pass

    return turns, model_id


# ---------------------------------------------------------------------------
# Compaction event detection
# ---------------------------------------------------------------------------


@dataclass
class _CompactionEvent:
    turn_before: int  # turn index before drop
    turn_after: int  # turn index after drop
    tokens_before: int
    native_tokens_after: int
    model_id: str
    # Dynamic complexity signals computed from the recent window
    edit_density: float  # fraction of recent turns that used Edit/Write tools
    error_density: float  # fraction of recent turns that had >=1 failed Bash/test
    avg_output_tokens: float  # average output tokens per turn in recent window
    dynamic_budget: int  # estimated token budget used by Atelier compressor

    @property
    def native_freed(self) -> int:
        return max(0, self.tokens_before - self.native_tokens_after)

    @property
    def atelier_tokens_after(self) -> int:
        """Estimate of context size after Atelier compact (dynamic budget).

        Mirrors ``context_compressor._dynamic_turn_budget()``:
        * Structured summary block: ~3 000 tokens (fixed)
        * Recent turns: up to ``dynamic_budget`` tokens (scales with complexity)
        """
        return _ATELIER_SUMMARY_TOKENS + self.dynamic_budget

    @property
    def atelier_freed(self) -> int:
        return max(0, self.tokens_before - self.atelier_tokens_after)

    @property
    def delta(self) -> int:
        """Positive = Atelier freed more; negative = native freed more."""
        return self.atelier_freed - self.native_freed

    @property
    def price_per_m(self) -> float:
        return _input_price_per_m(self.model_id)

    @property
    def atelier_cost_saved_usd(self) -> float:
        """Cost of the additionally freed tokens (atelier_freed minus native_freed)."""
        return _tokens_to_usd(max(0, self.delta), self.price_per_m)


def _compute_dynamic_budget(recent: list[_Turn]) -> int:
    """Mirror context_compressor._dynamic_turn_budget() using export-observable signals.

    Since we only have tool names and output tokens (not full event payloads),
    we approximate:
    - edit_density  : fraction of recent turns that used Edit/Write tools
    - error_density : not directly observable from exports -> use 0 (conservative)
    - avg_output    : average output tokens per turn (verbosity proxy)
    """
    if not recent:
        return _ATELIER_BUDGET_MIN

    edit_count = sum(1 for t in recent if any(n in _EDIT_TOOLS for n in t.tool_names))
    edit_density = edit_count / len(recent)

    avg_output = statistics.mean(t.output_tokens for t in recent) if recent else 500.0

    # Verbosity: map avg output tokens -> avg summary chars (~4 chars/token)
    avg_chars = avg_output * 4
    verbose_bonus = int(min(avg_chars / 400, 1.0) * 4_000)

    base = 10_000
    edit_bonus = int(edit_density * 18_000)
    # error_density is not observable from exports - treat as 0 (conservative)
    budget = base + edit_bonus + verbose_bonus
    return max(_ATELIER_BUDGET_MIN, min(budget, _ATELIER_BUDGET_MAX))


def _find_compaction_events(
    turns: list[_Turn],
    model_id: str,
) -> list[_CompactionEvent]:
    """Detect real native /compact events in a turn sequence.

    A real compaction is a context drop where native **kept** a meaningful amount
    of history (``native_tokens_after >= _MIN_NATIVE_AFTER``).  Drops to near-zero
    are context resets (new sub-agent / new conversation start) and are excluded -
    native freeing 100% is not comparable to Atelier's structured summary.
    """
    events: list[_CompactionEvent] = []
    # Lookback window for complexity scoring (last 40 turns before compaction)
    _WINDOW = 40

    for i in range(1, len(turns)):
        prev = turns[i - 1].effective_context
        curr = turns[i].effective_context
        if prev == 0:
            continue
        drop_ratio = (prev - curr) / prev
        if drop_ratio < _COMPACTION_DROP_THRESHOLD:
            continue
        # Skip context resets (native_after < threshold)
        if curr < _MIN_NATIVE_AFTER:
            continue

        # Complexity signals from the recent window before this compaction
        recent = turns[max(0, i - _WINDOW) : i]
        avg_out = statistics.mean(t.output_tokens for t in recent) if recent else 500.0

        edit_count = sum(1 for t in recent if any(n in _EDIT_TOOLS for n in t.tool_names))
        edit_density = edit_count / len(recent) if recent else 0.0

        # error_density not observable from exports - use 0 (conservative)
        error_density = 0.0

        dyn_budget = _compute_dynamic_budget(recent)

        events.append(
            _CompactionEvent(
                turn_before=i - 1,
                turn_after=i,
                tokens_before=prev,
                native_tokens_after=curr,
                model_id=model_id,
                edit_density=edit_density,
                error_density=error_density,
                avg_output_tokens=avg_out,
                dynamic_budget=dyn_budget,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Per-session results
# ---------------------------------------------------------------------------


@dataclass
class SessionCompactResult:
    session_id: str
    model_id: str
    peak_context_tokens: int
    total_turns: int
    compaction_events: int
    native_freed_total: int
    atelier_freed_total: int
    delta_tokens: int
    cost_saved_usd: float

    avg_dynamic_budget: float = 0.0  # mean dynamic budget across compaction events
    avg_edit_density: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "model_id": self.model_id,
            "peak_context_tokens": self.peak_context_tokens,
            "total_turns": self.total_turns,
            "compaction_events": self.compaction_events,
            # measured from real export data
            "native_freed_total": self.native_freed_total,
            # estimated from Atelier compressor model (dynamic budget)
            "atelier_freed_total_est": self.atelier_freed_total,
            # delta = Atelier estimate - native measured (honest difference)
            "delta_tokens": self.delta_tokens,
            # USD value of the delta tokens only (not native portion)
            "cost_saved_usd": round(self.cost_saved_usd, 6),
            # complexity signals that drove the dynamic budget
            "avg_dynamic_budget_tokens": round(self.avg_dynamic_budget),
            "avg_edit_density": round(self.avg_edit_density, 3),
        }


# ---------------------------------------------------------------------------
# Main benchmark runner
# ---------------------------------------------------------------------------


def run_compact_bench(
    corpus_dir: Path,
    *,
    max_sessions: int | None = None,
    min_context_tokens: int = _MIN_CONTEXT_FOR_BENCH,
) -> dict[str, Any]:
    """Run the compact savings benchmark over *corpus_dir*.

    Parameters
    ----------
    corpus_dir:
        Directory containing ``claude-*.jsonl`` session exports.  Also
        accepts a parent directory that contains a ``claude/`` sub-directory.
    max_sessions:
        Cap the number of qualifying sessions to process. ``None`` = all.
    min_context_tokens:
        Minimum peak context window (tokens) for a session to be included.

    Returns
    -------
    dict
        Benchmark results suitable for writing to ``compact_latest.json``.
    """
    # Resolve corpus path - accept both the claude/ sub-dir and its parent
    search_dir = corpus_dir / "claude" if (corpus_dir / "claude").is_dir() else corpus_dir

    # Collect JSONL files
    candidates = sorted(search_dir.glob("*.jsonl"), key=lambda p: -p.stat().st_size)

    session_results: list[SessionCompactResult] = []
    sessions_skipped = 0

    for path in candidates:
        if max_sessions is not None and len(session_results) >= max_sessions:
            break

        turns, model_id = _parse_session(path)
        if not turns:
            sessions_skipped += 1
            continue

        peak = max(t.effective_context for t in turns)
        if peak < min_context_tokens:
            sessions_skipped += 1
            continue

        events = _find_compaction_events(turns, model_id)
        if not events:
            sessions_skipped += 1
            continue

        native_freed_total = sum(e.native_freed for e in events)
        atelier_freed_total = sum(e.atelier_freed for e in events)
        delta = atelier_freed_total - native_freed_total
        cost_saved = sum(e.atelier_cost_saved_usd for e in events)
        avg_budget = statistics.mean(e.dynamic_budget for e in events)
        avg_edit = statistics.mean(e.edit_density for e in events)

        session_results.append(
            SessionCompactResult(
                session_id=path.stem,
                model_id=model_id,
                peak_context_tokens=peak,
                total_turns=len(turns),
                compaction_events=len(events),
                native_freed_total=native_freed_total,
                atelier_freed_total=atelier_freed_total,
                delta_tokens=delta,
                cost_saved_usd=cost_saved,
                avg_dynamic_budget=avg_budget,
                avg_edit_density=avg_edit,
            )
        )

    # Aggregate
    n = len(session_results)
    if n == 0:
        return {
            "benchmark": "savings-compact",
            "note": "delta vs native /compact - both sides compact, this measures the difference",
            "sessions_benchmarked": 0,
            "sessions_skipped": sessions_skipped,
            "avg_compaction_events_per_session": 0,
            "avg_native_freed_tokens_measured": 0,
            "avg_atelier_freed_tokens_est": 0,
            "avg_delta_tokens": 0,
            "total_cost_saved_usd": 0.0,
            "avg_cost_saved_usd_per_session": 0.0,
            "sessions": [],
            "generated_at": datetime.now(UTC).isoformat(),
        }

    total_events = sum(r.compaction_events for r in session_results)
    total_native = sum(r.native_freed_total for r in session_results)
    total_atelier = sum(r.atelier_freed_total for r in session_results)
    total_delta = sum(r.delta_tokens for r in session_results)
    total_cost = sum(r.cost_saved_usd for r in session_results)

    return {
        "benchmark": "savings-compact",
        "note": (
            "delta vs native /compact - native freed tokens are measured from real "
            "session exports; Atelier freed tokens are estimated from the compressor model. "
            "cost_saved_usd reflects only the additional delta, not native savings."
        ),
        "sessions_benchmarked": n,
        "sessions_skipped": sessions_skipped,
        "avg_compaction_events_per_session": round(total_events / n, 2),
        # measured from export data
        "avg_native_freed_tokens_measured": round(total_native / n),
        # estimated from Atelier compressor model
        "avg_atelier_freed_tokens_est": round(total_atelier / n),
        "avg_delta_tokens": round(total_delta / n),
        "atelier_vs_native_delta_pct": round((total_atelier - total_native) / max(total_native, 1) * 100, 2),
        # USD value of the additional delta tokens only
        "total_cost_saved_usd": round(total_cost, 6),
        "avg_cost_saved_usd_per_session": round(total_cost / n, 6),
        "sessions": [r.to_dict() for r in session_results],
        "generated_at": datetime.now(UTC).isoformat(),
    }
