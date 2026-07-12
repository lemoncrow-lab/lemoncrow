"""``lc db`` -- database maintenance (reclaim space, start fresh)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import click

from lemoncrow.gateway.cli.commands._shared import _emit

# The store is split into six per-concern SQLite files (see infra.storage.bundle).
# Trace history -- the large, reclaimable data -- lives in the history file.
HISTORY_DB_NAME = "lemoncrow_history.db"
SPLIT_DB_NAMES = (
    HISTORY_DB_NAME,
    "lemoncrow_knowledge.db",
    "lemoncrow_lessons.db",
    "lemoncrow_jobs.db",
    "lemoncrow_memory.db",
    "lemoncrow_telemetry.db",
)


@click.group("db")
def db_group() -> None:
    """Database maintenance for the LemonCrow store."""


@db_group.command("vacuum")
@click.option("--reset-traces", is_flag=True, help="Delete trace history first (start fresh), then reclaim space.")
@click.option("-f", "--force", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def db_vacuum_cmd(ctx: click.Context, reset_traces: bool, force: bool, as_json: bool) -> None:
    """VACUUM every LemonCrow store file; with --reset-traces, drop trace history first."""
    root: Path = ctx.obj["root"]
    db_paths = [root / name for name in SPLIT_DB_NAMES if (root / name).exists()]
    if not db_paths:
        click.echo("no LemonCrow store to vacuum")
        return

    before = sum(p.stat().st_size for p in db_paths)
    if reset_traces and not force:
        click.confirm(
            "Delete ALL trace history (traces, search index, raw artifacts) and reclaim space?",
            abort=True,
        )

    cleared: dict[str, int] = {}
    history_db = root / HISTORY_DB_NAME
    if reset_traces and history_db.exists():
        conn = sqlite3.connect(str(history_db))
        try:
            # Trace history lives in the history file: traces + its FTS mirror,
            # sync bookkeeping, and redacted raw artifacts. Drop them so the
            # subsequent VACUUM reclaims the freed pages.
            for table in ("traces", "traces_fts", "sync_status", "raw_artifacts"):
                try:
                    cleared[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
                except sqlite3.OperationalError:
                    continue
            conn.commit()
        finally:
            conn.close()

    for db_path in db_paths:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()

    after = sum(p.stat().st_size for p in db_paths)
    result = {
        "before_mb": round(before / 1e6, 1),
        "after_mb": round(after / 1e6, 1),
        "reclaimed_mb": round((before - after) / 1e6, 1),
        "reset_traces": reset_traces,
        "cleared_rows": cleared,
    }
    if as_json:
        _emit(result, as_json=True)
        return
    line = (
        f"LemonCrow store: {result['before_mb']} MB -> {result['after_mb']} MB (reclaimed {result['reclaimed_mb']} MB)"
    )
    if reset_traces:
        line += f"; cleared {sum(cleared.values())} trace rows"
    click.echo(line)
