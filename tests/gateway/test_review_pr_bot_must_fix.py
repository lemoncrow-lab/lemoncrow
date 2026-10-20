"""A pull request's author cannot dismiss a must-fix on their own pull request (G9).

With ``fail-on: p0`` a P0 is what blocks the merge, so whoever may dismiss it holds the key to the gate. Feedback from
the author (a thumbs-down, a "won't fix" reply, a resolved thread, an ``ignore`` command), from the bot's own account
and from anyone below owner, member or collaborator is recorded and answered in the thread, and leaves the P0 and the
check as they were. A different owner, member or collaborator dismisses it as they would any finding, and the summary
names them. P1 and P2 are as they were: their author may suppress them.

Same world as the other bot tests: a real git origin, a fake GitHub and a fake model. ``alice`` opened the pull request
(she is a ``MEMBER``); ``maria`` is a maintainer with write access, ``carol`` an outside ``CONTRIBUTOR``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import learning
from lemoncrow.pro.capabilities.review.pr_bot import suppression as sup
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding, fingerprint_for
from lemoncrow.pro.capabilities.review.pr_bot.github import FeedbackThread, Reaction, ThreadComment
from lemoncrow.pro.capabilities.review.pr_bot.state import parse_state
from tests.gateway.pr_bot_support import AUTH_BUGGY, BASE, TOKEN_BUGGY, World, build_world

DOWN, UP = "THUMBS_DOWN", "THUMBS_UP"
HANDLER = 'from auth import authorize\n\n\ndef handle(request):\n    return "ok" if authorize(request.user) else "no"\n'
FILES = {**BASE, "src/handlers.py": HANDLER, "src/legacy/token.py": "def verify(token):\n    return True\n"}
PUSH = {"src/auth.py": AUTH_BUGGY, "src/legacy/token.py": TOKEN_BUGGY, "src/plain.py": "VALUE = 2\n"}
AUTH_TITLE = "lets a missing principal through"
TOKEN_TITLE = "compares secrets without a constant-time check"
NOTICE = "A must-fix on your own pull request needs a reviewer other than the author to dismiss it."
OTHERS_ONLY = (
    "A must-fix can be dismissed only by an owner, member or collaborator other than the pull request's author."
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """The auth finding is a P0 (a verified high-impact security finding, corroborated by its caller in
    ``src/handlers.py``); the token finding is whatever *the test* rates it, a P1 unless said otherwise."""

    made = build_world(tmp_path, monkeypatch, FILES)
    made.model.severity[AUTH_TITLE] = "high"
    made.model.cite_context.add(AUTH_TITLE)
    made.github.permissions["maria"] = "write"  # reacts without ever commenting: looked up as a collaborator
    made.github.associations["maria"] = "COLLABORATOR"  # and, when she comments, GitHub says the same
    made.github.associations["carol"] = "CONTRIBUTOR"
    return made


def reviewed(world: World, **options: Any) -> tuple[int, int]:
    """Push the change and review it; returns the ids of the threads on the auth (P0) and the token findings (0 for a
    finding too minor to get an inline comment)."""

    world.origin.push(PUSH)
    world.review(**options)
    world.model.reset()
    by_path = {row["path"]: int(row["id"]) for row in world.github.inline()}
    return by_path["src/auth.py"], by_path.get("src/legacy/token.py", 0)


def state_of(world: World) -> Any:
    return parse_state(summary_of(world))


def summary_of(world: World) -> str:
    return str(world.github.summary_comments()[0]["body"])


def push_noise(world: World, value: int = 3) -> None:
    world.origin.push({"src/plain.py": f"VALUE = {value}\n"})


def declined(world: World, thread: int) -> list[dict[str, Any]]:
    return [
        row
        for row in world.github.bot_replies()
        if "<!-- lemoncrow:declined " in row["body"] and row["in_reply_to_id"] == thread
    ]


def learned(world: World, thread: int) -> list[dict[str, Any]]:
    return [
        row
        for row in world.github.bot_replies()
        if "<!-- lemoncrow:learned " in row["body"] and row["in_reply_to_id"] == thread
    ]


def by_title(findings: Any, title: str) -> BotFinding:
    return next(f for f in findings if f.title == title)


def the_p0_stands(world: World, report: Any) -> None:
    """The P0 is open and counted, and the check is red, whatever was said."""

    p0 = by_title(report.findings, AUTH_TITLE)
    assert p0.severity == "p0" and p0.is_open and p0.suppressed_by == ""
    assert report.verdict is not None and report.verdict.p0 == 1 and report.verdict.state == "must_fix"
    assert report.status is not None and report.status[0] == "failure"
    assert world.github.last_status()["state"] == "failure"


# --------------------------------------------------------------------------- #
# the author's feedback on their own P0
# --------------------------------------------------------------------------- #


def thumbs_down(world: World, thread: int) -> None:
    world.github.react(thread, DOWN, "alice")


def wont_fix(world: World, thread: int) -> None:
    world.github.add_review_comment("won't fix, legacy path", in_reply_to=thread)


def resolve(world: World, thread: int) -> None:
    world.github.resolve(thread, "alice")


@pytest.mark.parametrize(
    ("give", "how"),
    [
        (thumbs_down, "thumbs-down from `alice`"),
        (wont_fix, "reply from `alice`: intended or an accepted risk"),
        (resolve, "thread resolved by `alice` with the code unchanged"),
    ],
    ids=["thumbs-down", "wont-fix-reply", "resolved-thread"],
)
def test_an_authors_feedback_on_their_own_p0_is_recorded_and_answered_and_changes_nothing_under_fail_on_p0(
    world: World, give: Any, how: str
) -> None:
    auth, token = reviewed(world, fail_on="p0")
    assert world.github.last_status()["state"] == "failure"
    inline, posts = len(world.github.inline()), world.github.review_posts

    give(world, auth)
    push_noise(world)
    report = world.review(fail_on="p0")

    the_p0_stands(world, report)
    assert by_title(report.findings, TOKEN_TITLE).is_open
    assert len(world.github.inline()) == inline and world.github.review_posts == posts
    # The thread is told, once, and not with the "learned" confirmation.
    (told,) = declined(world, auth)
    assert told["body"].startswith(NOTICE)
    assert f"Recorded: {how}." in told["body"] and "The P0 and the check stay as they are" in told["body"]
    assert re.search(r"<!-- lemoncrow:declined sup-[0-9a-f]{10} -->", told["body"])
    assert learned(world, auth) == [] and declined(world, token) == []
    # It is recorded: in the hidden state, who said it and that it cannot dismiss a must-fix.
    (row,) = state_of(world).suppressions
    assert row.by == "alice" and row.dismisses_p0 is False and row.thread_id == auth
    assert row.source in {"reaction", "reply", "resolved"}
    # And the summary says so, rather than counting a dismissal.
    body = summary_of(world)
    assert "dismissed by feedback" not in body, "nothing was dismissed"
    assert "Suggested rules from feedback" not in body, "a dismissal that did not happen is not offered as a rule"
    assert "a must-fix (P0) it covers stays open" in body and "`alice`" in body
    assert body.split("\n")[1].startswith("**1 must-fix before merge**")
    assert report.published["declined"] == 1

    # Saying it again, or on a later push, does not get the same answer twice, and the P0 is still there.
    world.github.react(auth, DOWN, "alice")
    push_noise(world, 4)
    again = world.review(fail_on="p0")
    the_p0_stands(world, again)
    assert len(declined(world, auth)) == 1
    assert len(state_of(world).suppressions) == 1


def test_an_authors_thumbs_down_does_not_change_the_status_without_a_new_push_either(world: World) -> None:
    """A re-run of the same head reads the reaction too: the check does not turn green."""

    auth, _ = reviewed(world, fail_on="p0")
    thumbs_down(world, auth)
    report = world.review(fail_on="p0")
    the_p0_stands(world, report)
    assert len(declined(world, auth)) == 1


def test_a_thumbs_up_from_the_author_is_still_only_a_thumbs_up(world: World) -> None:
    auth, _ = reviewed(world, fail_on="p0")
    world.github.react(auth, UP, "alice")
    push_noise(world)
    report = world.review(fail_on="p0")
    the_p0_stands(world, report)
    assert by_title(report.findings, AUTH_TITLE).confirmed is True
    assert declined(world, auth) == [] and state_of(world).suppressions == ()


# --------------------------------------------------------------------------- #
# someone else dismisses it
# --------------------------------------------------------------------------- #


def test_a_maintainers_thumbs_down_on_the_same_p0_suppresses_it_and_the_summary_names_them(world: World) -> None:
    auth, _ = reviewed(world, fail_on="p0")
    world.github.react(auth, DOWN, "maria")
    push_noise(world)

    report = world.review(fail_on="p0")

    held = by_title(report.findings, AUTH_TITLE)
    assert held.severity == "p0" and held.state == "suppressed" and held.suppressed_by.startswith("sup-")
    assert report.verdict is not None and report.verdict.p0 == 0
    assert report.status is not None and report.status[0] == "success"
    assert world.github.last_status()["state"] == "success"
    body = summary_of(world)
    assert "1 finding dismissed by feedback" in body
    dismissed = next(
        line for line in body.splitlines() if "lets a missing principal through" in line and "thumbs-down" in line
    )
    assert "**P0**" in dismissed and "thumbs-down from `maria`" in dismissed
    (row,) = state_of(world).suppressions
    assert (row.by, row.source, row.dismisses_p0) == ("maria", "reaction", True)
    (told,) = learned(world, auth)
    assert told["body"].startswith("Learned: won't flag") and "`maria`" in told["body"]
    assert declined(world, auth) == []


def test_the_authors_thumbs_down_does_not_shadow_a_maintainers_reply_on_the_same_p0(world: World) -> None:
    auth, _ = reviewed(world, fail_on="p0")
    world.github.react(auth, DOWN, "alice")  # first in line, and not heard on a P0
    world.github.add_review_comment(
        "Not a bug: callers never pass None.", in_reply_to=auth, user={"login": "maria", "type": "User"}
    )
    push_noise(world)

    report = world.review(fail_on="p0")

    assert by_title(report.findings, AUTH_TITLE).state == "suppressed"
    assert report.status is not None and report.status[0] == "success"
    (row,) = state_of(world).suppressions
    assert (row.by, row.source, row.reason, row.dismisses_p0) == ("maria", "reply", "incorrect", True)
    assert "callers never pass None" in row.note, "their words, not the author's"
    assert declined(world, auth) == [], "someone who may dismiss it did, so nothing was refused"


def test_a_maintainer_who_comes_after_the_authors_refused_thumbs_down_dismisses_it_for_good(world: World) -> None:
    auth, _ = reviewed(world, fail_on="p0")
    world.github.react(auth, DOWN, "alice")
    push_noise(world)
    the_p0_stands(world, world.review(fail_on="p0"))
    assert len(declined(world, auth)) == 1

    world.github.react(auth, DOWN, "maria")
    push_noise(world, 4)
    report = world.review(fail_on="p0")

    assert by_title(report.findings, AUTH_TITLE).state == "suppressed"
    assert report.status is not None and report.status[0] == "success"
    (row,) = state_of(world).suppressions
    assert (row.by, row.dismisses_p0) == ("maria", True), "the same instruction, now by someone who may give it"
    assert len(learned(world, auth)) == 1 and len(declined(world, auth)) == 1

    # The concern comes back on a later push: still held back, by the maintainer's instruction.
    world.origin.push({"src/auth.py": AUTH_BUGGY.replace(AUTH_TITLE, "accepts a null principal")})
    world.model.severity["accepts a null principal"] = "high"
    world.model.cite_context.add("accepts a null principal")
    later = world.review(fail_on="p0")
    assert by_title(later.findings, "accepts a null principal").state == "suppressed"
    assert later.status is not None and later.status[0] == "success"


# --------------------------------------------------------------------------- #
# below collaborator
# --------------------------------------------------------------------------- #


def test_a_contributor_teaches_the_bot_about_p1_but_cannot_dismiss_a_p0(world: World) -> None:
    """CONTRIBUTOR is on the feedback allow-list by default and deliberately not on the must-fix one: it means "has a
    commit here", not "can merge"."""

    auth, token = reviewed(world, fail_on="p0")
    world.github.add_review_comment("I agree.", in_reply_to=auth, user={"login": "carol", "type": "User"})
    world.github.react(auth, DOWN, "carol")
    world.github.react(token, DOWN, "carol")
    push_noise(world)

    report = world.review(fail_on="p0")

    the_p0_stands(world, report)
    assert by_title(report.findings, TOKEN_TITLE).state == "suppressed", "a P1 is still hers to dismiss"
    (told,) = declined(world, auth)
    assert told["body"].startswith(OTHERS_ONLY) and "your own pull request" not in told["body"]
    assert "`carol`" in told["body"]
    assert {row.by: row.dismisses_p0 for row in state_of(world).suppressions} == {"carol": False}


