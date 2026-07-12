"""pytest-based MCP verify tool benchmark.

Run:
    uv run pytest benchmarks/mcp_tools/bench_verify.py -v -s
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from lemoncrow.gateway.cli.progress import ProgressReporter

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.verify import VERIFY_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("bench_verify")


def make_verify_tool_fn(base_root: Path) -> Any:
    from lemoncrow.core.foundation.models import Rubric
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.infra.storage.factory import create_store

    repo_root = Path(__file__).resolve().parents[2]

    def _call(args: dict[str, Any]) -> Any:
        payload = dict(args)
        rubric_payload = payload.pop("_seed_rubric", None)
        case_root = base_root / payload.get("rubric_id", "case").replace("/", "-")[:80]
        configure_benchmark_runtime(case_root, workspace_root=repo_root)
        mcp_server._reset_runtime_cache_for_testing()
        store = create_store(Path(mcp_server._lemoncrow_root()))
        store.init()
        if isinstance(rubric_payload, dict):
            store.knowledge.upsert_rubric(Rubric.model_validate(rubric_payload), write_yaml=False)
        return mcp_server.tool_run_rubric_gate(payload)

    return _call


def run_verify_suite(base_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    tool_fn = make_verify_tool_fn(base_root)
    results: list[CaseResult] = []
    for case in VERIFY_CASES:
        if progress is not None:
            progress.phase("running MCP tool benchmark", current=f"verify {case.label}")
        results.append(run_case(case, tool_fn))
        if progress is not None:
            progress.step("running MCP tool benchmark", current=f"verify {case.label}")
    return ToolReport(tool_name="verify", results=results)


@pytest.fixture(scope="session")
def verify_report(bench_workspace: Path) -> ToolReport:
    return run_verify_suite(bench_workspace)


@pytest.fixture(scope="session", autouse=True)
def print_verify_report(verify_report: ToolReport) -> None:
    print(render_summary([verify_report]))


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for result in results:
        if result.case.label == label:
            return result
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", VERIFY_CASES, ids=lambda c: c.label)
def test_verify_correctness(case: BenchCase, verify_report: ToolReport) -> None:
    result = _find(verify_report.results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize("case", [c for c in VERIFY_CASES if c.baseline_tokens > 0], ids=lambda c: c.label)
def test_verify_saves_tokens(case: BenchCase, verify_report: ToolReport) -> None:
    result = _find(verify_report.results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert result.lemoncrow_tokens < result.baseline_tokens, (
        f"[{case.label}] no savings: lemoncrow={result.lemoncrow_tokens} >= baseline={result.baseline_tokens}"
    )
