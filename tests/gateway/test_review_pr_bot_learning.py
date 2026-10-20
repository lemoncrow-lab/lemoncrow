"""Learning from feedback (G9): a reaction, a resolved thread, a reply or an ``ignore`` command takes effect on the
next push, with a confirmation in the thread, and wider learning is only ever a suggestion.

Same world as the other bot tests: a real git origin, a fake GitHub (which holds reactions, thread resolution and
the repository permissions of the people involved) and a fake model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review.pr_bot import learning
from lemoncrow.pro.capabilities.review.pr_bot import suppression as sup
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from lemoncrow.pro.capabilities.review.pr_bot.state import parse_state
from lemoncrow.pro.capabilities.review.rule_suggestions import adoption_stable_key
from lemoncrow.pro.capabilities.review.rules import resolve_review_governance
from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument
from tests.gateway.pr_bot_support import (
    AUTH_BUGGY,
    AUTH_FIXED,
    BASE,
    RULE,
    SLUG,
    TOKEN_BUGGY,
    World,
    build_world,
)

DOWN, UP = "THUMBS_DOWN", "THUMBS_UP"
FILES = {**BASE, "src/legacy/token.py": "def verify(token):\n    return True\n"}
PUSH = {"src/auth.py": AUTH_BUGGY, "src/legacy/token.py": TOKEN_BUGGY, "src/plain.py": "VALUE = 2\n"}
AUTH_TITLE = "lets a missing principal through"
TOKEN_TITLE = "compares secrets without a constant-time check"
REWORDED = AUTH_BUGGY.replace(AUTH_TITLE, "accepts a null principal")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, FILES)


def threads(world: World) -> tuple[int, int]:
    """The ids of the threads on the auth finding and the token finding."""

    by_path = {row["path"]: int(row["id"]) for row in world.github.inline()}
    return by_path["src/auth.py"], by_path["src/legacy/token.py"]


def reviewed(world: World, **options: Any) -> tuple[int, int]:
    world.origin.push(PUSH)
    world.review(**options)
    world.model.reset()
    return threads(world)


def state_of(world: World) -> Any:
    return parse_state(str(world.github.summary_comments()[0]["body"]))


def summary_of(world: World) -> str:
    return str(world.github.summary_comments()[0]["body"])


def learned(world: World, thread: int | None = None) -> list[dict[str, Any]]:
    return [
        row
        for row in world.github.bot_replies()
        if "<!-- lemoncrow:learned " in row["body"] and (thread is None or row["in_reply_to_id"] == thread)
    ]


def by_title(report: Any, title: str) -> BotFinding:
    return next(f for f in report.findings if f.title == title)


def push_noise(world: World, value: int = 3) -> None:
    world.origin.push({"src/plain.py": f"VALUE = {value}\n"})


# --------------------------------------------------------------------------- #
# a thumbs-down
# --------------------------------------------------------------------------- #


def test_a_thumbs_down_suppresses_the_same_concern_on_the_next_push_and_the_thread_is_told(world: World) -> None:
    auth, token = reviewed(world)
    inline_before, posts = len(world.github.inline()), world.github.review_posts
    world.github.react(auth, DOWN, "alice")
    # The next push touches the very code, and the model words the same concern differently.
    world.origin.push({"src/auth.py": REWORDED})

    report = world.review()

    held = next(f for f in report.findings if f.path == "src/auth.py")
    assert held.state == "suppressed" and held.title == "accepts a null principal"
    assert held.suppressed_by.startswith("sup-") and held.thread_id == auth, "it kept its own thread"
    assert len(world.github.inline()) == inline_before and world.github.review_posts == posts, "nothing new is posted"
    # It no longer counts: the token finding is all that is open.
    assert report.verdict is not None and (report.verdict.p0, report.verdict.p1) == (0, 1)
    assert [f.path for f in report.findings if f.is_open] == ["src/legacy/token.py"]
    # The thread is told, once, what was learned and how to make it a rule.
    (confirmation,) = learned(world, auth)
    text = confirmation["body"]
    assert text.startswith("Learned: won't flag")
    assert "on this pull request" in text and "`alice`" in text and "thumbs-down" in text.lower()
    assert "Make it a repo rule" in text and ".lemoncrow/review.yaml" in text and "once it is merged" in text
    assert "```yaml\nrules:" in text and "learned-" in text
    assert "<!-- lemoncrow:learned " + held.suppressed_by + " -->" in text
    assert learned(world, token) == []
    # And the summary says so, apart from the fixed ones.
    body = summary_of(world)
    assert "1 finding dismissed by feedback" in body and "accepts a null principal" in body
    assert "fixed since last review</summary>" not in body, "a dismissal is not a fix"
    strip = next(line for line in body.splitlines() if line.startswith("<sub>Re-reviewed"))
    assert "1 dismissed by feedback" in strip, "the status strip says so too"


def test_the_suppression_is_in_the_hidden_state_and_keeps_applying_on_later_pushes(world: World) -> None:
    auth, _ = reviewed(world)
    world.github.react(auth, DOWN, "alice")
    push_noise(world)
    world.review()
    state = state_of(world)
    (row,) = state.suppressions
    assert (row.source, row.by, row.category, row.path, row.thread_id) == (
        "reaction",
        "alice",
        "security",
        "src/auth.py",
        auth,
    )
    assert row.fingerprint.startswith("fp-") and row.title == AUTH_TITLE
    assert [f.state for f in state.findings if f.path == "src/auth.py"] == ["suppressed"]
    assert len(learned(world)) == 1

    # The concern comes back on a later push: held back again, no comment, and the thread is not told twice.
    inline, posts = len(world.github.inline()), world.github.review_posts
    world.origin.push({"src/auth.py": REWORDED})
    again = world.review()
    assert by_title(again, "accepts a null principal").state == "suppressed"
    assert len(world.github.inline()) == inline and world.github.review_posts == posts
    assert len(learned(world)) == 1
    assert len(state_of(world).suppressions) == 1


def test_a_thumbs_down_on_an_unrelated_push_still_takes_effect_with_no_model_call(world: World) -> None:
    auth, _ = reviewed(world)
    world.github.react(auth, DOWN, "alice")
    push_noise(world)
    report = world.review()
    assert world.model.calls == [] or all(path != "src/auth.py" for path in world.model.paths())
    assert by_title(report, AUTH_TITLE).state == "suppressed" and len(learned(world, auth)) == 1


def test_a_thumbs_up_does_not_suppress_and_marks_the_finding_confirmed(world: World) -> None:
    auth, _ = reviewed(world)
    world.github.react(auth, UP, "alice")
    push_noise(world)
    report = world.review()
    finding = by_title(report, AUTH_TITLE)
    assert finding.state == "carried" and finding.confirmed is True
    assert state_of(world).suppressions == () and learned(world) == []
    assert (
        next(row for row in json.loads(_state_json(world))["findings"] if row["path"] == "src/auth.py")["confirmed"]
        is True
    )
    assert report.verdict is not None and report.verdict.p1 == 2


def _state_json(world: World) -> str:
    match = re.search(r"<!-- lemoncrow:state v1\n(.*?)\n-->", summary_of(world), re.DOTALL)
    assert match is not None
    return match.group(1)


def test_other_emoji_are_neutral_and_a_down_wins_over_an_up(world: World) -> None:
    auth, token = reviewed(world)
    world.github.react(auth, "LAUGH", "alice")
    world.github.react(token, UP, "alice")
    world.github.react(token, DOWN, "alice")
    push_noise(world)
    report = world.review()
    assert by_title(report, AUTH_TITLE).state == "carried" and by_title(report, AUTH_TITLE).confirmed is False
    assert by_title(report, TOKEN_TITLE).state == "suppressed"


def test_reactions_from_people_who_may_not_teach_the_bot_are_ignored(world: World) -> None:
    auth, token = reviewed(world)
    world.github.permissions.update({"bob": "read", "carol": "write", "dave": "none"})
    world.github.react(auth, DOWN, "bob")  # reads the repository
    world.github.react(auth, DOWN, "dave")  # nobody
    world.github.react(auth, DOWN, "dependabot[bot]")
    world.github.react(auth, DOWN, "")  # GitHub gives no user for an app
    world.github.react(token, DOWN, "mallory")  # unknown to GitHub: the lookup fails
    push_noise(world)
    report = world.review()
    assert {f.state for f in report.findings if f.is_open} == {"carried"} and len(report.findings) == 2
    assert state_of(world).suppressions == () and learned(world) == []
    # A person with write access is a collaborator, whatever they have commented on.
    world.github.react(auth, DOWN, "carol")
    push_noise(world, 4)
    assert by_title(world.review(), AUTH_TITLE).state == "suppressed"
    assert state_of(world).suppressions[0].by == "carol"


def test_the_allowed_associations_are_the_chat_ones_and_configurable(world: World) -> None:
    auth, _ = reviewed(world)
    world.github.react(auth, DOWN, "alice")  # a MEMBER
    world.monkeypatch.setenv("LEMONCROW_REVIEW_CHAT_ASSOCIATIONS", "OWNER")
    push_noise(world)
    report = world.review()
    assert by_title(report, AUTH_TITLE).state == "carried", "a member is not allowed when only owners are"


def test_a_pull_request_authors_association_comes_from_the_pull_request(world: World) -> None:
    auth, _ = reviewed(world)
    world.github.associations["alice"] = "CONTRIBUTOR"  # an outside contributor who opened the pull request
    world.github.react(auth, DOWN, "alice")
    push_noise(world)
    assert by_title(world.review(), AUTH_TITLE).state == "suppressed"


# --------------------------------------------------------------------------- #
# a reply, a resolved thread
# --------------------------------------------------------------------------- #


def test_a_reply_that_says_it_is_not_a_bug_dismisses_the_finding_with_its_reason(world: World) -> None:
    auth, _ = reviewed(world)
    world.github.add_review_comment("Not a bug: callers never pass None, see gate.py.", in_reply_to=auth)
    push_noise(world)
    report = world.review()
    assert by_title(report, AUTH_TITLE).state == "suppressed"
    (row,) = state_of(world).suppressions
    assert (row.source, row.reason, row.by) == ("reply", "incorrect", "alice")
    assert "callers never pass None" in row.note
    assert "Not a bug: callers" in learned(world, auth)[0]["body"] or "not a bug" in learned(world, auth)[0]["body"]


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("won't fix, legacy path", "accepted_risk"),
        ("This is intentional.", "accepted_risk"),
        ("false positive", "incorrect"),
        ("duplicate of the other one", "duplicate"),
        ("n/a for this module", "not_applicable"),
    ],
)
def test_the_reply_phrases_map_to_the_dismissal_reasons_of_a_review(reply: str, reason: str) -> None:
    assert learning.classify_reply(reply) == reason
    assert reason in sup.REASONS


@pytest.mark.parametrize(
    "reply",
    [
        "is this not a bug?",
        "I will fix this tomorrow",
        "the tests use bare asserts",
        "@lemoncrow is this a false positive",
        "> not a bug\nwhy do you say that",
        "```\nnot a bug\n```",
        "",
    ],
)
def test_a_question_a_mention_a_quote_or_a_plain_reason_is_not_a_dismissal(reply: str) -> None:
    assert learning.classify_reply(reply) == ""


def test_replies_from_people_who_may_not_teach_the_bot_are_ignored(world: World) -> None:
    auth, _ = reviewed(world)
    world.github.add_review_comment("won't fix", in_reply_to=auth, user={"login": "mallory", "type": "User"})
    world.github.add_review_comment(
        "false positive", in_reply_to=auth, user={"login": "x[bot]", "type": "Bot"}, association="NONE"
    )
    push_noise(world)
    assert by_title(world.review(), AUTH_TITLE).state == "carried"


def test_a_thread_resolved_by_a_person_without_a_code_change_dismisses_the_finding(world: World) -> None:
    auth, _ = reviewed(world)
    world.github.resolve(auth, "alice")
    push_noise(world)
    report = world.review()
    assert by_title(report, AUTH_TITLE).state == "suppressed"
    (row,) = state_of(world).suppressions
    assert (row.source, row.by) == ("resolved", "alice")
    assert "resolved" in learned(world, auth)[0]["body"]


def test_a_thread_resolved_after_the_code_was_fixed_is_a_fix_and_teaches_nothing(world: World) -> None:
    world.model.cite_lines.add(AUTH_TITLE)
    auth, _ = reviewed(world)
    world.origin.push({"src/auth.py": AUTH_FIXED})
    world.github.resolve(auth, "alice")
    report = world.review()
    assert by_title(report, AUTH_TITLE).state == "fixed"
    assert state_of(world).suppressions == () and learned(world) == []


def test_a_thread_the_bot_itself_resolved_teaches_nothing(world: World) -> None:
    world.model.cite_lines.add(AUTH_TITLE)
    auth, _ = reviewed(world)
    world.origin.push({"src/auth.py": AUTH_FIXED})
    world.review()  # fixes it, replies and resolves the thread as the bot
    assert world.github.threads[auth]["resolved"] and world.github.threads[auth]["by_kind"] == "Bot"
    push_noise(world)
    world.review()
    assert state_of(world).suppressions == ()


# --------------------------------------------------------------------------- #
# `@lemoncrow ignore ...`
# --------------------------------------------------------------------------- #


def test_an_ignore_command_scopes_by_path(world: World) -> None:
    reviewed(world)
    inline, posts = len(world.github.inline()), world.github.review_posts

    report = world.chat("@lemoncrow ignore security in src/legacy/**")

    assert report.published["chat"]["kind"] == "ignore"
    # Only the finding under the glob is held back; the same category elsewhere still stands.
    assert {(f.path, f.state) for f in report.findings} == {
        ("src/legacy/token.py", "suppressed"),
        ("src/auth.py", "carried"),
    }
    (reply,) = world.github.chat_replies()
    assert reply in world.github.issue_comments, "asked in the conversation, answered there"
    assert reply["body"].startswith("Learned: won't flag `security` findings in `src/legacy/**` on this pull request.")
    assert "1 open finding taken off the verdict" in reply["body"] and TOKEN_TITLE in reply["body"]
    assert "Make it a repo rule" in reply["body"] and "```yaml\nrules:" in reply["body"]
    # It took effect now: the summary and the status already agree, with no push in between.
    assert report.verdict is not None and (report.verdict.p0, report.verdict.p1) == (0, 1)
    assert world.github.last_status()["sha"] == world.origin.head
    assert "1 finding dismissed by feedback" in summary_of(world)
    assert len(world.github.inline()) == inline and world.github.review_posts == posts and world.model.calls == []
    (row,) = state_of(world).suppressions
    assert (row.source, row.category, row.paths, row.by) == ("command", "security", ("src/legacy/**",), "alice")

    # A later push that reports the token concern again does not post it; the auth one is still reviewed normally.
    world.origin.push({"src/legacy/token.py": TOKEN_BUGGY + "\n# touched\n"})
    later = world.review()
    assert by_title(later, TOKEN_TITLE).state == "suppressed"
    assert len(world.github.inline()) == inline


def test_ignore_matches_the_words_of_a_title_or_a_category_in_everyday_words(world: World) -> None:
    reviewed(world)
    report = world.chat('@lemoncrow ignore "constant-time" in src/**')
    assert report.published["chat"]["kind"] == "ignore"
    (row,) = state_of(world).suppressions
    assert (row.pattern, row.category, row.paths) == ("constant-time", "", ("src/**",))
    assert {(f.path, f.state) for f in report.findings if f.state == "suppressed"} == {
        ("src/legacy/token.py", "suppressed")
    }


@pytest.mark.parametrize(
    ("text", "category", "pattern", "paths"),
    [
        ("security in tests/**", "security", "", ("tests/**",)),
        ("all security findings in tests/** docs/**", "security", "", ("tests/**", "docs/**")),
        ("concurrency", "concurrency_state", "", ()),
        ("API compatibility in src/api/", "api_compatibility", "", ("src/api/",)),
        ("tests in **/legacy/*.py", "verification", "", ("**/legacy/*.py",)),
        ("missing null check", "", "missing null check", ()),
        ("missing null check in handlers", "", "missing null check in handlers", ()),
        ("`unused import` in src/*.py", "", "unused import", ("src/*.py",)),
    ],
)
def test_ignore_reads_a_category_or_words_and_an_optional_path_glob(
    text: str, category: str, pattern: str, paths: tuple[str, ...]
) -> None:
    parsed = learning.parse_ignore(text, finding=None, by="alice", thread_id=0, head="abc1234")
    assert parsed.problem == "" and parsed.suppression is not None
    row = parsed.suppression
    assert (row.category, row.pattern, row.paths, row.source) == (category, pattern, paths, "command")


@pytest.mark.parametrize("text", ["", "this", "it", "a", "in tests/**", "ab"])
def test_ignore_asks_what_to_ignore_when_it_cannot_tell(text: str) -> None:
    parsed = learning.parse_ignore(text, finding=None, by="alice", thread_id=0, head="abc1234")
    assert parsed.suppression is None and ("Say what to ignore" in parsed.problem or "Which finding?" in parsed.problem)


@pytest.mark.parametrize("text", ["security in ../../etc", "x-ray checks in /abs/**", "security in secrets"])
def test_a_glob_that_is_not_a_repository_path_scope_is_never_one(text: str) -> None:
    """Whatever does not read as a safe, repository-relative glob is words to match, never a path outside the repo."""

    parsed = learning.parse_ignore(text, finding=None, by="alice", thread_id=0, head="abc1234")
    assert parsed.suppression is not None and parsed.suppression.paths == ()


def test_ignore_without_arguments_in_a_finding_thread_dismisses_that_finding(world: World) -> None:
    auth, _ = reviewed(world)
    report = world.chat("@lemoncrow ignore this", thread=auth)
    assert report.published["chat"]["kind"] == "ignore"
    assert by_title(report, AUTH_TITLE).state == "suppressed" and by_title(report, TOKEN_TITLE).is_open
    (reply,) = world.github.chat_replies()
    assert reply["in_reply_to_id"] == auth and reply["body"].startswith("Learned: won't flag")
    assert learned(world) == world.github.chat_replies(), "the reply to the command is the thread's one confirmation"
    assert state_of(world).suppressions[0].source == "command"


def test_ignore_in_the_conversation_without_a_target_asks_what_to_ignore(world: World) -> None:
    reviewed(world)
    report = world.chat("@lemoncrow ignore")
    assert report.published["chat"]["kind"] == "clarify" and state_of(world).suppressions == ()
    assert (
        "Say what to ignore" in world.github.chat_replies()[0]["body"]
        or "Which finding?" in world.github.chat_replies()[0]["body"]
    )


def test_a_rule_violation_cannot_be_ignored_by_a_comment_or_a_reaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, {**FILES, ".lemoncrow/review.yaml": RULE})
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    world.review()
    world.model.reset()
    rule_finding = next(f for f in state_of(world).findings if f.category == "critical_rule")
    assert rule_finding.severity == "p0" and rule_finding.thread_id
    world.github.react(rule_finding.thread_id, DOWN, "alice")
    world.github.resolve(rule_finding.thread_id, "alice")
    by_command = world.chat("@lemoncrow ignore critical_rule")
    assert (
        by_command.published["chat"]["kind"] == "clarify"
        and "trusted base owns" in world.github.chat_replies()[0]["body"]
    )
    in_thread = world.chat("@lemoncrow ignore this", thread=rule_finding.thread_id)
    assert in_thread.published["chat"]["kind"] == "clarify"
    push_noise(world)
    report = world.review()
    assert state_of(world).suppressions == ()
    assert any(f.category == "critical_rule" and f.is_open and f.severity == "p0" for f in report.findings)
    assert report.verdict is not None and report.verdict.state == "must_fix"


def test_only_allowed_people_can_give_commands(world: World) -> None:
    reviewed(world)
    for author, association in (("mallory", "NONE"), ("bob", "FIRST_TIME_CONTRIBUTOR")):
        world.chat("@lemoncrow ignore security", author=author, association=association)
    world.chat("@lemoncrow ignore security", author="x[bot]", user_type="Bot", association="MEMBER")
    assert state_of(world).suppressions == () and world.github.chat_replies() == []


def test_an_ignore_when_the_pull_request_has_moved_on_is_kept_for_the_next_review(world: World) -> None:
    reviewed(world)
    push_noise(world)  # no review ran for it
    statuses = len(world.github.statuses)
    report = world.chat("@lemoncrow ignore security in src/legacy/**")
    assert report.published["chat"]["kind"] == "ignore" and world.model.calls == []
    assert "next review" in world.github.chat_replies()[0]["body"]
    state = state_of(world)
    assert [s.paths for s in state.suppressions] == [("src/legacy/**",)]
    assert [f.state for f in state.findings if f.path == "src/legacy/token.py"] == ["suppressed"]
    assert len(world.github.statuses) == statuses, "the check belongs to a review"
    # The next review applies it to what it finds.
    world.origin.push({"src/legacy/token.py": TOKEN_BUGGY + "\n# touched\n"})
    assert by_title(world.review(), TOKEN_TITLE).state == "suppressed"


def test_ignore_is_free_even_when_the_budget_is_spent(world: World) -> None:
    reviewed(world, max_runs_per_pr=1)
    report = world.chat("@lemoncrow ignore security in src/legacy/**", max_runs_per_pr=1)
    assert report.published["chat"]["kind"] == "ignore" and world.model.chats == [] and world.model.calls == []
    assert state_of(world).reviews == 1 and state_of(world).chats == 0


def test_the_same_instruction_twice_is_one_suppression(world: World) -> None:
    reviewed(world)
    world.chat("@lemoncrow ignore security in src/legacy/**")
    world.chat("@lemoncrow Ignore   security  in  src/legacy/**")
    assert len(state_of(world).suppressions) == 1


# --------------------------------------------------------------------------- #
# suggestions: wider learning is explicit, never silent
# --------------------------------------------------------------------------- #


def suggested_yaml(body: str) -> str:
    match = re.search(r"Suggested rules from feedback</summary>.*?```yaml\n(.*?)```", body, re.DOTALL)
    assert match is not None, "the summary has a collapsed block of suggested rules"
    return match.group(1)


def accepted_by_the_loader(text: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """What the repository's own rule loader makes of *text* as ``.lemoncrow/review.yaml``."""

    document = TrustedBaseDocument(path=".lemoncrow/review.yaml", content=text, state="present", source="test")
    absent = TrustedBaseDocument(path="", content="", state="absent")
    governance = resolve_review_governance(document, read_base_document=lambda _path: absent)
    assert governance.errors == ()
    return [(rule.stable_key, rule.instruction, rule.path_scope) for rule in governance.rules]


