"""Reading git's own index, so a file git already knows is unchanged is not re-hashed.

``git add`` and ``git checkout`` both leave behind a record of exactly what this
walk wants to know: for every tracked path, the stat data the file had when git
last verified it, and the object id of the content it had then. When the file's
current ``lstat`` still matches that record, git itself treats the file as
unmodified -- that is the check ``git status`` runs on every path, every time --
and the content is the staged blob.

Two things follow, and only two:

* the check is a *negative* one. Matching stat data means "not known to have
  changed". This module never claims a digest; it says whether git's own
  cleanliness test passes, and :mod:`lemoncrow_client.walkcache` decides what to
  do with that. Anything short of an exact match is reported as not clean, so
  every doubt costs a hash rather than a wrong answer.
* the object id is git's, not ours. It identifies *content*, so it is the right
  key for a cache that has to survive ``git checkout`` moving files between
  revisions -- the case where a per-path stat cache is guaranteed to miss and a
  content-keyed one is guaranteed to hit.

The parser is deliberately strict: index versions 2, 3 and 4, SHA-1 and SHA-256
object formats, and a hard refusal -- ``None``, meaning "walk as if there were
no index" -- on anything it does not fully understand. A half-understood index
is how a fast path starts inventing digests.

Nothing here runs a program. ``git`` is not on ``PATH`` in every environment the
client runs in, a subprocess is a capability an auditor has to reason about, and
the file format is stable and documented. It is read, never written.
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = ["GitIndex", "GitIndexEntry", "locate_index", "read_index"]

_SIGNATURE: Final[bytes] = b"DIRC"
_SUPPORTED_VERSIONS: Final[frozenset[int]] = frozenset({2, 3, 4})
#: A 1,000,000-entry SHA-1 index is about 80 MB. Past this the parse is slower
#: than the hashing it would save, so the fast path declines rather than stalls.
_MAX_INDEX_BYTES: Final[int] = 192 * 1024 * 1024
_MAX_GITDIR_BYTES: Final[int] = 4096
_MAX_CONFIG_BYTES: Final[int] = 1024 * 1024

#: ``ctime_s ctime_ns mtime_s mtime_ns dev ino mode uid gid size``
_STAT_FIELDS: Final[struct.Struct] = struct.Struct("!10I")
_STAT_BYTES: Final[int] = _STAT_FIELDS.size
_FLAGS: Final[struct.Struct] = struct.Struct("!H")

_NAME_MASK: Final[int] = 0x0FFF
_STAGE_SHIFT: Final[int] = 12
_EXTENDED: Final[int] = 0x4000
_ASSUME_VALID: Final[int] = 0x8000
_SKIP_WORKTREE: Final[int] = 0x4000
_INTENT_TO_ADD: Final[int] = 0x2000

#: The only two modes a *file* has in git. A symlink (0o120000) and a gitlink
#: (0o160000) are not files this walk hashes, so an entry carrying one is not a
#: candidate and is dropped at parse time.
_REGULAR_MODES: Final[frozenset[int]] = frozenset({0o100644, 0o100755})

_U32: Final[int] = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class GitIndexEntry:
    """One stage-0 regular-file entry, reduced to what a cleanliness test needs."""

    ctime_ns: int
    mtime_ns: int
    dev: int
    ino: int
    mode: int
    size: int
    oid: str

    def clean_against(self, info: os.stat_result, *, racy_before_ns: int) -> bool:
        """Whether git's own test says the worktree file is unmodified.

        Every field must match exactly. The index truncates ``dev``, ``ino`` and
        ``size`` to 32 bits, so the comparison is against the truncated value and
        a file past 4 GiB -- where the truncation is lossy -- is never clean.

        ``racy_before_ns`` is the index file's own mtime. An entry written in the
        same instant as the file it describes cannot be distinguished from one
        written just before a change in the same timestamp tick, which is git's
        "racily clean" case: those are reported dirty, exactly as git does.
        """
        if self.mtime_ns >= racy_before_ns:
            return False
        size = info.st_size
        if size > _U32 or size != self.size:
            return False
        if info.st_mtime_ns != self.mtime_ns or info.st_ctime_ns != self.ctime_ns:
            return False
        # ``0`` is what git records where it has no stat field to record -- some
        # filesystems and some ``core.checkStat`` settings. It proves nothing, so
        # it is not accepted as agreement.
        if self.ino == 0 or (info.st_ino & _U32) != self.ino:
            return False
        if self.dev == 0 or (info.st_dev & _U32) != self.dev:
            return False
        return _git_mode(info.st_mode) == self.mode


@dataclass(frozen=True, slots=True)
class GitIndex:
    """A parsed index: path -> entry, plus the index's own mtime for the racy test."""

    entries: dict[str, GitIndexEntry]
    index_mtime_ns: int
    version: int

    def clean_oid(self, relpath: str, info: os.stat_result) -> str | None:
        """The staged object id when git's test says the file is unmodified."""
        entry = self.entries.get(relpath)
        if entry is None:
            return None
        if not entry.clean_against(info, racy_before_ns=self.index_mtime_ns):
            return None
        return entry.oid


