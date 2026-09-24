from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_RUNTIME_POLICY_VERSIONS = {
    "search_feedback": "1",
    "code_context_evidence": "1",
    "bounded_evidence_resolution": "1-shadow",
}


def runtime_policy_fingerprint(payload: dict[str, Any]) -> str:
    """Stable short fingerprint for non-secret runtime policy metadata."""

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _host_control_tier(cli_driver: str) -> str:
    if cli_driver == "lemoncrow-run":
        return "O"
    if cli_driver == "claude":
        return "H"
    return "S"


def build_runtime_attribution(
    *,
    arms: list[str],
    cli_driver: str,
    comparators: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Describe what each benchmark arm actually controls and which policy ran."""

    rows: dict[str, dict[str, Any]] = {}
    comparator_rows = comparators or {}
    for arm in arms:
        if arm in comparator_rows:
            metadata = comparator_rows[arm]
            rows[arm] = {
                "role": "external",
                "description": "external comparator runtime/tooling",
                "host_control_tier": "external",
                "runtime_policy": None,
                "policy_fingerprint": None,
                "manifest_fingerprint": metadata.get("manifest_fingerprint"),
                "manifest_sha256": metadata.get("manifest_sha256"),
                "repo": metadata.get("repo", ""),
                "ref": metadata.get("ref", ""),
            }
            continue
        if arm in {"baseline", "off"}:
            rows[arm] = {
                "role": "A0",
                "description": "host-native baseline",
                "host_control_tier": "host-native",
                "runtime_policy": None,
                "policy_fingerprint": None,
            }
            continue

        resolution_mode = "off" if arm == "lemoncrow-control" else "shadow"
        if arm in {"lemoncrow-control", "on"}:
            role = "A1"
            description = "current LemonCrow control; candidate evidence resolution disabled"
            resolution_mode = "off"
        elif arm == "lemoncrow-shadow":
            role = "A2"
            description = "LemonCrow candidate evidence-resolution policy in shadow"
        elif arm == "lemoncrow-candidate":
            role = "A3"
            description = "unqualified benchmark-only evidence-resolution enforcement candidate"
            resolution_mode = "experiment"
        else:
            role = "standard"
            description = "LemonCrow runtime arm"

        policy = {
            "search_feedback": {"version": _RUNTIME_POLICY_VERSIONS["search_feedback"], "mode": "observe"},
            "code_context_evidence": {"version": _RUNTIME_POLICY_VERSIONS["code_context_evidence"], "mode": "shadow"},
            "bounded_evidence_resolution": {
                "version": (
                    "1-experiment"
                    if resolution_mode == "experiment"
                    else _RUNTIME_POLICY_VERSIONS["bounded_evidence_resolution"]
                ),
                "mode": resolution_mode,
                "enforcement_qualified": False,
            },
        }
        rows[arm] = {
            "role": role,
            "description": description,
            "host_control_tier": _host_control_tier(cli_driver),
            "runtime_policy": policy,
            "policy_fingerprint": runtime_policy_fingerprint(policy),
        }

    a3_available = any(row.get("role") == "A3" for row in rows.values())
    return {
        "schema_version": 1,
        "qualification_state": "candidate_experiment" if a3_available else "shadow_only",
        "enforcement_qualified": False,
        "a3_available": a3_available,
        "arms": rows,
        "comparators": comparator_rows,
    }


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
    runtime_attribution: dict[str, Any] | None = None,
    mode: str = "cost",
    judge: bool = False,
    judge_model: str | None = None,
    cli_version: str = "",
    judge_cli_version: str = "",
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
                arm: (
                    "lemoncrow:code"
                    if arm
                    in {
                        "lemoncrow",
                        "lemoncrow-control",
                        "lemoncrow-shadow",
                        "lemoncrow-candidate",
                        "lemoncrow-readable",
                        "execute",
                        "solve",
                        "auto",
                    }
                    and cli_driver == "claude"
                    else "host-default"
                )
                for arm in arms
            },
            "matched_fields": {
                "model": model,
                "cli_driver": cli_driver,
                "timeout_seconds": timeout,
                "jobs": jobs,
                "parallel_scope": parallel_scope,
                "mode": mode,
                "judge": judge,
                "judge_model": judge_model or "",
                "cli_version": cli_version,
                "judge_cli_version": judge_cli_version,
            },
            "bridge_command": bridge_command or "",
        },
        "runtime_attribution": runtime_attribution or build_runtime_attribution(arms=arms, cli_driver=cli_driver),
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
            "comparison_gates_json": "benchmark-comparison-gates.json",
            "runtime_policy_gate_json": "runtime-policy-gate.json",
            "runtime_policy_jsonl_glob": "*.runtime-policy.jsonl",
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
    runtime_attribution: dict[str, Any] | None = None,
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
        "runtime_attribution": runtime_attribution or build_runtime_attribution(arms=modes, cli_driver="terminalbench"),
        "artifacts": {
            "runs_jsonl": "runs.jsonl",
            "summary_json": "summary.json",
        },
    }


__all__ = [
    "build_codebench_manifest",
    "build_runtime_attribution",
    "build_terminalbench_manifest",
    "runtime_policy_fingerprint",
    "write_benchmark_manifest",
]
