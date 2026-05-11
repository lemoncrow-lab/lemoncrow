"""Quality-aware routing policy configuration."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from atelier.core.foundation.paths import default_store_root
from atelier.core.foundation.routing_models import ContextBudgetPolicy

DEFAULT_ROUTING_CONFIG_PATH = default_store_root() / "routing.toml"


class ModelTierConfig(BaseModel):
    """Provider-neutral model identifiers for each execution tier."""

    model_config = ConfigDict(extra="forbid")

    cheap: str = ""
    mid: str = ""
    premium: str = ""


class RouteThresholdConfig(BaseModel):
    """Thresholds used by the local routing policy."""

    model_config = ConfigDict(extra="forbid")

    cheap_context_ratio_max: float = Field(default=0.35, ge=0, le=1)
    premium_context_ratio_min: float = Field(default=0.85, ge=0, le=1)
    premium_evidence_confidence_max: float = Field(default=0.35, ge=0, le=1)
    cheap_evidence_confidence_min: float = Field(default=0.80, ge=0, le=1)


class VerifierRequirementConfig(BaseModel):
    """Verifier lists attached to decisions by route class."""

    model_config = ConfigDict(extra="forbid")

    default: list[str] = Field(default_factory=lambda: ["tests"])
    protected_file: list[str] = Field(default_factory=lambda: ["tests", "review"])
    high_risk: list[str] = Field(default_factory=lambda: ["tests", "rubric"])
    low_confidence: list[str] = Field(default_factory=lambda: ["tests", "review"])


class RoutingPolicyConfig(BaseModel):
    """Local policy configuration loaded from ``.atelier/routing.toml``."""

    model_config = ConfigDict(extra="forbid")

    models: ModelTierConfig = Field(default_factory=ModelTierConfig)
    thresholds: RouteThresholdConfig = Field(default_factory=RouteThresholdConfig)
    protected_file_patterns: list[str] = Field(
        default_factory=lambda: [
            ".github/workflows/**",
            ".github/actions/**",
            "pyproject.toml",
            "uv.lock",
            "src/atelier/core/foundation/**",
            "src/atelier/infra/storage/migrations/**",
            "src/atelier/gateway/adapters/**",
        ]
    )
    high_risk_domain_patterns: list[str] = Field(
        default_factory=lambda: [
            "state.change*",
            "source.truth*",
            "policy.change*",
            "knowledge*",
        ]
    )
    verifiers: VerifierRequirementConfig = Field(default_factory=VerifierRequirementConfig)

    @field_validator("protected_file_patterns", "high_risk_domain_patterns")
    @classmethod
    def _reject_empty_patterns(cls, value: list[str]) -> list[str]:
        return [pattern for pattern in value if pattern.strip()]

    def budget_policy(self, *, max_input_tokens: int, premium_call_budget: int = 1) -> ContextBudgetPolicy:
        """Create a routing budget policy from configured tier model names."""

        return ContextBudgetPolicy(
            max_input_tokens=max_input_tokens,
            premium_call_budget=premium_call_budget,
            cheap_model=self.models.cheap,
            mid_model=self.models.mid,
            premium_model=self.models.premium,
        )


def routing_config_path(repo_root: str | Path) -> Path:
    """Return the default routing config path for a repository root."""

    return Path(repo_root) / DEFAULT_ROUTING_CONFIG_PATH


def load_routing_policy_config(
    repo_root: str | Path = ".",
    *,
    path: str | Path | None = None,
) -> RoutingPolicyConfig:
    """Load routing policy config, falling back to defaults when missing."""

    config_path = Path(path) if path is not None else routing_config_path(repo_root)
    if not config_path.exists():
        return RoutingPolicyConfig()

    raw: dict[str, Any] = tomllib.loads(config_path.read_text(encoding="utf-8"))
    raw = _normalize_raw_config(raw)
    return RoutingPolicyConfig.model_validate(raw)


def _normalize_raw_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Accept common TOML placement mistakes without weakening strict models."""

    normalized = dict(raw)
    for section_name in ("models", "thresholds", "verifiers"):
        section = normalized.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in ("protected_file_patterns", "high_risk_domain_patterns"):
            if key in section and key not in normalized:
                normalized[key] = section.pop(key)
    return normalized


__all__ = [
    "DEFAULT_ROUTING_CONFIG_PATH",
    "ModelTierConfig",
    "RouteThresholdConfig",
    "RoutingPolicyConfig",
    "VerifierRequirementConfig",
    "load_routing_policy_config",
    "routing_config_path",
]
