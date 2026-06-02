from __future__ import annotations

import hashlib
import json
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

from atelier.core.capabilities.reporting.dashboard import _render_dashboard
from atelier.core.foundation.models import ReasonBlock, Rubric
from atelier.gateway.cli.commands._dev import dev_command as _dev_command
from atelier.gateway.cli.commands._shared import (
    _core_runtime,
    _emit,
    _load_store,
)
from atelier.gateway.integrations.openmemory_lifecycle import project_root as _project_root


def _detect_git_root(search_path: Path) -> Path | None:
    """Return the git repo root containing search_path, or None if not in a repo."""
    import subprocess as _subprocess

    try:
        result = _subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(search_path),
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (OSError, _subprocess.SubprocessError):
        return None
    return None


def _ensure_gitignore(git_root: Path) -> list[str]:
    """Add ``.atelier/`` to the project's ``.gitignore`` if not present.

    Returns a list of entries added.
    """
    gitignore_path = git_root / ".gitignore"
    existing = gitignore_path.read_text(encoding="utf-8").splitlines() if gitignore_path.exists() else []
    added: list[str] = []
    entries = [
        "# Atelier runtime data",
        ".atelier/",
    ]
    for entry in entries:
        if entry not in existing:
            existing.append(entry)
            added.append(entry)
    if added:
        gitignore_path.write_text("\n".join(existing) + "\n", encoding="utf-8")
    return added


_ATELIER_CLAUDE_MD_TEMPLATE = """# CLAUDE.md — Atelier

This file guides Claude Code when working in this repository. This project uses **Atelier** for code intelligence, context reuse, and agent reasoning.

## Environment

**All Python commands must use `uv run`** — this project uses `uv` for dependency management.

```bash
uv run python -c "..."          # one-off Python
uv run pytest ...               # tests
uv run mypy src                 # type-check
uv run atelier ...              # Atelier CLI
```

## Common Commands

```bash
# Test
uv run pytest -q                          # all tests
uv run pytest -q -x -m "not slow"        # fast, stop on first failure
uv run pytest tests/path/test_file.py -q # single file
uv run pytest -q -k "test_name"          # single test by name

# Lint / format / typecheck
make lint           # ruff
make format         # ruff --fix + black + prettier (frontend)
make typecheck      # mypy --strict src

# Full pre-commit gate
make pre-commit     # format + lint + typecheck + docs + test

# Docs governance
make sync-agent-context   # regenerate host instruction files from docs/agent-os/
make check-agent-context  # verify generated files are up to date
```

## Atelier Commands

| Command | Description |
|---|---|
| ``uv run atelier init`` | Initialize project for Atelier (store, seed, index) |
| ``uv run atelier init --project`` | Also set up .gitignore, CLAUDE.md, AGENTS.md, .mcp.json |
| ``uv run atelier code index`` | Build or refresh code index |
| ``uv run atelier search <query>`` | Semantic code search |
| ``uv run atelier status`` | Show runtime status (runs dashboard) |
| ``uv run atelier status --index`` | Show code index stats |
| ``uv run atelier doctor`` | Run installation diagnostics |
| ``uv run atelier reset`` | Reset code index (--all for full store reset) |

## Architecture

```
gateway/  →  core/  →  infra/
```

- **`gateway/`** — agent-facing entry points: CLI, MCP server, SDK façade. Keep thin.
- **`core/`** — domain logic: capabilities, models, orchestrator, API.
- **`infra/`** — persistence and integrations: storage, code intelligence, embeddings.

## Coding Guidelines

Behavioral guidelines to reduce common LLM coding mistakes. Bias toward caution over speed; use judgment for trivial tasks.

**1. Think Before Coding** — state assumptions explicitly; if uncertain, ask; if multiple interpretations exist, present them; push back when a simpler approach exists.

**2. Simplicity First** — minimum code that solves the problem; no speculative features, abstractions for single-use code, or error handling for impossible scenarios; if 200 lines could be 50, rewrite it.

**3. Surgical Changes** — touch only what you must; don't improve adjacent code, refactor things that aren't broken, or delete unrelated dead code; match existing style; remove only the imports/variables/functions that *your* changes made unused.

**4. Goal-Driven Execution** — transform tasks into verifiable goals before implementing; for multi-step work, state a brief plan with per-step verify checks; loop until verified.

See [docs/agent-os/coding-guidelines.md](docs/agent-os/coding-guidelines.md) for the full reference.

## Code Intelligence

Use the dedicated, focused code-intel tools (SCIP-indexed, prefer over `grep`):

| Need | Tool |
|---|---|
| Find a symbol definition by name | `mcp__atelier__symbols` |
| Read the full source of one symbol | `mcp__atelier__node` |
| Who calls a function / what it calls | `mcp__atelier__callers` / `mcp__atelier__callees` |
| All references to a symbol | `mcp__atelier__usages` |
| Blast radius before refactoring | `mcp__atelier__impact` |
| Match/rewrite code by AST shape | `mcp__atelier__pattern` |
| Grouped source + relationships in one call | `mcp__atelier__explore` |

## Agent Spawning Rules

When spawning sub-agents via the `Agent` tool, always pick the narrowest type:

| Role | `subagent_type` | When |
|---|---|---|
| Code-review **finder** (read, search, grep — never edits) | `atelier:explore` | All Phase 1 / Angle A-G finder agents in `/code-review` |
| Code-review **verifier** (applies rubric, never edits) | `atelier:review` | All Phase 2 verifier agents in `/code-review` |
| Read-only research / exploration | `atelier:explore` | Any agent that only reads files, symbols, or web pages |
| Coding, edits, fixes | `atelier:code` | Any agent that writes or modifies files |
| Repeated failure / rescue | `atelier:repair` | When the same approach fails twice |

**Never** use the default (`claude`) agent for a task that fits one of the typed roles above — the default has write access it doesn't need and costs more.

## Validation by Change Surface

| What changed | Minimum check |
|---|---|
| Python / CLI | ``make lint && make typecheck && make test`` |
| MCP tool handlers | ``uv run pytest tests/gateway/test_mcp_tool_handlers.py -q`` |
| Code-intel engine | ``uv run pytest tests/core/test_code_context.py -q && make lint && make typecheck`` |
| Frontend | ``cd frontend && npm run build && npm run test`` |
| Documentation | ``make docs-check && make check-agent-context`` |
"""


