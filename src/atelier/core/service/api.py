"""Atelier production service API.

Creates a FastAPI application exposing the Atelier reasoning runtime
over HTTP with optional Bearer auth.

Usage (import-safe — no server starts on import)::

    from atelier.core.service.api import create_app
    app = create_app(store_root="/path/to/data")

Run via CLI::

    atelier runtime start

"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from atelier.core.foundation.models import (
    Trace,
    to_jsonable,
)
from atelier.core.foundation.store import ReasoningStore
from atelier.core.service.config import cfg

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Auth                                                                        #
# --------------------------------------------------------------------------- #

security = HTTPBearer(auto_error=False)


def verify_api_key(
    auth: HTTPAuthorizationCredentials | None = Security(security),  # noqa: B008
) -> str:
    """Validate Bearer token against cfg.api_key.

    If cfg.require_auth is False, this is a no-op.
    """
    if not cfg.require_auth:
        return "anonymous"

    if auth is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # If key is not configured, we reject all authenticated requests
    if not cfg.api_key:
        _LOGGER.warning("API key not configured; rejecting request")
        raise HTTPException(status_code=401, detail="Authentication required but no key configured")

    if auth.credentials != cfg.api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")

    return "authenticated"


# --------------------------------------------------------------------------- #
# Savings helpers                                                             #
# --------------------------------------------------------------------------- #


def _normalize_lever(operation: str) -> str:
    op = (operation or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    if "search_read" in op or "smart_read" in op:
        return "search_read"
    if "batch_edit" in op:
        return "batch_edit"
    if "ast" in op and ("trunc" in op or "outline" in op):
        return "ast_truncation"
    if "sleep" in op:
        return "sleeptime"
    if "cache" in op:
        return "cached_read"
    if "recall" in op:
        return "scoped_recall"
    if "compact" in op:
        return "compact_lifecycle"
    if "reasonblock" in op or "reason_block" in op or "inject" in op:
        return "reasonblock_inject"
    return op


def _iter_live_savings_events(root: Path) -> list[dict[str, Any]]:
    path = root / "live_savings_events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _latest_savings_benchmark(root: Path) -> dict[str, Any] | None:
    path = root / "benchmarks" / "savings" / "latest.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    keys = {
        "run_id",
        "model",
        "n_prompts",
        "total_tokens_baseline",
        "total_tokens_atelier",
        "tokens_saved",
        "reduction_pct",
        "total_cost_baseline_usd",
        "total_cost_atelier_usd",
        "cost_saved_usd",
        "total_time_baseline_ms",
        "total_time_atelier_ms",
        "time_saved_ms",
        "baseline_success_rate",
        "atelier_success_rate",
    }
    return {key: payload[key] for key in keys if key in payload}


def _savings_summary_payload(root: Path, *, window_days: int) -> dict[str, Any]:
    from atelier.infra.runtime.cost_tracker import CostTracker, load_cost_history

    history = load_cost_history(root)
    ops = history.get("operations", {}) if isinstance(history, dict) else {}
    today = datetime.now(UTC).date()
    window_days = max(1, min(window_days, 30))
    start_day = today - timedelta(days=window_days - 1)

    by_day_seed: dict[str, dict[str, int | str]] = {}
    for i in range(window_days):
        day = (start_day + timedelta(days=i)).isoformat()
        by_day_seed[day] = {"day": day, "naive": 0, "actual": 0}

    total_naive = 0
    total_actual = 0
    per_lever: defaultdict[str, int] = defaultdict(int)
    live_cost_saved = 0.0
    live_calls_saved = 0
    live_time_saved_ms = 0
    source_totals: dict[tuple[str, str], dict[str, Any]] = {}

    for entry in (ops.values() if isinstance(ops, dict) else []):
        calls = entry.get("calls", []) if isinstance(entry, dict) else []
        for call in calls:
            if not isinstance(call, dict):
                continue
            input_tokens = int(call.get("input_tokens", 0) or 0)
            output_tokens = int(call.get("output_tokens", 0) or 0)
            cache_read_tokens = int(call.get("cache_read_tokens", 0) or 0)
            actual = input_tokens + output_tokens
            naive = actual + cache_read_tokens
            total_naive += naive
            total_actual += actual
            per_lever[_normalize_lever(str(call.get("operation", "unknown")))] += max(0, naive - actual)
            at_raw = str(call.get("at", ""))
            try:
                at_date = datetime.fromisoformat(at_raw.replace("Z", "+00:00")).date()
            except Exception:
                at_date = today
            day_key = at_date.isoformat()
            if day_key in by_day_seed:
                by_day_seed[day_key]["naive"] = int(by_day_seed[day_key]["naive"]) + naive
                by_day_seed[day_key]["actual"] = int(by_day_seed[day_key]["actual"]) + actual

    for event in _iter_live_savings_events(root):
        tokens_saved = int(event.get("tokens_saved", 0) or 0)
        if tokens_saved <= 0:
            continue
        at_raw = str(event.get("at", ""))
        try:
            at_date = datetime.fromisoformat(at_raw.replace("Z", "+00:00")).date()
        except Exception:
            at_date = today
        if at_date < start_day:
            continue
        lever = _normalize_lever(str(event.get("lever") or event.get("tool_name") or "plugin_live"))
        tool_name = str(event.get("tool_name") or lever)
        total_naive += tokens_saved
        per_lever[lever] += tokens_saved
        live_cost_saved += float(event.get("cost_saved_usd", 0.0) or 0.0)
        live_calls_saved += int(event.get("calls_saved", 0) or 0)
        live_time_saved_ms += int(event.get("time_saved_ms", 0) or 0)
        day_key = at_date.isoformat()
        if day_key in by_day_seed:
            by_day_seed[day_key]["naive"] = int(by_day_seed[day_key]["naive"]) + tokens_saved
        key = (lever, tool_name)
        source = source_totals.setdefault(
            key,
            {
                "lever": lever,
                "tool_name": tool_name,
                "calls_saved": 0,
                "tokens_saved": 0,
                "cost_saved_usd": 0.0,
                "time_saved_ms": 0,
            },
        )
        source["calls_saved"] += int(event.get("calls_saved", 0) or 0)
        source["tokens_saved"] += tokens_saved
        source["cost_saved_usd"] += float(event.get("cost_saved_usd", 0.0) or 0.0)
        source["time_saved_ms"] += int(event.get("time_saved_ms", 0) or 0)

    reduction_pct = round((1.0 - (total_actual / total_naive)) * 100.0, 1) if total_naive > 0 else 0.0
    sorted_levers = dict(sorted(per_lever.items(), key=lambda kv: kv[1], reverse=True))
    cost_summary = CostTracker(root).total_savings()
    saved_usd = round(float(cost_summary["saved_usd"]) + live_cost_saved, 6)
    would_have_cost = round(float(cost_summary["would_have_cost_usd"]) + live_cost_saved, 6)
    actually_cost = float(cost_summary["actually_cost_usd"])
    saved_pct = round(100.0 * saved_usd / would_have_cost, 2) if would_have_cost > 0 else 0.0
    top_sources = sorted(source_totals.values(), key=lambda row: float(row["cost_saved_usd"]), reverse=True)
    for src in top_sources:
        src["cost_saved_usd"] = round(float(src["cost_saved_usd"]), 6)

    return {
        "window_days": window_days,
        "total_naive_tokens": total_naive,
        "total_actual_tokens": total_actual,
        "reduction_pct": reduction_pct,
        "per_lever": sorted_levers,
        "by_day": list(by_day_seed.values()),
        "saved_usd": saved_usd,
        "saved_pct": saved_pct,
        "would_have_cost_usd": would_have_cost,
        "actually_cost_usd": round(actually_cost, 6),
        "total_calls": cost_summary["total_calls"],
        "live_calls_saved": live_calls_saved,
        "live_time_saved_ms": live_time_saved_ms,
        "live_saved_usd": round(live_cost_saved, 6),
        "top_sources": top_sources[:10],
        "latest_benchmark": _latest_savings_benchmark(root),
    }


# --------------------------------------------------------------------------- #
# App Factory                                                                 #
# --------------------------------------------------------------------------- #


def create_app(store_root: str | Path | None = None) -> FastAPI:
    """Construct the FastAPI instance."""
    app = FastAPI(
        title="Atelier",
        description="Agent Reasoning Runtime API",
        version="0.1.0",
        docs_url="/docs",
    )

    # CORS for local dev
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Late load store
    store_path = Path(store_root or cfg.atelier_root)
    store = ReasoningStore(store_path)

    def get_store() -> ReasoningStore:
        if not store._initialized:
            store.init()
        return store

    # ------------------------------------------------------------------ #
    # Metadata & Health                                                   #
    # ------------------------------------------------------------------ #

    @app.get("/health", tags=["system"])
    def health_check() -> dict[str, str]:
        return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}

    @app.get("/config", tags=["system"], dependencies=[Depends(verify_api_key)])
    def get_config() -> dict[str, Any]:
        return cfg.as_dict()

    @app.get("/overview", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_overview() -> dict[str, Any]:
        """Compatibility: GET /overview -> basic summary stats."""
        from atelier.core.foundation.metrics import summarize
        from atelier.core.improvement.failure_analyzer import FailureAnalyzer
        from atelier.infra.runtime.cost_tracker import CostTracker

        root = Path(cfg.atelier_root)
        store = get_store()
        summary = summarize(store)

        # Calculate tokens/cost from ALL traces in database (high-fidelity)
        total_raw_tokens = 0
        total_cost_usd = 0.0

        with sqlite3.connect(store.db_path) as conn:
            sql = """
                SELECT 
                    SUM(json_extract(payload, '$.input_tokens')),
                    SUM(json_extract(payload, '$.output_tokens')),
                    SUM(json_extract(payload, '$.cached_input_tokens')),
                    SUM(json_extract(payload, '$.thinking_tokens'))
                FROM traces
            """
            row = conn.execute(sql).fetchone()
            if row:
                inp, out, cr, th = row
                total_raw_tokens = (inp or 0) + (out or 0) + (cr or 0) + (th or 0)
                # Heuristic pricing for overview totals ($3/$15)
                total_cost_usd = ((inp or 0) * 3 + (cr or 0) * 0.3 + (out or 0) * 15) / 1000000.0

        tracker = CostTracker(root)
        savings = tracker.total_savings()

        analyzer = FailureAnalyzer(store=store)
        clusters = analyzer.analyze()

        return {
            "total_traces": summary.traces_total,
            "total_blocks": summary.blocks_active,
            "total_rubrics": summary.rubrics_total,
            "total_clusters": len(clusters),
            "total_raw_tokens_estimate": total_raw_tokens,
            "estimated_total_cost_usd": max(total_cost_usd, savings["actually_cost_usd"]),
            "estimated_saved_cost_usd": savings["saved_usd"],
            "average_compression_ratio": 1.0,
            "is_estimate": False,
        }

    # ------------------------------------------------------------------ #
    # Traces                                                              #
    # ------------------------------------------------------------------ #

    @app.get("/traces", tags=["traces"], dependencies=[Depends(verify_api_key)])
    def list_traces(
        domain: str | None = Query(None),
        status: str | None = Query(None),
        agent: str | None = Query(None),
        host: str | None = Query(None),
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        store = get_store()
        traces = store.list_traces(domain=domain, status=status, agent=agent, host=host, limit=limit, offset=offset)
        # Fetch global metrics for the current domain/agent/host filters
        metrics = store.get_traces_metrics(domain=domain, agent=agent, host=host)

        return {"items": [to_jsonable(t) for t in traces], "metrics": metrics}

    @app.get("/v1/traces/{trace_id}", tags=["traces"], dependencies=[Depends(verify_api_key)])
    def get_trace(trace_id: str) -> dict[str, Any]:
        trace = get_store().get_trace(trace_id)
        if not trace:
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
        return to_jsonable(trace)

    @app.post("/v1/traces", tags=["traces"], dependencies=[Depends(verify_api_key)])
    def record_trace(payload: dict[str, Any]) -> dict[str, str]:
        if "id" not in payload:
            payload["id"] = Trace.make_id(payload.get("task", "untitled"), payload.get("agent", "agent"))
        trace = Trace.model_validate(payload)
        get_store().record_trace(trace)
        return {"id": trace.id}

    # ------------------------------------------------------------------ #
    # Memory (agent long-term memory)                                     #
    # ------------------------------------------------------------------ #

    _mem_store: Any = None

    def _get_mem_store() -> Any:
        nonlocal _mem_store
        if _mem_store is None:
            from atelier.infra.storage.factory import make_memory_store

            _mem_store = make_memory_store(Path(cfg.atelier_root))
        return _mem_store

    @app.get("/v1/memory/blocks", tags=["knowledge"], dependencies=[Depends(verify_api_key)])
    def memory_list_or_get(
        agent_id: str,
        label: str | None = None,
        include_tombstoned: bool = False,
        limit: int = 200,
    ) -> Any:
        mem = _get_mem_store()
        if label is not None:
            block = mem.get_block(agent_id, label, include_tombstoned=include_tombstoned)
            if block is None:
                raise HTTPException(status_code=404, detail=f"Block not found: {label!r}")
            return block
        return mem.list_blocks(agent_id, include_tombstoned=include_tombstoned, limit=limit)

    @app.post("/v1/memory/blocks", tags=["knowledge"], dependencies=[Depends(verify_api_key)])
    def memory_upsert_block(payload: dict[str, Any]) -> Any:
        from atelier.core.foundation.memory_models import MemoryBlock
        from atelier.infra.storage.memory_store import MemoryConcurrencyError

        mem = _get_mem_store()
        agent_id = payload.get("agent_id")
        label = payload.get("label")
        if not agent_id or not label:
            raise HTTPException(status_code=400, detail="agent_id and label are required")
        existing = mem.get_block(agent_id, label)
        if existing is None:
            value = str(payload.get("value", ""))
            limit_chars = int(payload.get("limit_chars", 8000))
            if len(value) > limit_chars:
                raise HTTPException(status_code=400, detail="value exceeds limit_chars")
            block = MemoryBlock(
                agent_id=agent_id,
                label=label,
                value=value,
                limit_chars=limit_chars,
                description=str(payload.get("description", "")),
                read_only=bool(payload.get("read_only", False)),
                pinned=bool(payload.get("pinned", False)),
                metadata=payload.get("metadata") or {},
            )
        else:
            expected_version = payload.get("expected_version")
            if expected_version is not None and existing.version != int(expected_version):
                raise HTTPException(
                    status_code=409,
                    detail=f"version conflict: expected {expected_version}, got {existing.version}",
                )
            update: dict[str, Any] = {}
            for field in ("value", "description", "read_only", "pinned", "metadata", "limit_chars"):
                if field in payload and payload[field] is not None:
                    update[field] = payload[field]
            block = existing.model_copy(update=update)
        actor = str(payload.get("actor") or f"api:{agent_id}")
        try:
            return mem.upsert_block(block, actor=actor)
        except MemoryConcurrencyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/memory/archive", tags=["knowledge"], dependencies=[Depends(verify_api_key)])
    def memory_archive_passage(payload: dict[str, Any]) -> Any:
        import hashlib

        from atelier.core.foundation.memory_models import ArchivalPassage

        mem = _get_mem_store()
        agent_id = payload.get("agent_id")
        text = payload.get("text")
        if not agent_id or not text:
            raise HTTPException(status_code=400, detail="agent_id and text are required")
        valid_sources = ("trace", "block_evict", "user", "tool_output", "file_chunk")
        source = payload.get("source", "user")
        if source not in valid_sources:
            source = "user"
        dedup_hash = hashlib.sha256(f"{agent_id}:{text}".encode()).hexdigest()[:32]
        passage = ArchivalPassage(
            agent_id=agent_id,
            text=str(text),
            source=source,
            source_ref=str(payload.get("source_ref", "")),
            tags=list(payload.get("tags") or []),
            dedup_hash=dedup_hash,
        )
        saved = mem.insert_passage(passage)
        return {"id": saved.id, "dedup_hit": saved.dedup_hit}

    @app.post("/v1/memory/recall", tags=["knowledge"], dependencies=[Depends(verify_api_key)])
    def memory_recall_passages(payload: dict[str, Any]) -> Any:
        mem = _get_mem_store()
        agent_id = payload.get("agent_id")
        query = payload.get("query")
        if not agent_id or not query:
            raise HTTPException(status_code=400, detail="agent_id and query are required")
        since_str = payload.get("since")
        since_dt: datetime | None = None
        if since_str:
            try:
                since_dt = datetime.fromisoformat(since_str).replace(tzinfo=UTC)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"invalid since: {since_str!r}") from exc
        passages = mem.search_passages(
            agent_id,
            str(query),
            top_k=int(payload.get("top_k", 5)),
            tags=list(payload.get("tags") or []) or None,
            since=since_dt,
        )
        return {"passages": [p.model_dump(mode="json") for p in passages]}

    # ------------------------------------------------------------------ #
    # Telemetry                                                           #
    # ------------------------------------------------------------------ #

    @app.get("/telemetry/config", tags=["telemetry"], dependencies=[Depends(verify_api_key)])
    def get_telemetry_config() -> dict[str, Any]:
        from atelier.core.service.telemetry.config import load_telemetry_config

        cfg_telemetry = load_telemetry_config()
        return {
            "remote_enabled": cfg_telemetry.remote_enabled,
            "lexical_frustration_enabled": cfg_telemetry.lexical_frustration_enabled,
            "dev_mode": cfg.dev_mode,
        }

    @app.post("/telemetry/config", tags=["telemetry"], dependencies=[Depends(verify_api_key)])
    def update_telemetry_config(payload: dict[str, Any]) -> dict[str, Any]:
        from atelier.core.service.telemetry.config import save_telemetry_config

        cfg_telemetry = save_telemetry_config(
            remote_enabled=payload.get("remote_enabled"),
            lexical_frustration_enabled=payload.get("lexical_frustration_enabled"),
        )
        return {
            "remote_enabled": cfg_telemetry.remote_enabled,
            "lexical_frustration_enabled": cfg_telemetry.lexical_frustration_enabled,
            "dev_mode": cfg.dev_mode,
        }

    @app.post("/telemetry/ack", tags=["telemetry"], dependencies=[Depends(verify_api_key)])
    def acknowledge_telemetry() -> dict[str, Any]:
        from atelier.core.service.telemetry.banner import mark_acknowledged
        from atelier.core.service.telemetry.config import load_telemetry_config

        mark_acknowledged()
        cfg_telemetry = load_telemetry_config()
        return {
            "remote_enabled": cfg_telemetry.remote_enabled,
            "lexical_frustration_enabled": cfg_telemetry.lexical_frustration_enabled,
            "dev_mode": cfg.dev_mode,
        }

    @app.get("/telemetry/local", tags=["telemetry"], dependencies=[Depends(verify_api_key)])
    def list_local_telemetry(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        from atelier.core.service.telemetry.local_store import LocalTelemetryStore

        store = LocalTelemetryStore()
        events = store.list_events(limit=limit)
        return {"events": events}

    @app.post("/telemetry/local", tags=["telemetry"], dependencies=[Depends(verify_api_key)])
    def post_local_telemetry(payload: dict[str, Any]) -> dict[str, bool]:
        from atelier.core.service.telemetry.local_store import LocalTelemetryStore

        store = LocalTelemetryStore()
        store.write_event(event=payload["event"], props=payload["props"], exported=False)
        return {"ok": True}

    @app.get("/telemetry/summary", tags=["telemetry"], dependencies=[Depends(verify_api_key)])
    def get_telemetry_summary() -> dict[str, Any]:
        from atelier.core.service.telemetry.local_store import LocalTelemetryStore

        store = LocalTelemetryStore()
        return store.summary()

    @app.get("/telemetry/schema", tags=["telemetry"], dependencies=[Depends(verify_api_key)])
    def get_telemetry_schema() -> dict[str, Any]:
        from atelier.core.service.telemetry.schema import schema_dump

        return schema_dump()

    # ------------------------------------------------------------------ #
    # Analytics & Compatibility Endpoints                                 #
    # ------------------------------------------------------------------ #

    @app.get("/analytics", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_analytics(
        agent: str | None = Query(None),
        category: str | None = Query(None),
        limit: int = Query(1000, ge=1, le=10000),
        grouped: bool = Query(True),
        days: int | None = Query(None),
    ) -> list[dict[str, Any]]:
        """GET /analytics -> granular tool usage dynamically from traces.

        Calculated on the fly from the JSON payloads in the traces table
        to ensure 100% lossless session-level accuracy.
        """
        import sqlite3

        from atelier.core.capabilities.pricing import get_model_pricing

        db_path = store.db_path
        params: list[Any] = []

        if grouped:
            sql = f"""
                WITH
                trace_data AS (
                    SELECT
                        agent,
                        json_extract(payload, '$.model') as model,
                        CAST(json_extract(payload, '$.input_tokens') AS INTEGER) as input_tokens,
                        CAST(json_extract(payload, '$.output_tokens') AS INTEGER) as output_tokens,
                        CAST(json_extract(payload, '$.thinking_tokens') AS INTEGER) as thinking_tokens,
                        CAST(json_extract(payload, '$.cached_input_tokens') AS INTEGER) as cached_input_tokens,
                        CAST(json_extract(payload, '$.cache_creation_input_tokens') AS INTEGER) as cache_creation_input_tokens,
                        CAST(json_extract(payload, '$.user_prompt_tokens') AS INTEGER) as user_prompt_tokens,
                        payload,
                        created_at
                    FROM traces
                    WHERE 1=1 {"AND agent = ?" if agent else ""} {"AND created_at >= datetime('now', '-' || ? || ' days')" if days else ""}
                ),
                events AS (
                    SELECT agent, model, 'user_string' as event_type, 'User Entered String' as tool_name,
                           NULL as sub_command, 'User Activity' as category,
                           user_prompt_tokens as input_tokens, 0 as output_tokens, created_at, 1 as call_count
                    FROM trace_data WHERE user_prompt_tokens > 0
                    UNION ALL
                    SELECT agent, model, 'prompt' as event_type, 'Context Window (Base)' as tool_name,
                           NULL as sub_command, 'LLM Context' as category,
                           (COALESCE(input_tokens, 0) - COALESCE(cached_input_tokens, 0) - COALESCE(cache_creation_input_tokens, 0)) as input_tokens, 0 as output_tokens, created_at, 1 as call_count
                    FROM trace_data WHERE input_tokens > 0
                    UNION ALL
                    SELECT agent, model, 'cached_prompt', 'Cached Prompt (Cache Read)', NULL, 'LLM Context (Cache Read)',
                           cached_input_tokens, 0, created_at, 1 as call_count
                    FROM trace_data WHERE cached_input_tokens > 0
                    UNION ALL
                    SELECT agent, model, 'cache_create', 'Cache Write (Anthropic)', NULL, 'LLM Context (Cache Write)',
                           cache_creation_input_tokens, 0, created_at, 1 as call_count
                    FROM trace_data WHERE cache_creation_input_tokens > 0
                    UNION ALL
                    SELECT agent, model, 'result', 'Assistant Response', NULL, 'LLM Generation',
                           0, output_tokens, created_at, 1 as call_count
                    FROM trace_data WHERE output_tokens > 0
                    UNION ALL
                    SELECT agent, model, 'thinking', 'Thinking', NULL, 'LLM Generation',
                           0, thinking_tokens, created_at, 1 as call_count
                    FROM trace_data WHERE thinking_tokens > 0
                    UNION ALL
                    SELECT
                        t.agent, t.model, 'tool_call', json_extract(tc.value, '$.name'), NULL,
                        CASE WHEN json_extract(tc.value, '$.name') IN ('Read', 'Bash', 'Edit', 'Grep', 'Glob', 'Write', 'Agent', 'ListDir', 'bash', 'run_shell_command', 'exec_command', 'shell', 'read_file', 'replace', 'apply_patch', 'view') THEN 'Native / Unoptimized' ELSE 'Atelier Optimized' END,
                        CAST(json_extract(tc.value, '$.input_tokens') AS INTEGER),
                        CAST(json_extract(tc.value, '$.output_tokens') AS INTEGER),
                        t.created_at,
                        CAST(json_extract(tc.value, '$.count') AS INTEGER)
                    FROM trace_data t, json_each(t.payload, '$.tools_called') tc
                )
                SELECT
                    agent, model, event_type, tool_name, sub_command, category,
                    SUM(input_tokens) as input_tokens,
                    SUM(output_tokens) as output_tokens,
                    MIN(created_at) as first_seen,
                    MAX(created_at) as last_seen,
                    SUM(call_count) as call_count
                FROM events
                WHERE 1=1 {"AND category = ?" if category else ""}
                GROUP BY agent, model, event_type, tool_name, sub_command, category
                ORDER BY last_seen DESC LIMIT ?
            """
        else:
            sql = f"""
                WITH
                trace_data AS (
                    SELECT
                        agent,
                        json_extract(payload, '$.model') as model,
                        CAST(json_extract(payload, '$.input_tokens') AS INTEGER) as input_tokens,
                        CAST(json_extract(payload, '$.output_tokens') AS INTEGER) as output_tokens,
                        CAST(json_extract(payload, '$.thinking_tokens') AS INTEGER) as thinking_tokens,
                        CAST(json_extract(payload, '$.cached_input_tokens') AS INTEGER) as cached_input_tokens,
                        CAST(json_extract(payload, '$.cache_creation_input_tokens') AS INTEGER) as cache_creation_input_tokens,
                        CAST(json_extract(payload, '$.user_prompt_tokens') AS INTEGER) as user_prompt_tokens,
                        payload,
                        created_at
                    FROM traces
                    WHERE 1=1 {"AND agent = ?" if agent else ""} {"AND created_at >= datetime('now', '-' || ? || ' days')" if days else ""}
                ),
                events AS (
                    SELECT agent, model, 'user_string' as event_type, 'User Entered String' as tool_name, NULL as sub_command, 'User Activity' as category, user_prompt_tokens as input_tokens, 0 as output_tokens, created_at FROM trace_data WHERE user_prompt_tokens > 0
                    UNION ALL
                    SELECT agent, model, 'prompt' as event_type, 'Context Window (Base)' as tool_name, NULL as sub_command, 'LLM Context' as category, (COALESCE(input_tokens, 0) - COALESCE(cached_input_tokens, 0) - COALESCE(cache_creation_input_tokens, 0)) as input_tokens, 0 as output_tokens, created_at FROM trace_data WHERE input_tokens > 0
                    UNION ALL
                    SELECT agent, model, 'cached_prompt', 'Cached Prompt (Cache Read)', NULL, 'LLM Context (Cache Read)', cached_input_tokens, 0, created_at FROM trace_data WHERE cached_input_tokens > 0
                    UNION ALL
                    SELECT agent, model, 'cache_create', 'Cache Write (Anthropic)', NULL, 'LLM Context (Cache Write)', cache_creation_input_tokens, 0, created_at FROM trace_data WHERE cache_creation_input_tokens > 0
                    UNION ALL
                    SELECT agent, model, 'result', 'Assistant Response', NULL, 'LLM Generation', 0, output_tokens, created_at FROM trace_data WHERE output_tokens > 0
                    UNION ALL
                    SELECT agent, model, 'thinking', 'Thinking', NULL, 'LLM Generation', 0, thinking_tokens, created_at FROM trace_data WHERE thinking_tokens > 0
                    UNION ALL
                    SELECT
                        t.agent, t.model, 'tool_call', json_extract(tc.value, '$.name'), NULL,
                        CASE WHEN json_extract(tc.value, '$.name') IN ('Read', 'Bash', 'Edit', 'Grep', 'Glob', 'Write', 'Agent', 'ListDir', 'bash', 'run_shell_command', 'exec_command', 'shell', 'read_file', 'replace', 'apply_patch', 'view') THEN 'Native / Unoptimized' ELSE 'Atelier Optimized' END,
                        CAST(json_extract(tc.value, '$.input_tokens') AS INTEGER),
                        CAST(json_extract(tc.value, '$.output_tokens') AS INTEGER),
                        t.created_at
                    FROM trace_data t, json_each(t.payload, '$.tools_called') tc
                )
                SELECT agent, model, event_type, tool_name, sub_command, category, input_tokens, output_tokens, created_at as first_seen, created_at as last_seen, 1 as call_count
                FROM events
                WHERE 1=1 {"AND category = ?" if category else ""}
                ORDER BY created_at DESC LIMIT ?
            """

        if agent:
            params.append(agent)
        if days:
            params.append(days)
        if category:
            params.append(category)
        params.append(limit)

        result = []
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            for r in rows:
                d = dict(r)
                model_id = d.get("model") or "_default"
                pricing = get_model_pricing(model_id)

                etype = d.get("event_type")
                in_t = d.get("input_tokens", 0) or 0
                out_t = d.get("output_tokens", 0) or 0

                cache_r = 0
                cache_w = 0
                bill_in = 0
                bill_out = 0

                if etype == "prompt" or etype == "user_string":
                    # NOTE: user_string is informational; ONLY bill 'prompt'
                    bill_in = in_t if etype == "prompt" else 0
                elif etype == "cache_create":
                    cache_w = in_t
                elif etype == "cached_prompt":
                    cache_r = in_t
                elif etype == "result" or etype == "thinking":
                    bill_out = out_t
                elif etype == "tool_call":
                    # Tool output attributed as future context (input rate)
                    bill_in = out_t

                d["cost"] = pricing.cost_usd(
                    input_tokens=bill_in,
                    output_tokens=bill_out,
                    cache_read_tokens=cache_r,
                    cache_write_tokens=cache_w,
                )
                result.append(d)
        return result

    @app.get("/plans", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_plans(limit: int = 50) -> list[dict[str, Any]]:
        """Compatibility: GET /plans -> derives plan records from traces."""
        traces = get_store().list_traces(limit=limit)
        result = []
        for t in traces:
            if t.run_id:
                result.append(
                    {
                        "trace_id": t.id,
                        "domain": t.domain,
                        "task": t.task,
                        "status": t.status,
                        "plan_checks": [{"name": "plan_valid", "passed": t.status == "success"}],
                    }
                )
        return result

    @app.get("/pricing", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def get_pricing_table() -> dict[str, Any]:
        """GET /pricing -> returns the current model pricing table."""
        from atelier.core.capabilities.pricing import _load_pricing_table

        return _load_pricing_table()

    # ------------------------------------------------------------------ #
    # Raw artifacts                                                       #
    # ------------------------------------------------------------------ #

    @app.get("/raw-artifacts/{artifact_id}", tags=["artifacts"], dependencies=[Depends(verify_api_key)])
    def get_raw_artifact(artifact_id: str) -> dict[str, Any]:
        """Return metadata for a stored raw artifact."""
        store_inst = get_store()
        artifact = store_inst.get_raw_artifact(artifact_id)
        if not artifact:
            raise HTTPException(status_code=404, detail=f"Raw artifact not found: {artifact_id}")
        return artifact.model_dump(mode="json")

    @app.get(
        "/raw-artifacts/{artifact_id}/content",
        tags=["artifacts"],
        dependencies=[Depends(verify_api_key)],
    )
    def get_raw_artifact_content(artifact_id: str) -> Any:
        """Return the raw JSONL content of a stored artifact as plain text."""
        from fastapi.responses import PlainTextResponse

        store_inst = get_store()
        artifact = store_inst.get_raw_artifact(artifact_id)
        if not artifact:
            raise HTTPException(status_code=404, detail=f"Raw artifact not found: {artifact_id}")
        try:
            content = store_inst.read_raw_artifact_content(artifact)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Content file not found on disk") from exc
        return PlainTextResponse(content, media_type="text/plain")

    @app.get("/ledgers/{run_id}", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_ledger(run_id: str) -> dict[str, Any]:
        """Compatibility: GET /ledgers/{run_id} -> returns run ledger data.

        First checks for a live RunLedger JSON file (written by the reasoning
        runtime).  When that is absent, falls back to an imported Trace that
        carries the same run_id so that sessions imported from Claude / Codex /
        OpenCode / Copilot are still surfaced here.
        """
        from atelier.infra.runtime.run_ledger import RunLedger

        ledger_path = Path(cfg.atelier_root) / "runs" / f"{run_id}.json"
        snap = None
        if ledger_path.exists():
            try:
                ledger = RunLedger.load(ledger_path)
                snap = ledger.snapshot()
            except Exception as e:
                return {"run_id": run_id, "error": str(e)}

        # Always check for a trace to fetch the full conversation history.
        # Imported sessions from Claude/Codex/OpenCode/Copilot use the Trace as source of truth.
        store_inst = get_store()

        # 1. Try direct ID match (high performance)
        trace = store_inst.get_trace(run_id)

        # 2. Try host-prefixed IDs (standard for imported sessions)
        if trace is None:
            for host in ("copilot", "claude", "codex", "opencode", "gemini"):
                trace = store_inst.get_trace(f"{host}-{run_id}")
                if trace:
                    break

        # 3. Slower fallback: search for the run_id inside payloads
        if trace is None:
            with sqlite3.connect(store_inst.db_path) as conn:
                # Use json_extract for efficient searching
                row = conn.execute(
                    "SELECT payload FROM traces WHERE json_extract(payload, '$.run_id') = ?", (run_id,)
                ).fetchone()
                if row:
                    trace = Trace.model_validate_json(row[0])

        conversations: list[dict[str, Any]] = []
        if trace:
            if trace.raw_artifact_ids:
                # Reconstruct from raw artifacts (imported sessions)
                for art_id in trace.raw_artifact_ids:
                    artifact = store_inst.get_raw_artifact(art_id)
                    if artifact:
                        try:
                            raw_content = store_inst.get_raw_artifact_content(artifact)
                            from atelier.gateway.hosts.session_parsers._session_parser import (
                                parse_session_turns,
                            )

                            conversations = parse_session_turns(raw_content, artifact.source)
                        except Exception:
                            pass
                        if conversations:
                            break

            # If no conversation turns exist (e.g. native trace or aborted import),
            # synthesize turns from reasoning and summaries.
            if not conversations:
                if trace.reasoning:
                    for r in trace.reasoning:
                        conversations.append(
                            {
                                "kind": "thinking",
                                "summary": "Reasoning Step",
                                "content": r,
                                "at": trace.created_at.isoformat() if trace.created_at else None,
                            }
                        )
                if trace.output_summary:
                    conversations.append(
                        {
                            "kind": "agent_message",
                            "summary": "Final Outcome",
                            "content": trace.output_summary,
                            "at": trace.created_at.isoformat() if trace.created_at else None,
                        }
                    )
                if trace.diff_summary:
                    conversations.append(
                        {
                            "kind": "file_edit",
                            "summary": "Changes Applied",
                            "content": trace.diff_summary,
                            "at": trace.created_at.isoformat() if trace.created_at else None,
                        }
                    )

            # Calculate turn costs on the fly using backend pricing
            from atelier.core.capabilities.pricing import get_model_pricing

            pricing = get_model_pricing(trace.model or "_default")
            for turn in conversations:
                t_toks = turn.get("tokens") or {}
                turn["cost"] = pricing.cost_usd(
                    input_tokens=t_toks.get("in", 0),
                    output_tokens=t_toks.get("out", 0),
                    cache_read_tokens=t_toks.get("cache_read", 0),
                    cache_write_tokens=t_toks.get("cache_write", 0),
                )

        if snap:
            if conversations:
                snap["conversations"] = conversations
            return snap

        if trace:
            return {
                "run_id": trace.run_id or run_id,
                "trace_id": trace.id,
                "status": trace.status,
                "task": trace.task,
                "agent": trace.agent,
                "domain": trace.domain,
                "created_at": trace.created_at.isoformat() if trace.created_at else None,
                "files_touched": trace.files_touched,
                "commands_run": trace.commands_run,
                "errors_seen": trace.errors_seen,
                "tools_called": [tc.model_dump() for tc in trace.tools_called],
                "conversations": conversations,
                "raw_artifact_ids": trace.raw_artifact_ids,
                "input_tokens": trace.input_tokens,
                "output_tokens": trace.output_tokens,
                "thinking_tokens": trace.thinking_tokens,
                "cached_input_tokens": trace.cached_input_tokens,
                "cache_creation_input_tokens": trace.cache_creation_input_tokens,
                "user_prompt_tokens": trace.user_prompt_tokens,
                "model": trace.model,
                "note": "Imported from session logs." if trace.raw_artifact_ids else "Live run trace.",
                "trace": trace.model_dump(mode="json"),
            }

        return {"run_id": run_id, "status": "not_found"}

    # ------------------------------------------------------------------ #
    # MCP & Hosts                                                         #
    # ------------------------------------------------------------------ #

    @app.get("/mcp/status", tags=["ops"], dependencies=[Depends(verify_api_key)])
    def mcp_status() -> list[dict[str, Any]]:
        try:
            from atelier.gateway.adapters.mcp_server import TOOLS

            return [
                {"tool_name": name, "available": True, "description": spec.get("description", "")}
                for name, spec in TOOLS.items()
            ]
        except Exception:
            return []

    @app.get("/hosts", tags=["ops"], dependencies=[Depends(verify_api_key)])
    def list_hosts() -> list[dict[str, Any]]:
        import shutil

        hosts = [
            ("claude", "Claude Code", "claude"),
            ("codex", "Codex", "codex"),
            ("opencode", "opencode", None),
            ("copilot", "VS Code Copilot", None),
            ("gemini", "Gemini CLI", "gemini"),
        ]
        result = []
        for hid, label, check in hosts:
            if check in ("claude", "codex", "gemini"):
                installed = shutil.which(check) is not None
            elif hid == "opencode":
                installed = (Path.home() / ".opencode").exists()
            elif hid == "copilot":
                installed = (Path.home() / ".vscode").exists()
            else:
                installed = False
            result.append(
                {
                    "host_id": hid,
                    "label": label,
                    "status": "installed" if installed else "not_installed",
                    "active_domains": [],
                    "mcp_tools": [],
                    "last_seen": None,
                    "atelier_version": None,
                    "description": None,
                    "install_command": None,
                }
            )
        return result

    # ------------------------------------------------------------------ #
    # Skills                                                              #
    # ------------------------------------------------------------------ #

    @app.get("/skills", tags=["ops"], dependencies=[Depends(verify_api_key)])
    def list_skills() -> list[dict[str, Any]]:
        root = Path(__file__).parent.parent.parent.parent.parent
        skills: list[dict[str, Any]] = []
        skills_dir = root / "integrations" / "skills"
        if skills_dir.exists():
            for skill_dir in sorted(skills_dir.iterdir()):
                if skill_dir.is_dir():
                    md = skill_dir / "SKILL.md"
                    if md.exists():
                        content = md.read_text(encoding="utf-8")
                        desc = ""
                        if content.startswith("---"):
                            end = content.find("---", 3)
                            if end > 0:
                                for line in content[3:end].split("\n"):
                                    if line.startswith("description:"):
                                        desc = line.split(":", 1)[1].strip()
                        skills.append({"name": skill_dir.name, "description": desc, "content": content})
        return skills

    @app.get("/skills/{name}", tags=["ops"], dependencies=[Depends(verify_api_key)])
    def get_skill(name: str) -> dict[str, Any]:
        root = Path(__file__).parent.parent.parent.parent.parent
        md = root / "integrations" / "skills" / name / "SKILL.md"
        if not md.exists():
            raise HTTPException(status_code=404, detail=f"Skill not found: {name}")
        content = md.read_text(encoding="utf-8")
        return {"name": name, "description": "", "content": content}

    # ------------------------------------------------------------------ #
    # Rubrics                                                             #
    # ------------------------------------------------------------------ #

    @app.get("/v1/rubrics", tags=["rubrics"], dependencies=[Depends(verify_api_key)])
    def list_rubrics(domain: str | None = Query(None)) -> list[dict[str, Any]]:
        return [to_jsonable(r) for r in get_store().list_rubrics(domain=domain)]

    @app.get("/v1/rubrics/{rubric_id}", tags=["rubrics"], dependencies=[Depends(verify_api_key)])
    def get_rubric(rubric_id: str) -> dict[str, Any]:
        rubric = get_store().get_rubric(rubric_id)
        if rubric is None:
            raise HTTPException(status_code=404, detail=f"Rubric not found: {rubric_id}")
        return to_jsonable(rubric)

    # ------------------------------------------------------------------ #
    # Blocks (compat)                                                     #
    # ------------------------------------------------------------------ #

    @app.get("/blocks", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_blocks() -> list[dict[str, Any]]:
        return [to_jsonable(b) for b in get_store().list_blocks()]

    @app.get("/blocks/{block_id}", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_block(block_id: str) -> dict[str, Any]:
        block = get_store().get_block(block_id)
        if block is None:
            raise HTTPException(status_code=404, detail=f"Block not found: {block_id}")
        return to_jsonable(block)

    # ------------------------------------------------------------------ #
    # Savings & Calls (compat)                                            #
    # ------------------------------------------------------------------ #

    @app.get("/savings", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_savings() -> dict[str, Any]:
        from atelier.infra.runtime.cost_tracker import CostTracker

        return CostTracker(Path(cfg.atelier_root)).total_savings()

    @app.get("/v1/savings/summary", tags=["metrics"], dependencies=[Depends(verify_api_key)])
    def savings_summary_v1(window_days: int = Query(14)) -> dict[str, Any]:
        return _savings_summary_payload(Path(cfg.atelier_root), window_days=window_days)

    @app.get("/calls", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_calls(limit: int = Query(200)) -> list[dict[str, Any]]:
        from atelier.infra.runtime.cost_tracker import load_cost_history

        history = load_cost_history(Path(cfg.atelier_root))
        ops = history.get("operations", {})
        all_calls: list[dict[str, Any]] = []
        for op_key, entry in ops.items():
            for call in entry.get("calls", []):
                all_calls.append(
                    {
                        "run_id": call.get("run_id", ""),
                        "domain": entry.get("domain"),
                        "task": entry.get("task_sample"),
                        "operation": call.get("operation"),
                        "model": call.get("model"),
                        "input_tokens": call.get("input_tokens"),
                        "output_tokens": call.get("output_tokens"),
                        "cache_read_tokens": call.get("cache_read_tokens"),
                        "cost_usd": call.get("cost_usd"),
                        "lessons_used": call.get("lessons_used"),
                        "op_key": op_key,
                        "at": call.get("at"),
                    }
                )
        all_calls.sort(key=lambda c: c.get("at") or "", reverse=True)
        return all_calls[:limit]

    # ------------------------------------------------------------------ #
    # Clusters (compat)                                                   #
    # ------------------------------------------------------------------ #

    @app.get("/clusters", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_clusters() -> list[dict[str, Any]]:
        from atelier.core.improvement.failure_analyzer import FailureAnalyzer

        try:
            return [to_jsonable(c) for c in FailureAnalyzer(store=get_store()).analyze()]
        except Exception:
            return []

    # ------------------------------------------------------------------ #
    # Watchdogs                                                           #
    # ------------------------------------------------------------------ #

    @app.get("/watchdogs/config", tags=["ops"], dependencies=[Depends(verify_api_key)])
    def get_watchdog_config() -> dict[str, Any]:
        from atelier.core.foundation.watchdog_profiles import frontend_watchdog_profile_config

        return frontend_watchdog_profile_config(Path(cfg.atelier_root))

    @app.post("/watchdogs/config", tags=["ops"], dependencies=[Depends(verify_api_key)])
    def update_watchdog_config(payload: dict[str, Any]) -> dict[str, Any]:
        from atelier.core.foundation.watchdog_profiles import (
            frontend_watchdog_profile_config,
            save_watchdog_profile_config,
        )

        try:
            save_watchdog_profile_config(
                Path(cfg.atelier_root),
                active_profile=payload.get("active_profile"),
                profiles=payload.get("profiles"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return frontend_watchdog_profile_config(Path(cfg.atelier_root))

    # ------------------------------------------------------------------ #
    # Memory passages                                                     #
    # ------------------------------------------------------------------ #

    @app.get("/v1/memory/passages", tags=["knowledge"], dependencies=[Depends(verify_api_key)])
    def list_memory_passages(
        agent_id: str = Query(...),
        limit: int = Query(25, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        from atelier.infra.storage.factory import make_memory_store

        mem = make_memory_store(Path(cfg.atelier_root))
        passages = mem.list_passages(agent_id, limit=limit)
        return [p.model_dump(mode="json") for p in passages]

    return app


# --------------------------------------------------------------------------- #
# Module-level app instance — used by uvicorn / atelier-api entrypoint       #
# --------------------------------------------------------------------------- #

app = create_app()


def main(
    host: str | None = None,
    port: int | None = None,
    *,
    reload: bool = False,
) -> None:
    """Launch the service with uvicorn.

    Used by ``atelier service start`` CLI command and the ``atelier-service``
    entrypoint.
    """
    import uvicorn

    _host = host or cfg.host
    _port = port or cfg.port
    uvicorn.run(
        "atelier.core.service.api:app",
        host=_host,
        port=_port,
        reload=reload,
    )
