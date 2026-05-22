"""Benchmark tests for the Phase 6 external scope routing smoke suite."""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.code_intel.external_scope_bench import run_external_scope_bench


def test_external_scope_bench_is_json_serializable_and_records_trace(tmp_path: Path) -> None:
    payload = run_external_scope_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["trace_id"]
    assert reloaded["external_item_count"] == 1


def test_external_scope_bench_proves_repo_default_exclusion_and_edit_rejection(tmp_path: Path) -> None:
    result = run_external_scope_bench(tmp_path)

    assert result.repo_item_count == 0
    assert result.external_item_count == 1
    assert result.edit_error == "external_symbol_edit_not_allowed"
