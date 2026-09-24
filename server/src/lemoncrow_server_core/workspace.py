"""Materializing the bound view, so the tools answer from it and not from here.

This is the join between the index and the product. Without it the server holds
a three-layer index of a tenant's code and then dispatches every tool against
whatever directory the server process happens to be running in -- which in
Mode A is not the tenant's code at all, and in any mode is not the revision the
caller is bound to.

**The design chosen, and why.** Two honest options existed:

*native* -- reimplement ``read``/``code_search``/``relations``/``graph``/
``search`` directly over Layer 1 + Layer 2 + Layer 3, reproducing the public
tools' output byte for byte;

*materialized* -- rebuild the view as a real, revision-exact tree from the
organization's own content store and run the public tools against it.

Materialized wins on three counts that are not close. First, the server-side
surface is eighteen tools, not five: ``orient``, ``rescue``, ``context``,
``trace``, ``verify``, ``compact`` and the ``review_*`` family all resolve
paths against a workspace too, so a native port that covered the five named
ones would leave the other thirteen answering from the server's own checkout --
the very defect being fixed. Second, output-format parity would become a
permanent, hand-maintained obligation against upstream tools that keep moving;
materialization makes parity a property of running the same code on the same
bytes, which is what ``tests/test_tool_parity.py`` already measures. Third,
Layer 1 stores *derived analysis* -- terms, trigrams, symbol definitions,
embeddings -- not a substitute for the bytes ``read`` returns, so a native
``read`` would have to serve content out of the CAS regardless; the only
question left is whether those bytes reach the tool through a file or a buffer,
and the tools want a file.

**What materialization must guarantee**, each of which is enforced here rather
than assumed:

*revision-exact*
    The tree is rebuilt to the membership of one committed ``view_revision``,
    and the revision is re-read on both sides of the membership read. If the
    view advanced underneath the call, the call is refused as stale instead of
    being answered from a tree that is half one revision and half another.
*per-view*
    One directory per ``(org_id, view_id)``, named by a digest of both. Nothing
    is shared between views, so no eviction or edit can leak across one.
*never cross-organization, and never across the boundary below it*
    Every byte arrives through ``ContentStore.get(org_id, digest)``, which is
    org-scoped, and membership arrives through ``ViewStore.membership(org_id,
    view_id)``, which raises ``view_unknown`` for another organization's view.
    Two organizations holding byte-identical files still get two trees, and an
    organization that has not uploaded content does not get another's copy of
    it -- it gets a blob miss.

    Inside one organization the same is true one level down. A tree is written
    only from digests the view has *demonstrated possession* of
    (:mod:`.index.boundary`), never from every digest the manifest happens to
    name: this is the one place where a manifest row turns into real bytes on a
    real filesystem that eighteen tools then read, so a row naming a
    colleague's digest must produce no file here rather than produce their
    source. A file left out for that reason is counted in ``absent_paths``,
    exactly like one whose bytes nobody ever uploaded.
*observably changed*
    A rewritten file always carries a modification time no file in this process
    has carried before. The tools keep ``(path, st_mtime_ns, st_size)``-keyed
    caches, and a view's tree lives at one stable path forever, so without this
    a same-length edit would be invisible to those caches whenever the
    filesystem's timestamp granularity could not separate two writes -- making
    read-after-edit depend on how fast the machine is. See :meth:`_write`.
*garbage-collected*
    Trees are dropped when the view closes, when its lease expires, and under
    an LRU quota. Eviction never touches a tree that could still be in use: a
    slot is only reclaimed once it has been idle for longer than the tool
    deadline, which bounds how long any in-flight call can still be reading it.
*inside whatever encryption boundary the deployment claims*
    A materialized tree is the tenant's source in cleartext -- it has to be, or
    the tools could not read it. On a deployment whose content store is sealed,
    that is the one place where "content is encrypted under a per-organization
    data key" could be false of the node, and it *was* false: the sealed store
    kept ciphertext on the state volume while this wrote every file of every
    open workspace to the workspace root in the clear.

    So a sealed deployment never materializes onto disk. If the configured root
    is memory-backed, that root is used. If it is not -- or if none was
    configured at all -- the trees are relocated to a memory-backed directory
    instead, loudly: a warning names the substitution, ``ops preflight`` reports
    it, and the chart never produces the state in the first place because
    ``encryption.enabled`` renders the workspace volume with ``medium:
    Memory``. What the relocation costs is *sizing* -- the quota is then
    enforced against a volume the operator did not choose -- and what it
    protects is confidentiality, which is the only direction a security control
    is allowed to trade in. A host with no memory-backed filesystem at all has
    no honest answer left, and is a start-up refusal.

    Without encryption the trees are ordinary cleartext scratch. That is stated
    -- by ``ops preflight`` and by the chart's own documentation -- rather than
    left to be discovered, because the claim a customer is owed is the narrow
    one that is true, not the broad one that reads better.
*visibly owned while a server is running*
    The root is held under an exclusive advisory lock for the life of the
    server. Two purposes: a second server at the same root does not reclaim the
    first's trees as though they were stale, and an offline ``ops purge`` can
    tell that a server is holding the root and refuse rather than delete trees
    out from under a live tool call. The kernel drops the lock when the holder
    dies, so a server killed by an OOM leaves no stale answer behind. See
    :func:`root_is_held`.

**No view means an empty tree, not the server's directory.** A session that has
not opened a view has nothing to answer from. Pointing such a call at the
process working directory is how ``orient`` ends up describing the server's own
checkout to a tenant, so it is pointed at an empty directory instead.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .errors import AgentAction, ErrorCode, ServerError
from .index.boundary import addressable_in
from .index.contracts import (
    IndexBackend,
    IndexLayerState,
    IndexLayerStatus,
    ManifestEntry,
    validate_path,
)

__all__ = [
    "DEFAULT_MAX_VIEWS",
    "DEFAULT_QUOTA_BYTES",
    "MEMORY_FILESYSTEMS",
    "NO_TREES",
    "ROOT_LOCK_NAME",
    "MaterializedView",
    "TreeCensus",
    "ViewMaterializer",
    "WorkspaceStatus",
    "drop_trees",
    "filesystem_type",
    "is_memory_backed",
    "materialization_roots",
    "memory_backed_shelter",
    "root_is_held",
    "slot_name",
    "tree_census",
]

_LOG: Final[logging.Logger] = logging.getLogger("lemoncrow.server.workspace")

#: Live materialized trees kept before the least recently used ones are dropped.
DEFAULT_MAX_VIEWS: Final[int] = 64

#: Total bytes of materialized trees kept before the same LRU pass runs.
DEFAULT_QUOTA_BYTES: Final[int] = 2 * 1024 * 1024 * 1024

#: Directory under the materialization root that every viewless call is bound
#: to. Named so it cannot collide with a slot digest.
_EMPTY_SLOT: Final[str] = "empty"

#: Slot directory names are 64 hex characters. Used to recognize -- and
#: reclaim -- trees left behind by a previous process at a configured root.
_SLOT_NAME_LEN: Final[int] = 64
_HEX: Final[frozenset[str]] = frozenset("0123456789abcdef")

#: Directory and file modes. The root is the server's own private scratch: it
#: holds one tenant's source next to another's, so it is never group- or
#: world-readable.
_DIR_MODE: Final[int] = 0o700
_FILE_MODE: Final[int] = 0o600

#: The lock file one server takes on its materialization root. Named with a
#: leading dot and no hex digits so neither the stale-slot sweep nor the purge
#: census can mistake it for a tree.
ROOT_LOCK_NAME: Final[str] = ".lemoncrow-views.lock"

#: Filesystems whose contents never reach the node's disk. The set is short on
#: purpose: anything not named here is treated as disk-backed, because the
#: failure being prevented is a deployment that *believes* it is sealed.
MEMORY_FILESYSTEMS: Final[frozenset[str]] = frozenset({"tmpfs", "ramfs"})

#: The kernel's own mount table. Read rather than guessed at: ``statvfs`` does
#: not carry a filesystem name, and a heuristic on the path would call
#: ``/dev/shm`` memory-backed on a host where it is not mounted at all.
_MOUNTINFO: Final[Path] = Path("/proc/self/mountinfo")


def slot_name(org_id: str, view_id: str) -> str:
    """Directory name for one view, salted by the organization that owns it.

    View ids are server-issued and already unique, so the organization term is
    not what makes this collision-free -- it is what makes a collision
    *impossible to construct* from a client-supplied identifier, and what makes
    the mapping readable as "this tree belongs to exactly one tenant".

    Public because a deletion run from an operator shell has to find the same
    directories this server writes, and a purge that re-derived the mapping
    from its own copy of this code would be one edit away from censusing an
    empty set and reporting nothing left behind.
    """
    digest = hashlib.sha256()
    digest.update(org_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(view_id.encode("utf-8"))
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Where the root actually lives                                               #
# --------------------------------------------------------------------------- #


def _nearest_existing(path: Path) -> Path:
    """``path``, or the closest ancestor that exists. Never raises."""
    candidate = Path(path).absolute()
    while True:
        try:
            return candidate.resolve(strict=True)
        except OSError:
            pass
        parent = candidate.parent
        if parent == candidate:
            return candidate
        candidate = parent


def _unescape(field: str) -> str:
    """``mountinfo`` octal-escapes space, tab, newline and backslash."""
    for escape, literal in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(escape, literal)
    return field


def filesystem_type(path: Path, *, mountinfo: Path = _MOUNTINFO) -> str:
    """The filesystem ``path`` is on, according to the kernel's mount table.

    Empty when the table cannot be read -- a non-Linux host, a container that
    hides it. Callers must treat empty as *unknown*, never as safe: the whole
    point of asking is that an unproven encryption boundary is not one.
    """
    try:
        table = mountinfo.read_text(encoding="utf-8")
    except OSError:
        return ""
    target = _nearest_existing(path)
    best_depth = -1
    best_type = ""
    for line in table.splitlines():
        head, separator, tail = line.partition(" - ")
        if not separator:
            continue
        fields = head.split()
        rest = tail.split()
        if len(fields) < 5 or not rest:
            continue
        mount_point = Path(_unescape(fields[4]))
        if target != mount_point and mount_point not in target.parents:
            continue
        # The longest matching mount point wins, which is what makes a tmpfs
        # mounted *inside* a disk-backed tree report as tmpfs rather than as
        # whatever it was mounted over.
        depth = len(mount_point.parts)
        if depth > best_depth:
            best_depth = depth
            best_type = rest[0]
    return best_type


def is_memory_backed(path: Path, *, mountinfo: Path = _MOUNTINFO) -> bool:
    """True only when what is written at ``path`` never reaches the node's disk.

    ``tmpfs`` pages can still be written to swap, so this is exactly as strong
    as the node's swap configuration -- which is why the chart documents "swap
    off" beside the setting rather than leaving it implied.
    """
    return filesystem_type(path, mountinfo=mountinfo) in MEMORY_FILESYSTEMS


#: Where a sealed deployment looks for somewhere to materialize when the root it
#: was given is on disk. Both entries are ordinary on a Linux node; neither is
#: assumed to exist, and neither is assumed to be memory-backed without asking.
_SHELTERS: Final[tuple[str, ...]] = ("/dev/shm", "/run")


def memory_backed_shelter() -> Path | None:
    """A directory a sealed deployment may materialize into, or ``None``.

    The fallback, never the design. A deployment is supposed to name its own
    sized memory-backed volume; this exists so that a deployment which did not
    fails towards keeping the tenant's source off the disk rather than towards
    writing it there, and so that the failure is a sizing problem an operator
    can see rather than a confidentiality one they cannot.
    """
    candidates = [Path(name) for name in _SHELTERS]
    candidates.append(Path(tempfile.gettempdir()))
    for candidate in candidates:
        if candidate.is_dir() and os.access(candidate, os.W_OK) and is_memory_backed(candidate):
            return candidate
    return None


def _acquire_root_lock(root: Path) -> int | None:
    """Take the root's exclusive lock, or ``None`` when somebody else holds it."""
    target = Path(root) / ROOT_LOCK_NAME
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, _FILE_MODE)
    except OSError:  # pragma: no cover - an unwritable root fails louder later
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def _release_root_lock(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - the file may already be gone
        pass
    finally:
        os.close(descriptor)


def root_is_held(root: Path) -> bool:
    """True when a live process is materializing into ``root`` right now.

    The question an offline deletion has to ask before it starts. ``flock`` is
    the right primitive because the kernel drops it when the holder dies: a
    server killed by an OOM leaves no stale answer behind, which a pid file
    would.
    """
    target = Path(root) / ROOT_LOCK_NAME
    if not target.is_file():
        return False
    try:
        descriptor = os.open(target, os.O_RDWR | os.O_NOFOLLOW)
    except OSError:  # pragma: no cover - unreadable is not evidence of a holder
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    _release_root_lock(descriptor)
    return False


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    """How completely the bound view could be reconstructed on disk.

    ``absent_paths`` is the honest part. A view may name files whose bytes the
    server does not hold: one over the content size cap, which by policy is
    manifested but never uploaded, or one whose upload has not happened. Those
    files are not in the tree, so a tool that scans the tree cannot see them,
    and an answer computed over such a tree is flagged rather than passed off
    as complete.
    """

    view_revision: int
    member_paths: int
    materialized_paths: int
    absent_paths: int
    over_cap_paths: int

    @property
    def complete(self) -> bool:
        return self.absent_paths == 0

    @property
    def reason(self) -> str:
        if self.complete:
            return ""
        if self.over_cap_paths == self.absent_paths:
            return "content_over_size_cap"
        return "content_absent"

    def as_layer_status(self) -> IndexLayerStatus:
        """The same fact in the shape every other layer reports."""
        return IndexLayerStatus(
            state=IndexLayerState.READY if self.complete else IndexLayerState.COLD,
            reason=self.reason,
            covered_paths=self.materialized_paths,
            at_view_revision=self.view_revision,
        )

    def counts(self) -> dict[str, int]:
        return {
            "member_paths": self.member_paths,
            "materialized_paths": self.materialized_paths,
            "absent_paths": self.absent_paths,
            "over_cap_paths": self.over_cap_paths,
        }


@dataclass(frozen=True, slots=True)
class MaterializedView:
    """One tree, ready for the tools, at exactly one revision."""

    root: Path
    status: WorkspaceStatus
    #: True when this call rewrote the tree rather than reusing a warm one.
    rebuilt: bool
    #: Files written and removed by this call. Zero on a warm reuse, which is
    #: what makes "the second tool call is free" checkable rather than claimed.
    written: int
    removed: int
    bytes_written: int


@dataclass(slots=True)
class _Slot:
    """Mutable bookkeeping for one materialized tree."""

    org_id: str
    view_id: str
    tree: Path
    lock: threading.Lock
    #: Path -> content digest actually on disk. The tree is exactly this map.
    on_disk: dict[str, str]
    #: Revision the map corresponds to; ``-1`` means a write was interrupted
    #: and the next materialization must rebuild from scratch.
    at_revision: int
    bytes_on_disk: int
    last_used: float
    status: WorkspaceStatus


class ViewMaterializer:
    """Reconstructs a bound view as a real tree the public tools can read."""

    __slots__ = (
        "_backend",
        "_clock",
        "_empty",
        "_grace_s",
        "_lock",
        "_lock_fd",
        "_max_views",
        "_owns_root",
        "_quota_bytes",
        "_relocated_from",
        "_root",
        "_sealed",
        "_size_cap",
        "_slots",
        "_stamp",
        "_stamp_lock",
        "_stamp_source",
    )

    def __init__(
        self,
        backend: IndexBackend,
        *,
        root: Path | None = None,
        content_size_cap: int,
        max_views: int = DEFAULT_MAX_VIEWS,
        quota_bytes: int = DEFAULT_QUOTA_BYTES,
        eviction_grace_s: float = 120.0,
        clock: Callable[[], float] = time.time,
        stat_clock: Callable[[], int] = time.time_ns,
    ) -> None:
        if max_views < 1:
            raise ValueError("max_views must be >= 1")
        if quota_bytes < 1:
            raise ValueError("quota_bytes must be >= 1")
        if eviction_grace_s < 0:
            raise ValueError("eviction_grace_s must not be negative")
        self._backend = backend
        self._size_cap = content_size_cap
        self._max_views = max_views
        self._quota_bytes = quota_bytes
        self._grace_s = eviction_grace_s
        self._clock = clock
        self._stamp_source = stat_clock
        self._stamp = 0
        self._stamp_lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self._lock = threading.Lock()
        # Asked of the store rather than of the configuration, so the posture
        # this enforces is the posture the bytes actually have: the encrypted
        # content store is the only one that carries a key ring, and it is the
        # only one whose presence makes "the content is sealed" a sentence this
        # deployment is saying.
        self._sealed = getattr(backend.content, "keyring", None) is not None
        self._relocated_from: Path | None = None
        shelter: Path | None = None
        if self._sealed and (root is None or not is_memory_backed(root)):
            shelter = memory_backed_shelter()
            if shelter is None:
                raise ServerError(
                    ErrorCode.NOT_CONFIGURED,
                    "tenant content is sealed and this host offers no memory-backed filesystem to "
                    "materialize working trees into: every tool call would have to write this "
                    "organization's source back to the node in cleartext, so there is no "
                    "configuration of this server that is both usable and sealed here",
                    details={"setting": "LEMONCROW_SERVER_WORKSPACE_ROOT"},
                )
            self._relocated_from = None if root is None else Path(root)
            _LOG.warning(
                "tenant content is sealed and %s is not memory-backed; materializing working trees "
                "under %s instead so no cleartext reaches the node's disk. The materialization "
                "quota is now enforced against a volume this deployment did not size: point "
                "LEMONCROW_SERVER_WORKSPACE_ROOT at a sized memory-backed volume",
                "no configured root" if root is None else root,
                shelter,
            )
            root = None
        if root is None:
            _sweep_orphaned_roots()
            parent = shelter if shelter is not None else Path(tempfile.gettempdir())
            self._root = Path(tempfile.mkdtemp(prefix=_ROOT_PREFIX, dir=parent))
            self._owns_root = True
        else:
            self._root = root.resolve()
            self._root.mkdir(parents=True, exist_ok=True)
            self._owns_root = False
        os.chmod(self._root, _DIR_MODE)
        self._lock_fd = _acquire_root_lock(self._root)
        if not self._owns_root:
            if self._lock_fd is None:
                # Somebody else is materializing here. Reclaiming the stale
                # slots would delete their live trees, so this keeps its hands
                # off them and says so: sharing a root is a misconfiguration,
                # but destroying another process's working set is worse.
                _LOG.warning(
                    "materialization root %s is already held by another process; "
                    "leaving its trees alone and not reclaiming stale slots",
                    self._root,
                )
            else:
                self._purge_stale_slots()
        self._empty = self._root / _EMPTY_SLOT
        self._empty.mkdir(mode=_DIR_MODE, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Accessors                                                           #
    # ------------------------------------------------------------------ #

    @property
    def root(self) -> Path:
        """The directory every materialized tree lives under."""
        return self._root

    @property
    def sealed(self) -> bool:
        """True when the content store this materializes from is encrypted."""
        return self._sealed

    @property
    def relocated_from(self) -> Path | None:
        """The configured root a sealed deployment was moved off, if it was.

        ``None`` covers both "nothing was relocated" and "a sealed deployment
        configured no root at all"; :attr:`sealed` together with :attr:`root`
        is what an operator or a status route reads to tell those apart.
        """
        return self._relocated_from

    @property
    def empty_root(self) -> Path:
        """What a call with no bound view is answered from.

        Deliberately a real, empty directory rather than ``None``: returning
        ``None`` hands the call back to the process working directory, which is
        how a tenant ends up being told about the server's own checkout.
        """
        return self._empty

    @property
    def max_views(self) -> int:
        return self._max_views

    @property
    def quota_bytes(self) -> int:
        return self._quota_bytes

    @property
    def eviction_grace_s(self) -> float:
        """How long a tree must be idle before the quota may reclaim it."""
        return self._grace_s

    def tree_path(self, org_id: str, view_id: str) -> Path:
        """Where this view's tree lives, whether or not it has been built.

        Exposed because "which directory belongs to which tenant" is the
        isolation claim, and a claim that can only be checked by reading a
        private attribute is a claim nothing keeps honest.
        """
        return self._root / slot_name(org_id, view_id)

    @property
    def live_views(self) -> int:
        with self._lock:
            return len(self._slots)

    @property
    def bytes_on_disk(self) -> int:
        with self._lock:
            return sum(slot.bytes_on_disk for slot in self._slots.values())

    # ------------------------------------------------------------------ #
    # Materialization                                                     #
    # ------------------------------------------------------------------ #

    def materialize(self, org_id: str, view_id: str, view_revision: int) -> MaterializedView:
        """Bring this view's tree to ``view_revision`` and return it.

        Blocking, filesystem-bound work: call it from a worker thread, not from
        the event loop. It is safe to call concurrently -- one lock per slot --
        and cheap when the tree is already at the requested revision.
        """
        slot = self._slot_for(org_id, view_id)
        with slot.lock:
            if slot.at_revision == view_revision:
                # The scratch tree is private to this slot and a View revision
                # is immutable once committed. Tool dispatch already bound the
                # request to the current committed revision, so rebuilding
                # membership/provenance here would only re-prove the same tree.
                result = MaterializedView(
                    root=slot.tree,
                    status=slot.status,
                    rebuilt=False,
                    written=0,
                    removed=0,
                    bytes_written=0,
                )
            else:
                membership = self._membership_at(org_id, view_id, view_revision)
                reachable = addressable_in(
                    self._backend.provenance,
                    self._backend.views.get(org_id, view_id),
                    tuple(sorted({entry.content_digest for entry in membership.values()})),
                )
                result = self._apply(slot, membership, reachable, view_revision)
        self._enforce_quota(keep=slot.tree.name)
        return result

    def _membership_at(self, org_id: str, view_id: str, view_revision: int) -> Mapping[str, ManifestEntry]:
        """Membership for exactly ``view_revision``, or a refusal.

        The revision is read before and after the membership rows. A view only
        advances through ``apply_overlay``, so a revision that is unchanged
        across the read is a revision the rows belong to. If it moved, the
        caller's bound revision is no longer current and the honest answer is
        the same one ``require_acknowledged_revision`` would give a moment
        later -- refresh -- rather than a tree stitched from two revisions.
        """
        views = self._backend.views
        before = views.get(org_id, view_id).view_revision
        membership = views.membership(org_id, view_id)
        after = views.get(org_id, view_id).view_revision
        if before != after or after != view_revision:
            raise ServerError(
                ErrorCode.VIEW_REVISION_STALE,
                "the view advanced while its workspace was being materialized",
                details={
                    "claimed_view_revision": view_revision,
                    "committed_view_revision": after,
                },
                action=AgentAction.REFRESH_VIEW_REVISION,
            )
        return membership

    def _slot_for(self, org_id: str, view_id: str) -> _Slot:
        name = slot_name(org_id, view_id)
        now = float(self._clock())
        with self._lock:
            slot = self._slots.get(name)
            if slot is None:
                slot = _Slot(
                    org_id=org_id,
                    view_id=view_id,
                    tree=self._root / name,
                    lock=threading.Lock(),
                    on_disk={},
                    at_revision=-1,
                    bytes_on_disk=0,
                    last_used=now,
                    status=WorkspaceStatus(
                        view_revision=-1,
                        member_paths=0,
                        materialized_paths=0,
                        absent_paths=0,
                        over_cap_paths=0,
                    ),
                )
                self._slots[name] = slot
            slot.last_used = now
            return slot

    def _apply(
        self,
        slot: _Slot,
        membership: Mapping[str, ManifestEntry],
        reachable: frozenset[str],
        view_revision: int,
    ) -> MaterializedView:
        """Write the delta between what is on disk and what the view declares."""
        wanted: dict[str, str] = {}
        over_cap = 0
        unaddressable = 0
        for path, entry in membership.items():
            if entry.size > self._size_cap:
                # Manifested but never uploaded, by policy. There are no bytes
                # to write, and inventing a placeholder would make a tool
                # report a file it cannot actually read.
                over_cap += 1
                continue
            if entry.content_digest not in reachable:
                # Declared, but this view has never demonstrated possession of
                # these bytes. Writing them would serve a colleague's source to
                # whoever guessed the digest, and refusing them here -- rather
                # than only in the blob-miss handshake -- is what makes that
                # true of every tool that walks the tree rather than only of the
                # ones that name a path.
                unaddressable += 1
                continue
            wanted[path] = entry.content_digest

        if slot.at_revision == view_revision and slot.on_disk == wanted:
            slot.status = WorkspaceStatus(
                view_revision=view_revision,
                member_paths=len(membership),
                materialized_paths=len(slot.on_disk),
                absent_paths=len(membership) - len(slot.on_disk),
                over_cap_paths=over_cap,
            )
            return MaterializedView(
                root=slot.tree,
                status=slot.status,
                rebuilt=False,
                written=0,
                removed=0,
                bytes_written=0,
            )

        if slot.at_revision < 0:
            # Either a fresh slot or one left inconsistent by an interrupted
            # write. Both are rebuilt rather than patched: a delta against an
            # unknown base is a guess.
            _rmtree(slot.tree)
            slot.on_disk.clear()
            slot.bytes_on_disk = 0
        slot.tree.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)

        # Mark dirty for the whole write. A crash between here and the end
        # leaves ``at_revision == -1``, which forces the next call to rebuild.
        slot.at_revision = -1

        removed = 0
        for path in sorted(set(slot.on_disk) - set(wanted)):
            slot.bytes_on_disk -= self._unlink(slot.tree, path)
            del slot.on_disk[path]
            removed += 1

        written = 0
        bytes_written = 0
        missing_content = 0
        for path in sorted(wanted):
            digest = wanted[path]
            if slot.on_disk.get(path) == digest:
                continue
            data = self._backend.content.get(slot.org_id, digest)
            if data is None:
                # The organization does not hold these bytes. The blob-miss
                # guard already answered ``{need: [...]}`` for any path this
                # call actually referenced; this is a file nobody asked for by
                # name, so the tree simply does not contain it and the answer
                # is flagged.
                missing_content += 1
                if path in slot.on_disk:
                    slot.bytes_on_disk -= self._unlink(slot.tree, path)
                    del slot.on_disk[path]
                    removed += 1
                continue
            if path in slot.on_disk:
                slot.bytes_on_disk -= self._unlink(slot.tree, path)
                del slot.on_disk[path]
            self._write(slot.tree, path, data)
            slot.on_disk[path] = digest
            slot.bytes_on_disk += len(data)
            written += 1
            bytes_written += len(data)

        self._prune_empty_directories(slot.tree)
        slot.at_revision = view_revision
        slot.status = WorkspaceStatus(
            view_revision=view_revision,
            member_paths=len(membership),
            materialized_paths=len(slot.on_disk),
            absent_paths=over_cap + unaddressable + missing_content,
            over_cap_paths=over_cap,
        )
        return MaterializedView(
            root=slot.tree,
            status=slot.status,
            rebuilt=True,
            written=written,
            removed=removed,
            bytes_written=bytes_written,
        )

    # ------------------------------------------------------------------ #
    # Filesystem primitives                                               #
    # ------------------------------------------------------------------ #

    def _target(self, tree: Path, path: str) -> Path:
        """Resolve one manifest path inside ``tree``, or refuse.

        ``validate_path`` already rejected absolute paths, ``..``, ``.``,
        backslashes and NUL when the manifest row was accepted. It is applied
        again here because this is the place where a path becomes a write to
        the server's filesystem, and a containment check that lives only at the
        edge is one refactor away from not existing.
        """
        validate_path(path)
        target = tree / path
        if not target.is_relative_to(tree):  # pragma: no cover - defence in depth
            raise ServerError(
                ErrorCode.PATH_NOT_ADDRESSABLE,
                "a manifest path escaped the materialized view",
                details={"view_root": tree.name},
                retryable=False,
                action=AgentAction.FIX_REQUEST,
            )
        return target

    def _next_stamp(self) -> int:
        """A modification time no file in this process has carried before.

        Tracks the wall clock, but is never allowed to repeat or go backwards.
        See :meth:`_write` for why that matters.
        """
        with self._stamp_lock:
            self._stamp = max(int(self._stamp_source()), self._stamp + 1)
            return self._stamp

    def _write(self, tree: Path, path: str, data: bytes) -> None:
        """Write one file, and make the change visible to a stat-keyed cache.

        The bytes are the easy half. The other half is that the tools reading
        this tree keep their own caches keyed by ``(absolute path, st_mtime_ns,
        st_size)`` -- the repo-map tag cache and ``code_context``'s file-bytes
        cache both do, and both are *correctness-preserving only while that key
        changes when the content does*. A view's tree lives at one stable path
        for its whole life, so every revision rewrites the same absolute paths.
        Two revisions of a file with the same length therefore differ only in
        their modification time -- and whether the filesystem can tell those two
        writes apart is a property of its timestamp granularity, not of this
        protocol. On a mount with coarse timestamps, a same-length edit inside
        one tick would leave read-after-edit resting on how fast the machine
        happened to be.

        That is a timing dependence, and the contract does not have any. Each
        written file is stamped with a modification time drawn from a strictly
        increasing source, so the stat key provably changes on every write no
        matter what the clock or the filesystem does. ``futimens`` on the open
        descriptor, so the stamp lands on the file just written and never on
        something a symlink swapped in afterwards.
        """
        target = self._target(tree, path)
        target.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        # O_NOFOLLOW so a symlink left in the tree can never redirect a write
        # out of it. The caller unlinked any previous file at this path, so the
        # open creates rather than truncates in the normal case.
        descriptor = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, _FILE_MODE)
        try:
            os.write(descriptor, data)
            stamp = self._next_stamp()
            os.utime(descriptor, ns=(stamp, stamp))
        finally:
            os.close(descriptor)

    def _unlink(self, tree: Path, path: str) -> int:
        """Remove one materialized file; return the bytes reclaimed."""
        target = self._target(tree, path)
        try:
            size = target.stat().st_size
            target.unlink()
        except OSError:
            return 0
        return int(size)

    def _prune_empty_directories(self, tree: Path) -> None:
        """Drop directories a removal emptied, so the tree matches membership.

        A left-behind empty directory is not a correctness problem for content,
        but it is one for ``read`` on a directory and for any tool that lists a
        subtree -- the view would appear to still contain a package it does not.
        """
        for parent, directories, files in os.walk(tree, topdown=False):
            if parent == str(tree) or directories or files:
                continue
            try:
                os.rmdir(parent)
            except OSError:  # pragma: no cover - racing writer
                pass

    def _purge_stale_slots(self) -> None:
        """Reclaim trees a previous process left at a configured root.

        The slot bookkeeping is in memory by design -- a tree is a cache, not
        state worth recovering -- so a tree without bookkeeping is garbage. It
        is dropped at startup rather than left to accumulate across restarts.
        """
        try:
            entries = list(self._root.iterdir())
        except OSError:  # pragma: no cover - an unreadable root fails later anyway
            return
        for entry in entries:
            name = entry.name
            if name != _EMPTY_SLOT and not (len(name) == _SLOT_NAME_LEN and _HEX.issuperset(name)):
                continue
            _rmtree(entry)

    # ------------------------------------------------------------------ #
    # Reclamation                                                         #
    # ------------------------------------------------------------------ #

    def invalidate(self, org_id: str, view_id: str) -> bool:
        """Declare this view's tree untrustworthy, without removing it.

        The next materialization rebuilds from scratch instead of applying a
        delta. Used where the bytes on disk may no longer match the bookkeeping
        -- an interrupted write, or an operator who suspects one -- because a
        delta against an unknown base is not a delta.
        """
        with self._lock:
            slot = self._slots.get(slot_name(org_id, view_id))
            if slot is None:
                return False
            slot.at_revision = -1
            return True

    def forget(self, org_id: str, view_id: str) -> bool:
        """Drop the tree for one view. Called when the client closes it."""
        return self._drop(slot_name(org_id, view_id))

    def forget_view(self, view_id: str) -> bool:
        """Drop a tree knowing only the view id.

        Lease eviction discovers that a view is gone after the row naming its
        owner, exactly as it does for the derived layers, so the sweep needs a
        handle that does not require an organization.
        """
        with self._lock:
            names = [name for name, slot in self._slots.items() if slot.view_id == view_id]
        return any([self._drop(name) for name in names])

    def _drop(self, name: str) -> bool:
        with self._lock:
            slot = self._slots.pop(name, None)
        if slot is None:
            return False
        with slot.lock:
            _rmtree(slot.tree)
        return True

    def sweep(self) -> int:
        """Enforce the quota on demand. Returns how many trees were dropped."""
        return self._enforce_quota(keep=None)

    def _enforce_quota(self, *, keep: str | None) -> int:
        """Drop least-recently-used trees until the ceilings hold.

        Two rules make this safe rather than merely bounded. A tree used within
        the eviction grace window is never dropped -- the grace is the tool
        deadline, so no in-flight call can still be reading a tree older than
        it -- and the tree this call just built is never a candidate. When
        nothing is evictable the ceiling is exceeded and logged: overshooting a
        cache quota is an operational problem, pulling a tree out from under a
        running tool call is a correctness one.
        """
        dropped = 0
        while True:
            now = float(self._clock())
            with self._lock:
                total = sum(slot.bytes_on_disk for slot in self._slots.values())
                live = len(self._slots)
                if live <= self._max_views and total <= self._quota_bytes:
                    return dropped
                candidates = sorted(
                    (slot.last_used, name)
                    for name, slot in self._slots.items()
                    if name != keep and now - slot.last_used >= self._grace_s
                )
            if not candidates:
                _LOG.warning(
                    "materialized view quota exceeded with nothing evictable (%d tree(s), %d byte(s))",
                    live,
                    total,
                )
                return dropped
            if not self._drop(candidates[0][1]):  # pragma: no cover - raced with forget
                return dropped
            dropped += 1

    def close(self) -> None:
        """Drop every tree, and the root itself when this object made it.

        The root lock is released last, so nothing -- least of all an offline
        purge waiting for the server to stop -- can start reclaiming trees this
        call is still walking.
        """
        with self._lock:
            names = list(self._slots)
        for name in names:
            self._drop(name)
        if self._owns_root:
            _rmtree_until_gone(self._root)
        _release_root_lock(self._lock_fd)
        self._lock_fd = None


