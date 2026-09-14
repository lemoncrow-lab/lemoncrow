"""Per-session cost decomposition: shares that add up, and honest unknowns.

Two properties matter here. The breakdown's shares must sum to 1.0, or the
decomposition is not a decomposition. And an unpriced run must report
``total_cost_usd is None`` with a note saying why -- reporting ``$0.00`` for a
busy local session is the exact failure this whole substrate exists to stop.

Prefix resolution gets its own coverage because the failure mode is silent:
picking one of two matching sessions attributes a cost report to the wrong run.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation.history_store import HistoryStore
from lemoncrow.core.foundation.models import Trace
from lemoncrow.infra.runtime.session_report import load_report
from lemoncrow.pro.capabilities.usage.explain import (
    BREAKDOWN_BASIS_VALUES,
    BREAKDOWN_BUCKETS,
    _breakdown_basis,
    _session_bucket_costs,
    candidate_session_ids,
    explain_run,
    resolve_run_id,
)
from lemoncrow.pro.capabilities.usage.models import COST_PROVENANCE_VALUES, UsageRow

Call = tuple[str, int, int, int, int]


def _write_run(
    root: Path,
    *,
    session_id: str,
    host: str = "claude",
    calls: Sequence[Call] = (),
    total_cost_usd: float = 0.0,
    workspace: str | None = "/home/dev/myproject",
    telemetry: dict[str, Any] | None = None,
    tool_events: Sequence[tuple[str, str, int]] = (),
) -> Path:
    snapshot: dict[str, Any] = {
        "session_id": session_id,
        "status": "completed",
        "created_at": "2026-09-01T10:00:00+00:00",
        "updated_at": "2026-09-01T10:30:00+00:00",
        "workspace_path": workspace,
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
                "at": "2026-09-01T10:05:00+00:00",
                "payload": {"tool_name": name, "model": model, "estimated_input_tokens": tokens},
            }
            for name, model, tokens in tool_events
        ],
    }
    target = root / "sessions" / "2026" / "09" / "01" / host / session_id
    target.mkdir(parents=True, exist_ok=True)
    run_path = target / "run.json"
    run_path.write_text(json.dumps(snapshot), encoding="utf-8")
    return run_path


def _seed_trace(root: Path, *, trace_id: str, session_id: str, model: str = "claude-opus-5") -> None:
    store = HistoryStore(root)
    store.init()
    store.record_trace(
        Trace(
            id=trace_id,
            session_id=session_id,
            agent="agent",
            domain="code",
            task="do the thing",
            status="success",
            host="opencode",
            model=model,
            workspace_path="/home/dev/traced",
            created_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
            input_tokens=4000,
            output_tokens=800,
        )
    )


def _shares(payload: dict[str, Any]) -> float:
    return sum(float(entry["share"]) for entry in payload["breakdown"])


def test_explain_breakdown_shares_sum_to_one(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="sess-alpha",
        calls=[("claude-opus-5", 10_000, 2_000, 5_000, 1_000)],
    )
    payload = explain_run(tmp_path, "sess-alpha")
    assert [entry["bucket"] for entry in payload["breakdown"]] != []
    assert sorted(entry["bucket"] for entry in payload["breakdown"]) == sorted(BREAKDOWN_BUCKETS)
    assert _shares(payload) == pytest.approx(1.0, abs=1e-6)
    assert payload["total_tokens"] == 18_000
    assert payload["breakdown"][0]["bucket"] == "fresh_input"


def test_explain_bucket_costs_add_up_to_the_total(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="sess-bill",
        calls=[("claude-opus-5", 10_000, 2_000, 5_000, 1_000)],
        total_cost_usd=7.5,
    )
    payload = explain_run(tmp_path, "sess-bill")
    assert payload["cost_provenance"] == "api_estimated"
    assert payload["total_cost_usd"] == pytest.approx(7.5)
    priced = [entry for entry in payload["breakdown"] if entry["cost_usd"] is not None]
    assert sum(float(entry["cost_usd"]) for entry in priced) == pytest.approx(7.5, abs=1e-4)


def test_explain_prefix_match(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="abcdef123", calls=[("claude-opus-5", 1000, 100, 0, 0)])
    assert resolve_run_id(tmp_path, "abcd") == "abcdef123"
    assert explain_run(tmp_path, "abcd")["session_id"] == "abcdef123"


def test_explain_ambiguous_prefix_raises(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="abc-one", calls=[("claude-opus-5", 1000, 100, 0, 0)])
    _write_run(tmp_path, session_id="abc-two", calls=[("claude-opus-5", 1000, 100, 0, 0)])
    with pytest.raises(ValueError) as excinfo:
        explain_run(tmp_path, "abc")
    message = str(excinfo.value)
    assert "abc-one" in message
    assert "abc-two" in message
    assert "ambiguous" in message


def test_explain_unknown_run_raises_naming_the_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nope"):
        explain_run(tmp_path, "nope")
    with pytest.raises(ValueError):
        explain_run(tmp_path, "   ")


def test_explain_unpriced_run_reports_note(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="local-run",
        calls=[("ollama/qwen2.5-coder", 80_000, 12_000, 0, 0)],
    )
    payload = explain_run(tmp_path, "local-run")
    assert payload["total_cost_usd"] is None
    assert payload["cost_provenance"] == "self_hosted_unpriced"
    assert any("unpriced" in note for note in payload["notes"])
    assert all(entry["cost_usd"] is None for entry in payload["breakdown"])
    # Tokens survive: only the price is unknown.
    assert payload["total_tokens"] == 92_000
    assert _shares(payload) == pytest.approx(1.0, abs=1e-6)


def test_explain_subagent_bucket_never_fakes_a_split(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="sub-run",
        calls=[("claude-opus-5", 10_000, 2_000, 0, 0)],
        telemetry={"subagent_names": {"code-reviewer": 2}},
    )
    payload = explain_run(tmp_path, "sub-run")
    subagents = next(entry for entry in payload["breakdown"] if entry["bucket"] == "subagents")
    assert subagents["share"] == 0.0
    assert subagents["tokens"] == 0
    assert subagents["cost_usd"] is None
    assert any("folded" in note for note in payload["notes"])


def test_explain_reports_top_tools(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="tools-run",
        calls=[("claude-opus-5", 10_000, 2_000, 0, 0)],
        tool_events=[("Read", "claude-opus-5", 5000), ("Grep", "claude-opus-5", 1000)],
    )
    payload = explain_run(tmp_path, "tools-run")
    names = [tool["name"] for tool in payload["top_tools"]]
    assert names[:2] == ["Read", "Grep"]
    assert payload["top_tools"][0]["calls"] == 1


def test_explain_multi_model_run_lists_every_model(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="multi-run",
        calls=[
            ("claude-opus-5", 1000, 100, 0, 0),
            ("claude-opus-5", 1000, 100, 0, 0),
            ("gpt-5", 1000, 100, 0, 0),
        ],
    )
    payload = explain_run(tmp_path, "multi-run")
    assert payload["models"] == ["claude-opus-5", "gpt-5"]
    assert any("2 models used" in note for note in payload["notes"])
    assert any("approximate" in note for note in payload["notes"])
    assert _shares(payload) == pytest.approx(1.0, abs=1e-6)


def test_explain_and_session_report_agree_bucket_for_bucket(tmp_path: Path) -> None:
    """Two surfaces, one dollar, one split.

    The shape that broke: many cheap turns and a few expensive ones, with the
    expensive model carrying a different token mix. Pricing every bucket off
    the session's dominant model moved a quarter of the bill between
    cache_write and cache_read while the total stayed correct -- so the total
    agreeing proves nothing, and the buckets have to be checked one by one.
    """

    calls: list[Call] = [("claude-haiku-4-5-20251001", 8, 40, 60_000, 1_500)] * 86
    calls += [("claude-opus-5", 6, 200, 15_000, 2_500)] * 27
    _write_run(tmp_path, session_id="mixed-models", calls=calls, total_cost_usd=2.09374)

    payload = explain_run(tmp_path, "mixed-models")
    report = load_report("mixed-models", tmp_path)
    assert report is not None

    costs = {str(entry["bucket"]): entry["cost_usd"] for entry in payload["breakdown"]}
    expected = {
        "fresh_input": report.input_token_cost_usd,
        "cache_read": report.cache_read_cost_usd,
        "cache_write": report.cache_write_cost_usd,
        "output": report.output_token_cost_usd,
    }
    for bucket, want in expected.items():
        assert costs[bucket] == pytest.approx(want, abs=1e-5), bucket
    assert sum(float(v) for v in costs.values() if v is not None) == pytest.approx(2.09374, abs=1e-4)

    # The fixture has to be able to fail: confirm the dominant-model shape this
    # replaced would land somewhere materially different, or the assertions
    # above would pass on a session where every model priced the same.
    from lemoncrow.core.capabilities.pricing import get_model_pricing

    naive = get_model_pricing("claude-haiku-4-5-20251001").cost_breakdown_usd(
        input_tokens=report.input_tokens,
        output_tokens=report.output_tokens,
        cache_read_tokens=report.cache_read_tokens,
        cache_write_tokens=report.cache_write_tokens,
    )
    naive_sum = sum(naive.values())
    naive_cache_write = naive["cache_write"] * 2.09374 / naive_sum
    assert abs(naive_cache_write - float(costs["cache_write"])) > 0.05


def test_multi_model_session_without_a_ledger_reports_unknown_not_a_guess() -> None:
    """No per-turn record plus two rate cards means no honest split exists."""

    rows = [
        UsageRow(session_id="s", model="claude-haiku-4-5-20251001", cache_read_tokens=5_000_000, cost_usd=1.0),
        UsageRow(session_id="s", model="claude-opus-5", output_tokens=9_000, cost_usd=2.0),
    ]
    models = ["claude-haiku-4-5-20251001", "claude-opus-5"]
    assert _session_bucket_costs(None, rows, models, 3.0) is None

    # One model is still attributable: there is no second card to charge to.
    single = _session_bucket_costs(None, rows[:1], ["claude-haiku-4-5-20251001"], 1.0)
    assert single is not None
    assert sum(single.values()) == pytest.approx(1.0, abs=1e-4)


def test_provenance_describes_the_dollars_not_the_biggest_row(tmp_path: Path) -> None:
    """The basis word must come from the rows that produced the money.

    ``provenance = _dominant(rows).cost_provenance`` keyed on ``total_tokens``,
    while ``total_cost_usd`` sums only the priced rows. A session whose
    largest-token model is unpriced therefore printed a rate-card dollar figure
    tagged with the unpriced row's word -- ``$2.10 local`` in the human view,
    ``cost_provenance: "self_hosted_unpriced"`` on a non-null cost in ``--json``,
    which is a self-hosted-with-no-rate-card claim about API money.
    """

    from lemoncrow.pro.capabilities.usage.models import PRICED_PROVENANCE_VALUES

    calls: list[Call] = [("ollama/llama2", 500_000, 1_000, 0, 0)] * 9
    calls += [("claude-sonnet-4-5", 10_000, 2_000, 0, 0)]
    _write_run(tmp_path, session_id="unpriced-lead", calls=calls)

    payload = explain_run(tmp_path, "unpriced-lead")

    # The token-dominant row still names the session...
    assert payload["model"] == "ollama/llama2"
    # ...but it does not get to describe dollars it contributed none of.
    assert payload["total_cost_usd"] is not None
    assert float(payload["total_cost_usd"]) > 0
    assert payload["cost_provenance"] == "api_estimated"
    assert payload["cost_provenance"] != "self_hosted_unpriced"
    assert payload["cost_provenance"] in PRICED_PROVENANCE_VALUES


def test_unpriced_session_keeps_the_reason_it_has_no_price(tmp_path: Path) -> None:
    """With no priced row there are no dollars, so the lead row's word stands."""

    _write_run(tmp_path, session_id="all-local", calls=[("ollama/llama2", 500_000, 1_000, 0, 0)] * 9)

    payload = explain_run(tmp_path, "all-local")

    assert payload["total_cost_usd"] is None
    assert payload["cost_provenance"] == "self_hosted_unpriced"
    assert any("unpriced" in note for note in payload["notes"])