def test_suggested_rules_are_a_collapsed_block_of_yaml_the_rule_loader_accepts(world: World) -> None:
    reviewed(world)
    world.chat("@lemoncrow ignore security in src/legacy/**")
    body = summary_of(world)
    assert "<details><summary>Suggested rules from feedback</summary>" in body
    assert (
        ".lemoncrow/review.yaml" in body and "once it is merged" in body and "never writes to your repository" in body
    )
    text = suggested_yaml(body)
    parsed = yaml.safe_load(text)
    (row,) = parsed["rules"]
    assert row["paths"] == ["src/legacy/**"] and row["instruction"].startswith("Do not report security findings")
    (loaded,) = accepted_by_the_loader(text)
    assert loaded[0] == row["stable_key"] and loaded[0].startswith("learned-")
    assert loaded[2] == ("src/legacy/**",)
    # The same key the adoption flow uses for the same suggestion, so the two never disagree.
    (suggestion,) = learning.suggest(SLUG, state_of(world).suppressions)
    assert adoption_stable_key(suggestion.id) == loaded[0]


def test_one_silent_thumbs_down_is_not_yet_a_suggestion_but_a_repeat_or_a_reason_is(world: World) -> None:
    auth, token = reviewed(world)
    world.github.react(auth, DOWN, "alice")
    push_noise(world)
    world.review()
    assert "Suggested rules from feedback" not in summary_of(world), "one thumbs-down is not a pattern"
    # The confirmation in its thread still offers the rule for that one.
    assert "```yaml\nrules:" in learned(world, auth)[0]["body"]
    world.github.add_review_comment("won't fix: the legacy module is going away", in_reply_to=token)
    push_noise(world, 4)
    world.review()
    text = suggested_yaml(summary_of(world))
    rules = accepted_by_the_loader(text)
    assert rules, "a dismissal with a reason is offered"
    assert any("the legacy module is going away" in instruction for _key, instruction, _paths in rules)


