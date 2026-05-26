"""pytest-based MCP edit tool benchmark.

Run:
    uv run pytest benchmarks/mcp_tools/bench_edit.py -v -s

The edit tool writes real files. The fixture creates a temp workspace with
sentinel files and patches __EDIT_FILE_A/B/NEW__ placeholders in each case's
args before running, so every run starts from a fresh known state.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.edit import EDIT_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport
from benchmarks.mcp_tools.reporter import render_summary

# Sentinel content for scratch files
_FILE_A_CONTENT = "# scratch file A\nPLACEHOLDER_ALPHA = 1\n"
_FILE_B_CONTENT = "# scratch file B\nPLACEHOLDER_BETA = 2\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def edit_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_edit")
    return configure_benchmark_runtime(root)


@pytest.fixture(scope="session")
def edit_tool_fn(edit_workspace: Path) -> Any:
    from atelier.gateway.adapters.mcp_server import tool_smart_edit

    return tool_smart_edit


def _patch_paths(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Replace __EDIT_FILE_*__ placeholders with real absolute paths."""
    patched = copy.deepcopy(args)
    subs = {
        "__EDIT_FILE_A__": str(workspace / "file_a.py"),
        "__EDIT_FILE_B__": str(workspace / "file_b.py"),
        "__EDIT_FILE_NEW__": str(workspace / "file_new.py"),
    }
    for edit in patched.get("edits", []):
        fp = edit.get("file_path", "")
        if fp in subs:
            edit["file_path"] = subs[fp]
    return patched


def _reset_scratch_files(workspace: Path) -> None:
    """Write known initial content to scratch files before each case."""
    (workspace / "file_a.py").write_text(_FILE_A_CONTENT, encoding="utf-8")
    (workspace / "file_b.py").write_text(_FILE_B_CONTENT, encoding="utf-8")
    new_file = workspace / "file_new.py"
    if new_file.exists():
        new_file.unlink()


def _run_edit_case(case: BenchCase, tool_fn: Any, workspace: Path) -> CaseResult:
    """Run one edit case, resetting scratch files first and patching paths."""
    _reset_scratch_files(workspace)
    patched_args = _patch_paths(case.args, workspace)
    patched_case = BenchCase(
        op=case.op,
        label=case.label,
        args=patched_args,
        assert_keys=case.assert_keys,
        custom_assert=case.custom_assert,
        baseline_tokens=case.baseline_tokens,
    )
    from benchmarks.mcp_tools.harness import run_case

    return run_case(patched_case, tool_fn)


@pytest.fixture(scope="session")
def edit_bench_results(edit_workspace: Path, edit_tool_fn: Any) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in EDIT_CASES:
        results.append(_run_edit_case(case, edit_tool_fn, edit_workspace))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_edit_report(edit_bench_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="edit", results=edit_bench_results)
    print(render_summary([report]))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", EDIT_CASES, ids=lambda c: c.label)
def test_edit_op_correctness(case: BenchCase, edit_bench_results: list[CaseResult]) -> None:
    result = _find(edit_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in EDIT_CASES if c.baseline_tokens > 0],
    ids=lambda c: c.label,
)
def test_edit_op_saves_tokens(case: BenchCase, edit_bench_results: list[CaseResult]) -> None:
    result = _find(edit_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert (
        result.atelier_tokens < case.baseline_tokens
    ), f"[{case.label}] no savings: atelier={result.atelier_tokens} >= baseline={case.baseline_tokens}"
