"""`action.yml` and the workflow the docs tell customers to copy: what they promise, and that the code keeps it."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review.pr_bot import ci_env, init_action
from tests.gateway.pr_bot_support import BASE, FIRST_PUSH, build_world

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / "action.yml"
GUIDE = ROOT / "docs" / "setup" / "github-action.md"


def manifest() -> dict[str, Any]:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def fenced(language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.DOTALL)


def documented_block() -> str:
    """The guide's workflow: the first yaml block under "Install the GitHub Action"."""

    (block,) = [text for text in fenced("yaml") if text.startswith("name: LemonCrow\n")][:1]
    return block


def documented_workflow() -> dict[str, Any]:
    return yaml.safe_load(documented_block())  # type: ignore[no-any-return]


def trigger_of(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow["on"] if "on" in workflow else workflow[True]  # type: ignore[no-any-return]


def step_named(name: str) -> dict[str, Any]:
    return next(step for step in steps(manifest()) if step["name"] == name)


def steps(action: dict[str, Any]) -> list[dict[str, Any]]:
    return list(action["runs"]["steps"])


def test_the_action_is_a_composite_action_at_the_repository_root() -> None:
    action = manifest()
    assert action["name"] == "LemonCrow Review" and action["runs"]["using"] == "composite"
    assert ACTION.parent == ROOT


def test_the_inputs_are_the_documented_ones_with_safe_defaults() -> None:
    inputs = manifest()["inputs"]
    assert set(inputs) == {
        "anthropic-api-key",
        "openai-api-key",
        "github-token",
        "model-provider",
        "model",
        "depth",
        "fail-on",
        "max-usd-per-pr",
        "max-runs-per-pr",
        "review-drafts",
        "on-push",
        "security-scan",
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
    # The key is the one thing a person must provide; no input is required, and an empty key falls back to the env var.
    assert (inputs["anthropic-api-key"]["default"], inputs["openai-api-key"]["default"]) == ("", "")
    assert not any(spec.get("required") for spec in inputs.values())
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
    # The security scan is on by default and `off` is the one way to keep it from downloading anything.
    assert inputs["security-scan"]["default"] == "auto"
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


def test_every_input_reference_is_declared_and_a_key_reaches_only_the_steps_that_call_the_model() -> None:
    text = ACTION.read_text(encoding="utf-8")
    declared = set(manifest()["inputs"])
    assert set(re.findall(r"inputs\.([a-z-]+)", text)) <= declared
    # Credentials are inputs now (`anthropic-api-key`, `openai-api-key`); GitHub hands a calling step's `env` to every
    # step of an action, so the point of the input is that it is wired into exactly the two steps that need it.
    assert sorted(name for name in declared if "key" in name or "secret" in name) == [
        "anthropic-api-key",
        "openai-api-key",
    ]
    holders = [
        step["name"]
        for step in steps(manifest())
        if any("api-key" in str(value) for value in (step.get("env") or {}).values())
        or any("api-key" in str(value) for value in (step.get("with") or {}).values())
    ]
    assert holders == ["Decide whether to review", "Review the pull request"]
    for step in steps(manifest()):
        assert "api-key" not in str(step.get("run", "")), "the scripts read INPUT_*_API_KEY, never an expression"
        assert "api-key" not in str(step.get("if", ""))


def test_the_steps_run_the_gate_first_and_only_continue_when_it_says_so() -> None:
    action = manifest()
    gate, *rest = steps(action)
    assert gate["id"] == "gate" and "ci_env.py" in gate["run"]
    assert (ROOT / "src/lemoncrow/pro/capabilities/review/pr_bot/ci_env.py").is_file()
    # Every step waits for the gate. The scanner's install also waits for `security-scan: auto` and nothing else does;
    # the cache's restore needs a key. The last step is the one that runs `always()`, gate permitting.
    gated = "steps.gate.outputs.run == 'true'"
    *middle, settle = rest
    assert middle and all(step["if"].startswith(gated) for step in middle)
    for step in middle:
        extra = step["if"][len(gated) :]
        assert extra == "" or re.fullmatch(
            r" && (inputs\.security-scan == 'auto'|steps\.cache_pin\.outputs\.key != '')", extra
        ), step["name"]
    assert settle["name"] == "Settle the check" and settle["if"] == f"always() && {gated}"
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


def test_the_documented_workflow_is_the_minimal_one_with_one_step_and_one_decision() -> None:
    block = documented_block()
    workflow = documented_workflow()
    assert block == init_action.sample(), "the guide leads with the very file `lc review init-action` writes"
    assert len(block.strip().splitlines()) <= 12
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "write", "statuses": "write"}
    trigger = trigger_of(workflow)
    assert list(trigger) == ["pull_request"]
    # `ready_for_review` is there so a draft's `skipped: draft pull request` status does not stand once it is ready.
    assert {"opened", "synchronize", "reopened", "ready_for_review"} <= set(trigger["pull_request"]["types"])
    (job,) = workflow["jobs"].values()
    (step,) = job["steps"]
    # One pinned `uses:` and one decision (which key). No checkout, no fetch-depth, no cleanup step, no env, no fail-on.
    assert step["uses"] == f"lemoncrow-lab/lemoncrow@{init_action.PIN_PLACEHOLDER}"
    assert step["with"] == {"anthropic-api-key": "${{ secrets.ANTHROPIC_API_KEY }}"} and "env" not in step
    assert "actions/checkout" not in block and "fetch-depth" not in block and "if:" not in block
    assert "@v1" not in GUIDE.read_text(encoding="utf-8")
    assert "@v1" not in (ROOT / "README.md").read_text(encoding="utf-8")


def test_the_job_check_and_the_status_have_different_names_and_the_guide_says_which_to_require() -> None:
    """M7: two entries both called "LemonCrow Review" let branch protection attach to the wrong one."""

    workflow = documented_workflow()
    (job_id,) = workflow["jobs"]
    shown = f"{workflow['name']} / {workflow['jobs'][job_id].get('name', job_id)}"
    assert workflow["name"] != ci_env.STATUS_CONTEXT and shown != ci_env.STATUS_CONTEXT
    assert manifest()["name"] == "LemonCrow Review", "the Action's own name is not a check"
    guide = GUIDE.read_text(encoding="utf-8")
    assert "## The check to require" in guide
    assert "There is exactly one status to require in branch protection: **`LemonCrow Review`**" in guide
    assert f"`{shown} (pull_request)`" in guide
    # The status is the bot's commit status context; nothing else posts one under that name.
    from lemoncrow.pro.capabilities.review.pr_bot.github import STATUS_CONTEXT

    assert STATUS_CONTEXT == ci_env.STATUS_CONTEXT == "LemonCrow Review"


def test_the_samples_start_advisory_and_say_how_to_opt_into_p0() -> None:
    """R1: a hallucinated P0 must not become a merge block on a customer's first day, so the copy-paste starts at none."""

    assert "fail-on" not in documented_block(), "the default is none: the minimal workflow does not even say it"
    assert manifest()["inputs"]["fail-on"]["default"] == "none"
    guide = GUIDE.read_text(encoding="utf-8")
    assert "Start at `none` for a few weeks" in guide and "branch protection" in guide
    # The README carries the very same file, as the whole install.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    (block,) = [
        text for text in re.findall(r"```yaml\n(.*?)```", readme, re.DOTALL) if "lemoncrow-lab/lemoncrow@" in text
    ]
    assert block == init_action.sample() == documented_block()
    assert (
        init_action.PIN_LINE in documented_block()
        and f"lemoncrow-lab/lemoncrow@{init_action.PIN_PLACEHOLDER}" in readme
    )
    p0 = init_action.render_workflow(fail_on="p0")
    assert yaml.safe_load(p0)["jobs"]["review"]["steps"][0]["with"]["fail-on"] == "p0"


