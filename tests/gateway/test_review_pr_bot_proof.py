"""The summary's first screen: the verdict in words, the coverage, human review, proof of work and independence.

Proof of work is what CI reported on the exact reviewed head, labelled as CI's word; a refusal is said; a description
that claims passing tests with nothing passing on the head is flagged. Independence names an author family only when
a commit declares it exactly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import authorship, proof_of_work
from lemoncrow.pro.capabilities.review.pr_bot.github import BotGitHub
from lemoncrow.pro.capabilities.review.pr_bot.impact import Impact
from lemoncrow.pro.capabilities.review.pr_bot.render import (
    NO_HUMAN_REVIEW,
    coverage_words,
    human_review_words,
    independence_line,
    proof_lines,
    verdict,
    verdict_title,
)
from lemoncrow.pro.capabilities.review.pr_bot.state import strip_state
from lemoncrow.pro.capabilities.review.sources.github import PullRequestRef
from tests.gateway.pr_bot_support import BASE, NUMBER, World, build_world

SHA = "abc1234" + "0" * 33
CLAUDE = "Co-authored-by: Claude <noreply@anthropic.com>"
CODEX = "Co-authored-by: Codex <noreply@openai.com>"


def run(name: str, status: str = "completed", conclusion: str | None = "success", **extra: str) -> dict[str, object]:
    return {"name": name, "status": status, "conclusion": conclusion, "head_sha": SHA, **extra}


# --------------------------------------------------------------------------- #
# the headline
# --------------------------------------------------------------------------- #


def test_the_headline_says_the_verdict_in_words_for_must_fix_partial_and_clean() -> None:
    p0 = verdict([], complete=True)
    assert verdict_title(p0) == "No issues found", "zero findings is a calm, normal result"
    assert coverage_words(changed_files=12) == "All 12 changed files reviewed"
    assert coverage_words(changed_files=1) == "The one changed file reviewed"
    partial = verdict([], complete=True, deferred_files=6, total_files=14)
    assert verdict_title(partial) == "No must-fix in the part reviewed"
    assert (
        coverage_words(changed_files=8, deferred_files=6, total_files=14)
        == "Partially reviewed: 8 of 14 files, highest risk first"
    )
    assert (
        coverage_words(changed_files=3, signals=5, uninvestigated=2)
        == "Partially reviewed: 3 of 5 risk signals examined"
    )
    assert coverage_words(changed_files=3, complete=False) == "Coverage not confirmed: the review did not finish"
    assert coverage_words(changed_files=0, budget="3 of 3 reviews used") == "Nothing reviewed on this push"


def test_human_review_is_flagged_only_from_the_deterministic_signals() -> None:
    assert human_review_words(Impact("high", ("security", "persistence"), 3, 80)) == (
        "Human review required: touches security-sensitive code and persistence or a migration"
    )
    assert human_review_words(Impact("medium", ("concurrency", "sizeable"), 9, 300)) == NO_HUMAN_REVIEW
    assert human_review_words(None) == "Human review: not assessed"


# --------------------------------------------------------------------------- #
# proof of work
# --------------------------------------------------------------------------- #


def test_proof_of_work_lists_failed_running_did_not_pass_and_passed_for_the_head_only() -> None:
    report = proof_of_work.from_api(
        SHA,
        {
            "total_count": 6,
            "check_runs": [
                run("build"),
                run("test (3.12)", conclusion="failure"),
                run("e2e", status="in_progress", conclusion=None),
                run("deploy", conclusion="skipped"),
                run("old", head_sha="f" * 40),
                run("lemoncrow", status="in_progress", conclusion=None, details_url="https://x/actions/runs/42/job/1"),
            ],
        },
        {
            "sha": SHA,
            "statuses": [
                {"context": "LemonCrow Review", "state": "pending"},
                {"context": "ci/lint", "state": "success"},
            ],
        },
        exclude_run="42",
    )
    lines = proof_lines(report)
    assert lines[0] == "**Proof of work** · Reported by CI on `abc1234`, not run by LemonCrow"
    assert lines[2:] == [
        "- Failed: 1 · `test (3.12)`",
        "- Still running at review time: 1 · `e2e`",
        "- Did not pass (skipped, cancelled or neutral): 1 · `deploy` (skipped)",
        "- Passed: 2 · `build`, `ci/lint`",
    ], "an older commit's run, this review's own job and its own status are never shown"


def test_a_refused_read_says_the_permission_and_an_unbacked_claim_is_flagged() -> None:
    refused = proof_of_work.unreadable(SHA, "Resource not accessible by integration (HTTP 403)")
    assert proof_lines(refused)[2:] == ["- CI results not readable: the workflow needs `checks: read`"]
    pending = proof_of_work.from_api(
        SHA, {"check_runs": [run("test", status="queued", conclusion=None)]}, {"statuses": []}
    )
    assert proof_lines(pending, claimed=True)[-1] == (
        "- The description says tests pass; no completed passing run is reported on `abc1234` yet."
    )
    passed = proof_of_work.from_api(SHA, {"check_runs": [run("test")]}, {"statuses": []})
    assert not any("description says" in line for line in proof_lines(passed, claimed=True))
    mixed = proof_of_work.from_api(
        SHA, {"check_runs": [run("lint"), run("unit", conclusion="failure")]}, {"statuses": []}
    )
    assert proof_lines(mixed, claimed=True)[-1] == "- The description says tests pass; `unit` failed on `abc1234`."


@pytest.mark.parametrize(
    ("body", "claimed"),
    [
        ("All tests passing locally and in CI.", True),
        ("tests pass", True),
        ("CI is green", True),
        ("✅ tests", True),
        ("Not all tests pass yet", False),
        ("<!-- Confirm that tests pass -->\nFixes the retry.", False),
        ("- [ ] Tests pass", False),
        ("Adds a test for the retry path.", False),
    ],
)
def test_a_claim_that_tests_pass_is_read_from_the_authors_own_words(body: str, claimed: bool) -> None:
    assert proof_of_work.claims_tests_pass(body) is claimed


# --------------------------------------------------------------------------- #
# independence
# --------------------------------------------------------------------------- #


def test_independence_names_the_author_only_from_a_declared_trailer() -> None:
    declared = authorship.declared_authors([f"Fix retry\n\n{CODEX}", "Tidy"])
    found = authorship.assess("litellm/anthropic/claude-sonnet-4-5", declared, commits=2)
    assert independence_line(found) == (
        "Independence: reviewer Anthropic (`claude-sonnet-4-5`) · author Codex (OpenAI, Co-authored-by trailer in 1 of 2 "
        "commits) · **independent**"
    )
    same = authorship.assess("litellm/anthropic/claude-sonnet-4-5", authorship.declared_authors([CLAUDE]), commits=1)
    assert same.status == "same family"


def test_no_trailer_or_a_person_named_like_a_model_leaves_independence_unknown() -> None:
    none = authorship.assess("litellm/openai/gpt-4o", authorship.declared_authors(["Fix"]), commits=1)
    assert independence_line(none) == (
        "Independence: reviewer OpenAI (`gpt-4o`) · author unknown (no coding agent declared in the commits) · "
        "**unknown**"
    )
    person = authorship.declared_authors(["Co-authored-by: Claude Monet <claude@example.com>"])
    assert person == (), "a name is not a declaration; only an agent's address is"
    copilot = authorship.declared_authors(["Co-authored-by: Copilot <198982749+Copilot@users.noreply.github.com>"])
    assert copilot == (authorship.DeclaredAuthor("GitHub Copilot", ""),)
    assert authorship.assess("litellm/openai/gpt-4o", copilot).status == "unknown", "no model declared"
    assert authorship.assess("claude", authorship.declared_authors([CODEX])).status == "unknown", "a CLI default"
    unread = authorship.assess("litellm/openai/gpt-4o", authorship.declared_authors(None))
    assert "author unknown (the commits could not be read)" in independence_line(unread)


def test_other_exact_sources_plug_in_through_the_extra_hook() -> None:
    traced = authorship.DeclaredAuthor("Claude Code", "anthropic", source="Agent Trace record")
    declared = authorship.declared_authors(["Fix"], extra=[traced])
    assert authorship.assess("litellm/openai/gpt-4o", declared).status == "independent"


# --------------------------------------------------------------------------- #
# through the bot
# --------------------------------------------------------------------------- #


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, BASE)


def test_the_bot_shows_ci_for_the_head_the_unbacked_claim_and_the_declared_author(world: World) -> None:
    world.github.description = "Retries the call. All tests passing."
    world.github.check_runs = [
        {"name": "unit", "status": "in_progress", "conclusion": None},
        {"name": "lint", "status": "completed", "conclusion": "cancelled"},
    ]
    world.github.commit_messages = [f"Retry\n\n{CODEX}"]
    world.origin.push({"src/plain.py": "VALUE = 2\n"})
    report = world.review()
    body = strip_state(world.github.summary_comments()[0]["body"])
    head = world.origin.head[:7]
    assert f"**Proof of work** · Reported by CI on `{head}`, not run by LemonCrow" in body
    assert "- Still running at review time: 1 · `unit`" in body
    assert "- Did not pass (skipped, cancelled or neutral): 1 · `lint` (cancelled)" in body
    assert f"- The description says tests pass; no completed passing run is reported on `{head}` yet." in body
    assert "author Codex (OpenAI, Co-authored-by trailer in 1 of 1 commits)" in body
    assert body.index("Proof of work") < body.index("Independence:") < body.index("\n---\n")
    wire = report.to_wire()
    assert wire["proof_of_work"]["running"] == ["unit"] and wire["independence"]["authors"][0]["agent"] == "Codex"
    assert report.verdict is not None and wire["verdict"]["confidence"] == report.verdict.confidence


def test_a_budget_reached_summary_still_shows_ci_and_independence_for_the_new_head(world: World) -> None:
    world.origin.push({"src/plain.py": "VALUE = 2\n"})
    world.review(max_runs_per_pr=1)
    world.github.check_runs = [{"name": "unit", "status": "completed", "conclusion": "success"}]
    world.github.commit_messages = [f"Retry\n\n{CODEX}"]
    head = world.origin.push({"src/plain.py": "VALUE = 3\n"})
    report = world.review(max_runs_per_pr=1)
    assert report.verdict is not None and report.verdict.state == "budget"
    body = strip_state(world.github.summary_comments()[0]["body"])
    assert f"**Proof of work** · Reported by CI on `{head[:7]}`, not run by LemonCrow" in body
    assert "- Passed: 1 · `unit`" in body
    assert "author Codex (OpenAI, Co-authored-by trailer in 1 of 1 commits)" in body
    assert body.index("Proof of work") < body.index("Independence:") < body.index("\n---\n")


def test_every_page_of_check_runs_is_read_up_to_the_cap_and_the_rest_is_counted(world: World) -> None:
    world.github.check_runs = [
        {"name": f"job {index:03}", "status": "completed", "conclusion": "success"} for index in range(230)
    ]
    world.origin.push({"src/plain.py": "VALUE = 2\n"})
    report = world.review()
    assert report.ci is not None and len(report.ci.checks) == 230 and report.ci.unread == 0
    world.github.check_runs *= 3  # 690: five pages of 100 are read
    ci = BotGitHub(PullRequestRef("acme", "widgets", NUMBER), world.github).ci_results(world.origin.head)
    assert len(ci.checks) == 500 and ci.unread == 190
    assert "- 190 more checks on this commit not read (LemonCrow reads at most 500)" in proof_lines(ci)


def test_a_token_without_checks_read_says_so_and_the_review_still_passes(world: World) -> None:
    world.github.deny_checks = True
    world.origin.push({"src/plain.py": "VALUE = 2\n"})
    report = world.review()
    body = world.github.summary_comments()[0]["body"]
    assert "- CI results not readable: the workflow needs `checks: read`" in body
    assert report.exit_code == 0 and world.github.last_status()["state"] == "success"
