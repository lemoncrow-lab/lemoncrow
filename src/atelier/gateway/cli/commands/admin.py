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

from atelier.core.capabilities.model_settings import (
    TOP_MODEL_CHOICES,
    build_runtime_settings_payload,
    resolve_host_model,
    resolve_runtime_model,
    set_host_role_models,
    write_workspace_model_settings,
)
from atelier.core.capabilities.plugin_runtime import ATTRIBUTION_EMAIL, ATTRIBUTION_NAME
from atelier.core.capabilities.reporting.dashboard import _render_dashboard
from atelier.core.capabilities.workspace_host_overrides import (
    write_workspace_agents_md,
    write_workspace_claude_overrides,
    write_workspace_codex_agent_config,
    write_workspace_codex_agents,
    write_workspace_copilot_agents,
    write_workspace_cursor_rules,
    write_workspace_opencode_agents,
)
from atelier.core.foundation.models import Playbook, Rubric
from atelier.core.foundation.paths import detect_host
from atelier.gateway.cli.commands._shared import (
    _core_runtime,
    _emit,
    _load_store,
)
from atelier.gateway.integrations.openmemory_lifecycle import project_root as _project_root


def _detect_git_root(search_path: Path) -> Path | None:
    """Return the git repo root containing search_path, or None if not in a repo.

    Walks upward for a ``.git`` entry (a directory in a normal clone, a file in a
    worktree/submodule) rather than shelling out to ``git rev-parse``. Forking a
    git subprocess from this fully-imported, multi-threaded process costs seconds
    (page-table copy of a large parent), while the walk is a few ``stat`` calls
    and needs no ``git`` binary.
    """
    try:
        current = search_path.resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        git_entry = candidate / ".git"
        # A worktree/submodule uses a `.git` *file* (a gitlink); a normal clone
        # uses a `.git` *directory*, which always contains HEAD. Requiring HEAD
        # matches `git rev-parse` and rejects stray/empty `.git` dirs.
        if git_entry.is_file() or (git_entry.is_dir() and (git_entry / "HEAD").is_file()):
            return candidate
    return None


from atelier.core.foundation.paths import ensure_gitignore as _ensure_gitignore  # noqa: E402

_RUNTIME_ROLE_PROMPT_ORDER = ("code", "execute", "solve", "general", "explore", "plan", "research", "review")
_HOST_ROLE_PROMPT_ORDER = ("code", "execute", "solve", "explore", "plan", "research", "review")
_CUSTOM_MODEL_OPTION = "Others (Enter model)"


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
    click.secho(prompt, fg="magenta")
    rendered = 0
    while True:
        if rendered:
            _clear_rendered_lines(rendered)
        lines = [
            *(f"  {'▸ ●' if index == selected else '  ○'}  {option}" for index, option in enumerate(options)),
            "",
            "  ↑↓ navigate  ·  enter select",
        ]
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
    click.echo(f"  ●  {options[selected]}")
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
    click.secho(label, fg="magenta")
    while True:
        if rendered:
            _clear_rendered_lines(rendered)

        lines: list[str] = []
        for index, option in enumerate(options):
            marker = "▸ ●" if index == selected else "  ○"
            if option == _CUSTOM_MODEL_OPTION and index == selected:
                value = custom_value or click.style("Type custom model...", dim=True)
                if custom_value:
                    value = custom_value
                lines.append(f"  {marker}  {value}")
            else:
                lines.append(f"  {marker}  {option}")
        if options[selected] == _CUSTOM_MODEL_OPTION:
            lines.extend(["", "  type to edit  ·  enter confirm  ·  esc cancel"])
        else:
            lines.extend(["", "  ↑↓ navigate  ·  enter select"])

        for line in lines:
            click.echo(line)
        rendered = len(lines)

        key = _read_selector_key()
        if options[selected] == _CUSTOM_MODEL_OPTION:
            if key in ("\r", "\n"):
                if custom_value.strip():
                    _clear_rendered_lines(rendered)
                    click.echo(f"  ●  {custom_value.strip()}")
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
            click.echo(f"  ●  {options[selected]}")
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
            ("Customize now", "Keep current defaults"),
            default="Customize now",
        )
        return selection == "Customize now"
    return click.confirm("Customize role models?", default=True)


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
        "antigravity": "Antigravity",
        "cursor": "Cursor",
        "hermes": "Hermes",
    }.get(host, host.title())