def _git_mode(raw: int) -> int:
    """git's two file modes. Mirrors ``manifest._mode_of``, in git's encoding."""
    return 0o100755 if raw & 0o100 else 0o100644


def _resolve_git_dir(repo_root: Path) -> tuple[Path, Path] | None:
    """``(gitdir, commondir)`` for a checkout, a linked worktree or a submodule."""
    marker = repo_root / ".git"
    try:
        info = marker.lstat()
    except OSError:
        return None
    if (info.st_mode & 0o170000) == 0o040000:
        return marker, marker
    if (info.st_mode & 0o170000) != 0o100000 or info.st_size > _MAX_GITDIR_BYTES:
        return None
    try:
        text = marker.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return None
    target = text[len(prefix) :].strip()
    if not target:
        return None
    gitdir = Path(target)
    if not gitdir.is_absolute():
        gitdir = repo_root / gitdir
    common = gitdir / "commondir"
    try:
        raw = common.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeDecodeError):
        return gitdir, gitdir
    if not raw:
        return gitdir, gitdir
    resolved = Path(raw)
    return gitdir, (resolved if resolved.is_absolute() else gitdir / resolved)


def _object_id_bytes(commondir: Path) -> int:
    """20 for SHA-1, 32 for a SHA-256 repository. Declared in the shared config."""
    config = commondir / "config"
    try:
        if config.stat().st_size > _MAX_CONFIG_BYTES:
            return 20
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 20
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            continue
        if section != "extensions" or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip().lower() == "objectformat" and value.strip().lower() == "sha256":
            return 32
    return 20


def _decode_varint(data: bytes, offset: int) -> tuple[int, int] | None:
    """git's offset-encoded varint, as used by index version 4 path compression."""
    limit = len(data)
    if offset >= limit:
        return None
    byte = data[offset]
    value = byte & 0x7F
    offset += 1
    while byte & 0x80:
        if offset >= limit:
            return None
        byte = data[offset]
        offset += 1
        value = ((value + 1) << 7) | (byte & 0x7F)
        if value > _U32:
            return None
    return value, offset


def locate_index(repo_root: Path) -> Path | None:
    """Where this worktree's index file is, without reading it.

    Separate from :func:`read_index` because the decision to parse is a cost
    decision -- parsing is linear in the whole repository, while the saving is
    linear in what changed -- and the file's size is the only input that
    decision needs.
    """
    resolved = _resolve_git_dir(repo_root)
    if resolved is None:
        return None
    return resolved[0] / "index"


def read_index(repo_root: Path) -> GitIndex | None:
    """Parse ``.git/index``. ``None`` means "walk as if there were no index"."""
    resolved = _resolve_git_dir(repo_root)
    if resolved is None:
        return None
    gitdir, commondir = resolved
    index_path = gitdir / "index"
    try:
        info = index_path.stat()
        if info.st_size > _MAX_INDEX_BYTES:
            return None
        data = index_path.read_bytes()
    except OSError:
        return None
    declared = _object_id_bytes(commondir)
    parsed = parse_index(data, index_mtime_ns=info.st_mtime_ns, oid_bytes=declared)
    if parsed is not None:
        return parsed
    # The repository's configuration is a hint, not a proof: a worktree can be
    # read with a config this process cannot see. The trailing checksum settles
    # it, so the other length is worth one try before giving up.
    other = 32 if declared == 20 else 20
    return parse_index(data, index_mtime_ns=info.st_mtime_ns, oid_bytes=other)


