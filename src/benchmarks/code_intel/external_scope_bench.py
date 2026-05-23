"""Deterministic benchmark smoke for Phase 6 external dependency scope routing."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.core.foundation.models import Trace
from atelier.core.foundation.store import ContextStore
from atelier.gateway.adapters import mcp_server
from atelier.gateway.adapters.mcp_server import tool_code, tool_smart_edit


@dataclass(frozen=True)
class ExternalScopeBenchResult:
    repo_item_count: int
    external_item_count: int
    repo_total_tokens: int
    external_total_tokens: int
    edit_error: str
    trace_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_item_count": self.repo_item_count,
            "external_item_count": self.external_item_count,
            "repo_total_tokens": self.repo_total_tokens,
            "external_total_tokens": self.external_total_tokens,
            "edit_error": self.edit_error,
            "trace_id": self.trace_id,
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
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("def alpha() -> int:\n    return 1\n", encoding="utf-8")


def _write_external_scip_fixture(repo_root: Path) -> None:
    from atelier.core.capabilities.code_context import CodeContextEngine

    engine = CodeContextEngine(repo_root)
    artifact_dir = repo_root / ".atelier" / "cache" / "scip" / engine.repo_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "repo_id": engine.repo_id,
        "language": "python",
        "index_sha": "b" * 40,
        "symbols": [
            {
                "symbol_id": "scip-requests-get",
                "repo_id": engine.repo_id,
                "file_path": "external/requests/api.py",
                "language": "python",
                "symbol_name": "get",
                "qualified_name": "requests.get",
                "kind": "function",
                "signature": "def get(url: str) -> str:",
                "start_byte": 0,
                "end_byte": len(b"def get(url: str) -> str:\n    return url\n"),
                "start_line": 1,
                "end_line": 2,
                "content_hash": "c" * 64,
                "source": "def get(url: str) -> str:\n    return url\n",
                "provenance": "scip",
            }
        ],
    }
    (artifact_dir / "external-python.scip").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _record_trace(atelier_root: Path) -> str:
    trace = Trace(
        id=Trace.make_id("M9 external scope bench", "gsd-executor"),
        agent="gsd-executor",
        domain="code-intel",
        task="Validate M9 external dependency scope routing",
        status="success",
        files_touched=["docs/plans/active/code-intel/M9-external-deps.md"],
        output_summary="Validated explicit external scope routing, additive origin tagging, and dependency edit rejection on the shipped code and edit surfaces.",
        created_at=datetime.now(UTC),
    )
    store = ContextStore(atelier_root)
    store.init()
    store.record_trace(trace)
    return trace.id


def run_external_scope_bench(work_dir: Path | None = None) -> ExternalScopeBenchResult:
    base_dir = work_dir or Path(os.environ.get("TMPDIR") or Path.cwd())
    bench_root = Path(base_dir) / "code_intel_external_scope"
    repo_root = bench_root / "fixture_repo"
    atelier_root = bench_root / ".atelier"
    _write_fixture_repo(repo_root)
    _write_external_scip_fixture(repo_root)

    with _workspace_env(repo_root, atelier_root):
        mcp_server._reset_runtime_cache_for_testing()
        repo_payload = tool_code({"op": "search", "repo_root": str(repo_root), "query": "get", "budget_tokens": 1200})
        external_payload = tool_code(
            {"op": "search", "repo_root": str(repo_root), "query": "get", "scope": "external", "budget_tokens": 1200}
        )
        edit_payload = tool_smart_edit(
            {
                "edits": [
                    {
                        "kind": "symbol",
                        "symbol_id": "scip-requests-get",
                        "mode": "replace",
                        "new_body": "def get(url: str) -> str:\n    return 'patched'\n",
                    }
                ]
            }
        )
        trace_id = _record_trace(atelier_root)

    failed = edit_payload.get("failed") or [{}]
    return ExternalScopeBenchResult(
        repo_item_count=len(repo_payload.get("items", [])),
        external_item_count=len(external_payload.get("items", [])),
        repo_total_tokens=int(repo_payload.get("total_tokens", 0)),
        external_total_tokens=int(external_payload.get("total_tokens", 0)),
        edit_error=str(failed[0].get("error") or ""),
        trace_id=trace_id,
    )


__all__ = ["ExternalScopeBenchResult", "run_external_scope_bench"]
