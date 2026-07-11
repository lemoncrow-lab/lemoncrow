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
    """Render the default lemon telemetry view: state, storage, and controls."""
    from pathlib import Path

    from lemoncrow.core.foundation.identity import get_anon_id
    from lemoncrow.core.service.telemetry.banner import is_acknowledged
    from lemoncrow.core.service.telemetry.config import config_path, load_telemetry_config
    from lemoncrow.core.service.telemetry.local_store import default_db_path

    cfg = load_telemetry_config()
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
    lines.append(f"  {'Lexical frustration':<29}{_flag(cfg.lexical_frustration_enabled)}")
    lines.append(f"  {'Startup notice acknowledged':<29}{'yes' if is_acknowledged() else 'no'}")
    lines.append("")
    lines.append(f"  {'Anonymous ID':<14}{anon_short}")
    lines.append(f"  {'Local events':<14}{_short(default_db_path())}")
    lines.append(f"  {'Config file':<14}{_short(config_path())}")
    lines.append("")
    lines.append("  Controls")
    lines.append("    lemon telemetry remote on      opt in to anonymous remote telemetry")
    lines.append("    lemon telemetry remote off     keep telemetry local")
    lines.append("    lemon telemetry lexical off    disable frustration detection")
    lines.append("    lemon telemetry reset-id       rotate the anonymous ID")
    lines.append("    lemon telemetry show           inspect locally-stored events")
    lines.append("    lemon telemetry status --json  machine-readable status")
    return "\n".join(lines)


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
    from lemoncrow.core.service.telemetry.config import config_path, load_telemetry_config
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


@telemetry_group.command("show")
@click.option("--limit", default=20, show_default=True, type=int)
def telemetry_show(limit: int) -> None:
    from lemoncrow.core.service.telemetry.local_store import LocalTelemetryStore

    events = LocalTelemetryStore().list_events(limit=limit)
    _emit([{"event": item["event"], "props": item["props"]} for item in events], as_json=True)


@telemetry_group.group("remote")
def telemetry_remote_group() -> None:
    """Anonymous remote telemetry controls."""


@telemetry_remote_group.command("on")
def telemetry_remote_on() -> None:
    from lemoncrow.core.service.telemetry.config import save_telemetry_config

    save_telemetry_config(remote_enabled=True)
    click.echo("anonymous remote telemetry: on")


@telemetry_remote_group.command("off")
def telemetry_remote_off() -> None:
    from lemoncrow.core.service.telemetry.config import save_telemetry_config

    save_telemetry_config(remote_enabled=False)
    click.echo("anonymous remote telemetry: off")


@telemetry_remote_group.command("status")
def telemetry_remote_status() -> None:
    from lemoncrow.core.service.telemetry.config import load_telemetry_config

    cfg = load_telemetry_config()
    click.echo(f"anonymous remote telemetry: {'on' if cfg.remote_enabled else 'off'}")


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


__all__ = ["telemetry_group", "telemetry_lexical_group", "telemetry_remote_group"]