def test_explain_trace_only_session_says_the_ledger_is_missing(tmp_path: Path) -> None:
    _seed_trace(tmp_path, trace_id="t-only", session_id="trace-run")
    payload = explain_run(tmp_path, "trace-run")
    assert payload["host"] == "opencode"
    assert payload["project"] == "traced"
    assert payload["top_tools"] == []
    assert any("no run ledger" in note for note in payload["notes"])


def test_a_session_total_does_not_vouch_for_a_derived_split(tmp_path: Path) -> None:
    """Two facts, two fields.

    ``--json`` used to lead with a single ``cost_provenance`` and leave it at
    that, while ``notes`` further down admitted the rates were approximate and
    four models were in play. One word cannot vouch for both the session total
    and a split nobody bills: no invoice itemises cache-read against
    cache-write, so every bucket here is derived.
    """

    calls: list[Call] = [("claude-haiku-4-5-20251001", 8, 40, 60_000, 1_500)] * 60
    calls += [("claude-opus-5", 6, 200, 15_000, 2_500)] * 20
    _write_run(tmp_path, session_id="mixed-basis", calls=calls, total_cost_usd=3.5)

    payload = explain_run(tmp_path, "mixed-basis")

    # The total is the ledger's own recorded figure.
    assert payload["cost_provenance"] == "api_estimated"
    assert payload["total_cost_usd"] == pytest.approx(3.5)
    # The split is not, and says so in its own field rather than in prose.
    assert payload["breakdown_basis"] == "ledger_per_turn"
    assert payload["breakdown_basis"] != payload["cost_provenance"]
    assert payload["breakdown_basis"] not in COST_PROVENANCE_VALUES
    assert payload["breakdown_basis"] in BREAKDOWN_BASIS_VALUES

    priced = [entry for entry in payload["breakdown"] if entry["cost_usd"] is not None]
    assert priced
    assert all(entry["basis"] == "ledger_per_turn" for entry in priced)
    # The buckets nothing can separate carry their own basis, not the split's.
    unseparable = [entry for entry in payload["breakdown"] if entry["bucket"] in ("subagents", "tool_results")]
    assert [entry["basis"] for entry in unseparable] == ["unseparable", "unseparable"]
    assert all(entry["cost_usd"] is None for entry in unseparable)


