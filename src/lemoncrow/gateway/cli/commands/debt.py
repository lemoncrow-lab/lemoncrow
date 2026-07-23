"""``lc debt`` — harvest deferred-simplification markers into a ledger.

The coding guidelines tell agents to mark a deliberately cut corner with an
``lc-debt: <ceiling>; <upgrade path>`` comment. This command greps tracked files
for those markers so "later" doesn't become "never", and flags any marker that
names no upgrade path (``no-trigger``) — those are the ones that silently rot.
Pure ``git grep`` + render — no store, no state, no reimplemented file walk (git
already respects ``.gitignore``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click

from lemoncrow.gateway.cli.commands._shared import _emit

_MARKER = "lc-debt:"
# Require a comment leader before the marker so prose/backtick mentions of
# ``lc-debt:`` (docs, this file's own string) are not harvested as real debt.
_PATTERN = r"(#|//|/\*|<!--|--)[[:space:]]*lc-debt:"


def _harvest(root: Path) -> list[dict[str, object]]:
    try:
        proc = subprocess.run(
            ["git", "grep", "-nEI", "--no-color", _PATTERN],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    items: list[dict[str, object]] = []
    for line in proc.stdout.splitlines():
        path, _, rest = line.partition(":")
        lineno, _, content = rest.partition(":")
        if not lineno.isdigit() or _MARKER not in content:
            continue
        note = content.split(_MARKER, 1)[1].strip()
        # Convention: ``lc-debt: <ceiling>; <upgrade path>``. No ``;`` (or an
        # empty upgrade half) means the marker names no revisit trigger.
        ceiling, sep, upgrade = note.partition(";")
        upgrade = upgrade.strip()
        has_trigger = bool(sep and upgrade)
        items.append(
            {
                "file": path,
                "line": int(lineno),
                "note": note,
                "ceiling": ceiling.strip(),
                "upgrade": upgrade or None,
                "no_trigger": not has_trigger,
            }
        )
    return items


@click.command("debt")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def debt_cmd(ctx: click.Context, as_json: bool) -> None:
    """Harvest ``lc-debt:`` markers (deferred simplifications) into a ledger."""
    items = _harvest(Path.cwd())
    if as_json:
        _emit(items, as_json=True)
        return
    if not items:
        click.echo("No lc-debt: markers found — clean.")
        return
    missing = sum(1 for it in items if it["no_trigger"])
    header = f"{len(items)} deferred simplification(s)"
    if missing:
        header += f", {missing} with no trigger"
    click.echo(header + ":\n")
    for it in items:
        tail = "[no-trigger]" if it["no_trigger"] else f"→ {it['upgrade']}"
        click.echo(f"  {it['file']}:{it['line']}  {it['ceiling']}  {tail}")
