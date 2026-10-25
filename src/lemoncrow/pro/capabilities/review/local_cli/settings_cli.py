"""``lc settings``, as the installed Review engine runs it: save a key, remember an agent, change a setting.

The installed ``lc`` is the stdlib-only Review client; it hands this command to the engine's interpreter the way it hands
``lc review --ai`` (:mod:`lemoncrow.pro.capabilities.review.local_cli`):

    <engine python> -m lemoncrow.pro.capabilities.review.local_cli.settings_cli [show|get|set|reset ...]

It reads and writes the same ``<root>/plugin_settings.json`` as the full CLI's ``lc settings`` (a source checkout has the
picker; this has the plain commands), and it is gateway-free so the engine can ship it. The file can hold API keys, so it
is written readable by its owner only, and saying where a key went is part of saving it.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click

#: What a person using ``lc review --ai`` is most likely to want, in the order they will want it.
ESSENTIALS = ("llm.anthropic_api_key", "llm.openai_api_key", "review.agent")
_WIDTH = 76


def _root() -> Path:
    from lemoncrow.core.foundation.paths import default_store_root

    return default_store_root()


def _spec(key: str) -> Any:
    from lemoncrow.core.settings import find_by_key

    spec = find_by_key(key)
    if spec is None:
        raise click.ClickException(f"unknown setting: {key}. See them all: lc settings show --all")
    return spec


def _shown(spec: Any, value: Any) -> str:
    from lemoncrow.core.settings import is_secret

    if is_secret(spec) and value:
        return "saved (hidden)"
    if spec.type == "bool":
        return "on" if value else "off"
    if value in ("", None):
        return "not set"
    return str(value)


def _current(root: Path, spec: Any) -> Any:
    from lemoncrow.core.settings import load_settings

    return load_settings(root).get(spec.key, spec.default)


def _row(root: Path, spec: Any) -> str:
    text = " ".join(spec.description.split())
    if len(text) > _WIDTH:
        text = text[: _WIDTH - 1].rstrip() + "…"
    return f"  {spec.key:<24} {_shown(spec, _current(root, spec)):<15} {text}"


def _count() -> int:
    from lemoncrow.core.settings import all_settings

    return len(all_settings())


def _overview(root: Path) -> str:
    from lemoncrow.core.settings import settings_path

    lines = [f"Settings for lc review --ai  (saved in {settings_path(root)})"]
    lines += [_row(root, _spec(key)) for key in ESSENTIALS]
    lines += [
        "",
        "An exported ANTHROPIC_API_KEY or OPENAI_API_KEY wins over a saved key.",
        "Save a key:  lc settings set llm.anthropic_api_key      (leave the value out to be asked for it)",
        f"Change one:  lc settings set KEY VALUE        See all {_count()}:  lc settings show --all",
    ]
    return "\n".join(lines)


@click.group("settings", invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
def settings_group(ctx: click.Context) -> None:
    """Save an API key, remember an agent, or change a setting. Run with no command for the ones lc review --ai uses."""

    if ctx.invoked_subcommand is None:
        click.echo(_overview(_root()))


@settings_group.command("show")
@click.option("--all", "show_all", is_flag=True, help="List every setting, grouped by area.")
@click.option("--category", default=None, help="List one area (for example llm or review).")
@click.option("--json", "as_json", is_flag=True, help="Every setting's value as JSON (secrets are not printed).")
def show_cmd(show_all: bool, category: str | None, as_json: bool) -> None:
    """The settings lc review --ai uses; --all or --category lists more."""

    from lemoncrow.core.settings import CATEGORIES, all_settings, is_secret, load_settings

    root = _root()
    if category is not None and category not in CATEGORIES:
        raise click.ClickException(f"unknown category: {category} (choices: {', '.join(CATEGORIES)})")
    if as_json:
        values = {
            key: ("hidden" if is_secret(_spec(key)) and value else value)
            for key, value in load_settings(root, category=category).items()
        }
        click.echo(json.dumps(values, indent=2, sort_keys=True))
        return
    if not (show_all or category):
        click.echo(_overview(root))
        return
    for area in [category] if category else CATEGORIES:
        click.echo(f"{area}")
        for spec in all_settings():
            if spec.category == area:
                click.echo(_row(root, spec))
        click.echo()


@settings_group.command("get")
@click.argument("key")
def get_cmd(key: str) -> None:
    """One setting in full: value, default, allowed values and what it does."""

    from lemoncrow.core.settings import env_override

    spec = _spec(key)
    root = _root()
    click.echo(f"{spec.key}: {_shown(spec, _current(root, spec))}")
    click.echo(f"  {' '.join(spec.description.split())}")
    if spec.choices:
        click.echo(f"  one of: {', '.join(spec.choices)}")
    if spec.env_var:
        exported = env_override(spec)
        click.echo(f"  environment variable: {spec.env_var}" + (" (exported, and it wins)" if exported else ""))
    click.echo(f"  change it: lc settings set {spec.key} VALUE      default again: lc settings reset {spec.key}")


def _owner_only(path: Path) -> bool:
    try:
        return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    except OSError:
        return False


@settings_group.command("set")
@click.argument("key")
@click.argument("value", required=False)
def set_cmd(key: str, value: str | None) -> None:
    """Set KEY to VALUE. For a key or token, leave VALUE out to type it without it landing in your shell history."""

    from lemoncrow.core.settings import env_override, is_secret, secret_location_note, settings_path, write_setting

    spec = _spec(key)
    root = _root()
    if value is None or value == "-":
        if not is_secret(spec):
            raise click.UsageError(f"give a value: lc settings set {key} VALUE")
        if value == "-" or not sys.stdin.isatty():
            value = sys.stdin.read().strip()
        else:
            value = click.prompt(f"{key}", hide_input=True, default="", show_default=False).strip()
        if not value:
            raise click.ClickException("no value given; nothing was saved")
    try:
        write_setting(root, key, value)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    shown = "saved" if is_secret(spec) else f"set to {_shown(spec, _current(root, spec))}"
    click.echo(f"✓ {key} {shown}.")
    if is_secret(spec):
        if _owner_only(settings_path(root)):
            click.echo(secret_location_note(root, spec))
        else:
            click.echo(
                f"Stored as plain text in {settings_path(root)}, and this system would not make it owner-only: "
                "export the key in your shell instead."
            )
    vendor = spec.yields_to
    if vendor and os.environ.get(vendor):
        click.echo(f"Note: your exported {vendor} still wins over the saved key.")
    elif spec.env_var and env_override(spec):
        click.echo(f"Note: your exported {spec.env_var} still wins over the saved value.")


@settings_group.command("reset")
@click.argument("key")
def reset_cmd(key: str) -> None:
    """Put KEY back to its default (a saved key is removed)."""

    from lemoncrow.core.settings import load_raw, unset_setting

    _spec(key)
    root = _root()
    saved = key in load_raw(root)
    unset_setting(root, key)
    click.echo(f"✓ {key} is back to its default." if saved else f"{key} was already at its default.")


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``lc settings ARGV`` (default ``sys.argv[1:]``); click raises ``SystemExit`` with the status."""

    settings_group.main(args=list(sys.argv[1:] if argv is None else argv), prog_name="lc settings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
