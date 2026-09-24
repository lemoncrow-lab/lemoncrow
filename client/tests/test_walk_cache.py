"""The fast path is only allowed to be fast if it is also right.

Every test here compares the cached walk against the same walk with no cache at
all, on the same filesystem state, and demands *identical manifest rows* -- path,
digest, size, mode, parser profile and uploadability. That is the whole contract:
the cache may change how long a walk takes and nothing else. A digest that is
merely usually right is worse than no cache, because the failure is a view that
serves the wrong bytes to an agent that has no way to notice.

Three layers of evidence, in increasing order of how much they would catch:

1. **Randomized mutation.** Sixteen seeds, each running a worktree through rounds
   of random edits, restores, renames, deletions, symlink swaps, permission
   changes and ignore-rule changes, re-walking after every round. The digests are
   additionally checked against ``sha256`` of the files' actual bytes, so the
   reference walk and the cached walk cannot be wrong together.
2. **The adversarial cases, each on its own.** A randomized test finds them
   eventually; a named test says which one broke. Same size and mtime with
   different content, a file restored to an earlier version, a file touched but
   unchanged, a symlink, a mode change, a replace-by-rename.
3. **Randomized mutation with git driving it** -- commits, branches, checkouts,
   stashes -- because the blob-keyed half of the cache exists precisely for the
   states git puts a worktree in, and those are not states a test can invent by
   writing files.

Everything the cache persists is round-tripped through the real file on disk via
the real writer, :func:`lemoncrow_client.session.persist_walk_cache`, so the
format is under test rather than just the in-memory object.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from random import Random

import pytest
from lemoncrow_client.manifest import WalkResult, walk_worktree
from lemoncrow_client.session import persist_walk_cache
from lemoncrow_client.walkcache import (
    MINIMUM_INDEX_MISSES,
    WalkCache,
    cache_path,
    load_walk_cache,
)

CAP = 1024 * 1024
Row = tuple[str, str, int, int, str, bool]

GIT = shutil.which("git")


def rows(result: WalkResult) -> tuple[Row, ...]:
    return tuple(
        (walked.path, walked.content_digest, walked.size, walked.mode, walked.parser_profile, walked.uploadable)
        for walked in result.files
    )


def full_walk(root: Path) -> WalkResult:
    """The reference: read and hash everything, every time."""
    return walk_worktree(root, size_cap=CAP)


def cached_walk(root: Path, state_dir: Path) -> WalkResult:
    """One session's walk: load the cache, walk, persist what was learned."""
    cache = load_walk_cache(state_dir, root)
    result = walk_worktree(root, size_cap=CAP, cache=cache)
    persist_walk_cache(cache, state_dir)
    return result


def truth(root: Path, result: WalkResult) -> None:
    """Every digest is ``sha256`` of the bytes actually on disk right now."""
    for walked in result.files:
        if not walked.uploadable and walked.size > CAP:
            continue
        data = (root / walked.path).read_bytes()
        assert walked.content_digest == hashlib.sha256(data).hexdigest(), walked.path
        assert walked.size == len(data), walked.path


def agree(root: Path, state_dir: Path, note: str = "") -> WalkResult:
    """Assert the cached walk and a full walk produce the same manifest."""
    cached = cached_walk(root, state_dir)
    reference = full_walk(root)
    assert rows(cached) == rows(reference), note or "cached walk disagreed with a full walk"
    assert cached.root() == reference.root()
    truth(root, cached)
    return cached


# --------------------------------------------------------------------------- #
# A worktree, and ways to mutate one                                           #
# --------------------------------------------------------------------------- #

_EXTENSIONS = (".py", ".ts", ".go", ".rs", ".md", ".json", ".txt")


