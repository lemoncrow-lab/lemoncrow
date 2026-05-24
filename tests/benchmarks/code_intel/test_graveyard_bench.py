"""Smoke tests for the deleted-history graveyard benchmark."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.code_intel.graveyard_bench import run_graveyard_bench

pytestmark = pytest.mark.slow


def test_graveyard_bench_smoke(tmp_path: Path) -> None:
    result = run_graveyard_bench(tmp_path)

    assert result.result_count >= 1
    assert result.rename_target == "modern.py"
    assert result.uncached_cache_hit is False
    assert result.cached_cache_hit is True
    assert result.uncached_provenance == "graveyard"
    assert result.cached_provenance == "cached"
    assert result.uncached_total_tokens <= result.budget_tokens


def test_graveyard_bench_result_is_json_serializable(tmp_path: Path) -> None:
    payload = run_graveyard_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["cached_cache_hit"] is True
    assert reloaded["rename_target"] == "modern.py"


def test_graveyard_bench_beats_manual_git_archaeology_budget(tmp_path: Path) -> None:
    result = run_graveyard_bench(tmp_path)

    assert result.deleted_workflow_steps < result.manual_workflow_steps
    assert result.uncached_total_tokens < result.manual_total_tokens
    assert result.cached_total_tokens <= result.uncached_total_tokens
    assert result.deleted_to_manual_ratio < 1.0
