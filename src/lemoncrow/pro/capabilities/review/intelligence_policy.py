"""The effective intelligence route policy (BI0): driver, model, run-on and background enablement.

One typed policy every intelligence consumer resolves its model route from:
Review Intelligence, Ask, and the background capabilities that later packages
add (BI1 budgeting, BI2 scheduling, PK1 narration, DS0 summaries). It is a
seam, not a runtime: execution still goes through
``intelligence_runtime.resolve_driver_model``/``execute_structured``, which
accept the policy, and nothing here budgets, schedules or calls a model.

.. code-block:: yaml

    enabled: true       # operator layers only; a person sets the AI budget instead
    driver: auto        # auto | claude | codex | opencode | ollama | openai | litellm
    model: auto         # auto | an explicit provider model id
    run_on: auto        # auto | local | worker | managed (as the deployment allows)
    background:
      enabled: true

Layers resolve low to high (:data:`POLICY_LAYER_ORDER`); every effective value
carries the layer and setting that set it. Rules:

* ``driver: auto`` resolves the deployment's configured route
  (``LEMONCROW_LLM_BACKEND``): environment variables stay compatibility and
  deployment inputs, read only when the policy leaves the choice to them.
* ``model: auto`` leaves the model to the driver: its configured default, or a
  subscription CLI's own default, which is recorded as unreported and never
  guessed.
* The configured (requested) choice, the resolved route and the models a
  provider observed are separate identities (:class:`RouteRequest`,
  ``ModelIdentity`` requested/observed).
* Route trust and data locus are RC6's operator attestation
  (``data_governance.route_governance``), never inferred from a hostname or a
  driver name.
* A policy that cannot be parsed attests nothing: the route is ``invalid`` and
  no model runs. A content-access policy that cannot be parsed denies every
  path (:class:`ContentAccessBinding`), before any byte reaches a model.
* No credential belongs in this policy; the wire projection shows model ids
  only in RC6's allowlisted shape and never prints an environment value.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, cast, get_args

from lemoncrow.infra.internal_llm import InternalLLMError
from lemoncrow.pro.capabilities.review.data_governance import (
    DataGovernanceAttestations,
    RouteGovernance,
    route_governance,
    shown_model_name,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import (
    LLM_BACKEND_ENV,
    AgentCliDriverRefused,
    IntelligenceDisabled,
    ReviewIntelligenceDriver,
    RouteOverrideRefused,
    operator_off_reason,
    resolve_driver_model,
)
from lemoncrow.pro.capabilities.review.model_identity import ModelIdentity, clean_model_name
from lemoncrow.pro.capabilities.review.rules import (
    AUTHORITATIVE_RULE_TRUST,
    CONTENT_ACCESS_MODES,
    ContentAccessDecision,
    ContentAccessRule,
    ModelRouteTrust,
    RuleTrustClass,
    _safe_path,
    content_access_for_path,
)

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.rules import ContentAccessMode

AUTO: Final[str] = "auto"
DRIVERS: Final[tuple[str, ...]] = get_args(ReviewIntelligenceDriver)
DRIVER_CHOICES: Final[tuple[str, ...]] = (AUTO, *DRIVERS)
RunOn = Literal["local", "worker", "managed"]
RUN_ON_LOCI: Final[tuple[str, ...]] = get_args(RunOn)
RUN_ON_CHOICES: Final[tuple[str, ...]] = (AUTO, *RUN_ON_LOCI)

PolicyLayerName = Literal["built_in_default", "settings", "organization", "repository", "request"]
#: Low-to-high precedence. ``settings`` is the process's LemonCrow settings file
#: (a user's, locally); a hosted process reads none (:func:`use_operator_intelligence_policy`)
#: and takes ``organization``/``repository`` from the operator Review policy.
POLICY_LAYER_ORDER: Final[tuple[str, ...]] = get_args(PolicyLayerName)
#: Operator layers: a driver or model they pin bounds every request (no caller override).
OPERATOR_LAYERS: Final[frozenset[str]] = frozenset({"organization", "repository"})
OPERATOR_POLICY_SOURCE: Final[str] = "operator_review_policy"
#: What ``run_on`` and ``background.enabled`` govern today (BI2, PK1, DS0): automatic AI Review,
#: local Project Knowledge narration and local refreshes of summarized discussion threads.
BACKGROUND_NOTE: Final[str] = (
    "governs background work: automatic AI Review, local Project Knowledge narration and local "
    "refreshes of summarized discussion threads"
)
#: ``ready`` is a configuration fact, not a credential check.
READY_NOTE: Final[str] = (
    "ready means a model route is configured; provider credentials are checked on the first model call"
)

#: The settings key of each policy field (``lemoncrow settings set <key> <value>``). ``enabled`` has none: no
#: person switches AI off (SPEC §7.1 OWNER-1). Only an operator layer carries it, and the personal control is the
#: monthly AI budget (:mod:`ai_budget`).
SETTINGS_KEYS: Final[Mapping[str, str]] = {
    "driver": "intelligence.driver",
    "model": "intelligence.model",
    "run_on": "intelligence.run_on",
    "background_enabled": "intelligence.background.enabled",
}
#: The policy document's own key for each field (operator documents use this shape).
_DOCUMENT_KEYS: Final[Mapping[str, str]] = {
    "enabled": "enabled",
    "driver": "driver",
    "model": "model",
    "run_on": "run_on",
    "background_enabled": "background.enabled",
}
#: The model setting each configurable driver reads when the policy says ``model: auto``.
DRIVER_MODEL_SETTINGS: Final[Mapping[str, str]] = {
    "openai": "LEMONCROW_OPENAI_MODEL",
    "litellm": "LEMONCROW_LITELLM_MODEL",
    "ollama": "LEMONCROW_OLLAMA_MODEL",
}
MODEL_WITHHELD: Final[str] = "(model id withheld: it is not a plain model identifier)"


class IntelligencePolicyError(ValueError):
    """A policy value outside the vocabulary. The message names the field, never the value."""


class ContentAccessRefused(InternalLLMError):
    """Content-access policy denies (or cannot decide) model access; raised before any byte is read."""


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PolicyOrigin:
    """Which layer set a value, and the setting *name* it set (never the value)."""

    layer: str
    source: str
    scope_id: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"layer": self.layer, "source": self.source, "scope_id": self.scope_id}

    @classmethod
    def from_wire(cls, raw: object) -> PolicyOrigin:
        if not isinstance(raw, Mapping):
            return BUILT_IN_ORIGIN
        return cls(str(raw.get("layer") or ""), str(raw.get("source") or ""), str(raw.get("scope_id") or ""))


BUILT_IN_ORIGIN: Final[PolicyOrigin] = PolicyOrigin("built_in_default", "built_in_default")


def _driver(value: object, field: str) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    text = "openai" if text == "openai_compatible" else text
    if text not in DRIVER_CHOICES:
        raise IntelligencePolicyError(f"{field} must be one of {', '.join(DRIVER_CHOICES)}")
    return text


def _model(value: object, field: str) -> str:
    if isinstance(value, str) and value.strip().lower() == AUTO:
        return AUTO
    model = clean_model_name(value)
    if not model:
        raise IntelligencePolicyError(f"{field} must be auto or a plain model identifier")
    return model


def _run_on(value: object, field: str) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if text not in RUN_ON_CHOICES:
        raise IntelligencePolicyError(f"{field} must be one of {', '.join(RUN_ON_CHOICES)}")
    return text


def _flag(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise IntelligencePolicyError(f"{field} must be true or false")
    return value


@dataclass(frozen=True, slots=True)
class IntelligencePolicyLayer:
    """One layer's explicit values; ``None`` inherits the layer below."""

    origin: PolicyOrigin
    enabled: bool | None = None
    driver: str | None = None
    model: str | None = None
    run_on: str | None = None
    background_enabled: bool | None = None
    #: Per-field setting names this layer read (default: the policy document key).
    sources: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.origin.layer not in POLICY_LAYER_ORDER:
            raise IntelligencePolicyError(f"unknown intelligence policy layer {self.origin.layer!r}")
        if self.enabled is not None:
            _flag(self.enabled, "enabled")
        if self.driver is not None and _driver(self.driver, "driver") != self.driver:
            raise IntelligencePolicyError("driver must be normalized")
        if self.model is not None and _model(self.model, "model") != self.model:
            raise IntelligencePolicyError("model must be normalized")
        if self.run_on is not None and _run_on(self.run_on, "run_on") != self.run_on:
            raise IntelligencePolicyError("run_on must be normalized")
        if self.background_enabled is not None:
            _flag(self.background_enabled, "background.enabled")

    def origin_for(self, name: str) -> PolicyOrigin:
        source = (self.sources or {}).get(name) or _DOCUMENT_KEYS[name]
        return PolicyOrigin(self.origin.layer, source, self.origin.scope_id)


