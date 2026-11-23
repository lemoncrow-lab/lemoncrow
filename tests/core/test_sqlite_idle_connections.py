"""Idle SQLite connections: the bound follows the open-file limit, and a round of every store reuses them."""

from __future__ import annotations

import resource
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation import sqlite_base
from lemoncrow.core.foundation.sqlite_base import SqliteTableStore, _IdleConnections


class _Pooled(SqliteTableStore):
    SCHEMA = "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY);"
    REQUIRED_TABLES = ("items",)
    POOLED_CONNECTIONS = 2

    def __init__(self, root: Path) -> None:
        super().__init__(root, db_name="items.sqlite3")

    def count(self) -> int:
        with self._transaction() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])


@pytest.mark.parametrize(
    ("soft", "budget"),
    [(256, 64), (1024, 64), (4096, 256), (1_048_576, 512), (resource.RLIM_INFINITY, 512)],
)
def test_the_idle_bound_follows_the_open_file_limit(monkeypatch: pytest.MonkeyPatch, soft: int, budget: int) -> None:
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (soft, resource.RLIM_INFINITY))
    assert sqlite_base._idle_connection_budget() == budget


def test_a_round_of_more_stores_than_the_old_bound_reconnects_none_of_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home reads every repository's store in turn; at 64 kept, 94 stores reconnected all 94 on every read."""

    monkeypatch.setattr(sqlite_base, "_IDLE_CONNECTIONS", _IdleConnections(max_idle=512))
    stores = [_Pooled(tmp_path / f"repo-{index}") for index in range(94)]
    for store in stores:
        store.init()
        store.count()
    opened: list[str] = []
    real_connect = sqlite3.connect

    def counting(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(args[0]))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting)
    for store in stores:
        assert store.count() == 0
    assert opened == []
