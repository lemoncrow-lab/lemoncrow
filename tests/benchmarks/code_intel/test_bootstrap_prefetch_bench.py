"""Benchmark tests for the Phase 6 bootstrap prefetch smoke suite."""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.slow

import json
from pathlib import Path

from benchmarks.code_intel.bootstrap_prefetch_bench import run_bootstrap_prefetch_bench


def test_bootstrap_prefetch_bench_is_json_serializable_and_records_trace(tmp_path: Path) -> None:
    payload = run_bootstrap_prefetch_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["trace_id"]
    assert reloaded["block_count"] == 4


def test_bootstrap_prefetch_bench_proves_warm_context_reuse_and_lower_token_cost(tmp_path: Path) -> None:
    result = run_bootstrap_prefetch_bench(tmp_path)

    assert result.cold_jobs_started == 1
    assert result.warm_jobs_started == 0
    assert result.warm_status == "warm"
    assert result.block_count == 4
    assert result.warm_total_tokens < result.baseline_total_tokens
