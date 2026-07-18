#!/usr/bin/env python3
"""Compare the current LemonCrow Harbor run against the baseline's raw per-task costs.

Both sides use their own real, self-reported cost_usd -- no re-pricing. The baseline's
per-task average (avg_cost_usd) comes straight from
results/baseline/tbench_opus48_claudecode_2.1.205_per_task.csv.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_DIR = HERE / "results" / "lemoncrow" / "2026-07-14__13-44-30"
RUN_RESULT = RUN_DIR / "result.json"
BASELINE_CSV = HERE / "results" / "baseline" / "tbench_opus48_claudecode_2.1.205_per_task.csv"
COMPARISON_CSV = HERE / "results" / "baseline" / "lemoncrow_vs_baseline_per_task.csv"


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


def main() -> None:
    baseline = load_baseline()
    rows = []
    comparison_rows = []

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
        baseline_cost = float(base["avg_cost_usd"])

        cost_value = trial["agent_result"].get("cost_usd")
        if cost_value is None:
            lemoncrow_resolved = "no data" if lemoncrow_correct < 0.5 else "pass"
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
            comparison_rows.append(
                {
                    "task": task,
                    "baseline_resolved": baseline_correct(pass_rate, n_reps),
                    "lemoncrow_resolved": lemoncrow_resolved,
                    "baseline_avg_cost_raw": f"{baseline_cost:.4f}",
                    "lemoncrow_cost": "",
                    "save_pct": "",
                }
            )
            continue

        lemoncrow_cost = float(cost_value)
        saving = (baseline_cost - lemoncrow_cost) / baseline_cost * 100.0 if baseline_cost else 0.0

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
        comparison_rows.append(
            {
                "task": task,
                "baseline_resolved": baseline_correct(pass_rate, n_reps),
                "lemoncrow_resolved": "pass" if lemoncrow_correct >= 0.5 else "fail",
                "baseline_avg_cost_raw": f"{baseline_cost:.4f}",
                "lemoncrow_cost": f"{lemoncrow_cost:.4f}",
                "save_pct": f"{saving:.1f}",
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
    with COMPARISON_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(comparison_rows[0].keys()))
        writer.writeheader()
        writer.writerows(comparison_rows)
    print(f"wrote {COMPARISON_CSV} ({len(comparison_rows)} tasks)")


if __name__ == "__main__":
    main()
