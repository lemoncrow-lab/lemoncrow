"""Decompose one session's cost into buckets a reader can act on.

Why this module exists: a session total answers "how much" and nothing else.
The actionable question is which bucket the money went to -- fresh input, cache
reads, cache writes, output, thinking -- because each one has a different
remedy, and "$4.10" has none.

Three honesty rules shape the payload. First, an unpriced session reports
``total_cost_usd: None`` and says why in ``notes``; it does not report ``0.0``.
Second, the ``subagents`` bucket always reports ``share=0.0``: subagent
transcripts are folded into the parent session at import time and their tokens
are already inside the other buckets. Splitting them back out would require
data that was discarded, so this states the fold instead of faking the split.

Third, the total's basis and the split's basis are two different facts and get
two different fields. ``cost_provenance`` describes ``total_cost_usd`` and is
read off the rows that produced those dollars, never off the row with the most
tokens -- a session whose biggest model is unpriced must not lend its "local"
to a rate-card total. ``breakdown_basis`` describes the buckets, and it is
never "billed": no vendor itemises an invoice by cache-read versus cache-write,
so every split here is derived, from the run ledger's per-turn record or from a
single rate card. One word covering both let a total's provenance vouch for a
split nobody billed, which is exactly the claim this module must not make.

Bucket costs are scaled to the session's own cost so they add back up to it,
which matters when the ledger's per-call total is not the sum of the rate
card's buckets over the session aggregate.

The bucket *shape* is not derived here. ``lc session report`` already prices
every recorded turn at that turn's own model
(``session_report._cost_breakdown_from_calls``) and rescales the result to the
recorded total; this module reads those four figures back out rather than re-deriving
them from the session's dominant model. A second accounting path lets the two
surfaces print different splits of the same dollar -- which is what a
dominant-model shape did on mixed-model sessions, where pricing 5.5M haiku
cache-reads off the opus rate card moved 26% of the total between buckets.

When there is no run ledger the rate card is the only shape available, and it
is used only for a session that resolved to a single model -- there is then no
other model to mis-attribute to. A multi-model session with no per-turn record
reports ``cost_usd: None`` per bucket and says why, because a wrong split is
worse than an admitted unknown.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from lemoncrow.pro.capabilities.usage.collect import collect_rows_for_session
from lemoncrow.pro.capabilities.usage.models import USAGE_SCHEMA_VERSION, UsageRow

if TYPE_CHECKING:
    from lemoncrow.infra.runtime.session_report import SessionReport

EXPLAIN_SCHEMA_VERSION = USAGE_SCHEMA_VERSION

BREAKDOWN_BUCKETS: tuple[str, ...] = (
    "fresh_input",
    "cache_read",
    "cache_write",
    "output",
    "thinking",
    "subagents",
    "tool_results",
)

# Buckets whose tokens this substrate cannot separate out. They are reported at
# zero tokens / unknown cost rather than omitted, so the shape is stable and the
# gap is visible instead of implied.
_UNSEPARABLE_BUCKETS: tuple[str, ...] = ("subagents", "tool_results")

BREAKDOWN_BASIS_VALUES: tuple[str, ...] = (
    "ledger_per_turn",
    "rate_card_single_model",
    "unattributable",
    "unpriced",
    "unseparable",
    "none",
)
"""How a *bucket's* dollars were arrived at -- a different question from
``cost_provenance``, which answers it for the session total only.

The two are routinely different and the difference is the whole point. A
session with a run ledger carries a total summed from each recorded call; the
split of that total across buckets is a second and coarser derivation, because
nothing records tokens per bucket per call -- and no vendor itemises a bill
that way either. Reporting one word for both lets ``cost_provenance`` vouch for
a split it never described.

``ledger_per_turn``        each recorded call priced at its own model's rate
                           card, the four buckets then rescaled to the
                           recorded total.
``rate_card_single_model`` one model in the session, one rate card, rescaled.
``unattributable``         several models and no per-turn record: no split
                           exists, so the buckets carry no cost.
``unpriced``               the session total itself has no price.
``unseparable``            per-bucket only: these tokens are folded into the
                           others and cannot be pulled back out.
``none``                   nothing was recorded for this session.

