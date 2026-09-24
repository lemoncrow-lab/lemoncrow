from __future__ import annotations

import inspect
from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_compact


def test_mcp_server_reexports_extracted_compact_handler() -> None:
    assert mcp_server.tool_compact is tools_compact.tool_compact
    assert "def tool_compact(" not in inspect.getsource(mcp_server)


def test_compact_handler_hooks_are_late_bound(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_compress_context(*, session_id: str | None = None):
        seen["session_id"] = session_id
        return {"prompt_block": "ok"}

    monkeypatch.setattr(mcp_server, "_compress_context", fake_compress_context)
    hooks = mcp_server._compact_handler_hooks()
    assert hooks.compress_context(session_id="s1") == {"prompt_block": "ok"}
    assert seen == {"session_id": "s1"}


def test_compact_handler_preserves_consolidate_marker(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_server,
        "_compress_context",
        lambda *, session_id=None: {"prompt_block": "state", "session_id": session_id},
    )
    assert tools_compact.tool_compact({"op": "consolidate", "session_id": "s1"})["op"] == "consolidate"


def test_compact_module_does_not_import_mcp_server() -> None:
    source = Path(tools_compact.__file__).read_text(encoding="utf-8")
    assert "mcp_server" not in source
