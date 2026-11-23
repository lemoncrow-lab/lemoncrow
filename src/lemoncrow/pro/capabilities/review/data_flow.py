"""Effective data flow for one repository: where source, code and evidence may travel.

One builder serves both surfaces: ``lc review data-flow`` (local) and
``GET /v1/repos/{repo_id}/data-flow`` (hosted). It adds no policy and no
parallel mechanism. Every row is derived from the seam that already enforces
it: run settings (``run_plan``), AI content access (``rules``), hermetic
execution policy (``hermetic``), model routes (``intelligence_runtime``,
``independence``), custom model endpoints (``model_setup``) and evidence
connectors (``outcomes``). Every row cites the setting and the policy layer it
came from.

Two invariants hold for every row:

* An unknown is an explicit ``unknown`` row with its reason. It is never
  omitted and never reported as ``allowed``.
* Environment *values* never enter the projection, except validated driver
  names (a closed vocabulary), sanitized model ids and numbers. Proxy, CA,
  storage URLs and file paths are reported by setting *name* only.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args
from urllib.parse import urlsplit

import yaml
from lemoncrow_client.review_words import revision_name

from lemoncrow.pro.capabilities.review.capability_catalog import ExecutorLocus
from lemoncrow.pro.capabilities.review.data_governance import (
    ATTESTABLE_DRIVERS,
    DATA_GOVERNANCE_ENV,
    NOT_CONFIGURED,
    DataGovernanceAttestations,
    RouteGovernance,
    load_data_governance,
    normalize_driver,
    plain_name,
    route_governance,
    shown_model_name,
)
from lemoncrow.pro.capabilities.review.hermetic import HermeticPolicy, load_hermetic_policy
from lemoncrow.pro.capabilities.review.independence import REVIEWER_ROUTES_ENV, configured_reviewer_routes
from lemoncrow.pro.capabilities.review.intelligence_policy import (
    AUTO,
    DRIVER_MODEL_SETTINGS,
    EffectiveIntelligencePolicy,
    IntelligenceRoute,
    PolicyOrigin,
    process_intelligence_policy,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import (
    AGENT_CLI_DRIVERS,
    ReviewIntelligenceDriver,
    operator_off_reason,
    resolve_driver_model,
)
from lemoncrow.pro.capabilities.review.model_identity import UNREPORTED_DEFAULT_LABEL, ModelIdentity
from lemoncrow.pro.capabilities.review.outcomes.deployments import connector_status
from lemoncrow.pro.capabilities.review.rules import (
    INVALID_TRUSTED_BASE_ACCESS_ORIGIN,
    ORGANIZATION_AI_BLOCK_ORIGIN,
    ContentAccessRule,
    ModelRouteTrust,
    ReviewGovernance,
    organization_block,
    parse_organization_governance,
    resolve_review_governance,
)
from lemoncrow.pro.capabilities.review.run_evidence import (
    MAX_RAW_RETENTION_SECONDS,
    MIN_RAW_RETENTION_SECONDS,
    access_allows,
    most_restrictive_access,
)
from lemoncrow.pro.capabilities.review.run_plan import (
    EffectiveRunSettings,
    RunSettingsLayer,
    SettingOrigin,
    parse_review_config_layer,
    resolve_run_settings,
)
from lemoncrow.pro.capabilities.review.run_plan_service import (
    effective_policy_layers,
    policy_invalid_reason,
    resolve_execution_loci,
    unavailable_loci,
)
from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

DATA_FLOW_SCHEMA_VERSION: Final[int] = 1
REVIEW_CONFIG: Final[str] = ".lemoncrow/review.yaml"

Surface = Literal["local", "hosted"]
FlowStatus = Literal["allowed", "restricted", "denied", "off", "set", "not_set", "not_declared", "unknown", "fact"]
FLOW_STATUSES: Final[tuple[FlowStatus, ...]] = (
    "allowed",
    "restricted",
    "denied",
    "off",
    "set",
    "not_set",
    "not_declared",
    "unknown",
    "fact",
)
RouteState = Literal["configured", "none", "unrecognized", "refused", "disabled"]

#: Section order of the projection (and of the text rendering).
CATEGORIES: Final[tuple[tuple[str, str], ...]] = (
    ("policy", "Policy sources"),
    ("source", "Source storage"),
    ("intelligence", "Intelligence routes"),
    ("content", "AI content access"),
    ("models", "Model endpoints and terms"),
    ("web", "Public web"),
    ("execution", "Code execution"),
    ("retention", "Retention"),
    ("evidence", "External evidence feeds"),
    ("network", "Network settings"),
)

BUILT_IN: Final[str] = "built_in_default"
#: Layer of a value read from the local process environment.
LOCAL_ENVIRONMENT: Final[str] = "environment"
#: Layer of a value read from the hosted server's deployment environment.
DEPLOYMENT_ENVIRONMENT: Final[str] = "deployment_environment"

LLM_BACKEND_ENV: Final[str] = "LEMONCROW_LLM_BACKEND"
#: The model setting each configurable driver reads (``intelligence_runtime``).
MODEL_ENV: Final[Mapping[str, str]] = DRIVER_MODEL_SETTINGS
KNOWN_DRIVERS: Final[frozenset[str]] = frozenset(get_args(ReviewIntelligenceDriver))
#: Drivers Ask may select per request besides the agent CLIs (``api.py`` Ask driver list).
ASK_MODEL_DRIVERS: Final[tuple[str, ...]] = ("ollama", "openai", "litellm")
PROXY_SETTINGS: Final[tuple[str, ...]] = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)
CA_SETTINGS: Final[tuple[str, ...]] = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)
STORE_ROOT_ENV: Final[str] = "LEMONCROW_ROOT"
_RUNTIME_FAMILIES: Final[tuple[str, ...]] = ("checks", "behavior_diff", "visual", "deep_runtime")
_LOCUS_LABEL: Final[Mapping[str, str]] = {
    "local": "the machine running LemonCrow (local)",
    "worker": "a customer worker (worker)",
    "managed": "the hosted deployment's managed executor (managed)",
}
_ACCESS_STATUS: Final[Mapping[str, FlowStatus]] = {
    "external_allowed": "allowed",
    "customer_local_only": "restricted",
    "ai_denied": "denied",
}
_MAX_REASON: Final[int] = 400


# --------------------------------------------------------------------------- #
# projection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FlowOrigin:
    """Where one row's value came from: a setting *name* and the policy layer that set it."""

    setting: str
    layer: str

    def to_wire(self) -> dict[str, str]:
        return {"setting": self.setting, "layer": self.layer}


@dataclass(frozen=True, slots=True)
class FlowRow:
    id: str
    category: str
    subject: str
    status: FlowStatus
    detail: str
    origins: tuple[FlowOrigin, ...]
    reason: str = ""
    #: Where the work or data goes, as closed vocabulary: executor kinds for an
    #: ``execution.locus.*`` row, the route trust for a ``models.locus.*`` row. Never a name or
    #: host; not part of the wire projection (the diagnostic bundle reads it).
    locus: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "subject": self.subject,
            "status": self.status,
            "detail": self.detail,
            "origins": [origin.to_wire() for origin in self.origins],
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DataFlowProjection:
    surface: Surface
    repository: str
    rows: tuple[FlowRow, ...]

    def row(self, row_id: str) -> FlowRow | None:
        return next((row for row in self.rows if row.id == row_id), None)

    def to_wire(self) -> dict[str, Any]:
        counts = {status: 0 for status in FLOW_STATUSES}
        for row in self.rows:
            counts[row.status] += 1
        return {
            "schema_version": DATA_FLOW_SCHEMA_VERSION,
            "surface": self.surface,
            "repository": self.repository,
            "rows": [row.to_wire() for row in self.rows],
            "summary": {status: count for status, count in counts.items() if count},
        }


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RouteFact:
    """One configured model route; ``driver``/``model`` are validated ids, never raw setting values."""

    key: str
    subject: str
    state: RouteState
    origins: tuple[FlowOrigin, ...]
    driver: str = ""
    model: str = ""
    trust: ModelRouteTrust = "unknown"
    #: The executor locus whose automatic AI work this route serves.
    locus: ExecutorLocus = "local"
    #: Whether Ask (human questions) uses this route by default.
    ask: bool = False
    note: str = ""
    #: Where ``trust`` came from: ``data_governance.TrustSource`` or ``worker_route_policy``.
    trust_source: str = "driver_classifier"


@dataclass(frozen=True, slots=True)
class SourceGeneration:
    """The Review Snapshot whose captured trusted base governs a hosted projection.

    ``snapshot_number`` is the zero-based number the Reader shows. ``commit`` is empty when that Review has no provider-proven trusted base
    (a client capture): its trusted-base settings are then unknown.
    """

    review_id: str
    snapshot_number: int
    commit: str = ""
    basis: str = ""
    provider: str = ""
    captured_at: str = ""
    #: Model identities this Snapshot's settled intelligence runs recorded (``intelligence_runtime.run_identity``).
    models: tuple[ModelIdentity, ...] = ()


#: Distinct recorded-model entries one row names; the rest are counted, never dropped silently.
MAX_RECORDED_MODEL_ENTRIES: Final[int] = 8

#: Why a hosted repository's trusted-base settings are unknown before any Review captured one.
NO_SOURCE_GENERATION: Final[str] = "no Review-specific source generation yet"

