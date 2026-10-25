"""`action.yml` and the workflow the docs tell customers to copy: what they promise, and that the code keeps it."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review.pr_bot import ci_env
from tests.gateway.pr_bot_support import BASE, FIRST_PUSH, build_world

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / "action.yml"
GUIDE = ROOT / "docs" / "setup" / "github-action.md"


def manifest() -> dict[str, Any]:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def fenced(language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.DOTALL)


def documented_workflow() -> dict[str, Any]:
    (block,) = [text for text in fenced("yaml") if text.startswith("name: LemonCrow Review")]
    return yaml.safe_load(block)  # type: ignore[no-any-return]


def steps(action: dict[str, Any]) -> list[dict[str, Any]]:
    return list(action["runs"]["steps"])


def test_the_action_is_a_composite_action_at_the_repository_root() -> None:
    action = manifest()
    assert action["name"] == "LemonCrow Review" and action["runs"]["using"] == "composite"
    assert ACTION.parent == ROOT


def test_the_inputs_are_the_documented_ones_with_safe_defaults() -> None:
    inputs = manifest()["inputs"]
    assert set(inputs) == {
        "github-token",
        "model-provider",
        "model",
        "depth",
        "fail-on",
        "max-usd-per-pr",
        "max-runs-per-pr",
        "review-drafts",
        "on-push",
        "diagram",
        "strictness",
        "inline-comments",
        "max-files",
        "mention-aliases",
        "chat-allowed-associations",
        "pr-number",
        "working-directory",
    }
    assert inputs["github-token"]["default"] == "${{ github.token }}"
    assert (inputs["depth"]["default"], inputs["fail-on"]["default"], inputs["model-provider"]["default"]) == (
        "auto",
        "none",
        "auto",
    )
    assert (inputs["review-drafts"]["default"], inputs["on-push"]["default"]) == ("false", "true")
    # The change diagram is on by default and costs nothing when the pull request has none worth drawing (G17).
    assert inputs["diagram"]["default"] == "auto"
    # The noise controls are the file's to set: an input is only a default, so each is empty unless the workflow sets it.
    assert [inputs[name]["default"] for name in ("strictness", "inline-comments", "max-files")] == ["", "", ""]
    # Chat: no alias unless asked for, and the people it answers default to those with a stake in the repository.
    assert inputs["mention-aliases"]["default"] == ""
    assert inputs["chat-allowed-associations"]["default"] == "OWNER,MEMBER,COLLABORATOR,CONTRIBUTOR"
    # A comment on a pull request carries the number in `issue`, a pull_request event in `pull_request`.
    assert "github.event.pull_request.number" in inputs["pr-number"]["default"]
    assert "github.event.issue.number" in inputs["pr-number"]["default"]
    # No budget unless the workflow asks for one.
    assert (inputs["max-usd-per-pr"]["default"], inputs["max-runs-per-pr"]["default"]) == ("", "")
    documented = GUIDE.read_text(encoding="utf-8")
    for name in inputs:
        assert f"`{name}`" in documented, f"input {name} is not documented"


def test_no_secret_or_event_text_is_pasted_into_a_shell_script() -> None:
    """Expressions reach scripts only through ``env:``, never inline: an input is attacker-controllable text."""

    for step in steps(manifest()):
        script = str(step.get("run", ""))
        assert "${{" not in script, f"{step.get('name')}: expression interpolated into a script"


def test_every_input_reference_is_declared_and_no_credential_is_an_input() -> None:
    text = ACTION.read_text(encoding="utf-8")
    declared = set(manifest()["inputs"])
    assert set(re.findall(r"inputs\.([a-z-]+)", text)) <= declared
    assert not [name for name in declared if "key" in name or "secret" in name]


def test_the_steps_run_the_gate_first_and_only_continue_when_it_says_so() -> None:
    action = manifest()
    gate, *rest = steps(action)
    assert gate["id"] == "gate" and "ci_env.py" in gate["run"]
    assert (ROOT / "src/lemoncrow/pro/capabilities/review/pr_bot/ci_env.py").is_file()
    assert rest and all(step["if"] == "steps.gate.outputs.run == 'true'" for step in rest)
    read = set(re.findall(r"steps\.gate\.outputs\.(\w+)", ACTION.read_text(encoding="utf-8")))
    assert read == {"run", "extra"}
    source = Path(ci_env.__file__).read_text(encoding="utf-8")
    assert all(f'f"{name}=' in source for name in read)


def test_the_gate_can_post_a_skipped_status_with_the_same_token_the_review_uses() -> None:
    gate = steps(manifest())[0]
    assert gate["env"]["GH_TOKEN"] == "${{ inputs.github-token }}"
    guide = GUIDE.read_text(encoding="utf-8")
    assert "Required checks and skipped runs" in guide and "skipped: draft pull request" in guide
    assert "A pull request from a fork | **none posted**" in guide


def test_it_installs_from_its_own_checkout_and_lockfile_into_a_private_environment() -> None:
    install = next(step for step in steps(manifest()) if step["name"] == "Install LemonCrow")
    assert "uv sync" in install["run"] and "--frozen" in install["run"] and "--no-dev" in install["run"]
    assert "$GITHUB_ACTION_PATH" in install["run"]
    assert install["env"]["UV_PROJECT_ENVIRONMENT"] == "${{ runner.temp }}/lemoncrow-venv"
    assert (ROOT / "uv.lock").is_file() and (ROOT / "pyproject.toml").is_file()


def test_every_action_the_manifest_uses_is_pinned_to_a_sha_or_marked_as_not_yet() -> None:
    """R11: a tag can move, and orgs that require SHA-pinned actions reject it; never invent the SHA, say it is owed."""

    lines = ACTION.read_text(encoding="utf-8").splitlines()
    uses = [(index, line) for index, line in enumerate(lines) if re.match(r"\s*uses:\s*\S+@\S+", line)]
    assert uses, "the Action installs uv with a third-party action"
    for index, line in uses:
        ref = line.split("@", 1)[1].split()[0]
        pinned = re.fullmatch(r"[0-9a-f]{40}", ref) is not None
        owed = any("TODO(sha-pin)" in earlier for earlier in lines[max(0, index - 8) : index])
        assert pinned or owed, f"{line.strip()} is neither pinned to a full commit SHA nor marked TODO(sha-pin)"


def test_the_review_step_runs_the_headless_command_with_the_workflow_token() -> None:
    review = next(step for step in steps(manifest()) if step["name"] == "Review the pull request")
    assert review["env"]["GH_TOKEN"] == "${{ inputs.github-token }}"
    for flag in (
        "--ai",
        "--publish-bot",
        "--depth",
        "--fail-on",
        "--review-drafts",
        "--no-on-push",
        "--max-usd-per-pr",
        "--max-runs-per-pr",
        "--diagram",
    ):
        assert flag in review["run"]
    assert review["env"]["DIAGRAM"] == "${{ inputs.diagram }}"
    # An empty input falls back to the default instead of failing the option parser.
    assert '--diagram "${DIAGRAM:-auto}"' in review["run"]


def test_the_action_ships_in_the_public_mirror() -> None:
    spec = importlib.util.spec_from_file_location("_mirror", ROOT / "scripts" / "mirror.py")
    assert spec and spec.loader
    mirror = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mirror)
    prefixes = mirror.load_public_prefixes()
    for path in ("action.yml", "uv.lock", "pyproject.toml", "docs/setup/github-action.md"):
        assert mirror.is_public(path, prefixes), path
    assert mirror.is_public("src/lemoncrow/pro/capabilities/review/pr_bot/ci_env.py", prefixes)


def test_the_documented_workflow_has_least_privilege_and_per_pull_request_concurrency() -> None:
    workflow = documented_workflow()
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "write", "statuses": "write"}
    # A push cancels the review of an older push; a comment has a group of its own and never cancels anything.
    group = workflow["concurrency"]["group"]
    assert "format('review-{0}', github.event.pull_request.number)" in group
    assert "format('chat-{0}', github.event.comment.id)" in group
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    trigger = workflow["on"] if "on" in workflow else workflow[True]
    # Chat (G6): the same Action answers comments, through two more events.
    assert list(trigger) == ["pull_request", "issue_comment", "pull_request_review_comment"]
    assert {"opened", "synchronize", "reopened", "ready_for_review"} <= set(trigger["pull_request"]["types"])
    assert trigger["issue_comment"]["types"] == ["created"] and trigger["pull_request_review_comment"]["types"] == [
        "created"
    ]
    # A runner starts for a comment only when it mentions the bot; every other comment on the repository costs nothing.
    condition = " ".join(str(workflow["jobs"]["review"]["if"]).split())
    assert "github.event_name == 'pull_request'" in condition
    assert "contains(github.event.comment.body, '@lemoncrow')" in condition
    assert "github.event.issue.pull_request" in condition, "a comment on a plain issue never starts a runner"
    job_steps = workflow["jobs"]["review"]["steps"]
    assert job_steps[0]["uses"].startswith("actions/checkout@") and job_steps[0]["with"]["fetch-depth"] == 0
    action_step = job_steps[1]
    # R5: no `v1` tag exists (cutting one would fail the Release workflow), so the sample pins a commit.
    assert action_step["uses"] == "lemoncrow-lab/lemoncrow@<commit-sha>"
    assert "@v1" not in GUIDE.read_text(encoding="utf-8")
    assert "@v1" not in (ROOT / "README.md").read_text(encoding="utf-8")
    assert action_step["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    # The documented checkout is the default one: never the pull request's own head.
    assert "ref" not in job_steps[0].get("with", {})


def test_the_samples_start_advisory_and_say_how_to_opt_into_p0() -> None:
    """R1: a hallucinated P0 must not become a merge block on a customer's first day, so the copy-paste starts at none."""

    sample = documented_workflow()["jobs"]["review"]["steps"][1]
    assert sample["with"]["fail-on"] == "none"
    guide = GUIDE.read_text(encoding="utf-8")
    assert "change this to `p0`" in guide and "branch protection" in guide
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    (block,) = [
        text for text in re.findall(r"```yaml\n(.*?)```", readme, re.DOTALL) if "lemoncrow-lab/lemoncrow@" in text
    ]
    (step,) = yaml.safe_load(block)
    assert step["with"]["fail-on"] == "none" and "set `p0`" in block


