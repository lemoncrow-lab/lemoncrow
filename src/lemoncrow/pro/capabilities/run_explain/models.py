"""What `lc run explain` is allowed to say about a run -- pure data, zero I/O.

Why this module exists: the interesting question after a run is "what went
wrong and whose layer was it", and the tempting answer is a single verdict.
This contract deliberately makes that impossible. There is no ``cause`` field
and no confidence score; there is a list of :class:`AttributionSignal`, each of
which carries an ``evidence_path`` a reader can open. A category is only ever
attached to a concrete record that was actually observed, which is why the
report can be wrong about *significance* but never invents an event.

The second half of that discipline is :attr:`AttributionReport.unresolved`. A
source that was absent is named, so "no shell signals" is distinguishable from
"the run ledger was missing". Attribution by elimination -- concluding ``model``
because nothing else fired -- is exactly what that field exists to prevent.

Every field defaults, so a report built from a ledger alone, from a trace alone,
or from neither is still a value rather than a ``TypeError``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Bump on any field removal or type change. Additive changes keep version 1;
# consumers must ignore unknown keys.
SCHEMA_VERSION = 1

AttributionCategory = Literal[
    "model",
    "provider",
    "host",
    "lemoncrow",
    "shell",
    "policy",
    "repository",
    "unknown",
]
ATTRIBUTION_CATEGORY_VALUES: tuple[str, ...] = (
    "model",
    "provider",
    "host",
    "lemoncrow",
    "shell",
    "policy",
    "repository",
    "unknown",
)

# What a signal in each category asserts -- and, just as importantly, what it
# does not. Used by the renderer's legend so a reader never has to guess whether
# ``provider`` means "the provider is at fault" (it does not; it means the
# evidence came from the provider boundary).
CATEGORY_MEANING: dict[str, str] = {
    "model": "the model's own limits or repetition, as recorded",
    "provider": "the provider boundary: routing decisions and API-side errors",
    "host": "the agent host process: how the session started and stopped",
    "lemoncrow": "LemonCrow's own supervision: watchdog alerts",
    "shell": "commands the agent ran that reported failure",
    "policy": "rubrics and gates that blocked the run",
    "repository": "validations and tests run against the code",
    "unknown": "a recorded failure that maps to no category above",
}


@dataclass(frozen=True)
class AttributionSignal:
    """One piece of observed evidence, with the pointer that proves it.

    ``evidence_path`` is the whole point: ``<run.json>#events[41]`` or
    ``trace:<id>#errors_seen[3]``. A signal a reader cannot go and check is an
    opinion, and this report does not carry opinions.
    """

    category: AttributionCategory = "unknown"
    summary: str = ""
    """One factual line, already collapsed to a single line and length-capped."""
    evidence_path: str = ""
    """Points at the *first* occurrence when ``count`` collapsed duplicates."""
    at: str | None = None
    """ISO-8601 of the underlying event; ``None`` when the source records none."""
    count: int = 1
    """How many identical occurrences this one entry stands for."""

    def to_dict(self) -> dict[str, Any]:
        """Return the exact ``--json`` entry: asdict semantics, declaration order."""

        return asdict(self)


@dataclass(frozen=True)
class AttributionReport:
    """Everything `lc run explain` knows about one run, and what it could not see."""

    schema_version: int = SCHEMA_VERSION
    session_id: str = ""
    host: str = "unknown"
    model: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    """``None`` when the ledger never recorded a terminal status -- see the ``host`` rule."""
    status: str = "unknown"
    """The run ledger's own status; ``"unknown"`` when there is no ledger."""
    signals: tuple[AttributionSignal, ...] = ()
    """Sorted by ``(category, -count, summary)``; trimmed per category."""
    category_counts: tuple[tuple[str, int], ...] = ()
    """Full per-category totals *before* trimming, so truncation stays visible."""
    cost: dict[str, Any] = field(default_factory=dict)
    """``usage.explain_run()`` payload, or ``{}`` when usage could not be read."""
    unresolved: tuple[str, ...] = ()
    """Names of evidence sources that were absent. Never inferred from silence."""

    def to_dict(self) -> dict[str, Any]:
        """Return the exact ``--json`` payload, with tuples flattened to arrays."""

        payload: dict[str, Any] = asdict(self)
        payload["signals"] = [signal.to_dict() for signal in self.signals]
        payload["category_counts"] = [[name, count] for name, count in self.category_counts]
        payload["unresolved"] = list(self.unresolved)
        return payload


__all__ = [
    "ATTRIBUTION_CATEGORY_VALUES",
    "CATEGORY_MEANING",
    "SCHEMA_VERSION",
    "AttributionCategory",
    "AttributionReport",
    "AttributionSignal",
]
