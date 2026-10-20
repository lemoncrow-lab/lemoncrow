"""The real-PR benchmark corpus: its format, the rule that ground truth is never invented, and label derivation from git.

The seed corpus (``evals/review/real_prs.yaml``) is checked as data: it must load, every label must name the fix commit
that proves it, and nothing in it may claim a result. Derivation is tested on throwaway git repositories, where the right
answer is known because the test wrote the history.
"""

from __future__ import annotations

import copy
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review.evaluation.corpus import PERMISSIVE_LICENSES, CorpusError
from lemoncrow.pro.capabilities.review.evaluation.label_derivation import DerivationError, derive_label
from lemoncrow.pro.capabilities.review.evaluation.real_prs import load_real_pr_corpus
from tests.gateway.benchmark_support import corpus_dict, make_origin, write_corpus

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED = REPO_ROOT / "evals/review/real_prs.yaml"
SHA = "a" * 40


# --------------------------------------------------------------------------- #
# the seed corpus
# --------------------------------------------------------------------------- #


def test_the_seed_corpus_loads_and_every_label_names_the_fix_commit_that_proves_it() -> None:
    corpus = load_real_pr_corpus(SEED, repository_root=REPO_ROOT)
    assert corpus.visibility == "public" and 5 <= len(corpus.prs) <= 10
    for pr in corpus.prs:
        assert pr.provenance.source == "public_repository" and pr.license in PERMISSIVE_LICENSES
        assert pr.provenance.commit == pr.head_sha and pr.repository.count("/") == 1
        assert pr.labelled and pr.labels, f"{pr.id}: a seeded pull request is labelled or it is not in the seed"
        for label in pr.labels:
            assert re.fullmatch(r"[0-9a-f]{40}", label.evidence.fix_commit)
            assert label.evidence.attribution in {"fix_message", "blame"}
            assert label.summary and label.regions and label.symbols
        for push in pr.pushes:
            assert push.apply_commit == pr.labels[0].evidence.fix_commit


def test_the_seed_corpus_claims_no_result_and_carries_no_model_output() -> None:
    text = SEED.read_text(encoding="utf-8")
    assert "measured" not in text.lower()
    assert "Nothing in this file was run against a model" in text
    # Ground truth is derived, never written: every label's summary is a commit subject, not prose a person made up.
    corpus = load_real_pr_corpus(SEED, repository_root=REPO_ROOT)
    assert all(label.evidence.derivation for pr in corpus.prs for label in pr.labels)


def test_the_seed_corpus_pins_commits_and_needs_no_network_to_load() -> None:
    corpus = load_real_pr_corpus(SEED, repository_root=REPO_ROOT)
    for pr in corpus.prs:
        assert re.fullmatch(r"[0-9a-f]{40}", pr.base_sha) and re.fullmatch(r"[0-9a-f]{40}", pr.head_sha)
        assert pr.base_sha != pr.head_sha and pr.clone_url.startswith("https://github.com/")


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


@pytest.fixture
def origin(tmp_path: Path) -> Any:
    return make_origin(tmp_path)


def load(tmp_path: Path, origin: Any, mutate: Any = None, **kwargs: Any) -> Any:
    document = corpus_dict(origin)
    if mutate is not None:
        mutate(document)
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return load_real_pr_corpus(path, **kwargs)


def problems(tmp_path: Path, origin: Any, mutate: Any) -> str:
    with pytest.raises(CorpusError) as caught:
        load(tmp_path, origin, mutate)
    return str(caught.value)


def test_a_valid_corpus_loads_with_its_labels_and_pushes(tmp_path: Path, origin: Any) -> None:
    corpus = load(tmp_path, origin)
    assert [pr.id for pr in corpus.prs] == ["shop-12", "shop-14"]
    first, second = corpus.prs
    assert first.labelled and len(first.labels) == 1 and first.pushes[0].apply_commit == origin.fix
    assert not second.labelled and second.labels == ()
    assert corpus.summary()["labelled_prs"] == 1 and corpus.summary()["labels"] == 1


