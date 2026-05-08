"""FastAPI HTTP wrapper for the Atelier reasoning runtime.

Read-only dashboard API exposing the contents of a ``ReasoningStore``.

Endpoints (all GET):
  /healthz                     liveness probe
  /overview                    aggregate token / cost / counts
  /tokens                      per-trace token + savings estimates
  /plans                       recent PlanCheckResult entries
  /traces                      list traces (filterable by status/domain)
  /traces/{trace_id}           full trace detail with reasoning chain
  /cost                        per-cycle and per-reasoning cost estimates
  /clusters                    failure clusters
  /environments                loaded environments + linked rubrics
  /blocks                      reusable ReasonBlocks (the "memory")
  /blocks/{block_id}           single block
  /savings                     aggregate cost savings metrics
  /calls                       per-call detailed cost log

The cost/token model is **estimated** from observable trace content
(no provider billing integration). Estimates are documented per-field
so the UI can label them as such.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from atelier.core.foundation.paths import default_store_root, resolve_workspace_root
from atelier.core.foundation.environments import load_environments_from_dir
from atelier.core.foundation.memory_models import ArchivalPassage, MemoryBlock
from atelier.core.foundation.models import (
    CommandRecord,
    Environment,
    FailureCluster,
    FileEditRecord,
    ReasonBlock,
    Rubric,
    Trace,
)
from atelier.core.foundation.store import ReasoningStore
from atelier.core.improvement.failure_analyzer import FailureAnalyzer
from atelier.core.service.telemetry import (
    emit_product,
    emit_product_local,
    init_product_telemetry,
    set_remote_enabled,
)
from atelier.core.service.telemetry.banner import mark_acknowledged
from atelier.core.service.telemetry.config import save_telemetry_config
from atelier.core.service.telemetry.exporters.posthog_frontend import frontend_telemetry_config
from atelier.core.service.telemetry.local_store import LocalTelemetryStore
from atelier.core.service.telemetry.schema import bucket_duration_ms, schema_dump
from atelier.infra.runtime.cost_tracker import CostTracker

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

DEFAULT_WORKSPACE = resolve_workspace_root()
DEFAULT_STORE_ROOT = Path(os.environ.get("ATELIER_STORE_ROOT", str(default_store_root()))).resolve()
DEFAULT_ENV_DIR = Path(
    os.environ.get(
        "ATELIER_ENV_DIR",
        Path(__file__).resolve().parent.parent / "environments",
    )
).resolve()

# Heuristic token cost: 4 chars ≈ 1 token (OpenAI rule of thumb).
CHARS_PER_TOKEN = 4


# Model-aware per-1K-token USD rate.
# Resolution order (first non-empty wins):
#   1. ATELIER_USD_PER_1K_TOKENS env var  (explicit override — any model)
#   2. ATELIER_MODEL env var → looked up in model_pricing.toml
#   3. [default] entry in model_pricing.toml (sonnet-class)
def _resolve_usd_per_1k() -> float:
    explicit = os.environ.get("ATELIER_USD_PER_1K_TOKENS", "")
    if explicit:
        try:
            return float(explicit)
        except ValueError:
            pass
    from atelier.core.capabilities.pricing import active_model, get_model_pricing

    return get_model_pricing(active_model()).output / 1000.0


USD_PER_1K_TOKENS: float = _resolve_usd_per_1k()

# --------------------------------------------------------------------------- #
# Response schemas                                                            #
# --------------------------------------------------------------------------- #


class TokenStats(BaseModel):
    """Estimated token + savings for a single trace."""

    trace_id: str
    domain: str
    agent: str
    status: str
    raw_tokens_estimate: int
    compressed_tokens_estimate: int
    saved_tokens_estimate: int
    compression_ratio: float
    is_estimate: bool = True


class OverviewStats(BaseModel):
    total_traces: int
    total_blocks: int
    total_rubrics: int
    total_environments: int
    total_clusters: int
    total_raw_tokens_estimate: int
    total_saved_tokens_estimate: int
    total_compressed_tokens_estimate: int
    average_compression_ratio: float
    estimated_total_cost_usd: float
    estimated_saved_cost_usd: float
    usd_per_1k_tokens: float
    is_estimate: bool = True


class CostEntry(BaseModel):
    trace_id: str
    domain: str
    agent: str
    cycle_count: int
    raw_tokens_estimate: int
    compressed_tokens_estimate: int
    cost_per_cycle_usd: float
    cost_total_usd: float
    is_estimate: bool = True


class PlanRecord(BaseModel):
    """Wrapper around a Trace's validation results that look like plan checks."""

    trace_id: str
    domain: str
    task: str
    status: str
    plan_checks: list[dict[str, Any]]


