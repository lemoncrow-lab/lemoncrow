from __future__ import annotations

import click

from atelier.gateway.cli.commands._shared import _emit


@click.group("telemetry")
def telemetry_group() -> None:
    """Product telemetry controls."""


@telemetry_group.command("status")
@click.option("--json", "as_json", is_flag=True)
def telemetry_status(as_json: bool) -> None:
    from atelier.core.foundation.identity import (
        get_anon_id,
        new_session_id,
        telemetry_id_path,
    )
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.banner import is_acknowledged
    from atelier.core.service.telemetry.config import config_path, load_telemetry_config
    from atelier.core.service.telemetry.local_store import default_db_path

    session_id = new_session_id()
    emit_product(
        "cli_command_invoked",
        command_name="telemetry_status",
        session_id=session_id,
        anon_id=get_anon_id(),
    )
    cfg = load_telemetry_config()
    payload = {
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
    click.echo(f"lexical frustration detection: {'on' if cfg.lexical_frustration_enabled else 'off'}")
    click.echo(f"local database: {payload['local_db_path']}")


@telemetry_group.command("on")
def telemetry_on() -> None:
    from atelier.core.service.telemetry import set_remote_enabled

    set_remote_enabled(True)
    click.echo("remote telemetry: on")


@telemetry_group.command("off")
def telemetry_off() -> None:
    from atelier.core.service.telemetry import set_remote_enabled
    from atelier.core.service.telemetry.banner import mark_acknowledged

    set_remote_enabled(False)
    mark_acknowledged()
    click.echo("remote telemetry: off")


@telemetry_group.command("show")
@click.option("--limit", default=20, show_default=True, type=int)
def telemetry_show(limit: int) -> None:
    from atelier.core.service.telemetry.local_store import LocalTelemetryStore

    events = LocalTelemetryStore().list_events(limit=limit)
    _emit([{"event": item["event"], "props": item["props"]} for item in events], as_json=True)


@telemetry_group.command("reset-id")
def telemetry_reset_id() -> None:
    from atelier.core.foundation.identity import reset_anon_id

    click.echo(reset_anon_id())


@telemetry_group.group("lexical")
def telemetry_lexical_group() -> None:
    """Lexical frustration detection controls."""


@telemetry_lexical_group.command("on")
def telemetry_lexical_on() -> None:
    from atelier.core.service.telemetry.config import save_telemetry_config

    save_telemetry_config(lexical_frustration_enabled=True)
    click.echo("lexical frustration detection: on")


@telemetry_lexical_group.command("off")
def telemetry_lexical_off() -> None:
    from atelier.core.service.telemetry.config import save_telemetry_config

    save_telemetry_config(lexical_frustration_enabled=False)
    click.echo("lexical frustration detection: off")


@telemetry_lexical_group.command("status")
def telemetry_lexical_status() -> None:
    from atelier.core.service.telemetry.config import load_telemetry_config

    cfg = load_telemetry_config()
    click.echo(f"lexical frustration detection: {'on' if cfg.lexical_frustration_enabled else 'off'}")


__all__ = ["telemetry_group", "telemetry_lexical_group"]