def test_a_killed_job_cannot_leave_the_check_pending_forever(tmp_path: Path) -> None:
    """R9: the sample's last step turns a still-pending ``LemonCrow Review`` status into an error, and nothing else."""

    import stat
    import subprocess

    steps_ = documented_workflow()["jobs"]["review"]["steps"]
    fallback = steps_[-1]
    assert fallback["if"] == "(failure() || cancelled()) && github.event_name == 'pull_request'"
    assert "if" not in steps_[1], "the review step itself must not be conditional"
    assert fallback["env"]["SHA"] == "${{ github.event.pull_request.head.sha }}"
    script = str(fallback["run"])
    assert "${{" not in script and 'context="LemonCrow Review"' in script and "state=error" in script

    def run(current: str) -> list[str]:
        """Run the step's script with a fake ``gh`` that answers the status lookup with *current* and records writes."""

        log = tmp_path / f"calls-{current or 'none'}.txt"
        shim = tmp_path / f"bin-{current or 'none'}"
        shim.mkdir()
        gh = shim / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *--method*) echo "$*" >> "$LOG" ;;\n'
            f'  *) echo "{current}" ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        env = {
            "PATH": f"{shim}:/usr/bin:/bin",
            "LOG": str(log),
            "REPO": "acme/widgets",
            "SHA": "c" * 40,
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "99",
            "HOME": str(tmp_path),
        }
        done = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True, check=False)
        assert done.returncode == 0, done.stderr
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    (posted,) = run("pending")
    assert f"repos/acme/widgets/statuses/{'c' * 40}" in posted and "state=error" in posted
    assert "context=LemonCrow Review" in posted and "actions/runs/99" in posted
    assert run("success") == [] and run("failure") == [] and run("") == [], "only a pending status is touched"


