"""Shared provider-neutral execution contract for Review Intelligence.

This module owns driver/model resolution, structured execution and honest usage
metadata. Durable identity/budget accounting lives one layer above it so every
capability can share the same execution primitive without sharing prompts.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from lemoncrow.infra.internal_llm import InternalLLMError
from lemoncrow.infra.internal_llm.result import InternalLLMChatResult
from lemoncrow.pro.capabilities.review.intelligence_drivers import (
    CliExecutionCancelled,
    ExecutionOrigin,
    _execute_cli_structured,
    tool_profile_for,
)
from lemoncrow.pro.capabilities.review.model_identity import ModelIdentity, observed_from

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.intelligence_policy import (
        ContentAccessBinding,
        EffectiveIntelligencePolicy,
        RouteRequest,
    )

ReviewIntelligenceDriver = Literal[
    "claude",
    "codex",
    "opencode",
    "ollama",
    "openai",
    "litellm",
]
ReviewIntelligenceCapability = Literal[
    "ask",
    "brief_synthesis",
    "tour_synthesis",
    "behavior_narrative",
    "architecture_explain",
    "hypothesis_generation",
    "finding_investigation",
    "finding_dedupe",
    "premerge_check",
    # P11-ESC: one bounded question per trusted Review rule.
    "rule_check",
    # DS0: one source-linked summary of a Review discussion thread.
    "thread_summary",
]
#: Drivers that start a local agent CLI process.
AGENT_CLI_DRIVERS = frozenset({"claude", "codex", "opencode"})
#: The deployment's configured route: what ``driver: auto`` (or a legacy ``configured``) resolves.
LLM_BACKEND_ENV = "LEMONCROW_LLM_BACKEND"
#: Process-wide host setting; a hosted control plane turns it off before it serves.
_agent_cli_drivers_allowed = True


class AgentCliDriverRefused(InternalLLMError):
    """The route resolved to an agent CLI on a host that never starts one."""


class IntelligenceDisabled(InternalLLMError):
    """The effective intelligence policy turns model routes off, or cannot be read (fail closed)."""


class RouteOverrideRefused(InternalLLMError):
    """A request named a driver or model the operator policy pinned to something else."""


#: Policy layers whose pinned driver/model a request may not override (operator Review policy).
_LOCKING_LAYERS = frozenset({"organization", "repository"})


def forbid_agent_cli_drivers() -> None:
    """Refuse every agent-CLI route in this process from now on (one-way).

    The hosted control plane calls this before it serves anything. Every Review
    Intelligence call resolves its route through :func:`resolve_driver_model`
    before any CLI can start, so that refusal is the single authority: each
    surface then degrades exactly as it does with no provider configured, and
    route-level 400s only translate it for the client.
    """

    global _agent_cli_drivers_allowed
    _agent_cli_drivers_allowed = False


def agent_cli_drivers_allowed() -> bool:
    return _agent_cli_drivers_allowed


class CancellationCheck(Protocol):
    def __call__(self) -> bool: ...


class IntelligenceCancelled(InternalLLMError):
    """Raised when a Review Intelligence call is cancelled before publication."""


@dataclass(frozen=True, slots=True)
class ModelCallBudget:
    """Provider-neutral hard ceilings for one Review Intelligence operation."""

    max_calls: int = 1
    max_review_calls: int = 20
    max_cost: float | None = None
    currency: str = "USD"
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        if self.max_calls < 0 or self.max_review_calls < 0:
            raise ValueError("model-call budgets cannot be negative")
        if self.max_cost is not None and self.max_cost < 0:
            raise ValueError("cost budget cannot be negative")
        if self.timeout_s <= 0:
            raise ValueError("timeout must be positive")

    @classmethod
    def from_env(cls, *, max_calls: int = 1, timeout_s: float = 120.0) -> ModelCallBudget:
        raw_review_calls = os.environ.get(
            "LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS",
            os.environ.get("LEMONCROW_REVIEW_SYNTHESIS_MAX_CALLS", "20"),
        )
        try:
            max_review_calls = max(0, int(raw_review_calls))
        except ValueError:
            max_review_calls = 20
        raw_cost = os.environ.get("LEMONCROW_REVIEW_INTELLIGENCE_MAX_COST", "").strip()
        try:
            max_cost = max(0.0, float(raw_cost)) if raw_cost else None
        except ValueError:
            max_cost = None
        return cls(
            max_calls=max_calls,
            max_review_calls=max_review_calls,
            max_cost=max_cost,
            timeout_s=timeout_s,
        )


@dataclass(frozen=True, slots=True)
class ModelCallUsage:
    """Usage known from the provider/runtime; unknown fields remain None."""

    calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    provider_cost: float | None = None
    currency: str | None = None
    request_id: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "provider_cost": self.provider_cost,
            "currency": self.currency,
            "request_id": self.request_id,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> ModelCallUsage:
        def optional_int(name: str) -> int | None:
            raw = value.get(name)
            return int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None

        raw_cost = value.get("provider_cost")
        provider_cost = (
            float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) else None
        )
        return cls(
            calls=max(0, int(value.get("calls") or 0)),
            input_tokens=optional_int("input_tokens"),
            output_tokens=optional_int("output_tokens"),
            cache_read_input_tokens=optional_int("cache_read_input_tokens"),
            cache_write_input_tokens=optional_int("cache_write_input_tokens"),
            provider_cost=provider_cost,
            currency=str(value.get("currency") or "") or None,
            request_id=str(value.get("request_id") or ""),
        )


@dataclass(frozen=True, slots=True)
class ResolvedDriver:
    driver: ReviewIntelligenceDriver
    model: str


@dataclass(frozen=True, slots=True)
class StructuredExecution:
    result: dict[str, Any]
    driver: ReviewIntelligenceDriver
    model: str
    usage: ModelCallUsage
    #: Models the driver/provider itself reported executing (P11 item 15a);
    #: empty when unreported. ``model`` stays the requested identity.
    observed_models: tuple[str, ...] = ()


def _cancelled(check: CancellationCheck | None) -> bool:
    return bool(check is not None and check())


def _resolve_explicit_model(driver: ReviewIntelligenceDriver, model: str | None) -> str:
    if model:
        return model
    if driver == "openai":
        from lemoncrow.infra.internal_llm.openai_client import _resolve_model as resolve_openai_model

        return resolve_openai_model()
    if driver == "litellm":
        from lemoncrow.infra.internal_llm.litellm_client import _resolve_model as resolve_litellm_model

        return resolve_litellm_model(None)
    if driver == "ollama":
        from lemoncrow.infra.internal_llm.ollama_client import _resolve_model as resolve_ollama_model

        return resolve_ollama_model(None)
    # Subscription CLIs own their default aliases. Empty is deliberate durable
    # identity: it means the driver's configured/default model, not a guessed
    # model name or fabricated billing identity.
    return ""


#: The effective policy of the work this context is doing (a hosted job's operator
#: policy), consulted wherever no explicit policy is passed. ``None``: the process's.
_bound_policy: ContextVar[EffectiveIntelligencePolicy | None] = ContextVar(
    "lemoncrow_bound_intelligence_policy", default=None
)


@contextmanager
def bound_intelligence_policy(policy: EffectiveIntelligencePolicy | None) -> Iterator[None]:
    """Resolve every route in this context under *policy* (``None`` changes nothing).

    A hosted job binds its operator policy around its whole execution, so a
    nested resolve (reviewer selection, a structured executor, the facade's
    kill switch) cannot fall back to the process default.
    """

    if policy is None:
        yield
        return
    token = _bound_policy.set(policy)
    try:
        yield
    finally:
        _bound_policy.reset(token)


def require_intelligence_enabled(policy: EffectiveIntelligencePolicy | None = None) -> EffectiveIntelligencePolicy:
    """The effective policy, or :class:`IntelligenceDisabled`.

    *policy* ``None`` takes the policy bound to this context
    (:func:`bound_intelligence_policy`), else this process's. The kill switch
    every model call consults: an invalid or disabled policy refuses before
    any route is chosen or any byte leaves the process.
    """

    if policy is None:
        policy = _bound_policy.get()
    if policy is None:
        from lemoncrow.pro.capabilities.review.intelligence_policy import process_intelligence_policy

        policy = process_intelligence_policy()
    if not policy.valid:
        raise IntelligenceDisabled("intelligence policy is invalid: " + "; ".join(policy.errors))
    if not policy.enabled.value:
        raise IntelligenceDisabled(f"intelligence is turned off ({policy.enabled.origin.source})")
    return policy


def resolve_driver_model(
    driver: str | None,
    model: str | None,
    *,
    policy: EffectiveIntelligencePolicy | None = None,
) -> ResolvedDriver:
    """Resolve a request to the actual backend/model used for cache identity.

    (``intelligence_policy.EffectiveIntelligencePolicy``); without one, the
    policy bound to this context applies, else this process's (its settings
    file over the built-in default, which is ``driver: auto``, ``model: auto``:
    exactly the legacy behavior).
    An explicit *driver* is the caller's own choice and wins over a settings
    driver and model, but never over a driver or model an operator layer
    (organization/repository) pinned: that refuses with
    :class:`RouteOverrideRefused`. ``None``/``configured``/``auto`` take the
    policy's driver, and ``driver: auto`` reads the deployment's configured
    route (``LEMONCROW_LLM_BACKEND``). An empty *model* is no model. An invalid
    or disabled policy refuses every route; the hosted agent-CLI refusal
    applies whatever chose the driver.
    """

    policy = require_intelligence_enabled(policy)
    model = (model or "").strip() or None
    requested = (driver or "configured").lower().strip()
    if requested == "openai_compatible":
        requested = "openai"
    pinned_driver = str(policy.driver.value) if policy.driver.origin.layer in _LOCKING_LAYERS else "auto"
    pinned_model = str(policy.model.value) if policy.model.origin.layer in _LOCKING_LAYERS else "auto"
    if requested not in {"", "configured", "auto"} and pinned_driver != "auto" and requested != pinned_driver:
        raise RouteOverrideRefused(
            f"the {policy.driver.origin.layer} intelligence policy pins the driver; a request may not choose another"
        )
    if model is not None and pinned_model != "auto" and model != pinned_model:
        raise RouteOverrideRefused(
            f"the {policy.model.origin.layer} intelligence policy pins the model; a request may not choose another"
        )
    if requested == pinned_driver and model is None and pinned_model != "auto":
        model = pinned_model
    if requested in {"", "configured", "auto"}:
        if model is None and policy.model.value != "auto":
            model = str(policy.model.value)
        configured = str(policy.driver.value)
        if configured == "auto":
            configured = os.environ.get(LLM_BACKEND_ENV, "none").lower().strip()
        if configured == "openai_compatible":
            configured = "openai"
        requested = configured
    if requested not in {"claude", "codex", "opencode", "ollama", "openai", "litellm"}:
        if requested == "none":
            raise InternalLLMError(
                "Review Intelligence provider is disabled; configure a provider or select a local/subscription driver"
            )
        raise InternalLLMError(f"Unsupported Review Intelligence driver: {requested}")
    if requested in AGENT_CLI_DRIVERS and not _agent_cli_drivers_allowed:
        raise AgentCliDriverRefused(
            f"Review Intelligence route {requested!r} is an agent CLI, and agent CLIs do not run on this server; "
            "configure a model route (openai, litellm or ollama)"
        )
    resolved_driver = cast(ReviewIntelligenceDriver, requested)
    return ResolvedDriver(resolved_driver, _resolve_explicit_model(resolved_driver, model))


def _usage_from_internal(result: InternalLLMChatResult) -> ModelCallUsage:
    return ModelCallUsage(
        calls=1,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_input_tokens=result.cache_read_input_tokens,
        cache_write_input_tokens=result.cache_write_input_tokens,
        provider_cost=result.provider_cost,
        currency=result.currency if result.provider_cost is not None else None,
        request_id=result.request_id,
    )


def _mapping_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InternalLLMError("Review Intelligence model returned a non-object response")
    return dict(value)


def execute_structured(
    *,
    system: str,
    prompt: str,
    output_schema: Mapping[str, Any],
    driver: str | None = None,
    model: str | None = None,
    project_root: Path | None = None,
    origin: ExecutionOrigin,
    source_kind: Literal["review", "code"] = "review",
    on_raw_delta: Callable[[str], None] | None = None,
    cancellation: CancellationCheck | None = None,
    timeout_s: float = 120.0,
    cli_executor: Callable[..., Mapping[str, Any]] | None = None,
    provider_stream: Callable[..., Any] | None = None,
    cli_output_instruction: str = "",
    policy: EffectiveIntelligencePolicy | None = None,
) -> StructuredExecution:
    """Execute one structured model call through any supported Review driver.

    project_root is execution-only state. It is never part of durable identity.
    CLI Review execution refuses to run without an immutable Snapshot root.

    ``origin`` is required and typed: the caller that knows who started the call
    states it. Only ``human_ask`` (Review Ask, Code Ask) earns the shell/web
    ``review`` profile; ``machine`` work (synthesis, narration, pre-merge and
    rule checks, managed jobs) and any origin that is not exactly ``human_ask``
    get the read-only ``review_autonomous`` profile. This is the only route to
    the private CLI executor, and it resolves (and may refuse) the route first.
    ``cli_executor`` is a test seam called in its place, after the same checks.
    *policy* is the effective intelligence policy the route resolves under
    (``resolve_driver_model``).
    """

    value: Any
    provider_result: InternalLLMChatResult
    if _cancelled(cancellation):
        raise IntelligenceCancelled("Review Intelligence run was cancelled")
    # The legacy two-argument shape without a caller policy keeps existing stand-ins working.
    resolved = resolve_driver_model(driver, model, **({} if policy is None else {"policy": policy}))
    observed: list[str] = []
    cli_usage: dict[str, Any] = {}

    def record_cli_usage(reported: Mapping[str, Any]) -> None:
        # CLI metering is informational: a subscription process cannot supply
        # an enforceable provider invoice or an authoritative dollar charge.
        cli_usage.update(reported)

    def optional_nonnegative(name: str) -> int | None:
        value = cli_usage.get(name)
        return value if type(value) is int and value >= 0 else None

    if resolved.driver in {"claude", "codex", "opencode"}:
        if source_kind == "review" and project_root is None:
            raise InternalLLMError(f"{resolved.driver} Review Intelligence requires an immutable Review snapshot")
        cli_origin: ExecutionOrigin = "human_ask" if tool_profile_for(origin) == "review" else "machine"
        if project_root is None:
            raise InternalLLMError(f"{resolved.driver} execution requires a project root")
        if cli_executor is not None:
            value = cli_executor(
                driver=resolved.driver,
                model=resolved.model or None,
                system=system,
                prompt=prompt,
                project_root=project_root,
                on_raw_delta=on_raw_delta,
                source_kind=source_kind,
                origin=cli_origin,
                tool_profile=tool_profile_for(cli_origin),
            )
        else:
            try:
                value = _execute_cli_structured(
                    driver=resolved.driver,
                    model=resolved.model or None,
                    system=system,
                    prompt=prompt,
                    schema=output_schema,
                    project_root=project_root,
                    on_raw_delta=on_raw_delta,
                    source_kind=source_kind,
                    origin=cli_origin,
                    output_instruction=cli_output_instruction,
                    timeout_s=timeout_s,
                    on_model_reported=observed.append,
                    on_usage_reported=record_cli_usage,
                    cancellation=cancellation,
                )
            except CliExecutionCancelled as exc:
                raise IntelligenceCancelled(str(exc)) from exc
        if _cancelled(cancellation):
            raise IntelligenceCancelled("Review Intelligence run was cancelled")
        return StructuredExecution(
            result=_mapping_result(value),
            driver=resolved.driver,
            model=resolved.model,
            usage=ModelCallUsage(
                calls=1,
                input_tokens=optional_nonnegative("input_tokens"),
                output_tokens=optional_nonnegative("output_tokens"),
                cache_read_input_tokens=optional_nonnegative("cached_input_tokens"),
                cache_write_input_tokens=optional_nonnegative("cache_write_input_tokens"),
            ),
            observed_models=tuple(observed),
        )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    if on_raw_delta is not None:
        if provider_stream is not None:
            value = provider_stream(
                messages,
                on_delta=on_raw_delta,
                json_schema=dict(output_schema),
                backend=resolved.driver,
                model=resolved.model or None,
            )
        else:
            from lemoncrow.infra.internal_llm import chat_stream

            value = chat_stream(
                messages,
                on_delta=on_raw_delta,
                json_schema=dict(output_schema),
                backend=resolved.driver,
                model=resolved.model or None,
            )
        if _cancelled(cancellation):
            raise IntelligenceCancelled("Review Intelligence run was cancelled")
        return StructuredExecution(
            result=_mapping_result(value),
            driver=resolved.driver,
            model=resolved.model,
            usage=ModelCallUsage(calls=1),
        )

    if resolved.driver == "openai":
        from lemoncrow.infra.internal_llm.openai_client import chat_with_result as openai_chat_with_result

        provider_result = openai_chat_with_result(
            messages,
            model=resolved.model or None,
            json_schema=dict(output_schema),
            timeout=timeout_s,
        )
        value = provider_result.parsed_json or {}
        usage = _usage_from_internal(provider_result)
        observed.append(provider_result.reported_model)
    elif resolved.driver == "litellm":
        from lemoncrow.infra.internal_llm.litellm_client import chat_with_result as litellm_chat_with_result

        provider_result = litellm_chat_with_result(
            messages,
            model=resolved.model or None,
            json_schema=dict(output_schema),
        )
        value = provider_result.parsed_json or {}
        usage = _usage_from_internal(provider_result)
        observed.append(provider_result.reported_model)
    else:
        from lemoncrow.infra.internal_llm.ollama_client import chat

        value = chat(messages, model=resolved.model or None, json_schema=dict(output_schema))
        usage = ModelCallUsage(calls=1)

    if _cancelled(cancellation):
        raise IntelligenceCancelled("Review Intelligence run was cancelled")
    return StructuredExecution(
        result=_mapping_result(value),
        driver=resolved.driver,
        model=resolved.model,
        usage=usage,
        observed_models=tuple(item for item in observed if item),
    )


@dataclass(frozen=True, slots=True)
class IntelligenceIdentity:
    run_key: str
    scope_digest: str
    context_digest: str
    policy_digest: str
    schema_digest: str


@dataclass(frozen=True, slots=True)
class IntelligenceRunRequest:
    review_id: str
    revision_id: str
    snapshot_identity: str
    capability: ReviewIntelligenceCapability
    scope: Any
    context: Any
    policy: Any
    driver: str | None
    model: str | None
    output_schema: Mapping[str, Any]
    prompt_version: int
    schema_version: int
    budget: ModelCallBudget
    #: P11 item 15b: an explicit reviewer/admin fresh re-run. Non-empty executes
    #: again despite a ready row for the same run key (never a fake input
    #: change), under the normal budget; replays of the action reuse its row.
    fresh_action_id: str = ""
    #: BI0: the effective intelligence policy the route resolves under (``None``:
    #: this process's). Its requested choice is persisted beside the resolved
    #: and observed identity.
    route_policy: EffectiveIntelligencePolicy | None = None
    #: BI0: the effective AI content-access policy bound to this route. When
    #: set, its digest joins the run key, and a malformed or denying binding
    #: refuses the run before the executor (so before any byte reaches a model).
    content_access: ContentAccessBinding | None = None
    #: Repository paths whose content this run sends to the model (checked
    #: against ``content_access`` before execution). With a binding it must be
    #: declared: ``None`` refuses, ``()`` means no repository content.
    content_paths: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class IntelligenceRunOutcome:
    run_id: str
    status: Literal["pending", "ready", "failed", "cancelled"]
    result: dict[str, Any]
    driver: str
    model: str
    usage: ModelCallUsage
    provider_calls_this_request: int
    duration_ms: int
    cache_hit: bool
    error: str = ""
    #: Requested vs observed identity persisted on the run (P11 item 15a).
    identity: ModelIdentity | None = None
    #: ``fresh_rerun`` when the row was produced by an explicit fresh re-run.
    execution_mode: str = "standard"
    #: BI0: the configured/requested route (``auto`` kept as asked), when the run recorded one.
    configured: RouteRequest | None = None

    def to_wire(self) -> dict[str, Any]:
        configured = {} if self.configured is None else {"configured_route": self.configured.to_wire()}
        return {
            **configured,
            "run_id": self.run_id,
            "status": self.status,
            "driver": self.driver,
            "model": self.model,
            "usage": self.usage.to_wire(),
            "provider_calls_this_request": self.provider_calls_this_request,
            "duration_ms": self.duration_ms,
            "cache_hit": self.cache_hit,
            "error": self.error,
            "identity": (self.identity or ModelIdentity(self.driver, self.model)).to_wire(),
            "execution_mode": self.execution_mode,
        }


def intelligence_input_digest(value: Any) -> str:
    """Stable SHA-256 over JSON-compatible semantic input."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_intelligence_identity(
    *,
    revision_id: str,
    snapshot_identity: str,
    capability: str,
    scope: Any,
    context: Any,
    policy: Any,
    driver: str,
    model: str,
    output_schema: Mapping[str, Any],
    prompt_version: int,
    schema_version: int,
    content_access_digest: str = "",
) -> IntelligenceIdentity:
    """The durable identity of one model-derived result.

    ``content_access_digest`` (``ContentAccessBinding.digest``: the effective
    content-access rules and the route trust they were decided for) joins the
    run key only when given, so runs keyed before BI0 keep their identity.
    """

    if not revision_id or not snapshot_identity:
        raise ValueError("Review Intelligence identity requires revision and Snapshot identity")
    scope_digest = intelligence_input_digest(scope)
    context_digest = intelligence_input_digest(context)
    policy_digest = intelligence_input_digest(policy)
    schema_digest = intelligence_input_digest(output_schema)
    key_document: dict[str, Any] = {
        "revision_id": revision_id,
        "snapshot_identity": snapshot_identity,
        "capability": capability,
        "scope_digest": scope_digest,
        "context_digest": context_digest,
        "policy_digest": policy_digest,
        "schema_digest": schema_digest,
        "driver": driver,
        "model": model,
        "prompt_version": int(prompt_version),
        "schema_version": int(schema_version),
    }
    if content_access_digest:
        key_document["content_access_digest"] = content_access_digest
    run_key = intelligence_input_digest(key_document)
    return IntelligenceIdentity(
        run_key=run_key,
        scope_digest=scope_digest,
        context_digest=context_digest,
        policy_digest=policy_digest,
        schema_digest=schema_digest,
    )


