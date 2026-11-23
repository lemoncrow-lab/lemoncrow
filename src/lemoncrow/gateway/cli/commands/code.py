from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from lemoncrow_client.lite.index import LiteIndexReport

from lemoncrow.gateway.cli import style
from lemoncrow.gateway.cli.commands._shared import _emit, require_pro
from lemoncrow.gateway.integrations.openmemory_lifecycle import project_root as _project_root


@click.group("zoekt")
def zoekt_group() -> None:
    """Manage Zoekt local binaries and optional Docker sidecar."""


def _zoekt_workspace_prefix(repo_root: Path) -> str:
    from lemoncrow.core.foundation.paths import workspace_key

    return f"lemoncrow-zoekt-{workspace_key(repo_root.resolve())[:40]}-"


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
        raise click.ClickException("Install the commands above, or run: lc zoekt install --auto")
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
    require_pro("code_search", "Zoekt-backed code search")
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
        raise click.ClickException("Zoekt index binaries not found. Run: lc zoekt install")

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
    require_pro("code_search", "Zoekt-backed code search")
    zoekt_bin = shutil.which("zoekt")
    if zoekt_bin is None:
        raise click.ClickException("zoekt binary not found. Run: lc zoekt install")
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
    require_pro("code_search", "Zoekt-backed code search")
    webserver_bin = shutil.which("zoekt-webserver")
    if webserver_bin is None:
        raise click.ClickException("zoekt-webserver binary not found. Run: lc zoekt install")
    subprocess.run(
        [webserver_bin, "-index", str(index_dir.resolve()), "-listen", f"{host}:{port}"],
        check=True,
    )


@zoekt_group.command("up")
@click.pass_context
def zoekt_up(ctx: click.Context) -> None:
    """Start the persistent Zoekt search container for the current repo."""
    from lemoncrow.infra.code_intel.zoekt.binary import discover_zoekt_binary
    from lemoncrow.infra.code_intel.zoekt.server import get_zoekt_server

    repo_root = Path(_project_root())
    resolution = discover_zoekt_binary(repo_root)
    if not resolution.available:
        raise click.ClickException(f"Zoekt runtime unavailable: {resolution.reason}")
    server = get_zoekt_server(repo_root, resolution=resolution)
    handle = server.ensure_started_and_build()
    click.echo(f"Zoekt started: {handle}")


@zoekt_group.command("down")
@click.pass_context
def zoekt_down(ctx: click.Context) -> None:
    """Stop the persistent Zoekt container for the current repo."""
    from lemoncrow.infra.code_intel.zoekt.server import get_zoekt_server

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
        click.echo("Install with: lc zoekt install")
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
    from lemoncrow.core.foundation.paths import resolve_workspace_store_dir

    runtime_root = resolve_workspace_store_dir(workspace_root=repo_root) / "zoekt"
    shutil.rmtree(runtime_root, ignore_errors=True)
    click.echo("Zoekt state removed.")


def _code_context_engine(repo_root: str, db_path: Path | None = None) -> Any:
    from lemoncrow.core.capabilities.code_intelligence_provider import (
        ProEngineUnavailable,
        create_code_intelligence_provider,
    )

    # One-shot CLI commands don't need background autosync threads.
    try:
        return create_code_intelligence_provider(repo_root, db_path=db_path, autosync_enabled=False)
    except ProEngineUnavailable as exc:
        raise click.ClickException(str(exc)) from exc


def _index_repo_with_progress(
    engine: Any,
    *,
    force: bool = False,
    steal: bool = False,
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    description: str = "Indexing code",
    success_description: str | None = None,
    frame_prefix: str = "",
) -> dict[str, Any]:
    try:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
        )

        prefix_markup = f"[dim]{frame_prefix}[/dim]" if frame_prefix else ""
        indent = "  "
        console = Console(stderr=True)
        progress = Progress(
            TextColumn(f"{indent}{prefix_markup}{{task.description}}"),
            BarColumn(
                bar_width=32,
                style="bright_black",
                complete_style="cyan",
                finished_style="green",
                pulse_style="magenta",
            ),
            TextColumn("[bold cyan]{task.percentage:3.0f}%[/bold cyan]"),
            TimeRemainingColumn(),
            console=console,
            transient=False,
        )
        with progress:
            task_id = progress.add_task("[yellow]⏳[/yellow]  Acquiring index lock...", total=None)
            _phase: list[str] = ["lock"]  # list hack for nonlocal assignment
            _last_total: list[int] = [0]

            def _on_progress(current: int, total: int) -> None:
                # Transition: lock -> discovery -> indexing
                # When discovery sends (0, total), we transition to discovery phase
                if current == 0 and total > 0 and _phase[0] == "lock":
                    _phase[0] = "discovery"
                # Transition from discovery -> indexing when total drops
                # (raw git entries -> filtered file count).
                if total and total < _last_total[0] and _phase[0] == "discovery":
                    _phase[0] = "indexing"

                if _phase[0] == "lock":
                    progress.update(
                        task_id,
                        description="[yellow]⏳[/yellow]  Acquiring index lock...",
                    )
                elif _phase[0] == "discovery":
                    if total:
                        progress.update(
                            task_id,
                            description=f"[green]\u27f3[/green]  Discovering files...  ({current}/{total})",
                        )
                    else:
                        progress.update(
                            task_id,
                            description=f"[green]\u27f3[/green]  Discovering files...  ({current})",
                        )
                else:
                    progress.update(
                        task_id,
                        completed=current,
                        total=total,
                        description=f"[green]\u27f3[/green]  {description}  ({current}/{total})",
                    )
                _last_total[0] = total

            payload = engine.index_repo(
                force=force,
                require_lock=True,
                steal=steal,
                include_globs=include_globs,
                exclude_globs=exclude_globs,
                progress_callback=_on_progress,
            ).model_dump(mode="json")
            progress.update(
                task_id,
                total=100,
                completed=100,
                description=f"[green]✓[/green] [rgb(155,117,217)]{success_description or description}[/rgb(155,117,217)]",
            )
            return payload
    except ImportError:
        return engine.index_repo(
            force=force,
            require_lock=True,
            steal=steal,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
        ).model_dump(mode="json")


