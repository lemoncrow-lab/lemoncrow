"""``lemon db`` — database maintenance (reclaim space, start fresh)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import click

from lemoncrow.gateway.cli.commands._shared import _emit


@click.group("db")
def db_group() -> None:
    """Database maintenance for the LemonCrow store."""


@db_group.command("vacuum")
@click.option("--reset-traces", is_flag=True, help="Delete trace history first (start fresh), then reclaim space.")
@click.option("-f", "--force", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def db_vacuum_cmd(ctx: click.Context, reset_traces: bool, force: bool, as_json: bool) -> None:
    """VACUUM lemoncrow.db; with --reset-traces, drop trace history first to reclaim it."""
    root: Path = ctx.obj["root"]
    db_path = root / "lemoncrow.db"
    if not db_path.exists():
        click.echo("no lemoncrow.db to vacuum")
        return

    before = db_path.stat().st_size
    if reset_traces and not force:
        click.confirm(
            "Delete ALL trace history (traces, search index, raw artifacts) and reclaim space?",
            abort=True,
        )

    cleared: dict[str, int] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        if reset_traces:
            # traces/traces_fts/sync_status/raw_artifacts are legacy (removed from the
            # schema once sessions became file-based) — drop them to reclaim old DBs.
            for table in ("traces", "traces_fts", "sync_status", "raw_artifacts"):
                try:
                    cleared[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
                except sqlite3.OperationalError:
                    continue
            conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

    after = db_path.stat().st_size
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
    line = f"lemoncrow.db: {result['before_mb']} MB → {result['after_mb']} MB (reclaimed {result['reclaimed_mb']} MB)"
    if reset_traces:
        line += f"; cleared {sum(cleared.values())} trace rows"
    click.echo(line)
