"""Benchmark tests for the M4 symbol-edit token gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.code_intel.symbol_edit_bench import run_symbol_edit_bench

pytestmark = pytest.mark.slow


def test_symbol_edit_bench_is_json_serializable(tmp_path: Path) -> None:
    payload = run_symbol_edit_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["edited_path"] == "src/service.py#2-3"


def test_symbol_edit_uses_at_most_30pct_of_read_plus_line_edit_baseline(tmp_path: Path) -> None:
    result = run_symbol_edit_bench(tmp_path)

    assert result.symbol_edit_total_tokens > 0
    assert result.symbol_edit_total_tokens <= result.baseline_total_tokens * 0.3
