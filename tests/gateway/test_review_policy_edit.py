"""P7-3: a change that edits the review policy is its own decision and is approved only by a CODEOWNER (SPEC 4.19)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.answer_category import StructuralFacts, structural_answer
from lemoncrow.pro.capabilities.review.policy_edit import (
    evaluate_gate,
    is_policy_path,
    policy_paths,
    read_policy_history,
)
from lemoncrow.pro.capabilities.review.second_reviewer import OwnerRef

MAYA = OwnerRef("usr_maya", "mchen", "Maya Chen")
DANA = OwnerRef("usr_dana", "dkim", "Dana Kim")


@pytest.mark.parametrize(
    "path",
    [
        ".lemoncrow/review.yaml",
        ".lemoncrow/allowed_signers",
        ".github/CODEOWNERS",
        "CODEOWNERS",
        "docs/CODEOWNERS",
        "./CODEOWNERS",
    ],
)
def test_the_policy_files_are_recognised(path: str) -> None:
    assert is_policy_path(path)


@pytest.mark.parametrize(
    "path", ["app/review.yaml", ".lemoncrow/other.yaml", "docs/CODEOWNERS.md", "src/CODEOWNERS", ""]
)
def test_other_files_are_not_policy(path: str) -> None:
    assert not is_policy_path(path)


def test_policy_paths_lists_each_once_in_a_fixed_order() -> None:
    assert policy_paths(["CODEOWNERS", "app/x.py", ".lemoncrow/review.yaml", "CODEOWNERS"]) == (
        ".lemoncrow/review.yaml",
        "CODEOWNERS",
    )


def test_a_policy_file_is_a_decision_even_when_marked_generated() -> None:
    for path in (".lemoncrow/review.yaml", "CODEOWNERS"):
        answer = structural_answer(StructuralFacts(path=path, marked_generated=True))
        assert (answer.category, answer.rule, answer.reason) == ("decision", "", "policy_file")
    # Not a policy file: unchanged behaviour.
    assert structural_answer(StructuralFacts(path="app/x.py")).category == "quick_check"


def test_only_a_codeowner_who_is_not_the_author_may_approve() -> None:
    owners = {".lemoncrow/review.yaml": [MAYA]}
    assert evaluate_gate(owners, viewer="usr_maya", barred=["usr_dana"], files=[".lemoncrow/review.yaml"]).allowed
    blocked = evaluate_gate(owners, viewer="usr_sam", barred=["usr_dana"], files=[".lemoncrow/review.yaml"])
    assert not blocked.allowed
    assert blocked.reason == "You can't approve this: needs Maya Chen (CODEOWNER of .lemoncrow/**)"
    assert [owner.name for owner in blocked.needs] == ["Maya Chen"]


def test_an_author_who_owns_the_file_cannot_approve_their_own_change() -> None:
    owners = {".lemoncrow/review.yaml": [DANA]}
    gate = evaluate_gate(owners, viewer="usr_dana", barred=["usr_dana"], files=[".lemoncrow/review.yaml"])
    assert not gate.allowed
    assert "no CODEOWNER" in gate.reason and gate.needs == ()


def test_with_no_owner_at_all_nobody_may_approve() -> None:
    gate = evaluate_gate({}, viewer="usr_sam", barred=[], files=[".lemoncrow/review.yaml"])
    assert (gate.allowed, gate.needs) == (False, ())
    assert gate.reason == "You can't approve this: no CODEOWNER of .lemoncrow/** is available to approve it"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=Maya Chen", "-c", "user.email=maya@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def test_the_history_lists_only_the_commits_that_changed_the_rules_file(tmp_path: Path) -> None:
    repo = tmp_path / "shop"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "review.yaml").write_text("version: 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Create review policy")
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Unrelated change")
    (repo / ".lemoncrow" / "review.yaml").write_text("version: 1\nrules: []\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Add public API routes to always a decision")
    head = _git(repo, "rev-parse", "HEAD")

    rows = read_policy_history(repo, head)
    assert rows is not None
    assert [(row["author"], row["subject"]) for row in rows] == [
        ("Maya Chen", "Add public API routes to always a decision"),
        ("Maya Chen", "Create review policy"),
    ]
    assert all(
        re.fullmatch(r"[0-9a-f]{7,}", row["commit"]) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"]) for row in rows
    )
    # Read at the review's base commit: a later commit is not in the history of an earlier base.
    first = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    older = read_policy_history(repo, first)
    assert older is not None and [row["subject"] for row in older] == ["Create review policy"]


def test_a_history_git_cannot_read_is_unknown_not_empty(tmp_path: Path) -> None:
    assert read_policy_history(tmp_path, "0" * 40) is None  # not a repository
    assert read_policy_history(tmp_path, "not-a-commit; rm -rf /") is None  # never reaches a shell or git
    assert read_policy_history(tmp_path / "missing", "a" * 40) is None


def test_owning_one_edited_policy_file_does_not_let_you_approve_the_others() -> None:
    sam = OwnerRef("usr_sam", "srivera", "Sam Rivera")
    owners = {"docs/CODEOWNERS": [sam], ".lemoncrow/review.yaml": [MAYA]}
    files = ["docs/CODEOWNERS", ".lemoncrow/review.yaml"]
    gate = evaluate_gate(owners, viewer="usr_sam", barred=[], files=files)
    assert not gate.allowed
    assert gate.reason == "You can't approve this: needs Maya Chen (CODEOWNER of .lemoncrow/**)"
    assert not evaluate_gate(owners, viewer="usr_maya", barred=[], files=files).allowed
    both = {"docs/CODEOWNERS": [MAYA], ".lemoncrow/review.yaml": [MAYA]}
    assert evaluate_gate(both, viewer="usr_maya", barred=[], files=files).allowed
    # An edited file nobody eligible owns is a missing scope, named as such.
    nobody = evaluate_gate({".lemoncrow/review.yaml": [MAYA]}, viewer="usr_maya", barred=[], files=files)
    assert nobody.reason == "You can't approve this: no CODEOWNER of docs/CODEOWNERS is available to approve it"


def test_owners_of_two_files_under_the_same_scope_may_not_approve_each_others_file() -> None:
    sam = OwnerRef("usr_sam", "srivera", "Sam Rivera")
    owners = {".lemoncrow/review.yaml": [MAYA], ".lemoncrow/allowed_signers": [sam]}
    files = [".lemoncrow/review.yaml", ".lemoncrow/allowed_signers"]
    for viewer in ("usr_sam", "usr_maya"):
        gate = evaluate_gate(owners, viewer=viewer, barred=[], files=files)
        assert not gate.allowed, viewer
    assert "CODEOWNER of .lemoncrow/**" in evaluate_gate(owners, viewer="usr_sam", barred=[], files=files).reason
    both = {".lemoncrow/review.yaml": [MAYA, sam], ".lemoncrow/allowed_signers": [sam]}
    assert evaluate_gate(both, viewer="usr_sam", barred=[], files=files).allowed


def test_no_named_edited_file_fails_closed() -> None:
    gate = evaluate_gate({".lemoncrow/review.yaml": [MAYA]}, viewer="usr_maya", barred=[], files=[])
    assert not gate.allowed and "can't be checked" in gate.reason


from lemoncrow.pro.capabilities.review.policy_edit import diff_question, owner_token  # noqa: E402

_RULES_PATCH = """--- a/.lemoncrow/review.yaml
+++ b/.lemoncrow/review.yaml
@@ -3,7 +3,6 @@ version: 1
 always_decision:
   - migrations/**
-  - app/payments/**
   - app/auth/**
 paths:
   docs: "docs/**"
"""


def test_the_diff_is_asked_as_a_question_under_the_key_the_file_names() -> None:
    assert diff_question(".lemoncrow/review.yaml", _RULES_PATCH) == "Remove `app/payments/**` from `always_decision`?"
    added = _RULES_PATCH.replace("-  - app/payments/**", "+  - app/billing/**")
    assert diff_question(".lemoncrow/review.yaml", added) == "Add `app/billing/**` to `always_decision`?"


def test_more_than_one_change_asks_the_first_and_counts_the_rest() -> None:
    both = _RULES_PATCH.replace(" paths:", "+  - app/refunds/**\n paths:")
    assert (
        diff_question(".lemoncrow/review.yaml", both)
        == "Remove `app/payments/**` from `always_decision`? (and 1 more change)"
    )


def test_codeowners_and_signers_lines_are_asked_by_their_first_field() -> None:
    assert diff_question("CODEOWNERS", "@@ -1 +1 @@\n-/app/payments/ @mchen\n+/app/payments/ @dana\n").startswith(
        "Remove the owners of `/app/payments/`?"
    )
    assert diff_question(".lemoncrow/allowed_signers", "@@ -1 +0,0 @@\n-sam ssh-ed25519 AAAA\n") == (
        "Remove `sam` from the allowed signers?"
    )


def test_a_diff_that_cannot_be_read_gives_the_plain_question_and_never_a_guess() -> None:
    assert diff_question(".lemoncrow/review.yaml", "") == "Accept the change to `.lemoncrow/review.yaml`?"
    assert diff_question(".lemoncrow/review.yaml", "@@ -1 +1 @@\n-version: 1\n+version: 2\n") == (
        "Accept the change to `.lemoncrow/review.yaml`?"
    )


def test_an_approving_viewer_is_told_who_the_approval_is_signed_as() -> None:
    gate = evaluate_gate(
        {".lemoncrow/review.yaml": [MAYA]}, viewer="usr_maya", barred=[], files=[".lemoncrow/review.yaml"]
    )
    wire = gate.to_wire()
    assert gate.allowed and wire["signer"] == {"name": "Maya Chen", "handle": "mchen", "scope": ".lemoncrow/**"}
    blocked = evaluate_gate(
        {".lemoncrow/review.yaml": [MAYA]}, viewer="usr_sam", barred=[], files=[".lemoncrow/review.yaml"]
    )
    assert blocked.to_wire(asked=["usr_maya"], review_id="r1", token_key=b"k")["needs"] == [
        {"name": "Maya Chen", "handle": "mchen", "key": owner_token("r1", "usr_maya", b"k"), "asked": True}
    ]
    # The key is an opaque token per Review, never the principal id.
    assert "usr_maya" not in str(blocked.to_wire(review_id="r1", token_key=b"k")) and owner_token(
        "r1", "usr_maya", b"k"
    ) != owner_token("r2", "usr_maya")
    assert blocked.to_wire()["signer"] is None
