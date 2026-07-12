"""``lemoncrowd``/``lcd`` — LemonCrow HTTP service daemon CLI logic."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click

from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.infra.runtime.daemon_units import (
    DEFAULT_STACK_FRONTEND_PORT,
    DEFAULT_STACK_SERVICE_PORT,
    STACK_UNIT,
    SYSTEMD_USER_DIR,
)

# ── helpers ────────────────────────────────────────────────────────────────────


def _service_unit() -> str:
    return os.environ.get("LEMONCROWD_UNIT", STACK_UNIT)


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def _run_systemctl(*args: str) -> int:
    return subprocess.call(["systemctl", "--user", *args])


def _launchctl_available() -> bool:
    return sys.platform == "darwin" and shutil.which("launchctl") is not None


# ── CLI group ─────────────────────────────────────────────────────────────────


@click.group(context_settings={"help_option_names": ["-h", "--help"]}, invoke_without_command=True)
@click.version_option(prog_name="lemoncrowd")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """LemonCrow service daemon — manage the LemonCrow HTTP service.

    Run with no arguments to show service status (same as ``lemoncrowd status``).
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(status)


@cli.command()
@click.option("--host", default=None, help="Bind host (default: LEMONCROW_SERVICE_HOST or 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Bind port (default: LEMONCROW_SERVICE_PORT or 8787)")
@click.option("--reload", is_flag=True, default=False, help="Enable uvicorn hot-reload (dev)")
def start(host: str | None, port: int | None, reload: bool) -> None:
    """Start the LemonCrow HTTP service in the foreground."""
    if host:
        os.environ["LEMONCROW_SERVICE_HOST"] = host
    if port:
        os.environ["LEMONCROW_SERVICE_PORT"] = str(port)
    from lemoncrow.core.service.api import main

    main(host=host, port=port, reload=reload)


@cli.command()
def stop() -> None:
    """Stop the LemonCrow HTTP service."""
    if _systemctl_available():
        ret = _run_systemctl("stop", _service_unit())
        sys.exit(ret)
    # Fallback: find and kill the service process
    import signal

    killed = 0
    pid_file = default_store_root() / "stack" / "service.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            killed += 1
            click.echo(f"Sent SIGTERM to PID {pid}")
        except (ValueError, ProcessLookupError, OSError):
            pass
    if not killed:
        click.echo("No running lemoncrowd process found (try: systemctl --user stop lemoncrow-stack)", err=True)
        sys.exit(1)


@cli.command()
def restart() -> None:
    """Restart the LemonCrow HTTP service."""
    if _systemctl_available():
        ret = _run_systemctl("restart", _service_unit())
        sys.exit(ret)
    click.echo("systemctl not available; use 'lcd stop && lcd start'", err=True)
    sys.exit(1)


@cli.command()
def status() -> None:
    """Show running status of the LemonCrow HTTP service."""
    import urllib.request

    root_url = f"http://127.0.0.1:{os.environ.get('LEMONCROW_SERVICE_PORT', str(DEFAULT_STACK_SERVICE_PORT))}"
    try:
        with urllib.request.urlopen(f"{root_url}/health", timeout=2) as resp:
            data = resp.read().decode()
        click.echo(f"● lcd  running  {root_url}  {data.strip()}")
    except Exception:  # noqa: BLE001
        click.echo(f"● lcd  stopped  (not reachable at {root_url}/health)")

    if _systemctl_available():
        click.echo("")
        _run_systemctl("status", _service_unit(), "--no-pager")


@cli.command()
@click.option("--follow/--no-follow", "-f", default=True, help="Follow log output")
@click.option("--lines", "-n", default=50, help="Number of recent lines to show")
def logs(follow: bool, lines: int) -> None:
    """Show LemonCrow HTTP service logs."""
    if _systemctl_available():
        args = ["journalctl", "--user", "-u", _service_unit(), f"-n{lines}"]
        if follow:
            args.append("-f")
        os.execlp("journalctl", *args)
    # Fallback: try the log file
    log_path = default_store_root() / "service.log"
    if log_path.exists():
        if follow:
            os.execlp("tail", "tail", "-f", "-n", str(lines), str(log_path))
        else:
            subprocess.run(["tail", "-n", str(lines), str(log_path)])
    else:
        click.echo("No log file found. Use systemd: journalctl --user -u lemoncrow-stack", err=True)


