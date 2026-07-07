"""Tests for DifficultyFSM and score_step.

Covers:
  - score_step output range and directional correctness
  - FSMState transitions for all valid arcs
  - Hysteresis: single easy step does not exit SLOW
  - SKIP requires sustained very-hard window from SLOW
  - monitor_cooldown_steps and skip_etraces properties
  - advance_many helper
  - reset()
  - summary() dict
"""

import pytest

from atelier.core.capabilities.monitors.fsm import (
    DifficultyFSM,
    FSMState,
    advance_many,
    score_step,
)

# --------------------------------------------------------------------------- #
# score_step                                                                   #
# --------------------------------------------------------------------------- #


def test_score_step_range() -> None:
    for text in [
        "",
        "The fix is done.",
        "Error: null pointer exception. Maybe it crashed again. Uncertain.",
        "Successfully updated 10 files without any issues.",
    ]:
        s = score_step(text)
        assert 0.0 <= s <= 1.0, f"score out of range for: {text!r}"


def test_score_step_empty_text_is_neutral() -> None:
    s = score_step("")
    assert s == 0.5


def test_score_step_heavy_errors_score_higher() -> None:
    clean = "Successfully updated the auth module and all tests pass."
    noisy = "Error: exception raised. Bug in handler. Crash: null pointer. Failed again."
    assert score_step(noisy) > score_step(clean)


def test_score_step_hedging_increases_score() -> None:
    certain = "The root cause is the missing null check in line 42."
    hedgy = "Maybe possibly the issue might be unclear. Not sure what it could be."
    assert score_step(hedgy) > score_step(certain)


# --------------------------------------------------------------------------- #
# INIT → NORMAL                                                                #
# --------------------------------------------------------------------------- #


def test_first_transition_always_normal() -> None:
    fsm = DifficultyFSM()
    assert fsm.current_state == FSMState.INIT
    state = fsm.transition(0.1)
    assert state == FSMState.NORMAL


# --------------------------------------------------------------------------- #
# NORMAL → FAST                                                                #
# --------------------------------------------------------------------------- #


def test_normal_to_fast_after_fast_window() -> None:
    fsm = DifficultyFSM(fast_threshold=0.2, fast_window=3)
    fsm.transition(0.5)  # INIT → NORMAL
    for _ in range(3):
        fsm.transition(0.1)  # 3 consecutive easy steps
    assert fsm.current_state == FSMState.FAST


def test_normal_stays_normal_if_not_enough_easy_steps() -> None:
    fsm = DifficultyFSM(fast_threshold=0.2, fast_window=4)
    fsm.transition(0.5)  # NORMAL
    for _ in range(3):  # one short of fast_window=4
        fsm.transition(0.1)
    assert fsm.current_state == FSMState.NORMAL


# --------------------------------------------------------------------------- #
# NORMAL → SLOW                                                                #
# --------------------------------------------------------------------------- #


def test_normal_to_slow_after_slow_window() -> None:
    fsm = DifficultyFSM(slow_threshold=0.6, slow_window=3)
    fsm.transition(0.5)  # NORMAL
    for _ in range(3):
        fsm.transition(0.8)
    assert fsm.current_state == FSMState.SLOW


def test_normal_stays_normal_if_not_enough_hard_steps() -> None:
    fsm = DifficultyFSM(slow_threshold=0.6, slow_window=4)
    fsm.transition(0.5)  # NORMAL
    for _ in range(3):
        fsm.transition(0.8)
    assert fsm.current_state == FSMState.NORMAL


# --------------------------------------------------------------------------- #
# FAST → NORMAL (hysteresis)                                                   #
# --------------------------------------------------------------------------- #


def test_fast_returns_to_normal_above_hysteresis() -> None:
    fsm = DifficultyFSM(fast_threshold=0.2, hysteresis_margin=0.1, fast_window=3)
    fsm.transition(0.5)  # NORMAL
    for _ in range(3):
        fsm.transition(0.05)  # → FAST
    assert fsm.current_state == FSMState.FAST
    # Score > fast_threshold + margin = 0.3
    state = fsm.transition(0.35)
    assert state == FSMState.NORMAL


def test_fast_stays_fast_below_hysteresis() -> None:
    fsm = DifficultyFSM(fast_threshold=0.2, hysteresis_margin=0.1, fast_window=3)
    fsm.transition(0.5)
    for _ in range(3):
        fsm.transition(0.05)
    assert fsm.current_state == FSMState.FAST
    # Score ≤ 0.3 — stays FAST
    fsm.transition(0.25)
    assert fsm.current_state == FSMState.FAST


# --------------------------------------------------------------------------- #
# SLOW → NORMAL (hysteresis)                                                   #
# --------------------------------------------------------------------------- #


