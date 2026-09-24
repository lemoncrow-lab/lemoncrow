from __future__ import annotations

from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_context
from lemoncrow.gateway.tools.registry import tool_spec


def test_context_handler_is_registered_from_extracted_module() -> None:
    spec = tool_spec("context")
    assert spec is not None
    assert spec["handler"] is tools_context.tool_get_context
    assert mcp_server.tool_get_context is tools_context.tool_get_context


def test_mcp_server_no_longer_defines_context_registration() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/lemoncrow/gateway/adapters/mcp_server.py").read_text(encoding="utf-8")
    assert '@mcp_tool(name="context")' not in source
    assert "def tool_get_context(" not in source


def test_context_hooks_late_bind_runtime(monkeypatch) -> None:
    marker = object()

    def fake_runtime():
        return marker

    monkeypatch.setattr(mcp_server, "_runtime", fake_runtime)
    hooks = mcp_server._context_handler_hooks()
    assert hooks.runtime is fake_runtime
    assert hooks.runtime() is marker
