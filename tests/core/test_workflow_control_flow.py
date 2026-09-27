"""G19 control-flow workflow tests: map, conditional, loop, infinite-loop guard.

All tests use a stub executor so no real agents/tools/shells are spawned.
"""

from __future__ import annotations

from typing import Any

import pytest

from lemoncrow.core.capabilities.workflow_context import WorkflowContextState
from lemoncrow.core.capabilities.workflow_runner import (
    WorkflowLoopGuardError,
    WorkflowRunner,
    evaluate_predicate,
)
from lemoncrow.core.capabilities.workflow_schema import (
    WorkflowDefinition,
    WorkflowPredicate,
    WorkflowStepDefinition,
    validate_workflow_definition,
    workflow_definition_from_mapping,
)


def _record(calls: list[Any]):
    """Build deterministic stub executors that echo their inputs."""

    def tool_executor(step: WorkflowStepDefinition, args: dict[str, Any], _: WorkflowContextState) -> dict[str, Any]:
        calls.append((step.step_id, args))
        return {"output": f"tool::{step.step_id}::{args}", "output_json": dict(args)}

    def agent_executor(step: WorkflowStepDefinition, prompt: str, _: WorkflowContextState) -> dict[str, Any]:
        calls.append((step.step_id, prompt))
        return {"output": f"agent::{prompt}", "output_json": {"prompt": prompt}}

    def shell_executor(step: WorkflowStepDefinition, command: str, _: dict[str, Any]) -> dict[str, Any]:
        calls.append((step.step_id, command))
        return {"output": f"shell::{command}"}

    return agent_executor, tool_executor, shell_executor


def _runner(calls: list[Any]) -> WorkflowRunner:
    agent_executor, tool_executor, shell_executor = _record(calls)
    return WorkflowRunner(
        agent_executor=agent_executor,
        tool_executor=tool_executor,
        shell_executor=shell_executor,
    )


# --------------------------------------------------------------------------
# Back-compat: a plain DAG still parses and runs unchanged.
# --------------------------------------------------------------------------
def test_back_compat_dag_runs_unchanged() -> None:
    definition = workflow_definition_from_mapping(
        {
            "workflow_id": "legacy-dag",
            "steps": [
                {"step_id": "read", "kind": "tool", "tool": "read", "args": {"path": "a.md"}},
                {
                    "step_id": "review",
                    "kind": "agent",
                    "prompt": "{{steps.read.output}}",
                },
            ],
        }
    )
    calls: list[Any] = []
    result = _runner(calls).run(validate_workflow_definition(definition))
    assert result.status == "success"
    assert result.step_order == ["read", "review"]
    # legacy steps carry control-flow defaults that never engage.
    assert definition.steps[0].body == ()
    assert definition.steps[0].predicate is None


# --------------------------------------------------------------------------
# map-over-collection
# --------------------------------------------------------------------------
def test_map_runs_body_per_item_with_item_binding() -> None:
    definition = workflow_definition_from_mapping(
        {
            "workflow_id": "map-flow",
            "steps": [
                {
                    "step_id": "seed",
                    "kind": "tool",
                    "tool": "read",
                    "args": {"items": ["alpha", "beta", "gamma"]},
                },
                {
                    "step_id": "fan_out",
                    "kind": "map",
                    "over": "{{steps.seed.output_json.items}}",
                    "item_var": "name",
                    "body": [
                        {
                            "step_id": "handle",
                            "kind": "agent",
                            "prompt": "{{name}}",
                        }
                    ],
                },
            ],
        }
    )
    calls: list[Any] = []
    state = WorkflowContextState()
    result = _runner(calls).run(validate_workflow_definition(definition), context_state=state)

    assert result.status == "success"
    map_result = state.step_results["fan_out"]
    assert map_result.status == "done"
    assert map_result.output == ["agent::alpha", "agent::beta", "agent::gamma"]
    assert map_result.output_json["count"] == 3
    # The body agent ran once per item with the per-item binding substituted.
    agent_prompts = [prompt for sid, prompt in calls if sid == "handle"]
    assert agent_prompts == ["alpha", "beta", "gamma"]


