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
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar


class SqliteTableStore:
    """Base class: one store = one SQLite file = one set of tables."""

    SCHEMA: str = ""
    MIGRATIONS: ClassVar[tuple[str, ...]] = ()
    REQUIRED_TABLES: ClassVar[tuple[str, ...]] = ()

    # How long a bulk batch may hold the write lock before committing and
    # starting a fresh transaction. SQLite allows one writer per file, so an
    # unbroken BEGIN...COMMIT around a whole import starves every other writer
    # for the length of the run -- GH #43: a 690-session opencode import held
    # it for ~2 minutes and any concurrent `lc import` died on the 30s
    # busy_timeout. Chunking bounds that wait to roughly this interval while
    # keeping most of the fsync amortization a batch exists for.
    BATCH_CHECKPOINT_SECONDS: ClassVar[float] = 1.0

    # How long to stay out of the write lock after each checkpoint. Committing
    # alone is not enough: the batch re-opens its transaction on the very next
    # operation microseconds later, so the gap is far too narrow for another
    # process's busy-handler retry (which backs off in ~1-100ms steps) to land
    # in. Measured with a 100ms poller against a live import: 5 acquisitions in
    # 60s, longest block 26.5s -- effective starvation despite 1s chunks.
    # Sleeping here hands the lock over deterministically, at ~5% throughput.
    BATCH_CHECKPOINT_YIELD_SECONDS: ClassVar[float] = 0.05

    def __init__(self, root: Path | str, *, db_name: str) -> None:
        self.root = Path(root).resolve()
        self.db_path = self.root / db_name
        self._connection: sqlite3.Connection | None = None
        # Depth of nested _transaction() scopes on the batch connection; a
        # checkpoint is only safe at depth 0, where no caller is mid-way
        # through a multi-statement unit of work.
        self._batch_depth = 0
        self._batch_deadline: float | None = None

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

        conn = sqlite3.connect(self.db_path, timeout=120)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        # 120s, not 30s: chunked batches keep the typical wait near one
        # checkpoint interval, but a single huge record (a multi-hundred-MB
        # session artifact plus its FTS index) can hold the lock for ~20s on
        # its own -- measured. A writer that waits is always better than one
        # that fails with "database is locked" (GH #43), and readers never
        # wait at all under WAL.
        conn.execute("PRAGMA busy_timeout = 120000")
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
            # No explicit BEGIN here. isolation_level="IMMEDIATE" (set in
            # batch_mode) makes sqlite3 open the transaction implicitly, and
            # only before a write -- opening one here would take the write lock
            # for read-only operations too, which measured 2.9x slower on a
            # 690-session import (1m53s -> 5m25s).
            self._batch_depth += 1
            try:
                yield conn
            finally:
                self._batch_depth -= 1
            self._maybe_checkpoint_batch(conn)
        else:
            with conn:
                yield conn

    def _maybe_checkpoint_batch(self, conn: sqlite3.Connection) -> None:
        """Commit and reopen the batch transaction once it has run long enough.

        Called at the *end* of an outermost batch operation, which is a safe
        transaction boundary: each store method is one logical unit of work.
        Only runs on the success path -- an exception propagates to
        :meth:`batch_mode`, which rolls back.

        Commit only -- the next transaction is opened lazily by the next
        operation. Re-acquiring the write lock here instead would starve a
        waiting process: SQLite has no lock fairness, so a commit followed
        immediately by BEGIN IMMEDIATE on the same connection hands the lock
        straight back to this one, and the waiter still times out (observed:
        a concurrent import failed after 41s against a 30s busy_timeout even
        with 1s chunks). Leaving a real gap is what lets the other writer in.

        Trade-off (GH #43): the batch is no longer one all-or-nothing
        transaction. A crash mid-import keeps whatever was checkpointed rather
        than discarding the run. Each individual record stays atomic, and the
        write lock is released often enough that a concurrent writer proceeds
        instead of timing out.
        """
        if self._batch_depth or self._batch_deadline is None:
            return
        if time.monotonic() < self._batch_deadline:
            return
        if conn.in_transaction:
            conn.commit()
            # Leave the lock genuinely available for a moment; see the class
            # constant for why a bare commit is not enough.
            if self.BATCH_CHECKPOINT_YIELD_SECONDS:
                time.sleep(self.BATCH_CHECKPOINT_YIELD_SECONDS)
        self._batch_deadline = time.monotonic() + self.BATCH_CHECKPOINT_SECONDS

    @contextlib.contextmanager
    def read_scope(self) -> Iterator[sqlite3.Connection]:
        """Pin one connection for a run of read-only operations.

        Like :meth:`batch_mode` it stops every call from opening (and tearing
        down) its own connection, but it never begins a transaction, so it
        holds no lock and cannot block a writer. Use it around read-heavy loops
        that must stay outside a write batch -- e.g. the import reconstruction
        audit, where dropping the shared connection cost ~2x wall-clock on a
        690-session import (1m53s -> 3m47s) purely in per-call connection setup
        and lost page cache.

        Writes issued inside this scope still work; sqlite3 opens an implicit
        transaction for them and the ``finally`` below commits it.
        """
        conn = self._connect()
        conn.execute(f"PRAGMA cache_size = -{512 * 1024}")  # 512MB cache
        old_isolation = conn.isolation_level
        conn.isolation_level = "IMMEDIATE"
        old_conn = self._connection
        old_deadline = self._batch_deadline
        self._connection = conn
        # No deadline: nothing to checkpoint, since no transaction is held.
        self._batch_deadline = None
        try:
            yield conn
            if conn.in_transaction:
                conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.isolation_level = old_isolation
            self._connection = old_conn
            self._batch_deadline = old_deadline
            conn.close()

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

        # IMMEDIATE, not deferred: a deferred BEGIN takes a read snapshot on
        # the first SELECT and then has to upgrade to a write lock. If another
        # process committed in between, SQLite fails that upgrade with
        # SQLITE_BUSY *immediately* -- busy_timeout does not apply to snapshot
        # conflicts -- so a concurrent import died instantly instead of waiting
        # (GH #43). Taking the write lock up front makes the wait honor
        # busy_timeout, and the checkpointing above keeps that wait short.
        # "IMMEDIATE" rather than the default deferred BEGIN: a deferred
        # transaction takes a read snapshot on the first SELECT and then has to
        # upgrade to a write lock, and SQLite fails that upgrade with
        # SQLITE_BUSY *immediately* if another process committed in between --
        # busy_timeout does not apply to snapshot conflicts, so a concurrent
        # import died instantly (GH #43). Letting sqlite3 issue the implicit
        # BEGIN keeps read-only operations out of the write lock entirely.
        old_isolation = conn.isolation_level
        conn.isolation_level = "IMMEDIATE"
        old_conn = self._connection
        old_deadline = self._batch_deadline
        self._connection = conn
        self._batch_deadline = time.monotonic() + self.BATCH_CHECKPOINT_SECONDS
        try:
            yield conn
            if conn.in_transaction:
                conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.isolation_level = old_isolation
            self._connection = old_conn
            self._batch_deadline = old_deadline
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
