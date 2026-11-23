"""Local Background Intelligence on the existing jobs table (BI2).

No new queue: an admitted event becomes one ``background_intelligence`` row in
the local jobs table (SQLite or PostgreSQL), keyed by the job's semantic
identity, and the worker that already drains that table (the MCP worker tick
or ``lemoncrow worker``) runs it. Duplicate events converge on one row; a job
refused by a budget limit that clears with time pauses (no attempt spent) and
the maintenance tick resumes it; a job whose policy changed since it was
queued is superseded before it reserves anything.

Local automatic AI Review stays explicit (a ``trigger: auto`` request, never
an originated job): :func:`run_local_review_auto` meters it in the same
ledger. The Project Knowledge narration executors (PK1) and the thread
summary executor (DS0) are registered for a root's worker; the feedback digest
and rule suggestion capabilities have none (DS0b), so originating them is
refused with ``executor_unavailable``.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from lemoncrow.core.service.jobs import JOB_BACKGROUND_INTELLIGENCE, JobPaused
from lemoncrow.pro.capabilities.review.background_budget import BackgroundWorkRequest
from lemoncrow.pro.capabilities.review.background_budget_store import LocalBackgroundBudgetStore
from lemoncrow.pro.capabilities.review.background_scheduler import (
    REVIEW_AUTO_AI,
    SNAPSHOT_CAPTURED,
    AdmittedRun,
    BackgroundEvent,
    BackgroundOutcome,
    SchedulerReason,
    SchedulingDecision,
    background_enablement,
    capability,
    plan_background_work,
    run_admitted,
)
from lemoncrow.pro.capabilities.review.intelligence_policy import EffectiveIntelligencePolicy

__all__ = [
    "LocalBackgroundExecutor",
    "LocalBackgroundScheduler",
    "LocalOrigination",
    "default_local_executors",
    "local_intelligence_settings",
    "local_job_id",
    "local_review_auto_decision",
    "run_local_review_auto",
]

_LOG = logging.getLogger(__name__)

#: Local provider credentials stay in the process environment. Browser settings
#: write only non-secret intelligence preferences through a bounded API.
LOCAL_INTELLIGENCE_EDIT_SOURCE: Final[str] = (
    "this machine's LemonCrow settings; provider credentials stay in your environment"
)
#: The settings keys of the local background envelope (BI1), beside BI0's route keys.
LOCAL_BACKGROUND_SETTINGS: Final[tuple[str, ...]] = (
    "intelligence.background.monthly_model_calls",
    "intelligence.background.daily_model_calls",
)
#: Local background work has no daemon: it runs inside a live LemonCrow process.
LOCAL_LIFETIME_NOTE: Final[str] = (
    "Background work runs only while LemonCrow is running on this machine; nothing runs while it is stopped, "
    "and queued work continues the next time it starts."
)


class LocalBackgroundExecutor(Protocol):
    """One capability's local executor (PK1/DS0 register theirs)."""

    def is_current(self, subject: Mapping[str, Any]) -> bool:
        """Whether the job's source generation is still the one to compute (cheap; no model call)."""

    def execute(self, subject: Mapping[str, Any], *, max_calls: int) -> BackgroundOutcome:
        """Compute the result within ``max_calls`` model calls through the intelligence runtime."""


def default_local_executors(root: Path | str | None = None) -> dict[str, LocalBackgroundExecutor]:
    """The executors this build ships for the local jobs of *root*.

    ``review.auto_ai`` is never one: it runs inside an explicit request
    (:func:`run_local_review_auto`). With a root, PK1's Project Knowledge
    narration executors over that root's Review store and DS0's thread summary
    executor (its job subject names the Review store). Without a root, none.
    """

    if root is None:
        return {}
    from lemoncrow.pro.capabilities.review.discussion_summary import local_discussion_executors
    from lemoncrow.pro.capabilities.review.project_knowledge_narration import local_knowledge_executors

    return {**local_knowledge_executors(Path(root)), **local_discussion_executors()}


def local_job_id(identity: str) -> str:
    """The jobs-table id of a semantic identity (a UUID both SQLite and PostgreSQL accept)."""

    return uuid.UUID(hex=identity[:32]).hex


