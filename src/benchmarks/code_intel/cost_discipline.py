"""Deterministic M12 cost-discipline benchmark for shipped Phase 2 flows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.gateway.adapters.mcp_server import tool_code, tool_smart_read, tool_smart_search
from benchmarks.code_intel.blame_bench import run_blame_bench
from benchmarks.code_intel.graveyard_bench import run_graveyard_bench
from benchmarks.code_intel.pattern_bench import run_pattern_bench


@dataclass(frozen=True)
class CostDisciplineBenchResult:
    """Aggregate token summary for the current M12 partial-close benchmark."""

    search_baseline_tokens: int
    search_current_tokens: int
    pattern_baseline_tokens: int
    pattern_current_tokens: int
    historical_baseline_tokens: int
    historical_current_tokens: int
    blame_baseline_tokens: int
    blame_current_tokens: int
    aggregate_baseline_tokens: int
    aggregate_current_tokens: int
    aggregate_ratio: float
    cache_status_total_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "search_baseline_tokens": self.search_baseline_tokens,
            "search_current_tokens": self.search_current_tokens,
            "pattern_baseline_tokens": self.pattern_baseline_tokens,
            "pattern_current_tokens": self.pattern_current_tokens,
            "historical_baseline_tokens": self.historical_baseline_tokens,
            "historical_current_tokens": self.historical_current_tokens,
            "blame_baseline_tokens": self.blame_baseline_tokens,
            "blame_current_tokens": self.blame_current_tokens,
            "aggregate_baseline_tokens": self.aggregate_baseline_tokens,
            "aggregate_current_tokens": self.aggregate_current_tokens,
            "aggregate_ratio": self.aggregate_ratio,
            "cache_status_total_tokens": self.cache_status_total_tokens,
        }


def _write_search_fixture_repo(root: Path) -> None:
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


def _text_search_plus_read_tokens(repo_root: Path, query: str) -> int:
    search_payload = tool_smart_search({"query": query, "path": str(repo_root / "src"), "budget_tokens": 4000})
    matches = search_payload.get("matches", [])
    if not matches:
        raise AssertionError(f"expected text-search match for {query}")
    first_match = matches[0]
    read_payload = tool_smart_read({"file_path": str(first_match["path"]), "max_lines": 20})
    return count_tokens(json.dumps(search_payload, sort_keys=True, default=str)) + count_tokens(
        json.dumps(read_payload, sort_keys=True, default=str)
    )


def run_cost_discipline_bench(work_dir: Path | None = None) -> CostDisciplineBenchResult:
    """Compare shipped low-token defaults against pre-code-intel search/edit baselines."""

    bench_root = (work_dir or Path.cwd()) / "code_intel_cost_discipline"
    search_repo_root = bench_root / "search_fixture_repo"
    _write_search_fixture_repo(search_repo_root)

    queries = ["OrderService", "calculate_total", "helper", "checkout"]
    search_baseline_tokens = 0
    search_current_tokens = 0
    for query in queries:
        search_baseline_tokens += _text_search_plus_read_tokens(search_repo_root, query)
        payload = tool_code(
            {
                "op": "search",
                "repo_root": str(search_repo_root),
                "query": query,
                "limit": 1,
                "budget_tokens": 120,
            }
        )
        if not payload.get("items"):
            raise AssertionError(f"expected code-search match for {query}")
        search_current_tokens += int(payload.get("total_tokens", 0) or 0)

    cache_status_payload = tool_code({"op": "cache_status", "repo_root": str(search_repo_root), "budget_tokens": 180})
    pattern_result = run_pattern_bench(bench_root / "pattern")
    historical_result = run_graveyard_bench(bench_root / "graveyard")
    blame_result = run_blame_bench(bench_root / "blame")

    aggregate_baseline_tokens = (
        search_baseline_tokens
        + int(pattern_result.baseline_total_tokens)
        + int(historical_result.manual_total_tokens)
        + int(blame_result.manual_total_tokens)
    )
    aggregate_current_tokens = (
        search_current_tokens
        + int(pattern_result.pattern_total_tokens)
        + int(historical_result.uncached_total_tokens)
        + int(blame_result.cold_total_tokens)
    )
    aggregate_ratio = aggregate_current_tokens / aggregate_baseline_tokens if aggregate_baseline_tokens else 0.0

    return CostDisciplineBenchResult(
        search_baseline_tokens=search_baseline_tokens,
        search_current_tokens=search_current_tokens,
        pattern_baseline_tokens=int(pattern_result.baseline_total_tokens),
        pattern_current_tokens=int(pattern_result.pattern_total_tokens),
        historical_baseline_tokens=int(historical_result.manual_total_tokens),
        historical_current_tokens=int(historical_result.uncached_total_tokens),
        blame_baseline_tokens=int(blame_result.manual_total_tokens),
        blame_current_tokens=int(blame_result.cold_total_tokens),
        aggregate_baseline_tokens=aggregate_baseline_tokens,
        aggregate_current_tokens=aggregate_current_tokens,
        aggregate_ratio=aggregate_ratio,
        cache_status_total_tokens=int(cache_status_payload.get("total_tokens", 0) or 0),
    )


__all__ = ["CostDisciplineBenchResult", "run_cost_discipline_bench"]
