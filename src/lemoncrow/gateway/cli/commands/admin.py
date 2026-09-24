from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import click
import yaml

from lemoncrow.core.capabilities.code_context_contract import IndexLockTimeout
from lemoncrow.core.capabilities.model_settings import (
    TOP_MODEL_CHOICES,
    build_runtime_settings_payload,
    resolve_host_model,
    resolve_runtime_model,
    set_host_role_models,
    write_workspace_model_settings,
)
from lemoncrow.core.capabilities.plugin_runtime import ATTRIBUTION_EMAIL, ATTRIBUTION_NAME
from lemoncrow.core.capabilities.reporting.dashboard import _render_dashboard
from lemoncrow.core.capabilities.workspace_host_overrides import (
    write_workspace_agents_md,
    write_workspace_claude_overrides,
    write_workspace_codex_agent_config,
    write_workspace_codex_agents,
    write_workspace_copilot_agents,
    write_workspace_cursor_rules,
    write_workspace_lemoncode_agents,
    write_workspace_opencode_agents,
)
from lemoncrow.core.foundation.models import Playbook, Rubric
from lemoncrow.core.foundation.paths import detect_git_root as _detect_git_root
from lemoncrow.core.foundation.paths import detect_host
from lemoncrow.core.foundation.paths import ensure_gitignore as _ensure_gitignore
from lemoncrow.core.settings import CATEGORIES as SETTINGS_CATEGORIES
from lemoncrow.gateway.cli.commands._shared import (
    _core_runtime,
    _emit,
    _load_store,
)
from lemoncrow.gateway.integrations.openmemory_lifecycle import project_root as _project_root

_RUNTIME_ROLE_PROMPT_ORDER = ("code", "execute", "solve", "general", "explore", "plan", "research", "review")
_HOST_ROLE_PROMPT_ORDER = ("code", "execute", "solve", "explore", "plan", "research", "review")
_CUSTOM_MODEL_OPTION = "Others (Enter model)"
_PURPLE = (155, 117, 217)


def _is_interactive_terminal() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _supports_interactive_selector() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") not in (None, "", "dumb"))


def _clear_rendered_lines(count: int) -> None:
    if count > 0:
        click.echo(f"\x1b[{count}A\x1b[J", nl=False)


def _read_selector_key() -> str:
    key = click.getchar()
    if key != "\x1b":
        return key
    second = click.getchar()
    if second in ("[", "O"):
        return key + second + click.getchar()
    return key + second


def _selector_action(key: str) -> str:
    if key in ("\r", "\n"):
        return "enter"
    if key in ("k", "K", "\x1b[A", "\x1bOA"):
        return "up"
    if key in ("j", "J", "\x1b[B", "\x1bOB"):
        return "down"
    return "other"


def _interactive_single_select(prompt: str, options: tuple[str, ...], *, default: str | None = None) -> str:
    selected = options.index(default) if default in options else 0
    click.echo("")
    click.secho(prompt, fg=_PURPLE)
    rendered = 0
    while True:
        if rendered:
            _clear_rendered_lines(rendered)
        lines = [
            f"  {click.style('▸ ●', fg=_PURPLE) if index == selected else '  ○'}  {option}"
            for index, option in enumerate(options)
        ]
        lines.append("")
        lines.append(
            f"  {click.style('↑↓', fg=_PURPLE)} {click.style('navigate  ·  ', dim=True)}"
            f"{click.style('enter', fg=_PURPLE)} {click.style('select', dim=True)}"
        )
        for line in lines:
            click.echo(line)
        rendered = len(lines)
        action = _selector_action(_read_selector_key())
        if action == "up":
            selected = (selected - 1) % len(options)
        elif action == "down":
            selected = (selected + 1) % len(options)
        elif action == "enter":
            break
    _clear_rendered_lines(rendered)
    click.echo(click.style(f"  ●  {options[selected]}", dim=True))
    return options[selected]


def _normalize_model_choice(default: str) -> str:
    return default if default in TOP_MODEL_CHOICES else TOP_MODEL_CHOICES[0]


def _is_printable_key(key: str) -> bool:
    return len(key) == 1 and key.isprintable() and key not in ("\r", "\n")


def _interactive_model_select(label: str, *, default: str, allow_auto: bool, show_auto: bool) -> str:
    options = (("auto",) if show_auto else ()) + TOP_MODEL_CHOICES + (_CUSTOM_MODEL_OPTION,)
    selected = options.index(default) if default in options else 0
    rendered = 0
    custom_value = ""

    click.echo("")
    click.secho(label, fg=_PURPLE)
    while True:
        if rendered:
            _clear_rendered_lines(rendered)

        lines: list[str] = []
        for index, option in enumerate(options):
            marker = click.style("▸ ●", fg=_PURPLE) if index == selected else "  ○"
            if option == _CUSTOM_MODEL_OPTION and index == selected:
                value = custom_value or click.style("Type custom model...", dim=True)
                if custom_value:
                    value = custom_value
                lines.append(f"  {marker}  {value}")
            else:
                lines.append(f"  {marker}  {option}")
        if options[selected] == _CUSTOM_MODEL_OPTION:
            lines.extend(
                [
                    "",
                    f"  {click.style('type to edit  ·  ', dim=True)}{click.style('enter', fg=_PURPLE)}"
                    f"{click.style(' confirm  ·  ', dim=True)}{click.style('esc', fg=_PURPLE)}"
                    f"{click.style(' cancel', dim=True)}",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    f"  {click.style('↑↓', fg=_PURPLE)} {click.style('navigate  ·  ', dim=True)}"
                    f"{click.style('enter', fg=_PURPLE)} {click.style('select', dim=True)}",
                ]
            )

        for line in lines:
            click.echo(line)
        rendered = len(lines)

        key = _read_selector_key()
        if options[selected] == _CUSTOM_MODEL_OPTION:
            if key in ("\r", "\n"):
                if custom_value.strip():
                    _clear_rendered_lines(rendered)
                    click.echo(click.style(f"  ●  {custom_value.strip()}", dim=True))
                    return custom_value.strip()
                continue
            if key in ("\x7f", "\b"):
                custom_value = custom_value[:-1]
                continue
            if key == "\x1b":
                custom_value = ""
                continue
            if _is_printable_key(key):
                custom_value += key
                continue
            action = _selector_action(key)
            if action == "up":
                selected = (selected - 1) % len(options)
                continue
            if action == "down":
                selected = (selected + 1) % len(options)
                continue
            continue

        action = _selector_action(key)
        if action == "up":
            selected = (selected - 1) % len(options)
        elif action == "down":
            selected = (selected + 1) % len(options)
        elif action == "enter":
            _clear_rendered_lines(rendered)
            click.echo(click.style(f"  ●  {options[selected]}", dim=True))
            return options[selected]


