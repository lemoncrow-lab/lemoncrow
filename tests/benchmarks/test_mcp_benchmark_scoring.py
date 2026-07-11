from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_benchmarks_pkg = types.ModuleType("benchmarks")
_benchmarks_pkg.__path__ = [str(ROOT / "benchmarks")]
_mcp_pkg = types.ModuleType("benchmarks.mcp_tools")
_mcp_pkg.__path__ = [str(ROOT / "benchmarks" / "mcp_tools")]
sys.modules["benchmarks"] = _benchmarks_pkg
sys.modules["benchmarks.mcp_tools"] = _mcp_pkg

harness = _load_module("benchmarks.mcp_tools.harness", ROOT / "benchmarks" / "mcp_tools" / "harness.py")
reporter = _load_module("benchmarks.mcp_tools.reporter", ROOT / "benchmarks" / "mcp_tools" / "reporter.py")

sys.modules.pop("benchmarks.mcp_tools.reporter", None)
sys.modules.pop("benchmarks.mcp_tools.harness", None)
sys.modules.pop("benchmarks.mcp_tools", None)
sys.modules.pop("benchmarks", None)

BenchCase = harness.BenchCase
CaseResult = harness.CaseResult
ToolReport = harness.ToolReport
render_summary = reporter.render_summary


def test_code_matrix_quality_floor_is_enforced() -> None:
    _benchmarks_pkg = types.ModuleType("benchmarks")
    _benchmarks_pkg.__path__ = [str(ROOT / "benchmarks")]
    _mcp_pkg = types.ModuleType("benchmarks.mcp_tools")
    _mcp_pkg.__path__ = [str(ROOT / "benchmarks" / "mcp_tools")]
    _cases_pkg = types.ModuleType("benchmarks.mcp_tools.cases")
    _cases_pkg.__path__ = [str(ROOT / "benchmarks" / "mcp_tools" / "cases")]
    sys.modules["benchmarks"] = _benchmarks_pkg
    sys.modules["benchmarks.mcp_tools"] = _mcp_pkg
    sys.modules["benchmarks.mcp_tools.cases"] = _cases_pkg
    code_cases = _load_module(
        "benchmarks.mcp_tools.cases.code",
        ROOT / "benchmarks" / "mcp_tools" / "cases" / "code.py",
    )
    CODE_CASES = code_cases.CODE_CASES

    sys.modules.pop("benchmarks.mcp_tools.cases.code", None)
    sys.modules.pop("benchmarks.mcp_tools.cases", None)
    sys.modules.pop("benchmarks.mcp_tools", None)
    sys.modules.pop("benchmarks", None)

    assert CODE_CASES, "code benchmark matrix must not be empty"
    assert all(
        case.quality_score >= 0.6 for case in CODE_CASES
    ), "all code benchmark cases must keep quality_score at or above 0.6"


def test_effective_tokens_use_quality_floor() -> None:
    case = BenchCase(op="search", args={}, quality_score=0.4, label="search")
    result = CaseResult(
        case=case,
        response={},
        lemoncrow_tokens=100,
        baseline_tokens=200,
        quality_score=case.quality_score,
        input_file_tokens=0,
        baseline_commands=[],
        spill_probe_tokens=0,
        spill_probe_hits=0,
        elapsed_ms=1.0,
        passed=True,
    )

    assert result.effective_tokens == 250


def test_tool_report_accumulates_effective_tokens() -> None:
    case = BenchCase(op="search", args={}, quality_score=0.5, label="search")
    result = CaseResult(
        case=case,
        response={},
        lemoncrow_tokens=80,
        baseline_tokens=200,
        quality_score=case.quality_score,
        input_file_tokens=0,
        baseline_commands=[],
        spill_probe_tokens=0,
        spill_probe_hits=0,
        elapsed_ms=1.0,
        passed=True,
    )
    report = ToolReport(tool_name="code", results=[result])

    assert report.total_effective_tokens == 160
    assert report.avg_effective_tokens == 160


def test_render_summary_includes_effective_tokens() -> None:
    case = BenchCase(op="search", args={}, quality_score=1.0, label="search")
    result = CaseResult(
        case=case,
        response={},
        lemoncrow_tokens=42,
        baseline_tokens=100,
        quality_score=case.quality_score,
        input_file_tokens=0,
        baseline_commands=[],
        spill_probe_tokens=0,
        spill_probe_hits=0,
        elapsed_ms=1.0,
        passed=True,
    )

    summary = render_summary([ToolReport(tool_name="code", results=[result])])

    assert "effective tokens" in summary
    assert "effective:" in summary
