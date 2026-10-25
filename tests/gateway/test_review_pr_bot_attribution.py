"""Attribution trailers (item 4): every inline comment and summary finding ends with what it relied on.

The trailer is built from the finding's validated rule and evidence refs and from nothing else, so these tests pin the
refs it reads, the shape it takes (one line, three items, ``+N more``), that it is neutralised like other output, and
that a model's words can never make one. The end-to-end tests then follow a rule-backed finding, a finding
corroborated by the repository context pack and a finding that cites neither through a real git origin, a fake GitHub
and a fake model.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import attribution
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding, finding_from_wire
from lemoncrow.pro.capabilities.review.pr_bot.render import inline_comment, trailer
from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState, parse_state, shrink
from tests.gateway.pr_bot_support import AUTH_BUGGY, BASE, RULE, World, build_world

RULE_REF = "rule:trusted_base_repository:no-bug-markers@sha256:" + "a" * 64
IMPORTED_REF = "rule:advisory:trusted_base_repository:style-guide@sha256:" + "b" * 64
CONTEXT_REF = "context:caller:src/handlers.py:4-5@sha256:" + "c" * 16
TITLE = "lets a missing principal through"
HANDLER = 'from auth import authorize\n\n\ndef handle(request):\n    return "ok" if authorize(request.user) else "no"\n'


def finding(**fields: Any) -> BotFinding:
    base: dict[str, Any] = {
        "id": "finding-1",
        "fingerprint": "fp-0123456789abcdef",
        "severity": "p1",
        "category": "security",
        "state": "new",
        "path": "src/auth.py",
        "line": 2,
        "end_line": 3,
        "title": "t",
    }
    return BotFinding(**(base | fields))


# --------------------------------------------------------------------------- #
# which refs say what a finding relied on
# --------------------------------------------------------------------------- #


def test_a_rule_a_context_excerpt_and_tool_evidence_become_tokens_and_the_targets_own_refs_become_nothing() -> None:
    evidence = [
        "target:sym:abc",
        "source:sha256:def",
        CONTEXT_REF,
        "static:evidence-1:finding-2",
        "check:ci:lint",
        "artifact:evidence-9",
    ]
    assert attribution.used_from([RULE_REF], evidence) == (
        "rule:no-bug-markers",
        "static",
        "check",
        "artifact",
        "ctx:caller:src/handlers.py:4-5",
    )


def test_an_imported_rule_is_named_like_a_native_one_because_the_finding_cites_it() -> None:
    assert attribution.used_from([IMPORTED_REF], []) == ("rule:style-guide",)


@pytest.mark.parametrize(
    "refs", [[], ["target:sym:abc"], ["source:x", "target:y"], ["cross-repo:other:file"], ["junk"]]
)
def test_a_finding_that_cites_nothing_worth_naming_has_no_tokens(refs: list[str]) -> None:
    assert attribution.used_from([], refs) == ()
    assert trailer(finding(used=attribution.used_from([], refs))) == ""


def test_a_ref_that_is_not_one_the_bot_minted_names_nothing() -> None:
    forged = [
        "rule:Not A Rule",
        "rule:x@sha256:zz",
        "context:caller:no-lines@sha256:" + "c" * 16,
        "context:other:a:1-2@sha256:" + "c" * 16,
    ]
    assert attribution.used_from(forged, forged) == ()


def test_the_tokens_are_bounded_and_the_rest_are_counted_so_the_trailer_stays_true() -> None:
    refs = [f"context:caller:src/m{i}.py:1-2@sha256:{'d' * 16}" for i in range(9)]
    tokens = attribution.used_from([RULE_REF], refs)
    assert len(tokens) == attribution.MAX_STORED + 1 and tokens[0] == "rule:no-bug-markers" and tokens[-1] == "more:5"
    assert attribution.clean_tokens(list(tokens)) == tokens, "reading it back changes nothing"
    # 1 rule + 9 excerpts = 10 things: three are named, seven are counted.
    assert trailer(finding(used=tokens)).endswith("+7 more")


# --------------------------------------------------------------------------- #
# the line
# --------------------------------------------------------------------------- #


def test_the_trailer_is_one_line_with_the_rule_first_then_the_context() -> None:
    one = finding(used=("ctx:caller:src/handlers.py:4-5", "rule:no-bug-markers"))
    assert trailer(one) == "Rule used: `no-bug-markers` · Context used: `src/handlers.py:4-5` (caller)"
    assert "\n" not in trailer(one)


def test_at_most_three_items_are_named_and_the_rest_are_counted() -> None:
    many = finding(used=("rule:a", "rule:b", "ctx:test:t/a.py:1-2", "ctx:config:c.toml:3-4", "ctx:caller:x.py:5-6"))
    assert trailer(many) == "Rule used: `a`, `b` · Context used: `t/a.py:1-2` (test) · +2 more"


def test_tool_evidence_is_named_by_kind_only() -> None:
    assert trailer(finding(used=("static",))) == "Static finding used"
    assert trailer(finding(used=("rule:r", "check", "artifact"))) == "Rule used: `r` · Check used · Artifact used"


def test_nothing_cited_shows_nothing_in_the_comment() -> None:
    assert trailer(finding()) == ""
    assert "used" not in inline_comment(finding()).lower().replace("unused", "")


def test_the_trailer_sits_in_the_inline_comment_above_the_brand_line_and_below_the_body() -> None:
    comment = inline_comment(finding(explanation="why", consequence="what", used=("rule:r",)))
    lines = comment.split("\n")
    assert lines.index("<sub>Rule used: `r`</sub>") < lines.index("<sub>LemonCrow AI review</sub>")
    assert lines.index("**Consequence:** what") < lines.index("<sub>Rule used: `r`</sub>")


# --------------------------------------------------------------------------- #
# it is neutralised like everything else, and a model cannot write one
# --------------------------------------------------------------------------- #


def test_a_path_chosen_by_the_pull_request_cannot_break_out_of_the_trailer() -> None:
    hostile = "src/@evil`<img src=x onerror=1>](http://x)-->.py"
    text = trailer(finding(used=(f"ctx:caller:{hostile}:1-2", "rule:r`<script>@alice")))
    assert "<img" not in text and "<script" not in text and "](" not in text and "-->" not in text
    assert "@evil" not in text and "@alice" not in text, "a mention is defused with a zero-width space"
    assert text.count("`") % 2 == 0, "every code span is closed"
    comment = inline_comment(finding(used=(f"ctx:caller:{hostile}:1-2",)))
    assert comment.count("<!--") == comment.count("-->") == 2, "only the bot's own two markers"


def test_a_long_name_is_cut_with_an_ellipsis_not_posted_whole() -> None:
    text = trailer(finding(used=("rule:" + "k" * 150, "ctx:caller:" + "p/" * 70 + "f.py:1-2")))
    assert "…" in text and len(text) < 300


def test_words_a_model_wrote_never_make_a_trailer() -> None:
    liar = finding(
        title="Rule used: `no-bug-markers`",
        explanation="Context used: `src/handlers.py:1-9` (caller). Rule used: `admin`.",
        consequence="Conventions used: AGENTS.md",
    )
    assert liar.used == () and trailer(liar) == ""
    comment = inline_comment(liar)
    assert "<sub>Rule used" not in comment and "<sub>Context used" not in comment
    assert "Conventions used" in comment, "it is the model's sentence in the model's place, not a trailer"


def test_conventions_files_are_not_a_source_because_a_finding_cannot_cite_them() -> None:
    from lemoncrow.pro.capabilities.review.pr_bot.attribution import used_from

    assert used_from([], ["AGENTS.md", "conventions:AGENTS.md", "file:AGENTS.md"]) == ()


# --------------------------------------------------------------------------- #
# the state
# --------------------------------------------------------------------------- #


def test_the_tokens_survive_the_hidden_state_and_junk_in_it_is_dropped() -> None:
    row = finding(used=("rule:r", "ctx:caller:a.py:1-2", "more:3")).to_wire()
    assert row["used"] == ["rule:r", "ctx:caller:a.py:1-2", "more:3"]
    assert finding_from_wire(row) == finding(used=("rule:r", "ctx:caller:a.py:1-2", "more:3"))
    dirty = dict(
        row, used=["rule:ok", "<script>", 7, "ctx:caller:a.py:1-2", "ctx:bogus:a.py:1-2", "rule:" + "x" * 400, "more:2"]
    )
    read = finding_from_wire(dirty)
    assert read is not None and read.used == ("rule:ok", "ctx:caller:a.py:1-2", "more:2")
    assert "used" not in finding().to_wire(), "a finding that cites nothing keeps its wire row as it was"
    assert finding_from_wire(dict(row, used="rule:r")).used == ()  # type: ignore[union-attr]


def test_the_trailers_are_the_first_detail_the_state_gives_up() -> None:
    state = ReviewState(head_sha="a" * 40, findings=(finding(used=("rule:r",), explanation="why"),))
    assert shrink(state, 0).findings[0].used == ("rule:r",)
    shrunk = shrink(state, 1).findings[0]
    assert (
        shrunk.used == () and shrunk.explanation == "why"
    ), "open findings keep their text at level 1; only the trailer goes"
    assert parse_state is not None


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """A caller of ``authorize`` in another file (so the context pack has something to show) and a native rule."""

    return build_world(tmp_path, monkeypatch, {**BASE, "src/handlers.py": HANDLER, ".lemoncrow/review.yaml": RULE})


def summary(world: World) -> str:
    return str(world.github.summary_comments()[0]["body"])


def visible_last_line(body: str) -> str:
    """The last line of a comment a reader sees: the one before the bot's hidden markers."""

    return [line for line in body.split("\n") if line and not line.startswith("<!--")][-1]