def parse_intelligence_policy(raw: object, *, origin: PolicyOrigin) -> IntelligencePolicyLayer:
    """One strict policy document (operator layers); raises :class:`IntelligencePolicyError`.

    Unknown keys and values outside the vocabulary are refused, never ignored:
    a policy that cannot be read must not silently become ``auto``.
    """

    if not isinstance(raw, Mapping):
        raise IntelligencePolicyError("intelligence policy must be an object")
    unknown = sorted(str(key) for key in raw if key not in {"enabled", "driver", "model", "run_on", "background"})
    if unknown:
        raise IntelligencePolicyError("intelligence policy may only set enabled, driver, model, run_on, background")
    background = raw.get("background")
    if background is not None and (not isinstance(background, Mapping) or set(background) - {"enabled"}):
        raise IntelligencePolicyError("background may only set enabled")
    return IntelligencePolicyLayer(
        origin=origin,
        enabled=_flag(raw["enabled"], "enabled") if "enabled" in raw else None,
        driver=_driver(raw["driver"], "driver") if "driver" in raw else None,
        model=_model(raw["model"], "model") if "model" in raw else None,
        run_on=_run_on(raw["run_on"], "run_on") if "run_on" in raw else None,
        background_enabled=(
            _flag(background["enabled"], "background.enabled")
            if isinstance(background, Mapping) and "enabled" in background
            else None
        ),
    )


