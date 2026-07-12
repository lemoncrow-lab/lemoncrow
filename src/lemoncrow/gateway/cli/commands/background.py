"""Thin ``lc background`` group + hidden ``systemd`` alias (Phase 25-03).

Installs/uninstalls LemonCrow services as systemd (Linux) / launchd (macOS)
units. Unit and label constants are imported verbatim from
``infra/runtime/daemon_units``. The in-flight systemd user-bus tolerance WIP
(``_systemd_user_bus_unavailable``) is preserved behaviourally. The ``systemd``
alias group stays ``hidden=True``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import click

from lemoncrow.core.service.daemon import cli as _daemon_cli
from lemoncrow.gateway.integrations.openmemory_lifecycle import (
    ensure_service_env as _ensure_openmemory_service_env,
)
from lemoncrow.gateway.integrations.openmemory_lifecycle import (
    openmemory_log_path as _openmemory_log_path,
)
from lemoncrow.gateway.integrations.openmemory_lifecycle import (
    openmemory_service_env_path as _openmemory_service_env_path,
)
from lemoncrow.gateway.integrations.openmemory_lifecycle import project_root as _project_root
from lemoncrow.infra.runtime.daemon_units import (
    CONTROLLER_LABEL,
    CONTROLLER_UNIT,
    LAUNCHD_USER_DIR,
    LETTA_LABEL,
    LETTA_UNIT,
    OPENMEMORY_LABEL,
    OPENMEMORY_UNIT,
    STACK_LABEL,
    STACK_UNIT,
    SYSTEMD_USER_DIR,
    ZOEKT_LABEL,
    ZOEKT_UNIT,
    _is_linux,
    _is_macos,
    _subprocess_output,
    _systemd_user_bus_unavailable,
)
from lemoncrow.infra.runtime.stack_lifecycle import (
    _stack_log_path,
)


@click.group("background", hidden=True)
def background_group() -> None:
    """Manage LemonCrow background services (systemd on Linux, launchd on macOS)."""


# Add the daemon service commands (start/stop/status/logs) as 'lc background service'
background_group.add_command(_daemon_cli, name="service")


@background_group.command("install")
@click.option("--with-stack", is_flag=True, help="Also install the visualization stack service.")
@click.option(
    "--with-letta",
    is_flag=True,
    help="Also install the Letta memory server (Docker-based) service.",
)
@click.option(
    "--with-openmemory",
    is_flag=True,
    help="Also install the OpenMemory MCP (Docker-based) service.",
)
@click.option("--with-zoekt", is_flag=True, help="Enable managed Zoekt code search in the stack service.")
@click.pass_context
def background_install(
    ctx: click.Context, with_stack: bool, with_letta: bool, with_openmemory: bool, with_zoekt: bool
) -> None:
    """Install LemonCrow services as background units."""
    root = ctx.obj["root"]
    project_root = _project_root()
    lemoncrow_bin = shutil.which("lemoncrow") or str(Path(sys.argv[0]).resolve())
    # We no longer need a separate lemoncrowd/lcd binary; we use 'lc background service'
    service_start_cmd = f"{lemoncrow_bin} background service start"

    if with_letta and not shutil.which("docker"):
        click.echo(
            "Warning: 'docker' not found on PATH. "
            "The Letta service unit will be created but will fail until Docker is available."
        )

    if with_zoekt and not shutil.which("docker"):
        click.echo(
            "Warning: 'docker' not found on PATH. Managed Zoekt search will be unavailable until Docker is available."
        )

    if with_openmemory:
        _ensure_openmemory_service_env(root)
        missing = [name for name in ("git", "docker", "make") if not shutil.which(name)]
        if missing:
            click.echo(
                "Warning: OpenMemory requires "
                + ", ".join(missing)
                + ". The service unit will be created but will fail until those commands are available."
            )
        if not os.environ.get("LEMONCROW_OPENMEMORY_OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", "")).strip():
            click.echo(
                "Warning: OPENAI_API_KEY not set. "
                "The OpenMemory service unit will be created but startup will fail until the key is provided."
            )

    if _is_linux():
        if not shutil.which("systemctl"):
            raise click.ClickException("systemctl not found.")

        SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

        controller_content = f"""[Unit]
