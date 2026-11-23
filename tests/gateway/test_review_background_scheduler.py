"""BI2: the Background Intelligence capability registry and scheduler on the existing job planes.

One path from a durable event to model work: applicability, BI0's effective
policy, BI1 admission by priority, the existing local jobs table, and exactly
one settlement per attempt. Duplicate events converge on one job; a stale job
stops before it reserves or runs; a budget refusal pauses without spending an
attempt; review work is admitted before knowledge work under contention.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core import settings
from lemoncrow.core.service import maintenance_tick
from lemoncrow.core.service.jobs import JOB_BACKGROUND_INTELLIGENCE, JobPaused
from lemoncrow.core.service.worker import Worker
from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle
from lemoncrow.pro.capabilities.review.background_budget import (
    DEFAULT_HOLD_S,
    AdmissionReason,
    BackgroundAdmission,
    BackgroundBudgetPolicies,
    BackgroundBudgetPolicy,
    BackgroundWorkRequest,
    BudgetLane,
    CeilingSource,
    JobPriority,
    PeriodTotals,
    decide,
)
from lemoncrow.pro.capabilities.review.background_budget_store import LocalBackgroundBudgetStore
from lemoncrow.pro.capabilities.review.background_local import (
    LocalBackgroundScheduler,
    local_job_id,
    local_review_auto_decision,
    run_local_review_auto,
)
from lemoncrow.pro.capabilities.review.background_scheduler import (
    BACKGROUND_CAPABILITIES,
    BUDGET_RETRY_POLL_S,
    CHANGE_LANDED,
    REVIEW_AUTO_AI,
    SNAPSHOT_CAPTURED,
    AdmittedRun,
    BackgroundEvent,
    BackgroundOutcome,
    SchedulerReason,
    background_enablement,
    budget_retry_after_s,
    capability,
    envelope_under_policy,
    job_identity,
    plan_background_work,
    resolve_locus,
    run_admitted,
)
from lemoncrow.pro.capabilities.review.intelligence_policy import (
    EffectiveIntelligencePolicy,
    IntelligencePolicyLayer,
    PolicyOrigin,
    resolve_intelligence_policy,
)

NARRATION = "knowledge.change_narration"


@pytest.fixture(autouse=True)
def settings_root(tmp_path: Path) -> Iterator[Path]:
    """An empty LemonCrow root this process reads its settings (and BI0 policy) from."""

    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    set_active_store_root(root)
    try:
        yield root
    finally:
        set_active_store_root(None)


def _policy(**values: Any) -> EffectiveIntelligencePolicy:
    layer = IntelligencePolicyLayer(origin=PolicyOrigin("organization", "test"), **values) if values else None
    return resolve_intelligence_policy([layer])


def _invalid() -> EffectiveIntelligencePolicy:
    return resolve_intelligence_policy([], errors=("intelligence settings are malformed",))


def _envelope(**values: Any) -> BackgroundBudgetPolicy:
    return BackgroundBudgetPolicy(**{"monthly_model_calls": 100, "monthly_managed_micros": None, **values})


class _Executor:
    """A knowledge executor stand-in: currentness from a flag, calls as configured."""

    def __init__(self, *, calls: int = 1, current: bool = True, fail: bool = False) -> None:
        self.calls = calls
        self.current = current
        self.fail = fail
        self.executed: list[tuple[Mapping[str, Any], int]] = []

    def is_current(self, subject: Mapping[str, Any]) -> bool:
        return self.current

    def execute(self, subject: Mapping[str, Any], *, max_calls: int) -> BackgroundOutcome:
        self.executed.append((dict(subject), max_calls))
        if self.fail:
            raise RuntimeError("provider failed")
        return BackgroundOutcome(min(self.calls, max_calls), None, {"narrative": "ok"})


def _bundle(root: Path) -> Any:
    bundle = build_sqlite_store_bundle(root)
    bundle.init()
    return bundle


def _scheduler(bundle: Any, root: Path, executor: _Executor, **kwargs: Any) -> LocalBackgroundScheduler:
    return LocalBackgroundScheduler.for_root(bundle.jobs, root, executors={NARRATION: executor}, **kwargs)


def _event(source: str = "generation-1") -> BackgroundEvent:
    return BackgroundEvent(CHANGE_LANDED, source)


def _job(bundle: Any, job_id: str) -> dict[str, Any]:
    rows = bundle.jobs.list_jobs(job_type=JOB_BACKGROUND_INTELLIGENCE, limit=100)
    return next(row for row in rows if str(row["id"]) == job_id)


def _usage(root: Path) -> dict[str, Any]:
    return LocalBackgroundBudgetStore(root).status()


# --------------------------------------------------------------------------- #
# registry, enablement, locus, identity
# --------------------------------------------------------------------------- #


def test_the_registry_declares_six_capabilities_and_only_the_feedback_digest_and_rule_suggestion_lack_one() -> None:
    assert set(BACKGROUND_CAPABILITIES) == {
        REVIEW_AUTO_AI,
        "knowledge.change_narration",
        "knowledge.repository_refresh",
        "review.thread_summary",
        "review.feedback_digest",
        "review.rule_suggestion",
    }
    auto = capability(REVIEW_AUTO_AI)
    assert auto is not None and auto.priority is JobPriority.REVIEW_AUTO and auto.max_calls == 24
    assert auto.to_wire()["executor"] == "available"
    for entry in BACKGROUND_CAPABILITIES.values():
        wire = entry.to_wire()
        assert wire["content_access"] == "ai_content_access_rules" and wire["triggers"] and wire["loci"]
        assert 1 <= entry.max_calls <= 24 and entry.priority.rank >= JobPriority.REVIEW_AUTO.rank
        if entry.id.startswith("knowledge.") or entry.id == "review.thread_summary":
            # PK1 and DS0 ship local executors only: no hosted plane originates them yet.
            assert wire["executor"] == "available" and entry.loci == ("local",)
        elif entry.id != REVIEW_AUTO_AI:
            assert wire["executor"] == "lands with DS0b"
            # Declared, not runnable: refused until its package registers an executor.
            event = BackgroundEvent(sorted(entry.triggers)[0], "source")
            decision = plan_background_work(entry.id, event, policy=_policy(), envelope=_envelope(), offered=("local",))
            assert decision.reason == SchedulerReason.EXECUTOR_UNAVAILABLE.value
    assert plan_background_work("nope", _event(), policy=None, envelope=None, offered=("local",)).reason == (
        SchedulerReason.UNKNOWN_CAPABILITY.value
    )
    wrong = plan_background_work(REVIEW_AUTO_AI, _event(), policy=None, envelope=None, offered=("local",))
    assert wrong.reason == SchedulerReason.NOT_APPLICABLE.value


@pytest.mark.parametrize(
    ("policy", "envelope", "expected"),
    [
        (lambda: _policy(), lambda: _envelope(), None),
        (lambda: _policy(enabled=False), lambda: _envelope(), SchedulerReason.INTELLIGENCE_DISABLED),
        (lambda: _policy(background_enabled=False), lambda: _envelope(), SchedulerReason.BACKGROUND_DISABLED),
        (lambda: _policy(), lambda: _envelope(enabled=False), SchedulerReason.BACKGROUND_DISABLED),
        (lambda: _invalid(), lambda: _envelope(), SchedulerReason.POLICY_INVALID),
        (lambda: _policy(), lambda: BackgroundBudgetPolicy.invalid(), SchedulerReason.POLICY_INVALID),
        # BI0 is read first; either way nothing runs.
        (lambda: _invalid(), lambda: _envelope(enabled=False), SchedulerReason.POLICY_INVALID),
        (
            lambda: _policy(enabled=False),
            lambda: BackgroundBudgetPolicy.invalid(),
            SchedulerReason.INTELLIGENCE_DISABLED,
        ),
    ],
)
def test_enablement_is_one_conjunction_of_bi0_and_the_envelope(policy: Any, envelope: Any, expected: Any) -> None:
    assert background_enablement(policy(), envelope()) is expected
    decision = plan_background_work(
        REVIEW_AUTO_AI,
        BackgroundEvent(SNAPSHOT_CAPTURED, "rev"),
        policy=policy(),
        envelope=envelope(),
        offered=("local",),
    )
    assert decision.scheduled is (expected is None)
    if expected is not None:
        assert decision.reason == expected.value and decision.identity == ""


def test_the_envelope_follows_bi0_so_usage_and_admission_agree_with_the_kill_switch() -> None:
    envelope = _envelope()
    assert envelope_under_policy(envelope, _policy()) is envelope
    assert envelope_under_policy(envelope, _policy(enabled=False)).enabled is False
    assert envelope_under_policy(envelope, _policy(background_enabled=False)).enabled is False
    assert envelope_under_policy(envelope, _invalid()).policy_invalid is True


def test_run_on_is_honoured_and_never_degraded() -> None:
    entry = capability(REVIEW_AUTO_AI)
    assert entry is not None
    assert resolve_locus(entry, _policy(), offered=("local",)) == "local"
    assert resolve_locus(entry, _policy(run_on="local"), offered=("local",)) == "local"
    # An explicit locus this deployment does not offer is refused, never swapped for one it does.
    assert resolve_locus(entry, _policy(run_on="managed"), offered=("local",)) is SchedulerReason.ROUTE_UNAVAILABLE
    assert resolve_locus(entry, _policy(run_on="worker"), offered=("managed",)) is SchedulerReason.ROUTE_UNAVAILABLE
    # The run plan's executor is authoritative: run_on may agree with it, never redirect it.
    assert resolve_locus(entry, _policy(), offered=("worker",), planned="worker") == "worker"
    assert resolve_locus(entry, _policy(run_on="worker"), offered=("worker",), planned="worker") == "worker"
    assert (
        resolve_locus(entry, _policy(run_on="managed"), offered=("worker",), planned="worker")
        is SchedulerReason.ROUTE_UNAVAILABLE
    )
    assert resolve_locus(entry, _policy(), offered=("local",), planned="managed") is SchedulerReason.ROUTE_UNAVAILABLE
    refused = local_review_auto_decision(
        policy=_policy(run_on="managed"), budget=LocalBackgroundBudgetStore("/nonexistent", policy=_envelope()),
        source_digest="rev",
    )  # fmt: skip
    assert refused.reason == SchedulerReason.ROUTE_UNAVAILABLE.value


def test_job_identity_moves_with_every_input_and_duplicates_converge() -> None:
    entry = capability(NARRATION)
    assert entry is not None
    base = {"source_digest": "gen-1", "policy_digest": "p1", "content_access_digest": "c1", "route": {"locus": "local"}}
    first = job_identity(entry, **base)  # type: ignore[arg-type]
    assert first == job_identity(entry, **base)  # type: ignore[arg-type]
    for name, value in (("source_digest", "gen-2"), ("policy_digest", "p2"), ("content_access_digest", "c2"),
                        ("route", {"locus": "worker"})):  # fmt: skip
        assert job_identity(entry, **{**base, name: value}) != first, name  # type: ignore[arg-type]
    other = capability("knowledge.repository_refresh")
    assert other is not None and job_identity(other, **base) != first  # type: ignore[arg-type]
    executors = {NARRATION}
    one = plan_background_work(NARRATION, _event(), policy=_policy(), envelope=_envelope(), offered=("local",),
                               executors=executors)  # fmt: skip
    two = plan_background_work(NARRATION, _event(), policy=_policy(), envelope=_envelope(), offered=("local",),
                               executors=executors)  # fmt: skip
    assert one.scheduled and one.identity == two.identity
    moved = plan_background_work(NARRATION, _event(), policy=_policy(run_on="local"), envelope=_envelope(),
                                 offered=("local",), executors=executors)  # fmt: skip
    assert moved.identity != one.identity, "a policy change is a new identity"


# --------------------------------------------------------------------------- #
# automatic Review: metered always, bound by the envelope only when configured
# --------------------------------------------------------------------------- #


def test_automatic_review_is_metered_but_only_a_configured_envelope_limits_it(tmp_path: Path) -> None:
    request = BackgroundWorkRequest(key="k", capability=REVIEW_AUTO_AI, priority=JobPriority.REVIEW_AUTO, max_calls=5)
    knowledge = BackgroundWorkRequest(key="n", capability=NARRATION, priority=JobPriority.KNOWLEDGE_REFRESH,
                                      max_calls=2)  # fmt: skip
    spent = PeriodTotals(calls_used=100)
    # Only the shipped envelope nobody chose is unconfigured; an envelope built in code binds.
    assert BackgroundBudgetPolicy.shipped().binds(JobPriority.REVIEW_AUTO) is False
    assert BackgroundBudgetPolicy().binds(JobPriority.REVIEW_AUTO) is True
    unconfigured = _envelope(configured=False)
    assert decide(request, unconfigured, month=spent, day=PeriodTotals()).granted_calls == 5
    assert (
        decide(knowledge, unconfigured, month=spent, day=PeriodTotals()).reason is AdmissionReason.BACKGROUND_EXHAUSTED
    )
    configured = _envelope()
    assert decide(request, configured, month=spent, day=PeriodTotals()).reason is AdmissionReason.BACKGROUND_EXHAUSTED
    # The switch and an invalid policy still bind automatic Review either way.
    assert decide(request, _envelope(enabled=False), month=PeriodTotals(), day=PeriodTotals()).reason is (
        AdmissionReason.BACKGROUND_DISABLED
    )

    # Hosted: only an explicit ceiling configures a scope. A switch-only (or empty) section leaves
    # automatic Review metered but unenforced (BI2 review item 1).
    parse = BackgroundBudgetPolicies.parse
    assert BackgroundBudgetPolicies.standard().for_scope("org").binds(JobPriority.REVIEW_AUTO) is False
    assert parse({}).for_scope("org").binds(JobPriority.REVIEW_AUTO) is False
    switch_only = parse({"defaults": {"enabled": True}, "overrides": {"o": {"enabled": True}}})
    for scope in ("org", "o"):
        assert switch_only.for_scope(scope).configured is False
        assert decide(request, switch_only.for_scope(scope), month=spent, day=PeriodTotals()).granted_calls == 5
    for ceiling in ({"monthly_model_calls": 100}, {"daily_model_calls": 3}, {"monthly_managed_usd": 5}):
        configured_scope = parse({"defaults": {"enabled": True, **ceiling}}).for_scope("org")
        assert configured_scope.binds(JobPriority.REVIEW_AUTO) is True, ceiling
    assert parse({"overrides": {"o": {"daily_model_calls": 3}}}).for_scope("o").configured
    assert parse({"overrides": {"o": {"daily_model_calls": 3}}}).for_scope("org").configured is False
    # An override inherits a configured default.
    assert (
        parse({"defaults": {"monthly_model_calls": 100}, "overrides": {"o": {"enabled": True}}})
        .for_scope("o")
        .configured
    )
    limited = parse({"defaults": {"monthly_model_calls": 100, "monthly_managed_usd": None}}).for_scope("org")
    assert decide(request, limited, month=spent, day=PeriodTotals()).reason is AdmissionReason.BACKGROUND_EXHAUSTED
    # Local: setting a call ceiling configures it; the on/off switch alone does not.
    root = tmp_path / "local"
    store = LocalBackgroundBudgetStore(root)
    assert store.policy().configured is False
    settings.write_setting(settings_root_path(), "intelligence.background.enabled", True)
    assert LocalBackgroundBudgetStore(root).policy().configured is False
    settings.write_setting(settings_root_path(), "intelligence.background.monthly_model_calls", 50)
    assert LocalBackgroundBudgetStore(root).policy().configured is True
    status = LocalBackgroundBudgetStore(root).status()
    assert status["automatic_review"]["envelope_enforced"] is True

    # The ledger meters an unconfigured automatic Review in the background lane.
    settings.unset_setting(settings_root_path(), "intelligence.background.monthly_model_calls")
    run = run_local_review_auto(
        budget=store, revision_id="rev-1", max_calls=3, current=lambda: True,
        execute=lambda granted: BackgroundOutcome(2, None, "result"),
    )  # fmt: skip
    assert run.status == "completed" and run.calls_used == 2 and run.result == "result"
    usage = _usage(root)
    assert usage["background"]["model_calls_used"] == 2 and usage["interactive"]["model_calls_used"] == 0
    assert usage["automatic_review"]["envelope_enforced"] is False


def settings_root_path() -> Path:
    from lemoncrow.pro.capabilities.review.fix_agent import settings_root

    return settings_root()


# --------------------------------------------------------------------------- #
# exactly-once accounting
# --------------------------------------------------------------------------- #


def _request(key: str, *, max_calls: int = 4, priority: JobPriority = JobPriority.KNOWLEDGE_REFRESH) -> Any:
    return BackgroundWorkRequest(key=key, capability=NARRATION, priority=priority, max_calls=max_calls)


def test_run_admitted_settles_exactly_once_in_every_outcome(tmp_path: Path) -> None:
    store = LocalBackgroundBudgetStore(tmp_path, policy=_envelope(monthly_model_calls=100, configured=True))
    settled: list[tuple[str, int, int | None]] = []

    def settle(key: str, calls: int, cost: int | None) -> object:
        settled.append((key, calls, cost))
        return store.settle(key, calls=calls, cost_micros=cost)

    # Success: charged what it used.
    done = run_admitted(_request("ok"), reserve=store.reserve, settle=settle, current=lambda: True,
                        execute=lambda granted: BackgroundOutcome(3, 1_500, "r"))  # fmt: skip
    assert done.status == "completed" and done.calls_used == 3 and settled == [("ok", 3, 1_500)]

    # Stale before reserving: nothing reserved, nothing settled, nothing run.
    ran: list[int] = []
    stale = run_admitted(_request("stale"), reserve=store.reserve, settle=settle, current=lambda: False,
                         execute=lambda granted: ran.append(granted) or BackgroundOutcome(1))  # type: ignore[func-returns-value]  # fmt: skip
    assert stale.status == "superseded" and stale.admission is None and ran == [] and len(settled) == 1

    # Stale after reserving: released uncharged, never run.
    checks = iter([True, False])
    late = run_admitted(_request("late"), reserve=store.reserve, settle=settle, current=lambda: next(checks),
                        execute=lambda granted: ran.append(granted) or BackgroundOutcome(1))  # type: ignore[func-returns-value]  # fmt: skip
    assert late.status == "superseded" and late.admission is not None and ran == [] and settled[-1] == ("late", 0, 0)

    # Failure: charged its whole grant (the calls may have been made), and the error propagates.
    def boom(granted: int) -> BackgroundOutcome:
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        run_admitted(_request("fail"), reserve=store.reserve, settle=settle, current=lambda: True, execute=boom)
    assert settled[-1] == ("fail", 4, None)

    # An over-reporting executor is clamped to its grant (settling more would be an overrun).
    over = run_admitted(_request("over", max_calls=2), reserve=store.reserve, settle=settle, current=lambda: True,
                        execute=lambda granted: BackgroundOutcome(9))  # fmt: skip
    assert over.calls_used == 2 and settled[-1] == ("over", 2, None)

    # A refusal reserves nothing and settles nothing.
    tight = LocalBackgroundBudgetStore(tmp_path / "tight", policy=_envelope(monthly_model_calls=0, configured=True))
    count = len(settled)
    refused = run_admitted(_request("no"), reserve=tight.reserve, settle=settle, current=lambda: True,
                           execute=lambda granted: BackgroundOutcome(1))  # fmt: skip
    assert refused.status == "refused" and refused.paused and len(settled) == count

    # A replayed, already-settled attempt never runs or charges again.
    again = run_admitted(_request("ok"), reserve=store.reserve, settle=settle, current=lambda: True,
                         execute=lambda granted: ran.append(granted) or BackgroundOutcome(1))  # type: ignore[func-returns-value]  # fmt: skip
    assert again.status == "completed" and again.result is None and ran == [] and len(settled) == count
    usage = _usage(tmp_path)
    assert usage["background"]["model_calls_used"] == 3 + 0 + 4 + 2
    assert usage["background"]["model_calls_held"] == 0


def test_a_failed_settlement_keeps_the_hold_is_logged_and_never_fails_the_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """BI2 review item 7: the BI1 contract for a settle that never landed is the hold expiring charged in full."""

    store = LocalBackgroundBudgetStore(tmp_path, policy=_envelope(monthly_model_calls=100, configured=True))

    def unreachable(key: str, calls: int, cost: int | None) -> object:
        raise sqlite3.OperationalError("database is locked")

    with caplog.at_level("WARNING", logger="lemoncrow.pro.capabilities.review.background_scheduler"):
        done = run_admitted(_request("ok"), reserve=store.reserve, settle=unreachable, current=lambda: True,
                            execute=lambda granted: BackgroundOutcome(1, None, "result"))  # fmt: skip
    assert (done.status, done.calls_used, done.result) == ("completed", 1, "result")
    assert "settlement of 1 model calls failed" in caplog.text

    # The execution's own error still propagates, never the settlement's.
    def boom(granted: int) -> BackgroundOutcome:
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        run_admitted(_request("fail"), reserve=store.reserve, settle=unreachable, current=lambda: True, execute=boom)
    usage = _usage(tmp_path)["background"]
    assert (usage["model_calls_used"], usage["model_calls_held"]) == (0, 8)  # both holds kept
    later = LocalBackgroundBudgetStore(tmp_path, clock=lambda: time.time() + DEFAULT_HOLD_S + 1)
    for _ in range(2):  # expired: charged their whole grants, exactly once
        usage = later.status()["background"]
        assert (usage["model_calls_used"], usage["model_calls_held"]) == (8, 0)


def test_the_local_scheduler_keeps_running_when_a_settlement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store"
    bundle = _bundle(root)
    executor = _Executor(calls=1)
    scheduler = _scheduler(bundle, root, executor)
    first = scheduler.originate(NARRATION, _event(), {"repository": "r1"})
    second = scheduler.originate(NARRATION, _event("generation-2"), {"repository": "r1"})

    def unreachable(self: LocalBackgroundBudgetStore, key: str, **_kw: Any) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(LocalBackgroundBudgetStore, "settle", unreachable)
    worker = _worker(bundle, scheduler)
    assert worker.run_once() == first.job_id and worker.run_once() == second.job_id
    assert [_job(bundle, job.job_id)["status"] for job in (first, second)] == ["succeeded", "succeeded"]
    assert len(executor.executed) == 2
    assert _usage(root)["background"]["model_calls_held"] == 2 * capability(NARRATION).max_calls  # type: ignore[union-attr]


def test_a_paused_run_retries_at_the_poll_or_the_reset_whichever_comes_first() -> None:
    """BI2 review item 2: the delay a refused queued job waits before it asks the ledger again."""

    def refused(reason: AdmissionReason, binding: CeilingSource | None) -> AdmittedRun:
        return AdmittedRun("refused", reason.value, BackgroundAdmission(reason, BudgetLane.BACKGROUND, binding=binding))

    mid_month = datetime(2026, 10, 13, 12, 0, tzinfo=UTC).timestamp()
    period = refused(AdmissionReason.BACKGROUND_EXHAUSTED, CeilingSource.BACKGROUND_PERIOD)
    assert budget_retry_after_s(period, now=mid_month) == BUDGET_RETRY_POLL_S
    last_minute = datetime(2026, 10, 31, 23, 59, 30, tzinfo=UTC).timestamp()
    assert budget_retry_after_s(period, now=last_minute) == 30.0  # the month resets first
    daily = refused(AdmissionReason.DAILY_GUARD_EXHAUSTED, CeilingSource.BACKGROUND_DAILY)
    assert budget_retry_after_s(daily, now=datetime(2026, 10, 13, 23, 58, tzinfo=UTC).timestamp()) == 120.0
    held = refused(AdmissionReason.PRIORITY_RESERVED, CeilingSource.BACKGROUND_PERIOD)
    assert budget_retry_after_s(held, now=mid_month) == BUDGET_RETRY_POLL_S
    # Not a pausing refusal: never retried.
    assert budget_retry_after_s(refused(AdmissionReason.COST_UNMEASURABLE, None), now=mid_month) is None
    assert budget_retry_after_s(AdmittedRun("superseded", "superseded"), now=mid_month) is None


def test_an_explicit_local_auto_refusal_reports_at_once_and_queues_nothing(tmp_path: Path) -> None:
    """BI2 review item 2: local Auto stays explicit; a refusal is the caller's answer now, not a queued job."""

    root = tmp_path / "store"
    bundle = _bundle(root)
    settings.write_setting(settings_root_path(), "intelligence.background.monthly_model_calls", 0)
    ran: list[int] = []
    run = run_local_review_auto(budget=LocalBackgroundBudgetStore(root), revision_id="rev-1", max_calls=5,
                                current=lambda: True,
                                execute=lambda granted: ran.append(granted) or BackgroundOutcome(1))  # type: ignore[func-returns-value]  # fmt: skip
    assert (run.status, run.reason, run.paused) == ("refused", "background_exhausted", True)
    assert ran == [] and bundle.jobs.list_jobs(limit=10) == []


