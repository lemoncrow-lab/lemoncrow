"""Deterministic benchmark for the M8 routed call-graph workflow."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.gateway.adapters.mcp_server import tool_code


@dataclass(frozen=True)
class CallGraphBenchResult:
    """Summary of the default-vs-expanded call-graph token comparison."""

    budget_tokens: int
    expanded_budget_tokens: int
    default_total_tokens: int
    expanded_total_tokens: int
    default_within_budget: bool
    target_symbol: str
    default_related: list[str]
    expanded_related: list[str]
    snapshot_present: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_tokens": self.budget_tokens,
            "expanded_budget_tokens": self.expanded_budget_tokens,
            "default_total_tokens": self.default_total_tokens,
            "expanded_total_tokens": self.expanded_total_tokens,
            "default_within_budget": self.default_within_budget,
            "target_symbol": self.target_symbol,
            "default_related": self.default_related,
            "expanded_related": self.expanded_related,
            "snapshot_present": self.snapshot_present,
        }


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "app.py").write_text(
        "from src.alpha import alpha\n\n" "def handle() -> int:\n" "    return alpha()\n",
        encoding="utf-8",
    )
    (root / "src" / "alpha.py").write_text(
        "from src.beta import beta\n\n" "def alpha() -> int:\n" "    return beta()\n",
        encoding="utf-8",
    )
    (root / "src" / "beta.py").write_text(
        "from src.gamma import gamma\n\n" "def beta() -> int:\n" "    return gamma()\n",
        encoding="utf-8",
    )
    (root / "src" / "gamma.py").write_text(
        "from src.alpha import alpha\n\n" "def gamma() -> int:\n" "    return alpha()\n",
        encoding="utf-8",
    )


def _write_scip_fixture(engine: CodeContextEngine) -> None:
    artifact_dir = engine.repo_root / ".atelier" / "cache" / "scip" / engine.repo_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    symbol_specs = [
        ("scip-handle", "src/app.py", "handle"),
        ("scip-alpha", "src/alpha.py", "alpha"),
        ("scip-beta", "src/beta.py", "beta"),
        ("scip-gamma", "src/gamma.py", "gamma"),
    ]
    symbols: list[dict[str, Any]] = []
    for symbol_id, file_path, symbol_name in symbol_specs:
        source = (engine.repo_root / file_path).read_text(encoding="utf-8")
        symbols.append(
            {
                "symbol_id": symbol_id,
                "repo_id": engine.repo_id,
                "file_path": file_path,
                "language": "python",
                "symbol_name": symbol_name,
                "qualified_name": symbol_name,
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
    payload = {
        "version": 1,
        "repo_id": engine.repo_id,
        "language": "python",
        "index_sha": "0000000000000000000000000000000000000000",
        "symbols": symbols,
        "call_graph": {
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
            },
        },
    }
    (artifact_dir / "python.scip").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def run_call_graph_bench(
    work_dir: Path | None = None,
    *,
    budget_tokens: int = 520,
    expanded_budget_tokens: int = 1200,
) -> CallGraphBenchResult:
    """Compare cheap default callers traversal against a deeper snapshot-heavy request."""

    bench_root = (work_dir or Path.cwd()) / "code_intel_call_graph"
    repo_root = bench_root / "fixture_repo"
    _write_fixture_repo(repo_root)
    engine = CodeContextEngine(repo_root)
    engine.index_repo()
    _write_scip_fixture(engine)

    default_payload = tool_code(
        {
            "op": "callers",
            "repo_root": str(repo_root),
            "query": "beta",
            "budget_tokens": budget_tokens,
        }
    )
    expanded_payload = tool_code(
        {
            "op": "callers",
            "repo_root": str(repo_root),
            "query": "beta",
            "depth": 2,
            "snapshot": True,
            "budget_tokens": expanded_budget_tokens,
        }
    )
    default_total_tokens = count_tokens(json.dumps(default_payload, sort_keys=True, ensure_ascii=False, default=str))
    expanded_total_tokens = count_tokens(json.dumps(expanded_payload, sort_keys=True, ensure_ascii=False, default=str))
    return CallGraphBenchResult(
        budget_tokens=budget_tokens,
        expanded_budget_tokens=expanded_budget_tokens,
        default_total_tokens=default_total_tokens,
        expanded_total_tokens=expanded_total_tokens,
        default_within_budget=int(default_payload["total_tokens"]) <= budget_tokens,
        target_symbol=str(default_payload["target"]["qualified_name"]),
        default_related=[str(item["qualified_name"]) for item in default_payload["related"]],
        expanded_related=[str(item["qualified_name"]) for item in expanded_payload["related"]],
        snapshot_present=expanded_payload.get("snapshot") is not None,
    )


__all__ = ["CallGraphBenchResult", "run_call_graph_bench"]
