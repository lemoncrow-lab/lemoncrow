"""Linear-vs-per_agent benchmark reporter — LINEAR-05, TBEVAL-01, D-15..D-17.

Aggregates the raw cell JSON files produced by ``runner.run_cell`` into a
single report dict capturing per-cell aggregates, per-scenario linear-vs-
per_agent deltas, total savings split into cache-reuse vs minification
components (D-17), and threshold pass/fail flags against D-16 (>=30% cost
reduction, >=25% wall-time reduction at equal-or-better task success).

T-13-03 mitigation: scenarios whose ``expected_mode == "per_agent"`` are
excluded from the headline threshold check (the divergent scenario is
expected to favor per_agent — including it would falsely penalize linear).
T-13-04 mitigation: ``.tmp`` / ``.json.tmp`` files are skipped so partial
writes never poison the aggregate.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

# Match the pricing model used by runner._cell_totals_from_events so the
# reporter can attribute USD savings to cached tokens consistently.
_PRICE_IN = 3e-6
_PRICE_CACHE_READ = 0.3e-6


def _load_cells(raw_dir: Path) -> dict[str, dict[str, list[dict]]]:
    """Return ``{scenario_id: {mode: [cell_record, ...]}}``."""
    by_scenario: dict[str, dict[str, list[dict]]] = {}
    for path in sorted(raw_dir.glob("*.json")):
        if path.suffix == ".tmp" or path.name.endswith(".json.tmp"):
            continue
        try:
            rec = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        sid = rec.get("scenario_id") or path.stem.rsplit("__rep", 1)[0].rsplit("__", 1)[0]
        mode = rec.get("mode") or path.stem.rsplit("__rep", 1)[0].rsplit("__", 1)[-1]
        by_scenario.setdefault(sid, {}).setdefault(mode, []).append(rec)
    return by_scenario


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _aggregate_cell(records: list[dict]) -> dict[str, Any]:
    if not records:
        return {}

    def col(key: str, default: float = 0.0) -> list[float]:
        return [float(r.get(key, default) or default) for r in records]

    successes = [bool(r.get("task_success", False)) for r in records]
    return {
        "n_reps": len(records),
        "cost_usd": round(_mean(col("cost_usd")), 6),
        "wall_time_ms": round(_mean(col("wall_time_ms")), 3),
        "cache_read_tokens": round(_mean(col("cache_read_tokens")), 2),
        "cache_write_tokens": round(_mean(col("cache_write_tokens")), 2),
        "cache_hit_ratio": round(_mean(col("cache_hit_ratio")), 4),
        "minify_delta_tokens": round(_mean(col("minify_delta_tokens")), 2),
        "task_success_rate": round(sum(successes) / len(successes), 4),
    }


def _pct_reduction(linear: float, baseline: float) -> float:
    if baseline <= 0:
        return 0.0
    return round((baseline - linear) / baseline * 100.0, 4)


def compute_report(
    run_id: str,
    raw_dir: Path,
    *,
    scenarios_meta: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Aggregate raw cells into the report dict.

    Args:
        run_id: Free-form run identifier embedded in the report.
        raw_dir: Directory containing per-cell JSON files.
        scenarios_meta: Optional ``{scenario_id: expected_mode}`` map. If
            absent, ``expected_mode`` is read from each cell record and
            falls back to ``"linear"``. Scenarios with
            ``expected_mode == "per_agent"`` are excluded from the
            headline threshold check (T-13-03).

    Returns:
        Dict with keys: ``run_id``, ``cells``, ``deltas``,
        ``cache_savings``, ``minify_savings``, ``total_savings``,
        ``thresholds``.
    """
    raw_dir = Path(raw_dir)
    by_scenario = _load_cells(raw_dir)

    # ------------------------------------------------------------------
    # Per-cell aggregates.
    # ------------------------------------------------------------------
    cells: dict[str, dict[str, Any]] = {}
    expected_modes: dict[str, str] = dict(scenarios_meta or {})
    for sid, by_mode in by_scenario.items():
        cells[sid] = {}
        for mode, records in by_mode.items():
            cells[sid][mode] = _aggregate_cell(records)
            if sid not in expected_modes:
                exp = records[0].get("expected_mode")
                if exp:
                    expected_modes[sid] = exp

    # ------------------------------------------------------------------
    # Per-scenario deltas (linear vs per_agent).
    # ------------------------------------------------------------------
    deltas: dict[str, dict[str, Any]] = {}
    cache_saved_tokens = 0.0
    cache_saved_usd = 0.0
    minify_saved_tokens = 0.0

    for sid, by_mode in cells.items():
        lin = by_mode.get("linear")
        per = by_mode.get("per_agent")
        if not lin or not per:
            continue
        cost_red = _pct_reduction(lin["cost_usd"], per["cost_usd"])
        wall_red = _pct_reduction(lin["wall_time_ms"], per["wall_time_ms"])
        success_ok = lin["task_success_rate"] >= per["task_success_rate"]
        deltas[sid] = {
            "expected_mode": expected_modes.get(sid, "linear"),
            "cost_reduction_pct": cost_red,
            "wall_time_reduction_pct": wall_red,
            "success_at_least_equal": success_ok,
            "linear_cost_usd": lin["cost_usd"],
            "per_agent_cost_usd": per["cost_usd"],
            "linear_wall_time_ms": lin["wall_time_ms"],
            "per_agent_wall_time_ms": per["wall_time_ms"],
            "linear_cache_hit_ratio": lin["cache_hit_ratio"],
        }

        # D-17 decomposition: cache-reuse savings come from linear cells'
        # cached input tokens (read at the discounted rate vs full prefill
        # at the per_agent baseline). Minify savings come from the linear
        # cell's ``minify_delta_tokens`` (read-context shrinkage).
        cache_saved_tokens += lin["cache_read_tokens"]
        # Approx USD attribution: tokens that would have been paid at
        # full input rate but were charged at the cache-read rate.
        cache_saved_usd += lin["cache_read_tokens"] * (_PRICE_IN - _PRICE_CACHE_READ)
        minify_saved_tokens += lin["minify_delta_tokens"]

    cache_savings = {
        "tokens": round(cache_saved_tokens),
        "usd": round(cache_saved_usd, 6),
    }
    minify_savings = {
        "tokens": round(minify_saved_tokens),
        "usd": round(minify_saved_tokens * _PRICE_IN, 6),
    }
    total_savings = {
        "tokens": cache_savings["tokens"] + minify_savings["tokens"],
        "usd": round(cache_savings["usd"] + minify_savings["usd"], 6),
    }

    # ------------------------------------------------------------------
    # Headline threshold check — restricted to scenarios where AUTO would
    # pick linear (T-13-03: per_agent-expected scenarios are excluded).
    # ------------------------------------------------------------------
    in_scope = [d for sid, d in deltas.items() if expected_modes.get(sid, "linear") != "per_agent"]
    if in_scope:
        avg_cost = round(_mean([d["cost_reduction_pct"] for d in in_scope]), 4)
        avg_wall = round(_mean([d["wall_time_reduction_pct"] for d in in_scope]), 4)
        success_ok_all = all(d["success_at_least_equal"] for d in in_scope)
    else:
        avg_cost = avg_wall = 0.0
        success_ok_all = False

    thresholds = {
        "cost_pass": bool(avg_cost >= 30.0),
        "wall_time_pass": bool(avg_wall >= 25.0),
        "success_at_least_equal": bool(success_ok_all),
        "avg_cost_reduction_pct": avg_cost,
        "avg_wall_time_reduction_pct": avg_wall,
        "scope_scenarios": sorted(sid for sid in deltas if expected_modes.get(sid, "linear") != "per_agent"),
    }

    return {
        "run_id": run_id,
        "cells": cells,
        "deltas": deltas,
        "cache_savings": cache_savings,
        "minify_savings": minify_savings,
        "total_savings": total_savings,
        "thresholds": thresholds,
    }
