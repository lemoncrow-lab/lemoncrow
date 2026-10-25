"""``lc review init-action``: the workflow it writes is the one in the setup guide, edited only where the guide says to.

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


@pytest.mark.skipif(not GUIDE.exists(), reason="the setup guide is not part of this checkout")
def test_with_chat_the_command_writes_exactly_what_the_guide_shows(repo: Path) -> None:
    init(repo, "--chat")
    assert written(repo) == guide_sample()


def test_the_default_is_the_review_only_install_the_guide_describes(repo: Path) -> None:
    init(repo)
    text = written(repo)
    sample = init_action.sample()
    assert text != sample
    document = workflow(text)
    assert list(document["on"]) == ["pull_request"]
    job = document["jobs"]["review"]
    assert "if" not in job and job["runs-on"] == "ubuntu-latest" and job["timeout-minutes"] == 15
    assert "issue_comment" not in text and "pull_request_review_comment" not in text and "@lemoncrow" not in text
    # Everything else is the sample, line for line: exactly two blocks are deleted (the triggers, then the condition).
    ops = [
        op
        for op in difflib.SequenceMatcher(None, sample.splitlines(), text.splitlines(), autojunk=False).get_opcodes()
        if op[0] != "equal"
    ]
    assert [op[0] for op in ops] == ["delete", "delete"], ops
    lines = sample.splitlines()
    assert "issue_comment:" in "\n".join(lines[ops[0][1] : ops[0][2]])
    assert "if: >-" in "\n".join(lines[ops[1][1] : ops[1][2]])


def test_chat_keeps_the_comment_triggers_and_their_condition(repo: Path) -> None:
    init(repo, "--chat")
    document = workflow(written(repo))
    assert set(document["on"]) == {"pull_request", "issue_comment", "pull_request_review_comment"}
    condition = document["jobs"]["review"]["if"]
    assert "issue_comment" in condition and "@lemoncrow" in condition


@pytest.mark.parametrize(
    ("chat", "fail_on", "provider"), list(itertools.product([False, True], ["none", "p0"], ["anthropic", "openai"]))
)
def test_every_option_combination_finds_the_places_it_edits_and_is_a_valid_workflow(
    chat: bool, fail_on: str, provider: str
) -> None:
    text = init_action.render_workflow(provider=provider, fail_on=fail_on, chat=chat)
    document = workflow(text)
    steps = document["jobs"]["review"]["steps"]
    action = next(step for step in steps if "lemoncrow-lab/lemoncrow@" in str(step.get("uses")))
    assert action["with"]["fail-on"] == fail_on
    first, other = (
        ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") if provider == "openai" else ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    )
    assert action["env"] == {first: f"${{{{ secrets.{first} }}}}"}
    assert f"# or: {other}: ${{{{ secrets.{other} }}}}" in text
    assert document["permissions"] == {"contents": "read", "pull-requests": "write", "statuses": "write"}
    assert init_action.PIN_PLACEHOLDER in action["uses"]  # never a floating ref: the person pins a commit
    assert ("issue_comment" in document["on"]) is chat


def test_fail_on_p0_replaces_the_advisory_comment_with_its_own(repo: Path) -> None:
    init(repo, "--fail-on", "p0")
    text = written(repo)
    assert "fail-on: p0" in text and "fail-on: none" not in text
    assert "Advisory while you trial it" not in text and "branch protection" in text


def test_the_provider_decides_which_secret_is_passed(repo: Path) -> None:
    init(repo, "--provider", "openai")
    text = written(repo)
    assert "          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}\n" in text
    assert "          ANTHROPIC_API_KEY:" not in text.replace("# or: ANTHROPIC_API_KEY", "")


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
    monkeypatch.setattr(init_action, "sample", lambda: sample.replace("fail-on: none", "fail-on: whatever"))
    with pytest.raises(init_action.WorkflowTemplateError, match="advisory fail-on"):
        init_action.render_workflow(fail_on="p0")
    monkeypatch.setattr(init_action, "sample", lambda: sample.replace("issue_comment:", "issue_commented:"))
    assert init_action.render_workflow(chat=True)  # chat edits nothing
    monkeypatch.setattr(init_action, "sample", lambda: sample.replace("permissions:", "grants:"))
    with pytest.raises(init_action.WorkflowTemplateError, match="comment triggers"):
        init_action.render_workflow()


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
    assert "2. Pin the Action: replace <commit-sha> in .github/workflows/lemoncrow-review.yml" in out
    assert "full 40-character SHA" in out and "gh api repos/lemoncrow-lab/lemoncrow/commits/HEAD --jq .sha" in out
    assert "3. Commit the file" in out and "4. Chat is on" in out
    assert [p.name for p in (repo / ".github").rglob("*") if p.is_file()] == ["lemoncrow-review.yml"]


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
