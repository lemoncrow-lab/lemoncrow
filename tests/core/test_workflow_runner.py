from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.capabilities.workflow_context import StepResult, WorkflowContextState
from lemoncrow.core.capabilities.workflow_runner import WorkflowRunner, build_execution_waves
from lemoncrow.core.capabilities.workflow_schema import (
    WorkflowDefinition,
    WorkflowStepDefinition,
    validate_workflow_definition,
)
from lemoncrow.infra.runtime.run_ledger import RunLedger


def _owned_workflow_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="owned-review-loop",
        steps=(
            WorkflowStepDefinition(
                step_id="read_spec",
                kind="tool",
                tool="read",
                args={"path": "docs/spec.md"},
            ),
            WorkflowStepDefinition(
                step_id="symbols",
                kind="tool",
                tool="symbols",
                args={"query": "WorkflowRunner"},
            ),
            WorkflowStepDefinition(
                step_id="review",
                kind="agent",
                prompt="{{steps.read_spec.output}}",
                next_steps=("apply_fix",),
            ),
            WorkflowStepDefinition(
                step_id="apply_fix",
                kind="shell",
                command="echo apply",
                fork_from="review",
            ),
        ),
    )


def test_validate_workflow_definition_rejects_invalid_step_kind() -> None:
    definition = WorkflowDefinition(
        workflow_id="bad-kind",
        steps=(WorkflowStepDefinition(step_id="oops", kind="write", tool="read"),),
    )

    with pytest.raises(ValueError, match="unsupported step kind"):
        validate_workflow_definition(definition)


def test_validate_workflow_definition_rejects_invalid_context_mode() -> None:
    definition = WorkflowDefinition(
        workflow_id="bad-context-mode",
        steps=(
            WorkflowStepDefinition(
                step_id="research",
                kind="agent",
                context_mode="shared",
                prompt="Research independently.",
            ),
        ),
    )

    with pytest.raises(ValueError, match="unsupported context mode"):
        validate_workflow_definition(definition)


def test_workflow_step_mapping_supports_fresh_context_mode() -> None:
    from lemoncrow.core.capabilities.workflow_schema import workflow_step_from_mapping

    step = workflow_step_from_mapping(
        {
            "step_id": "research",
            "kind": "agent",
            "context_mode": "fresh",
            "prompt": "Research independently.",
        }
    )

    assert step.context_mode == "fresh"


def test_build_execution_waves_uses_template_and_parallel_tool_dependencies() -> None:
    validated = validate_workflow_definition(_owned_workflow_definition())

    waves = build_execution_waves(validated)

    assert waves == [("read_spec", "symbols"), ("review",), ("apply_fix",)]


def test_workflow_context_fork_is_copy_on_write() -> None:
    state = WorkflowContextState()
    state.record_step_result(
        StepResult(
            step_id="review",
            kind="agent",
            status="done",
            output="plan ready",
            output_json={"summary": "ready", "notes": ["a"]},
            duration_seconds=1.2,
        )
    )

    forked = state.fork_step_context("review")
    forked["output_json"]["summary"] = "changed"
    forked["output_json"]["notes"].append("b")

    original = state.step_results["review"]
    assert original.output_json == {"summary": "ready", "notes": ["a"]}


def test_workflow_runner_executes_steps_and_records_telemetry(tmp_path: Path) -> None:
    validated = validate_workflow_definition(_owned_workflow_definition())
    ledger = RunLedger(root=tmp_path / ".lemoncrow")
    state = WorkflowContextState()
    calls: list[tuple[str, Any]] = []

    def tool_executor(step: WorkflowStepDefinition, args: dict[str, Any], _: WorkflowContextState) -> dict[str, Any]:
        calls.append((step.step_id, args))
        if step.step_id == "read_spec":
            return {"output": "SPEC CONTENT"}
        return {"output": "SYMBOLS CONTENT", "output_json": {"count": 2}}

    def agent_executor(step: WorkflowStepDefinition, prompt: str, _: WorkflowContextState) -> dict[str, Any]:
        calls.append((step.step_id, prompt))
        return {"output": f"REVIEWED::{prompt}", "output_json": {"decision": "approve"}}

    def shell_executor(step: WorkflowStepDefinition, command: str, forked: dict[str, Any]) -> dict[str, Any]:
        calls.append((step.step_id, {"command": command, "forked": forked}))
        return {"output": "shell ok"}

    runner = WorkflowRunner(
        agent_executor=agent_executor,
        tool_executor=tool_executor,
        shell_executor=shell_executor,
    )

    result = runner.run(validated, context_state=state, ledger=ledger)

    assert result.status == "success"
    assert result.step_order == ["read_spec", "symbols", "review", "apply_fix"]
    assert state.step_results["review"].output == "REVIEWED::SPEC CONTENT"
    assert state.step_results["apply_fix"].output == "shell ok"
    assert calls[2] == ("review", "SPEC CONTENT")
    assert calls[3][0] == "apply_fix"
    assert calls[3][1]["forked"]["output"] == "REVIEWED::SPEC CONTENT"
    snapshot = ledger.snapshot()
    assert snapshot["workflow_step_events"] == [
        {"step_id": "read_spec", "event": "start", "kind": "tool", "status": "running"},
        {"step_id": "read_spec", "event": "done", "kind": "tool", "status": "done"},
        {"step_id": "symbols", "event": "start", "kind": "tool", "status": "running"},
        {"step_id": "symbols", "event": "done", "kind": "tool", "status": "done"},
        {"step_id": "review", "event": "start", "kind": "agent", "status": "running"},
        {"step_id": "review", "event": "done", "kind": "agent", "status": "done"},
        {"step_id": "apply_fix", "event": "start", "kind": "shell", "status": "running"},
        {"step_id": "apply_fix", "event": "done", "kind": "shell", "status": "done"},
    ]


