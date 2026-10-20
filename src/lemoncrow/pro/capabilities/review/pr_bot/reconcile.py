"""New, carried forward, fixed or stale: what became of each finding since the last review.

Pure, and the whole cost story of a push. The previous review's findings and
reviewed-unit fingerprints (the state in the summary comment) are compared with the
new Snapshot and with what the AI stage just re-examined:

* a finding whose code is untouched is **carried forward**, with no model call and no
  new comment;
* a finding whose code changed is looked at again. If the model reports it again
  (by the engine's finding id, else by the looser fingerprint when it is worded
  differently) it is carried and updated -- the same thread, never a second one;
* if the changed code was re-examined successfully and the finding is gone it is
  **fixed**; if the re-examination failed it stays carried and is marked
  ``unverified``, because a failed call proves nothing;
* a finding whose code left the change altogether is **stale**
  (``superseded_stale``).

This is the AI-finding re-bind of ``ai_review._persist_findings`` (an exact semantic
identity that survives into a later Snapshot is re-bound, not duplicated; a finding
the engine no longer confirms is closed) applied to state that outlives a Snapshot.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lemoncrow.pro.capabilities.review.pr_bot.findings import (
    IMPACTS,
    MAX_CONSEQUENCE,
    MAX_EXPLANATION,
    MAX_TITLE,
    BotFinding,
    Located,
    fingerprint_for,
    severity_for,
    trimmed,
)
from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState


@dataclass(frozen=True, slots=True)
class Reconciled:
    """Every finding the review now tracks, grouped by what happened to it this run."""

    findings: tuple[BotFinding, ...]

    def with_state(self, state: str) -> tuple[BotFinding, ...]:
        return tuple(finding for finding in self.findings if finding.state == state)

    @property
    def new(self) -> tuple[BotFinding, ...]:
        return self.with_state("new")

    @property
    def carried(self) -> tuple[BotFinding, ...]:
        return self.with_state("carried")

    @property
    def fixed(self) -> tuple[BotFinding, ...]:
        return self.with_state("fixed")

    @property
    def stale(self) -> tuple[BotFinding, ...]:
        return self.with_state("stale")

    @property
    def open(self) -> tuple[BotFinding, ...]:
        return tuple(finding for finding in self.findings if finding.is_open)


def finding_from_confirmed(confirmed: Any, located: Located | None, head: str) -> BotFinding:
    """A new tracked finding from the engine's ``ConfirmedFinding``."""

    target_ids = tuple(confirmed.target_ids)
    where = located or Located("", 0, 0)
    impact = str(getattr(confirmed, "severity", "") or "")
    verified = getattr(confirmed, "verified", False) is True
    fix = getattr(confirmed, "replacement", None)
    if fix is None or fix.verified is not True or not where.path or where.side != "new":
        fix = None  # only a replacement checked against the Snapshot head may ever be posted
    return BotFinding(
        id=confirmed.id,
        fingerprint=fingerprint_for(confirmed.category, target_ids, confirmed.rule_refs),
        severity=severity_for(confirmed.category, impact=impact, verified=verified),
        category=confirmed.category,
        state="new",
        path=where.path,
        line=where.line,
        end_line=where.end_line,
        side=where.side,
        target_ids=target_ids,
        title=trimmed(confirmed.title, MAX_TITLE),
        explanation=trimmed(confirmed.explanation, MAX_EXPLANATION),
        consequence=trimmed(confirmed.consequence, MAX_CONSEQUENCE),
        first_seen=head,
        last_seen=head,
        impact=impact if impact in IMPACTS else "",
        verified=verified,
        suggestion=fix.text if fix else "",
        suggestion_start=fix.start_line if fix else 0,
        suggestion_end=fix.end_line if fix else 0,
    )


def _moved(finding: BotFinding, located: Located | None) -> BotFinding:
    """*finding* at its current lines; unchanged when its target has no location any more.

    A suggestion was checked against the head it was made on: when its target now sits elsewhere the line
    numbers it names may mean other code, so it is dropped rather than carried.
    """

    if located is None or not located.path:
        return finding
    moved = finding.changed(path=located.path, line=located.line, end_line=located.end_line, side=located.side)
    if (located.path, located.line, located.end_line, located.side) != (
        finding.path,
        finding.line,
        finding.end_line,
        finding.side,
    ):
        moved = moved.changed(suggestion="", suggestion_start=0, suggestion_end=0)
    return moved


def reconcile(
    previous: ReviewState | None,
    confirmed: Sequence[Any],
    *,
    locations: Mapping[str, Located],
    current_targets: Collection[str],
    rereviewed: Collection[str],
    covered: Collection[str],
    head: str,
) -> Reconciled:
    """The findings of this review.

    ``locations`` maps every target of the new Snapshot to where its comment goes,
    ``current_targets`` is those ids, ``rereviewed`` the targets the AI stage was
    asked about, ``covered`` the subset whose examination completed. ``head`` is the
    short sha being reviewed.
    """

    fresh = [
        finding_from_confirmed(item, locations.get(item.target_ids[0]) if item.target_ids else None, head)
        for item in confirmed
    ]
    by_id = {finding.id: finding for finding in fresh}
    by_fingerprint: dict[str, list[BotFinding]] = {}
    for finding in fresh:
        by_fingerprint.setdefault(finding.fingerprint, []).append(finding)
    used: set[str] = set()
    result: list[BotFinding] = []

    def location_of(finding: BotFinding) -> Located | None:
        return next((locations[target] for target in finding.target_ids if target in locations), None)

    for old in previous.findings if previous is not None else ():
        if not old.is_open:
            continue  # reported as closed last time; nothing more to say
        targets = set(old.target_ids)
        if not targets & set(current_targets):
            result.append(old.changed(state="stale", resolved_in=head, last_seen=head))
            continue
        touched = targets & set(rereviewed)
        if not touched:
            result.append(_moved(old, location_of(old)).changed(state="carried", last_seen=head))
            continue
        match = by_id.get(old.id)
        if match is None or match.id in used:
            match = next((item for item in by_fingerprint.get(old.fingerprint, ()) if item.id not in used), None)
        if match is not None:
            used.add(match.id)
            # Same concern, possibly reworded: keep its identity, thread and history; take the current text and place.
            result.append(
                match.changed(
                    id=old.id,
                    state="carried",
                    first_seen=old.first_seen or head,
                    thread_id=old.thread_id,
                    thread_url=old.thread_url,
                    placement=old.placement,
                )
            )
        elif touched <= set(covered):
            result.append(old.changed(state="fixed", resolved_in=head, last_seen=head))
        else:
            # Nothing was proven this run, so whatever verification earned the finding is suspended too.
            result.append(
                _moved(old, location_of(old)).changed(state="carried", last_seen=head, unverified=True).rated()
            )
    result.extend(finding for finding in fresh if finding.id not in used)
    return Reconciled(tuple(result))
