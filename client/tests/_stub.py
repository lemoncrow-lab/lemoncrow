"""A loopback stand-in for the enterprise server.

The real server is what ``test_end_to_end_server.py`` drives, over a real
socket, and that is the test that proves the client speaks the protocol. This
stub exists for the cases the real server cannot easily be pushed into: an
older protocol version, a credential that is refused, a blob miss on demand, a
stale revision, a socket that stops answering mid-session.

It is faithful where it matters -- it enforces the revision binding, the
monotonic ``client_seq``, the blob-miss-exactly-once rule and digest
verification -- and deliberately thin everywhere else. Where it diverges from
the real server, the end-to-end suite is the one that counts.

The stub binds the port. The client never does, which is what
``test_packaging_audit.py`` asserts.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROTOCOL_VERSION = "2026-09-14"
SERVER_CAPABILITIES = {
    "tools.v1",
    "views.v1",
    "blobs.v1",
    "overlay.v1",
    "blob_miss.v1",
    "index_query.v1",
    "result_reuse.v1",
    "local_fs",
}
REQUIRED_CLIENT_CAPABILITIES = {"blob_miss.v1"}
TOKEN = "stub-token-0123456789abcdef"
CONTENT_SIZE_CAP = 1024 * 1024
CACHEABLE_TOOLS = {"code_search", "read", "relations", "search"}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class StubState:
    """Everything the stub remembers, and the knobs a test turns."""

    # -- knobs --------------------------------------------------------- #
    protocol_version: str = PROTOCOL_VERSION
    token: str = TOKEN
    content_size_cap: int = CONTENT_SIZE_CAP
    allow_local_fs: bool = False
    #: Tools that answer ``{need: [...]}`` once before succeeding.
    miss_once: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Tools that answer ``{need: [...]}`` every time, however often the client
    #: fills. Models a client that cannot satisfy the pull, which is the case
    #: the "exactly one retry" rule exists for.
    miss_always: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Declared blob ceiling, so a test can drive the batching path.
    max_blobs_per_request: int = 256
    #: Answer every request with ``server_unreachable``-shaped failure by
    #: closing the connection. Flipped mid-session by a test.
    dead: bool = False
    #: Refuse the next tool call as ``view_revision_stale``, naming a revision.
    stale_once: bool = False

    # -- observed state ------------------------------------------------ #
    blobs: dict[str, bytes] = field(default_factory=dict)
    manifest: dict[str, dict[str, Any]] = field(default_factory=dict)
    announced_chunks: set[str] = field(default_factory=set)
    chunks_received: list[str] = field(default_factory=list)
    view_revision: int = 0
    view_id: str = ""
    session_id: str = ""
    last_client_seq: int = 0
    replays: dict[int, int] = field(default_factory=dict)
    tool_calls: list[tuple[str, Mapping[str, Any], int]] = field(default_factory=list)
    reuse_hits: int = 0
    blob_requests: int = 0
    miss_served: set[str] = field(default_factory=set)
    local_fs_offer: Mapping[str, Any] | None = None
    manifest_root: str = ""
    closed_views: list[str] = field(default_factory=list)
    closed_sessions: list[str] = field(default_factory=list)
    requested_paths: list[str] = field(default_factory=list)
    review_captures: list[Mapping[str, Any]] = field(default_factory=list)
    #: Every review request in arrival order, drafts and appends included.
    review_requests: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)

    def content_for(self, path: str) -> bytes | None:
        row = self.manifest.get(path)
        if row is None:
            return None
        return self.blobs.get(str(row["content_digest"]))


def _error(code: str, message: str, *, retryable: bool = False, action: str = "none", **details: Any):
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "action": action,
            "details": details,
        }
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> StubState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, *args: Any) -> None:
        return

    # -- plumbing ------------------------------------------------------ #

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticated(self) -> bool:
        presented = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return presented == self.state.token

    def do_GET(self) -> None:
        if self.state.dead:
            self.close_connection = True
            return
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        self._send(404, _error("tool_unknown", "no such route"))

    def do_DELETE(self) -> None:
        if self.state.dead:
            self.close_connection = True
            return
        if self.path.startswith("/v1/sessions/"):
            self.state.closed_sessions.append(self.path.rsplit("/", 1)[-1])
            self._send(200, {"closed": True})
            return
        self._send(404, _error("session_unknown", "no such session"))

    def do_POST(self) -> None:
        state = self.state
        if state.dead:
            # A server that has gone away does not answer politely.
            self.close_connection = True
            return
        try:
            body = self._body()
        except ValueError:
            self._send(400, _error("payload_invalid", "body is not JSON"))
            return

        path = self.path
        if path == "/v1/handshake":
            self._handshake(body)
            return
        if not self._authenticated():
            self._send(401, _error("unauthenticated", "bad credential", action="reauthenticate"))
            return
        if path == "/v1/sessions":
            self._open_session(body)
            return
        if path == "/v1/views/open":
            self._open_view(body)
            return
        if path == "/v1/blobs":
            self._blobs(body)
            return
        if path.startswith("/v1/views/") and path.endswith("/manifest-chunks"):
            self._manifest_chunk(body)
            return
        if path.startswith("/v1/views/") and path.endswith("/missing"):
            self._missing(body)
            return
        if path.startswith("/v1/views/") and path.endswith("/overlay"):
            self._overlay(body)
            return
        if path.startswith("/v1/views/") and path.endswith("/query"):
            self._query(body)
            return
        if path.startswith("/v1/views/") and path.endswith("/close"):
            state.closed_views.append(path.split("/")[3])
            self._send(200, {"closed": True})
            return
        if path.startswith("/v1/repos/") and "/reviews/drafts/" in path:
            state.review_requests.append((path, dict(body)))
            if body.get("final"):
                self._send(201, {"review": {"id": "rev_hosted_stub"}, "revision_created": True})
            else:
                self._send(202, {"draft_id": "rdraft_stub", "next_seq": int(body["seq"]) + 1})
            return
        if path.startswith("/v1/repos/") and path.endswith("/reviews"):
            state.review_requests.append((path, dict(body)))
            if body.get("draft"):
                self._send(202, {"draft_id": "rdraft_stub", "next_seq": 1})
                return
            state.review_captures.append(dict(body))
            self._send(
                201,
                {
                    "review": {"id": "rev_hosted_stub"},
                    "revision": {"id": "rrv_hosted_stub", "revision_number": 1},
                    "revision_created": True,
                },
            )
            return
        if path.startswith("/v1/tools/"):
            self._tool(path.rsplit("/", 1)[-1], body)
            return
        self._send(404, _error("tool_unknown", f"no such route: {path}"))

    # -- routes -------------------------------------------------------- #

    def _handshake(self, body: Mapping[str, Any]) -> None:
        state = self.state
        offered = {str(name) for name in body.get("capabilities", [])}
        if body.get("protocol_version") != state.protocol_version:
            self._send(
                426,
                _error(
                    "protocol_version_mismatch",
                    "client protocol is not supported by this server",
                    action="upgrade_client",
                    server_protocol=state.protocol_version,
                ),
            )
            return
        missing = sorted(REQUIRED_CLIENT_CAPABILITIES - offered)
        if missing:
            self._send(
                400,
                _error(
                    "capability_unsupported",
                    f"client does not offer required capabilities: {', '.join(missing)}",
                    action="upgrade_client",
                ),
            )
            return
        granted = (offered & SERVER_CAPABILITIES) | REQUIRED_CLIENT_CAPABILITIES
        if not state.allow_local_fs:
            granted.discard("local_fs")
        self._send(
            200,
            {
                "protocol_version": state.protocol_version,
                "capabilities": sorted(granted),
                "local_fs": state.allow_local_fs and "local_fs" in granted,
            },
        )

    def _open_session(self, body: Mapping[str, Any]) -> None:
        state = self.state
        if body.get("protocol_version") != state.protocol_version:
            self._send(
                426,
                _error("protocol_version_mismatch", "unsupported", action="upgrade_client"),
            )
            return
        offered = {str(name) for name in body.get("capabilities", [])}
        granted = (offered & SERVER_CAPABILITIES) | REQUIRED_CLIENT_CAPABILITIES
        if not state.allow_local_fs:
            granted.discard("local_fs")
        state.session_id = "sess_stub"
        self._send(
            200,
            {
                "session_id": state.session_id,
                "protocol_version": state.protocol_version,
                "capabilities": sorted(granted),
                "local_fs": "local_fs" in granted,
                # The nonce a same-host offer has to be carrying. Chosen by the
                # server, exactly as the real one is.
                "local_fs_proof_token": "stub-local-fs-nonce-0123456789ab" if "local_fs" in granted else "",
                "expires_at": 0.0,
                "view_id": None,
                "acknowledged_view_revision": 0,
                "limits": {
                    "max_request_bytes": 4 * 1024 * 1024,
                    "max_blob_bytes": 8 * 1024 * 1024,
                    "max_blobs_per_request": state.max_blobs_per_request,
                    "max_manifest_entries_per_chunk": 5000,
                    "max_overlay_entries": 256,
                },
            },
        )

    def _open_view(self, body: Mapping[str, Any]) -> None:
        state = self.state
        for rejected in ("remote_url", "remote", "origin_url", "clone_url"):
            if rejected in (body.get("repo_identity") or {}):
                self._send(400, _error("payload_invalid", f"repo_identity.{rejected} is not accepted"))
                return
        root = str(body.get("manifest_root") or "")
        announced = [str(entry) for entry in body.get("manifest_chunks", [])]
        warm = bool(state.manifest_root) and state.manifest_root == root and not state.announced_chunks
        if not warm:
            state.manifest_root = root
            state.announced_chunks = set(announced) - set(state.chunks_received)
        state.view_id = state.view_id or "view_stub"
        state.local_fs_offer = body.get("local_fs")
        self._send(
            200,
            {
                "view_id": state.view_id,
                "repo_id": "repo_stub",
                "base_revision": str(body.get("base_revision") or ""),
                "view_revision": state.view_revision,
                "manifest_root": root,
                "entry_count": len(state.manifest),
                "lease_expires_at": 0.0,
                "need_manifest_chunks": sorted(state.announced_chunks),
                "manifest_complete": not state.announced_chunks,
                "have_link_ancestor": False,
                "warm": warm,
                "needs_manifest": not announced and not state.manifest,
                "missing_digests": self._outstanding(),
                "layers": {
                    "link": {"state": "ready", "reason": "", "covered_paths": len(state.manifest)},
                    "semantic": {"state": "ready", "reason": "", "covered_paths": len(state.manifest)},
                },
                "local_fs": {"granted": bool(state.allow_local_fs and state.local_fs_offer)},
                "content_size_cap": state.content_size_cap,
            },
        )

    def _manifest_chunk(self, body: Mapping[str, Any]) -> None:
        state = self.state
        chunk_id = str(body.get("chunk_id") or "")
        if chunk_id not in state.announced_chunks:
            self._send(400, _error("payload_invalid", "chunk_id was not announced at views/open"))
            return
        for entry in body.get("entries", []):
            state.manifest[str(entry["path"])] = dict(entry)
        state.announced_chunks.discard(chunk_id)
        state.chunks_received.append(chunk_id)
        self._send(
            200,
            {
                "view_id": state.view_id,
                "view_revision": state.view_revision,
                "manifest_root": state.manifest_root,
                "entry_count": len(state.manifest),
                "need_manifest_chunks": sorted(state.announced_chunks),
                "manifest_complete": not state.announced_chunks,
                "missing_digests": self._outstanding(),
            },
        )

    def _outstanding(self) -> list[str]:
        state = self.state
        wanted: list[str] = []
        for row in state.manifest.values():
            digest = str(row["content_digest"])
            if int(row.get("size", 0)) > state.content_size_cap:
                continue
            if digest not in state.blobs and digest not in wanted:
                wanted.append(digest)
        return wanted

    def _missing(self, body: Mapping[str, Any]) -> None:
        """Mirrors the real route, including the part that is easy to get wrong.

        A digest the view's manifest does not declare reads as *missing*,
        whether or not the organization holds it. That is what stops the resume
        point being a probe -- and it is also what lets a push upload new
        content before the overlay row that will declare it exists.
        """
        state = self.state
        asked = [str(entry) for entry in body.get("digests", [])]
        outstanding = self._outstanding()
        declared = {str(row["content_digest"]) for row in state.manifest.values()}
        answer = [digest for digest in asked if digest in declared and digest in outstanding]
        undeclared = [digest for digest in asked if digest not in declared]
        self._send(
            200,
            {"missing_digests": answer + undeclared, "outstanding_digests": sorted(outstanding)},
        )

    def _blobs(self, body: Mapping[str, Any]) -> None:
        state = self.state
        state.blob_requests += 1
        stored: list[str] = []
        total = 0
        for raw in body.get("blobs", []):
            digest = str(raw.get("content_digest") or "")
            payload = base64.b64decode(str(raw.get("data") or ""), validate=True)
            if str(raw.get("encoding") or "base64") == "gzip-base64":
                payload = gzip.decompress(payload)
            if sha256_hex(payload) != digest:
                self._send(400, _error("blob_digest_mismatch", "content does not hash to the digest"))
                return
            state.blobs[digest] = payload
            stored.append(digest)
            total += len(payload)
        self._send(200, {"stored": stored, "bytes": total})

    def _overlay(self, body: Mapping[str, Any]) -> None:
        state = self.state
        client_seq = int(body.get("client_seq") or 0)
        replay = state.replays.get(client_seq)
        if replay is not None:
            self._send(200, self._view_wire(replay))
            return
        if client_seq <= state.last_client_seq:
            self._send(400, _error("payload_invalid", "client_seq must increase monotonically"))
            return
        expected = int(body.get("expected_view_revision") or 0)
        if expected != state.view_revision:
            self._send(
                409,
                _error(
                    "view_revision_conflict",
                    "overlay write is based on a revision that is no longer current",
                    retryable=True,
                    action="refresh_view_revision",
                    committed_view_revision=state.view_revision,
                ),
            )
            return
        for entry in body.get("entries", []):
            state.manifest[str(entry["path"])] = dict(entry)
        for path in body.get("removed_paths", []):
            state.manifest.pop(str(path), None)
        state.view_revision += 1
        state.last_client_seq = client_seq
        state.replays[client_seq] = state.view_revision
        self._send(200, self._view_wire(state.view_revision))

    def _view_wire(self, revision: int) -> dict[str, Any]:
        state = self.state
        return {
            "view_id": state.view_id,
            "view_revision": revision,
            "manifest_root": state.manifest_root,
            "entry_count": len(state.manifest),
            "need_manifest_chunks": sorted(state.announced_chunks),
            "manifest_complete": not state.announced_chunks,
            "missing_digests": self._outstanding(),
        }

    def _claimed_revision(self, body: Mapping[str, Any]) -> int | None:
        header = self.headers.get("X-LemonCrow-View-Revision")
        if header is not None and header.strip():
            return int(header)
        raw = body.get("view_revision")
        return raw if isinstance(raw, int) else None

    def _bind_revision(self, body: Mapping[str, Any]) -> int | None:
        """Enforce the revision contract, returning ``None`` when it refused."""
        state = self.state
        claimed = self._claimed_revision(body)
        if claimed is None:
            self._send(
                409,
                _error(
                    "view_revision_unacknowledged",
                    "a tool call against a bound view must name the view_revision it observed",
                    action="refresh_view_revision",
                    committed_view_revision=state.view_revision,
                ),
            )
            return None
        if state.stale_once or claimed < state.view_revision:
            state.stale_once = False
            self._send(
                409,
                _error(
                    "view_revision_stale",
                    "the client has not observed the current committed view revision",
                    retryable=True,
                    action="refresh_view_revision",
                    claimed_view_revision=claimed,
                    committed_view_revision=state.view_revision,
                ),
            )
            return None
        if claimed > state.view_revision:
            self._send(
                409,
                _error(
                    "view_revision_unacknowledged",
                    "the client named a view revision this server has not committed",
                    action="refresh_view_revision",
                    committed_view_revision=state.view_revision,
                ),
            )
            return None
        return state.view_revision

    def _tool(self, name: str, body: Mapping[str, Any]) -> None:
        state = self.state
        bound = self._bind_revision(body)
        if bound is None:
            return
        arguments = body.get("arguments") or {}
        reuse_validator = _reuse_validator(name, arguments, bound) if name in CACHEABLE_TOOLS else ""
        if reuse_validator and body.get("reuse_validator") == reuse_validator:
            state.reuse_hits += 1
            self._send(
                200,
                {
                    "tool": name,
                    "reuse": True,
                    "reuse_validator": reuse_validator,
                    "view_revision": bound,
                },
            )
            return
        always = state.miss_always.get(name)
        if always:
            self._send(
                200,
                {"need": list(always), "view_id": state.view_id, "view_revision": bound, "retryable_once": True},
            )
            return
        need = state.miss_once.get(name)
        if need and name not in state.miss_served:
            state.miss_served.add(name)
            self._send(
                200,
                {"need": list(need), "view_id": state.view_id, "view_revision": bound, "retryable_once": True},
            )
            return
        state.tool_calls.append((name, dict(arguments), bound))
        text = self._render(name, arguments)
        payload: dict[str, Any] = {
            "tool": name,
            "content": [{"type": "text", "text": text}],
            "is_error": False,
            "degraded": False,
            "view_revision": bound,
        }
        if reuse_validator:
            payload["reuse_validator"] = reuse_validator
        self._send(200, payload)

    def _render(self, name: str, arguments: Mapping[str, Any]) -> str:
        """Answer ``read`` from the stored content, so a test can see the edit."""
        state = self.state
        if name != "read":
            return f"{name} ok"
        wanted = arguments.get("files") or []
        if isinstance(wanted, str):
            wanted = [wanted]
        blocks: list[str] = []
        for raw in wanted:
            path = raw.get("path") if isinstance(raw, Mapping) else str(raw)
            path = str(path).split(":", 1)[0]
            state.requested_paths.append(path)
            data = state.content_for(path)
            blocks.append(f"## {path}\n{'[missing]' if data is None else data.decode('utf-8')}")
        return "\n".join(blocks)

    def _query(self, body: Mapping[str, Any]) -> None:
        state = self.state
        bound = self._bind_revision(body)
        if bound is None:
            return
        needle = str(body.get("query") or "")
        hits = [
            {"path": path, "score": 1.0, "language": "", "line_count": 0, "size": int(row.get("size", 0))}
            for path, row in sorted(state.manifest.items())
            if needle and needle.encode("utf-8") in (state.content_for(path) or b"")
        ]
        self._send(
            200,
            {
                "kind": str(body.get("kind") or "lexical"),
                "hits": hits,
                "view_revision": bound,
                "searched_paths": len(state.manifest),
                "degraded": False,
                "truncated": False,
                "view_id": state.view_id,
            },
        )


def _reuse_validator(name: str, arguments: Mapping[str, Any], revision: int) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return "stub-rr1-" + hashlib.sha256(f"{revision}\x00{name}\x00{canonical}".encode()).hexdigest()


class StubServer:
    """A loopback HTTP server for one test."""

    def __init__(self, state: StubState | None = None) -> None:
        self.state = state or StubState()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.state = self.state  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> StubServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)
