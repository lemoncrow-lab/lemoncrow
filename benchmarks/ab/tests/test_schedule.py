"""Tests for ab.schedule — AB-02 (interleaving) and AB-04 (determinism)."""

from itertools import product

from ab.schedule import build_schedule


def test_schedule_length():
    schedule = build_schedule(["t1", "t2", "t3"], ["on", "off"], 5, seed=42)
    assert len(schedule) == 3 * 2 * 5  # 30
    assert all(isinstance(t, tuple) and len(t) == 3 for t in schedule)


def test_schedule_determinism_ab04():
    s1 = build_schedule(["a", "b", "c"], ["on", "off"], 3, seed=42)
    s2 = build_schedule(["a", "b", "c"], ["on", "off"], 3, seed=42)
    assert s1 == s2, "same seed must produce identical schedule"
    s_other = build_schedule(["a", "b", "c"], ["on", "off"], 3, seed=99)
    assert s_other != s1, "different seed should produce different schedule"


def test_schedule_rep_by_rep_interleaving_ab02():
    schedule = build_schedule(["t1", "t2"], ["on", "off"], 4, seed=42)
    for n in range(2, 5):
        last_prev = max(i for i, (_, _, r) in enumerate(schedule) if r == n - 1)
        first_curr = min(i for i, (_, _, r) in enumerate(schedule) if r == n)
        assert first_curr > last_prev, f"rep {n-1} not fully before rep {n}"


def test_schedule_contains_all_cells():
    tasks = ["x", "y"]
    modes = ["on", "off"]
    n_reps = 3
    schedule = build_schedule(tasks, modes, n_reps, seed=42)
    expected = {(t, m, r) for t, m, r in product(tasks, modes, range(1, n_reps + 1))}
    assert set(schedule) == expected, "not all (task, mode, rep) cells are present"


def test_schedule_modes_not_shuffled():
    schedule = build_schedule(["t1"], ["on", "off"], 2, seed=42)
    for rep in [1, 2]:
        rep_entries = [(t, m, r) for t, m, r in schedule if r == rep]
        modes_in_rep = [m for _, m, _ in rep_entries]
        assert modes_in_rep == ["on", "off"], f"modes reordered in rep {rep}: {modes_in_rep}"
