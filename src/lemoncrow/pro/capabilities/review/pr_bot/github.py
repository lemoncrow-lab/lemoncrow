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
from collections.abc import Sequence
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

# lc-debt: no retry or backoff on GitHub's secondary rate limits; a failed write is noted and the next push repeats it;
# upgrade: back off on 403/429 with Retry-After.
BOT_TYPE: Final[str] = "Bot"
_FINDING_MARKER = re.compile(r"<!-- lemoncrow:finding (\S+) -->")
_RESOLVED_MARKER = re.compile(r"<!-- lemoncrow:resolved (\S+) -->")

_THREADS_QUERY: Final[str] = (
    "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){reviewThreads(first:100,after:$cursor){pageInfo{hasNextPage endCursor}"
    "nodes{id isResolved comments(first:1){nodes{databaseId}}}}}}}"
)
_RESOLVE_MUTATION: Final[str] = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


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

    @property
    def by_bot(self) -> bool:
        return self.author_type == BOT_TYPE


def _remote(row: dict[str, Any]) -> Remote | None:
    identity = row.get("id")
    if not isinstance(identity, int) or isinstance(identity, bool):
        return None
    user = _mapping(row.get("user"))
    reply = row.get("in_reply_to_id")
    return Remote(
        id=identity,
        body=str(row.get("body") or ""),
        author=str(user.get("login") or ""),
        author_type=str(user.get("type") or ""),
        url=str(row.get("html_url") or ""),
        reply_to=reply if isinstance(reply, int) and not isinstance(reply, bool) else 0,
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

    def adopt_existing(self, findings: Sequence[BotFinding], comments: Sequence[Remote]) -> dict[str, Remote]:
        """Findings that already have an inline comment on the PR, found by their marker.

        This is what keeps a re-run, or a run whose summary write was lost, from
        posting a second comment for a finding.
        """

        wanted = {finding.id for finding in findings}
        found: dict[str, Remote] = {}
        for comment in comments:
            if not comment.by_bot or comment.reply_to or FINDING_MARKER_PREFIX not in comment.body:
                continue
            match = _FINDING_MARKER.search(comment.body)
            if match is not None and match[1] in wanted:
                found.setdefault(match[1], comment)
        return found

    def post_inline(self, findings: Sequence[BotFinding], head_sha: str, *, dry_run: bool = False) -> InlineOutcome:
        """Post the findings as one ``COMMENT`` review, each on its own diff line.

        Findings GitHub cannot place are returned as ``unplaced`` (the summary lists
        them); a PR that moved on is reported, not posted onto code it no longer holds.
        """

        if not findings:
            return InlineOutcome({}, ())
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
            return InlineOutcome({}, unplaced, skipped_reason="dry run" if dry_run and plan.inline else "")
        if state.head_sha and state.head_sha != head_sha:
            return InlineOutcome(
                {}, unplaced, skipped_reason=f"the pull request moved to {state.head_sha[:7]}; the next run reviews it"
            )
        bot_plan: GitHubReviewPlan = replace(plan, folded=(), summary="")
        operation_id = self.operation_id(head_sha, [item.annotation_id for item in plan.inline])
        result = publish_plan(bot_plan, operation_id, state, self.runner)
        posted = {
            annotation_id: (comment_id, self.comment_url(comment_id))
            for annotation_id, comment_id in result.comment_ids.items()
        }
        return InlineOutcome(posted, unplaced, review_url=result.html_url, sent=sent)

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


__all__ = ["BotGitHub", "InlineOutcome", "PullFacts", "PullRequestState", "Remote", "ThreadOutcome"]