def settings_policy_layer(raw: Mapping[str, Any]) -> tuple[IntelligencePolicyLayer, tuple[str, ...]]:
    """The ``intelligence.*`` keys of a LemonCrow settings file as one layer, plus its errors.

    A stored value outside the vocabulary is an error (naming the key, never
    the value), so the effective policy is ``invalid`` instead of quietly
    falling back to a default the user did not choose.
    """

    values: dict[str, Any] = {}
    errors: list[str] = []

    def flag(value: object, key: str) -> bool:
        return _flag(_settings_bool(value, key), key)

    parsers: dict[str, Callable[[Any, str], Any]] = {
        "driver": _driver,
        "model": _model,
        "run_on": _run_on,
        "background_enabled": flag,
    }
    for name, key in SETTINGS_KEYS.items():
        if key not in raw:
            continue
        if raw[key] is None or (isinstance(raw[key], str) and not raw[key].strip()):
            # A stored empty value is not "unset": it never reads as the default (on).
            errors.append(f"{key} is empty; set a value or remove the setting")
            continue
        try:
            values[name] = parsers[name](raw[key], key)
        except IntelligencePolicyError as exc:
            errors.append(str(exc))
    layer = IntelligencePolicyLayer(
        origin=PolicyOrigin("settings", "lemoncrow settings"),
        sources={name: SETTINGS_KEYS[name] for name in values},
        **values,
    )
    return layer, tuple(errors)


def _settings_bool(value: object, key: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise IntelligencePolicyError(f"{key} must be true or false")


@dataclass(frozen=True, slots=True)
class EffectiveSetting:
    value: Any
    origin: PolicyOrigin


@dataclass(frozen=True, slots=True)
class EffectiveIntelligencePolicy:
    """The resolved policy: each value with the layer and setting that set it."""

    enabled: EffectiveSetting
    driver: EffectiveSetting
    model: EffectiveSetting
    run_on: EffectiveSetting
    background_enabled: EffectiveSetting
    #: Why the policy is invalid (an invalid policy runs no model). Names keys, never values.
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def digest(self) -> str:
        """A digest of the effective values (not their origins): moving a value between layers is no change."""

        document = {
            "enabled": self.enabled.value,
            "driver": self.driver.value,
            "model": self.model.value,
            "run_on": self.run_on.value,
            "background_enabled": self.background_enabled.value,
            "valid": self.valid,
        }
        return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def to_wire(self) -> dict[str, Any]:
        model = str(self.model.value)
        shown = model if model == AUTO else shown_model_name(model) or MODEL_WITHHELD
        return {
            "enabled": _setting_wire(self.enabled.value, self.enabled.origin),
            "driver": _setting_wire(self.driver.value, self.driver.origin),
            "model": _setting_wire(shown, self.model.origin),
            "run_on": _setting_wire(self.run_on.value, self.run_on.origin),
            "background": {"enabled": _setting_wire(self.background_enabled.value, self.background_enabled.origin)},
            "valid": self.valid,
            "errors": list(self.errors),
            "precedence": list(POLICY_LAYER_ORDER),
        }


def _setting_wire(value: Any, origin: PolicyOrigin) -> dict[str, Any]:
    return {"effective_value": value, "origin": origin.to_wire()}


DEFAULT_INTELLIGENCE_POLICY: Final[EffectiveIntelligencePolicy] = EffectiveIntelligencePolicy(
    enabled=EffectiveSetting(True, BUILT_IN_ORIGIN),
    driver=EffectiveSetting(AUTO, BUILT_IN_ORIGIN),
    model=EffectiveSetting(AUTO, BUILT_IN_ORIGIN),
    run_on=EffectiveSetting(AUTO, BUILT_IN_ORIGIN),
    background_enabled=EffectiveSetting(True, BUILT_IN_ORIGIN),
)


def resolve_intelligence_policy(
    layers: Iterable[IntelligencePolicyLayer | None], *, errors: Sequence[str] = ()
) -> EffectiveIntelligencePolicy:
    """Apply *layers* over the built-in default, lowest first (:data:`POLICY_LAYER_ORDER`).

    A pinned model needs a pinned driver: ``model: <id>`` under ``driver: auto``
    is invalid, so a model id chosen for one provider is never sent to another.
    """

    current = {
        name: getattr(DEFAULT_INTELLIGENCE_POLICY, name)
        for name in ("enabled", "driver", "model", "run_on", "background_enabled")
    }
    rank = -1
    for layer in layers:
        if layer is None:
            continue
        position = POLICY_LAYER_ORDER.index(layer.origin.layer)
        if position < rank:
            raise ValueError("intelligence policy layers must be supplied lowest precedence first")
        rank = position
        for name in current:
            value = getattr(layer, name)
            if value is not None:
                current[name] = EffectiveSetting(value, layer.origin_for(name))
    found = list(errors)
    if current["model"].value != AUTO and current["driver"].value == AUTO:
        found.append(f"a pinned model ({current['model'].origin.source}) requires a pinned driver; driver is auto")
    return EffectiveIntelligencePolicy(**current, errors=tuple(found))


#: Process-wide: whether this process reads the LemonCrow settings file into its
#: policy. A hosted process turns it off (one-way) before it serves.
_settings_layer_enabled = True


def use_operator_intelligence_policy() -> None:
    """This process never reads a settings file into its intelligence policy (one-way).

    The hosted control plane and its managed workers call this: their effective
    policy is the built-in default under the operator Review policy's
    ``intelligence`` layers (:func:`review_intelligence_policy`), the same on
    every replica, never one replica's ``plugin_settings.json``.
    """

    global _settings_layer_enabled
    _settings_layer_enabled = False


def settings_layer_enabled() -> bool:
    return _settings_layer_enabled


def _settings_document(path: Path) -> tuple[Mapping[str, Any], str]:
    """The settings file's keys, or an error naming the file (never its content).

    A missing file is the defaults. A file that exists but cannot be read, is
    not JSON, is not an object, or uses the nested ``intelligence`` shape is an
    error: a stored value must never quietly read as the default.
    """

    if not path.exists():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}, f"the LemonCrow settings file {path} could not be read"
    except ValueError:
        return {}, f"the LemonCrow settings file {path} is not valid JSON"
    if not isinstance(data, Mapping):
        return {}, f"the LemonCrow settings file {path} must be a JSON object"
    merged = dict(data)
    nested = data.get("lemoncrow")
    if isinstance(nested, Mapping):  # the settings framework's own nested section (``core.settings``)
        merged.update(nested)
    if "intelligence" in merged:
        return {}, (
            f"the LemonCrow settings file {path} sets a nested 'intelligence' object; "
            "use the keys intelligence.driver, intelligence.model, ..."
        )
    return merged, ""


