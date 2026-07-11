"""Injectable LSP transport abstraction (G14).

The transport is the *only* coupling to a real process. A production transport
frames JSON-RPC over a server's stdio; a test injects a mock implementing the
same Protocol with no process at all. The client never imports a concrete
transport, so the whole bridge is unit-testable without a language server.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class LspTransportError(RuntimeError):
    """Raised by a transport when a request cannot be completed.

    The client treats this (and any other exception) as a graceful-degrade
    signal: it returns no locations rather than propagating the failure.
    """


@runtime_checkable
class LspTransport(Protocol):
    """A request/response channel to a language server.

    Implementations are responsible for JSON-RPC framing, ``initialize`` /
    ``initialized`` handshakes, and ``textDocument/didOpen`` bookkeeping. The
    client only calls :meth:`request` and :meth:`is_available`.
    """

    def is_available(self) -> bool:
        """Return True if the underlying server is reachable.

        A False return is the primary graceful-degrade hook -- the client emits
        no locations and the caller falls back to tree-sitter.
        """
        ...

    def request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a JSON-RPC request and return the decoded ``result`` payload.

        May raise :class:`LspTransportError` (or any exception) on failure; the
        client catches it and degrades.
        """
        ...