def build(root: Path, rng: Random, *, files: int) -> dict[str, list[bytes]]:
    """A repository-shaped tree, plus the history a restore can draw on."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\nbuild/\n", encoding="utf-8")
    history: dict[str, list[bytes]] = {}
    for index in range(files):
        relative = f"pkg{index % 7}/sub{index % 3}/unit_{index:04d}{_EXTENSIONS[index % len(_EXTENSIONS)]}"
        body = f"UNIT = {index}\n".encode() + b"# line\n" * (index % 40)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        history[relative] = [body]
    return history


def _existing(root: Path) -> list[Path]:
    return [path for path in sorted(root.rglob("*")) if path.is_file() and ".git" not in path.parts]


def _pick(root: Path, rng: Random) -> Path | None:
    candidates = _existing(root)
    return rng.choice(candidates) if candidates else None


def mutate_rewrite(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    target = _pick(root, rng)
    if target is None:
        return "rewrite: nothing to rewrite"
    body = f"UNIT = {rng.randrange(10**9)}\n".encode() + b"# changed\n" * rng.randrange(60)
    target.write_bytes(body)
    history.setdefault(str(target.relative_to(root)), []).append(body)
    return f"rewrite {target.name}"


def mutate_same_size_same_mtime(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    """The forgery: different content, identical length, mtime put back."""
    target = _pick(root, rng)
    if target is None:
        return "forge: nothing to forge"
    before = target.stat()
    original = target.read_bytes()
    forged = bytes((byte + 1) % 256 if 32 <= byte < 126 else byte for byte in original)
    if forged == original:
        forged = original[:-1] + bytes([(original[-1] + 1) % 256]) if original else b"x"
    target.write_bytes(forged)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    history.setdefault(str(target.relative_to(root)), []).append(forged)
    return f"forge {target.name}"


def mutate_restore(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    known = [relative for relative, versions in history.items() if len(versions) > 1]
    if not known:
        return "restore: no history"
    relative = rng.choice(known)
    body = rng.choice(history[relative][:-1])
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    history[relative].append(body)
    return f"restore {relative}"


def mutate_touch(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    target = _pick(root, rng)
    if target is None:
        return "touch: nothing to touch"
    stamp = time.time() + rng.randrange(-100_000, 0)
    os.utime(target, (stamp, stamp))
    return f"touch {target.name}"


def mutate_chmod(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    target = _pick(root, rng)
    if target is None:
        return "chmod: nothing to chmod"
    mode = target.stat().st_mode
    target.chmod(0o644 if mode & 0o100 else 0o755)
    return f"chmod {target.name}"


def mutate_delete(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    target = _pick(root, rng)
    if target is None:
        return "delete: nothing to delete"
    target.unlink()
    return f"delete {target.name}"


def mutate_create(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    relative = f"pkg{rng.randrange(7)}/new_{rng.randrange(10**6):06d}{rng.choice(_EXTENSIONS)}"
    body = f"NEW = {rng.randrange(10**9)}\n".encode() * rng.randrange(1, 30)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    history.setdefault(relative, []).append(body)
    return f"create {relative}"


def mutate_rename(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    target = _pick(root, rng)
    if target is None:
        return "rename: nothing to rename"
    destination = target.with_name(f"moved_{rng.randrange(10**6):06d}{target.suffix}")
    target.rename(destination)
    return f"rename {target.name}"


def mutate_replace_by_rename(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    """New inode, same path: what an editor's atomic save actually does."""
    target = _pick(root, rng)
    if target is None:
        return "replace: nothing to replace"
    body = f"REPLACED = {rng.randrange(10**9)}\n".encode() * rng.randrange(1, 20)
    temporary = target.with_name(target.name + ".incoming")
    temporary.write_bytes(body)
    os.replace(temporary, target)
    history.setdefault(str(target.relative_to(root)), []).append(body)
    return f"replace {target.name}"