Description=LemonCrow Background Controller
After=network.target

[Service]
Type=simple
ExecStart={lemoncrow_bin} --root {root} servicectl run --auto-update
Restart=always
Environment=LEMONCROW_ROOT={root}
Environment=PYTHONUNBUFFERED=1
WorkingDirectory={project_root}

[Install]
WantedBy=default.target
"""
        (SYSTEMD_USER_DIR / CONTROLLER_UNIT).write_text(controller_content, encoding="utf-8")
        click.echo(f"Installed {CONTROLLER_UNIT}")

        if with_stack:
            zoekt_env = "Environment=LEMONCROW_ZOEKT_MODE=installed\n" if with_zoekt else ""
            stack_content = f"""[Unit]
Description=LemonCrow HTTP Service
After={CONTROLLER_UNIT}

[Service]
Type=simple
WorkingDirectory={project_root}
ExecStart={service_start_cmd}
Restart=on-failure
RestartSec=5
Environment=LEMONCROW_ROOT={root}
Environment=PYTHONUNBUFFERED=1
{zoekt_env}
[Install]
WantedBy=default.target
"""
            (SYSTEMD_USER_DIR / STACK_UNIT).write_text(stack_content, encoding="utf-8")
            click.echo(f"Installed {STACK_UNIT}")

        if with_letta:
            letta_content = f"""[Unit]
