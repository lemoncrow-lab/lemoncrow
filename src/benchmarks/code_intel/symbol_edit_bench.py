"""Deterministic benchmark for the M4 symbol-edit workflow."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.gateway.adapters.mcp_server import tool_smart_edit, tool_smart_read, tool_smart_search


@dataclass(frozen=True)
class SymbolEditBenchResult:
    """Summary of the symbol-edit token comparison."""

    symbol_edit_total_tokens: int
    baseline_total_tokens: int
    token_ratio: float
    edited_path: str

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol_edit_total_tokens": self.symbol_edit_total_tokens,
            "baseline_total_tokens": self.baseline_total_tokens,
            "token_ratio": self.token_ratio,
            "edited_path": self.edited_path,
        }


@contextmanager
def _workspace_env(workspace_root: Path, atelier_root: Path) -> Iterator[None]:
    old_workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT")
    old_atelier = os.environ.get("ATELIER_ROOT")
    os.environ["CLAUDE_WORKSPACE_ROOT"] = str(workspace_root)
    os.environ["ATELIER_ROOT"] = str(atelier_root)
    try:
        yield
    finally:
        if old_workspace is None:
            os.environ.pop("CLAUDE_WORKSPACE_ROOT", None)
        else:
            os.environ["CLAUDE_WORKSPACE_ROOT"] = old_workspace
        if old_atelier is None:
            os.environ.pop("ATELIER_ROOT", None)
        else:
            os.environ["ATELIER_ROOT"] = old_atelier


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "service.py").write_text(
        "class AuthService:\n"
        "    def verify(self, token: str) -> bool:\n"
        "        return token == 'ok'\n"
        "\n"
        "def issue_token(prefix: str) -> str:\n"
        "    return f'{prefix}-token'\n",
        encoding="utf-8",
    )


def _measure_line_edit_baseline(repo_root: Path) -> tuple[int, str]:
    with _workspace_env(repo_root, repo_root / ".atelier"):
        search_payload = tool_smart_search({"query": "def verify", "path": "src", "budget_tokens": 4000})
        matches = search_payload.get("matches", [])
        if not matches:
            raise AssertionError("expected text-search match for symbol-edit baseline")
        target_path = str(matches[0]["path"])
        read_payload = tool_smart_read({"path": target_path, "max_lines": 20})
        line_edit_request = {
            "edits": [
                {
                    "file_path": "src/service.py#2-3",
                    "old_string": (
                        "    def verify(self, token: str) -> bool:\n"
                        "        return token == 'ok'"
                    ),
                    "new_string": (
                        "    def verify(self, token: str) -> bool:\n"
                        "        return token.startswith('ok')"
                    ),
                }
            ]
        }
        line_edit_payload = tool_smart_edit(line_edit_request)
        if line_edit_payload.get("failed"):
            raise AssertionError(f"line-edit baseline failed: {line_edit_payload['failed']}")
        total = (
            count_tokens(json.dumps(search_payload, sort_keys=True, default=str))
            + count_tokens(json.dumps(read_payload, sort_keys=True, default=str))
            + count_tokens(json.dumps(line_edit_request, sort_keys=True, default=str))
            + count_tokens(json.dumps(line_edit_payload, sort_keys=True, default=str))
        )
        return total, str(line_edit_payload["applied"][0]["path"])


def _measure_symbol_edit_tokens(repo_root: Path) -> tuple[int, str]:
    with _workspace_env(repo_root, repo_root / ".atelier"):
        symbol_edit_request = {
            "edits": [
                {
                    "kind": "symbol",
                    "name": "AuthService.verify",
                    "mode": "replace",
                    "new_body": (
                        "def verify(self, token: str) -> bool:\n"
                        "    return token.startswith('ok')"
                    ),
                }
            ]
        }
        symbol_edit_payload = tool_smart_edit(symbol_edit_request)
        if symbol_edit_payload.get("failed"):
            raise AssertionError(f"symbol-edit benchmark failed: {symbol_edit_payload['failed']}")
        total = count_tokens(json.dumps(symbol_edit_request, sort_keys=True, default=str)) + count_tokens(
            json.dumps(symbol_edit_payload, sort_keys=True, default=str)
        )
        return total, str(symbol_edit_payload["applied"][0]["path"])


def run_symbol_edit_bench(work_dir: Path | None = None) -> SymbolEditBenchResult:
    """Compare symbol-edit tokens against the read-plus-line-edit baseline."""

    bench_root = (work_dir or Path.cwd()) / "code_intel_symbol_edit"
    baseline_repo_root = bench_root / "baseline_fixture_repo"
    symbol_repo_root = bench_root / "symbol_fixture_repo"
    _write_fixture_repo(baseline_repo_root)
    _write_fixture_repo(symbol_repo_root)

    baseline_total_tokens, _ = _measure_line_edit_baseline(baseline_repo_root)
    symbol_edit_total_tokens, edited_path = _measure_symbol_edit_tokens(symbol_repo_root)
    ratio = symbol_edit_total_tokens / baseline_total_tokens if baseline_total_tokens else 0.0
    return SymbolEditBenchResult(
        symbol_edit_total_tokens=symbol_edit_total_tokens,
        baseline_total_tokens=baseline_total_tokens,
        token_ratio=ratio,
        edited_path=edited_path,
    )


__all__ = ["SymbolEditBenchResult", "run_symbol_edit_bench"]
