"""The per-pull-request cost cap, held during a run and not only before it.

``max-usd-per-pr`` is checked twice. Before a run, :func:`runner.budget_reached` reads what the summary's state says the
pull request has cost and skips the review when that is already at the cap. During a run, a :class:`SpendGuard` sits
in front of every model call: before each one it adds what this pull request has cost so far, what this run has spent,
what the calls already in flight may cost and a conservative estimate of the next call, and refuses the call when the
sum would pass the cap. A refused call raises :class:`SpendCapReached`, which the review counts as a call the budget
skipped (its message says ``budget exhausted``), so the run ends as a partial review that names what it did not reach.
Nothing is cancelled: a call already in flight finishes and is paid for.

How a route's cost is known decides how it is held:

- a route that reports tokens for a model the price table knows exactly (Anthropic and OpenAI through the Action's own
  routes) is held in dollars. The estimate of the next call is its prompt at three characters a token plus the full
  output limit (``LEMONCROW_REVIEW_MAX_OUTPUT_TOKENS``, 8192 by default), at the table's price, or the most any call of
  this run cost, whichever is more;
- a route that reports its own cost but whose model the table does not know (the ``claude`` CLI's own estimate) has no
  estimate before its first call, so its first call runs alone; after it, the most any call cost is the estimate;
- a route that reports no cost (``ollama``, ``codex``, ``opencode``, an OpenAI-compatible endpoint or a LiteLLM model
  the table does not know) cannot be held in dollars. It is never left uncapped: it is held to
  :data:`UNPRICED_CALLS_PER_PR` model calls per pull request, and the summary and the status say
  ``cost not reported by this route; capped at N model calls``.

The overshoot is bounded: a call is refused before it starts, so the cap can be passed only by what the calls already in
flight cost beyond their estimate (at most ``ai_coverage.MAX_PARALLEL_CALLS`` at once, one for a route with no
estimate yet), and by a JSON retry LiteLLM makes inside one call.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from lemoncrow.pro.capabilities.review.pr_bot.state import Usage

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.intelligence_runtime import StructuredExecution
    from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState

#: The default ``max-usd-per-pr``: what one pull request's reviews and chat answers may cost in total, unless the
#: workflow raises it or sets ``none``. Nothing is ever charged past it automatically.
DEFAULT_MAX_USD_PER_PR: Final[float] = 3.00
#: The cap on model calls for one pull request on a route that reports no cost: about what the default dollar cap buys on
#: a mid-priced model.
UNPRICED_CALLS_PER_PR: Final[int] = 60
#: Where the Action hands its ``max-usd-per-pr`` input to the review: a number, ``none``, or empty for the default.
MAX_USD_ENV: Final[str] = "LEMONCROW_REVIEW_MAX_USD"
#: Words that turn the cap off.
NO_CAP: Final[frozenset[str]] = frozenset({"none", "off", "unlimited"})
#: Characters per token for the estimate of a prompt: fewer than the usual four, so the estimate errs high.
CHARS_PER_TOKEN: Final[int] = 3
#: The words the unpriced cap is said in, in the summary and the status.
UNPRICED_WORDS: Final[str] = "cost not reported by this route; capped at {n} model calls"
#: The error a refused call ends with: ``budget exhausted`` is what the review counts as a call the budget skipped.
REFUSED: Final[str] = "pull request cost budget exhausted"


def resolve_max_usd(value: float | str | None, environ: Mapping[str, str] | None = None) -> float | None:
    """The cap in dollars, or ``None`` for no cap: *value*, else :data:`MAX_USD_ENV`, else the default.

    An empty value is the default (:data:`DEFAULT_MAX_USD_PER_PR`); ``none``, ``off`` or ``unlimited`` is no cap. Anything
    else must be a number above 0, else :class:`ValueError` says what to write.
    """

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raw = str(value)
    else:
        raw = str(value if value is not None else (os.environ if environ is None else environ).get(MAX_USD_ENV) or "")
    raw = raw.strip().lower().lstrip("$")
    if not raw:
        return DEFAULT_MAX_USD_PER_PR
    if raw in NO_CAP:
        return None
    try:
        cap = float(raw)
    except ValueError:
        cap = -1.0
    if not cap > 0 or cap == float("inf"):
        raise ValueError(
            f"max-usd-per-pr must be a number of dollars above 0, or `none` for no cap (got {raw!r}); "
            f"leave it empty for the default of ${DEFAULT_MAX_USD_PER_PR:.2f}"
        )
    return cap


class SpendCapReached(RuntimeError):
    """A model call refused before it started: it would have taken the pull request past its cost cap."""


def _output_limit(environ: Mapping[str, str]) -> int:
    from lemoncrow.infra.internal_llm.litellm_client import REVIEW_MAX_OUTPUT_ENV, REVIEW_MAX_OUTPUT_TOKENS

    raw = str(environ.get(REVIEW_MAX_OUTPUT_ENV) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else REVIEW_MAX_OUTPUT_TOKENS


def _table_estimate(model: str, prompt_chars: int, output_tokens: int) -> float | None:
    """USD for a call of *prompt_chars* and the full output limit at the price table's exact rate; ``None``: not listed."""

    from lemoncrow.core.capabilities.pricing import exact_model_pricing

    pricing = exact_model_pricing(model) if model else None
    if pricing is None:
        return None
    return pricing.call_cost_usd(prompt_tokens=prompt_chars // CHARS_PER_TOKEN + 1, output_tokens=output_tokens)


def _route_model(kwargs: Mapping[str, Any]) -> tuple[str, str]:
    """``(driver, model)`` a call will use; ``("", "")`` when the route cannot be resolved here."""

    from lemoncrow.pro.capabilities.review.intelligence_runtime import resolve_driver_model

    try:
        resolved = resolve_driver_model(kwargs.get("driver"), kwargs.get("model"))
    except Exception:  # the call itself will say why; the guard only needs a price
        return "", ""
    return str(resolved.driver), resolved.model


class SpendGuard:
    """Holds one run's model calls to the pull request's cost cap (the module docstring says how, per route).

    *prior* is what the pull request's earlier reviews and chat answers cost (the state's ``spent``); a cost of ``None``
    there means an earlier call reported none, and then this run is held by calls. Thread-safe: the review runs
    investigations in parallel.
    """

    def __init__(
        self,
        cap: float,
        *,
        prior: Usage | None = None,
        call_cap: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        before = prior or Usage()
        self.cap = cap
        self.call_cap = UNPRICED_CALLS_PER_PR if call_cap is None else call_cap
        self.prior_cost: float | None = before.cost_usd if before.calls else 0.0
        self.prior_calls = before.calls
        self.output_tokens = _output_limit(os.environ if environ is None else environ)
        self.spent = 0.0
        #: Calls this run started (in flight or finished) and refused.
        self.started = 0
        self.refused = 0
        #: Calls this run made whose cost nobody reported: one is enough to hold the rest of the run by calls.
        self.unpriced = 0
        self.largest = 0.0
        self.reserved = 0.0
        self.in_flight = 0
        self.stopped = ""
        #: How each priced call's cost was known (``basis_words``), for the receipt.
        self.bases: set[str] = set()
        self._lock = threading.Condition()

    # -- the modes ------------------------------------------------------------------------------------------------

    @property
    def by_calls(self) -> bool:
        """Whether the run is held by calls: an earlier cost, or one of this run's, is unknown."""

        return self.prior_cost is None or self.unpriced > 0

    def calls_used(self) -> int:
        return self.prior_calls + self.started

    def cost_so_far(self) -> float:
        return (self.prior_cost or 0.0) + self.spent

    # -- the gate -------------------------------------------------------------------------------------------------

    def wrap(self, inner: Callable[..., StructuredExecution]) -> Callable[..., StructuredExecution]:
        """*inner* with the cap checked before every call and the call's cost recorded after it."""

        def guarded(**kwargs: Any) -> StructuredExecution:
            reserved = self._admit(kwargs)
            try:
                execution = inner(**kwargs)
            except BaseException:
                self._release(reserved, None)
                raise
            self._release(reserved, execution)
            return execution

        return guarded

    def _admit(self, kwargs: Mapping[str, Any]) -> float:
        """Reserve the next call's estimate, or raise :class:`SpendCapReached`. Waits while a first call has no estimate."""

        chars = len(str(kwargs.get("system") or "")) + len(str(kwargs.get("prompt") or ""))
        _driver, model = _route_model(kwargs)
        table = _table_estimate(model, chars, self.output_tokens)
        with self._lock:
            while True:
                if self.stopped:
                    self.refused += 1
                    raise SpendCapReached(f"{REFUSED}: {self.stopped}")
                if self.by_calls:
                    if self.calls_used() + 1 > self.call_cap:
                        self._stop(f"{self.calls_used()} of {self.call_cap} model calls used (cost not reported)")
                        continue
                    self.started += 1
                    self.in_flight += 1
                    return 0.0
                estimate = max(table or 0.0, self.largest)
                if table is None and self.largest <= 0.0 and self.in_flight:
                    # No price for this route yet: the first call runs alone, and its cost is the estimate after it.
                    self._lock.wait()
                    continue
                if self.cost_so_far() + self.reserved + estimate > self.cap:
                    self._stop(
                        f"the next call (up to {_usd(estimate)}) would pass the {_usd(self.cap)} cap "
                        f"at {_usd(self.cost_so_far() + self.reserved)} spent or in flight"
                    )
                    continue
                self.started += 1
                self.in_flight += 1
                self.reserved += estimate
                return estimate

    def _stop(self, why: str) -> None:
        if not self.stopped:
            self.stopped = why
        self._lock.notify_all()

    def _release(self, reserved: float, execution: StructuredExecution | None) -> None:
        from lemoncrow.pro.capabilities.review.pr_bot.engine import call_usage

        cost: float | None = None
        if execution is not None:
            cost = call_usage(execution.model, execution.usage).cost_usd
        with self._lock:
            self.in_flight -= 1
            self.reserved = max(0.0, self.reserved - reserved)
            if execution is not None:
                if cost is None:
                    self.unpriced += 1
                else:
                    self.spent += cost
                    self.largest = max(self.largest, cost)
                    self.bases.add(basis_words(str(execution.driver), execution.usage.provider_cost is not None))
            self._lock.notify_all()


def _usd(value: float) -> str:
    from lemoncrow.pro.capabilities.review.pr_bot.render import money

    return money(value)


def basis_words(driver: str, reported: bool) -> str:
    """How a call's cost was known, in the words the receipt uses: every route's figure is an estimate at list price."""

    from lemoncrow.pro.capabilities.review.intelligence_runtime import AGENT_CLI_DRIVERS

    if driver in AGENT_CLI_DRIVERS:
        return f"estimated by the {driver} CLI"
    if reported and driver == "litellm":
        return "LiteLLM's list-price estimate"
    return "list-price estimate from reported tokens"


def _basis_of(model_label: str) -> str:
    """The cost basis of a route named by its model label (``litellm/anthropic/claude-...``, ``openai/gpt-...``)."""

    driver = model_label.split("/", 1)[0].split(",", 1)[0].strip()
    return basis_words(driver, reported=driver == "litellm") if driver else ""


def unpriced_words(call_cap: int | None = None) -> str:
    return UNPRICED_WORDS.format(n=UNPRICED_CALLS_PER_PR if call_cap is None else call_cap)


def unpriced(state: ReviewState | None) -> bool:
    """Whether this pull request's cost is unknown (a call reported none), so the cap holds it by calls."""

    return state is not None and state.spent.calls > 0 and state.spent.cost_usd is None


def receipt_line(state: ReviewState, cap: float | None) -> str:
    """The footer's spend line: ``$0.41 this run · $1.12 of $3.00 for this PR · list-price estimate from reported tokens``.

    On a route that reports no cost: ``cost not reported by this route; capped at 60 model calls · 14 of 60 used``.
    Without a cap it says so, and how the cap was turned off.
    """

    from lemoncrow.pro.capabilities.review.pr_bot.render import money

    def usd(value: float) -> str:
        return "$0.00" if value == 0 else money(value)

    total = state.spent
    if total.calls and total.cost_usd is None:
        if cap is None:
            return f"cost not reported by this route · no cap (max-usd-per-pr: none) · {total.calls} model calls"
        limit = UNPRICED_CALLS_PER_PR
        return f"{unpriced_words()} · {min(total.calls, limit)} of {limit} used for this PR"
    run = state.usage.cost_usd
    parts = [f"{usd(run)} this run" if run is not None else "this run's cost not reported"]
    spent = total.cost_usd or 0.0
    parts.append(f"{usd(spent)} of {usd(cap)} for this PR" if cap is not None else f"{usd(spent)} for this PR")
    if cap is None:
        parts.append("no cap (max-usd-per-pr: none)")
    if total.calls and (basis := _basis_of(state.model)):
        parts.append(basis)
    return " · ".join(parts)


def status_suffix(state: ReviewState | None, cap: float | None) -> str:
    """What the status adds for a pull request held by calls; ``""`` otherwise."""

    return unpriced_words() if cap is not None and unpriced(state) else ""


def with_suffix(status: tuple[str, str], suffix: str, limit: int = 140) -> tuple[str, str]:
    """*status* with *suffix* after its description, the description cut so both fit GitHub's limit.

    An ``error`` status is left whole: its description is the cause and the fix (a rejected key, a config error), and
    a call that failed reported no cost because it cost nothing, not because the route hides it.
    """

    if not suffix or status[0] == "error":
        return status
    from lemoncrow.pro.capabilities.review.pr_bot.findings import trimmed

    room = limit - len(suffix) - 3
    return status[0], f"{trimmed(status[1], max(room, 1))} · {suffix}"


def stop_banner(guard: SpendGuard, *, reviewed: tuple[str, ...], pending: tuple[str, ...]) -> str:
    """The block quote under the status strip of a run the cap stopped: why, what it reviewed, what it did not."""

    from lemoncrow.pro.capabilities.review.pr_bot.render import code, money

    def listed(paths: tuple[str, ...]) -> str:
        if not paths:
            return "none"
        shown = ", ".join(f"`{code(path)}`" for path in paths[:10])
        return shown + (f" and {len(paths) - 10} more" if len(paths) > 10 else "")

    if guard.by_calls:
        why = f"{unpriced_words(guard.call_cap)}, and {guard.calls_used()} have been made"
    else:
        why = f"max-usd-per-pr is {money(guard.cap)} and this pull request has spent {money(guard.cost_so_far())}"
    return (
        f"> **Stopped at the cost cap.** {why}, so {plural(guard.refused, 'planned model call')} "
        f"{'was' if guard.refused == 1 else 'were'} not made. "
        f"Reviewed: {listed(reviewed)}. Not reviewed: {listed(pending)}; they are picked up by the next run that has "
        "budget left. Raise `max-usd-per-pr`, or set it to `none`, to review the rest."
    )


def plural(count: int, singular: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


__all__ = [
    "DEFAULT_MAX_USD_PER_PR",
    "MAX_USD_ENV",
    "UNPRICED_CALLS_PER_PR",
    "SpendCapReached",
    "SpendGuard",
    "receipt_line",
    "resolve_max_usd",
    "status_suffix",
    "stop_banner",
    "with_suffix",
]
