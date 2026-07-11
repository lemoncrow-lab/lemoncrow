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
    # Do NOT pop the module to force re-import: each cases module generates
    # hundreds of BenchCase objects from a full src/lemoncrow AST scan at import
    # time. Popping made all three tests regenerate from scratch (~18s total).
    # The generated *_CASES lists are deterministic and parameter-free, so
    # reusing the cached module across tests is safe and pays generation once.
    _ensure_benchmarks_package()
    return importlib.import_module(module_name)


def test_generated_repo_backed_case_counts() -> None:
    read_module = _load("benchmarks.mcp_tools.cases.read")
    search_module = _load("benchmarks.mcp_tools.cases.search")
    grep_module = _load("benchmarks.mcp_tools.cases.grep")
    context_module = _load("benchmarks.mcp_tools.cases.context")
    code_module = _load("benchmarks.mcp_tools.cases.code")
    memory_module = _load("benchmarks.mcp_tools.cases.memory")
    compact_module = _load("benchmarks.mcp_tools.cases.compact")
    rescue_module = _load("benchmarks.mcp_tools.cases.rescue")
    edit_module = _load("benchmarks.mcp_tools.cases.edit")
    shell_module = _load("benchmarks.mcp_tools.cases.shell")
    sql_module = _load("benchmarks.mcp_tools.cases.sql")
    trace_module = _load("benchmarks.mcp_tools.cases.trace")
    verify_module = _load("benchmarks.mcp_tools.cases.verify")

    assert len(read_module.READ_CASES) == 300
    # search is now embeddings-only: 150 semantic chunk cases. Repo-map was dropped
    # from the agent surface entirely (no map tool), so grep keeps its 300 native
    # output-shape cases and the GREP_MAP_CASES export is gone.
    assert len(search_module.SEARCH_CASES) == 150
    assert len(grep_module.GREP_CASES) == 300
    assert not hasattr(search_module, "GREP_MAP_CASES")
    assert len(context_module.CONTEXT_CASES) == 300
    # Outline is no longer a code-op (measured via `read mode=outline`), so its
    # 77 generated/static cases were removed from CODE_CASES.
    assert len(code_module.CODE_CASES) >= 640
    assert len(memory_module.MEMORY_CASES) == 300
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
            or ("symbols" if case.op not in {"node", "callers", "callees", "usages", "explore", "pattern"} else case.op)
        )
        counts[tool_name] = counts.get(tool_name, 0) + 1

    # The bucketing folds every non-graph op (search, symbol, hover, index,
    # cache_status) into "symbols". Removing the 77 outline cases (outline is no
    # longer a code-op; it's measured via `read mode=outline`) drops this bucket
    # from ~312 to its true search+symbol+hover floor.
    assert counts["symbols"] >= 235
    assert counts["node"] >= 25
    assert counts["callers"] >= 25
    assert counts["callees"] >= 25
    assert counts["usages"] >= 300
    assert counts["explore"] >= 25


def test_generated_case_labels_are_unique() -> None:
    modules = [
        _load("benchmarks.mcp_tools.cases.read"),
        _load("benchmarks.mcp_tools.cases.search"),
        _load("benchmarks.mcp_tools.cases.grep"),
        _load("benchmarks.mcp_tools.cases.context"),
        _load("benchmarks.mcp_tools.cases.code"),
        _load("benchmarks.mcp_tools.cases.memory"),
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
        (modules[6], "COMPACT_CASES"),
        (modules[7], "RESCUE_CASES"),
        (modules[8], "EDIT_CASES"),
        (modules[9], "SHELL_CASES"),
        (modules[10], "SQL_CASES"),
        (modules[11], "TRACE_CASES"),
        (modules[12], "VERIFY_CASES"),
    ]:
        labels = [case.label for case in getattr(module, attr)]
        assert len(labels) == len(set(labels)), f"{attr} labels must be unique"
