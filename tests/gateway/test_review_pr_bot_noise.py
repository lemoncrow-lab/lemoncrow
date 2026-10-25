"""Noise controls for the pull request reviewer: the threshold, strictness, ignore globs, per-rule severity, summary-only
mode and the large-pull-request policy, all read from the trusted base and none of them able to hide a P0.

A real git origin, a fake GitHub and a fake model, like ``test_review_pr_bot_flow.py``. The default fixture gives a P1
(``src/auth.py``, a security finding) and a P2 (``src/cache.py``, a performance finding); with ``RULE`` the auth finding
is a P0 instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review.ai_review import ConfirmedFinding
from lemoncrow.pro.capabilities.review.criticality import (
    FINDING_THRESHOLDS,
    PolicyOrigin,
    parse_triage_settings,
    parse_trusted_base_triage,
    resolve_triage_policy,
    surfaced,
)
from lemoncrow.pro.capabilities.review.pr_bot import settings
from lemoncrow.pro.capabilities.review.pr_bot.engine import policy_digest
from lemoncrow.pro.capabilities.review.pr_bot.large_pr import plan_large
from lemoncrow.pro.capabilities.review.pr_bot.reconcile import finding_from_confirmed
from lemoncrow.pro.capabilities.review.pr_bot.runner import PrReviewError, RunReport
from lemoncrow.pro.capabilities.review.pr_bot.settings import NoiseInputs, NoisePolicy, parse_file_settings
from lemoncrow.pro.capabilities.review.pr_bot.state import parse_state
from lemoncrow.pro.capabilities.review.rules import ReviewGovernance, ReviewRule
from tests.gateway.pr_bot_noise_support import CONFIG, Recorder, clear_noise_env, review_yaml
from tests.gateway.pr_bot_support import (
    AUTH_BUGGY,
    BASE,
    CACHE_BUGGY,
    FIRST_PUSH,
    RULE,
    World,
    build_world,
)

AUTH_TITLE = "lets a missing principal through"
CACHE_TITLE = "grows without a bound"
RULE_KEY = "no-bug-markers"
RULES_FILE = "docs/review-rules.yaml"
IMPORTED = yaml.safe_dump(
    {"rules": [{"id": RULE_KEY, "rule": "Source must not carry known defects.", "scope": ["src/auth.py"]}]}
)
IMPORT_CONFIG = f"import:\n  rules_files: [{RULES_FILE}]\n"
IMPORTED_KEY = f"imported:{RULES_FILE}:{RULE_KEY}"


@pytest.fixture(autouse=True)
def _no_noise_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_noise_env(monkeypatch)


def world_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str, **files: str) -> World:
    return build_world(tmp_path, monkeypatch, {**BASE, CONFIG: config, **files})


def by_title(report: RunReport) -> dict[str, Any]:
    return {finding.title: finding for finding in report.findings}


def by_category(report: RunReport) -> dict[str, str]:
    """``{category: severity}``: with a rule in force the auth bug is found twice, as a rule violation and as a security finding."""

    return {finding.category: finding.severity for finding in report.findings}


def summary_of(world: World) -> str:
    (summary,) = world.github.summary_comments()
    return str(summary["body"])


def inline_titles(world: World) -> list[str]:
    return sorted(row["body"].split("—", 1)[1].split("\n", 1)[0].strip() for row in world.github.inline())


# --------------------------------------------------------------------------- #
# the threshold and strictness
# --------------------------------------------------------------------------- #


def test_without_any_setting_the_review_is_what_it_always_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push(FIRST_PUSH)
    report = world.review()
    body = summary_of(world)
    assert inline_titles(world) == [AUTH_TITLE]  # P1 inline, P2 only in the summary
    assert "<details><summary>1 low-priority finding held back</summary>" in body
    assert "**No must-fix findings** · 1 should-fix · 1 optional" in body
    assert "held back below" not in body and "Large pull request" not in body
    assert "held_back" not in report.to_wire()["verdict"]  # absent when nothing is held back
    assert report.state is not None and report.state.view == ""  # a default summary leaves no new field in the state


@pytest.mark.parametrize(
    ("config", "inline", "listed", "below"),
    [
        (review_yaml(strictness=1), [AUTH_TITLE, CACHE_TITLE], False, False),  # shows everything, P2 inline too
        (review_yaml(strictness=2), [AUTH_TITLE], True, False),  # the default: P2 held back in the collapsed block
        (review_yaml(strictness=3), [AUTH_TITLE], False, True),  # P2 never commented on: counted, below the threshold
        (review_yaml(finding_threshold="high"), [AUTH_TITLE], False, True),  # strictness 3 is this threshold
        (review_yaml(finding_threshold="suggestions"), [AUTH_TITLE, CACHE_TITLE], False, False),
    ],
)
def test_strictness_is_an_alias_for_the_finding_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str, inline: list[str], listed: bool, below: bool
) -> None:
    world = world_with(tmp_path, monkeypatch, config)
    world.origin.push(FIRST_PUSH)
    report = world.review()
    body = summary_of(world)
    assert inline_titles(world) == sorted(inline)
    assert ("low-priority finding held back" in body) is listed
    assert ("1 finding held back below strictness 3" in body) is below
    assert ("1 held back" in body.splitlines()[1]) is below  # counted in the headline, never silent
    if below:
        assert report.status is not None and "1 held back" in report.status[1]
        assert report.to_wire()["verdict"]["held_back"] == 1
        assert "cache.py" not in report.inline_bodies[0]
    # The finding is still tracked: lowering the threshold shows it again with no new review.
    assert CACHE_TITLE in by_title(report) and by_title(report)[CACHE_TITLE].is_open


def test_a_held_back_finding_is_in_no_verdict_count_and_not_in_the_fix_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(strictness=3))
    world.origin.push(FIRST_PUSH)
    report = world.review()
    body = summary_of(world)
    assert report.verdict is not None and (report.verdict.p1, report.verdict.p2, report.verdict.held) == (1, 0, 1)
    assert "1 optional" not in body
    prompt = body.split("Fix with your agent", 1)[1].split("</details>", 1)[0]
    assert "src/auth.py" in prompt and "src/cache.py" not in prompt
    held = body.split("held back below strictness 3", 1)[1].split("</details>", 1)[0]
    assert "src/cache.py:5" in held and "(strictness 3, from .lemoncrow/review.yaml)" in held


def test_a_critical_threshold_holds_back_p1_and_p2_but_never_a_p0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(finding_threshold="critical") + RULE)
    world.origin.push(FIRST_PUSH)
    report = world.review(fail_on="p0")
    assert by_category(report) == {"critical_rule": "p0", "security": "p1", "performance": "p2"}
    assert inline_titles(world) == [AUTH_TITLE]  # the P0 is posted whatever the threshold; the P1 and the P2 are not
    assert report.verdict is not None and report.verdict.state == "must_fix"
    assert report.verdict.held == 2  # the P1 and the P2; the P0 is not among the held
    assert report.status is not None and report.status[0] == "failure"
    assert "2 findings held back below threshold critical" in summary_of(world)


def test_a_threshold_that_holds_back_a_p1_still_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(finding_threshold="critical"))
    world.origin.push(FIRST_PUSH)
    report = world.review()
    body = summary_of(world)
    assert world.github.inline() == []  # neither the P1 nor the P2 is posted
    assert report.verdict is not None and report.verdict.held == 2
    assert "**No must-fix findings** · 2 held back" in body
    assert "2 findings held back below threshold critical" in body


def test_a_bad_strictness_is_named_and_the_default_applies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(strictness=7))
    world.origin.push(FIRST_PUSH)
    report = world.review()
    assert "config: " in summary_of(world) and "strictness must be 1, 2 or 3" in summary_of(world)
    assert any("strictness must be 1, 2 or 3" in note for note in report.notes)
    assert "low-priority finding held back" in summary_of(world)  # the default, not a guess


def test_strictness_and_a_threshold_together_use_the_threshold_and_say_so() -> None:
    origin = PolicyOrigin("trusted_base_repository", ".lemoncrow/review.yaml")
    layer = parse_triage_settings({"strictness": 1, "finding_threshold": "high"}, origin=origin)
    assert layer.finding_threshold == "high"
    assert any("not both" in error for error in layer.errors)
    assert parse_triage_settings({"strictness": True}, origin=origin).errors  # YAML yes/no is not a level
    assert parse_triage_settings({"strictness": 3}, origin=origin).finding_threshold == "high"


def test_the_threshold_decides_exactly_what_the_existing_surfaced_rule_decides() -> None:
    """The bot applies ``criticality.surfaced``; it does not keep a second table."""

    for threshold in FINDING_THRESHOLDS:
        policy = NoisePolicy(threshold=threshold)
        for severity in ("p0", "p1", "p2"):
            assert (policy.tier(severity) != "held") is surfaced(severity, threshold)  # type: ignore[arg-type]
        assert policy.tier("p0") == "row"


# --------------------------------------------------------------------------- #
# summary only
# --------------------------------------------------------------------------- #


def test_inline_comments_false_posts_nothing_on_the_diff_and_lists_everything_in_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(inline_comments=False) + RULE)
    world.origin.push(FIRST_PUSH)
    report = world.review(fail_on="p0")
    body = summary_of(world)
    assert world.github.inline() == [] and world.github.review_posts == 0
    assert report.inline_bodies == ()
    assert (
        "- **P0** lets a missing principal through — `src/auth.py:2-3` · new" in body
    )  # a row, with no thread to link
    assert "`inline_comments` is off" in body
    assert report.verdict is not None and report.verdict.state == "must_fix"
    assert (
        report.status is not None and report.status[0] == "failure"
    )  # the check does not depend on where it is posted
    assert all(finding.thread_id == 0 and not finding.placement for finding in report.findings)


def test_turning_inline_comments_back_on_posts_what_was_only_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push(FIRST_PUSH)
    monkeypatch.setenv("LEMONCROW_REVIEW_INLINE_COMMENTS", "false")
    world.review()
    assert world.github.inline() == []
    monkeypatch.delenv("LEMONCROW_REVIEW_INLINE_COMMENTS")
    world.review()  # the same head: the open P1 now gets its comment, and the summary says so
    assert inline_titles(world) == [AUTH_TITLE]
    assert "`inline_comments` is off" not in summary_of(world)


# --------------------------------------------------------------------------- #
# ignore
# --------------------------------------------------------------------------- #


def test_an_ignored_path_gets_no_finding_and_the_model_never_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(ignore=["src/cache.py"]))
    seen = world.model = Recorder(world.model)  # type: ignore[assignment]
    world.origin.push(FIRST_PUSH)
    report = world.review()
    assert set(by_title(report)) == {AUTH_TITLE}
    assert world.model.paths() == ["src/auth.py"]
    assert not any("src/cache.py" in json.dumps(prompt) for prompt in seen.prompts)
    assert not any(CACHE_TITLE in json.dumps(prompt) for prompt in seen.prompts)
    body = summary_of(world)
    assert "1 file left out by `review.ignore` (`src/cache.py`)" in body  # said, never a silent skip
    assert report.state is not None and not any(f.path == "src/cache.py" for f in report.state.findings)


def test_ignore_globs_use_the_content_access_mechanism_and_join_the_policy_digest() -> None:
    config = type(
        "Doc",
        (),
        {"content": review_yaml(ignore=["docs/**", "**/*.lock"]), "state": "present", "source": "s", "path": CONFIG},
    )()
    policy = settings.resolve_policy(config=config, triage=None, rules=())
    governed = settings.with_ignored_paths(ReviewGovernance(), policy, config)
    (row,) = governed.content_access
    assert (row.mode, row.path_scope, row.trust_class) == (
        "ai_denied",
        ("docs/**", "**/*.lock"),
        "trusted_base_repository",
    )
    assert row.authoritative and row.applies_to("docs/a/b.md") and not row.applies_to("src/a.py")
    assert policy.ignores("docs/guide/x.md") and policy.ignores("pkg/poetry.lock") and not policy.ignores("src/a.py")
    # A changed ignore list re-reviews once: it is in the policy digest through the content-access rows.
    assert policy_digest(governed, "auto") != policy_digest(ReviewGovernance(), "auto")


def test_an_ignored_caller_never_reaches_the_prompt_of_a_file_that_is_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = (
        "from src.auth import authorize\n\n\ndef handle(user):\n    # CALLER_MARKER_7731\n    return authorize(user)\n"
    )
    shown: dict[str, bool] = {}
    for name, config in (("open", ""), ("ignored", review_yaml(ignore=["docs/**"]))):
        (tmp_path / name).mkdir()
        files = {**BASE, "docs/handler.py": caller, **({CONFIG: config} if config else {})}
        world = build_world(tmp_path / name, monkeypatch, files)
        seen = world.model = Recorder(world.model)  # type: ignore[assignment]
        world.origin.push({"src/auth.py": AUTH_BUGGY})
        world.review()
        shown[name] = any("CALLER_MARKER_7731" in json.dumps(prompt) for prompt in seen.investigator_prompts())
    assert shown == {"open": True, "ignored": False}  # the control proves the marker can reach a prompt


def test_ignoring_everything_reviews_nothing_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(ignore=["**"]))
    world.origin.push(FIRST_PUSH)
    report = world.review()
    assert world.model.calls == [] and report.findings == ()
    assert "3 files left out by `review.ignore` (`**`)" in summary_of(world)


def test_an_ignore_that_covers_a_rules_files_says_the_rule_is_not_checked_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(ignore=["src/auth.py"]) + RULE)
    world.origin.push(FIRST_PUSH)
    report = world.review(fail_on="p0")
    assert f"review.ignore leaves out files in the scope of rule {RULE_KEY}; it is not checked there" in summary_of(
        world
    )
    assert all(finding.category != "critical_rule" for finding in report.findings)
    assert report.status is not None and report.status[0] == "success"  # the rule cannot gate a file that is ignored


@pytest.mark.parametrize("bad", ["../secrets", "/etc/passwd", 5, ""])
def test_a_bad_ignore_entry_is_named_and_skipped(bad: Any) -> None:
    parsed = parse_file_settings(review_yaml(ignore=["docs/**", bad]))
    assert parsed.ignore == ("docs/**",)
    assert any("review.ignore[1]" in note for note in parsed.notes)


# --------------------------------------------------------------------------- #
# per-rule severity
# --------------------------------------------------------------------------- #


def test_a_critical_rule_can_be_lowered_and_its_violation_is_then_not_a_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = review_yaml(rules={RULE_KEY: {"severity": "p1"}}) + RULE
    world = world_with(tmp_path, monkeypatch, config)
    world.origin.push(FIRST_PUSH)
    report = world.review(fail_on="p0")
    assert by_category(report) == {"critical_rule": "p1", "security": "p1", "performance": "p2"}
    assert report.verdict is not None and report.verdict.state != "must_fix"
    assert report.status is not None and report.status[0] == "success"
    assert inline_titles(world) == [AUTH_TITLE, AUTH_TITLE]  # both are P1 comments now


def test_an_advisory_rule_can_be_lowered_and_is_never_raised_past_p1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {RULES_FILE: IMPORTED}
    results: dict[str, str] = {}
    for name, level in (("none", None), ("p2", "p2"), ("p0", "p0")):
        (tmp_path / name).mkdir()
        config = IMPORT_CONFIG + (review_yaml(rules={IMPORTED_KEY: {"severity": level}}) if level else "")
        world = world_with(tmp_path / name, monkeypatch, config, **files)
        world.origin.push({"src/auth.py": AUTH_BUGGY})
        report = world.review(fail_on="p0")
        results[name] = by_category(report)["critical_rule"]
        assert report.status is not None and report.status[0] == "success"  # an advisory rule never gates
        if level == "p0":
            assert any("only a critical rule can be P0" in note for note in report.notes)
            assert "only a critical rule can be P0" in summary_of(world)
    assert results == {"none": "p1", "p2": "p2", "p0": "p1"}  # asking for P0 gives P1 and says why


def test_an_override_replaces_the_ladders_answer_for_a_rule_it_names() -> None:
    refs, overrides = ["rule:advisory:x"], {"rule:advisory:x": "p1"}
    low = {"impact": "low", "verified": False, "rule_refs": refs}
    assert settings.severity_with_overrides("critical_rule", overrides={}, **low) == "p2"  # type: ignore[arg-type]
    assert settings.severity_with_overrides("critical_rule", overrides=overrides, **low) == "p1"  # type: ignore[arg-type]
    assert (
        settings.severity_with_overrides(  # a rating of high, verified, in a category that can be P0: the rule's word stands
            "correctness", impact="high", verified=True, rule_refs=refs, overrides={"rule:advisory:x": "p2"}
        )
        == "p2"
    )


def test_naming_a_critical_rule_p0_changes_nothing_and_an_unknown_key_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = review_yaml(rules={RULE_KEY: {"severity": "p0"}, "no-such-rule": {"severity": "p1"}}) + RULE
    world = world_with(tmp_path, monkeypatch, config)
    world.origin.push(FIRST_PUSH)
    report = world.review(fail_on="p0")
    assert by_category(report)["critical_rule"] == "p0"
    assert any("review.rules.no-such-rule: no rule has this key" in note for note in report.notes)


def test_a_model_cannot_raise_a_rule_it_was_lowered_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = review_yaml(rules={RULE_KEY: {"severity": "p2"}}) + RULE
    world = world_with(tmp_path, monkeypatch, config)
    world.model.severity[AUTH_TITLE] = "high"
    world.model.cite_context.add(AUTH_TITLE)
    world.origin.push(FIRST_PUSH)
    assert by_category(world.review())["critical_rule"] == "p2"


def test_severities_from_the_file_are_validated() -> None:
    parsed = parse_file_settings(review_yaml(rules={"a": {"severity": "P1"}, "b": {"severity": "critical"}, "c": "p1"}))
    assert parsed.rule_severity == {"a": "p1"}
    assert len(parsed.notes) == 2 and all("review.rules." in note for note in parsed.notes)
    assert parse_file_settings(review_yaml(rules=["x"])).notes  # a list is the wrong shape here


def test_a_rule_severity_is_in_the_policy_digest() -> None:
    rule = ReviewRule(
        stable_key="k",
        instruction="i",
        path_scope=(),
        origin="o",
        trust_class="trusted_base_repository",
        source_identity="s",
    )
    governance = ReviewGovernance(rules=(rule,))
    plain = policy_digest(governance, "auto")
    assert policy_digest(governance, "auto", {}) == plain  # no override: the digest a repository had before
    assert policy_digest(governance, "auto", {rule.ref: "p1"}) != plain


def test_severity_with_overrides_follows_the_most_severe_cited_rule() -> None:
    low = {"rule:a": "p2", "rule:b": "p1"}
    both = settings.severity_with_overrides(
        "critical_rule", impact="", verified=False, rule_refs=["rule:a", "rule:b"], overrides=low
    )
    assert both == "p1"
    none = settings.severity_with_overrides("security", impact="high", verified=True, rule_refs=[], overrides=low)
    assert none == "p0"  # no overridden rule cited: the ordinary ladder
    mixed = settings.severity_with_overrides(
        "critical_rule", impact="", verified=False, rule_refs=["rule:a", "rule:other"], overrides=low
    )
    assert mixed == "p0"  # another critical rule is cited too, so its P0 stays


# --------------------------------------------------------------------------- #
# the trusted base, and the workflow's inputs
# --------------------------------------------------------------------------- #


HOSTILE_CONFIG = review_yaml(
    strictness=3, inline_comments=False, max_files=1, ignore=["src/**"], rules={RULE_KEY: {"severity": "p2"}}
)


def test_a_pull_request_cannot_loosen_its_own_review_by_editing_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "control").mkdir()
    (tmp_path / "hostile").mkdir()
    base = {**BASE, CONFIG: RULE}
    control = build_world(tmp_path / "control", monkeypatch, base)
    control.origin.push(FIRST_PUSH)
    expected = control.review(fail_on="p0")
    assert by_title(expected)[AUTH_TITLE].severity == "p0"

    world = build_world(tmp_path / "hostile", monkeypatch, base)
    seen = world.model = Recorder(world.model)  # type: ignore[assignment]
    # The change rewrites the config to hide everything: ignore all of src, drop P2, summary only, one file, no P0.
    world.origin.push({**FIRST_PUSH, CONFIG: RULE + HOSTILE_CONFIG})
    report = world.review(fail_on="p0")
    assert {f.title: f.severity for f in report.findings} == {f.title: f.severity for f in expected.findings}
    assert sorted(world.model.paths()) == sorted(control.model.paths())
    assert inline_titles(world) == inline_titles(control) and report.status == expected.status
    body = summary_of(world)
    assert "held back below" not in body and "left out by" not in body and "Large pull request" not in body
    assert not any("inline_comments" in note for note in report.notes)
    assert seen.investigator_prompts()


def test_a_config_the_pull_request_adds_where_the_base_has_none_governs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)  # the base has no .lemoncrow/review.yaml at all
    world.origin.push({**FIRST_PUSH, CONFIG: HOSTILE_CONFIG})
    report = world.review()
    body = summary_of(world)
    assert inline_titles(world) == [AUTH_TITLE]  # exactly the default review: the P1 inline ...
    assert "<details><summary>1 low-priority finding held back</summary>" in body  # ... the P2 held back, as ever
    assert "held back below" not in body and "left out by" not in body and "Large pull request" not in body
    assert (
        report.verdict is not None and report.verdict.held == 0 and report.state is not None and report.state.view == ""
    )


def test_the_base_config_keeps_governing_when_the_pull_request_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(strictness=3, inline_comments=False))
    world.origin.push({**FIRST_PUSH, CONFIG: "# no settings at all\n"})
    report = world.review()
    assert world.github.inline() == []
    assert report.verdict is not None and report.verdict.held == 1


def test_the_workflow_inputs_fill_in_where_the_file_is_silent_and_the_file_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "silent").mkdir()
    (tmp_path / "set").mkdir()
    monkeypatch.setenv("LEMONCROW_REVIEW_STRICTNESS", "3")
    monkeypatch.setenv("LEMONCROW_REVIEW_INLINE_COMMENTS", "false")
    silent = build_world(tmp_path / "silent", monkeypatch, BASE)
    silent.origin.push(FIRST_PUSH)
    report = silent.review()
    assert silent.github.inline() == [] and report.verdict is not None and report.verdict.held == 1
    assert "(strictness 3, from the strictness input)" in summary_of(silent)

    chosen = world_with(tmp_path / "set", monkeypatch, review_yaml(strictness=1, inline_comments=True))
    chosen.origin.push(FIRST_PUSH)
    chosen.review()
    assert inline_titles(chosen) == sorted([AUTH_TITLE, CACHE_TITLE])  # the file's strictness 1 and inline comments win


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("LEMONCROW_REVIEW_STRICTNESS", "4", "strictness must be 1, 2 or 3"),
        ("LEMONCROW_REVIEW_INLINE_COMMENTS", "maybe", "inline-comments must be true or false"),
        ("LEMONCROW_REVIEW_MAX_FILES", "-3", "max-files must be a whole number"),
    ],
)
def test_a_bad_workflow_input_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push(FIRST_PUSH)
    monkeypatch.setenv(name, value)
    with pytest.raises(PrReviewError, match=message):
        world.review()
    assert world.github.statuses == [] and world.github.issue_comments == []


def test_empty_inputs_are_not_set() -> None:
    assert NoiseInputs.from_env({"LEMONCROW_REVIEW_STRICTNESS": " ", "LEMONCROW_REVIEW_MAX_FILES": ""}) == NoiseInputs()
    assert NoiseInputs.from_env({"LEMONCROW_REVIEW_MAX_FILES": "0"}).max_files == 0


def test_a_changed_threshold_rewrites_the_summary_of_an_unchanged_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push(FIRST_PUSH)
    first = world.review()
    assert first.state is not None and first.state.view == ""
    monkeypatch.setenv("LEMONCROW_REVIEW_STRICTNESS", "3")
    calls = len(world.model.calls)
    again = world.review()  # the same commit, the job re-run after the workflow changed
    assert len(world.model.calls) == calls  # no model call: only what is shown changed
    body = summary_of(world)
    assert "1 finding held back below strictness 3" in body and "low-priority finding held back" not in body
    assert again.state is not None and again.state.view == "high:inline"
    assert again.state.reviews == first.state.reviews  # not another review
    assert "this review: 2 model calls" in body  # the cost of the review that was last made


def test_a_budget_notice_keeps_showing_what_the_last_review_showed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(strictness=3))
    world.origin.push(FIRST_PUSH)
    world.review(max_runs_per_pr=1)
    world.origin.push({"src/plain.py": "VALUE = 3\n"}, "second push")
    report = world.review(max_runs_per_pr=1)
    body = summary_of(world)
    assert "Review budget reached" in body
    assert "1 finding held back below strictness 3" in body and "low-priority finding held back" not in body
    assert report.verdict is not None and report.verdict.held == 1


# --------------------------------------------------------------------------- #
# a large pull request
# --------------------------------------------------------------------------- #

MANY = {
    "src/auth.py": AUTH_BUGGY,
    "src/cache.py": CACHE_BUGGY,
    "src/plain.py": "VALUE = 2\n",
    "src/a.py": "A = 1\n",
    "src/b.py": "B = 1\n",
}


LEFT = ("src/plain.py", "src/a.py", "src/b.py")


def test_above_max_files_the_riskiest_files_are_reviewed_first_and_the_rest_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(max_files=2))
    world.origin.push(MANY)
    report = world.review()
    body = summary_of(world)
    assert sorted(world.model.paths()) == ["src/auth.py", "src/cache.py"]  # the two files with a risk signal
    assert set(by_title(report)) == {AUTH_TITLE, CACHE_TITLE}
    assert "> **Large pull request: 5 files, over the limit of 2.** Reviewed the 2 highest-risk files first." in body
    assert "3 lower-risk files are not reviewed yet: " in body
    assert all(f"`{path}`" in body.split("not reviewed yet:", 1)[1].split("They are", 1)[0] for path in LEFT)
    assert body.splitlines()[1].startswith("**Partially reviewed: 2 of 5 files, highest risk first**")
    assert report.status is not None and report.status[0] == "success"
    assert report.status[1].startswith("Partially reviewed: 2 of 5 files, highest risk first")
    assert "No must-fix findings" not in body.splitlines()[1]
    assert any("large pull request: 3 of 5 files left for a later run" in note for note in report.notes)
    assert report.verdict is not None and report.verdict.state == "partial"


def test_the_files_left_over_are_reviewed_on_the_next_run_and_nothing_is_skipped_for_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(max_files=2))
    world.origin.push(MANY)
    first = world.review()
    assert first.state is not None
    reviewed_first = set(first.state.reviewed_units)
    second = world.review()  # the job re-run on the same commit
    assert second.state is not None and set(second.state.reviewed_units) > reviewed_first
    assert "Large pull request: 5 files, over the limit of 2" in summary_of(world)
    assert "Partially reviewed: 4 of 5 files, highest risk first" in summary_of(world)
    assert "1 lower-risk file is not reviewed yet: " in summary_of(world)
    third = world.review()
    assert third.state is not None and len(third.state.reviewed_units) == 5  # every file has been reviewed
    body = summary_of(world)
    assert "Large pull request" not in body and "**No must-fix findings**" in body
    assert [f.title for f in third.findings if f.is_open] == [f.title for f in first.findings if f.is_open]


def test_a_finding_on_a_file_that_was_left_over_is_carried_unverified_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(max_files=1))
    world.origin.push({"src/cache.py": CACHE_BUGGY})
    first = world.review()
    assert CACHE_TITLE in by_title(first)
    # Push two changes a lot of files and cache.py again; the limit keeps only the riskier auth.py now.
    changed_cache = CACHE_BUGGY.replace("append(value)", "append((key, value))")
    world.origin.push({"src/auth.py": AUTH_BUGGY, "src/cache.py": changed_cache}, "second")
    second = world.review()
    carried = by_title(second)[CACHE_TITLE]
    assert carried.is_open and carried.unverified  # its code changed and was not looked at again
    assert AUTH_TITLE in by_title(second)


def test_max_files_zero_means_no_limit_and_the_default_is_a_hundred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(max_files=0))
    world.origin.push(MANY)
    world.review()
    assert "Large pull request" not in summary_of(world)
    assert NoisePolicy().max_files == settings.DEFAULT_MAX_FILES == 100


def test_the_input_sets_the_limit_where_the_file_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push(MANY)
    monkeypatch.setenv("LEMONCROW_REVIEW_MAX_FILES", "3")
    world.review()
    assert "Large pull request: 5 files, over the limit of 3" in summary_of(world)


def test_a_policy_marked_critical_is_never_left_behind_a_file_with_a_marker() -> None:
    class Target:
        def __init__(self, target_id: str, path: str) -> None:
            self.target_id, self.path = target_id, path

    class Candidate:
        def __init__(self, target_id: str, score: int) -> None:
            self.target_ids, self.score = (target_id,), score

    targets = [
        Target("t1", "docs/readme.md"),
        Target("t2", "src/hot.py"),
        Target("t3", "ops/deploy.sh"),
        Target("t4", "src/x.py"),
    ]
    plan = plan_large(
        targets,
        limit=2,
        total_files=4,
        candidates=[Candidate("t2", 72)],
        criticality=lambda path: "critical" if path.startswith("ops/") else "normal",
        order={"t1": 0, "t2": 1, "t3": 2, "t4": 3},
    )
    assert plan is not None
    assert [t.path for t in plan.kept] == ["src/hot.py", "ops/deploy.sh"] or [t.path for t in plan.kept] == [
        "ops/deploy.sh",
        "src/hot.py",
    ]
    assert plan.deferred_paths == ("docs/readme.md", "src/x.py")  # equal risk: the queue's order decides
    assert {t.path for t in plan.kept} | {t.path for t in plan.deferred} == {t.path for t in targets}
    assert plan_large(targets, limit=4, total_files=4) is None and plan_large(targets, limit=0, total_files=4) is None


# --------------------------------------------------------------------------- #
# state and wire
# --------------------------------------------------------------------------- #


def test_the_view_of_a_summary_round_trips_and_a_forged_one_is_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_with(tmp_path, monkeypatch, review_yaml(strictness=3, inline_comments=False))
    world.origin.push(FIRST_PUSH)
    report = world.review()
    parsed = parse_state(summary_of(world))
    assert parsed is not None and parsed.view == "high:summary" == (report.state.view if report.state else "")
    assert NoisePolicy.from_view(parsed.view).tier("p2") == "held"
    forged = parse_state(summary_of(world).replace('"view":"high:summary"', '"view":"nonsense"'))
    assert forged is not None and forged.view == ""


def test_the_policy_resolution_names_where_each_value_came_from() -> None:
    class Doc:
        content = review_yaml(strictness=3)
        state = "present"
        source = "base Snapshot .lemoncrow/review.yaml"
        path = CONFIG

    layer = parse_trusted_base_triage(Doc.content, source=Doc.source)
    triage = resolve_triage_policy([layer])
    policy = settings.resolve_policy(config=Doc, triage=triage, rules=(), inputs=NoiseInputs(strictness=1))
    assert (policy.threshold, policy.threshold_source, policy.label) == (
        "high",
        ".lemoncrow/review.yaml",
        "strictness 3",
    )
    fallback = settings.resolve_policy(config=None, triage=None, rules=(), inputs=NoiseInputs(strictness=1))
    assert (fallback.threshold, fallback.threshold_source) == ("suggestions", "the strictness input")
    assert settings.resolve_policy(config=None, triage=None, rules=()).threshold_source == "default"


def test_an_intent_mismatch_is_never_more_than_a_p1() -> None:
    from lemoncrow.pro.capabilities.review.pr_bot.pr_intent import MISMATCH_PREFIX

    def confirmed(**fields: Any) -> ConfirmedFinding:
        base: dict[str, Any] = {
            "id": "finding-1",
            "run_id": "run-1",
            "category": "correctness",
            "target_ids": ("t1",),
            "title": "returns a stale total",
            "explanation": "x",
            "consequence": "y",
            "evidence_refs": ("target:t1",),
            "rule_refs": (),
            "driver": "openai",
            "model": "m",
            "severity": "high",
            "verified": True,
        }
        return ConfirmedFinding(**{**base, **fields})

    ordinary = finding_from_confirmed(confirmed(), None, "abc1234")
    mismatch = finding_from_confirmed(confirmed(intent_mismatch=True), None, "abc1234")
    assert ordinary.severity == "p0"  # a verified high correctness finding is a must-fix
    assert mismatch.severity == "p1" and mismatch.title == MISMATCH_PREFIX + "returns a stale total"
