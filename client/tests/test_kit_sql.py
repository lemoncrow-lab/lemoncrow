"""The kit's ``sql`` engine: a read cannot write, and lint checks the real schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.kit.sql import discover_connection, render_sql_text, run_sql_tool

_WRITES_ON = {"LEMONCROW_SQL_ALLOW_WRITES": "1"}


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Run from an empty directory: a DSN resolved against the working
    # directory instead of the repository lands there, not in the checkout.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    root = tmp_path / "repo"
    root.mkdir()
    conn = sqlite3.connect(root / "shop.db")
    conn.executescript(
        "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO users(name) VALUES ('a'), ('b'), ('c');"
    )
    conn.close()
    return root


def _sql(repo: Path, action: str, **arguments: Any) -> dict[str, Any]:
    arguments.setdefault("connection", "sqlite:///shop.db")
    arguments.setdefault("env", {})
    return run_sql_tool(action=action, repo_root=repo, **arguments)


def _users(repo: Path) -> int:
    conn = sqlite3.connect(repo / "shop.db")
    try:
        return int(conn.execute("SELECT count(*) FROM users").fetchone()[0])
    finally:
        conn.close()


def test_a_read_cannot_switch_the_journal_mode(repo: Path) -> None:
    result = _sql(repo, "query", sql="PRAGMA journal_mode(wal)")
    assert result["isError"]
    assert result["results"][0]["message"] == "attempt to write a readonly database"
    conn = sqlite3.connect(repo / "shop.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()


def test_a_read_never_creates_a_missing_database(repo: Path) -> None:
    result = _sql(repo, "tables", connection="sqlite:///missing.db")
    assert result == {"isError": True, "message": "no such database file: missing.db"}
    assert not (repo / "missing.db").exists()


def test_a_permitted_write_may_create_a_database(repo: Path) -> None:
    result = _sql(repo, "query", sql="CREATE TABLE t(a)", connection="sqlite:///new.db", write=True, env=_WRITES_ON)
    assert result["isError"] is False
    assert (repo / "new.db").is_file()


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_a_false_switch_does_not_enable_writes(repo: Path, value: str) -> None:
    result = _sql(repo, "query", sql="DELETE FROM users", write=True, env={"LEMONCROW_SQL_ALLOW_WRITES": value})
    assert "LEMONCROW_SQL_ALLOW_WRITES=1" in result["results"][0]["message"]
    assert _users(repo) == 3


def test_a_false_switch_does_not_open_a_database_outside_the_repository(repo: Path, tmp_path: Path) -> None:
    sqlite3.connect(tmp_path / "ext.db").close()
    env = {"LEMONCROW_SQL_ALLOW_EXTERNAL_DB": "0"}
    result = _sql(repo, "tables", connection=f"sqlite:///{tmp_path / 'ext.db'}", env=env)
    assert result["isError"]
    assert "outside the repo sandbox" in result["message"]


def test_a_relative_dsn_names_a_file_under_the_repository(repo: Path, tmp_path: Path) -> None:
    for dsn in ("sqlite:///shop.db", "shop.db", "file:shop.db"):
        result = _sql(repo, "query", sql="SELECT count(*) AS n FROM users", connection=dsn)
        assert result["results"][0]["rows"] == [[3]], dsn
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_a_write_refusal_names_what_would_permit_it(repo: Path) -> None:
    refused = _sql(repo, "query", sql="DELETE FROM users")["results"][0]["message"]
    assert "pass write=true" in refused
    refused = _sql(repo, "query", sql="DELETE FROM users", write=True)["results"][0]["message"]
    assert "LEMONCROW_SQL_ALLOW_WRITES=1" in refused


def test_lint_compiles_against_the_schema_without_running_anything(repo: Path) -> None:
    assert _sql(repo, "lint", sql="SELECT name FROM users") == {"isError": False, "ok": True, "message": "ok"}
    assert _sql(repo, "lint", sql="SELECT * FROM nosuch")["message"] == "no such table: nosuch"
    assert "syntax error" in _sql(repo, "lint", sql="SELEC 1")["message"]
    assert _sql(repo, "lint", sql="EXPLAIN QUERY PLAN SELECT * FROM users")["ok"]
    assert _sql(repo, "lint", sql="DELETE FROM users", write=True, env=_WRITES_ON)["ok"]
    assert _users(repo) == 3


def test_lint_checks_every_query_of_a_batch(repo: Path) -> None:
    batch = [{"name": "good", "sql": "SELECT 1"}, {"name": "bad", "sql": "SELECT * FROM nosuch"}]
    result = _sql(repo, "lint", queries=batch)
    assert result == {"isError": True, "ok": False, "message": "bad: no such table: nosuch", "calls_saved": 1}
    assert render_sql_text(result) == "### sql lint: bad: no such table: nosuch"


def test_a_write_reports_the_rows_it_changed(repo: Path) -> None:
    result = _sql(repo, "query", sql="UPDATE users SET name = 'z' WHERE id < 3", write=True, env=_WRITES_ON)
    assert result["results"][0]["rows_affected"] == 2
    assert render_sql_text(result) == "### sql query result · 0 rows · 2 affected"


def test_discovery_reads_only_the_environment_it_is_given(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///from-the-process.db")
    assert discover_connection(repo, env={})["connection_string"] is None
    (repo / ".env").write_text("# local\nexport DATABASE_URL=sqlite:///shop.db\n", encoding="utf-8")
    assert discover_connection(repo, env={}) == {
        "connection_string": "sqlite:///shop.db",
        "source": "dotenv:DATABASE_URL",
        "dialect": "sqlite",
    }
