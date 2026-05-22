from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.gateway.adapters.mcp_server import (
    TOOLS,
    tool_code,
    tool_grep,
    tool_smart_edit,
    tool_smart_read,
    tool_smart_search,
    tool_sql,
)


def test_mcp_grep_native_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")

    result = tool_grep({"content_regex": "needle", "file_glob_patterns": ["*.py"]})

    assert result["_meta"]["fileMatchCount"] == 1


skip_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is required for the managed Zoekt runtime"
)


@skip_docker
def test_mcp_search_adds_backend_metadata_for_large_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_ZOEKT_LOC_THRESHOLD", "20")
    monkeypatch.delenv("ATELIER_ZOEKT_BIN", raising=False)
    monkeypatch.delenv("ATELIER_ZOEKT_BIN_SHA256", raising=False)
    src = tmp_path / "src"
    src.mkdir()
    for index in range(24):
        (src / f"module_{index}.py").write_text(
            "".join(f"def item_{index}_{line}() -> str: return 'needle token {index}'\n" for line in range(24)),
            encoding="utf-8",
        )

    result = tool_smart_search({"query": "needle token", "file_path": str(tmp_path), "budget_tokens": 4000})

    assert result["backend"] == "zoekt"
    assert isinstance(result["index_age_seconds"], int)
    assert "matches" in result
    assert "total_tokens" in result


def test_search_tool_schema_prefers_file_path_and_documents_ranked_contract() -> None:
    search_tool = TOOLS["search"]
    properties = search_tool["inputSchema"]["properties"]

    assert "query" in search_tool["description"]
    assert "grep" in search_tool["description"]
    assert "file_path" in properties
    assert "path" not in properties
    assert "content_regex" not in properties
    assert "canonical search root" in properties["file_path"]["description"]
    assert "repo map" in properties["mode"]["description"].lower()
    assert "mode='map'" in properties["seed_files"]["description"]


def test_grep_tool_schema_covers_native_contract() -> None:
    grep_tool = TOOLS["grep"]
    properties = grep_tool["inputSchema"]["properties"]

    assert "regex" in grep_tool["description"].lower()
    assert "context lines" in grep_tool["description"].lower()
    assert "file_path" in properties
    assert "path" not in properties
    assert "timestamp from the previous result header" in properties["if_modified_since"]["description"].lower()
    assert "summarize file structure" in properties["summary"]["description"].lower()
    assert "truncate long rendered lines" in properties["max_line_length"]["description"].lower()


def test_grep_tool_accepts_legacy_path_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sample.py"
    target.write_text("needle\\n", encoding="utf-8")

    result = tool_grep({"path": str(target), "content_regex": "needle"})

    assert result["_meta"]["fileMatchCount"] == 1


def test_search_tool_map_mode_requires_seed_files() -> None:
    with pytest.raises(ValueError, match="seed_files is required when mode='map'"):
        tool_smart_search({"mode": "map"})


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


def test_tool_code_search_name_first_contract_stays_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_search.return_value = {
        "items": [{"symbol_name": "OrderService", "file_path": "src/orders.py", "provenance": "local"}],
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 8,
        "total_tokens": 90,
        "mode": "auto",
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 220})

    assert payload["provenance"] == "local"
    assert "backend" not in payload
    fake_engine.tool_search.assert_called_once_with(
        "OrderService",
        limit=20,
        mode="auto",
        kind=None,
        language=None,
        snippet="none",
        snippet_lines=8,
        file_glob=None,
        scope="repo",
        budget_tokens=220,
    )


def test_tool_code_schema_exposes_additive_repo_filter() -> None:
    properties = TOOLS["code"]["inputSchema"]["properties"]

    assert "repo" in properties


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
    assert (
        payload["items"][0]["snippet"] == "class OrderService:\n    def calculate_total(self, items: list[int]) -> int:"
    )


