"""Local (SQLite) ledger of the shared background budget (BI1).

One file, ``<root>/background_budget.db``, per LemonCrow root; the scope is
the whole root (``org_id = 'local'``). Every write runs under ``BEGIN
IMMEDIATE``, which takes SQLite's database write lock before the first read, so
two processes reserving at once are serialized and cannot both read the same
remaining envelope. The algorithm and SQL are
:mod:`~lemoncrow.pro.capabilities.review.background_budget`'s, shared with
the hosted ledger.

Policy comes from the user settings file: ``intelligence.background.enabled``
(BI0's switch, under an operator layer's AI-on) and the
``intelligence.background.*`` ceilings below; a ceiling setting makes the
envelope ``configured``, so it binds automatic Review too. A malformed
setting fails closed: background work is refused with ``policy_invalid`` and
neither :meth:`LocalBackgroundBudgetStore.reserve` nor ``status`` raises. There
is no managed execution locally, so a ``managed`` attempt is refused with
``managed_unavailable`` (the dollar ceiling never applies here) and the
projection says so.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from lemoncrow.pro.capabilities.review.background_budget import (
    BACKGROUND_BUDGET_RETENTION_S,
    AdmissionReason,
    AiUse,
    BackgroundAdmission,
    BackgroundBudgetPolicy,
    BackgroundBudgetPolicyError,
    BackgroundWorkRequest,
    BudgetSettlement,
    CeilingInput,
    SqlLedgerTransaction,
    ai_use_in,
    meter_in,
    read_status,
    reserve_in,
    settle_in,
)

__all__ = [
    "BACKGROUND_BUDGET_DB",
    "LOCAL_SCOPE",
    "SQLITE_SCHEMA",
    "LocalBackgroundBudgetStore",
    "local_background_policy",
]

_LOG = logging.getLogger(__name__)

BACKGROUND_BUDGET_DB: Final[str] = "background_budget.db"
LOCAL_SCOPE: Final[str] = "local"

#: The ledger tables. The hosted quota authority creates the same columns.
SQLITE_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS background_budget_periods (
    org_id TEXT NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN ('background', 'interactive')),
    period_kind TEXT NOT NULL CHECK (period_kind IN ('month', 'day')),
    period_start INTEGER NOT NULL,
    calls_held INTEGER NOT NULL DEFAULT 0 CHECK (calls_held >= 0),
    calls_used INTEGER NOT NULL DEFAULT 0 CHECK (calls_used >= 0),
    micros_held INTEGER NOT NULL DEFAULT 0 CHECK (micros_held >= 0),
    micros_used INTEGER NOT NULL DEFAULT 0 CHECK (micros_used >= 0),
    reported_micros INTEGER NOT NULL DEFAULT 0 CHECK (reported_micros >= 0),
    unmeasured_calls INTEGER NOT NULL DEFAULT 0 CHECK (unmeasured_calls >= 0),
    updated_at REAL NOT NULL,
    PRIMARY KEY (org_id, lane, period_kind, period_start)
);
CREATE TABLE IF NOT EXISTS background_budget_reservations (
    org_id TEXT NOT NULL,
    key_digest TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN ('background', 'interactive')),
    priority TEXT NOT NULL,
    capability TEXT NOT NULL,
    locus TEXT NOT NULL CHECK (locus IN ('local', 'worker', 'managed')),
    month_start INTEGER NOT NULL,
    day_start INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held', 'settled')),
    granted_calls INTEGER NOT NULL CHECK (granted_calls > 0),
    held_micros INTEGER NOT NULL CHECK (held_micros >= 0),
    cost_per_call_micros INTEGER,
    calls_used INTEGER NOT NULL DEFAULT 0 CHECK (calls_used >= 0),
    micros_used INTEGER NOT NULL DEFAULT 0 CHECK (micros_used >= 0),
    reported_micros INTEGER NOT NULL DEFAULT 0 CHECK (reported_micros >= 0),
    unmeasured_calls INTEGER NOT NULL DEFAULT 0 CHECK (unmeasured_calls >= 0),
    cost_state TEXT NOT NULL DEFAULT '',
    settle_fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    settled_at REAL,
    PRIMARY KEY (org_id, key_digest)
);
CREATE INDEX IF NOT EXISTS background_budget_reservations_due
    ON background_budget_reservations (org_id, lane, state, expires_at);
CREATE INDEX IF NOT EXISTS background_budget_reservations_month
    ON background_budget_reservations (org_id, month_start);
"""


def local_background_policy(raw: Mapping[str, Any]) -> BackgroundBudgetPolicy:
    """The local envelope from the raw user settings (dotted keys). Malformed values are an error."""

    fields: dict[str, Any] = {}
    for setting, name in (
        ("intelligence.background.enabled", "enabled"),
        ("intelligence.background.monthly_model_calls", "monthly_model_calls"),
        ("intelligence.background.daily_model_calls", "daily_model_calls"),
    ):
        if setting in raw:
            value = raw[setting]
            # A negative count in the settings file means "no ceiling" (settings cannot hold null).
            fields[name] = None if isinstance(value, int) and not isinstance(value, bool) and value < 0 else value
    try:
        return BackgroundBudgetPolicy.from_mapping(fields)
    except BackgroundBudgetPolicyError as exc:
        raise BackgroundBudgetPolicyError(f"intelligence.background: {exc}") from exc


