"""MCP rubric verification handler with late-bound runtime composition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lemoncrow.core.foundation.models import to_jsonable
from lemoncrow.core.foundation.rubric_gate import run_rubric
from lemoncrow.gateway.adapters.mcp.framework import mcp_tool


@dataclass(frozen=True, slots=True)
class VerifyHandlerHooks:
    runtime: Callable[[], Any]
    get_ledger: Callable[[], Any]


_HooksFactory = Callable[[], VerifyHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_verify_handler_hooks(factory: _HooksFactory) -> None:
    """Install the process composition used by the verify handler."""
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> VerifyHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("verify handler hooks are not configured")
    return factory()


@mcp_tool(name="verify")
def tool_run_rubric_gate(rubric_id: str, checks: dict[str, Any]) -> Any:
    """Evaluate agent results against a domain rubric. Returns pass|warn|fail with per-check detail."""
    hooks = _hooks()
    runtime = hooks.runtime()
    ledger = hooks.get_ledger()
    ledger.record_tool_call("run_rubric_gate", {"rubric_id": rubric_id, "checks": checks})

    rubric = runtime.store.knowledge.get_rubric(rubric_id)
    if rubric is None:
        raise ValueError(f"rubric not found: {rubric_id}")

    if rubric_id not in ledger.active_rubrics:
        ledger.active_rubrics.append(rubric_id)

    result = run_rubric(rubric, checks)
    ledger.record("rubric_run", f"Rubric {rubric_id} status: {result.status}", to_jsonable(result))
    return to_jsonable(result)


__all__ = ["VerifyHandlerHooks", "configure_verify_handler_hooks", "tool_run_rubric_gate"]
