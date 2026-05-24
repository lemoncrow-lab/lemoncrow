"""Benchmark tests for the M3 usages token gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.code_intel.usages_bench import run_usages_bench

pytestmark = pytest.mark.slow


def test_usages_bench_is_json_serializable(tmp_path: Path) -> None:
    payload = run_usages_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["reference_count"] >= 2


def test_usages_uses_at_most_30pct_of_grep_plus_read_baseline(tmp_path: Path) -> None:
    result = run_usages_bench(tmp_path)

    assert result.usages_total_tokens > 0
    assert result.usages_total_tokens <= result.baseline_total_tokens * 0.3
