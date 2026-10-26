"""``@lemoncrow review [quick|auto|thorough|deep]``: a whole-pull-request review started from a comment (item 3).

A real git origin, a fake GitHub that keeps what a pull request would hold (plus the reactions the bot puts on a
comment) and a fake model. The tests check what the command did: who it obeyed, what it spent, which depth it ran, what
it reused, and what the thread and the summary say. They do not check that a model is clever.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import chat, engine
from lemoncrow.pro.capabilities.review.pr_bot.chat import parse_command, review_depth
from lemoncrow.pro.capabilities.review.pr_bot.state import parse_state
from tests.gateway.pr_bot_support import (
    AUTH_FIXED,
    BASE,
    BOT,
    FIRST_PUSH,
    NUMBER,
    SLUG,
    FakeGitHub,
    World,
    build_world,
)

AUTH_TITLE = "lets a missing principal through"


class ReactiveGitHub(FakeGitHub):
    """The shared fake plus the one write it does not know: a reaction on a comment (``reactions`` here is its own list)."""

    def __init__(self, origin: Any) -> None:
        super().__init__(origin)
        self.bot_reactions: list[tuple[str, int, str]] = []
        """``(endpoint kind, comment id, content)`` for every reaction the bot put on a comment."""
        self.deny_reactions = False

    def _write(self, method: str, path: str, body: dict[str, Any]) -> str:
        found = re.fullmatch(rf"repos/{SLUG}/(issues|pulls)/comments/(\d+)/reactions", path)
        if method == "POST" and found is not None:
            if self.deny_reactions:
                from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError

                raise GitHubCliError("Resource not accessible by integration (HTTP 403)")
            self.bot_reactions.append((found[1], int(found[2]), body["content"]))
            return json.dumps({"id": 1, "content": body["content"], "user": BOT})
        return super()._write(method, path, body)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    made = build_world(tmp_path, monkeypatch, BASE)
    made.github = ReactiveGitHub(made.origin)
    return made


def github(world: World) -> ReactiveGitHub:
    assert isinstance(world.github, ReactiveGitHub)
    return world.github


def summary_body(world: World) -> str:
    (summary,) = world.github.summary_comments()
    return str(summary["body"])


def state_of(world: World) -> Any:
    state = parse_state(summary_body(world))
    assert state is not None
    return state


def reviewed(world: World, **options: Any) -> None:
    """Push the change and review it once the way a push does (Auto), then forget the model's calls."""

    world.origin.push(FIRST_PUSH)
    world.review(**options)
    world.model.reset()


def answer(world: World) -> str:
    return str(world.github.chat_replies()[-1]["body"])


# --------------------------------------------------------------------------- #
# the command and its depth
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("review", "review"),
        ("Review deep", "review"),
        ("please review at thorough", "review"),
        ("review this PR again", "review"),
        ("re-view", "review"),
        ("review turbo", "review"),
        ("review the auth change and say why it is risky", "ask"),
        ("review the auth change?", "ask"),
        ("reviewing the cache is slow", "ask"),
        ("recheck", "recheck"),
    ],
)
def test_review_is_a_command_only_when_it_names_nothing_or_one_depth(text: str, name: str) -> None:
    assert parse_command(text).name == name


@pytest.mark.parametrize(
    ("text", "default", "depth", "noted"),
    [
        ("", "auto", "auto", False),
        ("auto", "thorough", "auto", False),
        ("thorough", "auto", "thorough", False),
        ("at DEEP", "auto", "deep", False),
        ("", "thorough", "thorough", False),
        ("quick", "auto", "auto", True),
        ("quick", "deep", "auto", True),
        ("turbo", "auto", "auto", True),
        ("turbo", "deep", "deep", True),
    ],
)
def test_the_depth_is_the_one_asked_for_else_the_workflows_with_a_note_when_it_is_not_what_was_typed(
    text: str, default: str, depth: str, noted: bool
) -> None:
    got, note = review_depth(text, default)
    assert (got, bool(note)) == (depth, noted)
    assert got in engine.DEPTHS and set(engine.DEPTHS) <= set(chat.REVIEW_DEPTHS)


def test_the_note_for_an_unknown_depth_names_the_depths_and_cannot_forge_markup() -> None:
    _, note = review_depth("turbo", "auto")
    assert "`turbo`" in note and "quick, auto, thorough or deep" in note and "Auto" in note
    # The word is a single [a-z0-9_-] token, so nothing from a comment survives but plain text.
    assert parse_command("review <img src=x>").name == "ask"