def test_a_killed_job_cannot_leave_the_check_pending_forever() -> None:
    """R9, now the Action's own: its last step runs `always()` and settles a status still `pending`; no workflow needs one."""

    settle = steps(manifest())[-1]
    assert settle["name"] == "Settle the check" and settle["if"].startswith("always() && ")
    assert "continue-on-error" not in settle and "ci_env.py" in settle["run"] and settle["run"].endswith(" settle")
    # The workflow the guide leads with has no cleanup step: a single `uses:` is the whole job.
    assert len(documented_workflow()["jobs"]["review"]["steps"]) == 1
    assert "Do not leave LemonCrow Review pending" not in GUIDE.read_text(encoding="utf-8")
    # The token it writes with is the one the review used, and it needs nothing the sample does not grant.
    assert settle["env"]["GH_TOKEN"] == "${{ inputs.github-token }}"


def test_the_review_step_gives_the_key_and_the_git_credentials_to_the_review_and_nobody_else(tmp_path: Path) -> None:
    """Run the step's own script with a fake `lemoncrow` that reports what it was given."""

    import os
    import stat
    import subprocess

    script = str(step_named("Review the pull request")["run"])
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    fake = venv / "bin" / "lemoncrow"
    fake.write_text(
        "#!/bin/sh\n"
        'echo "anthropic=${ANTHROPIC_API_KEY:-}"\n'
        'echo "openai=${OPENAI_API_KEY:-}"\n'
        'echo "inputs=${INPUT_ANTHROPIC_API_KEY:-}${INPUT_OPENAI_API_KEY:-}"\n'
        'echo "git=${GIT_CONFIG_COUNT:-0}|${GIT_CONFIG_KEY_0:-}|${GIT_CONFIG_VALUE_0:-}"\n'
        'echo "args=$*"\n',
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    def run(**extra: str) -> dict[str, str]:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "VENV": str(venv),
            "GH_TOKEN": "ghs_TOKEN123456",
            "GITHUB_SERVER_URL": "https://github.com",
            "XDG_CACHE_HOME": str(tmp_path / "xdg"),
            "LEMONCROW_REVIEW_PR": "7",
            "LEMONCROW_REVIEW_DRAFTS": "false",
            "LEMONCROW_REVIEW_ON_PUSH": "true",
            "INPUT_MODEL": "",
            "DEPTH": "auto",
            "FAIL_ON": "none",
            "DIAGRAM": "auto",
            "MAX_USD": "",
            "MAX_RUNS": "",
            "INPUT_ANTHROPIC_API_KEY": "",
            "INPUT_OPENAI_API_KEY": "",
            **extra,
        }
        done = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        return dict(line.split("=", 1) for line in done.stdout.splitlines())

    # The input becomes the variable the model client reads, and the input variable itself is not passed on.
    seen = run(INPUT_ANTHROPIC_API_KEY="sk-ant-from-the-input")
    assert seen["anthropic"] == "sk-ant-from-the-input" and seen["inputs"] == "" and seen["openai"] == ""
    assert run(INPUT_OPENAI_API_KEY="sk-openai-from-the-input")["openai"] == "sk-openai-from-the-input"
    # No input: a key the calling step set as an environment variable is left alone.
    assert run(ANTHROPIC_API_KEY="sk-ant-from-env")["anthropic"] == "sk-ant-from-env"
    # No credential in the repository's config (persist-credentials: false, or a checkout this action made): git is given
    # the workflow token through the environment for this step only.
    count, key, value = seen["git"].split("|", 2)
    assert (count, key) == ("1", "http.https://github.com/.extraheader")
    assert value == "AUTHORIZATION: basic " + __import__("base64").b64encode(b"x-access-token:ghs_TOKEN123456").decode()
    assert "ghs_TOKEN123456" not in "".join(seen.values()), "the token is encoded, never echoed"
    # actions/checkout's own credential is used as it is: a second Authorization header would be sent twice.
    subprocess.run(
        ["git", "-C", str(repo), "config", "http.https://github.com/.extraheader", "AUTHORIZATION: basic abc"],
        check=True,
    )
    assert run()["git"] == "0||"
    assert "--fail-on none" in run()["args"] and "--ai --publish-bot" in run()["args"]
    assert (tmp_path / "xdg").is_dir(), "the parser cache directory exists before the review needs it"