def _process_layers(root: Path | None = None) -> tuple[tuple[IntelligencePolicyLayer, ...], tuple[str, ...]]:
    if not _settings_layer_enabled:
        return (), ()
    from lemoncrow.core.settings import settings_path
    from lemoncrow.pro.capabilities.review.ai_budget import migrate_legacy_switch
    from lemoncrow.pro.capabilities.review.fix_agent import settings_root

    base = root if root is not None else settings_root()
    migrate_legacy_switch(base)  # a retired intelligence.enabled: false becomes a $0 budget (idempotent)
    raw, error = _settings_document(settings_path(base))
    if error:
        return (), (error,)
    layer, errors = settings_policy_layer(raw)
    return (layer,), errors


def process_intelligence_policy(root: Path | None = None) -> EffectiveIntelligencePolicy:
    """This process's policy: the built-in default under its LemonCrow settings file.

    Read fresh on every call (one small JSON file), like the preferred fixing
    agent, so ``lemoncrow settings set intelligence.*`` applies without a
    restart. A settings file that exists but cannot be read makes the policy
    invalid (no model call), never the default. A hosted process reads no
    settings file (:func:`use_operator_intelligence_policy`); callers holding
    an operator policy pass :func:`review_intelligence_policy` instead.
    """

    layers, errors = _process_layers(root)
    return resolve_intelligence_policy(layers, errors=errors)


def _operator_layer(raw: object, index: int) -> IntelligencePolicyLayer:
    if not isinstance(raw, Mapping) or set(raw) - {"origin", "settings"}:
        raise IntelligencePolicyError(f"intelligence.layers[{index}] must be an object of origin and settings")
    origin = raw.get("origin")
    layer = origin.get("layer") if isinstance(origin, Mapping) else None
    if layer not in OPERATOR_LAYERS:
        raise IntelligencePolicyError(f"intelligence.layers[{index}] must be an organization or repository layer")
    scope_id = str(origin.get("scope_id") or "") if isinstance(origin, Mapping) else ""
    parsed = parse_intelligence_policy(
        raw.get("settings"), origin=PolicyOrigin(str(layer), OPERATOR_POLICY_SOURCE, scope_id)
    )
    sources = {name: OPERATOR_POLICY_SOURCE for name in _DOCUMENT_KEYS}
    return IntelligencePolicyLayer(
        origin=parsed.origin,
        enabled=parsed.enabled,
        driver=parsed.driver,
        model=parsed.model,
        run_on=parsed.run_on,
        background_enabled=parsed.background_enabled,
        sources=sources,
    )