ExecutionLocusKind = Literal["local", "managed", "customer_worker", "unknown"]
EXECUTION_LOCUS_KINDS: Final[tuple[ExecutionLocusKind, ...]] = get_args(ExecutionLocusKind)


@dataclass(frozen=True, slots=True)
class RuntimeExecutionLocus:
    """Where one kind of Review work executes, as the component that runs it reports.

    The extensible execution-locus slot. ``work`` names the work (``runtime``,
    ``ai``, ``observation``, ...: a lowercase identifier). A customer-operated
    worker reports ``kind="customer_worker"`` and its label in ``worker``.
    ``origin`` names the setting (never a value) and layer the report comes
    from. ``failure`` is set only by the composition when a reporter raised: the
    exception class name, never its text. A row shows ``worker`` only when it
    is a plain name (``data_governance.plain_name``); anything else is withheld.
    """

    work: str
    kind: ExecutionLocusKind
    origin: FlowOrigin
    worker: str = ""
    failure: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceFeed:
    """An evidence connector's effective settings (``DeploymentSettings``/``ObservabilitySettings``)."""

    settings: Any
    origins: tuple[FlowOrigin, ...]
    #: Why ``settings`` is ``None`` (could not be resolved).
    reason: str = ""


@dataclass(frozen=True, slots=True)
class OperatorSetting:
    value: Any
    layer: str


@dataclass(frozen=True, slots=True)
class OperatorPolicy:
    """The hosted operator layer: the effective Review policy document the planner reads.

    ``effective`` is exactly the document ``ReviewReaderBridge`` gives the
    Review API (``effective_review``). The other fields only attribute rows to
    the layer that set them; they never change a verdict.
    """

    effective: Mapping[str, Any]
    source: FlowOrigin
    #: ``(layer, raw ai_content_access rows)``, organization first.
    content_access_layers: tuple[tuple[str, tuple[Mapping[str, Any], ...]], ...] = ()
    untrusted_execution: OperatorSetting = OperatorSetting(None, BUILT_IN)
    #: Execution class (``ai``/``runtime``) -> the deployment setting that places it.
    locus_settings: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class HostedFacts:
    """Hosted deployment settings: names of what is set, and validated numbers."""

    storage_settings: tuple[str, ...]
    storage_checked: tuple[str, ...]
    archived_retention_days: int
    blob_retention_seconds: float
    retention_setting: str
    blob_retention_setting: str


@dataclass(frozen=True, slots=True)
class DataFlowInputs:
    surface: Surface
    repository: str
    #: The repository's trusted-base ``.lemoncrow/review.yaml`` (``None``: unknown).
    trusted_base: TrustedBaseDocument | None
    deployments: EvidenceFeed
    observability: EvidenceFeed
    trusted_base_reason: str = ""
    operator: OperatorPolicy | None = None
    routes: tuple[RouteFact, ...] = ()
    agent_cli_allowed: bool = True
    endpoints: tuple[Any, ...] = ()
    endpoints_origin: FlowOrigin = FlowOrigin("providers.json endpoints", "user_settings")
    #: Names (never values) of the checked environment settings that are set.
    environment_names: frozenset[str] = frozenset()
    environment_layer: str = LOCAL_ENVIRONMENT
    proxy_settings: tuple[str, ...] = PROXY_SETTINGS
    ca_settings: tuple[str, ...] = CA_SETTINGS
    hosted: HostedFacts | None = None
    #: Where the local store location came from (``None``: ``LEMONCROW_ROOT`` or the default).
    store_origin: FlowOrigin | None = None
    #: Operator data-governance attestations (``data_governance.load_data_governance``).
    governance: DataGovernanceAttestations = NOT_CONFIGURED
    #: Where work runs as its executors report it (one ``execution.locus.<work>`` row per work).
    execution_loci: tuple[RuntimeExecutionLocus, ...] = ()
    #: Hosted: the Review Snapshot whose trusted base governs (``None``: no Review captured one yet).
    source_generation: SourceGeneration | None = None
    #: BI0: this process's effective intelligence route (``intelligence_policy.resolve_intelligence_route``).
    intelligence: IntelligenceRoute | None = None


# --------------------------------------------------------------------------- #
# input helpers shared by both surfaces
# --------------------------------------------------------------------------- #


