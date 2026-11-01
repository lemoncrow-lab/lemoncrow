"""Every way the Action ends leaves a ``LemonCrow Review`` status that names why, so a required check never waits.

The gate's exits (fork, draft, no key, an event it does not review, a run that goes ahead) are driven through the gate
script's ``main`` with the status call captured; the review's exits (a rejected key, no credit, the budget, a broken
``.lemoncrow/review.yaml``, a refusal before the review started, a crash) run the pull request bot against the fake
GitHub; the last step's wording is read for each step that can die.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import ci_env, runner
from tests.gateway.pr_bot_support import BASE, FIRST_PUSH, SLUG, FakeModel, World, build_world

HEAD = "a" * 40


def pull_event(**pull: Any) -> dict[str, Any]:
    return {
        "action": "opened",
        "pull_request": {
            "number": 7,
            "head": {"sha": HEAD, "repo": {"full_name": SLUG}},
            "base": {"repo": {"full_name": SLUG}},
            **pull,
        },
    }


def gate_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, event_name: str, event: dict[str, Any], **env: str
) -> tuple[int, list[tuple[str, str, bool, str]], str]:
    """Run the gate script's entry point; return its exit code, the statuses it posted and its ``$GITHUB_OUTPUT``."""

    for name in ("INPUT_ANTHROPIC_API_KEY", "INPUT_OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("LEMONCROW_LLM_BACKEND", "LEMONCROW_REVIEW_PR", "GITHUB_SHA", "GITHUB_ACTOR"):
        monkeypatch.delenv(name, raising=False)
    path, output = tmp_path / "event.json", tmp_path / "output"
    path.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    posted: list[tuple[str, str, bool, str]] = []

    def capture(
        _env: object, state: str, description: str, *, writable: bool = True, event: Any = None, **_: Any
    ) -> str:
        head = str(((event or {}).get("pull_request") or {}).get("head", {}).get("sha") or "") if event else HEAD
        posted.append((state, description, writable, head))
        return ""

    monkeypatch.setattr(ci_env, "post_status", capture)
    code = ci_env.main([])
    return code, posted, output.read_text(encoding="utf-8") if output.exists() else ""


FORK = {"head": {"sha": HEAD, "repo": {"full_name": "mallory/widgets"}}}


@pytest.mark.parametrize(
    ("name", "event_name", "event", "env", "code", "expected"),
    [
        (
            "fork",
            "pull_request",
            pull_event(**FORK),
            {"OPENAI_API_KEY": "sk-x"},
            0,
            ("success", "skipped: pull request from a fork", False),
        ),
        (
            "draft",
            "pull_request",
            pull_event(draft=True),
            {"OPENAI_API_KEY": "sk-x"},
            0,
            ("success", "skipped: draft pull request", True),
        ),
        ("no key", "pull_request", pull_event(), {}, 1, ("error", ci_env.NO_KEY_STATUS, True)),
        (
            "pull_request_target",
            "pull_request_target",
            pull_event(),
            {"OPENAI_API_KEY": "sk-x"},
            1,
            ("error", "Unsupported event pull_request_target: use the pull_request event", True),
        ),
        ("runs", "pull_request", pull_event(), {"OPENAI_API_KEY": "sk-x"}, 0, ("pending", ci_env.STARTING, True)),
    ],
)
def test_every_gate_exit_posts_a_status_naming_why(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    event_name: str,
    event: dict[str, Any],
    env: dict[str, str],
    code: int,
    expected: tuple[str, str, bool],
) -> None:
    exit_code, posted, _ = gate_run(monkeypatch, tmp_path, event_name, event, **env)
    assert exit_code == code, name
    assert [row[:3] for row in posted] == [expected], name


def test_the_missing_key_status_names_the_secret_the_sample_workflow_reads() -> None:
    sample = (Path(ci_env.__file__).parent / "lemoncrow-review.workflow.yml").read_text(encoding="utf-8")
    assert "secrets.ANTHROPIC_API_KEY" in sample and "ANTHROPIC_API_KEY" in ci_env.NO_KEY_STATUS
    assert len(ci_env.NO_KEY_STATUS) <= 140


def test_an_event_the_action_does_not_review_says_so_on_the_commit_and_keeps_the_job_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, posted, output = gate_run(
        monkeypatch, tmp_path, "push", {"ref": "refs/heads/main"}, OPENAI_API_KEY="sk-x", GITHUB_SHA="b" * 40
    )
    assert code == 0 and output == "run=false\nextra=\n"
    assert posted == [("error", "Unsupported event push: LemonCrow Review runs on pull_request", True, "b" * 40)]


def test_a_finished_ci_run_refreshes_the_proof_of_work_and_posts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, posted, output = gate_run(monkeypatch, tmp_path, "workflow_run", {"action": "completed"})
    assert code == 0 and posted == [] and output == "run=false\nextra=\nrefresh=true\n"


@pytest.mark.parametrize(
    ("outcomes", "words"),
    [
        ({"LEMONCROW_OUTCOME_CHECKOUT": "failure"}, "Could not check out the pull request"),
        ({"LEMONCROW_OUTCOME_CHECKOUT": "success", "LEMONCROW_OUTCOME_INSTALL": "failure"}, "Could not install"),
        ({"LEMONCROW_OUTCOME_INSTALL": "success", "LEMONCROW_OUTCOME_REVIEW": "failure"}, "The review crashed"),
        ({"LEMONCROW_OUTCOME_REVIEW": "cancelled"}, "cancelled or timed out"),
        ({}, ci_env.UNFINISHED),
    ],
)
def test_the_last_step_names_the_step_that_died(outcomes: dict[str, str], words: str) -> None:
    reason = ci_env.unfinished_reason(outcomes)
    assert words in reason and len(reason) <= 140


# --------------------------------------------------------------------------- #
# the review's own exits, against the fake GitHub
# --------------------------------------------------------------------------- #


class Failing(FakeModel):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append({"path": "(call)", "targets": [], "category": ""})
        raise RuntimeError(self.message)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("GITHUB_REPOSITORY", SLUG)
    return build_world(tmp_path, monkeypatch, BASE)


def last_status(world: World) -> tuple[str, str]:
    row = world.github.statuses[-1]
    assert row["sha"] == world.origin.head
    return row["state"], row["description"]


def test_a_present_key_ends_in_a_summary_and_a_status(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    report = world.review()
    assert world.github.summary_comments() and report.status is not None
    assert last_status(world) == report.status and report.status[0] == "success"


def test_a_missing_key_in_the_review_step_is_an_error_status_naming_the_secret(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    world.origin.push(FIRST_PUSH)
    with pytest.raises(runner.PrReviewError):
        world.review()
    assert last_status(world) == ("error", ci_env.NO_KEY_STATUS)
    assert world.model.calls == [] and not world.github.summary_comments()


@pytest.mark.parametrize(
    ("message", "words"),
    [
        ("litellm.AuthenticationError: AnthropicException - invalid x-api-key", "rejected the API key"),
        ("litellm.RateLimitError: Your credit balance is too low to access the Anthropic API", "no credit left"),
    ],
)
def test_a_rejected_key_or_an_empty_balance_is_an_error_status_naming_it(
    world: World, message: str, words: str
) -> None:
    world.model = Failing(message)
    world.origin.push(FIRST_PUSH)
    world.review()
    state, description = last_status(world)
    assert state == "error" and words in description and "cost not reported" not in description


def test_a_failed_call_does_not_make_the_route_look_unpriced(world: World) -> None:
    world.model = Failing("litellm.AuthenticationError: invalid x-api-key")
    world.origin.push(FIRST_PUSH)
    report = world.review()
    assert report.state is not None and report.state.spent.cost_usd == 0.0
    assert "cost not reported" not in world.github.summary_comments()[0]["body"]


def test_a_spent_budget_is_a_status_naming_it(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review(max_runs_per_pr=1)
    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    world.review(max_runs_per_pr=1)
    state, description = last_status(world)
    assert state == "success" and description.startswith("Review budget reached: 1 review used of 1 allowed")


def test_a_review_yaml_that_does_not_parse_is_a_config_error_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = build_world(tmp_path, monkeypatch, {**BASE, ".lemoncrow/review.yaml": "review: [\n  bad"})
    broken.origin.push(FIRST_PUSH)
    report = broken.review()
    state, description = last_status(broken)
    assert state == "error" and "config error in .lemoncrow/review.yaml: it is not valid YAML (line 2)" in description
    assert report.annotations and report.annotations[0][0] == "error"


def test_a_refusal_before_the_review_started_still_posts_its_reason(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    with pytest.raises(runner.PrReviewError):
        world.review(fail_on="p9")
    state, description = last_status(world)
    assert state == "error" and description.startswith("Review not run: fail-on must be one of")
    assert len(world.github.statuses) == 1, "one status, posted once"


def test_a_crash_is_an_error_status_naming_the_exception(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyError("surprise")

    monkeypatch.setattr(runner, "_review", boom)
    world.origin.push(FIRST_PUSH)
    with pytest.raises(KeyError):
        world.review()
    assert last_status(world) == ("error", "Review crashed (KeyError); see the workflow log, then re-run.")
