"""The bot's GitHub calls: one summary comment, one atomic inline review, threads, a commit status.

Everything goes through ``gh api`` (a :data:`GhRunner`), which the Actions runner
authenticates with ``GH_TOKEN``. The identity is whatever owns that token -- the
workflow's ``GITHUB_TOKEN`` posts as ``github-actions[bot]`` -- so AI output is
authored by a bot and never by the person who happened to run the command.

This is the bot publish path, separate from the human one in ``github_publish`` and
``api.py``, which keep refusing every non-human annotation. Two rules hold here:

* A review is only ever posted with event ``COMMENT``. The bot never approves and
  never requests changes, whatever the findings say.
* Only comments GitHub types as ``Bot`` are trusted as the bot's own. The summary
  comment carries the state the next run continues from; a comment anyone can write
  must not be able to supply it.

Placement and the atomic review POST are ``github_publish`` (hunk-aware placement, a
retry adopts the review it already made); this module adds what a bot needs on top.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from lemoncrow.pro.capabilities.review.github_placement import index_files, unplaceable_reason
from lemoncrow.pro.capabilities.review.github_publish import (
    GitHubReviewPlan,
    PublishCandidate,
    PullRequestState,
    build_plan,
    load_bot_pull_request_state,
    publish_plan,
)
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from lemoncrow.pro.capabilities.review.pr_bot.render import STATUS_CONTEXT, inline_comment, reply_text
from lemoncrow.pro.capabilities.review.pr_bot.state import (
    FINDING_MARKER_PREFIX,
    SUMMARY_MARKER,
    ReviewState,
    parse_state,
)
from lemoncrow.pro.capabilities.review.sources.github import (
    GhRunner,
    GitHubCliError,
    PullRequestRef,
    gh_json,
    gh_paginated,
)

BOT_TYPE: Final[str] = "Bot"
_FINDING_MARKER = re.compile(r"<!-- lemoncrow:finding (\S+) -->")
_FINGERPRINT_MARKER = re.compile(r"<!-- lemoncrow:fingerprint (fp-[0-9a-f]{16}) -->")
_RESOLVED_MARKER = re.compile(r"<!-- lemoncrow:resolved (\S+) -->")

_THREADS_QUERY: Final[str] = (
    "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){reviewThreads(first:100,after:$cursor){pageInfo{hasNextPage endCursor}"
    "nodes{id isResolved comments(first:1){nodes{databaseId}}}}}}}"
)
#: Every review thread with who resolved it, its comments (author, association) and the reactions on them: what G9
#: reads to learn from people. Pages of 100 threads; a thread past 30 comments or a comment past 30 reactions is
#: read as far as that (the first comment's reactions and the replies that matter are early).
_FEEDBACK_QUERY: Final[str] = (
    "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){reviewThreads(first:100,after:$cursor){pageInfo{hasNextPage endCursor}"
    "nodes{id isResolved resolvedBy{login __typename} comments(first:30){nodes{databaseId body "
    "author{login __typename} authorAssociation reactions(first:30){nodes{content user{login}}}}}}}}}}"
)
_LOGIN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_RESOLVE_MUTATION: Final[str] = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _feedback_thread(node: dict[str, Any]) -> FeedbackThread | None:
    comments: list[ThreadComment] = []
    for row in _mapping(node.get("comments")).get("nodes") or ():
        item = _mapping(row)
        identity = item.get("databaseId")
        if not isinstance(identity, int) or isinstance(identity, bool):
            continue
        author = _mapping(item.get("author"))
        reactions = tuple(
            Reaction(str(_mapping(r).get("content") or ""), str(_mapping(_mapping(r).get("user")).get("login") or ""))
            for r in _mapping(item.get("reactions")).get("nodes") or ()
        )
        comments.append(
            ThreadComment(
                id=identity,
                body=str(item.get("body") or ""),
                login=str(author.get("login") or ""),
                kind=str(author.get("__typename") or ""),
                association=str(item.get("authorAssociation") or "").upper(),
                reactions=reactions,
            )
        )
    if not comments:
        return None
    resolver = _mapping(node.get("resolvedBy"))
    return FeedbackThread(
        root_id=comments[0].id,
        resolved=node.get("isResolved") is True,
        resolved_by=str(resolver.get("login") or ""),
        resolved_by_kind=str(resolver.get("__typename") or ""),
        comments=tuple(comments),
    )


def finding_id_of(comment: Remote) -> str:
    """The id of the finding an inline comment of the bot's carries in its marker; ``""`` for any other comment.

    Only a comment typed ``Bot`` counts, the rule the summary comment follows: a person can write a marker too.
    """

    if not comment.by_bot:
        return ""
    found = _FINDING_MARKER.search(comment.body)
    return found[1] if found is not None else ""


# --------------------------------------------------------------------------- #
# GitHub's rate limits
# --------------------------------------------------------------------------- #

#: What ``gh`` prints when GitHub throttles a request (HTTP 403 or 429). A 403 for any other reason ("Resource not
#: accessible by integration") is a missing permission, which waiting cannot fix, so it never matches.
_RATE_LIMITED = re.compile(
    r"secondary rate limit|rate limit exceeded|abuse detection|too many requests|\(HTTP 429\)", re.IGNORECASE
)
_THROTTLED_403 = re.compile(r"\(HTTP 403\)")
#: A wait GitHub asked for, when the error text carries it (``Retry-After: 30``, ``try again in 30 seconds``).
_RETRY_AFTER = re.compile(r"retry[- ]after:?\s*(\d+)|(?:try again|wait) (?:in |for )?(\d+) seconds?", re.IGNORECASE)
#: Seconds to wait before each retry when GitHub does not say how long, and how many retries that is at most.
BACKOFF_DELAYS: Final[tuple[float, ...]] = (5.0, 15.0, 45.0)
#: One wait never exceeds this, whatever GitHub asks for, and one run never waits longer than the budget in total:
#: a CI job has a timeout, and a limit that lasts longer than this is for the next push to meet.
MAX_WAIT_S: Final[float] = 60.0
WAIT_BUDGET_S: Final[float] = 120.0


def _retry_after(message: str) -> float | None:
    found = _RETRY_AFTER.search(message)
    if found is None:
        return None
    return float(next(group for group in found.groups() if group is not None))


def _is_rate_limited(message: str) -> bool:
    return bool(_RATE_LIMITED.search(message)) or (
        bool(_THROTTLED_403.search(message)) and _retry_after(message) is not None
    )


def with_backoff(
    runner: GhRunner,
    *,
    delays: Sequence[float] = BACKOFF_DELAYS,
    max_wait: float = MAX_WAIT_S,
    budget: float = WAIT_BUDGET_S,
    sleep: Callable[[float], None] = time.sleep,
) -> GhRunner:
    """*runner* that waits and tries again when GitHub throttles a call, a bounded number of times.

    Only a rate limit (403 or 429) is retried, never a permission error, a timeout or a bad request, none of
    which waiting changes. The wait is the one GitHub asked for when the error says (capped at *max_wait*), else
    the next of *delays*. Each call retries at most ``len(delays)`` times and the run as a whole waits at most
    *budget* seconds, so a limit that outlasts a CI job fails the one write (noted, and repeated by the next push)
    instead of holding the job. A throttled write was refused, so retrying it cannot post twice.

    *sleep* is how a wait is spent. It is bound to the real ``time.sleep`` when this module loads, so a caller that
    wants to see or skip the waits passes its own instead of patching ``time.sleep``, which every other caller in the
    process shares (``subprocess`` polls with it).
    """

    waited = 0.0

    def run(args: Sequence[str], stdin: str | None = None) -> str:
        nonlocal waited
        retries = 0
        while True:
            try:
                return runner(args, stdin)
            except GitHubCliError as exc:
                message = str(exc)
                if not _is_rate_limited(message):
                    raise
                hinted = _retry_after(message)
                wait = min(
                    hinted if hinted is not None else (delays[retries] if retries < len(delays) else 0), max_wait
                )
                if retries >= len(delays) or waited + wait > budget:
                    raise GitHubCliError(
                        f"{message} (still rate limited after {retries} "
                        f"{'retry' if retries == 1 else 'retries'} and {waited:.0f}s of waiting)"
                    ) from exc
                sleep(wait)
                waited += wait
                retries += 1

    return run


@dataclass(frozen=True)
class PullFacts:
    """The live facts about the pull request that decide whether and where to review."""

    number: int
    state: str
    draft: bool
    title: str
    author: str
    head_sha: str
    head_repo: str
    base_repo: str
    html_url: str
    #: GitHub's ``author_association`` of the pull request's author, for the people-checks of chat and learning.
    author_association: str = ""

    @property
    def open(self) -> bool:
        return self.state == "open"

    @property
    def fork(self) -> bool:
        return not self.head_repo or self.head_repo.lower() != self.base_repo.lower()


@dataclass(frozen=True)
class Remote:
    """One comment on the pull request, issue-level or inline."""

    id: int
    body: str
    author: str
    author_type: str
    url: str
    reply_to: int = 0
    #: For an inline comment: its file and the lines (new side) it covers; ``0`` when GitHub no longer shows them.
    path: str = ""
    line: int = 0
    start_line: int = 0
    #: GitHub's ``author_association`` for the commenter (``OWNER``, ``MEMBER``, ``COLLABORATOR``, ``CONTRIBUTOR``, ...).
    association: str = ""

    @property
    def by_bot(self) -> bool:
        return self.author_type == BOT_TYPE


def _remote(row: dict[str, Any]) -> Remote | None:
    identity = row.get("id")
    if not isinstance(identity, int) or isinstance(identity, bool):
        return None
    user = _mapping(row.get("user"))
    reply = row.get("in_reply_to_id")

    def number(*keys: str) -> int:
        for key in keys:
            value = row.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return 0

    last = number("line", "original_line")
    return Remote(
        id=identity,
        body=str(row.get("body") or ""),
        author=str(user.get("login") or ""),
        author_type=str(user.get("type") or ""),
        url=str(row.get("html_url") or ""),
        reply_to=reply if isinstance(reply, int) and not isinstance(reply, bool) else 0,
        path=str(row.get("path") or ""),
        line=last,
        start_line=number("start_line", "original_start_line") or last,
        association=str(row.get("author_association") or "").upper(),
    )


@dataclass(frozen=True)
class InlineOutcome:
    """What posting the inline review did, per finding."""

    posted: dict[str, tuple[int, str]]
    """finding id -> (inline comment id, its URL), for comments created or adopted."""
    unplaced: tuple[str, ...]
    """Findings GitHub cannot show on a diff line; they live in the summary only."""
    review_url: str = ""
    skipped_reason: str = ""
    sent: dict[str, BotFinding] = field(default_factory=dict)
    """finding id -> the finding as it was sent: its suggestion and lines after GitHub's own diff was consulted."""
    adopted: tuple[str, ...] = ()
    """Findings in ``posted`` that already had a comment from the bot, found just before posting; nothing was sent for them."""


