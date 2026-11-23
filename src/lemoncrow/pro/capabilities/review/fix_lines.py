"""Local Review fix lines: fix one immutable Snapshot in its own worktree.

A fix line is a child Review bound to a temporary git branch/worktree.  It starts
from the exact selected Snapshot, records subsequent fix Snapshots in the child
Review, and can apply the latest fix Snapshot back to the parent checkout as
uncommitted changes.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from lemoncrow_client.review_words import revision_name

from lemoncrow.core.foundation.paths import confine_to_root
from lemoncrow.core.foundation.run_file_io import RunFileLock
from lemoncrow.pro.capabilities.review.session_models import (
    ReviewRelationship,
    ReviewRevision,
    ReviewSession,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore, utc_now

FIX_WORKTREE_DIR = ".lemoncrow/fixes"
FIX_LINE_KIND = "fix_line"
BRANCH_REVIEW_KEY = "lemoncrowReview"
OPEN_FIX_STATES = frozenset({"fixing", "applied_with_conflicts"})
CONFLICT_MARKER = b"<<<<<<< main worktree"

_SYNTHETIC_IDENTITY = {
    "GIT_AUTHOR_NAME": "LemonCrow",
    "GIT_AUTHOR_EMAIL": "lemoncrow@localhost",
    "GIT_COMMITTER_NAME": "LemonCrow",
    "GIT_COMMITTER_EMAIL": "lemoncrow@localhost",
}
_SYMLINK_MODE = 0o120000


class FixLineError(ValueError):
    """A fix-line action cannot proceed; the message is safe to show to the user."""

    def __init__(self, message: str, *, files: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.files = files


class FixLineDirty(FixLineError):
    """Closing would drop uncommitted work in the fix worktree."""


@dataclass(frozen=True)
class FixLine:
    relationship: ReviewRelationship
    worktree_path: str
    branch: str
    base_commit: str
    snapshot_number: int
    applied_revision_id: str = ""
    applied_files: tuple[str, ...] = ()
    applied_at: str = ""

    @property
    def id(self) -> str:
        return self.relationship.id

    @property
    def parent_review_id(self) -> str:
        return self.relationship.review_id

    @property
    def snapshot_revision_id(self) -> str:
        return self.relationship.external_id

    @property
    def child_review_id(self) -> str:
        return self.relationship.target_review_id

    @property
    def status(self) -> str:
        return self.relationship.status

    @property
    def name(self) -> str:
        return Path(self.worktree_path).name

    @property
    def worktree_missing(self) -> bool:
        return self.status in OPEN_FIX_STATES and not Path(self.worktree_path).is_dir()


FileOutcomeKind = Literal["write", "delete", "conflict", "unchanged"]


@dataclass(frozen=True)
class FileOutcome:
    path: str
    outcome: FileOutcomeKind
    content: bytes | None = None
    reason: str = ""
    expected_main_digest: str = ""


@dataclass(frozen=True)
class FixApplyPlan:
    fix_line_id: str
    revision_id: str
    fix_number: int
    files: tuple[FileOutcome, ...]
    warnings: tuple[str, ...] = ()

    @property
    def conflicts(self) -> tuple[FileOutcome, ...]:
        return tuple(item for item in self.files if item.outcome == "conflict")

    @property
    def changes(self) -> tuple[FileOutcome, ...]:
        return tuple(item for item in self.files if item.outcome in {"write", "delete"})


@dataclass(frozen=True)
class FixApplyResult:
    line: FixLine
    applied: bool
    plan: FixApplyPlan


@dataclass(frozen=True)
class ChecklistItem:
    annotation_id: str
    path: str
    start_line: int
    end_line: int
    body: str
    state: Literal["open", "touched", "resolved"]


def _fix_line(relationship: ReviewRelationship) -> FixLine:
    try:
        meta = json.loads(relationship.metadata_json or "{}")
    except ValueError:
        meta = {}
    return FixLine(
        relationship=relationship,
        worktree_path=str(meta.get("worktree_path") or ""),
        branch=str(meta.get("branch") or ""),
        base_commit=str(meta.get("base_commit") or ""),
        snapshot_number=int(meta.get("snapshot_number") or 0),
        applied_revision_id=str(meta.get("applied_revision_id") or ""),
        applied_files=tuple(str(path) for path in meta.get("applied_files") or ()),
        applied_at=str(meta.get("applied_at") or ""),
    )


def _metadata(line: FixLine, **changes: object) -> str:
    meta: dict[str, object] = {
        "worktree_path": line.worktree_path,
        "branch": line.branch,
        "base_commit": line.base_commit,
        "snapshot_number": line.snapshot_number,
        "applied_revision_id": line.applied_revision_id,
        "applied_files": list(line.applied_files),
        "applied_at": line.applied_at,
    }
    meta.update(changes)
    return json.dumps(meta, sort_keys=True)


def _set_state(store: ReviewStore, line: FixLine, status: str, **changes: object) -> FixLine:
    updated = store.update_relationship_if_unchanged(
        line.relationship,
        status=status,
        metadata_json=_metadata(line, **changes),
    )
    if updated is None:
        raise FixLineError("This fix line changed meanwhile; reload and try again")
    return _fix_line(updated)


def list_fix_lines(store: ReviewStore, parent_review_id: str) -> tuple[FixLine, ...]:
    return tuple(
        _fix_line(row)
        for row in store.list_relationships(parent_review_id, kind=FIX_LINE_KIND)
        if row.relation == "fixes"
    )


def get_fix_line(store: ReviewStore, parent_review_id: str, fix_id: str) -> FixLine:
    for line in list_fix_lines(store, parent_review_id):
        if line.id == fix_id:
            return line
    raise FixLineError("This fix line does not exist")


def fix_line_of_child(store: ReviewStore, child_review_id: str) -> FixLine | None:
    for row in store.list_relationships_by_kind(FIX_LINE_KIND):
        if row.target_review_id == child_review_id:
            return _fix_line(row)
    return None


def fix_child_review_ids(store: ReviewStore) -> frozenset[str]:
    return frozenset(
        row.target_review_id for row in store.list_relationships_by_kind(FIX_LINE_KIND) if row.target_review_id
    )


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise FixLineError(f"git {args[0]} failed: {detail}")
    return result.stdout


def _git_ok(cwd: Path, *args: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _blob_at(repo_root: Path, commit: str, path: str) -> bytes | None:
    # lc-debt: one git process per file; use cat-file --batch if fixes become huge.
    result = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "blob", f"{commit}:{path}"],
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _safe_target(root: Path, rel: str) -> Path:
    pure = PurePosixPath(rel)
    if not rel or pure.is_absolute() or ".." in pure.parts:
        raise FixLineError("The revision contains an unsafe path", files=(rel,))
    target = confine_to_root(root / rel, root)
    if target.is_symlink():
        raise FixLineError(
            "Fix lines do not yet support changed symlinks; use the main checkout for this fix",
            files=(rel,),
        )
    parent = target.parent
    while parent != root and parent != parent.parent:
        if parent.is_symlink():
            raise FixLineError(
                "Fix lines do not write through symlinked directories",
                files=(rel,),
            )
        parent = parent.parent
    return target


def _mode_for(tree: Mapping[str, Mapping[str, object]] | None, path: str) -> int:
    if tree is None:
        return 0
    row = tree.get(path)
    if not isinstance(row, Mapping):
        return 0
    raw = row.get("mode")
    return int(raw) if isinstance(raw, (int, str)) and str(raw).strip() else 0


def _validate_snapshot_file_types(
    store: ReviewStore,
    snapshot: ReviewRevision,
    packet: Mapping[str, object],
) -> None:
    """Fail closed when exact draft reconstruction needs type/mode semantics we cannot replay."""

    entries = packet.get("files")
    if not isinstance(entries, list):
        return
    degraded_raw = packet.get("degraded")
    degraded = (
        {str(item) for item in degraded_raw if isinstance(item, str)}
        if isinstance(degraded_raw, (list, tuple, set))
        else set()
    )
    if {"symlink_change", "file_mode_change"} & degraded or "mode_only" in degraded:
        raise FixLineError("This revision changes a symlink or file mode; exact fix worktrees do not support that yet")

    new_tree = store.read_source_tree_artifact(snapshot.review_id, snapshot.id, side="new")
    old_tree = store.read_source_tree_artifact(snapshot.review_id, snapshot.id, side="old")
    if new_tree is None and old_tree is None:
        return
    unsupported: list[str] = []
    for raw in entries:
        if not isinstance(raw, Mapping):
            continue
        path = str(raw.get("path") or "")
        old_path = str(raw.get("old_path") or path)
        status = str(raw.get("status") or "modified")
        old_mode = _mode_for(old_tree, old_path)
        new_mode = 0 if status == "deleted" else _mode_for(new_tree, path)
        if old_mode == _SYMLINK_MODE or new_mode == _SYMLINK_MODE:
            unsupported.append(path)
            continue
        if old_mode and new_mode and old_mode != new_mode:
            unsupported.append(path)
            continue
        if not old_mode and new_mode and (new_mode & 0o111):
            # An added executable would be materialized as 0644 by write_bytes.
            unsupported.append(path)
            continue
        if status == "renamed" and new_mode and (new_mode & 0o111):
            unsupported.append(path)
    if unsupported:
        raise FixLineError(
            "This revision changes a symlink or file mode; exact fix worktrees do not support that yet",
            files=tuple(sorted(set(unsupported))),
        )


def _snapshot_content(
    store: ReviewStore,
    snapshot: ReviewRevision,
    number: int,
) -> tuple[str, dict[str, bytes | None]]:
    from lemoncrow.pro.capabilities.review import snapshot as snapshot_module
    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    if snapshot.capture_kind == "commit" and snapshot.commit_sha:
        return snapshot.commit_sha, {}
    if snapshot.head_sha:
        return snapshot.head_sha, {}
    packet = read_packet_json(store, snapshot)
    rows = packet.get("files") if isinstance(packet, Mapping) else None
    if not isinstance(rows, list) or not snapshot.base_sha:
        raise FixLineError(f"{revision_name(number)} can't be rebuilt exactly: its stored files are missing")
    if any(isinstance(row, Mapping) and row.get("submodule_pointer") is not None for row in rows):
        raise FixLineError(f"{revision_name(number)} changes a submodule; fixing it in a worktree is not supported yet")
    assert isinstance(packet, Mapping)
    _validate_snapshot_file_types(store, snapshot, packet)
    overlay = snapshot_module.frozen_revision_overlay(store.root, snapshot, store=store)
    if overlay is None:
        raise FixLineError(f"{revision_name(number)} can't be rebuilt exactly: some changed files were not stored")
    return snapshot.base_sha, overlay


def _free_fix_name(root: Path, stem: str) -> tuple[str, str, Path]:
    for attempt in range(1, 100):
        name = stem if attempt == 1 else f"{stem}-{attempt}"
        branch = f"lc-fix/{name}"
        path = root / FIX_WORKTREE_DIR / name
        if not path.exists() and not _git_ok(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
            return name, branch, path
    raise FixLineError(f"every lc-fix/{stem} branch name is taken; delete old fix branches first")


def _exclude_fix_worktrees(root: Path) -> None:
    common = Path(_git(root, "rev-parse", "--git-common-dir").strip())
    exclude = (common if common.is_absolute() else root / common) / "info" / "exclude"
    entry = f"/{FIX_WORKTREE_DIR}/"
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if entry in existing.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(("" if not existing or existing.endswith("\n") else "\n") + entry + "\n")


def _commit_overlay(
    worktree: Path,
    overlay: Mapping[str, bytes | None],
    message: str,
) -> str:
    for rel, data in overlay.items():
        target = _safe_target(worktree, rel)
        if data is None:
            if target.is_file() or target.is_symlink():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if _git(worktree, "status", "--porcelain", "--untracked-files=all").strip():
        _git(worktree, "add", "-A")
        _git(
            worktree,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            message,
            env=_SYNTHETIC_IDENTITY,
        )
    return _git(worktree, "rev-parse", "HEAD").strip()


def _undo_start(
    store: ReviewStore,
    root: Path,
    path: Path,
    branch: str,
    child: ReviewSession | None,
    relationship: ReviewRelationship | None,
) -> None:
    if relationship is not None:
        store.update_relationship_if_unchanged(
            relationship,
            status="abandoned",
            metadata_json=relationship.metadata_json,
        )
    if child is not None:
        store.unbind_review(child.id)
        store.update_session(child.id, status="archived")
    _git(root, "worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)
    _git(root, "worktree", "prune", check=False)
    _git(root, "branch", "-D", branch, check=False)


def start_fix_line(
    store: ReviewStore,
    parent: ReviewSession,
    snapshot: ReviewRevision,
    *,
    repo_root: Path,
    repository_identity: str,
    review_short: str,
    snapshot_number: int,
) -> tuple[FixLine, bool]:
    from lemoncrow.pro.capabilities.review.sources.local import review_subject, worktree_key

    if snapshot.review_id != parent.id:
        raise FixLineError("That revision belongs to another Review")
    if parent.status != "open":
        raise FixLineError("Reopen this Review before fixing one of its revisions")
    root = repo_root.resolve()
    with RunFileLock(store.root / "fix-lines"):
        for line in list_fix_lines(store, parent.id):
            if line.snapshot_revision_id == snapshot.id and line.status in OPEN_FIX_STATES:
                return line, False
        base_sha, overlay = _snapshot_content(store, snapshot, snapshot_number)
        _name, branch, path = _free_fix_name(root, f"{review_short}-s{snapshot_number}")
        child: ReviewSession | None = None
        relationship: ReviewRelationship | None = None
        try:
            _exclude_fix_worktrees(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            _git(root, "worktree", "add", "-q", "-b", branch, str(path), base_sha)
            heading = (
                f"{revision_name(snapshot_number)} · {parent.title}" if parent.title else revision_name(snapshot_number)
            )
            base_commit = _commit_overlay(path, overlay, heading)
            subject = review_subject(
                repository_identity,
                branch=branch,
                worktree_key=worktree_key(path),
                range_mode="working_tree",
            )
            if subject is None:
                raise FixLineError("This Review has no repository identity to bind a fix branch to")
            title = (
                f"Fix of {revision_name(snapshot_number)} · {parent.title}"
                if parent.title
                else f"Fix of {revision_name(snapshot_number)}"
            )
            created_child, created = store.create_bound_session(
                ReviewSession(
                    id="",
                    subject_type="local_change",
                    repo_root=parent.repo_root,
                    range_mode="working_tree",
                    title=title[:120],
                    review_base_sha=base_commit,
                    actor_type="human",
                    reviewer_id=parent.reviewer_id,
                ),
                subject.binding(),
            )
            if not created:
                raise FixLineError(f"{branch} already belongs to another Review")
            child = created_child
            relationship = store.upsert_relationship(
                ReviewRelationship(
                    id="",
                    review_id=parent.id,
                    kind=FIX_LINE_KIND,
                    relation="fixes",
                    provider="lemoncrow",
                    external_id=snapshot.id,
                    target_review_id=child.id,
                    status="fixing",
                    metadata_json=json.dumps(
                        {
                            "worktree_path": str(path),
                            "branch": branch,
                            "base_commit": base_commit,
                            "snapshot_number": snapshot_number,
                            "applied_revision_id": "",
                            "applied_files": [],
                            "applied_at": "",
                        },
                        sort_keys=True,
                    ),
                )
            )
            _git(
                root,
                "config",
                f"branch.{branch}.{BRANCH_REVIEW_KEY}",
                child.id,
            )
        except BaseException:
            _undo_start(store, root, path, branch, child, relationship)
            raise
        return _fix_line(relationship), True


def _digest(data: bytes | None) -> str:
    if data is None:
        return "absent"
    return hashlib.sha256(data).hexdigest()


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def merge_text(ours: bytes, base: bytes, theirs: bytes) -> tuple[bytes, bool]:
    with tempfile.TemporaryDirectory(prefix="lc-fix-merge-") as scratch:
        paths: list[str] = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            target = Path(scratch) / name
            target.write_bytes(data)
            paths.append(str(target))
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "-L",
                "main worktree",
                "-L",
                "Revision",
                "-L",
                "fix",
                *paths,
            ],
            capture_output=True,
            check=False,
        )
    if result.returncode < 0 or result.returncode >= 128:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise FixLineError(f"git merge-file failed: {detail}")
    return result.stdout, result.returncode > 0


def decide(
    path: str,
    base: bytes | None,
    ours: bytes | None,
    theirs: bytes | None,
) -> FileOutcome:
    expected = _digest(ours)
    if ours == theirs:
        return FileOutcome(path, "unchanged", expected_main_digest=expected)
    if theirs is None:
        if ours == base:
            return FileOutcome(path, "delete", expected_main_digest=expected)
        if ours is None:
            return FileOutcome(path, "unchanged", expected_main_digest=expected)
        return FileOutcome(
            path,
            "conflict",
            reason="changed in the main worktree; the fix deletes it",
            expected_main_digest=expected,
        )
    if base is None:
        if ours is None:
            return FileOutcome(path, "write", theirs, expected_main_digest=expected)
        return FileOutcome(
            path,
            "conflict",
            reason="added on both sides with different content",
            expected_main_digest=expected,
        )
    if ours is None:
        return FileOutcome(
            path,
            "conflict",
            reason="deleted in the main worktree; the fix changes it",
            expected_main_digest=expected,
        )
    if ours == base:
        return FileOutcome(path, "write", theirs, expected_main_digest=expected)
    if _is_binary(base) or _is_binary(ours) or _is_binary(theirs):
        return FileOutcome(
            path,
            "conflict",
            reason="binary file changed on both sides",
            expected_main_digest=expected,
        )
    merged, conflicted = merge_text(ours, base, theirs)
    if conflicted:
        return FileOutcome(
            path,
            "conflict",
            merged,
            reason="overlapping edits",
            expected_main_digest=expected,
        )
    if merged == ours:
        return FileOutcome(path, "unchanged", expected_main_digest=expected)
    return FileOutcome(path, "write", merged, expected_main_digest=expected)


def _git_path(root: Path, name: str) -> Path:
    raw = Path(_git(root, "rev-parse", "--git-path", name).strip())
    return raw if raw.is_absolute() else root / raw


def _require_quiet_checkout(root: Path) -> None:
    for marker in (
        "MERGE_HEAD",
        "rebase-merge",
        "rebase-apply",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
    ):
        if _git_path(root, marker).exists():
            raise FixLineError("Finish the merge or rebase in the main worktree first")
    unmerged = tuple(_git(root, "diff", "--name-only", "--diff-filter=U").splitlines())
    if unmerged:
        raise FixLineError("The main worktree has unmerged files", files=unmerged)


def _branch_warning(
    store: ReviewStore,
    parent: ReviewSession,
    root: Path,
) -> tuple[str, ...]:
    current = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
    for binding in store.list_bindings(parent.id, active_only=True):
        if binding.subject_kind != "branch":
            continue
        expected = binding.subject_key.removesuffix("#staged").removeprefix("refs/heads/")
        if current != expected:
            return (f"The main worktree is on {current or 'a detached HEAD'}, not {expected}",)
    return ()


def _read_main(root: Path, rel: str) -> bytes | None:
    target = _safe_target(root, rel)
    return target.read_bytes() if target.is_file() else None


def plan_fix_apply(
    store: ReviewStore,
    parent: ReviewSession,
    line: FixLine,
    *,
    repo_root: Path,
) -> FixApplyPlan:
    from lemoncrow.pro.capabilities.review import snapshot as snapshot_module
    from lemoncrow.pro.capabilities.review.gitdiff import source_state
    from lemoncrow.pro.capabilities.review.sources.local import range_for_session

    if line.status not in OPEN_FIX_STATES:
        raise FixLineError(f"This fix line is {line.status.replace('_', ' ')}")
    root = repo_root.resolve()
    child = store.get_session(line.child_review_id)
    latest = store.latest_revision(line.child_review_id)
    if child is None or latest is None:
        raise FixLineError("Capture the fix first: this fix line has no revision yet")
    fix_path = Path(line.worktree_path)
    if fix_path.is_dir() and latest.source_fingerprint:
        current = source_state(
            fix_path,
            range_for_session(fix_path, child),
        )
        if not current.matches(latest.source_fingerprint):
            raise FixLineError("Capture the fix first: the fix worktree has changes that are not in a revision")
    if latest.base_sha != line.base_commit:
        raise FixLineError("Capture the fix first: its latest revision is not based on the revision it fixes")
    overlay = snapshot_module.frozen_revision_overlay(store.root, latest, store=store)
    if overlay is None:
        raise FixLineError("The latest fix revision can't be read exactly")
    _require_quiet_checkout(root)
    files: list[FileOutcome] = []
    for rel, theirs in sorted(overlay.items()):
        ours = _read_main(root, rel)
        files.append(decide(rel, _blob_at(root, line.base_commit, rel), ours, theirs))
    return FixApplyPlan(
        fix_line_id=line.id,
        revision_id=latest.id,
        fix_number=latest.revision_number,
        files=tuple(files),
        warnings=_branch_warning(store, parent, root),
    )


def _journal_path(root: Path, line: FixLine) -> Path:
    return root / FIX_WORKTREE_DIR / f"{line.name}.apply.json"


def _atomic_write(target: Path, data: bytes) -> None:
    mode = target.stat().st_mode if target.exists() and not target.is_symlink() else None
    scratch = target.with_name(f".{target.name}.lc-fix-tmp")
    scratch.write_bytes(data)
    if mode is not None:
        scratch.chmod(mode)
    try:
        os.replace(scratch, target)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise


def _restore(root: Path, backups: list[dict[str, str | None]]) -> None:
    for entry in backups:
        path = str(entry.get("path") or "")
        target = _safe_target(root, path)
        original = entry.get("original")
        if original is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, base64.b64decode(original))


def recover_apply_journal(repo_root: Path, line: FixLine) -> bool:
    root = repo_root.resolve()
    journal = _journal_path(root, line)
    if not journal.is_file():
        return False
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FixLineError(
            "The previous fix apply journal is unreadable; do not apply again until it is repaired"
        ) from exc
    if str(payload.get("fix_line_id") or "") != line.id:
        raise FixLineError("The fix apply journal belongs to another fix line")
    backups = payload.get("files")
    if not isinstance(backups, list):
        raise FixLineError("The previous fix apply journal is incomplete")
    _restore(root, backups)
    journal.unlink()
    return True


def _current_digest(root: Path, rel: str) -> str:
    return _digest(_read_main(root, rel))


def _write_all(
    root: Path,
    line: FixLine,
    items: list[FileOutcome],
) -> Path:
    """Write outcomes transactionally; leave the recovery journal until state commits."""

    stale = tuple(item.path for item in items if _current_digest(root, item.path) != item.expected_main_digest)
    if stale:
        raise FixLineError(
            "The main worktree changed while the fix was being prepared; review the apply plan again",
            files=stale,
        )

    backups: list[dict[str, str | None]] = []
    for item in items:
        current = _read_main(root, item.path)
        backups.append(
            {
                "path": item.path,
                "original": (None if current is None else base64.b64encode(current).decode("ascii")),
            }
        )
    journal = _journal_path(root, line)
    journal.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        journal,
        json.dumps({"fix_line_id": line.id, "files": backups}).encode("utf-8"),
    )
    try:
        for item in items:
            if _current_digest(root, item.path) != item.expected_main_digest:
                raise FixLineError(
                    "The main worktree changed while the fix was being applied; nothing was kept",
                    files=(item.path,),
                )
            target = _safe_target(root, item.path)
            if item.outcome == "delete":
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(target, item.content or b"")
    except BaseException:
        _restore(root, backups)
        journal.unlink(missing_ok=True)
        raise
    return journal


def _finish_child(
    store: ReviewStore,
    line: FixLine,
    *,
    outcome: Literal["applied", "abandoned"],
) -> None:
    store.unbind_review(line.child_review_id)
    store.update_session(
        line.child_review_id,
        status="finished" if outcome == "applied" else "archived",
    )


def _cleanup_fix_checkout(root: Path, line: FixLine) -> None:
    path = Path(line.worktree_path)
    if path.is_dir():
        _git(root, "worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)
    _git(root, "worktree", "prune", check=False)
    if _git_ok(root, "show-ref", "--verify", "--quiet", f"refs/heads/{line.branch}"):
        _git(root, "branch", "-D", line.branch, check=False)


def apply_fix_line(
    store: ReviewStore,
    parent: ReviewSession,
    line: FixLine,
    *,
    repo_root: Path,
    with_conflict_markers: bool = False,
) -> FixApplyResult:
    root = repo_root.resolve()
    with RunFileLock(root / FIX_WORKTREE_DIR / "apply"):
        recover_apply_journal(root, line)
        plan = plan_fix_apply(store, parent, line, repo_root=root)
        conflicts = plan.conflicts
        if conflicts and not with_conflict_markers:
            return FixApplyResult(line, False, plan)
        unmarkable = tuple(item.path for item in conflicts if item.content is None)
        if unmarkable:
            raise FixLineError(
                "These conflicts can't carry conflict markers; keep fixing",
                files=unmarkable,
            )
        items = [item for item in plan.files if item.outcome != "unchanged"]
        journal = _write_all(root, line, items)
        applied = {
            "applied_revision_id": plan.revision_id,
            "applied_files": [item.path for item in items],
            "applied_at": utc_now(),
        }
        original_line = line
        try:
            if conflicts:
                updated = _set_state(store, line, "applied_with_conflicts", **applied)
            else:
                updated = _set_state(store, line, "applied", **applied)
                _finish_child(store, updated, outcome="applied")
        except BaseException:
            # Filesystem state is not durable until Review state acknowledges it.
            recover_apply_journal(root, original_line)
            raise
        journal.unlink(missing_ok=True)
        if not conflicts:
            _cleanup_fix_checkout(root, updated)
        return FixApplyResult(updated, True, plan)


def _dirty_files(worktree: Path) -> tuple[str, ...]:
    out: list[str] = []
    for row in _git(worktree, "status", "--porcelain", "--untracked-files=all").splitlines():
        if not row.strip():
            continue
        value = row[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        out.append(value)
    return tuple(out)


def close_fix_line(
    store: ReviewStore,
    line: FixLine,
    *,
    repo_root: Path,
    outcome: Literal["applied", "abandoned"],
    force: bool = False,
) -> FixLine:
    root = repo_root.resolve()
    with RunFileLock(root / FIX_WORKTREE_DIR / "apply"):
        recover_apply_journal(root, line)
        if line.status not in OPEN_FIX_STATES:
            raise FixLineError(f"This fix line is already {line.status.replace('_', ' ')}")
        path = Path(line.worktree_path)
        if path.is_dir() and not force:
            dirty = _dirty_files(path)
            if dirty:
                raise FixLineDirty("The fix worktree has uncommitted work", files=dirty)
        closed = _set_state(store, line, outcome)
        _finish_child(store, closed, outcome=outcome)
        _cleanup_fix_checkout(root, closed)
        return closed


def resolve_fix_line(
    store: ReviewStore,
    line: FixLine,
    *,
    repo_root: Path,
    force: bool = False,
) -> FixLine:
    root = repo_root.resolve()
    if line.status != "applied_with_conflicts":
        raise FixLineError("Only a fix applied with conflicts waits for Done resolving")
    left = tuple(
        path for path in line.applied_files if (root / path).is_file() and CONFLICT_MARKER in (root / path).read_bytes()
    )
    if left:
        raise FixLineError("Conflict markers are still in these files", files=left)
    return close_fix_line(store, line, repo_root=root, outcome="applied", force=force)


def recreate_fix_worktree(
    store: ReviewStore,
    line: FixLine,
    *,
    repo_root: Path,
) -> FixLine:
    """Recreate the missing worktree at the latest captured fix Snapshot.

    The branch alone is not enough: a fix Snapshot may contain captured but
    uncommitted edits. Rebuild from the immutable child Snapshot so Recreate
    cannot silently roll the fixer backward.
    """

    from lemoncrow.pro.capabilities.review import snapshot as snapshot_module

    root = repo_root.resolve()
    if line.status not in OPEN_FIX_STATES:
        raise FixLineError(f"This fix line is {line.status.replace('_', ' ')}")
    path = Path(line.worktree_path)
    if path.is_dir():
        return line
    if not _git_ok(root, "show-ref", "--verify", "--quiet", f"refs/heads/{line.branch}"):
        raise FixLineError("The fix branch is gone too; discard this fix line")

    latest = store.latest_revision(line.child_review_id)
    _git(root, "worktree", "prune")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", str(path), line.branch)
    try:
        if latest is None:
            _git(path, "reset", "--hard", "-q", line.base_commit)
            return line
        if latest.capture_kind == "commit" and latest.commit_sha:
            _git(path, "reset", "--hard", "-q", latest.commit_sha)
            return line
        overlay = snapshot_module.frozen_revision_overlay(store.root, latest, store=store)
        if overlay is None:
            raise FixLineError("The latest fix revision can't be rebuilt exactly; discard this fix line")
        _git(path, "reset", "--hard", "-q", line.base_commit)
        _commit_overlay(
            path,
            overlay,
            f"Recreated fix {line.snapshot_number + 1}.{latest.revision_number}",
        )
        return line
    except BaseException:
        _git(root, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
        _git(root, "worktree", "prune", check=False)
        raise


def _changed_base_lines(
    base: bytes | None,
    fixed: bytes | None,
) -> tuple[tuple[int, int], ...] | None:
    if base is None or fixed is None:
        return None
    old = base.decode("utf-8", "replace").splitlines()
    new = fixed.decode("utf-8", "replace").splitlines()
    ranges: list[tuple[int, int]] = []
    # lc-debt: difflib is quadratic on pathological files; use git diff -U0 if needed.
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag != "equal":
            ranges.append((i1 + 1, max(i1 + 1, i2)))
    return tuple(ranges)


def fix_line_checklist(
    store: ReviewStore,
    line: FixLine,
    *,
    repo_root: Path,
) -> tuple[ChecklistItem, ...]:
    from lemoncrow.pro.capabilities.review import snapshot as snapshot_module

    root = repo_root.resolve()
    latest = store.latest_revision(line.child_review_id)
    overlay = snapshot_module.frozen_revision_overlay(store.root, latest, store=store) if latest else None
    changed: dict[str, tuple[tuple[int, int], ...] | None] = {}
    for rel, fixed in (overlay or {}).items():
        changed[rel] = _changed_base_lines(_blob_at(root, line.base_commit, rel), fixed)
    current = {item.id: item for item in store.list_annotations(line.parent_review_id)}
    items: list[ChecklistItem] = []
    for annotation in store.list_annotations_at_revision(line.parent_review_id, line.snapshot_revision_id):
        if annotation.parent_id or annotation.state == "obsolete":
            continue
        now = current.get(annotation.id, annotation)
        anchor = annotation.anchor
        ranges = changed.get(anchor.path, ())
        if now.state == "resolved":
            state: Literal["open", "touched", "resolved"] = "resolved"
        elif ranges is None or (ranges and anchor.side != "new"):
            state = "touched"
        elif any(start <= anchor.end_line and anchor.start_line <= end for start, end in ranges):
            state = "touched"
        else:
            state = "open"
        items.append(
            ChecklistItem(
                annotation.id,
                anchor.path,
                anchor.start_line,
                anchor.end_line,
                now.body[:280],
                state,
            )
        )
    return tuple(items)