class EnvironmentSummary(BaseModel):
    environment: Environment
    rubric: Rubric | None = None


class SavingsPerOp(BaseModel):
    op_key: str
    domain: str | None = None
    task_sample: str | None = None
    baseline_cost_usd: float
    last_cost_usd: float
    current_cost_usd: float
    delta_vs_last_usd: float
    delta_vs_base_usd: float
    pct_vs_base: float
    calls_count: int


class SavingsSummary(BaseModel):
    operations_tracked: int
    total_calls: int
    would_have_cost_usd: float
    actually_cost_usd: float
    saved_usd: float
    saved_pct: float
    per_operation: list[SavingsPerOp]


class CallEntry(BaseModel):
    run_id: str | None = None
    domain: str | None = None
    task: str | None = None
    operation: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float
    lessons_used: list[str]
    op_key: str
    at: str


class MemoryUpsertRequest(BaseModel):
    agent_id: str
    label: str
    value: str
    limit_chars: int = 8000
    description: str = ""
    read_only: bool = False
    pinned: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    expected_version: int | None = None
    actor: str | None = None


class MemoryArchiveRequest(BaseModel):
    agent_id: str
    text: str
    source: str
    source_ref: str = ""
    tags: list[str] = Field(default_factory=list)


class MemoryRecallRequest(BaseModel):
    agent_id: str
    query: str
    top_k: int = 5
    tags: list[str] = Field(default_factory=list)
    since: str | None = None


# --------------------------------------------------------------------------- #
# Token / cost estimators                                                     #
# --------------------------------------------------------------------------- #


def _approx_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def _trace_file_path(item: str | FileEditRecord) -> str:
    return item if isinstance(item, str) else item.path


def _trace_command_text(item: str | CommandRecord) -> str:
    return item if isinstance(item, str) else item.command


def _trace_raw_tokens(trace: Trace) -> int:
    """Estimate uncompressed observable tokens for a trace."""
    parts: list[str] = [
        trace.task,
        trace.diff_summary,
        trace.output_summary,
        *(_trace_file_path(item) for item in trace.files_touched),
        *(_trace_command_text(item) for item in trace.commands_run),
        *trace.errors_seen,
    ]
    parts.extend(t.name for t in trace.tools_called)
    parts.extend(rf.signature for rf in trace.repeated_failures)
    return sum(_approx_tokens(p) for p in parts)


def _trace_compressed_tokens(trace: Trace) -> int:
    """Estimate the compressed footprint after Atelier dedup/compaction.

    Heuristic: collapse repeated commands / files / tool calls.
    """
    unique_files = {_trace_file_path(item) for item in trace.files_touched}
    unique_cmds = {_trace_command_text(item) for item in trace.commands_run}
    unique_tools = {t.name for t in trace.tools_called}
    unique_errors = {*trace.errors_seen}
    parts = [
        trace.task,
        trace.diff_summary,
        trace.output_summary,
        *unique_files,
        *unique_cmds,
        *unique_tools,
        *unique_errors,
    ]
    return sum(_approx_tokens(p) for p in parts)


