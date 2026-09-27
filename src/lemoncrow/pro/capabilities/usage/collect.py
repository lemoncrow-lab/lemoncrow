"""Build :class:`UsageRow` records from the stores that already exist.

Why this module exists: usage is recorded in two places with different shapes
and different gaps. Run ledgers (``sessions/**/run.json``) have per-call cost
and tool activity but only know their host from the directory they live in.
Traces have the workspace path and the reasoning/cache-write counts but no
ledger. Neither is complete and both cover the same sessions, so every reader
that picked one got a different answer.

This is a *read* model. It adds no table, no migration and no write path: it
reads both sources, normalises them onto one row, and dedups on
``(session_id, model)`` with the run ledger winning. The savings sidecars
(``savings.jsonl`` / ``savings_aggregate.json``) are deliberately not read --
they are a savings ledger, and folding them in here double-counts.

The rule the whole module is built around: a model with no rate card yields
``cost_usd = None``. Not ``0.0``. ``get_model_pricing`` returns a zero-cost
sentinel with ``known=False`` for such a model, and every previous consumer
multiplied that zero by real tokens and reported a free session. :func:`derive_cost`
is the single place that decision is made, and it is public so it can be tested
without a store on disk.

Every failure here degrades to a value. A missing store, an unreadable
``run.json``, a schema-less database: all of them produce fewer rows, never an
exception.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.models import Trace
from lemoncrow.pro.capabilities.usage.models import (
    SELF_HOSTED_VENDOR_PREFIXES,
    SUBSCRIPTION_VENDOR_PREFIXES,
    UNKNOWN_STARTED_AT,
    USAGE_SCHEMA_VERSION,
    CostProvenance,
    UsageRow,
    provider_for_model,
    sum_token_components,
)

_TRACE_DETAIL_LIMIT = 2000
"""How many traces are pulled for host/workspace/cache detail. ``list_traces``
parses a full JSON payload per row, so this is bounded; ``token_rows`` (which
is a projection, not a payload parse) stays the unbounded enumerator."""

_FLAT_LAYOUT_SEGMENT = "sessions"
"""``sessions/<sid>/run.json`` (the legacy flat layout) has no host segment at
all -- ``parent.parent.name`` lands on ``sessions`` itself."""


# --------------------------------------------------------------------------- #
# Normalisation                                                                #
# --------------------------------------------------------------------------- #


def normalize_utc(value: datetime | None) -> datetime | None:
    """Return *value* as timezone-aware UTC, or ``None``.

    Writers disagree: five ``plugin_runtime`` writers use ``datetime.now(UTC)``
    while ``mcp_server.py:2833`` writes a naive ``datetime.utcnow()``. A naive
    value is therefore already UTC and is stamped as such rather than shifted;
    an aware value is converted.
    """

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def project_for_workspace(workspace_path: str | None) -> str:
    """Return the project label for *workspace_path*.

    There is no project/repo id outside Postgres, so the workspace basename is
    the only local anchor -- and it is ``NULL`` for every antigravity/hermes/pi
    import. Those rows land in an honest ``"unknown"`` bucket instead of being
    dropped from the totals.
    """

    raw = (workspace_path or "").strip()
    if not raw:
        return "unknown"
    return Path(raw).name or "unknown"


def host_from_run_path(run_path: Path) -> str:
    """Return the host that owns *run_path*.

    ``host`` is not a stored field anywhere -- it exists only as the
    ``sessions/YYYY/MM/DD/<host>/<sid>/`` path segment. An empty segment means
    the pre-host layout, which was Claude-only, so it reads ``"claude"`` (the
    rule ``savings_summary.py:3220-3222`` already uses). The flat
    ``sessions/<sid>/`` layout has no host segment to read at all, so it reads
    ``"unknown"`` rather than inheriting Claude's default.
    """

    segment = run_path.parent.parent.name
    if not segment:
        return "claude"
    if segment == _FLAT_LAYOUT_SEGMENT:
        return "unknown"
    return segment


# --------------------------------------------------------------------------- #
# Cost provenance -- the one rule this package exists to enforce               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CostAttribution:
    """The outcome of pricing one row: a figure *and* how it was arrived at."""

    cost_usd: float | None
    provenance: CostProvenance
    pricing_model_id: str | None = None
    approximate: bool = False


def _has_a_rate(pricing: Any) -> bool:
    """True when a rate card charges for *something*.

    An all-zero card is LiteLLM's placeholder for "self-hosted, nobody bills
    you", not a measured price -- see :func:`derive_cost`.
    """

    return any(
        float(getattr(pricing, axis, 0.0) or 0.0) > 0.0
        for axis in ("input", "output", "cache_read", "cache_write", "cache_write_1h", "thinking")
    ) or any(
        bool(getattr(pricing, tiers, ()) or ())
        for tiers in ("input_tiers", "output_tiers", "cache_read_tiers", "cache_write_tiers", "thinking_tiers")
    )


def derive_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    thinking_tokens: int = 0,
    recorded_total_usd: float | None = None,
) -> CostAttribution:
    """Price one row, or state honestly that it cannot be priced.

    Evaluated top-down, first match wins:

    1. *recorded_total_usd* is not ``None`` -- the ledger already recorded a
       total for this session, so that figure is used verbatim instead of
       being re-derived from the aggregate token counts.
    2. a subscription namespace (``copilot/``, ``cursor/``) -- the seat already
       paid for this usage and no per-call price exists, so none is invented.
    3. a self-hosted namespace with a *non-zero* rate card -- the user
       configured a price, so use it.
    4. a self-hosted namespace without one -- tokens are known, cost is not.
    5. any other model with a resolvable rate card -- the ordinary API estimate.
    6. anything else -- the id could not be resolved.

    Cases 2, 4 and 6 return ``cost_usd=None``. They must never return ``0.0``:
    ``get_model_pricing`` hands back a zero-rate sentinel for exactly these
    ids, and multiplying it by real tokens is what made local and Copilot
    sessions look free.

    That sentinel is *not* always ``known=False``. The shipped LiteLLM table
    carries ~29 ``ollama/*`` entries (``ollama/llama2``, ``ollama/codellama``,
    ``ollama/mixtral`` ...) with every rate set to ``0.0`` and ``known=True``,
    because nobody bills for a model you run yourself. Trusting ``known`` alone
    therefore reintroduces the exact bug on the exact ids R7 names. A
    self-hosted rate card that is zero on every axis is treated as *absent*:
    ``$0.00`` and "no rate card" are indistinguishable here, and R7 mandates
    the honest one.

    Case 1 is deliberately NOT ``provider_billed``. Every writer of a run
    ledger's ``cost.total_cost_usd`` -- ``CostTracker.snapshot`` for live runs,
    ``persist_imported_run_snapshot`` for imported ones -- fills it by summing
    this same LiteLLM rate card over the recorded calls, so no vendor figure
    reaches this function. The recorded total is still preferred (it is priced
    per call, which beats re-pricing a session aggregate) but it is reported
    for what it is: an estimate. Stamping it "billed" told a reader
    reconciling it against an Anthropic invoice that it *was* the invoice.
    """

    from lemoncrow.core.capabilities.pricing import get_model_pricing

    raw = (model or "").strip()
    vendor = raw.split("/", 1)[0].lower() if "/" in raw else ""
    pricing = get_model_pricing(raw)

    def _priced() -> float:
        # Tiers are a PER-REQUEST long-context premium: a vendor charges the
        # whole request at the premium rate once that request's own context
        # crosses the threshold, which is what ``request_cost_usd`` models.
        # ``cost_usd`` instead walks the tiers progressively, and these token
        # counts are a session aggregate over an unknown number of requests --
        # so pricing the sum through the tiers crosses a tier no single request
        # need ever have crossed (1M aggregate input tokens on
        # ``claude-sonnet-4-5`` priced $5.40 where the same tokens spread over
        # 20 sub-200k requests bill $3.00). With the request boundaries gone,
        # the base rate is the only defensible one, so the tiers are dropped.
        card = dataclasses.replace(
            pricing,
            input_tiers=(),
            output_tiers=(),
            cache_read_tiers=(),
            cache_write_tiers=(),
            thinking_tiers=(),
        )
        return round(
            card.cost_usd(
                input_tokens=max(0, input_tokens),
                output_tokens=max(0, output_tokens),
                cache_read_tokens=max(0, cache_read_tokens),
                cache_write_tokens=max(0, cache_write_tokens),
                cache_write_1h_tokens=max(0, cache_write_1h_tokens),
                thinking_tokens=max(0, thinking_tokens),
            ),
            6,
        )

    if recorded_total_usd is not None:
        return CostAttribution(
            cost_usd=round(float(recorded_total_usd), 6),
            provenance="api_estimated",
            pricing_model_id=pricing.model_id if pricing.known else None,
            approximate=pricing.approximate,
        )

    if vendor in SUBSCRIPTION_VENDOR_PREFIXES:
        return CostAttribution(cost_usd=None, provenance="enterprise_allocated")

    if vendor in SELF_HOSTED_VENDOR_PREFIXES:
        if pricing.known and _has_a_rate(pricing):
            return CostAttribution(
                cost_usd=_priced(),
                provenance="self_hosted_estimated",
                pricing_model_id=pricing.model_id,
                approximate=pricing.approximate,
            )
        return CostAttribution(cost_usd=None, provenance="self_hosted_unpriced")

    if pricing.known:
        return CostAttribution(
            cost_usd=_priced(),
            provenance="api_estimated",
            pricing_model_id=pricing.model_id,
            approximate=pricing.approximate,
        )

    return CostAttribution(cost_usd=None, provenance="unknown")


# --------------------------------------------------------------------------- #
# Proportional splitting for multi-model sessions                              #
# --------------------------------------------------------------------------- #


def _split_int(total: int, weights: Sequence[int]) -> list[int]:
    """Split *total* across *weights*, conserving the sum exactly.

    Largest-remainder, so ``_split_int(10, [3, 1])`` is ``[8, 2]`` and never
    ``[7, 2]`` -- a session's per-model rows must add back up to the session.
    """

    weight_sum = sum(weights)
    if not weights or weight_sum <= 0 or total <= 0:
        return [0] * len(weights)
    exact = [total * w / weight_sum for w in weights]
    floors = [int(value) for value in exact]
    remainder = total - sum(floors)
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - floors[i]), i))
    for index in order[:remainder]:
        floors[index] += 1
    return floors


def _split_float(total: float, weights: Sequence[int]) -> list[float]:
    """Split a dollar *total* across *weights*, conserving the sum to 6dp."""

    weight_sum = sum(weights)
    if not weights or weight_sum <= 0 or total <= 0:
        return [0.0] * len(weights)
    parts = [round(total * w / weight_sum, 6) for w in weights]
    parts[-1] = round(total - sum(parts[:-1]), 6)
    return parts


# --------------------------------------------------------------------------- #
# Source 1 -- run ledgers                                                      #
# --------------------------------------------------------------------------- #


def _model_weights(models_used: dict[str, int], started_model: str | None) -> list[tuple[str, int]]:
    """Return ``(model, call_count)`` pairs, deterministically ordered."""

    usable = {name: count for name, count in models_used.items() if str(name).strip()}
    if not usable:
        fallback = (started_model or "").strip()
        return [(fallback, 1)] if fallback else [("", 1)]
    return sorted(usable.items(), key=lambda item: (-item[1], item[0]))


def rows_from_run_file(run_path: Path, root: Path) -> list[UsageRow]:
    """Return every row recorded by one ``run.json``, or ``[]`` if unreadable.

    A session that used more than one model yields one row per model with the
    tokens (and any recorded bill) split by call count -- a single row would
    have to pick one model's rate card for another model's tokens. Split rows
    are flagged ``pricing_approximate`` because the split is by call count, not
    by measured tokens, which the ledger does not record per model.
    """

    from lemoncrow.infra.runtime.session_report import build_report

    try:
        snapshot: dict[str, Any] = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(snapshot, dict):
        return []
    try:
        # include_carry_credit=False is not a default to inherit: True costs a
        # per-session transcript scan, and this runs once per session on disk.
        report = build_report(snapshot, root, include_carry_credit=False)
    except Exception:
        return []

    session_id = run_path.parent.name or report.session_id
    if not session_id:
        return []
    host = host_from_run_path(run_path)
    workspace_path = str(snapshot.get("workspace_path") or "").strip() or None
    started_at = normalize_utc(report.started_at) or UNKNOWN_STARTED_AT
    ended_at = normalize_utc(report.ended_at)
    subagent_count = _subagent_count(report.telemetry)

    recorded_total: float | None = None
    if not report.cost_estimated and report.total_cost_usd > 0:
        recorded_total = float(report.total_cost_usd)

    pairs = _model_weights(report.models_used, report.started_model)
    weights = [count for _model, count in pairs]
    split = len(pairs) > 1

    inputs = _split_int(report.input_tokens, weights) if split else [report.input_tokens]
    outputs = _split_int(report.output_tokens, weights) if split else [report.output_tokens]
    cache_reads = _split_int(report.cache_read_tokens, weights) if split else [report.cache_read_tokens]
    cache_writes = _split_int(report.cache_write_tokens, weights) if split else [report.cache_write_tokens]
    bills = (
        _split_float(recorded_total, weights) if split and recorded_total is not None else [recorded_total] * len(pairs)
    )
    turns = _split_int(report.total_turns, weights) if split else [report.total_turns]
    tool_calls = _split_int(report.tool_call_count, weights) if split else [report.tool_call_count]

    rows: list[UsageRow] = []
    for index, (model, _count) in enumerate(pairs):
        attribution = derive_cost(
            model,
            input_tokens=inputs[index],
            output_tokens=outputs[index],
            cache_read_tokens=cache_reads[index],
            cache_write_tokens=cache_writes[index],
            recorded_total_usd=bills[index],
        )
        rows.append(
            UsageRow(
                schema_version=USAGE_SCHEMA_VERSION,
                session_id=session_id,
                host=host,
                provider=provider_for_model(model),
                model=model,
                project=project_for_workspace(workspace_path),
                workspace_path=workspace_path,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=float(report.duration_seconds),
                input_tokens=inputs[index],
                output_tokens=outputs[index],
                cache_read_tokens=cache_reads[index],
                cache_write_tokens=cache_writes[index],
                total_tokens=sum_token_components(
                    input_tokens=inputs[index],
                    output_tokens=outputs[index],
                    cache_read_tokens=cache_reads[index],
                    cache_write_tokens=cache_writes[index],
                ),
                cost_usd=attribution.cost_usd,
                cost_provenance=attribution.provenance,
                pricing_model_id=attribution.pricing_model_id,
                pricing_approximate=attribution.approximate or split,
                turns=turns[index],
                tool_calls=tool_calls[index],
                subagent_count=subagent_count if index == 0 else 0,
                source="run_ledger",
                source_path=str(run_path),
            )
        )
    return rows


def _subagent_count(telemetry: dict[str, Any]) -> int:
    """Return the recorded subagent count, or 0.

    Subagent usage is *already inside* this row: transcripts are folded into
    the parent session at import time and the totals summed, so this is a count
    of delegations, not a slice of cost that could be subtracted out. Only
    Claude records it (``telemetry["subagent_names"]``); everyone else is 0.
    """

    raw = telemetry.get("subagent_names") if telemetry else None
    if isinstance(raw, dict):
        total = 0
        for value in raw.values():
            try:
                total += int(value)
            except (TypeError, ValueError):
                continue
        return total
    if isinstance(raw, list):
        return len(raw)
    return 0


# --------------------------------------------------------------------------- #
# Source 2 -- traces                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _TraceFacts:
    """The slice of a ``Trace`` this read model needs."""

    trace_id: str
    session_id: str
    host: str
    model: str
    workspace_path: str | None
    created_at: datetime
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    thinking_tokens: int
    reasoning_output_tokens: int
    subagent_count: int


def _facts_from_trace(trace: Trace) -> _TraceFacts:
    return _TraceFacts(
        trace_id=trace.id,
        session_id=(trace.session_id or trace.id),
        host=(trace.host or "").strip() or "unknown",
        model=(trace.model or "").strip(),
        workspace_path=(trace.workspace_path or "").strip() or None,
        created_at=normalize_utc(trace.created_at) or UNKNOWN_STARTED_AT,
        input_tokens=int(trace.input_tokens or 0),
        output_tokens=int(trace.output_tokens or 0),
        # Token naming is normalised here: the trace schema's
        # cached_input_tokens / cache_creation_input_tokens are this model's
        # cache_read_tokens / cache_write_tokens.
        cache_read_tokens=int(trace.cached_input_tokens or 0),
        cache_write_tokens=int(trace.cache_creation_input_tokens or 0),
        thinking_tokens=int(trace.thinking_tokens or 0),
        reasoning_output_tokens=int(trace.reasoning_output_tokens or 0),
        subagent_count=_subagent_count(trace.telemetry),
    )


def load_trace_facts(root: Path, *, since: datetime | None = None) -> dict[str, _TraceFacts]:
    """Return trace detail keyed by session id (and by trace id as a fallback).

    A missing, locked or schema-less store is a gap, not a failure: the caller
    gets an empty mapping and the run ledgers carry the report alone.
    """

    index: dict[str, _TraceFacts] = {}
    try:
        from lemoncrow.core.foundation.history_store import HistoryStore

        store = HistoryStore(root)
        if not store.db_path.exists():
            return index
        traces = store.list_traces(since=since, limit=_TRACE_DETAIL_LIMIT)
    except Exception:
        return index
    for trace in traces:
        facts = _facts_from_trace(trace)
        index.setdefault(facts.session_id, facts)
        index.setdefault(facts.trace_id, facts)
    return index


def _token_rows(
    root: Path,
    *,
    since: datetime | None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the cheap per-trace token projection, or ``[]``."""

    try:
        from lemoncrow.core.foundation.history_store import HistoryStore

        store = HistoryStore(root)
        if not store.db_path.exists():
            return []
        return store.token_rows(since=since, session_id=session_id)
    except Exception:
        return []


def _row_from_trace_facts(facts: _TraceFacts) -> UsageRow:
    attribution = derive_cost(
        facts.model,
        input_tokens=facts.input_tokens,
        output_tokens=facts.output_tokens,
        cache_read_tokens=facts.cache_read_tokens,
        cache_write_tokens=facts.cache_write_tokens,
        thinking_tokens=facts.thinking_tokens,
    )
    return UsageRow(
        schema_version=USAGE_SCHEMA_VERSION,
        session_id=facts.session_id,
        host=facts.host,
        provider=provider_for_model(facts.model),
        model=facts.model,
        project=project_for_workspace(facts.workspace_path),
        workspace_path=facts.workspace_path,
        started_at=facts.created_at,
        input_tokens=facts.input_tokens,
        output_tokens=facts.output_tokens,
        cache_read_tokens=facts.cache_read_tokens,
        cache_write_tokens=facts.cache_write_tokens,
        thinking_tokens=facts.thinking_tokens,
        reasoning_output_tokens=facts.reasoning_output_tokens,
        total_tokens=sum_token_components(
            input_tokens=facts.input_tokens,
            output_tokens=facts.output_tokens,
            cache_read_tokens=facts.cache_read_tokens,
            cache_write_tokens=facts.cache_write_tokens,
            thinking_tokens=facts.thinking_tokens,
        ),
        cost_usd=attribution.cost_usd,
        cost_provenance=attribution.provenance,
        pricing_model_id=attribution.pricing_model_id,
        pricing_approximate=attribution.approximate,
        subagent_count=facts.subagent_count,
        source="trace",
        source_path=facts.trace_id or None,
    )


def _facts_from_token_row(row: dict[str, Any], detail: dict[str, _TraceFacts]) -> _TraceFacts | None:
    """Promote one ``token_rows`` projection to full facts.

    ``token_rows`` selects no ``created_at`` and no workspace, so the parsed
    trace is preferred whenever it is in the detail page. Outside it the row is
    still emitted -- its tokens are real -- with ``UNKNOWN_STARTED_AT`` standing
    in for the time the projection does not carry.

    Every *priced* axis the projection can carry is read through, including
    cache writes: dropping that one to a hardcoded zero deleted a $3.75/M axis
    from every row outside the detail page, which on the maintainer's store was
    367,428 cache-write tokens across 200 of 3,191 rows.
    """

    trace_id = str(row.get("id") or "").strip()
    session_id = str(row.get("session_id") or "").strip() or trace_id
    if not session_id:
        return None
    # A session can own multiple traces. Trace id is the exact row identity;
    # session id is only a metadata fallback. Looking up the session first made
    # every token row in a multi-trace session reuse the first trace's counts.
    known = detail.get(trace_id) or detail.get(session_id)
    if known is not None:
        return known

    def _int(key: str) -> int:
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return _TraceFacts(
        trace_id=trace_id,
        session_id=session_id,
        host=str(row.get("host") or "").strip() or "unknown",
        model=str(row.get("model") or "").strip(),
        workspace_path=None,
        created_at=UNKNOWN_STARTED_AT,
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cache_read_tokens=_int("cached_input_tokens"),
        cache_write_tokens=_int("cache_creation_input_tokens"),
        thinking_tokens=_int("thinking_tokens"),
        reasoning_output_tokens=0,
        subagent_count=0,
    )


def _backfill(row: UsageRow, facts: _TraceFacts) -> UsageRow:
    """Fill a run-ledger row's gaps from a trace. The ledger always wins."""

    changes: dict[str, Any] = {}
    if row.workspace_path is None and facts.workspace_path:
        changes["workspace_path"] = facts.workspace_path
        changes["project"] = project_for_workspace(facts.workspace_path)
    if row.host == "unknown" and facts.host != "unknown":
        changes["host"] = facts.host
    if row.started_at == UNKNOWN_STARTED_AT and facts.created_at != UNKNOWN_STARTED_AT:
        changes["started_at"] = facts.created_at
    if row.subagent_count == 0 and facts.subagent_count:
        changes["subagent_count"] = facts.subagent_count
    if not changes:
        return row
    return dataclasses.replace(row, **changes)


def _merge_trace_rows(existing: UsageRow, row: UsageRow) -> UsageRow:
    """Fold two trace-derived rows for the same session/model into one row."""

    input_tokens = existing.input_tokens + row.input_tokens
    output_tokens = existing.output_tokens + row.output_tokens
    cache_read_tokens = existing.cache_read_tokens + row.cache_read_tokens
    cache_write_tokens = existing.cache_write_tokens + row.cache_write_tokens
    thinking_tokens = existing.thinking_tokens + row.thinking_tokens
    reasoning_output_tokens = existing.reasoning_output_tokens + row.reasoning_output_tokens
    attribution = derive_cost(
        existing.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        thinking_tokens=thinking_tokens,
    )
    return dataclasses.replace(
        existing,
        started_at=max(existing.started_at, row.started_at),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        thinking_tokens=thinking_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        total_tokens=sum_token_components(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            thinking_tokens=thinking_tokens,
        ),
        cost_usd=attribution.cost_usd,
        cost_provenance=attribution.provenance,
        pricing_model_id=attribution.pricing_model_id,
        pricing_approximate=attribution.approximate,
        subagent_count=existing.subagent_count + row.subagent_count,
    )


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #


def collect_usage_rows(
    root: Path,
    *,
    since: datetime | None = None,
    host: str | None = None,
    model: str | None = None,
    project: str | None = None,
    limit: int | None = None,
) -> list[UsageRow]:
    """Return the canonical usage rows under *root*, newest first.

    Run ledgers are primary; traces fill the fields a ledger cannot carry
    (workspace path, cache-write and reasoning counts) and supply whole rows
    for sessions that were imported without one. Deduplication is on
    ``(session_id, model)`` and the ledger row always wins.

    *host* and *project* match case-insensitively and exactly; *model* matches
    case-insensitively as a substring, so ``--model opus`` is useful without
    the caller having to know a full vendor-prefixed id. *limit* is applied
    after sorting, so it always returns the newest N.
    """

    from lemoncrow.infra.runtime.session_report import list_run_files

    root = Path(root)
    since_utc = normalize_utc(since)

    by_key: dict[tuple[str, str], UsageRow] = {}
    try:
        run_files = list_run_files(root, since=since_utc)
    except OSError:
        run_files = []
    for run_path in run_files:
        for row in rows_from_run_file(run_path, root):
            by_key.setdefault((row.session_id, row.model), row)

    detail = load_trace_facts(root, since=since_utc)
    ledger_sessions = {session_id for session_id, _model in by_key}

    # Backfill first: a ledger row missing a workspace path is the common case
    # (imported run.json omits it) and the trace is the only place it exists.
    for key, row in list(by_key.items()):
        facts = detail.get(row.session_id) or (detail.get(row.source_path or "") if row.source_path else None)
        if facts is not None:
            by_key[key] = _backfill(row, facts)

    for token_row in _token_rows(root, since=since_utc):
        facts = _facts_from_token_row(token_row, detail)
        if facts is None or facts.session_id in ledger_sessions:
            continue
        key = (facts.session_id, facts.model)
        row = _row_from_trace_facts(facts)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = row
            continue
        # Multiple traces may belong to one host session/model. Keep one
        # session-shaped row and aggregate every trace into it.
        by_key[key] = _merge_trace_rows(existing, row)

    rows = _filter(by_key.values(), since=since_utc, host=host, model=model, project=project)
    rows.sort(key=lambda r: (-r.started_at.timestamp(), r.session_id, r.model))
    if limit is not None and limit >= 0:
        return rows[:limit]
    return rows


def collect_rows_for_session(root: Path, session_id: str) -> list[UsageRow]:
    """Return canonical usage rows for exactly one session.

    The ledger lookup stays O(1) by session id. When there is no ledger, the
    SQLite token projection is filtered by session id so every trace in that
    session is included without walking unrelated session directories.
    """

    from lemoncrow.core.foundation.paths import find_session_dir, flat_session_dir

    root = Path(root)
    wanted = str(session_id).strip()
    if not wanted:
        return []

    rows: list[UsageRow] = []
    session_root = find_session_dir(root, wanted) or flat_session_dir(root, wanted)
    if session_root is not None:
        run_path = session_root / "run.json"
        if run_path.exists():
            rows = rows_from_run_file(run_path, root)

    detail = load_trace_facts(root)
    facts = detail.get(wanted)
    if rows:
        if facts is not None:
            rows = [_backfill(row, facts) for row in rows]
        return rows

    by_model: dict[tuple[str, str], UsageRow] = {}
    for token_row in _token_rows(root, since=None, session_id=wanted):
        trace_facts = _facts_from_token_row(token_row, detail)
        if trace_facts is None:
            continue
        key = (trace_facts.session_id, trace_facts.model)
        row = _row_from_trace_facts(trace_facts)
        existing = by_model.get(key)
        by_model[key] = row if existing is None else _merge_trace_rows(existing, row)
    if by_model:
        result = list(by_model.values())
        result.sort(key=lambda row: (-row.started_at.timestamp(), row.session_id, row.model))
        return result

    if facts is not None:
        return [_row_from_trace_facts(facts)]
    return []


def _filter(
    rows: Iterable[UsageRow],
    *,
    since: datetime | None,
    host: str | None,
    model: str | None,
    project: str | None,
) -> list[UsageRow]:
    wanted_host = (host or "").strip().lower()
    wanted_model = (model or "").strip().lower()
    wanted_project = (project or "").strip().lower()
    kept: list[UsageRow] = []
    for row in rows:
        # A row whose source records tokens but no time keeps its place: the
        # store already applied the window, only the exact instant is missing.
        if since is not None and row.started_at != UNKNOWN_STARTED_AT and row.started_at < since:
            continue
        if wanted_host and row.host.lower() != wanted_host:
            continue
        if wanted_model and wanted_model not in row.model.lower():
            continue
        if wanted_project and row.project.lower() != wanted_project:
            continue
        kept.append(row)
    return kept


__all__ = [
    "CostAttribution",
    "collect_rows_for_session",
    "collect_usage_rows",
    "derive_cost",
    "host_from_run_path",
    "normalize_utc",
    "project_for_workspace",
    "rows_from_run_file",
]
