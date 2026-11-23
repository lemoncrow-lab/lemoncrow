"""BI1: the shared background budget contract and its local (SQLite) ledger."""

from __future__ import annotations

import multiprocessing
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.background_budget import (
    BACKGROUND_PRIORITIES,
    MAX_POLICY_CALLS,
    MAX_POLICY_MICROS,
    PRIORITY_ENVELOPE_PERCENT,
    AdmissionReason,
    BackgroundBudgetPolicies,
    BackgroundBudgetPolicy,
    BackgroundBudgetPolicyError,
    BackgroundWorkRequest,
    BudgetConflict,
    BudgetLane,
    BudgetOverrun,
    CeilingInput,
    CeilingSource,
    JobPriority,
    PeriodTotals,
    PriorityTier,
    SqlLedgerTransaction,
    UnknownReservation,
    day_start,
    decide,
    extend_in,
    lane_for,
    month_start,
    next_month_start,
    read_status,
    reserve_in,
    settle_in,
    usd_to_micros,
)
from lemoncrow.pro.capabilities.review.background_budget_store import (
    SQLITE_SCHEMA,
    LocalBackgroundBudgetStore,
    local_background_policy,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC).timestamp()
AUTO = JobPriority.REVIEW_AUTO
KNOW = JobPriority.KNOWLEDGE_REFRESH
DISC = JobPriority.DISCUSSION_SUMMARY
LEARN = JobPriority.LEARNING_SUGGESTION
REQ = JobPriority.REVIEW_REQUIRED


def _req(key: str = "k", *, priority: JobPriority = AUTO, max_calls: int = 1, **kw: Any) -> BackgroundWorkRequest:
    return BackgroundWorkRequest(key=key, capability=kw.pop("capability", "review.ai"), priority=priority,
                                 max_calls=max_calls, **kw)  # fmt: skip


def _policy(**kw: Any) -> BackgroundBudgetPolicy:
    base: dict[str, Any] = {"monthly_model_calls": 100, "monthly_managed_micros": None, "daily_model_calls": None}
    base.update(kw)
    return BackgroundBudgetPolicy(**base)


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #


def test_priority_vocabulary_is_the_rc8_one_and_lanes_follow_it() -> None:
    assert BACKGROUND_PRIORITIES == {KNOW, DISC, LEARN}
    assert {p.tier for p in BACKGROUND_PRIORITIES} == {PriorityTier.BACKGROUND}
    assert [p.rank for p in (REQ, AUTO, KNOW, DISC, LEARN)] == [1, 2, 3, 4, 5]
    assert lane_for(REQ) is BudgetLane.INTERACTIVE
    assert lane_for(AUTO) is BudgetLane.BACKGROUND
    assert lane_for(LEARN, explicit=True) is BudgetLane.INTERACTIVE
    # Lower priority never gets a larger share than a higher one.
    shares = [PRIORITY_ENVELOPE_PERCENT[p] for p in (AUTO, KNOW, DISC, LEARN)]
    assert shares == sorted(shares, reverse=True) and shares[0] == 100


def test_reason_codes_are_stable_tokens() -> None:
    assert {reason.value for reason in AdmissionReason} == {
        "admitted",
        "background_disabled",
        "background_exhausted",
        "daily_guard_exhausted",
        "managed_spend_exhausted",
        "cost_unmeasurable",
        "priority_reserved",
        "operation_limit",
        "capability_limit",
        "review_limit",
        "hosted_quota_exhausted",
        "policy_invalid",
        "managed_unavailable",
        "ai_budget_exhausted",
    }


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #


def test_policy_parsing_merges_nulls_and_refuses_unknown_or_malformed_values() -> None:
    default = BackgroundBudgetPolicy()
    assert default.monthly_model_calls == 200 and default.monthly_managed_micros == 25_000_000
    parsed = BackgroundBudgetPolicy.from_mapping(
        {"monthly_model_calls": 0, "monthly_managed_usd": "12.345678", "daily_model_calls": None}
    )
    assert parsed.monthly_model_calls == 0 and parsed.monthly_managed_micros == 12_345_678
    assert parsed.daily_model_calls is None
    assert BackgroundBudgetPolicy.from_mapping({"monthly_managed_usd": None}).monthly_managed_micros is None
    assert usd_to_micros(0.1) == 100_000
    for bad in (
        {"monthly_model_calls": -1},
        {"monthly_model_calls": True},
        {"monthly_model_calls": 1.5},
        {"monthly_managed_usd": "NaN"},
        {"monthly_managed_usd": -1},
        {"enabled": "yes"},
        {"monthly_calls": 3},
    ):
        with pytest.raises(BackgroundBudgetPolicyError):
            BackgroundBudgetPolicy.from_mapping(bad)
    narrowed = _policy(monthly_model_calls=50, daily_model_calls=None).narrowed(
        _policy(monthly_model_calls=80, daily_model_calls=5, enabled=False)
    )
    assert (narrowed.monthly_model_calls, narrowed.daily_model_calls, narrowed.enabled) == (50, 5, False)


