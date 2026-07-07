"""Unit tests for the honest per-file baseline-avoidance cap (pure functions).

The contract: the FIRST outline/range read of a file credits its full-baseline
saving; every later outline/range read of the SAME file is NOT credited (you can
only avoid reading a file once). Full-mode reads are never capped.
"""

from __future__ import annotations

from typing import Any

import pytest

from atelier.core.capabilities import read_baseline_credit as rbc


def test_first_outline_read_is_credited() -> None:
    state: dict[str, Any] = {}
    state, credit = rbc.should_credit(state, "src/a.py", "outline")
    assert credit is True


def test_second_baseline_read_same_file_is_not_credited() -> None:
    state: dict[str, Any] = {}
    state, first = rbc.should_credit(state, "src/a.py", "outline")
    state, second = rbc.should_credit(state, "src/a.py", "range")
    assert first is True
    assert second is False  # baseline already counted -> would double-count


def test_full_mode_is_never_capped() -> None:
    # full-mode saving is minification, not baseline avoidance -> untouched.
    state: dict[str, Any] = {}
    state, a = rbc.should_credit(state, "src/a.py", "full")
    state, b = rbc.should_credit(state, "src/a.py", "full")
    assert a is True and b is True


def test_distinct_files_each_credited() -> None:
    state: dict[str, Any] = {}
    state, a = rbc.should_credit(state, "src/a.py", "outline")
    state, b = rbc.should_credit(state, "src/b.py", "range")
    assert a is True and b is True


def test_abs_then_rel_same_file_collapses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", "/work")
    state: dict[str, Any] = {}
    state, first = rbc.should_credit(state, "/work/src/a.py", "outline")
    state, second = rbc.should_credit(state, "src/a.py", "range")
    assert first is True
    assert second is False


def test_should_credit_path_shares_one_ledger_with_reads() -> None:
    # A file claimed by the code counterfactual blocks the read baseline credit…
    state: dict[str, Any] = {}
    state, credit = rbc.should_credit_path(state, "src/a.py")
    assert credit is True
    state, credit = rbc.should_credit(state, "src/a.py", "outline")
    assert credit is False
    # …and vice versa: an outline-read file blocks a later counterfactual claim.
    state, credit = rbc.should_credit(state, "src/b.py", "range")
    assert credit is True
    state, credit = rbc.should_credit_path(state, "src/b.py")
    assert credit is False


def test_should_credit_path_once_per_session() -> None:
    state: dict[str, Any] = {}
    state, first = rbc.should_credit_path(state, "src/c.py")
    state, second = rbc.should_credit_path(state, "src/c.py")
    assert first is True
    assert second is False


def test_reset_clears() -> None:
    state: dict[str, Any] = {}
    state, _ = rbc.should_credit(state, "src/a.py", "outline")
    state = rbc.reset(state)
    state, again = rbc.should_credit(state, "src/a.py", "outline")
    assert again is True


def test_tolerates_garbage() -> None:
    assert rbc.should_credit(None, "src/a.py", "outline") == (None, True)  # type: ignore[arg-type]
    state: dict[str, Any] = {}
    _, credit = rbc.should_credit(state, "", "outline")
    assert credit is True
    _, credit2 = rbc.should_credit(state, None, "outline")
    assert credit2 is True