def review_intelligence_policy(effective: object) -> EffectiveIntelligencePolicy | None:
    """The policy an effective Review policy document carries, or ``None`` when it carries none.

    *effective* is the Review API's policy (``{"effective_review": {...}}``) or
    the effective Review document itself. Its ``intelligence`` section (the
    operator Review policy's organization and repository layers) applies over
    this process's policy; a malformed section makes the policy invalid. With
    no section the caller keeps this process's policy (``None``).
    """

    document = effective.get("effective_review", effective) if isinstance(effective, Mapping) else None
    if not isinstance(document, Mapping) or "intelligence" not in document:
        return None
    base, errors = _process_layers()
    section = document["intelligence"]
    layers: list[IntelligencePolicyLayer] = []
    try:
        rows = section.get("layers") if isinstance(section, Mapping) else None
        if not isinstance(rows, list) or set(section) - {"layers"}:
            raise IntelligencePolicyError("the intelligence section must be an object with a layers list")
        layers = [_operator_layer(row, index) for index, row in enumerate(rows)]
        return resolve_intelligence_policy((*base, *layers), errors=errors)
    except (IntelligencePolicyError, ValueError) as exc:
        return resolve_intelligence_policy(base, errors=(*errors, f"operator intelligence policy: {exc}"))


def intelligence_policy_digest(effective: object) -> str:
    """The digest a durable job binds: the operator section's policy, else this process's.

    ``""`` for the built-in default, so documents and digests that predate the
    policy keep their identity.
    """

    policy = review_intelligence_policy(effective) or process_intelligence_policy()
    return "" if policy.digest == DEFAULT_INTELLIGENCE_POLICY.digest else policy.digest


# --------------------------------------------------------------------------- #
# route
# --------------------------------------------------------------------------- #

RouteStatus = Literal["ready", "disabled", "not_configured", "refused", "invalid"]


def _shown_model(model: str) -> str:
    """RC6's display allowlist, applied only when a model id is projected to a wire or UI."""

    return model if model in {AUTO, ""} else shown_model_name(model) or MODEL_WITHHELD


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """The configured/requested route: ``auto`` is a meaningful value, never replaced by a guess.

    :meth:`to_record` is the persisted identity (the raw model id, as asked);
    :meth:`to_wire` is its display projection (RC6's allowlist).
    """

    driver: str
    model: str
    driver_origin: PolicyOrigin = BUILT_IN_ORIGIN
    model_origin: PolicyOrigin = BUILT_IN_ORIGIN

    def to_record(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "model": self.model,
            "driver_origin": self.driver_origin.to_wire(),
            "model_origin": self.model_origin.to_wire(),
        }

    def to_wire(self) -> dict[str, Any]:
        return {**self.to_record(), "model": _shown_model(self.model)}

    @classmethod
    def from_record(cls, raw: object) -> RouteRequest | None:
        if not isinstance(raw, Mapping):
            return None
        driver = raw.get("driver")
        model = raw.get("model")
        if driver not in DRIVER_CHOICES or not isinstance(model, str):
            return None
        return cls(
            driver=str(driver),
            model=model,
            driver_origin=PolicyOrigin.from_wire(raw.get("driver_origin")),
            model_origin=PolicyOrigin.from_wire(raw.get("model_origin")),
        )


def route_request(
    driver: str | None, model: str | None, policy: EffectiveIntelligencePolicy | None = None
) -> RouteRequest:
    """What was asked for: the caller's explicit driver/model, else the policy's choices.

    An empty *model* is no model (exactly like ``None``), as in ``resolve_driver_model``.
    """

    effective = policy or DEFAULT_INTELLIGENCE_POLICY
    requested = (driver or "").strip().lower()
    requested = "openai" if requested == "openai_compatible" else requested
    model = (model or "").strip() or None
    request_origin = PolicyOrigin("request", "caller")
    if requested and requested not in {"configured", AUTO}:
        return RouteRequest(requested, model or AUTO, request_origin, request_origin)
    if model is not None:
        return RouteRequest(str(effective.driver.value), model, effective.driver.origin, request_origin)
    return RouteRequest(
        str(effective.driver.value), str(effective.model.value), effective.driver.origin, effective.model.origin
    )


