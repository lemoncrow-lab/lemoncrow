#!/usr/bin/env python3
"""Compact per-task cost x correctness table for a Harbor run.

Saves you re-deriving the breakdown by hand. Shows, for every task the RUN
actually finished, its mean cost + pass rate next to an optional second run
(--vs) and the normalized baseline, sorted by cost delta (cheapest-vs-reference
first).

Usage:
    uv run python benchmarks/harbor/compare_run.py [RUN] [--vs OTHER] [--all]

RUN / OTHER accept an absolute path to a run dir, a bare timestamp under
benchmarks/harbor/results/atelier/, or 'latest' (RUN defaults to 'latest').
The cost-delta column compares RUN to OTHER when --vs is given, else to baseline.

Examples:
    # latest local run vs baseline
    uv run python benchmarks/harbor/compare_run.py
    # a worktree run vs the R10 run
    uv run python benchmarks/harbor/compare_run.py \
        /home/.../atelier-r6-repro/benchmarks/harbor/results/atelier/2026-07-04__09-28-38 \
        --vs 2026-07-04__03-33-24
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

HARBOR = Path(__file__).resolve().parent
RESULTS = HARBOR / "results" / "atelier"
BASELINE = HARBOR / "results" / "baseline" / "normalized_cost.csv"


def resolve(ref: str | None) -> Path:
    if ref in (None, "latest"):
        dirs = sorted(d for d in RESULTS.iterdir() if d.is_dir())
        if not dirs:
            raise SystemExit(f"no run dirs under {RESULTS}")
        return dirs[-1]
    p = Path(ref)
    if p.is_dir():
        return p
    if (RESULTS / ref).is_dir():
        return RESULTS / ref
    raise SystemExit(f"run dir not found: {ref}")


def load(run: Path) -> dict[str, list[tuple[float | None, float | None]]]:
    """{task: [(cost, reward), ...]} over every trial dir of the task."""
    per: dict[str, list[tuple[float | None, float | None]]] = defaultdict(list)
    for td in sorted(p for p in run.iterdir() if p.is_dir()):
        task = td.name.split("__")[0]
        rj, rt = td / "result.json", td / "verifier" / "reward.txt"
        cost = rew = None
        if rj.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                j = json.loads(rj.read_text())
                cost = (j.get("agent_result") or {}).get("cost_usd")
                rew = ((j.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        if rew is None and rt.exists():
            with contextlib.suppress(ValueError, OSError):
                rew = float(rt.read_text().strip())
        if cost is not None or rew is not None:
            per[task].append((cost, rew))
    return per


def mean_cost(d: dict, task: str) -> float | None:
    v = [c for c, _ in d.get(task, []) if c is not None]
    return st.mean(v) if v else None


def mean_rew(d: dict, task: str) -> float | None:
    v = [r for _, r in d.get(task, []) if r is not None]
    return st.mean(v) if v else None


def pass_str(d: dict, task: str) -> str:
    reps = [r for _, r in d.get(task, []) if r is not None]
    if not reps:
        return "  —"
    m = st.mean(reps)
    if len(reps) == 1:  # single rep -> pass/fail is clearer than 0%/100%
        return "  P" if m >= 0.999 else "  F"
    return f"{round(m * 100):3d}%"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", nargs="?", default="latest", help="run dir / timestamp / 'latest' (default)")
    ap.add_argument(
        "--vs",
        default="auto",
        help="second run to compare against: 'auto' = latest other run in results/atelier (default, "
        "typically the current-commit run), a path/timestamp, or 'none' for baseline-only",
    )
    ap.add_argument("--all", action="store_true", help="show every baseline task, not just those RUN finished")
    args = ap.parse_args()

    run = resolve(args.run)
    if args.vs == "none":
        other = None
    elif args.vs == "auto":
        cands = sorted(d for d in RESULTS.iterdir() if d.is_dir() and d.resolve() != run.resolve())
        other = cands[-1] if cands else None
    else:
        other = resolve(args.vs)
    R = load(run)
    OT = load(other) if other else {}
    base = {
        row["task"]: (float(row["cost_norm_blended_1h"]), float(row["pass_rate"]))
        for row in csv.DictReader(BASELINE.open())
    }

    tasks = sorted(base) if args.all else sorted(t for t in R if any(rw is not None for _, rw in R[t]))

    def delta(x: float | None, y: float | None) -> float | None:
        return None if (x is None or not y) else (x - y) / y * 100.0

    def c(v: float | None) -> str:
        return f"{v:5.2f}" if v is not None else "    —"

    def dfmt(x: float | None) -> str:
        return f"{x:+4.0f}%" if x is not None else "   —"

    header = f"{'task':<26} {'RUN$':>5} {'':>2}"
    if other:
        header += f" │ {'OTH$':>5} {'OTH':>4}"
    header += f" │ {'base$':>5} {'base':>4} │ "
    header += f"{'ΔvsO':>6} {'ΔvsB':>6}" if other else f"{'Δvsbase':>7}"
    print(f"RUN   = {run}")
    if other:
        print(f"OTHER = {other}")
    print(header)
    print("-" * len(header))

    rows = []
    for t in tasks:
        rc, oc, bc = mean_cost(R, t), mean_cost(OT, t), base.get(t, (None, None))[0]
        rows.append((t, rc, oc, bc, delta(rc, oc), delta(rc, bc)))
    sk = 4 if other else 5  # sort by Δ-vs-OTHER when --vs, else Δ-vs-baseline
    rows.sort(key=lambda r: (r[sk] is None, r[sk] if r[sk] is not None else 0))

    run_o = oth = run_b = bas = 0.0
    pr_o = po = pr_b = pb = 0.0
    no = nb = 0
    pass_n = pass_d = 0
    for t, rc, oc, bc, d_o, d_b in rows:
        line = f"{t[:26]:<26} {c(rc)} {pass_str(R, t):>2}"
        if other:
            line += f" │ {c(oc)} {pass_str(OT, t):>4}"
        bp = f"{round(base[t][1] * 100):3d}%" if t in base else "  —"
        line += f" │ {c(bc)} {bp:>4} │ "
        line += f"{dfmt(d_o):>6} {dfmt(d_b):>6}" if other else f"{dfmt(d_b):>7}"
        print(line)
        if rc is not None and oc is not None:
            run_o += rc
            oth += oc
        if rc is not None and bc is not None:
            run_b += rc
            bas += bc
        rr, orr, brr = mean_rew(R, t), mean_rew(OT, t), base.get(t, (None, None))[1]
        if rr is not None and orr is not None:
            pr_o += rr
            po += orr
            no += 1
        if rr is not None and brr is not None:
            pr_b += rr
            pb += brr
            nb += 1
        reps = [r for _, r in R.get(t, []) if r is not None]
        if reps:
            pass_n += sum(reps)
            pass_d += len(reps)

    def pct(x: float, n: float) -> str:
        return f"{x / n * 100:.0f}%" if n else "—"

    print("-" * len(header))
    print(f"done in RUN: {len(rows)} tasks | pass {pass_n:.0f}/{pass_d} ({pass_n / max(pass_d, 1) * 100:.0f}%)")
    if other and oth:
        print(
            f"cost vs OTHER   : RUN ${run_o:.2f} vs ${oth:.2f} ({(run_o - oth) / oth * 100:+.0f}%)"
            f"   pass {pct(pr_o, no)} vs {pct(po, no)}"
        )
    if bas:
        print(
            f"cost vs baseline: RUN ${run_b:.2f} vs ${bas:.2f} ({(run_b - bas) / bas * 100:+.0f}%)"
            f"   pass {pct(pr_b, nb)} vs {pct(pb, nb)}"
        )


if __name__ == "__main__":
    main()
