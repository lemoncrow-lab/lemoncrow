"""Benchmark tests for the M8 call-graph token gate."""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.code_intel.call_graph_bench import run_call_graph_bench


def test_call_graph_bench_is_json_serializable(tmp_path: Path) -> None:
    payload = run_call_graph_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["target_symbol"] == "beta"


def test_call_graph_default_depth_stays_under_budget_and_smaller_than_expanded_snapshot(tmp_path: Path) -> None:
    result = run_call_graph_bench(tmp_path)

    assert result.default_within_budget is True
    assert result.target_symbol == "beta"
    assert result.default_related == ["alpha"]
    assert set(result.expanded_related) == {"alpha", "gamma", "handle"}
    assert result.snapshot_present is True
    assert result.default_total_tokens < result.expanded_total_tokens
    assert result.default_total_tokens <= result.expanded_total_tokens * 0.8
