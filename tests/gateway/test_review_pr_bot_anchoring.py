"""Where a comment sits (R3): on the line the investigator cited, checked against the Snapshot, else where it always did.

A finding used to carry no line at all, so its comment landed on the first lines its target changed: for a function
with a harmless edit at line 2 and a bug at line 36, on line 2. The investigator now cites the line. The claim is
held to the standard of a suggested replacement (the lines exist, were shown to the model, sit inside one right-side
hunk), and the bot falls back to the first changed lines only when it fails, saying so in the state. A thread cannot
move, so a carried finding keeps the lines its thread is on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.ai_review import PR_INVESTIGATION_SCHEMA
from lemoncrow.pro.capabilities.review.finding_replacement import (
    MAX_CITED_LINES,
    CitedLines,
    parse_cited_lines,
    verify_cited_lines,
)
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding, Located
from lemoncrow.pro.capabilities.review.pr_bot.reconcile import finding_from_confirmed
from lemoncrow.pro.capabilities.review.pr_bot.state import parse_state
from tests.gateway.pr_bot_support import BASE, World, build_world

TITLE = "grants every caller admin rights"


def long_auth(first: str, last: str, *, header: str = "") -> str:
    """A function of about forty lines: ``first`` is line 2 and ``last`` line 36 (one more under a ``header`` line)."""

    filler = "".join(f"    audit_{n} = {n}\n" for n in range(3, 36))
    return f"{header}def authorize(user):\n{first}\n{filler}{last}\n"


BEFORE = long_auth("    role = user.role", "    return user.is_admin")
# A harmless edit at line 2 and the bug at line 36, far enough apart to be two hunks.
AFTER = long_auth("    role = user.role  # normalised", f"    return True  # BUG: {TITLE}")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, {**BASE, "src/auth.py": BEFORE})


def summary(world: World) -> str:
    return str(world.github.summary_comments()[0]["body"])


def only_finding(report: Any) -> BotFinding:
    (found,) = report.findings
    assert isinstance(found, BotFinding)
    return found


# --------------------------------------------------------------------------- #
# the comment sits on the cited line
# --------------------------------------------------------------------------- #


def test_the_comment_sits_on_the_line_the_model_cited_not_on_the_first_changed_line(world: World) -> None:
    world.model.cite_lines.add(TITLE)
    world.origin.push({"src/auth.py": AFTER})
    report = world.review()

    found = only_finding(report)
    assert (found.path, found.line, found.end_line, found.anchor) == ("src/auth.py", 36, 36, "cited")
    (inline,) = world.github.inline()
    assert (inline["path"], inline["line"]) == ("src/auth.py", 36)
    assert "start_line" not in inline  # a single-line comment
    # The summary row, the fix prompt and the hidden state all say line 36, and the state says how it was chosen.
    body = summary(world)
    assert "`src/auth.py:36`" in body and "src/auth.py:36 - " in body
    state = parse_state(body)
    assert state is not None
    assert (state.findings[0].line, state.findings[0].anchor) == (36, "cited")
    assert report.to_wire()["findings"][0]["anchor"] == "cited"


def test_without_a_cited_line_the_comment_falls_back_to_the_first_changed_line_and_the_state_says_so(
    world: World,
) -> None:
    world.origin.push({"src/auth.py": AFTER})
    found = only_finding(world.review())
    assert (found.line, found.anchor) == (2, "target")
    (inline,) = world.github.inline()
    assert inline["line"] == 2
    state = parse_state(summary(world))
    assert state is not None and state.findings[0].anchor == "target"


@pytest.mark.parametrize(
    ("claim", "why"),
    [
        ((20, 20), "a line of the function the pull request did not change, in no hunk"),
        ((900, 900), "a line past the end of the file, which the model was never shown"),
        ((50, 51), "past the end of the file by a few lines"),
        ((36, 30), "a reversed range"),
        ((0, 0), "line zero"),
    ],
)
def test_a_claim_that_fails_its_checks_falls_back_and_never_costs_the_finding(
    world: World, claim: tuple[int, int], why: str
) -> None:
    world.model.cite_lines.add(TITLE)
    world.model.lines[TITLE] = claim
    world.origin.push({"src/auth.py": AFTER})
    report = world.review()

    found = only_finding(report)
    assert (found.line, found.anchor) == (2, "target"), why
    (inline,) = world.github.inline()
    assert inline["line"] == 2


def test_a_long_range_keeps_its_first_lines() -> None:
    assert parse_cited_lines({"start_line": 3, "end_line": 300}) == CitedLines(3, 3 + MAX_CITED_LINES - 1)
    assert parse_cited_lines({"start_line": 3, "end_line": 3 + MAX_CITED_LINES - 1}) == CitedLines(
        3, 3 + MAX_CITED_LINES - 1
    )


def test_a_cited_range_inside_the_diff_covers_those_lines(world: World) -> None:
    world.model.lines[TITLE] = (35, 36)
    world.origin.push({"src/auth.py": AFTER})
    found = only_finding(world.review())
    assert (found.line, found.end_line, found.anchor) == (35, 36, "cited")
    (inline,) = world.github.inline()
    assert (inline["start_line"], inline["line"]) == (35, 36)


def test_github_showing_a_narrower_diff_than_the_snapshot_moves_the_comment_to_the_first_changed_line(
    world: World,
) -> None:
    # The Snapshot's hunks include line 36, GitHub is served a diff that does not: the comment is kept, on the line
    # the target's change begins at, and the state says it was not the cited one.
    world.model.cite_lines.add(TITLE)
    world.origin.push({"src/auth.py": AFTER})
    world.origin.patches = lambda: [  # type: ignore[method-assign]
        {
            "filename": "src/auth.py",
            "patch": "@@ -1,3 +1,3 @@\n def authorize(user):\n-    role = user.role\n+    role = user.role  # normalised",
        }
    ]
    found = only_finding(world.review())
    assert (found.line, found.anchor) == (2, "target")
    (inline,) = world.github.inline()
    assert inline["line"] == 2


def test_a_suggestion_still_decides_the_lines_of_a_new_comment(world: World) -> None:
    world.model.cite_lines.add(TITLE)
    world.model.replacements[TITLE] = {
        "start_line": 36,
        "end_line": 36,
        "original": f"    return True  # BUG: {TITLE}",
        "text": "    return user.is_admin",
    }
    world.origin.push({"src/auth.py": AFTER})
    found = only_finding(world.review())
    assert found.has_suggestion and (found.line, found.end_line) == (36, 36)
    (inline,) = world.github.inline()
    assert inline["line"] == 36 and "```suggestion" in inline["body"]


# --------------------------------------------------------------------------- #
# a carried finding stays where its thread is
# --------------------------------------------------------------------------- #


def test_a_carried_finding_keeps_the_line_of_its_thread_after_an_unrelated_edit_above_it(world: World) -> None:
    world.model.cite_lines.add(TITLE)
    world.origin.push({"src/auth.py": AFTER})
    first = only_finding(world.review())
    thread = first.thread_id
    assert (first.line, first.thread_id > 0) == (36, True)

    # An unrelated line is added at the top of the file: the bug is now on line 37, and the file is re-asked.
    world.model.reset()
    world.origin.push({"src/auth.py": long_auth(*AFTER_PARTS, header="import os\n")}, "an unrelated edit above")
    again = world.review()

    assert world.model.calls, "the changed file is asked about again"
    kept = only_finding(again)
    assert (kept.state, kept.thread_id) == ("carried", thread)
    # The thread is on line 36 and cannot move; the summary must not point anywhere else.
    assert (kept.line, kept.end_line, kept.anchor) == (36, 36, "cited")
    assert "`src/auth.py:36`" in summary(world) and "`src/auth.py:37`" not in summary(world)
    assert world.github.review_posts == 1 and len(world.github.inline()) == 1
    assert world.github.inline()[0]["line"] == 36


def test_a_finding_with_no_cited_line_also_stays_on_its_thread_when_the_target_moves(world: World) -> None:
    """Without a cited line the re-asked finding is placed on the target's new first changed line; the thread stays."""

    world.origin.push({"src/auth.py": AFTER})
    first = only_finding(world.review())
    assert first.line == 2
    world.origin.push({"src/auth.py": long_auth(*AFTER_PARTS, header="import os\n")}, "an unrelated edit above")
    kept = only_finding(world.review())
    assert (kept.thread_id, kept.line) == (first.thread_id, 2), "still on the thread's line, not the target's new one"


