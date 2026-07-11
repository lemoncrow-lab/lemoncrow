"""Benchmark cases for the public code-intel MCP tools.

Savings vs baseline come from:
- Symbol-level abstraction: return signature/location, not full file content
- Code index: exact cross-language references vs. textual grep (no false positives)
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tiktoken

from benchmarks.mcp_tools.harness import BaselineMeasurement, BenchCase
from benchmarks.mcp_tools.repo_facts import (
    CallRelationFact,
    SymbolFact,
    benchmark_query_text,
    collect_call_relation_facts,
    collect_symbol_facts,
    stable_symbol_facts,
    symbols_with_text_references,
    unique_symbol_facts,
)

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


DEFAULT_REPO_ROOT = str(_repo_root())


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
        [
            "rg",
            "-n",
            "tool_code|mcp_tool|classify_command|run_command",
            "src",
            "tests",
            "benchmarks",
        ],
    ]
    by_op: dict[str, tuple[list[list[str]], list[str]]] = {
        "index": (
            [
                *common_cmds,
                ["rg", "-n", "def\\s+|class\\s+", "src/lemoncrow/gateway/adapters/mcp_server.py"],
                [
                    "rg",
                    "-n",
                    "def\\s+tool_|def\\s+search_symbols|def\\s+_tool_call_graph",
                    "src/lemoncrow/core/capabilities/code_context/engine.py",
                ],
            ],
            [
                "src/lemoncrow/gateway/adapters/mcp_server.py",
                "src/lemoncrow/core/capabilities/code_context/engine.py",
                "tests/gateway/test_p0_mcp_surfaces.py",
                "tests/gateway/test_mcp_tool_handlers.py",
                "tests/core/test_code_context.py",
                "src/lemoncrow/core/capabilities/tool_supervision/native_search.py",
            ],
        ),
        "search": (
            [
                *common_cmds,
                ["rg", "-n", "classify_command|run_command", "src/lemoncrow"],
                [
                    "rg",
                    "-n",
                    "search_symbols|tool_search|resolve_search_mode",
                    "src/lemoncrow/core/capabilities/code_context/engine.py",
                ],
            ],
            [
                "src/lemoncrow/core/capabilities/code_context/engine.py",
                "src/lemoncrow/gateway/adapters/mcp_server.py",
                "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
                "tests/core/test_code_context.py",
            ],
        ),
        "symbol": (
            [
                *common_cmds,
                [
                    "rg",
                    "-n",
                    "classify_command",
                    "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
                ],
            ],
            [
                "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
                "src/lemoncrow/core/capabilities/code_context/engine.py",
                "src/lemoncrow/gateway/adapters/mcp_server.py",
            ],
        ),
        "hover": (
            [
                *common_cmds,
                [
                    "rg",
                    "-n",
                    "classify_command|CommandPolicyDecision",
                    "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
                ],
            ],
            [
                "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
                "src/lemoncrow/core/capabilities/code_context/engine.py",
                "src/lemoncrow/gateway/adapters/mcp_server.py",
            ],
        ),
        "usages": (
            [
                *common_cmds,
                ["rg", "-n", "run_command\\(", "src", "tests", "benchmarks"],
                ["rg", "-n", "run_command", "src", "tests", "benchmarks"],
            ],
            [
                "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
                "src/lemoncrow/gateway/adapters/mcp_server.py",
                "tests/gateway/test_run_tool.py",
                "src/lemoncrow/core/capabilities/code_context/engine.py",
            ],
        ),
        "callers": (
            [
                *common_cmds,
                ["rg", "-n", "run_command\\(", "src", "tests", "benchmarks"],
                [
                    "rg",
                    "-n",
                    "_run_bash_tool|tool_bash",
                    "src/lemoncrow/gateway/adapters/mcp_server.py",
                ],
            ],
            [
                "src/lemoncrow/gateway/adapters/mcp_server.py",
                "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
                "src/lemoncrow/core/capabilities/code_context/engine.py",
                "tests/gateway/test_run_tool.py",
            ],
        ),
        "callees": (
            [
                *common_cmds,
                [
                    "rg",
                    "-n",
                    "classify_command|_is_|split\\(",
                    "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
                ],
            ],
            [
                "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
                "src/lemoncrow/core/capabilities/code_context/engine.py",
                "src/lemoncrow/gateway/adapters/mcp_server.py",
            ],
        ),
        "pattern": (
            [
                *common_cmds,
                [
                    "rg",
                    "-n",
                    "def\\s+tool_pattern|tool_pattern\\(",
                    "src/lemoncrow/gateway/adapters/mcp_server.py",
                    "src/lemoncrow/core/capabilities/code_context/engine.py",
                ],
            ],
            [
                "src/lemoncrow/gateway/adapters/mcp_server.py",
                "src/lemoncrow/core/capabilities/code_context/engine.py",
                "tests/gateway/test_p0_mcp_surfaces.py",
            ],
        ),
        "context": (
            [
                *common_cmds,
                ["rg", "-n", "tool_code|tool_context|tool_search|tool_symbol", "src/lemoncrow"],
                ["rg", "-n", "test_tool_code_|code_context", "tests/gateway", "tests/core"],
            ],
            [
                "src/lemoncrow/gateway/adapters/mcp_server.py",
                "src/lemoncrow/core/capabilities/code_context/engine.py",
                "tests/gateway/test_mcp_tool_handlers.py",
                "tests/gateway/test_p0_mcp_surfaces.py",
                "tests/core/test_code_context.py",
            ],
        ),
        "cache_status": (
            [
                *common_cmds,
                [
                    "rg",
                    "-n",
                    "tool_cache_status|cache_invalidate|entries_by_tool",
                    "src/lemoncrow",
                    "tests/core",
                    "tests/gateway",
                ],
            ],
            [
                "src/lemoncrow/core/capabilities/code_context/engine.py",
                "src/lemoncrow/core/capabilities/code_context/renderer.py",
                "src/lemoncrow/gateway/adapters/mcp_server.py",
                "tests/core/test_code_context.py",
                "tests/gateway/test_mcp_tool_handlers.py",
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
    assert (
        "items" in result or "matches" in result or "hits" in result or "results" in result
    ), f"semantic search must return items/matches/hits/results, got keys={list(result)}"
    items = result.get("items") or result.get("matches") or result.get("hits") or result.get("results") or []
    assert len(items) > 0, f"semantic search for 'classify shell commands' must return at least one hit, got: {result}"
    for item in items:
        fp = str(item.get("path") or item.get("file_path", ""))
        assert fp.startswith("src/"), f"search results must be filtered to src/, got file_path={fp!r}"


# ---------------------------------------------------------------------------
# 2. search/lexical — exact symbol lookup
# ---------------------------------------------------------------------------


def _assert_search_lexical(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    assert "run_command" in text, f"lexical search for 'run_command' must find the function, got: {text[:300]!r}"
    # Must include file path
    assert "bash_exec" in text, f"lexical search must include file_path with bash_exec, got: {text[:300]!r}"


def _assert_search_compact_location_only(result: dict[str, Any]) -> None:
    _assert_search_lexical(result)
    rendered = str(result.get("rendered") or "")
    assert rendered, "compact search must render a non-empty summary"
    assert "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py" in rendered
    assert "classify shell command text" not in rendered
    assert "```" not in rendered


def _assert_search_target_view(result: dict[str, Any]) -> None:
    _assert_search_lexical(result)
    assert result.get("view") == "target"
    assert result.get("has_more_context") is True
    items = result.get("items")
    assert isinstance(items, list) and items, "target view must return primary items"
    first = items[0]
    assert isinstance(first, dict)
    assert first.get("role") == "definition"
    assert "path" in first and "line" in first, f"target item must be a pointer, got {first}"
    assert (
        "content_hash" not in first and "symbol_id" not in first
    ), f"target item should not expose internal fields, got {first}"
    assert result.get("suggested_next") == [
        {"op": "usages", "query": "run_command"},
        {"op": "context", "query": "run_command"},
    ]


def _assert_search_graph_view(result: dict[str, Any]) -> None:
    _assert_ok(result)
    assert result.get("view") == "graph"
    assert "items" not in result, "graph view should not mix relationship rows into items"
    target = result.get("target")
    assert isinstance(target, dict), "graph view must include the selected target"
    assert target.get("name") == "run_command"
    related = result.get("related")
    assert isinstance(related, dict), "graph view must include related buckets"
    for key in ("imports", "usages", "callers", "callees"):
        assert key in related, f"graph view missing related.{key}"
        assert isinstance(related[key], list), f"related.{key} must be a list"
    assert related["usages"] or related["callers"], f"graph view should surface usage/caller evidence, got {related}"


def _assert_search_explain_view(result: dict[str, Any]) -> None:
    graph_shape = {key: value for key, value in result.items() if key != "items"}
    _assert_search_graph_view({**graph_shape, "view": "graph"})
    assert result.get("view") == "explain"
    items = result.get("items")
    assert isinstance(items, list) and items, "explain view must preserve primary targets"
    assert "explanation" in result
    assert "related" in result


# ---------------------------------------------------------------------------
# 3. symbol — full definition retrieval
# ---------------------------------------------------------------------------


def _assert_symbol(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    assert "classify_command" in text, f"symbol op must return the requested symbol name, got: {text[:300]!r}"
    assert "bash_exec" in text, f"symbol op must include file_path, got: {text[:300]!r}"
    # Should have signature/body or at least file+line location
    assert any(
        k in result for k in ("source", "signature", "line", "line_number", "body")
    ), f"symbol must include source/signature/line, got keys={list(result)}"


def _assert_symbol_compact_no_full_source(result: dict[str, Any]) -> None:
    _assert_symbol(result)
    rendered = str(result.get("rendered") or "")
    assert rendered, "compact symbol must render a non-empty summary"
    assert "classify_command" in rendered
    assert "return CommandPolicyDecision" not in rendered
    assert "```" not in rendered


# ---------------------------------------------------------------------------
# 4. hover — positional type/signature lookup
# ---------------------------------------------------------------------------


def _assert_hover(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    # hover at the classify_command definition line should return its signature info
    assert (
        "classify_command" in text or "CommandPolicyDecision" in text
    ), f"hover at classify_command must return relevant type/signature info, got: {text[:300]!r}"


# ---------------------------------------------------------------------------
# 6. usages — all references across the repo
# ---------------------------------------------------------------------------


def _assert_usages(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    assert "run_command" in text, f"usages of run_command must reference that symbol, got: {text[:300]!r}"
    # Should include at least one file path that's not bash_exec (a caller)
    assert (
        "mcp_server" in text or "bench_shell" in text or "test_" in text
    ), f"usages must include files outside bash_exec.py (cross-file refs), got: {text[:400]!r}"


# ---------------------------------------------------------------------------
# 7. callers — who calls this function (inbound call graph)
# ---------------------------------------------------------------------------


def _assert_callers(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    assert "run_command" in text, f"callers result must reference run_command, got: {text[:300]!r}"
    if result.get("data_status") == "unavailable":
        return
    # _run_bash_tool in mcp_server.py calls run_command
    assert any(
        name in text for name in ("mcp_server", "_run_bash_tool", "bench_shell", "test_")
    ), f"callers must surface at least one known caller file, got: {text[:400]!r}"


# ---------------------------------------------------------------------------
# 8. callees — what this function calls (outbound call graph)
# ---------------------------------------------------------------------------


def _assert_callees(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    # classify_command calls things; or even if empty, no error
    assert (
        "classify_command" in text or "callees" in text.lower() or "calls" in text.lower()
    ), f"callees result must reference the target symbol, got: {text[:300]!r}"


# ---------------------------------------------------------------------------
# 9. pattern — AST structural search
# ---------------------------------------------------------------------------


def _assert_pattern_decorator(result: dict[str, Any]) -> None:
    _assert_ok(result)
    matches = result.get("matches")
    assert isinstance(matches, list), f"pattern must return a matches list, got keys={list(result)}"
    assert matches, f"pattern search for class definitions must find matches, got: {result}"
    text = str(matches)
    assert (
        "class " in text or "snippet" in text
    ), f"pattern search for class definitions must include match snippets, got: {text[:300]!r}"
    assert any(
        k in result for k in ("matches", "results", "files", "count")
    ), f"pattern must return matches/results/files, got keys={list(result)}"


# ---------------------------------------------------------------------------
# 11. context — task-based context builder
# ---------------------------------------------------------------------------


def _assert_index_compact_summary(result: dict[str, Any]) -> None:
    _assert_index(result)
    rendered = str(result.get("rendered") or "")
    assert rendered, "compact index must render a non-empty summary"
    assert "counts: files=" in rendered


def _assert_cache_status_compact(result: dict[str, Any]) -> None:
    _assert_has(result, "entry_count", "entries_by_tool", "total_bytes", "max_bytes")
    rendered = str(result.get("rendered") or "")
    assert rendered, "compact cache_status must render a non-empty summary"
    assert "payload_json" not in str(result)


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
    assert (
        result.get("data_status") == "available"
    ), f"stress callers should be available after fallback, got data_status={result.get('data_status')!r}"
    edge_count = int(result.get("edge_count", 0))
    assert edge_count >= 1, f"stress callers should return at least one edge, got edge_count={edge_count}"


def _assert_pattern_stress(result: dict[str, Any]) -> None:
    _assert_ok(result)
    text = str(result)
    assert (
        any(k in result for k in ("matches", "results", "files", "count")) and len(text) > 200
    ), f"stress pattern must return non-trivial result payload, got: {text[:300]!r}"


def _assert_generated_search(result: dict[str, Any], expected_name: str, expected_path: str) -> None:
    _assert_ok(result)
    items = result.get("items") or []
    assert isinstance(items, list) and items, "generated lexical search must return items"
    matching_items = [
        item
        for item in items
        if isinstance(item, dict) and expected_path in str(item.get("path") or item.get("file_path") or "")
    ]
    assert matching_items, f"generated lexical search must include {expected_path!r}, got {items!r}"
    assert any(
        expected_name in str(item) for item in matching_items
    ), f"generated lexical search must include {expected_name!r}, got {matching_items!r}"


def _assert_generated_symbol(result: dict[str, Any], expected_name: str, expected_path: str) -> None:
    _assert_ok(result)
    text = str(result)
    assert expected_name in text, f"symbol result must include {expected_name!r}"
    assert expected_path in text, f"symbol result must include {expected_path!r}"
    assert any(
        k in result for k in ("source", "signature", "line", "line_number", "body")
    ), f"symbol must include source/signature/line, got keys={list(result)}"


def _assert_generated_hover(result: dict[str, Any], expected_name: str, expected_path: str) -> None:
    _assert_ok(result)
    text = str(result)
    assert (
        expected_name in text or expected_path in text
    ), f"hover result must include {expected_name!r} or {expected_path!r}, got: {text[:300]!r}"


def _search_assert(expected_name: str, expected_path: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_generated_search(result, expected_name, expected_path)

    return _assert


def _symbol_assert(expected_name: str, expected_path: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_generated_symbol(result, expected_name, expected_path)

    return _assert


def _hover_assert(expected_name: str, expected_path: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_generated_hover(result, expected_name, expected_path)

    return _assert


def _node_assert(expected_name: str, expected_path: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_generated_symbol(result, expected_name, expected_path)

    return _assert


def _callers_assert(expected_name: str, expected_paths: tuple[str, ...]) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_ok(result)
        text = str(result)
        assert expected_name in text, f"callers result must include {expected_name!r}"
        if result.get("data_status") == "unavailable":
            return
        assert any(
            path in text for path in expected_paths
        ), f"callers result must include one of {expected_paths!r}, got: {text[:400]!r}"

    return _assert


def _callees_assert(expected_name: str, expected_paths: tuple[str, ...]) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_ok(result)
        text = str(result)
        assert expected_name in text, f"callees result must include {expected_name!r}"
        assert any(
            path in text for path in expected_paths
        ), f"callees result must include one of {expected_paths!r}, got: {text[:400]!r}"

    return _assert


def _usages_assert(expected_name: str, expected_paths: tuple[str, ...]) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_ok(result)
        text = str(result)
        assert expected_name in text, f"usages result must include {expected_name!r}"
        assert any(
            path in text for path in expected_paths
        ), f"usages result must include one of {expected_paths!r}, got: {text[:400]!r}"

    return _assert


def _explore_assert(expected_name: str, expected_path: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_ok(result)
        text = str(result)
        assert expected_name in text, f"explore result must include {expected_name!r}"
        assert expected_path in text, f"explore result must include {expected_path!r}"

    return _assert


def _group_callers(
    relations: list[CallRelationFact], *, allowed_queries: set[str]
) -> list[tuple[SymbolFact, tuple[str, ...]]]:
    grouped: dict[str, tuple[SymbolFact, set[str]]] = {}
    for relation in relations:
        query = benchmark_query_text(relation.callee)
        if query not in allowed_queries:
            continue
        grouped.setdefault(query, (relation.callee, set()))[1].add(relation.caller.path)
    return [
        (symbol, tuple(sorted(paths)))
        for _query, (symbol, paths) in sorted(grouped.items(), key=lambda item: item[0])
        if paths
    ]


def _group_callees(
    relations: list[CallRelationFact], *, allowed_queries: set[str]
) -> list[tuple[SymbolFact, tuple[str, ...]]]:
    grouped: dict[str, tuple[SymbolFact, set[str]]] = {}
    for relation in relations:
        query = benchmark_query_text(relation.caller)
        if query not in allowed_queries:
            continue
        grouped.setdefault(query, (relation.caller, set()))[1].add(relation.callee.path)
    return [
        (symbol, tuple(sorted(paths)))
        for _query, (symbol, paths) in sorted(grouped.items(), key=lambda item: item[0])
        if paths
    ]


# ---------------------------------------------------------------------------
# Case definitions
# ---------------------------------------------------------------------------

CODE_CASES: list[BenchCase] = [
    BenchCase(
        op="index",
        label="index — build code index before query benchmarks",
        args={
            "op": "index",
            "repo_root": DEFAULT_REPO_ROOT,
            "include_globs": ["src/**/*.py", "tests/**/*.py", "benchmarks/**/*.py"],
            "exclude_globs": [
                ".claude/**",
                ".git/**",
                ".venv/**",
                "node_modules/**",
                "dist/**",
                "build/**",
            ],
            "force": True,
            "budget_tokens": 1200,
            "render_compact": True,
        },
        assert_keys=[],
        custom_assert=_assert_index_compact_summary,
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
            "render_compact": True,
        },
        assert_keys=[],
        custom_assert=_assert_search_compact_location_only,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="search",
        label="search/view-target — pointer-first primary results",
        args={
            "op": "search",
            "query": "run_command",
            "mode": "lexical",
            "view": "target",
            "limit": 5,
            "file_glob": "src/**/*.py",
            "budget_tokens": 600,
        },
        assert_keys=[],
        custom_assert=_assert_search_target_view,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="search",
        label="search/view-graph — relationships separated from items",
        args={
            "op": "search",
            "query": "run_command",
            "mode": "lexical",
            "view": "graph",
            "limit": 10,
            "file_glob": "src/**/*.py",
            "budget_tokens": 1200,
        },
        assert_keys=[],
        custom_assert=_assert_search_graph_view,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="search",
        label="search/view-explain — targets plus graph evidence",
        args={
            "op": "search",
            "query": "run_command",
            "mode": "lexical",
            "view": "explain",
            "limit": 10,
            "file_glob": "src/**/*.py",
            "budget_tokens": 1400,
        },
        assert_keys=[],
        custom_assert=_assert_search_explain_view,
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
            "render_compact": True,
        },
        assert_keys=[],
        custom_assert=_assert_symbol_compact_no_full_source,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="hover",
        label="hover — positional lookup at known line",
        args={
            "op": "hover",
            "path": "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
            "line": 133,
            "budget_tokens": 300,
        },
        assert_keys=[],
        custom_assert=_assert_hover,
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="usages",
        label="usages — all references to run_command across repo",
        args={
            "op": "usages",
            "symbol_name": "run_command",
            "path": "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
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
            "path": "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
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
        label="pattern — structural search for class definitions",
        args={
            "op": "pattern",
            "pattern": "class $C:\n    $$$BODY",
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
        op="cache_status",
        label="cache_status — compact diagnostics summary only",
        args={
            "op": "cache_status",
            "budget_tokens": 300,
            "render_compact": True,
        },
        assert_keys=[],
        custom_assert=_assert_cache_status_compact,
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
        spill_probe_pattern='"symbol_name": "',
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
    BenchCase(
        op="usages",
        label="stress/usages — broad refs high limit and no grouping",
        args={
            "op": "usages",
            "symbol_name": "run_command",
            "path": "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
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
            "path": "src/lemoncrow/core/capabilities/tool_supervision/bash_exec.py",
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
        label="stress/pattern — wide AST function body sweep",
        args={
            "op": "pattern",
            "pattern": "def $F($$$ARGS):\n    $$$BODY",
            "language": "python",
            "file_glob": "src/**/*.py",
            "limit": 300,
            "budget_tokens": 12000,
        },
        assert_keys=[],
        custom_assert=_assert_pattern_stress,
        spill_probe_pattern='"matches"',
        baseline_builder=_build_measured_baseline,
        min_baseline_tokens=BASELINE_MIN_TOKENS,
    ),
]


def _build_generated_code_cases() -> list[BenchCase]:
    repo_root = _repo_root()
    symbol_facts, _ = collect_symbol_facts(repo_root)
    unique_symbols = unique_symbol_facts(symbol_facts)
    stable_symbols = stable_symbol_facts(unique_symbols)
    stable_method_symbols = stable_symbol_facts(unique_symbols, require_dotted=True)
    call_relations = collect_call_relation_facts(repo_root)
    stable_method_queries = {benchmark_query_text(symbol) for symbol in stable_method_symbols}
    callers_targets = _group_callers(call_relations, allowed_queries=stable_method_queries)
    callees_targets = _group_callees(call_relations, allowed_queries=stable_method_queries)
    referenced_symbols = symbols_with_text_references(repo_root, stable_method_symbols, minimum_mentions=3)

    search_symbols = stable_symbols[:75]
    symbol_targets = stable_symbols[75:150]
    hover_targets = stable_symbols[150:225]
    node_targets = stable_symbols[225:250]
    explore_targets = stable_method_symbols[:25]
    usages_targets = referenced_symbols[:300]

    callers_cases = callers_targets[:25]
    callees_cases = callees_targets[:25]

    assert len(search_symbols) == 75, "not enough unique symbols for generated code search cases"
    assert len(symbol_targets) == 75, "not enough unique symbols for generated code symbol cases"
    assert len(hover_targets) == 75, "not enough unique symbols for generated code hover cases"
    assert len(node_targets) == 25, "not enough unique symbols for generated node cases"
    assert len(explore_targets) == 25, "not enough unique symbols for generated explore cases"
    assert len(usages_targets) == 300, "not enough referenced symbols for generated usages cases"

    assert len(callers_cases) == 25, "not enough caller targets for generated callers cases"
    assert len(callees_cases) == 25, "not enough callee targets for generated callees cases"

    cases: list[BenchCase] = []
    for index, symbol in enumerate(search_symbols, start=1):
        query = benchmark_query_text(symbol)
        cases.append(
            BenchCase(
                op="search",
                label=f"generated/search-lexical/{index:03d}",
                args={
                    "op": "search",
                    "query": query,
                    "mode": "lexical",
                    "limit": 5,
                    "file_glob": "src/**/*.py",
                    "budget_tokens": 600,
                    "render_compact": True,
                },
                custom_assert=_search_assert(symbol.name, symbol.path),
                baseline_builder=_build_measured_baseline,
                min_baseline_tokens=BASELINE_MIN_TOKENS,
            )
        )
    for index, symbol in enumerate(symbol_targets, start=1):
        query = benchmark_query_text(symbol)
        cases.append(
            BenchCase(
                op="symbol",
                label=f"generated/symbol/{index:03d}",
                args={
                    "op": "symbol",
                    **({"qualified_name": query} if "." in query else {"symbol_name": query}),
                    "snippet": "head",
                    "budget_tokens": 900,
                    "render_compact": True,
                },
                custom_assert=_symbol_assert(symbol.name, symbol.path),
                baseline_builder=_build_measured_baseline,
                min_baseline_tokens=BASELINE_MIN_TOKENS,
            )
        )
    for index, symbol in enumerate(hover_targets, start=1):
        cases.append(
            BenchCase(
                op="hover",
                label=f"generated/hover/{index:03d}",
                args={
                    "op": "hover",
                    "path": symbol.path,
                    "line": symbol.line,
                    "budget_tokens": 300,
                },
                custom_assert=_hover_assert(symbol.name, symbol.path),
                baseline_builder=_build_measured_baseline,
                min_baseline_tokens=BASELINE_MIN_TOKENS,
            )
        )
    for index, symbol in enumerate(node_targets, start=1):
        cases.append(
            BenchCase(
                op="node",
                label=f"generated/node/{index:03d}",
                args={
                    "_tool": "node",
                    "path": symbol.path,
                    "line": symbol.line,
                    "repo_root": DEFAULT_REPO_ROOT,
                },
                custom_assert=_node_assert(symbol.name, symbol.path),
                baseline_builder=_build_measured_baseline,
                min_baseline_tokens=BASELINE_MIN_TOKENS,
            )
        )
    for index, (symbol, caller_paths) in enumerate(callers_cases, start=1):
        query = benchmark_query_text(symbol)
        cases.append(
            BenchCase(
                op="callers",
                label=f"generated/callers/{index:03d}",
                args={
                    "_tool": "callers",
                    "symbol": query,
                    "depth": 1,
                    "limit": max(8, min(24, len(caller_paths) * 2)),
                    "repo_root": DEFAULT_REPO_ROOT,
                },
                custom_assert=_callers_assert(symbol.name, caller_paths[:3]),
                baseline_builder=_build_measured_baseline,
                min_baseline_tokens=BASELINE_MIN_TOKENS,
            )
        )
    for index, (symbol, callee_paths) in enumerate(callees_cases, start=1):
        query = benchmark_query_text(symbol)
        cases.append(
            BenchCase(
                op="callees",
                label=f"generated/callees/{index:03d}",
                args={
                    "_tool": "callees",
                    "symbol": query,
                    "depth": 1,
                    "limit": max(8, min(24, len(callee_paths) * 2)),
                    "repo_root": DEFAULT_REPO_ROOT,
                },
                custom_assert=_callees_assert(symbol.name, callee_paths[:3]),
                baseline_builder=_build_measured_baseline,
                min_baseline_tokens=BASELINE_MIN_TOKENS,
            )
        )
    for index, symbol in enumerate(usages_targets, start=1):
        related_paths = tuple(
            sorted(
                {
                    relation.caller.path
                    for relation in call_relations
                    if relation.callee.qualified_name == symbol.qualified_name
                }
            )[:3]
        ) or (symbol.path,)
        query = benchmark_query_text(symbol)
        cases.append(
            BenchCase(
                op="usages",
                label=f"generated/usages/{index:03d}",
                args={
                    "_tool": "usages",
                    "symbol": query,
                    "limit": 24,
                    "repo_root": DEFAULT_REPO_ROOT,
                },
                custom_assert=_usages_assert(symbol.name, related_paths),
                baseline_builder=_build_measured_baseline,
                min_baseline_tokens=BASELINE_MIN_TOKENS,
            )
        )

    for index, symbol in enumerate(explore_targets, start=1):
        query = benchmark_query_text(symbol)
        cases.append(
            BenchCase(
                op="explore",
                label=f"generated/explore/{index:03d}",
                args={
                    "_tool": "explore",
                    "query": query,
                    "seed_files": [symbol.path],
                    "max_files": 4,
                    "repo_root": DEFAULT_REPO_ROOT,
                },
                custom_assert=_explore_assert(symbol.name, symbol.path),
                baseline_builder=_build_measured_baseline,
                min_baseline_tokens=BASELINE_MIN_TOKENS,
            )
        )
    return cases


CODE_CASES.extend(_build_generated_code_cases())

for case in CODE_CASES:
    case.args.setdefault("repo_root", DEFAULT_REPO_ROOT)