@dataclass(frozen=True, slots=True)
class IntelligenceRoute:
    """One resolved route: requested choice, resolved driver/model, locus, trust and status.

    ``driver``/``model`` are the resolved route ``resolve_driver_model``
    returns (``model`` empty: the driver's own default, unreported). Observed
    models are recorded per run (``ModelIdentity.observed_models``), never here.
    ``governance`` is RC6's route projection: trust is the model-data locus.
    ``ready`` means configured (:data:`READY_NOTE`), not that credentials work.
    ``run_on`` is recorded with whether this deployment offers it; background
    scheduling honours it and never degrades a route (:data:`BACKGROUND_NOTE`).
    """

    status: RouteStatus
    requested: RouteRequest
    policy: EffectiveIntelligencePolicy
    driver: str = ""
    model: str = ""
    driver_source: PolicyOrigin = BUILT_IN_ORIGIN
    model_source: PolicyOrigin = BUILT_IN_ORIGIN
    run_on: str = ""
    run_on_origin: PolicyOrigin = BUILT_IN_ORIGIN
    governance: RouteGovernance | None = None
    reason: str = ""
    run_on_offered: bool = True

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def background_enabled(self) -> bool:
        return bool(self.policy.enabled.value) and bool(self.policy.background_enabled.value)

    @property
    def trust(self) -> ModelRouteTrust:
        """Where content goes (RC6): ``unknown`` unless a resolved route's trust is known."""

        return self.governance.trust if self.governance is not None else "unknown"

    @property
    def resolved_identity(self) -> ModelIdentity:
        return ModelIdentity(driver=self.driver, requested_model=self.model)

    def identity(self) -> dict[str, str]:
        """The route facts a model-derived result depends on (driver, model, trust).

        The requested choice is deliberately absent: ``auto`` and an explicit
        choice that resolve to the same route execute the same model, so they
        share results; any change of the resolved route changes this.
        """

        return {"driver": self.driver, "model": self.model, "trust": self.trust}

    def to_wire(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            **({"status_note": READY_NOTE} if self.ready else {}),
            "requested": self.requested.to_wire(),
            "resolved": {
                "driver": self.driver,
                "model": _shown_model(self.model),
                "model_source": "pinned" if self.model else ("unreported_default" if self.driver else ""),
                "driver_origin": self.driver_source.to_wire(),
                "model_origin": self.model_source.to_wire(),
            },
            "run_on": {
                "effective_value": self.run_on,
                "origin": self.run_on_origin.to_wire(),
                "offered_here": self.run_on_offered,
                "note": BACKGROUND_NOTE,
            },
            "background_enabled": self.background_enabled,
            "background_note": BACKGROUND_NOTE,
            "data_locus": self.governance.to_wire() if self.governance is not None else None,
            "policy": self.policy.to_wire(),
        }


def _model_source(driver: str, model: str, policy_model: EffectiveSetting, env_layer: str) -> PolicyOrigin:
    if policy_model.value != AUTO:
        return policy_model.origin
    setting = DRIVER_MODEL_SETTINGS.get(driver)
    if setting is not None and str(os.environ.get(setting) or "").strip():
        return PolicyOrigin(env_layer, setting)
    if setting is not None:
        return PolicyOrigin("built_in_default", setting)
    del model
    return PolicyOrigin("provider_default", f"{driver} CLI default model")


def resolve_intelligence_route(
    policy: EffectiveIntelligencePolicy | None = None,
    *,
    allowed_run_on: Sequence[str] = ("local",),
    default_run_on: str | None = None,
    governance: DataGovernanceAttestations | None = None,
    environment_layer: str = "environment",
) -> IntelligenceRoute:
    """Resolve *policy* (default: this process's) to the route this process would use.

    The driver/model come from ``resolve_driver_model`` itself, so the route a
    projection shows is the route execution takes; a refusal (agent CLI on a
    hosted control plane), a disabled or missing provider, or an invalid
    policy is an explicit status with its reason, never a silent fallback.
    """

    effective = policy if policy is not None else process_intelligence_policy()
    requested = route_request(None, None, effective)
    allowed = tuple(locus for locus in allowed_run_on if locus in RUN_ON_LOCI)
    run_on_choice = str(effective.run_on.value)
    if run_on_choice == AUTO:
        run_on = default_run_on if default_run_on in allowed else (allowed[0] if allowed else "")
        offered = bool(run_on)
    else:
        run_on, offered = run_on_choice, run_on_choice in allowed

    def degraded(status: RouteStatus, reason: str) -> IntelligenceRoute:
        return IntelligenceRoute(
            status,
            requested,
            effective,
            run_on=run_on,
            run_on_origin=effective.run_on.origin,
            reason=reason,
            run_on_offered=offered,
        )

    if not effective.valid:
        return degraded("invalid", "intelligence policy is invalid: " + "; ".join(effective.errors))
    if not effective.enabled.value:
        return degraded("disabled", operator_off_reason(effective))
    try:
        resolved = resolve_driver_model(None, None, policy=effective)
    except AgentCliDriverRefused:
        return degraded(
            "refused",
            "the configured route is an agent CLI, and agent CLIs do not run on this server; "
            "configure a model route (openai, litellm or ollama)",
        )
    except (IntelligenceDisabled, RouteOverrideRefused) as exc:
        return degraded("disabled", str(exc))
    except InternalLLMError:
        if effective.driver.value == AUTO and str(os.environ.get(LLM_BACKEND_ENV) or "none").strip().lower() in {
            "",
            "none",
        }:
            return degraded("not_configured", f"no intelligence provider is configured ({LLM_BACKEND_ENV})")
        return degraded("invalid", f"the configured provider is not a supported driver ({LLM_BACKEND_ENV})")
    driver_source = (
        effective.driver.origin if effective.driver.value != AUTO else PolicyOrigin(environment_layer, LLM_BACKEND_ENV)
    )
    return IntelligenceRoute(
        "ready",
        requested,
        effective,
        driver=resolved.driver,
        model=resolved.model,
        driver_source=driver_source,
        model_source=_model_source(resolved.driver, resolved.model, effective.model, environment_layer),
        run_on=run_on,
        run_on_origin=effective.run_on.origin,
        governance=route_governance(resolved.driver, attestations=governance),
        run_on_offered=offered,
    )


