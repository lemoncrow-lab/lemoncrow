"""Thin ``atelier swarm`` command group for isolated multi-worktree attempts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

from atelier.core.capabilities.swarm import (
    discover_repo_root,
    format_swarm_summary,
    initialize_swarm_run,
    launch_swarm_children,
    load_swarm_state,
    resolve_state_path,
    run_child_once,
    save_swarm_state,
    stop_swarm_run,
)
from atelier.gateway.cli.commands._shared import _emit


@click.group("swarm")
def swarm_group() -> None:
    """Coordinate isolated child attempts in separate git worktrees."""


@swarm_group.command("start")
@click.argument("spec_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--runs", default=3, show_default=True, type=int, help="Number of isolated child attempts.")
@click.option(
    "--validate",
    "validation_commands",
    multiple=True,
    help="Validation command to run inside each child worktree after the main command.",
)
@click.option(
    "--detach",
    is_flag=True,
    help="Launch the coordinator in the background and return immediately.",
)
@click.option(
    "--cleanup/--keep-worktrees",
    default=False,
    show_default=True,
    help="Remove child worktrees after completion instead of leaving them for inspection.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable run metadata.")
@click.argument("child_command", nargs=-1, type=str)
@click.pass_context
def swarm_start(
    ctx: click.Context,
    spec_path: Path,
    runs: int,
    validation_commands: tuple[str, ...],
    detach: bool,
    cleanup: bool,
    as_json: bool,
    child_command: tuple[str, ...],
) -> None:
    """Create isolated git worktrees and launch one child wrapper per attempt.

    Pass the child agent command after ``--``. The command receives a per-child
    ``ATELIER_ROOT``, workspace root, and ``ATELIER_SWARM_SPEC_PATH``.
    """

    if runs < 1:
        raise click.ClickException("--runs must be >= 1")
    if not child_command:
        raise click.ClickException("pass the child agent command after '--'")
    repo_root = discover_repo_root(Path.cwd())
    root = Path(ctx.obj["root"])
    state, state_path = initialize_swarm_run(
        root=root,
        repo_root=repo_root,
        spec_path=spec_path,
        child_command=list(child_command),
        runs=runs,
        validation_commands=list(validation_commands),
        keep_worktrees=not cleanup,
        detached=detach,
    )
    if detach:
        log_path = state_path.parent / "coordinator.log"
        with log_path.open("w", encoding="utf-8") as handle:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "atelier.gateway.cli",
                    "--root",
                    str(root),
                    "swarm",
                    "_run",
                    "--state",
                    str(state_path),
                ],
                cwd=repo_root,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        state.coordinator_pid = proc.pid
        save_swarm_state(state_path, state)
        payload = {
            "run_id": state.run_id,
            "status": "running",
            "state_path": str(state_path),
            "coordinator_pid": proc.pid,
            "log_path": str(log_path),
        }
        if as_json:
            _emit(payload, as_json=True)
            return
        click.echo(f"run_id: {state.run_id}\nstatus: running\ncoordinator_pid: {proc.pid}\nstate_path: {state_path}")
        return

    completed = launch_swarm_children(root, state_path)
    payload = completed.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(format_swarm_summary(completed))


@swarm_group.command("status")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.pass_context
def swarm_status(ctx: click.Context, run_id: str, as_json: bool) -> None:
    """Show persisted coordinator state for RUN_ID."""

    state_path = resolve_state_path(ctx.obj["root"], run_id)
    if not state_path.exists():
        raise click.ClickException(f"unknown swarm run: {run_id}")
    state = load_swarm_state(state_path)
    if as_json:
        _emit(state.model_dump(mode="json"), as_json=True)
        return
    click.echo(format_swarm_summary(state))


@swarm_group.command("stop")
@click.argument("run_id")
@click.option("--cleanup", is_flag=True, help="Also remove the child git worktrees.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.pass_context
def swarm_stop(ctx: click.Context, run_id: str, cleanup: bool, as_json: bool) -> None:
    """Stop a running swarm coordinator and optionally clean up its worktrees."""

    state_path = resolve_state_path(ctx.obj["root"], run_id)
    if not state_path.exists():
        raise click.ClickException(f"unknown swarm run: {run_id}")
    state = stop_swarm_run(
        root=ctx.obj["root"],
        state_path=state_path,
        cleanup=cleanup,
    )
    if as_json:
        _emit(state.model_dump(mode="json"), as_json=True)
        return
    click.echo(format_swarm_summary(state))


@swarm_group.command("_run", hidden=True)
@click.option(
    "--state",
    "state_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.pass_context
def swarm_run(ctx: click.Context, state_path: Path) -> None:
    """Run the coordinator loop for a previously initialized swarm."""

    state = launch_swarm_children(Path(ctx.obj["root"]), state_path)
    click.echo(format_swarm_summary(state))


@swarm_group.command("_child-run", hidden=True)
@click.option(
    "--state",
    "state_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--child-id", required=True)
def swarm_child_run(state_path: Path, child_id: str) -> None:
    """Execute one child wrapper inside its dedicated worktree."""

    child = run_child_once(state_path, child_id)
    _emit(child.model_dump(mode="json"), as_json=True)


__all__ = ["swarm_group"]