# --------------------------------------------------------------------------- #
# the local plane: the existing jobs table and worker
# --------------------------------------------------------------------------- #


def test_duplicate_events_converge_on_one_local_job(tmp_path: Path) -> None:
    root = tmp_path / "store"
    bundle = _bundle(root)
    scheduler = _scheduler(bundle, root, _Executor())
    first = scheduler.originate(NARRATION, _event(), {"repository": "r1"})
    second = scheduler.originate(NARRATION, _event(), {"repository": "r1"})
    assert first.decision.scheduled and first.enqueued and first.job_id == local_job_id(first.decision.identity)
    assert second.job_id == first.job_id and second.enqueued is False
    other = scheduler.originate(NARRATION, _event("generation-2"), {"repository": "r1"})
    assert other.enqueued and other.job_id != first.job_id
    rows = bundle.jobs.list_jobs(job_type=JOB_BACKGROUND_INTELLIGENCE, limit=10)
    assert len(rows) == 2
    payload = _job(bundle, first.job_id)["payload"]
    assert payload["capability"] == NARRATION and payload["priority"] == "knowledge_refresh"
    assert payload["locus"] == "local" and payload["identity"] == first.decision.identity
    # Originating never reserves budget or runs a model.
    assert _usage(root)["background"]["model_calls_held"] == 0


