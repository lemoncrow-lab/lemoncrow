from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.core.environment import HIDDEN_LLM_TOOLS
from atelier.gateway.adapters import mcp_server
from atelier.gateway.adapters.mcp_server import (
    TOOLS,
    tool_grep,
    tool_smart_edit,
    tool_smart_read,
    tool_smart_search,
    tool_sql,
)
from atelier.gateway.sdk.mcp import _LoopbackTransport


def _op_result(render_name: str, op_fn: Any, **kwargs: Any) -> Any:
    """Mirror _handle's render path for a direct _op_* call: returns rendered
    markdown when a code renderer applies, else the raw payload dict."""
    mcp_server._tool_call_rendered_text.value = None
    payload = op_fn(**kwargs)
    rendered = mcp_server.render_tool_result_text(render_name, payload)
    return rendered if rendered is not None else payload


def _preindex(repo_root: str | Path) -> None:
    """Explicitly index the repo for deterministic code-context tests.

    The gateway conftest disables the background autosync worker so tests that
    need a populated index build it explicitly via ``_op_index``.
    """
    mcp_server._op_index(repo_root=str(repo_root), force=True)


def test_symbols_removed_in_favor_of_grep() -> None:
    # The `symbols` tool was a redundant second face over _op_search and is
    # fully removed (no registry entry, no handler). Agents use `grep`
    # (semantic / relation modes) to find code by name and read definitions.
    # The _op_search engine survives: grep(semantic=True) routes through it.
    assert "symbols" not in TOOLS
    assert "symbols" not in HIDDEN_LLM_TOOLS
    assert not hasattr(mcp_server, "tool_symbols")
    assert "code" not in TOOLS
    transport = _LoopbackTransport()
    with pytest.raises(KeyError):
        transport.call_tool("code", {})


def test_relations_tool_routes_to_targeted_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    # `relations` is the single drill-in tool for a symbol's call-graph relation:
    # kind=callers|callees|usages|self routes to the matching _op_* with the parsed
    # symbol, returning that op's focused payload verbatim. (grep shows the COUNTS
    # inline; relations expands one count into the list.)
    seen: dict[str, dict[str, Any]] = {}

    def _rec(name: str) -> Any:
        def _fn(**kwargs: Any) -> dict[str, Any]:
            seen[name] = kwargs
            return {"relation": name}

        return _fn

    monkeypatch.setattr(mcp_server, "_op_callers", _rec("callers"))
    monkeypatch.setattr(mcp_server, "_op_callees", _rec("callees"))
    monkeypatch.setattr(mcp_server, "_op_usages", _rec("usages"))
    monkeypatch.setattr(mcp_server, "_op_node", _rec("self"))

    assert mcp_server.tool_relations({"kind": "callers", "symbol": "OrderService", "depth": 2})["relation"] == "callers"
    assert seen["callers"]["symbol_name"] == "OrderService"
    assert seen["callers"]["depth"] == 2
    assert mcp_server.tool_relations({"kind": "callees", "symbol": "OrderService", "depth": 2})["relation"] == "callees"
    assert seen["callees"]["symbol_name"] == "OrderService"
    assert seen["callees"]["depth"] == 2
    assert mcp_server.tool_relations({"kind": "usages", "symbol": "OrderService"})["relation"] == "usages"
    assert seen["usages"]["symbol_name"] == "OrderService"
    assert mcp_server.tool_relations({"kind": "self", "symbol": "OrderService"})["relation"] == "self"
    # An unknown kind is rejected.
    with pytest.raises(ValueError):
        mcp_server.tool_relations({"kind": "bogus", "symbol": "OrderService"})


def test_explore_is_primary_grep_relations_hidden() -> None:
    # Single-primary retrieval surface: `explore` (ranked source + call-graph
    # relations + blast-radius in one call) is the advertised retrieval tool.
    # `grep` and `relations` stay registered and callable (escape hatch / drill-in
    # / internal routing) but are HIDDEN so the agent leads with `explore`.
    # `callers`/`callees`/`usages`/`node` remain folded away entirely; `search`
    # stays registered-but-hidden (semantic, surfaced once embeddings are wired).
    for name in ("callers", "callees", "usages", "node"):
        assert name not in TOOLS
        assert name not in HIDDEN_LLM_TOOLS
    assert "code_search" in TOOLS
    assert "code_search" not in HIDDEN_LLM_TOOLS
    for name in ("grep", "relations", "search"):
        assert name in TOOLS
        assert name in HIDDEN_LLM_TOOLS