def test_policies_per_scope_overrides_merge_onto_defaults() -> None:
    policies = BackgroundBudgetPolicies.parse(
        {
            "defaults": {"monthly_model_calls": 500, "monthly_managed_usd": 50},
            "overrides": {"org_trial": {"monthly_model_calls": 50}},
        }
    )
    assert policies.for_scope("org_x").monthly_model_calls == 500
    trial = policies.for_scope("org_trial")
    assert trial.monthly_model_calls == 50 and trial.monthly_managed_micros == 50_000_000
    assert BackgroundBudgetPolicies.parse(None) == BackgroundBudgetPolicies.standard()
    for bad in ({"default": {}}, {"overrides": []}, {"overrides": {"": {}}}, []):
        with pytest.raises(BackgroundBudgetPolicyError):
            BackgroundBudgetPolicies.parse(bad)


def test_local_policy_reads_settings_keys_and_negative_means_no_ceiling() -> None:
    policy = local_background_policy(
        {
            "intelligence.background.enabled": False,
            "intelligence.background.monthly_model_calls": -1,
            "intelligence.background.daily_model_calls": 7,
            "intelligence.background.monthly_managed_usd": 999,  # not a local setting: ignored
        }
    )
    assert policy.enabled is False and policy.monthly_model_calls is None and policy.daily_model_calls == 7
    with pytest.raises(BackgroundBudgetPolicyError, match=r"intelligence\.background"):
        local_background_policy({"intelligence.background.monthly_model_calls": "lots"})
    from lemoncrow.core.settings_registry import SETTINGS

    keys = {spec.key for spec in SETTINGS}
    assert {"intelligence.background.monthly_model_calls", "intelligence.background.daily_model_calls"} <= keys


# --------------------------------------------------------------------------- #
# decide (pure)
# --------------------------------------------------------------------------- #


def test_admission_grants_the_most_restrictive_ceiling() -> None:
    request = _req(max_calls=40, min_calls=1)
    external = [CeilingInput.operation(30), CeilingInput.capability(25), CeilingInput.review(20, used=8)]
    decision = decide(request, _policy(monthly_model_calls=100), month=PeriodTotals(calls_used=90), day=PeriodTotals(),
                      external=external)  # fmt: skip
    assert decision.admitted and decision.granted_calls == 10  # the envelope (100-90) binds
    decision = decide(request, _policy(monthly_model_calls=100), month=PeriodTotals(), day=PeriodTotals(),
                      external=external)  # fmt: skip
    assert decision.granted_calls == 12  # the Review (20-8) binds
    decision = decide(request, _policy(daily_model_calls=3), month=PeriodTotals(), day=PeriodTotals(calls_held=1),
                      external=external)  # fmt: skip
    assert decision.granted_calls == 2  # the daily guard binds


@pytest.mark.parametrize(
    ("external", "policy", "month", "day", "reason"),
    [
        ([CeilingInput.operation(1)], _policy(), PeriodTotals(), PeriodTotals(), AdmissionReason.OPERATION_LIMIT),
        ([CeilingInput.capability(1)], _policy(), PeriodTotals(), PeriodTotals(), AdmissionReason.CAPABILITY_LIMIT),
        ([CeilingInput.review(5, used=4)], _policy(), PeriodTotals(), PeriodTotals(), AdmissionReason.REVIEW_LIMIT),
        ([], _policy(monthly_model_calls=10), PeriodTotals(calls_used=9), PeriodTotals(),
         AdmissionReason.BACKGROUND_EXHAUSTED),
        ([], _policy(daily_model_calls=3), PeriodTotals(), PeriodTotals(calls_used=2),
         AdmissionReason.DAILY_GUARD_EXHAUSTED),
        ([], _policy(enabled=False), PeriodTotals(), PeriodTotals(), AdmissionReason.BACKGROUND_DISABLED),
    ],
)  # fmt: skip
def test_refusal_names_the_ceiling_that_refused(
    external: list[CeilingInput],
    policy: BackgroundBudgetPolicy,
    month: PeriodTotals,
    day: PeriodTotals,
    reason: AdmissionReason,
) -> None:
    decision = decide(_req(max_calls=4, min_calls=2), policy, month=month, day=day, external=external)
    assert decision.reason is reason and decision.granted_calls == 0