Description=LemonCrow Letta Memory Server (Docker)
After=network.target docker.service
Wants=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={lemoncrow_bin} --root {root} letta up
ExecStop={lemoncrow_bin} --root {root} letta down
WorkingDirectory={project_root}
Environment=LEMONCROW_ROOT={root}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
            (SYSTEMD_USER_DIR / LETTA_UNIT).write_text(letta_content, encoding="utf-8")
            click.echo(f"Installed {LETTA_UNIT}")

        if with_openmemory:
            openmemory_content = f"""[Unit]
Description=LemonCrow OpenMemory MCP Server
After=network.target docker.service
Wants=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile=-{_openmemory_service_env_path(root)}
ExecStart={lemoncrow_bin} --root {root} openmemory up
ExecStop={lemoncrow_bin} --root {root} openmemory down
WorkingDirectory={project_root}
StandardOutput=append:{_openmemory_log_path(root)}
StandardError=append:{_openmemory_log_path(root)}

[Install]
WantedBy=default.target
"""
            (SYSTEMD_USER_DIR / OPENMEMORY_UNIT).write_text(openmemory_content, encoding="utf-8")
        # Clean up stale units for features no longer requested
        # (makes `background install` idempotent across re-installs)
        for flag, unit in [
            (with_stack, STACK_UNIT),
            (with_letta, LETTA_UNIT),
            (with_openmemory, OPENMEMORY_UNIT),
        ]:
            if not flag:
                unit_path = SYSTEMD_USER_DIR / unit
                if unit_path.exists():
                    subprocess.run(
                        ["systemctl", "--user", "disable", "--now", unit],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    unit_path.unlink()
        zoekt_unit_path = SYSTEMD_USER_DIR / ZOEKT_UNIT
        if zoekt_unit_path.exists():
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", ZOEKT_UNIT],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            zoekt_unit_path.unlink()

        daemon_reload = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            capture_output=True,
            text=True,
        )
        if getattr(daemon_reload, "returncode", 0) != 0:
            output = _subprocess_output(daemon_reload)
            if _systemd_user_bus_unavailable(output):
                click.echo(
                    "Warning: systemd user bus is unavailable; unit files were installed "
                    "but services were not enabled or started. Run 'lc background install' "
                    "from a login session to activate them.",
                    err=True,
                )
                return
            raise click.ClickException(
                "systemctl --user daemon-reload failed" + (f": {output.strip()}" if output.strip() else "")
            )

        # Use enable + restart (not enable --now) so already-running services
        # pick up the new code after re-install. `restart` starts inactive units too.
        subprocess.run(["systemctl", "--user", "enable", CONTROLLER_UNIT], check=True)
        subprocess.run(["systemctl", "--user", "restart", CONTROLLER_UNIT], check=True)
        if with_stack:
            subprocess.run(["systemctl", "--user", "enable", STACK_UNIT], check=True)
            subprocess.run(["systemctl", "--user", "restart", STACK_UNIT], check=True)
        if with_letta:
            subprocess.run(["systemctl", "--user", "enable", LETTA_UNIT], check=True)
            subprocess.run(["systemctl", "--user", "restart", LETTA_UNIT], check=True)
        if with_openmemory:
            subprocess.run(["systemctl", "--user", "enable", OPENMEMORY_UNIT], check=True)
            subprocess.run(["systemctl", "--user", "restart", OPENMEMORY_UNIT], check=True)

    elif _is_macos():
        # Clean up stale plists for features no longer requested
        for flag, label in [
            (with_stack, STACK_LABEL),
            (with_letta, LETTA_LABEL),
            (with_openmemory, OPENMEMORY_LABEL),
        ]:
            if not flag:
                plist = LAUNCHD_USER_DIR / f"{label}.plist"
                if plist.exists():
                    subprocess.run(
                        ["launchctl", "unload", str(plist)],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    plist.unlink()
        zoekt_plist_path = LAUNCHD_USER_DIR / f"{ZOEKT_LABEL}.plist"
        if zoekt_plist_path.exists():
            subprocess.run(
                ["launchctl", "unload", str(zoekt_plist_path)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            zoekt_plist_path.unlink()

        LAUNCHD_USER_DIR.mkdir(parents=True, exist_ok=True)

        # XML-escape every value interpolated into the plist <string> blocks.
        # launchd plists are XML, so a path or label containing '&', '<' or
        # '>' (e.g. '~/R&D/proj') would otherwise corrupt the file and launchd
        # would refuse to load it. (The Linux systemd units above are INI, not
        # XML, and must NOT be escaped.)
        xb = _xml_escape(str(lemoncrow_bin))
        xroot = _xml_escape(str(root))
        xproject = _xml_escape(str(project_root))
        x_controller_label = _xml_escape(str(CONTROLLER_LABEL))
        x_stack_label = _xml_escape(str(STACK_LABEL))
        x_letta_label = _xml_escape(str(LETTA_LABEL))
        x_openmemory_label = _xml_escape(str(OPENMEMORY_LABEL))
        x_stack_log = _xml_escape(str(_stack_log_path(root)))
        x_letta_log = _xml_escape(str(Path(root) / "letta" / "letta.log"))
        x_openmemory_log = _xml_escape(str(_openmemory_log_path(root)))

        controller_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{x_controller_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{xb}</string>
        <string>--root</string>
        <string>{xroot}</string>
        <string>servicectl</string>
        <string>run</string>
        <string>--auto-update</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{xproject}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LEMONCROW_ROOT</key>
        <string>{xroot}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""
        (LAUNCHD_USER_DIR / f"{CONTROLLER_LABEL}.plist").write_text(controller_plist, encoding="utf-8")
        click.echo(f"Installed {CONTROLLER_LABEL}.plist")

        if with_stack:
            zoekt_env = (
                """
        <key>LEMONCROW_ZOEKT_MODE</key>
        <string>managed</string>"""
                if with_zoekt
                else ""
            )
            stack_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{x_stack_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{xb}</string>
        <string>--root</string>
        <string>{xroot}</string>
        <string>stack</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{xproject}</string>
    <key>StandardOutPath</key>
    <string>{x_stack_log}</string>
    <key>StandardErrorPath</key>
    <string>{x_stack_log}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LEMONCROW_ROOT</key>
        <string>{xroot}</string>
        <key>LEMONCROW_STACK_ROOT</key>
        <string>{xroot}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
{zoekt_env}
    </dict>
</dict>
</plist>
"""
            (LAUNCHD_USER_DIR / f"{STACK_LABEL}.plist").write_text(stack_plist, encoding="utf-8")
            click.echo(f"Installed {STACK_LABEL}.plist")

        if with_letta:
            letta_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{x_letta_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{xb}</string>
        <string>--root</string>
        <string>{xroot}</string>
        <string>letta</string>
        <string>up</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>{xproject}</string>
    <key>StandardOutPath</key>
    <string>{x_letta_log}</string>
    <key>StandardErrorPath</key>
    <string>{x_letta_log}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LEMONCROW_ROOT</key>
        <string>{xroot}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""
            (LAUNCHD_USER_DIR / f"{LETTA_LABEL}.plist").write_text(letta_plist, encoding="utf-8")
            click.echo(f"Installed {LETTA_LABEL}.plist")

        if with_openmemory:
            # XML-escape every interpolated value before it lands in the plist.
            # The OpenAI key especially can contain XML-special characters; an
            # unescaped '&', '<' or '>' would corrupt the plist (launchd would
            # refuse to load it) or allow markup injection into the file.
            openmemory_api_key = _xml_escape(
                os.environ.get("LEMONCROW_OPENMEMORY_OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
            )
            openmemory_user_id = _xml_escape(
                os.environ.get("LEMONCROW_OPENMEMORY_USER_ID", os.environ.get("USER", "lemoncrow"))
            )
            openmemory_url = _xml_escape(os.environ.get("LEMONCROW_OPENMEMORY_URL", "http://127.0.0.1:8765"))
            openmemory_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{x_openmemory_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{xb}</string>
        <string>--root</string>
        <string>{xroot}</string>
        <string>openmemory</string>
        <string>up</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>{xproject}</string>
    <key>StandardOutPath</key>
    <string>{x_openmemory_log}</string>
    <key>StandardErrorPath</key>
    <string>{x_openmemory_log}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OPENAI_API_KEY</key>
        <string>{openmemory_api_key}</string>
        <key>LEMONCROW_OPENMEMORY_USER_ID</key>
        <string>{openmemory_user_id}</string>
        <key>LEMONCROW_OPENMEMORY_URL</key>
        <string>{openmemory_url}</string>
    </dict>
</dict>
</plist>
"""
            (LAUNCHD_USER_DIR / f"{OPENMEMORY_LABEL}.plist").write_text(openmemory_plist, encoding="utf-8")
            click.echo(f"Installed {OPENMEMORY_LABEL}.plist")

        subprocess.run(["launchctl", "load", str(LAUNCHD_USER_DIR / f"{CONTROLLER_LABEL}.plist")], check=False)
        if with_stack:
            subprocess.run(["launchctl", "load", str(LAUNCHD_USER_DIR / f"{STACK_LABEL}.plist")], check=False)
        if with_letta:
            subprocess.run(["launchctl", "load", str(LAUNCHD_USER_DIR / f"{LETTA_LABEL}.plist")], check=False)
        if with_openmemory:
            subprocess.run(
                ["launchctl", "load", str(LAUNCHD_USER_DIR / f"{OPENMEMORY_LABEL}.plist")],
                check=False,
            )

    else:
        raise click.ClickException(f"Unsupported platform for background services: {sys.platform}")

    click.echo("Services enabled and started.")


@background_group.command("uninstall")
@click.pass_context
def background_uninstall(ctx: click.Context) -> None:
    """Stop and remove LemonCrow background units."""
    if _is_linux():
        for unit in [CONTROLLER_UNIT, STACK_UNIT, LETTA_UNIT, OPENMEMORY_UNIT, ZOEKT_UNIT]:
            path = SYSTEMD_USER_DIR / unit
            if path.exists():
                subprocess.run(["systemctl", "--user", "disable", "--now", unit], check=False)
                path.unlink()
                click.echo(f"Removed {unit}")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)

    elif _is_macos():
        for label in [CONTROLLER_LABEL, STACK_LABEL, LETTA_LABEL, OPENMEMORY_LABEL, ZOEKT_LABEL]:
            plist = LAUNCHD_USER_DIR / f"{label}.plist"
            if plist.exists():
                subprocess.run(["launchctl", "unload", str(plist)], check=False)
                plist.unlink()
                click.echo(f"Removed {label}")
    else:
        raise click.ClickException(f"Unsupported platform: {sys.platform}")

    click.echo("Uninstallation complete.")


@background_group.command("status")
@click.pass_context
def background_status(ctx: click.Context) -> None:
    """Show status of LemonCrow background units."""
    if _is_linux():
        units = [CONTROLLER_UNIT]
        if (SYSTEMD_USER_DIR / STACK_UNIT).exists():
            units.append(STACK_UNIT)
        if (SYSTEMD_USER_DIR / LETTA_UNIT).exists():
            units.append(LETTA_UNIT)
        if (SYSTEMD_USER_DIR / OPENMEMORY_UNIT).exists():
            units.append(OPENMEMORY_UNIT)
        for unit in units:
            click.echo(f"--- {unit} ---")
            subprocess.run(["systemctl", "--user", "status", unit, "--no-pager"], check=False)
            click.echo("")
    elif _is_macos():
        for label in [CONTROLLER_LABEL, STACK_LABEL, LETTA_LABEL, OPENMEMORY_LABEL]:
            if (LAUNCHD_USER_DIR / f"{label}.plist").exists():
                click.echo(f"--- {label} ---")
                subprocess.run(["launchctl", "list", label], check=False)
                click.echo("")
    else:
        click.echo(f"Background services not supported on {sys.platform}")


@background_group.command("restart")
@click.pass_context
def background_restart(ctx: click.Context) -> None:
    """Restart LemonCrow background units."""
    if _is_linux():
        units = [CONTROLLER_UNIT]
        if (SYSTEMD_USER_DIR / STACK_UNIT).exists():
            units.append(STACK_UNIT)
        if (SYSTEMD_USER_DIR / LETTA_UNIT).exists():
            units.append(LETTA_UNIT)
        if (SYSTEMD_USER_DIR / OPENMEMORY_UNIT).exists():
            units.append(OPENMEMORY_UNIT)
        for unit in units:
            subprocess.run(["systemctl", "--user", "restart", unit], check=True)
            click.echo(f"Restarted {unit}")
    elif _is_macos():
        uid = os.getuid()
        for label in [CONTROLLER_LABEL, STACK_LABEL, LETTA_LABEL, OPENMEMORY_LABEL]:
            if (LAUNCHD_USER_DIR / f"{label}.plist").exists():
                subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], check=False)
                click.echo(f"Restarted {label}")
    else:
        click.echo(f"Background services not supported on {sys.platform}")


# --------------------------------------------------------------------------- #
# Alias 'systemd' to 'background' for backward compatibility                  #
# --------------------------------------------------------------------------- #


@click.group("systemd", hidden=True)
def systemd_alias_group() -> None:
    """Alias for 'background' group."""


@systemd_alias_group.command("install")
@click.option("--with-stack", is_flag=True)
@click.option("--with-letta", is_flag=True)
@click.option("--with-openmemory", is_flag=True)
@click.option("--with-zoekt", is_flag=True)
@click.pass_context
def systemd_install_alias(
    ctx: click.Context,
    with_stack: bool,
    with_letta: bool,
    with_openmemory: bool,
    with_zoekt: bool,
) -> None:
    ctx.invoke(
        background_install,
        with_stack=with_stack,
        with_letta=with_letta,
        with_openmemory=with_openmemory,
        with_zoekt=with_zoekt,
    )


@systemd_alias_group.command("uninstall")
@click.pass_context
def systemd_uninstall_alias(ctx: click.Context) -> None:
    ctx.invoke(background_uninstall)


@systemd_alias_group.command("status")
@click.pass_context
def systemd_status_alias(ctx: click.Context) -> None:
    ctx.invoke(background_status)


@systemd_alias_group.command("restart")
@click.pass_context
def systemd_restart_alias(ctx: click.Context) -> None:
    ctx.invoke(background_restart)


# --------------------------------------------------------------------------- #
# Unified logs command                                                        #
# --------------------------------------------------------------------------- #
