"""Deterministic benchmark for the M3 usages workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.gateway.adapters.mcp_server import tool_code, tool_smart_read, tool_smart_search


@dataclass(frozen=True)
class UsagesBenchResult:
    """Summary of the usages token comparison."""

    usages_total_tokens: int
    baseline_total_tokens: int
    token_ratio: float
    reference_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "usages_total_tokens": self.usages_total_tokens,
            "baseline_total_tokens": self.baseline_total_tokens,
            "token_ratio": self.token_ratio,
            "reference_count": self.reference_count,
        }


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n",
        encoding="utf-8",
    )
    (root / "src" / "checkout.py").write_text(
        "from src.orders import OrderService\n\n"
        "def checkout(items: list[int]) -> int:\n"
        "    return OrderService().calculate_total(items)\n",
        encoding="utf-8",
    )
    (root / "src" / "report.py").write_text(
        "from src.orders import OrderService\n\n"
        "def summarize(items: list[int]) -> int:\n"
        "    service = OrderService()\n"
        "    return service.calculate_total(items)\n",
        encoding="utf-8",
    )


def _measure_grep_read_baseline(repo_root: Path) -> int:
    search_payload = tool_smart_search({"query": "OrderService", "path": str(repo_root / "src"), "budget_tokens": 4000})
    unique_paths = sorted({str(match["path"]) for match in search_payload.get("matches", [])})
    read_tokens = 0
    for path in unique_paths:
        read_payload = tool_smart_read({"path": path, "max_lines": 20})
        read_tokens += count_tokens(json.dumps(read_payload, sort_keys=True, default=str))
    return count_tokens(json.dumps(search_payload, sort_keys=True, default=str)) + read_tokens


def run_usages_bench(work_dir: Path | None = None) -> UsagesBenchResult:
    """Compare `code op=\"usages\"` against a grep-plus-read baseline."""

    bench_root = (work_dir or Path.cwd()) / "code_intel_usages"
    repo_root = bench_root / "fixture_repo"
    _write_fixture_repo(repo_root)

    baseline_total_tokens = _measure_grep_read_baseline(repo_root)
    usages_payload = tool_code(
        {"op": "usages", "repo_root": str(repo_root), "query": "OrderService", "budget_tokens": 220}
    )
    usages_total_tokens = int(usages_payload.get("total_tokens", 0) or 0)
    ratio = usages_total_tokens / baseline_total_tokens if baseline_total_tokens else 0.0
    return UsagesBenchResult(
        usages_total_tokens=usages_total_tokens,
        baseline_total_tokens=baseline_total_tokens,
        token_ratio=ratio,
        reference_count=int(usages_payload.get("reference_count", 0) or 0),
    )


__all__ = ["UsagesBenchResult", "run_usages_bench"]