def test_low_priority_pauses_first_as_the_envelope_drains() -> None:
    policy = _policy(monthly_model_calls=100)

    def open_for(used: int) -> set[JobPriority]:
        month = PeriodTotals(calls_used=used)
        return {p for p in (AUTO, KNOW, DISC, LEARN) if decide(_req(priority=p), policy, month=month,
                                                                   day=PeriodTotals()).admitted}  # fmt: skip

    assert open_for(0) == {AUTO, KNOW, DISC, LEARN}
    assert open_for(70) == {AUTO, KNOW, DISC}
    assert open_for(80) == {AUTO, KNOW}
    assert open_for(90) == {AUTO}
    assert open_for(100) == set()
    paused = decide(_req(priority=LEARN), policy, month=PeriodTotals(calls_used=75), day=PeriodTotals())
    assert paused.reason is AdmissionReason.PRIORITY_RESERVED
    exhausted = decide(_req(priority=LEARN), policy, month=PeriodTotals(calls_used=100), day=PeriodTotals())
    assert exhausted.reason is AdmissionReason.BACKGROUND_EXHAUSTED


def test_explicit_and_required_work_is_never_refused_by_the_envelope_but_obeys_other_ceilings() -> None:
    drained = _policy(monthly_model_calls=0, daily_model_calls=0, monthly_managed_micros=0, enabled=False)
    for request in (_req(priority=REQ, max_calls=5), _req(priority=LEARN, max_calls=5, explicit=True)):
        assert request.lane is BudgetLane.INTERACTIVE
        decision = decide(request, drained, month=PeriodTotals(calls_used=10**6), day=PeriodTotals(calls_used=10**6))
        assert decision.admitted and decision.granted_calls == 5
        bound = decide(request, drained, month=PeriodTotals(), day=PeriodTotals(),
                       external=[CeilingInput.review(3, used=3)])  # fmt: skip
        assert bound.reason is AdmissionReason.REVIEW_LIMIT


def test_unknown_cost_cannot_pretend_to_be_zero_against_a_dollar_ceiling() -> None:
    policy = _policy(monthly_managed_micros=1_000_000)
    for cost in (None, 0):
        managed = _req(locus="managed", cost_per_call_micros=cost)
        decision = decide(managed, policy, month=PeriodTotals(), day=PeriodTotals())
        assert decision.reason is AdmissionReason.COST_UNMEASURABLE
        assert decision.binding is CeilingSource.MANAGED_SPEND
        # A hosted dollar quota binds explicit managed work the same way.
        explicit = _req(locus="managed", cost_per_call_micros=cost, explicit=True)
        quota = decide(explicit, policy, month=PeriodTotals(), day=PeriodTotals(),
                       external=[CeilingInput.hosted_quota(10**9)])  # fmt: skip
        assert quota.reason is AdmissionReason.COST_UNMEASURABLE and quota.binding is CeilingSource.HOSTED_QUOTA
    # Local/worker routes are not under a dollar ceiling: unknown cost is counted, not refused.
    for locus in ("local", "worker"):
        assert decide(_req(locus=locus), policy, month=PeriodTotals(), day=PeriodTotals()).admitted
    # Managed with an estimate: the dollar ceiling binds by estimate.
    priced = _req(locus="managed", cost_per_call_micros=300_000, max_calls=10)
    decision = decide(priced, policy, month=PeriodTotals(micros_used=100_000), day=PeriodTotals())
    assert decision.granted_calls == 3 and decision.held_micros == 900_000
    spent = decide(priced, policy, month=PeriodTotals(micros_used=900_000), day=PeriodTotals())
    assert spent.reason is AdmissionReason.MANAGED_SPEND_EXHAUSTED
    quota = decide(_req(locus="managed", cost_per_call_micros=10, explicit=True), policy, month=PeriodTotals(),
                   day=PeriodTotals(), external=[CeilingInput.hosted_quota(5)])  # fmt: skip
    assert quota.reason is AdmissionReason.HOSTED_QUOTA_EXHAUSTED


def test_request_validation_and_fingerprint() -> None:
    for bad in ({"max_calls": 0}, {"min_calls": 3, "max_calls": 2}, {"locus": "cloud"}, {"capability": "a b"},
                {"cost_per_call_micros": -1}, {"hold_s": 1.0}):  # fmt: skip
        with pytest.raises(ValueError):
            _req(**bad)
    assert _req("a").fingerprint() == _req("b").fingerprint()
    assert _req("a").fingerprint() != _req("a", max_calls=2).fingerprint()
    assert _req("a").fingerprint() != _req("a", explicit=True).fingerprint()