def mutate_symlink(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    """A file becomes a link to another file. The walk must stop counting it."""
    candidates = _existing(root)
    if len(candidates) < 2:
        return "symlink: too few files"
    victim, other = rng.sample(candidates, 2)
    victim.unlink()
    os.symlink(other.name, victim)
    return f"symlink {victim.name} -> {other.name}"


def mutate_unlink_symlink(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    links = [path for path in root.rglob("*") if path.is_symlink()]
    if not links:
        return "unlink-symlink: none"
    link = rng.choice(links)
    link.unlink()
    body = f"REAL = {rng.randrange(10**9)}\n".encode()
    link.write_bytes(body)
    history.setdefault(str(link.relative_to(root)), []).append(body)
    return f"symlink->file {link.name}"


def mutate_truncate(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    target = _pick(root, rng)
    if target is None:
        return "truncate: nothing to truncate"
    data = target.read_bytes()
    body = data[: len(data) // 2]
    target.write_bytes(body)
    history.setdefault(str(target.relative_to(root)), []).append(body)
    return f"truncate {target.name}"


def mutate_ignore_rule(root: Path, rng: Random, history: dict[str, list[bytes]]) -> str:
    rules = ["*.log\nbuild/\n", "*.log\nbuild/\n*.txt\n", "*.log\n", "*.log\nbuild/\npkg3/\n"]
    (root / ".gitignore").write_text(rng.choice(rules), encoding="utf-8")
    return "ignore rules"


MUTATIONS: Sequence[Callable[[Path, Random, dict[str, list[bytes]]], str]] = (
    mutate_rewrite,
    mutate_same_size_same_mtime,
    mutate_restore,
    mutate_touch,
    mutate_chmod,
    mutate_delete,
    mutate_create,
    mutate_rename,
    mutate_replace_by_rename,
    mutate_symlink,
    mutate_unlink_symlink,
    mutate_truncate,
    mutate_ignore_rule,
)


# --------------------------------------------------------------------------- #
# 1. Randomized mutation                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", range(16))
def test_the_fast_path_equals_full_hashing_under_random_mutation(tmp_path: Path, seed: int) -> None:
    rng = Random(seed)
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    history = build(root, rng, files=60)
    agree(root, state_dir, "the first walk already disagreed")

    applied: list[str] = []
    for round_index in range(10):
        for _ in range(rng.randrange(1, 7)):
            applied.append(MUTATIONS[rng.randrange(len(MUTATIONS))](root, rng, history))
        agree(root, state_dir, f"seed {seed} round {round_index} after: {applied[-6:]}")


def test_a_walk_with_a_cache_really_does_less_work(tmp_path: Path) -> None:
    """Otherwise every test above passes against a cache that never hits."""
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(1), files=60)

    first = cached_walk(root, state_dir)
    assert first.digests.hashed == len(first.files)
    assert first.digests.reused == 0

    second = cached_walk(root, state_dir)
    assert second.digests.hashed == 0, "a second walk re-hashed an unchanged tree"
    assert second.digests.reused_by_path == len(second.files)

    (root / "pkg0/sub0/unit_0000.py").write_bytes(b"CHANGED = 1\n")
    third = cached_walk(root, state_dir)
    assert third.digests.hashed == 1, "exactly the changed file should be re-read"
    assert third.digests.reused_by_path == len(third.files) - 1


# --------------------------------------------------------------------------- #
# 2. The adversarial cases, named                                             #
# --------------------------------------------------------------------------- #


def test_same_size_and_same_mtime_with_different_content_is_not_reused(tmp_path: Path) -> None:
    """The forgery the whole design turns on. ``ctime`` is what catches it."""
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(2), files=8)
    target = root / "pkg0/sub0/unit_0000.py"
    target.write_bytes(b"AAAAAAAAAA\n")
    first = agree(root, state_dir)
    original_digest = {walked.path: walked.content_digest for walked in first.files}["pkg0/sub0/unit_0000.py"]

    before = target.stat()
    target.write_bytes(b"BBBBBBBBBB\n")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = target.stat()
    assert after.st_size == before.st_size and after.st_mtime_ns == before.st_mtime_ns

    second = agree(root, state_dir, "a same-size, same-mtime rewrite was served from the cache")
    replaced = {walked.path: walked.content_digest for walked in second.files}["pkg0/sub0/unit_0000.py"]
    assert replaced != original_digest
    assert replaced == hashlib.sha256(b"BBBBBBBBBB\n").hexdigest()
    assert second.digests.hashed >= 1


def test_a_file_restored_to_an_earlier_version_gets_the_earlier_digest(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(3), files=8)
    target = root / "pkg1/sub1/unit_0001.ts"
    first_body = b"VERSION = 1\n"
    target.write_bytes(first_body)
    agree(root, state_dir)

    target.write_bytes(b"VERSION = 2\n")
    agree(root, state_dir)

    target.write_bytes(first_body)
    restored = agree(root, state_dir, "a restored file kept a stale digest")
    digest = {walked.path: walked.content_digest for walked in restored.files}["pkg1/sub1/unit_0001.ts"]
    assert digest == hashlib.sha256(first_body).hexdigest()


def test_a_touched_but_unchanged_file_is_re_hashed_and_keeps_its_digest(tmp_path: Path) -> None:
    """The cache is a stat cache, so a touch costs a hash -- and nothing else."""
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(4), files=8)
    target = root / "pkg2/sub2/unit_0002.go"
    before = agree(root, state_dir)
    digest_before = {walked.path: walked.content_digest for walked in before.files}["pkg2/sub2/unit_0002.go"]

    os.utime(target, (time.time() - 5_000, time.time() - 5_000))
    after = agree(root, state_dir)
    assert after.digests.hashed == 1, "a touch should cost exactly one hash"
    digest_after = {walked.path: walked.content_digest for walked in after.files}["pkg2/sub2/unit_0002.go"]
    assert digest_after == digest_before


def test_a_symlink_is_never_served_from_the_cache_and_never_followed(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(5), files=8)
    victim = root / "pkg0/sub0/unit_0000.py"
    other = root / "pkg1/sub1/unit_0001.ts"
    other.write_bytes(b"TARGET = 1\n")
    agree(root, state_dir)

    victim.unlink()
    os.symlink(other.resolve(), victim)
    after = agree(root, state_dir, "a path that became a symlink was still manifested")
    assert "pkg0/sub0/unit_0000.py" not in {walked.path for walked in after.files}
    assert after.symlinks == 1


def test_a_mode_change_changes_the_row_without_changing_the_digest(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(6), files=8)
    target = root / "pkg3/sub0/unit_0003.rs"
    before = agree(root, state_dir)
    row_before = {walked.path: (walked.mode, walked.content_digest) for walked in before.files}[
        "pkg3/sub0/unit_0003.rs"
    ]
    assert row_before[0] == 0o100644

    target.chmod(0o755)
    after = agree(root, state_dir, "a chmod was hidden by the cache")
    row_after = {walked.path: (walked.mode, walked.content_digest) for walked in after.files}["pkg3/sub0/unit_0003.rs"]
    assert row_after[0] == 0o100755
    assert row_after[1] == row_before[1]
    assert before.root() != after.root(), "the manifest root must move when a mode does"


def test_a_replace_by_rename_is_not_served_from_the_previous_inode(tmp_path: Path) -> None:
    """An atomic save writes a new file and renames it over the old one."""
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(7), files=8)
    target = root / "pkg4/sub1/unit_0004.md"
    target.write_bytes(b"SAME LENGTH\n")
    before = target.stat()
    agree(root, state_dir)

    incoming = target.with_name("incoming")
    incoming.write_bytes(b"DIFFERENT!!\n")
    os.utime(incoming, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(incoming, target)
    assert target.stat().st_size == before.st_size
    assert target.stat().st_mtime_ns == before.st_mtime_ns

    after = agree(root, state_dir, "a replace-by-rename was served from the cache")
    digest = {walked.path: walked.content_digest for walked in after.files}["pkg4/sub1/unit_0004.md"]
    assert digest == hashlib.sha256(b"DIFFERENT!!\n").hexdigest()


def test_a_file_written_during_its_own_hash_is_never_remembered(tmp_path: Path) -> None:
    """The race guard, at the only place it can be observed: the persisted file."""
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(8), files=8)
    target = root / "pkg5/sub2/unit_0005.json"
    # A timestamp inside -- indeed ahead of -- the walk's own window is exactly
    # what a file being written during the walk would carry.
    future = time.time() + 3_600
    os.utime(target, (future, future))

    cached_walk(root, state_dir)
    second = cached_walk(root, state_dir)
    reused = {walked.path for walked in second.files}
    assert "pkg5/sub2/unit_0005.json" in reused
    assert second.digests.hashed >= 1, "a file stamped inside the race window was trusted"

    cache = load_walk_cache(state_dir, root)
    assert "pkg5/sub2/unit_0005.json" not in cache.by_path


