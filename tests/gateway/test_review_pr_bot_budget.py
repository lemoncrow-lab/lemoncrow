"""A per-pull-request budget (R7): the cost of one review is capped, but nothing capped a pull request's total.

The summary's hidden state already carries the cumulative usage (``usage_total``) and the number of reviews, so
the budget is enforced from it: no service, no counter of our own. When it is reached the summary says so, nothing
new is reviewed, and the commit status is a success (a commit status has no neutral state) that says nothing was
reviewed. An earlier open P0 still fails the check under ``fail-on: p0``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review.pr_bot.runner import PrReviewError, RunOptions, budget_reached, run_pr_review
from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState, Usage, parse_state
from tests.gateway.pr_bot_support import (
    AUTH_BUGGY,
    BASE,
    FIRST_PUSH,
    NUMBER,
    RULE,
    World,
    build_world,
    one_call_per_file,
)

TITLE = "lets a missing principal through"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    built = build_world(tmp_path, monkeypatch, BASE)
    built.model.input_tokens = 1_000_000  # $0.15 a call on the priced default model, so a dollar cap is reachable
    return built


def summary(world: World) -> str:
    return str(world.github.summary_comments()[0]["body"])


# --------------------------------------------------------------------------- #
# the cap on the number of reviews
# --------------------------------------------------------------------------- #


def test_after_the_allowed_reviews_a_push_is_not_reviewed_and_the_summary_says_so(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    first = world.review(max_runs_per_pr=1)
    assert first.state is not None and first.state.reviews == 1 and world.model.calls
    inline_before, posts_before = len(world.github.inline()), world.github.review_posts
    world.model.reset()

    world.origin.push({"src/plain.py": "VALUE = 3\n", "src/token.py": "def verify(token):\n    return token == 'x'\n"})
    report = world.review(max_runs_per_pr=1)

    assert world.model.calls == [], "no model is called once the budget is reached"
    assert report.verdict is not None and report.verdict.state == "budget"
    headline = "Review budget reached: 1 review used of 1 allowed"
    assert report.verdict.headline == headline
    assert report.status == ("success", f"{headline}; this push was not reviewed")
    body = summary(world)
    assert body.split("\n")[1].startswith(f"**{headline}**")
    assert "This push was not reviewed" in body and "max-runs-per-pr" in body
    # The status is on the new head, the findings of the last review stand, nothing new is posted or replied to.
    last = world.github.last_status()
    assert (last["sha"], last["state"]) == (world.origin.head, "success")
    assert [row.id for row in report.findings] == [f.id for f in first.findings]
    assert all(f.state == "carried" for f in report.findings if f.is_open)
    assert len(world.github.inline()) == inline_before and world.github.review_posts == posts_before
    assert world.github.replies() == []
    assert report.published["status"]["description"].startswith(headline)
    # The state a later run continues from is intact: still one review, the old head, the old units.
    state = parse_state(body)
    assert state is not None and state.reviews == 1 and state.head_sha != world.origin.head
    assert state.reviewed_units == first.state.reviewed_units and state.usage_total == first.state.usage_total


def test_a_larger_budget_picks_up_where_the_cap_stopped_it(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review(max_runs_per_pr=1)
    world.origin.push({"src/plain.py": "VALUE = 3\n", "src/token.py": "def verify(token):\n    return token == 'x'\n"})
    assert world.review(max_runs_per_pr=1).verdict.state == "budget"  # type: ignore[union-attr]
    world.model.reset()

    resumed = world.review(max_runs_per_pr=2)
    assert resumed.verdict is not None and resumed.verdict.state != "budget"
    assert resumed.state is not None and resumed.state.reviews == 2
    assert world.model.calls, "the push that was skipped is reviewed now"
    assert "Review budget" not in summary(world)


def test_a_rerun_of_a_head_that_was_already_reviewed_is_not_a_budget_event(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review(max_runs_per_pr=1)
    world.model.reset()
    again = world.review(max_runs_per_pr=1)
    assert again.noop and again.verdict is not None and again.verdict.state != "budget"
    assert world.model.calls == []


def test_without_a_budget_nothing_changes(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    report = world.review()
    assert report.state is not None and report.state.reviews == 2 and "Review budget" not in summary(world)


# --------------------------------------------------------------------------- #
# the cap on dollars
# --------------------------------------------------------------------------- #


def test_a_dollar_cap_is_enforced_from_the_cumulative_cost_in_the_state(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_call_per_file(monkeypatch)  # the three files would share one call; the cost below is of one call each
    world.origin.push(FIRST_PUSH)
    first = world.review(max_usd_per_pr=1.00)
    assert first.state is not None and first.state.usage_total.cost_usd is not None
    spent = first.state.usage_total.cost_usd
    assert 0.2 < spent < 1.0, "three priced calls of a million tokens"

    # Under the cap a second review still runs (and adds to the total).
    world.origin.push({"src/token.py": "def verify(token):\n    return token == 'x'\n"}, "add token")
    second = world.review(max_usd_per_pr=1.00)
    assert second.state is not None and second.state.reviews == 2 and second.state.usage_total.cost_usd > spent  # type: ignore[operator]

    # At or over the cap it does not.
    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 3\n"}, "another push")
    stopped = world.review(max_usd_per_pr=spent)
    assert stopped.verdict is not None and stopped.verdict.state == "budget"
    assert stopped.verdict.headline.startswith("Review budget reached: $") and "allowed" in stopped.verdict.headline
    assert world.model.calls == []


def test_a_dollar_cap_cannot_hold_a_cost_nobody_knows_so_it_says_so_and_the_run_cap_still_works(world: World) -> None:
    world.model.model = "an-unpriced-model-xyz"
    world.origin.push(FIRST_PUSH)
    first = world.review(max_usd_per_pr=0.0001)
    assert first.state is not None and first.state.usage_total.cost_usd is None
    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    second = world.review(max_usd_per_pr=0.0001)
    assert second.verdict is not None and second.verdict.state != "budget", "an unknown cost is never counted as spent"
    assert any("max-usd-per-pr is not enforced" in note for note in second.notes)
    world.origin.push({"src/plain.py": "VALUE = 4\n"})
    third = world.review(max_runs_per_pr=2, max_usd_per_pr=0.0001)
    assert third.verdict is not None and third.verdict.state == "budget"


def test_budget_reached_reads_only_the_state() -> None:
    options = RunOptions(pr="7", repo_root=Path("."), max_usd_per_pr=1.0, max_runs_per_pr=3)
    fresh = ReviewState(head_sha="a" * 40)
    assert budget_reached(options, None) == "" and budget_reached(options, fresh) == ""
    runs = ReviewState(head_sha="a" * 40, reviews=3, usage_total=Usage(3, 1, 1, 0.1))
    assert budget_reached(options, runs) == "3 reviews used of 3 allowed"
    spend = ReviewState(head_sha="a" * 40, reviews=1, usage_total=Usage(3, 1, 1, 1.25))
    assert budget_reached(options, spend) == "$1.25 spent of $1.00 allowed"
    both = ReviewState(head_sha="a" * 40, reviews=4, usage_total=Usage(3, 1, 1, 2.0))
    assert budget_reached(options, both) == "4 reviews used of 3 allowed and $2.00 spent of $1.00 allowed"
    unknown = ReviewState(head_sha="a" * 40, reviews=1, usage_total=Usage(3, None, None, None))
    assert budget_reached(options, unknown) == ""
    assert budget_reached(RunOptions(pr="7", repo_root=Path(".")), runs) == ""


# --------------------------------------------------------------------------- #
# what a spent budget does not do
# --------------------------------------------------------------------------- #


def test_an_open_p0_from_the_last_review_still_fails_the_check_when_the_budget_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, {**BASE, ".lemoncrow/review.yaml": RULE})
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    first = world.review(fail_on="p0", max_runs_per_pr=1)
    assert first.status is not None and first.status[0] == "failure"
    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    calls = len(world.model.calls)
    report = world.review(fail_on="p0", max_runs_per_pr=1)
    assert len(world.model.calls) == calls, "nothing was reviewed"
    assert report.verdict is not None and report.verdict.state == "must_fix"
    assert report.status == ("failure", "1 must-fix before merge")
    # The headline is the P0, which is still open; the strip under it says why nothing newer was looked at.
    assert summary(world).split("\n")[1].startswith("**1 must-fix before merge**")
    assert "This push was not reviewed" in summary(world)


def test_a_dry_run_over_budget_writes_nothing(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review(max_runs_per_pr=1)
    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    body, statuses = summary(world), len(world.github.statuses)
    report = world.review(max_runs_per_pr=1, dry_run=True)
    assert report.verdict is not None and report.verdict.state == "budget" and report.published == {"dry_run": True}
    assert summary(world) == body and len(world.github.statuses) == statuses


@pytest.mark.parametrize("fields", [{"max_usd_per_pr": 0.0}, {"max_usd_per_pr": -1.0}, {"max_runs_per_pr": 0}])
def test_a_limit_that_cannot_mean_anything_is_refused(world: World, fields: dict[str, Any]) -> None:
    world.origin.push(FIRST_PUSH)
    with pytest.raises(PrReviewError, match="max-usd-per-pr must be above 0"):
        run_pr_review(
            RunOptions(pr=str(NUMBER), repo_root=world.origin.clone, publish=True, **fields),
            runner=world.github,
            structured_executor=world.model,
        )
    assert world.github.calls == []


def test_the_cli_takes_both_limits_and_rejects_nonsense(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import intelligence_runtime
    from lemoncrow.pro.capabilities.review.pr_bot import runner as bot_runner

    world.origin.push(FIRST_PUSH)
    world.review(max_runs_per_pr=1)
    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(bot_runner, "run_gh", lambda args, stdin=None, cwd=None: world.github(args, stdin))
    monkeypatch.setattr(intelligence_runtime, "execute_structured", world.model)

    def invoke(*args: str) -> Any:
        root = str(world.root / "store")
        return CliRunner().invoke(
            cli, ["--root", root, "review", "--repo-root", str(world.origin.clone), "--pr", "7", "--ai", *args]
        )

    stopped = invoke("--max-runs-per-pr", "1", "--json")
    assert stopped.exit_code == 0, stopped.output
    assert json.loads(stopped.stdout)["verdict"]["state"] == "budget"
    assert invoke("--max-runs-per-pr", "0").exit_code != 0
    assert invoke("--max-usd-per-pr", "0").exit_code != 0
    assert invoke("--max-usd-per-pr", "lots").exit_code != 0
    help_text = CliRunner().invoke(cli, ["review", "--help"]).output
    assert "--max-usd-per-pr" in help_text and "--max-runs-per-pr" in help_text
