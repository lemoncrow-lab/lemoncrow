#!/usr/bin/env python3
"""Compare the current LemonCrow Harbor run against normalized baseline costs."""

from __future__ import annotations

import csv
import json
from pathlib import Path

RUN_DIR = Path("/home/pankaj/Projects/leanchain/lemoncrow/benchmarks/harbor/results/lemoncrow/2026-07-07__02-24-29")
RUN_RESULT = RUN_DIR / "result.json"
BASELINE_CSV = Path("/home/pankaj/Projects/leanchain/lemoncrow/benchmarks/harbor/results/baseline/normalized_cost.csv")
COMPARISON_CSV = Path(
    "/home/pankaj/Projects/leanchain/lemoncrow/benchmarks/harbor/results/baseline/lemoncrow_vs_baseline_per_task.csv"
)


def money(value: float) -> str:
    return f"${value:.4f}"


def pct(value: float) -> str:
    return f"{value:.1f}%"


def baseline_correct(pass_rate: float, n_reps: int) -> str:
    n_pass = pass_rate * n_reps
    if abs(n_pass - round(n_pass)) < 1e-9:
        return f"{round(n_pass)}/{n_reps}"
    return f"{n_pass:.2f}/{n_reps}"


def load_baseline() -> dict[str, dict[str, str]]:
    with BASELINE_CSV.open(newline="") as f:
        return {row["task"]: row for row in csv.DictReader(f)}


def completed_trials() -> list[str]:
    run = json.loads(RUN_RESULT.read_text())
    evals = run["stats"]["evals"]
    if len(evals) != 1:
        raise RuntimeError(f"Expected exactly one eval entry, got {len(evals)}")

    stats = next(iter(evals.values()))
    rewards = stats["reward_stats"]["reward"]
    trials: list[str] = []
    for reward in ("1.0", "0.0"):
        trials.extend(rewards.get(reward, []))
    return sorted(trials)


def write_comparison_csv(lemoncrow_by_task: dict[str, tuple[str, str]]) -> None:
    """Refresh lemoncrow_resolved/lemoncrow_cost in the CSV from the current run.

    Leaves baseline_* columns untouched -- re-blend those afterwards with
    normalize_baseline_cost.py, which reads lemoncrow_cost back out of this file.
    save_pct is cleared here since it is stale until that re-blend runs.
    """
    with COMPARISON_CSV.open(newline="") as f:
        existing = list(csv.DictReader(f))
    for row in existing:
        resolved, cost = lemoncrow_by_task.get(row["task"], (row["lemoncrow_resolved"], row["lemoncrow_cost"]))
        row["lemoncrow_resolved"] = resolved
        row["lemoncrow_cost"] = cost
        row["save_pct"] = ""
    with COMPARISON_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(existing[0].keys()))
        writer.writeheader()
        writer.writerows(existing)
    print(f"wrote {COMPARISON_CSV} ({len(lemoncrow_by_task)} tasks refreshed)")


def main() -> None:
    baseline = load_baseline()
    rows = []
    lemoncrow_by_task: dict[str, tuple[str, str]] = {}

    lemoncrow_total_cost = 0.0
    lemoncrow_total_correct = 0.0
    baseline_total_cost = 0.0
    baseline_total_expected_correct = 0.0

    for trial_name in completed_trials():
        trial_result = RUN_DIR / trial_name / "result.json"
        trial = json.loads(trial_result.read_text())

        task = trial["task_id"]["name"]
        lemoncrow_correct = float(trial["verifier_result"]["rewards"]["reward"])

        base = baseline[task]
        n_reps = int(base["n_reps"])
        pass_rate = float(base["pass_rate"])
        baseline_cost = float(base["cost_norm_blended_1h"])

        cost_value = trial["agent_result"].get("cost_usd")
        if cost_value is None:
            lemoncrow_by_task[task] = ("no data" if lemoncrow_correct < 0.5 else "pass", "")
            lemoncrow_total_correct += lemoncrow_correct
            baseline_total_expected_correct += pass_rate
            rows.append(
                {
                    "task": task,
                    "lemoncrow": f"{int(lemoncrow_correct)}/1, timed out",
                    "baseline": f"{baseline_correct(pass_rate, n_reps)}, {money(baseline_cost)}",
                    "saving": "N/A",
                    "cheaper": "N/A",
                }
            )
            continue

        lemoncrow_cost = float(cost_value)
        saving = (baseline_cost - lemoncrow_cost) / baseline_cost * 100.0 if baseline_cost else 0.0

        lemoncrow_by_task[task] = ("pass" if lemoncrow_correct >= 0.5 else "fail", f"{lemoncrow_cost:.4f}")
        lemoncrow_total_cost += lemoncrow_cost
        lemoncrow_total_correct += lemoncrow_correct
        baseline_total_cost += baseline_cost
        baseline_total_expected_correct += pass_rate

        rows.append(
            {
                "task": task,
                "lemoncrow": f"{int(lemoncrow_correct)}/1, {money(lemoncrow_cost)}",
                "baseline": f"{baseline_correct(pass_rate, n_reps)}, {money(baseline_cost)}",
                "saving": pct(saving),
                "cheaper": "yes" if lemoncrow_cost < baseline_cost else "no",
            }
        )

    headers = ["task", "LemonCrow (correct / cost)", "baseline (correct / cost)", "saving %", "LemonCrow cheaper"]
    table_rows = [[row["task"], row["lemoncrow"], row["baseline"], row["saving"], row["cheaper"]] for row in rows]

    widths = [len(header) for header in headers]
    for row in table_rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row, strict=False)]

    def print_row(values: list[str]) -> None:
        print("  ".join(value.ljust(width) for value, width in zip(values, widths, strict=False)))

    print(f"Run:      {RUN_DIR}")
    print(f"Baseline: {BASELINE_CSV}")
    print()
    print_row(headers)
    print_row(["-" * width for width in widths])
    for row in table_rows:
        print_row(row)

    print()
    print("Totals")
    print(f"  LemonCrow:  {int(lemoncrow_total_correct)}/{len(rows)}, {money(lemoncrow_total_cost)}")
    print(f"  Baseline: {baseline_total_expected_correct:.2f}/{len(rows)} expected, {money(baseline_total_cost)}")
    print(f"  Total saving: {pct((baseline_total_cost - lemoncrow_total_cost) / baseline_total_cost * 100.0)}")
    if lemoncrow_total_correct:
        lemoncrow_cpc = lemoncrow_total_cost / lemoncrow_total_correct
        baseline_cpc = baseline_total_cost / baseline_total_expected_correct
        print(f"  Cost/correct: LemonCrow {money(lemoncrow_cpc)} vs baseline {money(baseline_cpc)}")
        print(f"  Cost/correct saving: {pct((baseline_cpc - lemoncrow_cpc) / baseline_cpc * 100.0)}")

    print()
    write_comparison_csv(lemoncrow_by_task)


if __name__ == "__main__":
    main()
