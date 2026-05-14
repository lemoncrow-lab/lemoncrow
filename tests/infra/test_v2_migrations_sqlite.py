from __future__ import annotations

import sqlite3
from pathlib import Path

from atelier.core.foundation.store import ContextStore
from atelier.infra.storage.migrations import V2_REQUIRED_TABLES
from atelier.infra.storage.sqlite_store import SQLiteStore


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '%_config'"
        ).fetchall()
    return {row[0] for row in rows}


def test_v2_migrations_apply_idempotently_for_reasoning_store(tmp_path: Path) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    store.init()

    assert set(V2_REQUIRED_TABLES).issubset(_tables(store.db_path))
    assert store.verify_v2_schema()


def test_v2_migrations_apply_idempotently_for_sqlite_store(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "atelier")
    store.init()
    store.init()

    assert set(V2_REQUIRED_TABLES).issubset(_tables(store.db_path))
    assert store.health_check()["ok"] is True
