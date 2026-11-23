"""Background Intelligence capability registry and scheduling contract (BI2).

One path from a durable event to model work, on the job planes that already
exist::

    durable event -> capability applicability -> effective intelligence policy
      -> budget admission + priority -> existing local/hosted job plane
      -> shared intelligence runtime -> settle

This module is that path's contract. It is a registry, not another scheduler
or runtime: hosted work rides the RC8 durable job plane (lease, heartbeat,
reclaim, idempotency, tenant fairness), local work rides the jobs table that
the opportunistic worker tick drains (:mod:`.background_local`), and every
model call still goes through the intelligence runtime.

**Registry.** Six capabilities are declared with their trigger kinds, fixed
priority, max model calls per execution, identity inputs, allowed loci,
deterministic fallback and result kind. ``review.auto_ai`` is an adapter over
the existing automatic AI Review, whose per-Review authority stays the Review
run plan. The two Project Knowledge capabilities have local executors (PK1,
:mod:`.project_knowledge_narration`); a plane runs them only where it
registers one, so they are declared for the local locus only. The discussion
capabilities are declarations whose executors land with DS0; until then
scheduling them is refused with ``executor_unavailable``.

**Enablement** is one conjunction: BI0's effective policy (on unless an operator layer turned it off)
and ``intelligence.background.enabled``, and the background envelope's own
switch (the hosted policy file's ``background`` section, or the local
settings). Anything off or invalid is a typed refusal, never an exception.

**Locus.** BI0's ``run_on`` is honoured and never silently degraded: an
explicit locus this deployment or capability does not offer is refused with
``route_unavailable``. For ``review.auto_ai`` the run plan names the
executor; ``run_on`` may agree with it but never redirects it.

**Exactly-once accounting.** :func:`run_admitted` checks currentness,
reserves through the BI1 ledger, re-checks, runs, and settles exactly once in
every outcome: success (what was used), a stale job (released, nothing
charged), a failure (charged in full: the calls may have been made).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.ai_budget import admission_metering
from lemoncrow.pro.capabilities.review.background_budget import (
    AdmissionReason,
    BackgroundAdmission,
    BackgroundBudgetPolicy,
    BackgroundBudgetPolicyError,
    BackgroundWorkRequest,
    CeilingSource,
    JobPriority,
    day_start,
    month_start,
    next_month_start,
    usd_to_micros,
)
from lemoncrow.pro.capabilities.review.intelligence_policy import AUTO, RUN_ON_LOCI, EffectiveIntelligencePolicy

__all__ = [
    "BACKGROUND_CAPABILITIES",
    "BUDGET_RETRY_POLL_S",
    "CHANGE_LANDED",
    "FEEDBACK_REPEATED",
    "KNOWLEDGE_BOOTSTRAP",
    "NARRATION_REQUESTED",
    "PAUSING_REASONS",
    "REVIEW_AUTO_AI",
    "SNAPSHOT_CAPTURED",
    "SOURCE_ADVANCED",
    "THREAD_CHANGED",
    "THREAD_SUMMARIES_CHANGED",
    "AdmittedRun",
    "BackgroundCapability",
    "BackgroundEvent",
    "BackgroundOutcome",
    "SchedulerReason",
    "SchedulingDecision",
    "background_enablement",
    "budget_retry_after_s",
    "capability",
    "envelope_under_policy",
    "job_identity",
    "plan_background_work",
    "reported_cost_micros",
    "resolve_locus",
    "run_admitted",
]

_LOG = logging.getLogger(__name__)

_TOKEN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class SchedulerReason(StrEnum):
    """Why scheduling went the way it did (budget refusals use :class:`AdmissionReason`)."""

    SCHEDULED = "scheduled"
    UNKNOWN_CAPABILITY = "unknown_capability"
    #: The event is not one of the capability's trigger kinds.
    NOT_APPLICABLE = "not_applicable"
    #: The capability is declared, but no executor for it is registered on this plane (DS0 has not landed).
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    #: BI0's effective intelligence policy or the envelope settings are malformed.
    POLICY_INVALID = "policy_invalid"
    #: An operator layer turned AI off: no model work at all.
    INTELLIGENCE_DISABLED = "intelligence_disabled"
    #: BI0 ``intelligence.background.enabled`` or the envelope's own switch is off.
    BACKGROUND_DISABLED = "background_disabled"
    #: The explicit ``run_on`` locus is not offered here, or disagrees with the run plan's executor.
    ROUTE_UNAVAILABLE = "route_unavailable"
    #: A newer source generation, policy or content-access authority replaced this work.
    SUPERSEDED = "superseded"


#: Budget refusals that clear with time or a settings change: the work pauses
#: (local: the job waits for a later tick) instead of being dropped.
PAUSING_REASONS: Final[frozenset[str]] = frozenset(
    {
        AdmissionReason.BACKGROUND_EXHAUSTED.value,
        AdmissionReason.DAILY_GUARD_EXHAUSTED.value,
        AdmissionReason.MANAGED_SPEND_EXHAUSTED.value,
        AdmissionReason.PRIORITY_RESERVED.value,
        AdmissionReason.HOSTED_QUOTA_EXHAUSTED.value,
        AdmissionReason.AI_BUDGET_EXHAUSTED.value,
        AdmissionReason.POLICY_INVALID.value,
    }
)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

#: Event kinds (durable events only: a browser read is never one).
SNAPSHOT_CAPTURED: Final[str] = "review.snapshot_captured"
CHANGE_LANDED: Final[str] = "knowledge.change_landed"
SOURCE_ADVANCED: Final[str] = "knowledge.source_advanced"
KNOWLEDGE_BOOTSTRAP: Final[str] = "knowledge.bootstrap"
#: A person asked for a fresh narration of one stored narrative (UX0b regenerate).
NARRATION_REQUESTED: Final[str] = "knowledge.narration_requested"
THREAD_CHANGED: Final[str] = "review.thread_changed"
THREAD_SUMMARIES_CHANGED: Final[str] = "review.thread_summaries_changed"
FEEDBACK_REPEATED: Final[str] = "review.feedback_repeated"

REVIEW_AUTO_AI: Final[str] = "review.auto_ai"

_ALL_LOCI: Final[tuple[str, ...]] = ("local", "worker", "managed")


@dataclass(frozen=True, slots=True)
class BackgroundCapability:
    """One autonomous capability's declaration (plan §5.5).

    ``loci`` is in preference order for ``run_on: auto``. ``executor_package``
    names the package whose executor has not landed yet; empty means the
    capability has an executor. Changing what a capability computes bumps
    ``version``, which is part of every job identity.
    """

    id: str
    version: int
    triggers: frozenset[str]
    priority: JobPriority
    max_calls: int
    identity_inputs: tuple[str, ...]
    loci: tuple[str, ...]
    deterministic_fallback: bool
    result_kind: str
    #: AI content-access rules apply before any byte or derived context reaches a model.
    content_access: str = "ai_content_access_rules"
    executor_package: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "triggers": sorted(self.triggers),
            "priority": self.priority.value,
            "max_model_calls": self.max_calls,
            "identity_inputs": list(self.identity_inputs),
            "loci": list(self.loci),
            "deterministic_fallback": self.deterministic_fallback,
            "result_kind": self.result_kind,
            "content_access": self.content_access,
            "executor": "available" if not self.executor_package else f"lands with {self.executor_package}",
        }


# The max-call bounds of the not-yet-landed (DS0b) capabilities are declarations
# their package may revise (bumping ``version``); review.auto_ai's matches the
# hosted job's default and the run plan's per-Review ceiling cap (24).
BACKGROUND_CAPABILITIES: Final[Mapping[str, BackgroundCapability]] = {
    capability.id: capability
    for capability in (
        BackgroundCapability(
            id=REVIEW_AUTO_AI,
            version=1,
            triggers=frozenset({SNAPSHOT_CAPTURED}),
            priority=JobPriority.REVIEW_AUTO,
            max_calls=24,
            identity_inputs=(
                "review_id",
                "semantic_snapshot_digest",
                "effort",
                "policy_digest",
                "content_access_digest",
                "run_plan_digest",
            ),
            loci=_ALL_LOCI,
            deterministic_fallback=True,
            result_kind="review_findings",
        ),
        # PK1: local executors only (no hosted plane originates landed history yet).
        BackgroundCapability(
            id="knowledge.change_narration",
            version=1,
            triggers=frozenset({CHANGE_LANDED, NARRATION_REQUESTED}),
            priority=JobPriority.KNOWLEDGE_REFRESH,
            max_calls=2,
            identity_inputs=("source_generation", "policy_digest", "content_access_digest", "version", "route"),
            loci=("local",),
            deterministic_fallback=True,
            result_kind="knowledge_narrative",
        ),
        # A landed Change and a direct canonical advance both refresh the subjects they touched;
        # a person's regenerate request refreshes the one subject it names.
        BackgroundCapability(
            id="knowledge.repository_refresh",
            version=1,
            triggers=frozenset({SOURCE_ADVANCED, KNOWLEDGE_BOOTSTRAP, CHANGE_LANDED, NARRATION_REQUESTED}),
            priority=JobPriority.KNOWLEDGE_REFRESH,
            max_calls=8,
            identity_inputs=("source_generation", "policy_digest", "content_access_digest", "version", "route"),
            loci=("local",),
            deterministic_fallback=True,
            result_kind="knowledge_narrative",
        ),
        # DS0: a local executor only (the hosted Reader does not offer thread summaries yet).
        BackgroundCapability(
            id="review.thread_summary",
            version=1,
            triggers=frozenset({THREAD_CHANGED}),
            priority=JobPriority.DISCUSSION_SUMMARY,
            max_calls=1,
            identity_inputs=("review_id", "thread_id", "message_digest", "content_access_digest", "version", "route"),
            loci=("local",),
            deterministic_fallback=False,
            result_kind="thread_summary",
        ),
        # The digest and the AI rule suggestion have no executor: deterministic repeated-feedback
        # suggestions and explicit adoption (rule_suggestions) are the supported path.
        # lc-debt: DS0b owns an AI feedback digest/rule suggestion; until then they answer executor_unavailable.
        BackgroundCapability(
            id="review.feedback_digest",
            version=1,
            triggers=frozenset({THREAD_SUMMARIES_CHANGED}),
            priority=JobPriority.DISCUSSION_SUMMARY,
            max_calls=1,
            identity_inputs=("review_id", "thread_summary_digest", "version", "route"),
            loci=_ALL_LOCI,
            deterministic_fallback=False,
            result_kind="feedback_digest",
            executor_package="DS0b",
        ),
        BackgroundCapability(
            id="review.rule_suggestion",
            version=1,
            triggers=frozenset({FEEDBACK_REPEATED}),
            priority=JobPriority.LEARNING_SUGGESTION,
            max_calls=2,
            identity_inputs=("repository", "feedback_digest", "version", "route"),
            loci=_ALL_LOCI,
            deterministic_fallback=False,
            result_kind="rule_suggestion",
            executor_package="DS0b",
        ),
    )
}


def capability(capability_id: str) -> BackgroundCapability | None:
    return BACKGROUND_CAPABILITIES.get(str(capability_id))


# --------------------------------------------------------------------------- #
# events, enablement, locus, identity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BackgroundEvent:
    """A durable event: its kind and the digest of the source generation/input it is about."""

    kind: str
    source_digest: str
    content_access_digest: str = ""

    def __post_init__(self) -> None:
        if _TOKEN.fullmatch(str(self.kind)) is None:
            raise ValueError("an event kind must be a bounded machine token")
        if not self.source_digest or len(self.source_digest) > 256:
            raise ValueError("an event needs a source digest of at most 256 characters")
        if len(self.content_access_digest) > 256:
            raise ValueError("a content-access digest is at most 256 characters")

    def to_wire(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "source_digest": self.source_digest,
            "content_access_digest": self.content_access_digest,
        }


def background_enablement(
    policy: EffectiveIntelligencePolicy | None, envelope: BackgroundBudgetPolicy | None
) -> SchedulerReason | None:
    """``None`` when background work may run; otherwise the typed reason it may not.

    The conjunction of BI0's operator AI-on and
    ``intelligence.background.enabled`` and the envelope's own switch.
    """

    if policy is not None:
        if not policy.valid:
            return SchedulerReason.POLICY_INVALID
        if not policy.enabled.value:
            return SchedulerReason.INTELLIGENCE_DISABLED
        if not policy.background_enabled.value:
            return SchedulerReason.BACKGROUND_DISABLED
    if envelope is not None:
        if envelope.policy_invalid:
            return SchedulerReason.POLICY_INVALID
        if not envelope.enabled:
            return SchedulerReason.BACKGROUND_DISABLED
    return None


def envelope_under_policy(
    envelope: BackgroundBudgetPolicy, policy: EffectiveIntelligencePolicy
) -> BackgroundBudgetPolicy:
    """The envelope as BI0's effective policy leaves it: closed when invalid, off when either switch is."""

    if not policy.valid:
        return BackgroundBudgetPolicy.invalid()
    if envelope.enabled and not (policy.enabled.value and policy.background_enabled.value):
        return replace(envelope, enabled=False)
    return envelope


