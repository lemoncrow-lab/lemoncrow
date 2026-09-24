"""Host-neutral model recommendation runtime for LemonCrow tool execution."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lemoncrow.core.capabilities.pricing import get_model_pricing
from lemoncrow.gateway.tools.execution import route_enforcement_enabled
from lemoncrow.gateway.tools.routing import (
    normalize_model_id,
    provider_for_model,
    restore_legacy_route,
    route_outcome_calibration,
    task_text_from_args,
)
from lemoncrow.infra.runtime.run_ledger import RunLedger
from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import RouteConfigError
from lemoncrow.pro.capabilities.cross_vendor_routing.router import NoFeasibleRouteError
from lemoncrow.pro.capabilities.model_routing import ModelRouter


@dataclass(frozen=True, slots=True)
class ModelRoutingHooks:
    read_workspace_state: Callable[[], dict[str, Any]]
    write_workspace_state: Callable[[dict[str, Any]], None]
    state_lock: Any
    select_owned_execution_route: Callable[..., Any]
    detect_agent: Callable[[], str]
    append_live_savings_event: Callable[[dict[str, Any]], Any]
    get_host_session_sidecar_path: Callable[[], Any]
    make_outcome_writer: Callable[[RunLedger], Any]
    latest_cache_affinity_model: Callable[[RunLedger], str | None]
    ledger_turn_count: Callable[[RunLedger], int]


def workflow_state_from_workspace(hooks: ModelRoutingHooks) -> dict[str, Any]:
    workflow = hooks.read_workspace_state().get("workflow")
    return workflow if isinstance(workflow, dict) else {}


def persist_legacy_route(
    hooks: ModelRoutingHooks,
    workflow: dict[str, Any],
    payload: dict[str, Any],
    current_step: str,
) -> None:
    if not current_step:
        return
    tier = str(payload.get("tier") or "").strip()
    model = str(payload.get("model") or "").strip()
    if tier not in {"cheap", "medium", "expensive"} or not model:
        return
    sticky_window = max(0, int(workflow.get("sticky_window") or 0))
    remaining = max(0, int(payload.get("sticky_until_tool_calls") or 0))
    if str(payload.get("decision") or "baseline") != "sticky":
        remaining = sticky_window
    workflow["routing"] = {
        "step": current_step,
        "remaining_tool_calls": remaining,
        "recommendation": {
            "tier": tier,
            "route_tier": payload.get("route_tier"),
            "model": model,
            "reasons": list(payload.get("reasons") or []),
            "score": int(payload.get("score") or 0),
            "cache_affinity_model": payload.get("cache_affinity_model"),
            "cache_cost_usd": float(payload.get("cache_cost_usd") or 0.0),
            "quality_gain_usd_estimated": float(payload.get("quality_gain_usd_estimated") or 0.0),
            "decision": str(payload.get("decision") or "baseline"),
            "baseline_tier": payload.get("baseline_tier"),
            "sticky_until_tool_calls": remaining,
        },
    }
    routing_entry = workflow["routing"]
    # Merge only routing onto a lock-fresh workflow so concurrent sibling state
    # updates (current_task/current_step/task_outputs) are never overwritten.
    with hooks.state_lock:
        state = hooks.read_workspace_state()
        fresh_workflow = state.get("workflow")
        fresh_workflow = fresh_workflow if isinstance(fresh_workflow, dict) else {}
        fresh_workflow["routing"] = routing_entry
        state["workflow"] = fresh_workflow
        hooks.write_workspace_state(state)


def model_recommendation_state(
    hooks: ModelRoutingHooks,
    led: RunLedger,
    args: dict[str, Any],
) -> dict[str, Any]:
    tool_call_events = [e for e in led.events if e.kind == "tool_call"]
    recent_tool_calls = [e.payload.get("tool", "") for e in tool_call_events[-10:]]
    turn_number = len(tool_call_events)
    workflow = workflow_state_from_workspace(hooks)
    session_state: dict[str, Any] = {
        "prior_errors": len(led.errors_seen) + len(led.repeated_failures),
        "cache_affinity_model": hooks.latest_cache_affinity_model(led),
        "turn_number": turn_number,
        "recent_tool_calls": recent_tool_calls,
        "session_cost_usd": round(
            sum(
                float((event.payload or {}).get("cost_usd") or 0.0)
                for event in led.events
                if event.kind == "tool_call" and (event.payload or {}).get("kind") == "llm_call"
            ),
            6,
        ),
    }
    workflow_step = str(workflow.get("current_step") or workflow.get("workflow_step") or "").strip()
    if workflow_step:
        session_state["workflow_step"] = workflow_step
    session_phase = str(workflow.get("session_phase") or "").strip()
    if session_phase:
        session_state["session_phase"] = session_phase
    if "max_output_tokens" in args:
        session_state["max_output_tokens"] = args["max_output_tokens"]
    if "budget_tokens" in args:
        session_state["max_output_tokens"] = args["budget_tokens"]
    expected_input_tokens = max(1_000, int(led.token_count or 0) // max(1, hooks.ledger_turn_count(led)))
    session_state["expected_input_tokens"] = expected_input_tokens
    session_state.setdefault("expected_output_tokens", max(1, int(expected_input_tokens * 0.2)))
    return session_state


def prepare_model_recommendation(
    hooks: ModelRoutingHooks,
    tool_name: str,
    args: dict[str, Any],
    led: RunLedger,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    session_state = model_recommendation_state(hooks, led, args)
    session_state.update(route_outcome_calibration(tool_name, session_state, led))
    workflow = workflow_state_from_workspace(hooks)
    current_step = str(session_state.get("workflow_step") or "")
    prior_route, stickiness_remaining = restore_legacy_route(workflow, current_step)
    estimated_input_tokens = max(1_000, int(session_state.get("expected_input_tokens") or 0))
    try:
        decision = hooks.select_owned_execution_route(
            tool_name=tool_name,
            task_text=task_text_from_args(args),
            mode="auto",
            provider="",
            model="",
            runner="",
            session_state=session_state,
        )
        led.record("route_decision", f"{decision.mode} route for {tool_name}", decision.to_dict())
        actual_model = str(getattr(led, "model", "") or os.environ.get("LEMONCROW_MODEL") or "").strip()
        actual_vendor = provider_for_model(actual_model)
        recommendation = {
            **decision.to_dict(),
            "vendor": decision.provider,
            "actual_model": actual_model,
            "actual_vendor": actual_vendor,
            "recommendation_followed": normalize_model_id(actual_model) == normalize_model_id(decision.model),
        }
        vs_model = actual_model or "auto"
        cost_saved_usd = 0.0
        if recommendation["model"] != vs_model and vs_model != "auto":
            expensive_pricing = get_model_pricing(vs_model)
            recommended_pricing = get_model_pricing(recommendation["model"])
            cost_saved_usd = max(
                0.0,
                expensive_pricing.cost_usd(input_tokens=estimated_input_tokens)
                - recommended_pricing.cost_usd(input_tokens=estimated_input_tokens),
            )
        payload = {
            "at": datetime.now(UTC).isoformat(),
            "kind": "model_recommendation",
            "lever": "model_routing",
            "session_id": led.session_id,
            "agent": led.agent or hooks.detect_agent(),
            "tool_name": tool_name,
            "tokens_saved": 0,
            "cost_saved_usd": round(cost_saved_usd, 6),
            "vs_model": vs_model,
            "estimated_input_tokens": estimated_input_tokens,
            "configured": True,
            **recommendation,
        }
    except (RouteConfigError, NoFeasibleRouteError) as exc:

        def _record_route_decision(route_payload: dict[str, Any]) -> None:
            led.record(
                "route_decision",
                f"{route_payload.get('decision', 'baseline')} route for {tool_name}",
                route_payload,
            )

        legacy = ModelRouter().recommend(
            tool_name,
            task_text_from_args(args),
            session_state,
            prior_route=prior_route,
            stickiness_remaining=stickiness_remaining,
            route_decision_sink=_record_route_decision,
        )
        if legacy is None:
            raise NoFeasibleRouteError("bench-off") from None
        payload = {
            "at": datetime.now(UTC).isoformat(),
            "kind": "model_recommendation",
            "lever": "model_routing",
            "session_id": led.session_id,
            "agent": led.agent or hooks.detect_agent(),
            "tool_name": tool_name,
            "tokens_saved": 0,
            "configured": False,
            "cost_saved_usd": 0.0,
            "estimated_input_tokens": estimated_input_tokens,
            "vs_model": "auto",
            "error": str(exc),
            **legacy.to_dict(),
        }
    return payload, session_state, workflow, current_step


def finalize_model_recommendation(
    hooks: ModelRoutingHooks,
    payload: dict[str, Any],
    *,
    led: RunLedger,
    tool_name: str,
    session_state: Mapping[str, Any],
    workflow: dict[str, Any],
    current_step: str,
    wrapper_applied: bool = False,
    wrapper_model: str | None = None,
) -> dict[str, Any]:
    finalized = dict(payload)
    finalized["route_enforcement_active"] = route_enforcement_enabled() and finalized.get("configured") is not False
    finalized["wrapper_applied"] = wrapper_applied
    if wrapper_model:
        finalized["wrapper_model"] = wrapper_model
        finalized["executed_model_scope"] = "local_mcp_only"
    if wrapper_applied:
        finalized["recommendation_followed"] = True
    led.record(
        "model_recommendation",
        f"recommend {finalized.get('model', 'unconfigured')} for {tool_name}",
        finalized,
    )
    if finalized.get("recommendation_followed") or float(finalized.get("cost_saved_usd") or 0.0) > 0:
        hooks.append_live_savings_event(finalized)
    else:
        hooks.append_live_savings_event(
            {
                key: finalized[key]
                for key in (
                    "at",
                    "kind",
                    "lever",
                    "session_id",
                    "agent",
                    "tool_name",
                    "tokens_saved",
                    "cost_saved_usd",
                    "configured",
                    "model",
                    "vs_model",
                    "tier",
                    "recommendation_followed",
                )
                if key in finalized
            }
        )

    routing_usd = float(finalized.get("cost_saved_usd") or 0.0)
    if routing_usd > 0:
        with contextlib.suppress(Exception):
            sidecar = hooks.get_host_session_sidecar_path()
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            routing_row = {
                "kind": "routing",
                "usd": round(routing_usd, 6),
                "tool": tool_name,
                "model": str(finalized.get("model") or ""),
                "ts": str(finalized.get("at") or ""),
            }
            with sidecar.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(routing_row) + "\n")
            with contextlib.suppress(ImportError):
                from lemoncrow.core.capabilities.savings_summary import (
                    _bump_historical_savings_cache as bump_routing_cache,
                )

                bump_routing_cache(routing_row)

    persist_legacy_route(hooks, workflow, finalized, current_step)

    if finalized.get("configured") is not False:
        from lemoncrow.pro.runtime import outcome_capture

        outcome_capture.schedule_route(
            session_id=led.session_id,
            tool=tool_name,
            recommended_vendor=str(finalized.get("vendor") or ""),
            recommended_tier=str(finalized.get("tier") or ""),
            recommended_model=str(finalized.get("model") or ""),
            actual_vendor=str(finalized.get("actual_vendor") or ""),
            actual_model=str(finalized.get("actual_model") or ""),
            recommendation_followed=bool(finalized.get("recommendation_followed")),
            applied_lessons=[str(item) for item in finalized.get("applied_lessons") or []],
            cost_cap_triggered=bool(finalized.get("cost_cap_triggered")),
            cost_cap_limit_usd_per_session=(
                float(finalized["cost_cap_limit_usd_per_session"])
                if finalized.get("cost_cap_limit_usd_per_session") is not None
                else None
            ),
            scored_state={
                "turn_number": int(session_state.get("turn_number") or 0),
                "prior_errors": len(led.errors_seen) + len(led.repeated_failures),
                "session_phase": str(session_state.get("session_phase") or "explore"),
                "workflow_step": str(session_state.get("workflow_step") or ""),
            },
            writer=hooks.make_outcome_writer(led),
        )

    return finalized


__all__ = [
    "ModelRoutingHooks",
    "finalize_model_recommendation",
    "model_recommendation_state",
    "persist_legacy_route",
    "prepare_model_recommendation",
    "workflow_state_from_workspace",
]