def ask_route_options(policy: EffectiveIntelligencePolicy | None = None) -> dict[str, str]:
    """The route an interactive Ask would take, for a driver/model picker.

    Derived from the effective policy (the same resolver execution uses), so
    the picker shows what will run: an explicit ``intelligence.driver`` wins
    over ``LEMONCROW_LLM_BACKEND``, and a disabled or invalid policy says so
    instead of naming a route. ``driver`` is ``configured`` when no route is
    ready, as before; ``model`` is the raw configured model id (this user's own
    configuration, shown as the environment did before).
    """

    route = resolve_intelligence_route(policy, allowed_run_on=RUN_ON_LOCI)
    if not route.ready:
        return {"driver": "configured", "model": "", "status": route.status, "reason": route.reason}
    return {"driver": route.driver, "model": route.model, "status": "ready", "reason": ""}


# --------------------------------------------------------------------------- #
# content access
# --------------------------------------------------------------------------- #

_RULE_KEYS: Final[frozenset[str]] = frozenset(
    {"mode", "paths", "origin", "trust_class", "source_identity", "source_location", "digest", "authoritative"}
)
_MALFORMED: Final[str] = "the AI content-access policy is malformed; model access is denied for every path"
_UNKNOWN_POLICY: Final[str] = (
    "no AI content-access policy was supplied (unknown policy); model access is denied for every path"
)
_UNDECLARED: Final[str] = (
    "the run did not declare which repository paths it sends to the model; declare them (or none) first"
)


@dataclass(frozen=True, slots=True)
class ContentAccessBinding:
    """The effective AI content-access policy for one route, decided before materialization.

    An invalid binding (a malformed or unknown policy) denies every path.
    ``digest`` covers the rules, the route trust and validity, so it belongs in
    the identity of any model-derived result built under it. ``route_trust``
    is the trust the rules were decided for; a run on a route of another trust
    refuses the binding (``intelligence_runtime.run_intelligence``).
    """

    # lc-debt: no product surface passes a binding to run_intelligence yet; each still
    # decides per path through rules.content_access_for_path before building its prompt.
    # Upgrade: bind once per surface (Ask, brief, tour, AI review) and declare content_paths.
    rules: tuple[ContentAccessRule, ...]
    route_trust: ModelRouteTrust
    valid: bool = True
    reason: str = ""

    @property
    def digest(self) -> str:
        document = {
            "valid": self.valid,
            "route_trust": self.route_trust,
            "rules": sorted(rule.digest for rule in self.rules),
        }
        return "access-" + hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()

    def _denied(self, path: str, reason: str) -> ContentAccessDecision:
        return ContentAccessDecision(
            path=path,
            mode="ai_denied",
            allowed=False,
            route_trust=self.route_trust,
            reason=reason,
            policy_refs=(),
            digest=self.digest,
        )

    def decide(self, path: str) -> ContentAccessDecision:
        if not self.valid:
            return self._denied(str(path)[:512], self.reason or _MALFORMED)
        try:
            _safe_path(path, field="path")
            return content_access_for_path(path, self.rules, route_trust=self.route_trust)
        except (TypeError, ValueError):
            return self._denied(str(path)[:512], "the path is not a safe repository-relative path")

    def allowed(self, paths: Iterable[str]) -> tuple[str, ...]:
        return tuple(path for path in paths if self.decide(path).allowed)

    def require(self, paths: Iterable[str] | None) -> None:
        """Raise :class:`ContentAccessRefused` unless every path may reach this route's model.

        ``None`` (paths not declared) refuses; ``()`` is an explicit "no
        repository content" and passes a valid binding.
        """

        if not self.valid:
            raise ContentAccessRefused(self.reason or _MALFORMED)
        if paths is None:
            raise ContentAccessRefused(_UNDECLARED)
        for path in paths:
            decision = self.decide(path)
            if not decision.allowed:
                raise ContentAccessRefused(f"{decision.path}: {decision.reason}")


