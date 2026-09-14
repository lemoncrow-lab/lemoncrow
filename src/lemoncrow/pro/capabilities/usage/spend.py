"""Spend over a trailing window — the one number every surface has to agree on.

Why this module exists: until now two independent things both called themselves
"spend". ``lc usage`` summed the usage read model (run ledgers + traces, priced
per row, unknown models deliberately unpriced). The savings screen summed the
``spend`` column of the savings ledger's day-bucketed aggregate (Claude
transcript turn costs, attributed by session). On a real store those two
disagreed by 64% and both numbers were printed on the same screen, four lines
apart, with no way for a reader to tell which one was the spend.

The savings aggregate's column is also wrong in a way the read model is not: it
is keyed by session id and a resumed Claude session gets one ledger directory
per date partition, every one of which resolves back to the *same* transcript,
so the same turns are folded two or three times (see
``_window_from_aggregate``, which now de-duplicates them at read time).

So this module makes the choice explicit: **the usage read model is the source
of truth for spend**, and every surface that shows a dollar of spend reads it
through here. :func:`reconcile_spend` rewrites the savings payload's spend
figures in place of the ledger-derived ones, which is what makes
``lc usage --since 30d`` and ``lc usage optimize`` incapable of disagreeing.

Two rules this module will not bend:

* A window is collected at exactly the window the caller asked for. Slicing a
  30-day collection down to 1 day would be cheaper, but rows whose source
  records tokens and no timestamp are kept by whichever window collected them,
  so a slice and a real collection are not the same set. Agreement has to be
  exact or it is not agreement.
* A failure produces ``available=False``, never ``$0.00``. An unreadable store
  has *unknown* spend, and rendering that as zero would turn a missing signal
  into a false one — the same mistake the read model exists to avoid.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_WINDOWS: tuple[int, ...] = (1, 7, 30)
"""The windows the savings breakdown shows, in days."""

SPEND_SOURCE = "usage_read_model"
"""Stamped onto any payload whose spend came from here, so a JSON consumer can
tell a reconciled payload from a legacy one without guessing."""

HEADLINE_WINDOW_DAYS = 30
"""The window the savings headline quotes. Named so the renderer, the payload
and the tests cannot drift apart on which window "the" spend refers to."""


@dataclass(frozen=True)
class WindowSpend:
    """What was actually spent over one trailing window, and how well it is known.

    ``billed_usd`` and ``estimated_usd`` stay apart for the same reason
    :class:`~lemoncrow.pro.capabilities.usage.models.UsageAggregate` keeps them
    apart, and ``unpriced_rows`` is carried all the way to the renderer because
    it is what makes ``total_usd`` a floor rather than a fact: rows with no rate
    card contribute nothing to it.
    """

    window_days: int
    since: str
    available: bool = True
    total_usd: float = 0.0
    billed_usd: float = 0.0
    estimated_usd: float = 0.0
    rows: int = 0
    unpriced_rows: int = 0
    sessions: int = 0

    @property
    def is_floor(self) -> bool:
        """True when some rows in the window carry no price.

        A spend total built from a set that includes unpriced rows can only be
        a lower bound, which makes any ratio taken against it an upper bound.
        Callers that print a percentage are expected to say so.
        """

        return self.unpriced_rows > 0

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this window.

        ``since`` is deliberately NOT in it. It is a wall-clock instant, and
        ``lc savings --json`` is contractually a pure function of the store --
        two invocations a second apart must produce byte-identical payloads
        (``test_build_savings_payload_matches_the_cli_json``). ``window_days``
        states the window in the terms every surface labels it with anyway, and
        the instant stays on the dataclass for callers that need it.
        """

        return {
            "window_days": self.window_days,
            "available": self.available,
            "source": SPEND_SOURCE,
            "total_usd": round(self.total_usd, 6),
            "billed_usd": round(self.billed_usd, 6),
            "estimated_usd": round(self.estimated_usd, 6),
            "rows": self.rows,
            "unpriced_rows": self.unpriced_rows,
            "sessions": self.sessions,
        }


def _since(days: int, now: datetime | None = None) -> datetime:
    """The exact instant the window starts.

    Deliberately an instant and not a calendar day: ``lc usage --since 30d``
    subtracts a ``timedelta`` from *now*, and rounding down to 00:00 UTC here
    would silently widen this window to 31 days — on a real store that one
    boundary day was worth $2,493.
    """

    return (now or datetime.now(UTC)) - timedelta(days=max(0, int(days)))


