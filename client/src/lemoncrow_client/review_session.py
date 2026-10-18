"""Review-owned remote protocol for immutable snapshot capture.

This module intentionally contains only the network/session work Review needs:
handshake, authenticated session registration, immutable snapshot upload, Review
capture, browser URL construction, and best-effort close. It does not import the
LemonCrow Runtime session, MCP server, routing, local tools, autosync, traces, or
live-workspace state.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlencode

from . import CLIENT_NAME, CLIENT_VERSION
from .blobs import BlobService
from .capture_polling import await_capture
from .config import ClientConfig, repo_identity
from .credentials import RefreshingHttpTransport, managed_credentials_path
from .errors import AgentAction, ClientError, ErrorCode
from .manifest import ManifestChunk, ManifestEntry, chunk_manifest, manifest_root, sha256_hex, walk_worktree
from .protocol import REQUIRED_CLIENT_CAPABILITIES, SAME_HOST_CAPABILITY, negotiate_protocol, parse_review_manifest
from .review_base import BRANCH_REVIEW_PATH
from .state import SessionState
from .transport import HttpTransport, Response
from .workspace_paths import LOCAL_FS_PROOF_PATH, write_local_fs_proof

__all__ = ["ReviewSession"]

_MAX_CHUNK_ROWS: Final[int] = 2_000
_REVIEW_SNAPSHOT_LEASE_S: Final[float] = 86_400.0
_REVIEW_CAPABILITIES: Final[frozenset[str]] = REQUIRED_CLIENT_CAPABILITIES | frozenset({"views.v1", "blobs.v1"})


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item)


class ReviewSession:
    """One short-lived Review capture session against a LemonCrow server."""

    __slots__ = ("_blobs", "_chunks", "_config", "_state", "_transport")

    def __init__(self, config: ClientConfig, transport: HttpTransport | None = None) -> None:
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
        self._chunks: tuple[ManifestChunk, ...] = ()

    @property
    def state(self) -> SessionState:
        return self._state

    def _negotiation_body(self, protocol_version: str) -> dict[str, Any]:
        offered = set(_REVIEW_CAPABILITIES)
        if self._config.offer_local_fs and write_local_fs_proof(self._config.repo_root, secrets.token_hex(32)):
            offered.add(SAME_HOST_CAPABILITY)
        return {
            "protocol_version": protocol_version,
            "capabilities": sorted(offered),
            "client_name": f"{CLIENT_NAME}-review",
            "client_version": CLIENT_VERSION,
        }

    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.token}"} if self._config.token else {}

    def _auth(self, *, with_revision: bool = True) -> dict[str, str]:
        return {**self._bearer(), **self._state.headers(with_revision=with_revision)}

    def handshake(self, *, timeout_s: float | None = None) -> Mapping[str, Any]:
        def post(version: str) -> Mapping[str, Any]:
            return self._transport.post(
                "/v1/handshake",
                body=self._negotiation_body(version),
                timeout_s=timeout_s,
            ).require()

        payload, version = negotiate_protocol(post)
        self._state.protocol_version = version
        server = payload.get("server")
        self._state.review_manifest = parse_review_manifest(version, server if isinstance(server, Mapping) else {})
        return payload

    def open_session(self, *, timeout_s: float | None = None) -> Mapping[str, Any]:
        if self._config.hosted and not self._config.authenticated:
            raise ClientError(
                ErrorCode.UNAUTHENTICATED,
                "Hosted Review requires sign-in; run `lc auth login` first",
                action=AgentAction.REAUTHENTICATE,
            )
        if not self._state.protocol_version:
            self.handshake(timeout_s=timeout_s)
        payload = self._transport.post(
            "/v1/sessions",
            body=self._negotiation_body(self._state.protocol_version),
            headers=self._bearer(),
            timeout_s=timeout_s,
        ).require()
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ClientError(
                ErrorCode.INTERNAL, "server opened a Review session with no id", action=AgentAction.ABANDON
            )
        self._state.session_id = session_id
        raw_caps = payload.get("capabilities")
        self._state.capabilities = (
            frozenset(str(name) for name in raw_caps if isinstance(name, str))
            if isinstance(raw_caps, list)
            else frozenset()
        )
        proof = payload.get("local_fs_proof_token")
        self._state.local_fs_proof = proof if isinstance(proof, str) else ""
        limits = payload.get("limits")
        if isinstance(limits, Mapping):
            self._state.adopt_limits(limits)
        self._state.online = True
        return payload

    def open_snapshot(
        self,
        snapshot_root: Path,
        *,
        source_revision: str,
        timeout_s: float | None = None,
        allow_oversize: bool = False,
    ) -> Mapping[str, Any]:
        """Upload one already-frozen immutable Review snapshot."""

        root = snapshot_root.expanduser().resolve()
        walk = walk_worktree(
            root,
            size_cap=self._state.content_size_cap if allow_oversize else self._state.max_blob_bytes,
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

        # Prove the original checkout, never the temporary frozen snapshot.
        # Manifest digests still pin every uploaded byte to the reviewed side.
        local_fs = None
        if (
            self._config.offer_local_fs
            and SAME_HOST_CAPABILITY in self._state.capabilities
            and self._state.local_fs_proof
            and write_local_fs_proof(self._config.repo_root, self._state.local_fs_proof)
        ):
            local_fs = {"root": str(self._config.repo_root.resolve()), "proof_path": LOCAL_FS_PROOF_PATH}
        entries = walk.entries()
        self._chunks = chunk_manifest(
            entries,
            max_rows=min(_MAX_CHUNK_ROWS, self._state.max_manifest_entries_per_chunk),
        )
        opened = dict(
            self._transport.post(
                "/v1/views/open",
                body={
                    "repo_identity": repo_identity(self._config.repo_root).to_wire(),
                    "base_revision": source_revision or "review-snapshot",
                    "manifest_root": manifest_root(entries),
                    "manifest_chunks": [chunk.chunk_id for chunk in self._chunks],
                    "lease_s": _REVIEW_SNAPSHOT_LEASE_S,
                    **({"local_fs": local_fs} if local_fs is not None else {}),
                },
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
            opened["missing_digests"] = state.get(
                "outstanding_digests",
                state.get("missing_digests", []),
            )

        missing = set(_string_list(opened.get("missing_digests")))
        if not allow_oversize:
            missing.update(entry.content_digest for entry in entries if entry.size > self._state.content_size_cap)
        sources = {file.content_digest: root / file.path for file in walk.uploadable()}
        stored, plaintext = self._blobs.upload_digests(
            tuple(sorted(missing)),
            sources,
            timeout_s=timeout_s,
        )
        opened["manifest_chunks_sent"] = sent
        opened["uploaded_blobs"] = stored
        opened["uploaded_bytes"] = plaintext
        opened["oversize_paths"] = walk.oversize
        self._state.online = True
        return opened

    def open_delta_snapshot(
        self,
        entries: Mapping[str, ManifestEntry],
        *,
        source_revision: str,
        payload: Callable[[str], tuple[bytes, str]],
        on_open: Callable[[Mapping[str, Any]], None] | None = None,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """Content-addressed View, fetching only blobs the server reports missing."""
        ordered = tuple(entries[path] for path in sorted(entries))
        if len(ordered) > 200_000 or any(
            entry.size > min(self._state.max_blob_bytes, self._state.content_size_cap) for entry in ordered
        ):
            raise ClientError(ErrorCode.REQUEST_TOO_LARGE, "Review delta exceeds server limits")
        chunks = chunk_manifest(ordered, max_rows=min(_MAX_CHUNK_ROWS, self._state.max_manifest_entries_per_chunk))
        opened = dict(
            self._transport.post(
                "/v1/views/open",
                body={
                    "repo_identity": repo_identity(self._config.repo_root).to_wire(),
                    "base_revision": source_revision or "review-snapshot",
                    "manifest_root": manifest_root(ordered),
                    "manifest_chunks": [chunk.chunk_id for chunk in chunks],
                    "lease_s": _REVIEW_SNAPSHOT_LEASE_S,
                },
                headers=self._auth(),
                timeout_s=timeout_s,
            ).require()
        )
        view_id, repo_id = opened.get("view_id"), opened.get("repo_id")
        if not isinstance(view_id, str) or not view_id or not isinstance(repo_id, str) or not repo_id:
            raise ClientError(ErrorCode.VIEW_UNKNOWN, "server did not open a Review delta View")
        self._state.view_id, self._state.repo_id = view_id, repo_id
        self._state.view_revision = int(opened.get("view_revision", 0) or 0)
        if on_open is not None:
            # The server assigns branch publication order as soon as the View
            # exists, before a slow transfer could invert two publishers.
            on_open(opened)
        sent = self._blobs.send_chunks(chunks, _string_list(opened.get("need_manifest_chunks")), timeout_s=timeout_s)
        if sent:
            reply = self._transport.post(
                f"/v1/views/{view_id}/missing",
                body={"digests": []},
                headers=self._auth(with_revision=False),
                timeout_s=timeout_s,
            ).require()
            opened["missing_digests"] = reply.get("outstanding_digests", reply.get("missing_digests", []))
        missing = sorted(set(_string_list(opened.get("missing_digests"))))
        if not set(missing).issubset({entry.content_digest for entry in ordered}):
            raise ClientError(ErrorCode.MANIFEST_INCOMPLETE, "server requested unmanifested source")
        uploaded = 0
        plaintext = 0
        for start in range(0, len(missing), 32):
            batch = missing[start : start + 32]
            items = {digest: payload(digest) for digest in batch}
            if any(sha256_hex(data) != digest for digest, (data, _profile) in items.items()):
                raise ClientError(ErrorCode.VIEW_REVISION_STALE, "source changed during Review upload", retryable=True)
            count, size = self._blobs.upload_payloads(
                batch, items, timeout_s=timeout_s, completion_check=start + len(batch) >= len(missing)
            )
            uploaded += count
            plaintext += size
        opened.update(manifest_chunks_sent=sent, uploaded_blobs=uploaded, uploaded_bytes=plaintext)
        return opened

    def begin_capture_intent(self, repo_id: str, *, branch: str, mode: str) -> str:
        answer = self._transport.post(
            f"/v1/repos/{quote(repo_id, safe='')}/review-capture-intents",
            body={
                "workspace_branch": branch,
                "range_mode": mode,
                "view_id": self._state.view_id,
                "view_revision": self._state.view_revision,
            },
            headers=self._auth(),
            timeout_s=self._config.request_timeout_s,
        )
        if answer.status == 404:
            # Old Hosted servers have no capture-intent route. Compatibility is
            # explicit, not a broad fallback on auth/network/storage failures.
            return ""
        response = answer.require()
        token = response.get("capture_intent", "")
        if not isinstance(token, str) or (
            token and (len(token) != 64 or any(c not in "0123456789abcdef" for c in token))
        ):
            raise ClientError(ErrorCode.PAYLOAD_INVALID, "invalid Hosted capture ticket")
        return token

    def capture_from_views(
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
        workspace_branch: str = "",
        capture_intent: str = "",
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
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
            **({"workspace_branch": workspace_branch} if workspace_branch else {}),
            **({"capture_intent": capture_intent} if capture_intent else {}),
        }
        manifest = self._state.review_manifest
        if manifest is None:
            self.handshake(timeout_s=self._config.request_timeout_s)
            manifest = self._state.review_manifest
        if manifest is not None:
            manifest.require_capture("derived", {key: value for key, value in body.items() if key != "capture_intent"})
        path = f"/v1/repos/{quote(target_repo, safe='')}/reviews"
        payload = await_capture(
            self._transport,
            self._auth,
            self._config.request_timeout_s,
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

    def branch_review(self, repo_id: str, *, branch: str, range_mode: str, timeout_s: float | None = None) -> Response:
        """The server's answer to the branch Review lookup, unread.

        Returned whole so the caller can tell a server without the lookup (404)
        from one that served it and refused.
        """

        query = urlencode({"range_mode": range_mode, "branch": branch, "repo_id": repo_id})
        return self._transport.get(f"{BRANCH_REVIEW_PATH}?{query}", headers=self._bearer(), timeout_s=timeout_s)

    def review_url(self, review_id: str) -> str:
        internal_id = review_id[2:] if review_id.startswith("r/") else review_id
        return self._config.endpoint(f"/r/{quote(internal_id, safe='')}")

    def close(self) -> None:
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
                        f"/v1/sessions/{self._state.session_id}",
                        headers=self._bearer(),
                        timeout_s=2.0,
                    )
                except ClientError:
                    pass
        finally:
            self._transport.close()