@dataclass(frozen=True, slots=True)
class Reaction:
    """One reaction on a comment. ``content`` is GitHub's enum (``THUMBS_UP``, ``THUMBS_DOWN``, ``LAUGH``, ...); ``login`` is
    empty when GitHub names no user (an app)."""

    content: str
    login: str


@dataclass(frozen=True, slots=True)
class ThreadComment:
    id: int
    body: str
    login: str
    kind: str
    association: str
    reactions: tuple[Reaction, ...] = ()


@dataclass(frozen=True, slots=True)
class FeedbackThread:
    """A review thread as people left it: whether and by whom it was resolved, and what was said and reacted to."""

    root_id: int
    resolved: bool
    resolved_by: str
    resolved_by_kind: str
    comments: tuple[ThreadComment, ...]


@dataclass(frozen=True)
class ThreadOutcome:
    replied: int = 0
    resolved: int = 0
    notes: tuple[str, ...] = ()


class BotGitHub:
    """GitHub, as the bot sees one pull request."""

    def __init__(self, pr: PullRequestRef, runner: GhRunner) -> None:
        self.pr = pr
        self.runner = runner

    # ----- reads -------------------------------------------------------- #

    def pull_facts(self) -> PullFacts:
        pull = gh_json(self.runner, ["api", self.pr.api_path])
        if not isinstance(pull, dict):
            raise GitHubCliError(f"GitHub returned an unexpected shape for {self.pr.url}")
        return PullFacts(
            number=self.pr.number,
            state=str(pull.get("state") or ""),
            draft=pull.get("draft") is True,
            title=str(pull.get("title") or ""),
            author=str(_mapping(pull.get("user")).get("login") or ""),
            head_sha=str(_mapping(pull.get("head")).get("sha") or ""),
            head_repo=str(_mapping(_mapping(_mapping(pull.get("head")).get("repo"))).get("full_name") or ""),
            base_repo=str(_mapping(_mapping(_mapping(pull.get("base")).get("repo"))).get("full_name") or ""),
            html_url=str(pull.get("html_url") or self.pr.url),
            author_association=str(pull.get("author_association") or "").upper(),
        )

    def _issue_path(self) -> str:
        return f"repos/{self.pr.slug}/issues/{self.pr.number}"

    def summary(self) -> tuple[Remote | None, ReviewState | None]:
        """The bot's summary comment (the oldest, if a race made two) and the state it carries."""

        found = [
            comment
            for comment in (
                _remote(row) for row in gh_paginated(self.runner, f"{self._issue_path()}/comments?per_page=100")
            )
            if comment is not None and comment.by_bot and SUMMARY_MARKER in comment.body
        ]
        if not found:
            return None, None
        first = min(found, key=lambda comment: comment.id)
        return first, parse_state(first.body)

    def review_comments(self) -> list[Remote]:
        rows = gh_paginated(self.runner, f"{self.pr.api_path}/comments?per_page=100")
        return [comment for comment in (_remote(row) for row in rows) if comment is not None]

    def issue_comments(self) -> list[Remote]:
        """The pull request's conversation comments (not the inline ones), oldest first."""

        rows = gh_paginated(self.runner, f"{self._issue_path()}/comments?per_page=100")
        return sorted(
            (comment for comment in (_remote(row) for row in rows) if comment is not None), key=lambda c: c.id
        )

    # ----- chat ---------------------------------------------------------- #

    def reply_in_thread(self, root_id: int, body: str) -> Remote:
        """Reply in the review thread whose first comment is *root_id* (GitHub takes replies to a thread's root only)."""

        written = _remote(
            _mapping(
                gh_json(
                    self.runner,
                    ["api", "--method", "POST", f"{self.pr.api_path}/comments/{root_id}/replies", "--input", "-"],
                    json.dumps({"body": body}, ensure_ascii=False),
                )
            )
        )
        if written is None:
            raise GitHubCliError(f"GitHub did not return the reply on thread {root_id} of {self.pr.url}")
        return replace(written, body=body)

    def react(self, comment_id: int, *, review_comment: bool, content: str = "eyes") -> None:
        """Put the reaction *content* (``eyes``, ``+1``, ...) on a comment: the quickest answer to "did the bot see it".

        *review_comment* is a comment in a review thread (``pull_request_review_comment``); otherwise it is a
        conversation comment. Covered by ``pull-requests: write``, like the replies.
        """

        kind = "pulls" if review_comment else "issues"
        gh_json(
            self.runner,
            ["api", "--method", "POST", f"repos/{self.pr.slug}/{kind}/comments/{comment_id}/reactions", "--input", "-"],
            json.dumps({"content": content}),
        )

    def post_comment(self, body: str) -> Remote:
        """A new comment in the pull request's conversation."""

        written = _remote(
            _mapping(
                gh_json(
                    self.runner,
                    ["api", "--method", "POST", f"{self._issue_path()}/comments", "--input", "-"],
                    json.dumps({"body": body}, ensure_ascii=False),
                )
            )
        )
        if written is None:
            raise GitHubCliError(f"GitHub did not return the comment on {self.pr.url}")
        return replace(written, body=body)

    # ----- the summary comment ------------------------------------------ #

    def write_summary(self, body: str, existing: Remote | None) -> Remote:
        """Create the summary, or update it in place -- and leave it alone when nothing changed."""

        if existing is not None:
            if existing.body == body:
                return existing
            updated = gh_json(
                self.runner,
                ["api", "--method", "PATCH", f"repos/{self.pr.slug}/issues/comments/{existing.id}", "--input", "-"],
                json.dumps({"body": body}, ensure_ascii=False),
            )
        else:
            updated = gh_json(
                self.runner,
                ["api", "--method", "POST", f"{self._issue_path()}/comments", "--input", "-"],
                json.dumps({"body": body}, ensure_ascii=False),
            )
        written = _remote(_mapping(updated))
        if written is None:
            raise GitHubCliError(f"GitHub did not return the summary comment for {self.pr.url}")
        return replace(written, body=body)

    # ----- inline comments ---------------------------------------------- #

    def comment_url(self, comment_id: int) -> str:
        return f"https://{self.pr.host}/{self.pr.slug}/pull/{self.pr.number}#discussion_r{comment_id}"

    def adopt_existing(
        self, findings: Sequence[BotFinding], comments: Sequence[Remote], *, tracked: Collection[str] = ()
    ) -> dict[str, Remote]:
        """Findings that already have an inline comment on the PR, found by their marker.

        This is what keeps a re-run, or a run whose summary write was lost, from posting a second comment for a
        finding. A comment is matched by the finding's id first. A finding the id does not find is matched by its
        fingerprint, which does not change when the model words the same concern differently (a cancelled run
        that posted its comment and lost its summary meets a reworded finding next time): the bot's own inline
        comment on the same file with the same fingerprint, one comment per finding. A comment whose finding was
        closed (the bot replied that it was fixed) is never matched, or a bug that comes back would be silenced by
        the thread of the one that was fixed; neither is a comment of another finding the review tracks
        (*tracked*: all their ids), which is why two bugs in one function keep a comment each, or a person's.
        """

        wanted = {finding.id for finding in findings}
        found: dict[str, Remote] = {}
        for comment in comments:
            if not comment.by_bot or comment.reply_to or FINDING_MARKER_PREFIX not in comment.body:
                continue
            match = _FINDING_MARKER.search(comment.body)
            if match is not None and match[1] in wanted:
                found.setdefault(match[1], comment)
        unmatched = [finding for finding in findings if finding.id not in found and finding.fingerprint]
        if not unmatched:
            return found
        closed = {
            match[1]
            for comment in comments
            if comment.by_bot
            for match in [_RESOLVED_MARKER.search(comment.body)]
            if match is not None
        }
        taken = {comment.id for comment in found.values()}
        for finding in unmatched:
            for comment in comments:
                if comment.id in taken or not comment.by_bot or comment.reply_to or comment.path != finding.path:
                    continue
                marked = _FINGERPRINT_MARKER.search(comment.body)
                owner = _FINDING_MARKER.search(comment.body)
                if marked is None or marked[1] != finding.fingerprint:
                    continue
                if owner is not None and owner[1] in closed | wanted | set(tracked):
                    continue  # a closed finding's thread, or one that belongs to a finding of its own
                found[finding.id] = comment
                taken.add(comment.id)
                break
        return found

    def post_inline(
        self,
        findings: Sequence[BotFinding],
        head_sha: str,
        *,
        dry_run: bool = False,
        tracked: Collection[str] = (),
    ) -> InlineOutcome:
        """Post the findings as one ``COMMENT`` review, each on its own diff line.

        Findings GitHub cannot place are returned as ``unplaced`` (the summary lists
        them); a PR that moved on is reported, not posted onto code it no longer holds.
        """

        if not findings:
            return InlineOutcome({}, ())
        # The last look before writing: a run that was cancelled while posting, or one that raced this, may have put
        # the comment there since this run read the pull request. Those findings are adopted, not posted again.
        existing = self.adopt_existing(findings, self.review_comments(), tracked=tracked)
        adopted = {
            finding_id: (comment.id, comment.url or self.comment_url(comment.id))
            for finding_id, comment in existing.items()
        }
        # An adopted finding sits where the comment it was adopted from sits.
        as_found = {
            finding.id: finding.changed(
                line=existing[finding.id].start_line or finding.line,
                end_line=existing[finding.id].line or finding.end_line,
                suggestion="",
                suggestion_start=0,
                suggestion_end=0,
            )
            for finding in findings
            if finding.id in existing and existing[finding.id].line > 0
        }
        findings = [finding for finding in findings if finding.id not in existing]
        if not findings:
            return InlineOutcome(adopted, (), sent=as_found, adopted=tuple(adopted))
        state = load_bot_pull_request_state(self.pr, self.runner)
        files = index_files(state.files)
        candidates = []
        sent: dict[str, BotFinding] = {}
        for finding in findings:
            if finding.line <= 0:
                continue
            finding = self._placeable_anchor(self._placeable_suggestion(finding, files), files)
            sent[finding.id] = finding
            start, end = finding.span
            candidates.append(
                PublishCandidate(
                    annotation_id=finding.id,
                    path=finding.path,
                    side=finding.side,
                    start_line=start,
                    end_line=end,
                    kind="comment",
                    body=inline_comment(finding),
                )
            )
        plan = build_plan(
            self.pr, candidates, commit_id=head_sha, outcome="", outcome_summary="", outcome_current=True, state=state
        )
        # The bot never approves or requests changes; this is the one place that is asserted.
        if plan.event != "COMMENT":
            raise GitHubCliError(f"refusing to post a {plan.event} review as the bot")
        placed = {item.annotation_id for item in plan.inline}
        unplaced = tuple(finding.id for finding in findings if finding.id not in placed)
        if not plan.inline or dry_run:
            return InlineOutcome(
                adopted,
                unplaced,
                skipped_reason="dry run" if dry_run and plan.inline else "",
                sent=as_found,
                adopted=tuple(adopted),
            )
        if state.head_sha and state.head_sha != head_sha:
            return InlineOutcome(
                adopted,
                unplaced,
                skipped_reason=f"the pull request moved to {state.head_sha[:7]}; the next run reviews it",
                sent=as_found,
                adopted=tuple(adopted),
            )
        bot_plan: GitHubReviewPlan = replace(plan, folded=(), summary="")
        operation_id = self.operation_id(head_sha, [item.annotation_id for item in plan.inline])
        result = publish_plan(bot_plan, operation_id, state, self.runner)
        posted = {
            annotation_id: (comment_id, self.comment_url(comment_id))
            for annotation_id, comment_id in result.comment_ids.items()
        }
        return InlineOutcome(
            {**adopted, **posted},
            unplaced,
            review_url=result.html_url,
            sent={**as_found, **sent},
            adopted=tuple(adopted),
        )

    @staticmethod
    def _placeable_anchor(finding: BotFinding, files: dict[str, Any]) -> BotFinding:
        """*finding* on its target's first changed line when GitHub's own diff cannot hold the lines it cites.

        The cited lines were checked against the captured Snapshot's hunks; GitHub attaches a comment only to lines
        inside a hunk of the diff it serves. The comment is kept, on the line its target's change begins at.
        """

        if finding.anchor != "cited" or finding.target_line <= 0:
            return finding
        start, end = finding.span
        pr_file = files.get(finding.path)
        if pr_file is not None and not unplaceable_reason(
            patch=pr_file.patch, anchor_side=finding.side, start_line=start, end_line=end
        ):
            return finding
        return finding.changed(
            line=finding.target_line,
            end_line=finding.target_line,
            anchor="target",
            suggestion="",
            suggestion_start=0,
            suggestion_end=0,
        )

    @staticmethod
    def _placeable_suggestion(finding: BotFinding, files: dict[str, Any]) -> BotFinding:
        """*finding*, without its suggestion when GitHub's own diff cannot hold the lines it replaces.

        The suggestion was checked against the captured Snapshot; GitHub only attaches a comment to lines inside a
        hunk of the diff it serves, so the same rule is applied to its patch. The finding keeps its comment, on
        its target's lines, and only loses the one-click edit.
        """

        if not finding.has_suggestion:
            return finding
        start, end = finding.span
        pr_file = files.get(finding.path)
        if pr_file is None or unplaceable_reason(
            patch=pr_file.patch, anchor_side=finding.side, start_line=start, end_line=end
        ):
            return finding.changed(suggestion="", suggestion_start=0, suggestion_end=0)
        return finding

    @staticmethod
    def operation_id(head_sha: str, finding_ids: Sequence[str]) -> str:
        """Deterministic, so a retry of the same findings on the same head adopts the review it already made."""

        digest = hashlib.sha256("\0".join(sorted(finding_ids)).encode("utf-8")).hexdigest()[:10]
        return f"bot-{head_sha[:12]}-{digest}"

    # ----- threads ------------------------------------------------------ #

    def reply_and_resolve(self, findings: Sequence[BotFinding], comments: Sequence[Remote]) -> ThreadOutcome:
        """Reply once on the thread of each closed finding, then resolve it if the token allows.

        A reply is the contract; resolving is best effort, because ``resolveReviewThread``
        needs a permission a token may not have, and a failed resolve must not fail the run.
        A finding that is only *no longer reported* gets its reply and keeps its thread open: nothing showed
        that it was fixed, so closing the conversation is not ours to do.
        """

        already = {
            match[1]
            for comment in comments
            if comment.by_bot
            for match in [_RESOLVED_MARKER.search(comment.body)]
            if match is not None
        }
        replied = resolved = 0
        notes: list[str] = []
        threads: dict[int, tuple[str, bool]] | None = None
        for finding in findings:
            if finding.thread_id <= 0:
                continue
            if finding.id not in already:
                gh_json(
                    self.runner,
                    [
                        "api",
                        "--method",
                        "POST",
                        f"{self.pr.api_path}/comments/{finding.thread_id}/replies",
                        "--input",
                        "-",
                    ],
                    json.dumps({"body": reply_text(finding)}, ensure_ascii=False),
                )
                replied += 1
            if finding.state == "unreported":
                continue
            try:
                if threads is None:
                    threads = self.review_threads()
                thread = threads.get(finding.thread_id)
                if thread is not None and not thread[1]:
                    self.resolve_thread(thread[0])
                    resolved += 1
            except GitHubCliError as exc:
                notes.append(f"could not resolve the thread for {finding.id}: {str(exc)[:160]}")
                threads = threads or {}
        return ThreadOutcome(replied, resolved, tuple(notes))

    def review_threads(self) -> dict[int, tuple[str, bool]]:
        """``{first comment id: (thread node id, is resolved)}`` for every review thread."""

        threads: dict[int, tuple[str, bool]] = {}
        cursor: str | None = None
        owner, _, name = self.pr.slug.partition("/")
        while True:
            data = gh_json(
                self.runner,
                ["api", "graphql", "--input", "-"],
                json.dumps(
                    {
                        "query": _THREADS_QUERY,
                        "variables": {"owner": owner, "name": name, "number": self.pr.number, "cursor": cursor},
                    }
                ),
            )
            if _mapping(data).get("errors"):
                raise GitHubCliError(str(_mapping(data)["errors"])[:200])
            page = _mapping(
                _mapping(_mapping(_mapping(_mapping(data).get("data")).get("repository")).get("pullRequest")).get(
                    "reviewThreads"
                )
            )
            for node in page.get("nodes") or ():
                nodes = _mapping(_mapping(node).get("comments")).get("nodes") or ()
                first = _mapping(nodes[0]) if nodes else {}
                database_id = first.get("databaseId")
                if isinstance(database_id, int) and isinstance(_mapping(node).get("id"), str):
                    threads[database_id] = (str(node["id"]), _mapping(node).get("isResolved") is True)
            info = _mapping(page.get("pageInfo"))
            if info.get("hasNextPage") is not True or not isinstance(info.get("endCursor"), str):
                return threads
            cursor = str(info["endCursor"])

    def feedback_threads(self) -> list[FeedbackThread]:
        """Every review thread with its comments, their reactions and who resolved it (one GraphQL call per 100 threads)."""

        found: list[FeedbackThread] = []
        cursor: str | None = None
        owner, _, name = self.pr.slug.partition("/")
        while True:
            data = gh_json(
                self.runner,
                ["api", "graphql", "--input", "-"],
                json.dumps(
                    {
                        "query": _FEEDBACK_QUERY,
                        "variables": {"owner": owner, "name": name, "number": self.pr.number, "cursor": cursor},
                    }
                ),
            )
            if _mapping(data).get("errors"):
                raise GitHubCliError(str(_mapping(data)["errors"])[:200])
            page = _mapping(
                _mapping(_mapping(_mapping(_mapping(data).get("data")).get("repository")).get("pullRequest")).get(
                    "reviewThreads"
                )
            )
            for node in page.get("nodes") or ():
                thread = _feedback_thread(_mapping(node))
                if thread is not None:
                    found.append(thread)
            info = _mapping(page.get("pageInfo"))
            if info.get("hasNextPage") is not True or not isinstance(info.get("endCursor"), str):
                return found
            cursor = str(info["endCursor"])

    def permission_of(self, login: str) -> str:
        """The repository permission GitHub reports for *login* (``admin``, ``write``, ``read``, ``none``), else ``""``.

        A reaction carries no ``author_association``, so a person who only reacted is looked up. Fails closed: a login
        that is not a user name, a refusal and a missing user are all ``""``.
        """

        if not _LOGIN.fullmatch(login):
            return ""
        try:
            data = gh_json(self.runner, ["api", f"repos/{self.pr.slug}/collaborators/{login}/permission"])
        except GitHubCliError:
            return ""
        return str(_mapping(data).get("permission") or "")

    def resolve_thread(self, thread_id: str) -> None:
        data = gh_json(
            self.runner,
            ["api", "graphql", "--input", "-"],
            json.dumps({"query": _RESOLVE_MUTATION, "variables": {"id": thread_id}}),
        )
        if _mapping(data).get("errors"):
            raise GitHubCliError(str(_mapping(data)["errors"])[:200])

    # ----- the check ---------------------------------------------------- #

    def set_status(self, sha: str, state: str, description: str, target_url: str = "") -> None:
        """The ``LemonCrow Review`` commit status on the reviewed head (needs ``statuses: write``)."""

        body: dict[str, str] = {"state": state, "context": STATUS_CONTEXT, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        gh_json(
            self.runner,
            ["api", "--method", "POST", f"repos/{self.pr.slug}/statuses/{sha}", "--input", "-"],
            json.dumps(body, ensure_ascii=False),
        )


__all__ = [
    "BotGitHub",
    "FeedbackThread",
    "InlineOutcome",
    "PullFacts",
    "PullRequestState",
    "Reaction",
    "Remote",
    "ThreadComment",
    "ThreadOutcome",
    "finding_id_of",
    "with_backoff",
]