def test_help_lists_the_review_command(world: World) -> None:
    reviewed(world)
    world.chat("@lemoncrow help")
    text = answer(world)
    assert "@lemoncrow review [quick|auto|thorough|deep]" in text and "counts toward the budget" in text
    assert world.model.chats == [] and world.model.calls == []


# --------------------------------------------------------------------------- #
# an authorised trigger runs the review at the depth it asked for
# --------------------------------------------------------------------------- #


def test_review_deep_runs_the_whole_review_at_deep_acknowledges_at_once_and_updates_the_summary(world: World) -> None:
    reviewed(world)
    assert state_of(world).depth == "auto" and state_of(world).reviews == 1
    earlier = len(world.github.statuses)

    report = world.chat("@lemoncrow review deep")

    assert not report.skipped and report.published["chat"]["kind"] == "review"
    assert report.published["chat"]["depth"] == "deep" and report.published["chat"]["reused"] is False
    # Deep's synthesis step ran: this was a Deep review, not a repeat of the Auto one.
    assert "hypotheses" in {call["category"] for call in world.model.calls}
    state = state_of(world)
    assert (state.depth, state.reviews) == ("deep", 2)
    assert "· Deep ·" in summary_body(world), "the summary footer says what depth reviewed it"
    # Acknowledged on the person's comment before the review ran, and the reply is in the conversation.
    comment = world.github.issue_comments[-2]  # the person's comment; the bot's reply is the last
    assert github(world).bot_reactions == [("issues", comment["id"], "eyes")]
    assert report.published["acknowledged"] == "eyes"
    text = answer(world)
    assert text.startswith(f"Reviewed `{world.origin.head[:7]}` at **Deep**: ")
    assert "[Summary](" in text and "2 reviews so far" in text
    assert world.github.last_status()["state"] == "success"
    assert [row["state"] for row in world.github.statuses[earlier:]] == ["success"], "no pending status to leave behind"
    assert len(world.github.summary_comments()) == 1, "the summary is updated in place"


def test_review_without_a_depth_runs_auto_on_a_draft_the_bot_has_not_seen(world: World) -> None:
    world.github.draft = True
    world.origin.push(FIRST_PUSH)
    assert world.review().skipped, "a push review skips a draft"

    report = world.chat("@lemoncrow review")

    assert report.published["chat"]["kind"] == "review" and report.published["chat"]["depth"] == "auto"
    assert state_of(world).depth == "auto" and state_of(world).reviews == 1
    assert {row["path"] for row in world.github.inline()} == {"src/auth.py"}, "P0/P1 findings get their comments"
    assert world.github.last_status()["description"] == "No must-fix findings; 1 should-fix · 1 optional"
    assert "Reviewed" in answer(world) and "**Auto**" in answer(world)


def test_the_workflows_depth_is_the_default_so_a_command_does_not_undo_it(world: World) -> None:
    reviewed(world, depth="thorough")
    world.model.reset()
    report = world.chat("@lemoncrow review", depth="thorough")
    assert report.published["chat"]["reused"] is True and world.model.calls == []
    assert state_of(world).depth == "thorough" and state_of(world).reviews == 1


def test_an_unknown_depth_falls_back_to_auto_with_a_note(world: World) -> None:
    reviewed(world)
    world.origin.push({"src/auth.py": AUTH_FIXED}, "fix it")
    report = world.chat("@lemoncrow review turbo")
    assert report.published["chat"]["depth"] == "auto" and state_of(world).depth == "auto"
    text = answer(world)
    assert text.startswith("I did not recognise the depth `turbo`") and "so I used Auto." in text
    assert "Reviewed" in text and state_of(world).head_sha == world.origin.head


def test_quick_runs_auto_and_says_why(world: World) -> None:
    reviewed(world)
    world.origin.push({"src/auth.py": AUTH_FIXED}, "fix it")
    report = world.chat("@lemoncrow review quick")
    assert report.published["chat"]["depth"] == "auto" and state_of(world).depth == "auto"
    assert "Quick cannot go below this repository's default depth, so I ran Auto" in answer(world)


def test_a_review_asked_from_a_finding_thread_is_answered_in_that_thread(world: World) -> None:
    reviewed(world)
    (thread,) = [row["id"] for row in world.github.inline() if row["path"] == "src/auth.py"]
    report = world.chat("@lemoncrow review thorough", thread=thread)
    assert report.published["chat"]["kind"] == "review" and state_of(world).depth == "thorough"
    reply = world.github.chat_replies()[-1]
    assert reply["in_reply_to_id"] == thread and reply["user"] == BOT
    comment = world.github.review_comments[-2]
    assert github(world).bot_reactions == [
        ("pulls", comment["id"], "eyes")
    ], "a review comment is reacted to on its own endpoint"


