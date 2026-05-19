from __future__ import annotations

import hashlib
import json
from pathlib import Path

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.core.capabilities.code_context.budget import BudgetPacker
from atelier.infra.code_intel.astgrep import PatternMatch, PatternSearchResult


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "def helper() -> OrderService:\n"
        "    return OrderService()\n",
        encoding="utf-8",
    )
    (root / "src" / "checkout.py").write_text(
        "from src.orders import OrderService\n\n"
        "def checkout(items: list[int]) -> int:\n"
        "    return OrderService().calculate_total(items)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_checkout.py").write_text(
        "from src.checkout import checkout\n\n" "def test_checkout() -> None:\n" "    assert checkout([1, 2]) == 3\n",
        encoding="utf-8",
    )


def _write_semantic_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "auth.py").write_text(
        "def issue_access_token(user_id: str) -> str:\n"
        "    \"\"\"Create a login session token for an authenticated user.\"\"\"\n"
        "    session_token = f'session:{user_id}'\n"
        "    return session_token\n"
        "\n"
        "def revoke_access_token(token: str) -> None:\n"
        "    \"\"\"Invalidate a session token after logout.\"\"\"\n"
        "    return None\n",
        encoding="utf-8",
    )
    (root / "src" / "audit.py").write_text(
        "def create_login_history_for_authenticated_user(user_id: str) -> dict[str, str]:\n"
        "    \"\"\"Record login history entries for audit review.\"\"\"\n"
        "    return {'user_id': user_id}\n",
        encoding="utf-8",
    )


def _write_call_graph_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "app.py").write_text(
        "from src.alpha import alpha\n\n"
        "def handle() -> int:\n"
        "    return alpha()\n",
        encoding="utf-8",
    )
    (root / "src" / "alpha.py").write_text(
        "from src.beta import beta\n\n"
        "def alpha() -> int:\n"
        "    return beta()\n",
        encoding="utf-8",
    )
    (root / "src" / "beta.py").write_text(
        "from src.gamma import gamma\n\n"
        "def beta() -> int:\n"
        "    return gamma()\n",
        encoding="utf-8",
    )
    (root / "src" / "gamma.py").write_text(
        "from src.alpha import alpha\n\n"
        "def gamma() -> int:\n"
        "    return alpha()\n",
        encoding="utf-8",
    )


