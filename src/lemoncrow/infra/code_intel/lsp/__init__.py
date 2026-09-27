"""Minimal, opt-in LSP client bridge foundation (G14).

This package is a *foundation*, not a 30-language solution. It provides:

* :class:`~lemoncrow.infra.code_intel.lsp.transport.LspTransport` -- an injectable
  request/response abstraction. A real implementation speaks JSON-RPC over a
  server's stdio; tests inject a mock with no process.
* :class:`~lemoncrow.infra.code_intel.lsp.client.LspClient` -- a thin bridge
  exposing ``textDocument/definition`` and ``textDocument/references``. It
  gracefully degrades: a missing transport / missing server simply returns
  nothing, so callers fall back to tree-sitter.
* :mod:`~lemoncrow.infra.code_intel.lsp.registry` -- the language -> server-command
  table with exactly ONE adapter wired (see ``SERVER_COMMANDS``). Adding a
  language is a one-line table entry, documented there.

The default code path is unchanged: nothing instantiates an LspClient unless a
caller opts in by supplying a transport.
"""

from .client import Location, LspClient
from .registry import SERVER_COMMANDS, server_command_for_language
from .transport import LspTransport, LspTransportError

__all__ = [
    "SERVER_COMMANDS",
    "Location",
    "LspClient",
    "LspTransport",
    "LspTransportError",
    "server_command_for_language",
]
