from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from atelier.gateway.adapters.mcp_server import tool_smart_edit, tool_smart_search, tool_sql


def test_mcp_search_native_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")

    result = tool_smart_search({"content_regex": "needle", "file_glob_patterns": ["*.py"]})

    assert result["_meta"]["fileMatchCount"] == 1


def test_mcp_edit_rich_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    result = tool_smart_edit({"edits": [{"file_path": "a.txt", "old_string": "hello", "new_string": "hi"}]})

    assert result["failed"] == []
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hi\n"


def test_mcp_sql_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    db_path = tmp_path / "data.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items(id integer)")
    conn.execute("INSERT INTO items VALUES(1)")
    conn.commit()
    conn.close()

    result = tool_sql(
        {
            "action": "query",
            "connection_string": f"sqlite:///{db_path}",
            "sql": "SELECT * FROM items",
        }
    )

    assert result["isError"] is False
    assert result["results"][0]["rows"] == [{"id": 1}]
