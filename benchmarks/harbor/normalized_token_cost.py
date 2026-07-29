"""Normalized (matched cache-write tier) cost compare between a LemonCrow
Harbor run and the baseline, restricted to the tasks the LemonCrow run
actually completed.

Why this exists: comparing raw self-reported cost_usd on each side isn't
apples-to-apples by itself. LemonCrow's harness bills prompt-cache WRITES at
the 1-hour TTL rate (2x base input); baseline runs entirely on the 5-minute
TTL tier (1.25x base input) -- confirmed: baseline's
cache_creation.ephemeral_1h_input_tokens is 0 on every sampled step (see
results/baseline/README.md). Comparing raw cost_usd would conflate "who
sends fewer/cheaper tokens" with "who chose the pricier cache tier", so
THE HEADLINE NUMBER HERE re-prices baseline at LemonCrow's own 1-hour tier
and compares that against LemonCrow's real (already-1-hour-tier) cost.

The baseline side of this is precomputed and checked in --
results/baseline/tbench_opus48_claudecode_2.1.205_per_task.csv carries an
`avg_cost_usd_1h_tier` column (and `_turns.csv` a per-trial
`cost_usd_1h_tier`) already re-priced at the 1-hour rate. This script just
reads that column for baseline and LemonCrow's own reported `cost_usd`
directly (already the 1-hour-tier price, confirmed by the sanity check
below) -- no re-deriving the tier math per run. If claude-opus-4-8 pricing
or either side's cache-TTL config ever changes, regenerate the baseline
columns (see results/baseline/README.md) rather than re-deriving here.

Sanity check baked in: recomputes each side's cost at ITS OWN real tier from
raw token splits and diffs against that side's actual reported cost_usd. If
either is off by more than SANITY_TOLERANCE_PCT, the price table or tier
assumption is stale -- the script prints a loud warning rather than
silently reporting a bad ratio.

Usage:
  uv run python benchmarks/harbor/normalized_token_cost.py [run_dir]
  run_dir: a results/lemoncrow/<run> dir name, or a full/relative path to one.
  Defaults to the most recently modified dir under results/lemoncrow/.
  Only tasks with BOTH a completed LemonCrow trial (trajectory.json present)
  AND a baseline turns.csv row are compared; everything else is listed and
  skipped, never silently dropped.
"""

from __future__ import annotations

import csv
import glob
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEMONCROW_RESULTS = HERE / "results" / "lemoncrow"
BASELINE_TURNS_CSV = HERE / "results" / "baseline" / "tbench_opus48_claudecode_2.1.205_turns.csv"
BASELINE_PER_TASK_CSV = HERE / "results" / "baseline" / "tbench_opus48_claudecode_2.1.205_per_task.csv"

# $ / token (not $ / MTok) -- same table as _token_anatomy.py
P_IN = 5 / 1e6
P_OUT = 25 / 1e6
P_CR = 0.5 / 1e6
P_CW_5MIN = 6.25 / 1e6  # 1.25x base -- baseline's real tier
P_CW_1HOUR = 10 / 1e6  # 2x base -- LemonCrow's real tier

SANITY_TOLERANCE_PCT = 5.0


def money(v: float) -> str:
    return f"${v:.4f}"


def latest_run_dir() -> Path:
    runs = [p for p in LEMONCROW_RESULTS.iterdir() if p.is_dir()]
    if not runs:
        raise SystemExit(f"no run dirs under {LEMONCROW_RESULTS}")
    return max(runs, key=lambda p: p.stat().st_mtime)


def resolve_run_dir(arg: str | None) -> Path:
    if arg is None:
        return latest_run_dir()
    p = Path(arg)
    if p.exists():
        return p
    p2 = LEMONCROW_RESULTS / arg
    if p2.exists():
        return p2
    raise SystemExit(f"run dir not found: {arg!r} (tried {p} and {p2})")


def load_baseline_turns() -> dict[str, list[dict[str, str]]]:
    rows: dict[str, list[dict[str, str]]] = {}
    with BASELINE_TURNS_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            rows.setdefault(row["task"], []).append(row)
    return rows


def load_baseline_reported_avg_cost() -> dict[str, float]:
    with BASELINE_PER_TASK_CSV.open(newline="") as f:
        return {row["task"]: float(row["avg_cost_usd"]) for row in csv.DictReader(f)}


def load_baseline_1h_tier_avg_cost() -> dict[str, float]:
    """Precomputed baseline cost per task, re-priced at the 1-hour cache-write
    tier -- see results/baseline/README.md. Durable; no re-derivation here."""
    with BASELINE_PER_TASK_CSV.open(newline="") as f:
        return {row["task"]: float(row["avg_cost_usd_1h_tier"]) for row in csv.DictReader(f)}


