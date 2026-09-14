"""The canonical usage row — pure data, zero I/O.

Why this module exists: three unrelated stores already record "what a session
cost" (run ledgers, traces, savings sidecars) and every consumer so far
re-derived its own shape from whichever one it happened to read. The result is
a reporting surface where the same session has three token totals and where a
model nobody has a rate card for silently reports ``$0.00`` — indistinguishable
from a session that genuinely cost nothing.

The fix this contract encodes is one field: ``cost_usd`` is ``float | None``
and every row states *how* its cost was arrived at in ``cost_provenance``.
``None`` means "tokens known, price unknown" and must never be rendered or
summed as zero. Everything else here exists to make that field trustworthy —
timestamps are normalised, host/provider/project are derived once, and the
token components are disjoint so a total can be added up without
double-counting.

Stdlib plus one provider-prefix table; no store is opened from this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from lemoncrow.core.capabilities.providers.config import LITELLM_PREFIX

# Bump on any field removal or type change. Additive changes keep version 1;
# consumers must ignore unknown keys.
USAGE_SCHEMA_VERSION = 1

CostProvenance = Literal[
    "provider_billed",
    "api_estimated",
    "enterprise_allocated",
    "self_hosted_estimated",
    "self_hosted_unpriced",
    "unknown",
]
"""How a row's ``cost_usd`` was arrived at.

``provider_billed``       reserved; nothing in this package emits it.
                          :func:`~lemoncrow.pro.capabilities.usage.collect.derive_cost`
                          is the only writer of this field, and a run ledger's
                          recorded total is LemonCrow's own rate-card sum over
                          the recorded calls rather than a vendor figure, so it
                          is stamped ``api_estimated``. The value stays in the
                          union for rows a host one day really does report a
                          bill for -- and for rows persisted by builds that did
                          stamp it.
