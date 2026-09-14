"""Group-by over usage rows: separate dollar columns, and no invented zeroes.

The aggregate is where an unpriced row is most tempting to treat as free --
summing ``cost_usd or 0.0`` is one keystroke away and produces a total that
looks fine and is wrong. ``test_unpriced_rows_do_not_inflate_cost`` and
``test_billed_and_estimated_are_separate_columns`` are the two guards; the rest
fix the ordering so the output is byte-reproducible.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime

from lemoncrow.pro.capabilities.usage.aggregate import (
    GROUP_BY_VALUES,
    TOTAL_KEY,
    GroupBy,
    aggregate,
    group_key,
    provenance_mix,
    totals,
)
from lemoncrow.pro.capabilities.usage.models import COST_PROVENANCE_VALUES, CostProvenance, UsageRow


def _row(
    *,
    session_id: str = "s",
    host: str = "claude",
    provider: str = "anthropic",
    model: str = "claude-opus-5",
    project: str = "myproject",
    started_at: datetime | None = None,
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    thinking_tokens: int = 0,
    cost_usd: float | None = 1.0,
    cost_provenance: CostProvenance = "api_estimated",
    duration_seconds: float = 10.0,
    tool_calls: int = 2,
) -> UsageRow:
    return UsageRow(
        session_id=session_id,
        host=host,
        provider=provider,
        model=model,
        project=project,
        started_at=started_at or datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        thinking_tokens=thinking_tokens,
        total_tokens=input_tokens + output_tokens + cache_read_tokens + cache_write_tokens + thinking_tokens,
        cost_usd=cost_usd,
        cost_provenance=cost_provenance,
        duration_seconds=duration_seconds,
        tool_calls=tool_calls,
    )


def test_aggregate_by_host_sums_tokens() -> None:
    rows = [
        _row(session_id="a", host="claude", input_tokens=100, output_tokens=10),
        _row(session_id="b", host="claude", input_tokens=200, output_tokens=20),
        _row(session_id="c", host="codex", input_tokens=50, output_tokens=5),
    ]
    buckets = {bucket.key: bucket for bucket in aggregate(rows, by="host")}
    assert buckets["claude"].rows == 2
    assert buckets["claude"].sessions == 2
    assert buckets["claude"].input_tokens == 300
    assert buckets["claude"].total_tokens == 330
    assert buckets["codex"].total_tokens == 55
    assert buckets["claude"].tool_calls == 4


def test_unpriced_rows_do_not_inflate_cost() -> None:
    rows = [
        _row(session_id="paid", cost_usd=5.0, cost_provenance="api_estimated"),
        _row(session_id="free", cost_usd=None, cost_provenance="self_hosted_unpriced"),
    ]
    bucket = totals(rows)
    assert bucket.estimated_usd == 5.0
    assert bucket.billed_usd == 0.0
    assert bucket.unpriced_rows == 1
    assert bucket.total_usd == 5.0
    assert bucket.rows == 2


def test_billed_and_estimated_are_separate_columns() -> None:
    rows = [
        _row(session_id="a", cost_usd=3.0, cost_provenance="provider_billed"),
        _row(session_id="b", cost_usd=1.5, cost_provenance="api_estimated"),
        _row(session_id="c", cost_usd=0.5, cost_provenance="self_hosted_estimated"),
        _row(session_id="d", cost_usd=None, cost_provenance="enterprise_allocated"),
    ]
    bucket = totals(rows)
    assert bucket.billed_usd == 3.0
    assert bucket.estimated_usd == 2.0
    assert bucket.unpriced_rows == 1
    assert bucket.total_usd == 5.0


def test_provenance_mix_is_sorted_and_complete() -> None:
    rows = [
        _row(session_id=str(i), cost_provenance=value, cost_usd=None) for i, value in enumerate(COST_PROVENANCE_VALUES)
    ]
    rows.append(_row(session_id="extra", cost_provenance="api_estimated", cost_usd=None))
    mix = provenance_mix(rows)
    assert [name for name, _count in mix] == sorted(COST_PROVENANCE_VALUES)
    assert dict(mix)["api_estimated"] == 2
    assert sum(count for _name, count in mix) == len(rows)


def test_aggregate_is_deterministic() -> None:
    rows = [_row(session_id=f"s{i}", host=f"host{i % 3}", model=f"m{i % 4}", cost_usd=float(i % 5)) for i in range(40)]
    for dimension in GROUP_BY_VALUES:
        by: GroupBy = dimension  # type: ignore[assignment]
        expected = aggregate(rows, by=by, limit=-1)
        for seed in (1, 2, 3):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            assert aggregate(shuffled, by=by, limit=-1) == expected


def test_all_unpriced_bucket_is_not_sunk_below_a_tiny_paid_one() -> None:
    """Spend ranks first, but tokens break the tie so a big local bucket stays visible."""

    rows = [
        _row(
            session_id="local",
            host="claude",
            input_tokens=1_000_000,
            cost_usd=None,
            cost_provenance="self_hosted_unpriced",
        ),
        _row(session_id="tiny", host="codex", input_tokens=10, cost_usd=None, cost_provenance="unknown"),
    ]
    assert [bucket.key for bucket in aggregate(rows, by="host")] == ["claude", "codex"]


def test_group_key_never_returns_empty() -> None:
    row = _row(host="", provider="", model="", project="", session_id="")
    for dimension in GROUP_BY_VALUES:
        by: GroupBy = dimension  # type: ignore[assignment]
        assert group_key(row, by)


def test_day_key_uses_the_row_timestamp_not_the_directory() -> None:
    row = _row(started_at=datetime(2026, 9, 3, 23, 30, tzinfo=UTC))
    assert group_key(row, "day") == "2026-09-03"


def test_limit_caps_the_bucket_count() -> None:
    rows = [_row(session_id=f"s{i}", model=f"m{i}", cost_usd=float(i)) for i in range(10)]
    assert len(aggregate(rows, by="model", limit=3)) == 3
    assert len(aggregate(rows, by="model", limit=-1)) == 10


def test_totals_key_is_reserved_and_payload_serialises() -> None:
    bucket = totals([_row(cost_usd=None, cost_provenance="self_hosted_unpriced")])
    assert bucket.key == TOTAL_KEY
    payload = json.loads(json.dumps(bucket.to_dict()))
    assert payload["unpriced_rows"] == 1
    assert payload["provenance_mix"] == [["self_hosted_unpriced", 1]]
    assert payload["total_usd"] == 0.0


def test_empty_rows_aggregate_to_nothing_without_raising() -> None:
    assert aggregate([], by="host") == []
    empty = totals([])
    assert empty.rows == 0
    assert empty.unpriced_rows == 0
    assert empty.provenance_mix == ()
