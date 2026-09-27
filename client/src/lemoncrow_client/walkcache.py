"""The hashing fast path: what lets a warm ``SessionStart`` skip re-reading the worktree.

Without it, every session hashes every file. At 2,000 files that is invisible;
at 50,000 files and 300 MB it is most of the budget, and it is pure waste,
because between two sessions a developer changes a handful of files.

The rule this module exists to enforce is that **a skipped hash must be
indistinguishable from a performed one**. A wrong ``content_digest`` is not a
slow session, it is a view that serves the wrong bytes, so every shortcut here
is a proof obligation and every doubt costs a hash:

**Per-path stat identity.** A digest is reused only when *size, mtime, ctime,
inode, device and mode* all still match what they were when the digest was
computed. ``ctime`` is the load-bearing field: it is the one a process cannot
set, so the classic forgery -- rewrite the content, restore the mtime -- moves
it and the entry misses. ``inode`` catches a replace-by-rename, ``device``
catches a remounted or bind-mounted tree.

**A race guard.** An entry is persisted only if the file was already quiescent:
its mtime *and* ctime must be strictly older than the moment the walk started.
A file written *while* it is being hashed would otherwise be recorded with a
digest of half-old content against stat data that looks settled forever after.
The stat compared is the one taken *after* the read, so a write that lands
during the read moves it into the guarded window and the entry is dropped.

**git's index, for the case a path-keyed cache cannot serve.** ``git checkout``
rewrites files: new mtime, new ctime, often a new inode, so every per-path entry
for a switched branch misses. But git wrote down the stat data and the object id
of what it just put there, so for any file git's own cleanliness test passes on,
the content *is* that blob -- and a blob id is a content key, which survives the
file moving between revisions. Digests learned for a blob id are therefore
reused across checkouts, where the per-path cache is guaranteed to miss. The
index is read lazily: a session where nothing changed never opens it.

  The trust boundary is explicit. Reusing a digest under a blob id assumes the
  object id identifies the content -- git's own assumption, the one ``git
  checkout`` relies on to put the right bytes in a worktree. The fast path is
  never *more* trusting than the repository it is reading: a blob id is
  consulted only for a file git itself reports as unmodified at that id.

**Where it lives.** ``~/.lemoncrow`` (``LEMONCROW_HOME``) -- the one directory
outside the repository this client writes, and the one the packaging audit
already permits. Not in the worktree, which would be a file the developer has to
``.gitignore``; not in ``.git``, which is git's. One file per repository, named
by a digest of the worktree path, mode 0600. It is a cache in the strict sense:
deleting it costs one slow session and nothing else, and a corrupt, truncated or
foreign file is discarded rather than repaired.

This module never writes. It produces the bytes and the plan;
:mod:`lemoncrow_client.session` performs the write, so the set of modules that
touch persistent state stays the set the audit already names.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .gitindex import GitIndex, locate_index, read_index

__all__ = [
    "CACHE_DIRECTORY",
    "MINIMUM_INDEX_MISSES",
    "CacheWrite",
    "WalkCache",
    "WalkCacheProof",
    "WalkCacheStats",
    "cache_path",
    "load_walk_cache",
    "peek_walk_cache_proof",
]

#: Part of the file, so a format change is a miss rather than a misreading.
_FORMAT: Final[str] = "lemoncrow-walkcache/4"
_FIELD: Final[str] = "\x00"

#: Under the state directory, so one repository's cache is one file.
CACHE_DIRECTORY: Final[str] = "walk-cache"

#: Keep persistence bounded by both serialized bytes and row count. The old
#: 200k-row ceiling rejected the measured 250k enterprise tier even though its
#: complete path proof is only ~42 MiB, comfortably inside the 64 MiB byte cap.
#: 400k covers that tier while the byte cap remains the tighter bound for long
#: paths / larger repositories.
_MAX_CACHE_BYTES: Final[int] = 64 * 1024 * 1024
_MAX_ENTRIES: Final[int] = 400_000
#: The blob map is keyed by content, so unlike the path map it has no natural
#: end: every branch ever checked out adds to it. This is where it stops.
_MAX_BLOBS: Final[int] = 200_000
#: Caches for this many worktrees are kept; the least recently used are dropped.
_MAX_REPOSITORIES: Final[int] = 12
#: v4 stores only bounded manifest IDs in the first line. Refuse an absurd
#: header without reading the 64 MiB body merely to discover corruption.
_MAX_PROOF_HEADER_BYTES: Final[int] = 1024 * 1024

#: Parsing git's index is linear in the *whole* repository; the hashing it saves
#: is linear in what *changed*. Measured: one index entry costs about 1.8 us to
#: parse and occupies about 120 bytes, and one avoided hash of an average source
#: file saves about 8 us. Parsing therefore pays for itself once something like
#: a quarter of the index would otherwise be re-hashed -- a branch switch, not an
#: edit. The entry count is estimated from the index file's size, which is one
#: stat, rather than by parsing the thing the estimate exists to avoid.
_INDEX_ENTRY_BYTES: Final[int] = 120
_INDEX_BREAK_EVEN: Final[int] = 4
#: Below this, a small repository's index is cheap enough to read regardless.
MINIMUM_INDEX_MISSES: Final[int] = 128


def _identity(info: os.stat_result) -> str:
    """The stat fields that must all still agree for a digest to be reusable."""
    return f"{info.st_size} {info.st_mtime_ns} {info.st_ctime_ns} {info.st_ino} {info.st_dev} {info.st_mode}"


def cache_path(state_dir: Path, repo_root: Path) -> Path:
    """One file per worktree, named by a digest of its absolute path."""
    try:
        resolved = str(repo_root.resolve())
    except OSError:  # pragma: no cover - resolve() on a vanished directory
        resolved = str(repo_root)
    name = hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return state_dir / CACHE_DIRECTORY / f"{name}.walkcache"


@dataclass(frozen=True, slots=True)
class CacheWrite:
    """What :mod:`lemoncrow_client.session` should do on disk, if anything."""

    path: Path
    data: bytes
    prune: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class WalkCacheProof:
    """Metadata sufficient to open an unchanged clean generation lazily."""

    entry_count: int = 0
    workspace_fingerprint: str = ""
    generation_revision: str = ""
    manifest_root: str = ""
    manifest_chunk_rows: int = 0
    manifest_chunk_ids: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return bool(
            self.workspace_fingerprint
            and self.generation_revision
            and self.manifest_root
            and self.manifest_chunk_rows > 0
        )


@dataclass(frozen=True, slots=True)
class WalkCacheStats:
    """Where one walk's digests came from. Reported, so the claim is checkable."""

    reused_by_path: int = 0
    reused_by_blob: int = 0
    hashed: int = 0

    @property
    def total(self) -> int:
        return self.reused_by_path + self.reused_by_blob + self.hashed

    @property
    def reused(self) -> int:
        return self.reused_by_path + self.reused_by_blob


