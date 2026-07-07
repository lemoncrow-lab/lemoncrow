#!/usr/bin/env python3
"""Fixed evaluation harness for the Atelier auto-improvement loop.

This is the *immutable* objective function. The improvement agent edits
``src/atelier/**`` (and tests), then runs this script to score the result.
Do NOT change the metric definitions casually -- comparability across
experiments depends on them staying fixed (this mirrors prepare.py / bench.py
in karpathy/autoresearch and jyotilakra92/auto-improving-kernel).

Objectives
----------
health (default, free)
    Hard gate ``correct`` = all fast tests pass. Then drive lint + type errors
    to zero: ``score = -(mypy_errors + ruff_issues)`` (0 is best, higher is
    better). No API spend.
mini (paid)
    Runs ``atelier benchmark mini --json`` and optimizes cost-per-accepted
    -patch. Spends API budget -- only run with explicit approval.

Usage
-----
    uv run python benchmarks/self_optimization/eval.py
    uv run python benchmarks/self_optimization/eval.py --objective mini --limit 5
    uv run python benchmarks/self_optimization/eval.py --json benchmarks/self_optimization/last.json \
        --log benchmarks/self_optimization/results.tsv --desc "tighten types in store"

Output: a grep-friendly ``key: value`` block between ``---`` fences, e.g.

    ---
    objective: health
    correct: True
    score: -3
    tests_passed: 1487
    tests_failed: 0
    mypy_errors: 1
    ruff_issues: 2
    eval_seconds: 92.4
    ---
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shlex
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_PYTEST_ARGS = ["-q", "-p", "no:cacheprovider", "-m", "not slow"]
LOG_COLUMNS = [
    "commit",
    "objective",
    "correct",
    "score",
    "eval_seconds",
    "status",
    "detail",
    "description",
]


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout)


def _last_int(matches: list[str]) -> int:
    return int(matches[-1]) if matches else 0


def objective_health(args: argparse.Namespace) -> dict[str, Any]:
    """Free, deterministic objective: tests green, lint+type errors -> 0."""
    metrics: dict[str, Any] = {}

    # --- tests: the hard correctness gate ---
    pytest_args = shlex.split(args.pytest_args) if args.pytest_args else DEFAULT_PYTEST_ARGS
    t = _run(["uv", "run", "pytest", *pytest_args], timeout=args.timeout)
    out = t.stdout + t.stderr
    passed = _last_int(re.findall(r"(\d+) passed", out))
    failed = _last_int(re.findall(r"(\d+) failed", out))
    errored = _last_int(re.findall(r"(\d+) errors?\b", out))
    metrics["tests_passed"] = passed
    metrics["tests_failed"] = failed + errored
    tests_ok = t.returncode == 0 and failed == 0 and errored == 0

    # --- mypy ---
    m = _run(
        ["uv", "run", "mypy", "--explicit-package-bases", "src/atelier"],
        timeout=args.timeout,
    )
    mout = m.stdout + m.stderr
    found = re.search(r"Found (\d+) error", mout)
    if found:
        mypy_errors = int(found.group(1))
    elif "Success" in mout:
        mypy_errors = 0
    else:
        mypy_errors = mout.count(": error:")
    metrics["mypy_errors"] = mypy_errors

    # --- ruff ---
    r = _run(
        [
            "uv",
            "run",
            "ruff",
            "check",
            "src",
            "benchmarks",
            "tests",
            "scripts",
            "integrations",
            "--output-format",
            "json",
        ],
        timeout=args.timeout,
    )
    try:
        ruff_issues = len(json.loads(r.stdout or "[]"))
    except json.JSONDecodeError:
        ruff_issues = -1
    metrics["ruff_issues"] = ruff_issues

    metrics["correct"] = tests_ok
    metrics["score"] = -(max(mypy_errors, 0) + max(ruff_issues, 0))
    return metrics


def objective_mini(args: argparse.Namespace) -> dict[str, Any]:
    """Paid objective: cost/quality from ``atelier benchmark mini``."""
    cmd = ["uv", "run", "atelier", "benchmark", "mini", "--json"]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    p = _run(cmd, timeout=args.timeout)
    data = _extract_json(p.stdout)

    metrics: dict[str, Any] = {}
    for key in ("cost_per_accepted_patch", "accepted_patch_rate", "trace_coverage_pct"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            metrics[key] = value
    cpap = metrics.get("cost_per_accepted_patch")
    apr = metrics.get("accepted_patch_rate")
    metrics["correct"] = p.returncode == 0 and isinstance(apr, (int, float)) and apr > 0
    metrics["score"] = -cpap if isinstance(cpap, (int, float)) and cpap > 0 else 0.0
    metrics["_raw"] = data
    return metrics


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def _read_knobs(path: str) -> dict[str, str]:
    """Read KEY=VALUE env-knob overrides (the loop's most-controlled experiment surface)."""
    p = _resolve(path)
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


@contextlib.contextmanager
def _inject_knobs(knobs: dict[str, str]):
    """Inject knobs into the benchmark `.env` cascade for one run, then restore.

    multiswe_run reads agent_env from the .env cascade and applies it LAST in the
    container (overriding incontainer defaults), so root `.env` is a safe override
    point for cost knobs (which are not otherwise set). Fully reversible.
    """
    if not knobs:
        yield
        return
    target = REPO_ROOT / ".env"
    backup = target.read_text() if target.exists() else None
    block = "\n# --- self-optimization knobs (transient) ---\n" + "\n".join(f"{k}={v}" for k, v in knobs.items()) + "\n"
    target.write_text((backup or "") + block)
    try:
        yield
    finally:
        if backup is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(backup)


def _read_task_ids(path: str) -> list[str]:
    ids: list[str] = []
    for raw in _resolve(path).read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            ids.append(line)
    return ids


def _load_baseline(path: str) -> dict[str, dict[str, Any]]:
    data = json.loads(_resolve(path).read_text())
    tasks = data.get("tasks", data) if isinstance(data, dict) else {}
    return {k: v for k, v in tasks.items() if isinstance(v, dict)}


def _read_results_jsonl(path: Path) -> list[dict[str, Any]]:
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


def _solve_rate(runs: list[dict[str, Any]]) -> float:
    """Fraction of reps that passed grading (falls back to `ok` when ungraded)."""
    graded = [r.get("correct") for r in runs if r.get("correct") is not None]
    if graded:
        return sum(1 for g in graded if g) / len(graded)
    oks = [bool(r.get("ok")) for r in runs]
    return (sum(oks) / len(oks)) if oks else 0.0


def objective_swe(args: argparse.Namespace) -> dict[str, Any]:
    """Paid objective: $ saved on the atelier arm vs a FROZEN baseline.

    Baseline = vanilla Claude Code, which is invariant to Atelier source edits,
    so we run ONLY the atelier arm and compare against the frozen reference.
    """
    tasks = _read_task_ids(args.tasks)
    baseline = _load_baseline(args.baseline)
    knobs = _read_knobs(args.knobs)
    out_dir = Path(args.out) if args.out else REPO_ROOT / "benchmarks" / "self_optimization" / "runs" / "last"

    cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "benchmarks.codebench.multiswe_run",
        "--suite",
        "swe-bench-verified",
        "--instances",
        *tasks,
        "-a",
        "atelier",
        "--reps",
        str(args.reps),
        "--model",
        args.model,
        "--jobs",
        str(args.jobs),
        "--timeout",
        str(args.run_timeout),
        "--out",
        str(out_dir),
    ]
    if args.no_grade:
        cmd.append("--no-grade")
    if args.resume:
        cmd.append("--resume")

    if args.dry_run:
        print("[dry-run] would run:\n  " + " ".join(cmd))
        if knobs:
            print("[dry-run] knobs:", ", ".join(f"{k}={v}" for k, v in knobs.items()))
        return {
            "tasks": len(tasks),
            "evaluated": 0,
            "baseline_covered": sum(1 for t in tasks if t in baseline),
            "savings_pct": 0.0,
            "correct": True,
            "target_met": False,
            "score": 0.0,
            "_planned_cmd": " ".join(cmd),
            "_knobs": knobs,
        }

    # The wrapper timeout must cover ALL agent runs + grading, not one step; scale it
    # by task count x reps (multiswe_run enforces the per-run timeout itself).
    wrapper_timeout = args.run_timeout * max(1, len(tasks)) * max(1, args.reps) + 900
    with _inject_knobs(knobs):
        _run(cmd, timeout=wrapper_timeout)
    atel = [r for r in _read_results_jsonl(out_dir / "results.jsonl") if r.get("arm") == "atelier"]

    per: dict[str, dict[str, Any]] = {}
    for tid in tasks:
        runs = [r for r in atel if r.get("task") == tid]
        base = baseline.get(tid) or {}
        costs = [float(r["cost_usd"]) for r in runs if r.get("cost_usd") is not None]
        per[tid] = {
            "atel_cost": statistics.fmean(costs) if costs else None,
            "atel_solve_rate": _solve_rate(runs) if runs else 0.0,
            "base_cost": base.get("cost_usd"),
            "base_solve_rate": base.get("solved_rate"),
        }

    def _pair(v: dict[str, Any]) -> bool:
        return v["atel_cost"] is not None and v["base_cost"] is not None

    paired = [v for v in per.values() if _pair(v)]
    base_total = sum(v["base_cost"] for v in paired)
    atel_total = sum(v["atel_cost"] for v in paired)
    saved = base_total - atel_total
    savings_pct = round((1 - atel_total / base_total) * 100, 1) if base_total else 0.0

    # Resolved is rep-normalized (sum of per-task solve rates) so a reps=1 iterate
    # run compares fairly to the reps=3 frozen baseline. Same model on both arms.
    base_resolved = sum(v["base_solve_rate"] for v in per.values() if v.get("base_solve_rate") is not None)
    atel_resolved = sum(v["atel_solve_rate"] for v in per.values())
    cost_regressions = [t for t, v in per.items() if _pair(v) and v["atel_cost"] > v["base_cost"]]
    reliability = [t for t, v in per.items() if (v.get("base_solve_rate") or 0.0) > (v.get("atel_solve_rate") or 0.0)]
    evaluated = sum(1 for v in per.values() if v["atel_cost"] is not None)

    # Goal: >=50% cheaper AND correctness same-or-more. Time is ignored.
    correctness_ok = atel_resolved >= base_resolved - 1e-9
    return {
        "tasks": len(tasks),
        "evaluated": evaluated,
        "savings_pct": savings_pct,
        "total_saved_usd": round(saved, 4),
        "base_cost_usd": round(base_total, 4),
        "atel_cost_usd": round(atel_total, 4),
        "base_resolved": round(base_resolved, 2),
        "atel_resolved": round(atel_resolved, 2),
        "reliability_regressions": len(reliability),
        "cost_regressions": len(cost_regressions),
        "correct": correctness_ok,
        "target_met": correctness_ok and savings_pct >= 50.0,
        "score": savings_pct,
        "_regressors": cost_regressions,
        "_reliability": reliability,
        "_per_task": per,
        "_out_dir": str(out_dir),
        "_knobs": knobs,
    }


