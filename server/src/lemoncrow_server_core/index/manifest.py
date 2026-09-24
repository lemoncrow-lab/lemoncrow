"""Layer 2, on the wire: the canonical manifest, its root and its chunks.

Three jobs, and they are all about making a *warm open* sound.

**Canonical.** :func:`manifest_root` is a digest over normalized, sorted rows
with a version prefix. Two clients that see the same worktree produce the same
root; a client that reorders its walk does not invent a second root for the
same file set.

**Verified.** The server stores manifest rows once per root and shares them
across every view that names that root. That sharing is only safe if the rows
really hash to the root, so the row store verifies the assembled manifest when
the last announced chunk lands and refuses the completion otherwise. Without
that check a client could claim a root it does not match and silently inherit
another manifest's file set.

That is also why a fill in progress is *not* shared. The rows a chunk delivers
stay on the view that delivered them, and the chunks a view owes are the chunks
it announced, until its own assembly proves the root; only then do the rows
become the root's. When an in-flight root carried rows and a pending set shared
by everyone naming it, a colleague who reached a victim's root -- computed from
the same repository, or echoed back in a ``views/open`` response -- could
announce a chunk against it and add a row, and the victim's manifest could
thereafter never hash to the root she had announced. A root is immutable
because nothing unproved reaches it, not because nobody tried.

**Chunkable.** :func:`chunk_manifest` splits the canonical rows into bounded
chunks whose ids are themselves content digests, so an interrupted upload
resumes by re-announcing the chunks that are still pending and a repeated chunk
is a no-op rather than a duplicate.

The walk half of the module -- :class:`GitIgnore` and :func:`walk_worktree` --
is the canonical definition of *what belongs in a manifest*: ``.gitignore`` is
respected, ``.git`` is never walked, symlinks are recorded but never followed,
and a file over ``size_cap`` is **manifested but not uploaded**. The server
needs this itself for the negotiated ``local_fs`` capability, where it reads
the same worktree from disk; the thin client mirrors it.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .analysis import profile_for_path
from .contracts import ManifestEntry, sha256_hex

__all__ = [
    "EMPTY_MANIFEST_ROOT",
    "MANIFEST_FORMAT",
    "GitIgnore",
    "ManifestChunk",
    "WalkResult",
    "WalkedFile",
    "canonical_rows",
    "chunk_manifest",
    "manifest_root",
    "required_digests",
    "walk_worktree",
]

#: Bumped whenever the canonical row encoding changes. Part of the digest, so
#: an old client and a new server can never agree on a root by accident.
MANIFEST_FORMAT: Final[str] = "lemoncrow-manifest/1"

_ROW_SEPARATOR: Final[str] = "\x00"
_DEFAULT_CHUNK_ROWS: Final[int] = 2_000

#: Never walked. These are LemonCrow/Git runtime state, not repository source.
#: In particular, ``.lc-worktrees`` may contain many complete nested checkouts;
#: indexing them would duplicate the corpus and poison ranking for the parent
#: workspace even when the repository did not add an explicit ignore rule.
_NEVER_WALK: Final[frozenset[str]] = frozenset({".git", ".lc-worktrees", ".lemoncrow"})


def canonical_rows(entries: Iterable[ManifestEntry]) -> tuple[str, ...]:
    """Normalized, sorted, deduplicated rows. One row per path."""
    by_path: dict[str, ManifestEntry] = {}
    for entry in entries:
        by_path[entry.path] = entry
    return tuple(
        _ROW_SEPARATOR.join(
            (
                entry.path,
                entry.content_digest,
                entry.parser_profile,
                format(entry.mode, "o"),
                str(entry.size),
            )
        )
        for _path, entry in sorted(by_path.items())
    )


def _digest_rows(rows: Sequence[str]) -> str:
    hasher = hashlib.sha256()
    hasher.update(MANIFEST_FORMAT.encode("utf-8"))
    hasher.update(b"\n")
    for row in rows:
        hasher.update(row.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def manifest_root(entries: Iterable[ManifestEntry]) -> str:
    """The canonical root digest over a complete manifest."""
    return _digest_rows(canonical_rows(entries))


#: The root of a manifest with no rows. A view that legitimately contains no
#: files still has a verifiable root rather than a special case.
EMPTY_MANIFEST_ROOT: Final[str] = _digest_rows(())


@dataclass(frozen=True, slots=True)
class ManifestChunk:
    """One bounded piece of a canonical manifest.

    ``chunk_id`` is the digest of the chunk's own canonical rows, so a chunk is
    self-identifying: re-announcing it after an interrupted upload names the
    same id, and a chunk whose rows were altered in transit names a different
    one.
    """

    chunk_id: str
    entries: tuple[ManifestEntry, ...]


def chunk_manifest(
    entries: Iterable[ManifestEntry], *, max_rows: int = _DEFAULT_CHUNK_ROWS
) -> tuple[ManifestChunk, ...]:
    """Split a manifest into content-identified chunks, in canonical order."""
    if max_rows < 1:
        raise ValueError("max_rows must be >= 1")
    by_path = {entry.path: entry for entry in entries}
    ordered = [by_path[path] for path in sorted(by_path)]
    if not ordered:
        return (ManifestChunk(chunk_id=_digest_rows(()), entries=()),)
    chunks: list[ManifestChunk] = []
    for start in range(0, len(ordered), max_rows):
        piece = tuple(ordered[start : start + max_rows])
        chunks.append(ManifestChunk(chunk_id=_digest_rows(canonical_rows(piece)), entries=piece))
    return tuple(chunks)


def required_digests(entries: Iterable[ManifestEntry], *, size_cap: int) -> tuple[str, ...]:
    """Digests whose content the server actually wants, first-seen order.

    A file over ``size_cap`` is manifested but not uploaded: it stays
    searchable by path and metadata and never becomes a demand for bytes. That
    rule lives on the server, not in a client-supplied flag, because a client
    that could mark its own uploads exempt could also hide a file from the
    index.
    """
    seen: set[str] = set()
    out: list[str] = []
    for entry in entries:
        if entry.size > size_cap or entry.content_digest in seen:
            continue
        seen.add(entry.content_digest)
        out.append(entry.content_digest)
    return tuple(out)


# --------------------------------------------------------------------------- #
# .gitignore                                                                  #
# --------------------------------------------------------------------------- #


def _translate(pattern: str) -> str:
    """Translate one gitignore glob to a regex body.

    ``*`` does not cross ``/``; ``**`` does; ``?`` is one non-separator
    character; ``[...]`` is a character class with ``!`` meaning negation.
    """
    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**", index):
                index += 2
                if pattern.startswith("/", index):
                    index += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        if char == "[":
            close = pattern.find("]", index + 1)
            if close < 0:
                out.append(re.escape(char))
                index += 1
                continue
            body = pattern[index + 1 : close]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            index = close + 1
            continue
        out.append(re.escape(char))
        index += 1
    return "".join(out)


@dataclass(frozen=True, slots=True)
class _Rule:
    regex: re.Pattern[str]
    negated: bool
    directory_only: bool


class GitIgnore:
    """A ``.gitignore`` rule set anchored at one directory.

    Supports the subset that decides what is in a repository in practice:
    comments, negation, directory-only patterns, anchored patterns, ``*``,
    ``**``, ``?`` and character classes. Later rules win, which is what makes
    negation work.
    """

    __slots__ = ("_base", "_rules")

    def __init__(self, patterns: Iterable[str], *, base: str = "") -> None:
        self._base = base.strip("/")
        rules: list[_Rule] = []
        for raw in patterns:
            rule = self._compile(raw)
            if rule is not None:
                rules.append(rule)
        self._rules = tuple(rules)

    @classmethod
    def from_file(cls, path: Path, *, base: str = "") -> GitIgnore:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return cls((), base=base)
        return cls(text.splitlines(), base=base)

    @property
    def base(self) -> str:
        return self._base

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def _compile(self, raw: str) -> _Rule | None:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            return None
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        if line.startswith("\\"):
            line = line[1:]
        directory_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            return None
        anchored = line.startswith("/") or "/" in line.rstrip("/")
        line = line.lstrip("/")
        body = _translate(line)
        prefix = f"{re.escape(self._base)}/" if self._base else ""
        if anchored:
            pattern = f"^{prefix}{body}(?:/.*)?$"
        else:
            pattern = f"^{prefix}(?:.*/)?{body}(?:/.*)?$"
        return _Rule(regex=re.compile(pattern), negated=negated, directory_only=directory_only)

    def decide(self, relpath: str, *, is_dir: bool) -> bool | None:
        """``True`` ignore, ``False`` explicitly un-ignored, ``None`` no opinion."""
        verdict: bool | None = None
        for rule in self._rules:
            if rule.directory_only and not is_dir:
                continue
            if rule.regex.match(relpath):
                verdict = not rule.negated
        return verdict


class _IgnoreStack:
    """Composed ``.gitignore`` rule sets, nearest directory last."""

    __slots__ = ("_layers",)

    def __init__(self) -> None:
        self._layers: list[GitIgnore] = []

    def push(self, layer: GitIgnore) -> None:
        self._layers.append(layer)

    def pop(self) -> None:
        self._layers.pop()

    def ignored(self, relpath: str, *, is_dir: bool) -> bool:
        verdict: bool | None = None
        for layer in self._layers:
            decision = layer.decide(relpath, is_dir=is_dir)
            if decision is not None:
                verdict = decision
        return bool(verdict)


# --------------------------------------------------------------------------- #
# Worktree walk                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WalkedFile:
    """One file a worktree walk produced."""

    path: str
    content_digest: str
    size: int
    mode: int
    parser_profile: str
    #: ``False`` when the file is over the size cap: manifested, not uploaded.
    uploadable: bool

    def entry(self) -> ManifestEntry:
        return ManifestEntry(
            path=self.path,
            content_digest=self.content_digest,
            parser_profile=self.parser_profile,
            mode=self.mode,
            size=self.size,
        )


@dataclass(frozen=True, slots=True)
class WalkResult:
    files: tuple[WalkedFile, ...]
    ignored: int
    symlinks: int
    oversize: int
    unreadable: int
    truncated: bool = False

    def entries(self) -> tuple[ManifestEntry, ...]:
        return tuple(walked.entry() for walked in self.files)

    def root(self) -> str:
        return manifest_root(self.entries())


@dataclass
class _Counters:
    ignored: int = 0
    symlinks: int = 0
    oversize: int = 0
    unreadable: int = 0
    files: list[WalkedFile] = field(default_factory=list)


def walk_worktree(
    root: Path,
    *,
    size_cap: int,
    max_files: int = 200_000,
    read_limit: int = 64 * 1024 * 1024,
) -> WalkResult:
    """Walk a worktree into canonical manifest rows.

    ``.gitignore`` is respected at every level, ``.git`` is never entered, and
    symlinks are counted but never followed -- following one is how a walk
    escapes the repository it is supposed to describe. A file larger than
    ``size_cap`` still produces a row (path, mode, size and a digest) but is
    marked not uploadable. ``read_limit`` bounds what is hashed at all.
    """
    counters = _Counters()
    stack = _IgnoreStack()
    truncated = _descend(root, "", stack, counters, size_cap=size_cap, max_files=max_files, read_limit=read_limit)
    ordered = sorted(counters.files, key=lambda walked: walked.path)
    return WalkResult(
        files=tuple(ordered),
        ignored=counters.ignored,
        symlinks=counters.symlinks,
        oversize=counters.oversize,
        unreadable=counters.unreadable,
        truncated=truncated,
    )


def _descend(
    directory: Path,
    relative: str,
    stack: _IgnoreStack,
    counters: _Counters,
    *,
    size_cap: int,
    max_files: int,
    read_limit: int,
) -> bool:
    ignore_file = directory / ".gitignore"
    pushed = False
    if ignore_file.is_file():
        stack.push(GitIgnore.from_file(ignore_file, base=relative))
        pushed = True
    try:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            counters.unreadable += 1
            return False
        for child in children:
            name = child.name
            child_relative = f"{relative}/{name}" if relative else name
            if name in _NEVER_WALK:
                continue
            if child.is_symlink():
                counters.symlinks += 1
                continue
            try:
                is_dir = child.is_dir(follow_symlinks=False)
            except OSError:
                counters.unreadable += 1
                continue
            if stack.ignored(child_relative, is_dir=is_dir):
                counters.ignored += 1
                continue
            if is_dir:
                if _descend(
                    Path(child.path),
                    child_relative,
                    stack,
                    counters,
                    size_cap=size_cap,
                    max_files=max_files,
                    read_limit=read_limit,
                ):
                    return True
                continue
            if not child.is_file(follow_symlinks=False):
                continue
            if len(counters.files) >= max_files:
                return True
            walked = _walk_file(Path(child.path), child_relative, size_cap=size_cap, read_limit=read_limit)
            if walked is None:
                counters.unreadable += 1
                continue
            if not walked.uploadable:
                counters.oversize += 1
            counters.files.append(walked)
        return False
    finally:
        if pushed:
            stack.pop()


def _walk_file(path: Path, relative: str, *, size_cap: int, read_limit: int) -> WalkedFile | None:
    try:
        info = path.stat()
    except OSError:
        return None
    size = int(info.st_size)
    if size > read_limit:
        # Too large even to digest. Record it by metadata so the path is still
        # searchable, with a digest over the metadata rather than the bytes.
        digest = sha256_hex(f"{MANIFEST_FORMAT}:oversize:{relative}:{size}".encode())
        return WalkedFile(
            path=relative,
            content_digest=digest,
            size=size,
            mode=_mode_of(info.st_mode),
            parser_profile=profile_for_path(relative),
            uploadable=False,
        )
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                hasher.update(block)
    except OSError:
        return None
    return WalkedFile(
        path=relative,
        content_digest=hasher.hexdigest(),
        size=size,
        mode=_mode_of(info.st_mode),
        parser_profile=profile_for_path(relative),
        uploadable=size <= size_cap,
    )


def _mode_of(raw: int) -> int:
    """Git's two file modes, and nothing host-specific."""
    return 0o100755 if raw & stat.S_IXUSR else 0o100644
