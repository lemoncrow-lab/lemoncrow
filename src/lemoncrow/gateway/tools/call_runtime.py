"""Transport-neutral orchestration for one prepared LemonCrow tool call."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lemoncrow.gateway.tools.errors import ToolArgumentError, ToolProtocolError
from lemoncrow.gateway.tools.execution import execute_prepared_tool_call
from lemoncrow.gateway.tools.invocation import ToolCallPreparationError, prepare_tool_call
from lemoncrow.gateway.tools.lifecycle import ToolCallLifecycle, ToolLifecycleHooks
from lemoncrow.gateway.tools.state import tool_call_images
from lemoncrow.gateway.tools.workspace import clear_request_project, extract_request_project, set_request_project


@dataclass(frozen=True, slots=True)
class ToolCallRuntimeHooks:
    broker_spec: dict[str, Any]
    remote_tools: frozenset[str]
    dispatch_remote: Callable[[str, dict[str, Any]], Any]
    get_ledger: Callable[[], Any]
    prepare_model_recommendation: Callable[..., Any]
    finalize_model_recommendation: Callable[..., Any]
    lifecycle_hooks: Callable[[], ToolLifecycleHooks]
    record_session_cwd: Callable[[str, dict[str, Any]], None]
    is_deferred_result: Callable[[Any], bool]


@dataclass(slots=True)
class ToolCallRun:
    lifecycle: ToolCallLifecycle
    result: Any = None
    error: Exception | None = None
    deferred: bool = False


def run_tool_call(rid: Any, params: Any, *, hooks: ToolCallRuntimeHooks) -> ToolCallRun:
    """Prepare and execute one tool call without choosing transport framing.

    Request-shape/project binding faults raise :class:`ToolProtocolError` before
    a lifecycle exists, matching the historical dispatcher behavior. Handler
    failures are returned with the lifecycle so each host can apply the same
    payload/error finalization policy. Deferred results are identified by an
    injected predicate so this generic runtime never imports the MCP deferral
    implementation.
    """
    tool_call_images.value = []
    try:
        prepared = prepare_tool_call(
            params,
            broker_spec=hooks.broker_spec,
            remote_tools=hooks.remote_tools,
        )
    except ToolCallPreparationError as exc:
        raise ToolProtocolError(exc.code, str(exc)) from exc

    name = prepared.name
    args = prepared.args
    try:
        prior_project = set_request_project(extract_request_project(params, args))
    except ToolArgumentError as exc:
        raise ToolProtocolError(-32602, str(exc)) from exc

    hooks.record_session_cwd(name, args)
    lifecycle = ToolCallLifecycle(
        rid=rid,
        name=name,
        args=args,
        remote_routed=prepared.remote_routed,
        call_started=time.perf_counter(),
        hooks=hooks.lifecycle_hooks(),
    )
    try:
        try:
            outcome = execute_prepared_tool_call(
                prepared,
                dispatch_remote=hooks.dispatch_remote,
                get_ledger=hooks.get_ledger,
                prepare_model_recommendation=hooks.prepare_model_recommendation,
                finalize_model_recommendation=hooks.finalize_model_recommendation,
            )
        except Exception as exc:
            return ToolCallRun(lifecycle=lifecycle, error=exc)

        lifecycle.duration_ms = outcome.duration_ms
        lifecycle.ledger = outcome.ledger
        return ToolCallRun(
            lifecycle=lifecycle,
            result=outcome.result,
            deferred=hooks.is_deferred_result(outcome.result),
        )
    finally:
        clear_request_project(prior_project)


def finalize_tool_payload(run: ToolCallRun) -> dict[str, Any]:
    """Finalize one synchronous run into host-neutral tool content."""
    if run.error is not None:
        return run.lifecycle.finalize_error_payload(run.error)
    if run.deferred:
        return run.lifecycle.finalize_error_payload(
            RuntimeError("deferred tool result is unsupported by synchronous payload execution")
        )
    return run.lifecycle.finalize_payload(run.result)


_DefaultHooksFactory = Callable[[], ToolCallRuntimeHooks]
_default_hooks_factory: _DefaultHooksFactory | None = None


def configure_default_tool_runtime(factory: _DefaultHooksFactory) -> None:
    """Install the process default hook factory used by non-MCP host surfaces.

    The factory is intentionally late-bound: the legacy composition root can
    preserve runtime monkey-patching while consumers depend only on this module.
    """
    global _default_hooks_factory
    _default_hooks_factory = factory


def default_tool_runtime_configured() -> bool:
    return _default_hooks_factory is not None


def execute_default_tool_payload(rid: Any, params: Any) -> dict[str, Any]:
    """Execute and finalize one synchronous tool call via the configured runtime."""
    factory = _default_hooks_factory
    if factory is None:
        raise RuntimeError("default tool runtime is not configured")
    return finalize_tool_payload(run_tool_call(rid, params, hooks=factory()))


__all__ = [
    "ToolCallRun",
    "ToolCallRuntimeHooks",
    "configure_default_tool_runtime",
    "default_tool_runtime_configured",
    "execute_default_tool_payload",
    "finalize_tool_payload",
    "run_tool_call",
]
