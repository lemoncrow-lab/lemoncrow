"""The golden-comment loader and its answer key: loaded as data, with no network and no model.

The loader and the key reader behave: a missing answer address, an unpinned key, a key at another commit, a modified
key and a reordered key are each refused, and a ``Low`` comment is marked as excluded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.evaluation.corpus import CorpusError
from lemoncrow.pro.capabilities.review.evaluation.golden_comments import (
    GoldenError,
    GoldenKey,
    GoldenRef,
    load_golden_corpus,
    open_golden_key,
)
from tests.gateway.golden_support import KEY_DIRECTORY, SHA_A, git, make_key, write_corpus

# --------------------------------------------------------------------------- #
# the loader
# --------------------------------------------------------------------------- #


def test_a_corpus_addresses_one_answer_key_entry_per_pull_request(tmp_path: Path) -> None:
    _key, commit = make_key(tmp_path)
    golden = load_golden_corpus(write_corpus(tmp_path, commit))
    assert [pr.id for pr in golden.corpus.prs] == ["alpha-01", "alpha-02", "beta-01"]
    assert golden.ref("beta-01") == GoldenRef("beta.json", 1, "Add retries", "go", 0)
    assert golden.source.commit == commit and golden.source.directory == KEY_DIRECTORY


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda d: d["prs"][0].pop("golden"), "golden is required"),
        (lambda d: d["prs"][0]["golden"].update(index=0), "1-based"),
        (lambda d: d["prs"][0]["golden"].update(file="../x.json"), "bare .json file name"),
        (lambda d: d["prs"][0]["golden"].pop("language"), "golden.language is required"),
        (lambda d: d["prs"][1]["golden"].update(index=1), "already used by alpha-01"),
        (lambda d: d["golden_comments"].update(commit="main"), "40-character"),
        (lambda d: d["golden_comments"].update(directory="/abs"), "relative path"),
        (lambda d: d.pop("golden_comments"), "required"),
    ],
)
def test_a_corpus_that_does_not_pin_its_answers_is_refused(tmp_path: Path, mutate: Any, problem: str) -> None:
    _key, commit = make_key(tmp_path)
    with pytest.raises(CorpusError) as caught:
        load_golden_corpus(write_corpus(tmp_path, commit, mutate))
    assert problem in " ".join(caught.value.args[1:] or caught.value.args)


def test_the_real_pr_rules_still_apply(tmp_path: Path) -> None:
    _key, commit = make_key(tmp_path)
    with pytest.raises(CorpusError):
        load_golden_corpus(write_corpus(tmp_path, commit, lambda d: d["prs"][0].update(base_sha="abc")))


def test_pr_ids_narrow_the_corpus(tmp_path: Path) -> None:
    _key, commit = make_key(tmp_path)
    golden = load_golden_corpus(write_corpus(tmp_path, commit), pr_ids=["beta-01"])
    assert [pr.id for pr in golden.corpus.prs] == ["beta-01"] and set(golden.refs) == {"beta-01"}


# --------------------------------------------------------------------------- #
# the answer key
# --------------------------------------------------------------------------- #


def test_a_golden_comment_of_severity_low_is_excluded(tmp_path: Path) -> None:
    key_dir, commit = make_key(tmp_path)
    golden = load_golden_corpus(write_corpus(tmp_path, commit))
    key = open_golden_key(golden.source, key_dir, cache_root=tmp_path / "cache")
    comments = key.comments(golden.ref("alpha-01"))
    assert [c.id for c in comments] == ["G1", "G2", "G3"]
    assert [(c.severity, c.counted) for c in comments] == [("High", True), ("Critical", True), ("Low", False)]
    assert comments[1].rank < comments[0].rank < comments[2].rank


def test_a_key_at_another_commit_is_refused_with_the_command_that_fixes_it(tmp_path: Path) -> None:
    key_dir, commit = make_key(tmp_path)
    (key_dir / "extra.txt").write_text("x", encoding="utf-8")
    git(key_dir, "add", "-A")
    git(key_dir, "commit", "-q", "-m", "later")
    golden = load_golden_corpus(write_corpus(tmp_path, commit))
    with pytest.raises(GoldenError, match=rf"pins {commit[:12]}.*checkout --detach {commit}"):
        open_golden_key(golden.source, key_dir, cache_root=tmp_path / "cache")


def test_a_key_with_local_changes_is_refused(tmp_path: Path) -> None:
    key_dir, commit = make_key(tmp_path)
    (key_dir / KEY_DIRECTORY / "alpha.json").write_text("[]", encoding="utf-8")
    golden = load_golden_corpus(write_corpus(tmp_path, commit))
    with pytest.raises(GoldenError, match="local changes"):
        open_golden_key(golden.source, key_dir, cache_root=tmp_path / "cache")


def test_a_directory_that_is_not_a_checkout_is_refused(tmp_path: Path) -> None:
    _key, commit = make_key(tmp_path)
    golden = load_golden_corpus(write_corpus(tmp_path, commit))
    (tmp_path / "plain").mkdir()
    with pytest.raises(GoldenError, match="not a git checkout"):
        open_golden_key(golden.source, tmp_path / "plain", cache_root=tmp_path / "cache")


def test_a_reordered_key_cannot_score_the_wrong_pull_request(tmp_path: Path) -> None:
    key_dir, commit = make_key(tmp_path)
    golden = load_golden_corpus(write_corpus(tmp_path, commit))
    key = GoldenKey(key_dir, golden.source)
    swapped = GoldenRef("alpha.json", 2, "Cache the lookup", "python")  # entry 2 is "Rename the flag"
    with pytest.raises(GoldenError, match="not the one this corpus was built against"):
        key.comments(swapped)
    with pytest.raises(GoldenError, match="has 2 entries"):
        key.comments(GoldenRef("alpha.json", 3, "x", "python"))


def test_an_unknown_severity_or_empty_comment_in_the_key_is_an_error(tmp_path: Path) -> None:
    key_dir, _commit = make_key(tmp_path)
    path = key_dir / KEY_DIRECTORY / "beta.json"
    path.write_text(
        json.dumps([{"pr_title": "Add retries", "comments": [{"comment": "x", "severity": "Urgent"}]}]),
        encoding="utf-8",
    )
    git(key_dir, "add", "-A")
    git(key_dir, "commit", "-q", "-m", "bad")
    bad = git(key_dir, "rev-parse", "HEAD")
    golden = load_golden_corpus(write_corpus(tmp_path, bad))
    key = open_golden_key(golden.source, key_dir, cache_root=tmp_path / "cache")
    with pytest.raises(GoldenError, match="severity"):
        key.comments(golden.ref("beta-01"))
    assert SHA_A  # the fake commits are never fetched: nothing here touches a network
