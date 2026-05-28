"""Tests for PhaseRunner — LINEAR-01 and LINEAR-02.

Verifies the phase state-machine foundation:
- Typed models (Phase, PhasePlan, PhaseResult, PhaseCacheStats, RunMode) — LINEAR-01
- Survey→Plan continuation, fixed system prompt, Implement starts lean,
  per-phase cache breakpoints, reader/writer tool profile enforcement — LINEAR-02
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest
from atelier.core.capabilities.context_reuse.phase_runner import (
    _READER_TOOLS,
    _WRITER_TOOLS,
    PhaseRunner,
)

from atelier.core.capabilities.context_reuse.models import (
    Phase,
    PhaseCacheStats,
    PhasePlan,
    PhaseResult,
    RunMode,
)
from atelier.core.capabilities.prefix_cache.diagnostics import PrefixCacheDiagnostics
from atelier.core.capabilities.prefix_cache.planner import PrefixCachePlanner
from atelier.infra.runtime.run_ledger import RunLedger


class _FakeProvider:
    """Records every call's messages snapshot; returns scripted cache stats."""

    def __init__(self, scripted_cache: list[tuple[int, int]] | None = None) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self._scripted = list(scripted_cache or [])

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> tuple[str, int, int, int, int]:
        self.calls.append(copy.deepcopy(messages))
        if self._scripted:
            cache_read, cache_write = self._scripted.pop(0)
        return ("ok <phase-complete>", 100, 50, cache_read, cache_write)


def _build_plan() -> PhasePlan:
    """Build the canonical survey→plan→implement DAG (design-spec §1)."""
    survey = Phase(
        name="survey",
        kind="agent",
        profile="reader",
        objective_path="survey.md",
        continue_from=None,
        next="plan",
    )
    plan = Phase(
        name="plan",
        kind="agent",
        profile="reader",
        objective_path="plan.md",
        continue_from="survey",
        next="implement",
    )
    implement = Phase(
        name="implement",
        kind="agent",
        profile="writer",
        objective_path="implement.md",
        continue_from=None,
        next=None,
    )
    return PhasePlan(
        name="phase-linear-cache-reuse",
        entry="survey",
        phases={"survey": survey, "plan": plan, "implement": implement},
    )


@pytest.fixture()
def runner_factory(tmp_path: Path):
    """Build a fresh PhaseRunner wired to a FakeProvider, returning (runner, provider)."""

    def _make(
        scripted_cache: list[tuple[int, int]] | None = None,
    ) -> tuple[PhaseRunner, _FakeProvider]:
        ledger_path = tmp_path / ".atelier" / "ledger.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger = RunLedger(root=ledger_path.parent, agent="test", task="t", domain="d")
        planner = PrefixCachePlanner()
        diag = PrefixCacheDiagnostics()
        provider = _FakeProvider(scripted_cache=scripted_cache)
        runner = PhaseRunner(
            _build_plan(),
            provider=provider,
            ledger=ledger,
            planner=planner,
            diag=diag,
        )
        return runner, provider

    return _make


# 13-01-01 — LINEAR-01
def test_models_have_required_fields() -> None:
    phase = Phase(
        name="survey",
        kind="agent",
        profile="reader",
        objective_path="survey.md",
        continue_from=None,
        next="plan",
    )
    assert phase.name == "survey"
    assert phase.kind == "agent"
    assert phase.profile == "reader"
    assert phase.continue_from is None
    assert phase.next == "plan"
    assert phase.to_dict()["name"] == "survey"

    plan = PhasePlan(name="p", entry="survey", phases={"survey": phase})
    assert plan.entry == "survey"
    assert "survey" in plan.phases

    stats = PhaseCacheStats(
        prefix_hash="abc",
        prefix_tokens=100,
        dynamic_tokens=10,
        total_tokens=110,
    )
    assert stats.cache_read_input_tokens == 0
    assert stats.cache_creation_input_tokens == 0
    assert stats.invalidated_reason == ""
    assert stats.minify_deltas == []
    d = stats.to_dict()
    assert d["prefix_hash"] == "abc"

    result = PhaseResult(
        phase_name="survey",
        messages=[{"role": "system", "content": "hi"}],
        cache_stats=stats,
        output_text="out",
    )
    assert result.phase_name == "survey"
    assert result.to_dict()["output_text"] == "out"

    assert RunMode.LINEAR.value == "linear"
    assert RunMode.PER_AGENT.value == "per_agent"
    assert RunMode.AUTO.value == "auto"


# 13-01-02 — LINEAR-01
def test_state_machine_schema() -> None:
    plan = _build_plan()
    order = list(plan.iter_order())
    assert order == ["survey", "plan", "implement"]
    assert plan.phases["plan"].continue_from == "survey"
    assert plan.phases["implement"].continue_from is None
    assert plan.phases["implement"].next is None


# 13-01-03 — LINEAR-02, D-04
def test_plan_continues_survey_messages(runner_factory) -> None:
    runner, provider = runner_factory()
    runner.run()

    # Expect at least 3 calls (one per phase)
    assert len(provider.calls) >= 3
    survey_first_call = provider.calls[0]
    plan_first_call = provider.calls[1]

    # Plan-phase first call must begin with Survey's tail messages verbatim.
    survey_tail_len = len(survey_first_call)
    assert (
        plan_first_call[:survey_tail_len] == survey_first_call
    ), "plan-phase first call must include survey-tail messages bytewise (D-04)"
    # Plan call must also have appended its own objective.
    assert len(plan_first_call) > survey_tail_len


# 13-01-04 — LINEAR-02, D-06
def test_system_prompt_byte_stable(runner_factory) -> None:
    runner, provider = runner_factory()
    runner.run()

    assert provider.calls, "provider was never called"
    first_system = provider.calls[0][0]
    assert first_system["role"] == "system"
    h = hashlib.sha256(first_system["content"].encode("utf-8")).hexdigest()
    for call in provider.calls:
        assert call[0]["role"] == "system"
        this_hash = hashlib.sha256(call[0]["content"].encode("utf-8")).hexdigest()
        assert this_hash == h, "system prompt drifted across calls (D-06)"


# 13-01-05 — LINEAR-02, D-07
def test_breakpoint_per_phase_tail(runner_factory) -> None:
    runner, _provider = runner_factory()
    runner.run()

    # PrefixCacheDiagnostics.turn_count counts record_plan invocations
    assert runner.diag.turn_count == 3, f"expected exactly one breakpoint per phase tail; got {runner.diag.turn_count}"


# 13-01-06 — LINEAR-02, D-05 + T-13-01
def test_implement_starts_lean(runner_factory) -> None:
    runner, provider = runner_factory()
    runner.run()

    assert len(provider.calls) >= 3
    implement_first_call = provider.calls[2]
    assert len(implement_first_call) == 2, "implement phase must start lean: [system, user(objective)] only (D-05)"
    assert implement_first_call[0]["role"] == "system"
    assert implement_first_call[1]["role"] == "user"

    # Tool-profile enforcement (T-13-01)
    survey_phase = runner.plan.phases["survey"]
    implement_phase = runner.plan.phases["implement"]
    assert "write" in _WRITER_TOOLS
    assert "write" not in _READER_TOOLS
    assert "write" in runner._allowed_tools(implement_phase)
    assert "write" not in runner._allowed_tools(survey_phase)

    # Dispatcher rejects writes under reader profile
    with pytest.raises(PermissionError):
        runner._dispatch_tool(survey_phase, "write", {})
    # Writer profile accepts writes (returns without raising)
    runner._dispatch_tool(implement_phase, "write", {})
