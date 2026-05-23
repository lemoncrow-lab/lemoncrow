"""pytest-based MCP sql tool benchmark.

Run:
    uv run pytest benchmarks/mcp_tools/bench_sql.py -v -s

The fixture creates a temp SQLite DB with a `users` table and patches the
__SQL_TEST_DB__ placeholder in each case's args before running.
"""

from __future__ import annotations

import copy
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from benchmarks.mcp_tools.cases.sql import SQL_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def sql_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bench_sql")
    db_path = root / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT
        );
        INSERT INTO users VALUES (1, 'Alice', 'alice@example.com');
        INSERT INTO users VALUES (2, 'Bob', 'bob@example.com');
        INSERT INTO users VALUES (3, 'Carol', 'carol@example.com');
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture(scope="session")
def sql_tool_fn() -> Any:
    from atelier.gateway.adapters.mcp_server import tool_sql
    return tool_sql


def _patch_db(args: dict[str, Any], db_path: Path) -> dict[str, Any]:
    patched = copy.deepcopy(args)
    _patch_value(patched, "__SQL_TEST_DB__", f"sqlite:///{db_path}")
    return patched


def _patch_value(obj: Any, placeholder: str, value: str) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if v == placeholder:
                obj[k] = value
            else:
                _patch_value(v, placeholder, value)
    elif isinstance(obj, list):
        for item in obj:
            _patch_value(item, placeholder, value)


@pytest.fixture(scope="session")
def sql_bench_results(sql_db: Path, sql_tool_fn: Any) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in SQL_CASES:
        patched_args = _patch_db(case.args, sql_db)
        patched_case = BenchCase(
            op=case.op,
            label=case.label,
            args=patched_args,
            assert_keys=case.assert_keys,
            custom_assert=case.custom_assert,
            baseline_tokens=case.baseline_tokens,
        )
        results.append(run_case(patched_case, sql_tool_fn))
    return results


@pytest.fixture(scope="session", autouse=True)
def print_sql_report(sql_bench_results: list[CaseResult]) -> None:
    report = ToolReport(tool_name="sql", results=sql_bench_results)
    print(render_summary([report]))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _find(results: list[CaseResult], label: str) -> CaseResult:
    for r in results:
        if r.case.label == label:
            return r
    raise KeyError(f"no case with label={label!r}")


@pytest.mark.parametrize("case", SQL_CASES, ids=lambda c: c.label)
def test_sql_op_correctness(case: BenchCase, sql_bench_results: list[CaseResult]) -> None:
    result = _find(sql_bench_results, case.label)
    assert result.passed, f"[{case.label}] FAILED: {result.failure}\nresponse={result.response}"


@pytest.mark.parametrize(
    "case",
    [c for c in SQL_CASES if c.baseline_tokens > 0],
    ids=lambda c: c.label,
)
def test_sql_op_saves_tokens(case: BenchCase, sql_bench_results: list[CaseResult]) -> None:
    result = _find(sql_bench_results, case.label)
    if not result.passed:
        pytest.skip(f"skipping savings check — op failed: {result.failure}")
    assert result.atelier_tokens < case.baseline_tokens, (
        f"[{case.label}] no savings: atelier={result.atelier_tokens} >= baseline={case.baseline_tokens}"
    )
