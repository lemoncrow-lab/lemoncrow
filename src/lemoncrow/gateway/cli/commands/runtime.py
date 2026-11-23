"""Canonical LemonCrow Runtime product namespace.

Frequent user jobs (code, run, session, context) are intentionally promoted to
the root CLI as ergonomic first-class commands while remaining available here
by identity. Product-management and advanced Runtime machinery stay namespaced.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import click

from lemoncrow import __version__
from lemoncrow.gateway.cli import style

SUPPORTED_RUNTIME_HOSTS = ("claude", "codex", "opencode")


class RuntimeProductGroup(click.Group):
    """Group Runtime help by user intent rather than implementation module."""

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        groups: dict[str, list[tuple[str, str]]] = {
            "Get started": [],
            "Explore": [],
            "Work": [],
            "Customize": [],
            "Advanced": [],
        }
        work = {"code", "run", "session", "context"}
        explore = {"map", "dashboard", "savings"}
        customize = {"model", "agent", "skill", "swarm"}
        start = {"status", "enable", "disable", "index"}
        visible = [
            (name, command)
            for name in self.list_commands(ctx)
            if (command := self.get_command(ctx, name)) is not None and not command.hidden
        ]
        for row in style.short_help_rows(formatter, visible):
            name = row[0]
            if name in start:
                groups["Get started"].append(row)
            elif name in explore:
                groups["Explore"].append(row)
            elif name in work:
                groups["Work"].append(row)
            elif name in customize:
                groups["Customize"].append(row)
            else:
                groups["Advanced"].append(row)
        for title, rows in groups.items():
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)


def _repo_root() -> Path:
    from lemoncrow.gateway.cli.commands.admin import _project_root

    return _project_root()


def _host_binary(host: str) -> str | None:
    return shutil.which(host)


def _integration_mode(output: str, *, enabled: bool) -> str:
    if not enabled:
        return "unknown"
    lowered = output.lower()
    if "lite-mcp" in lowered:
        return "local_lite"
    if re.search(r"(?:^|[\s\"'/])mcp(?:[\s\"'/]|$)", lowered):
        return "hosted"
    return "unknown"


def _query_integration(host: str, workspace: Path | None) -> dict[str, Any]:
    binary = _host_binary(host)
    if binary is None:
        return {
            "host": host,
            "installed": False,
            "enabled": False,
            "mode": "unknown",
            "detail": "host CLI not found",
        }

    cwd = str(workspace) if workspace is not None else None
    if host == "claude":
        argv = [binary, "mcp", "get", "lc"]
    elif host == "codex":
        argv = [binary, "mcp", "get", "lc", "--json"]
    elif host == "opencode":
        argv = [binary, "mcp", "list"]
    else:
        raise ValueError(host)

    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except OSError as exc:
        return {
            "host": host,
            "installed": True,
            "enabled": False,
            "mode": "unknown",
            "detail": str(exc),
        }

    if host == "opencode":
        output = f"{completed.stdout}\n{completed.stderr}"
        enabled = completed.returncode == 0 and re.search(r"(?im)(?:^|\s)lc(?:\s|$)", output) is not None
    else:
        enabled = completed.returncode == 0

    output = f"{completed.stdout}\n{completed.stderr}"
    mode = _integration_mode(output, enabled=enabled)
    detail = (
        "" if enabled else (completed.stderr or completed.stdout or "Workspaces integration not configured").strip()
    )
    return {
        "host": host,
        "installed": True,
        "enabled": enabled,
        "mode": mode,
        "detail": detail[:512],
    }


def query_integrations(hosts: tuple[str, ...], workspace: Path | None) -> list[dict[str, Any]]:
    """Ask every host CLI at once: each probe takes about a second, so in sequence they stall the screen."""
    from concurrent.futures import ThreadPoolExecutor

    with style.working("Checking your coding agents…"), ThreadPoolExecutor(max_workers=len(hosts) or 1) as pool:
        return list(pool.map(lambda host: _query_integration(host, workspace), hosts))


def _run_host_script(
    action: str,
    host: str,
    *,
    workspace: Path | None,
    dry_run: bool,
    hosted: bool = False,
) -> None:
    script_name = f"{'install' if action == 'enable' else 'uninstall'}_{host}.sh"
    script = _repo_root() / "scripts" / script_name
    if not script.is_file():
        raise click.ClickException(f"Workspaces {action} script not found: {script}")

    argv = ["bash", str(script)]
    if dry_run:
        argv.append("--dry-run")
    if workspace is not None:
        argv.extend(["--workspace", str(workspace.resolve())])
    if action == "enable":
        argv.append("--strict")
        if hosted:
            argv.append("--hosted")

    try:
        subprocess.run(argv, check=True)
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"Workspaces {action} for {host} failed with exit code {exc.returncode}") from exc
    if dry_run:
        return
    label = _HOST_LABELS.get(host, host)
    click.echo("")
    if action == "enable":
        mode = "Hosted" if hosted else "Lite"
        click.echo(style.success(f"Workspaces enabled for {style.bold(label)} {style.dim('· ' + mode)}"))
        click.echo(style.next_step(f"Restart {label} to load the lc tools, then check with", "lc rt status"))
    else:
        click.echo(style.success(f"Workspaces removed from {style.bold(label)}"))


@click.group("runtime", cls=RuntimeProductGroup)
def runtime_group() -> None:
    """Use LemonCrow tools in coding agents and manage Workspaces."""


@runtime_group.command("status", short_help="Which coding agents have LemonCrow's tools.")
@click.option("--host", type=click.Choice(SUPPORTED_RUNTIME_HOSTS), default=None, help="Show one host only.")
@click.option("--workspace", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable status.")
def runtime_status_cmd(host: str | None, workspace: Path | None, as_json: bool) -> None:
    """Show Workspaces installation and host-integration state."""

    hosts = (host,) if host else SUPPORTED_RUNTIME_HOSTS
    integrations = query_integrations(hosts, workspace)
    payload = {
        "product": "runtime",
        "installed": True,
        "version": __version__,
        "workspace": str(workspace.resolve()) if workspace else None,
        "integrations": integrations,
    }
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
        return

    click.echo(style.title(f"LemonCrow Workspaces {__version__}"))
    click.echo("")
    for line in integration_rows(integrations, width=10):
        click.echo(line)
    idle = [item["host"] for item in integrations if item["installed"] and not item["enabled"]]
    if idle:
        click.echo("")
        click.echo(style.next_step("Give an agent LemonCrow's tools:", f"lc rt enable {idle[0]}"))
    elif not any(item["installed"] for item in integrations):
        click.echo("")
        click.echo(style.next_step("Install Claude Code, Codex or OpenCode, then run", "lc rt enable <host>"))


_HOST_LABELS = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}
_MODE_LABELS = {"local_lite": "Lite", "hosted": "Hosted", "unknown": "Unknown mode"}


def integration_rows(integrations: list[dict[str, Any]], *, width: int, indent: int = 2) -> list[str]:
    """One ``✓ host  state`` row per coding agent, shared by ``lc status`` and ``lc rt status``."""
    rows = []
    for item in integrations:
        host = str(item.get("host", ""))
        if not item.get("installed"):
            state: style.State = "off"
            text = style.dim("host not installed")
        elif not item.get("enabled"):
            state, text = "off", "not enabled"
        else:
            mode = str(item.get("mode") or "unknown")
            if "mode" not in item:
                state, text = "ok", "enabled"
            else:
                state = "warn" if mode == "unknown" else "ok"
                text = f"enabled · {_MODE_LABELS.get(mode, mode)}"
        rows.append(style.row(state, host, text, width=width, indent=indent))
    return rows


def _host_action_options(command: Any) -> Any:
    command = click.option("--dry-run", is_flag=True, help="Show changes without modifying host configuration.")(
        command
    )
    command = click.option(
        "--workspace",
        type=click.Path(path_type=Path, file_okay=False),
        default=None,
        help="Change only this project's config instead of your user-wide config.",
    )(command)
    command = click.argument("host", type=click.Choice(SUPPORTED_RUNTIME_HOSTS), metavar="HOST")(command)
    return command


@runtime_group.command("enable", short_help="Give a coding agent LemonCrow's tools.")
@click.option(
    "--hosted",
    is_flag=True,
    help="Explicitly register hosted retrieval (the thin client) instead of Workspaces Lite.",
)
@_host_action_options
def runtime_enable_cmd(host: str, workspace: Path | None, dry_run: bool, hosted: bool) -> None:
    """Enable Workspaces for HOST. Workspaces Lite is the default.

    HOST is claude, codex or opencode.
    """

    _run_host_script("enable", host, workspace=workspace, dry_run=dry_run, hosted=hosted)


@runtime_group.command("disable", short_help="Remove LemonCrow's tools from a coding agent.")
@_host_action_options
def runtime_disable_cmd(host: str, workspace: Path | None, dry_run: bool) -> None:
    """Remove LemonCrow Workspaces integration from HOST without touching Review.

    HOST is claude, codex or opencode.
    """

    _run_host_script("disable", host, workspace=workspace, dry_run=dry_run)


@runtime_group.command("lite-mcp", hidden=True)
@click.option("--host", default=None, help="Calling host identifier for attribution.")
def runtime_lite_mcp_cmd(host: str | None) -> None:
    """Internal Workspaces Lite stdio entrypoint used by local Workspaces integrations."""

    if host:
        import os

        os.environ["LEMONCROW_AGENT"] = host
    try:
        from lemoncrow_client.lite.mcp import serve
    except ImportError as exc:
        raise click.ClickException(
            "Workspaces Lite is unavailable in this installation; reinstall the Workspaces product"
        ) from exc
    raise SystemExit(serve())
