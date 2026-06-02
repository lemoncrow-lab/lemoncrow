"""Benchmark cases for the `sql` MCP tool."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from benchmarks.mcp_tools.harness import BenchCase


def _assert_connect(result: dict[str, Any]) -> None:
    assert not result.get("isError"), f"connect must not error, got: {result}"
    assert "overview" in result, f"connect must return overview, got: {list(result)}"
    overview = result["overview"]
    assert "tables" in overview, f"overview must have tables, got: {list(overview)}"
    assert len(overview["tables"]) >= 1, "DB must have at least one table"


def _assert_query_rows(min_rows: int = 1, max_rows: int | None = None) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        assert not result.get("isError"), f"query must not error, got: {result}"
        assert "results" in result, f"query must return results, got: {list(result)}"
        rows = result["results"][0]["rows"]
        assert len(rows) >= min_rows, f"expected at least {min_rows} rows, got {len(rows)}"
        if max_rows is not None:
            assert len(rows) <= max_rows, f"expected at most {max_rows} rows, got {len(rows)}"

    return _assert


def _assert_batch_query(expected_results: int) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        assert not result.get("isError"), f"batch query must not error, got: {result}"
        assert "results" in result, f"batch must return results, got: {list(result)}"
        assert (
            len(result["results"]) == expected_results
        ), f"batch of {expected_results} queries must return {expected_results} results, got {len(result['results'])}"
        for item in result["results"]:
            assert not item.get("isError"), f"each batch result must not error, got: {item}"

    return _assert


def _assert_lint_ok(result: dict[str, Any]) -> None:
    assert not result.get("isError"), f"lint of valid SQL must not error, got: {result}"
    assert result.get("ok") is True, f"lint must return ok=True for valid SQL, got: {result}"


def _assert_lint_fail(result: dict[str, Any]) -> None:
    assert result.get("isError") or result.get("ok") is False, f"lint of invalid SQL must flag error, got: {result}"


def _assert_query_error(result: dict[str, Any]) -> None:
    assert result.get("isError"), f"query should error, got: {result}"


SQL_CASES: list[BenchCase] = [
    BenchCase(
        op="sql",
        label="sql/connect",
        args={"action": "connect", "connection_string": "__SQL_TEST_DB__"},
        assert_keys=["overview"],
        custom_assert=_assert_connect,
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
        custom_assert=_assert_query_rows(),
        baseline_tokens=150,
    ),
    BenchCase(
        op="sql",
        label="sql/query-filtered",
        args={
            "action": "query",
            "sql": "SELECT id, email FROM users WHERE name = 'Alice'",
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=["results"],
        custom_assert=_assert_query_rows(1, 1),
        baseline_tokens=170,
    ),
    BenchCase(
        op="sql",
        label="sql/query-count",
        args={
            "action": "query",
            "sql": "SELECT COUNT(*) AS total FROM users",
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=["results"],
        custom_assert=_assert_query_rows(1, 1),
        baseline_tokens=150,
    ),
    BenchCase(
        op="sql",
        label="sql/query-max-rows",
        args={
            "action": "query",
            "sql": "SELECT id, name FROM users ORDER BY id",
            "connection_string": "__SQL_TEST_DB__",
            "max_rows": 2,
            "auto_limit": True,
        },
        assert_keys=["results"],
        custom_assert=_assert_query_rows(1, 2),
        baseline_tokens=160,
    ),
    BenchCase(
        op="sql",
        label="sql/query-batch/02",
        args={
            "action": "query",
            "queries": [
                {"name": "all_users", "sql": "SELECT id, name FROM users"},
                {"name": "count", "sql": "SELECT COUNT(*) AS total FROM users"},
            ],
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=["results"],
        custom_assert=_assert_batch_query(2),
        baseline_tokens=300,
    ),
    BenchCase(
        op="sql",
        label="sql/query-batch/03",
        args={
            "action": "query",
            "queries": [
                {"name": "ids", "sql": "SELECT id FROM users ORDER BY id"},
                {"name": "emails", "sql": "SELECT email FROM users ORDER BY id"},
                {"name": "count", "sql": "SELECT COUNT(*) AS total FROM users"},
            ],
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=["results"],
        custom_assert=_assert_batch_query(3),
        baseline_tokens=450,
    ),
    BenchCase(
        op="sql",
        label="sql/lint-valid/basic",
        args={
            "action": "lint",
            "sql": "SELECT id, name FROM users WHERE id = 1",
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=["ok"],
        custom_assert=_assert_lint_ok,
        baseline_tokens=200,
    ),
    BenchCase(
        op="sql",
        label="sql/lint-valid/order",
        args={
            "action": "lint",
            "sql": "SELECT id, email FROM users ORDER BY email DESC",
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=["ok"],
        custom_assert=_assert_lint_ok,
        baseline_tokens=200,
    ),
    BenchCase(
        op="sql",
        label="sql/lint-invalid/multi-statement",
        args={
            "action": "lint",
            "sql": "SELECT 1; SELECT 2",
            "connection_string": "__SQL_TEST_DB__",
        },
        assert_keys=[],
        custom_assert=_assert_lint_fail,
        baseline_tokens=0,
    ),
    BenchCase(
        op="sql",
        label="sql/query-disallow-write",
        args={
            "action": "query",
            "sql": "INSERT INTO users VALUES (4, 'Dan', 'dan@example.com')",
            "connection_string": "__SQL_TEST_DB__",
            "allow_writes": False,
        },
        assert_keys=[],
        custom_assert=_assert_query_error,
        baseline_tokens=0,
    ),
    BenchCase(
        op="sql",
        label="sql/query-missing-sql",
        args={"action": "query", "connection_string": "__SQL_TEST_DB__"},
        assert_keys=[],
        custom_assert=_assert_query_error,
        baseline_tokens=0,
    ),
]
