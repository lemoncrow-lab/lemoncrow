"""Smoke tests for the Phase 1 code-intel symbol-search benchmark."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from time import perf_counter_ns

import pytest

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.gateway.adapters.mcp_server import tool_code, tool_smart_read, tool_smart_search
from benchmarks.code_intel.symbol_search_bench import (
    run_semantic_symbol_search_bench,
    run_symbol_search_bench,
)

pytestmark = pytest.mark.slow  # Full repo-map + token-budget benchmark; takes ~15-20s


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
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


def _write_scip_fixture(engine: CodeContextEngine) -> None:
    artifact_dir = engine.repo_root / ".atelier" / "cache" / "scip" / engine.repo_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    symbols = []
    for qualified_name, file_path in (
        ("OrderService", "src/orders.py"),
        ("OrderService.calculate_total", "src/orders.py"),
        ("helper", "src/orders.py"),
        ("checkout", "src/checkout.py"),
    ):
        source = (engine.repo_root / file_path).read_text(encoding="utf-8")
        symbol_name = qualified_name.rsplit(".", 1)[-1]
        kind = "method" if "." in qualified_name else "class" if symbol_name == "OrderService" else "function"
        symbols.append(
            {
                "symbol_id": f"scip-{qualified_name}",
                "repo_id": engine.repo_id,
                "file_path": file_path,
                "language": "python",
                "symbol_name": symbol_name,
                "qualified_name": qualified_name,
                "kind": kind,
                "signature": source.splitlines()[0],
                "start_byte": 0,
                "end_byte": len(source.encode("utf-8")),
                "start_line": 1,
                "end_line": max(1, len(source.splitlines())),
                "content_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "source": source,
                "provenance": "scip",
            }
        )
    (artifact_dir / "python.scip").write_text(
        json.dumps(
            {
                "version": 1,
                "repo_id": engine.repo_id,
                "language": "python",
                "symbols": symbols,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _measure_ns(func: Callable[[], None], iterations: int) -> int:
    start = perf_counter_ns()
    for _ in range(iterations):
        func()
    elapsed = perf_counter_ns() - start
    return max(1, elapsed // iterations)


def _text_search_plus_read_tokens(repo_root: Path, query: str) -> int:
    search_payload = tool_smart_search({"query": query, "path": str(repo_root / "src"), "budget_tokens": 4000})
    matches = search_payload.get("matches", [])
    assert matches, f"expected text-search match for {query}"
    first_match = matches[0]
    read_payload = tool_smart_read({"path": str(first_match["path"])})
    return count_tokens(json.dumps(search_payload, sort_keys=True, default=str)) + count_tokens(
        json.dumps(read_payload, sort_keys=True, default=str)
    )


def test_symbol_search_bench_smoke(tmp_path: Path) -> None:
    result = run_symbol_search_bench(tmp_path)

    assert result.result_count >= 1
    assert result.uncached_cache_hit is False
    assert result.cached_cache_hit is True
    assert result.uncached_provenance == "local"
    assert result.cached_provenance == "cached"
    assert result.uncached_total_tokens <= result.budget_tokens


def test_symbol_search_bench_result_is_json_serializable(tmp_path: Path) -> None:
    payload = run_symbol_search_bench(tmp_path).to_dict()

    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)

    assert reloaded["cached_cache_hit"] is True
    assert reloaded["uncached_provenance"] == "local"


def test_semantic_symbol_search_bench_meets_m6_ndcg_gate(tmp_path: Path) -> None:
    result = run_semantic_symbol_search_bench(tmp_path)

    assert result.semantic_ndcg_at_5 >= 0.7
    assert result.hybrid_ndcg_at_5 >= 0.7
    assert all(
        int(mode_result["total_tokens"]) <= result.budget_tokens
        for fixture in result.fixtures
        for mode_result in fixture["modes"].values()
        if mode_result["total_tokens"] is not None
    )


def test_semantic_symbol_search_bench_preserves_exact_identifier_regression_gate(tmp_path: Path) -> None:
    payload = run_semantic_symbol_search_bench(tmp_path).to_dict()

    assert payload["lexical_exact_identifier_first"] is True
    assert payload["hybrid_ndcg_at_5"] >= payload["lexical_ndcg_at_5"]


def test_symbol_search_planner_handles_typo_fuzzy_without_displacing_exact(tmp_path: Path) -> None:
    repo_root = tmp_path / "planner_repo"
    _write_fixture_repo(repo_root)

    exact = tool_code(
        {"op": "search", "repo_root": str(repo_root), "query": "OrderService", "limit": 5, "mode": "lexical"}
    )
    fuzzy = tool_code(
        {"op": "search", "repo_root": str(repo_root), "query": "OrdreServce", "limit": 5, "mode": "lexical"}
    )

    assert exact["items"]
    assert exact["items"][0]["symbol_name"] == "OrderService"
    assert fuzzy["items"]
    assert fuzzy["items"][0]["symbol_name"] == "OrderService"


def test_scip_vs_local_latency_ratio_min_100x(tmp_path: Path) -> None:
    local_repo_root = tmp_path / "latency_local_repo"
    routed_repo_root = tmp_path / "latency_routed_repo"
    _write_fixture_repo(local_repo_root)
    _write_fixture_repo(routed_repo_root)

    local_counter = {"value": 0}

    def run_local_once() -> None:
        db_path = local_repo_root / f"local_{local_counter['value']}.sqlite"
        local_counter["value"] += 1
        engine = CodeContextEngine(local_repo_root, db_path=db_path)
        for query in ("OrderService", "helper", "checkout"):
            search_payload = engine.tool_search(query, limit=5, budget_tokens=4000)
            assert search_payload["items"]
            symbol_payload = engine.tool_symbol(symbol_id=search_payload["items"][0]["symbol_id"], budget_tokens=4000)
            assert symbol_payload["provenance"] == "local"

    scip_engine = CodeContextEngine(routed_repo_root, db_path=routed_repo_root / "scip.sqlite")
    scip_engine.index_repo()
    _write_scip_fixture(scip_engine)
    warmed = scip_engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    assert warmed["provenance"] == "scip"

    def run_scip_once() -> None:
        hits = scip_engine.search_symbols("OrderService", limit=5)
        assert hits
        assert hits[0].provenance == "scip"

    local_latency_ns = _measure_ns(run_local_once, iterations=5)
    scip_latency_ns = _measure_ns(run_scip_once, iterations=500)
    ratio = local_latency_ns / scip_latency_ns

    assert ratio >= 100, f"expected >=100x warm SCIP speedup, got {ratio:.2f}x"


def test_scip_navigation_tokens_at_most_half_of_local_baseline(tmp_path: Path) -> None:
    local_repo_root = tmp_path / "tokens_local_repo"
    routed_repo_root = tmp_path / "tokens_routed_repo"
    _write_fixture_repo(local_repo_root)
    _write_fixture_repo(routed_repo_root)

    local_engine = CodeContextEngine(local_repo_root, db_path=local_repo_root / "local_nav.sqlite")
    local_engine.index_repo()
    scip_engine = CodeContextEngine(routed_repo_root, db_path=routed_repo_root / "scip_nav.sqlite")
    scip_engine.index_repo()
    _write_scip_fixture(scip_engine)

    local_queries = ["OrderService", "helper", "checkout"]
    local_tokens = 0
    for query in local_queries:
        search_payload = local_engine.tool_search(query, limit=1, budget_tokens=4000)
        assert search_payload["items"]
        symbol_payload = local_engine.tool_symbol(symbol_id=search_payload["items"][0]["symbol_id"], budget_tokens=4000)
        local_tokens += int(search_payload["total_tokens"]) + int(symbol_payload["total_tokens"])

    routed_tokens = 0
    for query in local_queries:
        payload = scip_engine.tool_search(query, limit=1, budget_tokens=260)
        assert payload["items"]
        assert payload["provenance"] == "scip"
        routed_tokens += int(payload["total_tokens"])

    assert routed_tokens <= local_tokens / 2


def test_symbol_search_uses_at_most_25pct_of_text_search_tokens(tmp_path: Path) -> None:
    repo_root = tmp_path / "m2_repo"
    _write_fixture_repo(repo_root)

    queries = ["OrderService", "calculate_total", "helper", "checkout"]
    baseline_tokens = 0
    symbol_tokens = 0
    for query in queries:
        baseline_tokens += _text_search_plus_read_tokens(repo_root, query)
        payload = tool_code(
            {"op": "search", "repo_root": str(repo_root), "query": query, "limit": 1, "budget_tokens": 120}
        )
        assert payload["items"], f"expected code-search match for {query}"
        symbol_tokens += int(payload["total_tokens"])

    assert symbol_tokens <= baseline_tokens * 0.25
