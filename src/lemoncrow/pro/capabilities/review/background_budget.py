"""Shared background-intelligence budget: one autonomous envelope, one accounting path (BI1).

Autonomous AI work -- automatic Review intelligence, knowledge refresh,
discussion summaries, learning suggestions -- draws on one shared *background*
envelope per scope (a hosted organization, or the local LemonCrow root). The
envelope is **additional** to every limit that already exists; it replaces none
of them:

* the per-operation cap (``ModelCallBudget.max_calls``),
* the capability's own bound (max model calls per execution),
* the per-Review cap (``budget.per_review_model_calls`` of the run plan, whose
  run-plan authority is unchanged),
* the hosted organization quota (``model_spend_micros``),
* and this envelope: period model calls, an optional daily guard, and managed
  spend.

Admission (:func:`decide`) grants the **most restrictive** of all of them and
names the one that bound with a stable :class:`AdmissionReason`.

**Lanes.** Explicit human work -- Ask, a manual or deeper Review, an explicit
fresh re-run -- and policy-required Review work (:attr:`JobPriority.REVIEW_REQUIRED`)
run in the :attr:`BudgetLane.INTERACTIVE` lane: metered in the same ledger, in
separate counters, and never refused because the background envelope is empty.
They still obey every other ceiling the caller composes.

**Low priority pauses first.** Within the background lane each priority may use
only its share of a ceiling (:data:`PRIORITY_ENVELOPE_PERCENT`), so as the
envelope drains ``learning_suggestion`` pauses before ``discussion_summary``,
before ``knowledge_refresh``, before ``review_auto``.

**Money is never pretended.** A dollar ceiling binds only managed execution
(LemonCrow pays the provider). A managed request whose cost cannot be estimated
is refused with :attr:`AdmissionReason.COST_UNMEASURABLE` rather than charged
as zero. Calls on local/worker routes are counted; their cost is shown only
where the provider reported one, and calls of unknown cost are counted as such.

**Exactly-once accounting.** A reservation is keyed by the caller's
idempotency key (one per execution *attempt*; only its SHA-256 digest is
stored). Reserving the same key again returns the same reservation; settling it
again with the same usage is a no-op and with different usage a
:class:`BudgetConflict`. Model calls are held at admission, so concurrent
admissions can never together exceed a call ceiling. A hold that is never
settled (a worker died with its lease) expires and is charged in full -- the
conservative reading, since the calls may have been made -- and a late truthful
settlement may still correct it once.

**Lifecycle of the durable state.** Rows are written only by :func:`reserve_in`
and :func:`settle_in`. Each reservation is charged to the period (UTC calendar
month and day) it was admitted in, so a settlement after a rollover never
touches the new period. Period and reservation rows older than the retention
horizon are deleted when a new period first opens (the rollover is the
closer), and a scope purge deletes all of them. The ledger stores no
repository, Review, path or prompt: only digests, capability ids, priorities
and counts.

This module is backend-neutral. :class:`SqlLedgerTransaction` runs the same
algorithm on SQLite (local, ``background_budget_store``) and on PostgreSQL
(hosted, the quota authority). It is a contract, not a scheduler: BI2 decides
what to run and asks this ledger whether it may.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final, Literal, Protocol, cast

__all__ = [
    "BACKGROUND_BUDGET_RETENTION_S",
    "BACKGROUND_PRIORITIES",
    "DEFAULT_HOLD_S",
    "DEFAULT_MONTHLY_MANAGED_MICROS",
    "DEFAULT_MONTHLY_MODEL_CALLS",
    "MAX_OPERATION_CALLS",
    "MAX_POLICY_CALLS",
    "MAX_POLICY_MICROS",
    "PRIORITY_ENVELOPE_PERCENT",
    "AdmissionReason",
    "AiUse",
    "BackgroundAdmission",
    "BackgroundBudgetPolicies",
    "BackgroundBudgetPolicy",
    "BackgroundBudgetPolicyError",
    "BackgroundWorkRequest",
    "BudgetConflict",
    "BudgetLane",
    "BudgetLocus",
    "BudgetOverrun",
    "BudgetSettlement",
    "CeilingInput",
    "CeilingSource",
    "Decision",
    "JobPriority",
    "LedgerTransaction",
    "PeriodTotals",
    "PriorityTier",
    "SqlLedgerTransaction",
    "UnknownReservation",
    "ai_use_in",
    "build_status",
    "day_start",
    "decide",
    "expire_in",
    "extend_in",
    "key_digest",
    "lane_for",
    "meter_in",
    "month_start",
    "next_month_start",
    "read_status",
    "reserve_in",
    "settle_in",
    "usd_to_micros",
]

_DAY_S: Final[int] = 86_400
_TOKEN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")

#: How long settled accounting is kept: the hosted retired-usage horizon
#: (``hosted_jobs.JOB_USAGE_RETENTION_S``, pinned equal by a test), so a
#: Usage total and the job usage it sits beside expire together.
BACKGROUND_BUDGET_RETENTION_S: Final[float] = 400.0 * _DAY_S
#: How long an unsettled hold lives before it is charged in full by default.
DEFAULT_HOLD_S: Final[float] = 3600.0
_MIN_HOLD_S: Final[float] = 60.0
_MAX_HOLD_S: Final[float] = 7.0 * _DAY_S
#: The most model calls one admission may ask for.
MAX_OPERATION_CALLS: Final[int] = 10_000
#: Shipped period ceilings (plan §5.1): a starting point, not a measurement.
DEFAULT_MONTHLY_MODEL_CALLS: Final[int] = 200
DEFAULT_MONTHLY_MANAGED_MICROS: Final[int] = 25_000_000
#: Holds one admission expires before deciding (bounded work per call).
_EXPIRY_BATCH: Final[int] = 64


# --------------------------------------------------------------------------- #
# priority vocabulary (RC8 dispatch and BI1 admission share it)
# --------------------------------------------------------------------------- #


class PriorityTier(StrEnum):
    """The strictly ordered dispatch tiers: every foreground job before any background job."""

    FOREGROUND = "foreground"
    BACKGROUND = "background"


class JobPriority(StrEnum):
    """The fixed priority vocabulary of model work (RC8 dispatch, BI1 admission, BI2 scheduling).

    Not user-configurable and not numeric on the wire. Hosted dispatch
    (``tenant_fairness``) serves the foreground :attr:`tier` before the
    background tier; within a tier a lower :attr:`rank` goes first only inside
    one organization's turn. Stored as ``lc_hosted_jobs.priority_rank``; never
    part of a job's contract digest, so a priority change never re-keys work.
    Budget admission uses the same values: see :func:`lane_for` and
    :data:`PRIORITY_ENVELOPE_PERCENT`.
    """

    #: Policy-required model work for a current Review, or work a person asked
    #: for and is waiting on (manual/fresh AI review, AI pre-merge checks).
    REVIEW_REQUIRED = "review_required"
    #: Normal automatic Review intelligence; the default for every other job.
    REVIEW_AUTO = "review_auto"
    #: Background: landed-change / project-context refresh.
    KNOWLEDGE_REFRESH = "knowledge_refresh"
    #: Background: comment/thread digests.
    DISCUSSION_SUMMARY = "discussion_summary"
    #: Background: repeated-feedback / rule / knowledge suggestions.
    LEARNING_SUGGESTION = "learning_suggestion"

    @property
    def rank(self) -> int:
        return _PRIORITY_RANK[self]

    @classmethod
    def from_rank(cls, rank: int) -> JobPriority:
        for priority, value in _PRIORITY_RANK.items():
            if value == rank:
                return priority
        raise ValueError(f"unknown job priority rank {rank!r}")

    @property
    def background(self) -> bool:
        return self in BACKGROUND_PRIORITIES

    @property
    def tier(self) -> PriorityTier:
        return PriorityTier.BACKGROUND if self.background else PriorityTier.FOREGROUND


_PRIORITY_RANK: Final[Mapping[JobPriority, int]] = {
    JobPriority.REVIEW_REQUIRED: 1,
    JobPriority.REVIEW_AUTO: 2,
    JobPriority.KNOWLEDGE_REFRESH: 3,
    JobPriority.DISCUSSION_SUMMARY: 4,
    JobPriority.LEARNING_SUGGESTION: 5,
}
#: Dispatch-background priorities (RC8 tiering). ``review_auto`` is foreground
#: for dispatch yet autonomous for budget: see :func:`lane_for`.
BACKGROUND_PRIORITIES: Final[frozenset[JobPriority]] = frozenset(
    {JobPriority.KNOWLEDGE_REFRESH, JobPriority.DISCUSSION_SUMMARY, JobPriority.LEARNING_SUGGESTION}
)

#: The share (percent) of each background ceiling a priority may fill, so the
#: lowest priority pauses first as the envelope drains. Shares round down
#: (``cap * percent // 100``), so a tiny ceiling is reserved for the top
#: priority by design: a daily guard of 1 lets only ``review_auto`` start (the
#: others report ``priority_reserved``); from 2 every priority may start.
# lc-debt: fixed shares (100/90/80/70) rather than a measured reservation per
# priority; a busy month can leave the top 10-30% unused by low-priority work.
# Upgrade: derive shares from observed per-priority demand once BI2 records it.
PRIORITY_ENVELOPE_PERCENT: Final[Mapping[JobPriority, int]] = {
    JobPriority.REVIEW_REQUIRED: 100,
    JobPriority.REVIEW_AUTO: 100,
    JobPriority.KNOWLEDGE_REFRESH: 90,
    JobPriority.DISCUSSION_SUMMARY: 80,
    JobPriority.LEARNING_SUGGESTION: 70,
}


class BudgetLane(StrEnum):
    """Which counters a reservation is charged to."""

    #: Autonomous work: charged to and enforced against the background envelope.
    BACKGROUND = "background"
    #: Explicit human or policy-required work: metered separately, never refused by the envelope.
    INTERACTIVE = "interactive"


def lane_for(priority: JobPriority, *, explicit: bool = False) -> BudgetLane:
    """The lane of one piece of work: explicit or required work is interactive, the rest background."""

    if explicit or priority is JobPriority.REVIEW_REQUIRED:
        return BudgetLane.INTERACTIVE
    return BudgetLane.BACKGROUND


class AdmissionReason(StrEnum):
    """Why one admission went the way it did. Stable machine tokens."""

    ADMITTED = "admitted"
    #: Background intelligence is turned off for this scope.
    BACKGROUND_DISABLED = "background_disabled"
    #: The period (monthly) model-call ceiling is reached.
    BACKGROUND_EXHAUSTED = "background_exhausted"
    #: The optional daily model-call guard is reached.
    DAILY_GUARD_EXHAUSTED = "daily_guard_exhausted"
    #: The managed (LemonCrow-paid) spend ceiling is reached.
    MANAGED_SPEND_EXHAUSTED = "managed_spend_exhausted"
    #: The personal monthly AI budget is $0 or used up (local only; it applies to both lanes).
    AI_BUDGET_EXHAUSTED = "ai_budget_exhausted"
    #: A dollar ceiling binds this managed work but its cost cannot be estimated.
    COST_UNMEASURABLE = "cost_unmeasurable"
    #: Room remains, but it is held for higher-priority background work.
    PRIORITY_RESERVED = "priority_reserved"
    #: The per-operation cap is below the minimum this work needs.
    OPERATION_LIMIT = "operation_limit"
    #: The capability's own hard bound is below the minimum this work needs.
    CAPABILITY_LIMIT = "capability_limit"
    #: The Review's own model-call budget is spent.
    REVIEW_LIMIT = "review_limit"
    #: The hosted organization model-spend quota is reached.
    HOSTED_QUOTA_EXHAUSTED = "hosted_quota_exhausted"
    #: The background budget settings are malformed: background work fails closed.
    POLICY_INVALID = "policy_invalid"
    #: Managed (LemonCrow-paid) execution does not exist here (local), so it cannot be admitted.
    MANAGED_UNAVAILABLE = "managed_unavailable"


class CeilingSource(StrEnum):
    """Where one ceiling in an admission came from."""

    CAPABILITY = "capability"
    OPERATION = "operation"
    REVIEW = "review"
    HOSTED_QUOTA = "hosted_quota"
    BACKGROUND_PERIOD = "background_period"
    BACKGROUND_DAILY = "background_daily"
    MANAGED_SPEND = "managed_spend"


_EXTERNAL_SOURCES: Final[frozenset[CeilingSource]] = frozenset(
    {CeilingSource.CAPABILITY, CeilingSource.OPERATION, CeilingSource.REVIEW, CeilingSource.HOSTED_QUOTA}
)
_REASON_FOR: Final[Mapping[CeilingSource, AdmissionReason]] = {
    CeilingSource.CAPABILITY: AdmissionReason.CAPABILITY_LIMIT,
    CeilingSource.OPERATION: AdmissionReason.OPERATION_LIMIT,
    CeilingSource.REVIEW: AdmissionReason.REVIEW_LIMIT,
    CeilingSource.HOSTED_QUOTA: AdmissionReason.HOSTED_QUOTA_EXHAUSTED,
    CeilingSource.BACKGROUND_PERIOD: AdmissionReason.BACKGROUND_EXHAUSTED,
    CeilingSource.BACKGROUND_DAILY: AdmissionReason.DAILY_GUARD_EXHAUSTED,
    CeilingSource.MANAGED_SPEND: AdmissionReason.MANAGED_SPEND_EXHAUSTED,
}

BudgetLocus = Literal["local", "worker", "managed"]
_LOCI: Final[frozenset[str]] = frozenset({"local", "worker", "managed"})


class BackgroundBudgetPolicyError(ValueError):
    """A background budget policy value is malformed. Names the key, never the value."""


class BudgetConflict(RuntimeError):
    """An idempotency key was reused with different work or a different settlement."""


class BudgetOverrun(ValueError):
    """A settlement reported more model calls than the reservation granted."""


class UnknownReservation(LookupError):
    """A settlement named a key this scope never reserved (or already purged)."""


# --------------------------------------------------------------------------- #
# periods
# --------------------------------------------------------------------------- #


def month_start(ts: float) -> int:
    """Unix start of the UTC calendar month holding ``ts``."""

    moment = datetime.fromtimestamp(float(ts), UTC)
    return int(datetime(moment.year, moment.month, 1, tzinfo=UTC).timestamp())


def next_month_start(start: int) -> int:
    moment = datetime.fromtimestamp(int(start), UTC)
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return int(datetime(year, month, 1, tzinfo=UTC).timestamp())


def day_start(ts: float) -> int:
    """Unix start of the UTC day holding ``ts``."""

    return int(float(ts) // _DAY_S) * _DAY_S


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #


def usd_to_micros(value: object, *, key: str = "monthly_managed_usd") -> int:
    """A non-negative USD amount as integer micros (exact decimal, no float rounding)."""

    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise BackgroundBudgetPolicyError(f"{key} must be a non-negative number of US dollars")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise BackgroundBudgetPolicyError(f"{key} must be a non-negative number of US dollars") from exc
    if not amount.is_finite() or amount < 0 or amount * 1_000_000 > MAX_POLICY_MICROS:
        raise BackgroundBudgetPolicyError(f"{key} must be a non-negative number of US dollars")
    return int((amount * 1_000_000).to_integral_value())


#: The largest model-call ceiling a policy accepts.
MAX_POLICY_CALLS: Final[int] = 10**9
#: The largest dollar ceiling a policy accepts, in micros (``usd_to_micros``' $1e9 bound).
MAX_POLICY_MICROS: Final[int] = 10**15


def _count(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_POLICY_CALLS:
        raise BackgroundBudgetPolicyError(f"{key} must be a whole number from 0 to {MAX_POLICY_CALLS}")
    return value


_POLICY_KEYS: Final[frozenset[str]] = frozenset(
    {"enabled", "monthly_model_calls", "daily_model_calls", "monthly_managed_usd"}
)
#: The keys that set a ceiling (``enabled`` is a switch, not a ceiling).
_CEILING_KEYS: Final[frozenset[str]] = _POLICY_KEYS - {"enabled"}


@dataclass(frozen=True, slots=True)
class BackgroundBudgetPolicy:
    """The background envelope for one scope. ``None`` means no ceiling of that kind.

    ``0`` is a legal ceiling: no autonomous work of that kind at all.

    ``configured`` says whether an operator (the hosted policy file's
    ``background`` section) or the user (an ``intelligence.background.*``
    ceiling setting) set this envelope; it defaults to ``True`` (an envelope
    built in code binds). Only :meth:`shipped` -- the envelope nobody chose --
    is unconfigured: its ceilings bind knowledge, summary and suggestion work,
    while automatic Review (``review_auto``) is metered in the envelope but
    bounded only by its own Review, operation and hosted-quota ceilings, so an
    envelope nobody chose never stops automatic Review (BI2 owner decision,
    2026-10-07).
    """

    enabled: bool = True
    monthly_model_calls: int | None = DEFAULT_MONTHLY_MODEL_CALLS
    monthly_managed_micros: int | None = DEFAULT_MONTHLY_MANAGED_MICROS
    daily_model_calls: int | None = None
    #: Set only by :meth:`invalid`: the configured policy could not be read, so
    #: background work fails closed with ``policy_invalid``.
    policy_invalid: bool = False
    configured: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.configured, bool):
            raise BackgroundBudgetPolicyError("enabled must be true or false")
        for name in ("monthly_model_calls", "daily_model_calls"):
            value = getattr(self, name)
            if value is not None:
                _count(value, name)
        micros = self.monthly_managed_micros
        if micros is not None and (
            isinstance(micros, bool) or not isinstance(micros, int) or not 0 <= micros <= MAX_POLICY_MICROS
        ):
            raise BackgroundBudgetPolicyError("monthly_managed_usd must be a non-negative number of US dollars")

    @classmethod
    def invalid(cls) -> BackgroundBudgetPolicy:
        """The closed policy used when the configured one is malformed: no background work at all."""

        return cls(
            enabled=False,
            monthly_model_calls=0,
            monthly_managed_micros=0,
            daily_model_calls=0,
            policy_invalid=True,
            configured=True,
        )

    @classmethod
    def shipped(cls) -> BackgroundBudgetPolicy:
        """The shipped envelope, unconfigured: nobody set it, so it does not limit automatic Review."""

        return cls(configured=False)

    @classmethod
    def from_mapping(cls, raw: object, *, base: BackgroundBudgetPolicy | None = None) -> BackgroundBudgetPolicy:
        """Parse ``{enabled, monthly_model_calls, daily_model_calls, monthly_managed_usd}`` onto ``base``.

        A key that is absent keeps ``base`` (default: :meth:`shipped`); an explicit
        ``null`` removes that ceiling. A ceiling key makes the result ``configured``.
        """

        start = base if base is not None else cls.shipped()
        if not isinstance(raw, Mapping):
            raise BackgroundBudgetPolicyError("a background budget must be an object")
        unknown = sorted(str(key) for key in raw if key not in _POLICY_KEYS)
        if unknown:
            raise BackgroundBudgetPolicyError(
                "a background budget may only set " + ", ".join(sorted(_POLICY_KEYS)) + f" (not {unknown[0][:64]})"
            )
        values: dict[str, Any] = {
            "enabled": start.enabled,
            "monthly_model_calls": start.monthly_model_calls,
            "monthly_managed_micros": start.monthly_managed_micros,
            "daily_model_calls": start.daily_model_calls,
            "configured": start.configured or any(key in raw for key in _CEILING_KEYS),
        }
        if "enabled" in raw:
            if not isinstance(raw["enabled"], bool):
                raise BackgroundBudgetPolicyError("enabled must be true or false")
            values["enabled"] = raw["enabled"]
        for key in ("monthly_model_calls", "daily_model_calls"):
            if key in raw:
                values[key] = None if raw[key] is None else _count(raw[key], key)
        if "monthly_managed_usd" in raw:
            amount = raw["monthly_managed_usd"]
            values["monthly_managed_micros"] = None if amount is None else usd_to_micros(amount)
        return cls(**values)

    def narrowed(self, other: BackgroundBudgetPolicy) -> BackgroundBudgetPolicy:
        """The most restrictive combination (an admin setting under an operator ceiling)."""

        def tighter(left: int | None, right: int | None) -> int | None:
            if left is None:
                return right
            return left if right is None else min(left, right)

        return BackgroundBudgetPolicy(
            enabled=self.enabled and other.enabled,
            monthly_model_calls=tighter(self.monthly_model_calls, other.monthly_model_calls),
            monthly_managed_micros=tighter(self.monthly_managed_micros, other.monthly_managed_micros),
            daily_model_calls=tighter(self.daily_model_calls, other.daily_model_calls),
            configured=self.configured or other.configured,
        )

    def binds(self, priority: JobPriority) -> bool:
        """Whether this envelope's ceilings bind background work of ``priority`` (see ``configured``)."""

        return self.configured or priority is not JobPriority.REVIEW_AUTO

    def to_wire(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "monthly_model_calls": self.monthly_model_calls,
            "daily_model_calls": self.daily_model_calls,
            "monthly_managed_usd": _usd(self.monthly_managed_micros),
            "valid": not self.policy_invalid,
            "configured": self.configured,
        }


