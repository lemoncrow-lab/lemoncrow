from __future__ import annotations

import inspect
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_search


def test_mcp_server_reexports_extracted_search_handler_and_scope_helper() -> None:
    assert mcp_server.tool_smart_search is tools_search.tool_smart_search
    assert mcp_server.tool_code_search is tools_search.tool_code_search
    assert mcp_server.tool_grep is tools_search.tool_grep
    assert mcp_server._scope_search_matches_to_range is tools_search.scope_search_matches_to_range
    source = inspect.getsource(mcp_server)
    assert "def tool_smart_search(" not in source
    assert "def _scope_search_matches_to_range(" not in source


def test_search_hooks_are_late_bound(monkeypatch, tmp_path) -> None:
    marker = object()

    def fake_engine(_root: str):
        return marker

    monkeypatch.setattr(mcp_server, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_server, "_code_context_engine", fake_engine)
    hooks = mcp_server._search_handler_hooks()
    assert hooks.workspace_root() == tmp_path
    assert hooks.code_context_engine(str(tmp_path)) is marker


def test_search_module_does_not_import_mcp_server() -> None:
    source = Path(tools_search.__file__).read_text(encoding="utf-8")
    assert "mcp_server" not in source


def test_search_hooks_late_bind_grep_and_code_search(monkeypatch) -> None:
    def fake_grep(**_kwargs):
        return {"ok": True}

    def fake_repeat(_query: str) -> bool:
        return True

    monkeypatch.setattr(mcp_server, "_run_native_grep", fake_grep)
    monkeypatch.setattr(mcp_server, "_check_repeat_query", fake_repeat)
    hooks = mcp_server._search_handler_hooks()
    assert hooks.run_native_grep is fake_grep
    assert hooks.check_repeat_query is fake_repeat


@pytest.mark.parametrize("force", [False, True])
def test_code_search_never_blanks_similar_queries(monkeypatch, tmp_path, force) -> None:
    """Independent forced calls must execute even after a similar session query."""
    monkeypatch.setattr(mcp_server, "_RECENT_CODE_SEARCH_QUERIES", OrderedDict())
    assert not mcp_server._check_repeat_query("auth token refresh")
    payload = {
        "exact_match": True,
        "files": [
            {
                "path": "auth.py",
                "source_sections": [
                    {
                        "qualified_name": "refresh_token",
                        "line": 1,
                        "end_line": 2,
                        "content": "def refresh_token():\n    return 'refreshed'\n",
                    }
                ],
            }
        ],
    }
    engine = Mock()
    engine.tool_explore.return_value = payload
    monkeypatch.setattr(mcp_server, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_server, "_code_context_engine", lambda _root: engine)
    monkeypatch.setattr(mcp_server, "_workspace_code_router", lambda _root: SimpleNamespace(is_configured=False))
    monkeypatch.setattr(mcp_server, "_resolve_query_as_existing_file", lambda *_args: None)
    monkeypatch.setattr(mcp_server, "_attach_code_search_savings", lambda result, _root: result)

    handler = mcp_server.TOOLS["code_search"]["handler"]
    for _ in range(2):
        result = handler({"query": "auth token refresh handler", "max_files": 10, "force": force})
        assert [entry["path"] for entry in result["files"]] == ["auth.py"]
    assert engine.tool_explore.call_count == 2
