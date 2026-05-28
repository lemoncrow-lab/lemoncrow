"""Tests for AtelierRuntimeCore.run_phased mode dispatch — LINEAR-04.

Covers D-12, D-13 (and D-14 via the per-agent ledger-writing path):

* Explicit ``RunMode.LINEAR`` and ``RunMode.PER_AGENT`` are honored exactly.
* ``RunMode.AUTO`` picks LINEAR for context-sharing scenarios that fit under
  ``LINEAR_PREFIX_THRESHOLD``.
* ``RunMode.AUTO`` falls back to PER_AGENT when ``projected_prefix_tokens``
  exceeds the threshold (D-13: oversized prefix).
* ``RunMode.AUTO`` falls back to PER_AGENT when ``divergence_signal`` is True
  (D-13: divergent contexts).
* The PER_AGENT path writes one RunLedger row per phase so the Plan 13-04
  benchmark's per-agent arm has comparable telemetry (D-14).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from atelier.core.capabilities.context_reuse.models import (
    Phase,
    PhasePlan,
    RunMode,
)
from atelier.core.runtime import AtelierRuntimeCore
from atelier.core.runtime.engine import LINEAR_PREFIX_THRESHOLD
from atelier.infra.runtime.run_ledger import RunLedger


def _build_plan() -> PhasePlan:
    """Canonical survey→plan→implement DAG (mirrors test_phase_runner)."""
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


class _StubRunner:
    """Minimal stand-in for PhaseRunner.run()."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self) -> dict[str, Any]:
        self.calls += 1
        return {}


class _FakeProvider:
    """Returns canned (text, in_tok, out_tok, cache_read, cache_write)."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> tuple[str, int, int, int, int]:
        self.calls.append(list(messages))
        return ("ok <phase-complete>", 100, 50, 0, 25)


def _make_runtime(tmp_path: Path) -> AtelierRuntimeCore:
    root = tmp_path / ".atelier"
    return AtelierRuntimeCore(root)


# 13-03-01 — LINEAR-04 / D-12
def test_explicit_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_runtime(tmp_path)
    plan = _build_plan()

    stub = _StubRunner()
    per_agent_calls: list[int] = []

    monkeypatch.setattr(rt, "_build_phase_runner", lambda p: stub)
    monkeypatch.setattr(
        rt,
        "_run_per_agent",
        lambda p: (per_agent_calls.append(1), {})[1],
    )

    res_linear = rt.run_phased(plan, mode=RunMode.LINEAR)
    assert res_linear["mode"] == "linear"
    assert stub.calls == 1
    assert per_agent_calls == []

    res_per_agent = rt.run_phased(plan, mode=RunMode.PER_AGENT)
    assert res_per_agent["mode"] == "per_agent"
    # Linear-stub must NOT have been invoked again.
    assert stub.calls == 1
    assert per_agent_calls == [1]


# 13-03-02 — LINEAR-04 / D-12
def test_auto_picks_linear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_runtime(tmp_path)
    plan = _build_plan()
    stub = _StubRunner()
    monkeypatch.setattr(rt, "_build_phase_runner", lambda p: stub)
    monkeypatch.setattr(rt, "_run_per_agent", lambda p: {})

    # 1000 is well under the threshold.
    assert LINEAR_PREFIX_THRESHOLD > 1000
    res = rt.run_phased(
        plan,
        mode=RunMode.AUTO,
        projected_prefix_tokens=1000,
        divergence_signal=False,
    )
    assert res["mode"] == "linear"
    assert stub.calls == 1


# 13-03-03 — LINEAR-04 / D-13
def test_auto_falls_back_oversized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_runtime(tmp_path)
    plan = _build_plan()
    stub = _StubRunner()
    per_agent_calls: list[int] = []
    monkeypatch.setattr(rt, "_build_phase_runner", lambda p: stub)
    monkeypatch.setattr(
        rt,
        "_run_per_agent",
        lambda p: (per_agent_calls.append(1), {})[1],
    )

    res = rt.run_phased(
        plan,
        mode=RunMode.AUTO,
        projected_prefix_tokens=LINEAR_PREFIX_THRESHOLD + 1,
        divergence_signal=False,
    )
    assert res["mode"] == "per_agent"
    assert stub.calls == 0
    assert per_agent_calls == [1]


# 13-03-04 — LINEAR-04 / D-13
def test_auto_falls_back_divergent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_runtime(tmp_path)
    plan = _build_plan()
    stub = _StubRunner()
    per_agent_calls: list[int] = []
    monkeypatch.setattr(rt, "_build_phase_runner", lambda p: stub)
    monkeypatch.setattr(
        rt,
        "_run_per_agent",
        lambda p: (per_agent_calls.append(1), {})[1],
    )

    res = rt.run_phased(
        plan,
        mode=RunMode.AUTO,
        projected_prefix_tokens=0,
        divergence_signal=True,
    )
    assert res["mode"] == "per_agent"
    assert stub.calls == 0
    assert per_agent_calls == [1]


# 13-03-05 — D-14: per-agent arm writes one ledger row per phase
def test_per_agent_writes_ledger(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    plan = _build_plan()

    provider = _FakeProvider()
    ledger_root = tmp_path / ".atelier"
    ledger_root.mkdir(parents=True, exist_ok=True)
    ledger = RunLedger(root=ledger_root, agent="test", task="t", domain="d")

    # Inject provider + ledger via the engine's per-agent wiring points.
    rt._provider = provider  # type: ignore[attr-defined]
    rt._ledger = ledger  # type: ignore[attr-defined]

    res = rt.run_phased(plan, mode=RunMode.PER_AGENT)
    assert res["mode"] == "per_agent"

    # One provider call per phase (3 phases).
    assert len(provider.calls) == 3

    # Ledger has exactly 3 llm_call rows, one per phase, all cache_read=0.
    llm_events = [e for e in ledger.events if e.detail.get("kind") == "llm_call"]
    assert len(llm_events) == 3
    phases_seen = [e.detail.get("phase") for e in llm_events]
    assert sorted(phases_seen) == ["implement", "plan", "survey"]
    for ev in llm_events:
        assert ev.detail.get("cache_read_tokens") == 0