def test_the_guide_never_recommends_pull_request_target() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    for block in fenced("yaml"):
        assert "pull_request_target" not in block
    assert "Do not work around it with `pull_request_target`" in text
    # The fork recipe checks out the base branch the Action makes itself, never the pull request's code.
    assert "it never checks out the pull request's code. Never add a step that does" in text
    assert "actions/checkout" not in "\n".join(fenced("yaml"))


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


def test_the_security_scan_is_optional_pinned_cached_and_never_fails_the_job() -> None:
    """The scan's install runs only for `security-scan: auto`, verifies before it runs, and is allowed to fail."""

    action = manifest()
    by_name = {step["name"]: step for step in steps(action)}
    install = by_name["Install Opengrep"]
    review = by_name["Review the pull request"]
    names = [step["name"] for step in steps(action)]
    assert names.index("Cache downloads") < names.index("Install LemonCrow") < names.index("Install Opengrep")
    assert names.index("Install Opengrep") < names.index("Review the pull request")
    assert "inputs.security-scan == 'auto'" in install["if"], "off downloads and runs nothing"
    assert install["continue-on-error"] is True, "no scanner problem fails the job"
    # The installer is the stdlib script in the repository, run before anything else is trusted.
    script = ROOT / "src/lemoncrow/pro/capabilities/review/pr_bot/opengrep_install.py"
    assert script.is_file()
    assert 'opengrep_install.py" install' in install["run"]
    # What the review step is told: the mode, the verified binary (empty when there is none) and why not.
    assert review["env"]["LEMONCROW_SECURITY_SCAN"] == "${{ inputs.security-scan }}"
    assert review["env"]["LEMONCROW_OPENGREP_BIN"] == "${{ steps.opengrep.outputs.bin }}"
    assert review["env"]["LEMONCROW_OPENGREP_NOTE"] == "${{ steps.opengrep.outputs.note }}"
    # The pin is a release tag and a checksum, or the marked TODO; the installer refuses anything else (own tests).
    from lemoncrow.pro.capabilities.review.pr_bot import opengrep_install, security

    assert review["env"]["LEMONCROW_SECURITY_SCAN"] and security.MODE_ENV == "LEMONCROW_SECURITY_SCAN"
    assert security.BIN_ENV == "LEMONCROW_OPENGREP_BIN" and security.NOTE_ENV == "LEMONCROW_OPENGREP_NOTE"
    assert opengrep_install.VERSION in GUIDE.read_text(encoding="utf-8")