def test_map_over_literal_list() -> None:
    definition = WorkflowDefinition(
        workflow_id="map-literal",
        steps=(
            WorkflowStepDefinition(
                step_id="loop_items",
                kind="map",
                over=[1, 2],
                item_var="n",
                body=(WorkflowStepDefinition(step_id="echo", kind="tool", tool="read", args={"n": "{{n}}"}),),
            ),
        ),
    )
    calls: list[Any] = []
    state = WorkflowContextState()
    result = _runner(calls).run(validate_workflow_definition(definition), context_state=state)
    assert result.status == "success"
    assert [args for sid, args in calls if sid == "echo"] == [{"n": 1}, {"n": 2}]


# --------------------------------------------------------------------------
# conditional
# --------------------------------------------------------------------------
def _conditional_definition() -> dict[str, Any]:
    return {
        "workflow_id": "cond-flow",
        "steps": [
            {
                "step_id": "decide",
                "kind": "agent",
                "prompt": "decide",
            },
            {
                "step_id": "branch",
                "kind": "conditional",
                "predicate": {"op": "eq", "ref": "{{steps.decide.output_json.prompt}}", "value": "decide"},
                "body": [{"step_id": "apply", "kind": "shell", "command": "echo then"}],
                "else_body": [{"step_id": "skip", "kind": "shell", "command": "echo else"}],
            },
        ],
    }


def test_conditional_runs_then_branch_when_predicate_holds() -> None:
    definition = workflow_definition_from_mapping(_conditional_definition())
    calls: list[Any] = []
    state = WorkflowContextState()
    result = _runner(calls).run(validate_workflow_definition(definition), context_state=state)

    assert result.status == "success"
    branch = state.step_results["branch"]
    assert branch.output_json["branch"] == "then"
    assert branch.output == "shell::echo then"
    assert [sid for sid, _ in calls] == ["decide", "apply"]


def test_conditional_runs_else_branch_when_predicate_fails() -> None:
    raw = _conditional_definition()
    raw["steps"][1]["predicate"]["value"] = "never"
    definition = workflow_definition_from_mapping(raw)
    calls: list[Any] = []
    state = WorkflowContextState()
    result = _runner(calls).run(validate_workflow_definition(definition), context_state=state)

    assert result.status == "success"
    branch = state.step_results["branch"]
    assert branch.output_json["branch"] == "else"
    assert [sid for sid, _ in calls] == ["decide", "skip"]


# --------------------------------------------------------------------------
# loop (bounded) + infinite-loop guard
# --------------------------------------------------------------------------
def test_loop_repeats_until_predicate_holds() -> None:
    # The stub tool echoes args; we make the body emit a counter that the
    # until-predicate watches. Because each iteration runs in an isolated child
    # scope, we drive convergence with a mutable closure counter.
    counter = {"n": 0}

    def tool_executor(step: WorkflowStepDefinition, args: dict[str, Any], _: WorkflowContextState) -> dict[str, Any]:
        counter["n"] += 1
        return {"output": counter["n"], "output_json": {"n": counter["n"]}}

    def agent_executor(step: WorkflowStepDefinition, prompt: str, _: WorkflowContextState) -> dict[str, Any]:
        return {"output": prompt}

    def shell_executor(step: WorkflowStepDefinition, command: str, _: dict[str, Any]) -> dict[str, Any]:
        return {"output": command}

    runner = WorkflowRunner(
        agent_executor=agent_executor,
        tool_executor=tool_executor,
        shell_executor=shell_executor,
    )
    definition = WorkflowDefinition(
        workflow_id="loop-flow",
        steps=(
            WorkflowStepDefinition(
                step_id="poll_loop",
                kind="loop",
                max_iterations=10,
                predicate=WorkflowPredicate(op="gte", ref="{{steps.tick.output_json.n}}", value=3),
                body=(WorkflowStepDefinition(step_id="tick", kind="tool", tool="read", args={}),),
            ),
        ),
    )
    state = WorkflowContextState()
    result = runner.run(validate_workflow_definition(definition), context_state=state)
    assert result.status == "success"
    loop_result = state.step_results["poll_loop"]
    assert loop_result.output_json["converged"] is True
    assert loop_result.output_json["completed"] == 3


def test_loop_infinite_guard_trips_when_never_converges() -> None:
    definition = WorkflowDefinition(
        workflow_id="runaway-loop",
        steps=(
            WorkflowStepDefinition(
                step_id="never",
                kind="loop",
                max_iterations=5,
                # until-condition can never hold: literal False truthiness.
                predicate=WorkflowPredicate(op="truthy", ref="{{steps.body_step.output_json.done}}"),
                body=(
                    WorkflowStepDefinition(
                        step_id="body_step",
                        kind="tool",
                        tool="read",
                        args={"done": False},
                    ),
                ),
            ),
        ),
    )
    calls: list[Any] = []
    with pytest.raises(WorkflowLoopGuardError, match="exceeded max_iterations=5"):
        _runner(calls).run(validate_workflow_definition(definition))
    # Guard fired only after exhausting the bounded budget.
    assert len([sid for sid, _ in calls if sid == "body_step"]) == 5


