from __future__ import annotations

from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_read
from lemoncrow.gateway.tools.registry import tool_spec


def test_read_handler_is_registered_from_extracted_module() -> None:
    spec = tool_spec("read")
    assert spec is not None
    assert spec["handler"] is tools_read.tool_smart_read
    assert mcp_server.tool_smart_read is tools_read.tool_smart_read
    assert mcp_server._recover_read_stray_query is tools_read.recover_read_stray_query


def test_mcp_server_no_longer_defines_read_registration() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/lemoncrow/gateway/adapters/mcp_server.py").read_text(encoding="utf-8")
    assert '@mcp_tool(\n    name="read"' not in source
    assert "def tool_smart_read(" not in source
    assert "def _recover_read_stray_query(" not in source


def test_read_hooks_late_bind_mcp_helpers(monkeypatch) -> None:
    marker = object()

    def fake_reader(*_args, **_kwargs):
        return marker

    monkeypatch.setattr(mcp_server, "_smart_read_single", fake_reader)
    hooks = mcp_server._read_handler_hooks()
    assert hooks.smart_read_single is fake_reader
    assert hooks.smart_read_single() is marker