def _index_git_history_with_progress(engine: Any, frame_prefix: str = "") -> dict[str, int] | None:
    from lemoncrow.pro.code_intel.git_history.adapter import history_indexing_enabled

    if not history_indexing_enabled():
        return None
    try:
        from rich.console import Console
        from rich.progress import BarColumn, Progress, TextColumn

        adapter = engine._deleted_history_adapter()
        current_head = adapter._current_head()
        if current_head is None:
            return None

        from contextlib import closing

        with closing(adapter._connection_factory()) as conn:
            row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (adapter._head_state_key,)).fetchone()
            previous_head = str(row["value"]) if row is not None else None
            try:
                count_row = conn.execute("SELECT COUNT(*) AS n FROM symbol_graveyard").fetchone()
                graveyard_count = int(count_row["n"]) if count_row is not None else 0
            except sqlite3.OperationalError:
                graveyard_count = 0  # graveyard table not created yet -> nothing indexed

        if previous_head == current_head and graveyard_count > 0:
            return None

        prefix_markup = f"[dim]{frame_prefix}[/dim]" if frame_prefix else ""
        console = Console(stderr=True)
        progress = Progress(
            TextColumn(f"{prefix_markup}{{task.description}}"),
            BarColumn(
                bar_width=32,
                style="bright_black",
                complete_style="cyan",
                finished_style="green",
                pulse_style="magenta",
            ),
            console=console,
            transient=False,
        )
        with progress:
            task_id = progress.add_task("[green]⟳[/green]  Indexing Git history...", total=None)

            def on_commit(current: int, total: int) -> None:
                progress.update(
                    task_id,
                    total=total,
                    completed=current,
                    description=f"[cyan]⟳[/cyan]  Indexing Git history... {current}/{total}",
                )

            summary = adapter._ensure_history_ready(on_commit=on_commit)
            progress.update(
                task_id,
                total=100,
                completed=100,
                description="[green]✓[/green]  Indexed Git history",
            )
            return summary
    except Exception:
        logging.exception("Failed to index git history")
        try:
            engine._deleted_history_adapter()._ensure_history_ready()
        except Exception:
            logging.exception("Failed to ensure git history ready")
        return None


def _trigger_zoekt_with_progress(repo_root: Path, frame_prefix: str = "", *, quiet: bool = False) -> None:
    """Build the per-workspace Zoekt trigram index if binaries are available."""
    try:
        from lemoncrow.infra.code_intel.zoekt.binary import discover_zoekt_binary
        from lemoncrow.infra.code_intel.zoekt.server import get_zoekt_server

        resolution = discover_zoekt_binary(repo_root)
        if not resolution.available:
            return  # Zoekt not installed — silent skip, FTS5 is the fallback

        if quiet:
            # Skip Rich progress (avoids stderr pollution in --json mode).
            server = get_zoekt_server(repo_root, resolution=resolution)
            server.ensure_started_and_build()
            return

        from rich.console import Console
        from rich.progress import Progress, TextColumn

        prefix_markup = f"[dim]{frame_prefix}[/dim]" if frame_prefix else ""
        console = Console(stderr=True)
        progress = Progress(TextColumn(f"{prefix_markup}{{task.description}}"), console=console, transient=False)
        with progress:
            task_id = progress.add_task("[green]⟳[/green]  Building Zoekt trigram index...", total=None)
            try:
                server = get_zoekt_server(repo_root, resolution=resolution)
                server.ensure_started_and_build()
                progress.update(task_id, description="[green]✓[/green]  Zoekt trigram index ready")
            except Exception as exc:
                progress.update(task_id, description=f"[yellow]⚠[/yellow]  Zoekt: {exc}")
    except Exception:
        logging.exception("Zoekt prewarm failed")


@click.group("code", invoke_without_command=True)
@click.option(
    "--internal-private",
    "internal_private",
    is_flag=True,
    hidden=True,
    help="Internal LemonCrow private-runtime development path.",
)
@click.option(
    "--engine",
    type=str,
    default="auto",
    show_default=True,
    help="Internal private-development frontend selector.",
    hidden=True,
)
@click.option("--provider", default="", hidden=True)
@click.option("--model", default="", hidden=True)
@click.option("--budget", type=click.Choice(["cheap", "balanced", "best"]), default="balanced", hidden=True)
@click.option("--cache-policy", type=click.Choice(["auto", "5m", "1h", "off"]), default="auto", hidden=True)
@click.option("--max-cost", type=float, default=None, hidden=True)
@click.option("--optimization-mode", type=click.Choice(["off", "shadow", "enforce"]), default="shadow", hidden=True)
@click.option("--local-retrieval", type=click.Choice(["off", "auto", "force"]), default="auto", hidden=True)
@click.option("--local-retrieval-model", default="", hidden=True)
@click.option("--yolo", is_flag=True, hidden=True)
@click.option(
    "-p", "--prompt", default=None, help="Reserved: run one prompt with the Hosted coding agent (not available yet)."
)
@click.option(
    "--resume",
    default=None,
    metavar="SESSION_ID",
    help="Reserved: resume a Hosted coding-agent session (not available yet).",
)
@click.option(
    "--project-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    hidden=True,
    help="Internal private-development workspace root.",
)
@click.option("--mcp/--no-mcp", "mcp_enabled", default=True, hidden=True)
@click.option("--mcp-schema-mode", type=click.Choice(["auto", "eager", "focused"]), default="auto", hidden=True)
@click.pass_context
def code_group(
    ctx: click.Context,
    internal_private: bool,
    engine: str,
    provider: str,
    model: str,
    budget: str,
    cache_policy: str,
    max_cost: float | None,
    optimization_mode: str,
    local_retrieval: str,
    local_retrieval_model: str,
    yolo: bool,
    prompt: str | None,
    resume: str | None,
    project_root: Path | None,
    mcp_enabled: bool,
    mcp_schema_mode: str,
) -> None:
    """Reserved for the Hosted coding agent; not available yet.

    `lc code` is reserved for LemonCrow's Hosted coding agent, which is not
    available yet: it says so and exits. For a local agent, use `lc with <host>`.
    """

    if internal_private and os.environ.get("LEMONCROW_INTERNAL_PRIVATE_RUNTIME", "").strip() != "1":
        raise click.ClickException(
            "local private Runtime execution is internal LemonCrow development tooling; "
            "set LEMONCROW_INTERNAL_PRIVATE_RUNTIME=1 only for engineering, benchmarks, or private-runtime development"
        )
    if ctx.invoked_subcommand is not None:
        return

    root_obj = ctx.find_root().obj if isinstance(ctx.find_root().obj, dict) else {}
    store_root = Path(root_obj.get("root", Path.home() / ".lemoncrow"))
    resolved_project = (project_root or Path.cwd()).resolve()

    if not internal_private:
        raise click.ClickException(
            "`lc code` is reserved for the Hosted LemonCrow coding agent, which is not available yet. "
            "It is not a local Workspaces Lite launcher: local agent integrations live under `lc with ...` "
            "and public host lifecycle under `lc host`."
        )

    from lemoncrow.core.capabilities.code_intelligence_provider import (
        ProEngineUnavailable,
        require_private_code_intelligence,
    )

    try:
        require_private_code_intelligence()
    except ProEngineUnavailable as exc:
        raise click.ClickException(str(exc)) from exc

    from lemoncrow.core.capabilities.private_runtime_provider import (
        PrivateRuntimeUnavailable,
        run_private_coding_engine,
    )

    try:
        return_code = run_private_coding_engine(
            store_root=store_root,
            project_root=resolved_project,
            engine=engine,  # type: ignore[arg-type]
            provider=provider,
            model=model,
            budget=budget,
            cache_policy=cache_policy,
            max_cost=max_cost,
            yolo=yolo,
            prompt=prompt,
            resume=resume,
            mcp_enabled=mcp_enabled,
            mcp_schema_mode=mcp_schema_mode,
            optimization_mode=optimization_mode,
            local_retrieval=local_retrieval,
            local_retrieval_model=local_retrieval_model,
        )
    except PrivateRuntimeUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    if return_code:
        raise SystemExit(return_code)


