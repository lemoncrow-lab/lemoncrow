"""The remote half of one short-lived client process.

This is the MCP-initialize bootstrap sequence from the design, in order and
with the failure behaviour the degradation table requires:

1. **Handshake before authentication.** ``POST /v1/handshake`` negotiates
   protocol and capabilities without a credential, so a client that must be
   upgraded gets ``protocol_version_mismatch`` instead of a confusing 401. A
   mismatch is refused loudly and the session does not open -- never a silent
   downgrade.
2. **Auth.** The bearer token resolved by :mod:`lemoncrow_client.config`:
   ``LEMONCROW_TOKEN``, else an IT-provisioned file. No token means no remote
   session, reported in one line.
3. **``views/open``** with a canonical manifest root, then only the chunks and
   the content the server says it lacks. The manifest comes from a walk that
   re-hashes only what changed since the last session -- see
   :mod:`lemoncrow_client.walkcache`, which owns the proof that a skipped hash
   equals a performed one. The cache it leaves behind is written *here*, after a
   view has actually opened, which is what keeps "an offline session writes
   nothing at all" true.
4. **Session register** -- which is step 2's ``POST /v1/sessions`` -- returning
   the tool surface and the server's limits, which the client then obeys rather
   than assuming.

Everything after bootstrap goes through :meth:`RemoteSession.call_tool`, which
owns the two contracts that make remote execution safe:

* every call names the ``view_revision`` it observed, so read-after-edit is a
  protocol guarantee; and
* a ``{"need": [...]}`` answer is filled and retried exactly once, because an
  unbounded fill/retry loop is worse than a typed refusal -- and a needed path
  no fill can satisfy (absent here, a directory) is refused as
  ``path_not_addressable`` with suggestions, never retried into a misleading
  ``blob_missing``.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Concatenate, Final
from urllib.parse import quote

from .blobs import BlobService
from .capture_polling import await_capture
from .config import (
    LOCAL_FS_PROOF_PATH,
    REVIEW_CONFIG_PATH,
    ClientConfig,
    is_index_candidate,
    not_indexed_error,
    repo_identity,
)
from .credentials import RefreshingHttpTransport, managed_credentials_path
from .errors import AgentAction, ClientError, ErrorCode
from .generation import plan_repository_generation
from .git_sync import GitBase, committed_base, dirty_paths, object_batches, overlay_entries
from .git_sync import head as git_head
from .liveview import LiveViewBinding, clear_live_view_binding, write_live_view_binding
from .manifest import (
    ManifestChunk,
    ManifestEntry,
    WalkedFile,
    WalkResult,
    chunk_manifest,
    manifest_root,
    walk_worktree,
)
from .protocol import (
    CLIENT_CAPABILITIES,
    HEADER_HOST,
    HEADER_HOST_SESSION,
    HEADER_MODEL,
    HEADER_REQUEST_ID,
    RESULT_REUSE_CAPABILITY,
    SAME_HOST_CAPABILITY,
    ReviewManifest,
    negotiate_protocol,
    parse_review_manifest,
    protocol_window,
    review_route_requirement,
    window_refusal,
)
from .resolution_cache import CacheKey, CacheStats, ResolutionCache, make_cache_key
from .routing import ROUTES
from .runtime_attribution import record_runtime_policy_attribution
from .search_markdown import render_local_code_search
from .state import SessionState
from .trace import current_trace_id, new_trace_id
from .transport import HttpTransport
from .walkcache import WalkCache, WalkCacheStats, load_walk_cache, peek_walk_cache_proof

__all__ = ["BootstrapReport", "RemoteSession", "ToolAnswer", "persist_walk_cache", "workspace_state_fingerprint"]

_MAX_CHUNK_ROWS: Final[int] = 2_000
#: Lease asked for a Review snapshot View; the server caps it at its maximum.
_REVIEW_SNAPSHOT_LEASE_S: Final[float] = 86_400.0
#: Slack under the server's request cap for the JSON envelope and header bytes.
_REQUEST_HEADROOM: Final[int] = 1024
#: Covers the plan's 1M scale fixture without silently turning a truncated
#: worktree walk into overlay tombstones against the complete Git generation.
_MAX_INDEX_FILES: Final[int] = 2_000_000
#: Bytes an append request adds around its files: seq, final, new_blobs keys.
_APPEND_ENVELOPE: Final[int] = 256
_PROOF_BYTES: Final[int] = 24
#: Tools that cannot answer at all without an index (refused when not indexed).
#: Freshness is wider: see :func:`_reads_view`.
_INDEX_VIEW_TOOLS: Final[frozenset[str]] = frozenset({"code_search", "read", "relations", "graph", "search"})
_INTERNAL_WORKTREE_ROOTS: Final[frozenset[str]] = frozenset({".git", ".claude", ".lc-worktrees", ".lemoncrow"})
_HOST_SESSION_ENVS: Final[tuple[tuple[str, str], ...]] = (
    ("CLAUDE_CODE_SESSION_ID", "claude"),
    ("CODEX_SESSION_ID", "codex"),
    ("OPENCODE_SESSION_ID", "opencode"),
    ("GITHUB_COPILOT_SESSION_ID", "copilot"),
    ("CURSOR_SESSION_ID", "cursor"),
    ("CURSOR_TRACE_ID", "cursor"),
    ("HERMES_SESSION_ID", "hermes"),
    ("ANTIGRAVITY_SESSION_ID", "antigravity"),
    ("AGY_SESSION_ID", "antigravity"),
)
#: Bounds on the "did you mean" work done when a path argument names nothing.
_MAX_PATH_SUGGESTIONS: Final[int] = 5
_MAX_REPORTED_PATHS: Final[int] = 5
_GIT_LISTING_TIMEOUT_S: Final[float] = 5.0
#: Refusals meaning the server restarted under this session: re-open, then retry once.
_REOPEN_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {ErrorCode.SESSION_UNKNOWN, ErrorCode.SESSION_EXPIRED, ErrorCode.VIEW_UNKNOWN}
)
#: Why a requested path is unfillable, keyed by :meth:`BlobService.unfillable`.
_UNFILLABLE_REASONS: Final[Mapping[str, str]] = {
    "absent": "does not exist in this workspace",
    "directory": "is a directory the index holds no files under (ignored, empty, or not yet synced)",
    "symlink": "is a symlink, which is never indexed",
    "special": "is not a regular file",
}


def _workspace_host_bridge(repo_root: Path) -> tuple[str, str, str]:
    """Read host session identity written by the existing host hooks."""
    path = repo_root / ".lemoncrow" / "workspace" / "session_state.json"
    try:
        if not path.is_file() or path.stat().st_size > 256 * 1024:
            return "", "", ""
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return "", "", ""
    if not isinstance(raw, dict):
        return "", "", ""
    return (
        str(raw.get("session_id") or "").strip(),
        str(raw.get("host") or "").strip().lower(),
        str(raw.get("model") or "").strip(),
    )


def _host_runtime_headers(config: ClientConfig) -> dict[str, str]:
    """Return real coding-host identity for local savings attribution."""
    bridge_sid, bridge_host, bridge_model = _workspace_host_bridge(config.repo_root)
    explicit_host = os.environ.get("LEMONCROW_AGENT", "").strip().lower()

    env_sessions = [
        (candidate, host)
        for env_name, host in _HOST_SESSION_ENVS
        if (candidate := os.environ.get(env_name, "").strip())
    ]

    # An override may be inherited from a different host. Keep the host and
    # session id from the same source; a Claude window id is the strongest
    # evidence when no session id matches the override.
    matching_env = next(((sid, host) for sid, host in env_sessions if host == explicit_host), None)
    claude_env = next(((sid, host) for sid, host in env_sessions if host == "claude"), None)
    selected = matching_env or claude_env or (env_sessions[0] if env_sessions else None)
    if selected:
        env_sid, env_host = selected
        model = bridge_model if bridge_sid == env_sid and (not bridge_host or bridge_host == env_host) else ""
        return {
            HEADER_HOST_SESSION: env_sid,
            HEADER_HOST: env_host,
            **({HEADER_MODEL: model} if model else {}),
        }

    # Codex/OpenCode hooks refresh the workspace relay with the active native
    # session. Claude is deliberately excluded here: multiple Claude windows
    # may share one repository, making a workspace-only id last-writer-wins.
    if bridge_sid and bridge_host and bridge_host != "claude" and (not explicit_host or explicit_host == bridge_host):
        return {
            HEADER_HOST_SESSION: bridge_sid,
            HEADER_HOST: bridge_host,
            **({HEADER_MODEL: bridge_model} if bridge_model else {}),
        }
    return {}


def _git_workspace_state_fingerprint(repo_root: Path) -> str:
    """Cheap Git identity of the source state, or empty outside a usable checkout.

    One porcelain-v2 status call carries both the exact HEAD oid and the dirty
    worktree records. Hash stat identity for every reported path as well, so a
    second edit to an already-dirty same-size file still changes the fingerprint.
    LemonCrow's own nested worktrees/state are excluded at the Git pathspec, so
    their churn cannot invalidate the source View. A non-Git/unusable checkout
    returns empty here; the public wrapper owns the canonical-walk fallback.
    """
    try:
        status = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v2",
                "--branch",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=dirty",
                "--",
                ".",
                ":(exclude).claude/worktrees",
                ":(exclude).lc-worktrees",
                ":(exclude).lemoncrow",
            ],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            # ``git status`` normally may refresh index stat metadata. The thin
            # client needs observation only; disabling optional locks tells Git
            # not to perform that opportunistic write.
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        status = None

    if status is not None and status.returncode == 0:
        hasher = hashlib.sha256()
        records = status.stdout.split(b"\0")
        index = 0
        while index < len(records):
            raw = records[index]
            index += 1
            if not raw:
                continue
            if raw.startswith(b"# "):
                # Includes ``branch.oid``, so a commit/checkout changes the
                # fingerprint without a second ``git rev-parse`` process.
                hasher.update(raw)
                continue

            path_bytes: bytes | None = None
            if raw.startswith(b"1 "):
                fields = raw.split(b" ", 8)
                path_bytes = fields[8] if len(fields) == 9 else None
            elif raw.startswith(b"2 "):
                fields = raw.split(b" ", 9)
                path_bytes = fields[9] if len(fields) == 10 else None
                # Porcelain v2 -z follows a rename/copy record with the source
                # path. The record itself already carries the destination and
                # score; hash the source too, but never stat it as the live path.
                if index < len(records):
                    hasher.update(records[index])
                    index += 1
            elif raw.startswith(b"u "):
                fields = raw.split(b" ", 10)
                path_bytes = fields[10] if len(fields) == 11 else None
            elif raw.startswith((b"? ", b"! ")):
                path_bytes = raw[2:]

            hasher.update(raw)
            if not path_bytes:
                continue
            try:
                relative = path_bytes.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            except UnicodeError:
                continue
            root_name = relative.split("/", 1)[0]
            if root_name in _INTERNAL_WORKTREE_ROOTS:
                continue
            target = repo_root / relative.rstrip("/")
            try:
                info = target.stat(follow_symlinks=False)
            except OSError:
                hasher.update(b"<missing>")
            else:
                hasher.update(
                    f"{info.st_mode}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}:{info.st_ino}:{info.st_dev}".encode()
                )
        # ``.lemoncrow`` stays excluded from the status scan (it is local state
        # and can be large), but its committed Review config is part of the View.
        # A pathspec exclude cannot carve out that one file, so stat it directly.
        try:
            config = (repo_root / REVIEW_CONFIG_PATH).stat(follow_symlinks=False)
        except OSError:
            hasher.update(b"review-config:<missing>")
        else:
            hasher.update(
                f"review-config:{config.st_mode}:{config.st_size}:{config.st_mtime_ns}:{config.st_ctime_ns}:{config.st_ino}".encode()
            )
        return hasher.hexdigest()

    return ""


def suggest_paths(
    requested: str,
    candidates: Iterable[str],
    *,
    limit: int = _MAX_PATH_SUGGESTIONS,
) -> tuple[str, ...]:
    """Indexed paths that end with ``requested`` on a segment boundary.

    A path written relative to the wrong directory -- ``components/pdp`` for
    ``frontend/src/components/pdp`` -- is the usual way an agent names nothing.
    Each candidate file yields itself (it ends with the path) or the directory
    prefix that does. Shallowest first, so the likeliest root comes first.
    """
    needle = requested.strip("/")
    if not needle:
        return ()
    suffix = "/" + needle
    found: dict[str, None] = {}
    for candidate in candidates:
        probe = "/" + candidate
        index = probe.find(suffix + "/")
        if index >= 0:
            match = probe[1 : index + len(suffix)]
        elif probe.endswith(suffix):
            match = candidate
        else:
            continue
        if match != needle:
            found[match] = None
    return tuple(sorted(found, key=lambda path: (path.count("/"), path))[:limit])


def _git_listed_paths(repo_root: Path) -> tuple[str, ...]:
    """Tracked plus unignored untracked files: the set a walk would index."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=_GIT_LISTING_TIMEOUT_S,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    return tuple(path for path in completed.stdout.decode("utf-8", errors="replace").split("\0") if path)


