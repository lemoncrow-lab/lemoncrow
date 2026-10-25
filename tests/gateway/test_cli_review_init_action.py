"""``lc review init-action``: the workflow it writes is the one in the setup guide, plus only what the guide shows as additions.

The guide (``docs/setup/github-action.md``) and the packaged sample must be the same text, so the command and the page
cannot drift; every option combination must still find the places it edits; and an existing workflow file is never
overwritten without ``--force``.
"""

from __future__ import annotations

import difflib
import itertools
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review.pr_bot import init_action

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs" / "setup" / "github-action.md"
WORKFLOW = ".github/workflows/lemoncrow-review.yml"


def guide_sample() -> str:
    """The workflow block under "Install the GitHub Action" in the setup guide."""

    text = GUIDE.read_text(encoding="utf-8")
    found = re.search(r"## Install the GitHub Action\n.*?```yaml\n(.*?)\n```\n", text, re.DOTALL)
    assert found is not None, "the setup guide lost its sample workflow"
    return found.group(1) + "\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return path


def init(repo: Path, *args: str, ok: bool = True) -> Any:
    result = CliRunner().invoke(cli, ["review", "init-action", "--repo-root", str(repo), *args])
    assert (result.exit_code == 0) == ok, result.output
    return result


def written(repo: Path) -> str:
    return (repo / WORKFLOW).read_text(encoding="utf-8")


def workflow(text: str) -> dict[str, Any]:
    document = yaml.safe_load(text)
    document["on"] = document.pop(True)  # YAML reads a bare `on` as true
    return document


# --------------------------------------------------------------------------- #
# the guide and the command are one text
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not GUIDE.exists(), reason="the setup guide is not part of this checkout")
def test_the_packaged_sample_is_the_guides_sample_byte_for_byte() -> None:
    assert init_action.sample() == guide_sample()


def test_with_chat_the_command_adds_exactly_the_lines_the_guide_shows_as_the_chat_addition(repo: Path) -> None:
    init(repo, "--chat")
    text = written(repo)
    guide = GUIDE.read_text(encoding="utf-8")
    # The addition the guide shows to copy is built from the same two constants the command inserts.
    assert init_action.CHAT_TRIGGERS in guide and init_action.CHAT_CONDITION in guide
    (shown,) = [block for block in re.findall(r"```yaml\n(.*?)```", guide, re.DOTALL) if "issue_comment" in block]
    assert init_action.CHAT_TRIGGERS in shown and init_action.CHAT_CONDITION in shown
    sample = guide_sample()
    ops = [
        op
        for op in difflib.SequenceMatcher(None, sample.splitlines(), text.splitlines(), autojunk=False).get_opcodes()
        if op[0] != "equal"
    ]
    assert [op[0] for op in ops] == ["insert", "insert"], "chat only adds lines to the guide's workflow"


def test_the_default_is_the_minimal_workflow_the_guide_leads_with(repo: Path) -> None:
    init(repo)
    text = written(repo)
    assert text == init_action.sample() == guide_sample()
    document = workflow(text)
    assert list(document["on"]) == ["pull_request"]
    job = document["jobs"]["review"]
    assert "if" not in job and job["runs-on"] == "ubuntu-latest" and "timeout-minutes" not in job
    assert "issue_comment" not in text and "@lemoncrow" not in text and "actions/checkout" not in text
    (step,) = job["steps"]
    assert step["with"] == {"anthropic-api-key": "${{ secrets.ANTHROPIC_API_KEY }}"}


def test_chat_adds_the_comment_triggers_and_their_condition(repo: Path) -> None:
    init(repo, "--chat")
    document = workflow(written(repo))
    assert set(document["on"]) == {"pull_request", "issue_comment", "pull_request_review_comment"}
    condition = document["jobs"]["review"]["if"]
    assert "issue_comment" in condition and "@lemoncrow" in condition and "github.event.issue.pull_request" in condition


@pytest.mark.parametrize(
    ("chat", "fail_on", "provider"), list(itertools.product([False, True], ["none", "p0"], ["anthropic", "openai"]))
)
def test_every_option_combination_finds_the_places_it_edits_and_is_a_valid_workflow(
    chat: bool, fail_on: str, provider: str
) -> None:
    text = init_action.render_workflow(provider=provider, fail_on=fail_on, chat=chat)
    document = workflow(text)
    (action,) = document["jobs"]["review"]["steps"]
    assert action["uses"] == f"lemoncrow-lab/lemoncrow@{init_action.PIN_PLACEHOLDER}"  # never a floating ref
    key = f"{provider}-api-key"
    assert action["with"][key] == f"${{{{ secrets.{provider.upper()}_API_KEY }}}}"
    assert {name for name in action["with"] if name.endswith("api-key")} == {key}
    assert action["with"].get("fail-on", "none") == fail_on
    assert document["permissions"] == {"contents": "read", "pull-requests": "write", "statuses": "write"}
    assert ("issue_comment" in document["on"]) is chat and ("if" in document["jobs"]["review"]) is chat
    assert "env" not in action, "the key is an input, not an environment variable on the step"


def test_fail_on_p0_adds_the_one_line_to_the_with_block(repo: Path) -> None:
    init(repo, "--fail-on", "p0")
    text = written(repo)
    assert "          fail-on: p0\n" in text and "fail-on: none" not in text
    assert workflow(text)["jobs"]["review"]["steps"][0]["with"]["fail-on"] == "p0"


def test_the_provider_decides_which_secret_is_passed(repo: Path) -> None:
    init(repo, "--provider", "openai")
    text = written(repo)
    assert "          openai-api-key: ${{ secrets.OPENAI_API_KEY }}\n" in text
    assert "anthropic" not in text.lower()