@dataclass(frozen=True, slots=True)
class BackgroundBudgetPolicies:
    """Operator policy: one default envelope plus per-scope (organization) overrides.

    The ``background`` section of the hosted quota policy file::

        "background": {
          "defaults": {"monthly_model_calls": 500, "monthly_managed_usd": 50},
          "overrides": {"org_trial": {"monthly_model_calls": 50, "daily_model_calls": 10}}
        }

    ``defaults`` is merged onto the shipped envelope; an override is merged onto
    the defaults and may raise as well as lower, like every quota override. Only
    an explicit ceiling (``monthly_model_calls``, ``daily_model_calls`` or
    ``monthly_managed_usd``) makes an envelope ``configured`` (it then binds
    automatic Review too); a section that sets only ``enabled`` -- or none at
    all -- leaves the shipped envelope unconfigured, so automatic Review stays
    metered but unenforced.
    """

    defaults: BackgroundBudgetPolicy = field(default_factory=BackgroundBudgetPolicy.shipped)
    overrides: Mapping[str, BackgroundBudgetPolicy] = field(default_factory=dict)

    @classmethod
    def standard(cls) -> BackgroundBudgetPolicies:
        return cls()

    @classmethod
    def parse(cls, raw: object) -> BackgroundBudgetPolicies:
        if raw is None:
            return cls.standard()
        if not isinstance(raw, Mapping):
            raise BackgroundBudgetPolicyError("'background' must be an object")
        if set(raw) - {"defaults", "overrides"}:
            raise BackgroundBudgetPolicyError("'background' accepts only 'defaults' and 'overrides'")
        defaults = BackgroundBudgetPolicy.from_mapping(raw.get("defaults", {}), base=BackgroundBudgetPolicy.shipped())
        raw_overrides = raw.get("overrides", {})
        if not isinstance(raw_overrides, Mapping):
            raise BackgroundBudgetPolicyError("'background.overrides' must be an object keyed by organization id")
        overrides: dict[str, BackgroundBudgetPolicy] = {}
        for scope_id, entry in raw_overrides.items():
            if not isinstance(scope_id, str) or not scope_id or len(scope_id) > 256:
                raise BackgroundBudgetPolicyError("a background override key must be a non-empty organization id")
            overrides[scope_id] = BackgroundBudgetPolicy.from_mapping(entry, base=defaults)
        return cls(defaults=defaults, overrides=overrides)

    def for_scope(self, scope_id: str) -> BackgroundBudgetPolicy:
        return self.overrides.get(scope_id, self.defaults)


