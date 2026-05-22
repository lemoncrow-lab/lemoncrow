"""Benchmark tests for the M5 structural-pattern token gate."""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.code_intel.pattern_bench import run_pattern_bench


def test_pattern_bench_is_json_serializable(tmp_path: Path) -> None:
    payload = run_pattern_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["files_changed"] == ["src/http.py", "src/worker.py"]


def test_pattern_rewrite_uses_at_most_30pct_of_search_read_edit_baseline(tmp_path: Path) -> None:
    result = run_pattern_bench(tmp_path)

    assert result.pattern_total_tokens > 0
    assert result.pattern_total_tokens <= result.baseline_total_tokens * 0.3
