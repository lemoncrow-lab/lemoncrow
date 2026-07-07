from __future__ import annotations

import json
from pathlib import Path

import click

from atelier.core.capabilities.workflow_defaults import bootstrap_default_definitions


@click.group("defaults")
def defaults_group() -> None:
    """Inspect and bootstrap canonical default definitions."""


@defaults_group.command("bootstrap")
@click.option(
    "--target-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    show_default=True,
    help="Write packaged default definitions under TARGET_ROOT/defaults/.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the bootstrap receipt as JSON.")
def defaults_bootstrap_cmd(target_root: Path, as_json: bool) -> None:
    """Write missing packaged defaults without overwriting local changes."""
    receipt = bootstrap_default_definitions(target_root.resolve(), repo_root=Path.cwd().resolve())
    entries: list[dict[str, str]] = [
        {
            "path": str(entry.path),
            "status": entry.status,
            "kind": entry.kind,
        }
        for entry in receipt.entries
    ]
    payload = {
        "target_root": str(target_root.resolve()),
        "entries": entries,
    }
    if as_json:
        click.echo(json.dumps(payload))
        return

    for entry in entries:
        click.echo(f"{entry['status']}\t{entry['kind']}\t{entry['path']}")


__all__ = ["defaults_bootstrap_cmd", "defaults_group"]
