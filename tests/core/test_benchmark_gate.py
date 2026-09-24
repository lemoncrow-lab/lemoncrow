from __future__ import annotations

import csv
import json
from pathlib import Path

from lemoncrow.core.capabilities.benchmark_gate import (
    evaluate_codebench_comparison_gates,
    evaluate_codebench_gate,
    evaluate_runtime_policy_gate,
    evaluate_terminalbench_gate,
)
from lemoncrow.core.capabilities.benchmark_manifest import build_runtime_attribution


def _write_passing_pair(run_dir: Path, *, control_arm: str, candidate_arm: str) -> None:
    rows = [
        {
            "task": "t1",
            "rep": 0,
            "arm": control_arm,
            "correct": True,
            "cost_usd": 2.0,
            "valid": True,
            "runtime_policy_events": 1,
            "runtime_policy_experiment_events": 0,
            "runtime_policy_expansions": 0,
        },
        {
            "task": "t1",
            "rep": 0,
            "arm": candidate_arm,
            "correct": True,
            "cost_usd": 1.0,
            "valid": True,
            "runtime_policy_events": 1,
            "runtime_policy_experiment_events": 1,
            "runtime_policy_expansions": 1,
        },
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
                "baseline_arm": control_arm,
                "candidate_arm": candidate_arm,
                "judged": "True",
                "candidate_at_least_baseline": "True",
                "quality_adjusted_saved_usd": "1.0",
            }
        )


def test_codebench_ceiling_mode_gates_on_completion_not_cost(tmp_path: Path) -> None:
    """Ceiling tasks are sized past baseline capacity, so finishing more costs more."""
    run_dir = tmp_path / "ceiling"
    run_dir.mkdir()
    rows = [
        {"arm": "baseline", "correct": False, "cost_usd": 1.0, "valid": True},
        {"arm": "lemoncrow", "correct": True, "cost_usd": 3.0, "valid": True},
    ]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    cost_mode = evaluate_codebench_gate(run_dir)
    ceiling_mode = evaluate_codebench_gate(run_dir, mode="ceiling")

    assert "candidate did not reduce measured cost versus baseline" in cost_mode["reasons"]
    assert "candidate did not reduce measured cost versus baseline" not in ceiling_mode["reasons"]
    assert "candidate did not complete more ceiling tasks than baseline" not in ceiling_mode["reasons"]
    assert ceiling_mode["checks"]["cost_metric"] == "completion rate"


def test_codebench_ceiling_mode_fails_when_candidate_completes_no_more(tmp_path: Path) -> None:
    run_dir = tmp_path / "ceiling-flat"
    run_dir.mkdir()
    rows = [
        {"arm": "baseline", "correct": True, "cost_usd": 1.0, "valid": True},
        {"arm": "lemoncrow", "correct": True, "cost_usd": 0.5, "valid": True},
    ]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    verdict = evaluate_codebench_gate(run_dir, mode="ceiling")

    assert "candidate did not complete more ceiling tasks than baseline" in verdict["reasons"]


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
        {"arm": "lemoncrow", "correct": None, "cost_usd": 1.0, "valid": True},
    ]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    verdict = evaluate_codebench_gate(run_dir, baseline_arm="baseline", candidate_arm="lemoncrow")

    assert verdict["suite"] == "codebench"
    assert verdict["passed"] is False
    assert "quality gate requires judged results" in verdict["reasons"][0]


def test_evaluate_codebench_gate_requires_pairwise_quality(tmp_path: Path) -> None:
    run_dir = tmp_path / "codebench"
    run_dir.mkdir()
    rows = [
        {"task": "t1", "rep": 0, "arm": "baseline", "correct": True, "cost_usd": 2.0, "valid": True},
        {"task": "t1", "rep": 0, "arm": "lemoncrow", "correct": True, "cost_usd": 1.0, "valid": True},
    ]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    verdict = evaluate_codebench_gate(run_dir, baseline_arm="baseline", candidate_arm="lemoncrow")

    assert verdict["passed"] is False
    assert any("pairwise quality gate" in reason for reason in verdict["reasons"])


