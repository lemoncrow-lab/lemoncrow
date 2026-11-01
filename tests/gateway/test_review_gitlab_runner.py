"""The runner in a GitLab CI job: a real capture of a merge request, published through ``BotGitLab`` on ``FakeGitLab``."""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.pr_bot.runner import PrPermissionError, RunOptions, RunReport, run_pr_review
from lemoncrow.pro.capabilities.review.pr_bot.state import SUMMARY_MARKER, parse_state
from lemoncrow.pro.capabilities.review.sources.gitlab import resolve_merge_request
from tests.gateway.gitlab_support import API, IID, PATH, PROJECT_ID, SERVER, TOKEN, FakeGitLab, ref
from tests.gateway.pr_bot_support import AUTH_FIXED, BASE, FIRST_PUSH, World, build_world, git

REMOTE = f"{SERVER}/{PATH}.git"
JOB_URL = f"{SERVER}/{PATH}/-/jobs/99"


class GitLabWorld:
    """``build_world``'s origin, checked out as a GitLab CI job would: an ``origin`` on the GitLab project."""

    def __init__(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world = world
        self.fake = FakeGitLab()
        self.monkeypatch = monkeypatch
        clone = world.origin.clone
        git(clone, "remote", "set-url", "origin", REMOTE)
        git(clone, "config", f"url.{world.origin.origin}.insteadOf", REMOTE)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        for key, value in {
            "GITLAB_CI": "true",
            "CI_PIPELINE_SOURCE": "merge_request_event",
            "CI_MERGE_REQUEST_IID": str(IID),
            "CI_MERGE_REQUEST_PROJECT_ID": PROJECT_ID,
            "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": PROJECT_ID,
            "CI_PROJECT_ID": PROJECT_ID,
            "CI_PROJECT_PATH": PATH,
            "CI_SERVER_URL": SERVER,
            "CI_SERVER_HOST": "gitlab.example.com",
            "CI_API_V4_URL": API,
            "CI_PIPELINE_ID": "555",
            "CI_JOB_ID": "99",
            "CI_JOB_URL": JOB_URL,
            "LEMONCROW_GITLAB_TOKEN": TOKEN,
        }.items():
            monkeypatch.setenv(key, value)

    def push(self, files: dict[str, str | None]) -> str:
        """Commit *files* on the merge request's branch, publish ``refs/merge-requests/7/head``, and let GitLab say so."""

        origin = self.world.origin
        head = origin.push(files)
        git(origin.author, "push", "-q", "-f", "origin", f"HEAD:refs/merge-requests/{IID}/head")
        base = origin.base_tip
        self.fake.merge_request["sha"] = head
        self.fake.merge_request["diff_refs"] = {"base_sha": base, "start_sha": base, "head_sha": head}
        self.fake.diffs = [
            {"old_path": row["filename"], "new_path": row["filename"], "diff": row["patch"] or ""}
            for row in origin.patches()
        ]
        self.fake.commits = [{"id": head, "message": "change"}]
        self.monkeypatch.setenv("CI_COMMIT_SHA", head)
        return head

    def review(self) -> RunReport:
        return run_pr_review(
            RunOptions(pr=str(IID), repo_root=self.world.origin.clone, publish=True),
            structured_executor=self.world.model,
            gitlab_transport=self.fake.transport,
        )

    def summary(self) -> str:
        (note,) = [row for row in self.fake.notes if SUMMARY_MARKER in row["body"]]
        return str(note["body"])

    def statuses(self, name: str) -> list[dict[str, object]]:
        return [row for row in self.fake.statuses if row["name"] == name]


@pytest.fixture
def gitlab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitLabWorld:
    return GitLabWorld(build_world(tmp_path, monkeypatch, BASE), monkeypatch)


def test_a_gitlab_job_reviews_its_merge_request_and_closes_the_discussion_once_fixed(gitlab: GitLabWorld) -> None:
    gitlab.world.model.cite_lines.add("lets a missing principal through")  # a fix is only claimed for cited lines
    head = gitlab.push(FIRST_PUSH)
    report = gitlab.review()
    assert not report.skipped and report.pr == f"{PATH}#{IID}"
    (discussion,) = gitlab.fake.discussions
    position = discussion["notes"][0]["position"]
    assert (position["new_path"], position["head_sha"]) == ("src/auth.py", head)
    body = gitlab.summary()
    assert f"{SERVER}/{PATH}/-/commit/{head}" in body and "github.com" not in body
    state = parse_state(body)
    assert state is not None and state.review_run == "99" and state.head_sha == head
    review = gitlab.statuses("LemonCrow Review")
    assert [row["state"] for row in review] == ["pending", "success"]
    assert all(row["sha"] == head and row["pipeline_id"] == 555 for row in review)
    # A pending external status would hold the pipeline: the human-review tier is the summary's line alone on GitLab.
    assert gitlab.statuses("LemonCrow / human review") == [] and "Human review required" in body
    # A merge request takes no comment commands: the re-review line names what does start one there.
    assert "Re-review: push a commit, or run the merge request's pipeline again" in body
    assert "@lemoncrow review" not in body and "workflow" not in body
    assert gitlab.fake.called("POST", "award_emoji"), "eyes on the merge request while it runs"

    fixed = gitlab.push({"src/auth.py": AUTH_FIXED})
    gitlab.review()
    notes = discussion["notes"]
    assert len(notes) == 2 and notes[0]["resolved"] is True, "one reply, and the discussion resolved"
    assert f"Fixed in {fixed[:7]}." in notes[1]["body"]
    assert gitlab.statuses("LemonCrow Review")[-1]["sha"] == fixed
    assert len([row for row in gitlab.fake.notes if SUMMARY_MARKER in row["body"]]) == 1, "updated in place"
    assert "Not on this head" not in gitlab.summary(), "the carried finding was checked on this head"


def test_a_token_below_developer_stops_before_any_model_call_and_says_the_role(gitlab: GitLabWorld) -> None:
    head = gitlab.push(FIRST_PUSH)
    gitlab.fake.access_level = 20
    # A merged-results pipeline runs on a merge commit: its status goes to GitLab's pick, not into this pipeline.
    gitlab.monkeypatch.setenv("CI_COMMIT_SHA", "m" * 40)
    with pytest.raises(PrPermissionError, match="Developer role") as raised:
        gitlab.review()
    assert "permissions:" not in str(raised.value) and gitlab.world.model.calls == []
    last = gitlab.statuses("LemonCrow Review")[-1]
    assert (last["sha"], last["state"]) == (head, "failed") and "Developer role" in str(last["description"])
    assert last["target_url"] == JOB_URL
    assert all("pipeline_id" not in row for row in gitlab.fake.statuses)


def test_resolve_merge_request_fetches_the_head_and_names_gitlabs_range(gitlab: GitLabWorld) -> None:
    head = gitlab.push(FIRST_PUSH)
    base = gitlab.world.origin.base_tip
    pull = resolve_merge_request(gitlab.world.origin.clone, ref(), gitlab.fake.api())
    assert (pull.head_sha, pull.base_sha, pull.merge_base_sha) == (head, base, base)
    assert (pull.base_ref, pull.remote, pull.title) == ("main", "origin", "Add widgets")
    assert git(gitlab.world.origin.clone, "cat-file", "-t", head) == "commit"