def outside_fences(text: str) -> str:
    """*text* without its code fences (where GitHub never notifies anyone) and without broken-up mentions."""

    return re.sub(r"```.*?```", "", text, flags=re.DOTALL).replace("@\u200b", "@~")


def test_text_a_person_wrote_cannot_break_the_state_the_comment_or_the_yaml(world: World) -> None:
    reviewed(world)
    hostile = 'x" } evil: [1] --> lemoncrow:state v1 ` @everyone ![p](http://e.example/p.png)'
    world.chat(f"@lemoncrow ignore {hostile} in src/**")
    body = summary_of(world)
    assert body.count("<!-- lemoncrow:state v1") == 1 and parse_state(body) is not None
    assert "@everyone" not in outside_fences(body) and "![" not in body
    text = suggested_yaml(body)
    parsed = yaml.safe_load(text)
    assert list(parsed) == ["rules"] and set(parsed["rules"][0]) == {"stable_key", "instruction", "paths"}
    accepted_by_the_loader(text)
    reply = world.github.chat_replies()[0]["body"]
    assert "<!-- lemoncrow:state" not in reply and "![" not in reply
    assert "@everyone" not in outside_fences(reply)
    # A multi-line comment is a question, not an instruction to remember.
    before = state_of(world).suppressions
    world.chat("@lemoncrow ignore this\nsecond line")
    assert state_of(world).suppressions == before


