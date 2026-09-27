from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import bash
from lemoncrow.gateway.cli import runtime
from lemoncrow.gateway.tools import rendering


def test_mcp_wrapper_delegates_to_canonical_renderer() -> None:
    source = inspect.getsource(mcp_server.render_tool_result_text)
    assert "_canonical_render_tool_result_text" in source
    assert "_BASH_IDLE_GRACE_S" in source


def test_bash_adapter_wrapper_delegates_to_canonical_renderer() -> None:
    source = inspect.getsource(bash._render_bash_text)
    assert "render_bash_text" in source
    assert "_BASH_IDLE_GRACE_S" in source


def test_cli_renderer_no_longer_imports_mcp_server_renderer() -> None:
    source = inspect.getsource(runtime._render_tool_result)
    assert "mcp_server import render_tool_result_text" not in source
    assert runtime.render_tool_result_text is rendering.render_tool_result_text


def test_canonical_bash_renderer_respects_explicit_grace() -> None:
    payload = {"status": "running", "session_id": "s1", "idle_return": True}
    assert "~3s" in rendering.render_bash_text(payload, idle_grace_s=3.9)


def test_canonical_renderer_uses_threadlocal_code_rendering(monkeypatch) -> None:
    rendering.tool_call_rendered_text.value = "canonical-code-view"
    try:
        assert rendering.render_tool_result_text("node", {"ignored": True}) == "canonical-code-view"
    finally:
        rendering.tool_call_rendered_text.value = None
