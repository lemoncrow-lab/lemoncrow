"""Immutable filesystem snapshots for Review runtimes.

Every executable review surface consumes this layer. A provider never receives
the mutable checkout as the reviewed revision: commit ranges materialize Git
trees directly, while staged/working-tree reviews reconstruct base + frozen
review artifacts captured when the revision was recorded.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import shutil
import stat
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from lemoncrow.pro.capabilities.review.session_models import ReviewRevision

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.store import ReviewStore


class ReviewSnapshotUnavailable(RuntimeError):
    """The exact reviewed filesystem cannot be reconstructed safely."""


_SUBMODULE_SNAPSHOT_VERSION = 1
_MAX_SUBMODULE_FILE_BYTES = 64 * 1024 * 1024
_MAX_SUBMODULE_TOTAL_BYTES = 256 * 1024 * 1024


def capture_dirty_submodule_snapshots(repo_root: Path) -> tuple[bytes | None, str, tuple[str, ...]]:
    """Freeze dirty submodule worktrees as HEAD -> WORKDIR overlays.

    The parent packet still treats an unchanged gitlink as a pointer rather than
    pretending its nested file diffs belong to the parent repository. Rendered
    surfaces are different: a configured app inside that submodule needs exact
    bytes. This artifact is the immutable bridge between those two truths.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import (
        _open_repo,
        _submodule_dirt_is_internal,
        collect_diff,
        resolve_rev_range,
    )

    root = repo_root.expanduser().resolve()
    repo = _open_repo(root)
    try:
        submodule_paths = sorted(str(path) for path in repo.listall_submodules())
    except Exception:
        return None, "", ()

    roots: dict[str, object] = {}
    total_bytes = 0
    changed_prefixed: list[str] = []
    bundle_hash = hashlib.sha256()

    for sub_path in submodule_paths:
        if not _submodule_dirt_is_internal(repo, sub_path):
            continue
        sub_root = (root / sub_path).resolve()
        if root not in sub_root.parents or not sub_root.is_dir():
            continue
        try:
            rng = resolve_rev_range(sub_root, working_tree=True)
            diff = collect_diff(sub_root, rng, with_patch_text=False)
        except (KeyError, OSError, RuntimeError, ValueError):
            # A shallow/partial submodule can name a base object that is not
            # present locally. Nested preview is optional enrichment; inability
            # to freeze it must not make the parent review workspace crash.
            continue
        if not diff.files:
            continue

        changes: dict[str, object] = {}
        root_hash = hashlib.sha256(rng.base_sha.encode("utf-8"))
        for item in diff.files:
            if item.submodule_pointer is not None:
                # Nested submodules can be added later with recursive artifacts;
                # never smuggle today's mutable nested checkout into this one.
                raise ReviewSnapshotUnavailable(
                    f"dirty nested submodule inside {sub_path} cannot be frozen safely yet: {item.path}"
                )
            if item.status == "renamed" and item.old_path:
                changes[item.old_path] = {"deleted": True}
                root_hash.update(f"D\0{item.old_path}\0".encode())
            if item.status == "deleted":
                changes[item.path] = {"deleted": True}
                root_hash.update(f"D\0{item.path}\0".encode())
                changed_prefixed.append(f"{sub_path}/{item.path}")
                continue

            candidate = (sub_root / item.path).resolve()
            if candidate != sub_root and sub_root not in candidate.parents:
                raise ReviewSnapshotUnavailable(f"submodule snapshot path escapes {sub_path}: {item.path}")
            if not candidate.is_file():
                raise ReviewSnapshotUnavailable(f"submodule snapshot file is unavailable: {sub_path}/{item.path}")
            payload = candidate.read_bytes()
            if len(payload) > _MAX_SUBMODULE_FILE_BYTES:
                raise ReviewSnapshotUnavailable(f"submodule snapshot file is too large: {sub_path}/{item.path}")
            total_bytes += len(payload)
            if total_bytes > _MAX_SUBMODULE_TOTAL_BYTES:
                raise ReviewSnapshotUnavailable("dirty submodule snapshot exceeds the review size limit")
            mode = stat.S_IMODE(candidate.stat().st_mode)
            digest = hashlib.sha256(payload).hexdigest()
            changes[item.path] = {
                "deleted": False,
                "mode": mode,
                "sha256": digest,
                "data": base64.b64encode(payload).decode("ascii"),
            }
            root_hash.update(f"F\0{item.path}\0{mode:o}\0{digest}\0".encode())
            changed_prefixed.append(f"{sub_path}/{item.path}")

        fingerprint = root_hash.hexdigest()
        roots[sub_path] = {
            "base_sha": rng.base_sha,
            "fingerprint": fingerprint,
            "changes": changes,
        }
        bundle_hash.update(f"{sub_path}\0{fingerprint}\n".encode())

    if not roots:
        return None, "", ()
    document = {"version": _SUBMODULE_SNAPSHOT_VERSION, "roots": roots}
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, mtime=0), bundle_hash.hexdigest(), tuple(sorted(set(changed_prefixed)))