_PRIVATE_REASONING_KEYS = {
    "chain_of_thought",
    "chainofthought",
    "hidden_reasoning",
    "private_reasoning",
    "internal_reasoning",
    "scratchpad",
}


def _public_structured_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.lower().replace("-", "_").replace(" ", "_")
            if normalized in _PRIVATE_REASONING_KEYS:
                continue
            cleaned[key] = _public_structured_value(item)
        return cleaned
    if isinstance(value, list):
        return [_public_structured_value(item) for item in value]
    if isinstance(value, tuple):
        return [_public_structured_value(item) for item in value]
    return value


def _usage_json(
    usage: ModelCallUsage,
    *,
    duration_ms: int,
    identity: ModelIdentity | None = None,
    fresh_action_id: str = "",
    configured: RouteRequest | None = None,
) -> str:
    from lemoncrow.pro.capabilities.review.fresh_rerun import execution_document

    document: dict[str, Any] = {"usage": usage.to_wire(), "duration_ms": max(0, int(duration_ms))}
    if identity is not None:
        document["identity"] = identity.to_wire()
        document["execution"] = execution_document(fresh_action_id)
    if configured is not None:
        # The raw requested identity; the display allowlist applies on the wire only.
        document["configured_route"] = configured.to_record()
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def run_identity(row: Any) -> ModelIdentity:
    """The requested/observed identity persisted on one run row.

    Rows written before P11 carry no identity document; they read as their
    requested identity (pinned, or the unreported default), never as observed.
    """

    from lemoncrow.pro.capabilities.review.fresh_rerun import usage_document

    return ModelIdentity.from_wire(
        usage_document(row).get("identity"),
        driver=str(getattr(row, "driver", "") or ""),
        requested_model=str(getattr(row, "model", "") or ""),
    )