@pytest.mark.parametrize(
    ("login", "association", "author", "kind", "may"),
    [
        ("maria", "COLLABORATOR", "alice", "", True),
        ("maria", "owner", "alice", "", True),
        ("maria", "MEMBER", "alice", "User", True),
        ("carol", "CONTRIBUTOR", "alice", "", False),
        ("carol", "FIRST_TIME_CONTRIBUTOR", "alice", "", False),
        ("nobody", "NONE", "alice", "", False),
        ("alice", "OWNER", "alice", "", False),
        ("Alice", "OWNER", "alice", "", False),
        ("maria", "OWNER", "", "", False),
        ("github-actions[bot]", "MEMBER", "alice", "Bot", False),
        ("x", "MEMBER", "alice", "Bot", False),
        ("", "MEMBER", "alice", "", False),
    ],
)
def test_who_may_dismiss_a_must_fix(login: str, association: str, author: str, kind: str, may: bool) -> None:
    assert learning.may_dismiss_must_fix(login, association=association, author=author, kind=kind) is may


# --------------------------------------------------------------------------- #
# the bot's own account
# --------------------------------------------------------------------------- #


def p0_finding(**fields: Any) -> BotFinding:
    values: dict[str, Any] = {
        "id": "f-1",
        "fingerprint": fingerprint_for("security", ("t1",)),
        "severity": "p0",
        "category": "security",
        "state": "new",
        "path": "src/auth.py",
        "line": 2,
        "end_line": 2,
        "target_ids": ("t1",),
        "title": AUTH_TITLE,
        "impact": "high",
        "verified": True,
        "thread_id": 11,
        "placement": "inline",
    }
    values.update(fields)
    return BotFinding(**values)


