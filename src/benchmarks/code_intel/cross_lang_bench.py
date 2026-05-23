"""Deterministic benchmark smoke for Phase 5 cross-language edges."""

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
from atelier.gateway.adapters.mcp_server import tool_code, tool_smart_read, tool_smart_search
from atelier.infra.code_intel.cross_lang.runner import CrossLangRunner


@dataclass(frozen=True)
class CrossLangBenchResult:
    symbol_total_tokens: int
    unresolved_symbol_total_tokens: int
    usages_total_tokens: int
    baseline_total_tokens: int
    combined_total_tokens: int
    reference_count: int
    resolved_edge_seen: bool
    unresolved_edge_seen: bool
    within_budget: bool
    trace_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol_total_tokens": self.symbol_total_tokens,
            "unresolved_symbol_total_tokens": self.unresolved_symbol_total_tokens,
            "usages_total_tokens": self.usages_total_tokens,
            "baseline_total_tokens": self.baseline_total_tokens,
            "combined_total_tokens": self.combined_total_tokens,
            "reference_count": self.reference_count,
            "resolved_edge_seen": self.resolved_edge_seen,
            "unresolved_edge_seen": self.unresolved_edge_seen,
            "within_budget": self.within_budget,
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
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "plugins" / "__init__.py").write_text("", encoding="utf-8")
    (root / "plugins" / "worker.py").write_text(
        "def plugin_entry() -> str:\n" "    return 'worker'\n",
        encoding="utf-8",
    )
    (root / "scripts" / "worker.py").write_text(
        "def main() -> int:\n" "    return 1\n",
        encoding="utf-8",
    )
    (root / "src" / "local_worker.py").write_text(
        "from scripts.worker import main\n\n" "def call_local() -> int:\n" "    return main()\n",
        encoding="utf-8",
    )
    (root / "src" / "bootstrap.py").write_text(
        "import subprocess\n\n"
        "def launch_worker() -> None:\n"
        "    subprocess.run(['python', 'scripts/worker.py'], check=False)\n",
        encoding="utf-8",
    )
    (root / "src" / "ffi_user.py").write_text(
        "import cffi\n\n"
        "def soft_native() -> str:\n"
        "    ffi = cffi.FFI()\n"
        "    ffi.cdef('int soft_missing(int value);')\n"
        "    return 'soft'\n",
        encoding="utf-8",
    )


def _seed_cross_lang_edges(repo_root: Path) -> None:
    engine = CodeContextEngine(repo_root)
    engine.index_repo()
    CrossLangRunner(repo_root=repo_root, repo_id=engine.repo_id, connection_factory=engine.connection).resolve_all()


def _record_trace(atelier_root: Path) -> str:
    trace = Trace(
        id=Trace.make_id("M17 cross-lang bench", "gsd-executor"),
        agent="gsd-executor",
        domain="code-intel",
        task="Validate M17 literal-only cross-language edges",
        status="success",
        files_touched=["docs/plans/active/code-intel/M17-cross-lang.md"],
        output_summary="Validated additive symbol/usages cross-language payloads against the M17 fixture benchmark.",
        created_at=datetime.now(UTC),
    )
    store = ContextStore(atelier_root)
    store.init()
    store.record_trace(trace)
    return trace.id


def _baseline_tokens(repo_root: Path) -> int:
    search_payload = tool_smart_search({"query": "worker", "path": str(repo_root), "budget_tokens": 4000})
    unique_paths = sorted({str(match["path"]) for match in search_payload.get("matches", [])})
    read_tokens = 0
    for path in unique_paths:
        read_payload = tool_smart_read({"path": path, "max_lines": 40})
        read_tokens += count_tokens(json.dumps(read_payload, sort_keys=True, default=str))
    return count_tokens(json.dumps(search_payload, sort_keys=True, default=str)) + read_tokens


def run_cross_lang_bench(
    work_dir: Path | None = None,
    *,
    symbol_budget_tokens: int = 320,
    usages_budget_tokens: int = 360,
) -> CrossLangBenchResult:
    bench_root = (work_dir or Path.cwd()) / "code_intel_cross_lang"
    repo_root = bench_root / "fixture_repo"
    atelier_root = bench_root / ".atelier"
    _write_fixture_repo(repo_root)
    with _workspace_env(repo_root, atelier_root):
        _seed_cross_lang_edges(repo_root)
        trace_id = _record_trace(atelier_root)
        resolved_symbol_payload = tool_code(
            {
                "op": "symbol",
                "repo_root": str(repo_root),
                "qualified_name": "launch_worker",
                "file_path": "src/bootstrap.py",
                "budget_tokens": symbol_budget_tokens,
            }
        )
        unresolved_symbol_payload = tool_code(
            {
                "op": "symbol",
                "repo_root": str(repo_root),
                "qualified_name": "soft_native",
                "file_path": "src/ffi_user.py",
                "budget_tokens": symbol_budget_tokens,
            }
        )
        usages_payload = tool_code(
            {
                "op": "usages",
                "repo_root": str(repo_root),
                "symbol_name": "main",
                "file_path": "scripts/worker.py",
                "budget_tokens": usages_budget_tokens,
            }
        )
        baseline_total_tokens = _baseline_tokens(repo_root)

    symbol_total_tokens = int(resolved_symbol_payload.get("total_tokens", 0) or 0)
    unresolved_total_tokens = int(unresolved_symbol_payload.get("total_tokens", 0) or 0)
    usages_total_tokens = int(usages_payload.get("total_tokens", 0) or 0)
    combined_total_tokens = symbol_total_tokens + unresolved_total_tokens + usages_total_tokens
    resolved_refs = resolved_symbol_payload.get("cross_lang_refs", [])
    unresolved_refs = unresolved_symbol_payload.get("cross_lang_refs", [])
    return CrossLangBenchResult(
        symbol_total_tokens=symbol_total_tokens,
        unresolved_symbol_total_tokens=unresolved_total_tokens,
        usages_total_tokens=usages_total_tokens,
        baseline_total_tokens=baseline_total_tokens,
        combined_total_tokens=combined_total_tokens,
        reference_count=int(usages_payload.get("reference_count", 0) or 0),
        resolved_edge_seen=any(
            ref.get("edge_kind") == "subprocess" and ref.get("confidence", 0) >= 0.7 for ref in resolved_refs
        ),
        unresolved_edge_seen=any(
            ref.get("edge_kind") == "ffi_cffi" and ref.get("confidence", 1) < 0.6 and not ref.get("symbol_id")
            for ref in unresolved_refs
        ),
        within_budget=(
            symbol_total_tokens <= symbol_budget_tokens
            and unresolved_total_tokens <= symbol_budget_tokens
            and usages_total_tokens <= usages_budget_tokens
        ),
        trace_id=trace_id,
    )


__all__ = ["CrossLangBenchResult", "run_cross_lang_bench"]
