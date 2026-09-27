"""The rendering contract: what a bucket with no real price is allowed to print.

``lc usage`` is the first surface that reads ``UsageRow.cost_usd is None``, and
the whole point of that field dies at the renderer: ``total_usd`` is ``0.0`` for
an unpriced bucket, so the naive cell prints ``$0.00`` and a busy local session
reads as free. Every test here pins one half of the rule -- what gets printed
instead, and that a dollar sign only ever appears next to a real measurement.

The rest covers shape: totals, truncation, billions, and the two degraded cases
(empty report, unpriced run) that must render a value rather than raise.
"""

from __future__ import annotations

from typing import Any

import pytest

from lemoncrow.pro.capabilities.usage.models import UsageAggregate
from lemoncrow.pro.capabilities.usage.render import (
    UNPRICED_LABELS,
    basis_cell,
    cost_cell,
    render_explain,
    render_usage,
)


def _bucket(
    key: str,
    *,
    rows: int = 1,
    sessions: int = 1,
    tokens: int = 1000,
    billed: float = 0.0,
    estimated: float = 0.0,
    unpriced: int = 0,
    mix: tuple[tuple[str, int], ...] = (("api_estimated", 1),),
) -> UsageAggregate:
    return UsageAggregate(
        key=key,
        rows=rows,
        sessions=sessions,
        total_tokens=tokens,
        billed_usd=billed,
        estimated_usd=estimated,
        unpriced_rows=unpriced,
        provenance_mix=mix,
    )


# --------------------------------------------------------------------------- #
# cost_cell -- the rule                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("provenance", "expected"),
    [
        ("self_hosted_unpriced", "local"),
        ("enterprise_allocated", "seat"),
        ("unknown", UNPRICED_LABELS["unknown"]),
    ],
)
def test_fully_unpriced_bucket_names_its_reason(provenance: str, expected: str) -> None:
    bucket = _bucket("k", rows=3, unpriced=3, mix=((provenance, 3),))

    assert cost_cell(bucket) == expected
    assert "$" not in cost_cell(bucket)


def test_zero_rate_self_hosted_bucket_still_renders_local() -> None:
    """LiteLLM prices a few ``ollama/*`` ids at literally zero. Not money."""

    bucket = _bucket("ollama", rows=2, estimated=0.0, mix=(("self_hosted_estimated", 2),))

    assert cost_cell(bucket) == "local"


def test_priced_bucket_renders_dollars() -> None:
    bucket = _bucket("claude", rows=2, billed=4.1, mix=(("provider_billed", 2),))

    assert cost_cell(bucket) == "$4.10"


def test_genuinely_zero_cloud_bucket_may_render_zero() -> None:
    """A rate-carded model that consumed nothing really did cost $0.00."""

    bucket = _bucket("openai", rows=1, estimated=0.0, tokens=0, mix=(("api_estimated", 1),))

    assert cost_cell(bucket) == "$0.00"


def test_partly_unpriced_bucket_reports_the_money_it_does_know() -> None:
    bucket = _bucket(
        "claude",
        rows=4,
        billed=2.5,
        unpriced=1,
        mix=(("provider_billed", 3), ("unknown", 1)),
    )

    assert cost_cell(bucket) == "$2.50"


def test_empty_bucket_does_not_invent_a_label() -> None:
    assert cost_cell(_bucket("k", rows=0, mix=())) == "$0.00"


# --------------------------------------------------------------------------- #
# basis_cell                                                                  #
# --------------------------------------------------------------------------- #


def test_basis_names_a_single_provenance() -> None:
    assert basis_cell(_bucket("k", mix=(("provider_billed", 3),))) == "billed"


def test_basis_joins_two_provenances_dominant_first() -> None:
    bucket = _bucket("k", mix=(("api_estimated", 1), ("provider_billed", 9)))

    assert basis_cell(bucket) == "billed+estimated"


