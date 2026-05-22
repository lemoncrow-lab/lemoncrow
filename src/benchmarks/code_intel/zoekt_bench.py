"""Public-search benchmark smoke for the Phase 5 Zoekt routing path."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from atelier.core.foundation.models import Trace
from atelier.core.foundation.store import ContextStore
from atelier.gateway.adapters.mcp_server import tool_smart_search
from atelier.infra.code_intel.zoekt.adapter import reset_zoekt_supervisors


@dataclass(frozen=True)
class ZoektBenchResult:
    query: str
    budget_tokens: int
    baseline_total_tokens: int
    zoekt_total_tokens: int
    baseline_latency_ns: int
    warm_latency_ns: int
    speedup_ratio: float
    backend: str
    index_age_seconds: int | None
    within_budget: bool
    trace_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "budget_tokens": self.budget_tokens,
            "baseline_total_tokens": self.baseline_total_tokens,
            "zoekt_total_tokens": self.zoekt_total_tokens,
            "baseline_latency_ns": self.baseline_latency_ns,
            "warm_latency_ns": self.warm_latency_ns,
            "speedup_ratio": self.speedup_ratio,
            "backend": self.backend,
            "index_age_seconds": self.index_age_seconds,
            "within_budget": self.within_budget,
            "trace_id": self.trace_id,
        }


@contextmanager
def _workspace_env(workspace_root: Path, atelier_root: Path) -> Iterator[None]:
    prior = {name: os.environ.get(name) for name in ("CLAUDE_WORKSPACE_ROOT", "ATELIER_ROOT", "ATELIER_CACHE_DISABLED")}
    os.environ["CLAUDE_WORKSPACE_ROOT"] = str(workspace_root)
    os.environ["ATELIER_ROOT"] = str(atelier_root)
    os.environ["ATELIER_CACHE_DISABLED"] = "1"
    try:
        yield
    finally:
        for name, value in prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)


@contextmanager
def _env_override(**values: str) -> Iterator[None]:
    prior = [(name, os.environ.get(name)) for name in values]
    for name, value in values.items():
        os.environ[name] = value
    try:
        yield
    finally:
        for name, previous_value in prior:
            if previous_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous_value


def _write_fixture_repo(repo_root: Path, *, files: int = 1500, lines_per_file: int = 300) -> None:
    src = repo_root / "src"
    src.mkdir(parents=True, exist_ok=True)
    target_line = "def zoekt_target() -> str: return 'needle token benchmark target'\n"
    filler = "def helper_{index}_{line}() -> str: return 'filler content for zoekt benchmark'\n"
    for index in range(files):
        body = [target_line]
        body.extend(filler.format(index=index, line=line) for line in range(lines_per_file))
        (src / f"module_{index}.py").write_text("".join(body), encoding="utf-8")


def _measure_average_ns(func: Callable[[], dict[str, Any]], *, iterations: int) -> tuple[int, dict[str, Any]]:
    payload: dict[str, Any] = {}
    start = perf_counter_ns()
    for _ in range(iterations):
        payload = func()
    elapsed = perf_counter_ns() - start
    return max(1, elapsed // iterations), payload


def _record_trace(atelier_root: Path, *, speedup_ratio: float, backend: str) -> str:
    trace = Trace(
        id=Trace.make_id("M16 zoekt bench", "gsd-executor"),
        agent="gsd-executor",
        domain="code-intel",
        task="Validate M16 large-repo search routing",
        status="success",
        files_touched=[
            "docs/plans/active/code-intel/M16-zoekt-scale.md",
            "docs/plans/active/code-intel/M12-token-budget.md",
        ],
        output_summary=(
            f"Validated warm public-search Zoekt routing at {speedup_ratio:.2f}x baseline speed "
            f"with backend={backend} and additive index metadata."
        ),
        created_at=datetime.now(UTC),
    )
    store = ContextStore(atelier_root)
    store.init()
    store.record_trace(trace)
    return trace.id


def _search_payload(repo_root: Path, *, query: str, budget_tokens: int) -> dict[str, Any]:
    return dict(
        tool_smart_search(
            {
                "query": query,
                "path": str(repo_root),
                "budget_tokens": budget_tokens,
                "max_files": 1,
                "include_outline": False,
            }
        )
    )


def run_zoekt_bench(
    work_dir: Path | None = None,
    *,
    query: str = "needle token benchmark target",
    budget_tokens: int = 4000,
) -> ZoektBenchResult:
    bench_root = (work_dir or Path.cwd()) / "code_intel_zoekt"
    repo_root = bench_root / "fixture_repo"
    atelier_root = bench_root / ".atelier"
    _write_fixture_repo(repo_root)
    reset_zoekt_supervisors()
    with _workspace_env(repo_root, atelier_root):
        with _env_override(ATELIER_ZOEKT_LOC_THRESHOLD="99999999"):
            baseline_latency_ns, baseline_payload = _measure_average_ns(
                lambda: _search_payload(repo_root, query=query, budget_tokens=budget_tokens),
                iterations=3,
            )
        reset_zoekt_supervisors()
        with _env_override(ATELIER_ZOEKT_LOC_THRESHOLD="20"):
            _search_payload(repo_root, query=query, budget_tokens=budget_tokens)
            warm_latency_ns, zoekt_payload = _measure_average_ns(
                lambda: _search_payload(repo_root, query=query, budget_tokens=budget_tokens),
                iterations=12,
            )
        speedup_ratio = baseline_latency_ns / max(1, warm_latency_ns)
        trace_id = _record_trace(
            atelier_root,
            speedup_ratio=speedup_ratio,
            backend=str(zoekt_payload.get("backend") or ""),
        )
    return ZoektBenchResult(
        query=query,
        budget_tokens=budget_tokens,
        baseline_total_tokens=int(baseline_payload.get("total_tokens", 0) or 0),
        zoekt_total_tokens=int(zoekt_payload.get("total_tokens", 0) or 0),
        baseline_latency_ns=baseline_latency_ns,
        warm_latency_ns=warm_latency_ns,
        speedup_ratio=speedup_ratio,
        backend=str(zoekt_payload.get("backend") or ""),
        index_age_seconds=(
            int(zoekt_payload["index_age_seconds"]) if zoekt_payload.get("index_age_seconds") is not None else None
        ),
        within_budget=int(zoekt_payload.get("total_tokens", 0) or 0) <= budget_tokens,
        trace_id=trace_id,
    )


__all__ = ["ZoektBenchResult", "run_zoekt_bench"]
