"""Reactions as the review's status: eyes while it runs, a thumbs up when it is done, confused when it could not run.

Best effort and never a failure: a token that may not react (the sample workflow's cannot react on the pull request or
on a conversation comment, which GitHub lists under ``issues: write``) costs the review nothing and says nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import reactions
from lemoncrow.pro.capabilities.review.pr_bot.github import BotGitHub, ReactionTarget
from lemoncrow.pro.capabilities.review.pr_bot.runner import RunReport
from lemoncrow.pro.capabilities.review.sources.github import PullRequestRef
from tests.gateway.pr_bot_support import BASE, FIRST_PUSH, NUMBER, RULE, SLUG, World, build_world


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, BASE)


def on_pull(world: World) -> list[str]:
    return world.github.bot_reactions("issues", NUMBER)


def log(world: World) -> list[tuple[str, str]]:
    """``(add | remove, content)`` of what the bot did on the pull request itself."""

    return [(op, content) for op, family, target, content in world.github.reaction_log if target == NUMBER]


class Peek:
    """A model that notes which reactions were on the pull request when it was first asked something."""

    def __init__(self, inner: Any, github: Any) -> None:
        self.inner, self.github = inner, github
        self.seen: list[list[str]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.seen.append(self.github.bot_reactions("issues", NUMBER))
        return self.inner(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


# --------------------------------------------------------------------------- #
# the automatic run: on the pull request itself
# --------------------------------------------------------------------------- #


def test_a_review_puts_eyes_on_the_pull_request_before_the_model_is_asked_and_leaves_a_thumbs_up(world: World) -> None:
    world.model = Peek(world.model, world.github)  # type: ignore[assignment]
    world.origin.push(FIRST_PUSH)
    report = world.review()
    assert world.model.seen and all(seen == ["eyes"] for seen in world.model.seen), "eyes were on before any model call"
    assert log(world) == [("add", "eyes"), ("add", "+1"), ("remove", "eyes")]
    assert on_pull(world) == ["+1"] and report.published["reaction"] == "thumbs up"
    assert report.notes == [] and not report.skipped


def test_a_review_that_found_a_p0_still_ran_so_it_is_a_thumbs_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruled = build_world(tmp_path, monkeypatch, {**BASE, ".lemoncrow/review.yaml": RULE})
    ruled.origin.push(FIRST_PUSH)
    report = ruled.review(fail_on="p0")
    assert report.verdict is not None and report.verdict.state == "must_fix" and report.exit_code == 1
    assert on_pull(ruled) == ["+1"], "what it found is not whether it ran"


def test_a_review_that_could_not_finish_is_a_confused_face(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.model.fail = True
    report = world.review()
    assert report.verdict is not None and report.verdict.state == "incomplete"
    assert on_pull(world) == ["confused"] and report.published["reaction"] == "confused"
    assert log(world) == [("add", "eyes"), ("add", "confused"), ("remove", "eyes")]


def test_a_review_that_failed_with_an_error_is_a_confused_face_and_the_error_still_surfaces(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.pr_bot import engine

    world.origin.push(FIRST_PUSH)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise engine.AiStageError("the Snapshot has no persisted effective run plan")

    monkeypatch.setattr(engine, "run_ai_stage", broken)
    with pytest.raises(Exception, match="persisted effective run plan"):
        world.review()
    assert on_pull(world) == ["confused"]


def test_a_spent_budget_is_a_confused_face_because_nothing_new_was_reviewed(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review(max_runs_per_pr=1)
    assert on_pull(world) == ["+1"]
    world.origin.push({"src/plain.py": "VALUE = 9\n"})
    stopped = world.review(max_runs_per_pr=1)
    assert stopped.verdict is not None and stopped.verdict.state == "budget"
    assert on_pull(world) == ["confused"], "the earlier thumbs up is not left beside it"


def test_the_outcome_of_the_last_run_is_the_only_one_left_on_the_pull_request(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    assert on_pull(world) == ["+1"]
    world.origin.push(
        {"src/auth.py": "def authorize(user):\n    return bool(user)\n"}
    )  # a risk signal: the model is asked
    world.model.fail = True
    world.review()
    assert on_pull(world) == ["confused"]
    world.model.fail = False
    world.origin.push({"src/plain.py": "VALUE = 4\n"})
    world.review()
    assert on_pull(world) == ["+1"]


def test_a_skipped_run_reacts_to_nothing(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.github.draft = True
    assert world.review().skipped
    world.github.draft = False
    world.github.state = "closed"
    assert world.review().skipped
    assert world.github.reaction_log == []


def test_a_dry_run_reacts_to_nothing(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    report = world.review(dry_run=True)
    assert report.published.get("dry_run") and world.github.reaction_log == []


# --------------------------------------------------------------------------- #
# best effort: a refusal never fails the review
# --------------------------------------------------------------------------- #


def test_a_token_that_may_not_react_on_issues_costs_the_review_nothing_and_says_nothing(world: World) -> None:
    """The sample workflow's ``pull-requests: write`` cannot react on the pull request: GitHub lists that under ``issues``."""

    world.origin.push(FIRST_PUSH)
    world.github.deny_reactions = {"issues"}
    report = world.review(fail_on="p0")
    assert report.exit_code == 0 and report.annotations == [] and not report.notes.denied
    assert report.notes == [], "an expected refusal is not a line in the job log"
    assert report.published["reaction"] == "not permitted"
    assert world.github.last_status()["state"] == "success" and report.verdict is not None
    assert len(world.github.summary_comments()) == 1 and len(world.github.inline()) == 1
    assert world.github.reaction_log == []
    # It does not keep trying: the eyes were refused, so the thumbs up was not even attempted.
    assert [call for call in world.github.calls if call[1].endswith("/reactions")] == [
        ("POST", f"repos/{SLUG}/issues/{NUMBER}/reactions")
    ]