_ATELIER_AGENTS_MD_TEMPLATE = """# Project Instructions — Atelier

This project uses Atelier for code intelligence, context reuse, and agent reasoning.

## Atelier commands

| Command | Description |
|---|---|
| ``atelier init`` | Initialize project (gitignore, MCP config, host files) |
| ``atelier code index`` | Build or refresh code index |
| ``atelier search <query>`` | Semantic search across the codebase |
| ``atelier status`` | Show runtime status |
| ``atelier doctor`` | Run diagnostics |
"""


_ATELIER_MCP_JSON_TEMPLATE = """{
  "mcpServers": {
    "atelier": {
      "command": "atelier-mcp",
      "args": ["--host", "mcp"]
    }
  }
  }
  """


def _project_init_setup(git_root: Path) -> dict[str, list[str]]:
    """Run project-scoped init steps inside a git repo.

    Returns a dict of ``{section: [messages]}`` describing what was done.
    """
    results: dict[str, list[str]] = {}

    # .gitignore
    added = _ensure_gitignore(git_root)
    if added:
        results["gitignore"] = [f"added to .gitignore: {', '.join(a for a in added if not a.startswith('#'))}"]
    else:
        results["gitignore"] = [".atelier/ already in .gitignore"]

    # CLAUDE.md
    claude_path = git_root / "CLAUDE.md"
    if not claude_path.exists():
        claude_path.write_text(_ATELIER_CLAUDE_MD_TEMPLATE.lstrip(), encoding="utf-8")
        results["claude_md"] = ["created CLAUDE.md"]
    else:
        content = claude_path.read_text(encoding="utf-8")
        # Detect whether the file already has any CLAUDE.md-style header or Atelier content
        has_header = any(
            marker in content for marker in ("# CLAUDE.md", "# CLAUDE.md — Atelier", "# Atelier", "mcp__atelier__")
        )
        if not has_header:
            claude_path.write_text(
                content.rstrip() + "\n\n" + _ATELIER_CLAUDE_MD_TEMPLATE,
                encoding="utf-8",
            )
            results["claude_md"] = ["appended Atelier guidance to CLAUDE.md"]
        else:
            results["claude_md"] = ["CLAUDE.md already has Atelier guidance"]

    # AGENTS.md
    agents_path = git_root / "AGENTS.md"
    if not agents_path.exists():
        agents_path.write_text(_ATELIER_AGENTS_MD_TEMPLATE.lstrip(), encoding="utf-8")
        results["agents_md"] = ["created AGENTS.md"]
    else:
        content = agents_path.read_text(encoding="utf-8")
        if "Atelier" not in content:
            agents_path.write_text(
                content.rstrip() + "\n\n" + _ATELIER_AGENTS_MD_TEMPLATE,
                encoding="utf-8",
            )
            results["agents_md"] = ["appended Atelier guidance to AGENTS.md"]
        else:
            results["agents_md"] = ["AGENTS.md already has Atelier guidance"]

    # .mcp.json
    mcp_path = git_root / ".mcp.json"
    if not mcp_path.exists():
        mcp_path.write_text(_ATELIER_MCP_JSON_TEMPLATE.lstrip(), encoding="utf-8")
        results["mcp_json"] = ["created .mcp.json"]
    else:
        try:
            existing_mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
            servers = existing_mcp.get("mcpServers", {})
            if "atelier" in servers:
                results["mcp_json"] = [".mcp.json already has atelier server"]
            else:
                atelier_config = json.loads(_ATELIER_MCP_JSON_TEMPLATE)
                servers["atelier"] = atelier_config["mcpServers"]["atelier"]
                mcp_path.write_text(
                    json.dumps(existing_mcp, indent=2) + "\n",
                    encoding="utf-8",
                )
                results["mcp_json"] = ["added atelier server to existing .mcp.json"]
        except (json.JSONDecodeError, KeyError):
            results["mcp_json"] = [".mcp.json exists but could not be updated (parse error)"]

    return results