def test_periods_are_utc_calendar_months_and_days() -> None:
    assert month_start(NOW) == int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
    assert next_month_start(int(datetime(2026, 12, 1, tzinfo=UTC).timestamp())) == int(
        datetime(2027, 1, 1, tzinfo=UTC).timestamp()
    )
    assert day_start(NOW) == int(datetime(2026, 10, 15, tzinfo=UTC).timestamp())


# --------------------------------------------------------------------------- #
# local ledger
# --------------------------------------------------------------------------- #


def _store(tmp_path: Path, policy: BackgroundBudgetPolicy | None = None, clock: _Clock | None = None) -> Any:
    return LocalBackgroundBudgetStore(tmp_path, policy=policy or _policy(), clock=clock or _Clock())


def test_reservation_is_idempotent_and_settlement_exactly_once(tmp_path: Path) -> None:
    store = _store(tmp_path, _policy(monthly_model_calls=10))
    first = store.reserve(_req("attempt-1", max_calls=4))
    assert first.admitted and first.granted_calls == 4 and not first.replayed
    again = store.reserve(_req("attempt-1", max_calls=4))
    assert again.admitted and again.replayed and again.granted_calls == 4
    assert store.status()["background"]["model_calls_held"] == 4
    with pytest.raises(BudgetConflict):
        store.reserve(_req("attempt-1", max_calls=5))
    with pytest.raises(BudgetOverrun):
        store.settle("attempt-1", calls=5)
    settled = store.settle("attempt-1", calls=3)
    assert settled.calls_used == 3 and not settled.replayed and settled.cost_state == "unmeasured"
    assert store.settle("attempt-1", calls=3).replayed
    with pytest.raises(BudgetConflict):
        store.settle("attempt-1", calls=2)
    with pytest.raises(UnknownReservation):
        store.settle("never", calls=1)
    status = store.status()["background"]
    assert (status["model_calls_used"], status["model_calls_held"], status["model_calls_remaining"]) == (3, 0, 7)
    # A reserved key replays even once settled, so a retried admission never charges twice.
    assert store.reserve(_req("attempt-1", max_calls=4)).replayed
    store.reserve(_req("attempt-2", max_calls=2))
    assert store.release("attempt-2").calls_used == 0
    assert store.status()["background"]["model_calls_remaining"] == 7


def test_refusal_stores_nothing_and_the_key_can_be_admitted_later(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, _policy(monthly_model_calls=2), clock)
    assert store.reserve(_req("a", max_calls=2)).admitted
    refused = store.reserve(_req("b"))
    assert refused.reason is AdmissionReason.BACKGROUND_EXHAUSTED and refused.state == "refused"
    clock.now = next_month_start(month_start(NOW)) + 10
    assert store.reserve(_req("b")).admitted  # next month, same key


def test_an_unsettled_hold_expires_and_is_charged_in_full_then_a_late_settle_corrects_it_once(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, _policy(monthly_model_calls=10), clock)
    assert store.reserve(_req("lost", max_calls=6, hold_s=60)).granted_calls == 6
    clock.now += 61
    # The next admission expires the hold first: charged as fully used (fail-safe).
    assert store.reserve(_req("next", max_calls=10)).granted_calls == 4
    status = store.status()["background"]
    assert status["model_calls_used"] == 6 and status["model_calls_held"] == 4
    late = store.settle("lost", calls=1)
    assert late.calls_used == 1
    assert store.status()["background"]["model_calls_used"] == 1
    assert store.settle("lost", calls=1).replayed
    with pytest.raises(BudgetConflict):
        store.settle("lost", calls=2)


def test_settlement_after_rollover_is_charged_to_the_admitting_period(tmp_path: Path) -> None:
    clock = _Clock(next_month_start(month_start(NOW)) - 30)
    store = _store(tmp_path, _policy(monthly_model_calls=5), clock)
    assert store.reserve(_req("late", max_calls=5)).admitted
    clock.now += 60  # a new month begins while the attempt runs
    fresh = store.status()["background"]
    assert fresh["model_calls_used"] == 0 and fresh["model_calls_remaining"] == 5
    store.settle("late", calls=5)
    assert store.status()["background"]["model_calls_remaining"] == 5
    assert store.reserve(_req("new", max_calls=5)).granted_calls == 5