def _cycle_count(trace: Trace) -> int:
    """A 'cycle' ≈ tool_call + command in a single iteration."""
    return max(1, len(trace.tools_called) + len(trace.commands_run))


def _token_stats(trace: Trace) -> TokenStats:
    raw = _trace_raw_tokens(trace)
    compressed = _trace_compressed_tokens(trace)
    saved = max(0, raw - compressed)
    ratio = (compressed / raw) if raw else 1.0
    return TokenStats(
        trace_id=trace.id,
        domain=trace.domain,
        agent=trace.agent,
        status=trace.status,
        raw_tokens_estimate=raw,
        compressed_tokens_estimate=compressed,
        saved_tokens_estimate=saved,
        compression_ratio=round(ratio, 4),
    )


def _cost(tokens: int) -> float:
    return round((tokens / 1000.0) * USD_PER_1K_TOKENS, 6)


# --------------------------------------------------------------------------- #
# App factory                                                                 #
# --------------------------------------------------------------------------- #


def create_app(
    *,
    store_root: Path | None = None,
    env_dir: Path | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    store = ReasoningStore(store_root or DEFAULT_STORE_ROOT)
    store.init()
    runs_dir = (store_root or DEFAULT_STORE_ROOT) / "runs"
    analyzer = FailureAnalyzer(runs_dir)
    env_directory = env_dir or DEFAULT_ENV_DIR
    cost_tracker = CostTracker(store_root or DEFAULT_STORE_ROOT)

    from atelier.infra.storage.factory import make_memory_store

    mem_store = make_memory_store(store_root or DEFAULT_STORE_ROOT)

    app = FastAPI(
        title="Atelier Reasoning Runtime API",
        version="0.1.0",
        description="Read-only dashboard API over the Atelier reasoning store.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    init_product_telemetry(service_version="0.1.0")

    @app.middleware("http")
    async def product_telemetry_middleware(request: Request, call_next: Any) -> Any:
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            emit_product(
                "api_request",
                endpoint=_route_path(request),
                method=request.method,
                status_code=500,
                duration_ms_bucket=bucket_duration_ms((time.perf_counter() - started_at) * 1000),
            )
            raise
        emit_product(
            "api_request",
            endpoint=_route_path(request),
            method=request.method,
            status_code=int(getattr(response, "status_code", 0) or 0),
            duration_ms_bucket=bucket_duration_ms((time.perf_counter() - started_at) * 1000),
        )
        return response

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _all_traces() -> list[Trace]:
        return store.list_traces(limit=10_000)

    def _all_blocks() -> list[ReasonBlock]:
        return store.list_blocks()

    def _all_rubrics() -> list[Rubric]:
        return store.list_rubrics()

    def _load_envs() -> list[Environment]:
        if not env_directory.exists():
            return []
        return load_environments_from_dir(env_directory)

    def _route_path(request: Request) -> str:
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        return str(path or request.url.path)

    # ------------------------------------------------------------------ #
    # Endpoints                                                          #
    # ------------------------------------------------------------------ #

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "store_root": str(store.root),
            "env_dir": str(env_directory),
        }

    @app.get("/telemetry/local")
    def telemetry_local(
        since: float | None = None,
        event: str | None = None,
        limit: int = Query(500, ge=1, le=1000),
    ) -> dict[str, Any]:
        return {"events": LocalTelemetryStore().list_events(since=since, event=event, limit=limit)}

    @app.post("/telemetry/local")
    def telemetry_local_write(payload: dict[str, Any]) -> dict[str, Any]:
        event = str(payload.get("event", ""))
        props = payload.get("props", {})
        if not isinstance(props, dict):
            raise HTTPException(status_code=400, detail="props must be an object")
        emit_product_local(event, **props)
        return {"ok": True}

    @app.get("/telemetry/summary")
    def telemetry_summary(since: float | None = None) -> dict[str, Any]:
        return LocalTelemetryStore().summary(since=since)

    @app.get("/telemetry/schema")
    def telemetry_schema() -> dict[str, Any]:
        return schema_dump()

    @app.get("/telemetry/config")
    def telemetry_config() -> dict[str, Any]:
        return frontend_telemetry_config()

    @app.post("/telemetry/config")
    def telemetry_config_update(payload: dict[str, Any]) -> dict[str, Any]:
        remote = payload.get("remote_enabled")
        lexical = payload.get("lexical_frustration_enabled")
        if remote is not None and not isinstance(remote, bool):
            raise HTTPException(status_code=400, detail="remote_enabled must be boolean")
        if lexical is not None and not isinstance(lexical, bool):
            raise HTTPException(
                status_code=400,
                detail="lexical_frustration_enabled must be boolean",
            )
        if lexical is not None:
            save_telemetry_config(lexical_frustration_enabled=lexical)
        if remote is not None:
            set_remote_enabled(remote)
        return frontend_telemetry_config()

    @app.post("/telemetry/ack")
    def telemetry_ack() -> dict[str, Any]:
        mark_acknowledged()
        return frontend_telemetry_config()

    @app.get("/overview", response_model=OverviewStats)
    def overview() -> OverviewStats:
        traces = _all_traces()
        stats = [_token_stats(t) for t in traces]
        raw_total = sum(s.raw_tokens_estimate for s in stats)
        compressed_total = sum(s.compressed_tokens_estimate for s in stats)
        saved_total = sum(s.saved_tokens_estimate for s in stats)
        avg_ratio = round(sum(s.compression_ratio for s in stats) / len(stats), 4) if stats else 1.0
        return OverviewStats(
            total_traces=len(traces),
            total_blocks=len(_all_blocks()),
            total_rubrics=len(_all_rubrics()),
            total_environments=len(_load_envs()),
            total_clusters=len(analyzer.analyze()) if runs_dir.exists() else 0,
            total_raw_tokens_estimate=raw_total,
            total_saved_tokens_estimate=saved_total,
            total_compressed_tokens_estimate=compressed_total,
            average_compression_ratio=avg_ratio,
            estimated_total_cost_usd=_cost(compressed_total),
            estimated_saved_cost_usd=_cost(saved_total),
            usd_per_1k_tokens=USD_PER_1K_TOKENS,
        )

    @app.get("/tokens", response_model=list[TokenStats])
    def tokens(limit: int = Query(100, ge=1, le=10_000)) -> list[TokenStats]:
        return [_token_stats(t) for t in store.list_traces(limit=limit)]

    @app.get("/cost", response_model=list[CostEntry])
    def cost(limit: int = Query(100, ge=1, le=10_000)) -> list[CostEntry]:
        out: list[CostEntry] = []
        for trace in store.list_traces(limit=limit):
            cycles = _cycle_count(trace)
            raw = _trace_raw_tokens(trace)
            compressed = _trace_compressed_tokens(trace)
            total = _cost(compressed)
            out.append(
                CostEntry(
                    trace_id=trace.id,
                    domain=trace.domain,
                    agent=trace.agent,
                    cycle_count=cycles,
                    raw_tokens_estimate=raw,
                    compressed_tokens_estimate=compressed,
                    cost_per_cycle_usd=round(total / cycles, 6) if cycles else total,
                    cost_total_usd=total,
                )
            )
        return out

    @app.get("/plans")
    def plans(limit: int = Query(100, ge=1, le=10_000)) -> list[dict[str, Any]]:
        """Lightweight view of plan-related validation results per trace."""
        out: list[dict[str, Any]] = []
        for trace in store.list_traces(limit=limit):
            plan_checks = [vr.model_dump() for vr in trace.validation_results if "plan" in vr.name.lower()]
            if not plan_checks:
                continue
            out.append(
                {
                    "trace_id": trace.id,
                    "domain": trace.domain,
                    "task": trace.task,
                    "status": trace.status,
                    "plan_checks": plan_checks,
                }
            )
        return out

    @app.get("/traces", response_model=list[Trace])
    def list_traces(
        limit: int = Query(100, ge=1, le=10_000),
        offset: int = Query(0, ge=0),
        status: str | None = Query(None),
        domain: str | None = Query(None),
        agent: str | None = Query(None),
    ) -> list[Trace]:
        traces = store.list_traces(limit=limit, offset=offset, status=status, domain=domain, agent=agent)
        return traces

    @app.get("/traces/{trace_id}", response_model=Trace)
    def get_trace(trace_id: str) -> Trace:
        trace = store.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"trace {trace_id} not found")
        return trace

    @app.get("/clusters", response_model=list[FailureCluster])
    def clusters() -> list[FailureCluster]:
        if not runs_dir.exists():
            return []
        return analyzer.analyze()

    @app.get("/environments", response_model=list[EnvironmentSummary])
    def environments() -> list[EnvironmentSummary]:
        envs = _load_envs()
        rubrics = {r.id: r for r in _all_rubrics()}
        return [
            EnvironmentSummary(
                environment=e,
                rubric=rubrics.get(e.rubric_id) if e.rubric_id else None,
            )
            for e in envs
        ]

    @app.get("/blocks", response_model=list[ReasonBlock])
    def blocks(
        limit: int = Query(200, ge=1, le=10_000),
        domain: str | None = Query(None),
    ) -> list[ReasonBlock]:
        return store.list_blocks(domain=domain)[:limit]

    @app.get("/blocks/{block_id}", response_model=ReasonBlock)
    def get_block(block_id: str) -> ReasonBlock:
        block = store.get_block(block_id)
        if block is None:
            raise HTTPException(status_code=404, detail=f"block {block_id} not found")
        return block

    @app.get("/ledgers/{run_id}")
    def get_ledger(run_id: str) -> dict[str, Any]:
        path = runs_dir / f"{run_id}.json"
        if not path.exists():
            # Fallback: check if this is an imported trace and return trace data with note
            # Try exact run_id match first on all traces (since run_id is separate from id)
            traces = store.list_traces(limit=5000)
            trace = next((t for t in traces if getattr(t, "run_id", None) == run_id), None)
            if trace:
                return {
                    "run_id": run_id,
                    "status": trace.status,
                    "task": trace.task,
                    "agent": trace.agent,
                    "domain": trace.domain,
                    "created_at": trace.created_at.isoformat() if trace.created_at else None,
                    "note": "This trace was imported from a session file; no live ledger exists.",
                    "trace": trace.model_dump(mode="json"),
                }
            raise HTTPException(status_code=404, detail=f"ledger {run_id} not found")
        try:
            import typing

            return typing.cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    @app.get("/savings", response_model=SavingsSummary)
    def savings() -> dict[str, Any]:
        return cost_tracker.total_savings()

    @app.get("/calls", response_model=list[CallEntry])
    def calls(limit: int = Query(200, ge=1, le=10_000)) -> list[dict[str, Any]]:
        from atelier.infra.runtime.cost_tracker import load_cost_history

        history = load_cost_history(store.root)
        ops = history.get("operations", {}) or {}
        all_calls: list[dict[str, Any]] = []
        for _op_key, entry in ops.items():
            domain = entry.get("domain")
            task = entry.get("task_sample")
            for c in entry.get("calls", []):
                all_calls.append(
                    {
                        "run_id": c.get("run_id"),  # Some might not have it if recorded early
                        "domain": domain,
                        "task": task,
                        **c,
                    }
                )
        # Sort by 'at' descending
        all_calls.sort(key=lambda x: x.get("at", ""), reverse=True)
        return all_calls[:limit]

    # ------------------------------------------------------------------ #
    # Memory routes (/v1/memory/*)                                       #
    # ------------------------------------------------------------------ #

    @app.get("/v1/memory/blocks", response_model=list[MemoryBlock] | MemoryBlock | None)
    def memory_blocks(
        agent_id: str = Query(...),
        label: str | None = Query(None),
        include_tombstoned: bool = Query(False),
        limit: int = Query(200, ge=1, le=2000),
    ) -> Any:
        """List all memory blocks for an agent, or get a single block by label."""
        if label is not None:
            return mem_store.get_block(agent_id, label, include_tombstoned=include_tombstoned)
        return mem_store.list_blocks(agent_id, include_tombstoned=include_tombstoned, limit=limit)

    @app.post("/v1/memory/blocks", response_model=MemoryBlock)
    def memory_upsert(req: MemoryUpsertRequest) -> MemoryBlock:
        """Create or update a memory block (optimistic-locking via expected_version)."""
        from atelier.infra.storage.memory_store import MemoryConcurrencyError

        existing = mem_store.get_block(req.agent_id, req.label)
        if existing is None:
            block = MemoryBlock(
                agent_id=req.agent_id,
                label=req.label,
                value=req.value,
                limit_chars=req.limit_chars,
                description=req.description,
                read_only=req.read_only,
                pinned=req.pinned,
                metadata=req.metadata,
            )
        else:
            if req.expected_version is not None and existing.version != req.expected_version:
                raise HTTPException(
                    status_code=409,
                    detail=f"version conflict: expected {req.expected_version}, got {existing.version}",
                )
            block = existing.model_copy(
                update={
                    "value": req.value,
                    "limit_chars": req.limit_chars,
                    "description": req.description,
                    "read_only": req.read_only,
                    "pinned": req.pinned,
                    "metadata": req.metadata,
                }
            )
        try:
            return mem_store.upsert_block(block, actor=req.actor or f"api:{req.agent_id}")
        except MemoryConcurrencyError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.post("/v1/memory/archive")
    def memory_archive(req: MemoryArchiveRequest) -> dict[str, Any]:
        """Archive a text passage into long-term memory."""
        import hashlib

        from atelier.core.foundation.memory_models import ArchivalSource

        src: ArchivalSource = req.source if req.source in ("trace", "block_evict", "user", "tool_output", "file_chunk") else "user"  # type: ignore[assignment]
        dedup_hash = hashlib.sha256(f"{req.agent_id}:{req.text}".encode()).hexdigest()[:32]
        passage = ArchivalPassage(
            agent_id=req.agent_id,
            text=req.text,
            source=src,
            source_ref=req.source_ref,
            tags=req.tags,
            dedup_hash=dedup_hash,
        )
        saved = mem_store.insert_passage(passage)
        return {"id": saved.id, "dedup_hit": saved.dedup_hit}

    @app.post("/v1/memory/recall")
    def memory_recall(req: MemoryRecallRequest) -> dict[str, Any]:
        """Recall relevant archived passages for a query."""
        from datetime import datetime

        since_dt: datetime | None = None
        if req.since:
            try:
                since_dt = datetime.fromisoformat(req.since).replace(tzinfo=UTC)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"invalid since format: {req.since!r}") from None
        passages = mem_store.search_passages(
            req.agent_id,
            req.query,
            top_k=req.top_k,
            tags=req.tags or None,
            since=since_dt,
        )
        return {"passages": [p.model_dump(mode="json") for p in passages]}

    return app


# --------------------------------------------------------------------------- #
# Entrypoint                                                                  #
# --------------------------------------------------------------------------- #


app = create_app()


def main() -> None:
    """Run the API with uvicorn (entry point: ``atelier-api``)."""
    import uvicorn

    host = os.environ.get("ATELIER_API_HOST", "0.0.0.0")
    port = int(os.environ.get("ATELIER_API_PORT", "8124"))
    uvicorn.run(
        "atelier.gateway.adapters.http_api:app",
        host=host,
        port=port,
        reload=os.environ.get("ATELIER_API_RELOAD") == "1",
    )


if __name__ == "__main__":
    main()
