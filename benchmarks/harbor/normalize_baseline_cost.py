#!/usr/bin/env python3
"""Regenerate baseline cost normalization at LemonCrow's REAL cost model.

tbench.ai displays costs with cache tokens billed at $0, while LemonCrow's own
Harbor runs pay the real bill: 1h ephemeral cache writes at 2x the input rate
plus cache reads. Comparing a real bill against a cache-free number always
reads against LemonCrow, so the baseline is re-priced here.

tbench exposes only a single combined cache figure (read+write) per trial, so
the cache term uses a blended $/M rate: the cache-WRITE share is measured
(token-weighted) from every LemonCrow Harbor trial's claude-run.json usage
report and priced at the 1h write rate; the remainder is priced as reads.

Outputs (written into results/baseline/):
  normalized_cost.csv              per-task baseline re-priced
  lemoncrow_vs_baseline_per_task.csv baseline columns re-blended; the lemoncrow
                                   columns (resolved/cost) are preserved from
                                   the existing file (they are real bills)

Usage: uv run python benchmarks/harbor/normalize_baseline_cost.py
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE_DIR = HERE / "results" / "baseline"
LEMONCROW_RESULTS = HERE / "results" / "lemoncrow"
PER_TRIAL_CSV = BASELINE_DIR / "tbench_opus48_claudecode_2.1.152_tasks.csv"
COMPARISON_CSV = BASELINE_DIR / "lemoncrow_vs_baseline_per_task.csv"
NORMALIZED_CSV = BASELINE_DIR / "normalized_cost.csv"

# $/M-token -- LemonCrow's real cost model (Opus 4.8, 1h ephemeral cache).
INPUT_RATE = 5.0
OUTPUT_RATE = 25.0
CACHE_READ_RATE = 0.5
CACHE_WRITE_1H_RATE = 10.0  # 1h ephemeral writes bill at 2x input


def measure_write_share() -> tuple[float, int]:
    """Token-weighted cache-write share across every LemonCrow Harbor trial."""
    write = read = trials = 0
    for path in sorted(LEMONCROW_RESULTS.glob("*/*/agent/claude-run.json")):
        usage = None
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if row.get("type") == "result":
                        usage = row.get("usage") or {}
        except (OSError, json.JSONDecodeError):
            continue
        if not usage:
            continue
        w = int(usage.get("cache_creation_input_tokens") or 0)
        r = int(usage.get("cache_read_input_tokens") or 0)
        if w + r == 0:
            continue
        write += w
        read += r
        trials += 1
    if write + read == 0:
        raise SystemExit(f"no LemonCrow trial usage found under {LEMONCROW_RESULTS}")
    return write / (write + read), trials


def _f(value: str) -> float | None:
    value = value.strip()
    return float(value) if value else None


def load_trials() -> dict[str, list[dict[str, float | None]]]:
    """Per-task trial rows: tokens/cost as floats, blank cells as None."""
    by_task: dict[str, list[dict[str, float | None]]] = defaultdict(list)
    with open(PER_TRIAL_CSV, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            by_task[row["task"]].append(
                {
                    "pass": 1.0 if row["result"].strip() == "pass" else 0.0,
                    "input": _f(row["input_tokens"]),
                    "output": _f(row["output_tokens"]),
                    "cache": _f(row["cache_tokens"]),
                    "cost": _f(row["cost_usd"]),
                }
            )
    return dict(by_task)


def corrected_cost(input_tokens: float, output_tokens: float, cache_tokens: float, blend: float) -> float:
    """Re-price one trial at LemonCrow's cost model (cache via the blended rate)."""
    fresh = max(0.0, input_tokens - cache_tokens)
    return (fresh * INPUT_RATE + output_tokens * OUTPUT_RATE + cache_tokens * blend) / 1e6


