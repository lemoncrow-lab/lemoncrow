"""New, carried forward, fixed or stale: what became of each finding since the last review.

Pure, and the whole cost story of a push. The previous review's findings and
reviewed-unit fingerprints (the state in the summary comment) are compared with the
new Snapshot and with what the AI stage just re-examined:

* a finding whose code is untouched is **carried forward**, with no model call and no
  new comment;
* a finding whose code changed is looked at again. If the model reports it again
  (by the engine's finding id, else by the looser fingerprint when it is worded
  differently) it is carried and updated -- the same thread, never a second one;
* if the changed code was re-examined successfully and the model did not report the finding again, absence
  proves nothing (a model does not repeat itself reliably, and an unrelated edit in the same file re-asks it), so
  the re-check also asked about it by id. It is **fixed** only when the model says it is resolved *and* the lines
  the finding cited have changed; **carried** when the model says it is still present; otherwise it is
  **unreported** ("no longer reported"): closed, so it blocks nothing, with its thread left open for a person. If a
  later review reports it again it comes back as carried on the same thread. If the re-examination failed it stays
  carried and is marked ``unverified``, because a failed call proves nothing;
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
    trimmed,
)
from lemoncrow.pro.capabilities.review.pr_bot.pr_intent import MISMATCH_PREFIX
from lemoncrow.pro.capabilities.review.pr_bot.settings import capped, severity_with_overrides
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
    def unreported(self) -> tuple[BotFinding, ...]:
        return self.with_state("unreported")

    @property
    def open(self) -> tuple[BotFinding, ...]:
        return tuple(finding for finding in self.findings if finding.is_open)


def finding_from_confirmed(
    confirmed: Any, located: Located | None, head: str, rule_severity: Mapping[str, str] | None = None
) -> BotFinding:
    """A new tracked finding from the engine's ``ConfirmedFinding``.

    *rule_severity* is ``{rule ref: severity}`` for the rules the repository gave a severity (``review.rules`` in the
    trusted-base ``.lemoncrow/review.yaml``): a finding citing one gets it instead of the ordinary ladder's answer.
    A finding the investigator marked as the code contradicting the stated intent is labelled so, in words this code
    chose, and is never more than a P1: a mismatch rests on text written by people, which is not evidence of a defect.
    """

    target_ids = tuple(confirmed.target_ids)
    where = located or Located("", 0, 0)
    impact = str(getattr(confirmed, "severity", "") or "")
    verified = getattr(confirmed, "verified", False) is True
    fix = getattr(confirmed, "replacement", None)
    if fix is None or fix.verified is not True or not where.path or where.side != "new":
        fix = None  # only a replacement checked against the Snapshot head may ever be posted
    cited = getattr(confirmed, "lines", None)
    if cited is None or cited.verified is not True or not where.path or where.side != "new":
        cited = None  # likewise the lines: only ones checked against the head and the diff may place a comment
    if cited is not None:
        line, end_line, anchor = cited.start_line, cited.end_line, "cited"
    else:
        line, end_line, anchor = where.line, where.end_line, "target" if where.path else ""
    # An imported (advisory) rule never makes a P0; only a critical rule does (G10). A severity the repository gave a
    # rule replaces the ladder's answer for a finding that cites it (settings.severity_with_overrides).
    severity = severity_with_overrides(
        confirmed.category,
        impact=impact,
        verified=verified,
        rule_refs=tuple(confirmed.rule_refs),
        overrides=rule_severity or {},
    )
    title = confirmed.title
    if getattr(confirmed, "intent_mismatch", False) is True:
        severity = capped(severity, "p1")
        title = MISMATCH_PREFIX + title
    return BotFinding(
        id=confirmed.id,
        fingerprint=fingerprint_for(confirmed.category, target_ids, confirmed.rule_refs),
        severity=severity,
        category=confirmed.category,
        state="new",
        path=where.path,
        line=line,
        end_line=end_line,
        side=where.side,
        target_ids=target_ids,
        title=trimmed(title, MAX_TITLE),
        explanation=trimmed(confirmed.explanation, MAX_EXPLANATION),
        consequence=trimmed(confirmed.consequence, MAX_CONSEQUENCE),
        first_seen=head,
        last_seen=head,
        impact=impact if impact in IMPACTS else "",
        verified=verified,
        suggestion=fix.text if fix else "",
        suggestion_start=fix.start_line if fix else 0,
        suggestion_end=fix.end_line if fix else 0,
        anchor=anchor,
        target_line=where.line,
        anchor_digest=cited.digest if cited is not None else "",
    )


def _moved(finding: BotFinding, located: Located | None) -> BotFinding:
    """*finding* after a push that left its target's code alone but may have shifted it; unchanged if it has no place.

    The target's first changed line is compared with where it was when the finding was located. A suggestion was
    checked against the head it was made on, so when the target moved the line numbers it names may mean other
    code and it is dropped rather than carried.

    Where the finding is shown follows its thread. A thread cannot move, so a finding that has one stays on the
    lines it was posted on and the summary keeps pointing at the thread. A finding with no thread follows its
    code: by the same shift when it cites a line, else to the target's first changed lines.
    """

    if located is None or not located.path:
        return finding
    baseline = finding.target_line or finding.line
    same_file = (located.path, located.side) == (finding.path, finding.side)
    if same_file and baseline > 0 and located.line == baseline:
        return finding if finding.target_line else finding.changed(target_line=located.line)
    moved = finding.changed(suggestion="", suggestion_start=0, suggestion_end=0, target_line=located.line)
    if finding.thread_id:
        return moved
    if same_file and finding.anchor == "cited" and baseline > 0 and located.line > 0:
        shift = located.line - baseline
        return moved.changed(line=max(1, finding.line + shift), end_line=max(1, finding.end_line + shift))
    return moved.changed(path=located.path, line=located.line, end_line=located.end_line, side=located.side)


def reconcile(
    previous: ReviewState | None,
    confirmed: Sequence[Any],
    *,
    locations: Mapping[str, Located],
    current_targets: Collection[str],
    rereviewed: Collection[str],
    covered: Collection[str],
    head: str,
    answers: Mapping[str, str] | None = None,
    intact: Collection[str] = (),
    rule_severity: Mapping[str, str] | None = None,
) -> Reconciled:
    """The findings of this review.

    ``locations`` maps every target of the new Snapshot to where its comment goes,
    ``current_targets`` is those ids, ``rereviewed`` the targets the AI stage was
    asked about, ``covered`` the subset whose examination completed. ``head`` is the
    short sha being reviewed. ``answers`` is what the re-check said about each earlier finding it was asked
    about (``still_present``, ``resolved`` or ``unclear``) and ``intact`` the earlier findings whose cited lines
    are still in the file word for word: together they are the only way a finding is declared fixed.
    """

    fresh = [
        finding_from_confirmed(
            item, locations.get(item.target_ids[0]) if item.target_ids else None, head, rule_severity
        )
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

    said = answers or {}
    for old in previous.findings if previous is not None else ():
        if not old.is_open:
            continue  # reported as closed last time; nothing more to say (an unreported one is looked at below)
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
            # Same concern, possibly reworded: keep its identity, thread and history; take the current text. The
            # place is the current one too, unless a thread exists: that stays where it was posted, and so does the
            # finding, or the summary would point away from its own thread.
            carried = match.changed(
                id=old.id,
                state="carried",
                first_seen=old.first_seen or head,
                thread_id=old.thread_id,
                thread_url=old.thread_url,
                placement=old.placement,
            )
            if old.thread_id:
                carried = carried.changed(
                    path=old.path,
                    line=old.line,
                    end_line=old.end_line,
                    side=old.side,
                    anchor=old.anchor,
                    anchor_digest=carried.anchor_digest or old.anchor_digest,
                )
            result.append(carried)
        elif touched <= set(covered):
            answer = said.get(old.id, "")
            if answer == "still_present":
                result.append(_moved(old, location_of(old)).changed(state="carried", last_seen=head))
            elif answer == "resolved" and old.anchor_digest and old.id not in intact:
                result.append(old.changed(state="fixed", resolved_in=head, last_seen=head))
            else:
                # Not reported again, but nothing showed it fixed: the model did not say so, or the lines it cited
                # are still there. Say that, and leave the thread for a person.
                result.append(old.changed(state="unreported", resolved_in=head, last_seen=head))
        else:
            # Nothing was proven this run, so whatever verification earned the finding is suspended too.
            result.append(
                _moved(old, location_of(old)).changed(state="carried", last_seen=head, unverified=True).rated()
            )
    for old in previous.findings if previous is not None else ():
        if old.state != "unreported" or not set(old.target_ids) & set(current_targets):
            continue  # (an unreported finding whose code left the change has nothing more to say)
        match = by_id.get(old.id)
        if match is None or match.id in used:
            match = next((item for item in by_fingerprint.get(old.fingerprint, ()) if item.id not in used), None)
        if match is None:
            result.append(old)
            continue
        used.add(match.id)
        result.append(
            match.changed(
                id=old.id,
                state="carried",
                first_seen=old.first_seen or head,
                thread_id=old.thread_id,
                thread_url=old.thread_url,
                placement=old.placement,
                **(
                    {"path": old.path, "line": old.line, "end_line": old.end_line, "side": old.side}
                    if old.thread_id
                    else {}
                ),
            )
        )
    result.extend(finding for finding in fresh if finding.id not in used)
    return Reconciled(tuple(result))
