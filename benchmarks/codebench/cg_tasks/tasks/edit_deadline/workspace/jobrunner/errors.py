"""Exception hierarchy for the :mod:`jobrunner` package.

All exceptions raised by the library derive from :class:`JobError`, so callers
can catch a single base type if they do not care about the specific failure
mode.  The two concrete errors carry structured context (the offending step
name and, where relevant, timing information) so that callers can build good
diagnostics without having to parse human-readable messages.
"""

from __future__ import annotations


class JobError(Exception):
    """Base class for every error raised by :mod:`jobrunner`."""


class StepFailed(JobError):
    """Raised when a step exhausts its retries and still fails.

    Attributes:
        step_name: Name of the step that failed.
        cause: The final underlying exception that caused the failure.
    """

    def __init__(self, step_name: str | None = None, cause: BaseException | None = None) -> None:
        self.step_name = step_name
        self.cause = cause
        super().__init__(self._message())

    def _message(self) -> str:
        return f"step {self.step_name!r} failed: {self.cause!r}"

    def __str__(self) -> str:
        return self._message()


class DeadlineExceeded(JobError):
    """Raised (in strict mode) when a pipeline runs past its deadline.

    Attributes:
        step_name: The first step that was *not* run because the deadline had
            already passed (the "tripped" step).
        elapsed: Wall-clock time elapsed when the deadline tripped.
        deadline: The absolute deadline value that was exceeded.
    """

    def __init__(
        self,
        step_name: str | None = None,
        elapsed: float | None = None,
        deadline: float | None = None,
    ) -> None:
        self.step_name = step_name
        self.elapsed = elapsed
        self.deadline = deadline
        super().__init__(self._message())

    def _message(self) -> str:
        return (
            f"deadline exceeded before step {self.step_name!r} " f"(elapsed={self.elapsed}, deadline={self.deadline})"
        )

    def __str__(self) -> str:
        return self._message()
