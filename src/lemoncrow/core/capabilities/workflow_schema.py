from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

MAX_WORKFLOW_STEPS = 256
# Leaf (executor-backed) step kinds and control-flow step kinds. Leaf kinds run
# through the agent/tool/shell executors; control-flow kinds (G19) orchestrate
# nested sub-steps deterministically without an executor of their own.
LEAF_STEP_KINDS = frozenset({"agent", "tool", "shell"})
CONTROL_FLOW_STEP_KINDS = frozenset({"map", "conditional", "loop"})
SUPPORTED_STEP_KINDS = LEAF_STEP_KINDS | CONTROL_FLOW_STEP_KINDS
SUPPORTED_CONTEXT_MODES = frozenset({"inherit", "fresh"})
# Hard ceiling so a mis-specified `loop` (or a never-satisfied `until`) can never
# spin forever; per-step `max_iterations` is clamped to this absolute cap.
LOOP_ITERATION_HARD_CAP = 1000
# Hard ceiling on `map` fan-out: `over` is rendered from prior step output, so a
# step emitting a huge list could otherwise spawn an unbounded number of
# sub-bodies. A map whose resolved item count exceeds this cap is rejected.
MAP_ITEM_HARD_CAP = 1000
SUPPORTED_PREDICATE_OPS = frozenset(
    {"eq", "ne", "truthy", "falsy", "contains", "not_contains", "gt", "gte", "lt", "lte", "in"}
)
SAFE_PARALLEL_TOOL_NAMES = frozenset(
    {
        "read",
        "grep",
        "search",
        "symbols",
        "node",
        "explore",
    }
)
_STEP_REF_PATTERN = re.compile(r"\{\{\s*steps\.([A-Za-z0-9_\-]+)\.(?:output|output_json(?:\.[A-Za-z0-9_\-]+)*)\s*\}\}")


@dataclass(frozen=True)
class WorkflowPredicate:
    """A deterministic, sandbox-free condition over prior step results.

    No expression eval: a predicate compares one resolved value (a ``ref``
    template like ``{{steps.x.output_json.decision}}`` or a literal ``value``)
    against a comparison ``value`` using a fixed, allow-listed ``op``. Unary ops
    (``truthy``/``falsy``) ignore ``value``.
    """

    op: str
    ref: str = ""
    value: Any = None


@dataclass(frozen=True)
class WorkflowStepDefinition:
    step_id: str
    kind: str
    role_id: str = ""
    next_steps: tuple[str, ...] = ()
    fork_from: str = ""
    context_mode: str = "inherit"
    parallel_safe: bool = False
    requires_plan_review: bool = False
    prompt: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    command: str = ""
    output_name: str = ""
    json_output: bool = False
    interactive: bool = False
    # --- G19 control-flow fields (ignored by leaf kinds) ---
    # `map`: iterate `over` (a list literal or a step-ref resolving to a list),
    # running `body` once per item; `item_var` names the per-item binding.
    over: Any = None
    item_var: str = "item"
    # `conditional`: run `body` when `predicate` holds, else `else_body`.
    # `loop`: repeat `body` until `predicate` holds or `max_iterations` is hit.
    predicate: WorkflowPredicate | None = None
    max_iterations: int = 0
    body: tuple[WorkflowStepDefinition, ...] = ()
    else_body: tuple[WorkflowStepDefinition, ...] = ()


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_id: str
    steps: tuple[WorkflowStepDefinition, ...] = ()


def workflow_predicate_from_mapping(raw: Mapping[str, Any]) -> WorkflowPredicate:
    return WorkflowPredicate(
        op=str(raw.get("op") or "").strip(),
        ref=str(raw.get("ref") or "").strip(),
        value=raw.get("value"),
    )


