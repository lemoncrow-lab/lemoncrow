"""Raw-diff extraction for `lc review` — pygit2 only, one pass, no subprocess.

Why this module exists: every other diff path in the tree forks ``git`` once
per file, which is the single biggest cost in reviewing a large change from a
fully-imported, multi-threaded process (each fork copies the parent's page
table). Everything the packet needs — revision resolution, merge-base, rename
detection, hunk geometry, file classification and the two blob sides — comes
out of one libgit2 diff, so the builder above never learns about git plumbing.

Revision resolution is deliberately total: an unresolvable spec raises
``ValueError`` with the offending spec, and every other failure mode (empty
repo, single-commit repo, mode-only delta, submodule, binary file) yields a
value plus a ``degraded`` signal rather than an exception.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from lemoncrow.core.capabilities.code_intel_contract import GitHistoryBootstrapError
from lemoncrow.core.foundation.paths import (
    DEFAULT_STORE_DIRNAME,
    resolve_workspace_root,
)
from lemoncrow.core.foundation.paths import (
    detect_git_root as _detect_git_root,
)
from lemoncrow.infra.tree_sitter.tags import detect_language
from lemoncrow.pro.code_intel.git_history import require_pygit2
from lemoncrow.pro.code_intel.git_history.renames import detect_renames

from .models import ChangedFile, DiffHunk, FileCategory, FileStatus, RangeMode, oversized_diff_reason

# The canonical libgit2 empty tree. Diffing against it is how a single-commit
# repo (no ``HEAD~1``) still produces a full "everything was added" packet.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Blobs larger than this are never decoded: the impact detectors are text
# diffing and a multi-megabyte minified bundle costs more than it can inform.
MAX_BLOB_BYTES = 512 * 1024
MAX_DIFF_FILE_BYTES = 1024 * 1024
MAX_REVIEW_MEDIA_BYTES = 32 * 1024 * 1024

# The largest hunk body ``collect_diff(..., with_patch_text=True)`` will carry.
# A hunk is one edit; 64 KB is far above any hand-written one and small enough
# that a generated-file diff cannot turn a packet into a memory event. Over the
# cap the body is dropped and ``hunk_patch_truncated`` is named in ``degraded``,
# because a half-patch that still looks like a patch is worse than no patch.
MAX_HUNK_PATCH_BYTES = 64 * 1024

# Named in ``DiffResult.degraded`` when at least one hunk body exceeded the cap.
HUNK_PATCH_TRUNCATED_SIGNAL = "hunk_patch_truncated"
SYMLINK_CHANGE_SIGNAL = "symlink_change"
FILE_MODE_CHANGE_SIGNAL = "file_mode_change"

# Bound the classification blob peek so a 10k-file diff cannot turn into 10k
# odb lookups just to check for an ``@generated`` banner.
_MAX_PREVIEW_FILES = 500

_STATUS_CHAR_TO_STATUS: dict[str, FileStatus] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "typechange",
    # libgit2 reports workdir-only files as UNTRACKED ('?'). They are additions
    # for review purposes -- a file the agent created is exactly what a human
    # most needs to read.
    "?": "added",
}

_TEST_SEGMENTS = frozenset({"tests", "test", "__tests__", "spec"})
_TEST_BASENAME_RE = re.compile(r"^test_|_test\.|\.test\.|\.spec\.")
_VENDOR_SEGMENTS = frozenset({"vendor", "node_modules", "third_party", "3rdparty", ".venv", "site-packages", "bundle"})
_GENERATED_BASENAME_RE = re.compile(r"(\.lock|\.min\.js|\.pb\.go|_pb2\.py|\.generated\.[a-z]+)$")
_GENERATED_BASENAMES = frozenset({"uv.lock", "package-lock.json", "poetry.lock", "Cargo.lock"})
_GENERATED_MARKERS = ("@generated", "DO NOT EDIT")
_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})
_CONFIG_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"})
_CONFIG_BASENAMES = frozenset({"Makefile", "Dockerfile"})


@dataclass(frozen=True)
class RevRange:
    """A resolved comparison: what to diff, and how to describe it to a human."""

    mode: RangeMode
    base_rev: str
    head_rev: str
    base_sha: str
    head_sha: str
    merge_base_sha: str = ""
    dirty: bool = False
    title: str = ""
    # Submodules whose only dirt is inside their own repository. `is_dirty`
    # discounts them when picking the default range, so without carrying the
    # count the reviewer saw "working tree clean" over a `git status` that says
    # otherwise. `collect_diff` turns it back into the `submodule_dirty` signal.
    submodule_dirt_discounted: int = 0
    # A session-scoped Review of a shared worktree: only these repo-relative
    # paths belong to the Change. Empty means the whole range.
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiffResult:
    """The whole diff pass: the files plus whatever had to be approximated."""

    files: tuple[ChangedFile, ...]
    degraded: tuple[str, ...]


@dataclass(frozen=True)
class BlobPair:
    """Exact changed-file content captured with one review packet.

    ``old``/``new`` remain text-only because they feed symbol/impact analysis.
    ``binary_new`` freezes changed binary/media bytes for working-tree and staged
    reviews so later Preview/Compare surfaces never read a newer worktree file.
    """

    old: dict[str, str]
    new: dict[str, str]
    degraded: tuple[str, ...]
    binary_new: dict[str, bytes] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# repo discovery
# ---------------------------------------------------------------------------


def detect_repo_root(start: Path | None = None) -> Path:
    """Return the git root for *start*, or for cwd/the workspace when *start* is None.

    An explicit *start* is never second-guessed: reviewing some *other*
    repository because the named directory was not a checkout would silently
    answer a question the user did not ask. The workspace fallback exists only
    for the no-argument call, where "the repo I am working in" is the intent.

    Raises:
        ValueError: *start* is not inside a git repository, or -- when *start*
            is None -- neither cwd nor the resolved workspace root is.
    """

    if start is not None:
        base = Path(start).expanduser()
        found = _detect_git_root(base)
        if found is None:
            raise ValueError(f"not a git repository: {base}")
        return found

    base = Path.cwd()
    found = _detect_git_root(base)
    if found is not None:
        return found
    try:
        workspace = resolve_workspace_root()
    except Exception:
        workspace = None
    if workspace is not None:
        found = _detect_git_root(workspace)
        if found is not None:
            return found
    raise ValueError(f"not a git repository: {base}")


def _open_repo(repo_root: Path) -> Any:
    try:
        pygit2 = require_pygit2()
    except GitHistoryBootstrapError as exc:
        raise ValueError(f"pygit2 is required for `lc review`: {exc}") from exc
    try:
        repo = pygit2.Repository(str(repo_root))
    except Exception as exc:
        raise ValueError(f"not a git repository: {repo_root}") from exc
    _apply_sparse_ignore_rules(repo)
    return repo


_SKIP_WORKTREE_FLAG = 1 << 14  # GIT_INDEX_ENTRY_SKIP_WORKTREE, in flags_extended
_libgit2_add_rule: Any = None


def _sparse_gitignore_rules(repo: Any) -> str:
    """Ignore rules git reads from the index that libgit2 would not see.

    In a sparse checkout a ``.gitignore`` outside the cone is skip-worktree: not
    on disk, but git still reads it from the index. libgit2 does not, so a sparse
    checkout's ``node_modules`` (145k files in one measured submodule) read as
    untracked, were hashed on every source check and made the Review report
    phantom dirt. Returns them as repository-root rules (nested files keep their
    directory scope); empty for any repository that is not sparse.
    """

    try:
        if not repo.config.get_bool("core.sparseCheckout"):
            return ""
    except Exception:
        return ""
    from pygit2 import C, ffi

    handle = repo.index._index
    lines: list[str] = []
    for position in range(int(C.git_index_entrycount(handle))):
        entry = C.git_index_get_byindex(handle, position)
        if entry == ffi.NULL or not (int(entry.flags_extended) & _SKIP_WORKTREE_FLAG):
            continue
        path = str(ffi.string(entry.path).decode("utf-8", errors="replace"))
        if path != ".gitignore" and not path.endswith("/.gitignore"):
            continue
        blob = repo.get(repo.index[path].id)
        if blob is None:
            continue
        base = path[: -len(".gitignore")].rstrip("/")
        for raw in blob.data.decode("utf-8", errors="replace").splitlines():
            rule = raw.rstrip()
            if not rule or rule.startswith("#"):
                continue
            negated = rule.startswith("!")
            pattern = rule[1:] if negated else rule
            if base:
                anchored = "/" in pattern.rstrip("/")
                pattern = f"{base}/{pattern.lstrip('/')}" if anchored else f"{base}/**/{pattern}"
            lines.append(("!" if negated else "") + pattern)
    return "\n".join(lines)


def _apply_sparse_ignore_rules(repo: Any) -> None:
    """Give libgit2 the index-only ignore rules of a sparse checkout. Best effort."""

    global _libgit2_add_rule
    try:
        rules = _sparse_gitignore_rules(repo)
        if not rules:
            return
        if _libgit2_add_rule is None:
            # lc-debt: pygit2 does not bind git_ignore_add_rule; call the libgit2 it
            # already loaded. Drop this when pygit2 exposes it.
            import ctypes

            with open("/proc/self/maps", encoding="utf-8") as maps:
                loaded = next((line.split()[-1] for line in maps if "/libgit2" in line), "")
            if not loaded:
                _libgit2_add_rule = False
                return
            function = ctypes.CDLL(loaded).git_ignore_add_rule
            function.argtypes = (ctypes.c_void_p, ctypes.c_char_p)
            function.restype = ctypes.c_int
            _libgit2_add_rule = function
        if _libgit2_add_rule is False:
            return
        from pygit2 import ffi

        pointer = int(ffi.cast("uintptr_t", repo._repo))
        _libgit2_add_rule(pointer, rules.encode("utf-8"))
    except Exception:
        return


# ---------------------------------------------------------------------------
# revision resolution
# ---------------------------------------------------------------------------


def _commit_for(repo: Any, spec: str) -> Any:
    pygit2 = require_pygit2()
    try:
        obj = repo.revparse_single(spec)
        return obj.peel(pygit2.Commit)
    except Exception as exc:
        raise ValueError(f"cannot resolve revision {spec!r}") from exc


def _head_commit(repo: Any) -> Any | None:
    if repo.head_is_unborn:
        return None
    try:
        return _commit_for(repo, "HEAD")
    except ValueError:
        return None


def _summary(commit: Any | None, fallback: str) -> str:
    if commit is None:
        return fallback
    message = getattr(commit, "message", "") or ""
    lines = message.splitlines()
    return lines[0].strip() if lines else fallback


# libgit2's ``GIT_SUBMODULE_STATUS_*`` bits that describe the submodule's *own*
# repository rather than a change to this one. The four ``IN_*`` bits only say
# where the submodule is registered (HEAD tree, index, .gitmodules, working
# dir) and are set for every healthy submodule; the three ``WD_*`` content bits
# are the submodule's own index, its own working tree and its own untracked
# files. Deliberately absent: ``WD_MODIFIED`` (0x400 -- the checked-out commit
# no longer matches the pointer this repo records) and the ``INDEX_*`` bits
# (0x10/0x20/0x40), which *are* changes to this repository, plus
# ``WD_UNINITIALIZED``/``WD_ADDED``/``WD_DELETED`` (0x80/0x100/0x200).
_SUBMODULE_INTERNAL_STATUS = (
    0x0001  # IN_HEAD
    | 0x0002  # IN_INDEX
    | 0x0004  # IN_CONFIG
    | 0x0008  # IN_WD
    | 0x0800  # WD_INDEX_MODIFIED
    | 0x1000  # WD_WD_MODIFIED
    | 0x2000  # WD_UNTRACKED
)


def _submodule_paths(repo: Any) -> frozenset[str]:
    """Every registered submodule path, or empty when they cannot be listed."""

    try:
        return frozenset(str(path) for path in repo.listall_submodules())
    except Exception:
        return frozenset()


def _submodule_dirt_is_internal(repo: Any, path: str) -> bool:
    """True when the only dirt at *path* lives inside the submodule's own repo.

    ``repo.status()`` reports a submodule whose own working tree is dirty as a
    plain ``WT_MODIFIED`` entry, indistinguishable from a moved pointer. The
    submodule status bits are the only place the difference survives, and
    ``collect_diff`` already draws the same line: a gitlink naming the same
    commit on both sides is dropped and counted under ``SUBMODULE_DIRTY_SIGNAL``
    because uncommitted work in another repository is not part of this change.

    Unreadable bits count as real dirt: refusing to hide a change we could not
    disprove is the safer failure, and it is what this function did before.
    """

    try:
        bits = int(repo.submodules.status(path))
    except Exception:
        return False
    return bool(bits) and not bits & ~_SUBMODULE_INTERNAL_STATUS


def _dirt_state(repo: Any) -> tuple[bool, int]:
    """``(dirty, submodules discounted)`` -- :func:`is_dirty` plus what it ignored.

    The count exists so the discount is never silent: a repository whose only
    change is uncommitted work inside a submodule is *not* dirty for range
    purposes, but ``git status`` still reports it, and a reviewer told "working
    tree clean" over that has been told something git denies.
    """

    try:
        status = repo.status(untracked_files="normal", ignored=False)
    except Exception:
        return False, 0
    prefix = f"{DEFAULT_STORE_DIRNAME}/"
    candidates = [str(path) for path in status if not str(path).startswith(prefix)]
    if not candidates:
        return False, 0
    submodules = _submodule_paths(repo)
    non_submodule = [path for path in candidates if path not in submodules]
    # Probing a submodule walks its whole working tree, which is why it happens
    # here and not before: any ordinary changed file settles the question.
    if non_submodule:
        return True, 0
    internal = [path for path in candidates if _submodule_dirt_is_internal(repo, path)]
    # Dirty submodule worktrees are now reviewable as nested frozen revisions.
    # They therefore keep the parent in working-tree mode instead of silently
    # substituting the previous parent commit. ``collect_diff`` still avoids a
    # fake gitlink row; rendered surfaces consume the separately frozen nested
    # snapshot.
    if internal:
        return True, 0
    return True, 0


def is_dirty(repo: Any) -> bool:
    """Return True when the working tree or index differs from HEAD.

    Ignored files are excluded; an untracked-but-not-ignored file counts as
    dirty, matching ``git status --porcelain`` — with two exceptions.

    LemonCrow's own store directory is skipped. The impact analysis builds a code
    index at ``<repo>/.lemoncrow/workspace/*.sqlite``, so in a repository that has
    not gitignored it the *first* ``lc review`` leaves the tree "dirty" and the
    *second* one silently reviews the working tree instead of ``HEAD~1..HEAD``.
    A tool's own cache is not the user's uncommitted work, and a review command
    whose default range depends on whether it has been run before is broken.

    A submodule whose only dirt is inside its own repository is skipped for the
    same reason from the other end: ``collect_diff`` drops that delta on purpose
    (``SUBMODULE_DIRTY_SIGNAL``), so counting it here picks working-tree mode for
    a diff that is empty by construction — bare ``lc review`` renders ``0 files ·
    +0 -0`` instead of the documented ``HEAD~1..HEAD`` fallback. A submodule
    whose recorded *pointer* moved is a real change to this repo and still counts.
    """

    return _dirt_state(repo)[0]


def _parent_is_unfetched(repo: Any, commit: Any) -> bool:
    """True when *commit* records a parent this clone does not contain.

    A shallow clone grafts its boundary commits, so libgit2 reports
    ``parent_ids`` as empty for them -- byte for byte what it reports for a real
    root commit, which is why an unresolvable ``HEAD~1`` cannot tell the two
    apart. The raw ODB object still carries the ``parent`` headers the commit
    was written with, and that is the only place the difference survives.

    Gated on ``is_shallow`` so a deliberate ``git replace`` graft in a full
    clone keeps the root-commit fallback it has today.
    """

    try:
        if not bool(getattr(repo, "is_shallow", False)):
            return False
        _kind, raw = repo.odb.read(commit.id)
    except Exception:
        return False
    for line in bytes(raw).split(b"\n"):
        if not line:
            # The header block ends at the first blank line; the message below
            # it may say anything at all.
            return False
        if line.startswith(b"parent "):
            return True
    return False


def _merge_base_sha(repo: Any, left: Any, right: Any) -> str:
    try:
        oid = repo.merge_base(left.id, right.id)
    except Exception:
        return ""
    return str(oid) if oid is not None else ""


def _fallback_discount(
    head_commit: Any,
    base_sha: str,
    head_sha: str,
    *,
    dirty: bool,
    discounted: int,
) -> int:
    """*discounted*, but only for the range the submodule discount chose.

    The disclosure belongs to a diff of HEAD's first parent against HEAD taken
    over a tree that reads clean only because a submodule's own dirt was set
    aside -- and it belongs to that diff however the caller spelled it. A
    commit-range session stores two resolved SHAs unless *both* endpoints are
    canonical refs (``sources.local.source_ref``), so a bare ``lc review``,
    ``HEAD~1..HEAD``, ``HEAD~1...HEAD`` and ``lc review HEAD~1`` all persist the
    identical ``<parent>..<head>`` key and all reopen through
    ``base=``/``head=``. A spelling that stayed silent would be contradicted by
    its own Refresh, so the range is recognised by its shape and not by the
    words that asked for it.
    """

    if dirty or discounted <= 0 or head_commit is None:
        return 0
    if head_sha != str(head_commit.id):
        return 0
    parents = [str(parent) for parent in (getattr(head_commit, "parent_ids", None) or [])]
    return discounted if parents and base_sha == parents[0] else 0


# ---------------------------------------------------------------------------
# mutable branch Review base
# ---------------------------------------------------------------------------

_INTEGRATION_REMOTES: tuple[str, ...] = ("origin", "upstream")
_INTEGRATION_BRANCHES: tuple[str, ...] = ("main", "master")


def _branch_ref(repo: Any) -> str:
    try:
        if repo.head_is_unborn:
            name = str(repo.lookup_reference("HEAD").target or "")
            return name if name.startswith("refs/heads/") else ""
        if not repo.head_is_detached:
            name = str(repo.head.name or "")
            return name if name.startswith("refs/heads/") else ""
    except Exception:
        return ""
    # A rebase detaches HEAD for its whole duration. Routing those captures by
    # commit would open a new Review per replayed commit.
    gitdir = Path(str(repo.path))
    for state in ("rebase-merge", "rebase-apply"):
        try:
            name = (gitdir / state / "head-name").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if name.startswith("refs/heads/"):
            return name
    return ""


def current_branch_ref(repo_root: Path) -> str:
    """``refs/heads/<name>`` HEAD is on, or the branch a paused rebase is rewriting."""

    try:
        return _branch_ref(_open_repo(repo_root))
    except ValueError:
        return ""


def _integration_tips(repo: Any) -> tuple[Any, ...]:
    """Tips of the remote branches this repository integrates into.

    Only remote-tracking refs count. A local ``main`` may carry the Review's
    own unpushed commits (a Review started on ``main`` and continued on a
    branch), so treating it as upstream would drop the Review's own work.
    """

    pygit2 = require_pygit2()
    names: list[str] = []
    for remote in _INTEGRATION_REMOTES:
        try:
            symbolic = repo.references.get(f"refs/remotes/{remote}/HEAD")
        except Exception:
            symbolic = None
        if symbolic is not None and isinstance(symbolic.target, str):
            names.append(symbolic.target)
        names.extend(f"refs/remotes/{remote}/{branch}" for branch in _INTEGRATION_BRANCHES)
    tips: list[Any] = []
    for name in dict.fromkeys(names):
        try:
            reference = repo.references.get(name)
            if reference is None:
                continue
            oid = reference.resolve().peel(pygit2.Commit).id
        except Exception:
            continue
        if oid not in tips:
            tips.append(oid)
    return tuple(tips)


def _advance_review_base(repo: Any, frozen_base: str, head_id: Any, tips: Sequence[Any]) -> str:
    anchor: Any | None = None
    if frozen_base != EMPTY_TREE_SHA:
        try:
            anchor = _commit_for(repo, frozen_base).id
        except ValueError:
            return frozen_base
        if anchor != head_id and not repo.descendant_of(head_id, anchor):
            rewound = repo.merge_base(head_id, anchor)
            if rewound is None:
                return frozen_base
            anchor = rewound
    for tip in tips:
        point = repo.merge_base(head_id, tip)
        if point is None:
            continue
        if anchor is None or point == anchor or repo.descendant_of(point, anchor):
            anchor = point
    return frozen_base if anchor is None else str(anchor)


def review_branch_base(repo_root: Path, frozen_base: str, *, head: str = "", advance: bool = True) -> str:
    """The base a mutable branch Review diffs *head* (default ``HEAD``) against.

    ``frozen_base`` is where the Review started and stays a floor. Work the
    branch later absorbs from its remote integration branch (``git pull``,
    merging ``origin/main`` in, rebasing onto it) is someone else's change, so
    the base advances to ``merge-base(head, integration tip)`` whenever that
    point descends from the floor. History rewritten below the floor falls
    back to ``merge-base(head, frozen_base)``. Blank *frozen_base* returns
    blank: the Review has no branch base and the caller keeps its own range.
    ``advance=False`` (the ``fixed`` policy) keeps the floor as it is.

    A clone that no longer has the frozen base commit (pruned after a reword,
    shallow, or never fetched) also returns blank: the capture keeps ``HEAD``
    as its base rather than diffing from a commit it cannot read.

    Committing directly on ``main`` and pushing makes those commits integrated
    work too, so they leave the Draft (their checkpoints stay in history).
    """

    frozen = frozen_base.strip()
    if not frozen:
        return ""
    try:
        repo = _open_repo(repo_root)
        head_commit = _commit_for(repo, head) if head else _head_commit(repo)
    except ValueError:
        return frozen
    if frozen != EMPTY_TREE_SHA:
        try:
            _commit_for(repo, frozen)
        except ValueError:
            return ""
    if head_commit is None or not advance:
        return frozen
    return _advance_review_base(repo, frozen, head_commit.id, _integration_tips(repo))


def with_review_base(repo_root: Path, frozen_base: str, rng: RevRange, *, advance: bool = False) -> RevRange:
    """*rng* re-based onto its Review's branch base; commit ranges pass through."""

    if rng.mode not in {"working_tree", "staged"}:
        return rng
    base = review_branch_base(repo_root, frozen_base) if advance else frozen_base.strip()
    if not base or base == rng.base_sha:
        return rng
    return replace(rng, base_rev="(empty tree)" if base == EMPTY_TREE_SHA else base, base_sha=base)


def review_branch_commits(
    repo_root: Path,
    frozen_base: str,
    *,
    limit: int,
    advance: bool = True,
) -> tuple[tuple[str, str], ...] | None:
    """First-parent commits a branch Review owns, oldest first, as ``(sha, base)``.

    Commits reachable from an integration branch are hidden: upstream work that
    was pulled, merged or rebased in is not a checkpoint of this Review. Each
    ``base`` is :func:`review_branch_base` at that commit. ``None`` means more
    than *limit* commits qualified and the caller should not backfill. A clone
    without the frozen base commit owns none it can name: without the floor
    every ancestor would qualify, each diffed from a commit it cannot read.
    """

    frozen = frozen_base.strip()
    if not frozen:
        return ()
    pygit2 = require_pygit2()
    repo = _open_repo(repo_root)
    head_commit = _head_commit(repo)
    if head_commit is None:
        return ()
    if frozen != EMPTY_TREE_SHA:
        try:
            _commit_for(repo, frozen)
        except ValueError:
            return ()
    tips = _integration_tips(repo) if advance else ()
    walker = repo.walk(head_commit.id, pygit2.enums.SortMode.TOPOLOGICAL)
    walker.simplify_first_parent()
    bases = (frozen, _advance_review_base(repo, frozen, head_commit.id, tips)) if advance else (frozen,)
    for hidden in dict.fromkeys(bases):
        if hidden == EMPTY_TREE_SHA:
            continue
        try:
            walker.hide(_commit_for(repo, hidden).id)
        except ValueError:
            continue
    for tip in tips:
        walker.hide(tip)
    owned: list[Any] = []
    for commit in walker:
        owned.append(commit.id)
        if len(owned) > limit:
            return None
    owned.reverse()
    return tuple((str(oid), _advance_review_base(repo, frozen, oid, tips) if advance else frozen) for oid in owned)


def commits_in_head(repo_root: Path, shas: Sequence[str]) -> frozenset[str]:
    """The members of *shas* that are ``HEAD`` or one of its ancestors."""

    repo = _open_repo(repo_root)
    head_commit = _head_commit(repo)
    if head_commit is None:
        return frozenset()
    present: set[str] = set()
    for sha in shas:
        try:
            oid = _commit_for(repo, sha).id
        except ValueError:
            continue
        if oid == head_commit.id or repo.descendant_of(head_commit.id, oid):
            present.add(sha)
    return frozenset(present)


def resolve_new_review_range(repo_root: Path, *, base: str | None = None, staged: bool = False) -> RevRange:
    """Freeze --new at origin/<branch>, its default-branch fork point, or HEAD.

    Remote-tracking refs are local Git state; opening a Review never waits for
    a network fetch. An explicit base names one exact commit.
    """

    repo = _open_repo(repo_root)
    head = _head_commit(repo)
    rng = resolve_rev_range(repo_root, staged=staged, working_tree=not staged)
    if base is not None:
        frozen = str(_commit_for(repo, base).id)
    else:
        frozen = str(head.id) if head is not None else EMPTY_TREE_SHA
        branch = _branch_ref(repo).removeprefix("refs/heads/")
        remote = repo.references.get(f"refs/remotes/origin/{branch}") if branch else None
        if remote is not None:
            frozen = str(remote.resolve().peel(require_pygit2().Commit).id)
        elif head is not None:
            default_ref = repo.references.get("refs/remotes/origin/HEAD")
            candidates = [default_ref.target] if default_ref is not None and isinstance(default_ref.target, str) else []
            candidates.extend(f"refs/remotes/origin/{name}" for name in _INTEGRATION_BRANCHES)
            for name in dict.fromkeys(candidates):
                reference = repo.references.get(name)
                if reference is None:
                    continue
                tip = reference.resolve().peel(require_pygit2().Commit)
                point = repo.merge_base(head.id, tip.id)
                if point is None:
                    raise ValueError("origin's default branch has no common ancestor; choose --new --base <commit>")
                frozen = str(point)
                break
    return replace(rng, base_rev="(empty tree)" if frozen == EMPTY_TREE_SHA else frozen, base_sha=frozen)


def resolve_rev_range(
    repo_root: Path,
    rev: str | None = None,
    *,
    base: str | None = None,
    head: str | None = None,
    staged: bool = False,
    working_tree: bool = False,
) -> RevRange:
    """Resolve what to compare, deterministically.

    Precedence: ``--staged`` > ``--working-tree`` > explicit ``--base/--head`` >
    ``a...b`` (merge base) > ``a..b`` (literal) > a single spec (merge base with
    HEAD) > no argument (working tree when dirty, else ``HEAD~1..HEAD``).

    Raises:
        ValueError: a revision spec cannot be resolved, or *repo_root* is not a
            git repository.
    """

    repo = _open_repo(repo_root)
    head_commit = _head_commit(repo)
    dirty, discounted = _dirt_state(repo)
    head_sha = str(head_commit.id) if head_commit is not None else ""

    # `submodule_dirt_discounted` rides the range the discount chose, under
    # every spelling of it. `--staged` and `--working-tree` never carry it: they
    # substitute nothing, so stamping them would raise `submodule_dirty` over a
    # diff that withheld nothing, reporting degradation to a reviewer already
    # shown everything the range contained. (`--working-tree` still raises the
    # signal, from `skipped_submodule_dirty`, because a working-tree diff really
    # does drop the gitlink. Both modes are also re-entered as themselves on
    # reopen -- `range_for_session` dispatches on `range_mode` -- so the CLI and
    # the reader agree whatever they say.)
    #
    # The commit-range branches below all defer to `_fallback_discount` rather
    # than deciding for themselves. A commit-range session stores two resolved
    # SHAs unless both endpoints are canonical refs, so `lc review`,
    # `HEAD~1..HEAD`, `HEAD~1...HEAD` and `lc review HEAD~1` persist one and the
    # same key and every one of them reopens through `base=`/`head=`: a branch
    # that stayed silent would watch its own Refresh contradict it over a range
    # that has not moved.
    if staged:
        return RevRange(
            mode="staged",
            base_rev="HEAD" if head_commit is not None else "(empty tree)",
            head_rev="INDEX",
            base_sha=head_sha or EMPTY_TREE_SHA,
            head_sha="",
            merge_base_sha="",
            dirty=dirty,
            title="staged changes",
        )

    if working_tree:
        return RevRange(
            mode="working_tree",
            base_rev="HEAD" if head_commit is not None else "(empty tree)",
            head_rev="WORKDIR",
            base_sha=head_sha or EMPTY_TREE_SHA,
            head_sha="",
            merge_base_sha="",
            dirty=dirty,
            title="working tree",
        )

    if base is not None or head is not None:
        head_spec = head or "HEAD"
        head_obj = _commit_for(repo, head_spec)
        base_spec = base if base is not None else f"{head_spec}~1"
        base_obj = _commit_for(repo, base_spec)
        return RevRange(
            mode="commit_range",
            base_rev=base_spec,
            head_rev=head_spec,
            base_sha=str(base_obj.id),
            head_sha=str(head_obj.id),
            merge_base_sha="",
            dirty=dirty,
            title=_summary(head_obj, head_spec),
            submodule_dirt_discounted=_fallback_discount(
                head_commit, str(base_obj.id), str(head_obj.id), dirty=dirty, discounted=discounted
            ),
        )

    if rev:
        if "..." in rev:
            left, _, right = rev.partition("...")
            left_spec = left or "HEAD"
            right_spec = right or "HEAD"
            left_obj = _commit_for(repo, left_spec)
            right_obj = _commit_for(repo, right_spec)
            merge_base = _merge_base_sha(repo, left_obj, right_obj)
            left_sha = merge_base or str(left_obj.id)
            right_sha = str(right_obj.id)
            return RevRange(
                mode="commit_range",
                base_rev=left_spec,
                head_rev=right_spec,
                base_sha=left_sha,
                head_sha=right_sha,
                merge_base_sha=merge_base,
                dirty=dirty,
                title=_summary(right_obj, right_spec),
                submodule_dirt_discounted=_fallback_discount(
                    head_commit, left_sha, right_sha, dirty=dirty, discounted=discounted
                ),
            )
        if ".." in rev:
            left, _, right = rev.partition("..")
            left_spec = left or "HEAD"
            right_spec = right or "HEAD"
            left_obj = _commit_for(repo, left_spec)
            right_obj = _commit_for(repo, right_spec)
            left_sha = str(left_obj.id)
            right_sha = str(right_obj.id)
            return RevRange(
                mode="commit_range",
                base_rev=left_spec,
                head_rev=right_spec,
                base_sha=left_sha,
                head_sha=right_sha,
                merge_base_sha="",
                dirty=dirty,
                title=_summary(right_obj, right_spec),
                submodule_dirt_discounted=_fallback_discount(
                    head_commit, left_sha, right_sha, dirty=dirty, discounted=discounted
                ),
            )
        rev_obj = _commit_for(repo, rev)
        if head_commit is None:
            raise ValueError(f"cannot resolve revision {rev!r}")
        merge_base = _merge_base_sha(repo, rev_obj, head_commit)
        rev_base_sha = merge_base or str(rev_obj.id)
        return RevRange(
            mode="commit_range",
            base_rev=rev,
            head_rev="HEAD",
            base_sha=rev_base_sha,
            head_sha=head_sha,
            merge_base_sha=merge_base,
            dirty=dirty,
            title=_summary(head_commit, "HEAD"),
            submodule_dirt_discounted=_fallback_discount(
                head_commit, rev_base_sha, head_sha, dirty=dirty, discounted=discounted
            ),
        )

    if dirty:
        return RevRange(
            mode="working_tree",
            base_rev="HEAD" if head_commit is not None else "(empty tree)",
            head_rev="WORKDIR",
            base_sha=head_sha,
            head_sha="",
            merge_base_sha="",
            dirty=True,
            title="working tree",
        )

    if head_commit is None:
        raise ValueError("cannot resolve revision 'HEAD'")

    try:
        parent = _commit_for(repo, "HEAD~1")
    except ValueError:
        if _parent_is_unfetched(repo, head_commit):
            # Not a root commit: the parent exists, this clone just does not
            # have it. Diffing against the empty tree here would present every
            # tracked file as newly added under the last commit's subject --
            # a review of the whole repository wearing the label of one change.
            raise ValueError(
                "cannot resolve revision 'HEAD~1': its commit is not in this clone "
                "(shallow history) -- fetch more history, or pass an explicit range"
            ) from None
        # Root commit: compare against the empty tree rather than failing, so a
        # freshly initialised repo still reviews.
        return RevRange(
            mode="commit_range",
            base_rev="(empty tree)",
            head_rev="HEAD",
            base_sha="",
            head_sha=head_sha,
            merge_base_sha="",
            dirty=False,
            title=_summary(head_commit, "HEAD"),
            submodule_dirt_discounted=discounted,
        )
    return RevRange(
        mode="commit_range",
        base_rev="HEAD~1",
        head_rev="HEAD",
        base_sha=str(parent.id),
        head_sha=head_sha,
        merge_base_sha="",
        dirty=False,
        title=_summary(head_commit, "HEAD"),
        # This is the fallback the discount produced: the tree reads clean only
        # because submodule-internal dirt was set aside, so the packet has to
        # say so or the "working tree clean" header contradicts `git status`.
        submodule_dirt_discounted=discounted,
    )


# ---------------------------------------------------------------------------
# file classification
# ---------------------------------------------------------------------------


def classify_path(path: str, *, head_preview: str = "") -> FileCategory:
    """Classify a repo-relative path into a review category. First match wins."""

    pure = PurePosixPath(path)
    segments = set(pure.parts[:-1])
    name = pure.name
    suffix = pure.suffix.lower()

    if segments & _TEST_SEGMENTS or _TEST_BASENAME_RE.search(name):
        return "test"
    if segments & _VENDOR_SEGMENTS:
        return "vendor"
    if name in _GENERATED_BASENAMES or _GENERATED_BASENAME_RE.search(name):
        return "generated"
    if head_preview:
        first_lines = "\n".join(head_preview.splitlines()[:5])
        if any(marker in first_lines for marker in _GENERATED_MARKERS):
            return "generated"
    if suffix in _DOC_SUFFIXES:
        return "docs"
    if suffix in _CONFIG_SUFFIXES or name in _CONFIG_BASENAMES:
        return "config"
    return "production"


# ---------------------------------------------------------------------------
# blob access
# ---------------------------------------------------------------------------


def _tree_for_sha(repo: Any, sha: str) -> Any:
    """Return the tree for *sha*, or the empty tree when *sha* is blank."""

    pygit2 = require_pygit2()
    if not sha:
        return repo.get(repo.TreeBuilder().write())
    try:
        return repo.get(sha).peel(pygit2.Tree)
    except Exception as exc:
        raise ValueError(f"cannot resolve revision {sha!r}") from exc


# Why the readers return a reason: "we skipped a 4 MB bundle" and "that path is
# a submodule pointer, not a blob" are different facts for a reviewer, and
# collapsing both into one `degraded` name would make the packet lie about why
# its impact recall dropped.
_OVERSIZE = "large_file_skipped"
_UNREADABLE = "blob_unreadable"

# A symbol name we can safely wrap in `\b...\b`. Anything else (a dotted path, a
# contract literal) gets waved through rather than mis-matched.
_BARE_IDENTIFIER_RE = re.compile(r"\w+")


def _blob_payload(blob: Any, *, max_bytes: int = MAX_BLOB_BYTES) -> tuple[bytes | None, str]:
    data = getattr(blob, "data", None)
    if not isinstance(data, bytes):
        return None, _UNREADABLE
    if len(data) > max_bytes:
        return None, _OVERSIZE
    return data, ""


def _blob_from_tree(repo: Any, tree: Any, path: str, *, max_bytes: int = MAX_BLOB_BYTES) -> tuple[bytes | None, str]:
    if tree is None or not path:
        return None, _UNREADABLE
    try:
        entry = tree / path
        blob = repo.get(entry.id)
    except Exception:
        return None, _UNREADABLE
    return _blob_payload(blob, max_bytes=max_bytes)


def _blob_from_index(repo: Any, path: str, *, max_bytes: int = MAX_BLOB_BYTES) -> tuple[bytes | None, str]:
    try:
        entry = repo.index[path]
        blob = repo.get(entry.id)
    except Exception:
        return None, _UNREADABLE
    return _blob_payload(blob, max_bytes=max_bytes)


def _blob_from_disk(repo_root: Path, path: str, *, max_bytes: int = MAX_BLOB_BYTES) -> tuple[bytes | None, str]:
    """The worktree bytes of *path* — as git records them, never as the link resolves.

    ``stat()``/``read_bytes()`` follow symlinks, and git does not: a symlink's
    blob *is* its target path (mode 120000). Following it read the target file
    instead, so a link pointing out of the repository put foreign bytes into the
    packet and the stored blob artifact, and the same tree fingerprinted
    differently in working-tree and staged mode. A link to a FIFO was worse:
    ``open()`` blocks forever there, somewhere ``except OSError`` cannot reach.

    Anything that is neither a regular file nor a symlink has no blob to read
    and may block on open, so it is unreadable rather than opened.
    """

    target = repo_root / path
    try:
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode):
            link = os.readlink(target)
            data = os.fsencode(link)
            if len(data) > max_bytes:
                return None, _OVERSIZE
            return data, ""
        if not stat.S_ISREG(info.st_mode):
            return None, _UNREADABLE
        if info.st_size > max_bytes:
            return None, _OVERSIZE
        return target.read_bytes(), ""
    except OSError:
        return None, _UNREADABLE


def _new_side_bytes(
    repo: Any,
    repo_root: Path,
    rng: RevRange,
    head_tree: Any,
    path: str,
    *,
    max_bytes: int = MAX_BLOB_BYTES,
) -> tuple[bytes | None, str]:
    if rng.mode == "commit_range":
        return _blob_from_tree(repo, head_tree, path, max_bytes=max_bytes)
    if rng.mode == "staged":
        return _blob_from_index(repo, path, max_bytes=max_bytes)
    return _blob_from_disk(repo_root, path, max_bytes=max_bytes)


def _decode(data: bytes | None) -> str:
    if data is None:
        return ""
    return data.decode("utf-8", errors="replace")


def head_path_filter(repo_root: Path, rng: RevRange) -> Callable[[str], bool]:
    """Return "does this repo-relative path exist in the revision under review?".

    The impact pass reaches outside the patch through a *persisted* code index,
    which is keyed to the workspace as it exists on disk right now -- not to the
    revision being reviewed. Reviewing an older commit therefore let it cite call
    sites in files that commit had never heard of, and a reviewer who opens one
    finds nothing there. This is the gate that keeps every out-of-patch claim
    inside the tree it claims to be about.

    A predicate rather than a materialised set: it is consulted only when a site
    is about to be reported, so a review with no out-of-patch reach never pays
    for a tree walk. Failure answers ``True`` -- an unreadable tree must not
    silently delete real findings.
    """

    try:
        repo = _open_repo(repo_root)
    except Exception:
        return lambda _path: True

    if rng.mode == "working_tree":
        # The "revision" here is the working tree itself, so the filesystem is
        # the authority -- including files git has never seen.
        def _on_disk(path: str) -> bool:
            try:
                return (repo_root / path).is_file()
            except OSError:
                return True

        return _on_disk

    if rng.mode == "staged":

        def _in_index(path: str) -> bool:
            try:
                return repo.index[path] is not None
            except Exception:
                return False

        return _in_index

    try:
        tree = _tree_for_sha(repo, rng.head_sha)
    except Exception:
        return lambda _path: True

    def _in_tree(path: str) -> bool:
        try:
            return (tree / path) is not None
        except Exception:
            return False

    return _in_tree


def _first_line_matching(lines: Sequence[str], pattern: re.Pattern[str], begin: int = 0) -> int | None:
    """1-based number of the first line at or after *begin* matching *pattern*."""

    for offset in range(max(0, begin), len(lines)):
        if pattern.search(lines[offset]):
            return offset + 1
    return None


def _anchor_line(text: str, caller: str) -> int:
    """Where in *text* the site naming *caller* points. 0 when nothing here knows.

    The index reports a caller's *definition* line, so that is what is looked for
    here -- in the reviewed revision's own copy of the file, which is the only
    text whose line numbers the packet is entitled to print.

    Preference order, widening only when the narrower answer is absent: the
    caller's definition line, then any mention of the caller. A dotted caller
    (``Class.method``) is searched from its owning ``class`` line down, because
    ``__init__`` alone matches every class in the file -- and it is *not*
    re-searched from the top afterwards. Restarting at line 0 hands back exactly
    what the class scope was found to exclude: a same-named method of a different
    class, printed under this one's name. A scope that finds nothing has found
    nothing.

    There is no third fallback. "Any mention of the changed symbol" is, in a
    caller file, overwhelmingly the ``import`` line -- so a site whose caller
    could not be located was cited at the import statement, under the caller's
    own name, as though that were where the call is. ``0`` is the honest answer
    and the code path already exists for it: the file is still cited, as a bare
    path, and nothing claims to know where.
    """

    lines = text.splitlines()
    if not lines:
        return 0
    owner, _, leaf = caller.rpartition(".")
    begin = 0
    if _BARE_IDENTIFIER_RE.fullmatch(owner):
        owning_class = _first_line_matching(lines, re.compile(rf"^[ \t]*class[ \t]+{re.escape(owner)}\b"))
        if owning_class is not None:
            begin = owning_class
    if _BARE_IDENTIFIER_RE.fullmatch(leaf):
        quoted = re.escape(leaf)
        patterns = (
            re.compile(rf"^[ \t]*(?:async[ \t]+)?(?:def|class|func|fn|function)[ \t]+{quoted}\b"),
            re.compile(rf"\b{quoted}\b"),
        )
        for pattern in patterns:
            hit = _first_line_matching(lines, pattern, begin)
            if hit is not None:
                return hit
    return 0


def head_symbol_filter(repo_root: Path, rng: RevRange) -> Callable[[str, str, str], int | None]:
    """Return "where in *path* does the revision under review put this call site?".

    Called as ``(path, symbol, caller) -> int | None``:

    * ``None`` -- the reviewed revision has no such call site, so drop it.
    * ``0``    -- keep it, but nothing here may claim to know *where*; the site
      is printed as a bare path.
    * ``> 0``  -- the line, **in the reviewed revision**, to print.

    :func:`head_path_filter` is file-granular, and file granularity is not enough.
    The index resolves call edges against the workspace, so a file that merely
    *still exists* at the reviewed revision was accepted as a call site of a
    symbol that revision's copy of it never named -- the caller was added later,
    or the whole import was rewritten since. The reviewer opens the cited line and
    finds unrelated code, which is the same failure as citing a missing file, one
    step further in.

    So the file check is kept and the blob at that revision is read and searched
    for the symbol as a whole word. Word-boundary, not parsed: an over-precise
    test here deletes real findings, and the caller has already survived four
    ambiguity guards by the time it gets asked.

    Returning the line rather than a yes/no is the other half of the same fix.
    Answering "yes, that call site is real" while the packet went on to print the
    *workspace's* line number for it sent the reviewer past the end of the file
    he was reviewing -- a commit range is precisely the case where the two
    coordinate systems disagree. The blob that proves the site exists is the blob
    that says where it is, so it is asked both questions at once.

    Failure answers ``0`` throughout -- an unreadable tree or an oversized blob
    must never silently delete a real finding, though it does forfeit the right
    to name a line. Blob text is cached per path because a central symbol's
    callers are re-checked once per symbol.
    """

    in_head = head_path_filter(repo_root, rng)

    try:
        repo = _open_repo(repo_root)
        head_tree = _tree_for_sha(repo, rng.head_sha) if rng.mode == "commit_range" else None
    except Exception:
        return lambda path, _symbol, _caller: 0 if in_head(path) else None

    cache: dict[str, str | None] = {}

    def _text_at_rev(path: str) -> str | None:
        if path not in cache:
            data, _reason = _new_side_bytes(repo, repo_root, rng, head_tree, path)
            cache[path] = None if data is None else _decode(data)
        return cache[path]

    def _locate(path: str, symbol: str, caller: str) -> int | None:
        if not in_head(path):
            return None
        text = _text_at_rev(path)
        if text is None:
            return 0
        if symbol and _BARE_IDENTIFIER_RE.fullmatch(symbol):
            if re.search(rf"\b{re.escape(symbol)}\b", text) is None:
                return None
        return _anchor_line(text, caller)

    return _locate


# ---------------------------------------------------------------------------
# intent-to-add
# ---------------------------------------------------------------------------

# `git add -N <path>` writes an index entry holding the *empty* blob plus the
# GIT_INDEX_ENTRY_INTENT_TO_ADD extended flag. Git's porcelain hides those from
# `git diff --cached` (`--ita-invisible-in-index` is its default) and `git
# commit` leaves them out of the tree it writes -- they are a promise to add,
# not staged content. libgit2's tree-to-index diff is plumbing and reports each
# one as an `A` with +0 -0, so an unfiltered `lc review --staged` contradicts
# both `git diff --cached` and what the next commit will actually contain.
_ITA_FLAG = 0x2000

# Emitted with a count because "we dropped some rows" is only actionable if the
# reviewer knows how many; the renderer matches on the name prefix.
ITA_EXCLUDED_SIGNAL = "intent_to_add_excluded"
ITA_UNDETERMINED_SIGNAL = "intent_to_add_undetermined"

# The file mode libgit2 reports for a gitlink -- a submodule pointer rather than
# a blob. Both sides carry it for an ordinary pointer bump.
_GITLINK_MODE = 0o160000

# Named with a count when a diff reported a submodule whose gitlink did not move.
# libgit2 marks such a submodule modified because the submodule's *own* working
# tree is dirty, and renders the delta as `-<sha>` / `+<sha>-dirty`: a `+1 -1`
# row for a pointer that is byte-identical on both sides. Uncommitted work inside
# another repository is not part of this change, and a row claiming it is is
# fabricated evidence -- so the row is dropped and this signal says so, with the
# count, because a reviewer who saw the submodule in `git status` needs to know
# where it went. A pointer bump between two *different* shas is a real,
# reviewable change and is kept.
SUBMODULE_DIRTY_SIGNAL = "submodule_dirty"

# Named with a count when rows under LemonCrow's own store directory were left
# out of a working-tree or staged diff. The skip itself is deliberate (the code
# index this very command writes lives there), but a silent skip is how a
# force-added `.lemoncrow/` file becomes a change nobody can see.
STORE_DIR_EXCLUDED_SIGNAL = "lemoncrow_store_excluded"


def _review_policy_path_inside_store(path: str) -> bool:
    """Repository-owned Review authority that must remain visible as Change content.

    ``.lemoncrow`` is also LemonCrow's local scratch root, so working-tree diffs
    normally exclude it. Phase 6 makes the repository Review policy and scoped
    rule-document namespace an explicit exception: these files can change future
    Review behavior and therefore must be captured in the immutable Snapshot.
    """

    return path == ".lemoncrow/review.yaml" or path.startswith(".lemoncrow/rules/")


# libgit2's ``GIT_DELTA_CONFLICTED``. Its ``status_char()`` is a space, so the
# char table cannot see it and the row falls through to the ``modified``
# default; the numeric delta status is the only place the fact survives.
_CONFLICTED_DELTA = 10

# Named with a count when the range contained an unmerged path. A conflicted
# entry has no stage-0 content: there is nothing to diff and nothing to
# fingerprint, so the row renders `+0 -0`, and a reviewer who is not told the
# path is unmerged reads that as "nothing changed here".
CONFLICTED_SIGNAL = "unmerged_paths"


def _is_gitlink(delta: Any) -> bool:
    """True when *delta* is a submodule pointer rather than a blob.

    Either side is enough: a directory that became a submodule (or stopped being
    one) is still a gitlink delta, and the blob readers cannot serve it.
    """

    for side in (getattr(delta, "new_file", None), getattr(delta, "old_file", None)):
        try:
            if int(getattr(side, "mode", 0) or 0) == _GITLINK_MODE:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _gitlink_unmoved(delta: Any) -> bool:
    """True when a gitlink delta names the same commit on both sides.

    That is the ``-dirty`` case: this repository records the same submodule
    commit it always did, and libgit2 raised the delta only because the
    submodule's own working tree has uncommitted content. Returning ``False``
    when the ids cannot be read keeps the entry -- refusing to show a change we
    could not disprove is safer than hiding one.
    """

    try:
        return str(delta.old_file.id) == str(delta.new_file.id)
    except Exception:
        return False


def _gitlink_oid(side: Any) -> str:
    """The commit id one side of a gitlink names, or "" when there is none.

    libgit2 reports the absent side of an added or removed submodule as the null
    oid. Passing that through would offer the reviewer forty zeros to paste into
    ``git show``; "" says "no pointer here" instead.
    """

    try:
        text = str(getattr(side, "id", "") or "")
    except Exception:
        return ""
    return text if text.strip("0") else ""


def _gitlink_pointer(delta: Any) -> tuple[str, str]:
    """``(old_commit, new_commit)`` for a gitlink delta.

    This *is* the content of a submodule change: there is no blob behind a
    gitlink, so the two commit ids are the whole reviewable fact, and carrying
    them on the row is what lets the renderer say what the row is instead of
    printing a ``+1 -1`` that counts git's own patch text.
    """

    return (
        _gitlink_oid(getattr(delta, "old_file", None)),
        _gitlink_oid(getattr(delta, "new_file", None)),
    )


def _intent_to_add_paths(repo: Any) -> frozenset[str] | None:
    """Return every intent-to-add path in the index, or None when undeterminable.

    ``pygit2.IndexEntry`` exposes only path/id/mode, so the extended flags are
    read through the cffi handle libgit2 already parsed for us -- cheaper and
    far safer than re-implementing the on-disk index format, which varies by
    version (v4 path compression) and hash algorithm.

    Returning ``None`` rather than an empty set keeps the two answers apart: an
    index with no placeholders and an index we could not inspect must not look
    the same to the caller, or a future pygit2 that moves the handle would
    silently restore the very divergence this exists to remove.
    """

    try:
        from pygit2 import C, ffi

        handle = repo.index._index
        found: set[str] = set()
        for position in range(int(C.git_index_entrycount(handle))):
            entry = C.git_index_get_byindex(handle, position)
            if entry == ffi.NULL:
                continue
            if int(entry.flags_extended) & _ITA_FLAG:
                found.add(str(ffi.string(entry.path).decode("utf-8", errors="replace")))
        return frozenset(found)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# the diff pass
# ---------------------------------------------------------------------------


def _build_diff(repo: Any, rng: RevRange, context_lines: int) -> Any:
    pygit2 = require_pygit2()
    base_tree = _tree_for_sha(repo, rng.base_sha)
    if rng.mode == "staged":
        return base_tree.diff_to_index(repo.index, context_lines=context_lines)
    if rng.mode == "working_tree":
        # A file the agent just created is untracked, and it is exactly what a
        # reviewer most needs to see -- so include untracked content (ignored
        # files stay excluded: INCLUDE_IGNORED is deliberately not set).
        flags = (
            pygit2.enums.DiffOption.INCLUDE_UNTRACKED
            | pygit2.enums.DiffOption.RECURSE_UNTRACKED_DIRS
            | pygit2.enums.DiffOption.SHOW_UNTRACKED_CONTENT
        )
        return base_tree.diff_to_workdir(flags=flags, context_lines=context_lines)
    head_tree = _tree_for_sha(repo, rng.head_sha)
    return repo.diff(base_tree, head_tree, context_lines=context_lines)


def _collapse(points: set[int]) -> tuple[tuple[int, int], ...]:
    """Fold touched line numbers into ascending, disjoint, inclusive ranges.

    Adjacent lines merge (``{4, 5, 6}`` is one ``(4, 6)``) so a block edit is one
    range rather than one per line; a gap of two or more keeps them apart, which
    is what lets a consumer tell "this whole body was rewritten" from "two
    unrelated lines moved".
    """

    ordered = sorted(point for point in points if point > 0)
    if not ordered:
        return ()
    spans: list[tuple[int, int]] = []
    low = high = ordered[0]
    for point in ordered[1:]:
        if point <= high + 1:
            high = point
            continue
        spans.append((low, high))
        low = high = point
    spans.append((low, high))
    return tuple(spans)


def _line_no(raw: Any) -> int:
    """A pygit2 line number, or 0 when that side of the line does not exist."""

    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


# libgit2's "\ No newline at end of file" markers: `>` when the new side gained
# one, `<` when the old side lost one. Neither is a line of the file.
_EOFNL_ORIGINS = frozenset({">", "<"})


def _hunk_body(hunk: Any) -> tuple[int, int, tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    """Walk one hunk's lines: ``(added, removed, new-side ranges, old-side ranges)``.

    The counts were the only thing read out of the body before, and that is why
    the geometry was wrong everywhere downstream: the header's
    ``+new_start,new_lines`` describes the *printed* hunk, context lines and all,
    so it is three lines too wide on each side under the default ``-U3``.
    Recording which lines actually carry a ``+``/``-`` costs one extra set
    insertion per line over the walk that already happens.

    A one-sided line still has an effect on the other side: an insertion sits
    *between* two base-side lines and a deletion *between* two new-side lines.
    Both are recorded as that pair, tracked by a cursor holding the last line
    number seen on the side in question, so a pure deletion is not invisible to a
    new-side range test.

    An unreadable body yields zeros and empty ranges -- the honest "unknown",
    which consumers must widen back to the full span rather than read as "nothing
    changed".
    """

    try:
        body = list(hunk.lines)
    except Exception:
        return 0, 0, (), ()
    added = 0
    removed = 0
    new_touched: set[int] = set()
    old_touched: set[int] = set()
    new_cursor = max(0, int(hunk.new_start) - 1)
    old_cursor = max(0, int(hunk.old_start) - 1)
    # The deletion join point is charged per *run* of changed lines rather than
    # per deleted line, because a run that also added something has already
    # named its own new-side position. In a delete+add replacement the cursor is
    # the last unchanged context line above the edit, and recording it reports a
    # byte-identical definition ending there as modified -- the exact false
    # positive `new_ranges` exists to remove.
    run_anchor: int | None = None
    run_added = False
    run_removed = False

    def close_run() -> None:
        nonlocal run_anchor, run_added, run_removed
        if run_anchor is not None and run_removed and not run_added:
            new_touched.update((run_anchor, run_anchor + 1))
        run_anchor = None
        run_added = False
        run_removed = False

    for line in body:
        origin = str(getattr(line, "origin", " "))
        old_no = _line_no(getattr(line, "old_lineno", 0))
        new_no = _line_no(getattr(line, "new_lineno", 0))
        if origin == "+":
            if run_anchor is None:
                run_anchor = new_cursor
            run_added = True
            added += 1
            if new_no:
                new_cursor = new_no
                new_touched.add(new_no)
            old_touched.update((old_cursor, old_cursor + 1))
        elif origin == "-":
            if run_anchor is None:
                run_anchor = new_cursor
            run_removed = True
            removed += 1
            if old_no:
                old_cursor = old_no
                old_touched.add(old_no)
        elif origin in _EOFNL_ORIGINS:
            # libgit2 emits a "\ No newline at end of file" marker line for each
            # side that gains or loses the final newline. It carries no line of
            # its own, so closing the run on it made a replacement of the last
            # line of a newline-less file look like a bare deletion and charge
            # the context line above -- reporting a byte-identical definition
            # ending there as modified. Neutral: it neither opens nor closes.
            continue
        else:
            close_run()
            if new_no:
                new_cursor = new_no
            if old_no:
                old_cursor = old_no
    close_run()
    return added, removed, _collapse(new_touched), _collapse(old_touched)


def _hunk_patch_text(hunk: Any, *, max_bytes: int | None = MAX_HUNK_PATCH_BYTES) -> tuple[str, bool]:
    """``(body, over the cap)`` for one hunk, as unified-diff text.

    Reconstructed from the same ``hunk.lines`` walk :func:`_hunk_body` already
    makes -- libgit2 exposes no raw per-hunk string, only the patch-wide one,
    and slicing that back apart would be writing a diff parser to read our own
    diff. The header is not included: it belongs to ``DiffHunk.header`` and it
    carries line numbers, which a content fingerprint must not.

    The cap is enforced *while* accumulating, so a pathological hunk is abandoned
    rather than materialised in full and then thrown away.

    ``("", False)`` on an unreadable body is the same honest "unknown" the rest
    of this module uses; consumers fall back to the header and the ranges.
    """

    try:
        body = list(hunk.lines)
    except Exception:
        return "", False
    out: list[str] = []
    size = 0
    for line in body:
        origin = str(getattr(line, "origin", " "))
        content = str(getattr(line, "content", ""))
        piece = f"{origin}{content}"
        size += len(piece.encode("utf-8", errors="replace"))
        if max_bytes is not None and size > max_bytes:
            return "", True
        out.append(piece)
    return "".join(out), False


def _hunks_for_patch(
    patch: Any,
    *,
    with_patch_text: bool = False,
    patch_text_limit: int | None = MAX_HUNK_PATCH_BYTES,
) -> tuple[tuple[DiffHunk, ...], int, int, bool]:
    """``(hunks, additions, deletions, any body dropped for exceeding the cap)``."""

    hunks: list[DiffHunk] = []
    additions = 0
    deletions = 0
    truncated = False
    try:
        raw_hunks = list(patch.hunks)
    except Exception:
        return (), 0, 0, False
    for hunk in raw_hunks:
        added, removed, new_ranges, old_ranges = _hunk_body(hunk)
        additions += added
        deletions += removed
        text = ""
        if with_patch_text:
            text, over_cap = _hunk_patch_text(hunk, max_bytes=patch_text_limit)
            truncated = truncated or over_cap
        hunks.append(
            DiffHunk(
                old_start=int(hunk.old_start),
                old_lines=int(hunk.old_lines),
                new_start=int(hunk.new_start),
                new_lines=int(hunk.new_lines),
                header=str(hunk.header).rstrip("\n"),
                added=added,
                removed=removed,
                new_ranges=new_ranges,
                old_ranges=old_ranges,
                patch=text,
            )
        )
    return tuple(hunks), additions, deletions, truncated


def collect_diff(
    repo_root: Path,
    rng: RevRange,
    *,
    context_lines: int = 3,
    with_patch_text: bool = False,
    patch_text_limit: int | None = MAX_HUNK_PATCH_BYTES,
) -> DiffResult:
    """Walk the range once and return every changed file plus degradation signals.

    *with_patch_text* fills ``DiffHunk.patch`` with each hunk's body. It is off
    by default so ``lc review``'s terminal and ``--json`` output stay what they
    were: the body is wanted only where a hunk has to be *fingerprinted* or
    re-rendered later, and carrying every diff line through a packet nobody asked
    to persist is a cost with no reader.
    """

    repo = _open_repo(repo_root)
    diff = _build_diff(repo, rng, context_lines)
    try:
        detect_renames(diff)
    except Exception:
        # Rename detection is an enrichment, not a precondition: a diff that
        # cannot be similarity-matched still reviews as add + delete.
        pass

    head_tree = _tree_for_sha(repo, rng.head_sha) if rng.mode == "commit_range" else None
    degraded: set[str] = set()
    files: list[ChangedFile] = []
    previews_taken = 0

    # Only `--staged` needs this: a working-tree diff reads the file from disk,
    # so an intent-to-add path already shows its real content there.
    intent_to_add: frozenset[str] = frozenset()
    if rng.mode == "staged":
        resolved = _intent_to_add_paths(repo)
        if resolved is None:
            degraded.add(ITA_UNDETERMINED_SIGNAL)
        else:
            intent_to_add = resolved
    skipped_intent_to_add = 0
    skipped_submodule_dirty = 0
    skipped_store_dir = 0
    conflicted_paths = 0
    scope = frozenset(rng.paths)

    for patch in diff:
        delta = patch.delta
        try:
            status_char = str(delta.status_char())
        except Exception:
            status_char = "M"
        status: FileStatus = _STATUS_CHAR_TO_STATUS.get(status_char, "modified")
        try:
            is_conflicted = int(getattr(delta, "status", 0) or 0) == _CONFLICTED_DELTA
        except (TypeError, ValueError):
            is_conflicted = False
        new_path = str(delta.new_file.path or "")
        old_path_raw = str(delta.old_file.path or "")
        path = old_path_raw if status == "deleted" else (new_path or old_path_raw)
        old_path = old_path_raw if status in ("renamed", "copied", "deleted") else None
        if scope and path not in scope and (old_path or "") not in scope:
            continue
        if (
            path.startswith(f"{DEFAULT_STORE_DIRNAME}/")
            and rng.mode != "commit_range"
            and not _review_policy_path_inside_store(path)
        ):
            # Same reasoning as `is_dirty`: the code index this very command
            # builds lives here, so a working-tree review of a repo that has not
            # gitignored it would otherwise open with four untracked sqlite
            # blobs it created itself. A commit range holds no scratch space of
            # ours -- everything in a tree was committed by someone -- so the
            # skip stops there, and where it does apply it is counted below
            # rather than dropped without trace.
            skipped_store_dir += 1
            continue
        if path in intent_to_add:
            # `git add -N` placeholder: nothing of it is staged, so `git diff
            # --cached` and the next commit both ignore it, and so do we. The
            # count is reported below rather than dropped silently.
            skipped_intent_to_add += 1
            continue
        old_mode = int(getattr(delta.old_file, "mode", 0) or 0)
        new_mode = int(getattr(delta.new_file, "mode", 0) or 0)
        if old_mode == 0o120000 or new_mode == 0o120000:
            degraded.add(SYMLINK_CHANGE_SIGNAL)
        elif (old_mode and new_mode and old_mode != new_mode) or (
            status in {"added", "renamed", "copied"} and new_mode == 0o100755
        ):
            degraded.add(FILE_MODE_CHANGE_SIGNAL)

        is_submodule = _is_gitlink(delta)
        if is_submodule and _gitlink_unmoved(delta):
            # Same commit on both sides: the only thing that changed is the
            # submodule's own working tree, which is a different repository and
            # not part of this change. Reported below, never rendered as a row.
            skipped_submodule_dirty += 1
            continue
        is_binary = bool(getattr(delta, "is_binary", False))
        oversized = not is_submodule and max(delta.old_file.size, delta.new_file.size) > MAX_DIFF_FILE_BYTES

        # A gitlink has no lines, and neither does a binary. For the gitlink the
        # zeros are load-bearing: git renders a pointer bump as a two-line
        # `-Subproject commit <old>` / `+Subproject commit <new>` patch, and
        # counting those two lines as `+1 -1` puts git's own prose into the
        # range header the reviewer reads first (`1 file · +1 -1` for a change
        # containing no line anyone can open) and into the JSON behind it. The
        # pair of commit ids on `submodule_pointer` is the whole change, so the
        # row carries that and no counts.
        #
        # The empty hunk tuple is the other half: the symbol and caller passes
        # anchor on hunk line ranges, so no hunks makes `3 known callers`
        # structurally unsayable about a pointer -- it would be a claim about
        # code this delta does not contain. `mode_only` is not raised either;
        # the row is not a patch that came up empty, it is a pointer, and it
        # says so.
        hunks: tuple[DiffHunk, ...] = ()
        additions = 0
        deletions = 0
        if not is_submodule and not is_binary and not oversized:
            hunks, additions, deletions, truncated = _hunks_for_patch(
                patch,
                with_patch_text=with_patch_text,
                patch_text_limit=patch_text_limit,
            )
            if truncated:
                degraded.add(HUNK_PATCH_TRUNCATED_SIGNAL)
            if not hunks and is_conflicted:
                # An unmerged path, not a mode change: there is no stage-0 entry
                # to diff, so the row is real and its content is unknown. Saying
                # `mode_only` here would name a cause that is not the cause.
                conflicted_paths += 1
            elif not hunks:
                # A pure rename has no textual hunk and is already named by its
                # status/old_path. Calling it `mode_only` invents the wrong cause.
                if status != "renamed" or old_mode != new_mode:
                    degraded.add("mode_only")

        preview = ""
        if not is_binary and not is_submodule and status != "deleted" and previews_taken < _MAX_PREVIEW_FILES:
            previews_taken += 1
            preview = _decode(_new_side_bytes(repo, repo_root, rng, head_tree, path)[0])[:4096]

        files.append(
            ChangedFile(
                path=path,
                old_path=old_path,
                status=status,
                similarity=int(getattr(delta, "similarity", 0) or 0),
                is_binary=is_binary,
                additions=additions,
                deletions=deletions,
                language=detect_language(Path(path)),
                # A gitlink is a pinned pointer, not source we can read: it has
                # no blob, no symbols and no callers, so ranking it alongside
                # production code would put an unreadable row above real files.
                category="config" if is_submodule else classify_path(path, head_preview=preview),
                hunks=hunks,
                submodule_pointer=_gitlink_pointer(delta) if is_submodule else None,
                diff_omitted_reason=oversized_diff_reason(MAX_DIFF_FILE_BYTES) if oversized else "",
                # libgit2 reports 0 for a side that does not exist, which is
                # exactly the packet's "not recorded" sentinel.
                old_mode=old_mode,
                new_mode=new_mode,
            )
        )

    if skipped_intent_to_add:
        degraded.add(f"{ITA_EXCLUDED_SIGNAL}:{skipped_intent_to_add}")
    if skipped_submodule_dirty:
        degraded.add(f"{SUBMODULE_DIRTY_SIGNAL}:{skipped_submodule_dirty}")
    elif rng.submodule_dirt_discounted:
        # The range picker discounted this dirt to choose the range at all, so
        # the diff never sees the delta and cannot count it. Say it anyway: the
        # header reads "working tree clean -- reviewing the last commit instead"
        # over a `git status` that reports the submodule, and a silent discount
        # is how the substitution stops being disclosed.
        degraded.add(f"{SUBMODULE_DIRTY_SIGNAL}:{rng.submodule_dirt_discounted}")
    if skipped_store_dir:
        degraded.add(f"{STORE_DIR_EXCLUDED_SIGNAL}:{skipped_store_dir}")
    if conflicted_paths:
        degraded.add(f"{CONFLICTED_SIGNAL}:{conflicted_paths}")

    files.sort(key=lambda item: item.path)
    return DiffResult(files=tuple(files), degraded=tuple(sorted(degraded)))


def commit_datetime(repo_root: Path, sha: str) -> datetime | None:
    """Return the commit time of *sha* as timezone-aware UTC, or None.

    The provenance correlator anchors its time window on this; for working-tree
    and staged ranges there is no commit, so None is the honest answer and the
    caller anchors on ``now()`` instead.
    """

    if not sha:
        return None
    try:
        repo = _open_repo(repo_root)
        commit = repo.get(sha)
        return datetime.fromtimestamp(int(commit.commit_time), tz=UTC)
    except Exception:
        return None


def load_blobs(
    repo_root: Path,
    rng: RevRange,
    files: tuple[ChangedFile, ...],
    *,
    max_bytes_by_path: Mapping[str, int] | None = None,
) -> BlobPair:
    """Return the exact changed content needed by Review.

    Text sides feed analysis/fingerprints and remain capped by the normal blob
    limit unless a caller supplies a purpose-specific per-path cap. Review's
    dependency normalizer uses that seam for supported generated lockfiles so
    semantic component targets can replace raw lockfile churn without widening
    the general source-analysis budget.

    A key is present only for a side that was actually read. A skipped or
    unreadable blob is **absent**, never ``""``: an empty string is a file we
    looked at, and storing one for a 946 KB lockfile let ``units._file_unit``
    fingerprint it as empty under ``blob_sha256`` -- a per-path constant that
    does not move however the file is rewritten, so a ``reviewed`` mark survived
    a full regeneration of content nobody had seen. Absence sends that unit down
    the ``unknown`` branch instead, which both downgrade guards already treat as
    untrustworthy. An added file's old side and a deleted file's new side are
    genuinely empty, not unread, and keep their ``""``.
    """

    repo = _open_repo(repo_root)
    base_tree = _tree_for_sha(repo, rng.base_sha)
    head_tree = _tree_for_sha(repo, rng.head_sha) if rng.mode == "commit_range" else None
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    binary_new: dict[str, bytes] = {}
    degraded: set[str] = set()

    for item in files:
        if item.is_binary:
            if item.status != "deleted":
                payload, reason = _new_side_bytes(
                    repo,
                    repo_root,
                    rng,
                    head_tree,
                    item.path,
                    max_bytes=MAX_REVIEW_MEDIA_BYTES,
                )
                if reason:
                    degraded.add(reason)
                elif payload is not None:
                    binary_new[item.path] = payload
            continue
        if item.submodule_pointer is not None:
            # A gitlink has no blob on either side: the old side resolves to a
            # commit this repository does not contain, and the new side is a
            # directory on disk. Both reads fail, and the `blob_unreadable` they
            # used to raise carried a remedy -- `lc runtime index` -- that cannot
            # apply to a pointer, sending the reviewer to rebuild an index that
            # would never have held this row. Skipped like a binary: the row
            # still renders, carrying the two commit ids that *are* its content.
            continue
        path_max_bytes = (
            max_bytes_by_path.get(item.path, MAX_BLOB_BYTES) if max_bytes_by_path is not None else MAX_BLOB_BYTES
        )
        old_bytes: bytes | None = None
        new_bytes: bytes | None = None
        old_reason = ""
        new_reason = ""
        if item.status != "added":
            old_bytes, old_reason = _blob_from_tree(
                repo, base_tree, item.old_path or item.path, max_bytes=path_max_bytes
            )
            if old_reason:
                degraded.add(old_reason)
        if item.status != "deleted":
            new_bytes, new_reason = _new_side_bytes(
                repo, repo_root, rng, head_tree, item.path, max_bytes=path_max_bytes
            )
            if new_reason:
                degraded.add(new_reason)
        if not old_reason:
            old[item.path] = _decode(old_bytes)
        if not new_reason:
            new[item.path] = _decode(new_bytes)

    return BlobPair(old=old, new=new, degraded=tuple(sorted(degraded)), binary_new=binary_new)


@dataclass(frozen=True)
class SourceState:
    """Cheap identity of the source a local review would capture right now.

    This intentionally excludes syntax, impact, provenance, hunk bodies and
    ordering. A mismatch can only offer the human a refresh; it never moves
    marks, comments or the review frontier by itself.
    """

    fingerprint: str
    paths: tuple[str, ...] = ()
    legacy_fingerprint: str = ""
    """The same source under the identity used before the base was named by its tree.

    The old form hashed the base commit id, so a reword (or a clone that lost
    the recorded base and fell back to an identical ``HEAD``) read as a new
    source. Blank for commit ranges, whose identity did not change.
    """

    def matches(self, stored: str) -> bool:
        """Whether a stored source identity names this source, in either form."""

        # lc-debt: accepts the commit-id form Snapshots recorded before the tree-id identity
        # (2026-10-13). Remove with legacy_fingerprint, legacy_source_fingerprint and the
        # capture's legacy_source_fingerprint field no earlier than 2027-01-13, the window in
        # which a Review opened before the upgrade is expected to be captured again or finished.
        return bool(stored) and stored in {self.fingerprint, self.legacy_fingerprint}


def legacy_source_fingerprint(repo_root: Path, rng: RevRange, fingerprint: str) -> str:
    """The old-form identity of the source *fingerprint* names; ``""`` once that source moved on.

    Read from the current source and kept only when its new-form identity is
    *fingerprint*, so the two forms always describe the same moment.
    """

    if not fingerprint or rng.mode == "commit_range":
        return ""
    try:
        state = source_state(repo_root, rng)
    except (OSError, RuntimeError, ValueError):
        return ""
    return state.legacy_fingerprint if state.fingerprint == fingerprint else ""


def _worktree_identity(repo_root: Path, path: str) -> str:
    """Raw content identity for one working-tree path, streaming large files."""

    target = repo_root / path
    try:
        if target.is_symlink():
            link = os.readlink(target).encode("utf-8", errors="surrogateescape")
            return "symlink:" + hashlib.sha256(link).hexdigest()
        if not target.is_file():
            return "missing"
        digest = hashlib.sha256()
        with target.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                digest.update(chunk)
        return "file:" + digest.hexdigest()
    except OSError:
        return "unreadable"


_SOURCE_STATE_ATTEMPTS = 3

# A working-tree file modified this recently may still change without moving its
# mtime/size (coarse timestamps, in-place rewrite), so it is never trusted from cache.
_SOURCE_STATE_SETTLE_NS = 2_000_000_000
_SOURCE_STATE_CACHE_MAX = 16
_source_state_cache: OrderedDict[tuple[str, str, str, str], tuple[tuple[Any, ...], SourceState]] = OrderedDict()
_source_state_cache_lock = threading.Lock()


def _workdir_signature(repo_root: Path, rng: RevRange) -> tuple[Any, ...] | None:
    """Cheap identity of the working tree, or ``None`` when it cannot be trusted.

    ``diff_to_workdir`` against the base tree re-reads the workdir (~1s on a
    modest repository, far more on a large one) and ``lc review``, the live
    worker and the Reader each ask for the source state several times. libgit2's
    ``status`` is answered from the index stat cache in milliseconds; when every
    changed path still has the same (mtime, size, inode) and none is fresh enough
    to be mid-write, the previous full answer is still exact.
    """

    try:
        repo = _open_repo(repo_root)
        entries = repo.status(untracked_files="all", ignored=False)
        now = time.time_ns()
        rows: list[tuple[Any, ...]] = []
        for path in sorted(entries):
            try:
                st = os.lstat(repo_root / path)
            except FileNotFoundError:
                rows.append((path, int(entries[path]), None))
                continue
            if stat.S_ISDIR(st.st_mode) and (repo_root / path / ".git").exists():
                # A submodule's own dirt is part of the parent's state.
                sub_root = (repo_root / path).resolve()
                nested = _workdir_signature(sub_root, resolve_rev_range(sub_root, working_tree=True))
                if nested is None:
                    return None
                rows.append((path, int(entries[path]), "submodule", nested))
                continue
            if (
                not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode))
                or now - st.st_mtime_ns < _SOURCE_STATE_SETTLE_NS
            ):
                return None  # a directory, or a file that may still be written
            rows.append((path, int(entries[path]), st.st_mtime_ns, st.st_size, st.st_ino))
        index = os.stat(repo.path + "index") if os.path.exists(repo.path + "index") else None
        head = "" if repo.head_is_unborn else str(repo.head.target)
        return (
            head,
            rng.base_sha,
            rng.merge_base_sha,
            None if index is None else (index.st_mtime_ns, index.st_size),
            tuple(rows),
        )
    except Exception:
        return None


def source_state(repo_root: Path, rng: RevRange) -> SourceState:
    """Content-sensitive identity, reusing the last exact answer while the tree is unchanged."""

    if rng.mode != "working_tree":
        return _source_state_uncached(repo_root, rng)
    signature = _workdir_signature(repo_root, rng)
    if signature is None:
        return _source_state_uncached(repo_root, rng)
    key = (str(repo_root), rng.mode, rng.base_sha, rng.merge_base_sha)
    with _source_state_cache_lock:
        hit = _source_state_cache.get(key)
        if hit is not None and hit[0] == signature:
            _source_state_cache.move_to_end(key)
            return hit[1]
    stamp = hashlib.sha256(repr((key, signature)).encode("utf-8", "surrogatepass")).hexdigest()
    state = _read_shared_source_state(repo_root, stamp)
    if state is None:
        state = _source_state_uncached(repo_root, rng)
        # Only remember it if nothing moved while it was computed.
        if _workdir_signature(repo_root, rng) != signature:
            return state
        _write_shared_source_state(repo_root, stamp, state)
    with _source_state_cache_lock:
        _source_state_cache[key] = (signature, state)
        _source_state_cache.move_to_end(key)
        while len(_source_state_cache) > _SOURCE_STATE_CACHE_MAX:
            _source_state_cache.popitem(last=False)
    return state


def _shared_source_state_path(repo_root: Path) -> Path:
    # One small file per worktree, overwritten in place: `lc review`, its
    # background finalize job, the live worker and the server are separate
    # processes that each asked for the same answer.
    # lc-debt: files for deleted worktrees are never removed; sweep by age if they pile up.
    from lemoncrow.core.foundation.paths import default_store_root

    digest = hashlib.sha256(str(repo_root).encode("utf-8", "surrogateescape")).hexdigest()[:32]
    return default_store_root() / "review" / "source-state" / f"{digest}.json"


def _read_shared_source_state(repo_root: Path, stamp: str) -> SourceState | None:
    try:
        payload = json.loads(_shared_source_state_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("stamp") != stamp:
        return None
    fingerprint, paths = payload.get("fingerprint"), payload.get("paths")
    # A file without the legacy form was written before the tree-id identity:
    # its fingerprint is the old form, so it is not this answer.
    legacy = payload.get("legacy_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or not isinstance(legacy, str)
        or not isinstance(paths, list)
        or not all(isinstance(p, str) for p in paths)
    ):
        return None
    return SourceState(fingerprint, tuple(paths), legacy)


def _write_shared_source_state(repo_root: Path, stamp: str, state: SourceState) -> None:
    target = _shared_source_state_path(repo_root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(
            json.dumps(
                {
                    "stamp": stamp,
                    "fingerprint": state.fingerprint,
                    "legacy_fingerprint": state.legacy_fingerprint,
                    "paths": list(state.paths),
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, target)
    except OSError:
        return


def _source_state_uncached(repo_root: Path, rng: RevRange) -> SourceState:
    """Content-sensitive identity for change detection, without building a packet.

    libgit2 refuses a file that changes while it hashes it -- an editor or
    agent is mid-write -- so another pass reads the settled bytes. A tree
    that keeps changing raises ``RuntimeError``, which callers already treat
    as "cannot inspect the source right now".
    """

    if rng.mode == "commit_range":
        return _source_state_once(repo_root, rng)
    try:
        git_error = require_pygit2().GitError
    except GitHistoryBootstrapError as exc:
        raise ValueError(f"pygit2 is required for `lc review`: {exc}") from exc
    for attempt in range(1, _SOURCE_STATE_ATTEMPTS + 1):
        try:
            return _source_state_once(repo_root, rng)
        except git_error as exc:
            if attempt == _SOURCE_STATE_ATTEMPTS:
                raise RuntimeError(f"the working tree kept changing while it was read: {exc}") from exc
    raise AssertionError("unreachable")


def _source_state_once(repo_root: Path, rng: RevRange) -> SourceState:
    """Content-sensitive identity for change detection, without building a packet.

    Working-tree reviews hash only paths libgit2 already reports as changed.
    Staged reviews use index object ids, so unstaged edits do not trigger them.
    Commit-range sessions store resolved SHAs and are immutable by construction.
    """

    if rng.mode == "commit_range":
        payload = f"commit_range\0{rng.base_sha}\0{rng.head_sha}\0{rng.merge_base_sha}"
        return SourceState(hashlib.sha256(payload.encode("utf-8")).hexdigest())

    repo = _open_repo(repo_root)
    diff = _build_diff(repo, rng, 0)
    intent_to_add = _intent_to_add_paths(repo) if rng.mode == "staged" else frozenset()
    if intent_to_add is None:
        intent_to_add = frozenset()

    rows: list[str] = []
    paths: list[str] = []
    for patch in diff:
        delta = patch.delta
        try:
            status_char = str(delta.status_char())
        except Exception:
            status_char = "M"
        status = _STATUS_CHAR_TO_STATUS.get(status_char, "modified")
        new_path = str(delta.new_file.path or "")
        old_path_raw = str(delta.old_file.path or "")
        path = old_path_raw if status == "deleted" else (new_path or old_path_raw)
        if not path or path.startswith(f"{DEFAULT_STORE_DIRNAME}/") or path in intent_to_add:
            continue
        if _is_gitlink(delta) and _gitlink_unmoved(delta):
            if rng.mode == "working_tree" and _submodule_dirt_is_internal(repo, path):
                sub_root = (repo_root / path).resolve()
                try:
                    nested = source_state(sub_root, resolve_rev_range(sub_root, working_tree=True))
                except (KeyError, OSError, RuntimeError, ValueError):
                    # Shallow/partial submodules may lack the object named by
                    # their local base. The parent review still needs a stable
                    # advisory fingerprint instead of crashing on that gap.
                    nested = SourceState(hashlib.sha256(f"unreadable-submodule\0{path}".encode()).hexdigest())
                rows.append("\0".join(("nested", path, "", nested.fingerprint)))
                paths.append(path)
            continue

        old_path = old_path_raw if status in ("renamed", "copied", "deleted") else ""
        if _is_gitlink(delta):
            old_oid, new_oid = _gitlink_pointer(delta)
            identity = f"gitlink:{old_oid}:{new_oid}"
        elif status == "deleted":
            identity = f"deleted:{getattr(delta.old_file, 'id', '')}"
        elif rng.mode == "staged":
            identity = f"index:{getattr(delta.new_file, 'id', '')}:{getattr(delta.new_file, 'mode', 0)}"
        else:
            identity = _worktree_identity(repo_root, path)
        rows.append("\0".join((status, path, old_path, identity)))
        paths.append(path)

    # The reviewed patch is a function of both the mutable side and its Git
    # base. A commit can move HEAD while leaving the same working-tree bytes in
    # place; hashing only those bytes would then claim the review is unchanged
    # even though every hunk is now relative to a different base tree. The base
    # is named by its tree, not its commit: a reworded base, or a clone that
    # lost the recorded base and fell back to an identical HEAD, is the same patch.
    ordered = sorted(rows)
    base_tree, merge_base_tree = _tree_id(repo, rng.base_sha), _tree_id(repo, rng.merge_base_sha)
    payload = "\n".join((f"range:{rng.mode}:{base_tree}:{merge_base_tree}", *ordered))
    legacy = "\n".join((f"range:{rng.mode}:{rng.base_sha}:{rng.merge_base_sha}", *ordered))
    return SourceState(
        hashlib.sha256(payload.encode()).hexdigest(),
        tuple(sorted(set(paths))),
        hashlib.sha256(legacy.encode()).hexdigest(),
    )


def _tree_id(repo: Any, sha: str) -> str:
    """The tree a base commit names; blank stays blank and the empty tree is itself."""

    if not sha or sha == EMPTY_TREE_SHA:
        return sha
    return str(_tree_for_sha(repo, sha).id)


__all__ = [
    "CONFLICTED_SIGNAL",
    "EMPTY_TREE_SHA",
    "HUNK_PATCH_TRUNCATED_SIGNAL",
    "ITA_EXCLUDED_SIGNAL",
    "ITA_UNDETERMINED_SIGNAL",
    "MAX_BLOB_BYTES",
    "MAX_HUNK_PATCH_BYTES",
    "STORE_DIR_EXCLUDED_SIGNAL",
    "SUBMODULE_DIRTY_SIGNAL",
    "BlobPair",
    "DiffResult",
    "RevRange",
    "SourceState",
    "_anchor_line",
    "classify_path",
    "collect_diff",
    "commit_datetime",
    "commits_in_head",
    "current_branch_ref",
    "detect_repo_root",
    "is_dirty",
    "legacy_source_fingerprint",
    "load_blobs",
    "resolve_rev_range",
    "review_branch_base",
    "review_branch_commits",
    "source_state",
    "with_review_base",
]
