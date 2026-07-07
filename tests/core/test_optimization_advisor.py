from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from atelier.core.capabilities.optimization.complexity import score_complexity
from atelier.core.capabilities.optimization.golden_runner import run_golden_suite
from atelier.core.capabilities.optimization.optimizer import (
    INSUFFICIENT_HISTORY_MESSAGE,
    optimize_from_traces,
)
from atelier.core.capabilities.optimization.policy import (
    load_current_policy,
    preset_policy,
    save_policy,
)
from atelier.core.capabilities.optimization.shadow import build_shadow_state
from atelier.core.foundation.models import Trace, UsageEntry


def _trace(index: int, *, task: str = "Fix a bug", cost_usd: float = 1.0) -> Trace:
    return Trace(
        id=f"trace-{index}",
        agent="codex",
        host="codex",
        domain="optimization-test",
        task=f"{task} {index}",
        status="success",
        files_touched=[f"src/module_{index % 3}/file.py"],
        input_tokens=10_000,
        output_tokens=1_000,
        model="gpt-4o",
        usage_entries=[
            UsageEntry(
                model="gpt-4o",
                input_tokens=10_000,
                output_tokens=1_000,
                cost_usd=cost_usd,
            )
        ],
        created_at=datetime.now(UTC),
    )


def test_complexity_scores_risky_migration_above_explanation() -> None:
    explain = score_complexity(task="Explain this module", files_touched=0, required_tools=1)
    migration = score_complexity(
        task="Urgent production auth schema migration?",
        files_touched=25,
        distinct_modules=5,
        failed_tests_or_commands=1,
        prior_failures=2,
        required_tools=8,
    )

    assert migration.score > explain.score
    assert migration.label in {"medium", "hard"}


@pytest.fixture(autouse=True)
def _entitle_savings_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    # This module exercises the Pro savings engine; treat the install as licensed
    # so load_current_policy returns the configured policy, not the Free baseline.
    monkeypatch.setattr("atelier.core.capabilities.licensing.has_feature", lambda *a, **k: True)


def test_policy_roundtrip_preserves_preset(tmp_path: Path) -> None:
    path = save_policy(tmp_path, preset_policy("economy"))

    assert path.exists()
    loaded = load_current_policy(tmp_path)
    assert loaded.preset == "economy"
    assert loaded.compaction.lossy_summary is True


def test_optimizer_requires_enough_history_before_recommending() -> None:
    result = optimize_from_traces(
        [_trace(i) for i in range(5)],
        current_policy=preset_policy("balanced"),
        days=7,
    )

    assert result.has_recommendation is False
    assert result.message == INSUFFICIENT_HISTORY_MESSAGE
    assert result.confidence == "low"


def test_optimizer_recommends_with_confidence_and_all_compaction_types() -> None:
    traces = [_trace(i, task="Fix a regression", cost_usd=1.0 + (i * 0.01)) for i in range(20)]
    result = optimize_from_traces(traces, current_policy=preset_policy("balanced"), days=7)
    payload = result.to_dict()

    assert result.has_recommendation is True
    assert result.confidence in {"medium", "high"}
    assert payload["estimation"]["source"] == "stored_atelier_traces"
    assert payload["estimation"]["replay"] == "not_replayed"
    assert payload["estimation"]["savings_are_estimates"] is True
    assert "does not prove that a cheaper model would solve the same task" in payload["estimation"]["limitations"]
    recommended = min(
        [candidate for candidate in result.candidates if candidate.id != "current"],
        key=lambda candidate: candidate.weekly_cost_usd,
    )
    assert set(recommended.compaction_breakdown) == {
        "prompt_cache_reorder",
        "dedup",
        "retrieval_filter",
        "lossy_summary",
    }


def test_golden_optimization_corpus_has_at_least_50_well_formed_tasks() -> None:
    result = run_golden_suite(preset_policy("balanced"))

    assert result.total >= 50
    assert result.failures == []


def test_shadow_daily_cap_enforced_when_baseline_is_zero() -> None:
    # Intentional guardrail (M5): with a zero 7-day baseline the maximum
    # allowed daily cap is 0, so any positive explicit max_daily_spend_usd is
    # rejected. A new/zero-history account must not authorize real spend off a
    # baseline it has no evidence for. The CLI caller wraps this ValueError in
    # a click.ClickException, so it surfaces as a clean user-facing error.
    with pytest.raises(ValueError, match="cannot exceed 25%"):
        build_shadow_state(
            policy="balanced",
            days=7,
            baseline_weekly_cost_usd=0.0,
            max_daily_spend_usd=1000.0,
        )


def test_shadow_zero_baseline_defaults_to_zero_cap_without_raising() -> None:
    # When no explicit cap is given the requested cap derives from the (zero)
    # baseline, equals the maximum allowed (0), and is accepted -- the guard
    # only rejects an *explicit* positive cap that exceeds the baseline-derived
    # ceiling, never the implicit default.
    state = build_shadow_state(
        policy="balanced",
        days=7,
        baseline_weekly_cost_usd=0.0,
    )
    assert state.max_daily_spend_usd == 0.0
    assert state.estimated_weekly_spend_usd == 0.0


def test_shadow_positive_cap_within_baseline_ceiling_is_accepted() -> None:
    # Positive control: a non-zero baseline admits a cap up to 25% of the
    # trailing daily baseline. $70/wk -> $10/day -> ceiling $2.50/day.
    state = build_shadow_state(
        policy="balanced",
        days=7,
        baseline_weekly_cost_usd=70.0,
        max_daily_spend_usd=2.0,
    )
    assert state.max_daily_spend_usd == 2.0
