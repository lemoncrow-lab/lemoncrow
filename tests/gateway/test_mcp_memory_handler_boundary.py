from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_memory
from lemoncrow.gateway.tools.registry import tool_spec


def test_mcp_server_reexports_extracted_memory_handler() -> None:
    assert mcp_server.tool_memory is tools_memory.tool_memory


def test_registry_uses_extracted_memory_handler_object() -> None:
    spec = tool_spec("memory")
    assert spec is not None
    assert spec["handler"] is tools_memory.tool_memory


def test_memory_handler_module_does_not_import_mcp_server() -> None:
    source = inspect.getsource(tools_memory)
    assert "from lemoncrow.gateway.adapters import mcp_server" not in source
    assert "from lemoncrow.gateway.adapters.mcp_server import" not in source


def test_memory_hook_factory_late_binds_legacy_monkeypatches(monkeypatch) -> None:
    marker = object()

    def symbol_recall():
        return marker

    monkeypatch.setattr(mcp_server, "_symbol_recall", symbol_recall)
    hooks = mcp_server._memory_handler_hooks()
    assert hooks.symbol_recall is symbol_recall
    assert hooks.symbol_recall() is marker
