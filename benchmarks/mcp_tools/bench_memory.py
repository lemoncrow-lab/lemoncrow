"""pytest-based MCP tool benchmarks.

Run:
    uv run pytest benchmarks/mcp_tools/bench_memory.py -v

Each test validates correctness for one memory op AND records savings data.
A session-scoped fixture runs the full benchmark and prints the report once.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools.cases.memory import MEMORY_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_memory")
    os.environ["CLAUDE_WORKSPACE_ROOT"] = str(root)
    os.environ["ATELIER_MEM_ROOT"] = str(root / "mem")
    return root


@pytest.fixture(scope="session")
def memory_tool_fn(bench_workspace: Path) -> Any:
    from atelier.gateway.adapters.mcp_server import tool_memory

    return tool_memory


@pytest.fixture(scope="session")
def memory_bench_results(bench_workspace: Path, memory_tool_fn: Any) -> list[CaseResult]:
    """Run all memory cases once and return results (order-dependent — state builds up)."""
    results: list[CaseResult] = []
    for case in MEMORY_CASES:
        results.append(run_case(case, memory_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_memory_report(memory_bench_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="memory", results=memory_bench_results)
    print(render_summary([report]))


# ---------------------------------------------------------------------------
# Per-op tests (parametrize by case label for clear output)
# ---------------------------------------------------------------------------


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", MEMORY_CASES, ids=lambda c: c.label)
def test_memory_op_correctness(
    case: BenchCase,
    memory_bench_results: list[CaseResult],
) -> None:
    """Assert each memory op passes its correctness checks."""
    result = _find(memory_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in MEMORY_CASES if c.baseline_tokens > 0],
    ids=lambda c: c.label,
)
def test_memory_op_saves_tokens(
    case: BenchCase,
    memory_bench_results: list[CaseResult],
) -> None:
    """Assert Atelier response is smaller than the baseline token estimate."""
    result = _find(memory_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert result.atelier_tokens < case.baseline_tokens, (
        f"[{case.label}] no savings: atelier={result.atelier_tokens} "
        f">= baseline={case.baseline_tokens}"
    )