def test_basis_collapses_three_or_more_to_mixed() -> None:
    bucket = _bucket("k", mix=(("api_estimated", 1), ("provider_billed", 2), ("unknown", 3)))

    assert basis_cell(bucket) == "mixed"


def test_basis_of_an_empty_mix_is_not_a_claim() -> None:
    assert basis_cell(_bucket("k", mix=())) == UNPRICED_LABELS["unknown"]


# --------------------------------------------------------------------------- #
# render_usage                                                                #
# --------------------------------------------------------------------------- #


def test_empty_report_renders_a_value_not_a_blank() -> None:
    empty = _bucket("__total__", rows=0, sessions=0, tokens=0, mix=())

    text = render_usage([], empty, by="host", window="7d", no_color=True)

    assert "no usage recorded in the last 7d" in text
    assert "\x1b[" not in text


def test_report_carries_a_totals_row() -> None:
    groups = [_bucket("claude", rows=2, billed=3.0, mix=(("provider_billed", 2),))]
    total = _bucket("__total__", rows=2, billed=3.0, mix=(("provider_billed", 2),))

    text = render_usage(groups, total, by="host", window="7d", no_color=True)

    assert "HOST" in text
    assert "TOTAL" in text
    assert "$3.00" in text


def test_all_unpriced_report_never_prints_dollar_zero() -> None:
    groups = [_bucket("opencode", rows=5, unpriced=5, mix=(("self_hosted_unpriced", 5),))]
    total = _bucket("__total__", rows=5, unpriced=5, mix=(("self_hosted_unpriced", 5),))

    text = render_usage(groups, total, by="host", window="30d", no_color=True)

    assert "$0.00" not in text
    assert "local" in text
    assert "5 of 5 rows carry no price" in text


def test_unpriced_footer_explains_each_reason() -> None:
    total = _bucket(
        "__total__",
        rows=10,
        billed=1.0,
        unpriced=4,
        mix=(("provider_billed", 6), ("enterprise_allocated", 3), ("unknown", 1)),
    )

    text = render_usage([], total, by="host", window="7d", no_color=True)

    assert "subscription seat" in text
    assert "model id unresolved" in text


def test_billions_do_not_render_as_thousands_of_millions() -> None:
    total = _bucket("__total__", rows=1, tokens=53_949_350_000, billed=1.0, mix=(("provider_billed", 1),))

    text = render_usage([total], total, by="host", window="90d", no_color=True)

    assert "53.95B" in text
    assert "53949.35M" not in text


def test_truncation_is_declared_when_limit_dropped_a_group() -> None:
    shown = [_bucket("claude", rows=2, billed=1.0, mix=(("provider_billed", 2),))]
    total = _bucket("__total__", rows=9, billed=1.0, mix=(("provider_billed", 9),))

    text = render_usage(shown, total, by="host", window="7d", no_color=True)

    assert "raise `--limit`" in text


def test_no_truncation_notice_when_every_group_is_shown() -> None:
    shown = [_bucket("claude", rows=2, billed=1.0, mix=(("provider_billed", 2),))]
    total = _bucket("__total__", rows=2, billed=1.0, mix=(("provider_billed", 2),))

    text = render_usage(shown, total, by="host", window="7d", no_color=True)

    assert "raise `--limit`" not in text


def test_first_screen_states_no_counterfactual() -> None:
    """Plan 2026-09-07 §4.2: visibility first, savings only when asked for."""

    total = _bucket("__total__", rows=3, billed=9.0, mix=(("provider_billed", 3),))

    text = render_usage([total], total, by="host", window="7d", no_color=True).lower()

    for claim in ("potential optimization", "would have saved", "could have saved"):
        assert claim not in text


def test_rich_path_renders_the_same_facts() -> None:
    groups = [_bucket("claude", rows=2, billed=3.0, mix=(("provider_billed", 2),))]
    total = _bucket("__total__", rows=2, billed=3.0, mix=(("provider_billed", 2),))

    text = render_usage(groups, total, by="host", window="7d")

    assert "claude" in text
    assert "TOTAL" in text
    assert "$3.00" in text