def test_the_guide_never_recommends_pull_request_target() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    for block in fenced("yaml"):
        assert "pull_request_target" not in block
    assert "Do not work around it with `pull_request_target`" in text
    assert "Never check out the pull request's head" in text


def test_the_documented_review_yaml_is_accepted_and_changes_nothing_it_should_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The guide shows two files with this header: the budget-and-rules one, and the `import:` one (own tests).
    (config,) = [
        text for text in fenced("yaml") if text.startswith("# .lemoncrow/review.yaml") and "\nrules:\n" in text
    ]
    world = build_world(tmp_path, monkeypatch, {**BASE, ".lemoncrow/review.yaml": config})
    world.origin.push(FIRST_PUSH)
    report = world.review()
    assert not report.skipped and "Review policy invalid" not in report.reason
    assert report.verdict is not None and report.state is not None and report.state.complete
    assert any("auth-required" in str(rule) for rule in yaml.safe_load(config)["rules"])


def test_chat_is_wired_through_the_gate_and_the_review_step_and_documented() -> None:
    """G6: the comment events reach the same Action, which reads its chat settings from the same two variables."""

    action = manifest()
    gate = steps(action)[0]
    review = next(step for step in steps(action) if step["name"] == "Review the pull request")
    for step in (gate, review):
        assert step["env"][ci_env.MENTIONS_ENV] == "${{ inputs.mention-aliases }}"
        assert step["env"][ci_env.ASSOCIATIONS_ENV] == "${{ inputs.chat-allowed-associations }}"
    guide = GUIDE.read_text(encoding="utf-8")
    for phrase in (
        "## Chat in pull request threads",
        "The trigger word is `@lemoncrow`",
        "`issue_comment`",
        "`pull_request_review_comment`",
        "`recheck`",
        "`max-runs-per-pr`",
        "`chat-allowed-associations`",
        "`mention-aliases`",
        "`pull-requests: write`",
    ):
        assert phrase in guide, phrase
    # The default the guide states is the default the code applies.
    assert ci_env.DEFAULT_MENTION == "@lemoncrow"
    assert ",".join(ci_env.DEFAULT_ASSOCIATIONS) == manifest()["inputs"]["chat-allowed-associations"]["default"]
    assert "OWNER`, `MEMBER`, `COLLABORATOR` or `CONTRIBUTOR`" in guide


