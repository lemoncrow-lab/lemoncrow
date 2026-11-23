from __future__ import annotations

import click

from lemoncrow.gateway.cli.commands._shared import _emit


@click.group("telemetry", invoke_without_command=True)
@click.pass_context
def telemetry_group(ctx: click.Context) -> None:
    """Product telemetry controls."""
    if ctx.invoked_subcommand is None:
        click.echo(_render_telemetry_overview())


def _render_telemetry_overview() -> str:
    """Render the default lc rt telemetry view: state, storage, and controls."""
    from pathlib import Path

    from lemoncrow.core.foundation.identity import get_anon_id
    from lemoncrow.core.service.telemetry.banner import is_acknowledged
    from lemoncrow.core.service.telemetry.config import config_path, load_telemetry_config, telemetry_status
    from lemoncrow.core.service.telemetry.local_store import default_db_path

    cfg = load_telemetry_config()
    status = telemetry_status()
    anon = get_anon_id() or ""
    anon_short = (anon[:12] + "…") if len(anon) > 12 else (anon or "—")

    def _short(path: object) -> str:
        text = str(path)
        home = str(Path.home())
        return "~" + text[len(home) :] if text.startswith(home) else text

    def _flag(on: bool) -> str:
        return "on" if on else "off"

    lines = ["LemonCrow telemetry", "─" * 56]
    lines.append(f"  {'Remote telemetry':<29}{_flag(cfg.remote_enabled)}")
    lines.append(f"  {'Destination':<29}{_describe_destination(status)}")
    lines.append(f"  {'Lexical frustration':<29}{_flag(cfg.lexical_frustration_enabled)}")
    lines.append(f"  {'Startup notice acknowledged':<29}{'yes' if is_acknowledged() else 'no'}")
    lines.append("")
    lines.append(f"  {'Anonymous ID':<14}{anon_short}")
    lines.append(f"  {'Local events':<14}{_short(default_db_path())}")
    lines.append(f"  {'Config file':<14}{_short(config_path())}")
    lines.append("")
    lines.append("  Controls")
    lines.append("    lc rt telemetry remote off     opt out of anonymous remote telemetry")
    lines.append("    lc rt telemetry remote on      opt back in (on by default)")
    lines.append("    lc rt telemetry destination    local only, lemoncrow.com, your server, or a custom host")
    lines.append("    lc rt telemetry lexical off    disable frustration detection")
    lines.append("    lc rt telemetry reset-id       rotate the anonymous ID")
    lines.append("    lc rt telemetry show           inspect locally-stored events")
    lines.append("    lc rt telemetry status --json  machine-readable status")
    return "\n".join(lines)


def _describe_destination(status: dict[str, object]) -> str:
    kind = str(status.get("destination") or "off")
    url = str(status.get("destination_url") or "")
    text = {
        "off": "local only",
        "lemoncrow": "lemoncrow.com",
        "server": f"LemonCrow server {url}",
        "custom": f"custom {url}",
    }.get(kind, kind)
    if status.get("managed"):
        text += f" (managed by policy {status.get('policy_path')})"
    elif status.get("destination_setting") == "auto" and kind != "off":
        text += " (automatic)"
    if status.get("destination_error"):
        text += f" -- {status.get('destination_error')}"
    return text


@telemetry_group.command("status")
@click.option("--json", "as_json", is_flag=True)
def telemetry_status(as_json: bool) -> None:
    from lemoncrow.core.foundation.identity import (
        get_anon_id,
        new_session_id,
        telemetry_id_path,
    )
    from lemoncrow.core.service.telemetry import emit_product
    from lemoncrow.core.service.telemetry.banner import is_acknowledged
    from lemoncrow.core.service.telemetry.config import config_path, load_telemetry_config, telemetry_status
    from lemoncrow.core.service.telemetry.local_store import default_db_path

    session_id = new_session_id()
    emit_product(
        "cli_command_invoked",
        command_name="telemetry_status",
        session_id=session_id,
        anon_id=get_anon_id(),
    )
    cfg = load_telemetry_config()
    payload = {
        **telemetry_status(),
        "remote_enabled": cfg.remote_enabled,
        "lexical_frustration_enabled": cfg.lexical_frustration_enabled,
        "config_path": str(config_path()),
        "telemetry_id_path": str(telemetry_id_path()),
        "local_db_path": str(default_db_path()),
        "acknowledged": is_acknowledged(),
        "anon_id": get_anon_id(),
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"remote telemetry: {'on' if cfg.remote_enabled else 'off'}")
    click.echo(f"destination: {_describe_destination(payload)}")
    click.echo(f"lexical frustration detection: {'on' if cfg.lexical_frustration_enabled else 'off'}")
    click.echo(f"local database: {payload['local_db_path']}")


