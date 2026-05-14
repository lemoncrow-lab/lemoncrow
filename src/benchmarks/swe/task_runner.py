"""Run one (task x mode x attempt) and emit a :class:`RunMetrics` row."""

from __future__ import annotations

import time
from pathlib import Path

from benchmarks.swe.agent_runner import Agent, AgentResult, summarize_workflow
from benchmarks.swe.config import BenchConfig
from benchmarks.swe.datasets import Task
from benchmarks.swe.metrics import RunMetrics
from benchmarks.swe.modes import Mode, get_spec
from benchmarks.swe.patch_export import write_patch_file


def run_one(
    *,
    task: Task,
    mode: Mode,
    attempt: int,
    cfg: BenchConfig,
    agent: Agent,
    out_dir: Path,
) -> RunMetrics:
    spec = get_spec(mode)

    payload = task.to_agent_payload()
    # Hard guard: never let a gold patch leak into the agent payload.
    for k in ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS"):
        assert k not in payload, f"safety: gold field '{k}' leaked into agent payload"

    t0 = time.monotonic()
    err: str | None = None
    try:
        result: AgentResult = agent.solve(task, spec, cfg)
    except NotImplementedError as e:
        err = str(e)
        result = AgentResult(patch="", error=err)
    elapsed = time.monotonic() - t0

    patch_path: Path | None = None
    if result.patch:
        patch_path = write_patch_file(
            result.patch, task.instance_id, mode.value, out_dir / "patches"
        )

    workflow = summarize_workflow(result.workflow_events)
    metrics = RunMetrics(
        task_id=task.instance_id,
        mode=mode.value,
        attempt=attempt,
        resolved=_resolved_check(task, result),
        patch_generated=bool(result.patch),
        patch_path=str(patch_path) if patch_path else None,
        tokens_input=result.tokens_input,
        tokens_output=result.tokens_output,
        estimated_cost_usd=result.estimated_cost_usd,
        wall_time_seconds=round(elapsed, 4),
        turns=result.turns,
        tool_calls=result.tool_calls,
        file_reads=result.file_reads,
        searches=result.searches,
        repeated_commands=result.repeated_commands,
        monitor_events=workflow["monitor_events"],
        compression_events=workflow["compression_events"],
        reasonblocks_retrieved=workflow["reasonblocks_retrieved"],
        reasonblock_hits=workflow["reasonblock_hits"],
        check_plan_statuses=list(workflow["check_plan_statuses"]),
        rubric_status=workflow["rubric_status"],
        rescue_count=workflow["rescue_count"],
        trace_id=workflow["trace_id"],
        budget_cap_hit=result.budget_cap_hit,
        error=result.error or err,
    )
    return metrics


def _resolved_check(task: Task, result: AgentResult) -> bool:
    """Lightweight resolved-flag for offline mock runs.

    Real evaluation comes from :mod:`benchmarks.swe.swebench_eval`. This
    flag exists so reports remain useful without the heavy harness.
    """
    if not result.patch:
        return False
    # For the mock dataset, we declare resolved if the patch contains the
    # known fix marker. This matches the gold patch only for the first task.
    if task.instance_id == "mock__add-1":
        return "return a+b" in result.patch
    return False
