"""A single unit of work in a pipeline.

A :class:`Step` wraps a callable that takes the shared :class:`~jobrunner.context.Context`
and returns an arbitrary result.  Steps support a simple retry policy: if the
callable raises, it is retried up to ``retries`` additional times.  When all
attempts are exhausted the original exception is wrapped in a
:class:`~jobrunner.errors.StepFailed` carrying the step name and the final cause.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .context import Context
from .errors import StepFailed


class Step:
    """A named, retryable unit of work.

    Args:
        name: Unique (within a pipeline) identifier for the step.
        func: Callable invoked as ``func(ctx)``; its return value is the output.
        retries: Number of *additional* attempts after the first (>= 0).
    """

    def __init__(self, name: str, func: Callable[[Context], Any], retries: int = 0) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("step name must be a non-empty string")
        if not callable(func):
            raise TypeError("step func must be callable")
        if retries < 0:
            raise ValueError("retries must be >= 0")
        self.name = name
        self.func = func
        self.retries = retries

    def execute(self, ctx: Context) -> Any:
        """Run the step, retrying on failure.

        Returns:
            Whatever ``func(ctx)`` returns on the first successful attempt.

        Raises:
            StepFailed: If every attempt (1 + ``retries``) raises. The final
                underlying exception is attached as ``StepFailed.cause``.
        """
        attempts = self.retries + 1
        last_exc: BaseException | None = None
        for _ in range(attempts):
            try:
                return self.func(ctx)
            except StepFailed:
                # Already a domain error - propagate without double-wrapping.
                raise
            except Exception as exc:
                last_exc = exc
        raise StepFailed(step_name=self.name, cause=last_exc)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Step(name={self.name!r}, retries={self.retries!r})"