def test_slow_returns_to_normal_below_hysteresis() -> None:
    fsm = DifficultyFSM(slow_threshold=0.6, hysteresis_margin=0.1, slow_window=3)
    fsm.transition(0.5)
    for _ in range(3):
        fsm.transition(0.8)
    assert fsm.current_state == FSMState.SLOW
    # Score < slow_threshold - margin = 0.5
    state = fsm.transition(0.4)
    assert state == FSMState.NORMAL


def test_slow_stays_slow_above_hysteresis() -> None:
    fsm = DifficultyFSM(slow_threshold=0.6, hysteresis_margin=0.1, slow_window=3)
    fsm.transition(0.5)
    for _ in range(3):
        fsm.transition(0.8)
    assert fsm.current_state == FSMState.SLOW
    # Score ≥ 0.5 — stays SLOW
    fsm.transition(0.55)
    assert fsm.current_state == FSMState.SLOW


# --------------------------------------------------------------------------- #
# SLOW → SKIP                                                                  #
# --------------------------------------------------------------------------- #


def test_slow_to_skip_after_skip_window() -> None:
    fsm = DifficultyFSM(
        slow_threshold=0.6,
        slow_window=3,
        skip_threshold=0.85,
        skip_window=5,
    )
    fsm.transition(0.5)  # NORMAL
    for _ in range(3):
        fsm.transition(0.8)  # → SLOW
    assert fsm.current_state == FSMState.SLOW
    state = FSMState.SLOW
    for _ in range(5):
        state = fsm.transition(0.9)  # very hard → SKIP
    assert state == FSMState.SKIP


def test_skip_returns_to_normal_below_hysteresis() -> None:
    fsm = DifficultyFSM(
        slow_threshold=0.6,
        slow_window=3,
        hysteresis_margin=0.1,
        skip_threshold=0.85,
        skip_window=5,
    )
    fsm.transition(0.5)
    for _ in range(3):
        fsm.transition(0.8)
    for _ in range(5):
        fsm.transition(0.9)
    assert fsm.current_state == FSMState.SKIP
    # Drop below 0.5
    state = fsm.transition(0.3)
    assert state == FSMState.NORMAL


# --------------------------------------------------------------------------- #
# Properties                                                                   #
# --------------------------------------------------------------------------- #


def test_skip_etraces_only_in_fast() -> None:
    fsm = DifficultyFSM(fast_window=3, fast_threshold=0.2)
    assert not fsm.skip_etraces  # INIT
    fsm.transition(0.5)
    assert not fsm.skip_etraces  # NORMAL
    for _ in range(3):
        fsm.transition(0.05)
    assert fsm.skip_etraces  # FAST
    state = fsm.transition(0.9)
    assert state == FSMState.NORMAL
    assert not fsm.skip_etraces  # back to NORMAL


def test_monitor_cooldown_steps() -> None:
    fsm = DifficultyFSM(fast_window=3, fast_threshold=0.2, slow_window=3, slow_threshold=0.6)
    assert fsm.monitor_cooldown_steps == 1  # INIT

    fsm.transition(0.5)
    assert fsm.monitor_cooldown_steps == 3  # NORMAL

    for _ in range(3):
        fsm.transition(0.05)
    assert fsm.monitor_cooldown_steps == 5  # FAST

    fsm.transition(0.9)  # NORMAL
    for _ in range(3):
        fsm.transition(0.8)
    assert fsm.monitor_cooldown_steps == 2  # SLOW


# --------------------------------------------------------------------------- #
# advance_many                                                                 #
# --------------------------------------------------------------------------- #


def test_advance_many_empty_steps() -> None:
    fsm = DifficultyFSM()
    state = advance_many(fsm, [])
    assert state == FSMState.INIT


def test_advance_many_processes_all_steps() -> None:
    fsm = DifficultyFSM()
    steps = ["Fix it.", "Done."]
    advance_many(fsm, steps)
    assert len(fsm.history) == 2


# --------------------------------------------------------------------------- #
# reset + summary                                                              #
# --------------------------------------------------------------------------- #


def test_reset_clears_history_and_state() -> None:
    fsm = DifficultyFSM()
    fsm.transition(0.5)
    fsm.transition(0.8)
    fsm.reset()
    assert fsm.current_state == FSMState.INIT
    assert fsm.history == []


def test_summary_keys() -> None:
    fsm = DifficultyFSM()
    fsm.transition(0.4)
    s = fsm.summary()
    assert set(s.keys()) == {"state", "steps_recorded", "last_score", "skip_etraces", "monitor_cooldown"}
    assert s["state"] == "NORMAL"
    assert s["steps_recorded"] == 1
    assert s["last_score"] == pytest.approx(0.4, abs=1e-3)
    assert s["skip_etraces"] is False


def test_summary_initial_state() -> None:
    fsm = DifficultyFSM()
    s = fsm.summary()
    assert s["state"] == "INIT"
    assert s["last_score"] is None
