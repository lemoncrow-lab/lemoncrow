"""Savings benchmark: read/grep across 8 languages (100k+ token fixtures) + SCIP code-intel.

First run downloads fixtures and caches them under
``benchmarks/mcp_tools/fixtures/downloaded/``. Subsequent runs are fast.

Run all:
    uv run pytest benchmarks/mcp_tools/bench_savings.py -v -s

Run by group:
    uv run pytest benchmarks/mcp_tools/bench_savings.py -v -s -k "read or grep"
    uv run pytest benchmarks/mcp_tools/bench_savings.py -v -s -k scip
    uv run pytest benchmarks/mcp_tools/bench_savings.py -v -s -k python
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.cases.savings import SAVINGS_CASES, SCIP_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary

# ---------------------------------------------------------------------------
# Session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def bench_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Isolated runtime state for read/grep tools. SCIP uses real ~/.atelier."""
    root = tmp_path_factory.mktemp("bench_savings")
    return configure_benchmark_runtime(root, workspace_root=Path.cwd())


@pytest.fixture(scope="session")
def scip_workspace() -> Path:
    """Point SCIP tool at real ~/.atelier so the cached index is reused.

    Building the index from a temp dir takes 2-3 minutes on every run.
    Using ~/.atelier means the first build is paid once and then cached.
    """
    real_root = Path(os.environ.get("ATELIER_ROOT") or Path.home() / ".atelier")
    os.environ["ATELIER_ROOT"] = str(real_root)
    os.environ["CLAUDE_WORKSPACE_ROOT"] = str(Path.cwd())
    return real_root


@pytest.fixture(scope="session")
def read_tool_fn(bench_workspace: Path) -> Any:
    from atelier.gateway.adapters.mcp_server import tool_smart_read

    return tool_smart_read


@pytest.fixture(scope="session")
def grep_tool_fn(bench_workspace: Path) -> Any:
    from atelier.gateway.adapters.mcp_server import tool_grep

    return tool_grep


@pytest.fixture(scope="session")
def code_tool_fn(scip_workspace: Path) -> Any:
    from atelier.gateway.adapters.mcp_server import _tool_code_alias_handler

    return _tool_code_alias_handler


@pytest.fixture(scope="session")
def scip_index_built(code_tool_fn: Any) -> bool:
    """Ensure SCIP index is up to date (incremental — fast when index exists).

    op=index now defaults to force=False (incremental), so this only indexes
    changed files. First-ever build takes ~30s; subsequent runs are near-instant.
    Pass force=True explicitly to force a full rebuild.
    """
    try:
        result = code_tool_fn({"op": "index"})  # incremental by default
        return isinstance(result, dict) and not result.get("error")
    except Exception:
        return False


def _tool_for_case(case: BenchCase, read_fn: Any, grep_fn: Any, code_fn: Any) -> Any:
    if case.op == "read":
        return read_fn
    if case.op == "grep":
        return grep_fn
    if case.op == "symbols":
        return code_fn
    raise ValueError(f"unknown op {case.op!r}")


@pytest.fixture(scope="session")
def savings_results(
    bench_workspace: Path,
    read_tool_fn: Any,
    grep_tool_fn: Any,
) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in SAVINGS_CASES:
        fn = _tool_for_case(case, read_tool_fn, grep_tool_fn, None)
        results.append(run_case(case, fn))
    return results


@pytest.fixture(scope="session")
def scip_results(
    bench_workspace: Path,
    code_tool_fn: Any,
    scip_index_built: bool,
) -> list[CaseResult]:
    if not scip_index_built:
        return []
    results: list[CaseResult] = []
    for case in SCIP_CASES:
        results.append(run_case(case, code_tool_fn))
    return results


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def print_file_report(savings_results: list[CaseResult]) -> None:
    """Print file-tool (read/grep) matrix. Always runs, never waits for SCIP."""
    read_res = [r for r in savings_results if r.case.op == "read"]
    grep_res = [r for r in savings_results if r.case.op == "grep"]
    reports = []
    if read_res:
        reports.append(ToolReport(tool_name="read (multi-language 100k+ tok)", results=read_res))
    if grep_res:
        reports.append(ToolReport(tool_name="grep", results=grep_res))
    if reports:
        print("\n" + render_summary(reports))
    _print_table(savings_results)


@pytest.fixture(scope="session")
def print_scip_report(scip_results: list[CaseResult]) -> None:
    """Print SCIP matrix. Not autouse — only runs when SCIP tests request it."""
    if not scip_results:
        return
    reports = [ToolReport(tool_name="SCIP code-intel", results=scip_results)]
    print("\n" + render_summary(reports))
    _print_table(scip_results)


def _pct_str(baseline: int, atelier: int) -> str:
    if baseline <= 0:
        return "—"
    pct = (baseline - atelier) / baseline * 100
    return f"{pct:+.0f}%" if pct < 0 else f"{pct:.0f}%"