def _prompt_custom_model_value(label: str, *, default: str) -> str:
    while True:
        value = str(click.prompt(f"{label} value", default=default, show_default=True)).strip()
        if value:
            return value


def _prompt_model_select(label: str, *, default: str, allow_auto: bool, show_auto: bool = True) -> str:
    resolved_default = default if allow_auto and default == "auto" else _normalize_model_choice(default)
    if _supports_interactive_selector():
        selector_default = "auto" if show_auto else resolved_default
        selection = _interactive_model_select(
            label,
            default=selector_default,
            allow_auto=allow_auto,
            show_auto=show_auto,
        )
        if selection == "auto" and not allow_auto:
            return _normalize_model_choice(default)
        return selection
    while True:
        value = str(click.prompt(label, default=default, show_default=True)).strip()
        if not value:
            continue
        if value == "auto" and not allow_auto:
            return _normalize_model_choice(default)
        return value


def _confirm_customize_models() -> bool:
    if _supports_interactive_selector():
        selection = _interactive_single_select(
            "Customize role models",
            ("Keep current defaults", "Customize now"),
            default="Keep current defaults",
        )
        return selection == "Customize now"
    return click.confirm("Customize role models?", default=False)


def _confirm_optional(prompt: str, *, default: bool, yes_label: str, no_label: str) -> bool:
    if _supports_interactive_selector():
        options = (yes_label, no_label) if default else (no_label, yes_label)
        selection = _interactive_single_select(
            prompt,
            options,
            default=yes_label if default else no_label,
        )
        return selection == yes_label
    return click.confirm(prompt, default=default)


def _counted(count: int, noun: str) -> str:
    """``"1 Codex agent"`` / ``"3 Codex agents"`` — never "1 ... agents"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _detected_workspace_hosts(workspace_root: Path) -> tuple[str, ...]:
    checks: tuple[tuple[str, bool], ...] = (
        (
            "copilot",
            bool(
                shutil.which("code") or (workspace_root / ".github").exists() or (workspace_root / ".vscode").exists()
            ),
        ),
        ("claude", bool(shutil.which("claude") or (workspace_root / ".claude").exists())),
        ("codex", bool(shutil.which("codex") or (workspace_root / ".codex").exists())),
        (
            "opencode",
            bool(
                shutil.which("opencode")
                or (workspace_root / "opencode.json").exists()
                or (workspace_root / ".opencode").exists()
            ),
        ),
        # LemonCode shares opencode's `.opencode/` workspace dir, so only the
        # binary distinguishes it.
        ("lemoncode", bool(shutil.which("lemoncode"))),
        ("antigravity", bool(shutil.which("antigravity") or shutil.which("agy"))),
        ("cursor", bool((workspace_root / ".cursor").exists())),
        (
            "hermes",
            # detect_host() covers the env-var signal canonically; shutil.which
            # catches an installed hermes binary even with no session active.
            bool(detect_host() == "hermes" or shutil.which("hermes")),
        ),
    )
    return tuple(host for host, present in checks if present)


def _host_label(host: str) -> str:
    return {
        "copilot": "Copilot/VS Code",
        "claude": "Claude Code",
        "codex": "Codex CLI",
        "opencode": "OpenCode",
        "lemoncode": "LemonCode",
        "antigravity": "Antigravity",
        "cursor": "Cursor",
        "hermes": "Hermes",
    }.get(host, host.title())


def _prompt_workspace_model_config(workspace_root: Path) -> dict[str, Any] | None:
    click.echo("")
    click.echo("LemonCrow workspace model configuration.")
    if not _confirm_customize_models():
        return None

    runtime_models = _prompt_runtime_role_models(workspace_root)
    payload = build_runtime_settings_payload(runtime_models)
    configurable_hosts = _detected_workspace_hosts(workspace_root)
    if configurable_hosts and _confirm_optional(
        "Customize host-specific models as well",
        default=False,
        yes_label="Customize host overrides",
        no_label="No - inherit runtime defaults",
    ):
        for host in configurable_hosts:
            if _confirm_optional(
                f"Customize {_host_label(host)} models",
                default=False,
                yes_label="Yes - customize",
                no_label="No - inherit runtime defaults",
            ):
                host_models = _prompt_host_role_models(host, workspace_root=workspace_root)
                payload = set_host_role_models(payload, host=host, models=host_models)
    return payload


def _prompt_runtime_role_models(workspace_root: Path) -> dict[str, str]:
    click.echo("Runtime/default role models")
    resolved: dict[str, str] = {}
    for role_id in _RUNTIME_ROLE_PROMPT_ORDER:
        default = resolve_runtime_model(role_id, workspace_root)
        resolved[role_id] = _prompt_model_select(f"  {role_id}", default=default, allow_auto=False, show_auto=True)
    click.echo("")
    return resolved


def _prompt_host_role_models(
    host: str,
    *,
    workspace_root: Path,
) -> dict[str, str]:
    click.echo(f"{host.title()} host role models")
    models = {}
    for role_id in _HOST_ROLE_PROMPT_ORDER:
        current = resolve_host_model(host, role_id, workspace_root=workspace_root)
        default = current if current is not None else "auto"
        models[role_id] = _prompt_model_select(f"  {role_id}", default=default, allow_auto=True, show_auto=True)
    click.echo("")
    return models


def _apply_workspace_model_config(
    workspace_root: Path,
    payload: dict[str, Any],
    *,
    detected_hosts: tuple[str, ...] | None = None,
) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}
    detected = set(detected_hosts or _detected_workspace_hosts(workspace_root))
    settings_path = write_workspace_model_settings(workspace_root, payload)
    results["model_settings"] = [f"wrote {settings_path}"]
    if "copilot" in detected:
        copilot_agents = write_workspace_copilot_agents(workspace_root)
        results["copilot"] = [f"updated {_counted(len(copilot_agents), 'workspace-local Copilot file')}"]
    if "claude" in detected:
        claude_paths = write_workspace_claude_overrides(workspace_root)
        results["claude"] = [f"updated {_counted(len(claude_paths), 'workspace-local Claude file')}"]
    if "opencode" in detected:
        opencode_agents = write_workspace_opencode_agents(workspace_root)
        results["opencode"] = [f"updated {_counted(len(opencode_agents), 'workspace-local OpenCode agent')}"]
    if "lemoncode" in detected:
        lemoncode_agents = write_workspace_lemoncode_agents(workspace_root)
        results["lemoncode"] = [f"updated {_counted(len(lemoncode_agents), 'workspace-local LemonCode agent')}"]
    if "codex" in detected:
        codex_agents = write_workspace_codex_agents(workspace_root)
        results["codex"] = [f"updated {_counted(len(codex_agents), 'workspace-local Codex agent')}"]
    if "cursor" in detected:
        cursor_rules = write_workspace_cursor_rules(workspace_root)
        results["cursor"] = [f"updated {_counted(len(cursor_rules), 'workspace-local Cursor rule file')}"]
    return results


# The shell body of the ``prepare-commit-msg`` hook, byte-identical to the one
# ``integrations/claude/plugin/scripts/install_attribution_hook.sh`` and
# ``scripts/lib/common.sh`` emit. Kept as a raw string so the sed expression's
# backslashes survive unmangled.
#
# ``LemonCrow-Session`` / ``LemonCrow-Model`` are what make a committed range an
# exact join with the session that authored it, rather than a wall-clock guess:
# ``lc review <range>`` can read the trailer instead of scoring candidates. Both
# are omitted when the session state does not name them -- an absent trailer is
# honest, an invented one is not.
_ATTRIBUTION_HOOK_BODY = r"""LEMONCROW_STATE="$(git rev-parse --show-toplevel 2>/dev/null)/.lemoncrow/workspace/session_state.json"
lemoncrow_state_value() {
  [ -f "$LEMONCROW_STATE" ] || return 0
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$LEMONCROW_STATE" 2>/dev/null | head -n 1
}
case "$2" in
  merge|squash) ;;
  *)
    if ! grep -qF "$LEMONCROW_TRAILER" "$1" 2>/dev/null; then
      printf '\n%s\n' "$LEMONCROW_TRAILER" >> "$1"
    fi
    LEMONCROW_SID="$(lemoncrow_state_value session_id)"
    if [ -n "$LEMONCROW_SID" ] && ! grep -q '^LemonCrow-Session:' "$1" 2>/dev/null; then
      printf 'LemonCrow-Session: %s\n' "$LEMONCROW_SID" >> "$1"
    fi
    LEMONCROW_MODEL="$(lemoncrow_state_value model)"
    if [ -n "$LEMONCROW_MODEL" ] && ! grep -q '^LemonCrow-Model:' "$1" 2>/dev/null; then
      printf 'LemonCrow-Model: %s\n' "$LEMONCROW_MODEL" >> "$1"
    fi
    ;;