def _reads_view(tool: str) -> bool:
    """Whether a call must see the working tree as it is now.

    The server answers every tool that binds a View from that View's tree, so
    the probe covers every server route unless it declares itself
    independent of the source; an unrouted name is probed too (fail-safe).
    """
    route = ROUTES.get(tool)
    return route is None or route.reads_view


def workspace_state_fingerprint(repo_root: Path) -> str:
    """Exact source-state identity used to validate a long-lived server View.

    Git worktrees use the cheap porcelain-v2 proof above. Non-Git directories
    retain the canonical-manifest fallback for correctness; whole-walk cache
    reuse deliberately calls the Git-only helper so it never replaces one full
    non-Git walk with another hidden inside fingerprinting.
    """
    observed = _git_workspace_state_fingerprint(repo_root)
    if observed:
        return observed
    try:
        return walk_worktree(repo_root, size_cap=1).root()
    except OSError:
        return ""


@dataclass(frozen=True, slots=True)
class ToolAnswer:
    """One server-side tool result, already unwrapped from the envelope."""

    tool: str
    content: tuple[Mapping[str, Any], ...]
    is_error: bool
    view_revision: int
    degraded: bool = False
    degraded_reason: str = ""
    structured: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """What SessionStart achieved, for the one line the hook prints."""

    ok: bool
    reason: str
    session_id: str
    view_id: str
    view_revision: int
    warm: bool
    files: int
    uploaded_blobs: int
    uploaded_bytes: int
    manifest_chunks_sent: int
    elapsed_s: float
    local_fs: bool = False
    #: How many of ``files`` this session had to read and hash. The rest came
    #: from the walk cache, which is the whole reason a warm start is warm.
    hashed_files: int = 0
    #: Why no view was opened for a connected session: the root is not a git
    #: repository. Empty when the root is indexed.
    not_indexed: str = ""

    def one_line(self, url: str) -> str:
        """A single visible line. Never more, whatever happened."""
        if not self.ok:
            return f"LemonCrow: server-side tools unavailable ({self.reason}); client-side tools still work"
        if self.not_indexed:
            return (
                f"LemonCrow: {self.not_indexed}; code_search, search, relations and graph are "
                "unavailable here. Use grep, read and bash."
            )
        mode = "warm" if self.warm else "cold"
        return (
            f"LemonCrow: {url} view {self.view_id} rev {self.view_revision} "
            f"({mode}, {self.files} files, {self.hashed_files} hashed, "
            f"{self.uploaded_blobs} uploaded, {self.elapsed_s:.2f}s)"
        )


