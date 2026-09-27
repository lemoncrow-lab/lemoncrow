from __future__ import annotations

from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_trace
from lemoncrow.gateway.tools.registry import tool_spec


def test_trace_handler_is_registered_from_extracted_module() -> None:
    spec = tool_spec("trace")
    assert spec is not None
    assert spec["handler"] is tools_trace.tool_record_trace
    assert mcp_server.tool_record_trace is tools_trace.tool_record_trace


def test_mcp_server_no_longer_defines_trace_registration() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/lemoncrow/gateway/adapters/mcp_server.py").read_text(encoding="utf-8")
    assert '@mcp_tool(name="trace")' not in source
    assert "def tool_record_trace(" not in source


def test_trace_hooks_late_bind_runtime_and_limits(monkeypatch) -> None:
    marker = object()

    def fake_runtime():
        return marker

    monkeypatch.setattr(mcp_server, "_runtime", fake_runtime)
    monkeypatch.setattr(mcp_server, "_MAX_TRACE_FILES", 7)
    monkeypatch.setattr(mcp_server, "_MAX_TRACE_FILE_BYTES", 1234)
    hooks = mcp_server._trace_handler_hooks()
    assert hooks.runtime is fake_runtime
    assert hooks.runtime() is marker
    assert hooks.max_trace_files == 7
    assert hooks.max_trace_file_bytes == 1234
