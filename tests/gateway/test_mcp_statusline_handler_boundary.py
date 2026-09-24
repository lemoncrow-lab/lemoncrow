from __future__ import annotations

import inspect
from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_statusline


def test_mcp_server_reexports_extracted_statusline_handler() -> None:
    assert mcp_server.tool_statusline_segment is tools_statusline.tool_statusline_segment
    source = inspect.getsource(mcp_server)
    assert "def tool_statusline_segment(" not in source


def test_statusline_handler_hooks_are_late_bound(tmp_path, monkeypatch) -> None:
    root = tmp_path / ".lemoncrow"
    sidecar = root / "sessions" / "session-1" / "savings.jsonl"
    monkeypatch.setattr(mcp_server, "_lemoncrow_root", lambda: root)
    monkeypatch.setattr(mcp_server, "_get_host_session_sidecar_path", lambda: sidecar)
    hooks = mcp_server._statusline_handler_hooks()
    assert hooks.lemoncrow_root() == root
    assert hooks.session_sidecar() == sidecar


def test_statusline_module_does_not_import_mcp_server() -> None:
    source = Path(tools_statusline.__file__).read_text(encoding="utf-8")
    assert "mcp_server" not in source
