"""Benchmark cases for the `sql` MCP tool.

Savings come from structured dispatch: one call handles connect+overview,
schema inspection, batched queries, and linting vs N separate raw queries
or repeated schema reads.

Baseline estimates:
  - connect:     manual DB path discovery + sqlite3 call (~300 tokens overhead)
  - query-batch: N separate query calls, each with framing (~150 * N tokens)
  - lint:        agent reads file, strips comments, runs regex check (~200 tokens)

SQL_TEST_DB env var must point to a SQLite DB created by the bench fixture.
"""

from __future__ import annotations

from typing import Any

from benchmarks.mcp_tools.harness import BenchCase

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def _assert_connect(result: dict[str, Any]) -> None:
    assert not result.get("isError"), f"connect must not error, got: {result}"
    assert "overview" in result, f"connect must return overview, got: {list(result)}"
    overview = result["overview"]
    assert "tables" in overview, f"overview must have tables, got: {list(overview)}"
    assert len(overview["tables"]) >= 1, "DB must have at least one table"


def _assert_query_result(result: dict[str, Any]) -> None:
    assert not result.get("isError"), f"query must not error, got: {result}"
    assert "results" in result, f"query must return results, got: {list(result)}"
    assert len(result["results"]) >= 1, "results must have at least one output"
    r0 = result["results"][0]
    assert "rows" in r0, f"result must have rows, got: {list(r0)}"


def _assert_batch_query(result: dict[str, Any]) -> None:
    assert not result.get("isError"), f"batch query must not error, got: {result}"
    assert "results" in result, f"batch must return results, got: {list(result)}"
    assert len(result["results"]) == 2, f"batch of 2 queries must return 2 results, got {len(result['results'])}"
    for r in result["results"]:
        assert not r.get("isError"), f"each batch result must not error, got: {r}"


def _assert_lint_ok(result: dict[str, Any]) -> None:
    assert not result.get("isError"), f"lint of valid SQL must not error, got: {result}"
    assert result.get("ok") is True, f"lint must return ok=True for valid SQL, got: {result}"


def _assert_lint_fail(result: dict[str, Any]) -> None:
    assert result.get("isError") or result.get("ok") is False, f"lint of invalid SQL must flag error, got: {result}"


# ---------------------------------------------------------------------------
# Cases  (SQL_TEST_DB is injected by bench_sql.py via args substitution)
# ---------------------------------------------------------------------------

SQL_CASES: list[BenchCase] = [
    BenchCase(
        op="sql",
        label="sql/connect",
        args={
            "action": "connect",
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=["overview"],
        custom_assert=_assert_connect,
        # baseline: manual sqlite3.connect + cursor + fetchall to get tables (~300 tokens)
        baseline_tokens=300,
    ),
    BenchCase(
        op="sql",
        label="sql/query-single",
        args={
            "action": "query",
            "sql": "SELECT id, name FROM users ORDER BY id",
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=["results"],
        custom_assert=_assert_query_result,
        # baseline: raw sqlite3 execute + fetchall + manual dict build (~150 tokens)
        baseline_tokens=150,
    ),
    BenchCase(
        op="sql",
        label="sql/query-batch",
        args={
            "action": "query",
            "queries": [
                {"name": "all_users", "sql": "SELECT id, name FROM users"},
                {"name": "count", "sql": "SELECT COUNT(*) AS total FROM users"},
            ],
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=["results"],
        custom_assert=_assert_batch_query,
        # baseline: 2 separate query calls, each ~150 tokens
        baseline_tokens=300,
    ),
    BenchCase(
        op="sql",
        label="sql/lint-valid",
        args={
            "action": "lint",
            "sql": "SELECT id, name FROM users WHERE id = 1",
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=["ok"],
        custom_assert=_assert_lint_ok,
        # baseline: agent manually strips comments + checks statement count (~200 tokens)
        baseline_tokens=200,
    ),
    BenchCase(
        op="sql",
        label="sql/lint-multi-statement-fail",
        args={
            "action": "lint",
            "sql": "SELECT 1; SELECT 2",
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=[],
        custom_assert=_assert_lint_fail,
        # correctness only
        baseline_tokens=0,
    ),
]