def _prompt_workspace_model_config(workspace_root: Path) -> dict[str, Any] | None:
    click.echo("")
    click.echo("Atelier workspace model configuration.")
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
        results["copilot"] = [f"updated {len(copilot_agents)} workspace-local Copilot files"]
    if "claude" in detected:
        claude_paths = write_workspace_claude_overrides(workspace_root)
        results["claude"] = [f"updated {len(claude_paths)} workspace-local Claude files"]
    if "opencode" in detected:
        opencode_agents = write_workspace_opencode_agents(workspace_root)
        results["opencode"] = [f"updated {len(opencode_agents)} workspace-local OpenCode agents"]
    if "codex" in detected:
        codex_agents = write_workspace_codex_agents(workspace_root)
        results["codex"] = [f"updated {len(codex_agents)} workspace-local Codex agents"]
    if "cursor" in detected:
        cursor_rules = write_workspace_cursor_rules(workspace_root)
        results["cursor"] = [f"updated {len(cursor_rules)} workspace-local Cursor rule files"]
    return results


def _install_attribution_hook(git_root: Path) -> str | None:
    """Install the Atelier co-author ``prepare-commit-msg`` hook.

    Resolves the active hooks directory via ``git rev-parse --git-path hooks``
    (respects ``core.hooksPath``). Idempotent — skips if the Atelier marker is
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
    marker = "# >>> atelier attribution >>>"
    end_marker = "# <<< atelier attribution <<<"
    trailer = f"Co-Authored-By: {ATTRIBUTION_NAME} <{ATTRIBUTION_EMAIL}>"

    if hook_path.exists() and marker in hook_path.read_text(encoding="utf-8"):
        return None  # already installed

    hooks_dir.mkdir(parents=True, exist_ok=True)
    content = (
        "#!/usr/bin/env bash\n"
        "\n"
        f"{marker}\n"
        "# Managed by Atelier (atelier init). Appends the co-author trailer unless already present.\n"
        "# Skips merge/squash commit messages.\n"
        f'ATELIER_TRAILER="{trailer}"\n'
        'case "$2" in\n'
        "  merge|squash) ;;\n"
        "  *)\n"
        '    if ! grep -qF "$ATELIER_TRAILER" "$1" 2>/dev/null; then\n'
        '      printf \'\\n%s\\n\' "$ATELIER_TRAILER" >> "$1"\n'
        "    fi\n"
        "    ;;\n"
        "esac\n"
        f"{end_marker}\n"
    )
    hook_path.write_text(content, encoding="utf-8")
    hook_path.chmod(0o755)
    return f"Atelier co-author hook installed at {hook_path.relative_to(git_root)}"


def _project_init_setup(git_root: Path) -> dict[str, list[str]]:
    """Run project-scoped init steps inside a git repo.

    Returns a dict of ``{section: [messages]}`` describing what was done.
    """
    results: dict[str, list[str]] = {}

    # .gitignore \u2014 write .atelier/.gitignore so the dir stays visible in git
    added = _ensure_gitignore(git_root)
    if added:
        results["gitignore"] = [".atelier/.gitignore written (ignores everything inside .atelier/)"]
    else:
        results["gitignore"] = [".atelier/.gitignore already present"]

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
            f"updated {len(codex_agents)} workspace-local Codex agents",
            f"updated {codex_config.relative_to(git_root)}",
        ]

    # Attribution hook — write via the active hooks directory (respects core.hooksPath)
    hook_msg = _install_attribution_hook(git_root)
    if hook_msg:
        results["attribution"] = [hook_msg]

    return results


def _code_index_db_path(repo_root: Path) -> Path:
    """Return the default code index database path for a repo."""
    from atelier.core.foundation.paths import default_store_root, workspace_key

    workspace_hash = workspace_key(repo_root.resolve())
    return default_store_root() / "workspaces" / workspace_hash / "code_context.sqlite"


def _index_stats_pretty(repo_root: Path) -> list[str]:
    """Return human-readable index stats lines for a repo."""
    db_path = _code_index_db_path(repo_root)
    if not db_path.exists():
        return ["(no index — run `atelier code index` first)"]
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
    blocks_dir = resources.files("atelier") / "infra" / "seed_playbooks"
    rubrics_dir = resources.files("atelier") / "core" / "rubrics"
    block_files = sorted(Path(str(p)) for p in blocks_dir.iterdir() if p.name.endswith(".yaml"))
    rubric_files = sorted(Path(str(p)) for p in rubrics_dir.iterdir() if p.name.endswith(".yaml"))
    return block_files, rubric_files


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_domain_manager(root: Path) -> Any:
    from atelier.core.domains import DomainManager

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
    "--configure-models/--no-configure-models",
    default=None,
    help="Prompt for project-local role/host model settings when running inside a git repo.",
)
@click.pass_context
def init(
    ctx: click.Context,
    seed: bool,
    index: bool,
    configure_models: bool | None,
) -> None:
    """Initialize the runtime store at --root."""
    root: Path = ctx.obj["root"]
    # A non-git, never-registered cwd must be marked BEFORE `create_store`:
    # ContextStore resolves the active workspace root internally (for its
    # blocks/rubrics mirror dir under the store root), which now requires cwd
    # to be either a git repo or already `atelier init`-registered. Without
    # this, the very first `init` run in a fresh non-git directory would
    # raise WorkspaceNotRegisteredError from inside `create_store` before this
    # command ever gets a chance to register the directory itself below.
    if _detect_git_root(Path.cwd()) is None:
        _ensure_gitignore(Path.cwd())
    from atelier.infra.storage.factory import create_store

    try:
        store = create_store(root)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    store.init()
    click.echo(f"initialized atelier store at {store.root}")
    if seed:
        block_files, rubric_files = _seed_resources()
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
            store.upsert_block(block)
            n_b += 1
        n_r = 0
        for path in rubric_files:
            data = _load_yaml(path)
            try:
                rubric = Rubric.model_validate(data)
            except (KeyError, ValueError) as exc:
                raise click.ClickException(f"invalid seed rubric {path}: {exc}") from exc
            store.upsert_rubric(rubric)
            n_r += 1
        click.echo(f"seeded {n_b} playbooks and {n_r} rubrics")
    if index:
        git_root = _detect_git_root(Path.cwd())
        if git_root is not None:
            from atelier.gateway.cli.commands.code import (
                _code_context_engine,
                _index_repo_with_progress,
            )

            engine = _code_context_engine(str(git_root))
            stats = _index_repo_with_progress(
                engine,
                description="Bootstrapping code index",
                success_description="Code index ready",
            )
            click.echo(
                f"indexed {stats['files_indexed']} files, "
                f"{stats['symbols_indexed']} symbols "
                f"({stats['imports_indexed']} imports)"
            )
        else:
            click.echo("code index skipped (no git repository detected in current directory)")
    git_root = _detect_git_root(Path.cwd())
    if git_root is not None:
        results = _project_init_setup(git_root)
        for section, messages in results.items():
            for msg in messages:
                click.echo(f"  [{section}] {msg}")
    else:
        _ensure_gitignore(Path.cwd())
        click.echo(f"registered {Path.cwd()} as an Atelier workspace (no git repository detected)")
    should_offer_model_config = bool(git_root is not None and _is_interactive_terminal())
    if configure_models and not should_offer_model_config:
        raise click.ClickException("--configure-models requires an interactive terminal inside a git repository.")
    if should_offer_model_config and configure_models is not False:
        assert git_root is not None
        payload = _prompt_workspace_model_config(git_root)
        if payload is not None:
            results = _apply_workspace_model_config(
                git_root, payload, detected_hosts=_detected_workspace_hosts(git_root)
            )
            for section, messages in results.items():
                for msg in messages:
                    click.echo(f"  [{section}] {msg}")


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def doctor_cmd(ctx: click.Context, as_json: bool) -> None:
    """Run diagnostics on the Atelier installation."""
    checks: dict[str, Any] = {}

    # Python version
    py_ok = sys.version_info >= (3, 10)
    checks["python"] = {
        "ok": py_ok,
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    }

    # Git repo
    git_root = _detect_git_root(Path.cwd())
    checks["git_repo"] = {
        "ok": git_root is not None,
        "path": str(git_root) if git_root else None,
    }

    # Atelier store
    root: Path = ctx.obj["root"]
    store_ok = root.exists() and (root / "atelier.db").exists()
    checks["store"] = {
        "ok": store_ok,
        "root": str(root),
        "exists": root.exists(),
    }

    # Code index
    if git_root:
        index_path = _code_index_db_path(git_root)
        index_ok = index_path.exists()
        stats = _index_stats_pretty(git_root) if index_ok else []
        checks["code_index"] = {
            "ok": index_ok,
            "path": str(index_path),
            "stats": stats if stats else None,
        }
    else:
        checks["code_index"] = {"ok": False, "path": None, "stats": None}

    # External compactors (optional soft integrations, e.g. rtk). Absence is
    # never a failure -- Atelier falls back to the plain shell path.
    from atelier.core.capabilities.tool_supervision.external_compactors import (
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
            hint = "installed but disabled (ATELIER_BASH_EXTERNAL_COMPACTORS=0)"
        checks[compactor.name] = {
            "ok": True,  # optional -- absence never fails diagnostics
            "optional": True,
            "installed": resolution.available,
            "enabled": compactors_enabled,
            "path": str(resolution.path) if resolution.path else None,
            "version": resolution.version,
            "hint": hint,
        }

    if as_json:
        _emit(checks, as_json=True)
        return

    click.echo("Atelier diagnostics")
    click.echo("==================")
    for name, info in checks.items():
        if info.get("optional") and not info.get("installed"):
            status = "○"  # optional and absent -- informational, not a failure
        else:
            status = "✓" if info.get("ok") else "✗"
        click.echo(f"  {status} {name}")
        if info.get("version"):
            click.echo(f"       version: {info['version']}")
        if info.get("path"):
            click.echo(f"       path: {info['path']}")
        if info.get("root"):
            click.echo(f"       root: {info['root']}")
        if info.get("hint"):
            click.echo(f"       {info['hint']}")
        if info.get("stats"):
            for line in info["stats"]:
                click.echo(f"       {line}")


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
    """Remove Atelier and all agent-host integrations."""
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
        if store.get_rubric(rubric_id) is not None:
            click.echo(f"ok: {env_name}")
            return
    raise click.ClickException(f"unknown environment: {env_name}")


@click.command()
@click.argument("block_id")
@click.pass_context
def deprecate(ctx: click.Context, block_id: str) -> None:
    """Mark a block as deprecated."""
    store = _load_store(ctx.obj["root"])
    if not store.update_block_status(block_id, "deprecated"):
        raise click.ClickException(f"block not found: {block_id}")
    click.echo(f"deprecated {block_id}")


@click.command()
@click.argument("block_id")
@click.pass_context
def quarantine(ctx: click.Context, block_id: str) -> None:
    """Quarantine a block (will not be retrieved)."""
    store = _load_store(ctx.obj["root"])
    if not store.update_block_status(block_id, "quarantined"):
        raise click.ClickException(f"block not found: {block_id}")
    click.echo(f"quarantined {block_id}")


def _auth_status(root: Path, as_json: bool) -> None:
    """Show auth/subscription status: email, plan, and device slots."""
    from atelier.core.capabilities.licensing.store import (
        load_auth_base,
        load_auth_token,
        load_auth_user,
        save_auth_user,
    )

    auth_token = load_auth_token()

    # Always fetch live for status — disk cache is for background entitlement
    # checks, not explicit status queries.
    cached: dict[str, object] | None = None
    if auth_token:
        import json as _json
        import urllib.request

        from atelier.core.capabilities.licensing.entitlements import USER_AGENT

        _base_url = load_auth_base()
        try:
            req = urllib.request.Request(
                f"{_base_url}/api/auth/me",
                headers={"Authorization": f"Bearer {auth_token}", "User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                cached = _json.loads(resp.read())
            save_auth_user({**cached, "_base": _base_url})
        except Exception:  # noqa: BLE001
            cached = load_auth_user()  # fall back to disk if offline

    if auth_token and cached:
        # OAuth session — show cached user info
        email = str(cached.get("email") or "")
        plan = str(cached.get("plan") or "free")
        device_id = str(cached.get("device_id") or auth_token[:8])
        cli_count = int(cached.get("cli_device_count") or 0)  # type: ignore[call-overload]
        cli_limit = int(cached.get("cli_device_limit") or 3)  # type: ignore[call-overload]
        if as_json:
            _emit(
                {
                    "authenticated": True,
                    "email": email,
                    "plan": plan,
                    "device_id": device_id,
                    "cli_devices": f"{cli_count}/{cli_limit}",
                    "mode": "oauth",
                },
                as_json=True,
            )
            return
        click.secho(f"✓ {email}", fg="green", bold=True)
        click.echo(f"  plan:    {plan}")
        click.echo(f"  device:  {device_id}")
        click.echo(f"  devices: {cli_count} of {cli_limit} used")
    elif auth_token and not cached:
        # fetch failed and no disk fallback
        if as_json:
            _emit({"mode": "oauth", "status": "unreachable"}, as_json=True)
            return
        click.secho("⚠ Could not reach auth server", fg="yellow")
    else:
        # No OAuth token — check plugin-runtime auth state (written by `login --token`),
        # which lives at root/auth.json and is distinct from the global licensing store.
        import json as _json2

        from atelier.core.capabilities.plugin_runtime import auth_state_path as _auth_state_path

        _plugin_auth: dict[str, Any] | None = None
        try:
            _plugin_auth_path = _auth_state_path(root)
            if _plugin_auth_path.exists():
                _plugin_auth = _json2.loads(_plugin_auth_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

        if isinstance(_plugin_auth, dict) and _plugin_auth.get("authenticated") and _plugin_auth.get("email"):
            email = str(_plugin_auth["email"])
            if as_json:
                _emit({"authenticated": True, "email": email, "mode": "token"}, as_json=True)
                return
            click.secho(f"✓ {email}", fg="green", bold=True)
        else:
            if as_json:
                _emit({"mode": "none", "status": "not logged in"}, as_json=True)
                return
            click.secho("✗ Not logged in — run: atelier login", fg="red")


@click.command("login")
@click.option("--token", default=None, help="Credentials JSON, base64 payload, or refresh token.")
@click.option("--anonymous", "anonymous", is_flag=True, help="Start a local anonymous trial.")
@click.option("--status", "status_mode", is_flag=True, help="Show auth/subscription status and exit.")
@click.option("--json", "as_json", is_flag=True)
@click.option("--dev", "dev_mode", is_flag=True, help="Login against local dev server (http://localhost:4321).")
@click.pass_context
def login_cmd(
    ctx: click.Context, token: str | None, anonymous: bool, status_mode: bool, as_json: bool, dev_mode: bool
) -> None:
    """Create local Atelier auth state for plugin operations.

    Use --status to show the current auth/subscription state instead.
    """
    from atelier.core.capabilities.plugin_runtime import (
        claim_anonymous_trial,
        parse_login_token,
        write_auth_state,
    )

    if status_mode:
        _auth_status(ctx.obj["root"], as_json)
        return

    if anonymous:
        payload = {"auth": claim_anonymous_trial(ctx.obj["root"]), "mode": "anonymous"}
        if as_json:
            _emit(payload, as_json=True)
            return
        auth_payload = payload.get("auth")
        auth = auth_payload if isinstance(auth_payload, dict) else {}
        label = "anonymous trial" if auth.get("isAnonymous") else auth.get("email") or auth.get("userId")
        click.echo(f"logged in: {label}")
    elif token:
        payload = {
            "auth": write_auth_state(ctx.obj["root"], parse_login_token(token)),
            "mode": "token",
        }
        if as_json:
            _emit(payload, as_json=True)
            return
        auth_payload = payload.get("auth")
        auth = auth_payload if isinstance(auth_payload, dict) else {}
        label = auth.get("email") or auth.get("userId")
        click.echo(f"logged in: {label}")
    else:
        _oauth_login(as_json, dev_mode=dev_mode)


def _oauth_login(as_json: bool, dev_mode: bool = False) -> None:
    """Run the OAuth browser flow and persist the returned session token."""
    import http.server
    import json
    import socket
    import threading
    import urllib.parse
    import urllib.request
    import webbrowser

    from atelier.core.capabilities.licensing.store import (
        load_or_create_device_id,
        save_auth_base,
        save_auth_token,
        save_auth_user,
    )

    base = "http://localhost:4321" if dev_mode else "https://atelier.ws"

    # Always open the browser — atelier login is intentional and issues a fresh device token.

    # Find a free port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    import platform

    hostname = platform.node() or "cli"
    stable_device_id = load_or_create_device_id()
    cli_redirect = f"http://localhost:{port}/callback"
    oauth_url = (
        f"{base}/account"
        f"?cli_redirect={urllib.parse.quote(cli_redirect, safe='')}"
        f"&device_name={urllib.parse.quote(hostname, safe='')}"
        f"&stable_device_id={urllib.parse.quote(stable_device_id, safe='')}"
    )

    received: dict[str, str] = {}
    server_ready = threading.Event()
    shutdown_event = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/callback":
                qs = urllib.parse.parse_qs(parsed.query)
                received["token"] = qs.get("token", [""])[0]
                received["email"] = qs.get("email", [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><p>Logged in. You can close this tab.</p>"
                    b"<script>window.close()</script></body></html>"
                )
            else:
                self.send_response(404)
                self.end_headers()
            shutdown_event.set()

        def log_message(self, *args: object) -> None:
            pass  # suppress access log

    httpd = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    httpd.timeout = 1

    def _serve() -> None:
        server_ready.set()
        deadline = 120
        import time

        start = time.monotonic()
        while not shutdown_event.is_set() and time.monotonic() - start < deadline:
            httpd.handle_request()
        httpd.server_close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    server_ready.wait()

    click.secho("Opening browser to sign in...", fg="cyan", dim=True)
    webbrowser.open(oauth_url)

    shutdown_event.wait(timeout=120)
    thread.join(timeout=5)

    session_token = received.get("token", "")
    email = received.get("email", "")

    if not session_token:
        click.secho("✗ Login timed out or was cancelled.", fg="red", err=True)
        raise SystemExit(1)

    save_auth_token(session_token)

    # Best-effort: fetch plan + device_id from server
    plan = "free"
    device_id = session_token[:8]
    try:
        from atelier.core.capabilities.licensing.entitlements import USER_AGENT

        req = urllib.request.Request(
            f"{base}/api/auth/me",
            headers={"Authorization": f"Bearer {session_token}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data: dict[str, object] = json.loads(resp.read())
        plan = str(data.get("plan") or plan)
        device_id = str(data.get("device_id") or device_id)
        save_auth_user({**data, "_base": base})
        save_auth_base(base)
    except Exception:  # noqa: BLE001
        pass

    if as_json:
        _emit({"email": email, "plan": plan, "device_id": device_id, "mode": "oauth"}, as_json=True)
        return
    click.secho(f"✓ Logged in as {email} ({plan}) · device {device_id}", fg="green")
    if plan == "free":
        from atelier.core.capabilities.licensing import pro_url

        click.secho(
            f"💡 You're on Free. Pro ($19/mo) unlocks large-repo search & indexing, "
            f"cross-session memory, the savings engine, model routing, and multi-repo "
            f"swarm — upgrade at {pro_url()}",
            fg="cyan",
        )
    elif plan == "pro":
        click.secho(
            "✨ Pro is active — large-repo search, cross-session memory, savings engine, "
            "model routing & multi-repo swarm all unlocked. Thanks for supporting Atelier!",
            fg="cyan",
        )


@click.command("logout")
@click.option("--no-trial", is_flag=True, help="Do not create a local anonymous trial after logout.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def logout_cmd(ctx: click.Context, no_trial: bool, as_json: bool) -> None:
    """Remove local auth and optionally activate an anonymous trial."""
    from atelier.core.capabilities.licensing.store import delete_auth_base, delete_auth_token, delete_auth_user
    from atelier.core.capabilities.plugin_runtime import logout_local

    delete_auth_token()
    delete_auth_user()
    delete_auth_base()
    payload = logout_local(ctx.obj["root"], claim_trial=not no_trial)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.secho("✓ Logged out", fg="green")


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

    Use --index for index stats; `atelier login --status` for auth status.
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
            from atelier.core.foundation.paths import find_session_dir

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
    """Render local referral/share text."""
    from atelier.core.capabilities.plugin_runtime import share_referral

    payload = share_referral(ctx.obj["root"])
    if payload.get("is_error"):
        raise click.ClickException(str(payload["message"]))
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(payload["text"])


@click.group("settings")
def plugin_settings_group() -> None:
    """Manage local Atelier settings — every ATELIER_* env-var-backed knob plus the plugin toggles.

    Settings persist to ``<root>/plugin_settings.json`` and are applied to the
    process environment (via ``setdefault``) the next time Atelier starts, so
    an explicitly-exported env var always wins over a stored setting. Use
    ``show --category <name>`` to browse one area (service, retrieval,
    embeddings, code_context, tool_supervision, mcp, statusline, telemetry,
    memory, swarm, zoekt, bench, routing, llm, licensing, lessons, cli, core,
    plugin, internal).
    """


def _format_setting_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@plugin_settings_group.command("show")
@click.option("--category", "category", default=None, help="Filter to one category (e.g. service, retrieval, mcp).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def plugin_settings_show(ctx: click.Context, category: str | None, as_json: bool) -> None:
    from atelier.core.settings import CATEGORIES, all_settings, load_settings

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
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def plugin_settings_set(ctx: click.Context, key: str, value: str, as_json: bool) -> None:
    from atelier.core.reply_register import REPLY_REGISTER_LEVELS, TELEGRAPHIC_SETTING_KEY
    from atelier.core.settings import load_settings, write_setting

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
        os.environ["ATELIER_TELEGRAPHIC"] = str(coerced)
        from atelier.gateway.cli.commands.agents_skills import reapply_installed_agents

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


@click.group("team")
def team_group() -> None:
    """Manage local team workspace state."""


@team_group.command("init")
@click.option("--name", required=True, help="Workspace display name.")
@click.option("--admin-email", default="admin@local", show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_init_cmd(ctx: click.Context, name: str, admin_email: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    workspace = TeamWorkspaceManager(ctx.obj["root"]).init_workspace(name=name, admin_email=admin_email)
    payload = workspace.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"initialized workspace {workspace.name} ({workspace.id})")


@team_group.command("invite")
@click.argument("emails", nargs=-1)
@click.option("--role", type=click.Choice(["member", "viewer", "admin"]), default="member", show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_invite_cmd(ctx: click.Context, emails: tuple[str, ...], role: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    if not emails:
        raise click.ClickException("provide at least one email")
    invites = TeamWorkspaceManager(ctx.obj["root"]).invite_members(list(emails), role=role)  # type: ignore[arg-type]
    payload = [invite.model_dump(mode="json") for invite in invites]
    if as_json:
        _emit(payload, as_json=True)
        return
    for invite in invites:
        click.echo(f"{invite.email}\t{invite.role}\t{invite.code}")


@team_group.command("join")
@click.argument("invite_code")
@click.option("--user-id", default=None, help="Override the invite email as the local user id.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_join_cmd(ctx: click.Context, invite_code: str, user_id: str | None, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    member = TeamWorkspaceManager(ctx.obj["root"]).join_workspace(invite_code, user_id=user_id)
    payload = member.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"joined workspace as {member.user_id} ({member.role})")


@team_group.command("role")
@click.argument("user_id")
@click.argument("role", type=click.Choice(["admin", "member", "viewer"]))
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_role_cmd(ctx: click.Context, user_id: str, role: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    member = TeamWorkspaceManager(ctx.obj["root"]).set_role(user_id, role)  # type: ignore[arg-type]
    payload = member.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"{member.user_id}\t{member.role}")


@team_group.command("usage")
@click.option("--since", default="30d", show_default=True, help="Time window like 30d, 24h, or 2026-05-01.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_usage_cmd(ctx: click.Context, since: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager, summarize_workspace_usage

    manager = TeamWorkspaceManager(ctx.obj["root"])
    manager.require_admin()
    payload = summarize_workspace_usage(ctx.obj["root"], manager=manager, since=_parse_since_arg(since))
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"workspace: {payload['workspace_id']}")
    click.echo(f"sessions: {payload['session_count']}")
    click.echo(f"total cost usd: {payload['total_cost_usd']:.6f}")
    for row in payload["users"]:
        click.echo(f"{row['user_id']}\t{row['role']}\t{row['session_count']}\t{row['total_cost_usd']:.6f}")


@team_group.command("audit")
@click.option("--since", default="30d", show_default=True, help="Time window like 30d, 24h, or 2026-05-01.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_audit_cmd(ctx: click.Context, since: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    manager = TeamWorkspaceManager(ctx.obj["root"])
    manager.require_admin()
    events = manager.list_audit_events(since=_parse_since_arg(since))
    payload = [event.model_dump(mode="json") for event in events]
    if as_json:
        _emit(payload, as_json=True)
        return
    if not events:
        click.echo("(no team audit events)")
        return
    for event in events:
        click.echo(f"{event.at.isoformat()}\t{event.action}\t{event.actor_user_id}")


@click.group("governance")
def governance_group() -> None:
    """Inspect and apply workspace governance policy."""


@governance_group.command("show")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def governance_show_cmd(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.capabilities.governance import load_policy

    policy = load_policy(ctx.obj["root"])
    payload = policy.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(yaml.safe_dump(payload, sort_keys=True).rstrip())


@governance_group.command("apply")
@click.option(
    "--file",
    "file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def governance_apply_cmd(ctx: click.Context, file_path: Path, as_json: bool) -> None:
    from atelier.core.capabilities.governance import GovernancePolicy, save_policy
    from atelier.core.capabilities.team import TeamAuditEvent, TeamWorkspaceManager

    manager = TeamWorkspaceManager(ctx.obj["root"])
    member = manager.require_admin()
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    policy = GovernancePolicy.model_validate(loaded)
    saved = save_policy(ctx.obj["root"], policy)
    manager.append_audit_event(
        TeamAuditEvent(
            action="governance.apply",
            actor_user_id=member.user_id,
            details={"source": str(file_path)},
        )
    )
    payload = saved.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"applied governance policy from {file_path}")


@click.group("audit")
def audit_group() -> None:
    """Export and verify workspace audit bundles."""


@audit_group.command("export")
@click.option("--since", default="30d", show_default=True, help="Time window like 30d, 24h, or 2026-05-01.")
@click.option("--out", "out_dir", required=True, type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def audit_export_cmd(ctx: click.Context, since: str, out_dir: Path, as_json: bool) -> None:
    from atelier.core.capabilities.audit_export import export_audit_bundle
    from atelier.core.capabilities.team import TeamAuditEvent, TeamWorkspaceManager

    manager = TeamWorkspaceManager(ctx.obj["root"])
    member = manager.require_admin()
    payload = export_audit_bundle(ctx.obj["root"], out_dir=out_dir, since=_parse_since_arg(since))
    manager.append_audit_event(
        TeamAuditEvent(
            action="audit.export",
            actor_user_id=member.user_id,
            details={"bundle_dir": payload["bundle_dir"]},
        )
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(payload["bundle_dir"])


@audit_group.command("verify")
@click.argument("bundle_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def audit_verify_cmd(ctx: click.Context, bundle_dir: Path, as_json: bool) -> None:
    from atelier.core.capabilities.audit_export import verify_audit_bundle

    payload = verify_audit_bundle(ctx.obj["root"], bundle_dir=bundle_dir)
    if as_json:
        _emit(payload, as_json=True)
        return
    if payload["valid"]:
        click.echo(f"verified {bundle_dir}")
        return
    raise click.ClickException(
        f"bundle verification failed: {', '.join(payload['tampered_files']) or 'signature mismatch'}"
    )


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
    from atelier.infra.runtime.insights import (
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
        total_atelier_savings_usd=window.total_atelier_savings_usd,
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
    "governance_group",
    "init",
    "insights_cmd",
    "login_cmd",
    "logout_cmd",
    "plugin_settings_group",
    "quarantine",
    "reset_cmd",
    "share_cmd",
    "status_cmd",
    "team_group",
    "tool_report_cmd",
    "uninstall",
]
