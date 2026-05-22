"""Smoke tests for the Phase 5 Zoekt public-search benchmark."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from benchmarks.code_intel.zoekt_bench import run_zoekt_bench

pytestmark = pytest.mark.slow
skip_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker is required for the real Zoekt benchmark")


@skip_docker
def test_zoekt_bench_is_json_serializable_and_records_trace(tmp_path: Path) -> None:
    payload = run_zoekt_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["trace_id"]
    assert reloaded["backend"] == "zoekt"


@skip_docker
def test_zoekt_bench_meets_m16_speed_and_budget_gates(tmp_path: Path) -> None:
    result = run_zoekt_bench(tmp_path)

    assert result.backend == "zoekt"
    assert result.index_age_seconds is not None
    assert result.speedup_ratio >= 10.0
    assert result.within_budget is True
    assert result.zoekt_total_tokens <= result.baseline_total_tokens