AFTER_PARTS = ("    role = user.role  # normalised", f"    return True  # BUG: {TITLE}")


# --------------------------------------------------------------------------- #
# the claim, on its own
# --------------------------------------------------------------------------- #

SOURCE = "".join(f"line {number}\n" for number in range(1, 41))
HUNKS = "@@ -10,3 +10,5 @@\n@@ -30,2 +32,6 @@"  # new side: lines 10-14 and 32-37


def test_the_pull_request_schema_asks_for_nullable_lines_next_to_the_replacement() -> None:
    properties = PR_INVESTIGATION_SCHEMA["properties"]
    assert {"type": "null"} in properties["lines"]["anyOf"]
    object_schema = next(row for row in properties["lines"]["anyOf"] if row.get("type") == "object")
    assert object_schema["required"] == ["start_line", "end_line"] and object_schema["additionalProperties"] is False
    assert "lines" in PR_INVESTIGATION_SCHEMA["required"]
    assert set(PR_INVESTIGATION_SCHEMA["required"]) == set(properties)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "36",
        {},
        {"start_line": 5},
        {"start_line": 0, "end_line": 3},
        {"start_line": 9, "end_line": 4},
        {"start_line": "a", "end_line": 3},
    ],
)
def test_a_malformed_claim_reads_as_no_lines(raw: object) -> None:
    assert parse_cited_lines(raw) is None