def write_normalized(by_task: dict[str, list[dict[str, float | None]]], blend: float) -> list[str]:
    rows = []
    for task in sorted(by_task):
        trials = by_task[task]
        with_data = [t for t in trials if t["input"] is not None]
        n = len(trials)
        pass_rate = sum(float(t["pass"]) for t in trials) / n

        def avg(key: str, data: list[dict[str, float | None]] = with_data) -> float:
            return sum(float(t[key] or 0.0) for t in data) / len(data) if data else 0.0

        avg_in, avg_out, avg_cache = avg("input"), avg("output"), avg("cache")
        avg_fresh = max(0.0, avg_in - avg_cache)
        costs = [t["cost"] for t in trials if t["cost"] is not None]
        raw = round(sum(costs) / len(costs), 4) if costs else ""
        read_only = round((avg_fresh * INPUT_RATE + avg_out * OUTPUT_RATE + avg_cache * CACHE_READ_RATE) / 1e6, 4)
        blended = round((avg_fresh * INPUT_RATE + avg_out * OUTPUT_RATE + avg_cache * blend) / 1e6, 4)
        rows.append(
            f"{task},{n},{pass_rate:g},{avg_in:.0f},{avg_fresh:.0f},{avg_cache:.0f},{avg_out:.0f},"
            f"{raw},{read_only},{blended}"
        )
    header = (
        "task,n_reps,pass_rate,avg_input_tokens,avg_fresh_input_tokens,avg_cache_tokens,"
        "avg_output_tokens,cost_raw_tbench_cache_free,cost_norm_read_0p5,cost_norm_blended_1h"
    )
    NORMALIZED_CSV.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return rows


def rewrite_comparison(by_task: dict[str, list[dict[str, float | None]]], blend: float) -> list[dict[str, str]]:
    """Re-blend the baseline columns; keep the lemoncrow columns (real bills)."""
    out: list[dict[str, str]] = []
    with open(COMPARISON_CSV, encoding="utf-8", newline="") as fh:
        existing = list(csv.DictReader(fh))
    for row in existing:
        trials = [t for t in by_task.get(row["task"], []) if t["input"] is not None]
        if not trials:
            out.append(row)
            continue
        rep_costs = [
            corrected_cost(float(t["input"] or 0), float(t["output"] or 0), float(t["cache"] or 0), blend)
            for t in trials
        ]
        baseline_avg = sum(rep_costs) / len(rep_costs)
        row["baseline_avg_cost_corrected"] = f"{baseline_avg:.4f}"
        row["baseline_rep_costs_corrected"] = "[" + ", ".join(f"{c:.2f}" for c in rep_costs) + "]"
        try:
            lemoncrow_cost = float(row["lemoncrow_cost"])
            row["save_pct"] = f"{(1 - lemoncrow_cost / baseline_avg) * 100:.1f}"
        except (ValueError, ZeroDivisionError):
            pass
        out.append(row)
    with open(COMPARISON_CSV, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(existing[0].keys()))
        writer.writeheader()
        writer.writerows(out)
    return out


def main() -> None:
    write_share, trials = measure_write_share()
    blend = write_share * CACHE_WRITE_1H_RATE + (1 - write_share) * CACHE_READ_RATE
    print(f"write share: {write_share:.4f} (token-weighted over {trials} LemonCrow trials)")
    print(f"blended cache rate: ${blend:.4f}/M  (1h write ${CACHE_WRITE_1H_RATE}/M, read ${CACHE_READ_RATE}/M)")

    by_task = load_trials()
    write_normalized(by_task, blend)
    print(f"wrote {NORMALIZED_CSV}")

    rows = rewrite_comparison(by_task, blend)
    print(f"wrote {COMPARISON_CSV}")

    matched = [r for r in rows if r.get("lemoncrow_cost") and r.get("baseline_avg_cost_corrected")]
    lemoncrow_total = sum(float(r["lemoncrow_cost"]) for r in matched)
    baseline_total = sum(float(r["baseline_avg_cost_corrected"]) for r in matched)
    print(
        f"matched {len(matched)} tasks: lemoncrow ${lemoncrow_total:.2f} vs baseline corrected "
        f"${baseline_total:.2f} ({lemoncrow_total / baseline_total:.2f}x)"
    )
    buckets = [("< $0.50", 0.0, 0.5), ("$0.50-$1.50", 0.5, 1.5), (">= $1.50", 1.5, float("inf"))]
    for label, lo, hi in buckets:
        rows_b = [r for r in matched if lo <= float(r["baseline_avg_cost_corrected"]) < hi]
        if not rows_b:
            continue
        b = sum(float(r["baseline_avg_cost_corrected"]) for r in rows_b) / len(rows_b)
        a = sum(float(r["lemoncrow_cost"]) for r in rows_b) / len(rows_b)
        print(
            f"  {label}: n={len(rows_b)} avg baseline ${b:.2f} avg lemoncrow ${a:.2f} delta {a - b:+.2f} ({a / b:.1f}x)"
        )
    more = sum(1 for r in matched if float(r["lemoncrow_cost"]) > float(r["baseline_avg_cost_corrected"]))
    print(f"  {more}/{len(matched)} tasks cost more on lemoncrow, {len(matched) - more} cost less")


if __name__ == "__main__":
    main()
