"""`lc status` — one product-level orientation screen."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import click

from lemoncrow import __version__
from lemoncrow.gateway.cli import style
from lemoncrow.gateway.cli.commands.runtime import integration_rows


def _review_installed(root: Path) -> bool:
    return (
        any(
            path.exists()
            for path in (
                root / "review" / "client" / "bin" / "lcr",
                root / "review" / "engine" / "bin" / "python",
                root / "review" / "env",
            )
        )
        or shutil.which("lcr") is not None
    )


def _product_config(product: str) -> dict[str, Any]:
    try:
        from lemoncrow_client.config import load_config

        config = load_config(product=product)
        return {
            "deployment": config.install_mode,
            "endpoint": config.url,
            "authenticated": config.authenticated,
        }
    except Exception:
        return {"deployment": "unknown", "endpoint": "", "authenticated": False}


def _runtime_integrations() -> list[dict[str, Any]]:
    try:
        from lemoncrow.gateway.cli.commands.runtime import SUPPORTED_RUNTIME_HOSTS, query_integrations

        return query_integrations(SUPPORTED_RUNTIME_HOSTS, None)
    except Exception:
        return []


@click.command("status", short_help="What is installed, signed in and enabled.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable product status.")
@click.pass_context
def product_status_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show installed products, deployment, sign-in, and Workspaces integrations."""

    root = Path(ctx.obj["root"])
    review_installed = _review_installed(root)
    review = _product_config("review") if review_installed else {}
    runtime = _product_config("runtime")
    integrations = _runtime_integrations()
    payload = {
        "version": __version__,
        "review": {
            "installed": review_installed,
            "deployment": review.get("deployment"),
            "endpoint": review.get("endpoint"),
            "authenticated": review.get("authenticated") if review_installed else None,
        },
        "runtime": {
            "installed": True,
            "deployment": runtime.get("deployment"),
            "endpoint": runtime.get("endpoint"),
            "full_cli": True,
            "integrations": integrations,
        },
    }
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
        return

    width = 12
    lines = [style.title(f"LemonCrow {__version__}"), "", f"  {style.heading('Review')}"]
    if not review_installed:
        lines.append(style.row("off", "Installed", "no", width=width, indent=4))
        lines.append(style.next_step("Add it with", "install.sh --product review", indent=4))
    else:
        deployment = str(review.get("deployment") or "unknown")
        lines.append(style.row("ok", "Installed", style.dim(f"{deployment} deployment"), width=width, indent=4))
        if deployment == "hosted":
            signed_in = bool(review.get("authenticated"))
            lines.append(
                style.row(
                    "ok" if signed_in else "warn", "Signed in", "yes" if signed_in else "no", width=width, indent=4
                )
            )
            if not signed_in:
                lines.append(style.next_step("Sign in with", "lc auth login", indent=4))
        lines.append(style.next_step("Review the current change with", "lcr", indent=4))

    lines += ["", f"  {style.heading('Workspaces')} {style.dim('· coding agents')}"]
    lines += integration_rows(integrations, width=width, indent=4)
    lines.append(style.next_step("Manage coding agents with", "lc rt status", indent=4))
    for line in lines:
        click.echo(line)


__all__ = ["product_status_cmd"]
