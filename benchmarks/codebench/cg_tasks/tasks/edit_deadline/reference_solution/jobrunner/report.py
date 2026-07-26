"""Result types produced by running a pipeline.

This module defines two closely related classes:

* :class:`StepRecord` - the outcome of a single step (ok, failed, or skipped),
  together with its output, timing, and any error.
* :class:`Report` - the ordered collection of :class:`StepRecord` objects for a
  whole run, plus the deadline bookkeeping used by the deadline feature.

The :class:`Report` deliberately exposes a rich, convenient API (properties for
the common name lists, container-protocol support, lookups by name, and a
human-readable :meth:`Report.summary`) because it is the primary object callers
inspect after a run.  None of these helpers mutate the underlying records once
they have been recorded; the recording helpers are the only mutators.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

#: The set of statuses a :class:`StepRecord` may take.
VALID_STATUSES: frozenset[str] = frozenset({"ok", "failed", "skipped"})

#: Short display markers keyed by status, used by :meth:`Report.summary`.
_STATUS_MARKERS: dict[str, str] = {"ok": "OK", "failed": "FAIL", "skipped": "SKIP"}


class StepRecord:
    """The recorded outcome of a single step.

    Args:
        name: The step's name.
        status: One of ``"ok"``, ``"failed"``, or ``"skipped"``.
        output: The value returned by the step (only meaningful when ``ok``).
        elapsed: Seconds spent executing the step.
        error: The exception raised (only meaningful when ``failed``).
    """

    __slots__ = ("elapsed", "error", "name", "output", "status")

    def __init__(
        self,
        name: str,
        status: str,
        output: Any = None,
        elapsed: float = 0.0,
        error: BaseException | None = None,
    ) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}")
        self.name = name
        self.status = status
        self.output = output
        self.elapsed = float(elapsed)
        self.error = error

    # -- convenience constructors -----------------------------------------

    @classmethod
    def ok(cls, name: str, output: Any = None, elapsed: float = 0.0) -> StepRecord:
        """Build an ``ok`` record."""
        return cls(name, "ok", output=output, elapsed=elapsed)

    @classmethod
    def failed(cls, name: str, error: BaseException | None = None, elapsed: float = 0.0) -> StepRecord:
        """Build a ``failed`` record."""
        return cls(name, "failed", elapsed=elapsed, error=error)

    @classmethod
    def skipped(cls, name: str, elapsed: float = 0.0) -> StepRecord:
        """Build a ``skipped`` record."""
        return cls(name, "skipped", elapsed=elapsed)

    # -- status predicates -------------------------------------------------

    @property
    def is_ok(self) -> bool:
        """True when the step completed successfully."""
        return self.status == "ok"

    @property
    def is_failed(self) -> bool:
        """True when the step failed."""
        return self.status == "failed"

    @property
    def is_skipped(self) -> bool:
        """True when the step was skipped (e.g. by a deadline)."""
        return self.status == "skipped"

    @property
    def duration(self) -> float:
        """Alias for :attr:`elapsed`, in seconds."""
        return self.elapsed

    # -- presentation & serialization -------------------------------------

    def describe(self) -> str:
        """Return a compact one-line description of this record."""
        marker = _STATUS_MARKERS[self.status]
        text = f"[{marker}] {self.name} ({self.elapsed:.6f}s)"
        if self.is_failed and self.error is not None:
            text += f" -> {self.error!r}"
        return text

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-dict view suitable for logging or serialization."""
        return {
            "name": self.name,
            "status": self.status,
            "output": self.output,
            "elapsed": self.elapsed,
            "error": None if self.error is None else repr(self.error),
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, StepRecord):
            return NotImplemented
        return (
            self.name == other.name
            and self.status == other.status
            and self.output == other.output
            and self.elapsed == other.elapsed
        )

    def __repr__(self) -> str:
        return f"StepRecord(name={self.name!r}, status={self.status!r}, " f"elapsed={self.elapsed!r})"


