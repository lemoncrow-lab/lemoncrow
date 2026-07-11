from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lemoncrow.gateway.adapters import mcp_server
from tests.helpers import init_store_at


@pytest.fixture()
def mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    mcp_server._current_ledger = None
    mcp_server._realtime_ctx = None
    mcp_server._remote_client = None
    return tmp_path


def test_web_fetch_renders_content_only(mcp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ = mcp_env

    def fake_fetch_url(
        url: str,
        *,
        output_format: str,
        max_chars: int,
        timeout_s: float,
        include_meta: bool,
        query: str | None = None,
        summary: bool = False,
    ) -> dict[str, Any]:
        _ = (url, output_format, max_chars, timeout_s, include_meta, query, summary)
        return {"content": "# Hello\n\nWorld", "format": "markdown", "tokens_saved": 12}

    monkeypatch.setattr("lemoncrow.core.capabilities.web_fetch.fetch_url", fake_fetch_url)
    monkeypatch.setattr(mcp_server, "_append_workspace_savings", lambda *args, **kwargs: None)

    response = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "web_fetch", "arguments": {"url": "https://example.com"}},
        }
    )
    assert response is not None
    content_item = response["result"]["content"][0]
    assert content_item["type"] == "text"
    assert content_item["text"] == "# Hello\n\nWorld"
    assert content_item["saved"] == {"tokens": 12, "calls": 0}


def test_web_fetch_listed_in_tool_surface(mcp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ = mcp_env

    response = mcp_server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response is not None
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert "web_fetch" in names
