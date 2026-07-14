"""Public contract types for cross-vendor routing.

Exception types, the persisted ``RouteConfig`` model, and its supporting
constants/aliases are the caller-facing API, not engine IP. They live here (open)
because mypyc cannot compile builtin-exception subclasses or pydantic models, so
the pro routing logic compiles to native ``.so`` while callers keep importing the
same names.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ROUTE_CONFIG_VERSION = 1
SUPPORTED_ROUTE_VENDORS = (
    "anthropic",
    "openai",
    "google",
    "bedrock",
    "vertex",
    "azure",
    "openrouter",
    "groq",
    "mistral",
    "ollama",
    "together",
    "fireworks",
)

EditMode = Literal["pin-actual-vendor", "allow-cross-vendor"]
ReadMode = Literal["cheapest-capable"]
AgentMode = Literal["any-tool-use"]


class RoutePolicyError(ValueError):
    """Raised when routing policy cannot be applied safely."""


class NoFeasibleRouteError(ValueError):
    """Raised when no configured vendor can satisfy the requested turn safely."""


class RouteConfigError(ValueError):
    """Raised when route configuration is missing or invalid."""


class RouteConfig(BaseModel):
    """Persisted user-owned route configuration."""

    model_config = ConfigDict(extra="forbid")

    version: int = ROUTE_CONFIG_VERSION
    risk_class: Literal["low", "medium", "high"] = "low"
    enabled_vendors: list[str] = Field(default_factory=list)
    read_mode: ReadMode = "cheapest-capable"
    edit_mode: EditMode = "pin-actual-vendor"
    agent_mode: AgentMode = "any-tool-use"

    @field_validator("enabled_vendors")
    @classmethod
    def _validate_enabled_vendors(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for vendor in value:
            item = str(vendor).strip().lower()
            if not item:
                continue
            if item not in SUPPORTED_ROUTE_VENDORS:
                supported = ", ".join(SUPPORTED_ROUTE_VENDORS)
                raise ValueError(f"unsupported vendor {item!r}; expected one of: {supported}")
            if item in seen:
                continue
            normalized.append(item)
            seen.add(item)
        if not normalized:
            raise ValueError("enabled_vendors must contain at least one supported vendor")
        return normalized