def test_a_suggested_rule_governs_reviews_only_once_it_is_merged_to_the_trusted_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pr").mkdir()
    world = build_world(tmp_path / "pr", monkeypatch, FILES)
    world.origin.push(PUSH)
    world.review()
    before = state_of(world).policy
    world.chat("@lemoncrow ignore security in src/legacy/**")
    text = suggested_yaml(summary_of(world))
    # Copied into the pull request's own branch it is not policy for that pull request: the policy is read from the
    # merge base, so a pull request cannot loosen (or tighten) its own review.
    world.origin.push({".lemoncrow/review.yaml": text})
    world.review()
    assert state_of(world).policy == before
    # Merged to the base branch, the same text is a rule of the reviews that follow.
    (tmp_path / "merged").mkdir()
    merged = build_world(tmp_path / "merged", monkeypatch, {**FILES, ".lemoncrow/review.yaml": text})
    merged.origin.push(PUSH)
    merged.review()
    assert state_of(merged).policy != before


def test_the_bot_never_writes_to_the_repository_whatever_it_learns(world: World) -> None:
    auth, token = reviewed(world)
    world.github.react(auth, DOWN, "alice")
    world.github.resolve(token, "alice")
    world.chat("@lemoncrow ignore performance")
    push_noise(world)
    world.review()
    written = {(method, re.sub(r"\d+", "N", path)) for method, path in world.github.calls if method != "GET"}
    assert all(
        "/contents/" not in path and "/git/" not in path and "/branches" not in path for _m, path in written
    ), written
    assert {method for method, _ in written} <= {"POST", "PATCH"}


