"""Terminal views for `lc usage` — the accounting screen and one run's explain.

Why this module exists: the aggregate carries two dollar columns, an unpriced
row count and a provenance mix precisely because one ``total_usd`` cannot be
read honestly. A renderer that collapses all of that back into a single money
column throws the contract away at the last step, and the failure is silent —
a session on a local model prints ``$0.00`` and reads as free.

So the rule this module enforces is narrow and absolute: **a bucket with no
priced rows never renders a dollar amount.** It renders what it actually is —
``local`` for a self-hosted model with no rate card, ``seat`` for subscription
usage the vendor never bills per token, ``—`` when even the model id could not
be resolved. The BASIS column carries the same information for mixed buckets,
and the footer counts what the COST column had to leave out.

The second rule is a product one (plan 2026-09-07 §4.2): the first screen is
*visibility*, never a counterfactual. Nothing here computes what usage could
have cost under a different policy — that lives behind `lc usage optimize`.

Rich is imported inside the render functions, so importing this module is free
and a Rich failure degrades to the plain path instead of losing the report.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from typing import Any

from .models import UNPRICED_PROVENANCE_VALUES, UsageAggregate

_KEY_HEADERS: dict[str, str] = {
    "host": "HOST",
    "provider": "PROVIDER",
    "model": "MODEL",
    "project": "PROJECT",
    "session": "SESSION",
    "day": "DAY",
}

# What an unpriced bucket says instead of a dollar amount. Each one names the
# reason no price exists, because "unpriced" and "free" are different facts and
# only the first is true here.
UNPRICED_LABELS: dict[str, str] = {
    "self_hosted_unpriced": "local",
    "enterprise_allocated": "seat",
    "unknown": "—",
}

# Short forms for the BASIS column: how much of a bucket's money is measured
# rather than inferred. Kept to one word so the column never wraps.
_BASIS_LABELS: dict[str, str] = {
    "provider_billed": "billed",
    "api_estimated": "estimated",
    "self_hosted_estimated": "local-est",
    "self_hosted_unpriced": "local",
    "enterprise_allocated": "seat",
    "unknown": "unknown",
}

# How a *bucket* got its dollars, which is never the same question as how the
# session total got its own -- see `explain.BREAKDOWN_BASIS_VALUES`. Short forms
# for the BASIS column; the sentences below carry the reason.
_SPLIT_BASIS_LABELS: dict[str, str] = {
    "ledger_per_turn": "derived",
    "rate_card_single_model": "derived",
    "unattributable": "unknown",
    "unpriced": "unpriced",
    "unseparable": "unseparable",
    "none": "—",
}

# The line under the headline. It exists because "$532.90 billed" is true of the
# total and false of every row beneath it, and a reader has no way to know that
# from a provenance word attached to the total.
_SPLIT_BASIS_SENTENCES: dict[str, str] = {
    "ledger_per_turn": (
        "split: derived, not billed — each recorded turn priced at its own model, rescaled to the total"
    ),
    "rate_card_single_model": (
        "split: derived, not billed — this session's one model priced off its rate card, rescaled to the total"
    ),
    "unattributable": "split: unknown — several models ran and nothing records which turn used which",
    "unpriced": "split: unpriced — the session total itself has no price",
    "none": "split: none — no usage was recorded",
}

# The footer spells the same three states out: a one-character column cell is
# not a sentence, and this line is the one place the omission is explained.
_UNPRICED_REASONS: dict[str, str] = {
    "self_hosted_unpriced": "local, no rate card",
    "enterprise_allocated": "subscription seat",
    "unknown": "model id unresolved",
}

_UNKNOWN_COST = "—"
_MAX_SESSION_KEY = 36
_MAX_MODEL_KEY = 44
_RICH_WIDTH = 110


def _fmt_usd(value: float) -> str:
    """Format dollars exactly as every other LemonCrow surface does."""

    from lemoncrow.core.capabilities.savings_summary import _fmt_usd as _shared

    return _shared(value)


def _fmt_tok(value: int) -> str:
    """Format a token count as every other LemonCrow surface does, plus billions.

    The shared formatter stops at ``M``, which was fine for one session and is
    not fine for a 90-day total: it renders 54 billion tokens as ``53949.35M``.
    Anything below a billion is delegated unchanged, so every number this report
    shares with `lc savings` still matches it character for character.
    """

    from lemoncrow.core.capabilities.savings_summary import _fmt_tok as _shared

    count = int(value or 0)
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.2f}B"
    return _shared(count)


def _fmt_duration(seconds: float) -> str:
    """Render a wall-clock span compactly: ``42s``, ``9m 3s``, ``2h 14m``."""

    total = int(max(0.0, float(seconds or 0.0)))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    return f"{total // 3600}h {(total % 3600) // 60}m"


def _unpriced_label(mix: Sequence[tuple[str, int]]) -> str:
    """Return the label for a bucket whose rows are all unpriced.

    The dominant unpriced provenance wins, ties broken by name so the same
    rows always render the same word.
    """

    unpriced = [entry for entry in mix if entry[0] in UNPRICED_PROVENANCE_VALUES]
    if not unpriced:
        return _UNKNOWN_COST
    dominant = sorted(unpriced, key=lambda entry: (-entry[1], entry[0]))[0][0]
    return UNPRICED_LABELS.get(dominant, _UNKNOWN_COST)


_SELF_HOSTED_PROVENANCES: frozenset[str] = frozenset({"self_hosted_estimated", "self_hosted_unpriced"})


def _all_self_hosted(bucket: UsageAggregate) -> bool:
    """True when every row in *bucket* ran on a model the user hosts."""

    mix = bucket.provenance_mix
    return bool(mix) and all(provenance in _SELF_HOSTED_PROVENANCES for provenance, _count in mix)


def cost_cell(bucket: UsageAggregate) -> str:
    """Return *bucket*'s COST cell — a dollar amount only when one is real.

    Two cases never render as money. A bucket in which every row has
    ``cost_usd is None`` has a ``total_usd`` of ``0.0``, and printing that is
    the single most misleading thing this report could do; it prints the reason
    instead.

    The second is subtler and is the reason this is not a one-line function:
    LiteLLM *does* carry rate-card entries for a handful of self-hosted ids
    (``ollama/llama3`` among them) and every rate on them is zero. Those rows
    come back priced, at exactly ``$0.00``. That figure is a rate-card artifact,
    not a measurement of what the hardware cost, so a bucket that is entirely
    self-hosted and totals zero renders ``local`` too.
    """

    if bucket.rows > 0 and bucket.unpriced_rows >= bucket.rows:
        return _unpriced_label(bucket.provenance_mix)
    if bucket.total_usd == 0.0 and _all_self_hosted(bucket):
        return UNPRICED_LABELS["self_hosted_unpriced"]
    return _fmt_usd(bucket.total_usd)


def basis_cell(bucket: UsageAggregate) -> str:
    """Return *bucket*'s BASIS cell: where its number came from.

    One provenance renders as itself, two render joined, three or more render
    ``mixed`` — past two the column stops being readable and the ``--json``
    payload carries the full mix anyway.
    """

    labels: list[str] = []
    for provenance, _count in sorted(bucket.provenance_mix, key=lambda entry: (-entry[1], entry[0])):
        label = _BASIS_LABELS.get(provenance, provenance)
        if label not in labels:
            labels.append(label)
    if not labels:
        return _UNKNOWN_COST
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return "+".join(labels)
    return "mixed"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _key_cell(bucket: UsageAggregate, by: str) -> str:
    if by == "session":
        return _truncate(bucket.key, _MAX_SESSION_KEY)
    if by == "model":
        return _truncate(bucket.key, _MAX_MODEL_KEY)
    return bucket.key


def _header_line(by: str, window: str) -> str:
    return f"USAGE  ·  last {window}  ·  by {by}"


def _unpriced_footer(total: UsageAggregate) -> str | None:
    """Return the line that accounts for everything the COST column omitted."""

    if total.unpriced_rows <= 0:
        return None
    parts = [
        f"{count} {_UNPRICED_REASONS.get(provenance, provenance)}"
        for provenance, count in sorted(total.provenance_mix, key=lambda entry: (-entry[1], entry[0]))
        if provenance in UNPRICED_PROVENANCE_VALUES
    ]
    detail = f" ({', '.join(parts)})" if parts else ""
    noun = "row" if total.unpriced_rows == 1 else "rows"
    return f"{total.unpriced_rows} of {total.rows} {noun} carry no price and are not in any COST above{detail}."


def _hint_lines() -> list[str]:
    # Deliberately not a savings claim: the first screen reports what happened,
    # and the counterfactual lives one explicit command away.
    return [
        "`lc usage explain <run>` breaks one session down; `lc usage optimize` is the savings analysis.",
    ]


def _empty_lines(by: str, window: str) -> list[str]:
    return [
        _header_line(by, window),
        "",
        f"no usage recorded in the last {window}.",
        "",
        "widen the window with `--since 30d`, or check that a host is importing sessions (`lc doctor`).",
    ]


def _usage_rows(groups: Sequence[UsageAggregate], by: str) -> list[tuple[str, ...]]:
    return [
        (
            _key_cell(bucket, by),
            f"{bucket.sessions:,}",
            f"{bucket.rows:,}",
            _fmt_tok(bucket.total_tokens),
            cost_cell(bucket),
            basis_cell(bucket),
        )
        for bucket in groups
    ]


def _plain_table(headers: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[bool]) -> list[str]:
    """Lay out a fixed-width table. Column widths follow the widest cell."""

    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def _line(cells: Sequence[str]) -> str:
        parts = [
            cell.rjust(widths[index]) if right[index] else cell.ljust(widths[index]) for index, cell in enumerate(cells)
        ]
        return "  ".join(parts).rstrip()

    return [_line(headers), *(_line(row) for row in rows)]


def _render_usage_plain(
    groups: Sequence[UsageAggregate],
    total: UsageAggregate,
    *,
    by: str,
    window: str,
) -> str:
    if total.rows == 0:
        return "\n".join(_empty_lines(by, window))

    headers = (_KEY_HEADERS.get(by, by.upper()), "SESSIONS", "ROWS", "TOKENS", "COST", "BASIS")
    right = (False, True, True, True, True, False)
    body = _usage_rows(groups, by)
    footer = (
        "TOTAL",
        f"{total.sessions:,}",
        f"{total.rows:,}",
        _fmt_tok(total.total_tokens),
        cost_cell(total),
        basis_cell(total),
    )

    out = [_header_line(by, window), ""]
    table = _plain_table(headers, [*body, footer], right)
    # A rule before TOTAL: without it the totals row reads as one more group.
    table.insert(len(table) - 1, "-" * max(len(line) for line in table))
    out.extend(table)
    unpriced = _unpriced_footer(total)
    if unpriced:
        out.extend(["", unpriced])
    if _was_truncated(total, groups):
        out.append(f"showing the top {len(groups)} groups; raise `--limit` for the rest.")
    out.append("")
    out.extend(_hint_lines())
    return "\n".join(out)


def _was_truncated(total: UsageAggregate, groups: Sequence[UsageAggregate]) -> bool:
    """True when ``--limit`` dropped at least one group.

    The aggregate carries no pre-truncation count, so this compares the rows the
    shown groups account for against the total: any shortfall means a group was
    left out, and a total that does not match its own table needs saying.
    """

    return sum(bucket.rows for bucket in groups) < total.rows


def _render_usage_rich(
    groups: Sequence[UsageAggregate],
    total: UsageAggregate,
    *,
    by: str,
    window: str,
) -> str:
    from rich import box
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=_RICH_WIDTH, legacy_windows=False, highlight=False)

    if total.rows == 0:
        lines = _empty_lines(by, window)
        console.print(f"[bold bright_white]{escape(lines[0])}[/]")
        for line in lines[2:]:
            console.print(f"[dim]{escape(line)}[/]" if line else "")
        return buffer.getvalue()

    console.print(f"[bold bright_white]USAGE[/]  [dim]·  last {escape(window)}  ·  by {escape(by)}[/]")
    console.print()

    table = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
    table.add_column(_KEY_HEADERS.get(by, by.upper()), overflow="fold")
    table.add_column("Sessions", justify="right")
    table.add_column("Rows", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Basis")
    for cells in _usage_rows(groups, by):
        table.add_row(*(escape(cell) for cell in cells))
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/]",
        f"[bold]{total.sessions:,}[/]",
        f"[bold]{total.rows:,}[/]",
        f"[bold]{escape(_fmt_tok(total.total_tokens))}[/]",
        f"[bold]{escape(cost_cell(total))}[/]",
        f"[bold]{escape(basis_cell(total))}[/]",
    )
    console.print(table)

    unpriced = _unpriced_footer(total)
    if unpriced:
        console.print(f"[yellow]{escape(unpriced)}[/]")
    if _was_truncated(total, groups):
        console.print(f"[dim]showing the top {len(groups)} groups; raise `--limit` for the rest.[/]")
    console.print()
    for line in _hint_lines():
        console.print(f"[dim]{escape(line)}[/]")
    return buffer.getvalue()


def render_usage(
    groups: Sequence[UsageAggregate],
    total: UsageAggregate,
    *,
    by: str,
    window: str,
    no_color: bool = False,
) -> str:
    """Render the `lc usage` accounting screen.

    ``no_color`` takes the Rich-free path outright rather than stripping ANSI
    afterwards, so piping into a file or a hook never yields box-drawing
    characters either.
    """

    if no_color:
        return _render_usage_plain(groups, total, by=by, window=window)
    try:
        return _render_usage_rich(groups, total, by=by, window=window)
    except Exception:
        # A renderer must never be the reason a cost report fails to print.
        return _render_usage_plain(groups, total, by=by, window=window)


def _explain_cost_cell(cost: float | None) -> str:
    return _UNKNOWN_COST if cost is None else _fmt_usd(cost)


def _explain_headline(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return the three header lines: id, identity, headline figures."""

    session_id = str(payload.get("session_id") or "unknown")
    identity = "  ·  ".join(
        part
        for part in (
            str(payload.get("host") or "unknown"),
            ", ".join(str(m) for m in payload.get("models") or []) or str(payload.get("model") or "unknown model"),
            str(payload.get("project") or "unknown"),
        )
        if part
    )

    cost = payload.get("total_cost_usd")
    provenance = str(payload.get("cost_provenance") or "unknown")
    # Same rule as the table: an unpriced run states why, it does not say $0.00.
    money = UNPRICED_LABELS.get(provenance, _UNKNOWN_COST) if cost is None else _fmt_usd(float(cost))
    figures = "  ·  ".join(
        (
            f"{money} {_BASIS_LABELS.get(provenance, provenance)}",
            f"{_fmt_tok(int(payload.get('total_tokens') or 0))} tokens",
            _fmt_duration(float(payload.get("duration_seconds") or 0.0)),
        )
    )
    return f"RUN  {session_id}", identity, figures


