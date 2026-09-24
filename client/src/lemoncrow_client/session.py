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
  unbounded fill/retry loop is worse than a typed refusal.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from .blobs import BlobService
from .config import LOCAL_FS_PROOF_PATH, ClientConfig, base_revision, repo_identity
from .credentials import RefreshingHttpTransport, managed_credentials_path
from .errors import AgentAction, ClientError, ErrorCode
from .manifest import (
    ManifestChunk,
    ManifestEntry,
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
    PROTOCOL_VERSION,
    RESULT_REUSE_CAPABILITY,
    SAME_HOST_CAPABILITY,
)
from .resolution_cache import CacheKey, CacheStats, ResolutionCache, make_cache_key
from .runtime_attribution import record_runtime_policy_attribution
from .search_markdown import render_local_code_search
from .state import SessionState
from .transport import HttpTransport
from .walkcache import WalkCache, load_walk_cache

__all__ = ["BootstrapReport", "RemoteSession", "ToolAnswer", "persist_walk_cache"]

_MAX_CHUNK_ROWS: Final[int] = 2_000
#: Slack under the server's request cap for the JSON envelope and header bytes.
_REQUEST_HEADROOM: Final[int] = 1024
#: Bytes an append request adds around its files: seq, final, new_blobs keys.
_APPEND_ENVELOPE: Final[int] = 256
_PROOF_BYTES: Final[int] = 24
_INDEX_VIEW_TOOLS: Final[frozenset[str]] = frozenset({"code_search", "read", "relations", "graph", "search"})
_INTERNAL_WORKTREE_ROOTS: Final[frozenset[str]] = frozenset({".git", ".lc-worktrees", ".lemoncrow"})
_HOST_SESSION_ENVS: Final[tuple[tuple[str, str], ...]] = (
    ("CLAUDE_CODE_SESSION_ID", "claude"),
    ("CODEX_SESSION_ID", "codex"),
    ("OPENCODE_SESSION_ID", "opencode"),
    ("LEMONCODE_SESSION_ID", "lemoncode"),
    ("GITHUB_COPILOT_SESSION_ID", "copilot"),
    ("CURSOR_SESSION_ID", "cursor"),
    ("CURSOR_TRACE_ID", "cursor"),
    ("HERMES_SESSION_ID", "hermes"),
    ("ANTIGRAVITY_SESSION_ID", "antigravity"),
    ("AGY_SESSION_ID", "antigravity"),
)


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

    env_sid = ""
    env_host = ""
    for env_name, host in _HOST_SESSION_ENVS:
        candidate = os.environ.get(env_name, "").strip()
        if candidate:
            env_sid, env_host = candidate, host
            break

    if env_sid:
        host = explicit_host or env_host
        model = bridge_model if bridge_sid == env_sid and (not bridge_host or bridge_host == host) else ""
        return {
            HEADER_HOST_SESSION: env_sid,
            HEADER_HOST: host,
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


def _workspace_state_fingerprint(repo_root: Path) -> str:
    """Cheap identity of the source state a long-lived server View should mirror.

    One porcelain-v2 status call carries both the exact HEAD oid and the dirty
    worktree records. Hash stat identity for every reported path as well, so a
    second edit to an already-dirty same-size file still changes the fingerprint.
    LemonCrow's own nested worktrees/state are excluded at the Git pathspec, so
    their churn cannot invalidate the source View.

    Non-Git directories are uncommon for coding-agent sessions; there we fall
    back to the canonical manifest root for correctness rather than inventing a
    weaker freshness signal.
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
        return hasher.hexdigest()

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

    def one_line(self, url: str) -> str:
        """A single visible line. Never more, whatever happened."""
        if not self.ok:
            return f"LemonCrow: server-side tools unavailable ({self.reason}); client-side tools still work"
        mode = "warm" if self.warm else "cold"
        return (
            f"LemonCrow: {url} view {self.view_id} rev {self.view_revision} "
            f"({mode}, {self.files} files, {self.hashed_files} hashed, "
            f"{self.uploaded_blobs} uploaded, {self.elapsed_s:.2f}s)"
        )


class RemoteSession:
    """One authenticated session against one LemonCrow server."""

    __slots__ = (
        "_blobs",
        "_cache",
        "_chunks",
        "_config",
        "_fingerprint_executor",
        "_state",
        "_transport",
        "_walk",
        "_workspace_fingerprint",
    )

    def __init__(self, config: ClientConfig, transport: HttpTransport | None = None) -> None:
        self._config = config
        if transport is not None:
            self._transport = transport
        elif (
            config.hosted
            and config.token
            and config.refresh_token
            and config.token_source == str(managed_credentials_path(config.state_dir))
        ):
            self._transport = RefreshingHttpTransport(
                config.url,
                timeout_s=config.request_timeout_s,
                state_dir=config.state_dir,
                access_token=config.token,
                refresh_token=config.refresh_token,
            )
        else:
            self._transport = HttpTransport(config.url, timeout_s=config.request_timeout_s)
        self._state = SessionState()
        self._blobs = BlobService(self._transport, self._state, config.repo_root, token=config.token)
        self._cache = ResolutionCache(config.resolution_cache_bytes)
        # Lazily created on the first indexed tool call and always joined by
        # close(). Keeping it session-owned preserves the one-session/no-helper
        # audit contract while still overlapping Git freshness with server I/O.
        self._fingerprint_executor: ThreadPoolExecutor | None = None
        self._walk: WalkResult | None = None
        self._chunks: tuple[ManifestChunk, ...] = ()
        self._workspace_fingerprint = ""

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
    def view_revision(self) -> int:
        return self._state.view_revision

    @property
    def cache_stats(self) -> CacheStats:
        return self._cache.stats

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
            opened = self.open_view(timeout_s=budget)
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
            files=len(self._walk.files) if self._walk is not None else 0,
            uploaded_blobs=int(opened.get("uploaded_blobs", 0)),
            uploaded_bytes=int(opened.get("uploaded_bytes", 0)),
            manifest_chunks_sent=int(opened.get("manifest_chunks_sent", 0)),
            elapsed_s=time.monotonic() - started,
            local_fs=self._state.local_fs,
            hashed_files=int(opened.get("hashed_files", 0)),
        )

    def handshake(self, *, timeout_s: float | None = None) -> Mapping[str, Any]:
        """Negotiate before authenticating. Refuses a mismatch loudly."""
        payload = self._transport.post("/v1/handshake", body=self._negotiation_body(), timeout_s=timeout_s).require()
        negotiated = payload.get("protocol_version")
        if negotiated != PROTOCOL_VERSION:
            # The server answered with a version this build did not offer. It
            # is a bug rather than a downgrade path, and pretending otherwise
            # is how a client ends up parsing a wire format it does not know.
            raise ClientError(
                ErrorCode.PROTOCOL_VERSION_MISMATCH,
                f"server negotiated protocol {negotiated!r}; this client speaks {PROTOCOL_VERSION!r}",
                details={"server": str(negotiated), "client": PROTOCOL_VERSION},
                action=AgentAction.UPGRADE_CLIENT,
            )
        return payload

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
        payload = self._transport.post(
            "/v1/sessions",
            body=self._negotiation_body(),
            headers=self._bearer(),
            timeout_s=timeout_s,
        ).require()
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

    def open_view(
        self,
        *,
        timeout_s: float | None = None,
        upload_missing_content: bool = True,
    ) -> Mapping[str, Any]:
        """Walk and negotiate a View, optionally filling missing source content.

        Tool sessions use the default because server-side intelligence needs the
        source blobs. Hosted Review capture disables that fill: the Review API
        validates against the committed manifest and receives changed-file text
        in its own request, so unrelated file contents need not be uploaded.
        """
        cache = load_walk_cache(self._config.state_dir, self._config.repo_root)
        walk = walk_worktree(self._config.repo_root, size_cap=self._state.content_size_cap, cache=cache)
        self._walk = walk
        # Built once and carried through. At 50,000 files each rebuild of this
        # tuple is tens of milliseconds, and the root, the chunking and the
        # known-row map all want the same rows.
        entries: tuple[ManifestEntry, ...] = walk.entries()
        self._chunks = chunk_manifest(
            entries, max_rows=min(_MAX_CHUNK_ROWS, self._state.max_manifest_entries_per_chunk)
        )
        body: dict[str, Any] = {
            "repo_identity": repo_identity(self._config.repo_root).to_wire(),
            "base_revision": base_revision(self._config.repo_root),
            "manifest_root": manifest_root(entries),
            "manifest_chunks": [chunk.chunk_id for chunk in self._chunks],
        }
        grant = self._local_fs_offer()
        if grant is not None:
            body["local_fs"] = grant

        opened = dict(
            self._transport.post("/v1/views/open", body=body, headers=self._auth(), timeout_s=timeout_s).require()
        )
        view_id = opened.get("view_id")
        if not isinstance(view_id, str) or not view_id:
            raise ClientError(ErrorCode.VIEW_UNKNOWN, "server opened a view with no id", action=AgentAction.REOPEN_VIEW)
        self._state.view_id = view_id
        repo_id = opened.get("repo_id")
        if not isinstance(repo_id, str) or not repo_id:
            raise ClientError(
                ErrorCode.INTERNAL,
                "server opened a view with no repository id",
                action=AgentAction.ABANDON,
            )
        self._state.repo_id = repo_id
        self._state.view_revision = int(opened.get("view_revision", 0) or 0)
        self._cache.prune_scope(self._state.session_id, view_id, self._state.view_revision)
        cap = opened.get("content_size_cap")
        if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 and cap != self._state.content_size_cap:
            # The server's cap is policy, and it differs from the default this
            # walk assumed. Re-walk so ``uploadable`` reflects the real cap
            # rather than a guess -- a row is still produced either way, so the
            # root is unchanged and the open stays valid.
            self._state.content_size_cap = cap
            self._walk = walk_worktree(self._config.repo_root, size_cap=cap, cache=cache)
            entries = self._walk.entries()

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
            opened["missing_digests"] = state.get("outstanding_digests", [])

        missing = _string_list(opened.get("missing_digests"))
        missing_set = set(missing)
        self._state.remember_server_digests(
            entry.content_digest
            for entry in entries
            if entry.size <= self._state.content_size_cap and entry.content_digest not in missing_set
        )
        stored = 0
        plaintext = 0
        if upload_missing_content:
            stored, plaintext = self._upload_missing(missing)
        # A newly negotiated View is authoritative. Do not retain paths from a
        # previous View that disappeared because of a delete, branch switch, or
        # newly-active ignore rule.
        self._state.known.clear()
        for entry in entries:
            self._state.known[entry.path] = entry
        self._workspace_fingerprint = _workspace_state_fingerprint(self._config.repo_root)
        opened["manifest_chunks_sent"] = sent
        opened["uploaded_blobs"] = stored
        opened["uploaded_bytes"] = plaintext
        opened["content_sync_skipped"] = bool(missing) and not upload_missing_content
        opened["hashed_files"] = self._walk.digests.hashed if self._walk is not None else 0
        persist_walk_cache(cache, self._config.state_dir)
        return opened

    def _upload_missing(self, digests: Sequence[str]) -> tuple[int, int]:
        if not digests or self._walk is None:
            return 0, 0
        sources = {file.content_digest: self._config.repo_root / file.path for file in self._walk.uploadable()}
        return self._blobs.upload_digests(tuple(digests), sources)

    def open_review_snapshot(
        self,
        snapshot_root: Path,
        *,
        source_revision: str,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """Open a complete immutable View for the exact Review new side.

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
        walk = walk_worktree(root, size_cap=self._state.max_blob_bytes)
        if walk.truncated or walk.unreadable or walk.oversize or walk.symlinks:
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
        missing.update(entry.content_digest for entry in entries if entry.size > self._state.content_size_cap)
        sources = {file.content_digest: root / file.path for file in walk.uploadable()}
        stored, plaintext = self._blobs.upload_digests(tuple(sorted(missing)), sources, timeout_s=timeout_s)
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
        timeout_s: float | None = None,
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
        payload = self._post_review(
            f"/v1/repos/{quote(target_repo, safe='')}/reviews",
            {
                "view_id": self._state.view_id,
                "view_revision": self._state.view_revision,
                "source_ref": source_ref,
                "title": title,
                "restore_archived": restore_archived,
                "packet": dict(packet),
                "new_blobs": dict(new_blobs),
                "base_view_id": base_view_id,
                "base_view_revision": base_view_revision,
            },
            timeout_s if timeout_s is not None else self._config.tool_timeout_s,
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
        payload = self._transport.post(
            f"/v1/repos/{quote(target_repo, safe='')}/reviews",
            body={
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
            },
            headers=self._auth(),
            timeout_s=timeout_s if timeout_s is not None else self._config.tool_timeout_s,
        ).require()
        self._state.online = True
        return payload

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

    def _post_review(self, path: str, body: dict[str, Any], timeout_s: float) -> Mapping[str, Any]:
        """One request when the capture fits the server's limit, else a chunked upload.

        Chunked: the first request opens a draft with the packet head and the
        first files, each later one appends the next files, and the last append
        commits -- so N requests for N chunks, never an extra open or commit.
        """
        budget = self._state.max_request_bytes - _REQUEST_HEADROOM
        if _json_size(body) <= budget:
            return self._transport.post(path, body=body, headers=self._auth(), timeout_s=timeout_s).require()
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
            return self._transport.post(path, body=body, headers=self._auth(), timeout_s=timeout_s).require()
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
            answer = self._transport.post(
                draft_path,
                body={"seq": seq, "files": files, "new_blobs": blobs, "final": seq == len(chunks) - 1},
                headers=self._auth(),
                timeout_s=timeout_s,
            ).require()
        return answer

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
        observed = observed if observed is not None else _workspace_state_fingerprint(self._config.repo_root)
        if not observed:
            return False
        if not self._workspace_fingerprint:
            self._workspace_fingerprint = observed
            return False
        if observed == self._workspace_fingerprint:
            return False

        cache = load_walk_cache(self._config.state_dir, self._config.repo_root)
        walk = walk_worktree(self._config.repo_root, size_cap=self._state.content_size_cap, cache=cache)
        current = {entry.path: entry for entry in walk.entries()}
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
        persist_walk_cache(cache, self._config.state_dir)
        return advanced

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
        fingerprint: Future[str] | None = None
        if tool in _INDEX_VIEW_TOOLS and self._state.view_bound:
            if self._fingerprint_executor is None:
                self._fingerprint_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lc-fingerprint")
            fingerprint = self._fingerprint_executor.submit(_workspace_state_fingerprint, self._config.repo_root)

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
            if exc.code is ErrorCode.SESSION_UNKNOWN:
                # A central-server restart invalidates the old in-memory session
                # id while the durable workspace/index survives. Re-open the
                # session/view once and retry transparently instead of forcing
                # every MCP host to reconnect manually. Bootstrap is bounded by
                # the configured startup budget and performs a warm view open on
                # the normal path. Never loop: the retried request escapes any
                # second SESSION_UNKNOWN to the caller.
                previous_revision = self._state.view_revision
                self._state.record_recovery(
                    "session_reopen",
                    "retrying",
                    error_code=exc.code.value,
                    tool=tool,
                    from_revision=previous_revision,
                )
                report = self.bootstrap(deadline_s=self._config.startup_budget_s)
                if report.ok:
                    try:
                        payload = self._post_tool(tool, arguments)
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
                    return payload
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

    def push_paths(self, paths: tuple[str, ...]) -> int:
        """Bring the server's view forward after a local write."""
        if not paths or not self._state.view_bound:
            return self._state.view_revision
        revision = self._blobs.push_paths(paths).view_revision
        self._cache.prune_scope(self._state.session_id, self._state.view_id, revision)
        return revision

    def pin_review_snapshot(
        self,
        new_blobs: Mapping[str, str],
        *,
        deleted_paths: tuple[str, ...] = (),
    ) -> int:
        """Pin the packet's already-read text to this View before Review capture."""
        if not self._state.view_bound:
            return self._state.view_revision
        report = self._blobs.push_snapshot(
            {path: text.encode("utf-8") for path, text in new_blobs.items()},
            removed_paths=deleted_paths,
        )
        self._cache.prune_scope(self._state.session_id, self._state.view_id, report.view_revision)
        return report.view_revision

    def resync(self) -> Mapping[str, Any]:
        """Re-walk and re-negotiate the view. What ``index`` does."""
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
            if self._state.session_id:
                try:
                    if self._state.view_id:
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

    # -- helpers -------------------------------------------------------- #

    def _negotiation_body(self) -> dict[str, Any]:
        offered = set(CLIENT_CAPABILITIES)
        # Offering the capability is a claim that this client can write into
        # the worktree, so it is made only after proving it can -- with a
        # throwaway value, because the nonce that will actually gate the grant
        # is minted by the server one round trip later.
        if self._config.offer_local_fs and self._write_proof(secrets.token_hex(_PROOF_BYTES)):
            offered.add(SAME_HOST_CAPABILITY)
        return {
            "protocol_version": PROTOCOL_VERSION,
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
        target = self._config.repo_root / LOCAL_FS_PROOF_PATH
        try:
            self._exclude_local_fs_proof_from_git()
            descriptor = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(nonce)
        except OSError:
            return False
        return True

    def _exclude_local_fs_proof_from_git(self) -> None:
        git_dir = _git_metadata_dir(self._config.repo_root)
        if git_dir is None:
            return
        target = git_dir / "info" / "exclude"
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        rule = f"/{LOCAL_FS_PROOF_PATH}"
        if any(line.strip() == rule for line in existing.splitlines()):
            return
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}# LemonCrow same-host capability proof\n{rule}\n")

    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.token}"} if self._config.token else {}

    def _auth(self, *, with_revision: bool = True) -> dict[str, str]:
        return {**self._bearer(), **self._state.headers(with_revision=with_revision)}


def _git_metadata_dir(repo_root: Path) -> Path | None:
    """Return this checkout's Git metadata directory without invoking Git."""

    marker = repo_root / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    try:
        first = marker.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    prefix = "gitdir:"
    if not first.lower().startswith(prefix):
        return None
    raw = first[len(prefix) :].strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = marker.parent / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _client_version() -> str:
    from . import CLIENT_VERSION

    return CLIENT_VERSION


def _request_id() -> str:
    return f"req-{secrets.token_hex(8)}"


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


def persist_walk_cache(cache: WalkCache, state_dir: Path) -> Path | None:
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
    plan = cache.plan_write(state_dir)
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
