"""pytest-based MCP tool benchmarks for the `compact` tool.

Run:
    uv run pytest benchmarks/mcp_tools/bench_compact.py -v -s
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.compact import COMPACT_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_compact")
    return configure_benchmark_runtime(root)


@pytest.fixture(scope="session")
def compact_tool_fn(bench_workspace: Path) -> Any:
    from atelier.gateway.adapters import mcp_server
    from atelier.infra.runtime.run_ledger import RunLedger

    def _call(args: dict[str, Any]) -> Any:
        payload = dict(args)
        seed = dict(payload.pop("_seed", {}) or {})
        session_id = str(payload.get("session_id") or seed.get("session_id") or "bench-compact")
        previous = mcp_server._current_ledger
        ledger = RunLedger(session_id=session_id, agent="benchmark", root=bench_workspace)
        ledger.task = str(seed.get("task") or "")
        ledger.token_count = int(seed.get("token_count") or 0)
        ledger.current_plan = list(seed.get("current_plan") or [])
        ledger.files_touched = list(seed.get("files_touched") or [])
        ledger.tools_called = list(seed.get("tools_called") or [])
        ledger.commands_run = list(seed.get("commands_run") or [])
        ledger.tests_run = list(seed.get("tests_run") or [])
        ledger.errors_seen = list(seed.get("errors_seen") or [])
        ledger.repeated_failures = list(seed.get("repeated_failures") or [])
        ledger.verified_facts = list(seed.get("verified_facts") or [])
        ledger.open_questions = list(seed.get("open_questions") or [])
        ledger.active_playbooks = list(seed.get("active_playbooks") or [])
        for event in seed.get("tool_events") or []:
            if isinstance(event, dict):
                ledger.record_tool_call(
                    str(event.get("tool") or "tool"),
                    args=dict(event.get("args") or {}),
                    output=str(event.get("output") or ""),
                )
        for event in seed.get("command_events") or []:
            if isinstance(event, dict):
                ledger.record_command(
                    str(event.get("command") or ""),
                    ok=bool(event.get("ok")),
                    stdout=str(event.get("stdout") or ""),
                    stderr=str(event.get("stderr") or ""),
                )
        mcp_server._current_ledger = ledger
        try:
            return mcp_server.tool_compact(payload)
        finally:
            mcp_server._current_ledger = previous

    return _call


@pytest.fixture(scope="session")
def compact_bench_results(bench_workspace: Path, compact_tool_fn: Any) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in COMPACT_CASES:
        results.append(run_case(case, compact_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_compact_report(compact_bench_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="compact", results=compact_bench_results)
    print(render_summary([report]))


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", COMPACT_CASES, ids=lambda c: c.label)
def test_compact_op_correctness(case: BenchCase, compact_bench_results: list[CaseResult]) -> None:
    result = _find(compact_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in COMPACT_CASES if c.baseline_tokens > 0],
    ids=lambda c: c.label,
)
def test_compact_op_saves_tokens(case: BenchCase, compact_bench_results: list[CaseResult]) -> None:
    result = _find(compact_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert (
        result.atelier_tokens < case.baseline_tokens
    ), f"[{case.label}] no savings: atelier={result.atelier_tokens} >= baseline={case.baseline_tokens}"
