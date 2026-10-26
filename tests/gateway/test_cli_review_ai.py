"""`lc review --pr N --ai [--publish-bot]`: the flags, the output and the exit codes a CI job depends on."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review import intelligence_runtime
from lemoncrow.pro.capabilities.review.pr_bot import runner as bot_runner
from tests.gateway.pr_bot_support import NUMBER, SLUG, FakeGitHub, FakeModel, Origin

BASE = {
    "src/auth.py": "def authorize(user):\n    return user.is_admin\n",
    "src/cache.py": "CACHE = {}\n\n\ndef remember(key, value):\n    CACHE[key] = value\n",
}
PUSH = {
    "src/auth.py": "def authorize(user):\n    if user is None:  # BUG: lets a missing principal through\n        return True\n    return user.is_admin\n",
}


@pytest.fixture
def ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Origin, FakeGitHub, FakeModel]:
    for name in (
        "LEMONCROW_LLM_BACKEND",
        "LEMONCROW_OPENAI_MODEL",
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
        "GITHUB_ACTOR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    origin = Origin(tmp_path)
    origin.seed_main(BASE)
    origin.push(PUSH)
    github, model = FakeGitHub(origin), FakeModel()
    monkeypatch.setattr(bot_runner, "run_gh", lambda args, stdin=None, cwd=None: github(args, stdin))
    monkeypatch.setattr(intelligence_runtime, "execute_structured", model)
    return origin, github, model


def invoke(origin: Origin, *args: str) -> Any:
    return CliRunner().invoke(
        cli, ["--root", str(origin.clone.parent / "store"), "review", "--repo-root", str(origin.clone), *args]
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--publish-bot"], "--publish-bot posts to a pull request; add --pr NUMBER|URL"),
        (["--pr", "7", "--publish", "--publish-bot"], "Use one of them"),
        (["--pr", "7", "--ai", "--mark", "src/a.py"], "drop --mark"),
        (["--pr", "7", "--ai", "--list-reviews"], "drop --current/--resume-author/--list-reviews"),
        (["--pr", "7", "--ai", "--base", "main"], "--pr picks the range itself"),
        (["--pr", "7", "--dry-run"], "use it with --pr NUMBER|URL and --ai, --publish-bot or --publish"),
        (["--pr", "7", "--ai", "--fail-on", "p1"], "--fail-on p1 is for a local --ai review"),
        (["--pr", "7", "--ai", "--fail-on", "p2"], "is not one of"),
        (["--pr", "7", "--ai", "--strict"], "--strict and --fresh apply to a local --ai review"),
        (["--pr", "7", "--ai", "--depth", "huge"], "is not one of"),
    ],
)
def test_flag_combinations_are_checked(ci: Any, args: list[str], message: str) -> None:
    result = invoke(ci[0], *args)
    assert result.exit_code != 0
    assert message in result.output


def test_publishing_as_a_bot_is_refused_outside_github_actions(ci: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    result = invoke(ci[0], "--pr", "7", "--publish-bot")
    assert result.exit_code != 0 and "runs only in GitHub Actions" in result.output
    assert ci[1].calls == [] and ci[2].calls == []


def test_a_local_ai_review_prints_the_findings_and_posts_nothing(ci: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    result = invoke(ci[0], "--pr", "7", "--ai")
    assert result.exit_code == 0, result.output
    assert "**No must-fix findings** · 1 should-fix" in result.stdout
    assert "lets a missing principal through" in result.stdout and "lemoncrow:state" not in result.stdout
    assert ci[1].statuses == [] and ci[1].issue_comments == []


def test_ai_alone_prints_the_inline_comments_too_not_only_the_summary(ci: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--pr N --ai` is a preview of what the bot would post: the summary and each inline comment with its place."""

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    out = invoke(ci[0], "--pr", "7", "--ai").stdout
    assert "Dry run: would post 1 inline comment(s) and the summary above. Nothing was posted." in out
    assert "--- inline comment on src/auth.py:" in out


def _writes(github: Any) -> list[tuple[str, str]]:
    return [call for call in github.calls if call[0] != "GET"]