def _originate_in_child(root: str, settings_root: str, queue: Any) -> None:
    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    set_active_store_root(Path(settings_root))
    bundle = _bundle(Path(root))
    origination = _scheduler(bundle, Path(root), _Executor()).originate(NARRATION, _event("race"), {"r": 1})
    queue.put((origination.job_id, origination.enqueued))


def test_concurrent_processes_enqueue_one_job_for_one_event(tmp_path: Path, settings_root: Path) -> None:
    root = tmp_path / "store"
    _bundle(root)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [
        context.Process(target=_originate_in_child, args=(str(root), str(settings_root), queue)) for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
        assert worker.exitcode == 0
    results = [queue.get(timeout=10) for _ in workers]
    assert len({job_id for job_id, _ in results}) == 1
    assert sum(1 for _, enqueued in results if enqueued) == 1
    assert len(_bundle(root).jobs.list_jobs(job_type=JOB_BACKGROUND_INTELLIGENCE, limit=10)) == 1


def _worker(bundle: Any, scheduler: LocalBackgroundScheduler, results: list[dict[str, Any]] | None = None) -> Worker:
    """The real Worker over the scheduler's handler (the jobs table keeps no result: ``results`` does)."""

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        result = scheduler.run_job(payload)
        if results is not None:
            results.append(result)
        return result

    return Worker(bundle, dispatch={JOB_BACKGROUND_INTELLIGENCE: handler})


def test_the_worker_runs_a_queued_job_under_the_budget_and_settles_once(tmp_path: Path) -> None:
    root = tmp_path / "store"
    bundle = _bundle(root)
    executor = _Executor(calls=2)
    scheduler = _scheduler(bundle, root, executor)
    origination = scheduler.originate(NARRATION, _event(), {"repository": "r1"})
    assert _worker(bundle, scheduler).run_once() == origination.job_id
    row = _job(bundle, origination.job_id)
    assert row["status"] == "succeeded", row
    assert executor.executed == [({"repository": "r1"}, 2)]  # granted the capability bound
    usage = _usage(root)
    assert usage["background"]["model_calls_used"] == 2 and usage["background"]["model_calls_held"] == 0
    by_capability = {row["capability"]: row for row in usage["by_capability"]}
    assert by_capability[NARRATION]["admissions"] == 1
    # Replayed durable events converge even after success: one semantic job, one billable run.
    replay = scheduler.originate(NARRATION, _event(), {"repository": "r1"})
    assert replay.enqueued is False and replay.job_id == origination.job_id
    assert _job(bundle, origination.job_id)["status"] == "succeeded"
    assert _worker(bundle, scheduler).run_once() is None


def test_a_stale_source_stops_before_it_reserves_or_runs(tmp_path: Path) -> None:
    root = tmp_path / "store"
    bundle = _bundle(root)
    executor = _Executor()
    scheduler = _scheduler(bundle, root, executor)
    origination = scheduler.originate(NARRATION, _event(), {"repository": "r1"})
    executor.current = False  # a newer source generation landed
    results: list[dict[str, Any]] = []
    _worker(bundle, scheduler, results).run_once()
    row = _job(bundle, origination.job_id)
    assert row["status"] == "succeeded" and executor.executed == []
    assert results == [{"capability": NARRATION, "status": "superseded", "reason": "superseded", "calls_used": 0,
                        "admission": None}]  # fmt: skip
    usage = _usage(root)
    assert usage["background"]["model_calls_used"] == 0 and usage["by_capability"] == []


def test_a_policy_change_supersedes_the_queued_job_and_originates_its_own(tmp_path: Path) -> None:
    root = tmp_path / "store"
    bundle = _bundle(root)
    executor = _Executor()
    scheduler = _scheduler(bundle, root, executor)
    old = scheduler.originate(NARRATION, _event(), {"repository": "r1"})
    settings.write_setting(settings_root_path(), "intelligence.run_on", "local")
    new = scheduler.originate(NARRATION, _event(), {"repository": "r1"})
    assert new.enqueued and new.job_id != old.job_id
    results: list[dict[str, Any]] = []
    worker = _worker(bundle, scheduler, results)
    assert worker.run_once() == old.job_id
    assert worker.run_once() == new.job_id
    assert [result["status"] for result in results] == ["superseded", "completed"]
    assert results[0] == {"status": "superseded", "reason": "superseded"}, "stopped before any reservation"
    assert len(executor.executed) == 1
    assert _usage(root)["by_capability"][0]["admissions"] == 1


def test_background_off_refuses_and_an_invalid_policy_pauses(tmp_path: Path) -> None:
    root = tmp_path / "store"
    bundle = _bundle(root)
    executor = _Executor()
    scheduler = _scheduler(bundle, root, executor)
    queued = scheduler.originate(NARRATION, _event(), {"repository": "r1"})
    settings.write_setting(settings_root_path(), "intelligence.background.enabled", False)
    refused = scheduler.originate(NARRATION, _event("generation-2"), {"repository": "r1"})
    assert refused.decision.reason == SchedulerReason.BACKGROUND_DISABLED.value and not refused.enqueued
    results: list[dict[str, Any]] = []
    _worker(bundle, scheduler, results).run_once()
    row = _job(bundle, queued.job_id)
    assert row["status"] == "succeeded" and results == [{"status": "refused", "reason": "background_disabled"}]
    assert executor.executed == [] and _usage(root)["enabled"] is False

    # An unreadable policy is fixed, not waited out forever: the job pauses (no attempt spent).
    settings.unset_setting(settings_root_path(), "intelligence.background.enabled")
    queued = scheduler.originate(NARRATION, _event("generation-3"), {"repository": "r1"})
    settings._write_json(settings.settings_path(settings_root_path()), {"intelligence.driver": "yes please"})
    with pytest.raises(JobPaused, match="policy_invalid"):
        scheduler.run_job(_job(bundle, queued.job_id)["payload"])
    _worker(bundle, scheduler).run_once()
    row = _job(bundle, queued.job_id)
    assert row["status"] == "paused" and row["attempts"] == 0 and "policy_invalid" in str(row["error"])
    assert _usage(root)["reason"] == AdmissionReason.POLICY_INVALID.value


def _backdate(root: Path, job_id: str, *, seconds: int) -> None:
    stamp = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    with sqlite3.connect(root / "lemoncrow_jobs.db") as conn:
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (stamp, job_id))


