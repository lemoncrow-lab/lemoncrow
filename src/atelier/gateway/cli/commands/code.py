from __future__ import annotations

import shutil
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any

import click

from atelier.gateway.cli.commands._shared import _emit
from atelier.gateway.integrations.openmemory_lifecycle import project_root as _project_root


@click.group("zoekt")
def zoekt_group() -> None:
    """Manage Zoekt local binaries and optional Docker sidecar."""


def _zoekt_workspace_prefix(repo_root: Path) -> str:
    return f"atelier-zoekt-{sha256(str(repo_root.resolve()).encode('utf-8')).hexdigest()[:12]}-"


def _zoekt_default_index_dir() -> Path:
    return Path.home() / ".zoekt"


def _zoekt_missing_local_binaries() -> list[str]:
    required = ("zoekt-git-index", "zoekt-index", "zoekt", "zoekt-webserver")
    return [name for name in required if shutil.which(name) is None]


def _zoekt_install_commands() -> tuple[str, ...]:
    return (
        "go install github.com/sourcegraph/zoekt/cmd/zoekt-git-index@latest",
        "go install github.com/sourcegraph/zoekt/cmd/zoekt-index@latest",
        "go install github.com/sourcegraph/zoekt/cmd/zoekt@latest",
        "go install github.com/sourcegraph/zoekt/cmd/zoekt-webserver@latest",
    )


@zoekt_group.command("install")
@click.option("--auto", is_flag=True, help="Run go install commands automatically.")
@click.option("--print-only", is_flag=True, help="Only print the install commands.")
def zoekt_install(auto: bool, print_only: bool) -> None:
    """Install/check local Zoekt binaries (native, no Docker)."""
    missing = _zoekt_missing_local_binaries()
    commands = _zoekt_install_commands()

    if not missing:
        click.echo("Zoekt local binaries are already installed.")
        return

    click.echo("Missing Zoekt binaries: " + ", ".join(missing))
    click.echo("Install with:")
    for command in commands:
        click.echo(f"  {command}")

    if print_only:
        return
    if not auto:
        raise click.ClickException("Install the commands above, or run: atelier zoekt install --auto")
    if shutil.which("go") is None:
        raise click.ClickException("Go is required for --auto install (go command not found on PATH)")

    for command in commands:
        subprocess.run(command.split(), check=True)

    missing_after = _zoekt_missing_local_binaries()
    if missing_after:
        raise click.ClickException("Zoekt install incomplete; still missing: " + ", ".join(missing_after))
    click.echo("Zoekt local binaries installed.")


@zoekt_group.command("index")
@click.argument(
    "target",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=".",
    required=False,
)
@click.option(
    "--index",
    "index_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=str(_zoekt_default_index_dir()),
    show_default=True,
)
def zoekt_index(target: Path, index_dir: Path) -> None:
    """Index a repository/directory into a local Zoekt index."""
    target = target.resolve()
    index_dir = index_dir.resolve()
    index_dir.mkdir(parents=True, exist_ok=True)

    git_index = shutil.which("zoekt-git-index")
    plain_index = shutil.which("zoekt-index")
    if git_index and (target / ".git").exists():
        cmd = [git_index, "-index", str(index_dir), str(target)]
    elif plain_index:
        cmd = [plain_index, "-index", str(index_dir), str(target)]
    elif git_index:
        cmd = [git_index, "-index", str(index_dir), str(target)]
    else:
        raise click.ClickException("Zoekt index binaries not found. Run: atelier zoekt install")

    subprocess.run(cmd, check=True)
    click.echo(f"Zoekt index updated at {index_dir}")


@zoekt_group.command("search")
@click.argument("query", nargs=-1, required=True)
@click.option(
    "--index",
    "index_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=str(_zoekt_default_index_dir()),
    show_default=True,
)
def zoekt_search(query: tuple[str, ...], index_dir: Path) -> None:
    """Search the local Zoekt index from CLI."""
    zoekt_bin = shutil.which("zoekt")
    if zoekt_bin is None:
        raise click.ClickException("zoekt binary not found. Run: atelier zoekt install")
    q = " ".join(query).strip()
    if not q:
        raise click.ClickException("query cannot be empty")
    result = subprocess.run([zoekt_bin, "-index", str(index_dir.resolve()), q], check=False)
    if result.returncode not in (0, 1):
        raise click.ClickException(f"zoekt search failed (exit {result.returncode})")


@zoekt_group.command("serve")
@click.option(
    "--index",
    "index_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=str(_zoekt_default_index_dir()),
    show_default=True,
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=6070, show_default=True, type=int)
def zoekt_serve(index_dir: Path, host: str, port: int) -> None:
    """Run local Zoekt web/API server against the local index."""
    webserver_bin = shutil.which("zoekt-webserver")
    if webserver_bin is None:
        raise click.ClickException("zoekt-webserver binary not found. Run: atelier zoekt install")
    subprocess.run(
        [webserver_bin, "-index", str(index_dir.resolve()), "-listen", f"{host}:{port}"],
        check=True,
    )


