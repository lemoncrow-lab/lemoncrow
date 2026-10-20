"""GitHub's secondary rate limits (R11): wait and try again a few times, and only for a rate limit.

``gh api`` reports a throttled call as an error carrying the message GitHub sent. The bot used to note a failed
write and leave it to the next push; now it waits (the ``Retry-After`` GitHub gave, when the error carries it,
else a short growing backoff), bounded per call and per run, because a CI job has a timeout.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import github as github_module
from lemoncrow.pro.capabilities.review.pr_bot.github import BACKOFF_DELAYS, with_backoff
from lemoncrow.pro.capabilities.review.pr_bot.runner import RunOptions, run_pr_review
from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError
from tests.gateway.pr_bot_support import BASE, FIRST_PUSH, NUMBER, SLUG, World, build_world

SECONDARY = (
    "You have exceeded a secondary rate limit. Please wait a few minutes before you try again. "
    "If you reach out to GitHub Support for help, please include the request ID ABCD:1234. (HTTP 403)"
)
FORBIDDEN = "Resource not accessible by integration (HTTP 403)"


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(github_module.time, "sleep", waits.append)
    return waits


class Throttled:
    """A runner that fails with *message* for its first *times* calls, then answers ``ok``."""

    def __init__(self, message: str, times: int) -> None:
        self.message, self.times, self.calls = message, times, 0

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        self.calls += 1
        if self.calls <= self.times:
            raise GitHubCliError(self.message)
        return "ok"


def test_a_throttled_call_waits_a_growing_backoff_and_then_succeeds(slept: list[float]) -> None:
    inner = Throttled(SECONDARY, 2)
    assert with_backoff(inner)(["api", "x"], None) == "ok"
    assert inner.calls == 3 and slept == [5.0, 15.0]


def test_the_wait_github_asked_for_wins_over_the_default_and_is_capped(slept: list[float]) -> None:
    assert with_backoff(Throttled(f"{SECONDARY} Retry-After: 7", 1))(["api"], None) == "ok"
    assert (
        with_backoff(Throttled("API rate limit exceeded. Try again in 12 seconds. (HTTP 429)", 1))(["api"], None)
        == "ok"
    )
    assert with_backoff(Throttled("HTTP 403 (HTTP 403) retry-after: 600", 1))(["api"], None) == "ok"
    assert slept == [7.0, 12.0, 60.0]


@pytest.mark.parametrize("message", ["Too Many Requests (HTTP 429)", SECONDARY, "abuse detection mechanism (HTTP 403)"])
def test_429_and_secondary_limits_are_retried(slept: list[float], message: str) -> None:
    assert with_backoff(Throttled(message, 1))(["api"], None) == "ok" and slept == [5.0]


def test_a_limit_that_does_not_lift_fails_after_the_bounded_retries_and_says_so(slept: list[float]) -> None:
    inner = Throttled(SECONDARY, 99)
    with pytest.raises(GitHubCliError, match=r"secondary rate limit.*still rate limited after 3 retries and 65s"):
        with_backoff(inner)(["api"], None)
    assert inner.calls == len(BACKOFF_DELAYS) + 1 and slept == list(BACKOFF_DELAYS)


def test_a_run_never_waits_longer_than_its_budget_in_total(slept: list[float]) -> None:
    run = with_backoff(Throttled(f"{SECONDARY} Retry-After: 50", 99), budget=120)
    for _ in range(5):
        with pytest.raises(GitHubCliError, match="still rate limited"):
            run(["api"], None)
    assert sum(slept) <= 120 and slept == [50.0, 50.0], "the third wait would cross the budget, so it is not taken"


@pytest.mark.parametrize(
    "message",
    [FORBIDDEN, "Not Found (HTTP 404)", "Validation Failed (HTTP 422)", "`gh api` timed out after 60s", "HTTP 403"],
)
def test_nothing_else_is_retried_waiting_would_not_change_it(slept: list[float], message: str) -> None:
    inner = Throttled(message, 99)
    with pytest.raises(GitHubCliError):
        with_backoff(inner)(["api"], None)
    assert inner.calls == 1 and slept == []


# --------------------------------------------------------------------------- #
# in the run
# --------------------------------------------------------------------------- #


class Flaky:
    """The fake GitHub, except that the first review POST and the first status POST are throttled."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.refused: list[str] = []

    def __call__(self, args: Any, stdin: str | None = None) -> str:
        call = list(args)
        for needle in ("/reviews", "/statuses/"):
            if call[:3] == ["api", "--method", "POST"] and needle in call[3] and needle not in self.refused:
                self.refused.append(needle)
                raise GitHubCliError(SECONDARY)
        return self.world.github(args, stdin)


def test_a_throttled_review_post_is_waited_out_and_the_comment_is_posted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push(FIRST_PUSH)
    flaky = Flaky(world)
    report = run_pr_review(
        RunOptions(pr=str(NUMBER), repo_root=world.origin.clone, publish=True),
        runner=flaky,
        structured_executor=world.model,
    )
    assert sorted(flaky.refused) == ["/reviews", "/statuses/"] and slept == [5.0, 5.0]
    assert world.github.review_posts == 1 and len(world.github.inline()) == 1, "a refused post is not a posted one"
    assert not [note for note in report.notes if "could not" in note]
    assert world.github.last_status()["state"] == "success" and f"{SLUG}" in report.pr


def test_a_forbidden_status_is_still_just_a_note_and_never_slept_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push(FIRST_PUSH)
    world.github.deny_status = True
    report = world.review()
    assert slept == [] and any("could not set the commit status" in note for note in report.notes)