def test_a_budget_refusal_pauses_without_spending_an_attempt_and_the_maintenance_tick_resumes_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    bundle = _bundle(root)
    executor = _Executor()
    settings.write_setting(settings_root_path(), "intelligence.background.monthly_model_calls", 0)
    scheduler = _scheduler(bundle, root, executor)
    origination = scheduler.originate(NARRATION, _event(), {"repository": "r1"})
    for _ in range(5):  # more ticks than max_attempts: a pause never dead-letters the job
        _worker(bundle, scheduler).run_once()
        bundle.jobs.resume_paused_jobs(older_than_seconds=0)
    _worker(bundle, scheduler).run_once()
    row = _job(bundle, origination.job_id)
    assert row["status"] == "paused" and row["attempts"] == 0 and executor.executed == []
    assert "background_exhausted" in str(row["error"])

    # Not due yet: a just-paused job waits out the interval.
    state: dict[str, Any] = {}
    now = datetime.now(UTC)
    assert maintenance_tick._maybe_resume_paused_jobs(bundle, state, now=now) == "resumed:0"
    assert _job(bundle, origination.job_id)["status"] == "paused"
    assert maintenance_tick._maybe_resume_paused_jobs(bundle, state, now=now) == "not_due"

    settings.write_setting(settings_root_path(), "intelligence.background.monthly_model_calls", 10)
    _backdate(root, origination.job_id, seconds=maintenance_tick.MAINTENANCE_INTERVAL_SECONDS + 1)
    later = now + timedelta(seconds=maintenance_tick.MAINTENANCE_INTERVAL_SECONDS + 1)
    assert maintenance_tick._maybe_resume_paused_jobs(bundle, state, now=later) == "resumed:1"
    _worker(bundle, scheduler).run_once()
    row = _job(bundle, origination.job_id)
    assert row["status"] == "succeeded" and row["attempts"] == 1 and len(executor.executed) == 1


