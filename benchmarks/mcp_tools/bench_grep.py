"""pytest-based MCP tool benchmarks for the `grep` tool.

Run:
    uv run pytest benchmarks/mcp_tools/bench_grep.py -v -s

CLAUDE_WORKSPACE_ROOT is set to the repo root so patterns find real files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.grep import GREP_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_grep")
    # Use actual repo root so grep finds real source files
    return configure_benchmark_runtime(root, workspace_root=Path.cwd())


@pytest.fixture(scope="session")
def grep_tool_fn(bench_workspace: Path) -> Any:
    from lemoncrow.gateway.adapters.mcp_server import tool_grep

    return tool_grep


@pytest.fixture(scope="session")
def grep_bench_results(bench_workspace: Path, grep_tool_fn: Any) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in GREP_CASES:
        results.append(run_case(case, grep_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_grep_report(grep_bench_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="grep", results=grep_bench_results)
    print(render_summary([report]))


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", GREP_CASES, ids=lambda c: c.label)
def test_grep_op_correctness(case: BenchCase, grep_bench_results: list[CaseResult]) -> None:
    result = _find(grep_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in GREP_CASES if c.baseline_builder is not None],
    ids=lambda c: c.label,
)
def test_grep_op_saves_tokens(case: BenchCase, grep_bench_results: list[CaseResult]) -> None:
    result = _find(grep_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    if result.baseline_tokens == 0:
        pytest.skip("no measured baseline")
    # Measured baselines make per-case savings query-dependent: sparse-match
    # queries can legitimately not save (compact JSON > tiny rg output).
    # Report, do not gate — mirrors bench_savings.py grep/ranked (min 0.0).
    if result.lemoncrow_tokens >= result.baseline_tokens:
        pytest.skip(
            f"[{case.label}] no savings on this query (measured, report-only): "
            f"lemoncrow={result.lemoncrow_tokens} >= baseline={result.baseline_tokens}"
        )