def _split_basis_line(payload: Mapping[str, Any]) -> str:
    """Return the one line that keeps the split from inheriting the total's basis."""

    basis = str(payload.get("breakdown_basis") or "none")
    sentence = _SPLIT_BASIS_SENTENCES.get(basis, f"split: {basis}")
    if payload.get("breakdown_basis_approximate") and basis in ("ledger_per_turn", "rate_card_single_model"):
        sentence += ", at approximate rates"
    return sentence


def _breakdown_rows(payload: Mapping[str, Any]) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for entry in payload.get("breakdown") or []:
        cost = entry.get("cost_usd")
        basis = str(entry.get("basis") or "none")
        rows.append(
            (
                str(entry.get("bucket") or ""),
                _fmt_tok(int(entry.get("tokens") or 0)),
                f"{float(entry.get('share') or 0.0) * 100:.1f}%",
                _explain_cost_cell(None if cost is None else float(cost)),
                _SPLIT_BASIS_LABELS.get(basis, basis),
            )
        )
    return rows


def _tool_rows(payload: Mapping[str, Any]) -> list[tuple[str, ...]]:
    return [
        (
            str(entry.get("name") or ""),
            f"{int(entry.get('calls') or 0):,} calls",
            _fmt_usd(float(entry.get("cost_usd") or 0.0)),
        )
        for entry in payload.get("top_tools") or []
    ]


