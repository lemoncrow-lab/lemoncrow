"""One-click suggested fixes (G7): a finding's replacement is proven against the Snapshot head before a comment may carry it.

Three layers, each tested on its own and then together:

* ``finding_replacement``: the shape a model may return and the check that it applies to the exact head;
* the investigator schema and validator, which must keep accepting a finding with no replacement at all;
* the bot: a checked replacement becomes a GitHub ``suggestion`` fence on a (multi-line) comment and a step in
  the "Fix with your agent" prompt, and a stale or out-of-hunk one is dropped while the finding stays.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.ai_review import (
    INVESTIGATION_SCHEMA,
    PR_INVESTIGATION_SCHEMA,
    ConfirmedFinding,
    EvidenceRegistryEntry,
    ReviewHypothesis,
    _validate_investigation,
    _with_checked_replacement,
    run_independent_review,
)
from lemoncrow.pro.capabilities.review.finding_replacement import (
    MAX_LINES,
    Replacement,
    hunk_headers,
    parse_replacement,
    verify_replacement,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import (
    IntelligenceRunOutcome,
    ModelCallUsage,
    StructuredExecution,
)
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding, Located
from lemoncrow.pro.capabilities.review.pr_bot.reconcile import finding_from_confirmed, reconcile
from lemoncrow.pro.capabilities.review.pr_bot.render import agent_prompt, badge, inline_comment
from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState, parse_state
from lemoncrow.pro.capabilities.review.targets import TargetSpan
from tests.gateway.pr_bot_support import AUTH_BUGGY, BASE, CACHE_BUGGY, World, build_world
from tests.gateway.test_review_ai_review import _execution, _store, _target

TITLE = "lets a missing principal through"
CACHE_TITLE = "grows without a bound"

SOURCE = "def authorize(user):\n    if user is None:\n        return True\n    return user.is_admin\n"
HUNKS = "@@ -1,2 +1,4 @@"


def claim(**fields: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "start_line": 2,
        "end_line": 3,
        "original": "    if user is None:\n        return True",
        "text": "    if user is None:\n        return False",
    }
    values.update(fields)
    return values


def checked(**fields: Any) -> Replacement | None:
    parsed = parse_replacement(claim(**fields))
    assert parsed is not None
    return verify_replacement(parsed, source=SOURCE, hunks=HUNKS)


# --------------------------------------------------------------------------- #
# the claim
# --------------------------------------------------------------------------- #


def test_a_well_formed_claim_is_parsed_but_is_not_yet_trusted() -> None:
    parsed = parse_replacement(claim())
    assert parsed == Replacement(
        2, 3, "    if user is None:\n        return True", "    if user is None:\n        return False"
    )
    assert parsed is not None and not parsed.verified


def test_trailing_newlines_are_not_part_of_the_replacement() -> None:
    parsed = parse_replacement(claim(text="    return False\n\n"))
    assert parsed is not None and parsed.text == "    return False"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "a string",
        [],
        {},
        claim(start_line=0),
        claim(start_line=3, end_line=2),
        claim(start_line="2"),
        claim(start_line=2.0),
        claim(start_line=True),
        claim(end_line=2 + MAX_LINES),  # longer than a small, local edit
        claim(original=""),
        claim(original="   \n"),
        claim(original=7),
        claim(text=""),
        claim(text="\n\n"),
        claim(text=None),
        claim(text="x" * 2001),
        claim(text="ok\0"),
        claim(text="x = '-->'"),  # could close an HTML comment and forge the bot's markers
        claim(text="<!-- lemoncrow:finding abc -->"),
    ],
)
def test_a_malformed_or_unsafe_claim_reads_as_no_replacement(raw: object) -> None:
    assert parse_replacement(raw) is None


# --------------------------------------------------------------------------- #
# the proof against the Snapshot head
# --------------------------------------------------------------------------- #


def test_a_replacement_that_matches_the_head_inside_a_hunk_is_verified() -> None:
    proven = checked()
    assert proven is not None and proven.verified and (proven.start_line, proven.end_line) == (2, 3)
    assert proven.original == "    if user is None:\n        return True"


def test_trailing_whitespace_in_the_quote_is_forgiven_but_indentation_is_not() -> None:
    assert checked(original="    if user is None:   \n        return True  ") is not None
    assert checked(original="  if user is None:\n      return True") is None


def test_a_stale_quote_is_dropped() -> None:
    # The model saw other code at these lines than the head holds.
    assert checked(original="    if user is None:\n        return False") is None
    assert checked(start_line=3, end_line=4) is None  # the range moved, the quote did not
    assert checked(original="    if user is None:") is None  # a different number of lines


def test_a_range_past_the_end_of_the_file_is_dropped() -> None:
    assert checked(start_line=4, end_line=5, original="    return user.is_admin\n") is None
    assert checked(start_line=40, end_line=41) is None


def test_a_range_outside_every_hunk_of_the_change_is_dropped() -> None:
    parsed = parse_replacement(claim())
    assert parsed is not None
    assert verify_replacement(parsed, source=SOURCE, hunks="@@ -1,2 +1,2 @@") is None  # hunk ends at line 2
    assert verify_replacement(parsed, source=SOURCE, hunks="") is None  # no hunk known at all
    assert verify_replacement(parsed, source=SOURCE, hunks="@@ -5 +5 @@") is None


def test_a_range_must_sit_inside_one_hunk_not_straddle_two() -> None:
    parsed = parse_replacement(claim())
    assert parsed is not None
    assert verify_replacement(parsed, source=SOURCE, hunks="@@ -1,2 +1,2 @@\n@@ -3,2 +3,2 @@") is None
    assert verify_replacement(parsed, source=SOURCE, hunks="@@ -1,2 +1,2 @@\n@@ -2,3 +2,3 @@") is not None


def test_an_edit_that_changes_nothing_is_dropped() -> None:
    assert checked(text="    if user is None:\n        return True   ") is None


@pytest.mark.parametrize(
    "source",
    [
        SOURCE.replace("\n", "\r\n"),  # CRLF: a suggestion's line endings are not ours to choose
        SOURCE.replace("return True", "return True\x0c"),  # a form feed splits lines in Python and not on GitHub
        SOURCE.replace("is None", "is None" + chr(0x2028)),
    ],
)
def test_a_file_whose_line_numbers_are_ambiguous_gets_no_suggestion(source: str) -> None:
    parsed = parse_replacement(claim())
    assert parsed is not None and verify_replacement(parsed, source=source, hunks=HUNKS) is None


def test_hunk_headers_come_from_the_stored_packet_files() -> None:
    files = [
        {"path": "a.py", "hunks": [{"header": "@@ -1,2 +1,4 @@"}, {"header": "@@ -9 +11,2 @@ def f():"}]},
        {"path": "b.py", "hunks": []},
        {"path": "c.py"},
    ]
    assert hunk_headers(files, "a.py") == "@@ -1,2 +1,4 @@\n@@ -9 +11,2 @@ def f():"
    assert hunk_headers(files, "b.py") == "" and hunk_headers(files, "c.py") == ""
    assert hunk_headers(files, "missing.py") == "" and hunk_headers(None, "a.py") == ""
    assert hunk_headers("not a list", "a.py") == ""


# --------------------------------------------------------------------------- #
# the schema and the validator
# --------------------------------------------------------------------------- #


def test_the_readers_investigation_schema_is_untouched_so_recorded_calls_stay_valid() -> None:
    assert (
        "severity" not in INVESTIGATION_SCHEMA["properties"] and "replacement" not in INVESTIGATION_SCHEMA["properties"]
    )
    assert "severity" not in INVESTIGATION_SCHEMA["required"]


def test_the_pull_request_schema_asks_for_a_rating_and_a_nullable_replacement() -> None:
    properties = PR_INVESTIGATION_SCHEMA["properties"]
    assert properties["severity"]["enum"] == ["low", "medium", "high"]
    assert {"type": "null"} in properties["replacement"]["anyOf"]
    assert set(INVESTIGATION_SCHEMA["properties"]) < set(properties)
    # Every property is required and closed, which strict structured-output routes demand.
    assert set(PR_INVESTIGATION_SCHEMA["required"]) == set(properties)
    assert PR_INVESTIGATION_SCHEMA["additionalProperties"] is False
    json.dumps(PR_INVESTIGATION_SCHEMA)
    assert "violated" not in json.dumps(PR_INVESTIGATION_SCHEMA)  # the fake model tells rule questions apart by it


def investigate(**result: Any) -> tuple[str, ConfirmedFinding | None]:
    hypothesis = ReviewHypothesis(
        id="h1",
        category="correctness",
        target_ids=("t1",),
        failure_mode="",
        investigation_rationale="",
        priority_rationale="",
        evidence_needed="",
    )
    registry = (
        EvidenceRegistryEntry("target:t1", "target", "the unit", ("t1",), "src/a.py"),
        EvidenceRegistryEntry("source:new:src/a.py@sha256:abc", "source", "Exact Snapshot source", ("t1",), "src/a.py"),
        EvidenceRegistryEntry("cross-repo:x", "cross_repository", "elsewhere", ("t1",)),
        EvidenceRegistryEntry("check:ci:unit", "check", "unit tests failed", ("t1",)),
        EvidenceRegistryEntry("artifact:a1", "artifact", "an attached log", ("t1",)),
        EvidenceRegistryEntry("static:semgrep:1", "static_finding", "a scanner hit", ("t1",)),
        EvidenceRegistryEntry("context:caller:src/b.py:1-3@sha256:1", "repo_context", "caller", ("t1",), "src/b.py"),
        EvidenceRegistryEntry("context:caller:src/a.py:9-9@sha256:2", "repo_context", "same file", ("t1",), "src/a.py"),
        EvidenceRegistryEntry("context:caller:unknown@sha256:3", "repo_context", "no known file", ("t1",)),
    )
    answer: dict[str, Any] = {
        "status": "confirmed",
        "category": "correctness",
        "target_ids": ["t1"],
        "title": "Off by one",
        "explanation": "The loop stops one short.",
        "consequence": "A record is lost.",
        "evidence_refs": ["target:t1"],
        "rule_refs": [],
    }
    answer.update(result)
    outcome = IntelligenceRunOutcome(
        run_id="run-1",
        status="ready",
        result=answer,
        driver="openai",
        model="m",
        usage=ModelCallUsage(calls=1),
        provider_calls_this_request=1,
        duration_ms=1,
        cache_hit=False,
    )
    return _validate_investigation(outcome, hypothesis, registry, ())


def test_a_finding_without_a_rating_or_a_replacement_still_validates() -> None:
    status, found = investigate()
    assert status == "confirmed" and found is not None
    assert (found.severity, found.verified, found.replacement) == ("", False, None)
    assert "severity" not in found.to_wire() and "replacement" not in found.to_wire()


def test_a_null_replacement_is_the_same_as_none() -> None:
    status, found = investigate(severity="medium", replacement=None)
    assert status == "confirmed" and found is not None
    assert (found.severity, found.replacement) == ("medium", None)


def test_the_rating_is_kept_only_when_it_is_in_the_vocabulary() -> None:
    assert investigate(severity="HIGH")[1].severity == "high"  # type: ignore[union-attr]
    assert investigate(severity="catastrophic")[1].severity == ""  # type: ignore[union-attr]
    assert investigate(severity=5)[1].severity == ""  # type: ignore[union-attr]


def verified(*refs: str) -> bool:
    status, found = investigate(evidence_refs=["target:t1", *refs])
    assert status == "confirmed" and found is not None
    return found.verified


def test_the_findings_own_target_and_exact_source_never_verify_it() -> None:
    # R1: every target has these two refs and the model is told which to cite, so they prove nothing.
    assert verified() is False
    assert verified("source:new:src/a.py@sha256:abc") is False
    # Related-repository context is never proof of this Snapshot (and cannot stand alone at all).
    assert verified("cross-repo:x") is False


@pytest.mark.parametrize("ref", ["check:ci:unit", "artifact:a1", "static:semgrep:1"])
def test_a_check_an_artifact_or_a_static_finding_verifies_a_finding(ref: str) -> None:
    assert verified(ref) is True


def test_an_excerpt_of_another_file_verifies_a_finding_but_the_findings_own_file_does_not() -> None:
    assert verified("context:caller:src/b.py:1-3@sha256:1") is True
    assert verified("source:new:src/a.py@sha256:abc", "context:caller:src/b.py:1-3@sha256:1") is True
    assert verified("context:caller:src/a.py:9-9@sha256:2") is False  # the finding's own file is not a second opinion
    assert verified("context:caller:unknown@sha256:3") is False  # an excerpt whose file is not known counts for nothing


def test_the_pull_request_prompt_does_not_tell_the_model_that_its_own_source_earns_a_must_fix() -> None:
    from lemoncrow.pro.capabilities.review.ai_review import _PR_FIELD_RULES

    assert "exact-source (source:)" not in _PR_FIELD_RULES
    assert "do not count" in _PR_FIELD_RULES and "repo_context excerpt of another file" in _PR_FIELD_RULES


def test_a_parsed_replacement_rides_on_the_finding_unverified() -> None:
    status, found = investigate(replacement=claim())
    assert status == "confirmed" and found is not None and found.replacement is not None
    assert not found.replacement.verified
    assert "replacement" not in found.to_wire(), "an unchecked claim never leaves the engine"


def test_a_malformed_replacement_never_costs_the_finding() -> None:
    status, found = investigate(replacement={"start_line": "two", "text": 3})
    assert status == "confirmed" and found is not None and found.replacement is None


def test_the_replacement_does_not_change_the_findings_identity() -> None:
    plain = investigate()[1]
    with_fix = investigate(replacement=claim(), severity="high")[1]
    assert plain is not None and with_fix is not None and plain.id == with_fix.id


def run_stage(tmp_path: Path, *, pr_fields: bool, answer: dict[str, Any]) -> tuple[Any, dict[str, Any], Any]:
    """One real investigation on the shared two-line fixture store; returns the result, what the model saw, the store."""

    store, revision = _store(tmp_path)
    seen: dict[str, Any] = {}

    def execute(**kwargs: Any) -> StructuredExecution:
        seen.update(schema=kwargs["output_schema"], system=kwargs["system"])
        registry = json.loads(kwargs["prompt"])["evidence_registry"]["value"]
        source_ref = next(row["ref"] for row in registry if row["kind"] == "source")
        return _execution(
            {
                "status": "confirmed",
                "category": "security",
                "target_ids": ["target-auth"],
                "title": "Authorization path accepts a missing principal",
                "explanation": "The changed branch reaches the privileged return without a principal guard.",
                "consequence": "An unauthenticated request can reach administrator behavior.",
                "evidence_refs": ["target:target-auth", source_ref],
                **answer,
            }
        )

    result = run_independent_review(
        store=store,
        revision=revision,
        targets=(_target(),),
        repo_root=tmp_path,
        effort="quick",
        driver="openai",
        model="review-model",
        structured_executor=execute,
        pr_fields=pr_fields,
    )
    return result, seen, store


def test_the_readers_review_asks_exactly_what_it_always_did(tmp_path: Path) -> None:
    result, seen, _ = run_stage(tmp_path, pr_fields=False, answer={"severity": "high", "replacement": claim()})
    assert seen["schema"] is INVESTIGATION_SCHEMA and "replacement" not in seen["system"]
    (found,) = result.machine_findings
    # An answer that volunteers the extras is read, but nothing is verified or posted for the Reader.
    assert found.replacement is not None and not found.replacement.verified


def test_a_pull_request_review_asks_for_a_rating_and_a_replacement(tmp_path: Path) -> None:
    result, seen, _ = run_stage(tmp_path, pr_fields=True, answer={"severity": "high", "replacement": None})
    assert seen["schema"] is PR_INVESTIGATION_SCHEMA
    assert "Also return severity" in seen["system"] and "Also return replacement: null unless" in seen["system"]
    (found,) = result.machine_findings
    # The answer cites the target and its own exact source: a rating, but not evidence from beyond the code.
    assert (found.severity, found.verified, found.replacement) == ("high", False, None)


def test_a_replacement_for_deleted_code_is_dropped_because_nothing_on_the_new_side_can_take_it(tmp_path: Path) -> None:
    store, revision = _store(tmp_path)
    deleted = replace(_target(), spans=(TargetSpan("old", 1, 2, 0),))
    parsed = parse_replacement({"start_line": 2, "end_line": 2, "original": "    return user.is_admin", "text": "x"})
    finding = ConfirmedFinding(
        id="f",
        run_id="r",
        category="security",
        target_ids=("target-auth",),
        title="t",
        explanation="e",
        consequence="c",
        evidence_refs=("target:target-auth",),
        rule_refs=(),
        driver="openai",
        model="m",
        replacement=parsed,
    )
    kept = _with_checked_replacement(store, revision, deleted, finding)
    assert kept is not None and kept.replacement is None and kept.title == "t"
    assert _with_checked_replacement(store, revision, None, finding).replacement is None  # type: ignore[union-attr]
    assert _with_checked_replacement(store, revision, deleted, None) is None


def test_a_replacement_that_cannot_be_proven_against_the_snapshot_is_dropped_and_the_finding_kept(
    tmp_path: Path,
) -> None:
    # The fixture store holds the source but no stored packet, so there is no hunk to place the lines in.
    fix = {
        "start_line": 2,
        "end_line": 2,
        "original": "    return user.is_admin",
        "text": "    return bool(user.is_admin)",
    }
    result, _, _ = run_stage(tmp_path, pr_fields=True, answer={"replacement": fix})
    (found,) = result.machine_findings
    assert found.replacement is None and found.title == "Authorization path accepts a missing principal"
    assert result.confirmed == 1


# --------------------------------------------------------------------------- #
# reconcile and the finding record
# --------------------------------------------------------------------------- #


def confirmed_with(replacement: Replacement | None, **fields: Any) -> Any:
    values: dict[str, Any] = {
        "id": "n1",
        "category": "security",
        "target_ids": ("a",),
        "title": "Problem",
        "explanation": "explained",
        "consequence": "consequence",
        "rule_refs": (),
        "replacement": replacement,
    }
    values.update(fields)
    return SimpleNamespace(**values)


def test_only_a_verified_replacement_becomes_a_suggestion() -> None:
    proven = checked()
    assert proven is not None
    where = Located("src/auth.py", 2, 3, "new")
    kept = finding_from_confirmed(confirmed_with(proven), where, "abcdef0")
    assert (kept.suggestion, kept.suggestion_start, kept.suggestion_end) == (proven.text, 2, 3)
    assert kept.has_suggestion and kept.span == (2, 3)

    claimed = parse_replacement(claim())
    assert not finding_from_confirmed(confirmed_with(claimed), where, "abcdef0").has_suggestion
    assert not finding_from_confirmed(confirmed_with(None), where, "abcdef0").has_suggestion
    # Nor on the old side of the diff, nor for a finding with no place on it.
    assert not finding_from_confirmed(
        confirmed_with(proven), Located("src/auth.py", 2, 3, "old"), "abcdef0"
    ).has_suggestion
    assert not finding_from_confirmed(confirmed_with(proven), None, "abcdef0").has_suggestion


def test_the_comment_covers_the_lines_the_suggestion_replaces_not_the_targets_first_lines() -> None:
    proven = checked(start_line=3, end_line=3, original="        return True", text="        return False")
    assert proven is not None
    item = finding_from_confirmed(confirmed_with(proven), Located("src/auth.py", 2, 3, "new"), "abcdef0")
    assert (item.line, item.end_line) == (2, 3) and item.span == (3, 3)
    without = item.changed(suggestion="", suggestion_start=0, suggestion_end=0)
    assert without.span == (2, 3)


def finding(**fields: Any) -> BotFinding:
    values: dict[str, Any] = {
        "id": "keep",
        "fingerprint": "fp-keep",
        "severity": "p1",
        "category": "security",
        "state": "carried",
        "path": "src/auth.py",
        "line": 2,
        "end_line": 3,
        "target_ids": ("a",),
        "title": "Problem",
        "suggestion": "    return False",
        "suggestion_start": 3,
        "suggestion_end": 3,
        "thread_id": 5,
        "placement": "inline",
    }
    values.update(fields)
    return BotFinding(**values)


def carry(item: BotFinding, where: Located) -> BotFinding:
    previous = ReviewState(head_sha="a" * 40, findings=(item,))
    result = reconcile(
        previous, [], locations={"a": where}, current_targets={"a"}, rereviewed=set(), covered=set(), head="abcdef0"
    )
    (kept,) = result.findings
    return kept


def test_a_carried_suggestion_survives_while_its_target_stays_put() -> None:
    assert carry(finding(), Located("src/auth.py", 2, 3, "new")).has_suggestion


def test_a_carried_suggestion_is_dropped_when_its_target_moved() -> None:
    # The suggestion's line numbers may now mean other code. The finding has a thread, which cannot move, so the
    # summary keeps pointing at lines 2-3, where the thread is.
    moved = carry(finding(), Located("src/auth.py", 12, 13, "new"))
    assert not moved.has_suggestion and (moved.line, moved.end_line) == (2, 3)
    renamed = carry(finding(), Located("src/renamed.py", 2, 3, "new"))
    assert not renamed.has_suggestion and (renamed.path, renamed.line) == ("src/auth.py", 2)
    # With no thread yet there is nothing to stay consistent with: the finding follows its code.
    unposted = carry(finding(thread_id=0, placement=""), Located("src/auth.py", 12, 13, "new"))
    assert not unposted.has_suggestion and (unposted.line, unposted.end_line) == (12, 13)


def test_a_suggestion_is_whole_or_absent_in_the_state() -> None:
    wire = finding().to_wire()
    assert (wire["suggestion"], wire["suggestion_start"], wire["suggestion_end"]) == ("    return False", 3, 3)
    assert (
        "suggestion" not in finding(suggestion="").to_wire()
        and "suggestion_start" not in finding(suggestion="").to_wire()
    )
    from lemoncrow.pro.capabilities.review.pr_bot.findings import finding_from_wire

    restored = finding_from_wire(wire)
    assert restored is not None and restored.has_suggestion
    for broken in (
        {**wire, "suggestion_start": 0},
        {**wire, "suggestion_end": 1},
        {**wire, "suggestion": ""},
        {**wire, "suggestion": "x" * 4001},  # never truncated into a different edit
    ):
        row = finding_from_wire(broken)
        assert row is not None and not row.has_suggestion and row.suggestion_start == row.suggestion_end == 0


def test_shrinking_the_state_never_cuts_a_suggestion_in_half() -> None:
    from lemoncrow.pro.capabilities.review.pr_bot.state import shrink

    long_fix = "\n".join(f"    line_{n} = {n}" for n in range(60))
    shrunk = shrink(ReviewState(head_sha="a" * 40, findings=(finding(suggestion=long_fix, suggestion_end=62),)), 3)
    (kept,) = shrunk.findings
    assert kept.suggestion == "" and not kept.has_suggestion
    small = shrink(ReviewState(head_sha="a" * 40, findings=(finding(),)), 3)
    assert small.findings[0].suggestion == "    return False"


# --------------------------------------------------------------------------- #
# the comment and the agent prompt
# --------------------------------------------------------------------------- #


def test_the_inline_comment_carries_a_suggestion_fence_with_the_exact_edit() -> None:
    comment = inline_comment(
        finding(suggestion="    if user is None:\n        return False", suggestion_start=2, suggestion_end=3)
    )
    assert "```suggestion\n    if user is None:\n        return False\n```" in comment
    assert (
        comment.index("```suggestion")
        < comment.index("Prompt To Fix With AI")
        < comment.index("LemonCrow AI review</sub>")
    )
    assert comment.endswith("<!-- lemoncrow:finding keep -->")


def test_the_agent_prompt_names_the_lines_and_the_edit_and_keeps_its_indentation() -> None:
    item = finding(suggestion="    if user is None:\n        return False", suggestion_start=2, suggestion_end=3)
    prompt = agent_prompt([item], pull_number=7, repository="acme/widgets", head_sha="a" * 40)
    assert "1. [P1] `src/auth.py:2-3` - Problem" in prompt or "1. [P1] src/auth.py:2-3 - Problem" in prompt
    assert "Suggested edit, checked against this commit: replace lines 2-3 with" in prompt
    assert "\n```\n    if user is None:\n        return False\n```" in prompt
    single = agent_prompt([finding()], pull_number=7, repository="acme/widgets", head_sha="a" * 40)
    assert "replace line 3 with" in single
    plain = agent_prompt([finding(suggestion="")], pull_number=7, repository="acme/widgets", head_sha="a" * 40)
    assert "Suggested edit" not in plain


# --------------------------------------------------------------------------- #
# end to end: a real git origin, a fake GitHub that keeps what it was sent, a fake model
# --------------------------------------------------------------------------- #


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, BASE)


def summary_body(world: World) -> str:
    (summary,) = world.github.summary_comments()
    return str(summary["body"])


#: Lines 2-3 of ``AUTH_BUGGY`` as the head holds them, and the edit that fixes them.
AUTH_FIX = {
    "start_line": 2,
    "end_line": 3,
    "original": "    if user is None:  # BUG: lets a missing principal through\n        return True",
    "text": "    if user is None:\n        return False",
}


def offer(world: World, **fix: Any) -> None:
    world.model.replacements[TITLE] = {**AUTH_FIX, **fix}


def test_a_verified_multi_line_replacement_becomes_a_suggestion_on_the_right_range(world: World) -> None:
    offer(world)
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review()

    (found,) = report.findings
    assert found.has_suggestion and found.span == (2, 3)
    (inline,) = world.github.inline()
    assert "```suggestion\n    if user is None:\n        return False\n```" in inline["body"]
    # The multi-line comment GitHub receives: it starts on line 2, ends on line 3, both on the new side.
    (review,) = world.github.posted_reviews
    (sent,) = review["comments"]
    assert {key: sent[key] for key in ("path", "side", "start_side", "start_line", "line")} == {
        "path": "src/auth.py",
        "side": "RIGHT",
        "start_side": "RIGHT",
        "start_line": 2,
        "line": 3,
    }
    assert review["event"] == "COMMENT" and review["commit_id"] == world.origin.head
    assert sent["body"] == inline["body"]


def test_the_suggestion_reaches_the_summary_prompt_the_state_and_the_report(world: World) -> None:
    offer(world)
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review()
    body = summary_body(world)
    assert "`src/auth.py:2-3`" in body
    assert "Suggested edit, checked against this commit: replace lines 2-3 with" in body
    assert "\n```\n    if user is None:\n        return False\n```\n" in body
    state = parse_state(body)
    assert state is not None
    (stored,) = state.findings
    assert (stored.suggestion, stored.suggestion_start, stored.suggestion_end) == (
        "    if user is None:\n        return False",
        2,
        3,
    )
    assert report.to_wire()["findings"][0]["suggestion_start"] == 2
    assert report.inline_bodies and "```suggestion" in report.inline_bodies[0]


def test_a_single_line_replacement_is_a_single_line_comment(world: World) -> None:
    offer(world, start_line=3, end_line=3, original="        return True", text="        return False")
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    world.review()
    (review,) = world.github.posted_reviews
    (sent,) = review["comments"]
    assert sent["line"] == 3 and "start_line" not in sent and "start_side" not in sent
    assert "```suggestion\n        return False\n```" in sent["body"]


@pytest.mark.parametrize(
    "fix",
    [
        {"original": "    if user is None:\n        return False"},  # the quote is not what the head holds
        {"start_line": 3, "end_line": 4},  # the model miscounted: the quote lives at 2-3
        {"start_line": 40, "end_line": 41},  # past the end of the file
        {"text": "    if user is None:  # BUG: lets a missing principal through\n        return True"},  # no change
        {"text": "    return False  # -->"},  # could close the bot's own markers
    ],
)
def test_a_stale_or_unsafe_replacement_is_dropped_and_the_finding_is_kept(world: World, fix: dict[str, Any]) -> None:
    offer(world, **fix)
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review()

    (found,) = report.findings
    assert not found.has_suggestion and found.title == TITLE and found.severity == "p1"
    (inline,) = world.github.inline()
    assert "suggestion" not in inline["body"] and inline["body"].startswith(badge("p1"))
    assert "Suggested edit" not in summary_body(world)
    # The comment stays on its target's first changed lines.
    (sent,) = world.github.posted_reviews[0]["comments"]
    assert (sent["start_line"], sent["line"]) == (2, 3)


def test_a_replacement_outside_the_hunks_of_the_change_is_dropped(world: World) -> None:
    # src/cache.py changes lines 2-5 only; line 1 exists, and the quote is right, but GitHub cannot attach to it.
    world.model.severity[CACHE_TITLE] = "high"  # a performance finding: P1 at most, however it is backed
    world.model.replacements[CACHE_TITLE] = {
        "start_line": 1,
        "end_line": 1,
        "original": "CACHE = {}",
        "text": "CACHE: dict = {}",
    }
    world.origin.push({"src/cache.py": CACHE_BUGGY})
    report = world.review()

    (found,) = report.findings
    assert found.severity == "p1" and not found.has_suggestion
    (inline,) = world.github.inline()
    assert "suggestion" not in inline["body"]


def test_the_same_replacement_inside_a_hunk_is_kept(world: World) -> None:
    world.model.severity[CACHE_TITLE] = "high"  # a performance finding: P1 at most, however it is backed
    world.model.replacements[CACHE_TITLE] = {
        "start_line": 5,
        "end_line": 5,
        "original": "    CACHE.setdefault(key, []).append(value)  # BUG: grows without a bound",
        "text": "    CACHE[key] = value",
    }
    world.origin.push({"src/cache.py": CACHE_BUGGY})
    report = world.review()
    (found,) = report.findings
    assert found.has_suggestion and found.span == (5, 5)
    (inline,) = world.github.inline()
    assert "```suggestion\n    CACHE[key] = value\n```" in inline["body"]


def test_github_showing_a_narrower_diff_than_the_snapshot_drops_the_suggestion_not_the_comment(
    world: World,
) -> None:
    # The Snapshot's hunk is lines 1-4. GitHub is served a diff whose only hunk ends at line 3, which can hold the
    # finding (lines 2-3) but not the lines the replacement covers (3-4).
    offer(
        world,
        start_line=3,
        end_line=4,
        original="        return True\n    return user.is_admin",
        text="        return False\n    return bool(user.is_admin)",
    )
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    world.origin.patches = lambda: [  # type: ignore[method-assign]
        {
            "filename": "src/auth.py",
            "patch": "@@ -1,2 +1,3 @@\n def authorize(user):\n+    if user is None:\n+        return True",
        }
    ]
    report = world.review()

    (found,) = report.findings
    assert found.has_suggestion, "the Snapshot proved it; only GitHub's diff could not hold it"
    (inline,) = world.github.inline()
    assert "suggestion" not in inline["body"]
    (sent,) = world.github.posted_reviews[0]["comments"]
    assert (sent["start_line"], sent["line"]) == (2, 3)
    # It is still a good fix for an agent that has the checkout.
    assert "Suggested edit, checked against this commit: replace lines 3-4 with" in summary_body(world)


def test_a_dry_run_shows_the_suggestion_it_would_post(world: World) -> None:
    offer(world)
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review(dry_run=True)
    assert world.github.review_posts == 0
    assert (
        report.inline_bodies
        and "```suggestion\n    if user is None:\n        return False\n```" in report.inline_bodies[0]
    )


def test_a_finding_with_no_replacement_is_posted_exactly_as_before(world: World) -> None:
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review()
    (found,) = report.findings
    assert not found.has_suggestion
    (inline,) = world.github.inline()
    assert "suggestion" not in inline["body"] and "Suggested edit" not in summary_body(world)
