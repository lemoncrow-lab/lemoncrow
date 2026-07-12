"""Proxy tool support: discover and call tools on OTHER configured stdio MCP
servers (never LemonCrow's own).

Each configured server is spawned lazily on first use (a `catalog()` listing
or a `call()`) and the spawned process is cached for the life of this
gateway process -- never eagerly spawned at import time, since each spawn is
a subprocess that can hang for up to the loader's RPC timeout. A slim catalog
(server -> tool names/descriptions/param summary) is cached in-process and a
copy is persisted to disk so a fresh session can show a recent listing
without paying spawn cost again.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.mcp_integration.loader import (
    SELF_SERVER_NAMES,
    MCPServerConfig,
    MCPServerProcess,
    MCPTool,
    discover_mcp_configs,
)

logger = logging.getLogger(__name__)

_DESCRIPTION_TRIM_CHARS = 200


def _catalog_cache_path() -> Path:
    """On-disk catalog cache location, honoring ``LEMONCROW_ROOT`` like the rest
    of the gateway (and so test-isolated by the same fixture that sandboxes it).
    """
    root = os.environ.get("LEMONCROW_ROOT") or str(Path.home() / ".lemoncrow")
    return Path(root) / "mcp_proxy_catalog.json"


def _is_self(config: MCPServerConfig) -> bool:
    """True if *config* would spawn LemonCrow's own MCP server (avoid recursion).

    Matches LemonCrow's real self-registration shapes (``command="lc"``,
    ``args=["mcp", ...]``, optionally wrapped in ``uv run``/``uvx``) by exact
    token, not substring: a substring match on the full command path would
    false-positive on any command whose absolute path happens to contain
    "lc" (e.g. an arg or path segment that happens to contain those two
    letters) or whose arg happens to contain "mcp" as a substring (e.g. a
    fixture script named ``fake_mcp_server.py``).

    Also matches by server NAME alone (``SELF_SERVER_NAMES``, shared with
    ``discover_mcp_configs`` so both layers agree): some real host configs
    register LemonCrow's own server under a shape the token check above misses
    entirely -- e.g. Cursor's config-merge path writes
    ``{"command": "lc", "args": ["--host", "cursor"]}``, with no
    "mcp"/"serve" token anywhere. The server name is a weaker signal (a
    third-party server could coincidentally be named "lc") but every real
    LemonCrow self-registration observed across hosts uses one of these names, so
    the trade-off favors closing the recursion hole. In practice
    ``discover_mcp_configs`` already drops these names before this function
    ever sees them; this check is a second, independent layer in case a config
    reaches this registry through some other path.
    """
    tokens = {Path(config.command).name.lower()}
    tokens.update(str(a).lower() for a in config.args)
    if "lc" in tokens and ("mcp" in tokens or "serve" in tokens):
        return True
    return config.name.strip().lower() in SELF_SERVER_NAMES


def _workspace_root_for_discovery() -> Path | None:
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.environ.get("LEMONCROW_WORKSPACE_ROOT")
    return Path(workspace) if workspace else None


def _slim_tool(tool: MCPTool) -> dict[str, Any]:
    """Compact tool summary for the catalog: name, one-line description, params."""
    description = (tool.description or "").strip().splitlines()[0] if tool.description else ""
    if len(description) > _DESCRIPTION_TRIM_CHARS:
        description = description[: _DESCRIPTION_TRIM_CHARS - 1] + "…"
    schema = tool.input_schema or {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    required_params: list[str] = []
    optional_params: list[str] = []
    if isinstance(properties, dict):
        for prop_name in properties:
            (required_params if prop_name in required else optional_params).append(prop_name)
    return {
        "name": tool.name,
        "description": description,
        "params": {"required": required_params, "optional": optional_params},
    }


def _persist_catalog(catalog: dict[str, Any]) -> None:
    path = _catalog_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("failed to persist MCP proxy catalog", exc_info=True)


def cached_server_names() -> list[str]:
    """Server names from the last persisted catalog snapshot, if any.

    Never spawns a server: reads the on-disk cache written by a prior
    ``catalog()`` call. Best-effort; returns an empty list on any error
    (missing file, corrupt JSON, unreadable) or when nothing has been
    persisted yet.
    """
    try:
        data = json.loads(_catalog_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("servers") if isinstance(data, dict) else None
    return sorted(servers) if isinstance(servers, dict) else []


class _ProxyRegistry:
    """Thread-safe lazy registry of spawned MCP server processes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, MCPServerProcess] = {}
        self._configs: dict[str, MCPServerConfig] | None = None

    def _configs_by_name(self, *, refresh: bool = False) -> dict[str, MCPServerConfig]:
        if refresh or self._configs is None:
            configs = discover_mcp_configs(workspace_root=_workspace_root_for_discovery())
            self._configs = {c.name: c for c in configs if not _is_self(c)}
        return self._configs

    def _get_or_spawn(self, server: str, configs: dict[str, MCPServerConfig]) -> MCPServerProcess | None:
        with self._lock:
            proc = self._processes.get(server)
            if proc is not None:
                return proc
            config = configs.get(server)
            if config is None:
                return None
            proc = MCPServerProcess(config)
            if not proc.start():
                return None
            self._processes[server] = proc
            return proc

    def catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        configs = self._configs_by_name(refresh=refresh)
        servers: dict[str, Any] = {}
        for name, _config in configs.items():
            proc = self._get_or_spawn(name, configs)
            if proc is None:
                servers[name] = {"error": "failed to start MCP server"}
                continue
            try:
                tools = proc.list_tools()
            except Exception as exc:  # noqa: BLE001 - never raise out of catalog()
                servers[name] = {"error": f"failed to list tools: {exc}"}
                continue
            servers[name] = {"tools": [_slim_tool(t) for t in tools]}
        result = {"servers": servers}
        _persist_catalog(result)
        return result

    def call(self, server: str, tool: str, params: dict[str, Any]) -> str:
        configs = self._configs_by_name()
        if server not in configs:
            known = ", ".join(sorted(configs)) or "(none configured)"
            return f"Error: unknown MCP server {server!r}; known servers: {known}"
        proc = self._get_or_spawn(server, configs)
        if proc is None:
            return f"Error: failed to start MCP server {server!r}"
        tool_names = {t.name for t in (proc.tools or proc.list_tools())}
        if tool not in tool_names:
            known_tools = ", ".join(sorted(tool_names)) or "(none)"
            return f"Error: unknown tool {tool!r} on server {server!r}; known tools: {known_tools}"
        return proc.call_tool(tool, params, max_chars=None)

    def shutdown(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
            self._processes.clear()
        for proc in processes:
            proc.stop()


_registry = _ProxyRegistry()
atexit.register(lambda: _registry.shutdown())


def catalog(*, refresh: bool = False) -> dict[str, Any]:
    """Discovered servers -> their tools (name, description, param summary)."""
    return _registry.catalog(refresh=refresh)


def call(server: str, tool: str, params: dict[str, Any]) -> str:
    """Spawn-or-reuse *server* and call *tool*, returning the full result text."""
    return _registry.call(server, tool, params)


def shutdown() -> None:
    """Stop every spawned server process. Also registered via ``atexit``."""
    _registry.shutdown()
