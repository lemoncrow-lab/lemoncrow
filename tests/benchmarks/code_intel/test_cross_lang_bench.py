"""Benchmark tests for the Phase 5 cross-language smoke suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.code_intel.cross_lang_bench import run_cross_lang_bench

pytestmark = pytest.mark.slow


def test_cross_lang_bench_is_json_serializable_and_records_trace(tmp_path: Path) -> None:
    payload = run_cross_lang_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["trace_id"]
    assert reloaded["resolved_edge_seen"] is True


def test_cross_lang_bench_proves_resolved_and_unresolved_edges_with_budget_discipline(tmp_path: Path) -> None:
    result = run_cross_lang_bench(tmp_path)

    assert result.resolved_edge_seen is True
    assert result.unresolved_edge_seen is True
    assert result.reference_count >= 2
    assert result.within_budget is True
    assert result.combined_total_tokens < result.baseline_total_tokens