def thread_with(
    *reactions: Reaction, root_login: str = "github-actions[bot]", root_kind: str = "Bot"
) -> FeedbackThread:
    root = ThreadComment(11, "**P0 · must fix**", root_login, root_kind, "NONE", tuple(reactions))
    return FeedbackThread(11, False, "", "", (root,))


def people(known: dict[str, str]) -> learning.People:
    return learning.People(
        {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"},
        known,
        owner="acme",
        lookup=lambda _login: "",
        author="alice",
    )


def test_the_account_that_wrote_the_comment_is_the_bots_even_when_it_looks_like_a_person() -> None:
    """A workflow that comments with a personal access token posts as a user, not as ``...[bot]``: the account on the
    root comment is the bot's, and its own thumbs-down never dismisses a must-fix."""

    thread = thread_with(Reaction(DOWN, "review-bot"), root_login="review-bot", root_kind="User")
    finding = p0_finding()
    (signal,) = learning.read_signals(
        [thread], [finding], people({"review-bot": "MEMBER"}), code_changed=lambda _finding: False
    )
    assert (signal.kind, signal.by, signal.dismisses_p0) == ("down", "review-bot", False)
    assert learning.suppression_from(signal, "abc1234").declines(finding)


def test_an_app_or_a_bot_login_is_never_heard_at_all() -> None:
    thread = thread_with(Reaction(DOWN, "dependabot[bot]"), Reaction(DOWN, ""))
    assert learning.read_signals([thread], [p0_finding()], people({}), code_changed=lambda _finding: False) == []


def test_a_maintainer_is_heard_before_the_author_on_a_p0_and_the_author_alone_is_recorded_not_heard() -> None:
    both = thread_with(Reaction(DOWN, "alice"), Reaction(DOWN, "maria"))
    known = {"alice": "MEMBER", "maria": "COLLABORATOR"}
    (heard,) = learning.read_signals([both], [p0_finding()], people(known), code_changed=lambda _finding: False)
    assert (heard.by, heard.dismisses_p0) == ("maria", True)
    (recorded,) = learning.read_signals(
        [thread_with(Reaction(DOWN, "alice"))], [p0_finding()], people(known), code_changed=lambda _finding: False
    )
    assert (recorded.by, recorded.dismisses_p0) == ("alice", False)


# --------------------------------------------------------------------------- #
# what a suppression may hold back
# --------------------------------------------------------------------------- #


def test_a_suppression_holds_back_a_p0_only_when_its_giver_may_dismiss_one() -> None:
    p0, p1 = p0_finding(), p0_finding(severity="p1")
    from_author = sup.make_suppression("reaction", fingerprint=p0.fingerprint, by="alice")
    from_maintainer = sup.make_suppression("reaction", fingerprint=p0.fingerprint, by="maria", dismisses_p0=True)
    assert not from_author.matches(p0) and from_author.declines(p0) and from_author.matches(p1)
    assert from_maintainer.matches(p0) and not from_maintainer.declines(p0) and from_maintainer.matches(p1)
    assert [f.state for f in sup.apply([p0, p1], [from_author], "abc1234")] == ["new", "suppressed"]
    assert [f.state for f in sup.apply([p0, p1], [from_maintainer], "abc1234")] == ["suppressed", "suppressed"]


def test_a_blanket_instruction_from_the_author_covers_every_finding_but_the_must_fix() -> None:
    by_category = sup.make_suppression("command", category="security", by="alice")
    by_words = sup.make_suppression("command", pattern="missing principal", by="alice")
    p0, p2 = p0_finding(), p0_finding(id="f-2", fingerprint="fp-ffffffffffffffff", severity="p2")
    for row in (by_category, by_words):
        assert row.matches(p2) and not row.matches(p0) and row.declines(p0) and not row.declines(p2)
    # A rule violation is protected for everyone, whatever else is true.
    rule = p0_finding(category="critical_rule")
    assert not sup.make_suppression("command", category="critical_rule", dismisses_p0=True).matches(rule)
    assert not sup.make_suppression("reaction", fingerprint=rule.fingerprint, dismisses_p0=True).declines(rule)


def test_the_flag_is_kept_in_the_state_and_a_row_without_it_cannot_hold_back_a_must_fix() -> None:
    row = sup.make_suppression("reaction", fingerprint="fp-0123456789abcdef", by="maria", dismisses_p0=True)
    wire = row.to_wire()
    assert wire["p0"] is True
    assert sup.suppression_from_wire(wire) == row
    assert "p0" not in sup.make_suppression("reaction", fingerprint="fp-0123456789abcdef").to_wire()
    older = {k: v for k, v in wire.items() if k != "p0"}
    legacy = sup.suppression_from_wire(older)
    assert legacy is not None and legacy.dismisses_p0 is False
    for truthy in ("true", 1, "yes"):
        forged = sup.suppression_from_wire({**wire, "p0": truthy})
        assert forged is not None and forged.dismisses_p0 is False, "only a JSON true counts"


def test_the_same_instruction_from_someone_who_may_replaces_the_one_from_someone_who_may_not() -> None:
    first = sup.make_suppression("command", category="security", by="alice")
    second = sup.make_suppression("command", category="security", by="maria", dismisses_p0=True)
    assert first.id == second.id
    assert sup.merge([first], [second]) == (second,)
    assert sup.merge([second], [first]) == (second,), "and not the other way round"
    assert sup.merge([first], [first]) == (first,)
    other = sup.make_suppression("command", category="performance", by="bob")
    assert sup.merge([first, other], [second]) == (second, other), "it keeps its place"


# --------------------------------------------------------------------------- #
# `@lemoncrow ignore`
# --------------------------------------------------------------------------- #


def test_an_authors_ignore_command_covering_a_p0_is_refused_for_that_p0(world: World) -> None:
    reviewed(world, fail_on="p0")
    inline, posts = len(world.github.inline()), world.github.review_posts

    report = world.chat("@lemoncrow ignore security", fail_on="p0")

    assert report.published["chat"]["kind"] == "ignore" and report.published["chat"]["refused"] == 1
    # The P1 it covers is held back; the P0 it covers stays, and so does the check.
    assert by_title(report.findings, TOKEN_TITLE).state == "suppressed"
    the_p0_stands(world, report)
    (reply,) = world.github.chat_replies()
    assert "1 open finding taken off the verdict: " + TOKEN_TITLE in reply["body"]
    assert f"1 must-fix finding it covers stays open: {AUTH_TITLE}. {NOTICE}" in reply["body"]
    (row,) = state_of(world).suppressions
    assert (row.source, row.category, row.by, row.dismisses_p0) == ("command", "security", "alice", False)
    assert len(world.github.inline()) == inline and world.github.review_posts == posts and world.model.calls == []
    body = summary_of(world)
    assert "1 finding dismissed by feedback" in body and "a must-fix (P0) it covers stays open" in body

    # It keeps not covering the P0 on later pushes, including a P0 that appears later in the same category.
    push_noise(world)
    later = world.review(fail_on="p0")
    the_p0_stands(world, later)
    assert "Suggested rules from feedback" in summary_of(world), "the command is still a rule she could propose"


def test_an_authors_ignore_in_the_thread_of_their_own_p0_is_refused_and_nothing_is_dismissed(world: World) -> None:
    auth, _ = reviewed(world, fail_on="p0")

    report = world.chat("@lemoncrow ignore this", thread=auth, fail_on="p0")

    assert report.published["chat"]["kind"] == "clarify"
    (reply,) = world.github.chat_replies()
    assert reply["in_reply_to_id"] == auth and reply["body"].startswith(NOTICE)
    the_p0_stands(world, world.review(fail_on="p0"))
    assert state_of(world).suppressions == ()


def test_an_ignore_command_from_a_maintainer_dismisses_the_p0_and_the_summary_names_them(world: World) -> None:
    auth, _ = reviewed(world, fail_on="p0")

    report = world.chat("@lemoncrow ignore this", thread=auth, author="maria", association="COLLABORATOR", fail_on="p0")

    assert report.published["chat"]["kind"] == "ignore"
    held = by_title(report.findings, AUTH_TITLE)
    assert held.state == "suppressed" and held.severity == "p0"
    assert report.verdict is not None and report.verdict.p0 == 0
    assert world.github.last_status()["state"] == "success"
    (row,) = state_of(world).suppressions
    assert (row.source, row.by, row.dismisses_p0) == ("command", "maria", True)
    dismissed = next(
        line
        for line in summary_of(world).splitlines()
        if "lets a missing principal through" in line and "asked for it" in line
    )
    assert "**P0**" in dismissed and "`maria` asked for it" in dismissed


def test_a_category_ignore_from_a_maintainer_covers_the_p0_too(world: World) -> None:
    reviewed(world, fail_on="p0")
    report = world.chat("@lemoncrow ignore security", author="maria", association="OWNER", fail_on="p0")
    assert {f.state for f in report.findings} == {"suppressed"}
    assert report.published["chat"]["refused"] == 0
    assert world.github.last_status()["state"] == "success"


def test_a_contributors_ignore_command_is_refused_for_a_p0_in_the_words_for_someone_who_is_not_the_author(
    world: World,
) -> None:
    auth, _ = reviewed(world, fail_on="p0")
    report = world.chat("@lemoncrow ignore this", thread=auth, author="carol", association="CONTRIBUTOR", fail_on="p0")
    assert report.published["chat"]["kind"] == "clarify"
    assert world.github.chat_replies()[0]["body"].startswith(OTHERS_ONLY)
    the_p0_stands(world, world.review(fail_on="p0"))


def test_the_instruction_the_author_gave_while_the_head_moved_on_still_leaves_the_p0(world: World) -> None:
    reviewed(world, fail_on="p0")
    push_noise(world)  # no review ran for it
    report = world.chat("@lemoncrow ignore security", fail_on="p0")
    assert report.published["chat"]["kind"] == "ignore" and report.published["chat"]["refused"] == 1
    assert "next review" in world.github.chat_replies()[0]["body"]
    assert [f.state for f in state_of(world).findings if f.title == AUTH_TITLE] == ["new"]
    world.origin.push({"src/legacy/token.py": TOKEN_BUGGY + "\n# touched\n"})
    later = world.review(fail_on="p0")
    the_p0_stands(world, later)
    assert by_title(later.findings, TOKEN_TITLE).state == "suppressed"


# --------------------------------------------------------------------------- #
# P1 and P2 are as they were
# --------------------------------------------------------------------------- #


def test_an_author_can_still_suppress_their_own_p1_with_a_thumbs_down_and_the_thread_is_told(world: World) -> None:
    auth, token = reviewed(world, fail_on="p0")
    thumbs_down(world, token)
    push_noise(world)

    report = world.review(fail_on="p0")

    assert by_title(report.findings, TOKEN_TITLE).state == "suppressed"
    the_p0_stands(world, report)
    (told,) = learned(world, token)
    assert told["body"].startswith("Learned: won't flag") and "`alice`" in told["body"]
    assert declined(world, token) == [] and declined(world, auth) == []
    (row,) = state_of(world).suppressions
    assert (row.by, row.dismisses_p0) == ("alice", False), "hers to give for a P1, not for a P0"


def test_an_author_can_still_suppress_their_own_p2(world: World) -> None:
    world.model.severity[TOKEN_TITLE] = "low"
    reviewed(world, fail_on="p0")
    assert by_title(state_of(world).findings, TOKEN_TITLE).severity == "p2"

    report = world.chat('@lemoncrow ignore "constant-time" in src/legacy/**', fail_on="p0")

    assert report.published["chat"]["kind"] == "ignore" and report.published["chat"]["refused"] == 0
    assert by_title(report.findings, TOKEN_TITLE).state == "suppressed"
    the_p0_stands(world, report)
    (reply,) = world.github.chat_replies()
    assert "must-fix" not in reply["body"], "nothing of hers was refused"
    (row,) = state_of(world).suppressions
    assert (row.source, row.by) == ("command", "alice")

    # The same concern returns on a later push: held back again.
    world.origin.push({"src/legacy/token.py": TOKEN_BUGGY + "\n# touched\n"})
    assert by_title(world.review(fail_on="p0").findings, TOKEN_TITLE).state == "suppressed"


def test_with_no_p0_on_the_pull_request_nothing_changes_for_the_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet = build_world(tmp_path, monkeypatch, FILES)  # no rating: both findings are P1
    auth, _ = reviewed(quiet, fail_on="p0")
    quiet.github.react(auth, DOWN, "alice")
    push_noise(quiet)
    report = quiet.review(fail_on="p0")
    assert (
        by_title(report.findings, AUTH_TITLE).state == "suppressed"
        and by_title(report.findings, AUTH_TITLE).severity == "p1"
    )
    assert report.published.get("declined") is None and declined(quiet, auth) == []


def test_a_rule_violation_is_still_protected_from_everyone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.gateway.pr_bot_support import RULE

    ruled = build_world(tmp_path, monkeypatch, {**FILES, ".lemoncrow/review.yaml": RULE})
    ruled.github.permissions["maria"] = "write"
    ruled.origin.push({"src/auth.py": AUTH_BUGGY})
    ruled.review(fail_on="p0")
    ruled.model.reset()
    rule_finding = next(f for f in state_of(ruled).findings if f.category == "critical_rule")
    ruled.github.react(rule_finding.thread_id, DOWN, "maria")
    push_noise(ruled)
    report = ruled.review(fail_on="p0")
    assert any(f.category == "critical_rule" and f.is_open for f in report.findings)
    assert state_of(ruled).suppressions == ()
    assert [row for row in ruled.github.bot_replies() if "<!-- lemoncrow:declined " in row["body"]] == []
