"""Shared pytest fixtures for the jobrunner test suite.

Everything here is deterministic: time is driven by a :class:`FakeClock` that
only moves when a step (or the test) advances it. Nothing in the library or the
tests ever sleeps.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from jobrunner import Step


class FakeClock:
    """A controllable, callable clock for deterministic time in tests.

    Call the instance to read the current time; use :meth:`advance` to move it
    forward. It never advances on its own.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> float:
        """Move the clock forward by ``dt`` and return the new time."""
        self._t += float(dt)
        return self._t


@pytest.fixture
def fake_clock() -> FakeClock:
    """A fresh :class:`FakeClock` starting at 0.0."""
    return FakeClock()


@pytest.fixture
def make_step() -> Callable[..., Step]:
    """Factory for steps whose work is "advance the clock by ``cost``".

    Usage: ``make_step("a", 5, fake_clock)`` builds a :class:`~jobrunner.Step`
    named ``"a"`` whose function advances ``fake_clock`` by ``5`` and returns the
    step's name. This models a step that "takes ``cost`` time" without sleeping.
    """

    def _make(name: str, cost: float, clock: FakeClock, retries: int = 0) -> Step:
        def func(ctx: object) -> str:
            clock.advance(cost)
            return name

        return Step(name, func, retries=retries)

    return _make