def _sub_steps_from_raw(value: Any) -> tuple[WorkflowStepDefinition, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(workflow_step_from_mapping(item) for item in value if isinstance(item, Mapping))


def workflow_step_from_mapping(raw: Mapping[str, Any]) -> WorkflowStepDefinition:
    next_steps_raw = raw.get("next_steps") or ()
    next_steps = (
        tuple(str(step_id) for step_id in next_steps_raw if str(step_id).strip())
        if isinstance(next_steps_raw, list | tuple)
        else ()
    )
    raw_args = raw.get("args")
    args: dict[str, Any] = dict(raw_args) if isinstance(raw_args, dict) else {}
    raw_predicate = raw.get("predicate")
    predicate = workflow_predicate_from_mapping(raw_predicate) if isinstance(raw_predicate, Mapping) else None
    raw_max_iterations = raw.get("max_iterations")
    max_iterations = int(raw_max_iterations) if isinstance(raw_max_iterations, int | float) else 0
    return WorkflowStepDefinition(
        step_id=str(raw.get("step_id") or raw.get("id") or "").strip(),
        kind=str(raw.get("kind") or "").strip(),
        role_id=str(raw.get("role_id") or "").strip(),
        next_steps=next_steps,
        fork_from=str(raw.get("fork_from") or "").strip(),
        context_mode=str(raw.get("context_mode") or "inherit").strip() or "inherit",
        parallel_safe=bool(raw.get("parallel_safe", False)),
        requires_plan_review=bool(raw.get("requires_plan_review", False)),
        prompt=str(raw.get("prompt") or "").strip(),
        tool=str(raw.get("tool") or "").strip(),
        args=dict(args),
        command=str(raw.get("command") or "").strip(),
        output_name=str(raw.get("output_name") or "").strip(),
        json_output=bool(raw.get("json_output", False)),
        interactive=bool(raw.get("interactive", False)),
        over=raw.get("over"),
        item_var=str(raw.get("item_var") or "item").strip() or "item",
        predicate=predicate,
        max_iterations=max_iterations,
        body=_sub_steps_from_raw(raw.get("body")),
        else_body=_sub_steps_from_raw(raw.get("else_body")),
    )


def workflow_definition_from_mapping(raw: Mapping[str, Any]) -> WorkflowDefinition:
    steps_raw = raw.get("steps") or ()
    steps = tuple(workflow_step_from_mapping(step) for step in steps_raw if isinstance(step, Mapping))
    return WorkflowDefinition(
        workflow_id=str(raw.get("workflow_id") or raw.get("id") or "").strip(),
        steps=steps,
    )


def referenced_step_ids(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        refs.update(match.group(1) for match in _STEP_REF_PATTERN.finditer(value))
        return refs
    if isinstance(value, Mapping):
        for nested in value.values():
            refs.update(referenced_step_ids(nested))
        return refs
    if isinstance(value, list | tuple):
        for nested in value:
            refs.update(referenced_step_ids(nested))
    return refs


def step_is_safe_parallel(step: WorkflowStepDefinition) -> bool:
    if step.kind == "tool":
        if step.interactive:
            return False
        if step.tool == "context":
            return str(step.args.get("mode") or "").strip() == "symbols"
        return step.tool in SAFE_PARALLEL_TOOL_NAMES
    if step.kind == "agent":
        return step.parallel_safe and not step.requires_plan_review
    return False


def _control_flow_refs(step: WorkflowStepDefinition) -> set[str]:
    """Outer-scope step ids a control-flow step depends on.

    Always includes `over` (map iterates a prior result). A `conditional`
    predicate is evaluated *before* its body, so its refs are outer deps too. A
    `loop` predicate is the until-condition evaluated *after* each body
    iteration, so it references body-internal ids and is NOT an outer dep.
    """
    refs: set[str] = set()
    refs.update(referenced_step_ids(step.over))
    if step.kind == "conditional" and step.predicate is not None:
        refs.update(referenced_step_ids(step.predicate.ref))
        refs.update(referenced_step_ids(step.predicate.value))
    return refs


def step_dependencies(definition: WorkflowDefinition) -> dict[str, set[str]]:
    deps: dict[str, set[str]] = {step.step_id: set() for step in definition.steps}
    for step in definition.steps:
        for next_step in step.next_steps:
            deps.setdefault(next_step, set()).add(step.step_id)
        if step.fork_from:
            deps[step.step_id].add(step.fork_from)
        refs = set()
        refs.update(referenced_step_ids(step.prompt))
        refs.update(referenced_step_ids(step.args))
        refs.update(referenced_step_ids(step.command))
        refs.update(_control_flow_refs(step))
        deps[step.step_id].update(refs)
    return deps


def _validate_predicate(predicate: WorkflowPredicate, *, step_id: str, known_ids: set[str]) -> None:
    if predicate.op not in SUPPORTED_PREDICATE_OPS:
        raise ValueError(f"unsupported predicate op: {predicate.op}")
    if predicate.op in {"truthy", "falsy"} and not predicate.ref:
        raise ValueError(f"predicate {predicate.op} requires ref: {step_id}")
    refs = referenced_step_ids(predicate.ref) | referenced_step_ids(predicate.value)
    unknown = sorted(ref for ref in refs if ref not in known_ids)
    if unknown:
        raise ValueError(f"unknown step reference: {', '.join(unknown)}")


def _validate_sub_steps(
    sub_steps: tuple[WorkflowStepDefinition, ...],
    *,
    owner_step_id: str,
    outer_known_ids: set[str],
) -> None:
    """Validate a nested control-flow body as its own scoped DAG.

    Sub-steps may reference outer-scope ids plus their siblings; the map/loop
    item binding is injected by the caller via ``outer_known_ids``.
    """
    if not sub_steps:
        raise ValueError(f"control-flow step requires a non-empty body: {owner_step_id}")
    sub_ids = {sub.step_id for sub in sub_steps}
    if "" in sub_ids:
        raise ValueError(f"workflow step requires step_id (in body of {owner_step_id})")
    if len(sub_ids) != len(sub_steps):
        raise ValueError(f"duplicate step id in body of {owner_step_id}")
    known = outer_known_ids | sub_ids
    for sub in sub_steps:
        _validate_step(sub, known_ids=known)


def _validate_step(step: WorkflowStepDefinition, *, known_ids: set[str]) -> None:
    if step.kind not in SUPPORTED_STEP_KINDS:
        raise ValueError(f"unsupported step kind: {step.kind}")
    if step.context_mode not in SUPPORTED_CONTEXT_MODES:
        raise ValueError(f"unsupported context mode: {step.context_mode}")
    if step.kind == "agent" and not step.prompt:
        raise ValueError(f"agent step requires prompt: {step.step_id}")
    if step.kind == "tool" and not step.tool:
        raise ValueError(f"tool step requires tool: {step.step_id}")
    if step.kind == "shell" and not step.command:
        raise ValueError(f"shell step requires command: {step.step_id}")
    for next_step in step.next_steps:
        if next_step not in known_ids:
            raise ValueError(f"unknown next step: {next_step}")
        if next_step == step.step_id:
            raise ValueError(f"step cannot point to itself: {step.step_id}")
    if step.fork_from:
        if step.fork_from not in known_ids:
            raise ValueError(f"unknown fork source: {step.fork_from}")
        if step.fork_from == step.step_id:
            raise ValueError(f"step cannot fork from itself: {step.step_id}")
    refs = referenced_step_ids(step.prompt) | referenced_step_ids(step.args) | referenced_step_ids(step.command)
    refs |= _control_flow_refs(step)
    unknown_refs = sorted(ref for ref in refs if ref not in known_ids)
    if unknown_refs:
        raise ValueError(f"unknown step reference: {', '.join(unknown_refs)}")
    if step.step_id in refs:
        raise ValueError(f"step cannot reference itself: {step.step_id}")

    if step.kind not in CONTROL_FLOW_STEP_KINDS:
        return

    # Control-flow steps: the body runs in a child scope that also sees a per-item
    # binding (`item_var`) for `map`. The item binding is referenced as a step id.
    body_known = set(known_ids)
    if step.kind == "map":
        if step.over is None:
            raise ValueError(f"map step requires `over`: {step.step_id}")
        if not step.item_var:
            raise ValueError(f"map step requires item_var: {step.step_id}")
        body_known.add(step.item_var)
    if step.kind == "conditional":
        if step.predicate is None:
            raise ValueError(f"conditional step requires predicate: {step.step_id}")
        # Evaluated before the body runs, so it may only reference outer steps.
        _validate_predicate(step.predicate, step_id=step.step_id, known_ids=known_ids)
    if step.kind == "loop":
        if step.predicate is None:
            raise ValueError(f"loop step requires a predicate (until condition): {step.step_id}")
        if step.max_iterations <= 0:
            raise ValueError(f"loop step requires max_iterations > 0: {step.step_id}")
        # The until-condition is evaluated after each body iteration, so it may
        # reference outer steps OR the loop body's own step ids.
        loop_pred_scope = body_known | {sub.step_id for sub in step.body}
        _validate_predicate(step.predicate, step_id=step.step_id, known_ids=loop_pred_scope)
    _validate_sub_steps(step.body, owner_step_id=step.step_id, outer_known_ids=body_known)
    if step.kind == "conditional" and step.else_body:
        _validate_sub_steps(step.else_body, owner_step_id=step.step_id, outer_known_ids=body_known)


def validate_workflow_definition(definition: WorkflowDefinition) -> WorkflowDefinition:
    if not definition.workflow_id:
        raise ValueError("workflow definition requires workflow_id")
    if not definition.steps:
        raise ValueError("workflow definition requires at least one step")
    if len(definition.steps) > MAX_WORKFLOW_STEPS:
        raise ValueError(
            f"workflow definition exceeds maximum step count: {len(definition.steps)} > {MAX_WORKFLOW_STEPS}"
        )

    seen_ids: set[str] = set()
    step_ids = {step.step_id for step in definition.steps}
    if "" in step_ids:
        raise ValueError("workflow step requires step_id")

    for step in definition.steps:
        if step.step_id in seen_ids:
            raise ValueError(f"duplicate step id: {step.step_id}")
        seen_ids.add(step.step_id)
        _validate_step(step, known_ids=step_ids)

    deps = step_dependencies(definition)
    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(step_id: str) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            raise ValueError(f"workflow contains a cycle at step: {step_id}")
        visiting.add(step_id)
        for dep in deps.get(step_id, ()):
            _visit(dep)
        visiting.remove(step_id)
        visited.add(step_id)

    for step in definition.steps:
        _visit(step.step_id)
    return definition


__all__ = [
    "SAFE_PARALLEL_TOOL_NAMES",
    "SUPPORTED_CONTEXT_MODES",
    "SUPPORTED_STEP_KINDS",
    "WorkflowDefinition",
    "WorkflowStepDefinition",
    "referenced_step_ids",
    "step_dependencies",
    "step_is_safe_parallel",
    "validate_workflow_definition",
    "workflow_definition_from_mapping",
    "workflow_step_from_mapping",
]