_HISTORY_DB_NAME = "lemoncrow_history.db"
"""``HistoryStore``'s file name, spelled out rather than imported: this probe
runs on the ``lc savings`` / statusline path and pulling in the store package
to read one constant is not worth it.
``test_the_probed_history_db_name_is_the_one_the_store_uses`` guards the
duplication against drift."""


def _opens(path: Path) -> bool:
    """True when *path* can actually be opened for reading.

    A directory listing reports a file it cannot read exactly like one it can:
    the permission bit lives on the file, not on the entry naming it. Opening
    it is the only way to tell a readable ledger from a ``chmod 000`` one.
    """

    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    os.close(handle)
    return True


def _store_is_readable(root: Path) -> bool:
    """True when the sources ``collect_usage_rows`` reads can actually be read.

    ``collect_usage_rows`` degrades every I/O failure to "fewer rows" by
    contract: ``list_run_files`` globs (``pathlib`` swallows a mid-walk
    ``PermissionError`` silently), ``rows_from_run_file`` returns ``[]`` for a
    ``run.json`` it cannot open, and ``load_trace_facts`` / ``_token_rows``
    catch everything. So an unreadable store and an empty one both arrive at
    :func:`window_spend` as zero rows, and only one of them means the spend was
    zero -- which is how ``available=False`` became unreachable for the real
    I/O failures it was written for.

    All three of the collector's failure surfaces are probed, because closing
    only the first left the fabrication one permission bit away:

    * the ``sessions/`` tree cannot be enumerated -- walking it with
      ``onerror`` re-raising is what makes that visible;
    * the tree enumerates but a ``run.json`` in it will not open. Every
      directory readable and the ledgers themselves unreadable is the ordinary
      shape of a bad umask or a half-synced mount, and a directory walk is
      blind to it;
    * the history database exists but will not open. It is the *only* source
      for a store whose sessions were imported without a run ledger, so a
      store that reads all of its spend out of traces was never probed at all.

    Only the all-or-nothing case is guarded: a *partially* unreadable tree
    still yields rows and is reported as a (low) total rather than as unknown,
    because this runs on the zero-row path alone. That is a floor, not a
    fabrication; a zero conjured out of an unreadable store is the fabrication.

    The deliberate cost of that: one unreadable ledger anywhere in the tree
    makes every *empty* window unknown, even a window it could not have
    contributed to. Erring that way is the module's whole thesis -- a store
    with a file nobody can read has an unknown spend, and "unknown" is the one
    answer that cannot be wrong.
    """

    def _reraise(error: OSError) -> None:
        raise error

    try:
        entries = {entry.name for entry in os.scandir(root)}
    except FileNotFoundError:
        # No store on disk at all: an empty window, not a failure.
        return True
    except OSError:
        return False
    if _HISTORY_DB_NAME in entries:
        # The database is three files, not one. `SqliteTableStore._connect` sets
        # `PRAGMA journal_mode = WAL`, so a live history store carries `-wal` and
        # `-shm` sidecars and sqlite needs all of them: with `-wal` at chmod 000
        # the main file opens fine, `token_rows()` raises OperationalError, and
        # the window reported a measured $0.00 -- the same fabricated free month
        # one file further along.
        for suffix in ("", "-wal", "-shm"):
            sidecar = root / f"{_HISTORY_DB_NAME}{suffix}"
            if sidecar.exists() and not _opens(sidecar):
                return False
    if "sessions" not in entries:
        return True
    try:
        for dirpath, _dirnames, filenames in os.walk(root / "sessions", onerror=_reraise):
            if "run.json" in filenames and not _opens(Path(dirpath) / "run.json"):
                return False
    except OSError:
        return False
    return True


def _readability_probe(root: Path) -> Callable[[], bool]:
    """A memoised :func:`_store_is_readable` for *root*.

    The probe costs a full walk of ``sessions/`` and does not depend on the
    window, so the windows :func:`spend_by_window` collects share one instead
    of walking the tree once each. On a store with 4,530 session directories
    that walk measured ~0.1s, and ``lc savings`` and the ``statusline_segment``
    MCP path both ask for three windows.
    """

    answer: bool | None = None

    def probe() -> bool:
        nonlocal answer
        if answer is None:
            answer = _store_is_readable(root)
        return answer

    return probe