def _checked_rule(rule: ContentAccessRule) -> ContentAccessRule:
    """The same validation for typed rows as for wire rows (RC6 vocabulary and safe paths)."""

    if rule.mode not in CONTENT_ACCESS_MODES or rule.trust_class not in get_args(RuleTrustClass):
        raise ValueError("content-access value outside the vocabulary")
    scope = rule.path_scope
    if not isinstance(scope, tuple) or not scope or not all(isinstance(item, str) and item for item in scope):
        raise ValueError("content-access paths must be a non-empty list of strings")
    for pattern in scope:
        _safe_path(pattern, field="content-access path")
    return rule


def _rule_from_wire(raw: Mapping[str, Any]) -> ContentAccessRule:
    if set(raw) - _RULE_KEYS:
        raise ValueError("unknown content-access key")
    mode = raw.get("mode")
    paths = raw.get("paths")
    trust_class = raw.get("trust_class")
    if mode not in CONTENT_ACCESS_MODES or trust_class not in get_args(RuleTrustClass):
        raise ValueError("content-access value outside the vocabulary")
    if not isinstance(paths, list) or not paths or not all(isinstance(item, str) and item for item in paths):
        raise ValueError("content-access paths must be a non-empty list of strings")
    rule = _checked_rule(
        ContentAccessRule(
            mode=cast("ContentAccessMode", mode),
            path_scope=tuple(paths),
            origin=str(raw.get("origin") or ""),
            trust_class=cast("RuleTrustClass", trust_class),
            source_identity=str(raw.get("source_identity") or ""),
            source_location=str(raw.get("source_location") or ""),
        )
    )
    if "digest" in raw and raw["digest"] != rule.digest:
        raise ValueError("content-access row does not match its digest")
    if "authoritative" in raw and raw["authoritative"] is not (rule.trust_class in AUTHORITATIVE_RULE_TRUST):
        raise ValueError("content-access row misstates its authority")
    return rule


def bind_content_access(
    rows: object,
    *,
    route: IntelligenceRoute | None = None,
    route_trust: ModelRouteTrust | None = None,
) -> ContentAccessBinding:
    """Bind content-access rows (typed or wire) to one route; malformed rows deny every path.

    ``None`` is an unknown policy (denies every path); an empty sequence is an
    explicit empty rule set. ``route_trust`` (a trust recorded elsewhere, such
    as a customer worker's attested route) wins over the route's RC6 trust; a
    route that is not ready has ``unknown`` trust.
    """

    trust: ModelRouteTrust = (
        route_trust if route_trust is not None else (route.trust if route is not None and route.ready else "unknown")
    )
    if rows is None:
        return ContentAccessBinding((), trust, valid=False, reason=_UNKNOWN_POLICY)
    if isinstance(rows, (str, bytes, Mapping)) or not isinstance(rows, Iterable):
        return ContentAccessBinding((), trust, valid=False, reason=_MALFORMED)
    parsed: list[ContentAccessRule] = []
    try:
        for row in rows:
            if isinstance(row, ContentAccessRule):
                parsed.append(_checked_rule(row))
            elif isinstance(row, Mapping):
                parsed.append(_rule_from_wire(row))
            else:
                raise ValueError("content-access row must be an object")
    except (TypeError, ValueError):
        return ContentAccessBinding((), trust, valid=False, reason=_MALFORMED)
    return ContentAccessBinding(tuple(parsed), trust)


__all__ = [
    "AUTO",
    "BACKGROUND_NOTE",
    "BUILT_IN_ORIGIN",
    "DEFAULT_INTELLIGENCE_POLICY",
    "DRIVER_CHOICES",
    "DRIVER_MODEL_SETTINGS",
    "MODEL_WITHHELD",
    "OPERATOR_LAYERS",
    "OPERATOR_POLICY_SOURCE",
    "POLICY_LAYER_ORDER",
    "READY_NOTE",
    "RUN_ON_CHOICES",
    "SETTINGS_KEYS",
    "ContentAccessBinding",
    "ContentAccessRefused",
    "EffectiveIntelligencePolicy",
    "EffectiveSetting",
    "IntelligencePolicyError",
    "IntelligencePolicyLayer",
    "IntelligenceRoute",
    "PolicyOrigin",
    "RouteRequest",
    "RouteStatus",
    "ask_route_options",
    "bind_content_access",
    "intelligence_policy_digest",
    "parse_intelligence_policy",
    "process_intelligence_policy",
    "resolve_intelligence_policy",
    "resolve_intelligence_route",
    "review_intelligence_policy",
    "route_request",
    "settings_layer_enabled",
    "settings_policy_layer",
    "use_operator_intelligence_policy",
]