def test_a_claim_is_parsed_but_not_yet_trusted() -> None:
    claim = parse_cited_lines({"start_line": 33, "end_line": 34})
    assert claim == CitedLines(33, 34) and not claim.verified


@pytest.mark.parametrize(
    ("start", "end", "shown", "ok"),
    [
        (33, 34, (1, 40), True),  # inside the hunk of lines 32-37 and the excerpt
        (12, 12, (1, 40), True),
        (15, 16, (1, 40), False),  # between the hunks
        (36, 38, (1, 40), False),  # straddles the end of a hunk
        (33, 34, (1, 20), False),  # the model was never shown these lines
        (41, 41, (1, 60), False),  # past the end of the file
    ],
)
def test_cited_lines_must_exist_have_been_shown_and_sit_in_one_hunk(
    start: int, end: int, shown: tuple[int, int], ok: bool
) -> None:
    checked = verify_cited_lines(CitedLines(start, end), source=SOURCE, hunks=HUNKS, shown=shown)
    assert (checked is not None and checked.verified) is ok


def test_blank_lines_and_ambiguous_line_numbers_are_not_a_place_to_comment() -> None:
    blank = "a\n\n\n\nb\n"
    assert verify_cited_lines(CitedLines(2, 3), source=blank, hunks="@@ -1,5 +1,5 @@", shown=(1, 5)) is None
    crlf = "a\r\nb\r\nc\r\n"
    assert verify_cited_lines(CitedLines(2, 2), source=crlf, hunks="@@ -1,3 +1,3 @@", shown=(1, 3)) is None


def test_only_checked_lines_place_a_comment() -> None:
    from types import SimpleNamespace

    base = {
        "id": "f",
        "category": "security",
        "target_ids": ("t",),
        "title": "t",
        "explanation": "e",
        "consequence": "c",
        "rule_refs": (),
    }
    where = Located("src/auth.py", 2, 3, "new")
    claimed = finding_from_confirmed(SimpleNamespace(**base, lines=CitedLines(36, 36)), where, "abcdef0")
    assert (claimed.line, claimed.anchor) == (2, "target"), "an unchecked claim places nothing"
    checked = finding_from_confirmed(SimpleNamespace(**base, lines=CitedLines(36, 36, True)), where, "abcdef0")
    assert (checked.line, checked.end_line, checked.anchor, checked.target_line) == (36, 36, "cited", 2)
    old_side = finding_from_confirmed(
        SimpleNamespace(**base, lines=CitedLines(36, 36, True)), Located("src/auth.py", 2, 3, "old"), "abcdef0"
    )
    assert (old_side.line, old_side.anchor) == (2, "target"), "lines are new-side numbers"
    nowhere = finding_from_confirmed(SimpleNamespace(**base), None, "abcdef0")
    assert (nowhere.line, nowhere.anchor) == (0, "")


def test_anchor_and_baseline_survive_the_hidden_state() -> None:
    from lemoncrow.pro.capabilities.review.pr_bot.findings import finding_from_wire

    item = BotFinding(
        id="f", fingerprint="fp", severity="p1", category="security", state="new", path="a.py", line=36,
        end_line=36, anchor="cited", target_line=2,
    )  # fmt: skip
    wire = item.to_wire()
    assert (wire["anchor"], wire["target_line"]) == ("cited", 2)
    back = finding_from_wire(wire)
    assert back is not None and (back.anchor, back.target_line) == ("cited", 2)
    # A state written before the field existed still reads, and an invented anchor reads as none.
    for key in ("anchor", "target_line"):
        wire.pop(key)
    old = finding_from_wire(wire)
    assert old is not None and (old.anchor, old.target_line) == ("", 0)
    assert finding_from_wire({**wire, "anchor": "somewhere"}).anchor == ""  # type: ignore[union-attr]
