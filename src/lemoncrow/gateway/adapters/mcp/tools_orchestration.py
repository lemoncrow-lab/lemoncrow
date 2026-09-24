"""Agent and durable-workflow MCP handlers with late-bound orchestration hooks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast

from pydantic import Field

from lemoncrow.gateway.adapters.mcp.framework import mcp_tool
from lemoncrow.pro.capabilities.owned_execution_lanes import OwnedExecutionError
from lemoncrow.pro.capabilities.owned_execution_routing import (
    NoFeasibleRouteError,
    OwnedCachePolicy,
    OwnedRouteRequest,
)


@dataclass(frozen=True, slots=True)
class OrchestrationHandlerHooks:
    workspace_root: Callable[[], Any]
    read_workspace_session_state: Callable[[], dict[str, Any]]
    detect_agent: Callable[[], str]
    select_owned_route: Callable[..., Any]
    execute_owned_prompt: Callable[..., Any]
    state_lock: Any
    run_owned_workflow: Callable[[dict[str, Any]], dict[str, Any]]
    coerce_workflow_runtime_status: Callable[[dict[str, Any]], dict[str, Any]]
    inspect_workflow_runtime: Callable[[dict[str, Any]], dict[str, Any]]
    require_active_workflow_runtime: Callable[[dict[str, Any], str], Any]
    pause_workflow_runtime: Callable[..., Any]
    write_workspace_session_state: Callable[[dict[str, Any]], None]
    stop_workflow_runtime: Callable[..., Any]


_HooksFactory = Callable[[], OrchestrationHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_orchestration_handler_hooks(factory: _HooksFactory) -> None:
    """Install the process composition used by agent/workflow handlers."""
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> OrchestrationHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("orchestration handler hooks are not configured")
    return factory()


WORKFLOW_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["run", "status", "inspect", "pause", "resume", "stop"],
        },
        "workflow": {"type": "object"},
        "run_id": {"type": "string"},
        "route": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["native", "auto", "explicit"]},
                "provider": {"type": "string"},
                "model": {"type": "string"},
                "runner": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "plan_review": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["approve", "revise", "rerun"]},
            },
            "additionalProperties": False,
        },
        "pause_reason": {"type": "string"},
        "stop_reason": {"type": "string"},
    },
    "required": ["op"],
    "additionalProperties": False,
}


@mcp_tool(name="agent")
def tool_agent(
    prompt: Annotated[
        str,
        Field(description="Full task/instruction for the sub-agent."),
    ],
    budget: Annotated[
        str,
        Field(description="Cost/quality tier: 'cheap' | 'balanced' | 'best'. Default 'balanced'."),
    ] = "balanced",
    provider: Annotated[
        str,
        Field(description="Force a provider (e.g. 'anthropic'); empty = auto-select from configured vendors."),
    ] = "",
    model: Annotated[
        str,
        Field(description="Force a model id; empty = auto-pick by budget."),
    ] = "",
    cache_policy: Annotated[
        str,
        Field(
            description="'inherit' = share the prompt-cache scope with prior owned spawns (cheaper); 'fresh' = new scope."
        ),
    ] = "inherit",
) -> dict[str, Any]:
    """Spawn an LemonCrow-owned sub-agent, return its result.

    Runs on LemonCrow's owned-execution runtime: picks provider + model from
    credentials already configured (provider API key when present, else the
    installed host CLI), executes the prompt, shares a prompt-cache scope with
    sibling spawns when ``cache_policy='inherit'``. Prefer over the host
    ``Agent`` tool when LemonCrow should control the sub-agent's model, cost,
    and cache affinity.
    """
    hooks = _hooks()
    from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import RouteConfigError

    root = hooks.workspace_root()
    session_state = hooks.read_workspace_session_state()
    norm_cache: OwnedCachePolicy = "fresh" if str(cache_policy).strip().lower() == "fresh" else "inherit"
    use_explicit = bool(provider.strip() and model.strip())
    request = OwnedRouteRequest(
        tool_name="agent",
        task_text=prompt,
        mode="explicit" if use_explicit else "auto",
        budget=cast(Any, str(budget).strip().lower() or "balanced"),
        provider=provider.strip(),
        model=model.strip(),
        host_agent=hooks.detect_agent(),
        cache_policy=norm_cache,
        session_state=session_state,
    )
    try:
        decision = hooks.select_owned_route(root, request)
    except (NoFeasibleRouteError, RouteConfigError) as exc:
        return {
            "isError": True,
            "status": "no_route",
            "message": (
                f"No owned-execution route available: {exc}. Configure a route config (route.yaml) "
                "plus a provider API key in the environment or an installed host CLI, and enable "
                "owned routing."
            ),
        }
    try:
        result = hooks.execute_owned_prompt(
            prompt,
            root=root,
            tool_name="agent",
            task_text=prompt,
            decision=decision,
            host_agent=hooks.detect_agent(),
            session_state=session_state,
            cache_policy=norm_cache,
        )
    except OwnedExecutionError as exc:
        return {
            "isError": True,
            "status": "failed",
            "message": str(exc),
            "receipt": exc.receipt.to_dict(),
        }
    receipt = result.receipt
    return {
        "status": receipt.status,
        "output": result.output,
        "provider": receipt.executed_provider,
        "model": receipt.executed_model,
        "transport": receipt.executed_transport,
        "cost_usd": receipt.cost_usd,
        "tokens": {
            "input": receipt.input_tokens,
            "output": receipt.output_tokens,
            "cache_read": receipt.cache_read_input_tokens,
            "cache_write": receipt.cache_write_input_tokens,
        },
        "cache": {
            "evidence": receipt.cache_evidence,
            "reuse_observed": receipt.reuse_observed,
            "scope_id": receipt.cache_scope_id,
        },
    }


@mcp_tool(name="workflow", input_schema=WORKFLOW_TOOL_INPUT_SCHEMA)
def tool_workflow(
    op: str,
    workflow: dict[str, Any] | None = None,
    run_id: str | None = None,
    route: dict[str, Any] | None = None,
    plan_review: dict[str, Any] | None = None,
    pause_reason: str | None = None,
    stop_reason: str | None = None,
) -> dict[str, Any]:
    """Run or inspect LemonCrow's durable workflow runtime.

    Ops:
      run     — execute a workflow synchronously from fresh runtime state
      status  — persisted runtime state for this workspace
      inspect — spawn/cache receipts for the persisted runtime
      pause   — mark persisted runtime paused (live synchronous call not cancelled)
      resume  — continue persisted runtime with its stored workflow + route
      stop    — mark persisted runtime stopped (live synchronous call not cancelled)
    """
    hooks = _hooks()
    normalized_op = op.strip().lower()
    if normalized_op == "run":
        return hooks.run_owned_workflow(
            {"workflow": workflow or {}, "route": route or {}, "plan_review": plan_review or {}}
        )
    # Hold hooks.state_lock across the read so the pause/stop read-modify-write below
    # cannot lose a concurrent handler's session_state update. hooks.state_lock is an
    # RLock; the resume branch reacquires it reentrantly via hooks.run_owned_workflow.
    with hooks.state_lock:
        session_state = hooks.read_workspace_session_state()
        if normalized_op == "status":
            return hooks.coerce_workflow_runtime_status(session_state)
        if normalized_op == "inspect":
            return hooks.inspect_workflow_runtime(session_state)
        if normalized_op not in {"pause", "resume", "stop"}:
            return {
                "isError": True,
                "status": "unsupported_op",
                "message": f"unsupported workflow op: {op}",
            }
        hooks.require_active_workflow_runtime(session_state, run_id or "")
        if normalized_op == "resume":
            arguments: dict[str, Any] = {"resume": True, "plan_review": plan_review or {}}
            if workflow is not None:
                arguments["workflow"] = workflow
            if route is not None:
                arguments["route"] = route
            return hooks.run_owned_workflow(arguments)
        if normalized_op == "pause":
            hooks.pause_workflow_runtime(
                session_state,
                run_id=run_id or "",
                pause_reason=str(pause_reason or ""),
            )
            hooks.write_workspace_session_state(session_state)
            return hooks.coerce_workflow_runtime_status(session_state)
        if normalized_op == "stop":
            hooks.stop_workflow_runtime(
                session_state,
                run_id=run_id or "",
                stop_reason=str(stop_reason or ""),
            )
            hooks.write_workspace_session_state(session_state)
            return hooks.coerce_workflow_runtime_status(session_state)
    raise AssertionError(f"unreachable workflow op: {op!r}")  # op is guaranteed pause/resume/stop above


__all__ = [
    "WORKFLOW_TOOL_INPUT_SCHEMA",
    "OrchestrationHandlerHooks",
    "configure_orchestration_handler_hooks",
    "tool_agent",
    "tool_workflow",
]