# --------------------------------------------------------------------------- #
# 3. git driving the mutations                                                #
# --------------------------------------------------------------------------- #


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert GIT is not None
    return subprocess.run(
        [
            GIT,
            "-c",
            "user.email=b@example.invalid",
            "-c",
            "user.name=B",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(root),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
@pytest.mark.parametrize("seed", range(6))
def test_the_fast_path_equals_full_hashing_across_real_git_operations(tmp_path: Path, seed: int) -> None:
    """Commits, branches and checkouts -- the states the blob map exists for.

    The tree is deliberately larger than ``MINIMUM_INDEX_MISSES``: below that the
    walk never opens git's index at all, because parsing it would cost more than
    the hashing it saves, and a test that stayed under the threshold would be
    asserting against a code path that never ran.
    """
    rng = Random(1000 + seed)
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    files = MINIMUM_INDEX_MISSES * 3
    shutil.rmtree(root, ignore_errors=True)
    history = build(root, rng, files=files)
    shutil.rmtree(root / ".git")
    assert run_git(root, "init", "--initial-branch=main").returncode == 0
    assert run_git(root, "add", "-A").returncode == 0
    assert run_git(root, "commit", "-m", "initial").returncode == 0
    agree(root, state_dir, "the first walk of a git worktree disagreed")

    # A branch where every file differs, so a checkout moves the whole tree.
    assert run_git(root, "checkout", "-b", "feature").returncode == 0
    for relative in list(history):
        target = root / relative
        if target.is_file():
            target.write_bytes(target.read_bytes() + b"# feature\n")
    assert run_git(root, "add", "-A").returncode == 0
    assert run_git(root, "commit", "-m", "feature").returncode == 0

    operations = (
        ("checkout", "main"),
        ("checkout", "feature"),
        ("checkout", "main"),
        ("stash", "list"),
        ("checkout", "feature"),
    )
    for index in range(8):
        if rng.random() < 0.5:
            MUTATIONS[rng.randrange(len(MUTATIONS))](root, rng, history)
        else:
            run_git(root, *operations[index % len(operations)])
        agree(root, state_dir, f"git seed {seed} step {index}")


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
def test_a_checkout_is_served_from_the_blob_map_rather_than_re_hashed(tmp_path: Path) -> None:
    """The reason the blob map exists, asserted as work avoided."""
    rng = Random(77)
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    files = MINIMUM_INDEX_MISSES * 4
    history = build(root, rng, files=files)
    shutil.rmtree(root / ".git")
    assert run_git(root, "init", "--initial-branch=main").returncode == 0
    assert run_git(root, "add", "-A").returncode == 0
    assert run_git(root, "commit", "-m", "initial").returncode == 0
    cached_walk(root, state_dir)  # learns both maps

    assert run_git(root, "checkout", "-b", "feature").returncode == 0
    for relative in list(history):
        target = root / relative
        if target.is_file():
            target.write_bytes(target.read_bytes() + b"# feature\n")
    assert run_git(root, "add", "-A").returncode == 0
    assert run_git(root, "commit", "-m", "feature").returncode == 0
    agree(root, state_dir, "the feature branch disagreed")  # learns the feature blobs

    assert run_git(root, "checkout", "main").returncode == 0
    time.sleep(0.05)
    run_git(root, "update-index", "--refresh")
    back = agree(root, state_dir, "the checkout back to main disagreed")
    assert back.digests.reused_by_blob > files // 2, (
        f"only {back.digests.reused_by_blob} of {files} came from the blob map "
        f"({back.digests.hashed} were re-hashed)"
    )


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
def test_a_small_change_never_pays_to_parse_the_index(tmp_path: Path) -> None:
    """The cost gate: a two-file edit must not read a whole repository's index."""
    rng = Random(78)
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, rng, files=MINIMUM_INDEX_MISSES * 3)
    shutil.rmtree(root / ".git")
    assert run_git(root, "init", "--initial-branch=main").returncode == 0
    assert run_git(root, "add", "-A").returncode == 0
    assert run_git(root, "commit", "-m", "initial").returncode == 0
    cached_walk(root, state_dir)

    (root / "pkg0/sub0/unit_0000.py").write_bytes(b"EDITED = 1\n")
    (root / "pkg1/sub1/unit_0001.ts").write_bytes(b"EDITED = 2\n")
    cache = load_walk_cache(state_dir, root)
    result = walk_worktree(root, size_cap=CAP, cache=cache)
    assert result.digests.hashed == 2
    assert cache.index_read is False, "two changed files were enough to parse the index"