def test_one_content_keyed_cache_holds_the_scanner_and_the_parser_bundle_and_python_packages_are_not_cached() -> None:
    by_name = {step["name"]: step for step in steps(manifest())}
    resolve, cache = by_name["Resolve cache"], by_name["Cache downloads"]
    review, uv = by_name["Review the pull request"], by_name["Set up uv"]
    assert resolve["continue-on-error"] is True and 'opengrep_install.py" pin' in resolve["run"]
    assert resolve["env"]["LEMONCROW_SECURITY_SCAN"] == "${{ inputs.security-scan }}", "scan off keys no scanner"
    # Keyed on content (scanner release + digest, parser pack version), restored for every run that reviews.
    assert cache["uses"].startswith("actions/cache@") and cache["continue-on-error"] is True
    assert cache["with"] == {"path": "${{ steps.cache_pin.outputs.dir }}", "key": "${{ steps.cache_pin.outputs.key }}"}
    assert "github.ref" not in str(cache) and "pull_request" not in str(cache)
    # The parser bundle is downloaded into that directory, not into the runner's home.
    assert review["env"]["XDG_CACHE_HOME"] == "${{ runner.temp }}/lemoncrow-cache/xdg"
    # setup-uv's implicit cache keys on the caller's lockfiles, not this action's: off, with the reason said.
    assert uv["with"]["enable-cache"] is False
    assert "UV_CACHE_DIR" not in ACTION.read_text(encoding="utf-8")


