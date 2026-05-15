from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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

    assert result.has_recommendation is True
    assert result.confidence in {"medium", "high"}
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
