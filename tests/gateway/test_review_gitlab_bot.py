"""The GitLab merge request bot, unit by unit, against ``FakeGitLab`` (no network).

The end-to-end runs (a real capture, the runner choosing GitLab) are in ``test_review_gitlab_runner.py``.
"""

from __future__ import annotations

import hashlib

import pytest

from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from lemoncrow.pro.capabilities.review.pr_bot.github import finding_id_of, is_denied, with_backoff
from lemoncrow.pro.capabilities.review.pr_bot.gitlab import (
    NOT_READABLE,
    BotGitLab,
    _old_line_for,
    decide_gitlab,
)
from lemoncrow.pro.capabilities.review.pr_bot.state import SUMMARY_MARKER
from lemoncrow.pro.capabilities.review.sources.gitlab import GitLabApiError

from .gitlab_support import HEAD_SHA, PERSON, PROJECT_ID, SERVER, TOKEN, FakeGitLab, ref

MR = f"projects/{PROJECT_ID}/merge_requests/7"
# Old lines 1-4, new lines 1-6: new 1, 3 and 6 are context (old 1, 2 and 4); new 2, 4 and 5 are added.
DIFF = (
    "@@ -1,4 +1,6 @@\n"
    " import os\n"
    "+import sys\n"
    " def run():\n"
    "-    return 1\n"
    "+    value = 2\n"
    "+    return value\n"
    "     # end\n"
)


def finding(fid: str, line: int, end_line: int = 0, **fields: object) -> BotFinding:
    base: dict[str, object] = {
        "id": fid,
        "fingerprint": f"fp-{hashlib.sha256(fid.encode()).hexdigest()[:16]}",
        "severity": "p1",
        "category": "correctness",
        "state": "new",
        "path": "app/run.py",
        "line": line,
        "end_line": end_line or line,
        "title": f"Problem {fid}",
        "explanation": "It breaks.",
    }
    return BotFinding(**(base | fields))  # type: ignore[arg-type]


@pytest.fixture
def fake() -> FakeGitLab:
    world = FakeGitLab()
    world.diffs = [{"old_path": "app/run.py", "new_path": "app/run.py", "diff": DIFF}]
    return world


def test_summary_note_is_posted_then_updated_in_place(fake: FakeGitLab) -> None:
    bot = fake.bot()
    first = bot.write_summary(f"{SUMMARY_MARKER}\nfirst", None)
    found, _ = bot.summary()
    assert found is not None and found.id == first.id and found.by_bot

    updated = bot.write_summary(f"{SUMMARY_MARKER}\nsecond", found)
    assert updated.id == first.id
    assert [note["body"] for note in fake.notes] == [f"{SUMMARY_MARKER}\nsecond"]
    assert len(fake.called("PUT", f"{MR}/notes/{first.id}")) == 1

    calls = len(fake.calls)
    assert bot.write_summary(f"{SUMMARY_MARKER}\nsecond", updated) is updated
    assert len(fake.calls) == calls  # nothing changed, nothing sent


def test_a_marker_written_by_another_user_is_not_the_bots(fake: FakeGitLab) -> None:
    fake.note(f"{SUMMARY_MARKER}\nforged state", PERSON)
    fake.discussions.append(
        {
            "id": "e" * 40,
            "notes": [
                {
                    "id": 77,
                    "type": "DiffNote",
                    "body": "<!-- lemoncrow:finding f-1 -->",
                    "author": dict(PERSON),
                    "position": {"new_path": "app/run.py", "new_line": 2},
                }
            ],
        }
    )
    bot = fake.bot()
    assert bot.summary() == (None, None)
    [comment] = bot.review_comments()
    assert not comment.by_bot and finding_id_of(comment) == ""
    assert bot.adopt_existing([finding("f-1", 2)], [comment]) == {}


