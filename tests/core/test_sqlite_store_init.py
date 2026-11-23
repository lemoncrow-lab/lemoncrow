from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import ClassVar

import pytest

from lemoncrow.core.foundation.sqlite_base import SqliteTableStore, _migration_statements
from lemoncrow.infra.storage.migrations import read_migration
from lemoncrow.pro.capabilities.review.store import ReviewStore


class _TinyStore(SqliteTableStore):
    SCHEMA = "CREATE TABLE IF NOT EXISTS a (x INTEGER);\nCREATE TABLE IF NOT EXISTS b (y INTEGER);\n"
    REQUIRED_TABLES: ClassVar[tuple[str, ...]] = ("a", "b")

    def __init__(self, root: Path) -> None:
        super().__init__(root, db_name="tiny.db")


def _traced(store: SqliteTableStore, statements: list[str]) -> None:
    connect = store._connect

    def traced_connect() -> sqlite3.Connection:
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    store._connect = traced_connect  # type: ignore[method-assign]


def _objects(db_path: Path) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        return sorted(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    finally:
        conn.close()


def test_a_new_review_store_commits_its_schema_and_migrations_once(tmp_path: Path) -> None:
    # Autocommit made each of ~230 statements its own commit (an fsync each):
    # over a second for the first capture of every repository on a busy disk.
    store = ReviewStore(tmp_path)
    statements: list[str] = []
    _traced(store, statements)
    store.init()

    begin = statements.index("BEGIN IMMEDIATE;")
    end = statements.index("COMMIT", begin)
    creates = [i for i, s in enumerate(statements) if s.lstrip().upper().startswith("CREATE")]
    # Only review_meta (the version check that must precede any DDL) is created outside it.
    assert [statements[i].split("(")[0].strip() for i in creates if not begin < i < end] == [
        "CREATE TABLE IF NOT EXISTS review_meta"
    ]
    assert len(creates) > 100


def test_a_new_review_store_has_the_schema_statement_by_statement_init_built(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "one-transaction")
    store.init()

    reference = tmp_path / "reference"
    reference.mkdir()
    conn = sqlite3.connect(reference / "review.db")
    conn.executescript(ReviewStore.SCHEMA)
    conn.execute("CREATE TABLE IF NOT EXISTS _schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for name in ReviewStore.MIGRATIONS:
        for statement in _migration_statements(read_migration(name)):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                assert "duplicate column name" in str(exc).lower() or "already exists" in str(exc).lower()
        conn.execute("INSERT INTO _schema_migrations VALUES (?, datetime('now'))", (name,))
        conn.commit()
    conn.close()

    # ReviewStore.init creates review_meta with its own DDL before the schema runs.
    built = {row for row in _objects(store.db_path) if row[1] != "review_meta"}
    expected = {row for row in _objects(reference / "review.db") if row[1] != "review_meta"}
    assert built == expected
    applied = sqlite3.connect(store.db_path).execute("SELECT name FROM _schema_migrations ORDER BY name").fetchall()
    assert [row[0] for row in applied] == sorted(ReviewStore.MIGRATIONS)


def test_opening_an_existing_store_never_waits_for_the_write_lock(tmp_path: Path) -> None:
    _TinyStore(tmp_path).init()
    holder = sqlite3.connect(tmp_path / "tiny.db")
    holder.execute("BEGIN IMMEDIATE")
    try:
        opened = threading.Event()

        def reopen() -> None:
            _TinyStore(tmp_path).init()
            opened.set()

        worker = threading.Thread(target=reopen, daemon=True)
        worker.start()
        assert opened.wait(timeout=10.0)
    finally:
        holder.rollback()
        holder.close()


def test_a_failed_new_store_leaves_no_partial_schema(tmp_path: Path) -> None:
    class _Broken(_TinyStore):
        SCHEMA = _TinyStore.SCHEMA + "CREATE TABLE c (z INTEGER REFERENCES;\n"

    with pytest.raises(sqlite3.OperationalError):
        _Broken(tmp_path).init()
    assert _objects(tmp_path / "tiny.db") == []
    _TinyStore(tmp_path).init()
    assert [row[1] for row in _objects(tmp_path / "tiny.db")] == ["a", "b"]
