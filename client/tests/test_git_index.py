"""``gitindex`` against the only oracle that counts: git itself.

The parser exists to answer one question -- "does git consider this worktree
file unmodified?" -- and git answers that question too, in ``git status``. So
every test here is a differential: build a repository, put it in some state, and
assert that the set of paths this parser calls clean is exactly the set git does
not call modified. A parser that agreed with the format documentation but not
with the program would be a fast path built on a plausible misreading.

The second half is about refusal. An index this module does not fully understand
must produce ``None`` -- walk as if there were no index -- rather than a partial
answer, because a partial answer here becomes a wrong ``content_digest``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from lemoncrow_client.gitindex import locate_index, parse_index, read_index

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is not on PATH")


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """One git command, isolated from the developer's own configuration."""
    assert GIT is not None
    return subprocess.run(
        [
            GIT,
            "-c",
            "user.email=budget@example.invalid",
            "-c",
            "user.name=Budget",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(root),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )


def git(root: Path, *args: str) -> str:
    completed = run_git(root, *args)
    assert completed.returncode == 0, f"git {args}: {completed.stderr}"
    return completed.stdout


def settle(root: Path) -> None:
    """Let git re-verify the stat data it wrote in the same tick as the files.

    git deliberately distrusts an index entry whose timestamp is the index's own
    -- it cannot tell that entry from one changed a moment later in the same tick
    -- and resolves it by re-reading the file on the next refresh. A session that
    starts in that same tick sees the unresolved state; one that starts a moment
    later does not, and both are worth testing.
    """
    time.sleep(0.05)
    run_git(root, "update-index", "--refresh")


def write(root: Path, relative: str, body: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    for index in range(40):
        write(root, f"pkg{index % 4}/module_{index:03d}.py", f"VALUE = {index}\n" + "# filler\n" * index)
    write(root, "run.sh", "#!/bin/sh\necho run\n")
    (root / "run.sh").chmod(0o755)
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")
    return root


def modified_according_to_git(root: Path) -> set[str]:
    """Tracked paths ``git status`` reports as changed in the worktree."""
    out: set[str] = set()
    for line in git(root, "status", "--porcelain", "-z").split("\0"):
        if len(line) < 4 or line.startswith("??"):
            continue
        out.add(line[3:])
    return out


def clean_according_to_us(root: Path) -> set[str]:
    index = read_index(root)
    assert index is not None
    clean: set[str] = set()
    for relative in index.entries:
        try:
            info = os.lstat(root / relative)
        except OSError:
            continue
        if index.clean_oid(relative, info) is not None:
            clean.add(relative)
    return clean


def tracked(root: Path) -> set[str]:
    return {line for line in git(root, "ls-files", "-z").split("\0") if line}


# --------------------------------------------------------------------------- #
# Agreement with git                                                          #
# --------------------------------------------------------------------------- #


def test_a_pristine_checkout_is_clean_everywhere(repository: Path) -> None:
    assert modified_according_to_git(repository) == set()
    assert clean_according_to_us(repository) == tracked(repository)


def test_the_clean_set_is_exactly_gits_unmodified_set(repository: Path) -> None:
    write(repository, "pkg0/module_000.py", "VALUE = 999\n")
    write(repository, "pkg1/module_005.py", "VALUE = 5\n" + "# filler\n" * 5 + "# extra\n")
    (repository / "pkg2/module_002.py").chmod(0o755)
    os.utime(repository / "pkg3/module_003.py", (1_600_000_000, 1_600_000_000))

    ours = clean_according_to_us(repository)
    theirs = tracked(repository) - modified_according_to_git(repository)
    # ``touch`` is the one place the two may legitimately differ: git re-reads
    # the file, finds the content unchanged and reports it clean. This parser
    # never reads a file, so it reports the mismatched stat as not clean -- a
    # strictly more conservative answer, which is the only direction that is
    # allowed to differ.
    assert ours <= theirs, sorted(ours - theirs)
    assert theirs - ours == {"pkg3/module_003.py"}, sorted(theirs - ours)


def test_a_file_restored_byte_for_byte_is_still_not_clean_on_stat_data_alone(
    repository: Path,
) -> None:
    """The test is about stat data, not content, and never pretends otherwise.

    Restoring the original bytes leaves the file with new stat data, and nothing
    in the index has been re-verified against it. The honest answer is "not
    known to be unchanged", which costs one hash -- and the walk cache's own
    blob map is what turns that hash back into a hit, from the other direction.
    """
    target = repository / "pkg0/module_000.py"
    original = target.read_text(encoding="utf-8")
    target.write_text("VALUE = 111\n", encoding="utf-8")
    assert "pkg0/module_000.py" not in clean_according_to_us(repository)
    target.write_text(original, encoding="utf-8")
    settle(repository)
    assert "pkg0/module_000.py" not in clean_according_to_us(repository)
    assert "pkg0/module_000.py" not in modified_according_to_git(repository)


def test_a_checkout_leaves_every_switched_file_clean_at_its_new_blob(repository: Path) -> None:
    """The case a per-path cache cannot serve and a blob-keyed one can."""
    git(repository, "checkout", "-b", "feature")
    for index in range(40):
        write(repository, f"pkg{index % 4}/module_{index:03d}.py", f"VALUE = {index * 7}\n# on the feature branch\n")
    git(repository, "add", "-A")
    git(repository, "commit", "-m", "feature")

    feature_oids = {path: _oid(repository, path) for path in tracked(repository)}
    git(repository, "checkout", "main")

    def survey() -> tuple[int, int, int]:
        """``(clean, racy, total)``. Every path is one of the first two or a bug."""
        index = read_index(repository)
        assert index is not None
        clean = racy = 0
        for path in tracked(repository):
            entry = index.entries.get(path)
            assert entry is not None, path
            oid = index.clean_oid(path, os.lstat(repository / path))
            if oid is None:
                # The only reason a just-checked-out file may be refused: git
                # wrote the index in the same tick as the file, so the entry
                # cannot be told from one changed a moment later.
                assert entry.mtime_ns >= index.index_mtime_ns, path
                racy += 1
                continue
            assert oid == _oid(repository, path), path
            if path != "run.sh":
                assert oid != feature_oids[path]
            clean += 1
        return clean, racy, len(tracked(repository))

    immediate, racy, total = survey()
    # Straight after a checkout every path is either clean at its new blob or
    # racy -- never wrong, and never unexplained. How the two split depends on
    # how much of the tree git wrote inside one timestamp tick, which is a
    # property of the machine, not of this parser.
    assert immediate + racy == total, f"{total - immediate - racy} paths were neither clean nor racy"

    settle(repository)
    settled, still_racy, total = survey()
    assert settled == total, f"{still_racy} of {total} were still unresolved after a refresh"


def _oid(root: Path, relative: str) -> str:
    return git(root, "rev-parse", f"HEAD:{relative}").strip()


# --------------------------------------------------------------------------- #
# The entries that must never be trusted                                      #
# --------------------------------------------------------------------------- #


def test_an_assume_valid_entry_is_never_reported_clean(repository: Path) -> None:
    """``--assume-valid`` means "stop checking", which is the opposite of proof."""
    git(repository, "update-index", "--assume-unchanged", "pkg0/module_000.py")
    assert "pkg0/module_000.py" not in clean_according_to_us(repository)
    write(repository, "pkg0/module_000.py", "VALUE = -1\n")
    assert "pkg0/module_000.py" not in clean_according_to_us(repository)


def test_a_skip_worktree_entry_is_never_reported_clean(repository: Path) -> None:
    git(repository, "update-index", "--skip-worktree", "pkg1/module_001.py")
    assert "pkg1/module_001.py" not in clean_according_to_us(repository)


def test_an_intent_to_add_entry_is_never_reported_clean(repository: Path) -> None:
    """``git add -N`` records a path with no content behind it."""
    write(repository, "pkg0/pending.py", "VALUE = 0\n")
    git(repository, "add", "-N", "pkg0/pending.py")
    assert "pkg0/pending.py" not in clean_according_to_us(repository)


def test_a_conflicted_path_is_never_reported_clean(repository: Path) -> None:
    """A merge conflict puts three stages in the index and no clean answer."""
    write(repository, "pkg0/module_000.py", "VALUE = 1\n")
    git(repository, "commit", "-am", "main side")
    git(repository, "checkout", "-b", "other", "HEAD~1")
    write(repository, "pkg0/module_000.py", "VALUE = 2\n")
    git(repository, "commit", "-am", "other side")
    merge = run_git(repository, "merge", "main")
    assert merge.returncode != 0, "the merge was supposed to conflict"
    assert "CONFLICT" in merge.stdout + merge.stderr, merge.stderr
    assert "pkg0/module_000.py" not in clean_according_to_us(repository)


def test_a_symlink_entry_is_not_a_file_this_walk_would_hash(repository: Path) -> None:
    os.symlink("module_000.py", repository / "pkg0/alias.py")
    git(repository, "add", "-A")
    git(repository, "commit", "-m", "symlink")
    index = read_index(repository)
    assert index is not None
    assert "pkg0/alias.py" not in index.entries


def test_a_gitlink_is_not_an_entry(tmp_path: Path) -> None:
    """A submodule is a commit id in the index, not content to hash."""
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "--initial-branch=main")
    write(inner, "a.txt", "a\n")
    git(inner, "add", "-A")
    git(inner, "commit", "-m", "inner")
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "--initial-branch=main")
    write(outer, "b.txt", "b\n")
    git(outer, "-c", "protocol.file.allow=always", "submodule", "add", str(inner), "sub")
    git(outer, "add", "-A")
    git(outer, "commit", "-m", "outer")
    index = read_index(outer)
    assert index is not None
    assert "sub" not in index.entries
    assert "b.txt" in index.entries