def test_a_bad_option_is_refused() -> None:
    for kwargs in ({"provider": "gemini"}, {"fail_on": "p1"}):
        with pytest.raises(ValueError):
            init_action.render_workflow(**kwargs)
    result = CliRunner().invoke(cli, ["review", "init-action", "--provider", "gemini"])
    assert result.exit_code != 0 and "is not one of" in result.output


def test_a_sample_that_lost_a_place_an_option_edits_is_an_error_not_a_silent_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = init_action.sample()
    monkeypatch.setattr(init_action, "sample", lambda: sample.replace("anthropic-api-key", "the-key"))
    with pytest.raises(init_action.WorkflowTemplateError, match="Anthropic key line"):
        init_action.render_workflow(provider="openai")
    with pytest.raises(init_action.WorkflowTemplateError, match="key line the fail-on"):
        init_action.render_workflow(fail_on="p0")
    assert init_action.render_workflow(chat=False)  # no edit, no anchor needed
    monkeypatch.setattr(
        init_action,
        "sample",
        lambda: sample.replace("    types: [opened", "    branches: [x]\n    types: [opened").replace(
            "types: [opened, synchronize, reopened, ready_for_review]", "types: [opened]"
        ),
    )
    with pytest.raises(init_action.WorkflowTemplateError, match="pull_request trigger"):
        init_action.render_workflow(chat=True)
    monkeypatch.setattr(
        init_action, "sample", lambda: sample.replace("runs-on: ubuntu-latest", "runs-on: ubuntu-22.04")
    )
    with pytest.raises(init_action.WorkflowTemplateError, match="runs-on line"):
        init_action.render_workflow(chat=True)


# --------------------------------------------------------------------------- #
# writing it
# --------------------------------------------------------------------------- #


def test_it_writes_one_file_and_prints_the_next_steps(repo: Path) -> None:
    result = init(repo, "--provider", "openai", "--chat")
    assert (repo / WORKFLOW).exists()
    out = result.output
    assert f"Wrote {WORKFLOW} (review and chat, openai, fail-on none)." in out
    assert (
        "1. Add your model key as a repository secret named OPENAI_API_KEY" in out
        and "gh secret set OPENAI_API_KEY" in out
    )
    assert f"2. Pin the Action: replace <sha> in {WORKFLOW} with the release commit shown at" in out
    assert init_action.RELEASES_URL in out and "Pin the Action" in out
    # One pin approach: never the newest commit, a branch or a tag, and no command that finds one.
    assert "commits/HEAD" not in out and "40-character" not in out and "gh api" not in out and "<commit-sha>" not in out
    assert "3. Commit the file" in out and "4. Chat is on" in out
    assert [p.name for p in (repo / ".github").rglob("*") if p.is_file()] == ["lemoncrow-review.yml"]


def test_the_pin_is_one_placeholder_line_in_the_readme_the_guide_and_the_sample() -> None:
    line = init_action.PIN_LINE
    assert line == "lemoncrow-lab/lemoncrow@<sha>  # v0.9.0"
    for text in (
        init_action.sample(),
        GUIDE.read_text(encoding="utf-8"),
        (REPO / "README.md").read_text(encoding="utf-8"),
    ):
        assert line in text
    guide = GUIDE.read_text(encoding="utf-8").split("## Releasing the Action")[0]  # the owner's checklist is separate
    for contradiction in (
        "commits/HEAD",
        "<commit-sha>",
        "git ls-remote",
        "moving `v1` tag",
        "Installing a pinned version",
    ):
        assert contradiction not in guide, contradiction
    assert init_action.RELEASES_URL in guide and init_action.GUIDE_PIN_URL.endswith("#pin-the-action")
    assert "\n## Pin the Action\n" in guide


def test_the_next_steps_name_the_chosen_secret_and_leave_chat_out_when_it_is_off(repo: Path) -> None:
    out = init(repo).output
    assert "ANTHROPIC_API_KEY" in out and "OPENAI_API_KEY" not in out and "Chat is on" not in out


def test_an_existing_workflow_is_never_overwritten_without_force(repo: Path) -> None:
    target = repo / WORKFLOW
    target.parent.mkdir(parents=True)
    target.write_text("name: mine\non: push\njobs: {}\n")
    refused = init(repo, ok=False)
    assert "already exists and differs" in refused.output and "--force" in refused.output
    assert target.read_text() == "name: mine\non: push\njobs: {}\n"
    forced = init(repo, "--force")
    assert "Wrote" in forced.output and written(repo) == init_action.render_workflow()


def test_running_it_twice_is_a_no_op_the_second_time(repo: Path) -> None:
    init(repo, "--chat")
    before = (repo / WORKFLOW).stat().st_mtime_ns
    second = init(repo, "--chat")
    assert "already up to date; nothing changed" in second.output
    assert (repo / WORKFLOW).stat().st_mtime_ns == before
    different = init(repo, "--fail-on", "p0", ok=False)  # other options are another file: not silently replaced
    assert "differs" in different.output


def test_it_writes_at_the_repository_root_from_a_subdirectory(repo: Path) -> None:
    (repo / "src").mkdir()
    init(repo / "src")
    assert (repo / WORKFLOW).exists() and not (repo / "src" / ".github").exists()


def test_outside_a_repository_it_writes_nothing(tmp_path: Path) -> None:
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    result = init(nowhere, ok=False)
    assert "not inside a Git repository" in result.output and not (nowhere / ".github").exists()


def test_the_packaged_sample_ships_beside_its_module() -> None:
    assert init_action.template_path().is_file() and init_action.template_path().parent.name == "pr_bot"
