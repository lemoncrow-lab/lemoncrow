"""The human-review tier: a ``LemonCrow / human review`` status from fixed signals, and what is on an older head."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import ci_env, proof_refresh
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from lemoncrow.pro.capabilities.review.pr_bot.render import approved_on, stale_line
from lemoncrow.pro.capabilities.review.pr_bot.state import strip_state
from tests.gateway.pr_bot_support import BASE, NUMBER, SLUG, World, build_world


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, BASE)


def test_auth_code_leaves_the_human_review_status_pending_with_the_summarys_reason(world: World) -> None:
    head = world.origin.push({"src/auth.py": "def authorize(user):\n    return user.is_admin or user.is_owner\n"})
    world.review()
    status = world.github.human_statuses[-1]
    assert (status["sha"], status["state"], status["context"]) == (head, "pending", "LemonCrow / human review")
    assert status["description"].startswith("Human review required: touches security-sensitive code")
    body = world.github.summary_comments()[0]["body"]
    assert f"· {status['description']}" in body  # the summary's one human-review line, not a second
    assert body.count("Human review required") == 1
    assert world.github.last_status()["context"] == "LemonCrow Review"  # the review's own status is separate


def _reviewed(world: World, sha: str, action: str = "submitted") -> list[proof_refresh.Refreshed]:
    """The optional human-review workflow's run: a ``pull_request_review`` event for the pull request at *sha*."""

    event = world.root / "pull_request_review.json"
    payload = {"action": action, "pull_request": {"number": NUMBER, "head": {"sha": sha}}}
    event.write_text(json.dumps(payload), encoding="utf-8")
    env = {"GITHUB_EVENT_NAME": "pull_request_review", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": SLUG}
    return proof_refresh.run(env, runner=world.github)


def _approval(world: World, login: str, sha: str, association: str) -> None:
    world.github.reviews.append(
        {
            "id": len(world.github.reviews) + 1,
            "user": {"login": login, "type": "User"},
            "state": "APPROVED",
            "commit_id": sha,
            "author_association": association,
        }
    )


def test_a_collaborators_approval_of_this_head_clears_the_tier_and_a_drive_by_one_does_not(world: World) -> None:
    head = world.origin.push({"src/auth.py": "def authorize(user):\n    return user.is_admin or user.is_owner\n"})
    world.review()
    calls = len(world.model.calls)
    _approval(world, "mallory", head, "NONE")  # anyone can approve on a public repository
    (done,) = _reviewed(world, head)
    status = world.github.human_statuses[-1]
    assert done.updated and (status["sha"], status["state"]) == (head, "pending")
    assert status["description"].startswith("Human review required: touches security-sensitive code")

    _approval(world, "alice", head, "COLLABORATOR")
    _reviewed(world, head)
    status = world.github.human_statuses[-1]
    assert (status["sha"], status["state"], status["context"]) == (head, "success", "LemonCrow / human review")
    assert status["description"] == "Approved on this head by alice"
    assert len(world.model.calls) == calls, "no model call"
    # A review run on the same head keeps it: the approval is read there too.
    world.review()
    assert world.github.human_statuses[-1]["state"] == "success"


def test_an_approval_of_an_older_head_does_not_count_and_the_gate_routes_the_event(world: World) -> None:
    assert approved_on("b" * 40, [("alice", "a" * 40)]) == ()
    assert approved_on("a" * 40, [("bob", "a" * 40), ("alice", "a" * 40)]) == ("alice", "bob")
    gate = ci_env.decide({"GITHUB_EVENT_NAME": "pull_request_review"}, event={})
    assert (gate.run, gate.refresh, gate.skip, gate.error_status) == (False, True, "", "")
    assert _reviewed(world, "c" * 40, action="edited") == []  # an edited review text changes no approval


def test_a_plain_change_gets_a_green_human_review_status(world: World) -> None:
    head = world.origin.push({"src/plain.py": "VALUE = 2\n"})
    world.review()
    status = world.github.human_statuses[-1]
    assert (status["sha"], status["state"], status["description"]) == (head, "success", "No high-risk area touched")


def test_the_summary_names_an_approval_given_on_an_older_head(world: World) -> None:
    old = world.origin.push({"src/plain.py": "VALUE = 2\n"})
    world.review()
    world.github.reviews.append(
        {"id": 1, "user": {"login": "alice", "type": "User"}, "state": "APPROVED", "commit_id": old}
    )
    world.github.reviews.append(
        {"id": 2, "user": {"login": "bob", "type": "User"}, "state": "APPROVED", "commit_id": old}
    )
    world.github.reviews.append(
        {"id": 3, "user": {"login": "bob", "type": "User"}, "state": "CHANGES_REQUESTED", "commit_id": old}
    )
    head = world.origin.push({"src/plain.py": "VALUE = 3\n"})
    world.review()
    body = strip_state(world.github.summary_comments()[0]["body"])
    assert f"Not on this head (`{head[:7]}`): alice approved `{old[:7]}`." in body
    assert "bob approved" not in body and "@alice" not in body


def test_findings_last_checked_on_an_older_head_are_said() -> None:
    old, head = "a" * 40, "b" * 40
    findings = [
        BotFinding(
            id="F1", fingerprint="x", severity="p1", category="correctness", path="a.py", state="carried", last_seen=old
        ),
        BotFinding(
            id="F2", fingerprint="y", severity="p1", category="correctness", path="a.py", state="new", last_seen=head
        ),
        BotFinding(
            id="F3", fingerprint="z", severity="p1", category="correctness", path="a.py", state="fixed", last_seen=old
        ),
    ]
    assert (
        stale_line(head, findings, None)
        == "Not on this head (`bbbbbbb`): 1 open finding was last checked on `aaaaaaa`."
    )
    assert stale_line(head, findings[1:], []) == ""