def test_review_work_is_admitted_before_knowledge_work_under_contention(tmp_path: Path) -> None:
    root = tmp_path / "store"
    bundle = _bundle(root)
    executor = _Executor(calls=2)
    settings.write_setting(settings_root_path(), "intelligence.background.monthly_model_calls", 10)
    scheduler = _scheduler(bundle, root, executor)
    store = LocalBackgroundBudgetStore(root)
    # Nine of ten calls spent: knowledge's 90% share is gone, review's 100% is not.
    run_local_review_auto(budget=store, revision_id="rev-1", max_calls=9, current=lambda: True,
                          execute=lambda granted: BackgroundOutcome(granted))  # fmt: skip
    origination = scheduler.originate(NARRATION, _event(), {"repository": "r1"})
    _worker(bundle, scheduler).run_once()
    row = _job(bundle, origination.job_id)
    assert row["status"] == "paused" and "priority_reserved" in str(row["error"]) and executor.executed == []
    review = run_local_review_auto(budget=store, revision_id="rev-2", max_calls=4, current=lambda: True,
                                   execute=lambda granted: BackgroundOutcome(granted, None, granted))  # fmt: skip
    assert review.status == "completed" and review.result == 1  # the last call goes to review work
    usage = _usage(root)
    priorities = {item["priority"]: item for item in usage["priorities"]}
    assert priorities["review_auto"]["state"] == "paused" and priorities["knowledge_refresh"]["state"] == "paused"


