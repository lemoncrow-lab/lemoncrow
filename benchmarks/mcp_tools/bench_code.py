"""pytest-based MCP code tool benchmark.

Run:
    uv run pytest benchmarks/mcp_tools/bench_code.py -v -s

Exercises all code tool operations benchmarked here (including explicit index build)
against the real Atelier codebase.
The first run builds the SCIP index (~10-30 s); subsequent runs are cached.

Baseline comparison: each case has a `baseline_tokens` estimate of what
a naive grep / read approach would require.  Atelier should be ≤ baseline
for nearly all symbol-level operations.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.code import CODE_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def code_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Point workspace reads at the repo while keeping runtime state isolated."""
    root = tmp_path_factory.mktemp("bench_code")
    repo_root = Path(__file__).resolve().parent.parent.parent
    configure_benchmark_runtime(root, workspace_root=repo_root)
    return repo_root


@pytest.fixture(scope="session")
def code_tool_fn() -> Any:
    from atelier.gateway.adapters.mcp_server import tool_code

    return tool_code


@pytest.fixture(scope="session")
def code_bench_results(code_workspace: Path, code_tool_fn: Any) -> list[CaseResult]:
    """Run all code benchmark cases once and cache results for the session."""
    # Avoid stale retrieval-cache artifacts masking worst-case behavior between runs.
    with contextlib.suppress(Exception):
        code_tool_fn({"op": "cache_invalidate", "budget_tokens": 2000})
    results: list[CaseResult] = []
    for case in CODE_CASES:
        results.append(run_case(case, code_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_code_report(code_bench_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="code", results=code_bench_results)
    print(render_summary([report]))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CODE_CASES, ids=lambda c: c.label)
def test_code_op_correctness(case: BenchCase, code_bench_results: list[CaseResult]) -> None:
    result = _find(code_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in CODE_CASES if c.baseline_tokens > 0 or c.baseline_builder is not None],
    ids=lambda c: c.label,
)
def test_code_op_saves_tokens(case: BenchCase, code_bench_results: list[CaseResult]) -> None:
    result = _find(code_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert result.baseline_tokens > 0, f"[{case.label}] baseline is zero"
    assert (
        result.atelier_tokens < result.baseline_tokens
    ), f"[{case.label}] no savings: atelier={result.atelier_tokens} >= baseline={result.baseline_tokens}"
