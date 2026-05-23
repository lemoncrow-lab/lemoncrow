"""Smoke tests for the blame benchmark."""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.slow

import json
from pathlib import Path

from benchmarks.code_intel.blame_bench import run_blame_bench


def test_blame_bench_smoke(tmp_path: Path) -> None:
    result = run_blame_bench(tmp_path)

    assert result.last_author == "carol@example.com"
    assert result.cold_cache_hit is False
    assert result.hot_cache_hit is True
    assert result.cold_provenance == "blame"
    assert result.hot_provenance == "cached"
    assert result.churn_commit_count == 2
    assert result.cold_total_tokens <= result.budget_tokens
    assert result.hot_elapsed_ms >= 0.0


def test_blame_bench_result_is_json_serializable(tmp_path: Path) -> None:
    payload = run_blame_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["hot_cache_hit"] is True
    assert reloaded["last_author"] == "carol@example.com"


def test_blame_bench_beats_manual_git_blame_workflow(tmp_path: Path) -> None:
    result = run_blame_bench(tmp_path)

    assert result.blame_workflow_steps < result.manual_workflow_steps
    assert result.cold_total_tokens < result.manual_total_tokens
    assert result.hot_total_tokens <= result.cold_total_tokens
    assert result.blame_to_manual_ratio < 1.0