def test_discussions_carry_gitlab_positions(fake: FakeGitLab) -> None:
    assert [_old_line_for(DIFF, line) for line in (1, 2, 3, 4, 5, 6, 9)] == [1, None, 2, None, None, 4, None]
    added = finding("f-added", 2)
    context = finding("f-context", 3)
    fix = finding("f-fix", 4, 5, suggestion="    value = 3\n    return value\n", suggestion_start=4, suggestion_end=5)
    outcome = fake.bot().post_inline([added, context, fix], HEAD_SHA)

    assert set(outcome.posted) == {"f-added", "f-context", "f-fix"} and outcome.unplaced == ()
    positions = {d["notes"][0]["body"].split("**")[1]: d["notes"][0]["position"] for d in fake.discussions}
    shas = {"position_type": "text", "base_sha": "b" * 40, "start_sha": "s" * 40, "head_sha": HEAD_SHA}
    paths = {"old_path": "app/run.py", "new_path": "app/run.py"}
    assert positions["Problem f-added"] == {**shas, **paths, "new_line": 2}
    assert positions["Problem f-context"] == {**shas, **paths, "new_line": 3, "old_line": 2}
    assert positions["Problem f-fix"] == {**shas, **paths, "new_line": 5}  # anchored at the span's end
    body = next(d["notes"][0]["body"] for d in fake.discussions if "f-fix" in d["notes"][0]["body"])
    assert "```suggestion:-1+0\n    value = 3\n    return value\n```" in body
    assert "```suggestion\n" not in body and "<sub>Lines 4-5</sub>" in body
    note_id, url = outcome.posted["f-added"]
    assert url == f"{SERVER}/acme/platform/widgets/-/merge_requests/7#note_{note_id}"

    # A merge request that moved on is not posted onto.
    fake.merge_request["diff_refs"] = {**fake.merge_request["diff_refs"], "head_sha": "n" * 40}
    moved = fake.bot().post_inline([finding("f-later", 1)], HEAD_SHA)
    assert moved.posted == {}
    assert "moved to nnnnnnn" in moved.skipped_reason and len(fake.discussions) == 3


def test_a_fixed_finding_gets_one_reply_and_a_resolved_discussion(fake: FakeGitLab) -> None:
    posted = fake.bot().post_inline([finding("f-1", 2)], HEAD_SHA)
    note_id = posted.posted["f-1"][0]
    fixed = finding("f-1", 2, state="fixed", thread_id=note_id, resolved_in="c" * 40)

    bot = fake.bot()  # a later run: it knows the discussion only from reading them
    outcome = bot.reply_and_resolve([fixed], bot.review_comments())
    assert (outcome.replied, outcome.resolved, outcome.notes) == (1, 1, ())
    [discussion] = fake.discussions
    assert len(discussion["notes"]) == 2 and discussion["notes"][0]["resolved"] is True
    assert fake.called("PUT", f"{MR}/discussions/{discussion['id']}")[0][2] == {"resolved": "true"}

    again = fake.bot()
    assert again.reply_and_resolve([fixed], again.review_comments()).replied == 0  # replied once, by marker


def test_status_states_map_to_gitlab(fake: FakeGitLab) -> None:
    bot = fake.bot()
    for state in ("pending", "success", "failure", "error"):
        bot.set_status(HEAD_SHA, state, "x" * 300, "https://ci.example/job")
    assert [row["state"] for row in fake.statuses] == ["pending", "success", "failed", "failed"]
    last = fake.statuses[-1]
    assert last["name"] == "LemonCrow Review" and last["sha"] == HEAD_SHA
    assert len(last["description"]) == 255 and last["pipeline_id"] == 555
    assert last["target_url"] == "https://ci.example/job"


