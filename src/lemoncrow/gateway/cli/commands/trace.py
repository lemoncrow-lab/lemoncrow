"""``lemoncrow trace`` — inspect, replay and compare per-call runtime traces."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click

from lemoncrow.core.foundation.runtime_trace import TraceStore


def trace_stores(root: Path) -> list[TraceStore]:
    """The runtime store plus the thin client's, when they differ."""
    roots = [Path(root).expanduser()]
    home = os.environ.get("LEMONCROW_HOME", "").strip()
    client_root = Path(home).expanduser() if home else Path.home() / ".lemoncrow"
    if client_root.resolve() != roots[0].resolve():
        roots.append(client_root)
    return [TraceStore(r) for r in roots]


def iter_all_records(root: Path, *, days: int | None = None) -> Iterator[dict[str, Any]]:
    for store in trace_stores(root):
        yield from store.iter_records(days=days)


def find_records(root: Path, trace_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for store in trace_stores(root):
        found.extend(store.find(trace_id))
    return found


def _call(root: Path, trace_id: str) -> Any:
    from lemoncrow.core.capabilities.trace_replay import assemble_calls

    records = find_records(root, trace_id)
    if not records:
        raise click.ClickException(f"trace not found: {trace_id}")
    return assemble_calls(records)[0]


@click.group("trace")
def trace_group() -> None:
    """Per-call runtime traces (lt_…): request, decisions, retrieval, outcome."""


@trace_group.command("show")
@click.argument("trace_id")
@click.option("--json", "as_json", is_flag=True, help="Print every layer's raw record.")
@click.pass_context
def trace_show(ctx: click.Context, trace_id: str, as_json: bool) -> None:
    """Show one call across client, server and runtime layers."""
    records = find_records(ctx.obj["root"], trace_id)
    if not records:
        raise click.ClickException(f"trace not found: {trace_id}")
    if as_json:
        click.echo(json.dumps(records, indent=2, ensure_ascii=False, default=str))
        return
    call = _call(ctx.obj["root"], trace_id)
    click.echo(f"trace     {call.trace_id}")
    click.echo(f"tool      {call.tool}")
    click.echo(f"started   {call.started_at}")
    click.echo(f"layers    {', '.join(sorted(call.layers))}")
    click.echo(f"duration  {call.duration_ms} ms")
    click.echo(f"outcome   {json.dumps(call.outcome, default=str)}")
    click.echo(f"arguments {json.dumps(call.arguments, ensure_ascii=False, default=str)[:400]}")
    retrieval = call.retrieval
    if retrieval:
        click.echo(f"retrieval ranker={retrieval.get('ranker')} candidates={retrieval.get('candidate_total')}")
        for path in (retrieval.get("selected") or [])[:10]:
            click.echo(f"  → {path}")
        suff = (retrieval.get("extra") or {}).get("sufficiency")
        if isinstance(suff, dict):
            click.echo(f"sufficiency {suff.get('score')} sufficient={suff.get('sufficient')}")
    for decision in call.decisions:
        reasons = ",".join(decision.get("reason_codes") or [])
        click.echo(f"decision  {decision.get('kind')} [{decision.get('mode')}] {reasons}")


@trace_group.command("list")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--tool", default=None, help="Only calls to this tool.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def trace_list(ctx: click.Context, limit: int, tool: str | None, as_json: bool) -> None:
    """Most recent calls, newest first."""
    from lemoncrow.core.capabilities.trace_replay import assemble_calls

    calls = [c for c in assemble_calls(iter_all_records(ctx.obj["root"], days=14)) if tool is None or c.tool == tool]
    calls = sorted(calls, key=lambda c: c.started_at, reverse=True)[: max(0, limit)]
    if as_json:
        rows = [
            {"trace_id": c.trace_id, "tool": c.tool, "started_at": c.started_at, "layers": sorted(c.layers)}
            for c in calls
        ]
        click.echo(json.dumps(rows, indent=2))
        return
    if not calls:
        click.echo("(no traces)")
        return
    for c in calls:
        err = " error" if c.outcome.get("is_error") else ""
        click.echo(f"{c.trace_id}\t{c.started_at[:19]}\t{c.tool}\t{c.duration_ms}ms\t{'+'.join(sorted(c.layers))}{err}")


@trace_group.command("replay")
@click.argument("trace_id")
@click.option("--ranker", default="feature-v1", show_default=True, help="Ranker to replay the candidates with.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def trace_replay(ctx: click.Context, trace_id: str, ranker: str, as_json: bool) -> None:
    """Re-rank one recorded retrieval with another ranker (no index access)."""
    from lemoncrow.core.capabilities.context_engine import Candidate, ContextQuery, assess_sufficiency, get_ranker

    call = _call(ctx.obj["root"], trace_id)
    retrieval = call.retrieval
    if not retrieval or not retrieval.get("candidates"):
        raise click.ClickException(f"{trace_id} has no recorded retrieval candidates")
    query_text = retrieval.get("query")
    if not isinstance(query_text, str):
        raise click.ClickException(f"{trace_id} was recorded redacted; it cannot be replayed")
    try:
        chosen = get_ranker(ranker)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    extra = retrieval.get("extra") or {}
    query = ContextQuery(
        text=query_text,
        limit=int(retrieval.get("limit") or 8),
        scopes=tuple(str(s) for s in extra.get("scopes") or ()),
    )
    candidates = [Candidate.from_trace(row) for row in retrieval["candidates"]]
    replayed = chosen.rank(query, candidates)[: query.limit]
    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "query": query_text,
        "production": {"ranker": retrieval.get("ranker"), "selected": retrieval.get("selected") or []},
        "replay": {
            "ranker": chosen.name,
            "selected": [c.path for c in replayed],
            "sufficiency": assess_sufficiency(query, replayed).to_payload(),
        },
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    click.echo(f"query: {query_text}")
    production = payload["production"]["selected"]
    width = max([len(str(p)) for p in production] + [10])
    click.echo(f"{'production (' + str(retrieval.get('ranker')) + ')':<{width}}   replay ({chosen.name})")
    for index in range(max(len(production), len(replayed))):
        left = str(production[index]) if index < len(production) else ""
        right = replayed[index].path if index < len(replayed) else ""
        marker = " " if left == right else "*"
        click.echo(f"{left:<{width}} {marker} {right}")
    click.echo(f"replay sufficiency: {payload['replay']['sufficiency']['score']}")


@trace_group.command("compare")
@click.argument("trace_a")
@click.argument("trace_b")
@click.pass_context
def trace_compare(ctx: click.Context, trace_a: str, trace_b: str) -> None:
    """Structured diff of two calls (arguments, selection, decisions, outcome)."""
    from lemoncrow.core.capabilities.trace_replay import compare_calls

    diff = compare_calls(_call(ctx.obj["root"], trace_a), _call(ctx.obj["root"], trace_b))
    click.echo(json.dumps(diff, indent=2, ensure_ascii=False, default=str))


__all__ = ["find_records", "iter_all_records", "trace_group", "trace_stores"]