@dataclass
class WalkCache:
    """Digests this client has already computed, and what it may do with them.

    Two maps, because the two cases they serve are different:

    ``by_path``
        ``relpath -> (stat identity, sha256)``. Serves the common session, where
        nothing changed.
    ``by_blob``
        ``git object id -> sha256``. Serves the session after a checkout, where
        every file moved but the content came back.
    """

    repo_root: Path
    by_path: dict[str, tuple[str, str]] = field(default_factory=dict)
    by_blob: dict[str, str] = field(default_factory=dict)
    #: The instant the walk began. Nothing whose mtime or ctime reaches into
    #: this moment is persisted.
    started_ns: int = 0
    reused_by_path: int = 0
    reused_by_blob: int = 0
    hashed: int = 0
    #: Per-path lookups that failed this walk. Also the input to the decision
    #: about whether git's index is worth parsing.
    misses: int = 0
    fresh_path: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)
    fresh_blob: dict[str, str] = field(default_factory=dict, repr=False)
    index: GitIndex | None = field(default=None, repr=False)
    index_read: bool = field(default=False, repr=False)
    index_budget: int = field(default=-1, repr=False)
    #: Exact source-state fingerprint that produced ``by_path``. Empty means
    #: no complete-workspace proof is available, so callers perform an ordinary
    #: walk rather than trusting incomplete cache state.
    workspace_fingerprint: str = ""
    #: Immutable Git revision proved to have exactly the same path set/content
    #: as this complete workspace. Empty for dirty/sparse worktrees and older
    #: cache formats. Reusing it is safe only together with an exact fingerprint
    #: match, which is what whole-walk restoration already requires.
    generation_revision: str = ""
    #: Canonical manifest metadata proved by the same complete workspace. The
    #: chunk IDs are valid only for ``manifest_chunk_rows``; a server that
    #: negotiates a different chunk ceiling forces ordinary recomputation.
    manifest_root: str = ""
    manifest_chunk_rows: int = 0
    manifest_chunk_ids: tuple[str, ...] = ()

    # -- the walk's interface ------------------------------------------- #

    def begin(self) -> None:
        """Mark the start of a walk. Sets the race-guard horizon."""
        self.started_ns = time.time_ns()
        self.reused_by_path = 0
        self.reused_by_blob = 0
        self.hashed = 0
        self.misses = 0
        self.fresh_path = {}
        self.fresh_blob = {}

    @property
    def stats(self) -> WalkCacheStats:
        return WalkCacheStats(
            reused_by_path=self.reused_by_path,
            reused_by_blob=self.reused_by_blob,
            hashed=self.hashed,
        )

    def lookup(self, relpath: str, info: os.stat_result) -> str | None:
        """A digest for ``relpath``, or ``None`` when it has to be computed."""
        known = self.by_path.get(relpath)
        if known is None:
            return None
        identity, digest = known
        if identity != _identity(info):
            return None
        self.reused_by_path += 1
        self.fresh_path[relpath] = known
        return digest

    def lookup_blob(self, relpath: str, info: os.stat_result) -> str | None:
        """A digest via git's index, for a file a per-path lookup just missed.

        Reads the index at most once per walk, and only when enough has missed
        for the parse to pay for itself -- a session where nothing changed never
        opens it, and a session where three files changed hashes those three
        rather than reading the whole index to avoid it.
        """
        self.misses += 1
        if not self.by_blob:
            return None
        index = self._git_index()
        if index is None:
            return None
        oid = index.clean_oid(relpath, info)
        if oid is None:
            return None
        digest = self.by_blob.get(oid)
        if digest is None:
            return None
        self.reused_by_blob += 1
        self._remember(relpath, info, digest, oid=oid)
        return digest

    def record(self, relpath: str, info: os.stat_result, digest: str) -> None:
        """Remember a digest that *was* computed, if it is safe to remember it.

        ``info`` must be the stat taken **after** the bytes were read: a file
        rewritten during its own hash then carries a timestamp inside the guarded
        window, and is not remembered at all.

        This is also where ``object id -> digest`` is learned, because a file
        that was hashed is a file whose index entry is worth consulting: it is
        the only moment at which both halves of that pair are in hand.
        """
        self.hashed += 1
        if info.st_mtime_ns >= self.started_ns or info.st_ctime_ns >= self.started_ns:
            return
        index = self._git_index()
        oid = index.clean_oid(relpath, info) if index is not None else None
        self._remember(relpath, info, digest, oid=oid)

    def _remember(self, relpath: str, info: os.stat_result, digest: str, *, oid: str | None) -> None:
        if info.st_mtime_ns >= self.started_ns or info.st_ctime_ns >= self.started_ns:
            # The race guard, applied at the one place every path into the cache
            # passes through, so no caller has to be trusted to apply it.
            return
        if "\n" not in relpath and _FIELD not in relpath:
            # The cache is line-oriented. A path that cannot be written down
            # unambiguously is simply not cached; it costs one hash per session.
            self.fresh_path[relpath] = (_identity(info), digest)
        if oid is not None:
            self.fresh_blob[oid] = digest

    def _git_index(self) -> GitIndex | None:
        """The parsed index, once this walk has missed enough to justify it."""
        if self.index_read:
            return self.index
        if self.misses < self._budget():
            return None
        self.index_read = True
        self.index = read_index(self.repo_root)
        return self.index

    def reuse_proven_paths(self, workspace_fingerprint: str) -> dict[str, tuple[str, str]] | None:
        """Reuse the complete path map when an exact workspace proof matches.

        This is stronger than the ordinary per-path stat cache: the caller has
        already asked Git for the exact HEAD + dirty/untracked state and it is
        byte-for-byte the state saved after the previous successful walk. Any
        missing proof or old cache format declines to the normal filesystem
        walk. Mark the paths fresh so persisting this session keeps the cache
        complete rather than replacing it with an empty fast-path observation.
        """
        if not workspace_fingerprint or workspace_fingerprint != self.workspace_fingerprint or not self.by_path:
            return None
        self.begin()
        self.reused_by_path = len(self.by_path)
        self.fresh_path = dict(self.by_path)
        return self.fresh_path

    def _budget(self) -> int:
        """How many misses make parsing the index cheaper than hashing."""
        if self.index_budget < 0:
            located = locate_index(self.repo_root)
            size = 0
            if located is not None:
                try:
                    size = located.stat().st_size
                except OSError:
                    size = 0
            estimated = size // _INDEX_ENTRY_BYTES
            self.index_budget = max(MINIMUM_INDEX_MISSES, estimated // _INDEX_BREAK_EVEN)
        return self.index_budget

    # -- persistence ---------------------------------------------------- #

    def plan_write(
        self,
        state_dir: Path,
        *,
        workspace_fingerprint: str = "",
        generation_revision: str = "",
        manifest_root: str = "",
        manifest_chunk_rows: int = 0,
        manifest_chunk_ids: Sequence[str] = (),
    ) -> CacheWrite | None:
        """The bytes to persist and the stale caches to drop, or ``None``.

        Only what this walk actually saw is written: an entry for a file that no
        longer exists is dead weight, and a cache that only grows is a cache that
        eventually costs more to read than it saves. The blob map is the one part
        that legitimately outlives the walk -- it is keyed by content, not by a
        path that may be gone -- so it is carried forward. It is also the part
        with no natural end, one entry per blob ever seen on any branch, so it is
        capped: what this walk learned is kept first and the older half of the map
        is what falls off.
        """
        if not self.started_ns or len(self.fresh_path) > _MAX_ENTRIES:
            return None
        proof = workspace_fingerprint if workspace_fingerprint and " " not in workspace_fingerprint else "-"
        revision = generation_revision.strip().lower()
        if len(revision) not in {40, 64} or any(char not in "0123456789abcdef" for char in revision):
            revision = "-"
        root = manifest_root.strip().lower()
        chunks = tuple(str(chunk).strip().lower() for chunk in manifest_chunk_ids)
        valid_manifest = (
            len(root) == 64
            and all(char in "0123456789abcdef" for char in root)
            and manifest_chunk_rows > 0
            and all(len(chunk) == 64 and all(char in "0123456789abcdef" for char in chunk) for chunk in chunks)
        )
        if not valid_manifest:
            root, manifest_chunk_rows, chunks = "-", 0, ()
        chunk_token = ",".join(chunks) if chunks else "-"
        head = [
            f"{_FORMAT} {self.started_ns} {len(self.fresh_path)} {proof} {revision} "
            f"{root} {manifest_chunk_rows} {chunk_token}"
        ]
        head.extend(
            f"{relpath}{_FIELD}{identity}{_FIELD}{digest}"
            for relpath, (identity, digest) in sorted(self.fresh_path.items())
        )
        head.append(_FORMAT)
        paths_only = "\n".join(head).encode("utf-8", "surrogatepass")
        if len(paths_only) > _MAX_CACHE_BYTES:
            return None
        data = paths_only
        blobs = self._bounded_blobs()
        if blobs:
            tail = "\n".join(f"{oid}{_FIELD}{digest}" for oid, digest in blobs.items())
            candidate = paths_only + b"\n" + tail.encode("utf-8", "surrogatepass")
            if len(candidate) <= _MAX_CACHE_BYTES:
                data = candidate
        target = cache_path(state_dir, self.repo_root)
        return CacheWrite(path=target, data=data, prune=_stale_caches(target))

    def _bounded_blobs(self) -> dict[str, str]:
        """This walk's blob knowledge first, then as much of the old map as fits."""
        blobs = dict(self.fresh_blob)
        if len(blobs) > _MAX_BLOBS:
            return dict(list(blobs.items())[:_MAX_BLOBS])
        for oid, digest in self.by_blob.items():
            if len(blobs) >= _MAX_BLOBS:
                break
            blobs.setdefault(oid, digest)
        return blobs


def _stale_caches(keep: Path) -> tuple[Path, ...]:
    """Caches for other worktrees past the retention count, least recent first."""
    try:
        scan = list(os.scandir(keep.parent))
    except OSError:
        return ()
    dated: list[tuple[float, Path]] = []
    for entry in scan:
        if not entry.name.endswith(".walkcache") or entry.path == str(keep):
            continue
        try:
            dated.append((entry.stat().st_mtime, Path(entry.path)))
        except OSError:
            continue
    if len(dated) < _MAX_REPOSITORIES:
        return ()
    dated.sort()
    return tuple(path for _mtime, path in dated[: len(dated) - _MAX_REPOSITORIES + 1])


def peek_walk_cache_proof(state_dir: Path, repo_root: Path) -> WalkCacheProof:
    """Read only a v4 cache header; older/corrupt caches have no fast proof."""
    target = cache_path(state_dir, repo_root)
    try:
        if target.stat().st_size > _MAX_CACHE_BYTES:
            return WalkCacheProof()
        with target.open("rb") as handle:
            raw = handle.readline(_MAX_PROOF_HEADER_BYTES + 1)
    except OSError:
        return WalkCacheProof()
    if not raw.endswith(b"\n") or len(raw) > _MAX_PROOF_HEADER_BYTES:
        return WalkCacheProof()
    try:
        header = raw[:-1].decode("utf-8", "strict").split(" ")
    except UnicodeDecodeError:
        return WalkCacheProof()
    if len(header) != 8 or header[0] != _FORMAT:
        return WalkCacheProof()
    try:
        entry_count = int(header[2])
        chunk_rows = int(header[6])
    except ValueError:
        return WalkCacheProof()
    if entry_count < 0 or entry_count > _MAX_ENTRIES or chunk_rows <= 0:
        return WalkCacheProof()
    fingerprint = header[3]
    revision = header[4].lower()
    root = header[5].lower()
    chunks = () if header[7] == "-" else tuple(part.lower() for part in header[7].split(",") if part)

    def valid_hex(value: str, lengths: set[int]) -> bool:
        return len(value) in lengths and all(char in "0123456789abcdef" for char in value)

    expected_chunks = (entry_count + chunk_rows - 1) // chunk_rows if entry_count else 0
    if (
        not valid_hex(fingerprint.lower(), {64})
        or not valid_hex(revision, {40, 64})
        or not valid_hex(root, {64})
        or len(chunks) != expected_chunks
        or any(not valid_hex(chunk, {64}) for chunk in chunks)
    ):
        return WalkCacheProof()
    return WalkCacheProof(
        entry_count=entry_count,
        workspace_fingerprint=fingerprint,
        generation_revision=revision,
        manifest_root=root,
        manifest_chunk_rows=chunk_rows,
        manifest_chunk_ids=chunks,
    )


def load_walk_cache(state_dir: Path, repo_root: Path) -> WalkCache:
    """Read the cache for ``repo_root``. Anything unreadable is an empty cache."""
    cache = WalkCache(repo_root=repo_root)
    target = cache_path(state_dir, repo_root)
    try:
        if target.stat().st_size > _MAX_CACHE_BYTES:
            return cache
        raw = target.read_bytes()
    except OSError:
        return cache
    try:
        text = raw.decode("utf-8", "surrogatepass")
    except UnicodeDecodeError:
        return cache
    parsed = _parse(text)
    if parsed is not None:
        (
            cache.by_path,
            cache.by_blob,
            cache.workspace_fingerprint,
            cache.generation_revision,
            cache.manifest_root,
            cache.manifest_chunk_rows,
            cache.manifest_chunk_ids,
        ) = parsed
    return cache


def _parse(
    text: str,
) -> tuple[dict[str, tuple[str, str]], dict[str, str], str, str, str, int, tuple[str, ...]] | None:
    """The cache file, or ``None`` for anything not exactly well-formed."""
    rows = iter(text.split("\n"))
    header = next(rows, "").split(" ")
    if not header:
        return None
    if header[0] != _FORMAT or len(header) != 8:
        return None
    marker = _FORMAT
    workspace_fingerprint = "" if header[3] == "-" else header[3]
    generation_revision = ""
    candidate = header[4].lower()
    if candidate != "-" and len(candidate) in {40, 64} and all(char in "0123456789abcdef" for char in candidate):
        generation_revision = candidate
    manifest_root = ""
    manifest_chunk_rows = 0
    manifest_chunk_ids: tuple[str, ...] = ()
    root = header[5].lower()
    try:
        rows_per_chunk = int(header[6])
    except ValueError:
        return None
    chunk_ids = () if header[7] == "-" else tuple(part.lower() for part in header[7].split(",") if part)
    valid_manifest = (
        len(root) == 64
        and all(char in "0123456789abcdef" for char in root)
        and rows_per_chunk > 0
        and all(len(chunk) == 64 and all(char in "0123456789abcdef" for char in chunk) for chunk in chunk_ids)
    )
    if valid_manifest:
        manifest_root = root
        manifest_chunk_rows = rows_per_chunk
        manifest_chunk_ids = chunk_ids
    try:
        declared = int(header[2])
    except ValueError:
        return None
    if declared > _MAX_ENTRIES:
        return None
    by_path: dict[str, tuple[str, str]] = {}
    terminated = False
    for row in rows:
        if row == marker:
            terminated = True
            break
        fields = row.split(_FIELD)
        if len(fields) != 3:
            return None
        by_path[fields[0]] = (fields[1], fields[2])
    if not terminated or len(by_path) != declared:
        # The end of the path section is marked, so a file truncated mid-write
        # is rejected rather than read as a shorter, complete one.
        return None
    by_blob: dict[str, str] = {}
    for row in rows:
        if not row:
            continue
        oid, separator, digest = row.partition(_FIELD)
        if not separator:
            return None
        by_blob[oid] = digest
    return (
        by_path,
        by_blob,
        workspace_fingerprint,
        generation_revision,
        manifest_root,
        manifest_chunk_rows,
        manifest_chunk_ids,
    )
