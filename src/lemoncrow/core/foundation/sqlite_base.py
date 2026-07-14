"""Shared SQLite connection/transaction/migration machinery.

Each of LemonCrow's storage concerns (history, knowledge, lessons, jobs,
memory, telemetry) is its own physical SQLite file so SQLite's single-writer
lock only ever contends within one concern -- e.g. ``servicectl tick``
(jobs + history) and ``mcp server`` (knowledge, read-mostly) never block each
other, because they open different files.

Subclasses set ``SCHEMA`` (DDL for their own tables only, nothing else) and
``REQUIRED_TABLES`` (for post-init verification). ``MIGRATIONS`` is available
for future schema changes applied after the initial DDL -- empty today,
since every current store's SCHEMA already includes every column.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar


class SqliteTableStore:
    """Base class: one store = one SQLite file = one set of tables."""

    SCHEMA: str = ""
    MIGRATIONS: ClassVar[tuple[str, ...]] = ()
    REQUIRED_TABLES: ClassVar[tuple[str, ...]] = ()

    def __init__(self, root: Path | str, *, db_name: str) -> None:
        self.root = Path(root).resolve()
        self.db_path = self.root / db_name
        self._connection: sqlite3.Connection | None = None

    # ----- lifecycle ------------------------------------------------------- #

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.executescript(self.SCHEMA)
            self._apply_migrations(conn)
            self._verify_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        if self._connection:
            return self._connection

        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Connection for one read or write; commits/rolls back on exit.

        Outside batch_mode() this is identical to using ``self._connect()``
        directly -- ``_connect()`` hands back a fresh connection each call, and
        wrapping it in ``with conn:`` commits (or rolls back, on exception)
        exactly as before. Inside batch_mode(), though, ``_connect()`` returns
        the SHARED batch connection for every call, so a per-call ``with conn:``
        would commit -- and on exception, roll back only the current call, not
        the batch -- after the very first one, silently splitting "one atomic
        import" into many small auto-committed transactions. When the
        connection IS the batch connection we skip the per-call commit/rollback
        entirely and let batch_mode's own try/except own the transaction
        boundary for the whole batch.
        """
        conn = self._connect()
        if conn is self._connection:
            yield conn
        else:
            with conn:
                yield conn

    @contextlib.contextmanager
    def batch_mode(self) -> Iterator[sqlite3.Connection]:
        """Wrap multiple operations in a single connection and transaction.

        Optimized for bulk imports with high-performance PRAGMAs.
        """
        conn = self._connect()
        # NORMAL, not OFF: this db holds non-derivable state, so a bulk import
        # must not risk corrupting it on power loss. Under WAL, NORMAL still
        # skips the fsync-per-statement that OFF also skips, but fsyncs the WAL
        # at each checkpoint, so a crash can only lose the in-flight transaction
        # (rolled back on reopen), never corrupt the db -- OFF gives no such
        # guarantee. One BEGIN/COMMIT for the whole batch already amortizes
        # that fsync across the entire import, so NORMAL costs far less here
        # than it would per-statement.
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA cache_size = -{512 * 1024}")  # 512MB cache

        conn.execute("BEGIN TRANSACTION")
        old_conn = self._connection
        self._connection = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._connection = old_conn
            conn.close()

    def _build_fts_prefix_query(self, query: str) -> str:
        """Build a robust FTS5 ``MATCH`` query from free-form user text.

        Quoted phrases match verbatim (inner double-quotes escaped); bare
        words are split on non-alphanumeric/underscore boundaries into prefix
        terms (``term*``) joined by AND, preferring recall over strict phrase
        matching. Falls back to the escaped whole query when nothing tokenizes.
        Shared by history (trace search) and knowledge (playbook search).
        """
        clauses: list[str] = []
        for phrase, token in re.findall(r'"([^"]+)"|(\S+)', query):
            term = (phrase or token).strip().lower()
            if not term:
                continue
            if phrase:
                escaped = term.replace('"', '""')
                clauses.append(f'"{escaped}"')
                continue
            pieces = [piece for piece in re.split(r"[^0-9a-z_]+", term) if piece]
            clauses.extend(f"{piece}*" for piece in pieces)
        if clauses:
            return " AND ".join(clauses)
        escaped = query.strip().replace('"', '""')
        return f'"{escaped}"'

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        if not self.MIGRATIONS:
            return
        from lemoncrow.infra.storage.migrations import read_migration

        conn.executescript(
            "CREATE TABLE IF NOT EXISTS _schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL);"
        )
        applied = {row[0] for row in conn.execute("SELECT name FROM _schema_migrations").fetchall()}
        for name in self.MIGRATIONS:
            if name in applied:
                continue
            for stmt in (s.strip() for s in read_migration(name).split(";")):
                if not stmt:
                    continue
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    msg = str(exc).lower()
                    if "duplicate column name" not in msg and "already exists" not in msg:
                        raise
            conn.execute(
                "INSERT OR IGNORE INTO _schema_migrations (name, applied_at) VALUES (?, datetime('now'))",
                (name,),
            )
            conn.commit()

    def _verify_schema(self, conn: sqlite3.Connection) -> bool:
        """Return True when every table this store owns exists."""
        if not self.REQUIRED_TABLES:
            return True
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name IN ({})".format(
                ",".join("?" for _ in self.REQUIRED_TABLES)
            ),
            self.REQUIRED_TABLES,
        ).fetchall()
        found = {row["name"] for row in rows}
        missing = set(self.REQUIRED_TABLES) - found
        if missing:
            raise RuntimeError(f"missing tables in {self.db_path.name}: {', '.join(sorted(missing))}")
        return True

    def health_check(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1")
            return {"ok": True, "backend": "sqlite", "db_path": str(self.db_path)}
        except Exception as exc:
            logging.exception("Recovered from broad exception handler")
            return {"ok": False, "backend": "sqlite", "db_path": str(self.db_path), "error": str(exc)}


__all__ = ["SqliteTableStore"]
