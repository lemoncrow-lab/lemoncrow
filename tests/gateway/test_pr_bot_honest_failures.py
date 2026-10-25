"""A bad key, an empty balance, a down provider, a token that cannot write and a killed job are all said out loud.

The review used to turn each of them into a green job whose log held the cause. Now the cause is named in the
``LemonCrow Review`` status, in the summary comment's headline and in an ``::error`` annotation, and nothing that did
not run is reported as a pass. The runs here are the pull request bot's own (a real git origin, a fake GitHub, a fake
model); the gate and the last step are exercised with their GitHub calls injected.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import ci_env, model_failure, runner
from lemoncrow.pro.capabilities.review.pr_bot.runner import announce, run_url
from tests.gateway.pr_bot_support import BASE, FIRST_PUSH, FakeModel, World, build_world

KEY = "sk-ant-api03-ThisIsTheSecretKey0123456789"


# --------------------------------------------------------------------------- #
# the cause, in one line
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "kind", "fragments"),
    [
        (
            'LiteLLMUnavailable: LiteLLM completion unavailable: litellm.AuthenticationError: AnthropicException - {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}',
            "auth",
            ("Anthropic rejected the API key (HTTP 401)", "`anthropic-api-key`"),
        ),
        (
            "OpenAIClientUnavailable: OpenAI-compatible API unavailable: Error code: 401 - {'error': {'message': 'Incorrect API key provided: sk-proj-****abcd'}}",
            "auth",
            ("rejected the API key (HTTP 401)", "`openai-api-key`"),
        ),
        ("litellm.PermissionDeniedError: Error code: 403 - not allowed", "auth", ("HTTP 403, not allowed",)),
        (
            "litellm.RateLimitError: AnthropicException - Your credit balance is too low to access the Anthropic API",
            "credit",
            ("no credit left", "Add credit"),
        ),
        ("litellm.BadRequestError: insufficient_quota: You exceeded your current quota", "credit", ("no credit left",)),
        ("litellm.RateLimitError: rate_limit_error: slow down", "rate_limit", ("rate-limited", "HTTP 429")),
        (
            "litellm.NotFoundError: model: claude-sonnet-9 not found",
            "model",
            ("does not know the model", "`model` input"),
        ),
        (
            'litellm.InternalServerError: {"type": "overloaded_error"}',
            "overloaded",
            ("unavailable or overloaded", "re-run"),
        ),
        ("litellm.APIConnectionError: connection reset", "overloaded", ("unavailable or overloaded",)),
        (
            "LiteLLMUnavailable: LiteLLM returned invalid JSON, also after one retry: Expecting value (char 403)",
            "json",
            ("did not answer in the format",),
        ),
        ("RuntimeError: provider unavailable", "other", ("model calls failed", "see the workflow log")),
    ],
)
def test_each_failure_is_named_with_its_cause_and_its_fix(error: str, kind: str, fragments: tuple[str, ...]) -> None:
    provider = "openai" if "OpenAI" in error else "anthropic"
    failure = model_failure.classify([error], provider=provider)
    assert failure is not None and failure.kind == kind
    for fragment in fragments:
        assert fragment in failure.annotation + failure.headline, fragment
    assert "\n" not in failure.headline and len(f"Review incomplete: {failure.headline}") <= 140
    assert not any(
        secret in failure.annotation for secret in ("sk-proj", "abcd")
    ), "the provider's text is never repeated"


def test_a_status_code_in_a_number_is_not_mistaken_for_one() -> None:
    """A JSON parse error at ``char 403`` or a count of 429 findings is no HTTP status."""

    failure = model_failure.classify(["LiteLLM returned invalid JSON: Expecting ',' delimiter: line 9 (char 429)"])
    assert failure is not None and failure.kind == "json"


def test_nothing_failed_means_nothing_to_say() -> None:
    assert model_failure.classify([]) is None and model_failure.classify(["  "]) is None


def test_a_key_is_scrubbed_from_the_one_raw_excerpt_the_log_keeps() -> None:
    secrets = model_failure.key_values(
        {"ANTHROPIC_API_KEY": KEY, "OPENAI_API_KEY": "short", "GH_TOKEN": "ghs_abcdefghijkl"}
    )
    assert KEY in secrets and "short" not in secrets
    text = model_failure.first_error(
        [f"AuthenticationError: bad key {KEY} and sk-other-ABCDEFGH12345 ghp_abcdefghijklmnop"], secrets
    )
    assert KEY not in text and "sk-other" not in text and "ghp_" not in text and text.count("***") == 3


# --------------------------------------------------------------------------- #
# a failed review, as a pull request shows it
# --------------------------------------------------------------------------- #


class RejectedKey(FakeModel):
    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append({"path": "(call)", "targets": [], "category": ""})
        raise RuntimeError("litellm.AuthenticationError: AnthropicException - invalid x-api-key")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, BASE)


def test_a_rejected_key_sets_the_check_to_error_names_the_cause_and_leaves_the_job_green(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.model = RejectedKey()
    world.origin.push(FIRST_PUSH)
    report = world.review()

    headline = "Review incomplete: OpenAI rejected the API key (HTTP 401); check the secret passed as `openai-api-key`"
    assert report.status == ("error", headline[:140]) and report.verdict is not None and report.verdict.failed
    assert f"**{headline}**" in world.github.summary_comments()[0]["body"], "one plain line in the comment"
    assert report.exit_code == 0, "fail-on is none: the check and the annotation are the signal, the job stays green"
    ((level, message),) = report.annotations
    assert level == "error" and "Check the secret passed as `openai-api-key`" in message
    out = capsys.readouterr().out
    assert out.startswith("::error title=LemonCrow Review::OpenAI rejected the API key (HTTP 401).")
    assert "test-key" not in out + json.dumps(world.github.issue_comments) + json.dumps(world.github.statuses)


def test_with_fail_on_p0_the_same_failure_fails_the_job_too(world: World) -> None:
    world.model = RejectedKey()
    world.origin.push(FIRST_PUSH)
    report = world.review(fail_on="p0")
    assert report.status is not None and report.status[0] == "error" and report.exit_code == 1


def test_a_clean_review_and_a_partial_one_stay_green_and_exit_zero(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    report = world.review(fail_on="p0")
    assert report.exit_code == 0 and report.annotations == []


def test_an_open_p0_under_fail_on_p0_fails_the_job_with_the_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.gateway.pr_bot_support import AUTH_BUGGY, RULE

    ruled = build_world(tmp_path, monkeypatch, {**BASE, ".lemoncrow/review.yaml": RULE})
    ruled.origin.push({"src/auth.py": AUTH_BUGGY})
    advisory = ruled.review()
    assert advisory.exit_code == 0 and advisory.status is not None and advisory.status[0] == "success"
    gating = ruled.review(fail_on="p0")
    assert gating.status is not None and gating.status[0] == "failure" and gating.exit_code == 1


def test_the_pending_and_error_statuses_link_to_the_run(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    monkeypatch.setenv("GITHUB_RUN_ID", "99")
    world.origin.push(FIRST_PUSH)
    world.github.deny_comments = True
    with pytest.raises(runner.PrPermissionError):
        world.review()
    pending, error = world.github.statuses
    assert pending["target_url"] == error["target_url"] == "https://github.com/acme/widgets/actions/runs/99"
    assert run_url({}) == "" and run_url({"GITHUB_SERVER_URL": "https://github.com"}) == ""


# --------------------------------------------------------------------------- #
# annotations
# --------------------------------------------------------------------------- #


def test_an_annotation_is_a_workflow_command_only_on_a_runner_and_never_carries_a_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    announce("error", "no", {})
    assert capsys.readouterr().out == ""
    env = {"GITHUB_ACTIONS": "true", "ANTHROPIC_API_KEY": KEY}
    announce("error", f"line one\nline two 100% {KEY}", env)
    announce("surprise", "level is normalized", env)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "::error title=LemonCrow Review::line one line two 100%25 ***"
    assert out[1].startswith("::notice title=LemonCrow Review::")
    assert KEY not in "".join(out)


# --------------------------------------------------------------------------- #
# the key input
# --------------------------------------------------------------------------- #


def test_the_key_inputs_become_the_standard_variables_and_win_over_them() -> None:
    merged = ci_env.key_environment(
        {
            "INPUT_ANTHROPIC_API_KEY": f" {KEY} ",
            "INPUT_OPENAI_API_KEY": "",
            "ANTHROPIC_API_KEY": "old",
            "OPENAI_API_KEY": "env",
        }
    )
    assert (
        merged["ANTHROPIC_API_KEY"] == KEY and merged["OPENAI_API_KEY"] == "env"
    ), "an empty input leaves the variable"
    route = ci_env.resolve_model_route(ci_env.key_environment({"INPUT_OPENAI_API_KEY": "sk-openai-123456"}))
    assert route is not None and route.provider == "openai"
    assert ci_env.resolve_model_route({}) is None


def test_every_key_is_masked_once_and_a_placeholder_is_not() -> None:
    commands = ci_env.mask_commands(
        {
            "INPUT_ANTHROPIC_API_KEY": KEY,
            "ANTHROPIC_API_KEY": KEY,
            "OPENAI_API_KEY": "line1-secret\nline2-secret",
            "X": "k",
        }
    )
    assert commands == [f"::add-mask::{KEY}", "::add-mask::line1-secret", "::add-mask::line2-secret"]
    assert ci_env.mask_commands({"OPENAI_API_KEY": "k"}) == []


def test_the_gate_masks_the_key_first_and_never_prints_it_again(tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "opened", "pull_request": {"number": 7, "draft": True}}))
    env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_OUTPUT": str(tmp_path / "out"),
        "INPUT_ANTHROPIC_API_KEY": KEY,
        "LEMONCROW_REVIEW_DRAFTS": "false",
    }
    done = subprocess.run(
        [sys.executable, "-I", "-S", ci_env.__file__], env=env, capture_output=True, text=True, check=False
    )
    lines = done.stdout.splitlines()
    assert lines[0] == f"::add-mask::{KEY}" and KEY not in "\n".join(lines[1:]) + done.stderr


def test_no_key_is_a_red_job_with_both_ways_to_pass_one_and_an_error_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "action": "opened",
                "pull_request": {
                    "number": 7,
                    "head": {"sha": "a" * 40, "repo": {"full_name": "acme/w"}},
                    "base": {"repo": {"full_name": "acme/w"}},
                },
            }
        )
    )
    for name in (
        "INPUT_ANTHROPIC_API_KEY",
        "INPUT_OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LEMONCROW_LLM_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_ACTOR", "alice")
    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ci_env, "post_status", lambda env, state, description, **kw: posted.append((state, description)) or ""
    )
    assert ci_env.main([]) == 1
    out = capsys.readouterr().out
    assert out.startswith("::error title=LemonCrow Review::No model key. ")
    assert (
        "anthropic-api-key" in out and "openai-api-key" in out and "environment variable on the step also works" in out
    )
    assert posted == [("error", "No model key: pass anthropic-api-key or openai-api-key to the LemonCrow Action")]


def test_no_key_on_a_comment_posts_no_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = ci_env.decide_chat(
        {"GITHUB_EVENT_NAME": "issue_comment"},
        event={
            "action": "created",
            "issue": {"number": 7, "pull_request": {"url": "x"}},
            "comment": {
                "id": 1,
                "body": "@lemoncrow help",
                "user": {"login": "bob", "type": "User"},
                "author_association": "MEMBER",
            },
        },
    )
    assert not gate.run and gate.exit_code == 1 and gate.error_status == ""


# --------------------------------------------------------------------------- #
# the Action's last step
# --------------------------------------------------------------------------- #

HEAD = "c" * 40


def settle_env(**extra: str) -> dict[str, str]:
    return {
        "GH_TOKEN": "ghs_token",
        "GITHUB_REPOSITORY": "acme/widgets",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_API_URL": "https://api.example.invalid",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "42",
        **extra,
    }


EVENT = {"pull_request": {"head": {"sha": HEAD}}}


def settle(
    env: Mapping[str, str], statuses: object, *, event: Mapping[str, object] = EVENT
) -> tuple[str, list[dict[str, Any]], list[str]]:
    asked: list[str] = []
    sent: list[dict[str, Any]] = []

    def fetch(url: str, headers: Mapping[str, str]) -> object:
        asked.append(url)
        if isinstance(statuses, Exception):
            raise statuses
        if "/pulls/" in url:
            return {"head": {"sha": HEAD}}
        return statuses

    def send(url: str, body: bytes, headers: Mapping[str, str]) -> None:
        sent.append({"url": url, **json.loads(body)})

    return ci_env.settle_pending_status(env, event=event, fetch=fetch, send=send), sent, asked


def test_a_review_left_pending_by_a_killed_job_becomes_an_error_with_a_link_to_the_run() -> None:
    note, sent, asked = settle(
        settle_env(), [{"context": "LemonCrow Review", "state": "pending"}, {"context": "ci", "state": "success"}]
    )
    assert [s["state"] for s in sent] == ["error"] and sent[0]["description"] == ci_env.UNFINISHED
    assert sent[0]["context"] == "LemonCrow Review"
    assert sent[0]["target_url"] == "https://github.com/acme/widgets/actions/runs/42"
    assert (
        sent[0]["url"] == f"https://api.example.invalid/repos/acme/widgets/statuses/{HEAD}" and "left pending" in note
    )
    assert asked == [f"https://api.example.invalid/repos/acme/widgets/commits/{HEAD}/statuses?per_page=100"]


@pytest.mark.parametrize(
    "statuses",
    [
        [
            {"context": "LemonCrow Review", "state": "success"},
            {"context": "LemonCrow Review", "state": "pending"},
        ],  # newest first
        [{"context": "LemonCrow Review", "state": "failure"}],
        [{"context": "LemonCrow Review", "state": "error"}],
        [{"context": "other", "state": "pending"}],
        [],
    ],
)
def test_only_a_newest_status_that_is_still_pending_is_touched(statuses: list[dict[str, str]]) -> None:
    note, sent, _ = settle(settle_env(), statuses)
    assert sent == [] and note == ""


def test_a_comment_event_sets_no_status_so_there_is_nothing_to_settle() -> None:
    note, sent, asked = settle(
        settle_env(GITHUB_EVENT_NAME="issue_comment"), [{"context": "LemonCrow Review", "state": "pending"}]
    )
    assert (note, sent, asked) == ("", [], [])


def test_a_dispatched_run_finds_its_head_through_the_pull_request() -> None:
    _, sent, asked = settle(
        settle_env(GITHUB_EVENT_NAME="workflow_dispatch", LEMONCROW_REVIEW_PR="7"),
        [{"context": "LemonCrow Review", "state": "pending"}],
        event={"inputs": {"pr": "7"}},
    )
    assert asked[0].endswith("/repos/acme/widgets/pulls/7") and [s["state"] for s in sent] == ["error"]


def error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.example.invalid", code, "x", {}, None)  # type: ignore[arg-type]


def test_the_last_step_never_fails_the_job_and_stays_quiet_when_the_token_cannot_read_statuses() -> None:
    assert settle(settle_env(), error(403))[0] == "", "the run already said the token lacks a permission"
    assert settle(settle_env(), error(500))[0].endswith("(HTTP 500).")
    assert "OSError" in settle(settle_env(), OSError("down"))[0]
    assert settle(settle_env(GH_TOKEN=""), [])[0] == ""


def test_the_last_step_runs_as_the_script_the_action_names_and_exits_zero(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "GITHUB_EVENT_NAME": "issue_comment"}
    done = subprocess.run(
        [sys.executable, "-I", "-S", ci_env.__file__, "settle"], env=env, capture_output=True, text=True
    )
    assert done.returncode == 0 and done.stdout == ""
