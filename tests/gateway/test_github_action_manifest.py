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
    assert "TODO(sha-pin)" in GUIDE.read_text(encoding="utf-8")


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
    ):
        assert flag in review["run"]


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
    assert workflow["concurrency"] == {
        "group": "lemoncrow-review-${{ github.event.pull_request.number }}",
        "cancel-in-progress": True,
    }
    trigger = workflow["on"] if "on" in workflow else workflow[True]
    assert list(trigger) == ["pull_request"]
    assert {"opened", "synchronize", "reopened", "ready_for_review"} <= set(trigger["pull_request"]["types"])
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
    assert fallback["if"] == "failure() || cancelled()"
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
    (config,) = [text for text in fenced("yaml") if text.startswith("# .lemoncrow/review.yaml")]
    world = build_world(tmp_path, monkeypatch, {**BASE, ".lemoncrow/review.yaml": config})
    world.origin.push(FIRST_PUSH)
    report = world.review()
    assert not report.skipped and "Review policy invalid" not in report.reason
    assert report.verdict is not None and report.state is not None and report.state.complete
    assert any("auth-required" in str(rule) for rule in yaml.safe_load(config)["rules"])
