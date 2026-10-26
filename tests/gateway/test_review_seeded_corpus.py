"""The seeded public bugs corpora: real pull requests, pinned and labelled by the fix that proves each bug.

``evals/review/seeded_public_bugs.yaml`` (permissively licensed repositories, public) and
``evals/review/seeded_public_bugs_nonpermissive.yaml`` (the others, ``visibility: private``, never mirrored; the tests
that need it skip in a checkout without it, which is the public mirror) are checked as data: they parse without a network, every pull request is pinned to two commits and carries at least one label,
every label names the fix commit and the derivation that produced it, and the licence rule holds. Nothing here calls a
model or fetches a repository.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli.developer_tools import developer_tools
from lemoncrow.pro.capabilities.review.evaluation.corpus import PERMISSIVE_LICENSES
from lemoncrow.pro.capabilities.review.evaluation.real_prs import RealPrCorpus, load_real_pr_corpus

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC = REPO_ROOT / "evals/review/seeded_public_bugs.yaml"
PRIVATE = REPO_ROOT / "evals/review/seeded_public_bugs_nonpermissive.yaml"
SHA = re.compile(r"[0-9a-f]{40}")
#: The benchmark set has five repositories; a corpus entry is named <repository>-<index of 01..10>.
ID = re.compile(r"(sentry|calcom|grafana|keycloak|discourse)-(0[1-9]|10)")

_mirror_spec = importlib.util.spec_from_file_location("_mirror_under_test", REPO_ROOT / "scripts" / "mirror.py")
assert _mirror_spec and _mirror_spec.loader
mirror = importlib.util.module_from_spec(_mirror_spec)
_mirror_spec.loader.exec_module(mirror)


needs_private = pytest.mark.skipif(not PRIVATE.exists(), reason="the private corpus is not in the public mirror")
CORPORA = [pytest.param(PUBLIC, id="public"), pytest.param(PRIVATE, id="private", marks=needs_private)]


def load(path: Path) -> RealPrCorpus:
    return load_real_pr_corpus(path, repository_root=REPO_ROOT)


@pytest.fixture(scope="module", params=CORPORA)
def corpus(request: pytest.FixtureRequest) -> RealPrCorpus:
    return load(request.param)


@needs_private
def test_both_corpora_parse_without_a_network_and_say_what_they_are() -> None:
    public, private = load(PUBLIC), load(PRIVATE)
    assert (public.visibility, private.visibility) == ("public", "private")
    assert public.prs and private.prs
    assert public.summary()["labels"] >= len(public.prs) and private.summary()["labels"] >= len(private.prs)


def test_every_entry_is_pinned_and_has_labels(corpus: RealPrCorpus) -> None:
    for pr in corpus.prs:
        assert ID.fullmatch(pr.id), pr.id
        assert (
            pr.provenance.source == "public_repository"
            and pr.provenance.repository == f"https://github.com/{pr.repository}"
        )
        assert SHA.fullmatch(pr.base_sha) and SHA.fullmatch(pr.head_sha) and pr.base_sha != pr.head_sha
        assert pr.provenance.commit == pr.head_sha
        assert pr.clone_url == f"https://github.com/{pr.repository}.git"
        assert pr.labelled and pr.labels, f"{pr.id}: an entry without a label has no place in this corpus"
        assert not pr.pushes, f"{pr.id}: the fixes landed too late to apply on the reviewed head"


def test_every_label_names_the_fix_that_proves_it_and_how_it_was_derived(corpus: RealPrCorpus) -> None:
    seen: set[str] = set()
    for pr in corpus.prs:
        for number, label in enumerate(pr.labels, 1):
            assert label.id == f"{pr.id}-bug-{number}" and label.id not in seen
            seen.add(label.id)
            assert SHA.fullmatch(label.evidence.fix_commit) and label.evidence.fix_commit != pr.head_sha
            assert label.evidence.introduced_by == pr.head_sha
            assert label.evidence.attribution in {"fix_message", "blame"}
            assert "kept only where the reviewed change itself added or changed the line" in label.evidence.derivation
            assert label.summary and label.regions
            assert all(region.role == "fix_site" and region.start_line <= region.end_line for region in label.regions)
        fixes = [label.evidence.fix_commit for label in pr.labels]
        assert len(set(fixes)) == len(fixes), f"{pr.id}: two labels for one fix commit"


@needs_private
def test_the_licence_rule_holds_and_the_split_between_the_files_follows_it() -> None:
    public, private = load(PUBLIC), load(PRIVATE)
    assert {pr.license for pr in public.prs} <= PERMISSIVE_LICENSES
    assert not {pr.license for pr in private.prs} & PERMISSIVE_LICENSES
    assert {pr.repository for pr in public.prs}.isdisjoint({pr.repository for pr in private.prs})
    ids = [pr.id for pr in (*public.prs, *private.prs)]
    assert len(ids) == len(set(ids))
    # A pull request is one commit pair; the same pair must not be in both files.
    pairs = [(pr.repository, pr.head_sha) for pr in (*public.prs, *private.prs)]
    assert len(pairs) == len(set(pairs))


def test_the_private_corpus_is_left_out_of_the_public_mirror() -> None:
    prefixes = mirror.load_public_prefixes()
    assert mirror.is_public("evals/review/seeded_public_bugs.yaml", prefixes) is True
    assert mirror.is_public("evals/review/seeded_public_bugs_nonpermissive.yaml", prefixes) is False


@pytest.mark.parametrize("path", CORPORA)
def test_a_corpus_claims_no_result_and_carries_no_model_output(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "Nothing in this file was run against a model" in text
    assert "measured" not in text.lower() and "catch rate of" not in text.lower()


@pytest.mark.parametrize("path", CORPORA)
def test_the_benchmark_plans_a_corpus_without_a_model_or_a_fetch(path: Path, tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    result: Any = CliRunner().invoke(
        developer_tools,
        ["eval", "review", "benchmark", "--corpus", str(path), "--plan", "--max-usd", "20", "--workdir", str(workdir)],
        catch_exceptions=False,
    )
    corpus = load(path)
    assert result.exit_code == 0, result.output
    assert (
        f"corpus {corpus.id} ({corpus.visibility}): {len(corpus.prs)} pull requests, {len(corpus.prs)} labelled"
        in result.output
    )
    assert "dollars cannot be bounded before a call returns its tokens" in result.output
    assert not workdir.exists()  # nothing was fetched
