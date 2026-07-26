"""The high-level entry point: :func:`run_pipeline`.

This wraps the lower-level :class:`~jobrunner.pipeline.Pipeline` /
:class:`~jobrunner.context.Context` machinery in a single convenient call.  It
is responsible for two things beyond simply running the steps:

* Building the context, including resolving the *deadline*.  A caller may pass
  an absolute ``deadline`` or a relative ``timeout``; ``deadline`` wins if both
  are given, and if neither is given the run has no deadline.
* Enforcing *strict* mode.  When ``strict=True`` and the run tripped a deadline,
  a :class:`~jobrunner.errors.DeadlineExceeded` is raised instead of returning a
  report with ``deadline_exceeded`` set.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .context import Context
from .errors import DeadlineExceeded
from .pipeline import Pipeline
from .report import Report
from .step import Step


def run_pipeline(
    steps: list[Step],
    inputs: dict | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
    timeout: float | None = None,
    strict: bool = False,
) -> Report:
    """Run ``steps`` as a pipeline and return the resulting :class:`Report`.

    Args:
        steps: The steps to run, in order.
        inputs: Optional inputs made available to every step via the context.
        clock: Injectable time source (zero-arg callable returning a float).
        deadline: Absolute deadline (in ``clock`` units). Takes precedence over
            ``timeout`` when both are provided.
        timeout: Relative deadline: ``started_at + timeout``. Used only when
            ``deadline`` is ``None``.
        strict: When True, raise :class:`~jobrunner.errors.DeadlineExceeded` if
            the run trips a deadline instead of returning the report.

    Returns:
        The :class:`~jobrunner.report.Report` for the run (unless ``strict`` and
        the deadline tripped, in which case an exception is raised).

    Raises:
        DeadlineExceeded: When ``strict`` is True and the run tripped a deadline.
    """
    started_at = clock()
    resolved_deadline = _resolve_deadline(started_at, deadline, timeout)
    ctx = Context(
        inputs=inputs or {},
        clock=clock,
        started_at=started_at,
        deadline=resolved_deadline,
    )
    pipeline = Pipeline(steps)
    report = pipeline.run(ctx)
    if strict and report.deadline_exceeded:
        raise DeadlineExceeded(
            step_name=report.tripped_step,
            elapsed=clock() - started_at,
            deadline=resolved_deadline,
        )
    return report


def _resolve_deadline(started_at: float, deadline: float | None, timeout: float | None) -> float | None:
    """Resolve an absolute deadline from the ``deadline``/``timeout`` inputs.

    ``deadline`` wins when both are given; ``timeout`` is interpreted relative to
    ``started_at``; if neither is given the result is ``None`` (no deadline).
    """
    if deadline is not None:
        return deadline
    if timeout is not None:
        return started_at + timeout
    return None