def test_mcp_grep_native_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")

    # Default mode is `content`: matched lines + context as content blocks, not a
    # ranked-file-map payload.
    result = tool_grep({"content_regex": "needle", "file_glob_patterns": ["*.py"]})
    assert "needle" in result["content"][0]["text"]
    assert "_meta" not in result

    # The ranked file map remains available as the explicit `map` mode.
    ranked = tool_grep({"content_regex": "needle", "file_glob_patterns": ["*.py"], "mode": "map"})
    assert ranked["matches"]


def test_mcp_search_adds_backend_metadata_for_large_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from atelier.core.capabilities.tool_supervision import smart_search as smart_search_mod
    from atelier.core.capabilities.tool_supervision.search_read import FileMatch, SearchReadResult
    from atelier.infra.code_intel.zoekt.adapter import ZoektBackendHealth

    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_ZOEKT_LOC_THRESHOLD", "20")
    src = tmp_path / "src"
    src.mkdir()
    for index in range(24):
        (src / f"module_{index}.py").write_text(
            "".join(f"def item_{index}_{line}() -> str: return 'needle token {index}'\n" for line in range(24)),
            encoding="utf-8",
        )

    fake_supervisor = MagicMock()
    fake_supervisor.should_route.return_value = True
    fake_supervisor.health.return_value = ZoektBackendHealth(
        ok=True, backend="zoekt", binary_path="/usr/bin/docker", index_age_seconds=5
    )
    fake_supervisor.search.return_value = SearchReadResult(
        matches=[FileMatch(path=str(src / "module_0.py"), lang="python", snippets=[], outline=None, tokens=10)],
        total_tokens=10,
        tokens_saved_vs_naive=0,
        cache_hit=False,
        backend="zoekt",
        index_age_seconds=5,
    )
    monkeypatch.setattr(smart_search_mod, "get_zoekt_supervisor", lambda _root: fake_supervisor)

    result = tool_smart_search(
        {
            "query": "needle token",
            "path": str(tmp_path),
            "budget_tokens": 4000,
            "include_meta": True,
        }
    )

    assert result["backend"] == "zoekt"
    assert isinstance(result["index_age_seconds"], int)
    assert "matches" in result
    assert "total_tokens" in result
    assert result["mode"] == "chunks"
    assert result["discovery"] == {"tool": "search", "mode": "chunks"}
    assert result["handoff"]["read"] == {"tool": "read"}
    assert result["handoff"]["context"]["tool"] == "context"
    assert result["handoff"]["relations"]["tool"] == "grep"


def test_search_tool_returns_search_first_handoffs_without_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sample.py"
    target.write_text("class Needle:\n    pass\n", encoding="utf-8")

    result = tool_smart_search({"query": "Needle", "path": str(tmp_path), "budget_tokens": 4000})

    assert result["mode"] == "chunks"
    assert result["discovery"] == {"tool": "search", "mode": "chunks"}
    assert result["calls_saved"] >= 0
    assert result["matches"]
    assert result["matches"][0]["follow_up"]["read"]["tool"] == "read"
    assert result["matches"][0]["follow_up"]["context"]["tool"] == "context"
    assert result["handoff"]["memory"] == {
        "tool": "context",
        "mode": "procedures",
        "task": "Needle",
        "files": [match["path"] for match in result["matches"]],
        "recall": True,
    }
    assert "backend" not in result
    assert "cache_hit" not in result
    assert "total_tokens" not in result