# --------------------------------------------------------------------------- #
# the same head reuses; a new head is reviewed afresh
# --------------------------------------------------------------------------- #


def test_reviewing_again_on_an_unchanged_head_reuses_the_carried_results_without_a_model_call(world: World) -> None:
    reviewed(world)
    before = summary_body(world)
    findings = state_of(world).findings
    spent = state_of(world).usage_total

    report = world.chat("@lemoncrow review")

    assert world.model.calls == [] and world.model.chats == [], "no model call at all"
    assert report.published["chat"]["reused"] is True and report.published["chat"]["kind"] == "review"
    assert answer(world).startswith(f"Nothing has changed since my last review of `{world.origin.head[:7]}` at Auto")
    assert "Ask for `review thorough` or `review deep`" in answer(world)
    after = state_of(world)
    assert (after.reviews, after.usage_total, after.findings) == (1, spent, findings)
    assert summary_body(world) == before, "the summary is not rewritten when nothing differs"
    assert len(world.github.inline()) == 1 and world.github.review_posts == 1, "no second set of comments"
    # And the reuse is not a billed run: a budget of one review still allows it.
    again = world.chat("@lemoncrow review", max_runs_per_pr=1)
    assert again.published["chat"]["kind"] == "budget", "...but a spent budget refuses the command before looking"


def test_a_new_head_is_reviewed_again_from_what_changed_and_the_rest_is_carried(world: World) -> None:
    world.model.cite_lines.add(AUTH_TITLE)  # a careful model names the line, so a fix can be recognised
    reviewed(world)
    world.origin.push({"src/auth.py": AUTH_FIXED}, "fix the auth bug")
    report = world.chat("@lemoncrow review")

    assert world.model.paths() == ["src/auth.py"], "only the changed file is asked again"
    assert report.published["chat"]["reused"] is False
    state = state_of(world)
    assert state.head_sha == world.origin.head and state.reviews == 2
    by_title = {finding.title: finding.state for finding in state.findings}
    assert by_title[AUTH_TITLE] == "fixed" and by_title["grows without a bound"] == "carried"
    assert "Reviewed" in answer(world)


def test_a_deeper_depth_on_an_unchanged_head_looks_again_because_the_depth_is_part_of_what_was_reviewed(
    world: World,
) -> None:
    reviewed(world)
    world.chat("@lemoncrow review deep")
    assert world.model.calls, "Deep is a different review of the same commit"
    world.model.reset()
    again = world.chat("@lemoncrow review deep")
    assert world.model.calls == [] and again.published["chat"]["reused"] is True
    assert "at Deep, so I reused" in answer(world)


# --------------------------------------------------------------------------- #
# who may ask, and what it may spend
# --------------------------------------------------------------------------- #


def test_a_stranger_is_ignored_entirely(world: World) -> None:
    reviewed(world)
    before = summary_body(world)
    report = world.chat("@lemoncrow review deep", author="mallory")  # no association: NONE
    assert report.skipped and "not allowed" in report.reason
    assert world.github.chat_replies() == [] and github(world).bot_reactions == []
    assert world.model.calls == [] and summary_body(world) == before


def test_a_bot_asking_is_ignored_even_with_an_allowed_association(world: World) -> None:
    reviewed(world)
    report = world.chat("@lemoncrow review deep", author="helper[bot]", user_type="Bot", association="MEMBER")
    assert report.skipped and world.github.chat_replies() == [] and world.model.calls == []


def test_the_allowed_associations_are_the_ones_chat_uses(world: World) -> None:
    reviewed(world)
    world.github.associations["carol"] = "CONTRIBUTOR"
    assert world.chat("@lemoncrow review deep", author="carol").published["chat"]["kind"] == "review"
    quiet = world.chat(
        "@lemoncrow review thorough", author="carol", env={"LEMONCROW_REVIEW_CHAT_ASSOCIATIONS": "OWNER,MEMBER"}
    )
    assert quiet.skipped, "chat-allowed-associations governs the command too"


def test_a_spent_budget_refuses_the_command_in_the_thread_and_calls_no_model(world: World) -> None:
    reviewed(world, max_runs_per_pr=1)
    before = summary_body(world)
    report = world.chat("@lemoncrow review deep", max_runs_per_pr=1)
    assert report.published["chat"]["kind"] == "budget" and world.model.calls == []
    text = answer(world)
    assert "Review budget reached: 1 review used of 1 allowed" in text and "no model was called" in text
    assert github(world).bot_reactions == [], "a refusal is not acknowledged as work"
    assert state_of(world).reviews == 1 and summary_body(world) == before


