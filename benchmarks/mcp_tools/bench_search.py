"""pytest-based MCP tool benchmarks for the `search` tool.

Run:
    uv run pytest benchmarks/mcp_tools/bench_search.py -v -s
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools.cases.search import SEARCH_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_search")
    os.environ["CLAUDE_WORKSPACE_ROOT"] = str(Path.cwd())
    os.environ["ATELIER_MEM_ROOT"] = str(root / "mem")
    return root


@pytest.fixture(scope="session")
def search_tool_fn(bench_workspace: Path) -> Any:
    from atelier.gateway.adapters.mcp_server import tool_smart_search

    return tool_smart_search


@pytest.fixture(scope="session")
def search_bench_results(bench_workspace: Path, search_tool_fn: Any) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in SEARCH_CASES:
        results.append(run_case(case, search_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_search_report(search_bench_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="search", results=search_bench_results)
    print(render_summary([report]))


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", SEARCH_CASES, ids=lambda c: c.label)
def test_search_op_correctness(case: BenchCase, search_bench_results: list[CaseResult]) -> None:
    result = _find(search_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in SEARCH_CASES if c.baseline_tokens > 0],
    ids=lambda c: c.label,
)
def test_search_op_saves_tokens(case: BenchCase, search_bench_results: list[CaseResult]) -> None:
    result = _find(search_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert result.atelier_tokens < case.baseline_tokens, (
        f"[{case.label}] no savings: atelier={result.atelier_tokens} >= baseline={case.baseline_tokens}"
    )