def window_spend(
    root: str | Path,
    *,
    days: int,
    now: datetime | None = None,
    store_readable: Callable[[], bool] | None = None,
) -> WindowSpend:
    """Return spend over the last *days*, read exactly the way ``lc usage`` reads it.

    Same collector, same window arithmetic, same pricing contract — so this can
    never return a different number than the usage table for the same window.
    Any failure degrades to ``available=False`` with zeroed columns; it never
    raises and never claims the spend was zero.

    *store_readable* is the zero-row readability probe, injected so
    :func:`spend_by_window` can share one walk of the store across its windows
    rather than paying for a walk per window.
    """

    started = _since(days, now)
    try:
        from lemoncrow.pro.capabilities.usage.aggregate import totals
        from lemoncrow.pro.capabilities.usage.collect import collect_usage_rows

        rows = collect_usage_rows(Path(root), since=started)
        total = totals(rows)
    except Exception:
        # Missing store, unreadable ledger, schema-less database: the honest
        # answer is "unknown", which is what available=False means.
        return WindowSpend(window_days=int(days), since=started.isoformat(), available=False)
    if not total.rows:
        # The collector never raises, so a store it could not read comes back
        # as an empty one. "$0.00 measured" over an unreadable store is the
        # false signal this module exists to prevent. The probe walks the whole
        # tree, so it is gated on the zero-row path: a window with any activity
        # in it never pays for one, and a quiet window pays for it once per
        # `spend_by_window` call rather than once per window.
        probe = store_readable if store_readable is not None else _readability_probe(Path(root))
        if not probe():
            return WindowSpend(window_days=int(days), since=started.isoformat(), available=False)
    return WindowSpend(
        window_days=int(days),
        since=started.isoformat(),
        available=True,
        total_usd=total.total_usd,
        billed_usd=total.billed_usd,
        estimated_usd=total.estimated_usd,
        rows=total.rows,
        unpriced_rows=total.unpriced_rows,
        sessions=total.sessions,
    )


def spend_by_window(
    root: str | Path,
    *,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    now: datetime | None = None,
) -> dict[int, WindowSpend]:
    """Return :func:`window_spend` for each of *windows*, keyed by day count.

    One collection per window on purpose (see the module docstring): the cheap
    version — collect the widest window once and slice — produces a different
    row set for the narrow windows, and a spend figure that is nearly right is
    exactly the failure this module was written to end.

    The readability probe is the one thing that *is* shared: it asks about the
    store, not about a window, so walking the tree once per window was pure
    waste on a store whose recent windows are quiet.
    """

    at = now or datetime.now(UTC)
    probe = _readability_probe(Path(root))
    return {int(days): window_spend(root, days=days, now=at, store_readable=probe) for days in windows}


_WINDOW_KEYS: dict[int, str] = {1: "1D", 7: "7D", 30: "30D"}
"""``summary_breakdown`` keys, which are day counts spelled the payload's way."""


def reconcile_spend(
    payload: dict[str, Any],
    root: str | Path,
    *,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Replace the savings payload's ledger-derived spend with the read model's.

    Mutates and returns *payload* (it is a freshly built dict owned by the
    caller). Every window bucket gains:

    ``spend``
        The usage read model's total for that window, or the key is **removed**
        when the read model could not answer — an absent figure renders as
        "spend unavailable", where a zero would render as a free month.
    ``spend_unpriced_rows`` / ``spend_rows``
        What the total had to leave out, so the renderer can qualify it.

    The payload itself gains ``spend_source`` and ``spend_windows`` so a
    ``--json`` consumer can see which model produced the number.
    """

    breakdown = payload.get("summary_breakdown")
    if not isinstance(breakdown, dict):
        return payload

    measured = spend_by_window(root, windows=windows, now=now)
    payload["spend_source"] = SPEND_SOURCE
    payload["spend_windows"] = {_WINDOW_KEYS.get(d, f"{d}D"): s.to_dict() for d, s in sorted(measured.items())}

    for days, spend in measured.items():
        key = _WINDOW_KEYS.get(days, f"{days}D")
        bucket = breakdown.get(key)
        if not isinstance(bucket, dict):
            continue
        if not spend.available:
            # Drop it rather than zero it: the renderer prints nothing for a
            # missing key and "$0.00 spend" for a present zero.
            bucket.pop("spend", None)
            bucket["spend_available"] = False
            continue
        bucket["spend"] = round(spend.total_usd, 2)
        bucket["spend_available"] = True
        bucket["spend_rows"] = spend.rows
        bucket["spend_unpriced_rows"] = spend.unpriced_rows
    return payload


__all__ = [
    "DEFAULT_WINDOWS",
    "HEADLINE_WINDOW_DAYS",
    "SPEND_SOURCE",
    "WindowSpend",
    "reconcile_spend",
    "spend_by_window",
    "window_spend",
]
