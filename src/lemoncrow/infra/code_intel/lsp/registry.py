"""Language -> LSP server-command registry (G14).

This is the *extensibility seam*. To add a language, append one entry mapping
its canonical :data:`lemoncrow.infra.code_intel.languages` name to the argv of a
stdio language server, e.g.::

    SERVER_COMMANDS["swift"] = ("sourcekit-lsp",)

A non-empty entry only declares that an adapter exists; whether the server is
actually installed is decided at runtime by the transport's ``is_available``.

We deliberately wire exactly ONE adapter -- Kotlin, a language with NO code-intel
indexer (i.e. LSP escalation is the
only precise resolution available. The rest are intentionally absent: claiming
30 languages we cannot test would be dishonest.
"""

from __future__ import annotations

# canonical language name -> stdio LSP server argv.
# ONE real adapter; extend by adding rows (documented above).
SERVER_COMMANDS: dict[str, tuple[str, ...]] = {
    "kotlin": ("kotlin-language-server",),
}


def server_command_for_language(language: str) -> tuple[str, ...] | None:
    """Return the LSP server argv for *language*, or None if no adapter exists.

    None is the signal to skip LSP entirely and stay on tree-sitter -- callers
    must treat it as a graceful no-op, never an error.
    """
    return SERVER_COMMANDS.get(language)
