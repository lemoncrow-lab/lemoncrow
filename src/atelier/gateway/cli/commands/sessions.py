from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from rich.console import Console as RichConsole

    from atelier.core.capabilities.savings_summary import TranscriptSavingsBlock

from atelier.core.foundation.models import Trace, to_jsonable
from atelier.core.foundation.store import ContextStore
from atelier.gateway.cli.commands._shared import _emit, _load_store, _parse_duration
from atelier.gateway.hosts.session_parsers.registry import (
    SUPPORTED_SESSION_IMPORT_HOSTS,
)


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
    from atelier.core.foundation.paths import find_session_dir
    from atelier.infra.runtime.outcome_capture import load_outcomes_from_state

    root: Path = ctx.obj["root"]
    session_path = find_session_dir(root, session_id)
    data = (
        load_outcomes_from_state(session_path / "outcomes.json")
        if session_path is not None
        else {"route_outcomes": [], "compact_outcomes": []}
    )
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
    runs_dir = root / "sessions"
    if not runs_dir.exists():
        click.echo(json.dumps([], indent=2))
        return

    combined: dict[str, list[dict[str, Any]]] = {
        "route_outcomes": [],
        "compact_outcomes": [],
    }
    for outcomes_file in runs_dir.glob("*/*/*/*/*/outcomes.json"):
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
        session_id = files[0].parent.name

    # Single-session view: cheap to also compute the transcript-based carry
    # component here (unlike the bulk /v1/sessions and insights aggregation
    # paths, which default this off -- see build_report's docstring).
    report = load_report(session_id, root, include_carry_credit=True)
    if report is None:
        click.echo(f"Session '{session_id}' not found in {root / 'sessions'}.", err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(render_json(report))
    else:
        click.echo(render_text(report, no_color=no_color))


# Claude Code launches subagents via "Agent" (formerly "Task").
_SUBAGENT_TOOL_NAMES = {"agent", "task"}


def _tool_call_total(trace: Trace) -> int:
    total = 0
    for call in trace.tools_called:
        total += int(call.count or 0)
    return total


def _subagent_total(trace: Trace) -> int:
    total = 0
    for call in trace.tools_called:
        if str(call.name or "").strip().lower() in _SUBAGENT_TOOL_NAMES:
            total += int(call.count or 0)
    return total


def _trace_cost_usd(trace: Trace) -> float:
    total = 0.0
    for entry in trace.usage_entries:
        total += float(entry.cost_usd or 0.0)
    return round(total, 6)


def _estimated_trace_cost_usd(trace: Trace) -> float:
    from atelier.core.capabilities.pricing import usage_cost_usd
    from atelier.core.capabilities.savings_summary import resolve_model_id

    # No cross-host fallback model: a session with no recorded model prices at
    # $0/unknown (get_model_pricing("") already returns a zero-cost sentinel)
    # rather than being fabricated at another host's rate card.
    estimated = 0.0
    if trace.model_usages:
        for usage in trace.model_usages:
            model = resolve_model_id(usage.model or trace.model)
            estimated += usage_cost_usd(
                model,
                input_tokens=int(usage.input_tokens or 0),
                output_tokens=int(usage.output_tokens or 0),
                cache_read_tokens=int(usage.cached_input_tokens or 0),
                cache_write_tokens=int(usage.cache_creation_input_tokens or 0),
                thinking_tokens=int(usage.thinking_tokens or 0),
            )
        return round(estimated, 6)

    model = resolve_model_id(trace.model)
    estimated = usage_cost_usd(
        model,
        input_tokens=int(trace.input_tokens or 0),
        output_tokens=int(trace.output_tokens or 0),
        cache_read_tokens=int(trace.cached_input_tokens or 0),
        cache_write_tokens=int(trace.cache_creation_input_tokens or 0),
        thinking_tokens=int(trace.thinking_tokens or 0),
    )
    return round(float(estimated), 6)


def _estimated_trace_cost_breakdown(trace: Trace) -> dict[str, float]:
    from atelier.core.capabilities.pricing import usage_cost_breakdown_usd
    from atelier.core.capabilities.savings_summary import resolve_model_id

    # See _estimated_trace_cost_usd: no cross-host fallback model.
    breakdown = {"input": 0.0, "cache_read": 0.0, "cache_write": 0.0, "output": 0.0}
    if trace.model_usages:
        for usage in trace.model_usages:
            model = resolve_model_id(usage.model or trace.model)
            part = usage_cost_breakdown_usd(
                model,
                input_tokens=int(usage.input_tokens or 0),
                output_tokens=int(usage.output_tokens or 0),
                cache_read_tokens=int(usage.cached_input_tokens or 0),
                cache_write_tokens=int(usage.cache_creation_input_tokens or 0),
                thinking_tokens=int(usage.thinking_tokens or 0),
            )
            breakdown["input"] += float(part.get("input") or 0.0)
            breakdown["cache_read"] += float(part.get("cache_read") or 0.0)
            breakdown["cache_write"] += float(part.get("cache_write") or 0.0)
            breakdown["output"] += float(part.get("output") or 0.0)
    else:
        model = resolve_model_id(trace.model)
        part = usage_cost_breakdown_usd(
            model,
            input_tokens=int(trace.input_tokens or 0),
            output_tokens=int(trace.output_tokens or 0),
            cache_read_tokens=int(trace.cached_input_tokens or 0),
            cache_write_tokens=int(trace.cache_creation_input_tokens or 0),
            thinking_tokens=int(trace.thinking_tokens or 0),
        )
        breakdown["input"] = float(part.get("input") or 0.0)
        breakdown["cache_read"] = float(part.get("cache_read") or 0.0)
        breakdown["cache_write"] = float(part.get("cache_write") or 0.0)
        breakdown["output"] = float(part.get("output") or 0.0)
    return {k: round(v, 6) for k, v in breakdown.items()}


def _best_trace_cost(trace: Trace) -> tuple[float, float, float]:
    reported = _trace_cost_usd(trace)
    estimated = _estimated_trace_cost_usd(trace)
    chosen = estimated if estimated > 0 else reported
    if chosen <= 0:
        chosen = reported
    return round(chosen, 6), reported, estimated


def _claude_subagent_count(session_id: str) -> int:
    if not session_id:
        return 0
    try:
        from atelier.core.capabilities.savings_summary import claude_transcript_candidates

        for candidate in claude_transcript_candidates(session_id):
            if candidate.stem != session_id:
                continue
            subagent_dir = candidate.parent / session_id / "subagents"
            if subagent_dir.is_dir():
                return len(list(subagent_dir.glob("*.jsonl")))
    except Exception:
        logging.exception("failed to count claude subagents for session=%s", session_id)
    return 0


def _claude_subagent_cost_usd(session_id: str) -> float:
    if not session_id:
        return 0.0
    try:
        from atelier.core.capabilities.savings_summary import claude_transcript_candidates, read_transcript_stats

        for candidate in claude_transcript_candidates(session_id):
            if candidate.stem != session_id:
                continue
            subagent_dir = candidate.parent / session_id / "subagents"
            if not subagent_dir.is_dir():
                continue
            total = 0.0
            for subagent_file in subagent_dir.glob("*.jsonl"):
                stats = read_transcript_stats(subagent_file)
                if stats is not None:
                    total += float(stats.est_cost_usd or 0.0)
            return round(total, 6)
    except Exception:
        logging.exception("failed to compute claude subagent cost for session=%s", session_id)
    return 0.0


def _subagent_cost_from_trace(trace: Trace) -> float:
    total = 0.0
    for entry in trace.usage_entries:
        source_type = str(entry.source_type or "").lower()
        source_id = str(entry.source_id or "").lower()
        tool_name = str(entry.tool_name or "").lower()
        if "subagent" in source_type or "subagent" in source_id or tool_name in _SUBAGENT_TOOL_NAMES:
            total += float(entry.cost_usd or 0.0)
    return round(total, 6)


def _artifact_subagent_count(store: ContextStore, trace: Trace) -> int:
    count = 0
    for artifact_id in trace.raw_artifact_ids:
        artifact = store.get_raw_artifact(artifact_id)
        if artifact is None:
            continue
        rel = str(artifact.relative_path or "").lower()
        if "subagent" in rel or "/subagents/" in rel or "\\subagents\\" in rel:
            count += 1
    return count


def _host_subagent_count(store: ContextStore, host_name: str, session_id: str, trace: Trace) -> int:
    count = _subagent_total(trace)
    count = max(count, _artifact_subagent_count(store, trace))
    if host_name == "claude" and session_id:
        count = max(count, _claude_subagent_count(session_id))
    return count


def _host_subagent_cost_usd(host_name: str, session_id: str, trace: Trace) -> float:
    heuristic = _subagent_cost_from_trace(trace)
    if host_name == "claude":
        return max(heuristic, _claude_subagent_cost_usd(session_id))
    return heuristic


def _claude_transcript_block(session_id: str) -> TranscriptSavingsBlock | None:
    """Savings recovered from the session's own transcript file.

    The stop hook embeds its summary (est. cost / savings / context carry) in
    the conversation, so the numbers live inside the host session file itself
    — the only source that exists when analyzing someone else's sessions.
    """
    if not session_id:
        return None
    try:
        from atelier.core.capabilities.savings_summary import (
            claude_transcript_candidates,
            read_transcript_savings_block,
        )

        for candidate in claude_transcript_candidates(session_id):
            if candidate.stem != session_id:
                continue
            return read_transcript_savings_block(candidate)
    except Exception:
        logging.exception("failed to read transcript savings for session=%s", session_id)
    return None


def _claude_live_savings_summary(
    session_id: str,
    root: Path,
) -> tuple[float, int, int, float, int, float, float]:
    """Authoritative Claude savings from the same source as statusline/Stop.

    Returns ``(saved_usd, saved_tokens, calls_avoided, carry_usd,
    carry_tokens, est_cost_usd, read_saved_usd)``. All values are zero when no
    local sidecar is available, so callers can fall back to the portable
    transcript block. ``read_saved_usd`` is an informational subcomponent
    already folded inside ``saved_usd`` (same convention as
    ``SavingsSummary.saved_usd``/``total_saved_usd``) -- never add it on top.
    """
    if not session_id:
        return 0.0, 0, 0, 0.0, 0, 0.0, 0.0
    try:
        from atelier.core.capabilities.savings_summary import (
            compute_savings_summary,
            read_session_end_carry,
        )

        summary = compute_savings_summary(session_id, atelier_root=root)
    except Exception:
        logging.exception("failed to read Claude savings summary for session=%s", session_id)
        return 0.0, 0, 0, 0.0, 0, 0.0, 0.0
    # Prefer the persisted last-Stop carry snapshot over a fresh live
    # recompute: it's the exact value the day-bucketed aggregate (and thus
    # the statusline's windowed ↓ figure) already folded in, frozen at the
    # moment Stop last fired. Re-deriving carry live can drift from that
    # frozen number if pricing data changes between the Stop fire and this
    # call (see read_session_end_carry docstring) -- reusing it here keeps
    # `session stats`/`session list` byte-identical with the statusline for
    # any session that has produced at least one Stop snapshot. Sessions
    # still active with no snapshot yet fall back to the live recompute.
    carry_usd = float(summary.carry_usd or 0.0)
    carry_tokens = int(summary.carry_tokens or 0)
    try:
        persisted_carry = read_session_end_carry(session_id, root)
    except Exception:
        logging.exception("failed to read persisted carry for session=%s", session_id)
        persisted_carry = None
    if persisted_carry is not None:
        carry_usd, carry_tokens = persisted_carry
    return (
        float(summary.saved_usd or 0.0),
        int(summary.ctx_saved or 0),
        int(summary.smart_calls or 0),
        carry_usd,
        carry_tokens,
        float(summary.est_cost_usd or 0.0),
        float(summary.read_saved_usd or 0.0),
    )


def _cache_read_rate(model: str, breakdown: dict[str, float], cache_read_tokens: int) -> float:
    """Per-token cache-read USD rate: model rate card first, observed fallback."""
    try:
        from atelier.core.capabilities.pricing import get_model_pricing
        from atelier.core.capabilities.savings_summary import resolve_model_id

        pricing = get_model_pricing(resolve_model_id(model))
        if pricing is not None and pricing.known and pricing.cache_read > 0:
            return float(pricing.cache_read) / 1_000_000
    except Exception:
        logging.exception("failed to resolve cache-read rate for model=%s", model)
    if cache_read_tokens > 0 and breakdown["cache_read"] > 0:
        return breakdown["cache_read"] / cache_read_tokens
    return 0.0


def _input_rate(model: str, breakdown: dict[str, float], input_tokens: int) -> float:
    """Per-token input USD rate: model rate card first, observed fallback."""
    try:
        from atelier.core.capabilities.pricing import get_model_pricing
        from atelier.core.capabilities.savings_summary import resolve_model_id

        pricing = get_model_pricing(resolve_model_id(model))
        if pricing is not None and pricing.known and pricing.input > 0:
            return float(pricing.input) / 1_000_000
    except Exception:
        logging.exception("failed to resolve input rate for model=%s", model)
    if input_tokens > 0 and breakdown["input"] > 0:
        return breakdown["input"] / input_tokens
    return 0.0


def _term_width() -> int:
    return shutil.get_terminal_size(fallback=(120, 24)).columns


def _wrap_csv_items(items: list[str], *, width: int | None = None) -> list[str]:
    max_width = (width or _term_width()) - 16  # 16 = label indent
    if not items:
        return ["(none)"]
    lines: list[str] = []
    current = ""
    for item in items:
        chunk = item if not current else f", {item}"
        if current and len(current) + len(chunk) > max_width:
            lines.append(current)
            current = item
        else:
            current += chunk
    if current:
        lines.append(current)
    return lines


def _fmt_tok_compact(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _emit_kv(label: str, value: str) -> None:
    click.echo(click.style(f"    {label:<11}", fg="cyan") + value)


def _emit_tree_rows(rows: list[tuple[str, str]]) -> None:
    """Emit a list of (label, value) pairs with ├─ / └─ connectors.

    An empty label signals a continuation line (tool-list wrap, etc.);
    those are indented under the previous connector.
    """
    for i, (label, value) in enumerate(rows):
        last = i == len(rows) - 1
        connector = "└─" if last else "├─"
        if label:
            click.echo(click.style(f"  {connector} {label:<10}", fg="cyan") + value)
        else:
            # continuation: align under the value column
            prefix = "   " if last else "  │"
            click.echo(f"{prefix}  {' ' * 10} {value}")


def _emit_tree_rows_rich(rows: list[tuple[str, str]], console: RichConsole) -> None:
    """Emit (label, value) pairs with ├─/└─ connectors via Rich console."""
    for i, (label, value) in enumerate(rows):
        last = i == len(rows) - 1
        connector = "└─" if last else "├─"
        if label:
            console.print(f"  [cyan]{connector} {label:<10}[/] {value}")
        else:
            prefix = "   " if last else "  │"
            console.print(f"{prefix}  {' ' * 10} {value}")


def _render_host_header_rich(host_name: str, imported_count: int) -> None:
    """Rich-styled host section header for session hosts.

    *imported_count* is the number of sessions actually imported for this
    host this run -- the same count the rows printed below and the footer
    totals reflect, so the three numbers agree.
    """
    from rich.console import Console

    console = Console(highlight=False)
    console.print()
    if imported_count > 0:
        console.rule(
            f"[bold bright_magenta]{host_name}[/]  [dim]imported this run: {imported_count}[/]",
            style="dim",
        )
    else:
        console.rule(f"[bold bright_magenta]{host_name}[/]", style="dim")


def _render_hosts_footer_rich(rows: list[dict[str, Any]], since_label: str) -> None:
    """Rich summary panels footer for session hosts."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    if not rows:
        return

    console = Console(highlight=False)

    n = len(rows)
    n_atelier = sum(1 for r in rows if int(r["atelier_calls"]) > 0)
    total_cost = sum(float(r["cost_usd"]) for r in rows)
    total_saved = sum(float(r["saved_usd"]) for r in rows)
    total_carry = sum(float(r["carry_usd"]) for r in rows)
    total_in = sum(int(r["input_tokens"]) for r in rows)
    total_cr = sum(int(r["cache_read_tokens"]) for r in rows)
    total_cw = sum(int(r["cache_write_tokens"]) for r in rows)
    total_out = sum(int(r["output_tokens"]) for r in rows)
    total_calls = sum(int(r["tool_calls"]) for r in rows)
    total_atelier = sum(int(r["atelier_calls"]) for r in rows)
    total_subagents = sum(int(r["subagents"]) for r in rows)
    total_sub_cost = sum(float(r["subagent_cost_usd"]) for r in rows)
    total_saved_tokens = sum(int(r["saved_tokens"]) for r in rows)
    total_calls_avoided = sum(int(r["calls_avoided"]) for r in rows)
    total_pot_usd = sum(float(r["potential_saved_usd"]) for r in rows)
    total_pot_carry = sum(float(r["potential_carry_usd"]) for r in rows)

    console.print()
    console.rule("[dim]Summary[/]")
    console.print()

    atelier_p = 100 * total_atelier // total_calls if total_calls > 0 else 0
    baseline = total_cost + total_saved + total_carry

    usage_lines = [
        f"  [dim]Cost          [/]  [bright_red]${total_cost:,.4f}[/]",
        f"  [dim]Sessions      [/]  [bright_white]{n}[/]  [dim]({n_atelier} w/ Atelier)[/]",
        f"  [dim]Calls         [/]  [bright_yellow]{total_calls:,}[/]  [dim]({atelier_p}% atelier)[/]",
        f"  [dim]Tokens in     [/]  [white]{_fmt_tok_compact(total_in)}[/]",
        f"  [dim]Cache read    [/]  [bright_cyan]{_fmt_tok_compact(total_cr)}[/]",
        f"  [dim]Cache write   [/]  [bright_blue]{_fmt_tok_compact(total_cw)}[/]",
        f"  [dim]Output        [/]  [white]{_fmt_tok_compact(total_out)}[/]",
    ]
    if total_subagents > 0:
        sub_pct = 100 * total_sub_cost / total_cost if total_cost > 0 else 0.0
        usage_lines.append(
            f"  [dim]Subagents     [/]  [white]{total_subagents}[/]  [dim]≈${total_sub_cost:,.4f}  ({sub_pct:.1f}%)[/]"
        )

    # Headline "Saved" is the canonical total (saved_usd + carry_usd, same
    # sum as SavingsSummary.total_saved_usd / WindowSavings.total_saved_usd)
    # so it reconciles with the statusline's single "↓ $X" figure; carry is
    # still broken out as a sub-line for detail.
    savings_lines = [
        f"  [dim]Saved         [/]  [bright_green]${total_saved + total_carry:,.4f}[/]",
        f"  [dim]  of which carry[/]  [magenta]${total_carry:,.4f}[/]",
    ]
    if total_saved_tokens > 0:
        savings_lines.append(f"  [dim]Tok saved     [/]  [bright_green]{_fmt_tok_compact(total_saved_tokens)}[/]")
    if total_calls_avoided > 0:
        savings_lines.append(f"  [dim]Calls avoided [/]  [bright_green]{total_calls_avoided}[/]")
    if total_saved + total_carry > 0:
        pct = 100 * (total_saved + total_carry) / baseline
        savings_lines.append(f"  [dim]Reduction     [/]  [bright_green]-{pct:.1f}%[/]")
        savings_lines.append(f"  [dim]Baseline      [/]  [dim]≈${baseline:,.4f}[/]")
    if total_pot_usd > 0 or total_pot_carry > 0:
        savings_lines.append("")
        savings_lines.append(f"  [dim]Potential     [/]  [yellow]≈${total_pot_usd:,.4f}[/]")
        savings_lines.append(f"  [dim]Carry avail.  [/]  [yellow]≈${total_pot_carry:,.4f}[/]")

    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(
        Panel("\n".join(usage_lines), title="[bold]Usage[/]", border_style="dim", padding=(1, 2)),
        Panel(
            "\n".join(savings_lines),
            title="[bold bright_green]Atelier Savings[/]",
            border_style="bright_green dim",
            padding=(1, 2),
        ),
    )
    console.print(grid)
    console.print()


def _render_stats_rich(
    rows: list[dict[str, Any]],
    since_label: str,
    top: int,
    show_header: bool = True,
) -> None:
    """Full Rich redesign for session stats display."""
    from rich import box as rbox
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console(highlight=False)

    if not rows:
        console.print("[dim]No sessions found.[/]")
        return

    n = len(rows)
    n_atelier = sum(1 for r in rows if int(r["atelier_calls"]) > 0)
    total_cost = sum(float(r["cost_usd"]) for r in rows)
    total_saved = sum(float(r["saved_usd"]) for r in rows)
    total_carry = sum(float(r["carry_usd"]) for r in rows)
    total_in = sum(int(r["input_tokens"]) for r in rows)
    total_cr = sum(int(r["cache_read_tokens"]) for r in rows)
    total_cw = sum(int(r["cache_write_tokens"]) for r in rows)
    total_out = sum(int(r["output_tokens"]) for r in rows)
    total_calls = sum(int(r["tool_calls"]) for r in rows)
    total_atelier = sum(int(r["atelier_calls"]) for r in rows)
    total_subagents = sum(int(r["subagents"]) for r in rows)
    total_sub_cost = sum(float(r["subagent_cost_usd"]) for r in rows)
    total_pot_usd = sum(float(r["potential_saved_usd"]) for r in rows)
    total_pot_carry = sum(float(r["potential_carry_usd"]) for r in rows)

    host_agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        hn = str(r.get("host") or r.get("source") or "unknown")
        if hn not in host_agg:
            host_agg[hn] = {
                "sessions": 0,
                "cost": 0.0,
                "saved": 0.0,
                "carry": 0.0,
                "calls": 0,
                "atelier": 0,
                "pot_saved": 0.0,
                "pot_carry": 0.0,
            }
        ha = host_agg[hn]
        ha["sessions"] += 1
        ha["cost"] += float(r["cost_usd"])
        ha["saved"] += float(r["saved_usd"])
        ha["carry"] += float(r["carry_usd"])
        ha["calls"] += int(r["tool_calls"])
        ha["atelier"] += int(r["atelier_calls"])
        ha["pot_saved"] += float(r["potential_saved_usd"])
        ha["pot_carry"] += float(r["potential_carry_usd"])

    hosts_sorted = sorted(host_agg.items(), key=lambda x: -x[1]["cost"])

    # ── Header ──
    if show_header:
        hosts_label = ", ".join(h for h, _ in hosts_sorted)
        console.print()
        console.rule(f"[bold bright_white]Last {since_label}[/]  [dim]·  {n} sessions  ·  {hosts_label}[/]")
        if n_atelier > 0:
            console.print(f"  [dim]{n_atelier} of {n} sessions used Atelier tools[/]")
        console.print()

    # ── Hero chips ──
    def _chip(label: str, value: str, color: str) -> Panel:
        return Panel(
            f"[bold {color}]{value}[/]\n[dim]{label}[/]",
            border_style="dim",
            padding=(0, 2),
        )

    hero = Table.grid(expand=True)
    for _ in range(5):
        hero.add_column(justify="center")

    atelier_pct_t = 100 * total_atelier // total_calls if total_calls > 0 else 0
    hero = Table.grid(expand=True)
    for _ in range(4):
        hero.add_column(justify="center")

    savings_total = total_saved + total_carry
    hero.add_row(
        _chip("Cost", f"${total_cost:,.0f}", "bright_red"),
        _chip("Sessions", f"{n}", "bright_white"),
        _chip("Calls", f"{total_calls:,}", "bright_yellow"),
        _chip("Savings", f"${savings_total:,.0f}", "bright_green"),
    )
    console.print(hero)
    console.print()

    # ── Token usage ──
    console.print("[bold bright_white]  Tokens[/]  [dim]across all sessions[/]")
    console.print()
    tok_table = Table(box=rbox.SIMPLE, show_header=True, header_style="dim", padding=(0, 2))
    tok_table.add_column("Input", justify="right")
    tok_table.add_column("Cache Read", justify="right")
    tok_table.add_column("Cache Write", justify="right")
    tok_table.add_column("Output", justify="right")
    tok_table.add_row(
        f"[bright_white]{_fmt_tok_compact(total_in)}[/]",
        f"[bright_cyan]{_fmt_tok_compact(total_cr)}[/]",
        f"[bright_blue]{_fmt_tok_compact(total_cw)}[/]",
        f"[white]{_fmt_tok_compact(total_out)}[/]",
    )
    console.print(tok_table)

    # ── By host ──
    if len(host_agg) > 1:
        console.print("[bold bright_white]  By Host[/]")
        console.print()
        host_table = Table(box=rbox.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
        host_table.add_column("Host", style="bold bright_magenta", min_width=10)
        host_table.add_column("Cost", justify="right", min_width=10)
        host_table.add_column("N", justify="right", style="dim", min_width=3)
        host_table.add_column("Calls", justify="right", min_width=6)
        host_table.add_column("Atel%", justify="right", min_width=5)
        host_table.add_column("Savings", justify="right", min_width=12)
        host_table.add_column("Opportunity", justify="right", min_width=10)
        for hn, ha in hosts_sorted:
            if ha["sessions"] == 0:
                continue
            a_pct = 100 * ha["atelier"] // ha["calls"] if ha["calls"] > 0 else 0
            savings_total = ha["saved"] + ha["carry"]
            savings_s = f"[bright_green]${savings_total:,.2f}[/]" if savings_total > 0 else "[dim]—[/]"
            pot_t = ha["pot_saved"] + ha["pot_carry"]
            pot_s = f"[yellow]${pot_t:,.2f}[/]" if pot_t > 0 else "[dim]—[/]"
            host_table.add_row(
                hn,
                f"[bright_red]${ha['cost']:,.2f}[/]",
                str(ha["sessions"]),
                f"{ha['calls']:,}",
                f"[cyan]{a_pct}%[/]",
                savings_s,
                pot_s,
            )
        console.print(host_table)

    # ── Top sessions ──
    if top > 0:
        sorted_rows = sorted(rows, key=lambda r: -float(r["cost_usd"]))
        top_rows = [r for r in sorted_rows if float(r["cost_usd"]) > 0][:top]
        if top_rows:
            console.print(f"[bold bright_white]  Top {len(top_rows)} Sessions[/]  [dim]by cost[/]")
            console.print()
            sess_table = Table(box=rbox.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
            sess_table.add_column("Date", style="dim", min_width=10, no_wrap=True)
            sess_table.add_column("Host", min_width=9, no_wrap=True)
            sess_table.add_column("Model", min_width=14, style="dim", no_wrap=True)
            sess_table.add_column("Cost", justify="right", min_width=10, no_wrap=True)
            sess_table.add_column("Prompt", no_wrap=True)
            for r in top_rows:
                date = str(r["created_at"])[:10] if r["created_at"] else "-"
                model_short = str(r["model"] or "-")[:16]
                hn_r = str(r.get("host") or "")
                prompt = str(r["first_user"] or "").replace("\n", " ").strip()[:55]
                cost_f = float(r["cost_usd"])
                sess_table.add_row(
                    date,
                    f"[bright_magenta]{hn_r}[/]",
                    model_short,
                    f"[bright_red]${cost_f:,.2f}[/]",
                    f"[dim]{prompt}[/]",
                )
            console.print(sess_table)

    # ── Bottom panels ──
    console.print()
    bottom = Table.grid(expand=True, padding=(0, 1))
    bottom.add_column(ratio=1)
    bottom.add_column(ratio=1)

    if total_saved + total_carry > 0:
        baseline = total_cost + total_saved + total_carry
        pct = 100 * (total_saved + total_carry) / baseline
        # "Saved" is the canonical total (saved_usd + carry_usd) so it matches
        # the statusline's single "↓ $X" headline; carry stays as a sub-line.
        cost_lines = [
            f"  [dim]Total cost    [/]  [bright_red]${total_cost:,.2f}[/]",
            f"  [dim]Saved         [/]  [bright_green]${total_saved + total_carry:,.2f}[/]",
            f"  [dim]  of which carry[/]  [magenta]${total_carry:,.2f}[/]",
            f"  [dim]Baseline      [/]  [dim]≈${baseline:,.2f}[/]",
            f"  [dim]Reduction     [/]  [bright_green]-{pct:.1f}%[/]",
        ]
    else:
        cost_lines = [f"  [dim]Total cost    [/]  [bright_red]${total_cost:,.2f}[/]"]

    if total_subagents > 0:
        sub_pct = 100 * total_sub_cost / total_cost if total_cost > 0 else 0.0
        cost_lines.append(f"  [dim]Subagents     [/]  [white]{total_subagents}[/]")
        cost_lines.append(f"  [dim]  ≈cost        [/]  [dim]${total_sub_cost:,.2f}  ({sub_pct:.1f}%)[/]")

    if total_pot_usd > 0 or total_pot_carry > 0:
        atelier_lines = [
            f"  [dim]Potential     [/]  [yellow]≈${total_pot_usd:,.2f}[/]",
            f"  [dim]Carry avail.  [/]  [yellow]≈${total_pot_carry:,.2f}[/]",
            f"  [dim]Opportunity   [/]  [bright_yellow]≈${total_pot_usd + total_pot_carry:,.2f}[/]",
            "",
            f"  [dim]Atelier calls [/]  [cyan]{total_atelier:,}[/]",
            f"  [dim]  of total     [/]  [dim]{total_calls:,}  ({atelier_pct_t}%)[/]",
        ]
        atelier_title = "[bold yellow]Atelier Potential[/]"
        atelier_border = "yellow dim"
    else:
        atelier_lines = [
            f"  [dim]Saved         [/]  [bright_green]${total_saved + total_carry:,.2f}[/]",
            f"  [dim]  of which carry[/]  [magenta]${total_carry:,.2f}[/]",
            f"  [dim]Atelier calls [/]  [cyan]{total_atelier:,}[/]  [dim]({atelier_pct_t}%)[/]",
            f"  [dim]Atelier sess. [/]  [bright_cyan]{n_atelier}[/]  [dim]of {n}[/]",
        ]
        atelier_title = "[bold bright_green]Atelier Savings[/]"
        atelier_border = "bright_green dim"

    bottom.add_row(
        Panel("\n".join(cost_lines), title="[bold]Cost Breakdown[/]", border_style="dim", padding=(1, 2)),
        Panel(
            "\n".join(atelier_lines),
            title=atelier_title,
            border_style=atelier_border,
            padding=(1, 2),
        ),
    )
    console.print(bottom)
    console.print()


def _tool_name_parts(name: str) -> list[str]:
    return [part for part in re.split(r"__|::|\.", (name or "").strip().lower()) if part]


def _is_atelier_tool_name(name: str) -> bool:
    lowered = (name or "").strip().lower()
    return lowered.startswith("atelier_") or any("atelier" in part for part in _tool_name_parts(name))


# Builtin tools with a direct Atelier equivalent (read/search, shell, edit
# families).  Only these count toward routable volume; bookkeeping tools
# (todo lists, plan updates, ask-user, task spawns) are never routed.
_ROUTABLE_BUILTIN = frozenset(
    (
        # read/search family
        "read",
        "view",
        "grep",
        "rg",
        "glob",
        "search",
        "explore",
        "symbols",
        "read_file",
        "readfile",
        "grep_search",
        "list_directory",
        "list_dir",
        "codebase_search",
        # shell family
        "bash",
        "shell",
        "exec",
        "exec_command",
        "run_shell_command",
        "run_terminal_cmd",
        # edit family
        "edit",
        "apply_patch",
        "applypatch",
        "patch",
        "replace",
        "write",
        "write_file",
        "str_replace",
    )
)

# Fleet-measured Atelier savings rate, shipped with the binary so estimates
# work on machines with no local Atelier history.  Measured 2026-06 across
# 20 Claude sessions / 1,554 routed calls (stop-hook ground truth): weighted
# average 3,265 output tokens saved per routed call (focused reads + store
# dedup vs raw builtin output), stable across fable-5 / sonnet-4-6 / opus-4-8.
_FLEET_SAVED_TOKENS_PER_CALL = 3265


def _base_tool_name(name: str) -> str:
    """Normalize a host tool name to its base form.

    Any Atelier-qualified name collapses to the trailing tool segment, so
    atelier.bash, atelier::bash, atelier_bash, and mcp__...atelier...__bash
    all account as bash.
    """
    base = (name or "").strip().lower()
    if base.startswith("atelier_"):
        return base[len("atelier_") :]
    parts = _tool_name_parts(base)
    if any("atelier" in part for part in parts) and len(parts) > 1:
        return parts[-1]
    base = base.split(":")[-1]
    if "__" in base:
        base = base.split("__")[-1]
    return base


def _builtin_potential(
    trace: Trace,
) -> dict[str, Any]:
    """Classify a session's tool calls for savings estimation.

    Returns builtin/atelier call counts plus ``routable_builtin``: the
    builtin calls with a direct Atelier equivalent (read/search, shell,
    edit).  These are the calls the potential-savings estimate applies to;
    bookkeeping tools (todos, plan updates, ask-user) are excluded.
    """
    atelier_calls = 0
    builtin_calls = 0
    routable_builtin = 0
    for tool in trace.tools_called:
        name = str(tool.name or "").strip()
        count = int(tool.count or 0)
        if count <= 0:
            continue
        if _is_atelier_tool_name(name):
            atelier_calls += count
        else:
            builtin_calls += count
            if _base_tool_name(name) in _ROUTABLE_BUILTIN:
                routable_builtin += count

    return {
        "builtin_calls": builtin_calls,
        "atelier_calls": atelier_calls,
        "routable_builtin": routable_builtin,
    }


def _trace_model(trace: Trace) -> str:
    if trace.model:
        return trace.model
    if trace.model_usages:
        first = trace.model_usages[0]
        if first.model:
            return first.model
    return "-"


def _build_session_row(trace: Trace, store: ContextStore, host_name: str, root: Path) -> dict[str, Any]:
    """Build a display row dict from a single imported trace."""
    sid = (trace.session_id or trace.id or "").strip()
    input_tokens = int(trace.input_tokens or 0)
    cache_read_tokens = int(trace.cached_input_tokens or 0)
    cache_write_tokens = int(trace.cache_creation_input_tokens or 0)
    output_tokens = int(trace.output_tokens or 0)
    total_cost_usd, reported_cost_usd, estimated_cost_usd = _best_trace_cost(trace)
    model = _trace_model(trace)
    pricing_model = model if model != "-" else ""  # "-" is a display sentinel; don't warn on it
    breakdown = _estimated_trace_cost_breakdown(trace)
    subagents = _host_subagent_count(store, host_name, sid, trace)
    subagent_cost_usd = _host_subagent_cost_usd(host_name, sid, trace)
    potential = _builtin_potential(trace)
    saved_usd = 0.0
    carry_usd = 0.0
    carry_tokens = 0
    saved_tokens = 0
    calls_avoided = 0
    block_tool_calls = 0
    read_saved_usd = 0.0
    if host_name == "claude":
        block = _claude_transcript_block(sid)
        if block is not None:
            saved_usd = float(block.saved_usd)
            saved_tokens = int(block.saved_tokens)
            calls_avoided = int(block.calls_avoided)
            carry_usd = float(block.carry_usd)
            carry_tokens = int(block.carry_tokens)
            block_tool_calls = int(block.tool_calls)
            # The block freezes at the session's last clean Stop event, so a
            # resumed/interrupted session can under-report cost there even
            # though the full-transcript estimate keeps counting past it —
            # take whichever is larger (mirrors the savings max() below)
            # instead of always trusting the block.
            if block.est_cost_usd > total_cost_usd:
                total_cost_usd = block.est_cost_usd
                estimated_cost_usd = block.est_cost_usd
                bucket_sum = sum(breakdown.values())
                if bucket_sum > 0:
                    ratio = block.est_cost_usd / bucket_sum
                    breakdown = {k: v * ratio for k, v in breakdown.items()}
        # The stop-hook block freezes at the session's last clean Stop event.
        # Prefer the live summary when present, and take saved+carry together so
        # stats do not mix two different calculation epochs.
        (
            live_saved_usd,
            live_saved_tokens,
            live_calls,
            live_carry_usd,
            live_carry_tokens,
            live_cost,
            live_read_saved_usd,
        ) = _claude_live_savings_summary(sid, root)
        if live_saved_tokens > 0 or live_calls > 0 or live_carry_usd > 0:
            saved_usd = live_saved_usd
            saved_tokens = live_saved_tokens
            calls_avoided = live_calls
            carry_usd = live_carry_usd
            carry_tokens = live_carry_tokens
            read_saved_usd = live_read_saved_usd
            if live_cost > total_cost_usd:
                total_cost_usd = live_cost
                estimated_cost_usd = live_cost
                bucket_sum = sum(breakdown.values())
                if bucket_sum > 0:
                    ratio = live_cost / bucket_sum
                    breakdown = {k: v * ratio for k, v in breakdown.items()}
    elif int(potential["atelier_calls"]) > 0:
        # Estimate actual savings for non-Claude hosts that routed work through
        # Atelier: fleet rate (tokens saved per routed call) x routed calls.
        # API turns ~= tool calls (each call is one round trip); usage_entries
        # is the fallback for hosts that record one entry per assistant turn.
        turns = max(len(trace.usage_entries), _tool_call_total(trace))
        saved_tokens = int(potential["atelier_calls"]) * _FLEET_SAVED_TOKENS_PER_CALL
        # Carry: every saved token also avoids one cache re-read per later
        # turn; a call made mid-session has ~turns/2 turns after it.
        carry_tokens = saved_tokens * max(0, turns // 2)
    cr_rate = _cache_read_rate(pricing_model, breakdown, cache_read_tokens)
    in_rate = _input_rate(pricing_model, breakdown, input_tokens)
    atelier_calls = int(potential["atelier_calls"])
    builtin_calls = int(potential["builtin_calls"])
    total_calls = atelier_calls + builtin_calls
    atelier_share = atelier_calls / max(1, total_calls)
    # Channel cap bases: saved tokens would have been fed once (input/cache-
    # write spend); carry tokens are re-reads (cache-read spend).
    feed_cost = breakdown["input"] + breakdown["cache_write"]
    reread_cost = breakdown["cache_read"]
    if host_name != "claude" and saved_tokens > 0:
        saved_usd = saved_tokens * in_rate
        carry_usd = carry_tokens * cr_rate
        # Channel caps on the atelier share of observed spend.  Carry allows
        # 2x the call share: measured Claude sessions show avoided carry can
        # match the full cache-read spend at ~50% routing.
        saved_cap = feed_cost * atelier_share
        carry_cap = reread_cost * min(1.0, atelier_share * 2)
        if saved_usd > saved_cap:
            scale = saved_cap / saved_usd if saved_usd > 0 else 0.0
            saved_usd = saved_cap
            saved_tokens = int(saved_tokens * scale)
        if carry_usd > carry_cap:
            scale = carry_cap / carry_usd if carry_usd > 0 else 0.0
            carry_usd = carry_cap
            carry_tokens = int(carry_tokens * scale)

    # --- potential (builtin calls that could have routed through Atelier) ---
    routable_builtin = int(potential["routable_builtin"])
    potential_saved_usd = 0.0
    potential_carry_usd = 0.0
    potential_tokens_saved = 0
    potential_carry_tokens = 0
    if routable_builtin > 0:
        if host_name == "claude" and (saved_usd + carry_usd) > 0 and atelier_calls > 0:
            # Rate from measured direct savings only (compact reads / shell
            # output / dedup).  Carry is excluded because it's a session-level
            # property (context compression reduces cache re-reads across
            # turns) that doesn't transfer to individual "what-if" calls.
            rate = saved_usd / atelier_calls
            potential_saved_usd = rate * routable_builtin
            potential_tokens_saved = int(potential_saved_usd / in_rate) if in_rate > 0 else 0
        else:
            # No local ground truth: use the fleet rate shipped with the
            # binary, priced at this session's model rates.  Carry applies
            # here because the routable calls are in the same session (no
            # sub-agent split), so Atelier's context savings would compound
            # across the session's own turns.
            turns = max(len(trace.usage_entries), _tool_call_total(trace))
            potential_tokens_saved = routable_builtin * _FLEET_SAVED_TOKENS_PER_CALL
            potential_carry_tokens = potential_tokens_saved * max(0, turns // 2)
            potential_saved_usd = potential_tokens_saved * in_rate
            potential_carry_usd = potential_carry_tokens * cr_rate
            # Hard spend cap: turns//2 grows carry quadratically with session
            # length (more tool calls -> more routable_builtin AND more
            # turns), with nothing tying it back to reality -- a long session
            # could "opportunity" itself into claiming more carry than the
            # session could ever have spent. Carry can never legitimately
            # exceed what the session actually spent re-reading cache
            # (reread_cost) -- you cannot avoid re-reading more than you paid
            # to re-read; likewise the one-time "saved" (not-fed) tokens
            # can't exceed what was actually fed (feed_cost). This is the
            # same invariant compute_savings_summary enforces via
            # context-window residency, expressed against the session's own
            # observed spend since the fleet-rate fallback has no per-turn
            # residency data to work from.
            if potential_carry_usd > reread_cost:
                scale = reread_cost / potential_carry_usd if potential_carry_usd > 0 else 0.0
                potential_carry_usd = reread_cost
                potential_carry_tokens = int(potential_carry_tokens * scale)
            if potential_saved_usd > feed_cost:
                scale = feed_cost / potential_saved_usd if potential_saved_usd > 0 else 0.0
                potential_saved_usd = feed_cost
                potential_tokens_saved = int(potential_tokens_saved * scale)
    savings_estimated = host_name != "claude" and (saved_usd > 0 or carry_usd > 0)
    return {
        "host": host_name,
        "session_id": sid,
        "trace_id": trace.id,
        "created_at": trace.created_at.isoformat() if trace.created_at else "",
        "task": trace.task,
        "model": model,
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(total_cost_usd, 6),
        "reported_cost_usd": round(reported_cost_usd, 6),
        "estimated_cost_usd": round(estimated_cost_usd, 6),
        "cost_input_usd": round(breakdown["input"], 6),
        "cost_cache_read_usd": round(breakdown["cache_read"], 6),
        "cost_cache_write_usd": round(breakdown["cache_write"], 6),
        "cost_output_usd": round(breakdown["output"], 6),
        "saved_usd": round(saved_usd, 6),
        "saved_tokens": int(saved_tokens),
        "calls_avoided": int(calls_avoided),
        "carry_usd": round(carry_usd, 6),
        "carry_tokens": int(carry_tokens),
        # Informational subcomponent already folded inside saved_usd (see
        # _claude_live_savings_summary) -- claude-host rows only; other hosts
        # have no read-savings estimate path.
        "read_saved_usd": round(read_saved_usd, 6),
        "savings_estimated": savings_estimated,
        "tool_calls": _tool_call_total(trace),
        "subagents": subagents,
        "subagent_cost_usd": round(subagent_cost_usd, 6),
        "builtin_calls": int(potential["builtin_calls"]),
        "atelier_calls": int(potential["atelier_calls"]),
        "potential_calls_saved": routable_builtin,
        "potential_tokens_saved": potential_tokens_saved,
        "potential_saved_usd": round(potential_saved_usd, 6),
        "potential_carry_tokens": potential_carry_tokens,
        "potential_carry_usd": round(potential_carry_usd, 6),
        "block_tool_calls": block_tool_calls,
        "first_user": str(trace.task or "").strip(),
        "commands": [
            c if isinstance(c, str) else str(c.command)
            for c in trace.commands_run
            if isinstance(c, str) or hasattr(c, "command")
        ],
        "tools": [{"name": t.name, "count": int(t.count or 0)} for t in trace.tools_called],
        "subagent_names": dict(trace.telemetry.get("subagent_names", {})) if trace.telemetry else {},
        "source": "host_sessions",
    }


def _print_session_row(row: dict[str, Any], verbose: bool) -> None:
    """Print a single session row using Rich markup and tree-style connectors."""
    from rich.console import Console
    from rich.markup import escape as _re

    from atelier.core.capabilities.savings_summary import _fmt_pct, _fmt_tok, _fmt_usd

    console = Console(highlight=False)
    created = str(row["created_at"])[:19].replace("T", " ") if row["created_at"] else "-"
    sid = str(row["session_id"]) if row["session_id"] else "-"
    model = str(row["model"] or "-")[:32]
    console.print(f"\n  [bold bright_white]{created}[/]  [dim]{sid}[/]  [bold bright_yellow]{model}[/]")

    detail: list[tuple[str, str]] = []

    # tokens
    detail.append(
        (
            "tokens",
            f"[white]in={_fmt_tok(int(row['input_tokens']))}[/]"
            f"  [bright_cyan]cR={_fmt_tok(int(row['cache_read_tokens']))}[/]"
            f"  [bright_blue]cW={_fmt_tok(int(row['cache_write_tokens']))}[/]"
            f"  [white]out={_fmt_tok(int(row['output_tokens']))}[/]",
        )
    )

    # cost
    detail.append(
        (
            "cost",
            f"[bright_red]{_fmt_usd(float(row['cost_usd']))}[/]  "
            f"[dim](in {_fmt_usd(float(row['cost_input_usd']))} · cR {_fmt_usd(float(row['cost_cache_read_usd']))}"
            f" · cW {_fmt_usd(float(row['cost_cache_write_usd']))} · out {_fmt_usd(float(row['cost_output_usd']))})[/]",
        )
    )

    # Flag a large estimated-vs-host-reported cost gap regardless of source:
    # "trace_fallback" was never a value _build_session_row produces (always
    # "host_sessions"), which made this check unreachable dead code.
    est = float(row["estimated_cost_usd"])
    rep = float(row["reported_cost_usd"])
    if est > 0 and rep > 0 and abs(est - rep) / max(est, rep) > 0.25:
        detail.append(("cost-check", f"[yellow]estimated {_fmt_usd(est)} vs host-reported {_fmt_usd(rep)}[/]"))

    # subagents
    if int(row["subagents"]) > 0:
        sub_cost = float(row["subagent_cost_usd"])
        cost_detail = f" · [dim]≈{_fmt_usd(sub_cost)} (included in cost)[/]" if sub_cost > 0 else ""
        subagent_names: dict[str, int] = row.get("subagent_names") or {}
        if subagent_names:
            name_parts = [f"{n}x{c}" for n, c in sorted(subagent_names.items(), key=lambda x: -x[1])]
            wrapped_sub = _wrap_csv_items(name_parts)
            detail.append(("subagents", f"[dim]{_re(wrapped_sub[0])}[/]{cost_detail}"))
            for extra_line in wrapped_sub[1:]:
                detail.append(("", f"[dim]{_re(extra_line)}[/]"))
        else:
            detail.append(("subagents", f"[dim]{int(row['subagents'])}[/]{cost_detail}"))

    # savings
    saved = float(row["saved_usd"])
    carry = float(row["carry_usd"])
    read_saved = float(row.get("read_saved_usd") or 0.0)
    row_cost = float(row["cost_usd"])
    savings_parts: list[str] = []
    if saved > 0 or int(row["saved_tokens"]) > 0 or int(row["calls_avoided"]) > 0:
        sp = [f"[bright_green]{_fmt_usd(saved)}[/]"]
        if int(row["saved_tokens"]) > 0:
            sp.append(f"[bright_green]{_fmt_tok(int(row['saved_tokens']))} tok saved[/]")
        if int(row["calls_avoided"]) > 0:
            sp.append(f"[bright_green]{int(row['calls_avoided'])} calls avoided[/]")
        savings_parts.append(" · ".join(sp))
    # read_saved_usd is an informational subcomponent already folded inside
    # `saved` above (see _claude_live_savings_summary) -- shown, never summed.
    if read_saved > 0:
        savings_parts.append(f"[cyan]read {_fmt_usd(read_saved)}[/]")
    if carry > 0:
        savings_parts.append(f"[magenta]carry {_fmt_usd(carry)} · {_fmt_tok(int(row['carry_tokens']))} tok[/]")
    if row_cost > 0 and (saved + carry) > 0:
        baseline = row_cost + saved + carry
        savings_parts.append(f"[dim]baseline ≈{_fmt_usd(baseline)} (-{_fmt_pct(100 * (saved + carry) / baseline)})[/]")
    if savings_parts:
        detail.append(("savings", "  ·  ".join(savings_parts)))

    # calls
    detail.append(
        (
            "calls",
            f"[white]{int(row['tool_calls'])} total[/] · [cyan]{int(row['atelier_calls'])} atelier[/]"
            f" · [dim]{int(row['builtin_calls'])} builtin[/]",
        )
    )

    trace_calls = int(row["tool_calls"])
    block_calls = int(row.get("block_tool_calls") or 0)
    if block_calls > 0 and trace_calls > 0 and trace_calls / block_calls < 0.5:
        detail.append(
            (
                "calls-check",
                f"[red]trace import counted {trace_calls} but session file recorded {block_calls}"
                f" — trace parser may have missed some calls[/]",
            )
        )

    # potential
    if float(row["potential_saved_usd"]) > 0 or float(row["potential_carry_usd"]) > 0:
        pot = (
            f"[yellow]saved {_fmt_usd(float(row['potential_saved_usd']))}"
            f" ({_fmt_tok(int(row['potential_tokens_saved']))} tok)[/]"
        )
        if float(row["potential_carry_usd"]) > 0:
            pot += (
                f" + [magenta]carry {_fmt_usd(float(row['potential_carry_usd']))}"
                f" ({_fmt_tok(int(row['potential_carry_tokens']))} tok)[/]"
            )
        detail.append(("potential", pot + "[dim]  via Atelier[/]"))

    # tools (may wrap)
    tool_items = [f"{t['name']}x{t['count']}" for t in (row["tools"] or [])]
    wrapped_tools = _wrap_csv_items(tool_items)
    detail.append(("tools", f"[dim]{_re(wrapped_tools[0])}[/]"))
    for extra_line in wrapped_tools[1:]:
        detail.append(("", f"[dim]{_re(extra_line)}[/]"))

    # prompt
    first_user = str(row["first_user"] or "").replace("\n", " ").strip()
    max_prompt = max(40, _term_width() - 16)
    if len(first_user) > max_prompt:
        first_user = first_user[: max_prompt - 3] + "..."
    detail.append(("prompt", f"[dim]{_re(first_user) or '(none)'}[/]"))

    if verbose:
        for cmd in (row["commands"] or [])[:8]:
            detail.append(("cmd", f"[dim]{_re(str(cmd))}[/]"))

    _emit_tree_rows_rich(detail, console)


def _path_mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def _pick_live_sessions(
    items: list[Any],
    *,
    path_of: Callable[[Any], Path],
    limit: int,
    scan: int,
    session_filter: str = "",
    cutoff: datetime | None = None,
) -> list[Any]:
    """Newest-first lazy selection of session files for a live scan.

    Sorts candidates by file mtime (newest first), applies cheap pre-import
    filters, and returns a bounded candidate set. When filters are active, keep
    scanning up to ``scan`` so post-import trace filtering can still backfill
    rows after stale or non-matching candidates are rejected.
    """
    if limit <= 0 or scan <= 0:
        return []
    newest = sorted(items, key=lambda item: _path_mtime(path_of(item)), reverse=True)[:scan]
    picked: list[Any] = []
    target = scan if cutoff is not None or session_filter else limit
    for item in newest:
        p = path_of(item)
        if cutoff is not None and datetime.fromtimestamp(_path_mtime(p), tz=UTC) < cutoff:
            break  # newest-first: everything after this is older still
        if session_filter and session_filter not in p.stem.lower():
            continue
        picked.append(item)
        if len(picked) >= target:
            break
    return picked


def _imported_trace_matches_filters(
    store: ContextStore,
    trace_id: str,
    *,
    session_filter: str = "",
    cutoff: datetime | None = None,
) -> bool:
    trace = store.get_trace(trace_id)
    if trace is None:
        return False
    if cutoff is not None and (trace.created_at is None or trace.created_at < cutoff):
        return False
    sid = (trace.session_id or trace.id or "").strip().lower()
    return not session_filter or session_filter in sid


def _import_live_host_sessions(
    *,
    host_name: str,
    importer_cls: type[Any],
    store: ContextStore,
    path: Path | None,
    force: bool,
    max_per_host: int,
    limit: int,
    session_filter: str = "",
    cutoff: datetime | None = None,
) -> list[str]:
    """Discover, filter, and import one host's live sessions into *store*.

    Single source of truth for which sessions a live scan surfaces: both the
    JSON presenter (``_scan_hosts_live``) and the text presenter
    (``_stream_hosts_live``) call this, so --since/--id/--scan/--limit can
    never disagree between them (previously JSON pre-filtered every host via
    find_*()+pick while text special-cased a narrower host set — e.g. copilot
    transcripts/debug-logs were only ever live-imported for JSON, and --since
    was silently ignored for generic hosts in text mode).

    Returns imported trace ids, newest-first.
    """
    if host_name == "codex":
        from atelier.gateway.hosts.session_parsers.codex import CodexImporter, find_codex_sessions

        codex_importer = CodexImporter(store)
        picked = _pick_live_sessions(
            list(find_codex_sessions(path)),
            path_of=lambda p: p,
            limit=limit,
            scan=max_per_host,
            session_filter=session_filter,
            cutoff=cutoff,
        )
        imported: list[str] = []
        for session_path in picked:
            tid = codex_importer.import_session(session_path, force=force)
            if tid and _imported_trace_matches_filters(
                store,
                tid,
                session_filter=session_filter,
                cutoff=cutoff,
            ):
                imported.append(tid)
                if len(imported) >= limit:
                    break
        return imported

    if host_name == "claude":
        from atelier.gateway.hosts.session_parsers.claude import ClaudeImporter, find_claude_sessions

        claude_importer = ClaudeImporter(store)
        claude_root = path if path is not None else None
        picked_sessions = _pick_live_sessions(
            list(find_claude_sessions(claude_root)),
            path_of=lambda item: item[1],
            limit=limit,
            scan=max_per_host,
            session_filter=session_filter,
            cutoff=cutoff,
        )
        imported = []
        for workspace_slug, session_path in picked_sessions:
            tid = claude_importer.import_session(workspace_slug, session_path, force=force)
            if tid and _imported_trace_matches_filters(
                store,
                tid,
                session_filter=session_filter,
                cutoff=cutoff,
            ):
                imported.append(tid)
                if len(imported) >= limit:
                    break
        return imported

    if host_name == "copilot":
        from atelier.gateway.hosts.session_parsers.copilot import (
            CopilotImporter,
            find_copilot_debug_log_dirs,
            find_copilot_sessions,
            find_copilot_transcript_files,
        )

        copilot_importer = CopilotImporter(store)
        imported = []
        picked_sessions = _pick_live_sessions(
            list(find_copilot_sessions(path)),
            path_of=lambda p: p,
            limit=limit,
            scan=max_per_host,
            session_filter=session_filter,
            cutoff=cutoff,
        )
        for session_dir in picked_sessions:
            tid = copilot_importer.import_session(session_dir, force=force)
            if tid and _imported_trace_matches_filters(
                store,
                tid,
                session_filter=session_filter,
                cutoff=cutoff,
            ):
                imported.append(tid)
                if len(imported) >= limit:
                    return imported
        # Transcript files and debug-log directories are copilot's other two
        # session sources (CopilotImporter.import_all imports all three) --
        # apply the same since/--id/--scan filters so text and JSON agree.
        picked_transcripts = _pick_live_sessions(
            list(find_copilot_transcript_files(path)),
            path_of=lambda p: p,
            limit=limit,
            scan=max_per_host,
            session_filter=session_filter,
            cutoff=cutoff,
        )
        for transcript_path in picked_transcripts:
            tid = copilot_importer.import_transcript_file(transcript_path, force=force)
            if tid and _imported_trace_matches_filters(
                store,
                tid,
                session_filter=session_filter,
                cutoff=cutoff,
            ):
                imported.append(tid)
                if len(imported) >= limit:
                    return imported
        picked_debug = _pick_live_sessions(
            list(find_copilot_debug_log_dirs(path)),
            path_of=lambda p: p,
            limit=limit,
            scan=max_per_host,
            session_filter=session_filter,
            cutoff=cutoff,
        )
        for debug_log_dir in picked_debug:
            tid = copilot_importer.import_debug_log_dir(debug_log_dir, force=force)
            if tid and _imported_trace_matches_filters(
                store,
                tid,
                session_filter=session_filter,
                cutoff=cutoff,
            ):
                imported.append(tid)
                if len(imported) >= limit:
                    break
        return imported

    if host_name == "opencode":
        from atelier.gateway.hosts.session_parsers.opencode import (
            OpenCodeImporter,
            find_opencode_sessions,
        )
        from atelier.gateway.hosts.session_parsers.opencode import (
            _ms_to_dt as _oc_ms_to_dt,
        )

        oc_db = path or (Path.home() / ".local/share/opencode/opencode.db")
        if not oc_db.exists():
            return []
        opencode_importer = OpenCodeImporter(store)
        all_oc = find_opencode_sessions(oc_db)  # already newest-first (ORDER BY time_created DESC)
        if cutoff is not None:
            all_oc = [r for r in all_oc if _oc_ms_to_dt(r.get("time_created")) >= cutoff]
        if session_filter:
            all_oc = [r for r in all_oc if session_filter in str(r.get("id") or "").lower()]
        picked_oc = all_oc[:max_per_host][:limit]
        imported = []
        for session_row in picked_oc:
            tid = opencode_importer.import_session(session_row, oc_db, force=force)
            if tid:
                imported.append(tid)
        return imported

    # Generic importers (antigravity, cursor, ...): no per-file discovery is
    # exposed at this layer to pre-filter before import, so when there's
    # nothing to filter by, import exactly `limit` newest sessions (cheap
    # path, matches the other hosts' picked count). Only pay for scanning up
    # to `max_per_host` when --since/--id require looking past the newest
    # `limit` sessions for a match -- contract: import_all(limit=N) returns
    # the N newest sessions, newest-first.
    import_cap = max_per_host if (cutoff is not None or session_filter) else limit
    generic_importer: Any = importer_cls(store)
    imported_ids = list(
        generic_importer.import_all(path, force=force, limit=import_cap)
        if path is not None
        else generic_importer.import_all(force=force, limit=import_cap)
    )
    if cutoff is None and not session_filter:
        return imported_ids[:limit]
    filtered: list[str] = []
    for tid in imported_ids:
        trace = store.get_trace(tid)
        if trace is None:
            continue
        if cutoff is not None and trace.created_at < cutoff:
            continue
        sid = (trace.session_id or trace.id or "").strip().lower()
        if session_filter and session_filter not in sid:
            continue
        filtered.append(tid)
        if len(filtered) >= limit:
            break
    return filtered


def _scan_hosts_live(
    *,
    selected_hosts: list[str],
    force: bool,
    path: Path | None,
    max_per_host: int,
    limit: int,
    session_filter: str = "",
    cutoff: datetime | None = None,
) -> tuple[dict[str, int], dict[str, list[str]], ContextStore, tempfile.TemporaryDirectory[str]]:
    """JSON presenter: live-import each selected host via ``_import_live_host_sessions``.

    Returns ``(counts, imported_ids_by_host, store, tmp)``. Callers should
    build display rows from ``imported_ids_by_host`` directly rather than
    re-querying ``store.list_traces(since=cutoff, ...)``: the importer already
    applied the live-scan filters before returning trace ids.
    """
    from atelier.gateway.hosts.session_parsers.registry import iter_importer_classes

    tmp = tempfile.TemporaryDirectory(prefix="atelier-session-hosts-")
    tmp_root = Path(tmp.name)
    store = ContextStore(tmp_root)
    store.init()

    counts: dict[str, int] = {}
    imported_by_host: dict[str, list[str]] = {}
    host_set = set(selected_hosts)
    for host_name, importer_cls in iter_importer_classes():
        if host_set and host_name not in host_set:
            continue
        try:
            imported_ids = _import_live_host_sessions(
                host_name=host_name,
                importer_cls=importer_cls,
                store=store,
                path=path,
                force=force,
                max_per_host=max_per_host,
                limit=limit,
                session_filter=session_filter,
                cutoff=cutoff,
            )
            counts[host_name] = len(imported_ids)
            imported_by_host[host_name] = imported_ids
        except Exception:
            logging.exception("session hosts live scan failed for host=%s", host_name)
            counts[host_name] = 0
            imported_by_host[host_name] = []
    return counts, imported_by_host, store, tmp


def _stream_hosts_live(
    *,
    selected_hosts: list[str],
    force: bool,
    path: Path | None,
    max_per_host: int,
    limit: int,
    root: Path,
    session_filter: str = "",
    cutoff: datetime | None = None,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """Text presenter: live-import each selected host and stream-print rows.

    Delegates discovery/filtering/import to ``_import_live_host_sessions`` --
    the same routine ``_scan_hosts_live`` (JSON) uses -- so text and JSON
    output can never diverge on which sessions get shown.
    """
    from atelier.gateway.hosts.session_parsers.registry import iter_importer_classes

    tmp = tempfile.TemporaryDirectory(prefix="atelier-session-hosts-")
    tmp_root = Path(tmp.name)
    store = ContextStore(tmp_root)
    store.init()

    host_set = set(selected_hosts)
    any_found = False
    collected_rows: list[dict[str, Any]] = []

    for host_name, importer_cls in iter_importer_classes():
        if host_set and host_name not in host_set:
            continue
        try:
            imported_ids = _import_live_host_sessions(
                host_name=host_name,
                importer_cls=importer_cls,
                store=store,
                path=path,
                force=force,
                max_per_host=max_per_host,
                limit=limit,
                session_filter=session_filter,
                cutoff=cutoff,
            )
        except Exception:
            logging.exception("session hosts live scan failed for host=%s", host_name)
            continue
        if not imported_ids:
            continue
        any_found = True
        # Header count is the actual imported/displayed count for this host
        # (not a pre-import "picked" estimate), so it agrees with what's
        # printed below and with the footer totals.
        _render_host_header_rich(host_name, len(imported_ids))
        for tid in imported_ids:
            trace = store.get_trace(tid)
            if trace is None:
                continue
            row = _build_session_row(trace, store, host_name, root)
            collected_rows.append(row)
            _print_session_row(row, verbose)

    tmp.cleanup()

    if not any_found:
        click.echo("No host sessions found for the selected filters.")

    return collected_rows


@session_group.command("list")
@click.option(
    "--host",
    "hosts",
    multiple=True,
    type=click.Choice(list(SUPPORTED_SESSION_IMPORT_HOSTS)),
    help="Filter to one or more hosts. Repeat option to include multiple hosts.",
)
@click.option("--limit", default=5, show_default=True, type=click.IntRange(min=1), help="Rows per host.")
@click.option(
    "--scan",
    default=500,
    show_default=True,
    type=click.IntRange(min=1),
    help="Upper bound on live pre-scan per host; effective live import cap is min(--scan, --limit).",
)
@click.option("--since", default=None, help="Look-back window, e.g. 7d, 24h.")
@click.option("--id", "session_id_filter", default=None, help="Filter by session-id substring.")
@click.option("--verbose", is_flag=True, default=False, help="Show per-session tool and command details.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.option(
    "--source",
    "source_mode",
    type=click.Choice(["live", "store"]),
    default="live",
    show_default=True,
    help="live=read directly from host session directories via a temporary store (no persistent import), store=read existing Atelier store only.",
)
@click.option("--force", is_flag=True, default=False, help="Force host re-import while syncing.")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override source path for the selected host (requires exactly one --host).",
)
@click.pass_context
def session_list_cmd(
    ctx: click.Context,
    hosts: tuple[str, ...],
    limit: int,
    scan: int,
    since: str | None,
    session_id_filter: str | None,
    verbose: bool,
    as_json: bool,
    source_mode: str,
    force: bool,
    path: Path | None,
) -> None:
    """List host sessions derived from host session files."""
    root: Path = ctx.obj["root"]
    selected_hosts = list(hosts) if hosts else list(SUPPORTED_SESSION_IMPORT_HOSTS)

    if path is not None and len(selected_hosts) != 1:
        raise click.ClickException("--path requires exactly one --host")

    cutoff = datetime.now(UTC) - _parse_duration(since) if since else None
    session_filter = (session_id_filter or "").strip().lower()

    if as_json:
        # Batch mode for JSON output: scan all hosts, collect rows, then dump.
        sync_counts: dict[str, int] = {}
        live_ids: dict[str, list[str]] = {}
        temp_handle: tempfile.TemporaryDirectory[str] | None = None
        if source_mode == "live":
            sync_counts, live_ids, store, temp_handle = _scan_hosts_live(
                selected_hosts=selected_hosts,
                force=force,
                path=path,
                max_per_host=scan,
                limit=limit,
                session_filter=session_filter,
                cutoff=cutoff,
            )
        else:
            store = _load_store(root)

        grouped: dict[str, list[dict[str, Any]]] = {}
        try:
            for host_name in selected_hosts:
                rows: list[dict[str, Any]] = []
                if source_mode == "live":
                    # Live picks are already filtered by since/--id/--scan at
                    # import time; text and JSON share _import_live_host_sessions
                    # so they render the same selected trace ids.
                    for tid in live_ids.get(host_name, []):
                        trace = store.get_trace(tid)
                        if trace is not None:
                            rows.append(_build_session_row(trace, store, host_name, root))
                else:
                    traces = store.list_traces(host=host_name, since=cutoff, limit=scan)
                    for trace in traces:
                        sid = (trace.session_id or trace.id or "").strip()
                        if session_filter and session_filter not in sid.lower():
                            continue
                        rows.append(_build_session_row(trace, store, host_name, root))
                        if len(rows) >= limit:
                            break
                if rows:
                    grouped[host_name] = rows

            click.echo(
                json.dumps(
                    {"source": source_mode, "scan_counts": sync_counts, "hosts": grouped},
                    indent=2,
                    default=str,
                )
            )
        finally:
            if temp_handle is not None:
                temp_handle.cleanup()
        return

    # Text display: stream per-host so each host's sessions appear immediately.
    if source_mode == "store":
        store = _load_store(root)
        any_found = False
        all_store_rows: list[dict[str, Any]] = []
        for host_name in sorted(selected_hosts):
            traces = store.list_traces(host=host_name, since=cutoff, limit=scan)
            rows_store: list[dict[str, Any]] = []
            for trace in traces:
                sid = (trace.session_id or trace.id or "").strip()
                if session_filter and session_filter not in sid.lower():
                    continue
                rows_store.append(_build_session_row(trace, store, host_name, root))
                if len(rows_store) >= limit:
                    break
            if rows_store:
                any_found = True
                _render_host_header_rich(host_name, 0)
                for row in rows_store:
                    _print_session_row(row, verbose)
                all_store_rows.extend(rows_store)
        if not any_found:
            click.echo("No host sessions found for the selected filters.")
        elif all_store_rows:
            _render_hosts_footer_rich(all_store_rows, since or f"{limit} sessions")
        return

    # live mode + text: stream each host as it's scanned
    displayed_rows = _stream_hosts_live(
        selected_hosts=selected_hosts,
        force=force,
        path=path,
        max_per_host=scan,
        limit=limit,
        root=root,
        session_filter=session_filter,
        cutoff=cutoff,
        verbose=verbose,
    )
    # Every row here was already successfully imported and printed above, so
    # the footer total agrees with the per-host header counts and the rows
    # the user just saw (no separate "active-only" re-filter to disagree).
    if displayed_rows:
        _render_hosts_footer_rich(displayed_rows, since or f"{limit} sessions")


def _print_stats(
    rows: list[dict[str, Any]],
    since_label: str,
    top: int,
    show_header: bool = True,
) -> None:
    """Print aggregate usage statistics from a list of session rows."""
    if not rows:
        click.echo("No sessions found.")
        return

    # --- aggregate totals ---
    n = len(rows)
    n_atelier = sum(1 for r in rows if int(r["atelier_calls"]) > 0)
    total_cost = sum(float(r["cost_usd"]) for r in rows)
    total_saved = sum(float(r["saved_usd"]) for r in rows)
    total_carry = sum(float(r["carry_usd"]) for r in rows)
    total_in = sum(int(r["input_tokens"]) for r in rows)
    total_cr = sum(int(r["cache_read_tokens"]) for r in rows)
    total_cw = sum(int(r["cache_write_tokens"]) for r in rows)
    total_out = sum(int(r["output_tokens"]) for r in rows)
    total_calls = sum(int(r["tool_calls"]) for r in rows)
    total_atelier = sum(int(r["atelier_calls"]) for r in rows)
    total_builtin = sum(int(r["builtin_calls"]) for r in rows)
    total_subagents = sum(int(r["subagents"]) for r in rows)
    total_sub_cost = sum(float(r["subagent_cost_usd"]) for r in rows)
    total_pot_usd = sum(float(r["potential_saved_usd"]) for r in rows)
    total_pot_carry = sum(float(r["potential_carry_usd"]) for r in rows)

    # per-host aggregation
    host_agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        hn = str(r.get("host") or r.get("source") or "unknown")
        if hn not in host_agg:
            host_agg[hn] = {
                "sessions": 0,
                "cost": 0.0,
                "saved": 0.0,
                "carry": 0.0,
                "calls": 0,
                "atelier": 0,
                "builtin": 0,
                "pot_saved": 0.0,
                "pot_carry": 0.0,
            }
        ha = host_agg[hn]
        ha["sessions"] += 1
        ha["cost"] += float(r["cost_usd"])
        ha["saved"] += float(r["saved_usd"])
        ha["carry"] += float(r["carry_usd"])
        ha["calls"] += int(r["tool_calls"])
        ha["atelier"] += int(r["atelier_calls"])
        ha["builtin"] += int(r["builtin_calls"])
        ha["pot_saved"] += float(r["potential_saved_usd"])
        ha["pot_carry"] += float(r["potential_carry_usd"])

    hosts_sorted = sorted(host_agg.items(), key=lambda x: -x[1]["cost"])

    # header
    hosts_label = ", ".join(h for h, _ in hosts_sorted if host_agg[h]["sessions"] > 0)
    if show_header:
        click.secho(f"Last {since_label}  ·  {n} sessions  ·  {hosts_label}", bold=True)
        if n_atelier > 0:
            click.echo(f"  {n_atelier} of {n} sessions used Atelier tools")

    # total section
    click.echo("")
    click.secho("  Total", bold=True)
    total_rows: list[tuple[str, str]] = []

    cost_str = click.style(f"${total_cost:.4f}", bold=True)
    if total_saved + total_carry > 0:
        baseline = total_cost + total_saved + total_carry
        pct = 100 * (total_saved + total_carry) / baseline
        savings_str = (
            click.style(f"  saved ${total_saved:.4f}", fg="green")
            + click.style(f" + carry ${total_carry:.4f}", fg="magenta")
            + click.style(f" via Atelier  (-{pct:.1f}% vs baseline ≈${baseline:.4f})", dim=True)
        )
        total_rows.append(("cost", cost_str + savings_str))
    else:
        total_rows.append(("cost", cost_str))

    total_rows.append(
        (
            "tokens",
            f"in={_fmt_tok_compact(total_in)}"
            f"  cR={_fmt_tok_compact(total_cr)}"
            f"  cW={_fmt_tok_compact(total_cw)}"
            f"  out={_fmt_tok_compact(total_out)}",
        )
    )

    if total_calls > 0:
        atelier_pct = 100 * total_atelier / total_calls
        calls_str = (
            f"{total_calls:,} total · "
            + click.style(f"{total_atelier:,} atelier ({atelier_pct:.0f}%)", fg="cyan")
            + f" · {total_builtin:,} builtin"
        )
        total_rows.append(("calls", calls_str))

    if total_subagents > 0:
        sub_pct = 100 * total_sub_cost / total_cost if total_cost > 0 else 0.0
        total_rows.append(("subagents", f"{total_subagents} total · ≈${total_sub_cost:.4f} ({sub_pct:.1f}% of cost)"))

    if total_pot_usd > 0 or total_pot_carry > 0:
        pot_str = click.style(f"≈${total_pot_usd:.4f} saved", fg="yellow")
        if total_pot_carry > 0:
            pot_str += click.style(f" + ≈${total_pot_carry:.4f} carry", fg="yellow")
        pot_str += click.style(" via Atelier", fg="yellow")
        total_rows.append(("potential", pot_str))

    _emit_tree_rows(total_rows)

    # by host section
    if len(host_agg) > 1:
        click.echo("")
        click.secho("  By host", bold=True)
        host_rows: list[tuple[str, str]] = []
        for hn, ha in hosts_sorted:
            if ha["sessions"] == 0:
                continue
            atelier_pct = 100 * ha["atelier"] / ha["calls"] if ha["calls"] > 0 else 0.0
            parts = [
                click.style(f"${ha['cost']:.4f}", bold=True),
                f"{ha['sessions']} session{'s' if ha['sessions'] != 1 else ''}",
                f"{ha['calls']:,} calls ({atelier_pct:.0f}% atelier)",
            ]
            # realized savings
            if ha["saved"] > 0 or ha["carry"] > 0:
                sp = []
                if ha["saved"] > 0:
                    sp.append(click.style(f"saved ${ha['saved']:.4f}", fg="green"))
                if ha["carry"] > 0:
                    sp.append(click.style(f"carry ${ha['carry']:.4f}", fg="magenta"))
                parts.append(" + ".join(sp))
            # potential additional savings (always show if non-zero)
            pot_total = ha["pot_saved"] + ha["pot_carry"]
            if pot_total > 0:
                pot_str = click.style("potential", fg="yellow")
                if ha["pot_saved"] > 0:
                    pot_str += click.style(f" ${ha['pot_saved']:.4f} saved", fg="yellow")
                if ha["pot_carry"] > 0:
                    pot_str += click.style(f" + ${ha['pot_carry']:.4f} carry", fg="yellow")
                parts.append(pot_str)
            host_rows.append((hn, "  ·  ".join(parts)))
        _emit_tree_rows(host_rows)

    # top sessions section
    if top > 0:
        sorted_rows = sorted(rows, key=lambda r: -float(r["cost_usd"]))
        top_rows = [r for r in sorted_rows if float(r["cost_usd"]) > 0][:top]
        if top_rows:
            click.echo("")
            click.secho(f"  Top {len(top_rows)} sessions by cost", bold=True)
            session_rows: list[tuple[str, str]] = []
            for r in top_rows:
                date = str(r["created_at"])[:10] if r["created_at"] else "-"
                sid_short = str(r["session_id"] or "")[:8] if r["session_id"] else "-"
                model_short = str(r["model"] or "-")[:14]
                host_name_r = str(r.get("host") or "")
                prompt = str(r["first_user"] or "").replace("\n", " ").strip()[:60]
                cost = click.style(f"${float(r['cost_usd']):.4f}", bold=True)
                session_rows.append(
                    (
                        f"{date}  {sid_short}",
                        f"{cost}  {host_name_r:<8}  {model_short:<14}  {prompt}",
                    )
                )
            _emit_tree_rows(session_rows)


# ---------------------------------------------------------------------------
# session stats
# ---------------------------------------------------------------------------


@session_group.command("stats")
@click.option("--since", "since_str", default=None, help="Time window, e.g. 1d, 7d, 30d. Default: 7d.")
@click.option("--limit", default=None, type=int, help="Most-recent N sessions (alternative to --since).")
@click.option(
    "--host",
    "hosts_filter",
    multiple=True,
    type=click.Choice(list(SUPPORTED_SESSION_IMPORT_HOSTS)),
    help="Filter by host (can repeat).",
)
@click.option("--source", type=click.Choice(["live", "store"]), default="live", show_default=True)
@click.option("--top", default=5, show_default=True, type=int, help="Top sessions by cost to list.")
@click.option(
    "--path",
    "data_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override source path for the selected host (requires exactly one --host).",
)
@click.pass_context
def session_stats_cmd(
    ctx: click.Context,
    since_str: str | None,
    limit: int | None,
    hosts_filter: tuple[str, ...],
    source: str,
    top: int,
    data_path: Path | None,
) -> None:
    """Aggregate usage statistics. Use --since for a time window or --limit for the last N sessions."""
    if since_str and limit:
        raise click.UsageError("--since and --limit are mutually exclusive.")

    root = Path(ctx.obj["root"])
    selected_hosts = list(hosts_filter) if hosts_filter else list(SUPPORTED_SESSION_IMPORT_HOSTS)

    if data_path is not None and len(selected_hosts) != 1:
        raise click.ClickException("--path requires exactly one --host")

    if limit:
        cutoff: datetime | None = None
        scan_cap = limit
        label = f"{limit} sessions"
        since_mode = False
    else:
        since_str = since_str or "7d"
        cutoff = datetime.now(UTC) - _parse_duration(since_str)
        scan_cap = 15  # practical limit per host; active large sessions can be slow to parse
        label = since_str
        since_mode = True

    all_rows: list[dict[str, Any]] = []
    truncated = False

    if source == "store":
        store = _load_store(root)
        for hn in selected_hosts:
            for trace in store.list_traces(host=hn, since=cutoff, limit=scan_cap):
                all_rows.append(_build_session_row(trace, store, hn, root))
    else:
        click.echo(f"Scanning last {label} across {len(selected_hosts)} host(s)…", err=True)
        _sync_counts, live_ids, store, tmp_handle = _scan_hosts_live(
            selected_hosts=selected_hosts,
            force=False,
            path=data_path,
            max_per_host=scan_cap,
            limit=scan_cap,
            cutoff=cutoff,
        )
        try:
            for hn in selected_hosts:
                # Live picks are already filtered by --since against file
                # mtime / session activity; use them directly rather than
                # re-querying the temp store by created_at (see
                # _scan_hosts_live docstring for why the two can disagree).
                ids = live_ids.get(hn, [])
                if since_mode and len(ids) >= scan_cap:
                    truncated = True
                for tid in ids:
                    trace = store.get_trace(tid)
                    if trace is not None:
                        all_rows.append(_build_session_row(trace, store, hn, root))
        finally:
            tmp_handle.cleanup()

    all_rows = [r for r in all_rows if int(r["tool_calls"]) > 0 or int(r["input_tokens"]) > 0]

    if limit:
        # --limit is documented as the global most-recent N sessions; the
        # scan above caps per host (bounding parse work, which guarantees
        # the global top-N is among the scanned rows), so trim the merged
        # rows to the newest N across all hosts.
        all_rows.sort(key=lambda r: str(r["created_at"]), reverse=True)
        all_rows = all_rows[:limit]

    if not all_rows:
        click.echo(f"No sessions found in the last {label}.")
        return

    if truncated:
        # scan_cap is a per-host parse-cost cap, not a claim about the full
        # window -- say so explicitly instead of a header that implies
        # completeness ("Last 7d") while silently dropping older matches.
        label = f"{label} (capped at {scan_cap}/host, more may exist)"

    _render_stats_rich(all_rows, label, top)


__all__ = ["outcomes_group", "runs_group", "session_group"]