#: Prefix of a materializer-owned temporary root. Shared by creation and by the
#: orphan sweep, so the two cannot disagree about what this server owns.
_ROOT_PREFIX: Final[str] = "lemoncrow-views-"

#: How long an orphaned root must have been untouched before a later server
#: reclaims it. Comfortably longer than any single tool deadline, so a root a
#: live server is still using is never swept out from under it.
_ORPHAN_AGE_S: Final[float] = 3600.0

_RECLAIM_ATTEMPTS: Final[int] = 6
_RECLAIM_PAUSE_S: Final[float] = 0.05


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _rmtree_until_gone(path: Path) -> bool:
    """Remove a tree, retrying while something races us to recreate it.

    The public tools own background threads that write derived state (their own
    index, workspace scratch) into whatever tree they were pointed at. One of
    those can recreate a slot directory microseconds after ``rmtree`` walked
    past it, which leaves the root behind after an otherwise clean shutdown --
    observed as orphaned ``lemoncrow-views-*`` roots surviving the process that
    made them. Retrying closes the race in the common case; ``_sweep_orphaned_
    roots`` is the backstop for the case where it does not.
    """
    for attempt in range(_RECLAIM_ATTEMPTS):
        _rmtree(path)
        if not path.exists():
            return True
        if attempt + 1 < _RECLAIM_ATTEMPTS:
            time.sleep(_RECLAIM_PAUSE_S)
    _LOG.warning("materialized view root survived shutdown; left for the orphan sweep")
    return False


