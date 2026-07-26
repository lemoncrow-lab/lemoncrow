"""Execution context shared by every step in a pipeline run.

A :class:`Context` is created once per :func:`jobrunner.run_pipeline` call (or
manually, when driving a :class:`~jobrunner.pipeline.Pipeline` directly).  It
carries the immutable *inputs*, the accumulating per-step *outputs*, and the
timing state used by the deadline feature.

The clock is injectable so that tests can drive time deterministically without
sleeping.  Any zero-argument callable returning a float works; the default is
:func:`time.monotonic`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class Context:
    """State passed to every step's function during a pipeline run.

    Args:
        inputs: Read-mostly inputs for the run. Copied defensively.
        outputs: Optional seed for per-step outputs (keyed by step name).
        clock: Zero-arg callable returning the current time as a float.
        started_at: Time the run started. Defaults to ``clock()``.
        deadline: Absolute time (same units as ``clock``) after which the run is
            considered expired, or ``None`` for no deadline.
    """

    def __init__(
        self,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        started_at: float | None = None,
        deadline: float | None = None,
    ) -> None:
        self.inputs: dict[str, Any] = dict(inputs or {})
        self.outputs: dict[str, Any] = dict(outputs or {})
        self.clock: Callable[[], float] = clock
        self.started_at: float = clock() if started_at is None else started_at
        self.deadline: float | None = deadline

    def get(self, key: str, default: Any = None) -> Any:
        """Return ``inputs[key]`` if present, else ``default``."""
        return self.inputs.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key`` in :attr:`inputs`."""
        self.inputs[key] = value

    def elapsed(self) -> float:
        """Seconds elapsed since :attr:`started_at` according to the clock."""
        return self.clock() - self.started_at

    def remaining(self) -> float | None:
        """Seconds until the deadline, or ``None`` when no deadline is set.

        The value may be zero or negative once the deadline has passed.
        """
        if self.deadline is None:
            return None
        return self.deadline - self.clock()

    def expired(self) -> bool:
        """Whether the deadline has been reached.

        The boundary is inclusive: ``clock() >= deadline`` counts as expired.
        A context with no deadline never expires.
        """
        if self.deadline is None:
            return False
        return self.clock() >= self.deadline

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Context(inputs={self.inputs!r}, outputs={self.outputs!r}, "
            f"started_at={self.started_at!r}, deadline={self.deadline!r})"
        )
