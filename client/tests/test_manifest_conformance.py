"""The manifest the client builds must be the manifest the server verifies.

The server *proves* the announced root when the last chunk lands and refuses
the completion otherwise, so a divergence in the row encoding is not a
cosmetic difference -- it is a client that can never open a cold view. The
encoding therefore gets two kinds of test:

* properties that hold with no server in sight (ordering, ``.gitignore``,
  symlinks, the size cap, chunk identity);
* a direct comparison with the server's own functions on randomized trees,
  when the private package is importable.
"""

from __future__ import annotations

import os
import random
import string
from pathlib import Path

import pytest
from _serverpkg import REASON, server_available
from lemoncrow_client.manifest import (
    EMPTY_MANIFEST_ROOT,
    ManifestEntry,
    canonical_rows,
    chunk_manifest,
    manifest_root,
    profile_for_path,
    walk_worktree,
)

CAP = 1024 * 1024


def _entry(path: str, digest: str = "a" * 64, size: int = 10) -> ManifestEntry:
    return ManifestEntry(
        path=path, content_digest=digest, parser_profile=profile_for_path(path), mode=0o100644, size=size
    )


def test_the_root_does_not_depend_on_walk_order() -> None:
    """Two clients that see the same worktree must produce the same root."""
    entries = [_entry(f"pkg/mod{index}.py") for index in range(25)]
    shuffled = list(entries)
    random.Random(7).shuffle(shuffled)
    assert manifest_root(entries) == manifest_root(shuffled)


def test_one_row_per_path_even_when_a_path_repeats() -> None:
    duplicated = [_entry("a.py", digest="b" * 64), _entry("a.py", digest="c" * 64)]
    rows = canonical_rows(duplicated)
    assert len(rows) == 1
    assert "c" * 64 in rows[0], "the later row must win, as the server's does"


def test_an_empty_manifest_still_has_a_verifiable_root() -> None:
    assert manifest_root(()) == EMPTY_MANIFEST_ROOT
    assert chunk_manifest(())[0].entries == ()


def test_chunks_are_content_identified_and_reassemble_to_the_root() -> None:
    entries = [_entry(f"pkg/mod{index:03d}.py", digest=f"{index:064x}") for index in range(37)]
    chunks = chunk_manifest(entries, max_rows=10)
    assert len(chunks) == 4
    assert len({chunk.chunk_id for chunk in chunks}) == 4
    reassembled = [entry for chunk in chunks for entry in chunk.entries]
    assert manifest_root(reassembled) == manifest_root(entries)
    # Re-chunking after an interrupted upload names the same ids, which is what
    # makes re-announcing a pending chunk a no-op rather than a duplicate.
    assert [chunk.chunk_id for chunk in chunk_manifest(entries, max_rows=10)] == [chunk.chunk_id for chunk in chunks]


def test_the_walk_respects_gitignore_never_enters_git_and_never_follows_symlinks(
    worktree: Path,
) -> None:
    (worktree / "pkg" / "link.py").symlink_to(worktree / "pkg" / "alpha.py")
    (worktree / ".lc-worktrees" / "nested").mkdir(parents=True)
    (worktree / ".lc-worktrees" / "nested" / "duplicate.py").write_text("def duplicate(): pass\n")
    (worktree / ".lemoncrow").mkdir()
    (worktree / ".lemoncrow" / "runtime.py").write_text("def runtime_state(): pass\n")
    walked = walk_worktree(worktree, size_cap=CAP)
    paths = {file.path for file in walked.files}
    assert "pkg/alpha.py" in paths
    assert not any(path.startswith(".lc-worktrees/") for path in paths)
    assert not any(path.startswith(".lemoncrow/") for path in paths)
    assert "build/artifact.o" not in paths, ".gitignore directory rule was not applied"
    assert "noisy.log" not in paths, ".gitignore glob rule was not applied"
    assert not any(path.startswith(".git/") for path in paths), ".git must never be walked"
    assert "pkg/link.py" not in paths
    assert walked.symlinks == 1
    assert walked.ignored >= 2