def test_evaluate_codebench_gate_passes_with_pairwise_non_regression(tmp_path: Path) -> None:
    run_dir = tmp_path / "codebench"
    run_dir.mkdir()
    rows = [
        {"task": "t1", "rep": 0, "arm": "baseline", "correct": True, "cost_usd": 2.0, "valid": True},
        {"task": "t1", "rep": 0, "arm": "lemoncrow", "correct": True, "cost_usd": 1.0, "valid": True},
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
                "candidate_arm": "lemoncrow",
                "judged": "True",
                "candidate_at_least_baseline": "True",
                "quality_adjusted_saved_usd": "1.0",
            }
        )

    verdict = evaluate_codebench_gate(run_dir, baseline_arm="baseline", candidate_arm="lemoncrow", margin=1.0)

    assert verdict["passed"] is True
    assert verdict["details"]["pairwise_quality"]["quality_adjusted_savings_usd"] == 1.0


def test_codebench_gate_allows_tied_five_rep_quality_with_default_margin(tmp_path: Path) -> None:
    run_dir = tmp_path / "five-rep-tie"
    run_dir.mkdir()
    rows = []
    for rep in range(5):
        rows.extend(
            [
                {"task": "t1", "rep": rep, "arm": "baseline", "correct": True, "cost_usd": 2.0, "valid": True},
                {"task": "t1", "rep": rep, "arm": "lemoncrow", "correct": True, "cost_usd": 1.0, "valid": True},
            ]
        )
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
        for rep in range(5):
            writer.writerow(
                {
                    "task": "t1",
                    "rep": rep,
                    "baseline_arm": "baseline",
                    "candidate_arm": "lemoncrow",
                    "judged": "True",
                    "candidate_at_least_baseline": "True",
                    "quality_adjusted_saved_usd": "1.0",
                }
            )

    verdict = evaluate_codebench_gate(run_dir, baseline_arm="baseline", candidate_arm="lemoncrow")

    assert verdict["passed"] is True
    assert verdict["details"]["judged_delta"]["pass_rate"] == 0.0
    assert verdict["details"]["judged_delta"]["confidence_intervals_are_diagnostic"] is True


def test_codebench_gate_rejects_observed_quality_drop_beyond_margin(tmp_path: Path) -> None:
    run_dir = tmp_path / "quality-drop"
    run_dir.mkdir()
    rows = []
    for rep in range(5):
        rows.append({"task": "t1", "rep": rep, "arm": "baseline", "correct": True, "cost_usd": 2.0, "valid": True})
        rows.append(
            {
                "task": "t1",
                "rep": rep,
                "arm": "lemoncrow",
                "correct": rep != 0,
                "cost_usd": 1.0,
                "valid": True,
            }
        )
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
        for rep in range(5):
            writer.writerow(
                {
                    "task": "t1",
                    "rep": rep,
                    "baseline_arm": "baseline",
                    "candidate_arm": "lemoncrow",
                    "judged": "True",
                    "candidate_at_least_baseline": "True",
                    "quality_adjusted_saved_usd": "1.0",
                }
            )

    verdict = evaluate_codebench_gate(run_dir, baseline_arm="baseline", candidate_arm="lemoncrow")

    assert verdict["passed"] is False
    assert any("observed judged solved-rate delta" in reason for reason in verdict["reasons"])