def test_no_runtime_download_the_review_does_not_need() -> None:
    review = step_named("Review the pull request")
    # LiteLLM reads its price table from its own package instead of fetching GitHub's main branch on every import.
    assert review["env"]["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    install = str(step_named("Install LemonCrow")["run"])
    # uv would fetch its own CPython (107 MB) on a runner whose python3 is not the .python-version; use the runner's.
    assert "--python" in install and "(3, 12) <= sys.version_info[:2] < (3, 14)" in install
    guide = GUIDE.read_text(encoding="utf-8")
    for host in ("pypi.org", "files.pythonhosted.org", "release-assets.githubusercontent.com", "api.github.com"):
        assert f"`{host}`" in guide, host
    assert (
        "raw.githubusercontent.com"
        not in guide.split("### Caching and network")[1].split("## What the bot posts")[0].split("No LemonCrow host")[0]
    )


def test_the_install_step_uses_the_runners_python_only_when_it_is_a_supported_one(tmp_path: Path) -> None:
    import stat
    import subprocess
    import sys

    script = str(step_named("Install LemonCrow")["run"])

    def run(python_version: str | None, extra: str) -> str:
        bin_dir = tmp_path / f"bin-{python_version}-{extra or 'none'}"
        bin_dir.mkdir()
        uv = bin_dir / "uv"
        uv.write_text('#!/bin/sh\necho "uv $*"\n', encoding="utf-8")
        uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
        if python_version is not None:
            major, minor = python_version.split(".")
            python = bin_dir / "python3"
            python.write_text(
                f"#!{sys.executable}\nimport sys\nsys.version_info = ({major}, {minor}, 0, 'final', 0)\n"
                "exec(compile(sys.argv[2], '<step>', 'exec'))\n",
                encoding="utf-8",
            )
            python.chmod(python.stat().st_mode | stat.S_IXUSR)
        env = {
            "PATH": str(bin_dir),  # nothing else: a missing python3 is really missing
            "GITHUB_ACTION_PATH": "/action",
            "EXTRA": extra,
            "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv"),
        }
        done = subprocess.run(
            ["/bin/bash", "-e", "-o", "pipefail", "-c", script], env=env, capture_output=True, text=True
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    base = "uv sync --project /action --frozen --no-dev --no-progress"
    assert run("3.13", "litellm") == f"{base} --extra litellm --python {tmp_path}/bin-3.13-litellm/python3"
    assert run("3.12", "") == f"{base} --python {tmp_path}/bin-3.12-none/python3"
    assert run("3.11", "cloud") == f"{base} --extra cloud", "an unsupported python is left to uv, which downloads one"
    assert run(None, "") == base, "no python3 at all: the same"


def test_the_action_checks_out_the_pull_request_itself_before_anything_is_installed() -> None:
    action = manifest()
    names = [step["name"] for step in steps(action)]
    checkout = step_named("Check out the pull request")
    assert (
        names.index("Decide whether to review")
        < names.index("Check out the pull request")
        < names.index("Install LemonCrow")
    )
    assert checkout["if"] == "steps.gate.outputs.run == 'true'"
    assert checkout["env"]["LEMONCROW_WORKDIR"] == "${{ inputs.working-directory }}"
    assert (
        checkout["run"].strip()
        == 'python3 "$GITHUB_ACTION_PATH/src/lemoncrow/pro/capabilities/review/pr_bot/checkout.py"'
    )
    assert (ROOT / "src/lemoncrow/pro/capabilities/review/pr_bot/checkout.py").is_file()
    guide = GUIDE.read_text(encoding="utf-8")
    assert "the Action checks out the pull request itself" in guide and "No `actions/checkout` step" in guide.replace(
        "There is no `actions/checkout` step", "No `actions/checkout` step"
    )
    assert "`fetch-depth: 0`" in guide, "the failure message the Action prints is explained"


def test_every_failure_the_guide_promises_is_in_its_table() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    table = guide.split("### When something is wrong")[1].split("### Inputs")[0]
    for phrase in (
        "Anthropic rejected the API key (HTTP 401)",
        "No model key: pass anthropic-api-key or openai-api-key",
        "`pull-requests: write` or `statuses: write`",
        "permissions: { contents: read, pull-requests: write, statuses: write }",
        "`fetch-depth: 0`",
        "The review job failed or was cancelled before it finished. Re-run it.",
        "**`fail-on: p0` makes the job fail exactly when the status is `failure` or `error`.**",
    ):
        assert phrase in table, phrase
    assert "issues: read" in guide.split("### Permissions")[1].split("### When something is wrong")[0]


def test_the_guide_documents_the_security_scan_its_licensing_and_how_to_add_rules() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    for phrase in (
        "## Security scan",
        "`security-scan: off` downloads and runs nothing",
        "never run until its SHA-256 matches",
        "A result is a **finding** only when its line is a line the pull request added or changed",
        "`static:` reference",
        "The scanner alone is never a P0.",
        "`Static check: <rule id>`",
        "`security scan unavailable`",
        "**Licensing of the rules.**",
        "Semgrep Rules License v1.0",
        "Commons Clause",
        "**Add your own.**",
        "security:\n  rules:",
        "`default_rules: false`",
        "from the pull request's **merge base**",
        "`nosem` comments in the pull request are ignored",
    ):
        assert phrase in guide, phrase
    (block,) = [text for text in fenced("yaml") if text.startswith("# .lemoncrow/review.yaml") and "security:" in text]
    parsed = yaml.safe_load(block)
    assert set(parsed["security"]) == {"rules", "default_rules"} and isinstance(parsed["security"]["rules"], list)
    from lemoncrow.pro.capabilities.review.pr_bot import security

    block_config = security.parse_security_block(block)
    assert block_config.rules == ("security/rules", "security/payments.yaml") and not block_config.notes