def test_a_label_without_a_fix_commit_is_refused_because_ground_truth_is_never_invented(
    tmp_path: Path, origin: Any
) -> None:
    def drop_evidence(document: dict[str, Any]) -> None:
        del document["prs"][0]["labels"][0]["evidence"]

    assert "ground truth is never invented" in problems(tmp_path, origin, drop_evidence)

    def short_sha(document: dict[str, Any]) -> None:
        document["prs"][0]["labels"][0]["evidence"]["fix_commit"] = "dba3c26"

    assert "full 40-character commit sha" in problems(tmp_path, origin, short_sha)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["prs"][0].update(license="GPL-3.0-only"), "permissive license"),
        (lambda d: d["prs"][0].update(base_sha="main"), "40-character commit sha"),
        (lambda d: d["prs"][0].update(repository="not a slug"), "OWNER/NAME"),
        (lambda d: d["prs"][1].update(id=d["prs"][0]["id"]), "must be unique"),
        (lambda d: d["prs"][0]["provenance"].update(commit=SHA), "provenance.commit must be the reviewed head_sha"),
        (lambda d: d["prs"][0].update(labelled=False), "labelled is false but labels are present"),
        (lambda d: d["prs"][0]["labels"][0]["regions"][0].update(lines=[5, 2]), "lines must be"),
        (lambda d: d["prs"][0]["labels"][0]["regions"][0].update(path="../etc/passwd"), "repository-relative"),
        (lambda d: d["prs"][0]["pushes"][0].update(head_sha=SHA), "exactly one of head_sha"),
        (lambda d: d.update(match="whatever"), "match must be one of"),
        (lambda d: d.update(version=2), "version must be 1"),
    ],
)
def test_invalid_corpora_say_what_is_wrong(tmp_path: Path, origin: Any, mutate: Any, expected: str) -> None:
    assert expected in problems(tmp_path, origin, mutate)


def test_a_pull_request_known_to_be_clean_says_so_and_an_unlabelled_one_does_not_claim_it(
    tmp_path: Path, origin: Any
) -> None:
    def clean(document: dict[str, Any]) -> None:
        document["prs"][1]["labelled"] = True

    corpus = load(tmp_path, origin, clean)
    assert corpus.prs[1].labelled and corpus.prs[1].labels == ()
    assert load(tmp_path, origin).prs[1].labelled is False


def test_the_input_digest_follows_the_pinned_git_inputs_not_the_labels(tmp_path: Path, origin: Any) -> None:
    base = load(tmp_path, origin)

    def relabel(document: dict[str, Any]) -> None:
        document["prs"][0]["labels"][0]["summary"] = "a different summary"

    def move_head(document: dict[str, Any]) -> None:
        document["prs"][0]["head_sha"] = origin.fix
        document["prs"][0]["provenance"]["commit"] = origin.fix

    assert load(tmp_path, origin, relabel).prs[0].input_digest() == base.prs[0].input_digest()
    assert load(tmp_path, origin, relabel).digest() != base.digest()
    assert load(tmp_path, origin, move_head).prs[0].input_digest() != base.prs[0].input_digest()


def test_pr_filter_and_unknown_ids(tmp_path: Path, origin: Any) -> None:
    path = write_corpus(tmp_path, origin)
    assert [pr.id for pr in load_real_pr_corpus(path, pr_ids=["shop-14"]).prs] == ["shop-14"]
    with pytest.raises(CorpusError, match="unknown pull request ids"):
        load_real_pr_corpus(path, pr_ids=["nope"])


# --------------------------------------------------------------------------- #
# organization corpora: a customer's pull requests
# --------------------------------------------------------------------------- #


