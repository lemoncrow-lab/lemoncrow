"""Binding to the public LemonCrow tool registry.

The server does not fork tool implementations. Server-side execution uses the
same prepared-call execution and lifecycle path as public MCP, but consumes the
finalized tool payload directly rather than constructing/parsing JSON-RPC:

``lemoncrow.gateway.tools.call_runtime.execute_default_tool_payload(id, params)``
    Transport-neutral finalized-content executor. Handler bootstrap currently
    installs its default hook factory; server-core does not import the MCP
    transport module.
``lemoncrow.gateway.tools.registry.registered_tools()``
    The canonical live registry. Name -> spec, where the spec holds ``handler``
    and ``inputSchema``. Consumers do not reach into ``mcp_server.TOOLS``.
``lemoncrow.gateway.adapters.mcp.ledger``
    Per-thread request ledger/session scoping. The public HTTP adapter and this
    server share those narrow helpers directly; they are not owned by the JSON-RPC
    dispatcher module.

Handler bootstrap remains lazy. Construction resolves the canonical registry
first, then verifies that the transport-neutral default runtime is configured,
so a composition error fails before the first tenant tool call.

Multi-tenancy, stated rather than discovered later: the public dispatcher
resolves one workspace per *process*, so nothing here may be left to resolve
itself. ``ToolInvocation.workspace_root`` names the tree instead, and the
shipped server always sets it -- :mod:`.workspace` rebuilds the bound view from
the tenant's own content at the acknowledged revision before dispatch. Because
the binding below is process-global it is held under one lock, which serializes
tool dispatch within a process; see :func:`_workspace_binding`.

How that root is made real matters, because the obvious answer is wrong. The
``_meta`` project-override channel the public HTTP adapter documents is opt-in
(``LEMONCROW_HTTP_ALLOW_PROJECT_OVERRIDE``, default off), confined to the
process workspace root, and read only by ``mcp_server._workspace_root``. The
file tools resolve through ``mcp_server._workspace_path``, which consults
``CLAUDE_WORKSPACE_ROOT``/``cwd`` and never the override -- so a server that
sent only ``_meta`` would answer ``read`` out of its own checkout while
believing the tenant was isolated. :func:`_workspace_binding` therefore binds
the environment the tools actually read, under a lock, and sends ``_meta`` as
well so both channels name the same tree.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from lemoncrow.gateway.adapters.mcp.ledger import (
    _clear_request_ledger,
    _clear_request_session,
    _set_request_ledger,
    _set_request_session,
)
from lemoncrow.gateway.tools.errors import ToolProtocolError

from .dispatch import ToolInvocation, ToolResult
from .errors import ErrorCode, ServerError
from .matrix import DISPATCHABLE_TOOLS

__all__ = ["REQUIRED_REGISTRY_ATTRIBUTES", "PublicRegistryDispatcher"]

_LOG: Final[logging.Logger] = logging.getLogger("lemoncrow.server.dispatch")

_RUNTIME_MODULE: Final[str] = "lemoncrow.gateway.tools.call_runtime"
_REGISTRY_MODULE: Final[str] = "lemoncrow.gateway.tools.registry"

#: Probed at construction. A rename upstream must break the server here, not
#: at the first tool call of a customer's session.
REQUIRED_REGISTRY_ATTRIBUTES: Final[tuple[str, ...]] = ("_handle_tool_payload",)

#: Every variable the public workspace resolution consults, in its own
#: precedence order. All of them are set together so no lower-precedence
#: leftover from the server's own process can win inside a tenant's call.
_WORKSPACE_ENV: Final[tuple[str, ...]] = (
    "CLAUDE_WORKSPACE_ROOT",
    "LEMONCROW_WORKSPACE_ROOT",
    "VSCODE_CWD",
)

#: Upstream opt-in for the wire-supplied project override.
_OVERRIDE_ENV: Final[str] = "LEMONCROW_HTTP_ALLOW_PROJECT_OVERRIDE"

#: Protocol fault codes emitted by prepared tool-call validation/storage.
_PROTOCOL_TO_CODE: Final[Mapping[int, ErrorCode]] = {
    -32601: ErrorCode.TOOL_UNKNOWN,
    -32602: ErrorCode.PAYLOAD_INVALID,
    -32700: ErrorCode.PAYLOAD_INVALID,
    -32600: ErrorCode.PAYLOAD_INVALID,
}


class PublicRegistryDispatcher:
    """Execute a server-side tool through the public registry."""

    __slots__ = ("_lock", "_payload_executor", "_tools")

    def __init__(self, module: Any | None = None) -> None:
        injected = module is not None
        if injected:
            missing = [name for name in REQUIRED_REGISTRY_ATTRIBUTES if not hasattr(module, name)]
            if missing:
                raise ServerError(
                    ErrorCode.DISPATCHER_UNAVAILABLE,
                    f"public tool dispatcher is missing required entrypoints: {', '.join(missing)}",
                    details={"missing": missing, "module": type(module).__name__},
                )
            registry = getattr(module, "TOOLS", None)
            if not isinstance(registry, Mapping):
                raise ServerError(
                    ErrorCode.DISPATCHER_UNAVAILABLE,
                    "injected public tool dispatcher is missing a TOOLS registry",
                    details={"missing": ["TOOLS"], "module": type(module).__name__},
                )
            registered = frozenset(str(name) for name in registry)
            self._payload_executor = module._handle_tool_payload
        else:
            registered = _registered_tool_names()
            self._payload_executor = _default_payload_executor()
        self._tools = registered & DISPATCHABLE_TOOLS
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return True

    @property
    def tools(self) -> frozenset[str]:
        """Server-side tools this build can actually dispatch.

        The intersection of the execution matrix with what the public registry
        really registers: advertising a tool the registry dropped would turn a
        configuration problem into a mid-session failure.
        """
        return self._tools

    @property
    def unregistered_matrix_tools(self) -> frozenset[str]:
        """Matrix entries the loaded registry does not provide."""
        return DISPATCHABLE_TOOLS - self._tools

    def dispatch(self, invocation: ToolInvocation) -> ToolResult:
        if invocation.tool not in self._tools:
            raise ServerError(
                ErrorCode.TOOL_UNKNOWN,
                f"{invocation.tool} is not registered by the loaded tool registry",
                details={"tool": invocation.tool},
            )
        payload_executor = self._payload_executor
        params: dict[str, Any] = {
            "name": invocation.tool,
            "arguments": dict(invocation.arguments),
        }
        root = _validated_workspace_root(invocation)
        if root is not None:
            # Belt: the metadata channel the public HTTP adapter documents for
            # request-scoped project isolation (``_extract_request_project``).
            # It is opt-in upstream and does not reach every tool, so it is not
            # load-bearing on its own -- see ``_workspace_binding``.
            params["_meta"] = {"mcp-project-path": str(root)}
        with _workspace_binding(root, self._lock):
            # Scope the ledger and session identity to THIS tenant's session,
            # on this worker thread, for the duration of the call -- the public
            # helpers are per-thread and _handle runs on this thread.
            prior_ledger = _set_request_ledger(invocation.tenant.session_id)
            prior_session = _set_request_session(
                invocation.host_session_id or invocation.tenant.session_id,
                invocation.host_name or invocation.client_name or "lemoncrow-server",
                invocation.model,
                "",
            )
            try:
                payload = payload_executor(invocation.tenant.request_id, params)
            except ToolProtocolError as exc:
                code = _PROTOCOL_TO_CODE.get(exc.code, ErrorCode.TOOL_FAILED)
                raise ServerError(
                    code,
                    str(exc),
                    details={"tool": invocation.tool, "protocol_code": exc.code},
                    status=200 if code is ErrorCode.TOOL_FAILED else None,
                ) from exc
            except Exception as exc:
                correlation_id = uuid.uuid4().hex
                _LOG.exception(
                    "tool dispatch raised (tool=%s correlation_id=%s)",
                    invocation.tool,
                    correlation_id,
                )
                raise ServerError(
                    ErrorCode.INTERNAL,
                    f"internal error (correlation_id={correlation_id})",
                    details={"tool": invocation.tool, "correlation_id": correlation_id},
                ) from exc
            finally:
                _clear_request_session(prior_session)
                _clear_request_ledger(prior_ledger)

        return _payload_to_result(invocation.tool, payload)


def _validated_workspace_root(invocation: ToolInvocation) -> Path | None:
    """The tenant's tree, or ``None`` when the deployment serves one workspace.

    A root that is relative, absent or not a directory is a deployment fault,
    not a tenant's: refusing here keeps a misconfigured resolver from silently
    serving whatever tree the server process happens to sit in.
    """
    raw = invocation.workspace_root
    if not raw:
        return None
    root = Path(raw)
    if not root.is_absolute() or not root.is_dir():
        raise ServerError(
            ErrorCode.INTERNAL,
            "workspace_resolver returned a path that is not an existing absolute directory",
            details={"tool": invocation.tool},
        )
    return root


@contextmanager
def _workspace_binding(root: Path | None, lock: threading.Lock) -> Iterator[None]:
    """Make ``root`` the workspace the public tools actually resolve against.

    The ``_meta`` project override is not sufficient and must not be trusted on
    its own. Upstream it is gated behind ``LEMONCROW_HTTP_ALLOW_PROJECT_OVERRIDE``
    (default off), confined to the process workspace root, and -- decisively --
    read only by ``mcp_server._workspace_root``. The path-resolving helper the
    file tools use, ``mcp_server._workspace_path``, consults
    ``CLAUDE_WORKSPACE_ROOT``/``cwd`` and never the override, so a server that
    set only ``_meta`` would answer ``read`` from its own checkout while
    believing it had isolated the tenant. Verified against the public package
    and pinned by ``tests/test_workspace_binding.py``.

    The working directory is bound too, and for a reason that only shows up in
    output: several tools relativize the paths they report against
    ``Path.cwd()`` (the multi-file ``read`` header among them). A server that
    bound only the environment would answer correct *content* under headers
    naming its own absolute filesystem layout -- a path the agent cannot use
    and a detail the tenant should never see.

    The environment and the working directory are process-global, so the
    binding is held under one lock: tool dispatch inside a process serving more
    than one tenant is serialized rather than racy. Scale is a deployment
    question (one process per tenant, or a worker per tenant); correctness is
    not negotiable. The server's own durable state is immune by construction --
    ``index.sqlite._absolute`` pins the database path when it is opened.
    """
    if root is None:
        yield
        return
    with lock:
        previous = {name: os.environ.get(name) for name in _WORKSPACE_ENV}
        for name in _WORKSPACE_ENV:
            os.environ[name] = str(root)
        # Upstream honours the wire override only when this is set; with it set
        # the override is additionally confined to the root we just declared,
        # so the two channels agree instead of one quietly winning.
        previous[_OVERRIDE_ENV] = os.environ.get(_OVERRIDE_ENV)
        os.environ[_OVERRIDE_ENV] = "1"
        working_directory = os.getcwd()
        os.chdir(root)
        try:
            yield
        finally:
            os.chdir(working_directory)
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _default_payload_executor() -> Any:
    try:
        from lemoncrow.gateway.tools.call_runtime import (
            default_tool_runtime_configured,
            execute_default_tool_payload,
        )
    except Exception as exc:
        raise ServerError(
            ErrorCode.DISPATCHER_UNAVAILABLE,
            f"public tool runtime {_RUNTIME_MODULE} could not be imported",
            details={"module": _RUNTIME_MODULE, "reason": type(exc).__name__},
        ) from exc
    if not default_tool_runtime_configured():
        raise ServerError(
            ErrorCode.DISPATCHER_UNAVAILABLE,
            "public tool runtime is not configured after registry bootstrap",
            details={"module": _RUNTIME_MODULE},
        )
    return execute_default_tool_payload


def _registered_tool_names() -> frozenset[str]:
    try:
        from lemoncrow.gateway.tools.registry import registered_tool_names

        return registered_tool_names()
    except Exception as exc:
        raise ServerError(
            ErrorCode.DISPATCHER_UNAVAILABLE,
            f"public tool registry {_REGISTRY_MODULE} could not be imported",
            details={"module": _REGISTRY_MODULE, "reason": type(exc).__name__},
        ) from exc


def _payload_to_result(tool: str, payload: Any) -> ToolResult:
    """Convert a finalized public tool payload into the server result contract."""
    if not isinstance(payload, dict):
        raise ServerError(
            ErrorCode.INTERNAL,
            "tool runtime returned a non-object payload",
            details={"tool": tool},
        )

    raw_content = payload.get("content")
    blocks: tuple[Mapping[str, Any], ...] = ()
    if isinstance(raw_content, list):
        blocks = tuple(block for block in raw_content if isinstance(block, dict))

    structured: Mapping[str, Any] | None = None
    candidate = payload.get("structuredContent")
    if isinstance(candidate, dict):
        structured = candidate
    elif len(blocks) == 1 and blocks[0].get("type") == "text":
        text = blocks[0].get("text")
        if isinstance(text, str) and text[:1] in "{[":
            try:
                parsed = json.loads(text)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, dict):
                structured = parsed

    return ToolResult(
        tool=tool,
        content=blocks,
        structured=structured,
        is_error=bool(payload.get("isError", False)),
    )