def test_ai_with_dry_run_is_the_obvious_preview_and_writes_nothing(ci: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    result = invoke(ci[0], "--pr", "7", "--ai", "--dry-run")
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "**No must-fix findings** · 1 should-fix" in out and "lemoncrow:state" not in out
    assert "Dry run: would post 1 inline comment(s) and the summary above. Nothing was posted." in out
    assert "--- inline comment on src/auth.py:" in out
    assert _writes(ci[1]) == [] and ci[1].statuses == [] and ci[1].issue_comments == []


def test_the_three_forms_of_a_pull_request_preview_print_the_same_thing(
    ci: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    forms = (["--ai"], ["--ai", "--dry-run"], ["--publish-bot", "--dry-run"])
    outputs = [invoke(ci[0], "--pr", "7", *form).stdout for form in forms]
    assert outputs[0] == outputs[1] == outputs[2] and "--- inline comment on" in outputs[0]
    assert _writes(ci[1]) == [], "none of the three wrote to GitHub"
    wires = [json.loads(invoke(ci[0], "--pr", "7", *form, "--json").stdout) for form in forms]
    assert all(wire["dry_run"] is True and wire["comments"] == wires[0]["comments"] for wire in wires)


def test_ai_in_actions_still_posts_nothing_and_only_publish_bot_writes(
    ci: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert invoke(ci[0], "--pr", "7", "--ai").exit_code == 0
    assert _writes(ci[1]) == [] and ci[1].statuses == [] and ci[1].issue_comments == []
    assert invoke(ci[0], "--pr", "7", "--publish-bot").exit_code == 0
    assert _writes(ci[1]) != [] and ci[1].issue_comments, "--publish-bot is the one form that writes"


def test_the_installed_engine_command_takes_dry_run_with_a_pull_request(
    ci: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.local_cli import main as engine

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    repo = ["--repo-root", str(ci[0].clone)]
    plain = CliRunner().invoke(engine.review_cmd, [*repo, "--pr", "7"])
    dry = CliRunner().invoke(engine.review_cmd, [*repo, "--pr", "7", "--dry-run"])
    assert plain.exit_code == 0 and dry.exit_code == 0, (plain.output, dry.output)
    assert plain.stdout == dry.stdout and "--- inline comment on src/auth.py:" in dry.stdout
    assert _writes(ci[1]) == []
    refused = CliRunner().invoke(engine.review_cmd, [*repo, "--dry-run"])
    assert refused.exit_code == 3 and "add --pr NUMBER|URL to preview a pull request" in refused.output


def test_json_output_is_the_machine_readable_result(ci: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    result = invoke(ci[0], "--pr", "7", "--publish-bot", "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    wire = json.loads(result.stdout)
    assert wire["schema"] == 1 and wire["dry_run"] is True and wire["skipped"] is False
    assert wire["pull_request"] == f"{SLUG}#{NUMBER}" and wire["head_sha"] == ci[0].head
    assert wire["verdict"] == {
        "state": "clean",
        "headline": "No must-fix findings",
        "p0": 0,
        "p1": 1,
        "p2": 0,
        "confidence": 3,
    }
    assert wire["status"] == {"state": "success", "description": "No must-fix findings; 1 should-fix"}
    (finding,) = wire["findings"]
    assert finding["severity"] == "p1" and finding["state"] == "new" and finding["path"] == "src/auth.py"
    assert wire["usage"]["calls"] == 1
    assert ci[1].statuses == [] and ci[1].issue_comments == []


def test_a_dry_run_prints_the_summary_and_each_inline_comment_it_would_post(
    ci: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    result = invoke(ci[0], "--pr", "7", "--publish-bot", "--dry-run")
    assert result.exit_code == 0, result.output
    out = result.stdout
    # The summary comment itself (not only its headline), without the hidden state block.
    assert "**No must-fix findings** · 1 should-fix" in out and "lemoncrow:state" not in out
    assert "lets a missing principal through" in out
    # Then each inline comment, with the place it would go.
    assert "Dry run: would post 1 inline comment(s) and the summary above. Nothing was posted." in out
    assert "--- inline comment on src/auth.py:" in out
    assert out.index("--- inline comment on") > out.index("Dry run: would post")
    assert ci[1].statuses == [] and ci[1].issue_comments == []


def test_a_dry_run_json_carries_the_comments_and_so_does_ai_alone(ci: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    wire = json.loads(invoke(ci[0], "--pr", "7", "--publish-bot", "--dry-run", "--json").stdout)
    comments = wire["comments"]
    assert "lets a missing principal through" in comments["summary"] and "lemoncrow:state" not in comments["summary"]
    (inline,) = comments["inline"]
    assert inline["where"].startswith("src/auth.py:") and inline["body"]
    assert json.loads(invoke(ci[0], "--pr", "7", "--ai", "--json").stdout)["comments"] == comments


def test_ai_reviews_a_closed_pull_request_and_says_once_that_nothing_is_posted(
    ci: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    ci[1].state = "closed"
    result = invoke(ci[0], "--pr", "7", "--ai")
    assert result.exit_code == 0, result.output
    assert result.output.count("is closed; reviewing it anyway. Nothing will be posted.") == 1
    assert "lets a missing principal through" in result.stdout and "--- inline comment on src/auth.py:" in result.stdout
    assert _writes(ci[1]) == [] and ci[1].statuses == [] and ci[1].issue_comments == []
    wire = json.loads(invoke(ci[0], "--pr", "7", "--ai", "--json").stdout)
    assert wire["skipped"] is False and any("reviewing it anyway" in note for note in wire["notes"])


def test_the_bot_in_actions_still_skips_a_closed_pull_request_and_says_so_on_the_head(
    ci: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    ci[1].state = "closed"
    result = invoke(ci[0], "--pr", "7", "--publish-bot")
    assert result.exit_code == 0 and result.stdout.startswith("::notice title=LemonCrow Review::")
    assert "is closed; nothing to review." in result.stdout and ci[2].calls == []
    assert [row["description"] for row in ci[1].statuses] == ["skipped: pull request is closed"]
    assert ci[1].issue_comments == []


def test_a_failed_model_call_in_a_terminal_does_not_send_you_to_a_workflow_log(
    ci: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    ci[2].fail = "RuntimeError: provider unavailable"
    result = invoke(ci[0], "--pr", "7", "--ai")
    assert result.exit_code == 0, result.output
    assert "! model call errors: " in result.output and "provider unavailable" in result.output
    assert "Review incomplete: model calls failed; see the model call errors line" in result.stdout
    assert "workflow log" not in result.output
    assert _writes(ci[1]) == []


def test_the_same_failure_in_actions_still_points_at_the_workflow_log(ci: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    ci[2].fail = "RuntimeError: provider unavailable"
    result = invoke(ci[0], "--pr", "7", "--ai")
    assert "Review incomplete: model calls failed; see the workflow log" in result.stdout


def test_publishing_in_actions_writes_the_comment_and_the_status_and_says_so(
    ci: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    result = invoke(ci[0], "--pr", "7", "--publish-bot", "--fail-on", "p0")
    assert result.exit_code == 0, result.output
    assert "No must-fix findings" in result.stdout
    assert "Summary comment  https://github.com/acme/widgets/pull/7#issuecomment-" in result.stdout
    assert "Inline comments  1" in result.stdout and "Status           success:" in result.stdout
    assert [row["state"] for row in ci[1].statuses] == ["pending", "success"]
    again = invoke(ci[0], "--pr", "7", "--publish-bot")
    assert again.exit_code == 0 and "Nothing changed since the last review; nothing posted." in again.stdout


def test_a_fork_pull_request_exits_zero_with_a_workflow_notice(
    ci: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "action": "opened",
                "pull_request": {
                    "number": NUMBER,
                    "head": {"repo": {"full_name": "mallory/widgets"}},
                    "base": {"repo": {"full_name": SLUG}},
                },
            }
        )
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    result = invoke(ci[0], "--pr", "7", "--publish-bot")
    assert result.exit_code == 0
    assert result.stdout.startswith("::notice title=LemonCrow Review::Pull request from a fork")
    assert ci[1].statuses == [] and ci[2].calls == []


def test_pull_request_target_fails_the_job_with_an_error_command(ci: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    result = invoke(ci[0], "--pr", "7", "--publish-bot")
    assert result.exit_code == 1
    assert result.stdout.startswith(
        "::error title=LemonCrow Review::LemonCrow Review does not run on pull_request_target"
    )


def test_draft_and_push_flags_reach_the_run(ci: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ci[1].draft = True
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    skipped = invoke(ci[0], "--pr", "7", "--ai", "--json")
    assert json.loads(skipped.stdout)["skipped"] is True and ci[2].calls == []
    reviewed = invoke(ci[0], "--pr", "7", "--ai", "--review-drafts", "--json")
    assert json.loads(reviewed.stdout)["skipped"] is False and ci[2].calls


def test_help_documents_the_bot_flags() -> None:
    result = CliRunner().invoke(cli, ["review", "--help"])
    for flag in ("--ai", "--publish-bot", "--depth", "--fail-on", "--review-drafts", "--on-push"):
        assert flag in result.output


def test_help_explains_the_three_forms_of_a_pull_request_review() -> None:
    text = " ".join(CliRunner().invoke(cli, ["review", "--help"]).output.split())
    assert "--pr N --ai review it and print what the bot would post" in text
    assert "--pr N --ai --dry-run the same" in text
    assert "--pr N --publish-bot post those comments as a bot. Only in GitHub Actions" in text
    assert "--dry-run With --pr, show what would be posted" in text