# --------------------------------------------------------------------------- #
# requests and ceilings
# --------------------------------------------------------------------------- #


def _token(value: object, name: str) -> str:
    text = str(value)
    if _TOKEN.fullmatch(text) is None:
        raise ValueError(f"{name} must be a bounded machine token")
    return text


def key_digest(key: str) -> str:
    """The only form in which an idempotency key is stored."""

    if not isinstance(key, str) or not key or len(key) > 1024:
        raise ValueError("a budget idempotency key must be 1 to 1024 characters")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _digest(document: Mapping[str, Any]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CeilingInput:
    """One ceiling composed into an admission from outside the envelope.

    ``remaining_calls`` bounds model calls; ``remaining_micros`` is a dollar
    ceiling, which binds managed work only.
    """

    source: CeilingSource
    remaining_calls: int | None = None
    remaining_micros: int | None = None

    def __post_init__(self) -> None:
        if self.source not in _EXTERNAL_SOURCES:
            raise ValueError("only capability, operation, review and hosted_quota ceilings are composed from outside")
        for name in ("remaining_calls", "remaining_micros"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"{name} must be an integer")

    @classmethod
    def operation(cls, max_calls: int) -> CeilingInput:
        """The per-operation cap (``ModelCallBudget.max_calls``)."""

        return cls(CeilingSource.OPERATION, remaining_calls=int(max_calls))

    @classmethod
    def capability(cls, max_calls: int) -> CeilingInput:
        """The capability's declared max model calls per execution."""

        return cls(CeilingSource.CAPABILITY, remaining_calls=int(max_calls))

    @classmethod
    def review(cls, per_review_model_calls: int | None, used: int) -> CeilingInput | None:
        """The Review's own model-call budget (run plan ``budget.per_review_model_calls``), if one is set."""

        if per_review_model_calls is None:
            return None
        return cls(CeilingSource.REVIEW, remaining_calls=int(per_review_model_calls) - max(0, int(used)))

    @classmethod
    def hosted_quota(cls, remaining_micros: int) -> CeilingInput:
        """The hosted organization model-spend quota still free, in micros."""

        return cls(CeilingSource.HOSTED_QUOTA, remaining_micros=int(remaining_micros))


@dataclass(frozen=True, slots=True)
class BackgroundWorkRequest:
    """One execution attempt asking for model calls.

    ``key`` is the attempt's idempotency key (for a hosted job, its id and
    attempt). ``cost_per_call_micros`` is the provider-cost estimate per call;
    ``None`` means the route cannot estimate it. ``explicit`` marks work a
    person asked for.
    """

    key: str
    capability: str
    priority: JobPriority
    max_calls: int
    min_calls: int = 1
    locus: BudgetLocus = "local"
    cost_per_call_micros: int | None = None
    explicit: bool = False
    hold_s: float = DEFAULT_HOLD_S

    def __post_init__(self) -> None:
        key_digest(self.key)
        _token(self.capability, "capability")
        if not isinstance(self.priority, JobPriority):
            object.__setattr__(self, "priority", JobPriority(str(self.priority)))
        if self.locus not in _LOCI:
            raise ValueError("locus must be local, worker or managed")
        for name in ("max_calls", "min_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_OPERATION_CALLS:
                raise ValueError(f"{name} must be from 1 to {MAX_OPERATION_CALLS}")
        if self.min_calls > self.max_calls:
            raise ValueError("min_calls cannot exceed max_calls")
        cost = self.cost_per_call_micros
        if cost is not None and (isinstance(cost, bool) or not isinstance(cost, int) or cost < 0):
            raise ValueError("cost_per_call_micros must be a non-negative integer or None")
        if not _MIN_HOLD_S <= float(self.hold_s) <= _MAX_HOLD_S:
            raise ValueError("hold_s must be between one minute and seven days")

    @property
    def lane(self) -> BudgetLane:
        return lane_for(self.priority, explicit=self.explicit)

    @property
    def key_digest(self) -> str:
        return key_digest(self.key)

    def fingerprint(self) -> str:
        """What the same key must ask for again to be the same reservation."""

        return _digest(
            {
                "capability": self.capability,
                "priority": self.priority.value,
                "lane": self.lane.value,
                "max_calls": self.max_calls,
                "min_calls": self.min_calls,
                "locus": self.locus,
                "cost_per_call_micros": self.cost_per_call_micros,
            }
        )


# --------------------------------------------------------------------------- #
# decision (pure)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PeriodTotals:
    """One lane's counters for one period. ``held`` is admitted but not yet settled."""

    calls_held: int = 0
    calls_used: int = 0
    micros_held: int = 0
    micros_used: int = 0
    reported_micros: int = 0
    unmeasured_calls: int = 0

    @property
    def calls(self) -> int:
        return self.calls_held + self.calls_used

    @property
    def micros(self) -> int:
        return self.micros_held + self.micros_used

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> PeriodTotals:
        if row is None:
            return cls()
        return cls(**{name: max(0, int(row[name] or 0)) for name in _PERIOD_COLUMNS})


_PERIOD_COLUMNS: Final[tuple[str, ...]] = (
    "calls_held",
    "calls_used",
    "micros_held",
    "micros_used",
    "reported_micros",
    "unmeasured_calls",
)


@dataclass(frozen=True, slots=True)
class Decision:
    reason: AdmissionReason
    granted_calls: int = 0
    held_micros: int = 0
    binding: CeilingSource | None = None

    @property
    def admitted(self) -> bool:
        return self.reason is AdmissionReason.ADMITTED


@dataclass(frozen=True, slots=True)
class _Bound:
    usable: int
    full: int
    source: CeilingSource


def decide(
    request: BackgroundWorkRequest,
    policy: BackgroundBudgetPolicy,
    *,
    month: PeriodTotals,
    day: PeriodTotals,
    external: Sequence[CeilingInput] = (),
) -> Decision:
    """Grant the most restrictive of every ceiling, or name the one that refuses.

    ``month``/``day`` are the request lane's counters; they bind only the
    background lane. Granted calls are at most ``max_calls`` and at least
    ``min_calls``, or nothing.
    """

    managed = request.locus == "managed"
    cost = request.cost_per_call_micros
    background = request.lane is BudgetLane.BACKGROUND
    # Whether the lane may run at all comes before any ceiling's arithmetic.
    if background and policy.policy_invalid:
        return Decision(AdmissionReason.POLICY_INVALID)
    if background and not policy.enabled:
        return Decision(AdmissionReason.BACKGROUND_DISABLED)
    # A zero estimate cannot prove a dollar ceiling holds any better than no estimate.
    measurable = cost is not None and cost > 0
    bounds: list[_Bound] = []
    for ceiling in external:
        if ceiling.remaining_calls is not None:
            left = max(0, ceiling.remaining_calls)
            bounds.append(_Bound(left, left, ceiling.source))
        if ceiling.remaining_micros is not None and managed:
            if not measurable:
                return Decision(AdmissionReason.COST_UNMEASURABLE, binding=ceiling.source)
            left = max(0, ceiling.remaining_micros) // cast(int, cost)
            bounds.append(_Bound(left, left, ceiling.source))
    if background and policy.binds(request.priority):
        percent = PRIORITY_ENVELOPE_PERCENT[request.priority]

        def envelope(cap: int, used: int, source: CeilingSource, per: int = 1) -> None:
            full = max(0, cap - used) // per
            usable = max(0, cap * percent // 100 - used) // per
            bounds.append(_Bound(usable, full, source))

        if policy.monthly_model_calls is not None:
            envelope(policy.monthly_model_calls, month.calls, CeilingSource.BACKGROUND_PERIOD)
        if policy.daily_model_calls is not None:
            envelope(policy.daily_model_calls, day.calls, CeilingSource.BACKGROUND_DAILY)
        if policy.monthly_managed_micros is not None and managed:
            if not measurable:
                return Decision(AdmissionReason.COST_UNMEASURABLE, binding=CeilingSource.MANAGED_SPEND)
            envelope(policy.monthly_managed_micros, month.micros, CeilingSource.MANAGED_SPEND, cast(int, cost))
    granted = min([request.max_calls, *(bound.usable for bound in bounds)])
    if granted >= request.min_calls:
        held = granted * cost if managed and cost is not None else 0
        return Decision(AdmissionReason.ADMITTED, granted_calls=granted, held_micros=held)
    hard = [bound for bound in bounds if bound.full < request.min_calls]
    if not hard:
        # Every ceiling has room; only the priority shares hold it back.
        binding = next(bound for bound in bounds if bound.usable < request.min_calls)
        return Decision(AdmissionReason.PRIORITY_RESERVED, binding=binding.source)
    return Decision(_REASON_FOR[hard[0].source], binding=hard[0].source)


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BackgroundAdmission:
    """What :func:`reserve_in` decided. ``replayed``: the key was already reserved."""

    reason: AdmissionReason
    lane: BudgetLane
    granted_calls: int = 0
    held_micros: int = 0
    state: Literal["held", "settled", "refused"] = "refused"
    replayed: bool = False
    binding: CeilingSource | None = None

    @property
    def admitted(self) -> bool:
        return self.reason is AdmissionReason.ADMITTED

    def to_wire(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "reason": self.reason.value,
            "lane": self.lane.value,
            "granted_calls": self.granted_calls,
            "held_usd": _usd(self.held_micros),
            "state": self.state,
            "replayed": self.replayed,
            "binding": None if self.binding is None else self.binding.value,
        }


@dataclass(frozen=True, slots=True)
class BudgetSettlement:
    calls_used: int
    micros_used: int
    cost_state: Literal["measured", "estimated", "unmeasured", "expired", "none"]
    replayed: bool = False


# --------------------------------------------------------------------------- #
# ledger transaction (backend-neutral SQL)
# --------------------------------------------------------------------------- #


class LedgerTransaction(Protocol):
    """One scope's open write transaction. Implementations: :class:`SqlLedgerTransaction`."""

    def lock_key(self, digest: str) -> None: ...

    def lock_lane(self, lane: BudgetLane) -> None: ...

    def ensure_period(self, lane: BudgetLane, kind: str, start: int, now: float) -> bool: ...

    def period(self, lane: BudgetLane, kind: str, start: int) -> PeriodTotals: ...

    def add_period(self, lane: BudgetLane, kind: str, start: int, delta: Mapping[str, int], now: float) -> None: ...

    def reservation(self, digest: str) -> dict[str, Any] | None: ...

    def insert_reservation(self, row: Mapping[str, Any]) -> None: ...

    def update_reservation(self, digest: str, fields: Mapping[str, Any]) -> None: ...

    def due_holds(self, lane: BudgetLane, now: float, limit: int) -> list[dict[str, Any]]: ...

    def collect(self, cutoff_month: int) -> int: ...

    def capability_usage(self, month: int) -> list[dict[str, Any]]: ...


_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r":([a-z_]+)")


class SqlLedgerTransaction:
    """The ledger SQL, once, over a DB-API connection already inside a transaction.

    ``paramstyle`` is ``named`` (sqlite3) or ``pyformat`` (psycopg). Every
    statement filters by ``org_id`` (the scope id), so a hosted row-level
    security policy and a local single-scope file see the same SQL.
    ``lock_lane`` serializes writers of one scope and lane: SQLite already
    holds the database write lock (``BEGIN IMMEDIATE``); PostgreSQL passes a
    transaction-scoped advisory lock.
    """

    def __init__(
        self,
        conn: Any,
        scope_id: str,
        *,
        paramstyle: Literal["named", "pyformat"],
        lock: Callable[[str], None] | None = None,
    ) -> None:
        self._conn = conn
        self._scope = scope_id
        self._pyformat = paramstyle == "pyformat"
        self._lock = lock

    def _sql(self, statement: str) -> str:
        return _PLACEHOLDER.sub(r"%(\1)s", statement) if self._pyformat else statement

    def _run(self, statement: str, **params: Any) -> Any:
        return self._conn.execute(self._sql(statement), {"org_id": self._scope, **params})

    @staticmethod
    def _dict(row: Any) -> dict[str, Any] | None:
        return None if row is None else {key: row[key] for key in row.keys()}

    def lock_key(self, digest: str) -> None:
        # Every writer of one key -- in any lane -- serializes here, before any lane lock.
        if self._lock is not None:
            self._lock(f"{self._scope}:key:{digest}")

    def lock_lane(self, lane: BudgetLane) -> None:
        if self._lock is not None:
            self._lock(f"{self._scope}:{lane.value}")

    def ensure_period(self, lane: BudgetLane, kind: str, start: int, now: float) -> bool:
        cursor = self._run(
            "INSERT INTO background_budget_periods (org_id, lane, period_kind, period_start, updated_at)"
            " VALUES (:org_id, :lane, :kind, :start, :now) ON CONFLICT DO NOTHING",
            lane=lane.value,
            kind=kind,
            start=int(start),
            now=float(now),
        )
        return int(cursor.rowcount or 0) > 0

    def period(self, lane: BudgetLane, kind: str, start: int) -> PeriodTotals:
        row = self._run(
            "SELECT " + ", ".join(_PERIOD_COLUMNS) + " FROM background_budget_periods"
            " WHERE org_id = :org_id AND lane = :lane AND period_kind = :kind AND period_start = :start",
            lane=lane.value,
            kind=kind,
            start=int(start),
        ).fetchone()
        return PeriodTotals.from_row(self._dict(row))

    def add_period(self, lane: BudgetLane, kind: str, start: int, delta: Mapping[str, int], now: float) -> None:
        moves = {name: int(value) for name, value in delta.items() if value}
        if not moves:
            return
        unknown = set(moves) - set(_PERIOD_COLUMNS)
        if unknown:
            raise ValueError("unknown period counter")
        assignments = ", ".join(f"{name} = {name} + :d_{name}" for name in moves)
        cursor = self._run(
            f"UPDATE background_budget_periods SET {assignments}, updated_at = :now"
            " WHERE org_id = :org_id AND lane = :lane AND period_kind = :kind AND period_start = :start",
            lane=lane.value,
            kind=kind,
            start=int(start),
            now=float(now),
            **{f"d_{name}": value for name, value in moves.items()},
        )
        if int(cursor.rowcount or 0) != 1:
            raise RuntimeError("background budget period row is missing")

    def reservation(self, digest: str) -> dict[str, Any] | None:
        row = self._run(
            "SELECT * FROM background_budget_reservations WHERE org_id = :org_id AND key_digest = :digest",
            digest=digest,
        ).fetchone()
        return self._dict(row)

    def insert_reservation(self, row: Mapping[str, Any]) -> None:
        names = list(row)
        self._run(
            "INSERT INTO background_budget_reservations (org_id, "
            + ", ".join(names)
            + ") VALUES (:org_id, "
            + ", ".join(f":{name}" for name in names)
            + ")",
            **row,
        )

    def update_reservation(self, digest: str, fields: Mapping[str, Any]) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        self._run(
            f"UPDATE background_budget_reservations SET {assignments} WHERE org_id = :org_id AND key_digest = :digest",
            digest=digest,
            **fields,
        )

    def due_holds(self, lane: BudgetLane, now: float, limit: int) -> list[dict[str, Any]]:
        rows = self._run(
            "SELECT * FROM background_budget_reservations"
            " WHERE org_id = :org_id AND lane = :lane AND state = 'held' AND expires_at <= :now"
            " ORDER BY expires_at, key_digest LIMIT :limit",
            lane=lane.value,
            now=float(now),
            limit=int(limit),
        ).fetchall()
        return [cast(dict[str, Any], self._dict(row)) for row in rows]

    def collect(self, cutoff_month: int) -> int:
        # Only settled rows go: a hold this old already expired (charged) on an
        # earlier admission, and a live one must keep its idempotency record.
        removed = self._run(
            "DELETE FROM background_budget_reservations"
            " WHERE org_id = :org_id AND state = 'settled' AND month_start < :cutoff",
            cutoff=int(cutoff_month),
        ).rowcount
        removed += self._run(
            "DELETE FROM background_budget_periods WHERE org_id = :org_id AND period_start < :cutoff"
            " AND NOT EXISTS (SELECT 1 FROM background_budget_reservations r WHERE r.org_id = :org_id"
            " AND r.state = 'held' AND r.lane = background_budget_periods.lane"
            " AND (r.month_start = background_budget_periods.period_start"
            " OR r.day_start = background_budget_periods.period_start))",
            cutoff=int(cutoff_month),
        ).rowcount
        return max(0, int(removed or 0))

    def capability_usage(self, month: int) -> list[dict[str, Any]]:
        # lc-debt: scans the month's reservation rows of one scope (one per admitted
        # attempt) to break usage down by capability. Upgrade: per-capability
        # period counters maintained beside background_budget_periods.
        rows = self._run(
            "SELECT lane, priority, capability,"
            " SUM(CASE WHEN state = 'held' THEN granted_calls ELSE 0 END) AS calls_held,"
            " SUM(calls_used) AS calls_used,"
            " SUM(CASE WHEN state = 'held' THEN held_micros ELSE 0 END) AS micros_held,"
            " SUM(micros_used) AS micros_used,"
            " SUM(reported_micros) AS reported_micros,"
            " SUM(unmeasured_calls) AS unmeasured_calls,"
            " COUNT(*) AS admissions"
            " FROM background_budget_reservations WHERE org_id = :org_id AND month_start = :month"
            " GROUP BY lane, priority, capability ORDER BY lane, priority, capability",
            month=int(month),
        ).fetchall()
        return [cast(dict[str, Any], self._dict(row)) for row in rows]


# --------------------------------------------------------------------------- #
# the algorithm (run inside one transaction by every backend)
# --------------------------------------------------------------------------- #


def _admission_from_row(row: Mapping[str, Any], *, replayed: bool) -> BackgroundAdmission:
    return BackgroundAdmission(
        reason=AdmissionReason.ADMITTED,
        lane=BudgetLane(str(row["lane"])),
        granted_calls=int(row["granted_calls"]),
        held_micros=int(row["held_micros"]),
        state="held" if str(row["state"]) == "held" else "settled",
        replayed=replayed,
    )


def reserve_in(
    txn: LedgerTransaction,
    request: BackgroundWorkRequest,
    policy: BackgroundBudgetPolicy,
    *,
    now: float,
    external: Sequence[CeilingInput] = (),
    retention_s: float = BACKGROUND_BUDGET_RETENTION_S,
) -> BackgroundAdmission:
    """Admit (and hold) or refuse one attempt, idempotently by its key.

    A key already reserved returns that reservation unchanged -- even if the
    envelope has since run out -- so a retried admission never charges twice.
    A refusal stores nothing: the same key may be admitted later.
    """

    lane = request.lane
    digest = request.key_digest
    fingerprint = request.fingerprint()
    # Key first, then lane (every caller keeps this order): the same key admitted
    # in two lanes at once meets here, and the second sees the first's row.
    txn.lock_key(digest)
    txn.lock_lane(lane)
    existing = txn.reservation(digest)
    if existing is not None:
        if str(existing["fingerprint"]) != fingerprint:
            raise BudgetConflict("budget idempotency key was reused for different work")
        return _admission_from_row(existing, replayed=True)
    month, day = month_start(now), day_start(now)
    opened = txn.ensure_period(lane, "month", month, now)
    txn.ensure_period(lane, "day", day, now)
    if opened:
        # The rollover is the closer: a new month expires the oldest accounting.
        txn.collect(month_start(now - retention_s))
    _expire_lane(txn, lane, now, _EXPIRY_BATCH)
    decision = decide(
        request,
        policy,
        month=txn.period(lane, "month", month),
        day=txn.period(lane, "day", day),
        external=external,
    )
    if not decision.admitted:
        return BackgroundAdmission(reason=decision.reason, lane=lane, binding=decision.binding)
    txn.insert_reservation(
        {
            "key_digest": digest,
            "fingerprint": fingerprint,
            "lane": lane.value,
            "priority": request.priority.value,
            "capability": request.capability,
            "locus": request.locus,
            "month_start": month,
            "day_start": day,
            "state": "held",
            "granted_calls": decision.granted_calls,
            "held_micros": decision.held_micros,
            "cost_per_call_micros": request.cost_per_call_micros,
            "calls_used": 0,
            "micros_used": 0,
            "reported_micros": 0,
            "unmeasured_calls": 0,
            "cost_state": "",
            "settle_fingerprint": "",
            "created_at": float(now),
            "expires_at": float(now) + float(request.hold_s),
            "settled_at": None,
        }
    )
    held = {"calls_held": decision.granted_calls, "micros_held": decision.held_micros}
    txn.add_period(lane, "month", month, held, now)
    txn.add_period(lane, "day", day, held, now)
    return BackgroundAdmission(
        reason=AdmissionReason.ADMITTED,
        lane=lane,
        granted_calls=decision.granted_calls,
        held_micros=decision.held_micros,
        state="held",
    )


def _expire_lane(txn: LedgerTransaction, lane: BudgetLane, now: float, limit: int) -> int:
    """Charge up to ``limit`` due holds of ``lane`` in full (the caller holds the lane lock)."""

    rows = txn.due_holds(lane, now, limit)
    for row in rows:
        _settle_row(txn, row, calls=int(row["granted_calls"]), cost_micros=None, now=now, expired=True)
    return len(rows)


#: Due holds one status read expires per lane before projecting.
# lc-debt: a status read expires at most this many due holds per lane, so a
# backlog larger than that (thousands of dead workers since the last admission)
# still shows some as held until the next read. Upgrade: expire from the
# maintenance pass once BI2 owns a schedule.
_STATUS_EXPIRY_LIMIT: Final[int] = 1024


def expire_in(txn: LedgerTransaction, *, now: float) -> int:
    """Expire every due hold of the scope (bounded), so a projection never shows a dead hold as live."""

    total = 0
    for lane in BudgetLane:
        txn.lock_lane(lane)
        expired = 0
        while expired < _STATUS_EXPIRY_LIMIT:
            batch = _expire_lane(txn, lane, now, min(_EXPIRY_BATCH, _STATUS_EXPIRY_LIMIT - expired))
            expired += batch
            if batch < _EXPIRY_BATCH:
                break
        total += expired
    return total


def extend_in(txn: LedgerTransaction, key: str, *, until: float, now: float) -> bool:
    """Keep a live hold alive until ``until`` (a renewed job lease). ``False`` when there is none.

    Never shortens a hold, never revives an expired one, and never reaches past
    the longest hold a request may ask for.
    """

    digest = key_digest(key)
    txn.lock_key(digest)
    row = txn.reservation(digest)
    if row is None:
        return False
    # The lane lock too (key, then lane): an expiry pass holds only the lane lock.
    txn.lock_lane(BudgetLane(str(row["lane"])))
    row = txn.reservation(digest)
    if row is None or str(row["state"]) != "held" or float(row["expires_at"]) <= float(now):
        return False
    target = min(float(until), float(now) + _MAX_HOLD_S)
    if target <= float(row["expires_at"]):
        return True
    txn.update_reservation(digest, {"expires_at": target})
    return True


def _amounts(row: Mapping[str, Any], calls: int, cost_micros: int | None) -> dict[str, Any]:
    """What a settlement of ``calls`` charges, by the reservation's locus and cost knowledge."""

    managed = str(row["locus"]) == "managed"
    per_call = row.get("cost_per_call_micros")
    if calls == 0 and not cost_micros:
        return {"micros_used": 0, "reported_micros": 0, "unmeasured_calls": 0, "cost_state": "none"}
    if managed:
        if cost_micros is not None:
            return {"micros_used": cost_micros, "reported_micros": 0, "unmeasured_calls": 0, "cost_state": "measured"}
        if per_call is not None:
            # Unreported managed cost is charged at the admission estimate, never as zero.
            estimate = int(per_call) * calls
            return {"micros_used": estimate, "reported_micros": 0, "unmeasured_calls": 0, "cost_state": "estimated"}
        return {"micros_used": 0, "reported_micros": 0, "unmeasured_calls": calls, "cost_state": "unmeasured"}
    if cost_micros is not None:
        return {"micros_used": 0, "reported_micros": cost_micros, "unmeasured_calls": 0, "cost_state": "measured"}
    return {"micros_used": 0, "reported_micros": 0, "unmeasured_calls": calls, "cost_state": "unmeasured"}


def _settle_row(
    txn: LedgerTransaction,
    row: Mapping[str, Any],
    *,
    calls: int,
    cost_micros: int | None,
    now: float,
    expired: bool = False,
) -> BudgetSettlement:
    granted = int(row["granted_calls"])
    if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
        raise ValueError("settled calls must be a non-negative integer")
    if cost_micros is not None and (
        isinstance(cost_micros, bool) or not isinstance(cost_micros, int) or cost_micros < 0
    ):
        raise ValueError("settled cost must be a non-negative integer of micros or None")
    if calls > granted:
        raise BudgetOverrun(f"settlement reports {calls} model calls; the reservation granted {granted}")
    lane = BudgetLane(str(row["lane"]))
    amounts = _amounts(row, calls, cost_micros)
    if expired:
        amounts["cost_state"] = "expired"
    fingerprint = "" if expired else _digest({"calls": calls, "cost_micros": cost_micros})
    if str(row["state"]) == "settled":
        if str(row["settle_fingerprint"]) == fingerprint and fingerprint:
            return BudgetSettlement(
                calls_used=int(row["calls_used"]),
                micros_used=int(row["micros_used"]),
                cost_state=cast(Any, str(row["cost_state"])),
                replayed=True,
            )
        if str(row["cost_state"]) != "expired" or str(row["settle_fingerprint"]) or expired:
            raise BudgetConflict("budget reservation was already settled with different usage")
        # A late truthful settlement corrects the conservative expiry charge once.
        delta = {
            "calls_used": calls - int(row["calls_used"]),
            "micros_used": int(amounts["micros_used"]) - int(row["micros_used"]),
            "reported_micros": int(amounts["reported_micros"]) - int(row["reported_micros"]),
            "unmeasured_calls": int(amounts["unmeasured_calls"]) - int(row["unmeasured_calls"]),
        }
    else:
        delta = {
            "calls_held": -granted,
            "micros_held": -int(row["held_micros"]),
            "calls_used": calls,
            "micros_used": int(amounts["micros_used"]),
            "reported_micros": int(amounts["reported_micros"]),
            "unmeasured_calls": int(amounts["unmeasured_calls"]),
        }
    # Charged to the periods it was admitted in, whatever period it settles in.
    txn.add_period(lane, "month", int(row["month_start"]), delta, now)
    txn.add_period(lane, "day", int(row["day_start"]), delta, now)
    txn.update_reservation(
        str(row["key_digest"]),
        {
            "state": "settled",
            "calls_used": calls,
            "micros_used": int(amounts["micros_used"]),
            "reported_micros": int(amounts["reported_micros"]),
            "unmeasured_calls": int(amounts["unmeasured_calls"]),
            "cost_state": str(amounts["cost_state"]),
            "settle_fingerprint": fingerprint,
            "settled_at": float(now),
        },
    )
    return BudgetSettlement(
        calls_used=calls, micros_used=int(amounts["micros_used"]), cost_state=cast(Any, amounts["cost_state"])
    )


def settle_in(
    txn: LedgerTransaction,
    key: str,
    *,
    calls: int,
    cost_micros: int | None,
    now: float,
) -> BudgetSettlement:
    """Charge what one admitted attempt actually used and release the rest of its hold.

    ``calls`` may not exceed the grant (:class:`BudgetOverrun`). ``cost_micros``
    is the provider-reported cost, or ``None`` when the route reported none.
    Settling ``calls=0, cost_micros=0`` releases the hold without a charge.
    """

    digest = key_digest(key)
    txn.lock_key(digest)
    row = txn.reservation(digest)
    if row is None:
        raise UnknownReservation("no budget reservation for this key")
    txn.lock_lane(BudgetLane(str(row["lane"])))
    locked = txn.reservation(digest)
    if locked is None:  # purged between the read and the lock
        raise UnknownReservation("no budget reservation for this key")
    return _settle_row(txn, locked, calls=calls, cost_micros=cost_micros, now=now)


# --------------------------------------------------------------------------- #
# the AI budget's meter (P8-4)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AiUse:
    """One UTC month of model calls on this machine, for the personal AI budget.

    ``micros`` is the cost that is known; ``unreported_calls`` is how many calls
    had no cost to add, so ``complete`` is False while any exists and ``micros``
    is then a lower bound, never a total.
    """

    calls: int = 0
    micros: int = 0
    unreported_calls: int = 0

    @property
    def complete(self) -> bool:
        return self.unreported_calls <= 0


def meter_in(
    txn: LedgerTransaction,
    *,
    calls: int,
    cost_micros: int | None,
    now: float,
    retention_s: float = BACKGROUND_BUDGET_RETENTION_S,
) -> None:
    """Record ``calls`` model calls that no admission metered: the interactive lane's month and day counters.

    ``cost_micros`` is their whole known cost, or ``None`` when it is not known
    (the calls are then counted as unmeasured, never as free). Rows are written
    here and only here; the rollover that opens a new month deletes the
    accounting past the retention horizon, as :func:`reserve_in` does.
    """

    if isinstance(calls, bool) or not isinstance(calls, int) or calls < 1:
        raise ValueError("metered calls must be a positive integer")
    if cost_micros is not None and (
        isinstance(cost_micros, bool) or not isinstance(cost_micros, int) or cost_micros < 0
    ):
        raise ValueError("metered cost must be a non-negative integer of micros or None")
    lane = BudgetLane.INTERACTIVE
    txn.lock_lane(lane)
    month, day = month_start(now), day_start(now)
    opened = txn.ensure_period(lane, "month", month, now)
    txn.ensure_period(lane, "day", day, now)
    if opened:
        txn.collect(month_start(now - retention_s))
    delta = {
        "calls_used": calls,
        "reported_micros": 0 if cost_micros is None else cost_micros,
        "unmeasured_calls": calls if cost_micros is None else 0,
    }
    txn.add_period(lane, "month", month, delta, now)
    txn.add_period(lane, "day", day, delta, now)


def ai_use_in(txn: LedgerTransaction, *, now: float) -> AiUse:
    """The month's model calls and known cost across both lanes (explicit work and the background envelope).

    Calls an admission metered are settled in their lane; calls nothing admitted
    are metered by :func:`meter_in`. No call is in both: a metered call inside an
    admission is added to that admission's settlement instead.
    """

    month = month_start(now)
    totals = [txn.period(lane, "month", month) for lane in BudgetLane]
    return AiUse(
        calls=sum(item.calls_used for item in totals),
        micros=sum(item.micros_used + item.reported_micros for item in totals),
        unreported_calls=sum(item.unmeasured_calls for item in totals),
    )


# --------------------------------------------------------------------------- #
# projection (Usage, BI2 admission preview)
# --------------------------------------------------------------------------- #


def _usd(micros: int | None) -> float | None:
    return None if micros is None else round(int(micros) / 1_000_000, 6)


def _remaining(cap: int | None, used: int) -> int | None:
    return None if cap is None else max(0, cap - used)


def _cost_fields(managed_micros: int, reported_micros: int, unmeasured_calls: int) -> dict[str, Any]:
    """Spend figures that never read as a complete total while some calls' cost is unknown.

    With any unmeasured-cost call, ``managed_usd_used`` and ``reported_other_usd``
    are ``None`` and ``cost_complete`` is ``False``; the ``measured_*`` figures
    keep the part that is known (a lower bound, not a total).
    """

    complete = unmeasured_calls <= 0
    return {
        "cost_complete": complete,
        "managed_usd_used": _usd(managed_micros) if complete else None,
        "reported_other_usd": _usd(reported_micros) if complete else None,
        "measured_managed_usd": _usd(managed_micros),
        "measured_other_usd": _usd(reported_micros),
        "unmeasured_cost_calls": max(0, unmeasured_calls),
    }


def build_status(
    policy: BackgroundBudgetPolicy,
    *,
    now: float,
    periods: Mapping[tuple[BudgetLane, str], PeriodTotals],
    capabilities: Sequence[Mapping[str, Any]] = (),
    managed_available: bool,
) -> dict[str, Any]:
    """The one Usage/BI2 projection of a scope's envelope. Wire shape, no identifiers beyond capability ids.

    ``managed_available``: whether this deployment has LemonCrow-managed
    execution at all (hosted). Locally the dollar ceiling cannot apply, and
    the projection says so instead of showing a spend of zero.
    """

    month = month_start(now)
    background = periods.get((BudgetLane.BACKGROUND, "month"), PeriodTotals())
    today = periods.get((BudgetLane.BACKGROUND, "day"), PeriodTotals())
    interactive = periods.get((BudgetLane.INTERACTIVE, "month"), PeriodTotals())

    def priority_state(priority: JobPriority) -> tuple[str, AdmissionReason]:
        probe = BackgroundWorkRequest(key="status", capability="status", priority=priority, max_calls=1)
        decision = decide(probe, policy, month=background, day=today)
        if decision.admitted:
            return "running", AdmissionReason.ADMITTED
        if decision.reason in (AdmissionReason.POLICY_INVALID, AdmissionReason.BACKGROUND_DISABLED):
            return "disabled", decision.reason
        return "paused", decision.reason

    priorities = []
    for priority in sorted(PRIORITY_ENVELOPE_PERCENT, key=lambda item: item.rank):
        if lane_for(priority) is not BudgetLane.BACKGROUND:
            continue
        priority_run, priority_reason = priority_state(priority)
        priorities.append(
            {
                "priority": priority.value,
                "share_percent": PRIORITY_ENVELOPE_PERCENT[priority],
                "state": priority_run,
                "reason": priority_reason.value,
            }
        )
    # The envelope's state is that of the highest background priority it binds
    # (an unconfigured envelope does not bind automatic Review: BI2).
    bound = [item for item in priorities if policy.binds(JobPriority(str(item["priority"])))]
    state, reason = str(bound[0]["state"]), str(bound[0]["reason"])
    managed_cap = policy.monthly_managed_micros if managed_available else None
    if state == "running" and managed_cap is not None and background.micros >= managed_cap:
        # Calls remain, but managed work is paused: local/worker routes may still run.
        state, reason = "limited", AdmissionReason.MANAGED_SPEND_EXHAUSTED.value
    elif state == "running" and any(item["state"] == "paused" for item in bound):
        state, reason = "limited", AdmissionReason.PRIORITY_RESERVED.value
    return {
        "available": True,
        "enabled": policy.enabled,
        "state": state,
        "reason": reason,
        "period": {"kind": "month", "start_unix_s": month, "end_unix_s": next_month_start(month)},
        "policy": policy.to_wire(),
        "background": {
            "model_calls_used": background.calls_used,
            "model_calls_held": background.calls_held,
            "model_calls_remaining": _remaining(policy.monthly_model_calls, background.calls),
            "today_model_calls": today.calls,
            "today_model_calls_remaining": _remaining(policy.daily_model_calls, today.calls),
            **_cost_fields(background.micros_used, background.reported_micros, background.unmeasured_calls),
            "managed_usd_held": _usd(background.micros_held),
            "managed_usd_remaining": _usd(_remaining(managed_cap, background.micros)),
        },
        "interactive": {
            "model_calls_used": interactive.calls_used,
            "model_calls_held": interactive.calls_held,
            **_cost_fields(interactive.micros_used, interactive.reported_micros, interactive.unmeasured_calls),
            "limited_by_background_envelope": False,
        },
        "managed_spend": {
            "applies": managed_available,
            "enforced": managed_cap is not None,
            "note": (
                "the dollar ceiling binds LemonCrow-managed execution; work whose cost cannot be estimated is refused"
                if managed_available
                else "no LemonCrow-managed execution here: managed work is refused (managed_unavailable); model "
                "calls are counted, and cost is shown only where the provider reported it"
            ),
        },
        "priorities": priorities,
        "automatic_review": {
            "envelope_enforced": policy.binds(JobPriority.REVIEW_AUTO),
            "note": (
                "automatic Review is metered and limited by this envelope"
                if policy.binds(JobPriority.REVIEW_AUTO)
                else "automatic Review is metered here; its call ceilings bind once a background budget is set"
            ),
        },
        "by_capability": [
            {
                "lane": str(row["lane"]),
                "priority": str(row["priority"]),
                "capability": str(row["capability"]),
                "admissions": int(row["admissions"] or 0),
                "model_calls_used": int(row["calls_used"] or 0),
                "model_calls_held": int(row["calls_held"] or 0),
                **_cost_fields(
                    int(row["micros_used"] or 0), int(row["reported_micros"] or 0), int(row["unmeasured_calls"] or 0)
                ),
            }
            for row in capabilities
        ],
    }


def read_status(
    txn: LedgerTransaction, policy: BackgroundBudgetPolicy, *, now: float, managed_available: bool
) -> dict[str, Any]:
    """Project one scope's counters, first expiring every due hold (so ``txn`` must be a write transaction).

    A hold whose worker died is shown as what it is -- expired and charged in
    full -- even if no admission ran in its lane since.
    """

    expire_in(txn, now=now)
    month, day = month_start(now), day_start(now)
    periods = {
        (lane, kind): txn.period(lane, kind, start)
        for lane in BudgetLane
        for kind, start in (("month", month), ("day", day))
    }
    return build_status(
        policy,
        now=now,
        periods=periods,
        capabilities=txn.capability_usage(month),
        managed_available=managed_available,
    )