def _code_store_root(ctx: click.Context) -> Path:
    root_obj = ctx.find_root().obj if isinstance(ctx.find_root().obj, dict) else {}
    return Path(root_obj.get("root", Path.home() / ".lemoncrow"))


def _code_group_params(ctx: click.Context) -> dict[str, Any]:
    current: click.Context | None = ctx
    while current is not None:
        if current.command is code_group:
            return dict(current.params)
        current = current.parent
    return {}


def _code_subcommand_uses_internal_private(ctx: click.Context) -> bool:
    return bool(_code_group_params(ctx).get("internal_private"))


def _require_code_subcommand_internal_private(ctx: click.Context, name: str) -> None:
    if not _code_subcommand_uses_internal_private(ctx):
        raise click.ClickException(
            f"lc code {name} is internal private-development tooling, not a public Hosted command; "
            "advanced private-runtime maintenance is internal to LemonCrow"
        )
    if os.environ.get("LEMONCROW_INTERNAL_PRIVATE_RUNTIME", "").strip() != "1":
        raise click.ClickException(
            "local private Runtime execution is internal LemonCrow development tooling; "
            "set LEMONCROW_INTERNAL_PRIVATE_RUNTIME=1 only for engineering, benchmarks, or private-runtime development"
        )


@click.group("host")
def host_group() -> None:
    """Manage public LemonCrow-supported coding-agent hosts."""


def _host_engine_module(engine: str) -> tuple[Any, str]:
    if engine == "pi":
        from lemoncrow.gateway.cli import pi_host

        return pi_host, "Pi"
    raise click.ClickException("public host management supports Pi")


def _host_engine_option(function: Any) -> Any:
    return click.option(
        "--engine",
        type=click.Choice(["pi"]),
        default="pi",
        show_default=True,
        help="Public coding-agent host managed by LemonCrow.",
    )(function)


@host_group.command("status")
@_host_engine_option
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def code_host_status_cmd(ctx: click.Context, engine: str, as_json: bool) -> None:
    """Show whether the managed host is installed, where, and at which version."""
    module, label = _host_engine_module(engine)
    value = module.host_status(_code_store_root(ctx))
    if as_json:
        click.echo(json.dumps(value, sort_keys=True))
        return
    installed = bool(value["installed"])
    click.echo(style.title(f"{label} host", "installed" if installed else "not installed"))
    metadata = value["metadata"]
    if metadata:
        click.echo(style.row(None, "Version", str(metadata.get("tag", "unknown")), width=9))
    if resolved := value["resolved_path"]:
        click.echo(style.row(None, "Path", style.home_relative(resolved), width=9))
    click.echo(style.row(None, "Updates", str(value["update_policy"]), width=9))
    if not installed:
        click.echo("")
        click.echo(style.next_step("Install it with", f"lc host install --engine {engine}"))