def test_a_dry_run_reads_the_feedback_and_posts_nothing(world: World) -> None:
    auth, _ = reviewed(world)
    world.github.react(auth, DOWN, "alice")
    push_noise(world)
    replies = len(world.github.bot_replies())
    report = world.review(publish=True, dry_run=True)
    assert by_title(report, AUTH_TITLE).state == "suppressed"
    assert len(world.github.bot_replies()) == replies and state_of(world).suppressions == ()


def test_a_silent_run_with_no_feedback_changes_nothing_and_costs_no_extra_write(world: World) -> None:
    reviewed(world)
    again = world.review()
    assert again.noop and state_of(world).suppressions == () and learned(world) == []


def test_the_suppression_list_is_capped_and_keeps_the_newest() -> None:
    rows = [sup.make_suppression("command", pattern=f"words {n}", by="alice") for n in range(sup.MAX_SUPPRESSIONS + 5)]
    kept = sup.merge((), rows)
    assert len(kept) == sup.MAX_SUPPRESSIONS and kept[-1].pattern == f"words {sup.MAX_SUPPRESSIONS + 4}"


def test_a_state_row_that_holds_nothing_back_or_is_out_of_vocabulary_is_dropped() -> None:
    assert sup.suppression_from_wire({"id": "sup-1", "source": "command"}) is None
    assert sup.suppression_from_wire({"id": "sup-1", "source": "hacker", "category": "security"}) is None
    assert sup.suppression_from_wire({"id": "", "source": "command", "category": "security"}) is None
    row = sup.suppression_from_wire({"id": "sup-1", "source": "command", "category": "bogus", "pattern": "x" * 500})
    assert row is not None and row.category == "" and len(row.pattern) == sup.MAX_PATTERN
    assert (
        sup.suppression_from_wire({"id": "sup-2", "source": "reply", "category": "security", "reason": "nope"})
        is not None
    )


def test_the_suppression_matches_what_it_says_and_never_a_rule_violation() -> None:
    def finding(**fields: Any) -> BotFinding:
        base: dict[str, Any] = {
            "id": "f1",
            "fingerprint": "fp-0123456789abcdef",
            "severity": "p1",
            "category": "security",
            "state": "new",
            "path": "src/legacy/a.py",
            "title": "Missing check",
            "explanation": "no auth",
        }
        return BotFinding(**{**base, **fields})

    by_fingerprint = sup.make_suppression("reaction", fingerprint="fp-0123456789abcdef")
    assert by_fingerprint.matches(finding()) and not by_fingerprint.matches(finding(fingerprint="fp-ffffffffffffffff"))
    by_category = sup.make_suppression("command", category="security", paths=("src/legacy/**",))
    assert by_category.matches(finding()) and not by_category.matches(finding(path="src/new/a.py"))
    assert not by_category.matches(finding(category="performance"))
    by_words = sup.make_suppression("command", pattern="MISSING check")
    assert by_words.matches(finding()) and by_words.matches(finding(title="x", explanation="the missing check here"))
    for held in (by_fingerprint, by_category, by_words, sup.make_suppression("command", category="critical_rule")):
        assert not held.matches(finding(category="critical_rule"))
