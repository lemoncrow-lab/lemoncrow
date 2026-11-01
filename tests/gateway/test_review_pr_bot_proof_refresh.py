"""After CI finishes, the summary's proof-of-work block catches up: for that exact head only, with no model call."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import proof_refresh
from tests.gateway.pr_bot_support import BASE, NUMBER, SLUG, World, build_world


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, BASE)


def ci_finished(world: World, sha: str, *, pulls: list[dict[str, Any]] | None = None) -> list[proof_refresh.Refreshed]:
    event = world.root / "workflow_run.json"
    rows = (
        pulls
        if pulls is not None
        else [{"number": NUMBER, "base": {"repo": {"url": f"https://api.github.com/repos/{SLUG}"}}}]
    )
    event.write_text(
        json.dumps({"action": "completed", "workflow_run": {"head_sha": sha, "pull_requests": rows}}), encoding="utf-8"
    )
    env = {"GITHUB_EVENT_NAME": "workflow_run", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": SLUG}
    return proof_refresh.run(env, runner=world.github)


def summary(world: World) -> str:
    return str(world.github.summary_comments()[0]["body"])


def test_ci_finishing_rewrites_only_the_proof_of_work_block_and_calls_no_model(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    review_job = {
        "name": "review",
        "status": "in_progress",
        "conclusion": None,
        "details_url": f"https://github.com/{SLUG}/actions/runs/555/job/1",
    }
    world.github.check_runs = [{"name": "unit", "status": "in_progress", "conclusion": None}, review_job]
    world.origin.push({"src/plain.py": "VALUE = 2\n"})
    world.review()
    before, head = summary(world), world.origin.head
    assert "- Still running at review time: 1 · `unit`" in before
    world.model.reset()
    statuses = list(world.github.statuses)

    world.github.check_runs = [{"name": "unit", "status": "completed", "conclusion": "success"}, review_job]
    (done,) = ci_finished(world, head)
    after = summary(world)

    assert done.updated and world.model.calls == [] and world.github.statuses == statuses
    assert "- Passed: 1 · `unit`" in after and "Still running" not in after
    assert "`review`" not in after, "the review's own job is never proof of anything"
    changed = [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert changed == ["-- Still running at review time: 1 · `unit`", "+- Passed: 1 · `unit`"]

    (again,) = ci_finished(world, head)
    assert not again.updated and "already current" in again.reason and summary(world) == after


def test_a_summary_of_another_head_is_never_touched(world: World) -> None:
    world.origin.push({"src/plain.py": "VALUE = 2\n"})
    world.review()
    before = summary(world)
    world.github.check_runs = [{"name": "unit", "status": "completed", "conclusion": "failure"}]
    (done,) = ci_finished(world, "f" * 40)
    assert not done.updated and "left alone" in done.reason and summary(world) == before


def test_a_run_with_no_pull_request_of_this_repository_does_nothing(world: World) -> None:
    world.origin.push({"src/plain.py": "VALUE = 2\n"})
    world.review()
    before = summary(world)
    other = [{"number": NUMBER, "base": {"repo": {"url": "https://api.github.com/repos/someone/else"}}}]
    assert ci_finished(world, world.origin.head, pulls=other) == []
    assert ci_finished(world, world.origin.head, pulls=[]) == [] and summary(world) == before


def test_the_block_is_replaced_in_place_and_a_body_without_one_is_left_alone() -> None:
    body = "<h2>x</h2>\n\n**Proof of work** · Reported by CI on `abc`\n\n- Failed: 1\n- Passed: 2\n\n<sub>next</sub>"
    new = proof_refresh.replace_proof(body, ["**Proof of work** · Reported by CI on `abc`", "", "- Passed: 3"])
    assert new == "<h2>x</h2>\n\n**Proof of work** · Reported by CI on `abc`\n\n- Passed: 3\n\n<sub>next</sub>"
    assert proof_refresh.replace_proof("<h2>x</h2>", ["**Proof of work** · y"]) is None