def organization(origin: Any) -> dict[str, Any]:
    document = copy.deepcopy(corpus_dict(origin))
    document.update(
        visibility="organization",
        organization="acme",
        opt_in={"organization": "acme", "approved_by": "Pat", "approved_at": "2026-10-20", "reference": "ticket-1"},
    )
    for pr in document["prs"]:
        pr["provenance"]["source"] = "organization_review"
        pr["license"] = "proprietary"
    return document


def test_a_customers_corpus_needs_an_explicit_opt_in_and_stays_out_of_the_checkout(tmp_path: Path, origin: Any) -> None:
    outside = tmp_path / "customer.yaml"
    outside.write_text(yaml.safe_dump(organization(origin)), encoding="utf-8")
    with pytest.raises(CorpusError, match="requires an explicit organization opt-in"):
        load_real_pr_corpus(outside)
    with pytest.raises(CorpusError, match="belongs to 'acme'"):
        load_real_pr_corpus(outside, organization_opt_in="other")
    corpus = load_real_pr_corpus(outside, organization_opt_in="acme", repository_root=REPO_ROOT)
    assert corpus.organization == "acme" and corpus.opt_in is not None

    inside = tmp_path / "checkout"
    inside.mkdir()
    bundled = inside / "customer.yaml"
    bundled.write_text(yaml.safe_dump(organization(origin)), encoding="utf-8")
    with pytest.raises(CorpusError, match="never bundled"):
        load_real_pr_corpus(bundled, organization_opt_in="acme", repository_root=inside)


def test_a_public_corpus_refuses_a_customer_review_as_provenance(tmp_path: Path, origin: Any) -> None:
    def customer(document: dict[str, Any]) -> None:
        document["prs"][0]["provenance"]["source"] = "organization_review"

    assert "not allowed in a public corpus" in problems(tmp_path, origin, customer)


# --------------------------------------------------------------------------- #
# deriving a label from git
# --------------------------------------------------------------------------- #

IDENTITY = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True, env={**os.environ, **IDENTITY}
        ).stdout.strip()

    def commit(self, message: str, files: dict[str, str]) -> str:
        for name, text in files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")


LIB_V1 = (
    "".join(f"def f{n}():\n    return {n}\n\n\n" for n in range(1, 8)) + "def total(items):\n    return sum(items)\n"
)
LIB_V2 = LIB_V1.replace("    return sum(items)\n", "    return sum(items) + 1  # off by one\n")
LIB_V2_FIXED = LIB_V2.replace(" + 1  # off by one", "")


def history(tmp_path: Path, *, subject: str = "Fix total") -> tuple[Repo, str, str, str]:
    repo = Repo(tmp_path / "r")
    base = repo.commit("base", {"lib.py": LIB_V1, "tests/test_lib.py": "def test_total():\n    pass\n"})
    introduced = repo.commit("Add the bonus (#7)", {"lib.py": LIB_V2})
    fix = repo.commit(
        subject,
        {"lib.py": LIB_V2_FIXED, "tests/test_lib.py": "def test_total():\n    assert True\n", "CHANGES.md": "fixed\n"},
    )
    return repo, base, introduced, fix


def test_a_label_is_the_line_the_fix_edited_in_the_reviewed_head(tmp_path: Path) -> None:
    repo, base, introduced, fix = history(tmp_path, subject=f"Fix total, introduced in {'#7'}")
    derived = derive_label(repo.root, fix, introduced, pull=7)

    line = LIB_V2.splitlines().index("    return sum(items) + 1  # off by one") + 1
    assert [(r.path, r.start_line, r.end_line, r.kind, r.in_change) for r in derived.regions] == [
        ("lib.py", line, line, "changed", 1)
    ]
    assert (derived.base_sha, derived.head_sha, derived.fix_parent, derived.merge) == (
        base,
        introduced,
        introduced,
        False,
    )
    assert derived.attribution == "fix_message"
    assert "off" not in derived.fix_subject and derived.fix_subject == "Fix total, introduced in #7"
    # Tests and changelogs are not where a reviewer is asked to find a bug.
    assert {r.path for r in derived.regions} == {"lib.py"}
    label = derived.label("lib-total")
    assert label["summary"] == derived.fix_subject and label["evidence"]["fix_commit"] == fix


