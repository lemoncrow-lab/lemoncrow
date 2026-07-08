#!/usr/bin/env python3
"""Read the latest run.py snapshot and print/write the savings table.

Savings = 1 - median(ultra_output_tokens) / median(baseline_output_tokens),
per prompt, from real Claude Code usage.output_tokens (ground truth, not a
tokenizer approximation). Reports median/mean/min/max like caveman's own
measure.py so a reader can see whether a number is solid or noisy.

Run: uv run python benchmarks/telegraphic/report.py [path/to/snapshot.json]
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"


def latest_snapshot() -> Path:
    candidates = sorted(RESULTS_DIR.glob("telegraphic_*.json"))
    if not candidates:
        raise SystemExit(f"no snapshot found under {RESULTS_DIR} -- run run.py first")
    return candidates[-1]


def tokens(calls: list[dict]) -> list[int]:
    return [c["output_tokens"] for c in calls if "output_tokens" in c]


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_snapshot()
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data["metadata"]
    rows = data["rows"]

    print(f"_Source: {path.name}_")
    print(f"_Model: {meta.get('model', '?')} - CLI: {meta.get('claude_cli_version', '?')}_")
    print(
        f"_n = {meta.get('n_prompts', len(rows))} prompts x {meta.get('trials', '?')} trial(s), real usage.output_tokens_"
    )
    print()
    print("| ID | Source | Baseline (tokens) | Ultra (tokens) | Saved |")
    print("|----|--------|-------------------:|----------------:|------:|")

    savings = []
    baseline_totals = []
    ultra_totals = []
    skipped = []
    for row in rows:
        b = tokens(row["baseline"])
        u = tokens(row["ultra"])
        if not b or not u:
            skipped.append(row["id"])
            continue
        bm, um = statistics.median(b), statistics.median(u)
        pct = round((1 - um / bm) * 100) if bm else 0
        savings.append(pct)
        baseline_totals.append(bm)
        ultra_totals.append(um)
        print(f"| {row['id']} | {row['source']} | {int(bm)} | {int(um)} | {pct}% |")

    if savings:
        avg_b, avg_u = statistics.mean(baseline_totals), statistics.mean(ultra_totals)
        print(f"| **Average** | | **{round(avg_b)}** | **{round(avg_u)}** | **{round(statistics.mean(savings))}%** |")
        print()
        print(
            f"_Median saving {statistics.median(savings)}%, range {min(savings)}%-{max(savings)}%, "
            f"stdev {statistics.pstdev(savings):.0f}pp across {len(savings)} prompts."
            + (f" Skipped (errored): {', '.join(skipped)}." if skipped else "")
            + "_"
        )
    elif skipped:
        print(f"\nAll prompts errored: {', '.join(skipped)}")


if __name__ == "__main__":
    main()
