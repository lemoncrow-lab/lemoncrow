"""The high-level entry point: :func:`run_pipeline`.

This wraps the lower-level :class:`~jobrunner.pipeline.Pipeline` /
:class:`~jobrunner.context.Context` machinery in a single convenient call.
"""

from __future__ import annotations

from .context import Context
from .pipeline import Pipeline
from .report import Report
from .step import Step


def run_pipeline(steps: list[Step], inputs: dict | None = None) -> Report:
    """Run ``steps`` as a pipeline and return the resulting :class:`Report`.

    Args:
        steps: The steps to run, in order.
        inputs: Optional inputs made available to every step via the context.

    Returns:
        The :class:`~jobrunner.report.Report` for the run.
    """
    ctx = Context(inputs=inputs or {})
    pipeline = Pipeline(steps)
    return pipeline.run(ctx)
