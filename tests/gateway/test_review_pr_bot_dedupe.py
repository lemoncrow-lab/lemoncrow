"""One comment per concern (R8): an existing bot comment for the same finding is adopted, never posted again.

Adoption used to be by finding id, and the id includes the model's title and evidence, so a run cancelled by
``concurrency`` after it posted its inline comment but before it wrote the summary (which carries the state) left a
comment the next run could not recognise: the model worded the same concern differently, got a new id, and the
bot posted a duplicate. The comment now also carries the finding's fingerprint (category, targets, rules), which
does not change when the wording does, and the bot looks for it, on the same file, right before it posts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding, fingerprint_for
from lemoncrow.pro.capabilities.review.pr_bot.github import BotGitHub
from lemoncrow.pro.capabilities.review.pr_bot.render import inline_comment
from lemoncrow.pro.capabilities.review.sources.github import pull_request_ref
from tests.gateway.pr_bot_support import (
    AUTH_BUGGY,
    BASE,
    BOT,
    NUMBER,
    PERSON,
    World,
    build_world,
)

TITLE = "lets a missing principal through"
REWORDED = AUTH_BUGGY.replace(TITLE, "accepts a request with no principal at all")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, BASE)


def test_a_cancelled_run_that_lost_its_summary_does_not_post_a_reworded_duplicate(world: World) -> None:
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    first = world.review()
    (found,) = first.findings
    (posted,) = world.github.inline()
    assert f"<!-- lemoncrow:fingerprint {found.fingerprint} -->" in posted["body"]
    # The run was cancelled between the inline review and the summary: the state is gone.
    world.github.issue_comments.clear()

    # The next push words the same concern differently: a new finding id, the same category, target and file.
    world.origin.push({"src/auth.py": REWORDED}, "reword")
    again = world.review()

    (kept,) = again.findings
    assert kept.id != found.id and kept.fingerprint == found.fingerprint, "the wording changed, the concern did not"
    assert world.github.review_posts == 1 and len(world.github.inline()) == 1, "no duplicate comment"
    assert (kept.thread_id, kept.placement, kept.state) == (posted["id"], "inline", "new")
    assert kept.thread_url.endswith(f"#discussion_r{posted['id']}")
    # The summary links the comment that is there, on the lines it sits on.
    assert f"(https://github.com/acme/widgets/pull/{NUMBER}#discussion_r{posted['id']})" in again.summary
    assert again.published.get("inline", 0) == 0


def test_a_fingerprint_on_another_file_or_from_a_person_suppresses_nothing(world: World) -> None:
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review()
    (found,) = report.findings
    (posted,) = world.github.inline()
    world.github.issue_comments.clear()
    world.github.review_comments.clear()
    # The same markers, but on another file, and then on the right file but from a person.
    world.github.review_comments.append({**posted, "id": 5001, "path": "src/other.py"})
    world.github.review_comments.append({**posted, "id": 5002, "user": PERSON})
    world.origin.push({"src/auth.py": REWORDED}, "reword")
    again = world.review()
    assert world.github.review_posts == 2
    (kept,) = again.findings
    assert kept.thread_id not in {5001, 5002} and kept.fingerprint == found.fingerprint


def test_the_thread_of_a_fixed_finding_does_not_silence_the_bug_when_it_comes_back(world: World) -> None:
    world.model.cite_lines.add(TITLE)
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    world.review()
    world.origin.push({"src/auth.py": AUTH_BUGGY.replace("return True", "return False").replace("# BUG: " + TITLE, "")})
    fixed = world.review()
    assert [f.state for f in fixed.findings] == ["fixed"]

    # The summary is lost, and the bug returns: the old thread (replied to, resolved) must not adopt it.
    world.github.issue_comments.clear()
    world.origin.push({"src/auth.py": AUTH_BUGGY.replace(TITLE, "lets an absent principal through")}, "regression")
    back = world.review()
    assert len(world.github.inline()) == 2 and world.github.review_posts == 2
    (kept,) = back.findings
    assert kept.state == "new" and kept.thread_id == world.github.inline()[1]["id"]


def finding(identifier: str, *, line: int = 2) -> BotFinding:
    return BotFinding(
        id=identifier,
        fingerprint=fingerprint_for("security", ("target:one",)),
        severity="p1",
        category="security",
        state="new",
        path="src/auth.py",
        line=line,
        end_line=line,
        target_ids=("target:one",),
        title=f"Problem {identifier}",
    )


def github_for(world: World) -> BotGitHub:
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    return BotGitHub(pull_request_ref(world.origin.clone, str(NUMBER), runner=world.github), world.github)


def bot_comment(identifier: str, owner: BotFinding, *, comment_id: int, line: int = 2) -> dict[str, object]:
    return {
        "id": comment_id,
        "path": owner.path,
        "line": line,
        "side": "RIGHT",
        "body": inline_comment(owner.changed(id=identifier)),
        "user": BOT,
        "html_url": f"https://github.com/acme/widgets/pull/{NUMBER}#discussion_r{comment_id}",
    }


def test_right_before_posting_a_comment_that_appeared_meanwhile_is_adopted_not_duplicated(world: World) -> None:
    github = github_for(world)
    world.github.review_comments.append(bot_comment("racing-run", finding("racing-run"), comment_id=7001, line=3))
    outcome = github.post_inline([finding("mine")], world.origin.head)
    assert world.github.review_posts == 0
    assert outcome.posted == {"mine": (7001, f"https://github.com/acme/widgets/pull/{NUMBER}#discussion_r7001")}
    assert outcome.adopted == ("mine",)
    assert (outcome.sent["mine"].line, outcome.sent["mine"].end_line) == (3, 3), "it sits where the comment sits"


def test_two_findings_in_one_function_keep_a_comment_each(world: World) -> None:
    # Same category and target, so the same fingerprint, but different findings: the second is not the first's twin.
    github = github_for(world)
    world.github.review_comments.append(bot_comment("first", finding("first"), comment_id=7001))
    outcome = github.post_inline([finding("second", line=3)], world.origin.head, tracked={"first", "second"})
    assert world.github.review_posts == 1 and outcome.adopted == ()
    assert "second" in outcome.posted and outcome.posted["second"][0] != 7001


def test_one_existing_comment_is_adopted_by_one_finding_only(world: World) -> None:
    github = github_for(world)
    world.github.review_comments.append(bot_comment("gone", finding("gone"), comment_id=7001))
    found = github.adopt_existing([finding("a"), finding("b")], github.review_comments())
    assert list(found) == ["a"] and found["a"].id == 7001


def test_the_marker_is_written_only_for_a_fingerprint_this_bot_made() -> None:
    body = inline_comment(finding("x"))
    assert body.splitlines()[-2].startswith("<!-- lemoncrow:fingerprint fp-") and body.endswith(
        "<!-- lemoncrow:finding x -->"
    )
    for forged in ("", "fp-short", "fp-0123456789abcdef --> <!-- lemoncrow:finding evil", "x" * 30):
        assert "lemoncrow:fingerprint" not in inline_comment(finding("x").changed(fingerprint=forged))
