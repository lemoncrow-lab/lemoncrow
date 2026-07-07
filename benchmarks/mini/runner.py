"""Execute mini eval cases and aggregate them into a :class:`MiniEvalReport`.

The runner has two modes:

* **dry-run** — validate and plan only. No git mutation, no subprocess, and no
  API calls. Every case result is ``skipped``.
* **live** — reset git, drive the agent through :class:`InteractiveRuntime`,
  run the deterministic verify command, check the file boundary, then restore
  git state.

``InteractiveRuntime`` is imported lazily inside the live path so dry-run works
with zero API keys and zero network access.
"""

from __future__ import annotations

import fnmatch
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from atelier.core.capabilities.savings_summary import estimate_cost_usd

from .schema import MiniEvalCaseResult, MiniEvalReport

if TYPE_CHECKING:
    from .schema import MiniEvalCase


@dataclass
class _AgentRun:
    """Typed agent-execution outcome captured from runtime events."""

    trace_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model: str | None = None
    selected_route: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Git / shell helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _git_head(cwd: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd).strip()


def _git_stash(cwd: Path) -> bool:
    """Stash tracked + untracked changes. Returns True if a stash was created."""
    out = _git(["stash", "push", "-u", "-m", "atelier-mini-eval"], cwd)
    return "No local changes to save" not in out


def _changed_files(cwd: Path) -> list[str]:
    """Return repo-relative paths of working-tree changes via ``git status``."""
    out = _git(["status", "--porcelain"], cwd)
    files: list[str] = []
    for raw in out.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip().strip('"'))
    return files


def _boundary_respected(changed: list[str], allowed: list[str]) -> bool:
    """True when every changed file matches at least one allowed glob.

    When ``allowed`` is empty, the boundary is respected only if nothing
    changed.
    """
    if not changed:
        return True
    if not allowed:
        return False
    return all(any(fnmatch.fnmatch(path, pattern) for pattern in allowed) for path in changed)


def _run_verify(command: str, cwd: Path) -> bool:
    proc = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True, text=True)
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Agent execution (live mode only)
# ---------------------------------------------------------------------------


def _run_agent(case: MiniEvalCase, *, root: Path, git_repo: Path) -> _AgentRun:
    """Drive the agent through one prompt. Lazy-imports InteractiveRuntime."""
    import asyncio

    from atelier.gateway.cli.events import (
        ContextUsageUpdated,
        RouteSelected,
        RuntimeErrorEvent,
    )
    from atelier.gateway.cli.runtime import InteractiveRuntime

    session_id = f"mini-{case.id}-{uuid.uuid4().hex[:8]}"
    runtime = InteractiveRuntime(root=root, yolo=True)
    run = _AgentRun(trace_id=session_id)

    async def _go() -> None:
        await runtime.start_session(str(git_repo), session_id=session_id)
        try:
            async for event in runtime.handle_user_message(session_id, case.prompt):
                if isinstance(event, ContextUsageUpdated):
                    run.input_tokens = event.input_tokens
                    run.output_tokens = event.output_tokens
                    run.cache_read_tokens = event.cache_read_tokens
                    run.cache_write_tokens = event.cache_write_tokens
                elif isinstance(event, RouteSelected):
                    run.model = event.model
                    run.selected_route = event.reason or event.provider or event.model
                elif isinstance(event, RuntimeErrorEvent):
                    run.error = event.message
        finally:
            runtime.shutdown()

    asyncio.run(_go())
    return run


# ---------------------------------------------------------------------------
# Single-case execution
# ---------------------------------------------------------------------------


def run_case_dry(case: MiniEvalCase) -> MiniEvalCaseResult:
    """Dry-run a case: validate only, return a skipped result, no side effects."""
    return MiniEvalCaseResult(
        id=case.id,
        title=case.title,
        status="skipped",
        file_boundary_respected=True,
        notes="dry_run: case validated, not executed (no git, no subprocess, no API)",
    )


