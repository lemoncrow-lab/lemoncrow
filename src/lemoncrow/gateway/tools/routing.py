"""Transport-independent model-routing primitives for tool execution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from lemoncrow.infra.runtime.run_ledger import RunLedger, outcomes_path

TASK_TEXT_KEYS = ("task", "user_goal", "query", "prompt", "content", "description", "error")


def normalize_model_id(model_id: str) -> str:
    return model_id.strip().lower().replace(".", "-")


def provider_for_model(model_id: str) -> str:
    normalized = normalize_model_id(model_id)
    if not normalized:
        return ""
    if normalized.startswith("claude"):
        return "anthropic"
    if normalized.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    if normalized.startswith("gemini"):
        return "google"
    return "unknown"


def task_text_from_args(args: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in TASK_TEXT_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def route_outcome_calibration(tool_name: str, session_state: Mapping[str, Any], led: RunLedger) -> dict[str, Any]:
    from lemoncrow.pro.runtime.outcome_capture import load_outcomes_from_state

    root = led._root
    if root is None:
        return {}
    outcomes = load_outcomes_from_state(outcomes_path(root, led.agent or "claude", led.session_id))
    session_phase = str(session_state.get("session_phase") or "").strip()
    followed: list[float] = []
    unfollowed: list[float] = []
    samples = 0
    for entry in outcomes.get("route_outcomes", []):
        if str(entry.get("tool") or "") != tool_name:
            continue
        scored_state = entry.get("scored_state")
        if not isinstance(scored_state, dict):
            continue
        if session_phase and str(scored_state.get("session_phase") or "") != session_phase:
            continue
        outcome_window = entry.get("outcome_window")
        if not isinstance(outcome_window, dict):
            continue
        raw_score = outcome_window.get("outcome_score")
        if isinstance(raw_score, bool):
            continue
        if isinstance(raw_score, int | float):
            score = float(raw_score)
        elif isinstance(raw_score, str):
            try:
                score = float(raw_score.strip())
            except ValueError:
                continue
        else:
            continue
        samples += 1
        if bool(entry.get("recommendation_followed")):
            followed.append(score)
        else:
            unfollowed.append(score)
    if not followed or not unfollowed:
        return {}
    delta = round(sum(followed) / len(followed) - sum(unfollowed) / len(unfollowed), 4)
    if delta <= 0.0:
        return {"route_outcome_samples": samples}
    return {
        "route_outcome_score_delta": delta,
        "route_outcome_samples": samples,
    }


def restore_legacy_route(workflow: dict[str, Any], current_step: str) -> tuple[Any | None, int]:
    from lemoncrow.pro.capabilities.model_routing import ModelRecommendation

    routing = workflow.get("routing")
    if not isinstance(routing, dict):
        return None, 0
    if str(routing.get("step") or "") != current_step:
        return None, 0
    raw = routing.get("recommendation")
    if not isinstance(raw, dict):
        return None, 0
    tier = str(raw.get("tier") or "").strip()
    if tier not in {"cheap", "medium", "expensive"}:
        return None, 0
    typed_tier = cast(Literal["cheap", "medium", "expensive"], tier)
    route_tier = str(raw.get("route_tier") or "")
    if route_tier not in {
        "deterministic",
        "local_slm",
        "cheap_llm",
        "frontier_llm",
        "human_review",
    }:
        route_tier = "frontier_llm" if tier == "expensive" else "cheap_llm"
    typed_route_tier = cast(
        Literal["deterministic", "local_slm", "cheap_llm", "frontier_llm", "human_review"],
        route_tier,
    )
    baseline_tier_raw = str(raw.get("baseline_tier") or "").strip()
    baseline_tier = (
        cast(Literal["cheap", "medium", "expensive"], baseline_tier_raw)
        if baseline_tier_raw in {"cheap", "medium", "expensive"}
        else None
    )
    return (
        ModelRecommendation(
            tier=typed_tier,
            route_tier=typed_route_tier,
            model=str(raw.get("model") or ""),
            reasons=[str(reason) for reason in raw.get("reasons") or []],
            score=int(raw.get("score") or 0),
            cache_affinity_model=str(raw.get("cache_affinity_model") or "") or None,
            cache_cost_usd=float(raw.get("cache_cost_usd") or 0.0),
            quality_gain_usd_estimated=float(raw.get("quality_gain_usd_estimated") or 0.0),
            decision=str(raw.get("decision") or "baseline"),
            baseline_tier=baseline_tier,
            sticky_until_tool_calls=int(raw.get("sticky_until_tool_calls") or 0),
        ),
        max(0, int(routing.get("remaining_tool_calls") or 0)),
    )


__all__ = [
    "TASK_TEXT_KEYS",
    "normalize_model_id",
    "provider_for_model",
    "restore_legacy_route",
    "route_outcome_calibration",
    "task_text_from_args",
]