OBJECTIVES = {"health": objective_health, "mini": objective_mini, "swe": objective_swe}


def _print_block(metrics: dict[str, Any]) -> None:
    order = ["objective", "correct", "score"]
    keys = order + [k for k in metrics if k not in order and not k.startswith("_")]
    print("---")
    for k in keys:
        if k in metrics:
            print(f"{k}: {metrics[k]}")
    print("---")


def _git_commit() -> str:
    try:
        return _run(["git", "rev-parse", "--short", "HEAD"], timeout=30).stdout.strip() or "-"
    except (subprocess.SubprocessError, OSError):
        return "-"


def _detail(metrics: dict[str, Any]) -> str:
    obj = metrics.get("objective")
    if obj == "health":
        return (
            f"tests={metrics.get('tests_passed', '?')}/{metrics.get('tests_failed', '?')} "
            f"mypy={metrics.get('mypy_errors', '?')} ruff={metrics.get('ruff_issues', '?')}"
        )
    if obj == "swe":
        return (
            f"savings={metrics.get('savings_pct', '?')}% "
            f"resolved={metrics.get('atel_resolved', '?')}/{metrics.get('base_resolved', '?')} "
            f"relreg={metrics.get('reliability_regressions', '?')} "
            f"target_met={metrics.get('target_met', '?')}"
        )
    if obj == "mini":
        return f"cpap={metrics.get('cost_per_accepted_patch', '?')} apr={metrics.get('accepted_patch_rate', '?')}"
    return ""


