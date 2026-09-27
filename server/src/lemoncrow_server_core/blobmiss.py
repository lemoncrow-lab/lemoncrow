"""The blob-miss protocol.

From the design: "On a miss the server replies ``{need: [paths]}`` and the shim
fills it in the same turn." Two properties matter and both are enforced here:

1. **The server never guesses.** If the content backing a referenced path is
   not addressable by the open view, the answer names the paths instead of
   returning a partial or stale result. A miss is a protocol answer with HTTP
   200, not an error -- an error would make the agent treat a normal cold read
   as a failure.

   *Addressable*, not *held by the organization*: the question this route
   answers is "do I need to upload this?", and answering it from what the
   tenant happens to hold made possession of a digest into possession of the
   file. A path whose digest this view has never demonstrated possession of is
   reported as needed, which is also the answer for a digest nobody in the
   tenant has ever uploaded -- see :mod:`.index.boundary`.
2. **Exactly one retry.** The identical call gets one ``need`` answer. If it
   comes back with the same paths still missing, the client did not fill them
   and a second ``need`` would be an unbounded ping-pong, so the second attempt
   refuses with a typed, non-retryable ``blob_missing``.

The replay budget is keyed by ``(session, view, tool, canonical arguments)``,
which is what "the same turn" means operationally: a different call, or the
same call after the agent changed its arguments, gets its own budget.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .dispatch import ToolInvocation
from .errors import AgentAction, ErrorCode, ServerError
from .index.boundary import addressable_in
from .index.contracts import IndexBackend, ManifestEntry

__all__ = [
    "BlobMissAnswer",
    "BlobMissGuard",
    "referenced_paths",
    "reject_unaddressable_paths",
]

#: Which argument keys carry repository paths, per tool. A tool absent from
#: this table references no path, so it can never produce a blob miss.
_PATH_ARGS: Final[Mapping[str, tuple[str, ...]]] = {
    "read": ("files",),
    "code_search": ("paths",),
    "search": ("paths",),
    "relations": ("path", "file", "paths"),
    "graph": ("path", "paths"),
}

#: Which argument keys carry a repository path for CONTAINMENT purposes. This
#: is deliberately wider than ``_PATH_ARGS``: a path a tool merely reads as
#: scope or context still resolves on the server's filesystem, so it must be
#: contained even when no upload could ever satisfy it. Drift is caught by
#: ``test_registry_dispatch.test_every_path_shaped_argument_is_contained``.
_CONTAINMENT_ARGS: Final[Mapping[str, tuple[str, ...]]] = {
    "read": ("files",),
    "code_search": ("paths", "path"),
    "search": ("paths", "path"),
    "relations": ("path", "file", "paths"),
    "graph": ("path", "paths"),
    "context": ("files", "excluded_paths"),
    "rescue": ("files",),
    "trace": ("capture_files",),
}

#: Range/mode suffixes the public ``read`` tool accepts on a path entry.
_SUFFIX_SEPARATOR: Final[str] = ":"

_MAX_TRACKED_KEYS: Final[int] = 4096


@dataclass(frozen=True, slots=True)
class BlobMissAnswer:
    """The ``{need: [...]}`` answer."""

    need: tuple[str, ...]
    view_id: str
    view_revision: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "need": list(self.need),
            "view_id": self.view_id,
            "view_revision": self.view_revision,
            "retryable_once": True,
        }


def _normalize(raw: object) -> str | None:
    """Reduce one argument entry to a repository-relative path, or ``None``.

    ``read`` accepts ``"pkg/mod.py:L10-L20"``, ``"pkg/mod.py:full"`` and
    ``{"path": ..., "range": ...}``. Everything after the first colon is a mode
    or a range, never part of the path.
    """
    if isinstance(raw, Mapping):
        raw = raw.get("path")
    if not isinstance(raw, str):
        return None
    candidate = raw.split(_SUFFIX_SEPARATOR, 1)[0].strip()
    if not candidate:
        return None
    if candidate.startswith("/") or candidate.startswith("~") or "\\" in candidate:
        # Absolute or host-native. Not addressable in a view manifest, so it is
        # not something this server can be missing.
        return None
    if any(segment in {"..", "."} for segment in candidate.split("/")):
        return None
    return candidate


def _path_arguments(tool: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Every raw path-shaped argument entry a call carries, unnormalized."""
    keys = _CONTAINMENT_ARGS.get(tool)
    if not keys:
        return ()
    out: list[str] = []
    for key in keys:
        value = arguments.get(key)
        if value is None:
            continue
        entries: Sequence[Any] = value if isinstance(value, (list, tuple)) else (value,)
        for entry in entries:
            raw = entry.get("path") if isinstance(entry, Mapping) else entry
            if isinstance(raw, str) and raw.strip():
                out.append(raw)
    return tuple(out)


