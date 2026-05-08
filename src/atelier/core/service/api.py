"""Atelier production service API.

Creates a FastAPI application exposing the Atelier reasoning runtime
over HTTP with optional Bearer auth.

Usage (import-safe — no server starts on import)::

    from atelier.core.service.api import create_app
    app = create_app()

Start with uvicorn::

    uvicorn atelier.core.service.api:app --host 127.0.0.1 --port 8787

Or via CLI::

    atelier service start
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware

from atelier.core.capabilities.lesson_promotion import LessonPromoterCapability
from atelier.core.foundation.extractor import extract_candidate
from atelier.core.foundation.models import (
    ReasonBlock,
    Rubric,
    Trace,
    to_jsonable,
)
from atelier.core.foundation.plan_checker import check_plan
from atelier.core.foundation.redaction import redact, redact_list
from atelier.core.foundation.rubric_gate import run_rubric
from atelier.core.service.auth import verify_api_key
from atelier.core.service.config import cfg
from atelier.core.service.schemas import (
    AnalyzeFailuresRequest,
    CheckPlanRequest,
    ExtractReasonBlockRequest,
    FinishTraceRequest,
    HealthResponse,
    HostDetailResponse,
    HostListItemResponse,
    HostRegisterRequest,
    HostRegisterResponse,
    HostStatusRequest,
    HostStatusResponse,
    PatchBlockRequest,
    ReadyResponse,
    ReasoningContextRequest,
    ReasoningContextResponse,
    RecordTraceRequest,
    RecordTraceResponse,
    RescueRequest,
    RunEvalsRequest,
    RunRubricRequest,
    UpsertBlockRequest,
    UpsertEnvironmentRequest,
    UpsertRubricRequest,
)
from atelier.core.service.telemetry import (
    emit_audit,
    emit_product,
    emit_product_local,
    init_product_telemetry,
    set_remote_enabled,
)
from atelier.core.service.telemetry.banner import mark_acknowledged
from atelier.core.service.telemetry.config import save_telemetry_config
from atelier.core.service.telemetry.exporters.posthog_frontend import frontend_telemetry_config
from atelier.core.service.telemetry.frustration import match_frustration
from atelier.core.service.telemetry.local_store import LocalTelemetryStore
from atelier.core.service.telemetry.schema import bucket_duration_ms, hash_identifier, schema_dump
from atelier.gateway.hosts import HostRegistry, HostStatus
from atelier.infra.runtime.cost_tracker import CostTracker, load_cost_history
from atelier.infra.storage.factory import create_store

# --------------------------------------------------------------------------- #
# Store factory — lazily created per-app instance                            #
# --------------------------------------------------------------------------- #

_APP_STORE: Any = None
_HOST_REGISTRY: HostRegistry | None = None

logger = logging.getLogger("atelier.service")


def _get_store() -> Any:
    """Return (or create) the module-level store singleton."""
    global _APP_STORE
    if _APP_STORE is None:
        root = Path(cfg.atelier_root)
        _APP_STORE = create_store(root)
        _APP_STORE.init()
    return _APP_STORE


def _get_host_registry() -> HostRegistry:
    """Return (or create) the module-level host registry singleton."""
    global _HOST_REGISTRY
    if _HOST_REGISTRY is None:
        root = Path(cfg.atelier_root)
        storage_dir = root / "hosts"
        _HOST_REGISTRY = HostRegistry(storage_dir=storage_dir)
    return _HOST_REGISTRY


def _runtime(store: Any) -> Any:
    """Build a lightweight ReasoningRuntime backed by *store*."""
    from atelier.core.runtime import AtelierRuntimeCore
    from atelier.gateway.adapters.runtime import ReasoningRuntime

    rt = ReasoningRuntime.__new__(ReasoningRuntime)
    runtime_root = Path(getattr(store, "root", cfg.atelier_root))
    rt.core_runtime = AtelierRuntimeCore(runtime_root)
    rt.core_runtime.store = store
    rt.store = store
    return rt


def _normalize_lever(operation: str) -> str:
    """Map call operation names to stable savings lever keys."""
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


def _live_savings_events_path(root: Path) -> Path:
    return root / "live_savings_events.jsonl"


def _iter_live_savings_events(root: Path) -> list[dict[str, Any]]:
    path = _live_savings_events_path(root)
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
    """Build token-savings summary for API/dashboard rendering."""
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

    for entry in ops.values() if isinstance(ops, dict) else []:
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

    # USD cost savings (from CostTracker baseline comparison).
    cost_summary = CostTracker(root).total_savings()
    saved_usd = round(float(cost_summary["saved_usd"]) + live_cost_saved, 6)
    would_have_cost = round(float(cost_summary["would_have_cost_usd"]) + live_cost_saved, 6)
    actually_cost = float(cost_summary["actually_cost_usd"])
    saved_pct = round(100.0 * saved_usd / would_have_cost, 2) if would_have_cost > 0 else 0.0
    top_sources = sorted(source_totals.values(), key=lambda row: float(row["cost_saved_usd"]), reverse=True)
    for source in top_sources:
        source["cost_saved_usd"] = round(float(source["cost_saved_usd"]), 6)

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
# Application factory                                                         #
# --------------------------------------------------------------------------- #


def create_app(*, store: Any = None) -> FastAPI:
    """Create and return the FastAPI application.

    Args:
        store: Optional pre-built store — used in tests to inject a
               temporary SQLite instance without touching the filesystem.
    """
    _store = store

    # Validation: Ensure the data store exists
    root = Path(cfg.atelier_root)
    if not root.exists():
        logger.error(
            "Atelier root directory missing: %s. "
            "The service requires an initialized store to function correctly. "
            "Please run 'make install' or the installation script to bootstrap the environment.",
            root,
        )

    def get_store() -> Any:
        nonlocal _store
        if _store is None:
            _store = _get_store()
        return _store

    app = FastAPI(
        title="Atelier Service API",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
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

    def _route_path(request: Request) -> str:
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        return str(path or request.url.path)

    # ------------------------------------------------------------------ #
    # Liveness / readiness                                                #
    # ------------------------------------------------------------------ #

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/ready", response_model=ReadyResponse, tags=["ops"])
    def ready() -> dict[str, Any]:
        try:
            store = _get_store()
            return {
                "status": "ok",
                "storage": {"ok": store is not None, "backend": cfg.storage_backend},
            }
        except Exception:
            return {
                "status": "degraded",
                "storage": {"ok": False, "backend": cfg.storage_backend},
            }

    @app.get("/mcp/status", tags=["ops"])
    def mcp_status() -> list[dict[str, Any]]:
        """Return status of all MCP tools (always present in atelier-mcp)."""
        from atelier.gateway.adapters.mcp_server import TOOLS

        return [
            {
                "tool_name": name,
                "available": True,
                "description": spec.get("description", ""),
            }
            for name, spec in TOOLS.items()
        ]

    # ------------------------------------------------------------------ #
    # Product telemetry                                                   #
    # ------------------------------------------------------------------ #

    @app.get("/telemetry/local", tags=["telemetry"])
    def telemetry_local(
        since: float | None = None,
        event: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        return {"events": LocalTelemetryStore().list_events(since=since, event=event, limit=limit)}

    @app.post("/telemetry/local", tags=["telemetry"])
    def telemetry_local_write(payload: dict[str, Any]) -> dict[str, Any]:
        event = str(payload.get("event", ""))
        props = payload.get("props", {})
        if not isinstance(props, dict):
            raise HTTPException(status_code=400, detail="props must be an object")
        emit_product_local(event, **props)
        return {"ok": True}

    @app.get("/telemetry/summary", tags=["telemetry"])
    def telemetry_summary(since: float | None = None) -> dict[str, Any]:
        return LocalTelemetryStore().summary(since=since)

    @app.get("/telemetry/schema", tags=["telemetry"])
    def telemetry_schema() -> dict[str, Any]:
        return schema_dump()

    @app.get("/telemetry/config", tags=["telemetry"])
    def telemetry_config() -> dict[str, Any]:
        return frontend_telemetry_config()

    @app.post("/telemetry/config", tags=["telemetry"])
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

    @app.post("/telemetry/ack", tags=["telemetry"])
    def telemetry_ack() -> dict[str, Any]:
        mark_acknowledged()
        return frontend_telemetry_config()

    @app.post("/api/hosts/register", tags=["hosts"], response_model=HostRegisterResponse)
    def register_host(request: HostRegisterRequest) -> HostRegisterResponse:
        """Register a new host with Atelier.

        Generates a UUID and fingerprint for the host, stores in registry.
        """
        registry = _get_host_registry()
        registration = registry.register(request.atelier_version)
        return HostRegisterResponse(
            host_id=str(registration.host_id),
            fingerprint=registration.fingerprint.model_dump(),
            registered_at=registration.registered_at.isoformat(),
            atelier_version=registration.atelier_version,
        )

    @app.get("/api/hosts", tags=["hosts"], response_model=list[HostListItemResponse])
    def list_all_hosts() -> list[HostListItemResponse]:
        """List all registered and detected hosts.

        Includes both registered hosts from HostRegistry and legacy hosts
        detected via CLI/config file checks.
        """
        import shutil

        registry = _get_host_registry()
        result: list[HostListItemResponse] = []

        # Add registered hosts from HostRegistry
        for registration in registry.list_all():
            result.append(
                HostListItemResponse(
                    host_id=str(registration.host_id),
                    label=str(registration.fingerprint.hostname),
                    status="registered",
                    active_domains=registration.metadata.get("active_domains", []),
                    mcp_tools=registration.metadata.get("mcp_tools", []),
                    last_seen=(registration.last_seen.isoformat() if registration.last_seen else None),
                    atelier_version=registration.atelier_version,
                )
            )

        # Add legacy detected hosts
        legacy_hosts = [
            ("claude", "Claude Code", "claude"),
            ("codex", "Codex", "codex"),
            ("opencode", "OpenCode", None),
            ("copilot", "VS Code Copilot", None),
            ("gemini", "Gemini CLI", "gemini"),
        ]
        for hid, label, check in legacy_hosts:
            if check == "claude":
                installed = shutil.which("claude") is not None
            elif check == "codex":
                installed = shutil.which("codex") is not None
            elif check == "gemini":
                installed = shutil.which("gemini") is not None
            elif hid == "opencode":
                installed = (Path.home() / ".opencode").exists()
            elif hid == "copilot":
                installed = (Path.home() / ".vscode").exists()
            else:
                installed = False

            if installed and not any(h.host_id == hid for h in result):
                result.append(
                    HostListItemResponse(
                        host_id=hid,
                        label=label,
                        status="installed",
                        active_domains=[],
                        mcp_tools=[],
                    )
                )

        return result

    @app.get("/api/hosts/{host_id}", tags=["hosts"], response_model=HostDetailResponse)
    def get_host_details(host_id: str) -> HostDetailResponse:
        """Get detailed information about a host.

        Returns full host information including fingerprint, metadata,
        installed packs, and MCP tools.
        """
        registry = _get_host_registry()

        # Try to get from registry
        try:
            registration = registry.get(host_id)
            if registration:
                return HostDetailResponse(
                    host_id=str(registration.host_id),
                    label=str(registration.fingerprint.hostname),
                    fingerprint=registration.fingerprint.model_dump(),
                    status="registered",
                    active_domains=registration.metadata.get("active_domains", []),
                    mcp_tools=registration.metadata.get("mcp_tools", []),
                    last_seen=(registration.last_seen.isoformat() if registration.last_seen else None),
                    registered_at=registration.registered_at.isoformat(),
                    atelier_version=registration.atelier_version,
                )
        except Exception:
            pass

        # Check for legacy hosts
        import shutil

        legacy_hosts = {
            "claude": ("Claude Code", "claude"),
            "codex": ("Codex", "codex"),
            "opencode": ("OpenCode", None),
            "copilot": ("VS Code Copilot", None),
            "gemini": ("Gemini CLI", "gemini"),
        }

        if host_id in legacy_hosts:
            label, check = legacy_hosts[host_id]
            if check == "claude":
                installed = shutil.which("claude") is not None
            elif check == "codex":
                installed = shutil.which("codex") is not None
            elif check == "gemini":
                installed = shutil.which("gemini") is not None
            elif host_id == "opencode":
                installed = (Path.home() / ".opencode").exists()
            elif host_id == "copilot":
                installed = (Path.home() / ".vscode").exists()
            else:
                installed = False

            if installed:
                return HostDetailResponse(
                    host_id=host_id,
                    label=label,
                    fingerprint={},
                    status="installed",
                    active_domains=[],
                    mcp_tools=[],
                )

        raise HTTPException(status_code=404, detail=f"Host {host_id} not found")

    @app.get("/api/hosts/{host_id}/status", tags=["hosts"], response_model=HostStatusResponse)
    def get_host_status(host_id: str) -> HostStatusResponse:
        """Get current status of a host.

        Returns last seen timestamp, installed packs, and available MCP tools.
        """
        registry = _get_host_registry()
        registration = registry.get(host_id)

        if not registration:
            raise HTTPException(status_code=404, detail=f"Host {host_id} not found")

        return HostStatusResponse(
            host_id=str(registration.host_id),
            last_seen=(registration.last_seen.isoformat() if registration.last_seen else datetime.utcnow().isoformat()),
            active_domains=registration.metadata.get("active_domains", []),
            available_mcp_tools=registration.metadata.get("mcp_tools", []),
            atelier_version=registration.atelier_version,
        )

    @app.patch("/api/hosts/{host_id}/status", tags=["hosts"], response_model=HostStatusResponse)
    def update_host_status(host_id: str, request: HostStatusRequest) -> HostStatusResponse:
        """Update host status with installed packs and MCP tools.

        The host reports its current state, and we update last_seen timestamp.
        """
        registry = _get_host_registry()
        registration = registry.get(host_id)

        if not registration:
            raise HTTPException(status_code=404, detail=f"Host {host_id} not found")

        # Update metadata with new status
        status_update = HostStatus(
            host_id=UUID(str(registration.host_id)),
            atelier_version=registration.atelier_version,
            active_domains=request.active_domains,
            available_mcp_tools=request.available_mcp_tools,
        )

        updated = registry.update_status(host_id, status_update)
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to update host status")

        return HostStatusResponse(
            host_id=str(updated.host_id),
            last_seen=updated.last_seen.isoformat(),
            active_domains=updated.metadata.get("active_domains", []),
            available_mcp_tools=updated.metadata.get("mcp_tools", []),
            atelier_version=updated.atelier_version,
        )

    @app.get("/hosts", tags=["ops"])
    def list_hosts() -> list[dict[str, Any]]:
        """Return status of agent host installations (legacy endpoint)."""
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
            if check == "claude":
                installed = shutil.which("claude") is not None
            elif check == "codex":
                installed = shutil.which("codex") is not None
            elif check == "gemini":
                installed = shutil.which("gemini") is not None
            elif hid == "opencode":
                installed = (Path.home() / ".opencode").exists()
            elif hid == "copilot":
                installed = (Path.home() / ".vscode").exists()
            else:
                installed = False
            result.append(
                {
                    "name": hid,
                    "label": label,
                    "status": "installed" if installed else "not_installed",
                    "mcp_connected": False,
                }
            )
        return result

    @app.get("/skills", tags=["ops"])
    def list_skills() -> list[dict[str, Any]]:
        """Return available skills with full markdown content (no duplication by source)."""
        from pathlib import Path

        # Go up from src/atelier/core/service/api.py to project root (4 levels)
        root = Path(__file__).parent.parent.parent.parent.parent
        skills: list[dict[str, Any]] = []

        # Shared skills - atelier/integrations/skills/
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

                        skills.append(
                            {
                                "name": skill_dir.name,
                                "description": desc,
                                "content": content,
                            }
                        )

        return skills

    @app.get("/mcp-servers", tags=["ops"])
    def list_mcp_servers() -> dict[str, Any]:
        """Return available MCP server tools."""
        from pathlib import Path

        root = Path(__file__).parent.parent.parent.parent
        tools_file = root / ".atelier" / "mcp_tools.json"

        if tools_file.exists():
            import json

            try:
                return json.loads(tools_file.read_text())  # type: ignore[no-any-return]
            except Exception:
                pass

        # Fallback: return empty structure
        return {
            "tools": [],
            "description": "MCP Server tools - query /mcp/status for current availability",
        }

    @app.get("/skills/{source}/{name}", tags=["ops"])
    def get_skill(source: str, name: str) -> dict[str, Any]:
        """Return a specific skill by source and name."""
        from pathlib import Path

        root = Path(__file__).parent.parent.parent.parent

        if source == "claude":
            skill_dir = root / "integrations" / "claude" / "plugin" / "skills" / name
        elif source == "codex":
            skill_dir = root / ".codex" / "skills" / "atelier" / name
        else:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=f"Unknown source: {source}")

        if not skill_dir.exists():
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=f"Skill not found: {name}")

        md = skill_dir / "SKILL.md"
        if not md.exists():
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=f"SKILL.md not found for: {name}")

        content = md.read_text(encoding="utf-8")
        return {
            "name": name,
            "source": source,
            "content": content,
            "path": str(md),
        }

    @app.get("/metrics", tags=["ops"])
    def metrics(request: Request, _auth: None = Depends(verify_api_key)) -> Any:
        """Return Prometheus metrics in text format.

        Includes:
        - Block and trace counts
        - Context budget and token savings metrics (if prometheus_client is available)
        """
        st = get_store()
        blocks = st.list_blocks()
        traces = st.list_traces(limit=1000)

        # Basic metrics as JSON (for backward compatibility)
        basic_metrics = {
            "block_count": len(blocks),
            "trace_count": len(traces),
            "success_traces": sum(1 for t in traces if t.status == "success"),
            "failed_traces": sum(1 for t in traces if t.status == "failed"),
        }

        wants_prometheus = request.query_params.get("format") == "prometheus" or (
            "text/plain" in request.headers.get("accept", "")
        )
        if not wants_prometheus:
            return basic_metrics

        # Try to expose Prometheus metrics in text format
        try:
            from prometheus_client import REGISTRY, generate_latest

            # Collect all metrics from the default registry
            metrics_text = generate_latest(REGISTRY).decode("utf-8")

            # Add basic metrics as comments if Prometheus is available
            metrics_lines = [
                "# HELP atelier_basic_metrics Basic Atelier metrics",
                "# TYPE atelier_basic_metrics gauge",
                f"atelier_basic_metrics{{type=\"block_count\"}} {basic_metrics['block_count']}",
                f"atelier_basic_metrics{{type=\"trace_count\"}} {basic_metrics['trace_count']}",
                f"atelier_basic_metrics{{type=\"success_traces\"}} {basic_metrics['success_traces']}",
                f"atelier_basic_metrics{{type=\"failed_traces\"}} {basic_metrics['failed_traces']}",
                "",
                metrics_text,
            ]

            # Return as text/plain for Prometheus
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse("\n".join(metrics_lines))
        except ImportError:
            # If Prometheus is not available, return basic metrics as JSON
            return basic_metrics

    # ------------------------------------------------------------------ #
    # Reasoning                                                           #
    # ------------------------------------------------------------------ #

    @app.post(
        "/v1/reasoning/context",
        response_model=ReasoningContextResponse,
        tags=["reasoning"],
        dependencies=[Depends(verify_api_key)],
    )
    def reasoning_context(req: ReasoningContextRequest) -> ReasoningContextResponse:
        rt = _runtime(get_store())
        match_frustration(req.task, surface="api_body")
        with contextlib.suppress(Exception):
            scored = rt.core_runtime.reasoning_reuse.retrieve(
                task=req.task,
                domain=req.domain,
                files=req.files,
                tools=req.tools,
                errors=req.errors,
                limit=req.max_blocks,
                token_budget=req.token_budget,
                dedup=req.dedup,
            )
            for rank, item in enumerate(scored, start=1):
                block = getattr(item, "block", None)
                emit_product(
                    "reasonblock_retrieved",
                    block_id_hash=hash_identifier(str(getattr(block, "id", ""))),
                    domain=str(getattr(block, "domain", req.domain or "")),
                    retrieval_score=float(getattr(item, "score", 0.0)),
                    rank=rank,
                )
        text = rt.get_reasoning_context(
            task=req.task,
            domain=req.domain,
            files=req.files,
            tools=req.tools,
            errors=req.errors,
            max_blocks=req.max_blocks,
            token_budget=req.token_budget,
            dedup=req.dedup,
            include_telemetry=req.include_telemetry,
            agent_id=req.agent_id,
            recall=req.recall,
        )
        payload = text if isinstance(text, dict) else {"context": text}
        tokens_saved = payload.get("tokens_saved_vs_naive") if isinstance(payload, dict) else None
        if isinstance(tokens_saved, int):
            emit_product(
                "value_estimate",
                tokens_saved_estimate=tokens_saved,
                cache_hits=0,
                blocks_applied=len(payload.get("matched_blocks", []) or []),
            )
        return ReasoningContextResponse.model_validate(payload)

    @app.post(
        "/v1/reasoning/check-plan",
        tags=["reasoning"],
        dependencies=[Depends(verify_api_key)],
    )
    def check_plan_endpoint(req: CheckPlanRequest) -> dict[str, Any]:
        match_frustration(req.task, surface="api_body")
        result = check_plan(
            get_store(),
            task=req.task,
            plan=req.plan,
            domain=req.domain,
            files=req.files,
            tools=req.tools,
            errors=req.errors,
        )
        matched_blocks = list(getattr(result, "matched_blocks", []) or [])
        if getattr(result, "status", "") == "blocked":
            emit_product(
                "plan_check_blocked",
                domain=req.domain or "",
                blocking_rule_id=hash_identifier(str(matched_blocks[0] if matched_blocks else "blocked")),
                severity="high",
            )
        else:
            emit_product(
                "plan_check_passed",
                domain=req.domain or "",
                rule_count=len(matched_blocks),
            )
        return to_jsonable(result)

    @app.post(
        "/v1/reasoning/rescue",
        tags=["reasoning"],
        dependencies=[Depends(verify_api_key)],
    )
    def rescue(req: RescueRequest) -> dict[str, Any]:
        rt = _runtime(get_store())
        match_frustration(req.task, surface="api_body")
        match_frustration(req.error, surface="api_body")
        result = rt.rescue_failure(
            task=req.task,
            error=req.error,
            domain=req.domain,
            files=req.files,
            recent_actions=req.recent_actions,
        )
        matched = list(getattr(result, "matched_blocks", []) or [])
        emit_product(
            "rescue_offered",
            cluster_id_hash=hash_identifier(str(matched[0] if matched else "unmatched_rescue")),
            rescue_type="reasonblock" if matched else "summary",
        )
        return to_jsonable(result)

    # ------------------------------------------------------------------ #
    # Rubrics                                                             #
    # ------------------------------------------------------------------ #

    @app.get("/v1/rubrics", tags=["rubrics"], dependencies=[Depends(verify_api_key)])
    def list_rubrics(domain: str | None = None) -> list[dict[str, Any]]:
        rubrics = get_store().list_rubrics(domain=domain)
        return [to_jsonable(r) for r in rubrics]

    @app.get("/v1/rubrics/{rubric_id}", tags=["rubrics"], dependencies=[Depends(verify_api_key)])
    def get_rubric(rubric_id: str) -> dict[str, Any]:
        rubric = get_store().get_rubric(rubric_id)
        if rubric is None:
            raise HTTPException(status_code=404, detail=f"Rubric not found: {rubric_id}")
        return to_jsonable(rubric)

    @app.post("/v1/rubrics", tags=["rubrics"], dependencies=[Depends(verify_api_key)])
    def create_rubric(req: UpsertRubricRequest) -> dict[str, Any]:
        rubric = Rubric(**req.model_dump())
        get_store().upsert_rubric(rubric, write_yaml=False)
        emit_audit(
            actor="api",
            action="upsert_rubric",
            resource_type="rubric",
            resource_id=rubric.id,
            store=get_store(),
        )
        return to_jsonable(rubric)

    @app.post(
        "/v1/rubrics/run",
        tags=["rubrics"],
        dependencies=[Depends(verify_api_key)],
    )
    def run_rubric_endpoint(req: RunRubricRequest) -> dict[str, Any]:
        rubric = get_store().get_rubric(req.rubric_id)
        if rubric is None:
            raise HTTPException(status_code=404, detail=f"Rubric not found: {req.rubric_id}")
        result = run_rubric(rubric, req.checks)
        return to_jsonable(result)

    # ------------------------------------------------------------------ #
    # Traces                                                              #
    # ------------------------------------------------------------------ #

    @app.get(
        "/v1/traces",
        tags=["traces"],
        dependencies=[Depends(verify_api_key)],
    )
    def list_traces(
        limit: int = 50,
        offset: int = 0,
        domain: str | None = None,
        status: str | None = None,
        agent: str | None = None,
    ) -> list[dict[str, Any]]:
        traces = get_store().list_traces(limit=limit, offset=offset, domain=domain, status=status, agent=agent)
        return [to_jsonable(t) for t in traces]

    @app.get(
        "/v1/traces/{trace_id}",
        tags=["traces"],
        dependencies=[Depends(verify_api_key)],
    )
    def get_trace(trace_id: str) -> dict[str, Any]:
        trace = get_store().get_trace(trace_id)
        if trace is not None:
            return to_jsonable(trace)
        # Fallback: look for a RunLedger JSON in runs/ (live/unrecorded sessions).
        ledger_path = Path(cfg.atelier_root) / "runs" / f"{trace_id}.json"
        if ledger_path.exists():
            from atelier.infra.runtime.run_ledger import RunLedger as _RunLedger

            try:
                led = _RunLedger.load(ledger_path)
                snap = led.snapshot()
                summary = led.to_trace_summary()
                raw_status = snap.get("status", "running")
                if raw_status in ("complete", "success"):
                    mapped_status = "success"
                elif raw_status in ("error", "failed"):
                    mapped_status = "failed"
                else:
                    mapped_status = "partial"
                return {
                    "id": snap.get("run_id", trace_id),
                    "run_id": snap.get("run_id", trace_id),
                    "agent": snap.get("agent") or "unknown",
                    "domain": snap.get("domain") or "",
                    "task": snap.get("task") or "",
                    "status": mapped_status,
                    "files_touched": summary["files_touched"],
                    "tools_called": summary["tools_called"],
                    "commands_run": summary["commands_run"],
                    "errors_seen": snap.get("errors_seen", []),
                    "repeated_failures": [{"signature": f, "count": 1} for f in snap.get("repeated_failures", [])],
                    "diff_summary": "",
                    "output_summary": "",
                    "validation_results": [],
                    "created_at": snap.get("created_at", ""),
                    "_live": True,
                }
            except Exception:
                pass
        raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")

    @app.post(
        "/v1/traces",
        response_model=RecordTraceResponse,
        tags=["traces"],
        dependencies=[Depends(verify_api_key)],
    )
    def record_trace(req: RecordTraceRequest) -> RecordTraceResponse:
        payload = req.model_dump()
        # Redact secrets from user-supplied fields.
        for key in ("task", "diff_summary", "output_summary"):
            if isinstance(payload.get(key), str):
                payload[key] = redact(payload[key])
        for key in ("errors_seen",):
            if isinstance(payload.get(key), list):
                payload[key] = redact_list([str(v) for v in payload[key]])
        # Redact string values inside enriched records
        for key in ("files_touched", "commands_run"):
            items = payload.get(key)
            if not isinstance(items, list):
                continue
            redacted_items: list[Any] = []
            for item in items:
                if isinstance(item, str):
                    redacted_items.append(redact(item))
                elif isinstance(item, dict):
                    # Redact string fields within dicts (CommandRecord, FileEditRecord)
                    redacted_item = {}
                    for k, v in item.items():
                        redacted_item[k] = redact(v) if isinstance(v, str) else v
                    redacted_items.append(redacted_item)
                else:
                    redacted_items.append(item)
            payload[key] = redacted_items
        if "id" not in payload:
            payload["id"] = Trace.make_id(payload.get("task", "untitled"), payload.get("agent", "agent"))
        trace = Trace.model_validate(payload)
        get_store().record_trace(trace, write_json=False)
        emit_audit(
            actor="api",
            action="record_trace",
            resource_type="trace",
            resource_id=trace.id,
            store=get_store(),
        )
        return RecordTraceResponse(id=trace.id)

    @app.post(
        "/v1/traces/{trace_id}/events",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["traces"],
        dependencies=[Depends(verify_api_key)],
    )
    def add_trace_event(trace_id: str, req: dict[str, Any]) -> None:
        # Lightweight stub — events stored to audit log only for now.
        emit_audit(
            actor="api",
            action="trace_event",
            resource_type="trace",
            resource_id=trace_id,
            store=get_store(),
        )

    @app.post(
        "/v1/traces/{trace_id}/finish",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["traces"],
        dependencies=[Depends(verify_api_key)],
    )
    def finish_trace(trace_id: str, req: FinishTraceRequest) -> None:
        st = get_store()
        trace = st.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
        # Update status field by re-recording with updated values.
        updated = Trace(
            **{
                **to_jsonable(trace),
                "status": req.status,
                "diff_summary": redact(req.diff_summary),
                "output_summary": redact(req.output_summary),
            }
        )
        st.record_trace(updated, write_json=False)

    # ------------------------------------------------------------------ #
    # ReasonBlocks                                                        #
    # ------------------------------------------------------------------ #

    @app.get(
        "/v1/reasonblocks",
        tags=["reasonblocks"],
        dependencies=[Depends(verify_api_key)],
    )
    def list_blocks(
        domain: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        st = get_store()
        blocks = st.search_blocks(query) if query else st.list_blocks(domain=domain)
        return [to_jsonable(b) for b in blocks]

    @app.post(
        "/v1/reasonblocks",
        tags=["reasonblocks"],
        dependencies=[Depends(verify_api_key)],
    )
    def create_block(req: UpsertBlockRequest) -> dict[str, Any]:
        block = ReasonBlock(**req.model_dump())
        get_store().upsert_block(block, write_markdown=False)
        emit_audit(
            actor="api",
            action="upsert_block",
            resource_type="reasonblock",
            resource_id=block.id,
            store=get_store(),
        )
        return to_jsonable(block)

    @app.patch(
        "/v1/reasonblocks/{block_id}",
        tags=["reasonblocks"],
        dependencies=[Depends(verify_api_key)],
    )
    def patch_block(block_id: str, req: PatchBlockRequest) -> dict[str, Any]:
        st = get_store()
        if req.status is not None:
            changed = st.update_block_status(block_id, req.status)
            if not changed:
                raise HTTPException(status_code=404, detail=f"Block not found: {block_id}")
            emit_audit(
                actor="api",
                action="patch_block_status",
                resource_type="reasonblock",
                resource_id=block_id,
                store=st,
            )
        block = st.get_block(block_id)
        if block is None:
            raise HTTPException(status_code=404, detail=f"Block not found: {block_id}")
        return to_jsonable(block)

    # ------------------------------------------------------------------ #
    # Environments                                                        #
    # ------------------------------------------------------------------ #

    @app.get(
        "/v1/environments",
        tags=["environments"],
        dependencies=[Depends(verify_api_key)],
    )
    def list_environments() -> list[dict[str, Any]]:
        from atelier.core.foundation.environments import load_packaged_environments

        envs = load_packaged_environments()
        return [to_jsonable(e) for e in envs]

    @app.post(
        "/v1/environments",
        tags=["environments"],
        dependencies=[Depends(verify_api_key)],
    )
    def create_environment(req: UpsertEnvironmentRequest) -> dict[str, Any]:
        from atelier.core.foundation.models import Environment

        env = Environment(**req.model_dump())
        # Environments are file-backed — validate and echo back.
        return to_jsonable(env)

    # ------------------------------------------------------------------ #
    # Evals                                                               #
    # ------------------------------------------------------------------ #

    @app.get("/v1/evals", tags=["evals"], dependencies=[Depends(verify_api_key)])
    def list_evals(domain: str | None = None) -> dict[str, Any]:
        return {"evals": [], "note": "eval storage not yet wired"}

    @app.post("/v1/evals/run", tags=["evals"], dependencies=[Depends(verify_api_key)])
    def run_evals(req: RunEvalsRequest) -> dict[str, Any]:
        return {"status": "queued", "note": "eval runner not yet wired"}

    # ------------------------------------------------------------------ #
    # Benchmarks (removed — pack benchmark runner deprecated)             #
    # ------------------------------------------------------------------ #

    @app.post("/api/benchmarks/run", tags=["benchmarks"], dependencies=[Depends(verify_api_key)])
    def run_benchmark(req: dict[str, Any]) -> dict[str, Any]:
        """Pack benchmark runner has been removed. Use domain bundles instead."""
        raise HTTPException(status_code=410, detail="Pack benchmark runner removed. Use domain bundles.")

    @app.get("/api/benchmarks", tags=["benchmarks"], dependencies=[Depends(verify_api_key)])
    def list_benchmarks(bundle_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Returns empty benchmark list (pack benchmarks removed)."""
        return {"benchmarks": [], "total": 0}

    @app.get(
        "/api/benchmarks/{benchmark_id}",
        tags=["benchmarks"],
        dependencies=[Depends(verify_api_key)],
    )
    def get_benchmark(benchmark_id: str) -> dict[str, Any]:
        """Pack benchmark runner has been removed."""
        raise HTTPException(status_code=410, detail="Pack benchmark runner removed.")

        # ------------------------------------------------------------------ #

    # Extract / Failures                                                  #
    # ------------------------------------------------------------------ #

    @app.post(
        "/v1/extract/reasonblock",
        tags=["extract"],
        dependencies=[Depends(verify_api_key)],
    )
    def extract_reasonblock(req: ExtractReasonBlockRequest) -> dict[str, Any]:
        st = get_store()
        trace = st.get_trace(req.trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"Trace not found: {req.trace_id}")
        candidate = extract_candidate(trace)
        if req.save:
            st.upsert_block(candidate.block, write_markdown=False)
            emit_audit(
                actor="api",
                action="extract_reasonblock",
                resource_type="reasonblock",
                resource_id=candidate.block.id,
                store=st,
            )
        return {
            "block": to_jsonable(candidate.block),
            "confidence": candidate.confidence,
            "reasons": candidate.reasons,
            "saved": req.save,
        }

    @app.post(
        "/v1/failures/analyze",
        tags=["failures"],
        dependencies=[Depends(verify_api_key)],
    )
    def analyze_failures(req: AnalyzeFailuresRequest) -> dict[str, Any]:
        from atelier.core.improvement.failure_analyzer import FailureAnalyzer

        st = get_store()
        traces = st.list_traces(domain=req.domain, status="failed", limit=req.limit)
        snapshots = [to_jsonable(t) for t in traces]
        analyzer = FailureAnalyzer(store=st)
        try:
            clusters = analyzer.analyze()
            return {"clusters": [to_jsonable(c) for c in clusters]}
        except Exception:
            # Fall back to standalone analysis when store-based analyzer fails.
            from atelier.core.improvement.failure_analyzer import analyze_failures as _af

            clusters = _af(snapshots)
            return {"clusters": [to_jsonable(c) for c in clusters]}

    @app.get(
        "/v1/lessons/inbox",
        tags=["lessons"],
        dependencies=[Depends(verify_api_key)],
    )
    def lesson_inbox(domain: str | None = None, limit: int = 25) -> dict[str, Any]:
        promoter = LessonPromoterCapability(get_store())
        lessons = promoter.inbox(domain=domain, limit=limit)
        return {"lessons": [lesson.model_dump(mode="json") for lesson in lessons]}

    @app.post(
        "/v1/lessons/decide",
        tags=["lessons"],
        dependencies=[Depends(verify_api_key)],
    )
    def lesson_decide(payload: dict[str, Any]) -> dict[str, Any]:
        promoter = LessonPromoterCapability(get_store())
        try:
            return promoter.decide(
                lesson_id=str(payload.get("lesson_id", "")),
                decision=str(payload.get("decision", "")),
                reviewer=str(payload.get("reviewer", "")),
                reason=str(payload.get("reason", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------ #
    # Metrics / savings                                                   #
    # ------------------------------------------------------------------ #

    @app.get(
        "/v1/metrics/savings",
        tags=["metrics"],
        dependencies=[Depends(verify_api_key)],
    )
    def metrics_savings() -> dict[str, Any]:
        root = Path(cfg.atelier_root)
        tracker = CostTracker(root)
        return tracker.total_savings()

    @app.get(
        "/v1/savings/summary",
        tags=["metrics"],
        dependencies=[Depends(verify_api_key)],
    )
    def savings_summary(window_days: int = 14) -> dict[str, Any]:
        root = Path(cfg.atelier_root)
        return _savings_summary_payload(root, window_days=window_days)

    # ------------------------------------------------------------------ #
    # Compatibility endpoints (frontend dashboard)                        #
    # ------------------------------------------------------------------ #

    @app.get("/blocks", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_blocks() -> list[dict[str, Any]]:
        """Compatibility: GET /blocks -> maps to /v1/reasonblocks."""
        st = get_store()
        blocks = st.list_blocks()
        return [to_jsonable(b) for b in blocks]

    @app.get("/blocks/{block_id}", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_block(block_id: str) -> dict[str, Any]:
        """Compatibility: GET /blocks/{block_id} -> maps to /v1/reasonblocks/{block_id}."""
        st = get_store()
        block = st.get_block(block_id)
        if block is None:
            raise HTTPException(status_code=404, detail=f"Block not found: {block_id}")
        return to_jsonable(block)

    @app.get("/traces", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_traces(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Compatibility: GET /traces -> maps to /v1/traces with proper frontend format.

        Also surfaces live sessions from RunLedger JSON files that have not yet
        been committed to the SQLite store via atelier_record_trace.
        """
        traces = get_store().list_traces(limit=limit, offset=offset)
        result = [to_jsonable(t) for t in traces]

        # Collect run_ids already persisted in SQLite so we don't duplicate them.
        known_run_ids: set[str] = {t.run_id for t in traces if t.run_id}

        # Scan the runs/ directory for RunLedger JSON files (live + unrecorded sessions).
        runs_dir = Path(cfg.atelier_root) / "runs"
        if runs_dir.exists():
            # local import avoids circular dependency
            from atelier.infra.runtime.run_ledger import RunLedger as _RunLedger

            live: list[dict[str, Any]] = []
            for ledger_path in sorted(runs_dir.glob("*.json"), reverse=True):
                try:
                    led = _RunLedger.load(ledger_path)
                    # Read raw JSON for fields snapshot() recomputes (created_at, updated_at).
                    raw_json: dict[str, Any] = json.loads(ledger_path.read_text("utf-8"))
                except Exception:
                    continue
                if led.run_id in known_run_ids:
                    continue  # already recorded in SQLite
                snap = led.snapshot()
                raw_status = snap.get("status", "running")
                if raw_status in ("complete", "success"):
                    mapped_status = "success"
                elif raw_status in ("running",):
                    mapped_status = "partial"
                elif raw_status in ("error", "failed"):
                    mapped_status = "failed"
                else:
                    mapped_status = "partial"  # unknown → treat as in-progress
                tools_called = [{"name": t, "args_hash": "", "count": 1} for t in snap.get("tools_called", [])]
                repeated_failures = [{"signature": f, "count": 1} for f in snap.get("repeated_failures", [])]
                live.append(
                    {
                        "id": snap.get("run_id", ""),
                        "run_id": snap.get("run_id"),
                        "agent": snap.get("agent") or "unknown",
                        "domain": snap.get("domain") or "",
                        "task": snap.get("task") or "",
                        "status": mapped_status,
                        "files_touched": snap.get("files_touched", []),
                        "tools_called": tools_called,
                        "commands_run": snap.get("commands_run", []),
                        "errors_seen": snap.get("errors_seen", []),
                        "repeated_failures": repeated_failures,
                        "diff_summary": "",
                        "output_summary": "",
                        "validation_results": [],
                        # Use raw JSON timestamps — snapshot() regenerates them on the fly.
                        "created_at": raw_json.get("created_at", ""),
                        "_live": True,  # frontend hint: this is a live/unsaved session
                    }
                )
            result = live + result

        # Sort combined result newest-first before applying the limit slice.
        def _ts(rec: dict[str, Any]) -> float:
            raw = rec.get("created_at") or ""
            try:
                from datetime import datetime

                if isinstance(raw, str) and raw:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    return dt.timestamp()
            except Exception:
                pass
            return 0.0

        result.sort(key=_ts, reverse=True)
        return result[:limit]

    @app.get("/savings", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_savings() -> dict[str, Any]:
        """Compatibility: GET /savings -> maps to /v1/metrics/savings."""
        root = Path(cfg.atelier_root)
        tracker = CostTracker(root)
        return tracker.total_savings()

    @app.get("/savings/summary", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_savings_summary(window_days: int = 14) -> dict[str, Any]:
        """Compatibility: GET /savings/summary -> maps to /v1/savings/summary."""
        root = Path(cfg.atelier_root)
        return _savings_summary_payload(root, window_days=window_days)

    @app.get("/calls", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_calls(limit: int = 200) -> list[dict[str, Any]]:
        """Compatibility: GET /calls -> reads call entries from cost history."""
        root = Path(cfg.atelier_root)
        history = load_cost_history(root)
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
        all_calls.sort(key=lambda c: c.get("at", ""), reverse=True)
        return all_calls[:limit]

    @app.get("/clusters", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_clusters(limit: int = 100) -> list[dict[str, Any]]:
        """Compatibility: GET /clusters -> maps to /v1/failures/analyze."""
        from atelier.core.improvement.failure_analyzer import FailureAnalyzer

        st = get_store()
        traces = st.list_traces(status="failed", limit=limit)
        snapshots = [to_jsonable(t) for t in traces]
        runs_dir = Path(cfg.atelier_root) / "runs"
        analyzer = FailureAnalyzer(runs_dir=runs_dir)
        try:
            clusters = analyzer.analyze()
            return [to_jsonable(c) for c in clusters]
        except Exception:
            from atelier.core.improvement.failure_analyzer import analyze_failures as _af

            clusters = _af(snapshots)
            return [to_jsonable(c) for c in clusters]

    @app.get("/environments", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_environments() -> list[dict[str, Any]]:
        """Compatibility: GET /environments -> maps to /v1/environments with frontend format."""
        from atelier.core.foundation.environments import load_packaged_environments

        envs = load_packaged_environments()
        result = []
        for e in envs:
            env_dict = to_jsonable(e)
            result.append(
                {
                    "environment": {
                        "id": env_dict["id"],
                        "name": env_dict.get("name", ""),
                        "status": "active",
                        "details": {k: v for k, v in env_dict.items() if k not in ("id", "name")},
                    },
                    "rubric": None,
                }
            )
        return result

    @app.get("/overview", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_overview() -> dict[str, Any]:
        """Compatibility: GET /overview -> returns stats summary."""
        from atelier.core.foundation.environments import load_packaged_environments
        from atelier.core.improvement.failure_analyzer import FailureAnalyzer

        st = get_store()
        blocks = st.list_blocks()
        traces = st.list_traces(limit=10000)

        total_raw_tokens = sum(
            sum(c.get("input_tokens", 0) + c.get("output_tokens", 0) for c in entry.get("calls", []))
            for entry in load_cost_history(Path(cfg.atelier_root)).get("operations", {}).values()
        )

        root = Path(cfg.atelier_root)
        history = load_cost_history(root)
        ops = history.get("operations", {})
        total_cost = 0.0
        total_saved = 0.0
        for op_key in ops:
            s = CostTracker(root).savings_for(op_key)
            calls = s.get("calls_count", 0)
            if calls > 0:
                total_cost += s.get("current_cost_usd", 0.0) * calls
                total_saved += s.get("delta_vs_base_usd", 0.0) * calls

        return {
            "total_traces": len(traces),
            "total_blocks": len([b for b in blocks if b.status == "active"]),
            "total_rubrics": len(st.list_rubrics()),
            "total_environments": len(load_packaged_environments()),
            "total_clusters": len(
                [
                    to_jsonable(c)
                    for c in FailureAnalyzer(Path(cfg.atelier_root) / "runs").analyze()
                    if len(getattr(c, "trace_ids", [])) > 0
                ]
            ),
            "total_raw_tokens_estimate": total_raw_tokens,
            "total_saved_tokens_estimate": int(total_saved * 1000000 / 3.0),
            "total_compressed_tokens_estimate": 0,
            "average_compression_ratio": 1.0,
            "estimated_total_cost_usd": round(total_cost, 6),
            "estimated_saved_cost_usd": round(total_saved, 6),
            "usd_per_1k_tokens": 3.0,
            "is_estimate": True,
        }

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
        return artifact.model_dump(mode="json")  # type: ignore[no-any-return]

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

        # Always check if there's a corresponding trace to fetch the full conversation history
        # for imported sessions, as the RunLedger JSON doesn't store the full chat history.
        store_inst = get_store()
        traces = store_inst.list_traces(limit=10000)
        trace = next((t for t in traces if getattr(t, "run_id", None) == run_id), None)

        conversations: list[dict[str, Any]] = []
        if trace and trace.raw_artifact_ids:
            for art_id in trace.raw_artifact_ids:
                artifact = store_inst.get_raw_artifact(art_id)
                if artifact:
                    try:
                        raw_content = store_inst.read_raw_artifact_content(artifact)
                        from atelier.gateway.integrations._session_parser import parse_session_turns

                        conversations = parse_session_turns(raw_content, artifact.source)
                    except Exception:
                        pass
                    break  # use first artifact only

        if snap:
            if conversations:
                snap["conversations"] = conversations
            return snap
        if trace:
            return {
                "run_id": run_id,
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
                "note": "Imported from session file — no live ledger exists.",
                "trace": trace.model_dump(mode="json"),
            }

        return {"run_id": run_id, "status": "not_found"}

    @app.post("/install/{host_id}", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_install_host(host_id: str) -> dict[str, Any]:
        """Compatibility: POST /install/{host_id} -> installs MCP config for host."""
        import json
        from pathlib import Path

        configs: dict[str, dict[str, Any]] = {
            "claude": {
                "mcpServers": {
                    "atelier": {
                        "command": "uv",
                        "args": ["run", "atelier-mcp"],
                        "env": {"ATELIER_ROOT": str(Path.home() / ".atelier")},
                    }
                }
            },
            "codex": {
                "agents": {
                    "skills": [
                        "atelier-task",
                        "atelier-check-plan",
                        "atelier-rescue",
                        "atelier-record-trace",
                    ]
                },
                "mcp": {
                    "servers": {
                        "atelier": {
                            "command": "uv",
                            "args": ["run", "atelier-mcp"],
                            "env": {"ATELIER_ROOT": str(Path.home() / ".atelier")},
                        }
                    }
                },
            },
            "opencode": {
                "mcpServers": {
                    "atelier": {
                        "command": "uv",
                        "args": ["run", "atelier-mcp"],
                        "env": {"ATELIER_ROOT": str(Path.home() / ".atelier")},
                    }
                }
            },
            "copilot": {
                "mcpServers": {
                    "atelier": {
                        "command": "uv",
                        "args": ["run", "atelier-mcp"],
                        "env": {"ATELIER_ROOT": str(Path.home() / ".atelier")},
                    }
                }
            },
            "gemini": {
                "mcpServers": {
                    "atelier": {
                        "command": "uv",
                        "args": ["run", "atelier-mcp"],
                        "env": {"ATELIER_ROOT": str(Path.home() / ".atelier")},
                    }
                }
            },
        }

        if host_id not in configs:
            return {"status": "error", "message": f"Unknown host: {host_id}"}

        config = configs[host_id]
        config_dir = Path.home()
        existing: dict[str, Any]

        try:
            if host_id == "claude":
                claude_dir = config_dir / ".claude"
                claude_dir.mkdir(exist_ok=True)
                config_path = claude_dir / "claude_desktop_config.json"
                existing = (
                    json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {"mcpServers": {}}
                )
                existing.setdefault("mcpServers", {}).update(config["mcpServers"])
                config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

            elif host_id == "codex":
                codex_dir = config_dir / ".codex"
                codex_dir.mkdir(exist_ok=True)
                config_path = codex_dir / "config.jsonc"
                existing = (
                    json.loads(config_path.read_text(encoding="utf-8").replace("// ", ""))
                    if config_path.exists()
                    else {"agents": {}, "mcp": {}}
                )
                existing.setdefault("agents", {}).update(config["agents"])
                existing.setdefault("mcp", {}).update(config["mcp"])
                config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

            elif host_id == "opencode":
                opencode_dir = config_dir / ".opencode"
                opencode_dir.mkdir(exist_ok=True)
                config_path = opencode_dir / "config.jsonc"
                existing = (
                    json.loads(config_path.read_text(encoding="utf-8").replace("// ", ""))
                    if config_path.exists()
                    else {"mcpServers": {}}
                )
                existing.setdefault("mcpServers", {}).update(config["mcpServers"])
                config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

            elif host_id == "copilot":
                copilot_dir = config_dir / ".vscode"
                copilot_dir.mkdir(exist_ok=True)
                config_path = copilot_dir / "settings.json"
                existing = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
                existing.setdefault("mcp", {}).setdefault("servers", {}).update(config["mcpServers"])
                config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

            elif host_id == "gemini":
                gemini_dir = config_dir / ".gemini"
                gemini_dir.mkdir(exist_ok=True)
                config_path = gemini_dir / "settings.json"
                existing = (
                    json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {"mcpServers": {}}
                )
                existing.setdefault("mcpServers", {}).update(config["mcpServers"])
                config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

            return {"status": "success", "installed": host_id, "config_path": str(config_path)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------ #
    # Host Integrations (Phase D.7)                                       #
    # ------------------------------------------------------------------ #

    @app.get("/api/integrations/hosts", tags=["integrations"], dependencies=[Depends(verify_api_key)])
    def list_host_integrations() -> dict[str, Any]:
        """List all available host integrations."""
        import yaml

        # Find hosts directory relative to this module
        hosts_dir = Path(__file__).parent.parent / "integrations" / "hosts"
        if not hosts_dir.exists():
            return {"hosts": [], "total": 0}

        hosts = []
        for host_file in sorted(hosts_dir.glob("*.yaml")):
            try:
                with open(host_file) as f:
                    config = yaml.safe_load(f)
                if config:
                    hosts.append(
                        {
                            "host_id": config.get("host_id"),
                            "name": config.get("name"),
                            "version": config.get("version", "1.0.0"),
                            "description": config.get("description", ""),
                            "platforms": config.get("platforms", []),
                            "detection": config.get("detection", {}),
                            "recommended_domains": config.get("recommended_domains", []),
                            "mcp_servers": config.get("mcp", {}).get("servers", []),
                        }
                    )
            except Exception:
                pass

        return {"hosts": hosts, "total": len(hosts)}

    @app.get(
        "/api/integrations/hosts/{host_id}",
        tags=["integrations"],
        dependencies=[Depends(verify_api_key)],
    )
    def get_host_integration(host_id: str) -> dict[str, Any]:
        """Get host integration configuration."""
        import yaml

        # Find hosts directory relative to this module
        hosts_dir = Path(__file__).parent.parent / "integrations" / "hosts"
        host_file = hosts_dir / f"{host_id}.yaml"

        if not host_file.exists():
            raise HTTPException(status_code=404, detail=f"Host integration not found: {host_id}")

        try:
            with open(host_file) as f:
                config = yaml.safe_load(f)

            if not config:
                raise HTTPException(status_code=404, detail=f"Host integration not found: {host_id}")

            return {
                "host_id": config.get("host_id"),
                "name": config.get("name"),
                "version": config.get("version", "1.0.0"),
                "description": config.get("description", ""),
                "platforms": config.get("platforms", []),
                "detection": config.get("detection", {}),
                "recommended_domains": config.get("recommended_domains", []),
                "mcp_servers": config.get("mcp", {}).get("servers", []),
                "installation": config.get("installation", {}),
                "prompt_templates": config.get("prompt_templates", []),
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # ------------------------------------------------------------------ #
    # Memory routes (/v1/memory/*)                                       #
    # ------------------------------------------------------------------ #

    _mem_store: Any = None

    def _get_mem_store() -> Any:
        nonlocal _mem_store
        if _mem_store is None:
            from atelier.infra.storage.factory import make_memory_store

            root = Path(cfg.atelier_root)
            _mem_store = make_memory_store(root)
        return _mem_store

    @app.get("/v1/memory/blocks")
    def memory_list_or_get(
        agent_id: str,
        label: str | None = None,
        include_tombstoned: bool = False,
        limit: int = 200,
    ) -> Any:
        """List all memory blocks for an agent, or get a single block by label."""
        mem = _get_mem_store()
        if label is not None:
            block = mem.get_block(agent_id, label, include_tombstoned=include_tombstoned)
            if block is None:
                raise HTTPException(status_code=404, detail=f"Block not found: {label!r}")
            return block
        return mem.list_blocks(agent_id, include_tombstoned=include_tombstoned, limit=limit)

    @app.post("/v1/memory/blocks")
    def memory_upsert_block(payload: dict[str, Any]) -> Any:
        """Create or update a memory block."""
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

    @app.post("/v1/memory/archive")
    def memory_archive_passage(payload: dict[str, Any]) -> Any:
        """Archive a text passage into long-term memory."""
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

    @app.post("/v1/memory/recall")
    def memory_recall_passages(payload: dict[str, Any]) -> Any:
        """Search archived passages for a query."""
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


if __name__ == "__main__":  # pragma: no cover
    main()
