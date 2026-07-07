"""pytest-based benchmark for direct Zoekt-vs-rg token savings.

Run:
    uv run pytest benchmarks/mcp_tools/bench_zoekt.py -v -s
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.zoekt import ZOEKT_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_zoekt")
    return configure_benchmark_runtime(root, workspace_root=Path.cwd())


@pytest.fixture(scope="session")
def zoekt_tool_fn(bench_workspace: Path) -> Any:
    from atelier.infra.code_intel.zoekt.adapter import get_zoekt_supervisor

    repo_root = Path.cwd()
    supervisor = get_zoekt_supervisor(repo_root)
    health = supervisor.health()
    if not health.ok:
        pytest.skip(f"Zoekt runtime unavailable for benchmark: {health.reason}")

    def _run(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            return {"error": "empty query"}
        search_path = str(args.get("search_path", "src")).strip() or "src"
        max_files = int(args.get("max_files", 20))
        max_chars_per_file = int(args.get("max_chars_per_file", 500))
        result = supervisor.search(
            query=query,
            search_path=search_path,
            max_files=max_files,
            max_chars_per_file=max_chars_per_file,
            include_outline=False,
        )
        files: list[dict[str, Any]] = []
        for match in result.matches[:max_files]:
            snippets = [
                {
                    "line_start": snippet.line_start,
                    "line_end": snippet.line_end,
                    "text": snippet.text,
                }
                for snippet in match.snippets[:2]
            ]
            files.append({"path": match.path, "snippets": snippets})
        return {
            "backend": result.backend,
            "file_count": len(result.matches),
            "total_tokens": result.total_tokens,
            "index_age_seconds": result.index_age_seconds,
            "files": files,
        }

    return _run


@pytest.fixture(scope="session")
def zoekt_bench_results(bench_workspace: Path, zoekt_tool_fn: Any) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in ZOEKT_CASES:
        results.append(run_case(case, zoekt_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_zoekt_report(zoekt_bench_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="zoekt-vs-rg", results=zoekt_bench_results)
    print(render_summary([report]))


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for result in results:
        if result.case.label == label:
            return result
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", ZOEKT_CASES, ids=lambda case: case.label)
def test_zoekt_case_correctness(case: BenchCase, zoekt_bench_results: list[CaseResult]) -> None:
    result = _find(zoekt_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize("case", ZOEKT_CASES, ids=lambda case: case.label)
def test_zoekt_case_saves_tokens(case: BenchCase, zoekt_bench_results: list[CaseResult]) -> None:
    result = _find(zoekt_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert result.baseline_tokens > 0, f"[{case.label}] measured baseline is zero"
    assert (
        result.atelier_tokens < result.baseline_tokens
    ), f"[{case.label}] no savings: zoekt={result.atelier_tokens} >= rg_baseline={result.baseline_tokens}"