def test_a_file_over_the_cap_is_manifested_but_not_uploadable(worktree: Path) -> None:
    """The size cap is server policy: the row travels, the bytes do not."""
    (worktree / "big.bin").write_bytes(b"x" * 4096)
    walked = walk_worktree(worktree, size_cap=1024)
    rows = {file.path: file for file in walked.files}
    assert "big.bin" in rows, "an over-cap file still gets a manifest row"
    assert rows["big.bin"].uploadable is False
    assert "big.bin" not in {file.path for file in walked.uploadable()}
    assert walked.oversize == 1


def test_mode_is_gits_two_modes_and_nothing_host_specific(worktree: Path) -> None:
    script = worktree / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(script, 0o755)
    modes = {file.path: file.mode for file in walk_worktree(worktree, size_cap=CAP).files}
    assert modes["run.sh"] == 0o100755
    assert modes["pkg/alpha.py"] == 0o100644


def test_parser_profile_is_derived_from_the_extension_only() -> None:
    assert profile_for_path("a/b/c.py") == "python-1"
    assert profile_for_path("a/b/c.PY") == "python-1"
    assert profile_for_path("Makefile") == "lexical-1"
    assert profile_for_path("a.tar.gz") == "lexical-1"
    assert profile_for_path("x/.hidden") == "lexical-1", "a dotfile has no extension"


# --------------------------------------------------------------------------- #
# Against the server's own encoding                                           #
# --------------------------------------------------------------------------- #


def _random_tree(root: Path, seed: int) -> None:
    rng = random.Random(seed)
    for index in range(rng.randint(3, 40)):
        depth = rng.randint(0, 3)
        parts = ["".join(rng.choices(string.ascii_lowercase, k=4)) for _ in range(depth)]
        suffix = rng.choice([".py", ".ts", ".go", ".rs", ".md", "", ".pyi"])
        target = root.joinpath(*parts, f"file{index}{suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(rng.choices(range(32, 127), k=rng.randint(0, 4096))))
    if rng.random() < 0.6:
        (root / ".gitignore").write_text(
            "\n".join(rng.sample(["*.md", "build/", "/file1*", "**/*.rs", "!keep.md"], k=3)),
            encoding="utf-8",
        )


@pytest.mark.skipif(not server_available(), reason=REASON)
@pytest.mark.parametrize("seed", list(range(12)))
def test_the_client_and_the_server_agree_on_the_canonical_encoding(tmp_path: Path, seed: int) -> None:
    """Same tree, same rows, same root, same chunk ids -- or a cold view fails."""
    from lemoncrow_server_core.index import manifest as server_manifest

    root = tmp_path / f"tree{seed}"
    root.mkdir()
    _random_tree(root, seed)

    mine = walk_worktree(root, size_cap=CAP)
    theirs = server_manifest.walk_worktree(root, size_cap=CAP)

    assert [file.path for file in mine.files] == [file.path for file in theirs.files]
    assert [file.content_digest for file in mine.files] == [file.content_digest for file in theirs.files]
    assert [file.parser_profile for file in mine.files] == [file.parser_profile for file in theirs.files]
    assert [file.mode for file in mine.files] == [file.mode for file in theirs.files]
    assert [file.uploadable for file in mine.files] == [file.uploadable for file in theirs.files]
    assert (mine.ignored, mine.symlinks, mine.oversize) == (
        theirs.ignored,
        theirs.symlinks,
        theirs.oversize,
    )
    assert mine.root() == theirs.root()
    assert [chunk.chunk_id for chunk in chunk_manifest(mine.entries(), max_rows=5)] == [
        chunk.chunk_id for chunk in server_manifest.chunk_manifest(theirs.entries(), max_rows=5)
    ]


@pytest.mark.skipif(not server_available(), reason=REASON)
def test_the_empty_root_matches_the_servers() -> None:
    from lemoncrow_server_core.index import manifest as server_manifest

    assert EMPTY_MANIFEST_ROOT == server_manifest.EMPTY_MANIFEST_ROOT
