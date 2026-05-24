"""Benchmark tests for the Phase 6 workspace routing smoke suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.code_intel.workspace_bench import run_workspace_bench

pytestmark = pytest.mark.slow


def test_workspace_bench_is_json_serializable_and_records_trace(tmp_path: Path) -> None:
    payload = run_workspace_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["trace_id"]
    assert reloaded["union_item_count"] == 2


def test_workspace_bench_proves_union_and_repo_filter_routing(tmp_path: Path) -> None:
    result = run_workspace_bench(tmp_path)

    assert result.union_item_count == 2
    assert result.filtered_item_count == 1
    assert result.union_repo_names == ["atelier", "billing"]
    assert result.filtered_repo_names == ["billing"]
