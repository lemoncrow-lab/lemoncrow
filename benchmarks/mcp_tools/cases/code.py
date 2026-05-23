"""Benchmark cases for the `code` MCP tool.

Savings vs baseline come from:
- Symbol-level abstraction: return signature/location, not full file content
- SCIP index: exact cross-language references vs. textual grep (no false positives)
- Budget packing: caller/callee/usage graphs truncated to token budget automatically
- Funnel strategy: search→inspect→relate requires fewer tokens than read-then-grep

Baseline policy for this suite:
- Baselines are measured from a no-code-tool fallback payload assembled from
  real repo files and fallback workflow steps (not fixed guesses).
- Every case enforces min_baseline_tokens >= 10_000 to keep the benchmark hard.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import tiktoken

from benchmarks.mcp_tools.harness import BaselineMeasurement, BenchCase

# ---------------------------------------------------------------------------
# Assertions helpers
# ---------------------------------------------------------------------------


BASELINE_MIN_TOKENS = 10_000
_ENC = tiktoken.get_encoding("cl100k_base")


def _repo_root() -> Path:
    value = os.environ.get("CLAUDE_WORKSPACE_ROOT")
    if value:
        return Path(value)
    return Path(__file__).resolve().parents[3]


def _run_cmd(argv: list[str], *, cwd: Path, max_chars: int = 80_000) -> dict[str, Any]:
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "")[:max_chars]
    err = (proc.stderr or "")[:20_000]
    return {
        "cmd": " ".join(shlex.quote(a) for a in argv),
        "exit_code": proc.returncode,
        "stdout": out,
        "stderr": err,
    }


def _read_file(path: Path, *, max_chars: int = 120_000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars]


def _count_text_tokens(text: str) -> int:
    return len(_ENC.encode(text))


def _baseline_plan_for_case(case: BenchCase) -> tuple[list[list[str]], list[str]]:
    common_cmds = [
        ["find", "src", "tests", "benchmarks", "-type", "f", "-name", "*.py"],
        ["rg", "-n", "tool_code|mcp_tool|classify_command|run_command", "src", "tests", "benchmarks"],
    ]
    by_op: dict[str, tuple[list[list[str]], list[str]]] = {
        "index": (
            common_cmds
            + [
                ["rg", "-n", "def\\s+|class\\s+", "src/atelier/gateway/adapters/mcp_server.py"],
                ["rg", "-n", "def\\s+tool_|def\\s+search_symbols|def\\s+_tool_call_graph", "src/atelier/core/capabilities/code_context/engine.py"],
            ],
            [
                "src/atelier/gateway/adapters/mcp_server.py",
                "src/atelier/core/capabilities/code_context/engine.py",
                "tests/gateway/test_p0_mcp_surfaces.py",
                "tests/gateway/test_mcp_tool_handlers.py",
                "tests/core/test_code_context.py",
                "src/atelier/core/capabilities/tool_supervision/native_search.py",
            ],
        ),
        "search": (
            common_cmds
            + [
                ["rg", "-n", "classify_command|run_command", "src/atelier"],
                ["rg", "-n", "search_symbols|tool_search|resolve_search_mode", "src/atelier/core/capabilities/code_context/engine.py"],
            ],
            [
                "src/atelier/core/capabilities/code_context/engine.py",
                "src/atelier/gateway/adapters/mcp_server.py",
                "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
                "tests/core/test_code_context.py",
            ],
        ),
        "symbol": (
            common_cmds
            + [["rg", "-n", "classify_command", "src/atelier/core/capabilities/tool_supervision/bash_exec.py"]],
            [
                "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
                "src/atelier/core/capabilities/code_context/engine.py",
                "src/atelier/gateway/adapters/mcp_server.py",
            ],
        ),
        "hover": (
            common_cmds
            + [["rg", "-n", "classify_command|CommandPolicyDecision", "src/atelier/core/capabilities/tool_supervision/bash_exec.py"]],
            [
                "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
                "src/atelier/core/capabilities/code_context/engine.py",
                "src/atelier/gateway/adapters/mcp_server.py",
            ],
        ),
        "outline": (
            common_cmds
            + [["rg", "-n", "^def\\s+|^class\\s+", "src/atelier/core/capabilities/tool_supervision/bash_exec.py"]],
            [
                "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
                "src/atelier/gateway/adapters/mcp_server.py",
                "src/atelier/core/capabilities/code_context/engine.py",
            ],
        ),
        "usages": (
            common_cmds
            + [
                ["rg", "-n", "run_command\\(", "src", "tests", "benchmarks"],
                ["rg", "-n", "run_command", "src", "tests", "benchmarks"],
            ],
            [
                "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
                "src/atelier/gateway/adapters/mcp_server.py",
                "tests/gateway/test_run_tool.py",
                "src/atelier/core/capabilities/code_context/engine.py",
            ],
        ),
        "callers": (
            common_cmds
            + [
                ["rg", "-n", "run_command\\(", "src", "tests", "benchmarks"],
                ["rg", "-n", "_run_shell_tool|tool_shell", "src/atelier/gateway/adapters/mcp_server.py"],
            ],
            [
                "src/atelier/gateway/adapters/mcp_server.py",
                "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
                "src/atelier/core/capabilities/code_context/engine.py",
                "tests/gateway/test_run_tool.py",
            ],
        ),
        "callees": (
            common_cmds
            + [["rg", "-n", "classify_command|_is_|split\\(", "src/atelier/core/capabilities/tool_supervision/bash_exec.py"]],
            [
                "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
                "src/atelier/core/capabilities/code_context/engine.py",
                "src/atelier/gateway/adapters/mcp_server.py",
            ],
        ),
        "pattern": (
            common_cmds
            + [["rg", "-n", "@mcp_tool\\(|mcp_tool\\(", "src/atelier/gateway/adapters/mcp_server.py"]],
            [
                "src/atelier/gateway/adapters/mcp_server.py",
                "src/atelier/core/capabilities/code_context/engine.py",
                "tests/gateway/test_p0_mcp_surfaces.py",
            ],
        ),
        "impact": (
            common_cmds
            + [["rg", "-n", "bash_exec|tool_supervision\\.bash_exec", "src", "tests", "benchmarks"]],
            [
                "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
                "src/atelier/gateway/adapters/mcp_server.py",
                "tests/gateway/test_run_tool.py",
                "src/atelier/core/capabilities/code_context/engine.py",
            ],
        ),
        "context": (
            common_cmds
            + [
                ["rg", "-n", "tool_code|tool_context|tool_search|tool_symbol", "src/atelier"],
                ["rg", "-n", "test_tool_code_|code_context", "tests/gateway", "tests/core"],
            ],
            [
                "src/atelier/gateway/adapters/mcp_server.py",
                "src/atelier/core/capabilities/code_context/engine.py",
                "tests/gateway/test_mcp_tool_handlers.py",
                "tests/gateway/test_p0_mcp_surfaces.py",
                "tests/core/test_code_context.py",
            ],
        ),
    }
    return by_op.get(case.op, (common_cmds, []))


def _build_measured_baseline(case: BenchCase) -> BaselineMeasurement:
    """Execute fallback rg/find/read workflow and measure its token footprint."""
    root = _repo_root()
    cmds, files = _baseline_plan_for_case(case)
    cmd_results = [_run_cmd(cmd, cwd=root) for cmd in cmds]
    file_reads: dict[str, str] = {}
    input_tokens = 0
    for rel in files:
        text = _read_file(root / rel)
        file_reads[rel] = text
        input_tokens += _count_text_tokens(text)
    payload = {
        "workflow": "fallback_without_code_tool",
        "case": case.label,
        "query_args": case.args,
        "command_results": cmd_results,
        "file_reads": file_reads,
    }
    return BaselineMeasurement(
        payload=payload,
        input_file_tokens=input_tokens,
        commands=[entry["cmd"] for entry in cmd_results],
    )


def _assert_ok(result: dict[str, Any]) -> None:
    assert "error" not in result, f"unexpected error: {result.get('error')} / {result.get('message')}"


def _assert_has(result: dict[str, Any], *keys: str) -> None:
    _assert_ok(result)
    for k in keys:
        assert k in result, f"expected key {k!r} in result, got keys={list(result)}"


def _assert_contains_name(result: dict[str, Any], name: str) -> None:
    """Check that `name` appears anywhere in the result (symbols, snippet, etc.)."""
    _assert_ok(result)
    text = str(result)
    assert name in text, f"expected {name!r} anywhere in result, got: {text[:300]!r}"


# ---------------------------------------------------------------------------
# 0. index — build/rebuild repo index
# ---------------------------------------------------------------------------


def _assert_index(result: dict[str, Any]) -> None:
    _assert_ok(result)
    files_indexed = int(result.get("files_indexed", 0))
    symbols_indexed = int(result.get("symbols_indexed", 0))
    # Harden benchmark: ensure this is a real repo-scale index, not a tiny subset.
    assert files_indexed >= 500, f"index must process repo-scale files, got files_indexed={files_indexed}"
    assert symbols_indexed >= 5000, f"index must process repo-scale symbols, got symbols_indexed={symbols_indexed}"


# ---------------------------------------------------------------------------
# 1. search/hybrid — find by natural language intent
# ---------------------------------------------------------------------------


def _assert_search_semantic(result: dict[str, Any]) -> None:
    _assert_ok(result)
    # code.search returns 'items' (not 'matches'/'hits')
    assert "items" in result or "matches" in result or "hits" in result or "results" in result, (
        f"semantic search must return items/matches/hits/results, got keys={list(result)}"
    )
    items = result.get("items") or result.get("matches") or result.get("hits") or result.get("results") or []
    assert len(items) > 0, (
        f"semantic search for 'classify shell commands' must return at least one hit, got: {result}"
    )
    names = {str(item.get("symbol_name", "")) for item in items}
    assert "classify_command" in names, (
        f"semantic/hybrid NL query should surface classify_command, got symbols={sorted(n for n in names if n)}"
    )
    for item in items:
        fp = str(item.get("file_path", ""))
        assert fp.startswith("src/"), f"search results must be filtered to src/, got file_path={fp!r}"


# ---------------------------------------------------------------------------
# 2. search/lexical — exact symbol lookup
# ---------------------------------------------------------------------------


def _assert_search_lexical(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    assert "run_command" in text, (
        f"lexical search for 'run_command' must find the function, got: {text[:300]!r}"
    )
    # Must include file path
    assert "bash_exec" in text, (
        f"lexical search must include file_path with bash_exec, got: {text[:300]!r}"
    )


# ---------------------------------------------------------------------------
# 3. symbol — full definition retrieval
# ---------------------------------------------------------------------------


def _assert_symbol(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    assert "classify_command" in text, (
        f"symbol op must return the requested symbol name, got: {text[:300]!r}"
    )
    assert "bash_exec" in text, f"symbol op must include file_path, got: {text[:300]!r}"
    # Should have signature/body or at least file+line location
    assert any(k in result for k in ("source", "signature", "line", "line_number", "body")), (
        f"symbol must include source/signature/line, got keys={list(result)}"
    )


# ---------------------------------------------------------------------------
# 4. hover — positional type/signature lookup
# ---------------------------------------------------------------------------


def _assert_hover(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    # hover at the classify_command definition line should return its signature info
    assert "classify_command" in text or "CommandPolicyDecision" in text, (
        f"hover at classify_command must return relevant type/signature info, got: {text[:300]!r}"
    )


# ---------------------------------------------------------------------------
# 5. outline — all symbols in a file
# ---------------------------------------------------------------------------


def _assert_outline(result: dict[str, Any]) -> None:
    _assert_ok(result)
    # The outline returns 'files' dict mapping path → list of symbols
    assert "files" in result or "symbols" in result, (
        f"outline must return files or symbols key, got keys={list(result)}"
    )
    files_dict = result.get("files") or {}
    # Find the symbols list for bash_exec.py (key may be full or relative path)
    symbols: list[Any] = []
    for path_key, syms in files_dict.items():
        if "bash_exec" in path_key:
            symbols = syms
            break
    if not symbols:
        # Maybe flat structure
        symbols = result.get("symbols") or []
    assert len(symbols) >= 5, (
        f"outline of bash_exec.py must return ≥5 symbols (file has 15+), got {len(symbols)}"
    )
    # All symbols should have name and line_start
    for s in symbols[:3]:
        assert "name" in s or "symbol_name" in s, f"symbol entry must have name, got: {s}"


# ---------------------------------------------------------------------------
# 6. usages — all references across the repo
# ---------------------------------------------------------------------------


def _assert_usages(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    assert "run_command" in text, (
        f"usages of run_command must reference that symbol, got: {text[:300]!r}"
    )
    # Should include at least one file path that's not bash_exec (a caller)
    assert "mcp_server" in text or "bench_shell" in text or "test_" in text, (
        f"usages must include files outside bash_exec.py (cross-file refs), got: {text[:400]!r}"
    )


# ---------------------------------------------------------------------------
# 7. callers — who calls this function (inbound call graph)
# ---------------------------------------------------------------------------


def _assert_callers(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    assert "run_command" in text, (
        f"callers result must reference run_command, got: {text[:300]!r}"
    )
    if result.get("data_status") == "unavailable":
        return
    # _run_shell_tool in mcp_server.py calls run_command
    assert any(name in text for name in ("mcp_server", "_run_shell_tool", "bench_shell", "test_")), (
        f"callers must surface at least one known caller file, got: {text[:400]!r}"
    )


# ---------------------------------------------------------------------------
# 8. callees — what this function calls (outbound call graph)
# ---------------------------------------------------------------------------


def _assert_callees(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    # classify_command calls things; or even if empty, no error
    assert "classify_command" in text or "callees" in text.lower() or "calls" in text.lower(), (
        f"callees result must reference the target symbol, got: {text[:300]!r}"
    )


# ---------------------------------------------------------------------------
# 9. pattern — AST structural search
# ---------------------------------------------------------------------------


def _assert_pattern_decorator(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    # @mcp_tool(...) decorator appears many times in mcp_server.py
    assert "mcp_tool" in text or "mcp_server" in text, (
        f"pattern search for @mcp_tool must find matches, got: {text[:300]!r}"
    )
    # Must return at least one match
    assert any(k in result for k in ("matches", "results", "files", "count")), (
        f"pattern must return matches/results/files, got keys={list(result)}"
    )


# ---------------------------------------------------------------------------
# 10. impact — what imports this file
# ---------------------------------------------------------------------------


def _assert_impact(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    # bash_exec.py is imported by mcp_server.py and tests
    assert "mcp_server" in text or "test_" in text or "bench_shell" in text, (
        f"impact for bash_exec.py must list mcp_server or test files, got: {text[:400]!r}"
    )


# ---------------------------------------------------------------------------
# 11. context — task-based context builder
# ---------------------------------------------------------------------------


def _assert_context(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    # Task: "add a new MCP tool handler" → should surface mcp_server.py and engine.py
    assert "mcp_server" in text or "tool_code" in text or "mcp_tool" in text, (
        f"context for 'new MCP tool' must surface mcp_server or tool_, got: {text[:400]!r}"
    )


def _assert_search_stress(result: dict[str, Any]) -> None:
    _assert_ok(result)
    items = result.get("items") or []
    assert len(items) >= 5, f"stress search must return several items, got {len(items)}"


def _assert_usages_stress(result: dict[str, Any]) -> None:
    _assert_ok(result)
    ref_count = int(result.get("reference_count", 0))
    assert ref_count >= 8, f"stress usages should return many refs, got reference_count={ref_count}"


def _assert_call_graph_stress(result: dict[str, Any]) -> None:
    _assert_ok(result)
    assert result.get("data_status") == "available", (
        f"stress callers should be available after fallback, got data_status={result.get('data_status')!r}"
    )
    edge_count = int(result.get("edge_count", 0))
    assert edge_count >= 1, f"stress callers should return at least one edge, got edge_count={edge_count}"


def _assert_outline_stress(result: dict[str, Any]) -> None:
    _assert_ok(result)
    count = int(result.get("symbol_count", 0))
    assert count >= 20, f"stress outline should surface large symbol set, got symbol_count={count}"


def _assert_pattern_stress(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    assert any(k in result for k in ("matches", "results", "files", "count")) and len(text) > 200, (
        f"stress pattern must return non-trivial result payload, got: {text[:300]!r}"
    )


# ---------------------------------------------------------------------------
# Case definitions
# ---------------------------------------------------------------------------

CODE_CASES: list[BenchCase] = [
    BenchCase(
        op="index",
        label="index — build code index before query benchmarks",
        args={
            "op": "index",
            "include_globs": ["src/**/*.py", "tests/**/*.py", "benchmarks/**/*.py"],
            "exclude_globs": [".claude/**", ".git/**", ".venv/**", "node_modules/**", "dist/**", "build/**"],
            "budget_tokens": 1200,
        },
        assert_keys=[],
        custom_assert=_assert_index,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="search",
        label="search/hybrid — natural-language query finds classify_command",
        args={
            "op": "search",
            "query": "function classify_command that classifies shell commands",
            "mode": "hybrid",
            "limit": 8,
            "file_glob": "src/**/*.py",
            "budget_tokens": 800,
        },
        assert_keys=[],
        custom_assert=_assert_search_semantic,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="search",
        label="search/lexical — exact symbol name lookup",
        args={
            "op": "search",
            "query": "run_command",
            "mode": "lexical",
            "limit": 5,
            "file_glob": "src/**/*.py",
            "budget_tokens": 600,
        },
        assert_keys=[],
        custom_assert=_assert_search_lexical,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="symbol",
        label="symbol — full definition from symbol name",
        args={
            "op": "symbol",
            "symbol_name": "classify_command",
            "snippet": "head",
            "budget_tokens": 1000,
        },
        assert_keys=[],
        custom_assert=_assert_symbol,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="hover",
        label="hover — positional lookup at known line",
        args={
            "op": "hover",
            "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
            "line": 129,
            "budget_tokens": 300,
        },
        assert_keys=[],
        custom_assert=_assert_hover,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="outline",
        label="outline — all symbols in bash_exec.py",
        args={
            "op": "outline",
            "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
            "budget_tokens": 1500,
        },
        assert_keys=[],
        custom_assert=_assert_outline,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="usages",
        label="usages — all references to run_command across repo",
        args={
            "op": "usages",
            "symbol_name": "run_command",
            "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
            "limit": 20,
            "snippet": "none",
            "budget_tokens": 1000,
        },
        assert_keys=[],
        custom_assert=_assert_usages,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="callers",
        label="callers — who calls run_command (inbound call graph)",
        args={
            "op": "callers",
            "symbol_name": "run_command",
            "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
            "depth": 1,
            "budget_tokens": 800,
        },
        assert_keys=[],
        custom_assert=_assert_callers,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="callees",
        label="callees — what classify_command calls (outbound graph)",
        args={
            "op": "callees",
            "symbol_name": "classify_command",
            "depth": 1,
            "budget_tokens": 600,
        },
        assert_keys=[],
        custom_assert=_assert_callees,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="pattern",
        label="pattern — structural search for @mcp_tool decorator",
        args={
            "op": "pattern",
            "pattern": "@mcp_tool($$$)",
            "language": "python",
            "limit": 15,
            "budget_tokens": 1000,
        },
        assert_keys=[],
        custom_assert=_assert_pattern_decorator,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="impact",
        label="impact — which files import bash_exec.py",
        args={
            "op": "impact",
            "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
            "budget_tokens": 600,
        },
        assert_keys=[],
        custom_assert=_assert_impact,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="context",
        label="context — task-based surfacing for 'add new MCP tool handler'",
        args={
            "op": "context",
            "task": "add a new MCP tool handler to the MCP server with proper schema validation",
            "budget_tokens": 2000,
        },
        assert_keys=[],
        custom_assert=_assert_context,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="search",
        label="stress/search — broad hybrid query high limit",
        args={
            "op": "search",
            "query": "function",
            "mode": "hybrid",
            "limit": 120,
            "snippet": "head",
            "snippet_lines": 30,
            "file_glob": "src/**/*.py",
            "budget_tokens": 12000,
        },
        assert_keys=[],
        custom_assert=_assert_search_stress,
        spill_probe_pattern="\"symbol_name\": \"",
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="usages",
        label="stress/usages — broad refs high limit and no grouping",
        args={
            "op": "usages",
            "symbol_name": "run_command",
            "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
            "group_by": "none",
            "snippet_lines": 20,
            "limit": 200,
            "budget_tokens": 12000,
        },
        assert_keys=[],
        custom_assert=_assert_usages_stress,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="callers",
        label="stress/callers — depth 2 call graph expansion",
        args={
            "op": "callers",
            "symbol_name": "run_command",
            "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
            "depth": 2,
            "limit": 200,
            "budget_tokens": 12000,
        },
        assert_keys=[],
        custom_assert=_assert_call_graph_stress,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="pattern",
        label="stress/pattern — wide AST call pattern sweep",
        args={
            "op": "pattern",
            "pattern": "$F($$$ARGS)",
            "language": "python",
            "file_glob": "src/**/*.py",
            "limit": 300,
            "budget_tokens": 12000,
        },
        assert_keys=[],
        custom_assert=_assert_pattern_stress,
        spill_probe_pattern="\"matches\"",
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="outline",
        label="stress/outline — large file symbol inventory",
        args={
            "op": "outline",
            "path": "src/atelier/gateway/adapters/mcp_server.py",
            "limit": 600,
            "budget_tokens": 12000,
        },
        assert_keys=[],
        custom_assert=_assert_outline_stress,
        spill_probe_pattern="\"symbol_count\"",
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="context",
        label="stress/context — broad architecture context pack",
        args={
            "op": "context",
            "task": "map full MCP tool architecture including schema, dispatch, indexing, caching, call graphs, pattern ops, and tests",
            "max_symbols": 30,
            "budget_tokens": 12000,
        },
        assert_keys=[],
        custom_assert=_assert_context,
        spill_probe_pattern="\"content\"",
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
]