# --------------------------------------------------------------------------- #
# 4. The file on disk                                                         #
# --------------------------------------------------------------------------- #


def test_the_cache_lives_in_the_state_directory_and_nowhere_else(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(9), files=12)
    before = {path for path in root.rglob("*")}

    cached_walk(root, state_dir)

    target = cache_path(state_dir, root)
    assert target.is_file()
    assert str(target).startswith(str(state_dir) + os.sep)
    assert target.stat().st_mode & 0o777 == 0o600
    assert {path for path in root.rglob("*")} == before, "the walk wrote inside the worktree"


def test_two_worktrees_never_share_a_cache_file(tmp_path: Path) -> None:
    """Two checkouts of the same repository have different content at the same paths."""
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    first = tmp_path / "one"
    second = tmp_path / "two"
    build(first, Random(10), files=10)
    build(second, Random(10), files=10)
    (second / "pkg0/sub0/unit_0000.py").write_bytes(b"DIFFERENT = 1\n")

    cached_walk(first, state_dir)
    assert cache_path(state_dir, first) != cache_path(state_dir, second)
    agree(second, state_dir, "one worktree's cache answered for another")


def _miscount(raw: bytes) -> bytes:
    """The header's row count, made to disagree with the rows that follow."""
    header, _, rest = raw.partition(b"\n")
    fields = header.split(b" ")
    fields[2] = str(int(fields[2]) + 1).encode("ascii")
    return b" ".join(fields) + b"\n" + rest


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda raw: raw[: len(raw) // 2], id="truncated-mid-row"),
        pytest.param(
            lambda raw: raw[: raw.rindex(b"\n", 0, len(raw) // 2)],
            id="truncated-on-a-row-boundary",
        ),
        pytest.param(lambda raw: b"lemoncrow-walkcache/9 1 1\n" + raw, id="wrong-format"),
        pytest.param(lambda raw: raw.replace(b"\x00", b"|", 3), id="mangled-separators"),
        pytest.param(lambda raw: b"", id="empty"),
        pytest.param(lambda raw: raw + b"\nnot-a-row", id="trailing-garbage"),
        pytest.param(_miscount, id="header-count-disagrees-with-the-rows"),
    ],
)
def test_a_damaged_cache_is_discarded_rather_than_believed(tmp_path: Path, corrupt: Callable[[bytes], bytes]) -> None:
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(11), files=12)
    cached_walk(root, state_dir)
    target = cache_path(state_dir, root)
    target.write_bytes(corrupt(target.read_bytes()))

    cache = load_walk_cache(state_dir, root)
    result = walk_worktree(root, size_cap=CAP, cache=cache)
    assert result.digests.reused_by_path == 0, "a damaged cache was used"
    assert rows(result) == rows(full_walk(root))