def _install_pi_integration(engine: str) -> None:
    if engine != "pi":
        return
    from lemoncrow_client import pi_integration

    try:
        settings = pi_integration.install()
    except (OSError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(style.row(None, "Pi settings", style.home_relative(settings), width=12))


@host_group.command("install")
@_host_engine_option
@click.pass_context
def code_host_install_cmd(ctx: click.Context, engine: str) -> None:
    """Download and install the managed host release."""
    module, label = _host_engine_module(engine)
    path = module.install_host_release(_code_store_root(ctx))
    _install_pi_integration(engine)
    click.echo(style.success(f"{label} host installed {style.dim(style.home_relative(path))}"))


@host_group.command("update")
@_host_engine_option
@click.pass_context
def code_host_update_cmd(ctx: click.Context, engine: str) -> None:
    """Update the managed host to its latest release."""
    module, label = _host_engine_module(engine)
    path = module.install_host_release(_code_store_root(ctx))
    _install_pi_integration(engine)
    click.echo(style.success(f"{label} host updated {style.dim(style.home_relative(path))}"))


@host_group.command("build")
@_host_engine_option
@click.option(
    "--source",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Pi host source checkout (required for source builds).",
)
@click.pass_context
def code_host_build_cmd(ctx: click.Context, engine: str, source: Path | None) -> None:
    """Build the host from a source checkout and install it."""
    module, label = _host_engine_module(engine)
    if source is None:
        raise click.ClickException("--source is required for Pi source builds")
    path = module.build_host_from_source(_code_store_root(ctx), source)
    _install_pi_integration(engine)
    click.echo(style.success(f"{label} host built and installed {style.dim(style.home_relative(path))}"))


@host_group.command("remove")
@_host_engine_option
@click.option("--yes", is_flag=True, help="Confirm removal without prompting.")
@click.pass_context
def code_host_remove_cmd(ctx: click.Context, engine: str, yes: bool) -> None:
    """Remove the managed host and its LemonCrow integration."""
    module, label = _host_engine_module(engine)
    if not yes and not click.confirm(f"Remove the managed {label} host and its LemonCrow integration?"):
        return
    removed = False
    if engine == "pi":
        from lemoncrow_client import pi_integration

        try:
            removed = pi_integration.remove()
        except (OSError, RuntimeError) as exc:
            raise click.ClickException(str(exc)) from exc
    removed = module.remove_host(_code_store_root(ctx)) or removed
    click.echo(style.success(f"{label} host removed" if removed else f"{label} host was not installed"))


@click.command("index", short_help="Build or refresh this repository's Workspaces Lite index.")
@click.option(
    "--repo-root",
    default=None,
    help="Repository root to index (default: git root of the current directory, else cwd).",
)
@click.option("--reindex", is_flag=True, help="Full rebuild from scratch (default: incremental).")
@click.option("--json", "as_json", is_flag=True)
@click.option("--frame-prefix", default="", hidden=True)
@click.option("--no-stats", is_flag=True, hidden=True)
def runtime_index_cmd(
    repo_root: str | None,
    reindex: bool,
    as_json: bool,
    frame_prefix: str,
    no_stats: bool,
) -> None:
    """Build or refresh the local open-source Workspaces Lite repository index."""
    del no_stats  # Lite output is already compact; accepted for background callers.
    if repo_root is None:
        from lemoncrow.core.foundation.paths import resolve_workspace_root

        lite_root = Path(resolve_workspace_root()).resolve()
    else:
        lite_root = Path(repo_root).expanduser().resolve()
    from lemoncrow_client.lite.index import LiteIndex

    if as_json:
        _emit(LiteIndex(lite_root).prepare(force=reindex).to_dict(), as_json=True)
        return
    report = _lite_index_with_progress(lite_root, force=reindex, frame_prefix=frame_prefix)
    prefix_markup = style.dim(frame_prefix) if frame_prefix else ""
    took = _format_lite_duration(report.elapsed_s)
    files = style.plural(report.files_seen, "file")
    if report.files_changed or report.files_deleted:
        headline = "Workspaces Lite index ready"
        summary = f"{files} · {report.files_changed:,} indexed · {report.files_deleted:,} deleted · {took}"
    else:
        headline = "Workspaces Lite index up to date"
        summary = f"{files} checked, nothing changed · {took}"
    line = f"{style.success(style.bold(headline))}: {summary}\n  {style.dim(style.home_relative(report.path))}"
    click.echo(f"{prefix_markup}{line}" if frame_prefix else line)


def _format_lite_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m{rest:02d}s"


_LITE_PHASES: dict[str, str] = {
    "seed": "Preparing",
    "lock": "Waiting for another lc to finish",
    "scan": "Indexing files",
    "prune": "Cleaning up",
    "commit": "Saving",
    "done": "Done",
}


def _lite_index_with_progress(lite_root: Path, *, force: bool, frame_prefix: str) -> LiteIndexReport:
    """Run a Runtime Lite prepare with a live progress bar on stderr."""

    from lemoncrow_client.lite.index import LiteIndex, LiteIndexProgress
    from rich.console import Console
    from rich.markup import escape
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeRemainingColumn,
    )
    from rich.table import Column

    console = Console(stderr=True, highlight=False)
    prefix = f"[dim]{escape(frame_prefix)}[/dim]" if frame_prefix else ""
    mode = "Rebuilding" if force else "Updating"
    brand = f"color({style.BRAND})"
    console.print(
        f"{prefix}[{brand}]◆[/{brand}] [bold]{mode} Workspaces Lite index[/bold] [dim]for "
        f"{escape(style.home_relative(lite_root))}[/dim]",
        soft_wrap=True,
    )
    # The bar takes whatever width the terminal has. The label and the changed
    # count get fixed widths so the bar does not twitch as they change.
    # The rare lock-wait label may push the bar once; sizing for it would leave a gap every run.
    label_width = max(len(label) for phase, label in _LITE_PHASES.items() if phase != "lock") + len(frame_prefix)
    progress = Progress(
        SpinnerColumn("dots", style=brand, finished_text="[green]✓[/green]"),
        TextColumn(f"{prefix}{{task.description}}", table_column=Column(min_width=label_width, no_wrap=True)),
        BarColumn(
            bar_width=None,
            style="grey30",
            complete_style=brand,
            finished_style="green",
            pulse_style=brand,
        ),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("[dim]eta[/dim]"),
        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
        TextColumn("[dim]{task.fields[changed]}[/dim]", table_column=Column(min_width=18, no_wrap=True)),
        console=console,
        transient=True,
        expand=True,
        disable=not console.is_terminal,
    )
    task_id = progress.add_task(_LITE_PHASES["seed"], total=None, changed="")

    def on_progress(seen: LiteIndexProgress) -> None:
        progress.update(
            task_id,
            description=_LITE_PHASES.get(seen.phase, seen.phase),
            completed=seen.done,
            total=seen.total or None,
            changed=f"· {seen.changed:,} changed" if seen.changed else "",
        )

    # An up-to-date index returns in well under a second and should not flash a bar.
    with style.after_delay(progress.start, progress.stop):
        return LiteIndex(lite_root).prepare(force=force, progress=on_progress)


