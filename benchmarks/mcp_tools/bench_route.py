"""pytest-based MCP route tool benchmark.

Run:
    uv run pytest benchmarks/mcp_tools/bench_route.py -v -s

Tests split into two groups:
  - decide/* and spawn/directive-no-cli: shutil.which mocked to None
  - spawn/subprocess-live: real claude CLI, ATELIER_API_KEY must be set
    (skip if claude binary missing or no API key)
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from benchmarks.mcp_tools.cases.route import ROUTE_CASES, _assert_spawn_subprocess
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------

MOCKED_CASES = [c for c in ROUTE_CASES if c.label != "spawn/subprocess-live"]
LIVE_CASE = next(c for c in ROUTE_CASES if c.label == "spawn/subprocess-live")


def _setup_env(root: Path) -> None:
    os.environ["ATELIER_ROOT"] = str(root / ".atelier")
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
# Fixtures — mocked (decide + directive-no-cli)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def bench_workspace_mocked(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_route_mocked")
    _setup_env(root)
    return root


@pytest.fixture(scope="session")
def route_tool_fn(bench_workspace_mocked: Path) -> Any:
    from atelier.gateway.adapters.mcp_server import tool_route
    return tool_route


@pytest.fixture(scope="session")
def mocked_results(bench_workspace_mocked: Path, route_tool_fn: Any) -> list[CaseResult]:
    results: list[CaseResult] = []
    with patch("shutil.which", return_value=None):
        for case in MOCKED_CASES:
            results.append(run_case(case, route_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_route_report(mocked_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="route", results=mocked_results)
    print(render_summary([report]))


# ---------------------------------------------------------------------------
# Fixtures — live subprocess
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def bench_workspace_live(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_route_live")
    _setup_env(root)
    return root


@pytest.fixture(scope="session")
def live_spawn_result(bench_workspace_live: Path) -> CaseResult | None:
    claude_path = shutil.which("claude")
    if not claude_path:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "test-anthropic-key":
        return None
    from atelier.gateway.adapters.mcp_server import tool_route
    return run_case(LIVE_CASE, tool_route)


# ---------------------------------------------------------------------------
# Tests — mocked cases
# ---------------------------------------------------------------------------

def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", MOCKED_CASES, ids=lambda c: c.label)
def test_route_op_correctness(case: BenchCase, mocked_results: list[CaseResult]) -> None:
    result = _find(mocked_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in MOCKED_CASES if c.baseline_tokens > 0],
    ids=lambda c: c.label,
)
def test_route_op_saves_tokens(case: BenchCase, mocked_results: list[CaseResult]) -> None:
    result = _find(mocked_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert result.atelier_tokens < case.baseline_tokens, (
        f"[{case.label}] no savings: atelier={result.atelier_tokens} >= baseline={case.baseline_tokens}"
    )


# ---------------------------------------------------------------------------
# Tests — live subprocess spawn
# ---------------------------------------------------------------------------

def test_route_spawn_subprocess_live(live_spawn_result: CaseResult | None) -> None:
    """Real end-to-end: claude CLI fires a subprocess and returns handled=true."""
    if live_spawn_result is None:
        pytest.skip("claude CLI not found or ANTHROPIC_API_KEY not set — skipping live spawn test")
    assert live_spawn_result.passed, (
        f"live spawn FAILED: {live_spawn_result.failure}\nresponse={live_spawn_result.response}"
    )
    payload = live_spawn_result.response
    assert payload.get("handled") is True, f"expected handled=true from live spawn, got: {payload}"
    assert payload.get("spawn_method") == "cli_subprocess", f"unexpected spawn_method: {payload}"
    assert "response" in payload, f"missing 'response' in live spawn result: {payload}"
    print(f"\n✓ live spawn: response={payload.get('response')!r}  cost=${payload.get('cost_usd', 0):.5f}  turns={payload.get('num_turns')}")

