"""``atelier router`` — run a local model-routing proxy daemon."""

from __future__ import annotations

import click

from atelier.gateway.cli.commands._shared import _emit, require_pro


@click.group("router")
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
    require_pro("model_routing", "The model-routing proxy daemon")

    from atelier.core.capabilities import router_daemon

    result = router_daemon.start(ctx.obj["root"], port=port, wire_host=wire_host)
    if as_json:
        _emit(result, as_json=True)
        return
    if result.get("already_running"):
        click.echo(f"Router already running (pid {result.get('pid')}, {result.get('base_url')}).")
        return
    click.echo(f"Router started: pid {result['pid']} — {result['base_url']}")
    if result.get("host_wired"):
        click.echo(f"Wired {result['host_wired']} -> {result['base_url']} (restored on stop).")
    else:
        click.echo(f"Point your host at it: export ANTHROPIC_BASE_URL={result['base_url']}")


@router_daemon_group.command("stop")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def router_stop_cmd(ctx: click.Context, as_json: bool) -> None:
    """Stop the router daemon and restore host settings."""
    from atelier.core.capabilities import router_daemon

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
    from atelier.core.capabilities import router_daemon

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
    from atelier.core.capabilities import router_daemon

    result = router_daemon.restart(ctx.obj["root"], port=port, wire_host=wire_host)
    if as_json:
        _emit(result, as_json=True)
        return
    click.echo(f"Router restarted: pid {result.get('pid')} — {result.get('base_url')}")