def test_tool_code_search_accepts_semantic_modes_additively(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "auth.py").write_text(
        "def issue_access_token(user_id: str) -> str:\n"
        '    """Create a login session token for an authenticated user."""\n'
        "    session_token = f'session:{user_id}'\n"
        "    return session_token\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "audit.py").write_text(
        "def create_login_history_for_authenticated_user(user_id: str) -> dict[str, str]:\n"
        '    """Record login history entries for audit review."""\n'
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


def test_tool_code_workspace_repo_filter_rejects_unsupported_ops(tmp_path: Path) -> None:
    billing_root = tmp_path.parent / "billing"
    billing_root.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".atelier").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".atelier" / "workspace.toml").write_text(
        "\n".join(
            [
                "[workspace]",
                'id = "fixture-workspace"',
                "",
                "[[workspace.repos]]",
                'name = "atelier"',
                'path = "."',
                "",
                "[[workspace.repos]]",
                'name = "billing"',
                'path = "../billing"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="repo filter is only supported for workspace search and symbol operations"):
        tool_code(
            {
                "op": "outline",
                "repo_root": str(tmp_path),
                "repo": "billing",
                "file_path": "src/config.py",
            }
        )


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


def test_tool_code_call_graph_dispatches_to_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_callers.return_value = {
        "target": {"qualified_name": "beta"},
        "related": [{"qualified_name": "alpha", "provenance": "scip"}],
        "edges": [{"caller_symbol_id": "scip-alpha", "callee_symbol_id": "scip-beta", "depth": 1}],
        "data_status": "available",
        "snapshot": None,
        "cache_hit": False,
        "provenance": "scip",
        "tokens_saved": 10,
        "total_tokens": 100,
    }
    fake_engine.tool_callees.return_value = {
        "target": {"qualified_name": "handle"},
        "related": [{"qualified_name": "alpha", "provenance": "scip"}],
        "edges": [{"caller_symbol_id": "scip-handle", "callee_symbol_id": "scip-alpha", "depth": 1}],
        "data_status": "available",
        "snapshot": {"snapshot_id": "snap"},
        "cache_hit": False,
        "provenance": "scip",
        "tokens_saved": 10,
        "total_tokens": 100,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    callers = tool_code({"op": "callers", "repo_root": str(tmp_path), "query": "beta", "budget_tokens": 220})
    callees = tool_code(
        {
            "op": "callees",
            "repo_root": str(tmp_path),
            "query": "handle",
            "snapshot": True,
            "budget_tokens": 220,
        }
    )

    assert callers["data_status"] == "available"
    assert callees["snapshot"]["snapshot_id"] == "snap"
    fake_engine.tool_callers.assert_called_once_with(
        query="beta",
        symbol_id=None,
        qualified_name=None,
        symbol_name=None,
        file_path=None,
        kind=None,
        language=None,
        depth=1,
        limit=20,
        snapshot=False,
        budget_tokens=220,
    )
    fake_engine.tool_callees.assert_called_once_with(
        query="handle",
        symbol_id=None,
        qualified_name=None,
        symbol_name=None,
        file_path=None,
        kind=None,
        language=None,
        depth=1,
        limit=20,
        snapshot=True,
        budget_tokens=220,
    )


def test_tool_code_pattern_dispatches_to_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_pattern.return_value = {
        "matches": [{"file_path": "src/app.py", "line": 1, "column": 0, "captures": {"URL": "url"}}],
        "cache_hit": False,
        "provenance": "ast-grep",
        "tokens_saved": 10,
        "total_tokens": 100,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

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
        "references": {
            "src/checkout.py": [{"file_path": "src/checkout.py", "line": 4, "column": 12, "provenance": "scip"}]
        },
        "group_by": "file",
        "cache_hit": False,
        "provenance": "scip",
        "tokens_saved": 10,
        "total_tokens": 80,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

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
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    status = tool_code({"op": "cache_status", "repo_root": str(tmp_path), "budget_tokens": 220})
    invalidated = tool_code(
        {
            "op": "cache_invalidate",
            "repo_root": str(tmp_path),
            "cache_tool": "search",
            "budget_tokens": 220,
        }
    )

    assert status["entry_count"] == 2
    assert invalidated["invalidated_entries"] == 1
    fake_engine.tool_cache_status.assert_called_once_with(budget_tokens=220)
    fake_engine.tool_cache_invalidate.assert_called_once_with(cache_tool="search", budget_tokens=220)


def test_tool_code_deleted_search_stays_on_additive_code_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_search.return_value = {
        "items": [
            {
                "symbol_name": "LegacyCheckout",
                "qualified_name": "LegacyCheckout",
                "file_path": "legacy.py",
                "kind": "historical",
                "signature": "LegacyCheckout",
                "start_line": 1,
                "end_line": 1,
                "language": "python",
                "provenance": "graveyard",
                "deleted_at": "2025-01-01T00:00:00Z",
                "deleted_at_sha": "abc123",
                "rename_target": "modern.py",
                "rename_note": "matched current public identity",
            }
        ],
        "cache_hit": False,
        "provenance": "graveyard",
        "provenance_breakdown": {"graveyard": 1},
        "tokens_saved": 10,
        "total_tokens": 140,
        "mode": "auto",
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "ModernCheckout",
            "scope": "deleted",
            "since": "2025-01-01",
            "touched_by": "history@example.com",
            "budget_tokens": 220,
        }
    )

    assert sorted(payload.keys()) == [
        "cache_hit",
        "items",
        "mode",
        "provenance",
        "provenance_breakdown",
        "tokens_saved",
        "total_tokens",
    ]
    assert payload["items"][0]["deleted_at_sha"] == "abc123"
    assert payload["items"][0]["rename_target"] == "modern.py"
    fake_engine.tool_search.assert_called_once_with(
        "ModernCheckout",
        limit=20,
        mode="auto",
        kind=None,
        language=None,
        snippet="none",
        snippet_lines=8,
        file_glob=None,
        scope="deleted",
        since="2025-01-01",
        touched_by="history@example.com",
        budget_tokens=220,
    )


def test_tool_code_blame_is_an_additive_extension_to_code_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_blame.return_value = {
        "symbol_name": "risk_score",
        "qualified_name": "risk_score",
        "file_path": "service.py",
        "freshness": "fresh",
        "last_author": "carol@example.com",
        "last_commit_sha": "abc123",
        "distinct_authors": 2,
        "local_edits": False,
        "cache_hit": False,
        "provenance": "blame",
        "tokens_saved": 10,
        "total_tokens": 120,
    }
    fake_engine.tool_search.return_value = {
        "items": [{"symbol_name": "OrderService", "file_path": "src/orders.py", "provenance": "local"}],
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 8,
        "total_tokens": 90,
        "mode": "auto",
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    blame = tool_code({"op": "blame", "repo_root": str(tmp_path), "query": "risk_score", "budget_tokens": 220})
    search = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "OrderService",
            "include_churn": False,
            "budget_tokens": 220,
        }
    )

    assert sorted(blame.keys()) == [
        "cache_hit",
        "distinct_authors",
        "file_path",
        "freshness",
        "last_author",
        "last_commit_sha",
        "local_edits",
        "provenance",
        "qualified_name",
        "symbol_name",
        "tokens_saved",
        "total_tokens",
    ]
    assert blame["provenance"] == "blame"
    assert search["provenance"] == "local"
    fake_engine.tool_blame.assert_called_once_with(
        query="risk_score",
        symbol_id=None,
        qualified_name=None,
        symbol_name=None,
        file_path=None,
        include_churn=True,
        budget_tokens=220,
    )
    fake_engine.tool_search.assert_called_once_with(
        "OrderService",
        limit=20,
        mode="auto",
        kind=None,
        language=None,
        snippet="none",
        snippet_lines=8,
        file_glob=None,
        scope="repo",
        budget_tokens=220,
    )


def test_tool_code_cache_diagnostics_hide_payloads_and_keep_other_ops_cached(
    tmp_path: Path,
) -> None:
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
        {
            "op": "cache_invalidate",
            "repo_root": str(tmp_path),
            "cache_tool": "search",
            "budget_tokens": 4000,
        }
    )
    search_after = tool_code(
        {"op": "search", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 4000}
    )
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

    default_payload = tool_smart_read({"file_path": str(target), "max_lines": 20})
    expanded_payload = tool_smart_read({"file_path": str(target), "expand": True})

    assert count_tokens(json.dumps(default_payload, sort_keys=True, default=str)) < count_tokens(
        json.dumps(expanded_payload, sort_keys=True, default=str)
    )
