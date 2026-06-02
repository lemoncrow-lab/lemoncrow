"""Thin ``atelier swarm`` command group for isolated multi-worktree attempts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import click

from atelier.core.capabilities.swarm import (
    build_swarm_apply_payload,
    build_swarm_export_payload,
    discover_repo_root,
    format_swarm_summary,
    initialize_swarm_run,
    launch_swarm_children,
    list_swarm_runner_profiles,
    list_swarm_runs,
    load_swarm_state,
    read_swarm_log,
    resolve_state_path,
    resolve_swarm_child_command,
    resolve_swarm_runner_metadata,
    resolve_swarm_spec_path,
    run_child_once,
    run_provider_swarm_worker,
    save_swarm_state,
    spawn_swarm_coordinator,
    stop_swarm_run,
)
from atelier.core.capabilities.swarm.models import SwarmEvaluatorBackend
from atelier.gateway.cli.commands._shared import _emit

DEFAULT_RUNNER_PROMPT = (
    "The authoritative task spec is stored at {spec}.\n\n"
    "<task_spec>\n"
    "{spec_contents}\n"
    "</task_spec>\n\n"
    "Work directly in the current repository, make only the requested changes, "
    "do not commit, and print a concise summary of what you changed or why you "
    "left it unchanged."
)

RUNNER_CHOICES = click.Choice([profile["id"] for profile in list_swarm_runner_profiles()], case_sensitive=False)


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
        "run_id                           status    runner           model               wave  accepted fail live planned/max  created_at",
        "--------------------------------------------------------------------------------------------------------------------------------",
    ]
    for state in states:
        running = sum(1 for child in state.children if child.status == "running")
        failed = sum(1 for child in state.children if child.status == "failed")
        runner_label = state.runner_name[:16]
        model_label = (state.runner_model or "-")[:18]
        latest_wave = state.waves[-1] if state.waves else None
        planned = latest_wave.planned_runs if latest_wave is not None else 0
        max_runs = latest_wave.max_runs if latest_wave is not None else (state.max_runs or state.runs)
        lines.append(
            f"{state.run_id:<32} {state.status:<9} {runner_label:<16} {model_label:<18} {state.current_wave:<5} {len(state.accepted_child_ids):>8} {failed:<4} {running:<4} {planned:>3}/{max_runs:<7} {state.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    click.echo("\n".join(lines))


@swarm_group.command("start")
@click.argument("spec_path", required=False, type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--runs",
    default=3,
    show_default=True,
    type=int,
    help="Maximum children to launch per wave. The coordinator may launch fewer for bounded scopes.",
)
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
    "--max-waves",
    default=0,
    show_default=True,
    type=int,
    help="Hard cap on waves for continuous mode. Use 0 for no explicit cap.",
)
@click.option(
    "--evaluator-backend",
    type=click.Choice(["auto", "disabled", "ollama", "openai", "litellm"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Backend used by the semantic evaluator that judges wave outcomes.",
)
@click.option(
    "--evaluator-model",
    help="Optional model override for the semantic evaluator.",
)
@click.option(
    "--max-evaluator-failures",
    default=3,
    show_default=True,
    type=int,
    help="How many consecutive evaluator failures continuous mode tolerates before stopping.",
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
    spec_path: Path | None,
    runs: int,
    validation_commands: tuple[str, ...],
    detach: bool,
    continuous: bool,
    max_waves: int,
    evaluator_backend: str,
    evaluator_model: str | None,
    max_evaluator_failures: int,
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
    if max_waves < 0:
        raise click.ClickException("--max-waves must be >= 0")
    if max_evaluator_failures < 1:
        raise click.ClickException("--max-evaluator-failures must be >= 1")
    repo_root = discover_repo_root(Path.cwd())
    root = Path(ctx.obj["root"])
    try:
        resolved_spec_path, spec_resolution, used_program_md = resolve_swarm_spec_path(
            project_root=repo_root,
            spec_path=spec_path,
        )
        resolved_child_command = resolve_swarm_child_command(
            runner=runner,
            runner_model=runner_model,
            runner_args=runner_args,
            child_command=child_command,
            prompt_template=DEFAULT_RUNNER_PROMPT,
        )
        resolved_runner_name, resolved_runner_model = resolve_swarm_runner_metadata(
            runner=runner,
            runner_model=runner_model,
            child_command=resolved_child_command,
        )
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    state, state_path = initialize_swarm_run(
        root=root,
        repo_root=repo_root,
        spec_path=resolved_spec_path,
        spec_source_path=str(spec_path) if spec_path is not None else str(resolved_spec_path),
        spec_resolution=spec_resolution,
        used_program_md=used_program_md,
        runner_name=resolved_runner_name,
        runner_model=resolved_runner_model,
        child_command=resolved_child_command,
        runs=runs,
        validation_commands=list(validation_commands),
        keep_worktrees=not cleanup,
        detached=detach,
        continuous=continuous,
        max_waves=max_waves if continuous else 1,
        launch_provider="cli",
        evaluator_backend=cast(SwarmEvaluatorBackend, evaluator_backend),
        evaluator_model=evaluator_model or "",
        max_evaluator_failures=max_evaluator_failures,
    )
    if detach:
        coordinator_pid, log_path = spawn_swarm_coordinator(root, repo_root, state_path)
        state.coordinator_pid = coordinator_pid
        state.coordinator_log_path = str(log_path)
        save_swarm_state(state_path, state)
        payload = {
            "run_id": state.run_id,
            "status": "running",
            "state_path": str(state_path),
            "coordinator_pid": coordinator_pid,
            "log_path": str(log_path),
        }
        if as_json:
            _emit(payload, as_json=True)
            return
        click.echo(
            f"run_id: {state.run_id}\nstatus: running\ncoordinator_pid: {coordinator_pid}\nstate_path: {state_path}"
        )
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


@swarm_group.command("export")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable export metadata.")
@click.pass_context
def swarm_export(ctx: click.Context, run_id: str, as_json: bool) -> None:
    """Show durable export artifacts for RUN_ID."""

    state_path = resolve_state_path(ctx.obj["root"], run_id)
    if not state_path.exists():
        raise click.ClickException(f"unknown swarm run: {run_id}")
    state = load_swarm_state(state_path)
    payload = build_swarm_export_payload(state)
    if as_json:
        _emit(payload, as_json=True)
        return
    lines = [
        f"run_id: {state.run_id}",
        f"base_snapshot_ref: {state.base_snapshot_ref}",
        f"integration_base_ref: {state.integration_base_ref}",
    ]
    if state.base_snapshot_artifact is not None:
        lines.append(f"base_snapshot_artifact: {state.base_snapshot_artifact.path}")
    lines.append("accepted_commits:")
    for accepted in state.accepted_commits:
        lines.append(f"  - {accepted.child_id}: {accepted.commit_ref} patch={accepted.patch_path or '-'}")
    lines.append("artifacts:")
    for artifact in state.export_artifacts:
        lines.append(f"  - {artifact.kind}: {artifact.path}")
    lines.append("transplant_commands:")
    for command in state.transplant_commands or ["(none)"]:
        lines.append(f"  - {command}")
    click.echo("\n".join(lines))


@swarm_group.command("apply")
@click.argument("run_id")
@click.option("--wave", "wave_index", type=int, help="Limit to accepted commits from one wave.")
@click.option("--child-id", help="Limit to one accepted child.")
@click.option("--execute", is_flag=True, help="Actually execute the transplant commands.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable apply instructions.")
@click.pass_context
def swarm_apply(
    ctx: click.Context,
    run_id: str,
    wave_index: int | None,
    child_id: str | None,
    execute: bool,
    as_json: bool,
) -> None:
    """Show or execute transplant commands for accepted commits."""

    state_path = resolve_state_path(ctx.obj["root"], run_id)
    if not state_path.exists():
        raise click.ClickException(f"unknown swarm run: {run_id}")
    state = load_swarm_state(state_path)
    try:
        payload = build_swarm_apply_payload(state, wave_index=wave_index, child_id=child_id)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return

    selected_commits = cast(list[dict[str, Any]], payload["selected_commits"])
    commands = cast(list[str], payload["commands"])

    if not selected_commits:
        click.echo("No accepted commits found to apply.")
        return

    lines = [
        f"run_id: {state.run_id}",
        f"base_snapshot_ref: {state.base_snapshot_ref}",
        f"selected_commits: {len(selected_commits)}",
        "\nCOMMIT SUMMARIES:",
    ]

    for commit in selected_commits:
        header = f"  - {commit['child_id']} ({commit['commit_ref'][:8]})"
        if commit.get("score") is not None:
            header += f" score={commit['score']:.1f}"
        lines.append(header)
        if commit.get("summary"):
            for s_line in commit["summary"].strip().splitlines():
                lines.append(f"    {s_line}")

    lines.append("\nCOMMANDS:")
    for command in commands or ["(none)"]:
        lines.append(f"  - {command}")

    click.echo("\n".join(lines))

    if execute:
        if not commands:
            click.echo("\nNothing to execute.")
            return

        click.echo("\nExecuting transplant commands...")
        import subprocess

        for command in commands:
            click.echo(f"  > {command}")
            try:
                # We use shell=True because commands are formatted strings with paths and multiple refs
                subprocess.run(command, shell=True, check=True, cwd=ctx.obj["root"])
            except subprocess.CalledProcessError as exc:
                raise click.ClickException(f"Command failed: {command}\n{exc}") from exc
        click.echo("\nSuccessfully applied all changes.")
    else:
        # Show the user how to actually apply the changes
        click.echo("\nTo apply these changes to your current repository, run:")
        click.echo(f"  uv run atelier --root {ctx.obj['root']} swarm apply {run_id} --execute")
        click.echo(
            "\nThis will execute the git cherry-pick and git apply commands sequentially in your current repository."
        )
        click.echo("\nWarning: Before running with --execute, please make sure your working directory is clean")
        click.echo(
            "(commit or stash your current changes) to avoid potential merge conflicts during the cherry-pick process."
        )


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


@swarm_group.command("_provider-worker", hidden=True)
def swarm_provider_worker() -> None:
    """Execute the provider-backed hidden swarm worker."""

    raise SystemExit(run_provider_swarm_worker())


__all__ = ["swarm_group"]