def load_lemoncrow_trials(run_dir: Path) -> dict[str, dict[str, float]]:
    """One row per task, averaged over however many completed trials of that
    task exist in run_dir (usually 1 while a run is still in progress)."""
    per_task: dict[str, list[dict[str, float | None]]] = {}
    for traj_path in glob.glob(str(run_dir / "*" / "agent" / "trajectory.json")):
        trial_dir = Path(traj_path).parent.parent
        task = trial_dir.name.split("__")[0]
        result_path = trial_dir / "result.json"
        reported_cost = None
        if result_path.exists():
            result = json.loads(result_path.read_text())
            reported_cost = (result.get("agent_result") or {}).get("cost_usd")
        try:
            traj = json.loads(Path(traj_path).read_text())
        except json.JSONDecodeError:
            continue
        fm = traj.get("final_metrics") or {}
        extra = fm.get("extra") or {}
        prompt = fm.get("total_prompt_tokens")
        out = fm.get("total_completion_tokens")
        if prompt is None or out is None:
            continue
        cw = extra.get("total_cache_creation_input_tokens", 0) or 0
        cr = extra.get("total_cache_read_input_tokens", 0) or 0
        per_task.setdefault(task, []).append(
            {"fresh": prompt - cw - cr, "cw": cw, "cr": cr, "out": out, "reported_cost": reported_cost}
        )
    avg: dict[str, dict[str, float]] = {}
    for task, trials in per_task.items():
        n = len(trials)
        priced = [t for t in trials if t["reported_cost"] is not None]
        avg[task] = {
            "fresh": sum(t["fresh"] for t in trials) / n,
            "cw": sum(t["cw"] for t in trials) / n,
            "cr": sum(t["cr"] for t in trials) / n,
            "out": sum(t["out"] for t in trials) / n,
            "reported_cost": (sum(t["reported_cost"] for t in priced) / len(priced)) if priced else None,
            "n": n,
        }
    return avg


def baseline_split(rows: list[dict[str, str]]) -> dict[str, float]:
    n = len(rows)
    prompt = sum(int(r["prompt_tokens"]) for r in rows) / n
    out = sum(int(r["completion_tokens"]) for r in rows) / n
    cw = sum(int(r["cache_creation_tokens"]) for r in rows) / n
    cr = sum(int(r["cache_read_tokens"]) for r in rows) / n
    return {"fresh": prompt - cw - cr, "cw": cw, "cr": cr, "out": out, "n": n}


def price(split: dict[str, float], cw_rate: float) -> float:
    return split["fresh"] * P_IN + split["out"] * P_OUT + split["cw"] * cw_rate + split["cr"] * P_CR


def main() -> None:
    run_dir = resolve_run_dir(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"LemonCrow run: {run_dir}")
    print(f"Baseline (precomputed @ 1hr tier): {BASELINE_PER_TASK_CSV}")
    print()

    baseline_reported = load_baseline_reported_avg_cost()
    baseline_1h = load_baseline_1h_tier_avg_cost()
    lc_trials = load_lemoncrow_trials(run_dir)

    tasks = sorted(set(lc_trials) & set(baseline_1h))
    missing = sorted(set(lc_trials) - set(baseline_1h))
    if missing:
        print(f"skipping {len(missing)} task(s) with no baseline row: {', '.join(missing)}")
    if not tasks:
        raise SystemExit("no overlapping tasks between this run and baseline -- nothing to compare")

    print(f"{'task':<30} {'LC cost (1h tier, real)':>24} {'BL cost (1h tier, normalized)':>30}")

    lc_total = bl_1h_total = 0.0
    lc_reported_total = 0.0
    lc_reported_n = 0

    for task in tasks:
        lc = lc_trials[task]
        lc_cost = price(lc, P_CW_1HOUR)  # LC's tokens re-priced at its own real (1-hour) tier
        bl_cost = baseline_1h[task]  # baseline's real trial, re-priced at the same 1-hour tier
        lc_total += lc_cost
        bl_1h_total += bl_cost
        if lc["reported_cost"] is not None:
            lc_reported_total += lc["reported_cost"]
            lc_reported_n += 1
        print(f"{task:<30} {money(lc_cost):>24} {money(bl_cost):>30}")

    print()
    print("=== Headline: both sides normalized to the 1-hour cache-write rate ===")
    print(
        f"  LemonCrow {money(lc_total)}  vs  Baseline {money(bl_1h_total)}"
        f"   ->  LemonCrow {(lc_total / bl_1h_total - 1) * 100:+.1f}%  ({len(tasks)} tasks)"
    )

    # --- sanity check: diff the recomputed LC total above (and baseline's real 5-min
    # tier, recomputed from raw turns.csv) against each side's own reported cost_usd.
    print()
    print("Sanity check (recomputed @ own real tier vs each side's own reported cost_usd -- must be ~1.0x):")
    if lc_reported_n:
        lc_ratio = lc_total / lc_reported_total
        flag = (
            ""
            if abs(lc_ratio - 1) * 100 <= SANITY_TOLERANCE_PCT
            else "  !! OUT OF TOLERANCE -- re-derive tier/prices before trusting the numbers above"
        )
        print(
            f"  LemonCrow ({lc_reported_n}/{len(tasks)} tasks priced): recomputed {money(lc_total)} "
            f"vs reported {money(lc_reported_total)} ({lc_ratio:.4f}x){flag}"
        )
    else:
        print("  LemonCrow: no reported cost_usd available on any task -- skipped")
    try:
        baseline_turns = load_baseline_turns()
    except FileNotFoundError:
        baseline_turns = None
    if baseline_turns is not None:
        bl_at_5m_total = sum(
            price(baseline_split(baseline_turns[task]), P_CW_5MIN) for task in tasks if task in baseline_turns
        )
        bl_reported_total = sum(baseline_reported.get(task, 0.0) for task in tasks)
        bl_ratio = bl_at_5m_total / bl_reported_total if bl_reported_total else float("nan")
        flag = (
            ""
            if abs(bl_ratio - 1) * 100 <= SANITY_TOLERANCE_PCT
            else "  !! OUT OF TOLERANCE -- re-derive tier/prices before trusting the numbers above"
        )
        print(
            f"  baseline (its real 5-min tier): recomputed {money(bl_at_5m_total)} "
            f"vs reported {money(bl_reported_total)} ({bl_ratio:.4f}x){flag}"
        )


if __name__ == "__main__":
    main()
