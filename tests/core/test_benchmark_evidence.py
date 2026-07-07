from __future__ import annotations

import json
from pathlib import Path

from atelier.core.capabilities.benchmark_evidence import (
    build_codebench_evidence,
    build_terminalbench_evidence,
)


def test_build_terminalbench_evidence_embeds_summary_and_artifact_paths(tmp_path: Path) -> None:
    run_dir = tmp_path / "terminalbench"
    run_dir.mkdir()
    manifest_path = run_dir / "benchmark-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    (run_dir / "runs.jsonl").write_text(
        json.dumps({"task_id": "hello-world", "mode": "on", "grader_verdict": "pass"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text(
        json.dumps({"delta_on_minus_off": {"pass_rate": 0.2}}),
        encoding="utf-8",
    )
    (run_dir / "hello-world__on__rep1.json").write_text("{}", encoding="utf-8")

    evidence = build_terminalbench_evidence(
        run_dir=run_dir,
        manifest_path=manifest_path,
        repo_state={"commit": "abc123", "dirty": False},
    )

    assert evidence["commit_under_test"]["commit"] == "abc123"
    assert evidence["artifacts"]["summary_json"]["exists"] is True
    assert evidence["artifacts"]["transcripts"] == ["hello-world__on__rep1.json"]
    assert evidence["summary"]["delta_on_minus_off"]["pass_rate"] == 0.2


def test_build_codebench_evidence_summarizes_results_and_judge_fields(tmp_path: Path) -> None:
    run_dir = tmp_path / "codebench"
    run_dir.mkdir()
    manifest_path = run_dir / "benchmark-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    results = [
        {
            "task": "task1",
            "arm": "baseline",
            "valid": True,
            "correct": False,
            "cost_usd": 1.2,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 5,
            "duration_ms": 500,
            "flow_path": "baseline.flow",
        },
        {
            "task": "task1",
            "arm": "atelier",
            "valid": True,
            "correct": True,
            "cost_usd": 0.8,
            "input_tokens": 80,
            "output_tokens": 16,
            "cache_read_tokens": 10,
            "duration_ms": 400,
            "flow_path": "atelier.flow",
        },
    ]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in results) + "\n",
        encoding="utf-8",
    )

    evidence = build_codebench_evidence(
        run_dir=run_dir,
        manifest_path=manifest_path,
        repo_state={"commit": "def456", "dirty": True},
    )

    assert evidence["commit_under_test"]["dirty"] is True
    assert evidence["judge_outputs"]["fields"] == ["correct", "score", "judge_model", "judge_reason"]
    assert evidence["summary"]["by_arm"]["atelier"]["correct"] == 1
    assert evidence["summary"]["by_arm"]["baseline"]["cost_usd_sum"] == 1.2
    assert evidence["artifacts"]["task_metrics_csv"]["exists"] is False
    assert evidence["artifacts"]["task_correctness_csv"]["exists"] is False
    assert evidence["artifacts"]["model_audit_csv"]["exists"] is False
    assert evidence["artifacts"]["pairwise_quality_csv"]["exists"] is False
    assert evidence["artifacts"]["quality_adjusted_summary_csv"]["exists"] is False
    assert evidence["judge_outputs"]["pairwise_fields"] == [
        "candidate_at_least_baseline",
        "baseline_score",
        "candidate_score",
        "quality_delta",
        "judge_reason",
    ]
    assert evidence["artifacts"]["flow_paths"] == ["atelier.flow", "baseline.flow"]