def test_rollover_collects_accounting_past_retention(tmp_path: Path) -> None:
    clock = _Clock()
    store = LocalBackgroundBudgetStore(tmp_path, policy=_policy(), clock=clock, retention_s=40 * 86400)
    store.reserve(_req("old"))
    store.settle("old", calls=1)
    store.reserve(_req("old-held", hold_s=7 * 86400))
    for _ in range(3):
        clock.now = next_month_start(month_start(clock.now)) + 5
        store.reserve(_req(f"k{clock.now}"))
    with sqlite3.connect(tmp_path / "background_budget.db") as conn:
        starts = {row[0] for row in conn.execute("SELECT period_start FROM background_budget_periods")}
        old_rows = conn.execute(
            "SELECT COUNT(*) FROM background_budget_reservations WHERE month_start = ?", (month_start(NOW),)
        ).fetchone()[0]
        kept = conn.execute("SELECT COUNT(*) FROM background_budget_reservations").fetchone()[0]
    assert min(starts) >= month_start(clock.now - 40 * 86400)  # collected below the retention month
    assert old_rows == 0  # settled and expired-then-charged rows both closed
    assert kept == 3  # the three newer months' reservations stay


def test_unknown_cost_local_routes_are_counted_never_priced(tmp_path: Path) -> None:
    store = _store(tmp_path, _policy(monthly_managed_micros=1_000_000))
    store.reserve(_req("a", max_calls=3))
    store.settle("a", calls=3)
    store.reserve(_req("b", max_calls=2, locus="worker"))
    store.settle("b", calls=2, cost_micros=40_000)
    status = store.status()
    assert status["managed_spend"]["applies"] is False and status["managed_spend"]["enforced"] is False
    assert status["background"]["managed_usd_remaining"] is None
    # Three calls of unknown cost: no figure may read as a complete total.
    background = status["background"]
    assert background["cost_complete"] is False and background["unmeasured_cost_calls"] == 3
    assert background["managed_usd_used"] is None and background["reported_other_usd"] is None
    assert (background["measured_managed_usd"], background["measured_other_usd"]) == (0.0, 0.04)
    by_cap = {row["capability"]: row for row in status["by_capability"]}
    assert by_cap["review.ai"]["model_calls_used"] == 5
    assert by_cap["review.ai"]["cost_complete"] is False and by_cap["review.ai"]["reported_other_usd"] is None
    # Interactive work with unknown cost is marked incomplete the same way; complete lanes keep their totals.
    store.reserve(_req("ask", priority=LEARN, explicit=True, max_calls=2))
    store.settle("ask", calls=2)
    interactive = store.status()["interactive"]
    assert interactive["cost_complete"] is False and interactive["managed_usd_used"] is None
    clean = _store(tmp_path / "clean").status()["background"]
    assert clean["cost_complete"] is True and (clean["managed_usd_used"], clean["reported_other_usd"]) == (0.0, 0.0)


def test_manual_work_is_metered_separately_and_still_runs_when_background_is_exhausted(tmp_path: Path) -> None:
    store = _store(tmp_path, _policy(monthly_model_calls=2, enabled=True))
    assert store.reserve(_req("bg", max_calls=2)).admitted
    assert store.reserve(_req("bg2")).reason is AdmissionReason.BACKGROUND_EXHAUSTED
    manual = store.reserve(_req("manual", priority=LEARN, explicit=True, max_calls=7))
    fresh = store.reserve(_req("fresh", priority=REQ, max_calls=3))
    assert manual.admitted and fresh.admitted and manual.lane is BudgetLane.INTERACTIVE
    store.settle("manual", calls=7)
    status = store.status()
    assert status["interactive"]["model_calls_used"] == 7 and status["interactive"]["model_calls_held"] == 3
    assert status["background"]["model_calls_held"] == 2 and status["background"]["model_calls_remaining"] == 0
    assert status["state"] == "paused" and status["reason"] == "background_exhausted"


def test_status_reports_priority_states(tmp_path: Path) -> None:
    store = _store(tmp_path, _policy(monthly_model_calls=10))
    store.reserve(_req("a", max_calls=8))
    status = store.status()
    states = {row["priority"]: row["state"] for row in status["priorities"]}
    assert states == {"review_auto": "running", "knowledge_refresh": "running", "discussion_summary": "paused",
                      "learning_suggestion": "paused"}  # fmt: skip
    assert status["state"] == "limited" and status["reason"] == "priority_reserved"
    disabled = _store(tmp_path, _policy(enabled=False)).status()
    assert disabled["state"] == "disabled" and disabled["reason"] == "background_disabled"