def test_approximate_rates_are_flagged_on_the_split_not_only_in_notes(tmp_path: Path) -> None:
    """The caveat has to travel with the field it qualifies.

    A machine reader that trusts ``cost_provenance`` and never parses English
    prose was, before this, told nothing at all about the split's accuracy.
    """

    calls: list[Call] = [("claude-haiku-4-5-20251001", 8, 40, 60_000, 1_500)] * 60
    calls += [("claude-opus-5", 6, 200, 15_000, 2_500)] * 20
    _write_run(tmp_path, session_id="sibling-rates", calls=calls, total_cost_usd=3.5)

    payload = explain_run(tmp_path, "sibling-rates")

    assert payload["breakdown_basis_approximate"] is True
    assert any("approximate" in note for note in payload["notes"])


def test_an_exact_rate_card_is_not_flagged_as_approximate(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="exact-rates",
        calls=[("claude-fable-5", 1_000, 200, 5_000, 500)],
        total_cost_usd=1.25,
    )

    payload = explain_run(tmp_path, "exact-rates")

    assert payload["breakdown_basis_approximate"] is False
    assert not any("approximate" in note for note in payload["notes"])


def test_an_unattributable_split_says_so_in_the_basis_field(tmp_path: Path) -> None:
    """No per-turn record plus two rate cards: the buckets have no basis at all."""

    rows = [
        UsageRow(session_id="s", model="claude-haiku-4-5-20251001", cache_read_tokens=5_000_000, cost_usd=1.0),
        UsageRow(session_id="s", model="claude-opus-5", output_tokens=9_000, cost_usd=2.0),
    ]
    assert _breakdown_basis(total_cost=3.0, unattributable=True, ledger_present=False) == "unattributable"
    assert _session_bucket_costs(None, rows, [r.model for r in rows], 3.0) is None