def _resume_in_child(root: str, settings_root: str, queue: Any) -> None:
    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    set_active_store_root(Path(settings_root))
    bundle = _bundle(Path(root))
    executor = _Executor(calls=1)
    processed = _worker(bundle, _scheduler(bundle, Path(root), executor)).run_once()
    queue.put((str(processed), len(executor.executed)))


def test_a_queued_job_runs_in_a_new_process(tmp_path: Path, settings_root: Path) -> None:
    root = tmp_path / "store"
    bundle = _bundle(root)
    origination = _scheduler(bundle, root, _Executor()).originate(NARRATION, _event(), {"repository": "r1"})
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    child = context.Process(target=_resume_in_child, args=(str(root), str(settings_root), queue))
    child.start()
    child.join(timeout=120)
    assert child.exitcode == 0
    assert queue.get(timeout=10) == (origination.job_id, 1)
    assert _job(bundle, origination.job_id)["status"] == "succeeded"
    assert _usage(root)["background"]["model_calls_used"] == 1


class _LogsEachRun(_Executor):
    """An executor that appends one line per execution to a file every process shares."""

    def __init__(self, log: str) -> None:
        super().__init__(calls=1)
        self.log = log

    def execute(self, subject: Mapping[str, Any], *, max_calls: int) -> BackgroundOutcome:
        with open(self.log, "a", encoding="utf-8") as sink:
            sink.write(f"{subject['repository']}\n")
        return super().execute(subject, max_calls=max_calls)


