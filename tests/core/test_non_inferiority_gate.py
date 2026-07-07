from __future__ import annotations

import json
from pathlib import Path

import pytest

from atelier.core.capabilities.optimization import (
    evaluate_non_inferiority,
    evaluate_non_inferiority_from_runs,
    load_terminalbench_records,
    summarize_terminalbench_arm,
)


def _record(*, mode: str, verdict: str | None, is_error: bool = False) -> dict[str, object]:
    return {
        "task_id": "task-1",
        "mode": mode,
        "rep": 1,
        "grader_verdict": verdict,
        "is_error": is_error,
    }


def test_load_terminalbench_records_accepts_directory(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    runs.write_text(json.dumps(_record(mode="on", verdict="pass")) + "\n\n", encoding="utf-8")

    loaded = load_terminalbench_records(tmp_path)

    assert loaded == [_record(mode="on", verdict="pass")]


def test_summarize_terminalbench_arm_counts_error_like_rows() -> None:
    summary = summarize_terminalbench_arm(
        [
            _record(mode="on", verdict="pass"),
            _record(mode="on", verdict="fail"),
            _record(mode="on", verdict=None, is_error=True),
            _record(mode="off", verdict="pass"),
        ],
        mode="on",
    )

    assert summary.total == 3
    assert summary.passed == 1
    assert summary.failed == 2
    assert summary.error_like == 1


def test_evaluate_non_inferiority_passes_with_cost_savings_and_quality_margin() -> None:
    rows = [*[_record(mode="off", verdict="pass") for _ in range(950)]]
    rows.extend(_record(mode="off", verdict="fail") for _ in range(50))
    rows.extend(_record(mode="on", verdict="pass") for _ in range(948))
    rows.extend(_record(mode="on", verdict="fail") for _ in range(52))

    verdict = evaluate_non_inferiority(
        rows,
        baseline_cost_usd=10.0,
        candidate_cost_usd=8.0,
        margin=0.05,
    )

    assert verdict.passed is True
    assert verdict.estimated_cost_savings_usd == pytest.approx(2.0)
    assert verdict.delta_lower_bound >= -0.05


def test_evaluate_non_inferiority_fails_when_quality_regresses_beyond_margin() -> None:
    rows = [*[_record(mode="off", verdict="pass") for _ in range(90)]]
    rows.extend(_record(mode="off", verdict="fail") for _ in range(10))
    rows.extend(_record(mode="on", verdict="pass") for _ in range(70))
    rows.extend(_record(mode="on", verdict="fail") for _ in range(30))

    verdict = evaluate_non_inferiority(
        rows,
        baseline_cost_usd=10.0,
        candidate_cost_usd=7.0,
        margin=0.05,
    )

    assert verdict.passed is False
    assert any("pass-rate delta" in reason for reason in verdict.reasons)


def test_evaluate_non_inferiority_fails_without_cost_savings(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    rows = [*[_record(mode="off", verdict="pass") for _ in range(50)]]
    rows.extend(_record(mode="on", verdict="pass") for _ in range(50))
    runs.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    verdict = evaluate_non_inferiority_from_runs(
        runs,
        baseline_cost_usd=10.0,
        candidate_cost_usd=10.5,
        margin=0.05,
    )

    assert verdict.passed is False
    assert "candidate did not reduce estimated cost versus baseline" in verdict.reasons
