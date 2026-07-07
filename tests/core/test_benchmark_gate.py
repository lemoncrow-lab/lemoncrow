from __future__ import annotations

import csv
import json
from pathlib import Path

from atelier.core.capabilities.benchmark_gate import (
    evaluate_codebench_gate,
    evaluate_terminalbench_gate,
)


def test_evaluate_terminalbench_gate_passes_with_noninferior_cheaper_candidate(tmp_path: Path) -> None:
    run_dir = tmp_path / "terminalbench"
    run_dir.mkdir()
    rows = [{"mode": "off", "grader_verdict": "pass", "cost_usd": 3.0} for _ in range(40)] + [
        {"mode": "on", "grader_verdict": "pass", "cost_usd": 1.0} for _ in range(40)
    ]
    (run_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    verdict = evaluate_terminalbench_gate(run_dir, margin=0.10, confidence=0.95)

    assert verdict["suite"] == "terminalbench"
    assert verdict["passed"] is True
    assert verdict["details"]["estimated_cost_savings_usd"] == 80.0


def test_evaluate_codebench_gate_requires_judged_results_and_cost_reduction(tmp_path: Path) -> None:
    run_dir = tmp_path / "codebench"
    run_dir.mkdir()
    rows = [
        {"arm": "baseline", "correct": True, "cost_usd": 2.0, "valid": True},
        {"arm": "atelier", "correct": None, "cost_usd": 1.0, "valid": True},
    ]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    verdict = evaluate_codebench_gate(run_dir, baseline_arm="baseline", candidate_arm="atelier")

    assert verdict["suite"] == "codebench"
    assert verdict["passed"] is False
    assert "quality gate requires judged results" in verdict["reasons"][0]


def test_evaluate_codebench_gate_requires_pairwise_quality(tmp_path: Path) -> None:
    run_dir = tmp_path / "codebench"
    run_dir.mkdir()
    rows = [
        {"task": "t1", "rep": 0, "arm": "baseline", "correct": True, "cost_usd": 2.0, "valid": True},
        {"task": "t1", "rep": 0, "arm": "atelier", "correct": True, "cost_usd": 1.0, "valid": True},
    ]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    verdict = evaluate_codebench_gate(run_dir, baseline_arm="baseline", candidate_arm="atelier")

    assert verdict["passed"] is False
    assert any("pairwise quality gate" in reason for reason in verdict["reasons"])


def test_evaluate_codebench_gate_passes_with_pairwise_non_regression(tmp_path: Path) -> None:
    run_dir = tmp_path / "codebench"
    run_dir.mkdir()
    rows = [
        {"task": "t1", "rep": 0, "arm": "baseline", "correct": True, "cost_usd": 2.0, "valid": True},
        {"task": "t1", "rep": 0, "arm": "atelier", "correct": True, "cost_usd": 1.0, "valid": True},
    ]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    with (run_dir / "pairwise_quality.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "task",
                "rep",
                "baseline_arm",
                "candidate_arm",
                "judged",
                "candidate_at_least_baseline",
                "quality_adjusted_saved_usd",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "task": "t1",
                "rep": 0,
                "baseline_arm": "baseline",
                "candidate_arm": "atelier",
                "judged": "True",
                "candidate_at_least_baseline": "True",
                "quality_adjusted_saved_usd": "1.0",
            }
        )

    verdict = evaluate_codebench_gate(run_dir, baseline_arm="baseline", candidate_arm="atelier", margin=1.0)

    assert verdict["passed"] is True
    assert verdict["details"]["pairwise_quality"]["quality_adjusted_savings_usd"] == 1.0
