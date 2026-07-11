from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def write_benchmark_manifest(run_dir: Path, payload: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "benchmark-manifest.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def build_codebench_manifest(
    *,
    tasks: list[dict[str, Any]],
    arms: list[str],
    reps: int,
    model: str,
    cli_driver: str,
    timeout: int,
    jobs: int,
    parallel_scope: str,
    codebench_tasks_dir: Path,
    bridge_command: str | None,
) -> dict[str, Any]:
    baseline_arm = "baseline" if "baseline" in arms else arms[0]
    treatment_arms = [arm for arm in arms if arm != baseline_arm]
    return {
        "suite": "codebench",
        "frozen_at": datetime.now(UTC).isoformat(),
        "corpus": {
            "dataset_name": "codebench",
            "dataset_version": "ported-local",
            "source_root": str(codebench_tasks_dir.resolve()),
            "tasks": tasks,
        },
        "protocol": {
            "baseline_arm": baseline_arm,
            "treatment_arms": treatment_arms,
            "reps": reps,
            "arm_agents": {
                arm: "lemon:code" if arm == "lemoncrow" and cli_driver == "claude" else "host-default" for arm in arms
            },
            "matched_fields": {
                "model": model,
                "cli_driver": cli_driver,
                "timeout_seconds": timeout,
                "jobs": jobs,
                "parallel_scope": parallel_scope,
            },
            "bridge_command": bridge_command or "",
        },
        "artifacts": {
            "results_jsonl": "results.jsonl",
            "report_txt": "report.txt",
            "results_csv": "results.csv",
            "summary_csv": "summary.csv",
            "task_metrics_csv": "task_metrics.csv",
            "task_correctness_csv": "task_correctness.csv",
            "model_audit_csv": "model_audit.csv",
            "pairwise_quality_csv": "pairwise_quality.csv",
            "quality_adjusted_summary_csv": "quality_adjusted_summary.csv",
        },
    }


def build_terminalbench_manifest(
    *,
    task_ids: list[str],
    modes: list[str],
    rep: int,
    model: str,
    provider: str,
    dataset_meta: dict[str, str],
    tasks_path: Path,
) -> dict[str, Any]:
    baseline_mode = "off" if "off" in modes else modes[0]
    treatment_modes = [mode for mode in modes if mode != baseline_mode]
    return {
        "suite": "terminalbench",
        "frozen_at": datetime.now(UTC).isoformat(),
        "corpus": {
            "dataset_name": dataset_meta.get("name", "terminal-bench-core"),
            "dataset_version": dataset_meta.get("version", ""),
            "tasks_path": str(tasks_path.resolve()),
            "tasks": task_ids,
        },
        "protocol": {
            "baseline_arm": baseline_mode,
            "treatment_arms": treatment_modes,
            "reps": rep,
            "matched_fields": {
                "model": model,
                "provider": provider,
            },
        },
        "artifacts": {
            "runs_jsonl": "runs.jsonl",
            "summary_json": "summary.json",
        },
    }


__all__ = [
    "build_codebench_manifest",
    "build_terminalbench_manifest",
    "write_benchmark_manifest",
]
