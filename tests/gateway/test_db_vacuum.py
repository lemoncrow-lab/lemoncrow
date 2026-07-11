from __future__ import annotations

import sqlite3
from pathlib import Path

from click.testing import CliRunner

from lemoncrow.core.foundation.store import ContextStore
from lemoncrow.gateway.cli import cli


def _seed_trace(root: Path) -> None:
    # The traces table (see SCHEMA in core.foundation.store) requires agent,
    # status, task, created_at, and payload NOT NULL -- supply all of them so
    # this seed row satisfies the real schema `db vacuum --reset-traces` acts on.
    ContextStore(root).init()
    with sqlite3.connect(str(root / "lemoncrow.db")) as conn:
        conn.execute(
            "INSERT INTO traces (id, agent, status, task, created_at, payload) "
            "VALUES ('legacy-1', 'codex', 'success', 'seed', '2026-01-01T00:00:00Z', '{}')"
        )
        conn.commit()


def _trace_count(root: Path) -> int:
    conn = sqlite3.connect(str(root / "lemoncrow.db"))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0])
    except sqlite3.OperationalError:
        return 0  # legacy table dropped by reset-traces
    finally:
        conn.close()


def test_db_vacuum_reset_traces_clears_history(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _seed_trace(root)
    assert _trace_count(root) == 1

    result = CliRunner().invoke(cli, ["--root", str(root), "db", "vacuum", "--reset-traces", "-f", "--json"])
    assert result.exit_code == 0, result.output
    assert _trace_count(root) == 0


def test_db_vacuum_without_reset_keeps_traces(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _seed_trace(root)
    result = CliRunner().invoke(cli, ["--root", str(root), "db", "vacuum", "--json"])
    assert result.exit_code == 0, result.output
    assert _trace_count(root) == 1  # vacuum alone must not delete data


def test_db_vacuum_no_db(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(tmp_path / ".lemoncrow"), "db", "vacuum"])
    assert result.exit_code == 0
    assert "no lemoncrow.db" in result.output
