"""Build an immutable Git base generation plus the working-tree overlay.

Ordinary sync used to send the complete working tree while labelling it with
``base_revision=HEAD``. That is not an immutable indexing identity: two dirty
worktrees on the same commit can have different roots. This module computes the
actual HEAD manifest cheaply by reusing the already-walked rows for unchanged
files and reading Git blobs only for tracked paths that differ from the walk.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .config import REVIEW_CONFIG_PATH
from .errors import AgentAction, ClientError, ErrorCode
from .manifest import ManifestEntry, profile_for_path, sha256_hex

__all__ = ["GenerationPlan", "plan_repository_generation"]

_GIT_TIMEOUT_S: Final[float] = 60.0
_EMPTY_REVISION: Final[str] = "0" * 40
_NEVER_GENERATION_ROOTS: Final[frozenset[str]] = frozenset({".git", ".lc-worktrees", ".lemoncrow"})


@dataclass(frozen=True, slots=True)
class _TreeBlob:
    path: str
    oid: str
    mode: int


@dataclass(frozen=True, slots=True)
class GenerationPlan:
    """The immutable base and exact initial View overlay for one worktree."""

    base_revision: str
    base_entries: tuple[ManifestEntry, ...]
    overlay_entries: tuple[ManifestEntry, ...]
    removed_paths: tuple[str, ...]
    #: Historical base bytes needed because the current worktree no longer
    #: carries that base digest. Only indexable payloads are retained here.
    base_payloads: dict[str, tuple[bytes, str]]

    @property
    def overlay_paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.overlay_entries) + self.removed_paths


def _git(repo: Path, *arguments: str, required: bool = True) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    if completed.returncode != 0 and required:
        lines = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = lines[-1] if lines else "git command failed"
        raise ClientError(
            ErrorCode.PAYLOAD_INVALID,
            detail[:512],
            action=AgentAction.FIX_REQUEST,
        )
    return completed.stdout if completed.returncode == 0 else b""


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "--verify", "HEAD^{commit}", required=False).decode("ascii", errors="ignore").strip()


def _tree(repo: Path, revision: str) -> tuple[_TreeBlob, ...]:
    if not revision:
        return ()
    rows: list[_TreeBlob] = []
    raw = _git(repo, "ls-tree", "-rz", "--full-tree", revision)
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_raw = record.split(b"\t", 1)
            mode_raw, kind, oid = metadata.decode("ascii").split()[:3]
            mode = int(mode_raw, 8)
        except (ValueError, UnicodeDecodeError):
            continue
        # Symlinks and gitlinks are intentionally outside the ordinary View
        # protocol today. Match worktree walking rather than inventing bytes.
        if kind != "blob" or mode not in {0o100644, 0o100755}:
            continue
        path = path_raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        if path.split("/", 1)[0] in _NEVER_GENERATION_ROOTS and path != REVIEW_CONFIG_PATH:
            continue
        rows.append(_TreeBlob(path=path, oid=oid, mode=mode))
    return tuple(rows)


def _changed_tracked_paths(repo: Path, revision: str) -> frozenset[str]:
    if not revision:
        return frozenset()
    raw = _git(repo, "diff", "--name-only", "--no-renames", "-z", revision, "--")
    return frozenset(part.decode("utf-8", errors="surrogateescape") for part in raw.split(b"\0") if part)


def _blob(repo: Path, oid: str) -> bytes:
    return _git(repo, "cat-file", "blob", oid)


def plan_repository_generation(
    repo: Path,
    worktree_entries: tuple[ManifestEntry, ...],
    *,
    content_size_cap: int,
) -> GenerationPlan:
    """Return ``HEAD`` as immutable base plus the worktree delta.

    Cost on the common path is Git tree/status metadata plus dictionary work.
    Source bytes are reread from Git only for base paths that differ from the
    already-walked working tree (modified/deleted/mode-changed paths). That
    keeps warm large-repository bootstrap proportional to the overlay rather
    than hashing the entire commit a second time.
    """

    root = repo.expanduser().resolve()
    before = _head(root)
    current = {entry.path: entry for entry in worktree_entries}
    if not before:
        return GenerationPlan(
            base_revision=_EMPTY_REVISION,
            base_entries=(),
            overlay_entries=tuple(sorted(current.values(), key=lambda entry: entry.path)),
            removed_paths=(),
            base_payloads={},
        )

    changed = _changed_tracked_paths(root, before)
    base: dict[str, ManifestEntry] = {}
    payloads: dict[str, tuple[bytes, str]] = {}
    for item in _tree(root, before):
        existing = current.get(item.path)
        if item.path not in changed and existing is not None and existing.mode == item.mode:
            base[item.path] = existing
            continue
        data = _blob(root, item.oid)
        digest = sha256_hex(data)
        profile = profile_for_path(item.path)
        entry = ManifestEntry(
            path=item.path,
            content_digest=digest,
            parser_profile=profile,
            mode=item.mode,
            size=len(data),
        )
        base[item.path] = entry
        if len(data) <= content_size_cap:
            payloads[digest] = (data, profile)

    after = _head(root)
    if after != before:
        raise ClientError(
            ErrorCode.VIEW_REVISION_STALE,
            "HEAD changed while the repository generation was being prepared; retry",
            details={"before": before, "after": after},
            retryable=True,
            action=AgentAction.RETRY_LATER,
        )

    overlay = tuple(current[path] for path in sorted(current) if base.get(path) != current[path])
    removed = tuple(path for path in sorted(base) if path not in current)
    return GenerationPlan(
        base_revision=before,
        base_entries=tuple(base[path] for path in sorted(base)),
        overlay_entries=overlay,
        removed_paths=removed,
        base_payloads=payloads,
    )