def test_search_tool_uses_cached_code_index_before_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "src" / "commands.py"
    target.parent.mkdir()
    target.write_text("def configure_rate_limits() -> None:\n    pass\n", encoding="utf-8")
    fake_engine = MagicMock()
    fake_engine.tool_search.return_value = {
        "items": [
            {
                "file_path": "src/commands.py",
                "language": "python",
                "start_line": 1,
                "end_line": 2,
                "snippet": "def configure_rate_limits() -> None:\n    pass",
            }
        ],
        "total_tokens": 20,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda _repo_root=".": fake_engine,
    )

    result = tool_smart_search(
        {
            "query": "configure command execution rate limits",
            "path": "src",
            "budget_tokens": 4000,
            "include_meta": True,
        }
    )

    assert result["backend"] == "code_index"
    assert result["fallback"]["strategy"] == "indexed_hybrid"
    fake_engine.tool_search.assert_called_once_with(
        "configure command execution rate limits",
        limit=40,
        mode="hybrid",
        intent="auto",
        snippet="head",
        snippet_lines=12,
        file_glob="src/**",
        budget_tokens=4000,
    )


def test_explore_is_primary_search_and_relations_hidden() -> None:
    # `explore` is the advertised primary retrieval tool (ranked source + relations
    # in one call). `search` and `relations` stay registered but hidden.
    assert "code_search" in TOOLS
    assert "code_search" not in HIDDEN_LLM_TOOLS
    assert hasattr(mcp_server, "tool_code_search")
    assert "search" in TOOLS
    assert "search" in HIDDEN_LLM_TOOLS
    assert "relations" in HIDDEN_LLM_TOOLS
    assert hasattr(mcp_server, "tool_smart_search")
    # `relations` is the single drill-in tool: just `symbol` + `kind`.
    rel_props = TOOLS["relations"]["inputSchema"]["properties"]
    assert "symbol" in rel_props
    assert "kind" in rel_props
    # grep stays a lean regex tool -- no relation/symbol/seed_files params, and a
    # short output-shape `mode` enum.
    grep_props = TOOLS["grep"]["inputSchema"]["properties"]
    assert "relation" not in grep_props
    assert "symbol" not in grep_props
    assert "seed_files" not in grep_props
    mode_desc = grep_props["mode"]["description"]
    for mode_name in ("with_content", "ranked_map", "paths_only", "count_only"):
        assert mode_name in mode_desc, mode_desc
    assert ":Lx-Ly" in grep_props["path"]["description"]


def test_grep_tool_schema_covers_native_contract() -> None:
    grep_tool = TOOLS["grep"]
    properties = grep_tool["inputSchema"]["properties"]

    assert "regex" in grep_tool["description"].lower()
    # grep advertises the inline call-graph counts on definition matches.
    assert "counts" in grep_tool["description"].lower()
    assert "path" in properties
    assert "file_path" not in properties
    assert "summarize" in properties["summary"]["description"].lower()
    assert "max_line_length" not in properties


def test_grep_tool_accepts_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sample.py"
    target.write_text("needle\\n", encoding="utf-8")

    result = tool_grep({"path": str(target), "content_regex": "needle", "include_meta": True})

    assert result["_meta"]["fileMatchCount"] == 1


def test_grep_modes_are_output_shapes_only() -> None:
    # grep's `mode` is purely about output SHAPE now (with_content/ranked_map/
    # paths_only/count_only, self-documenting names per fe520724; short aliases
    # still accepted). The search-tool leftovers -- symbol-locate and repo-map
    # (seed_files) -- were dropped from grep; ranked_map means the ranked file
    # map, not a repo map.
    grep_props = TOOLS["grep"]["inputSchema"]["properties"]
    assert "mode" in grep_props
    mode_desc = grep_props["mode"]["description"]
    for mode_name in ("with_content", "ranked_map", "paths_only", "count_only"):
        assert mode_name in mode_desc, mode_desc
    assert "symbol" not in mode_desc.split(":")[0]
    assert "seed_files" not in grep_props


def test_mcp_edit_rich_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    result = tool_smart_edit({"edits": [{"file_path": "a.txt", "old_string": "hello", "new_string": "hi"}]})

    # Clean exact edit echoes the minimal applied range; change confirmed on disk.
    assert "failed" not in result
    assert result.get("applied") == ["a.txt:1"]
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
    assert result["results"][0]["rows"] == [[1]]


