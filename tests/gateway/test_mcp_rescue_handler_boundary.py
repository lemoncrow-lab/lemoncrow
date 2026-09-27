from __future__ import annotations

import inspect
from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_rescue


def test_mcp_server_reexports_extracted_rescue_handler() -> None:
    assert mcp_server.tool_rescue_failure is tools_rescue.tool_rescue_failure
    assert "def tool_rescue_failure(" not in inspect.getsource(mcp_server)


def test_rescue_hooks_are_late_bound(monkeypatch) -> None:
    marker = object()
    monkeypatch.setattr(mcp_server, "_runtime", lambda: marker)
    hooks = mcp_server._rescue_handler_hooks()
    assert hooks.runtime() is marker


def test_rescue_module_does_not_import_mcp_server() -> None:
    source = Path(tools_rescue.__file__).read_text(encoding="utf-8")
    assert "mcp_server" not in source
