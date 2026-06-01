"""Regression tests for the consolidated MCP tool surface."""

from __future__ import annotations

EXPECTED_TOOLS = {
    "context",
    "route",
    "rescue",
    "trace",
    "verify",
    "memory",
    "read",
    "edit",
    "grep",
    "sql",
    "search",
    "compact",
    "symbols",
    "node",
    "callers",
    "callees",
    "impact",
    "explore",
    "shell",
    "pattern",
    "usages",
}


class TestMCPTools:
    """Test MCP tool registration and schemas."""

    def test_core_tools_present(self) -> None:
        from atelier.gateway.adapters import mcp_server

        assert set(mcp_server.TOOLS.keys()) == EXPECTED_TOOLS

    def test_all_tools_have_handlers(self) -> None:
        from atelier.gateway.adapters import mcp_server

        for tool_name, tool_spec in mcp_server.TOOLS.items():
            assert "handler" in tool_spec, f"{tool_name} missing handler"
            assert callable(tool_spec["handler"]), f"{tool_name} handler not callable"
            assert "description" in tool_spec, f"{tool_name} missing description"
            assert "inputSchema" in tool_spec, f"{tool_name} missing inputSchema"

    def test_all_schemas_valid_json_schema(self) -> None:
        from atelier.gateway.adapters import mcp_server

        for tool_spec in mcp_server.TOOLS.values():
            schema = tool_spec["inputSchema"]
            assert isinstance(schema, dict)
            assert schema.get("type") in ("object", None)
            if "properties" in schema:
                assert isinstance(schema["properties"], dict)
            if "required" in schema:
                assert isinstance(schema["required"], list)
