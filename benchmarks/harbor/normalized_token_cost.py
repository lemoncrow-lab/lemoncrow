"""Normalized (same $/MTok pricing, real per-trial token splits) cost compare
between a LemonCrow Harbor run and the baseline, restricted to the tasks the
LemonCrow run actually completed.

Why this exists: comparing raw self-reported cost_usd on each side isn't
apples-to-apples by itself. LemonCrow's harness bills prompt-cache WRITES at
the 1-hour TTL rate (2x base input); baseline runs entirely on the 5-minute
TTL tier (1.25x base input) -- confirmed: baseline's
cache_creation.ephemeral_1h_input_tokens is 0 on every sampled step (see
results/baseline/README.md), and recomputing LemonCrow's own trials at the
1-hour rate reproduces its reported cost_usd to 1.0000x (recomputing at the
5-minute rate does not). Comparing raw cost_usd conflates "who sends
fewer/cheaper tokens" with "who chose the pricier cache tier" -- this script
splits those into two separate, explicit numbers.

Token definitions (the two sources label fields differently -- both are
reduced to the same 4-bucket split before pricing):
  - baseline `prompt_tokens` (Harbor Hub's scrape, results/baseline/*_turns.csv)
    = fresh_input + cache_write + cache_read (baseline's own README: "input_tokens
    is total input including cache").
  - LemonCrow `trajectory.json` final_metrics.total_prompt_tokens uses the same
    convention -- confirmed empirically: total_prompt_tokens - cache_read -
    cache_creation == the harness's own reported agent_result.n_input_tokens,
    exactly, on every trial checked.
  fresh_input = prompt_tokens - cache_write - cache_read, for both sides.

Price table ($/MTok, matches benchmarks/harbor/_token_anatomy.py):
  input $5.00 | output $25.00 | cache_read $0.50 (0.1x)
  cache_write 5-min tier $6.25 (1.25x) | cache_write 1-hour tier $10.00 (2x)
(If claude-opus-4-8 pricing or either side's cache-TTL config changes, update
these constants -- the sanity check below will flag a stale table.)

Sanity check baked in: recomputes each side's cost at ITS OWN real tier and
diffs against that side's actual reported cost_usd. If either is off by more
than SANITY_TOLERANCE_PCT, the price table or tier assumption is stale --
the script prints a loud warning rather than silently reporting a bad ratio.

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
    print(f"Baseline turns: {BASELINE_TURNS_CSV}")
    print()

    baseline_turns = load_baseline_turns()
    baseline_reported = load_baseline_reported_avg_cost()
    lc_trials = load_lemoncrow_trials(run_dir)

    tasks = sorted(set(lc_trials) & set(baseline_turns))
    missing = sorted(set(lc_trials) - set(baseline_turns))
    if missing:
        print(f"skipping {len(missing)} task(s) with no baseline turns.csv row: {', '.join(missing)}")
    if not tasks:
        raise SystemExit("no overlapping tasks between this run and baseline -- nothing to compare")

    print(
        f"{'task':<30} {'LC_fresh':>9} {'LC_cw':>8} {'LC_cr':>9} {'LC_out':>7} "
        f"{'BL_fresh':>9} {'BL_cw':>8} {'BL_cr':>9} {'BL_out':>7}  "
        f"{'LC@1h':>8} {'BL@5m':>8} {'LC@5m':>8} {'BL@1h':>8}"
    )

    lc_actual_total = bl_actual_total = lc_at_5m_total = bl_at_1h_total = 0.0
    lc_reported_total = bl_reported_total = 0.0
    lc_reported_n = 0

    for task in tasks:
        lc = lc_trials[task]
        bl = baseline_split(baseline_turns[task])

        lc_at_1h = price(lc, P_CW_1HOUR)  # LC's real tier
        lc_at_5m = price(lc, P_CW_5MIN)  # LC priced as if it ran baseline's tier
        bl_at_5m = price(bl, P_CW_5MIN)  # baseline's real tier
        bl_at_1h = price(bl, P_CW_1HOUR)  # baseline priced as if it ran LC's tier

        lc_actual_total += lc_at_1h
        bl_actual_total += bl_at_5m
        lc_at_5m_total += lc_at_5m
        bl_at_1h_total += bl_at_1h
        if lc["reported_cost"] is not None:
            lc_reported_total += lc["reported_cost"]
            lc_reported_n += 1
        bl_reported_total += baseline_reported.get(task, 0.0)

        print(
            f"{task:<30} {lc['fresh']:>9.0f} {lc['cw']:>8.0f} {lc['cr']:>9.0f} {lc['out']:>7.0f} "
            f"{bl['fresh']:>9.0f} {bl['cw']:>8.0f} {bl['cr']:>9.0f} {bl['out']:>7.0f}  "
            f"{lc_at_1h:>8.4f} {bl_at_5m:>8.4f} {lc_at_5m:>8.4f} {bl_at_1h:>8.4f}"
        )

    print()
    print("Totals, each priced at its own real cache-write tier (what each side actually pays):")
    print(
        f"  LC {money(lc_actual_total)}  vs  BL {money(bl_actual_total)}"
        f"   -> LC {(lc_actual_total / bl_actual_total - 1) * 100:+.1f}%"
    )
    print()
    print("Tier-neutral (same cache-write rate both sides -- isolates token composition from tier choice):")
    print(
        f"  both @ 5-min ($6.25/M write):  LC {money(lc_at_5m_total)}  vs  BL {money(bl_actual_total)}"
        f"   -> LC {(lc_at_5m_total / bl_actual_total - 1) * 100:+.1f}%"
    )
    print(
        f"  both @ 1-hour ($10/M write):   LC {money(lc_actual_total)}  vs  BL {money(bl_at_1h_total)}"
        f"   -> LC {(lc_actual_total / bl_at_1h_total - 1) * 100:+.1f}%"
    )
    print()
    print("Sanity check (recomputed @ own real tier vs each side's own reported cost_usd -- must be ~1.0x):")
    if lc_reported_n:
        lc_ratio = lc_actual_total / lc_reported_total
        flag = (
            ""
            if abs(lc_ratio - 1) * 100 <= SANITY_TOLERANCE_PCT
            else "  !! OUT OF TOLERANCE -- re-derive tier/prices before trusting the numbers above"
        )
        print(
            f"  LemonCrow ({lc_reported_n}/{len(tasks)} tasks priced): recomputed {money(lc_actual_total)} "
            f"vs reported {money(lc_reported_total)} ({lc_ratio:.4f}x){flag}"
        )
    else:
        print("  LemonCrow: no reported cost_usd available on any task -- skipped")
    bl_ratio = bl_actual_total / bl_reported_total
    flag = (
        ""
        if abs(bl_ratio - 1) * 100 <= SANITY_TOLERANCE_PCT
        else "  !! OUT OF TOLERANCE -- re-derive tier/prices before trusting the numbers above"
    )
    print(
        f"  baseline: recomputed {money(bl_actual_total)} vs reported {money(bl_reported_total)} ({bl_ratio:.4f}x){flag}"
    )


if __name__ == "__main__":
    main()