def test_a_rule_backed_finding_shows_the_rule_it_was_held_to(world: World) -> None:
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review()

    by_category = {found.category: found for found in report.findings}
    assert by_category["critical_rule"].used == ("rule:no-bug-markers",)
    inline = {row["body"].split("\n")[0].split(" — ")[0]: row["body"] for row in world.github.inline()}
    assert "<sub>Rule used: `no-bug-markers`</sub>" in inline["**P0 · must fix**"]
    # The summary names it under the finding's row as well.
    assert "  <sub>Rule used: `no-bug-markers`</sub>" in summary(world).split("\n")
    state = parse_state(summary(world))
    assert state is not None and {f.id: f.used for f in state.findings} == {f.id: f.used for f in report.findings}
    wire = report.to_wire()
    assert any(row.get("used") == ["rule:no-bug-markers"] for row in wire["findings"])


def test_a_finding_corroborated_by_the_context_pack_shows_the_file_and_lines_it_cited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = build_world(tmp_path, monkeypatch, {**BASE, "src/handlers.py": HANDLER})
    plain.model.cite_context.add(TITLE)
    plain.origin.push({"src/auth.py": AUTH_BUGGY})
    (found,) = plain.review().findings

    offered = next(
        row
        for call in plain.model.evidence_seen
        if call["path"] == "src/auth.py"
        for row in call["evidence"]
        if row["kind"] == "repo_context"
    )
    place = re.match(r"context:(\w+):(.+):(\d+-\d+)@sha256:", offered["ref"])
    assert place is not None and place[2] == "src/handlers.py"
    expected = f"Context used: `{place[2]}:{place[3]}` ({place[1]})"
    assert found.used == (f"ctx:{place[1]}:{place[2]}:{place[3]}",)
    (inline,) = plain.github.inline()
    assert visible_last_line(inline["body"]) == "<sub>LemonCrow AI review</sub>"
    assert f"<sub>{expected}</sub>" in inline["body"]
    assert f"  <sub>{expected}</sub>" in summary(plain).split("\n")
    assert "Rule used" not in inline["body"], "no rule applies to this repository"