esac
"""


def _install_attribution_hook(git_root: Path) -> str | None:
    """Install the LemonCrow co-author ``prepare-commit-msg`` hook.

    Resolves the active hooks directory via ``git rev-parse --git-path hooks``
    (respects ``core.hooksPath``). Idempotent — skips if the LemonCrow marker is
    already present.

    Returns a status message, or ``None`` if already installed.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(git_root), "rev-parse", "--git-path", "hooks"],
            capture_output=True,
            text=True,
            check=True,
        )
        hooks_dir = Path(proc.stdout.strip())
    except (subprocess.CalledProcessError, OSError):
        return None  # not a git repo or git unavailable

    if not hooks_dir.is_absolute():
        hooks_dir = git_root / hooks_dir

    hook_path = hooks_dir / "prepare-commit-msg"
    marker = "# >>> lemoncrow attribution >>>"
    end_marker = "# <<< lemoncrow attribution <<<"
    trailer = f"Co-Authored-By: {ATTRIBUTION_NAME} <{ATTRIBUTION_EMAIL}>"

    if hook_path.exists() and marker in hook_path.read_text(encoding="utf-8"):
        return None  # already installed

    hooks_dir.mkdir(parents=True, exist_ok=True)
    content = (
        "#!/usr/bin/env bash\n"
        "\n"
        f"{marker}\n"
        "# Managed by LemonCrow (lc init). Appends the co-author trailer unless already present.\n"
        "# Skips merge/squash commit messages.\n"
        f'LEMONCROW_TRAILER="{trailer}"\n'
        f"{_ATTRIBUTION_HOOK_BODY}"
        f"{end_marker}\n"
    )
    hook_path.write_text(content, encoding="utf-8")
    hook_path.chmod(0o755)
    return f"LemonCrow co-author hook installed at {hook_path.relative_to(git_root)}"