def run_configured_route(row: Any) -> RouteRequest | None:
    """The configured/requested route one run recorded (``None``: recorded before BI0, or without a policy)."""

    from lemoncrow.pro.capabilities.review.fresh_rerun import usage_document
    from lemoncrow.pro.capabilities.review.intelligence_policy import RouteRequest

    return RouteRequest.from_record(usage_document(row).get("configured_route"))


def _outcome_from_run(row: Any, *, cache_hit: bool, provider_calls: int = 0) -> IntelligenceRunOutcome:
    from lemoncrow.pro.capabilities.review.fresh_rerun import run_execution

    try:
        result_value = json.loads(str(row.result_json or "{}"))
    except (TypeError, json.JSONDecodeError):
        result_value = {}
    result = dict(result_value) if isinstance(result_value, Mapping) else {}
    try:
        usage_value = json.loads(str(row.usage_json or "{}"))
    except (TypeError, json.JSONDecodeError):
        usage_value = {}
    usage_mapping = usage_value.get("usage") if isinstance(usage_value, Mapping) else None
    usage = ModelCallUsage.from_wire(usage_mapping) if isinstance(usage_mapping, Mapping) else ModelCallUsage()
    duration_raw = usage_value.get("duration_ms") if isinstance(usage_value, Mapping) else 0
    duration_ms = (
        int(duration_raw) if isinstance(duration_raw, (int, float)) and not isinstance(duration_raw, bool) else 0
    )
    return IntelligenceRunOutcome(
        run_id=str(row.id),
        status=cast(Literal["pending", "ready", "failed", "cancelled"], row.status),
        result=result,
        driver=str(row.driver),
        model=str(row.model),
        usage=usage,
        provider_calls_this_request=provider_calls,
        duration_ms=max(0, duration_ms),
        cache_hit=cache_hit,
        error=str(row.error or ""),
        identity=run_identity(row),
        execution_mode=run_execution(row)["mode"],
        configured=run_configured_route(row),
    )


