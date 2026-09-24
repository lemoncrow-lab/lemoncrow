"""The canonical manifest, and the walk that produces it.

The server verifies the assembled rows against the announced ``manifest_root``
when the last chunk lands, so a client that invents a root digest is refused.
That makes this module a *conformance* surface rather than an implementation
detail: the encoding here has to be the encoding in
``lemoncrow_server_core.index.manifest``, and
``tests/test_manifest_conformance.py`` asserts it against the server's own
functions on randomized trees whenever the server package is importable.

The walk is the other half of the same contract. ``.gitignore`` is respected at
every level, ``.git`` is never entered, symlinks are counted but never followed,
and a file over the server's ``content_size_cap`` produces a row but is never
uploaded. Following a symlink is how a walk escapes the repository it is meant
to describe, and hashing a 400 MB object database is how a "thin" client stops
being thin.

The walk is also the whole cost of a warm ``SessionStart``, so two things about
its shape are deliberate:

* **One ``lstat`` per entry, and no ``Path``.** ``os.scandir`` already returns a
  handle that can be stat-ed; going through ``Path(entry.path).stat()`` costs a
  second syscall, a second object, and -- because ``Path.stat`` follows symlinks
  -- a second answer to a question already settled.
* **The hash is optional.** A :class:`~lemoncrow_client.walkcache.WalkCache`
  turns "hash every file" into "hash the files that changed". It is the module
  that owns the proof that a skipped hash equals a performed one; this one only
  asks. With no cache passed, every file is read and hashed, which is what every
  conformance test here does.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .config import LOCAL_FS_PROOF_PATH
from .walkcache import WalkCache, WalkCacheStats

__all__ = [
    "DEFAULT_PARSER_PROFILE",
    "EMPTY_MANIFEST_ROOT",
    "MANIFEST_FORMAT",
    "GitIgnore",
    "ManifestChunk",
    "ManifestEntry",
    "WalkResult",
    "WalkedFile",
    "canonical_rows",
    "chunk_manifest",
    "manifest_root",
    "profile_for_path",
    "sha256_hex",
    "walk_worktree",
]

#: Part of the root digest, so an old client and a new server can never agree
#: on a root by accident.
MANIFEST_FORMAT: Final[str] = "lemoncrow-manifest/1"

DEFAULT_PARSER_PROFILE: Final[str] = "lexical-1"

_ROW_SEPARATOR: Final[str] = "\x00"
_DEFAULT_CHUNK_ROWS: Final[int] = 2_000
_NEVER_WALK: Final[frozenset[str]] = frozenset({".git", ".lc-worktrees", ".lemoncrow"})

#: Extension to parser profile. A *hint*: the server derives Layer 1 under
#: whatever profile the manifest row declares, because two repositories can
#: legitimately read the same bytes as different languages. Mirrors the
#: server's ``index.analysis.profile_for_path``.
_PROFILE_BY_EXTENSION: Final[dict[str, str]] = {
    ".py": "python-1",
    ".pyi": "python-1",
    ".ts": "typescript-1",
    ".tsx": "typescript-1",
    ".js": "typescript-1",
    ".jsx": "typescript-1",
    ".mjs": "typescript-1",
    ".cjs": "typescript-1",
    ".go": "go-1",
    ".rs": "rust-1",
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def profile_for_path(path: str) -> str:
    """Which parser profile a manifest row should declare for ``path``."""
    lowered = path.lower()
    dot = lowered.rfind(".")
    if dot <= lowered.rfind("/"):
        return DEFAULT_PARSER_PROFILE
    return _PROFILE_BY_EXTENSION.get(lowered[dot:], DEFAULT_PARSER_PROFILE)


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One Layer-2 row: a repository path selecting a Layer-1 artifact."""

    path: str
    content_digest: str
    parser_profile: str
    mode: int
    size: int

    def to_wire(self) -> dict[str, object]:
        return {
            "path": self.path,
            "content_digest": self.content_digest,
            "parser_profile": self.parser_profile,
            "mode": self.mode,
            "size": self.size,
        }


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


#: A view that legitimately contains no files still has a verifiable root.
EMPTY_MANIFEST_ROOT: Final[str] = _digest_rows(())


@dataclass(frozen=True, slots=True)
class ManifestChunk:
    """One bounded piece of a canonical manifest, identified by its own rows."""

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


# --------------------------------------------------------------------------- #
# .gitignore                                                                  #
# --------------------------------------------------------------------------- #