def _sweep_orphaned_roots(*, now: Callable[[], float] = time.time) -> int:
    """Reclaim materializer roots a previous server left behind. Best effort.

    What this server creates, this server closes; this is the closer for the
    case where a process died before it could. Only roots older than
    ``_ORPHAN_AGE_S`` and held by nobody are touched, so a concurrently running
    server's root is never removed -- the age test alone would reclaim the root
    of a healthy server that has simply been idle for an hour.
    """
    swept = 0
    cutoff = now() - _ORPHAN_AGE_S
    for candidate in _private_roots():
        try:
            if candidate.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        if root_is_held(candidate):
            continue
        _rmtree(candidate)
        if not candidate.exists():
            swept += 1
    return swept


def _private_roots() -> tuple[Path, ...]:
    """Per-process roots of this build, in every directory one can be created in.

    Both the ordinary temporary directory and the memory-backed shelters a
    sealed deployment relocates into, because a deletion that only looked in
    ``/tmp`` would miss exactly the trees an encrypted deployment has.
    """
    parents = [Path(name) for name in _SHELTERS]
    parents.append(Path(tempfile.gettempdir()))
    found: list[Path] = []
    for parent in dict.fromkeys(parents):
        try:
            found.extend(path for path in parent.glob(f"{_ROOT_PREFIX}*") if path.is_dir())
        except OSError:  # pragma: no cover - unreadable directory
            continue
    return tuple(sorted(dict.fromkeys(found)))


