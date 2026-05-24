"""Benchmark tests for the M7 symbol recall token gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.code_intel.recall_symbol_bench import run_recall_symbol_bench

pytestmark = pytest.mark.slow


def test_recall_symbol_bench_is_json_serializable(tmp_path: Path) -> None:
    payload = run_recall_symbol_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["default_included"] == ["definition", "memory"]


def test_recall_symbol_default_bundle_stays_under_budget_and_smaller_than_expanded_and_manual(tmp_path: Path) -> None:
    result = run_recall_symbol_bench(tmp_path)

    assert result.definition_preserved is True
    assert result.default_within_budget is True
    assert result.default_total_tokens < result.expanded_total_tokens
    assert result.default_total_tokens < result.baseline_total_tokens
    assert result.default_total_tokens <= result.expanded_total_tokens * 0.8
