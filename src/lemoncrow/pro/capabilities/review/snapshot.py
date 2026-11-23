"""Immutable filesystem snapshots for Review runtimes.

Every executable review surface consumes this layer. A provider never receives
the mutable checkout as the reviewed revision: commit ranges materialize Git
trees directly, while staged/working-tree reviews reconstruct base + frozen
review artifacts captured when the revision was recorded.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Any, cast

from lemoncrow.pro.capabilities.review import request_reads
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.store import ReviewStore


class ReviewSnapshotUnavailable(RuntimeError):
    """The exact reviewed filesystem cannot be reconstructed safely."""


#: How :func:`materialize_review_side` starts its message when a working-tree or staged Snapshot cannot be rebuilt
#: because changed files were not captured; the rest names them and why.
NOT_CAPTURED = "the review snapshot could not be rebuilt"


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
                raise ReviewSnapshotUnavailable(f"submodule revision path escapes {sub_path}: {item.path}")
            if not candidate.is_file():
                raise ReviewSnapshotUnavailable(f"submodule revision file is unavailable: {sub_path}/{item.path}")
            payload = candidate.read_bytes()
            if len(payload) > _MAX_SUBMODULE_FILE_BYTES:
                raise ReviewSnapshotUnavailable(f"submodule revision file is too large: {sub_path}/{item.path}")
            total_bytes += len(payload)
            if total_bytes > _MAX_SUBMODULE_TOTAL_BYTES:
                raise ReviewSnapshotUnavailable("dirty submodule revision exceeds the review size limit")
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
        raise ReviewSnapshotUnavailable("review revision scope must stay inside the repository")
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

    return _frozen_overlay(store_root, revision, scope=scope, store=store)[0]


def _frozen_overlay(
    store_root: Path,
    revision: ReviewRevision,
    *,
    scope: str,
    store: ReviewStore | None,
) -> tuple[dict[str, bytes | None] | None, tuple[str, ...]]:
    """:func:`frozen_revision_overlay`, and the changed paths in scope whose bytes were not captured.

    ``(None, ())`` is a record that cannot be read at all; ``(None, paths)`` names every file the overlay lacks.
    """

    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    requested_scope = _scope_value(scope)
    artifact_store = _artifact_store(store_root, store)
    packet = read_packet_json(artifact_store, revision)
    blobs = artifact_store.read_blob_artifact(revision.review_id, revision.id)
    media_blobs = artifact_store.read_media_artifact(revision.review_id, revision.id) or {}
    if packet is None or blobs is None:
        return None, ()
    entries = packet.get("files")
    if not isinstance(entries, list):
        return None, ()

    overlay: dict[str, bytes | None] = {}
    missing: list[str] = []
    for raw in entries:
        if not isinstance(raw, Mapping):
            return None, ()
        path = str(raw.get("path") or "")
        rel = PurePosixPath(path)
        if not path or rel.is_absolute() or ".." in rel.parts:
            return None, ()
        status = str(raw.get("status") or "modified")
        old_path = str(raw.get("old_path") or "")
        old_in_scope = False
        if old_path:
            old_rel = PurePosixPath(old_path)
            if old_rel.is_absolute() or ".." in old_rel.parts:
                return None, ()
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
                missing.append(path)
                continue
            overlay[path] = media_payload
            continue
        text = blobs.get(path)
        if text is None:
            missing.append(path)
            continue
        overlay[path] = text.encode("utf-8")
    if missing:
        return None, tuple(missing)
    return overlay, ()


def _byte_words(count: int) -> str:
    for unit, shift in (("MB", 20), ("KB", 10)):
        if count >= 1 << shift:
            return f"{count / (1 << shift):.0f} {unit}"
    return f"{count} bytes"


def _not_captured(repo_root: Path, missing: tuple[str, ...]) -> str:
    """Which changed files a working-tree Snapshot lacks, and why, as the message of its failure.

    A file still over the capture limit on disk says so with both sizes; any other is named as not captured.
    """

    if not missing:
        return f"{NOT_CAPTURED}: its record of the changed files could not be read"
    from lemoncrow.pro.capabilities.review.gitdiff import MAX_CAPTURE_BLOB_BYTES

    limit = MAX_CAPTURE_BLOB_BYTES
    named: list[str] = []
    for path in missing[:3]:
        try:
            size = (repo_root / path).stat().st_size
        except OSError:
            size = -1
        over = f" ({_byte_words(size)}, over the {_byte_words(limit)} capture limit)" if size > limit else ""
        named.append(path + over)
    rest = len(missing) - len(named)
    listed = ", ".join(named) + (f" and {rest} more" if rest > 0 else "")
    return f"{NOT_CAPTURED}: {listed} {'was' if len(missing) == 1 else 'were'} not captured"


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

    pointers = _packet_submodule_pointers(store_root, revision, store=store)
    pointer = pointers.get(scope)
    if pointer is None:
        return None
    return pointer[0 if side == "old" else 1]


# (store root, revision id, packet digest) -> {path: (old, new)}. The packet is
# immutable per digest; parsing it once per file (tens of ms each) made reading
# every changed file of a large Snapshot cost seconds.
_SUBMODULE_POINTERS: OrderedDict[tuple[str, str, str], dict[str, tuple[str, str]]] = OrderedDict()
_SUBMODULE_POINTERS_LOCK = threading.Lock()


def _packet_submodule_pointers(
    store_root: Path, revision: ReviewRevision, *, store: ReviewStore | None = None
) -> dict[str, tuple[str, str]]:
    digest = str(getattr(revision, "packet_sha256", "") or "")
    key = (str(store.root if store is not None else store_root), str(getattr(revision, "id", "")), digest)
    with _SUBMODULE_POINTERS_LOCK:
        cached = _SUBMODULE_POINTERS.get(key) if digest else None
        if cached is not None:
            _SUBMODULE_POINTERS.move_to_end(key)
            return cached

    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    packet = read_packet_json(_artifact_store(store_root, store), revision)
    entries = packet.get("files") if isinstance(packet, Mapping) else None
    pointers: dict[str, tuple[str, str]] = {}
    for raw in entries if isinstance(entries, list) else ():
        if not isinstance(raw, Mapping):
            continue
        path = str(raw.get("path") or "")
        pointer = raw.get("submodule_pointer")
        if path and path not in pointers and isinstance(pointer, (list, tuple)) and len(pointer) == 2:
            pointers[path] = (str(pointer[0] or ""), str(pointer[1] or ""))
    if packet is not None and digest and key[1]:
        # An unreadable packet or an undigested revision is never remembered.
        with _SUBMODULE_POINTERS_LOCK:
            _SUBMODULE_POINTERS[key] = pointers
            while len(_SUBMODULE_POINTERS) > 64:
                _SUBMODULE_POINTERS.popitem(last=False)
    return pointers


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


_GIT_LIST_TIMEOUT_SECONDS = 120


def _git_object_env() -> dict[str, str]:
    # Read objects from exactly the repository pygit2 opened (no inherited
    # GIT_DIR/GIT_OBJECT_DIRECTORY redirection), and never lazily fetch a
    # partial clone's missing object from its promisor remote.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _feed_and_close(stream: IO[bytes], payload: bytes) -> None:
    try:
        stream.write(payload)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def _unsafe_tree_path(rel: str) -> bool:
    """Whether a Git tree path could leave the target or plant repository metadata.

    Git itself accepts ``..``/``.git`` tree entries when ``transfer.fsckObjects``
    is off, so a fetched PR head can carry them.
    """

    if not rel or "\0" in rel or "\\" in rel or rel.startswith("/"):
        return True
    return any(part in {"", ".", ".."} or part.lower() == ".git" for part in rel.split("/"))


def _write_tree_file(root: Path, rel: str, payload: bytes, mode: int, made: set[Path]) -> None:
    """Write one materialized blob strictly inside *root*, never through a symlink."""

    if _unsafe_tree_path(rel):
        raise ReviewSnapshotUnavailable(f"Git tree contains an unsafe path: {rel!r}")
    destination = root / rel
    parent = destination.parent
    if parent not in made:
        # Resolve before creating anything: a symlink already in the target
        # must not let mkdir build directories outside it.
        resolved = parent.resolve()
        if resolved != root and root not in resolved.parents:
            raise ReviewSnapshotUnavailable(f"Git tree contains an unsafe path: {rel!r}")
        parent.mkdir(parents=True, exist_ok=True)
        made.add(parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(destination, flags, 0o666)
    except OSError as exc:
        if destination.is_symlink():
            raise ReviewSnapshotUnavailable(f"Git tree contains an unsafe path: {rel!r}") from exc
        raise
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        if mode & stat.S_IXUSR:
            os.fchmod(handle.fileno(), os.fstat(handle.fileno()).st_mode | stat.S_IXUSR)


def _stream_git_tree(repo: Any, tree_oid: str, prefix: str, target: Path) -> bool:
    """Write every blob under *tree_oid* with one ``ls-tree`` and one ``cat-file --batch``.

    Same file set and bytes as the pygit2 walk (raw objects with no clean/smudge
    filters, gitlinks skipped, symlinks written as plain files) but several
    times faster, because git inflates packed objects with its delta-base
    cache. Returns False, leaving the pygit2 walk to (re)write everything, when
    git is unavailable or the tree is incomplete locally.
    """

    git = shutil.which("git")
    git_dir = str(getattr(repo, "path", "") or "")
    if not git or not git_dir:
        return False
    env = _git_object_env()
    try:
        listing = subprocess.run(
            [git, f"--git-dir={git_dir}", "ls-tree", "-r", "-z", tree_oid],
            capture_output=True,
            env=env,
            timeout=_GIT_LIST_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if listing.returncode != 0:
        return False
    rows: list[tuple[int, bytes, str]] = []
    for record in listing.stdout.split(b"\0"):
        if not record:
            continue
        meta, _tab, raw_path = record.partition(b"\t")
        fields = meta.split(b" ")
        if len(fields) != 3 or fields[1] != b"blob":
            continue  # a gitlink is a submodule pointer, not a file
        rel = os.fsdecode(raw_path)
        rows.append((int(fields[0], 8), fields[2], f"{prefix}/{rel}" if prefix else rel))
    if not rows:
        return True

    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [git, f"--git-dir={git_dir}", "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        assert proc.stdin is not None and proc.stdout is not None
        feeder = threading.Thread(
            target=_feed_and_close,
            args=(proc.stdin, b"".join(oid + b"\n" for _mode, oid, _rel in rows)),
            daemon=True,
        )
        feeder.start()
        out = proc.stdout
        made: set[Path] = set()
        root = target.resolve()
        for mode, oid, rel in rows:
            header = out.readline().split()
            if len(header) == 2 and header[0] == oid and header[1] == b"missing":
                continue  # the pygit2 walk skips objects absent from a shallow/partial clone too
            if len(header) != 3 or header[0] != oid or header[1] != b"blob":
                return False
            size = int(header[2])
            payload = out.read(size)
            if len(payload) != size or out.read(1) != b"\n":
                return False
            _write_tree_file(root, rel, payload, mode, made)
        feeder.join()
        return proc.wait() == 0
    except (OSError, ValueError):
        return False
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()


def materialize_git_revision(repo_root: Path, sha: str, target: Path, *, scope: str = ".") -> None:
    from lemoncrow.pro.capabilities.review.gitdiff import _open_repo, _tree_for_sha

    requested_scope = _scope_value(scope)
    repo = _open_repo(repo_root)
    tree = _tree_for_sha(repo, sha)
    target.mkdir(parents=True, exist_ok=True)
    scoped, mode, _oid = _scoped_tree_object(repo, tree, requested_scope)
    if scoped is None:
        return
    prefix = "" if requested_scope == "." else requested_scope
    if getattr(scoped, "type_str", "") == "tree" and _stream_git_tree(repo, str(scoped.id), prefix, target):
        return
    rows: Iterable[tuple[str, Any, int]]
    if requested_scope == ".":
        rows = _walk_tree(repo, scoped)
    elif getattr(scoped, "type_str", "") == "tree":
        rows = _walk_tree(repo, scoped, requested_scope)
    else:
        rows = ((requested_scope, scoped, mode),)
    root = target.resolve()
    made: set[Path] = set()
    for rel, blob, mode in rows:
        _write_tree_file(root, rel, bytes(blob.data), mode, made)


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

    def read() -> dict[str, dict[str, int | str]] | None:
        try:
            return store.read_source_tree_artifact(review_id, revision_id, side=side)
        except TypeError as exc:
            if side != "new" or "unexpected keyword argument 'side'" not in str(exc):
                raise
            return store.read_source_tree_artifact(review_id, revision_id)

    return cast(
        "dict[str, dict[str, int | str]] | None",
        request_reads.source_tree_once(store, review_id, revision_id, side, read),
    )


def _materialize_source_tree_snapshot(
    store: ReviewStore,
    revision: ReviewRevision,
    target: Path,
    *,
    scope: str,
    side: str,
    unavailable: list[str] | None = None,
) -> bool:
    """Materialize a hosted revision side from its durable content-addressed tree.

    A file whose bytes the content store does not hold fails the side, unless the
    caller passes ``unavailable``: it then collects those paths and the rest of the
    tree is written. Bytes that are present but wrong always fail.
    """

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
        if payload is None:
            if unavailable is None:
                raise ReviewSnapshotUnavailable(f"hosted Review source blob is unavailable: {rel}")
            unavailable.append(rel)
            continue
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ReviewSnapshotUnavailable(f"hosted Review source blob does not match its digest: {rel}")
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
    unavailable: list[str] | None = None,
) -> str:
    """Materialize one exact review side and return its immutable identity.

    ``unavailable`` is for a reader that can work without some files (Ask): a file
    of the stored source tree with no stored bytes is listed there and left out.
    Anything that executes the tree omits it, so an incomplete tree still fails.
    """

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
            raise ReviewSnapshotUnavailable(f"frozen submodule revision is unavailable: {requested_scope}")
        project_root = target / requested_scope
        project_root.mkdir(parents=True, exist_ok=True)
        confined = target.resolve()
        for rel, (file_bytes, mode) in files.items():
            destination = (project_root / rel).resolve()
            if destination != confined and confined not in destination.parents:
                raise ReviewSnapshotUnavailable("submodule revision contains an unsafe path")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(file_bytes)
            if mode & stat.S_IXUSR:
                destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
        return f"submodule:{nested['base_sha']}:{nested['fingerprint']}:{side}"

    if store is not None and _materialize_source_tree_snapshot(
        store, revision, target, scope=requested_scope, side=side, unavailable=unavailable
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

    overlay, missing = _frozen_overlay(store_root, revision, scope=requested_scope, store=store)
    if overlay is None:
        raise ReviewSnapshotUnavailable(_not_captured(repo_root, missing))
    materialize_git_revision(repo_root, revision.base_sha, target, scope=requested_scope)
    root = target.resolve()
    for rel, overlay_payload in overlay.items():
        destination = (target / rel).resolve()
        if destination != root and root not in destination.parents:
            raise ReviewSnapshotUnavailable("review revision contains an unsafe path")
        if overlay_payload is None:
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(overlay_payload)
    return revision.source_fingerprint or revision.tree_fingerprint or revision.id


def revision_side_identity(revision: ReviewRevision, side: str) -> str:
    """Content identity of one Snapshot side, known before materializing it.

    Two Snapshots with the same identity for a side have byte-identical content
    on that side, so anything derived only from that content can be shared. A
    side without a stable identity falls back to the Snapshot id (no sharing).
    """

    if side == "old":
        return revision.base_sha or f"{revision.id}:old"
    if revision.range_mode == "commit_range":
        return revision.head_sha or revision.id
    return revision.source_fingerprint or revision.tree_fingerprint or revision.id


# Whole-side trees are ~the repository size each; a handful covers the Snapshots
# a reviewer is actively moving between.
SNAPSHOT_SOURCES_KEEP = 12


def cached_review_side(
    cache_root: Path,
    store_root: Path,
    repo_root: Path,
    revision: ReviewRevision,
    *,
    side: str,
    store: ReviewStore | None = None,
) -> Path:
    """Return a shared materialization of one whole Snapshot side.

    Keyed by content identity, so every Snapshot whose side holds the same bytes
    (usually the old side, at the Review base) shares one tree instead of each
    Snapshot keeping its own copy. Readers treat it as read-only. It is a cache:
    idle entries are retired and the next caller materializes again from the
    base commit plus the Snapshot's frozen changed files.
    """

    from lemoncrow.core.foundation.run_file_io import RunFileLock
    from lemoncrow.pro.capabilities.review.scratch_cache import (
        HEAVY_TTL_SECONDS,
        IDLE_GRACE_SECONDS,
        prune_cache_entries,
        retire_tree,
        touch_cache_entry,
    )

    expected = revision_side_identity(revision, side)
    payload = json.dumps({"identity": expected, "side": side}, sort_keys=True)
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    entry = cache_root / key
    tree = entry / "tree"
    marker = entry / "identity"
    touch_cache_entry(entry)
    # A side is immutable: materialize it once under the lock and share it.
    # Re-materializing per request would delete a tree a concurrent reader uses.
    with RunFileLock(entry / "materialize"):
        try:
            current = marker.read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        if current != expected or not tree.is_dir():
            marker.unlink(missing_ok=True)
            retire_tree(tree, trash_dir=cache_root)
            try:
                identity = materialize_review_side(store_root, repo_root, revision, side=side, target=tree, store=store)
            except BaseException:
                retire_tree(tree, trash_dir=cache_root)
                raise
            marker.write_text(identity + "\n", encoding="utf-8")
    prune_cache_entries(
        cache_root,
        entry,
        keep_count=SNAPSHOT_SOURCES_KEEP,
        idle_seconds=IDLE_GRACE_SECONDS,
        ttl_seconds=HEAVY_TTL_SECONDS,
    )
    return tree


_LEGACY_SIDE_NAMES = frozenset({"old", "new", "workspace"})
_LEGACY_SWEEP_INTERVAL_SECONDS = 3600.0
_LEGACY_SWEPT_AT: dict[str, float] = {}
_LEGACY_GUARD = threading.Lock()


def _has_legacy_snapshot_shape(review_dir: Path) -> bool:
    try:
        snapshots = list(review_dir.iterdir())
        if not snapshots:
            return False
        for snapshot_dir in snapshots:
            if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
                return False
            sides = list(snapshot_dir.iterdir())
            if not sides or any(
                item.name not in _LEGACY_SIDE_NAMES or item.is_symlink() or not item.is_dir() for item in sides
            ):
                return False
    except OSError:
        return False
    return True


def retire_legacy_snapshot_sources(roots: Iterable[Path]) -> None:
    """Retire per-Snapshot source trees left by the layout before ``cached_review_side``.

    Surfaces and dependency review each wrote ``<root>/<review>/<snapshot>/<side>``
    and nothing removed them. Only directories with exactly that shape (every
    child a Snapshot directory holding only ``old``/``new``/``workspace``) and
    idle past the grace are retired; other users of a shared scratch root keep
    their own names and shapes. Each root is swept at most once an hour.
    """

    from lemoncrow.pro.capabilities.review.scratch_cache import IDLE_GRACE_SECONDS, retire_tree

    now = time.time()
    for root in roots:
        with _LEGACY_GUARD:
            if now - _LEGACY_SWEPT_AT.get(str(root), 0.0) < _LEGACY_SWEEP_INTERVAL_SECONDS:
                continue
            _LEGACY_SWEPT_AT[str(root)] = now
        try:
            candidates = [item for item in root.iterdir() if not item.name.startswith(".")]
        except OSError:
            continue
        for review_dir in candidates:
            try:
                if review_dir.is_symlink() or not review_dir.is_dir():
                    continue
                if now - review_dir.stat().st_mtime < IDLE_GRACE_SECONDS:
                    continue
            except OSError:
                continue
            if _has_legacy_snapshot_shape(review_dir):
                retire_tree(review_dir)


__all__ = [
    "ReviewSnapshotUnavailable",
    "cached_review_side",
    "frozen_revision_overlay",
    "materialize_git_revision",
    "materialize_review_side",
    "retire_legacy_snapshot_sources",
    "review_submodule_files",
    "revision_side_identity",
    "submodule_files_at_parent_revision",
]