def materialization_roots(configured: Path | None = None) -> tuple[Path, ...]:
    """The directories *this deployment* materializes into. Never anyone else's.

    Exactly the configured root, or nothing. The other shape a root can take --
    the private temporary directory a server with no ``WORKSPACE_ROOT`` makes
    for itself -- is deliberately excluded, and the exclusion is the point: a
    private root is anonymous, so a deletion that swept every
    ``lemoncrow-views-*`` on the host would reclaim the working set of any other
    deployment, test run or evaluation server sharing the machine. It is also
    unnecessary: a private root belongs to one process, which removes it on
    shutdown, and :func:`_sweep_orphaned_roots` reclaims what a crash left.

    The consequence is real and belongs in the deletion report rather than in a
    comment: on a deployment that names no workspace root, a purge cannot see
    the working trees at all, and "the server removed them when it stopped" is
    a claim about the server rather than an observation of the volume. Naming a
    root is what turns it into an observation.
    """
    if configured is None:
        return ()
    root = Path(configured)
    return (root,) if root.is_dir() else ()


def _slot_directories(root: Path) -> tuple[Path, ...]:
    """Every materialized tree at ``root``. Never the lock, never the empty slot."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return ()
    return tuple(
        entry
        for entry in entries
        if entry.is_dir() and len(entry.name) == _SLOT_NAME_LEN and _HEX.issuperset(entry.name)
    )


def _bytes_under(tree: Path) -> int:
    total = 0
    for parent, _directories, files in os.walk(tree):
        for name in files:
            try:
                total += os.stat(os.path.join(parent, name)).st_size
            except OSError:  # pragma: no cover - racing removal
                continue
    return total


@dataclass(frozen=True, slots=True)
class TreeCensus:
    """What the materialization roots hold. Counts only -- never a path.

    ``unattributed`` is the honest number. A tree is named by a digest of its
    organization and view id, so a tree whose view row no longer exists cannot
    be attributed to anyone from the outside. Reporting it separately is the
    difference between "nothing of yours is left" and "nothing is left that we
    could still recognise as yours".
    """

    trees: int
    files: int
    bytes_on_disk: int
    unattributed: int

    @property
    def empty(self) -> bool:
        return not (self.trees or self.unattributed)

    def to_wire(self) -> dict[str, int]:
        return {
            "materialized_trees": self.trees,
            "materialized_files": self.files,
            "materialized_bytes": self.bytes_on_disk,
            "materialized_trees_unattributed": self.unattributed,
        }


#: The census of a deployment whose roots were never looked at. Frozen, so one
#: shared instance is a safe default rather than a call in a dataclass field.
NO_TREES: Final[TreeCensus] = TreeCensus(trees=0, files=0, bytes_on_disk=0, unattributed=0)


def tree_census(roots: Iterable[Path], org_id: str, view_ids: Sequence[str]) -> TreeCensus:
    """Count the cleartext working trees these roots still hold for ``org_id``."""
    wanted = {slot_name(org_id, view_id) for view_id in view_ids}
    trees = files = total = unattributed = 0
    for root in roots:
        for entry in _slot_directories(Path(root)):
            if entry.name not in wanted:
                unattributed += 1
                continue
            trees += 1
            for parent, _directories, names in os.walk(entry):
                for name in names:
                    files += 1
                    try:
                        total += os.stat(os.path.join(parent, name)).st_size
                    except OSError:  # pragma: no cover - racing removal
                        continue
    return TreeCensus(trees=trees, files=files, bytes_on_disk=total, unattributed=unattributed)


def drop_trees(
    roots: Iterable[Path],
    org_id: str,
    view_ids: Sequence[str],
    *,
    reclaim_unattributed: bool = False,
) -> tuple[int, int]:
    """Remove trees at these roots without a running server. ``(trees, bytes)``.

    The offline half of :meth:`ViewMaterializer.drop_trees`. Callers must have
    established that no server holds the root -- see :func:`root_is_held` --
    because removing a tree a live tool call is reading would half-finish the
    deletion and break the call.
    """
    wanted = {slot_name(org_id, view_id) for view_id in view_ids}
    trees = 0
    reclaimed = 0
    for root in roots:
        for entry in _slot_directories(Path(root)):
            if entry.name not in wanted and not reclaim_unattributed:
                continue
            reclaimed += _bytes_under(entry)
            _rmtree(entry)
            if not entry.exists():
                trees += 1
    return trees, reclaimed
