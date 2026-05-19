from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.gateway.adapters.mcp_server import (
    tool_code,
    tool_smart_edit,
    tool_smart_read,
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


def test_tool_code_search_accepts_semantic_modes_additively(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "auth.py").write_text(
        "def issue_access_token(user_id: str) -> str:\n"
        "    \"\"\"Create a login session token for an authenticated user.\"\"\"\n"
        "    session_token = f'session:{user_id}'\n"
        "    return session_token\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "audit.py").write_text(
        "def create_login_history_for_authenticated_user(user_id: str) -> dict[str, str]:\n"
        "    \"\"\"Record login history entries for audit review.\"\"\"\n"
        "    return {'user_id': user_id}\n",
        encoding="utf-8",
    )

    semantic = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "create login token for authenticated user",
            "mode": "semantic",
            "budget_tokens": 4000,
        }
    )
    hybrid_auto = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "create login token for authenticated user",
            "mode": "auto",
            "budget_tokens": 4000,
        }
    )
    exact_auto = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "issue_access_token",
            "mode": "auto",
            "budget_tokens": 4000,
        }
    )

    assert semantic["mode"] == "semantic"
    assert semantic["items"][0]["symbol_name"] == "issue_access_token"
    assert hybrid_auto["mode"] == "hybrid"
    assert hybrid_auto["items"][0]["symbol_name"] == "issue_access_token"
    assert exact_auto["mode"] == "lexical"
    assert exact_auto["items"][0]["symbol_name"] == "issue_access_token"


def test_tool_code_pattern_requires_pattern(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pattern is required for code pattern"):
        tool_code({"op": "pattern", "repo_root": str(tmp_path), "dry_run": True})


def test_tool_code_usages_returns_grouped_references(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "checkout.py").write_text(
        "from src.orders import OrderService\n\n"
        "def checkout(items: list[int]) -> int:\n"
        "    return OrderService().calculate_total(items)\n",
        encoding="utf-8",
    )

    payload = tool_code({"op": "usages", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 4000})

    assert payload["target"]["qualified_name"] == "OrderService"
    assert payload["group_by"] == "file"
    assert "src/checkout.py" in payload["references"]
    assert payload["references"]["src/checkout.py"][0]["provenance"] == "treesitter"


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


def test_tool_code_usages_dispatches_to_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_usages.return_value = {
        "target": {"qualified_name": "OrderService"},
        "references": {"src/checkout.py": [{"file_path": "src/checkout.py", "line": 4, "column": 12, "provenance": "scip"}]},
        "group_by": "file",
        "cache_hit": False,
        "provenance": "scip",
        "tokens_saved": 10,
        "total_tokens": 80,
    }
    monkeypatch.setattr("atelier.gateway.adapters.mcp_server._code_context_engine", lambda repo_root=".": fake_engine)

    payload = tool_code({"op": "usages", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 220})

    assert payload["cache_hit"] is False
    assert payload["provenance"] == "scip"
    fake_engine.tool_usages.assert_called_once_with(
        query="OrderService",
        symbol_id=None,
        qualified_name=None,
        symbol_name=None,
        file_path=None,
        kind=None,
        language=None,
        file_glob=None,
        group_by="file",
        snippet_lines=3,
        limit=20,
        budget_tokens=220,
    )


def test_tool_code_cache_diagnostics_dispatch_to_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_cache_status.return_value = {
        "entry_count": 2,
        "entries_by_tool": {"code.search": 1, "code.symbol": 1},
        "repo_id": "repo",
        "index_version": 1,
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 0,
        "total_tokens": 42,
    }
    fake_engine.tool_cache_invalidate.return_value = {
        "invalidated_entries": 1,
        "entries_by_tool": {"code.search": 1},
        "scope": {"cache_tool": "search"},
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 0,
        "total_tokens": 40,
    }
    monkeypatch.setattr("atelier.gateway.adapters.mcp_server._code_context_engine", lambda repo_root=".": fake_engine)

    status = tool_code({"op": "cache_status", "repo_root": str(tmp_path), "budget_tokens": 220})
    invalidated = tool_code(
        {"op": "cache_invalidate", "repo_root": str(tmp_path), "cache_tool": "search", "budget_tokens": 220}
    )

    assert status["entry_count"] == 2
    assert invalidated["invalidated_entries"] == 1
    fake_engine.tool_cache_status.assert_called_once_with(budget_tokens=220)
    fake_engine.tool_cache_invalidate.assert_called_once_with(cache_tool="search", budget_tokens=220)


def test_tool_code_cache_diagnostics_hide_payloads_and_keep_other_ops_cached(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n",
        encoding="utf-8",
    )

    tool_code({"op": "search", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 4000})
    tool_code(
        {
            "op": "symbol",
            "repo_root": str(tmp_path),
            "qualified_name": "OrderService",
            "file_path": "src/orders.py",
            "budget_tokens": 4000,
        }
    )

    status = tool_code({"op": "cache_status", "repo_root": str(tmp_path), "budget_tokens": 4000})
    invalidated = tool_code(
        {"op": "cache_invalidate", "repo_root": str(tmp_path), "cache_tool": "search", "budget_tokens": 4000}
    )
    search_after = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 4000})
    symbol_after = tool_code(
        {
            "op": "symbol",
            "repo_root": str(tmp_path),
            "qualified_name": "OrderService",
            "file_path": "src/orders.py",
            "budget_tokens": 4000,
        }
    )

    assert status["entries_by_tool"] == {"code.search": 1, "code.symbol": 1}
    assert "payload_json" not in json.dumps(status, sort_keys=True)
    assert "items" not in status
    assert invalidated["entries_by_tool"] == {"code.search": 1}
    assert search_after["cache_hit"] is False
    assert symbol_after["cache_hit"] is True


def test_read_budget_safe_mode_is_smaller_than_expand_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "big_module.py"
    target.write_text(
        "\n".join(
            [
                "class OrderService:",
                "    def calculate_total(self, items: list[int]) -> int:",
                "        return sum(items)",
                "",
            ]
            + [f"def helper_{index}() -> int:\n    return {index}\n" for index in range(40)]
        ),
        encoding="utf-8",
    )

    default_payload = tool_smart_read({"path": str(target), "max_lines": 20})
    expanded_payload = tool_smart_read({"path": str(target), "expand": True})

    assert count_tokens(json.dumps(default_payload, sort_keys=True, default=str)) < count_tokens(
        json.dumps(expanded_payload, sort_keys=True, default=str)
    )
