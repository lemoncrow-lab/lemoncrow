"""pytest-based MCP route tool benchmark.

Run:
    uv run pytest benchmarks/mcp_tools/bench_route.py -v -s
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.route import ROUTE_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary

# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------

ROUTE_CASES_LOCAL = ROUTE_CASES


def _setup_env(root: Path) -> None:
    configure_benchmark_runtime(root)
    os.environ["ATELIER_MODEL"] = "claude-sonnet-4.6"
    os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "test-anthropic-key")
    os.environ["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "test-openai-key")
    os.environ["GOOGLE_API_KEY"] = os.environ.get("GOOGLE_API_KEY", "test-google-key")

    from atelier.core.capabilities.cross_vendor_routing.configuration import (
        RouteConfig,
        save_route_config,
    )

    save_route_config(root / ".atelier", RouteConfig(enabled_vendors=["anthropic", "openai", "google"]))

    import atelier.gateway.adapters.mcp_server as m

    m._current_ledger = None
    m._client_sampling_supported = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_route")
    _setup_env(root)
    return root


@pytest.fixture(scope="session")
def route_tool_fn(bench_workspace: Path) -> Any:
    from atelier.gateway.adapters.mcp_server import tool_route

    return tool_route


@pytest.fixture(scope="session")
def route_results(bench_workspace: Path, route_tool_fn: Any) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in ROUTE_CASES_LOCAL:
        results.append(run_case(case, route_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_route_report(route_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="route", results=route_results)
    print(render_summary([report]))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", ROUTE_CASES_LOCAL, ids=lambda c: c.label)
def test_route_op_correctness(case: BenchCase, route_results: list[CaseResult]) -> None:
    result = _find(route_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in ROUTE_CASES_LOCAL if c.baseline_tokens > 0],
    ids=lambda c: c.label,
)
def test_route_op_saves_tokens(case: BenchCase, route_results: list[CaseResult]) -> None:
    result = _find(route_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert (
        result.atelier_tokens < case.baseline_tokens
    ), f"[{case.label}] no savings: atelier={result.atelier_tokens} >= baseline={case.baseline_tokens}"
