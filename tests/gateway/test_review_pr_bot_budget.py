"""A per-pull-request budget (R7): the cost of one review is capped, but nothing capped a pull request's total.

The summary's hidden state already carries the cumulative usage (``usage_total``) and the number of reviews, so
the budget is enforced from it: no service, no counter of our own. When it is reached the summary says so, nothing
new is reviewed, and the commit status is a success (a commit status has no neutral state) that says nothing was
reviewed. An earlier open P0 still fails the check under ``fail-on: p0`` (an open P1 too under ``p1``, see
``test_review_pr_bot_severity``), and the job with it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review.intelligence_runtime import StructuredExecution
from lemoncrow.pro.capabilities.review.pr_bot.render import money
from lemoncrow.pro.capabilities.review.pr_bot.runner import PrReviewError, RunOptions, budget_reached, run_pr_review
from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState, Usage, parse_state
from tests.gateway.pr_bot_support import (
    AUTH_BUGGY,
    BASE,
    FIRST_PUSH,
    NUMBER,
    RULE,
    FakeModel,
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
    assert any(line.startswith(f"<h2>{headline}") for line in body.split("\n"))
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


def test_the_cap_is_held_during_a_run_and_a_crossing_ends_it_as_a_partial_review_that_names_what_it_reached(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1.5: before every model call, spent so far plus the next call's estimate is checked against the cap."""

    one_call_per_file(monkeypatch)
    world.model.input_tokens = 4_000_000  # $0.60 a call
    world.origin.push(FIRST_PUSH)
    report = world.review(max_usd_per_pr=1.00, fail_on="p0")

    asked = len(world.model.calls)
    assert 0 < asked < 3, "some files were reviewed and the call that would pass the cap was never made"
    assert report.state is not None and report.state.usage_total.cost_usd is not None
    assert report.state.usage_total.cost_usd <= 1.00 + 0.61, "at most the calls in flight pass the cap"
    assert report.verdict is not None and report.verdict.state == "partial"
    assert "stopped at the cost cap" in report.verdict.headline
    # Never a failed job, even under a gate: the owner set the cap.
    assert report.status is not None and report.status[0] == "success" and report.exit_code == 0
    body = summary(world)
    assert "Stopped at the cost cap." in body and "Reviewed: " in body and "Not reviewed: " in body
    assert "of $1.00 for this PR" in body and " this run · " in body
    # What was not reached is pending: the next run with budget left picks it up.
    assert report.state.complete is False


