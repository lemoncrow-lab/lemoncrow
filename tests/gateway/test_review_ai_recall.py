"""What the pull request investigator is allowed to report, and what stops a real finding from reaching the page.

The checks that bind a finding to its evidence run on the model's answer after the call has been paid for, so a check
that is too strict throws away a finding that cost money to make. These tests pin the cases that matter: evidence from
another file of the same call, which is how a defect that spans two files is shown, and the instructions that tell the
model to report every defect it can support rather than the first.
"""

from __future__ import annotations

from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import ai_review
from lemoncrow.pro.capabilities.review.ai_review import (
    EvidenceRegistryEntry,
    ReviewHypothesis,
    _finding_from_row,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import IntelligenceRunOutcome, ModelCallUsage

TARGET_A = "target:sym:aaaaaaaa"
TARGET_A2 = "target:hun:a2a2a2a2"
TARGET_B = "target:sym:bbbbbbbb"
TARGET_C = "target:sym:cccccccc"
SOURCE_A = "source:new:pkg/a.py@sha256:" + "a" * 64
SOURCE_B = "source:new:pkg/b.py@sha256:" + "b" * 64
SOURCE_OLD_B = "source:old:pkg/b.py@sha256:" + "c" * 64


def _outcome() -> IntelligenceRunOutcome:
    return IntelligenceRunOutcome(
        run_id="run-1",
        status="ready",
        result={},
        driver="claude",
        model="sonnet",
        usage=ModelCallUsage(),
        provider_calls_this_request=1,
        duration_ms=1,
        cache_hit=False,
    )


def _hypothesis(*target_ids: str) -> ReviewHypothesis:
    return ReviewHypothesis(
        id="hyp-1",
        category="correctness",
        target_ids=tuple(target_ids),
        failure_mode="the change may be wrong",
        investigation_rationale="",
        priority_rationale="",
        evidence_needed="",
        general=True,
    )


def _registry() -> tuple[EvidenceRegistryEntry, ...]:
    """Three targets in two files, the way one batched call carries them: a target ref each, a source ref per file."""

    def entry(ref: str, kind: Any, targets: tuple[str, ...], path: str) -> EvidenceRegistryEntry:
        return EvidenceRegistryEntry(ref=ref, kind=kind, label=ref, target_ids=targets, path=path)

    return (
        entry(f"target:{TARGET_A}", "target", (TARGET_A,), "pkg/a.py"),
        entry(f"target:{TARGET_A2}", "target", (TARGET_A2,), "pkg/a.py"),
        entry(f"target:{TARGET_B}", "target", (TARGET_B,), "pkg/b.py"),
        entry(SOURCE_A, "source", (TARGET_A, TARGET_A2), "pkg/a.py"),
        entry(SOURCE_B, "source", (TARGET_B,), "pkg/b.py"),
        entry(SOURCE_OLD_B, "source", (TARGET_C,), "pkg/b.py"),
    )


def _row(refs: list[str], targets: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": "confirmed",
        "category": "correctness",
        "target_ids": targets or [TARGET_A],
        "title": "A wrong result",
        "explanation": "A concrete input gives a wrong result.",
        "consequence": "The caller gets the wrong value.",
        "evidence_refs": refs,
        "rule_refs": [],
        "severity": "medium",
        "lines": None,
        "replacement": None,
    }


def _finding(row: dict[str, Any]) -> Any:
    return _finding_from_row(
        row, _outcome(), _hypothesis(TARGET_A, TARGET_A2, TARGET_B, TARGET_C), _registry(), (), any_category=True
    )


def test_a_finding_may_cite_the_exact_source_of_another_file_of_the_same_call() -> None:
    """A defect that only shows against a second changed file is evidenced by both files' exact source."""

    finding = _finding(_row([SOURCE_A, SOURCE_B]))

    assert finding is not None
    assert finding.target_ids == (TARGET_A,)
    assert finding.evidence_refs == (SOURCE_A, SOURCE_B)


def test_the_other_files_source_is_not_independent_evidence() -> None:
    """Citing another changed file never lifts a finding toward a must-fix: that still needs a check or a context excerpt."""

    finding = _finding(_row([SOURCE_A, SOURCE_B]))

    assert finding is not None
    assert finding.verified is False


def test_a_finding_whose_evidence_is_all_about_other_targets_is_refused() -> None:
    """The evidence must still be about the finding: citing only another file's source says nothing about its targets."""

    assert _finding(_row([SOURCE_B])) is None
    assert _finding(_row([SOURCE_B, SOURCE_OLD_B])) is None


def test_the_target_ref_of_another_target_is_still_refused() -> None:
    """A target ref names the unit itself: a finding cites its own, so a stranger's is a wrong binding."""

    assert _finding(_row([SOURCE_A, f"target:{TARGET_B}"])) is None


def test_evidence_outside_the_calls_targets_is_refused() -> None:
    registry = (
        *_registry(),
        EvidenceRegistryEntry("source:new:pkg/z.py@sha256:" + "z" * 64, "source", "z", ("target:sym:zz",)),
    )
    row = _row([SOURCE_A, "source:new:pkg/z.py@sha256:" + "z" * 64])

    finding = _finding_from_row(row, _outcome(), _hypothesis(TARGET_A, TARGET_B), registry, (), any_category=True)

    assert finding is None


@pytest.mark.parametrize("refs", [[SOURCE_A], [f"target:{TARGET_A}"], [f"target:{TARGET_A}", SOURCE_A]])
def test_a_finding_citing_its_own_code_is_accepted_as_before(refs: list[str]) -> None:
    assert _finding(_row(refs)) is not None


def test_the_instructions_ask_for_every_defect_and_do_not_discourage_a_small_one() -> None:
    rules = ai_review._PR_OTHER_RULES
    sources = ai_review._PR_DEFECT_SOURCES

    assert "every distinct defect you can support" in rules
    assert str(ai_review.MAX_OTHER_FINDINGS) in rules
    assert ai_review.MAX_OTHER_FINDINGS >= 5
    assert "moves or copies" in rules  # code the change relocates is the change's own
    assert "rate it low" in sources and "Do not leave a defect out because its effect is small" in sources
    assert "invented defect is the worst" not in sources
    assert "When unsure, rate lower" not in ai_review._PR_FIELD_RULES


def test_both_kinds_of_look_carry_the_defect_checklist() -> None:
    for rules in (ai_review._PR_GENERAL_RULES, ai_review._PR_SPECIFIC_RULES):
        assert ai_review._PR_DEFECT_SOURCES in rules


# --------------------------------------------------------------------------- #
# message catalogues cost no call
# --------------------------------------------------------------------------- #


def _changed(path: str) -> Any:
    import dataclasses

    from tests.gateway.test_review_ai_review import _target

    return dataclasses.replace(_target(target_id="t", path=path, level="normal"), symbol="s", additions=3, deletions=0)


@pytest.mark.parametrize(
    "path",
    [
        "web/public/static/locales/en/common.json",
        "public/locales/en-US/app.json",
        "config/locales/en.yml",
        "src/i18n/de.po",
        "app/src/main/res/l10n/strings.xlf",
        "server/translations/messages_en.properties",
    ],
)
def test_a_message_catalogue_is_not_worth_a_call_and_its_name_is_no_risk(path: str) -> None:
    item = _changed(path)

    assert ai_review.not_worth_a_look(item) == "a translation file"
    assert ai_review.general_review_targets([item]) == ()
    assert ai_review.pr_candidates([item]) == ()


@pytest.mark.parametrize(
    "path",
    [
        "src/i18n/format.ts",
        "src/locale.py",
        "lib/locales/loader.rb",
        "web/locales_service/handler.json",
        "src/app/translations.go",
    ],
)
def test_code_that_lives_beside_a_catalogue_or_is_named_after_one_still_gets_a_look(path: str) -> None:
    assert ai_review.not_worth_a_look(_changed(path)) == ""
