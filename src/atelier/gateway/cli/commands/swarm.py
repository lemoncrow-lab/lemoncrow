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
    list_swarm_runs,
    load_swarm_state,
    read_swarm_log,
    resolve_state_path,
    run_child_once,
    save_swarm_state,
    stop_swarm_run,
)
from atelier.gateway.cli.commands._shared import _emit

DEFAULT_RUNNER_PROMPT = (
    "Read the task spec at {spec}. Work directly in the current repository, "
    "make only the requested changes, do not commit, and print a concise "
    "summary of what you changed or why you left it unchanged."
)

RUNNER_CHOICES = click.Choice(
    ["claude", "codex", "copilot", "opencode", "ollama-claude"],
    case_sensitive=False,
)


def _resolve_child_command(
    *,
    runner: str | None,
    runner_model: str | None,
    runner_args: tuple[str, ...],
    child_command: tuple[str, ...],
) -> list[str]:
    if runner and child_command:
        raise click.ClickException("choose either --runner or a raw child command after '--', not both")
    if child_command:
        return list(child_command)
    if not runner:
        raise click.ClickException("pass a raw child command after '--' or select a built-in --runner")

    profile = runner.lower()
    if profile == "claude":
        command = ["claude"]
        if runner_model:
            command.extend(["--model", runner_model])
        command.extend(["--dangerously-skip-permissions", *runner_args, "-p", DEFAULT_RUNNER_PROMPT])
        return command
    if profile == "codex":
        command = ["codex", "exec"]
        if runner_model:
            command.extend(["-m", runner_model])
        command.extend(
            [
                "--dangerously-bypass-approvals-and-sandbox",
                *runner_args,
                DEFAULT_RUNNER_PROMPT,
            ]
        )
        return command
    if profile == "copilot":
        command = ["copilot"]
        if runner_model:
            command.extend(["--model", runner_model])
        command.extend(["--allow-all", *runner_args, "-p", DEFAULT_RUNNER_PROMPT])
        return command
    if profile == "opencode":
        command = ["opencode", "run"]
        if runner_model:
            command.extend(["-m", runner_model])
        command.extend(
            [
                "--dangerously-skip-permissions",
                *runner_args,
                DEFAULT_RUNNER_PROMPT,
            ]
        )
        return command
    if profile == "ollama-claude":
        command = ["ollama", "launch", "claude"]
        if runner_model:
            command.extend(["--model", runner_model])
        command.extend(
            [
                "--",
                "-p",
                DEFAULT_RUNNER_PROMPT,
                "--dangerously-skip-permissions",
                *runner_args,
            ]
        )
        return command
    raise click.ClickException(f"unsupported runner profile: {runner}")


def _resolve_runner_metadata(
    *, runner: str | None, runner_model: str | None, child_command: list[str]
) -> tuple[str, str]:
    if runner:
        return runner.lower(), runner_model or ""
    if not child_command:
        return "custom", ""
    inferred_model = ""
    for index, token in enumerate(child_command[:-1]):
        if token in {"--model", "-m"} and index + 1 < len(child_command):
            inferred_model = child_command[index + 1]
            break
    return child_command[0], inferred_model


@click.group("swarm")
def swarm_group() -> None:
    """Coordinate isolated child attempts in separate git worktrees."""


@swarm_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.pass_context
def swarm_list(ctx: click.Context, as_json: bool) -> None:
    """Show all known swarm runs under the current Atelier root."""

    states = list_swarm_runs(Path(ctx.obj["root"]))
    if as_json:
        _emit([state.model_dump(mode="json") for state in states], as_json=True)
        return
    if not states:
        click.echo("No swarm runs found.")
        return
    lines = [
        "run_id                           status    runner           model               wave  ok/fail/run  created_at",
        "---------------------------------------------------------------------------------------------------------------",
    ]
    for state in states:
        running = sum(1 for child in state.children if child.status == "running")
        failed = sum(1 for child in state.children if child.status == "failed")
        runner_label = state.runner_name[:16]
        model_label = (state.runner_model or "-")[:18]
        lines.append(
            f"{state.run_id:<32} {state.status:<9} {runner_label:<16} {model_label:<18} {state.current_wave:<5} {len(state.accepted_child_ids):>2}/{failed:<4}/{running:<3} {state.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    click.echo("\n".join(lines))


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
    "--continuous",
    is_flag=True,
    help="Keep launching new waves until a wave produces no accepted improvements or the swarm is explicitly stopped.",
)
@click.option(
    "--runner",
    type=RUNNER_CHOICES,
    help="Built-in child runner profile instead of passing a raw command after '--'.",
)
@click.option(
    "--runner-model",
    help="Model name for the selected runner profile (for example claude-opus-4-8 or qwen3.6).",
)
@click.option(
    "--runner-arg",
    "runner_args",
    multiple=True,
    help="Extra argument to append to the selected runner profile.",
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
    continuous: bool,
    runner: str | None,
    runner_model: str | None,
    runner_args: tuple[str, ...],
    cleanup: bool,
    as_json: bool,
    child_command: tuple[str, ...],
) -> None:
    """Create isolated git worktrees and launch one child wrapper per attempt.

    Pass a raw child agent command after ``--`` or choose a built-in
    ``--runner`` profile. The child command receives a per-child
    ``ATELIER_ROOT``, workspace root, and ``ATELIER_SWARM_SPEC_PATH``.
    """

    if runs < 1:
        raise click.ClickException("--runs must be >= 1")
    repo_root = discover_repo_root(Path.cwd())
    root = Path(ctx.obj["root"])
    resolved_child_command = _resolve_child_command(
        runner=runner,
        runner_model=runner_model,
        runner_args=runner_args,
        child_command=child_command,
    )
    resolved_runner_name, resolved_runner_model = _resolve_runner_metadata(
        runner=runner,
        runner_model=runner_model,
        child_command=resolved_child_command,
    )
    state, state_path = initialize_swarm_run(
        root=root,
        repo_root=repo_root,
        spec_path=spec_path,
        runner_name=resolved_runner_name,
        runner_model=resolved_runner_model,
        child_command=resolved_child_command,
        runs=runs,
        validation_commands=list(validation_commands),
        keep_worktrees=not cleanup,
        detached=detach,
        continuous=continuous,
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
        state.coordinator_log_path = str(log_path)
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


@swarm_group.command("logs")
@click.argument("run_id")
@click.option("--child-id", help="Show a specific child log instead of the coordinator log.")
@click.option("--stderr", is_flag=True, help="Read stderr instead of stdout for child logs.")
@click.option("--tail", default=40, show_default=True, type=int, help="Number of lines to print.")
@click.pass_context
def swarm_logs(
    ctx: click.Context,
    run_id: str,
    child_id: str | None,
    stderr: bool,
    tail: int,
) -> None:
    """Tail coordinator or child logs for a swarm run."""

    try:
        content = read_swarm_log(
            Path(ctx.obj["root"]),
            run_id,
            child_id=child_id,
            stderr=stderr,
            tail=tail,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(content)


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