@code_group.command("index", hidden=True)
@click.option(
    "--repo-root",
    default=None,
    help="Repository root to index (default: git root of the current directory, else cwd).",
)
@click.option("--include", "include_globs", multiple=True, hidden=True)
@click.option("--exclude", "exclude_globs", multiple=True, hidden=True)
@click.option("--reindex", is_flag=True, help="Full rebuild from scratch (default: incremental).")
@click.option(
    "--force",
    "steal_lock",
    is_flag=True,
    hidden=True,
)
@click.option("--json", "as_json", is_flag=True)
@click.option("--frame-prefix", default="", hidden=True, help="Prefix for progress output (used by dev.sh)")
@click.option("--no-stats", is_flag=True, hidden=True)
@click.option("--db-path", default=None, type=click.Path(), hidden=True)
@click.pass_context
def code_index_cmd(
    ctx: click.Context,
    repo_root: str | None,
    include_globs: tuple[str, ...],
    exclude_globs: tuple[str, ...],
    reindex: bool,
    steal_lock: bool,
    as_json: bool,
    no_stats: bool,
    frame_prefix: str,
    db_path: str | None,
) -> None:
    """Build or refresh the private development index."""
    _require_code_subcommand_internal_private(ctx, "index")
    if repo_root is None:
        # Resolve the same way the MCP code_search / read tools do
        # (LEMONCROW_WORKSPACE_ROOT / host env / git root / cwd) so the index the
        # CLI builds is keyed to the exact workspace those tools later query.
        from lemoncrow.core.foundation.paths import resolve_workspace_root

        repo_root = str(resolve_workspace_root())
    engine = _code_context_engine(repo_root, db_path=Path(db_path) if db_path else None)
    force = reindex
    if as_json:
        payload = engine.index_repo(
            force=force,
            require_lock=True,
            steal=steal_lock,
            include_globs=list(include_globs) or None,
            exclude_globs=list(exclude_globs) or None,
        ).model_dump(mode="json")
        # A successfully indexed repository is a durable LemonCrow project.
        # Persist it in the shared registry so service surfaces such as `/code`
        # can discover it even when no MCP process for this workspace is alive.
        from lemoncrow.core.service.project_registry import register_project

        register_project(Path(repo_root).resolve())
        try:
            engine._deleted_history_adapter()._ensure_history_ready()
        except Exception:
            logging.exception("Failed to prepare background indexes")
        try:
            _trigger_zoekt_with_progress(Path(repo_root).resolve(), quiet=True)
        except Exception:
            logging.exception("Failed to prewarm Zoekt index")
        _emit(payload, as_json=True)
        return

    payload = _index_repo_with_progress(
        engine,
        force=force,
        steal=steal_lock,
        include_globs=list(include_globs) or None,
        exclude_globs=list(exclude_globs) or None,
        description="Indexing code",
        success_description="Indexed code",
        frame_prefix=frame_prefix,
    )

    # Keep project discovery independent from transient MCP-session liveness.
    # This also backfills repositories initialized by older LemonCrow versions
    # the next time their existing index is refreshed.
    from lemoncrow.core.service.project_registry import register_project

    register_project(Path(repo_root).resolve())

    git_summary = _index_git_history_with_progress(engine, frame_prefix=frame_prefix)
    _trigger_zoekt_with_progress(Path(repo_root).resolve(), frame_prefix=frame_prefix)

    stats_line = (
        f"{click.style('✓', fg='green')}  Indexed {payload['files_indexed']} files, {payload['symbols_indexed']} "
        f"symbols ({payload['imports_indexed']} imports)"
    )
    prefix_markup = click.style(frame_prefix, dim=True) if frame_prefix else ""
    click.echo(f"{prefix_markup}{stats_line}" if frame_prefix else stats_line)

    if git_summary and git_summary.get("commits_walked", 0) > 0:
        walked = git_summary["commits_walked"]
        total_commits = git_summary.get("total_commits", walked)
        if total_commits > walked:
            commit_desc = f"{walked} of {total_commits} commits (rest indexing in background)"
        else:
            commit_desc = f"{walked} commits"
        git_line = (
            f"{click.style('✓', fg='green')}  Indexed Git history: "
            f"{commit_desc}, {git_summary['symbols_found']} deleted/renamed symbols "
            f"({git_summary['deletions_found']} deletions, {git_summary['renames_found']} renames)"
        )
        click.echo(f"{prefix_markup}{git_line}" if frame_prefix else git_line)

    if not no_stats:
        _print_index_stats(engine, frame_prefix=frame_prefix)


# A /tmp-sourced workspace (benchmark/eval runs) used to be pruned unconditionally,
# on sight, by the daily auto-update daemon -- with no check for whether it was
# still actively in use. That silently deleted a live, hours-long debugging session's
# index mid-investigation. Give /tmp-sourced workspaces the same last-touched grace
# period as everything else, just a shorter one: 5 days is enough to survive a
# multi-day debugging/benchmarking session while still reclaiming truly abandoned runs.
_TMP_BENCHMARK_GRACE_DAYS = 5


def _entry_age_days(entry: Path, now: float) -> float:
    """Days since the workspace dir was last touched.

    Uses the newest mtime among the dir and its top-level children — the SQLite
    index files and ``session_state.json`` are rewritten on each index/session,
    so this tracks last activity without an expensive recursive walk.
    """
    newest = 0.0
    with suppress(OSError):
        newest = entry.stat().st_mtime
    with suppress(OSError):
        for child in entry.iterdir():
            with suppress(OSError):
                newest = max(newest, child.stat().st_mtime)
    if newest <= 0:
        return 0.0
    return max(0.0, (now - newest) / 86_400.0)


@code_group.command("prune", hidden=True)
@click.option(
    "--store-root",
    default=None,
    help="LemonCrow store root (default: ~/.lemoncrow).",
)
@click.option(
    "--max-age-days",
    type=int,
    default=None,
    help="Also remove indexes whose source still exists but that have been inactive for more than N days.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be deleted without deleting anything.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON summary instead of human-readable lines.")