def test_an_unpriced_session_reports_an_unpriced_split(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        session_id="local-split",
        calls=[("ollama/qwen2.5-coder", 80_000, 12_000, 0, 0)],
    )

    payload = explain_run(tmp_path, "local-split")

    assert payload["breakdown_basis"] == "unpriced"
    assert all(entry["cost_usd"] is None for entry in payload["breakdown"])


def test_a_single_model_session_without_a_ledger_names_the_rate_card(tmp_path: Path) -> None:
    _seed_trace(tmp_path, trace_id="t-card", session_id="card-run")

    payload = explain_run(tmp_path, "card-run")

    assert payload["breakdown_basis"] == "rate_card_single_model"
    assert any("no run ledger" in note for note in payload["notes"])


def test_explain_payload_is_json_serialisable(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="json-run", calls=[("claude-opus-5", 1000, 100, 0, 0)])
    payload = explain_run(tmp_path, "json-run")
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["schema_version"] == 1
    assert round_tripped["session_id"] == "json-run"


def test_candidate_session_ids_covers_both_stores(tmp_path: Path) -> None:
    _write_run(tmp_path, session_id="from-ledger", calls=[("claude-opus-5", 1000, 100, 0, 0)])
    _seed_trace(tmp_path, trace_id="t2", session_id="from-trace")
    assert candidate_session_ids(tmp_path) == ["from-ledger", "from-trace"]
