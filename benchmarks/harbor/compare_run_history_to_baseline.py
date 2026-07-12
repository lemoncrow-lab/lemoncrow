#!/usr/bin/env python3
"""Compare every detected Harbor run against the baseline, side by side.

Scans benchmarks/harbor/results/lemoncrow/<timestamp>/ for every run directory,
sorts them oldest -> newest, and prints one compact table: rows are the tasks
in the latest run, columns are the baseline plus every run (R1..Rn, oldest
first). The last column is each task's cost saving vs baseline for the
latest run only.

A single run directory can contain several trial dirs for the same task
(multiple reps). Each run's cell is the MEAN cost/reward across that task's
reps, so one unlucky rep no longer masquerades as the task's cost.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import TypedDict

RESULTS_DIR = Path(__file__).parent / "results" / "lemoncrow"
BASELINE_CSV = Path(__file__).parent / "results" / "baseline" / "normalized_cost.csv"


class TaskResult(TypedDict):
    status: str
    cost: float | None
    reward: float | None
    n_reps: int
    n_ok: int


def parse_trial(result_json: Path) -> tuple[str, float | None, float | None]:
    """Return (status, cost, reward) for a single trial's result.json."""
    trial = json.loads(result_json.read_text())
    agent_result = trial.get("agent_result") or {}
    verifier_result = trial.get("verifier_result") or {}
    cost = agent_result.get("cost_usd")
    reward = (verifier_result.get("rewards") or {}).get("reward")

    if not verifier_result and not agent_result and trial.get("exception_info") is not None:
        status = "errored"
    elif reward is not None and cost is None:
        status = "timeout"
    elif reward is not None and cost is not None:
        status = "ok"
    else:
        status = "errored"
    return status, cost, reward


def load_run(run_dir: Path) -> dict[str, TaskResult]:
    """Return {task_name: aggregated TaskResult} for one run directory.

    A run dir can hold several trial dirs for the same task (multiple reps).
    Aggregate them: cost is the mean over priced (``ok``) reps, reward the mean
    over reps that produced a reward (``ok`` or ``timeout``). Last-wins over the
    alphabetically-last rep is exactly the bug this replaces.
    """
    reps: dict[str, list[tuple[str, float | None, float | None]]] = {}
    for trial_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        task = trial_dir.name.rsplit("__", 1)[0]
        result_json = trial_dir / "result.json"
        if not result_json.exists():
            # no result.json yet -> still running (or was, when this run was live)
            reps.setdefault(task, []).append(("running", None, None))
            continue
        reps.setdefault(task, []).append(parse_trial(result_json))

    per_task: dict[str, TaskResult] = {}
    for task, trials in reps.items():
        ok_costs = [c for s, c, _ in trials if s == "ok" and c is not None]
        rewards = [r for s, _, r in trials if s in ("ok", "timeout") and r is not None]
        if ok_costs:
            status = "ok"
        elif any(s == "timeout" for s, _, _ in trials):
            status = "timeout"
        elif any(s == "running" for s, _, _ in trials):
            status = "running"
        else:
            status = "errored"
        per_task[task] = {
            "status": status,
            "cost": sum(ok_costs) / len(ok_costs) if ok_costs else None,
            "reward": sum(rewards) / len(rewards) if rewards else None,
            "n_reps": len(trials),
            "n_ok": len(ok_costs),
        }
    return per_task


def load_baseline() -> dict[str, dict[str, str]]:
    with BASELINE_CSV.open(newline="") as f:
        return {row["task"]: row for row in csv.DictReader(f)}