@zoekt_group.command("up")
@click.pass_context
def zoekt_up(ctx: click.Context) -> None:
    """Start the persistent Zoekt search container for the current repo."""
    from atelier.infra.code_intel.zoekt.binary import discover_zoekt_binary
    from atelier.infra.code_intel.zoekt.server import get_zoekt_server

    repo_root = Path(_project_root())
    resolution = discover_zoekt_binary(repo_root)
    if not resolution.available:
        raise click.ClickException(f"Zoekt runtime unavailable: {resolution.reason}")
    server = get_zoekt_server(repo_root, resolution=resolution)
    handle = server.ensure_started()
    click.echo(f"Zoekt started: {handle}")


@zoekt_group.command("down")
@click.pass_context
def zoekt_down(ctx: click.Context) -> None:
    """Stop the persistent Zoekt container for the current repo."""
    from atelier.infra.code_intel.zoekt.server import get_zoekt_server

    repo_root = Path(_project_root())
    server = get_zoekt_server(repo_root)
    server.stop()
    click.echo("Zoekt stopped.")


@zoekt_group.command("status")
@click.option(
    "--index",
    "index_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=str(_zoekt_default_index_dir()),
    show_default=True,
)
@click.pass_context
def zoekt_status(ctx: click.Context, index_dir: Path) -> None:
    """Show local Zoekt status (and Docker sidecar status if present)."""
    missing = _zoekt_missing_local_binaries()
    if missing:
        click.echo("Local Zoekt binaries: missing -> " + ", ".join(missing))
        click.echo("Install with: atelier zoekt install")
    else:
        click.echo("Local Zoekt binaries: installed")
    resolved_index = index_dir.resolve()
    click.echo(f"Local index dir: {resolved_index} ({'exists' if resolved_index.exists() else 'missing'})")

    repo_root = Path(_project_root())
    prefix = _zoekt_workspace_prefix(repo_root)
    if shutil.which("docker") is None:
        return
    click.echo("")
    click.echo("Docker sidecar containers (optional):")
    subprocess.run(["docker", "ps", "-a", "--filter", f"name={prefix}"], check=False)


@zoekt_group.command("reindex")
@click.option(
    "--index",
    "index_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=str(_zoekt_default_index_dir()),
    show_default=True,
)
@click.pass_context
def zoekt_reindex(ctx: click.Context, index_dir: Path) -> None:
    """Reindex current repository into local Zoekt index."""
    target = Path(_project_root())
    ctx.invoke(zoekt_index, target=target, index_dir=index_dir)


@zoekt_group.command("reset")
@click.option("--yes", is_flag=True, help="Confirm removal of Zoekt runtime data.")
@click.pass_context
def zoekt_reset(ctx: click.Context, yes: bool) -> None:
    """Stop Zoekt and remove runtime state for this repository."""
    if not yes:
        raise click.ClickException("Pass --yes to confirm index cleanup.")
    repo_root = Path(_project_root())
    prefix = _zoekt_workspace_prefix(repo_root)
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name={prefix}"],
        capture_output=True,
        text=True,
        check=False,
    )
    container_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if container_ids:
        subprocess.run(["docker", "rm", "-f", *container_ids], check=False)
    from atelier.core.foundation.paths import default_store_root

    workspace_hash = sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:12]
    runtime_root = default_store_root() / "workspaces" / workspace_hash / "zoekt"
    shutil.rmtree(runtime_root, ignore_errors=True)
    click.echo("Zoekt state removed.")


def _code_context_engine(repo_root: str) -> Any:
    from atelier.core.capabilities.code_context import CodeContextEngine

    return CodeContextEngine(repo_root)


@click.group("code")
def code_group() -> None:
    """Code context indexing, retrieval, repo maps, and impact analysis."""


@code_group.command("index")
@click.option("--repo-root", default=".", show_default=True)
@click.option("--include", "include_globs", multiple=True)
@click.option("--exclude", "exclude_globs", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def code_index_cmd(
    repo_root: str,
    include_globs: tuple[str, ...],
    exclude_globs: tuple[str, ...],
    as_json: bool,
) -> None:
    """Index a repository into the SQLite FTS5 symbol store."""
    engine = _code_context_engine(repo_root)

    try:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
        )

        console = Console(stderr=True)
        progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(bar_width=24),
            TextColumn("{task.percentage:3.0f}%"),
            TimeRemainingColumn(),
            console=console,
            transient=False,
        )
        with progress:
            task_id = progress.add_task("Indexing code", total=None)

            def _on_progress(current: int, total: int) -> None:
                if total:
                    progress.update(
                        task_id,
                        completed=current,
                        total=total,
                        description=f"Indexing  ({current}/{total})",
                    )
                else:
                    progress.update(task_id, completed=current)

            payload = engine.index_repo(
                include_globs=list(include_globs) or None,
                exclude_globs=list(exclude_globs) or None,
                progress_callback=_on_progress,
            ).model_dump(mode="json")
            progress.update(
                task_id,
                description=f"✓ Indexed  {payload['files_indexed']} files",
            )
    except ImportError:
        payload = engine.index_repo(
            include_globs=list(include_globs) or None,
            exclude_globs=list(exclude_globs) or None,
        ).model_dump(mode="json")

    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(
        f"indexed {payload['files_indexed']} files, {payload['symbols_indexed']} symbols "
        f"({payload['imports_indexed']} imports)"
    )


__all__ = ["_code_context_engine", "code_group", "zoekt_group"]
