"""Routing savings benchmark - real export replay.

Reads real Claude Code session exports (exports/claude/*.jsonl) and measures
how much cost Atelier model routing would have saved by recommending a cheaper
model tier instead of the session's actual model.

Key design principle
--------------------
Savings are computed as ``actual_model_cost - recommended_tier_cost``.
If the session already used a cheap model (haiku), routing cannot save more -
``delta = 0`` for those calls. Savings are never inflated.

Algorithm
---------
1. For every session JSONL, parse all assistant turns.
2. For each turn that used real model API calls (non-synthetic):
   a. Record the actual model, input tokens, and the first tool call (if any).
   b. Run ``ModelRouter().score(tool_name, "", session_state)`` to get the
      recommended tier.
   c. Compute:
      * ``baseline_cost``    - tokens x actual session model price.
      * ``recommended_cost`` - tokens x recommended model price.
      * ``delta``            - ``baseline_cost - recommended_cost``.
      Positive = Atelier would have recommended a cheaper model.
      Zero or negative = actual model was already optimal or cheaper.
3. Aggregate per-session and globally.
4. Write to ``<root>/benchmarks/savings/routing_latest.json``.

Pricing is hardcoded so the benchmark is deterministic without LiteLLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.core.capabilities.model_routing.router import ModelRouter, ModelTier

# ---------------------------------------------------------------------------
# Pricing constants (USD per 1 M input tokens)
# ---------------------------------------------------------------------------

_TIER_MODELS: dict[ModelTier, str] = {
    "cheap": "claude-haiku-4-5",
    "medium": "claude-sonnet-4-6",
    "expensive": "claude-opus-4-7",
}

# Input price per 1 M tokens (non-cached fresh input + cache writes)
_TIER_INPUT_PRICE_PER_M: dict[ModelTier, float] = {
    "cheap": 0.80,
    "medium": 3.0,
    "expensive": 15.0,
}

# Output price per 1 M tokens
_TIER_OUTPUT_PRICE_PER_M: dict[ModelTier, float] = {
    "cheap": 4.0,
    "medium": 15.0,
    "expensive": 75.0,
}


# Map actual model names to their tier (for baseline cost)
_MODEL_TO_TIER: dict[str, ModelTier] = {
    "claude-haiku-4-5": "cheap",
    "claude-haiku-4-6": "cheap",
    "claude-sonnet-4-5": "medium",
    "claude-sonnet-4-6": "medium",
    "claude-sonnet-4.6": "medium",
    "claude-opus-4-5": "expensive",
    "claude-opus-4-7": "expensive",
    "claude-opus-4.7": "expensive",
}


def _model_tier(model: str) -> ModelTier:
    m = (model or "").lower().strip()
    for key, tier in _MODEL_TO_TIER.items():
        if key in m:
            return tier
    # Unknown model: default to medium
    return "medium"


def _tokens_to_usd(tokens: int, price_per_m: float) -> float:
    return tokens * price_per_m / 1_000_000


# ---------------------------------------------------------------------------
# Routing result per turn
# ---------------------------------------------------------------------------


def _turn_cost(input_tokens: int, output_tokens: int, tier: ModelTier) -> float:
    """Total USD cost for a turn (fresh input + output, not cache reads)."""
    return _tokens_to_usd(input_tokens, _TIER_INPUT_PRICE_PER_M[tier]) + _tokens_to_usd(
        output_tokens, _TIER_OUTPUT_PRICE_PER_M[tier]
    )


@dataclass
class _TurnRoutingResult:
    turn_index: int
    tool_name: str
    input_tokens: int
    output_tokens: int
    actual_tier: ModelTier
    recommended_tier: ModelTier
    baseline_cost_usd: float
    recommended_cost_usd: float

    @property
    def delta_usd(self) -> float:
        """Cost saved by routing (positive = cheaper recommendation)."""
        return max(0.0, self.baseline_cost_usd - self.recommended_cost_usd)

    @property
    def was_downtiered(self) -> bool:
        tier_rank: dict[ModelTier, int] = {"cheap": 0, "medium": 1, "expensive": 2}
        return tier_rank[self.recommended_tier] < tier_rank[self.actual_tier]


# ---------------------------------------------------------------------------
# Per-session result
# ---------------------------------------------------------------------------


@dataclass
class SessionRoutingResult:
    session_id: str
    actual_model: str
    actual_tier: ModelTier
    total_turns: int
    turns_with_tool_calls: int
    downtiered_turns: int
    total_baseline_cost_usd: float
    total_recommended_cost_usd: float
    cost_saved_usd: float
    by_tier: dict[str, int] = field(default_factory=dict)  # recommended tier -> count

    def to_dict(self) -> dict[str, Any]:
        downtiered_pct = (
            round(self.downtiered_turns / self.turns_with_tool_calls * 100, 1)
            if self.turns_with_tool_calls > 0
            else 0.0
        )
        return {
            "session_id": self.session_id,
            "actual_model": self.actual_model,
            "actual_tier": self.actual_tier,
            "total_turns": self.total_turns,
            "turns_with_tool_calls": self.turns_with_tool_calls,
            "downtiered_turns": self.downtiered_turns,
            "downtiered_pct": downtiered_pct,
            "total_baseline_cost_usd": round(self.total_baseline_cost_usd, 6),
            "total_recommended_cost_usd": round(self.total_recommended_cost_usd, 6),
            "cost_saved_usd": round(self.cost_saved_usd, 6),
            "by_tier": self.by_tier,
        }


# ---------------------------------------------------------------------------
# Session parser
# ---------------------------------------------------------------------------


def _parse_session_routing(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Parse a session JSONL and return (turn_data_list, dominant_model).

    Each entry in turn_data_list is:
    {
        "index": int,
        "input_tokens": int,    # fresh input (inp + cache_create)
        "output_tokens": int,
        "cache_read_tokens": int,
        "model": str,
        "tool_names": list[str],
        "synthetic": bool,
    }

    Deduplication
    -------------
    Consecutive assistant events with identical token counts are collapsed to
    one turn to avoid counting duplicate sub-agent context flushes.
    """
    turns: list[dict[str, Any]] = []
    dominant_model = "claude-sonnet-4-6"
    last_fingerprint: tuple[int, int, int, int] | None = None

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
                    continue
                last_fingerprint = fingerprint

                model = str(msg.get("model") or "")
                synthetic = model == "<synthetic>" or not model

                if model and not synthetic:
                    dominant_model = model

                tool_names = [
                    str(b.get("name", ""))
                    for b in (msg.get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]

                turns.append(
                    {
                        "index": len(turns),
                        # Use only fresh input tokens (not cache reads) for cost.
                        # Cache reads are already priced at a steep discount and
                        # are accounted for separately in billing - using the full
                        # effective context would inflate savings estimates.
                        "input_tokens": inp + cache_create,
                        "output_tokens": out,
                        "cache_read_tokens": cache_read,
                        "model": model,
                        "tool_names": tool_names,
                        "synthetic": synthetic,
                    }
                )
    except Exception:
        pass

    return turns, dominant_model


# ---------------------------------------------------------------------------
# Main benchmark runner
# ---------------------------------------------------------------------------


def run_routing_bench(
    corpus_dir: Path,
    *,
    max_sessions: int | None = None,
) -> dict[str, Any]:
    """Run the routing savings benchmark over *corpus_dir*.

    Parameters
    ----------
    corpus_dir:
        Directory containing ``claude-*.jsonl`` session exports.  Also
        accepts a parent directory that contains a ``claude/`` sub-directory.
    max_sessions:
        Cap the number of sessions to process. ``None`` = all.

    Returns
    -------
    dict
        Benchmark results suitable for writing to ``routing_latest.json``.
    """
    # Resolve corpus path
    search_dir = corpus_dir / "claude" if (corpus_dir / "claude").is_dir() else corpus_dir

    candidates = sorted(search_dir.glob("*.jsonl"), key=lambda p: -p.stat().st_size)

    router = ModelRouter(
        cheap_model=_TIER_MODELS["cheap"],
        medium_model=_TIER_MODELS["medium"],
        expensive_model=_TIER_MODELS["expensive"],
    )

    session_results: list[SessionRoutingResult] = []
    sessions_skipped = 0

    for path in candidates:
        if max_sessions is not None and len(session_results) >= max_sessions:
            break

        turns, dominant_model = _parse_session_routing(path)
        if not turns:
            sessions_skipped += 1
            continue

        # Skip sessions that are entirely synthetic (no real API calls)
        real_turns = [t for t in turns if not t["synthetic"]]
        if not real_turns:
            sessions_skipped += 1
            continue

        actual_tier = _model_tier(dominant_model)

        turn_results: list[_TurnRoutingResult] = []
        by_tier: dict[str, int] = {"cheap": 0, "medium": 0, "expensive": 0}

        prior_errors = 0
        for i, turn in enumerate(real_turns):
            tool_names = turn["tool_names"]
            # Use first tool name if available, else empty (will default medium)
            tool_name = tool_names[0] if tool_names else ""
            inp = turn["input_tokens"]
            out = turn.get("output_tokens", 0)

            if inp == 0 and out == 0:
                continue

            # Build session-phase signals from the preceding turns' tool calls.
            recent_tool_calls = [t["tool_names"][0] if t["tool_names"] else "" for t in real_turns[max(0, i - 10) : i]]
            rec = router.score(
                tool_name,
                "",  # no task text in replays - conservative estimate
                {
                    "prior_errors": prior_errors,
                    "turn_number": i,
                    "recent_tool_calls": recent_tool_calls,
                },
            )

            if rec is None:
                continue
            baseline_cost = _turn_cost(inp, out, actual_tier)
            rec_cost = _turn_cost(inp, out, rec.tier)

            turn_results.append(
                _TurnRoutingResult(
                    turn_index=turn["index"],
                    tool_name=tool_name or "(none)",
                    input_tokens=inp,
                    output_tokens=out,
                    actual_tier=actual_tier,
                    recommended_tier=rec.tier,
                    baseline_cost_usd=baseline_cost,
                    recommended_cost_usd=rec_cost,
                )
            )
            by_tier[rec.tier] = by_tier.get(rec.tier, 0) + 1

        if not turn_results:
            sessions_skipped += 1
            continue

        turns_with_tools = sum(1 for t in turn_results if t.tool_name != "(none)")
        downtiered = sum(1 for t in turn_results if t.was_downtiered)
        total_baseline = sum(t.baseline_cost_usd for t in turn_results)
        total_rec = sum(t.recommended_cost_usd for t in turn_results)
        cost_saved = sum(t.delta_usd for t in turn_results)

        session_results.append(
            SessionRoutingResult(
                session_id=path.stem,
                actual_model=dominant_model,
                actual_tier=actual_tier,
                total_turns=len(turn_results),
                turns_with_tool_calls=turns_with_tools,
                downtiered_turns=downtiered,
                total_baseline_cost_usd=total_baseline,
                total_recommended_cost_usd=total_rec,
                cost_saved_usd=cost_saved,
                by_tier=by_tier,
            )
        )

    # Aggregate
    n = len(session_results)
    if n == 0:
        return {
            "benchmark": "savings-routing",
            "note": "delta vs actual session model - only turns where Atelier recommends cheaper tier",
            "sessions_benchmarked": 0,
            "sessions_skipped": sessions_skipped,
            "total_turns_analyzed": 0,
            "total_downtiered_turns": 0,
            "downtiered_pct": 0.0,
            "total_baseline_cost_usd": 0.0,
            "total_recommended_cost_usd": 0.0,
            "total_cost_saved_usd": 0.0,
            "avg_cost_saved_usd_per_session": 0.0,
            "by_tier": {"cheap": 0, "medium": 0, "expensive": 0},
            "sessions": [],
            "generated_at": datetime.now(UTC).isoformat(),
        }

    total_turns = sum(r.total_turns for r in session_results)
    total_down = sum(r.downtiered_turns for r in session_results)
    total_baseline = sum(r.total_baseline_cost_usd for r in session_results)
    total_rec = sum(r.total_recommended_cost_usd for r in session_results)
    total_saved = sum(r.cost_saved_usd for r in session_results)
    by_tier_agg: dict[str, int] = {"cheap": 0, "medium": 0, "expensive": 0}
    for r in session_results:
        for tier, cnt in r.by_tier.items():
            by_tier_agg[tier] = by_tier_agg.get(tier, 0) + cnt

    return {
        "benchmark": "savings-routing",
        "note": (
            "savings = (actual_model_cost - recommended_model_cost) per turn. "
            "Only positive deltas are counted - never inflated by assuming model was worse."
        ),
        "sessions_benchmarked": n,
        "sessions_skipped": sessions_skipped,
        "total_turns_analyzed": total_turns,
        "total_downtiered_turns": total_down,
        "downtiered_pct": round(total_down / max(total_turns, 1) * 100, 1),
        "total_baseline_cost_usd": round(total_baseline, 6),
        "total_recommended_cost_usd": round(total_rec, 6),
        "total_cost_saved_usd": round(total_saved, 6),
        "avg_cost_saved_usd_per_session": round(total_saved / n, 6),
        "by_tier": by_tier_agg,
        "sessions": [r.to_dict() for r in session_results],
        "generated_at": datetime.now(UTC).isoformat(),
    }
