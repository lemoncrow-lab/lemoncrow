"""Refresh the proof-of-work block of a review summary once CI finishes: no checkout, no model call, no spend.

The review reads CI on the head it reviewed while CI is often still running, so its proof-of-work block says "still
running". A second, optional workflow on ``workflow_run: completed`` (the setup guide, "Proof of work after CI")
runs this Action again; the gate sees the event and this module runs instead of the review:

* the pull requests come from the event's ``workflow_run.pull_requests`` (GitHub leaves it empty for a fork, whose
  pull requests the review skips anyway), only those of this repository;
* the summary is touched only when the head its state names is exactly the ``head_sha`` CI finished on. A summary of
  another head is left alone: the review of the newer head will read CI itself;
* only the block between ``**Proof of work**`` and its last ``- `` bullet is replaced, with :func:`render.proof_lines`
  for what CI reports now (the review's own job left out, as the review left it out). Nothing else in the comment, and
  not the state block, changes, and an unchanged block is not written.

A ``pull_request_review`` run (``submitted`` or ``dismissed``; the setup guide, "The check to require") re-sets the
``LemonCrow / human review`` status on the pull request's head instead: ``success`` once an owner, member or
collaborator has a standing approval of exactly that head, else the summary's own reasons again
(:func:`render.human_review_status`). It reads the summary's hidden state, never the code, and calls no model.

It never raises and always exits 0: a refresh that cannot read or write says why in the log and changes nothing.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lemoncrow.pro.capabilities.review.pr_bot import ci_env, impact
from lemoncrow.pro.capabilities.review.pr_bot.github import BotGitHub, with_backoff
from lemoncrow.pro.capabilities.review.pr_bot.render import (
    HUMAN_REVIEW_CONTEXT,
    approved_on,
    human_review_status,
    proof_lines,
    short,
)
from lemoncrow.pro.capabilities.review.sources.github import GhRunner, GitHubCliError, PullRequestRef, run_gh

#: The first line of the block (:func:`render.proof_lines`), and the claim line it carries when the description says
#: the tests pass. The claim is kept as the review decided it: the refresh does not read the description again.
PROOF_HEADER = "**Proof of work** · "
CLAIM = "- The description says tests pass;"
#: The ``pull_request_review`` actions that can change whether this head is approved (``edited`` changes only text).
REVIEW_ACTIONS = frozenset({"submitted", "dismissed"})


@dataclass(frozen=True)
class Refreshed:
    """What the refresh did for one pull request: ``updated`` when the summary was rewritten, else why not."""

    number: int
    updated: bool
    reason: str


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def event_pulls(event: Mapping[str, object], repository: str) -> tuple[str, list[int]]:
    """The head CI finished on and the pull requests of *repository* it belongs to, from a ``workflow_run`` payload."""

    run = _mapping(event.get("workflow_run"))
    sha = str(run.get("head_sha") or "").strip()
    numbers: list[int] = []
    rows = run.get("pull_requests")
    for row in rows if isinstance(rows, list) else ():
        pull = _mapping(row)
        number = pull.get("number")
        base = str(_mapping(_mapping(pull.get("base")).get("repo")).get("url") or "")
        if base and not base.lower().endswith(f"/repos/{repository.lower()}"):
            continue  # a pull request of another repository whose branch has the same name
        if isinstance(number, int) and not isinstance(number, bool) and number > 0 and number not in numbers:
            numbers.append(number)
    return sha, numbers


def replace_proof(body: str, block: Sequence[str]) -> str | None:
    """*body* with its proof-of-work block replaced by *block*; ``None`` when the body has no such block."""

    lines = body.split("\n")
    start = next((index for index, line in enumerate(lines) if line.startswith(PROOF_HEADER)), -1)
    if start < 0 or not block:
        return None
    end = start + 1
    while end < len(lines) and not lines[end].strip():
        end += 1
    while end < len(lines) and lines[end].startswith("- "):
        end += 1
    return "\n".join([*lines[:start], *block, *lines[end:]])


def refresh(repository: str, number: int, sha: str, runner: GhRunner) -> Refreshed:
    """Rewrite the proof-of-work block of pull request *number*'s summary for *sha*, or say why it was left alone."""

    owner, _, repo = repository.partition("/")
    github = BotGitHub(PullRequestRef(owner, repo, number), runner)
    try:
        existing, state = github.summary()
        if existing is None or state is None:
            return Refreshed(number, False, "no review summary on this pull request yet")
        if state.head_sha != sha:
            return Refreshed(
                number,
                False,
                f"the summary is for {short(state.head_sha)} and CI finished on {short(sha)}; left alone",
            )
        claimed = any(line.startswith(CLAIM) for line in existing.body.split("\n"))
        ci = github.ci_results(sha, exclude_run=state.review_run)
        body = replace_proof(existing.body, proof_lines(ci, claimed=claimed))
        if body is None:
            return Refreshed(number, False, "the summary has no proof-of-work block to refresh")
        if body == existing.body:
            return Refreshed(number, False, f"the proof of work for {short(sha)} is already current")
        github.write_summary(body, existing)
    except GitHubCliError as exc:
        return Refreshed(number, False, f"could not refresh it: {exc}")
    return Refreshed(number, True, f"proof of work refreshed for {short(sha)}")


