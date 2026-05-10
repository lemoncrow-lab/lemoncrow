"""Routing execution contract (WP-31).

A ``RouteExecutionContract`` is a serialisable description of whether a specific
host can *enforce* an Atelier route decision or can only *advise* on it.

Execution modes
---------------
advisory
    Atelier returns the route decision to the agent.  The host/user decides
    whether to follow it.  This is the safe default for any host where Atelier
    has no hook or wrapper surface.

wrapper_enforced
    An Atelier wrapper (e.g. ``atelier-codex``) gates task start, model flags,
    or completion.  The wrapper can block start, enforce required flags, or
    withhold success without verification.

provider_enforced
    Atelier would perform the model call through a configured provider adapter.
    **This mode is future-only and must not be selected at runtime.**  It is
    present in the schema so that callers can confirm it is disabled.

Hook-style enforcement (``hook_enforced`` for Claude Code) is represented as
``wrapper_enforced`` with ``can_block_start=True`` because hooks are an
opt-in wrapper surface in Atelier's extension model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ExecutionMode = Literal["advisory", "wrapper_enforced", "provider_enforced"]

_KNOWN_HOSTS = frozenset({"claude", "codex", "copilot", "opencode", "gemini"})

# Per-host defaults derived from the host-capability-matrix.md contract.
# Each entry: (mode, can_block_start, can_force_model, can_require_verification,
#              fallback_mode, unsupported_reason, host_native_owner)
_HOST_CONTRACTS: dict[str, dict[str, str | bool | list[str]]] = {
    "claude": {
        "mode": "wrapper_enforced",
        "supported_tiers": ["cheap", "mid", "premium", "deterministic"],
        "can_block_start": True,
        "can_force_model": False,
        "can_require_verification": True,
        "fallback_mode": "advisory",
        "unsupported_reason": (
            "provider_enforced is future-only and disabled; " "full model-provider override is outside host surface"
        ),
        "host_native_owner": "model,agent_orchestration",
    },
    "codex": {
        "mode": "wrapper_enforced",
        "supported_tiers": ["cheap", "mid", "premium", "deterministic"],
        "can_block_start": True,
        "can_force_model": False,
        "can_require_verification": True,
        "fallback_mode": "advisory",
        "unsupported_reason": (
            "hook_enforced parity with Claude hooks is unsupported; " "provider_enforced is future-only and disabled"
        ),
        "host_native_owner": "model,edit,agent_orchestration",
    },
    "copilot": {
        "mode": "advisory",
        "supported_tiers": ["cheap", "mid", "premium", "deterministic"],
        "can_block_start": False,
        "can_force_model": False,
        "can_require_verification": False,
        "fallback_mode": "advisory",
        "unsupported_reason": (
            "host-level hard blocking of model/tool calls is unsupported; "
            "provider_enforced is future-only and disabled"
        ),
        "host_native_owner": "model,edit,compact,agent_orchestration",
    },
    "opencode": {
        "mode": "wrapper_enforced",
        "supported_tiers": ["cheap", "mid", "premium", "deterministic"],
        "can_block_start": True,
        "can_force_model": False,
        "can_require_verification": True,
        "fallback_mode": "advisory",
        "unsupported_reason": ("cross-host hook parity is unsupported; provider_enforced is future-only and disabled"),
        "host_native_owner": "model,edit,agent_orchestration",
    },
    "gemini": {
        "mode": "advisory",
        "supported_tiers": ["cheap", "mid", "premium", "deterministic"],
        "can_block_start": False,
        "can_force_model": False,
        "can_require_verification": False,
        "fallback_mode": "advisory",
        "unsupported_reason": (
            "host-native hard enforcement beyond wrapper is unsupported; "
            "provider_enforced is future-only and disabled"
        ),
        "host_native_owner": "model,edit,compact,agent_orchestration",
    },
}


class RouteExecutionContract(BaseModel):
    """Describes whether and how a host can enforce an Atelier route decision."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(description="Identifier for the host CLI/IDE integration.")
    mode: ExecutionMode = Field(
        description=(
            "advisory: Atelier can only advise. "
            "wrapper_enforced: Atelier wrapper gates start/completion. "
            "provider_enforced: disabled (future-only)."
        )
    )
    supported_tiers: list[str] = Field(
        default_factory=list,
        description="Execution tiers the host accepts route decisions for.",
    )
    can_block_start: bool = Field(description="Whether the wrapper/hook surface can prevent a task from starting.")
    can_force_model: bool = Field(
        description=(
            "Whether Atelier can override the model selection made by the host. "
            "Always False unless provider_enforced mode is enabled."
        )
    )
    can_require_verification: bool = Field(
        description="Whether the wrapper surface can withhold task completion pending verification."
    )
    fallback_mode: ExecutionMode = Field(description="Mode used when the primary enforcement surface is unavailable.")
    unsupported_reason: str = Field(
        description="Human-readable note on what enforcement capabilities are not available."
    )
    host_native_owner: str = Field(
        description=(
            "Comma-separated list of capabilities that remain owned by the host, "
            "e.g. 'model,edit,compact,agent_orchestration'."
        )
    )
    provider_enforced_disabled: bool = Field(
        default=True,
        description=("Always True. provider_enforced mode is future-only and cannot be selected."),
    )


def route_execution_contract(host: str) -> RouteExecutionContract:
    """Return the ``RouteExecutionContract`` for the named host.

    Raises ``ValueError`` for unknown hosts.  Known hosts:
    ``claude``, ``codex``, ``copilot``, ``opencode``, ``gemini``.

    The ``provider_enforced`` mode is **never** returned as the active ``mode``
    because no provider execution packet exists yet.
    """
    normalized = host.strip().lower()
    if normalized not in _KNOWN_HOSTS:
        raise ValueError(f"Unknown host {host!r}. Supported hosts: {sorted(_KNOWN_HOSTS)}")
    data = _HOST_CONTRACTS[normalized]
    tiers = data["supported_tiers"]
    return RouteExecutionContract(
        host=normalized,
        mode=data["mode"],  # type: ignore[arg-type]
        supported_tiers=list(tiers) if isinstance(tiers, list) else [],
        can_block_start=bool(data["can_block_start"]),
        can_force_model=bool(data["can_force_model"]),
        can_require_verification=bool(data["can_require_verification"]),
        fallback_mode=data["fallback_mode"],  # type: ignore[arg-type]
        unsupported_reason=str(data["unsupported_reason"]),
        host_native_owner=str(data["host_native_owner"]),
    )
