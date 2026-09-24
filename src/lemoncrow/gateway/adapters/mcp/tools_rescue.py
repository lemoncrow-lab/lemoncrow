"""Rescue MCP handler with late-bound runtime/session composition."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lemoncrow.core.foundation.models import to_jsonable
from lemoncrow.gateway.adapters.mcp.framework import mcp_tool


@dataclass(frozen=True, slots=True)
class RescueHandlerHooks:
    runtime: Callable[[], Any]
    get_ledger: Callable[[], Any]
    match_mcp_lexical: Callable[[dict[str, Any]], Any]
    get_product_session_id: Callable[[], str]


_HooksFactory = Callable[[], RescueHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_rescue_handler_hooks(factory: _HooksFactory) -> None:
    """Install process composition used by the rescue handler."""
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> RescueHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("rescue handler hooks are not configured")
    return factory()


@mcp_tool(name="rescue")
def tool_rescue_failure(
    task: str,
    error: str,
    domain: str | None = None,
    files: list[str] | None = None,
    recent_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Suggest a rescue procedure for a repeated failure (call after the same approach fails twice).

    Returns: {cluster_id, domain, rescue_type, procedure: [{step, rationale}], rationale, analysis?}.
    """
    hooks = _hooks()
    if recent_actions is None:
        recent_actions = []
    if files is None:
        files = []
    rt = hooks.runtime()
    led = hooks.get_ledger()
    hooks.match_mcp_lexical({"task": task, "error": error})
    led.record_tool_call(
        "rescue_failure",
        {
            "task": task,
            "error": error,
            "domain": domain,
            "files": files,
            "recent_actions": recent_actions,
        },
    )

    result = rt.rescue_failure(
        task=task,
        error=error,
        files=files,
        domain=domain,
        recent_actions=recent_actions,
    )
    payload = to_jsonable(result)
    with contextlib.suppress(Exception):
        from lemoncrow.core.service.telemetry import emit_product
        from lemoncrow.core.service.telemetry.schema import hash_identifier

        matched = list(payload.get("matched_blocks", []) or []) if isinstance(payload, dict) else []
        emit_product(
            "rescue_offered",
            cluster_id_hash=hash_identifier(str(matched[0] if matched else "unmatched_rescue")),
            rescue_type="playbook" if matched else "summary",
            session_id=hooks.get_product_session_id(),
        )

    # Failure incident analysis from prior failed traces.
    with contextlib.suppress(Exception):
        analysis = rt.core_runtime.analyze_failure_for_error(
            task=task,
            error=error,
            domain=domain,
            lookback=200,
        )
        payload["analysis"] = analysis
        incident = analysis.get("incident") if isinstance(analysis, dict) else None
        if isinstance(incident, dict):
            root_cause = incident.get("root_cause_hypothesis", "")
            if isinstance(root_cause, str) and root_cause:
                led.record(
                    "note",
                    "failure_analysis",
                    {
                        "root_cause": root_cause,
                        "fingerprint": incident.get("fingerprint"),
                        "count": incident.get("count"),
                    },
                )

    return payload


__all__ = ["RescueHandlerHooks", "configure_rescue_handler_hooks", "tool_rescue_failure"]