def _stat_like(info: os.stat_result, **overrides: int) -> os.stat_result:
    """A ``stat_result`` identical to ``info`` except for the named fields.

    The only way to vary one stat field at a time: a filesystem will not hand
    out two files that differ solely in inode, and ``ctime`` cannot be set by any
    public call at all -- which is exactly why it is in the identity.
    """
    fields = {
        "st_mode": info.st_mode,
        "st_ino": info.st_ino,
        "st_dev": info.st_dev,
        "st_nlink": info.st_nlink,
        "st_uid": info.st_uid,
        "st_gid": info.st_gid,
        "st_size": info.st_size,
        "st_atime_ns": info.st_atime_ns,
        "st_mtime_ns": info.st_mtime_ns,
        "st_ctime_ns": info.st_ctime_ns,
    }
    fields.update(overrides)
    return os.stat_result(
        (
            fields["st_mode"],
            fields["st_ino"],
            fields["st_dev"],
            fields["st_nlink"],
            fields["st_uid"],
            fields["st_gid"],
            fields["st_size"],
            0,
            0,
            0,
        ),
        {
            "st_atime_ns": fields["st_atime_ns"],
            "st_mtime_ns": fields["st_mtime_ns"],
            "st_ctime_ns": fields["st_ctime_ns"],
        },
    )