def run_case(case: MiniEvalCase, *, root: Path, git_repo: Path) -> MiniEvalCaseResult:
    """Execute one mini eval case live and return its result.

    Resets git to ``case.starting_git_sha`` (unless it is ``HEAD``), runs the
    agent (except zero-cost cases which are verify-only), verifies, checks the
    file boundary, then restores git state.

    A case with ``max_cost_usd == 0.0`` skips the agent entirely — the prompt
    is user-visible metadata and the verify command must pass without any LLM
    call. This matches the use case of CLI smoke tests and schema assertions
    that need no mutation.
    """
    started_head = _git_head(git_repo)
    stashed = _git_stash(git_repo)
    did_reset = False
    try:
        target = case.starting_git_sha
        if target and target != "HEAD" and target != started_head:
            _git(["reset", "--hard", target], git_repo)
            did_reset = True

        # Zero-cost cases are verify-only: no agent, no LLM call, no litellm.
        if case.max_cost_usd == 0.0:
            changed = _changed_files(git_repo)
            boundary_ok = _boundary_respected(changed, case.allowed_files)
            tests_passed = _run_verify(case.command_to_verify, git_repo)
            accepted = tests_passed and boundary_ok
            return MiniEvalCaseResult(
                id=case.id,
                title=case.title,
                status="accepted" if accepted else "failed",
                patch_created=bool(changed),
                tests_passed=tests_passed,
                accepted=accepted,
                file_boundary_respected=boundary_ok,
                notes="" if accepted else ("file boundary violated" if not boundary_ok else "verify command failed"),
            )

        run = _run_agent(case, root=root, git_repo=git_repo)

        cost = estimate_cost_usd(
            model_id=run.model or "",
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cache_read_tokens=run.cache_read_tokens,
            cache_write_tokens=run.cache_write_tokens,
        )
        cache_tokens = run.cache_read_tokens + run.cache_write_tokens

        if run.error:
            return MiniEvalCaseResult(
                id=case.id,
                title=case.title,
                status="error",
                trace_id=run.trace_id,
                selected_route=run.selected_route,
                model=run.model,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                cache_tokens=cache_tokens,
                estimated_cost_usd=round(cost, 6),
                notes=f"agent error: {run.error}",
            )

        changed = _changed_files(git_repo)
        patch_created = bool(changed)
        boundary_ok = _boundary_respected(changed, case.allowed_files)
        tests_passed = _run_verify(case.command_to_verify, git_repo)
        accepted = tests_passed and boundary_ok
        regression = patch_created and not accepted

        notes = ""
        if not boundary_ok:
            notes = f"file boundary violated: changed={changed} allowed={case.allowed_files}"
        elif not tests_passed:
            notes = "verify command failed (exit != 0)"

        return MiniEvalCaseResult(
            id=case.id,
            title=case.title,
            status="accepted" if accepted else "failed",
            trace_id=run.trace_id,
            selected_route=run.selected_route,
            model=run.model,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cache_tokens=cache_tokens,
            estimated_cost_usd=round(cost, 6),
            patch_created=patch_created,
            tests_passed=tests_passed,
            accepted=accepted,
            regression=regression,
            file_boundary_respected=boundary_ok,
            notes=notes,
        )
    finally:
        if did_reset:
            _git(["reset", "--hard", started_head], git_repo)
        else:
            _git(["checkout", "--", "."], git_repo)
        # Remove agent-created untracked files before restoring the stash so they
        # are not left as cruft and cannot collide with the user's stashed
        # untracked files (which would make ``stash pop`` abort and strand it).
        _git(["clean", "-fd"], git_repo)
        if stashed:
            _git(["stash", "pop"], git_repo)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_report(
    results: list[MiniEvalCaseResult],
    *,
    dry_run: bool,
    started_at: str,
    finished_at: str,
    suite: str = "mini",
    context_reduction_pct: float | None = None,
) -> MiniEvalReport:
    """Compute mini eval metrics from per-case results.

    Failed cases still count toward ``total_cost_usd`` — savings can never hide
    a failure. Status is derived from accepted patches and verification, never
    from token reduction.
    """
    total_tasks = len(results)
    non_skipped = [r for r in results if r.status != "skipped"]
    accepted_tasks = sum(1 for r in results if r.accepted)
    failed_tasks = sum(1 for r in results if r.status in ("failed", "error"))
    total_cost_usd = sum(max(0.0, r.estimated_cost_usd) for r in results)
    regressions = sum(1 for r in results if r.regression)

    accepted_patch_rate = accepted_tasks / total_tasks if total_tasks else 0.0
    cost_per_accepted_patch = total_cost_usd / accepted_tasks if accepted_tasks else total_cost_usd
    cheap_success_rate = accepted_tasks / total_tasks if total_tasks else 0.0
    routing_regression_rate = 0.0 if dry_run else (regressions / total_tasks if total_tasks else 0.0)

    trace_covered = sum(1 for r in non_skipped if r.trace_id)
    trace_coverage_pct = (trace_covered / len(non_skipped) * 100.0) if non_skipped else 0.0

    if dry_run:
        status: str = "dry_run"
    elif non_skipped and failed_tasks == 0 and accepted_tasks == len(non_skipped):
        status = "pass"
    else:
        status = "fail"

    return MiniEvalReport(
        suite=suite,
        status=status,  # type: ignore[arg-type]
        started_at=started_at,
        finished_at=finished_at,
        total_tasks=total_tasks,
        accepted_tasks=accepted_tasks,
        failed_tasks=failed_tasks,
        accepted_patch_rate=round(accepted_patch_rate, 6),
        total_cost_usd=round(total_cost_usd, 6),
        cost_per_accepted_patch=round(cost_per_accepted_patch, 6),
        cheap_success_rate=round(cheap_success_rate, 6),
        routing_regression_rate=round(routing_regression_rate, 6),
        context_reduction_pct=context_reduction_pct,
        trace_coverage_pct=round(trace_coverage_pct, 4),
        cases=results,
    )


def run_suite(
    cases: list[MiniEvalCase],
    *,
    root: Path,
    git_repo: Path,
    dry_run: bool = False,
    limit: int = 5,
) -> MiniEvalReport:
    """Run cases (up to ``limit``) and return an aggregated report."""
    started_at = datetime.now(UTC).isoformat()
    selected = cases[:limit] if limit and limit > 0 else list(cases)
    results: list[MiniEvalCaseResult] = []

    if dry_run:
        # dry_run: all cases skipped — no git, no subprocess, no API calls.
        for case in selected:
            results.append(run_case_dry(case))
    else:
        for case in selected:
            try:
                results.append(run_case(case, root=root, git_repo=git_repo))
            except Exception as exc:
                results.append(
                    MiniEvalCaseResult(
                        id=case.id,
                        title=case.title,
                        status="error",
                        notes=f"runner error: {exc}",
                    )
                )

    finished_at = datetime.now(UTC).isoformat()
    return aggregate_report(results, dry_run=dry_run, started_at=started_at, finished_at=finished_at)


__all__ = [
    "aggregate_report",
    "run_case",
    "run_case_dry",
    "run_suite",
]
