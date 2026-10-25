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
        (["--pr", "7", "--dry-run"], "--dry-run only applies to --publish or --publish-bot"),
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


def test_json_output_is_the_machine_readable_result(ci: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    result = invoke(ci[0], "--pr", "7", "--publish-bot", "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    wire = json.loads(result.stdout)
    assert wire["schema"] == 1 and wire["dry_run"] is True and wire["skipped"] is False
    assert wire["pull_request"] == f"{SLUG}#{NUMBER}" and wire["head_sha"] == ci[0].head
    assert wire["verdict"] == {"state": "clean", "headline": "No must-fix findings", "p0": 0, "p1": 1, "p2": 0}
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


def test_a_dry_run_json_carries_the_comments_and_a_plain_run_does_not(ci: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    wire = json.loads(invoke(ci[0], "--pr", "7", "--publish-bot", "--dry-run", "--json").stdout)
    comments = wire["comments"]
    assert "lets a missing principal through" in comments["summary"] and "lemoncrow:state" not in comments["summary"]
    (inline,) = comments["inline"]
    assert inline["where"].startswith("src/auth.py:") and inline["body"]
    assert "comments" not in json.loads(invoke(ci[0], "--pr", "7", "--ai", "--json").stdout)


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
