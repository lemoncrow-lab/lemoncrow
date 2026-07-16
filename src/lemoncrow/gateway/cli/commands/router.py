"""``lc router`` — run a local model-routing proxy daemon."""

from __future__ import annotations

import click

from lemoncrow.gateway.cli.commands._shared import _emit

_ROUTING_UNAVAILABLE = "Model routing is not available in this release."


@click.group("router", hidden=True)
def router_daemon_group() -> None:
    """Local model-routing proxy daemon (start/stop/status/restart).

    Reroutes the host's main model calls per <root>/router/route.json
    ({"*opus*": "openai/gpt-5.5", ...}); empty map = Anthropic passthrough.
    """


@router_daemon_group.command("start")
@click.option("--port", type=int, default=4000, show_default=True)
@click.option(
    "--wire-host", type=click.Choice(["claude"]), default=None, help="Point this host at the proxy (restored on stop)."
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def router_start_cmd(ctx: click.Context, port: int, wire_host: str | None, as_json: bool) -> None:
    """Start the router proxy daemon (detached)."""
    del ctx, port, wire_host, as_json
    raise click.ClickException(_ROUTING_UNAVAILABLE)


@router_daemon_group.command("stop")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def router_stop_cmd(ctx: click.Context, as_json: bool) -> None:
    """Stop the router daemon and restore host settings."""
    from lemoncrow.core.capabilities import router_daemon

    result = router_daemon.stop(ctx.obj["root"])
    if as_json:
        _emit(result, as_json=True)
        return
    click.echo("Router stopped." if result.get("stopped") else f"Router not running ({result.get('reason', '')}).")


@router_daemon_group.command("status")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def router_status_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show daemon liveness, port, and PID."""
    from lemoncrow.core.capabilities import router_daemon

    result = router_daemon.status(ctx.obj["root"])
    if as_json:
        _emit(result, as_json=True)
        return
    if result.get("running"):
        click.echo(
            f"running — pid {result['pid']}, {result['base_url']}"
            + (f", wired:{result['host_wired']}" if result.get("host_wired") else "")
        )
    elif result.get("stale"):
        click.echo(f"not running (stale state, pid {result.get('pid')} dead)")
    else:
        click.echo("not running")


@router_daemon_group.command("restart")
@click.option("--port", type=int, default=4000, show_default=True)
@click.option("--wire-host", type=click.Choice(["claude"]), default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def router_restart_cmd(ctx: click.Context, port: int, wire_host: str | None, as_json: bool) -> None:
    """Stop (if running) and start a fresh daemon, picking up route.json."""
    del ctx, port, wire_host, as_json
    raise click.ClickException(_ROUTING_UNAVAILABLE)