def test_loop_max_iterations_clamped_to_hard_cap() -> None:
    from lemoncrow.core.capabilities.workflow_schema import LOOP_ITERATION_HARD_CAP

    definition = WorkflowDefinition(
        workflow_id="over-cap",
        steps=(
            WorkflowStepDefinition(
                step_id="capped",
                kind="loop",
                max_iterations=LOOP_ITERATION_HARD_CAP + 10_000,
                predicate=WorkflowPredicate(op="truthy", ref="{{steps.never.output_json.done}}"),
                body=(WorkflowStepDefinition(step_id="never", kind="tool", tool="read", args={"done": False}),),
            ),
        ),
    )
    calls: list[Any] = []
    with pytest.raises(WorkflowLoopGuardError, match=f"max_iterations={LOOP_ITERATION_HARD_CAP}"):
        _runner(calls).run(validate_workflow_definition(definition))


# --------------------------------------------------------------------------
# predicate evaluator + validation guards
# --------------------------------------------------------------------------
def test_evaluate_predicate_ops() -> None:
    state = WorkflowContextState()
    assert evaluate_predicate(WorkflowPredicate(op="eq", ref="x", value="x"), state) is True
    assert evaluate_predicate(WorkflowPredicate(op="ne", ref="x", value="y"), state) is True
    assert evaluate_predicate(WorkflowPredicate(op="truthy", ref="non-empty"), state) is True
    assert evaluate_predicate(WorkflowPredicate(op="falsy", ref=""), state) is True
    assert evaluate_predicate(WorkflowPredicate(op="contains", ref="hello", value="ell"), state) is True
    assert evaluate_predicate(WorkflowPredicate(op="in", ref=2, value=[1, 2, 3]), state) is True
    assert evaluate_predicate(WorkflowPredicate(op="gt", ref=5, value=3), state) is True
    assert evaluate_predicate(WorkflowPredicate(op="lte", ref=3, value=3), state) is True


def test_validation_rejects_loop_without_positive_max_iterations() -> None:
    definition = WorkflowDefinition(
        workflow_id="bad-loop",
        steps=(
            WorkflowStepDefinition(
                step_id="loop",
                kind="loop",
                max_iterations=0,
                predicate=WorkflowPredicate(op="truthy", ref="{{steps.x.output}}"),
                body=(WorkflowStepDefinition(step_id="x", kind="tool", tool="read"),),
            ),
        ),
    )
    with pytest.raises(ValueError, match="max_iterations > 0"):
        validate_workflow_definition(definition)


def test_validation_rejects_map_without_over() -> None:
    definition = WorkflowDefinition(
        workflow_id="bad-map",
        steps=(
            WorkflowStepDefinition(
                step_id="m",
                kind="map",
                body=(WorkflowStepDefinition(step_id="b", kind="tool", tool="read"),),
            ),
        ),
    )
    with pytest.raises(ValueError, match="map step requires `over`"):
        validate_workflow_definition(definition)


def test_validation_rejects_empty_body() -> None:
    definition = WorkflowDefinition(
        workflow_id="empty-body",
        steps=(
            WorkflowStepDefinition(
                step_id="m",
                kind="map",
                over=[1],
                body=(),
            ),
        ),
    )
    with pytest.raises(ValueError, match="non-empty body"):
        validate_workflow_definition(definition)


def test_validation_rejects_unknown_predicate_op() -> None:
    definition = WorkflowDefinition(
        workflow_id="bad-op",
        steps=(
            WorkflowStepDefinition(step_id="prior", kind="tool", tool="read"),
            WorkflowStepDefinition(
                step_id="c",
                kind="conditional",
                predicate=WorkflowPredicate(op="regex", ref="{{steps.prior.output}}"),
                body=(WorkflowStepDefinition(step_id="x", kind="tool", tool="read"),),
            ),
        ),
    )
    with pytest.raises(ValueError, match="unsupported predicate op"):
        validate_workflow_definition(definition)