class Report:
    """Ordered results of a pipeline run plus deadline bookkeeping.

    Args:
        records: Optional initial list of :class:`StepRecord` objects.
        deadline_exceeded: Whether the run stopped because a deadline tripped.
        tripped_step: Name of the first step not run due to the deadline.
    """

    def __init__(
        self,
        records: list[StepRecord] | None = None,
        deadline_exceeded: bool = False,
        tripped_step: str | None = None,
    ) -> None:
        self.records: list[StepRecord] = list(records) if records else []
        self.deadline_exceeded: bool = deadline_exceeded
        self.tripped_step: str | None = tripped_step

    # -- recording helpers -------------------------------------------------

    def record(self, record: StepRecord) -> StepRecord:
        """Append an already-built :class:`StepRecord` and return it."""
        self.records.append(record)
        return record

    def extend(self, records: list[StepRecord]) -> None:
        """Append several records at once, preserving order."""
        for record in records:
            self.record(record)

    def record_ok(self, name: str, output: Any = None, elapsed: float = 0.0) -> StepRecord:
        """Append an ``ok`` record and return it."""
        return self.record(StepRecord.ok(name, output=output, elapsed=elapsed))

    def record_failed(self, name: str, error: BaseException | None = None, elapsed: float = 0.0) -> StepRecord:
        """Append a ``failed`` record and return it."""
        return self.record(StepRecord.failed(name, error=error, elapsed=elapsed))

    def record_skipped(self, name: str, elapsed: float = 0.0) -> StepRecord:
        """Append a ``skipped`` record and return it."""
        return self.record(StepRecord.skipped(name, elapsed=elapsed))

    # -- lookups & container protocol -------------------------------------

    def get(self, name: str) -> StepRecord | None:
        """Return the first record with ``name``, or ``None``."""
        for record in self.records:
            if record.name == name:
                return record
        return None

    def status_of(self, name: str) -> str | None:
        """Return the status recorded for ``name``, or ``None`` if not present."""
        record = self.get(name)
        return None if record is None else record.status

    def elapsed_for(self, name: str) -> float:
        """Return the elapsed time recorded for ``name`` (0.0 if not present)."""
        record = self.get(name)
        return 0.0 if record is None else record.elapsed

    def filter(self, predicate: Callable[[StepRecord], bool]) -> list[StepRecord]:
        """Return the records for which ``predicate`` is truthy, in order."""
        return [record for record in self.records if predicate(record)]

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[StepRecord]:
        return iter(self.records)

    def __contains__(self, name: object) -> bool:
        return any(record.name == name for record in self.records)

    def __getitem__(self, index: int) -> StepRecord:
        return self.records[index]

    def __bool__(self) -> bool:
        return self.ok

    # -- derived views -----------------------------------------------------

    @property
    def succeeded(self) -> list[str]:
        """Names of steps that completed successfully, in order."""
        return [r.name for r in self.records if r.status == "ok"]

    @property
    def failed(self) -> list[str]:
        """Names of steps that failed, in order."""
        return [r.name for r in self.records if r.status == "failed"]

    @property
    def skipped(self) -> list[str]:
        """Names of steps that were skipped, in order."""
        return [r.name for r in self.records if r.status == "skipped"]

    @property
    def first_failure(self) -> StepRecord | None:
        """The first failed record, or ``None`` if nothing failed."""
        for record in self.records:
            if record.is_failed:
                return record
        return None

    @property
    def ok(self) -> bool:
        """True when nothing failed and no deadline tripped."""
        return not self.failed and not self.deadline_exceeded

    @property
    def total_elapsed(self) -> float:
        """Sum of the elapsed time across all records."""
        return sum(r.elapsed for r in self.records)

    def counts(self) -> dict[str, int]:
        """Return a ``{status: count}`` breakdown across all records."""
        result = {status: 0 for status in VALID_STATUSES}
        for record in self.records:
            result[record.status] += 1
        return result

    # -- presentation ------------------------------------------------------

    def summary(self) -> str:
        """Return a multi-line, human-readable summary of the run.

        The summary always lists each step with its status and elapsed time.
        When the run stopped because of a deadline, an explicit line naming the
        tripped step is included so operators can see *why* work stopped.
        """
        lines: list[str] = [f"Pipeline report: {len(self.records)} step(s)"]
        for record in self.records:
            lines.append(f"  {record.describe()}")
        if self.deadline_exceeded:
            lines.append(
                f"Deadline exceeded: tripped at step {self.tripped_step!r}; " f"{len(self.skipped)} step(s) skipped."
            )
        lines.append(
            f"Total elapsed: {self.total_elapsed:.6f}s "
            f"(ok={len(self.succeeded)}, failed={len(self.failed)}, "
            f"skipped={len(self.skipped)})"
        )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-dict view of the whole report."""
        return {
            "records": [r.to_dict() for r in self.records],
            "deadline_exceeded": self.deadline_exceeded,
            "tripped_step": self.tripped_step,
            "ok": self.ok,
            "total_elapsed": self.total_elapsed,
        }

    def __repr__(self) -> str:
        return (
            f"Report(records={len(self.records)}, "
            f"deadline_exceeded={self.deadline_exceeded!r}, "
            f"tripped_step={self.tripped_step!r})"
        )
