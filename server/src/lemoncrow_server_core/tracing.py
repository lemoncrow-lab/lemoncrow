"""Per-call runtime tracing for every server-dispatched tool.

Wraps the outermost :class:`~.dispatch.ToolDispatcher` so native index answers
(``code_search``, ``search``, server graph kinds) and registry-routed tools are
traced alike.  The trace id is the client's ``X-LemonCrow-Request-Id`` when it
is an ``lt_`` id, so the client record and the server record share one id;
the in-process runtime joins this trace instead of writing its own.

It wraps ``dispatch`` itself (not the HTTP route) because the route runs the
dispatcher on an executor thread, and the active trace is a context variable.
"""

from __future__ import annotations

from typing import Any

from lemoncrow.core.foundation.runtime_trace import TraceMode, TraceStore, is_trace_id, open_trace

from .dispatch import ToolDispatcher, ToolInvocation, ToolResult
from .errors import ServerError

__all__ = ["TracingDispatcher"]


def _content_chars(result: ToolResult) -> int:
    return sum(len(block["text"]) for block in result.content if isinstance(block.get("text"), str))


class TracingDispatcher:
    """Delegating dispatcher that records one runtime trace per call."""

    def __init__(self, inner: ToolDispatcher, *, mode: TraceMode, store: TraceStore | None = None) -> None:
        self._inner = inner
        self._mode: TraceMode = mode
        self._store = store

    @property
    def inner(self) -> ToolDispatcher:
        return self._inner

    @property
    def available(self) -> bool:
        return self._inner.available

    @property
    def tools(self) -> frozenset[str]:
        return self._inner.tools

    def __getattr__(self, name: str) -> Any:
        # Optional capabilities (prewarm_workspace, forget_workspace, close...)
        # are probed with getattr by the apps; keep them visible.
        return getattr(self._inner, name)

    def dispatch(self, invocation: ToolInvocation) -> ToolResult:
        tenant = invocation.tenant
        request_id = tenant.request_id
        with open_trace(
            trace_id=request_id if is_trace_id(request_id) else None,
            writer="server",
            tool=invocation.tool,
            arguments=invocation.arguments,
            mode=self._mode,
            session_id=invocation.host_session_id or tenant.session_id,
            workspace_id=tenant.view_id,
            workspace_revision=tenant.view_revision,
            store=self._store,
        ) as trace:
            try:
                result = self._inner.dispatch(invocation)
            except ServerError as exc:
                trace.note("outcome", is_error=True, error_code=exc.code.value)
                raise
            chars = _content_chars(result)
            trace.note(
                "outcome",
                is_error=result.is_error,
                degraded=result.degraded,
                degraded_reason=result.degraded_reason or None,
                output_chars=chars,
                est_output_tokens=chars // 4,
            )
            return result
