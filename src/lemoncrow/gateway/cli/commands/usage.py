"""``lc usage`` — where the money and the tokens actually went.

Why this command exists: LemonCrow already recorded every session's usage in
three different stores, and the only surface that read them was `lc savings`,
which answers a *counterfactual* — what you did not spend. That is the second
question. The first one is "what did I spend, on which host, on which model,
for which project", and until now nothing answered it.

So the default screen here is pure visibility: rows in, grouped, totalled, with
the provenance of every dollar stated. Nothing on it requires a savings model,
by design (plan 2026-09-07 §4.2) — the optimisation view lives behind
``lc usage optimize`` and is reached only when asked for.

``lc savings`` stays registered as a hidden alias: installed statusline scripts
shell out to ``lc savings --segment`` and ship from outside this repo.

Heavy imports stay inside the callbacks so ``lc --help`` stays instant.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import click

from lemoncrow.gateway.cli.commands._shared import _emit, _parse_duration

if TYPE_CHECKING:  # `from __future__ import annotations` keeps these off the runtime path.
    from datetime import datetime

    from lemoncrow.pro.capabilities.usage.aggregate import GroupBy

_GROUP_BY_CHOICES = ("host", "provider", "model", "project", "session", "day")


class _PromotedGroup(click.Group):
    """A group that lists its own subcommands even when they are hidden globally.

    ``optimize`` is one Click object registered under two parents: hidden at the
    top level, where ``lc optimize`` survives only as a back-compat alias, and
    attached here as ``lc usage optimize``, which the plan makes the promoted
    spelling. Click stores the object once, so the top-level ``hidden`` flag
    would also erase the promoted name from this group's help -- leaving the
    headline optimisation surface reachable but undiscoverable.

    The flag is lifted for the duration of the render and restored afterwards,
    so ``lc --help`` is unaffected.
    """

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        hidden = [
            command
            for command in (self.get_command(ctx, name) for name in self.list_commands(ctx))
            if command is not None and command.hidden
        ]
        for command in hidden:
            command.hidden = False
        try:
            super().format_commands(ctx, formatter)
        finally:
            for command in hidden:
                command.hidden = True


def _store_root(ctx: click.Context) -> Path:
    """Return the store root, falling back the same way every other command does."""

    from lemoncrow.core.foundation.paths import default_store_root

    obj = ctx.obj or {}
    return Path(obj.get("root") or default_store_root())


def _window_start(since: str) -> datetime:
    """Return the UTC instant *since* names, e.g. ``"7d"`` -> now minus 7 days."""

    from datetime import UTC
    from datetime import datetime as _datetime

    return _datetime.now(UTC) - _parse_duration(since)


@click.group("usage", cls=_PromotedGroup, invoke_without_command=True)
@click.option("--since", default="7d", show_default=True, help="Look-back window: 7d, 12h, 30m.")
@click.option(
    "--by",
    "group_by",
    type=click.Choice(_GROUP_BY_CHOICES),
    default="host",
    show_default=True,
    help="Group the report by this dimension.",
)
@click.option("--host", "host_filter", default=None, help="Only rows from this host.")
@click.option("--model", "model_filter", default=None, help="Only rows for this model id.")
@click.option("--project", "project_filter", default=None, help="Only rows for this project.")
@click.option("--limit", default=20, show_default=True, type=int, help="Max groups to show.")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.option("--no-color", is_flag=True, help="Disable ANSI colour / Rich output.")
@click.pass_context
def usage_group(
    ctx: click.Context,
    since: str,
    group_by: str,
    host_filter: str | None,
    model_filter: str | None,
    project_filter: str | None,
    limit: int,
    as_json: bool,
    no_color: bool,
) -> None:
    """Where your AI coding usage went - across hosts, models, and projects."""

    if ctx.invoked_subcommand is not None:
        return

    from lemoncrow.pro.capabilities.usage.aggregate import aggregate, provenance_mix, totals
    from lemoncrow.pro.capabilities.usage.collect import collect_usage_rows
    from lemoncrow.pro.capabilities.usage.models import USAGE_SCHEMA_VERSION
    from lemoncrow.pro.capabilities.usage.render import render_usage

    started = _window_start(since)
    rows = collect_usage_rows(
        _store_root(ctx),
        since=started,
        host=host_filter,
        model=model_filter,
        project=project_filter,
    )
    # ``--limit`` bounds the *report*, never the collection: the totals row has
    # to cover every row in the window or it is not a total.
    groups = aggregate(rows, by=cast("GroupBy", group_by), limit=limit)
    total = totals(rows)

    if as_json:
        _emit(
            {
                "schema_version": USAGE_SCHEMA_VERSION,
                "since": started.isoformat(),
                "window": since,
                "by": group_by,
                "filters": {"host": host_filter, "model": model_filter, "project": project_filter},
                "limit": limit,
                "groups": [bucket.to_dict() for bucket in groups],
                "totals": total.to_dict(),
                "provenance_mix": [list(entry) for entry in provenance_mix(rows)],
            },
            as_json=True,
        )
        return

    _emit(render_usage(groups, total, by=group_by, window=since, no_color=no_color), as_json=False)


@usage_group.command("explain")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.option("--no-color", is_flag=True, help="Disable ANSI colour / Rich output.")
@click.pass_context
def usage_explain_cmd(ctx: click.Context, run_id: str, as_json: bool, no_color: bool) -> None:
    """Explain why one run consumed what it did."""

    from lemoncrow.pro.capabilities.usage.explain import explain_run
    from lemoncrow.pro.capabilities.usage.render import render_explain

    try:
        payload = explain_run(_store_root(ctx), run_id)
    except ValueError as exc:
        # Unknown or ambiguous id: the message already names the id and the
        # candidates, and a traceback would bury both.
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _emit(payload, as_json=True)
        return
    _emit(render_explain(payload, no_color=no_color), as_json=False)


@usage_group.command("rows")
@click.option("--since", default="7d", show_default=True, help="Look-back window: 7d, 12h, 30m.")
@click.option("--limit", default=200, show_default=True, type=int, help="Max rows to emit.")
@click.option("--host", "host_filter", default=None, help="Only rows from this host.")
@click.option("--model", "model_filter", default=None, help="Only rows for this model id.")
@click.option("--project", "project_filter", default=None, help="Only rows for this project.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=True,
    help="Output JSON (the only format for this command).",
)
@click.pass_context
def usage_rows_cmd(
    ctx: click.Context,
    since: str,
    limit: int,
    host_filter: str | None,
    model_filter: str | None,
    project_filter: str | None,
    as_json: bool,
) -> None:
    """Emit the canonical usage rows for scripting."""

    from lemoncrow.pro.capabilities.usage.collect import collect_usage_rows
    from lemoncrow.pro.capabilities.usage.models import USAGE_SCHEMA_VERSION

    started = _window_start(since)
    rows = collect_usage_rows(
        _store_root(ctx),
        since=started,
        host=host_filter,
        model=model_filter,
        project=project_filter,
        limit=limit,
    )
    # This command exists to be piped into jq; ``--json`` is accepted for
    # symmetry with every other command and is the only shape emitted.
    _emit(
        {
            "schema_version": USAGE_SCHEMA_VERSION,
            "since": started.isoformat(),
            "window": since,
            "count": len(rows),
            "rows": [row.to_dict() for row in rows],
        },
        as_json=True,
    )


__all__ = ["usage_explain_cmd", "usage_group", "usage_rows_cmd"]