def _wait_for_settled_run(
    store: Any,
    *,
    review_id: str,
    run_key: str,
    timeout_s: float,
    cancellation: CancellationCheck | None,
) -> IntelligenceRunOutcome:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _cancelled(cancellation):
            current = store.get_intelligence_run(review_id, run_key)
            return IntelligenceRunOutcome(
                run_id="" if current is None else current.id,
                status="cancelled",
                result={},
                driver="" if current is None else current.driver,
                model="" if current is None else current.model,
                usage=ModelCallUsage(),
                provider_calls_this_request=0,
                duration_ms=0,
                cache_hit=False,
                error="cancelled while waiting for an identical intelligence run",
            )
        current = store.get_intelligence_run(review_id, run_key)
        if current is None:
            break
        if current.status != "pending":
            return _outcome_from_run(current, cache_hit=current.status == "ready")
        time.sleep(0.01)
    current = store.get_intelligence_run(review_id, run_key)
    if current is None:
        return IntelligenceRunOutcome(
            run_id="",
            status="failed",
            result={},
            driver="",
            model="",
            usage=ModelCallUsage(),
            provider_calls_this_request=0,
            duration_ms=0,
            cache_hit=False,
            error="intelligence run disappeared while waiting",
        )
    return _outcome_from_run(current, cache_hit=False)


