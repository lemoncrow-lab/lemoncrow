"""prompt_toolkit completer for the interactive LemonCrow CLI."""

from __future__ import annotations

from collections.abc import Iterable

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

SLASH_COMMANDS = [
    "/help",
    "/exit",
    "/quit",
    "/clear",
    "/tools",
    "/sessions",
    "/session",
    "/memory",
    "/route",
    "/context",
    "/verify",
    "/background",
    "/diff",
    "/approve",
    "/deny",
    "/bash",
]


class LemonCrowCompleter(Completer):
    """Complete slash commands and session ids."""

    def __init__(self, session_ids: list[str] | None = None) -> None:
        self._session_ids = session_ids if session_ids is not None else []

    def get_completions(self, document: Document, complete_event: object) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        parts = text.split()
        # Completing the command name itself.
        if len(parts) <= 1 and not text.endswith(" "):
            word = parts[0] if parts else "/"
            for cmd in SLASH_COMMANDS:
                if cmd.startswith(word):
                    yield Completion(cmd, start_position=-len(word))
            return

        # Completing arguments for /session — offer known session ids.
        if parts[0] in ("/session",):
            fragment = "" if text.endswith(" ") else parts[-1]
            for sid in self._session_ids:
                if sid.startswith(fragment):
                    yield Completion(sid, start_position=-len(fragment))
