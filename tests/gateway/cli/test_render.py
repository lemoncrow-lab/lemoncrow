"""Tests for the Rich-based event renderer."""

from __future__ import annotations

import asyncio
from io import StringIO

from rich.console import Console

from atelier.gateway.cli.events import (
    AssistantMessage,
    AtelierEvent,
    RuntimeErrorEvent,
)
from atelier.gateway.cli.render import EventRenderer
from tests.gateway.cli.test_events import _all_events


def _render(event: AtelierEvent) -> str:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=80)
    renderer = EventRenderer(console)
    asyncio.run(renderer.render(event))
    renderer.end_stream()
    return buf.getvalue()


def test_each_event_renders_without_error() -> None:
    for event in _all_events():
        _render(event)


def test_assistant_markdown_renders() -> None:
    out = _render(AssistantMessage(type="assistant.message", text="# Title\n\n**bold**"))
    assert "Title" in out
    assert "bold" in out


def test_error_event_renders() -> None:
    out = _render(RuntimeErrorEvent(type="error", message="kaboom"))
    assert "kaboom" in out


def test_welcome_banner() -> None:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=80)
    renderer = EventRenderer(console)
    renderer.print_welcome(session_id="tui-abc", project_root="/repo")
    out = buf.getvalue()
    assert "tui-abc" in out
    assert "/repo" in out