def _write_call_graph_scip_fixture(engine: CodeContextEngine, *, include_call_graph: bool = True) -> None:
    artifact_dir = engine.repo_root / ".atelier" / "cache" / "scip" / engine.repo_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "repo_id": engine.repo_id,
        "language": "python",
        "symbols": [],
    }
    symbol_specs = [
        ("scip-handle", "src/app.py", "handle", "handle"),
        ("scip-alpha", "src/alpha.py", "alpha", "alpha"),
        ("scip-beta", "src/beta.py", "beta", "beta"),
        ("scip-gamma", "src/gamma.py", "gamma", "gamma"),
    ]
    for symbol_id, file_path, symbol_name, qualified_name in symbol_specs:
        source = (engine.repo_root / file_path).read_text(encoding="utf-8")
        payload["symbols"].append(
            {
                "symbol_id": symbol_id,
                "repo_id": engine.repo_id,
                "file_path": file_path,
                "language": "python",
                "symbol_name": symbol_name,
                "qualified_name": qualified_name,
                "kind": "function",
                "signature": f"def {symbol_name}() -> int:",
                "start_byte": source.index(f"def {symbol_name}"),
                "end_byte": len(source.encode("utf-8")),
                "start_line": 3,
                "end_line": 4,
                "content_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "source": source,
                "provenance": "scip",
            }
        )
    if include_call_graph:
        payload["call_graph"] = {
            "callers": {
                "scip-alpha": [
                    {
                        "symbol_id": "scip-handle",
                        "symbol_name": "handle",
                        "qualified_name": "handle",
                        "file_path": "src/app.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    },
                    {
                        "symbol_id": "scip-gamma",
                        "symbol_name": "gamma",
                        "qualified_name": "gamma",
                        "file_path": "src/gamma.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    },
                ],
                "scip-beta": [
                    {
                        "symbol_id": "scip-alpha",
                        "symbol_name": "alpha",
                        "qualified_name": "alpha",
                        "file_path": "src/alpha.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
                "scip-gamma": [
                    {
                        "symbol_id": "scip-beta",
                        "symbol_name": "beta",
                        "qualified_name": "beta",
                        "file_path": "src/beta.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
            },
            "callees": {
                "scip-handle": [
                    {
                        "symbol_id": "scip-alpha",
                        "symbol_name": "alpha",
                        "qualified_name": "alpha",
                        "file_path": "src/alpha.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
                "scip-alpha": [
                    {
                        "symbol_id": "scip-beta",
                        "symbol_name": "beta",
                        "qualified_name": "beta",
                        "file_path": "src/beta.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
                "scip-beta": [
                    {
                        "symbol_id": "scip-gamma",
                        "symbol_name": "gamma",
                        "qualified_name": "gamma",
                        "file_path": "src/gamma.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
                "scip-gamma": [
                    {
                        "symbol_id": "scip-alpha",
                        "symbol_name": "alpha",
                        "qualified_name": "alpha",
                        "file_path": "src/alpha.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
            },
        }
    (artifact_dir / "python.scip").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def test_code_context_indexes_searches_and_retrieves_exact_symbol(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    stats = engine.index_repo()
    assert stats.index_version >= 1
    assert stats.files_indexed >= 3
    assert stats.symbols_indexed >= 4
    assert stats.imports_indexed >= 2

    hits = engine.search_symbols("OrderService", limit=5)
    assert hits
    assert hits[0].symbol_name == "OrderService"

    symbol = engine.get_symbol(qualified_name="OrderService", file_path="src/orders.py")
    assert symbol["start_line"] == 1
    assert "class OrderService" in symbol["source"]
    assert "calculate_total" in symbol["source"]


def test_code_context_outline_context_pack_and_impact(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    outline = engine.file_outline(file_path="src/orders.py")
    assert "src/orders.py" in outline["files"]
    assert any(item["qualified_name"] == "OrderService.calculate_total" for item in outline["files"]["src/orders.py"])

    pack = engine.context_pack(
        task="change OrderService calculate_total",
        seed_files=["src/orders.py"],
        budget_tokens=350,
    )
    assert pack.token_count <= pack.budget_tokens
    assert "OrderService" in pack.content
    assert "src/checkout.py" in pack.import_neighbors

    impact = engine.impact("src/orders.py")
    assert "src/checkout.py" in impact.direct_importers
    assert "tests/test_checkout.py" in impact.transitive_importers
    assert impact.risk_level in {"medium", "high", "critical"}


def test_code_context_search_text_uses_literal_matches(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    matches = engine.search_text("calculate_total", path="src", limit=10)
    assert {match.file_path for match in matches} == {"src/orders.py", "src/checkout.py"}


def test_retrieval_cache_hit_returns_cached_payload(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    first = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    second = engine.tool_search("OrderService", limit=5, budget_tokens=4000)

    assert first["cache_hit"] is False
    assert first["provenance"] == "local"
    assert second["cache_hit"] is True
    assert second["provenance"] == "cached"
    assert first["items"] == second["items"]
    assert first["tokens_saved"] == second["tokens_saved"]


def test_retrieval_cache_invalidated_on_index_bump(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    _ = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    cached = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    assert cached["cache_hit"] is True

    indexed = engine.tool_index(budget_tokens=4000)
    fresh = engine.tool_search("OrderService", limit=5, budget_tokens=4000)

    assert indexed["index_version"] >= 2
    assert fresh["cache_hit"] is False
    assert fresh["provenance"] == "local"


def test_budget_packer_drops_optional_keys_first() -> None:
    packer = BudgetPacker()
    items = [
        {
            "symbol_id": f"sym-{index}",
            "symbol_name": f"Symbol{index}",
            "qualified_name": f"pkg.Symbol{index}",
            "file_path": f"src/mod_{index}.py",
            "kind": "function",
            "signature": f"def symbol_{index}(value: str) -> str",
            "start_line": index + 1,
            "end_line": index + 2,
            "language": "python",
            "provenance": "local",
            "doc_summary": "summary " * 20,
        }
        for index in range(10)
    ]

    packed, _, token_count = packer.pack(
        items,
        200,
        essential_keys=[
            "symbol_id",
            "symbol_name",
            "qualified_name",
            "file_path",
            "kind",
            "signature",
            "start_line",
            "end_line",
            "language",
            "provenance",
        ],
        optional_keys_in_drop_order=["doc_summary"],
    )

    assert token_count > 0
    assert len(packed) >= 3
    assert all("doc_summary" not in item for item in packed[3:])
    for item in packed[:3]:
        assert item["symbol_id"].startswith("sym-")
        assert "signature" in item
        assert "file_path" in item


def test_tool_search_keeps_total_tokens_within_budget(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    lines: list[str] = []
    for index in range(3):
        lines.append(f"def func_{index}() -> int:\n    return {index}\n")
    (tmp_path / "src" / "big.py").write_text("\n".join(lines), encoding="utf-8")

    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_search("func", limit=20, budget_tokens=255)

    assert payload["total_tokens"] <= 255


def test_provenance_local_default(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    search_payload = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    symbol_payload = engine.tool_symbol(qualified_name="OrderService", file_path="src/orders.py", budget_tokens=4000)
    context_payload = engine.tool_context(
        task="change OrderService calculate_total",
        seed_files=["src/orders.py"],
        budget_tokens=350,
    )
    cached_search = engine.tool_search("OrderService", limit=5, budget_tokens=4000)

    assert search_payload["provenance"] == "local"
    assert all(item["provenance"] == "local" for item in search_payload["items"])
    assert symbol_payload["provenance"] == "local"
    assert context_payload["provenance"] == "local"
    assert cached_search["provenance"] == "cached"


def test_tool_usages_groups_local_references_and_reports_treesitter_fallback(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_usages(query="OrderService", budget_tokens=4000)

    assert payload["target"]["qualified_name"] == "OrderService"
    assert payload["group_by"] == "file"
    assert payload["references"]["src/checkout.py"][0]["provenance"] == "treesitter"
    assert payload["reference_count"] >= 1
    assert payload["provenance_breakdown"]["treesitter"] >= 1
    assert payload["cache_hit"] is False


def test_tool_usages_returns_disambiguation_payload_for_ambiguous_name(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def helper() -> int:\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("def helper() -> int:\n    return 2\n", encoding="utf-8")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_usages(query="helper", budget_tokens=4000)

    assert payload["error"] == "disambiguation_required"
    assert len(payload["matches"]) == 2
    assert payload["cache_hit"] is False


def test_tool_callers_and_callees_traverse_depth_and_handle_cycles(tmp_path: Path) -> None:
    _write_call_graph_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_call_graph_scip_fixture(engine)

    callers = engine.tool_callers(query="beta", depth=2, budget_tokens=4000)
    callees = engine.tool_callees(query="handle", depth=2, budget_tokens=4000)

    assert callers["target"]["qualified_name"] == "beta"
    assert callers["depth"] == 2
    assert callers["data_status"] == "available"
    assert {item["qualified_name"] for item in callers["related"]} == {"alpha", "gamma", "handle"}
    assert callers["edge_count"] == 3
    assert callers["provenance"] == "scip"
    assert callees["target"]["qualified_name"] == "handle"
    assert {item["qualified_name"] for item in callees["related"]} == {"alpha", "beta"}
    assert all(edge["depth"] in {1, 2} for edge in callees["edges"])


def test_tool_callers_returns_structured_unavailable_when_call_graph_data_is_missing(tmp_path: Path) -> None:
    _write_call_graph_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_call_graph_scip_fixture(engine, include_call_graph=False)

    payload = engine.tool_callers(query="alpha", budget_tokens=4000)

    assert payload["target"]["qualified_name"] == "alpha"
    assert payload["data_status"] == "unavailable"
    assert payload["related"] == []
    assert payload["edges"] == []
    assert payload["provenance"] == "scip"


def test_tool_callees_snapshot_is_opt_in_and_returns_metadata(tmp_path: Path) -> None:
    _write_call_graph_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_call_graph_scip_fixture(engine)

    default_payload = engine.tool_callees(query="handle", budget_tokens=4000)
    snapshot_payload = engine.tool_callees(query="handle", snapshot=True, budget_tokens=4000)

    assert default_payload["snapshot"] is None
    assert snapshot_payload["snapshot"]["direction"] == "callees"
    assert snapshot_payload["snapshot"]["target_symbol_id"] == "scip-handle"
    assert snapshot_payload["snapshot"]["edge_count"] == snapshot_payload["edge_count"]


def test_tool_search_snippet_none_omits_snippets_and_keeps_exact_match_first(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "class OrderServiceFactory:\n"
        "    def build(self) -> OrderService:\n"
        "        return OrderService()\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_search("OrderService", limit=5, snippet="none", budget_tokens=4000)

    assert payload["items"][0]["symbol_name"] == "OrderService"
    assert all("snippet" not in item for item in payload["items"])


def test_semantic_and_hybrid_modes_rank_intent_query_above_lexical(tmp_path: Path) -> None:
    _write_semantic_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    query = "create login token for authenticated user"

    lexical_hits = engine.search_symbols(query, limit=5, mode="lexical")
    semantic_hits = engine.search_symbols(query, limit=5, mode="semantic")
    hybrid_hits = engine.search_symbols(query, limit=5, mode="hybrid")

    assert lexical_hits
    assert semantic_hits
    assert hybrid_hits
    assert lexical_hits[0].symbol_name == "create_login_history_for_authenticated_user"
    assert semantic_hits[0].symbol_name == "issue_access_token"
    assert hybrid_hits[0].symbol_name == "issue_access_token"


def test_auto_mode_keeps_identifier_queries_on_exact_lexical_order(tmp_path: Path) -> None:
    _write_semantic_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    hits = engine.search_symbols("issue_access_token", limit=5, mode="auto")
    payload = engine.tool_search("issue_access_token", limit=5, mode="auto", budget_tokens=4000)

    assert hits
    assert hits[0].symbol_name == "issue_access_token"
    assert payload["items"][0]["symbol_name"] == "issue_access_token"
    assert payload["mode"] == "lexical"


def test_tool_search_cache_keys_are_mode_aware(tmp_path: Path) -> None:
    _write_semantic_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    query = "create login token for authenticated user"

    lexical_first = engine.tool_search(query, limit=5, mode="lexical", budget_tokens=4000)
    semantic_first = engine.tool_search(query, limit=5, mode="semantic", budget_tokens=4000)
    lexical_second = engine.tool_search(query, limit=5, mode="lexical", budget_tokens=4000)
    semantic_second = engine.tool_search(query, limit=5, mode="semantic", budget_tokens=4000)

    assert lexical_first["cache_hit"] is False
    assert semantic_first["cache_hit"] is False
    assert lexical_second["cache_hit"] is True
    assert semantic_second["cache_hit"] is True
    assert lexical_first["mode"] == "lexical"
    assert semantic_first["mode"] == "semantic"
    assert lexical_first["items"][0]["symbol_name"] != semantic_first["items"][0]["symbol_name"]


def test_retrieval_cache_diagnostics_hide_payloads_and_invalidate_one_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    engine.tool_symbol(qualified_name="OrderService", file_path="src/orders.py", budget_tokens=4000)

    status = engine.tool_cache_status(budget_tokens=4000)

    assert status["entry_count"] == 2
    assert status["entries_by_tool"] == {"code.search": 1, "code.symbol": 1}
    assert status["repo_id"] == engine.repo_id
    assert "payload_json" not in str(status)
    assert "items" not in status
    assert "matches" not in status

    invalidated = engine.tool_cache_invalidate(cache_tool="search", budget_tokens=4000)

    assert invalidated["invalidated_entries"] == 1
    assert invalidated["entries_by_tool"] == {"code.search": 1}
    assert invalidated["scope"]["cache_tool"] == "search"

    status_after = engine.tool_cache_status(budget_tokens=4000)
    assert status_after["entry_count"] == 1
    assert status_after["entries_by_tool"] == {"code.symbol": 1}

    fresh_search = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    cached_symbol = engine.tool_symbol(qualified_name="OrderService", file_path="src/orders.py", budget_tokens=4000)
    assert fresh_search["cache_hit"] is False
    assert cached_symbol["cache_hit"] is True


def test_low_token_defaults_stay_lighter_for_search_and_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "class OrderServiceFactory:\n"
        "    def build(self) -> OrderService:\n"
        "        return OrderService()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "http.py").write_text(
        "\n".join(
            f"def fetch_{index}(url: str) -> object:\n    return requests.get(url)\n"
            for index in range(30)
        ),
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    search_default = engine.tool_search("OrderService", limit=5, snippet="none", budget_tokens=4000)
    search_heavy = engine.tool_search("OrderService", limit=5, snippet="full", budget_tokens=4000)

    assert search_default["total_tokens"] < search_heavy["total_tokens"]

    tight = engine.tool_search("fetch_", limit=20, snippet="full", budget_tokens=320)
    assert tight["total_tokens"] <= 320
    assert tight["items"]
    for key in ("symbol_id", "symbol_name", "file_path", "start_line", "signature"):
        assert key in tight["items"][0]

    monkeypatch.setattr(
        "atelier.core.capabilities.code_context.engine.AstGrepAdapter.search",
        lambda self, *, pattern, language=None, file_glob=None, limit=20: PatternSearchResult(
            matches=[
                PatternMatch(
                    file_path="src/http.py",
                    line=index + 1,
                    column=4,
                    end_line=index + 1,
                    end_column=28,
                    snippet="return requests.get(url)",
                    captures={"URL": "url"},
                )
                for index in range(30)
            ],
            truncated=limit < 30,
            total_matches=30,
        ),
    )

    pattern_default = engine.tool_pattern(pattern="requests.get($URL)", budget_tokens=4000)
    pattern_heavy = engine.tool_pattern(pattern="requests.get($URL)", limit=100, budget_tokens=4000)

    assert pattern_default["total_tokens"] < pattern_heavy["total_tokens"]
