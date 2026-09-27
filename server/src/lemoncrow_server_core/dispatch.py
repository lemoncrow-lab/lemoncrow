"""The tool dispatch boundary.

The server does not reimplement tools. It routes to the tool implementations
that already exist in the public ``lemoncrow`` package, through the same
JSON-RPC dispatcher that the stdio and HTTP MCP transports use, so "parity with
current stdio behaviour" is a property of the code path rather than a test we
have to keep writing.

This module owns the vocabulary; :mod:`.registry_dispatch` owns the binding.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .context import TenantContext
from .errors import ErrorCode, ServerError

__all__ = [
    "BlobMissing",
    "ToolDispatcher",
    "ToolInvocation",
    "ToolResult",
    "UnavailableDispatcher",
]


class BlobMissing(Exception):
    """The server lacks content it needs to answer.

    Raised by the blob-miss guard before dispatch, and available to any index
    implementation that discovers a gap mid-answer. It is not an error on the
    wire: the route turns it into the ``{"need": [...]}`` protocol answer.
    """

    __slots__ = ("paths",)

    def __init__(self, paths: tuple[str, ...]) -> None:
        self.paths = paths
        super().__init__(f"{len(paths)} path(s) missing server-side")


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """One server-side tool call, fully scoped."""

    tool: str
    arguments: Mapping[str, Any]
    tenant: TenantContext
    client_name: str = ""
    #: Optional local-runtime attribution identity. Hosted/shared composition
    #: leaves these empty; loopback local mode may populate them from the
    #: same-machine thin client. They never replace tenant identity.
    host_session_id: str = ""
    host_name: str = ""
    model: str = ""
    #: Absolute path of the tree this call answers from. The shipped server
    #: always sets it: :class:`~.workspace.ViewMaterializer` rebuilds the bound
    #: view from the tenant's own content store at the acknowledged revision,
    #: and ``ServerApp._bind_workspace`` attaches the result here. A deployment
    #: that genuinely owns one fixed tree says so with
    #: ``ServerApp(workspace_resolver=...)`` instead. ``None`` means neither was
    #: configured, which leaves the public dispatcher resolving against the
    #: server's own process directory -- never a tenant-facing configuration.
    workspace_root: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a dispatched tool produced.

    ``content`` is the MCP content-block list the public dispatcher returns, so
    the thin client can hand it to the host unchanged. ``structured`` carries
    the same payload parsed, when the tool emitted JSON, for callers that would
    otherwise re-parse it.
    """

    tool: str
    content: tuple[Mapping[str, Any], ...]
    structured: Mapping[str, Any] | None = None
    is_error: bool = False
    degraded: bool = False
    degraded_reason: str = ""
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def degrade(self, reason: str) -> ToolResult:
        """Return a copy flagged degraded. Never clears an existing flag."""
        if not reason or self.degraded:
            return self
        return ToolResult(
            tool=self.tool,
            content=self.content,
            structured=self.structured,
            is_error=self.is_error,
            degraded=True,
            degraded_reason=reason,
            diagnostics=self.diagnostics,
        )

    def to_wire(self, *, view_revision: int | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": self.tool,
            "content": [dict(block) for block in self.content],
            "is_error": self.is_error,
            "degraded": self.degraded,
            "view_revision": view_revision,
        }
        if self.degraded:
            payload["degraded_reason"] = self.degraded_reason
        if self.structured is not None:
            payload["structured"] = dict(self.structured)
        if self.diagnostics:
            payload["diagnostics"] = dict(self.diagnostics)
        return payload


class ToolDispatcher(Protocol):
    """Synchronous tool execution.

    Synchronous on purpose: the public dispatcher is synchronous, and wrapping
    it in a coroutine that secretly blocks the event loop is the bug that made
    the public HTTP transport offload to a thread pool in the first place. The
    HTTP layer runs implementations of this protocol in a bounded executor.
    """

    @property
    def available(self) -> bool: ...

    @property
    def tools(self) -> frozenset[str]: ...

    def dispatch(self, invocation: ToolInvocation) -> ToolResult: ...


class UnavailableDispatcher:
    """Refuses every call with a typed, retryable error.

    Used when the public tool registry cannot be imported -- a deployment
    mistake, not a tenant's problem. The server still serves discovery,
    handshake, session and sync routes, so a client gets a precise reason
    instead of a connection failure.
    """

    __slots__ = ("_reason",)

    def __init__(self, reason: str) -> None:
        self._reason = reason

    @property
    def available(self) -> bool:
        return False

    @property
    def tools(self) -> frozenset[str]:
        return frozenset()

    def dispatch(self, invocation: ToolInvocation) -> ToolResult:
        raise ServerError(
            ErrorCode.DISPATCHER_UNAVAILABLE,
            f"tool dispatcher is unavailable: {self._reason}",
            details={"tool": invocation.tool},
        )