def resolve_locus(
    entry: BackgroundCapability,
    policy: EffectiveIntelligencePolicy | None,
    *,
    offered: Sequence[str],
    planned: str | None = None,
) -> str | SchedulerReason:
    """Where the work runs, or ``route_unavailable``. Never a silent fallback.

    ``offered`` is what this deployment runs (locally ``("local",)``). With
    ``planned`` (the run plan's executor) that locus is the answer, and an
    explicit ``run_on`` must agree with it.
    """

    choices = [locus for locus in entry.loci if locus in offered and locus in RUN_ON_LOCI]
    run_on = AUTO if policy is None else str(policy.run_on.value)
    if planned is not None:
        if planned not in choices or (run_on != AUTO and run_on != planned):
            return SchedulerReason.ROUTE_UNAVAILABLE
        return planned
    if run_on == AUTO:
        return choices[0] if choices else SchedulerReason.ROUTE_UNAVAILABLE
    return run_on if run_on in choices else SchedulerReason.ROUTE_UNAVAILABLE


def job_identity(
    entry: BackgroundCapability,
    *,
    source_digest: str,
    policy_digest: str,
    content_access_digest: str,
    route: Mapping[str, str],
) -> str:
    """The semantic identity of one background job (plan §6.4): duplicates of an event converge on it.

    Source generation/input digest, policy digest, content-access digest,
    capability id and version, and the route identity the output depends on.
    """

    document = {
        "capability": entry.id,
        "version": entry.version,
        "source": source_digest,
        "policy": policy_digest,
        "content_access": content_access_digest,
        "route": {str(key): str(value) for key, value in sorted(route.items())},
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SchedulingDecision:
    reason: str
    capability: str
    priority: JobPriority | None = None
    locus: str = ""
    identity: str = ""

    @property
    def scheduled(self) -> bool:
        return self.reason == SchedulerReason.SCHEDULED.value

    def to_wire(self) -> dict[str, Any]:
        return {
            "scheduled": self.scheduled,
            "reason": self.reason,
            "capability": self.capability,
            "priority": None if self.priority is None else self.priority.value,
            "locus": self.locus,
        }


def plan_background_work(
    capability_id: str,
    event: BackgroundEvent,
    *,
    policy: EffectiveIntelligencePolicy | None,
    envelope: BackgroundBudgetPolicy | None,
    offered: Sequence[str],
    planned: str | None = None,
    route: Mapping[str, str] | None = None,
    executors: Collection[str] = (),
) -> SchedulingDecision:
    """Applicability, executor, enablement and locus of one event for one capability.

    Budget admission is not decided here: it happens at dispatch, where the
    calls are spent (:func:`run_admitted`).
    """

    entry = capability(capability_id)
    if entry is None:
        return SchedulingDecision(SchedulerReason.UNKNOWN_CAPABILITY.value, str(capability_id)[:128])

    def refused(reason: SchedulerReason) -> SchedulingDecision:
        return SchedulingDecision(reason.value, entry.id, entry.priority)

    if event.kind not in entry.triggers:
        return refused(SchedulerReason.NOT_APPLICABLE)
    if entry.executor_package and entry.id not in executors:
        return refused(SchedulerReason.EXECUTOR_UNAVAILABLE)
    refusal = background_enablement(policy, envelope)
    if refusal is not None:
        return refused(refusal)
    locus = resolve_locus(entry, policy, offered=offered, planned=planned)
    if isinstance(locus, SchedulerReason):
        return refused(locus)
    identity = job_identity(
        entry,
        source_digest=event.source_digest,
        policy_digest="" if policy is None else policy.digest,
        content_access_digest=event.content_access_digest,
        route={"locus": locus, **dict(route or {})},
    )
    return SchedulingDecision(SchedulerReason.SCHEDULED.value, entry.id, entry.priority, locus, identity)


# --------------------------------------------------------------------------- #
# execution: reserve, run, settle exactly once
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BackgroundOutcome:
    """What one execution used: model calls, the provider-reported cost (micros) if any, its result."""

    calls_used: int
    cost_micros: int | None = None
    result: Any = None


@dataclass(frozen=True, slots=True)
class AdmittedRun:
    status: Literal["completed", "refused", "superseded"]
    reason: str
    admission: BackgroundAdmission | None = None
    calls_used: int = 0
    result: Any = None

    @property
    def paused(self) -> bool:
        """Refused by a budget limit that clears with time or settings: the work waits, it is not dropped."""

        return self.status == "refused" and self.reason in PAUSING_REASONS

    def to_wire(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "calls_used": self.calls_used,
            "admission": None if self.admission is None else self.admission.to_wire(),
        }


def reported_cost_micros(cost: object, currency: object) -> int | None:
    """A provider-reported cost as micros, or ``None`` when there is none or it is not in USD."""

    if cost is None or str(currency or "USD").upper() != "USD":
        return None
    try:
        return usd_to_micros(cost, key="provider_cost")
    except BackgroundBudgetPolicyError:
        return None


#: How long a paused (budget-refused) queued job waits before it asks the ledger
#: again. Room returns whenever a hold settles or expires, so the job polls; a
#: day or month reset that comes sooner shortens the wait.
BUDGET_RETRY_POLL_S: Final[float] = 300.0


def budget_retry_after_s(run: AdmittedRun, *, now: float, poll_s: float = BUDGET_RETRY_POLL_S) -> float | None:
    """Seconds a paused run waits before its job asks again; ``None`` when the refusal is not a pausing one."""

    if not run.paused:
        return None
    binding = None if run.admission is None else run.admission.binding
    reset: float | None = None
    if binding is CeilingSource.BACKGROUND_DAILY:
        reset = float(day_start(now) + 86_400)
    elif binding in {CeilingSource.BACKGROUND_PERIOD, CeilingSource.MANAGED_SPEND}:
        reset = float(next_month_start(month_start(now)))
    delay = poll_s if reset is None else min(poll_s, reset - float(now))
    return max(1.0, float(delay))


def run_admitted(
    request: BackgroundWorkRequest,
    *,
    reserve: Callable[[BackgroundWorkRequest], BackgroundAdmission],
    settle: Callable[[str, int, int | None], object],
    current: Callable[[], bool],
    execute: Callable[[int], BackgroundOutcome],
    checked: bool = False,
) -> AdmittedRun:
    """Check currentness, reserve, re-check, execute within the grant, and settle exactly once.

    ``checked``: the caller validated currentness immediately before (only the
    post-reservation check runs). ``request.key`` must be fresh per execution
    attempt. A refusal reserves nothing; a stale job after admission is
    released uncharged; a failed execution is charged its whole grant (its
    calls may have been made) and the error propagates.

    A settlement that fails (the ledger is unreachable, or rejects it) is
    logged and never replaces the run's outcome: the hold stays, and the BI1
    ledger charges it its whole grant when it expires (a later truthful
    settlement corrects that once).
    """

    if not checked and not current():
        return AdmittedRun("superseded", SchedulerReason.SUPERSEDED.value)
    admission = reserve(request)
    if not admission.admitted:
        return AdmittedRun("refused", admission.reason.value, admission)
    if admission.replayed and admission.state == "settled":
        # This attempt already ran and settled: it never runs (or charges) twice.
        return AdmittedRun("completed", AdmissionReason.ADMITTED.value, admission)
    granted = admission.granted_calls
    # Until the executor reports, the conservative charge is the whole grant.
    calls, cost = granted, None
    try:
        if not current():
            calls, cost = 0, 0
            return AdmittedRun("superseded", SchedulerReason.SUPERSEDED.value, admission)
        # Calls made inside the execution are added up here and settled with it, never metered a second time.
        with admission_metering() as metered:
            outcome = execute(granted)
        used = max(0, int(outcome.calls_used))
        if used > granted:
            _LOG.warning("background %s reported %d model calls over a grant of %d", request.capability, used, granted)
        calls, cost = min(used, granted), (
            outcome.cost_micros if outcome.cost_micros is not None else metered.cost_micros()
        )
        return AdmittedRun("completed", AdmissionReason.ADMITTED.value, admission, calls, outcome.result)
    finally:
        try:
            settle(request.key, calls, cost)
        except Exception:
            # The hold is kept: it expires charged its whole grant, never free.
            _LOG.warning(
                "background %s settlement of %d model calls failed; its hold expires charged in full",
                request.capability,
                calls,
                exc_info=True,
            )
