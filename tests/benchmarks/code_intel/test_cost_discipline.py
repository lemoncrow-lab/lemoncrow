"""Benchmark tests for the M12 cost-discipline partial close."""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.slow

import json
from pathlib import Path

from benchmarks.code_intel.cost_discipline import run_cost_discipline_bench


def test_cost_discipline_bench_is_json_serializable(tmp_path: Path) -> None:
    payload = run_cost_discipline_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["aggregate_baseline_tokens"] > reloaded["aggregate_current_tokens"]
    assert reloaded["cache_status_total_tokens"] > 0
    assert reloaded["historical_baseline_tokens"] > reloaded["historical_current_tokens"]
    assert reloaded["blame_baseline_tokens"] > reloaded["blame_current_tokens"]


def test_shipped_phase2_flows_stay_within_30pct_of_baseline(tmp_path: Path) -> None:
    result = run_cost_discipline_bench(tmp_path)

    assert result.search_current_tokens < result.search_baseline_tokens
    assert result.pattern_current_tokens < result.pattern_baseline_tokens
    assert result.historical_current_tokens < result.historical_baseline_tokens
    assert result.blame_current_tokens < result.blame_baseline_tokens
    assert result.aggregate_current_tokens <= result.aggregate_baseline_tokens * 0.3