def _serialized[**P, R](
    method: Callable[Concatenate[RemoteSession, P], R],
) -> Callable[Concatenate[RemoteSession, P], R]:
    """Run a View-mutating method under the session lock.

    Server-routed calls already arrive one at a time (stdio is serial, and the
    gateway keeps them in one lane), but the gateway runs client-lane tools such
    as ``bash`` concurrently, and the sync that follows one must never advance
    the View underneath a call that is mid-flight.
    """

    @functools.wraps(method)
    def locked(self: RemoteSession, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._sync_lock:
            return method(self, *args, **kwargs)

    return locked


class RemoteSession:
    """One authenticated session against one LemonCrow server."""

    __slots__ = (
        "_blobs",
        "_cache",
        "_chunks",
        "_config",
        "_fingerprint_executor",
        "_generation_base_revision",
        "_git_base",
        "_git_overlay_paths",
        "_indexed",
        "_owns_view",
        "_publish_live_views",
        "_published_live_view_id",
        "_state",
        "_sync_lock",
        "_transport",
        "_unsynced_fingerprint",
        "_walk",
        "_workspace_fingerprint",
    )

    def __init__(
        self,
        config: ClientConfig,
        transport: HttpTransport | None = None,
        *,
        publish_live_view: bool = True,
    ) -> None:
        self._config = config
        if transport is not None:
            self._transport = transport
        elif (
            config.hosted
            and config.token
            and config.refresh_token
            and config.token_source
            in {
                str(managed_credentials_path(config.account_state_dir)),
                str(managed_credentials_path(config.state_dir)),
            }
        ):
            self._transport = RefreshingHttpTransport(
                config.url,
                timeout_s=config.request_timeout_s,
                state_dir=config.account_state_dir,
                legacy_state_dir=config.state_dir,
                access_token=config.token,
                refresh_token=config.refresh_token,
            )
        else:
            self._transport = HttpTransport(config.url, timeout_s=config.request_timeout_s)
        self._state = SessionState()
        self._blobs = BlobService(self._transport, self._state, config.repo_root, token=config.token)
        self._cache = ResolutionCache(config.resolution_cache_bytes)
        # Lazily created for indexed freshness work and always joined by
        # close(). Keeping it session-owned preserves the one-session/no-helper
        # audit contract while letting cache I/O, Git proof and server I/O
        # overlap where they are independent.
        self._fingerprint_executor: ThreadPoolExecutor | None = None
        self._generation_base_revision = ""
        self._git_base: GitBase | None = None
        self._git_overlay_paths: frozenset[str] = frozenset()
        self._walk: WalkResult | None = None
        self._chunks: tuple[ManifestChunk, ...] = ()
        self._indexed = is_index_candidate(config.repo_root)
        self._owns_view = False
        self._publish_live_views = publish_live_view
        self._published_live_view_id = ""
        # Re-entrant: call_tool refreshes, and a refresh may reopen the View.
        self._sync_lock = threading.RLock()
        # The tree an opportunistic sync last failed to push (server hung or
        # down), so the tail of every later command does not wait on it again.
        self._unsynced_fingerprint = ""

    # -- read-only state ------------------------------------------------ #

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def blobs(self) -> BlobService:
        return self._blobs

    @property
    def online(self) -> bool:
        return self._state.online

    @property
    def bootstrapped(self) -> bool:
        return self._state.bootstrapped

    @property
    def indexed(self) -> bool:
        """Whether this root is a git repository and so gets a server view."""
        return self._indexed

    @property
    def view_revision(self) -> int:
        return self._state.view_revision

    @property
    def cache_stats(self) -> CacheStats:
        return self._cache.stats

    def _publish_live_view_binding(self, fingerprint: str | None = None) -> None:
        if not self._publish_live_views or not self._owns_view:
            return
        if not self._state.view_id or not self._state.repo_id:
            return
        observed = fingerprint if fingerprint is not None else workspace_state_fingerprint(self._config.repo_root)
        if not observed:
            return
        self._workspace_fingerprint = observed
        try:
            write_live_view_binding(
                self._config.runtime_state_dir,
                self._config.repo_root,
                LiveViewBinding(
                    server_url=self._config.url,
                    repo_id=self._state.repo_id,
                    view_id=self._state.view_id,
                    view_revision=self._state.view_revision,
                    workspace_fingerprint=observed,
                ),
            )
            self._published_live_view_id = self._state.view_id
        except OSError:
            # The lease is an optimization. Losing it must never break tools.
            pass

    def adopt_live_view(self, binding: LiveViewBinding) -> None:
        """Bind this fresh HTTP session to an already-synced same-user View.

        Callers must prove the workspace fingerprint before adoption.  Review
        capture is then the server-side authority for whether the leased View
        still exists at the claimed revision.
        """

        if binding.server_url.rstrip("/") != self._config.url.rstrip("/"):
            raise ValueError("live View belongs to a different LemonCrow server")
        self._state.repo_id = binding.repo_id
        self._state.view_id = binding.view_id
        self._state.view_revision = binding.view_revision
        # The compact live-view lease predates RepositoryGeneration and does
        # not carry the immutable base id. On the first observed source change
        # we therefore reopen rather than risk applying a new HEAD as an overlay
        # to an unknown older generation.
        self._generation_base_revision = ""
        self._owns_view = False
        self._workspace_fingerprint = binding.workspace_fingerprint
        self._state.online = True

    # -- bootstrap ------------------------------------------------------ #

    def bootstrap(self, *, deadline_s: float | None = None) -> BootstrapReport:
        """Handshake, authenticate, register, open and sync one view.

        Fail-open by construction: every failure becomes a report with
        ``ok=False`` and a reason, because a hook that raises into the host is
        a hook that breaks the developer's session over a server outage.
        """
        started = time.monotonic()
        budget = deadline_s if deadline_s is not None else self._config.startup_budget_s
        try:
            self.handshake(timeout_s=budget)
            self.open_session(timeout_s=budget)
            # Outside a git repository the session stays open for tools that
            # need no repository, but nothing is walked, uploaded or indexed.
            opened: Mapping[str, Any] = self.open_view(timeout_s=budget) if self._indexed else {}
        except ClientError as exc:
            self._state.go_offline(exc.message)
            return BootstrapReport(
                ok=False,
                reason=f"{exc.server_code}: {exc.message}",
                session_id=self._state.session_id,
                view_id=self._state.view_id,
                view_revision=self._state.view_revision,
                warm=False,
                files=0,
                uploaded_blobs=0,
                uploaded_bytes=0,
                manifest_chunks_sent=0,
                elapsed_s=time.monotonic() - started,
            )
        self._state.bootstrapped = True
        self._state.online = True
        self._state.reason = ""
        return BootstrapReport(
            ok=True,
            reason="",
            session_id=self._state.session_id,
            view_id=self._state.view_id,
            view_revision=self._state.view_revision,
            warm=bool(opened.get("warm")),
            files=int(opened.get("entry_count", len(self._walk.files) if self._walk is not None else 0)),
            uploaded_blobs=int(opened.get("uploaded_blobs", 0)),
            uploaded_bytes=int(opened.get("uploaded_bytes", 0)),
            manifest_chunks_sent=int(opened.get("manifest_chunks_sent", 0)),
            elapsed_s=time.monotonic() - started,
            local_fs=self._state.local_fs,
            hashed_files=int(opened.get("hashed_files", 0)),
            not_indexed=(
                "" if self._indexed else f"{self._config.repo_root} is not a git repository, so it is not indexed"
            ),
        )

    def handshake(self, *, timeout_s: float | None = None) -> Mapping[str, Any]:
        """Negotiate before authenticating: N, or N-1 once; anything else is refused loudly."""

        def post(version: str) -> Mapping[str, Any]:
            return self._transport.post(
                "/v1/handshake", body=self._negotiation_body(version), timeout_s=timeout_s
            ).require()

        payload, version = negotiate_protocol(post)
        self._state.protocol_version = version
        server = payload.get("server")
        self._state.review_manifest = parse_review_manifest(version, server if isinstance(server, Mapping) else {})
        return payload

    def review_manifest(self, *, timeout_s: float | None = None) -> ReviewManifest:
        """The server's Review manifest: from the handshake, else ``GET /v1/reviews/manifest``.

        A server from before manifests has no such route; the handshake then
        settles the version and its deployment's baseline stands in.
        """
        if self._state.review_manifest is not None:
            return self._state.review_manifest
        response = self._transport.get("/v1/reviews/manifest", headers=self._bearer(), timeout_s=timeout_s)
        payload = {} if response.status == 404 else response.require()
        raw = payload.get("supported_protocol_versions")
        if not isinstance(raw, list):
            self.handshake(timeout_s=timeout_s)
        else:
            served = [version for version in raw if isinstance(version, str)]
            refusal = window_refusal(served)
            if refusal is not None:
                raise refusal
            current, previous = protocol_window()
            version = current if current in served else previous
            self._state.review_manifest = parse_review_manifest(version, payload)
        manifest = self._state.review_manifest
        if manifest is None:  # pragma: no cover - handshake always sets it
            raise ClientError(ErrorCode.INTERNAL, "the server published no Review manifest")
        return manifest

    def open_session(self, *, timeout_s: float | None = None) -> Mapping[str, Any]:
        """Register the session. Hosted mode requires a credential; loopback local mode does not."""
        if self._config.hosted and not self._config.authenticated:
            raise ClientError(
                ErrorCode.UNAUTHENTICATED,
                (
                    "no credential: run `lc auth login`, set LEMONCROW_TOKEN, or have IT provision "
                    f"{self._config.state_dir / 'token'} with mode 0600"
                ),
                action=AgentAction.REAUTHENTICATE,
            )

        def post(version: str) -> Mapping[str, Any]:
            return self._transport.post(
                "/v1/sessions",
                body=self._negotiation_body(version),
                headers=self._bearer(),
                timeout_s=timeout_s,
            ).require()

        if self._state.protocol_version:
            payload = post(self._state.protocol_version)
        else:
            payload, self._state.protocol_version = negotiate_protocol(post)
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ClientError(ErrorCode.INTERNAL, "server opened a session with no id", action=AgentAction.ABANDON)
        self._state.session_id = session_id
        self._state.client_seq = 0
        self._cache.clear()
        self._state.server_digests.clear()
        raw_caps = payload.get("capabilities")
        self._state.capabilities = (
            frozenset(str(name) for name in raw_caps if isinstance(name, str))
            if isinstance(raw_caps, list)
            else frozenset()
        )
        self._state.local_fs = bool(payload.get("local_fs"))
        issued = payload.get("local_fs_proof_token")
        self._state.local_fs_proof = issued if isinstance(issued, str) else ""
        limits = payload.get("limits")
        if isinstance(limits, Mapping):
            self._state.adopt_limits(limits)
        self._state.online = True
        return payload

    @_serialized
    def open_view(
        self,
        *,
        timeout_s: float | None = None,
        upload_missing_content: bool = True,
    ) -> Mapping[str, Any]:
        """Negotiate the immutable Git base, then synchronize only local changes."""
        root = self._config.repo_root
        before = workspace_state_fingerprint(root)
        chunk_rows = min(_MAX_CHUNK_ROWS, self._state.max_manifest_entries_per_chunk)
        proof = peek_walk_cache_proof(self._config.runtime_state_dir, root)
        if proof.complete and proof.workspace_fingerprint == before and proof.manifest_chunk_rows == chunk_rows:
            body: dict[str, Any] = {
                "repo_identity": repo_identity(root).to_wire(),
                "base_revision": proof.generation_revision,
                "manifest_root": proof.manifest_root,
                "manifest_chunks": list(proof.manifest_chunk_ids),
            }
            grant = self._local_fs_offer()
            if grant is not None:
                body["local_fs"] = grant
            opened = dict(
                self._transport.post(
                    "/v1/views/open",
                    body=body,
                    headers=self._auth(),
                    timeout_s=timeout_s,
                ).require()
            )
            if (
                opened.get("warm") is True
                and not opened.get("needs_manifest")
                and not opened.get("need_manifest_chunks")
                and not opened.get("missing_digests")
                and isinstance(opened.get("view_id"), str)
                and opened.get("view_id")
                and isinstance(opened.get("repo_id"), str)
                and opened.get("repo_id")
            ):
                self._state.view_id = opened["view_id"]
                self._state.repo_id = opened["repo_id"]
                self._state.view_revision = int(opened.get("view_revision", 0) or 0)
                self._owns_view = True
                self._cache.prune_scope(self._state.session_id, self._state.view_id, self._state.view_revision)
                cap = opened.get("content_size_cap")
                if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
                    self._state.content_size_cap = cap
                self._git_base = None
                self._git_overlay_paths = frozenset()
                self._generation_base_revision = proof.generation_revision
                self._walk = None
                self._chunks = ()
                self._state.known.clear()
                self._workspace_fingerprint = before
                self._publish_live_view_binding(before)
                opened.update(
                    manifest_chunks_sent=0,
                    uploaded_blobs=0,
                    uploaded_bytes=0,
                    hashed_files=0,
                    content_sync_skipped=False,
                )
                return opened
            if isinstance(opened.get("view_id"), str) and opened["view_id"]:
                self._transport.post(
                    f"/v1/views/{opened['view_id']}/close",
                    body={},
                    headers=self._auth(with_revision=False),
                    timeout_s=2.0,
                ).require()
        cache = load_walk_cache(self._config.runtime_state_dir, root)
        cache.begin()
        base = committed_base(root, cache, max_files=_MAX_INDEX_FILES)
        dirty = dirty_paths(root, max_files=_MAX_INDEX_FILES)
        overlay = overlay_entries(root, tuple(sorted(dirty)))
        if workspace_state_fingerprint(root) != before:
            raise ClientError(
                ErrorCode.VIEW_REVISION_STALE, "working tree changed while opening the View", retryable=True
            )
        chunk_rows = min(_MAX_CHUNK_ROWS, self._state.max_manifest_entries_per_chunk)
        self._chunks = chunk_manifest(tuple(base.entries.values()), max_rows=chunk_rows)
        body = {
            "repo_identity": repo_identity(root).to_wire(),
            "base_revision": base.revision,
            "manifest_root": manifest_root(tuple(base.entries.values())),
            "manifest_chunks": [chunk.chunk_id for chunk in self._chunks],
        }
        grant = self._local_fs_offer()
        if grant is not None:
            body["local_fs"] = grant
        opened = dict(
            self._transport.post(
                "/v1/views/open",
                body=body,
                headers=self._auth(),
                timeout_s=timeout_s,
            ).require()
        )
        view_id, repo_id = opened.get("view_id"), opened.get("repo_id")
        if not isinstance(view_id, str) or not view_id or not isinstance(repo_id, str) or not repo_id:
            raise ClientError(ErrorCode.VIEW_UNKNOWN, "server opened an invalid View")
        self._state.view_id = view_id
        self._state.repo_id = repo_id
        self._state.view_revision = int(opened.get("view_revision", 0) or 0)
        self._owns_view = True
        self._cache.prune_scope(self._state.session_id, view_id, self._state.view_revision)
        cap = opened.get("content_size_cap")
        if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
            self._state.content_size_cap = cap
        sent = self._blobs.send_chunks(
            self._chunks,
            _string_list(opened.get("need_manifest_chunks")),
            timeout_s=timeout_s,
        )
        if sent:
            state = self._transport.post(
                f"/v1/views/{view_id}/missing",
                body={"digests": []},
                headers=self._auth(with_revision=False),
                timeout_s=timeout_s,
            ).require()
            opened["missing_digests"] = state.get("outstanding_digests", [])
        missing = _string_list(opened.get("missing_digests"))
        stored = plaintext = 0
        if upload_missing_content:
            wanted = {base.sources[digest][0]: base.sources[digest][1] for digest in missing if digest in base.sources}
            by_oid = {base.sources[digest][0]: digest for digest in missing if digest in base.sources}
            supplied = 0
            for objects in object_batches(root, wanted):
                supplied += len(objects)
                payloads = {by_oid[oid]: (data, base.sources[by_oid[oid]][2]) for oid, data in objects.items()}
                count, size = self._blobs.upload_payloads(
                    tuple(payloads),
                    payloads,
                    timeout_s=timeout_s,
                    completion_check=supplied == len(wanted) and not dirty,
                )
                stored += count
                plaintext += size
        self._state.known = dict(base.entries)
        changed = tuple(entry for path, entry in overlay.items() if base.entries.get(path) != entry)
        removed = tuple(path for path in sorted(dirty) if path in base.entries and path not in overlay)
        sources = {entry.content_digest: root / entry.path for entry in changed}
        report = self._blobs.push_manifest_delta(
            changed,
            removed,
            sources=sources,
            upload_content=upload_missing_content,
            timeout_s=timeout_s,
        )
        stored += report.uploaded
        plaintext += report.bytes_sent
        # Keep immutable rows separately: restoring a dirty file never needs to
        # read its clean working-tree bytes or rediscover the complete manifest.
        self._git_base = base
        self._git_overlay_paths = frozenset(entry.path for entry in changed) | frozenset(removed)
        self._generation_base_revision = base.revision
        self._workspace_fingerprint = before
        self._publish_live_view_binding(before)
        effective = self._state.known
        self._walk = WalkResult(
            files=tuple(
                WalkedFile(
                    entry.path,
                    entry.content_digest,
                    entry.parser_profile,
                    entry.mode,
                    entry.size,
                    entry.size <= self._state.content_size_cap,
                )
                for entry in effective.values()
            ),
            ignored=0,
            symlinks=0,
            oversize=sum(entry.size > self._state.content_size_cap for entry in effective.values()),
            unreadable=0,
            digests=WalkCacheStats(hashed=base.hashed + len(overlay)),
        )
        persist_walk_cache(
            cache,
            self._config.runtime_state_dir,
            workspace_fingerprint=before if not dirty else "",
            manifest_entry_count=0,
            generation_entry_count=len(base.entries),
            generation_revision=base.revision,
            manifest_root_value=body["manifest_root"],
            manifest_chunk_rows=chunk_rows,
            manifest_chunk_ids=tuple(chunk.chunk_id for chunk in self._chunks),
        )
        opened.update(
            manifest_chunks_sent=sent,
            uploaded_blobs=stored,
            uploaded_bytes=plaintext,
            content_sync_skipped=not upload_missing_content and bool(missing or changed),
            hashed_files=base.hashed + len(overlay),
            view_revision=self._state.view_revision,
            entry_count=len(effective),
            generation_base_revision=base.revision,
            initial_overlay_entries=len(changed),
            initial_overlay_removed=len(removed),
        )
        return opened

    def _refresh_git_overlay(self, observed: str) -> bool:
        root = self._config.repo_root
        base = self._git_base
        assert base is not None
        if git_head(root) != base.revision:
            old_view, old_owned = self._state.view_id, self._owns_view
            self.open_view()
            if old_owned and old_view != self._state.view_id:
                self._transport.post(
                    f"/v1/views/{old_view}/close",
                    body={},
                    headers=self._auth(with_revision=False),
                    timeout_s=2.0,
                ).require()
            return True
        dirty = dirty_paths(root, max_files=_MAX_INDEX_FILES)
        current = overlay_entries(root, tuple(sorted(dirty)))
        candidates = dirty | self._git_overlay_paths
        entries: list[ManifestEntry] = []
        removed: list[str] = []
        for path in sorted(candidates):
            entry = current.get(path) if path in dirty else base.entries.get(path)
            if entry is None:
                if path in self._state.known:
                    removed.append(path)
            elif self._state.known.get(path) != entry:
                entries.append(entry)
        if workspace_state_fingerprint(root) != observed:
            raise ClientError(ErrorCode.VIEW_REVISION_STALE, "working tree changed while synchronizing", retryable=True)
        # Restored entries already belong to the base; only dirty contents can
        # require an upload. Reuse the declared-base possession boundary.
        sources = {entry.content_digest: root / entry.path for entry in entries if entry.path in dirty}
        report = self._blobs.push_manifest_delta(entries, removed, sources=sources)
        self._cache.prune_scope(self._state.session_id, self._state.view_id, report.view_revision)
        self._git_overlay_paths = frozenset(path for path in dirty if current.get(path) != base.entries.get(path))
        self._workspace_fingerprint = observed
        self._publish_live_view_binding(observed)
        return bool(entries or removed)

    def _send_large_review_source(self, root: Path, *, timeout_s: float | None = None) -> tuple[int, int]:
        """Best effort: a file that cannot be sent is reported missing by Ask, and never fails the capture."""

        try:
            return self._blobs.large_source_from(root, timeout_s=timeout_s)
        except ClientError:
            return 0, 0

    @_serialized
    def upload_large_review_source(self, *, timeout_s: float | None = None) -> tuple[int, int]:
        """Send this checkout's files above the index cap that the bound View's Review lacks.

        A Review's source tree names every file of its View, while syncing a View
        uploads only what the index reads. Bytes are checked against the View's
        digests, so a file edited since the View was pinned is not sent.
        """

        return self._send_large_review_source(self._config.repo_root, timeout_s=timeout_s)

    def open_review_snapshot(
        self,
        snapshot_root: Path,
        *,
        source_revision: str,
        timeout_s: float | None = None,
        allow_oversize: bool = False,
    ) -> Mapping[str, Any]:
        """Open a complete immutable View for the exact Review new side.

        ``allow_oversize`` admits files the blob transport cannot carry: they keep
        their metadata without bytes, and files between the index cap and that
        ceiling are sent once per repository. Runtime materialization still refuses
        missing content. The default continues to require a complete snapshot.

        ``snapshot_root`` is already frozen by the Review capture layer, so this
        method never walks the mutable developer checkout. Unlike an ordinary
        indexing View, the runtime snapshot must be complete: truncated or
        unreadable walks, symlinks the View protocol cannot faithfully encode,
        and files above the blob transport ceiling are refused. Files above the
        normal search/index cap are proactively uploaded when the blob transport
        can carry them, because runtime reconstruction needs their exact bytes.
        """

        root = snapshot_root.expanduser().resolve()
        # Review execution needs exact bytes, not merely indexable content.
        # Walk up to the transport's real single-blob ceiling and proactively
        # upload files above the normal search/index cap. Only files that cannot
        # be represented by the sync protocol at all are rejected.
        walk = walk_worktree(
            root, size_cap=self._state.content_size_cap if allow_oversize else self._state.max_blob_bytes
        )
        if walk.truncated or walk.unreadable or (walk.oversize and not allow_oversize) or walk.symlinks:
            raise ClientError(
                ErrorCode.PAYLOAD_INVALID,
                "the frozen Review snapshot cannot be uploaded completely",
                details={
                    "truncated": bool(walk.truncated),
                    "unreadable": walk.unreadable,
                    "oversize": walk.oversize,
                    "symlinks": walk.symlinks,
                    "max_blob_bytes": self._state.max_blob_bytes,
                },
                action=AgentAction.FIX_REQUEST,
            )
        entries = walk.entries()
        self._chunks = chunk_manifest(
            entries,
            max_rows=min(_MAX_CHUNK_ROWS, self._state.max_manifest_entries_per_chunk),
        )
        body: dict[str, Any] = {
            "repo_identity": repo_identity(self._config.repo_root).to_wire(),
            "base_revision": source_revision or "review-snapshot",
            "manifest_root": manifest_root(entries),
            "manifest_chunks": [chunk.chunk_id for chunk in self._chunks],
            # Nothing renews a snapshot View once `lc review` exits, and the
            # Reader navigates through it long after; the server caps the ask.
            "lease_s": _REVIEW_SNAPSHOT_LEASE_S,
        }
        # A Review snapshot still offers the developer checkout only when the
        # operator explicitly enabled same-host local filesystem access. The
        # View itself remains the immutable source of runtime bytes.
        grant = self._local_fs_offer()
        if grant is not None:
            body["local_fs"] = grant
        opened = dict(
            self._transport.post(
                "/v1/views/open",
                body=body,
                headers=self._auth(),
                timeout_s=timeout_s,
            ).require()
        )
        view_id = opened.get("view_id")
        repo_id = opened.get("repo_id")
        if not isinstance(view_id, str) or not view_id:
            raise ClientError(
                ErrorCode.VIEW_UNKNOWN,
                "server opened a Review snapshot with no view id",
                action=AgentAction.REOPEN_VIEW,
            )
        if not isinstance(repo_id, str) or not repo_id:
            raise ClientError(
                ErrorCode.INTERNAL,
                "server opened a Review snapshot with no repository id",
                action=AgentAction.ABANDON,
            )
        self._state.view_id = view_id
        self._owns_view = True
        self._state.repo_id = repo_id
        self._state.view_revision = int(opened.get("view_revision", 0) or 0)

        sent = self._blobs.send_chunks(
            self._chunks, _string_list(opened.get("need_manifest_chunks")), timeout_s=timeout_s
        )
        if sent:
            state = self._transport.post(
                f"/v1/views/{view_id}/missing",
                body={"digests": []},
                headers=self._auth(with_revision=False),
                timeout_s=timeout_s,
            ).require()
            opened["missing_digests"] = state.get("outstanding_digests", state.get("missing_digests", []))

        missing = set(_string_list(opened.get("missing_digests")))
        # The ordinary View protocol intentionally does not request blobs above
        # content_size_cap because they are not indexed. A Review runtime is
        # stricter: it needs those bytes to reconstruct the exact filesystem.
        if not allow_oversize:
            missing.update(entry.content_digest for entry in entries if entry.size > self._state.content_size_cap)
        sources = {file.content_digest: root / file.path for file in walk.uploadable()}
        stored, plaintext = self._blobs.upload_digests(tuple(sorted(missing)), sources, timeout_s=timeout_s)
        if allow_oversize:
            large_stored, large_bytes = self._send_large_review_source(root, timeout_s=timeout_s)
            stored += large_stored
            plaintext += large_bytes
        for entry in entries:
            self._state.known[entry.path] = entry
        opened["manifest_chunks_sent"] = sent
        opened["uploaded_blobs"] = stored
        opened["uploaded_bytes"] = plaintext
        opened["oversize_paths"] = walk.oversize
        self._state.online = True
        return opened

    # -- the tool path -------------------------------------------------- #

    def capture_review(
        self,
        *,
        packet: Mapping[str, Any],
        new_blobs: Mapping[str, str],
        source_ref: str,
        title: str = "",
        base_view_id: str = "",
        base_view_revision: int = 0,
        repo_id: str | None = None,
        restore_archived: bool = False,
        source_fingerprint: str = "",
        legacy_source_fingerprint: str = "",
        workspace_branch: str = "",
        force_new_review: bool = False,
        continue_review_id: str = "",
        reuse_closed_review: bool = False,
        review_scope: str = "",
        timeout_s: float | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> Mapping[str, Any]:
        """Capture the currently synced view as one durable Review revision.

        The Review server validates every changed UTF-8 blob against the exact
        view revision this client just committed. ``repo_id`` is optional only
        for compatibility with callers that already resolved the repository;
        when both are known they must agree with the bound view.
        """

        bound_repo = self._state.repo_id
        target_repo = repo_id or bound_repo
        if not self._state.view_id or not target_repo:
            raise ClientError(
                ErrorCode.VIEW_UNKNOWN,
                "no synced view is available for Review capture",
                action=AgentAction.REOPEN_VIEW,
            )
        if bound_repo and repo_id and bound_repo != repo_id:
            raise ClientError(
                ErrorCode.VIEW_UNKNOWN,
                "the requested Review repository does not match the bound source view",
                action=AgentAction.REOPEN_VIEW,
            )
        body: dict[str, Any] = {
            "view_id": self._state.view_id,
            "view_revision": self._state.view_revision,
            "source_ref": source_ref,
            "title": title,
            "restore_archived": restore_archived,
            "packet": dict(packet),
            "new_blobs": dict(new_blobs),
            "base_view_id": base_view_id,
            "base_view_revision": base_view_revision,
            **({"source_fingerprint": source_fingerprint} if source_fingerprint else {}),
            **({"legacy_source_fingerprint": legacy_source_fingerprint} if legacy_source_fingerprint else {}),
            **({"workspace_branch": workspace_branch} if workspace_branch else {}),
            **({"force_new_review": True} if force_new_review else {}),
            **({"continue_review_id": continue_review_id} if continue_review_id else {}),
            **({"reuse_closed_review": True} if reuse_closed_review else {}),
            **({"review_scope": review_scope} if review_scope else {}),
        }
        # Fail closed before sending: a packet schema or an option the server
        # would not apply is a capture it could only make in part.
        manifest = self.review_manifest(timeout_s=self._config.request_timeout_s)
        manifest.require_capture("packet", body)
        manifest.require_packet(packet)
        payload = self._post_review(
            f"/v1/repos/{quote(target_repo, safe='')}/reviews",
            body,
            timeout_s if timeout_s is not None else self._config.tool_timeout_s,
            progress=progress,
        )
        self._state.online = True
        return payload

    def capture_review_from_views(
        self,
        *,
        range_spec: Mapping[str, Any],
        source_ref: str,
        gitlinks: Mapping[str, Any] | None = None,
        title: str = "",
        base_view_id: str = "",
        base_view_revision: int = 0,
        repo_id: str | None = None,
        with_impact: bool = True,
        restore_archived: bool = False,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """Ask the server to derive a Review packet from exact base/head Views.

        Unlike :meth:`capture_review`, this method ships no Review engine output
        and no changed-file payloads. The currently bound View is the immutable
        new side; ``base_view_id`` names the immutable old side. The server owns
        packet/diff/ranking derivation from those already-uploaded bytes.
        """

        bound_repo = self._state.repo_id
        target_repo = repo_id or bound_repo
        if not self._state.view_id or not target_repo:
            raise ClientError(
                ErrorCode.VIEW_UNKNOWN,
                "no synced view is available for Review capture",
                action=AgentAction.REOPEN_VIEW,
            )
        if bound_repo and repo_id and bound_repo != repo_id:
            raise ClientError(
                ErrorCode.VIEW_UNKNOWN,
                "the requested Review repository does not match the bound source view",
                action=AgentAction.REOPEN_VIEW,
            )
        body = {
            "view_id": self._state.view_id,
            "view_revision": self._state.view_revision,
            "source_ref": source_ref,
            "title": title,
            "restore_archived": restore_archived,
            "derive_packet": True,
            "with_impact": with_impact,
            "range": dict(range_spec),
            "gitlinks": dict(gitlinks or {}),
            "base_view_id": base_view_id,
            "base_view_revision": base_view_revision,
        }
        self.review_manifest(timeout_s=self._config.request_timeout_s).require_capture("derived", body)
        path = f"/v1/repos/{quote(target_repo, safe='')}/reviews"
        payload = self._await_capture(
            path,
            self._transport.post(
                path,
                body=body,
                headers={**self._auth(), "Prefer": "respond-async"},
                timeout_s=timeout_s if timeout_s is not None else self._config.tool_timeout_s,
            ).require(),
        )
        self._state.online = True
        return payload

    def enrich_review_projection(
        self,
        *,
        repo_id: str,
        review_id: str,
        revision_id: str,
        packet: Mapping[str, Any],
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """Enrich analysis metadata on one exact already-visible revision."""

        self.review_manifest(timeout_s=self._config.request_timeout_s).require_packet(packet)
        return self._transport.post(
            (
                f"/v1/repos/{quote(repo_id, safe='')}/reviews/{quote(review_id, safe='')}"
                f"/revisions/{quote(revision_id, safe='')}/projection"
            ),
            body={"packet": dict(packet)},
            headers=self._auth(),
            timeout_s=timeout_s if timeout_s is not None else self._config.tool_timeout_s,
        ).require()

    def update_review_preparation(
        self,
        *,
        repo_id: str,
        review_id: str,
        preparation_id: str,
        state: str,
        detail: str = "",
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """Publish progressive startup state for the browser Reader."""

        return self._transport.post(
            f"/v1/repos/{quote(repo_id, safe='')}/reviews/{quote(review_id, safe='')}/preparation",
            body={
                "preparation_id": preparation_id,
                "state": state,
                "detail": detail,
            },
            headers=self._auth(),
            timeout_s=timeout_s if timeout_s is not None else self._config.request_timeout_s,
        ).require()

    def attach_review_base(
        self,
        *,
        repo_id: str,
        review_id: str,
        revision_id: str,
        base_view_id: str,
        base_view_revision: int,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """Attach a frozen base View after the source revision is already reviewable.

        Progressive local Review publishes the exact changed source first so the
        human can start reading immediately. Compare/runtime reconstruction still
        needs the complete base tree, but that tree is metadata enrichment rather
        than part of the source identity the reviewer is looking at.
        """

        return self._transport.post(
            (
                f"/v1/repos/{quote(repo_id, safe='')}/reviews/{quote(review_id, safe='')}"
                f"/revisions/{quote(revision_id, safe='')}/base-view"
            ),
            body={
                "base_view_id": base_view_id,
                "base_view_revision": base_view_revision,
            },
            headers=self._auth(),
            timeout_s=timeout_s if timeout_s is not None else self._config.tool_timeout_s,
        ).require()

    def _post_review(
        self, path: str, body: dict[str, Any], timeout_s: float, *, progress: Callable[[str], None] | None = None
    ) -> Mapping[str, Any]:
        """One request when the capture fits the server's limit, else a chunked upload.

        Chunked: the first request opens a draft with the packet head and the
        first files, each later one appends the next files, and the last append
        commits -- so N requests for N chunks, never an extra open or commit.
        """
        budget = self._state.max_request_bytes - _REQUEST_HEADROOM
        capture_headers = {**self._auth(), "Prefer": "respond-async"}
        if _json_size(body) <= budget:
            return self._await_capture(
                path,
                self._transport.post(path, body=body, headers=capture_headers, timeout_s=timeout_s).require(),
                progress=progress,
            )
        packet = dict(body["packet"])
        head = {**body, "packet": {**packet, "files": []}, "new_blobs": {}, "draft": True}
        head_size = _json_size(head)
        if head_size > budget:
            raise ClientError(
                ErrorCode.REQUEST_TOO_LARGE,
                f"the Review packet without its file list is {head_size:,} bytes; the request limit is {budget:,}",
                action=AgentAction.SPLIT_REQUEST,
            )
        chunks = _pack_review_chunks(
            list(packet.get("files") or []),
            body["new_blobs"],
            first_room=budget - head_size,
            later_room=budget - _APPEND_ENVELOPE,
        )
        if len(chunks) == 1:
            return self._await_capture(
                path,
                self._transport.post(path, body=body, headers=capture_headers, timeout_s=timeout_s).require(),
                progress=progress,
            )
        first_files, first_blobs = chunks[0]
        opened = self._transport.post(
            path,
            body={**head, "packet": {**packet, "files": first_files}, "new_blobs": first_blobs},
            headers=self._auth(),
            timeout_s=timeout_s,
        ).require()
        draft_id = opened.get("draft_id")
        if not isinstance(draft_id, str) or not draft_id:
            raise ClientError(
                ErrorCode.INTERNAL,
                "the server did not open a Review draft for a chunked capture",
                action=AgentAction.ABANDON,
            )
        draft_path = f"{path}/drafts/{quote(draft_id, safe='')}"
        answer: Mapping[str, Any] = opened
        for seq in range(1, len(chunks)):
            files, blobs = chunks[seq]
            final = seq == len(chunks) - 1
            answer = self._transport.post(
                draft_path,
                body={"seq": seq, "files": files, "new_blobs": blobs, "final": final},
                headers=capture_headers if final else self._auth(),
                timeout_s=timeout_s,
            ).require()
        return self._await_capture(path, answer, progress=progress)

    def _await_capture(
        self, path: str, answer: Mapping[str, Any], *, progress: Callable[[str], None] | None = None
    ) -> Mapping[str, Any]:
        return await_capture(self._transport, self._auth, self._config.request_timeout_s, path, answer, progress)

    def _unfillable_paths_error(self, tool: str, unfillable: Mapping[str, str]) -> ClientError:
        """A path argument no upload can satisfy, said so -- with where to look.

        Reported as ``blob_missing`` it would read as a sync failure, and the
        agent would retry or re-sync a workspace that is already in sync. The
        fix is the argument, usually a path relative to the wrong directory.
        """
        root = self._config.repo_root
        shown = list(unfillable.items())[:_MAX_REPORTED_PATHS]
        candidates: tuple[str, ...] | None = None
        parts: list[str] = []
        described: dict[str, dict[str, Any]] = {}
        for path, reason in shown:
            hints: tuple[str, ...] = ()
            if reason == "absent":
                if candidates is None:
                    candidates = self._path_candidates()
                hints = suggest_paths(path, candidates)
            text = f"'{path}' {_UNFILLABLE_REASONS.get(reason, 'cannot be uploaded')}"
            if hints:
                text += " (did you mean " + ", ".join(f"'{hint}'" for hint in hints) + "?)"
            parts.append(text)
            described[path] = {"reason": reason, "suggestions": list(hints)}
        if len(unfillable) > len(shown):
            parts.append(f"+{len(unfillable) - len(shown)} more")
        return ClientError(
            ErrorCode.PATH_NOT_ADDRESSABLE,
            f"{tool}: " + "; ".join(parts) + f". Paths are relative to the workspace root {root}; "
            "fix the path argument and call again -- no upload can satisfy it.",
            details={"tool": tool, "workspace_root": str(root), "paths": described},
            action=AgentAction.FIX_REQUEST,
        )

    def _path_candidates(self) -> tuple[str, ...]:
        """Indexed paths to suggest from: the known manifest, else git's listing.

        A metadata-only warm open holds no local manifest, so it falls back to
        one bounded read-only ``git ls-files``, run only on this error path.
        """
        if self._state.known:
            return tuple(self._state.known)
        return _git_listed_paths(self._config.repo_root)

    def _refresh_view_if_source_changed(self, observed: str | None = None) -> bool:
        """Refresh a long-lived View after out-of-band workspace changes.

        Returns ``True`` only when the server View actually advanced. ``observed``
        lets :meth:`call_tool` run the cheap Git probe concurrently with the
        server request. If that speculative request raced a real source change,
        the caller discards it and retries after this method synchronizes the
        View, preserving the same freshness guarantee as the old serial path.
        """
        if not self._state.view_bound:
            return False
        observed = observed if observed is not None else workspace_state_fingerprint(self._config.repo_root)
        if not observed:
            return False
        if not self._workspace_fingerprint and not self._git_overlay_paths:
            self._workspace_fingerprint = observed
            return False
        if observed == self._workspace_fingerprint:
            return False

        if self._git_base is not None:
            return self._refresh_git_overlay(observed)

        if self._walk is None:
            # A metadata-only warm open intentionally has no 250k-row local
            # baseline. Once the source changes, reopen through the ordinary
            # full path instead of pretending an empty known-map is a delta.
            old_view_id = self._state.view_id
            old_owned = self._owns_view
            self.open_view()
            if old_owned and old_view_id and old_view_id != self._state.view_id:
                try:
                    self._transport.post(
                        f"/v1/views/{old_view_id}/close",
                        body={},
                        headers=self._auth(with_revision=False),
                        timeout_s=2.0,
                    )
                except ClientError:
                    pass
            return True

        cache = load_walk_cache(self._config.runtime_state_dir, self._config.repo_root)
        walk = walk_worktree(
            self._config.repo_root,
            size_cap=self._state.content_size_cap,
            max_files=_MAX_INDEX_FILES,
            cache=cache,
        )
        entries = walk.entries()
        generation = plan_repository_generation(
            self._config.repo_root,
            entries,
            content_size_cap=self._state.content_size_cap,
        )
        if not self._generation_base_revision or generation.base_revision != self._generation_base_revision:
            # A commit/checkout/rebase changes the immutable base. Do not smear
            # that transition into the old generation as a giant overlay: open
            # a fresh generation-bound View, then retire the old lease.
            old_view_id = self._state.view_id
            old_owned = self._owns_view
            self.open_view()
            if old_owned and old_view_id and old_view_id != self._state.view_id:
                try:
                    self._transport.post(
                        f"/v1/views/{old_view_id}/close",
                        body={},
                        headers=self._auth(with_revision=False),
                        timeout_s=2.0,
                    )
                except ClientError:
                    pass
            return True

        current = {entry.path: entry for entry in entries}
        changed = tuple(path for path, entry in current.items() if self._state.known.get(path) != entry)
        removed = tuple(path for path in self._state.known if path not in current)

        # Existing/new paths are re-read by the ordinary push path so executable
        # mode and oversize policy stay identical to edits. Paths that vanished
        # from the canonical manifest (delete or newly ignored) need explicit
        # tombstones even when the file still exists on disk.
        advanced = bool(changed or removed)
        if changed:
            self.push_paths(changed)
        if removed:
            report = self._blobs.push_snapshot({}, removed_paths=removed)
            self._cache.prune_scope(self._state.session_id, self._state.view_id, report.view_revision)
        self._walk = walk
        self._workspace_fingerprint = observed
        self._publish_live_view_binding(observed)
        persist_walk_cache(
            cache,
            self._config.runtime_state_dir,
            workspace_fingerprint=self._workspace_fingerprint,
            manifest_entry_count=len(entries),
        )
        return advanced

    @_serialized
    def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> ToolAnswer:
        """Dispatch one revision-bound tool call, with server-validated reuse.

        Cached payloads are never returned merely because the client has them.
        The client sends the opaque validator back to the authenticated server;
        only a ``reuse`` answer for this exact revision lets the cached payload
        satisfy the call. A miss, stale revision or unsupported server follows
        the normal execution path.
        """
        if not self._state.session_id:
            raise ClientError(
                ErrorCode.SERVER_SESSION_UNAVAILABLE,
                self._state.reason or "no LemonCrow session; server-side tools are unavailable",
                details={"tool": tool},
                retryable=True,
                action=AgentAction.RETRY_LATER,
            )
        if not self._indexed and tool in _INDEX_VIEW_TOOLS:
            # Without a view the server would answer from an empty tree, and an
            # agent reading empty results keeps searching.
            raise not_indexed_error(self._config.repo_root, tool)
        fingerprint: Future[str] | None = None
        if _reads_view(tool) and self._state.view_bound:
            if self._fingerprint_executor is None:
                self._fingerprint_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lc-fingerprint")
            fingerprint = self._fingerprint_executor.submit(workspace_state_fingerprint, self._config.repo_root)
            route = ROUTES.get(tool)
            if route is None or not route.retry_safe:
                # Only a call that is safe to repeat may be sent speculatively and
                # re-sent after the View moves; anything else (a memory write, a
                # trace, Review evidence) syncs the View first and is sent once.
                self._refresh_view_if_source_changed(fingerprint.result())
                fingerprint = None

        wire_arguments = dict(arguments)
        if tool == "code_search":
            wire_arguments["_client_hydrate"] = True
        key = self._cache_key(tool, wire_arguments)
        cached = self._cache.get(key)
        payload = self._dispatch(tool, wire_arguments, reuse_validator=cached.validator if cached is not None else "")

        if fingerprint is not None and self._refresh_view_if_source_changed(fingerprint.result()):
            # The first request was intentionally speculative. Its revision is
            # now stale relative to the source tree, so consume none of its
            # reuse/blob/cache effects and execute once against the synchronized
            # View. A no-op fingerprint change does not pay for a second call.
            key = self._cache_key(tool, wire_arguments)
            cached = self._cache.get(key)
            payload = self._dispatch(
                tool, wire_arguments, reuse_validator=cached.validator if cached is not None else ""
            )

        if payload.get("reuse") is True:
            validator = payload.get("reuse_validator")
            revision = payload.get("view_revision")
            if (
                cached is not None
                and isinstance(validator, str)
                and validator == cached.validator
                and revision == self._state.view_revision
            ):
                if tool != "code_search":
                    return _unwrap(tool, cached.payload, self._state.view_revision)
                payload = cached.payload
            else:
                # The server said reuse but this process no longer owns that entry.
                # Ask once without a validator rather than fabricating an answer.
                self._state.record_recovery(
                    "reuse_reject",
                    "retrying",
                    tool=tool,
                    from_revision=self._state.view_revision,
                )
                try:
                    payload = self._dispatch(tool, wire_arguments)
                except ClientError as exc:
                    self._state.record_recovery(
                        "reuse_reject",
                        "failed",
                        error_code=exc.code.value,
                        tool=tool,
                        from_revision=self._state.view_revision,
                    )
                    raise
                self._state.record_recovery(
                    "reuse_reject",
                    "recovered",
                    tool=tool,
                    from_revision=self._state.view_revision,
                    to_revision=self._state.view_revision,
                )

        need = payload.get("need")
        if isinstance(need, list) and need:
            unfillable = self._blobs.unfillable([str(path) for path in need])
            if unfillable:
                # A fill would change nothing and the retry would come back as
                # a misleading ``blob_missing``; name the bad argument instead.
                self._state.record_recovery(
                    "blob_fill",
                    "failed",
                    error_code=ErrorCode.PATH_NOT_ADDRESSABLE.value,
                    tool=tool,
                    from_revision=self._state.view_revision,
                    count=len(unfillable),
                )
                raise self._unfillable_paths_error(tool, unfillable)
            self._state.record_recovery(
                "blob_fill",
                "retrying",
                tool=tool,
                from_revision=self._state.view_revision,
                count=len(need),
            )
            try:
                self._blobs.fill([str(path) for path in need])
                self._cache.prune_scope(self._state.session_id, self._state.view_id, self._state.view_revision)
                payload = self._dispatch(tool, wire_arguments)
            except ClientError as exc:
                self._state.record_recovery(
                    "blob_fill",
                    "failed",
                    error_code=exc.code.value,
                    tool=tool,
                    from_revision=self._state.view_revision,
                    count=len(need),
                )
                raise
            if isinstance(payload.get("need"), list) and payload.get("need"):
                self._state.record_recovery(
                    "blob_fill",
                    "failed",
                    error_code=ErrorCode.BLOB_MISSING.value,
                    tool=tool,
                    from_revision=self._state.view_revision,
                    count=len(payload["need"]),
                )
                raise ClientError(
                    ErrorCode.BLOB_MISSING,
                    "the server still lacks content for this call after one fill",
                    details={"tool": tool, "need": len(payload["need"])},
                    action=AgentAction.UPLOAD_BLOBS,
                )
            self._state.record_recovery(
                "blob_fill",
                "recovered",
                tool=tool,
                from_revision=self._state.view_revision,
                to_revision=self._state.view_revision,
                count=len(need),
            )

        answer = _unwrap(tool, payload, self._state.view_revision)
        hydration = answer.structured.get("_hydration") if answer.structured is not None else None
        if (
            tool == "code_search"
            and not answer.is_error
            and answer.structured is not None
            and isinstance(hydration, Mapping)
        ):
            query = arguments.get("query")
            rendered = (
                render_local_code_search(str(query), answer.structured, repo_root=self._config.repo_root)
                if isinstance(query, str)
                else None
            )
            if rendered is None:
                fallback_arguments = dict(arguments)
                fallback_arguments["_client_hydrate"] = False
                fallback_payload = self._dispatch(tool, fallback_arguments)
                return _unwrap(tool, fallback_payload, self._state.view_revision)
            answer = ToolAnswer(
                tool=answer.tool,
                content=({"type": "text", "text": rendered},),
                is_error=answer.is_error,
                view_revision=answer.view_revision,
                degraded=answer.degraded,
                degraded_reason=answer.degraded_reason,
                structured=answer.structured,
            )
        validator = payload.get("reuse_validator")
        if not answer.is_error and not answer.degraded and isinstance(validator, str) and validator:
            self._cache.put(self._cache_key(tool, wire_arguments), payload, validator)
        return answer

    def _dispatch(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        reuse_validator: str = "",
    ) -> Mapping[str, Any]:
        try:
            return self._post_tool(tool, arguments, reuse_validator=reuse_validator)
        except ClientError as exc:
            if exc.code in _REOPEN_CODES:
                # A server restart can invalidate the session or its bound view.
                return self._reopen_then(tool, exc, lambda: self._post_tool(tool, arguments))
            if exc.code is ErrorCode.SERVER_UNREACHABLE and self._reconnect_eligible(tool, exc):
                return self._reconnect_and_retry(tool, arguments, exc)
            if exc.code is not ErrorCode.VIEW_REVISION_STALE:
                if exc.code is ErrorCode.SERVER_UNREACHABLE:
                    self._state.go_offline(exc.message)
                raise
            committed = exc.details.get("committed_view_revision")
            if not isinstance(committed, int):
                raise
            previous_revision = self._state.view_revision
            self._state.record_recovery(
                "revision_retry",
                "retrying",
                error_code=exc.code.value,
                tool=tool,
                from_revision=previous_revision,
                to_revision=committed,
            )
            self._state.view_revision = committed
            self._cache.prune_scope(self._state.session_id, self._state.view_id, committed)
            try:
                payload = self._post_tool(tool, arguments)
            except ClientError as retry_exc:
                self._state.record_recovery(
                    "revision_retry",
                    "failed",
                    error_code=retry_exc.code.value,
                    tool=tool,
                    from_revision=previous_revision,
                    to_revision=committed,
                )
                raise
            self._state.record_recovery(
                "revision_retry",
                "recovered",
                error_code=exc.code.value,
                tool=tool,
                from_revision=previous_revision,
                to_revision=committed,
            )
            return payload

    def _reopen_then[T](self, tool: str, exc: ClientError, attempt: Callable[[], T]) -> T:
        """Re-open the session and its View once, then run ``attempt`` once more.

        For a refusal that means the server restarted underneath this session.
        A second refusal escapes to the caller.
        """
        previous_revision = self._state.view_revision
        self._state.record_recovery(
            "session_reopen",
            "retrying",
            error_code=exc.code.value,
            tool=tool,
            from_revision=previous_revision,
        )
        report = self.bootstrap(deadline_s=max(self._config.startup_budget_s, self._config.request_timeout_s))
        if not report.ok:
            self._state.record_recovery(
                "session_reopen",
                "failed",
                error_code=ErrorCode.SERVER_SESSION_UNAVAILABLE.value,
                tool=tool,
                from_revision=previous_revision,
                to_revision=self._state.view_revision,
            )
            raise ClientError(
                ErrorCode.SERVER_SESSION_UNAVAILABLE,
                report.reason or "the LemonCrow session could not be reopened after the server restarted",
                details={"tool": tool, "previous_error": exc.code.value},
                retryable=True,
                action=AgentAction.RETRY_LATER,
            ) from exc
        self._state.generation += 1
        try:
            result = attempt()
        except ClientError as retry_exc:
            self._state.record_recovery(
                "session_reopen",
                "failed",
                error_code=retry_exc.code.value,
                tool=tool,
                from_revision=previous_revision,
                to_revision=self._state.view_revision,
            )
            raise
        self._state.record_recovery(
            "session_reopen",
            "recovered",
            error_code=exc.code.value,
            tool=tool,
            from_revision=previous_revision,
            to_revision=self._state.view_revision,
        )
        return result

    def _reconnect_eligible(self, tool: str, exc: ClientError) -> bool:
        """Re-send only calls that are safe to repeat, and never after a timeout.

        A timeout means the server may still be executing the first attempt;
        a refused/reset connection means it is gone and will come back empty.
        """
        if exc.details.get("timed_out") or self._config.reconnect_budget_s <= 0:
            return False
        route = ROUTES.get(tool)
        return route is not None and route.retry_safe

    def _reconnect_and_retry(self, tool: str, arguments: Mapping[str, Any], exc: ClientError) -> Mapping[str, Any]:
        """Wait (bounded) for the server to return, rebind, and re-send once it does."""
        before = self._state.state_token()
        previous_revision = self._state.view_revision
        self._state.record_recovery("reconnect_retry", "retrying", error_code=exc.code.value, tool=tool)
        deadline = time.monotonic() + self._config.reconnect_budget_s
        delay = 0.2
        last: ClientError = exc
        while time.monotonic() < deadline:
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 2.0)
            try:
                payload = self._post_tool(tool, arguments)
            except ClientError as retry_exc:
                last = retry_exc
                if retry_exc.code is ErrorCode.SERVER_UNREACHABLE and not retry_exc.details.get("timed_out"):
                    continue
                if retry_exc.code in {ErrorCode.SESSION_UNKNOWN, ErrorCode.SESSION_EXPIRED, ErrorCode.VIEW_UNKNOWN}:
                    # The server restarted: rebind session + view, then one send.
                    report = self.bootstrap(deadline_s=max(0.5, deadline - time.monotonic()))
                    if not report.ok:
                        continue
                    self._state.generation += 1
                    if self._state.state_token() != before:
                        self._cache.prune_scope(self._state.session_id, self._state.view_id, previous_revision)
                    try:
                        payload = self._post_tool(tool, arguments)
                    except ClientError as final_exc:
                        last = final_exc
                        break
                else:
                    break
            self._state.record_recovery(
                "reconnect_retry",
                "recovered",
                error_code=exc.code.value,
                tool=tool,
                from_revision=previous_revision,
                to_revision=self._state.view_revision,
            )
            return payload
        self._state.record_recovery(
            "reconnect_retry",
            "failed",
            error_code=last.code.value,
            tool=tool,
            from_revision=previous_revision,
        )
        if last.code is ErrorCode.SERVER_UNREACHABLE:
            self._state.go_offline(last.message)
        raise last

    def _post_tool(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        reuse_validator: str = "",
    ) -> Mapping[str, Any]:
        body: dict[str, Any] = {"arguments": dict(arguments), "view_revision": self._state.view_revision}
        if reuse_validator and RESULT_REUSE_CAPABILITY in self._state.capabilities:
            body["reuse_validator"] = reuse_validator
        response = self._transport.post(
            f"/v1/tools/{tool}",
            body=body,
            headers={
                **self._auth(),
                **_host_runtime_headers(self._config),
                HEADER_REQUEST_ID: _request_id(),
            },
            timeout_s=self._config.tool_timeout_s,
        )
        payload = response.require()
        self._state.online = True
        return payload

    def _cache_key(self, tool: str, arguments: Mapping[str, Any]) -> CacheKey | None:
        if RESULT_REUSE_CAPABILITY not in self._state.capabilities or not self._cache.enabled:
            return None
        return make_cache_key(
            session_id=self._state.session_id,
            view_id=self._state.view_id,
            view_revision=self._state.view_revision,
            tool=tool,
            arguments=arguments,
        )

    def query_index(
        self,
        kind: str,
        *,
        query: str = "",
        path: str = "",
        symbol: str = "",
        limit: int = 50,
    ) -> Mapping[str, Any]:
        """Ask the three-layer index directly, bound to the same revision.

        The tool route and this route are the two ways to reach the server, and
        both name the revision they observed. 11D joins them; until then this
        is how the client reaches ``lexical``/``symbol``/``relations``/
        ``semantic`` without a public tool registry on the server.
        """
        if not self._state.view_bound:
            raise ClientError(
                ErrorCode.VIEW_UNKNOWN, "no view is bound to this session", action=AgentAction.REOPEN_VIEW
            )
        return self._transport.post(
            f"/v1/views/{self._state.view_id}/query",
            body={
                "kind": kind,
                "query": query,
                "path": path,
                "symbol": symbol,
                "limit": limit,
                "view_revision": self._state.view_revision,
            },
            headers=self._auth(),
        ).require()

    @_serialized
    def push_paths(self, paths: tuple[str, ...]) -> int:
        """Bring the server's view forward after a local write."""
        if not paths or not self._state.view_bound:
            return self._state.view_revision
        try:
            revision = self._blobs.push_paths(paths).view_revision
        except ClientError as exc:
            if exc.code not in _REOPEN_CODES:
                raise
            # The server restarted under this session. The edit is already on
            # disk: re-open and push it again, or every later edit is refused.
            revision = self._reopen_then("edit", exc, lambda: self._blobs.push_paths(paths).view_revision)
        self._cache.prune_scope(self._state.session_id, self._state.view_id, revision)
        self._git_overlay_paths = self._git_overlay_paths | frozenset(paths)
        # A path-specific push proves no complete workspace fingerprint. Invalidate
        # the old proof too: restoring files can otherwise match it while the
        # server still holds this edit. The next sync reconciles all dirty paths.
        self._workspace_fingerprint = ""
        if self._publish_live_views and self._owns_view:
            try:
                clear_live_view_binding(
                    self._config.runtime_state_dir, self._config.repo_root, expected_view_id=self._state.view_id
                )
            except OSError:
                pass
        return revision

    def sync_if_changed(self, *, retry_failed: bool = True) -> bool | None:
        """Bring the View up to date now, if and only if the working tree changed.

        For opportunistic callers, such as the tail of a command that may have
        rewritten files. ``False``: nothing changed, nothing was sent. ``True``:
        the changed files were pushed. ``None``: a call is in flight and was not
        waited on -- it runs this same check itself. ``retry_failed=False``
        skips a tree whose push already failed rather than wait on the server
        again; it is retried once the tree changes, or by the next View call.
        """
        if not self._sync_lock.acquire(blocking=False):
            return None
        observed = ""
        try:
            if not self._state.view_bound or not self._indexed:
                return False
            observed = workspace_state_fingerprint(self._config.repo_root)
            if not retry_failed and observed and observed == self._unsynced_fingerprint:
                return False
            synced = self._refresh_view_if_source_changed(observed)
            self._unsynced_fingerprint = ""
            return synced
        except ClientError:
            self._unsynced_fingerprint = observed
            return False  # best effort; the next call's own check still guarantees freshness
        finally:
            self._sync_lock.release()

    @_serialized
    def pin_review_snapshot(
        self,
        new_blobs: Mapping[str, str],
        *,
        binary_new_blobs: Mapping[str, bytes] | None = None,
        deleted_paths: tuple[str, ...] = (),
        timeout_s: float | None = None,
    ) -> int:
        """Pin the packet's already-read new side to this View before Review capture.

        Review reads the working tree before it opens/synchronizes a server View.
        An editor or coding agent may save again in that window.  The View is the
        immutable source of truth for the published Snapshot, so advance it to the
        bytes Review already captured instead of rejecting useful work merely
        because the live checkout moved on.
        """
        if not self._state.view_bound:
            return self._state.view_revision
        contents = {path: text.encode("utf-8") for path, text in new_blobs.items()}
        if binary_new_blobs:
            contents.update(binary_new_blobs)
        report = self._blobs.push_snapshot(
            contents,
            removed_paths=deleted_paths,
            timeout_s=timeout_s,
        )
        self._cache.prune_scope(self._state.session_id, self._state.view_id, report.view_revision)
        return report.view_revision

    def resync(self) -> Mapping[str, Any]:
        """Re-negotiate the Git base and local overlay. What ``index`` does."""
        if not self._indexed:
            raise not_indexed_error(self._config.repo_root, "index")
        return self.open_view()

    def review_inbox(self, *, timeout_s: float | None = None) -> Mapping[str, Any]:
        """List hosted Reviews visible to this credential."""
        return self._transport.get(
            "/v1/reviews",
            headers=self._bearer(),
            timeout_s=timeout_s,
        ).require()

    def review_detail(
        self,
        repo_id: str,
        review_id: str,
        *,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """Load one hosted Review after its repository scope is known."""
        return self._transport.get(
            f"/v1/repos/{quote(repo_id, safe='')}/reviews/{quote(review_id, safe='')}",
            headers=self._bearer(),
            timeout_s=timeout_s,
        ).require()

    def review_attachment_projection(
        self,
        repo_id: str,
        review_id: str,
        *,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """What this principal sees of a hosted Review a local Review is attached to."""
        return self._transport.get(
            f"/v1/repos/{quote(repo_id, safe='')}/reviews/{quote(review_id, safe='')}/attachment",
            headers=self._bearer(),
            timeout_s=timeout_s,
        ).require()

    def review_attachment_replay(
        self,
        repo_id: str,
        review_id: str,
        body: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """Replay one queued local action on the hosted Review; idempotent on ``action_id``."""
        return self._transport.post(
            f"/v1/repos/{quote(repo_id, safe='')}/reviews/{quote(review_id, safe='')}/attachment/actions",
            body=dict(body),
            headers=self._bearer(),
            timeout_s=timeout_s,
        ).require()

    def review_url(self, review_id: str) -> str:
        """Canonical browser URL for a Review on the configured origin."""
        internal_id = review_id[2:] if review_id.startswith("r/") else review_id
        return self._config.endpoint(f"/r/{quote(internal_id, safe='')}")

    def pair_local_review_browser(
        self,
        path: str,
        *,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """Arm one local Review browser path using the machine bearer.

        The browser never receives this bearer. It opens the clean URL and
        claims a separate Review-only capability from the loopback server.
        """
        if self._config.hosted:
            raise ClientError(
                ErrorCode.NOT_CONFIGURED,
                "local Review browser pairing is only available in local mode",
                action=AgentAction.ABANDON,
            )
        return self._transport.post(
            "/v1/auth/local-browser/pair",
            body={"path": path},
            headers=self._bearer(),
            timeout_s=timeout_s,
        ).require()

    def review_reader_request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """Call the authenticated Reader compatibility API on the same server.

        Capture, browser Review, and terminal Review actions all share
        ``LEMONCROW_URL``. This method intentionally accepts only ``/api/``
        paths so a terminal feature cannot quietly grow another destination.
        """
        if not path.startswith("/api/"):
            raise ClientError(
                ErrorCode.NOT_CONFIGURED,
                "Review Reader requests must stay under /api/ on LEMONCROW_URL",
                action=AgentAction.ABANDON,
            )
        # Only routes some Review servers lack consult the manifest, so the
        # common reads cost no extra round trip.
        requirement = review_route_requirement(method, path)
        if requirement is not None:
            # lc-debt: one extra GET /v1/reviews/manifest per gated Ask, evidence, record or
            # readiness call, because CLI callers open a fresh RemoteSession per call. Upgrade
            # path: cache the manifest per server URL and credential for the process (refresh
            # on a 426 or a typed capability refusal), or reuse one session across CLI calls.
            self.review_manifest(timeout_s=timeout_s).require_route(requirement)
        response = self._transport.request(
            method.upper(),
            path,
            body=body,
            headers=self._bearer(),
            timeout_s=timeout_s,
        )
        if response.status >= 400 and isinstance(response.payload.get("detail"), str):
            status_map = {
                400: (ErrorCode.PAYLOAD_INVALID, AgentAction.FIX_REQUEST),
                401: (ErrorCode.UNAUTHENTICATED, AgentAction.REAUTHENTICATE),
                403: (ErrorCode.FORBIDDEN, AgentAction.REQUEST_ACCESS),
                404: (ErrorCode.REVIEW_UNKNOWN, AgentAction.ABANDON),
                409: (ErrorCode.REVIEW_STATE_CONFLICT, AgentAction.FIX_REQUEST),
                422: (ErrorCode.PAYLOAD_INVALID, AgentAction.FIX_REQUEST),
            }
            code, action = status_map.get(response.status, (ErrorCode.UNKNOWN, AgentAction.NONE))
            raise ClientError(code, str(response.payload["detail"]), action=action)
        return response.require()

    def close(self) -> None:
        """Best effort remote close, with deterministic local helper teardown."""
        try:
            if self._publish_live_views and self._published_live_view_id:
                clear_live_view_binding(
                    self._config.runtime_state_dir,
                    self._config.repo_root,
                    expected_view_id=self._published_live_view_id,
                )
                self._published_live_view_id = ""
            if self._state.session_id:
                try:
                    if self._state.view_id and self._owns_view:
                        self._transport.post(
                            f"/v1/views/{self._state.view_id}/close",
                            body={},
                            headers=self._auth(with_revision=False),
                            timeout_s=2.0,
                        )
                    self._transport.delete(
                        f"/v1/sessions/{self._state.session_id}", headers=self._bearer(), timeout_s=2.0
                    )
                except ClientError:
                    # "Close is best effort. A lease/TTL makes abandoned views
                    # evictable without trusting SessionEnd." Failing here would
                    # turn a clean exit into a visible error for no benefit.
                    pass
        finally:
            if self._fingerprint_executor is not None:
                self._fingerprint_executor.shutdown(wait=True, cancel_futures=True)
                self._fingerprint_executor = None
            self._cache.clear()
            self._transport.close()

    # -- helpers -------------------------------------------------------- #

    def _negotiation_body(self, protocol_version: str) -> dict[str, Any]:
        offered = set(CLIENT_CAPABILITIES)
        # Offering the capability is a claim that this client can write into
        # the worktree, so it is made only after proving it can -- with a
        # throwaway value, because the nonce that will actually gate the grant
        # is minted by the server one round trip later.
        if self._config.offer_local_fs and self._write_proof(secrets.token_hex(_PROOF_BYTES)):
            offered.add(SAME_HOST_CAPABILITY)
        return {
            "protocol_version": protocol_version,
            "capabilities": sorted(offered),
            "client_name": "lemoncrow-client",
            "client_version": _client_version(),
        }

    def _local_fs_offer(self) -> dict[str, Any] | None:
        """The same-host proof, or nothing.

        Three conditions, all required: the operator opted in, the session
        negotiated the capability, and this client can write **the nonce the
        server issued for this session** into the worktree the server is being
        asked to read. The third is what stops "read this path" from being a
        file-disclosure primitive -- and it is the server's nonce rather than
        one of this client's choosing precisely because a self-chosen token
        demonstrates only that the caller could predict a file's contents.
        """
        if not self._config.offer_local_fs or SAME_HOST_CAPABILITY not in self._state.capabilities:
            return None
        if not self._state.local_fs_proof or not self._write_proof(self._state.local_fs_proof):
            return None
        return {
            "root": str(self._config.repo_root.resolve()),
            "proof_path": LOCAL_FS_PROOF_PATH,
        }

    def _write_proof(self, nonce: str) -> bool:
        """Write the same-host nonce without turning transport state into source.

        The proof must remain directly under the checkout root: that is what
        lets the server verify that the writer and the repository root have the
        same owner. Before writing it, hide the fixed transport filename from
        Git's worktree status. The manifest walker excludes it independently,
        so neither source synchronization nor Review can ever treat this nonce
        as repository content.
        """
        from .workspace_paths import write_local_fs_proof

        return write_local_fs_proof(self._config.repo_root, nonce)

    def _exclude_local_fs_proof_from_git(self) -> None:
        from .workspace_paths import exclude_local_fs_proof

        exclude_local_fs_proof(self._config.repo_root)

    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.token}"} if self._config.token else {}

    def _auth(self, *, with_revision: bool = True) -> dict[str, str]:
        return {**self._bearer(), **self._state.headers(with_revision=with_revision)}


# Backward-compatible private alias for older callers/tests; new code uses the public name.
_workspace_state_fingerprint = workspace_state_fingerprint


def _git_metadata_dir(repo_root: Path) -> Path | None:
    from .workspace_paths import git_metadata_dir

    return git_metadata_dir(repo_root)


def _client_version() -> str:
    from . import CLIENT_VERSION

    return CLIENT_VERSION


def _request_id() -> str:
    """The active call's trace id, so server/runtime records share it."""
    return current_trace_id() or new_trace_id()


def _json_size(value: object) -> int:
    """Bytes the transport will send for ``value`` (same encoding as its body)."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _pack_review_chunks(
    files: Sequence[dict[str, Any]],
    blobs: Mapping[str, str],
    *,
    first_room: int,
    later_room: int,
) -> list[tuple[list[dict[str, Any]], dict[str, str]]]:
    """Split a Review's files (and their text blobs) into request-sized chunks.

    Order-preserving and greedy: a chunk closes only when the next entry would
    not fit. One entry is never split -- the per-file limit still applies -- so
    an entry larger than a whole request is refused with its size. Blobs travel
    with their file; blobs naming no file ride in whichever chunk has room.
    """
    file_paths = {item.get("path") for item in files}
    units: list[tuple[dict[str, Any] | None, str | None, str | None, int]] = []
    for item in files:
        path = item.get("path")
        blob = blobs.get(path) if isinstance(path, str) else None
        cost = _json_size(item) + 1 + (_json_size({path: blob}) + 1 if blob is not None else 0)
        units.append((item, path if blob is not None else None, blob, cost))
    for path, blob in blobs.items():
        if path not in file_paths:
            units.append((None, path, blob, _json_size({path: blob}) + 1))
    chunks: list[tuple[list[dict[str, Any]], dict[str, str]]] = []
    files_now: list[dict[str, Any]] = []
    blobs_now: dict[str, str] = {}
    used, room = 0, first_room
    for entry, blob_path, blob, cost in units:
        if used + cost > room:
            if cost > later_room:
                raise ClientError(
                    ErrorCode.REQUEST_TOO_LARGE,
                    f"one Review file entry is {cost:,} bytes; the request limit leaves {later_room:,}",
                    details={"path": str((entry or {}).get("path") or blob_path or "")},
                    action=AgentAction.SPLIT_REQUEST,
                )
            chunks.append((files_now, blobs_now))
            files_now, blobs_now, used, room = [], {}, 0, later_room
        if entry is not None:
            files_now.append(entry)
        if blob_path is not None and blob is not None:
            blobs_now[blob_path] = blob
        used += cost
    chunks.append((files_now, blobs_now))
    return chunks


def _string_list(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(entry) for entry in raw if isinstance(entry, str))


def persist_walk_cache(
    cache: WalkCache,
    state_dir: Path,
    *,
    workspace_fingerprint: str = "",
    manifest_entry_count: int | None = None,
    generation_entry_count: int | None = None,
    generation_revision: str = "",
    manifest_root_value: str = "",
    manifest_chunk_rows: int = 0,
    manifest_chunk_ids: Sequence[str] = (),
) -> Path | None:
    """Leave the next session the digests this one computed. Returns the file.

    Best effort in every direction: a cache that cannot be written costs one
    slow session and nothing else, so every failure is swallowed rather than
    allowed to turn a working session into a failed one. It is called only after
    a view has actually opened, which is what keeps an offline session's "writes
    nothing at all" literally true.

    The write is atomic -- a temporary file named for this process, then
    ``os.replace`` -- because a half-written cache read by the next session would
    be a half-truth about which files are unchanged. The reader refuses a
    truncated file as well, so neither half of the pair trusts the other.
    """
    complete_proof = (
        workspace_fingerprint
        if manifest_entry_count is not None and manifest_entry_count == len(cache.fresh_path)
        else ""
    )
    plan = cache.plan_write(
        state_dir,
        workspace_fingerprint=complete_proof,
        generation_revision=generation_revision if complete_proof else "",
        manifest_root=manifest_root_value if complete_proof else "",
        manifest_chunk_rows=manifest_chunk_rows if complete_proof else 0,
        manifest_chunk_ids=manifest_chunk_ids if complete_proof else (),
        generation_entry_count=generation_entry_count if complete_proof else None,
    )
    if plan is None:
        return None
    temporary = plan.path.with_name(f"{plan.path.name}.{os.getpid()}.tmp")
    try:
        plan.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(plan.data)
        os.replace(temporary, plan.path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return None
    for stale in plan.prune:
        try:
            os.unlink(stale)
        except OSError:
            pass
    return plan.path


def _unwrap(tool: str, payload: Mapping[str, Any], fallback_revision: int) -> ToolAnswer:
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        record_runtime_policy_attribution(diagnostics)
    raw_content = payload.get("content")
    content = (
        tuple(block for block in raw_content if isinstance(block, Mapping)) if isinstance(raw_content, list) else ()
    )
    revision = payload.get("view_revision")
    structured = payload.get("structured")
    return ToolAnswer(
        tool=str(payload.get("tool") or tool),
        content=content,
        is_error=bool(payload.get("is_error")),
        view_revision=revision if isinstance(revision, int) else fallback_revision,
        degraded=bool(payload.get("degraded")),
        degraded_reason=str(payload.get("degraded_reason") or ""),
        structured=structured if isinstance(structured, Mapping) else None,
    )
