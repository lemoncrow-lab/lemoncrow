"""What this client declares at the handshake.

The server refuses a client that does not offer ``blob_miss.v1``, and it is
right to: a client that cannot answer ``{"need": [...]}`` turns every cold read
into a hard failure, and from the server's side that is indistinguishable from
a broken client. So the required capability is not a constant this module may
drift from -- it is the set :mod:`lemoncrow_client.blobs` actually implements,
and ``tests/test_protocol_conformance.py`` asserts the two agree with the
server's own ``REQUIRED_CLIENT_CAPABILITIES`` when the server is importable.

The protocol version is a date string. A mismatch is refused loudly at
``POST /v1/handshake`` before authentication, so a client that must be upgraded
learns that instead of seeing a confusing 401.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "CLIENT_CAPABILITIES",
    "HEADER_HOST",
    "HEADER_HOST_SESSION",
    "HEADER_MODEL",
    "HEADER_REQUEST_ID",
    "HEADER_REVISION",
    "HEADER_SESSION",
    "HEADER_VIEW",
    "PROTOCOL_VERSION",
    "REQUIRED_CLIENT_CAPABILITIES",
    "RESULT_REUSE_CAPABILITY",
    "SAME_HOST_CAPABILITY",
]

#: Wire protocol this build speaks. Must match the server's
#: ``lemoncrow_server_core.protocol.PROTOCOL_VERSION``.
PROTOCOL_VERSION: Final[str] = "2026-09-14"

#: The minimum the server will accept. Offering less is refused at handshake.
REQUIRED_CLIENT_CAPABILITIES: Final[frozenset[str]] = frozenset({"blob_miss.v1"})

#: Everything this client implements. Each name corresponds to code in this
#: package, not to an aspiration:
#:
#: ``blob_miss.v1``    :meth:`lemoncrow_client.blobs.BlobService.fill`
#: ``tools.v1``        :meth:`lemoncrow_client.session.RemoteSession.call_tool`
#: ``views.v1``        :meth:`lemoncrow_client.session.RemoteSession.open_view`
#: ``blobs.v1``        :meth:`lemoncrow_client.blobs.BlobService.upload`
#: ``overlay.v1``      :meth:`lemoncrow_client.blobs.BlobService.push`
#: ``index_query.v1``  :meth:`lemoncrow_client.session.RemoteSession.query_index`
RESULT_REUSE_CAPABILITY: Final[str] = "result_reuse.v1"

CLIENT_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "blob_miss.v1",
        "tools.v1",
        "views.v1",
        "blobs.v1",
        "overlay.v1",
        "index_query.v1",
        RESULT_REUSE_CAPABILITY,
    }
)

#: Offered only when the operator opted in *and* the client can write a proof
#: file into the worktree. It is an optimization the server may decline; the
#: upload path is the one that always works and the one CI runs.
SAME_HOST_CAPABILITY: Final[str] = "local_fs"

HEADER_SESSION: Final[str] = "X-LemonCrow-Session"
HEADER_VIEW: Final[str] = "X-LemonCrow-View"
HEADER_REVISION: Final[str] = "X-LemonCrow-View-Revision"
HEADER_REQUEST_ID: Final[str] = "X-LemonCrow-Request-Id"
HEADER_HOST_SESSION: Final[str] = "X-LemonCrow-Host-Session"
HEADER_HOST: Final[str] = "X-LemonCrow-Host"
HEADER_MODEL: Final[str] = "X-LemonCrow-Model"