Only the first two ever carry dollars, and both of them are *derived*.
"""

_MAX_AMBIGUOUS_CANDIDATES = 8
_MAX_TOP_TOOLS = 5

_UNPRICED_REASON: dict[str, str] = {
    "self_hosted_unpriced": "local or self-hosted model with no rate card",
    "enterprise_allocated": "subscription seat: the vendor never bills this usage per token",
    "unknown": "the model id could not be resolved to a rate card",
}


def candidate_session_ids(root: Path) -> list[str]:
    """Return every session id under *root*, cheaply.

    Reads directory names and one indexed column; no ``run.json`` is parsed and
    no report is built, because this only has to answer "which ids exist".
    """

    from lemoncrow.infra.runtime.session_report import list_run_files

    root = Path(root)
    ids: set[str] = set()
    try:
        for run_path in list_run_files(root):
            name = run_path.parent.name
            if name:
                ids.add(name)
    except OSError:
        pass
    try:
        from lemoncrow.core.foundation.history_store import HistoryStore

        store = HistoryStore(root)
        if store.db_path.exists():
            for row in store.token_rows():
                sid = str(row.get("session_id") or row.get("id") or "").strip()
                if sid:
                    ids.add(sid)
    except Exception:
        pass
    return sorted(ids)


def resolve_run_id(root: Path, run_id: str) -> str:
    """Resolve *run_id* -- an exact session id or a unique prefix -- to an id.

    Raises ``ValueError`` when nothing matches, and when a prefix matches more
    than one session it names the candidates: silently picking one would
    attribute a cost report to the wrong session.
    """

    wanted = str(run_id or "").strip()
    if not wanted:
        raise ValueError("a run id is required")
    ids = candidate_session_ids(root)
    if wanted in ids:
        return wanted
    matches = [sid for sid in ids if sid.startswith(wanted)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        shown = ", ".join(matches[:_MAX_AMBIGUOUS_CANDIDATES])
        more = (
            ""
            if len(matches) <= _MAX_AMBIGUOUS_CANDIDATES
            else f" (and {len(matches) - _MAX_AMBIGUOUS_CANDIDATES} more)"
        )
        raise ValueError(f"run id {wanted!r} is ambiguous, it matches {len(matches)} sessions: {shown}{more}")
    raise ValueError(f"no run found for {wanted!r}")


def _bucket_costs(row: UsageRow) -> dict[str, float] | None:
    """Return per-bucket dollars for *row* off its own rate card, or ``None``.

    The no-ledger fallback only. It prices every one of *row*'s tokens at
    ``row.model``, so it is honest exactly when the session used one model;
    :func:`_session_bucket_costs` is what decides that, and never calls this
    for a session where a second rate card was in play.

    Scaled so the buckets sum to the row's own ``cost_usd``: when that total
    came from a ledger's per-call accounting the rate card's buckets are a
    shape, not the amount, and printing bucket figures that do not add up to
    the total is how a cost report loses a reader's trust.
    """

    if row.cost_usd is None:
        return None

    from lemoncrow.core.capabilities.pricing import get_model_pricing

    raw = get_model_pricing(row.model).cost_breakdown_usd(
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_read_tokens=row.cache_read_tokens,
        cache_write_tokens=row.cache_write_tokens,
        cache_write_1h_tokens=row.cache_write_1h_tokens,
        thinking_tokens=row.thinking_tokens,
    )
    mapped = {
        "fresh_input": raw.get("input", 0.0),
        "cache_read": raw.get("cache_read", 0.0),
        "cache_write": raw.get("cache_write", 0.0),
        "output": raw.get("output", 0.0),
        "thinking": raw.get("thinking", 0.0),
    }
    bucket_sum = sum(mapped.values())
    if bucket_sum <= 0:
        return dict.fromkeys(mapped, 0.0)
    ratio = row.cost_usd / bucket_sum
    return {name: round(value * ratio, 6) for name, value in mapped.items()}


def _bucket_costs_from_report(report: SessionReport, total_cost: float) -> dict[str, float]:
    """Return per-bucket dollars from the ledger's own per-turn accounting.

    ``build_report`` prices each recorded call at *that call's* model and then
    rescales the four buckets to the session's recorded total. Those figures are
    what ``lc session report`` prints, so reading them back is what makes the
    two surfaces agree; recomputing the same thing differently is how they
    stopped agreeing.

    The rescale to *total_cost* is a no-op (ratio 1.0 to within a rounding ulp)
    whenever the ledger carried a recorded total, because both surfaces are then
    dividing the same dollar. It only bites when the ledger recorded no total
    and each surface estimated its own, and it preserves this payload's
    invariant -- buckets sum to ``total_cost_usd``.

    ``thinking`` is ``0.0``, not ``None``: a run ledger records no thinking
    tokens on any call, so this is a measured zero rather than an unknown.
    """

    mapped = {
        "fresh_input": float(report.input_token_cost_usd),
        "cache_read": float(report.cache_read_cost_usd),
        "cache_write": float(report.cache_write_cost_usd),
        "output": float(report.output_token_cost_usd),
        "thinking": 0.0,
    }
    bucket_sum = sum(mapped.values())
    if bucket_sum <= 0:
        return dict.fromkeys(mapped, 0.0)
    ratio = total_cost / bucket_sum
    return {name: round(value * ratio, 6) for name, value in mapped.items()}


def _session_bucket_costs(
    report: SessionReport | None,
    priced_rows: list[UsageRow],
    models: list[str],
    total_cost: float,
) -> dict[str, float] | None:
    """Return the session's per-bucket dollars, or ``None`` when unattributable.

    Order matters and is the whole point. The ledger's per-turn accounting wins
    whenever it exists. Without it, a single-model session can still be priced
    off its one rate card. A multi-model session without it cannot be split at
    all -- nothing records which turn ran on which model -- so it returns
    ``None`` rather than charging one model's tokens at another's rates.
    """

    if report is not None:
        return _bucket_costs_from_report(report, total_cost)
    if len(models) > 1:
        return None
    summed: dict[str, float] = {}
    for row in priced_rows:
        per_row = _bucket_costs(row)
        if per_row is None:
            continue
        for name, value in per_row.items():
            summed[name] = round(summed.get(name, 0.0) + value, 6)
    return summed


def _dominant(rows: list[UsageRow]) -> UsageRow:
    """Return the row that best represents the session (most tokens)."""

    return max(rows, key=lambda r: (r.total_tokens, r.model))


def _load_report(root: Path, session_id: str) -> SessionReport | None:
    """Return the run ledger's report for *session_id*, or ``None``.

    One load serves both the per-tool table and the cost buckets: they are two
    views of the same ledger, and reading it twice only invites them to drift.
    Every failure degrades to ``None``.
    """

    from lemoncrow.infra.runtime.session_report import load_report

    try:
        return load_report(session_id, root, include_carry_credit=False)
    except Exception:
        return None


def _top_tools(report: SessionReport | None) -> list[dict[str, Any]]:
    """Return the top tools by cost recorded in *report*."""

    if report is None:
        return []
    tools: list[dict[str, Any]] = []
    for name, calls, cost in report.top_tools_by_cost[:_MAX_TOP_TOOLS]:
        tools.append({"name": str(name), "calls": int(calls), "cost_usd": round(float(cost), 6)})
    return tools


def explain_run(root: Path, run_id: str) -> dict[str, Any]:
    """Return the cost/token decomposition for one session.

    *run_id* is an exact session id or a unique prefix. Raises ``ValueError``
    for an unknown or ambiguous id -- the one place this package raises, because
    there is no honest answer to "explain a session I cannot identify".
    """

    from lemoncrow.core.capabilities.savings_summary import _fmt_tok, _fmt_usd

    root = Path(root)
    session_id = resolve_run_id(root, run_id)
    rows = collect_rows_for_session(root, session_id)
    notes: list[str] = []

    if not rows:
        # Resolvable id, no usage recorded against it. A value, not a raise.
        notes.append("no usage was recorded for this session")
        return {
            "schema_version": EXPLAIN_SCHEMA_VERSION,
            "session_id": session_id,
            "host": "unknown",
            "model": "",
            "models": [],
            "project": "unknown",
            "total_cost_usd": None,
            "cost_provenance": "unknown",
            "breakdown_basis": "none",
            "breakdown_basis_approximate": False,
            "total_tokens": 0,
            "duration_seconds": 0.0,
            "breakdown": _empty_breakdown(),
            "top_tools": [],
            "notes": notes,
        }

    lead = _dominant(rows)
    models = sorted({row.model for row in rows if row.model})

    tokens: dict[str, int] = {
        "fresh_input": sum(r.input_tokens for r in rows),
        "cache_read": sum(r.cache_read_tokens for r in rows),
        "cache_write": sum(r.cache_write_tokens for r in rows),
        "output": sum(r.output_tokens for r in rows),
        "thinking": sum(r.thinking_tokens for r in rows),
        "subagents": 0,
        "tool_results": 0,
    }
    total_tokens = sum(r.total_tokens for r in rows)

    # One ledger read serves the buckets and the per-tool table both.
    report = _load_report(root, session_id)
    ledger_present = report is not None

    priced_rows = [r for r in rows if r.cost_usd is not None]
    total_cost: float | None = round(sum(float(r.cost_usd or 0.0) for r in priced_rows), 6) if priced_rows else None

    costs: dict[str, float | None] = dict.fromkeys(BREAKDOWN_BUCKETS, None)
    unattributable = False
    if total_cost is not None:
        priced = _session_bucket_costs(report, priced_rows, models, total_cost)
        if priced is None:
            unattributable = True
        else:
            for name in BREAKDOWN_BUCKETS:
                if name not in _UNSEPARABLE_BUCKETS:
                    costs[name] = priced.get(name, 0.0)

    basis = _breakdown_basis(
        total_cost=total_cost,
        unattributable=unattributable,
        ledger_present=ledger_present,
    )
    basis_approximate = any(r.pricing_approximate for r in rows)

    shares = _shares(tokens, total_tokens)
    breakdown: list[dict[str, Any]] = [
        {
            "bucket": name,
            "tokens": tokens[name],
            "cost_usd": costs[name],
            "share": shares[name],
            "basis": "unseparable" if name in _UNSEPARABLE_BUCKETS else basis,
        }
        for name in BREAKDOWN_BUCKETS
    ]
    breakdown.sort(key=lambda entry: (-float(entry["share"]), str(entry["bucket"])))

    # The basis word has to describe the dollars, not the tokens. ``lead`` is
    # the token-dominant row and may well be an unpriced one, which labelled a
    # rate-card total "local" (and reported ``self_hosted_unpriced`` on a
    # non-null cost). Whenever there is a total, its basis comes from the rows
    # that actually produced it -- the largest of them, so a mixed session is
    # described by the row carrying most of the money.
    provenance = lead.cost_provenance
    if priced_rows:
        provenance = max(priced_rows, key=lambda r: (float(r.cost_usd or 0.0), r.model)).cost_provenance

    if total_cost is None:
        reason = _UNPRICED_REASON.get(provenance, "no rate card resolved for this model")
        notes.append(f"cost is unpriced: {reason}")
        if total_tokens > 0:
            notes.append(f"{_fmt_tok(total_tokens)} tokens are recorded; only the price is unknown")
    elif len(priced_rows) < len(rows):
        notes.append(
            f"{len(rows) - len(priced_rows)} of {len(rows)} rows are unpriced; "
            f"{_fmt_usd(total_cost)} covers only the priced rows"
        )

    if unattributable:
        notes.append(
            f"per-bucket cost is unknown: {len(models)} models were used and no run ledger "
            "records which turn used which, so any split would mis-attribute the dollars"
        )

    if lead.subagent_count:
        notes.append(
            f"{lead.subagent_count} subagent delegations are folded into these totals; "
            "per-subagent cost is not recoverable"
        )
    else:
        notes.append("subagent usage is folded into this session's totals and is not separable")

    if len(models) > 1:
        notes.append(f"{len(models)} models used ({', '.join(models)}); tokens are split by call count")
    if any(r.pricing_approximate for r in rows):
        notes.append("pricing is approximate: rates come from a nearest sibling model or a proportional split")

    tools = _top_tools(report)
    if not ledger_present:
        notes.append("no run ledger for this session; per-tool cost is unavailable")

    return {
        "schema_version": EXPLAIN_SCHEMA_VERSION,
        "session_id": session_id,
        "host": lead.host,
        "model": lead.model,
        "models": models,
        "project": lead.project,
        "total_cost_usd": total_cost,
        "cost_provenance": provenance,
        "breakdown_basis": basis,
        "breakdown_basis_approximate": basis_approximate,
        "total_tokens": total_tokens,
        "duration_seconds": round(max(r.duration_seconds for r in rows), 3),
        "breakdown": breakdown,
        "top_tools": tools,
        "notes": notes,
    }


def _shares(tokens: dict[str, int], total_tokens: int) -> dict[str, float]:
    """Return per-bucket shares that sum to exactly 1.0 (or all zero).

    Rounding each share independently leaves a residual of up to one ulp per
    bucket, which is enough to break the "shares sum to one" property a
    decomposition is only useful for. The residual is folded into the largest
    bucket, where it is invisible, rather than left to accumulate.
    """

    if total_tokens <= 0:
        return dict.fromkeys(tokens, 0.0)
    shares = {name: round(count / total_tokens, 6) for name, count in tokens.items()}
    residual = round(1.0 - sum(shares.values()), 6)
    if residual:
        largest = max(tokens, key=lambda name: (tokens[name], name))
        shares[largest] = round(shares[largest] + residual, 6)
    return shares


def _breakdown_basis(*, total_cost: float | None, unattributable: bool, ledger_present: bool) -> str:
    """Return how the buckets got their dollars, independent of the total's basis.

    Mirrors the branch order in :func:`_session_bucket_costs` exactly, because
    the value's only job is to say which of those branches actually ran. The one
    thing it must never do is echo ``cost_provenance``: a per-call total whose
    split was derived a second time is precisely the case this field exists to
    separate.
    """

    if total_cost is None:
        return "unpriced"
    if unattributable:
        return "unattributable"
    return "ledger_per_turn" if ledger_present else "rate_card_single_model"


def _empty_breakdown() -> list[dict[str, Any]]:
    """Return the zeroed breakdown, so the payload shape never varies."""

    return [
        {"bucket": name, "tokens": 0, "cost_usd": None, "share": 0.0, "basis": "none"} for name in BREAKDOWN_BUCKETS
    ]


__all__ = [
    "BREAKDOWN_BASIS_VALUES",
    "BREAKDOWN_BUCKETS",
    "EXPLAIN_SCHEMA_VERSION",
    "candidate_session_ids",
    "explain_run",
    "resolve_run_id",
]