def test_tool_code_search_returns_cache_hit_field(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n    def calculate_total(self, items: list[int]) -> int:\n        return sum(items)\n",
        encoding="utf-8",
    )

    first = mcp_server._op_search(repo_root=str(tmp_path), query="OrderService", budget_tokens=4000)
    second = mcp_server._op_search(repo_root=str(tmp_path), query="OrderService", budget_tokens=4000)

    assert "provenance" not in first
    assert "cache_hit" not in first
    # Cached response is byte-identical on the LLM surface.
    assert second["items"] == first["items"]
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

    payload = mcp_server._op_search(repo_root=str(tmp_path), query="OrderService", budget_tokens=220)

    assert "provenance" not in payload
    assert "backend" not in payload
    fake_engine.tool_search.assert_called_once_with(
        "OrderService",
        limit=20,
        mode="auto",
        intent="auto",
        kind=None,
        language=None,
        seed_files=None,
        snippet="none",
        snippet_lines=8,
        file_glob=None,
        scope="repo",
        budget_tokens=220,
    )


def test_tool_code_search_can_attach_compact_rendered_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_search.return_value = {
        "items": [
            {
                "symbol_name": "OrderService",
                "qualified_name": "orders.OrderService",
                "kind": "class",
                "file_path": "src/orders.py",
                "start_line": 1,
                "source": "class OrderService:\n    ...",
                "snippet": "class OrderService:",
                "provenance": "local",
            }
        ],
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 0,
        "total_tokens": 100,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = mcp_server._op_search(
        repo_root=str(tmp_path),
        query="OrderService",
        budget_tokens=220,
        render_compact=True,
    )

    # Rendered markdown travels on the response-body channel only (the
    # thread-local), never duplicated into the JSON result.
    assert "rendered" not in payload
    rendered = mcp_server._tool_call_rendered_text.value
    # Grouped by file: path header once, then an indented per-hit line.
    assert "- src/orders.py" in rendered
    assert "  - 1 — orders.OrderService [class]" in rendered
    assert "class OrderService" not in rendered


def test_explore_is_primary_relations_is_hidden_drill_in() -> None:
    # `explore` is the advertised primary retrieval tool. `relations` stays a
    # registered-but-hidden drill-in (kind=callers|callees|usages|self).
    assert "code_search" in mcp_server.TOOLS
    assert hasattr(mcp_server, "tool_code_search")
    assert mcp_server.TOOLS["relations"]["inputSchema"]["properties"]["kind"]["type"] == "string"
    assert hasattr(mcp_server, "_op_explore")


@pytest.mark.slow
def test_tool_code_search_invalidates_cache_after_reindex(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n    def calculate_total(self, items: list[int]) -> int:\n        return sum(items)\n",
        encoding="utf-8",
    )

    _ = mcp_server._op_search(repo_root=str(tmp_path), query="OrderService", budget_tokens=4000)
    cached = mcp_server._op_search(repo_root=str(tmp_path), query="OrderService", budget_tokens=4000)
    # force=True guarantees a version bump regardless of autosync timing.
    indexed = mcp_server._op_index(repo_root=str(tmp_path), budget_tokens=4000, force=True)
    fresh = mcp_server._op_search(repo_root=str(tmp_path), query="OrderService", budget_tokens=4000)

    assert "provenance" not in cached
    assert cached["items"] == fresh["items"]
    assert indexed["index_version"] >= 2
    assert "provenance" not in fresh


def test_tool_code_search_respects_budget_after_wrapper_metadata(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    lines = [f"def func_{index}() -> int:\n    return {index}\n" for index in range(3)]
    (tmp_path / "src" / "big.py").write_text("\n".join(lines), encoding="utf-8")

    payload = mcp_server._op_search(repo_root=str(tmp_path), query="func", budget_tokens=260)

    assert "items" in payload


def test_tool_code_search_accepts_hardened_params(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n    def calculate_total(self, items: list[int]) -> int:\n        return sum(items)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_orders.py").write_text(
        "from src.orders import OrderService\n",
        encoding="utf-8",
    )
    _preindex(tmp_path)

    payload = mcp_server._op_search(
        repo_root=str(tmp_path),
        query="OrderService",
        snippet="head",
        snippet_lines=2,
        file_glob="src/*.py",
        scope="repo",
        budget_tokens=4000,
    )

    assert "provenance" not in payload
    assert "provenance_breakdown" not in payload
    assert payload["items"][0]["path"] == "src/orders.py"
    assert payload["items"][0]["signature"] == "class OrderService:"


def test_tool_code_search_semantic_unavailable_without_embedder(tmp_path: Path) -> None:
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
    _preindex(tmp_path)

    # Semantic search is opt-in: with no embedding backend configured (the default),
    # an explicit semantic request reports it is unavailable instead of contacting an
    # external LLM (ollama). It does not silently fall back to lexical.
    semantic = mcp_server._op_search(
        repo_root=str(tmp_path),
        query="create login token for authenticated user",
        mode="semantic",
        budget_tokens=4000,
    )
    assert semantic.get("semantic_available") is False
    assert semantic["items"] == []

    # Auto mode with an exact identifier still works via lexical search (no LLM).
    exact_auto = mcp_server._op_search(
        repo_root=str(tmp_path),
        query="issue_access_token",
        mode="auto",
        budget_tokens=4000,
    )
    exact_names = {item["name"] for item in exact_auto["items"]}
    assert "issue_access_token" in exact_names


def test_tool_code_pattern_requires_pattern(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pattern is required for code pattern"):
        mcp_server._op_pattern(repo_root=str(tmp_path), dry_run=True)


def test_tool_code_usages_returns_grouped_references(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n    def calculate_total(self, items: list[int]) -> int:\n        return sum(items)\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "checkout.py").write_text(
        "from src.orders import OrderService\n\n"
        "def checkout(items: list[int]) -> int:\n"
        "    return OrderService().calculate_total(items)\n",
        encoding="utf-8",
    )
    _preindex(tmp_path)

    payload = mcp_server._op_usages(repo_root=str(tmp_path), query="OrderService", budget_tokens=4000)

    target = payload["target"]
    assert (target.get("name") or target.get("symbol_name")) == "OrderService"
    assert "qualified_name" not in target  # identical to name — deduped
    assert payload["group_by"] == "file"
    assert "src/checkout.py" in payload["references"]
    assert "provenance" not in payload["references"]["src/checkout.py"][0]


def test_tool_code_call_graph_dispatches_to_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_callers.return_value = {
        "target": {"qualified_name": "beta"},
        "related": [{"qualified_name": "alpha", "provenance": "tree_sitter"}],
        "edges": [{"caller_symbol_id": "ts-alpha", "callee_symbol_id": "ts-beta", "depth": 1}],
        "data_status": "available",
        "snapshot": None,
        "cache_hit": False,
        "provenance": "tree_sitter",
        "tokens_saved": 10,
        "total_tokens": 100,
    }
    fake_engine.tool_callees.return_value = {
        "target": {"qualified_name": "handle"},
        "related": [{"qualified_name": "alpha", "provenance": "tree_sitter"}],
        "edges": [{"caller_symbol_id": "ts-handle", "callee_symbol_id": "ts-alpha", "depth": 1}],
        "data_status": "available",
        "snapshot": {"snapshot_id": "snap"},
        "cache_hit": False,
        "provenance": "tree_sitter",
        "tokens_saved": 10,
        "total_tokens": 100,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    callers = mcp_server._op_callers(repo_root=str(tmp_path), query="beta", budget_tokens=220)
    callees = mcp_server._op_callees(
        repo_root=str(tmp_path),
        query="handle",
        snapshot=True,
        budget_tokens=220,
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

    payload = mcp_server._op_pattern(
        repo_root=str(tmp_path),
        pattern="requests.get($URL)",
        dry_run=True,
        budget_tokens=220,
    )

    assert "provenance" not in payload
    fake_engine.tool_pattern.assert_called_once_with(
        pattern="requests.get($URL)",
        rewrite=None,
        language=None,
        file_glob=None,
        dry_run=True,
        limit=20,
        budget_tokens=220,
    )


def test_tool_code_explore_requires_query(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="query is required for code explore"):
        mcp_server._op_explore(repo_root=str(tmp_path), budget_tokens=220)


def test_tool_code_explore_dispatches_to_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_explore.return_value = {
        "query": "OrderService",
        "repo_id": "repo",
        "entry_points": [{"symbol_id": "s1", "symbol_name": "OrderService"}],
        "files": [{"file_path": "src/orders.py", "symbols": []}],
        "relationships": {"callers": [], "callees": [], "usages": []},
        "additional_relevant_files": [],
        "truncated": False,
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 0,
        "total_tokens": 100,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = mcp_server._op_explore(
        repo_root=str(tmp_path),
        query="OrderService",
        seed_files=["src/orders.py"],
        max_files=4,
        max_symbols=12,
        include_source=True,
        include_relationships=True,
        line_numbers=True,
        depth=2,
        budget_tokens=600,
    )

    assert payload["query"] == "OrderService"
    fake_engine.tool_explore.assert_called_once_with(
        query="OrderService",
        seed_files=["src/orders.py"],
        max_files=4,
        max_symbols=12,
        include_source=True,
        include_relationships=True,
        line_numbers=True,
        skeletonize=True,
        complete_families=None,
        depth=2,
        budget_tokens=600,
    )


def test_tool_code_callers_rendered_shape_excludes_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_callers.return_value = {
        "target": {"qualified_name": "beta", "file_path": "src/service.py", "start_line": 10},
        "related": [
            {
                "qualified_name": "checkout.place_order",
                "file_path": "src/checkout.py",
                "start_line": 24,
                "source": "def place_order(): ...",
            }
        ],
        "edges": [{"caller_symbol_id": "a", "callee_symbol_id": "b", "depth": 1}],
        "data_status": "available",
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 0,
        "total_tokens": 80,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = mcp_server._op_callers(
        repo_root=str(tmp_path),
        query="beta",
        budget_tokens=220,
        render_compact=True,
    )

    assert "rendered" not in payload
    rendered = mcp_server._tool_call_rendered_text.value
    # Grouped by file: path header once, then an indented per-hit line.
    assert "- src/checkout.py" in rendered
    assert "  - 24 — checkout.place_order" in rendered
    assert "def place_order" not in rendered


def test_tool_code_symbol_rendered_shape_includes_numbered_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_symbol.return_value = {
        "symbol_id": "sym-order-total",
        "qualified_name": "OrderService.calculate_total",
        "symbol_name": "calculate_total",
        "kind": "method",
        "signature": "def calculate_total(self, items: list[int]) -> int",
        "file_path": "src/orders.py",
        "start_line": 12,
        "end_line": 20,
        "source": "def calculate_total(self, items):\n    total = sum(items)\n    return total\n",
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 0,
        "total_tokens": 95,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = mcp_server._op_node(
        repo_root=str(tmp_path),
        qualified_name="OrderService.calculate_total",
        path="src/orders.py",
        budget_tokens=220,
        render_compact=True,
    )

    assert "rendered" not in payload
    rendered = mcp_server._tool_call_rendered_text.value
    assert "OrderService.calculate_total [method]" in rendered
    assert "src/orders.py:L12-L20" in rendered
    # Node now returns the symbol body inline, line-numbered from its start line,
    # so an agent can cite file:line and edit without a follow-up read.
    assert "12\tdef calculate_total(self, items):" in rendered
    assert "#### " in rendered  # header present
    assert "13\t    total = sum(items)" in rendered


def test_tool_code_index_rendered_shape_is_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_index.return_value = {
        "repo_id": "repo",
        "index_version": 7,
        "files_indexed": 3,
        "symbols_indexed": 8,
        "imports_indexed": 2,
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 0,
        "total_tokens": 60,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = mcp_server._op_index(repo_root=str(tmp_path), budget_tokens=220, render_compact=True)

    assert "rendered" not in payload
    rendered = mcp_server._tool_call_rendered_text.value
    assert "- counts: files=3, symbols=8, imports=2" in rendered
    fake_engine.tool_index.assert_called_once_with(
        include_globs=None, exclude_globs=None, force=False, budget_tokens=220
    )


def test_tool_code_cache_status_rendered_shape_is_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_cache_status.return_value = {
        "repo_id": "repo",
        "index_version": 7,
        "entry_count": 4,
        "entries_by_tool": {"code.search": 2, "code.symbol": 2},
        "total_bytes": 512,
        "max_bytes": 4096,
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 0,
        "total_tokens": 60,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = mcp_server._op_cache_status(
        repo_root=str(tmp_path),
        budget_tokens=220,
        render_compact=True,
    )

    assert "rendered" not in payload
    rendered = mcp_server._tool_call_rendered_text.value
    assert "- entries: 4" in rendered
    assert "- tools: code.search=2, code.symbol=2" in rendered
    fake_engine.tool_cache_status.assert_called_once_with(budget_tokens=220)


def test_tool_code_usages_dispatches_to_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_usages.return_value = {
        "target": {"qualified_name": "OrderService"},
        "references": {
            "src/checkout.py": [{"file_path": "src/checkout.py", "line": 4, "column": 12, "provenance": "tree_sitter"}]
        },
        "group_by": "file",
        "cache_hit": False,
        "provenance": "tree_sitter",
        "tokens_saved": 10,
        "total_tokens": 80,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = mcp_server._op_usages(repo_root=str(tmp_path), query="OrderService", budget_tokens=220)

    assert "provenance" not in payload
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

    status = mcp_server._op_cache_status(repo_root=str(tmp_path), budget_tokens=220)
    invalidated = mcp_server._op_cache_invalidate(
        repo_root=str(tmp_path),
        cache_tool="search",
        budget_tokens=220,
    )

    assert status["entry_count"] == 2
    assert invalidated["invalidated_entries"] == 1
    fake_engine.tool_cache_status.assert_called_once_with(budget_tokens=220)
    fake_engine.tool_cache_invalidate.assert_called_once_with(cache_tool="search", budget_tokens=220)


def test_tool_code_deleted_search_stays_on_additive_code_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine = MagicMock()
    fake_engine.db_path = tmp_path / "code_context.sqlite"
    fake_engine.db_path.touch()  # mark as indexed so bootstrap_note is not injected
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

    payload = mcp_server._op_search(
        repo_root=str(tmp_path),
        query="ModernCheckout",
        scope="deleted",
        since="2025-01-01",
        touched_by="history@example.com",
        budget_tokens=220,
    )

    assert sorted(payload.keys()) == ["items"]
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
    fake_engine.db_path = tmp_path / "code_context.sqlite"
    fake_engine.db_path.touch()  # mark as indexed so bootstrap_note is not injected
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

    blame = mcp_server._op_blame(repo_root=str(tmp_path), query="risk_score", budget_tokens=220)
    search = mcp_server._op_search(
        repo_root=str(tmp_path),
        query="OrderService",
        budget_tokens=220,
    )

    assert sorted(blame.keys()) == [
        "distinct_authors",
        "file_path",
        "freshness",
        "last_author",
        "last_commit_sha",
        "local_edits",
        "qualified_name",
        "symbol_name",
    ]
    assert "provenance" not in blame
    assert "provenance" not in search
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
        intent="auto",
        kind=None,
        language=None,
        seed_files=None,
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
        "class OrderService:\n    def calculate_total(self, items: list[int]) -> int:\n        return sum(items)\n",
        encoding="utf-8",
    )
    _preindex(tmp_path)

    mcp_server._op_search(repo_root=str(tmp_path), query="OrderService", budget_tokens=4000)
    mcp_server._op_node(
        repo_root=str(tmp_path),
        qualified_name="OrderService",
        path="src/orders.py",
        budget_tokens=4000,
    )

    status = mcp_server._op_cache_status(repo_root=str(tmp_path), budget_tokens=4000)
    invalidated = mcp_server._op_cache_invalidate(
        repo_root=str(tmp_path),
        cache_tool="search",
        budget_tokens=4000,
    )
    search_after = mcp_server._op_search(repo_root=str(tmp_path), query="OrderService", budget_tokens=4000)
    symbol_after = mcp_server._op_node(
        repo_root=str(tmp_path),
        qualified_name="OrderService",
        path="src/orders.py",
        budget_tokens=4000,
    )

    assert status["entries_by_tool"] == {"code.search": 1, "code.symbol": 1}
    assert "payload_json" not in json.dumps(status, sort_keys=True)
    assert "items" not in status
    assert invalidated["entries_by_tool"] == {"code.search": 1}
    assert "provenance" not in search_after
    assert "provenance" not in symbol_after


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
    expanded_payload = tool_smart_read({"path": str(target), "full": True})

    assert count_tokens(json.dumps(default_payload, sort_keys=True, default=str)) < count_tokens(
        json.dumps(expanded_payload, sort_keys=True, default=str)
    )
