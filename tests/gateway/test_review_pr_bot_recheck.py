"""A finding is "Fixed" only when the model says so and its own lines changed (R4).

A changed target used to be re-asked as a fresh, blind review. If the model did not report the finding again, the
bot declared it fixed, replied "Fixed in <sha>" and resolved the thread: a harmless edit elsewhere in the same file,
or a model that does not repeat itself, closed a conversation about code that still had the bug. Now the re-check
shows the model the earlier findings and asks about each by id; "fixed" needs its affirmative answer and a change to
the lines the finding cited; anything else is "no longer reported", and the thread is left open for a person.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.ai_review import (
    _PR_FIELD_RULES,
    INVESTIGATION_SCHEMA,
    PR_INVESTIGATION_SCHEMA,
    _merge_answers,
    _prior_answers,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import IntelligenceRunOutcome, ModelCallUsage
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from tests.gateway.pr_bot_support import BASE, World, build_world
from tests.gateway.test_review_pr_bot_anchoring import AFTER, BEFORE, TITLE, long_auth

#: The same function with its bug fixed, and the bug line itself unchanged but another line edited.
FIXED = long_auth("    role = user.role  # normalised", "    return bool(user.is_admin)")
EDITED_ELSEWHERE = AFTER.replace("# normalised", "# normalised, again")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    built = build_world(tmp_path, monkeypatch, {**BASE, "src/auth.py": BEFORE})
    built.model.cite_lines.add(TITLE)  # a careful model: it names the line, so a fix can be recognised
    return built


def first_review(world: World) -> BotFinding:
    world.origin.push({"src/auth.py": AFTER})
    (found,) = world.review().findings
    assert (found.line, found.anchor, found.anchor_digest != "") == (36, "cited", True)
    return found


def summary(world: World) -> str:
    return str(world.github.summary_comments()[0]["body"])


def only(findings: tuple[BotFinding, ...]) -> BotFinding:
    (found,) = findings
    return found


# --------------------------------------------------------------------------- #
# the re-check shows what was found before
# --------------------------------------------------------------------------- #


def test_the_re_check_prompt_carries_the_earlier_finding_of_the_changed_target_and_asks_per_id(world: World) -> None:
    found = first_review(world)
    assert world.model.prior_seen == [], "a first review has nothing to re-check"

    world.origin.push({"src/auth.py": EDITED_ELSEWHERE}, "edit another line")
    world.review()
    (rows,) = world.model.prior_seen
    (row,) = rows
    assert (row["id"], row["title"], row["category"]) == (found.id, TITLE, found.category)
    assert row["target_ids"] == list(found.target_ids)


def test_a_target_that_did_not_change_is_not_re_asked_so_its_finding_is_carried_untouched(world: World) -> None:
    first_review(world)
    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 5\n"}, "touch another file")
    kept = only(world.review().findings)
    assert kept.state == "carried" and world.model.paths() == [] and world.model.prior_seen == []


# --------------------------------------------------------------------------- #
# not reported again is not fixed
# --------------------------------------------------------------------------- #


def test_an_unrelated_edit_in_the_same_file_does_not_resolve_the_thread(world: World) -> None:
    found = first_review(world)
    (thread,) = world.github.inline()
    # The file is re-asked after an edit on another line; this time the model does not report the bug again, and
    # does not say it is fixed either. The bug line is exactly as it was.
    world.model.forget.add(TITLE)
    world.model.answer_prior = False
    world.origin.push({"src/auth.py": EDITED_ELSEWHERE}, "edit another line")
    report = world.review(fail_on="p0")

    kept = only(report.findings)
    assert (kept.id, kept.state, kept.is_open) == (found.id, "unreported", False)
    (reply,) = world.github.replies()
    assert reply["in_reply_to_id"] == thread["id"]
    assert reply["body"].startswith(f"No longer reported in {world.origin.head[:7]}")
    assert "Fixed" not in reply["body"]
    assert world.github.threads[thread["id"]]["resolved"] is False, "nothing showed it was fixed; a person decides"
    body = summary(world)
    assert "1 finding no longer reported, not confirmed fixed" in body and "· 1 no longer reported" in body
    assert "finding fixed since last review" not in body and "~~" not in body
    assert report.status is not None and report.status[0] == "success"


def test_a_model_that_says_resolved_about_lines_that_did_not_change_is_not_believed(world: World) -> None:
    first_review(world)
    world.model.forget.add(TITLE)
    world.model.prior[TITLE] = "resolved"  # the model claims a fix, but the bug line is word for word the same
    world.origin.push({"src/auth.py": EDITED_ELSEWHERE}, "edit another line")
    kept = only(world.review().findings)
    assert kept.state == "unreported"
    assert world.github.replies()[0]["body"].startswith("No longer reported in ")
    assert all(not thread["resolved"] for thread in world.github.threads.values())


def test_a_model_that_says_the_bug_is_still_there_keeps_the_finding_open(world: World) -> None:
    first_review(world)
    world.model.forget.add(TITLE)  # it will not re-report, but it will answer the question it was asked
    world.origin.push({"src/auth.py": EDITED_ELSEWHERE}, "edit another line")
    kept = only(world.review().findings)
    assert (kept.state, kept.is_open) == ("carried", True)
    assert world.github.replies() == []


def test_a_fix_on_the_cited_lines_with_the_models_word_resolves_the_thread(world: World) -> None:
    found = first_review(world)
    (thread,) = world.github.inline()
    world.origin.push({"src/auth.py": FIXED}, "fix the bug on line 36")
    report = world.review()

    kept = only(report.findings)
    assert (kept.id, kept.state, kept.resolved_in) == (found.id, "fixed", world.origin.head[:7])
    (reply,) = world.github.replies()
    assert reply["body"].startswith(f"Fixed in {world.origin.head[:7]}.")
    assert world.github.threads[thread["id"]]["resolved"] is True
    assert "1 finding fixed since last review" in summary(world)


def test_a_fix_is_found_wherever_the_cited_lines_moved_to(world: World) -> None:
    """An edit above shifts every line number; the cited text is looked for, not its old position."""

    first_review(world)
    shifted = AFTER.replace("def authorize", "import os\n\n\ndef authorize")
    world.origin.push({"src/auth.py": shifted}, "an unrelated edit above, bug still there")
    kept = only(world.review().findings)
    assert (kept.state, kept.line) == (
        "carried",
        36,
    ), "re-reported or answered, but never closed, and still on its thread"

    fixed = shifted.replace(f"    return True  # BUG: {TITLE}", "    return bool(user.is_admin)")
    world.origin.push({"src/auth.py": fixed}, "fix it, three lines lower than before")
    assert only(world.review().findings).state == "fixed"


def test_without_a_checked_cited_line_nothing_is_ever_auto_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, {**BASE, "src/auth.py": BEFORE})  # this model cites no line
    world.origin.push({"src/auth.py": AFTER})
    found = only(world.review().findings)
    assert found.anchor == "target" and found.anchor_digest == ""
    world.origin.push({"src/auth.py": FIXED}, "fix the bug")
    kept = only(world.review().findings)
    assert kept.state == "unreported", "the model said resolved, but there is no cited line to see change"


# --------------------------------------------------------------------------- #
# what it costs a person, and what comes back
# --------------------------------------------------------------------------- #


def test_an_unreported_finding_is_answered_once_and_comes_back_on_its_own_thread(world: World) -> None:
    found = first_review(world)
    world.model.forget.add(TITLE)
    world.model.answer_prior = False
    world.origin.push({"src/auth.py": EDITED_ELSEWHERE}, "edit another line")
    world.review()
    world.origin.push({"src/plain.py": "VALUE = 7\n"}, "touch another file")
    world.review()
    assert len(world.github.replies()) == 1, "one reply, however many later runs list it"

    # The model finds it again on a later push: the same finding, on the same thread, not a second comment.
    world.model.forget.clear()
    world.model.answer_prior = True
    world.origin.push({"src/auth.py": EDITED_ELSEWHERE.replace("again", "once more")}, "edit again")
    back = only(world.review().findings)
    assert (back.id, back.state, back.thread_id) == (found.id, "carried", found.thread_id)
    assert len(world.github.inline()) == 1 and world.github.review_posts == 1


def test_a_failed_re_check_still_leaves_the_finding_open_and_unverified(world: World) -> None:
    first_review(world)
    world.model.fail = True
    world.origin.push({"src/auth.py": FIXED}, "fix the bug")
    kept = only(world.review().findings)
    assert (kept.state, kept.unverified) == ("carried", True)
    assert world.github.replies() == []


def test_a_push_the_triage_calls_trivial_asks_nobody_so_nothing_is_called_fixed(world: World) -> None:
    first_review(world)
    world.model.reset()
    # The bug line goes back to what the base had; what remains of the change is a comment. No risk signal is left,
    # so no model is asked, and "no model said it is still there" must not read as "fixed".
    world.origin.push(
        {"src/auth.py": long_auth("    role = user.role  # normalised", "    return user.is_admin")}, "revert"
    )
    kept = only(world.review().findings)
    assert kept.state == "unreported" and world.model.calls == []
    assert world.github.replies()[0]["body"].startswith("No longer reported in ")


# --------------------------------------------------------------------------- #
# the question, and how its answer is read
# --------------------------------------------------------------------------- #


def outcome(result: dict[str, object], status: str = "ready") -> IntelligenceRunOutcome:
    return IntelligenceRunOutcome(
        run_id="run-1",
        status=status,  # type: ignore[arg-type]
        result=result,
        driver="openai",
        model="m",
        usage=ModelCallUsage(calls=1),
        provider_calls_this_request=1,
        duration_ms=1,
        cache_hit=False,
    )


ASKED = [{"id": "f1", "category": "security", "target_ids": ["t1"], "title": "A"}, {"id": "f2"}]


def test_the_pull_request_schema_asks_for_an_answer_per_earlier_finding_and_the_readers_does_not() -> None:
    item = PR_INVESTIGATION_SCHEMA["properties"]["prior_findings"]["items"]
    assert item["properties"]["status"]["enum"] == ["still_present", "resolved", "unclear"]
    assert item["required"] == ["id", "status"] and item["additionalProperties"] is False
    assert "prior_findings" in PR_INVESTIGATION_SCHEMA["required"]
    assert set(PR_INVESTIGATION_SCHEMA["required"]) == set(PR_INVESTIGATION_SCHEMA["properties"])
    assert "prior_findings" not in INVESTIGATION_SCHEMA["properties"]
    assert "prior_findings" in _PR_FIELD_RULES and "resolved" in _PR_FIELD_RULES


def test_only_asked_ids_and_known_statuses_are_read_as_answers() -> None:
    rows = [
        {"id": "f1", "status": "resolved"},
        {"id": "f2", "status": "fixed"},  # not in the vocabulary
        {"id": "other", "status": "resolved"},  # never asked about
        "f1",
        {"id": 3, "status": "resolved"},
    ]
    assert _prior_answers(outcome({"prior_findings": rows}), ASKED) == {"f1": "resolved"}
    assert _prior_answers(outcome({"prior_findings": "resolved"}), ASKED) == {}
    assert _prior_answers(outcome({}), ASKED) == {}
    assert _prior_answers(outcome({"prior_findings": rows}), []) == {}
    # A call that failed answered nothing, which is never "resolved".
    assert _prior_answers(outcome({"prior_findings": rows}, status="failed"), ASKED) == {}


def test_resolved_needs_every_investigation_that_was_asked_to_agree() -> None:
    assert _merge_answers([{"f1": "resolved"}, {"f1": "resolved"}]) == {"f1": "resolved"}
    assert _merge_answers([{"f1": "resolved"}, {"f1": "unclear"}]) == {"f1": "unclear"}
    assert _merge_answers([{"f1": "unclear"}, {"f1": "still_present"}, {"f1": "resolved"}]) == {"f1": "still_present"}
    assert _merge_answers([]) == {}