def _is_unaddressable(raw: str) -> bool:
    """True when a path argument names something outside the tenant's view.

    A view manifest holds repository-relative paths only, so a host-absolute
    path, a ``~`` path, a Windows drive or UNC path, or one that climbs out with
    ``..`` cannot name view content by construction. Such an argument is not a
    cold read to be filled -- it is a request for a file the view does not
    describe, and the only safe answer is a refusal.
    """
    candidate = raw.strip()
    if not candidate:
        return False
    if "\x00" in candidate:
        return True
    if candidate.startswith(("/", "~", "\\")):
        return True
    if len(candidate) >= 2 and candidate[1] == ":" and candidate[0].isalpha():
        # C:\ or C:/ -- a drive-qualified host path, not a repository path.
        return True
    if "\\" in candidate:
        return True
    body = candidate.split(_SUFFIX_SEPARATOR, 1)[0]
    return any(segment == ".." for segment in body.split("/"))


def reject_unaddressable_paths(tool: str, arguments: Mapping[str, Any]) -> None:
    """Refuse a call whose path arguments cannot name view content.

    Containment is security policy, so it fails closed and is deliberately kept
    separate from the blob-miss question of what the store is merely lacking:
    treating "not addressable in a manifest" as "nothing to fill" would hand the
    argument to a tool that resolves it against the server's own filesystem.
    """
    offenders = tuple(raw for raw in _path_arguments(tool, arguments) if _is_unaddressable(raw))
    if not offenders:
        return
    raise ServerError(
        ErrorCode.PATH_NOT_ADDRESSABLE,
        "path arguments must be repository-relative paths inside the open view",
        details={"tool": tool, "unaddressable_path_count": len(offenders)},
        retryable=False,
        action=AgentAction.FIX_REQUEST,
    )


