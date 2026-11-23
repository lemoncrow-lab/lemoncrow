"""How a part is asked of a person: decision, quick check, or accepted by rule.

A pure, deterministic function of checked facts. No model call, no clock, no I/O,
and no reviewer input participates: the same facts always give the same answer.

The facts are not re-derived here. They arrive from the rules that already own them:

* ``review_priority`` decides whether a part's own reasons put it in Review first
  (a removed safeguard, wide reach, a policy path, a failed linked check, an open
  decision). That is ``review_first`` below, and it makes the part a **decision**.
  Path policy ("always a decision") reaches the part through that same flag, so a
  policy path is never a quick check and never accepted by rule.
* ``special_changes`` describes submodule, symlink and mode changes. A submodule
  pointer, a symlink and an added executable bit are decisions.
* ``routine`` proves a change is comment-only or formatting-only. The proof does not
  decide the category (such a part is a quick check either way); it is the reason
  the quick check carries.
* ``review_roles`` resolves ``linguist-generated`` from the trusted base. Only a file
  marked generated there is accepted by the ``generated_files`` rule.
* ``dependencies`` produces the semantic lockfile units and their reasons. A lockfile
  is accepted by the ``lockfiles`` rule only while every reason it carries is a
  resolved-version move; a new package, a changed source, a changed licence or a
  changed manifest declaration is a decision.

Two kinds of rule exist and no other: ``generated_files`` and ``lockfiles``. An
accepted-by-rule part is listed with its rule name and is never recorded as reviewed
or quick-checked by anything but a person's own explicit mark.

The structural answer (everything knowable from the captured change) is stamped on
the unit when the revision is recorded. The final answer adds ``review_first`` and
``routine_rule``, which only the queue knows, and can only move a part towards
``decision``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lemoncrow.pro.capabilities.review.policy_edit import is_policy_path
from lemoncrow.pro.capabilities.review.session_models import ANSWER_CATEGORIES, AnswerCategory

RULE_GENERATED_FILES = "generated_files"
RULE_LOCKFILES = "lockfiles"
ACCEPTED_BY_RULE_RULES: tuple[str, ...] = (RULE_GENERATED_FILES, RULE_LOCKFILES)
RULE_LABELS: dict[str, str] = {RULE_GENERATED_FILES: "generated files", RULE_LOCKFILES: "lockfiles"}

# The dependency reasons ``dependencies.derive_dependency_units`` writes. They live here so
# the writer and this classifier read one spelling.
DEPENDENCY_RESOLVED_VERSION_REASON = "resolved version "
DEPENDENCY_INTEGRITY_REASON = "integrity/checksum changed"
DEPENDENCY_DIRECT_ADDED_REASON = "direct dependency added"


@dataclass(frozen=True)
class Answer:
    """One part's category, the rule that accepted it (accepted by rule only) and why."""

    category: AnswerCategory
    rule: str = ""
    reason: str = ""

    @property
    def rule_label(self) -> str:
        return RULE_LABELS.get(self.rule, "")


QUICK_CHECK = Answer("quick_check")
"""The default answer: a person glances and confirms."""


@dataclass(frozen=True)
class StructuralFacts:
    """What the captured change alone says about one unit."""

    path: str
    kind: str = "file"
    fingerprint_known: bool = True
    marked_generated: bool = False
    #: ``special_changes`` kind (``submodule``, ``symlink``, ``mode``, ``rename``, ``metadata``) or ``""``.
    git_change: str = ""
    #: The special change needs a person: a symlink, a submodule pointer, an added executable bit.
    git_change_needs_person: bool = False
    #: ``ReviewUnit.reasons`` (dependency units carry their checked package facts here).
    reasons: tuple[str, ...] = ()


def git_change_needs_person(change: Mapping[str, Any] | None) -> bool:
    """Whether a ``special_changes.special_git_change`` result is a decision."""

    if not change:
        return False
    kind = change.get("kind")
    if kind in ("submodule", "symlink"):
        return True
    return kind == "mode" and change.get("executable") == "added"


#: Every lockfile this product knows by name. The ones ``dependencies`` can parse are a subset; the rest
#: (yarn, pnpm, Bundler, Go) are named here so none of them can be accepted by any rule but ``lockfiles``.
LOCKFILE_NAMES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "Gemfile.lock",
        "go.sum",
        "Cargo.lock",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "pdm.lock",
        "composer.lock",
        "Podfile.lock",
        "mix.lock",
        "gradle.lockfile",
        "packages.lock.json",
    }
)


def is_lockfile_path(path: str) -> bool:
    """Whether *path* is a lockfile. A lockfile is judged by the ``lockfiles`` rule or by a person, nothing else."""

    return path.replace("\\", "/").rsplit("/", 1)[-1] in LOCKFILE_NAMES or _is_supported_lockfile(path)


def _is_supported_lockfile(path: str) -> bool:
    # Imported here: ``dependencies`` imports ``units``, which imports this module.
    from lemoncrow.pro.capabilities.review.dependencies import supported_lockfile

    return supported_lockfile(path) is not None


