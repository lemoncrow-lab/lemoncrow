from __future__ import annotations

from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tool_registry_state import REGISTERED_TOOLS
from lemoncrow.gateway.tools.registry import call_registered_tool, registered_tool_names, registered_tools, tool_spec


def test_registry_boundary_returns_the_live_framework_registry() -> None:
    tools = registered_tools()
    assert tools is REGISTERED_TOOLS
    assert tools is mcp_server.TOOLS
    assert registered_tool_names() == frozenset(tools)
    assert tool_spec("read") is tools["read"]
    assert tool_spec("definitely-not-a-tool") is None


def test_non_dispatch_consumers_do_not_reach_into_mcp_server_registry() -> None:
    repo = Path(__file__).resolve().parents[2]
    paths = (
        repo / "src/lemoncrow/gateway/cli/runtime.py",
        repo / "src/lemoncrow/gateway/cli/commands/tools.py",
        repo / "src/lemoncrow/core/service/api.py",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "from lemoncrow.gateway.adapters.mcp_server import TOOLS" not in text, path
        if path.name == "tools.py":
            assert "mcp_server" not in text, path


def test_registry_state_does_not_depend_on_mcp_framework() -> None:
    repo = Path(__file__).resolve().parents[2]
    registry = (repo / "src/lemoncrow/gateway/tools/registry.py").read_text(encoding="utf-8")
    state = (repo / "src/lemoncrow/gateway/tool_registry_state.py").read_text(encoding="utf-8")
    assert "adapters.mcp.framework" not in registry
    assert "lemoncrow.gateway.adapters" not in state


def test_cli_and_sdk_do_not_import_mcp_server_for_tool_calls() -> None:
    repo = Path(__file__).resolve().parents[2]
    for rel in ("src/lemoncrow/gateway/cli/runtime.py", "src/lemoncrow/gateway/sdk/mcp.py"):
        text = (repo / rel).read_text(encoding="utf-8")
        assert "adapters.mcp_server" not in text, rel
        assert "adapters import mcp_server" not in text, rel


def test_call_registered_tool_uses_live_handler(monkeypatch) -> None:
    spec = tool_spec("read")
    assert spec is not None
    monkeypatch.setitem(spec, "handler", lambda args: {"echo": args})
    assert call_registered_tool("read", {"path": "x.py"}) == {"echo": {"path": "x.py"}}
