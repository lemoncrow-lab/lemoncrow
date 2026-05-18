"""Policy presets and YAML persistence for the Optimization Advisor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import yaml

ModelTier = Literal["cheap", "medium", "expensive"]
PresetName = Literal["conservative", "balanced", "economy", "custom", "recommended", "maximum_saving"]
ConfidenceLevel = Literal["low", "medium", "high"]

DEFAULT_ESCALATE_ON: tuple[str, ...] = (
    "low_confidence",
    "failed_tests",
    "repeated_tool_error",
    "high_diff_risk",
    "user_marks_wrong",
)
DEFAULT_PRESERVE: tuple[str, ...] = (
    "user_requirements",
    "repo_facts",
    "active_plan",
    "open_files",
    "failing_tests",
    "tool_results",
)


@dataclass(frozen=True)
class CompactionPolicy:
    prompt_cache_reorder: bool
    dedup: bool
    retrieval_filter: bool
    lossy_summary: bool
    trigger_at_context_fraction: float
    preserve: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_cache_reorder": self.prompt_cache_reorder,
            "dedup": self.dedup,
            "retrieval_filter": self.retrieval_filter,
            "lossy_summary": self.lossy_summary,
            "trigger_at_context_fraction": self.trigger_at_context_fraction,
            "preserve": list(self.preserve),
        }


@dataclass(frozen=True)
class RoutingPolicy:
    policy: str
    simple: ModelTier
    medium: ModelTier
    hard: ModelTier
    escalate_on: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "simple": self.simple,
            "medium": self.medium,
            "hard": self.hard,
            "escalate_on": list(self.escalate_on),
        }


@dataclass(frozen=True)
class Policy:
    name: str
    preset: PresetName
    quality_floor: float
    confidence_required: ConfidenceLevel
    routing: RoutingPolicy
    compaction: CompactionPolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "preset": self.preset,
            "quality_floor": self.quality_floor,
            "confidence_required": self.confidence_required,
            "routing": self.routing.to_dict(),
            "compaction": self.compaction.to_dict(),
        }


def _base_compaction(
    *,
    prompt_cache_reorder: bool = True,
    dedup: bool = True,
    retrieval_filter: bool = True,
    lossy_summary: bool = False,
    trigger_at_context_fraction: float = 0.72,
) -> CompactionPolicy:
    return CompactionPolicy(
        prompt_cache_reorder=prompt_cache_reorder,
        dedup=dedup,
        retrieval_filter=retrieval_filter,
        lossy_summary=lossy_summary,
        trigger_at_context_fraction=trigger_at_context_fraction,
        preserve=list(DEFAULT_PRESERVE),
    )


def _routing(policy: str, simple: ModelTier, medium: ModelTier, hard: ModelTier) -> RoutingPolicy:
    return RoutingPolicy(
        policy=policy,
        simple=simple,
        medium=medium,
        hard=hard,
        escalate_on=list(DEFAULT_ESCALATE_ON),
    )


def preset_policy(preset: str) -> Policy:
    normalized = preset.strip().lower().replace("-", "_")
    if normalized == "conservative":
        return Policy(
            name="Conservative",
            preset="conservative",
            quality_floor=0.98,
            confidence_required="medium",
            routing=_routing("prefer_strongest", "medium", "medium", "expensive"),
            compaction=_base_compaction(retrieval_filter=False),
        )
    if normalized == "balanced":
        return Policy(
            name="Balanced",
            preset="balanced",
            quality_floor=0.96,
            confidence_required="medium",
            routing=_routing("complexity_escalate", "cheap", "medium", "expensive"),
            compaction=_base_compaction(),
        )
    if normalized == "economy":
        return Policy(
            name="Economy",
            preset="economy",
            quality_floor=0.93,
            confidence_required="low",
            routing=_routing("cheap_first", "cheap", "cheap", "medium"),
            compaction=_base_compaction(lossy_summary=True, trigger_at_context_fraction=0.65),
        )
    if normalized == "maximum_saving":
        return Policy(
            name="Maximum saving",
            preset="maximum_saving",
            quality_floor=0.88,
            confidence_required="low",
            routing=_routing("cheap_first", "cheap", "cheap", "cheap"),
            compaction=_base_compaction(lossy_summary=True, trigger_at_context_fraction=0.45),
        )
    if normalized == "recommended":
        return Policy(
            name="Recommended",
            preset="recommended",
            quality_floor=0.96,
            confidence_required="medium",
            routing=_routing("complexity_escalate", "cheap", "medium", "expensive"),
            compaction=_base_compaction(),
        )
    raise ValueError(f"unknown optimization preset: {preset}")


def identify_policy(policy: Policy, *, name: str, preset: PresetName) -> Policy:
    return Policy(
        name=name,
        preset=preset,
        quality_floor=policy.quality_floor,
        confidence_required=policy.confidence_required,
        routing=policy.routing,
        compaction=policy.compaction,
    )


def optimization_config_path(root: Path) -> Path:
    return Path(root) / "optimization.yaml"


def load_optimization_config(root: Path) -> dict[str, Any]:
    path = optimization_config_path(root)
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid optimization config at {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"optimization config at {path} must be a mapping")
    return cast(dict[str, Any], loaded)


def _tier(value: object, fallback: ModelTier) -> ModelTier:
    if value in {"cheap", "medium", "expensive"}:
        return cast(ModelTier, value)
    return fallback


def _confidence(value: object, fallback: ConfidenceLevel) -> ConfidenceLevel:
    if value in {"low", "medium", "high"}:
        return cast(ConfidenceLevel, value)
    return fallback


def _bool(value: object, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def _float(value: object, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int | float | str):
        try:
            return float(value)
        except ValueError:
            return fallback
    return fallback


def _string_list(value: object, fallback: tuple[str, ...] | list[str]) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return list(fallback)


def policy_from_config(data: dict[str, Any]) -> Policy:
    optimization = data.get("optimization")
    optimization_map = optimization if isinstance(optimization, dict) else {}
    preset_value = str(optimization_map.get("preset", data.get("preset", "balanced")))
    try:
        base = preset_policy(preset_value)
    except ValueError:
        base = identify_policy(preset_policy("balanced"), name="Custom", preset="custom")

    routing_data = data.get("routing")
    routing_map = routing_data if isinstance(routing_data, dict) else {}
    compaction_data = data.get("compaction")
    compaction_map = compaction_data if isinstance(compaction_data, dict) else {}

    routing = RoutingPolicy(
        policy=str(routing_map.get("policy", base.routing.policy)),
        simple=_tier(routing_map.get("simple"), base.routing.simple),
        medium=_tier(routing_map.get("medium"), base.routing.medium),
        hard=_tier(routing_map.get("hard"), base.routing.hard),
        escalate_on=_string_list(routing_map.get("escalate_on"), base.routing.escalate_on),
    )
    compaction = CompactionPolicy(
        prompt_cache_reorder=_bool(compaction_map.get("prompt_cache_reorder"), base.compaction.prompt_cache_reorder),
        dedup=_bool(compaction_map.get("dedup"), base.compaction.dedup),
        retrieval_filter=_bool(compaction_map.get("retrieval_filter"), base.compaction.retrieval_filter),
        lossy_summary=_bool(compaction_map.get("lossy_summary"), base.compaction.lossy_summary),
        trigger_at_context_fraction=_float(
            compaction_map.get("trigger_at_context_fraction"),
            base.compaction.trigger_at_context_fraction,
        ),
        preserve=_string_list(compaction_map.get("preserve"), base.compaction.preserve),
    )
    name = str(optimization_map.get("name", base.name))
    return Policy(
        name=name,
        preset=base.preset,
        quality_floor=_float(optimization_map.get("quality_floor"), base.quality_floor),
        confidence_required=_confidence(
            optimization_map.get("confidence_required"),
            base.confidence_required,
        ),
        routing=routing,
        compaction=compaction,
    )


def load_current_policy(root: Path) -> Policy:
    config = load_optimization_config(root)
    if not config:
        return preset_policy("balanced")
    return policy_from_config(config)


def save_policy(root: Path, policy: Policy) -> Path:
    config = load_optimization_config(root)
    optimization = config.get("optimization")
    optimization_map = dict(optimization) if isinstance(optimization, dict) else {}
    shadow_consent_at = optimization_map.get("shadow_consent_at")
    data: dict[str, Any] = {
        "optimization": {
            "name": policy.name,
            "preset": policy.preset,
            "quality_floor": policy.quality_floor,
            "confidence_required": policy.confidence_required,
        },
        "routing": policy.routing.to_dict(),
        "compaction": policy.compaction.to_dict(),
    }
    if isinstance(shadow_consent_at, str):
        data["optimization"]["shadow_consent_at"] = shadow_consent_at
    path = optimization_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def shadow_consent_at(root: Path) -> str | None:
    optimization = load_optimization_config(root).get("optimization")
    if not isinstance(optimization, dict):
        return None
    value = optimization.get("shadow_consent_at")
    return value if isinstance(value, str) and value else None


def record_shadow_consent(root: Path, when: datetime | None = None) -> str:
    config = load_optimization_config(root)
    optimization = config.get("optimization")
    optimization_map = dict(optimization) if isinstance(optimization, dict) else {}
    accepted_at = (when or datetime.now(UTC)).isoformat()
    optimization_map["shadow_consent_at"] = accepted_at
    config["optimization"] = optimization_map
    path = optimization_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return accepted_at


def forget_shadow_consent(root: Path) -> bool:
    config = load_optimization_config(root)
    optimization = config.get("optimization")
    if not isinstance(optimization, dict) or "shadow_consent_at" not in optimization:
        return False
    optimization_map = dict(optimization)
    del optimization_map["shadow_consent_at"]
    config["optimization"] = optimization_map
    optimization_config_path(root).write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return True
