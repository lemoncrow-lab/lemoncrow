"""Second reviewer rules (SPEC 4.16, P7-1): pure functions, no store and no clock.

An always-a-decision area needs two people. The second one is a CODEOWNER of a
decision part the change touches, never the author, never the first reviewer and
never a bot, and is asked only when the first reviewer approves (D1). Nothing here
reads or writes anything; the composition that owns identities (hosted only, P0-2)
calls these rules with the facts it holds. Locally nothing calls them.

Counts are counts of signatures that exist: :func:`signature_counts` takes the
facts a caller has already verified and never invents a number.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

SecondState = Literal[
    "not_required", "unavailable", "not_needed_yet", "will_be_asked", "waiting", "agrees", "disagrees"
]
SECOND_STATES: Final[tuple[str, ...]] = (
    "not_required",
    "unavailable",
    "not_needed_yet",
    "will_be_asked",
    "waiting",
    "agrees",
    "disagrees",
)
Settled = Literal["approved", "changes_requested", "waiting"]

#: The verdicts a person can sign with. A comment is not a signature.
SIGNING_OUTCOMES: Final[tuple[str, ...]] = ("lgtm", "changes_requested")
#: The two mark states that are an answer ("Looks good" / "Needs a change").
ANSWER_STATES: Final[tuple[str, ...]] = ("reviewed", "needs_changes")


@dataclass(frozen=True, slots=True)
class OwnerRef:
    """One CODEOWNER who can sign: a bound LemonCrow principal (a provider handle alone cannot sign)."""

    principal_id: str
    handle: str = ""
    name: str = ""


@dataclass(frozen=True, slots=True)
class DecisionPart:
    """One part the change asks a person to decide (answer category ``decision``)."""

    unit_key: str
    path: str
    label: str
    start_line: int = 0
    end_line: int = 0


@dataclass(frozen=True, slots=True)
class SecondPlan:
    """Who must co-sign, and for what.

    ``required`` is true only when a person other than the author, the first reviewer and any bot owns a
    decision part. ``unavailable`` is non-empty when decision parts exist and nobody eligible owns them: the
    plan is then not required (a signature nobody can give cannot be waited for) and says why.
    """

    required: bool
    reviewer: OwnerRef | None = None
    area: str = ""
    parts: tuple[DecisionPart, ...] = ()
    unavailable: str = ""


def area_label(paths: Iterable[str]) -> str:
    """A short name for the area some paths form: one file by its path without the extension, else their folder."""

    names = sorted({path.strip("/") for path in paths if path.strip("/")})
    if not names:
        return ""
    if len(names) == 1:
        stem, _ext = posixpath.splitext(names[0])
        return stem or names[0]
    common = posixpath.commonpath([posixpath.dirname(name) or "." for name in names])
    return "" if common in {"", "."} else common


def plan_second_reviewer(
    parts: Sequence[DecisionPart],
    owners: Mapping[str, Sequence[OwnerRef]],
    *,
    excluded_ids: Iterable[str],
) -> SecondPlan:
    """Pick the one second reviewer for the decision parts, from their CODEOWNERS.

    *excluded_ids* is every principal who may not co-sign: the author, the first reviewer, authoring agents.
    The owner who covers the most decision parts wins (ties: the lower principal id, so the choice is stable).
    Parts the winner does not own are not their area; the area names only what they own.
    """

    if not parts:
        return SecondPlan(required=False)
    barred = {item for item in excluded_ids if item}
    covered: dict[str, list[DecisionPart]] = {}
    people: dict[str, OwnerRef] = {}
    owned_by_anyone = False
    for part in parts:
        candidates = owners.get(part.path, ())
        if candidates:
            owned_by_anyone = True
        for owner in candidates:
            if not owner.principal_id or owner.principal_id in barred:
                continue
            covered.setdefault(owner.principal_id, []).append(part)
            people.setdefault(owner.principal_id, owner)
    if not covered:
        if not owned_by_anyone:
            return SecondPlan(required=False)  # no CODEOWNER names this area: the repo set no second-person rule
        # An owner exists but cannot sign (the author, the first reviewer, or no LemonCrow identity).
        return SecondPlan(
            required=False,
            unavailable="no CODEOWNER of these decisions can sign besides the people already on this review",
        )
    winner = sorted(covered, key=lambda pid: (-len(covered[pid]), pid))[0]
    mine = tuple(covered[winner])
    return SecondPlan(
        required=True,
        reviewer=people[winner],
        area=area_label(part.path for part in mine),
        parts=mine,
    )


def second_state(
    plan: SecondPlan,
    *,
    first_outcome: str | None,
    second_outcome: str | None,
) -> SecondState:
    """Where the second signature stands.

    *first_outcome* is the first reviewer's recorded verdict on this revision (``lgtm`` or
    ``changes_requested``), ``None`` before it is sent. *second_outcome* is the second reviewer's signed
    verdict, ``None`` until a signature exists. A "Request changes" first verdict never asks anyone (D1).
    """

    if not plan.required:
        return "unavailable" if plan.unavailable else "not_required"
    if first_outcome is None:
        return "will_be_asked"
    if first_outcome != "lgtm":
        return "not_needed_yet"
    if second_outcome is None:
        return "waiting"
    return "agrees" if second_outcome == "lgtm" else "disagrees"


def settled_verdict(state: SecondState, first_outcome: str | None) -> Settled:
    """The verdict the pair of reviewers settle on (R13): any needs-a-change from either blocks approval."""

    if first_outcome == "changes_requested" or state == "disagrees":
        return "changes_requested"
    if state == "waiting":
        return "waiting"
    return "approved" if first_outcome == "lgtm" else "waiting"


def signature_counts(*, first_signed: bool, second_signed: bool, required: bool) -> tuple[int, int]:
    """``(signatures that exist, signatures the policy needs)``; never a count nobody signed."""

    needed = 2 if required else 1
    have = int(first_signed) + (int(second_signed) if required else 0)
    return have, needed


def answered(plan: SecondPlan, states: Mapping[str, str]) -> bool:
    """Whether the second reviewer has answered every part they were asked, with a real answer."""

    if not plan.parts:
        return False
    return all(states.get(part.unit_key) in ANSWER_STATES for part in plan.parts)


def agrees_with(plan: SecondPlan, states: Mapping[str, str]) -> bool:
    """The second reviewer's own answers say \"Looks good\" for every part they were asked."""

    return answered(plan, states) and all(states[part.unit_key] == "reviewed" for part in plan.parts)


def first_answers_visible(*, viewer_is_second: bool, second_answered: bool) -> bool:
    """Anti-anchoring (SPEC 1.5): the second reviewer sees the first reviewer's answers only after answering.

    Anyone who is not the second reviewer is outside this rule (the first reviewer sees their own).
    """

    return (not viewer_is_second) or second_answered


__all__ = [
    "ANSWER_STATES",
    "SECOND_STATES",
    "SIGNING_OUTCOMES",
    "DecisionPart",
    "OwnerRef",
    "SecondPlan",
    "SecondState",
    "Settled",
    "agrees_with",
    "answered",
    "area_label",
    "first_answers_visible",
    "plan_second_reviewer",
    "second_state",
    "settled_verdict",
    "signature_counts",
]
