"""How a part is asked of a person: the pure classifier and its place in the queue.

The classifier is a pure function of checked facts, accepts by rule only for the two named rules,
and a Review-first fact always makes a part a decision. Nothing a reviewer marks changes the answer.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.answer_category import (
    ACCEPTED_BY_RULE_RULES,
    DEPENDENCY_DIRECT_ADDED_REASON,
    DEPENDENCY_INTEGRITY_REASON,
    DEPENDENCY_RESOLVED_VERSION_REASON,
    QUICK_CHECK,
    RULE_GENERATED_FILES,
    RULE_LOCKFILES,
    Answer,
    StructuralFacts,
    answer_counts,
    effective_answer,
    git_change_needs_person,
    lockfile_changes_only_versions,
    stamped_answer,
    structural_answer,
)
from lemoncrow.pro.capabilities.review.review_priority import (
    QueueReason,
    assign_review_priority,
)
from lemoncrow.pro.capabilities.review.routine import RoutineProof
from lemoncrow.pro.capabilities.review.session_models import (
    ANSWER_CATEGORIES,
    MARK_STATES,
)
from lemoncrow.pro.capabilities.review.targets import (
    ReviewTarget,
    answers_payload,
    progress_payload,
    review_progress,
    target_payload,
)

VERSION_MOVE = f"{DEPENDENCY_RESOLVED_VERSION_REASON}1.2.0 → 1.3.0"


def _facts(**kw: Any) -> StructuralFacts:
    base: dict[str, Any] = {"path": "src/app.py", "kind": "symbol"}
    base.update(kw)
    return StructuralFacts(**base)


def _target(
    key: str,
    *,
    path: str = "src/a.py",
    state: str = "unreviewed",
    structural: Answer = QUICK_CHECK,
    rank: int = 0,
) -> ReviewTarget:
    return ReviewTarget(
        target_id=f"target:{key}",
        unit_key=key,
        kind="symbol",
        path=path,
        label=f"{path}::{key}",
        symbol=key,
        start_line=1,
        end_line=1,
        hunk_ordinals=(0,),
        spans=(),
        state=state,  # type: ignore[arg-type]
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=rank,
        attention_level="normal",
        structural_level="normal",
        reasons=(),
        additions=1,
        deletions=0,
        fingerprint_method="symbol_body_sha256",
        structural=structural,
        answer=effective_answer(structural),
    )


# ---- the classifier ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("facts", "category", "rule"),
    [
        # Plain change: a person glances.
        (_facts(), "quick_check", ""),
        # Only the trusted base's `linguist-generated` marking accepts a file by rule.
        (_facts(path="gen/api.pb.go", kind="file", marked_generated=True), "accepted_by_rule", RULE_GENERATED_FILES),
        (_facts(path="gen/api.pb.go", kind="file"), "quick_check", ""),
        # A lockfile that only moves versions is accepted by the lockfiles rule.
        (
            _facts(path="uv.lock", kind="dependency", reasons=(VERSION_MOVE, DEPENDENCY_INTEGRITY_REASON)),
            "accepted_by_rule",
            RULE_LOCKFILES,
        ),
        # ... but a lockfile that adds a package is its own decision.
        (
            _facts(path="uv.lock", kind="dependency", reasons=(DEPENDENCY_DIRECT_ADDED_REASON, VERSION_MOVE)),
            "decision",
            "",
        ),
        (_facts(path="uv.lock", kind="dependency", reasons=("resolved lock entry changed",)), "decision", ""),
        (_facts(path="uv.lock", kind="dependency", reasons=(VERSION_MOVE, "package source changed")), "decision", ""),
        (
            _facts(path="uv.lock", kind="dependency", reasons=(f"{DEPENDENCY_RESOLVED_VERSION_REASON}? → 1.0",)),
            "decision",
            "",
        ),
        (_facts(path="uv.lock", kind="dependency", reasons=()), "decision", ""),
        # A manifest is never accepted by the lockfiles rule.
        (_facts(path="pyproject.toml", kind="dependency", reasons=(VERSION_MOVE,)), "decision", ""),
        # Special Git changes a person must see.
        (_facts(git_change="symlink", git_change_needs_person=True), "decision", ""),
        (_facts(git_change="submodule", git_change_needs_person=True), "decision", ""),
        (_facts(git_change="rename"), "quick_check", ""),
        # Unreadable content cannot be glanced at.
        (_facts(fingerprint_known=False), "decision", ""),
        # A generated marking never makes unreadable content acceptable: a person must see it.
        (_facts(marked_generated=True, fingerprint_known=False), "decision", ""),
        # ... but never over a special change.
        (_facts(marked_generated=True, git_change="symlink", git_change_needs_person=True), "decision", ""),
    ],
)
def test_structural_rules(facts: StructuralFacts, category: str, rule: str) -> None:
    answer = structural_answer(facts)
    assert (answer.category, answer.rule) == (category, rule)


def test_a_review_first_fact_makes_a_part_a_decision_and_drops_any_rule() -> None:
    accepted = structural_answer(_facts(marked_generated=True))
    assert accepted.category == "accepted_by_rule"
    final = effective_answer(accepted, review_first=True)
    assert (final.category, final.rule) == ("decision", "")
    assert effective_answer(structural_answer(_facts()), review_first=True).category == "decision"


def test_the_queue_can_only_raise_a_structural_answer() -> None:
    structurals = [Answer("decision", "", "x"), Answer("quick_check"), Answer("accepted_by_rule", RULE_LOCKFILES)]
    rank = {"quick_check": 0, "accepted_by_rule": 0, "decision": 1}
    for structural, review_first, routine in itertools.product(structurals, (False, True), ("", "comment_only")):
        final = effective_answer(structural, review_first=review_first, routine_rule=routine)
        assert rank[final.category] >= rank[structural.category]
        assert (final.category == "decision") == (structural.category == "decision" or review_first)
        if structural.category == "accepted_by_rule" and not review_first:
            assert final == structural


def test_a_routine_proof_is_the_reason_a_quick_check_carries_and_never_changes_the_category() -> None:
    quick = structural_answer(_facts())
    proven = effective_answer(quick, routine_rule="formatting_only")
    assert proven.category == "quick_check"
    assert proven.reason == "routine:formatting_only"
    assert effective_answer(quick).category == "quick_check"


def test_the_classifier_is_total_deterministic_and_names_a_rule_only_when_it_accepts() -> None:
    paths = ("src/app.py", "uv.lock", "package-lock.json", "yarn.lock", "pyproject.toml")
    reason_sets = ((), (VERSION_MOVE,), (VERSION_MOVE, DEPENDENCY_INTEGRITY_REASON), (DEPENDENCY_DIRECT_ADDED_REASON,))
    seen: set[tuple[str, str]] = set()
    for path, kind, known, generated, needs_person, reasons in itertools.product(
        paths, ("file", "hunk", "symbol", "dependency"), (True, False), (True, False), (True, False), reason_sets
    ):
        facts = StructuralFacts(
            path=path,
            kind=kind,
            fingerprint_known=known,
            marked_generated=generated,
            git_change_needs_person=needs_person,
            reasons=reasons,
        )
        first = structural_answer(facts)
        assert first == structural_answer(facts)
        assert first.category in ANSWER_CATEGORIES
        assert (first.category == "accepted_by_rule") == (first.rule != "")
        if first.rule:
            assert first.rule in ACCEPTED_BY_RULE_RULES
        seen.add((first.category, first.rule))
    assert seen == {
        ("decision", ""),
        ("quick_check", ""),
        ("accepted_by_rule", RULE_GENERATED_FILES),
        ("accepted_by_rule", RULE_LOCKFILES),
    }


def test_a_stamp_the_classifier_does_not_know_is_ignored() -> None:
    assert stamped_answer("", "") is None
    assert stamped_answer("decision-ish", "") is None
    assert stamped_answer("accepted_by_rule", "made_up_rule") is None
    assert stamped_answer("accepted_by_rule", RULE_LOCKFILES) == Answer(
        "accepted_by_rule", RULE_LOCKFILES, "rule:lockfiles"
    )
    # A rule on a category that does not accept is dropped, not trusted.
    assert stamped_answer("decision", RULE_LOCKFILES) == Answer("decision", "", "decided_at_capture")


def test_lockfile_version_only_reads_the_dependency_writers_own_spelling() -> None:
    assert lockfile_changes_only_versions([VERSION_MOVE])
    assert lockfile_changes_only_versions([VERSION_MOVE, DEPENDENCY_INTEGRITY_REASON])
    assert not lockfile_changes_only_versions([DEPENDENCY_INTEGRITY_REASON])
    assert not lockfile_changes_only_versions([])


def test_git_change_that_needs_a_person() -> None:
    assert git_change_needs_person({"kind": "symlink"})
    assert git_change_needs_person({"kind": "submodule"})
    assert git_change_needs_person({"kind": "mode", "executable": "added"})
    assert not git_change_needs_person({"kind": "mode", "executable": "removed"})
    assert not git_change_needs_person({"kind": "rename"})
    assert not git_change_needs_person(None)


def test_answer_counts_list_accepted_by_rule_per_rule_and_nothing_else_as_judged() -> None:
    counts = answer_counts(
        [
            Answer("decision"),
            Answer("quick_check"),
            Answer("quick_check"),
            Answer("accepted_by_rule", RULE_LOCKFILES),
            Answer("accepted_by_rule", RULE_LOCKFILES),
            Answer("accepted_by_rule", RULE_GENERATED_FILES),
        ]
    )
    assert counts == {
        "decision": 1,
        "quick_check": 2,
        "accepted_by_rule": 3,
        "rules": {RULE_GENERATED_FILES: 1, RULE_LOCKFILES: 2},
    }


# ---- the queue assigns the final answer ----------------------------------------------------


def test_assigning_priority_sets_the_final_answer_and_a_policy_path_is_never_accepted_by_rule() -> None:
    generated = Answer("accepted_by_rule", RULE_GENERATED_FILES)
    targets = (
        _target("plain", path="src/a.py"),
        _target("gen", path="gen/api.pb.go", structural=generated),
        _target("gen-on-policy-path", path="app/payments/gen.go", structural=generated),
    )
    policy = {"target:gen-on-policy-path": [QueueReason("review_first_policy", "Always a decision")]}
    ordered = assign_review_priority(targets, policy_reasons=policy)
    answers = {target.unit_key: target.answer for target in ordered}
    assert answers["plain"].category == "quick_check"
    assert (answers["gen"].category, answers["gen"].rule) == ("accepted_by_rule", RULE_GENERATED_FILES)
    assert (answers["gen-on-policy-path"].category, answers["gen-on-policy-path"].rule) == ("decision", "")


def test_a_routine_proof_labels_the_quick_check() -> None:
    proof = RoutineProof("src/a.py", "formatting_only", "python", "Formatting-only change")
    (target,) = assign_review_priority((_target("fmt"),), routine_proofs={"target:fmt": proof})
    assert (target.answer.category, target.answer.reason) == ("quick_check", "routine:formatting_only")


def test_the_answer_does_not_depend_on_the_reviewers_marks() -> None:
    by_state = {}
    for state in MARK_STATES:
        (target,) = assign_review_priority((_target("k", state=state),))
        by_state[state] = target.answer
    assert len(set(by_state.values())) == 1


def test_the_target_payload_and_overview_carry_the_answer() -> None:
    generated = Answer("accepted_by_rule", RULE_GENERATED_FILES)
    targets = assign_review_priority(
        (_target("a"), _target("gen", path="gen/x.go", structural=generated), _target("b", state="later"))
    )
    by_key = {target.unit_key: target for target in targets}
    assert target_payload(by_key["gen"])["answer"] == {
        "category": "accepted_by_rule",
        "rule": RULE_GENERATED_FILES,
        "rule_label": "generated files",
        "reason": "",
    }
    assert answers_payload(targets) == {
        "decision": 0,
        "quick_check": 2,
        "accepted_by_rule": 1,
        "rules": {RULE_GENERATED_FILES: 1},
    }
    progress = progress_payload(review_progress(targets))
    assert (progress["decision"], progress["quick_check"], progress["accepted_by_rule"]) == (0, 2, 1)
    # Progress stays flat integers: readiness folds it into int counts.
    assert all(isinstance(value, int) for value in progress.values())


def test_a_replaced_target_keeps_its_structural_answer() -> None:
    base = _target("k", structural=Answer("accepted_by_rule", RULE_LOCKFILES))
    assert replace(base, state="later").structural == base.structural


def test_what_an_ai_raised_orders_the_queue_but_never_moves_the_category() -> None:
    """SPEC section 1: AI hints only. The same part gets the same category with AI on or off."""
    generated = Answer("accepted_by_rule", RULE_GENERATED_FILES)
    finding = QueueReason("finding_unresolved", "AI finding · P0: injection")
    question = QueueReason("implementation_choice_unrecorded", "AI: an unrecorded choice")
    deterministic = QueueReason("removed_safeguard", "No longer rejects a percent over 100")

    def run(*, ai: bool, extra: bool = False) -> ReviewTarget:
        policy = {"target:gen": [finding] if ai else []}
        questions = {"target:gen": [question] if ai else []}
        if extra:
            policy["target:gen"].append(deterministic)
        raised = {"target:gen": [finding, question]} if ai else None
        (target,) = assign_review_priority(
            (_target("gen", structural=generated),),
            policy_reasons=policy,
            question_reasons=questions,
            ai_reasons=raised,
        )
        return target

    off, on = run(ai=False), run(ai=True)
    assert off.answer == on.answer == effective_answer(generated)
    # The queue still reads the AI reasons, exactly as before.
    assert on.priority.review_first is True and off.priority.review_first is False
    assert on.priority.tier == 3 and off.priority.tier == 4
    # A deterministic fact beside the AI ones still makes it a decision, with AI on or off.
    assert run(ai=False, extra=True).answer.category == run(ai=True, extra=True).answer.category == "decision"
    # Without telling the queue which reasons were AI's, the old reading holds (guards the parameter).
    (legacy,) = assign_review_priority(
        (_target("gen", structural=generated),), policy_reasons={"target:gen": [finding]}
    )
    assert legacy.answer.category == "decision"