def _translate(pattern: str) -> str:
    """Translate one gitignore glob to a regex body."""
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
    """A ``.gitignore`` rule set anchored at one directory. Later rules win."""

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
    """One file the walk produced."""

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
    #: Where this walk's digests came from. Everything is ``hashed`` when no
    #: cache was passed, which is what makes the fast path auditable rather than
    #: merely fast.
    digests: WalkCacheStats = field(default_factory=WalkCacheStats)

    def entries(self) -> tuple[ManifestEntry, ...]:
        return tuple(walked.entry() for walked in self.files)

    def root(self) -> str:
        return manifest_root(self.entries())

    def uploadable(self) -> Iterator[WalkedFile]:
        """Files whose bytes the server will actually accept."""
        for walked in self.files:
            if walked.uploadable:
                yield walked


@dataclass
class _Counters:
    ignored: int = 0
    symlinks: int = 0
    oversize: int = 0
    unreadable: int = 0
    hashed: int = 0
    files: list[WalkedFile] = field(default_factory=list)


def walk_worktree(
    root: Path,
    *,
    size_cap: int,
    max_files: int = 200_000,
    read_limit: int = 64 * 1024 * 1024,
    cache: WalkCache | None = None,
) -> WalkResult:
    """Walk a worktree into canonical manifest rows.

    ``cache`` may supply a digest for a file it can prove is unchanged. The rows
    are identical either way -- that equality is the property
    ``tests/test_walk_cache.py`` asserts over randomized mutations -- so the only
    observable difference is how many files were read.
    """
    counters = _Counters()
    stack = _IgnoreStack()
    if cache is not None:
        cache.begin()
    truncated = _descend(
        root,
        "",
        stack,
        counters,
        size_cap=size_cap,
        max_files=max_files,
        read_limit=read_limit,
        cache=cache,
    )
    ordered = sorted(counters.files, key=lambda walked: walked.path)
    return WalkResult(
        files=tuple(ordered),
        ignored=counters.ignored,
        symlinks=counters.symlinks,
        oversize=counters.oversize,
        unreadable=counters.unreadable,
        truncated=truncated,
        digests=cache.stats if cache is not None else WalkCacheStats(hashed=counters.hashed),
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
    cache: WalkCache | None,
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
            if name in _NEVER_WALK or child_relative == LOCAL_FS_PROOF_PATH:
                if child_relative == LOCAL_FS_PROOF_PATH:
                    counters.ignored += 1
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
                    cache=cache,
                ):
                    return True
                continue
            try:
                # The one stat of the walk. ``follow_symlinks=False`` is not a
                # micro-optimization: the entry was already established not to be
                # a symlink, and a following stat would answer about whatever it
                # became since.
                info = child.stat(follow_symlinks=False)
            except OSError:
                counters.unreadable += 1
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if len(counters.files) >= max_files:
                return True
            walked = _walk_file(
                child.path,
                child_relative,
                info,
                counters,
                size_cap=size_cap,
                read_limit=read_limit,
                cache=cache,
            )
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


def _walk_file(
    path: str,
    relative: str,
    info: os.stat_result,
    counters: _Counters,
    *,
    size_cap: int,
    read_limit: int,
    cache: WalkCache | None,
) -> WalkedFile | None:
    size = int(info.st_size)
    mode = _mode_of(info.st_mode)
    if size > read_limit:
        # Manifested so the view knows the path exists, never read: hashing an
        # object that large is how a thin client stops being thin.
        return WalkedFile(
            path=relative,
            content_digest=sha256_hex(f"{MANIFEST_FORMAT}:oversize:{relative}:{size}".encode()),
            size=size,
            mode=mode,
            parser_profile=profile_for_path(relative),
            uploadable=False,
        )
    known: str | None = None
    if cache is not None:
        known = cache.lookup(relative, info)
        if known is None:
            known = cache.lookup_blob(relative, info)
    if known is None:
        known = _hash_file(path)
        if known is None:
            return None
        counters.hashed += 1
        if cache is not None:
            try:
                # The stat *after* the read. A write that landed while the file
                # was being hashed shows up here, and the cache refuses to
                # remember a digest it cannot attribute to a settled file.
                cache.record(relative, os.stat(path, follow_symlinks=False), known)
            except OSError:
                pass
    return WalkedFile(
        path=relative,
        content_digest=known,
        size=size,
        mode=mode,
        parser_profile=profile_for_path(relative),
        uploadable=size <= size_cap,
    )


def _hash_file(path: str) -> str | None:
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                hasher.update(block)
    except OSError:
        return None
    return hasher.hexdigest()


def _mode_of(raw: int) -> int:
    """Git's two file modes, and nothing host-specific."""
    return 0o100755 if raw & stat.S_IXUSR else 0o100644