def _code_index_db_path(repo_root: Path) -> Path:
    """Return the default code index database path for a repo."""
    from atelier.core.foundation.paths import default_store_root

    workspace_hash = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:12]
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
    blocks_dir = resources.files("atelier") / "infra" / "seed_blocks"
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
        f"Cannot parse --since value {value!r}. " "Use a duration like '7d', '24h', or a date like '2026-05-01'."
    )


@click.command()
@click.option("--seed/--no-seed", default=True, help="Import bundled seed blocks and rubrics.")
@click.option("--stack", default=None, help="Copy starter ReasonBlock templates for a stack.")
@click.option("--list-stacks", "show_stacks", is_flag=True, help="List available starter stacks.")
@click.option(
    "--index/--no-index",
    default=True,
    help="Bootstrap the code index for the current git repo (default: on).",
)
@click.option(
    "--project",
    is_flag=True,
    help="Also run project-scoped setup: .gitignore, CLAUDE.md, AGENTS.md, .mcp.json.",
)
@click.pass_context
def init(ctx: click.Context, seed: bool, stack: str | None, show_stacks: bool, index: bool, project: bool) -> None:
    """Initialize the runtime store at --root."""
    if show_stacks:
        from atelier.core.capabilities.starter_packs import list_stacks

        stacks = list_stacks()
        if not stacks:
            click.echo("No starter stacks available.")
            return
        click.echo("Available starter stacks:")
        for item in stacks:
            click.echo(f"  {item.slug:20} {item.name} ({item.version}) - {item.description}")
        return

    root: Path = ctx.obj["root"]
    from atelier.infra.storage.factory import create_store

    try:
        store = create_store(root)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    store.init()
    click.echo(f"initialized atelier store at {store.root}")
    if seed:
        block_files, rubric_files = _seed_resources()
        seeded_blocks: dict[str, ReasonBlock] = {}
        for path in block_files:
            data = _load_yaml(path)
            if "id" not in data:
                data["id"] = ReasonBlock.make_id(data["title"], data["domain"])
            block = ReasonBlock.model_validate(data)
            seeded_blocks[block.id] = block
        for block in _load_domain_manager(root).all_reasonblocks():
            seeded_blocks[block.id] = block
        n_b = 0
        for block in seeded_blocks.values():
            store.upsert_block(block)
            n_b += 1
        n_r = 0
        for path in rubric_files:
            data = _load_yaml(path)
            rubric = Rubric.model_validate(data)
            store.upsert_rubric(rubric)
            n_r += 1
        click.echo(f"seeded {n_b} reasonblocks and {n_r} rubrics")
    if stack:
        from atelier.core.capabilities.starter_packs import copy_stack_templates

        try:
            copied, skipped = copy_stack_templates(stack, store.blocks_dir)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        suffix = f", skipped {skipped} existing" if skipped else ""
        click.echo(f"copied {copied} starter reasonblocks for stack {stack}{suffix}")
    if index:
        git_root = _detect_git_root(Path.cwd())
        if git_root is not None:
            click.echo(f"bootstrapping code index for {git_root} ...")
            from atelier.gateway.cli.commands.code import _code_context_engine

            engine = _code_context_engine(str(git_root))
            stats = engine.index_repo().model_dump(mode="json")
            click.echo(
                f"indexed {stats['files_indexed']} files, "
                f"{stats['symbols_indexed']} symbols "
                f"({stats['imports_indexed']} imports)"
            )
        else:
            click.echo("code index skipped (no git repository detected in current directory)")
    if project:
        git_root = _detect_git_root(Path.cwd())
        if git_root is not None:
            click.echo("running project-scoped setup ...")
            results = _project_init_setup(git_root)
            for section, messages in results.items():
                for msg in messages:
                    click.echo(f"  [{section}] {msg}")
        else:
            click.echo("project init skipped (no git repository detected)")


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

    if as_json:
        _emit(checks, as_json=True)
        return

    click.echo("Atelier diagnostics")
    click.echo("==================")
    for name, info in checks.items():
        status = "✓" if info.get("ok") else "✗"
        click.echo(f"  {status} {name}")
        if info.get("version"):
            click.echo(f"       version: {info['version']}")
        if info.get("path"):
            click.echo(f"       path: {info['path']}")
        if info.get("root"):
            click.echo(f"       root: {info['root']}")
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


