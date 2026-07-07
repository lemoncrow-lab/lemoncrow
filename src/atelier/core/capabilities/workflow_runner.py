from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from atelier.core.capabilities.workflow_context import StepResult, WorkflowContextState
from atelier.core.capabilities.workflow_schema import (
    CONTROL_FLOW_STEP_KINDS,
    LOOP_ITERATION_HARD_CAP,
    MAP_ITEM_HARD_CAP,
    WorkflowDefinition,
    WorkflowPredicate,
    WorkflowStepDefinition,
    referenced_step_ids,
    step_dependencies,
    step_is_safe_parallel,
    validate_workflow_definition,
)
from atelier.core.capabilities.workflow_spawn import new_wave_spawn_plan
from atelier.infra.runtime.run_ledger import RunLedger

logger = logging.getLogger(__name__)

# Bound owned-execution fan-out: never spawn more than this many concurrent
# subprocesses per wave, regardless of wave width.
MAX_PARALLEL = 8
# Wall-clock cap for a single step so one hung spawn cannot wedge the whole run.
STEP_TIMEOUT_SECONDS = 600.0

AgentExecutor = Callable[[WorkflowStepDefinition, str, WorkflowContextState], Any]
ToolExecutor = Callable[[WorkflowStepDefinition, dict[str, Any], WorkflowContextState], Any]
ShellExecutor = Callable[[WorkflowStepDefinition, str, dict[str, Any]], Any]


@dataclass(frozen=True)
class WorkflowRunResult:
    run_id: str
    status: str
    step_order: list[str]
    step_results: dict[str, StepResult]
    failed_step_id: str | None = None
    paused_step_id: str | None = None


class WorkflowLoopGuardError(RuntimeError):
    """Raised when a `loop` step exceeds its (capped) max-iterations budget."""


class WorkflowMapGuardError(RuntimeError):
    """Raised when a `map` step's resolved item count exceeds its hard cap."""


def _resolve_operand(value: Any, state: WorkflowContextState) -> Any:
    """Resolve a predicate operand: a full step/item reference or a literal."""
    if isinstance(value, str) and referenced_step_ids(value):
        return state.render_value(value)
    if isinstance(value, str):
        # May still be a `{{item...}}` reference inside an active map scope.
        try:
            return state.render_value(value)
        except ValueError:
            return value
    return value


def evaluate_predicate(predicate: WorkflowPredicate, state: WorkflowContextState) -> bool:
    """Deterministically evaluate a predicate against the current context.

    No code eval: a fixed allow-list of comparison ops over a resolved left
    operand (`ref`) and, for binary ops, a resolved right operand (`value`).
    """
    left = _resolve_operand(predicate.ref, state) if predicate.ref else None
    op = predicate.op
    if op == "truthy":
        return bool(left)
    if op == "falsy":
        return not bool(left)
    right = _resolve_operand(predicate.value, state)
    if op == "eq":
        return bool(left == right)
    if op == "ne":
        return bool(left != right)
    if op == "contains":
        return _safe_contains(left, right)
    if op == "not_contains":
        return not _safe_contains(left, right)
    if op == "in":
        return _safe_contains(right, left)
    if op in {"gt", "gte", "lt", "lte"}:
        return _safe_compare(op, left, right)
    raise ValueError(f"unsupported predicate op: {op}")


def _safe_contains(container: Any, member: Any) -> bool:
    if isinstance(container, str):
        return isinstance(member, str) and member in container
    if isinstance(container, list | tuple | set | frozenset | dict):
        try:
            return member in container
        except TypeError:
            return False
    return False


def _safe_compare(op: str, left: Any, right: Any) -> bool:
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        raise ValueError(f"predicate {op} requires numeric operands")
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    return left <= right


def build_execution_waves(definition: WorkflowDefinition) -> list[tuple[str, ...]]:
    validated = validate_workflow_definition(definition)
    order = [step.step_id for step in validated.steps]
    by_id = {step.step_id: step for step in validated.steps}
    deps = {step_id: set(values) for step_id, values in step_dependencies(validated).items()}
    completed: set[str] = set()
    waves: list[tuple[str, ...]] = []

    while len(completed) < len(order):
        ready = [step_id for step_id in order if step_id not in completed and deps[step_id].issubset(completed)]
        if not ready:
            raise ValueError("workflow contains unresolved dependencies")
        first = by_id[ready[0]]
        if step_is_safe_parallel(first):
            wave = tuple(step_id for step_id in ready if step_is_safe_parallel(by_id[step_id]))
        else:
            wave = (ready[0],)
        completed.update(wave)
        waves.append(wave)
    return waves


