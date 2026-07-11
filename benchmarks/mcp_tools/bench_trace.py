"""pytest-based MCP tool benchmarks for the `trace` tool.

Run:
    uv run pytest benchmarks/mcp_tools/bench_trace.py -v -s
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.trace import TRACE_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_trace")
    return configure_benchmark_runtime(root)


@pytest.fixture(scope="session")
def trace_tool_fn(bench_workspace: Path) -> Any:
    from lemoncrow.gateway.adapters.mcp_server import tool_record_trace

    return tool_record_trace


@pytest.fixture(scope="session")
def trace_bench_results(bench_workspace: Path, trace_tool_fn: Any) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in TRACE_CASES:
        results.append(run_case(case, trace_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_trace_report(trace_bench_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="trace", results=trace_bench_results)
    print(render_summary([report]))


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", TRACE_CASES, ids=lambda c: c.label)
def test_trace_op_correctness(case: BenchCase, trace_bench_results: list[CaseResult]) -> None:
    result = _find(trace_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in TRACE_CASES if c.baseline_tokens > 0],
    ids=lambda c: c.label,
)
def test_trace_op_saves_tokens(case: BenchCase, trace_bench_results: list[CaseResult]) -> None:
    result = _find(trace_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert (
        result.lemoncrow_tokens < case.baseline_tokens
    ), f"[{case.label}] no savings: lemoncrow={result.lemoncrow_tokens} >= baseline={case.baseline_tokens}"