# --------------------------------------------------------------------------- #
# Formats and layouts                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", [2, 3, 4])
def test_every_supported_index_version_agrees_with_git(repository: Path, version: int) -> None:
    if version == 3:
        # git writes version 3 only when an entry needs the extended flag word,
        # and downgrades otherwise. One skip-worktree bit is what forces it.
        git(repository, "update-index", "--skip-worktree", "run.sh")
    git(repository, "update-index", f"--index-version={version}", "--force-write-index")
    index = read_index(repository)
    assert index is not None
    assert index.version == version, f"git wrote version {index.version}"
    settle(repository)
    expected = tracked(repository) - ({"run.sh"} if version == 3 else set())
    assert clean_according_to_us(repository) == expected


def test_a_sha256_repository_parses_with_thirty_two_byte_object_ids(tmp_path: Path) -> None:
    root = tmp_path / "sha256"
    root.mkdir()
    probe = run_git(root, "init", "--object-format=sha256", "--initial-branch=main")
    if probe.returncode != 0:  # pragma: no cover - old git
        pytest.skip("this git does not support --object-format=sha256")
    for index in range(8):
        write(root, f"m_{index}.py", f"VALUE = {index}\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")
    index = read_index(root)
    assert index is not None
    assert clean_according_to_us(root) == tracked(root)
    assert all(len(entry.oid) == 64 for entry in index.entries.values())