def _append_row(path: Path, metrics: dict[str, Any], status: str, desc: str) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\t".join(LOG_COLUMNS) + "\n")
    row = {
        "commit": _git_commit(),
        "objective": metrics.get("objective", ""),
        "correct": metrics.get("correct", ""),
        "score": metrics.get("score", ""),
        "eval_seconds": metrics.get("eval_seconds", ""),
        "status": status,
        "detail": _detail(metrics).replace("\t", " ").replace("\n", " "),
        "description": desc.replace("\t", " ").replace("\n", " "),
    }
    with path.open("a") as f:
        f.write("\t".join(str(row[c]) for c in LOG_COLUMNS) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fixed objective harness for the Atelier auto-improvement loop.")
    ap.add_argument("--objective", choices=sorted(OBJECTIVES), default="health")
    ap.add_argument("--pytest-args", default="", help="override pytest args (health objective)")
    ap.add_argument("--limit", type=int, default=None, help="case limit (mini objective)")
    ap.add_argument("--timeout", type=int, default=1800, help="per-step timeout in seconds")
    ap.add_argument("--json", dest="json_path", default=None, help="write full metrics JSON here")
    ap.add_argument("--log", dest="log_path", default=None, help="append a row to this results.tsv")
    ap.add_argument("--desc", default="", help="description recorded in the results log")
    ap.add_argument("--status", default=None, help="override status (keep/discard/crash)")
    ap.add_argument("--tasks", default="benchmarks/codebench/data/verified.txt", help="instance-id file (swe)")
    ap.add_argument(
        "--baseline", default="benchmarks/self_optimization/baseline/swe30.json", help="frozen baseline JSON (swe)"
    )
    ap.add_argument("--reps", type=int, default=1, help="reps per task (swe)")
    ap.add_argument(
        "--model",
        default="claude-opus-4-8",
        help="model for the atelier arm (swe). MUST match the frozen baseline model; "
        "never downgrade to a cheaper model -- savings must come from token efficiency.",
    )
    ap.add_argument("--jobs", type=int, default=1, help="parallel container runs (swe)")
    ap.add_argument("--out", default=None, help="benchmark run output dir (swe)")
    ap.add_argument("--no-grade", dest="no_grade", action="store_true", help="skip Docker grading; cost-only (swe)")
    ap.add_argument("--resume", action="store_true", help="reuse existing atelier patches (swe)")
    ap.add_argument(
        "--knobs", default="benchmarks/self_optimization/knobs.env", help="env-knob overrides for the atelier arm (swe)"
    )
    ap.add_argument("--run-timeout", type=int, default=1800, help="per-agent-run timeout passed to multiswe_run (swe)")
    ap.add_argument(
        "--dry-run", dest="dry_run", action="store_true", help="print planned command without running (swe)"
    )
    args = ap.parse_args()

    start = time.monotonic()
    crashed = False
    try:
        metrics = OBJECTIVES[args.objective](args)
    except subprocess.TimeoutExpired:
        metrics = {"correct": False, "score": float("-inf")}
        crashed = True
    metrics.setdefault("correct", False)
    metrics.setdefault("score", 0)
    metrics["objective"] = args.objective
    metrics["eval_seconds"] = round(time.monotonic() - start, 1)

    status = args.status or ("crash" if crashed else ("keep" if metrics["correct"] else "discard"))

    _print_block(metrics)

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(metrics, indent=2, default=str))
    if args.log_path:
        _append_row(Path(args.log_path), metrics, status, args.desc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