# --------------------------------------------------------------------------- #
# render_explain                                                              #
# --------------------------------------------------------------------------- #


def _explain_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "session_id": "abc123",
        "host": "claude",
        "model": "claude-sonnet-5",
        "models": ["claude-sonnet-5"],
        "project": "alpha",
        "total_cost_usd": 2.5,
        "cost_provenance": "provider_billed",
        "breakdown_basis": "ledger_per_turn",
        "breakdown_basis_approximate": False,
        "total_tokens": 1_000_000,
        "duration_seconds": 543.0,
        "breakdown": [
            {"bucket": "cache_read", "tokens": 900_000, "cost_usd": 2.0, "share": 0.9, "basis": "ledger_per_turn"},
            {"bucket": "output", "tokens": 100_000, "cost_usd": 0.5, "share": 0.1, "basis": "ledger_per_turn"},
            {"bucket": "subagents", "tokens": 0, "cost_usd": None, "share": 0.0, "basis": "unseparable"},
        ],
        "top_tools": [{"name": "Read", "calls": 12, "cost_usd": 0.31}],
        "notes": ["subagent usage is folded into this session's totals"],
    }
    payload.update(overrides)
    return payload


def test_explain_renders_buckets_tools_and_notes() -> None:
    text = render_explain(_explain_payload(), no_color=True)

    assert "RUN  abc123" in text
    assert "$2.50 billed" in text
    assert "9m 3s" in text
    assert "cache_read" in text
    assert "Read" in text
    assert "folded" in text


def test_explain_unseparable_bucket_shows_unknown_not_free() -> None:
    text = render_explain(_explain_payload(), no_color=True)
    subagent_line = next(line for line in text.splitlines() if "subagents" in line)

    assert UNPRICED_LABELS["unknown"] in subagent_line
    assert "$" not in subagent_line


def test_explain_unpriced_run_states_why() -> None:
    payload = _explain_payload(
        total_cost_usd=None,
        cost_provenance="self_hosted_unpriced",
        breakdown=[{"bucket": "output", "tokens": 10, "cost_usd": None, "share": 1.0}],
        top_tools=[],
        notes=["cost is unpriced: local or self-hosted model with no rate card"],
    )

    text = render_explain(payload, no_color=True)

    assert "local" in text
    assert "$0.00" not in text


def test_explain_separates_the_billed_total_from_the_derived_split() -> None:
    """The headline says billed; the line under it must not let that spread.

    ``$2.50 billed`` is true of the total and false of every row beneath it.
    Without this line the reader has one provenance word and four dollar figures
    it does not actually cover.
    """

    text = render_explain(_explain_payload(), no_color=True)

    assert "$2.50 billed" in text
    assert "split: derived, not billed" in text
    cache_line = next(line for line in text.splitlines() if "cache_read" in line)
    assert "derived" in cache_line
    subagent_line = next(line for line in text.splitlines() if "subagents" in line)
    assert "unseparable" in subagent_line


def test_explain_flags_approximate_rates_on_the_split_line() -> None:
    text = render_explain(_explain_payload(breakdown_basis_approximate=True), no_color=True)

    assert "at approximate rates" in text


def test_explain_states_when_no_split_exists_at_all() -> None:
    payload = _explain_payload(
        breakdown_basis="unattributable",
        breakdown=[
            {"bucket": "cache_read", "tokens": 900_000, "cost_usd": None, "share": 1.0, "basis": "unattributable"}
        ],
    )

    text = render_explain(payload, no_color=True)

    assert "split: unknown" in text
    assert "nothing records which turn used which" in text


def test_explain_survives_a_minimal_payload() -> None:
    """A degraded payload must render, not raise (spec §6.5)."""

    text = render_explain({"session_id": "x"}, no_color=True)

    assert "RUN  x" in text
    assert "WHERE IT WENT" in text


def test_explain_rich_path_renders_the_same_facts() -> None:
    text = render_explain(_explain_payload())

    assert "abc123" in text
    assert "cache_read" in text
