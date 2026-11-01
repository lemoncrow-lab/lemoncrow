"""A live Review server and a CLI may seed the same SQLite trace database."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime

from lemoncrow.core.foundation.history_store import HistoryStore
from lemoncrow.core.foundation.models import Trace


class _ShortBusyStore(HistoryStore):
    def _connect(self) -> sqlite3.Connection:
        conn = super()._connect()
        # Shorten the real SQLite lock wait to reproduce handoff without a
        # 120-second integration test. Production retains the generous timeout.
        conn.execute("PRAGMA busy_timeout = 70")
        return conn


def test_trace_seed_retries_transient_real_sqlite_writer_lock(tmp_path) -> None:
    store = _ShortBusyStore(tmp_path)
    store.init()
    holder = sqlite3.connect(store.db_path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Thread(target=lambda: (time.sleep(0.24), holder.commit()), daemon=True)
    release.start()
    try:
        store.record_trace(
            Trace(
                id="trace-r07-lock",
                session_id="r07",
                agent="codex",
                host="codex",
                domain="code",
                task="One exact Review trace",
                status="success",
                created_at=datetime.now(UTC),
            ),
            write_json=False,
        )
        release.join(timeout=2)
        assert not release.is_alive()
        with sqlite3.connect(store.db_path) as verify:
            assert verify.execute("SELECT COUNT(*) FROM traces WHERE id = ?", ("trace-r07-lock",)).fetchone()[0] == 1
            assert (
                verify.execute("SELECT COUNT(*) FROM traces_fts WHERE id = ?", ("trace-r07-lock",)).fetchone()[0] == 1
            )
    finally:
        release.join(timeout=2)
        holder.close()