def decode_submodule_snapshots(payload: bytes | None) -> dict[str, dict[str, Any]]:
    if not payload:
        return {}
    try:
        document = json.loads(gzip.decompress(payload).decode("utf-8"))
    except (OSError, EOFError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(document, dict) or document.get("version") != _SUBMODULE_SNAPSHOT_VERSION:
        return {}
    roots = document.get("roots")
    if not isinstance(roots, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for raw_root, raw in roots.items():
        root = str(raw_root)
        rel = PurePosixPath(root)
        if not root or rel.is_absolute() or ".." in rel.parts or not isinstance(raw, dict):
            return {}
        base_sha = str(raw.get("base_sha") or "")
        fingerprint = str(raw.get("fingerprint") or "")
        changes = raw.get("changes")
        if not base_sha or not fingerprint or not isinstance(changes, dict):
            return {}
        clean_changes: dict[str, dict[str, Any]] = {}
        for raw_path, change in changes.items():
            path = str(raw_path)
            path_rel = PurePosixPath(path)
            if not path or path_rel.is_absolute() or ".." in path_rel.parts or not isinstance(change, dict):
                return {}
            clean_changes[path] = dict(change)
        out[root] = {"base_sha": base_sha, "fingerprint": fingerprint, "changes": clean_changes}
    return out


def _artifact_store(store_root: Path, store: ReviewStore | None = None) -> ReviewStore:
    if store is not None:
        return store
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    return ReviewStore(store_root)


def frozen_submodule_snapshots(
    store_root: Path,
    revision: ReviewRevision,
    *,
    store: ReviewStore | None = None,
) -> dict[str, dict[str, Any]]:
    artifact_store = _artifact_store(store_root, store)
    return decode_submodule_snapshots(artifact_store.read_submodule_artifact(revision.review_id, revision.id))


def frozen_submodule_changed_paths(
    store_root: Path,
    revision: ReviewRevision,
    *,
    store: ReviewStore | None = None,
) -> tuple[str, ...]:
    snapshots = frozen_submodule_snapshots(store_root, revision, store=store)
    paths: list[str] = []
    for root, snapshot in snapshots.items():
        for rel in snapshot["changes"]:
            paths.append(f"{root}/{rel}")
    return tuple(sorted(set(paths)))


def _submodule_tree_bytes(repo_root: Path, root: str, base_sha: str) -> dict[str, tuple[bytes, int]]:
    from lemoncrow.pro.capabilities.review.gitdiff import _open_repo, _tree_for_sha

    sub_root = (repo_root / root).resolve()
    repo = _open_repo(sub_root)
    tree = _tree_for_sha(repo, base_sha)
    out: dict[str, tuple[bytes, int]] = {}
    for rel, blob, mode in _walk_tree(repo, tree):
        out[rel] = (bytes(blob.data), mode)
    return out


def frozen_submodule_files(
    store_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    root: str,
    *,
    side: str,
    store: ReviewStore | None = None,
) -> dict[str, tuple[bytes, int]] | None:
    if side not in {"old", "new"}:
        return None
    snapshot = frozen_submodule_snapshots(store_root, revision, store=store).get(root)
    if snapshot is None:
        return None
    try:
        files = _submodule_tree_bytes(repo_root, root, str(snapshot["base_sha"]))
    except (OSError, RuntimeError, ValueError):
        return None
    if side == "old":
        return files
    for rel, change in snapshot["changes"].items():
        if bool(change.get("deleted")):
            files.pop(rel, None)
            continue
        try:
            payload = base64.b64decode(str(change.get("data") or ""), validate=True)
            mode = int(change.get("mode") or 0o644)
        except (ValueError, TypeError):
            return None
        if hashlib.sha256(payload).hexdigest() != str(change.get("sha256") or ""):
            return None
        files[rel] = (payload, mode)
    return files


def submodule_files_at_parent_revision(
    repo_root: Path, parent_sha: str, root: str
) -> dict[str, tuple[bytes, int]] | None:
    """Read a clean configured submodule directly from the parent gitlink."""

    if not parent_sha:
        return None
    from lemoncrow.pro.capabilities.review.gitdiff import _open_repo, _tree_for_sha

    repo = _open_repo(repo_root)
    tree = _tree_for_sha(repo, parent_sha)
    _obj, mode, oid = _scoped_tree_object(repo, tree, root)
    if mode != 0o160000 or not oid:
        return None
    try:
        return _submodule_tree_bytes(repo_root, root, oid)
    except (KeyError, OSError, RuntimeError, ValueError):
        return None


def review_submodule_files(
    store_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    root: str,
    *,
    side: str,
    store: ReviewStore | None = None,
) -> dict[str, tuple[bytes, int]] | None:
    """Read one exact submodule side, whether clean or frozen-dirty."""

    if root in frozen_submodule_snapshots(store_root, revision, store=store):
        return frozen_submodule_files(store_root, repo_root, revision, root, side=side, store=store)
    pointer = _revision_submodule_pointer(store_root, repo_root, revision, root, side=side, store=store)
    if not pointer:
        return None
    try:
        return _submodule_tree_bytes(repo_root, root, pointer)
    except (KeyError, OSError, RuntimeError, ValueError):
        return None


def review_submodule_changed_paths(
    store_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    root: str,
    *,
    store: ReviewStore | None = None,
) -> tuple[str, ...]:
    """Return exact nested changed paths for a clean parent gitlink bump.

    Dirty working-tree submodules already carry a frozen overlay artifact. A
    commit-range submodule change instead stores only the parent gitlink pair,
    so surface discovery has to compare the exact old/new submodule trees.
    Refuse enrichment when either tree is unavailable rather than borrowing the
    currently checked-out submodule.
    """

    old = review_submodule_files(store_root, repo_root, revision, root, side="old", store=store)
    new = review_submodule_files(store_root, repo_root, revision, root, side="new", store=store)
    if old is None or new is None:
        return ()
    changed = [f"{root}/{rel}" for rel in sorted(set(old) | set(new)) if old.get(rel) != new.get(rel)]
    return tuple(changed)


def _scope_value(scope: str) -> str:
    raw = (scope or ".").strip().replace("\\", "/") or "."
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ReviewSnapshotUnavailable("review snapshot scope must stay inside the repository")
    return path.as_posix()


def _path_in_scope(path: str, scope: str) -> bool:
    return scope == "." or path == scope or path.startswith(scope + "/")


def frozen_revision_overlay(
    store_root: Path,
    revision: ReviewRevision,
    *,
    scope: str = ".",
    store: ReviewStore | None = None,
) -> dict[str, bytes | None] | None:
    """Return exact changed-file bytes for one frozen revision scope.

    Runtime roots are independent execution boundaries. A binary/doc change
    elsewhere in a monorepo must not make an otherwise-complete frontend/API
    snapshot unavailable, while a missing blob *inside* the requested scope
    still fails closed.
    """

    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    requested_scope = _scope_value(scope)
    artifact_store = _artifact_store(store_root, store)
    packet = read_packet_json(artifact_store, revision)
    blobs = artifact_store.read_blob_artifact(revision.review_id, revision.id)
    media_blobs = artifact_store.read_media_artifact(revision.review_id, revision.id) or {}
    if packet is None or blobs is None:
        return None
    entries = packet.get("files")
    if not isinstance(entries, list):
        return None

    overlay: dict[str, bytes | None] = {}
    for raw in entries:
        if not isinstance(raw, Mapping):
            return None
        path = str(raw.get("path") or "")
        rel = PurePosixPath(path)
        if not path or rel.is_absolute() or ".." in rel.parts:
            return None
        status = str(raw.get("status") or "modified")
        old_path = str(raw.get("old_path") or "")
        old_in_scope = False
        if old_path:
            old_rel = PurePosixPath(old_path)
            if old_rel.is_absolute() or ".." in old_rel.parts:
                return None
            old_in_scope = _path_in_scope(old_path, requested_scope)
        path_in_scope = _path_in_scope(path, requested_scope)
        if not path_in_scope and not old_in_scope:
            continue
        if raw.get("submodule_pointer") is not None:
            # A gitlink is a revision pointer, not a file blob. Root-level
            # materialization deliberately omits submodule contents; a runtime
            # scoped to that submodule is handled by the exact nested/pointer
            # branches in materialize_review_side(). Requiring blob bytes here
            # therefore makes an unrelated parent runtime look incomplete.
            continue
        if status == "deleted":
            if path_in_scope:
                overlay[path] = None
            continue
        if status == "renamed" and old_path and old_in_scope:
            overlay[old_path] = None
        if not path_in_scope:
            continue
        if bool(raw.get("is_binary")):
            media_payload = media_blobs.get(path)
            if media_payload is None:
                return None
            overlay[path] = media_payload
            continue
        text = blobs.get(path)
        if text is None:
            return None
        overlay[path] = text.encode("utf-8")
    return overlay


def _walk_tree(repo: Any, tree: Any, prefix: str = "") -> Iterator[tuple[str, Any, int]]:
    for entry in tree:
        path = f"{prefix}/{entry.name}" if prefix else entry.name
        mode = int(getattr(entry, "filemode", 0) or 0)
        if mode == 0o160000:
            continue
        try:
            obj = repo[entry.id]
        except KeyError:
            continue
        if getattr(obj, "type_str", "") == "tree":
            yield from _walk_tree(repo, obj, path)
        elif getattr(obj, "type_str", "") == "blob":
            yield path, obj, mode


def _scoped_tree_object(repo: Any, tree: Any, scope: str) -> tuple[Any | None, int, str]:
    """Return the exact object named by *scope* without walking sibling trees.

    Preview/runtime roots are usually one project inside a large monorepo. The
    old materializer recursively walked the whole Git tree and discarded paths
    outside the requested scope afterwards, which made a small frontend preview
    pay for every object in a repository. Resolve the scope path first, then walk
    only that subtree.
    """

    requested_scope = _scope_value(scope)
    if requested_scope == ".":
        return tree, 0o040000, ""
    current = tree
    parts = PurePosixPath(requested_scope).parts
    for index, part in enumerate(parts):
        entry = next((candidate for candidate in current if candidate.name == part), None)
        if entry is None:
            return None, 0, ""
        mode = int(getattr(entry, "filemode", 0) or 0)
        oid = str(getattr(entry, "id", "") or "")
        if index == len(parts) - 1:
            if mode == 0o160000:
                return None, mode, oid
            try:
                return repo[entry.id], mode, oid
            except KeyError:
                return None, mode, oid
        if mode == 0o160000:
            return None, mode, oid
        try:
            current = repo[entry.id]
        except KeyError:
            return None, mode, oid
        if getattr(current, "type_str", "") != "tree":
            return None, mode, oid
    return None, 0, ""


def _packet_submodule_pointer(
    store_root: Path,
    revision: ReviewRevision,
    scope: str,
    *,
    side: str,
    store: ReviewStore | None = None,
) -> str | None:
    """Return a recorded gitlink pointer when this review changed *scope*."""

    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    packet = read_packet_json(_artifact_store(store_root, store), revision)
    entries = packet.get("files") if isinstance(packet, Mapping) else None
    if not isinstance(entries, list):
        return None
    index = 0 if side == "old" else 1
    for raw in entries:
        if not isinstance(raw, Mapping) or str(raw.get("path") or "") != scope:
            continue
        pointer = raw.get("submodule_pointer")
        if isinstance(pointer, (list, tuple)) and len(pointer) == 2:
            return str(pointer[index] or "")
    return None


def _revision_submodule_pointer(
    store_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    scope: str,
    *,
    side: str,
    store: ReviewStore | None = None,
) -> str | None:
    """Resolve the exact gitlink commit for one reviewed side, if *scope* is a submodule."""

    recorded = _packet_submodule_pointer(store_root, revision, scope, side=side, store=store)
    if recorded is not None:
        return recorded
    parent_sha = (
        revision.base_sha
        if side == "old"
        else (revision.head_sha if revision.range_mode == "commit_range" else revision.base_sha)
    )
    if not parent_sha:
        return None
    from lemoncrow.pro.capabilities.review.gitdiff import _open_repo, _tree_for_sha

    repo = _open_repo(repo_root)
    tree = _tree_for_sha(repo, parent_sha)
    _obj, mode, oid = _scoped_tree_object(repo, tree, scope)
    return oid if mode == 0o160000 else None


def materialize_git_revision(repo_root: Path, sha: str, target: Path, *, scope: str = ".") -> None:
    from lemoncrow.pro.capabilities.review.gitdiff import _open_repo, _tree_for_sha

    requested_scope = _scope_value(scope)
    repo = _open_repo(repo_root)
    tree = _tree_for_sha(repo, sha)
    target.mkdir(parents=True, exist_ok=True)
    scoped, mode, _oid = _scoped_tree_object(repo, tree, requested_scope)
    if scoped is None:
        return
    rows: Iterable[tuple[str, Any, int]]
    if requested_scope == ".":
        rows = _walk_tree(repo, scoped)
    elif getattr(scoped, "type_str", "") == "tree":
        rows = _walk_tree(repo, scoped, requested_scope)
    else:
        rows = ((requested_scope, scoped, mode),)
    for rel, blob, mode in rows:
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(bytes(blob.data))
        if mode & stat.S_IXUSR:
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR)


def _read_source_tree_artifact(
    store: ReviewStore,
    review_id: str,
    revision_id: str,
    *,
    side: str,
) -> dict[str, dict[str, int | str]] | None:
    """Read hosted source trees across rolling upgrades of ReviewStore.

    Older ReviewStore builds only accepted ``(review_id, revision_id)`` and
    represented the new side. During a rolling/local upgrade the Reader/API can
    be newer than that installed store class. Keep new-side discovery working
    through that window; never pretend the old side exists on an old store.
    """

    try:
        return store.read_source_tree_artifact(review_id, revision_id, side=side)
    except TypeError as exc:
        if side != "new" or "unexpected keyword argument 'side'" not in str(exc):
            raise
        return store.read_source_tree_artifact(review_id, revision_id)


def _materialize_source_tree_snapshot(
    store: ReviewStore,
    revision: ReviewRevision,
    target: Path,
    *,
    scope: str,
    side: str,
) -> bool:
    """Materialize a hosted revision side from its durable content-addressed tree."""

    entries = _read_source_tree_artifact(store, revision.review_id, revision.id, side=side)
    if entries is None:
        return False
    requested_scope = _scope_value(scope)
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    prefix = "" if requested_scope == "." else requested_scope.rstrip("/") + "/"
    for rel, raw in entries.items():
        if requested_scope != "." and rel != requested_scope and not rel.startswith(prefix):
            continue
        digest = str(raw["content_digest"])
        payload = store.read_source_content(digest)
        if payload is None or hashlib.sha256(payload).hexdigest() != digest:
            raise ReviewSnapshotUnavailable(f"hosted Review source blob is unavailable: {rel}")
        expected_size = int(raw["size"])
        if len(payload) != expected_size:
            raise ReviewSnapshotUnavailable(f"hosted Review source blob size changed: {rel}")
        destination = (target / rel).resolve()
        if destination != root and root not in destination.parents:
            raise ReviewSnapshotUnavailable("hosted Review source tree contains an unsafe path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        if int(raw["mode"]) & stat.S_IXUSR:
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
    return True


def materialize_review_side(
    store_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    *,
    side: str,
    target: Path,
    scope: str = ".",
    store: ReviewStore | None = None,
) -> str:
    """Materialize one exact review side and return its immutable identity."""

    requested_scope = _scope_value(scope)
    if side not in {"old", "new"}:
        raise ReviewSnapshotUnavailable("review side must be old or new")
    if target.exists():
        shutil.rmtree(target)

    nested = frozen_submodule_snapshots(store_root, revision, store=store).get(requested_scope)
    if nested is not None:
        files = frozen_submodule_files(
            store_root,
            repo_root,
            revision,
            requested_scope,
            side=side,
            store=store,
        )
        if files is None:
            raise ReviewSnapshotUnavailable(f"frozen submodule snapshot is unavailable: {requested_scope}")
        project_root = target / requested_scope
        project_root.mkdir(parents=True, exist_ok=True)
        confined = target.resolve()
        for rel, (file_bytes, mode) in files.items():
            destination = (project_root / rel).resolve()
            if destination != confined and confined not in destination.parents:
                raise ReviewSnapshotUnavailable("submodule snapshot contains an unsafe path")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(file_bytes)
            if mode & stat.S_IXUSR:
                destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
        return f"submodule:{nested['base_sha']}:{nested['fingerprint']}:{side}"

    if store is not None and _materialize_source_tree_snapshot(
        store, revision, target, scope=requested_scope, side=side
    ):
        if side == "new":
            return revision.source_fingerprint or revision.tree_fingerprint or revision.id
        return revision.base_sha or f"{revision.id}:old"

    submodule_pointer = _revision_submodule_pointer(
        store_root,
        repo_root,
        revision,
        requested_scope,
        side=side,
        store=store,
    )
    if submodule_pointer is not None:
        if not submodule_pointer:
            raise ReviewSnapshotUnavailable(f"submodule is absent on the {side} side: {requested_scope}")
        sub_root = (repo_root / requested_scope).resolve()
        repo_resolved = repo_root.resolve()
        if repo_resolved not in sub_root.parents or not sub_root.is_dir():
            raise ReviewSnapshotUnavailable(f"submodule checkout is unavailable: {requested_scope}")
        project_root = target / requested_scope
        try:
            materialize_git_revision(sub_root, submodule_pointer, project_root)
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            raise ReviewSnapshotUnavailable(
                f"submodule revision is unavailable locally: {requested_scope}@{submodule_pointer}"
            ) from exc
        return f"submodule:{submodule_pointer}:{side}"

    if side == "old":
        if not revision.base_sha:
            raise ReviewSnapshotUnavailable("old revision tree is unavailable")
        materialize_git_revision(repo_root, revision.base_sha, target, scope=requested_scope)
        return revision.base_sha

    if revision.range_mode == "commit_range":
        if not revision.head_sha:
            raise ReviewSnapshotUnavailable("new revision tree is unavailable")
        materialize_git_revision(repo_root, revision.head_sha, target, scope=requested_scope)
        return revision.head_sha

    if revision.range_mode not in {"working_tree", "staged"} or not revision.base_sha:
        raise ReviewSnapshotUnavailable("new revision tree is unavailable")

    overlay = frozen_revision_overlay(store_root, revision, scope=requested_scope, store=store)
    if overlay is None:
        raise ReviewSnapshotUnavailable(
            "the frozen review revision is incomplete in this runtime root; refresh after its changed files can be captured"
        )
    materialize_git_revision(repo_root, revision.base_sha, target, scope=requested_scope)
    root = target.resolve()
    for rel, overlay_payload in overlay.items():
        destination = (target / rel).resolve()
        if destination != root and root not in destination.parents:
            raise ReviewSnapshotUnavailable("review snapshot contains an unsafe path")
        if overlay_payload is None:
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(overlay_payload)
    return revision.source_fingerprint or revision.tree_fingerprint or revision.id


__all__ = [
    "ReviewSnapshotUnavailable",
    "frozen_revision_overlay",
    "materialize_git_revision",
    "materialize_review_side",
    "review_submodule_files",
    "submodule_files_at_parent_revision",
]