def test_a_line_moved_by_later_commits_is_mapped_back_to_the_reviewed_head(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    repo.commit("base", {"lib.py": LIB_V1})
    introduced = repo.commit("Add the bonus (#7)", {"lib.py": LIB_V2})
    repo.commit("Add a header", {"lib.py": "# header one\n# header two\n" + LIB_V2})
    fix = repo.commit("Fix total, introduced in #7", {"lib.py": "# header one\n# header two\n" + LIB_V2_FIXED})
    derived = derive_label(repo.root, fix, introduced, pull=7)

    line = LIB_V2.splitlines().index("    return sum(items) + 1  # off by one") + 1
    assert derived.regions[0].start_line == line  # not line + 2: the header is not in the code that was reviewed


def test_a_line_that_changed_since_the_reviewed_head_is_dropped_not_guessed(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    repo.commit("base", {"lib.py": LIB_V1})
    introduced = repo.commit("Add the bonus (#7)", {"lib.py": LIB_V2})
    repo.commit("Reword", {"lib.py": LIB_V2.replace("# off by one", "# off by 1")})
    fix = repo.commit("Fix total, introduced in #7", {"lib.py": LIB_V2_FIXED})
    with pytest.raises(DerivationError, match="edits no source line that is still as it was"):
        derive_label(repo.root, fix, introduced, pull=7)


def test_blame_can_be_the_evidence_when_the_fix_message_names_nothing(tmp_path: Path) -> None:
    repo, _base, introduced, fix = history(tmp_path, subject="Fix total")
    derived = derive_label(repo.root, fix, introduced)
    assert derived.attribution == "blame"


def test_a_pair_with_no_evidence_of_a_regression_is_not_a_case(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    repo.commit("base", {"lib.py": LIB_V1, "other.py": "X = 1\n"})
    unrelated = repo.commit("Change other (#9)", {"other.py": "X = 2\n"})
    fix = repo.commit("Tidy total", {"lib.py": LIB_V1.replace("sum(items)", "sum(list(items))")})
    with pytest.raises(DerivationError, match="not evidence of a regression"):
        derive_label(repo.root, fix, unrelated, pull=9)


def test_a_pull_request_with_several_commits_is_reviewed_as_its_branch(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    base = repo.commit("base", {"lib.py": LIB_V1})
    repo.git("checkout", "-qb", "feature")
    repo.commit("first", {"other.py": "X = 1\n"})
    head = repo.commit("second", {"lib.py": LIB_V2})
    repo.git("checkout", "-q", "main")
    repo.commit("main moves", {"unrelated.py": "Y = 1\n"})
    repo.git("merge", "--no-ff", "-qm", "Merge pull request #7", "feature")
    merge = repo.git("rev-parse", "HEAD")
    fix = repo.commit("Fix total, introduced in #7", {"lib.py": LIB_V2_FIXED})

    derived = derive_label(repo.root, fix, merge, pull=7)
    assert derived.merge and derived.head_sha == head
    assert derived.base_sha != base  # the first parent the branch is merged into
    assert derived.regions[0].in_change == 1  # blame names the branch commit, not the merge


def test_a_pure_insertion_points_at_the_line_before_it(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    repo.commit("base", {"lib.py": LIB_V1})
    introduced = repo.commit("Add a guard (#7)", {"lib.py": LIB_V1 + "\n\ndef guarded(x):\n    return x\n"})
    fix = repo.commit(
        "Fix guarded, introduced in #7",
        {"lib.py": LIB_V1 + "\n\ndef guarded(x):\n    assert x\n    return x\n"},
    )
    derived = derive_label(repo.root, fix, introduced, pull=7)
    (region,) = derived.regions
    assert region.kind == "insertion" and region.start_line == region.end_line
