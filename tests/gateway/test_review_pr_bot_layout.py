"""The pull request bot's comments, section by section, the way a team reads them.

Golden renders of the summary comment (clean, findings, partial, budget reached, model failed, a re-review with fixed
findings) and of an inline comment, plus the rules that make them honest: the confidence score is read off the same facts
as the verdict, the badges are images with their text as the alt text, the size limit gives things up in a fixed order,
and nothing a model wrote can forge a marker.

The goldens are the exact Markdown (the hidden state block aside, which has its own tests). To change one on purpose, run
the test with ``LEMONCROW_UPDATE_GOLDEN=1`` and read the diff.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import overview_diagram
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from lemoncrow.pro.capabilities.review.pr_bot.impact import Impact, RiskRow
from lemoncrow.pro.capabilities.review.pr_bot.overview import DiagramSpec, Edge, Node, Overview, Vocabulary, parse
from lemoncrow.pro.capabilities.review.pr_bot.render import (
    BADGE_BASE_URL,
    BADGE_VERSION,
    MAX_COMMENT,
    Receipt,
    RunFacts,
    SummaryView,
    Verdict,
    badge,
    fixed_badge,
    inline_comment,
    render_summary,
    reply_text,
    verdict,
)
from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState, Usage, parse_state, strip_state

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "pr_bot"
HEAD = "a" * 40
BASE = "b" * 40
COMMIT = f"https://github.com/acme/widgets/commit/{HEAD}"
THREAD = "https://github.com/acme/widgets/pull/7#discussion_r"


def assert_golden(name: str, text: str) -> None:
    path = GOLDEN / f"{name}.md"
    if os.environ.get("LEMONCROW_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"no golden {path}; write it with LEMONCROW_UPDATE_GOLDEN=1"
    assert text == path.read_text(encoding="utf-8"), f"{name} changed; if on purpose, LEMONCROW_UPDATE_GOLDEN=1"


# --------------------------------------------------------------------------- #
# fixtures: one change, three findings, an overview
# --------------------------------------------------------------------------- #


def finding(identifier: str, severity: str, state: str = "new", **fields: Any) -> BotFinding:
    values: dict[str, Any] = {
        "id": identifier,
        "fingerprint": f"fp-{identifier:0<16}"[:19],
        "severity": severity,
        "category": "security",
        "state": state,
        "path": "src/auth.py",
        "line": 12,
        "end_line": 14,
        "target_ids": (f"target:sym:{identifier}",),
        "title": "Missing principal accepted",
        "explanation": "The changed branch returns before it rejects a missing principal.",
        "consequence": "An unauthenticated request can reach behaviour meant for administrators.",
        "first_seen": "1234567",
        "last_seen": "1234567",
    }
    values.update(fields)
    return BotFinding(**values)


P0 = finding(
    "p0",
    "p0",
    category="critical_rule",
    title="Token is logged in clear text",
    path="src/auth.py",
    line=31,
    end_line=31,
    thread_id=1,
    thread_url=f"{THREAD}1",
    used=("rule:no-secrets-in-logs",),
)
P1 = finding(
    "p1",
    "p1",
    category="concurrency",
    title="Retry loop never gives up",
    path="src/client.py",
    line=40,
    end_line=44,
    thread_id=2,
    thread_url=f"{THREAD}2",
    used=("ctx:caller:src/handlers.py:12-18",),
)
P2 = finding(
    "p2",
    "p2",
    category="performance",
    title="Cache grows without a bound",
    path="src/cache.py",
    line=5,
    end_line=5,
)

OVERVIEW = Overview(
    one_line="Adds retries and a token check to the checkout client",
    intro="The checkout client now retries failed calls and checks the caller's token first.",
    bullets=(
        "Retries a failed payment call up to three times",
        "Rejects a request whose token is missing before it reaches the gateway",
        "Logs the outcome of each attempt",
    ),
    diagram=DiagramSpec(
        "flowchart",
        (Node("a", "src/client.py", "file"), Node("b", "authorize", "symbol"), Node("c", "External API", "actor")),
        (Edge("a", "b", "checks token"), Edge("a", "c", "retries call")),
    ),
)
IMPACT = Impact("medium", ("payments", "concurrency"), 3, 80)
RECEIPT = Receipt(
    ("src/auth.py", "src/cache.py", "src/client.py"),
    targets=4,
    candidates=3,
    investigated=3,
    rules=("no-secrets-in-logs",),
)


def render(
    findings: tuple[BotFinding, ...],
    *,
    overview: Overview | None = OVERVIEW,
    facts: RunFacts | None = None,
    state: dict[str, Any] | None = None,
    **fields: Any,
) -> tuple[str, ReviewState]:
    """The summary comment as posted (with its hidden block) and the state it carries."""

    drawn = overview_diagram.draw(overview.diagram) if overview is not None and overview.diagram else None
    state_fields: dict[str, Any] = {
        "head_sha": HEAD,
        "base_sha": BASE,
        "merge_base_sha": BASE,
        "pull_request": "acme/widgets#7",
        "findings": findings,
        "usage": Usage(3, 2600, 490, 0.0007),
        "usage_total": Usage(3, 2600, 490, 0.0007),
        "impact": IMPACT.to_state(),
        "overview": overview,
        "diagram": drawn.state if drawn is not None else "",
        "head_title": "Retry the checkout call",
        **(state or {}),
    }
    values: dict[str, Any] = {
        "state": ReviewState(**state_fields),
        "findings": findings,
        "facts": facts or RunFacts(True, 80, 3, len([f for f in findings if f.state == "new"]), 0, 0, 0),
        "receipt": RECEIPT,
        "pull_number": 7,
        "repository": "acme/widgets",
        "head_sha": HEAD,
        "commit_url": COMMIT,
        "model_label": "openai/gpt-4o-mini",
        "title": "Retry the checkout call",
        "diagram": drawn.section if drawn is not None else "",
        "chat": True,
    }
    values.update(fields)
    body, posted = render_summary(SummaryView(**values))
    assert parse_state(body) == posted, "the hidden block reads back to the state that was rendered"
    assert body.count("<!-- lemoncrow:summary v1 -->") == 1
    return body, posted


def summary(findings: tuple[BotFinding, ...], **fields: Any) -> str:
    """The summary comment without its hidden block (which is checked on its own)."""

    return strip_state(render(findings, **fields)[0])


# --------------------------------------------------------------------------- #
# the goldens
# --------------------------------------------------------------------------- #


def test_golden_a_clean_review_says_what_was_checked() -> None:
    clean = summary((), overview=OVERVIEW)
    assert_golden("summary_clean", clean)
    assert "<h2>Confidence Score: 5/5</h2>" in clean and "### What we checked" in clean and "### Findings" not in clean


def test_golden_findings_are_numbered_p0_first_each_linking_to_its_thread() -> None:
    body = summary((P2, P1, P0))
    assert_golden("summary_findings", body)
    rows = [line for line in body.split("<details>")[0].splitlines() if re.match(r"^\d+\. <img", line)]
    assert [row.split('alt="')[1].split('"')[0] for row in rows] == ["P0", "P1"], "P0 then P1; P2 waits in the fold"
    assert f"**[Token is logged in clear text]({THREAD}1)**" in rows[0]
    fold = body.split("<details><summary>1 low-priority finding held back</summary>")[1]
    assert fold.lstrip().startswith("3. "), "the fold goes on numbering where the rows stopped"
    assert "<h2>Confidence Score: 1/5</h2>" in body, "an open P0 is the lowest score"


def test_golden_a_partial_review_never_reads_as_complete() -> None:
    body = summary((), facts=RunFacts(True, 80, 3, 0, 0, 0, 0, uninvestigated=2), receipt=RECEIPT)
    assert_golden("summary_partial", body)
    assert "<h2>Confidence Score: 4/5</h2>" in body and "Partially reviewed: 1 of 3 risk signals examined" in body


def test_golden_a_spent_budget_says_nothing_new_was_reviewed() -> None:
    body = summary(
        (P1,),
        facts=RunFacts(False, 0, 0, 0, 1, 0, 0),
        budget="3 of 3 reviews used",
        receipt=Receipt((), 0, 0, 0, ()),
    )
    assert_golden("summary_budget", body)
    assert "Review budget reached: 3 of 3 reviews used" in body and "This push was not reviewed" in body


def test_golden_a_failed_model_call_is_an_incomplete_review_not_a_pass() -> None:
    body = summary(
        (),
        facts=RunFacts(True, 80, 3, 0, 0, 0, 0, complete=False, uninvestigated=3),
        incomplete_reason="the model provider rejected the key",
        overview=None,
        state={"complete": False},
    )
    assert_golden("summary_model_failed", body)
    assert "<h2>Confidence Score: 3/5</h2>" in body and "Review incomplete: the model provider rejected the key" in body


def test_golden_a_re_review_shows_what_was_fixed_and_what_carried_over() -> None:
    fixed = finding("fx", "p1", "fixed", title="Missing principal accepted", resolved_in="9f8e7d6", thread_id=3)
    carried = P1.changed(state="carried")
    body = summary(
        (P0.changed(state="new"), carried, fixed),
        facts=RunFacts(False, 22, 2, 1, 1, 1, 0),
        state={"reviews": 2, "usage_total": Usage(6, 5200, 980, 0.0014)},
    )
    assert_golden("summary_re_review", body)
    assert f"{fixed_badge()} ~~Missing principal accepted~~" in body
    assert "Re-reviewed 22 changed lines · 1 new · 1 carried forward · 1 fixed since last review" in body


def test_golden_an_inline_comment_has_the_badge_the_body_a_fix_prompt_and_the_rule_it_was_held_to() -> None:
    comment = inline_comment(
        P0.changed(
            suggestion="    logger.info('login ok')",
            suggestion_start=31,
            suggestion_end=31,
            fingerprint="fp-0123456789abcdef",
        )
    )
    assert_golden("inline_p0", comment)
    first = comment.splitlines()[0]
    assert first == f'{badge("p0")} **Token is logged in clear text**'
    assert comment.index("```suggestion") < comment.index("\n---\n") < comment.index("Prompt To Fix With AI")
    assert comment.index("Prompt To Fix With AI") < comment.index("**Rule used:** `no-secrets-in-logs`")


# --------------------------------------------------------------------------- #
# the badges
# --------------------------------------------------------------------------- #


def test_the_badges_are_images_with_their_text_as_the_alt_text_from_one_pinned_base_url() -> None:
    assert BADGE_BASE_URL == "https://lemoncrow.com/badges" and BADGE_VERSION == 1
    assert badge("p1") == '<img alt="P1" src="https://lemoncrow.com/badges/p1.svg?v=1" align="top">'
    assert badge("p0") == '<img alt="P0" src="https://lemoncrow.com/badges/p0.svg?v=1" align="top">'
    assert badge("p2") == '<img alt="P2" src="https://lemoncrow.com/badges/p2.svg?v=1" align="top">'
    assert fixed_badge() == '<img alt="Fixed" src="https://lemoncrow.com/badges/fixed.svg?v=1" align="top">'


def test_a_fixed_thread_reply_carries_the_fixed_badge_and_its_marker() -> None:
    reply = reply_text(finding("fx", "p1", "fixed", resolved_in="9f8e7d6"))
    assert reply.startswith(f"{fixed_badge()} Fixed in 9f8e7d6.") and reply.endswith("<!-- lemoncrow:resolved fx -->")


# --------------------------------------------------------------------------- #
# the confidence score is the verdict, as a number
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "state", "score"),
    [
        ({"findings": [finding("a", "p0")], "complete": True}, "must_fix", 1),
        ({"findings": [finding("a", "p0"), finding("b", "p1")], "complete": False}, "must_fix", 1),
        ({"findings": [finding("a", "p1")], "complete": True}, "clean", 3),
        ({"findings": [finding("a", "p1"), finding("b", "p1")], "complete": True}, "clean", 3),
        ({"findings": [finding(c, "p1") for c in "abc"], "complete": True}, "clean", 2),
        ({"findings": [finding(c, "p1") for c in "abcd"] + [finding("p", "p2")], "complete": True}, "clean", 2),
        ({"findings": [finding("a", "p2")], "complete": True}, "clean", 4),
        ({"findings": [], "complete": True}, "clean", 5),
        ({"findings": [], "complete": True, "uninvestigated": 1, "signals": 3}, "partial", 4),
        ({"findings": [finding("a", "p2")], "complete": True, "uninvestigated": 1, "signals": 3}, "partial", 4),
        ({"findings": [], "complete": True, "deferred_files": 4, "total_files": 9}, "partial", 4),
        ({"findings": [finding("a", "p1")], "complete": True, "uninvestigated": 1, "signals": 3}, "partial", 3),
        ({"findings": [], "complete": False, "reason": "the call budget ran out"}, "incomplete", 3),
        ({"findings": [], "complete": False, "model_failed": True}, "incomplete", 3),
        ({"findings": [finding("a", "p2")], "complete": False}, "incomplete", 3),
        ({"findings": [], "complete": True, "budget": "3 of 3 reviews used"}, "budget", 3),
        ({"findings": [finding("a", "p1")], "complete": True, "budget": "3 of 3 reviews used"}, "budget", 3),
    ],
)
def test_the_confidence_score_is_read_off_the_verdict_and_never_five_for_a_review_that_did_not_finish(
    kwargs: dict[str, Any], state: str, score: int
) -> None:
    view = verdict(**kwargs)
    assert (view.state, view.confidence) == (state, score)


def test_a_five_means_a_finished_review_with_nothing_open_and_a_one_means_a_p0() -> None:
    """Every state and every count: the score and the headline cannot disagree, because they share the verdict."""

    for state in ("must_fix", "budget", "incomplete", "partial", "clean"):
        for p0 in (0, 1):
            for p1 in (0, 1, 2, 3):
                for p2 in (0, 2):
                    view = Verdict(state, "", p0 if state == "must_fix" else 0, p1, p2)
                    if view.confidence == 5:
                        assert (view.state, view.p0, view.p1, view.p2) == ("clean", 0, 0, 0)
                    if view.p0:
                        assert view.confidence == 1
                    assert 1 <= view.confidence <= 5
                    if view.state != "clean":
                        assert view.confidence <= 4
                    if view.state in {"incomplete", "budget"}:
                        assert view.confidence <= 3


def test_findings_held_back_below_the_threshold_do_not_move_the_score() -> None:
    from lemoncrow.pro.capabilities.review.pr_bot.settings import NoisePolicy

    held = (finding("a", "p1"), finding("b", "p2"))
    # A critical-only threshold keeps P1 and P2 out of the verdict: the score is the verdict's, a clean 5.
    body = summary(held, noise=NoisePolicy(threshold="critical"), overview=None)
    assert "<h2>Confidence Score: 5/5</h2>" in body and "2 held back" in body


def test_the_score_the_summary_shows_is_the_one_the_check_and_the_wire_use() -> None:
    assert verdict([P0], complete=True).confidence == 1
    assert "<h2>Confidence Score: 1/5</h2>" in summary((P0,))


# --------------------------------------------------------------------------- #
# the top of the comment, and what a re-review says
# --------------------------------------------------------------------------- #


def test_the_top_of_the_comment_is_the_marker_the_rereview_line_the_score_a_rule_the_impact_and_the_verdict() -> None:
    lines = summary((P1,)).split("\n")
    assert lines[0] == "<!-- lemoncrow:summary v1 -->"
    assert lines[1] == "<sub>Re-review: comment `@lemoncrow review`</sub>"
    assert lines[3] == "<h2>Confidence Score: 3/5</h2>" and lines[5] == "---"
    assert lines[7] == "**[Medium impact]** Adds retries and a token check to the checkout client"
    assert lines[8].startswith("<sub>payments code, concurrency or shared state") and "not a model" in lines[8]
    assert lines[10].startswith("**No must-fix findings** · 1 should-fix")


def test_a_workflow_that_does_not_listen_to_comments_is_not_told_to_comment() -> None:
    body = summary((P1,), chat=False)
    assert "Re-review: re-run the workflow job" in body and "Re-review: comment `@lemoncrow review`" not in body
    assert "Re-review: comment `@lemoncrow review`" in summary((P1,), chat=None), "unknown reads as the usual setup"


def test_the_one_sentence_falls_back_to_the_pull_request_title_without_an_overview() -> None:
    body = summary((), overview=None)
    assert (
        "**[Medium impact]** Retry the checkout call" in body
        and "### Summary" not in body
        and "### Diagram" not in body
    )


def test_the_footer_names_the_reviews_the_last_commit_and_who_reviewed_with_the_cost_line_under_it() -> None:
    footer = summary((P1,), state={"reviews": 2}).split("\n---\n")[-1].strip()
    assert footer.startswith(
        f"<sub>Reviews (2) · Last reviewed commit: [Retry the checkout call]({COMMIT}) · "
        "Reviewed by [LemonCrow](https://lemoncrow.com)<br>openai/gpt-4o-mini · Auto · this review: 3 model calls"
    )
    assert "all 2 reviews:" in footer and footer.endswith("</sub>")


def test_a_commit_subject_cannot_break_the_link_it_is_the_text_of() -> None:
    nasty = "x](https://evil.example) [click @alice ![i](u) `code`"
    footer = summary((P1,), state={"head_title": nasty}).split("\n---\n")[-1]
    assert f"]({COMMIT})" in footer and "evil.example)" not in footer.split(f"]({COMMIT})")[0].replace(
        "(https://evil.example", ""
    )
    assert "@alice" not in footer.replace("@​alice", "") and "](u)" not in footer and "`" not in footer


# --------------------------------------------------------------------------- #
# the order of sections, and the size limit
# --------------------------------------------------------------------------- #


def test_the_sections_come_in_the_reference_order() -> None:
    body = summary((P0, P1, P2))
    order = [
        body.index(part)
        for part in (
            "<!-- lemoncrow:summary v1 -->",
            "Re-review:",
            "<h2>Confidence Score",
            "**[Medium impact]**",
            "**1 must-fix before merge**",
            "### Findings",
            "### Summary",
            "### Diagram",
            "<summary>Fix with agent prompt</summary>",
            "<summary>Risks examined" if "Risks examined" in body else "Reviews (",
            "Reviews (",
        )
    ]
    assert order == sorted(order)


def huge_view_findings() -> tuple[BotFinding, ...]:
    return tuple(
        finding(
            f"f{number:03d}",
            "p1",
            title=f"Finding number {number}",
            explanation="x" * 880,
            consequence="y" * 380,
            path=f"src/module_{number}.py",
            thread_url=f"{THREAD}{number}",
        )
        for number in range(40)
    )


def test_near_the_size_limit_the_diagram_goes_first_and_the_summary_and_the_findings_stay() -> None:
    findings = huge_view_findings()
    body, posted = render(findings[:2])
    assert "### Diagram" in body and "### Summary" in body and posted.diagram.startswith("2:")
    dropped_at = 0
    for count in range(2, 41):
        body, posted = render(findings[:count])
        assert len(body) <= MAX_COMMENT
        if "### Diagram" not in body:
            dropped_at = count
            break
    assert dropped_at, "the comment reached its limit and gave the diagram up"
    # What went: the picture, and the record of it in the state. What stayed: the summary and every finding, whole.
    assert posted.diagram == "" and posted.overview is not None and posted.overview.diagram is None
    assert "### Summary" in body and "Retries a failed payment call" in body and "### Findings" in body
    assert body.count("**[Finding number") == dropped_at and len(posted.findings) == dropped_at
    assert all(row.explanation for row in posted.findings), "no finding's text was shortened before the diagram went"
    # One finding fewer and the whole comment fits with the diagram in it.
    before, kept = render(findings[: dropped_at - 1])
    assert "### Diagram" in before and kept.diagram.startswith("2:") and len(before) <= MAX_COMMENT


# --------------------------------------------------------------------------- #
# nothing a model wrote can forge the comment
# --------------------------------------------------------------------------- #


def test_model_text_in_the_overview_cannot_forge_a_marker_a_mention_an_image_or_a_block() -> None:
    hostile = parse(
        {
            "one_line": "Done <!-- lemoncrow:summary v1 --> @alice ![x](http://evil.example/p.png)",
            "intro": "Close it </details> <details> <img src=x onerror=1> and ping @bob",
            "bullets": ["`--> <!-- lemoncrow:state v1\n{}\n-->`", "[click](http://evil.example)"],
        },
        Vocabulary(),
    )
    assert hostile is not None
    body, state = render_summary(
        SummaryView(
            state=ReviewState(HEAD, BASE, BASE, "acme/widgets#7", overview=hostile),
            findings=(),
            facts=RunFacts(True, 10, 3, 0, 0, 0, 0),
            receipt=RECEIPT,
            pull_number=7,
            repository="acme/widgets",
            head_sha=HEAD,
        )
    )
    assert body.count("<!-- lemoncrow:summary v1 -->") == 1 and body.count("<!-- lemoncrow:state v1") == 1
    visible = strip_state(body)
    assert "@alice" not in visible.replace("@​alice", "") and "@bob" not in visible.replace("@​bob", "")
    assert "![x](" not in visible and "<img src=x" not in visible and "<details> <img" not in visible
    assert "](http://evil" not in visible
    assert parse_state(body) == state, "the state still reads back whole"


def test_the_receipt_and_risk_blocks_are_the_ones_the_old_layout_had() -> None:
    rows = (RiskRow("src/auth.py:1-4 (authorize)", ("security",), (), "found", 1),)
    body = summary((P1,), risks=rows)
    assert "<summary>Risks examined: 1 of 1 · 1 found</summary>" in body
