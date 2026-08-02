from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lemoncrow.pro.capabilities.optimization.cache_economics import (
    choose_cache_decision,
    select_cache_breakpoint,
    should_rewrite_compacted_prefix,
)


def _messages():
    return [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "fix parser"},
    ]


def test_auto_cache_promotes_only_after_observed_long_reuse_and_enforcement(tmp_path) -> None:
    started = datetime(2026, 8, 2, tzinfo=UTC)
    decisions = [
        choose_cache_decision(
            tmp_path,
            requested_policy="auto",
            provider="anthropic",
            model="anthropic/claude-sonnet",
            messages=_messages(),
            optimization_mode="shadow",
            now=started + timedelta(minutes=10 * index),
        )
        for index in range(4)
    ]

    assert [decision.actual_tier for decision in decisions] == ["5m", "5m", "5m", "5m"]
    assert decisions[-1].proposed_tier == "1h"
    enforced = choose_cache_decision(
        tmp_path,
        requested_policy="auto",
        provider="anthropic",
        model="anthropic/claude-sonnet",
        messages=_messages(),
        optimization_mode="enforce",
        now=started + timedelta(minutes=40),
    )
    assert enforced.proposed_tier == enforced.actual_tier == "1h"
    assert enforced.long_gap_probability == 1.0


def test_explicit_cache_policy_wins_and_unsupported_provider_stays_off(tmp_path) -> None:
    explicit = choose_cache_decision(
        tmp_path,
        requested_policy="off",
        provider="anthropic",
        model="anthropic/claude-sonnet",
        messages=_messages(),
        optimization_mode="enforce",
    )
    unsupported = choose_cache_decision(
        tmp_path,
        requested_policy="1h",
        provider="test",
        model="test/model",
        messages=_messages(),
        optimization_mode="enforce",
    )
    assert explicit.actual_tier == "off"
    assert unsupported.actual_tier == "off"


def test_global_off_keeps_legacy_cache_behavior_without_observation_store(tmp_path) -> None:
    decision = choose_cache_decision(
        tmp_path,
        requested_policy="auto",
        provider="anthropic",
        model="anthropic/claude-sonnet-4-5",
        messages=_messages(),
        optimization_mode="off",
    )

    assert decision.actual_tier == "5m"
    assert not (tmp_path / "cache" / "cache-economics.sqlite3").exists()


def test_selective_breakpoint_rejects_build_logs_and_model_prose() -> None:
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "fix parser"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "read-1", "function": {"name": "read"}}],
        },
        {"role": "tool", "tool_call_id": "read-1", "content": "parser.py:10 def parse():"},
        {"role": "assistant", "content": "I will now run tests."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "bash-1", "function": {"name": "bash"}}],
        },
        {"role": "tool", "tool_call_id": "bash-1", "content": "short test summary info\nFAILED test_parser"},
    ]
    selected = select_cache_breakpoint(messages)
    assert selected is not None
    assert selected.index == 3
    assert selected.reason == "read_evidence"


def test_compacted_prefix_rewrite_requires_positive_read_economics() -> None:
    assert should_rewrite_compacted_prefix(
        old_tokens=100_000,
        compacted_tokens=10_000,
        expected_future_reads=10,
    )
    assert not should_rewrite_compacted_prefix(
        old_tokens=100_000,
        compacted_tokens=10_000,
        expected_future_reads=1,
    )