@dataclass(frozen=True, slots=True)
class LocalOrigination:
    decision: SchedulingDecision
    job_id: str = ""
    #: False when this semantic identity was previously queued or executed.
    enqueued: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {**self.decision.to_wire(), "job_id": self.job_id, "enqueued": self.enqueued}


def _process_policy() -> EffectiveIntelligencePolicy:
    from lemoncrow.pro.capabilities.review.fix_agent import settings_root
    from lemoncrow.pro.capabilities.review.intelligence_policy import process_intelligence_policy

    return process_intelligence_policy(settings_root())


class LocalBackgroundScheduler:
    """Originate background jobs into the local jobs table and run them under the budget.

    ``jobs`` is the store's job queue (``enqueue_job_once``). ``policy`` returns
    BI0's effective policy (default: this process's settings), read fresh on
    every call so a settings change applies at once.
    """

    def __init__(
        self,
        jobs: Any,
        *,
        budget: LocalBackgroundBudgetStore,
        policy: Callable[[], EffectiveIntelligencePolicy] | None = None,
        executors: Mapping[str, LocalBackgroundExecutor] | None = None,
        route: Mapping[str, str] | None = None,
    ) -> None:
        self._jobs = jobs
        self._budget = budget
        self._policy = policy or _process_policy
        self._executors = dict(default_local_executors() if executors is None else executors)
        self._route = dict(route or {})

    @classmethod
    def for_root(cls, jobs: Any, root: Path | str, **kwargs: Any) -> LocalBackgroundScheduler:
        if kwargs.get("executors") is None:
            kwargs["executors"] = default_local_executors(root)
        return cls(jobs, budget=LocalBackgroundBudgetStore(root), **kwargs)

    def originate(self, capability_id: str, event: BackgroundEvent, subject: Mapping[str, Any]) -> LocalOrigination:
        """Queue the capability's job for a durable event, or say why not. Never runs a model."""

        policy = self._policy()
        decision = plan_background_work(
            capability_id,
            event,
            policy=policy,
            envelope=self._budget.policy(),
            offered=("local",),
            route=self._route,
            executors=self._executors.keys(),
        )
        if decision.scheduled and decision.capability not in self._executors:
            # Declared and enabled, but nothing here runs it as a job (local Auto stays explicit).
            decision = SchedulingDecision(
                SchedulerReason.EXECUTOR_UNAVAILABLE.value, decision.capability, decision.priority
            )
        if not decision.scheduled:
            return LocalOrigination(decision)
        entry = capability(decision.capability)
        assert entry is not None and decision.priority is not None
        job_id = local_job_id(decision.identity)
        payload = {
            "capability": entry.id,
            "version": entry.version,
            "identity": decision.identity,
            "event": event.to_wire(),
            "policy_digest": policy.digest,
            "locus": decision.locus,
            "priority": decision.priority.value,
            "subject": dict(subject),
        }
        enqueued = bool(self._jobs.enqueue_job_once(JOB_BACKGROUND_INTELLIGENCE, payload, job_id=job_id))
        return LocalOrigination(decision, job_id, enqueued)

    def run_job(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The worker handler: re-check, reserve, run, settle once. Raises :class:`JobPaused` to wait."""

        entry = capability(str(payload.get("capability", "")))
        if entry is None or payload.get("version") != entry.version:
            return {"status": "superseded", "reason": SchedulerReason.SUPERSEDED.value}
        executor = self._executors.get(entry.id)
        if executor is None:
            return {"status": "refused", "reason": SchedulerReason.EXECUTOR_UNAVAILABLE.value}
        policy = self._policy()
        refusal = background_enablement(policy, self._budget.policy())
        if refusal is SchedulerReason.POLICY_INVALID:
            # A malformed setting is fixed, not waited out forever: pause, do not drop.
            raise JobPaused(refusal.value)
        if refusal is not None:
            return {"status": "refused", "reason": refusal.value}
        if policy.digest != payload.get("policy_digest"):
            # A changed policy originates its own identity; this job's answer would be stale.
            return {"status": "superseded", "reason": SchedulerReason.SUPERSEDED.value}
        identity = str(payload.get("identity") or "")
        subject: Mapping[str, Any] = dict(payload.get("subject") or {})
        request = BackgroundWorkRequest(
            # Fresh per attempt: a retried job reserves anew, a crashed attempt's hold expires.
            key=f"bg-local:{identity[:32]}:{uuid.uuid4().hex}",
            capability=entry.id,
            priority=entry.priority,
            max_calls=entry.max_calls,
            locus="local",
        )
        run = run_admitted(
            request,
            reserve=self._budget.reserve,
            settle=lambda key, calls, cost: self._budget.settle(key, calls=calls, cost_micros=cost),
            current=lambda: executor.is_current(subject),
            execute=lambda granted: executor.execute(subject, max_calls=granted),
        )
        if run.paused:
            raise JobPaused(run.reason)
        return {"capability": entry.id, **run.to_wire()}


def local_review_auto_decision(
    *, policy: EffectiveIntelligencePolicy, budget: LocalBackgroundBudgetStore, source_digest: str
) -> SchedulingDecision:
    """Whether a local ``trigger: auto`` AI Review may run here (enablement and ``run_on``)."""

    return plan_background_work(
        REVIEW_AUTO_AI,
        BackgroundEvent(SNAPSHOT_CAPTURED, source_digest),
        policy=policy,
        envelope=budget.policy(),
        offered=("local",),
        planned="local",
    )


def run_local_review_auto(
    *,
    budget: LocalBackgroundBudgetStore,
    revision_id: str,
    max_calls: int,
    current: Callable[[], bool],
    execute: Callable[[int], BackgroundOutcome],
) -> AdmittedRun:
    """Meter one local automatic AI Review: reserve up to ``max_calls``, run within the grant, settle once.

    ``max_calls`` is the Review's own ceiling (run plan and environment); the
    grant may be smaller. The caller has checked enablement
    (:func:`local_review_auto_decision`) and currentness just before.
    """

    entry = capability(REVIEW_AUTO_AI)
    assert entry is not None
    request = BackgroundWorkRequest(
        key=f"bg-local-auto:{revision_id[:64]}:{uuid.uuid4().hex}",
        capability=entry.id,
        priority=entry.priority,
        max_calls=max(1, min(int(max_calls), entry.max_calls)),
        locus="local",
    )
    return run_admitted(
        request,
        reserve=budget.reserve,
        settle=lambda key, calls, cost: budget.settle(key, calls=calls, cost_micros=cost),
        current=current,
        execute=execute,
        checked=True,
    )


def local_intelligence_settings() -> dict[str, Any]:
    """The local Settings / Usage projection (UX0a): effective route and budget.

    Local preferences can be updated through the authenticated local Settings
    endpoint. Hosted policy remains separate and read-only to individuals.
    """

    from lemoncrow.core.settings import load_settings
    from lemoncrow.pro.capabilities.review.ai_budget import BUDGET_SETTING, ai_budget_state
    from lemoncrow.pro.capabilities.review.data_governance import load_data_governance
    from lemoncrow.pro.capabilities.review.fix_agent import settings_root
    from lemoncrow.pro.capabilities.review.intelligence_policy import (
        SETTINGS_KEYS,
        process_intelligence_policy,
        resolve_intelligence_route,
    )

    route = resolve_intelligence_route(
        process_intelligence_policy(), allowed_run_on=("local",), governance=load_data_governance()
    )
    try:
        background: dict[str, Any] = LocalBackgroundBudgetStore(settings_root()).status()
    except (sqlite3.Error, OSError):
        _LOG.warning("local background budget status failed", exc_info=True)
        background = {"available": False, "reason": "the background budget ledger could not be read"}
    preferences = load_settings(settings_root(), category="intelligence")
    budget = ai_budget_state()
    # A stored -1 is "no limit"; the wire says null (the field is empty), as the budget does.
    preferences[BUDGET_SETTING] = budget.to_wire()["monthly_budget_usd"]
    return {
        "scope": "local",
        "intelligence": {
            **route.to_wire(),
            "editable": True,
            "edit_source": LOCAL_INTELLIGENCE_EDIT_SOURCE,
            "settings": [BUDGET_SETTING, *SETTINGS_KEYS.values(), *LOCAL_BACKGROUND_SETTINGS],
        },
        "background": background,
        # P8-4: the personal AI budget, this month's use and whether a model call may start now.
        "ai_budget": budget.to_wire(),
        "preferences": {key: preferences[key] for key in _LOCAL_AI_EDIT_KEYS},
        "lifetime": LOCAL_LIFETIME_NOTE,
    }


_LOCAL_AI_EDIT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "intelligence.monthly_budget_usd",
        "intelligence.driver",
        "intelligence.model",
        "intelligence.run_on",
        "intelligence.background.enabled",
        "intelligence.background.monthly_model_calls",
        "intelligence.background.daily_model_calls",
    }
)


def update_local_intelligence_settings(body: Mapping[str, Any]) -> None:
    """Persist only known, non-secret local AI settings; validate all before writing.

    Hosted organization/repository policy and provider secrets are never modified.
    This uses the same file and validation logic as the terminal settings command.
    """
    from lemoncrow.core.settings import coerce, find_by_key, load_settings, unset_setting, write_setting
    from lemoncrow.pro.capabilities.review.ai_budget import BUDGET_SETTING
    from lemoncrow.pro.capabilities.review.fix_agent import settings_root

    if set(body) != {"settings"} or not isinstance(body["settings"], dict):
        raise ValueError("expected an object containing settings")
    supplied = body["settings"]
    if not supplied or len(supplied) > len(_LOCAL_AI_EDIT_KEYS):
        raise ValueError("provide at least one AI setting")
    validated: dict[str, Any] = {}
    for key, raw in supplied.items():
        if not isinstance(key, str) or key not in _LOCAL_AI_EDIT_KEYS:
            raise ValueError("unknown or unsupported AI setting")
        spec = find_by_key(key)
        if spec is None or not spec.settable:
            raise ValueError("AI setting cannot be changed here")
        if key == BUDGET_SETTING:
            # A number of dollars, 0 or more; null is "no limit" (the setting is removed).
            if raw is not None and (isinstance(raw, bool) or not isinstance(raw, (int, float))):
                raise ValueError(f"{key} must be a number of dollars, 0 or more, or null for no limit")
            if raw is not None and not 0 <= raw <= (spec.float_range or (0, 1e9))[1]:
                raise ValueError(f"{key} must be a number of dollars, 0 or more, or null for no limit")
            validated[key] = None if raw is None else float(raw)
            continue
        if spec.type == "bool":
            if not isinstance(raw, bool):
                raise ValueError(f"{key} must be true or false")
        elif spec.type == "int":
            if type(raw) is not int:
                raise ValueError(f"{key} must be an integer")
        elif not isinstance(raw, str) or not raw or len(raw) > 128 or any(ord(c) < 32 for c in raw):
            raise ValueError(f"{key} must be a nonempty setting value")
        value = coerce(spec, raw)
        if spec.choices and value not in spec.choices:
            raise ValueError(f"{key} must be one of: {', '.join(spec.choices)}")
        if spec.int_range and not spec.int_range[0] <= value <= spec.int_range[1]:
            raise ValueError(f"{key} is outside the permitted range")
        if key == "intelligence.run_on" and value not in {"auto", "local"}:
            raise ValueError("local AI settings support only Auto or this machine")
        validated[key] = value
    root = settings_root()
    if {"intelligence.model", "intelligence.driver"} & validated.keys():
        # Saved, this pair would turn the whole AI route invalid and stop background work.
        stored = load_settings(root, category="intelligence")
        model = validated.get("intelligence.model", stored["intelligence.model"])
        driver = validated.get("intelligence.driver", stored["intelligence.driver"])
        if model != "auto" and driver == "auto":
            raise ValueError("choose a provider for this model, or set the model back to auto")
    for key, value in validated.items():
        if value is None:
            unset_setting(root, key)
        else:
            write_setting(root, key, value)