def test_proof_of_work_leaves_out_this_job_and_keeps_the_latest_of_each(fake: FakeGitLab) -> None:
    jobs = f"{SERVER}/acme/platform/widgets/-/jobs"
    fake.commit_statuses = [
        {"id": 1, "name": "test", "status": "failed"},  # retried: the newer run is the result
        {"id": 5, "name": "test", "status": "success"},
        {"id": 2, "name": "lint", "status": "failed"},
        {"id": 3, "name": "e2e", "status": "running"},
        {"id": 4, "name": "flaky", "status": "failed", "allow_failure": True},
        {"id": 8, "name": "deploy", "status": "manual"},
        {"id": 777, "name": "lemoncrow-review", "status": "running", "target_url": f"{jobs}/777"},
        {"id": 6, "name": "LemonCrow Review", "status": "pending"},
        {"id": 9, "name": "older", "status": "success", "sha": "o" * 40},
    ]
    report = fake.bot().ci_results(HEAD_SHA, exclude_run="777")
    assert report.readable
    assert [(c.name, c.outcome, c.detail) for c in report.checks] == [
        ("lint", "failed", ""),
        ("e2e", "running", ""),
        ("deploy", "did_not_pass", "manual"),
        ("flaky", "did_not_pass", "failed, allowed to fail"),
        ("test", "passed", ""),
    ]

    fake.fail("GET", f"projects/{PROJECT_ID}/repository/commits/{HEAD_SHA}/statuses", 403, {"message": "403"})
    refused = fake.bot().ci_results(HEAD_SHA)
    assert not refused.readable and refused.reason == NOT_READABLE


def test_decide_gitlab_skips_forks_and_refuses_what_it_must() -> None:
    env = {
        "GITLAB_CI": "true",
        "CI_PIPELINE_SOURCE": "merge_request_event",
        "CI_MERGE_REQUEST_IID": "7",
        "CI_PROJECT_ID": "42",
        "CI_MERGE_REQUEST_PROJECT_ID": "42",
        "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "42",
        "LEMONCROW_GITLAB_TOKEN": TOKEN,
    }
    assert decide_gitlab(env, require_credentials=False).run

    in_fork = decide_gitlab({**env, "CI_PROJECT_ID": "99", "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "99"})
    assert (in_fork.run, in_fork.exit_code, in_fork.writable, in_fork.skip) == (
        False,
        0,
        False,
        "merge request from a fork",
    )
    in_parent = decide_gitlab({**env, "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "99"})
    assert (in_parent.run, in_parent.level, in_parent.exit_code) == (False, "error", 1)

    no_token = decide_gitlab({**env, "LEMONCROW_GITLAB_TOKEN": ""}, require_credentials=False)
    assert (no_token.run, no_token.exit_code) == (False, 1) and "LEMONCROW_GITLAB_TOKEN" in no_token.reason
    assert TOKEN not in decide_gitlab(env).reason  # no model key: the reason names the fix, never the token

    branch = decide_gitlab({**env, "CI_PIPELINE_SOURCE": "push"})
    assert (branch.run, branch.level, branch.exit_code) == (False, "warning", 0)


def test_list_endpoints_follow_x_next_page(fake: FakeGitLab) -> None:
    fake.page_limit = 2
    for number in range(5):
        fake.note(f"note {number}")
    assert [note.body for note in fake.bot().issue_comments()] == [f"note {n}" for n in range(5)]
    pages = [call[2]["page"] for call in fake.called("GET", f"{MR}/notes")]
    assert pages == ["1", "2", "3"]


def test_rate_limits_and_refusals_read_as_the_github_ones(fake: FakeGitLab) -> None:
    fake.fail("GET", MR, 429, {"message": "Retry later"}, {"Retry-After": "7"})
    with pytest.raises(GitLabApiError) as limited:
        fake.bot().pull_facts()
    assert "(HTTP 429)" in str(limited.value) and "retry after 7s" in str(limited.value)

    waits: list[float] = []
    fake.fail("GET", MR, 429, {"message": "Retry later"}, {"Retry-After": "7"})
    patient = BotGitLab(ref(), fake.api(wrap=lambda send: with_backoff(send, sleep=waits.append)))
    assert patient.pull_facts().head_sha == HEAD_SHA and waits == [7.0]

    fake.fail("POST", f"{MR}/notes", 403, {"message": "403 Forbidden"})
    with pytest.raises(GitLabApiError) as denied:
        fake.bot().post_comment("hello")
    assert is_denied(denied.value) and "(HTTP 403)" in str(denied.value)

    fake.access_level = 20  # Reporter: may read, may not post
    with pytest.raises(GitLabApiError) as probe:
        fake.bot().probe_comment_access()
    assert is_denied(probe.value) and "Reporter" in str(probe.value)
    assert not fake.statuses and not fake.notes  # the probe wrote nothing
    assert all(TOKEN not in str(error.value) for error in (limited, denied, probe))
