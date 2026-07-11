"""MCP-tool-only gating primitives for the LemonCrow CLI.

These symbols are moved verbatim from ``app.py`` so command modules can import
them *downward* (``commands/* -> commands/_dev``) without depending on the
global ``cli`` object defined in ``app.py``. Keeping them here breaks the
future ``app.py`` <-> ``commands/*`` circular-import risk (RESEARCH Pitfall 1).

``_dev_command`` / ``_dev_group`` themselves stay in ``app.py`` because they
register on the global ``cli`` object; they import the sets, ``_DummyGroup``,
from this module.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click


class _DummyGroup:
    """A placeholder for a Click group that does nothing."""

    def command(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return lambda f: f

    def group(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        return lambda f: _DummyGroup()


MCP_TOOL_ONLY_COMMANDS = frozenset({"context", "rescue", "verify", "read", "edit", "search"})
MCP_TOOL_ONLY_GROUPS = frozenset({"memory", "route"})


def dev_command(name: str | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Build a Click command, suppressing names reserved as MCP-only tools."""
    if name in MCP_TOOL_ONLY_COMMANDS:
        return lambda f: f

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return click.command(name, **kwargs)(func)  # type: ignore[no-untyped-call]

    return decorator


def dev_group(name: str | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
    """Build a Click group, suppressing names reserved as MCP-only groups."""
    if name in MCP_TOOL_ONLY_GROUPS:
        return lambda f: _DummyGroup()

    def decorator(func: Callable[..., Any]) -> Any:
        return click.group(name, **kwargs)(func)  # type: ignore[no-untyped-call]

    return decorator
