from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from atelier.gateway.adapters.mcp_server import (
    tool_code,
    tool_smart_edit,
    tool_smart_search,
    tool_sql,
)


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


def test_tool_code_search_returns_cache_hit_field(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n",
        encoding="utf-8",
    )

    first = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 4000})
    second = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 4000})

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert "tokens_saved" in first
    assert first["provenance"] == "local"
    assert second["provenance"] == "cached"
    assert all("snippet" not in item for item in first["items"])


def test_tool_code_search_invalidates_cache_after_reindex(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n",
        encoding="utf-8",
    )

    _ = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 4000})
    cached = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 4000})
    indexed = tool_code({"op": "index", "repo_root": str(tmp_path), "budget_tokens": 4000})
    fresh = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 4000})

    assert cached["cache_hit"] is True
    assert indexed["index_version"] >= 2
    assert fresh["cache_hit"] is False
    assert fresh["provenance"] == "local"


def test_tool_code_search_respects_budget_after_wrapper_metadata(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    lines = [f"def func_{index}() -> int:\n    return {index}\n" for index in range(3)]
    (tmp_path / "src" / "big.py").write_text("\n".join(lines), encoding="utf-8")

    payload = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "func", "budget_tokens": 260})

    assert payload["total_tokens"] <= 260


def test_tool_code_search_accepts_hardened_params(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_orders.py").write_text(
        "from src.orders import OrderService\n",
        encoding="utf-8",
    )

    payload = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "OrderService",
            "snippet": "head",
            "snippet_lines": 2,
            "file_glob": "src/*.py",
            "scope": "repo",
            "budget_tokens": 4000,
        }
    )

    assert payload["provenance"] == "local"
    assert payload["provenance_breakdown"] == {"local": len(payload["items"])}
    assert payload["items"][0]["file_path"] == "src/orders.py"
    assert payload["items"][0]["snippet"] == "class OrderService:\n    def calculate_total(self, items: list[int]) -> int:"


def test_tool_code_pattern_requires_pattern(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pattern is required for code pattern"):
        tool_code({"op": "pattern", "repo_root": str(tmp_path), "dry_run": True})


def test_tool_code_pattern_dispatches_to_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_pattern.return_value = {
        "matches": [{"file_path": "src/app.py", "line": 1, "column": 0, "captures": {"URL": "url"}}],
        "cache_hit": False,
        "provenance": "ast-grep",
        "tokens_saved": 10,
        "total_tokens": 100,
    }
    monkeypatch.setattr("atelier.gateway.adapters.mcp_server._code_context_engine", lambda repo_root=".": fake_engine)

    payload = tool_code(
        {
            "op": "pattern",
            "repo_root": str(tmp_path),
            "pattern": "requests.get($URL)",
            "dry_run": True,
            "budget_tokens": 220,
        }
    )

    assert payload["cache_hit"] is False
    assert payload["provenance"] == "ast-grep"
    fake_engine.tool_pattern.assert_called_once_with(
        pattern="requests.get($URL)",
        rewrite=None,
        language=None,
        file_glob=None,
        dry_run=True,
        limit=20,
        budget_tokens=220,
    )
