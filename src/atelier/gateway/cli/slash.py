"""Slash-command parsing for the interactive Atelier CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ParsedInput:
    kind: Literal["empty", "slash", "message", "exit", "clear"]
    text: str = ""
    name: str = ""
    args: list[str] = field(default_factory=list)


def parse_input(line: str) -> ParsedInput:
    """Parse a raw input line into a structured command."""
    stripped = line.strip()
    if not stripped:
        return ParsedInput(kind="empty")

    if stripped.startswith("/"):
        parts = stripped[1:].split()
        if not parts:
            return ParsedInput(kind="empty")
        name = parts[0].lower()
        args = parts[1:]
        if name in ("exit", "quit"):
            return ParsedInput(kind="exit", name=name)
        if name == "clear":
            return ParsedInput(kind="clear", name=name)
        return ParsedInput(kind="slash", name=name, args=args)

    return ParsedInput(kind="message", text=stripped)