@click.pass_context
def code_prune_cmd(
    ctx: click.Context,
    store_root: str | None,
    max_age_days: int | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Internal cleanup for private-development workspace store dirs.

    Always removes dirs that are orphaned (no ``session_state.json``), came from
    a ``/tmp`` benchmark run, or whose source repo no longer exists.  With
    ``--max-age-days N`` it additionally removes indexes whose source still
    exists but that have not been touched in more than N days — they rebuild
    automatically on next use.

    Use --dry-run to preview what would be removed.
    """
    _require_code_subcommand_internal_private(ctx, "prune")

    import json
    import time

    from lemoncrow.core.foundation.paths import default_store_root

    root = Path(store_root).expanduser().resolve() if store_root else default_store_root()
    ws_dir = root / "workspaces"
    if not ws_dir.exists():
        if as_json:
            _emit({"deleted": 0, "skipped": 0, "freed_bytes": 0, "dry_run": dry_run, "entries": []}, as_json=True)
        else:
            click.echo("No workspaces directory found.")
        return

    now = time.time()

    def _classify(entry: Path) -> str | None:
        """Return a removal reason, or None to keep the dir."""
        ss = entry / "session_state.json"
        if not ss.exists():
            return "no session_state.json (orphaned index)"
        try:
            data = json.loads(ss.read_text("utf-8"))
        except Exception:
            return "unreadable session_state.json"
        transcript_path = str(data.get("transcript_path", ""))
        lemoncrow_root = str(data.get("lemoncrow_root", ""))
        if transcript_path.startswith("/tmp"):
            age_days = _entry_age_days(entry, now)
            if age_days <= _TMP_BENCHMARK_GRACE_DAYS:
                return None  # keep: still within the grace window, may be in active use
            return f"source was in /tmp (benchmark run), inactive {age_days:.0f}d"
        if lemoncrow_root and not Path(lemoncrow_root).exists():
            return f"source gone: {lemoncrow_root}"
        # Source still exists: only remove when explicitly GC-ing by age.
        if max_age_days is not None:
            age_days = _entry_age_days(entry, now)
            if age_days > max_age_days:
                return f"inactive {age_days:.0f}d (source still exists)"
        return None

    total_size = 0
    deleted = 0
    skipped = 0
    entries: list[dict[str, Any]] = []

    for entry in sorted(ws_dir.iterdir()):
        if not entry.is_dir():
            continue
        reason = _classify(entry)
        if reason is None:
            skipped += 1
            continue
        entry_bytes = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        if not dry_run:
            shutil.rmtree(entry, ignore_errors=True)
        if not as_json:
            verb = "would remove" if dry_run else "removed"
            click.echo(f"  {verb}  {entry.name}  ({entry_bytes / 1_000_000:.0f} MB)  — {reason}")
        entries.append({"name": entry.name, "bytes": entry_bytes, "reason": reason})
        deleted += 1
        total_size += entry_bytes

    if as_json:
        _emit(
            {
                "deleted": deleted,
                "skipped": skipped,
                "freed_bytes": total_size,
                "dry_run": dry_run,
                "entries": entries,
            },
            as_json=True,
        )
        return

    total_mb = total_size / 1_000_000
    verb = "Would free" if dry_run else "Freed"
    click.echo(f"\n{verb} {total_mb:.0f} MB across {deleted} workspace(s). {skipped} kept.")


def _print_index_stats(engine: Any, frame_prefix: str = "") -> None:
    """Print language and symbol-kind breakdown after indexing."""
    from pathlib import Path

    db_path = engine.db_path
    if not Path(db_path).exists():
        return
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Language breakdown
    rows = c.execute(
        "SELECT f.language, COUNT(DISTINCT f.file_path), COUNT(s.symbol_id) "
        "FROM files f LEFT JOIN symbols s ON s.repo_id = f.repo_id AND s.file_path = f.file_path "
        "GROUP BY f.language ORDER BY COUNT(DISTINCT f.file_path) DESC"
    ).fetchall()

    total_f = 0
    total_s = 0
    for _, fls, syms in rows:
        total_f += fls
        total_s += syms

    # Symbol kinds (top ones)
    kinds = c.execute("SELECT kind, COUNT(*) FROM symbols GROUP BY kind ORDER BY COUNT(*) DESC").fetchall()

    # Embedding stats
    from lemoncrow.core.capabilities.code_intelligence_provider import (
        advanced_code_intelligence_helper,
    )

    ranker_configured = getattr(engine._semantic_ranker, "available", False)
    flag_enabled = bool(advanced_code_intelligence_helper("ann_retrieval_enabled"))
    should_show_embeddings = ranker_configured or flag_enabled

    embedding_count = 0
    try:
        table_exists = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='symbol_vectors'"
        ).fetchone()
        if table_exists:
            embedding_count = c.execute("SELECT COUNT(*) FROM symbol_vectors").fetchone()[0]
    except Exception:
        logging.exception("Failed to query symbol_vectors table")

    graveyard_exists = bool(
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='symbol_graveyard'").fetchone()
    )

    prefix_markup = click.style(frame_prefix, dim=True) if frame_prefix else ""

    try:
        from rich import box
        from rich.console import Console
        from rich.table import Table

        console = Console()

        def print_prefixed(renderable: Any) -> None:
            # Capture what rich would print, then write it with the prefix
            with console.capture() as cap:
                console.print(renderable)
            text = cap.get()
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            for line in lines:
                if line.strip():
                    click.echo(f"{prefix_markup}  {line}")
                else:
                    click.echo(f"{prefix_markup}")

        # Language breakdown
        print_prefixed("")
        print_prefixed("[bold bright_white]Language breakdown[/]  [dim]by files and symbols[/]")

        lang_table = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="dim",
            padding=(0, 1),
            show_footer=True,
        )
        lang_table.add_column("Language", style="bold", min_width=15, footer="TOTAL")
        lang_table.add_column("Files", justify="right", footer=f"{total_f:,}")
        lang_table.add_column("Symbols", justify="right", footer=f"{total_s:,}")

        lang_styles = {
            "python": ("Python", "bright_yellow"),
            "typescript": ("TypeScript", "bright_cyan"),
            "javascript": ("JavaScript", "yellow"),
            "rust": ("Rust", "red"),
            "go": ("Go", "cyan"),
            "swift": ("Swift", "orange3"),
            "kotlin": ("Kotlin", "bright_magenta"),
            "java": ("Java", "bright_blue"),
            "c/c++": ("C/C++", "blue"),
            "cpp": ("C++", "blue"),
            "c": ("C", "blue"),
            "csharp": ("C#", "bright_green"),
            "ruby": ("Ruby", "red"),
            "php": ("PHP", "magenta"),
            "scala": ("Scala", "bright_red"),
            "bash": ("Shell", "green"),
            "shell": ("Shell", "green"),
            "html": ("HTML", "orange1"),
            "css": ("CSS", "bright_blue"),
            "toml": ("TOML", "dim white"),
            "yaml": ("YAML", "dim white"),
            "json": ("JSON", "dim white"),
            "markdown": ("Markdown", "dim white"),
            "sql": ("SQL", "dim white"),
            "astro": ("Astro", "bright_cyan"),
        }

        for lang, fls, syms in rows:
            if lang in lang_styles:
                display_name, color = lang_styles[lang]
            else:
                display_name = lang.title() if lang else "Unknown"
                color = "white"
            lang_table.add_row(f"[{color}]{display_name}[/]", f"{fls:,}", f"{syms:,}")

        print_prefixed(lang_table)

        # Symbol kinds
        if kinds:
            print_prefixed("")
            print_prefixed("[bold bright_white]Symbol kinds[/]  [dim]top kinds by count[/]")

            kind_table = Table(
                box=box.SIMPLE,
                show_header=True,
                header_style="dim",
                padding=(0, 1),
                show_footer=True,
            )
            kind_table.add_column("Symbol Kind", style="bold", min_width=20, footer="TOTAL")
            kind_table.add_column("Count", justify="right", footer=f"{sum(cnt for _, cnt in kinds):,}")

            kind_styles = {
                "class": "bright_blue",
                "interface": "bright_blue",
                "struct": "bright_blue",
                "method": "bright_cyan",
                "function": "cyan",
                "async_function": "cyan",
                "variable": "white",
                "heading": "dim white",
                "type": "bright_green",
                "module": "bright_magenta",
                "import": "dim white",
            }

            for kind, cnt in kinds:
                display_kind = kind.replace("_", " ").title() if kind else "Unknown"
                color = kind_styles.get(kind, "white")
                kind_table.add_row(f"[{color}]{display_kind}[/]", f"{cnt:,}")

            print_prefixed(kind_table)

        # Embeddings stats
        if should_show_embeddings:
            print_prefixed("")
            if embedding_count > 0:
                print_prefixed(f"[bold bright_white]Embeddings[/]  [dim]total indexed: {embedding_count:,}[/]")
            elif not ranker_configured:
                print_prefixed("[bold bright_white]Embeddings[/]  [dim]flag enabled, but provider not configured[/]")
            else:
                print_prefixed("[bold bright_white]Embeddings[/]  [dim]enabled, awaiting indexing[/]")

        # Git history graveyard stats
        try:
            graveyard_count = (
                conn.execute("SELECT COUNT(*) FROM symbol_graveyard").fetchone()[0] if graveyard_exists else 0
            )
            if graveyard_count > 0:
                print_prefixed("")
                print_prefixed("[bold bright_white]Git history[/]  [dim]deleted and renamed symbols[/]")

                graveyard_langs = conn.execute(
                    "SELECT language, COUNT(*) FROM symbol_graveyard WHERE language IS NOT NULL GROUP BY language ORDER BY COUNT(*) DESC"
                ).fetchall()

                graveyard_table = Table(
                    box=box.SIMPLE,
                    show_header=True,
                    header_style="dim",
                    padding=(0, 1),
                    show_footer=True,
                )
                graveyard_table.add_column("Language", style="bold", min_width=15, footer="TOTAL")
                graveyard_table.add_column("Deleted", justify="right")

                total_deleted = 0
                for lang, cnt in graveyard_langs:
                    total_deleted += cnt
                    if lang in lang_styles:
                        display_name, color = lang_styles[lang]
                    else:
                        display_name = lang.title() if lang else "Unknown"
                        color = "white"
                    graveyard_table.add_row(f"[{color}]{display_name}[/]", f"{cnt:,}")

                graveyard_table.columns[1].footer = f"{total_deleted:,}"
                print_prefixed(graveyard_table)
        except Exception:
            logging.exception("Failed to query symbol_graveyard")

    except ImportError:
        # Fallback to simple prints if rich is not available, but with prefix support
        click.echo(f"{prefix_markup}")
        click.echo(f"{prefix_markup}  ── Language breakdown ──")
        click.echo(f"{prefix_markup}  {'Language':<15s}  {'Files':>5s}  {'Symbols':>7s}")
        click.echo(f"{prefix_markup}  " + "-" * 35)
        for lang, fls, syms in rows:
            click.echo(f"{prefix_markup}  {lang:<15s}  {fls:>5d}  {syms:>7d}")
        click.echo(f"{prefix_markup}  " + "-" * 35)
        click.echo(f"{prefix_markup}  {'TOTAL':<15s}  {total_f:>5d}  {total_s:>7d}")

        if kinds:
            click.echo(f"{prefix_markup}")
            click.echo(f"{prefix_markup}  ── Symbol kinds ──")
            click.echo(f"{prefix_markup}  {'Kind':<20s}  {'Count':>7s}")
            click.echo(f"{prefix_markup}  " + "-" * 29)
            for kind, cnt in kinds:
                click.echo(f"{prefix_markup}  {kind:<20s}  {cnt:>7d}")

        # Git history in fallback
        try:
            graveyard_count = (
                conn.execute("SELECT COUNT(*) FROM symbol_graveyard").fetchone()[0] if graveyard_exists else 0
            )
            if graveyard_count > 0:
                graveyard_langs = conn.execute(
                    "SELECT language, COUNT(*) FROM symbol_graveyard WHERE language IS NOT NULL GROUP BY language ORDER BY COUNT(*) DESC"
                ).fetchall()

                click.echo(f"{prefix_markup}")
                click.echo(f"{prefix_markup}  ── Git history (deleted symbols) ──")
                click.echo(f"{prefix_markup}  {'Language':<15s}  {'Count':>7s}")
                click.echo(f"{prefix_markup}  " + "-" * 25)
                total_deleted = 0
                for lang, cnt in graveyard_langs:
                    total_deleted += cnt
                    click.echo(f"{prefix_markup}  {lang:<15s}  {cnt:>7d}")
                click.echo(f"{prefix_markup}  " + "-" * 25)
                click.echo(f"{prefix_markup}  {'TOTAL':<15s}  {total_deleted:>7d}")
        except Exception:
            logging.exception("Failed to query symbol_graveyard in fallback")

    conn.close()


@code_group.command("train", hidden=True)
@click.option(
    "--name",
    "name",
    required=True,
    type=click.Choice(["embedding"]),
    help="What to train. Currently: embedding (a per-repo code embedder).",
)
@click.option("--repo-root", default=".", show_default=True, help="Repository to specialise the embedder for.")
@click.option("--base-model", default="BAAI/bge-code-v1", show_default=True)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(),
    help="Where to save the finetuned model (default: <store>/embeddings/<repo>).",
)
@click.option(
    "--pairs-per-file",
    type=int,
    default=5,
    show_default=True,
    help="Synthetic (query->gold-file) pairs mined per source file.",
)
@click.option("--epochs", type=int, default=2, show_default=True)
@click.option("--batch-size", type=int, default=16, show_default=True)
@click.option("--max-seq", type=int, default=512, show_default=True)
@click.option("--learning-rate", type=float, default=2e-5, show_default=True)
@click.option("--dry-run", is_flag=True, help="Print the pipeline plan and exit without training.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def code_train_cmd(
    ctx: click.Context,
    name: str,
    repo_root: str,
    base_model: str,
    output_dir: str | None,
    pairs_per_file: int,
    epochs: int,
    batch_size: int,
    max_seq: int,
    learning_rate: float,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Internal private-development per-repo embedder training.

    Pipeline (GPU-bound steps run on CUDA when available):
      1. mine synthetic (query -> gold-file) pairs from the repo source;
      2. build the private embedder's train/test/corpus split;
      3. finetune the base model and report held-out base-vs-finetuned MRR.

    Steps 2-3 are supplied by the private enterprise runtime (its ``semantic``
    extra provides the model dependencies); without it this command fails with
    the private-code-intelligence-unavailable message.

    Measured lift on the grep-style retrieval benchmark is ~0: the bench
    queries are lexical, so lexical+zoekt already wins. This command is the productised shape, wired for
    NL-query training data where the embedder is expected to help.
    """
    _require_code_subcommand_internal_private(ctx, "train")
    import sys

    from lemoncrow.core.capabilities.code_intelligence_provider import (
        ProEngineUnavailable,
        advanced_code_intelligence_helper,
    )

    try:
        scripts = advanced_code_intelligence_helper("code_embedder_training_scripts")
    except ProEngineUnavailable as exc:
        raise click.ClickException(str(exc)) from exc

    # The pair miner is public benchmark tooling in the LemonCrow source tree,
    # resolved relative to this module: src/lemoncrow/gateway/cli/commands/code.py -> repo root.
    src_root = Path(__file__).resolve().parents[5]
    miner = src_root / "benchmarks/codebench/synthetic_pair_miner.py"
    prep = Path(scripts["prepare"])
    trainer = Path(scripts["train"])
    missing = [str(p) for p in (miner, prep, trainer) if not p.is_file()]
    if missing:
        raise click.ClickException(
            "Training scripts are only available in a source checkout; missing: " + ", ".join(missing)
        )

    repo = Path(repo_root).resolve()
    if output_dir:
        out = Path(output_dir).resolve()
    else:
        from lemoncrow.core.foundation.paths import resolve_workspace_store_dir

        out = resolve_workspace_store_dir(workspace_root=repo) / "embeddings"
    work = out / "work"
    pairs_json = work / "pairs.json"
    data_dir = work / "data"

    steps = [
        [
            sys.executable,
            str(miner),
            "--repo-dir",
            str(repo),
            "--out",
            str(pairs_json),
            "--pairs-per-file",
            str(pairs_per_file),
        ],
        [sys.executable, str(prep), "--pairs", str(pairs_json), "--repo-dir", str(repo), "--out-dir", str(data_dir)],
        [
            sys.executable,
            str(trainer),
            "--train-data",
            str(data_dir),
            "--model",
            base_model,
            "--output-dir",
            str(out),
            "--epochs",
            str(epochs),
            "--batch-size",
            str(batch_size),
            "--max-seq",
            str(max_seq),
            "--learning-rate",
            str(learning_rate),
            "--compare-baseline",
        ],
    ]
    plan = {
        "name": name,
        "repo": str(repo),
        "base_model": base_model,
        "output_dir": str(out),
        "steps": [" ".join(s) for s in steps],
    }
    if dry_run:
        _emit(plan, as_json=as_json)
        return

    missing_dependencies = advanced_code_intelligence_helper("missing_code_embedder_training_dependencies")
    if missing_dependencies:
        raise click.ClickException(
            "Code-embedder training needs the private runtime's `semantic` extra "
            f"(missing: {', '.join(missing_dependencies)})."
        )

    work.mkdir(parents=True, exist_ok=True)
    for i, step in enumerate(steps, 1):
        click.echo(f"[train] step {i}/{len(steps)}: {Path(step[1]).name}", err=True)
        if subprocess.run(step, check=False).returncode != 0:
            raise click.ClickException(f"step {i} ({Path(step[1]).name}) failed")
    _emit({"name": name, "repo": str(repo), "model": str(out)}, as_json=as_json)


__all__ = [
    "_code_context_engine",
    "_index_repo_with_progress",
    "code_group",
    "host_group",
    "runtime_index_cmd",
    "zoekt_group",
]