def test_the_guide_documents_the_review_command_and_the_help_text_lists_it() -> None:
    """Item 3: `@lemoncrow review` is in the command table, its rules are stated, and the bot's own help says it too."""

    from lemoncrow.pro.capabilities.review.pr_bot import chat

    guide = GUIDE.read_text(encoding="utf-8")
    for phrase in (
        "@lemoncrow review [quick|auto|thorough|deep]",
        "**`review`.**",
        "It is the review a push runs, started by a person",
        "on a draft",
        "`quick` is understood but runs Auto",
        "Nothing has changed since my last review",
        "A deeper depth on the same commit re-examines it all",
        "`max-runs-per-pr` and `max-usd-per-pr` apply as they do to a push",
        "even when nothing has changed",
        "reacts with eyes",
        "never set to `pending` by a command",
        "POST /pulls/comments/{id}/reactions",
    ):
        assert phrase in guide, phrase
    # The sample asks for no new permission, and the Action's own words agree with the guide.
    assert documented_workflow()["permissions"] == {"contents": "read", "pull-requests": "write", "statuses": "write"}
    assert (
        "@lemoncrow review" in manifest()["description"]
        and "@lemoncrow review" in manifest()["inputs"]["depth"]["description"]
    )
    assert "`{handle} review [quick|auto|thorough|deep]`" in chat.HELP


def test_the_guide_documents_learning_and_its_limits_and_the_sample_asks_for_no_extra_permission() -> None:
    """G9: feedback is read with the permissions the sample already has, and wider learning is explicit and inert."""

    guide = GUIDE.read_text(encoding="utf-8")
    for phrase in (
        "## Learning from feedback",
        "Suggested rules from feedback",
        "`@lemoncrow ignore <category or words> [in <path glob>]`",
        "**and its code has not changed**",
        "GitHub sends no event when someone reacts",
        "the bot writes nothing to your repository",
        "pull request's **merge base**",
        "cannot be dismissed by a reaction, a reply, a resolved thread or `ignore`",
        "A suppression cannot be undone from a comment yet",
        # A must-fix is dismissed by a second person, never by the pull request's author (and `CONTRIBUTOR` is not enough).
        "**Who may dismiss a must-fix (P0).**",
        "**owner, member or collaborator who did not open the pull request**",
        "A must-fix on your own pull request needs a reviewer other than the author to dismiss it.",
        "`CONTRIBUTOR` is on the default allow-list",
        "This needs a second person.",
    ):
        assert phrase in guide, phrase
    # Reading reactions and resolutions, replying in a thread and rewriting the summary need nothing new.
    assert documented_workflow()["permissions"] == {"contents": "read", "pull-requests": "write", "statuses": "write"}
    # The suggested YAML in the guide is a valid rules block for the repository's own loader.
    (example,) = [text for text in fenced("yaml") if text.startswith('rules:\n  - stable_key: "learned-')]
    parsed = yaml.safe_load(example)
    assert set(parsed["rules"][0]) == {"stable_key", "instruction", "paths"}


