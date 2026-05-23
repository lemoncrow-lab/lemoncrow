from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter
from typing import Any

import tiktoken

from atelier.gateway.adapters.mcp_server import tool_code

ENC = tiktoken.get_encoding("cl100k_base")


def _tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return len(ENC.encode(text))


def _contains_expected_terms(payload: Any, expected_terms: list[str]) -> bool:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    lowered = text.lower()
    return all(term.lower() in lowered for term in expected_terms)


def _default_ops() -> list[tuple[str, dict[str, Any], list[str]]]:
    return [
        ("cache_status", {"op": "cache_status", "budget_tokens": 2000}, ["cache"]),
        ("index", {"op": "index", "include_globs": ["src/**/*.py", "tests/**/*.py"], "budget_tokens": 3000}, ["files_indexed"]),
        (
            "search",
            {"op": "search", "query": "classify_command", "mode": "lexical", "limit": 10, "budget_tokens": 3000},
            ["classify_command"],
        ),
        ("symbol", {"op": "symbol", "symbol_name": "classify_command", "budget_tokens": 3000}, ["classify_command"]),
        (
            "outline",
            {"op": "outline", "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py", "budget_tokens": 3000},
            ["symbol_count"],
        ),
        ("pattern", {"op": "pattern", "pattern": "@mcp_tool($$$)", "language": "python", "limit": 20, "budget_tokens": 3000}, ["mcp_tool"]),
        (
            "callers",
            {
                "op": "callers",
                "symbol_name": "run_command",
                "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
                "depth": 1,
                "budget_tokens": 3000,
            },
            ["run_command"],
        ),
        (
            "callees",
            {"op": "callees", "symbol_name": "classify_command", "depth": 1, "budget_tokens": 3000},
            ["classify_command"],
        ),
        (
            "usages",
            {
                "op": "usages",
                "symbol_name": "run_command",
                "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
                "limit": 30,
                "budget_tokens": 3000,
            },
            ["run_command"],
        ),
        (
            "impact",
            {"op": "impact", "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py", "budget_tokens": 3000},
            ["importers"],
        ),
        (
            "context",
            {
                "op": "context",
                "task": "add a new MCP code handler with schema validation",
                "budget_tokens": 4000,
                "max_symbols": 12,
            },
            ["entry_points"],
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug benchmark matrix for code tool operations.")
    parser.add_argument(
        "--output",
        default="artifacts/code_matrix_debug.json",
        help="Output JSON path for op-level results.",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for operation, payload, expected_terms in _default_ops():
        t0 = perf_counter()
        error: str | None = None
        raw_output: Any
        try:
            raw_output = tool_code(payload)
        except Exception as exc:  # propagate failure information for baseline freeze
            raw_output = {}
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (perf_counter() - t0) * 1000.0
        rows.append(
            {
                "operation": operation,
                "input": payload,
                "raw_output": raw_output,
                "tokens": _tokens(raw_output),
                "contains_expected_terms": _contains_expected_terms(raw_output, expected_terms),
                "error": error,
                "elapsed_ms": round(elapsed_ms, 3),
            }
        )

    output_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"Wrote {len(rows)} operation rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