def test_a_command_review_counts_as_a_review_so_the_run_cap_is_reached(world: World) -> None:
    reviewed(world, max_runs_per_pr=2)
    assert world.chat("@lemoncrow review deep", max_runs_per_pr=2).published["chat"]["kind"] == "review"
    assert state_of(world).reviews == 2
    third = world.chat("@lemoncrow review thorough", max_runs_per_pr=2)
    assert third.published["chat"]["kind"] == "budget"
    assert "2 reviews used of 2 allowed" in answer(world)
    # The push after it is not reviewed either: the command used the run.
    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    assert world.review(max_runs_per_pr=2).verdict.state == "budget"  # type: ignore[union-attr]


def test_a_dollar_budget_counts_what_the_command_cost(world: World) -> None:
    # Each fake call costs $0.00027 and the first review makes three (a risk signal each in auth.py and cache.py, and
    # plain.py as changed code with no signal), so $0.0012 leaves room for exactly one more command.
    reviewed(world, max_usd_per_pr=0.0012)
    first = world.chat("@lemoncrow review deep", max_usd_per_pr=0.0012)
    assert first.published["chat"]["kind"] == "review", "the first review cost less than the cap"
    refused = world.chat("@lemoncrow review thorough", max_usd_per_pr=0.0012)
    assert refused.published["chat"]["kind"] == "budget" and "spent of $0.0012 allowed" in answer(world)


def test_a_pull_request_from_a_fork_is_reviewed_from_the_conversation_on_an_api_route(world: World) -> None:
    """The conversation comment runs with this repository's secrets; the Action reads the fork's objects and runs none."""

    reviewed(world)
    world.github.head_repo = "mallory/widgets"
    assert world.chat("@lemoncrow review deep").published["chat"]["kind"] == "review"


def test_a_review_comment_from_a_fork_never_runs(world: World) -> None:
    reviewed(world)
    (thread,) = [row["id"] for row in world.github.inline() if row["path"] == "src/auth.py"]
    report = world.chat("@lemoncrow review deep", thread=thread, fork=True)
    assert report.skipped and "fork" in report.reason
    assert world.github.chat_replies() == [] and world.model.calls == [] and github(world).bot_reactions == []


def test_a_forks_code_is_not_reviewed_on_a_route_that_runs_tools(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    reviewed(world)
    world.github.head_repo = "mallory/widgets"
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "claude")
    report = world.chat("@lemoncrow review deep")
    assert report.published["chat"]["kind"] == "refused" and world.model.calls == []
    assert "fork" in answer(world) and "agent CLI" in answer(world) and github(world).bot_reactions == []


# --------------------------------------------------------------------------- #
# what else it does and does not do
# --------------------------------------------------------------------------- #


def test_a_dry_run_reacts_to_nothing_and_posts_nothing(world: World) -> None:
    reviewed(world)
    before = summary_body(world)
    report = world.chat("@lemoncrow review deep", dry_run=True)
    assert report.published["dry_run"] is True and report.published["chat"]["kind"] == "review"
    assert github(world).bot_reactions == [] and world.github.chat_replies() == []
    assert summary_body(world) == before and "Reviewed" in report.inline_bodies[0]


def test_a_reaction_that_cannot_be_made_does_not_stop_the_review(world: World) -> None:
    reviewed(world)
    github(world).deny_reactions = True
    report = world.chat("@lemoncrow review deep")
    assert report.published["chat"]["kind"] == "review" and state_of(world).depth == "deep"
    assert any("could not react to the comment" in note for note in report.notes)


def test_the_same_comment_starts_one_review_not_two(world: World) -> None:
    reviewed(world)
    world.chat("@lemoncrow review deep")
    calls = len(world.model.calls)
    again = world.rerun_event()  # "Re-run jobs" on the same comment
    assert again.skipped and "already answered" in again.reason
    assert len(world.model.calls) == calls and state_of(world).reviews == 2


def test_a_failed_model_call_is_a_review_that_says_it_is_incomplete_not_a_green_one(world: World) -> None:
    reviewed(world)
    world.model.fail = True
    report = world.chat("@lemoncrow review deep")
    assert report.published["chat"]["kind"] == "review"
    assert state_of(world).complete is False
    assert "Review incomplete" in answer(world), "the thread is told what the summary says"


def test_the_command_is_not_a_question_so_no_chat_call_is_made(world: World) -> None:
    reviewed(world)
    world.chat("@lemoncrow review deep")
    assert world.model.chats == [] and state_of(world).chats == 0
    assert NUMBER == 7
