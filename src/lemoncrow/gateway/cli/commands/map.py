"""Backward-compatible shortcut to the dashboard's Map route."""

from __future__ import annotations

import urllib.parse
import webbrowser

import click

from lemoncrow.core.foundation.paths import resolve_workspace_root
from lemoncrow.infra.runtime.dashboard_url import discover_dashboard_url


@click.command("map")
@click.option("--no-open", is_flag=True, help="Print the local URL without opening a browser.")
@click.pass_context
def map_cmd(ctx: click.Context, no_open: bool) -> None:
    """Open Map inside the already-running LemonCrow dashboard."""

    frontend_url = discover_dashboard_url(ctx.obj["root"])
    if frontend_url is None:
        raise click.ClickException(
            "LemonCrow dashboard is not running. Start it once, then use the Map tab: lc stack start"
        )
    query = urllib.parse.urlencode({"repo": str(resolve_workspace_root())})
    url = f"{frontend_url.rstrip('/')}/map?{query}"
    click.echo(f"◆ Dashboard Map: {url}")
    if not no_open:
        webbrowser.open(url)


__all__ = ["map_cmd"]