def test_a_linked_worktree_reads_its_own_index(repository: Path, tmp_path: Path) -> None:
    """``.git`` is a file there, and the index is not under the worktree at all."""
    linked = tmp_path / "linked"
    git(repository, "worktree", "add", str(linked), "-b", "linked")
    assert (linked / ".git").is_file()
    located = locate_index(linked)
    assert located is not None and located.is_file()
    assert not str(located).startswith(str(linked) + os.sep)
    settle(linked)
    assert clean_according_to_us(linked) == tracked(linked)


# --------------------------------------------------------------------------- #
# Refusal                                                                     #
# --------------------------------------------------------------------------- #


def test_a_truncated_index_is_refused_rather_than_half_read(repository: Path) -> None:
    located = locate_index(repository)
    assert located is not None
    raw = located.read_bytes()
    for cut in (13, len(raw) // 3, len(raw) // 2, len(raw) - 25):
        assert parse_index(raw[:cut], index_mtime_ns=0) is None, f"accepted a {cut}-byte index"


def test_an_index_with_the_wrong_object_length_is_refused_or_finds_nothing(repository: Path) -> None:
    """A SHA-1 index read as SHA-256 must not produce confident nonsense."""
    located = locate_index(repository)
    assert located is not None
    raw = located.read_bytes()
    misread = parse_index(raw, index_mtime_ns=0, oid_bytes=32)
    truth = parse_index(raw, index_mtime_ns=0, oid_bytes=20)
    assert truth is not None and truth.entries
    assert misread is None or not (set(misread.entries) & set(truth.entries))


def test_a_foreign_or_unknown_index_is_refused(repository: Path) -> None:
    located = locate_index(repository)
    assert located is not None
    raw = bytearray(located.read_bytes())
    assert parse_index(b"NOPE" + bytes(raw[4:]), index_mtime_ns=0) is None
    forged = bytearray(raw)
    forged[4:8] = (99).to_bytes(4, "big")
    assert parse_index(bytes(forged), index_mtime_ns=0) is None
    assert parse_index(b"", index_mtime_ns=0) is None


def test_a_racily_clean_entry_is_reported_dirty(repository: Path) -> None:
    """An entry as new as the index that describes it proves nothing."""
    located = locate_index(repository)
    assert located is not None
    raw = located.read_bytes()
    fresh = parse_index(raw, index_mtime_ns=0)
    assert fresh is not None
    # With the index's own mtime at zero, every entry's mtime is >= it.
    for relative, entry in fresh.entries.items():
        info = os.lstat(repository / relative)
        assert not entry.clean_against(info, racy_before_ns=0)
    assert fresh.clean_oid("pkg0/module_000.py", os.lstat(repository / "pkg0/module_000.py")) is None


def test_a_directory_with_no_git_at_all_has_no_index(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("a = 1\n", encoding="utf-8")
    assert read_index(plain) is None
    assert locate_index(plain) is None