def referenced_paths(tool: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Repository-relative paths a call references, in first-seen order."""
    keys = _PATH_ARGS.get(tool)
    if not keys:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        value = arguments.get(key)
        if value is None:
            continue
        entries: Sequence[Any] = value if isinstance(value, (list, tuple)) else (value,)
        for entry in entries:
            path = _normalize(entry)
            if path is None or path in seen:
                continue
            seen.add(path)
            out.append(path)
    return tuple(out)


#: Shell-glob metacharacters. A path argument carrying one is a pattern the
#: tool expands, never a single file the client could upload.
_PATTERN_CHARACTERS: Final[frozenset[str]] = frozenset("*?[]")


def _is_pattern(path: str) -> bool:
    return any(character in _PATTERN_CHARACTERS for character in path)


def _selects_a_directory(path: str, membership: Mapping[str, ManifestEntry]) -> bool:
    """True when the view holds files *under* ``path``.

    ``code_search``, ``search`` and ``graph`` take ``paths`` as a scope, and
    ``read`` accepts a directory and answers with a listing. In every one of
    those cases the argument names a subtree the view already describes, so the
    only thing a ``need`` answer could achieve is a loop: there is no blob whose
    upload would make the path resolve.
    """
    prefix = path.rstrip("/") + "/"
    return any(candidate.startswith(prefix) for candidate in membership)


def call_key(invocation: ToolInvocation) -> str:
    """Stable identity for "the same call", digest-only so nothing is retained."""
    payload = json.dumps(
        {
            "view": invocation.tenant.view_id or "",
            "tool": invocation.tool,
            "args": invocation.arguments,
        },
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BlobMissGuard:
    """Decides when to answer ``{need: [...]}`` and when to refuse."""

    __slots__ = ("_backend", "_lock", "_max_need", "_seen", "_size_cap")

    def __init__(self, backend: IndexBackend, *, max_need_paths: int, content_size_cap: int) -> None:
        self._backend = backend
        self._max_need = max_need_paths
        self._size_cap = content_size_cap
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._lock = threading.Lock()

    def missing_paths(self, invocation: ToolInvocation) -> tuple[str, ...]:
        """Referenced paths whose content this view cannot address.

        A path with no manifest entry counts as missing: the view does not
        describe it, so answering from the index would be a guess.
        """
        view_id = invocation.tenant.view_id
        if not view_id:
            return ()
        paths = referenced_paths(invocation.tool, invocation.arguments)
        if not paths:
            return ()
        org_id = invocation.tenant.org_id
        entries: dict[str, ManifestEntry] = {}
        for path in paths:
            entry = self._backend.views.entry(org_id, view_id, path)
            if entry is not None:
                entries[path] = entry
        # One boundary question for the whole call, so a call naming a hundred
        # paths costs one lookup and -- more to the point -- costs the same
        # whether or not the organization holds the bytes under another scope.
        reachable = addressable_in(
            self._backend.provenance,
            self._backend.views.get(org_id, view_id),
            tuple(sorted({entry.content_digest for entry in entries.values()})),
        )
        missing: list[str] = []
        directories: Mapping[str, ManifestEntry] | None = None
        for path in paths:
            entry = entries.get(path)
            if entry is None:
                if _is_pattern(path):
                    # A glob is a scope, not a file. No upload could satisfy it.
                    continue
                if directories is None:
                    directories = self._backend.views.membership(org_id, view_id)
                if _selects_a_directory(path, directories):
                    # The view describes this prefix, just not as a file: the
                    # caller scoped a search (or read) to a directory. Demanding
                    # it as content would be unfillable -- the client has no
                    # blob to send -- so the call proceeds and the tool answers
                    # for the directory exactly as it would locally.
                    continue
                missing.append(path)
                continue
            if entry.size > self._size_cap:
                # Manifested but not uploaded, by policy. Asking for it would
                # be an unfillable demand: the upload route refuses it and the
                # same-host read path skips it, so the client could only loop.
                continue
            if entry.content_digest not in reachable:
                missing.append(path)
            if len(missing) >= self._max_need:
                break
        return tuple(missing)

    def resolve(self, invocation: ToolInvocation) -> BlobMissAnswer | None:
        """``None`` when the call can proceed; otherwise the ``need`` answer.

        Raises ``blob_missing`` when the same call already received one, and
        ``path_not_addressable`` when an argument names a path outside the view.
        """
        reject_unaddressable_paths(invocation.tool, invocation.arguments)
        missing = self.missing_paths(invocation)
        if not missing:
            return None
        key = (invocation.tenant.session_id, call_key(invocation))
        with self._lock:
            already = key in self._seen
            if not already:
                self._seen[key] = None
                while len(self._seen) > _MAX_TRACKED_KEYS:
                    self._seen.popitem(last=False)
        if already:
            raise ServerError(
                ErrorCode.BLOB_MISSING,
                ("content for the requested paths is still not on the server " "after one fill attempt"),
                details={
                    "tool": invocation.tool,
                    "missing_path_count": len(missing),
                    "view_id": invocation.tenant.view_id or "",
                },
                retryable=False,
                action=AgentAction.UPLOAD_BLOBS,
            )
        return BlobMissAnswer(
            need=missing,
            view_id=invocation.tenant.view_id or "",
            view_revision=int(invocation.tenant.view_revision or 0),
        )

    def forget_session(self, session_id: str) -> None:
        with self._lock:
            for key in [key for key in self._seen if key[0] == session_id]:
                del self._seen[key]

    @property
    def tracked(self) -> int:
        with self._lock:
            return len(self._seen)