def test_the_ledger_stores_no_raw_key_or_review_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.reserve(_req("review:rev_secret_123:attempt:1"))
    raw = (tmp_path / "background_budget.db").read_bytes()
    assert b"rev_secret_123" not in raw


_PROCS = 6
_PER_PROC = 25
_CAP = 40


def _fixed_clock() -> float:
    return NOW


def _race(root: str, worker: int, queue: Any) -> None:
    store = LocalBackgroundBudgetStore(root, policy=_policy(monthly_model_calls=_CAP), clock=_fixed_clock)
    granted = 0
    for index in range(_PER_PROC):
        admission = store.reserve(_req(f"w{worker}-{index}", max_calls=2))
        granted += admission.granted_calls
        # Every worker also retries one shared key: it may be charged once only.
        shared = store.reserve(_req("shared-attempt", max_calls=1))
        if shared.admitted and not shared.replayed:
            granted += shared.granted_calls
    queue.put(granted)


def test_concurrent_processes_cannot_overspend_the_envelope_or_double_charge(tmp_path: Path) -> None:
    LocalBackgroundBudgetStore(tmp_path, policy=_policy(), clock=_fixed_clock).status()  # create the schema
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    procs = [context.Process(target=_race, args=(str(tmp_path), worker, queue)) for worker in range(_PROCS)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(120)
        assert proc.exitcode == 0
    granted = sum(queue.get(timeout=5) for _ in procs)
    status = LocalBackgroundBudgetStore(tmp_path, policy=_policy(monthly_model_calls=_CAP), clock=_fixed_clock).status()
    assert granted == _CAP  # demand (6*25*2+1) far exceeds the cap; the cap is filled exactly
    assert status["background"]["model_calls_held"] == _CAP
    with sqlite3.connect(tmp_path / "background_budget.db") as conn:
        total = conn.execute("SELECT SUM(granted_calls) FROM background_budget_reservations").fetchone()[0]
    assert total == _CAP


@pytest.mark.parametrize(
    ("spent", "granted"),
    [(6, 4), (7, 3), (9, 1), (10, 0), (15, 0)],
    ids=["room-for-all", "one-call-short", "the-last-call", "at-the-limit", "past-the-limit"],
)
def test_the_local_envelope_at_and_past_its_limit(tmp_path: Path, spent: int, granted: int) -> None:
    wide = LocalBackgroundBudgetStore(tmp_path, policy=_policy(monthly_model_calls=100), clock=_fixed_clock)
    assert wide.reserve(_req("earlier", max_calls=spent)).granted_calls == spent
    wide.settle("earlier", calls=spent)
    store = LocalBackgroundBudgetStore(tmp_path, policy=_policy(monthly_model_calls=10), clock=_fixed_clock)
    admission = store.reserve(_req("next", max_calls=4))
    assert admission.granted_calls == granted
    if granted == 0:
        assert admission.reason is AdmissionReason.BACKGROUND_EXHAUSTED
    usage = store.status()["background"]
    assert (usage["model_calls_used"], usage["model_calls_held"]) == (spent, granted)


# --------------------------------------------------------------------------- #
# review fixes: bounds, settlement, fail-closed policy, projection, locus
# --------------------------------------------------------------------------- #


class _Ledger:
    """The shared ledger over one in-memory SQLite scope, with managed execution available (as hosted)."""

    def __init__(self, policy: BackgroundBudgetPolicy, clock: _Clock) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SQLITE_SCHEMA)
        self.policy, self.clock = policy, clock

    def _txn(self) -> Any:
        ledger = self

        class _Txn:
            def __enter__(self) -> SqlLedgerTransaction:
                ledger.conn.execute("BEGIN IMMEDIATE")
                return SqlLedgerTransaction(ledger.conn, "org", paramstyle="named")

            def __exit__(self, *exc: object) -> None:
                ledger.conn.execute("ROLLBACK" if exc[0] else "COMMIT")

        return _Txn()

    def reserve(self, request: BackgroundWorkRequest) -> Any:
        with self._txn() as txn:
            return reserve_in(txn, request, self.policy, now=self.clock())

    def settle(self, key: str, **kw: Any) -> Any:
        with self._txn() as txn:
            return settle_in(txn, key, now=self.clock(), **{"cost_micros": None, **kw})

    def extend(self, key: str, until: float) -> bool:
        with self._txn() as txn:
            return extend_in(txn, key, until=until, now=self.clock())

    def status(self) -> dict[str, Any]:
        with self._txn() as txn:
            return read_status(txn, self.policy, now=self.clock(), managed_available=True)


