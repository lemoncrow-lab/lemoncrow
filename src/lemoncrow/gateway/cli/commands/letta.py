"""Thin ``lemon letta`` command group.

The Letta sidecar lifecycle (``docker compose`` orchestration) lives in
``gateway/integrations/openmemory_lifecycle.py``. These callbacks are thin Click
wrappers that lazily import the lifecycle service and translate infrastructure
errors into ``click.ClickException`` (PATTERNS: lazy heavy imports; infra raises
``RuntimeError``, the CLI converts).

The group is defined as a standalone ``click.Group`` (not decorated against the
global ``cli``) so it can be ``add_command``-ed from ``commands/__init__.py``
without an import cycle (RESEARCH Pattern 1).
"""

from __future__ import annotations

import os
import urllib.request

import click


@click.group("letta")
def letta_group() -> None:
    """Manage the self-hosted Letta sidecar."""


@letta_group.command("up")
def letta_up() -> None:
    """Start the Letta memory server Docker Compose stack."""
    from lemoncrow.gateway.integrations.openmemory_lifecycle import run_compose

    run_compose(["up", "-d"])


@letta_group.command("down")
def letta_down() -> None:
    """Stop the Letta Docker Compose stack while preserving volumes."""
    from lemoncrow.gateway.integrations.openmemory_lifecycle import run_compose

    run_compose(["down"])


@letta_group.command("status")
def letta_status() -> None:
    """Print Letta health status."""
    url = os.environ.get("LEMONCROW_LETTA_URL", "http://localhost:8283").rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/v1/health", timeout=5) as response:
            body = response.read().decode("utf-8", errors="replace")
        click.echo(f"healthy\t{url}\t{body}")
    except Exception as exc:
        raise click.ClickException(f"Letta is not healthy at {url}: {exc}") from exc


@letta_group.command("reset")
@click.option("--yes", is_flag=True, help="Confirm destructive volume removal.")
def letta_reset(yes: bool) -> None:
    """Remove the Letta container and persistent volume."""
    from lemoncrow.gateway.integrations.openmemory_lifecycle import run_compose

    if not yes:
        raise click.ClickException("refusing to reset Letta data without --yes")
    run_compose(["down", "-v"])
