"""Benchmark tests for the M12 cost-discipline partial close."""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.slow

import json
from pathlib import Path

from benchmarks.code_intel.cost_discipline import run_cost_discipline_bench
from atelier.gateway.adapters.mcp_server import tool_code


def test_cost_discipline_bench_is_json_serializable(tmp_path: Path) -> None:
    payload = run_cost_discipline_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["aggregate_baseline_tokens"] > reloaded["aggregate_current_tokens"]
    assert reloaded["cache_status_total_tokens"] > 0
    assert reloaded["historical_baseline_tokens"] > reloaded["historical_current_tokens"]
    assert reloaded["blame_baseline_tokens"] > reloaded["blame_current_tokens"]


def test_shipped_phase2_flows_stay_within_30pct_of_baseline(tmp_path: Path) -> None:
    result = run_cost_discipline_bench(tmp_path)

    assert result.search_current_tokens < result.search_baseline_tokens
    assert result.pattern_current_tokens < result.pattern_baseline_tokens
    assert result.historical_current_tokens < result.historical_baseline_tokens
    assert result.blame_current_tokens < result.blame_baseline_tokens
    assert result.aggregate_current_tokens <= result.aggregate_baseline_tokens * 0.3


def test_phase10_token_caps_matrix(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "pkg" / "worker.py").write_text(
        "def run_command(cmd: str) -> int:\n"
        "    return len(cmd)\n\n"
        "def classify_command(cmd: str) -> str:\n"
        "    run_command(cmd)\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (repo / "src" / "pkg" / "server.py").write_text(
        "from pkg.worker import run_command\n\n"
        "def _run_shell_tool(command: str) -> int:\n"
        "    return run_command(command)\n",
        encoding="utf-8",
    )

    tool_code({"op": "index", "repo_root": str(repo), "include_globs": ["src/**/*.py"], "budget_tokens": 99_999})

    caps = {
        "cache_status": 50,
        "index": 80,
        "search": 300,
        "symbol": 300,
        "outline": 150,
        "pattern": 800,
        "callers": 700,
        "callees": 300,
        "usages": 700,
        "impact": 150,
        "context": 2400,
    }
    payloads = {
        "cache_status": {"op": "cache_status", "repo_root": str(repo), "budget_tokens": 99_999},
        "index": {"op": "index", "repo_root": str(repo), "include_globs": ["src/**/*.py"], "budget_tokens": 99_999},
        "search": {"op": "search", "repo_root": str(repo), "query": "run_command", "limit": 10, "budget_tokens": 99_999},
        "symbol": {"op": "symbol", "repo_root": str(repo), "symbol_name": "run_command", "file_path": "src/pkg/worker.py", "budget_tokens": 99_999},
        "outline": {"op": "outline", "repo_root": str(repo), "path": "src/pkg/worker.py", "budget_tokens": 99_999},
        "pattern": {"op": "pattern", "repo_root": str(repo), "pattern": "run_command($$$)", "language": "python", "budget_tokens": 99_999},
        "callers": {"op": "callers", "repo_root": str(repo), "symbol_name": "run_command", "path": "src/pkg/worker.py", "budget_tokens": 99_999},
        "callees": {"op": "callees", "repo_root": str(repo), "symbol_name": "classify_command", "path": "src/pkg/worker.py", "budget_tokens": 99_999},
        "usages": {"op": "usages", "repo_root": str(repo), "symbol_name": "run_command", "path": "src/pkg/worker.py", "group_by": "none", "budget_tokens": 99_999},
        "impact": {"op": "impact", "repo_root": str(repo), "path": "src/pkg/worker.py", "budget_tokens": 99_999},
        "context": {"op": "context", "repo_root": str(repo), "task": "trace run_command usage", "max_symbols": 8, "budget_tokens": 99_999},
    }

    for operation, request in payloads.items():
        payload = tool_code(request)
        assert int(payload.get("total_tokens", 0) or 0) <= caps[operation], operation