def test_the_dollar_ceiling_accepts_what_usd_to_micros_accepts_and_names_the_setting() -> None:
    assert BackgroundBudgetPolicy.from_mapping({"monthly_managed_usd": 2500}).monthly_managed_micros == 2_500_000_000
    parsed = BackgroundBudgetPolicies.parse({"defaults": {"monthly_managed_usd": 2500}})
    assert parsed.for_scope("org").monthly_managed_micros == 2_500_000_000
    assert usd_to_micros(MAX_POLICY_MICROS / 1_000_000) == MAX_POLICY_MICROS
    BackgroundBudgetPolicy(monthly_managed_micros=MAX_POLICY_MICROS)
    with pytest.raises(BackgroundBudgetPolicyError, match="monthly_managed_usd"):
        BackgroundBudgetPolicy(monthly_managed_micros=MAX_POLICY_MICROS + 1)
    with pytest.raises(BackgroundBudgetPolicyError, match="monthly_managed_usd"):
        BackgroundBudgetPolicy.from_mapping({"monthly_managed_usd": MAX_POLICY_MICROS})
    BackgroundBudgetPolicy(monthly_model_calls=MAX_POLICY_CALLS, daily_model_calls=MAX_POLICY_CALLS)
    with pytest.raises(BackgroundBudgetPolicyError):
        BackgroundBudgetPolicy.from_mapping({"monthly_model_calls": MAX_POLICY_CALLS + 1})


def test_a_settlement_with_no_completed_call_still_charges_its_reported_cost() -> None:
    ledger = _Ledger(_policy(monthly_model_calls=None, monthly_managed_micros=1_000_000), _Clock())
    assert ledger.reserve(_req("m", locus="managed", cost_per_call_micros=100_000, max_calls=10)).admitted
    settled = ledger.settle("m", calls=0, cost_micros=900_000)
    assert (settled.micros_used, settled.cost_state) == (900_000, "measured")
    assert ledger.status()["background"]["managed_usd_used"] == 0.9
    assert ledger.reserve(_req("m2", locus="managed", cost_per_call_micros=100_000, max_calls=10)).granted_calls == 1
    ledger.reserve(_req("w", locus="worker", max_calls=2))
    assert ledger.settle("w", calls=0, cost_micros=40_000).cost_state == "measured"
    assert ledger.status()["background"]["reported_other_usd"] == 0.04
    ledger.reserve(_req("r", max_calls=2))
    assert ledger.settle("r", calls=0, cost_micros=0).cost_state == "none"  # a release charges nothing


def test_background_disabled_is_reported_before_cost_unmeasurable() -> None:
    policy = _policy(enabled=False, monthly_managed_micros=1_000_000)
    quota = [CeilingInput.hosted_quota(10**9)]  # an unpriced managed request fails this ceiling first otherwise
    for external in ((), quota):
        decision = decide(_req(locus="managed"), policy, month=PeriodTotals(), day=PeriodTotals(), external=external)
        assert decision.reason is AdmissionReason.BACKGROUND_DISABLED
        invalid = decide(_req(locus="managed"), BackgroundBudgetPolicy.invalid(), month=PeriodTotals(),
                         day=PeriodTotals(), external=external)  # fmt: skip
        assert invalid.reason is AdmissionReason.POLICY_INVALID
    # The interactive lane is not the envelope's: its unpriced managed work still meets the quota's reason.
    explicit = decide(_req(locus="managed", explicit=True), policy, month=PeriodTotals(), day=PeriodTotals(),
                      external=quota)  # fmt: skip
    assert explicit.reason is AdmissionReason.COST_UNMEASURABLE


def test_settings_bound_the_call_ceilings_to_what_the_policy_accepts(tmp_path: Path) -> None:
    from lemoncrow.core.settings import load_raw, write_setting

    for key in ("intelligence.background.monthly_model_calls", "intelligence.background.daily_model_calls"):
        for bad in (MAX_POLICY_CALLS + 1, -2):
            with pytest.raises(ValueError, match="between -1 and"):
                write_setting(tmp_path, key, str(bad))
        assert write_setting(tmp_path, key, str(MAX_POLICY_CALLS)) == MAX_POLICY_CALLS
        assert write_setting(tmp_path, key, "-1") == -1
    assert local_background_policy(load_raw(tmp_path)).monthly_model_calls is None