def _render_explain_plain(payload: Mapping[str, Any]) -> str:
    title, identity, figures = _explain_headline(payload)
    out = [title, identity, figures, _split_basis_line(payload), "", "WHERE IT WENT"]
    out.extend(
        f"  {line}"
        for line in _plain_table(
            ("BUCKET", "TOKENS", "SHARE", "COST", "BASIS"),
            _breakdown_rows(payload),
            (False, True, True, True, False),
        )
    )

    tools = _tool_rows(payload)
    if tools:
        out.extend(["", "TOP TOOLS"])
        out.extend(f"  {line}" for line in _plain_table(("TOOL", "CALLS", "COST"), tools, (False, True, True)))

    notes = [str(note) for note in payload.get("notes") or []]
    if notes:
        out.extend(["", "NOTES"])
        out.extend(f"  - {note}" for note in notes)
    return "\n".join(out)


def _render_explain_rich(payload: Mapping[str, Any]) -> str:
    from rich import box
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=_RICH_WIDTH, legacy_windows=False, highlight=False)

    title, identity, figures = _explain_headline(payload)
    console.print(f"[bold bright_white]{escape(title)}[/]")
    console.print(f"[dim]{escape(identity)}[/]")
    console.print(f"[bold]{escape(figures)}[/]")
    console.print(f"[dim]{escape(_split_basis_line(payload))}[/]")

    console.print()
    console.print("[bold bright_white]WHERE IT WENT[/]")
    table = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
    table.add_column("Bucket")
    table.add_column("Tokens", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Basis")
    for cells in _breakdown_rows(payload):
        table.add_row(*(escape(cell) for cell in cells))
    console.print(table)

    tools = _tool_rows(payload)
    if tools:
        console.print("[bold bright_white]TOP TOOLS[/]")
        tool_table = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
        tool_table.add_column("Tool")
        tool_table.add_column("Calls", justify="right")
        tool_table.add_column("Cost", justify="right")
        for cells in tools:
            tool_table.add_row(*(escape(cell) for cell in cells))
        console.print(tool_table)

    notes = [str(note) for note in payload.get("notes") or []]
    if notes:
        console.print("[bold bright_white]NOTES[/]")
        for note in notes:
            console.print(f"  [dim]-[/] {escape(note)}")
    return buffer.getvalue()


def render_explain(payload: Mapping[str, Any], *, no_color: bool = False) -> str:
    """Render one run's cost decomposition.

    Takes the ``explain_run`` payload rather than a dataclass so the text and
    ``--json`` views are provably the same facts.
    """

    if no_color:
        return _render_explain_plain(payload)
    try:
        return _render_explain_rich(payload)
    except Exception:
        return _render_explain_plain(payload)


__all__ = [
    "UNPRICED_LABELS",
    "basis_cell",
    "cost_cell",
    "render_explain",
    "render_usage",
]
