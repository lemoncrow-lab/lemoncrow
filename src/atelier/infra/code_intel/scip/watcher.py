"""Bounded refresh detection for repo-local SCIP artifacts."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_GITDIR_PREFIX = "gitdir: "
_HEAD_REF_PREFIX = "ref: "
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_BRANCH_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_INOTIFY_MASK = (
    0x00000002  # IN_MODIFY
    | 0x00000004  # IN_ATTRIB
    | 0x00000008  # IN_CLOSE_WRITE
    | 0x00000080  # IN_MOVED_TO
    | 0x00000040  # IN_MOVED_FROM
    | 0x00000100  # IN_CREATE
    | 0x00000200  # IN_DELETE
    | 0x00000400  # IN_DELETE_SELF
    | 0x00000800  # IN_MOVE_SELF
)


@dataclass(frozen=True)
class GitRepoState:
    """Resolved git metadata needed for branch-aware SCIP freshness."""

    git_dir: Path
    common_dir: Path
    head_path: Path
    head_ref: str | None
    head_sha: str | None
    ref_path: Path | None
    packed_refs_path: Path
    branch_key: str

    @property
    def identity(self) -> str:
        if self.head_ref:
            return self.head_ref
        if self.head_sha:
            return f"detached:{self.head_sha}"
        return "unknown"


def _artifact_signature(artifact_paths: list[Path]) -> str:
    parts: list[str] = []
    for path in sorted(artifact_paths):
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}")
    return "\n".join(parts)


def _read_optional_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _resolve_git_dir(repo_root: Path) -> Path:
    dot_git = (repo_root / ".git").resolve()
    if dot_git.is_dir():
        return dot_git
    raw = _read_optional_text(dot_git)
    if raw and raw.startswith(_GITDIR_PREFIX):
        target = raw[len(_GITDIR_PREFIX) :].strip()
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = (dot_git.parent / candidate).resolve()
        return candidate.resolve()
    return dot_git


def _resolve_common_dir(git_dir: Path) -> Path:
    raw = _read_optional_text(git_dir / "commondir")
    if not raw:
        return git_dir
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (git_dir / candidate).resolve()
    return candidate.resolve()


def _read_ref_sha(path: Path) -> str | None:
    raw = _read_optional_text(path)
    if not raw or raw.startswith(_HEAD_REF_PREFIX):
        return None
    return raw if _GIT_SHA_RE.fullmatch(raw) else None


def _lookup_packed_ref(packed_refs_path: Path, ref_name: str) -> str | None:
    raw = _read_optional_text(packed_refs_path)
    if raw is None:
        return None
    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith(("#", "^")):
            continue
        sha, _, name = entry.partition(" ")
        if name == ref_name and _GIT_SHA_RE.fullmatch(sha):
            return sha
    return None


def _resolve_head_ref_path(git_dir: Path, common_dir: Path, ref_name: str) -> Path:
    git_path = git_dir / ref_name
    if git_path.exists():
        return git_path.resolve()
    return (common_dir / ref_name).resolve()


def _sanitize_branch_segment(value: str) -> str:
    sanitized = _BRANCH_SEGMENT_RE.sub("-", value).strip("-._")
    return sanitized[:48] or "branch"


def _branch_key(identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{_sanitize_branch_segment(identity)}-{digest}"


def resolve_git_repo_state(repo_root: Path) -> GitRepoState:
    """Resolve HEAD/ref metadata for normal repos and git worktrees."""

    resolved_root = repo_root.resolve()
    git_dir = _resolve_git_dir(resolved_root)
    common_dir = _resolve_common_dir(git_dir)
    head_path = (git_dir / "HEAD").resolve()
    head_raw = _read_optional_text(head_path)
    head_ref: str | None = None
    head_sha: str | None = None
    ref_path: Path | None = None
    if head_raw:
        if head_raw.startswith(_HEAD_REF_PREFIX):
            head_ref = head_raw[len(_HEAD_REF_PREFIX) :].strip()
            if head_ref:
                ref_path = _resolve_head_ref_path(git_dir, common_dir, head_ref)
                head_sha = _read_ref_sha(ref_path) or _lookup_packed_ref(common_dir / "packed-refs", head_ref)
        elif _GIT_SHA_RE.fullmatch(head_raw):
            head_sha = head_raw
    identity = head_ref or (f"detached-{head_sha[:12]}" if head_sha else "unknown")
    return GitRepoState(
        git_dir=git_dir,
        common_dir=common_dir,
        head_path=head_path,
        head_ref=head_ref,
        head_sha=head_sha,
        ref_path=ref_path,
        packed_refs_path=(common_dir / "packed-refs").resolve(),
        branch_key=_branch_key(identity),
    )


def _nearest_existing_path(path: Path) -> Path | None:
    candidate = path.resolve()
    while True:
        if candidate.exists():
            return candidate
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent


def _watch_paths(repo_state: GitRepoState, cache_root: Path) -> list[Path]:
    candidates = [
        cache_root.parent,
        cache_root,
        repo_state.head_path,
        repo_state.packed_refs_path,
    ]
    if repo_state.ref_path is not None:
        candidates.append(repo_state.ref_path)
    resolved: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        watch_path = _nearest_existing_path(candidate)
        if watch_path is None or watch_path in seen:
            continue
        seen.add(watch_path)
        resolved.append(watch_path)
    return resolved


def _watch_signature(repo_state: GitRepoState, cache_root: Path, artifact_paths: list[Path]) -> str:
    parts = [
        f"cache_root={cache_root.resolve()}",
        f"branch_key={repo_state.branch_key}",
        f"head_ref={repo_state.head_ref or ''}",
        f"head_sha={repo_state.head_sha or ''}",
        _artifact_signature(artifact_paths),
    ]
    return "\n".join(parts)


class _InotifyWatcher:
    """Best-effort Linux inotify integration with safe fallback elsewhere."""

    def __init__(self) -> None:
        self._fd: int | None = None
        self._wd_by_path: dict[Path, int] = {}
        self._path_by_wd: dict[int, Path] = {}
        try:
            libc = ctypes.CDLL(None, use_errno=True)
        except OSError:
            return
        init1 = getattr(libc, "inotify_init1", None)
        add_watch = getattr(libc, "inotify_add_watch", None)
        rm_watch = getattr(libc, "inotify_rm_watch", None)
        if init1 is None or add_watch is None or rm_watch is None:
            return
        init1.argtypes = [ctypes.c_int]
        init1.restype = ctypes.c_int
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        rm_watch.restype = ctypes.c_int
        fd = init1(os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        if fd < 0:
            return
        self._fd = fd
        self._add_watch = add_watch
        self._rm_watch = rm_watch

    def drain(self) -> bool:
        if self._fd is None:
            return False
        changed = False
        while True:
            try:
                payload = os.read(self._fd, 4096)
            except BlockingIOError:
                break
            except OSError:
                self.close()
                break
            if not payload:
                break
            changed = True
        return changed

    def sync_paths(self, paths: list[Path]) -> None:
        if self._fd is None:
            return
        desired = {path.resolve() for path in paths}
        for path in list(self._wd_by_path):
            if path in desired:
                continue
            wd = self._wd_by_path.pop(path)
            self._path_by_wd.pop(wd, None)
            self._rm_watch(self._fd, wd)
        for path in sorted(desired):
            if path in self._wd_by_path or not path.exists():
                continue
            wd = self._add_watch(self._fd, os.fsencode(path), _INOTIFY_MASK)
            if wd < 0:
                continue
            self._wd_by_path[path] = wd
            self._path_by_wd[wd] = path

    def close(self) -> None:
        if self._fd is None:
            return
        os.close(self._fd)
        self._fd = None
        self._wd_by_path.clear()
        self._path_by_wd.clear()


class ScipArtifactWatcher:
    """Tracks artifact refreshes and invalidates cache state through a callback."""

    def __init__(
        self,
        *,
        repo_root: Path,
        cache_root: Callable[[], Path],
        state_sync: Callable[[str, str], bool],
        state_key: str = "scip_artifact_signature",
    ) -> None:
        self._repo_root = repo_root.resolve()
        self._cache_root = cache_root
        self._state_sync = state_sync
        self._state_key = state_key
        self._events = _InotifyWatcher()

    def refresh(self, artifact_paths: list[Path]) -> bool:
        repo_state = resolve_git_repo_state(self._repo_root)
        active_cache_root = self._cache_root().resolve()
        self._events.drain()
        self._events.sync_paths(_watch_paths(repo_state, active_cache_root))
        return self._state_sync(self._state_key, _watch_signature(repo_state, active_cache_root, artifact_paths))


__all__ = ["GitRepoState", "ScipArtifactWatcher", "resolve_git_repo_state"]