def test_a_malformed_setting_fails_closed_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core import settings
    from lemoncrow.pro.capabilities.review import fix_agent

    settings._write_json(settings.settings_path(tmp_path), {"intelligence.background.monthly_model_calls": "lots"})
    monkeypatch.setattr(fix_agent, "settings_root", lambda: tmp_path)
    store = LocalBackgroundBudgetStore(tmp_path / "ledger", clock=_Clock())
    assert store.policy().policy_invalid
    refused = store.reserve(_req("bg"))
    assert refused.reason is AdmissionReason.POLICY_INVALID and refused.state == "refused"
    assert store.reserve(_req("manual", priority=REQ)).admitted  # explicit/required work is not the envelope's
    status = store.status()
    assert (status["state"], status["reason"], status["policy"]["valid"]) == ("disabled", "policy_invalid", False)

    def broken() -> BackgroundBudgetPolicy:
        raise BackgroundBudgetPolicyError("intelligence.background.daily_model_calls must be a whole number")

    other = LocalBackgroundBudgetStore(tmp_path / "other", policy=broken, clock=_Clock())
    assert other.reserve(_req("bg")).reason is AdmissionReason.POLICY_INVALID
    assert other.status()["reason"] == "policy_invalid"


def test_the_local_store_refuses_managed_work_and_its_status_says_so(tmp_path: Path) -> None:
    store = _store(tmp_path, _policy(monthly_managed_micros=1_000_000))
    for explicit in (False, True):
        refused = store.reserve(_req(f"m{explicit}", locus="managed", cost_per_call_micros=10, explicit=explicit))
        assert refused.reason is AdmissionReason.MANAGED_UNAVAILABLE and not refused.admitted
    with pytest.raises(UnknownReservation):
        store.settle("mFalse", calls=1)
    status = store.status()
    assert status["managed_spend"]["applies"] is False and status["managed_spend"]["enforced"] is False
    assert "managed_unavailable" in status["managed_spend"]["note"]
    assert status["policy"]["valid"] is True


def test_status_shows_a_dead_hold_as_expired_without_any_later_admission(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, _policy(monthly_model_calls=10), clock)
    store.reserve(_req("dead", max_calls=6, hold_s=60))
    assert store.status()["background"]["model_calls_held"] == 6
    clock.now += 61
    background = store.status()["background"]
    assert (background["model_calls_held"], background["model_calls_used"], background["model_calls_remaining"]) == (
        0,
        6,
        4,
    )
    with sqlite3.connect(tmp_path / "background_budget.db") as conn:
        state = conn.execute("SELECT state, cost_state FROM background_budget_reservations").fetchone()
    assert tuple(state) == ("settled", "expired")


def test_a_renewed_lease_extends_a_live_hold_but_never_shortens_or_revives_one() -> None:
    clock = _Clock()
    ledger = _Ledger(_policy(monthly_model_calls=10), clock)
    ledger.reserve(_req("live", max_calls=4, hold_s=60))
    clock.now += 50
    assert ledger.extend("live", clock.now + 60)
    assert ledger.extend("live", clock.now + 1)  # shorter: kept as it was
    clock.now += 50  # past the first hold, inside the renewed one
    assert ledger.status()["background"]["model_calls_held"] == 4
    clock.now += 11
    assert ledger.status()["background"]["model_calls_used"] == 4  # expired with its renewed lease
    assert not ledger.extend("live", clock.now + 600)  # never revived
    assert not ledger.extend("missing", clock.now + 600)
    # A hold already due but not yet expired by any pass is not revived either.
    ledger.reserve(_req("due", max_calls=2, hold_s=60))
    clock.now += 61
    assert not ledger.extend("due", clock.now + 600)
    assert ledger.status()["background"]["model_calls_used"] == 6


def test_a_status_read_expires_a_bounded_backlog_and_the_next_read_finishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import background_budget

    monkeypatch.setattr(background_budget, "_EXPIRY_BATCH", 2)
    monkeypatch.setattr(background_budget, "_STATUS_EXPIRY_LIMIT", 3)
    clock = _Clock()
    store = _store(tmp_path, _policy(monthly_model_calls=None), clock)
    for index in range(5):
        store.reserve(_req(f"dead-{index}", hold_s=60))
    store.reserve(_req("ask", priority=REQ, hold_s=60))
    clock.now += 61
    first = store.status()
    assert first["background"]["model_calls_used"] == 3 and first["background"]["model_calls_held"] == 2
    assert first["interactive"]["model_calls_used"] == 1  # each lane has its own bound
    second = store.status()["background"]
    assert (second["model_calls_used"], second["model_calls_held"]) == (5, 0)
