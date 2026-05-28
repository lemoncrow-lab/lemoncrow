"""Interleaved A/B execution schedule — AB-02 + AB-04."""

import random

__all__ = ["build_schedule"]


def build_schedule(
    task_ids: list[str],
    modes: list[str],
    n_reps: int,
    seed: int = 42,
) -> list[tuple[str, str, int]]:
    """Return an interleaved (task_id, mode, rep) schedule.

    Outer loop is rep (AB-02): all rep-1 entries appear before any rep-2 entry.
    Within each rep, task order is a seeded shuffle (AB-04): same seed → same order.
    Modes are NOT shuffled — they appear in the order passed.
    """
    rng = random.Random(seed)
    schedule: list[tuple[str, str, int]] = []
    for rep in range(1, n_reps + 1):
        shuffled_tasks = rng.sample(task_ids, len(task_ids))
        for task_id in shuffled_tasks:
            for mode in modes:
                schedule.append((task_id, mode, rep))
    return schedule
