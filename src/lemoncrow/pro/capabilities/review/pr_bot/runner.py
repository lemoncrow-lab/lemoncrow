"""One headless pull request review, start to finish.

``run_pr_review`` is what ``lc review --pr N --ai [--publish-bot]`` runs, in the
customer's CI with their own model key:

1. gate on the event and the live pull request (forks, drafts, pushes, closed);
2. read the previous review's state from the bot's summary comment;
3. capture the PR into a throwaway store and run the AI review on the targets that
   changed since that review (:mod:`engine`);
4. decide what became of every finding (:mod:`reconcile`);
5. publish: inline comments for new P0/P1 findings only, a reply and a resolved
   thread for fixed ones, the summary updated in place, and the commit status.

Publishing writes nothing for a dry run, and refuses to run outside GitHub Actions
(``GITHUB_ACTIONS=true``): on a laptop the ``gh`` login is a person, and a bot
comment from a person's login is exactly the confusion this path exists to avoid.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lemoncrow.pro.capabilities.review.pr_bot import ci_env
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from lemoncrow.pro.capabilities.review.pr_bot.github import BotGitHub, Remote
from lemoncrow.pro.capabilities.review.pr_bot.reconcile import reconcile
from lemoncrow.pro.capabilities.review.pr_bot.render import (
    Receipt,
    RunFacts,
    SummaryView,
    Verdict,
    check_status,
    inline_comment,
    render_summary,
    short,
    verdict,
)
from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState, cap_findings
from lemoncrow.pro.capabilities.review.sources.github import (
    GhRunner,
    GitHubCliError,
    PullRequestRef,
    pull_request_ref,
    resolve_pull_request,
    run_gh,
)

FAIL_ON = ("none", "p0")
# lc-debt: the repository's review.finding_threshold is not applied here, so P2 findings always show (held back, collapsed);
# upgrade: resolve criticality.triage_policy_for for the Snapshot and drop findings below the threshold.
#: Severities that get an inline comment; P2 stays in the summary, held back.
INLINE_SEVERITIES = frozenset({"p0", "p1"})


class PrReviewError(RuntimeError):
    """The review could not run, for a reason the person running it can act on."""


@dataclass(frozen=True)
class RunOptions:
    pr: str
    repo_root: Path
    depth: str = "auto"
    fail_on: str = "none"
    review_drafts: bool = False
    on_push: bool = True
    publish: bool = False
    dry_run: bool = False
    provider: str = "auto"
    with_impact: bool = True


@dataclass
class RunReport:
    """The outcome of one run. ``skipped`` runs did no review and wrote nothing."""

    pr: str
    skipped: bool = False
    reason: str = ""
    level: str = "notice"
    exit_code: int = 0
    head_sha: str = ""
    verdict: Verdict | None = None
    findings: tuple[BotFinding, ...] = ()
    state: ReviewState | None = None
    summary: str = ""
    inline_bodies: tuple[str, ...] = ()
    status: tuple[str, str] | None = None
    published: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    noop: bool = False

    def to_wire(self) -> dict[str, Any]:
        """The machine-readable result (``--json``): the same fields the summary's hidden block carries."""

        return {
            "schema": 1,
            "pull_request": self.pr,
            "skipped": self.skipped,
            "reason": self.reason,
            "head_sha": self.head_sha,
            "verdict": (
                None
                if self.verdict is None
                else {
                    "state": self.verdict.state,
                    "headline": self.verdict.headline,
                    "p0": self.verdict.p0,
                    "p1": self.verdict.p1,
                    "p2": self.verdict.p2,
                }
            ),
            "status": None if self.status is None else {"state": self.status[0], "description": self.status[1]},
            "findings": [finding.to_wire() for finding in self.findings],
            "usage": None if self.state is None else self.state.usage.to_wire(),
            "usage_total": None if self.state is None else self.state.usage_total.to_wire(),
            "published": self.published,
            "dry_run": bool(self.published.get("dry_run")),
            "noop": self.noop,
            "notes": list(self.notes),
        }


def _skip(pr: str, reason: str, *, level: str = "notice", exit_code: int = 0) -> RunReport:
    return RunReport(pr=pr, skipped=True, reason=reason, level=level, exit_code=exit_code)


