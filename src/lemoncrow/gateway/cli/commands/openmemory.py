"""Thin ``lemon openmemory`` command group.

The OpenMemory checkout / env-file generation / ``docker compose`` / ``make``
lifecycle lives in ``gateway/integrations/openmemory_lifecycle.py`` (QBL-CLI-03).
These callbacks are thin Click wrappers that keep the ``shutil.which`` preflight,
``ctx.obj["root"]`` access, ``click.echo`` output, and ``click.ClickException``
conversion exactly as before, while delegating all business logic to the
lifecycle service (lazily imported inside each callback per PATTERNS).

The group is a standalone ``click.Group`` (not decorated against the global
``cli``) so ``commands/__init__.py`` can ``add_command`` it without an import
cycle (RESEARCH Pattern 1).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import click


@click.group("openmemory")
def openmemory_group() -> None:
    """Manage the self-hosted OpenMemory sidecar."""


@openmemory_group.command("up")
@click.pass_context
def openmemory_up(ctx: click.Context) -> None:
    """Clone/update OpenMemory and start its local MCP stack."""
    from lemoncrow.gateway.integrations import openmemory_lifecycle as lifecycle

    root = ctx.obj["root"]
    missing = [name for name in ("git", "docker", "make") if not shutil.which(name)]
    if missing:
        raise click.ClickException(f"OpenMemory requires: {', '.join(missing)}")
    if not os.environ.get("LEMONCROW_OPENMEMORY_OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", "")).strip():
        raise click.ClickException("OPENAI_API_KEY or LEMONCROW_OPENMEMORY_OPENAI_API_KEY must be set for OpenMemory")
    try:
        lifecycle.ensure_checkout(root)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    lifecycle.ensure_service_env(root)
    lifecycle.write_env_files(root)
    lifecycle.run_make(root, "build")
    lifecycle.run_make(root, "up")
    click.echo(f"OpenMemory started at {os.environ.get('LEMONCROW_OPENMEMORY_URL', 'http://127.0.0.1:8765')}")


@openmemory_group.command("down")
@click.pass_context
def openmemory_down(ctx: click.Context) -> None:
    """Stop the local OpenMemory stack while preserving the checkout."""
    from lemoncrow.gateway.integrations import openmemory_lifecycle as lifecycle

    root = ctx.obj["root"]
    workdir = lifecycle.openmemory_workdir(root)
    if not workdir.exists():
        raise click.ClickException("OpenMemory checkout not found")
    lifecycle.run_make(root, "down")
    click.echo("OpenMemory stopped.")


@openmemory_group.command("status")
@click.pass_context
def openmemory_status(ctx: click.Context) -> None:
    """Show the local OpenMemory service status."""
    from lemoncrow.gateway.integrations import openmemory_lifecycle as lifecycle

    root = ctx.obj["root"]
    workdir = lifecycle.openmemory_workdir(root)
    if not workdir.exists():
        raise click.ClickException("OpenMemory checkout not found")
    subprocess.run(["docker", "compose", "ps"], cwd=workdir, check=False)


@openmemory_group.command("logs")
@click.option("-f", "--follow", is_flag=True, help="Follow the logs.")
@click.pass_context
def openmemory_logs(ctx: click.Context, follow: bool) -> None:
    """Show OpenMemory Docker Compose logs."""
    from lemoncrow.gateway.integrations import openmemory_lifecycle as lifecycle

    root = ctx.obj["root"]
    workdir = lifecycle.openmemory_workdir(root)
    if not workdir.exists():
        raise click.ClickException("OpenMemory checkout not found")
    cmd = ["docker", "compose", "logs"]
    if follow:
        cmd.append("-f")
    subprocess.run(cmd, cwd=workdir, check=False)
