"""Deterministic smoke benchmark for Phase 1 symbol-search retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atelier.gateway.adapters.mcp_server import tool_code


@dataclass
class SymbolSearchBenchResult:
    """Deterministic summary for the M0 symbol-search smoke harness."""

    query: str
    budget_tokens: int
    result_count: int
    uncached_total_tokens: int
    cached_total_tokens: int
    uncached_tokens_saved: int
    cached_tokens_saved: int
    uncached_cache_hit: bool
    cached_cache_hit: bool
    uncached_provenance: str
    cached_provenance: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "budget_tokens": self.budget_tokens,
            "result_count": self.result_count,
            "uncached_total_tokens": self.uncached_total_tokens,
            "cached_total_tokens": self.cached_total_tokens,
            "uncached_tokens_saved": self.uncached_tokens_saved,
            "cached_tokens_saved": self.cached_tokens_saved,
            "uncached_cache_hit": self.uncached_cache_hit,
            "cached_cache_hit": self.cached_cache_hit,
            "uncached_provenance": self.uncached_provenance,
            "cached_provenance": self.cached_provenance,
        }


@dataclass
class SemanticSymbolSearchBenchResult:
    """Deterministic summary for the M6 semantic search benchmark."""

    budget_tokens: int
    lexical_ndcg_at_5: float
    semantic_ndcg_at_5: float
    hybrid_ndcg_at_5: float
    lexical_exact_identifier_first: bool
    fixtures: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_tokens": self.budget_tokens,
            "lexical_ndcg_at_5": self.lexical_ndcg_at_5,
            "semantic_ndcg_at_5": self.semantic_ndcg_at_5,
            "hybrid_ndcg_at_5": self.hybrid_ndcg_at_5,
            "lexical_exact_identifier_first": self.lexical_exact_identifier_first,
            "fixtures": self.fixtures,
        }


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


def _write_semantic_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "auth.py").write_text(
        "def issue_access_token(user_id: str) -> str:\n"
        '    """Create a login session token for an authenticated user."""\n'
        "    session_token = f'session:{user_id}'\n"
        "    return session_token\n"
        "\n"
        "def refresh_access_token(user_id: str) -> str:\n"
        '    """Refresh a stale session token for a returning user."""\n'
        "    return f'refresh:{user_id}'\n",
        encoding="utf-8",
    )
    (root / "src" / "audit.py").write_text(
        "def create_login_history_for_authenticated_user(user_id: str) -> dict[str, str]:\n"
        '    """Record login history entries for audit review."""\n'
        "    return {'user_id': user_id}\n",
        encoding="utf-8",
    )


def _dcg(rank: int | None) -> float:
    if rank is None or rank > 5:
        return 0.0
    import math

    return 1.0 / math.log2(rank + 1)


def _expected_rank(items: list[dict[str, Any]], expected_symbol: str) -> int | None:
    for index, item in enumerate(items[:5], start=1):
        if str(item.get("symbol_name")) == expected_symbol:
            return index
    return None


def run_symbol_search_bench(
    work_dir: Path | None = None,
    *,
    query: str = "OrderService",
    budget_tokens: int = 255,
) -> SymbolSearchBenchResult:
    """Run a deterministic two-call code-search smoke benchmark."""

    bench_root = (work_dir or Path.cwd()) / "code_intel_symbol_search"
    repo_root = bench_root / "fixture_repo"
    _write_fixture_repo(repo_root)

    first = tool_code(
        {"op": "search", "repo_root": str(repo_root), "query": query, "limit": 5, "budget_tokens": budget_tokens}
    )
    second = tool_code(
        {"op": "search", "repo_root": str(repo_root), "query": query, "limit": 5, "budget_tokens": budget_tokens}
    )

    return SymbolSearchBenchResult(
        query=query,
        budget_tokens=budget_tokens,
        result_count=len(first.get("items", [])),
        uncached_total_tokens=int(first.get("total_tokens", 0) or 0),
        cached_total_tokens=int(second.get("total_tokens", 0) or 0),
        uncached_tokens_saved=int(first.get("tokens_saved", 0) or 0),
        cached_tokens_saved=int(second.get("tokens_saved", 0) or 0),
        uncached_cache_hit=bool(first.get("cache_hit")),
        cached_cache_hit=bool(second.get("cache_hit")),
        uncached_provenance=str(first.get("provenance") or ""),
        cached_provenance=str(second.get("provenance") or ""),
    )


def run_semantic_symbol_search_bench(
    work_dir: Path | None = None,
    *,
    budget_tokens: int = 4000,
) -> SemanticSymbolSearchBenchResult:
    """Run deterministic semantic and hybrid search fixtures through the MCP surface."""
    bench_root = (work_dir or Path.cwd()) / "code_intel_semantic_symbol_search"
    repo_root = bench_root / "fixture_repo"
    _write_semantic_fixture_repo(repo_root)

    fixture_specs = [
        {"query": "create login token for authenticated user", "expected": "issue_access_token"},
        {"query": "record login history for audit review", "expected": "create_login_history_for_authenticated_user"},
    ]

    fixture_results: list[dict[str, Any]] = []
    ndcg_scores: dict[str, list[float]] = {"lexical": [], "semantic": [], "hybrid": []}
    for spec in fixture_specs:
        fixture_result: dict[str, Any] = {"query": spec["query"], "expected": spec["expected"], "modes": {}}
        for mode in ("lexical", "semantic", "hybrid"):
            payload = tool_code(
                {
                    "op": "search",
                    "repo_root": str(repo_root),
                    "query": spec["query"],
                    "mode": mode,
                    "limit": 5,
                    "budget_tokens": budget_tokens,
                }
            )
            rank = _expected_rank(list(payload.get("items", [])), str(spec["expected"]))
            ndcg_scores[mode].append(_dcg(rank))
            fixture_result["modes"][mode] = {
                "mode": payload.get("mode"),
                "top_symbol": payload.get("items", [{}])[0].get("symbol_name") if payload.get("items") else None,
                "expected_rank": rank,
                "total_tokens": payload.get("total_tokens"),
            }
        fixture_results.append(fixture_result)

    exact_auto = tool_code(
        {
            "op": "search",
            "repo_root": str(repo_root),
            "query": "issue_access_token",
            "mode": "auto",
            "limit": 5,
            "budget_tokens": budget_tokens,
        }
    )
    lexical_exact_identifier_first = (
        exact_auto.get("mode") == "lexical"
        and bool(exact_auto.get("items"))
        and exact_auto["items"][0]["symbol_name"] == "issue_access_token"
    )

    return SemanticSymbolSearchBenchResult(
        budget_tokens=budget_tokens,
        lexical_ndcg_at_5=sum(ndcg_scores["lexical"]) / len(ndcg_scores["lexical"]),
        semantic_ndcg_at_5=sum(ndcg_scores["semantic"]) / len(ndcg_scores["semantic"]),
        hybrid_ndcg_at_5=sum(ndcg_scores["hybrid"]) / len(ndcg_scores["hybrid"]),
        lexical_exact_identifier_first=lexical_exact_identifier_first,
        fixtures=fixture_results,
    )


__all__ = [
    "SemanticSymbolSearchBenchResult",
    "SymbolSearchBenchResult",
    "run_semantic_symbol_search_bench",
    "run_symbol_search_bench",
]
