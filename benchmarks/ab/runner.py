"""A/B benchmark sweep runner — CLI entry point.

Usage:
    python -m ab.runner \\
        --suite terminalbench \\
        --tasks 10 \\
        --n 5 \\
        --models claude-sonnet-4-5 \\
        --modes on,off \\
        --out bench/runs/<run-id>/

Re-running with the same --out directory resumes where it left off (AB-03).
"""

import argparse
import datetime
import json
import os
from pathlib import Path

# terminalbench imports are deferred inside functions to keep startup fast


def load_suite_tasks(suite: str, n_tasks: int) -> list[str]:
    """Return the first n_tasks task IDs for the given suite."""
    if suite != "terminalbench":
        raise ValueError(f"Unknown suite: {suite!r}")
    tasks_yaml = Path(__file__).parent.parent / "terminalbench" / "tasks.yaml"
    import yaml

    with tasks_yaml.open() as fh:
        data = yaml.safe_load(fh)
    tasks: list[str] = data["tasks"]
    return tasks[:n_tasks]


def _load_suite_meta(suite: str) -> tuple[str, str]:
    """Return (dataset_name, dataset_version) for the given suite."""
    if suite != "terminalbench":
        return ("terminal-bench-core", "0.1.1")
    tasks_yaml = Path(__file__).parent.parent / "terminalbench" / "tasks.yaml"
    import yaml

    with tasks_yaml.open() as fh:
        data = yaml.safe_load(fh)
    ds = data.get("dataset", {})
    return (ds.get("name", "terminal-bench-core"), str(ds.get("version", "0.1.1")))


def write_config(out_dir: Path, config: dict) -> None:
    """Write config.json atomically to out_dir."""
    dest = out_dir / "config.json"
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2))
    os.replace(tmp, dest)


def run_cell(
    task_id: str,
    mode: str,
    rep: int,
    raw_dir: Path,
    trial_dir: Path,
    model: str,
    dataset_name: str,
    dataset_version: str,
) -> bool:
    """Run one A/B cell. Returns True on success or skip, False on error."""
    from terminalbench.agent_adapter import run_terminalbench_trial

    dest = raw_dir / f"{task_id}__{mode}__rep{rep}.json"
    if dest.exists():
        print(f"  skip {dest.name} (already done)")
        return True

    result = run_terminalbench_trial(
        task_id,
        bench_mode=mode,
        rep=rep,
        out_dir=trial_dir / f"{task_id}__{mode}__rep{rep}",
        model=model,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
    )
    payload = json.dumps(result.to_dict(), indent=2, default=str)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(payload)
    os.replace(tmp, dest)
    return not result.is_error


def main() -> None:
    parser = argparse.ArgumentParser(prog="ab.runner")
    parser.add_argument("--suite", required=True, help="Benchmark suite name (e.g. terminalbench)")
    parser.add_argument("--tasks", type=int, default=10, help="Number of tasks to select from suite")
    parser.add_argument("--n", type=int, default=5, help="Number of repetitions per cell")
    parser.add_argument("--models", required=True, help="Claude model slug (e.g. claude-sonnet-4-5)")
    parser.add_argument("--modes", default="on,off", help="Comma-separated list of bench modes")
    parser.add_argument("--out", required=True, help="Output directory (e.g. bench/runs/<run-id>/)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for task ordering (AB-04)")
    args = parser.parse_args()

    modes = [m.strip() for m in args.modes.split(",")]
    out_dir = Path(args.out)
    run_id = out_dir.name
    raw_dir = out_dir / "raw"
    trial_dir = out_dir / "trials"
    raw_dir.mkdir(parents=True, exist_ok=True)
    trial_dir.mkdir(parents=True, exist_ok=True)

    task_ids = load_suite_tasks(args.suite, args.tasks)
    dataset_name, dataset_version = _load_suite_meta(args.suite)

    config = {
        "run_id": run_id,
        "suite": args.suite,
        "tasks": task_ids,
        "n_reps": args.n,
        "model": args.models,
        "modes": modes,
        "seed": args.seed,
        "started_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    write_config(out_dir, config)

    from ab.schedule import build_schedule

    schedule = build_schedule(task_ids, modes, args.n, seed=args.seed)

    already_done = sum(1 for task_id, mode, rep in schedule if (raw_dir / f"{task_id}__{mode}__rep{rep}.json").exists())
    print(f"Schedule: {len(schedule)} cells total, {already_done} already completed")

    for i, (task_id, mode, rep) in enumerate(schedule, 1):
        print(f"[{i}/{len(schedule)}] {task_id} [{mode}] rep{rep}")
        try:
            run_cell(task_id, mode, rep, raw_dir, trial_dir, args.models, dataset_name, dataset_version)
        except Exception as exc:
            print(f"  ERROR: {exc}")

    from ab.aggregate import compute_summary

    summary = compute_summary(run_id, raw_dir)
    summary_path = out_dir / "summary.json"
    tmp = summary_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary, indent=2))
    os.replace(tmp, summary_path)
    print(f"\nSummary written to {summary_path}")

    print("\nResults:")
    for cell_key, cell in summary["cells"].items():
        print(f"  {cell_key}: {cell['passed']}/{cell['total']} " f"CI=[{cell['ci_lower']:.3f}, {cell['ci_upper']:.3f}]")


if __name__ == "__main__":
    main()