@telemetry_group.command("show")
@click.option("--limit", default=20, show_default=True, type=int)
def telemetry_show(limit: int) -> None:
    from lemoncrow.core.service.telemetry.local_store import LocalTelemetryStore

    events = LocalTelemetryStore().list_events(limit=limit)
    _emit([{"event": item["event"], "props": item["props"]} for item in events], as_json=True)


@telemetry_group.group("remote")
def telemetry_remote_group() -> None:
    """Anonymous remote telemetry controls."""


def _save(**changes: object) -> None:
    from lemoncrow.core.service.telemetry.config import TelemetryConfigError, save_telemetry_config

    try:
        save_telemetry_config(**changes)  # type: ignore[arg-type]
    except TelemetryConfigError as exc:
        raise click.ClickException(str(exc)) from exc


@telemetry_remote_group.command("on")
def telemetry_remote_on() -> None:
    _save(remote_enabled=True)
    click.echo("anonymous remote telemetry: on")


@telemetry_remote_group.command("off")
def telemetry_remote_off() -> None:
    _save(remote_enabled=False)
    click.echo("anonymous remote telemetry: off")


@telemetry_remote_group.command("status")
def telemetry_remote_status() -> None:
    from lemoncrow.core.service.telemetry.config import load_telemetry_config

    cfg = load_telemetry_config()
    click.echo(f"anonymous remote telemetry: {'on' if cfg.remote_enabled else 'off'}")


@telemetry_group.command("destination")
@click.argument("kind", required=False, type=click.Choice(["auto", "off", "lemoncrow", "server", "custom"]))
@click.argument("url", required=False)
@click.option("--token", default=None, help="Bearer token for a custom host ('' clears it).")
def telemetry_destination(kind: str | None, url: str | None, token: str | None) -> None:
    """Show or set where anonymous daily rollups go.

    \b
    auto       your LemonCrow server when it is hosted, else lemoncrow.com (default)
    off        local only; nothing leaves this machine
    lemoncrow  lemoncrow.com
    server     the LemonCrow server this install uses (LEMONCROW_URL)
    custom URL any host serving /api/telemetry/rollup and /api/telemetry/events
    """
    from lemoncrow.core.service.telemetry.config import telemetry_status

    if kind is None:
        if url is not None or token is not None:
            raise click.UsageError("name a destination to set a URL or token")
        click.echo(f"telemetry destination: {_describe_destination(telemetry_status())}")
        return
    if url is not None and kind != "custom":
        raise click.UsageError("only the custom destination takes a URL")
    changes: dict[str, object] = {"destination": kind}
    if url is not None:
        changes["endpoint"] = url
    if token is not None:
        changes["token"] = token
    _save(**changes)
    click.echo(f"telemetry destination: {_describe_destination(telemetry_status())}")


@telemetry_group.command("reset-id")
def telemetry_reset_id() -> None:
    from lemoncrow.core.foundation.identity import reset_anon_id

    click.echo(reset_anon_id())


@telemetry_group.group("lexical")
def telemetry_lexical_group() -> None:
    """Lexical frustration detection controls."""


@telemetry_lexical_group.command("on")
def telemetry_lexical_on() -> None:
    from lemoncrow.core.service.telemetry.config import save_telemetry_config

    save_telemetry_config(lexical_frustration_enabled=True)
    click.echo("lexical frustration detection: on")


@telemetry_lexical_group.command("off")
def telemetry_lexical_off() -> None:
    from lemoncrow.core.service.telemetry.config import save_telemetry_config

    save_telemetry_config(lexical_frustration_enabled=False)
    click.echo("lexical frustration detection: off")


@telemetry_lexical_group.command("status")
def telemetry_lexical_status() -> None:
    from lemoncrow.core.service.telemetry.config import load_telemetry_config

    cfg = load_telemetry_config()
    click.echo(f"lexical frustration detection: {'on' if cfg.lexical_frustration_enabled else 'off'}")


def _register_push() -> None:
    # The hosted client is an optional part of an install; without it the rest of this group still works.
    try:
        from lemoncrow.gateway.cli.commands.hosted_telemetry import telemetry_push
    except ImportError:
        return
    telemetry_group.add_command(telemetry_push)


_register_push()

__all__ = ["telemetry_destination", "telemetry_group", "telemetry_lexical_group", "telemetry_remote_group"]
