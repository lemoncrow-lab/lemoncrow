"""Group-by over :class:`UsageRow` — pure function of the rows, no I/O.

Why this module exists: the interesting question is never "what did everything
cost", it is "what did *this host* / *this model* / *this project* cost, and how
much of that number do I actually believe". A single ``total_usd`` column
cannot answer the second half, so every bucket here keeps host-reported dollars
and rate-card estimates in separate columns and carries the count of rows that
have no price at all.

That last column is the point. Summing a set of rows where some have
``cost_usd is None`` and reporting one number silently equates "free" with
"unknown". ``unpriced_rows`` and ``provenance_mix`` are what let a renderer say
"$4.10 across 12 rows, 3 unpriced" instead.

Output is a total order: same rows in any input order produce byte-identical
buckets.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from lemoncrow.pro.capabilities.usage.models import UsageAggregate, UsageRow

GroupBy = Literal["host", "provider", "model", "project", "session", "day"]
GROUP_BY_VALUES: tuple[str, ...] = ("host", "provider", "model", "project", "session", "day")

TOTAL_KEY = "__total__"
"""The reserved key :func:`totals` uses, so a totals row is never mistaken for
a group whose name happens to be ``"total"``."""

DEFAULT_LIMIT = 20


def group_key(row: UsageRow, by: GroupBy) -> str:
    """Return *row*'s bucket name for the *by* dimension.

    Never returns an empty string: an unresolvable dimension buckets as
    ``"unknown"`` so its tokens stay in the total instead of vanishing.
    """

    if by == "host":
        return row.host or "unknown"
    if by == "provider":
        return row.provider or "unknown"
    if by == "model":
        return row.model or "unknown"
    if by == "project":
        return row.project or "unknown"
    if by == "session":
        return row.session_id or "unknown"
    # "day" -- the row's own normalised UTC start, never the directory the
    # run.json sits in: session_dir resolves its date partition over a 3-day
    # back-window, so the folder date is not the session date.
    return row.started_at.strftime("%Y-%m-%d")


def _fold(key: str, rows: Sequence[UsageRow]) -> UsageAggregate:
    """Fold *rows* into one bucket."""

    billed = 0.0
    estimated = 0.0
    unpriced = 0
    mix: dict[str, int] = {}
    sessions: set[str] = set()

    for row in rows:
        mix[row.cost_provenance] = mix.get(row.cost_provenance, 0) + 1
        sessions.add(row.session_id)
        if row.cost_usd is None:
            # The whole contract: an unknown price adds nothing to either
            # dollar column and is counted instead.
            unpriced += 1
        elif row.cost_provenance == "provider_billed":
            billed += row.cost_usd
        else:
            estimated += row.cost_usd

    return UsageAggregate(
        key=key,
        rows=len(rows),
        sessions=len(sessions),
        total_tokens=sum(r.total_tokens for r in rows),
        input_tokens=sum(r.input_tokens for r in rows),
        output_tokens=sum(r.output_tokens for r in rows),
        cache_read_tokens=sum(r.cache_read_tokens for r in rows),
        cache_write_tokens=sum(r.cache_write_tokens for r in rows),
        thinking_tokens=sum(r.thinking_tokens for r in rows),
        billed_usd=round(billed, 6),
        estimated_usd=round(estimated, 6),
        unpriced_rows=unpriced,
        provenance_mix=tuple(sorted(mix.items())),
        duration_seconds=round(sum(r.duration_seconds for r in rows), 3),
        tool_calls=sum(r.tool_calls for r in rows),
    )


def aggregate(rows: Sequence[UsageRow], *, by: GroupBy, limit: int = DEFAULT_LIMIT) -> list[UsageAggregate]:
    """Return the *limit* largest buckets of *rows* along the *by* dimension.

    Ranked by spend, then by tokens, then by key -- a total order, so the same
    rows in a different order produce the identical list. Ranking by spend
    alone would sink an all-unpriced bucket to the bottom regardless of size,
    which is exactly the usage a reader most needs to see, so tokens break the
    tie before the name does.
    """

    buckets: dict[str, list[UsageRow]] = {}
    for row in rows:
        buckets.setdefault(group_key(row, by), []).append(row)

    folded = [_fold(key, bucket) for key, bucket in buckets.items()]
    folded.sort(key=lambda a: (-a.total_usd, -a.total_tokens, a.key))
    if limit >= 0:
        return folded[:limit]
    return folded


def totals(rows: Sequence[UsageRow]) -> UsageAggregate:
    """Return one bucket covering every row, keyed :data:`TOTAL_KEY`."""

    return _fold(TOTAL_KEY, rows)


def provenance_mix(rows: Sequence[UsageRow]) -> tuple[tuple[str, int], ...]:
    """Return the sorted ``(provenance, row_count)`` mix across *rows*.

    Exposed separately because the mix belongs at the top level of a ``--json``
    payload, not only inside each bucket: it is the one line that tells a reader
    how much of the report is measured and how much is estimated.
    """

    return totals(rows).provenance_mix


__all__ = [
    "DEFAULT_LIMIT",
    "GROUP_BY_VALUES",
    "TOTAL_KEY",
    "GroupBy",
    "aggregate",
    "group_key",
    "provenance_mix",
    "totals",
]
