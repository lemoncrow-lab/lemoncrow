"""``lc rt map``: browse this repository's code map in the terminal."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

import click
from lemoncrow_client.browser import launch_browser_url

from lemoncrow.gateway.cli import style


@click.command("map", short_help="Browse this repository's code: folders, symbols, imports.")
@click.option("--open", "open_browser", is_flag=True, help="Open the Code view of the local web dashboard instead.")
@click.pass_context
def map_cmd(ctx: click.Context, open_browser: bool) -> None:
    """Browse this repository's code map in the terminal.

    Folders and files with their symbol counts, each file's symbols, what it
    imports and what imports it, symbol source, and symbol search, read from
    the local Workspaces Lite index. Off a terminal it prints the overview.
    Press o on a file or symbol to open it in $VISUAL / $EDITOR.
    """
    from lemoncrow.core.foundation.paths import WorkspaceNotRegisteredError, resolve_workspace_root

    try:
        repo = resolve_workspace_root()
    except WorkspaceNotRegisteredError:
        raise style.Redirect(
            "Not inside a git repository.", "Run it from a repository:", "cd <repo> && lc rt map"
        ) from None
    if open_browser:
        _open_dashboard_code(ctx.obj["root"], repo)
        return

    from lemoncrow.core.service.code_map_lite import LiteCodeMapEngine
    from lemoncrow.gateway.cli import picker
    from lemoncrow.gateway.cli.code_map_ui import CodeMap, MapSource, overview_lines

    engine = LiteCodeMapEngine(repo)
    _prepare_with_progress(engine)
    with style.working("Reading the code map…"):
        code_map = CodeMap(engine, engine.connection())
    if not code_map.files:
        click.echo(style.row("off", "The index has no files for this repository.", indent=0))
        click.echo(style.next_step("Rebuild it:", "lc rt index --force", indent=0))
        return
    if picker.can_open():
        picker.run(picker.Picker(MapSource(code_map)))
    else:
        click.echo("\n".join(overview_lines(code_map)))


def _prepare_with_progress(engine: Any) -> None:
    """Bring the Lite index current; a bar appears only when that takes a while (a first build)."""
    from rich.console import Console
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

    from lemoncrow.gateway.cli.commands.code import _LITE_PHASES

    console = Console(stderr=True, highlight=False)
    brand = f"color({style.BRAND})"
    progress = Progress(
        SpinnerColumn("dots", style=brand),
        TextColumn("[dim]Updating the code index ·[/dim] {task.description}"),
        BarColumn(bar_width=None, style="grey30", complete_style=brand, pulse_style=brand),
        MofNCompleteColumn(),
        console=console,
        transient=True,
        expand=True,
        disable=not console.is_terminal,
    )
    task = progress.add_task(_LITE_PHASES["seed"], total=None)

    def report(seen: Any) -> None:
        progress.update(
            task, description=_LITE_PHASES.get(seen.phase, seen.phase), completed=seen.done, total=seen.total or None
        )

    try:
        with style.after_delay(progress.start, progress.stop):
            engine.prepare(progress=report)
    except Exception as exc:  # a stale index still reads; say so and carry on
        click.echo(
            style.row("warn", f"Couldn't update the index ({type(exc).__name__}); showing the last one."), err=True
        )


def _open_dashboard_code(root: Path, repo: Path) -> None:
    from lemoncrow.infra.runtime.dashboard_url import discover_dashboard_url

    frontend_url = discover_dashboard_url(root)
    if frontend_url is None:
        raise style.Redirect(
            "The web dashboard isn't running here.",
            "The same map is in the terminal:",
            "lc rt map",
        )
    url = f"{frontend_url.rstrip('/')}/code?{urllib.parse.urlencode({'repo': str(repo)})}"
    from lemoncrow.gateway.cli.commands.review import arm_local_browser_url

    click.echo(f"{style.accent('◆')} Opening the code map: {url}")
    arm_local_browser_url(url)
    if not launch_browser_url(url):
        click.echo(f"(could not open a browser; open {url} manually.)", err=True)


__all__ = ["map_cmd"]