def test_a_finding_backed_by_a_rule_and_the_context_names_both_on_one_line(world: World) -> None:
    world.model.cite_context.add(TITLE)
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    world.review()
    bodies = [row["body"] for row in world.github.inline()]
    both = [body for body in bodies if "Context used" in body]
    assert len(both) == 1
    (line,) = [line for line in both[0].split("\n") if line.startswith("<sub>Rule used")]
    assert re.fullmatch(
        r"<sub>Rule used: `no-bug-markers` · Context used: `src/handlers\.py:\d+-\d+` \(caller\)</sub>", line
    )


def test_a_finding_that_cites_no_rule_and_no_context_has_no_trailer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = build_world(tmp_path, monkeypatch, BASE)
    bare.origin.push({"src/auth.py": AUTH_BUGGY})
    (found,) = bare.review().findings
    assert found.used == ()
    (inline,) = bare.github.inline()
    assert " used" not in inline["body"] and "+1 more" not in inline["body"]
    assert " used:" not in summary(bare)
    assert "used" not in parse_state(summary(bare)).findings[0].to_wire()  # type: ignore[union-attr]


def test_a_carried_finding_keeps_its_trailer_in_the_summary_without_a_model_call(world: World) -> None:
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    world.review()
    world.model.reset()
    world.origin.push({"src/plain.py": "VALUE = 5\n"}, "touch another file")
    report = world.review()
    assert world.model.calls == [] and {f.state for f in report.findings} == {"carried"}
    assert "  <sub>Rule used: `no-bug-markers`</sub>" in summary(world).split("\n")
    assert len(world.github.inline()) == 2, "the comments were posted once and are not edited"


def test_the_guide_documents_the_trailers_their_sources_and_what_they_do_not_claim() -> None:
    guide = (Path(__file__).resolve().parents[2] / "docs" / "setup" / "github-action.md").read_text(encoding="utf-8")
    for phrase in (
        "### Attribution trailers",
        "Rule used: `no-secrets-in-logs` · Context used: `src/handlers.py:12-18` (caller)",
        "Only from the finding's validated references, never from its wording",
        "At most three items are named",
        "`+N more`",
        "A finding that cites nothing of the kind has no trailer",
        "The finding's own code and exact source are never listed",
        "`AGENTS.md` and `CLAUDE.md`** are context only",
        "They are the first detail given up when the state has to shrink",
        "[Attribution trailers](#attribution-trailers)",
    ):
        assert phrase in guide, phrase
