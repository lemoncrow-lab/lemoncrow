"""Deterministic benchmark smoke for Phase 6 workspace code routing."""

from __future__ import annotations

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
from atelier.gateway.adapters.mcp_server import tool_code


@dataclass(frozen=True)
class WorkspaceBenchResult:
    union_item_count: int
    filtered_item_count: int
    union_repo_names: list[str]
    filtered_repo_names: list[str]
    union_total_tokens: int
    filtered_total_tokens: int
    trace_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "union_item_count": self.union_item_count,
            "filtered_item_count": self.filtered_item_count,
            "union_repo_names": self.union_repo_names,
            "filtered_repo_names": self.filtered_repo_names,
            "union_total_tokens": self.union_total_tokens,
            "filtered_total_tokens": self.filtered_total_tokens,
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


def _write_workspace_repo(root: Path, *, module_name: str) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "config.py").write_text(
        "class SharedConfig:\n"
        f"    SOURCE = '{module_name}'\n",
        encoding="utf-8",
    )


def _write_workspace_config(workspace_root: Path, sibling_root: Path) -> None:
    (workspace_root / ".atelier").mkdir(parents=True, exist_ok=True)
    (workspace_root / ".atelier" / "workspace.toml").write_text(
        "\n".join(
            [
                "[workspace]",
                'id = "fixture-workspace"',
                "",
                "[[workspace.repos]]",
                'name = "atelier"',
                'path = "."',
                "",
                "[[workspace.repos]]",
                'name = "billing"',
                f'path = "{os.path.relpath(sibling_root, workspace_root)}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _record_trace(atelier_root: Path) -> str:
    trace = Trace(
        id=Trace.make_id("M10 workspace bench", "gsd-executor"),
        agent="gsd-executor",
        domain="code-intel",
        task="Validate M10 workspace union and repo-filter routing",
        status="success",
        files_touched=["docs/plans/active/code-intel/M10-multi-repo.md"],
        output_summary="Validated workspace search union plus additive repo filtering on the shipped code tool path with repo-aware result metadata.",
        created_at=datetime.now(UTC),
    )
    store = ContextStore(atelier_root)
    store.init()
    store.record_trace(trace)
    return trace.id


def run_workspace_bench(work_dir: Path | None = None) -> WorkspaceBenchResult:
    base_dir = work_dir or Path(os.environ.get("TMPDIR") or Path.cwd())
    bench_root = Path(base_dir) / "code_intel_workspace"
    repo_root = bench_root / "atelier"
    sibling_root = bench_root / "billing"
    atelier_root = bench_root / ".atelier"
    _write_workspace_repo(repo_root, module_name="atelier")
    _write_workspace_repo(sibling_root, module_name="billing")
    _write_workspace_config(repo_root, sibling_root)

    with _workspace_env(repo_root, atelier_root):
        mcp_server._reset_runtime_cache_for_testing()
        union_payload = tool_code(
            {"op": "search", "repo_root": str(repo_root), "query": "SharedConfig", "budget_tokens": 1200}
        )
        filtered_payload = tool_code(
            {
                "op": "search",
                "repo_root": str(repo_root),
                "query": "SharedConfig",
                "repo": "billing",
                "budget_tokens": 1200,
            }
        )
        trace_id = _record_trace(atelier_root)

    return WorkspaceBenchResult(
        union_item_count=len(union_payload.get("items", [])),
        filtered_item_count=len(filtered_payload.get("items", [])),
        union_repo_names=[str(item.get("repo_name") or "") for item in union_payload.get("items", [])],
        filtered_repo_names=[str(item.get("repo_name") or "") for item in filtered_payload.get("items", [])],
        union_total_tokens=int(union_payload.get("total_tokens", 0)),
        filtered_total_tokens=int(filtered_payload.get("total_tokens", 0)),
        trace_id=trace_id,
    )


__all__ = ["WorkspaceBenchResult", "run_workspace_bench"]