def test_a_failure_that_is_not_a_refusal_is_a_note_and_still_not_a_failure(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.github.break_reactions = True
    report = world.review()
    assert not report.skipped and report.exit_code == 0 and report.annotations == []
    assert any(note.startswith("could not react:") and "502" in note for note in report.notes)
    assert world.github.last_status()["state"] == "success"


def test_the_json_result_says_what_was_left_on_the_pull_request(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    report = world.review()
    assert report.to_wire()["published"]["reaction"] == "thumbs up"
    json.dumps(report.to_wire())


# --------------------------------------------------------------------------- #
# which endpoint, and so which permission
# --------------------------------------------------------------------------- #


class Recording:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Any, stdin: str | None = None) -> str:
        self.calls.append([str(item) for item in args])
        if "--paginate" in args:
            return json.dumps([[{"id": 5, "user": {"login": "me"}}, {"id": 6, "user": {"login": "someone"}}]])
        return json.dumps({"id": 9, "content": "eyes", "user": {"login": "me"}}) if "POST" in args else ""


def bot() -> tuple[BotGitHub, Recording]:
    recording = Recording()
    return BotGitHub(PullRequestRef("acme", "widgets", NUMBER), recording), recording


@pytest.mark.parametrize(
    ("target", "path"),
    [
        (ReactionTarget("pull"), f"repos/{SLUG}/issues/{NUMBER}/reactions"),
        (ReactionTarget("issue_comment", 41), f"repos/{SLUG}/issues/comments/41/reactions"),
        (ReactionTarget("review_comment", 42), f"repos/{SLUG}/pulls/comments/42/reactions"),
    ],
)
def test_each_target_has_its_own_rest_endpoint(target: ReactionTarget, path: str) -> None:
    github, recording = bot()
    assert github.add_reaction(target, "eyes") == (9, "me")
    assert recording.calls[-1][:4] == ["api", "--method", "POST", path] and "--input" in recording.calls[-1]
    github.remove_reaction(target, 9)
    assert recording.calls[-1] == ["api", "--method", "DELETE", f"{path}/9"]
    assert github.reactions_by(target, "+1", "me") == [5]
    assert recording.calls[-1][-1] == f"{path}?content=%2B1&per_page=100", "the plus of +1 is encoded"


def test_only_the_review_thread_endpoint_rides_on_pull_requests_write() -> None:
    """GitHub's REST reference lists a review-comment reaction under ``pull-requests`` and the other two under ``issues``."""

    text = (reactions.__doc__ or "").replace("\n", " ")
    assert "pulls/comments/{id}" in text and "pull-requests: write" in text
    assert "issues/comments/{id}" in text and "issues/{number}" in text and "issues: write" in text


def test_a_reaction_target_is_one_of_three_kinds_and_a_comment_has_an_id() -> None:
    with pytest.raises(ValueError):
        ReactionTarget("commit")
    with pytest.raises(ValueError):
        ReactionTarget("issue_comment")
    assert ReactionTarget("pull").id == 0


def test_the_signal_knows_what_ran_by_the_verdict() -> None:
    class Done:
        skipped = False

        def __init__(self, state: str | None) -> None:
            self.verdict = None if state is None else type("V", (), {"state": state})()

    assert [reactions.ran(Done(state)) for state in ("must_fix", "partial", "clean")] == [True, True, True]
    assert [reactions.ran(Done(state)) for state in ("incomplete", "budget", None)] == [False, False, False]
    skipped = RunReport(pr="x", skipped=True)
    assert reactions.ran(skipped) is False