``api_estimated``         priced from the LiteLLM-backed rate card, whether
                          per recorded call (a run ledger's own total) or over
                          the session's aggregate tokens.
``enterprise_allocated``  subscription seat (``copilot/``, ``cursor/``) — no
                          per-call price exists, so no price is invented.
``self_hosted_estimated`` local/custom model that *does* resolve to a rate card.
``self_hosted_unpriced``  local/custom model with no rate card — tokens known,
                          cost unknown.
``unknown``               the model id itself could not be resolved.

The last three always carry ``cost_usd is None``.
"""

COST_PROVENANCE_VALUES: tuple[str, ...] = (
    "provider_billed",
    "api_estimated",
    "enterprise_allocated",
    "self_hosted_estimated",
    "self_hosted_unpriced",
    "unknown",
)

PRICED_PROVENANCE_VALUES: tuple[str, ...] = ("provider_billed", "api_estimated", "self_hosted_estimated")
"""The provenances that may carry a non-``None`` ``cost_usd``. Everything else
is unpriced by construction — see :func:`~lemoncrow.pro.capabilities.usage.collect.derive_cost`."""

UNPRICED_PROVENANCE_VALUES: tuple[str, ...] = ("enterprise_allocated", "self_hosted_unpriced", "unknown")

UsageSource = Literal["run_ledger", "trace", "statusline"]
"""Which store the row came from. ``statusline`` is reserved: the savings
sidecars are a *savings* ledger, not usage, and mixing them double-counts, so
nothing in this package emits it today."""

USAGE_SOURCE_VALUES: tuple[str, ...] = ("run_ledger", "trace", "statusline")

SELF_HOSTED_VENDOR_PREFIXES: tuple[str, ...] = ("ollama", "local", "lm_studio", "custom", "vllm")
"""Model-id namespaces that identify a model the user runs themselves. Tokens
are real; a price only exists if the user configured one."""

SUBSCRIPTION_VENDOR_PREFIXES: tuple[str, ...] = ("copilot", "cursor")
"""Namespaces whose usage a flat subscription already paid for. Mirrors
``pricing._SUBSCRIPTION_VENDOR_PREFIXES``; ``test_subscription_prefixes_match_pricing``
is the guard against the two drifting apart."""

UNKNOWN_STARTED_AT: datetime = datetime(1970, 1, 1, tzinfo=UTC)
"""Sentinel start time for a row whose source records tokens but no timestamp
(``HistoryStore.token_rows`` selects no ``created_at``). Timezone-aware like
every other timestamp here, and deliberately older than any real session so
such a row sorts last rather than being dropped."""

# Vendor namespaces we can *read* off a model id. A namespace is evidence, not
# a guess: "ollama/qwen3" says who served it. Bare ids get the family map below,
# and anything else is "unknown" -- "qwen"/"llama"/"mistral"/"deepseek" with no
# namespace are served by a dozen providers and naming one would be a guess.
_KNOWN_VENDORS: frozenset[str] = (
    frozenset(LITELLM_PREFIX)
    | frozenset(value.rstrip("/") for value in LITELLM_PREFIX.values())
    | frozenset(SELF_HOSTED_VENDOR_PREFIXES)
    | frozenset(SUBSCRIPTION_VENDOR_PREFIXES)
)

_BARE_MODEL_VENDORS: tuple[tuple[str, str], ...] = (
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("gemini", "google"),
)


def provider_for_model(model: str) -> str:
    """Return the serving provider for *model*, or ``"unknown"``.

    A ``vendor/model`` id names its own provider, so the namespace is returned
    verbatim when it is one LiteLLM (or this module) recognises. A bare id is
    matched against the small family map above. Anything else is ``"unknown"``:
    an honest gap beats attributing a row to the wrong vendor's spend.
    """

    raw = (model or "").strip().lower()
    if not raw:
        return "unknown"
    vendor, separator, _rest = raw.partition("/")
    if separator:
        return vendor if vendor in _KNOWN_VENDORS else "unknown"
    for prefix, provider in _BARE_MODEL_VENDORS:
        if raw.startswith(prefix):
            return provider
    return "unknown"


def sum_token_components(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    thinking_tokens: int = 0,
) -> int:
    """Return ``UsageRow.total_tokens`` for the five *disjoint* token components.

    ``cache_write_1h_tokens`` and ``reasoning_output_tokens`` are deliberately
    absent: both are verified *subsets* of another component -- pricing folds
    the 1h-TTL count into ``cache_write`` (``pricing.ModelPricing.cost_breakdown_usd``)
    and every session parser clamps ``reasoning_output_tokens`` to
    ``min(reasoning, output_tokens)`` (``_session_parser.py:453``). Adding them
    would bill the same tokens twice. They stay on the row as informational
    detail; they never enter a total.
    """

    return (
        max(0, input_tokens)
        + max(0, output_tokens)
        + max(0, cache_read_tokens)
        + max(0, cache_write_tokens)
        + max(0, thinking_tokens)
    )


@dataclass(frozen=True)
class UsageRow:
    """One session's usage of one model.

    Every field defaults, so a row built from a partial source is still a valid
    row rather than a crash: unknown strings are ``"unknown"``, unknown counts
    are ``0``, and unknown *prices* are ``None`` -- never ``0.0``.
    """

    schema_version: int = USAGE_SCHEMA_VERSION

    # identity
    session_id: str = ""
    parent_session_id: str | None = None
    """Always ``None`` from this package. Subagent transcripts are folded into
    the parent session at import time (``session_parsers/claude.py:294-298``)
    and the split is not recoverable, so no row claims to be a subagent's."""
    host: str = "unknown"
    provider: str = "unknown"
    model: str = ""
    project: str = "unknown"
    workspace_path: str | None = None

    # time -- ALWAYS timezone-aware UTC
    started_at: datetime = UNKNOWN_STARTED_AT
    ended_at: datetime | None = None
    duration_seconds: float = 0.0

    # tokens
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    """Subset of ``cache_write_tokens``; 0 when the source does not split TTLs."""
    thinking_tokens: int = 0
    reasoning_output_tokens: int = 0
    """Subset of ``output_tokens``; informational only, never summed."""
    total_tokens: int = 0
    """``sum_token_components(...)`` over the five disjoint components."""

    # cost
    cost_usd: float | None = None
    """``None`` means the price is unknown. It is never a stand-in for zero."""
    cost_provenance: CostProvenance = "unknown"
    pricing_model_id: str | None = None
    """What ``get_model_pricing()`` actually resolved to; ``None`` when unpriced."""
    pricing_approximate: bool = False
    """``ModelPricing.approximate``, or ``True`` when a multi-model session was
    split proportionally across models."""

    # activity
    turns: int = 0
    tool_calls: int = 0
    subagent_count: int = 0

    # lineage
    source: UsageSource = "run_ledger"
    source_path: str | None = None
    """The ``run.json`` path or the trace id the row came from."""

    @property
    def is_priced(self) -> bool:
        """True when this row contributes a real dollar figure to a total."""

        return self.cost_usd is not None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload: asdict semantics, timestamps as ISO-8601 UTC."""

        payload: dict[str, Any] = asdict(self)
        payload["started_at"] = self.started_at.isoformat()
        payload["ended_at"] = self.ended_at.isoformat() if self.ended_at is not None else None
        return payload


@dataclass(frozen=True)
class UsageAggregate:
    """A group-by bucket over :class:`UsageRow`.

    Billed and estimated dollars are separate columns on purpose: adding a
    host-reported figure to a rate-card guess produces a number that is neither.
    ``unpriced_rows`` is first-class for the same reason -- it is the count the
    renderer needs to say "and N rows have no price" instead of quietly
    treating them as free.
    """

    key: str
    rows: int = 0
    sessions: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0
    billed_usd: float = 0.0
    """Sum over rows whose provenance is ``provider_billed``."""
    estimated_usd: float = 0.0
    """Sum over rows whose provenance is ``api_estimated`` or ``self_hosted_estimated``."""
    unpriced_rows: int = 0
    """Count of rows with ``cost_usd is None``."""
    provenance_mix: tuple[tuple[str, int], ...] = ()
    """``(provenance, row_count)`` sorted by provenance name; a tuple, not a
    dict, so the aggregate stays frozen-hashable and byte-reproducible."""
    duration_seconds: float = 0.0
    tool_calls: int = 0

    @property
    def total_usd(self) -> float:
        """Billed plus estimated. Unpriced rows contribute nothing, by design."""

        return round(self.billed_usd + self.estimated_usd, 6)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this bucket."""

        payload: dict[str, Any] = asdict(self)
        payload["provenance_mix"] = [list(entry) for entry in self.provenance_mix]
        payload["total_usd"] = self.total_usd
        return payload


__all__ = [
    "COST_PROVENANCE_VALUES",
    "PRICED_PROVENANCE_VALUES",
    "SELF_HOSTED_VENDOR_PREFIXES",
    "SUBSCRIPTION_VENDOR_PREFIXES",
    "UNKNOWN_STARTED_AT",
    "UNPRICED_PROVENANCE_VALUES",
    "USAGE_SCHEMA_VERSION",
    "USAGE_SOURCE_VALUES",
    "CostProvenance",
    "UsageAggregate",
    "UsageRow",
    "UsageSource",
    "provider_for_model",
    "sum_token_components",
]
