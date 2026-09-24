"""Dependency-minimal live registry state shared by tool decorators and callers."""

from __future__ import annotations

from typing import Any

# One process-wide registry: name -> {name, handler, description, inputSchema, ...}.
# This module intentionally imports nothing from adapters, SDK, CLI, or tools APIs.
REGISTERED_TOOLS: dict[str, dict[str, Any]] = {}

__all__ = ["REGISTERED_TOOLS"]