@pytest.mark.parametrize(
    "field",
    ["st_size", "st_mtime_ns", "st_ctime_ns", "st_ino", "st_dev", "st_mode"],
)
def test_every_field_of_the_stat_identity_is_load_bearing(tmp_path: Path, field: str) -> None:
    """Change one field, lose the hit. Each of the six, on its own.

    ``ctime`` alone would catch most of these on Linux; ``inode`` alone would
    catch a replace-by-rename on a platform whose ``ctime`` is a creation time.
    The point of the test is that no field is decoration, so none of them can be
    dropped on the grounds that another happens to cover the same case today.
    """
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(15), files=6)
    cached_walk(root, state_dir)

    relative = "pkg0/sub0/unit_0000.py"
    info = os.lstat(root / relative)
    cache = load_walk_cache(state_dir, root)
    cache.begin()
    assert cache.lookup(relative, info) is not None, "the unmodified file did not even hit"

    current = getattr(info, field)
    altered = _stat_like(info, **{field: current + (1 if field != "st_mode" else 0o111)})
    assert cache.lookup(relative, altered) is None, f"a changed {field} was still served from the cache"


def test_a_cache_for_a_vanished_file_does_not_survive_the_next_walk(tmp_path: Path) -> None:
    """A cache that only grows eventually costs more to read than it saves."""
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(12), files=20)
    cached_walk(root, state_dir)
    assert len(load_walk_cache(state_dir, root).by_path) == 21  # the files plus .gitignore

    for path in list(root.rglob("*.py")):
        path.unlink()
    cached_walk(root, state_dir)
    remaining = load_walk_cache(state_dir, root).by_path
    assert not any(relative.endswith(".py") for relative in remaining)


def test_the_blob_map_is_bounded_and_keeps_what_the_walk_just_learned(tmp_path: Path) -> None:
    """The one map with no natural end: every branch ever seen adds to it."""
    from lemoncrow_client.walkcache import _MAX_BLOBS

    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(16), files=4)

    cache = load_walk_cache(state_dir, root)
    cache.by_blob = {f"{index:040x}": f"{index:064x}" for index in range(_MAX_BLOBS)}
    walk_worktree(root, size_cap=CAP, cache=cache)
    learned = {f"f{index:039x}": f"e{index:063x}" for index in range(500)}
    cache.fresh_blob.update(learned)
    assert persist_walk_cache(cache, state_dir) is not None

    reloaded = load_walk_cache(state_dir, root)
    assert len(reloaded.by_blob) == _MAX_BLOBS, len(reloaded.by_blob)
    for oid, digest in learned.items():
        assert reloaded.by_blob[oid] == digest, "the freshly learned blobs were the ones dropped"


def test_caches_for_many_worktrees_are_bounded(tmp_path: Path) -> None:
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    for index in range(20):
        root = tmp_path / f"repo{index:02d}"
        build(root, Random(index), files=4)
        cached_walk(root, state_dir)
        time.sleep(0.002)  # so the retention order is well defined
    kept = list((state_dir / "walk-cache").glob("*.walkcache"))
    assert 1 <= len(kept) <= 12, f"{len(kept)} cache files survived"


def test_an_unreachable_server_leaves_the_state_directory_empty(tmp_path: Path) -> None:
    """The offline property, at the level the cache could have broken it.

    The cache is written after a view opens, so a session that never opened one
    has nothing to write -- which is what keeps the packaging audit's "an offline
    session writes nothing at all" true with a cache in the picture.
    """
    import socket

    from lemoncrow_client.config import load_config
    from lemoncrow_client.session import RemoteSession

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(13), files=10)
    config = load_config(
        {
            "LEMONCROW_URL": f"http://127.0.0.1:{port}",
            "LEMONCROW_TOKEN": "offline-token-0123456789abcdef",
            "LEMONCROW_HOME": str(state_dir),
            "HOME": str(tmp_path),
            "LEMONCROW_REQUEST_TIMEOUT_S": "2",
        },
        cwd=root,
    )
    report = RemoteSession(config).bootstrap(deadline_s=5.0)
    assert report.ok is False
    assert list(state_dir.iterdir()) == [], "an offline session wrote to the state directory"


def test_a_cache_that_cannot_be_written_is_not_an_error(tmp_path: Path) -> None:
    """A read-only home costs a slow session, never a failed one."""
    root = tmp_path / "worktree"
    state_dir = tmp_path / "home"
    state_dir.mkdir()
    build(root, Random(14), files=8)
    state_dir.chmod(0o500)
    try:
        cache = WalkCache(repo_root=root)
        result = walk_worktree(root, size_cap=CAP, cache=cache)
        assert persist_walk_cache(cache, state_dir) is None
        assert rows(result) == rows(full_walk(root))
    finally:
        state_dir.chmod(0o700)