def lockfile_changes_only_versions(reasons: Sequence[str]) -> bool:
    """True when a dependency unit's reasons show a resolved-version move and nothing else.

    Anything else (a new package, a changed source or licence, a declaration change, lock
    metadata, an unexplained lock entry) is not accepted: the lockfile "adds" something a
    person has not been shown.
    """

    moved = False
    for reason in reasons:
        if reason.startswith(DEPENDENCY_RESOLVED_VERSION_REASON):
            if "?" in reason:  # one side missing: a package appeared or vanished
                return False
            moved = True
        elif reason != DEPENDENCY_INTEGRITY_REASON:
            return False
    return moved


def structural_answer(facts: StructuralFacts) -> Answer:
    """The answer the captured change gives on its own, in rule order."""

    added = any(reason == DEPENDENCY_DIRECT_ADDED_REASON for reason in facts.reasons)
    if is_policy_path(facts.path):
        # A change to the rules file, CODEOWNERS or the allowed signers is its own decision (SPEC 4.19, P7-3):
        # never a quick check, never accepted by rule, whatever .gitattributes or the change's own copy says.
        return Answer("decision", "", "policy_file")
    if is_lockfile_path(facts.path):
        # Whatever .gitattributes says, a lockfile is accepted only by the lockfiles rule, and only once its
        # package facts prove the change is a version move. A lockfile nothing parsed is unproven: a person's.
        if (
            facts.kind == "dependency"
            and _is_supported_lockfile(facts.path)
            and lockfile_changes_only_versions(facts.reasons)
        ):
            return Answer("accepted_by_rule", RULE_LOCKFILES, "lockfile_version_only")
        return Answer("decision", "", "new_dependency" if added else "lockfile_unverified")
    if facts.kind == "dependency":
        return Answer("decision", "", "new_dependency" if added else "dependency_change")
    # Never accepted by a rule, even when marked generated: these carry something a person must see.
    if facts.git_change_needs_person:
        return Answer("decision", "", f"special_change:{facts.git_change}")
    if not facts.fingerprint_known:
        return Answer("decision", "", "content_not_fingerprinted")
    if facts.marked_generated:
        return Answer("accepted_by_rule", RULE_GENERATED_FILES, "marked_generated")
    return Answer("quick_check", "", "small_change")


def stamped_answer(category: str, rule: str) -> Answer | None:
    """The structural answer stored on a unit, or ``None`` when it is absent or not understood."""

    if category not in ANSWER_CATEGORIES:
        return None
    if category == "accepted_by_rule" and rule not in ACCEPTED_BY_RULE_RULES:
        return None
    # Only the category and rule are stored, so the reason is the one that follows from them.
    reason = {"accepted_by_rule": f"rule:{rule}", "decision": "decided_at_capture"}.get(category, "small_change")
    return Answer(category, rule if category == "accepted_by_rule" else "", reason)  # type: ignore[arg-type]


def effective_answer(
    structural: Answer,
    *,
    review_first: bool = False,
    routine_rule: str = "",
) -> Answer:
    """The part's final answer: the structural one, raised to ``decision`` by a Review-first fact.

    Never lowers a decision and never accepts by rule anything the structure did not.
    """

    if review_first:
        return Answer("decision", "", "review_first")
    if structural.category == "decision":
        return Answer("decision", "", structural.reason)
    if structural.category == "accepted_by_rule":
        return structural
    return Answer("quick_check", "", f"routine:{routine_rule}" if routine_rule else structural.reason or "small_change")


def answer_counts(answers: Sequence[Answer]) -> dict[str, Any]:
    """``decision`` / ``quick_check`` / ``accepted_by_rule`` counts plus the count per rule name."""

    counts: dict[str, Any] = {name: 0 for name in ANSWER_CATEGORIES}
    rules: dict[str, int] = {}
    for answer in answers:
        counts[answer.category] += 1
        if answer.category == "accepted_by_rule":
            rules[answer.rule] = rules.get(answer.rule, 0) + 1
    counts["rules"] = dict(sorted(rules.items()))
    return counts


def answer_payload(answer: Answer) -> dict[str, str]:
    return {"category": answer.category, "rule": answer.rule, "rule_label": answer.rule_label, "reason": answer.reason}


__all__ = [
    "ACCEPTED_BY_RULE_RULES",
    "DEPENDENCY_DIRECT_ADDED_REASON",
    "DEPENDENCY_INTEGRITY_REASON",
    "DEPENDENCY_RESOLVED_VERSION_REASON",
    "LOCKFILE_NAMES",
    "QUICK_CHECK",
    "RULE_GENERATED_FILES",
    "RULE_LABELS",
    "RULE_LOCKFILES",
    "Answer",
    "StructuralFacts",
    "answer_counts",
    "answer_payload",
    "effective_answer",
    "git_change_needs_person",
    "is_lockfile_path",
    "lockfile_changes_only_versions",
    "stamped_answer",
    "structural_answer",
]
