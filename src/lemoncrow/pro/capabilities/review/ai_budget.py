"""The personal AI budget: one monthly dollar limit on the model calls this machine makes (P8-4).

Owner decision 2026-11-08 (SPEC §7.1 OWNER-1 to OWNER-3). There is no AI on/off
switch. ``intelligence.monthly_budget_usd`` is the control:

* no value (or ``-1``, settings cannot hold null): no limit, today's behaviour;
* ``0``: no new AI runs; nothing already made is hidden;
* ``N``: no new AI run starts once the month's known cost reaches ``N``.

The month is a UTC calendar month. Every model call is metered in the existing
background-budget ledger (:mod:`background_budget`): explicit work (Ask, a manual
Review) and background work alike, in one total. A call costs, in order, the
provider's reported cost, the list price of its reported tokens
(``intelligence_runtime.exact_table_cost_usd``), ``$0`` for the local model on
this machine (driver ``ollama``), else it is counted as "cost not reported" and
never as ``$0``.

**Two questions, kept apart.** *Which route would run* is a label (data flow,
plan, route display) and never refuses: :func:`resolve_driver_model` only checks
the policy is readable and no operator layer turned AI off. *May this call run*
is :func:`model_call`, entered by every call that sends anything to a model; it
refuses before a byte leaves, with a typed :class:`AiBudgetRefused`.

The check is made before a call and its cost is known after it, so the budget can
be passed by the calls that are running when the check is made (one, when calls
come one at a time; as many as run in parallel otherwise). Nothing reserves
money for a call in flight.

State written here: the interactive lane's month and day rows of the ledger, by
:func:`background_budget.meter_in` (its writer), deleted at the month rollover
past the ledger's retention horizon (its closer). Nothing else is stored.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from lemoncrow.infra.internal_llm.usage import UsageNote, collect_usage
from lemoncrow.pro.capabilities.review.background_budget import (
    AiUse,
    BackgroundBudgetPolicyError,
    month_start,
    next_month_start,
    usd_to_micros,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import IntelligenceDisabled

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.intelligence_policy import EffectiveIntelligencePolicy
    from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage

__all__ = [
    "BUDGET_SETTING",
    "LEGACY_SWITCH",
    "NO_LIMIT",
    "AdmissionMeter",
    "AiBudgetRefused",
    "AiBudgetState",
    "BudgetSetting",
    "ModelCallMeter",
    "OrganizationGate",
    "admission_metering",
    "ai_budget_state",
    "budget_setting",
    "call_cost_micros",
    "install_organization_gate",
    "migrate_legacy_switch",
    "model_call",
    "reset_label",
]

_LOG = logging.getLogger(__name__)

BUDGET_SETTING: Final[str] = "intelligence.monthly_budget_usd"
#: The retired on/off switch. Never read as a setting; an existing ``false`` becomes a $0 budget.
LEGACY_SWITCH: Final[str] = "intelligence.enabled"
#: The settings file cannot hold null, so this value means "no limit" (as the background ceilings' ``-1``).
NO_LIMIT: Final[float] = -1.0

REASON_ZERO: Final[str] = "budget_zero"
REASON_USED_UP: Final[str] = "budget_used_up"
REASON_INVALID: Final[str] = "budget_invalid"
REASON_UNREADABLE: Final[str] = "budget_unreadable"

_MONTHS: Final[tuple[str, ...]] = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ON_WORDS: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


# --------------------------------------------------------------------------- #
# the setting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BudgetSetting:
    """The budget as the settings file states it: ``limit_micros`` ``None`` is no limit."""

    limit_micros: int | None
    #: Non-empty when the stored value cannot be read: the budget then fails closed ($0).
    invalid: str = ""


def _switch_is_off(value: object) -> bool:
    """Whether a stored retired switch said off. Anything but a clear "on" fails closed, as it did."""

    if isinstance(value, bool):
        return not value
    return str(value).strip().lower() not in _ON_WORDS


def budget_setting(raw: Mapping[str, Any]) -> BudgetSetting:
    """The effective budget of a raw settings document, with a retired ``intelligence.enabled: false`` as $0."""

    if LEGACY_SWITCH in raw and _switch_is_off(raw[LEGACY_SWITCH]):
        return BudgetSetting(0)
    value = raw.get(BUDGET_SETTING)
    if value is None or (isinstance(value, str) and not value.strip()):
        return BudgetSetting(None)
    if not isinstance(value, bool) and isinstance(value, (int, float)) and value == NO_LIMIT:
        return BudgetSetting(None)
    try:
        return BudgetSetting(usd_to_micros(value, key=BUDGET_SETTING))
    except BackgroundBudgetPolicyError:
        return BudgetSetting(
            0, invalid=f"{BUDGET_SETTING} must be a number of US dollars, 0 or more; remove it for no limit"
        )


def migrate_legacy_switch(root: Path | str) -> bool:
    """Replace a stored ``intelligence.enabled`` with the budget it means. Idempotent; ``True`` when it changed the file.

    ``false`` (or anything not clearly on) becomes a $0 budget, whatever budget
    was set, because it was the stricter choice; ``true`` is simply dropped. The
    key is gone afterwards, so a second run finds nothing to do. A file that
    cannot be written is not an error: :func:`budget_setting` reads the retired
    key the same way, so the answer is $0 either way.
    """

    from lemoncrow.core.settings import forget_retired_setting, load_raw, write_setting

    try:
        raw = load_raw(root)
        if LEGACY_SWITCH not in raw:
            return False
        if _switch_is_off(raw[LEGACY_SWITCH]):
            write_setting(root, BUDGET_SETTING, 0.0)
        forget_retired_setting(root, LEGACY_SWITCH)
    except (OSError, ValueError):
        _LOG.warning("could not migrate the retired intelligence.enabled setting", exc_info=True)
        return False
    return True


def _setting(root: Path) -> BudgetSetting:
    from lemoncrow.core.settings import load_raw

    raw = load_raw(root)
    if LEGACY_SWITCH in raw:
        migrate_legacy_switch(root)
    return budget_setting(raw)


# --------------------------------------------------------------------------- #
# the state
# --------------------------------------------------------------------------- #


def reset_label(resets_on: str) -> str:
    """``2026-12-01`` as ``Dec 1``."""

    year, month, day = (int(part) for part in resets_on.split("-"))
    del year
    return f"{_MONTHS[month - 1]} {day}"


@dataclass(frozen=True, slots=True)
class AiBudgetState:
    """The budget, this month's use and whether a model call may start now."""

    limit_micros: int | None
    use: AiUse
    period_start: str
    resets_on: str
    may_run: bool
    reason_code: str = ""
    reason: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "monthly_budget_usd": None if self.limit_micros is None else self.limit_micros / 1_000_000,
            "used_usd": self.use.micros / 1_000_000,
            "unreported_calls": self.use.unreported_calls,
            "complete": self.use.complete,
            "calls": self.use.calls,
            "period_start": self.period_start,
            "resets_on": self.resets_on,
            "may_run": self.may_run,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }


class AiBudgetRefused(IntelligenceDisabled):
    """A model call was refused before anything was sent: the budget is $0, used up, or unreadable."""

    def __init__(self, state: AiBudgetState) -> None:
        super().__init__(state.reason)
        self.code = state.reason_code
        self.resets_on = state.resets_on


def _iso(ts: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _store(root: Path) -> Any:
    from lemoncrow.pro.capabilities.review.background_budget_store import LocalBackgroundBudgetStore

    return LocalBackgroundBudgetStore(root)


def ai_budget_state(
    root: Path | str | None = None, *, now: float | None = None, with_use: bool = True
) -> AiBudgetState:
    """The budget state of this machine's LemonCrow root.

    ``with_use=False`` skips the ledger read when the answer does not need it (no
    limit, or $0): the model-call gate then costs one small file read. An
    unreadable ledger under a positive limit fails closed.
    """

    from lemoncrow.pro.capabilities.review.fix_agent import settings_root

    base = Path(root) if root is not None else settings_root()
    moment = time.time() if now is None else now
    start = month_start(moment)
    period, resets = _iso(start), _iso(next_month_start(start))
    setting = _setting(base)
    limit = setting.limit_micros

    def state(use: AiUse, may_run: bool, code: str = "", reason: str = "") -> AiBudgetState:
        return AiBudgetState(limit, use, period, resets, may_run, code, reason)

    if setting.invalid:
        return state(AiUse(), False, REASON_INVALID, setting.invalid)
    if limit == 0:
        return state(_use(base, moment, with_use), False, REASON_ZERO, "AI budget is $0")
    if limit is None and not with_use:
        return state(AiUse(), True)
    try:
        use = _store(base).ai_use()
    except Exception:  # lc-debt: any ledger fault; narrow to sqlite3.Error/OSError once the hosted ledger is split out
        _LOG.warning("AI budget use could not be read", exc_info=True)
        if limit is None:
            return state(AiUse(), True)
        return state(AiUse(), False, REASON_UNREADABLE, "AI budget use could not be read, so no AI runs until it can")
    if limit is not None and use.micros >= limit:
        return state(use, False, REASON_USED_UP, f"AI budget used up until {reset_label(resets)}")
    return state(use, True)


def _use(base: Path, moment: float, with_use: bool) -> AiUse:
    if not with_use:
        return AiUse()
    try:
        return _store(base).ai_use()
    except Exception:  # the use line is informational when the budget already refuses
        _LOG.warning("AI budget use could not be read", exc_info=True)
        return AiUse()


# --------------------------------------------------------------------------- #
# cost of one call
# --------------------------------------------------------------------------- #


def call_cost_micros(driver: str, model: str, usage: ModelCallUsage | None) -> int | None:
    """One call's cost in micros of a dollar, or ``None`` when it is not known (never ``0``).

    The provider's reported cost; else the list price of the reported tokens; the
    local model on this machine (``ollama`` on a loopback host) is free; else unknown.
    """

    if driver == "ollama":
        return 0 if ollama_is_local(model) else _priced(model, usage)
    return _priced(model, usage)


_LOOPBACK: Final[frozenset[str]] = frozenset({"", "localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


def ollama_is_local(model: str = "") -> bool:
    """Whether Ollama answers on this machine: its host is loopback and the model is not a hosted (``:cloud``) one."""

    import os
    from urllib.parse import urlparse

    if model.endswith((":cloud", "-cloud")):
        return False
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if not host:
        return True
    name = (urlparse(host if "://" in host else f"//{host}").hostname or "").lower()
    return name in _LOOPBACK or name.startswith("127.")


def _priced(model: str, usage: ModelCallUsage | None) -> int | None:
    if usage is None:
        return None
    if usage.provider_cost is not None and str(usage.currency or "USD").upper() == "USD":
        try:
            return usd_to_micros(usage.provider_cost, key="provider_cost")
        except BackgroundBudgetPolicyError:
            return None
    from lemoncrow.pro.capabilities.review.intelligence_runtime import exact_table_cost_usd

    if not (usage.input_tokens or usage.output_tokens):
        return None  # a call that sent anything had tokens; none reported means the provider reported no usage
    estimate = exact_table_cost_usd(model, usage) if model else None
    if estimate is None:
        return None
    try:
        return usd_to_micros(estimate, key="estimated_cost")
    except BackgroundBudgetPolicyError:
        return None


# --------------------------------------------------------------------------- #
# the gate and the meter
# --------------------------------------------------------------------------- #


class AdmissionMeter:
    """Calls made inside one ledger admission, added up for its settlement instead of metered twice."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.micros = 0
        self.unreported = False

    def add(self, calls: int, cost_micros: int | None) -> None:
        with self._lock:
            self.calls += calls
            if cost_micros is None:
                self.unreported = True
            else:
                self.micros += cost_micros

    def cost_micros(self) -> int | None:
        """The whole cost of what ran, or ``None`` when any call's cost is unknown or nothing ran."""

        with self._lock:
            return None if self.unreported or self.calls == 0 else self.micros


_admission: contextvars.ContextVar[AdmissionMeter | None] = contextvars.ContextVar(
    "lemoncrow_ai_admission", default=None
)
_in_call: contextvars.ContextVar[bool] = contextvars.ContextVar("lemoncrow_ai_in_call", default=False)


@contextmanager
def admission_metering() -> Iterator[AdmissionMeter]:
    """Calls made in this context are added to the returned meter, not written to the ledger.

    Used around an execution a ledger admission already holds, whose settlement
    carries its calls (:func:`background_scheduler.run_admitted`).
    """

    meter = AdmissionMeter()
    token = _admission.set(meter)
    try:
        yield meter
    finally:
        _admission.reset(token)


class ModelCallMeter:
    """What a call reports about itself: its route, and the use its transport reported."""

    __slots__ = ("calls", "cost_micros", "driver", "model", "usage")

    def __init__(self) -> None:
        self.calls = 1
        self.cost_micros: int | None = None
        self.driver = ""
        self.model = ""
        self.usage: ModelCallUsage | None = None

    def route(self, driver: str, model: str) -> None:
        """Say where the call goes, before it is made (so a call that fails is still priced for its route)."""

        self.driver, self.model = driver, model

    def report(self, driver: str, model: str, usage: ModelCallUsage | None = None) -> None:
        """Say what the call used. Without usage the clients' own notes are read, and without those it costs
        nothing only on the local model, else it is not reported."""

        self.route(driver, model)
        self.usage = usage

    def finish(self, notes: Sequence[UsageNote]) -> None:
        """Settle calls and cost: the usage the caller reported, else what the clients noted, else unknown."""

        from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage

        usage = self.usage
        calls = 1
        model = self.model
        if usage is not None and (usage.provider_cost is not None or usage.input_tokens or usage.output_tokens):
            calls = max(1, usage.calls)
            model = model or next((note.model for note in reversed(notes) if note.model), "")
        elif notes:
            calls = len(notes)
            model = next((note.model for note in reversed(notes) if note.model), model)
            costs = [note.provider_cost for note in notes]
            usage = (
                ModelCallUsage(
                    calls=calls,
                    input_tokens=sum(note.input_tokens for note in notes),
                    output_tokens=sum(note.output_tokens for note in notes),
                    cache_read_input_tokens=sum(note.cache_read_tokens for note in notes),
                    cache_write_input_tokens=sum(note.cache_write_tokens for note in notes),
                    provider_cost=(
                        None if any(cost is None for cost in costs) else sum(c for c in costs if c is not None)
                    ),
                    currency="USD",
                )
                if all(note.reported for note in notes)
                else None  # an answer that told us nothing makes the whole figure unknown, never a smaller one
            )
        else:
            usage = None
        self.calls = calls
        self.cost_micros = call_cost_micros(self.driver, model, usage)


def _enforced() -> bool:
    """The personal budget governs a local process; a hosted process reads no settings file (P7 owns its limits)."""

    from lemoncrow.pro.capabilities.review.intelligence_policy import settings_layer_enabled

    return settings_layer_enabled()


class OrganizationGate:
    """A hosted organization's cap on model calls (SPEC 4.19 "Cost cap", 7.1 P9-5), installed by the hosted server.

    A hosted process reads no settings file, so the personal budget does not govern it; the organization's cap does,
    for the organization the current request belongs to. ``check`` returns the state that refuses a call (or ``None``
    when it may run, or when no organization is known: a call made outside a request is not capped here) and
    ``record`` adds a finished call to that organization's month. Both read the request's organization themselves.
    """

    def check(self) -> AiBudgetState | None:  # pragma: no cover - interface
        raise NotImplementedError

    def record(self, calls: int, cost_micros: int | None) -> None:  # pragma: no cover - interface
        raise NotImplementedError


_organization_gate: OrganizationGate | None = None


def install_organization_gate(gate: OrganizationGate | None) -> None:
    """Install (or, with ``None``, remove) the hosted organization's gate. One per process."""

    global _organization_gate
    _organization_gate = gate


def _record(meter: ModelCallMeter) -> None:
    admission = _admission.get()
    if admission is not None:
        admission.add(meter.calls, meter.cost_micros)
        return
    from lemoncrow.pro.capabilities.review.fix_agent import settings_root

    try:
        _store(settings_root()).meter(calls=meter.calls, cost_micros=meter.cost_micros)
    except Exception:  # lc-debt: a call that ran is not undone when the ledger cannot be written; it goes unmetered
        _LOG.warning("model call could not be metered", exc_info=True)


def _record_any(meter: ModelCallMeter) -> None:
    if _enforced():
        _record(meter)
        return
    gate = _organization_gate
    if gate is None:
        return
    admission = _admission.get()
    if admission is not None:
        admission.add(meter.calls, meter.cost_micros)
        return
    try:
        gate.record(meter.calls, meter.cost_micros)
    except Exception:  # lc-debt: a call that ran is not undone when the ledger cannot be written; it goes unmetered
        _LOG.warning("model call could not be metered for the organization", exc_info=True)


def require_call_allowed(policy: EffectiveIntelligencePolicy | None = None) -> EffectiveIntelligencePolicy:
    """May a model call start? The policy must be readable and on, and the AI budget must have room.

    Raises :class:`IntelligenceDisabled` (policy) or :class:`AiBudgetRefused` (budget), before
    anything is sent. Route resolution for a label never comes here.
    """

    from lemoncrow.pro.capabilities.review.intelligence_runtime import require_intelligence_enabled

    checked = require_intelligence_enabled(policy)
    if _enforced():
        state = ai_budget_state(with_use=False)
        if not state.may_run:
            raise AiBudgetRefused(state)
    elif _organization_gate is not None:
        refused = _organization_gate.check()
        if refused is not None:
            raise AiBudgetRefused(refused)
    return checked


@contextmanager
def model_call(policy: EffectiveIntelligencePolicy | None = None) -> Iterator[ModelCallMeter]:
    """Wrap one call that sends something to a model: gate it first, meter it after.

    The outermost wrapper gates and meters; a call made inside another (the
    structured executor streaming through the facade) is one call and is
    metered once. A call that raises after a provider answered it (billed, then
    rejected as malformed) is metered from the answer, and a stream that was opened
    and then cut is metered as cost not reported; a call that never reached the
    provider is not, because nothing was billed.
    """

    meter = ModelCallMeter()
    if _in_call.get():
        yield meter
        return
    require_call_allowed(policy)
    token = _in_call.set(True)
    try:
        with collect_usage() as seen:
            try:
                yield meter
            except BaseException:
                if seen.notes and (_enforced() or _organization_gate is not None):
                    meter.finish(seen.notes)
                    _record_any(meter)
                raise
    finally:
        _in_call.reset(token)
    meter.finish(seen.notes)
    if _enforced() or _organization_gate is not None:
        _record_any(meter)
