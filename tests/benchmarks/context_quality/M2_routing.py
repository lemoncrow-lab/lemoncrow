"""M2 — Routing replay benchmark: recorded trace cost reduction.

De-circularized replacement for the removed synthetic M2 benchmark. This replay
consumes recorded ``model_recommendation`` traces instead of injecting arbitrary
``quality_gain`` values into the router.

What it measures directly
-------------------------
* Session-level replay over recorded routing traces.
* Estimated input-cost delta between the recorded baseline tier and the chosen
  cache-aware/sticky tier.
* Decision mix (baseline / sticky / cache_preserve / quality_gain).

What it reports only as a proxy
-------------------------------
* ``tier_downgrades_vs_baseline`` counts chosen tiers lower than the recorded
  baseline tier. This is *not* the same as a measured quality regression; it is
  surfaced separately until richer per-turn outcome coverage is available.

Usage:
    uv run pytest tests/benchmarks/context_quality/M2_routing.py -v -m slow
    ATELIER_ROUTE_TRACE_FILE=/path/to/live_savings_events.jsonl uv run pytest ... -v -m slow
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from atelier.core.capabilities.pricing import get_model_pricing
from atelier.core.foundation.paths import default_store_root

_TIER_MODELS: dict[str, str] = {
    "cheap": "claude-haiku-4-5",
    "medium": "claude-sonnet-4-6",
    "expensive": "claude-opus-4-7",
}
_TIER_RANK: dict[str, int] = {"cheap": 0, "medium": 1, "expensive": 2}
_MIN_SESSIONS = 50


@dataclass(frozen=True)
class RouteTrace:
    session_id: str
    chosen_tier: str
    baseline_tier: str
    estimated_input_tokens: int
    decision: str
    configured: bool


@dataclass(frozen=True)
class SessionReplay:
    session_id: str
    trace_count: int
    baseline_cost_usd: float
    chosen_cost_usd: float
    decisions: dict[str, int]
    tier_downgrades_vs_baseline: int
    configured_counts: dict[str, int]

    @property
    def cost_reduction_pct(self) -> float:
        if self.baseline_cost_usd <= 0:
            return 0.0
        return (self.baseline_cost_usd - self.chosen_cost_usd) / self.baseline_cost_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "trace_count": self.trace_count,
            "baseline_cost_usd": round(self.baseline_cost_usd, 6),
            "chosen_cost_usd": round(self.chosen_cost_usd, 6),
            "cost_reduction_pct": round(self.cost_reduction_pct, 6),
            "decisions": dict(self.decisions),
            "tier_downgrades_vs_baseline": self.tier_downgrades_vs_baseline,
            "configured_counts": dict(self.configured_counts),
        }


def _trace_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    env_path = os.environ.get("ATELIER_ROUTE_TRACE_FILE")
    if env_path:
        return Path(env_path).expanduser()
    primary = default_store_root() / "live_savings_events.jsonl"
    home_fallback = Path.home() / ".atelier" / "live_savings_events.jsonl"
    if primary.exists():
        return primary
    return home_fallback


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        nested = payload.get("route_decisions")
        if isinstance(nested, list):
            return [row for row in nested if isinstance(row, dict)]
    return []


def _iter_traces(rows: Iterable[dict[str, Any]]) -> Iterable[RouteTrace]:
    for row in rows:
        if row.get("kind") != "model_recommendation" or row.get("lever") != "model_routing":
            continue
        baseline_tier = str(row.get("baseline_tier") or "")
        chosen_tier = str(row.get("tier") or "")
        if baseline_tier not in _TIER_MODELS or chosen_tier not in _TIER_MODELS:
            continue
        estimated_input_tokens = int(row.get("estimated_input_tokens") or 0)
        if estimated_input_tokens <= 0:
            continue
        session_id = str(row.get("session_id") or "")
        if not session_id:
            continue
        yield RouteTrace(
            session_id=session_id,
            chosen_tier=chosen_tier,
            baseline_tier=baseline_tier,
            estimated_input_tokens=estimated_input_tokens,
            decision=str(row.get("decision") or "baseline"),
            configured=bool(row.get("configured")),
        )


def _input_cost_usd(tier: str, tokens: int) -> float:
    pricing = get_model_pricing(_TIER_MODELS[tier])
    return pricing.cost_usd(input_tokens=tokens)


def run_benchmark(
    trace_path: Path | None = None,
    *,
    max_sessions: int = _MIN_SESSIONS,
) -> dict[str, Any]:
    path = _trace_path(trace_path)
    rows = _load_rows(path)
    if not rows and trace_path is None:
        fallback = Path.home() / ".atelier" / "live_savings_events.jsonl"
        if fallback != path:
            path = fallback
            rows = _load_rows(path)
    traces_by_session: dict[str, list[RouteTrace]] = defaultdict(list)
    ordered_sessions: list[str] = []
    for trace in _iter_traces(rows):
        if trace.session_id not in traces_by_session:
            ordered_sessions.append(trace.session_id)
        traces_by_session[trace.session_id].append(trace)

    selected_sessions = ordered_sessions[:max_sessions]
    session_results: list[SessionReplay] = []
    decision_counts: Counter[str] = Counter()
    configured_counts: Counter[str] = Counter()
    tier_downgrades = 0
    total_baseline_cost = 0.0
    total_chosen_cost = 0.0
    total_traces = 0

    for session_id in selected_sessions:
        traces = traces_by_session[session_id]
        if not traces:
            continue
        session_decisions: Counter[str] = Counter()
        session_configured: Counter[str] = Counter()
        session_baseline = 0.0
        session_chosen = 0.0
        session_downgrades = 0
        for trace in traces:
            session_decisions[trace.decision] += 1
            session_configured["configured" if trace.configured else "fallback"] += 1
            session_baseline += _input_cost_usd(trace.baseline_tier, trace.estimated_input_tokens)
            session_chosen += _input_cost_usd(trace.chosen_tier, trace.estimated_input_tokens)
            if _TIER_RANK[trace.chosen_tier] < _TIER_RANK[trace.baseline_tier]:
                session_downgrades += 1

        decision_counts.update(session_decisions)
        configured_counts.update(session_configured)
        tier_downgrades += session_downgrades
        total_baseline_cost += session_baseline
        total_chosen_cost += session_chosen
        total_traces += len(traces)
        session_results.append(
            SessionReplay(
                session_id=session_id,
                trace_count=len(traces),
                baseline_cost_usd=session_baseline,
                chosen_cost_usd=session_chosen,
                decisions=dict(session_decisions),
                tier_downgrades_vs_baseline=session_downgrades,
                configured_counts=dict(session_configured),
            )
        )

    cost_reduction_pct = (
        (total_baseline_cost - total_chosen_cost) / total_baseline_cost if total_baseline_cost > 0 else 0.0
    )
    return {
        "benchmark": "routing-replay-traces",
        "trace_path": str(path),
        "source_format": path.suffix or "<none>",
        "sessions_available": len(traces_by_session),
        "sessions_benchmarked": len(session_results),
        "eligible_traces": total_traces,
        "baseline_cost_usd": round(total_baseline_cost, 6),
        "chosen_cost_usd": round(total_chosen_cost, 6),
        "cost_reduction_pct": round(cost_reduction_pct, 6),
        "decision_counts": dict(decision_counts),
        "configured_counts": dict(configured_counts),
        "tier_downgrades_vs_baseline": tier_downgrades,
        "notes": [
            "Cost estimates use recorded estimated_input_tokens and tier pricing only.",
            "tier_downgrades_vs_baseline is a proxy, not a measured quality regression.",
            "Replay is grouped per session to preserve trace boundaries before aggregation.",
        ],
        "sessions": [result.to_dict() for result in session_results],
    }


@pytest.mark.slow
def test_m2_routing_replay_cost_reduction() -> None:
    results = run_benchmark()
    if results["sessions_benchmarked"] < _MIN_SESSIONS:
        pytest.skip(
            f"Need at least {_MIN_SESSIONS} session traces for M2 replay; found {results['sessions_benchmarked']}"
        )

    print(json.dumps({k: v for k, v in results.items() if k != "sessions"}, indent=2, sort_keys=True))
    assert results["cost_reduction_pct"] >= 0.10, (
        "M2 benchmark FAIL: replayed routing traces did not reach the >=10% cost-reduction target. "
        f"Observed {results['cost_reduction_pct']:.1%} from {results['sessions_benchmarked']} session traces."
    )


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), indent=2, sort_keys=True))
