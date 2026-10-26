"""The pull request bot end to end: a real git origin, a fake GitHub and a fake model.

Capture, merge base, diffing, the Review API's AI endpoint, the run plan, the hard
call cap and the intelligence policy are all real. What is faked is only what leaves
the machine: GitHub's REST and GraphQL surface, and the model.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.pr_bot import engine
from lemoncrow.pro.capabilities.review.pr_bot.runner import PrPermissionError, PrReviewError, RunReport
from lemoncrow.pro.capabilities.review.pr_bot.state import dump_state, parse_state
from lemoncrow.pro.capabilities.review.sources.github import resolve_pull_request
from lemoncrow.pro.capabilities.review.sources.local import annotate_machine_finding
from tests.gateway.pr_bot_support import (
    AUTH_BUGGY,
    AUTH_FIXED,
    BASE,
    CACHE_BUGGY,
    FIRST_PUSH,
    NUMBER,
    PERSON,
    RULE,
    SLUG,
    TOKEN_BUGGY,
    World,
    build_world,
    one_call_per_file,
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, BASE)


@pytest.fixture
def ruled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """A repository whose trusted base carries a rule for ``src/auth.py``: a violation of it is a P0."""

    return build_world(tmp_path, monkeypatch, {**BASE, ".lemoncrow/review.yaml": RULE})


def posted_state(world: World) -> Any:
    (summary,) = world.github.summary_comments()
    state = parse_state(summary["body"])
    assert state is not None
    return state


def by_title(report: RunReport) -> dict[str, Any]:
    return {finding.title: finding for finding in report.findings}


# --------------------------------------------------------------------------- #
# the first review
# --------------------------------------------------------------------------- #


def test_a_first_review_posts_one_summary_and_inline_comments_for_p0_and_p1_only(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    report = world.review()

    assert not report.skipped
    # The two risk signals, and plain.py too: changed code that no keyword marks is looked at in general.
    assert sorted(world.model.paths()) == ["src/auth.py", "src/cache.py", "src/plain.py"]
    findings = by_title(report)
    assert findings["lets a missing principal through"].severity == "p1"
    assert findings["grows without a bound"].severity == "p2"

    # One atomic review, event COMMENT, with only the P1 inline; the P2 is held back in the summary.
    assert world.github.review_posts == 1
    (inline,) = world.github.inline()
    assert (inline["path"], inline["user"]["type"]) == ("src/auth.py", "Bot")
    assert inline["body"].startswith("**P1 · should fix** — lets a missing principal through")
    assert "<!-- lemoncrow:finding finding-" in inline["body"]
    assert world.github.reviews[0]["body"].strip().startswith("<!-- lemoncrow:op=bot-")

    (summary,) = world.github.summary_comments()
    assert summary["body"].startswith(
        "<!-- lemoncrow:summary v1 -->\n**No must-fix findings** · 1 should-fix · 1 optional"
    )
    assert "<details><summary>1 low-priority finding held back</summary>" in summary["body"]
    assert f"[lets a missing principal through](https://github.com/{SLUG}/pull/{NUMBER}#discussion_r" in summary["body"]
    state = posted_state(world)
    assert state.head_sha == world.origin.head and state.complete and state.reviews == 1
    thread = next(f for f in state.findings if f.severity == "p1")
    assert (thread.thread_id, thread.placement, thread.state) == (inline["id"], "inline", "new")

    pending, final = world.github.statuses
    assert (pending["state"], final["state"], final["context"]) == ("pending", "success", "LemonCrow Review")
    assert final["sha"] == world.origin.head and final["target_url"].endswith(f"#issuecomment-{summary['id']}")
    assert final["description"] == "No must-fix findings; 1 should-fix · 1 optional"


def test_the_report_and_the_posted_state_agree(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    report = world.review()
    state = posted_state(world)
    assert report.state is not None and state == report.state
    wire = report.to_wire()
    assert wire["head_sha"] == world.origin.head and wire["verdict"]["p1"] == 1 and wire["verdict"]["p2"] == 1
    assert wire["usage"]["calls"] == 3 and wire["status"]["state"] == "success"
    assert {row["id"] for row in wire["findings"]} == {finding.id for finding in state.findings}
    json.dumps(wire)  # serializable as is


def test_the_bot_never_approves_or_requests_changes(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    assert world.github.reviews and all(review["state"] == "COMMENTED" for review in world.github.reviews)


# --------------------------------------------------------------------------- #
# the same head again
# --------------------------------------------------------------------------- #


def test_a_rerun_on_the_same_head_posts_no_duplicates_and_calls_no_model(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    first = world.review()
    body = world.github.summary_comments()[0]["body"]
    writes = [call for call in world.github.calls if call[0] in {"POST", "PATCH"}]
    world.model.reset()

    again = world.review()
    assert again.noop and not again.skipped
    assert world.model.calls == []
    assert world.github.review_posts == 1 and len(world.github.inline()) == 1
    assert len(world.github.summary_comments()) == 1 and world.github.summary_comments()[0]["body"] == body
    new_writes = [call for call in world.github.calls if call[0] in {"POST", "PATCH"}][len(writes) :]
    # Only the status is written again (pending, then the same verdict): no comment, review or reply.
    assert new_writes and all("/statuses/" in path for _, path in new_writes)
    assert [(f.id, f.state) for f in again.findings] == [(f.id, "carried") for f in first.findings]


def test_a_rerun_after_a_crash_before_the_summary_adopts_the_inline_comment_it_already_posted(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    # The run died after the inline review but before the summary: nothing carries the state.
    world.github.issue_comments.clear()
    world.review()
    assert world.github.review_posts == 1 and len(world.github.inline()) == 1
    state = posted_state(world)
    assert next(f for f in state.findings if f.severity == "p1").thread_id == world.github.inline()[0]["id"]


# --------------------------------------------------------------------------- #
# a push
# --------------------------------------------------------------------------- #


def test_a_push_re_reviews_only_the_changed_file_and_carries_the_rest_without_a_model_call(world: World) -> None:
    world.model.cite_lines.add("lets a missing principal through")  # a fix is only claimed for cited lines
    world.origin.push(FIRST_PUSH)
    first = world.review()
    summary_id = world.github.summary_comments()[0]["id"]
    cache_before = next(f for f in first.findings if f.severity == "p2")
    world.model.reset()

    world.origin.push({"src/auth.py": AUTH_FIXED}, "fix auth")
    report = world.review()

    assert world.model.paths() == ["src/auth.py"]  # cache.py and plain.py were not shown to the model
    states = {f.title: f.state for f in report.findings}
    assert states == {"lets a missing principal through": "fixed", "grows without a bound": "carried"}
    carried = next(f for f in report.findings if f.state == "carried")
    assert carried.id == cache_before.id and carried.first_seen == cache_before.first_seen

    # Same summary comment, updated in place; no new inline comment and no second review.
    assert [row["id"] for row in world.github.summary_comments()] == [summary_id]
    assert world.github.review_posts == 1 and len(world.github.inline()) == 1
    body = world.github.summary_comments()[0]["body"]
    assert "<sub>Re-reviewed " in body and " · 0 new · 1 carried forward · 1 fixed since last review</sub>" in body
    assert "<details><summary>1 finding fixed since last review</summary>" in body
    state = posted_state(world)
    assert state.head_sha == world.origin.head and state.reviews == 2
    assert state.usage.calls == 1 and state.usage_total.calls == 4, "three calls the first time (plain.py too), one now"


def test_a_fixed_finding_gets_a_reply_and_its_thread_is_resolved(world: World) -> None:
    world.model.cite_lines.add("lets a missing principal through")  # a fix is only claimed for cited lines
    world.origin.push(FIRST_PUSH)
    world.review()
    (thread,) = world.github.inline()
    world.origin.push({"src/auth.py": AUTH_FIXED}, "fix auth")
    world.review()

    (reply,) = world.github.replies()
    assert reply["in_reply_to_id"] == thread["id"]
    assert reply["body"].startswith(f"Fixed in {world.origin.head[:7]}.")
    assert world.github.threads[thread["id"]]["resolved"] is True

    # A later run replies and resolves nothing more.
    world.origin.push({"src/plain.py": "VALUE = 3\n"}, "touch plain")
    world.review()
    assert len(world.github.replies()) == 1


def test_if_the_token_cannot_resolve_threads_the_reply_alone_stands(world: World) -> None:
    world.model.cite_lines.add("lets a missing principal through")  # a fix is only claimed for cited lines
    world.github.deny_resolve = True
    world.origin.push(FIRST_PUSH)
    world.review()
    world.origin.push({"src/auth.py": AUTH_FIXED}, "fix auth")
    report = world.review()
    assert len(world.github.replies()) == 1
    assert all(not thread["resolved"] for thread in world.github.threads.values())
    assert any("could not resolve the thread" in note for note in report.notes)
    assert world.github.last_status()["state"] == "success"


def test_a_new_finding_after_a_push_is_posted_once_and_a_carried_one_is_not_posted_again(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    world.model.reset()
    world.origin.push({"src/token.py": TOKEN_BUGGY}, "add token check")
    report = world.review()

    assert world.model.paths() == ["src/token.py"]
    assert sorted(f.state for f in report.findings) == ["carried", "carried", "new"]
    inline = world.github.inline()
    assert [row["path"] for row in inline] == ["src/auth.py", "src/token.py"]
    assert world.github.review_posts == 2
    assert "· 1 new · 2 carried forward · 0 fixed since last review" in world.github.summary_comments()[0]["body"]


def test_a_changed_depth_re_reviews_everything_because_last_time_meant_something_else(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    world.model.reset()
    report = world.review(depth="thorough")
    assert {"src/auth.py", "src/cache.py"} <= set(world.model.paths())
    assert report.state is not None and report.state.depth == "thorough" and report.state.complete
    assert not report.noop


def test_a_failed_model_call_is_retried_next_push_even_for_unchanged_code(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.model.fail = True
    broken = world.review()
    assert broken.state is not None and not broken.state.complete and broken.findings == ()
    body = world.github.summary_comments()[0]["body"]
    assert "**Review incomplete: model calls failed; see the workflow log**" in body
    # A review that never happened is an error whatever fail-on says; the cause is in the description.
    status = world.github.last_status()
    assert (status["state"], status["description"]) == (
        "error",
        "Review incomplete: model calls failed; see the workflow log",
    )
    assert status["target_url"].endswith("#issuecomment-1001"), "the check links to the summary comment"
    assert broken.annotations and broken.annotations[0][0] == "error"
    # No call returned, so no file counts as reviewed: every one of them is asked again.
    assert len(posted_state(world).reviewed_units) == 0

    world.model.fail = False
    world.model.reset()
    fixed = world.review()
    assert sorted(world.model.paths()) == ["src/auth.py", "src/cache.py", "src/plain.py"]
    assert fixed.state is not None and fixed.state.complete and len(fixed.findings) == 2
    assert len(posted_state(world).reviewed_units) == 3 and world.github.review_posts == 1


def test_a_finding_whose_code_changed_but_could_not_be_rechecked_stays_and_is_marked_unverified(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    world.origin.push({"src/auth.py": AUTH_FIXED}, "fix auth")
    world.model.fail = True
    report = world.review()
    kept = next(f for f in report.findings if f.severity == "p1")
    assert kept.state == "carried" and kept.unverified
    assert world.github.replies() == []
    assert "carried forward, unverified" in world.github.summary_comments()[0]["body"]


def test_a_file_that_leaves_the_pull_request_makes_its_finding_stale(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    world.origin.push({"src/auth.py": BASE["src/auth.py"]}, "revert auth")
    report = world.review()
    stale = next(f for f in report.findings if f.severity == "p1")
    assert stale.state == "stale"
    (reply,) = world.github.replies()
    assert reply["body"].startswith(f"No longer applies in {world.origin.head[:7]}")


# --------------------------------------------------------------------------- #
# skipping
# --------------------------------------------------------------------------- #


def test_draft_pull_requests_are_skipped_unless_review_drafts(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.github.draft = True
    skipped = world.review()
    assert skipped.skipped and "Draft" in skipped.reason and skipped.exit_code == 0
    assert world.model.calls == [] and world.github.issue_comments == []
    # R6: a required `LemonCrow Review` check must not wait for a review that was never going to run.
    (status,) = world.github.statuses
    assert (status["state"], status["description"], status["context"]) == (
        "success",
        "skipped: draft pull request",
        "LemonCrow Review",
    )
    assert status["sha"] == world.origin.head and skipped.published["status"]["description"] == status["description"]
    reviewed = world.review(review_drafts=True)
    assert not reviewed.skipped and len(world.github.summary_comments()) == 1
    assert [row["description"] for row in world.github.statuses][-1] != "skipped: draft pull request"


def test_a_push_is_skipped_when_on_push_is_off(world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world.origin.push(FIRST_PUSH)
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "action": "synchronize",
                "pull_request": {
                    "number": NUMBER,
                    "head": {"repo": {"full_name": SLUG}},
                    "base": {"repo": {"full_name": SLUG}},
                },
            }
        )
    )
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    off = world.review(on_push=False)
    assert off.skipped and "on-push" in off.reason and world.model.calls == []
    assert not world.review(on_push=True).skipped


def test_a_closed_pull_request_is_skipped_and_says_so_on_the_head(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.github.state = "closed"
    assert world.review().skipped and world.model.calls == []
    (status,) = world.github.statuses
    assert (status["state"], status["description"], status["sha"]) == (
        "success",
        "skipped: pull request is closed",
        world.origin.head,
    )


def test_a_skip_that_cannot_post_its_status_still_skips_quietly(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.github.draft = True
    world.github.deny_status = True
    report = world.review()
    assert report.skipped and report.exit_code == 0 and "status" not in report.published
    assert any("could not set the status" in note for note in report.notes)
    assert world.github.statuses == []


def test_a_dry_run_skip_writes_no_status(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.github.draft = True
    assert world.review(dry_run=True).skipped and world.github.statuses == []


def test_a_fork_pull_request_exits_zero_with_a_notice_and_touches_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world.origin.push(FIRST_PUSH)
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
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    report = world.review()
    assert report.skipped and report.exit_code == 0 and report.level == "notice" and "fork" in report.reason
    assert world.model.calls == [] and world.github.statuses == [] and world.github.issue_comments == []
    assert world.github.calls == []  # decided from the event alone: not even a GET


def test_pull_request_target_is_refused_with_a_failing_exit_code(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.origin.push(FIRST_PUSH)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    report = world.review()
    assert report.skipped and report.exit_code == 1 and report.level == "error"
    assert world.model.calls == [] and world.github.statuses == []


# --------------------------------------------------------------------------- #
# the check
# --------------------------------------------------------------------------- #


def test_a_p0_fails_the_status_only_when_fail_on_p0_is_set(ruled: World) -> None:
    ruled.origin.push(FIRST_PUSH)
    advisory = ruled.review()
    assert advisory.verdict is not None and advisory.verdict.state == "must_fix"
    p0 = next(f for f in advisory.findings if f.severity == "p0")
    assert p0.category == "critical_rule"
    final = ruled.github.last_status()
    assert final["state"] == "success" and final["description"] == "1 must-fix before merge (advisory: fail-on is off)"
    body = ruled.github.summary_comments()[0]["body"]
    assert body.split("\n")[1].startswith("**1 must-fix before merge**")

    blocking = ruled.review(fail_on="p0")
    assert blocking.status == ("failure", "1 must-fix before merge")
    assert ruled.github.last_status()["state"] == "failure"
    assert ruled.github.last_status()["description"] == "1 must-fix before merge"
    # The P0 has an inline comment; verdict, summary line and status all say the same thing.
    assert any(row["body"].startswith("**P0 · must fix**") for row in ruled.github.inline())


def test_no_p0_is_a_success_whatever_fail_on_says(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    report = world.review(fail_on="p0")
    assert report.status is not None and report.status[0] == "success"
    assert world.github.last_status()["state"] == "success"


def test_a_failure_on_the_way_sets_an_error_status_instead_of_leaving_pending(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.github.origin = world.origin
    original = engine.run_ai_stage

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise engine.AiStageError("the endpoint refused it")

    engine.run_ai_stage = boom  # type: ignore[assignment]
    try:
        with pytest.raises(PrReviewError, match="the endpoint refused it"):
            world.review()
    finally:
        engine.run_ai_stage = original
    assert [s["state"] for s in world.github.statuses] == ["pending", "error"]


def test_without_statuses_permission_the_review_stops_before_asking_the_model(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.github.deny_status = True
    with pytest.raises(PrPermissionError, match="statuses: write") as caught:
        world.review()
    assert caught.value.missing == ("statuses: write",)
    assert "permissions: { contents: read, pull-requests: write, statuses: write }" in str(caught.value)
    assert world.model.calls == [] and world.github.summary_comments() == [] and world.github.posted_reviews == []


def test_without_pull_requests_write_the_review_stops_before_asking_the_model_and_says_so_in_the_check(
    world: World,
) -> None:
    world.origin.push(FIRST_PUSH)
    world.github.deny_comments = True
    with pytest.raises(PrPermissionError, match="pull-requests: write") as caught:
        world.review()
    assert caught.value.missing == ("pull-requests: write",)
    assert world.model.calls == [] and world.github.issue_comments == [] and world.github.posted_reviews == []
    # The token can still write a status, so the check is red and names the permission instead of waiting.
    assert [s["state"] for s in world.github.statuses] == ["pending", "error"]
    assert world.github.last_status()["description"] == "Token is missing pull-requests: write; see the workflow log."


def test_a_token_that_passes_the_probe_and_is_refused_at_the_write_is_still_loud(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.github.deny_comments_after_probe = True
    report = world.review()
    assert report.exit_code == 1
    ((level, message),) = report.annotations
    assert level == "error" and "`pull-requests: write`" in message and "permissions: {" in message
    # The check does not read as a pass for a review nobody can read on the pull request.
    assert world.github.last_status()["state"] == "error"


def test_the_permission_probe_posts_nothing_and_is_not_counted_as_a_write(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    assert not [call for call in world.github.calls if call[1].endswith("/issues/comments/0")]


# --------------------------------------------------------------------------- #
# dry runs, local runs and the guards
# --------------------------------------------------------------------------- #


def test_a_dry_run_renders_everything_and_writes_nothing(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    report = world.review(dry_run=True)
    assert report.published == {"dry_run": True} and len(report.inline_bodies) == 1
    assert report.summary.startswith("<!-- lemoncrow:summary v1 -->")
    assert world.github.statuses == [] and world.github.issue_comments == [] and world.github.review_posts == 0


def test_a_local_ai_review_reads_but_never_writes(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    world.origin.push(FIRST_PUSH)
    report = world.review(publish=False)
    assert len(report.findings) == 2 and report.summary
    assert world.github.statuses == [] and world.github.issue_comments == [] and world.github.review_posts == 0
    assert all(method == "GET" or path.endswith("graphql") for method, path in world.github.calls)


def test_publishing_outside_github_actions_is_refused_because_the_login_would_be_a_person(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    world.origin.push(FIRST_PUSH)
    with pytest.raises(PrReviewError, match="runs only in GitHub Actions"):
        world.review()
    assert world.github.calls == [] and world.model.calls == []
    assert not world.review(dry_run=True).skipped  # a dry run writes nothing, so it may run anywhere


def test_the_kill_switch_stops_the_review_before_anything_is_captured_or_sent(world: World, tmp_path: Path) -> None:
    world.origin.push(FIRST_PUSH)
    root = tmp_path / ".lemoncrow"
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin_settings.json").write_text(json.dumps({"intelligence.enabled": False}))
    report = world.review()
    assert report.skipped and "turned off" in report.reason and report.exit_code == 0
    assert world.model.calls == [] and world.github.issue_comments == []
    assert [(row["state"], row["description"]) for row in world.github.statuses] == [
        ("success", "skipped: AI review is turned off")
    ]


def test_no_model_route_is_an_error_the_person_can_act_on(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    world.origin.push(FIRST_PUSH)
    with pytest.raises(PrReviewError, match="ANTHROPIC_API_KEY or OPENAI_API_KEY"):
        world.review()
    assert world.github.statuses == []


def test_the_model_route_comes_from_the_environment_keys(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    world.origin.push(FIRST_PUSH)
    monkeypatch.setenv("LEMONCROW_REVIEW_MODEL", "gpt-4o-mini")
    world.review(dry_run=True)
    assert os.environ["LEMONCROW_LLM_BACKEND"] == "openai" and os.environ["LEMONCROW_OPENAI_MODEL"] == "gpt-4o-mini"


def test_the_hard_call_cap_still_bounds_the_review(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.origin.push(FIRST_PUSH)
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "1")
    report = world.review()
    # The plan never exceeds the cap, so the review is not "incomplete": it looked at the highest-ranked work, said that
    # the rest is pending, and the next push competes for the cap again.
    assert len(world.model.calls) == 1 and world.model.paths() == ["src/auth.py"]
    assert report.verdict is not None and report.verdict.headline == "Partially reviewed: 1 of 3 changed areas examined"
    body = world.github.summary_comments()[0]["body"]
    assert "**Partially reviewed: 1 of 3 changed areas examined**" in body
    assert "2 changed areas not investigated yet" in body


def test_a_depth_that_cannot_cover_the_candidates_says_so_instead_of_passing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_call_per_file(monkeypatch)  # a test about the call cap: tiny files do not share a call here
    # The repository asks for Quick (depth auto follows review.effort). Quick spends one call: it looks at the
    # highest-ranked work and says the rest is pending. It must never read as a clean review.
    base = {
        "src/cache.py": BASE["src/cache.py"],
        "src/query.py": "def run(q):\n    return q\n",
        ".lemoncrow/review.yaml": "review:\n  effort: quick\n",
    }
    world = build_world(tmp_path, monkeypatch, base)
    world.origin.push(
        {"src/cache.py": CACHE_BUGGY, "src/query.py": "def run(q):\n    return q.all()  # BUG: unbounded\n"}
    )
    report = world.review()
    headline = "Partially reviewed: 1 of 2 risk signals examined"
    assert len(world.model.calls) == 1 and report.state is not None and report.state.complete
    assert report.status is not None and report.status[1].startswith(headline)  # "; 1 optional" follows: it found one
    assert f"**{headline}**" in report.summary
    assert "1 risk signal not investigated yet" in report.summary
    # The signal not reached is pending, so asking for more depth examines both.
    world.model.reset()
    world.review(depth="deep")
    assert {"src/cache.py", "src/query.py"} <= set(world.model.files_seen())


def test_a_risk_candidate_beyond_the_call_cap_is_said_so_and_investigated_on_a_later_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_call_per_file(monkeypatch)  # a test about the call cap: tiny files do not share a call here
    # The hard cap on model calls is 2 here. With three risk candidates the third is not looked at, and the summary
    # must not let that read as reviewed; the next push competes for the cap again, then nothing is owed.
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "2")
    base = {**BASE, "src/token.py": "def verify(token):\n    return True\n"}
    world = build_world(tmp_path, monkeypatch, base)
    world.origin.push({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY, "src/token.py": TOKEN_BUGGY})
    first = world.review()
    assert world.model.paths() == ["src/auth.py", "src/token.py"]
    assert {f.title for f in first.findings} == {
        "lets a missing principal through",
        "compares secrets without a constant-time check",
    }
    assert "1 risk signal not investigated yet" in world.github.summary_comments()[0]["body"]

    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 3\n"}, "touch an unrelated file")
    second = world.review()
    # cache.py was left over, and plain.py is changed code of its own; auth.py and token.py were reviewed and are carried.
    assert world.model.paths() == ["src/cache.py", "src/plain.py"]
    states = {f.title: f.state for f in second.findings}
    assert states == {
        "lets a missing principal through": "carried",
        "compares secrets without a constant-time check": "carried",
        "grows without a bound": "new",
    }
    assert "not investigated yet" not in world.github.summary_comments()[0]["body"]

    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 4\n"}, "touch it again")
    third = world.review()
    assert world.model.paths() == ["src/plain.py"] and {f.state for f in third.findings} == {"carried"}
    assert len(world.github.inline()) == 2  # the two P1s, once each; the P2 found on push two is held back


@pytest.mark.parametrize("fail_on", ["none", "p0"])
def test_a_review_that_examined_two_of_three_risk_signals_is_partial_not_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_on: str
) -> None:
    one_call_per_file(monkeypatch)  # a test about the call cap: tiny files do not share a call here
    # R2: three risk signals, the call cap allows two, nothing is found. That is "I looked at two of three and saw
    # nothing", which must not read as "No must-fix findings" in the headline or on the green check.
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "2")
    base = {**BASE, "src/token.py": "def verify(token):\n    return True\n"}
    world = build_world(tmp_path, monkeypatch, base)
    world.origin.push(
        {
            "src/auth.py": "def authorize(user):\n    return bool(user) and user.is_admin\n",
            "src/cache.py": "CACHE = {}\n\n\ndef remember(key, value):\n    CACHE[key] = (value,)\n",
            "src/token.py": "def verify(token):\n    return token is not None\n",
        }
    )
    report = world.review(fail_on=fail_on)
    assert len(world.model.paths()) == 2  # the cap allows two investigations
    assert report.findings == ()
    headline = "Partially reviewed: 2 of 3 risk signals examined"
    assert report.verdict is not None and (report.verdict.state, report.verdict.headline) == ("partial", headline)
    # Failing on P0 does not turn partial coverage into a block (its author cannot fix it), but it never passes as
    # a full review either: the check says "Partially reviewed", never "No must-fix findings".
    assert report.status == ("success", headline)
    body = world.github.summary_comments()[0]["body"]
    assert body.split("\n")[1] == f"**{headline}**"
    assert "1 risk signal not investigated yet" in body and "No must-fix findings" not in body
    pending, final = world.github.statuses
    assert (pending["state"], final["state"], final["description"]) == ("pending", "success", headline)
    assert report.to_wire()["verdict"]["state"] == "partial"

    # Once a later run reaches the signal that was left, the review reads clean again.
    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 3\n"}, "touch an unrelated file")
    done = world.review(fail_on=fail_on)
    assert done.verdict is not None and done.verdict.state == "clean"
    assert world.github.last_status()["description"] == "No must-fix findings"


def test_a_found_p0_still_reads_as_a_p0_on_a_partial_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    one_call_per_file(monkeypatch)  # a test about the call cap: tiny files do not share a call here
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "3")  # the rule's own check, then two investigations
    base = {**BASE, "src/token.py": "def verify(token):\n    return True\n", ".lemoncrow/review.yaml": RULE}
    world = build_world(tmp_path, monkeypatch, base)
    world.origin.push({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY, "src/token.py": TOKEN_BUGGY})
    report = world.review(fail_on="p0")
    assert report.verdict is not None and report.verdict.state == "must_fix"
    assert report.status is not None and report.status[0] == "failure"
    assert "risk signal not investigated yet" in report.summary


# --------------------------------------------------------------------------- #
# the human path stays human
# --------------------------------------------------------------------------- #


def test_the_human_publish_path_still_refuses_ai_annotations(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review.sources import github as github_source

    world.origin.push(FIRST_PUSH)
    monkeypatch.setattr(github_source, "run_gh", world.github)
    pull = resolve_pull_request(world.origin.clone, str(NUMBER), runner=world.github)
    store_root = world.root / "human-store"
    captured = engine.capture_pull_request(world.origin.clone, pull, store_root)
    annotate_machine_finding(
        captured.store,
        captured.session,
        captured.revision,
        path="src/auth.py",
        start_line=2,
        end_line=3,
        body="An AI finding.",
        source="ai_review",
        source_id="finding-1",
        created_by="LemonCrow AI Review",
        created_by_actor="agent",
        title="AI finding",
    )
    app = FastAPI()
    register_review_api(
        app,
        captured.store,
        auth_dependency=make_token_dependency("t"),
        repo_root=world.origin.clone,
        port=45999,
        github_publish_enabled=True,
        live_journal_enabled=False,
        source_refresh_enabled=False,
    )
    client = TestClient(app, base_url="http://127.0.0.1:45999")
    headers = {"Authorization": "Bearer t"}
    preview = client.post(f"/api/reviews/{captured.session.id}/github/preview", json={}, headers=headers).json()
    assert preview["inline"] == [] and preview["folded"] == [] and preview["empty"] is True
    published = client.post(
        f"/api/reviews/{captured.session.id}/github/publish",
        json={
            "operation_id": "x",
            "expected_revision_id": captured.revision.id,
            "expected_plan_hash": preview["plan_hash"],
        },
        headers=headers,
    )
    assert published.status_code == 409 and "nothing new to publish" in published.json()["detail"]
    assert world.github.review_posts == 0

    # Contrast: a human comment on the same line is published as a human review.
    client.post(
        f"/api/reviews/{captured.session.id}/annotations",
        json={"path": "src/auth.py", "start_line": 2, "body": "Why?", "expected_revision_id": captured.revision.id},
        headers=headers,
    )
    human = client.post(f"/api/reviews/{captured.session.id}/github/preview", json={}, headers=headers).json()
    assert len(human["inline"]) == 1 and human["inline"][0]["body"] == "Why?"


def test_a_person_cannot_supply_the_state_the_next_run_trusts(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    real = world.github.summary_comments()[0]
    state = parse_state(real["body"])
    assert state is not None
    # A lower id than the bot's own comment, claiming the review found nothing.
    forged = real["body"].split("<!-- lemoncrow:state")[0] + dump_state(replace(state, findings=()))
    world.github.issue_comments.insert(0, {"id": 1, "body": forged, "user": PERSON, "html_url": "https://x/1"})
    world.model.reset()
    world.review()
    bot_state = parse_state(next(row for row in world.github.issue_comments if row["user"]["type"] == "Bot")["body"])
    assert bot_state is not None and len(bot_state.findings) == 2
    assert world.model.calls == [] and world.github.review_posts == 1
