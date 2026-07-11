from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def git_state(repo_root: Path) -> dict[str, Any]:
    commit = _git_stdout(repo_root, "rev-parse", "HEAD")
    dirty = bool(_git_stdout(repo_root, "status", "--porcelain"))
    return {"commit": commit, "dirty": dirty}


def write_benchmark_evidence(run_dir: Path, payload: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "benchmark-evidence.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def build_terminalbench_evidence(
    *,
    run_dir: Path,
    manifest_path: Path,
    repo_state: dict[str, Any],
) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    runs_path = run_dir / "runs.jsonl"
    transcript_paths = sorted(path.name for path in run_dir.glob("*__*__rep*.json"))
    return {
        "suite": "terminalbench",
        "captured_at": datetime.now(UTC).isoformat(),
        "commit_under_test": repo_state,
        "manifest_path": str(manifest_path),
        "artifacts": {
            "runs_jsonl": _artifact_record(runs_path),
            "summary_json": _artifact_record(summary_path),
            "transcripts": transcript_paths,
        },
        "judge_outputs": {
            "kind": "embedded-jsonl",
            "path": str(runs_path),
            "fields": ["grader_verdict", "grader_is_resolved", "grader_failure_mode"],
        },
        "summary": _load_json(summary_path) or {},
    }


def build_codebench_evidence(
    *,
    run_dir: Path,
    manifest_path: Path,
    repo_state: dict[str, Any],
) -> dict[str, Any]:
    results_path = run_dir / "results.jsonl"
    summary_csv_path = run_dir / "summary.csv"
    results_csv_path = run_dir / "results.csv"
    task_metrics_csv_path = run_dir / "task_metrics.csv"
    task_correctness_csv_path = run_dir / "task_correctness.csv"
    model_audit_csv_path = run_dir / "model_audit.csv"
    pairwise_quality_csv_path = run_dir / "pairwise_quality.csv"
    quality_adjusted_summary_csv_path = run_dir / "quality_adjusted_summary.csv"
    report_path = run_dir / "report.txt"
    results = _load_jsonl(results_path)
    flow_paths = sorted(
        {
            str(item.get("flow_path") or "")
            for item in results
            if isinstance(item.get("flow_path"), str) and str(item.get("flow_path") or "")
        }
    )
    return {
        "suite": "codebench",
        "captured_at": datetime.now(UTC).isoformat(),
        "commit_under_test": repo_state,
        "manifest_path": str(manifest_path),
        "artifacts": {
            "results_jsonl": _artifact_record(results_path),
            "results_csv": _artifact_record(results_csv_path),
            "summary_csv": _artifact_record(summary_csv_path),
            "task_metrics_csv": _artifact_record(task_metrics_csv_path),
            "task_correctness_csv": _artifact_record(task_correctness_csv_path),
            "model_audit_csv": _artifact_record(model_audit_csv_path),
            "pairwise_quality_csv": _artifact_record(pairwise_quality_csv_path),
            "quality_adjusted_summary_csv": _artifact_record(quality_adjusted_summary_csv_path),
            "report_txt": _artifact_record(report_path),
            "flow_paths": flow_paths,
        },
        "judge_outputs": {
            "kind": "embedded-jsonl",
            "path": str(results_path),
            "fields": ["correct", "score", "judge_model", "judge_reason"],
            "pairwise_path": str(pairwise_quality_csv_path),
            "pairwise_fields": [
                "candidate_at_least_baseline",
                "baseline_score",
                "candidate_score",
                "quality_delta",
                "judge_reason",
            ],
        },
        "summary": _summarize_codebench_results(results),
    }


def _artifact_record(path: Path) -> dict[str, Any]:
    return {"path": str(path), "exists": path.exists()}


def _git_stdout(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _summarize_codebench_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    arms = sorted({str(result.get("arm") or "") for result in results if result.get("arm")})
    by_arm: dict[str, Any] = {}
    for arm in arms:
        arm_rows = [result for result in results if str(result.get("arm") or "") == arm]
        total = len(arm_rows)
        judged = sum(1 for row in arm_rows if row.get("correct") is not None)
        correct = sum(1 for row in arm_rows if row.get("correct") is True)
        by_arm[arm] = {
            "total": total,
            "valid": sum(1 for row in arm_rows if bool(row.get("valid", True))),
            "judged": judged,
            "correct": correct,
            "cost_usd_sum": sum(float(row.get("cost_usd") or 0.0) for row in arm_rows),
            "input_tokens_sum": sum(int(row.get("input_tokens") or 0) for row in arm_rows),
            "output_tokens_sum": sum(int(row.get("output_tokens") or 0) for row in arm_rows),
            "cache_read_tokens_sum": sum(int(row.get("cache_read_tokens") or 0) for row in arm_rows),
            "duration_ms_sum": sum(int(row.get("duration_ms") or 0) for row in arm_rows),
        }
    return {"total_runs": len(results), "arms": arms, "by_arm": by_arm}


__all__ = [
    "build_codebench_evidence",
    "build_terminalbench_evidence",
    "git_state",
    "write_benchmark_evidence",
]
