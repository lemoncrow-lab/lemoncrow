"""pytest-based MCP public code-intel benchmark.

Run:
    uv run pytest benchmarks/mcp_tools/bench_code.py -v -s

Exercises LemonCrow's code-intel surface against the real LemonCrow codebase:
the public `explore` and `pattern` tools, plus
`explore(relation=callers|callees|usages)` for the call graph + references
(the former standalone tools, now folded into `explore`).
The first run builds the code index (~10-30 s); subsequent runs are cached.

Baseline comparison: each case has a `baseline_tokens` estimate of what
a naive grep / read approach would require.  LemonCrow should be ≤ baseline
for nearly all symbol-level operations.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.code import CODE_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def code_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Point workspace reads at the repo while keeping runtime state isolated.

    Code-intel indexing is expensive to build cold (10-30s+ for this repo);
    point LEMONCROW_ROOT at the real ``~/.lemoncrow`` (mirroring bench_savings.py's
    ``scip_workspace`` fixture) so the index persists across benchmark
    invocations instead of rebuilding from a fresh temp dir on every run.
    """
    real_root = Path(os.environ.get("LEMONCROW_ROOT") or Path.home() / ".lemoncrow")
    root = tmp_path_factory.mktemp("bench_code")
    repo_root = Path(__file__).resolve().parent.parent.parent
    configure_benchmark_runtime(root, workspace_root=repo_root)
    os.environ["LEMONCROW_ROOT"] = str(real_root)
    return repo_root


def code_tool_dispatch(args: dict[str, Any]) -> Any:
    """Dispatch a code-intel benchmark case to its public MCP tool.

    Shared by the pytest suite (``code_tool_fn`` below) and the CLI/CSV
    exporter (``export_public_mcp_csv.py``) so the two entry points can't
    drift out of sync with each other or with the real tool surface.
    """
    from lemoncrow.gateway.adapters import mcp_server

    payload = dict(args)
    tool_name = _tool_name_for_case_args(payload)
    payload.pop("_tool", None)
    if tool_name == "search":
        # Symbol/search via the _op_search engine (the
        # `symbols` tool face was removed; `search` mode='symbol' and this
        # benchmark both route through _op_search, so the measured tokens
        # are identical to the former `symbols` tool).
        return mcp_server._op_search(
            **{
                key: value
                for key, value in payload.items()
                if key
                in {
                    "query",
                    "mode",
                    "intent",
                    "view",
                    "kind",
                    "language",
                    "snippet",
                    "snippet_lines",
                    "file_glob",
                    "scope",
                    "since",
                    "touched_by",
                    "provenance",
                    "seed_files",
                    "max_symbols",
                    "depth",
                    "limit",
                    "budget_tokens",
                    "repo",
                    "repo_root",
                    "render_compact",
                }
            }
        )
    if tool_name == "node":
        # `node` is reached via the `relations` drill-in tool (kind=self) --
        # the agent's only public path for a single definition now. It
        # delegates to the same _op_node wrapper, so the payload (and token
        # count) is identical to the former standalone tool.
        return mcp_server.tool_relations({"kind": "self", "symbol": _symbol_arg(payload)})
    # callers/callees/usages are reached via the `relations` tool -- the
    # agent's only public path now. It delegates to the same _op_* wrapper,
    # so the payload (and token count) is identical to the former standalone
    # tools; this measures what the agent can call. (grep shows the COUNTS
    # inline; relations expands one count into the list.)
    if tool_name == "callers":
        return mcp_server.tool_relations(
            {
                "kind": "callers",
                "symbol": _symbol_arg(payload),
                "depth": int(payload.get("depth", 1)),
                "limit": int(payload.get("limit", 20)),
            }
        )
    if tool_name == "callees":
        return mcp_server.tool_relations(
            {
                "kind": "callees",
                "symbol": _symbol_arg(payload),
                "depth": int(payload.get("depth", 1)),
                "limit": int(payload.get("limit", 20)),
            }
        )
    if tool_name == "usages":
        return mcp_server.tool_relations(
            {
                "kind": "usages",
                "symbol": _symbol_arg(payload),
                "limit": int(payload.get("limit", 20)),
            }
        )
    if tool_name == "explore":
        # Concept-mode explore has no single-tool agent surface after the
        # explore fold; measure the engine wrapper grep's relations route to.
        return mcp_server._op_explore(
            query=str(payload["query"]),
            seed_files=payload.get("seed_files"),
            max_files=int(payload.get("max_files", 8)),
        )
    if tool_name == "pattern":
        return mcp_server.tool_pattern(
            {
                key: value
                for key, value in payload.items()
                if key in {"pattern", "language", "file_glob", "rewrite", "limit", "dry_run"}
            }
        )
    raise ValueError(f"unsupported code-intel benchmark tool: {tool_name}")


@pytest.fixture(scope="session")
def code_tool_fn() -> Any:
    return code_tool_dispatch


@pytest.fixture(scope="session")
def code_bench_results(code_workspace: Path, code_tool_fn: Any) -> list[CaseResult]:
    """Run all code benchmark cases once and cache results for the session."""
    # Avoid stale retrieval-cache artifacts masking worst-case behavior between runs.
    with contextlib.suppress(Exception):
        code_tool_fn({"op": "cache_invalidate", "budget_tokens": 2000})
    results: list[CaseResult] = []
    for case in CODE_CASES:
        results.append(run_case(case, code_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_code_report(code_bench_results: list[CaseResult]) -> None:
    print(render_summary(_group_reports(code_bench_results)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


def _tool_name_for_case_args(args: dict[str, Any]) -> str:
    explicit = args.get("_tool")
    if isinstance(explicit, str) and explicit:
        return explicit
    op = str(args.get("op") or "")
    if op in {"callers", "callees", "explore", "usages", "pattern", "node"}:
        return op
    return "search"


def _tool_name_for_case(case: BenchCase) -> str:
    return _tool_name_for_case_args(case.args)


def _symbol_arg(args: dict[str, Any]) -> str:
    for key in ("symbol", "qualified_name", "symbol_name", "symbol_id", "query"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError(f"missing symbol identifier in args: {args}")


def _group_reports(results: list[CaseResult]) -> list[ToolReport]:
    grouped: dict[str, list[CaseResult]] = {}
    for result in results:
        grouped.setdefault(_tool_name_for_case(result.case), []).append(result)
    return [ToolReport(tool_name=name, results=grouped[name]) for name in sorted(grouped)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CODE_CASES, ids=lambda c: c.label)
def test_code_op_correctness(case: BenchCase, code_bench_results: list[CaseResult]) -> None:
    result = _find(code_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in CODE_CASES if c.baseline_tokens > 0 or c.baseline_builder is not None],
    ids=lambda c: c.label,
)
def test_code_op_saves_tokens(case: BenchCase, code_bench_results: list[CaseResult]) -> None:
    result = _find(code_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert result.baseline_tokens > 0, f"[{case.label}] baseline is zero"
    assert (
        result.lemoncrow_tokens < result.baseline_tokens
    ), f"[{case.label}] no savings: lemoncrow={result.lemoncrow_tokens} >= baseline={result.baseline_tokens}"