def _drain_in_child(root: str, settings_root: str, barrier: Any, log: str) -> None:
    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    set_active_store_root(Path(settings_root))
    bundle = _bundle(Path(root))
    worker = _worker(bundle, _scheduler(bundle, Path(root), _LogsEachRun(log)))
    barrier.wait(60)
    while worker.run_once() is not None:
        pass


def _race_two_workers(root: Path, settings_root: Path, log: Path) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    workers = [
        context.Process(target=_drain_in_child, args=(str(root), str(settings_root), barrier, str(log)))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(120)
        assert worker.exitcode == 0


def test_two_workers_racing_run_each_queued_job_once(tmp_path: Path, settings_root: Path) -> None:
    root, log = tmp_path / "store", tmp_path / "runs.log"
    bundle = _bundle(root)
    scheduler = _scheduler(bundle, root, _Executor())
    jobs = [scheduler.originate(NARRATION, _event(f"g-{n}"), {"repository": f"r{n}"}).job_id for n in range(8)]
    _race_two_workers(root, settings_root, log)
    assert sorted(log.read_text().split()) == sorted(f"r{n}" for n in range(8)), "each job ran exactly once"
    rows = [_job(bundle, job_id) for job_id in jobs]
    assert {row["status"] for row in rows} == {"succeeded"} and {row["attempts"] for row in rows} == {1}
    usage = _usage(root)["background"]
    assert (usage["model_calls_used"], usage["model_calls_held"]) == (8, 0)


def test_two_workers_racing_at_the_limit_pause_every_job_without_a_call(tmp_path: Path, settings_root: Path) -> None:
    root, log = tmp_path / "store", tmp_path / "runs.log"
    bundle = _bundle(root)
    settings.write_setting(settings_root, "intelligence.background.monthly_model_calls", 0)
    scheduler = _scheduler(bundle, root, _Executor())
    jobs = [scheduler.originate(NARRATION, _event(f"g-{n}"), {"repository": f"r{n}"}).job_id for n in range(4)]
    _race_two_workers(root, settings_root, log)
    assert not log.exists(), "no model call past the limit"
    rows = [_job(bundle, job_id) for job_id in jobs]
    assert {row["status"] for row in rows} == {"paused"} and {row["attempts"] for row in rows} == {0}


class _DiesMidCall:
    """An executor whose process dies inside the model call: reserved, never settled."""

    def is_current(self, subject: Mapping[str, Any]) -> bool:
        return True

    def execute(self, subject: Mapping[str, Any], *, max_calls: int) -> BackgroundOutcome:
        os._exit(3)


def _die_in_child(root: str, settings_root: str) -> None:
    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    set_active_store_root(Path(settings_root))
    bundle = _bundle(Path(root))
    scheduler = LocalBackgroundScheduler.for_root(bundle.jobs, Path(root), executors={NARRATION: _DiesMidCall()})
    _worker(bundle, scheduler).run_once()


def test_a_worker_that_dies_mid_job_is_reclaimed_once_and_its_dead_attempt_is_charged_once(
    tmp_path: Path, settings_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate K / KQ: a crashed local worker's job is reclaimed after its lease, never run twice at once."""

    monkeypatch.delenv("LEMONCROW_JOB_LEASE_SECONDS", raising=False)
    root = tmp_path / "store"
    bundle = _bundle(root)
    origination = _scheduler(bundle, root, _Executor()).originate(NARRATION, _event(), {"repository": "r1"})
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_die_in_child, args=(str(root), str(settings_root)))
    child.start()
    child.join(timeout=120)
    assert child.exitcode == 3, "the worker died inside the call"
    row = _job(bundle, origination.job_id)
    assert row["status"] == "running" and row["attempts"] == 1
    granted = capability(NARRATION).max_calls  # type: ignore[union-attr]
    assert _usage(root)["background"]["model_calls_held"] == granted

    # Inside the lease nobody else runs it.
    executor = _Executor(calls=1)
    scheduler = _scheduler(bundle, root, executor)
    assert _worker(bundle, scheduler).run_once() is None and executor.executed == []

    # Past the lease the job is reaped and retried once, under a fresh reservation.
    stamp = (datetime.now(UTC) - timedelta(seconds=901)).isoformat()
    with sqlite3.connect(root / "lemoncrow_jobs.db") as conn:
        conn.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stamp, origination.job_id))
    assert _worker(bundle, scheduler).run_once() == origination.job_id
    row = _job(bundle, origination.job_id)
    assert row["status"] == "succeeded" and row["attempts"] == 2 and len(executor.executed) == 1
    usage = _usage(root)["background"]
    assert usage["model_calls_used"] == 1 and usage["model_calls_held"] == granted
    assert _worker(bundle, scheduler).run_once() is None

    # The dead attempt may have reached the provider: its hold expires charged in full, exactly once.
    later = LocalBackgroundBudgetStore(root, clock=lambda: time.time() + DEFAULT_HOLD_S + 1)
    for _ in range(2):
        usage = later.status()["background"]
        assert usage["model_calls_used"] == 1 + granted and usage["model_calls_held"] == 0


