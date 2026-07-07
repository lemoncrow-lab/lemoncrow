"""``atelier perf`` -- MCP tool latency profiling with drift detection.

Bare ``atelier perf`` runs the profile and prints per-tool drift vs the last
recorded run (without recording). ``atelier perf append`` also records the run
into the history file. ``atelier perf show`` prints the recorded history.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from atelier.gateway.cli.commands._mcp_profile import (
    append_history,
    default_history_path,
    load_last_run,
    render_drift,
    run_profile,
    summarize_history,
)


def _profile_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    fn = click.option(
        "--repo",
        "repo",
        type=click.Path(file_okay=False, path_type=Path),
        default=Path("."),
        help="Repo to profile (default: cwd).",
    )(fn)
    fn = click.option("--runs", "runs", default=7, show_default=True, help="Warm samples per tool (median reported).")(
        fn
    )
    fn = click.option("--warmup", "warmup", default=2, show_default=True, help="Warm-up calls before sampling.")(fn)
    fn = click.option(
        "--threshold", "threshold", default=25.0, show_default=True, help="Drift %% that flags a regression."
    )(fn)
    fn = click.option(
        "--min-abs-ms",
        "min_abs_ms",
        default=10.0,
        show_default=True,
        help="Min absolute ms drift to flag (below this is jitter, not a regression).",
    )(fn)
    fn = click.option("--no-edit", "no_edit", is_flag=True, help="Skip the (mutating) edit probe.")(fn)
    fn = click.option("--json", "as_json", is_flag=True, help="Emit the run record as JSON instead of the table.")(fn)
    fn = click.option(
        "--history",
        "history",
        type=click.Path(path_type=Path),
        default=None,
        help="History JSONL path (default: <repo>/reports/perf/mcp_latency_history.jsonl). Point elsewhere to keep experiments out of the tracked file.",
    )(fn)
    return fn


def _profile_and_render(
    *,
    repo: Path,
    runs: int,
    warmup: int,
    threshold: float,
    min_abs_ms: float,
    no_edit: bool,
    as_json: bool,
    store: bool,
    history: Path | None = None,
    force: bool = False,
) -> bool:
    repo = repo.resolve()
    history = history.resolve() if history is not None else default_history_path(repo)
    current = run_profile(repo, warmup=warmup, runs=runs, include_edit=not no_edit)
    prev = load_last_run(history, str(repo))
    text, regressed = render_drift(current, prev, threshold, min_abs_ms=min_abs_ms)
    if as_json:
        click.echo(json.dumps(current, indent=2, sort_keys=True))
    else:
        click.echo(text)
    if store:
        # Guard the baseline: a regressed run is NOT recorded by default, so a bad
        # measurement can never silently become the reference the next run compares
        # against. Re-run to rule out noise, or pass --force to record deliberately.
        if regressed and not force:
            if not as_json:
                click.echo(
                    "\n⚠ NOT recorded: this run regressed vs the baseline, so the baseline is kept. "
                    "Re-run to rule out noise, or pass --force to record it anyway."
                )
        else:
            append_history(history, current)
            if not as_json:
                click.echo(f"\nappended to {history}")
    return regressed


@click.group("perf", invoke_without_command=True)
@click.pass_context
def perf_group(ctx: click.Context) -> None:
    """Profile MCP tool latency and track drift across runs."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(perf_run)


@perf_group.command("run")
@_profile_options
@click.option(
    "--fail-on-regression",
    "fail_on_regression",
    is_flag=True,
    help="Exit non-zero if any tool regresses past --threshold.",
)
def perf_run(
    repo: Path,
    runs: int,
    warmup: int,
    threshold: float,
    min_abs_ms: float,
    no_edit: bool,
    as_json: bool,
    history: Path | None,
    fail_on_regression: bool,
) -> None:
    """Profile and print drift vs the last recorded run (does NOT record)."""
    regressed = _profile_and_render(
        repo=repo,
        runs=runs,
        warmup=warmup,
        threshold=threshold,
        min_abs_ms=min_abs_ms,
        no_edit=no_edit,
        as_json=as_json,
        store=False,
        history=history,
    )
    if fail_on_regression and regressed:
        raise SystemExit(1)


@perf_group.command("append")
@_profile_options
@click.option(
    "--force", "force", is_flag=True, help="Record even if the run regressed (advance the baseline deliberately)."
)
def perf_append(
    repo: Path,
    runs: int,
    warmup: int,
    threshold: float,
    min_abs_ms: float,
    no_edit: bool,
    as_json: bool,
    history: Path | None,
    force: bool,
) -> None:
    """Profile, print drift, and record this run as the new baseline.

    A regressed run is NOT recorded unless --force is given, so a bad measurement
    cannot quietly become the reference for the next comparison.
    """
    _profile_and_render(
        repo=repo,
        runs=runs,
        warmup=warmup,
        threshold=threshold,
        min_abs_ms=min_abs_ms,
        no_edit=no_edit,
        as_json=as_json,
        store=True,
        history=history,
        force=force,
    )


@perf_group.command("show")
@click.option(
    "--repo", "repo", type=click.Path(file_okay=False, path_type=Path), default=Path("."), help="Repo (default: cwd)."
)
@click.option("--history", "history", type=click.Path(path_type=Path), default=None, help="History JSONL path.")
@click.option("--last", "last", default=10, show_default=True, help="How many recent runs to show.")
def perf_show(repo: Path, history: Path | None, last: int) -> None:
    """Print the recorded latency history (warm_ms per tool across runs)."""
    repo = repo.resolve()
    hist = history.resolve() if history is not None else default_history_path(repo)
    click.echo(summarize_history(hist, str(repo), last))
