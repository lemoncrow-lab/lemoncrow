from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _ensure_benchmarks_package() -> None:
    benchmarks_pkg = types.ModuleType("benchmarks")
    benchmarks_pkg.__path__ = [str(ROOT / "benchmarks")]
    mcp_pkg = types.ModuleType("benchmarks.mcp_tools")
    mcp_pkg.__path__ = [str(ROOT / "benchmarks" / "mcp_tools")]
    sys.modules["benchmarks"] = benchmarks_pkg
    sys.modules["benchmarks.mcp_tools"] = mcp_pkg


def _load(module_name: str) -> ModuleType:
    _ensure_benchmarks_package()
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def test_generated_repo_backed_case_counts() -> None:
    read_module = _load("benchmarks.mcp_tools.cases.read")
    search_module = _load("benchmarks.mcp_tools.cases.search")
    grep_module = _load("benchmarks.mcp_tools.cases.grep")
    context_module = _load("benchmarks.mcp_tools.cases.context")
    code_module = _load("benchmarks.mcp_tools.cases.code")
    memory_module = _load("benchmarks.mcp_tools.cases.memory")
    route_module = _load("benchmarks.mcp_tools.cases.route")
    compact_module = _load("benchmarks.mcp_tools.cases.compact")
    rescue_module = _load("benchmarks.mcp_tools.cases.rescue")
    edit_module = _load("benchmarks.mcp_tools.cases.edit")
    shell_module = _load("benchmarks.mcp_tools.cases.shell")
    sql_module = _load("benchmarks.mcp_tools.cases.sql")
    trace_module = _load("benchmarks.mcp_tools.cases.trace")
    verify_module = _load("benchmarks.mcp_tools.cases.verify")

    assert len(read_module.READ_CASES) == 300
    assert len(search_module.SEARCH_CASES) == 300
    assert len(grep_module.GREP_CASES) == 300
    assert len(context_module.CONTEXT_CASES) == 300
    assert len(code_module.CODE_CASES) >= 2118
    assert len(memory_module.MEMORY_CASES) == 300
    assert len(route_module.ROUTE_CASES) == 300
    assert len(compact_module.COMPACT_CASES) == 300
    assert len(rescue_module.RESCUE_CASES) == 300
    assert len(edit_module.EDIT_CASES) >= 20
    assert len(shell_module.SHELL_CASES) >= 15
    assert len(sql_module.SQL_CASES) >= 10
    assert len(trace_module.TRACE_CASES) >= 8
    assert len(verify_module.VERIFY_CASES) == 300


def test_generated_public_code_tool_counts() -> None:
    code_module = _load("benchmarks.mcp_tools.cases.code")
    counts: dict[str, int] = {}
    for case in code_module.CODE_CASES:
        tool_name = str(
            case.args.get("_tool")
            or (
                "symbols"
                if case.op not in {"node", "callers", "callees", "usages", "impact", "explore", "pattern"}
                else case.op
            )
        )
        counts[tool_name] = counts.get(tool_name, 0) + 1

    assert counts["symbols"] >= 300
    assert counts["node"] >= 300
    assert counts["callers"] >= 300
    assert counts["callees"] >= 300
    assert counts["usages"] >= 300
    assert counts["impact"] >= 300
    assert counts["explore"] >= 300


def test_generated_case_labels_are_unique() -> None:
    modules = [
        _load("benchmarks.mcp_tools.cases.read"),
        _load("benchmarks.mcp_tools.cases.search"),
        _load("benchmarks.mcp_tools.cases.grep"),
        _load("benchmarks.mcp_tools.cases.context"),
        _load("benchmarks.mcp_tools.cases.code"),
        _load("benchmarks.mcp_tools.cases.memory"),
        _load("benchmarks.mcp_tools.cases.route"),
        _load("benchmarks.mcp_tools.cases.compact"),
        _load("benchmarks.mcp_tools.cases.rescue"),
        _load("benchmarks.mcp_tools.cases.edit"),
        _load("benchmarks.mcp_tools.cases.shell"),
        _load("benchmarks.mcp_tools.cases.sql"),
        _load("benchmarks.mcp_tools.cases.trace"),
        _load("benchmarks.mcp_tools.cases.verify"),
    ]
    for module, attr in [
        (modules[0], "READ_CASES"),
        (modules[1], "SEARCH_CASES"),
        (modules[2], "GREP_CASES"),
        (modules[3], "CONTEXT_CASES"),
        (modules[4], "CODE_CASES"),
        (modules[5], "MEMORY_CASES"),
        (modules[6], "ROUTE_CASES"),
        (modules[7], "COMPACT_CASES"),
        (modules[8], "RESCUE_CASES"),
        (modules[9], "EDIT_CASES"),
        (modules[10], "SHELL_CASES"),
        (modules[11], "SQL_CASES"),
        (modules[12], "TRACE_CASES"),
        (modules[13], "VERIFY_CASES"),
    ]:
        labels = [case.label for case in getattr(module, attr)]
        assert len(labels) == len(set(labels)), f"{attr} labels must be unique"
