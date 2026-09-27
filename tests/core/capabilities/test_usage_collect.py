"""Canonical usage rows: identity, normalisation and -- above all -- pricing honesty.

The rule this file exists to protect: a model with no rate card must report
``cost_usd is None``, never ``0.0``. ``get_model_pricing`` hands back a
zero-rate sentinel with ``known=False`` for every local, self-hosted and
subscription model, so anything that multiplies it by real tokens reports a
busy session as free. ``test_unpriced_model_never_reports_zero_cost`` is the
regression guard and covers every prefix that hits that path.

The rest of the file guards the four substrate gaps the read model papers over:
host and project are path/trace-derived rather than stored, timestamps arrive
naive from some writers, subagent usage is folded into the parent and cannot be
split back out, and the same session is recorded in two stores at once.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation.history_store import HistoryStore
from lemoncrow.core.foundation.models import Trace
from lemoncrow.pro.capabilities.usage.collect import (
    collect_rows_for_session,
    collect_usage_rows,
    derive_cost,
    host_from_run_path,
    normalize_utc,
    project_for_workspace,
)
from lemoncrow.pro.capabilities.usage.models import (
    SELF_HOSTED_VENDOR_PREFIXES,
    SUBSCRIPTION_VENDOR_PREFIXES,
    UNKNOWN_STARTED_AT,
    UsageRow,
    sum_token_components,
)

_START = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

Call = tuple[str, int, int, int, int]
"""``(model, input, output, cache_read, cache_write)`` for one recorded call."""


def _call(model: str, *, inp: int = 1000, out: int = 200, cache_read: int = 0, cache_write: int = 0) -> Call:
    return (model, inp, out, cache_read, cache_write)


def _write_run(
    root: Path,
    *,
    session_id: str,
    host: str = "claude",
    day: tuple[str, str, str] = ("2026", "09", "01"),
    calls: Sequence[Call] = (),
    total_cost_usd: float = 0.0,
    workspace: str | None = "/home/dev/myproject",
    telemetry: dict[str, Any] | None = None,
    tool_call_count: int = 0,
    started_at: str = "2026-09-01T10:00:00+00:00",
    flat: bool = False,
    tool_events: Sequence[tuple[str, str, int]] = (),
) -> Path:
    """Seed one ``run.json`` in the canonical (or legacy flat) layout."""

    snapshot: dict[str, Any] = {
        "session_id": session_id,
        "status": "completed",
        "created_at": started_at,
        "updated_at": started_at,
        "tool_call_count": tool_call_count,
        "telemetry": dict(telemetry or {}),
        "cost": {
            "total_cost_usd": total_cost_usd,
            "calls": [
                {
                    "model": model,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                }
                for model, inp, out, cache_read, cache_write in calls
            ],
        },
        "events": [
            {
                "kind": "model_recommendation",
                "at": started_at,
                "payload": {"tool_name": name, "model": model, "estimated_input_tokens": tokens},
            }
            for name, model, tokens in tool_events
        ],
    }
    # An imported run.json omits workspace_path entirely -- that is the gap the
    # trace backfill exists for, so model it by omitting the key, not nulling it.
    if workspace is not None:
        snapshot["workspace_path"] = workspace

    if flat:
        target = root / "sessions" / session_id
    else:
        year, month, dayname = day
        target = root / "sessions" / year / month / dayname / host / session_id
    target.mkdir(parents=True, exist_ok=True)
    run_path = target / "run.json"
    run_path.write_text(json.dumps(snapshot), encoding="utf-8")
    return run_path


def _seed_trace(root: Path, trace: Trace) -> None:
    store = HistoryStore(root)
    store.init()
    store.record_trace(trace)


def _trace(
    *,
    trace_id: str,
    session_id: str,
    model: str = "claude-opus-5",
    host: str = "codex",
    workspace: str | None = "/home/dev/otherproject",
    created_at: datetime | None = None,
    input_tokens: int = 500,
    output_tokens: int = 100,
    cached_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    thinking_tokens: int = 0,
    reasoning_output_tokens: int = 0,
) -> Trace:
    return Trace(
        id=trace_id,
        session_id=session_id,
        agent="agent",
        domain="code",
        task="do the thing",
        status="success",
        host=host,
        model=model,
        workspace_path=workspace,
        created_at=created_at or _START,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        thinking_tokens=thinking_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
    )


def _only(rows: Sequence[UsageRow]) -> UsageRow:
    assert len(rows) == 1, [f"{r.session_id}/{r.model}" for r in rows]
    return rows[0]


# --------------------------------------------------------------------------- #
# Identity: host and project are derived, not stored                           #
# --------------------------------------------------------------------------- #


def test_row_from_run_ledger_has_host_from_path(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="abc", host="codex", calls=[_call("claude-opus-5")])
    row = _only(collect_usage_rows(tmp_path))
    assert row.host == "codex"
    assert row.session_id == "abc"
    assert row.source == "run_ledger"


def test_flat_layout_host_is_unknown_not_claude(tmp_path: Path) -> None:
    """The legacy flat layout has no host segment; inventing "claude" would be a guess."""

    run_path = _write_run(tmp_path, session_id="flat1", calls=[_call("claude-opus-5")], flat=True)
    assert host_from_run_path(run_path) == "unknown"
    assert _only(collect_usage_rows(tmp_path)).host == "unknown"


def test_missing_host_segment_defaults_to_claude() -> None:
    """A path with no segment to read falls back to the pre-host default."""

    assert host_from_run_path(Path("run.json")) == "claude"
    assert host_from_run_path(Path("/run.json")) == "claude"


def test_project_is_workspace_basename(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="p1", workspace="/home/dev/myproject", calls=[_call("claude-opus-5")])
    assert _only(collect_usage_rows(tmp_path)).project == "myproject"


def test_project_without_workspace_is_unknown_not_dropped(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="p2", workspace=None, calls=[_call("claude-opus-5")])
    row = _only(collect_usage_rows(tmp_path))
    assert row.workspace_path is None
    assert row.project == "unknown"
    assert project_for_workspace(None) == "unknown"


def test_workspace_is_backfilled_from_the_trace(tmp_path: Path) -> None:
    """An imported run.json has no workspace_path; the trace is the only anchor."""

    _write_run(tmp_path, session_id="s9", workspace=None, calls=[_call("claude-opus-5")])
    _seed_trace(tmp_path, _trace(trace_id="t9", session_id="s9", workspace="/home/dev/backfilled"))
    row = _only(collect_usage_rows(tmp_path))
    assert row.source == "run_ledger"
    assert row.project == "backfilled"
    assert row.workspace_path == "/home/dev/backfilled"


def test_provider_is_derived_from_the_model_namespace(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="pr1", calls=[_call("ollama/qwen2.5-coder")])
    assert _only(collect_usage_rows(tmp_path)).provider == "ollama"


# --------------------------------------------------------------------------- #
# Cost provenance -- the single most important rule in this package            #
# --------------------------------------------------------------------------- #


def test_local_model_is_unpriced_not_zero(tmp_path: Path) -> None:
    """Regression guard for the "$0 with known=False" bug."""

    _write_run(tmp_path, session_id="loc", calls=[_call("ollama/qwen2.5-coder", inp=50_000, out=8_000)])
    row = _only(collect_usage_rows(tmp_path))

    assert row.cost_provenance == "self_hosted_unpriced"
    assert row.cost_usd is None
    # Explicit: None is not 0.0, and 0.0 is exactly the wrong answer here.
    assert row.cost_usd != 0.0
    assert row.pricing_model_id is None
    # The tokens are real and must survive: only the price is unknown.
    assert row.input_tokens == 50_000
    assert row.total_tokens == 58_000


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        *[(f"{prefix}/some-model-x", "self_hosted_unpriced") for prefix in SELF_HOSTED_VENDOR_PREFIXES],
        *[(f"{prefix}/gpt-5", "enterprise_allocated") for prefix in SUBSCRIPTION_VENDOR_PREFIXES],
        ("<synthetic>", "unknown"),
        ("", "unknown"),
    ],
)
def test_unpriced_model_never_reports_zero_cost(tmp_path: Path, model: str, expected: str) -> None:
    """No unpriceable id may ever produce a dollar figure -- including 0.0."""

    attribution = derive_cost(model, input_tokens=120_000, output_tokens=20_000)
    assert attribution.provenance == expected
    assert attribution.cost_usd is None
    assert attribution.cost_usd != 0.0

    _write_run(tmp_path, session_id="u1", calls=[_call(model or "unknown", inp=120_000, out=20_000)])
    if model:
        row = _only(collect_usage_rows(tmp_path))
        assert row.cost_usd is None
        assert row.cost_provenance == expected


def test_self_hosted_id_in_the_pricing_table_is_still_unpriced(tmp_path: Path) -> None:
    """The R7 bug, on the ids R7 actually names.

    ``test_unpriced_model_never_reports_zero_cost`` only ever asks about
    ``<prefix>/some-model-x``, which no rate table has heard of, so it passes
    through the ``known=False`` door. The shipped LiteLLM table *does* carry
    real ``ollama/*`` ids with ``known=True`` and every rate ``0.0`` -- those
    took the priced branch and rendered a busy local session as ``$0.00``.
    """

    from lemoncrow.core.capabilities.pricing import get_model_pricing

    # Guard the premise, so this test fails loudly rather than vacuously if the
    # vendored table ever stops carrying zero-rate self-hosted entries.
    priced_in_table = get_model_pricing("ollama/llama2")
    assert priced_in_table.known, "premise gone: ollama/llama2 is no longer in the pricing table"
    assert priced_in_table.input == 0.0

    for model in ("ollama/llama2", "ollama/codellama", "ollama/llama3"):
        attribution = derive_cost(model, input_tokens=250_000, output_tokens=40_000)
        assert attribution.provenance == "self_hosted_unpriced", model
        assert attribution.cost_usd is None, model
        assert attribution.cost_usd != 0.0, model
        assert attribution.pricing_model_id is None, model

    _write_run(tmp_path, session_id="oll", calls=[_call("ollama/llama2", inp=250_000, out=40_000)])
    row = _only(collect_usage_rows(tmp_path))
    assert row.cost_usd is None
    assert row.cost_provenance == "self_hosted_unpriced"
    assert row.total_tokens == 290_000  # tokens are real; only the price is unknown


def test_subscription_model_is_enterprise_allocated(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="cp", calls=[_call("copilot/gpt-5", inp=9_000, out=1_500)])
    row = _only(collect_usage_rows(tmp_path))
    assert row.cost_provenance == "enterprise_allocated"
    assert row.cost_usd is None


def test_known_model_is_api_estimated(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="api", calls=[_call("claude-opus-5", inp=10_000, out=2_000)])
    row = _only(collect_usage_rows(tmp_path))
    assert row.cost_provenance == "api_estimated"
    assert row.cost_usd is not None
    assert row.cost_usd > 0
    assert row.pricing_model_id == "claude-opus-5"
    assert row.pricing_approximate is False


def test_approximate_pricing_is_flagged(tmp_path: Path) -> None:
    """First production reader of ``ModelPricing.approximate`` -- dead data before this."""

    _write_run(tmp_path, session_id="apx", calls=[_call("claude-opus-9-2-turbo", inp=10_000, out=2_000)])
    row = _only(collect_usage_rows(tmp_path))
    assert row.pricing_approximate is True
    assert row.pricing_model_id == "claude-opus-5"
    assert row.cost_provenance == "api_estimated"


def test_recorded_ledger_total_wins_the_value_but_is_never_called_billed(tmp_path: Path) -> None:
    """The ledger's total is LemonCrow's own rate-card sum, not a vendor figure.

    ``run.json``'s ``cost.total_cost_usd`` is written by ``CostTracker.snapshot``
    for live runs and by ``persist_imported_run_snapshot`` for imported ones,
    and both fill it by summing ``usage_cost_usd`` -- this same LiteLLM rate
    card -- over the recorded calls. Stamping it ``provider_billed`` made
    ``lc usage`` print the BASIS column as "billed", so a user reconciling
    $11,052.59 against an Anthropic invoice was told it *was* the invoice.

    The recorded figure still wins the *value* (it is priced per call, which
    beats re-pricing a session aggregate); only the claim about it changed.
    """

    from lemoncrow.pro.capabilities.usage.aggregate import totals

    _write_run(
        tmp_path,
        session_id="bill",
        calls=[_call("claude-opus-5", inp=10_000, out=2_000)],
        total_cost_usd=4.2,
    )
    row = _only(collect_usage_rows(tmp_path))
    assert row.cost_usd == pytest.approx(4.2)
    assert row.cost_provenance == "api_estimated"
    assert row.cost_provenance != "provider_billed"

    # The money is unchanged; only the column it lands in moved, which is what
    # turns the rendered basis word from "billed" into "estimated".
    total = totals([row])
    assert total.total_usd == pytest.approx(4.2)
    assert total.billed_usd == 0.0
    assert total.estimated_usd == pytest.approx(4.2)


def test_aggregate_tokens_are_not_priced_through_the_long_context_tiers() -> None:
    """A session's SUMMED tokens must not cross a tier no single request crossed.

    The ``*_above_200k_tokens`` rates are a PER-REQUEST long-context premium --
    a vendor charges the whole request at the premium rate once that request's
    own context crosses the threshold, which is what
    ``ModelPricing.request_cost_usd`` models. ``ModelPricing.cost_usd`` instead
    walks the tiers progressively, and a row here carries a session aggregate
    over an unknown number of requests: 1M input tokens on
    ``claude-sonnet-4-5`` came out at $5.40 (200k at $3/M, 800k at $6/M) where
    the same tokens spread over 20 sub-200k requests bill $3.00.
    """

    from lemoncrow.core.capabilities.pricing import get_model_pricing

    card = get_model_pricing("claude-sonnet-4-5")
    assert card.input_tiers, "fixture model must be tiered or this proves nothing"

    attribution = derive_cost("claude-sonnet-4-5", input_tokens=1_000_000)

    assert attribution.cost_usd == pytest.approx(1_000_000 * card.input / 1_000_000)
    # ...and NOT the progressive walk over the sum, which is what it used to be.
    assert attribution.cost_usd != pytest.approx(card.cost_usd(input_tokens=1_000_000))


def test_tierless_models_are_priced_exactly_as_before() -> None:
    """Dropping the tiers must not move a model that has none."""

    from lemoncrow.core.capabilities.pricing import usage_cost_usd

    attribution = derive_cost("gpt-5", input_tokens=250_000, output_tokens=40_000)
    assert attribution.cost_usd == pytest.approx(
        round(usage_cost_usd("gpt-5", input_tokens=250_000, output_tokens=40_000), 6)
    )


def test_self_hosted_with_a_rate_card_is_estimated_not_unpriced() -> None:
    """``ollama/gpt-5`` resolves through alias stripping, so it *can* be priced."""

    attribution = derive_cost("ollama/gpt-5", input_tokens=1_000_000)
    assert attribution.provenance == "self_hosted_estimated"
    assert attribution.cost_usd is not None
    assert attribution.cost_usd > 0


# --------------------------------------------------------------------------- #
# Timestamps (R9)                                                              #
# --------------------------------------------------------------------------- #


def test_timestamps_are_timezone_aware(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="tz1",
        calls=[_call("claude-opus-5")],
        started_at="2026-09-01T10:00:00",  # naive, as mcp_server's utcnow() writes
    )
    _seed_trace(
        tmp_path,
        _trace(trace_id="tz2", session_id="tz2", created_at=datetime(2026, 9, 1, 9, 0)),
    )
    rows = collect_usage_rows(tmp_path)
    assert len(rows) == 2
    for row in rows:
        assert row.started_at.tzinfo is not None
        assert row.started_at.utcoffset() == timedelta(0)
        assert row.ended_at is None or row.ended_at.tzinfo is not None


def test_normalize_utc_stamps_naive_values_without_shifting_them() -> None:
    naive = datetime(2026, 9, 1, 10, 0)
    stamped = normalize_utc(naive)
    assert stamped == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    assert normalize_utc(None) is None


# --------------------------------------------------------------------------- #
# Multi-model, dedup and sources                                               #
# --------------------------------------------------------------------------- #


def test_multi_model_session_emits_one_row_per_model(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="multi",
        calls=[
            _call("claude-opus-5", inp=1000, out=100),
            _call("claude-opus-5", inp=1000, out=100),
            _call("claude-opus-5", inp=1000, out=100),
            _call("gpt-5", inp=1000, out=100),
        ],
    )
    rows = sorted(collect_usage_rows(tmp_path), key=lambda r: r.model)
    assert [r.model for r in rows] == ["claude-opus-5", "gpt-5"]
    assert [r.input_tokens for r in rows] == [3000, 1000]
    assert [r.output_tokens for r in rows] == [300, 100]
    assert all(r.pricing_approximate for r in rows)
    # The split must conserve the session's own totals exactly.
    assert sum(r.input_tokens for r in rows) == 4000


def test_multi_model_split_conserves_a_recorded_total(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="mbill",
        calls=[_call("claude-opus-5"), _call("claude-opus-5"), _call("claude-opus-5"), _call("gpt-5")],
        total_cost_usd=8.0,
    )
    rows = collect_usage_rows(tmp_path)
    assert all(r.cost_provenance == "api_estimated" for r in rows)
    assert sum(float(r.cost_usd or 0.0) for r in rows) == pytest.approx(8.0)


def test_trace_only_session_is_collected(tmp_path: Path) -> None:
    _seed_trace(tmp_path, _trace(trace_id="t1", session_id="s1", host="opencode"))
    row = _only(collect_usage_rows(tmp_path))
    assert row.source == "trace"
    assert row.session_id == "s1"
    assert row.host == "opencode"
    assert row.source_path == "t1"


def test_no_double_count_between_sources(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="dup", calls=[_call("claude-opus-5", inp=10_000, out=2_000)])
    _seed_trace(tmp_path, _trace(trace_id="tdup", session_id="dup"))
    rows = collect_usage_rows(tmp_path)
    keys = [(r.session_id, r.model) for r in rows]
    assert len(keys) == len(set(keys))
    row = _only(rows)
    assert row.source == "run_ledger"
    assert row.input_tokens == 10_000  # the ledger wins; the trace never adds


def test_trace_token_names_are_normalised(tmp_path: Path) -> None:
    _seed_trace(
        tmp_path,
        _trace(
            trace_id="tn",
            session_id="tn",
            cached_input_tokens=700,
            cache_creation_input_tokens=300,
            thinking_tokens=50,
        ),
    )
    row = _only(collect_usage_rows(tmp_path))
    assert row.cache_read_tokens == 700
    assert row.cache_write_tokens == 300
    assert row.thinking_tokens == 50


# --------------------------------------------------------------------------- #
# Subagents (R10)                                                              #
# --------------------------------------------------------------------------- #


def test_subagent_usage_is_folded_never_split(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="sub",
        calls=[_call("claude-opus-5")],
        telemetry={"subagent_names": {"code-reviewer": 2, "tester": 1}},
    )
    row = _only(collect_usage_rows(tmp_path))
    assert row.subagent_count == 3
    # No row claims to be a subagent's: the split was discarded at import time.
    assert row.parent_session_id is None


def test_subagent_count_is_zero_when_the_host_records_none(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="nosub", host="codex", calls=[_call("claude-opus-5")])
    assert _only(collect_usage_rows(tmp_path)).subagent_count == 0


# --------------------------------------------------------------------------- #
# Totals, filters and degradation                                              #
# --------------------------------------------------------------------------- #


def test_total_tokens_excludes_subset_components(tmp_path: Path) -> None:
    """``reasoning_output_tokens`` is a subset of output; adding it double-bills."""

    _seed_trace(
        tmp_path,
        _trace(
            trace_id="tt",
            session_id="tt",
            input_tokens=1000,
            output_tokens=400,
            reasoning_output_tokens=300,
            thinking_tokens=25,
        ),
    )
    row = _only(collect_usage_rows(tmp_path))
    assert row.reasoning_output_tokens == 300
    assert row.total_tokens == 1425
    assert row.total_tokens == sum_token_components(
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_read_tokens=row.cache_read_tokens,
        cache_write_tokens=row.cache_write_tokens,
        thinking_tokens=row.thinking_tokens,
    )


def test_since_filter_excludes_older_rows(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="old",
        calls=[_call("claude-opus-5")],
        day=("2026", "08", "01"),
        started_at="2026-08-01T10:00:00+00:00",
    )
    _write_run(tmp_path, session_id="new", calls=[_call("claude-opus-5")])
    rows = collect_usage_rows(tmp_path, since=datetime(2026, 8, 20, tzinfo=UTC))
    assert [r.session_id for r in rows] == ["new"]


def test_filters_match_host_project_and_model(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="a", host="claude", calls=[_call("claude-opus-5")], workspace="/w/alpha")
    _write_run(tmp_path, session_id="b", host="codex", calls=[_call("gpt-5")], workspace="/w/beta")

    assert [r.session_id for r in collect_usage_rows(tmp_path, host="CODEX")] == ["b"]
    assert [r.session_id for r in collect_usage_rows(tmp_path, project="alpha")] == ["a"]
    assert [r.session_id for r in collect_usage_rows(tmp_path, model="opus")] == ["a"]
    assert collect_usage_rows(tmp_path, host="nope") == []


def test_rows_are_newest_first_and_limit_keeps_the_newest(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="older",
        calls=[_call("claude-opus-5")],
        started_at="2026-09-01T08:00:00+00:00",
    )
    _write_run(
        tmp_path,
        session_id="newer",
        calls=[_call("claude-opus-5")],
        started_at="2026-09-01T12:00:00+00:00",
    )
    assert [r.session_id for r in collect_usage_rows(tmp_path)] == ["newer", "older"]
    assert [r.session_id for r in collect_usage_rows(tmp_path, limit=1)] == ["newer"]


def test_empty_store_returns_no_rows_and_does_not_raise(tmp_path: Path) -> None:
    assert collect_usage_rows(tmp_path) == []
    assert collect_rows_for_session(tmp_path, "nope") == []
    assert collect_rows_for_session(tmp_path, "") == []


def test_unreadable_run_file_is_skipped_not_raised(tmp_path: Path) -> None:
    good = _write_run(tmp_path, session_id="ok", calls=[_call("claude-opus-5")])
    broken = good.parent.parent / "broken" / "run.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json", encoding="utf-8")
    assert [r.session_id for r in collect_usage_rows(tmp_path)] == ["ok"]


def test_collect_rows_for_session_matches_the_full_scan(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="one", calls=[_call("claude-opus-5")])
    _write_run(tmp_path, session_id="two", calls=[_call("gpt-5")])
    assert collect_rows_for_session(tmp_path, "one") == [
        row for row in collect_usage_rows(tmp_path) if row.session_id == "one"
    ]


def test_session_lookup_aggregates_every_trace_for_the_same_model(tmp_path: Path) -> None:
    _seed_trace(tmp_path, _trace(trace_id="multi-a", session_id="multi", input_tokens=100, output_tokens=10))
    _seed_trace(tmp_path, _trace(trace_id="multi-b", session_id="multi", input_tokens=300, output_tokens=30))

    full = [row for row in collect_usage_rows(tmp_path) if row.session_id == "multi"]
    targeted = collect_rows_for_session(tmp_path, "multi")

    assert targeted == full
    row = _only(targeted)
    assert row.input_tokens == 400
    assert row.output_tokens == 40


def test_session_lookup_keeps_every_model_from_a_multi_trace_session(tmp_path: Path) -> None:
    _seed_trace(
        tmp_path,
        _trace(trace_id="mixed-a", session_id="mixed", model="claude-opus-5", input_tokens=100, output_tokens=10),
    )
    _seed_trace(
        tmp_path,
        _trace(trace_id="mixed-b", session_id="mixed", model="gpt-5", input_tokens=300, output_tokens=30),
    )

    full = [row for row in collect_usage_rows(tmp_path) if row.session_id == "mixed"]
    targeted = collect_rows_for_session(tmp_path, "mixed")

    assert targeted == full
    assert {(row.model, row.input_tokens, row.output_tokens) for row in targeted} == {
        ("claude-opus-5", 100, 10),
        ("gpt-5", 300, 30),
    }


def test_token_row_without_a_parsed_trace_keeps_its_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Beyond the trace detail page a row still counts, with an explicit unknown time."""

    _seed_trace(tmp_path, _trace(trace_id="far", session_id="far", input_tokens=1234))
    monkeypatch.setattr("lemoncrow.pro.capabilities.usage.collect._TRACE_DETAIL_LIMIT", 0)
    row = _only(collect_usage_rows(tmp_path))
    assert row.input_tokens == 1234
    assert row.started_at == UNKNOWN_STARTED_AT
    assert row.started_at.tzinfo is not None


def test_the_token_projection_carries_cache_write_tokens(tmp_path: Path) -> None:
    """``token_rows`` is the only view of a trace that some readers ever get.

    It selected input/output/cache-read/thinking and stopped there, so the one
    priced axis it omitted -- cache writes, $3.75/M on Anthropic ids -- could not
    reach any reader downstream of it.
    """

    _seed_trace(
        tmp_path,
        _trace(trace_id="cw", session_id="cw", cache_creation_input_tokens=7_000),
    )

    rows = HistoryStore(tmp_path).token_rows()

    assert len(rows) == 1
    assert rows[0]["cache_creation_input_tokens"] == 7_000


def test_a_trace_beyond_the_detail_page_keeps_its_cache_write_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole priced axis used to vanish for rows rebuilt from the projection.

    ``load_trace_facts`` parses at most ``_TRACE_DETAIL_LIMIT`` traces; every
    trace past that page is rebuilt from ``HistoryStore.token_rows``, and
    ``_facts_from_token_row`` hardcoded ``cache_write_tokens=0`` because the
    projection did not select the column. On the maintainer's store that lost
    367,428 cache-write tokens across 200 of 3,191 rows, and a store whose
    out-of-page traces are Anthropic sessions would lose the $3.75/M axis
    outright. Modelled by emptying the detail page rather than by seeding 2,001
    traces.
    """

    monkeypatch.setattr("lemoncrow.pro.capabilities.usage.collect._TRACE_DETAIL_LIMIT", 0)
    _seed_trace(
        tmp_path,
        _trace(
            trace_id="cw-far",
            session_id="cw-far",
            model="claude-sonnet-4-5",
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=1_000_000,
        ),
    )

    row = _only(collect_usage_rows(tmp_path))

    assert row.source == "trace"
    assert row.cache_write_tokens == 1_000_000
    assert row.total_tokens == 1_000_000
    assert row.cost_usd is not None
    assert row.cost_usd > 0.0
    assert row.cost_usd == pytest.approx(derive_cost("claude-sonnet-4-5", cache_write_tokens=1_000_000).cost_usd)


def test_row_json_payload_is_serialisable(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="js", calls=[_call("ollama/qwen2.5-coder")])
    payload = _only(collect_usage_rows(tmp_path)).to_dict()
    assert payload["schema_version"] == 1
    assert payload["cost_usd"] is None
    assert payload["started_at"].endswith("+00:00")
    assert json.loads(json.dumps(payload))["cost_provenance"] == "self_hosted_unpriced"


def test_mtime_window_is_respected_by_the_source_scan(tmp_path: Path) -> None:
    run_path = _write_run(tmp_path, session_id="stale", calls=[_call("claude-opus-5")])
    ancient = (datetime(2020, 1, 1, tzinfo=UTC)).timestamp()
    os.utime(run_path, (ancient, ancient))
    assert collect_usage_rows(tmp_path, since=datetime(2026, 1, 1, tzinfo=UTC)) == []
