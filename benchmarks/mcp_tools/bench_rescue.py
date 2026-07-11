"""pytest-based MCP rescue tool benchmark.

Run:
    uv run pytest benchmarks/mcp_tools/bench_rescue.py -v -s
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from lemoncrow.gateway.cli.progress import ProgressReporter

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.rescue import RESCUE_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("bench_rescue")


def make_rescue_tool_fn(base_root: Path) -> Any:
    from lemoncrow.core.foundation.models import Playbook, Trace
    from lemoncrow.core.foundation.store import ContextStore
    from lemoncrow.gateway.adapters import mcp_server

    repo_root = Path(__file__).resolve().parents[2]

    def _call(args: dict[str, Any]) -> Any:
        payload = dict(args)
        seed_playbooks = list(payload.pop("_seed_playbooks", []) or [])
        seed_traces = list(payload.pop("_seed_traces", []) or [])
        case_root = base_root / payload.get("task", "case").replace("/", "-").replace(" ", "-")[:80]
        configure_benchmark_runtime(case_root, workspace_root=repo_root)
        mcp_server._reset_runtime_cache_for_testing()
        store = ContextStore(Path(mcp_server._lemoncrow_root()))
        store.init()
        for block in seed_playbooks:
            store.upsert_block(Playbook.model_validate(block), write_markdown=False)
        for trace in seed_traces:
            store.record_trace(Trace.model_validate(trace), write_json=False)
        return mcp_server.tool_rescue_failure(payload)

    return _call


def run_rescue_suite(base_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    tool_fn = make_rescue_tool_fn(base_root)
    results: list[CaseResult] = []
    for case in RESCUE_CASES:
        if progress is not None:
            progress.phase("running MCP tool benchmark", current=f"rescue {case.label}")
        results.append(run_case(case, tool_fn))
        if progress is not None:
            progress.step("running MCP tool benchmark", current=f"rescue {case.label}")
    return ToolReport(tool_name="rescue", results=results)


@pytest.fixture(scope="session")
def rescue_report(bench_workspace: Path) -> ToolReport:
    return run_rescue_suite(bench_workspace)


@pytest.fixture(scope="session", autouse=True)
def print_rescue_report(rescue_report: ToolReport) -> None:
    print(render_summary([rescue_report]))


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for result in results:
        if result.case.label == label:
            return result
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", RESCUE_CASES, ids=lambda c: c.label)
def test_rescue_correctness(case: BenchCase, rescue_report: ToolReport) -> None:
    result = _find(rescue_report.results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize("case", [c for c in RESCUE_CASES if c.baseline_tokens > 0], ids=lambda c: c.label)
def test_rescue_saves_tokens(case: BenchCase, rescue_report: ToolReport) -> None:
    result = _find(rescue_report.results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert (
        result.lemoncrow_tokens < result.baseline_tokens
    ), f"[{case.label}] no savings: lemoncrow={result.lemoncrow_tokens} >= baseline={result.baseline_tokens}"
