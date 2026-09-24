"""``index``: the one row of the matrix that is not a tool call at all.

The matrix says ``client -> server``: "client enumerates and uploads; server
builds". There is no ``POST /v1/tools/index`` -- the server refuses that name
with ``use_sync_protocol`` -- because indexing *is* the ``/v1/views`` +
``/v1/blobs`` protocol. So this executor re-walks the worktree, re-negotiates
the canonical manifest root and fills whatever the server is missing, then
reports what moved.

What it very deliberately does not do is build anything locally. The design's
non-goals are explicit: with no reachable server LemonCrow "degrades to a
documented reduced mode; it does not silently rebuild a local heavyweight
index". So with no session this returns a typed refusal naming that, and never
a 1.1 GB SQLite file in the developer's worktree.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import AgentAction, ClientError, ErrorCode
from . import LocalContext, LocalResult

__all__ = ["run_index"]


def run_index(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    """Re-enumerate the worktree and hand the server what it lacks."""
    sync = context.sync
    if sync is None or not sync.online:
        raise ClientError(
            ErrorCode.SERVER_SESSION_UNAVAILABLE,
            (
                "indexing happens on the LemonCrow server and there is no session. "
                "This client never builds a local index"
            ),
            retryable=True,
            action=AgentAction.RETRY_LATER,
        )
    for unsupported in ("include_globs", "exclude_globs"):
        if arguments.get(unsupported):
            raise ClientError(
                ErrorCode.PAYLOAD_INVALID,
                (
                    f"{unsupported} is not accepted: view membership is the canonical "
                    ".gitignore-respecting walk, so that local search and server search "
                    "describe the same repository"
                ),
                details={"argument": unsupported},
                action=AgentAction.FIX_REQUEST,
            )

    report = sync.resync()
    lines = [
        f"view {report.get('view_id', '')} rev {report.get('view_revision', 0)}",
        f"manifest root {str(report.get('manifest_root', ''))[:16]}… "
        f"({report.get('entry_count', 0)} entries, warm={bool(report.get('warm'))})",
        f"manifest chunks sent: {report.get('manifest_chunks_sent', 0)}",
        f"blobs uploaded: {report.get('uploaded_blobs', 0)} " f"({report.get('uploaded_bytes', 0)} bytes of content)",
    ]
    outstanding = report.get("missing_digests")
    if isinstance(outstanding, list) and outstanding:
        lines.append(f"still outstanding: {len(outstanding)} digest(s)")
    layers = report.get("layers")
    if isinstance(layers, Mapping):
        for name in sorted(layers):
            status = layers[name]
            if isinstance(status, Mapping):
                lines.append(f"{name} layer: {status.get('state')} ({status.get('covered_paths', 0)} paths)")
    return LocalResult(
        content=({"type": "text", "text": "\n".join(lines)},),
        structured={
            "view_id": report.get("view_id", ""),
            "view_revision": report.get("view_revision", 0),
            "warm": bool(report.get("warm")),
            "uploaded_blobs": report.get("uploaded_blobs", 0),
        },
    )