def _trailer_is_intact(data: bytes, oid_bytes: int) -> bool:
    """git ends the index with a hash over everything before it. Check it.

    This is the difference between "the bytes parsed" and "the bytes are the
    ones git wrote". A truncated index can parse cleanly for a while and then
    stop on a plausible-looking boundary, and every entry it yielded would be
    used to *skip hashing a file*. Verifying the trailer costs one pass over a
    few megabytes and turns every partial file into a refusal.
    """
    algorithm = "sha256" if oid_bytes == 32 else "sha1"
    try:
        hasher = hashlib.new(algorithm, usedforsecurity=False)
    except ValueError:  # pragma: no cover - a build without the algorithm
        return False
    hasher.update(data[: len(data) - oid_bytes])
    return hasher.digest() == data[len(data) - oid_bytes :]


def parse_index(data: bytes, *, index_mtime_ns: int, oid_bytes: int = 20) -> GitIndex | None:
    """Parse index bytes. Separated from the read so a test can feed it a forgery."""
    if len(data) < 12 + oid_bytes or data[:4] != _SIGNATURE:
        return None
    version, count = struct.unpack_from("!II", data, 4)
    if version not in _SUPPORTED_VERSIONS or count > 8_000_000:
        return None
    if not _trailer_is_intact(data, oid_bytes):
        return None
    limit = len(data) - oid_bytes  # the trailing checksum is not an entry
    offset = 12
    entries: dict[str, GitIndexEntry] = {}
    previous = b""
    for _ in range(count):
        start = offset
        if offset + _STAT_BYTES + oid_bytes + 2 > limit:
            return None
        (
            ctime_s,
            ctime_ns,
            mtime_s,
            mtime_ns,
            dev,
            ino,
            mode,
            _uid,
            _gid,
            size,
        ) = _STAT_FIELDS.unpack_from(data, offset)
        offset += _STAT_BYTES
        oid = data[offset : offset + oid_bytes]
        offset += oid_bytes
        (flags,) = _FLAGS.unpack_from(data, offset)
        offset += 2
        extended = bool(flags & _EXTENDED)
        skip_worktree = False
        intent_to_add = False
        if extended:
            if version < 3 or offset + 2 > limit:
                return None
            (extra,) = _FLAGS.unpack_from(data, offset)
            offset += 2
            skip_worktree = bool(extra & _SKIP_WORKTREE)
            intent_to_add = bool(extra & _INTENT_TO_ADD)
        name_len = flags & _NAME_MASK

        if version >= 4:
            decoded = _decode_varint(data, offset)
            if decoded is None:
                return None
            strip, offset = decoded
            if strip > len(previous):
                return None
            end = data.find(b"\x00", offset)
            if end < 0 or end > limit:
                return None
            raw_path = previous[: len(previous) - strip] + data[offset:end]
            offset = end + 1
        else:
            if name_len < _NAME_MASK:
                end = offset + name_len
                if end > limit or data[end : end + 1] != b"\x00":
                    return None
            else:
                end = data.find(b"\x00", offset)
                if end < 0 or end > limit:
                    return None
            raw_path = data[offset:end]
            # v2/v3 pad the entry with NULs to a multiple of eight bytes.
            offset = start + ((end - start + 8) & ~7)
            if offset > limit:
                return None
        previous = raw_path

        stage = (flags >> _STAGE_SHIFT) & 0b11
        if stage or skip_worktree or intent_to_add or flags & _ASSUME_VALID:
            # A merge conflict, a path git was told not to look at, and a path
            # with no content yet. None of them describe a clean worktree file.
            continue
        if mode not in _REGULAR_MODES or not raw_path:
            continue
        try:
            relpath = raw_path.decode("utf-8", "surrogateescape")
        except UnicodeDecodeError:  # pragma: no cover - surrogateescape does not raise
            continue
        entries[relpath] = GitIndexEntry(
            ctime_ns=ctime_s * 1_000_000_000 + ctime_ns,
            mtime_ns=mtime_s * 1_000_000_000 + mtime_ns,
            dev=dev,
            ino=ino,
            mode=mode,
            size=size,
            oid=oid.hex(),
        )
    if offset > limit:
        return None
    return GitIndex(entries=entries, index_mtime_ns=index_mtime_ns, version=version)
