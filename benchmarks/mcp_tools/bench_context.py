"""pytest-based MCP tool benchmarks for the `context` tool.

Run:
    uv run pytest benchmarks/mcp_tools/bench_context.py -v -s
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.context import CONTEXT_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_context")
    repo_root = Path(__file__).resolve().parent.parent.parent
    return configure_benchmark_runtime(root, workspace_root=repo_root)


def _disable_autosync_watcher() -> None:
    """Pre-seed the MCP code-intel engine cache with autosync disabled.

    mode="symbols" context cases construct a CodeContextEngine via
    mcp_server._code_context_engine(), which defaults autosync_enabled=True --
    a watchdog.Observer file-watcher + background resync thread meant for
    long-lived interactive MCP sessions. In this short-lived benchmark process
    that watcher thrashes (inotify watch-limit exhaustion -> polling fallback
    -> native tree-sitter Node objects dropped cross-thread), dominating
    wall-clock (~15s/case instead of near-instant). One-shot processes don't
    need it: gateway/cli/commands/code.py's own _code_context_engine() already
    opts out for the identical reason ("One-shot CLI commands don't need
    background autosync threads"). Pre-seed the cache so
    mcp_server._code_context_engine() reuses this instance instead of
    constructing a fresh autosync-enabled one.
    """
    import atelier.gateway.adapters.mcp_server as mcp_server
    from atelier.core.capabilities.code_context import CodeContextEngine

    resolved = mcp_server._workspace_root().resolve()
    mcp_server._code_engine_cache[str(resolved)] = CodeContextEngine(resolved, autosync_enabled=False)


def _disable_background_worker_spawn() -> None:
    """Neutralize tool_get_context's auto-spawned background bootstrap worker.

    On a cold repo, tool_get_context fires a daemon thread
    (_spawn_worker_if_idle -> _run_worker_tick_safe) that asynchronously
    rebuilds the same code-intel index _preseed_bootstrap() below rebuilds
    synchronously right after the cold-start case. The two race for the same
    on-disk index/lock (IndexLockTimeout), and the daemon thread can crash
    mid-teardown ("cannot schedule new futures after interpreter shutdown")
    once this short-lived benchmark process starts exiting. Neutralized the
    same way tests/gateway/test_mcp_tool_handlers.py does for this exact hazard.
    """
    import atelier.gateway.adapters.mcp_server as mcp_server

    mcp_server._run_worker_tick_safe = lambda root: None


@pytest.fixture(scope="session")
def context_tool_fn(bench_workspace: Path) -> Any:
    from atelier.gateway.adapters.mcp_server import tool_get_context

    _disable_background_worker_spawn()
    _disable_autosync_watcher()
    return tool_get_context


def _preseed_bootstrap(context_tool_fn: Any) -> None:
    import atelier.gateway.adapters.mcp_server as mcp_server
    from atelier.core.service.bootstrap_context import persist_bootstrap_plan
    from atelier.infra.storage.factory import make_memory_store

    atelier_root = Path(os.environ["ATELIER_ROOT"])
    workspace_root = Path(os.environ["ATELIER_WORKSPACE_ROOT"])
    persist_bootstrap_plan(workspace_root, make_memory_store(atelier_root))
    mcp_server._reset_runtime_cache_for_testing()
    payload = context_tool_fn({"task": "Use the warmed bootstrap state", "recall": False})
    assert (
        payload.get("bootstrap", {}).get("status") == "warm"
    ), f"context bootstrap did not reach warm state: {payload}"


@pytest.fixture(scope="session")
def context_bench_results(bench_workspace: Path, context_tool_fn: Any) -> list[CaseResult]:
    results: list[CaseResult] = []
    cold_start_cases = [case for case in CONTEXT_CASES if case.label == "context/cold-start"]
    remaining_cases = [case for case in CONTEXT_CASES if case.label != "context/cold-start"]
    for case in cold_start_cases:
        results.append(run_case(case, context_tool_fn))
    _preseed_bootstrap(context_tool_fn)
    for case in remaining_cases:
        results.append(run_case(case, context_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_context_report(context_bench_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="context", results=context_bench_results)
    print(render_summary([report]))


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", CONTEXT_CASES, ids=lambda c: c.label)
def test_context_op_correctness(case: BenchCase, context_bench_results: list[CaseResult]) -> None:
    result = _find(context_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in CONTEXT_CASES if c.baseline_tokens > 0],
    ids=lambda c: c.label,
)
def test_context_op_saves_tokens(case: BenchCase, context_bench_results: list[CaseResult]) -> None:
    result = _find(context_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert (
        result.atelier_tokens < case.baseline_tokens
    ), f"[{case.label}] no savings: atelier={result.atelier_tokens} >= baseline={case.baseline_tokens}"
