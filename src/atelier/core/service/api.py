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

import contextlib
import json
import logging
import sqlite3
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from atelier.core.capabilities.pricing import usage_cost_usd
from atelier.core.service.config import cfg

if TYPE_CHECKING:
    from atelier.core.foundation.models import Trace
    from atelier.core.foundation.store import ReasoningStore

logger = logging.getLogger(__name__)


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


def _build_external_analytics_summary(store: Any, *, days: int) -> dict[str, Any]:
    runs = store.list_external_analytics_runs(days=days, limit=200)
    latest: list[dict[str, Any]] = []
    seen: set[str] = set()
    successful_runs = sum(1 for run in runs if run.get("ok"))
    for run in runs:
        tool = str(run.get("tool") or "unknown")
        if tool in seen:
            continue
        latest.append(
            {
                "id": run.get("id"),
                "tool": tool,
                "period": run.get("period"),
                "source": run.get("source"),
                "ok": run.get("ok"),
                "returncode": run.get("returncode"),
                "summary": run.get("summary") or {},
                "collected_at": run.get("collected_at"),
            }
        )
        seen.add(tool)
    return {
        "runs_total": len(runs),
        "successful_runs": successful_runs,
        "failed_runs": len(runs) - successful_runs,
        "latest": latest,
    }


def _build_external_analytics_detail(
    store: Any,
    *,
    days: int,
    tool: str | None,
    limit: int,
) -> dict[str, Any]:
    runs = store.list_external_analytics_runs(tool=tool, days=days, limit=limit)
    latest_by_tool: dict[str, dict[str, Any]] = {}
    successful_runs = sum(1 for run in runs if run.get("ok"))
    for run in runs:
        tool_name = str(run.get("tool") or "unknown")
        latest_by_tool.setdefault(tool_name, run)
    return {
        "totals": {
            "runs_total": len(runs),
            "successful_runs": successful_runs,
            "failed_runs": len(runs) - successful_runs,
        },
        "latest_by_tool": latest_by_tool,
        "runs": runs,
    }


_BILLABLE_ANALYTICS_EVENTS = {"prompt", "cached_prompt", "cache_create", "result", "thinking"}
_NATIVE_ANALYTICS_TOOLS = {
    "Read",
    "Bash",
    "Edit",
    "Grep",
    "Glob",
    "Write",
    "Agent",
    "ListDir",
    "bash",
    "run_shell_command",
    "exec_command",
    "shell",
    "read_file",
    "replace",
    "apply_patch",
    "view",
}


def _llm_usage_cost(
    model_id: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    thinking_tokens: int = 0,
) -> float:
    return usage_cost_usd(
        model_id or "_default",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        thinking_tokens=thinking_tokens,
    )


def _usage_total_tokens(usage: dict[str, Any]) -> int:
    return sum(
        int(usage.get(field) or 0)
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
            "thinking_tokens",
        )
    )


def _model_usage_cost(usage: dict[str, Any]) -> float:
    return _llm_usage_cost(
        str(usage.get("model") or "_default"),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cached_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        thinking_tokens=int(usage.get("thinking_tokens") or 0),
    )


def _trace_model_usages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    raw_usages = payload.get("model_usages")
    if isinstance(raw_usages, list):
        for raw_usage in raw_usages:
            if not isinstance(raw_usage, dict):
                continue
            model_id = str(raw_usage.get("model") or payload.get("model") or "").strip()
            usage: dict[str, Any] = {
                "input_tokens": int(raw_usage.get("input_tokens") or 0),
                "output_tokens": int(raw_usage.get("output_tokens") or 0),
                "thinking_tokens": int(raw_usage.get("thinking_tokens") or 0),
                "cached_input_tokens": int(raw_usage.get("cached_input_tokens") or 0),
                "cache_creation_input_tokens": int(raw_usage.get("cache_creation_input_tokens") or 0),
            }
            if not model_id and not any(usage.values()):
                continue
            bucket = aggregated.setdefault(model_id, {"model": model_id, **{key: 0 for key in usage}})
            for field, value in usage.items():
                bucket[field] += value

    if not aggregated:
        model_id = str(payload.get("model") or "").strip()
        usage = {
            "model": model_id,
            "input_tokens": int(payload.get("input_tokens") or 0),
            "output_tokens": int(payload.get("output_tokens") or 0),
            "thinking_tokens": int(payload.get("thinking_tokens") or 0),
            "cached_input_tokens": int(payload.get("cached_input_tokens") or 0),
            "cache_creation_input_tokens": int(payload.get("cache_creation_input_tokens") or 0),
        }
        if model_id or any(value for key, value in usage.items() if key != "model"):
            aggregated[model_id] = usage

    usages = list(aggregated.values())
    usages.sort(key=lambda usage: (_model_usage_cost(usage), _usage_total_tokens(usage)), reverse=True)
    return usages


def _trace_primary_model(payload: dict[str, Any], model_usages: list[dict[str, Any]] | None = None) -> str:
    usages = model_usages if model_usages is not None else _trace_model_usages(payload)
    if usages:
        return str(usages[0].get("model") or payload.get("model") or "")
    return str(payload.get("model") or "")


def _trace_cost_from_payload(payload: dict[str, Any]) -> float:
    model_usages = _trace_model_usages(payload)
    if model_usages:
        return round(sum(_model_usage_cost(usage) for usage in model_usages), 8)
    return _llm_usage_cost(
        str(payload.get("model") or "_default"),
        input_tokens=int(payload.get("input_tokens") or 0),
        output_tokens=int(payload.get("output_tokens") or 0),
        cache_read_tokens=int(payload.get("cached_input_tokens") or 0),
        cache_write_tokens=int(payload.get("cache_creation_input_tokens") or 0),
        thinking_tokens=int(payload.get("thinking_tokens") or 0),
    )


def _analytics_event_cost(model_id: str | None, event_type: str, input_tokens: int, output_tokens: int) -> float:
    if event_type == "prompt":
        return _llm_usage_cost(model_id, input_tokens=input_tokens)
    if event_type == "cached_prompt":
        return _llm_usage_cost(model_id, cache_read_tokens=input_tokens)
    if event_type == "cache_create":
        return _llm_usage_cost(model_id, cache_write_tokens=input_tokens)
    if event_type == "result":
        return _llm_usage_cost(model_id, output_tokens=output_tokens)
    if event_type == "thinking":
        return _llm_usage_cost(model_id, thinking_tokens=output_tokens)
    if event_type == "tool_call":
        # Tool outputs re-enter the next prompt window and use input pricing.
        return _llm_usage_cost(model_id, input_tokens=output_tokens)
    return 0.0


