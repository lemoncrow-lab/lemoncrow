"""Publish a Review's human feedback to its GitHub pull request as one review.

Two halves, kept apart so the first is testable without a network:

* :func:`build_plan` is pure. It turns the Review's publishable comments, the
  reviewer's verdict and GitHub's own view of the PR diff into a deterministic
  :class:`GitHubReviewPlan`. The plan *is* the preview; its hash binds the
  confirm step to exactly what the reviewer was shown.
* :func:`publish_plan` posts that plan as **one** ``POST .../reviews`` through the
  user's ``gh`` login, so GitHub creates every comment or none.

GitHub only accepts a review comment on a line inside one of *its* diff hunks.
A comment that cannot be placed there -- file-level, orphaned, outside a hunk,
on a file GitHub shows no patch for -- is moved into the review body with its
location. It is never dropped.

Retries are safe: the body carries ``<!-- lemoncrow:op=<operation id> -->`` and a
retry first looks for a review of the viewer's that already carries it.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lemoncrow.pro.capabilities.review.github_placement import (
    github_side,
    hunk_ranges,
    index_files,
    unplaceable_reason,
)
from lemoncrow.pro.capabilities.review.sources.github import (
    GhRunner,
    GitHubCliError,
    PullRequestRef,
    gh_json,
    gh_paginated,
)

EVENT_BY_OUTCOME: dict[str, str] = {
    "lgtm": "APPROVE",
    "changes_requested": "REQUEST_CHANGES",
    "comment": "COMMENT",
}

_KIND_PREFIX: dict[str, str] = {
    "comment": "",
    "request_change": "**Change requested:** ",
    "suggestion": "**Suggestion:** ",
    "looks_good": "**Looks good:** ",
}

_MARKER = "<!-- lemoncrow:op={operation_id} -->"
_MARKER_PREFIX = "<!-- lemoncrow:op="

# GitHub's stored state for each review event.
_REVIEW_STATE: dict[str, str] = {
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
}


@dataclass(frozen=True)
class PublishCandidate:
    """One root human comment the Review could publish."""

    annotation_id: str
    path: str
    side: str
    start_line: int
    end_line: int
    kind: str
    body: str
    state: str = "open"
    anchor_detail: str = ""


@dataclass(frozen=True)
class InlineComment:
    annotation_id: str
    path: str
    side: str
    start_line: int
    line: int
    body: str

    def payload(self) -> dict[str, Any]:
        row: dict[str, Any] = {"path": self.path, "side": self.side, "line": self.line, "body": self.body}
        if self.start_line != self.line:
            row["start_line"] = self.start_line
            row["start_side"] = self.side
        return row


@dataclass(frozen=True)
class FoldedComment:
    annotation_id: str
    location: str
    body: str
    reason: str


@dataclass(frozen=True)
class PullRequestFile:
    """GitHub's view of one changed file: its path and diff patch (None when GitHub shows none)."""

    path: str
    patch: str | None
    previous_path: str = ""


@dataclass(frozen=True)
class RemoteReview:
    """One review the viewer already has on the PR."""

    id: int
    body: str
    commit_id: str
    state: str
    html_url: str = ""


@dataclass(frozen=True)
class PullRequestState:
    """Live PR facts that decide what GitHub will accept."""

    head_sha: str
    author: str
    viewer: str
    open: bool
    files: tuple[PullRequestFile, ...] = field(default_factory=tuple)
    viewer_reviews: tuple[RemoteReview, ...] = field(default_factory=tuple)
    repository_id: str = ""
    """The base repository's durable GitHub id: what later edits and dismissals are addressed by."""
    host: str = ""
    """The GitHub host that served the pull request (its ``html_url``): where that id is unique."""

    def marked_review(self, operation_id: str) -> RemoteReview | None:
        """The viewer's review a previous attempt of *operation_id* already created."""
        marker = _MARKER.format(operation_id=operation_id)
        return next((item for item in self.viewer_reviews if marker in item.body), None)

    def verdict_published(self, commit_id: str, event: str, summary: str) -> bool:
        """Whether LemonCrow already put this verdict (and summary) on GitHub for *commit_id*."""
        wanted = _REVIEW_STATE.get(event, "")
        return any(
            item.commit_id == commit_id
            and item.state == wanted
            and _MARKER_PREFIX in item.body
            and (not summary or summary in item.body)
            for item in self.viewer_reviews
        )


