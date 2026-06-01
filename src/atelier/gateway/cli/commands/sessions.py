from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from atelier.core.foundation.models import Trace, to_jsonable
from atelier.gateway.cli.commands._shared import _emit, _load_store, _parse_duration


@click.group("runs")
def runs_group() -> None:
    """Run record, list, and inspect commands."""


@runs_group.command("record")
@click.option(
    "--input",
    "input_path",
    type=click.Path(path_type=Path),
    default="-",
    show_default=True,
    help="Trace JSON file. Use '-' for stdin.",
)
@click.pass_context
def trace_record(ctx: click.Context, input_path: Path | str) -> None:
    """Record an observable trace."""
    import sys

    store = _load_store(ctx.obj["root"])
    raw = sys.stdin.read() if str(input_path) == "-" else Path(input_path).read_text("utf-8")
    data = json.loads(raw)
    if "id" not in data:
        data["id"] = Trace.make_id(data.get("task", "untitled"), data.get("agent", "agent"))
    trace = Trace.model_validate(data)
    store.record_trace(trace)
    click.echo(trace.id)


@runs_group.command("list")
@click.option("--domain", default=None, help="Filter by domain.")
@click.option("--status", default=None, type=click.Choice(["success", "failed", "partial"]))
@click.option("--agent", default=None, help="Filter by agent name.")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def trace_list(
    ctx: click.Context,
    domain: str | None,
    status: str | None,
    agent: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """List recorded traces."""
    store = _load_store(ctx.obj["root"])
    traces = store.list_traces(domain=domain, status=status, agent=agent, limit=limit)
    if as_json:
        _emit([to_jsonable(t) for t in traces], as_json=True)
        return
    if not traces:
        click.echo("(no traces)")
        return
    for t in traces:
        click.echo(f"{t.id}\t{t.agent}\t{t.status}\t{t.domain}\t{t.task[:60]}")


@runs_group.command("show")
@click.argument("trace_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def trace_show(ctx: click.Context, trace_id: str, as_json: bool) -> None:
    """Show a single trace by ID."""
    store = _load_store(ctx.obj["root"])
    trace = store.get_trace(trace_id)
    if trace is None:
        raise click.ClickException(f"trace not found: {trace_id}")
    if as_json:
        _emit(to_jsonable(trace), as_json=True)
        return
    click.echo(f"id:     {trace.id}")
    click.echo(f"agent:  {trace.agent}")
    click.echo(f"status: {trace.status}")
    click.echo(f"domain: {trace.domain}")
    click.echo(f"task:   {trace.task}")


@click.group("outcomes")
def outcomes_group() -> None:
    """Inspect captured route and compact decision outcomes."""


@outcomes_group.command("show")
@click.argument("session_id")
@click.pass_context
def outcomes_show(ctx: click.Context, session_id: str) -> None:
    """Print JSON outcome data for SESSION_ID."""
    from atelier.infra.runtime.outcome_capture import load_outcomes_from_state

    root: Path = ctx.obj["root"]
    path = root / "runs" / f"{session_id}_outcomes.json"
    data = load_outcomes_from_state(path)
    click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


@outcomes_group.command("summary")
@click.option("--since", default="7d", show_default=True, help="Look-back window, e.g. 7d, 24h.")
@click.pass_context
def outcomes_summary(ctx: click.Context, since: str) -> None:
    """Aggregate outcome_scores by (kind, tool) and print averages."""

    from atelier.infra.runtime.outcome_capture import (
        load_outcomes_from_state,
        summarise_outcomes,
    )

    cutoff = datetime.now(UTC) - _parse_duration(since)
    root: Path = ctx.obj["root"]
    runs_dir = root / "runs"
    if not runs_dir.exists():
        click.echo(json.dumps([], indent=2))
        return

    combined: dict[str, list[dict[str, Any]]] = {
        "route_outcomes": [],
        "compact_outcomes": [],
    }
    for outcomes_file in runs_dir.glob("*_outcomes.json"):
        try:
            mtime = datetime.fromtimestamp(outcomes_file.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        data = load_outcomes_from_state(outcomes_file)
        combined["route_outcomes"].extend(data.get("route_outcomes") or [])
        combined["compact_outcomes"].extend(data.get("compact_outcomes") or [])

    summary = summarise_outcomes(combined)
    click.echo(json.dumps(summary, indent=2, ensure_ascii=False, default=str))


@click.group("session")
def session_group() -> None:
    """Per-session cost and savings reports."""


@session_group.command("report")
@click.argument("session_id", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.option("--no-color", is_flag=True, default=False, help="Disable ANSI colours.")
@click.pass_context
def session_report_cmd(
    ctx: click.Context,
    session_id: str | None,
    as_json: bool,
    no_color: bool,
) -> None:
    """Show cost and savings breakdown for SESSION_ID (default: most recent)."""
    from atelier.infra.runtime.session_report import (
        list_run_files,
        load_report,
        render_json,
        render_text,
    )

    root: Path = ctx.obj["root"]

    if session_id is None:
        files = list_run_files(root)
        if not files:
            click.echo("No sessions found - run any AI command first.", err=True)
            raise SystemExit(1)
        session_id = files[0].stem

    report = load_report(session_id, root)
    if report is None:
        click.echo(f"Session '{session_id}' not found in {root / 'runs'}.", err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(render_json(report))
    else:
        click.echo(render_text(report, no_color=no_color))


@session_group.command("list")
@click.option("--since", default=None, help="Look-back window, e.g. 7d, 24h.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def session_list_cmd(ctx: click.Context, since: str | None, as_json: bool) -> None:
    """List recent sessions with costs and durations (newest first, max 20)."""
    import dataclasses

    from atelier.infra.runtime.session_report import (
        build_report,
        list_run_files,
    )

    root: Path = ctx.obj["root"]
    cutoff = datetime.now(UTC) - _parse_duration(since) if since else None
    files = list_run_files(root, since=cutoff)[:20]

    if not files:
        msg = "No sessions found"
        if since:
            msg += f" in the last {since}"
        click.echo(msg + ".", err=True)
        return

    rows = []
    for f in files:
        try:
            snapshot = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            report = build_report(snapshot, root)
        except Exception:
            logging.exception("session report build failed for %s", f)
            continue
        rows.append(report)

    if as_json:
        click.echo(
            json.dumps(
                [dataclasses.asdict(r) for r in rows],
                default=str,
                indent=2,
            )
        )
        return

    hdr = f"  {'Session':<10} {'Started':<22} {'Duration':<14} {'Turns':>6} {'Cost':>9} {'Saved':>9}"
    click.echo(hdr)
    click.echo("  " + "─" * (len(hdr) - 2))
    for r in rows:
        sid = r.session_id[:10]
        started = r.started_at.strftime("%Y-%m-%d %H:%M")
        from atelier.infra.runtime.session_report import (
            _fmt_cost,
            _fmt_duration,
        )

        dur = _fmt_duration(r.duration_seconds, r.is_running)
        click.echo(
            f"  {sid:<10} {started:<22} {dur:<14} {r.total_turns:>6}"
            f" {_fmt_cost(r.total_cost_usd):>9} {_fmt_cost(r.total_atelier_savings_usd):>9}"
        )


__all__ = ["outcomes_group", "runs_group", "session_group"]