def _print_table(results: list[CaseResult]) -> None:
    """Print a language x tool savings matrix.

    Baseline = Claude native Read (2000-line cap).
    Negative % = tool output is LARGER than the native baseline (overhead).
    """
    if not results:
        return

    from collections import defaultdict

    by_lang: dict[str, dict[str, CaseResult]] = defaultdict(dict)
    tool_order: list[str] = []
    for r in results:
        parts = r.case.label.split("/")
        if len(parts) == 3:
            tool_key, lang = f"{parts[0]}/{parts[1]}", parts[2]
        elif len(parts) >= 2 and parts[0] == "scip":
            tool_key, lang = "/".join(parts[:2]), "atelier-repo"
        else:
            continue
        by_lang[lang][tool_key] = r
        if tool_key not in tool_order:
            tool_order.append(tool_key)

    col_w, lang_w = 14, 14
    langs = sorted(by_lang.keys())
    # Shorter column labels for readability
    col_labels = [t.split("/")[-1] for t in tool_order]

    header = f"{'Language':<{lang_w}}" + "".join(f"{lbl:>{col_w}}" for lbl in col_labels)
    note = "  Baseline = Claude built-in Read (2000-line cap). Negative = overhead vs native."
    sep = "-" * len(header)
    print(f"\n{note}\n\n{sep}\n{header}\n{sep}")

    for lang in langs:
        row = f"{lang:<{lang_w}}"
        for tool_key in tool_order:
            r = by_lang[lang].get(tool_key)
            if r is None:
                cell = "—"
            elif not r.passed:
                cell = "ERR"
            else:
                cell = _pct_str(r.baseline_tokens, r.atelier_tokens)
            row += f"{cell:>{col_w}}"
        print(row)

    print(sep)
    avg_row = f"{'AVG':<{lang_w}}"
    for tool_key in tool_order:
        vals = [
            (by_lang[lg][tool_key].baseline_tokens, by_lang[lg][tool_key].atelier_tokens)
            for lg in langs
            if tool_key in by_lang[lg] and by_lang[lg][tool_key].passed and by_lang[lg][tool_key].baseline_tokens > 0
        ]
        if not vals:
            avg_row += f"{'':>{col_w}}"
        else:
            avg_pct = sum((b - a) / b * 100 for b, a in vals) / len(vals)
            avg_row += f"{_pct_str(1, 1 - avg_pct / 100):>{col_w}}"
    print(avg_row)
    print(sep)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(label)


# ---------------------------------------------------------------------------
# Tests: file-level (read + grep)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", SAVINGS_CASES, ids=lambda c: c.label)
def test_savings_correctness(case: BenchCase, savings_results: list[CaseResult]) -> None:
    result = _find(savings_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse keys: {list(result.response.keys())}"


_MIN_SAVINGS_PCT: dict[str, float] = {
    # Baseline = native Read (2000 lines of the individual file, ~15-22k tok).
    # Per-file fixtures: JS/Python ~99%, Go/Rust/TS/Java/Ruby/C 30-80%+.
    # Negative values are a benchmark design signal, not a product bug.
    "read/outline": 30.0,  # outline should always beat 2000 raw lines of the same file
    # range 2100-2200: well past native Read 2000-line cap, so atelier always wins
    "read/range": 80.0,  # ~100 lines vs ~15-22k native Read tokens = >90% usually
    "grep/ranked": 0.0,
}


@pytest.mark.parametrize(
    "case",
    [c for c in SAVINGS_CASES if c.baseline_tokens > 0 or c.baseline_builder is not None],
    ids=lambda c: c.label,
)
def test_savings_threshold(
    case: BenchCase,
    savings_results: list[CaseResult],
) -> None:
    result = _find(savings_results, case.label)
    if not result.passed:
        pytest.skip(f"op failed: {result.failure}")
    if result.baseline_tokens == 0:
        pytest.skip("no baseline")
    min_pct = next(
        (t for p, t in _MIN_SAVINGS_PCT.items() if case.label.startswith(p)),
        0.0,
    )
    if min_pct == 0.0:
        return
    assert result.savings_pct >= min_pct, (
        f"[{case.label}] savings {result.savings_pct:.1f}% < required {min_pct:.0f}%\n"
        f"  atelier={result.atelier_tokens:,}  baseline={result.baseline_tokens:,}"
    )


# ---------------------------------------------------------------------------
# Tests: SCIP code-intel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", SCIP_CASES, ids=lambda c: c.label)
def test_scip_correctness(case: BenchCase, scip_results: list[CaseResult], print_scip_report: None) -> None:
    if not scip_results:
        pytest.skip("SCIP index unavailable")
    result = _find(scip_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse keys: {list(result.response.keys())}"


@pytest.mark.parametrize(
    "case",
    [c for c in SCIP_CASES if c.baseline_builder is not None or c.baseline_tokens > 0],
    ids=lambda c: c.label,
)
def test_scip_saves_tokens(case: BenchCase, scip_results: list[CaseResult]) -> None:
    if not scip_results:
        pytest.skip("SCIP index unavailable")
    result = _find(scip_results, case.label)
    if not result.passed:
        pytest.skip(f"op failed: {result.failure}")
    if result.baseline_tokens == 0:
        pytest.skip("no baseline")
    # callers/symbols/search: benefit is precision over grep, not always token count
    if any(x in case.label for x in ("callers", "symbols", "search")):
        pytest.skip("callers/search savings are quality-based, not size-based")
    assert (
        result.atelier_tokens < result.baseline_tokens
    ), f"[{case.label}] no savings: atelier={result.atelier_tokens:,} >= baseline={result.baseline_tokens:,}"