def test_the_default_worker_runs_pk1_executors_and_ships_no_unlanded_one(tmp_path: Path) -> None:
    root = tmp_path / "store"
    bundle = _bundle(root)
    origination = _scheduler(bundle, root, _Executor()).originate(NARRATION, _event(), {"repository": "r1"})
    # The production dispatch registers PK1's executor: a job naming no stored generation is stale, not run.
    worker = Worker(bundle)
    handler = worker._dispatch[JOB_BACKGROUND_INTELLIGENCE]
    payload = _job(bundle, origination.job_id)["payload"]
    assert handler(payload) == {
        "capability": NARRATION,
        "status": "superseded",
        "reason": SchedulerReason.SUPERSEDED.value,
        "calls_used": 0,
        "admission": None,
    }
    assert worker.run_once() == origination.job_id
    assert _job(bundle, origination.job_id)["status"] == "succeeded"
    assert _usage(root)["by_capability"] == []
    # The feedback digest has no executor (DS0b), and local Auto stays explicit: neither is originated as a job.
    plain = LocalBackgroundScheduler.for_root(bundle.jobs, root)
    for capability_id, event in (
        ("review.feedback_digest", BackgroundEvent("review.thread_summaries_changed", "thread")),
        (REVIEW_AUTO_AI, BackgroundEvent(SNAPSHOT_CAPTURED, "rev")),
    ):
        refused = plain.originate(capability_id, event, {})
        assert refused.decision.reason == SchedulerReason.EXECUTOR_UNAVAILABLE.value and not refused.enqueued
    assert plain.originate(NARRATION, _event("x"), {}).enqueued
    # DS0 registered its executor: a thread change queues (it runs only against a real Review store).
    assert plain.originate("review.thread_summary", BackgroundEvent("review.thread_changed", "thread"), {}).enqueued