def _settings_policy() -> BackgroundBudgetPolicy:
    """The local envelope under BI0's effective policy (BI2): one switch, read the way model calls read it.

    an operator layer's AI off, ``intelligence.background.enabled`` off or an
    invalid intelligence policy turns the envelope off (or closed), so Usage and
    admission agree with the kill switch every model call consults.
    """

    from lemoncrow.core.settings import load_raw
    from lemoncrow.pro.capabilities.review.background_scheduler import envelope_under_policy
    from lemoncrow.pro.capabilities.review.fix_agent import settings_root
    from lemoncrow.pro.capabilities.review.intelligence_policy import process_intelligence_policy

    root = settings_root()
    return envelope_under_policy(local_background_policy(load_raw(root)), process_intelligence_policy(root))


class LocalBackgroundBudgetStore:
    """The background budget ledger of one local LemonCrow root."""

    def __init__(
        self,
        root: Path | str,
        *,
        policy: Callable[[], BackgroundBudgetPolicy] | BackgroundBudgetPolicy | None = None,
        clock: Callable[[], float] = time.time,
        retention_s: float = BACKGROUND_BUDGET_RETENTION_S,
    ) -> None:
        self.root = Path(root)
        self.db_path = self.root / BACKGROUND_BUDGET_DB
        self._policy = policy
        self._clock = clock
        self._retention_s = float(retention_s)
        self._ready = False

    def policy(self) -> BackgroundBudgetPolicy:
        """The current envelope (re-read every call, so a settings change applies at once).

        A malformed or unreadable setting yields :meth:`BackgroundBudgetPolicy.invalid`
        (fail closed: background work is refused with ``policy_invalid``), never an exception.
        """

        if isinstance(self._policy, BackgroundBudgetPolicy):
            return self._policy
        try:
            if self._policy is not None:
                return self._policy()
            return _settings_policy()
        except (BackgroundBudgetPolicyError, ValueError, TypeError, OSError) as exc:
            _LOG.warning("background budget policy is invalid; background work is refused: %s", exc)
            return BackgroundBudgetPolicy.invalid()

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        if not self._ready:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SQLITE_SCHEMA)
            self._ready = True
        return conn

    @contextlib.contextmanager
    def _write(self) -> Iterator[SqlLedgerTransaction]:
        conn = self._connect()
        try:
            # IMMEDIATE: the write lock is taken before the envelope is read, so a
            # concurrent process cannot decide on the same remaining calls.
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield SqlLedgerTransaction(conn, LOCAL_SCOPE, paramstyle="named")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def reserve(self, request: BackgroundWorkRequest, *, external: Sequence[CeilingInput] = ()) -> BackgroundAdmission:
        """Admit and hold, or refuse, one attempt (idempotent by ``request.key``).

        A ``managed`` attempt is refused with ``managed_unavailable``: nothing runs
        LemonCrow-managed execution locally, so no dollar ceiling could bind it.
        """

        if request.locus == "managed":
            return BackgroundAdmission(reason=AdmissionReason.MANAGED_UNAVAILABLE, lane=request.lane)
        policy = self.policy()
        with self._write() as txn:
            if self._ai_budget_refuses(txn):
                # The personal AI budget (P8-4) is one more ceiling: the most restrictive limit wins.
                return BackgroundAdmission(reason=AdmissionReason.AI_BUDGET_EXHAUSTED, lane=request.lane)
            return reserve_in(txn, request, policy, now=self._clock(), external=external, retention_s=self._retention_s)

    def _ai_budget_refuses(self, txn: SqlLedgerTransaction) -> bool:
        from lemoncrow.core.settings import load_raw
        from lemoncrow.pro.capabilities.review.ai_budget import budget_setting

        setting = budget_setting(load_raw(self.root))
        if setting.limit_micros is None:
            return False
        return setting.limit_micros == 0 or ai_use_in(txn, now=self._clock()).micros >= setting.limit_micros

    def meter(self, *, calls: int = 1, cost_micros: int | None = None) -> None:
        """Record model calls no admission metered (the AI budget's meter); ``cost_micros`` ``None`` is not reported."""

        with self._write() as txn:
            meter_in(txn, calls=calls, cost_micros=cost_micros, now=self._clock(), retention_s=self._retention_s)

    def ai_use(self) -> AiUse:
        """This month's model calls and known cost across both lanes."""

        with self._write() as txn:
            return ai_use_in(txn, now=self._clock())

    def settle(self, key: str, *, calls: int, cost_micros: int | None = None) -> BudgetSettlement:
        """Charge what the attempt used; ``cost_micros`` only when the provider reported it."""

        with self._write() as txn:
            return settle_in(txn, key, calls=calls, cost_micros=cost_micros, now=self._clock())

    def release(self, key: str) -> BudgetSettlement:
        """Give back an attempt's hold unused (it made no model call)."""

        return self.settle(key, calls=0, cost_micros=0)

    def status(self) -> dict[str, Any]:
        """The Usage / BI2 projection of the local envelope (due holds are expired first)."""

        policy = self.policy()
        with self._write() as txn:
            status = read_status(txn, policy, now=self._clock(), managed_available=False)
            if status["state"] in {"running", "limited"} and self._ai_budget_refuses(txn):
                # The personal AI budget (P8-4) is $0 or used up: nothing is admitted, whatever the envelope has left.
                status = {**status, "state": "paused", "reason": AdmissionReason.AI_BUDGET_EXHAUSTED.value}
            return {"scope": "local", **status}