@dataclass(frozen=True)
class GitHubReviewPlan:
    pr: PullRequestRef
    commit_id: str
    event: str
    summary: str
    inline: tuple[InlineComment, ...]
    folded: tuple[FoldedComment, ...]
    notices: tuple[str, ...] = ()

    @property
    def annotation_ids(self) -> tuple[str, ...]:
        return (*(item.annotation_id for item in self.inline), *(item.annotation_id for item in self.folded))

    @property
    def empty(self) -> bool:
        return not self.inline and not self.folded and not self.summary and self.event == "COMMENT"

    def body(self, operation_id: str) -> str:
        parts: list[str] = []
        if self.summary:
            parts.append(self.summary)
        if self.folded:
            lines = ["**Comments that GitHub cannot place on a diff line**", ""]
            for item in self.folded:
                text = item.body.replace("\n", "\n  ")
                lines.append(f"- `{item.location}`: {text}")
            parts.append("\n".join(lines))
        parts.append(_MARKER.format(operation_id=operation_id))
        return "\n\n".join(parts)

    def payload(self, operation_id: str) -> dict[str, Any]:
        return {
            "commit_id": self.commit_id,
            "event": self.event,
            "body": self.body(operation_id),
            "comments": [item.payload() for item in self.inline],
        }

    @property
    def plan_hash(self) -> str:
        """Identity of what the reviewer confirms; independent of the operation id."""
        canonical = json.dumps(self.payload(""), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def preview(self) -> dict[str, Any]:
        return {
            "pr": {"owner": self.pr.owner, "repo": self.pr.repo, "number": self.pr.number, "url": self.pr.url},
            "commit_id": self.commit_id,
            "event": self.event,
            "summary": self.summary,
            "inline": [
                {
                    "annotation_id": item.annotation_id,
                    "path": item.path,
                    "side": item.side,
                    "start_line": item.start_line,
                    "line": item.line,
                    "body": item.body,
                }
                for item in self.inline
            ],
            "folded": [
                {
                    "annotation_id": item.annotation_id,
                    "location": item.location,
                    "body": item.body,
                    "reason": item.reason,
                }
                for item in self.folded
            ],
            "notices": list(self.notices),
            "plan_hash": self.plan_hash,
        }


def _location(candidate: PublishCandidate) -> str:
    if candidate.start_line <= 0:
        return candidate.path
    end = max(candidate.end_line, candidate.start_line)
    span = f"L{candidate.start_line}" + (f"-L{end}" if end != candidate.start_line else "")
    return f"{candidate.path}:{span}" + (" (old)" if candidate.side == "old" else "")


def _comment_body(candidate: PublishCandidate) -> str:
    return f"{_KIND_PREFIX.get(candidate.kind, '')}{candidate.body.strip()}"


def build_plan(
    pr: PullRequestRef,
    candidates: Sequence[PublishCandidate],
    *,
    commit_id: str,
    outcome: str,
    outcome_summary: str,
    outcome_current: bool,
    state: PullRequestState,
) -> GitHubReviewPlan:
    """Place every candidate inline or in the body, and pick the review event."""

    files = index_files(state.files)

    inline: list[InlineComment] = []
    folded: list[FoldedComment] = []
    ordered = sorted(candidates, key=lambda c: (c.path, c.start_line, c.end_line, c.annotation_id))
    for candidate in ordered:
        body = _comment_body(candidate)
        if candidate.kind == "looks_good" and not candidate.body.strip():
            continue
        reason = ""
        side = github_side(candidate.side)
        pr_file = files.get(candidate.path)
        start = candidate.start_line
        end = max(candidate.end_line, start)
        if candidate.state == "orphaned":
            reason = candidate.anchor_detail or "its line no longer exists in this revision"
        elif pr_file is None and start > 0 and side:
            reason = "file is not part of the pull request diff"
        else:
            reason = unplaceable_reason(
                patch=pr_file.patch if pr_file else None,
                anchor_side=candidate.side,
                start_line=start,
                end_line=end,
            )
        if reason:
            folded.append(
                FoldedComment(
                    annotation_id=candidate.annotation_id,
                    location=_location(candidate),
                    body=body,
                    reason=reason,
                )
            )
            continue
        assert pr_file is not None
        inline.append(
            InlineComment(
                annotation_id=candidate.annotation_id,
                path=pr_file.path,
                side=side,
                start_line=start,
                line=end,
                body=body,
            )
        )

    notices: list[str] = []
    event = EVENT_BY_OUTCOME.get(outcome, "COMMENT") if outcome_current else "COMMENT"
    summary = outcome_summary.strip() if outcome_current else ""
    if outcome and not outcome_current:
        notices.append("Your verdict belongs to an older revision, so this publishes as Comment.")
    if event != "COMMENT" and state.viewer and state.viewer.lower() == state.author.lower():
        notices.append(
            "GitHub does not let you approve or request changes on your own pull request; publishing as Comment."
        )
        event = "COMMENT"
    if event != "COMMENT" and not state.open:
        notices.append("The pull request is closed; publishing as Comment.")
        event = "COMMENT"
    if (event != "COMMENT" or summary) and state.verdict_published(commit_id, event, summary):
        notices.append("Your verdict for this commit is already on GitHub; only new comments are published.")
        event, summary = "COMMENT", ""
    return GitHubReviewPlan(
        pr=pr,
        commit_id=commit_id,
        event=event,
        summary=summary,
        inline=tuple(inline),
        folded=tuple(folded),
        notices=tuple(notices),
    )


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _served_host(raw: object) -> str:
    """The host of a GitHub ``html_url``; ``""`` when absent or not an http(s) URL."""

    try:
        page = urllib.parse.urlsplit(raw) if isinstance(raw, str) else None
    except ValueError:
        return ""
    return page.netloc if page is not None and page.scheme in ("http", "https") else ""


def load_pull_request_state(pr: PullRequestRef, runner: GhRunner) -> PullRequestState:
    """Live head, author, viewer, open state and per-file patches from GitHub."""

    pull = gh_json(runner, ["api", pr.api_path])
    viewer = gh_json(runner, ["api", "user"])
    if not isinstance(pull, dict) or not isinstance(viewer, dict):
        raise GitHubCliError(f"GitHub returned an unexpected shape for {pr.url}")
    head = _mapping(pull.get("head"))
    user = _mapping(pull.get("user"))
    repository_id = _mapping(_mapping(pull.get("base")).get("repo")).get("id")
    files = tuple(
        PullRequestFile(
            path=str(row.get("filename") or ""),
            patch=row.get("patch") if isinstance(row.get("patch"), str) else None,
            previous_path=str(row.get("previous_filename") or ""),
        )
        for row in gh_paginated(runner, f"{pr.api_path}/files?per_page=100")
        if row.get("filename")
    )
    login = str(viewer.get("login") or "")
    reviews: list[RemoteReview] = []
    for row in gh_paginated(runner, f"{pr.api_path}/reviews?per_page=100"):
        reviewer = _mapping(row.get("user"))
        if not login or str(reviewer.get("login") or "").lower() != login.lower() or not isinstance(row.get("id"), int):
            continue
        reviews.append(
            RemoteReview(
                id=int(row["id"]),
                body=str(row.get("body") or ""),
                commit_id=str(row.get("commit_id") or ""),
                state=str(row.get("state") or ""),
                html_url=str(row.get("html_url") or ""),
            )
        )
    return PullRequestState(
        head_sha=str(head.get("sha") or ""),
        author=str(user.get("login") or ""),
        viewer=login,
        open=str(pull.get("state") or "") == "open",
        files=files,
        viewer_reviews=tuple(reviews),
        repository_id=(
            str(repository_id) if isinstance(repository_id, int) and not isinstance(repository_id, bool) else ""
        ),
        host=_served_host(pull.get("html_url")),
    )


def ensure_current_head(plan: GitHubReviewPlan, state: PullRequestState) -> None:
    if state.head_sha and state.head_sha != plan.commit_id:
        raise GitHubCliError(
            f"{plan.pr.url} has new commits ({state.head_sha[:12]}) since this Review revision "
            f"({plan.commit_id[:12]}); run `lc review --pr {plan.pr.number}` to refresh, then publish"
        )


@dataclass(frozen=True)
class PublishResult:
    review_id: int
    html_url: str
    adopted: bool
    comment_ids: Mapping[str, int]

    def remote_refs(self, annotation_ids: Sequence[str]) -> dict[str, str]:
        """Per-annotation remote id: its inline comment, else the review that carries it."""
        return {
            annotation_id: (
                f"github-comment:{self.comment_ids[annotation_id]}"
                if annotation_id in self.comment_ids
                else f"github-review:{self.review_id}"
            )
            for annotation_id in annotation_ids
        }


def _match_comments(plan: GitHubReviewPlan, remote: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    unused = [dict(row) for row in remote if isinstance(row.get("id"), int)]
    matched: dict[str, int] = {}
    for item in plan.inline:
        for index, row in enumerate(unused):
            line = row.get("line") or row.get("original_line")
            if (
                row.get("path") == item.path
                and str(row.get("body") or "").strip() == item.body.strip()
                and (line in (None, item.line))
            ):
                matched[item.annotation_id] = int(row["id"])
                del unused[index]
                break
    return matched


def publish_plan(plan: GitHubReviewPlan, operation_id: str, state: PullRequestState, runner: GhRunner) -> PublishResult:
    """Post *plan* once; adopt the review a previous attempt already created."""

    ensure_current_head(plan, state)
    existing = state.marked_review(operation_id)
    if existing is not None:
        review_id, html_url, adopted = existing.id, existing.html_url, True
    else:
        created = gh_json(
            runner,
            ["api", "--method", "POST", f"{plan.pr.api_path}/reviews", "--input", "-"],
            json.dumps(plan.payload(operation_id), ensure_ascii=False),
        )
        if not isinstance(created, dict) or not isinstance(created.get("id"), int):
            raise GitHubCliError(f"GitHub did not return a review id for {plan.pr.url}")
        review_id, html_url, adopted = int(created["id"]), str(created.get("html_url") or ""), False
    comments = (
        gh_paginated(runner, f"{plan.pr.api_path}/reviews/{review_id}/comments?per_page=100") if plan.inline else []
    )
    return PublishResult(
        review_id=review_id,
        html_url=html_url or plan.pr.url,
        adopted=adopted,
        comment_ids=_match_comments(plan, comments),
    )


__all__ = [
    "EVENT_BY_OUTCOME",
    "FoldedComment",
    "GitHubReviewPlan",
    "InlineComment",
    "PublishCandidate",
    "PublishResult",
    "PullRequestFile",
    "PullRequestState",
    "RemoteReview",
    "build_plan",
    "ensure_current_head",
    "hunk_ranges",
    "load_pull_request_state",
    "publish_plan",
]
