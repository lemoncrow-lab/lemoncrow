"""Shared host registry for session import, reconstruction, and analysis paths."""

from __future__ import annotations

from importlib import import_module
from typing import Any

SUPPORTED_SESSION_IMPORT_HOSTS: tuple[str, ...] = (
    "antigravity",
    "claude",
    "codex",
    "copilot",
    "cursor",
    "opencode",
)

HOST_IMPORTER_CLASSES: dict[str, tuple[str, str]] = {
    "antigravity": ("atelier.gateway.hosts.session_parsers.antigravity", "AntigravityImporter"),
    "claude": ("atelier.gateway.hosts.session_parsers.claude", "ClaudeImporter"),
    "codex": ("atelier.gateway.hosts.session_parsers.codex", "CodexImporter"),
    "copilot": ("atelier.gateway.hosts.session_parsers.copilot", "CopilotImporter"),
    "cursor": ("atelier.gateway.hosts.session_parsers.cursor", "CursorImporter"),
    "opencode": ("atelier.gateway.hosts.session_parsers.opencode", "OpenCodeImporter"),
}


def iter_importer_classes() -> list[tuple[str, type[Any]]]:
    """Resolve importer classes lazily so callers do not import every host eagerly."""

    resolved: list[tuple[str, type[Any]]] = []
    for host in SUPPORTED_SESSION_IMPORT_HOSTS:
        module_name, class_name = HOST_IMPORTER_CLASSES[host]
        module = import_module(module_name)
        resolved.append((host, getattr(module, class_name)))
    return resolved