def run_pr_review(
    options: RunOptions,
    *,
    runner: GhRunner | None = None,
    structured_executor: Callable[..., Any] | None = None,
    env: Mapping[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> RunReport:
    from lemoncrow.pro.capabilities.review.pr_bot import engine

    environ = os.environ if env is None else env
    say = progress or (lambda _message: None)
    if options.fail_on not in FAIL_ON:
        raise PrReviewError(f"fail-on must be one of {', '.join(FAIL_ON)}")
    if options.publish and not options.dry_run and environ.get("GITHUB_ACTIONS") != "true":
        raise PrReviewError(
            "--publish-bot posts as the identity of the token in use and runs only in GitHub Actions, where that is "
            "the workflow's bot token. Locally, use --ai to read the findings, or add --dry-run to see the comments."
        )
    run = runner or (lambda args, stdin: run_gh(args, stdin, cwd=options.repo_root))
    # Gate on the event alone first: a fork, a draft or a refused event costs no network call. A URL or
    # OWNER/REPO#N names its pull request by itself, so only a bare number needs the event to carry one.
    bare = re.fullmatch(r"#?(\d+)", options.pr.strip())
    gate = ci_env.decide(
        environ,
        review_drafts=options.review_drafts,
        on_push=options.on_push,
        provider=options.provider,
        pr_number=int(bare[1]) if bare else 1,
        require_credentials=False,
    )
    if not gate.run:
        return _skip(options.pr, gate.reason, level=gate.level, exit_code=gate.exit_code)
    try:
        ref = pull_request_ref(options.repo_root, options.pr, runner=run)
    except (GitHubCliError, ValueError) as exc:
        raise PrReviewError(str(exc)) from exc
    name = f"{ref.slug}#{ref.number}"
    github = BotGitHub(ref, run)
    try:
        facts = github.pull_facts()
    except GitHubCliError as exc:
        raise PrReviewError(str(exc)) from exc
    writes = options.publish and not options.dry_run

    def skipped(reason: str, because: str) -> RunReport:
        # A `LemonCrow Review` required check waits for a status on the head: a skip says so instead of hanging it.
        report = _skip(name, reason)
        if writes and facts.head_sha:
            description = f"skipped: {because}"
            if _best_effort(
                lambda: github.set_status(facts.head_sha, "success", description), report.notes, "set the status"
            ):
                report.published["status"] = {"state": "success", "description": description[:140]}
        return report

    if not facts.open:
        state = facts.state or "not open"
        return skipped(f"{name} is {state}; nothing to review.", f"pull request is {state}")
    if facts.draft and not options.review_drafts:
        return skipped("Draft pull request: skipped. Set review-drafts to review drafts.", "draft pull request")

    route = ci_env.resolve_model_route(environ, options.provider)
    if route is not None:
        # The drivers read the process environment, whatever environment the gate was decided from.
        ci_env.apply_model_route(route, os.environ)
    problem, why = engine.route_problem()
    if problem == "disabled":
        return skipped(f"AI review is turned off ({why}); nothing was reviewed.", "AI review is turned off")
    if problem:
        raise PrReviewError(
            "no model route is configured: set ANTHROPIC_API_KEY or OPENAI_API_KEY (or LEMONCROW_LLM_BACKEND with "
            f"its provider variables). {why}"
        )

    if writes:
        _best_effort(lambda: github.set_status(facts.head_sha, "pending", "Reviewing…"), [], "set the pending status")
    try:
        report = _review(options, ref, github, run, structured_executor, say, writes)
        if report.skipped and writes:
            # The pending status must not be left hanging on a run that decided not to review.
            _best_effort(
                lambda: github.set_status(facts.head_sha, "success", report.reason), report.notes, "set the status"
            )
    except Exception as exc:
        if writes:
            _best_effort(
                lambda: github.set_status(facts.head_sha, "error", "Review failed; see the workflow log."),
                [],
                "set the status",
            )
        if isinstance(exc, (PrReviewError, GitHubCliError, engine.AiStageError, ValueError)):
            raise PrReviewError(str(exc)) from exc
        raise
    return report


def _best_effort(action: Callable[[], object], notes: list[str], what: str) -> bool:
    """Run a write whose failure must not fail the review (a missing permission, a lost race)."""

    try:
        action()
    except GitHubCliError as exc:
        notes.append(f"could not {what}: {str(exc)[:200]}")
        return False
    return True


def _review(
    options: RunOptions,
    ref: PullRequestRef,
    github: BotGitHub,
    run: GhRunner,
    structured_executor: Callable[..., Any] | None,
    say: Callable[[str], None],
    writes: bool,
) -> RunReport:
    from lemoncrow.pro.capabilities.review.pr_bot import engine

    name = f"{ref.slug}#{ref.number}"
    existing, previous = github.summary()
    say("Fetching the pull request")
    pull = resolve_pull_request(options.repo_root, options.pr, runner=run)
    head = pull.head_sha
    with tempfile.TemporaryDirectory(prefix="lemoncrow-pr-review-") as scratch:
        say("Capturing the change")
        captured = engine.capture_pull_request(options.repo_root, pull, Path(scratch), with_impact=options.with_impact)
        say("Running the AI review" if previous is None else "Re-reviewing what changed")
        ai = engine.run_ai_stage(
            captured, depth=options.depth, previous=previous, structured_executor=structured_executor
        )
    if not ai.ran:
        return _skip(name, f"AI review did not run: {ai.reason}")

    merged = reconcile(
        previous,
        ai.findings,
        locations=ai.locations,
        current_targets=ai.target_ids,
        rereviewed=ai.rereviewed,
        covered=ai.covered,
        head=short(head),
        answers=ai.answers,
        intact=ai.intact,
    )
    report = RunReport(pr=name, head_sha=head)
    if ai.errors:
        report.notes.append("model call errors: " + " | ".join(error[:200] for error in ai.errors))
    findings = list(merged.findings)

    comments: list[Remote] = []
    if options.publish:
        comments = _best_effort_value(github.review_comments, report.notes, "read the review comments") or []
    findings = _adopt(github, findings, comments)
    wanted = [
        finding
        for finding in findings
        if finding.is_open
        and finding.severity in INLINE_SEVERITIES
        and finding.thread_id == 0
        and not finding.placement
    ]
    # A thread is answered once, in the run that closes its finding; a finding left "no longer reported" stays listed.
    was_open = {old.id for old in previous.open_findings()} if previous is not None else set()
    closed = [
        finding for finding in findings if not finding.is_open and finding.thread_id > 0 and finding.id in was_open
    ]
    same_head = previous is not None and previous.head_sha == head
    report.noop = bool(ai.status == "nothing_changed" and same_head and previous is not None and previous.complete)
    report.inline_bodies = tuple(inline_comment(finding) for finding in wanted)

    if writes and wanted:
        findings = _post_inline(github, findings, wanted, head, report)
    if writes and closed:
        threads = _best_effort_value(
            lambda: github.reply_and_resolve(closed, comments), report.notes, "update the threads"
        )
        if threads is not None:
            report.published["replied"] = threads.replied
            report.published["resolved"] = threads.resolved
            report.notes.extend(threads.notes)
    if writes and (wanted or closed):
        report.noop = False

    tracked, dropped = cap_findings(findings)
    usage = ai.usage
    reviews = 1 if previous is None else previous.reviews + (0 if report.noop else 1)
    total = usage if previous is None else previous.usage_total.plus(usage)
    state = ReviewState(
        head_sha=head,
        base_sha=pull.base_sha,
        merge_base_sha=pull.merge_base_sha,
        pull_request=name,
        depth=options.depth,
        model=ai.model,
        policy=ai.policy,
        complete=ai.complete,
        reviews=reviews,
        reviewed_units=ai.reviewed_units,
        units_truncated=ai.units_truncated,
        findings=tuple(tracked),
        findings_dropped=dropped,
        usage=usage,
        usage_total=total,
    )
    reason = "model calls failed; see the workflow log" if ai.errors else (ai.reason if not ai.complete else "")
    uninvestigated = max(ai.candidates - ai.investigated, 0)
    view = verdict(tracked, complete=ai.complete, reason=reason, signals=ai.candidates, uninvestigated=uninvestigated)
    summary_view = SummaryView(
        state=state,
        findings=tuple(tracked),
        facts=RunFacts(
            first_review=previous is None,
            changed_lines=ai.changed_lines,
            changed_files=ai.changed_files,
            new=len(_state_is(tracked, "new")),
            carried=len(_state_is(tracked, "carried")),
            fixed=len(_state_is(tracked, "fixed")),
            stale=len(_state_is(tracked, "stale")),
            unreported=len(_state_is(tracked, "unreported")),
            complete=ai.complete,
            uninvestigated=uninvestigated,
        ),
        receipt=Receipt(
            files=ai.files,
            targets=len(ai.target_ids),
            candidates=ai.candidates,
            investigated=ai.investigated,
            rules=ai.rules,
            unchanged=ai.unchanged,
        ),
        pull_number=ref.number,
        repository=ref.slug,
        head_sha=head,
        commit_url=f"https://{ref.host}/{ref.slug}/commit/{head}",
        incomplete_reason=reason,
        model_label=ai.model,
        depth_label=options.depth.capitalize(),
        max_calls=ai.max_calls,
        fail_on=options.fail_on,
    )
    body, posted_state = render_summary(summary_view)
    status = check_status(view, fail_on=options.fail_on)
    report.findings = tuple(tracked)
    report.state = posted_state
    report.summary = body
    report.verdict = view
    report.status = status

    if writes:
        link = existing.url if existing is not None else ""
        if not report.noop:
            written = _best_effort_value(
                lambda: github.write_summary(body, existing), report.notes, "write the summary comment"
            )
            if written is not None:
                link = written.url
                report.published["summary_url"] = written.url
        elif existing is not None:
            report.published["summary_url"] = existing.url
        _best_effort(lambda: github.set_status(head, status[0], status[1], link), report.notes, "set the commit status")
        report.published["status"] = {"state": status[0], "description": status[1]}
    elif options.dry_run:
        report.published["dry_run"] = True
    return report


def _state_is(findings: list[BotFinding], state: str) -> list[BotFinding]:
    return [finding for finding in findings if finding.state == state]


def _best_effort_value(action: Callable[[], Any], notes: list[str], what: str) -> Any:
    try:
        return action()
    except GitHubCliError as exc:
        notes.append(f"could not {what}: {str(exc)[:200]}")
        return None


def _adopt(github: BotGitHub, findings: list[BotFinding], comments: list[Remote]) -> list[BotFinding]:
    """Give each finding the inline comment it already has on the pull request, found by its marker."""

    found = github.adopt_existing([finding for finding in findings if finding.thread_id == 0], comments)
    if not found:
        return findings
    adopted: list[BotFinding] = []
    for finding in findings:
        comment = found.get(finding.id)
        if comment is not None and finding.thread_id == 0:
            finding = finding.posted(
                thread_id=comment.id, thread_url=comment.url or github.comment_url(comment.id), placement="inline"
            )
        adopted.append(finding)
    return adopted


def _post_inline(
    github: BotGitHub, findings: list[BotFinding], wanted: list[BotFinding], head: str, report: RunReport
) -> list[BotFinding]:
    outcome = _best_effort_value(lambda: github.post_inline(wanted, head), report.notes, "post the inline review")
    if outcome is None:
        return findings
    if outcome.skipped_reason:
        report.notes.append(f"inline comments not posted: {outcome.skipped_reason}")
    report.published["inline"] = len(outcome.posted)
    if outcome.review_url:
        report.published["review_url"] = outcome.review_url
    updated: list[BotFinding] = []
    for finding in findings:
        posted = outcome.posted.get(finding.id)
        if posted is not None:
            # The comment sits on the lines it was sent on, which GitHub's own diff may have narrowed from what the
            # Snapshot allowed. The finding keeps a suggestion the comment lost: an agent with the checkout can use it.
            sent = outcome.sent.get(finding.id, finding)
            start, end = sent.span
            finding = finding.changed(
                thread_id=posted[0],
                thread_url=posted[1],
                placement="inline",
                line=start,
                end_line=end,
                anchor=sent.anchor,
            )
        elif finding.id in outcome.unplaced:
            # Not on a line GitHub shows: the summary lists it, and later runs do not retry.
            finding = finding.changed(placement="summary")
        updated.append(finding)
    return updated


__all__ = ["FAIL_ON", "PrReviewError", "RunOptions", "RunReport", "run_pr_review"]