def set_environment_names(names: Iterable[str], environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """Which of *names* are set (non-empty). Only names leave this function, never values."""

    env = os.environ if environ is None else environ
    return frozenset(name for name in names if str(env.get(name) or "").strip())


def _validated_driver(raw: str) -> str:
    driver = raw.strip().lower()
    driver = "openai" if driver == "openai_compatible" else driver
    return driver if driver in KNOWN_DRIVERS else ""


def _governed(
    driver: str, governance: DataGovernanceAttestations, layer: str
) -> tuple[ModelRouteTrust, str, tuple[FlowOrigin, ...]]:
    """A process route's trust, where it came from, and the attestation origin when one decided it."""

    governed = route_governance(driver, attestations=governance)
    if governed.trust_source == "operator_attested":
        origin: tuple[FlowOrigin, ...] = (
            FlowOrigin(f"{DATA_GOVERNANCE_ENV} model_routes.{governed.driver}.trust", layer),
        )
    elif governed.trust_source == "attestation_unreadable":
        origin = (FlowOrigin(DATA_GOVERNANCE_ENV, layer),)
    else:
        origin = ()
    return governed.trust, governed.trust_source, origin


def policy_flow_origin(origin: PolicyOrigin, layer: str) -> FlowOrigin:
    """An intelligence-policy origin as a data-flow origin (the settings file is the user's or the server's)."""

    if origin.layer == "settings":
        return FlowOrigin(origin.source, "user_settings" if layer == LOCAL_ENVIRONMENT else "server_settings")
    return FlowOrigin(origin.source, origin.layer)


def environment_route_facts(
    *,
    layer: str,
    agent_cli_allowed: bool,
    locus: ExecutorLocus,
    governance: DataGovernanceAttestations | None = None,
    policy: EffectiveIntelligencePolicy | None = None,
) -> tuple[RouteFact, ...]:
    """This process's configured model routes: the primary provider and independent reviewer routes.

    The primary route is what ``resolve_driver_model`` resolves for a
    ``configured`` request under *policy* (default: this process's
    intelligence policy, the one that request resolves under): the policy's
    explicit driver/model, else ``LEMONCROW_LLM_BACKEND`` and the driver's own
    model. Reviewer routes are ``configured_reviewer_routes``. A setting that
    does not name a known driver is reported ``unrecognized`` without echoing
    it. Trust is ``data_governance.route_governance``: the value
    content-access enforcement reads (``process_route_trust``).
    """

    attestations = load_data_governance() if governance is None else governance
    effective = process_intelligence_policy() if policy is None else policy
    env = os.environ
    raw_backend = str(env.get(LLM_BACKEND_ENV) or "")
    backend_origin = FlowOrigin(LLM_BACKEND_ENV, layer if raw_backend.strip() else BUILT_IN)
    if effective.driver.value != AUTO:
        raw_backend = str(effective.driver.value)
        backend_origin = policy_flow_origin(effective.driver.origin, layer)
    facts: list[RouteFact] = []
    backend = raw_backend.strip().lower() or "none"
    driver = _validated_driver(backend)
    disabled = ""
    if not effective.valid:
        disabled = "intelligence policy is invalid: " + "; ".join(effective.errors)
    elif not effective.enabled.value:
        disabled = operator_off_reason(effective)
    if disabled:
        enabled_origin = policy_flow_origin(effective.enabled.origin, layer)
        facts.append(
            RouteFact(
                "primary", "Configured provider", "disabled", (enabled_origin,), locus=locus, ask=True, note=disabled
            )
        )
    elif backend == "none":
        facts.append(RouteFact("primary", "Configured provider", "none", (backend_origin,), locus=locus, ask=True))
    elif not driver:
        facts.append(
            RouteFact("primary", "Configured provider", "unrecognized", (backend_origin,), locus=locus, ask=True)
        )
    elif driver in AGENT_CLI_DRIVERS and not agent_cli_allowed:
        trust, source, _attested = _governed(driver, attestations, layer)
        facts.append(
            RouteFact(
                "primary",
                "Configured provider",
                "refused",
                (backend_origin,),
                driver=driver,
                trust=trust,
                locus=locus,
                ask=True,
                trust_source=source,
            )
        )
    else:
        trust, source, attested = _governed(driver, attestations, layer)
        model_setting = MODEL_ENV.get(driver, "")
        # The runtime's own resolution, so the projection names the model a
        # ``configured`` request really uses (agent CLIs own their default).
        if effective.model.value != AUTO:
            resolved = str(effective.model.value)
            model_origin = policy_flow_origin(effective.model.origin, layer)
        else:
            resolved = "" if driver in AGENT_CLI_DRIVERS else resolve_driver_model(driver, None, policy=effective).model
            model_origin = FlowOrigin(
                model_setting or f"{driver} CLI default model",
                layer if model_setting and str(env.get(model_setting) or "").strip() else BUILT_IN,
            )
        facts.append(
            RouteFact(
                "primary",
                "Configured provider",
                "configured",
                (backend_origin, model_origin, *attested),
                driver=driver,
                model=shown_model_name(resolved),
                trust=trust,
                locus=locus,
                ask=True,
                note="" if not resolved or shown_model_name(resolved) else _MODEL_ID_WITHHELD,
                trust_source=source,
            )
        )
    routes, ignored = configured_reviewer_routes(env)
    reviewer_origin = FlowOrigin(REVIEWER_ROUTES_ENV, layer)
    for index, (raw_driver, model) in enumerate(routes, start=1):
        reviewer = _validated_driver(raw_driver)
        state: RouteState = "configured"
        if disabled:
            state = "disabled"
        elif not reviewer:
            state = "unrecognized"
        elif reviewer in AGENT_CLI_DRIVERS and not agent_cli_allowed:
            state = "refused"
        reviewer_trust, reviewer_source, reviewer_attested = (
            _governed(reviewer, attestations, layer) if reviewer else ("unknown", "driver_classifier", ())
        )
        facts.append(
            RouteFact(
                f"reviewer.{index}",
                f"Independent reviewer route {index}",
                state,
                (reviewer_origin, *reviewer_attested),
                driver=reviewer,
                model=shown_model_name(model) if reviewer else "",
                trust=reviewer_trust,
                locus=locus,
                note=(
                    disabled
                    if disabled
                    else _MODEL_ID_WITHHELD if reviewer and model and not shown_model_name(model) else ""
                ),
                trust_source=reviewer_source,
            )
        )
    if ignored:
        facts.append(
            RouteFact(
                "reviewer.ignored",
                "Independent reviewer routes",
                "unrecognized",
                (reviewer_origin,),
                locus=locus,
                note=f"{ignored} malformed or excess route(s) ignored",
            )
        )
    return tuple(facts)


#: Why a configured model id is not shown (``data_governance.shown_model_name`` refused it).
_MODEL_ID_WITHHELD: Final[str] = "model id withheld: it is not a plain model identifier"


def endpoint_host(base_url: str) -> str:
    """The host of an endpoint URL only: no scheme, credentials, port or path."""

    try:
        return (urlsplit(base_url).hostname or "").strip().lower()
    except ValueError:
        return ""


# --------------------------------------------------------------------------- #
# builder
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _AccessRow:
    rule: ContentAccessRule
    origins: tuple[FlowOrigin, ...]


@dataclass(frozen=True, slots=True)
class _Access:
    rows: tuple[_AccessRow, ...]
    #: Why part of the content-access policy could not be read (``""``: fully known).
    unknown: str
    unknown_origin: FlowOrigin


def _bounded(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _MAX_REASON else text[: _MAX_REASON - 1] + "…"


#: A parser's message can quote the offending line of the file (a YAML error
#: snippet, an unknown key), so no row ever carries a document's error text.
_WITHHELD: Final[str] = "error text withheld: it may quote the file"


def _unreadable(subject: str, kind: str = "") -> str:
    """Why *subject* is not applied, naming it (and the exception class) but never its error text."""

    return f"{subject} could not be read" + (f" ({kind})" if kind else "") + f"; {_WITHHELD}"


def _run_setting(key: str, origin: SettingOrigin) -> FlowOrigin:
    """The setting name of a run-settings key in the layer that set it."""

    if origin.layer == "trusted_base_repository":
        return FlowOrigin(f"{REVIEW_CONFIG} review.{key}", origin.layer)
    if origin.layer == BUILT_IN:
        return FlowOrigin(f"review.{key}", BUILT_IN)
    return FlowOrigin(f"review policy run_plan.{key}", origin.layer)


def _dedupe(origins: Iterable[FlowOrigin]) -> tuple[FlowOrigin, ...]:
    return tuple(dict.fromkeys(origins))


def _absent_document(path: str) -> TrustedBaseDocument:
    # Rule documents name review *rules*, never content access; they are not read here.
    return TrustedBaseDocument(path=path, content="", state="absent")


def _whole_repository(scope: Sequence[str]) -> bool:
    """A pattern of only ``*`` matches every repository path (``rules.path_matches`` uses fnmatch)."""

    return any(pattern and set(pattern) == {"*"} for pattern in scope)


def _content_access(inputs: DataFlowInputs, config: TrustedBaseDocument) -> _Access:
    operator = inputs.operator
    organization = parse_organization_governance(operator.effective if operator is not None else None)
    governance: ReviewGovernance = resolve_review_governance(
        config, read_base_document=_absent_document, organization=organization
    )
    layer_keys: list[tuple[str, frozenset[tuple[str, tuple[str, ...]]], bool]] = []
    for layer, raw_rows in operator.content_access_layers if operator is not None else ():
        parsed = parse_organization_governance({"ai_content_access": list(raw_rows)})
        keys = frozenset((row.mode, row.path_scope) for row in parsed.content_access)
        layer_keys.append((layer, keys, bool(parsed.errors)))
    rows: list[_AccessRow] = []
    for rule in governance.content_access:
        origins: tuple[FlowOrigin, ...]
        if rule.origin == ORGANIZATION_AI_BLOCK_ORIGIN:
            origins = (_block_origin("ai"),)
        elif rule.trust_class == "trusted_base_repository":
            origins = (
                FlowOrigin(f"{rule.source_location or REVIEW_CONFIG} ai_content_access", "trusted_base_repository"),
            )
        else:
            invalid = rule.origin.startswith("invalid")
            origins = tuple(
                FlowOrigin("review policy ai_content_access", layer)
                for layer, keys, errors in layer_keys
                if (errors if invalid else (rule.mode, rule.path_scope) in keys)
            ) or (
                # No layer named it: cite the operator policy itself, never a guessed layer.
                (operator.source,)
                if operator is not None
                else (FlowOrigin("review policy ai_content_access", BUILT_IN),)
            )
        rows.append(_AccessRow(rule, origins))
    # A malformed trusted-base ai_content_access is not unknown: governance
    # resolution turns it into an ``ai_denied`` row for every path (fail closed).
    unknown = ""
    if config.state == "provisional":
        unknown = inputs.trusted_base_reason or "the trusted base could not be read"
    return _Access(
        rows=tuple(rows),
        unknown=_bounded(unknown),
        unknown_origin=FlowOrigin(f"{REVIEW_CONFIG} ai_content_access", "trusted_base_repository"),
    )


def _verdict(access: _Access, trust: ModelRouteTrust) -> tuple[FlowStatus, str, tuple[FlowOrigin, ...]]:
    """What AI content access lets a route of *trust* receive: denial dominates, then unknown."""

    blocking = [row for row in access.rows if not access_allows(row.rule.mode, trust)]
    whole = [row for row in blocking if _whole_repository(row.rule.path_scope)]
    block = [row for row in whole if row.rule.origin == ORGANIZATION_AI_BLOCK_ORIGIN]
    if block:
        return "denied", _BLOCKED_REASON["ai"], block[0].origins
    if whole:
        return (
            "denied",
            f"AI content access {whole[0].rule.mode} covers the whole repository for {trust} routes",
            _dedupe(origin for row in whole for origin in row.origins),
        )
    if access.unknown:
        return "unknown", access.unknown, (access.unknown_origin,)
    if blocking:
        paths = sorted({pattern for row in blocking for pattern in row.rule.path_scope})
        return (
            "restricted",
            f"paths withheld from {trust} routes: {', '.join(paths[:8])}" + (" …" if len(paths) > 8 else ""),
            _dedupe(origin for row in blocking for origin in row.origins),
        )
    return "allowed", "", ()


_RANK: Final[Mapping[str, int]] = {"allowed": 0, "restricted": 1, "unknown": 2, "denied": 3}


def _worst(
    verdicts: Sequence[tuple[FlowStatus, str, tuple[FlowOrigin, ...]]],
) -> tuple[FlowStatus, str, tuple[FlowOrigin, ...]]:
    return max(verdicts, key=lambda item: _RANK.get(item[0], 3))


class _Rows:
    def __init__(self) -> None:
        self.rows: list[FlowRow] = []

    def add(
        self,
        row_id: str,
        subject: str,
        status: FlowStatus,
        detail: str,
        origins: Iterable[FlowOrigin],
        reason: str = "",
        locus: tuple[str, ...] = (),
    ) -> None:
        cited = _dedupe(origins)
        if not cited:
            raise ValueError(f"data-flow row {row_id} cites no origin")
        self.rows.append(
            FlowRow(row_id, row_id.split(".", 1)[0], subject, status, detail, cited, _bounded(reason), locus)
        )


def _trusted_config(inputs: DataFlowInputs) -> TrustedBaseDocument:
    if inputs.trusted_base is not None:
        return inputs.trusted_base
    return TrustedBaseDocument(path=REVIEW_CONFIG, content="", state="provisional")


def _settings(
    inputs: DataFlowInputs, config: TrustedBaseDocument
) -> tuple[EffectiveRunSettings | None, dict[str, Any], str]:
    """Effective run settings exactly as ``RunPlanService.settings`` resolves them, or why not."""

    effective = inputs.operator.effective if inputs.operator is not None else None
    try:
        layers, execution = effective_policy_layers(effective)
        if config.content:
            layers.append(
                parse_review_config_layer(config.content, source=config.source or config.path)
                or RunSettingsLayer(origin=SettingOrigin("trusted_base_repository", config.source))
            )
        default_run_on, _allowed, _by_class = resolve_execution_loci(execution)
        return resolve_run_settings(layers, default_run_on=default_run_on), execution, ""
    except (RuntimeError, ValueError) as exc:
        return None, {}, f"effective Review policy could not be resolved ({type(exc).__name__}); {_WITHHELD}"


@dataclass(frozen=True, slots=True)
class _Context:
    """What every section reads: the resolved policy of the repository."""

    config: TrustedBaseDocument
    #: The trusted-base config error when the effective policy marks it invalid (nothing runs).
    invalid: str
    settings: EffectiveRunSettings | None
    execution: Mapping[str, Any]
    settings_error: str
    access: _Access
    #: Why the trusted base is unknown (``""``: known). It is a higher layer than
    #: the operator's, so whatever it may change is unknown too.
    base_unknown: str
    hermetic: HermeticPolicy | None
    hermetic_error: str
    org_execution: Mapping[str, Any] | None
    base_execution: Mapping[str, Any] | None
    #: The organization execution block (``rules.organization_block``): kind -> why.
    block: Mapping[str, str]

    @property
    def ai_origin(self) -> FlowOrigin:
        if self.settings is None:
            return FlowOrigin("review.capabilities.ai", BUILT_IN)
        return _run_setting("capabilities.ai", self.settings.origin("capabilities.ai"))


_TB_ORIGIN: Final[FlowOrigin] = FlowOrigin(REVIEW_CONFIG, "trusted_base_repository")
#: Reasons of the organization block, by kind (``rules.organization_block``).
_BLOCKED_REASON: Final[Mapping[str, str]] = {
    kind: f"blocked by organization policy (execution_block.{kind})" for kind in ("ai", "execution")
}
#: Execution class of a locus row -> the organization block kind that stops it.
_BLOCK_KIND: Final[Mapping[str, str]] = {"ai": "ai", "runtime": "execution"}


def _block_origin(kind: str) -> FlowOrigin:
    return FlowOrigin(f"review policy execution_block.{kind}", "organization")


def build_data_flow(inputs: DataFlowInputs) -> DataFlowProjection:
    """The data-flow projection of one repository under its effective policy."""

    config = _trusted_config(inputs)
    settings, execution, settings_error = _settings(inputs, config)
    effective = inputs.operator.effective if inputs.operator is not None else None
    org_block = effective.get("execution") if effective is not None else None
    org_execution = org_block if isinstance(org_block, Mapping) else None
    hermetic: HermeticPolicy | None = None
    hermetic_error = ""
    try:
        # Exactly the API's ``_hermetic_execution`` resolution: trusted base under the org block.
        hermetic = load_hermetic_policy(config.content or None, organization=org_execution)
    except ValueError as exc:
        hermetic_error = _unreadable(f"trusted-base {REVIEW_CONFIG} execution policy", type(exc).__name__)
    ctx = _Context(
        config=config,
        invalid=(
            f"the trusted-base {REVIEW_CONFIG} is invalid, so nothing runs; {_WITHHELD}"
            if policy_invalid_reason(effective)
            else ""
        ),
        settings=settings,
        execution=execution,
        settings_error=settings_error,
        access=_content_access(inputs, config),
        base_unknown=(
            _bounded(inputs.trusted_base_reason or "the trusted base could not be read")
            if config.state == "provisional"
            else ""
        ),
        hermetic=hermetic,
        hermetic_error=hermetic_error,
        org_execution=org_execution,
        base_execution=_base_execution_block(config.content),
        block=organization_block(effective),
    )
    out = _Rows()
    _policy_rows(out, inputs, ctx)
    _source_rows(out, inputs)
    agent_cli_status = _intelligence_rows(out, inputs, ctx)
    _content_rows(out, inputs, ctx)
    _model_rows(out, inputs)
    _web_rows(out, inputs, agent_cli_status)
    _execution_rows(out, inputs, ctx)
    _reported_locus_rows(out, inputs, ctx)
    _retention_rows(out, inputs, ctx)
    _deployment_row(out, inputs.deployments)
    _observability_row(out, inputs.observability)
    _network_rows(out, inputs)
    order = {category: index for index, (category, _title) in enumerate(CATEGORIES)}
    rows = sorted(out.rows, key=lambda row: order.get(row.category, len(order)))
    return DataFlowProjection(inputs.surface, inputs.repository, tuple(rows))


def _policy_rows(out: _Rows, inputs: DataFlowInputs, ctx: _Context) -> None:
    config = ctx.config
    subject = "Trusted-base Review config"
    layer_errors = tuple(ctx.settings.errors) if ctx.settings is not None else ()
    if ctx.invalid:
        out.add("policy.trusted_base", subject, "unknown", "", (_TB_ORIGIN,), ctx.invalid)
    elif config.state == "provisional":
        reason = inputs.trusted_base_reason or "the trusted base could not be read"
        out.add("policy.trusted_base", subject, "unknown", "", (_TB_ORIGIN,), reason)
    elif layer_errors:
        detail = f"read from {config.source or config.path}; part of it could not be applied"
        reason = f"{len(layer_errors)} setting error(s); {_WITHHELD}"
        out.add("policy.trusted_base", subject, "unknown", detail, (_TB_ORIGIN,), reason)
    elif config.state == "present":
        out.add("policy.trusted_base", subject, "fact", f"read from {config.source or config.path}", (_TB_ORIGIN,))
    else:
        detail = f"no {REVIEW_CONFIG} at the trusted base: built-in defaults apply"
        out.add("policy.trusted_base", subject, "fact", detail, (_TB_ORIGIN, FlowOrigin("review", BUILT_IN)))
    _source_generation_row(out, inputs)
    operator = inputs.operator
    if operator is None:
        detail = "no operator layer on this surface: repository policy comes from the trusted base only"
        out.add("policy.operator", "Operator policy", "fact", detail, (FlowOrigin("operator Review policy", BUILT_IN),))
    else:
        detail = "operator organization/repository layers apply below the trusted base"
        out.add("policy.operator", "Operator policy", "fact", detail, (operator.source,))
    subject = "Organization execution block"
    if ctx.block:
        kinds = [kind for kind in ("ai", "execution") if kind in ctx.block]
        detail = (
            "blocks "
            + " and ".join(
                {"ai": "every model call (ai)", "execution": "running reviewed code (execution)"}[k] for k in kinds
            )
            + "; no repository, trusted-base or personal setting lifts it"
        )
        reason = "; ".join(dict.fromkeys(ctx.block[kind] for kind in kinds))
        out.add("policy.organization_block", subject, "denied", detail, (_block_origin(k) for k in kinds), reason)
    elif operator is None:
        detail = "no organization layer on this surface: nothing is blocked above the repository"
        out.add("policy.organization_block", subject, "off", detail, (FlowOrigin("execution_block", BUILT_IN),))
    else:
        detail = "nothing is blocked: repository and personal settings apply"
        origin = FlowOrigin("review policy execution_block", BUILT_IN)
        out.add("policy.organization_block", subject, "off", detail, (origin,))
    governance = inputs.governance
    subject = "Operator data-governance attestations"
    if governance.state == "not_configured":
        detail = (
            f"{DATA_GOVERNANCE_ENV} is not set: provider terms, data regions, litellm route trust and egress are "
            "unknown"
        )
        out.add("policy.data_governance", subject, "not_set", detail, (FlowOrigin(DATA_GOVERNANCE_ENV, BUILT_IN),))
    elif governance.state == "unreadable":
        detail = "nothing it attests applies; every route whose trust it may attest is unknown"
        origin = _governance_origin(inputs)
        out.add("policy.data_governance", subject, "unknown", detail, (origin,), governance.reason)
    else:
        attested = [
            *(["storage region"] if governance.storage_region else []),
            *(["egress"] if governance.egress else []),
            *(f"model route {driver}" for driver, _route in governance.routes),
        ]
        detail = "operator attests: " + (", ".join(attested) or "nothing")
        out.add("policy.data_governance", subject, "set", detail, (_governance_origin(inputs),))


def _source_generation_row(out: _Rows, inputs: DataFlowInputs) -> None:
    """Hosted: which Review Snapshot's captured trusted base this projection reads."""

    generation = inputs.source_generation
    subject = "Review source generation"
    if generation is None:
        if inputs.surface == "hosted":
            detail = (
                f"{NO_SOURCE_GENERATION}: operator policy and deployment settings apply; trusted-base settings are "
                "unknown until a Review captures its trusted base"
            )
            origin = FlowOrigin("Review trusted-base capture", BUILT_IN)
            out.add("policy.source_generation", subject, "not_set", detail, (origin,))
        return
    where = f"Review {generation.review_id} {revision_name(generation.snapshot_number)}"
    origin = FlowOrigin(f"{where} trusted-base capture", "trusted_base_repository")
    if not generation.commit:
        reason = "this Review has no provider-proven trusted base, so trusted-base settings are unknown"
        out.add("policy.source_generation", subject, "unknown", where, (origin,), reason)
        return
    detail = (
        f"{where}: trusted base {generation.provider + ' ' if generation.provider else ''}"
        f"{generation.commit[:12]} ({generation.basis or 'unknown basis'})"
        + (f", captured {generation.captured_at}" if generation.captured_at else "")
    )
    out.add("policy.source_generation", subject, "fact", detail, (origin,))


def _governance_origin(inputs: DataFlowInputs, setting: str = "") -> FlowOrigin:
    """The attestation document setting (and a key in it), in the layer that names the document."""

    name = f"{DATA_GOVERNANCE_ENV} {setting}" if setting else DATA_GOVERNANCE_ENV
    return FlowOrigin(name, BUILT_IN if inputs.governance.state == "not_configured" else inputs.environment_layer)


def _not_attested(inputs: DataFlowInputs, what: str) -> str:
    """Why an attestable fact is unknown (never a value of the document)."""

    governance = inputs.governance
    if governance.state == "unreadable":
        return governance.reason
    if governance.state == "not_configured":
        return f"no operator attestation declares {what} ({DATA_GOVERNANCE_ENV} is not set)"
    return f"{DATA_GOVERNANCE_ENV} does not declare {what}"


def _source_rows(out: _Rows, inputs: DataFlowInputs) -> None:
    hosted = inputs.hosted
    if hosted is None:
        root_set = STORE_ROOT_ENV in inputs.environment_names
        origin = inputs.store_origin or FlowOrigin(STORE_ROOT_ENV, inputs.environment_layer if root_set else BUILT_IN)
        where = f" (location set by {origin.setting})" if origin.layer != BUILT_IN else " (default location)"
        out.add(
            "source.storage", "Revisions and evidence", "fact", f"kept in the store on this machine{where}", (origin,)
        )
        out.add("source.region", "Storage region", "fact", "this machine; Review uploads nothing itself", (origin,))
        return
    named = ", ".join(hosted.storage_settings) or "none of the storage settings"
    out.add(
        "source.storage",
        "Revisions and evidence",
        "fact",
        f"held by the hosted deployment; storage configured by {named}",
        tuple(FlowOrigin(name, inputs.environment_layer) for name in hosted.storage_settings)
        or tuple(FlowOrigin(name, BUILT_IN) for name in hosted.storage_checked),
    )
    region = inputs.governance.storage_region
    origin = _governance_origin(inputs, "storage.region")
    if region:
        out.add("source.region", "Storage region", "fact", f"{region} (operator attested)", (origin,))
        return
    out.add("source.region", "Storage region", "unknown", "", (origin,), _not_attested(inputs, "a storage region"))


def _intelligence_rows(out: _Rows, inputs: DataFlowInputs, ctx: _Context) -> FlowStatus:
    """AI review state, the route rows and the per-request Ask choices; returns the agent-CLI status."""

    settings = ctx.settings
    ai_status: FlowStatus
    if "ai" in ctx.block:
        # Above every layer: neither an unknown trusted base nor an unresolvable one lifts it.
        ai_status = "denied"
        out.add(
            "intelligence.ai", "Automatic AI review", "denied", "nothing runs", (_block_origin("ai"),), ctx.block["ai"]
        )
    elif settings is None:
        ai_status = "unknown"
        out.add("intelligence.ai", "Automatic AI review", "unknown", "", (ctx.ai_origin,), ctx.settings_error)
    elif ctx.invalid:
        ai_status = "denied"
        out.add("intelligence.ai", "Automatic AI review", "denied", "nothing runs", (_TB_ORIGIN,), ctx.invalid)
    elif ctx.base_unknown:
        # The trusted base is a higher layer: it may turn AI review on or off.
        ai_status = "unknown"
        detail = f"operator layers alone give capabilities.ai = {settings.capabilities.get('ai', 'auto')}"
        out.add("intelligence.ai", "Automatic AI review", "unknown", detail, (ctx.ai_origin,), ctx.base_unknown)
    else:
        mode = settings.capabilities.get("ai", "auto")
        ai_status = "denied" if mode == "off" else "allowed"
        reason = "capabilities.ai is off" if mode == "off" else ""
        out.add(
            "intelligence.ai", "Automatic AI review", ai_status, f"capabilities.ai = {mode}", (ctx.ai_origin,), reason
        )
    _policy_row(out, inputs)
    ai_locus = _class_loci(out, inputs, ctx)
    for fact in inputs.routes:
        _route_row(
            out,
            fact,
            ctx.access,
            ai_status=ai_status,
            ai_locus=ai_locus,
            ai_origin=ctx.ai_origin,
            blocked=ctx.block.get("ai", ""),
        )
    cli_status: FlowStatus
    if inputs.agent_cli_allowed:
        cli_status, cli_reason, cli_origins = _verdict(ctx.access, "external")
        out.add(
            "intelligence.agent_cli",
            "Agent CLI routes (Ask)",
            cli_status,
            "Ask may start claude, codex or opencode on this machine; each sends what it reads to its own "
            "provider (external) and may run commands in a disposable copy of the revision",
            (_ASK_ORIGIN, *cli_origins),
            cli_reason,
        )
    else:
        cli_status = "denied"
        out.add(
            "intelligence.agent_cli",
            "Agent CLI routes (Ask)",
            "denied",
            "claude, codex and opencode never run on the hosted server",
            (FlowOrigin("agent CLI routes", BUILT_IN),),
            "agent CLIs are refused in this process",
        )
    trusts = {driver: route_governance(driver, attestations=inputs.governance).trust for driver in ASK_MODEL_DRIVERS}
    ask = [(driver, _verdict(ctx.access, trusts[driver])) for driver in ASK_MODEL_DRIVERS]
    ask_status, ask_reason, ask_origins = _worst([verdict for _driver, verdict in ask])
    out.add(
        "intelligence.ask_models",
        "Ask model choice",
        ask_status,
        "Ask may pick a discovered model per request: "
        + ", ".join(f"{driver} ({trusts[driver]}: {verdict[0]})" for driver, verdict in ask),
        (_ASK_ORIGIN, *ask_origins),
        ask_reason,
    )
    return cli_status


_ASK_ORIGIN: Final[FlowOrigin] = FlowOrigin("Ask driver selection", BUILT_IN)

_ROUTE_STATUS: Final[Mapping[str, FlowStatus]] = {
    "ready": "set",
    "disabled": "off",
    "not_configured": "not_set",
    "refused": "denied",
    "invalid": "denied",
}


def _policy_row(out: _Rows, inputs: DataFlowInputs) -> None:
    """The effective intelligence policy (BI0): requested choices, run-on and background, with origins."""

    route = inputs.intelligence
    if route is None:
        return
    policy = route.policy
    settings = (policy.enabled, policy.driver, policy.model, policy.run_on, policy.background_enabled)
    requested = route.requested.to_wire()
    run_on = f"run_on {policy.run_on.value}" + (
        f" ({route.run_on})" if policy.run_on.value == AUTO and route.run_on else ""
    )
    # BI2: background and run_on govern background work (automatic AI Review today).
    detail = (
        f"driver {requested['driver']}, model {requested['model']}, {run_on}, "
        f"background intelligence {'on' if route.background_enabled else 'off'}; "
        "background and run_on govern automatic AI Review"
    )
    out.add(
        "intelligence.policy",
        "Intelligence policy",
        _ROUTE_STATUS[route.status],
        detail,
        tuple(policy_flow_origin(setting.origin, inputs.environment_layer) for setting in settings),
        route.reason,
    )


def _content_rows(out: _Rows, inputs: DataFlowInputs, ctx: _Context) -> None:
    access = ctx.access
    if not access.rows:
        out.add(
            "content.ai_access",
            "AI content access",
            "allowed",
            "no AI content-access rule: every path may reach any model route (external_allowed)",
            (FlowOrigin("ai_content_access", BUILT_IN),),
        )
    for index, row in enumerate(access.rows, start=1):
        out.add(
            f"content.ai_access.{index}",
            f"AI content access: {', '.join(row.rule.path_scope)}",
            _ACCESS_STATUS.get(row.rule.mode, "denied"),
            f"{row.rule.mode} ({row.rule.origin})",
            row.origins,
            _invalid_policy_reason(row.rule, inputs),
        )
    if access.unknown:
        out.add(
            "content.trusted_base",
            "Trusted-base AI content access",
            "unknown",
            "",
            (access.unknown_origin,),
            access.unknown,
        )
    mode = most_restrictive_access(row.rule.mode for row in access.rows)
    unknown = bool(access.unknown) and mode != "ai_denied"
    out.add(
        "content.execution_evidence",
        "Execution output to models",
        "unknown" if unknown else _ACCESS_STATUS.get(mode, "denied"),
        f"execution-derived content reaches a model only under the strictest mode in force: {mode}",
        _dedupe(origin for row in access.rows for origin in row.origins)
        or (FlowOrigin("ai_content_access", BUILT_IN),),
        access.unknown if unknown else "",
    )


_TRUST_SOURCE_LABEL: Final[Mapping[str, str]] = {
    "operator_attested": "operator attested",
    "driver_classifier": "by driver",
    "attestation_unreadable": "attestation unreadable",
    "worker_route_policy": "customer worker route policy",
}


@dataclass(frozen=True, slots=True)
class _ModelRoute:
    """One route whose model-data locus and provider terms get rows."""

    key: str
    label: str
    origins: tuple[FlowOrigin, ...]
    trust: ModelRouteTrust
    trust_source: str
    #: This process's governance of the route (``None``: governed elsewhere, a customer worker).
    governed: RouteGovernance | None


def _model_rows(out: _Rows, inputs: DataFlowInputs) -> None:
    source = inputs.endpoints_origin
    attestations = inputs.governance
    routes: list[_ModelRoute] = []
    for fact in inputs.routes:
        if fact.state != "configured":
            continue
        governed = None if fact.locus == "worker" else route_governance(fact.driver, attestations=attestations)
        model = shown_model_name(fact.model)
        label = fact.driver + (f"/{model}" if model else "")
        routes.append(_ModelRoute(fact.key, label, fact.origins, fact.trust, fact.trust_source, governed))
    litellm = route_governance("litellm", attestations=attestations)
    for index, endpoint in enumerate(inputs.endpoints, start=1):
        # The operator-chosen name keys the row only when it is a plain name; otherwise its position does.
        name = plain_name(endpoint.name)
        key = name or str(index)
        origin = FlowOrigin(f"{source.setting}.{key}", source.layer)
        shown = [shown_model_name(row.raw_id) for row in endpoint.models]
        models = ", ".join(model for model in shown if model)
        withheld = sum(1 for model in shown if not model)
        if withheld:
            models = (f"{models}, " if models else "") + f"{withheld} model id(s) withheld"
        out.add(
            f"models.endpoint.{key}",
            f"Custom endpoint {name or f'{index} (name withheld)'}",
            "fact",
            f"host {endpoint_host(str(endpoint.base_url)) or 'unknown'}, locus {endpoint.locus}, "
            f"models: {models or 'none'}; reached through the litellm route "
            f"(content access: {litellm.trust} trust)",
            (origin,),
        )
        routes.append(
            _ModelRoute(f"endpoint.{key}", f"endpoint {key}", (origin,), litellm.trust, litellm.trust_source, litellm)
        )
    if inputs.agent_cli_allowed:
        for driver in sorted(AGENT_CLI_DRIVERS):
            governed = route_governance(driver, attestations=attestations)
            routes.append(
                _ModelRoute(
                    f"agent_cli.{driver}", driver, (_ASK_ORIGIN,), governed.trust, governed.trust_source, governed
                )
            )
    for route in routes:
        _locus_row(out, inputs, route)
        _terms_row(out, inputs, route)
    _recorded_models_row(out, inputs)


_MODEL_WITHHELD: Final[str] = "(model id withheld)"


def _recorded_model_entry(identity: ModelIdentity) -> str:
    """One recorded identity: a known driver and shown model names only (``shown_model_name``)."""

    driver = normalize_driver(identity.driver)
    name = driver if driver in ATTESTABLE_DRIVERS else "unrecognized driver"
    source = identity.source
    if source == "observed":
        models = ", ".join(shown_model_name(model) or _MODEL_WITHHELD for model in identity.observed_models)
        return f"{name} observed {models}" + (" (+more)" if identity.observed_truncated else "")
    if source == "pinned":
        return f"{name} requested {shown_model_name(identity.requested_model) or _MODEL_WITHHELD} (not observed)"
    if source == "unverified":
        return f"{name} {shown_model_name(identity.unverified_model) or _MODEL_WITHHELD} (worker-reported, unverified)"
    return f"{name} {UNREPORTED_DEFAULT_LABEL}"


def _recorded_models_row(out: _Rows, inputs: DataFlowInputs) -> None:
    """Hosted: the model identities the source generation's intelligence runs recorded."""

    generation = inputs.source_generation
    if generation is None:
        return
    where = f"Review {generation.review_id} {revision_name(generation.snapshot_number)}"
    origin = FlowOrigin(f"{where} intelligence runs", "review_record")
    subject = "Models this revision's runs recorded"
    if not generation.models:
        out.add("models.recorded", subject, "not_set", f"{where} has no settled intelligence run", (origin,))
        return
    entries = sorted({_recorded_model_entry(identity) for identity in generation.models})
    shown = entries[:MAX_RECORDED_MODEL_ENTRIES]
    more = len(entries) - len(shown)
    detail = f"{where}: " + "; ".join(shown) + (f"; and {more} more" if more else "")
    out.add("models.recorded", subject, "fact", detail, (origin,))


def _locus_row(out: _Rows, inputs: DataFlowInputs, route: _ModelRoute) -> None:
    """Where a route sends content: its trust (the model-data locus), provider and data region."""

    row_id, subject = f"models.locus.{route.key}", f"Model data locus: {route.label}"
    governed = route.governed
    driver = governed.driver if governed is not None else ""
    attested = (_governance_origin(inputs, f"model_routes.{driver}"),) if governed is not None else ()
    source = _TRUST_SOURCE_LABEL.get(route.trust_source, route.trust_source)
    if route.trust == "unknown":
        if route.trust_source == "attestation_unreadable":
            reason = inputs.governance.reason
        else:
            reason = (
                f"no operator attestation says where the {driver} route sends content; a hostname is never proof"
                if governed is not None
                else "the route policy does not attest where this route sends content"
            )
        out.add(row_id, subject, "unknown", "", (*route.origins, *attested), reason, (route.trust,))
        return
    provider = governed.provider if governed is not None else ""
    region = governed.data_region if governed is not None else ""
    detail = (
        f"{route.trust} ({source})"
        + (f"; provider {provider}" if provider else "")
        + (f"; data region {region}" if region else "; data region unknown")
    )
    if region or route.trust == "customer_local":
        out.add(row_id, subject, "fact", detail, (*route.origins, *attested), locus=(route.trust,))
        return
    reason = (
        _not_attested(inputs, f"the {driver} provider's data region")
        if governed is not None
        else "the customer worker route policy records no data region"
    )
    out.add(row_id, subject, "unknown", detail, (*route.origins, *attested), reason, (route.trust,))


def _terms_row(out: _Rows, inputs: DataFlowInputs, route: _ModelRoute) -> None:
    """The provider's training/use and retention terms, as attested; anything else is unknown."""

    row_id, subject = f"models.terms.{route.key}", f"Training and retention: {route.label}"
    governed = route.governed
    if governed is None:
        reason = "the customer worker route policy records no provider training or retention terms"
        out.add(row_id, subject, "unknown", "training: unknown; retention: unknown", route.origins, reason)
        return
    days = governed.retention_days
    retention = "unknown" if days is None else f"{days} day" + ("" if days == 1 else "s")
    detail = f"training: {governed.training or 'unknown'}; retention: {retention}"
    origins = (*route.origins, _governance_origin(inputs, f"model_routes.{governed.driver}"))
    if governed.terms_known:
        out.add(row_id, subject, "set", detail, origins)
        return
    missing = " and ".join(
        name
        for name, value in (("training", governed.training), ("retention_days", governed.retention_days))
        if value is None
    )
    reason = _not_attested(inputs, f"model_routes.{governed.driver} {missing}")
    out.add(row_id, subject, "unknown", detail, origins, reason)


def _web_rows(out: _Rows, inputs: DataFlowInputs, agent_cli_status: FlowStatus) -> None:
    out.add(
        "web.automatic_review",
        "Automatic AI review",
        "denied",
        "automatic AI review has no web tools (review_autonomous tool profile; hosted jobs deny egress)",
        (FlowOrigin("review_autonomous tool profile", BUILT_IN),),
    )
    out.add(
        "web.ask",
        "Ask with an agent CLI",
        agent_cli_status,
        (
            "an agent CLI in Ask may call web_fetch (public targets only)"
            if inputs.agent_cli_allowed
            else "no agent CLI runs, so Ask has no web tools"
        ),
        (FlowOrigin("review tool profile", BUILT_IN),),
        "follows intelligence.agent_cli" if agent_cli_status != "allowed" else "",
    )


def _network_rows(out: _Rows, inputs: DataFlowInputs) -> None:
    for row_id, subject, names in (
        ("network.proxy", "Outbound proxy", inputs.proxy_settings),
        ("network.custom_ca", "Custom CA", inputs.ca_settings),
    ):
        present = [name for name in names if name in inputs.environment_names]
        if present:
            origins = tuple(FlowOrigin(name, inputs.environment_layer) for name in present)
            out.add(row_id, subject, "set", f"set by {', '.join(present)}", origins)
        else:
            out.add(
                row_id,
                subject,
                "not_set",
                f"none of {', '.join(names)} is set",
                (FlowOrigin(", ".join(names), BUILT_IN),),
            )
    egress = inputs.governance.egress
    origin = _governance_origin(inputs, "network.egress")
    subject = "Outbound network"
    if egress is None:
        reason = _not_attested(inputs, "outbound network") + (
            "; LemonCrow restricts only the web and dependency-fetch rows above"
        )
        out.add("network.egress", subject, "unknown", "", (origin,), reason)
        return
    status, detail = _EGRESS[egress]
    out.add("network.egress", subject, status, f"operator attests: {detail}", (origin,))


_EGRESS: Final[Mapping[str, tuple[FlowStatus, str]]] = {
    "no_egress": ("denied", "no outbound network (no_egress)"),
    "proxy_only": ("restricted", "outbound traffic leaves only through the proxy (proxy_only)"),
    "unrestricted": ("allowed", "outbound network is not restricted (unrestricted)"),
}


def _invalid_policy_reason(rule: ContentAccessRule, inputs: DataFlowInputs) -> str:
    """Why a fail-closed deny row exists, if this is one (never the policy's error text)."""

    if rule.origin == ORGANIZATION_AI_BLOCK_ORIGIN:
        return _BLOCKED_REASON["ai"]
    if rule.origin == INVALID_TRUSTED_BASE_ACCESS_ORIGIN:
        location = rule.source_location or REVIEW_CONFIG
        return f"trusted-base {location} ai_content_access could not be read, so it denies every route; {_WITHHELD}"
    if not rule.origin.startswith("invalid") or inputs.operator is None:
        return ""
    return f"operator Review policy ai_content_access is invalid, so it denies every route; {_WITHHELD}"


def _class_loci(out: _Rows, inputs: DataFlowInputs, ctx: _Context) -> ExecutorLocus | None:
    """Where AI and runtime work execute (``build_run_plan``'s per-class resolution); returns the AI locus."""

    operator = inputs.operator
    locus_settings = (operator.locus_settings if operator is not None else None) or {}
    settings = ctx.settings
    resolved: dict[str, ExecutorLocus | None] = {}
    for cls, row_id, subject in (
        ("ai", "execution.ai_locus", "AI work runs on"),
        ("runtime", "execution.runtime_locus", "Reviewed code runs on"),
    ):
        deployment = (
            (FlowOrigin(locus_settings[cls], DEPLOYMENT_ENVIRONMENT),)
            if cls in locus_settings
            else (FlowOrigin("allowed_run_on", BUILT_IN),)
        )
        blocked = ctx.block.get(_BLOCK_KIND[cls], "")
        if settings is None:
            resolved[cls] = None
            if blocked:
                out.add(
                    row_id,
                    subject,
                    "off",
                    "every locus is blocked",
                    (*deployment, _block_origin(_BLOCK_KIND[cls])),
                    blocked,
                )
            else:
                out.add(row_id, subject, "unknown", "", deployment, ctx.settings_error)
            continue
        _default, allowed, by_class = resolve_execution_loci(ctx.execution)
        class_allowed = by_class.get(cls) or allowed
        requested = settings.run_on
        locus: ExecutorLocus = (
            requested if requested in class_allowed else (class_allowed[0] if class_allowed else "local")
        )
        origins = (*deployment, _run_setting("run_on", settings.origin("run_on")))
        if blocked:
            # The block makes every locus of the class unavailable, whatever ``run_on`` picks.
            resolved[cls] = locus
            out.add(
                row_id, subject, "off", "every locus is blocked", (*origins, _block_origin(_BLOCK_KIND[cls])), blocked
            )
            continue
        if ctx.base_unknown and len(class_allowed) > 1:
            # The trusted base may set ``run_on`` to another allowed locus.
            resolved[cls] = None
            detail = f"one of: {', '.join(class_allowed)}"
            out.add(row_id, subject, "unknown", detail, origins, ctx.base_unknown)
            continue
        resolved[cls] = locus
        # Like the planner, a fallback is noted only when a layer asked for another locus.
        explicit = settings.origin("run_on").layer != BUILT_IN
        note = f"; {requested} is not enabled here" if explicit and requested != locus else ""
        reason = (unavailable_loci(ctx.execution.get("unavailable_run_on_by_class")).get(cls) or {}).get(locus, "")
        detail = f"{_LOCUS_LABEL.get(locus, locus)}{note}"
        out.add(row_id, subject, "off" if reason else "fact", detail, origins, reason)
    return resolved.get("ai")


def _route_row(
    out: _Rows,
    fact: RouteFact,
    access: _Access,
    *,
    ai_status: FlowStatus,
    ai_locus: ExecutorLocus | None,
    ai_origin: FlowOrigin,
    blocked: str = "",
) -> None:
    row_id = f"intelligence.route.{fact.key}"
    model = shown_model_name(fact.model)
    label = fact.driver + (f" / {model}" if model else "") if fact.driver else ""
    if fact.state == "none":
        out.add(row_id, fact.subject, "off", "no model provider is configured", fact.origins, fact.note)
        return
    if fact.state == "disabled":
        out.add(
            row_id, fact.subject, "off", "the intelligence policy refuses every model route", fact.origins, fact.note
        )
        return
    if fact.state == "unrecognized":
        reason = fact.note or "the setting does not name a known driver; Review Intelligence refuses it"
        out.add(row_id, fact.subject, "unknown", "", fact.origins, reason)
        return
    if fact.state == "refused":
        reason = "agent CLIs never run in this process"
        out.add(row_id, fact.subject, "denied", f"{label} (agent CLI)", fact.origins, reason)
        return
    attested = fact.trust_source in ("operator_attested", "attestation_unreadable")
    source = f", {_TRUST_SOURCE_LABEL[fact.trust_source]}" if attested else ""
    detail = f"{label} ({fact.trust} route{source})" + (f"; {fact.note}" if fact.note else "")
    if blocked:
        out.add(row_id, fact.subject, "denied", detail, (*fact.origins, _block_origin("ai")), blocked)
        return
    if ai_locus is None and not fact.ask:
        out.add(row_id, fact.subject, "unknown", detail, (*fact.origins, ai_origin), "where AI work runs is unknown")
        return
    if ai_locus is None:
        detail += "; whether automatic AI review uses it is unknown (where AI work runs is unknown)"
    on_locus = ai_locus == fact.locus
    automatic = on_locus and ai_status != "denied"
    uses = [use for use, on in (("automatic AI review", automatic), ("Ask", fact.ask)) if on]
    if not uses:
        if on_locus:
            out.add(row_id, fact.subject, "denied", detail, (*fact.origins, ai_origin), "capabilities.ai is off")
        else:
            reason = f"automatic AI work runs on {ai_locus}, not {fact.locus}"
            out.add(row_id, fact.subject, "off", detail, (*fact.origins, ai_origin), reason)
        return
    verdict, reason, origins = _verdict(access, fact.trust)
    if automatic and ai_status == "unknown" and verdict in ("allowed", "restricted"):
        # Never report a route allowed while whether automatic AI review uses it is unknown.
        verdict, reason, origins = "unknown", "whether automatic AI review runs is unknown", (ai_origin,)
    out.add(row_id, fact.subject, verdict, f"{detail}; used for {', '.join(uses)}", (*fact.origins, *origins), reason)


def _execution_rows(out: _Rows, inputs: DataFlowInputs, ctx: _Context) -> None:
    settings = ctx.settings
    subject = "Reviewed code execution"
    if "execution" in ctx.block:
        origin = _block_origin("execution")
        out.add("execution.reviewed_code", subject, "denied", "nothing runs", (origin,), ctx.block["execution"])
    elif settings is None:
        origin = FlowOrigin("review.capabilities", BUILT_IN)
        out.add("execution.reviewed_code", subject, "unknown", "", (origin,), ctx.settings_error)
    elif ctx.invalid:
        out.add("execution.reviewed_code", subject, "denied", "nothing runs", (_TB_ORIGIN,), ctx.invalid)
    else:
        modes = {family: settings.capabilities.get(family, "auto") for family in _RUNTIME_FAMILIES}
        enabled = [family for family in _RUNTIME_FAMILIES if modes[family] != "off"]
        family_origins = [
            _run_setting(f"capabilities.{family}", settings.origin(f"capabilities.{family}"))
            for family in _RUNTIME_FAMILIES
        ]
        detail = ", ".join(f"{family} = {modes[family]}" for family in _RUNTIME_FAMILIES)
        if ctx.base_unknown:
            # The trusted base is a higher layer: it may turn any family on or off.
            detail = f"operator layers alone give {detail}"
            out.add("execution.reviewed_code", subject, "unknown", detail, family_origins, ctx.base_unknown)
        else:
            reason = "" if enabled else "every runtime capability family is off"
            status: FlowStatus = "allowed" if enabled else "denied"
            out.add("execution.reviewed_code", subject, status, detail, family_origins, reason)
    operator = inputs.operator
    if operator is None:
        out.add(
            "execution.untrusted_code",
            "Untrusted head code",
            "fact",
            "local runs execute on this machine and are attested by its user (author_machine)",
            (FlowOrigin("executor class", BUILT_IN),),
        )
    elif "execution" in ctx.block:
        detail = "an untrusted head is never executed"
        block = (_block_origin("execution"),)
        out.add("execution.untrusted_code", "Untrusted head code", "denied", detail, block, ctx.block["execution"])
    else:
        setting = operator.untrusted_execution
        approved = setting.value is True
        out.add(
            "execution.untrusted_code",
            "Untrusted head code",
            "allowed" if approved else "denied",
            "a reviewer may approve executing an untrusted head" if approved else "an untrusted head is never executed",
            (FlowOrigin("review policy allow_untrusted_runtime_execution_approval", setting.layer),),
        )
    subject = "Dependency fetch during execution"
    policy = ctx.hermetic
    if policy is None:
        origin = FlowOrigin(f"{REVIEW_CONFIG} execution", "trusted_base_repository")
        out.add("execution.dependency_fetch", subject, "unknown", "", (origin,), ctx.hermetic_error)
        return
    key = "deny_all" if policy.deny_all else "registries"
    origins = _execution_origins(key, ctx.base_execution, ctx.org_execution)
    if policy.deny_all:
        out.add(
            "execution.dependency_fetch", subject, "denied", "execution.deny_all: no registry is reachable", origins
        )
        return
    registries = [f"{rule.ecosystem} {rule.host}" for rule in policy.registries]
    detail = f"installs may fetch from: {', '.join(registries)}" if registries else "no registry is allowlisted"
    if ctx.base_unknown:
        out.add(
            "execution.dependency_fetch",
            subject,
            "unknown",
            f"operator layers alone: {detail}",
            origins,
            ctx.base_unknown,
        )
        return
    out.add("execution.dependency_fetch", subject, "allowed" if registries else "denied", detail, origins)


_WORK_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,31}")
_EXCEPTION_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
#: Work -> the organization block kind that stops it. Customer-worker production observation
#: is LemonCrow-initiated work on customer infrastructure, so the execution block stops it too.
_WORK_BLOCK: Final[Mapping[str, str]] = {"ai": "ai", "runtime": "execution", "observation": "execution"}


def _reported_place(locus: RuntimeExecutionLocus) -> str:
    if locus.kind == "customer_worker":
        label = plain_name(locus.worker)
        return f"customer-operated worker {label}" if label else "a customer-operated worker (label withheld)"
    return _LOCUS_LABEL.get(locus.kind, locus.kind)


def _reported_locus_rows(out: _Rows, inputs: DataFlowInputs, ctx: _Context) -> None:
    """One ``execution.locus.<work>`` row per reported work: where it runs, by its executors' reports."""

    by_work: dict[str, list[RuntimeExecutionLocus]] = {}
    for locus in inputs.execution_loci:
        work = locus.work if _WORK_RE.fullmatch(locus.work or "") else "unrecognized"
        by_work.setdefault(work, []).append(locus)
    for work, loci in sorted(by_work.items()):
        row_id, subject = f"execution.locus.{work}", f"Reported execution locus: {work}"
        origins = _dedupe(locus.origin for locus in loci)
        if work == "unrecognized":
            out.add(row_id, subject, "unknown", "", origins, "a reported locus names work that is not an identifier")
            continue
        known = [locus for locus in loci if locus.kind in EXECUTION_LOCUS_KINDS and locus.kind != "unknown"]
        detail = "; ".join(dict.fromkeys(_reported_place(locus) for locus in known))
        kinds = tuple(sorted({locus.kind for locus in known}))
        blocked = ctx.block.get(_WORK_BLOCK.get(work, ""), "")
        if blocked:
            block_origin = _block_origin(_WORK_BLOCK[work])
            out.add(
                row_id, subject, "off", detail or "every locus is blocked", (*origins, block_origin), blocked, kinds
            )
            continue
        unknown = [locus for locus in loci if locus not in known]
        if not unknown:
            out.add(row_id, subject, "fact", detail, origins, locus=kinds)
            continue
        failed = [locus.failure for locus in unknown if locus.failure]
        if failed:
            kind = failed[0] if _EXCEPTION_RE.fullmatch(failed[0]) else "Exception"
            reason = f"the locus reporter failed ({kind}); {_WITHHELD}"
        else:
            reason = "the executor did not report where this work runs"
        out.add(row_id, subject, "unknown", detail, origins, reason)


def _base_execution_block(content: str) -> Mapping[str, Any] | None:
    """The trusted base's raw ``execution`` block, read only to attribute rows to it."""

    if not content:
        return None
    try:
        decoded = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    block = decoded.get("execution") if isinstance(decoded, Mapping) else None
    return block if isinstance(block, Mapping) else None


def _execution_origins(
    key: str, base: Mapping[str, Any] | None, organization: Mapping[str, Any] | None
) -> tuple[FlowOrigin, ...]:
    origins = []
    if organization is not None and key in organization:
        origins.append(FlowOrigin(f"review policy execution.{key}", "organization"))
    if base is not None and key in base:
        origins.append(FlowOrigin(f"{REVIEW_CONFIG} execution.{key}", "trusted_base_repository"))
    return tuple(origins) or (FlowOrigin(f"execution.{key}", BUILT_IN),)


def _days(seconds: float) -> str:
    if seconds < 86400:
        hours = seconds / 3600
        return f"{hours:g} hour" + ("" if hours == 1 else "s")
    days = seconds / 86400
    return f"{days:g} day" + ("" if days == 1 else "s")


def _retention_rows(out: _Rows, inputs: DataFlowInputs, ctx: _Context) -> None:
    policy = ctx.hermetic
    subject = "Raw run artifacts"
    if policy is None:
        origin = FlowOrigin(f"{REVIEW_CONFIG} execution.retention_days", "trusted_base_repository")
        out.add("retention.run_artifacts", subject, "unknown", "", (origin,), ctx.hermetic_error)
    else:
        # The window ``RunArtifactStore`` enforces: it clamps the policy to [1 hour, 365 days].
        seconds = max(MIN_RAW_RETENTION_SECONDS, min(int(policy.retention_seconds), MAX_RAW_RETENTION_SECONDS))
        origins = _execution_origins("retention_days", ctx.base_execution, ctx.org_execution)
        if ctx.base_unknown:
            # The trusted base may only shorten it (the shorter of the two applies).
            detail = f"raw logs, traces, video and payloads expire after at most {_days(seconds)}"
            out.add("retention.run_artifacts", subject, "unknown", detail, origins, ctx.base_unknown)
        else:
            detail = (
                f"raw logs, traces, video and payloads expire after {_days(seconds)} unless pinned; summaries are kept"
            )
            out.add("retention.run_artifacts", subject, "fact", detail, origins)
    hosted = inputs.hosted
    if hosted is None:
        detail = "kept in the local store until you delete them"
        out.add("retention.reviews", "Reviews", "fact", detail, (FlowOrigin("Review retention", BUILT_IN),))
        return
    out.add(
        "retention.reviews",
        "Reviews",
        "fact",
        f"archived Reviews may be pruned after {hosted.archived_retention_days} days; open and finished Reviews "
        "are never age-pruned",
        (FlowOrigin(hosted.retention_setting, inputs.environment_layer),),
    )
    out.add(
        "retention.source_index",
        "Unreferenced indexed source",
        "fact",
        f"unreferenced index content is pruned after {_days(hosted.blob_retention_seconds)}",
        (FlowOrigin(hosted.blob_retention_setting, inputs.environment_layer),),
    )


def _refused(feed: EvidenceFeed) -> str:
    # ``settings.error`` is built from raw parser text (``could not be read: {exc}``, unknown keys).
    names = ", ".join(origin.setting for origin in feed.origins)
    return f"{names} was refused, so nothing from it applies; {_WITHHELD}"


def _deployment_row(out: _Rows, feed: EvidenceFeed) -> None:
    settings = feed.settings
    if settings is None:
        out.add("evidence.deployments", "Deployment facts", "unknown", "", feed.origins, feed.reason or "not resolved")
        return
    status = connector_status(settings)
    if settings.error:
        out.add("evidence.deployments", "Deployment facts", "unknown", "", feed.origins, _refused(feed))
    elif status.state == "configured":
        out.add(
            "evidence.deployments",
            "Deployment facts",
            "allowed",
            f"connector {status.connector} observes deployments ({status.locus})",
            feed.origins,
        )
    else:
        out.add(
            "evidence.deployments",
            "Deployment facts",
            "off",
            "no deployment connector observes anything",
            feed.origins,
            status.reason,
        )


def _observability_row(out: _Rows, feed: EvidenceFeed) -> None:
    settings = feed.settings
    if settings is None:
        out.add(
            "evidence.observability", "Production telemetry", "unknown", "", feed.origins, feed.reason or "not resolved"
        )
        return
    if settings.error:
        out.add("evidence.observability", "Production telemetry", "unknown", "", feed.origins, _refused(feed))
        return
    if not settings.configured:
        out.add(
            "evidence.observability",
            "Production telemetry",
            "off",
            "no observability connector is configured",
            feed.origins,
        )
        return
    connectors = "; ".join(
        f"{connector.kind} {plain_name(connector.id) or f'connector {index} (id withheld)'} "
        f"at {endpoint_host(connector.base_url) or 'unknown host'}"
        + (f" (credential from {connector.credential.env})" if connector.credential.env else "")
        for index, connector in enumerate(settings.connectors, start=1)
    )
    out.add(
        "evidence.observability",
        "Production telemetry",
        "allowed",
        f"queried on {settings.locus}: {connectors}",
        feed.origins,
    )


# --------------------------------------------------------------------------- #
# text rendering
# --------------------------------------------------------------------------- #


def render_data_flow_text(projection: DataFlowProjection) -> str:
    """Human text: one section per category, one line per row plus its origin and reason."""

    lines = [f"Data flow for {projection.repository} ({projection.surface})"]
    for category, title in CATEGORIES:
        rows = [row for row in projection.rows if row.category == category]
        if not rows:
            continue
        lines.extend(("", title))
        for row in rows:
            text = f"  {row.status:<12} {row.subject}"
            lines.append(f"{text}: {row.detail}" if row.detail else text)
            if row.reason:
                lines.append(f"  {'':<12}   why: {row.reason}")
            origins = "; ".join(f"{origin.setting} ({origin.layer})" for origin in row.origins)
            lines.append(f"  {'':<12}   from: {origins}")
    counts = projection.to_wire()["summary"]
    lines.extend(("", "Summary: " + ", ".join(f"{count} {status}" for status, count in counts.items())))
    return "\n".join(lines) + "\n"


__all__ = [
    "BUILT_IN",
    "CA_SETTINGS",
    "DATA_FLOW_SCHEMA_VERSION",
    "DEPLOYMENT_ENVIRONMENT",
    "EXECUTION_LOCUS_KINDS",
    "FLOW_STATUSES",
    "KNOWN_DRIVERS",
    "LOCAL_ENVIRONMENT",
    "MAX_RECORDED_MODEL_ENTRIES",
    "NO_SOURCE_GENERATION",
    "PROXY_SETTINGS",
    "STORE_ROOT_ENV",
    "DataFlowInputs",
    "DataFlowProjection",
    "EvidenceFeed",
    "ExecutionLocusKind",
    "FlowOrigin",
    "FlowRow",
    "HostedFacts",
    "OperatorPolicy",
    "OperatorSetting",
    "RouteFact",
    "RuntimeExecutionLocus",
    "SourceGeneration",
    "build_data_flow",
    "endpoint_host",
    "environment_route_facts",
    "render_data_flow_text",
    "set_environment_names",
]
