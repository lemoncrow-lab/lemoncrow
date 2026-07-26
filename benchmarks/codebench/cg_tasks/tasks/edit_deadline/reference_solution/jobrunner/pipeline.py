"""The pipeline: an ordered sequence of steps executed against a context.

A :class:`Pipeline` validates its steps up front (non-empty, unique names) and
then runs them in order, producing a :class:`~jobrunner.report.Report`.

Two things can stop a run early:

* A step raising :class:`~jobrunner.errors.StepFailed` (after exhausting its own
  retries).  The failure is recorded and the run stops immediately.
* The deadline tripping.  Before each step, the pipeline checks
  :meth:`~jobrunner.context.Context.expired`.  If the deadline has passed, the
  current step and every step after it are recorded as ``skipped`` (in order),
  the report is marked ``deadline_exceeded``, and the run stops.

The expiry check happens *before* running each step, so a step that is already
in progress is never interrupted mid-flight; only not-yet-started steps are
skipped.  The boundary is inclusive (``clock() >= deadline`` is expired).
"""

from __future__ import annotations

from collections.abc import Iterator

from .context import Context
from .errors import StepFailed
from .report import Report
from .step import Step


class Pipeline:
    """An ordered, validated collection of :class:`~jobrunner.step.Step` objects.

    Args:
        steps: The steps to run, in execution order.

    Raises:
        ValueError: If ``steps`` is empty or contains duplicate names.
        TypeError: If any element is not a :class:`~jobrunner.step.Step`.
    """

    def __init__(self, steps: list[Step]) -> None:
        self._validate(steps)
        self.steps: list[Step] = list(steps)

    @classmethod
    def of(cls, *steps: Step) -> Pipeline:
        """Build a pipeline from positional steps: ``Pipeline.of(a, b, c)``."""
        return cls(list(steps))

    @staticmethod
    def _validate(steps: list[Step]) -> None:
        """Validate the step list, raising on the first problem found."""
        if not steps:
            raise ValueError("pipeline must contain at least one step")
        seen: set[str] = set()
        for step in steps:
            if not isinstance(step, Step):
                raise TypeError(f"expected Step, got {type(step).__name__}")
            if step.name in seen:
                raise ValueError(f"duplicate step name: {step.name!r}")
            seen.add(step.name)

    def validate(self) -> None:
        """Re-run validation against the current steps (raises on problems)."""
        self._validate(self.steps)

    # -- introspection -----------------------------------------------------

    @property
    def names(self) -> list[str]:
        """The step names in execution order."""
        return [step.name for step in self.steps]

    @property
    def positions(self) -> dict[str, int]:
        """A mapping of step name to its 0-based position in the pipeline."""
        return {step.name: i for i, step in enumerate(self.steps)}

    @property
    def first(self) -> Step:
        """The first step in the pipeline."""
        return self.steps[0]

    @property
    def last(self) -> Step:
        """The last step in the pipeline."""
        return self.steps[-1]

    @property
    def retry_budget(self) -> int:
        """Total number of retries configured across all steps."""
        return sum(step.retries for step in self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    def __iter__(self) -> Iterator[Step]:
        return iter(self.steps)

    def __contains__(self, name: object) -> bool:
        return any(step.name == name for step in self.steps)

    def __getitem__(self, index: int) -> Step:
        return self.steps[index]

    def get(self, name: str) -> Step | None:
        """Return the step named ``name``, or ``None`` if absent."""
        for step in self.steps:
            if step.name == name:
                return step
        return None

    def require(self, name: str) -> Step:
        """Return the step named ``name`` or raise :class:`KeyError`."""
        step = self.get(name)
        if step is None:
            raise KeyError(name)
        return step

    def index(self, name: str) -> int:
        """Return the position of the step named ``name``.

        Raises:
            KeyError: If no step has that name.
        """
        for i, step in enumerate(self.steps):
            if step.name == name:
                return i
        raise KeyError(name)

    def plan(self) -> list[str]:
        """Return the names of the steps that *would* run, in order.

        This is a static view of the pipeline shape; it does not consider the
        deadline (which is only known at run time, once a context is supplied).
        """
        return self.names

    # -- transforms --------------------------------------------------------

    def with_step(self, step: Step, *, at: int | None = None) -> Pipeline:
        """Return a new pipeline with ``step`` inserted (default: appended).

        The current pipeline is left unchanged. The result is re-validated, so
        inserting a duplicate name raises :class:`ValueError`.
        """
        steps = list(self.steps)
        if at is None:
            steps.append(step)
        else:
            steps.insert(at, step)
        return Pipeline(steps)

    def without(self, name: str) -> Pipeline:
        """Return a new pipeline with the step named ``name`` removed.

        Raises:
            KeyError: If no step has that name.
            ValueError: If removing the step would leave the pipeline empty.
        """
        if name not in self:
            raise KeyError(name)
        steps = [step for step in self.steps if step.name != name]
        return Pipeline(steps)

    def replace(self, name: str, step: Step) -> Pipeline:
        """Return a new pipeline with the step named ``name`` swapped for ``step``."""
        position = self.index(name)
        steps = list(self.steps)
        steps[position] = step
        return Pipeline(steps)

    def slice(self, start: int = 0, stop: int | None = None) -> Pipeline:
        """Return a new pipeline containing ``steps[start:stop]``.

        Raises:
            ValueError: If the resulting slice is empty.
        """
        return Pipeline(self.steps[start:stop])

    def concat(self, other: Pipeline) -> Pipeline:
        """Return a new pipeline with ``other``'s steps appended to this one's.

        Raises:
            ValueError: If the two pipelines share any step names.
        """
        return Pipeline(list(self.steps) + list(other.steps))

    # -- execution ---------------------------------------------------------

    def run(self, ctx: Context) -> Report:
        """Run every step in order against ``ctx`` and return a :class:`Report`.

        Behavior:
            * Before each step, if ``ctx.expired()`` is true, that step and all
              later steps are recorded as ``skipped`` (in order), the report's
              :attr:`~jobrunner.report.Report.deadline_exceeded` flag is set, and
              :attr:`~jobrunner.report.Report.tripped_step` is set to the first
              not-run step's name. The run then stops.
            * Otherwise the step is executed. On success its output is stored in
              ``ctx.outputs[name]`` and an ``ok`` record (with measured elapsed
              time) is added.
            * If a step raises :class:`~jobrunner.errors.StepFailed`, it is
              recorded as ``failed`` and the run stops immediately (remaining
              steps are *not* recorded).

        Args:
            ctx: The context to run against. Its ``clock`` is used for timing
                and its ``deadline`` (if any) drives the expiry check.

        Returns:
            A populated :class:`~jobrunner.report.Report`.
        """
        report = Report()
        for position, step in enumerate(self.steps):
            if ctx.expired():
                # The deadline has passed: skip this step and everything after
                # it, in order, and record why the run stopped.
                self._record_remaining_skips(report, position)
                report.deadline_exceeded = True
                report.tripped_step = step.name
                return report
            if not self._execute_step(step, ctx, report):
                # A step failed; the run stops without touching later steps.
                return report
        return report

    def _execute_step(self, step: Step, ctx: Context, report: Report) -> bool:
        """Run one step, recording the outcome.

        Returns:
            True if the run should continue, False if it should stop (because
            the step failed).
        """
        start = ctx.clock()
        try:
            output = step.execute(ctx)
        except StepFailed as exc:
            elapsed = ctx.clock() - start
            report.record_failed(step.name, error=exc, elapsed=elapsed)
            return False
        elapsed = ctx.clock() - start
        ctx.outputs[step.name] = output
        report.record_ok(step.name, output=output, elapsed=elapsed)
        return True

    def _record_remaining_skips(self, report: Report, from_position: int) -> None:
        """Record every step from ``from_position`` onward as skipped, in order."""
        for step in self.steps[from_position:]:
            report.record_skipped(step.name)

    def steps_from(self, name: str) -> list[Step]:
        """Return the step named ``name`` and every step after it, in order.

        Raises:
            KeyError: If no step has that name.
        """
        position = self.index(name)
        return self.steps[position:]

    # -- presentation ------------------------------------------------------

    def describe(self) -> str:
        """Return a short, human-readable description of the pipeline shape."""
        return f"Pipeline({len(self.steps)} steps: {', '.join(self.names)})"

    def explain(self) -> str:
        """Return a multi-line description of each step and its retry policy."""
        lines = [self.describe()]
        for position, step in enumerate(self.steps):
            lines.append(f"  {position}. {step.name} (retries={step.retries})")
        return "\n".join(lines)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Pipeline):
            return NotImplemented
        return self.names == other.names

    def __repr__(self) -> str:
        return f"Pipeline(steps={self.names!r})"
