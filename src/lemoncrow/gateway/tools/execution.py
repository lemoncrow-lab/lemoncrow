"""Transport-independent execution of a prepared LemonCrow tool call."""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lemoncrow.core.capabilities.pricing import active_model_override
from lemoncrow.gateway.tools.invocation import PreparedToolCall
from lemoncrow.gateway.tools.results import clean_tool_result
from lemoncrow.gateway.tools.state import (
    tool_call_counterfactual,
    tool_call_rendered_text,
    tool_call_tokens_saved,
)
from lemoncrow.infra.runtime.run_ledger import RunLedger


def route_enforcement_enabled() -> bool:
    """Whether local handler execution should apply the recommended model wrapper."""
    raw = os.environ.get("LEMONCROW_ENFORCE_ROUTE_MODEL")
    if raw is None:
        return False
    return raw.strip().lower() not in {"", "0", "false", "off", "no"}


@dataclass(slots=True)
class ToolExecutionOutcome:
    """Raw execution result plus runtime metadata needed by response lifecycles."""

    result: Any
    duration_ms: int
    remote_routed: bool
    ledger: RunLedger | None = None


def execute_prepared_tool_call(
    prepared: PreparedToolCall,
    *,
    dispatch_remote: Callable[[str, dict[str, Any]], Any],
    get_ledger: Callable[[], RunLedger],
    prepare_model_recommendation: Callable[[str, dict[str, Any], RunLedger], tuple[Any, Any, Any, str]],
    finalize_model_recommendation: Callable[..., Any],
) -> ToolExecutionOutcome:
    """Execute one normalized tool call without MCP framing or response rendering.

    Runtime routing/model policy is injected so this module owns the execution
    control flow without depending on a transport composition root. Exceptions
    deliberately propagate to the caller's lifecycle/error mapping.
    """
    name = prepared.name
    args = prepared.args

    if prepared.remote_routed:
        started = time.perf_counter()
        result = dispatch_remote(name, args)
        duration_ms = round((time.perf_counter() - started) * 1000)
        if isinstance(result, dict):
            result = clean_tool_result(result, name)
        return ToolExecutionOutcome(
            result=result,
            duration_ms=duration_ms,
            remote_routed=True,
        )

    ledger = get_ledger()
    route_payload, route_state, route_workflow, route_step = prepare_model_recommendation(name, args, ledger)
    handler = prepared.spec["handler"]
    if not callable(handler):
        raise TypeError(f"registered tool has no callable handler: {name}")

    # Per-call handler state must never leak from a previous invocation on the
    # same worker thread.
    tool_call_tokens_saved.value = 0
    tool_call_counterfactual.value = None
    tool_call_rendered_text.value = None

    wrapper_model = (
        str(route_payload.get("model") or "")
        if route_enforcement_enabled() and route_payload.get("configured") is not False
        else ""
    )
    try:
        started = time.perf_counter()
        with active_model_override(wrapper_model or None):
            result = handler(args)
        duration_ms = round((time.perf_counter() - started) * 1000)
    finally:
        # Recommendation finalization is telemetry/accounting and must not mask
        # the handler's real exception.
        with contextlib.suppress(Exception):
            finalize_model_recommendation(
                route_payload,
                led=ledger,
                tool_name=name,
                session_state=route_state,
                workflow=route_workflow,
                current_step=route_step,
                wrapper_applied=bool(wrapper_model),
                wrapper_model=wrapper_model or None,
            )

    return ToolExecutionOutcome(
        result=result,
        duration_ms=duration_ms,
        remote_routed=False,
        ledger=ledger,
    )


__all__ = ["ToolExecutionOutcome", "execute_prepared_tool_call", "route_enforcement_enabled"]