@click.command("login")
@click.option("--token", default=None, help="Credentials JSON, base64 payload, or refresh token.")
@click.option("--anonymous", "anonymous", is_flag=True, help="Start a local anonymous trial.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def login_cmd(ctx: click.Context, token: str | None, anonymous: bool, as_json: bool) -> None:
    """Create local Atelier auth state for plugin operations."""
    from atelier.core.capabilities.plugin_runtime import (
        begin_browser_login,
        claim_anonymous_trial,
        parse_login_token,
        write_auth_state,
    )

    if anonymous:
        payload = {"auth": claim_anonymous_trial(ctx.obj["root"]), "mode": "anonymous"}
    elif token:
        payload = {
            "auth": write_auth_state(ctx.obj["root"], parse_login_token(token)),
            "mode": "token",
        }
    else:
        pending = begin_browser_login(ctx.obj["root"])
        payload = {"mode": "browser", "pending": pending}
    if as_json:
        _emit(payload, as_json=True)
        return
    if str(payload.get("mode")) == "browser":
        pending_payload = payload.get("pending")
        pending = pending_payload if isinstance(pending_payload, dict) else {}
        click.echo("Open this URL to finish login:")
        click.echo(pending.get("url", ""))
    else:
        auth_payload = payload.get("auth")
        auth = auth_payload if isinstance(auth_payload, dict) else {}
        label = "anonymous trial" if auth.get("isAnonymous") else auth.get("email") or auth.get("userId")
        click.echo(f"logged in: {label}")


@click.command("logout")
@click.option("--no-trial", is_flag=True, help="Do not create a local anonymous trial after logout.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def logout_cmd(ctx: click.Context, no_trial: bool, as_json: bool) -> None:
    """Remove local auth and optionally activate an anonymous trial."""
    from atelier.core.capabilities.plugin_runtime import logout_local

    payload = logout_local(ctx.obj["root"], claim_trial=not no_trial)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("logged out" + ("; anonymous trial active" if payload.get("anonymous") else ""))


@click.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON of runs data.")
@click.option("--line", "line_mode", is_flag=True, help="One-liner mode (good for status bars).")
@click.option("-n", type=int, default=5, show_default=True, help="Number of recent runs to show.")
@click.option("--session-id", default=None, help="Show detail for a specific run only.")
@click.option("--auth", "auth_mode", is_flag=True, help="Show auth/subscription status instead of runs.")
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
    auth_mode: bool,
    index_mode: bool,
) -> None:
    """Show runs dashboard, auth status, or code index stats.

    Default view: runs dashboard (overview of recent runs, totals, savings).

    Use --auth to show auth/subscription status, --index for index stats.
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

    if auth_mode:
        from atelier.core.capabilities.plugin_runtime import auth_status, load_plugin_settings

        payload = auth_status(root)
        payload["settings"] = load_plugin_settings(root)
        if as_json:
            _emit(payload, as_json=True)
            return
        click.echo(f"authenticated: {payload['authenticated']}")
        click.echo(f"anonymous: {payload['isAnonymous']}")
        if payload.get("email"):
            click.echo(f"email: {payload['email']}")
        if payload.get("subscription"):
            click.echo(f"subscription: {payload['subscription']}")
        click.echo(f"root: {payload['root']}")
        return

    if as_json:
        runs_dir = root / "runs"
        target: Path | None
        if session_id:
            target = runs_dir / f"{session_id}.json"
        else:
            files = sorted(runs_dir.glob("*.json"), key=os.path.getmtime, reverse=True)
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
    """Manage local plugin settings."""


@plugin_settings_group.command("show")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def plugin_settings_show(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.capabilities.plugin_runtime import load_plugin_settings

    payload = load_plugin_settings(ctx.obj["root"])
    if as_json:
        _emit(payload, as_json=True)
        return
    for key, value in payload.items():
        click.echo(f"{key}: {str(value).lower()}")


@plugin_settings_group.command("set")
@click.argument("key")
@click.argument("value", type=click.Choice(["true", "false", "on", "off", "1", "0"]))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def plugin_settings_set(ctx: click.Context, key: str, value: str, as_json: bool) -> None:
    from atelier.core.capabilities.plugin_runtime import write_plugin_setting

    enabled = value in {"true", "on", "1"}
    try:
        payload = write_plugin_setting(ctx.obj["root"], key, enabled)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"set {key}={str(enabled).lower()}")


@_dev_command("detect-loop")
@click.option("--session-id", default=None, help="Specific session ID. Defaults to latest.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def detect_loop_cmd(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    """Detect loops, repeated failures, and dead-end trajectories in a run ledger."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.loop_report(session_id=session_id)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"loop_detected: {payload['loop_detected']}")
    click.echo(f"severity: {payload['severity']}")
    click.echo(f"loop_types: {', '.join(payload['loop_types']) or 'none'}")
    click.echo(f"prior_attempts: {payload['prior_attempts']}")
    if payload["rescue_strategies"]:
        click.echo("rescue_strategies:")
        for s in payload["rescue_strategies"]:
            click.echo(f"  - {s}")


@click.command("loop-report")
@click.option("--session-id", default=None, help="Specific session ID. Defaults to latest.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def loop_report_cmd(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    """Full loop analysis: signature, severity, alerts, rescue strategies."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.loop_report(session_id=session_id)
    _emit(payload, as_json=True) if as_json else click.echo(json.dumps(payload, indent=2))


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

    if vendor and not as_json:
        vendor_key = vendor.capitalize()
        filtered_cost = window.cost_by_vendor.get(vendor_key, 0.0)
        click.echo(f"Vendor filter: {vendor_key}  ${filtered_cost:.2f}" f" of ${window.total_cost_usd:.2f} total")

    display_window = window
    if group_by == "vendor" and not as_json:
        pass
    elif group_by == "model" and not as_json:
        display_window = InsightsWindow(
            since=window.since,
            until=window.until,
            session_count=window.session_count,
            total_duration_seconds=window.total_duration_seconds,
            total_cost_usd=window.total_cost_usd,
            total_atelier_savings_usd=window.total_atelier_savings_usd,
            cost_by_vendor=window.cost_by_vendor,
            cost_by_tool=window.cost_by_model,
            cost_by_model=window.cost_by_model,
            top_sessions=window.top_sessions,
            outcomes_summary=window.outcomes_summary,
            opportunities=window.opportunities,
        )
    elif group_by == "session" and not as_json:
        session_costs = {s.session_id[:8]: s.cost_usd for s in window.top_sessions}
        display_window = InsightsWindow(
            since=window.since,
            until=window.until,
            session_count=window.session_count,
            total_duration_seconds=window.total_duration_seconds,
            total_cost_usd=window.total_cost_usd,
            total_atelier_savings_usd=window.total_atelier_savings_usd,
            cost_by_vendor=window.cost_by_vendor,
            cost_by_tool=session_costs,
            cost_by_model=window.cost_by_model,
            top_sessions=window.top_sessions,
            outcomes_summary=window.outcomes_summary,
            opportunities=window.opportunities,
        )

    if as_json:
        click.echo(render_json(window))
    else:
        click.echo(render_text(display_window, no_color=no_color))


__all__ = [
    "_project_root",
    "audit_group",
    "deprecate",
    "detect_loop_cmd",
    "doctor_cmd",
    "env_group",
    "governance_group",
    "init",
    "insights_cmd",
    "login_cmd",
    "logout_cmd",
    "loop_report_cmd",
    "plugin_settings_group",
    "quarantine",
    "reset_cmd",
    "share_cmd",
    "status_cmd",
    "team_group",
    "tool_report_cmd",
    "uninstall",
]
