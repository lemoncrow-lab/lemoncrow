"""T5: threshold-triggered history compaction gating.

Covers the pure decision helper ``should_compact`` and the
``ATELIER_AUTO_COMPACT`` gating wired into
``AtelierRuntimeCore.summarize_memory``.

DEFAULT-OFF flag ``ATELIER_AUTO_COMPACT`` (see
``docs-internal/rollout/feature-flag-rollout.md``): off == current behavior
(unconditional compress); on == compress only once live fill reaches the
policy trigger fraction. Headless and fail-open.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.optimization.audit import context_window_for_model
from atelier.core.capabilities.optimization.policy import (
    CompactionPolicy,
    preset_policy,
    should_compact,
)
from atelier.core.runtime import AtelierRuntimeCore
from atelier.infra.runtime.run_ledger import RunLedger
from tests.helpers import init_store_at

# Default "balanced" preset trigger fraction (policy._base_compaction default 0.72).
BALANCED = preset_policy("balanced").compaction
FRACTION = BALANCED.trigger_at_context_fraction


@pytest.fixture(autouse=True)
def _entitle_savings_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    # This module exercises the Pro savings engine (compaction policy); treat the
    # install as licensed so load_current_policy returns the real policy.
    monkeypatch.setattr("atelier.core.capabilities.licensing.has_feature", lambda *a, **k: True)


# --------------------------------------------------------------------------- #
# 1) Pure helper: below / at / above the threshold
# --------------------------------------------------------------------------- #


def test_should_compact_below_threshold_is_false() -> None:
    assert should_compact(FRACTION - 0.01, BALANCED) is False


def test_should_compact_at_threshold_is_true() -> None:
    assert should_compact(FRACTION, BALANCED) is True


def test_should_compact_above_threshold_is_true() -> None:
    assert should_compact(FRACTION + 0.01, BALANCED) is True


def test_should_compact_respects_custom_fraction() -> None:
    policy = CompactionPolicy(
        prompt_cache_reorder=False,
        dedup=False,
        retrieval_filter=False,
        lossy_summary=False,
        trigger_at_context_fraction=0.5,
        preserve=[],
    )
    assert should_compact(0.49, policy) is False
    assert should_compact(0.50, policy) is True
    assert should_compact(0.51, policy) is True


# --------------------------------------------------------------------------- #
# summarize_memory gating fixtures
# --------------------------------------------------------------------------- #


def _runtime_and_session(tmp_path: Path) -> tuple[AtelierRuntimeCore, str]:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    rt = AtelierRuntimeCore(root)
    ledger = RunLedger(root=root, agent="test", task="t", domain="d")
    ledger.record_command("pytest", ok=False, error_signature="same")
    ledger.persist(root)
    return rt, ledger.session_id


class _SpyCompressor:
    """Stand-in for the context-compression capability that records calls."""

    def __init__(self) -> None:
        self.calls = 0

    def compress(self, ledger: RunLedger) -> dict[str, object]:
        self.calls += 1
        return {"compacted": True}


def _wire(rt: AtelierRuntimeCore, *, fill: float) -> _SpyCompressor:
    spy = _SpyCompressor()
    rt.context_compression = spy  # type: ignore[assignment]
    rt._live_context_fill = lambda ledger: fill  # type: ignore[assignment,method-assign]
    return spy


# --------------------------------------------------------------------------- #
# 2) Flag ON: compress fires only when fill >= fraction; skipped below
# --------------------------------------------------------------------------- #


def test_flag_on_compress_fires_at_or_above_fraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_AUTO_COMPACT", "1")
    rt, session_id = _runtime_and_session(tmp_path)
    spy = _wire(rt, fill=FRACTION)

    summary = rt.summarize_memory(session_id=session_id)

    assert spy.calls == 1
    assert summary["compacted"] is True
    assert summary["session_id"] == session_id
    assert "compacted" in summary


def test_flag_on_compress_skipped_below_fraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_AUTO_COMPACT", "1")
    rt, session_id = _runtime_and_session(tmp_path)
    spy = _wire(rt, fill=FRACTION - 0.1)

    summary = rt.summarize_memory(session_id=session_id)

    assert spy.calls == 0
    assert summary["compacted"] is False
    # Contract preserved even when compaction is skipped.
    assert summary["session_id"] == session_id
    assert "compacted" in summary


# --------------------------------------------------------------------------- #
# 3) Flag OFF: preserves current behavior (compress regardless of fill)
# --------------------------------------------------------------------------- #


def test_flag_off_preserves_current_behavior_low_fill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_AUTO_COMPACT", raising=False)
    rt, session_id = _runtime_and_session(tmp_path)
    # Fill far below the trigger; off == unconditional compress today.
    spy = _wire(rt, fill=0.0)

    summary = rt.summarize_memory(session_id=session_id)

    assert spy.calls == 1
    assert summary["compacted"] is True
    assert summary["session_id"] == session_id
    assert "compacted" in summary


def test_flag_off_explicit_false_value_preserves_current_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATELIER_AUTO_COMPACT", "0")
    rt, session_id = _runtime_and_session(tmp_path)
    spy = _wire(rt, fill=0.0)

    summary = rt.summarize_memory(session_id=session_id)

    assert spy.calls == 1
    assert summary["compacted"] is True


# --------------------------------------------------------------------------- #
# 4) Fail-open: gate errors fall through to prior behavior (compress)
# --------------------------------------------------------------------------- #


def test_flag_on_fail_open_when_fill_computation_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_AUTO_COMPACT", "1")
    rt, session_id = _runtime_and_session(tmp_path)
    spy = _SpyCompressor()
    rt.context_compression = spy  # type: ignore[assignment]

    def _boom(ledger: RunLedger) -> float:
        raise RuntimeError("fill computation exploded")

    rt._live_context_fill = _boom  # type: ignore[assignment,method-assign]

    summary = rt.summarize_memory(session_id=session_id)

    # Any gating error must fall through to the prior behavior (compress),
    # never crash the turn.
    assert spy.calls == 1
    assert summary["compacted"] is True
    assert summary["session_id"] == session_id


# --------------------------------------------------------------------------- #
# 5) _live_context_fill reflects RECENT occupancy, not the cumulative sum
# --------------------------------------------------------------------------- #


def _llm_call(ledger: RunLedger, *, model: str, input_tokens: int) -> None:
    # The helper inspects event.payload["kind"] == "llm_call"; the event's own
    # kind is an unrelated allowed Literal.
    ledger.record(
        "note",
        "llm call",
        {"kind": "llm_call", "model": model, "input_tokens": input_tokens},
    )


def test_live_context_fill_uses_recent_occupancy_not_cumulative_sum(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    rt = AtelierRuntimeCore(root)

    model = "claude-sonnet"
    window = context_window_for_model(model)

    ledger = RunLedger(root=root, agent="test", task="t", domain="d")
    # Several historical calls whose cumulative sum exceeds the window; the most
    # recent live occupancy is a small fraction of it.
    for _ in range(5):
        _llm_call(ledger, model=model, input_tokens=int(window * 0.5))
    recent = int(window * 0.10)
    _llm_call(ledger, model=model, input_tokens=recent)

    fill = rt._live_context_fill(ledger)

    # Fill tracks the most-recent call (~0.10), NOT the monotonic cumulative
    # sum (which would be ~2.6 and is clearly nonsensical as a fraction).
    assert fill == pytest.approx(recent / window, rel=1e-6)
    assert fill < 0.2


def test_live_context_fill_empty_ledger_is_zero(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    rt = AtelierRuntimeCore(root)
    ledger = RunLedger(root=root, agent="test", task="t", domain="d")
    assert rt._live_context_fill(ledger) == 0.0
