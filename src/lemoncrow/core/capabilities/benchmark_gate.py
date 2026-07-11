from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.optimization.non_inferiority import (
    evaluate_non_inferiority,
    load_terminalbench_records,
    wilson_interval,
)


def write_benchmark_gate(run_dir: Path, payload: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "benchmark-gate.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_benchmark_gate(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "benchmark-gate.json"
    if not path.is_file():
        raise FileNotFoundError(f"{path} is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def require_benchmark_gate_pass(run_dir: Path) -> dict[str, Any]:
    payload = load_benchmark_gate(run_dir)
    if bool(payload.get("passed")):
        return payload
    reasons = [str(reason) for reason in payload.get("reasons", []) if str(reason).strip()]
    detail = "; ".join(reasons) if reasons else "benchmark gate did not pass"
    raise ValueError(detail)


def evaluate_terminalbench_gate(
    run_dir: Path,
    *,
    margin: float = 0.05,
    confidence: float = 0.95,
) -> dict[str, Any]:
    runs_path = run_dir / "runs.jsonl"
    if not runs_path.is_file():
        return _failed_gate(
            suite="terminalbench",
            reasons=["runs.jsonl is missing, so no paired benchmark evidence is available"],
        )
    records = load_terminalbench_records(runs_path)
    if not records:
        return _failed_gate(
            suite="terminalbench",
            reasons=["runs.jsonl is empty, so no paired benchmark evidence is available"],
        )
    baseline_cost = sum(
        float(record.get("cost_usd") or 0.0) for record in records if str(record.get("mode") or "").lower() == "off"
    )
    candidate_cost = sum(
        float(record.get("cost_usd") or 0.0) for record in records if str(record.get("mode") or "").lower() == "on"
    )
    try:
        verdict = evaluate_non_inferiority(
            records,
            baseline_cost_usd=baseline_cost,
            candidate_cost_usd=candidate_cost,
            margin=margin,
            confidence=confidence,
        )
    except ValueError as exc:
        return _failed_gate(suite="terminalbench", reasons=[str(exc)])
    return {
        "suite": "terminalbench",
        "evaluated_at": datetime.now(UTC).isoformat(),
        "passed": verdict.passed,
        "reasons": list(verdict.reasons),
        "checks": {
            "quality_metric": "grader_verdict pass rate",
            "cost_metric": "cost_usd_sum",
            "margin": margin,
            "confidence": confidence,
        },
        "details": verdict.to_dict(),
    }


def evaluate_codebench_gate(
    run_dir: Path,
    *,
    baseline_arm: str = "baseline",
    candidate_arm: str = "lemoncrow",
    margin: float = 0.05,
    confidence: float = 0.95,
) -> dict[str, Any]:
    results_path = run_dir / "results.jsonl"
    if not results_path.is_file():
        return _failed_gate(
            suite="codebench",
            reasons=["results.jsonl is missing, so no paired benchmark evidence is available"],
        )
    results = _load_jsonl(results_path)
    baseline_rows = [row for row in results if str(row.get("arm") or "") == baseline_arm]
    candidate_rows = [row for row in results if str(row.get("arm") or "") == candidate_arm]
    reasons: list[str] = []
    if not baseline_rows:
        reasons.append(f"baseline arm {baseline_arm!r} is missing from results.jsonl")
    if not candidate_rows:
        reasons.append(f"candidate arm {candidate_arm!r} is missing from results.jsonl")
    if reasons:
        return _failed_gate(suite="codebench", reasons=reasons)
    baseline_judged = sum(1 for row in baseline_rows if row.get("correct") is not None)
    candidate_judged = sum(1 for row in candidate_rows if row.get("correct") is not None)
    if baseline_judged != len(baseline_rows) or candidate_judged != len(candidate_rows):
        reasons.append("quality gate requires judged results for every baseline and candidate run")
    baseline_correct = sum(1 for row in baseline_rows if row.get("correct") is True)
    candidate_correct = sum(1 for row in candidate_rows if row.get("correct") is True)
    baseline_lower, baseline_upper = wilson_interval(baseline_correct, len(baseline_rows), confidence=confidence)
    candidate_lower, candidate_upper = wilson_interval(candidate_correct, len(candidate_rows), confidence=confidence)
    delta_lower = candidate_lower - baseline_upper
    delta_upper = candidate_upper - baseline_lower
    if delta_lower < -margin:
        reasons.append(
            f"lower confidence bound on judged solved-rate delta {delta_lower:.4f} is below allowed margin {-margin:.4f}"
        )
    baseline_cost = sum(float(row.get("cost_usd") or 0.0) for row in baseline_rows)
    candidate_cost = sum(float(row.get("cost_usd") or 0.0) for row in candidate_rows)
    if candidate_cost >= baseline_cost:
        reasons.append("candidate did not reduce measured cost versus baseline")
    pairwise_rows = _load_csv(run_dir / "pairwise_quality.csv")
    selected_pairwise = [
        row
        for row in pairwise_rows
        if row.get("baseline_arm") == baseline_arm and row.get("candidate_arm") == candidate_arm
    ]
    expected_pairs = {(str(row.get("task") or ""), str(row.get("rep") or "")) for row in candidate_rows}
    judged_pairs = [row for row in selected_pairwise if _truthy(row.get("judged"))]
    passing_pairs = [row for row in judged_pairs if _truthy(row.get("candidate_at_least_baseline"))]
    if len(selected_pairwise) < len(expected_pairs):
        reasons.append("pairwise quality gate requires a baseline-vs-candidate row for every candidate run")
    if len(judged_pairs) != len(selected_pairwise) or len(judged_pairs) < len(expected_pairs):
        reasons.append("pairwise quality gate requires judged baseline-vs-candidate comparisons for every pair")
    if len(passing_pairs) != len(judged_pairs):
        reasons.append("candidate quality regressed versus baseline in at least one judged pair")
    return {
        "suite": "codebench",
        "evaluated_at": datetime.now(UTC).isoformat(),
        "passed": not reasons,
        "reasons": reasons,
        "checks": {
            "quality_metric": "judge correct rate",
            "cost_metric": "cost_usd_sum",
            "margin": margin,
            "confidence": confidence,
            "baseline_arm": baseline_arm,
            "candidate_arm": candidate_arm,
        },
        "details": {
            "baseline": _codebench_arm_summary(
                baseline_rows, correct=baseline_correct, lower=baseline_lower, upper=baseline_upper
            ),
            "candidate": _codebench_arm_summary(
                candidate_rows,
                correct=candidate_correct,
                lower=candidate_lower,
                upper=candidate_upper,
            ),
            "judged_delta": {
                "pass_rate": (candidate_correct / len(candidate_rows)) - (baseline_correct / len(baseline_rows)),
                "delta_lower_bound": delta_lower,
                "delta_upper_bound": delta_upper,
            },
            "cost": {
                "baseline_cost_usd": baseline_cost,
                "candidate_cost_usd": candidate_cost,
                "estimated_cost_delta_usd": candidate_cost - baseline_cost,
                "estimated_cost_savings_usd": baseline_cost - candidate_cost,
            },
            "pairwise_quality": {
                "pairs": len(selected_pairwise),
                "expected_pairs": len(expected_pairs),
                "judged_pairs": len(judged_pairs),
                "candidate_at_least_baseline": len(passing_pairs),
                "quality_adjusted_savings_usd": sum(
                    float(row.get("quality_adjusted_saved_usd") or 0.0) for row in selected_pairwise
                ),
            },
        },
    }


def _failed_gate(*, suite: str, reasons: list[str]) -> dict[str, Any]:
    return {
        "suite": suite,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "passed": False,
        "reasons": reasons,
        "checks": {},
        "details": {},
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _load_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _codebench_arm_summary(rows: list[dict[str, Any]], *, correct: int, lower: float, upper: float) -> dict[str, Any]:
    total = len(rows)
    return {
        "total": total,
        "judged": sum(1 for row in rows if row.get("correct") is not None),
        "correct": correct,
        "pass_rate": (correct / total) if total else 0.0,
        "wilson_lower": lower,
        "wilson_upper": upper,
        "valid": sum(1 for row in rows if bool(row.get("valid", True))),
        "cost_usd_sum": sum(float(row.get("cost_usd") or 0.0) for row in rows),
    }


__all__ = [
    "evaluate_codebench_gate",
    "evaluate_terminalbench_gate",
    "load_benchmark_gate",
    "require_benchmark_gate_pass",
    "write_benchmark_gate",
]