def run_intelligence(
    *,
    store: Any,
    request: IntelligenceRunRequest,
    executor: Callable[[], StructuredExecution],
    cancellation: CancellationCheck | None = None,
) -> IntelligenceRunOutcome:
    """Claim, budget, execute and durably publish one intelligence result.

    Every returned outcome carries *this* request's configured route, even a
    cache hit or a run another request settled (the requested choice is not
    part of the run key, so a shared row may have recorded a different one).
    """

    resolved = resolve_driver_model(
        request.driver,
        request.model,
        **({} if request.route_policy is None else {"policy": request.route_policy}),
    )
    configured: RouteRequest | None = None
    if request.route_policy is not None:
        from lemoncrow.pro.capabilities.review.intelligence_policy import route_request

        configured = route_request(request.driver, request.model, request.route_policy)
    outcome = _run_intelligence(
        store=store, request=request, executor=executor, cancellation=cancellation, resolved=resolved
    )
    return replace(outcome, configured=configured)


def _content_access_refusal(request: IntelligenceRunRequest, resolved: ResolvedDriver) -> str:
    """Why the bound content-access policy refuses this run (empty: it may run)."""

    from lemoncrow.pro.capabilities.review.data_governance import route_governance
    from lemoncrow.pro.capabilities.review.intelligence_policy import ContentAccessRefused

    binding = request.content_access
    if binding is None:
        return ""
    trust = route_governance(resolved.driver).trust
    if binding.route_trust != trust:
        # A binding evaluated for another route's trust says nothing about this one.
        return (
            f"the content-access binding was evaluated for route trust {binding.route_trust!r}, "
            f"but the resolved route has trust {trust!r}"
        )
    try:
        binding.require(request.content_paths)
    except ContentAccessRefused as exc:
        return str(exc)
    except (TypeError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


def _run_intelligence(
    *,
    store: Any,
    request: IntelligenceRunRequest,
    executor: Callable[[], StructuredExecution],
    cancellation: CancellationCheck | None,
    resolved: ResolvedDriver,
) -> IntelligenceRunOutcome:
    from lemoncrow.pro.capabilities.review.session_models import ReviewIntelligenceRun

    binding = request.content_access
    # Before any claim or executor: no byte of a denied path reaches a model.
    refusal = _content_access_refusal(request, resolved)
    if refusal:
        return IntelligenceRunOutcome(
            run_id="",
            status="failed",
            result={},
            driver=resolved.driver,
            model=resolved.model,
            usage=ModelCallUsage(),
            provider_calls_this_request=0,
            duration_ms=0,
            cache_hit=False,
            error=f"AI content access refused: {refusal}"[:4000],
        )
    identity = build_intelligence_identity(
        revision_id=request.revision_id,
        snapshot_identity=request.snapshot_identity,
        capability=request.capability,
        scope=request.scope,
        context=request.context,
        policy=request.policy,
        driver=resolved.driver,
        model=resolved.model,
        output_schema=request.output_schema,
        prompt_version=request.prompt_version,
        schema_version=request.schema_version,
        content_access_digest=binding.digest if binding is not None else "",
    )
    configured: RouteRequest | None = None
    if request.route_policy is not None:
        from lemoncrow.pro.capabilities.review.intelligence_policy import route_request

        configured = route_request(request.driver, request.model, request.route_policy)
    pending = ReviewIntelligenceRun(
        review_id=request.review_id,
        id="",
        revision_id=request.revision_id,
        snapshot_identity=request.snapshot_identity,
        capability=request.capability,
        run_key=identity.run_key,
        scope_digest=identity.scope_digest,
        context_digest=identity.context_digest,
        policy_digest=identity.policy_digest,
        schema_digest=identity.schema_digest,
        driver=resolved.driver,
        model=resolved.model,
        prompt_version=request.prompt_version,
        schema_version=request.schema_version,
        status="pending",
    )
    fresh_action_id = request.fresh_action_id
    if fresh_action_id:
        from lemoncrow.pro.capabilities.review.fresh_rerun import prior_fresh_run

        # A replayed fresh action reuses what its first delivery produced.
        replay = prior_fresh_run(
            store,
            review_id=request.review_id,
            revision_id=request.revision_id,
            capability=request.capability,
            run_key=identity.run_key,
            action_id=fresh_action_id,
        )
        if replay is not None:
            return _outcome_from_run(replay, cache_hit=True)
    row, claimed = store.claim_intelligence_run(
        pending,
        stale_after_s=max(60.0, request.budget.timeout_s * 2.0),
        bypass_cache=bool(fresh_action_id),
    )
    if not claimed:
        if row.status == "ready":
            return _outcome_from_run(row, cache_hit=True)
        return _wait_for_settled_run(
            store,
            review_id=request.review_id,
            run_key=identity.run_key,
            timeout_s=request.budget.timeout_s,
            cancellation=cancellation,
        )
    started = time.monotonic()
    attempts = 0
    requested_identity = ModelIdentity(driver=resolved.driver, requested_model=resolved.model)

    def finish(
        status: Literal["ready", "failed", "cancelled"],
        *,
        result: Mapping[str, Any] | None = None,
        usage: ModelCallUsage | None = None,
        error: str = "",
        observed: ModelIdentity | None = None,
    ) -> IntelligenceRunOutcome:
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        public_result = _public_structured_value(dict(result or {}))
        safe_result = dict(public_result) if isinstance(public_result, Mapping) else {}
        final_usage = usage or ModelCallUsage(calls=attempts)
        store.complete_intelligence_run(
            request.review_id,
            row.id,
            status=status,
            result_json=json.dumps(
                safe_result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            usage_json=_usage_json(
                final_usage,
                duration_ms=elapsed_ms,
                identity=observed or requested_identity,
                fresh_action_id=fresh_action_id,
                configured=configured,
            ),
            error=error[:4000],
            model_calls=attempts,
        )
        settled = store.get_intelligence_run(request.review_id, identity.run_key)
        if settled is None or settled.id != row.id:
            # A concurrent fresh re-run may be newer; this request reports its own row.
            settled = next(
                (
                    item
                    for item in store.list_intelligence_runs(
                        request.review_id, revision_id=request.revision_id, capability=request.capability
                    )
                    if item.id == row.id
                ),
                settled,
            )
        if settled is None:
            raise RuntimeError("completed Review Intelligence run is missing")
        return _outcome_from_run(settled, cache_hit=False, provider_calls=attempts)

    if _cancelled(cancellation):
        return finish("cancelled", error="Review Intelligence run was cancelled")
    if request.budget.max_calls <= 0:
        return finish("failed", error="Review Intelligence model-call budget exhausted")
    if request.budget.max_cost is not None and resolved.driver in {"claude", "codex", "opencode", "ollama"}:
        return finish(
            "failed",
            error=f"{resolved.driver} does not expose enforceable provider cost metadata for this cost budget",
        )

    last_error = ""
    for attempt in range(request.budget.max_calls):
        if _cancelled(cancellation):
            return finish("cancelled", error="Review Intelligence run was cancelled")
        if not store.reserve_intelligence_call(request.review_id, limit=request.budget.max_review_calls):
            return finish("failed", error="Review Intelligence Review-wide model-call budget exhausted")
        attempts += 1
        try:
            execution = executor()
        except IntelligenceCancelled:
            return finish("cancelled", error="Review Intelligence run was cancelled")
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < request.budget.max_calls:
                continue
            return finish("failed", error=last_error)

        if execution.driver != resolved.driver or execution.model != resolved.model:
            return finish("failed", error="Review Intelligence executor returned a different driver/model identity")
        observed_models, observed_truncated = observed_from(execution.observed_models)
        executed_identity = replace(
            requested_identity, observed_models=observed_models, observed_truncated=observed_truncated
        )

        execution_usage = execution.usage
        if attempts > 1:
            execution_usage = ModelCallUsage(calls=attempts, request_id=execution_usage.request_id)
        else:
            execution_usage = ModelCallUsage(
                calls=attempts,
                input_tokens=execution_usage.input_tokens,
                output_tokens=execution_usage.output_tokens,
                cache_read_input_tokens=execution_usage.cache_read_input_tokens,
                cache_write_input_tokens=execution_usage.cache_write_input_tokens,
                provider_cost=execution_usage.provider_cost,
                currency=execution_usage.currency,
                request_id=execution_usage.request_id,
            )

        if request.budget.max_cost is not None:
            if execution_usage.provider_cost is None:
                return finish(
                    "failed",
                    usage=execution_usage,
                    error="provider did not report cost; requested cost budget cannot be verified",
                    observed=executed_identity,
                )
            if execution_usage.provider_cost > request.budget.max_cost:
                return finish(
                    "failed",
                    usage=execution_usage,
                    error="Review Intelligence provider cost budget exceeded",
                    observed=executed_identity,
                )
        return finish("ready", result=execution.result, usage=execution_usage, observed=executed_identity)

    return finish("failed", error=last_error or "Review Intelligence model-call budget exhausted")


__all__ = [
    "AGENT_CLI_DRIVERS",
    "LLM_BACKEND_ENV",
    "AgentCliDriverRefused",
    "CancellationCheck",
    "IntelligenceCancelled",
    "IntelligenceDisabled",
    "IntelligenceIdentity",
    "IntelligenceRunOutcome",
    "IntelligenceRunRequest",
    "ModelCallBudget",
    "ModelCallUsage",
    "ResolvedDriver",
    "ReviewIntelligenceCapability",
    "ReviewIntelligenceDriver",
    "RouteOverrideRefused",
    "StructuredExecution",
    "build_intelligence_identity",
    "execute_structured",
    "intelligence_input_digest",
    "require_intelligence_enabled",
    "resolve_driver_model",
    "run_configured_route",
    "run_identity",
    "run_intelligence",
]
