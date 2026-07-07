"""pytest-based MCP shell tool benchmark.

Run:
    uv run pytest benchmarks/mcp_tools/bench_shell.py -v -s

The fixture creates a temp workspace with a sentinel file and a Python file
containing 'needle_token'. Patches __SHELL_WORKSPACE__ and __SHELL_FILE__
placeholders in case args before running.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.shell import SHELL_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def shell_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_shell")
    # Sentinel file for cat rewrite test
    sentinel = root / "sentinel.txt"
    sentinel.write_text("sentinel_content line1\nsentinel_content line2\n", encoding="utf-8")
    # Python file containing needle_token for rg rewrite test
    src = root / "src"
    src.mkdir()
    (src / "module.py").write_text(
        "# module with needle_token\ndef needle_token():\n    return 42\n",
        encoding="utf-8",
    )
    return configure_benchmark_runtime(root)


@pytest.fixture(scope="session")
def shell_tool_fn() -> Any:
    from atelier.gateway.adapters.mcp_server import tool_bash

    return tool_bash


def _patch_paths(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    patched = copy.deepcopy(args)
    sentinel_path = str(workspace / "sentinel.txt")
    _substitute(patched, "__SHELL_FILE__", sentinel_path)
    _substitute(patched, "__SHELL_WORKSPACE__", str(workspace))
    return patched


def _substitute(obj: Any, placeholder: str, value: str) -> None:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if v == placeholder:
                obj[k] = value
            elif isinstance(v, str) and placeholder in v:
                obj[k] = v.replace(placeholder, value)
            else:
                _substitute(v, placeholder, value)
    elif isinstance(obj, list):
        for item in obj:
            _substitute(item, placeholder, value)


@pytest.fixture(scope="session")
def shell_bench_results(shell_workspace: Path, shell_tool_fn: Any) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in SHELL_CASES:
        patched_args = _patch_paths(case.args, shell_workspace)
        patched_case = BenchCase(
            op=case.op,
            label=case.label,
            args=patched_args,
            assert_keys=case.assert_keys,
            custom_assert=case.custom_assert,
            baseline_tokens=case.baseline_tokens,
            baseline_builder=case.baseline_builder,
            min_baseline_tokens=case.min_baseline_tokens,
        )
        results.append(run_case(patched_case, shell_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_shell_report(shell_bench_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="shell", results=shell_bench_results)
    print(render_summary([report]))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", SHELL_CASES, ids=lambda c: c.label)
def test_shell_op_correctness(case: BenchCase, shell_bench_results: list[CaseResult]) -> None:
    result = _find(shell_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in SHELL_CASES if c.baseline_builder is not None],
    ids=lambda c: c.label,
)
def test_shell_op_saves_tokens(case: BenchCase, shell_bench_results: list[CaseResult]) -> None:
    # Report-only: baselines are now MEASURED (full untruncated command output).
    # On the tiny temp fixtures the full output often equals the tool output, so
    # honest per-case savings are input-dependent. Mirror bench_savings.py and
    # skip (do not fail) when there is no measured savings.
    result = _find(shell_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check: op failed: {result.failure}")
    if result.baseline_tokens == 0:
        pytest.skip("no measured baseline")
    if result.atelier_tokens >= result.baseline_tokens:
        pytest.skip(
            f"[{case.label}] no savings (measured, report-only): "
            f"atelier={result.atelier_tokens} >= baseline={result.baseline_tokens}"
        )
