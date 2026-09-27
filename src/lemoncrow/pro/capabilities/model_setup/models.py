"""Value objects for user-registered, OpenAI-compatible model endpoints.

Why this module exists: three consumers -- the ``providers.json`` writer, the
capability probe, and the routing candidate builder -- have to agree on what a
custom endpoint and a probed model *are*, and none of them may own that shape.
Pinning it in one stdlib-only module is what lets endpoint registration ship
before the probe exists, and keeps ``--json`` a mechanical projection of the
dataclasses rather than a hand-maintained second contract.

Unknown is a first-class value here. ``supports_tool_use=None`` means "not
probed / not determinable" and must never be collapsed into ``False`` at this
layer: routing makes that pessimistic choice for itself, and that is a decision
about risk, not a fact about the endpoint.

No credential field exists on ``CustomEndpoint`` by construction -- these
objects are what ``--json`` prints, so a key that is not modelled is a key that
cannot leak.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Bump on any field removal or type change. Additive changes keep version 1;
# consumers must ignore unknown keys.
PROBE_SCHEMA_VERSION = 1

# The providers.json key and the litellm prefix for user-registered endpoints.
# Model ids are ``custom/<endpoint_name>/<raw_id>``; the raw id may itself
# contain "/" (e.g. "Qwen/Qwen3-Coder-30B"), so only the first two segments are
# structural.
CUSTOM_PROVIDER = "custom"
CUSTOM_PREFIX = "custom/"

ProbeCheckName = Literal[
    "chat_completion",
    "tool_calling",
    "structured_output",
    "context_window",
    "throughput",
    "vision",
]
PROBE_CHECK_NAME_VALUES: tuple[str, ...] = (
    "chat_completion",
    "tool_calling",
    "structured_output",
    "context_window",
    "throughput",
    "vision",
)

# "unsupported" is a *measured* negative (the server rejected the feature);
# "unknown" is the absence of evidence. Keeping them distinct is what stops an
# unreachable endpoint from being recorded as a capability-less one.
ProbeStatus = Literal["ok", "unsupported", "unknown", "error"]
PROBE_STATUS_VALUES: tuple[str, ...] = ("ok", "unsupported", "unknown", "error")

ModelTier = Literal["cheap", "high"]
MODEL_TIER_VALUES: tuple[str, ...] = ("cheap", "high")


@dataclass(frozen=True)
class ProbeCheck:
    """One capability question asked of a live endpoint, and its answer."""

    name: ProbeCheckName
    status: ProbeStatus
    detail: str
    value: float | int | str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "value": self.value}


@dataclass(frozen=True)
class ProbeResult:
    """Everything one probe run learned about one model on one endpoint."""

    schema_version: int
    endpoint: str
    endpoint_name: str
    raw_model_id: str
    model_id: str
    checks: tuple[ProbeCheck, ...] = ()
    context_window: int | None = None
    supports_tool_use: bool | None = None
    supports_structured_output: bool | None = None
    supports_vision: bool | None = None
    observed_tokens_per_second: float | None = None
    latency_first_token_ms: float | None = None
    probed_at: str = ""
    ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "endpoint": self.endpoint,
            "endpoint_name": self.endpoint_name,
            "raw_model_id": self.raw_model_id,
            "model_id": self.model_id,
            "checks": [check.to_dict() for check in self.checks],
            "context_window": self.context_window,
            "supports_tool_use": self.supports_tool_use,
            "supports_structured_output": self.supports_structured_output,
            "supports_vision": self.supports_vision,
            "observed_tokens_per_second": self.observed_tokens_per_second,
            "latency_first_token_ms": self.latency_first_token_ms,
            "probed_at": self.probed_at,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class CustomModel:
    """A model registered against a custom endpoint, as persisted on disk."""

    raw_id: str
    supports_tool_use: bool | None = None
    supports_structured_output: bool | None = None
    supports_vision: bool | None = None
    context_window: int | None = None
    tier: ModelTier = "cheap"
    observed_tokens_per_second: float | None = None
    probed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_id": self.raw_id,
            "supports_tool_use": self.supports_tool_use,
            "supports_structured_output": self.supports_structured_output,
            "supports_vision": self.supports_vision,
            "context_window": self.context_window,
            "tier": self.tier,
            "observed_tokens_per_second": self.observed_tokens_per_second,
            "probed_at": self.probed_at,
        }


@dataclass(frozen=True)
class CustomEndpoint:
    """A registered OpenAI-compatible endpoint. Carries no secret, ever.

    ``api_key_env`` names the environment variable to read at request time; the
    literal key (when the user insisted on ``--api-key``) stays in
    ``providers.json`` and is deliberately absent from this projection.
    """

    name: str
    base_url: str
    api_key_env: str | None = None
    models: tuple[CustomModel, ...] = ()

    def model_ids(self) -> tuple[str, ...]:
        """Fully-qualified ``custom/<endpoint>/<raw>`` ids for this endpoint."""

        return tuple(custom_model_id(self.name, model.raw_id) for model in self.models)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "models": [model.to_dict() for model in self.models],
            "model_ids": list(self.model_ids()),
        }


def custom_model_id(endpoint_name: str, raw_id: str) -> str:
    """Namespace a server-reported model id by the endpoint that serves it.

    Two endpoints commonly serve the same ``raw_id`` (every vLLM box hosting
    Qwen reports "Qwen3-Coder-30B"), so the endpoint name is part of the id.
    """

    return f"{CUSTOM_PREFIX}{endpoint_name}/{raw_id}"


def split_custom_model_id(model: str) -> tuple[str, str] | None:
    """``custom/e1/Qwen/Qwen3`` -> ``("e1", "Qwen/Qwen3")``; ``None`` when not ours.

    Only the first two segments are structural: the raw id keeps every slash it
    came with, because that is what the endpoint expects back.
    """

    if not model.startswith(CUSTOM_PREFIX):
        return None
    remainder = model[len(CUSTOM_PREFIX) :]
    endpoint_name, separator, raw_id = remainder.partition("/")
    if not separator or not endpoint_name or not raw_id:
        return None
    return endpoint_name, raw_id


__all__ = [
    "CUSTOM_PREFIX",
    "CUSTOM_PROVIDER",
    "MODEL_TIER_VALUES",
    "PROBE_CHECK_NAME_VALUES",
    "PROBE_SCHEMA_VERSION",
    "PROBE_STATUS_VALUES",
    "CustomEndpoint",
    "CustomModel",
    "ModelTier",
    "ProbeCheck",
    "ProbeCheckName",
    "ProbeResult",
    "ProbeStatus",
    "custom_model_id",
    "split_custom_model_id",
]
