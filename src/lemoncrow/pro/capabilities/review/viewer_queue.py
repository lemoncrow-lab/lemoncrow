"""Per-viewer queue state (Home and queue, SPEC 4.1): snooze and resume position.

Pure rules. The store keeps the rows (``review_viewer_state``); nothing here reads
a clock, a database or the network, so every rule is a unit test.

* **Snooze** hides a Review from the viewer's *Your turn* until an instant. It ends
  by itself at that instant, and early when a newer Snapshot is captured: a Review
  that has new work in it is not something the viewer asked to forget.
* **Resume** is where the viewer paused: the screen, the part and "n of N". It is
  only the viewer's own place; it is never evidence that anything was read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from lemoncrow.pro.capabilities.review.freshness import format_timestamp, parse_timestamp

#: The screens a resume position may name; each maps to a route in the Reader.
RESUME_SCREENS: Final[tuple[str, ...]] = ("one", "quick_check", "wrap")
MAX_PART_LENGTH: Final[int] = 400
#: A snooze further out than this is refused: a snooze is a short deferral, not a way to bury a Review.
MAX_SNOOZE: Final[timedelta] = timedelta(days=31)


class ViewerStateInvalid(ValueError):
    """The request named a snooze or a resume position the rules refuse."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class Resume:
    screen: str
    part: str = ""
    position: int = 0
    total: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"screen": self.screen, "part": self.part, "position": self.position, "total": self.total}


@dataclass(frozen=True, slots=True)
class ViewerState:
    review_id: str
    viewer_id: str
    snoozed_until: str = ""
    snoozed_revision: int = 0
    resume: Resume | None = None
    updated_at: str = ""

    def queue_wire(self, *, current_revision: int, now: datetime) -> dict[str, Any]:
        """What Home reads: the snooze only while it holds, the resume position as stored."""

        until = self.snoozed_until if snooze_active(self, current_revision=current_revision, now=now) else ""
        return {"snoozed_until": until, "resume": None if self.resume is None else self.resume.to_wire()}


def snooze_active(state: ViewerState | None, *, current_revision: int, now: datetime) -> bool:
    """True while the snooze holds: not yet due, and no newer Snapshot since it was set."""

    if state is None or not state.snoozed_until:
        return False
    until = parse_timestamp(state.snoozed_until)
    if until is None or until <= now:
        return False
    return current_revision <= state.snoozed_revision


def parse_snooze_until(raw: object, *, now: datetime) -> str:
    """A snooze instant from the request: an aware ISO time later than *now* and within :data:`MAX_SNOOZE`."""

    until = parse_timestamp(raw)
    if until is None:
        raise ViewerStateInvalid("snoozed_until", "snoozed_until must be an ISO-8601 time with a zone")
    if until <= now:
        raise ViewerStateInvalid("snoozed_until", "snoozed_until must be in the future")
    if until - now > MAX_SNOOZE:
        raise ViewerStateInvalid("snoozed_until", "a Review can be snoozed for at most 31 days")
    return format_timestamp(until)


def parse_resume(raw: object) -> Resume:
    if not isinstance(raw, Mapping):
        raise ViewerStateInvalid("resume", "resume must be an object")
    screen = str(raw.get("screen") or "")
    if screen not in RESUME_SCREENS:
        raise ViewerStateInvalid("resume", f"resume.screen must be one of {', '.join(RESUME_SCREENS)}")
    part = str(raw.get("part") or "")
    if len(part) > MAX_PART_LENGTH:
        raise ViewerStateInvalid("resume", "resume.part is too long")
    numbers: list[int] = []
    for name in ("position", "total"):
        value = raw.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 100_000:
            raise ViewerStateInvalid("resume", f"resume.{name} must be a whole number from 0 to 100000")
        numbers.append(value)
    position, total = numbers
    if position > total:
        raise ViewerStateInvalid("resume", "resume.position cannot be past resume.total")
    return Resume(screen=screen, part=part, position=position, total=total)


def resume_to_json(resume: Resume | None) -> str:
    return "" if resume is None else json.dumps(resume.to_wire(), separators=(",", ":"), sort_keys=True)


def resume_from_json(text: str) -> Resume | None:
    """A stored resume position; a row that no longer parses reads as no position (never an error)."""

    if not text:
        return None
    try:
        return parse_resume(json.loads(text))
    except (ValueError, TypeError):
        return None


__all__ = [
    "MAX_SNOOZE",
    "RESUME_SCREENS",
    "Resume",
    "ViewerState",
    "ViewerStateInvalid",
    "parse_resume",
    "parse_snooze_until",
    "resume_from_json",
    "resume_to_json",
    "snooze_active",
]
