from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_utility
from lemoncrow.gateway.tools.registry import tool_spec


def test_mcp_server_reexports_extracted_utility_handlers() -> None:
    assert mcp_server.tool_sql is tools_utility.tool_sql
    assert mcp_server.tool_scan is tools_utility.tool_scan
    assert mcp_server.tool_orient is tools_utility.tool_orient
    assert mcp_server.SQL_TOOL_INPUT_SCHEMA is tools_utility.SQL_TOOL_INPUT_SCHEMA


def test_registry_uses_extracted_utility_handler_objects() -> None:
    for name, handler in (
        ("sql", tools_utility.tool_sql),
        ("scan", tools_utility.tool_scan),
        ("orient", tools_utility.tool_orient),
    ):
        spec = tool_spec(name)
        assert spec is not None
        assert spec["handler"] is handler


def test_utility_handler_module_does_not_import_mcp_server() -> None:
    source = inspect.getsource(tools_utility)
    assert "from lemoncrow.gateway.adapters import mcp_server" not in source
    assert "from lemoncrow.gateway.adapters.mcp_server import" not in source
    assert "import lemoncrow.gateway.adapters.mcp_server" not in source