def trunc(name: str, n: int = 16) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def main() -> None:
    run_dirs = sorted(d for d in RESULTS_DIR.iterdir() if d.is_dir())
    run_names = [d.name for d in run_dirs]
    if not run_names:
        raise RuntimeError(f"No run directories found under {RESULTS_DIR}")
    latest = run_names[-1]

    all_runs = {name: load_run(d) for name, d in zip(run_names, run_dirs, strict=True)}
    baseline_rows = load_baseline()
    all_tasks = sorted(baseline_rows.keys())

    def status_of(run: str, task: str) -> str | None:
        v = all_runs[run].get(task)
        return v["status"] if v is not None else None

    def cell(run: str, task: str) -> str:
        v = all_runs[run].get(task)
        if v is None:
            return "."
        status = v["status"]
        if status == "ok":
            ratio = float(v["reward"] or 0.0)
            suffix = f"x{v['n_ok']}" if v["n_ok"] > 1 else ""
            return f"({ratio:.1f}){float(v['cost'] or 0.0):.2f}{suffix}"
        if status == "timeout":
            ratio = float(v["reward"] or 0.0)
            return f"({ratio:.1f})TO"
        if status == "errored":
            return "-"
        if status == "running":
            return "..." if run == latest else "-"
        return "."

    def saving_abs(task: str) -> float | None:
        v = all_runs[latest].get(task)
        if not v or v["status"] != "ok":
            return None
        cost = float(v["cost"] or 0.0)
        base_cost = float(baseline_rows[task]["cost_norm_blended_1h"])
        return base_cost - cost

    def saving_value(task: str) -> float | None:
        abs_saving = saving_abs(task)
        if abs_saving is None:
            return None
        base_cost = float(baseline_rows[task]["cost_norm_blended_1h"])
        if base_cost == 0:
            return None
        return abs_saving / base_cost * 100.0

    def saving_pct_str(saving: float | None) -> str:
        if saving is None:
            return "."
        return f"{'+' if saving >= 0 else ''}{saving:.0f}%"

    def saving_abs_str(saving: float | None) -> str:
        if saving is None:
            return "."
        return f"{'+' if saving >= 0 else ''}${saving:.2f}"

    # Sort by saving % descending (best savings first); tasks with no saving
    # figure (timed out / errored / not attempted / zero-cost baseline) sort last.
    sorted_tasks = sorted(
        all_tasks,
        key=lambda t: (saving_value(t) is None, -(saving_value(t) or 0.0)),
    )

    def baseline_str(task: str) -> str:
        row = baseline_rows[task]
        n_reps = int(row["n_reps"])
        pass_rate = float(row["pass_rate"])
        cost = float(row["cost_norm_blended_1h"])
        n_pass = pass_rate * n_reps
        frac = f"{round(n_pass)}/{n_reps}" if abs(n_pass - round(n_pass)) < 1e-9 else f"{n_pass:.1f}/{n_reps}"
        return f"{frac} ({pass_rate:.2f}) {cost:.2f}"

    col_ids = [f"R{i + 1}" + ("*" if r == latest else "") for i, r in enumerate(run_names)]
    headers = ["task", "base", *col_ids, "sav%", "sav$"]
    rows = []
    for t in sorted_tasks:
        row = (
            [trunc(t), baseline_str(t)]
            + [cell(r, t) for r in run_names]
            + [saving_pct_str(saving_value(t)), saving_abs_str(saving_abs(t))]
        )
        rows.append(row)

    widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def fmt(row: list[str]) -> str:
        return " ".join(c.ljust(w) for c, w in zip(row, widths, strict=True))

    print(fmt(headers))
    print(" ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))

    # Totals for the latest run only, same shape as compare_current_lemoncrow_to_baseline.py:
    # every task the latest run actually attempted (status ok or timeout) counts toward
    # correctness; cost sums only include tasks where lemoncrow reported a cost (i.e. not
    # timed out), so lemoncrow/baseline cost totals stay apples-to-apples.
    attempted = [t for t in all_tasks if status_of(latest, t) in ("ok", "timeout")]
    ok_tasks = [t for t in attempted if all_runs[latest][t]["status"] == "ok"]
    n_timeout = len(attempted) - len(ok_tasks)
    lemoncrow_total_correct = sum(float(all_runs[latest][t]["reward"] or 0.0) for t in attempted)
    baseline_total_expected_correct = sum(float(baseline_rows[t]["pass_rate"]) for t in attempted)
    lemoncrow_total_cost = sum(float(all_runs[latest][t]["cost"] or 0.0) for t in ok_tasks)
    baseline_total_cost = sum(float(baseline_rows[t]["cost_norm_blended_1h"]) for t in ok_tasks)

    n = len(attempted)
    total_saving = (
        (baseline_total_cost - lemoncrow_total_cost) / baseline_total_cost * 100.0 if baseline_total_cost else 0.0
    )

    print()
    print(
        f"Totals ({latest}, {n}/{len(all_tasks)} tasks attempted"
        + (f", {n_timeout} timed out" if n_timeout else "")
        + ")"
    )
    print(f"  LemonCrow:  {lemoncrow_total_correct:.2f}/{n}, ${lemoncrow_total_cost:.4f}")
    print(f"  Baseline: {baseline_total_expected_correct:.2f}/{n} expected, ${baseline_total_cost:.4f}")
    print(f"  Total saving: {total_saving:+.1f}%")
    if lemoncrow_total_correct:
        lemoncrow_cpc = lemoncrow_total_cost / lemoncrow_total_correct
        baseline_cpc = baseline_total_cost / baseline_total_expected_correct
        cpc_saving = (baseline_cpc - lemoncrow_cpc) / baseline_cpc * 100.0 if baseline_cpc else 0.0
        print(f"  Cost/correct: LemonCrow ${lemoncrow_cpc:.4f} vs baseline ${baseline_cpc:.4f}")
        print(f"  Cost/correct saving: {cpc_saving:+.1f}%")
    print("Correctness per run (of tasks that run actually attempted, i.e. status ok or timeout):")
    b_expected = sum(float(r["pass_rate"]) for r in baseline_rows.values())
    print(f"  baseline : {b_expected:.2f}/{len(all_tasks)} expected ({b_expected / len(all_tasks) * 100:.0f}%)")
    for i, r in enumerate(run_names):
        run_attempted = [t for t in all_tasks if status_of(r, t) in ("ok", "timeout")]
        n_correct = sum(float(all_runs[r][t]["reward"] or 0.0) for t in run_attempted)
        n = len(run_attempted)
        pct = f"{n_correct / n * 100:.0f}%" if n else "n/a"
        tag = "*" if r == latest else " "
        print(f"  R{i + 1}{tag}      : {n_correct:.1f}/{n} correct ({pct})")
    print()
    print(
        "R# (oldest->newest, *=latest/live): "
        + ", ".join(
            f"R{i + 1}={r.split('__')[1].replace('-', ':')[:5]}/{r.split('__')[0][5:].replace('-', '/')}"
            for i, r in enumerate(run_names)
        )
    )
    print(
        "base=n_pass/n_reps (pass-rate decimal) cost$   (r)=reward ratio for that run's attempt (1.0=correct, 0.0=incorrect) then cost$"
    )
    print("multi-rep runs: (r) & cost are the MEAN over that run's reps of the task; xN marks N priced reps averaged")
    print("sav%=(baseline-latest)/baseline  sav$=baseline-latest ($), += cheaper/saved, -=pricier")
    print("TO=timeout(no cost) -=incomplete .=not attempted/na ...=running now")


if __name__ == "__main__":
    main()
