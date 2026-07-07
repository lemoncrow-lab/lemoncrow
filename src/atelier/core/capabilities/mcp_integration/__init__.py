"""MCP server integration: spawn .mcp.json servers and expose their tools."""

from __future__ import annotations

from atelier.core.capabilities.mcp_integration.loader import (
    MCPServerConfig,
    MCPServerProcess,
    MCPTool,
    discover_mcp_configs,
)

__all__ = ["MCPServerConfig", "MCPServerProcess", "MCPTool", "discover_mcp_configs"]