def _project_init_setup(git_root: Path) -> dict[str, list[str]]:
    """Run project-scoped init steps inside a git repo.

    Returns a dict of ``{section: [messages]}`` describing what was done.
    """
    results: dict[str, list[str]] = {}

    # .gitignore \u2014 write .lemoncrow/.gitignore so the dir stays visible in git
    added = _ensure_gitignore(git_root)
    if added:
        results["gitignore"] = [".lemoncrow/.gitignore written (ignores everything inside .lemoncrow/)"]
    else:
        results["gitignore"] = [".lemoncrow/.gitignore already present"]

    # jj — colocated Jujutsu repo for session-level undo without git history noise
    jj_dir = git_root / ".jj"
    if jj_dir.exists():
        results["jj"] = ["jj already initialized"]
    elif shutil.which("jj"):
        try:
            proc = subprocess.run(
                ["jj", "git", "init", "--colocate"],
                cwd=git_root,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                results["jj"] = ["jj initialized (colocated) — use `jj undo` to roll back any edit"]
            else:
                results["jj"] = [f"jj init failed: {proc.stderr.strip()}"]
        except OSError as exc:
            results["jj"] = [f"jj init error: {exc}"]
    else:
        results["jj"] = ["jj not found — install via `brew install jj` or `cargo install --locked jj-cli`"]

    agents_path = write_workspace_agents_md(git_root)
    results["agents_md"] = [f"updated {agents_path.relative_to(git_root)}"]

    if "codex" in _detected_workspace_hosts(git_root):
        codex_agents = write_workspace_codex_agents(git_root)
        codex_config = write_workspace_codex_agent_config(git_root)
        results["codex"] = [
            f"updated {_counted(len(codex_agents), 'workspace-local Codex agent')}",
            f"updated {codex_config.relative_to(git_root)}",
        ]

    # Attribution hook — write via the active hooks directory (respects core.hooksPath)
    hook_msg = _install_attribution_hook(git_root)
    if hook_msg:
        results["attribution"] = [hook_msg]

    return results


def _code_index_db_path(repo_root: Path) -> Path:
    """Return the default code index database path for a repo."""
    from lemoncrow.core.foundation.paths import resolve_workspace_store_dir

    return resolve_workspace_store_dir(workspace_root=repo_root) / "code_context.sqlite"


def _index_stats_pretty(repo_root: Path) -> list[str]:
    """Return human-readable index stats lines for a repo."""
    db_path = _code_index_db_path(repo_root)
    if not db_path.exists():
        return ["(no index — run `lc code index` first)"]
    lines: list[str] = []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT value FROM engine_state WHERE key = 'index_version'").fetchone()
        index_version = int(row["value"]) if row else 0
        file_count = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        symbol_count = conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
        import_count = conn.execute("SELECT COUNT(*) AS n FROM imports").fetchone()["n"]
        conn.close()
        lines.append(f"Index version: {index_version}")
        lines.append(f"Files indexed: {file_count}")
        lines.append(f"Symbols indexed: {symbol_count}")
        lines.append(f"Imports indexed: {import_count}")
    except sqlite3.Error as exc:
        lines.append(f"Error reading index: {exc}")
    return lines


def _seed_resources() -> tuple[list[Path], list[Path]]:
    """Return (block_files, rubric_files) bundled with the package."""
    blocks_dir = resources.files("lemoncrow") / "infra" / "seed_playbooks"
    rubrics_dir = resources.files("lemoncrow") / "core" / "rubrics"
    block_files = sorted(Path(str(p)) for p in blocks_dir.iterdir() if p.name.endswith(".yaml"))
    rubric_files = sorted(Path(str(p)) for p in rubrics_dir.iterdir() if p.name.endswith(".yaml"))
    return block_files, rubric_files


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_domain_manager(root: Path) -> Any:
    from lemoncrow.core.domains import DomainManager

    return DomainManager(root)


def _parse_since_arg(value: str) -> datetime:
    """Parse ``--since`` argument.

    Accepts:
    * ``7d``, ``30d``, ``24h``, ``30m``  - duration relative to now
    * ``YYYY-MM-DD``                       - absolute date (start of day UTC)
    """
    stripped = value.strip()
    match = re.fullmatch(r"(\d+)([dhm])", stripped)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = (
            timedelta(days=amount)
            if unit == "d"
            else timedelta(hours=amount) if unit == "h" else timedelta(minutes=amount)
        )
        return datetime.now(UTC) - delta

    try:
        return datetime.strptime(stripped, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        pass

    raise click.ClickException(
        f"Cannot parse --since value {value!r}. Use a duration like '7d', '24h', or a date like '2026-05-01'."
    )


@click.command()
@click.option("--seed/--no-seed", default=True, help="Import bundled seed blocks and rubrics.")
@click.option(
    "--index/--no-index",
    default=True,
    help="Bootstrap the code index for the current git repo (default: on).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Deprecated compatibility flag; a live SQLite writer is never bypassed.",
)
@click.option(
    "--configure-models/--no-configure-models",
    default=None,
    help="Prompt for project-local role/host model settings when running inside a git repo.",
)
@click.pass_context
def init(
    ctx: click.Context,
    seed: bool,
    index: bool,
    force: bool,
    configure_models: bool | None,
) -> None:
    """Initialize the fully local runtime store at --root."""

    root: Path = ctx.obj["root"]
    # A non-git, never-registered cwd must be marked BEFORE `create_store`:
    # ContextStore resolves the active workspace root internally (for its
    # blocks/rubrics mirror dir under the store root), which now requires cwd
    # to be either a git repo or already `lc init`-registered. Without
    # this, the very first `init` run in a fresh non-git directory would
    # raise WorkspaceNotRegisteredError from inside `create_store` before this
    # command ever gets a chance to register the directory itself below.
    if _detect_git_root(Path.cwd()) is None:
        _ensure_gitignore(Path.cwd())
    from lemoncrow.infra.storage.factory import create_store

    try:
        store = create_store(root)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    store.init()
    # One-time cleanup of legacy commercial device/cap state (idempotent).
    from lemoncrow.core.foundation.legacy_migration import run_startup_migrations

    run_startup_migrations(root)
    click.echo(
        f"  {click.style('✓', fg='green')} {click.style('store', fg=(155, 117, 217))} {click.style(f'initialized at {store.knowledge.root}', dim=True)}"
    )
    if seed:
        block_files, rubric_files = _seed_resources()
        if block_files or rubric_files:
            seeded_blocks: dict[str, Playbook] = {}
            for path in block_files:
                data = _load_yaml(path)
                try:
                    if "id" not in data:
                        data["id"] = Playbook.make_id(data["title"], data["domain"])
                    block = Playbook.model_validate(data)
                except (KeyError, ValueError) as exc:
                    raise click.ClickException(f"invalid seed playbook {path}: {exc}") from exc
                seeded_blocks[block.id] = block
            for block in _load_domain_manager(root).all_playbooks():
                seeded_blocks[block.id] = block
            n_b = 0
            for block in seeded_blocks.values():
                store.knowledge.upsert_block(block)
                n_b += 1
            n_r = 0
            for path in rubric_files:
                data = _load_yaml(path)
                try:
                    rubric = Rubric.model_validate(data)
                except (KeyError, ValueError) as exc:
                    raise click.ClickException(f"invalid seed rubric {path}: {exc}") from exc
                store.knowledge.upsert_rubric(rubric)
                n_r += 1
            click.echo(
                f"  {click.style('✓', fg='green')} {click.style('seed', fg=(155, 117, 217))} "
                f"{click.style(f'seeded {n_b} playbooks and {n_r} rubrics', dim=True)}"
            )
    if index:
        git_root = _detect_git_root(Path.cwd())
        if git_root is not None:
            from lemoncrow.gateway.cli.commands.code import (
                _code_context_engine,
                _index_repo_with_progress,
            )

            engine = _code_context_engine(str(git_root))
            try:
                stats = _index_repo_with_progress(
                    engine,
                    steal=force,
                    description="Bootstrapping code index",
                    success_description="Code index ready",
                )
            except IndexLockTimeout:
                if force:
                    raise
                click.echo("code index skipped (another LemonCrow process is indexing); retry when it finishes")
            else:
                fi = stats["files_indexed"]
                si = stats["symbols_indexed"]
                ii = stats["imports_indexed"]
                click.echo(
                    f"  {click.style('✓', fg='green')} {click.style('index', fg=(155, 117, 217))} "
                    f"{click.style(f'indexed {fi} files, {si} symbols ({ii} imports)', dim=True)}"
                )
        else:
            click.echo("code index skipped (no git repository detected in current directory)")
    git_root = _detect_git_root(Path.cwd())
    if git_root is not None:
        results = _project_init_setup(git_root)
        registered_root = git_root
        for section, messages in results.items():
            for msg in messages:
                click.echo(
                    f"  {click.style('✓', fg='green')} {click.style(section, fg=(155, 117, 217))} {click.style(msg, dim=True)}"
                )
    else:
        _ensure_gitignore(Path.cwd())
        registered_root = Path.cwd().resolve()
        click.echo(f"registered {Path.cwd()} as an LemonCrow workspace (no git repository detected)")
    # lc init is the durable registration point used by the one-daemon,
    # many-project request router. Indexes remain project-local.
    from lemoncrow.core.service.project_registry import register_project

    register_project(registered_root)
    # Hidden for now (needs more work) — no longer auto-prompts on a bare
    # `lc init`; only runs when explicitly requested via --configure-models.
    should_offer_model_config = bool(git_root is not None and _is_interactive_terminal())
    if configure_models and not should_offer_model_config:
        raise click.ClickException("--configure-models requires an interactive terminal inside a git repository.")
    if should_offer_model_config and configure_models is True:
        assert git_root is not None
        payload = _prompt_workspace_model_config(git_root)
        if payload is not None:
            results = _apply_workspace_model_config(
                git_root, payload, detected_hosts=_detected_workspace_hosts(git_root)
            )
            for section, messages in results.items():
                for msg in messages:
                    click.echo(
                        f"  {click.style('✓', fg='green')} {click.style(section, fg=(155, 117, 217))} {click.style(msg, dim=True)}"
                    )


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def doctor_cmd(ctx: click.Context, as_json: bool) -> None:
    """Full diagnostics: core install, services, MCP servers, integrations, environment.

    \b
    Sections:
      Core              python, lemoncrow version, git repo, store
      Code intelligence code index, zoekt search backend
      Services          local loopback server
      MCP               active LemonCrow MCP server processes (see also: lc mcp list)
      Integrations      letta, openmemory, langfuse, external compactors
      Environment       host CLIs, external tools, optional python packages, core libraries
    """
    import lemoncrow as _lemoncrow_pkg

    checks: dict[str, Any] = {}
    sections: dict[str, list[str]] = {}

    def add(section: str, name: str, info: dict[str, Any]) -> None:
        checks[name] = info
        sections.setdefault(section, []).append(name)

    # ── Core ────────────────────────────────────────────────────────────
    py_ok = sys.version_info >= (3, 10)
    add(
        "Core",
        "python",
        {"ok": py_ok, "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"},
    )
    add("Core", "lemoncrow", {"ok": True, "version": _lemoncrow_pkg.__version__})

    git_root = _detect_git_root(Path.cwd())
    add(
        "Core",
        "git_repo",
        {"ok": git_root is not None, "optional": True, "path": str(git_root) if git_root else None},
    )

    root: Path = ctx.obj["root"]
    from lemoncrow.core.foundation.knowledge_store import KnowledgeStore

    store_ok = root.exists() and KnowledgeStore(root).db_path.exists()
    store_info: dict[str, Any] = {"ok": store_ok, "root": str(root), "exists": root.exists()}
    try:
        from lemoncrow.infra.storage.factory import create_store

        store = create_store(root)
        store.init()
        health = store.health_check()
        store_info["backend"] = type(store).__name__
        store_info["healthy"] = (
            bool(health.get("ok", health.get("healthy", True))) if isinstance(health, dict) else True
        )
    except Exception as exc:
        store_info["healthy"] = False
        store_info["hint"] = f"store health check failed: {exc}"
    add("Core", "store", store_info)

    # ── Code intelligence ───────────────────────────────────────────────
    if git_root:
        index_path = _code_index_db_path(git_root)
        index_ok = index_path.exists()
        stats = _index_stats_pretty(git_root) if index_ok else []
        add(
            "Code intelligence",
            "code_index",
            {"ok": index_ok, "path": str(index_path), "stats": stats if stats else None},
        )
    else:
        add("Code intelligence", "code_index", {"ok": False, "optional": True, "path": None, "stats": None})

    try:
        from lemoncrow.infra.code_intel.zoekt.binary import discover_zoekt_binary

        zr = discover_zoekt_binary(git_root or Path.cwd())
        add(
            "Code intelligence",
            "zoekt",
            {
                "ok": True,
                "optional": True,
                "installed": zr.available,
                "path": str(zr.path) if zr.path else None,
                "hint": zr.reason if not zr.available else None,
            },
        )
    except Exception:
        add("Code intelligence", "zoekt", {"ok": True, "optional": True, "installed": False})

    # ── Services ────────────────────────────────────────────────────────
    try:
        from lemoncrow.infra.runtime.dashboard_url import DEFAULT_LOOPBACK_URL, discover_dashboard_url

        loopback_url = (os.environ.get("LEMONCROW_URL") or DEFAULT_LOOPBACK_URL).rstrip("/")
        frontend_url = discover_dashboard_url(root)
        add(
            "Services",
            "loopback_server",
            {
                "ok": frontend_url is not None,
                "optional": False,
                "installed": frontend_url is not None,
                "url": loopback_url,
                "hint": None if frontend_url else "not running — restart the local loopback server",
            },
        )
    except Exception as exc:
        add("Services", "loopback_server", {"ok": False, "optional": False, "installed": False, "hint": str(exc)})

    # ── MCP ─────────────────────────────────────────────────────────────
    try:
        from lemoncrow.gateway.cli.commands.mcp import active_mcp_sessions

        servers = active_mcp_sessions(root)
        lines = []
        for s in servers:
            ws = str(s.get("workspace") or "?")
            home = str(Path.home())
            if ws.startswith(home):
                ws = "~" + ws[len(home) :]
            sid = str(s.get("claude_session_id") or "")[:8]
            lines.append(f"pid {s.get('pid')}  {ws}" + (f"  session={sid}" if sid else ""))
        add(
            "MCP",
            "mcp_servers",
            {
                "ok": True,
                "optional": True,
                "installed": bool(servers),
                "count": len(servers),
                "stats": lines or None,
                "hint": None if servers else "no active servers — full list: lc mcp list",
            },
        )
    except Exception:
        add("MCP", "mcp_servers", {"ok": True, "optional": True, "installed": False, "count": 0})

    # ── Integrations ────────────────────────────────────────────────────
    letta_url = os.environ.get("LEMONCROW_LETTA_URL", "http://localhost:8283").rstrip("/")
    letta_info: dict[str, Any] = {"ok": True, "optional": True, "installed": False, "url": letta_url}
    try:
        import urllib.request as _urllib_request

        with _urllib_request.urlopen(f"{letta_url}/v1/health", timeout=1.5) as resp:
            letta_info["installed"] = resp.status == 200
    except Exception:
        letta_info["hint"] = "unreachable — start: lc letta up"
    add("Integrations", "letta", letta_info)

    try:
        from lemoncrow.gateway.integrations.openmemory_lifecycle import openmemory_workdir

        om_dir = openmemory_workdir(root)
        add(
            "Integrations",
            "openmemory",
            {
                "ok": True,
                "optional": True,
                "installed": om_dir.exists(),
                "path": str(om_dir) if om_dir.exists() else None,
                "hint": None if om_dir.exists() else "not set up — start: lc openmemory up",
            },
        )
    except Exception:
        add("Integrations", "openmemory", {"ok": True, "optional": True, "installed": False})

    langfuse_on = os.environ.get("LEMONCROW_LANGFUSE_ENABLED", "").lower() in ("1", "true", "yes")
    add(
        "Integrations",
        "langfuse",
        {
            "ok": True,
            "optional": True,
            "installed": langfuse_on,
            "hint": None if langfuse_on else "disabled — enable: LEMONCROW_LANGFUSE_ENABLED=1",
        },
    )

    # External compactors (optional soft integrations, e.g. rtk). Absence is
    # never a failure -- LemonCrow falls back to the plain shell path.
    from lemoncrow.pro.capabilities.tool_supervision.external_compactors import (
        external_compactors_enabled,
        registered_compactors,
        resolve_compactor,
    )

    compactors_enabled = external_compactors_enabled()
    for compactor in registered_compactors():
        resolution = resolve_compactor(compactor.name)
        hint: str | None = None
        if not resolution.available:
            hint = "optional — compacts git/gh/test/lint output"
            if compactor.install_hint:
                hint += f"; install: {compactor.install_hint}"
        elif not compactors_enabled:
            hint = "installed but disabled (LEMONCROW_BASH_EXTERNAL_COMPACTORS=0)"
        add(
            "Integrations",
            compactor.name,
            {
                "ok": True,  # optional -- absence never fails diagnostics
                "optional": True,
                "installed": resolution.available,
                "enabled": compactors_enabled,
                "path": str(resolution.path) if resolution.path else None,
                "version": resolution.version,
                "hint": hint,
            },
        )

    # ── Environment ─────────────────────────────────────────────────────
    host_clis = (
        ("claude", "claude"),
        ("codex", "codex"),
        ("opencode", "opencode"),
        ("copilot", "copilot"),
        ("antigravity", "agy"),
    )
    host_lines = [f"{name}: {'installed' if shutil.which(binary) else 'not installed'}" for name, binary in host_clis]
    add("Environment", "host_clis", {"ok": True, "optional": True, "stats": host_lines})

    tool_lines = []
    for tool in ("git", "docker", "node", "npm", "uv", "gh"):
        found = shutil.which(tool)
        tool_lines.append(f"{tool}: {found or 'not found'}")
    add("Environment", "external_tools", {"ok": True, "optional": True, "stats": tool_lines})

    from importlib import metadata as _metadata

    extras = (
        ("mcp", "mcp"),
        ("letta-client", "memory"),
        ("ollama", "smart"),
        ("openai", "cloud"),
        ("psycopg", "postgres"),
        ("pgvector", "vector"),
        ("sentence-transformers", "semantic"),
        ("litellm", "litellm"),
        ("langfuse", "langfuse"),
    )
    extra_lines = []
    for dist, extra in extras:
        try:
            extra_lines.append(f"{dist}: {_metadata.version(dist)}  [{extra}]")
        except _metadata.PackageNotFoundError:
            extra_lines.append(f"{dist}: not installed  (uv pip install 'lemoncrow[{extra}]')")
    add("Environment", "optional_packages", {"ok": True, "optional": True, "stats": extra_lines})

    core_lines = []
    for dist in ("pydantic", "click", "fastapi", "uvicorn", "tiktoken", "tree-sitter", "aiohttp", "rich"):
        try:
            core_lines.append(f"{dist}: {_metadata.version(dist)}")
        except _metadata.PackageNotFoundError:
            core_lines.append(f"{dist}: MISSING")
    add("Environment", "core_libraries", {"ok": all("MISSING" not in line for line in core_lines), "stats": core_lines})

    failed = [name for name, info in checks.items() if not info.get("ok") and not info.get("optional")]

    if as_json:
        _emit(checks, as_json=True)
        if failed:
            ctx.exit(1)
        return

    click.echo("LemonCrow diagnostics")
    click.echo("==================")
    for section, names in sections.items():
        click.echo(f"\n{section}")
        click.echo("-" * len(section))
        for name in names:
            info = checks[name]
            if info.get("optional") and (not info.get("ok") or not info.get("installed", True)):
                status = "○"  # optional and absent/failing -- informational, not a failure
            else:
                status = "✓" if info.get("ok") else "✗"
            click.echo(f"  {status} {name}")
            if info.get("version"):
                click.echo(f"       version: {info['version']}")
            if info.get("path"):
                click.echo(f"       path: {info['path']}")
            if info.get("root"):
                click.echo(f"       root: {info['root']}")
            if info.get("backend"):
                click.echo(f"       backend: {info['backend']}")
            if info.get("pid"):
                click.echo(f"       pid: {info['pid']}")
            if info.get("url"):
                click.echo(f"       url: {info['url']}")
            if info.get("count") is not None:
                click.echo(f"       active: {info['count']}")
            if info.get("last_tick_at"):
                click.echo(f"       last_tick: {info['last_tick_at']}")
            jq = info.get("job_queue")
            if jq:
                click.echo(
                    f"       jobs: pending={jq.get('pending', 0)} running={jq.get('running', 0)}"
                    f" failed={jq.get('failed', 0)} dead={jq.get('dead', 0)}"
                )
            if info.get("hint"):
                click.echo(f"       {info['hint']}")
            if info.get("stats"):
                for line in info["stats"]:
                    click.echo(f"       {line}")
    if failed:
        click.echo(f"\n  ✗ {len(failed)} required check(s) failed: {', '.join(failed)}")
        ctx.exit(1)


@click.command("reset")
@click.option("--all", "all_flag", is_flag=True, help="Reset everything (store + index).")
@click.option("-f", "--force", is_flag=True, help="Skip confirmation prompt.")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without deleting.")
@click.pass_context
def reset_cmd(ctx: click.Context, all_flag: bool, force: bool, dry_run: bool) -> None:
    """Reset runtime state — code index and optionally the store.

    By default only the code index database is removed.
    Use --all to also remove the entire store (blocks, rubrics, runs).
    """
    root: Path = ctx.obj["root"]
    git_root = _detect_git_root(Path.cwd())

    targets: dict[str, list[Path]] = {}

    # Code index. When --all also wipes the store, the index DB lives under the
    # store root (workspaces/), so the store removal already covers it; listing
    # it separately would double-count. Only list it on its own when not --all
    # or when it lives outside the store being removed.
    if git_root:
        index_path = _code_index_db_path(git_root)
        index_under_store = False
        if all_flag and root.exists():
            try:
                index_path.resolve().relative_to(root.resolve())
                index_under_store = True
            except ValueError:
                index_under_store = False
        if index_path.exists() and not index_under_store:
            targets["code index"] = [index_path]

    # Store (--all only)
    if all_flag:
        store_paths = list(root.iterdir()) if root.exists() else []
        if store_paths:
            targets["store"] = store_paths

    if not targets:
        click.echo("nothing to reset")
        return

    if dry_run:
        click.echo("Would remove:")
        for section, paths in targets.items():
            click.echo(f"  {section}:")
            for p in sorted(paths):
                click.echo(f"    - {p}")
        return

    if not force:
        summary = "; ".join(f"{k}: {len(v)} items" for k, v in targets.items())
        click.confirm(f"Remove {summary}?", abort=True)

    for section, paths in targets.items():
        for p in paths:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        click.echo(f"removed {section} ({len(paths)} items)")


@click.command("uninstall")
@click.option("--dry-run", is_flag=True, help="Print planned actions and exit.")
@click.option("--no-hosts", is_flag=True, help="Skip per-host uninstallation.")
@click.option(
    "--purge",
    is_flag=True,
    help="Also remove runtime state, install dirs, tool envs, and known host residue.",
)
@click.option(
    "--workspace",
    type=click.Path(path_type=Path),
    help="Uninstall for a specific workspace.",
)
def uninstall(dry_run: bool, no_hosts: bool, purge: bool, workspace: Path | None) -> None:
    """Remove LemonCrow and all agent-host integrations."""
    root = _project_root()
    script = root / "scripts" / "uninstall.sh"
    if not script.exists():
        raise click.ClickException(f"uninstall script not found: {script}")

    cmd = ["bash", str(script)]
    if dry_run:
        cmd.append("--dry-run")
    if no_hosts:
        cmd.append("--no-hosts")
    if purge:
        cmd.append("--purge")
    if workspace:
        cmd.extend(["--workspace", str(workspace)])

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"uninstall failed with code {exc.returncode}") from exc


@click.group("env")
def env_group() -> None:
    """Validate named compatibility environments."""


@env_group.command("validate")
@click.argument("env_name")
@click.pass_context
def env_validate(ctx: click.Context, env_name: str) -> None:
    """Validate that a named environment contract exists."""
    store = _load_store(ctx.obj["root"])
    candidates = [env_name]
    suffix = env_name[4:] if env_name.startswith("env_") else env_name
    candidates.append(f"rubric_{suffix}")
    for rubric_id in candidates:
        if store.knowledge.get_rubric(rubric_id) is not None:
            click.echo(f"ok: {env_name}")
            return
    raise click.ClickException(f"unknown environment: {env_name}")


@click.command()
@click.argument("block_id")
@click.pass_context
def deprecate(ctx: click.Context, block_id: str) -> None:
    """Mark a block as deprecated."""
    store = _load_store(ctx.obj["root"])
    if not store.knowledge.update_block_status(block_id, "deprecated"):
        raise click.ClickException(f"block not found: {block_id}")
    click.echo(f"deprecated {block_id}")


@click.command()
@click.argument("block_id")
@click.pass_context
def quarantine(ctx: click.Context, block_id: str) -> None:
    """Quarantine a block (will not be retrieved)."""
    store = _load_store(ctx.obj["root"])
    if not store.knowledge.update_block_status(block_id, "quarantined"):
        raise click.ClickException(f"block not found: {block_id}")
    click.echo(f"quarantined {block_id}")


@click.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON of runs data.")
@click.option("--line", "line_mode", is_flag=True, help="One-liner mode (good for status bars).")
@click.option("-n", type=int, default=5, show_default=True, help="Number of recent runs to show.")
@click.option("--session-id", default=None, help="Show detail for a specific run only.")
@click.option(
    "--index",
    "index_mode",
    is_flag=True,
    help="Show code index stats for the current repo.",
)
@click.pass_context
def status_cmd(
    ctx: click.Context,
    as_json: bool,
    line_mode: bool,
    n: int,
    session_id: str | None,
    index_mode: bool,
) -> None:
    """Show runs dashboard or code index stats.

    Default view: runs dashboard (overview of recent runs, totals, savings).

    Use --index for index stats.
    """
    root: Path = ctx.obj["root"]

    if index_mode:
        git_root = _detect_git_root(Path.cwd())
        if git_root is None:
            click.echo("not in a git repository")
            return
        lines = _index_stats_pretty(git_root)
        for line in lines:
            click.echo(line)
        return

    if as_json:
        sessions_dir = root / "sessions"
        target: Path | None
        if session_id:
            from lemoncrow.core.foundation.paths import find_session_dir

            existing = find_session_dir(root, session_id)
            target = (existing / "run.json") if existing is not None else None
        else:
            files = sorted(sessions_dir.glob("**/run.json"), key=os.path.getmtime, reverse=True)
            target = files[0] if files else None
        if target and target.exists():
            click.echo(target.read_text().strip())
        else:
            click.echo("{}")
        return

    _render_dashboard(root, line_mode=line_mode, n_runs=n, session_id=session_id)


@click.command("share")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def share_cmd(ctx: click.Context, as_json: bool) -> None:
    """Render a local LemonCrow project share link."""
    del ctx
    payload = {"url": "https://lemoncrow.com", "text": "LemonCrow: https://lemoncrow.com"}
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(payload["text"])


@click.group(
    "settings",
    help=(
        "Manage local LemonCrow settings — every LEMONCROW_* env-var-backed knob plus the plugin toggles.\n"
        "\n"
        "Settings persist to ``<root>/plugin_settings.json`` and are applied to the "
        "process environment (via ``setdefault``) the next time LemonCrow starts, so "
        "an explicitly-exported env var always wins over a stored setting. Use "
        "``show --category <name>`` to browse one area "
        f"({', '.join(SETTINGS_CATEGORIES)})."
    ),
)
def plugin_settings_group() -> None:
    pass


def _format_setting_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@plugin_settings_group.command("show")
@click.option("--category", "category", default=None, help="Filter to one category (e.g. service, retrieval, mcp).")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def plugin_settings_show(ctx: click.Context, category: str | None, as_json: bool) -> None:
    """Show persisted settings, grouped by category."""
    from lemoncrow.core.settings import CATEGORIES, all_settings, load_settings

    if category is not None and category not in CATEGORIES:
        raise click.ClickException(f"unknown category: {category} (choices: {', '.join(CATEGORIES)})")
    payload = load_settings(ctx.obj["root"], category=category)
    if as_json:
        _emit(payload, as_json=True)
        return
    by_category: dict[str, list[str]] = {}
    for spec in all_settings():
        if spec.key not in payload:
            continue
        by_category.setdefault(spec.category, []).append(spec.key)
    for cat in sorted(by_category):
        click.echo(f"# {cat}")
        for key in sorted(by_category[cat]):
            click.echo(f"{key}: {_format_setting_value(payload[key])}")


@plugin_settings_group.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--json", "as_json", is_flag=True, help="Output the full settings payload as JSON after the write.")
@click.pass_context
def plugin_settings_set(ctx: click.Context, key: str, value: str, as_json: bool) -> None:
    """Set setting KEY to VALUE (validated, coerced, persisted to plugin_settings.json)."""
    from lemoncrow.core.reply_register import REPLY_REGISTER_LEVELS, TELEGRAPHIC_SETTING_KEY
    from lemoncrow.core.settings import load_settings, write_setting

    if key == TELEGRAPHIC_SETTING_KEY and value not in REPLY_REGISTER_LEVELS:
        raise click.ClickException(f"invalid value for {key}: {value!r} (choices: {', '.join(REPLY_REGISTER_LEVELS)})")
    try:
        coerced = write_setting(ctx.obj["root"], key, value)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if key == TELEGRAPHIC_SETTING_KEY:
        # The level is baked into installed personas — re-render them now.
        # apply_settings_env seeded the env before this write; refresh it so
        # the regeneration below sees the new value.
        os.environ["LEMONCROW_TELEGRAPHIC"] = str(coerced)
        from lemoncrow.gateway.cli.commands.agents_skills import reapply_installed_agents

        if reapply_installed_agents() == 0:
            click.echo("no installed host agents found — level applies on the next host install")
    if as_json:
        _emit(load_settings(ctx.obj["root"]), as_json=True)
        return
    click.echo(f"set {key}={_format_setting_value(coerced)}")


@click.command("tool-report")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def tool_report_cmd(ctx: click.Context, as_json: bool) -> None:
    """Tool usage + savings summary including redundancy analysis."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.tool_report()
    if as_json:
        _emit(payload, as_json=True)
        return
    metrics = payload.get("metrics", {})
    click.echo(f"total_tool_calls: {metrics.get('total_tool_calls', 0)}")
    click.echo(f"avoided_tool_calls: {metrics.get('avoided_tool_calls', 0)}")
    click.echo(f"token_savings: {metrics.get('token_savings', 0)}")
    click.echo(f"cache_hit_rate: {metrics.get('cache_hit_rate', 0)}")
    recs = payload.get("recommendations", [])
    if recs:
        click.echo("recommendations:")
        for r in recs:
            click.echo(f"  - {r}")


@click.group("audit")
def audit_group() -> None:
    """Audit local developer context and shell usage."""


@click.command("insights")
@click.option(
    "--since",
    default="7d",
    show_default=True,
    help="Time window: '7d', '30d', '24h', or a date like '2026-05-01'.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.option("--no-color", is_flag=True, default=False, help="Disable ANSI colours.")
@click.option(
    "--vendor",
    default=None,
    help="Filter output to a specific vendor (e.g. 'anthropic').",
)
@click.option(
    "--group-by",
    "group_by",
    default="tool",
    type=click.Choice(["tool", "vendor", "model", "session"]),
    show_default=True,
    help="Primary grouping for cost breakdown.",
)
@click.pass_context
def insights_cmd(
    ctx: click.Context,
    since: str,
    as_json: bool,
    no_color: bool,
    vendor: str | None,
    group_by: str,
) -> None:
    """Weekly AI-spend insights and savings opportunities."""
    from lemoncrow.pro.runtime.insights import (
        InsightsWindow,
        build_insights,
        render_json,
        render_text,
    )

    root: Path = ctx.obj["root"]
    since_dt = _parse_since_arg(since)
    until_dt = datetime.now(UTC)

    window: InsightsWindow = build_insights(root, since=since_dt, until=until_dt)

    if window.session_count == 0:
        if as_json:
            click.echo(render_json(window))
        else:
            since_str = since_dt.strftime("%Y-%m-%d")
            click.echo(f"No sessions found since {since_str}.")
        return

    cost_by_vendor = window.cost_by_vendor
    if vendor:
        vendor_key = vendor.capitalize()
        filtered_cost = window.cost_by_vendor.get(vendor_key, 0.0)
        cost_by_vendor = {vendor_key: filtered_cost}
        if not as_json:
            click.echo(f"Vendor filter: {vendor_key}  ${filtered_cost:.2f} of ${window.total_cost_usd:.2f} total")

    if group_by == "model":
        cost_by_tool = window.cost_by_model
    elif group_by == "session":
        cost_by_tool = {s.session_id[:8]: s.cost_usd for s in window.top_sessions}
    elif group_by == "vendor":
        cost_by_tool = cost_by_vendor
    else:
        cost_by_tool = window.cost_by_tool

    display_window = InsightsWindow(
        since=window.since,
        until=window.until,
        session_count=window.session_count,
        total_duration_seconds=window.total_duration_seconds,
        total_cost_usd=window.total_cost_usd,
        total_lemoncrow_savings_usd=window.total_lemoncrow_savings_usd,
        cost_by_vendor=cost_by_vendor,
        cost_by_tool=cost_by_tool,
        cost_by_model=window.cost_by_model,
        top_sessions=window.top_sessions,
        outcomes_summary=window.outcomes_summary,
        opportunities=window.opportunities,
    )

    if as_json:
        click.echo(render_json(display_window))
    else:
        click.echo(render_text(display_window, no_color=no_color))


__all__ = [
    "_project_root",
    "audit_group",
    "deprecate",
    "doctor_cmd",
    "env_group",
    "init",
    "insights_cmd",
    "plugin_settings_group",
    "quarantine",
    "reset_cmd",
    "share_cmd",
    "status_cmd",
    "tool_report_cmd",
    "uninstall",
]
