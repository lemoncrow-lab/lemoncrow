#!/usr/bin/env python3
"""Freeze a vanilla-Claude baseline from an existing graded benchmark run.

The baseline arm (vanilla Claude Code, SAME model as lemoncrow) is invariant to
LemonCrow source edits, so we measure it ONCE from a prior run's results.jsonl and
reuse it as the frozen reference. The self-optimization loop then only runs the
lemoncrow arm. Savings must come from token efficiency at the same model -- never
from routing to a cheaper model.

    uv run python benchmarks/self_optimization/freeze_baseline.py detect
    uv run python benchmarks/self_optimization/freeze_baseline.py freeze <run_dir> --out benchmarks/self_optimization/baseline/swe30.json

Per task we store rep-normalized figures so a reps=1 iterate run compares fairly
to a reps=3 baseline:
  cost_usd    = mean cost per rep
  solved_rate = fraction of reps that passed grading (falls back to `ok`)
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text().strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
    except json.JSONDecodeError:
        pass
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _solve_stats(runs: list[dict[str, Any]]) -> tuple[int, int]:
    """Return (solved_reps, denom) using grading, falling back to `ok`."""
    graded = [r.get("correct") for r in runs if r.get("correct") is not None]
    if graded:
        return sum(1 for g in graded if g), len(graded)
    oks = [bool(r.get("ok")) for r in runs]
    return sum(oks), (len(oks) or 1)


def detect() -> int:
    pattern = str(REPO_ROOT / "reports" / "benchmark" / "**" / "results.jsonl")
    for f in sorted(glob.glob(pattern, recursive=True)):
        rows = _read_rows(Path(f))
        if not rows:
            continue
        arms = sorted({str(r.get("arm")) for r in rows})
        tasks = {r.get("task") for r in rows}
        models = sorted({str(r.get("model")) for r in rows if r.get("model")})
        rel = Path(f).relative_to(REPO_ROOT)
        print(f"{len(rows):>4} rows | {len(tasks):>3} tasks | arms={arms} | models={models[:2]} | {rel}")
    return 0


def freeze(run_dir: str, out: str) -> int:
    rows = _read_rows(Path(run_dir) / "results.jsonl")
    base = [r for r in rows if r.get("arm") == "baseline"]
    if not base:
        print(f"no baseline-arm rows in {run_dir}/results.jsonl")
        return 1
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in base:
        by_task.setdefault(str(r.get("task")), []).append(r)
    models = sorted({str(r.get("model")) for r in base if r.get("model")})
    tasks_out: dict[str, dict[str, Any]] = {}
    for tid, runs in sorted(by_task.items()):
        costs = [float(r["cost_usd"]) for r in runs if r.get("cost_usd") is not None]
        solved_reps, denom = _solve_stats(runs)
        tasks_out[tid] = {
            "cost_usd": round(statistics.fmean(costs), 4) if costs else None,
            "solved_rate": round(solved_reps / denom, 4),
            "solved_reps": solved_reps,
            "reps": len(runs),
        }
    out_path = Path(out) if Path(out).is_absolute() else REPO_ROOT / out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_note": "FROZEN baseline = vanilla Claude Code, SAME model as lemoncrow. Off-limits to the loop. Re-freeze only if the model changes.",
        "source_run": str(Path(run_dir)),
        "model": models[0] if models else None,
        "tasks": tasks_out,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    resolved = sum(v["solved_rate"] for v in tasks_out.values())
    total = sum(v["cost_usd"] for v in tasks_out.values() if v["cost_usd"] is not None)
    print(
        f"wrote {out_path} | {len(tasks_out)} tasks | "
        f"baseline resolved={resolved:.1f}/{len(tasks_out)} (task-equiv) | baseline ${total:.2f}/pass"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Freeze a vanilla-Claude baseline from a prior graded run.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect", help="list candidate run dirs with results.jsonl")
    fp = sub.add_parser("freeze", help="freeze the baseline arm from a run dir")
    fp.add_argument("run_dir")
    fp.add_argument("--out", default="benchmarks/self_optimization/baseline/swe30.json")
    args = ap.parse_args()
    if args.cmd == "detect":
        return detect()
    return freeze(args.run_dir, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