def test_the_guide_documents_the_change_diagram_and_how_to_turn_it_off() -> None:
    """G17: what it shows, why it never guesses, what it costs, and the one switch."""

    guide = GUIDE.read_text(encoding="utf-8")
    for phrase in (
        "## Change diagram",
        "after the findings and before the receipt",
        "**Why it never guesses.**",
        "Architecture Delta",
        "No model is called",
        "not a **sequence diagram**",
        "not an **entity-relationship diagram**",
        "not a **class diagram**",
        "`N more relationships not shown`",
        "the diagram is the first thing dropped",
        "`LEMONCROW_REVIEW_DIAGRAM_MAX_FILES`",
        "Set `diagram: off`",
        "There is no `.lemoncrow/review.yaml` key for it",
    ):
        assert phrase in guide, phrase
    # The example in the guide is the grammar the code emits, so a reader can recognise what the bot posts.
    (example,) = re.findall(r"```mermaid\n(.*?)```", guide, re.DOTALL)
    assert example.startswith("flowchart LR\n") and ":::added" in example and "linkStyle" in example


def test_the_noise_inputs_reach_the_reviewer_through_the_environment_and_the_guide_explains_them() -> None:
    """Noise controls: three inputs, passed as the variables ``NoiseInputs.from_env`` reads, under a file that wins."""

    from lemoncrow.pro.capabilities.review.pr_bot.settings import INLINE_ENV, MAX_FILES_ENV, STRICTNESS_ENV

    review = next(step for step in steps(manifest()) if step["name"] == "Review the pull request")
    assert review["env"][STRICTNESS_ENV] == "${{ inputs.strictness }}"
    assert review["env"][INLINE_ENV] == "${{ inputs.inline-comments }}"
    assert review["env"][MAX_FILES_ENV] == "${{ inputs.max-files }}"
    gate = steps(manifest())[0]
    assert not {STRICTNESS_ENV, INLINE_ENV, MAX_FILES_ENV} & set(gate["env"])  # the standard-library gate needs none
    guide = GUIDE.read_text(encoding="utf-8")
    for phrase in (
        "## Reducing noise",
        "### Strictness and the threshold",
        "### Ignore paths",
        "### A rule's severity",
        "### Summary only",
        "### A large pull request",
        "### Filters of the workflow",
        "### Where a setting comes from",
        "A P0 is always surfaced and always inline",
        "Held back is counted, never dropped.",
        "`ai_denied` row of `ai_content_access`",
        "**Asking for `p0` gives `p1`**",
        "Partially reviewed: 100 of 143 files, highest risk first",
        "**On a `pull_request` run GitHub reads the workflow from the pull request's own merge commit**",
    ):
        assert phrase in guide, phrase


def test_the_documented_noise_block_is_read_by_the_loaders_it_documents() -> None:
    """The example in "Reducing noise" parses with the very code that applies it, and every key is a documented one."""

    from lemoncrow.pro.capabilities.review.criticality import parse_trusted_base_triage
    from lemoncrow.pro.capabilities.review.pr_bot.settings import parse_file_settings

    (example,) = [text for text in fenced("yaml") if text.startswith("# Noise controls (.lemoncrow/review.yaml")]
    parsed = parse_file_settings(example)
    assert parsed.notes == () and parsed.ignore == ("docs/**", "**/*.lock")
    assert (parsed.inline_comments, parsed.max_files, parsed.rule_severity) == (True, 60, {"auth-required": "p1"})
    layer = parse_trusted_base_triage(example, source="doc")
    assert layer is not None and layer.errors == () and layer.finding_threshold == "high"  # strictness: 3
    # And the run plan, which rejects unknown keys of `review:`, accepts every key the example uses.
    from lemoncrow.pro.capabilities.review.run_plan import parse_review_config_layer

    run = parse_review_config_layer(example, source="doc")
    assert run is not None and run.errors == ()


def test_the_guide_documents_the_intent_it_reads_and_what_it_cannot_do() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    for phrase in (
        "## Intent",
        "`Fixes #12`",
        "**skipped with a note**",
        "`issues: read`",
        "**1,000 estimated tokens**",
        "`data:pr_metadata`, `data:work_item`",
        "**It is never more than a P1**",
        "not shown the intent at all",
        "It is not an entry of the evidence registry",
        "`LEMONCROW_REVIEW_INTENT=0`",
        "`Intent: read the pull request title (it has no description)`",
    ):
        assert phrase in guide, phrase
    # Reading an issue needs a permission the sample does not grant, so the sample stays at least privilege.
    assert "issues" not in documented_workflow()["permissions"]