def _filter_analytics_rows(
    rows: list[dict[str, Any]],
    *,
    agent: str | None = None,
    model: str | None = None,
    category: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    agent_match = (agent or "").strip().lower()
    model_match = (model or "").strip().lower()
    search_match = (search or "").strip().lower()

    filtered: list[dict[str, Any]] = []
    for row in rows:
        row_agent = str(row.get("agent") or "").strip().lower()
        row_model = str(row.get("model") or "").strip().lower()
        if agent_match and row_agent != agent_match:
            continue
        if model_match and row_model != model_match:
            continue
        if category and row.get("category") != category:
            continue
        if search_match:
            tool_name = str(row.get("tool_name") or "").lower()
            sub_command = str(row.get("sub_command") or "").lower()
            if search_match not in tool_name and search_match not in sub_command:
                continue
        filtered.append(row)
    return filtered


def _build_analytics_summary(rows: list[dict[str, Any]], *, days: int | None) -> dict[str, Any]:
    total_output_tokens = sum(
        int(row.get("output_tokens") or 0)
        for row in rows
        if row.get("event_type") in {"result", "thinking", "tool_call"}
    )
    user_input_tokens = sum(int(row.get("input_tokens") or 0) for row in rows if row.get("event_type") == "user_string")
    tool_calls = sum(int(row.get("call_count") or 1) for row in rows if row.get("event_type") == "tool_call")
    unique_tools = len(
        {
            str(row.get("tool_name") or "")
            for row in rows
            if row.get("event_type") == "tool_call" and row.get("tool_name")
        }
    )
    cached_prompt_tokens = sum(
        int(row.get("input_tokens") or 0) for row in rows if row.get("event_type") == "cached_prompt"
    )
    model_response_tokens = sum(int(row.get("output_tokens") or 0) for row in rows if row.get("event_type") == "result")
    model_thinking_tokens = sum(
        int(row.get("output_tokens") or 0) for row in rows if row.get("event_type") == "thinking"
    )
    tool_input_tokens = sum(int(row.get("input_tokens") or 0) for row in rows if row.get("event_type") == "tool_call")
    tool_output_tokens = sum(int(row.get("output_tokens") or 0) for row in rows if row.get("event_type") == "tool_call")
    total_cost = round(
        sum(float(row.get("cost") or 0.0) for row in rows if row.get("event_type") in _BILLABLE_ANALYTICS_EVENTS),
        6,
    )
    effective_days = max(1, days or 1)
    estimated_monthly_cost = round(total_cost * (30 / effective_days), 6)

    tool_costs: defaultdict[str, float] = defaultdict(float)
    for row in rows:
        tool_costs[str(row.get("tool_name") or "—")] += float(row.get("cost") or 0.0)
    top_cost_driver = max(tool_costs.items(), key=lambda item: item[1])[0] if tool_costs else "—"

    return {
        "total_cost": total_cost,
        "estimated_monthly_cost": estimated_monthly_cost,
        "top_cost_driver": top_cost_driver,
        "user_input_tokens": user_input_tokens,
        "model_thinking_tokens": model_thinking_tokens,
        "llm_output_tokens": model_response_tokens + tool_input_tokens,
        "tool_output_tokens": tool_output_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "tool_calls": tool_calls,
        "unique_tools": unique_tools,
        "total_output_tokens": total_output_tokens,
        "row_count": len(rows),
    }


def _tool_category(tool_name: str) -> str:
    return "Native / Unoptimized" if tool_name in _NATIVE_ANALYTICS_TOOLS else "Atelier Optimized"


def _trace_analytics_events(
    trace_id: str,
    agent: str,
    host: str | None,
    created_at: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    model_usages = _trace_model_usages(payload)
    primary_model = _trace_primary_model(payload, model_usages)
    session_host = host or agent

    def _append_event(
        *,
        model: str,
        event_type: str,
        tool_name: str,
        category: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        call_count: int = 1,
        sub_command: str | None = None,
    ) -> None:
        events.append(
            {
                "trace_id": trace_id,
                "host": session_host,
                "agent": agent,
                "model": model,
                "event_type": event_type,
                "tool_name": tool_name,
                "sub_command": sub_command,
                "category": category,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "first_seen": created_at,
                "last_seen": created_at,
                "call_count": call_count,
                "session_count": 1,
                "cost": _analytics_event_cost(model, event_type, input_tokens, output_tokens),
            }
        )

    user_prompt_tokens = int(payload.get("user_prompt_tokens") or 0)
    if user_prompt_tokens > 0:
        _append_event(
            model=primary_model,
            event_type="user_string",
            tool_name="User Entered String",
            category="User Activity",
            input_tokens=user_prompt_tokens,
        )

    for usage in model_usages:
        model_id = str(usage.get("model") or primary_model)
        input_tokens = int(usage.get("input_tokens") or 0)
        cached_input_tokens = int(usage.get("cached_input_tokens") or 0)
        cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        thinking_tokens = int(usage.get("thinking_tokens") or 0)

        if input_tokens > 0:
            _append_event(
                model=model_id,
                event_type="prompt",
                tool_name="Context Window (Base)",
                category="LLM Context",
                input_tokens=input_tokens,
            )
        if cached_input_tokens > 0:
            _append_event(
                model=model_id,
                event_type="cached_prompt",
                tool_name="Cached Prompt (Cache Read)",
                category="LLM Context (Cache Read)",
                input_tokens=cached_input_tokens,
            )
        if cache_write_tokens > 0:
            _append_event(
                model=model_id,
                event_type="cache_create",
                tool_name="Cache Write",
                category="LLM Context (Cache Write)",
                input_tokens=cache_write_tokens,
            )
        if output_tokens > 0:
            _append_event(
                model=model_id,
                event_type="result",
                tool_name="Assistant Response",
                category="LLM Generation",
                output_tokens=output_tokens,
            )
        if thinking_tokens > 0:
            _append_event(
                model=model_id,
                event_type="thinking",
                tool_name="Thinking",
                category="LLM Generation",
                output_tokens=thinking_tokens,
            )

    for raw_tool in payload.get("tools_called") or []:
        if not isinstance(raw_tool, dict):
            continue
        tool_name = str(raw_tool.get("name") or "unknown")
        _append_event(
            model=primary_model,
            event_type="tool_call",
            tool_name=tool_name,
            category=_tool_category(tool_name),
            input_tokens=int(raw_tool.get("input_tokens") or 0),
            output_tokens=int(raw_tool.get("output_tokens") or 0),
            call_count=int(raw_tool.get("count") or 1),
        )

    return events


def _group_analytics_rows(events: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    grouped_rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    session_ids: dict[tuple[Any, ...], set[str]] = {}

    for event in events:
        key = (
            event["host"],
            event["agent"],
            event["model"],
            event["event_type"],
            event["tool_name"],
            event["sub_command"],
            event["category"],
        )
        row = grouped_rows.get(key)
        if row is None:
            row = dict(event)
            grouped_rows[key] = row
            session_ids[key] = {str(event["trace_id"])}
            continue

        row["input_tokens"] += int(event.get("input_tokens") or 0)
        row["output_tokens"] += int(event.get("output_tokens") or 0)
        row["call_count"] += int(event.get("call_count") or 0)
        row["cost"] = round(float(row.get("cost") or 0.0) + float(event.get("cost") or 0.0), 8)
        row["first_seen"] = min(str(row.get("first_seen") or ""), str(event.get("first_seen") or ""))
        row["last_seen"] = max(str(row.get("last_seen") or ""), str(event.get("last_seen") or ""))
        session_ids[key].add(str(event["trace_id"]))

    rows: list[dict[str, Any]] = []
    for key, row in grouped_rows.items():
        row["session_count"] = len(session_ids[key])
        row.pop("trace_id", None)
        rows.append(row)

    rows.sort(key=lambda row: str(row.get("last_seen") or ""), reverse=True)
    return rows[:limit]


def _query_analytics_rows(
    db_path: str | Path,
    *,
    grouped: bool,
    days: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    params: list[Any] = []

    sql = f"""
        SELECT id, agent, host, payload, created_at
        FROM traces
        WHERE 1=1 {"AND created_at >= datetime('now', '-' || ? || ' days')" if days else ""}
        ORDER BY created_at DESC
    """

    if days:
        params.append(days)

    events: list[dict[str, Any]] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(sql, params).fetchall():
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            events.extend(
                _trace_analytics_events(
                    str(row["id"]),
                    str(row["agent"] or ""),
                    str(row["host"] or ""),
                    str(row["created_at"] or ""),
                    payload,
                )
            )

    if grouped:
        return _group_analytics_rows(events, limit=limit)

    events.sort(key=lambda event: str(event.get("last_seen") or ""), reverse=True)
    rows = [dict(event) for event in events[:limit]]
    for row in rows:
        row.pop("trace_id", None)
    return rows


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
        "session_id",
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


_OBSERVED_OPTIMIZATION_TITLES = {
    "search_read": "Search/read compaction",
    "batch_edit": "Batch edit",
    "compact_lifecycle": "Tool-output compaction",
    "scoped_recall": "Scoped recall",
    "reasonblock_inject": "ReasonBlock injection",
    "cached_read": "Cached reuse",
    "delta_read": "Delta read",
    "structure_map": "Structure map",
}


def _trace_created_at(trace: Trace) -> datetime:
    created = trace.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created


def _trace_run_key(trace: Trace) -> str:
    return str(trace.session_id or trace.id)


def _trace_total_tokens(trace: Trace) -> int:
    from atelier.core.capabilities.session_optimizer import effective_input_tokens

    return effective_input_tokens(trace) + int(trace.output_tokens or 0)


def _trace_cache_leverage(trace: Trace) -> float:
    from atelier.core.capabilities.session_optimizer import effective_input_tokens

    effective_input = effective_input_tokens(trace)
    if effective_input <= 0:
        return 0.0
    return round(int(trace.cached_input_tokens or 0) / effective_input, 4)


def _recent_traces(store: ReasoningStore, *, window_days: int) -> list[Trace]:
    cutoff = datetime.now(UTC) - timedelta(days=max(1, window_days))
    traces = store.list_traces(limit=5000)
    recent = [trace for trace in traces if _trace_created_at(trace) >= cutoff]
    return sorted(recent, key=_trace_created_at)


def _tracked_saved_tokens(store: ReasoningStore, trace: Trace) -> tuple[int, int]:
    try:
        rows = store.list_context_budgets(_trace_run_key(trace))
    except Exception as exc:
        logger.warning("Failed to load context budgets for trace %s: %s", trace.id, exc)
        return 0, 0

    saved_tokens = 0
    tracked_turns = 0
    for row in rows:
        tracked_turns += int(row.tool_calls or 0)
        lever_keys = [str(key or "") for key in row.lever_savings]
        non_marker_keys = [key for key in lever_keys if not key.startswith("tool:")]
        if non_marker_keys and all(key.startswith("compact_tool_output:") for key in non_marker_keys):
            continue
        actual_tokens = (
            int(row.input_tokens or 0)
            + int(row.cache_read_tokens or 0)
            + int(row.cache_write_tokens or 0)
            + int(row.output_tokens or 0)
        )
        naive_tokens = int(row.naive_input_tokens or 0)
        saved_tokens += max(0, naive_tokens - actual_tokens)
    return saved_tokens, tracked_turns


def _window_metrics(store: ReasoningStore, traces: list[Trace]) -> dict[str, Any]:
    from atelier.core.capabilities.session_optimizer import trace_cost_usd

    entries: list[dict[str, Any]] = []
    for trace in traces:
        saved_tokens, tracked_turns = _tracked_saved_tokens(store, trace)
        entries.append(
            {
                "trace_id": trace.id,
                "tokens": _trace_total_tokens(trace),
                "cost_usd": trace_cost_usd(trace),
                "cache_leverage": _trace_cache_leverage(trace),
                "saved_tokens": saved_tokens,
                "tracked_turns": tracked_turns,
                "created_at": _trace_created_at(trace).isoformat(),
            }
        )

    count = len(entries)
    if count == 0:
        return {
            "trace_count": 0,
            "avg_tokens": 0,
            "avg_cost_usd": 0.0,
            "avg_cache_leverage": 0.0,
            "avg_saved_tokens": 0,
            "tracked_turns": 0,
            "from": None,
            "to": None,
        }

    return {
        "trace_count": count,
        "avg_tokens": round(sum(int(item["tokens"]) for item in entries) / count),
        "avg_cost_usd": round(sum(float(item["cost_usd"]) for item in entries) / count, 6),
        "avg_cache_leverage": round(sum(float(item["cache_leverage"]) for item in entries) / count, 4),
        "avg_saved_tokens": round(sum(int(item["saved_tokens"]) for item in entries) / count),
        "tracked_turns": sum(int(item["tracked_turns"]) for item in entries),
        "from": entries[0]["created_at"],
        "to": entries[-1]["created_at"],
    }


def _pct_change(before: float, after: float) -> float:
    if before <= 0:
        if after <= 0:
            return 0.0
        return 100.0
    return round(((after - before) / before) * 100.0, 1)


def _impact_verdict(tokens_delta_pct: float, cost_delta_pct: float, cache_delta_pct: float) -> str:
    improvements = 0
    regressions = 0

    if tokens_delta_pct <= -5.0:
        improvements += 1
    elif tokens_delta_pct >= 5.0:
        regressions += 1

    if cost_delta_pct <= -5.0:
        improvements += 1
    elif cost_delta_pct >= 5.0:
        regressions += 1

    if cache_delta_pct >= 5.0:
        improvements += 1
    elif cache_delta_pct <= -5.0:
        regressions += 1

    if improvements >= 2 and regressions == 0:
        return "improved"
    if regressions >= 2 and improvements == 0:
        return "regressed"
    if improvements == 0 and regressions == 0:
        return "no_change"
    return "mixed"


def _build_impact_validation(
    store: ReasoningStore,
    traces: list[Trace],
    *,
    window_days: int,
) -> dict[str, Any]:
    if len(traces) < 2:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "window_days": window_days,
            "strategy": "chronological_halves",
            "verdict": "insufficient_data",
            "before": _window_metrics(store, []),
            "after": _window_metrics(store, []),
            "deltas": {
                "tokens_pct": 0.0,
                "cost_pct": 0.0,
                "cache_leverage_pct": 0.0,
                "saved_tokens_pct": 0.0,
            },
            "notes": ["Need at least two traces in the selected window to compare before vs after behavior."],
        }

    midpoint = max(1, len(traces) // 2)
    before = _window_metrics(store, traces[:midpoint])
    after = _window_metrics(store, traces[midpoint:])

    tokens_delta_pct = _pct_change(float(before["avg_tokens"]), float(after["avg_tokens"]))
    cost_delta_pct = _pct_change(float(before["avg_cost_usd"]), float(after["avg_cost_usd"]))
    cache_delta_pct = _pct_change(float(before["avg_cache_leverage"]), float(after["avg_cache_leverage"]))
    saved_tokens_delta_pct = _pct_change(float(before["avg_saved_tokens"]), float(after["avg_saved_tokens"]))

    notes: list[str] = []
    if after["avg_tokens"] < before["avg_tokens"]:
        notes.append("Average token load fell in the later half of the window.")
    if after["avg_cost_usd"] < before["avg_cost_usd"]:
        notes.append("Average per-trace cost dropped in the later half of the window.")
    if after["avg_cache_leverage"] > before["avg_cache_leverage"]:
        notes.append("Cache leverage improved in the later half of the window.")
    if not notes:
        notes.append("Later traces look materially similar to earlier traces in this window.")

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": window_days,
        "strategy": "chronological_halves",
        "verdict": _impact_verdict(tokens_delta_pct, cost_delta_pct, cache_delta_pct),
        "before": before,
        "after": after,
        "deltas": {
            "tokens_pct": tokens_delta_pct,
            "cost_pct": cost_delta_pct,
            "cache_leverage_pct": cache_delta_pct,
            "saved_tokens_pct": saved_tokens_delta_pct,
        },
        "notes": notes,
    }


def _lever_title(lever: str) -> str:
    return _OBSERVED_OPTIMIZATION_TITLES.get(lever, lever.replace("_", " ").title())


def _build_auto_optimizations(
    savings_payload: dict[str, Any],
    live_events: list[dict[str, Any]],
    *,
    window_days: int,
) -> list[dict[str, Any]]:
    start_day = datetime.now(UTC).date() - timedelta(days=max(1, window_days) - 1)
    per_lever = {
        str(key): int(value or 0)
        for key, value in (savings_payload.get("per_lever") or {}).items()
        if isinstance(key, str) and int(value or 0) > 0
    }
    observed: dict[str, dict[str, Any]] = {}

    for event in live_events:
        tokens_saved = int(event.get("tokens_saved", 0) or 0)
        if tokens_saved <= 0:
            continue
        try:
            at_date = datetime.fromisoformat(str(event.get("at", "")).replace("Z", "+00:00")).date()
        except Exception as exc:
            logger.debug("Bad timestamp %r in savings event, using today: %s", event.get("at"), exc)
            at_date = datetime.now(UTC).date()
        if at_date < start_day:
            continue
        lever = _normalize_lever(str(event.get("lever") or event.get("tool_name") or "plugin_live"))
        item = observed.setdefault(
            lever,
            {
                "id": lever,
                "title": _lever_title(lever),
                "tokens_saved": 0,
                "cost_saved_usd": 0.0,
                "calls_saved": 0,
                "sessions": set(),
                "tools": set(),
            },
        )
        item["tokens_saved"] += tokens_saved
        item["cost_saved_usd"] += float(event.get("cost_saved_usd", 0.0) or 0.0)
        item["calls_saved"] += int(event.get("calls_saved", 0) or 0)
        session_id = str(event.get("session_id") or "")
        if session_id:
            item["sessions"].add(session_id)
        tool_name = str(event.get("tool_name") or "")
        if tool_name:
            item["tools"].add(tool_name)

    rows: list[dict[str, Any]] = []
    for lever, tokens_saved in per_lever.items():
        item = observed.get(
            lever,
            {
                "id": lever,
                "title": _lever_title(lever),
                "tokens_saved": 0,
                "cost_saved_usd": 0.0,
                "calls_saved": 0,
                "sessions": set(),
                "tools": set(),
            },
        )
        item["tokens_saved"] = max(int(item["tokens_saved"]), tokens_saved)
        rows.append(
            {
                "id": item["id"],
                "title": item["title"],
                "tokens_saved": int(item["tokens_saved"]),
                "cost_saved_usd": round(float(item["cost_saved_usd"]), 6),
                "calls_saved": int(item["calls_saved"]),
                "session_count": len(item["sessions"]),
                "tools": sorted(item["tools"]),
            }
        )

    return sorted(rows, key=lambda row: int(row["tokens_saved"]), reverse=True)[:8]


def _reread_kind(event: dict[str, Any]) -> str | None:
    lever = _normalize_lever(str(event.get("lever") or ""))
    if lever in {"delta_read", "structure_map"}:
        return lever
    mode = str(event.get("read_mode") or "").strip().lower()
    if mode == "range":
        return "delta_read"
    if mode == "outline":
        return "structure_map"
    return None


def _build_reread_telemetry(root: Path, *, window_days: int) -> dict[str, Any]:
    start_day = datetime.now(UTC).date() - timedelta(days=max(1, window_days) - 1)
    by_kind: dict[str, dict[str, Any]] = {}
    by_path: dict[str, dict[str, Any]] = {}
    total_tokens_saved = 0
    total_cost_saved = 0.0
    event_count = 0

    for event in _iter_live_savings_events(root):
        tokens_saved = int(event.get("tokens_saved", 0) or 0)
        if tokens_saved <= 0:
            continue
        try:
            at = datetime.fromisoformat(str(event.get("at", "")).replace("Z", "+00:00"))
        except Exception as exc:
            logger.debug("Bad timestamp %r in reread event, using now: %s", event.get("at"), exc)
            at = datetime.now(UTC)
        if at.date() < start_day:
            continue
        kind = _reread_kind(event)
        if kind is None:
            continue

        event_count += 1
        total_tokens_saved += tokens_saved
        total_cost_saved += float(event.get("cost_saved_usd", 0.0) or 0.0)

        kind_row = by_kind.setdefault(
            kind,
            {
                "id": kind,
                "title": _lever_title(kind),
                "event_count": 0,
                "tokens_saved": 0,
                "cost_saved_usd": 0.0,
                "path_count": 0,
                "last_seen_at": None,
            },
        )
        kind_row["event_count"] += 1
        kind_row["tokens_saved"] += tokens_saved
        kind_row["cost_saved_usd"] += float(event.get("cost_saved_usd", 0.0) or 0.0)
        kind_row["last_seen_at"] = at.isoformat()

        path = str(event.get("path") or "(unknown file)")
        path_row = by_path.setdefault(
            path,
            {
                "path": path,
                "event_count": 0,
                "tokens_saved": 0,
                "kinds": set(),
            },
        )
        path_row["event_count"] += 1
        path_row["tokens_saved"] += tokens_saved
        path_row["kinds"].add(kind)

    for kind_row in by_kind.values():
        matching_paths = [row for row in by_path.values() if kind_row["id"] in row["kinds"]]
        kind_row["path_count"] = len(matching_paths)
        kind_row["cost_saved_usd"] = round(float(kind_row["cost_saved_usd"]), 6)

    top_paths = []
    for path_row in sorted(by_path.values(), key=lambda row: int(row["tokens_saved"]), reverse=True)[:5]:
        top_paths.append(
            {
                "path": path_row["path"],
                "event_count": int(path_row["event_count"]),
                "tokens_saved": int(path_row["tokens_saved"]),
                "kinds": sorted(path_row["kinds"]),
            }
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": window_days,
        "event_count": event_count,
        "total_tokens_saved": total_tokens_saved,
        "total_cost_saved_usd": round(total_cost_saved, 6),
        "kinds": sorted(by_kind.values(), key=lambda row: int(row["tokens_saved"]), reverse=True),
        "top_paths": top_paths,
    }


def _cheaper_model_for(model: str | None) -> str | None:
    normalized = (model or "").strip().lower()
    if not normalized:
        return None
    mappings = [
        (("claude-opus", "claude-sonnet"), "claude-haiku-4-5"),
        (("gpt-5.5", "gpt-5.4-pro", "chat-latest"), "gpt-5.4-mini"),
        (("gpt-5.4", "gpt-5.3-codex"), "gpt-5.4-mini"),
        (("o3",), "o4-mini-deep-research"),
        (("gemini-3.1-pro", "gemini-2.5-pro"), "gemini-2.5-flash"),
        (("gemini-3-flash", "gemini-2.5-flash"), "gemini-2.5-flash-lite"),
        (("grok-4.3", "grok-4.20"), "grok-4-1-fast-non-reasoning"),
        (("deepseek-v4-pro", "deepseek-reasoner"), "deepseek-chat"),
    ]
    for prefixes, target in mappings:
        if any(prefix in normalized for prefix in prefixes):
            return target
    return None


def _routine_trace_reason(trace: Trace) -> str | None:
    tool_calls = sum(int(tool.count or 0) for tool in trace.tools_called)
    file_count = len(trace.files_touched)
    total_tokens = _trace_total_tokens(trace)
    output_tokens = int(trace.output_tokens or 0)

    if trace.status != "success":
        return None
    if trace.errors_seen or trace.repeated_failures:
        return None
    if total_tokens > 120_000:
        return None
    if output_tokens > 8_000:
        return None
    if tool_calls > 4:
        return None
    if file_count > 2:
        return None
    if any(not bool(result.passed) for result in trace.validation_results):
        return None
    return "Success with bounded tokens, limited tools, and no visible recovery work."


def _build_model_routing_simulation(traces: list[Trace], *, window_days: int) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    total_current_cost = 0.0
    total_simulated_cost = 0.0
    rerouted_tokens = 0

    for trace in traces:
        target_model = _cheaper_model_for(trace.model)
        if target_model is None:
            continue
        reason = _routine_trace_reason(trace)
        if reason is None:
            continue

        from atelier.core.capabilities.pricing import get_model_pricing
        from atelier.core.capabilities.session_optimizer import trace_cost_usd

        current_cost = trace_cost_usd(trace)
        simulated_cost = get_model_pricing(target_model).cost_usd(
            input_tokens=int(trace.input_tokens or 0),
            output_tokens=int(trace.output_tokens or 0),
            cache_read_tokens=int(trace.cached_input_tokens or 0),
            cache_write_tokens=int(trace.cache_creation_input_tokens or 0),
        )
        if simulated_cost >= current_cost:
            continue

        total_current_cost += current_cost
        total_simulated_cost += simulated_cost
        rerouted_tokens += _trace_total_tokens(trace)
        candidates.append(
            {
                "trace_id": trace.id,
                "task": trace.task,
                "current_model": trace.model or "_default",
                "target_model": target_model,
                "current_cost_usd": round(current_cost, 6),
                "simulated_cost_usd": round(simulated_cost, 6),
                "estimated_cost_saved_usd": round(current_cost - simulated_cost, 6),
                "total_tokens": _trace_total_tokens(trace),
                "reason": reason,
            }
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": window_days,
        "candidate_count": len(candidates),
        "estimated_cost_saved_usd": round(total_current_cost - total_simulated_cost, 6),
        "current_cost_usd": round(total_current_cost, 6),
        "simulated_cost_usd": round(total_simulated_cost, 6),
        "total_tokens_rerouted": rerouted_tokens,
        "heuristic": "Conservative routine-trace filter: success only, no errors, <=120K total tokens, <=4 tool calls, <=2 files touched.",
        "candidates": sorted(candidates, key=lambda row: float(row["estimated_cost_saved_usd"]), reverse=True)[:8],
    }


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
            except Exception as exc:
                logger.debug("Bad timestamp %r in savings call, using today: %s", at_raw, exc)
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
        except Exception as exc:
            logger.debug("Bad timestamp %r in live savings event, using today: %s", at_raw, exc)
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


def _optimization_runtime_coverage() -> list[dict[str, Any]]:
    return [
        {
            "host": "claude",
            "mode": "runtime",
            "automatic_at_start": True,
            "automatic_mid_session": True,
            "advisory_only": False,
            "surfaces": [
                "SessionStart hook",
                "PostToolUse telemetry hook",
                "Host instructions",
            ],
            "notes": (
                "Live SessionStart guidance is emitted from the Claude plugin, and PostToolUse telemetry now "
                "emits one-shot nudges for no-edit drift, session-quality degradation, and loop-rescue intervention."
            ),
        },
        {
            "host": "codex",
            "mode": "runtime",
            "automatic_at_start": True,
            "automatic_mid_session": True,
            "advisory_only": False,
            "surfaces": [
                "SessionStart update hook",
                "Savings reporter hook",
                "Host instructions",
            ],
            "notes": (
                "Codex now gets live SessionStart budget guidance plus the same PostToolUse nudges for no-edit drift, "
                "session-quality degradation, and loop-rescue intervention."
            ),
        },
        {
            "host": "copilot",
            "mode": "task_wrapper",
            "automatic_at_start": True,
            "automatic_mid_session": False,
            "advisory_only": False,
            "surfaces": [
                "Copilot preflight task",
                "atelier CLI shell task",
                "Copilot instructions",
                "Atelier chatmode",
            ],
            "notes": (
                "Copilot now has a task-driven preflight that runs atelier CLI checks before chat work begins. "
                "Native Copilot chat remains instruction-backed if that task path is skipped."
            ),
        },
        {
            "host": "gemini",
            "mode": "wrapper",
            "automatic_at_start": True,
            "automatic_mid_session": False,
            "advisory_only": False,
            "surfaces": [
                "atelier-gemini wrapper",
                "Gemini extension commands",
                "Gemini extension instruction",
            ],
            "notes": (
                "Gemini can now launch through an Atelier wrapper that emits live start guidance before handing off to Gemini CLI. "
                "Extension-only startup still falls back to installed instructions."
            ),
        },
        {
            "host": "opencode",
            "mode": "wrapper",
            "automatic_at_start": True,
            "automatic_mid_session": False,
            "advisory_only": False,
            "surfaces": [
                "atelier-opencode wrapper",
                "OpenCode agent instruction",
            ],
            "notes": (
                "OpenCode can now launch through an Atelier wrapper that emits live start guidance before handing off to opencode. "
                "Agent-profile-only sessions still rely on installed instructions."
            ),
        },
    ]


def _optimization_lever_tokens(
    per_lever: dict[str, int],
    *,
    exact: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
) -> int:
    total = 0
    for lever, tokens in per_lever.items():
        if lever in exact or any(lever.startswith(prefix) for prefix in prefixes):
            total += int(tokens or 0)
    return total


def _optimization_lever_examples(
    top_sources: list[dict[str, Any]],
    *,
    exact: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
) -> list[str]:
    examples: list[str] = []
    for source in top_sources:
        lever = str(source.get("lever") or "")
        if lever in exact or any(lever.startswith(prefix) for prefix in prefixes):
            tool_name = str(source.get("tool_name") or lever)
            if tool_name not in examples:
                examples.append(tool_name)
    return examples[:3]


def _implemented_optimization_catalog(savings_payload: dict[str, Any]) -> list[dict[str, Any]]:
    from atelier.core.capabilities.session_optimizer import SUPPORTED_OPTIMIZER_HOSTS

    supported_hosts = list(SUPPORTED_OPTIMIZER_HOSTS)
    per_lever = {
        str(key): int(value or 0)
        for key, value in (savings_payload.get("per_lever") or {}).items()
        if isinstance(key, str)
    }
    top_sources = [item for item in (savings_payload.get("top_sources") or []) if isinstance(item, dict)]

    catalog = [
        {
            "id": "session_budget_optimizer",
            "title": "Session budget optimizer",
            "category": "session_control",
            "automation": "Automatic on Claude/Codex and wrapper/task-enforced at start for Copilot/Gemini/OpenCode",
            "status": "active",
            "observed_tokens_saved": 0,
            "applies_to": supported_hosts,
            "notes": (
                "Injects smallest-plan, bounded-context, and delivery-or-stop guardrails. "
                "Also powers the trace-based optimization recommendations shown below."
            ),
            "examples": ["SessionStart guidance", "No-edit 10m nudge", "atelier optimize"],
        },
        {
            "id": "search_read",
            "title": "Search + read compaction",
            "category": "tool_supervision",
            "automation": "Automatic when smart search/read tools are used",
            "status": "active",
            "observed_tokens_saved": _optimization_lever_tokens(per_lever, exact=("search_read",)),
            "applies_to": supported_hosts,
            "notes": "Targets grep/read workflows into bounded chunks instead of feeding whole-file context.",
            "examples": _optimization_lever_examples(top_sources, exact=("search_read",)),
        },
        {
            "id": "batch_edit",
            "title": "Batch edit",
            "category": "tool_supervision",
            "automation": "Automatic when batch edit paths are used",
            "status": "active",
            "observed_tokens_saved": _optimization_lever_tokens(per_lever, exact=("batch_edit",)),
            "applies_to": supported_hosts,
            "notes": "Combines related edits into a single operation to reduce repeated edit context and tool chatter.",
            "examples": _optimization_lever_examples(top_sources, exact=("batch_edit",)),
        },
        {
            "id": "compact_tool_output",
            "title": "Tool-output compaction",
            "category": "output_control",
            "automation": "Automatic in Atelier MCP paths, optional in Claude host hook",
            "status": "active",
            "observed_tokens_saved": _optimization_lever_tokens(
                per_lever,
                exact=("compact_lifecycle",),
                prefixes=("compact_tool_output:",),
            ),
            "applies_to": supported_hosts,
            "notes": "Reduces verbose tool output before it becomes future prompt context.",
            "examples": _optimization_lever_examples(
                top_sources,
                exact=("compact_lifecycle",),
                prefixes=("compact_tool_output:",),
            ),
        },
        {
            "id": "cached_read",
            "title": "Cached read reuse",
            "category": "cache",
            "automation": "Automatic when cached token paths are available",
            "status": "active",
            "observed_tokens_saved": _optimization_lever_tokens(per_lever, exact=("cached_read",)),
            "applies_to": supported_hosts,
            "notes": "Prefers cached or discounted context where the host/provider exposes it.",
            "examples": _optimization_lever_examples(top_sources, exact=("cached_read",)),
        },
        {
            "id": "scoped_recall",
            "title": "Scoped recall",
            "category": "memory",
            "automation": "Automatic when memory recall paths are used",
            "status": "active",
            "observed_tokens_saved": _optimization_lever_tokens(per_lever, exact=("scoped_recall",)),
            "applies_to": supported_hosts,
            "notes": "Pulls narrower memory slices instead of replaying broad historical context.",
            "examples": _optimization_lever_examples(top_sources, exact=("scoped_recall",)),
        },
        {
            "id": "reasonblock_inject",
            "title": "ReasonBlock injection",
            "category": "reasoning_reuse",
            "automation": "Automatic when matching reasoning blocks are selected",
            "status": "active",
            "observed_tokens_saved": _optimization_lever_tokens(per_lever, exact=("reasonblock_inject",)),
            "applies_to": supported_hosts,
            "notes": "Reuses prior solved procedures instead of re-deriving them from scratch.",
            "examples": _optimization_lever_examples(top_sources, exact=("reasonblock_inject",)),
        },
        {
            "id": "ast_truncation",
            "title": "AST-aware truncation",
            "category": "context_pruning",
            "automation": "Automatic when structured truncation paths are used",
            "status": "active",
            "observed_tokens_saved": _optimization_lever_tokens(per_lever, exact=("ast_truncation",)),
            "applies_to": supported_hosts,
            "notes": "Keeps syntax-relevant structure while trimming low-value surrounding text.",
            "examples": _optimization_lever_examples(top_sources, exact=("ast_truncation",)),
        },
        {
            "id": "loop_detection",
            "title": "Loop detection + rescue",
            "category": "control_plane",
            "automation": "Automatic detection, partial host-native rescue surfacing",
            "status": "active",
            "observed_tokens_saved": 0,
            "applies_to": supported_hosts,
            "notes": (
                "Detects repeated search/read or retry loops and redirects the agent toward rescue instead of repeated spend. "
                "This is a qualitative safeguard rather than a direct savings counter today."
            ),
            "examples": ["search_read_loop", "repeated bash failures"],
        },
    ]
    for item in catalog:
        observed_tokens = item.get("observed_tokens_saved")
        if isinstance(observed_tokens, int) and observed_tokens > 0 and item["status"] == "active":
            item["status"] = "active_observed"
    return catalog


def _optimization_implementation_gaps() -> list[dict[str, Any]]:
    from atelier.core.capabilities.session_optimizer import SUPPORTED_OPTIMIZER_HOSTS

    supported_hosts = list(SUPPORTED_OPTIMIZER_HOSTS)
    return [
        {
            "id": "wrapper-adoption-visibility",
            "priority": "medium",
            "title": "Distinguish wrapper-launched sessions from instruction-only sessions",
            "hosts": supported_hosts,
            "notes": (
                "These hosts now have live start-time wrapper/task entrypoints, but the dashboard still does not label which recorded runs actually used those enforced paths."
            ),
        },
        {
            "id": "mid-session-nudges-non-hook-hosts",
            "priority": "medium",
            "title": "Extend mid-session quality and loop nudges beyond Claude and Codex",
            "hosts": supported_hosts,
            "notes": (
                "Claude and Codex now emit no-edit, quality-drop, and loop-rescue nudges through runtime telemetry. "
                "Copilot, Gemini, and OpenCode still only get start-time automation today."
            ),
        },
        {
            "id": "host-native-output-compaction-rollout",
            "priority": "medium",
            "title": "Roll out host-native tool-output compaction more broadly",
            "hosts": supported_hosts,
            "notes": (
                "Tool-output compaction exists in Atelier's supervised paths and optional Claude hook surfaces, "
                "but the host-native rollout is not uniform yet."
            ),
        },
        {
            "id": "impact-validation-windows",
            "priority": "medium",
            "title": "Add before/after impact validation for optimization changes",
            "hosts": supported_hosts,
            "notes": (
                "Atelier now audits static context surfaces and recent trace quality, but it still does not compare pre-change and post-change windows to prove whether an optimization improved tokens, cost, or cache leverage."
            ),
        },
        {
            "id": "delta-read-and-structure-map-telemetry",
            "priority": "medium",
            "title": "Measure smart re-read savings at the file-structure level",
            "hosts": supported_hosts,
            "notes": (
                "Atelier has search/read compaction and AST-aware truncation, but it does not yet expose repeat-read telemetry comparable to delta-read and structure-map measurement."
            ),
        },
        {
            "id": "model-routing-simulation",
            "priority": "low",
            "title": "Simulate cheaper model routing for routine traces",
            "hosts": supported_hosts,
            "notes": (
                "The current optimizer flags expensive sessions, but it still does not estimate what could have been saved by routing clearly routine work to a cheaper model tier."
            ),
        },
    ]


def _optimizations_summary_payload(root: Path, store: ReasoningStore, *, window_days: int) -> dict[str, Any]:
    from atelier.core.capabilities.session_optimizer import (
        build_trace_optimization_report,
        render_session_optimizer_guidance,
        session_optimization_rules,
    )

    savings = _savings_summary_payload(root, window_days=window_days)
    traces = store.list_traces(limit=5000)
    recent_traces = _recent_traces(store, window_days=window_days)
    live_events = _iter_live_savings_events(root)
    recommendations = build_trace_optimization_report(traces, days=window_days)
    from atelier.core.foundation.paths import resolve_workspace_root

    project_root_candidate = resolve_workspace_root(root)
    if not ((project_root_candidate / "src").exists() or (project_root_candidate / "AGENTS.md").exists()):
        project_root_candidate = Path.cwd()
    from atelier.core.capabilities.optimization_audit import (
        build_context_audit,
        build_session_quality_summary,
    )

    context_audit = build_context_audit(
        project_root=project_root_candidate,
        blocks_dir=getattr(store, "blocks_dir", None),
        rubrics_dir=getattr(store, "rubrics_dir", None),
    )
    quality_score = build_session_quality_summary(traces, window_days=window_days, context_audit=context_audit)
    runtime_coverage = _optimization_runtime_coverage()
    implemented_levers = _implemented_optimization_catalog(savings)
    auto_optimizations = _build_auto_optimizations(savings, live_events, window_days=window_days)
    impact_validation = _build_impact_validation(store, recent_traces, window_days=window_days)
    reread_telemetry = _build_reread_telemetry(root, window_days=window_days)
    model_routing_simulation = _build_model_routing_simulation(recent_traces, window_days=window_days)

    # Fetch latest codeburn:optimize report
    external_optimizations = store.list_external_analytics_runs(tool="codeburn:optimize", days=window_days, limit=1)
    latest_external = external_optimizations[0] if external_optimizations else None

    automatic_hosts = sum(1 for item in runtime_coverage if item["automatic_at_start"])
    advisory_only_hosts = sum(1 for item in runtime_coverage if item["advisory_only"])
    observed_levers = sum(
        1
        for item in implemented_levers
        if isinstance(item.get("observed_tokens_saved"), int) and item["observed_tokens_saved"] > 0
    )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": window_days,
        "automatic_hosts": automatic_hosts,
        "advisory_only_hosts": advisory_only_hosts,
        "observed_levers": observed_levers,
        "runtime_coverage": runtime_coverage,
        "budget_guidance": render_session_optimizer_guidance(),
        "budget_rules": session_optimization_rules(),
        "implemented_levers": implemented_levers,
        "implementation_gaps": _optimization_implementation_gaps(),
        "recommendations": recommendations,
        "context_audit": context_audit,
        "quality_score": quality_score,
        "auto_optimizations": auto_optimizations,
        "impact_validation": impact_validation,
        "reread_telemetry": reread_telemetry,
        "model_routing_simulation": model_routing_simulation,
        "external_optimizations": latest_external,
        "savings": savings,
        "data_sources": [
            {
                "id": "runtime_coverage",
                "label": "Host automation coverage",
                "detail": "Static audit of current hook-backed and instruction-backed optimizer surfaces across the five supported hosts.",
            },
            {
                "id": "trace_recommendations",
                "label": "Trace-based opportunities",
                "detail": "Recommendations generated from stored traces via the session optimizer heuristics.",
            },
            {
                "id": "context_audit",
                "label": "Static context audit",
                "detail": "Prompt-relevant Atelier surfaces scanned for approximate token weight and optimization headroom.",
            },
            {
                "id": "quality_score",
                "label": "Recent trace quality scoring",
                "detail": "Multi-signal quality scoring built from recent Atelier traces using context fill, delivery, outcome, cache, and redundancy signals.",
            },
            {
                "id": "savings_telemetry",
                "label": "Savings telemetry",
                "detail": "Observed token savings pulled from cost history and live savings event streams.",
            },
        ],
    }


# --------------------------------------------------------------------------- #
# App Factory                                                                 #
# --------------------------------------------------------------------------- #


def create_app(store_root: str | Path | None = None) -> Any:
    """Construct the FastAPI instance."""
    from fastapi import Depends, FastAPI, HTTPException, Query, Security
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    from atelier.core.foundation.models import Trace, coerce_trace_json, to_jsonable
    from atelier.core.foundation.store import ReasoningStore

    security = HTTPBearer(auto_error=False)

    def verify_api_key(
        auth: HTTPAuthorizationCredentials | None = Security(security),  # noqa: B008
    ) -> str:
        if not cfg.require_auth:
            return "anonymous"
        if auth is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not cfg.api_key:
            logger.warning("API key not configured; rejecting request")
            raise HTTPException(status_code=401, detail="Authentication required but no key configured")
        if auth.credentials != cfg.api_key:
            raise HTTPException(status_code=403, detail="Invalid API key")
        return "authenticated"

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
    _store_init_lock = threading.Lock()

    def get_store() -> ReasoningStore:
        if not store._initialized:
            with _store_init_lock:
                if not store._initialized:  # double-checked locking
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

        # Calculate tokens/cost from ALL traces in database using the shared
        # backend pricing path.
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

            conn.row_factory = sqlite3.Row
            for trace_row in conn.execute("SELECT payload FROM traces"):
                try:
                    payload = json.loads(trace_row["payload"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                total_cost_usd += _trace_cost_from_payload(payload)

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
        query: str | None = Query(None),
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        store = get_store()
        traces = store.list_traces(
            domain=domain,
            status=status,
            agent=agent,
            host=host,
            query=query,
            limit=limit,
            offset=offset,
        )
        # Fetch global metrics for the current domain/agent/host filters
        metrics = store.get_traces_metrics(domain=domain, agent=agent, host=host)

        return {"items": [to_jsonable(t) for t in traces], "metrics": metrics}

    @app.get("/v1/traces/{trace_id}", tags=["traces"], dependencies=[Depends(verify_api_key)])
    def get_trace(trace_id: str) -> dict[str, Any]:
        trace = get_store().get_trace(trace_id)
        if not trace:
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
        return to_jsonable(trace)

    # ------------------------------------------------------------------ #
    # MCP service-backed task tools                                      #
    # ------------------------------------------------------------------ #

    def _runtime() -> Any:
        from atelier.gateway.adapters.runtime import ReasoningRuntime

        return ReasoningRuntime(root=Path(cfg.atelier_root))

    @app.post("/v1/reasoning/context", tags=["reasoning"], dependencies=[Depends(verify_api_key)])
    def task_context(payload: dict[str, Any]) -> dict[str, Any]:
        task = str(payload.get("task") or "")
        if not task:
            raise HTTPException(status_code=400, detail="task is required")
        result = _runtime().get_reasoning_context(
            task=task,
            domain=payload.get("domain"),
            files=list(payload.get("files") or []),
            tools=list(payload.get("tools") or []),
            errors=list(payload.get("errors") or []),
            max_blocks=int(payload.get("max_blocks", 5)),
            token_budget=payload.get("token_budget", 2000),
            dedup=bool(payload.get("dedup", True)),
            include_telemetry=bool(payload.get("include_telemetry", False)),
            agent_id=payload.get("agent_id"),
            recall=bool(payload.get("recall", True)),
        )
        return result if isinstance(result, dict) else {"context": result}

    @app.post("/v1/reasoning/rescue", tags=["reasoning"], dependencies=[Depends(verify_api_key)])
    def rescue_failure(payload: dict[str, Any]) -> dict[str, Any]:
        task = str(payload.get("task") or "")
        error = str(payload.get("error") or "")
        if not task or not error:
            raise HTTPException(status_code=400, detail="task and error are required")
        result = _runtime().rescue_failure(
            task=task,
            error=error,
            files=list(payload.get("files") or []),
            recent_actions=list(payload.get("recent_actions") or []),
            domain=payload.get("domain"),
        )
        response = to_jsonable(result)
        with contextlib.suppress(Exception):
            analysis = _runtime().core_runtime.analyze_failure_for_error(
                error,
                domain=payload.get("domain"),
            )
            if analysis:
                response["analysis"] = analysis
        return response

    @app.post("/v1/rubrics/run", tags=["rubrics"], dependencies=[Depends(verify_api_key)])
    def run_rubric_gate(payload: dict[str, Any]) -> dict[str, Any]:
        from atelier.core.foundation.rubric_gate import run_rubric

        rubric_id = str(payload.get("rubric_id") or "")
        if not rubric_id:
            raise HTTPException(status_code=400, detail="rubric_id is required")
        rubric = get_store().get_rubric(rubric_id)
        if rubric is None:
            raise HTTPException(status_code=404, detail=f"Rubric not found: {rubric_id}")
        checks = payload.get("checks") or {}
        if not isinstance(checks, dict):
            raise HTTPException(status_code=400, detail="checks must be an object")
        return to_jsonable(run_rubric(rubric, checks))

    def _redact_trace_json(value: Any) -> Any:
        from atelier.core.foundation.redaction import redact

        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [_redact_trace_json(item) for item in value]
        if isinstance(value, dict):
            return {str(key): _redact_trace_json(item) for key, item in value.items()}
        return value

    def _coerce_trace_validation_passed(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"pass", "passed", "success", "successful", "ok", "true"}:
                return True
            if lowered in {"fail", "failed", "failure", "error", "errored", "false"}:
                return False
        return False

    def _normalize_trace_tool_calls(items: list[Any]) -> list[dict[str, Any]]:
        from atelier.core.foundation.redaction import redact

        normalized: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, str):
                normalized.append({"name": redact(item), "args_hash": "", "count": 1})
                continue
            if isinstance(item, dict):
                raw_count = item.get("count") or 1
                with contextlib.suppress(TypeError, ValueError):
                    raw_count = int(raw_count)
                if not isinstance(raw_count, int):
                    raw_count = 1
                tool_call = {
                    "name": redact(str(item.get("name") or item.get("tool") or "unknown")),
                    "args_hash": redact(str(item.get("args_hash") or "")),
                    "count": raw_count,
                }
                if "args" in item:
                    tool_call["args"] = _redact_trace_json(item["args"])
                if isinstance(item.get("result_summary"), str):
                    tool_call["result_summary"] = redact(item["result_summary"])
                normalized.append(tool_call)
                continue
            normalized.append({"name": redact(str(item)), "args_hash": "", "count": 1})
        return normalized

    def _normalize_trace_validation_results(items: list[Any]) -> list[dict[str, Any]]:
        from atelier.core.foundation.redaction import redact

        normalized: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                name = item.get("name") or item.get("check") or "validation"
                detail = item.get("detail") or item.get("output") or ""
                passed = item.get("passed")
                if passed is None:
                    passed = item.get("status")
                normalized.append(
                    {
                        "name": redact(str(name)),
                        "passed": _coerce_trace_validation_passed(passed),
                        "detail": redact(str(detail)),
                    }
                )
                continue
            text = redact(str(item))
            lowered = text.lower()
            normalized.append(
                {
                    "name": text,
                    "passed": not any(token in lowered for token in ("fail", "error", "not run")),
                    "detail": "",
                }
            )
        return normalized

    def _normalize_trace_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        from atelier.core.foundation.redaction import redact, redact_list

        normalized = dict(payload)
        event_recorded = bool(normalized.pop("event_type", None))
        normalized.pop("event_payload", None)
        normalized.pop("prompt", None)
        normalized.pop("response", None)
        normalized.pop("bash_outputs", None)
        normalized.pop("tool_outputs", None)

        normalized["task"] = redact(str(normalized.get("task") or ""))
        normalized["files_touched"] = redact_list([str(item) for item in normalized.get("files_touched") or []])
        normalized["commands_run"] = redact_list([str(item) for item in normalized.get("commands_run") or []])
        normalized["errors_seen"] = redact_list([str(item) for item in normalized.get("errors_seen") or []])
        normalized["diff_summary"] = redact(str(normalized.get("diff_summary") or ""))
        normalized["output_summary"] = redact(str(normalized.get("output_summary") or ""))
        normalized["tools_called"] = _normalize_trace_tool_calls(list(normalized.get("tools_called") or []))
        normalized["validation_results"] = _normalize_trace_validation_results(
            list(normalized.get("validation_results") or [])
        )
        return normalized, event_recorded

    @app.post("/v1/traces", tags=["traces"], dependencies=[Depends(verify_api_key)])
    def record_trace(payload: dict[str, Any]) -> dict[str, Any]:
        if "id" not in payload:
            payload["id"] = Trace.make_id(payload.get("task", "untitled"), payload.get("agent", "agent"))
        normalized_payload, event_recorded = _normalize_trace_payload(payload)
        trace = Trace.model_validate(normalized_payload)
        get_store().record_trace(trace)
        response: dict[str, Any] = {"id": trace.id, "event_recorded": event_recorded}
        if trace.session_id:
            response["session_id"] = trace.session_id
        return response

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
    def list_local_telemetry(
        limit: int = Query(200, ge=1, le=5000),
        since: float | None = Query(None),
        event: str | None = Query(None),
        host: str | None = Query(None),
    ) -> dict[str, Any]:
        from atelier.core.service.telemetry.local_store import LocalTelemetryStore

        store = LocalTelemetryStore()
        events = store.list_events(limit=limit, since=since, event=event, host=host)
        return {"events": events}

    @app.post("/telemetry/local", tags=["telemetry"], dependencies=[Depends(verify_api_key)])
    def post_local_telemetry(payload: dict[str, Any]) -> dict[str, bool]:
        from atelier.core.service.telemetry.local_store import LocalTelemetryStore

        store = LocalTelemetryStore()
        store.write_event(event=payload["event"], props=payload["props"], exported=False)
        return {"ok": True}

    @app.get("/telemetry/summary", tags=["telemetry"], dependencies=[Depends(verify_api_key)])
    def get_telemetry_summary(
        since: float | None = Query(None),
        event: str | None = Query(None),
        host: str | None = Query(None),
    ) -> dict[str, Any]:
        from atelier.core.service.telemetry.local_store import LocalTelemetryStore

        store = LocalTelemetryStore()
        return store.summary(since=since, event=event, host=host)

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
        db_path = store.db_path
        rows = _query_analytics_rows(db_path, grouped=grouped, days=days, limit=limit)
        return _filter_analytics_rows(rows, agent=agent, category=category)

    @app.get("/analytics/summary", tags=["analytics"], dependencies=[Depends(verify_api_key)])
    def analytics_summary(
        agent: str | None = Query(None),
        model: str | None = Query(None),
        category: str | None = Query(None),
        search: str | None = Query(None),
        limit: int = Query(5000, ge=1, le=10000),
        grouped: bool = Query(True),
        days: int | None = Query(None),
    ) -> dict[str, Any]:
        rows = _query_analytics_rows(store.db_path, grouped=grouped, days=days, limit=limit)
        filtered = _filter_analytics_rows(rows, agent=agent, model=model, category=category, search=search)
        return _build_analytics_summary(filtered, days=days)

    @app.get("/analytics/dashboard", tags=["analytics"], dependencies=[Depends(verify_api_key)])
    def analytics_dashboard(
        days: int = Query(30, ge=1, le=365),
        host: str | None = Query(None),
    ) -> dict[str, Any]:
        """Session-level aggregated analytics for the dashboard.

        Returns daily activity, per-domain, per-host, per-model breakdowns,
        top sessions, and tool-type distributions in one call.
        """
        db_path = store.db_path
        host_filter = "AND COALESCE(host, agent) = ?" if host else ""
        sql = f"""
            SELECT
                id,
            COALESCE(host, agent) AS host,
                domain,
                json_extract(payload, '$.model') AS model,
                CAST(json_extract(payload, '$.input_tokens') AS INTEGER) AS input_tokens,
                CAST(json_extract(payload, '$.output_tokens') AS INTEGER) AS output_tokens,
                CAST(json_extract(payload, '$.thinking_tokens') AS INTEGER) AS thinking_tokens,
                CAST(json_extract(payload, '$.cached_input_tokens') AS INTEGER) AS cached_tokens,
                CAST(json_extract(payload, '$.cache_creation_input_tokens') AS INTEGER) AS cache_write_tokens,
                CAST(json_extract(payload, '$.user_prompt_tokens') AS INTEGER) AS user_prompt_tokens,
                payload,
                created_at,
                date(created_at) AS day
            FROM traces
            WHERE created_at >= datetime('now', '-' || ? || ' days')
            {host_filter}
            ORDER BY created_at DESC
        """
        params: list[Any] = [days]
        if host:
            params.append(host)

        sessions = []
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                d = dict(row)
                payload_obj: dict[str, Any] = {}
                tools_called: list[dict[str, Any]] = []
                try:
                    parsed_payload = json.loads(d.get("payload") or "{}")
                    if isinstance(parsed_payload, dict):
                        payload_obj = parsed_payload
                        raw_tools = payload_obj.get("tools_called") or []
                        if isinstance(raw_tools, list):
                            tools_called = [tool for tool in raw_tools if isinstance(tool, dict)]
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.warning("Failed to parse trace payload: %s", exc)

                payload_for_usage = payload_obj or {
                    "model": d.get("model") or "",
                    "input_tokens": d.get("input_tokens") or 0,
                    "output_tokens": d.get("output_tokens") or 0,
                    "thinking_tokens": d.get("thinking_tokens") or 0,
                    "cached_input_tokens": d.get("cached_tokens") or 0,
                    "cache_creation_input_tokens": d.get("cache_write_tokens") or 0,
                }
                model_usages = _trace_model_usages(payload_for_usage)
                primary_model = _trace_primary_model(payload_for_usage, model_usages)

                in_t = d.get("input_tokens") or 0
                out_t = d.get("output_tokens") or 0
                think_t = d.get("thinking_tokens") or 0
                cache_r = d.get("cached_tokens") or 0
                cache_w = d.get("cache_write_tokens") or 0

                cost = _trace_cost_from_payload(payload_for_usage)

                sessions.append(
                    {
                        "id": d["id"],
                        "host": d["host"] or "unknown",
                        "domain": d["domain"] or "unknown",
                        "model": primary_model,
                        "model_usages": model_usages,
                        "day": d.get("day") or "",
                        "created_at": d.get("created_at") or "",
                        "input_tokens": in_t,
                        "output_tokens": out_t,
                        "thinking_tokens": think_t,
                        "cached_tokens": cache_r,
                        "cache_write_tokens": cache_w,
                        "user_prompt_tokens": d.get("user_prompt_tokens") or 0,
                        "cost": cost,
                        "tools_called": tools_called,
                    }
                )

        def _tool_call_count(session: dict[str, Any]) -> int:
            total = 0
            for tool in session.get("tools_called") or []:
                if not isinstance(tool, dict):
                    continue
                total += int(tool.get("count") or 1)
            return total

        def _tool_output_tokens(session: dict[str, Any]) -> int:
            total = 0
            for tool in session.get("tools_called") or []:
                if not isinstance(tool, dict):
                    continue
                total += int(tool.get("output_tokens") or 0)
            return total

        def _has_usage_signal(session: dict[str, Any]) -> bool:
            model = str(session.get("model") or "").strip()
            return any(
                (
                    bool(model),
                    (session.get("output_tokens") or 0) > 0,
                    (session.get("thinking_tokens") or 0) > 0,
                    float(session.get("cost") or 0.0) > 0,
                    _tool_call_count(session) > 0,
                    _tool_output_tokens(session) > 0,
                )
            )

        # Imported host logs can contain prompt-only stubs with no model or assistant/tool activity.
        # Excluding them keeps dashboard breakdowns aligned with real usage sessions.
        dashboard_sessions = [session for session in sessions if _has_usage_signal(session)]

        daily: dict[str, dict[str, Any]] = {}
        hourly: dict[str, dict[str, Any]] = {}
        for s in dashboard_sessions:
            day = s["day"]
            if day not in daily:
                daily[day] = {
                    "date": day,
                    "sessions": 0,
                    "cost": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
            daily[day]["sessions"] += 1
            daily[day]["cost"] += s["cost"]
            daily[day]["input_tokens"] += s["input_tokens"]
            daily[day]["output_tokens"] += s["output_tokens"]

            created_at = str(s.get("created_at") or "")
            if len(created_at) >= 13:
                hour = created_at[:13].replace("T", " ") + ":00"
                if hour not in hourly:
                    hourly[hour] = {
                        "date": hour,
                        "sessions": 0,
                        "cost": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    }
                hourly[hour]["sessions"] += 1
                hourly[hour]["cost"] += s["cost"]
                hourly[hour]["input_tokens"] += s["input_tokens"]
                hourly[hour]["output_tokens"] += s["output_tokens"]
        daily_list = sorted(daily.values(), key=lambda x: x["date"])
        hourly_list = sorted(hourly.values(), key=lambda x: x["date"])

        by_domain: dict[str, dict[str, Any]] = {}
        for s in dashboard_sessions:
            dom = s["domain"]
            if dom not in by_domain:
                by_domain[dom] = {"domain": dom, "sessions": 0, "cost": 0.0}
            by_domain[dom]["sessions"] += 1
            by_domain[dom]["cost"] += s["cost"]
        by_domain_list = sorted(by_domain.values(), key=lambda x: x["cost"], reverse=True)[:30]
        for r in by_domain_list:
            r["avg_cost"] = r["cost"] / r["sessions"] if r["sessions"] else 0.0

        by_host: dict[str, dict[str, Any]] = {}
        for s in dashboard_sessions:
            h = s["host"]
            if h not in by_host:
                by_host[h] = {
                    "host": h,
                    "sessions": 0,
                    "cost": 0.0,
                    "input_tokens": 0,
                    "cached_tokens": 0,
                }
            by_host[h]["sessions"] += 1
            by_host[h]["cost"] += s["cost"]
            by_host[h]["input_tokens"] += s["input_tokens"]
            by_host[h]["cached_tokens"] += s["cached_tokens"]
        by_host_list = sorted(by_host.values(), key=lambda x: x["cost"], reverse=True)
        for r in by_host_list:
            total_in = r["input_tokens"] + r["cached_tokens"]
            r["cache_pct"] = (r["cached_tokens"] / total_in * 100) if total_in else 0.0

        by_model: dict[str, dict[str, Any]] = {}
        for s in dashboard_sessions:
            seen_models: set[str] = set()
            for usage in s.get("model_usages") or []:
                mdl = str(usage.get("model") or "unknown") or "unknown"
                if mdl not in by_model:
                    by_model[mdl] = {
                        "model": mdl,
                        "sessions": 0,
                        "cost": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cached_tokens": 0,
                    }
                if mdl not in seen_models:
                    by_model[mdl]["sessions"] += 1
                    seen_models.add(mdl)
                by_model[mdl]["cost"] += _model_usage_cost(usage)
                by_model[mdl]["input_tokens"] += int(usage.get("input_tokens") or 0)
                by_model[mdl]["output_tokens"] += int(usage.get("output_tokens") or 0)
                by_model[mdl]["cached_tokens"] += int(usage.get("cached_input_tokens") or 0)
        by_model_list = sorted(by_model.values(), key=lambda x: x["cost"], reverse=True)
        for r in by_model_list:
            total_in = r["input_tokens"] + r["cached_tokens"]
            r["cache_pct"] = (r["cached_tokens"] / total_in * 100) if total_in else 0.0

        host_model_overview: dict[tuple[str, str], dict[str, Any]] = {}
        for s in dashboard_sessions:
            host_name = s["host"] or "unknown"
            primary_model = s["model"] or "unknown"
            seen_pairs: set[tuple[str, str]] = set()
            for usage in s.get("model_usages") or []:
                model_name = str(usage.get("model") or primary_model) or "unknown"
                key = (host_name, model_name)
                if key not in host_model_overview:
                    host_model_overview[key] = {
                        "host": host_name,
                        "model": model_name,
                        "sessions": 0,
                        "user_typed_tokens": 0,
                        "base_context_tokens": 0,
                        "cached_prompt_tokens": 0,
                        "cache_write_tokens": 0,
                        "billable_output_tokens": 0,
                        "tool_output_tokens": 0,
                        "thinking_tokens": 0,
                        "tool_calls": 0,
                        "cost": 0.0,
                    }
                row = host_model_overview[key]
                if key not in seen_pairs:
                    row["sessions"] += 1
                    seen_pairs.add(key)
                row["base_context_tokens"] += int(usage.get("input_tokens") or 0)
                row["cached_prompt_tokens"] += int(usage.get("cached_input_tokens") or 0)
                row["cache_write_tokens"] += int(usage.get("cache_creation_input_tokens") or 0)
                row["billable_output_tokens"] += int(usage.get("output_tokens") or 0)
                row["thinking_tokens"] += int(usage.get("thinking_tokens") or 0)
                row["cost"] += _model_usage_cost(usage)
                if model_name == primary_model:
                    row["user_typed_tokens"] += s["user_prompt_tokens"]
                    row["tool_output_tokens"] += _tool_output_tokens(s)
                    row["tool_calls"] += _tool_call_count(s)
        host_model_overview_list = sorted(
            host_model_overview.values(),
            key=lambda row: (row["cost"], row["sessions"]),
            reverse=True,
        )

        top_sessions_clean = [
            {
                "id": s["id"],
                "host": s["host"],
                "domain": s["domain"],
                "model": s["model"],
                "date": s["created_at"][:10] if s["created_at"] else "",
                "cost": s["cost"],
                "input_tokens": s["input_tokens"],
                "output_tokens": s["output_tokens"],
                "cached_tokens": s["cached_tokens"],
            }
            for s in sorted(dashboard_sessions, key=lambda x: x["cost"], reverse=True)[:20]
        ]

        _CORE = {
            "Read",
            "Edit",
            "Write",
            "Grep",
            "Glob",
            "ListDir",
            "NotebookRead",
            "NotebookEdit",
            "TodoRead",
            "TodoWrite",
            "WebSearch",
            "WebFetch",
            "MultiEdit",
            "view",
            "read_file",
            "replace",
            "apply_patch",
        }
        _SHELL = {"Bash", "bash", "run_shell_command", "exec_command", "shell", "run_code"}

        core_tools: dict[str, dict[str, Any]] = {}
        shell_tools: dict[str, dict[str, Any]] = {}
        mcp_tools: dict[str, dict[str, Any]] = {}

        for s in dashboard_sessions:
            for tool in s["tools_called"]:
                if not isinstance(tool, dict):
                    continue
                name = tool.get("name") or ""
                calls = tool.get("count") or 1
                tin = tool.get("input_tokens") or 0
                tout = tool.get("output_tokens") or 0

                if name in _CORE:
                    bucket = core_tools
                elif name in _SHELL:
                    bucket = shell_tools
                elif name.startswith("mcp__") or name.startswith("mcp:"):
                    bucket = mcp_tools
                else:
                    continue

                if name not in bucket:
                    bucket[name] = {"name": name, "calls": 0, "input_tokens": 0, "output_tokens": 0}
                bucket[name]["calls"] += calls
                bucket[name]["input_tokens"] += tin
                bucket[name]["output_tokens"] += tout

        return {
            "summary": {
                "total_cost": round(sum(float(session["cost"]) for session in dashboard_sessions), 6),
                "projected_monthly_cost": round(
                    sum(float(session["cost"]) for session in dashboard_sessions) * (30 / max(days, 1)),
                    6,
                ),
                "total_sessions": len(dashboard_sessions),
            },
            "daily": daily_list,
            "hourly": hourly_list,
            "by_domain": by_domain_list,
            "by_host": by_host_list,
            "by_model": by_model_list,
            "host_model_overview": host_model_overview_list,
            "top_sessions": top_sessions_clean,
            "external": _build_external_analytics_summary(get_store(), days=days),
            "tools": {
                "core": sorted(core_tools.values(), key=lambda x: x["calls"], reverse=True)[:15],
                "shell": sorted(shell_tools.values(), key=lambda x: x["calls"], reverse=True)[:10],
                "mcp": sorted(mcp_tools.values(), key=lambda x: x["calls"], reverse=True)[:20],
            },
        }

    @app.get("/analytics/external", tags=["analytics"], dependencies=[Depends(verify_api_key)])
    def analytics_external(
        days: int = Query(30, ge=1, le=365),
        tool: str | None = Query(None),
        limit: int = Query(30, ge=1, le=200),
    ) -> dict[str, Any]:
        return _build_external_analytics_detail(get_store(), days=days, tool=tool, limit=limit)

    @app.get("/plans", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_plans(limit: int = 50) -> list[dict[str, Any]]:
        """Compatibility: GET /plans -> derives plan records from traces."""
        traces = get_store().list_traces(limit=limit)
        result = []
        for t in traces:
            if t.session_id:
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

    @app.get("/ledgers/{session_id}", tags=["compat"], dependencies=[Depends(verify_api_key)])
    def compat_ledger(session_id: str) -> dict[str, Any]:
        """Compatibility: GET /ledgers/{session_id} -> returns run ledger data.

        First checks for a live RunLedger JSON file (written by the reasoning
        runtime).  When that is absent, falls back to an imported Trace that
        carries the same session_id so that sessions imported from Claude / Codex /
        OpenCode / Copilot are still surfaced here.
        """
        from atelier.infra.runtime.run_ledger import RunLedger

        ledger_path = Path(cfg.atelier_root) / "runs" / f"{session_id}.json"
        snap = None
        if ledger_path.exists():
            try:
                ledger = RunLedger.load(ledger_path)
                snap = ledger.snapshot()
            except Exception as e:
                return {"session_id": session_id, "error": str(e)}

        # Always check for a trace to fetch the full conversation history.
        # Imported sessions from Claude/Codex/OpenCode/Copilot use the Trace as source of truth.
        store_inst = get_store()

        # 1. Try direct ID match (high performance)
        trace = store_inst.get_trace(session_id)

        # 2. Try host-prefixed IDs (standard for imported sessions)
        if trace is None:
            from atelier.gateway.hosts.session_parsers.registry import (
                SUPPORTED_SESSION_IMPORT_HOSTS,
            )

            for host in SUPPORTED_SESSION_IMPORT_HOSTS:
                trace = store_inst.get_trace(f"{host}-{session_id}")
                if trace:
                    break

        # 3. Slower fallback: search for the session_id inside payloads
        if trace is None:
            with sqlite3.connect(store_inst.db_path) as conn:
                # Use json_extract for efficient searching
                row = conn.execute(
                    "SELECT payload FROM traces WHERE json_extract(payload, '$.session_id') = ?",
                    (session_id,),
                ).fetchone()
                if row:
                    trace = Trace.model_validate_json(coerce_trace_json(row[0]))

        conversations: list[dict[str, Any]] = []
        source_paths: list[str] = []
        if trace:
            if trace.raw_artifact_ids:
                # Reconstruct from raw artifacts (imported sessions)
                for art_id in trace.raw_artifact_ids:
                    artifact = store_inst.get_raw_artifact(art_id)
                    if artifact:
                        if artifact.source_path:
                            source_paths.append(artifact.source_path)
                        try:
                            raw_content = store_inst.read_raw_artifact_content(artifact)
                            from atelier.gateway.hosts.session_parsers._session_parser import (
                                parse_session_turns,
                            )

                            conversations = parse_session_turns(raw_content, artifact.source)
                        except Exception as exc:
                            logger.error(
                                "Failed to reconstruct conversations from artifact %s (source=%s): %s",
                                art_id,
                                getattr(artifact, "source", "?"),
                                exc,
                                exc_info=True,
                            )
                        if conversations:
                            break

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
            if source_paths:
                snap["source_paths"] = source_paths
            return snap

        if trace:
            return {
                "session_id": trace.session_id or session_id,
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
                "source_paths": source_paths,
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

        return {"session_id": session_id, "status": "not_found"}

    # ------------------------------------------------------------------ #
    # MCP & Hosts                                                         #
    # ------------------------------------------------------------------ #

    @app.get("/mcp/status", tags=["ops"], dependencies=[Depends(verify_api_key)])
    def mcp_status() -> list[dict[str, Any]]:
        try:
            from atelier.gateway.adapters.mcp_server import (
                TOOLS,
                _tool_description,
                _tool_mode,
                _tool_visible_to_llm,
            )

            return [
                {
                    "tool_name": name,
                    "available": True,
                    "description": _tool_description(spec),
                    "is_dev": bool(spec.get("is_dev")),
                    "mode": _tool_mode(spec),
                }
                for name, spec in TOOLS.items()
                if _tool_visible_to_llm(name, spec)
            ]
        except Exception as exc:
            logger.warning("Failed to load MCP tool status: %s", exc, exc_info=True)
            return []

    @app.get("/hosts", tags=["ops"], dependencies=[Depends(verify_api_key)])
    def list_hosts() -> list[dict[str, Any]]:
        store = get_store()
        seen_hosts = set(store.get_traces_metrics()["hosts"])

        hosts = [
            ("claude", "Claude Code"),
            ("codex", "Codex"),
            ("opencode", "OpenCode"),
            ("copilot", "VS Code Copilot"),
            ("gemini", "Gemini CLI"),
        ]
        return [
            {
                "host_id": hid,
                "label": label,
                "status": "active" if hid in seen_hosts else "not_detected",
                "active_domains": [],
                "mcp_tools": [],
                "last_seen": None,
                "atelier_version": None,
                "description": None,
                "install_command": None,
            }
            for hid, label in hosts
        ]

    # ------------------------------------------------------------------ #
    # Skills                                                              #
    # ------------------------------------------------------------------ #

    @app.get("/skills", tags=["ops"], dependencies=[Depends(verify_api_key)])
    def list_skills() -> list[dict[str, Any]]:
        from atelier.core.environment import skill_visible

        root = Path(__file__).parent.parent.parent.parent.parent
        skills: list[dict[str, Any]] = []
        skills_dir = root / "integrations" / "skills"
        if skills_dir.exists():
            for skill_dir in sorted(skills_dir.iterdir()):
                if skill_dir.is_dir():
                    if not skill_visible(skill_dir.name):
                        continue
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
        from atelier.core.environment import skill_visible

        if not skill_visible(name):
            raise HTTPException(status_code=404, detail=f"Skill not available outside dev mode: {name}")
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

    @app.get("/v1/optimizations/summary", tags=["metrics"], dependencies=[Depends(verify_api_key)])
    def optimizations_summary(window_days: int = Query(14)) -> dict[str, Any]:
        return _optimizations_summary_payload(Path(cfg.atelier_root), get_store(), window_days=window_days)

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
                        "session_id": call.get("session_id", ""),
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
        except Exception as exc:
            logger.warning("Failure analyzer raised an exception: %s", exc, exc_info=True)
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
        "atelier.core.service.api:create_app",
        factory=True,
        host=_host,
        port=_port,
        reload=reload,
    )
