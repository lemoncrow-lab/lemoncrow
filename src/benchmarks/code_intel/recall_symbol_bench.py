"""Deterministic benchmark for the M7 symbol-linked recall workflow."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.core.foundation.memory_models import ArchivalPassage, MemoryBlock
from atelier.core.foundation.models import Trace, to_jsonable
from atelier.core.foundation.store import ContextStore
from atelier.gateway.adapters.mcp_server import tool_code, tool_memory
from atelier.infra.storage.sqlite_memory_store import SqliteMemoryStore


@dataclass(frozen=True)
class RecallSymbolBenchResult:
    """Summary of the M7 recall bundle token comparison."""

    budget_tokens: int
    expanded_budget_tokens: int
    default_total_tokens: int
    expanded_total_tokens: int
    baseline_total_tokens: int
    default_within_budget: bool
    definition_preserved: bool
    default_included: list[str]
    expanded_included: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_tokens": self.budget_tokens,
            "expanded_budget_tokens": self.expanded_budget_tokens,
            "default_total_tokens": self.default_total_tokens,
            "expanded_total_tokens": self.expanded_total_tokens,
            "baseline_total_tokens": self.baseline_total_tokens,
            "default_within_budget": self.default_within_budget,
            "definition_preserved": self.definition_preserved,
            "default_included": self.default_included,
            "expanded_included": self.expanded_included,
        }


@contextmanager
def _workspace_env(workspace_root: Path, atelier_root: Path) -> Iterator[None]:
    old_workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT")
    old_atelier = os.environ.get("ATELIER_ROOT")
    old_dev = os.environ.get("ATELIER_DEV_MODE")
    os.environ["CLAUDE_WORKSPACE_ROOT"] = str(workspace_root)
    os.environ["ATELIER_ROOT"] = str(atelier_root)
    os.environ["ATELIER_DEV_MODE"] = "1"
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
        if old_dev is None:
            os.environ.pop("ATELIER_DEV_MODE", None)
        else:
            os.environ["ATELIER_DEV_MODE"] = old_dev


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "decisions").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "auth.py").write_text(
        "class AuthService:\n"
        "    def verify_session(self, token: str) -> bool:\n"
        "        \"\"\"Validate session tokens before protected operations.\"\"\"\n"
        "        return token.startswith('session:')\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_auth.py").write_text(
        "from src.auth import AuthService\n\n"
        "def test_verify_session_accepts_session_tokens() -> None:\n"
        "    assert AuthService().verify_session('session:ok') is True\n",
        encoding="utf-8",
    )
    (root / "docs" / "decisions" / "001-session-auth.md").write_text(
        "# Session auth\n\n"
        "AuthService.verify_session remains the session token validation seam after the stale-session review.\n",
        encoding="utf-8",
    )


def _seed_symbol_linked_context(repo_root: Path, atelier_root: Path) -> None:
    engine = CodeContextEngine(repo_root)
    engine.index_repo()
    symbol_id = engine.search_symbols("AuthService.verify_session", limit=1)[0].symbol_id

    memory_store = SqliteMemoryStore(atelier_root)
    memory_store.upsert_block(
        MemoryBlock(
            agent_id="shared",
            label="edits/auth-verify-session",
            value="Tightened AuthService.verify_session after a stale-session incident.",
            metadata={"symbol_id": symbol_id, "origin": "edit"},
        ),
        actor="agent:benchmark",
        reason="seed benchmark memory",
    )
    memory_store.insert_passage(
        ArchivalPassage(
            agent_id="shared",
            text="AuthService.verify_session rejected stale sessions during the incident review.",
            tags=[f"symbol:{symbol_id}", "incident"],
            source="user",
            source_ref="postmortem/auth.md",
            dedup_hash="recall-symbol-bench",
        )
    )

    trace_store = ContextStore(atelier_root)
    trace_store.init()
    trace_store.record_trace(
        Trace(
            id=Trace.make_id("M7 recall bench", "gsd-executor"),
            agent="gsd-executor",
            domain="code-intel",
            task="Validate AuthService.verify_session recall",
            status="success",
            files_touched=["src/auth.py"],
            output_summary="Validated AuthService.verify_session during stale-session mitigation.",
            created_at=datetime.now(UTC),
        )
    )


def _manual_baseline_tokens(repo_root: Path, atelier_root: Path) -> int:
    symbol_request = {
        "op": "symbol",
        "repo_root": str(repo_root),
        "qualified_name": "AuthService.verify_session",
        "budget_tokens": 4000,
    }
    symbol_payload = tool_code(symbol_request)
    memory_request = {
        "op": "recall",
        "agent_id": "shared",
        "query": "AuthService.verify_session",
        "top_k": 5,
    }
    memory_payload = tool_memory(memory_request)
    trace_store = ContextStore(atelier_root)
    trace_store.init()
    trace_payload = [to_jsonable(trace) for trace in trace_store.list_traces(limit=20)]
    decision_payload = {
        str(path.relative_to(repo_root).as_posix()): path.read_text(encoding="utf-8")
        for path in (repo_root / "docs" / "decisions").rglob("*.md")
    }
    tests_payload = {
        str(path.relative_to(repo_root).as_posix()): path.read_text(encoding="utf-8")
        for path in (repo_root / "tests").rglob("*.py")
    }
    parts = [
        symbol_request,
        symbol_payload,
        memory_request,
        memory_payload,
        trace_payload,
        decision_payload,
        tests_payload,
    ]
    return sum(count_tokens(json.dumps(part, sort_keys=True, ensure_ascii=False, default=str)) for part in parts)


def run_recall_symbol_bench(
    work_dir: Path | None = None,
    *,
    budget_tokens: int = 520,
    expanded_budget_tokens: int = 1800,
) -> RecallSymbolBenchResult:
    """Compare default recall_symbol tokens against expanded and manual baselines."""

    bench_root = (work_dir or Path.cwd()) / "code_intel_recall_symbol"
    repo_root = bench_root / "fixture_repo"
    atelier_root = bench_root / ".atelier"
    _write_fixture_repo(repo_root)
    with _workspace_env(repo_root, atelier_root):
        _seed_symbol_linked_context(repo_root, atelier_root)

        default_request = {
            "op": "recall_symbol",
            "agent_id": "shared",
            "query": "AuthService.verify_session",
            "budget_tokens": budget_tokens,
        }
        default_payload = tool_memory(default_request)
        expanded_request = {
            "op": "recall_symbol",
            "agent_id": "shared",
            "query": "AuthService.verify_session",
            "include": ["traces", "decisions", "tests"],
            "budget_tokens": expanded_budget_tokens,
        }
        expanded_payload = tool_memory(expanded_request)
        baseline_total_tokens = _manual_baseline_tokens(repo_root, atelier_root)

    default_total_tokens = count_tokens(json.dumps(default_payload, sort_keys=True, ensure_ascii=False, default=str))
    expanded_total_tokens = count_tokens(json.dumps(expanded_payload, sort_keys=True, ensure_ascii=False, default=str))
    return RecallSymbolBenchResult(
        budget_tokens=budget_tokens,
        expanded_budget_tokens=expanded_budget_tokens,
        default_total_tokens=default_total_tokens,
        expanded_total_tokens=expanded_total_tokens,
        baseline_total_tokens=baseline_total_tokens,
        default_within_budget=int(default_payload["total_tokens"]) <= budget_tokens,
        definition_preserved=default_payload["definition"]["qualified_name"] == "AuthService.verify_session",
        default_included=list(default_payload["included"]),
        expanded_included=list(expanded_payload["included"]),
    )


__all__ = ["RecallSymbolBenchResult", "run_recall_symbol_bench"]
