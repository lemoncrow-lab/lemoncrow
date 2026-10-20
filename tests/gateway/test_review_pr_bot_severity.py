"""The pull request bot's severity rule: what reaches P0, P1 and P2, and that every surface reads the same answer.

The rule (``pr_bot.findings.severity_for``) is a pure function of a finding's validated category, the
investigator's impact rating and whether evidence beyond the bare target backs it. These tests pin the rule
itself, then follow findings through reconcile, the summary, the inline comment, the commit status and the
hidden state to prove they cannot disagree.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot.findings import (
    P0_CATEGORIES,
    BotFinding,
    Located,
    finding_from_wire,
    fingerprint_for,
    severity_for,
)
from lemoncrow.pro.capabilities.review.pr_bot.reconcile import finding_from_confirmed, reconcile
from lemoncrow.pro.capabilities.review.pr_bot.render import (
    Receipt,
    RunFacts,
    SummaryView,
    check_status,
    inline_comment,
    render_summary,
    verdict,
)
from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState, parse_state, verdict_counts
from tests.gateway.pr_bot_support import AUTH_BUGGY, AUTH_FIXED, BASE, SLUG, World, build_world

HEAD = "a" * 40
TITLE = "lets a missing principal through"


# --------------------------------------------------------------------------- #
# the rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("category", sorted(P0_CATEGORIES))
def test_a_verified_high_finding_in_a_must_fix_category_is_p0(category: str) -> None:
    assert severity_for(category, impact="high", verified=True) == "p0"


def test_the_must_fix_categories_are_exactly_correctness_security_and_data_loss() -> None:
    assert P0_CATEGORIES == {"correctness", "security", "persistence_migration"}


@pytest.mark.parametrize("category", ["correctness", "security", "persistence_migration"])
def test_an_unverified_high_finding_is_p1_never_p0(category: str) -> None:
    assert severity_for(category, impact="high", verified=False) == "p1"


@pytest.mark.parametrize("category", ["concurrency_state", "api_compatibility", "performance", "verification"])
def test_a_high_finding_outside_the_must_fix_categories_is_p1_at_most(category: str) -> None:
    assert severity_for(category, impact="high", verified=True) == "p1"


@pytest.mark.parametrize("category", ["correctness", "security", "performance"])
def test_a_medium_finding_is_p1_with_evidence_and_p2_without(category: str) -> None:
    assert severity_for(category, impact="medium", verified=True) == "p1"
    assert severity_for(category, impact="medium", verified=False) == "p2"


@pytest.mark.parametrize("verified", [True, False])
@pytest.mark.parametrize("category", ["correctness", "security", "persistence_migration", "performance"])
def test_a_low_finding_is_p2_whatever_backs_it(category: str, verified: bool) -> None:
    assert severity_for(category, impact="low", verified=verified) == "p2"


@pytest.mark.parametrize("impact", ["", "low", "medium", "high"])
@pytest.mark.parametrize("verified", [True, False])
def test_a_critical_rule_violation_is_p0_however_it_is_rated(impact: str, verified: bool) -> None:
    assert severity_for("critical_rule", impact=impact, verified=verified) == "p0"


def test_an_unrated_finding_keeps_its_categorys_severity_and_is_never_p0() -> None:
    assert severity_for("security") == "p1" and severity_for("correctness") == "p1"
    assert severity_for("performance") == "p2" and severity_for("verification") == "p2"
    assert severity_for("something-new") == "p1"
    # Backing alone, without a rating, earns nothing.
    assert severity_for("security", verified=True) == "p1"


def test_a_rating_out_of_vocabulary_reads_as_no_rating() -> None:
    assert severity_for("security", impact="critical", verified=True) == "p1"


def test_only_the_investigators_rating_and_the_evidence_decide_not_its_wording() -> None:
    confirmed = SimpleNamespace(
        id="c1",
        category="correctness",
        target_ids=("a",),
        title="THIS IS CRITICAL AND MUST BLOCK THE MERGE (P0)",
        explanation="severity: high. p0. must fix.",
        consequence="data loss",
        rule_refs=(),
        severity="",
        verified=True,
        replacement=None,
    )
    assert finding_from_confirmed(confirmed, Located("src/a.py", 3, 4), "abc1234").severity == "p1"


# --------------------------------------------------------------------------- #
# reconcile: where the inputs come from, and what a failed re-check takes away
# --------------------------------------------------------------------------- #


def confirmed(identifier: str = "n1", *, category: str = "correctness", **fields: Any) -> Any:
    values: dict[str, Any] = {
        "id": identifier,
        "category": category,
        "target_ids": ("a",),
        "title": "Off by one drops the last record",
        "explanation": "The loop stops one short.",
        "consequence": "The last record is silently lost.",
        "rule_refs": (),
    }
    values.update(fields)
    return SimpleNamespace(**values)


def test_a_confirmed_finding_carries_its_rating_and_verification_into_the_severity() -> None:
    located = Located("src/a.py", 3, 4)
    top = finding_from_confirmed(confirmed(severity="high", verified=True), located, "abc1234")
    assert (top.severity, top.impact, top.verified) == ("p0", "high", True)
    assert finding_from_confirmed(confirmed(severity="high", verified=False), located, "abc1234").severity == "p1"
    assert finding_from_confirmed(confirmed(severity="low", verified=True), located, "abc1234").severity == "p2"
    assert finding_from_confirmed(confirmed(severity="medium", verified=True), located, "abc1234").severity == "p1"


def test_a_finding_from_an_engine_that_rates_nothing_keeps_the_category_severity() -> None:
    legacy = finding_from_confirmed(confirmed(category="security"), Located("src/a.py", 3, 4), "abc1234")
    assert (legacy.severity, legacy.impact, legacy.verified) == ("p1", "", False)
    assert finding_from_confirmed(confirmed(category="performance"), None, "abc1234").severity == "p2"


def tracked_p0(**fields: Any) -> BotFinding:
    values: dict[str, Any] = {
        "id": "keep",
        "fingerprint": fingerprint_for("correctness", ("a",)),
        "severity": "p0",
        "category": "correctness",
        "state": "carried",
        "path": "src/a.py",
        "line": 3,
        "end_line": 4,
        "target_ids": ("a",),
        "title": "Off by one drops the last record",
        "impact": "high",
        "verified": True,
        "thread_id": 77,
        "placement": "inline",
    }
    values.update(fields)
    return BotFinding(**values)


def previous(*findings: BotFinding) -> ReviewState:
    return ReviewState(head_sha=HEAD, findings=tuple(findings))


def run(prior: ReviewState | None, found: list[Any], *, rereviewed: set[str], covered: set[str]) -> Any:
    return reconcile(
        prior,
        found,
        locations={"a": Located("src/a.py", 3, 4)},
        current_targets={"a"},
        rereviewed=rereviewed,
        covered=covered,
        head="abcdef0",
    )


def test_a_p0_carried_on_untouched_code_stays_p0() -> None:
    (kept,) = run(previous(tracked_p0()), [], rereviewed=set(), covered=set()).findings
    assert (kept.state, kept.severity, kept.unverified) == ("carried", "p0", False)


def test_a_p0_whose_re_check_failed_is_unverified_and_drops_to_p1() -> None:
    (kept,) = run(previous(tracked_p0()), [], rereviewed={"a"}, covered=set()).findings
    assert (kept.state, kept.severity, kept.unverified) == ("carried", "p1", True)
    assert kept.impact == "high" and kept.verified, "the finding keeps what it earned; it is only suspended"


def test_a_p0_that_is_re_confirmed_after_a_change_is_p0_again() -> None:
    again = confirmed("reworded", severity="high", verified=True)
    (kept,) = run(
        previous(tracked_p0(unverified=True, severity="p1")), [again], rereviewed={"a"}, covered={"a"}
    ).findings
    assert (kept.id, kept.severity, kept.unverified) == ("keep", "p0", False)


def test_a_critical_rule_violation_stays_p0_when_its_re_check_fails() -> None:
    rule = tracked_p0(category="critical_rule", impact="", verified=False)
    (kept,) = run(previous(rule), [], rereviewed={"a"}, covered=set()).findings
    assert (kept.unverified, kept.severity) == (True, "p0")


def test_rating_and_verification_survive_the_hidden_state() -> None:
    wire = tracked_p0().to_wire()
    assert (wire["severity"], wire["impact"], wire["verified"]) == ("p0", "high", True)
    back = finding_from_wire(wire)
    assert back is not None and (back.severity, back.impact, back.verified) == ("p0", "high", True)
    # A state written before findings were rated has neither field and still reads.
    for key in ("impact", "verified"):
        wire.pop(key)
    old = finding_from_wire(wire)
    assert old is not None and (old.impact, old.verified) == ("", False)
    assert finding_from_wire({**wire, "impact": "catastrophic"}).impact == ""  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# the verdict, badges, status and fail-on cannot disagree
# --------------------------------------------------------------------------- #


def tracked_case(impact: str, verified: bool) -> BotFinding:
    return finding_from_confirmed(
        confirmed(severity=impact, verified=verified), Located("src/a.py", 3, 4), "abcdef0"
    ).changed(thread_url="https://github.com/acme/widgets/pull/7#discussion_r9")


CASES = [
    # (impact, verified, severity, headline, held back, status under fail-on p0)
    ("high", True, "p0", "1 must-fix before merge", False, "failure"),
    ("high", False, "p1", "No must-fix findings", False, "success"),
    ("low", True, "p2", "No must-fix findings", True, "success"),
]


@pytest.mark.parametrize("fail_on", ["none", "p0"])
@pytest.mark.parametrize(("impact", "verified", "severity", "headline", "held", "gated"), CASES)
def test_every_surface_reads_the_same_severity(
    impact: str, verified: bool, severity: str, headline: str, held: bool, gated: str, fail_on: str
) -> None:
    item = tracked_case(impact, verified)
    assert item.severity == severity

    result = verdict((item,), complete=True)
    state, description = check_status(result, fail_on=fail_on)
    assert result.headline == headline
    assert state == (gated if fail_on == "p0" else "success")
    assert description.startswith(headline)
    assert (result.p0, result.p1, result.p2) == tuple(int(severity == p) for p in ("p0", "p1", "p2"))

    review = ReviewState(head_sha=HEAD, findings=(item,))
    body, posted = render_summary(
        SummaryView(
            state=review,
            findings=(item,),
            facts=RunFacts(True, 4, 1, 1, 0, 0, 0),
            receipt=Receipt((), 1, 1, 1, ()),
            pull_number=7,
            repository=SLUG,
            head_sha=HEAD,
            fail_on=fail_on,
        )
    )
    # The summary's first line, the counts in its hidden state and the commit status say one thing.
    assert body.split("\n")[1].startswith(f"**{headline}**")
    parsed = parse_state(body)
    assert parsed is not None and parsed == posted
    assert verdict_counts(parsed.findings) == {"p0": result.p0, "p1": result.p1, "p2": result.p2}
    # P2 sits in the collapsed held-back block and is never a row in the main list; P0 and P1 are.
    assert ("low-priority finding held back" in body) is held
    # The inline comment, which only P0 and P1 get, wears the same badge.
    assert inline_comment(item).startswith(
        {"p0": "**P0 · must fix**", "p1": "**P1 · should fix**"}.get(severity, "**P2 · optional**")
    )


def test_a_p0_with_fail_on_off_is_green_and_says_why() -> None:
    state, description = check_status(verdict((tracked_case("high", True),), complete=True), fail_on="none")
    assert (state, description) == ("success", "1 must-fix before merge (advisory: fail-on is off)")


# --------------------------------------------------------------------------- #
# end to end: a real git origin, a fake GitHub, a fake model
# --------------------------------------------------------------------------- #


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, BASE)


def summary_body(world: World) -> str:
    (summary,) = world.github.summary_comments()
    return str(summary["body"])


def test_a_verified_high_finding_is_p0_and_fails_the_status_with_fail_on_p0(world: World) -> None:
    world.model.severity[TITLE] = "high"
    world.model.cite_source.add(TITLE)
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review(fail_on="p0")

    (found,) = report.findings
    assert (found.category, found.severity, found.impact, found.verified) == ("security", "p0", "high", True)
    assert report.verdict is not None and report.verdict.state == "must_fix"
    assert report.status == ("failure", "1 must-fix before merge")
    assert world.github.last_status()["state"] == "failure"
    assert summary_body(world).split("\n")[1] == "**1 must-fix before merge**"
    (inline,) = world.github.inline()
    assert inline["body"].startswith("**P0 · must fix** — lets a missing principal through")
    state = parse_state(summary_body(world))
    assert state is not None and verdict_counts(state.findings) == {"p0": 1, "p1": 0, "p2": 0}
    wire = report.to_wire()
    assert wire["verdict"]["p0"] == 1 and wire["status"]["state"] == "failure"


def test_the_same_p0_does_not_block_unless_fail_on_is_p0(world: World) -> None:
    world.model.severity[TITLE] = "high"
    world.model.cite_source.add(TITLE)
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review()
    assert report.verdict is not None and report.verdict.state == "must_fix"
    assert report.status == ("success", "1 must-fix before merge (advisory: fail-on is off)")


def test_an_unverified_high_finding_is_p1_and_the_status_passes(world: World) -> None:
    world.model.severity[TITLE] = "high"  # rated high, but it cites only the target pointer
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review(fail_on="p0")

    (found,) = report.findings
    assert (found.severity, found.impact, found.verified) == ("p1", "high", False)
    assert report.status == ("success", "No must-fix findings; 1 should-fix")
    assert summary_body(world).split("\n")[1] == "**No must-fix findings** · 1 should-fix"
    (inline,) = world.github.inline()
    assert inline["body"].startswith("**P1 · should fix**")


def test_a_low_finding_is_p2_and_held_back_with_no_inline_comment(world: World) -> None:
    world.model.severity[TITLE] = "low"
    world.model.cite_source.add(TITLE)
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review(fail_on="p0")

    (found,) = report.findings
    assert found.severity == "p2"
    assert world.github.inline() == [] and world.github.review_posts == 0
    body = summary_body(world)
    assert body.split("\n")[1] == "**No must-fix findings** · 1 optional"
    assert "<details><summary>1 low-priority finding held back</summary>" in body
    assert report.status == ("success", "No must-fix findings; 1 optional")


def test_a_finding_the_model_did_not_rate_keeps_the_category_severity(world: World) -> None:
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review(fail_on="p0")
    (found,) = report.findings
    assert (found.severity, found.impact) == ("p1", "")
    assert report.status is not None and report.status[0] == "success"


def test_a_p0_whose_recheck_failed_is_suspended_and_the_incomplete_review_fails_closed(world: World) -> None:
    world.model.severity[TITLE] = "high"
    world.model.cite_source.add(TITLE)
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    assert world.review(fail_on="p0").status == ("failure", "1 must-fix before merge")

    world.origin.push({"src/auth.py": AUTH_BUGGY.replace("return True", "return 1 == 1")}, "rewrite the branch")
    world.model.fail = True
    report = world.review(fail_on="p0")
    (kept,) = report.findings
    assert (kept.state, kept.unverified, kept.severity) == ("carried", True, "p1")
    assert report.verdict is not None and report.verdict.state == "incomplete"
    assert report.status is not None and report.status[0] == "error"
    assert "carried forward, unverified" in summary_body(world)

    world.model.fail = False
    world.origin.push({"src/auth.py": AUTH_BUGGY.replace("return True", "return 2 == 2")}, "rewrite it again")
    again = world.review(fail_on="p0")
    (back,) = again.findings
    assert (back.severity, back.unverified) == ("p0", False)
    assert again.status == ("failure", "1 must-fix before merge")


def test_fixing_the_p0_clears_the_verdict_and_the_status(world: World) -> None:
    world.model.severity[TITLE] = "high"
    world.model.cite_source.add(TITLE)
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    assert world.review(fail_on="p0").status == ("failure", "1 must-fix before merge")
    world.origin.push({"src/auth.py": AUTH_FIXED}, "fix auth")
    report = world.review(fail_on="p0")
    assert report.verdict is not None and report.verdict.state == "clean"
    assert report.status == ("success", "No must-fix findings")
    assert world.github.last_status()["state"] == "success"