@cli.command()
@click.option(
    "--enable/--no-enable", default=True, show_default=True, help="Enable and start the unit immediately after install"
)
def install(enable: bool) -> None:
    """Install systemd unit for the LemonCrow HTTP service."""
    if not _systemctl_available():
        click.echo("systemctl not available. On macOS use 'lcd install --launchd'.", err=True)
        sys.exit(1)

    # Note: when running via 'lc background service install', sys.argv[0] is 'lc'
    # but we want the service to point to 'lc background service start'
    lemoncrow_bin = shutil.which("lc") or sys.argv[0]
    service_start_cmd = f"{lemoncrow_bin} background service start"

    project_root = os.getcwd()
    root = default_store_root()
    unit_dir = SYSTEMD_USER_DIR
    unit_dir.mkdir(parents=True, exist_ok=True)

    unit_content = f"""[Unit]
Description=LemonCrow HTTP Service
After=network.target

[Service]
Type=simple
WorkingDirectory={project_root}
ExecStart={service_start_cmd}
Restart=on-failure
RestartSec=5
Environment=LEMONCROW_ROOT={root}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
    unit_path = unit_dir / _service_unit()
    unit_path.write_text(unit_content, encoding="utf-8")
    click.echo(f"Installed {unit_path}")

    _run_systemctl("daemon-reload")
    if enable:
        _run_systemctl("enable", "--now", _service_unit())
        click.echo(f"Enabled and started {_service_unit()}")


@cli.command()
@click.option("--stop/--no-stop", default=True, help="Stop the service before uninstalling")
def uninstall(stop: bool) -> None:
    """Remove the systemd unit for the LemonCrow HTTP service."""
    if not _systemctl_available():
        click.echo("systemctl not available.", err=True)
        sys.exit(1)
    if stop:
        _run_systemctl("stop", _service_unit())
    _run_systemctl("disable", _service_unit())
    unit_path = SYSTEMD_USER_DIR / _service_unit()
    if unit_path.exists():
        unit_path.unlink()
        click.echo(f"Removed {unit_path}")
    _run_systemctl("daemon-reload")
    click.echo("Uninstalled.")


# ── frontend commands ─────────────────────────────────────────────────────────

_FRONTEND_UNIT = "lemoncrow-frontend.service"


def _frontend_dir() -> Path:
    """Return the frontend Vite source directory."""
    # 1. Explicit env var (cleanest override)
    env_dir = os.environ.get("LEMONCROW_FRONTEND_DIR")
    if env_dir:
        return Path(env_dir)
    # 2. cwd is the frontend dir (when systemd WorkingDirectory is set)
    cwd = Path.cwd()
    if (cwd / "package.json").exists() and (cwd / "node_modules" / ".bin" / "vite").exists():
        return cwd
    # 3. Look relative to the installed package source
    import importlib.util

    spec = importlib.util.find_spec("lemoncrow")
    if spec and spec.origin:
        src_root = Path(spec.origin).parents[3]
        candidate = src_root / "frontend"
        if (candidate / "package.json").exists():
            return candidate
    return Path.cwd() / "frontend"


@cli.command("frontend-start")
@click.option("--host", default="localhost", show_default=True)
@click.option("--port", default=DEFAULT_STACK_FRONTEND_PORT, show_default=True, type=int)
@click.option("--api-url", default=None, help="LemonCrow service URL for VITE_API_URL (default: http://localhost:8787)")
def frontend_start(host: str, port: int, api_url: str | None) -> None:
    """Start the LemonCrow visualization frontend (Vite dev server)."""
    fdir = _frontend_dir()
    if not fdir.exists():
        click.echo(f"Frontend directory not found: {fdir}", err=True)
        sys.exit(1)
    # Prefer locally installed vite binary to avoid npm exec download overhead
    vite_bin = fdir / "node_modules" / ".bin" / "vite"
    if not vite_bin.exists():
        # Install node_modules first
        subprocess.run(["npm", "ci"], cwd=str(fdir), check=True)
    env = os.environ.copy()
    env["VITE_API_URL"] = api_url or os.environ.get("LEMONCROW_SERVICE_URL", "http://localhost:8787")
    os.execlpe(str(vite_bin), "vite", "--host", host, "--port", str(port), env)


@cli.command("frontend-install")
@click.option("--enable/--no-enable", default=True, help="Enable and start the unit immediately")
def frontend_install(enable: bool) -> None:
    """Install systemd unit for the LemonCrow visualization frontend."""
    if not _systemctl_available():
        click.echo("systemctl not available.", err=True)
        sys.exit(1)
    fdir = _frontend_dir()
    if not fdir.exists():
        click.echo(f"Frontend directory not found: {fdir}", err=True)
        sys.exit(1)

    # We use 'lc background service' as the proxy for the daemon logic
    lemoncrow_bin = shutil.which("lc") or sys.argv[0]
    service_start_cmd = f"{lemoncrow_bin} background service"

    unit_dir = SYSTEMD_USER_DIR
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_content = f"""[Unit]
Description=LemonCrow Visualization Frontend
After={STACK_UNIT}

[Service]
Type=simple
WorkingDirectory={fdir}
ExecStart={service_start_cmd} frontend-start
Restart=on-failure
RestartSec=5
Environment=LEMONCROW_ROOT={default_store_root()}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
    unit_path = unit_dir / _FRONTEND_UNIT
    unit_path.write_text(unit_content, encoding="utf-8")
    click.echo(f"Installed {unit_path}")
    _run_systemctl("daemon-reload")
    if enable:
        _run_systemctl("enable", "--now", _FRONTEND_UNIT)
        click.echo(f"Enabled and started {_FRONTEND_UNIT}")


def main() -> None:
    """Entry point for the ``lemoncrowd``/``lcd`` console scripts."""
    cli()
