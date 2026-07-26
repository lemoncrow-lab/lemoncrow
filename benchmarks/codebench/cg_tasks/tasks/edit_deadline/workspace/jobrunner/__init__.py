"""jobrunner - a small, testable job-pipeline runner.

Public API:
    run_pipeline: high-level entry point to run a list of steps.
    Pipeline, Step, Context: the building blocks.
    Report, StepRecord: the result types.
    JobError, StepFailed, DeadlineExceeded: the exception hierarchy.
"""

from __future__ import annotations

from .api import run_pipeline
from .context import Context
from .errors import DeadlineExceeded, JobError, StepFailed
from .pipeline import Pipeline
from .report import Report, StepRecord
from .step import Step

__all__ = [
    "Context",
    "DeadlineExceeded",
    "JobError",
    "Pipeline",
    "Report",
    "Step",
    "StepFailed",
    "StepRecord",
    "run_pipeline",
]

__version__ = "0.1.0"
