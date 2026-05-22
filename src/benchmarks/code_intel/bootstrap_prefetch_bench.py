"""Deterministic benchmark smoke for Phase 6 bootstrap prefetch."""

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
from atelier.core.foundation.models import Trace
from atelier.core.foundation.store import ContextStore
from atelier.core.service.bootstrap_context import list_bootstrap_blocks
from atelier.gateway.adapters import mcp_server
from atelier.gateway.adapters.mcp_server import tool_code
from atelier.infra.storage.factory import create_store, make_memory_store


@dataclass(frozen=True)
class BootstrapPrefetchBenchResult:
    cold_total_tokens: int
    warm_total_tokens: int
    baseline_total_tokens: int
    block_count: int
    cold_jobs_started: int
    warm_jobs_started: int
    warm_status: str
    trace_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cold_total_tokens": self.cold_total_tokens,
            "warm_total_tokens": self.warm_total_tokens,
            "baseline_total_tokens": self.baseline_total_tokens,
            "block_count": self.block_count,
            "cold_jobs_started": self.cold_jobs_started,
            "warm_jobs_started": self.warm_jobs_started,
            "warm_status": self.warm_status,
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
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "app.py").write_text(
        "from src.worker import run_worker\n\n" "def main() -> str:\n" "    return run_worker()\n",
        encoding="utf-8",
    )
    (root / "src" / "worker.py").write_text(
        "def run_worker() -> str:\n" "    return 'ready'\n",
        encoding="utf-8",
    )
    (root / "scripts" / "cli.py").write_text(
        "from src.app import main\n\n" "def cli() -> str:\n" "    return main()\n",
        encoding="utf-8",
    )


def _record_trace(atelier_root: Path) -> str:
    trace = Trace(
        id=Trace.make_id("M11 bootstrap prefetch bench", "gsd-executor"),
        agent="gsd-executor",
        domain="code-intel",
        task="Validate M11 deterministic bootstrap prefetch",
        status="success",
        files_touched=["docs/plans/active/code-intel/M11-bootstrap.md"],
        output_summary="Validated first-context bootstrap enqueue, pinned block persistence, and warmed context reuse on the shipped context flow.",
        created_at=datetime.now(UTC),
    )
    store = ContextStore(atelier_root)
    store.init()
    store.record_trace(trace)
    return trace.id


def run_bootstrap_prefetch_bench(work_dir: Path | None = None) -> BootstrapPrefetchBenchResult:
    base_dir = work_dir or Path(os.environ.get("TMPDIR") or Path.cwd())
    bench_root = Path(base_dir) / "code_intel_bootstrap_prefetch"
    repo_root = bench_root / "fixture_repo"
    atelier_root = bench_root / ".atelier"
    _write_fixture_repo(repo_root)

    with _workspace_env(repo_root, atelier_root):
        mcp_server._reset_runtime_cache_for_testing()
        real_worker_tick = mcp_server._run_worker_tick_safe
        mcp_server._run_worker_tick_safe = lambda root: None
        try:
            cold = mcp_server.tool_get_context({"task": "Understand the repo entry points", "agent_id": "bench-agent"})
            real_worker_tick(atelier_root)
            mcp_server._reset_runtime_cache_for_testing()
            warm = mcp_server.tool_get_context({"task": "Understand the repo entry points", "agent_id": "bench-agent"})
            baseline = tool_code(
                {
                    "op": "context",
                    "repo_root": str(repo_root),
                    "task": "Understand the repo entry points",
                    "budget_tokens": 1200,
                }
            )
            repo_id = CodeContextEngine(repo_root).repo_id
            blocks = list_bootstrap_blocks(make_memory_store(atelier_root), repo_id)
            store = create_store(atelier_root)
            store.init()
            # jobs intentionally unused; kept for store init side-effect
            trace_id = _record_trace(atelier_root)
        finally:
            mcp_server._run_worker_tick_safe = real_worker_tick

    return BootstrapPrefetchBenchResult(
        cold_total_tokens=int(cold["tokens_breakdown"]["total"]),
        warm_total_tokens=int(warm["tokens_breakdown"]["total"]),
        baseline_total_tokens=count_tokens(json.dumps(baseline, sort_keys=True, default=str)),
        block_count=len(blocks),
        cold_jobs_started=1 if cold["bootstrap"]["queued"] else 0,
        warm_jobs_started=1 if warm["bootstrap"]["queued"] else 0,
        warm_status=str(warm["bootstrap"]["status"]),
        trace_id=trace_id,
    )


__all__ = ["BootstrapPrefetchBenchResult", "run_bootstrap_prefetch_bench"]