class WorkflowRunner:
    def __init__(
        self,
        *,
        agent_executor: AgentExecutor,
        tool_executor: ToolExecutor,
        shell_executor: ShellExecutor,
    ) -> None:
        self._agent_executor = agent_executor
        self._tool_executor = tool_executor
        self._shell_executor = shell_executor

    def _step_hash_payload(self, step: WorkflowStepDefinition) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "step_id": step.step_id,
            "kind": step.kind,
            "role_id": step.role_id,
            "next_steps": list(step.next_steps),
            "fork_from": step.fork_from,
            "context_mode": step.context_mode,
            "parallel_safe": step.parallel_safe,
            "requires_plan_review": step.requires_plan_review,
            "prompt": step.prompt,
            "tool": step.tool,
            "args": step.args,
            "command": step.command,
            "output_name": step.output_name,
            "json_output": step.json_output,
            "interactive": step.interactive,
        }
        if step.kind in CONTROL_FLOW_STEP_KINDS:
            payload["over"] = step.over
            payload["item_var"] = step.item_var
            payload["max_iterations"] = step.max_iterations
            payload["predicate"] = (
                {"op": step.predicate.op, "ref": step.predicate.ref, "value": step.predicate.value}
                if step.predicate is not None
                else None
            )
            payload["body"] = [self._step_hash_payload(sub) for sub in step.body]
            payload["else_body"] = [self._step_hash_payload(sub) for sub in step.else_body]
        return payload

    def _definition_hash(self, definition: WorkflowDefinition) -> str:
        payload = {
            "workflow_id": definition.workflow_id,
            "steps": [self._step_hash_payload(step) for step in definition.steps],
        }
        return sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _normalize_executor_result(
        self, step: WorkflowStepDefinition, raw: Any, *, duration_seconds: float
    ) -> StepResult:
        if isinstance(raw, StepResult):
            return StepResult(
                step_id=step.step_id,
                kind=step.kind,
                status=raw.status,
                output=raw.output,
                output_json=raw.output_json,
                execution_receipt=raw.execution_receipt,
                duration_seconds=raw.duration_seconds or duration_seconds,
                cost_usd=raw.cost_usd,
                error=raw.error,
            )
        if isinstance(raw, dict):
            raw_output_json = raw.get("output_json")
            output_json: dict[str, Any] = dict(raw_output_json) if isinstance(raw_output_json, dict) else dict(raw)
            raw_execution_receipt = raw.get("execution_receipt")
            execution_receipt = dict(raw_execution_receipt) if isinstance(raw_execution_receipt, dict) else {}
            if "output" in raw:
                output = raw.get("output")
            elif isinstance(raw.get("content"), str):
                output = raw.get("content")
            else:
                output = json.dumps(raw, sort_keys=True)
            return StepResult(
                step_id=step.step_id,
                kind=step.kind,
                status=str(raw.get("status") or "done"),
                output=output,
                output_json=output_json,
                execution_receipt=execution_receipt,
                duration_seconds=float(raw.get("duration_seconds") or duration_seconds),
                cost_usd=float(raw.get("cost_usd") or 0.0),
                error=str(raw.get("error") or ""),
            )
        return StepResult(
            step_id=step.step_id,
            kind=step.kind,
            status="done",
            output=raw,
            output_json={},
            duration_seconds=duration_seconds,
        )

    def _run_step(
        self,
        step: WorkflowStepDefinition,
        context_state: WorkflowContextState,
        ledger: RunLedger | None,
    ) -> StepResult:
        start = time.perf_counter()
        if ledger is not None:
            ledger.record_workflow_step_event(step_id=step.step_id, event="start", kind=step.kind, status="running")
        try:
            if step.kind in CONTROL_FLOW_STEP_KINDS:
                result = self._run_control_flow(step, context_state, ledger, start)
            elif step.kind == "agent":
                rendered_prompt = context_state.render_value(step.prompt)
                raw_result = self._agent_executor(step, str(rendered_prompt), context_state)
                result = self._normalize_executor_result(step, raw_result, duration_seconds=time.perf_counter() - start)
            elif step.kind == "tool":
                rendered_args = context_state.render_value(step.args)
                raw_result = self._tool_executor(step, dict(rendered_args), context_state)
                result = self._normalize_executor_result(step, raw_result, duration_seconds=time.perf_counter() - start)
            elif step.kind == "shell":
                rendered_command = context_state.render_value(step.command)
                forked = context_state.fork_step_context(step.fork_from) if step.fork_from else {}
                raw_result = self._shell_executor(step, str(rendered_command), forked)
                result = self._normalize_executor_result(step, raw_result, duration_seconds=time.perf_counter() - start)
            else:
                raise ValueError(f"unsupported step kind: {step.kind}")
        except (WorkflowLoopGuardError, WorkflowMapGuardError):
            raise
        except (RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
            result = StepResult(
                step_id=step.step_id,
                kind=step.kind,
                status="failed",
                output="",
                output_json={},
                duration_seconds=time.perf_counter() - start,
                error=str(exc),
            )
        if ledger is not None:
            event = "done" if result.status == "done" else "fail"
            ledger.record_workflow_step_event(step_id=step.step_id, event=event, kind=step.kind, status=result.status)
        return result

    # ------------------------------------------------------------------
    # G19 control flow: map / conditional / loop
    # ------------------------------------------------------------------
    def _child_state(
        self, parent: WorkflowContextState, item_bindings: dict[str, Any] | None = None
    ) -> WorkflowContextState:
        """A scoped context that inherits parent results but isolates sub-results.

        Outer-scope references resolve because parent step results are seeded in;
        sub-step results land only in the child, so repeated map/loop iterations
        never collide on step ids and the parent DAG stays clean.
        """
        child = WorkflowContextState(run_id=parent.run_id, definition_hash=parent.definition_hash)
        child.step_results = dict(parent.step_results)
        child.status = "running"
        if item_bindings:
            child.set_item_bindings(item_bindings)
        return child

    def _ordered_sub_steps(self, sub_steps: tuple[WorkflowStepDefinition, ...]) -> list[WorkflowStepDefinition]:
        """Topologically order sub-steps by intra-body dependencies (siblings only).

        Outer-scope and item references are already satisfied in the child state,
        so only sibling next_steps/refs constrain ordering.
        """
        by_id = {sub.step_id: sub for sub in sub_steps}
        deps: dict[str, set[str]] = {sub.step_id: set() for sub in sub_steps}
        for sub in sub_steps:
            refs = set()
            refs.update(referenced_step_ids(sub.prompt))
            refs.update(referenced_step_ids(sub.args))
            refs.update(referenced_step_ids(sub.command))
            refs.update(referenced_step_ids(sub.over))
            if sub.predicate is not None:
                refs.update(referenced_step_ids(sub.predicate.ref))
                refs.update(referenced_step_ids(sub.predicate.value))
            if sub.fork_from in by_id:
                refs.add(sub.fork_from)
            deps[sub.step_id].update(ref for ref in refs if ref in by_id)
            for nxt in sub.next_steps:
                if nxt in by_id:
                    deps[nxt].add(sub.step_id)
        ordered: list[WorkflowStepDefinition] = []
        done: set[str] = set()
        order_ids = [sub.step_id for sub in sub_steps]
        while len(done) < len(order_ids):
            ready = [sid for sid in order_ids if sid not in done and deps[sid].issubset(done)]
            if not ready:
                raise ValueError("control-flow body contains unresolved dependencies")
            for sid in ready:
                ordered.append(by_id[sid])
                done.add(sid)
        return ordered

    def _run_sub_body(
        self,
        sub_steps: tuple[WorkflowStepDefinition, ...],
        child: WorkflowContextState,
        ledger: RunLedger | None,
    ) -> tuple[bool, dict[str, StepResult]]:
        """Run a body sequentially. Returns (ok, results-by-id).

        Stops at the first failing sub-step (ok=False), mirroring top-level fail
        propagation. Sub-steps may themselves be control-flow steps (recursion).
        """
        results: dict[str, StepResult] = {}
        for sub in self._ordered_sub_steps(sub_steps):
            sub_result = self._run_step(sub, child, ledger)
            child.record_step_result(sub_result)
            results[sub.step_id] = sub_result
            if sub_result.status != "done":
                return False, results
        return True, results

    def _coerce_over_items(self, value: Any) -> list[Any]:
        if isinstance(value, list):
            return list(value)
        if isinstance(value, tuple):
            return list(value)
        raise ValueError("map step `over` must resolve to a list")

    def _run_control_flow(
        self,
        step: WorkflowStepDefinition,
        context_state: WorkflowContextState,
        ledger: RunLedger | None,
        start: float,
    ) -> StepResult:
        if step.kind == "map":
            return self._run_map(step, context_state, ledger, start)
        if step.kind == "conditional":
            return self._run_conditional(step, context_state, ledger, start)
        if step.kind == "loop":
            return self._run_loop(step, context_state, ledger, start)
        raise ValueError(f"unsupported control-flow kind: {step.kind}")

    def _run_map(
        self,
        step: WorkflowStepDefinition,
        context_state: WorkflowContextState,
        ledger: RunLedger | None,
        start: float,
    ) -> StepResult:
        items = self._coerce_over_items(context_state.render_value(step.over))
        if len(items) > MAP_ITEM_HARD_CAP:
            # `over` is rendered from prior step output: trip the fan-out guard
            # rather than spawning an unbounded number of sub-bodies.
            raise WorkflowMapGuardError(
                f"map step {step.step_id} resolved {len(items)} items, exceeding the hard cap of {MAP_ITEM_HARD_CAP}"
            )
        item_outputs: list[Any] = []
        iterations: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            child = self._child_state(context_state, {step.item_var: item, "index": index})
            ok, results = self._run_sub_body(step.body, child, ledger)
            terminal = results[self._ordered_sub_steps(step.body)[-1].step_id] if results else None
            iterations.append({rid: r.to_dict() for rid, r in results.items()})
            if not ok:
                return StepResult(
                    step_id=step.step_id,
                    kind=step.kind,
                    status="failed",
                    output="",
                    output_json={"items": item_outputs, "iterations": iterations, "failed_index": index},
                    duration_seconds=time.perf_counter() - start,
                    error=f"map iteration {index} failed",
                )
            item_outputs.append(terminal.output if terminal is not None else None)
        return StepResult(
            step_id=step.step_id,
            kind=step.kind,
            status="done",
            output=item_outputs,
            output_json={"items": item_outputs, "count": len(item_outputs), "iterations": iterations},
            duration_seconds=time.perf_counter() - start,
        )

    def _run_conditional(
        self,
        step: WorkflowStepDefinition,
        context_state: WorkflowContextState,
        ledger: RunLedger | None,
        start: float,
    ) -> StepResult:
        assert step.predicate is not None  # guaranteed by validation
        branch_taken = evaluate_predicate(step.predicate, context_state)
        body = step.body if branch_taken else step.else_body
        if not body:
            return StepResult(
                step_id=step.step_id,
                kind=step.kind,
                status="done",
                output="",
                output_json={"branch": "then" if branch_taken else "else", "skipped": True},
                duration_seconds=time.perf_counter() - start,
            )
        child = self._child_state(context_state)
        ok, results = self._run_sub_body(body, child, ledger)
        terminal = results[self._ordered_sub_steps(body)[-1].step_id] if results else None
        status = "done" if ok else "failed"
        return StepResult(
            step_id=step.step_id,
            kind=step.kind,
            status=status,
            output=terminal.output if terminal is not None else "",
            output_json={
                "branch": "then" if branch_taken else "else",
                "results": {rid: r.to_dict() for rid, r in results.items()},
            },
            duration_seconds=time.perf_counter() - start,
            error="" if ok else "conditional branch failed",
        )

    def _run_loop(
        self,
        step: WorkflowStepDefinition,
        context_state: WorkflowContextState,
        ledger: RunLedger | None,
        start: float,
    ) -> StepResult:
        assert step.predicate is not None  # guaranteed by validation
        cap = min(step.max_iterations, LOOP_ITERATION_HARD_CAP)
        iterations: list[dict[str, Any]] = []
        last_output: Any = ""
        completed = 0
        for index in range(cap):
            child = self._child_state(context_state, {"index": index})
            ok, results = self._run_sub_body(step.body, child, ledger)
            terminal = results[self._ordered_sub_steps(step.body)[-1].step_id] if results else None
            iterations.append({rid: r.to_dict() for rid, r in results.items()})
            completed = index + 1
            if not ok:
                return StepResult(
                    step_id=step.step_id,
                    kind=step.kind,
                    status="failed",
                    output="",
                    output_json={"iterations": iterations, "completed": completed, "failed_index": index},
                    duration_seconds=time.perf_counter() - start,
                    error=f"loop iteration {index} failed",
                )
            last_output = terminal.output if terminal is not None else ""
            # `predicate` is the until-condition: stop once it holds.
            if evaluate_predicate(step.predicate, child):
                return StepResult(
                    step_id=step.step_id,
                    kind=step.kind,
                    status="done",
                    output=last_output,
                    output_json={"iterations": iterations, "completed": completed, "converged": True},
                    duration_seconds=time.perf_counter() - start,
                )
        # Exhausted the (capped) iteration budget without the until-condition
        # holding: trip the infinite-loop guard rather than silently succeeding.
        raise WorkflowLoopGuardError(
            f"loop step {step.step_id} exceeded max_iterations={cap} without satisfying its until condition"
        )

    def _run_step_bounded(
        self,
        step: WorkflowStepDefinition,
        state: WorkflowContextState,
        ledger: RunLedger | None,
    ) -> StepResult:
        """Run a single step under the same wall-clock cap as the parallel path.

        A linear agent chain (and the whole body of a map/loop step, which is
        itself one step) would otherwise run with no deadline; mirror
        `_run_wave_parallel` so one hung spawn cannot wedge the run.

        The deadline is advisory: the submitted work is an opaque executor
        callable (`_agent_executor`/`_tool_executor`/`_shell_executor`) with no
        cancellation handle, so on timeout we abandon the worker thread and let
        the run proceed. The underlying spawn/subprocess may keep running until
        it finishes on its own; we emit a warning so that leak is not silent.

        Map/loop bodies run their sub-steps via `_run_sub_body`->`_run_step`
        directly (not through this method), so the whole body shares this single
        outer deadline and per-iteration work is itself unbounded.
        """
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(self._run_step, step, state, ledger)
            try:
                return future.result(timeout=STEP_TIMEOUT_SECONDS)
            except FutureTimeoutError:
                logger.warning(
                    "workflow step %s exceeded the %.0fs per-step deadline; "
                    "abandoning the worker thread — its underlying spawn may still be running",
                    step.step_id,
                    STEP_TIMEOUT_SECONDS,
                )
                return StepResult(
                    step_id=step.step_id,
                    kind=step.kind,
                    status="failed",
                    output="",
                    output_json={},
                    error="step timed out: exceeded the per-step deadline",
                )
        finally:
            # wait=False so a hung running step cannot re-wedge the run on shutdown.
            # cancel_futures only drops not-yet-started futures; an already-running
            # executor callable is non-cancellable and keeps running in a leaked
            # thread until it returns (see the timeout warning above).
            pool.shutdown(wait=False, cancel_futures=True)

    def _run_wave_parallel(
        self,
        pending_wave: tuple[str, ...],
        by_id: dict[str, WorkflowStepDefinition],
        state: WorkflowContextState,
        ledger: RunLedger | None,
    ) -> list[StepResult]:
        max_workers = min(len(pending_wave), MAX_PARALLEL)
        deadline = time.monotonic() + STEP_TIMEOUT_SECONDS
        results: dict[str, StepResult] = {}
        pool = ThreadPoolExecutor(max_workers=max_workers)
        try:
            future_to_step: dict[Future[StepResult], str] = {
                pool.submit(self._run_step, by_id[step_id], state, ledger): step_id for step_id in pending_wave
            }
            pending = set(future_to_step)
            while pending:
                timeout = deadline - time.monotonic()
                done, pending = wait(pending, timeout=max(timeout, 0.0), return_when=FIRST_COMPLETED)
                if not done:
                    # Wave-level deadline hit: a step is hung. Stop waiting and let
                    # the pending steps be marked timed-out below so the run does not wedge.
                    break
                for future in done:
                    # Collect every completed step (including failures) — a failed
                    # step does not cancel its already-running siblings; only a
                    # wave-deadline timeout does.
                    results[future_to_step[future]] = future.result()
        finally:
            # wait=False so a hung running step cannot re-wedge the run on shutdown;
            # cancel_futures drops siblings that have not started yet.
            pool.shutdown(wait=False, cancel_futures=True)
        for step_id in pending_wave:
            if step_id not in results:
                results[step_id] = StepResult(
                    step_id=step_id,
                    kind=by_id[step_id].kind,
                    status="failed",
                    output="",
                    output_json={},
                    error="step timed out: wave exceeded the per-wave deadline",
                )
        return [results[step_id] for step_id in pending_wave]

    def run(
        self,
        definition: WorkflowDefinition,
        *,
        context_state: WorkflowContextState | None = None,
        ledger: RunLedger | None = None,
        plan_review_decision: str = "",
    ) -> WorkflowRunResult:
        validated = validate_workflow_definition(definition)
        state = context_state if context_state is not None else WorkflowContextState()
        if not state.run_id:
            state.run_id = uuid.uuid4().hex
        state.status = "running"
        state.definition_hash = self._definition_hash(validated)
        waves = build_execution_waves(validated)
        by_id = {step.step_id: step for step in validated.steps}
        total_steps = len(validated.steps)
        approved = plan_review_decision.strip().lower() == "approve"
        completed_steps = sum(1 for result in state.step_results.values() if result.status == "done")

        if ledger is not None:
            ledger.record_workflow_event("workflow_state", {"workflow_step": "execution", "session_phase": "execute"})

        for wave in waves:
            pending_wave = tuple(
                step_id
                for step_id in wave
                if state.step_results.get(step_id) is None or state.step_results[step_id].status != "done"
            )
            if not pending_wave:
                continue
            self._plan_wave_spawn_context(pending_wave, by_id, state)
            gated_step = next(
                (step_id for step_id in pending_wave if by_id[step_id].requires_plan_review),
                None,
            )
            if gated_step is not None and not approved:
                state.status = "review_rejected" if plan_review_decision else "awaiting_review"
                return WorkflowRunResult(
                    run_id=state.run_id,
                    status=state.status,
                    step_order=list(state.step_order),
                    step_results=dict(state.step_results),
                    paused_step_id=gated_step,
                )
            results: list[StepResult] = []
            if len(pending_wave) > 1:
                results = self._run_wave_parallel(pending_wave, by_id, state, ledger)
            else:
                results.append(self._run_step_bounded(by_id[pending_wave[0]], state, ledger))

            results_by_id = {result.step_id: result for result in results}
            ordered_results = [results_by_id[step_id] for step_id in pending_wave]
            for result in ordered_results:
                state.record_step_result(result)
                if result.status == "done":
                    completed_steps += 1
                if ledger is not None:
                    ledger.record_workflow_event(
                        "task_progress",
                        {
                            "task_id": result.step_id,
                            "workflow_step": "execution",
                            "completed_tasks": completed_steps,
                            "remaining_tasks": total_steps - completed_steps,
                        },
                    )
                if result.status != "done":
                    state.status = "failed"
                    return WorkflowRunResult(
                        run_id=state.run_id,
                        status="failed",
                        step_order=list(state.step_order),
                        step_results=dict(state.step_results),
                        failed_step_id=result.step_id,
                    )

        state.status = "success"
        return WorkflowRunResult(
            run_id=state.run_id,
            status="success",
            step_order=list(state.step_order),
            step_results=dict(state.step_results),
        )

    def _plan_wave_spawn_context(
        self,
        pending_wave: tuple[str, ...],
        by_id: dict[str, WorkflowStepDefinition],
        state: WorkflowContextState,
    ) -> None:
        agent_steps = [step_id for step_id in pending_wave if by_id[step_id].kind == "agent"]
        if not agent_steps:
            return
        parallel = len(agent_steps) > 1
        shared_scope = new_wave_spawn_plan(cache_policy="inherit", parallel=parallel)
        for step_id in agent_steps:
            step = by_id[step_id]
            cache_policy = "fresh" if step.context_mode == "fresh" else "inherit"
            if cache_policy == "fresh":
                fresh_scope = new_wave_spawn_plan(cache_policy=cache_policy, parallel=parallel)
                plan_payload = fresh_scope.to_dict()
                plan_payload["wave_id"] = shared_scope.wave_id
                plan_payload["spawn_group_id"] = shared_scope.spawn_group_id
            else:
                plan_payload = shared_scope.to_dict()
            state.set_wave_spawn_plan(step_id, plan_payload)


__all__ = ["WorkflowRunResult", "WorkflowRunner", "build_execution_waves"]
