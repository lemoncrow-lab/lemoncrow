"""Cross-vendor recommendation engine built on the existing ModelRouter."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from atelier.core.capabilities.counterfactual.capabilities import (
    infer_turn_requirements,
    supports_turn,
)
from atelier.core.capabilities.counterfactual.pricing import (
    CandidateModel,
    PricingTable,
    load_pricing_table,
)
from atelier.core.capabilities.lesson_promotion.bindings import (
    apply_cost_cap,
    apply_route_preferences,
)
from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore
from atelier.core.capabilities.model_routing.router import ModelRouter

from .configuration import RouteConfig, detect_configured_vendors
from .policy import RoutePolicyError, allowed_vendors


class NoFeasibleRouteError(ValueError):
    """Raised when no configured vendor can satisfy the requested turn safely."""


@dataclass(frozen=True)
class RankedCandidate:
    vendor: str
    model: str
    tier: str
    estimated_cost_usd: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CrossVendorRecommendation:
    vendor: str
    model: str
    tier: str
    estimated_cost_usd: float
    reasons: tuple[str, ...] = field(default_factory=tuple)
    alternatives: tuple[RankedCandidate, ...] = field(default_factory=tuple)
    applied_lessons: tuple[str, ...] = field(default_factory=tuple)
    cost_cap_triggered: bool = False
    cost_cap_limit_usd_per_session: float | None = None
    projected_session_cost_usd: float | None = None


class CrossVendorRouter:
    """Recommend a vendor/model pair across enabled vendors."""

    def __init__(
        self,
        config: RouteConfig,
        *,
        pricing_table: PricingTable | None = None,
        env: Mapping[str, str] | None = None,
        lesson_store: TypedLessonStore | None = None,
    ) -> None:
        self._config = config
        self._pricing_table = pricing_table or load_pricing_table()
        self._configured_vendors = detect_configured_vendors(env)
        self._candidates_by_vendor = _group_candidates(self._pricing_table.candidates)
        self._lesson_store = lesson_store

    def recommend(
        self,
        *,
        tool_name: str,
        task_text: str,
        session_state: Mapping[str, Any] | None = None,
        actual_vendor: str | None = None,
    ) -> CrossVendorRecommendation:
        enabled = tuple(v for v in self._config.enabled_vendors if v in self._configured_vendors)
        if not enabled:
            raise NoFeasibleRouteError("no enabled vendors are configured with API keys")
        try:
            vendors = allowed_vendors(
                self._config,
                tool_name=tool_name,
                actual_vendor=actual_vendor,
                configured_vendors=enabled,
            )
        except RoutePolicyError as exc:
            raise NoFeasibleRouteError(str(exc)) from exc

        requirements = infer_turn_requirements(tool_name)
        ranked: list[RankedCandidate] = []
        for vendor in vendors:
            candidate = self._recommend_for_vendor(
                vendor=vendor,
                tool_name=tool_name,
                task_text=task_text,
                session_state=session_state or {},
                requires_tool_use=requirements.requires_tool_use,
            )
            if candidate is not None:
                ranked.append(candidate)
        if not ranked:
            raise NoFeasibleRouteError("no configured vendor can satisfy the requested turn")

        ranked.sort(key=lambda item: (item.estimated_cost_usd, item.vendor, item.model))
        active_lessons = _active_lessons(self._lesson_store, session_state or {})
        routed, route_lesson_ids = apply_route_preferences(
            ranked,
            lessons=active_lessons,
            tool_name=tool_name,
            session_state=dict(session_state or {}),
        )
        capped, cost_cap_lesson_ids, cost_cap_triggered, cost_cap_limit, projected_session_cost = apply_cost_cap(
            routed,
            lessons=active_lessons,
            session_state=dict(session_state or {}),
        )
        applied_lessons = tuple(sorted({*route_lesson_ids, *cost_cap_lesson_ids}))
        for lesson_id in applied_lessons:
            if self._lesson_store is not None:
                self._lesson_store.mark_applied(lesson_id)
        best = capped[0]
        return CrossVendorRecommendation(
            vendor=best.vendor,
            model=best.model,
            tier=best.tier,
            estimated_cost_usd=best.estimated_cost_usd,
            reasons=best.reasons,
            alternatives=tuple(capped),
            applied_lessons=applied_lessons,
            cost_cap_triggered=cost_cap_triggered,
            cost_cap_limit_usd_per_session=cost_cap_limit,
            projected_session_cost_usd=projected_session_cost,
        )

    def _recommend_for_vendor(
        self,
        *,
        vendor: str,
        tool_name: str,
        task_text: str,
        session_state: Mapping[str, Any],
        requires_tool_use: bool,
    ) -> RankedCandidate | None:
        candidates = self._candidates_by_vendor.get(vendor, ())
        if not candidates:
            return None
        requirements = infer_turn_requirements(tool_name)
        if requirements.turn_kind == "read" and self._config.read_mode == "cheapest-capable":
            candidate = _fallback_candidate(candidates, requires_tool_use=False, tier="cheap")
            if candidate is None:
                return None
            estimated_cost = _estimate_cost(candidate, session_state)
            return RankedCandidate(
                vendor=vendor,
                model=candidate.model_id,
                tier=candidate.tier,
                estimated_cost_usd=estimated_cost,
                reasons=(f"vendor={vendor}: read turns prefer cheapest capable candidate",),
            )
        router = _router_for_vendor(candidates)
        scored = router.score(tool_name, task_text, session_state)
        candidate = _candidate_for_model(candidates, scored.model)
        if candidate is None:
            return None
        if not supports_turn(candidate, requirements):
            candidate = _fallback_candidate(candidates, requires_tool_use=requires_tool_use, tier=scored.tier)
        if candidate is None:
            return None
        estimated_cost = _estimate_cost(candidate, session_state)
        reasons = tuple([*scored.reasons, f"vendor={vendor}: selected cross-vendor candidate"])
        return RankedCandidate(
            vendor=vendor,
            model=candidate.model_id,
            tier=candidate.tier,
            estimated_cost_usd=estimated_cost,
            reasons=reasons,
        )


def _group_candidates(candidates: tuple[CandidateModel, ...]) -> dict[str, tuple[CandidateModel, ...]]:
    grouped: dict[str, list[CandidateModel]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.vendor].append(candidate)
    return {vendor: tuple(items) for vendor, items in grouped.items()}


def _router_for_vendor(candidates: tuple[CandidateModel, ...]) -> ModelRouter:
    cheap = _pick_candidate(candidates, tier="cheap", highest=False)
    medium = _pick_tool_use_candidate(candidates, highest=False) or cheap
    expensive = _pick_tool_use_candidate(candidates, highest=True) or medium or cheap
    return ModelRouter(
        cheap_model=cheap.model_id,
        medium_model=(medium or cheap).model_id,
        expensive_model=(expensive or medium or cheap).model_id,
    )


def _pick_candidate(candidates: tuple[CandidateModel, ...], *, tier: str, highest: bool) -> CandidateModel:
    matching = [candidate for candidate in candidates if candidate.tier == tier]
    if not matching:
        raise NoFeasibleRouteError(f"no candidate models available for tier {tier!r}")
    matching.sort(key=lambda item: _effective_price(item), reverse=highest)
    return matching[0]


def _pick_tool_use_candidate(candidates: tuple[CandidateModel, ...], *, highest: bool) -> CandidateModel | None:
    matching = [candidate for candidate in candidates if candidate.supports_tool_use]
    if not matching:
        return None
    matching.sort(key=lambda item: _effective_price(item), reverse=highest)
    return matching[0]


def _candidate_for_model(candidates: tuple[CandidateModel, ...], model_id: str) -> CandidateModel | None:
    normalized = model_id.strip().lower()
    for candidate in candidates:
        if candidate.model_id.lower() == normalized:
            return candidate
    return None


def _fallback_candidate(
    candidates: tuple[CandidateModel, ...],
    *,
    requires_tool_use: bool,
    tier: str,
) -> CandidateModel | None:
    filtered = [candidate for candidate in candidates if (not requires_tool_use or candidate.supports_tool_use)]
    if not filtered:
        return None
    preferred_tiers = ("cheap",) if tier == "cheap" else ("high", "cheap")
    for preferred_tier in preferred_tiers:
        tier_candidates = [candidate for candidate in filtered if candidate.tier == preferred_tier]
        if tier_candidates:
            tier_candidates.sort(key=_effective_price)
            return tier_candidates[0]
    filtered.sort(key=_effective_price)
    return filtered[0]


def _effective_price(candidate: CandidateModel) -> float:
    return candidate.pricing.input + candidate.pricing.output * candidate.output_multiplier


def _estimate_cost(candidate: CandidateModel, session_state: Mapping[str, Any]) -> float:
    input_tokens = _token_budget(session_state, keys=("expected_input_tokens", "budget_tokens", "max_output_tokens"), default=1000)
    output_tokens = _token_budget(session_state, keys=("expected_output_tokens",), default=max(1, int(input_tokens * 0.2)))
    adjusted_output = int(output_tokens * candidate.output_multiplier)
    return round(candidate.pricing.cost_usd(input_tokens=input_tokens, output_tokens=adjusted_output), 6)


def _active_lessons(lesson_store: TypedLessonStore | None, session_state: Mapping[str, Any]) -> list[Any]:
    if lesson_store is None:
        return []
    now = datetime.now(UTC)
    active = list(lesson_store.list_active_lessons(scope="user", at=now))
    team_id = str(session_state.get("team_id") or "").strip()
    workspace_id = str(session_state.get("workspace_id") or "").strip()
    if not team_id and not workspace_id:
        return active
    for lesson in lesson_store.list_lessons():
        if not lesson.is_active_at(now, scope=lesson.scope):
            continue
        if lesson.scope == "team" and team_id and str(lesson.metadata.get("team_id") or "") == team_id:
            active.append(lesson)
        if lesson.scope == "workspace" and workspace_id and str(lesson.metadata.get("workspace_id") or "") == workspace_id:
            active.append(lesson)
    deduped: dict[str, Any] = {lesson.id: lesson for lesson in active}
    return list(deduped.values())


def _token_budget(session_state: Mapping[str, Any], *, keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        value = session_state.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, float) and value > 0:
            return int(value)
        if isinstance(value, str):
            try:
                parsed = int(value.strip())
            except ValueError:
                continue
            if parsed > 0:
                return parsed
    return default


__all__ = ["CrossVendorRecommendation", "CrossVendorRouter", "NoFeasibleRouteError", "RankedCandidate"]