def test_workflow_runner_stops_on_failed_step_and_keeps_downstream_unpublished(
    tmp_path: Path,
) -> None:
    definition = WorkflowDefinition(
        workflow_id="failure-path",
        steps=(
            WorkflowStepDefinition(step_id="read_spec", kind="tool", tool="read", args={"path": "docs/spec.md"}),
            WorkflowStepDefinition(step_id="review", kind="agent", prompt="{{steps.read_spec.output}}"),
            WorkflowStepDefinition(step_id="apply_fix", kind="shell", command="echo apply", next_steps=("verify",)),
            WorkflowStepDefinition(step_id="verify", kind="tool", tool="search", args={"query": "done"}),
        ),
    )
    validated = validate_workflow_definition(definition)
    ledger = RunLedger(root=tmp_path / ".lemoncrow")
    state = WorkflowContextState()

    runner = WorkflowRunner(
        agent_executor=lambda step, prompt, context: {"output": f"review::{prompt}"},
        tool_executor=lambda step, args, context: (
            {"output": "SPEC"} if step.step_id == "read_spec" else {"output": "VERIFY"}
        ),
        shell_executor=lambda step, command, forked: (_ for _ in ()).throw(RuntimeError("shell failed")),
    )

    result = runner.run(validated, context_state=state, ledger=ledger)

    assert result.status == "failed"
    assert result.failed_step_id == "apply_fix"
    assert "verify" not in state.step_results
    snapshot = ledger.snapshot()
    assert snapshot["workflow_step_events"][-1] == {
        "step_id": "apply_fix",
        "event": "fail",
        "kind": "shell",
        "status": "failed",
    }


def test_workflow_runner_pauses_before_review_gated_execution_and_resumes(tmp_path: Path) -> None:
    definition = WorkflowDefinition(
        workflow_id="review-gated",
        steps=(
            WorkflowStepDefinition(step_id="plan", kind="agent", prompt="Draft the implementation plan."),
            WorkflowStepDefinition(
                step_id="execute",
                kind="shell",
                command="echo apply",
                fork_from="plan",
                requires_plan_review=True,
            ),
        ),
    )
    validated = validate_workflow_definition(definition)
    ledger = RunLedger(root=tmp_path / ".lemoncrow")
    state = WorkflowContextState()
    calls: list[tuple[str, Any]] = []

    runner = WorkflowRunner(
        agent_executor=lambda step, prompt, context: calls.append((step.step_id, prompt)) or {"output": "plan ready"},
        tool_executor=lambda step, args, context: {"output": "unused"},
        shell_executor=lambda step, command, forked: calls.append((step.step_id, command)) or {"output": "applied"},
    )

    first = runner.run(validated, context_state=state, ledger=ledger)

    assert first.status == "awaiting_review"
    assert first.paused_step_id == "execute"
    assert list(state.step_results) == ["plan"]
    assert calls == [("plan", "Draft the implementation plan.")]

    second = runner.run(
        validated,
        context_state=state,
        ledger=ledger,
        plan_review_decision="approve",
    )

    assert second.status == "success"
    assert second.step_order == ["plan", "execute"]
    assert state.step_results["execute"].output == "applied"
    assert calls == [("plan", "Draft the implementation plan."), ("execute", "echo apply")]


def test_parallel_safe_agent_steps_share_spawn_group_but_fresh_context_gets_new_scope(tmp_path: Path) -> None:
    definition = WorkflowDefinition(
        workflow_id="parallel-agents",
        steps=(
            WorkflowStepDefinition(
                step_id="plan_a",
                kind="agent",
                role_id="plan",
                parallel_safe=True,
                prompt="Draft plan A.",
            ),
            WorkflowStepDefinition(
                step_id="plan_b",
                kind="agent",
                role_id="plan",
                parallel_safe=True,
                context_mode="fresh",
                prompt="Draft plan B.",
            ),
        ),
    )
    state = WorkflowContextState()
    seen: dict[str, dict[str, Any]] = {}

    def agent_executor(step: WorkflowStepDefinition, prompt: str, context: WorkflowContextState) -> dict[str, Any]:
        seen[step.step_id] = context.spawn_plan_for_step(step.step_id)
        return {"output": prompt}

    runner = WorkflowRunner(
        agent_executor=agent_executor,
        tool_executor=lambda step, args, context: {"output": "unused"},
        shell_executor=lambda step, command, forked: {"output": "unused"},
    )

    result = runner.run(definition, context_state=state)

    assert result.status == "success"
    assert seen["plan_a"]["spawn_group_id"] == seen["plan_b"]["spawn_group_id"]
    assert seen["plan_a"]["cache_scope_id"] != seen["plan_b"]["cache_scope_id"]
    assert seen["plan_a"]["cache_policy"] == "inherit"
    assert seen["plan_b"]["cache_policy"] == "fresh"