def test_codebench_gate_carries_runtime_attribution(tmp_path: Path) -> None:
    run_dir = tmp_path / "attributed"
    run_dir.mkdir()
    (run_dir / "benchmark-manifest.json").write_text(
        json.dumps(
            {
                "runtime_attribution": {
                    "qualification_state": "shadow_only",
                    "arms": {
                        "lemoncrow-control": {"role": "A1"},
                        "lemoncrow-shadow": {"role": "A2"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "task": "t1",
            "rep": 0,
            "arm": "lemoncrow-control",
            "correct": True,
            "cost_usd": 2.0,
            "valid": True,
        },
        {
            "task": "t1",
            "rep": 0,
            "arm": "lemoncrow-shadow",
            "correct": True,
            "cost_usd": 1.0,
            "valid": True,
        },
    ]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    verdict = evaluate_codebench_gate(
        run_dir,
        baseline_arm="lemoncrow-control",
        candidate_arm="lemoncrow-shadow",
        margin=1.0,
    )

    assert verdict["runtime_attribution"]["qualification_state"] == "shadow_only"
    assert verdict["runtime_attribution"]["arms"]["lemoncrow-shadow"]["role"] == "A2"


def test_comparison_gates_evaluate_every_candidate_independently(tmp_path: Path) -> None:
    run_dir = tmp_path / "comparisons"
    run_dir.mkdir()
    rows = [
        {"task": "t1", "rep": 0, "arm": "baseline", "correct": True, "cost_usd": 3.0, "valid": True},
        {"task": "t1", "rep": 0, "arm": "lemoncrow", "correct": True, "cost_usd": 1.0, "valid": True},
        {"task": "t1", "rep": 0, "arm": "other-tool", "correct": False, "cost_usd": 0.5, "valid": True},
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
                "candidate_arm": "lemoncrow",
                "judged": "True",
                "candidate_at_least_baseline": "True",
                "quality_adjusted_saved_usd": "2.0",
            }
        )
        writer.writerow(
            {
                "task": "t1",
                "rep": 0,
                "baseline_arm": "baseline",
                "candidate_arm": "other-tool",
                "judged": "True",
                "candidate_at_least_baseline": "False",
                "quality_adjusted_saved_usd": "0.0",
            }
        )

    gates = evaluate_codebench_comparison_gates(
        run_dir,
        baseline_arm="baseline",
        candidate_arms=["lemoncrow", "other-tool"],
        margin=1.0,
    )

    assert gates["candidate_arms"] == ["lemoncrow", "other-tool"]
    assert gates["candidates"]["lemoncrow"]["passed"] is True
    assert gates["candidates"]["other-tool"]["passed"] is False
    assert gates["passed_all"] is False


def test_runtime_policy_gate_qualifies_only_manifest_proven_a1_a3(tmp_path: Path) -> None:
    run_dir = tmp_path / "runtime-policy-pass"
    run_dir.mkdir()
    attribution = build_runtime_attribution(
        arms=["lemoncrow-control", "lemoncrow-candidate"],
        cli_driver="claude",
    )
    (run_dir / "benchmark-manifest.json").write_text(
        json.dumps({"runtime_attribution": attribution}),
        encoding="utf-8",
    )
    _write_passing_pair(
        run_dir,
        control_arm="lemoncrow-control",
        candidate_arm="lemoncrow-candidate",
    )

    verdict = evaluate_runtime_policy_gate(run_dir, margin=1.0)

    assert verdict["passed"] is True
    assert verdict["suite"] == "codebench_runtime_policy"
    assert verdict["qualification"]["enforcement_qualified"] is True
    assert verdict["qualification"]["control_policy_fingerprint"]
    assert verdict["qualification"]["candidate_policy_fingerprint"]
    assert (
        verdict["qualification"]["control_policy_fingerprint"]
        != verdict["qualification"]["candidate_policy_fingerprint"]
    )


def test_runtime_policy_gate_rejects_good_numbers_without_a3_manifest_role(tmp_path: Path) -> None:
    run_dir = tmp_path / "runtime-policy-wrong-role"
    run_dir.mkdir()
    attribution = build_runtime_attribution(
        arms=["lemoncrow-control", "lemoncrow-shadow"],
        cli_driver="claude",
    )
    (run_dir / "benchmark-manifest.json").write_text(
        json.dumps({"runtime_attribution": attribution}),
        encoding="utf-8",
    )
    _write_passing_pair(
        run_dir,
        control_arm="lemoncrow-control",
        candidate_arm="lemoncrow-shadow",
    )

    verdict = evaluate_runtime_policy_gate(
        run_dir,
        control_arm="lemoncrow-control",
        candidate_arm="lemoncrow-shadow",
        margin=1.0,
    )

    assert verdict["passed"] is False
    assert verdict["qualification"]["enforcement_qualified"] is False
    assert any("manifest role A3" in reason for reason in verdict["reasons"])
    assert any("explicit benchmark-only" in reason for reason in verdict["reasons"])


def test_runtime_policy_gate_rejects_manifest_only_candidate_without_observed_execution(tmp_path: Path) -> None:
    run_dir = tmp_path / "runtime-policy-no-observation"
    run_dir.mkdir()
    attribution = build_runtime_attribution(
        arms=["lemoncrow-control", "lemoncrow-candidate"],
        cli_driver="claude",
    )
    (run_dir / "benchmark-manifest.json").write_text(
        json.dumps({"runtime_attribution": attribution}),
        encoding="utf-8",
    )
    _write_passing_pair(
        run_dir,
        control_arm="lemoncrow-control",
        candidate_arm="lemoncrow-candidate",
    )
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["arm"] == "lemoncrow-candidate":
            row["runtime_policy_events"] = 0
            row["runtime_policy_experiment_events"] = 0
            row["runtime_policy_expansions"] = 0
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    verdict = evaluate_runtime_policy_gate(run_dir, margin=1.0)

    assert verdict["passed"] is False
    assert verdict["qualification"]["enforcement_qualified"] is False
    assert any("not observed" in reason for reason in verdict["reasons"])
    assert any("not exercised" in reason for reason in verdict["reasons"])