def review_pull(event: Mapping[str, object]) -> tuple[int, str]:
    """The pull request number and its current head sha, from a ``pull_request_review`` payload; ``(0, "")`` if absent."""

    pull = _mapping(event.get("pull_request"))
    number = pull.get("number")
    sha = str(_mapping(pull.get("head")).get("sha") or "").strip()
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return 0, ""
    return number, sha


def refresh_human_review(repository: str, number: int, sha: str, runner: GhRunner, link: str = "") -> Refreshed:
    """Re-set the ``LemonCrow / human review`` status of pull request *number* on its head *sha*."""

    owner, _, repo = repository.partition("/")
    github = BotGitHub(PullRequestRef(owner, repo, number), runner)
    try:
        existing, state = github.summary()
        if existing is None or state is None:
            return Refreshed(number, False, "no review summary on this pull request yet; the review sets the status")
        approved = approved_on(sha, github.approvals(trusted=True))
        # The summary's reasons hold for the head it reviewed; a head it did not review was not assessed.
        weighed = impact.Impact.from_state(state.impact)
        tier = human_review_status(weighed, approved_by=approved, assessed=state.head_sha == sha)
        github.set_status(sha, tier[0], tier[1], link, context=HUMAN_REVIEW_CONTEXT)
    except GitHubCliError as exc:
        return Refreshed(number, False, f"could not set the human review status: {exc}")
    return Refreshed(number, True, f"human review status on {short(sha)}: {tier[0]}, {tier[1]}")


def run(env: Mapping[str, str], runner: GhRunner | None = None) -> list[Refreshed]:
    """Refresh every pull request the ``workflow_run`` event names, or the human-review status of the one a
    ``pull_request_review`` event names; ``[]`` for any other event."""

    name = str(env.get("GITHUB_EVENT_NAME") or "")
    if name == "pull_request_review":
        event = ci_env.load_event(env)
        repository = str(env.get("GITHUB_REPOSITORY") or "").strip()
        number, sha = review_pull(event)
        if str(event.get("action") or "") not in REVIEW_ACTIONS or not (repository and number and sha):
            return []
        from lemoncrow.pro.capabilities.review.pr_bot.runner import run_url

        gh = with_backoff(runner or (lambda args, stdin: run_gh(args, stdin)))
        return [refresh_human_review(repository, number, sha, gh, run_url(env))]
    if name != "workflow_run":
        return []
    event = ci_env.load_event(env)
    if str(event.get("action") or "") != "completed":
        return []
    repository = str(env.get("GITHUB_REPOSITORY") or "").strip()
    sha, numbers = event_pulls(event, repository)
    if not (repository and sha):
        return []
    gh = with_backoff(runner or (lambda args, stdin: run_gh(args, stdin)))
    return [refresh(repository, number, sha, gh) for number in numbers]


def main() -> int:
    done = run(os.environ)
    if not done:
        print("::notice title=LemonCrow Review::No pull request of this repository in this run; nothing to refresh.")
    for item in done:
        print(f"::notice title=LemonCrow Review::#{item.number}: {item.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