def test_the_default_cap_is_three_dollars_and_none_turns_it_off(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review.pr_bot import spend

    assert spend.DEFAULT_MAX_USD_PER_PR == 3.00
    assert spend.resolve_max_usd(None, {}) == 3.00 and spend.resolve_max_usd("", {}) == 3.00
    assert spend.resolve_max_usd(None, {"LEMONCROW_REVIEW_MAX_USD": "none"}) is None
    assert spend.resolve_max_usd(None, {"LEMONCROW_REVIEW_MAX_USD": "$5"}) == 5.0
    with pytest.raises(ValueError, match="or `none` for no cap"):
        spend.resolve_max_usd(None, {"LEMONCROW_REVIEW_MAX_USD": "lots"})

    one_call_per_file(monkeypatch)
    world.model.input_tokens = 12_000_000  # $1.80 a call: two of them pass the default
    world.origin.push(FIRST_PUSH)
    capped = world.review()
    assert capped.verdict is not None and "stopped at the cost cap" in capped.verdict.headline
    assert "of $3.00 for this PR" in summary(world)

    monkeypatch.setenv("LEMONCROW_REVIEW_MAX_USD", "none")
    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 3\n"}, "another push")
    free = world.review()
    assert free.verdict is not None and "cost cap" not in free.verdict.headline
    assert "no cap (max-usd-per-pr: none)" in summary(world)

    monkeypatch.setenv("LEMONCROW_REVIEW_MAX_USD", "-2")
    with pytest.raises(PrReviewError, match="max-usd-per-pr must be a number"):
        world.review()


class ClaudeCliModel(FakeModel):
    """What the claude CLI route returns: a model name the price table cannot price, and a cost the CLI reported."""

    cli_cost: float | None = 0.04

    def __init__(self) -> None:
        super().__init__()
        self.model = "sonnet"

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        done = super().__call__(**kwargs)
        currency = None if self.cli_cost is None else "USD"
        return replace(done, usage=replace(done.usage, provider_cost=self.cli_cost, currency=currency))


def test_a_dollar_cap_holds_on_a_route_whose_cli_reports_its_cost(world: World) -> None:
    """The claude CLI route has no price in the local table ("sonnet" is an alias): its own `total_cost_usd` is the cost."""

    world.monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", "sonnet")  # the route's model: unknown to the price table
    world.model = ClaudeCliModel()
    world.origin.push(FIRST_PUSH)
    first = world.review(max_usd_per_pr=0.10)
    assert world.model.calls and first.state is not None and first.state.usage_total.cost_usd is not None
    spent = first.state.usage_total.cost_usd
    asked = len(world.model.calls) + len(world.model.overviews) + len(world.model.dedupes)  # each call is billed
    assert spent == pytest.approx(0.04 * asked) and spent > 0.05
    assert f"est. {money(spent)}" in summary(world), "the summary's usage line shows the cost"
    assert not any("max-usd-per-pr is not enforced" in note for note in first.notes)

    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 3\n"}, "another push")
    stopped = world.review(max_usd_per_pr=spent)
    assert stopped.verdict is not None and stopped.verdict.state == "budget", "the cap now stops the next review"
    assert world.model.calls == []


def test_a_route_that_reports_no_cost_is_never_uncapped_it_is_held_by_model_calls(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.pr_bot import spend

    monkeypatch.setattr(spend, "UNPRICED_CALLS_PER_PR", 2)
    one_call_per_file(monkeypatch)
    world.monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", "sonnet")
    world.model = ClaudeCliModel()
    world.model.cli_cost = None  # a route whose CLI reports tokens but no cost (Codex, OpenCode)
    world.origin.push(FIRST_PUSH)
    first = world.review(max_usd_per_pr=0.10)
    assert len(world.model.calls) + len(world.model.overviews) == 2, "held to the call cap, not to an invented price"
    assert first.state is not None and first.state.usage_total.cost_usd is None
    assert " est. $" not in summary(world), "no cost line rather than an invented price"
    words = "cost not reported by this route; capped at 2 model calls"
    assert words in summary(world) and first.status is not None and words in first.status[1]
    assert len(first.status[1]) <= 140 and first.status[0] == "success" and first.exit_code == 0

    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    second = world.review(max_usd_per_pr=0.10)
    assert second.verdict is not None and second.verdict.state == "budget" and world.model.calls == []
    assert "2 of 2 model calls used (cost not reported by this route)" in second.verdict.headline
    assert second.status is not None and words in second.status[1]


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
    spent_calls = ReviewState(head_sha="a" * 40, reviews=1, usage_total=Usage(60, None, None, None))
    assert budget_reached(options, spent_calls) == "60 of 60 model calls used (cost not reported by this route)"
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
    assert report.exit_code == 1, "fail-on makes the job fail exactly when the status fails, a spent budget included"
    # The headline is the P0, which is still open; the strip under it says why nothing newer was looked at.
    assert any(line.startswith("<h2>1 must-fix before merge") for line in summary(world).split("\n"))
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


def test_max_usd_per_pr_none_lifts_the_cap_and_the_help_names_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    import click

    from lemoncrow.pro.capabilities.review.local_cli.main import MaxUsd
    from lemoncrow.pro.capabilities.review.pr_bot import spend

    monkeypatch.setenv(spend.MAX_USD_ENV, "")  # restored after the test, whatever the option writes
    assert MaxUsd().convert("2.5", None, None) == 2.5
    assert MaxUsd().convert("None", None, None) is None
    assert spend.resolve_max_usd(None, os.environ) is None, "the review reads no cap, not the $3.00 default"
    with pytest.raises(click.BadParameter):
        MaxUsd().convert("0", None, None)
    text = " ".join(CliRunner().invoke(cli, ["review", "--help"]).output.split())
    assert "`none` for no cap" in text and "Default: $3.00 per pull request" in text
