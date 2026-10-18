"""The review workspace's HTTP surface -- routes only, never a server.

This module registers routes on a FastAPI app somebody else built and bound.
It opens no socket, mints no token and chooses no port: ``workspace.py`` owns
all three, and hands the token dependency in. That split is what lets the whole
API be tested with ``TestClient`` -- including every refusal -- without ever
listening on a port.

What it composes, and what it deliberately does not:

* The **only** review state is the one in ``ReviewStore``. These routes project
  it; they never invent a second opinion. The frontier is recomputed on every
  read (``revisions.compute_frontier``) precisely so it cannot drift from the
  marks it is derived from.
* Diff *rendering* is the browser's job. Source review receives the unified
  patch text git already produced, re-serialised from the stored packet. For
  Markdown, the same route may additionally return the exact old/new document
  texts pinned to the stored revision so the browser can render a rich preview.
  No diff is computed here and none is parsed.
* Every ranked row carries the sentences that ranked it. A score with no reason
  is a number the reader cannot argue with, which is the same as one they
  cannot trust (plan SS5.2).

Security (spec SS8) is not a layer on top of this module, it *is* this module's
first obligation, because the thing behind these routes is a private
repository:

* every route except ``/healthz`` requires the bearer token;
* ``Origin``/``Referer``/``Host`` are checked against the workspace's own
  origin, so a page on another local port cannot drive the API and a rebound
  DNS name cannot reach the listener;
* the response headers carry a CSP with ``frame-ancestors 'none'``;
* changed-file patch text comes from the stored revision artifact, never from
  today's worktree. The one related-source route is narrower still: it accepts
  only an out-of-patch path already named by this revision's impact evidence and
  reads it from the reviewed Git tree. There is no arbitrary repository browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import logging
import mimetypes
import os
import posixpath
import re
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from urllib.parse import unquote, urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from lemoncrow.core.foundation.optional_modules import module_absent
from lemoncrow.pro.capabilities.review.errors import (
    HostedActionRefused,
    HostedAttachmentConflict,
    ReviewArtifactReclaimed,
)
from lemoncrow.pro.capabilities.review.execution import verification_row
from lemoncrow.pro.capabilities.review.hosted_attachment import (
    CONFLICT_REASONS,
    REPLAY_STATES,
    RESOLVED_ACTION_KEEP,
    RESOLVED_ACTION_MAX_AGE_DAYS,
    UNRESOLVED_ACTION_LIMIT,
    action_wire,
    attached_identity,
    hosted_reference,
    parse_projection,
    prepare_action,
    reader_write_actions,
    requeue_keeps_content,
)
from lemoncrow.pro.capabilities.review.packet import REVIEW_JOURNAL
from lemoncrow.pro.capabilities.review.relationships import (
    change_context_payload as _change_context_payload,
)
from lemoncrow.pro.capabilities.review.relationships import (
    relationship_payload as _relationship_payload,
)
from lemoncrow.pro.capabilities.review.review_plan import review_brief_enabled as review_brief_enabled
from lemoncrow.pro.capabilities.review.review_priority import TIER_REVIEW_FIRST, legacy_group_for_tier
from lemoncrow.pro.capabilities.review.session_models import (
    ANCHOR_METHOD_LABELS,
    EXACT_ANCHOR_METHODS,
    FINDING_DISMISSAL_REASONS,
    FINDING_DISPOSITIONS,
    HOSTED_ACTION_STATES,
    HOSTED_ACTION_UNRESOLVED,
    REVIEW_EVIDENCE_KINDS,
    REVIEW_EVIDENCE_SOURCES,
    HostedAction,
    HostedAttachment,
    ReviewChangeProposal,
    ReviewDestination,
    ReviewEvidence,
    anchor_symbol_claim,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from lemoncrow.pro.capabilities.review.change_sets import ChangeSetDirectory
    from lemoncrow.pro.capabilities.review.code_intelligence import CodeIntelligenceProvider
    from lemoncrow.pro.capabilities.review.discussion_summary import DiscussionSummaryHost
    from lemoncrow.pro.capabilities.review.intelligence_policy import EffectiveIntelligencePolicy
    from lemoncrow.pro.capabilities.review.intelligence_runtime import ResolvedDriver
    from lemoncrow.pro.capabilities.review.project_context import (
        ProjectContext,
        ProjectContextRequest,
        ProjectKnowledgeBinding,
    )
    from lemoncrow.pro.capabilities.review.readiness import SnapshotProgress
    from lemoncrow.pro.capabilities.review.record_signing import RecordSigningAuthority
    from lemoncrow.pro.capabilities.review.rules import ModelRouteTrust
    from lemoncrow.pro.capabilities.review.session_models import (
        Annotation,
        DeliveryRecord,
        FrontierEntry,
        ReviewMark,
        ReviewRevision,
        ReviewSession,
        ReviewSessionStatus,
        ReviewUnit,
        ReviewUnitKind,
    )
    from lemoncrow.pro.capabilities.review.store import ReviewStore, ShortReferenceIndex
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget
    from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

HEALTHZ_PATH = "/healthz"
API_PREFIX = "/api"

# Large Reviews use a bounded startup projection so first-useful Code does not
# parse or serialize file-proportional chapter/group metadata before the first
# source diff is readable. Full endpoints remain authoritative and hydrate in
# the background after first paint.
LARGE_REVIEW_TARGET_THRESHOLD = 400
LARGE_REVIEW_STARTUP_TARGET_LIMIT = 50

#: What hosted owns while a local Review is attached (G5-SYNC contract).
_HOSTED_OWNED: tuple[str, ...] = ("comments", "judgments", "requests", "outcomes", "policy")
#: Largest hosted replay answer one queued action keeps.
_HOSTED_RESULT_LIMIT = 64 * 1024
#: Trust T2 shadow and on-mode consistency lines (operator telemetry, never product UI).
_TRUST_LOG = logging.getLogger("lemoncrow.review.trust")

# Emitted as real response headers, not a <meta> tag: a meta CSP cannot carry
# frame-ancestors, and frame-ancestors is exactly the clause that matters here
# -- a clickjacking frame could otherwise carry the bootstrap fragment that
# holds the token. `style-src 'unsafe-inline'` is required because
# `@pierre/diffs` injects stylesheets into its own shadow roots; scripts stay
# 'self' because Vite emits real asset files.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; connect-src 'self'; "
    "frame-src http://127.0.0.1:*; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cache-Control": "no-store",
}


# Origins the workspace is willing to be driven from. Both spellings of
# loopback, and nothing else: not "any 127.0.0.0/8 port" (that is every other
# local page), and not a wildcard.
def loopback_origins(port: int) -> tuple[str, ...]:
    """The only two origins this workspace answers to."""

    return (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def loopback_hosts(port: int) -> tuple[str, ...]:
    """The only two ``Host`` values this workspace answers to.

    A ``Host`` check is the DNS-rebinding defence: a name that resolves to
    127.0.0.1 reaches the listener, but it does not carry one of these two
    literal host values, so it is refused before any handler runs.
    """

    return (f"127.0.0.1:{port}", f"localhost:{port}")


def origin_refusal(
    *,
    host: str,
    origin: str,
    referer: str,
    port: int,
) -> str:
    """Why this request must be refused, or ``""`` when it may proceed.

    Pure, so every clause is testable without a socket. ``Origin`` and
    ``Referer`` are only checked when present -- a top-level navigation sends
    neither -- but ``Host`` is mandatory, because its absence is precisely how a
    rebinding probe avoids the check.
    """

    hosts = loopback_hosts(port)
    if not host:
        return "missing Host header"
    if host not in hosts:
        return f"Host {host!r} is not this workspace ({' or '.join(hosts)})"
    allowed = loopback_origins(port)
    if origin and origin not in allowed:
        return f"Origin {origin!r} is not this workspace"
    if referer and not any(referer.startswith(f"{item}/") or referer == item for item in allowed):
        return f"Referer {referer!r} is not this workspace"
    return ""


def make_token_dependency(token: str) -> Callable[..., None]:
    """A bearer-token dependency for *token*, shaped like the MCP daemon's.

    Unconditional by construction: there is no env flag and no config value that
    turns it off. A loopback bind is not an auth boundary -- every local process
    and every browser tab shares it -- which is why
    ``core/service/auth.py::verify_api_key`` (a no-op on loopback) must never be
    reused here.
    """

    def _verify_token(authorization: str = Header(default="")) -> None:
        scheme, _, presented = authorization.partition(" ")
        # `secrets.compare_digest(str, str)` rejects non-ASCII input. An
        # Authorization header is attacker-controlled, so compare UTF-8 bytes
        # and turn every malformed/foreign token into the same 403 instead of a
        # 500 with the security middleware bypassed by an exception.
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            presented.strip().encode("utf-8"), token.encode("utf-8")
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid workspace token")

    return _verify_token


# --------------------------------------------------------------------------- #
# patch text: re-serialised from the packet, never recomputed
# --------------------------------------------------------------------------- #

# The three origin characters a unified patch body may carry. libgit2 also emits
# '=', '>' and '<' for end-of-file-newline markers; a body carrying one of those
# is refused rather than guessed at, because emitting it into a patch would make
# the browser's parser read a line that is not a line.
_KNOWN_ORIGINS = (" ", "+", "-")

PATCH_REFUSALS: tuple[str, ...] = (
    "file_too_large",
    "binary_file",
    "no_hunks",
    "hunk_body_unavailable",
    "unknown_line_origin",
    "packet_unavailable",
    "file_not_in_packet",
)


def _drain_text(queue: asyncio.Queue[str]) -> str:
    chunks: list[str] = []
    while True:
        try:
            chunks.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return "".join(chunks)


async def _ask_stream_events(
    task: asyncio.Task[Any],
    deltas: asyncio.Queue[str],
    *,
    driver: str,
    model: str,
    heartbeat_s: float = 1.0,
) -> AsyncIterator[dict[str, Any]]:
    """Review Ask stream events after accepted: deltas/heartbeats, then outcome."""

    loop = asyncio.get_running_loop()
    started = last_working = loop.time()
    emitted_delta = False
    while not task.done():
        heartbeat_in = max(0.0, heartbeat_s - (loop.time() - last_working))
        try:
            first = await asyncio.wait_for(deltas.get(), timeout=heartbeat_in)
        except TimeoutError:
            if not task.done():
                last_working = loop.time()
                elapsed_ms = int((last_working - started) * 1000)
                yield {"type": "working", "elapsed_ms": elapsed_ms, "driver": driver, "model": model}
            continue
        text = first + _drain_text(deltas)
        if text:
            emitted_delta = True
            yield {"type": "delta", "text": text}

    rest = _drain_text(deltas)
    if rest:
        emitted_delta = True
        yield {"type": "delta", "text": rest}
    try:
        result = task.result()
    except HTTPException as exc:
        yield {"type": "error", "status": exc.status_code, "detail": str(exc.detail)}
        return
    except Exception as exc:
        yield {"type": "error", "status": 500, "detail": f"Review Ask failed: {exc}"}
        return

    # A durable cache hit performs no provider call, so there are no live token
    # deltas. Replay the cached body once to preserve the stream contract.
    if not emitted_delta and isinstance(result, Mapping):
        intelligence = result.get("intelligence")
        assistant = result.get("assistant")
        if isinstance(intelligence, Mapping) and bool(intelligence.get("cache_hit")) and isinstance(assistant, Mapping):
            cached_body = str(assistant.get("body") or "")
            if cached_body:
                yield {"type": "delta", "text": cached_body}
    yield {"type": "result", "result": result}


def _is_markdown_path(path: str) -> bool:
    """True for Markdown files the browser can render as documents."""

    return path.lower().endswith((".md", ".markdown", ".mdc"))


def _is_csv_path(path: str) -> bool:
    """True for CSV files the browser can render as bounded tables."""

    return path.lower().endswith(".csv")


_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))")
_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*(['\"])(.*?)\1", flags=re.IGNORECASE | re.DOTALL)
#: Fetches a Markdown document's remote images (SSRF-guarded); not in every build.
REMOTE_IMAGES = "lemoncrow.core.capabilities.web_fetch"


def _markdown_image_sources(text: str) -> frozenset[str]:
    """Image sources explicitly present in one Markdown document.

    This is a security boundary for the authenticated preview-image route, not a
    Markdown renderer. False negatives merely leave an image unresolved; false
    positives would turn that route into an arbitrary repository/network fetch.
    """

    sources: set[str] = set()
    for match in _MARKDOWN_IMAGE_RE.finditer(text):
        source = (match.group(1) or match.group(2) or "").strip()
        if source:
            sources.add(source)
    for match in _HTML_IMAGE_RE.finditer(text):
        source = (match.group(2) or "").strip()
        if source:
            sources.add(source)
    return frozenset(sources)


def _markdown_asset_repo_path(document_path: str, source: str) -> str | None:
    """Resolve a relative Markdown image source to a confined repo-relative path."""

    parsed = urlsplit(source)
    if parsed.scheme or parsed.netloc:
        return None
    raw_path = unquote(parsed.path).replace("\\", "/")
    if not raw_path or raw_path.startswith("#"):
        return None
    if raw_path.startswith("/"):
        candidate = posixpath.normpath(raw_path.lstrip("/"))
    else:
        candidate = posixpath.normpath(posixpath.join(posixpath.dirname(document_path), raw_path))
    if candidate in {"", ".", ".."} or candidate.startswith("../"):
        return None
    return candidate


def _snapshot_text(
    store: ReviewStore,
    review_id: str,
    revision: ReviewRevision,
    repo_root: Path,
    path: str,
    *,
    side: str,
) -> str | None:
    """Read one exact text file from an immutable Review side.

    The changed-file blob artifact is the fast path for working-tree/staged
    reviews. Commit ranges and hosted Reviews can reconstruct the same file from
    their immutable Git/source-tree Snapshot even when that cache artifact is
    absent. The mutable checkout is never consulted as file content.
    """

    if side not in {"old", "new"}:
        return None
    if side == "new":
        blobs = store.read_blob_artifact(review_id, revision.id)
        if blobs is not None and path in blobs:
            return blobs[path]

    tree_sha = (
        revision.base_sha if side == "old" else (revision.head_sha if revision.range_mode == "commit_range" else "")
    )
    if tree_sha:
        from lemoncrow.pro.capabilities.review.gitdiff import _blob_from_tree, _decode, _open_repo, _tree_for_sha

        try:
            repo = _open_repo(repo_root)
            tree = _tree_for_sha(repo, tree_sha)
            payload, reason = _blob_from_tree(repo, tree, path)
        except (KeyError, OSError, ValueError):
            payload, reason = b"", "unavailable"
        if not reason:
            return _decode(payload)

    # Hosted source trees and frozen working-tree overlays use the common
    # Snapshot materializer. Scope it to this one file so a document preview
    # never materializes an entire monorepo merely to render a table/document.
    from pathlib import Path as RuntimePath

    from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, materialize_review_side

    try:
        with tempfile.TemporaryDirectory(prefix="lc-review-text-") as raw_target:
            target = RuntimePath(raw_target)
            materialize_review_side(
                store.root,
                repo_root,
                revision,
                side=side,
                target=target,
                scope=path,
                store=store,
            )
            candidate = target / path
            if not candidate.is_file():
                return None
            return candidate.read_bytes().decode("utf-8", errors="replace")
    except (OSError, ReviewSnapshotUnavailable, ValueError):
        return None


def _text_document_preview_payload(
    store: ReviewStore,
    review_id: str,
    revision: ReviewRevision,
    repo_root: Path,
    entry: Mapping[str, Any],
    *,
    kind: str,
) -> dict[str, str] | None:
    path = str(entry.get("path") or "")
    if bool(entry.get("is_binary")):
        return None
    if kind == "markdown" and not _is_markdown_path(path):
        return None
    if kind == "csv" and not _is_csv_path(path):
        return None

    file_status = str(entry.get("status") or "modified")
    old_path = str(entry.get("old_path") or "") or path
    new_text: str | None
    old_text: str | None
    if file_status == "deleted":
        new_text = ""
    else:
        new_text = _snapshot_text(store, review_id, revision, repo_root, path, side="new")
        if new_text is None:
            return None
    if file_status == "added":
        old_text = ""
    else:
        old_text = _snapshot_text(store, review_id, revision, repo_root, old_path, side="old")
        if old_text is None:
            return None
    return {"kind": kind, "old_content": old_text, "new_content": new_text}


def _markdown_preview_payload(
    store: ReviewStore,
    review_id: str,
    revision: ReviewRevision,
    repo_root: Path,
    entry: Mapping[str, Any],
) -> dict[str, str] | None:
    """Return exact old/new Markdown texts for the stored review revision."""

    return _text_document_preview_payload(store, review_id, revision, repo_root, entry, kind="markdown")


def _csv_preview_payload(
    store: ReviewStore,
    review_id: str,
    revision: ReviewRevision,
    repo_root: Path,
    entry: Mapping[str, Any],
) -> dict[str, str] | None:
    """Return exact old/new CSV texts for a built-in table preview."""

    return _text_document_preview_payload(store, review_id, revision, repo_root, entry, kind="csv")


def _web_preview_payload(
    store: ReviewStore,
    revision: ReviewRevision,
    repo_root: Path,
    entry: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Describe revision-pinned routes affected by one frontend source file."""

    path = str(entry.get("path") or "")
    from lemoncrow.pro.capabilities.review.web_preview import infer_web_preview_descriptor

    try:
        descriptor = infer_web_preview_descriptor(repo_root, revision, path, store_root=store.root, store=store)
    except (OSError, ValueError):
        return None
    return descriptor.to_payload() if descriptor is not None else None


def _frozen_nested_web_preview_payload(
    store: ReviewStore,
    revision: ReviewRevision,
    repo_root: Path,
    path: str,
) -> dict[str, Any] | None:
    """Preview metadata for a file frozen inside a dirty submodule snapshot."""

    from lemoncrow.pro.capabilities.review.snapshot import frozen_submodule_changed_paths
    from lemoncrow.pro.capabilities.review.web_preview import infer_web_preview_descriptor

    if path not in set(frozen_submodule_changed_paths(store.root, revision, store=store)):
        return None
    try:
        descriptor = infer_web_preview_descriptor(repo_root, revision, path, store_root=store.root, store=store)
    except (OSError, ValueError):
        return None
    return descriptor.to_payload() if descriptor is not None else None


def _media_preview_payload(entry: Mapping[str, Any]) -> dict[str, str] | None:
    """Describe browser-renderable media without hiding meaningful textual source."""

    path = str(entry.get("path") or "")
    media_type = mimetypes.guess_type(path)[0] or ""
    is_binary = bool(entry.get("is_binary"))

    # SVG is often honest, useful source text. Keep it on the normal diff path
    # when Git can read it as text. PDFs, on the other hand, may happen to contain
    # only ASCII bytes while still being a rendered document rather than source a
    # reviewer should inspect line-by-line.
    if media_type == "image/svg+xml" and not is_binary:
        return None
    if media_type.startswith("image/"):
        media_kind = "image"
    elif media_type.startswith("video/"):
        media_kind = "video"
    elif media_type.startswith("audio/"):
        media_kind = "audio"
    elif media_type == "application/pdf":
        media_kind = "pdf"
    else:
        return None
    return {"kind": "media", "media_type": media_type, "media_kind": media_kind}


def _asset_entry(packet: Mapping[str, Any] | None, path: str, *, side: str) -> Mapping[str, Any] | None:
    direct = _files_by_path(packet).get(path)
    if direct is not None:
        return direct
    if side == "old" and packet is not None:
        for raw in packet.get("files") or ():
            if isinstance(raw, Mapping) and str(raw.get("old_path") or "") == path:
                return raw
    return None


def _revision_asset_bytes(
    store: ReviewStore,
    review_id: str,
    revision: ReviewRevision,
    repo_root: Path,
    packet: Mapping[str, Any] | None,
    path: str,
    *,
    side: str,
) -> tuple[bytes | None, str]:
    """Read exact old/new asset bytes without consulting today's mutable file."""

    from lemoncrow.pro.capabilities.review.gitdiff import (
        MAX_REVIEW_MEDIA_BYTES,
        _blob_from_tree,
        _open_repo,
        _tree_for_sha,
    )

    entry = _asset_entry(packet, path, side=side)
    if entry is not None:
        status_name = str(entry.get("status") or "modified")
        new_path = str(entry.get("path") or path)
        old_path = str(entry.get("old_path") or "") or new_path
        if side == "old" and status_name == "added":
            return None, "asset did not exist before this revision"
        if side == "new" and status_name == "deleted":
            return None, "asset was deleted in this revision"

        if side == "new" and revision.range_mode != "commit_range":
            if bool(entry.get("is_binary")):
                frozen_media = store.read_media_artifact(review_id, revision.id) or {}
                payload = frozen_media.get(new_path)
                if payload is None:
                    return None, "the reviewed revision did not preserve exact media bytes"
                return payload, ""
            frozen_text = store.read_blob_artifact(review_id, revision.id) or {}
            text = frozen_text.get(new_path)
            if text is None:
                return None, "the reviewed revision did not preserve exact file bytes"
            return text.encode("utf-8"), ""

        tree_path = old_path if side == "old" else new_path
    else:
        tree_path = path

    # Central/local server capture attaches complete immutable source-tree
    # manifests after the first diff becomes visible. Prefer that durable
    # content-addressed tree for both sides; the Reader must keep serving old
    # media even when the original checkout has moved, disappeared, or the API
    # is rooted at server scratch space rather than a Git worktree.
    source_tree = store.read_source_tree_artifact(review_id, revision.id, side=side)
    if source_tree is not None:
        raw = source_tree.get(tree_path)
        if raw is None:
            return None, f"asset did not exist on the {side} side of this revision"
        size = int(raw.get("size") or 0)
        digest = str(raw.get("content_digest") or "")
        if size > MAX_REVIEW_MEDIA_BYTES:
            return None, "asset exceeds the Review media size limit"
        payload = store.read_source_content(digest) if digest else None
        if payload is None or len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            return None, "the reviewed source tree did not preserve exact asset bytes"
        return payload, ""

    # Legacy/per-repository workspaces may not have a full source-tree artifact.
    # Their immutable Git revision remains a valid fallback.
    sha = (
        revision.base_sha
        if side == "old"
        else (revision.head_sha if revision.range_mode == "commit_range" else revision.base_sha)
    )
    if not sha:
        return None, "reviewed asset tree is unavailable"
    try:
        repo = _open_repo(repo_root)
        tree = _tree_for_sha(repo, sha)
        return _blob_from_tree(repo, tree, tree_path, max_bytes=MAX_REVIEW_MEDIA_BYTES)
    except (OSError, ValueError):
        return None, "asset is unreadable"


def synthesize_file_patch(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    """``(patch_text, refusal, detail)`` for one packet file entry.

    Not a diff algorithm and not a renderer: git already produced this patch and
    the packet already stores each hunk's body verbatim. All that is missing is
    the file header -- three to five lines whose contents we already know from
    ``status``/``old_path`` -- so this re-emits them and concatenates.

    A file whose body the producer could not record (``hunk_patch_truncated``)
    is refused with its reason. Rendering it half-populated would show a
    reviewer a diff that is missing lines without saying so, which is the one
    outcome worse than showing nothing.
    """
    path = str(entry.get("path") or "")
    old_path = str(entry.get("old_path") or "") or path
    file_status = str(entry.get("status") or "modified")
    if entry.get("diff_omitted_reason"):
        return "", "file_too_large", str(entry["diff_omitted_reason"])
    if bool(entry.get("is_binary")):
        return "", "binary_file", "the packet carries no text for a binary file"

    hunks = entry.get("hunks") or ()
    submodule_pointer = entry.get("submodule_pointer")
    if not hunks and isinstance(submodule_pointer, (list, tuple)) and len(submodule_pointer) == 2:
        old_commit = str(submodule_pointer[0] or "")
        new_commit = str(submodule_pointer[1] or "")
        submodule_lines = [f"diff --git a/{old_path} b/{path}"]
        if file_status == "added":
            submodule_lines.append("new file mode 160000")
        elif file_status == "deleted":
            submodule_lines.append("deleted file mode 160000")
        else:
            submodule_lines.append(f"index {old_commit[:7]}..{new_commit[:7]} 160000")
        submodule_lines.append("--- /dev/null" if file_status == "added" else f"--- a/{old_path}")
        submodule_lines.append("+++ /dev/null" if file_status == "deleted" else f"+++ b/{path}")
        if file_status == "added":
            submodule_lines.extend(("@@ -0,0 +1 @@", f"+Subproject commit {new_commit}"))
        elif file_status == "deleted":
            submodule_lines.extend(("@@ -1 +0,0 @@", f"-Subproject commit {old_commit}"))
        else:
            submodule_lines.extend(
                ("@@ -1 +1 @@", f"-Subproject commit {old_commit}", f"+Subproject commit {new_commit}")
            )
        return "\n".join(submodule_lines) + "\n", "", ""
    if not hunks:
        return "", "no_hunks", "this file entry carries no hunks"
    lines: list[str] = [f"diff --git a/{old_path} b/{path}"]
    if file_status == "added":
        lines.append("new file mode 100644")
    if file_status == "deleted":
        lines.append("deleted file mode 100644")
    if old_path != path:
        lines.append(f"rename from {old_path}")
        lines.append(f"rename to {path}")
    lines.append("--- /dev/null" if file_status == "added" else f"--- a/{old_path}")
    lines.append("+++ /dev/null" if file_status == "deleted" else f"+++ b/{path}")

    for hunk in hunks:
        header = str(hunk.get("header") or "")
        body = str(hunk.get("patch") or "")
        if not body:
            return "", "hunk_body_unavailable", f"{header or 'a hunk'} has no recorded body"
        body_lines = body.split("\n")
        if body_lines and body_lines[-1] == "":
            body_lines.pop()
        for body_line in body_lines:
            if body_line[:1] not in _KNOWN_ORIGINS and body_line.rstrip("\r") != "\\ No newline at end of file":
                return (
                    "",
                    "unknown_line_origin",
                    f"{header or 'a hunk'} carries line origin {body_line[:1]!r}",
                )
        lines.append(header)
        lines.extend(body_lines)
    return "\n".join(lines) + "\n", "", ""


# --------------------------------------------------------------------------- #
# degraded vocabulary: rendered, never swallowed
# --------------------------------------------------------------------------- #

_DEGRADED_TEXT: dict[str, str] = {
    "mode_only": "a file changed mode only; there is no text delta to review",
    "large_file_skipped": "a file was larger than the blob cap and its content was not loaded",
    "blob_unreadable": "a blob could not be read, so its content is unknown here",
    "dependency_side_unavailable": (
        "a dependency manifest or lockfile side could not be read, so it is reviewed as source, not package targets"
    ),
    "intent_to_add_undetermined": "an intent-to-add entry could not be classified",
    "symbol_relations": "the code index could not supply caller/callee relations",
    "centrality": "call-graph centrality was unavailable, so no symbol was ranked by it",
    "symbol_line_ranges": "symbol line ranges were unavailable for at least one file",
    "astgrep_unavailable": "ast-grep was unavailable, so structural matching fell back",
    "ambiguous_symbol_counts": "a symbol name resolved to more than one definition; counts are qualified",
    "caller_line_ranges": "caller line ranges were unavailable, so some sites carry no line",
    "symbol_resolution_truncated": "the symbol-qualification budget was reached; later symbols report unknown counts rather than borrowed zeros",
    "caller_queries_truncated": "the caller-expansion budget was reached; some changed symbols have caller counts without enumerated sites",
    "index_outline_incomplete": "the code index outline is incomplete for at least one file",
    "agent_reads_unrecorded": "this host records no file reads, so 'not inspected' is unknown, not a fact",
    "evidence_failed": "collecting verification evidence failed",
    "test_evidence_unavailable": "no test evidence was recorded; NOT_RUN means not observed, not 'did not happen'",
    "hunk_patch_truncated": "a hunk body exceeded the capture cap and was not stored, so that file cannot be rendered",
    "packet_unavailable": "the stored review packet is unavailable; diff context and packet-derived analysis are incomplete",
    "contract_literal_unparsed": (
        "at least one blob could not be parsed, so no contract literals were extracted from it"
    ),
    "provenance_store_missing": (
        "no agent history store was readable here, so provenance is unknown rather than empty"
    ),
}


def degraded_note(name: str) -> str:
    """One plain sentence for a degraded signal name.

    An unknown name still gets a sentence. Swallowing it because this table has
    not caught up would hide a producer that fell back, which is precisely the
    thing the vocabulary exists to make visible.
    """

    known = _DEGRADED_TEXT.get(name)
    if known is not None:
        return known
    if name.startswith("intent_to_add_excluded:"):
        _, _, count = name.partition(":")
        return f"{count} intent-to-add path(s) were excluded from this diff"
    if name.startswith("submodule_dirty:"):
        _, _, count = name.partition(":")
        return (
            f"{count} submodule(s) have uncommitted work in their own tree; "
            "the recorded pointer did not move, so they are not rows in this change"
        )
    if name.startswith("unmerged_paths:"):
        _, _, count = name.partition(":")
        return (
            f"{count} path(s) in this range are unmerged; a conflicted entry has no stage-0 content, "
            "so it is listed with no line changes because there is nothing to diff"
        )
    if name.startswith("lemoncrow_store_excluded:"):
        _, _, count = name.partition(":")
        return (
            f"{count} path(s) under LemonCrow's own store directory were left out of this diff; "
            "the skip is deliberate, and named here so a real change under it is not invisible"
        )
    if name.startswith("impact_"):
        return f"change-impact analysis fell back ({name})"
    if name.startswith("provenance_"):
        return f"agent-session correlation fell back ({name})"
    return f"a producer fell back ({name})"


# --------------------------------------------------------------------------- #
# left pane: the five attention groups (plan SS5.2)
# --------------------------------------------------------------------------- #

GROUP_KEYS: tuple[str, ...] = (
    "needs_attention",
    "changed_since_my_review",
    "unreviewed",
    "reviewed",
    "mechanical",
)

GROUP_LABELS: dict[str, str] = {
    "needs_attention": "Needs attention",
    "changed_since_my_review": "Changed since my review",
    "unreviewed": "Unreviewed",
    "reviewed": "Reviewed",
    "mechanical": "Mechanical / generated",
}

_MECHANICAL_GROUPS = ("generated", "vendor")
# Claim summaries Finish reports as evidence gaps (spec §4.7, §8).
_EVIDENCE_GAP_SUMMARIES = frozenset({"none", "partial", "contradictory", "inferred_failing"})
# A target-less file row sorts after every file the queue placed.
_NO_POSITION = 1_000_000_000


def group_for(entry: FrontierEntry, category: str) -> str:
    """Legacy left-pane group for a file entry that has no derived target.

    A file with targets takes ``review_priority.legacy_group_for_tier`` of its
    first outstanding queue entry instead (see :func:`build_groups`), so this
    fallback must never disagree with the queue about promotion: no ranker
    reason promotes a file (spec §7). Precedence:

    1. content that moved under a mark outranks everything -- it is the only
       group whose members were once believed done;
    2. an explicit ``needs_changes`` verdict is an open request, not a
       to-do item;
    3. a live ``reviewed`` mark means done;
    4. generated/vendor files are mechanical;
    5. everything else is unreviewed.
    """

    if entry.state == "changed_since_review" or entry.changed_since_mark:
        return "changed_since_my_review"
    if entry.state == "needs_changes":
        return "needs_attention"
    if entry.state == "reviewed":
        return "reviewed"
    if category in _MECHANICAL_GROUPS:
        return "mechanical"
    return "unreviewed"


_VCS_LETTER: dict[str, str] = {
    "added": "A",
    "modified": "M",
    "deleted": "D",
    "renamed": "R",
    "copied": "C",
    "typechange": "T",
}


def status_column(state: str, changed_since_mark: bool, file_status: str) -> str:
    """Two characters merging review state and VCS status, e.g. ``"/M"``.

    LemonCode's UX invention, adopted verbatim in shape: the reviewer reads one
    column instead of correlating a badge with a letter. A mark whose content
    has moved is never shown as done.
    """

    if changed_since_mark:
        glyph = "~"
    elif state == "reviewed":
        glyph = "✓"
    elif state == "needs_changes":
        glyph = "!"
    elif state == "unknown":
        glyph = "?"
    else:
        glyph = " "
    return f"{glyph}{_VCS_LETTER.get(file_status, ' ')}"


def _reason_lines(entry: FrontierEntry, group: str) -> list[str]:
    """The plain-language reasons for this row -- never empty.

    An empty reason list would render as a rank with no justification, which
    plan SS5.2 forbids outright. The two structural priors the ranker is allowed
    to stay silent about are spelled out here instead of left blank.
    """

    reasons = [reason for reason in entry.reasons if reason]
    if group == "changed_since_my_review":
        reasons.insert(0, "content changed after you marked it")
    if group == "reviewed":
        reasons.insert(0, "you marked this reviewed and its content has not moved")
    if entry.state == "unknown":
        reasons.insert(0, "content could not be fingerprinted, so no mark can attest it")
    if not reasons:
        reasons.append("no ranking signal fired -- nothing in this change points at it")
    return reasons


# --------------------------------------------------------------------------- #
# payload builders
# --------------------------------------------------------------------------- #


def _session_payload(
    session: ReviewSession,
    *,
    store: ReviewStore | None = None,
    references: ShortReferenceIndex | None = None,
    short_refs: bool = True,
    attachments: Mapping[str, HostedAttachment] | None = None,
) -> dict[str, Any]:
    """*references* (and *attachments*) let a list name every Review from one read of the domain.

    An attached Review (G5-SYNC) shows one identity: the canonical Change is
    hosted's, so ``id``/``ref``/``change_ref``/``short_id`` all name it (RC5)
    and the store resolves the hosted id back to this Review. The local id is
    only diagnostic provenance, ``hosted.local_review_id``.
    """

    attachment: HostedAttachment | None = None
    if attachments is not None:
        attachment = attachments.get(session.id)
    elif store is not None and store.hosted_attachment_supported():
        attachment = store.get_hosted_attachment(session.id)
    if attachment is not None:
        short_id = attachment.hosted_review_id
    elif not short_refs:
        short_id = session.id
    elif references is not None:
        short_id = references.shortest(session.id)
    elif store is not None:
        short_id = store.short_session_reference(session.id)
    else:
        short_id = session.id
    ref = f"r/{short_id}"
    payload: dict[str, Any] = {
        "id": session.id,
        "short_id": short_id,
        "ref": ref,
        "object": "change",
        "change_ref": ref,
        "title": session.title,
        "subject_type": session.subject_type,
        "range_mode": session.range_mode,
        "source_ref": session.source_ref,
        "review_base_sha": session.review_base_sha,
        "review_base_policy": session.review_base_policy,
        "status": session.status,
        "actor_type": session.actor_type,
        "reviewer_id": session.reviewer_id,
        "repo_root": session.repo_root,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }
    if attachment is not None:
        identity = attached_identity(
            local_review_id=session.id, origin=attachment.origin, hosted_review_id=attachment.hosted_review_id
        )
        payload.update({key: identity[key] for key in ("id", "short_id", "ref", "change_ref")})
        payload["hosted"] = {
            **identity["hosted"],
            "principal_id": attachment.principal_id,
            "synced_at": attachment.synced_at,
            "owns": list(_HOSTED_OWNED),
        }
    if not short_refs:
        del payload["short_id"]
    return payload


def _display_revision_number(revision: ReviewRevision) -> int:
    """Stable zero-based Review-local number shown outside persistence internals."""

    return revision.revision_number - 1


def _revision_payload(
    revision: ReviewRevision,
    *,
    store: ReviewStore | None = None,
    references: ShortReferenceIndex | None = None,
    short_refs: bool = True,
    commits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """*references* and *commits* (by sha) let a history name every Snapshot from one read each."""

    if not short_refs:
        short_id = revision.id
    elif references is not None:
        short_id = references.shortest(revision.id)
    elif store is not None:
        short_id = store.short_revision_reference(revision.id)
    else:
        short_id = revision.id
    ref = f"rr/{short_id}"
    commit = None
    if revision.capture_kind == "commit" and revision.commit_sha:
        if commits is not None:
            commit = commits.get(revision.commit_sha.strip())
        elif store is not None:
            commit = store.find_commit(revision.review_id, revision.commit_sha)
    return {
        "id": revision.id,
        "short_id": short_id,
        "ref": ref,
        "object": "snapshot",
        "snapshot_ref": ref,
        "revision_number": _display_revision_number(revision),
        "range_mode": revision.range_mode,
        "base_sha": revision.base_sha,
        "head_sha": revision.head_sha,
        "merge_base_sha": revision.merge_base_sha,
        "source_fingerprint": revision.source_fingerprint,
        "dirty": revision.dirty,
        "capture_kind": revision.capture_kind,
        "commit_sha": revision.commit_sha,
        "commit_number": commit.commit_number if commit is not None else None,
        "commit_state": commit.state if commit is not None else None,
        "branch_ref": revision.branch_ref,
        "worktree_key": revision.worktree_key,
        "title": revision.title,
        "packet_schema_version": revision.packet_schema_version,
        "degraded": list(revision.degraded),
        "provenance_host": revision.provenance_host,
        "provenance_model": revision.provenance_model,
        "provenance_session_id": revision.provenance_session_id,
        "provenance_certainty": revision.provenance_certainty,
        "created_at": revision.created_at,
    }


class UnitNaming(Protocol):
    """The five fields a unit's on-screen name is made of, and nothing else.

    A ``ReviewUnit`` satisfies it, and so does a ``DiscardedMark`` -- which is
    the point: a verdict whose unit has left the review still has to be named
    the way it was named while it was here, and the alternative was a second
    copy of this spelling written next to the discarded-verdict payload.
    Read-only members, so a frozen dataclass matches without variance games.
    """

    @property
    def kind(self) -> ReviewUnitKind: ...
    @property
    def path(self) -> str: ...
    @property
    def symbol(self) -> str: ...
    @property
    def ordinal(self) -> int: ...
    @property
    def start_line(self) -> int: ...


def unit_label(unit: UnitNaming, *, ambiguous: bool = False) -> str:
    """How one reviewable unit is written where a human reads it.

    The same spelling the terminal prints (``_unit_label`` in the CLI): a bare
    path for a file, ``path::symbol`` for a definition, ``path#ordinal`` for a
    hunk. One vocabulary across both surfaces, or a reviewer reads a row in the
    terminal and cannot find it in the browser.

    *ambiguous* says this revision holds another unit with the same name in the
    same file, and the label then carries the line that separates them. Their
    ``unit_key``s already differ, so nothing false is stored -- but on screen
    two rows reading ``svc.py::run`` are one row printed twice, and either click
    can land on the method the reviewer did not mean.
    """

    if not unit.path:
        return ""
    if unit.kind == "symbol":
        if not unit.symbol:
            return unit.path
        base = f"{unit.path}::{unit.symbol}"
        if not ambiguous:
            return base
        return f"{base}@L{unit.start_line}" if unit.start_line else f"{base}#{unit.ordinal}"
    if unit.kind == "dependency" and unit.symbol:
        return unit.symbol
    if unit.kind == "hunk":
        return f"{unit.path}#{unit.ordinal}"
    return unit.path


def ambiguous_symbols(units: Sequence[ReviewUnit]) -> frozenset[tuple[str, str]]:
    """``(path, symbol)`` pairs this revision holds more than one unit for.

    Computed over the whole revision rather than guessed per row: a label can
    only know it needs a disambiguator by seeing the sibling that makes it
    ambiguous. Nesting has usually separated them already (``Reader.run`` and
    ``Writer.run``); this is the backstop for the cases it cannot.
    """

    seen: set[tuple[str, str]] = set()
    repeated: set[tuple[str, str]] = set()
    for unit in units:
        if unit.kind != "symbol" or not unit.symbol:
            continue
        key = (unit.path, unit.symbol)
        if key in seen:
            repeated.add(key)
        seen.add(key)
    return frozenset(repeated)


def _unit_payload(unit: ReviewUnit, *, ambiguous: bool = False) -> dict[str, Any]:
    return {
        "unit_key": unit.unit_key,
        "label": unit_label(unit, ambiguous=ambiguous),
        "kind": unit.kind,
        "path": unit.path,
        "symbol": unit.symbol,
        "ordinal": unit.ordinal,
        "start_line": unit.start_line,
        "end_line": unit.end_line,
        "attention_rank": unit.attention_rank,
        "attention_group": unit.attention_group,
        "reasons": list(unit.reasons),
        "fingerprint_method": unit.fingerprint_method,
    }


def _files_by_path(packet: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if packet is None:
        return {}
    out: dict[str, Mapping[str, Any]] = {}
    for item in packet.get("files") or ():
        if isinstance(item, dict):
            out[str(item.get("path") or "")] = item
    return out


def build_groups(
    units: Sequence[ReviewUnit],
    entries: Sequence[FrontierEntry],
    packet: Mapping[str, Any] | None,
    *,
    targets: Sequence[ReviewTarget] = (),
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Legacy file rows, rolled up from the reader's target truth.

    File units still own ranking/category metadata, but human judgments are
    recorded on the non-overlapping ReviewTarget denominator. Deriving the row
    state from those targets keeps this compatibility projection from disagreeing
    with progress and completion.
    """

    by_key = {unit.unit_key: unit for unit in units}
    files = _files_by_path(packet)
    targets_by_path: dict[str, list[ReviewTarget]] = {}
    first_outstanding: dict[str, ReviewTarget] = {}
    position_by_path: dict[str, int] = {}
    for target in sorted(targets, key=lambda item: item.priority.position):
        targets_by_path.setdefault(target.path, []).append(target)
        position_by_path.setdefault(target.path, target.priority.position)
        if target.priority.outstanding:
            first_outstanding.setdefault(target.path, target)

    def _rolled_up(entry: FrontierEntry) -> FrontierEntry:
        path_targets = targets_by_path.get(entry.path, ())
        if not path_targets:
            return entry
        if any(target.state == "changed_since_review" or target.changed_since_mark for target in path_targets):
            return replace(entry, state="changed_since_review", changed_since_mark=True)
        if any(target.state == "needs_changes" for target in path_targets):
            return replace(entry, state="needs_changes", changed_since_mark=False)
        if all(target.state == "reviewed" for target in path_targets):
            return replace(entry, state="reviewed", changed_since_mark=False)
        if any(target.state == "unknown" for target in path_targets):
            return replace(entry, state="unknown", changed_since_mark=False)
        return replace(entry, state="unreviewed", changed_since_mark=False)

    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in GROUP_KEYS}
    for entry in entries:
        if entry.kind != "file":
            continue
        effective = _rolled_up(entry)
        unit = by_key.get(entry.unit_key)
        category = unit.attention_group if unit is not None else "production"
        # One policy: a file with targets sits where its first outstanding
        # queue entry puts it (spec §4.6); only a target-less file falls back.
        first = first_outstanding.get(entry.path)
        if first is not None:
            group = legacy_group_for_tier(first.priority.tier)
        elif entry.path in targets_by_path:
            group = "reviewed"
        else:
            group = group_for(effective, category)
        item = files.get(entry.path)
        file_status = str(item.get("status") or "modified") if item is not None else "modified"
        buckets[group].append(
            {
                "unit_key": entry.unit_key,
                "path": entry.path,
                "group": group,
                "state": effective.state,
                "changed_since_mark": effective.changed_since_mark,
                "attention_rank": entry.attention_rank,
                "category": category,
                "reasons": _reason_lines(entry, group),
                "file_status": file_status,
                "status_column": status_column(effective.state, effective.changed_since_mark, file_status),
                "additions": int(item.get("additions") or 0) if item is not None else 0,
                "deletions": int(item.get("deletions") or 0) if item is not None else 0,
                "is_binary": bool(item.get("is_binary")) if item is not None else False,
                "language": (item.get("language") if item is not None else None) or "",
                "renderable": item is not None
                and (
                    _media_preview_payload(item) is not None
                    or (not item.get("is_binary") and bool(item.get("hunks") or item.get("submodule_pointer")))
                ),
            }
        )

    def _order(row: dict[str, Any]) -> tuple[int, int, int, str]:
        rank = int(row["attention_rank"])
        return (position_by_path.get(str(row["path"]), _NO_POSITION), 1 if rank == 0 else 0, rank, str(row["path"]))

    groups: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for key in GROUP_KEYS:
        rows = sorted(buckets[key], key=_order)
        counts[key] = len(rows)
        groups.append({"key": key, "label": GROUP_LABELS[key], "rows": rows})
    return groups, counts


def _impact_for(packet: Mapping[str, Any] | None, path: str) -> list[dict[str, Any]]:
    """Impact sites caused by *path*, plus sites that live in *path*.

    ``source_path`` names the changed file whose edit caused a site; an
    out-of-patch site lives, by definition, in a file this diff does not
    contain, so without it the pane for the file that caused it would be empty.
    """

    if packet is None:
        return []
    out: list[dict[str, Any]] = []
    for site in packet.get("impact") or ():
        if not isinstance(site, dict):
            continue
        source = str(site.get("source_path") or "")
        located = str(site.get("path") or "")
        if source != path and not located.startswith(f"{path}:L") and located != path:
            continue
        out.append(
            {
                "kind": str(site.get("kind") or ""),
                "path": located,
                "old": str(site.get("old") or ""),
                "new": site.get("new"),
                "snippet": str(site.get("snippet") or ""),
                "in_patch": bool(site.get("in_patch")),
                "inspected_by_agent": site.get("inspected_by_agent"),
                "source_path": source,
                "uncertainty": str(site.get("uncertainty") or ""),
            }
        )
    return out


def _symbols_for(packet: Mapping[str, Any] | None, path: str) -> list[dict[str, Any]]:
    if packet is None:
        return []
    out: list[dict[str, Any]] = []
    for symbol in packet.get("symbols") or ():
        if not isinstance(symbol, dict) or str(symbol.get("file_path") or "") != path:
            continue
        out.append(
            {
                "symbol_name": str(symbol.get("symbol_name") or ""),
                "qualified_name": symbol.get("qualified_name") or "",
                "kind": symbol.get("kind") or "",
                "change": str(symbol.get("change") or "unknown"),
                "start_line": int(symbol.get("start_line") or 0),
                "end_line": int(symbol.get("end_line") or 0),
                "caller_count": int(symbol.get("caller_count", -1)),
                "centrality_rank": symbol.get("centrality_rank"),
                "source": str(symbol.get("source") or "unknown"),
            }
        )
    return out


def _provenance_for(packet: Mapping[str, Any] | None, path: str) -> dict[str, Any]:
    """Provenance, scoped to one file, with the honest-gap distinction intact.

    ``inspected`` is a tri-state on purpose. ``None`` means this host records no
    reads at all, which must render as "reads not recorded" and never as "the
    agent skipped it" -- a false accusation of negligence is worse than silence
    (spec R-13).
    """

    record: Mapping[str, Any] = {}
    if packet is not None and isinstance(packet.get("provenance"), dict):
        record = packet["provenance"]
    inspected_list = [str(item) for item in (record.get("files_inspected") or ())]
    reads_recorded = bool(inspected_list)
    inspected: bool | None = (path in inspected_list) if reads_recorded else None
    return {
        "status": str(record.get("status") or "unknown"),
        "host": record.get("host") or "",
        "model": record.get("model") or "",
        "session_id": record.get("session_id") or "",
        "task": record.get("task") or "",
        "certainty": str(record.get("certainty") or "none"),
        "match_confidence": float(record.get("match_confidence") or 0.0),
        "match_reason": str(record.get("match_reason") or ""),
        "commands_run": [str(item) for item in (record.get("commands_run") or ())],
        "subagents": [list(item) for item in (record.get("subagents") or ())],
        "reads_recorded": reads_recorded,
        "inspected": inspected,
        "uninspected_impacted": [str(item) for item in (record.get("uninspected_impacted") or ())],
    }


def _annotation_relevance_key(annotation: Annotation) -> tuple[Any, ...]:
    """Presentation order that keeps human judgment primary and machine noise bounded.

    Human comments preserve chronological order. Non-human context is ranked by
    trust/review utility only; this never changes ReviewTarget state or turns an
    AI confidence number into a reviewer verdict.
    """

    if annotation.source == "human":
        return (0, annotation.created_at, annotation.id)
    source_order = {"ai_review": 0, "author": 1, "lemoncrow": 2}.get(annotation.source, 3)
    inactive = 1 if annotation.state in {"orphaned", "obsolete", "resolved"} else 0
    exact = 0 if annotation.anchor_method in EXACT_ANCHOR_METHODS else 1
    confidence = annotation.confidence if annotation.confidence is not None else -1.0
    return (
        1,
        inactive,
        source_order,
        -confidence,
        exact,
        -len(annotation.evidence),
        annotation.created_at,
        annotation.id,
    )


def _ordered_annotations(annotations: Sequence[Annotation]) -> tuple[Annotation, ...]:
    return tuple(sorted(annotations, key=_annotation_relevance_key))


def _annotation_payload(annotation: Annotation) -> dict[str, Any]:
    """One comment, in the shape the centre pane draws a marker from.

    ``anchor_method`` and ``anchor_detail`` ride along on every row because a
    marker that cannot say *why* it is where it is has asked the reader to trust
    a number. An orphan carries its last known line for display and the sentence
    that explains why that line no longer means anything.

    Its last known *symbol* is carried the same way and under a different name:
    ``symbol`` is where the comment sits now and is empty unless a rung located
    it, and ``origin_symbol`` is where it was written. A card that printed "not
    relocated" beside "in ``gone_forever``" would deny in one line what it
    asserted in the next.
    """

    anchor = annotation.anchor
    symbol, origin_symbol = anchor_symbol_claim(annotation.anchor_method, anchor.symbol_qualified_name)
    return {
        "id": annotation.id,
        "review_id": annotation.review_id,
        "revision_id": annotation.revision_id,
        "parent_id": annotation.parent_id,
        "kind": annotation.kind,
        "state": annotation.state,
        "body": annotation.body,
        "created_by": annotation.created_by,
        "created_by_actor": annotation.created_by_actor,
        "source": annotation.source,
        "source_id": annotation.source_id,
        "agent_role": annotation.agent_role,
        "agent_host": annotation.agent_host,
        "agent_model": annotation.agent_model,
        "parent_session_id": annotation.parent_session_id,
        "source_message_id": annotation.source_message_id,
        "comment_scope": annotation.comment_scope,
        "title": annotation.title,
        "evidence": list(annotation.evidence),
        "rule_refs": list(annotation.rule_refs),
        "confidence": annotation.confidence,
        "finding_disposition": (
            "superseded_stale"
            if annotation.source == "ai_review"
            and annotation.state in {"orphaned", "obsolete"}
            and annotation.finding_disposition in {"", "open", "confirmed_by_reviewer"}
            else annotation.finding_disposition
        ),
        "finding_disposition_reason": annotation.finding_disposition_reason,
        "finding_disposition_revision_id": annotation.finding_disposition_revision_id,
        "finding_disposition_reviewer_id": annotation.finding_disposition_reviewer_id,
        "finding_disposition_evidence": list(annotation.finding_disposition_evidence),
        "author_response": annotation.author_response,
        "author_response_source_id": annotation.author_response_source_id,
        "author_response_at": annotation.author_response_at,
        "is_human_judgment": annotation.source == "human",
        "created_at": annotation.created_at,
        "updated_at": annotation.updated_at,
        "anchor_method": annotation.anchor_method,
        "anchor_method_label": ANCHOR_METHOD_LABELS.get(annotation.anchor_method, annotation.anchor_method),
        "anchor_exact": annotation.anchor_method in EXACT_ANCHOR_METHODS,
        "anchor_detail": annotation.anchor_detail,
        "anchored": annotation.state not in ("orphaned", "obsolete")
        and annotation.anchor_method not in ("orphaned", "removed", "unresolved"),
        "path": anchor.path,
        "side": anchor.side,
        "start_line": anchor.start_line,
        "end_line": anchor.end_line,
        "file_level": anchor.start_line == 0,
        "unit_key": anchor.unit_key,
        "symbol": symbol,
        "origin_symbol": origin_symbol,
    }


def _activity_payload(event: Any) -> dict[str, Any]:
    try:
        detail = json.loads(event.detail_json) if event.detail_json else {}
    except (TypeError, ValueError):
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    return {
        "id": event.id,
        "review_id": event.review_id,
        "revision_id": event.revision_id,
        "kind": event.kind,
        "actor_id": event.actor_id,
        "actor_type": event.actor_type,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "summary": event.summary,
        "detail": detail,
        "created_at": event.created_at,
    }


def _mark_event_payload(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "review_id": event.review_id,
        "reviewer_id": event.reviewer_id,
        "unit_key": event.unit_key,
        "revision_id": event.revision_id,
        "reviewed_revision_id": event.reviewed_revision_id,
        "event_kind": event.event_kind,
        "from_state": event.from_state,
        "to_state": event.to_state,
        "content_fingerprint": event.content_fingerprint,
        "previous_unit_key": event.previous_unit_key,
        "actor_type": event.actor_type,
        "note": event.note,
        "reason": event.reason,
        "created_at": event.created_at,
    }


def _annotation_version_payload(version: Any) -> dict[str, Any]:
    return {
        "id": version.id,
        "annotation_id": version.annotation_id,
        "review_id": version.review_id,
        "revision_id": version.revision_id,
        "version_number": version.version_number,
        "body": version.body,
        "kind": version.kind,
        "state": version.state,
        "author_response": version.author_response,
        "author_response_source_id": version.author_response_source_id,
        "author_response_at": version.author_response_at,
        "resolved_revision_id": version.resolved_revision_id,
        "changed_by": version.changed_by,
        "changed_by_actor": version.changed_by_actor,
        "change_kind": version.change_kind,
        "created_at": version.created_at,
    }


def _int_field(source: Mapping[str, Any], names: Sequence[str]) -> int:
    """The first of *names* present in *source*, as an int; ``0`` when none is.

    Two spellings are accepted because two callers exist: our own JSON says
    ``start_line``, and ``@pierre/diffs``' ``SelectedLineRange`` says ``start``.
    Translating the viewer's vocabulary at the door is cheaper than teaching the
    rest of the system two words for one thing.
    """

    for name in names:
        value = source.get(name)
        if isinstance(value, bool):  # `True` is an int in Python and a bug here
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
    return 0


def _safe_media_type(value: str) -> str:
    """A header-safe media type, or the binary default for legacy bad rows."""

    text = value.strip()
    if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        return "application/octet-stream"
    if "/" not in text or len(text) > 255:
        return "application/octet-stream"
    return text


#: Uploaded or repository bytes never script, navigate or fetch, even opened top-level.
UNTRUSTED_CONTENT_CSP = (
    "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_INLINE_SAFE_MEDIA_TYPES = frozenset({"text/plain", "application/json", "application/pdf"})


def _untrusted_content_response(content: bytes, media_type: str) -> Response:
    """Serve user- or repository-controlled bytes so they can never run as this origin.

    Only inert types (raster image, audio, video, plain text, JSON, PDF) are
    inline. SVG keeps its type so ``<img>`` still renders it, but as an
    attachment. Every other type (HTML, XML, script, CSS, ...) becomes an
    octet-stream attachment, so it can be neither a document nor, under
    ``nosniff``, a same-origin script or stylesheet.
    """

    media = _safe_media_type(media_type)
    essence = media.split(";", 1)[0].strip().lower()
    major = essence.split("/", 1)[0]
    inline = essence in _INLINE_SAFE_MEDIA_TYPES or (
        major in {"image", "audio", "video"} and "svg" not in essence and "xml" not in essence
    )
    if not inline and essence != "image/svg+xml":
        media = "application/octet-stream"
    return Response(
        content=content,
        media_type=media,
        headers={
            "Content-Security-Policy": UNTRUSTED_CONTENT_CSP,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Disposition": "inline" if inline else "attachment",
        },
    )


async def _json_object(request: Request) -> dict[str, Any]:
    """The request body as a JSON object, or a 400 that says which half failed."""

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="body must be a JSON object")
    return body


def _review_evidence_payload(
    evidence: ReviewEvidence,
    *,
    current: bool,
    revision_number: int | None = None,
) -> dict[str, Any]:
    """Browser projection. Staleness follows reviewed code identity, not row identity."""

    return {
        "id": evidence.id,
        "review_id": evidence.review_id,
        "revision_id": evidence.revision_id,
        "revision_number": revision_number,
        "kind": evidence.kind,
        "title": evidence.title,
        "path": evidence.path,
        "url": evidence.url,
        "content_hash": evidence.content_hash,
        "mime_type": evidence.mime_type,
        "bytes": evidence.bytes,
        "source": evidence.source,
        "source_ref": evidence.source_ref,
        "verification_status": evidence.verification_status,
        "detail": evidence.detail,
        "created_at": evidence.created_at,
        "attestation_class": evidence.attestation_class,
        "stable_key": evidence.stable_key,
        "provider": evidence.provider,
        "integration_id": evidence.integration_id,
        "provider_revision": evidence.provider_revision,
        "branch": evidence.branch,
        "environment": evidence.environment,
        "attestation_scope": evidence.attestation_scope,
        "attested_by": evidence.attested_by,
        "remote_identity": evidence.remote_identity,
        "observed_at": evidence.observed_at,
        "waived_requirement_key": evidence.waived_requirement_key,
        "execution_intent": evidence.execution_intent,
        "executor_class": evidence.executor_class,
        "status": "current" if current and evidence.execution_intent != "explicit_historical" else "stale",
        "content_url": f"{API_PREFIX}/evidence/{evidence.id}/content" if evidence.artifact_path else "",
    }


def _feedback_context(packet: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The *Review context* bullets of plan SS10.1, drawn from the packet.

    Delegated to the renderer that owns the bundle so the browser's export and
    the terminal's ``--feedback`` cannot drift into two different ideas of what
    context is. Nothing here turns silence into a pass.
    """

    from lemoncrow.pro.capabilities.review.feedback import packet_context

    return packet_context(packet)


def _evidence(
    packet: Mapping[str, Any] | None,
    artifacts: Sequence[Any] = (),
    *,
    path: str = "",
    revision_id: str = "",
) -> list[dict[str, Any]]:
    """Current verification claims, with newer revision-bound proof winning.

    Packet evidence records what was known when the revision snapshot was built.
    A reviewer or CI run may add stronger proof later without changing the code.
    Those immutable ReviewEvidence rows may override a packet row with the same
    name only when they belong to the current revision and carry an explicit
    verification status. A screenshot with no status is never a PASS.

    ``scope`` is review-wide for packet evidence and artifacts without a path;
    file-scoped evidence is only surfaced for the selected file. The UI can
    therefore use broad checks as context without implying they specifically
    prove the file under review.
    """

    rows: dict[str, dict[str, Any]] = {}
    if packet is not None:
        for item in packet.get("evidence") or ():
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            rows[name] = {
                "name": name,
                "status": str(item.get("status") or "UNKNOWN"),
                "detail": str(item.get("detail") or ""),
                "source": str(item.get("source") or "none"),
                "scope": "review",
            }
    artifact_rows: dict[str, dict[str, Any]] = {}
    for item in artifacts:
        if revision_id and str(getattr(item, "revision_id", "") or "") != revision_id:
            # Verification is stricter than visual evidence carry-forward: a
            # check that ran against another immutable Snapshot stays stale even
            # when the reviewed source bytes happen to be equivalent.
            continue
        item_path = str(getattr(item, "path", "") or "")
        if path and item_path and item_path != path:
            continue
        status_name = str(getattr(item, "verification_status", "") or "").upper()
        if status_name not in {"PASS", "FAIL", "NOT_RUN", "UNKNOWN"}:
            continue
        name = str(getattr(item, "title", "") or "Verification")
        if name in artifact_rows:
            continue
        artifact_rows[name] = {
            "name": name,
            "status": status_name,
            "detail": str(getattr(item, "detail", "") or ""),
            "source": str(getattr(item, "source", "") or "evidence"),
            "scope": "file" if item_path else "review",
        }
        # Where an executed result was verified and what that environment could not verify.
        execution = verification_row(str(getattr(item, "execution_verification", "") or ""))
        if execution is not None:
            artifact_rows[name]["execution"] = execution
    rows.update(artifact_rows)
    return list(rows.values())


# An Ask snapshot is a whole new-side checkout plus the index built in it.
_ASK_SNAPSHOTS_PER_REVIEW = 2
_ASK_SNAPSHOT_REVIEWS = 4
_ASK_SNAPSHOT_IDLE_S = 3600.0


def _prune_ask_snapshots(snapshot_cache: Path) -> None:
    """Closer for an Ask/intelligence snapshot cache: a few per Review and a few Reviews, once idle."""

    from lemoncrow.pro.capabilities.review.scratch_cache import HEAVY_TTL_SECONDS, prune_cache_entries

    review_cache = snapshot_cache.parent
    prune_cache_entries(
        review_cache,
        snapshot_cache,
        keep_count=_ASK_SNAPSHOTS_PER_REVIEW,
        idle_seconds=_ASK_SNAPSHOT_IDLE_S,
        ttl_seconds=HEAVY_TTL_SECONDS,
    )
    prune_cache_entries(
        review_cache.parent,
        review_cache,
        keep_count=_ASK_SNAPSHOT_REVIEWS,
        idle_seconds=_ASK_SNAPSHOT_IDLE_S,
        ttl_seconds=HEAVY_TTL_SECONDS,
    )


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #

#: (captured journal rows, provenance, changed paths) decoded from one stored packet.
_JournalPacketFacts = tuple[tuple[dict[str, Any], ...], Any, tuple[str, ...] | None]


def register_review_api(
    app: FastAPI,
    store: ReviewStore,
    *,
    auth_dependency: Callable[..., Any],
    repo_root: Path,
    port: int,
    session_repo_identity: str | None = None,
    enforce_origin_guard: bool = True,
    surface_workspace_root: Path | None = None,
    dependency_repo_root: Path | None = None,
    source_repo_root: Path | None = None,
    source_store_root: Path | None = None,
    source_refresh_enabled: bool = True,
    live_journal_enabled: bool = True,
    runner_registry: Any | None = None,
    runtime_validation_submitter: Callable[..., Mapping[str, Any]] | None = None,
    runtime_validation_job_lookup: Callable[[ReviewSession, ReviewRevision, Any, str], Mapping[str, Any]] | None = None,
    runtime_validation_job_cancel: Callable[[ReviewSession, ReviewRevision, Any, str], Mapping[str, Any]] | None = None,
    ai_review_submitter: Callable[..., Mapping[str, Any]] | None = None,
    ai_review_job_lookup: Callable[[ReviewSession, ReviewRevision], Mapping[str, Any] | None] | None = None,
    ai_review_job_cancel: Callable[[ReviewSession, ReviewRevision, str], Mapping[str, Any]] | None = None,
    background_budget: Any | None = None,
    acting_reviewer_id: Callable[[], str] | None = None,
    github_publish_enabled: bool = False,
    ask_answerer: Any | None = None,
    ask_enabled: bool = True,
    agent_cli_drivers: bool = True,
    code_intelligence: (
        CodeIntelligenceProvider | Callable[[ReviewSession, ReviewRevision], CodeIntelligenceProvider | None] | None
    ) = None,
    readiness_extension: Callable[[Any, Any], Mapping[str, Any]] | None = None,
    readiness_conditions: Sequence[Any] = (),
    readiness_gatherers: Mapping[str, Callable[[Any], object]] | None = None,
    policy_change_hook: Callable[[Any], None] | None = None,
    intelligence_policy: Callable[[ReviewSession, ReviewRevision], Mapping[str, Any]] | None = None,
    landing_delegate: Callable[..., Mapping[str, Any]] | None = None,
    short_references: bool = True,
    run_plan_usage: Callable[[ReviewSession, ReviewRevision], Mapping[str, Any] | None] | None = None,
    static_analysis_host: str = "local",
    interactive_preview: Any | None = None,
    automation_rules: Callable[[ReviewSession], Any] | None = None,
    automation_permission_check: Callable[[Any, str], bool] | None = None,
    branch_protection: Callable[[ReviewSession, Mapping[str, Any]], Any] | None = None,
    provider_merge_fact: Callable[[ReviewSession], Any] | None = None,
    post_merge_executors: Mapping[str, Callable[[Any], Any]] | None = None,
    feedback_sync_provider: Callable[[ReviewSession], Any] | None = None,
    hard_delete_allowed: Callable[[ReviewSession], bool] | None = None,
    deletion_audit: Callable[[Mapping[str, Any]], None] | None = None,
    automation_repository: Callable[[ReviewSession], str] | None = None,
    deletion_context: Callable[[ReviewSession], Any] | None = None,
    review_manager: Callable[[ReviewSession], bool] | None = None,
    premerge_executor: Callable[[Callable[[], None]], None] | None = None,
    premerge_submitter: (
        Callable[[ReviewSession, ReviewRevision, Mapping[str, Any], Mapping[str, Any], Any], Mapping[str, Any]] | None
    ) = None,
    reviewer_suggestions: Callable[[ReviewSession, ReviewRevision], Mapping[str, Any]] | None = None,
    record_signing: RecordSigningAuthority | None = None,
    approval_authority: Callable[[], Any] | None = None,
    approval_separate_requester: bool = True,
    deployment_outcomes: Callable[[ReviewSession], Mapping[str, Any]] | None = None,
    change_set_directory: ChangeSetDirectory | None = None,
    outcome_settings: Callable[[ReviewSession], tuple[Any, Any]] | None = None,
    landing_authority: Any | None = None,
    project_knowledge: Callable[[ReviewSession], ProjectKnowledgeBinding | None] | None = None,
    discussion_summaries: DiscussionSummaryHost | None = None,
    review_workflow: (
        Callable[[ReviewSession, datetime, SnapshotProgress | None], Mapping[str, Any] | None] | None
    ) = None,
    trust_observer: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> None:
    """Add the review routes and browser-facing guards to *app*.

    ``review_workflow`` (Trust T2) returns this Review's Inbox ``workflow`` and
    ``next_action`` for the requesting viewer at an instant, from the
    composition's own Inbox derivation. The trust section on Review Status
    (``readiness.trust_mode``) copies them, so it never computes a state or a
    next owner. Without it the section says the workflow is unavailable. The
    third argument is the target progress the same Status read derived
    (``readiness.SnapshotProgress``: the overview's ``progress`` and its
    Snapshot), or ``None`` when it has none: a composition whose Inbox row
    counts targets uses it for that Snapshot instead of deriving them again.

    ``trust_observer`` (Trust plan §39.8 item 10) receives one content-free
    observation (``trust_telemetry``) per trust section built and per run plan
    read, for the composition's operator metrics. It is never read back, and
    one that raises changes no answer.
    ``project_knowledge`` (PK2) binds a Review to the PK0 Project Knowledge
    store and repository identity it may read landed history from. Without it
    (the default) Brief, Ask, AI Review and the Map behave exactly as before.

    ``discussion_summaries`` (DS0) offers source-linked thread summaries
    (``discussion_summary``): explicit on request, and, with its scheduler, a
    background refresh when a summarized thread gains or edits a message.
    Without it (the default, and hosted) the summary routes say unavailable.

    ``landing_authority`` (``landing_api.LandingAuthority``, P10-LAND) is the
    connected SCM's provider truth for auto-approval (L1/L3/L4), approval
    delivery and withdrawal, the merge-readiness commit status, the merge
    result and control-plane signing of landing decisions. Without it
    eligibility is still explained but nothing is delivered to a provider.

    ``change_set_directory`` resolves the members of a multi-repository change
    set (Phase 9R item 42) as the current viewer may read them; without it a
    Review is never in a set. The composition passes one bound to this
    repository (local: every local repository; hosted: the viewer's tenant).

    ``deployment_outcomes`` projects a merged Review's deployments for Review
    Status (P15-DEPLOY, ``outcomes.deployments_api``); default: the local
    operator's ``review-deployments.json`` (no file: not configured).

    ``outcome_settings`` returns a Review's ``(DeploymentSettings,
    ObservabilitySettings)`` (P15-OBS: production observation, incident links,
    Revert / Fix forward); default: the local operator's
    ``review-deployments.json`` and ``review-observability.json``.

    ``short_references`` names Changes and Snapshots by their shortest unique
    prefix. Hosted turns it off: its URLs and refs always carry the full id,
    so one Review never has two public names.

    ``repo_root`` is the filesystem/runtime root used for local source context
    and previews. ``session_repo_identity`` is the durable ReviewSession
    identity expected in this store; it defaults to the resolved repository
    path for the historical per-repo workspace, while the central server passes
    its tenant/repository identity instead.

    ``enforce_origin_guard`` stays on for a directly-bound workspace. The main
    LemonCrow server disables the inner guard because the Reader app is invoked
    in-process behind that server's listener, authentication and authorization.

    ``source_repo_root`` is the trusted mutable checkout used only for source
    change detection and explicit Review refresh. Central/hosted compositions
    must disable ``source_refresh_enabled`` unless they have such a binding.
    ``github_publish_enabled`` lets a Review opened from a GitHub pull request
    publish through the ``gh`` login of the machine running this server, so only
    the local composition (the reviewer's own machine) turns it on.
    ``source_store_root`` supplies the normal LemonCrow trace/session store so a
    UI-triggered refresh preserves the provenance/evidence semantics of the CLI
    capture path instead of rebuilding against the Review database itself.

    ``ask_enabled`` is an explicit Review capability. A product composition may
    disable it, but Review Ask itself is Review-owned and does not require the
    LemonCrow Runtime product.

    ``agent_cli_drivers`` lets this host start a local agent CLI (claude, codex,
    opencode) for Review Intelligence, and lets Ask list them and read the
    OpenCode model catalog. Only a reviewer's own machine turns it on; hosted
    turns it off so no client, and no configured default, ever starts an agent
    CLI on the shared control plane. Turning it off forbids agent-CLI routes for
    the whole process (``intelligence_runtime.forbid_agent_cli_drivers``), the
    one authority every surface's intelligence call resolves through: brief,
    tour and behavior synthesis report AI as unavailable, and Ask and
    architecture explanation translate the refusal into a 400.

    ``code_intelligence`` is the narrow current-repository intelligence seam.
    It may be one provider or a per-Snapshot factory; the latter receives both
    the Review and exact revision so native current context can stay bound to
    the View/worktree that supplied that Snapshot. All returned evidence stays
    explicitly non-Snapshot-exact.

    ``readiness_extension`` lets an outer composition contribute organization
    policy conditions to the same readiness result; it may also return
    ``policy`` layers (``{layer, source, document}``) that join the policy
    generation and a stricter ``freshness`` max-age. ``readiness_conditions``
    registers additional :class:`readiness.RegisteredCondition` entries with
    the one readiness core; ``readiness_gatherers`` supplies the fact of a
    core condition the composition owns (hosted: ``org_policy``), so its
    evaluation stays the core's. ``policy_change_hook`` receives a typed
    :class:`readiness.PolicyChangeNotice` once per policy generation change
    that leaves a Change's decisions taken under an older generation
    (attention routing belongs to the composition). ``landing_delegate`` is the
    provider-owned final handoff; it receives ``action=arm|cancel`` and must not
    perform Git merging/rebasing inside LemonCrow.

    ``acting_reviewer_id`` resolves the authenticated reviewer for the current
    request. Multi-principal compositions (hosted) must pass it: every Reader
    judgment, outcome, comment and activity is then read and written as that
    principal instead of the session's stored single-reviewer identity. It
    must fail closed (raise) when no principal is bound to the request.

    ``static_analysis_host`` labels the process holding the Snapshot where the
    static side-effect inventory runs (``local``; the hosted server passes
    ``server``) on its metered compute.

    P12-AUTO seams (``review_automation_api``): ``automation_rules`` returns
    the effective organization/operator automation rules for a Review (default:
    the local operator file); ``automation_permission_check`` is the human-route
    permission check an automated run gets (default: the local owner);
    ``branch_protection`` / ``provider_merge_fact`` supply provider truth;
    ``feedback_sync_provider`` changes published feedback on the provider;
    ``hard_delete_allowed`` is the separate delete privilege and
    ``deletion_audit`` the audit sink a hard delete writes first (default: an
    append-only file under the Review store); ``automation_repository`` names
    the repository a rule's scope matches (default: the session's repo root;
    hosted: the tenant repository id); ``deletion_context`` names who is
    deleting, where and under which request id for the store's durable
    deletion tombstone (default: the local owner).

    ``review_manager`` says whether the acting principal may change what a
    Review is judged against (binding/re-binding intent); hosted passes its
    ``REVIEW_MANAGE`` check. Without it, the durable Review owner may (and
    anyone when no acting principal is configured). ``premerge_executor``
    runs scheduled pre-merge evaluations (default: a small in-process pool).
    ``premerge_submitter`` (hosted) enqueues one durable model-evaluation job
    per (Snapshot, check, input digest) on the deployment's Review
    Intelligence locus, called as ``(session, revision, request, policy,
    governance)``; with it, no pre-merge model call ever runs in this process.

    ``record_signing`` decides who signs a signed review record (Phase 10
    item 19); hosted passes its control-plane organization-key authority.
    Without it, records sign only with the reviewer's opted-in personal key
    and otherwise export explicitly UNSIGNED.

    ``approval_authority`` returns the calling principal as an
    ``action_approval_api.ApprovalPrincipal`` (P12-AGENT): only a ``human`` one
    may approve or deny a risky agent action. Without it every decision is
    refused. ``approval_separate_requester`` (default on) also refuses a decision
    by the principal whose agent filed the request; a local server that already
    tells the human's browser from the agent's process turns it off.
    """

    from fastapi import APIRouter

    from lemoncrow.core.foundation.paths import confine_to_root
    from lemoncrow.pro.capabilities.review.change_proposals import (
        replace_line_range,
        selected_line_text,
        text_sha256,
        unified_proposal_patch,
    )
    from lemoncrow.pro.capabilities.review.feedback import build_bundle, render_markdown
    from lemoncrow.pro.capabilities.review.revisions import compare_revision_units, compute_frontier, group_frontier
    from lemoncrow.pro.capabilities.review.session_models import (
        ANNOTATION_STATES,
        REVIEW_OUTCOME_KINDS,
        ReviewActivityEvent,
    )
    from lemoncrow.pro.capabilities.review.sources.local import (
        annotate,
        annotation_counts,
        baseline_revision,
        coerce_annotation_kind,
        coerce_mark_state,
        mark_downgrade_note,
        mark_unit,
        read_packet_json,
        resolve_mark_units,
        revision_new_side_text,
        set_review_status,
        unseen_units,
    )
    from lemoncrow.pro.capabilities.review.store import utc_now

    if not agent_cli_drivers:
        # The host setting goes to the runtime, the one authority every surface passes through.
        from lemoncrow.pro.capabilities.review.intelligence_runtime import forbid_agent_cli_drivers

        forbid_agent_cli_drivers()
    resolved_repo = repo_root.expanduser().resolve()
    resolved_dependency_repo = dependency_repo_root.expanduser().resolve() if dependency_repo_root is not None else None
    resolved_preview_repo = resolved_dependency_repo or resolved_repo
    resolved_source_repo = source_repo_root.expanduser().resolve() if source_repo_root is not None else resolved_repo
    resolved_source_store = source_store_root.expanduser().resolve() if source_store_root is not None else store.root
    # Whole Snapshot-side source trees read by surfaces and dependency review. A
    # cache: content-keyed so Snapshots sharing a side share one tree, and closed
    # by idle time (see snapshot.cached_review_side).
    snapshot_sources_root = (
        surface_workspace_root / "snapshot-sources"
        if surface_workspace_root is not None
        else store.root / "review" / "snapshot-sources"
    )
    legacy_snapshot_source_roots = (
        (surface_workspace_root,)
        if surface_workspace_root is not None
        else (store.root / "review" / "surface-context", store.root / "review" / "dependency-context")
    )

    def _snapshot_side_tree(revision: ReviewRevision, side: str, repo: Path) -> Path:
        from lemoncrow.pro.capabilities.review.snapshot import cached_review_side, retire_legacy_snapshot_sources

        retire_legacy_snapshot_sources(legacy_snapshot_source_roots)
        return cached_review_side(snapshot_sources_root, store.root, repo, revision, side=side, store=store)

    from lemoncrow.pro.capabilities.review.development import DevelopmentStore, ledger_ingestor

    # Change-level Development: an append-only stream on the local store, fed
    # incrementally from this machine's agent ledgers when live context is on
    # and this build reads them (``None`` reports live ingestion as off).
    development_store = DevelopmentStore(store)
    development_ingestor: Any | None = None
    if live_journal_enabled:
        development_ingestor = ledger_ingestor(
            store,
            source_store_root=resolved_source_store,
            repo_root=resolved_source_repo,
        )
    from lemoncrow.pro.capabilities.review.session_comments import ingestor_for as comment_ingestor_for

    comment_ingestor = comment_ingestor_for(store, resolved_source_store)
    expected_session_repo = session_repo_identity if session_repo_identity is not None else str(resolved_repo)
    effective_landing_delegate = landing_delegate
    from lemoncrow.pro.capabilities.review.readiness import DEFAULT_CONDITIONS

    # The one readiness core; compositions only register conditions with it.
    own_readiness_registry = DEFAULT_CONDITIONS.with_conditions(*readiness_conditions).with_gatherers(
        readiness_gatherers or {}
    )
    readiness_registry = own_readiness_registry
    if change_set_directory is not None:
        from lemoncrow.pro.capabilities.review.change_sets import readiness_condition as _change_set_condition

        # A change-set member also carries the set's blockers (its own readiness is the registry above).
        readiness_registry = own_readiness_registry.with_conditions(
            _change_set_condition(lambda sources: _change_set_fact(sources.session))
        )
    # Immutable Snapshot comparisons can be expensive (rendering two artifacts
    # or starting two isolated services) but are stable for the life of the
    # server. Keep a bounded in-process cache so reopening/reordering the Reader
    # does not execute reviewed code merely to rebuild surface ranking.
    surface_impact_cache: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    ask_snapshot_locks: dict[str, asyncio.Lock] = {}
    ai_review_auto_locks: dict[str, asyncio.Lock] = {}

    def _code_intelligence_for(
        session: ReviewSession,
        revision: ReviewRevision,
    ) -> CodeIntelligenceProvider | None:
        candidate = code_intelligence
        if candidate is None:
            return None
        if callable(candidate):
            return candidate(session, revision)
        return candidate

    if effective_landing_delegate is None and github_publish_enabled:
        from lemoncrow.pro.capabilities.review.landing import local_github_landing

        effective_landing_delegate = local_github_landing

    def _proposal_target_ids(proposal: ReviewChangeProposal, targets: Sequence[ReviewTarget]) -> list[str]:
        matched: list[str] = []
        for target in targets:
            if target.path != proposal.path:
                continue
            same_unit = bool(proposal.target_unit_key and target.unit_key == proposal.target_unit_key)
            overlaps = target.start_line <= proposal.end_line and target.end_line >= proposal.start_line
            if same_unit or overlaps:
                matched.append(target.target_id)
        return matched

    def _proposal_payload(
        proposal: ReviewChangeProposal,
        *,
        current_targets: Sequence[ReviewTarget] = (),
    ) -> dict[str, Any]:
        base_revision = store.get_revision(proposal.base_revision_id)
        current_revision = store.latest_revision(proposal.review_id)
        result_targets = (
            _proposal_target_ids(proposal, current_targets) if proposal.result_revision_id and current_targets else []
        )
        return {
            "id": proposal.id,
            "review_id": proposal.review_id,
            "base_revision_id": proposal.base_revision_id,
            "base_revision_number": _display_revision_number(base_revision) if base_revision is not None else None,
            "path": proposal.path,
            "start_line": proposal.start_line,
            "end_line": proposal.end_line,
            "original_text": proposal.original_text,
            "replacement_text": proposal.replacement_text,
            "patch": proposal.patch_text,
            "state": proposal.state,
            "target_unit_key": proposal.target_unit_key,
            "annotation_id": proposal.annotation_id,
            "intent": proposal.intent,
            "conflict_reason": proposal.conflict_reason,
            "created_by": proposal.created_by,
            "created_at": proposal.created_at,
            "updated_at": proposal.updated_at,
            "applied_at": proposal.applied_at,
            "result_revision_id": proposal.result_revision_id,
            "result_target_ids": result_targets,
            "can_apply": (
                source_refresh_enabled
                and proposal.state == "proposed"
                and current_revision is not None
                and current_revision.id == proposal.base_revision_id
            ),
        }

    def _atomic_write_source(path: Path, text: str, *, expected_sha256: str = "") -> None:
        mode = path.stat().st_mode
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.lc-proposal-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, mode)
            if expected_sha256:
                try:
                    current_text = path.read_bytes().decode("utf-8")
                except (OSError, UnicodeError) as exc:
                    raise ValueError("source changed while the proposal write was being committed") from exc
                if text_sha256(current_text) != expected_sha256:
                    raise ValueError("source changed while the proposal write was being committed")
            os.replace(temp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise

    def _feedback_annotation_version(annotation_id: str) -> int:
        """Latest reviewer-owned semantic version of one root feedback thread.

        Author ``addressed`` claims are append-only annotation versions too, but
        they must not make already-published human text look new again. Delivery
        therefore binds to the latest non-author-response version.
        """

        from lemoncrow.pro.capabilities.review.handoff import feedback_version

        return feedback_version(store.list_annotation_versions(annotation_id))

    def _feedback_partition(
        revision_id: str,
        annotations: Sequence[Annotation],
        *,
        source_ref: str = "",
        track_pull_request: bool = False,
    ) -> tuple[dict[str, Any], frozenset[str], dict[str, int]]:
        """Classify current root human feedback for one review revision.

        A comment version is publishable until that exact version is either in
        flight or sent for this revision. Reviewer edits create a newer version
        and become publishable again. An author ``addressed`` claim belongs to
        re-review instead of another automatic send.

        A Review opened from a GitHub pull request (``source_ref`` is
        ``github:...``) also reports ``github``: which comment versions are
        already on the PR, so the Reader can say so instead of offering an agent
        handoff the PR author will never see.
        """

        from lemoncrow.pro.capabilities.review.handoff import feedback_state, is_open_root_feedback
        from lemoncrow.pro.capabilities.review.sources.github import GITHUB_SOURCE_PREFIX

        track_github = source_ref.startswith(GITHUB_SOURCE_PREFIX) or track_pull_request
        github = {"unpublished": 0, "published": 0, "in_flight": 0}
        root_human_ids = tuple(annotation.id for annotation in annotations if is_open_root_feedback(annotation))
        store.recover_stale_delivery_attempts(revision_id, root_human_ids)
        unpublished: set[str] = set()
        versions: dict[str, int] = {}
        published = 0
        in_flight = 0
        addressed = 0
        open_total = 0
        for annotation in annotations:
            if not is_open_root_feedback(annotation):
                continue
            open_total += 1
            version = _feedback_annotation_version(annotation.id)
            versions[annotation.id] = version
            deliveries = store.list_deliveries(annotation.id)
            if track_github:
                on_github = tuple(
                    item
                    for item in deliveries
                    if item.target_type == "github_review" and item.annotation_version == version
                )
                if any(item.state == "sent" for item in on_github):
                    github["published"] += 1
                elif any(
                    item.state in {"dispatching", "queued", "uncertain"}
                    or (item.state in {"blocked", "failed"} and bool(item.remote_ref))
                    for item in on_github
                ):
                    github["in_flight"] += 1
                else:
                    github["unpublished"] += 1
            state, _matching = feedback_state(
                annotation, revision_id=revision_id, version=version, deliveries=deliveries
            )
            if state == "addressed":
                addressed += 1
            elif state == "published":
                published += 1
            elif state == "in_flight":
                in_flight += 1
            else:
                unpublished.add(annotation.id)
        status_payload: dict[str, Any] = {
            "open_total": open_total,
            "unpublished": len(unpublished),
            "published": published,
            "in_flight": in_flight,
            "addressed": addressed,
        }
        if track_github:
            status_payload["github"] = github
        return status_payload, frozenset(unpublished), versions

    if enforce_origin_guard:

        @app.middleware("http")
        async def _guard(request: Request, call_next: Any) -> Any:
            refusal = origin_refusal(
                host=request.headers.get("host", ""),
                origin=request.headers.get("origin", ""),
                referer=request.headers.get("referer", ""),
                port=port,
            )
            if refusal:
                # A refusal is a Response, not an exception: middleware sits outside
                # FastAPI's exception handlers, so raising here would surface as a
                # 500 and hide the reason from both the caller and the test.
                response: Any = JSONResponse({"detail": refusal}, status_code=status.HTTP_403_FORBIDDEN)
            else:
                response = await call_next(request)
            for name, value in SECURITY_HEADERS.items():
                if name == "Content-Security-Policy" and response.headers.get(name, "").startswith("sandbox;"):
                    # Untrusted plugin artifacts carry a strictly narrower
                    # sandbox policy; widening it to the app policy would let
                    # a top-level-opened artifact run same-origin script.
                    continue
                response.headers[name] = value
            return response

    @app.get(HEALTHZ_PATH)
    async def _healthz() -> dict[str, Any]:
        """Liveness only, and deliberately unauthenticated.

        It says nothing about the repository: no path, no session, no counts.
        The workspace's own health probe still authenticates a second call --
        liveness is not proof the token in a registration file still works.
        """

        return {"ok": True, "service": "lemoncrow-review-workspace"}

    async def _artifact_reclaimed(_request: Request, _exc: Exception) -> JSONResponse:
        # A hosted writer stalled past the orphan GC grace (ReviewArtifactReclaimed):
        # nothing committed, and the same request writes its artifacts again.
        return JSONResponse(
            {
                "detail": "an artifact this write produced was reclaimed before its row committed; retry the request",
                "code": "review_artifact_reclaimed",
                "retryable": True,
            },
            status_code=status.HTTP_409_CONFLICT,
            headers={"Retry-After": "1"},
        )

    app.add_exception_handler(ReviewArtifactReclaimed, _artifact_reclaimed)

    router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(auth_dependency)])

    def _require_ask_enabled() -> None:
        if not ask_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Review Ask is not available in this deployment",
            )

    def _session(review_id: str) -> ReviewSession:
        try:
            session = store.resolve_session(review_id)
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if session is None or session.repo_root != expected_session_repo:
            # A session for another repository is *not found* here rather than
            # forbidden: this Reader surface is repository-scoped, and saying
            # "exists elsewhere" would leak a Review in another repository.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such review")
        return _as_acting_reviewer(session)

    def _as_acting_reviewer(session: ReviewSession) -> ReviewSession:
        # The in-memory view only: `update_session` writes named fields, so the
        # durable session owner is never rewritten by this projection.
        if acting_reviewer_id is None:
            return session
        return replace(session, reviewer_id=acting_reviewer_id())

    def _mutable_session(review_id: str) -> ReviewSession:
        """Return a session only while human review state may still be changed.

        ``archived`` is the internal disposable state. It remains readable so a
        human can inspect what they discarded or explicitly restore it, but no
        mark, comment, evidence, feedback delivery or refresh may race retention
        cleanup against a review the UI still looks able to edit.
        """

        session = _session(review_id)
        if session.status == "archived":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="review is discarded and read-only; restore it before making changes",
            )
        return session

    def _refuse_hosted_owned(session: ReviewSession, action: str) -> None:
        """While attached, hosted owns comments, judgments, requests, outcomes and policy (G5-SYNC).

        The local write is refused, never applied locally and never forwarded.
        The Reader's queue-safe writes (one mark, one comment or reply, one
        outcome) queue through ``_queue_reader_write`` before reaching this
        guard (RC5); every write that still reaches it is online-only.
        """

        if not store.hosted_attachment_supported():
            return
        attachment = store.get_hosted_attachment(session.id)
        if attachment is None:
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"hosted_owned: this Review is attached to hosted {hosted_reference(attachment.hosted_review_id)}, "
                f"which owns {action}; this change is online-only: act on the hosted Review, or detach"
            ),
        )

    def _latest(session: ReviewSession) -> ReviewRevision:
        revision = store.latest_revision(session.id)
        if revision is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="this review has no revision yet")
        return revision

    def _project_knowledge_binding(session: ReviewSession) -> ProjectKnowledgeBinding | None:
        if project_knowledge is None:
            return None
        try:
            return project_knowledge(session)
        except Exception as exc:  # derived index: Review stays useful without it
            logging.getLogger(__name__).warning("Project Knowledge binding failed: %s", type(exc).__name__)
            return None

    def _knowledge_review_id(kind: str, value: str) -> str:
        """Resolve a knowledge ``review:``/``revision:`` ref to a Review this surface can open.

        Exact full-id lookup only (never a short-token prefix scan or a hosted
        attachment alias), and only a Review of this surface's repository, so a
        link never points at (or reveals) a Review the reader cannot open here.
        """

        with contextlib.suppress(Exception):
            review_id = value
            if kind == "revision":
                found_revision = store.get_revision(value)
                review_id = found_revision.review_id if found_revision is not None else ""
            elif kind != "review":
                return ""
            found = store.get_session(review_id) if review_id else None
            if found is not None and found.id == review_id and found.repo_root == expected_session_repo:
                return found.id
        return ""

    def _project_context(
        session: ReviewSession,
        revision: ReviewRevision,
        request: ProjectContextRequest,
        *,
        content_access: Sequence[Any] = (),
        route_trust: ModelRouteTrust = "unknown",
    ) -> ProjectContext:
        """Bounded, read-only PK2 retrieval; never enqueues work and never raises."""

        from dataclasses import replace as _replace

        from lemoncrow.pro.capabilities.review.project_context import retrieve_project_context, unavailable

        binding = _project_knowledge_binding(session)
        if binding is None:
            return unavailable(request.purpose, "not_configured")
        changed = tuple(sorted(_revision_packet_paths(revision)))
        return retrieve_project_context(
            binding,
            _replace(
                request,
                changed_paths=changed,
                # Brief and Ask default to the Change's own paths; a Map selection names its own keys.
                paths=request.paths or (changed if request.purpose in {"brief", "ask"} else ()),
                own_refs=(f"review:{session.id}", f"revision:{revision.id}"),
                base_revision=session.review_base_sha or revision.merge_base_sha or revision.base_sha,
            ),
            content_access=content_access,
            route_trust=route_trust,
            resolve_review=_knowledge_review_id,
        )

    def _ask_project_knowledge(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        question: str,
        paths: Sequence[str],
        symbol: str,
        surface_context: Any,
        content_access: Sequence[Any],
        route_trust: ModelRouteTrust,
        restricted: bool = False,
    ) -> ProjectContext | None:
        """Ask's history: the asked-about paths, symbol, Map selection and question terms.

        *restricted* (a changed path is AI-denied for this route) drops path-less
        history, the same fail-safe rule Ask applies to unscoped evidence.
        """

        if project_knowledge is None:
            return None
        from lemoncrow.pro.capabilities.review.project_context import ProjectContextRequest, question_terms

        selected_paths: list[str] = [value for value in paths if value]
        selected = surface_context.get("selected") if isinstance(surface_context, Mapping) else None
        if isinstance(selected, Mapping):
            selected_paths.extend(str(value) for value in [selected.get("path"), *list(selected.get("paths") or [])])
        return _project_context(
            session,
            revision,
            ProjectContextRequest(
                purpose="ask",
                paths=tuple(dict.fromkeys(value for value in selected_paths if value)),
                symbols=(symbol,) if symbol else (),
                terms=question_terms(question),
                restricted=restricted,
            ),
            content_access=content_access,
            route_trust=route_trust,
        )

    def _ai_review_knowledge_loader(
        session: ReviewSession, revision: ReviewRevision, content_access: Sequence[Any]
    ) -> Callable[[ModelRouteTrust, Sequence[Any]], Mapping[str, Any]] | None:
        """AI Review's advisory history over the candidate targets, filtered for the actual route.

        When any changed path is AI-denied for the route (AI Review's policy
        exclusion), path-less history is dropped, as in restricted Ask.
        """

        if project_knowledge is None:
            return None

        def load(route_trust: ModelRouteTrust, targets: Sequence[Any]) -> Mapping[str, Any]:
            from lemoncrow.pro.capabilities.review.project_context import ProjectContextRequest
            from lemoncrow.pro.capabilities.review.rules import content_access_for_path

            restricted = any(
                not content_access_for_path(path, content_access, route_trust=route_trust).allowed
                for path in _revision_packet_paths(revision)
            )
            context = _project_context(
                session,
                revision,
                ProjectContextRequest(
                    purpose="ai_review",
                    paths=tuple(dict.fromkeys(str(target.path) for target in targets if target.path)),
                    symbols=tuple(dict.fromkeys(str(target.symbol) for target in targets if target.symbol)),
                    restricted=restricted,
                ),
                content_access=content_access,
                route_trust=route_trust,
            )
            return context.model_view()

        return load

    def _ask_knowledge_references(context: ProjectContext | None) -> list[dict[str, Any]]:
        """Ask references for history actually shown to the model: never Snapshot-exact."""

        if context is None:
            return []
        rows: list[dict[str, Any]] = []
        for row in context.entries:
            wire = row.to_wire()
            review_ids = [str(c.get("review_id")) for c in wire["citations"] if c.get("review_id")]
            rows.append(
                {
                    "kind": "project_knowledge",
                    "exactness": "historical" if row.context_class == "project_knowledge" else "ai_narration",
                    "context_class": row.context_class,
                    "entry_id": row.entry.entry_id,
                    "label": f"{row.entry.subject.key} · {row.entry.knowledge_kind}",
                    "state": row.entry.state,
                    "freshness_label": row.freshness_label,
                    "stale_for_snapshot": row.stale_for_snapshot,
                    **({"review_id": review_ids[0]} if review_ids else {}),
                }
            )
        return rows

    def _require_expected_revision(
        body: Mapping[str, Any], revision: ReviewRevision, *, action: str, require_saved: bool = True
    ) -> None:
        """*require_saved* off only for a judgment or comment queued for hosted (RC5): it binds to
        the hosted Snapshot through exact content (a comment through its exact new-side text), so
        the local revision on screen need not be saved. A queued verdict still needs one."""

        expected = str(body.get("expected_revision_id") or "").strip()
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{action} requires expected_revision_id from the revision currently displayed",
            )
        if expected != revision.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"the Review advanced before {action}; reload the current revision and try again",
            )

        if require_saved:
            _require_saved_snapshot(revision)

    def _require_saved_snapshot(revision: ReviewRevision) -> None:
        if revision.capture_kind == "draft" and revision.id not in store.published_snapshot_ids(revision.review_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Running drafts are read-only; create a Snapshot before commenting or recording review decisions",
            )

    def _requested_revision(session: ReviewSession, revision_id: str) -> ReviewRevision:
        """Resolve an explicitly requested stored revision, or the latest when omitted."""

        if not revision_id:
            return _latest(session)
        try:
            revision = store.resolve_revision(revision_id)
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if revision is None or revision.review_id != session.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such revision in this review")
        return revision

    def _revision(reference: str) -> tuple[ReviewSession, ReviewRevision]:
        try:
            revision = store.resolve_revision(reference)
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if revision is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such review revision")
        session = _session(revision.review_id)
        return session, revision

    def _revision_number(session: ReviewSession, revision_number: int) -> ReviewRevision:
        """Resolve a zero-based Review-local number to the immutable stored revision."""

        if revision_number < 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such review revision")
        storage_number = revision_number + 1
        revision = next(
            (item for item in store.list_revisions(session.id) if item.revision_number == storage_number),
            None,
        )
        if revision is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such review revision")
        return revision

    def _marks_visible_at(session: ReviewSession, revision: ReviewRevision) -> tuple[ReviewMark, ...]:
        """Historical mark projection from H2 events, with a legacy-store fallback."""

        if store.list_mark_events(session.id, reviewer_id=session.reviewer_id):
            return store.marks_at_revision(session.id, revision.id, reviewer_id=session.reviewer_id)
        # Stores created before H2 have no transition log to replay. Under-report
        # rather than projecting a later mutable verdict backwards.
        numbers = {item.id: item.revision_number for item in store.list_revisions(session.id)}
        return tuple(
            mark
            for mark in store.list_marks(session.id, reviewer_id=session.reviewer_id)
            if numbers.get(mark.reviewed_revision_id, revision.revision_number + 1) <= revision.revision_number
        )

    def _last_commented_revision(
        session: ReviewSession,
        *,
        before: ReviewRevision | None = None,
    ) -> ReviewRevision | None:
        """Newest Snapshot with actual human or AI review feedback."""

        revisions = {item.id: item for item in store.list_revisions(session.id)}
        candidates = [
            revisions[item.revision_id]
            for item in store.list_annotations(session.id)
            if item.source in {"human", "ai_review"}
            and item.revision_id in revisions
            and (before is None or revisions[item.revision_id].revision_number < before.revision_number)
        ]
        return max(candidates, key=lambda item: item.revision_number, default=None)

    def _reviewed_revision(session: ReviewSession) -> ReviewRevision | None:
        """Durable intentional human-review cursor; opening a revision never moves it."""

        return baseline_revision(store, session.id, reviewer_id=session.reviewer_id)

    def _default_compare_source(session: ReviewSession, target: ReviewRevision) -> ReviewRevision:
        """Feedback selects a viewing base independently of review marks.

        Feedback on the target itself takes effect only after a newer Snapshot,
        preserving the current comparison instead of replacing it with no diff.
        A self-view means the full original Review-base-to-target change.
        """

        return _last_commented_revision(session, before=target) or target

    def _revision_state_payload(session: ReviewSession) -> dict[str, Any]:
        revisions = store.list_revisions(session.id)
        if not revisions:
            return {
                "latest_revision_number": None,
                "last_reviewed_revision_number": None,
                "last_commented_revision_number": None,
                "default_base_revision_number": None,
                "default_target_revision_number": None,
            }
        latest = _latest(session)
        reviewed = _reviewed_revision(session)
        commented = _last_commented_revision(session)
        default_source = _default_compare_source(session, latest)
        return {
            "latest_revision_number": _display_revision_number(latest),
            "last_reviewed_revision_number": (_display_revision_number(reviewed) if reviewed is not None else None),
            "last_commented_revision_number": (_display_revision_number(commented) if commented is not None else None),
            "default_base_revision_number": _display_revision_number(default_source),
            "default_target_revision_number": _display_revision_number(latest),
        }

    def _annotation_context_payloads(
        session: ReviewSession,
        target: ReviewRevision,
        annotations: Sequence[Annotation],
    ) -> list[dict[str, Any]]:
        """Every comment's state at *target*: Snapshots and versions read once, not per comment."""

        if not annotations:
            return []
        by_id = {item.id: item for item in store.list_revisions(session.id)}
        versions = store.annotation_versions_for_review(session.id)
        payloads = [
            _annotation_context_payload(session, target, item, by_id=by_id, versions=versions.get(item.id, ()))
            for item in annotations
        ]
        return _with_owning_change(session, target, annotations, payloads)

    def _with_owning_change(
        session: ReviewSession,
        target: ReviewRevision,
        annotations: Sequence[Annotation],
        payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Item 24: feedback on code a lower stack layer introduced points at that Change."""

        latest = store.latest_revision(session.id)
        if latest is None or latest.id != target.id:
            return payloads
        from lemoncrow.pro.capabilities.review.stack_awareness import ownership_resolver

        resolver = ownership_resolver(store, session, target)
        if not resolver.active:
            return payloads
        for annotation, payload in zip(annotations, payloads, strict=True):
            anchor = annotation.anchor
            if annotation.comment_scope == "change" or annotation.state in {"orphaned", "obsolete"}:
                continue
            owner = resolver.resolve(path=anchor.path, side=str(anchor.side), start_line=int(anchor.start_line or 0))
            if owner is not None:
                payload["owning_change"] = owner.to_wire()
        return payloads

    def _annotation_context_payload(
        session: ReviewSession,
        target: ReviewRevision,
        annotation: Annotation,
        *,
        by_id: Mapping[str, ReviewRevision] | None = None,
        versions: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        """Comment state at *target* without erasing the immutable origin revision."""

        if by_id is None:
            by_id = {item.id: item for item in store.list_revisions(session.id)}
        origin = by_id.get(annotation.revision_id)
        resolved = by_id.get(annotation.resolved_revision_id) if annotation.resolved_revision_id else None

        addressed_revision: ReviewRevision | None = None
        for version in store.list_annotation_versions(annotation.id) if versions is None else versions:
            candidate = by_id.get(version.revision_id)
            if candidate is None or candidate.revision_number > target.revision_number:
                continue
            # Track actual author-response transitions, not every semantic
            # version that happens to carry the current author_response field.
            # A human resolve preserves "addressed" but must not claim the agent
            # addressed it on the resolve revision.
            if version.change_kind == "author_response":
                addressed_revision = candidate if version.author_response == "addressed" else None
            elif version.change_kind in {"edited", "thread_reply"} and version.author_response != "addressed":
                addressed_revision = None

        payload = _annotation_payload(annotation)
        payload.update(
            {
                "origin_revision_number": (_display_revision_number(origin) if origin is not None else None),
                "relation_to_revision": (
                    "inherited"
                    if annotation.comment_scope == "change" and origin is None
                    else (
                        "originating"
                        if origin is not None and origin.id == target.id
                        else (
                            "inherited"
                            if origin is not None and origin.revision_number < target.revision_number
                            else "future"
                        )
                    )
                ),
                "author_addressed_revision_number": (
                    _display_revision_number(addressed_revision) if addressed_revision is not None else None
                ),
                "addressed_by_revision": (addressed_revision is not None and addressed_revision.id == target.id),
                "resolved_revision_number": (_display_revision_number(resolved) if resolved is not None else None),
            }
        )
        return payload

    def _revision_packet_paths(revision: ReviewRevision) -> set[str]:
        packet = read_packet_json(store, revision) or {}
        rows = packet.get("files")
        paths: set[str] = set()
        if not isinstance(rows, list):
            return paths
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            path = str(raw.get("path") or "")
            old_path = str(raw.get("old_path") or "")
            if path:
                paths.add(path)
            if old_path:
                paths.add(old_path)
        return paths

    def _revision_compare_paths(source: ReviewRevision, target: ReviewRevision) -> set[str]:
        """Paths whose frozen new-side bytes differ between two Snapshots."""

        source_tree = store.read_source_tree_artifact(source.review_id, source.id, side="new")
        target_tree = store.read_source_tree_artifact(target.review_id, target.id, side="new")
        if source_tree is not None and target_tree is not None:
            return {
                path for path in set(source_tree) | set(target_tree) if source_tree.get(path) != target_tree.get(path)
            }

        source_blobs = store.read_blob_artifact(source.review_id, source.id)
        target_blobs = store.read_blob_artifact(target.review_id, target.id)
        return (
            set(source_blobs or {})
            | set(target_blobs or {})
            | _revision_packet_paths(source)
            | _revision_packet_paths(target)
        )

    def _persisted_revision_snapshot(
        revision: ReviewRevision,
        paths: set[str],
    ) -> dict[str, str | None]:
        """Read exact immutable revision bytes without consulting mutable source."""

        from lemoncrow.pro.capabilities.review.gitdiff import _blob_from_tree, _decode, _open_repo, _tree_for_sha

        blobs = store.read_blob_artifact(revision.review_id, revision.id)
        source_tree = store.read_source_tree_artifact(revision.review_id, revision.id, side="new")
        if blobs is None and source_tree is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the Review revision predates persisted source snapshots",
            )

        packet = read_packet_json(store, revision) or {}
        raw_rows = packet.get("files")
        rows = raw_rows if isinstance(raw_rows, list) else []
        deleted: set[str] = set()
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            status_name = str(raw.get("status") or "")
            path = str(raw.get("path") or "")
            old_path = str(raw.get("old_path") or "")
            if status_name == "deleted" and path:
                deleted.add(path)
            elif status_name == "renamed" and old_path:
                deleted.add(old_path)

        repo = None
        base_tree = None
        base_tree_loaded = False
        snapshot: dict[str, str | None] = {}
        for path in paths:
            if source_tree is not None:
                entry = source_tree.get(path)
                if entry is None:
                    snapshot[path] = None
                    continue
                digest = str(entry.get("content_digest") or "")
                payload = store.read_source_content(digest) if digest else None
                if payload is None or hashlib.sha256(payload).hexdigest() != digest:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"frozen source content is unavailable for {path}",
                    )
                snapshot[path] = _decode(payload)
                continue
            if path in deleted:
                snapshot[path] = None
                continue
            if blobs is not None and path in blobs:
                snapshot[path] = blobs[path]
                continue
            if not base_tree_loaded:
                base_tree_loaded = True
                if revision.base_sha:
                    try:
                        repo = _open_repo(resolved_source_repo)
                        base_tree = _tree_for_sha(repo, revision.base_sha)
                    except (OSError, ValueError):
                        repo = None
                        base_tree = None
            if base_tree is None or repo is None:
                snapshot[path] = None
                continue
            payload, _reason = _blob_from_tree(repo, base_tree, path)
            snapshot[path] = None if payload is None else _decode(payload)
        return snapshot

    def _revision_compare_payload(
        session: ReviewSession,
        *,
        from_revision_number: int | None,
        to_revision_number: int | None,
    ) -> dict[str, Any]:
        target = _latest(session) if to_revision_number is None else _revision_number(session, to_revision_number)
        source = (
            _default_compare_source(session, target)
            if from_revision_number is None
            else _revision_number(session, from_revision_number)
        )
        comparison = compare_revision_units(
            store.list_units(source.id),
            store.list_units(target.id),
            previous_marks=_marks_visible_at(session, source),
            next_marks=_marks_visible_at(session, target),
        )
        compare_paths = _revision_compare_paths(source, target)
        try:
            source_snapshot = _persisted_revision_snapshot(source, compare_paths)
            target_snapshot = _persisted_revision_snapshot(target, compare_paths)
            code_available = True
        except HTTPException as exc:
            if exc.status_code != status.HTTP_409_CONFLICT:
                raise
            source_snapshot = {}
            target_snapshot = {}
            code_available = False

        code_files: list[dict[str, Any]] = []
        for path in sorted(compare_paths) if code_available else ():
            before = source_snapshot.get(path)
            after = target_snapshot.get(path)
            if before == after:
                continue
            status_name = "added" if before is None else "removed" if after is None else "changed"
            before_text = before or ""
            after_text = after or ""
            patch = "".join(
                difflib.unified_diff(
                    before_text.splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
                    fromfile=f"rev-{_display_revision_number(source)}/{path}",
                    tofile=f"rev-{_display_revision_number(target)}/{path}",
                    n=3,
                )
            )
            added_lines = sum(1 for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
            removed_lines = sum(1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---"))
            code_files.append(
                {
                    "path": path,
                    "status": status_name,
                    "added_lines": added_lines,
                    "removed_lines": removed_lines,
                    "patch": patch,
                }
            )
        annotation_context = _annotation_context_payloads(
            session,
            target,
            _ordered_annotations(
                (
                    *store.list_annotations_at_revision(session.id, target.id),
                    *store.list_discussion_comments_at_revision(session.id, target.id),
                )
            ),
        )
        return {
            "review_id": session.id,
            "reviewer_id": session.reviewer_id,
            "from_revision": _revision_payload(source, store=store, short_refs=short_references),
            "to_revision": _revision_payload(target, store=store, short_refs=short_references),
            "suggested_from_revision_number": _display_revision_number(_default_compare_source(session, target)),
            "revision_state": _revision_state_payload(session),
            "annotations": annotation_context,
            "annotation_groups": {
                "originating": [
                    item["id"] for item in annotation_context if item["relation_to_revision"] == "originating"
                ],
                "inherited": [item["id"] for item in annotation_context if item["relation_to_revision"] == "inherited"],
                "addressed_by_target": [item["id"] for item in annotation_context if item["addressed_by_revision"]],
            },
            "summary": {
                "added": comparison.added,
                "changed": comparison.changed,
                "removed": comparison.removed,
                "unchanged": comparison.unchanged,
            },
            "files": [
                {
                    "path": item.path,
                    "status": item.status,
                    "added": item.added,
                    "changed": item.changed,
                    "removed": item.removed,
                    "reviewed_before": item.reviewed_before,
                    "reviewed_after": item.reviewed_after,
                    "changed_since_review_after": item.changed_since_review_after,
                    "needs_changes_after": item.needs_changes_after,
                }
                for item in comparison.files
            ],
            "code_available": code_available,
            "code_unavailable_reason": (
                ""
                if code_available
                else "one or both revisions predate persisted source snapshots; semantic target comparison remains available"
            ),
            "code_summary": {
                "files": len(code_files),
                "added_lines": sum(item["added_lines"] for item in code_files),
                "removed_lines": sum(item["removed_lines"] for item in code_files),
            },
            "code_files": code_files,
            "units": [
                {
                    "unit_key": item.unit_key,
                    "path": item.path,
                    "kind": item.kind,
                    "symbol": item.symbol,
                    "status": item.status,
                    "from_state": item.from_state,
                    "to_state": item.to_state,
                    "from_start_line": item.from_start_line,
                    "to_start_line": item.to_start_line,
                }
                for item in comparison.units
            ],
        }

    def _revision_compare_file_contents(
        session: ReviewSession,
        source: ReviewRevision,
        target: ReviewRevision,
        path: str,
    ) -> dict[str, Any]:
        """Exact file sides for expanding a Snapshot-to-Snapshot diff.

        A normal Review file-content request returns Review-base -> Snapshot
        sides. Reusing that while rendering Snapshot A -> Snapshot B splices
        incompatible context into the patch (especially for files added by the
        Review). This projection instead returns each Snapshot's immutable
        *new* side.
        """

        try:
            confine_to_root(resolved_repo / path, resolved_repo)
        except (ValueError, OSError):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no such file in this comparison"
            ) from None

        if path not in _revision_compare_paths(source, target):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file in this comparison")

        source_snapshot = _persisted_revision_snapshot(source, {path})
        target_snapshot = _persisted_revision_snapshot(target, {path})
        before = source_snapshot.get(path)
        after = target_snapshot.get(path)
        if before == after:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file in this comparison")

        return {
            "path": path,
            "revision_id": target.id,
            "from_revision_id": source.id,
            "to_revision_id": target.id,
            "old_path": path,
            "status": "added" if before is None else "deleted" if after is None else "changed",
            "old_content": before,
            "new_content": after,
        }

    def _annotations_at(
        session: ReviewSession, revision: ReviewRevision, *, historical: bool
    ) -> tuple[Annotation, ...]:
        rows = (
            store.list_annotations_at_revision(session.id, revision.id)
            if historical
            else store.list_annotations(session.id)
        )
        return _ordered_annotations(rows)

    def _evidence_at(
        session: ReviewSession, revision: ReviewRevision, *, historical: bool
    ) -> tuple[ReviewEvidence, ...]:
        artifacts = store.list_evidence(session.id)
        if not historical:
            return artifacts
        numbers = {item.id: item.revision_number for item in store.list_revisions(session.id)}
        return tuple(
            item
            for item in artifacts
            if numbers.get(item.revision_id, revision.revision_number + 1) <= revision.revision_number
        )

    def _same_reviewed_code(left: ReviewRevision, right: ReviewRevision) -> bool:
        if left.id == right.id:
            return True
        if left.source_fingerprint and right.source_fingerprint:
            return left.source_fingerprint == right.source_fingerprint
        # New file-only tree fingerprints are also code identity. This fallback
        # helps fresh stores while source fingerprints are unavailable on older
        # rows; never infer equality from packet/analysis metadata.
        return bool(left.tree_fingerprint and left.tree_fingerprint == right.tree_fingerprint)

    def _evidence_is_current(evidence: ReviewEvidence, revision: ReviewRevision) -> bool:
        # Verification and supply-chain observations describe an execution or
        # artifact produced for one exact Snapshot. They never carry across a
        # newer Snapshot merely because the source bytes compare equal. Visual
        # artifacts may still use semantic code identity (Phase 4 behavior).
        if evidence.verification_status or evidence.kind in {
            "verification",
            "sbom",
            "provenance",
            "vulnerability_scan",
        }:
            return evidence.revision_id == revision.id
        captured = store.get_revision(evidence.revision_id)
        return captured is not None and _same_reviewed_code(captured, revision)

    def _current_evidence(session: ReviewSession, revision: ReviewRevision) -> tuple[ReviewEvidence, ...]:
        # Target verification must be exact-Snapshot. Visual evidence has a
        # separate semantic-current projection for base-only Snapshot movement.
        return tuple(
            item
            for item in store.list_evidence(session.id)
            if item.revision_id == revision.id and item.execution_intent != "explicit_historical"
        )

    _browser_policy_cache: dict[str, Any] = {}
    _web_routes_cache: dict[str, tuple[str, ...] | None] = {}

    def _bounded_cache_put(cache: dict[str, Any], key: str, value: Any) -> None:
        # lc-debt: per-process FIFO of 128 immutable Snapshot projections; upgrade to a shared LRU if hosted fan-out needs it
        cache.pop(key, None)
        cache[key] = value
        while len(cache) > 128:
            cache.pop(next(iter(cache)))

    def _snapshot_document_text(
        revision: ReviewRevision,
        path: str,
        *,
        side: Literal["old", "new"],
    ) -> str | None:
        """Read one exact Snapshot document without falling across the trust boundary."""

        from lemoncrow.pro.capabilities.review.gitdiff import _blob_from_tree, _decode, _open_repo, _tree_for_sha

        tree = store.read_source_tree_artifact(revision.review_id, revision.id, side=side)
        if tree is not None:
            entry = tree.get(path)
            if entry is None:
                return ""
            payload = store.read_source_content(str(entry.get("content_digest") or ""))
            return payload.decode("utf-8", errors="replace") if payload is not None else None

        # Legacy/local working-tree Snapshots persist exact changed-file new-side
        # bytes in the blob artifact even when revision.head_sha still names the
        # base commit. Consult those immutable bytes before Git so a dirty policy
        # candidate is described as future policy rather than silently replaced by
        # the base commit's content.
        if side == "new":
            blobs = store.read_blob_artifact(revision.review_id, revision.id) or {}
            if path in blobs:
                return str(blobs[path])
            packet = read_packet_json(store, revision) or {}
            raw_files = packet.get("files", ()) if isinstance(packet, Mapping) else ()
            for item in raw_files:
                if not isinstance(item, Mapping) or str(item.get("path") or "") != path:
                    continue
                if str(item.get("status") or "") == "deleted":
                    return ""
                break

        sha = revision.base_sha if side == "old" else revision.head_sha
        if not sha:
            return None
        for root in (resolved_dependency_repo, resolved_source_repo, resolved_repo):
            if root is None:
                continue
            try:
                repo = _open_repo(root)
                git_tree = _tree_for_sha(repo, sha)
            except (KeyError, OSError, RuntimeError, ValueError):
                continue
            try:
                git_tree / path
            except KeyError:
                return ""
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            data, _reason = _blob_from_tree(repo, git_tree, path)
            if data is None:
                return None
            return _decode(data)
        return None

    def _snapshot_base_document(revision: ReviewRevision, path: str) -> TrustedBaseDocument:
        """Resolve one repository-owned authority document from the trusted base only."""

        from lemoncrow.pro.capabilities.review.trusted_base import (
            TrustedBaseDocument,
            resolve_trusted_base_document,
        )

        hosted_authority = getattr(store, "trusted_base_authority", None)
        if callable(hosted_authority):
            # Hosted: only a provider-proven trusted-base commit is authority
            # (a client-pushed capture's own base tree never is).
            document = hosted_authority(revision.review_id, revision.id, path)
            if isinstance(document, TrustedBaseDocument):
                return document
        return resolve_trusted_base_document(path, _snapshot_document_text(revision, path, side="old"))

    def _snapshot_review_config(revision: ReviewRevision) -> TrustedBaseDocument:
        """Current .lemoncrow/review.yaml authority; head bytes are never a fallback."""

        return _snapshot_base_document(revision, ".lemoncrow/review.yaml")

    def _counted_evidence(session: ReviewSession, revision: ReviewRevision) -> tuple[ReviewEvidence, ...]:
        """The Review's evidence, less signed rows whose key this Snapshot's trusted base no longer trusts."""

        from lemoncrow.pro.capabilities.review.evidence_adapters import withhold_untrusted_signed

        return withhold_untrusted_signed(store.list_evidence(session.id), lambda: _snapshot_review_config(revision))

    def _triage_policy(session: ReviewSession, revision: ReviewRevision) -> Any:
        """Review-first/criticality/threshold policy for one Snapshot (``criticality.triage_policy_for``)."""

        from lemoncrow.pro.capabilities.review.criticality import triage_policy_for

        # Local: the trusted base is this Snapshot's base commit. Hosted: only
        # the provider-proven trusted-base capture's commit (never the
        # client-pushed base) names where repository policy was read.
        commit = revision.base_sha
        if callable(getattr(store, "trusted_base_authority", None)):
            reader = getattr(store, "read_trusted_base", None)
            record = reader(revision.review_id, revision.id) if callable(reader) else None
            commit = str(getattr(record, "commit_sha", "") or "")
        return triage_policy_for(
            _effective_intelligence_policy(session, revision).get("effective_review"),
            _snapshot_review_config(revision),
            commit=commit,
        )

    def _queue_policy_reasons(
        session: ReviewSession, revision: ReviewRevision, targets: Sequence[Any]
    ) -> dict[str, list[Any]]:
        """Path policy plus unresolved current P0/P1 finding reasons per target id."""

        from lemoncrow.pro.capabilities.review.criticality import target_policy_reasons
        from lemoncrow.pro.capabilities.review.review_triage import current_findings, finding_queue_reasons

        policy = _triage_policy(session, revision)
        reasons = target_policy_reasons(targets, policy)
        findings, _stale = current_findings(
            store, revision_id=revision.id, annotations=store.list_annotations(session.id)
        )
        for target_id, rows in finding_queue_reasons(findings).items():
            bucket = reasons.setdefault(target_id, [])
            bucket.extend(row for row in rows if row not in bucket)
        return reasons

    _review_governance_cache: dict[str, Any] = {}

    def _review_governance(session: ReviewSession, revision: ReviewRevision) -> Any:
        """Resolve explicit Review rules/content access from authoritative layers."""

        from lemoncrow.pro.capabilities.review.rules import (
            parse_organization_governance,
            resolve_review_governance,
        )

        cached = _review_governance_cache.get(revision.id)
        if cached is not None and cached[1]:
            return cached[0]

        policy = _effective_intelligence_policy(session, revision)
        effective = policy.get("effective_review")
        organization = parse_organization_governance(effective if isinstance(effective, Mapping) else None)
        config = _snapshot_review_config(revision)
        governance = resolve_review_governance(
            config,
            read_base_document=lambda path: _snapshot_base_document(revision, path),
            organization=organization,
        )
        base_known = config.base_known and governance.base_state != "provisional"
        _bounded_cache_put(_review_governance_cache, revision.id, (governance, base_known))
        return governance

    def _future_policy_projection(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.rules import project_future_policy

        governance = _review_governance(session, revision)
        base_config = _snapshot_review_config(revision)
        head_config = _snapshot_document_text(revision, base_config.path, side="new")
        future = project_future_policy(
            base_config,
            head_config_content=head_config,
            read_base_document=lambda path: _snapshot_base_document(revision, path),
            read_head_document=lambda path: _snapshot_document_text(revision, path, side="new"),
        )
        from lemoncrow.pro.capabilities.review.rule_suggestions import repeated_human_feedback_suggestions

        suggestions = repeated_human_feedback_suggestions(
            store.list_all_annotations(),
            repository_scope=expected_session_repo,
            dismissed_ids=store.dismissed_rule_suggestion_ids(repository_scope=expected_session_repo),
        )
        return {
            "authority": {
                "base_state": governance.base_state,
                "rules": [rule.to_wire() for rule in governance.rules],
                "ai_content_access": [row.to_wire() for row in governance.content_access],
                "rule_documents": list(governance.rule_documents),
                "errors": list(governance.errors),
            },
            "future_changes": [row.to_wire() for row in future],
            "suggestions": [row.to_wire() for row in suggestions],
        }

    def _browser_policy(revision: ReviewRevision) -> Any:
        from lemoncrow.pro.capabilities.review.browser_checks import BrowserPolicy, parse_browser_policy

        cached = _browser_policy_cache.get(revision.id)
        # Only a base-known answer is final. Provisional defaults are safe to
        # return but are re-resolved on the next read so late attach/fetch can
        # immediately install the trusted base's policy.
        if cached is not None and cached[1]:
            return cached[0]
        authority = _snapshot_review_config(revision)
        policy = (
            parse_browser_policy(authority.content, source=authority.source) if authority.content else BrowserPolicy()
        )
        _bounded_cache_put(_browser_policy_cache, revision.id, (policy, authority.base_known))
        return policy

    def _hermetic_execution(session: ReviewSession, revision: ReviewRevision, side: str) -> Any:
        """Hermetic context for executing one side, from trusted-base/org policy only.

        Local runs are caller-attested (``author_machine``) and the head's
        source trust is unknown here, so a new-side run uses the untrusted
        cache namespace (fail closed).
        """

        from lemoncrow.pro.capabilities.review.hermetic import hermetic_context
        from lemoncrow.pro.capabilities.review.run_evidence import most_restrictive_access

        authority = _snapshot_review_config(revision)
        organization: Mapping[str, Any] | None = None
        try:
            effective = _effective_intelligence_policy(session, revision).get("effective_review")
            block = effective.get("execution") if isinstance(effective, Mapping) else None
            organization = block if isinstance(block, Mapping) else None
        except (OSError, RuntimeError, TypeError, ValueError):
            organization = None
        # Executed code can read any file of the side, so execution-derived
        # content takes the most restrictive AI content-access mode in force
        # anywhere in the repository (Phase 6). Unresolvable governance denies.
        try:
            access = most_restrictive_access(row.mode for row in _review_governance(session, revision).content_access)
        except Exception:
            access = "ai_denied"
        return hermetic_context(
            trusted_review_config=authority.content,
            store_root=store.root,
            review_id=revision.review_id,
            side=side,
            source_trust="unknown",
            organization=organization,
            content_access=cast(Any, access),
        )

    def _changed_paths(revision: ReviewRevision) -> tuple[str, ...]:
        packet = read_packet_json(store, revision)
        raw_files = packet.get("files", ()) if isinstance(packet, Mapping) else ()
        return tuple(
            str(item.get("path") or "")
            for item in raw_files
            if isinstance(item, Mapping) and str(item.get("path") or "")
        )

    def _changed_files(revision: ReviewRevision) -> tuple[tuple[str, str, str], ...]:
        """The whole Change as ``(path, old_path, status)`` (test-impact selection needs deletions and renames)."""

        packet = read_packet_json(store, revision)
        raw_files = packet.get("files", ()) if isinstance(packet, Mapping) else ()
        return tuple(
            (str(item.get("path") or ""), str(item.get("old_path") or ""), str(item.get("status") or "modified"))
            for item in raw_files
            if isinstance(item, Mapping) and str(item.get("path") or "")
        )

    def _base_tree_paths(revision: ReviewRevision) -> Any:
        """Trusted-base file paths for capability detection, or ``None`` when unreadable."""

        from lemoncrow.pro.capabilities.review.gitdiff import _open_repo, _tree_for_sha
        from lemoncrow.pro.capabilities.review.run_plan_service import git_tree_paths

        tree = store.read_source_tree_artifact(revision.review_id, revision.id, side="old")
        if tree is not None:
            return list(tree)
        if not revision.base_sha:
            return None
        for root in (resolved_dependency_repo, resolved_source_repo, resolved_repo):
            if root is None:
                continue
            try:
                repo = _open_repo(root)
                git_tree = _tree_for_sha(repo, revision.base_sha)
            except (KeyError, OSError, RuntimeError, ValueError):
                continue
            return git_tree_paths(repo, git_tree)
        return None

    def _head_tree_paths(revision: ReviewRevision) -> list[str] | None:
        """Head file paths: the immutable tree, else the base listing with the Change applied."""

        tree = store.read_source_tree_artifact(revision.review_id, revision.id, side="new")
        if tree is not None:
            return list(tree)
        base = _base_tree_paths(revision)
        if base is None:
            return None
        paths = set(base)
        for path, old_path, file_status in _changed_files(revision):
            if file_status == "deleted":
                paths.discard(path)
                continue
            if old_path and old_path != path:
                paths.discard(old_path)
            paths.add(path)
        return sorted(paths)

    def _member_snapshot(session: ReviewSession) -> Any:
        """This repository's Change as a change-set member (Phase 9R item 42), or ``None``."""

        from lemoncrow.pro.capabilities.review import change_sets

        revision = store.latest_revision(session.id)
        if change_set_directory is None or revision is None:
            return None

        def link(ref: Any) -> object:
            return store.upsert_relationship(change_sets.link_row(session.id, ref))

        def unlink(ref: Any) -> object:
            return change_sets.unlink_row(store, session.id, ref)

        return change_sets.MemberSnapshot(
            repository=change_set_directory.repository,
            identity=expected_session_repo,
            review_id=session.id,
            title=session.title,
            status=session.status,
            revision_id=revision.id,
            snapshot_number=_display_revision_number(revision),
            changed_files=lambda: _changed_files(revision),
            read_side=lambda path, side: _snapshot_document_text(revision, path, side=cast(Any, side)),
            head_paths=lambda: _head_tree_paths(revision),
            own_readiness=lambda: _readiness_projection(session, include_change_set=False),
            links=lambda: change_sets.member_links(store, session.id),
            link=link,
            unlink=unlink,
            revalidate_landing=lambda: _revalidate_armed_landing(session, origin="action"),
        )

    def _change_set_member(review_id: str) -> Any:
        """``app.state.review_change_set_member``: how another member's directory reads a Change here.

        Scoped like every Reader route: a Change of another repository is not
        found. The caller (the composition's directory) has already decided the
        viewer may read this repository.
        """

        try:
            session = store.resolve_session(review_id)
        except LookupError:
            return None
        if session is None or session.repo_root != expected_session_repo:
            return None
        return _member_snapshot(session)

    app.state.review_change_set_member = _change_set_member

    def _change_set_discovery(session: ReviewSession) -> Any:
        """The change set *session* is in, as this request's viewer may see it (``None``: not linked)."""

        from lemoncrow.pro.capabilities.review import change_sets

        if change_set_directory is None or not change_sets.member_links(store, session.id):
            return None
        start = _member_snapshot(session)
        if start is None:
            return None
        discovery = change_sets.discover(start, change_set_directory.member)
        # Only dangling rows (the other side unlinked): this Change is in no set.
        return discovery if discovery.linked else None

    def _change_set_fact(session: ReviewSession) -> Any:
        from lemoncrow.pro.capabilities.review import change_sets

        discovery = _change_set_discovery(session)
        return None if discovery is None else change_sets.assess(discovery, include_start=False)

    def _change_set_heads(session: ReviewSession) -> Any:
        from lemoncrow.pro.capabilities.review import change_sets

        return change_sets.set_heads(_change_set_discovery(session))

    def _change_set_summary(session: ReviewSession) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review import change_sets

        if change_set_directory is None:
            return {"available": False, "linked": False, "links": 0}
        here = change_sets.MemberRef(change_set_directory.repository, session.id)
        count = 0
        for ref in change_sets.member_links(store, session.id):
            member = change_set_directory.member(ref)
            # A dangling row (the other side does not link back) is no link; an
            # unreadable member may be one, so it counts (the set shows incomplete).
            if member is None or member.ref != ref or here in member.links():
                count += 1
        return {"available": True, "linked": bool(count), "links": count}

    def _plan_changed_paths(revision: ReviewRevision) -> tuple[str, ...]:
        """A Snapshot a correction action produced is planned on what that correction changed."""

        from lemoncrow.pro.capabilities.review.review_actions import correction_changed_paths

        corrected = correction_changed_paths(store, revision)
        return corrected if corrected is not None else _changed_paths(revision)

    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    # One effective run plan per Snapshot drives every executor seam below.
    run_plans = RunPlanService(
        store=store,
        policy=lambda session, revision: _effective_intelligence_policy(session, revision),
        review_config=_snapshot_review_config,
        read_base=lambda revision, path: _snapshot_document_text(revision, path, side="old"),
        read_head=lambda revision, path: _snapshot_document_text(revision, path, side="new"),
        base_paths=_base_tree_paths,
        changed_paths=_plan_changed_paths,
        # A correction re-run narrows optional work only; required work keeps
        # the whole Change, so the scope can never satisfy readiness.
        required_paths=_changed_paths,
        browser_policy=_browser_policy,
        usage=run_plan_usage,
        # Trusted base and operator policy only: a head-side check never schedules.
        premerge_checks=lambda session, revision: _premerge_nl_count(session, revision),
        changed_files=_changed_files,
        # P11-ESC: one ai.rule_checks question per authoritative rule (never head-side policy).
        review_rules=lambda session, revision: _review_governance(session, revision).rules,
    )

    from lemoncrow.pro.capabilities.review.side_effects import (
        ChangedPath,
        StaticSideEffectService,
        install_static_producer,
        snapshot_base_tree,
    )

    def _static_changed(revision: ReviewRevision) -> list[Any]:
        packet = read_packet_json(store, revision)
        raw_files = packet.get("files", ()) if isinstance(packet, Mapping) else ()
        return [
            ChangedPath(
                path=str(item.get("path") or ""),
                old_path=str(item.get("old_path") or ""),
                status=str(item.get("status") or "modified"),
            )
            for item in raw_files
            if isinstance(item, Mapping) and str(item.get("path") or "")
        ]

    # Phase 9R items 9/17/26/28/34/35/45: ``side_effects.static``, run under the plan.
    static_side_effects = StaticSideEffectService(
        store=store,
        plan=run_plans.peek,
        changed=_static_changed,
        read_text=lambda revision, path, side: _snapshot_document_text(
            revision, path, side="old" if side == "old" else "new"
        ),
        base_tree=lambda revision: snapshot_base_tree(
            store, revision, (resolved_dependency_repo, resolved_source_repo, resolved_repo)
        ),
        recorder=run_plans.compute.recorder,
        host=static_analysis_host,
    )
    install_static_producer()

    def _planned_request(
        session: ReviewSession,
        revision: ReviewRevision,
        pick: Any,
        *,
        run_on: str,
        action: str,
    ) -> dict[str, Any]:
        """Add a reviewer-requested execution to the effective run plan, then admit it.

        The request is a manual amendment: add-only, so policy-required work is
        never removed, and its cost delta is previewed and returned. It executes
        only when the amended plan runs the entry on this executor, so what runs
        is what the Review displays. A discarded Review is read-only: only
        already planned work may execute there.
        """

        from lemoncrow.pro.capabilities.review.planned_execution import (
            PlannedExecutionRefused,
            admit_planned_execution,
        )

        def candidates() -> int:
            return _ai_candidate_count(session, revision)

        incremental: Mapping[str, Any] = {"minutes": 0.0, "model_calls": 0}
        if session.status == "archived":
            plan = run_plans.current(session, revision, ai_candidates=candidates, persist=False)
        else:
            incremental = run_plans.preview(session, revision, [pick], ai_candidates=candidates)["incremental"]
            plan = run_plans.amend(session, revision, [pick], ai_candidates=candidates)
        try:
            admit_planned_execution(plan, pick.id, run_on=run_on)
        except PlannedExecutionRefused as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{action} of {pick.id} is not in the effective run plan: {exc}",
            ) from exc
        return {"digest": plan.digest, "entry": pick.id, "incremental": dict(incremental)}

    def _planned_surface_request(
        session: ReviewSession,
        revision: ReviewRevision,
        surface: Any,
        *,
        executable: bool,
        run_on: str,
        action: str,
    ) -> dict[str, Any]:
        """Plan a reviewer-requested surface execution (configured or discovered)."""

        from lemoncrow.pro.capabilities.review.planned_execution import surface_pick

        pick = surface_pick(surface, run_plans.detected(revision), executable=executable)
        return _planned_request(session, revision, pick, run_on=run_on, action=action)

    def _with_discovered_targets(session: ReviewSession, revision: ReviewRevision, picks: Sequence[Any]) -> list[Any]:
        """Attach this Snapshot's discovery to ``surface:<provider>:<id>`` picks the trusted base lacks.

        Lets ``Run specific…`` preview and confirm a discovered surface the
        same way a reviewer's run adds it. Unmatched picks stay as they are and
        are rejected by the planner.
        """

        from lemoncrow.pro.capabilities.review.planned_execution import surface_pick
        from lemoncrow.pro.capabilities.review.run_plan import ManualPick

        detected = run_plans.detected(revision)
        wanted = {
            pick.id
            for pick in picks
            if pick.id.startswith("surface:") and pick.target is None and detected.dynamic(pick.id) is None
        }
        if not wanted:
            return list(picks)
        try:
            surfaces = _discovered_side(session.id, revision.id, "new")[4]
        except (OSError, RuntimeError, TypeError, ValueError):
            return list(picks)
        found: dict[str, Any] = {}
        for surface in surfaces:
            executable = "execute" in surface.capabilities or getattr(surface, "preview", None) is not None
            candidate = surface_pick(surface, detected, executable=executable)
            if candidate.id in wanted and candidate.target is not None:
                found[candidate.id] = candidate.target
        return [ManualPick(pick.id, pick.depth, target=found[pick.id]) if pick.id in found else pick for pick in picks]

    def _planned_web_preview(session: ReviewSession, revision: ReviewRevision, *, action: str) -> dict[str, Any]:
        """Web previews build and render reviewed code here: ``visual.screenshot``."""

        from lemoncrow.pro.capabilities.review.capability_catalog import DynamicTarget
        from lemoncrow.pro.capabilities.review.planned_execution import WEB_PREVIEW_ENTRY
        from lemoncrow.pro.capabilities.review.run_plan import ManualPick

        # Callers resolved the route from this Snapshot's own web preview
        # discovery, which establishes that the entry applies here.
        pick = ManualPick(
            WEB_PREVIEW_ENTRY,
            target=DynamicTarget(
                id=WEB_PREVIEW_ENTRY, title="Web preview", reason="web app discovered on this Snapshot"
            ),
        )
        return _planned_request(session, revision, pick, run_on="local", action=action)

    def _meter_execution(
        revision: ReviewRevision,
        planned: Mapping[str, Any],
        *,
        run_on: str,
        execution: Any,
        wall_seconds: float,
        fallback_stage: str,
    ) -> None:
        """Record one in-process execution's compute against its planned entry."""

        from lemoncrow.pro.capabilities.review.planned_execution import record_execution_compute

        record_execution_compute(
            run_plans.compute.recorder(revision.review_id, revision.id),
            entry_id=str(planned["entry"]),
            run_on=run_on,
            execution=execution if isinstance(execution, Mapping) else None,
            wall_seconds=wall_seconds,
            fallback_stage=fallback_stage,
        )

    def _with_run_plan_schedule(
        session: ReviewSession, revision: ReviewRevision, projection: dict[str, Any]
    ) -> dict[str, Any]:
        """Mark optional awaited browser scenarios the effective run plan does not schedule.

        Required scenarios are policy floors and are never touched.
        """

        pending = [
            row
            for row in projection.get("checks", ())
            if isinstance(row, dict)
            and isinstance(row.get("browser"), Mapping)
            and row.get("source") == "policy"
            and row.get("state") == "current"
            and not row.get("required")
            and str(row["browser"].get("scenario") or "")
        ]
        if not pending:
            return projection
        try:
            plan = run_plans.peek(session, revision)
        except RuntimeError:
            return projection
        for row in pending:
            entry = plan.entry(f"browser:{row['browser']['scenario']}")
            scheduled = entry is not None and entry.status == "run"
            reason = (
                entry.reason
                if entry is not None
                else f"{plan.selection.get('name', 'this depth')} does not select browser scenarios"
            )
            row["run_plan"] = {
                "digest": plan.digest,
                "scheduled": scheduled,
                "status": entry.status if entry is not None else "not_selected",
                "reason": reason,
            }
            if not scheduled:
                row["detail"] = f"Not scheduled by the effective run plan: {reason}."
        return projection

    def _snapshot_web_routes(session: ReviewSession, revision: ReviewRevision) -> tuple[str, ...] | None:
        """Web routes this exact Snapshot exposes, or ``None`` when unknowable.

        Factual signal for browser-check applicability: the existing Web
        review-surface discovery, never a guess about "UI-looking" code.
        """

        if revision.id in _web_routes_cache:
            return _web_routes_cache[revision.id]
        from lemoncrow.pro.capabilities.review.web_preview import is_web_preview_path

        routes: tuple[str, ...] | None
        if not any(is_web_preview_path(path) for path in _changed_paths(revision)):
            routes = ()
        else:
            try:
                _revision_found, registry, _runners, context = _surface_runtime(session.id, revision.id)
                routes = tuple(
                    dict.fromkeys(
                        surface.locator
                        for surface in registry.discover(context)
                        if surface.provider == "web" and surface.locator
                    )
                )
            except (HTTPException, OSError, RuntimeError, TypeError, ValueError):
                routes = None
        _bounded_cache_put(_web_routes_cache, revision.id, routes)
        return routes

    def _verification_projection(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        discover_web_routes: bool = False,
        freshness: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.browser_checks import scenario_applicability
        from lemoncrow.pro.capabilities.review.verification import build_verification_projection

        if freshness is None:
            # Every caller (Verification panel, Finish, Status) sees the same
            # freshness readiness enforces: an expired PASS is never counted.
            freshness = _evidence_freshness(session, revision)
        policy = _browser_policy(revision)
        changed_paths = _changed_paths(revision)
        web_routes: tuple[str, ...] | None = None
        # Route discovery can materialize the Snapshot. Readiness skips it only
        # when changed paths (or `always`) already select every scenario: one
        # they do not select still applies through a Web route the Change
        # exposes, and the gate must decide it as the Verification panel does.
        # Discovery costs nothing when no changed path can reach a Web surface.
        needs_routes = discover_web_routes or any(
            scenario_applicability(scenario, changed_paths, None)[0] != "applies" for scenario in policy.scenarios
        )
        if needs_routes:
            web_routes = _snapshot_web_routes(session, revision)
        evidence = _counted_evidence(session, revision)
        return _with_run_plan_schedule(
            session,
            revision,
            _with_evidence_qualification(
                session,
                revision,
                evidence,
                build_verification_projection(
                    packet=read_packet_json(store, revision),
                    evidence=evidence,
                    revision=revision,
                    revisions=store.list_revisions(session.id),
                    browser_policy=policy,
                    changed_paths=changed_paths,
                    web_routes=web_routes,
                    read_artifact=store.read_evidence_artifact,
                    freshness=freshness,
                ),
            ),
        )

    def _evidence_qualification_context(session: ReviewSession, revision: ReviewRevision) -> Any:
        """Gate H for one Snapshot: versioned thresholds and mode, the trusted-base override, stored measurements."""

        from lemoncrow.pro.capabilities.review.evidence_qualification import build_context

        return build_context(
            store=store,
            repository=str(session.repo_root),
            review_config=_snapshot_review_config(revision).content or "",
        )

    def _with_evidence_qualification(
        session: ReviewSession,
        revision: ReviewRevision,
        evidence: Sequence[ReviewEvidence],
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        """Stamp Gate H qualification on LemonCrow-executed verification rows (readiness reads it)."""

        from lemoncrow.pro.capabilities.review.evidence_qualification import (
            annotate_verification,
            is_lemoncrow_row,
            read_json_artifact,
        )

        rows = projection.get("checks")
        if not isinstance(rows, list) or not any(isinstance(row, Mapping) and is_lemoncrow_row(row) for row in rows):
            return projection
        by_id = {item.id: item for item in evidence}
        catalog: list[Any] = []

        def read_record(evidence_id: str) -> Mapping[str, Any] | None:
            item = by_id.get(evidence_id)
            return read_json_artifact(store, item) if item is not None else None

        def report_format(recipe_id: str, digest: str) -> str | None:
            # Only the trusted catalog's recipe with this exact digest names its classifier.
            if not catalog:
                try:
                    catalog.append(run_plans.recipe_catalog(revision))
                except Exception:  # unknown classifier: the subject is unmeasured, never qualified
                    catalog.append(None)
            recipe = catalog[0].get(recipe_id) if catalog[0] is not None else None
            return str(recipe.report or "") if recipe is not None and recipe.digest == digest else None

        return annotate_verification(
            projection,
            _evidence_qualification_context(session, revision),
            read_record=read_record,
            report_format=report_format,
        )

    def _check_qualification(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        """Gate H rows of one Snapshot by stable key (current results only), for the Checks rail."""

        projection = _verification_projection(session, revision)
        rows = {
            str(row.get("stable_key") or ""): row["qualification"]
            for row in projection.get("checks", ()) or ()
            if row.get("state") == "current" and isinstance(row.get("qualification"), Mapping)
        }
        return {"summary": projection.get("qualification"), "rows": rows}

    def _behavior_qualification(
        session: ReviewSession,
        revision: ReviewRevision,
        targets: Sequence[Any],
        projection: Mapping[str, Any],
    ) -> Any:
        """Gate H over behavior targets from LemonCrow-executed producers (``None``: none to qualify)."""

        from lemoncrow.pro.capabilities.review.evidence_qualification import (
            EXECUTED_SURFACE_PRODUCERS,
            RECIPE_ENTRY_PREFIX,
            RECIPE_PRODUCER,
            QualificationSubject,
            partition_behavior,
            read_json_artifact,
            render_qualification_subject,
        )

        executed = {RECIPE_PRODUCER, *EXECUTED_SURFACE_PRODUCERS}
        if not any(str(target.delta.producer) in executed for target in targets):
            return None
        recipe_rows: dict[str, QualificationSubject] = {}
        for row in projection.get("checks", ()) or ():
            mark = row.get("qualification") if isinstance(row, Mapping) else None
            key = str(row.get("stable_key") or "") if isinstance(row, Mapping) else ""
            if isinstance(mark, Mapping) and key.startswith(RECIPE_ENTRY_PREFIX) and row.get("state") == "current":
                subject = mark.get("subject")
                if isinstance(subject, Mapping):
                    recipe_rows.setdefault(key, QualificationSubject.from_wire(subject))
        by_id = {item.id: item for item in store.list_evidence(session.id)}

        def render_subject(evidence_id: str) -> Any:
            item = by_id.get(evidence_id)
            return render_qualification_subject(read_json_artifact(store, item) if item is not None else None)

        return partition_behavior(
            targets,
            _evidence_qualification_context(session, revision),
            recipe_rows=recipe_rows,
            render_subject=render_subject,
        )

    def _evidence_freshness(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        """Freshness of a Snapshot's evidence now, under the effective freshness policy."""

        from lemoncrow.pro.capabilities.review.freshness import assess_freshness

        _policy, freshness_policy = _readiness_policy(revision, _readiness_extension_payload(session, revision))
        now = utc_now()
        return {
            item.id: assess_freshness(item, now=now, policy=freshness_policy)
            for item in _current_evidence(session, revision)
        }

    def _verification_gate(verification: Mapping[str, Any]) -> dict[str, list[Any]]:
        """The verification rows that gate landing (one rule with readiness)."""

        from lemoncrow.pro.capabilities.review.readiness import verification_gate

        return verification_gate(verification)

    def _dependency_projection(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.dependencies import build_dependency_review, supported_path
        from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable
        from lemoncrow.pro.capabilities.review.supply_chain import parse_evidence_metadata

        packet = read_packet_json(store, revision)
        raw_files = packet.get("files", ()) if isinstance(packet, Mapping) else ()
        changed_paths = tuple(
            str(item.get("path") or "")
            for item in raw_files
            if isinstance(item, Mapping) and str(item.get("path") or "")
        )
        supported_paths = tuple(path for path in changed_paths if supported_path(path) is not None)
        empty: dict[str, Any] = {
            "review_id": session.id,
            "snapshot_id": revision.id,
            "snapshot_number": _display_revision_number(revision),
            "supported": False,
            "lockfiles": [],
            "changes": [],
            "summary": {
                "semantic_changes": 0,
                "direct_changes": 0,
                "transitive_changes": 0,
                "added": 0,
                "removed": 0,
                "version_changed": 0,
                "source_changed": 0,
                "integrity_changed": 0,
            },
            "supply_chain": {"sboms": [], "provenance": [], "vulnerability_scans": []},
            "degraded": [],
        }
        if not supported_paths:
            return empty

        try:
            old_root = _snapshot_side_tree(revision, "old", resolved_repo)
            new_root = _snapshot_side_tree(revision, "new", resolved_repo)
        except ReviewSnapshotUnavailable as exc:
            empty["supported"] = True
            empty["degraded"] = [f"exact Snapshot dependency source unavailable: {exc}"]
            return empty

        exact_supply = tuple(
            item
            for item in store.list_evidence(session.id)
            if item.revision_id == revision.id and item.kind in {"sbom", "provenance", "vulnerability_scan"}
        )
        projection = build_dependency_review(
            old_root,
            new_root,
            supported_paths,
            evidence=exact_supply,
            read_artifact=store.read_evidence_artifact,
        )
        supply: dict[str, list[dict[str, Any]]] = {
            "sboms": [],
            "provenance": [],
            "vulnerability_scans": [],
        }
        supply_key = {
            "sbom": "sboms",
            "provenance": "provenance",
            "vulnerability_scan": "vulnerability_scans",
        }
        for item in exact_supply:
            metadata = parse_evidence_metadata(item.detail)
            supply[supply_key[item.kind]].append(
                {
                    "evidence_id": item.id,
                    "title": item.title,
                    "artifact_digest": str(metadata.get("artifact_digest") or ""),
                    "source": str(metadata.get("observation_source") or item.source_ref or item.source),
                    "observed_at": item.created_at,
                    "content_digest": item.content_hash,
                    "standard": str(metadata.get("standard") or metadata.get("format") or ""),
                    "format_version": str(metadata.get("format_version") or ""),
                    "component_count": int(metadata.get("component_count") or 0),
                }
            )
        projection.update(
            {
                "review_id": session.id,
                "snapshot_id": revision.id,
                "snapshot_number": _display_revision_number(revision),
                "supply_chain": supply,
                "degraded": [],
            }
        )
        return projection

    def _landing_relationship(session: ReviewSession) -> Any | None:
        from lemoncrow.pro.capabilities.review.landing import landing_relationship

        return landing_relationship(store, session.id)

    def _landing_metadata(item: Any | None) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.landing import landing_metadata

        return landing_metadata(item)

    def _readiness_extension_payload(session: ReviewSession, revision: ReviewRevision) -> Mapping[str, Any]:
        if readiness_extension is None:
            return {}
        extra = readiness_extension(session, revision)
        return extra if isinstance(extra, Mapping) else {}

    def _merge_result_fact(session: ReviewSession, revision: ReviewRevision, extension: Mapping[str, Any]) -> object:
        """Phase 10 item 18: the provider's merge result of this Snapshot's head (unknown on a read failure)."""

        from lemoncrow.pro.capabilities.review.landing_api import landing_policy
        from lemoncrow.pro.capabilities.review.merge_result import MergeResultFacts

        policy = landing_policy(extension, _snapshot_review_config(revision))
        required = policy.merge_result_required
        checks = tuple((item.check, item.check_class) for item in policy.merge_result_checks)
        reader = getattr(landing_authority, "merge_result", None)
        if reader is None:
            return MergeResultFacts(required=required, required_checks=checks)
        try:
            observed = reader(session, revision)
        except Exception as exc:  # unknown, never "no new failure"
            logging.getLogger(__name__).warning("merge result unavailable for %s", revision.id, exc_info=True)
            return MergeResultFacts(required=required, required_checks=checks, error=str(exc) or exc.__class__.__name__)
        if not isinstance(observed, MergeResultFacts):
            return MergeResultFacts(required=required, required_checks=checks)
        return replace(observed, required=required, required_checks=checks)

    def _readiness_policy(revision: ReviewRevision, extension: Mapping[str, Any]) -> tuple[Any, Any]:
        """Effective policy generation and evidence-freshness policy for one Snapshot.

        Layers: built-in readiness rules, the trusted base's review config
        (never the Change's own head bytes) and any composition policy the
        readiness extension names. Every freshness source can only tighten.
        """

        from lemoncrow.pro.capabilities.review.freshness import FreshnessPolicy, parse_freshness_policy
        from lemoncrow.pro.capabilities.review.readiness import (
            POLICY_LAYER_STATES,
            PolicyLayerState,
            built_in_policy_layer,
            policy_generation,
            policy_layer,
            trusted_base_policy_layer,
        )

        config = _snapshot_review_config(revision)
        layers = [built_in_policy_layer(), trusted_base_policy_layer(config)]
        freshness = FreshnessPolicy()
        if config.content:
            freshness = freshness.stricter(parse_freshness_policy(config.content, source=config.source))
        for raw in extension.get("policy", ()) or ():
            if isinstance(raw, Mapping):
                raw_state = raw.get("state")
                # An unrecognised layer state fails closed as invalid.
                state: PolicyLayerState | None = (
                    None
                    if raw_state is None
                    else cast(PolicyLayerState, raw_state) if raw_state in POLICY_LAYER_STATES else "invalid"
                )
                supplied = policy_layer(
                    str(raw.get("layer") or "composition"),
                    str(raw.get("source") or "composition"),
                    raw.get("document"),
                    provisional=raw.get("provisional") is True,
                    state=state,
                )
                if supplied.layer == "trusted_base":
                    # The composition owns provider-proven trusted-base
                    # provenance (hosted: commit, blob digest, policy state);
                    # it replaces the layer derived from the base tree.
                    layers[1] = supplied
                else:
                    layers.append(supplied)
        raw_freshness = extension.get("freshness")
        if isinstance(raw_freshness, Mapping) and raw_freshness.get("external_max_age_seconds") is not None:
            try:
                seconds = int(raw_freshness["external_max_age_seconds"])
            except (TypeError, ValueError):
                seconds = 0  # unreadable composition policy fails closed
            freshness = freshness.stricter(
                FreshnessPolicy(
                    external_max_age_seconds=seconds,
                    source=str(raw_freshness.get("source") or "composition"),
                )
            )
        return policy_generation(layers), freshness

    def _readiness_facts(
        session: ReviewSession,
        revision: ReviewRevision,
        extension: Mapping[str, Any],
        evidence_freshness: Mapping[str, Any],
    ) -> tuple[dict[str, object], Mapping[str, Any]]:
        """Gather the built-in readiness facts: stored state only, never a model."""

        from lemoncrow.pro.capabilities.review.readiness import (
            ApprovalFacts,
            BehaviorFacts,
            HumanFeedbackFacts,
            ManualVerificationFacts,
            PolicyDecisionFacts,
            ReviewProgressFacts,
            RunPlanBlocker,
            RunPlanFacts,
            ScmRequirementFact,
            SnapshotProgress,
            StackDependencyFact,
            VerificationFacts,
            approval_records,
            fact_unavailable,
        )

        # The readiness Snapshot, not a fresh ``_latest``: the progress fact and the T2 hand-off
        # below must describe the Snapshot this evaluation is for.
        overview = _overview(session, revision)
        progress = overview.get("progress") or {}
        packet_unreadable = bool(revision.packet_path) and not overview.get("packet_available")
        context = _change_context_payload(store, session)
        facts: dict[str, object] = {
            # Review targets derive from the Snapshot's packet. A packet the
            # store names but cannot read (content store unavailable, a lost
            # artifact) would re-derive coarser file targets the human never
            # judged; that is an unreadable fact (unknown), not "unreviewed".
            "review_progress": (
                fact_unavailable("the Snapshot's review packet is recorded but cannot be read")
                if packet_unreadable
                else ReviewProgressFacts(
                    unreviewed=int(progress.get("unreviewed") or 0),
                    unknown=int(progress.get("unknown") or 0),
                    changed_since_review=int(progress.get("changed_since_review") or 0),
                    needs_changes=int(progress.get("needs_changes") or 0),
                )
            ),
            "extension": tuple(extension.get("conditions", ()) or ()),
        }
        _behavior_collected, behavior_now = _behavior_state(session, revision)
        # Fail closed while the static inventory is still being computed off the
        # request path: its behavior changes are not known yet.
        analyzing = _static_report(session, revision).status == "pending"
        if behavior_now or analyzing:
            facts["behavior"] = BehaviorFacts(
                open=sum(1 for item in behavior_now if item.priority.outstanding),
                blocked=sum(1 for item in behavior_now if item.state == "needs_changes"),
                analyzing=analyzing,
            )
        manual, _manual_items = _manual_checklist(session, revision, behavior_now)
        if manual is not None:
            facts["manual_verification"] = ManualVerificationFacts(
                policy_error=(
                    str(manual["policy_error"])
                    if not str(manual["policy_error"]).startswith("invalid review configuration")
                    else ""
                ),
                required=bool(manual["required"]),
                statuses=tuple(
                    None if row["result"] is None else str(row["result"]["status"]) for row in manual["items"]
                ),
            )

        def _settled_by_human(item: Annotation) -> bool:
            if item.state == "resolved":
                return True
            if item.state != "obsolete":
                return False
            # Only a human settles human feedback. Anchor relocation marks a
            # comment obsolete when the code it named disappeared (the author
            # deleted the file); that is not a human resolution.
            actor = ""
            previous = ""
            for version in store.list_annotation_versions(item.id):
                if version.state == "obsolete" and previous != "obsolete":
                    actor = version.changed_by_actor
                previous = version.state
            return actor == "human"

        annotations = tuple(
            item
            for item in store.list_annotations(session.id)
            if not item.parent_id and item.source == "human" and not _settled_by_human(item)
        )
        request_changes = tuple(item for item in annotations if item.kind == "request_change")
        other_comments = tuple(item for item in annotations if item.kind != "request_change")
        facts["human_feedback"] = HumanFeedbackFacts(
            request_changes=len(request_changes),
            request_changes_removed=sum(1 for item in request_changes if item.state == "obsolete"),
            comments=len(other_comments),
            comments_removed=sum(1 for item in other_comments if item.state == "obsolete"),
        )
        verification_facts = VerificationFacts(
            projection=_verification_projection(session, revision, freshness=evidence_freshness),
            snapshot_number=_display_revision_number(revision),
        )
        facts["verification"] = verification_facts
        # Gate H: behavior targets from advisory LemonCrow-executed results
        # neither block nor satisfy (enforce); report mode records the would-be.
        behavior_facts = facts.get("behavior")
        if isinstance(behavior_facts, BehaviorFacts) and behavior_now:
            gated = _behavior_qualification(session, revision, behavior_now, verification_facts.projection)
            if gated is not None:
                facts["behavior"] = BehaviorFacts(
                    open=gated.open,
                    blocked=gated.blocked,
                    analyzing=behavior_facts.analyzing,
                    advisory=gated.advisory,
                    gate_h_mode=gated.mode,
                    would_be={
                        "open": gated.would_be_open,
                        "blocked": gated.would_be_blocked,
                        "advisory": gated.would_be_advisory,
                    },
                )
        # Phase 10: pre-merge checks, only when trusted policy configures any.
        try:
            premerge = _premerge_facts(session, revision, extension, verification_facts.projection)
        except Exception as exc:  # unreadable pre-merge state blocks as unknown, never passes
            logging.getLogger(__name__).warning("pre-merge facts unavailable for %s: %s", revision.id, exc)
            premerge = fact_unavailable(f"pre-merge check facts are unavailable: {str(exc)[:200]}")
        if premerge is not None:
            facts["premerge"] = premerge
        # R1: required work the effective run plan does not run (Limited by
        # budget, or no catalog executor). A plan that cannot be computed, or
        # one stopped by an invalid trusted-base config, fails closed.
        try:
            readiness_plan = run_plans.peek(session, revision)
            facts["run_plan"] = RunPlanFacts(
                snapshot_number=_display_revision_number(revision),
                blockers=tuple(
                    RunPlanBlocker(id=row.id, title=row.title, status=row.status)
                    for row in readiness_plan.required_blockers()
                ),
                policy_error=readiness_plan.policy_error,
            )
        except Exception as exc:  # readiness fails closed on any planning failure
            logging.getLogger(__name__).warning("run plan for readiness failed for %s: %s", revision.id, exc)
            facts["run_plan"] = RunPlanFacts(
                snapshot_number=_display_revision_number(revision), error=str(exc) or type(exc).__name__
            )
        # P11 item 14: ``independent_ai_reviewer: require`` is visible, never silently satisfied.
        try:
            from lemoncrow.pro.capabilities.review.independence import independence_facts

            independence_policy, independence = _ai_independence(session, revision)
            if independence_policy.mode != "off":
                facts["independent_ai_review"] = independence_facts(independence_policy, independence)
        except Exception as exc:  # unreadable assurance state blocks as unknown, never passes
            logging.getLogger(__name__).warning("independence facts unavailable for %s: %s", revision.id, exc)
            facts["independent_ai_review"] = fact_unavailable(
                f"independent AI review state is unavailable: {str(exc)[:200]}"
            )
        current_outcomes = tuple(item for item in store.latest_outcomes(session.id) if item.revision_id == revision.id)
        records = approval_records(store, session.id, revision.id)
        facts["approval"] = ApprovalFacts(
            changes_requested=sum(1 for item in current_outcomes if item.outcome == "changes_requested"),
            approvals=sum(1 for item in current_outcomes if item.outcome == "lgtm"),
            records=records,
        )
        # The generations standing decisions were taken under: an armed (or
        # withdrawn) landing and every current approval.
        landing = _landing_relationship(session)
        facts["policy_decisions"] = PolicyDecisionFacts(
            landing_generation=(
                str(_landing_metadata(landing).get("policy_generation") or "")
                if landing is not None and landing.status in {"armed", "delegated", "queued", "blocked"}
                else ""
            ),
            approval_generations=tuple(item.policy_generation for item in records),
        )
        facts["scm_requirements"] = tuple(
            ScmRequirementFact(
                id=str(item.get("id")),
                label=str(item.get("title") or item.get("external_id") or item.get("relation") or "SCM requirement"),
                status=str(item.get("status") or ""),
                provider=str(item.get("provider") or ""),
                url=str(item.get("url") or ""),
            )
            for item in context.get("scm_requirements", ())
        )
        blocking_relations = {"depends_on", "requires", "parent", "previous"}
        stack: list[StackDependencyFact] = []
        for item in context.get("relations", ()):
            if str(item.get("relation") or "") not in blocking_relations:
                continue
            target_ref = str(item.get("target_change_ref") or "")
            target = store.resolve_session(target_ref) if target_ref else None
            target_landing = (
                next(
                    (
                        row
                        for row in store.list_relationships(target.id, kind="landing")
                        if row.relation == "merge_when_ready"
                    ),
                    None,
                )
                if target is not None
                else None
            )
            stack.append(
                StackDependencyFact(
                    id=str(item.get("id")),
                    label=str(item.get("title") or target_ref or item.get("external_id") or "Dependent Change"),
                    url=str(item.get("url") or ""),
                    target_ref=target_ref,
                    target_found=target is not None,
                    target_status="" if target is None else str(target.status),
                    target_landing_status="" if target_landing is None else str(target_landing.status),
                    raw_status=str(item.get("status") or ""),
                )
            )
        facts["stack"] = tuple(stack)
        facts["stack"] = tuple(stack)
        facts["merge_result"] = _merge_result_fact(session, revision, extension)
        # Trust T2: this read's target progress, for a composition whose Inbox row needs it. It is
        # derived from the same Snapshot, reviewer marks and annotations the Inbox reads, and target
        # state comes from those alone, so the counts the Inbox's workflow reads are the same.
        # None when the packet is unreadable or no progress was derived: the Inbox derives its own.
        target_progress = (
            None
            if packet_unreadable or not progress
            else SnapshotProgress(
                revision_id=revision.id, counts={str(key): int(value) for key, value in progress.items()}
            )
        )
        return facts, {**context, "target_progress": target_progress}

    def _premerge_policy(revision: ReviewRevision, extension: Mapping[str, Any]) -> Any:
        """Effective pre-merge check policy: operator layers (highest first), then the trusted base.

        Head-side ``premerge`` edits never enter; they are future policy.
        """

        from lemoncrow.pro.capabilities.review.premerge_checks import (
            merge_premerge_policies,
            parse_premerge_section,
            parse_trusted_premerge,
        )

        layers = [
            parse_premerge_section(
                raw.get("document"),
                layer=str(raw.get("layer") or "composition"),
                source=str(raw.get("source") or "composition policy"),
            )
            for raw in extension.get("premerge", ()) or ()
            if isinstance(raw, Mapping)
        ]
        config = _snapshot_review_config(revision)
        if config.content:
            layers.append(parse_trusted_premerge(config.content, source=config.source or config.path))
        return merge_premerge_policies(layers)

    def _premerge_nl_count(session: ReviewSession, revision: ReviewRevision) -> int:
        """Model-evaluated checks the effective (never head-side) policy configures: ``ai.premerge_checks`` calls."""

        policy = _premerge_policy(revision, _readiness_extension_payload(session, revision))
        return sum(1 for check in policy.checks if check.model_evaluated) if policy.configured else 0

    def _premerge_run_state(plan: Any) -> Any:
        from lemoncrow.pro.capabilities.review.capability_catalog import PREMERGE_CHECKS_ENTRY
        from lemoncrow.pro.capabilities.review.premerge_checks import PremergeRunState

        entry = plan.entry(PREMERGE_CHECKS_ENTRY)
        if entry is None:
            source = str(plan.selection.get("source") or "")
            return PremergeRunState(
                reason=f"not in the {source} selection" if source and source != "preset" else "",
                plan_digest=plan.digest,
            )
        return PremergeRunState(
            status=entry.status,
            reason=entry.reason,
            origin=entry.origin,
            depth=entry.depth,
            executor=entry.executor,
            plan_digest=plan.digest,
        )

    def _premerge_facts(
        session: ReviewSession,
        revision: ReviewRevision,
        extension: Mapping[str, Any],
        verification: Mapping[str, Any] | None = None,
        *,
        plan: Any | None = None,
    ) -> Any:
        """Stored facts pre-merge checks read on one Snapshot; ``None`` when no check is configured."""

        from lemoncrow.pro.capabilities.review.intent import intent_bindings, latest_description
        from lemoncrow.pro.capabilities.review.premerge_checks import (
            PremergeFacts,
            PremergeRunState,
            StaticFacts,
            nl_results_from_evidence,
            static_facts_from_report,
            waivers_from_evidence,
        )

        policy = _premerge_policy(revision, extension)
        if not policy.configured:
            return None
        evidence = store.list_evidence(session.id)
        numbers = {item.id: _display_revision_number(item) for item in store.list_revisions(session.id)}
        run: PremergeRunState | None = None
        if any(check.model_evaluated for check in policy.checks):
            try:
                run = _premerge_run_state(plan if plan is not None else run_plans.peek(session, revision))
            except Exception as exc:  # no plan: nothing is scheduled, never a pass
                run = PremergeRunState(reason=f"run plan unavailable: {str(exc)[:160]}")
        description = latest_description(evidence)
        if verification is None:
            verification = _verification_projection(session, revision)
        try:
            static = static_facts_from_report(_static_report(session, revision))
        except Exception as exc:  # unknown contract facts, never "no breaking change"
            static = StaticFacts(status="not_available", reason=str(exc)[:200] or type(exc).__name__)
        ai_unavailable = ""
        if any(check.model_evaluated for check in policy.checks):
            try:
                _resolve_route(session, revision, None, None)
            except Exception as exc:
                ai_unavailable = f"Review Intelligence is unavailable ({str(exc)[:160]})"
        return PremergeFacts(
            policy=policy,
            snapshot_id=revision.id,
            snapshot_number=_display_revision_number(revision),
            changed_paths=_changed_paths(revision),
            description=description,
            description_snapshot_number=None if description is None else numbers.get(description.revision_id),
            intents=intent_bindings(evidence, numbers),
            static=static,
            verification_rows=tuple(row for row in verification.get("checks", ()) if isinstance(row, Mapping)),
            nl_results=tuple(item for item in nl_results_from_evidence(evidence) if item.revision_id == revision.id),
            waivers=waivers_from_evidence(evidence, numbers),
            ai_unavailable=ai_unavailable,
            run=run,
        )

    def _premerge_block(facts: object, *, generation: str, now: str) -> dict[str, Any]:
        """Readiness ``premerge`` block (warning-mode trial, intent drift) when checks are configured."""

        from lemoncrow.pro.capabilities.review.premerge_checks import PremergeFacts, premerge_projection

        if not isinstance(facts, PremergeFacts):
            return {}
        return {"premerge": premerge_projection(facts, generation=generation, now=now)}

    def _persist_readiness_record(
        session: ReviewSession, revision: ReviewRevision, readiness: Mapping[str, Any]
    ) -> None:
        """Keep the Inbox's lightweight readiness record (P9-INBOX) for this Snapshot current.

        A projection of the answer just computed; a failure to store it never
        changes that answer, it only leaves the Inbox saying readiness is stale.
        """

        from lemoncrow.pro.capabilities.review.workflow_state import persist_readiness_summary

        try:
            persist_readiness_summary(
                store,
                review_id=session.id,
                revision_id=revision.id,
                readiness=readiness,
                evaluator=session.reviewer_id,
                watermark=store.fact_watermarks([session.id]).get(session.id, ""),
            )
        except Exception:
            logging.getLogger(__name__).warning("readiness record not stored for Review %s", session.id, exc_info=True)

    def _readiness_projection(
        session: ReviewSession, *, now: str | None = None, include_change_set: bool = True, trust: bool = False
    ) -> dict[str, Any]:
        """Readiness from the one core (``review/readiness.py``) plus landing state.

        Facts are gathered from durable state, then evaluated deterministically
        at one instant (*now*, default the current time) against one policy
        generation; nothing here calls a model, so the same state and instant
        always yield the same result. ``include_change_set=False`` is the
        Change's own readiness, which other change-set members read.
        ``trust`` (Review Status only) builds the T2 trust section of a live
        evaluation when ``readiness.trust_mode`` is not ``off``.
        """

        from lemoncrow.pro.capabilities.review.freshness import assess_freshness
        from lemoncrow.pro.capabilities.review.readiness import (
            ReadinessInputs,
            ReadinessSources,
            fact_unavailable,
            trust_mode,
        )

        registry = readiness_registry if include_change_set else own_readiness_registry
        revision = _latest(session)
        live_evaluation = now is None
        now = now or utc_now()
        extension = _readiness_extension_payload(session, revision)
        policy, freshness_policy = _readiness_policy(revision, extension)
        evidence_freshness = {
            item.id: assess_freshness(item, now=now, policy=freshness_policy)
            for item in _current_evidence(session, revision)
        }
        context: Mapping[str, Any]
        try:
            facts, context = _readiness_facts(session, revision, extension, evidence_freshness)
        except Exception as exc:  # a read failure is unknown, never a pass and never a regression
            logging.getLogger(__name__).warning("readiness facts unavailable for Review %s", session.id, exc_info=True)
            error = str(exc) or exc.__class__.__name__
            facts = {entry.key: fact_unavailable(error) for entry in registry.entries}
            context = {}
        work_items = context.get("work_items", [])
        scm_rows = context.get("scm", ())
        facts = registry.gather(
            ReadinessSources(store=store, session=session, revision=revision, evaluated_at=now, policy=policy),
            facts,
        )
        result = registry.evaluate(
            ReadinessInputs(
                review_id=session.id,
                snapshot_id=revision.id,
                snapshot_number=_display_revision_number(revision),
                evaluated_at=now,
                policy=policy,
                freshness=freshness_policy,
                evidence=evidence_freshness,
                facts=facts,
            )
        )
        readiness = result.to_wire()
        # A Change's own readiness (read by another change-set member) omits the set
        # blocker, so it never becomes the Inbox's record of this Snapshot.
        if live_evaluation and include_change_set:
            _persist_readiness_record(session, revision, readiness)
        raw_landing = extension.get("landing")
        extension_landing: Mapping[str, Any] = raw_landing if isinstance(raw_landing, Mapping) else {}
        landing = _landing_relationship(session)
        landing_metadata = _landing_metadata(landing)
        landing_state = str(getattr(landing, "status", "") or "") if landing is not None else ""
        semantic_stale = (
            bool(landing)
            and bool(landing_metadata.get("tree_fingerprint"))
            and str(landing_metadata.get("tree_fingerprint")) != revision.tree_fingerprint
        )
        if semantic_stale and landing_state not in {"merged", "blocked"}:
            # A newer Snapshot outdates an intent, never a merge that happened.
            landing_state = "stale"
        provider = str(getattr(landing, "provider", "") or "") if landing is not None else ""
        if not provider and scm_rows:
            provider = str(scm_rows[0].get("provider") or "")
        if not provider:
            provider = str(extension_landing.get("provider") or "")
        # A generic SCM identity is not itself a landing capability. Hosted
        # compositions advertise adapter support explicitly; local mode only
        # delegates GitHub PR landing through gh.
        extension_supported = extension_landing.get("supported") is True
        explicit_delegate_supported = landing_delegate is not None and bool(provider)
        local_github_supported = (
            landing_delegate is None and readiness_extension is None and session.source_ref.startswith("github:")
        )
        supported = effective_landing_delegate is not None and (
            extension_supported or explicit_delegate_supported or local_github_supported
        )
        armed = landing_state in {"armed", "delegated", "queued"} and not semantic_stale
        armed_generation = str(landing_metadata.get("policy_generation") or "")
        landing_extra = _landing_extras(session, revision, extension_landing, landing_metadata, provider, now=now)
        projection: dict[str, Any] = {
            "review_id": session.id,
            "snapshot_id": revision.id,
            "snapshot_number": _display_revision_number(revision),
            "tree_fingerprint": revision.tree_fingerprint,
            "source_fingerprint": revision.source_fingerprint,
            **readiness,
            # Item 11: the exact reasons in Brief, Inbox and the SCM status (one vocabulary).
            "blocking_reasons": _blocking_reasons(readiness),
            "freshness_policy": freshness_policy.to_wire(),
            "work_items": work_items,
            **_premerge_block(facts.get("premerge"), generation=policy.digest, now=now),
            "landing": {
                "supported": supported,
                "provider": provider,
                "state": landing_state or "not_armed",
                "armed": armed,
                "semantic_stale": semantic_stale,
                "revision_id": str(landing_metadata.get("revision_id") or ""),
                "tree_fingerprint": str(landing_metadata.get("tree_fingerprint") or ""),
                "conditions_hash": str(landing_metadata.get("conditions_hash") or ""),
                "detail": str(landing_metadata.get("detail") or ""),
                "url": (
                    str(getattr(landing, "url", "") or "")
                    if landing is not None
                    else str(extension_landing.get("url") or "")
                ),
                "mechanism": str(landing_metadata.get("mechanism") or ""),
                # The generation the intent was armed (or last revalidated)
                # under. An armed intent under another generation is a stale
                # decision: it is revalidated before it may keep landing.
                "policy_generation": armed_generation,
                "policy_stale": armed and armed_generation != policy.digest,
                "readiness_valid_until": str(landing_metadata.get("readiness_valid_until") or ""),
                # ``pending``: a provider cancellation is owed until the provider confirms it.
                "provider_cancel": str(landing_metadata.get("provider_cancel") or ""),
                **landing_extra,
            },
        }
        if trust and live_evaluation and include_change_set:
            mode = trust_mode()
            if mode != "off":
                section = _trust_section(session, result, now=now, mode=mode, progress=context.get("target_progress"))
                if mode == "on":
                    projection["trust"] = section
        return projection

    def _trust_section(
        session: ReviewSession,
        result: Any,
        *,
        now: str,
        mode: str,
        progress: SnapshotProgress | None = None,
    ) -> dict[str, Any]:
        """T2: the trust section of one live readiness answer, and its shadow consistency log line.

        Built after the viewer's readiness record was stored, so the Inbox
        derivation the composition supplies reads the same answer. A workflow
        read that fails leaves the section ``unknown``; readiness is unaffected.
        ``progress`` is this read's own target progress, handed to the
        composition so its Inbox row need not derive the targets again.
        The line's ``build_ms`` is the section's whole cost to this read (the
        workflow read, the items with their validity chains, the digest and the
        shadow checks): the trust plan's 150 ms p95 budget is measured on it.
        """

        from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
        from lemoncrow.pro.capabilities.review.readiness import trust_mismatches, trust_section
        from lemoncrow.pro.capabilities.review.trust_telemetry import (
            TRUST_SECTION,
            section_failure,
            section_observation,
        )

        started = time.perf_counter()
        supplied: Mapping[str, Any] | None = None
        if review_workflow is not None:
            instant = parse_timestamp(now) or datetime.now(UTC)
            try:
                supplied = review_workflow(session, instant, progress)
            except Exception:
                logging.getLogger(__name__).warning(
                    "Review %s workflow unavailable for trust", session.id, exc_info=True
                )
        workflow = supplied.get("workflow") if isinstance(supplied, Mapping) else None
        next_action = supplied.get("next_action") if isinstance(supplied, Mapping) else None
        try:
            section = trust_section(
                result,
                workflow=workflow if isinstance(workflow, Mapping) else None,
                next_action=next_action if isinstance(next_action, Mapping) else None,
            )
            mismatches = trust_mismatches(section)
        except Exception:
            # Reported, never absorbed: the read fails exactly as it would unobserved.
            failed_ms = (time.perf_counter() - started) * 1000.0
            _observe_trust(TRUST_SECTION, lambda: section_failure(mode=mode, build_ms=failed_ms))
            raise
        build_ms = (time.perf_counter() - started) * 1000.0
        summary = section["summary"]
        (_TRUST_LOG.warning if mismatches else _TRUST_LOG.info)(
            "review trust %s review=%s revision=%s state=%s rule=%s items=%d validity=%d mismatches=%s digest=%s"
            " build_ms=%.1f",
            mode,
            session.id,
            section["revision_id"],
            summary["state"],
            summary["rule"],
            len(section["items"]),
            sum(len(item["validity"]) for item in section["items"]),
            ",".join(mismatches) or "none",
            section["digest"],
            build_ms,
        )
        _observe_trust(
            TRUST_SECTION,
            lambda: section_observation(section, mode=mode, mismatches=mismatches, build_ms=build_ms),
        )
        return section

    def _observe_trust(kind: str, observe: Callable[[], Mapping[str, Any]]) -> None:
        """Hand the composition one operator observation; a failure is logged and changes no answer."""

        if trust_observer is None:
            return
        try:
            trust_observer(kind, observe())
        except Exception:
            logging.getLogger(__name__).warning("trust observer failed for %s", kind, exc_info=True)

    def _blocking_reasons(readiness: Mapping[str, Any]) -> list[dict[str, Any]]:
        from lemoncrow.pro.capabilities.review.blocking_reasons import blocking_reasons

        return blocking_reasons([row for row in readiness.get("conditions") or () if isinstance(row, Mapping)])

    def _landing_extras(
        session: ReviewSession,
        revision: ReviewRevision,
        extension_landing: Mapping[str, Any],
        metadata: Mapping[str, Any],
        provider: str,
        *,
        now: str,
    ) -> dict[str, Any]:
        """P10-LAND: auto-approval, break-glass and the SCM-owned landing state (store reads only)."""

        from lemoncrow.pro.capabilities.review.landing_api import landing_extras

        scm = {
            # Native auto-merge / merge queue state is the SCM's; LemonCrow only shows it.
            "managed_by": provider,
            "mechanism": str(metadata.get("mechanism") or ""),
            "auto_merge": str(extension_landing.get("auto_merge") or ""),
            "queue_state": str(extension_landing.get("queue_state") or ""),
        }
        basis = {
            "basis": str(metadata.get("basis") or ""),
            "break_glass_id": str(metadata.get("break_glass_id") or ""),
            "bypassed": [str(item) for item in metadata.get("bypassed") or ()],
        }
        try:
            extras = landing_extras(store, session, revision, now=now)
        except Exception as exc:  # shown as unavailable; readiness itself is unaffected
            logging.getLogger(__name__).warning("landing extras unavailable for %s", session.id, exc_info=True)
            extras = {"auto_approval": {"state": "unknown", "detail": str(exc)[:200]}, "break_glass": []}
        return {"scm": scm, **basis, **extras}

    def _armed_landing_revision(session: ReviewSession, landing: Any) -> ReviewRevision:
        armed_revision_id = str(_landing_metadata(landing).get("revision_id") or "")
        armed = store.get_revision(armed_revision_id) if armed_revision_id else None
        return armed if armed is not None and armed.review_id == session.id else _latest(session)

    def _settle_owed_landing_cancel(session: ReviewSession) -> None:
        """Perform a provider cancel an earlier step owes (Snapshot change, discard, readiness withdrawal)."""

        from lemoncrow.pro.capabilities.review.landing import complete_pending_landing_cancel, record_landing_state

        landing = _landing_relationship(session)
        metadata = _landing_metadata(landing)
        if landing is None or metadata.get("provider_cancel") != "pending":
            return
        if effective_landing_delegate is None:
            withdrawn = metadata.get("cancel_reason") == "readiness"
            record_landing_state(
                store,
                landing,
                status="blocked" if withdrawn else landing.status,
                metadata_updates={
                    "provider_cancel": "unavailable",
                    "detail": (
                        f"{metadata.get('withdraw_detail') or ''} No connected landing delegate is available to "
                        "withdraw provider landing; withdraw it in the provider.".strip()
                        if withdrawn
                        else "Semantic Snapshot changed; no connected landing delegate is available to cancel provider landing."
                    ),
                },
            )
            return
        armed_revision = _armed_landing_revision(session, landing)
        delegate = effective_landing_delegate
        complete_pending_landing_cancel(store, session.id, lambda: delegate(session, armed_revision, action="cancel"))

    async def _complete_pending_landing_cancel(session: ReviewSession) -> None:
        """Cancel provider landing still owed after a semantic Snapshot change."""

        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(_settle_owed_landing_cancel, session)

    async def _invalidate_landing_after_semantic_change(
        session: ReviewSession,
        previous_revision: ReviewRevision,
        next_revision: ReviewRevision,
    ) -> None:
        from lemoncrow.pro.capabilities.review.landing import mark_landing_stale_for_revision

        if previous_revision.tree_fingerprint != next_revision.tree_fingerprint:
            # Capture already records this; repeating it is idempotent and keeps
            # this path correct for a store populated by an older writer.
            mark_landing_stale_for_revision(store, session.id, next_revision)
        await _complete_pending_landing_cancel(session)

    async def _withdraw_landing_for_discard(session: ReviewSession) -> None:
        """A discarded Change never lands: withdraw its Merge-when-ready intent.

        The withdrawal is recorded as owed before the provider is asked, so a
        provider failure leaves it owed (retried by the landing watch and the
        next action that settles owed cancels) rather than a terminal state
        while the provider could still merge the discarded Change.
        """

        from lemoncrow.pro.capabilities.review.landing import owe_discard_withdrawal, record_landing_state

        owed = owe_discard_withdrawal(store, session.id)
        if owed is not None and effective_landing_delegate is None:
            # No provider holds this intent any more: it simply ends here.
            record_landing_state(
                store,
                owed,
                status="cancelled",
                metadata_updates={
                    "provider_cancel": "done",
                    "cancel_state": "cancelled",
                    "provider_state": "cancelled",
                    "detail": "The Change was discarded; Merge when ready was withdrawn.",
                },
            )
            return
        await _complete_pending_landing_cancel(session)

    def _record_landing_activity(
        session: ReviewSession,
        landing: Any,
        *,
        kind: str,
        summary: str,
        detail: Mapping[str, Any],
    ) -> None:
        """Audit one landing decision LemonCrow took on its own (never a human judgment)."""

        store.record_activity(
            ReviewActivityEvent(
                id="",
                review_id=session.id,
                revision_id=str(_landing_metadata(landing).get("revision_id") or ""),
                kind=kind,  # type: ignore[arg-type]
                actor_id="lemoncrow",
                actor_type="unknown",
                subject_type="landing",
                subject_id=str(getattr(landing, "id", "") or ""),
                summary=summary,
                detail_json=json.dumps(dict(detail), ensure_ascii=False, sort_keys=True),
            )
        )

    def _note_policy_change(session: ReviewSession, readiness: Mapping[str, Any], *, origin: str) -> None:
        """Audit a policy generation change once, and hand it to the attention hook.

        Fires when readiness shows decisions (an armed landing, approvals)
        taken under an older generation: one ``policy.generation_changed``
        event per (previous generations, generation), naming both.
        """

        from lemoncrow.pro.capabilities.review.readiness import (
            PolicyChangeNotice,
            approval_records,
            definite_blockers,
            short_generation,
        )

        row = next((item for item in readiness["conditions"] if item["id"] == "policy-generation"), None)
        if row is None:
            return
        previous = [str(value) for value in row.get("previous_generations") or ()]
        generation = str(row.get("generation") or "")
        # lc-debt: check-then-insert; two concurrent first reads may both audit
        # the same change. A unique (review, kind, subject) key would close it.
        for event in store.list_activity_events(session.id, kinds=("policy.generation_changed",), limit=5000):
            try:
                seen = json.loads(event.detail_json or "{}")
            except ValueError:
                continue
            if seen.get("generation") == generation and list(seen.get("previous_generations") or ()) == previous:
                return
        blocked_by = [
            str(item["label"])
            for item in definite_blockers(readiness["conditions"])
            if item["id"] != "policy-generation"
        ]
        approved = [
            item.reviewer_id
            for item in approval_records(store, session.id, str(readiness["snapshot_id"]))
            if item.policy_generation in previous
        ]
        change = f"{', '.join(short_generation(value) for value in previous)} -> {short_generation(generation)}"
        store.record_activity(
            ReviewActivityEvent(
                id="",
                review_id=session.id,
                revision_id=str(readiness["snapshot_id"]),
                kind="policy.generation_changed",
                actor_id="lemoncrow",
                actor_type="unknown",
                subject_type="policy_generation",
                subject_id=generation,
                summary=(
                    f"Review policy changed (generation {change}): "
                    + ("the current policy is not satisfied" if blocked_by else "standing decisions still satisfy it")
                ),
                detail_json=json.dumps(
                    {
                        "origin": origin,
                        "previous_generations": previous,
                        "generation": generation,
                        "tightened": bool(blocked_by),
                        "blocked_by": blocked_by,
                        "approved_under_previous": approved,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        if policy_change_hook is not None:
            try:
                policy_change_hook(
                    PolicyChangeNotice(
                        review_id=session.id,
                        snapshot_id=str(readiness["snapshot_id"]),
                        previous_generations=tuple(previous),
                        generation=generation,
                        tightened=bool(blocked_by),
                        blocked_by=tuple(blocked_by),
                        approved_under_previous=tuple(approved),
                    )
                )
            except Exception:  # attention routing never breaks readiness
                logging.getLogger(__name__).exception("policy change hook failed for Review %s", session.id)

    def _revalidate_armed_landing(session: ReviewSession, *, origin: str) -> str:
        """Revalidate a Change's standing landing decisions; the only path that withdraws them.

        Two decisions, one path (P10-LAND L2): the policy auto-approval
        (``landing_api.revalidate`` -> ``auto_approval.revalidate_auto_approval``)
        and the armed Merge-when-ready intent (:func:`_revalidate_merge_when_ready`).
        Returns the landing outcome, or the approval's when no landing is armed.
        """

        try:
            approval = landing_revalidate(session, origin)
        except Exception:  # an approval read failure never blocks the human intent's revalidation
            logging.getLogger(__name__).warning(
                "auto-approval revalidation failed for Review %s", session.id, exc_info=True
            )
            approval = "deferred"
        landing = _revalidate_merge_when_ready(session, origin=origin)
        return approval if landing == "none" else landing

    def _armed_trusted_base_stale(session: ReviewSession, origin: str) -> str | None:
        """L4 re-read for an armed landing: the reason when the default branch's policy moved, else ``None``.

        Re-read on every revalidation but a plain readiness read (the hosted sweep
        covers those). Only a definite ``stale`` withdraws: an unreadable base
        (``unknown``) never cancels a human's decision.
        """

        from lemoncrow.pro.capabilities.review import landing_api as _landing_api

        if origin not in _landing_api.L4_RECHECK_ORIGINS or not isinstance(
            landing_authority, _landing_api.LandingAuthority
        ):
            return None
        fact = _landing_api.trusted_base_now(landing_authority, session, _latest(session))
        if fact is None or fact.state != "stale":
            return None
        return fact.detail or "The default branch's review policy changed after this Snapshot was captured."

    def _revalidate_merge_when_ready(session: ReviewSession, *, origin: str) -> str:
        """Revalidate one armed landing against current readiness; the only path that withdraws it.

        Shared by Reader actions (``action``), readiness reads (``read``), the
        hosted maintenance sweep (``sweep``) and the local landing watch
        (``watch``). Readiness is evaluated at one instant under the current
        policy generation, as the reviewer who armed the intent: their judgment
        is what it carries, so another reader's unfinished marks never cancel it.

        * Still armable: a changed generation or validity is recorded as a
          revalidation (audited when the generation changed). Never arms more.
        * Blocked only by conditions that could not be evaluated (a store read
          or a rule that raised, :data:`readiness.TRANSIENT_REASONS`), or the
          evaluation itself failed: deferred and audited once per cause. A
          transient failure never withdraws a human's decision.
        * Blocked by a definite unsatisfied condition (including expired
          evidence and a policy generation it no longer satisfies): the
          withdrawal is claimed compare-and-set (exactly one caller wins, so
          repeated or concurrent reads never cancel or audit twice), audited,
          then performed through the provider; a provider failure stays owed
          and is retried.

        A break-glass landing (``basis: break_glass``) holds with exactly its
        named blockers bypassed: any *other* LemonCrow-owned regression still
        withdraws it.

        L4 (RC11): outside a plain read the default branch's review policy is
        re-read; once it moved away from the one the Snapshot was captured
        with, the landing is withdrawn (``trusted-base``, never bypassable).

        Returns ``none``, ``stale``, ``unchanged``, ``revalidated``,
        ``deferred``, ``raced`` or ``withdrawn``.
        """

        from dataclasses import replace as _replace

        from lemoncrow.pro.capabilities.review.break_glass import effective_blockers
        from lemoncrow.pro.capabilities.review.landing import (
            ARMED_LANDING_STATES,
            claim_readiness_withdrawal,
            record_landing_state,
        )
        from lemoncrow.pro.capabilities.review.readiness import (
            SATISFIED_STATES,
            definite_blockers,
            short_generation,
        )

        # A capture path without the delegate may have left a provider
        # cancellation owed; any later pass over the Change settles it.
        _settle_owed_landing_cancel(session)
        landing = _landing_relationship(session)
        if landing is None or landing.status not in ARMED_LANDING_STATES:
            return "none"
        armed_metadata = _landing_metadata(landing)
        armed_by = str(armed_metadata.get("armed_by") or "")
        subject = _replace(session, reviewer_id=armed_by) if armed_by else session

        def _defer(cause: str) -> str:
            if armed_metadata.get("revalidation_deferred") == cause:
                return "deferred"
            written = store.update_relationship_if_unchanged(
                landing,
                status=landing.status,
                metadata_json=json.dumps(
                    {**armed_metadata, "revalidation_deferred": cause}, ensure_ascii=False, sort_keys=True
                ),
            )
            if written is not None:
                _record_landing_activity(
                    session,
                    written,
                    kind="landing.revalidation_deferred",
                    summary="Readiness could not be evaluated; Merge when ready was kept, not withdrawn",
                    detail={"origin": origin, "cause": cause},
                )
            return "deferred"

        try:
            readiness = _readiness_projection(subject)
        except Exception as exc:  # a read failure is not a regression of the human decision
            logging.getLogger(__name__).warning(
                "landing revalidation deferred for Review %s", session.id, exc_info=True
            )
            return _defer(f"readiness evaluation failed: {(str(exc) or exc.__class__.__name__)[:300]}")
        if readiness["landing"]["semantic_stale"]:
            return "stale"  # the capture path owns a semantic invalidation
        _note_policy_change(session, readiness, origin=origin)
        generation = str(readiness["policy_generation"]["digest"])
        armed_generation = str(armed_metadata.get("policy_generation") or "")
        policy_changed = bool(armed_generation) and armed_generation != generation
        change = f"generation {short_generation(armed_generation)} -> {short_generation(generation)}"
        bypassed = (
            [str(item) for item in armed_metadata.get("bypassed") or ()]
            if armed_metadata.get("basis") == "break_glass"
            else None
        )
        armable = (
            readiness["armable"] if bypassed is None else not effective_blockers(readiness["conditions"], bypassed)
        )
        try:
            base_stale = _armed_trusted_base_stale(session, origin)
        except Exception:  # an unreadable base is unknown, never a withdrawal
            logging.getLogger(__name__).warning("trusted base re-read failed for Review %s", session.id, exc_info=True)
            base_stale = None
        if base_stale is not None:
            armable = False
        if armable:
            valid_until = str(readiness["valid_until"])
            if (
                armed_generation == generation
                and str(armed_metadata.get("readiness_valid_until") or "") == valid_until
                and not armed_metadata.get("revalidation_deferred")
            ):
                return "unchanged"
            # Weakening (or an unchanged-outcome tightening) keeps the human's
            # explicit intent exactly as armed: it never arms more.
            updates: dict[str, Any] = {
                "policy_generation": generation,
                "policy_layers": readiness["policy_generation"]["layers"],
                "readiness_valid_until": valid_until,
                "revalidation_deferred": "",
            }
            if policy_changed:
                updates.update(
                    {
                        "policy_revalidated_from": armed_generation,
                        "detail": (
                            f"Review policy changed after arming ({change}); the armed landing was revalidated "
                            "and still satisfies it."
                        ),
                    }
                )
            written = store.update_relationship_if_unchanged(
                landing,
                status=landing.status,
                metadata_json=json.dumps({**armed_metadata, **updates}, ensure_ascii=False, sort_keys=True),
            )
            if written is None:
                return "raced"
            if policy_changed:
                _record_landing_activity(
                    session,
                    written,
                    kind="landing.revalidated",
                    summary=f"Review policy changed ({change}); Merge when ready still satisfies it",
                    detail={"origin": origin, "previous_generation": armed_generation, "generation": generation},
                )
            return "revalidated"
        # ``policy-generation`` only explains the others; it is never a cause.
        blockers = [
            item
            for item in definite_blockers(readiness["conditions"])
            if item["id"] != "policy-generation" and (bypassed is None or item["id"] not in bypassed)
        ]
        if base_stale is not None:
            blockers.append({"id": "trusted-base", "label": "Trusted base current", "reason": base_stale})
        if not blockers:
            transient = [
                str(item["label"])
                for item in readiness["conditions"]
                if item["owner"] != "scm"
                and item["state"] not in SATISFIED_STATES
                and (bypassed is None or item["id"] not in bypassed)
            ]
            return _defer("; ".join(transient) or "readiness could not be evaluated")
        labels = [str(item["label"]) for item in blockers]
        cause = (
            f"Review policy changed after arming ({change}) and the new policy is not satisfied ("
            if policy_changed
            else "A LemonCrow-owned landing condition regressed after arming ("
        )
        claimed = claim_readiness_withdrawal(
            store,
            landing,
            detail=f"{cause}{'; '.join(labels)}).",
            blocked_by=labels,
            policy_generation=generation,
        )
        if claimed is None:
            return "raced"  # another pass (or a human) changed the intent first
        _record_landing_activity(
            session,
            claimed,
            kind="landing.withdrawn",
            summary=f"Merge when ready withdrawn: {'; '.join(labels)}"[:500],
            detail={
                "origin": origin,
                "blocked_by": [
                    {"id": str(item["id"]), "label": str(item["label"]), "reason": str(item.get("reason") or "")}
                    for item in blockers
                ],
                "previous_generation": armed_generation,
                "generation": generation,
                "policy_changed": policy_changed,
            },
        )
        if effective_landing_delegate is None:
            record_landing_state(
                store,
                claimed,
                status="blocked",
                metadata_updates={
                    "provider_cancel": "done",
                    "cancel_state": "cancelled",
                    "detail": f"{cause}{'; '.join(labels)}). Resolve it and re-arm.",
                },
            )
            return "withdrawn"
        _settle_owed_landing_cancel(session)
        return "withdrawn"

    def _revalidate_landing_by_id(review_id: str, *, origin: str) -> str:
        """Request-free entry (``app.state.review_landing_revalidator``): hosted sweep, local watch."""

        session = store.get_session(review_id)
        if session is None or session.repo_root != expected_session_repo:
            return "none"
        return _revalidate_armed_landing(session, origin=origin)

    def _trusted_base_moved_for(review_id: str) -> bool:
        """Whether the default branch's review policy moved away from the latest Snapshot's (L4 ``stale``).

        The hosted sweep's due check: a default-branch policy change touches
        nothing on the Change, so without it a standing decision would never be
        revalidated against the moved authority.
        """

        session = store.get_session(review_id)
        if session is None or session.repo_root != expected_session_repo:
            return False
        return _armed_trusted_base_stale(session, "sweep") is not None

    def _policy_generation_for(review_id: str) -> dict[str, Any]:
        """Current effective policy generation of a Change's latest Snapshot, without evaluating readiness."""

        session = store.get_session(review_id)
        if session is None or session.repo_root != expected_session_repo:
            return {}
        revision = _latest(session)
        policy, _freshness = _readiness_policy(revision, _readiness_extension_payload(session, revision))
        wire: dict[str, Any] = policy.to_wire()
        return wire

    async def _enforce_armed_landing(session: ReviewSession, *, origin: str = "action") -> str:
        """Revalidate an armed landing after a Reader action (see :func:`_revalidate_armed_landing`)."""

        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(lambda: _revalidate_armed_landing(session, origin=origin))

    def _derive_targets(
        session: ReviewSession,
        revision: ReviewRevision,
        units: Sequence[ReviewUnit],
        frontier_entries: Sequence[FrontierEntry],
        packet: Mapping[str, Any] | None,
        annotations: Sequence[Annotation],
    ) -> tuple[Any, ...]:
        from lemoncrow.pro.capabilities.review.targets import derive_review_targets

        evidence = _current_evidence(session, revision)
        # derive_review_targets is pure; every Reader endpoint (overview, targets,
        # readiness, map, ...) re-derived it -- ~0.4s each on a large Snapshot. The
        # key is every input (frozen rows; the packet is fixed by the revision's
        # packet digest), so any mark, comment, evidence or unit change misses.
        key = hashlib.sha256(
            repr(
                (revision, packet is None, tuple(units), tuple(frontier_entries), tuple(annotations), evidence)
            ).encode("utf-8", "surrogatepass")
        ).hexdigest()
        with _base_targets_lock:
            base = _base_targets_cache.get(key)
        if base is None:
            base = tuple(
                derive_review_targets(
                    units,
                    frontier_entries,
                    packet,
                    annotations=annotations,
                    evidence=evidence,
                )
            )
            with _base_targets_lock:
                _base_targets_cache[key] = base
                while len(_base_targets_cache) > 16:
                    _base_targets_cache.pop(next(iter(_base_targets_cache)))
        return _with_plan_inputs(session, revision, units, packet, base)[0]

    _base_targets_cache: dict[str, tuple[Any, ...]] = {}
    _base_targets_lock = threading.Lock()

    def _movement_projection(
        session: ReviewSession, revision: ReviewRevision, payloads: Sequence[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Phase 9 item 34: label base-only movement since this reviewer's judgment."""

        from lemoncrow.pro.capabilities.review.rebase_classification import (
            label_target_payloads,
            movement_for_reviewer,
            movement_summary,
        )

        movement = movement_for_reviewer(store, session.id, revision, reviewer_id=session.reviewer_id)
        labeled = label_target_payloads(payloads, movement)
        if movement is None:
            return labeled, None
        return labeled, movement_summary(
            movement, labeled, display_number=_display_revision_number(movement.from_revision)
        )

    _routine_proof_cache: dict[str, Any] = {}
    _check_rows_cache: dict[str, Any] = {}
    _contract_cache: dict[str, Any] = {}
    _synthesis_request_cache: dict[str, Any] = {}

    def _new_side_reader(session: ReviewSession, revision: ReviewRevision) -> Callable[[str], str | None]:
        """New-side text for one Snapshot with one decode of its blob artifact."""

        loaded: list[Mapping[str, str] | None] = []

        def read(path: str) -> str | None:
            if not loaded:
                loaded.append(store.read_blob_artifact(session.id, revision.id))
            blobs = loaded[0]
            if blobs is not None and path in blobs:
                return blobs[path]
            if blobs is not None and revision.range_mode != "commit_range":
                return None
            return _snapshot_text(store, session.id, revision, resolved_repo, path, side="new")

        return read

    def _effective_intelligence_policy(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        policy: dict[str, Any] = {
            "immutable_snapshot": True,
            "human_judgment_authoritative": True,
            "inference_marks_reviewed": False,
        }
        if intelligence_policy is not None:
            policy["effective_review"] = dict(intelligence_policy(session, revision))
        return policy

    def _route_policy(session: ReviewSession, revision: ReviewRevision) -> EffectiveIntelligencePolicy | None:
        """The operator intelligence policy for this Review (``None``: this process's policy applies)."""

        from lemoncrow.pro.capabilities.review.intelligence_policy import review_intelligence_policy

        if intelligence_policy is None:
            return None
        return review_intelligence_policy(_effective_intelligence_policy(session, revision))

    def _resolve_route(
        session: ReviewSession, revision: ReviewRevision, driver: str | None, model: str | None
    ) -> ResolvedDriver:
        """Resolve a route under this Review's effective intelligence policy."""

        from lemoncrow.pro.capabilities.review.intelligence_runtime import resolve_driver_model

        policy = _route_policy(session, revision)
        if policy is None:
            # The legacy two-argument shape keeps existing stand-ins working.
            return resolve_driver_model(driver, model)
        return resolve_driver_model(driver, model, policy=policy)

    def _critical_ai_rules(policy: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        """Consume configured legacy critical-rule inputs when the policy seam provides them."""

        effective_review = policy.get("effective_review")
        if not isinstance(effective_review, Mapping):
            return ()
        requirements = effective_review.get("requirements")
        if not isinstance(requirements, Mapping):
            return ()
        raw = requirements.get("critical_rules")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()
        return tuple(row for row in raw if isinstance(row, Mapping))

    def _brief_intelligence_inputs(
        session: ReviewSession,
        revision: ReviewRevision,
        view: Any,
        targets: Sequence[Any],
        checks: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
        """Stable Brief identity inputs with target/path policy identity."""

        target_paths = {
            str(target.target_id): str(target.path)
            for target in targets
            if str(getattr(target, "target_id", "")) and str(getattr(target, "path", ""))
        }
        context = {
            "guidance_ids": sorted(str(record.id) for record in view.records),
            "check_keys": sorted(
                {
                    str(row.get("name") or row.get("source_ref") or "")
                    for row in checks
                    if str(row.get("name") or row.get("source_ref") or "")
                }
            ),
            "input_bounds": {"max_items": 12, "max_chars": 24000},
        }
        return {"target_ids": sorted(target_paths)}, context, target_paths

    def _intelligence_spec(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        capability: Literal["brief_synthesis", "tour_synthesis", "behavior_narrative", "premerge_check"],
        scope: Mapping[str, Any],
        context: Mapping[str, Any],
        output_schema: Mapping[str, Any],
        prompt_version: int,
        schema_version: int,
        target_paths: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.data_governance import process_route_trust
        from lemoncrow.pro.capabilities.review.intelligence_runtime import (
            IntelligenceRunRequest,
            ModelCallBudget,
            build_intelligence_identity,
        )
        from lemoncrow.pro.capabilities.review.rules import content_access_for_path, organization_block

        budget = ModelCallBudget.from_env(max_calls=1)
        snapshot_identity = revision.source_fingerprint or revision.tree_fingerprint or revision.id
        policy = _effective_intelligence_policy(session, revision)
        blocked = organization_block(policy.get("effective_review")).get("ai", "")
        try:
            if blocked:
                # The organization AI block: no model call, whatever lower layers ask.
                raise PermissionError(blocked)
            resolved = _resolve_route(session, revision, None, None)
        except Exception as exc:
            return {
                "request": None,
                "row": None,
                "budget": budget,
                "model": "",
                "driver": "",
                "policy_digest": "",
                "context_digest": "",
                "snapshot_identity": snapshot_identity,
                "error": str(exc),
                "allowed_target_ids": (),
                "excluded_by_policy": (),
                "route_trust": "unknown",
            }

        governance = _review_governance(session, revision)
        route_trust = process_route_trust(resolved.driver)
        decisions = {
            target_id: content_access_for_path(
                path,
                governance.content_access,
                route_trust=route_trust,
            )
            for target_id, path in (target_paths or {}).items()
        }
        allowed_target_ids = tuple(sorted(target_id for target_id, decision in decisions.items() if decision.allowed))
        excluded = tuple(decision.to_wire() for _, decision in sorted(decisions.items()) if not decision.allowed)
        if decisions:
            policy = {
                **policy,
                "model_content_access": {
                    "route_trust": route_trust,
                    # Only applicable decisions participate in this capability's
                    # identity, so an unrelated policy row does not churn cache.
                    "decisions": [decisions[target_id].to_wire() for target_id in sorted(decisions)],
                    # CLI reviewers can search the whole filtered Snapshot. The
                    # executor policy therefore participates in identity for CLI
                    # routes even when a row is unrelated to the primary target.
                    "executor_policy": (
                        [row.to_wire() for row in governance.content_access]
                        if resolved.driver in {"claude", "codex", "opencode"}
                        else []
                    ),
                },
            }

        request = IntelligenceRunRequest(
            review_id=session.id,
            revision_id=revision.id,
            snapshot_identity=snapshot_identity,
            capability=capability,
            scope=dict(scope),
            context=dict(context),
            policy=policy,
            driver=resolved.driver,
            model=resolved.model,
            output_schema=dict(output_schema),
            prompt_version=prompt_version,
            schema_version=schema_version,
            budget=budget,
        )
        identity = build_intelligence_identity(
            revision_id=revision.id,
            snapshot_identity=snapshot_identity,
            capability=capability,
            scope=scope,
            context=context,
            policy=policy,
            driver=resolved.driver,
            model=resolved.model,
            output_schema=output_schema,
            prompt_version=prompt_version,
            schema_version=schema_version,
        )
        return {
            "request": request,
            "row": store.get_intelligence_run(session.id, identity.run_key),
            "budget": budget,
            "model": resolved.model,
            "driver": resolved.driver,
            "policy_digest": identity.policy_digest,
            "context_digest": identity.context_digest,
            "snapshot_identity": snapshot_identity,
            "error": "",
            "allowed_target_ids": allowed_target_ids,
            "excluded_by_policy": excluded,
            "route_trust": route_trust,
        }

    def _intelligence_status(
        session: ReviewSession,
        spec: Mapping[str, Any],
        *,
        candidates: int,
    ) -> dict[str, Any]:
        budget = spec["budget"]
        calls_used = store.intelligence_calls(session.id)
        base = {
            "model": str(spec.get("model") or ""),
            "driver": str(spec.get("driver") or ""),
            "candidates": candidates,
            "policy_digest": str(spec.get("policy_digest") or ""),
            "budget": {"calls_used": calls_used, "max_calls": budget.max_review_calls},
            "cache_hit": False,
            "excluded_by_policy": list(spec.get("excluded_by_policy") or ()),
        }
        error = str(spec.get("error") or "")
        if error:
            return {**base, "status": "disabled", "mode": "unavailable", "error": error}
        row = spec.get("row")
        if row is None:
            if budget.max_calls <= 0 or calls_used >= budget.max_review_calls:
                return {
                    **base,
                    "status": "failed",
                    "mode": "skipped_by_budget",
                    "error": "Review Intelligence model-call budget exhausted",
                }
            return {**base, "status": "none", "mode": "deterministic_only", "error": ""}
        status_value = str(row.status)
        row_error = str(row.error or "")
        if status_value == "ready":
            return {**base, "status": "ready", "mode": "cached_synthesis", "cache_hit": True, "error": ""}
        if status_value == "pending":
            return {**base, "status": "pending", "mode": "deterministic_only", "error": ""}
        mode = "skipped_by_budget" if "budget exhausted" in row_error.lower() else "error"
        return {**base, "status": "failed", "mode": mode, "error": row_error}

    def _outcome_status(
        session: ReviewSession,
        spec: Mapping[str, Any],
        outcome: Any,
        *,
        candidates: int,
    ) -> dict[str, Any]:
        block = _intelligence_status(session, {**spec, "row": None}, candidates=candidates)
        block.update(
            {
                "status": outcome.status if outcome.status != "cancelled" else "failed",
                "mode": (
                    "cached_synthesis"
                    if outcome.cache_hit
                    else (
                        "synthesized"
                        if outcome.status == "ready"
                        else "skipped_by_budget" if "budget exhausted" in outcome.error.lower() else "error"
                    )
                ),
                "model": outcome.model,
                "driver": outcome.driver,
                "cache_hit": outcome.cache_hit,
                "error": outcome.error,
            }
        )
        return block

    def _synthesis_request(
        session: ReviewSession,
        revision: ReviewRevision,
        plan: Any,
        targets: Sequence[Any],
        checks: Sequence[Mapping[str, Any]],
        digest: str,
        *,
        allowed_target_ids: Sequence[str] | None = None,
    ) -> Any:
        from lemoncrow.pro.capabilities.review.synthesis import SynthesisInputBounds, build_synthesis_request

        cache_key = digest + ":" + ",".join(sorted(allowed_target_ids or ()))
        cached = _synthesis_request_cache.get(cache_key)
        if cached is not None:
            return cached
        synthesis_request = build_synthesis_request(
            plan,
            targets,
            checks,
            _new_side_reader(session, revision),
            SynthesisInputBounds(),
            allowed_target_ids=allowed_target_ids,
        )
        _bounded_cache_put(_synthesis_request_cache, cache_key, synthesis_request)
        return synthesis_request

    def _routine_proofs(
        session: ReviewSession,
        revision: ReviewRevision,
        targets: Sequence[Any],
        packet: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        """Parser proofs for one immutable Snapshot (target ids are Snapshot-stable)."""

        cached = _routine_proof_cache.get(revision.id)
        if cached is not None:
            return cast(Mapping[str, Any], cached)
        import dataclasses

        from lemoncrow.pro.capabilities.review.routine import ROUTINE_PROOF_VERSION, RoutineProof, routine_proofs

        # Proofs are a pure function of the immutable Snapshot: computed once,
        # then read back from the store (survives restarts and cache eviction).
        stored = store.read_routine_proofs(session.id, revision.id, version=str(ROUTINE_PROOF_VERSION))
        if stored is not None:
            restored = {key: RoutineProof(**row) for key, row in stored.items()}  # type: ignore[arg-type]
            _bounded_cache_put(_routine_proof_cache, revision.id, restored)
            return restored

        # One decode of the Snapshot's blob artifact serves every file's new
        # side; _snapshot_text would decode the whole artifact per file.
        loaded: list[Mapping[str, str] | None] = []
        # Same for the durable source-tree manifest of each side: _snapshot_text
        # would gunzip and parse the whole manifest and materialize a temp tree
        # for every file (seconds on a large repository).
        manifests: dict[str, Mapping[str, Mapping[str, int | str]] | None] = {}

        def read(path: str, side: str) -> str | None:
            if side == "new":
                if not loaded:
                    loaded.append(store.read_blob_artifact(session.id, revision.id))
                blobs = loaded[0]
                if blobs is not None and path in blobs:
                    return blobs[path]
                if blobs is not None and revision.range_mode != "commit_range":
                    return None  # the frozen overlay has no such file
            if side not in manifests:
                manifests[side] = store.read_source_tree_artifact(session.id, revision.id, side=side)
            manifest = manifests[side]
            row = None if manifest is None else manifest.get(path)
            if row is not None:
                digest = str(row["content_digest"])
                payload = store.read_source_content(digest)
                # Anything unverifiable takes the slow, authoritative path below.
                if payload is not None and hashlib.sha256(payload).hexdigest() == digest:
                    return payload.decode("utf-8", errors="replace")
            return _snapshot_text(store, session.id, revision, resolved_repo, path, side=side)

        proofs = routine_proofs(targets, packet, read)
        with contextlib.suppress(OSError, ValueError):  # persisting is best effort
            store.write_routine_proofs(
                session.id,
                revision.id,
                {key: dataclasses.asdict(proof) for key, proof in proofs.items()},
                version=str(ROUTINE_PROOF_VERSION),
            )
        _bounded_cache_put(_routine_proof_cache, revision.id, proofs)
        return proofs

    def _check_rows(session: ReviewSession, revision: ReviewRevision, *, discover: bool) -> list[dict[str, Any]]:
        """Recorded check rows for one Snapshot, cached by the evidence they read.

        ``discover=False`` never materializes the Snapshot to learn web routes;
        an undecidable required scenario then stays required (fail-safe).
        """

        # Signed rows the trusted base no longer trusts are withheld first, so a revocation
        # changes the counted rows and therefore the cache key.
        evidence = _counted_evidence(session, revision)
        newest = max(((item.created_at, item.id) for item in evidence), default=("", ""))
        key = f"{revision.id}:{int(discover)}:{len(evidence)}:{newest[0]}:{newest[1]}"
        cached = _check_rows_cache.get(key)
        if cached is not None:
            return cast(list[dict[str, Any]], cached)
        if discover:
            projection = _verification_projection(session, revision)
        else:
            from lemoncrow.pro.capabilities.review.verification import build_verification_projection

            projection = build_verification_projection(
                packet=read_packet_json(store, revision),
                evidence=evidence,
                revision=revision,
                revisions=store.list_revisions(session.id),
                browser_policy=_browser_policy(revision),
                changed_paths=_changed_paths(revision),
                web_routes=None,
                read_artifact=store.read_evidence_artifact,
            )
        rows = [dict(row) for row in projection.get("checks") or () if isinstance(row, Mapping)]
        _bounded_cache_put(_check_rows_cache, key, rows)
        return rows

    def _guidance_view_for(
        session: ReviewSession,
        revision: ReviewRevision,
        targets: Sequence[Any],
        units: Sequence[ReviewUnit],
        packet: Mapping[str, Any] | None,
    ) -> Any:
        from lemoncrow.pro.capabilities.review.guidance import build_guidance_view, load_guidance_records

        # A Snapshot shows only guidance imported at or before it; later
        # guidance must not rewrite what an earlier Snapshot said.
        numbers = {item.id: item.revision_number for item in store.list_revisions(session.id)}
        rows = [
            row
            for row in store.list_author_guidance(session.id)
            if numbers.get(row.revision_id, revision.revision_number) <= revision.revision_number
        ]
        records, unreadable = load_guidance_records(rows)
        return build_guidance_view(records, targets=targets, units=units, packet=packet, unreadable=unreadable)

    def _decision_states_for(
        session: ReviewSession,
        revision: ReviewRevision,
        targets: Sequence[Any],
        units: Sequence[ReviewUnit],
        view: Any,
        checks: Sequence[Mapping[str, Any]],
        packet: Mapping[str, Any] | None,
        *,
        historical: bool,
    ) -> tuple[Any, ...]:
        """HumanDecision states for one Snapshot (spec §5).

        A Snapshot sees only the events recorded at or before it, so an
        older Snapshot keeps showing what was decided then.
        """

        from lemoncrow.pro.capabilities.review.contracts import UNAVAILABLE, contract_fingerprint
        from lemoncrow.pro.capabilities.review.decisions import derive_decision_states

        numbers = {item.id: item.revision_number for item in store.list_revisions(session.id)}
        events = tuple(
            event
            for event in store.list_decision_events(session.id, reviewer_id=session.reviewer_id)
            if numbers.get(event.revision_id, revision.revision_number) <= revision.revision_number
        )
        if not events and not view.decisions and not any(item.kind == "decision" for item in view.conflicts):
            return ()
        # A contract in a file this Snapshot deletes or renames away is absent.
        gone = {
            str(row.get("old_path") if row.get("status") == "renamed" else row.get("path") or "")
            for row in (packet or {}).get("files") or ()
            if isinstance(row, Mapping) and row.get("status") in ("deleted", "renamed")
        } - {""}

        def contracts(reference: str) -> str:
            key = f"{revision.id}:{reference}"
            cached = _contract_cache.get(key)
            if cached is not None:
                return str(cached)
            value = contract_fingerprint(
                reference,
                lambda path: _snapshot_text(store, session.id, revision, resolved_repo, path, side="new"),
                deleted_paths=gone,
            )
            if value != UNAVAILABLE:
                # Unavailable is recomputed: it may return (spec §5 "unverified").
                _bounded_cache_put(_contract_cache, key, value)
            return value

        return derive_decision_states(
            guidance=view,
            targets=targets,
            units=units,
            events=events,
            checks=checks,
            contracts=contracts,
            annotations=store.list_annotations(session.id),
            current_revision_id=revision.id,
            historical=historical,
        )

    def _pinned_queue_ranks(session: ReviewSession, revision: ReviewRevision) -> Mapping[str, int]:
        """The ranks this reviewer keeps on this Snapshot (``review_priority.pinned_ranks``); empty: none pinned."""

        from lemoncrow.pro.capabilities.review.review_priority import pinned_ranks

        # The record changes only with the Snapshot's packet (one enrichment rewrites both).
        key = (revision.id, revision.packet_sha256)
        with _ranking_history_lock:
            history = _ranking_history_cache.get(key)
        if history is None:
            history = store.read_ranking_history(session.id, revision.id)
            with _ranking_history_lock:
                _ranking_history_cache[key] = history
                while len(_ranking_history_cache) > 32:
                    _ranking_history_cache.pop(next(iter(_ranking_history_cache)))
        if not history:
            return {}
        started = store.first_judgment_at(session.id, revision.id, reviewer_id=session.reviewer_id)
        return pinned_ranks(history, started)

    _ranking_history_cache: dict[tuple[str, str], tuple[tuple[str, dict[str, int]], ...]] = {}
    _ranking_history_lock = threading.Lock()

    def _with_plan_inputs(
        session: ReviewSession,
        revision: ReviewRevision,
        units: Sequence[ReviewUnit],
        packet: Mapping[str, Any] | None,
        base: Sequence[Any],
        *,
        historical: bool = False,
    ) -> tuple[tuple[Any, ...], Any, Mapping[str, Any], tuple[Any, ...]]:
        """The canonical queue with Routine proofs, plan questions and decisions.

        Every reader surface derives targets through here, so API, sidebar,
        Next and the Brief can never disagree about order or tier.
        """

        from lemoncrow.pro.capabilities.review.decisions import decision_reasons
        from lemoncrow.pro.capabilities.review.review_plan import (
            narrative_positions,
            question_reasons,
            review_questions,
        )
        from lemoncrow.pro.capabilities.review.review_priority import reprioritize

        proofs = _routine_proofs(session, revision, base, packet)
        view = _guidance_view_for(session, revision, base, units, packet)
        checks = _check_rows(session, revision, discover=False)
        states = _decision_states_for(session, revision, base, units, view, checks, packet, historical=historical)
        questions = question_reasons(
            review_questions(base, view, checks, handled_decisions={state.key for state in states})
        )
        for target_id, reasons in decision_reasons(states).items():
            bucket = questions.setdefault(target_id, [])
            bucket.extend(reason for reason in reasons if reason not in bucket)

        from lemoncrow.pro.capabilities.review.synthesis import (
            PROMPT_VERSION,
            SYNTHESIS_JSON_SCHEMA,
            SYNTHESIS_SCHEMA_VERSION,
            stored_questions,
        )

        scope, context, target_paths = _brief_intelligence_inputs(session, revision, view, base, checks)
        spec = _intelligence_spec(
            session,
            revision,
            capability="brief_synthesis",
            scope=scope,
            context=context,
            output_schema=SYNTHESIS_JSON_SCHEMA,
            prompt_version=PROMPT_VERSION,
            schema_version=SYNTHESIS_SCHEMA_VERSION,
            target_paths=target_paths,
        )
        row = spec.get("row")
        if row is not None and str(row.status) == "ready":
            try:
                payload = json.loads(row.result_json or "{}")
            except ValueError:
                payload = {}
            known = {target.target_id for target in base}
            ai = stored_questions(payload if isinstance(payload, Mapping) else {}, known)
            for target_id, reasons in question_reasons(ai).items():
                bucket = questions.setdefault(target_id, [])
                bucket.extend(reason for reason in reasons if reason not in bucket)

        # The author's narrative orders each tier; the legacy rank only breaks ties.
        narrative = narrative_positions(base, view)
        policy = _queue_policy_reasons(session, revision, base)
        # Stable slots: a reviewer who had started on this Snapshot before a re-rank
        # keeps the ranks they were working through; only the final order sees them.
        pinned = _pinned_queue_ranks(session, revision)
        if not proofs and not questions and not narrative and not policy and not pinned:
            return tuple(base), view, proofs, states
        ranked = (
            tuple(replace(target, attention_rank=pinned.get(target.unit_key, target.attention_rank)) for target in base)
            if pinned
            else base
        )
        return (
            reprioritize(
                ranked, routine_proofs=proofs, question_reasons=questions, narrative=narrative, policy_reasons=policy
            ),
            view,
            proofs,
            states,
        )

    def _related_source_text(revision: ReviewRevision, path: str) -> tuple[str, str]:
        """Source at the revision the reviewer is looking at, never today's disk.

        For a commit range the relevant context is the head tree. For staged or
        working-tree reviews an *out-of-patch* impact site was unchanged by the
        reviewed delta, so HEAD/base is the exact content that surrounded the
        change when the revision was captured. This keeps a context click from
        silently showing a later worktree edit.
        """

        from lemoncrow.pro.capabilities.review.gitdiff import _blob_from_tree, _decode, _open_repo, _tree_for_sha

        sha = revision.head_sha if revision.range_mode == "commit_range" else revision.base_sha
        if not sha:
            return "", "the reviewed revision has no committed tree for related source"
        try:
            repo = _open_repo(resolved_repo)
            tree = _tree_for_sha(repo, sha)
            data, reason = _blob_from_tree(repo, tree, path)
        except (ValueError, OSError):
            return "", "the related source could not be read at the reviewed revision"
        if data is None:
            return "", reason or "the related source could not be read at the reviewed revision"
        return _decode(data), ""

    def _discarded_payload(session: ReviewSession, revision: ReviewRevision) -> list[dict[str, Any]]:
        """The reviewer's verdicts that were destroyed getting to *revision*.

        Read out of ``review_discarded_marks`` rather than off the in-process
        ``RefreshResult``, because a destroyed verdict is a property of the
        revision and not of the request that noticed it. Reconciliation runs
        once per revision, so the refresh response carried the names for exactly
        one caller: a second *Re-read tree* answered ``"discarded": []`` and a
        page reload carried no discard at all, which made an approval this tool
        deleted disappear from the browser the moment the reviewer moved. Same
        rows, same words, as the terminal.

        Scoped to the reviewer's own window -- from the revision they last
        recorded a verdict against to the one being rendered -- rather than to
        the single revision reconciliation stood on when it deleted the mark.
        The narrow reading survived one revision: the next agent edit moved the
        current revision on, ``GET /api/reviews/{id}`` and ``POST /refresh`` both
        answered ``"discarded": []`` while ``refreshed.removed`` still named the
        unit, and the browser's "your reviewed verdict went with it" annotation
        -- built from this list -- silently dropped off. Same window as the
        frontier and as the terminal; see
        :meth:`ReviewStore.discarded_marks_since` for when it stops being news.
        """

        baseline = baseline_revision(store, session.id, reviewer_id=session.reviewer_id)
        return [
            {
                "unit_key": record.unit_key,
                "kind": record.kind,
                "state": record.state,
                "label": unit_label(record) or record.unit_key,
                "reason": record.reason,
            }
            for record in store.discarded_marks_since(
                session.id,
                reviewer_id=session.reviewer_id,
                after_revision_number=baseline.revision_number if baseline is not None else 0,
                absent_from_revision_id=revision.id,
            )
        ]

    def _overview(
        session: ReviewSession,
        revision: ReviewRevision | None = None,
        *,
        historical: bool = False,
        startup: bool = False,
    ) -> dict[str, Any]:
        revision = revision or _latest(session)
        units = store.list_units(revision.id)
        marks = (
            _marks_visible_at(session, revision)
            if historical
            else store.list_marks(session.id, reviewer_id=session.reviewer_id)
        )
        packet = read_packet_json(store, revision)
        annotations = _annotations_at(session, revision, historical=historical)
        frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            revision,
            units,
            marks,
            annotations=annotations,
        )
        from lemoncrow.pro.capabilities.review.targets import (
            build_review_outline,
            outline_payload,
            progress_payload,
            review_progress,
        )

        targets = _derive_targets(session, revision, units, frontier.entries, packet, annotations)
        target_progress = review_progress(targets)
        startup_compact = bool(
            startup and not historical and target_progress.target_count > LARGE_REVIEW_TARGET_THRESHOLD
        )
        degraded = sorted(set(revision.degraded) | ({"packet_unavailable"} if packet is None else set()))
        stats = packet.get("stats") if packet is not None and isinstance(packet.get("stats"), dict) else {}
        artifacts = _evidence_at(session, revision, historical=historical)
        current_artifacts = tuple(item for item in artifacts if _evidence_is_current(item, revision))
        stale_artifact_count = len(artifacts) - len(current_artifacts)

        groups: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        chapters: dict[str, Any] = {}
        target_outline: Sequence[Any] = ()
        brief: dict[str, Any] | None = None
        story_labels: Sequence[str] = ()
        story_more = 0
        if not startup_compact:
            groups, counts = build_groups(units, frontier.entries, packet, targets=targets)
            target_outline = build_review_outline(targets)
            from lemoncrow.pro.capabilities.review.chapters import build_chapter_lenses, change_story_labels

            file_rows = [row for group in groups for row in group["rows"]]
            chapters = build_chapter_lenses(
                file_rows,
                packet,
                repo_root=resolved_repo,
                base_sha=revision.base_sha,
                head_sha=revision.head_sha,
                targets=targets,
            )
            from lemoncrow.pro.capabilities.review.preparation import build_review_brief

            brief = build_review_brief(
                packet=packet,
                groups=groups,
                chapters=chapters,
                annotations=annotations,
                artifacts=current_artifacts,
                revision_id=revision.id,
                stale_artifact_count=stale_artifact_count,
                targets=targets,
            )
            if revision.title:
                brief = {**brief, "summary": revision.title}
            intent_chapters = chapters.get("intent", [])
            story_labels, story_more = change_story_labels(intent_chapters) if len(file_rows) >= 8 else ((), 0)
            if len(intent_chapters) <= 1:
                story_labels, story_more = (), 0
        revision_state = _revision_state_payload(session)
        published_ids = store.published_snapshot_ids(session.id)
        return {
            "session": _session_payload(session, store=store, short_refs=short_references),
            "change": {
                **_change_context_payload(store, session, include_stack=not historical),
                # Phase 9R item 42: linked members are counted here; the set itself is /change-set.
                "change_set": _change_set_summary(session),
            },
            # `session.repo_root` is an opaque durable identity on server-backed
            # Review. Only a trusted local checkout binding may name a real
            # filesystem path for workspace actions (copy path, open in editor).
            "workspace_root": str(resolved_source_repo) if source_refresh_enabled else "",
            "revision": {
                **_revision_payload(revision, store=store, short_refs=short_references),
                "is_published_snapshot": revision.id in published_ids,
            },
            "revision_count": len(store.list_revisions(session.id)),
            "historical": historical,
            "latest_revision_number": _display_revision_number(_latest(session)),
            "latest_revision": {
                **_revision_payload(_latest(session), store=store, short_refs=short_references),
                "is_published_snapshot": _latest(session).id in published_ids,
            },
            "last_reviewed_revision_number": revision_state["last_reviewed_revision_number"],
            "last_commented_revision_number": revision_state["last_commented_revision_number"],
            "default_base_revision_number": revision_state["default_base_revision_number"],
            "default_target_revision_number": revision_state["default_target_revision_number"],
            "historical_judgment_complete": (
                not historical
                or bool(store.list_mark_events(session.id, reviewer_id=session.reviewer_id))
                or not store.list_marks(session.id, reviewer_id=session.reviewer_id)
            ),
            "packet_available": packet is not None,
            "stats": stats,
            "title": session.title or (packet.get("title") if packet is not None else ""),
            "change_story": list(story_labels),
            "change_story_more": story_more,
            "brief": brief,
            "startup_compact": startup_compact,
            "policy": _future_policy_projection(session, revision),
            "provenance": _provenance_for(packet, ""),
            "attributions": [
                {
                    "scope": item.scope,
                    "scope_key": item.scope_key,
                    "host": item.host,
                    "session_id": item.session_id,
                    "role": item.role,
                    "model": item.model,
                    "certainty": item.certainty,
                    "method": item.method,
                }
                for item in store.list_revision_attributions(revision.id)
            ],
            "evidence": _evidence(packet, current_artifacts, revision_id=revision.id),
            "journal": (
                [dict(item) for item in packet.get("journal", []) if isinstance(item, dict)]
                if packet is not None
                else []
            ),
            "degraded": [{"name": name, "note": degraded_note(name)} for name in degraded],
            "groups": groups,
            "group_counts": counts,
            "chapters": chapters,
            # R20 reader projection.  The current browser still consumes the
            # legacy file groups above; these additive fields let R21/R22 move
            # over without ever treating raw overlapping units as progress.
            "progress": progress_payload(target_progress),
            "movement": (
                None
                if historical or startup_compact
                else _movement_projection(session, revision, [{"unit_key": item.unit_key} for item in targets])[1]
            ),
            "outline": [] if startup_compact else [outline_payload(item) for item in target_outline],
            "unit_count": len(units),
            "target_count": target_progress.target_count,
            "mark_count": len(marks),
            # On the overview rather than only on a refresh response: a reviewer
            # who reloaded the page has not un-destroyed their verdict.
            "discarded": (
                [
                    {
                        "unit_key": record.unit_key,
                        "kind": record.kind,
                        "state": record.state,
                        "label": unit_label(record) or record.unit_key,
                        "reason": record.reason,
                    }
                    for record in store.discarded_marks_on(revision.id, reviewer_id=session.reviewer_id)
                ]
                if historical
                else _discarded_payload(session, revision)
            ),
        }

    @router.get("/reviews")
    def _list_reviews(status_filter: str = "open") -> dict[str, Any]:
        """List durable reviews for this repository without advancing any review.

        ``all`` is explicit rather than the default because the local Reader historically
        exposed only active work.  Hosted Review will project the same records into an
        organization inbox, while this loopback workspace remains repository-confined.
        """

        if status_filter not in {"open", "finished", "archived", "all"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="status_filter must be open, finished, archived, or all",
            )
        rows: list[dict[str, Any]] = []
        sessions = store.list_sessions(status="" if status_filter == "all" else status_filter, limit=500)
        references = store.session_references()
        attachments = store.hosted_attachments() if store.hosted_attachment_supported() else {}
        for session in sessions:
            if session.repo_root != str(resolved_repo):
                continue
            revision = store.latest_revision(session.id)
            annotations = store.list_annotations(session.id)
            payload = _session_payload(
                _as_acting_reviewer(session),
                store=store,
                references=references,
                short_refs=short_references,
                attachments=attachments,
            )
            payload["revision_number"] = _display_revision_number(revision) if revision is not None else None
            payload["revision_id"] = revision.id if revision is not None else ""
            payload["comment_count"] = len(annotations)
            payload["open_comment_count"] = sum(
                1 for item in annotations if item.state in {"open", "orphaned"} and not item.parent_id
            )
            if session.status == "archived":
                # A provider cancellation owed by the discard stays visible until confirmed.
                payload["owed_cancellation"] = automation_hooks.owed(session.id)
            rows.append(payload)
        return {"reviews": rows, "repo_root": str(resolved_repo), "status_filter": status_filter}

    @router.delete("/reviews/{review_id}")
    async def _delete_archived_review(review_id: str, request: Request) -> dict[str, Any]:
        """Permanent deletion: separate privilege, exact identity confirmation, audited first."""

        from starlette.concurrency import run_in_threadpool

        session = _session(review_id)
        body: dict[str, Any] = {}
        if (await request.body()).strip():
            body = await _json_object(request)
        # An attached Review shows hosted's id (RC5): typing exactly it names this local Review.
        attachment = store.get_hosted_attachment(session.id) if store.hosted_attachment_supported() else None
        if attachment is not None and str(body.get("confirm_identity") or "") in {
            attachment.hosted_review_id,
            hosted_reference(attachment.hosted_review_id),
        }:
            body = {**body, "confirm_identity": session.id}

        def _delete(review_id: str, tombstone: Any) -> bool:
            try:
                return store.delete_archived_session(review_id, tombstone=tombstone)
            except HostedAttachmentConflict as exc:  # unsent hosted work: an audited 409 refusal
                raise ValueError(f"{exc.code}: {exc.message}") from exc

        deleted = await run_in_threadpool(automation_hooks.hard_delete, session, body, _delete)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such review")
        tombstones = await run_in_threadpool(store.deletion_tombstones, session.id)
        return {
            "deleted": True,
            "review_id": session.id,
            "tombstone": tombstones[-1].to_wire() if tombstones else None,
        }

    @router.get("/reviews/{review_id}/preparation")
    def _get_review_preparation(review_id: str, preparation_id: str) -> dict[str, Any]:
        """Cheap startup status for a Reader opened before packet capture completes."""

        session = _session(review_id)
        from lemoncrow.pro.capabilities.review.workspace import read_review_preparation

        row = read_review_preparation(store.root, session.id, preparation_id)
        if row is None:
            return {"state": "ready", "detail": ""}
        return {
            "state": str(row.get("stage") or "preparing"),
            "detail": str(row.get("detail") or ""),
        }

    @router.get("/revisions/{revision_id}")
    def _get_revision_locator(revision_id: str) -> dict[str, Any]:
        session, revision = _revision(revision_id)
        return {
            "review": _session_payload(session, store=store, short_refs=short_references),
            "revision": _revision_payload(revision, store=store, short_refs=short_references),
        }

    # lc-debt: bounded FIFO per API instance; an LRU if many Reviews are open at once.
    _journal_packet_memo: OrderedDict[tuple[str, str], _JournalPacketFacts] = OrderedDict()
    _journal_packet_lock = threading.Lock()

    def _journal_packet_facts(revision: ReviewRevision) -> _JournalPacketFacts | None:
        """Captured journal rows, provenance and changed paths of one stored packet.

        The Reader's live-state long-poll asks for these every 0.75 s per open
        tab; the packet behind a revision never changes, so it is decoded once.
        """

        key = (revision.id, str(revision.packet_sha256 or ""))
        with _journal_packet_lock:
            hit = _journal_packet_memo.get(key)
        if hit is not None:
            return hit
        packet = read_packet_json(store, revision)
        if not isinstance(packet, dict):
            return None
        captured = tuple(dict(item) for item in packet.get("journal", []) if isinstance(item, dict))
        try:
            from lemoncrow.pro.capabilities.review.models import review_packet_from_dict

            parsed = review_packet_from_dict(packet)
            facts: _JournalPacketFacts = (captured, parsed.provenance, tuple(item.path for item in parsed.files))
        except (TypeError, ValueError):
            facts = (captured, None, None)
        if key[1]:
            with _journal_packet_lock:
                _journal_packet_memo[key] = facts
                while len(_journal_packet_memo) > 32:
                    _journal_packet_memo.popitem(last=False)
        return facts

    def _journal_projection(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        include_live: bool,
    ) -> dict[str, Any]:
        """Structured observable author context, pinned to one Review revision."""

        facts = _journal_packet_facts(revision)
        if facts is None:
            return {
                "available": False,
                "reason": "review packet is unavailable",
                "review_id": session.id,
                "revision_id": revision.id,
                "revision_number": _display_revision_number(revision),
                "activity": [],
                "entries": [],
            }
        captured = [dict(item) for item in facts[0]]
        provenance = facts[1]
        changed_paths = facts[2]
        exact = bool(
            provenance is not None and provenance.certainty == "exact" and provenance.session_id and provenance.host
        )

        state: dict[str, Any] = {
            "available": False,
            "reason": "no exact author session is attached to this Review" if not exact else "",
            "session_id": "" if provenance is None else provenance.session_id or "",
            "host": "" if provenance is None else provenance.host or "",
            "task": "",
            "status": "",
            "updated_at": "",
            "plan": [],
            "verified_facts": [],
            "hypotheses_tried": [],
            "hypotheses_rejected": [],
            "open_questions": [],
            "blockers": [],
            "next_validation": "",
            "workflow_state": {},
            "plan_review": {},
            "task_progress": {},
            "tools_called": [],
            "commands_run": [],
            "tests_run": [],
            "files_touched": [],
            "activity": [],
        }
        if include_live and live_journal_enabled and exact and changed_paths is not None and provenance is not None:
            try:
                from lemoncrow.pro.capabilities.review.journal import load_review_journal
            except ModuleNotFoundError as exc:
                if not module_absent(exc, REVIEW_JOURNAL):
                    raise
                state["reason"] = "live author-session journal is not in this build"
            else:
                try:
                    state = load_review_journal(
                        resolved_source_store,
                        session_id=provenance.session_id or "",
                        host=provenance.host or "",
                        changed_paths=list(changed_paths),
                        repo_root=resolved_source_repo,
                    )
                except (OSError, TypeError, ValueError):
                    state["reason"] = "live author-session journal is unavailable"

        activity: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for raw in [*captured, *(state.get("activity") or [])]:
            if not isinstance(raw, Mapping):
                continue
            row = {
                "id": str(raw.get("id") or ""),
                "kind": str(raw.get("kind") or ""),
                "summary": str(raw.get("summary") or ""),
                "path": str(raw.get("path") or ""),
                "status": str(raw.get("status") or "").upper(),
                "detail": str(raw.get("detail") or ""),
                "at": str(raw.get("at") or ""),
                "source": str(raw.get("source") or "run_ledger"),
            }
            key = (row["kind"], row["summary"], row["path"], row["status"], row["at"])
            if not row["summary"] or key in seen:
                continue
            seen.add(key)
            activity.append(row)
        activity.sort(key=lambda item: (item["at"], item["id"]))

        result = dict(state)
        result.update(
            {
                "review_id": session.id,
                "revision_id": revision.id,
                "revision_number": _display_revision_number(revision),
                "session_id": "" if provenance is None else provenance.session_id or "",
                "host": "" if provenance is None else provenance.host or "",
                "activity": activity,
                # Compatibility alias for the first journal API draft. New
                # Reader code consumes ``activity``; older clients/tests may
                # still read ``entries``. Both are the same sanitized rows.
                "entries": activity,
            }
        )
        result["available"] = bool(exact and (state.get("available") or captured))
        if result["available"] and captured and not state.get("available"):
            result["reason"] = (
                "showing author context captured with this revision"
                if not live_journal_enabled
                else "live author session is unavailable; showing context captured with this revision"
            )
        elif exact and not activity and not result.get("reason"):
            result["reason"] = (
                "live author-session journal is not available in this deployment"
                if not live_journal_enabled
                else "no observable review-relevant author activity is recorded yet"
            )
        return result

    async def _resolve_ask_surface_context(
        session: ReviewSession,
        revision: ReviewRevision,
        raw_context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Resolve a typed Ask surface selection against server-owned Review facts.

        The browser contributes only stable identity.  Source locations, graph
        evidence and exactness are reconstructed from the requested immutable
        Snapshot so a stale or forged Map payload cannot silently become Ask
        grounding.  The first result is the compact context persisted with the
        message, the second is bounded answerer evidence, and the third is an
        optional exact Snapshot source scope.
        """

        if raw_context is None:
            return {}, {}, {}
        if not isinstance(raw_context, Mapping):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ask context must be an object")
        kind = str(raw_context.get("kind") or "").strip()
        if kind not in {"review_map_node", "review_map_edge"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported Ask context kind")
        projection = str(raw_context.get("projection") or "change_map").strip()
        if projection not in {"change_map", "architecture_delta"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Review Map context projection")
        if str(raw_context.get("review_id") or "") != session.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ask context belongs to another Change")
        if str(raw_context.get("revision_id") or "") != revision.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ask context belongs to another Snapshot")

        level = str(raw_context.get("level") or "architecture")
        if level not in {"architecture", "file", "symbol"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Review Map context level")
        focus_node_id = str(raw_context.get("focus_node_id") or "").strip()
        identity_key = "node_id" if kind == "review_map_node" else "edge_id"
        identity = str(raw_context.get(identity_key) or "").strip()
        if not identity:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Ask context requires {identity_key}")
        if level in {"symbol", "file"} and not focus_node_id:
            if kind == "review_map_node" and str(raw_context.get("exactness") or "") == "snapshot":
                focus_node_id = identity
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"{level}-level Review Map context is missing its Snapshot focus",
                )

        latest = _latest(session)
        map_payload = await _review_map_payload(
            session,
            revision,
            historical=revision.id != latest.id,
            level=level,
            node_id=focus_node_id if level in {"symbol", "file"} else "",
            projection=projection,
        )
        graph = map_payload.get("graph") if isinstance(map_payload, Mapping) else None
        if not isinstance(graph, Mapping):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Review Map context is unavailable")
        rows_key = "nodes" if kind == "review_map_node" else "edges"
        rows = graph.get(rows_key)
        candidates = [item for item in rows if isinstance(item, Mapping)] if isinstance(rows, list) else []
        selected = next((item for item in candidates if str(item.get("id") or "") == identity), None)
        recovered_legacy_node = False
        if selected is None and kind == "review_map_node":
            supplied_path = str(raw_context.get("path") or "").strip()
            if supplied_path:
                selected = next(
                    (item for item in candidates if str(item.get("path") or "") == supplied_path),
                    None,
                )
                recovered_legacy_node = selected is not None
            if selected is None and identity.startswith("component:"):
                component = identity.removeprefix("component:")
                component_matches = [item for item in candidates if str(item.get("component") or "") == component]
                if len(component_matches) == 1:
                    selected = component_matches[0]
                    recovered_legacy_node = True
        if selected is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Review Map selection no longer resolves for this Snapshot/context generation",
            )
        canonical_raw = selected.get("selection")
        if not isinstance(canonical_raw, Mapping):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Review Map selection identity is unavailable"
            )
        canonical = dict(canonical_raw)
        identity_keys = ("kind", "review_id", "revision_id", "level")
        for key in (*identity_keys, *((identity_key,) if not recovered_legacy_node else ())):
            if str(canonical.get(key) or "") != str(raw_context.get(key) or ""):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Review Map selection identity changed"
                )
        canonical_projection = str(canonical.get("projection") or "change_map")
        if canonical_projection != projection:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Review Map selection projection changed")
        canonical_focus = str(canonical.get("focus_node_id") or "")
        supplied_focus = str(raw_context.get("focus_node_id") or "")
        if supplied_focus and supplied_focus != canonical_focus:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Review Map selection context changed")
        for key in (
            "exactness",
            "source_id",
            "target_id",
            "relation",
            "delta_status",
            "architecture_source",
            "architecture_comparison",
        ):
            supplied = str(raw_context.get(key) or "")
            if supplied and supplied != str(canonical.get(key) or ""):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Review Map selection context changed")

        node_rows = graph.get("nodes")
        nodes = (
            {
                str(item.get("id") or ""): item
                for item in node_rows
                if isinstance(item, Mapping) and str(item.get("id") or "")
            }
            if isinstance(node_rows, list)
            else {}
        )
        edge_rows = graph.get("edges")
        edges = [item for item in edge_rows if isinstance(item, Mapping)] if isinstance(edge_rows, list) else []

        def compact_node(item: Mapping[str, Any]) -> dict[str, Any]:
            symbols = item.get("symbols")
            return {
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or ""),
                "kind": str(item.get("kind") or ""),
                "path": str(item.get("path") or ""),
                "line": int(item.get("line") or 0),
                "end_line": int(item.get("end_line") or 0),
                "status": str(item.get("status") or ""),
                "change": str(item.get("change") or ""),
                "exactness": str(item.get("exactness") or ""),
                "delta_status": str(item.get("delta_status") or ""),
                "architecture_provenance": (
                    dict(item.get("architecture_provenance") or {})
                    if isinstance(item.get("architecture_provenance"), Mapping)
                    else {}
                ),
                "target_ids": [str(value) for value in list(item.get("target_ids") or [])[:12]],
                "important_public_contract": bool(item.get("important_public_contract")),
                "boundaries": [str(value) for value in list(item.get("boundaries") or [])[:8]],
                "uncertain": bool(item.get("uncertain")),
                "uncertainty": str(item.get("uncertainty") or ""),
                "caller_count": item.get("caller_count"),
                "usage_count": item.get("usage_count"),
                "centrality_rank": item.get("centrality_rank"),
                "changed_file_count": item.get("changed_file_count"),
                "impacted_file_count": item.get("impacted_file_count"),
                "paths": [str(value) for value in list(item.get("paths") or [])[:16]],
                "symbols": [dict(value) for value in list(symbols or [])[:8] if isinstance(value, Mapping)],
            }

        def compact_edge(item: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "id": str(item.get("id") or ""),
                "source": str(item.get("source") or ""),
                "target": str(item.get("target") or ""),
                "relation": str(item.get("kind") or ""),
                "exactness": str(item.get("exactness") or ""),
                "delta_status": str(item.get("delta_status") or ""),
                "base_weight": int(item.get("base_weight") or 0),
                "snapshot_weight": int(item.get("snapshot_weight") or 0),
                "architecture_provenance": (
                    dict(item.get("architecture_provenance") or {})
                    if isinstance(item.get("architecture_provenance"), Mapping)
                    else {}
                ),
                "weight": int(item.get("weight") or 0),
                "uncertain": bool(item.get("uncertain")),
                "evidence": [
                    dict(value) for value in list(item.get("evidence") or [])[:8] if isinstance(value, Mapping)
                ],
            }

        selected_nodes: list[Mapping[str, Any]] = []
        connected_edges: list[Mapping[str, Any]] = []
        if kind == "review_map_node":
            selected_nodes.append(selected)
            for edge in edges:
                if str(edge.get("source") or "") == identity or str(edge.get("target") or "") == identity:
                    connected_edges.append(edge)
                    other = (
                        str(edge.get("target") or "")
                        if str(edge.get("source") or "") == identity
                        else str(edge.get("source") or "")
                    )
                    if other in nodes:
                        selected_nodes.append(nodes[other])
                if len(connected_edges) >= 16:
                    break
        else:
            connected_edges.append(selected)
            for endpoint in (str(selected.get("source") or ""), str(selected.get("target") or "")):
                if endpoint in nodes:
                    selected_nodes.append(nodes[endpoint])

        unique_nodes: list[Mapping[str, Any]] = []
        seen_nodes: set[str] = set()
        for item in selected_nodes:
            node_id = str(item.get("id") or "")
            if node_id and node_id not in seen_nodes:
                seen_nodes.add(node_id)
                unique_nodes.append(item)
            if len(unique_nodes) >= 16:
                break

        snapshot_paths: list[str] = []
        for item in unique_nodes:
            if str(item.get("exactness") or "") != "snapshot":
                continue
            for value in [str(item.get("path") or ""), *[str(v) for v in list(item.get("paths") or [])]]:
                if value and value not in snapshot_paths:
                    snapshot_paths.append(value)
                if len(snapshot_paths) >= 12:
                    break

        source_scope: dict[str, Any] = {}
        if kind == "review_map_node" and str(selected.get("exactness") or "") == "snapshot":
            selected_side = str(canonical.get("side") or "new")
            source_scope = {
                "path": str(selected.get("path") or ""),
                "symbol": (
                    str(selected.get("qualified_name") or selected.get("label") or "") if level == "symbol" else ""
                ),
                "side": selected_side if selected_side in {"new", "old", "additions", "deletions"} else "new",
                "start_line": int(selected.get("line") or 0),
                "end_line": int(selected.get("end_line") or selected.get("line") or 0),
            }
        elif kind == "review_map_edge" and str(selected.get("exactness") or "") == "snapshot":
            map_endpoint = next(
                (
                    item
                    for item in unique_nodes
                    if str(item.get("exactness") or "") == "snapshot" and str(item.get("path") or "")
                ),
                None,
            )
            if map_endpoint is not None:
                endpoint_selection = map_endpoint.get("selection")
                endpoint_side = (
                    str(endpoint_selection.get("side") or "new") if isinstance(endpoint_selection, Mapping) else "new"
                )
                source_scope = {
                    "path": str(map_endpoint.get("path") or ""),
                    "symbol": (
                        str(map_endpoint.get("qualified_name") or map_endpoint.get("label") or "")
                        if level == "symbol"
                        else ""
                    ),
                    "side": endpoint_side if endpoint_side in {"new", "old", "additions", "deletions"} else "new",
                    "start_line": int(map_endpoint.get("line") or 0),
                    "end_line": int(map_endpoint.get("end_line") or map_endpoint.get("line") or 0),
                }

        raw_snapshot = map_payload.get("snapshot")
        snapshot: Mapping[str, Any] = raw_snapshot if isinstance(raw_snapshot, Mapping) else {}
        raw_surrounding = map_payload.get("context")
        surrounding: Mapping[str, Any] = raw_surrounding if isinstance(raw_surrounding, Mapping) else {}
        label = (
            str(selected.get("label") or selected.get("kind") or identity)
            if kind == "review_map_node"
            else str(selected.get("kind") or "relationship")
        )
        persisted = {"selection": canonical, "label": label}
        raw_architecture_delta = map_payload.get("architecture_delta")
        architecture_delta = raw_architecture_delta if isinstance(raw_architecture_delta, Mapping) else {}
        raw_architecture_summary = architecture_delta.get("summary")
        architecture_summary = dict(raw_architecture_summary) if isinstance(raw_architecture_summary, Mapping) else {}
        answer_context = {
            "kind": kind,
            "projection": projection,
            "selection": canonical,
            "selected": compact_node(selected) if kind == "review_map_node" else compact_edge(selected),
            "nodes": [compact_node(item) for item in unique_nodes],
            "edges": [compact_edge(item) for item in connected_edges[:16]],
            "snapshot_paths": snapshot_paths,
            "snapshot": {
                "tree_fingerprint": str(snapshot.get("tree_fingerprint") or ""),
                "source_fingerprint": str(snapshot.get("source_fingerprint") or ""),
                "degraded": [str(value) for value in list(snapshot.get("degraded") or [])[:16]],
                "verification": (
                    dict(snapshot.get("verification") or {})
                    if isinstance(snapshot.get("verification"), Mapping)
                    else {}
                ),
            },
            "surrounding_graph": {
                "source": str(surrounding.get("source") or "unavailable"),
                "exact_for_snapshot": bool(surrounding.get("exact_for_snapshot")),
                "snapshot_head_matches_checkout": bool(surrounding.get("snapshot_head_matches_checkout")),
                "note": str(surrounding.get("note") or ""),
                "index": dict(surrounding.get("index") or {}) if isinstance(surrounding.get("index"), Mapping) else {},
            },
            "graph_truncated": bool(graph.get("truncated")),
            "architecture_delta": (
                {
                    "status": str(architecture_delta.get("status") or ""),
                    "digest": str(architecture_delta.get("digest") or ""),
                    "summary": architecture_summary,
                    "reason": str(architecture_delta.get("reason") or ""),
                }
                if projection == "architecture_delta"
                else {}
            ),
        }
        return persisted, answer_context, source_scope

    def _ask_delivery_capability(
        revision: ReviewRevision,
        *,
        path: str,
        persisted_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        selection = persisted_context.get("selection")
        if isinstance(selection, Mapping) and str(selection.get("kind") or "").startswith("review_map_"):
            if str(selection.get("exactness") or "") != "snapshot":
                return {
                    "supported": False,
                    "host": "",
                    "session_id": "",
                    "target_ref": "",
                    "label": "",
                    "reason": (
                        "current-index Review Map context is read-only; open Snapshot-exact source "
                        "before sending a code change to the author"
                    ),
                }
        return _delivery_capability(revision, path=path)

    def _ask_message_payload(message: Any, revision_numbers: Mapping[str, int]) -> dict[str, Any]:
        try:
            context = json.loads(message.context_json or "{}")
        except (TypeError, json.JSONDecodeError):
            context = {}
        try:
            references = json.loads(message.references_json or "[]")
        except (TypeError, json.JSONDecodeError):
            references = []
        try:
            action = json.loads(message.action_json or "{}")
        except (TypeError, json.JSONDecodeError):
            action = {}
        handoff = store.get_ask_handoff(message.id)
        if isinstance(action, dict) and handoff is not None:
            action = {
                **action,
                "handoff": {
                    "state": handoff.state,
                    "target_ref": handoff.target_ref,
                    "remote_ref": handoff.remote_ref,
                    "last_error": handoff.last_error,
                    "updated_at": handoff.updated_at,
                },
            }
        return {
            "id": message.id,
            "review_id": message.review_id,
            "revision_id": message.revision_id,
            "revision_number": revision_numbers.get(message.revision_id),
            "role": message.role,
            "intent": message.intent,
            "body": message.body,
            "scope": {
                "target_unit_key": message.target_unit_key,
                "path": message.path,
                "symbol": message.symbol,
                "side": message.side,
                "start_line": message.start_line,
                "end_line": message.end_line,
            },
            "context": context if isinstance(context, dict) else {},
            "references": references if isinstance(references, list) else [],
            "action": action if isinstance(action, dict) else {},
            "created_at": message.created_at,
        }

    _LEGACY_ASK_CONVERSATION_ID = "legacy"

    def _ask_messages_payload(session: ReviewSession, conversation_id: str | None = None) -> list[dict[str, Any]]:
        revision_numbers = {item.id: _display_revision_number(item) for item in store.list_revisions(session.id)}
        return [
            _ask_message_payload(item, revision_numbers)
            for item in store.list_ask_messages(session.id, conversation_id=conversation_id)
        ]

    def _ask_origin_from_message(message: Any, revision_numbers: Mapping[str, int]) -> dict[str, Any]:
        try:
            context = json.loads(message.context_json or "{}")
        except (TypeError, json.JSONDecodeError):
            context = {}
        selection = context.get("selection") if isinstance(context, Mapping) else None
        origin: dict[str, Any] = {
            "kind": (
                "map"
                if isinstance(selection, Mapping)
                else ("selection" if (message.path or message.symbol or message.start_line) else "review")
            ),
            "revision_number": revision_numbers.get(message.revision_id),
        }
        if message.path:
            origin["path"] = message.path
        if message.symbol:
            origin["symbol"] = message.symbol
        if message.start_line:
            origin["start_line"] = message.start_line
        if message.end_line:
            origin["end_line"] = message.end_line
        if isinstance(context, Mapping) and context.get("label"):
            origin["label"] = str(context.get("label"))
        return origin

    def _ask_conversation_payload(
        record: Any, messages: Sequence[Any], revision_numbers: Mapping[str, int]
    ) -> dict[str, Any]:
        try:
            origin = json.loads(record.origin_json or "{}")
        except (TypeError, json.JSONDecodeError):
            origin = {}
        first_human = next((item for item in messages if item.role == "human"), None)
        title = str(record.title or "").strip()
        if not title and first_human is not None:
            title = " ".join(first_human.body.split())[:160]
        if not isinstance(origin, dict) or not origin:
            origin = (
                _ask_origin_from_message(first_human, revision_numbers)
                if first_human is not None
                else {"kind": "review"}
            )
        return {
            "id": record.id,
            "title": title or "New conversation",
            "origin": origin,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "message_count": len(messages),
        }

    def _ask_conversation_summaries(session: ReviewSession) -> list[dict[str, Any]]:
        revision_numbers = {item.id: _display_revision_number(item) for item in store.list_revisions(session.id)}
        rows: list[dict[str, Any]] = []
        for record in store.list_ask_conversations(session.id):
            messages = store.list_ask_messages(session.id, conversation_id=record.id)
            rows.append(_ask_conversation_payload(record, messages, revision_numbers))
        legacy = store.list_ask_messages(session.id, conversation_id="")
        if legacy:
            first_human = next((item for item in legacy if item.role == "human"), legacy[0])
            rows.append(
                {
                    "id": _LEGACY_ASK_CONVERSATION_ID,
                    "title": " ".join(first_human.body.split())[:160] or "Previous conversation",
                    "origin": _ask_origin_from_message(first_human, revision_numbers),
                    "created_at": legacy[0].created_at,
                    "updated_at": legacy[-1].created_at,
                    "message_count": len(legacy),
                    "legacy": True,
                }
            )
        rows.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("id") or "")), reverse=True)
        return rows

    def _ask_origin_payload(
        *,
        revision_number: int,
        path: str,
        symbol: str,
        start_line: int,
        end_line: int,
        persisted_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        selection = persisted_context.get("selection") if isinstance(persisted_context, Mapping) else None
        origin: dict[str, Any] = {
            "kind": (
                "map"
                if isinstance(selection, Mapping)
                else ("selection" if (path or symbol or start_line) else "review")
            ),
            "revision_number": revision_number,
        }
        if path:
            origin["path"] = path
        if symbol:
            origin["symbol"] = symbol
        if start_line:
            origin["start_line"] = start_line
        if end_line:
            origin["end_line"] = end_line
        label = persisted_context.get("label") if isinstance(persisted_context, Mapping) else None
        if label:
            origin["label"] = str(label)
        return origin

    def _refuse_agent_cli_driver(
        backend: str, model: str, surface: str, *, session: ReviewSession, revision: ReviewRevision
    ) -> None:
        """400/409 when the runtime refuses this route for this Review on this host.

        UX only: ``intelligence_runtime.resolve_driver_model`` is the authority
        every surface passes through; this translates its refusal for a client.
        An agent CLI where agent CLIs do not run is 400; a disabled or invalid
        intelligence policy, or a driver/model the operator policy pinned to
        something else, is 409.
        """

        from lemoncrow.pro.capabilities.review.intelligence_runtime import (
            AgentCliDriverRefused,
            IntelligenceDisabled,
            RouteOverrideRefused,
        )

        try:
            _resolve_route(session, revision, backend or None, model or None)
        except AgentCliDriverRefused as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{surface} on this server uses a model route; agent CLI drivers do not run here",
            ) from exc
        except (IntelligenceDisabled, RouteOverrideRefused) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"{surface} is unavailable: {exc}"
            ) from exc
        except Exception:
            return  # any other unresolvable route fails where it did before

    @router.get("/reviews/{review_id}/ask")
    async def _get_review_ask(review_id: str, conversation_id: str | None = None) -> dict[str, Any]:
        _require_ask_enabled()
        session = _session(review_id)
        latest = _latest(session)
        from lemoncrow.core.capabilities.providers.config import load_providers_config
        from lemoncrow.core.capabilities.providers.discovery import _fetch_ollama, discover_models
        from lemoncrow.pro.capabilities.review.ask import discover_opencode_models
        from lemoncrow.pro.capabilities.review.intelligence_policy import ask_route_options
        from lemoncrow.pro.capabilities.review.intelligence_runtime import AGENT_CLI_DRIVERS, agent_cli_drivers_allowed

        # The route this Review's effective policy resolves, never the environment alone.
        route_options = ask_route_options(_route_policy(session, latest))
        driver = route_options["driver"]
        configured_model = route_options["model"]
        try:
            discovered_models = await discover_models()
        except Exception:
            discovered_models = []
        try:
            local_ollama_models = await asyncio.to_thread(_fetch_ollama, load_providers_config())
        except Exception:
            local_ollama_models = []
        for item in local_ollama_models:
            if item not in discovered_models:
                discovered_models.append(item)
        opencode_models: list[str] = []
        cli_drivers = agent_cli_drivers_allowed()
        # The OpenCode catalog comes from running its CLI: never where agent CLIs do not run.
        if cli_drivers:
            try:
                opencode_models = await asyncio.to_thread(discover_opencode_models)
            except Exception:
                opencode_models = []
            # OpenCode Ask enables only OpenCode's own and the Ollama provider, so
            # any other catalog entry would be offered only to be refused on submit.
            opencode_models = [item for item in opencode_models if item.startswith(("opencode/", "ollama/"))]
            for item in local_ollama_models:
                if item not in opencode_models:
                    opencode_models.append(item)
        if configured_model and configured_model not in discovered_models:
            discovered_models.insert(0, configured_model)
        models_by_driver: dict[str, list[str]] = {
            "claude": ["sonnet", "opus", "haiku"],
            "codex": [],
            "opencode": opencode_models,
            "ollama": [item for item in discovered_models if item.startswith("ollama/")],
            "openai": [configured_model] if driver == "openai" and configured_model else [],
            "litellm": discovered_models,
        }
        drivers = ["configured", *models_by_driver]
        if not cli_drivers:
            drivers = [item for item in drivers if item not in AGENT_CLI_DRIVERS]
            models_by_driver = {name: rows for name, rows in models_by_driver.items() if name in drivers}
        conversations = _ask_conversation_summaries(session)
        active_conversation_id = (
            (conversation_id or "").strip()
            if conversation_id is not None
            else (str(conversations[0]["id"]) if conversations else "")
        )
        if active_conversation_id and active_conversation_id != _LEGACY_ASK_CONVERSATION_ID:
            selected = store.get_ask_conversation(active_conversation_id)
            if selected is None or selected.review_id != session.id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such Ask conversation")
        if active_conversation_id == _LEGACY_ASK_CONVERSATION_ID and not any(
            item.get("id") == _LEGACY_ASK_CONVERSATION_ID for item in conversations
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such Ask conversation")
        storage_conversation_id = (
            "" if active_conversation_id in {"", _LEGACY_ASK_CONVERSATION_ID} else active_conversation_id
        )
        return {
            "review_id": session.id,
            "latest_revision_number": _display_revision_number(latest),
            "messages": _ask_messages_payload(session, storage_conversation_id),
            "conversations": conversations,
            "active_conversation_id": active_conversation_id,
            "ai": {
                "driver": driver,
                "model": configured_model,
                "status": route_options["status"],
                "reason": route_options["reason"],
                "models": discovered_models,
                "drivers": drivers,
                "models_by_driver": models_by_driver,
            },
        }

    @router.post("/reviews/{review_id}/ask/conversations", status_code=status.HTTP_201_CREATED)
    async def _post_review_ask_conversation(review_id: str, request: Request) -> dict[str, Any]:
        _require_ask_enabled()
        session = _mutable_session(review_id)
        body = await _json_object(request)
        title = " ".join(str(body.get("title") or "").split())[:160]
        origin = body.get("origin") or {}
        if not isinstance(origin, Mapping):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Ask conversation origin must be an object"
            )
        encoded_origin = json.dumps(dict(origin), ensure_ascii=False)
        if len(encoded_origin) > 8192:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Ask conversation origin is too large"
            )
        try:
            record = store.create_ask_conversation(session.id, title=title, origin_json=encoded_origin)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        revision_numbers = {item.id: _display_revision_number(item) for item in store.list_revisions(session.id)}
        return _ask_conversation_payload(record, (), revision_numbers)

    @router.post("/reviews/{review_id}/ask", status_code=status.HTTP_201_CREATED)
    async def _post_review_ask(review_id: str, request: Request) -> dict[str, Any]:
        """Answer one read-only Review question against an exact immutable revision."""

        _require_ask_enabled()
        from lemoncrow.pro.capabilities.review.ask import (
            ASK_OUTPUT_SCHEMA,
            ASK_PROMPT_VERSION,
            ASK_SCHEMA_VERSION,
            DEFAULT_ANSWER_ASK,
            AskContextSelection,
            answer_ask,
            answer_ask_execution,
            build_ask_context,
            wants_previous_snapshot_comparison,
        )
        from lemoncrow.pro.capabilities.review.intelligence_runtime import (
            IntelligenceRunRequest,
            ModelCallBudget,
            ModelCallUsage,
            StructuredExecution,
            run_intelligence,
        )
        from lemoncrow.pro.capabilities.review.session_models import ReviewAskMessage

        # A discarded Review is read-only: Ask appends durable messages.
        session = _mutable_session(review_id)
        body = await _json_object(request)
        requested_conversation_id = str(body.get("conversation_id") or "").strip()
        if len(requested_conversation_id) > 128 or any(char in requested_conversation_id for char in "\r\n\0"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Ask conversation")
        ask_stream_callback = getattr(request.state, "ask_stream_callback", None)
        if not callable(ask_stream_callback):
            ask_stream_callback = None
        raw_driver = str(body.get("driver") or body.get("backend") or "").strip().lower()
        backend = "" if raw_driver in {"", "configured"} else raw_driver
        if backend == "openai_compatible":
            backend = "openai"
        if backend and backend not in {"claude", "codex", "opencode", "ollama", "openai", "litellm"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Ask AI driver")
        model = str(body.get("model") or "").strip()
        if len(model) > 512 or any(char in model for char in "\r\n\0"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Ask AI model")
        if backend == "claude" and model and ("/" in model or model not in {"sonnet", "opus", "haiku"}):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Claude Ask model")
        if backend == "codex" and model and "/" in model:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Codex Ask model")
        if backend == "opencode" and (not model or not (model.startswith("opencode/") or model.startswith("ollama/"))):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="OpenCode Ask requires an opencode/<model> or ollama/<model> selection",
            )
        if backend == "ollama" and model.startswith("ollama/"):
            model = model.removeprefix("ollama/")
        question = str(body.get("body") or "").strip()
        if not question:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ask body is required")
        if len(question) > 16_384:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Ask body is too large")
        raw_mentioned_paths = body.get("mentioned_paths") or []
        if not isinstance(raw_mentioned_paths, list):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ask mentioned_paths must be a list")
        mentioned_paths: list[str] = []
        for raw_path in raw_mentioned_paths:
            if not isinstance(raw_path, str):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Ask mentioned path")
            mentioned_path = raw_path.strip()
            if not mentioned_path or len(mentioned_path) > 1024 or any(char in mentioned_path for char in "\r\n\0"):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Ask mentioned path")
            if mentioned_path not in mentioned_paths:
                mentioned_paths.append(mentioned_path)
        if len(mentioned_paths) > 8:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Ask supports at most 8 mentioned files"
            )
        raw_evidence_ids = body.get("evidence_ids") or []
        if not isinstance(raw_evidence_ids, list):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ask evidence_ids must be a list")
        evidence_ids: list[str] = []
        for raw_evidence_id in raw_evidence_ids:
            if not isinstance(raw_evidence_id, str):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Ask attachment")
            evidence_id = raw_evidence_id.strip()
            if not evidence_id or len(evidence_id) > 256 or any(char in evidence_id for char in "\r\n\0"):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Ask attachment")
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        if len(evidence_ids) > 8:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Ask supports at most 8 external attachments"
            )
        try:
            revision_number = int(cast(Any, body.get("revision_number")))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="revision_number is required",
            ) from None
        revision = _revision_number(session, revision_number)
        revision_paths = _revision_packet_paths(revision)
        if any(mentioned_path not in revision_paths for mentioned_path in mentioned_paths):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ask mentioned files must belong to this Review revision",
            )

        from lemoncrow.pro.capabilities.review.data_governance import process_route_trust
        from lemoncrow.pro.capabilities.review.rules import content_access_for_path, organization_block

        ask_blocked = organization_block(_effective_intelligence_policy(session, revision).get("effective_review"))
        if ask_blocked.get("ai"):
            # The organization AI block wins over any driver or model a person picks.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"Review Ask is unavailable: {ask_blocked['ai']}"
            )
        ask_governance = _review_governance(session, revision)
        resolved_ask_driver = ""
        ask_route_trust: ModelRouteTrust = "unknown"
        if ask_answerer is None:
            _refuse_agent_cli_driver(backend, model, "Review Ask", session=session, revision=revision)
            with contextlib.suppress(Exception):
                resolved_ask_route = _resolve_route(session, revision, backend or None, model or None)
                resolved_ask_driver = resolved_ask_route.driver
                ask_route_trust = process_route_trust(resolved_ask_route.driver)
        ask_access = {
            candidate_path: content_access_for_path(
                candidate_path,
                ask_governance.content_access,
                route_trust=ask_route_trust,
            )
            for candidate_path in revision_paths
        }
        allowed_ask_paths = frozenset(
            candidate_path for candidate_path, decision in ask_access.items() if decision.allowed
        )
        restricted_ask_context = any(not decision.allowed for decision in ask_access.values())
        denied_mentions = [
            candidate_path for candidate_path in mentioned_paths if candidate_path not in allowed_ask_paths
        ]
        if denied_mentions:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ask mentioned file is excluded by AI content access policy",
            )
        attachment_rows: list[dict[str, Any]] = []
        attachment_records = []
        attachment_chars = 0
        text_mime_types = {
            "application/json",
            "application/xml",
            "application/yaml",
            "application/x-yaml",
            "application/toml",
        }
        text_extensions = {
            ".txt",
            ".md",
            ".mdx",
            ".json",
            ".jsonl",
            ".yaml",
            ".yml",
            ".toml",
            ".xml",
            ".csv",
            ".tsv",
            ".py",
            ".pyi",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".go",
            ".rs",
            ".java",
            ".kt",
            ".kts",
            ".scala",
            ".c",
            ".cc",
            ".cpp",
            ".cxx",
            ".h",
            ".hh",
            ".hpp",
            ".cs",
            ".rb",
            ".php",
            ".swift",
            ".sql",
            ".sh",
            ".bash",
            ".zsh",
            ".fish",
            ".ps1",
            ".html",
            ".css",
            ".scss",
            ".less",
            ".vue",
            ".svelte",
            ".graphql",
            ".gql",
            ".proto",
            ".diff",
            ".patch",
            ".log",
            ".ini",
            ".cfg",
            ".conf",
        }
        for evidence_id in evidence_ids:
            evidence = store.get_evidence(evidence_id)
            if evidence is None or evidence.review_id != session.id or evidence.revision_id != revision.id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Ask attachment must belong to this Review revision",
                )
            if evidence.path and evidence.path in ask_access and not ask_access[evidence.path].allowed:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Ask attachment is excluded by AI content access policy",
                )
            execution_record: Mapping[str, Any] | None = None
            if evidence.executor_class:
                # Execution-derived evidence reaches a model only through its
                # content-access projection, never as the raw record. Executed
                # code can read any file, so an untyped execution artifact is
                # held to the most restrictive mode in the repository.
                from lemoncrow.pro.capabilities.review.run_evidence import (
                    access_allows,
                    execution_evidence_for_model,
                    most_restrictive_access,
                )

                raw_record = store.read_evidence_artifact(evidence)
                with contextlib.suppress(TypeError, ValueError):
                    parsed_record = json.loads(raw_record or b"{}")
                    record_data = parsed_record.get("data") if isinstance(parsed_record, Mapping) else None
                    candidate = record_data.get("execution") if isinstance(record_data, Mapping) else None
                    execution_record = candidate if isinstance(candidate, Mapping) else None
                if execution_record is None and not access_allows(
                    most_restrictive_access(row.mode for row in ask_governance.content_access),
                    ask_route_trust,
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Ask attachment is execution evidence excluded by AI content access policy",
                    )
            if execution_record is not None:
                try:
                    projected = execution_evidence_for_model(execution_record, route_trust=ask_route_trust)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Ask attachment has an invalid execution record",
                    ) from exc
                text = json.dumps(projected, sort_keys=True)[: max(0, 240_000 - attachment_chars)]
                attachment_chars += len(text)
                attachment_rows.append(
                    {
                        "id": evidence.id,
                        "title": evidence.title,
                        "mime_type": "application/json",
                        "bytes": len(text),
                        "truncated": False,
                        "text": text,
                    }
                )
                attachment_records.append(evidence)
                continue
            payload = store.read_evidence_artifact(evidence)
            if payload is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Ask attachment content is unavailable"
                )
            mime_type = (evidence.mime_type or "").lower()
            suffix = Path(evidence.title or evidence.source_ref).suffix.lower()
            if not (mime_type.startswith("text/") or mime_type in text_mime_types or suffix in text_extensions):
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail=f"Ask attachment {evidence.title!r} is not a supported text/code file",
                )
            if len(payload) > 1024 * 1024:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Ask attachment {evidence.title!r} exceeds 1 MiB",
                )
            remaining = max(0, 240_000 - attachment_chars)
            if remaining <= 0:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Ask attachments exceed the combined context limit",
                )
            decoded = payload.decode("utf-8", errors="replace")
            text = decoded[: min(120_000, remaining)]
            attachment_chars += len(text)
            attachment_rows.append(
                {
                    "id": evidence.id,
                    "title": evidence.title,
                    "mime_type": evidence.mime_type,
                    "bytes": evidence.bytes,
                    "truncated": len(text) < len(decoded),
                    "text": text,
                }
            )
            attachment_records.append(evidence)

        base_revision_number: int | None = None
        raw_base_revision = body.get("base_revision_number")
        if raw_base_revision is not None:
            try:
                base_revision_number = int(cast(Any, raw_base_revision))
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid base_revision_number",
                ) from None
            _revision_number(session, base_revision_number)
        elif revision_number > 0 and wants_previous_snapshot_comparison(question):
            base_revision_number = revision_number - 1

        raw_context = body.get("context")
        if raw_context is not None and any(
            body.get(key) not in {None, "", 0}
            for key in ("target_unit_key", "path", "symbol", "side", "start_line", "end_line")
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Ask Map context cannot be mixed with client-supplied source scope",
            )
        persisted_context, surface_context, resolved_scope = await _resolve_ask_surface_context(
            session, revision, raw_context
        )
        path = str(resolved_scope.get("path") or body.get("path") or "").strip()
        target_unit_key = str(body.get("target_unit_key") or "").strip()
        symbol = str(resolved_scope.get("symbol") or body.get("symbol") or "").strip()
        side = str(resolved_scope.get("side") or body.get("side") or "new").strip()
        try:
            start_line = max(0, int(resolved_scope.get("start_line") or body.get("start_line") or 0))
            end_line = max(0, int(resolved_scope.get("end_line") or body.get("end_line") or start_line))
        except (TypeError, ValueError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Ask source range") from None
        if end_line and start_line and end_line < start_line:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Ask source range")
        if side not in {"new", "old", "additions", "deletions"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Ask source side")
        if path and not persisted_context and path not in revision_paths:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ask source must belong to this Review revision",
            )
        if path and path not in allowed_ask_paths:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ask source is excluded by AI content access policy",
            )
        if target_unit_key:
            unit = next(
                (item for item in store.list_units(revision.id) if item.unit_key == target_unit_key),
                None,
            )
            if unit is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Ask target does not belong to this Review revision",
                )
            if path and unit.path != path:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Ask target and source path do not match",
                )
            if symbol and unit.symbol and symbol != unit.symbol:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Ask target and source symbol do not match",
                )
            if not path:
                path = unit.path
            if not symbol and unit.kind != "document_section":
                symbol = unit.symbol

        conversation_id = ""
        if requested_conversation_id == _LEGACY_ASK_CONVERSATION_ID:
            if not store.list_ask_messages(session.id, conversation_id="", limit=1):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such Ask conversation")
        elif requested_conversation_id:
            conversation = store.get_ask_conversation(requested_conversation_id)
            if conversation is None or conversation.review_id != session.id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such Ask conversation")
            conversation_id = conversation.id
        else:
            legacy_messages = store.list_ask_messages(session.id, conversation_id="", limit=1)
            recent_conversations = store.list_ask_conversations(session.id, limit=1)
            if legacy_messages:
                conversation_id = ""
            elif recent_conversations:
                conversation_id = recent_conversations[0].id
            else:
                origin = _ask_origin_payload(
                    revision_number=revision_number,
                    path=path,
                    symbol=symbol,
                    start_line=start_line,
                    end_line=end_line,
                    persisted_context=persisted_context,
                )
                conversation_id = store.create_ask_conversation(
                    session.id, origin_json=json.dumps(origin, ensure_ascii=False)
                ).id

        packet = read_packet_json(store, revision)
        if not isinstance(packet, Mapping):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Review packet is unavailable for this revision",
            )
        journal = _journal_projection(session, revision, include_live=False)
        evidence_rows = []
        for item in store.list_evidence(session.id):
            if item.revision_id != revision.id:
                continue
            if item.path and item.path in ask_access and not ask_access[item.path].allowed:
                continue
            if restricted_ask_context and not item.path:
                # Unscoped detail may summarize restricted source and cannot be
                # proven safe for this model route.
                continue
            evidence_rows.append(
                {
                    "kind": item.kind,
                    "title": item.title,
                    "path": item.path,
                    "source": item.source,
                    "verification_status": item.verification_status,
                    "detail": item.detail[:1200],
                }
            )
        previous = store.list_ask_messages(session.id, conversation_id=conversation_id)
        comparison_context = (
            _revision_compare_payload(
                session,
                from_revision_number=base_revision_number,
                to_revision_number=revision_number,
            )
            if base_revision_number is not None and base_revision_number != revision_number
            else None
        )
        persisted_selection = persisted_context.get("selection") if isinstance(persisted_context, Mapping) else None
        context_kind = str(persisted_selection.get("kind") or "") if isinstance(persisted_selection, Mapping) else ""
        context_key = ""
        if isinstance(persisted_selection, Mapping):
            context_key = str(persisted_selection.get("node_id") or persisted_selection.get("edge_id") or "")
        selection = AskContextSelection(
            kind=context_kind
            or ("source" if (path or target_unit_key or symbol or start_line or end_line) else "change"),
            context_key=context_key or target_unit_key or path,
            target_unit_key=target_unit_key,
            path=path,
            symbol=symbol,
            side=side,
            start_line=start_line,
            end_line=end_line,
            surface_context=surface_context,
        )
        ask_code_intelligence = _code_intelligence_for(session, revision)

        def _ask_code_intelligence() -> tuple[Mapping[str, Any], Mapping[str, Any]]:
            # Index queries, and reopening a lapsed Snapshot View, are blocking
            # work: run in a worker thread, not on the server's event loop.
            semantic_retrieval: Mapping[str, Any] = {}
            if (
                not restricted_ask_context
                and ask_code_intelligence is not None
                and hasattr(ask_code_intelligence, "semantic_search")
            ):
                try:
                    semantic_retrieval = ask_code_intelligence.semantic_search(question, limit=6)
                except Exception as exc:
                    semantic_retrieval = {
                        "hits": [],
                        "degraded": True,
                        "degraded_reason": type(exc).__name__,
                        "truncated": False,
                    }
            # A native semantic query may synchronously advance a cold/deferred
            # layer. Read readiness afterwards so one Ask turn cannot carry stale
            # "building" state beside a successful ready query result.
            code_intelligence_readiness: Mapping[str, Any] = {}
            if ask_code_intelligence is not None:
                try:
                    code_intelligence_readiness = ask_code_intelligence.readiness().to_wire()
                except Exception as exc:
                    code_intelligence_readiness = {
                        "backend": "unavailable",
                        "reason": type(exc).__name__,
                        "graph": {"state": "unavailable", "reason": type(exc).__name__},
                        "semantic": {"state": "unavailable", "reason": type(exc).__name__},
                    }
            return semantic_retrieval, code_intelligence_readiness

        semantic_retrieval, code_intelligence_readiness = await asyncio.to_thread(_ask_code_intelligence)
        if restricted_ask_context and isinstance(semantic_retrieval, Mapping):
            semantic_retrieval = dict(semantic_retrieval)
            raw_hits = semantic_retrieval.get("hits")
            if isinstance(raw_hits, list):
                semantic_retrieval["hits"] = [
                    hit
                    for hit in raw_hits
                    if isinstance(hit, Mapping)
                    and str(hit.get("path") or hit.get("file_path") or "") in allowed_ask_paths
                ]
        context = build_ask_context(
            store=store,
            revision=revision,
            packet=packet,
            journal=journal,
            evidence=evidence_rows,
            previous_messages=previous,
            question=question,
            mentioned_paths=mentioned_paths,
            attachments=attachment_rows,
            comparison=comparison_context,
            selection=selection,
            code_intelligence_readiness=code_intelligence_readiness,
            semantic_retrieval=semantic_retrieval,
            allowed_paths=allowed_ask_paths,
            restricted_context=restricted_ask_context,
        )
        context["model_context_exclusions"] = [
            ask_access[candidate_path].to_wire()
            for candidate_path in sorted(ask_access)
            if not ask_access[candidate_path].allowed
        ][:64]
        ask_knowledge = await asyncio.to_thread(
            _ask_project_knowledge,
            session,
            revision,
            question=question,
            paths=(path, *mentioned_paths),
            symbol=symbol,
            surface_context=surface_context,
            content_access=ask_governance.content_access,
            route_trust=ask_route_trust,
            restricted=restricted_ask_context,
        )
        if ask_knowledge is not None and ask_knowledge.entries:
            # PK2: present only when access-filtered history exists, so an Ask
            # without Project Knowledge sends a byte-identical context.
            from lemoncrow.pro.capabilities.review.project_context import CONTEXT_CLASSES

            context["context_classes"] = dict(CONTEXT_CLASSES)
            context["project_knowledge"] = ask_knowledge.model_view()

        snapshot_identity = revision.source_fingerprint or revision.tree_fingerprint or revision.id
        intelligence_meta: dict[str, Any] | None = None
        project_root: Path | None = None
        try:
            if ask_answerer is None and resolved_ask_driver in {"claude", "codex", "opencode"}:
                import shutil

                from lemoncrow.pro.capabilities.review.rules import content_access_for_path
                from lemoncrow.pro.capabilities.review.scratch_cache import touch_cache_entry
                from lemoncrow.pro.capabilities.review.snapshot import materialize_review_side

                access_identity = hashlib.sha256(
                    json.dumps(
                        [row.to_wire() for row in ask_governance.content_access],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                cache_identity = f"{snapshot_identity}:{ask_route_trust}:{access_identity}"
                snapshot_cache = store.root / "cache" / "review-ask-snapshots" / session.id / revision.id
                snapshot_root = snapshot_cache / "repo"
                marker = snapshot_cache / "identity"
                lock = ask_snapshot_locks.setdefault(revision.id, asyncio.Lock())
                async with lock:
                    touch_cache_entry(snapshot_cache)
                    touch_cache_entry(snapshot_cache.parent)
                    marker_identity = ""
                    with contextlib.suppress(OSError):
                        marker_identity = marker.read_text(encoding="utf-8").strip()
                    if not snapshot_root.is_dir() or marker_identity != cache_identity:
                        if snapshot_root.exists():
                            await asyncio.to_thread(shutil.rmtree, snapshot_root)
                        await asyncio.to_thread(
                            materialize_review_side,
                            resolved_source_store,
                            resolved_source_repo,
                            revision,
                            side="new",
                            target=snapshot_root,
                            store=store,
                        )
                        for candidate in sorted(
                            snapshot_root.rglob("*"),
                            key=lambda item: len(item.parts),
                            reverse=True,
                        ):
                            if not (candidate.is_file() or candidate.is_symlink()):
                                continue
                            relative = candidate.relative_to(snapshot_root).as_posix()
                            decision = content_access_for_path(
                                relative,
                                ask_governance.content_access,
                                route_trust=ask_route_trust,
                            )
                            if not decision.allowed:
                                candidate.unlink(missing_ok=True)
                        marker.parent.mkdir(parents=True, exist_ok=True)
                        marker.write_text(cache_identity + "\n", encoding="utf-8")
                        await asyncio.to_thread(_prune_ask_snapshots, snapshot_cache)

                project_root = snapshot_root

            if ask_answerer is None and answer_ask is not DEFAULT_ANSWER_ASK:
                result = await asyncio.to_thread(
                    answer_ask,
                    context,
                    backend=backend or None,
                    model=model or None,
                    project_root=project_root,
                    on_text_delta=ask_stream_callback,
                )
            elif ask_answerer is None:
                intelligence_request = IntelligenceRunRequest(
                    review_id=session.id,
                    revision_id=revision.id,
                    snapshot_identity=snapshot_identity,
                    capability="ask",
                    scope={
                        "target_unit_key": target_unit_key,
                        "path": path,
                        "symbol": symbol,
                        "side": side,
                        "start_line": start_line,
                        "end_line": end_line,
                        "mentioned_paths": list(mentioned_paths),
                        "attachment_ids": [item.id for item in attachment_records],
                    },
                    context=context,
                    policy={
                        "tool_profile": "review",
                        "read_only": True,
                        "immutable_snapshot": True,
                        "model_content_access": {
                            "route_trust": ask_route_trust,
                            "decisions": [
                                ask_access[candidate_path].to_wire() for candidate_path in sorted(ask_access)
                            ],
                            "executor_policy": (
                                [row.to_wire() for row in ask_governance.content_access]
                                if resolved_ask_driver in {"claude", "codex", "opencode"}
                                else []
                            ),
                        },
                    },
                    driver=backend or None,
                    model=model or None,
                    output_schema=ASK_OUTPUT_SCHEMA,
                    prompt_version=ASK_PROMPT_VERSION,
                    schema_version=ASK_SCHEMA_VERSION,
                    budget=ModelCallBudget.from_env(max_calls=1),
                    # The operator policy (if any) refuses a driver/model it pinned to something else.
                    route_policy=_route_policy(session, revision),
                )

                def _execute_ask_intelligence() -> StructuredExecution:
                    if answer_ask is not DEFAULT_ANSWER_ASK:
                        legacy_result = answer_ask(
                            context,
                            backend=backend or None,
                            model=model or None,
                            project_root=project_root,
                            on_text_delta=ask_stream_callback,
                        )
                        resolved = _resolve_route(session, revision, backend or None, model or None)
                        return StructuredExecution(
                            result=dict(legacy_result),
                            driver=resolved.driver,
                            model=resolved.model,
                            usage=ModelCallUsage(calls=1),
                        )
                    # The route run_intelligence resolved, under the same policy.
                    ask_route = _resolve_route(session, revision, backend or None, model or None)
                    return answer_ask_execution(
                        context,
                        backend=ask_route.driver,
                        model=ask_route.model or None,
                        project_root=project_root,
                        on_text_delta=ask_stream_callback,
                        # A reviewer asked this question through the Ask route.
                        origin="human_ask",
                    )

                outcome = await asyncio.to_thread(
                    run_intelligence,
                    store=store,
                    request=intelligence_request,
                    executor=_execute_ask_intelligence,
                )
                if outcome.status != "ready":
                    raise RuntimeError(outcome.error or f"Review Intelligence run ended as {outcome.status}")
                result = outcome.result
                intelligence_meta = outcome.to_wire()
            else:
                result = await asyncio.to_thread(ask_answerer, context)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Review Ask is unavailable: {exc}",
            ) from exc
        if not isinstance(result, Mapping):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Review Ask returned an invalid answer",
            )
        raw_intent = str(result.get("intent") or "question")
        intent: Literal["question", "change_request"] = (
            "change_request" if raw_intent == "change_request" else "question"
        )
        answer = str(result.get("body") or "").strip()
        instruction = str(result.get("handoff_instruction") or "").strip()
        if not answer:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Review Ask returned an empty answer",
            )
        if intent != "change_request":
            instruction = ""

        references: list[dict[str, Any]] = []
        if persisted_context:
            persisted_selection = persisted_context.get("selection")
            if isinstance(persisted_selection, Mapping):
                references.append(
                    {
                        "kind": str(persisted_selection.get("kind") or "review_map_node"),
                        "label": str(persisted_context.get("label") or "Review Map"),
                        "map_selection": dict(persisted_selection),
                    }
                )
        if path:
            references.append(
                {
                    "kind": "source",
                    "exactness": "snapshot",
                    "path": path,
                    "side": side,
                    "start_line": start_line,
                    "end_line": end_line,
                    "target_unit_key": target_unit_key,
                    "symbol": symbol,
                }
            )
        references.extend(
            {
                "kind": "source",
                "exactness": "snapshot",
                "path": str(item.get("path") or ""),
                "side": str(item.get("side") or "new"),
                "start_line": int(item.get("start_line") or 0),
                "end_line": int(item.get("end_line") or 0),
                "target_unit_key": str(item.get("target_unit_key") or ""),
                "symbol": str(item.get("symbol") or ""),
            }
            for item in context.get("source", [])
            if isinstance(item, Mapping)
            and str(item.get("path") or "")
            and (not path or str(item.get("path") or "") != path)
        )
        if "project_knowledge" in context:
            references.extend(_ask_knowledge_references(ask_knowledge))
        action: dict[str, Any] = {}
        if intent == "change_request":
            if not instruction:
                instruction = question
            operation_id = f"aop-{secrets.token_hex(16)}"
            instruction_hash = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
            action = {
                "kind": "agent_handoff",
                "handoff_instruction": instruction,
                "operation_id": operation_id,
                "instruction_hash": instruction_hash,
                "expected_revision_id": revision.id,
                "expected_tree_fingerprint": revision.tree_fingerprint,
                "expected_source_fingerprint": revision.source_fingerprint,
                "delivery": _ask_delivery_capability(
                    revision,
                    path=path,
                    persisted_context=persisted_context,
                ),
            }

        mention_references = [
            {
                "kind": "source",
                "exactness": "snapshot",
                "path": mentioned_path,
                "side": "new",
                "start_line": 0,
                "end_line": 0,
                "target_unit_key": "",
                "symbol": "",
            }
            for mentioned_path in mentioned_paths
        ]
        mention_references.extend(
            {
                "kind": "attachment",
                "exactness": "snapshot",
                "evidence_id": evidence.id,
                "label": evidence.title,
            }
            for evidence in attachment_records
        )

        def _record_ask() -> dict[str, Any]:
            human = store.append_ask_message(
                ReviewAskMessage(
                    id="",
                    review_id=session.id,
                    revision_id=revision.id,
                    role="human",
                    intent=intent,
                    body=question,
                    target_unit_key=target_unit_key,
                    path=path,
                    symbol=symbol,
                    side=side,
                    start_line=start_line,
                    end_line=end_line,
                    context_json=json.dumps(persisted_context, ensure_ascii=False),
                    references_json=json.dumps(mention_references, ensure_ascii=False),
                    conversation_id=conversation_id,
                )
            )
            assistant = store.append_ask_message(
                ReviewAskMessage(
                    id="",
                    review_id=session.id,
                    revision_id=revision.id,
                    role="assistant",
                    intent=intent,
                    body=answer,
                    target_unit_key=target_unit_key,
                    path=path,
                    symbol=symbol,
                    side=side,
                    start_line=start_line,
                    end_line=end_line,
                    context_json=json.dumps(persisted_context, ensure_ascii=False),
                    references_json=json.dumps(references, ensure_ascii=False),
                    action_json=json.dumps(action, ensure_ascii=False),
                    conversation_id=conversation_id,
                )
            )
            if action:
                delivery = action.get("delivery")
                target_ref = str(delivery.get("target_ref") or "") if isinstance(delivery, Mapping) else ""
                store.prepare_ask_handoff(
                    assistant.id,
                    operation_id=str(action["operation_id"]),
                    instruction_hash=str(action["instruction_hash"]),
                    target_ref=target_ref,
                )

            revision_numbers = {item.id: _display_revision_number(item) for item in store.list_revisions(session.id)}
            response = {
                "review_id": session.id,
                "latest_revision_number": _display_revision_number(_latest(session)),
                "conversation_id": conversation_id or _LEGACY_ASK_CONVERSATION_ID,
                "human": _ask_message_payload(human, revision_numbers),
                "assistant": _ask_message_payload(assistant, revision_numbers),
            }
            if intelligence_meta is not None:
                response["intelligence"] = intelligence_meta
            return response

        # The two appends can wait out another writer's lock: off the event loop.
        # lc-debt: the Ask's pre-answer reads (packet, evidence, thread) still run
        # on the loop; move them into the context-building thread.
        return await asyncio.to_thread(_record_ask)

    @router.post("/reviews/{review_id}/ask/stream")
    async def _post_review_ask_stream(review_id: str, request: Request) -> StreamingResponse:
        """Stream Review Ask lifecycle events while preserving the existing durable POST semantics."""

        _require_ask_enabled()
        raw_body = await request.body()
        try:
            decoded = json.loads(raw_body or b"{}")
        except Exception:
            decoded = {}
        driver = str(decoded.get("driver") or "configured") if isinstance(decoded, Mapping) else "configured"
        model = str(decoded.get("model") or "") if isinstance(decoded, Mapping) else ""
        scope = dict(request.scope)
        scope["state"] = dict(cast(Mapping[str, Any], scope.get("state") or {}))
        event_loop = asyncio.get_running_loop()
        delta_queue: asyncio.Queue[str] = asyncio.Queue()

        def on_text_delta(text: str) -> None:
            if text:
                event_loop.call_soon_threadsafe(delta_queue.put_nowait, text)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": raw_body, "more_body": False}

        replayed = Request(scope, receive)
        replayed.state.ask_stream_callback = on_text_delta

        async def events() -> AsyncIterator[bytes]:
            def line(payload: Mapping[str, Any]) -> bytes:
                return (json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

            yield line({"type": "accepted", "driver": driver, "model": model})
            task = asyncio.create_task(_post_review_ask(review_id, replayed))
            async for event in _ask_stream_events(task, delta_queue, driver=driver, model=model):
                yield line(event)

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @router.post("/reviews/{review_id}/ask/{message_id}/handoff")
    async def _post_review_ask_handoff(review_id: str, message_id: str, request: Request) -> dict[str, Any]:
        """Send one explicit chat-requested code change to the exact author session."""

        _require_ask_enabled()
        from lemoncrow.pro.capabilities.review.delivery import deliver_prompt_to_agent_session

        session = _mutable_session(review_id)
        message = store.get_ask_message(message_id)
        if message is None or message.review_id != session.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such Ask message")
        if message.role != "assistant" or message.intent != "change_request":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only an assistant change-request preview can be sent to the author",
            )
        try:
            action = json.loads(message.action_json or "{}")
        except json.JSONDecodeError:
            action = {}
        if not isinstance(action, Mapping) or action.get("kind") != "agent_handoff":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Ask message has no author handoff preview"
            )

        body = await _json_object(request)
        operation_id = str(body.get("operation_id") or "").strip()
        instruction_hash = str(body.get("instruction_hash") or "").strip()
        expected_revision_id = str(body.get("expected_revision_id") or "").strip()
        expected_tree_fingerprint = str(body.get("expected_tree_fingerprint") or "").strip()
        expected_source_fingerprint = str(body.get("expected_source_fingerprint") or "").strip()
        preview_revision_id = str(action.get("expected_revision_id") or message.revision_id)
        preview_tree_fingerprint = str(action.get("expected_tree_fingerprint") or "")
        preview_source_fingerprint = str(action.get("expected_source_fingerprint") or "")
        if (
            operation_id != str(action.get("operation_id") or "")
            or instruction_hash != str(action.get("instruction_hash") or "")
            or expected_revision_id != preview_revision_id
            or (preview_tree_fingerprint and expected_tree_fingerprint != preview_tree_fingerprint)
            or (preview_source_fingerprint and expected_source_fingerprint != preview_source_fingerprint)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ask handoff does not match the reviewed preview/source guards",
            )

        latest = _latest(session)
        if latest.id != message.revision_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Review revision changed since this instruction was prepared; review the latest revision before sending",
            )
        if preview_tree_fingerprint and latest.tree_fingerprint != preview_tree_fingerprint:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Review source tree changed since preview")
        if preview_source_fingerprint and latest.source_fingerprint != preview_source_fingerprint:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Review source changed since preview")

        instruction = str(action.get("handoff_instruction") or "").strip()
        if not instruction or hashlib.sha256(instruction.encode("utf-8")).hexdigest() != instruction_hash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ask handoff instruction changed")

        try:
            handoff_context = json.loads(message.context_json or "{}")
        except (TypeError, json.JSONDecodeError):
            handoff_context = {}
        capability = _ask_delivery_capability(
            latest,
            path=message.path,
            persisted_context=handoff_context if isinstance(handoff_context, Mapping) else {},
        )
        if not capability["supported"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=capability["reason"])

        try:
            prepared, _ = store.prepare_ask_handoff(
                message.id,
                operation_id=operation_id,
                instruction_hash=instruction_hash,
                target_ref=str(capability["target_ref"]),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if prepared.state in {"sent", "uncertain", "dispatching"}:
            return {
                "state": "uncertain" if prepared.state == "dispatching" else prepared.state,
                "target_ref": prepared.target_ref,
                "remote_ref": prepared.remote_ref,
                "message": prepared.last_error
                or (
                    "instruction already sent"
                    if prepared.state == "sent"
                    else "instruction delivery may already be in progress"
                ),
                "operation_id": prepared.operation_id,
            }

        dispatching = store.claim_ask_handoff_dispatch(message.id)
        if dispatching is None:
            # Another request claimed delivery between preview validation and
            # dispatch. Report the durable state instead of waking the author twice.
            current = store.get_ask_handoff(message.id)
            if current is None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ask handoff state is unavailable")
            return {
                "state": "uncertain" if current.state == "dispatching" else current.state,
                "target_ref": current.target_ref,
                "remote_ref": current.remote_ref,
                "message": current.last_error or "instruction delivery is already in progress",
                "operation_id": current.operation_id,
            }

        location = ""
        if message.path:
            location = message.path
            if message.start_line > 0:
                location += (
                    f":L{message.start_line}"
                    if not message.end_line or message.end_line == message.start_line
                    else f":L{message.start_line}-L{message.end_line}"
                )
        prompt_lines = [
            f"Reviewer-requested code change from LemonCrow Review r/{session.id}, rev {_display_revision_number(latest)}.",
            f"Expected Review revision: {latest.id}",
            f"Expected source tree fingerprint: {latest.tree_fingerprint}",
        ]
        if latest.source_fingerprint:
            prompt_lines.append(f"Expected source fingerprint: {latest.source_fingerprint}")
        if location:
            prompt_lines.append(f"Scope: {location}")
        if message.symbol:
            prompt_lines.append(f"Symbol: {message.symbol}")
        if message.target_unit_key:
            prompt_lines.append(f"Review target: {message.target_unit_key}")
        prompt_lines.extend(
            [
                "",
                "Requested change:",
                instruction,
                "",
                "Verify the authoring source still matches the expected Review/source guards before editing; if it moved, refresh/capture Review instead of guessing.",
                "Apply only this requested change in the existing authoring session, preserve unrelated work, run relevant verification, and let the existing Review runtime capture any resulting source checkpoint as a new revision. Delivery does not mean fixed or resolved.",
            ]
        )
        result = await asyncio.to_thread(
            deliver_prompt_to_agent_session,
            str(capability["host"]),
            str(capability["session_id"]),
            resolved_repo,
            "\n".join(prompt_lines),
        )
        final_state: Literal["sent", "blocked", "failed", "uncertain"]
        if result.accepted:
            final_state = "sent"
        elif result.state == "blocked":
            final_state = "blocked"
        elif result.state == "failed":
            final_state = "failed"
        else:
            final_state = "uncertain"
        stored = store.update_ask_handoff(
            message.id,
            state=final_state,
            remote_ref=result.remote_ref,
            last_error="" if result.accepted else result.message,
        )
        if final_state == "sent":
            from lemoncrow.pro.capabilities.review.development import record_feedback_handoff

            with contextlib.suppress(Exception):
                record_feedback_handoff(
                    development_store,
                    session.id,
                    host=str(capability["host"]),
                    session_id=str(capability["session_id"]),
                    operation_id=stored.operation_id if stored is not None else message.id,
                    annotations=[("", instruction.strip().splitlines()[0][:160] if instruction.strip() else "")],
                    state=final_state,
                    causation_id=f"ask:{message.id}",
                    label="Review change request sent",
                )
        if stored is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ask handoff state is unavailable")
        payload = {
            "state": stored.state,
            "target_ref": result.target_ref or stored.target_ref,
            "remote_ref": stored.remote_ref,
            "message": result.message,
            "operation_id": stored.operation_id,
        }
        if final_state in {"blocked", "failed"}:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.message)
        return payload

    @router.get("/reviews/{review_id}/journal")
    def _get_review_journal(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        return _journal_projection(session, _latest(session), include_live=True)

    @router.get("/reviews/{review_id}/revisions/{revision_number}/journal")
    def _get_historical_review_journal(review_id: str, revision_number: int) -> dict[str, Any]:
        session = _session(review_id)
        return _journal_projection(session, _revision_number(session, revision_number), include_live=False)

    def _development_sync(session: ReviewSession) -> None:
        """Throttled incremental ingestion; never lets agent ledgers break Review."""

        if development_ingestor is None or session.status == "archived":
            return
        with contextlib.suppress(Exception):
            development_ingestor.sync(session)

    def _development_cursor(session: ReviewSession) -> str:
        from lemoncrow.pro.capabilities.review.development import DevelopmentUnavailable

        try:
            return str(development_store.cursor(session.id))
        except DevelopmentUnavailable:
            return ""
        except Exception:
            return ""

    def _development_payload(session: ReviewSession, revision: ReviewRevision, *, historical: bool) -> dict[str, Any]:
        """Snapshot-exact Development plus explicitly separate newer work."""

        from lemoncrow.pro.capabilities.review.development import development_view

        if not historical:
            _development_sync(session)
        packet = read_packet_json(store, revision)
        attributions = [
            {
                "scope": item.scope,
                "scope_key": item.scope_key,
                "host": item.host,
                "session_id": item.session_id,
                "model": item.model,
                "certainty": item.certainty,
                "method": item.method,
            }
            for item in store.list_revision_attributions(revision.id)
        ]
        numbers = {item.id: _display_revision_number(item) for item in store.list_revisions(session.id)}
        view = development_view(
            development_store if development_store.available() else None,
            change_id=session.id,
            revision_id=revision.id,
            revision_number=_display_revision_number(revision),
            revision_created_at=revision.created_at,
            packet=packet if isinstance(packet, dict) else None,
            attributions=attributions,
            revision_numbers=numbers,
            historical=historical,
            live_enabled=development_ingestor is not None,
        )
        # P12-AGENT: each observed side effect says whether a human pre-approved
        # exactly it, and Activity lists the decisions themselves.
        for scope in ("snapshot", "since"):
            part = view.get(scope)
            if isinstance(part, dict) and part.get("outside_workspace"):
                part["outside_workspace"] = _approval_annotated(list(part["outside_workspace"]), session)
        view["agent_approvals"] = _approval_decisions(session, limit=50)
        return view

    @router.get("/reviews/{review_id}/development")
    def _get_review_development(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        return _development_payload(session, _latest(session), historical=False)

    @router.get("/reviews/{review_id}/revisions/{revision_number}/development")
    def _get_historical_review_development(review_id: str, revision_number: int) -> dict[str, Any]:
        session = _session(review_id)
        return _development_payload(session, _revision_number(session, revision_number), historical=True)

    @router.get("/reviews/{review_id}/development/events")
    def _get_review_development_events(
        review_id: str,
        revision: int | None = None,
        scope: str = "snapshot",
        view: str = "all",
        before: int | None = None,
        limit: int = 50,
        path: str = "",
    ) -> dict[str, Any]:
        """Paginated Development timeline, newest first, never loaded upfront.

        ``scope=snapshot`` is frozen at the Snapshot's watermark (or its legacy
        packet journal); ``since`` is strictly newer work; ``all`` is the Change.
        """

        from lemoncrow.pro.capabilities.review.development import (
            DevelopmentUnavailable,
            event_payload,
            legacy_events,
            legacy_timeline,
            timeline_filter,
        )

        session = _session(review_id)
        target = _latest(session) if revision is None else _revision_number(session, revision)
        if scope not in {"snapshot", "since", "all"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="scope must be snapshot, since, or all",
            )
        try:
            kinds, roles = timeline_filter(view)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        bounded = max(1, min(int(limit), 200))
        available = development_store.available()
        watermark = development_store.snapshot_watermark(target.id) if available else None
        source = "events"
        rows = []
        try:
            snapshot_events = (
                bool(development_store.list_events(session.id, upto=watermark, limit=1))
                if available and watermark is not None
                else False
            )
            if scope == "snapshot" and not snapshot_events:
                packet = read_packet_json(store, target)
                packet_map = packet if isinstance(packet, dict) else {}
                provenance = packet_map.get("provenance")
                legacy = legacy_events(
                    session.id,
                    [item for item in packet_map.get("journal") or [] if isinstance(item, Mapping)],
                    provenance=provenance if isinstance(provenance, Mapping) else None,
                )
                source = "legacy_journal"
                rows = legacy_timeline(legacy, kinds=kinds, roles=roles, before=before, limit=bounded + 1, path=path)
            elif available:
                rows = development_store.list_events(
                    session.id,
                    after=watermark if scope == "since" and watermark is not None else 0,
                    after_at=target.created_at if scope == "since" and watermark is None else "",
                    upto=watermark if scope == "snapshot" else None,
                    before=before,
                    limit=bounded + 1,
                    kinds=kinds,
                    roles=roles,
                    path=path,
                )
        except DevelopmentUnavailable:
            rows = []
        has_more = len(rows) > bounded
        rows = rows[:bounded]
        return {
            "review_id": session.id,
            "revision_id": target.id,
            "revision_number": _display_revision_number(target),
            "scope": scope,
            "view": view,
            "source": source,
            "events": [event_payload(item) for item in rows],
            "next_before": rows[-1].sequence if has_more and rows else None,
        }

    @router.post("/reviews/{review_id}/development/sessions", status_code=status.HTTP_201_CREATED)
    async def _post_review_development_session(review_id: str, request: Request) -> dict[str, Any]:
        """Explicitly bind a session to this Change.  Binding scopes; it never authors."""

        from lemoncrow.pro.capabilities.review.development import DevelopmentUnavailable, bind_session

        session = _mutable_session(review_id)
        body = await _json_object(request)
        try:
            bound = bind_session(
                development_store,
                session.id,
                host=str(body.get("host") or ""),
                session_id=str(body.get("session_id") or ""),
                purpose=str(body.get("purpose") or ""),
                parent_session_id=str(body.get("parent_session_id") or ""),
                task=str(body.get("task") or ""),
                model=str(body.get("model") or ""),
            )
        except DevelopmentUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        return {
            "session": {
                "host": bound.host,
                "session_id": bound.session_id,
                "purpose": bound.purpose,
                "role": bound.role,
                "binding": bound.binding,
                "parent_session_id": bound.parent_session_id,
            }
        }

    @router.post("/reviews/{review_id}/development/events", status_code=status.HTTP_201_CREATED)
    async def _post_review_development_events(review_id: str, request: Request) -> dict[str, Any]:
        """Append explicitly reported, sanitized Development events (idempotent)."""

        from lemoncrow.pro.capabilities.review.development import DevelopmentUnavailable, event_payload, push_events

        session = _mutable_session(review_id)
        body = await _json_object(request)
        raw_events = body.get("events")
        if not isinstance(raw_events, list) or not all(isinstance(item, Mapping) for item in raw_events):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="events must be a list of objects",
            )
        try:
            result = push_events(development_store, session.id, raw_events)
        except DevelopmentUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        return {
            "inserted": len(result.inserted),
            "cursor": str(result.cursor),
            "events": [event_payload(item) for item in result.inserted],
        }

    async def _review_map_payload(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        historical: bool,
        level: str,
        node_id: str,
        projection: str = "change_map",
    ) -> dict[str, Any]:
        if level not in {"architecture", "file", "symbol"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="level must be 'architecture', 'file', or 'symbol'",
            )
        if projection not in {"change_map", "architecture_delta"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="projection must be 'change_map' or 'architecture_delta'",
            )
        if projection == "architecture_delta" and level != "architecture":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="architecture_delta projection is only available at architecture level",
            )
        # Units, marks, the packet and target derivation are store reads and
        # CPU work: off the event loop, which in the local server also carries
        # every agent's tool call.
        return await asyncio.to_thread(
            _review_map_payload_sync,
            session,
            revision,
            historical=historical,
            level=level,
            node_id=node_id,
            projection=projection,
        )

    def _review_map_payload_sync(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        historical: bool,
        level: str,
        node_id: str,
        projection: str = "change_map",
    ) -> dict[str, Any]:
        units = store.list_units(revision.id)
        marks = (
            _marks_visible_at(session, revision)
            if historical
            else store.list_marks(session.id, reviewer_id=session.reviewer_id)
        )
        packet = read_packet_json(store, revision)
        annotations = _annotations_at(session, revision, historical=historical)
        frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            revision,
            units,
            marks,
            annotations=annotations,
        )
        targets = _derive_targets(session, revision, units, frontier.entries, packet, annotations)
        artifacts = _evidence_at(session, revision, historical=historical)
        current_artifacts = tuple(item for item in artifacts if _evidence_is_current(item, revision))
        evidence = _evidence(packet, current_artifacts, revision_id=revision.id)
        context_repo_root = resolved_dependency_repo
        if context_repo_root is None and source_refresh_enabled:
            context_repo_root = resolved_source_repo

        from lemoncrow.pro.capabilities.review.review_map import build_review_map

        payload = build_review_map(
            review_id=session.id,
            revision=revision,
            packet=packet,
            targets=targets,
            evidence=evidence,
            context_repo_root=context_repo_root,
            code_intelligence=_code_intelligence_for(session, revision),
            level=cast(Any, level),
            selected_node_id=node_id,
        )
        snapshot = payload.get("snapshot")
        if isinstance(snapshot, dict):
            snapshot["degraded_notes"] = [
                {"name": name, "note": degraded_note(name)} for name in snapshot.get("degraded") or ()
            ]
        payload["projection"] = projection
        if projection == "architecture_delta":
            from lemoncrow.pro.capabilities.review.architecture_delta import build_architecture_delta_from_revision

            raw_graph = payload.get("graph")
            context_graph: Mapping[str, Any] = raw_graph if isinstance(raw_graph, Mapping) else {}
            raw_context = payload.get("context")
            map_context: Mapping[str, Any] = raw_context if isinstance(raw_context, Mapping) else {}
            delta = build_architecture_delta_from_revision(
                store=store,
                repo_root=resolved_source_repo,
                revision=revision,
                packet=packet or {},
                context_graph=context_graph,
                context=map_context,
            )
            # Observed execution traces from the same Snapshot's runs ground inferred flow steps (R7 item 14).
            from lemoncrow.pro.capabilities.review.flow_traces import ground_architecture_flow

            delta = ground_architecture_flow(delta, store, revision)
            payload["architecture_delta"] = delta
            graph = delta.get("graph")
            payload["graph"] = dict(graph) if isinstance(graph, Mapping) else payload["graph"]
        return payload

    @router.get("/reviews/{review_id}/map")
    async def _get_review_map(
        review_id: str,
        level: str = "architecture",
        node_id: str = "",
        projection: str = "change_map",
    ) -> dict[str, Any]:
        session = _session(review_id)
        return await _review_map_payload(
            session,
            _latest(session),
            historical=False,
            level=level,
            node_id=node_id,
            projection=projection,
        )

    @router.get("/reviews/{review_id}/revisions/{revision_number}/map")
    async def _get_historical_review_map(
        review_id: str,
        revision_number: int,
        level: str = "architecture",
        node_id: str = "",
        projection: str = "change_map",
    ) -> dict[str, Any]:
        session = _session(review_id)
        return await _review_map_payload(
            session,
            _revision_number(session, revision_number),
            historical=True,
            level=level,
            node_id=node_id,
            projection=projection,
        )

    @router.get("/reviews/{review_id}/project-context")
    async def _get_review_project_context(review_id: str, request: Request) -> dict[str, Any]:
        """Read-only, bounded landed history for the Brief or a Map selection (PK2).

        Reads PK0 only: it never synthesizes, enqueues or schedules anything.
        Project Knowledge is advisory and never Snapshot evidence.
        """

        from lemoncrow.pro.capabilities.review.project_context import ProjectContextRequest
        from lemoncrow.pro.capabilities.review.project_knowledge import KnowledgeSubject

        session = _session(review_id)
        params = request.query_params
        raw_revision = str(params.get("revision_number") or "").strip()
        if raw_revision:
            try:
                revision = _revision_number(session, int(raw_revision))
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid revision_number") from None
        else:
            revision = _latest(session)
        purpose = str(params.get("purpose") or "brief").strip()
        if purpose not in {"brief", "map"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="purpose must be brief or map")

        def keys(name: str) -> tuple[str, ...]:
            values = [str(value).strip() for value in params.getlist(name) if str(value).strip()]
            if len(values) > 16 or any(len(value) > 1024 or any(c in value for c in "\r\n\0") for value in values):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid {name} selection")
            return tuple(dict.fromkeys(values))

        paths = keys("path")
        for candidate in paths:
            try:
                KnowledgeSubject("path", candidate)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid path selection") from None
        context_request = ProjectContextRequest(
            purpose="map" if purpose == "map" else "brief",
            paths=paths,
            symbols=keys("symbol"),
            components=keys("component"),
            contracts=keys("contract"),
            edges=keys("edge"),
        )
        context = await asyncio.to_thread(_project_context, session, revision, context_request)
        return {
            "review_id": session.id,
            "revision_id": revision.id,
            "revision_number": revision.revision_number,
            "project_context": context.to_wire(),
        }

    async def _architecture_explanation_payload(
        session: ReviewSession,
        revision: ReviewRevision,
        request: Request,
    ) -> dict[str, Any]:
        body = await _json_object(request)
        raw_driver = str(body.get("driver") or body.get("backend") or "").strip().lower()
        backend = "" if raw_driver in {"", "configured"} else raw_driver
        if backend == "openai_compatible":
            backend = "openai"
        if backend and backend not in {"claude", "codex", "opencode", "ollama", "openai", "litellm"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Architecture Delta AI driver")
        model = str(body.get("model") or "").strip()
        if len(model) > 512 or any(char in model for char in "\r\n\0"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Architecture Delta AI model")
        # Before any Snapshot work: the requested or configured route must not be an agent CLI here.
        _refuse_agent_cli_driver(backend, model, "Architecture explanation", session=session, revision=revision)

        payload = await _review_map_payload(
            session,
            revision,
            historical=revision.id != _latest(session).id,
            level="architecture",
            node_id="",
            projection="architecture_delta",
        )
        raw_delta = payload.get("architecture_delta")
        delta: Mapping[str, Any] = raw_delta if isinstance(raw_delta, Mapping) else {}
        raw_summary = delta.get("summary")
        summary: Mapping[str, Any] = raw_summary if isinstance(raw_summary, Mapping) else {}
        raw_graph = delta.get("graph")
        graph: Mapping[str, Any] = raw_graph if isinstance(raw_graph, Mapping) else {}
        exact_changed_edges = [
            row
            for row in list(graph.get("edges") or [])
            if isinstance(row, Mapping)
            and str(row.get("exactness") or "") == "snapshot"
            and str(row.get("delta_status") or "") in {"added", "removed", "changed"}
        ]
        if not bool(summary.get("material")) or not exact_changed_edges:
            return {
                "status": "not_applicable",
                "architecture_delta_digest": str(delta.get("digest") or ""),
                "result": {
                    "summary": str(delta.get("reason") or "No material relationship delta needs AI explanation."),
                    "why_it_matters": [],
                    "grounded_edge_ids": [],
                    "flow": [],
                    "limitations": [
                        "No Snapshot-exact changed relationship is available for bounded architecture narration."
                    ],
                    "provenance": "deterministic",
                    "exactness": "snapshot",
                },
                "intelligence": None,
            }

        from lemoncrow.pro.capabilities.review.architecture_delta import (
            architecture_explain_context,
            run_architecture_explanation,
        )
        from lemoncrow.pro.capabilities.review.data_governance import process_route_trust
        from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallBudget
        from lemoncrow.pro.capabilities.review.rules import content_access_for_path, organization_block

        budget = ModelCallBudget.from_env(max_calls=1)
        snapshot_identity = revision.source_fingerprint or revision.tree_fingerprint or revision.id
        blocked = organization_block(_effective_intelligence_policy(session, revision).get("effective_review"))
        try:
            if blocked.get("ai"):
                raise PermissionError(blocked["ai"])
            resolved = _resolve_route(session, revision, backend or None, model or None)
        except Exception as exc:
            return {
                "status": "failed",
                "architecture_delta_digest": str(delta.get("digest") or ""),
                "result": {},
                "intelligence": None,
                "error": f"Architecture explanation is unavailable: {exc}",
            }
        governance = _review_governance(session, revision)
        route_trust = process_route_trust(resolved.driver)
        graph_paths = tuple(
            dict.fromkeys(
                str(row.get("path") or "")
                for row in list(graph.get("nodes") or ())
                if isinstance(row, Mapping) and str(row.get("path") or "")
            )
        )
        access = {
            path: content_access_for_path(path, governance.content_access, route_trust=route_trust)
            for path in graph_paths
        }
        excluded = tuple(access[path].to_wire() for path in sorted(access) if not access[path].allowed)
        allowed_paths = frozenset(path for path in graph_paths if access[path].allowed)
        filtered_paths = allowed_paths if excluded else None
        narration_context = architecture_explain_context(delta, allowed_paths=filtered_paths)
        if exact_changed_edges and not narration_context.get("changed_edges"):
            return {
                "status": "not_applicable",
                "architecture_delta_digest": str(delta.get("digest") or ""),
                "result": {
                    "summary": "Architecture narration is excluded by AI content access policy.",
                    "why_it_matters": [],
                    "grounded_edge_ids": [],
                    "flow": [],
                    "limitations": ["Human and deterministic architecture Review remain available."],
                    "provenance": "deterministic",
                    "exactness": "snapshot",
                },
                "intelligence": None,
                "excluded_by_policy": list(excluded[:64]),
            }
        policy_context = {
            **_effective_intelligence_policy(session, revision),
            "model_content_access": {
                "route_trust": route_trust,
                "decisions": [access[path].to_wire() for path in sorted(access)],
                "executor_policy": (
                    [row.to_wire() for row in governance.content_access]
                    if resolved.driver in {"claude", "codex", "opencode"}
                    else []
                ),
            },
        }

        def execute() -> Any:
            project_root = _intelligence_project_root(
                session,
                revision,
                capability="architecture_explain",
                driver=resolved.driver,
                snapshot_identity=snapshot_identity,
            )
            return run_architecture_explanation(
                store=store,
                review_id=session.id,
                revision_id=revision.id,
                snapshot_identity=snapshot_identity,
                delta=delta,
                driver=resolved.driver,
                model=resolved.model or None,
                budget=budget,
                policy_context=policy_context,
                allowed_paths=filtered_paths,
                project_root=project_root,
            )

        try:
            outcome = await asyncio.to_thread(execute)
        except Exception as exc:
            return {
                "status": "failed",
                "architecture_delta_digest": str(delta.get("digest") or ""),
                "result": {},
                "intelligence": None,
                "error": f"Architecture explanation is unavailable: {exc}",
                "excluded_by_policy": list(excluded[:64]),
            }
        if outcome.status != "ready":
            return {
                "status": outcome.status,
                "architecture_delta_digest": str(delta.get("digest") or ""),
                "result": outcome.result,
                "intelligence": outcome.to_wire(),
                "error": outcome.error,
                "excluded_by_policy": list(excluded[:64]),
            }
        return {
            "status": "ready",
            "architecture_delta_digest": str(delta.get("digest") or ""),
            "result": outcome.result,
            "intelligence": outcome.to_wire(),
            "excluded_by_policy": list(excluded[:64]),
        }

    @router.post("/reviews/{review_id}/architecture-explain")
    async def _explain_review_architecture(review_id: str, request: Request) -> dict[str, Any]:
        session = _session(review_id)
        return await _architecture_explanation_payload(session, _latest(session), request)

    @router.post("/reviews/{review_id}/revisions/{revision_number}/architecture-explain")
    async def _explain_historical_review_architecture(
        review_id: str,
        revision_number: int,
        request: Request,
    ) -> dict[str, Any]:
        session = _session(review_id)
        return await _architecture_explanation_payload(session, _revision_number(session, revision_number), request)

    @router.get("/reviews/{review_id}")
    def _get_review(review_id: str, startup: bool = False) -> dict[str, Any]:
        return _overview(_session(review_id), startup=startup)

    @router.get("/reviews/{review_id}/revisions/{revision_number}")
    def _get_historical_review(review_id: str, revision_number: int) -> dict[str, Any]:
        session = _session(review_id)
        revision = _revision_number(session, revision_number)
        return _overview(session, revision, historical=True)

    @router.get("/reviews/{review_id}/relationships")
    def _get_relationships(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        return _change_context_payload(store, session, include_stack=True)

    @router.get("/reviews/{review_id}/change-set")
    async def _get_change_set(review_id: str) -> dict[str, Any]:
        """The multi-repository change set this Change is in (Phase 9R item 42), as the viewer may see it."""

        from lemoncrow.pro.capabilities.review import change_sets

        session = _session(review_id)

        def build() -> dict[str, Any]:
            discovery = _change_set_discovery(session)
            if discovery is None:
                return change_sets.unlinked_wire(available=change_set_directory is not None)
            return change_sets.to_wire(change_sets.assess(discovery, include_start=True))

        return await asyncio.to_thread(build)

    async def _change_set_write(review_id: str, request: Request, operation: str) -> dict[str, Any]:
        """Apply one membership change (``link``, ``unlink`` or ``replace``) and return the set."""

        from lemoncrow.pro.capabilities.review import change_sets

        session = _mutable_session(review_id)
        body = await _json_object(request)
        directory = change_set_directory
        if directory is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="change sets link Reviews across repositories; this Reader serves one repository",
            )
        apply = {"link": change_sets.link, "unlink": change_sets.unlink, "replace": change_sets.replace}[operation]

        def run() -> dict[str, Any]:
            start = _member_snapshot(session)
            if start is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="this review has no revision yet")
            before = _change_set_discovery(session)
            try:
                target = apply(directory, start, str(body.get("repository") or ""), str(body.get("review") or ""))
            except change_sets.LinkRefused as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            discovery = _change_set_discovery(session)
            # Every member whose set changed -- joined, left or stayed -- has its armed
            # landing revalidated now; this Change's own follows the thread below.
            touched = {
                member.ref: member
                for member in (
                    *(before.others if before is not None else ()),
                    *(discovery.others if discovery is not None else ()),
                    target,
                )
                if member.ref != start.ref
            }
            for member in touched.values():
                if member.revalidate_landing is None:
                    continue
                try:
                    member.revalidate_landing()
                except Exception:  # one member's revalidation failure never fails the set change
                    logging.getLogger(__name__).warning(
                        "armed-landing revalidation failed for change-set member %s", member.ref.key, exc_info=True
                    )
            if discovery is None:
                return change_sets.unlinked_wire(available=True)
            return change_sets.to_wire(change_sets.assess(discovery, include_start=True))

        result = await asyncio.to_thread(run)
        await _enforce_armed_landing(session)
        return result

    @router.post("/reviews/{review_id}/change-set")
    async def _post_change_set(review_id: str, request: Request) -> dict[str, Any]:
        """Link this Change and another repository's Change into one set (``{repository, review}``)."""

        return await _change_set_write(review_id, request, "link")

    @router.post("/reviews/{review_id}/change-set/unlink")
    async def _post_change_set_unlink(review_id: str, request: Request) -> dict[str, Any]:
        """Remove the link between this Change and another repository's Change (``{repository, review}``)."""

        return await _change_set_write(review_id, request, "unlink")

    @router.post("/reviews/{review_id}/change-set/replace")
    async def _post_change_set_replace(review_id: str, request: Request) -> dict[str, Any]:
        """Point this Change's link in ``repository`` at another Change of it (``{repository, review}``)."""

        return await _change_set_write(review_id, request, "replace")

    @router.post("/reviews/{review_id}/relationships")
    async def _post_relationship(review_id: str, request: Request) -> dict[str, Any]:
        session = _mutable_session(review_id)
        body = await _json_object(request)
        kind = str(body.get("kind") or "").strip()
        relation = str(body.get("relation") or "").strip()
        if kind not in {"scm", "change", "work_item", "scm_requirement"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="kind must be scm, change, work_item, or scm_requirement",
            )
        target_ref = str(body.get("target_change_ref") or "").strip()
        external_id = str(body.get("external_id") or "").strip()
        url = str(body.get("url") or "").strip()
        if url:
            from urllib.parse import urlparse

            parsed_url = urlparse(url)
            # Relationship links render as clickable Reader links for every
            # reviewer; only absolute web URLs may be stored (no javascript:,
            # data:, file:, or scheme-relative links).
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="relationship url must be an absolute http(s) URL",
                )
        target_review_id = ""
        if target_ref:
            target = store.resolve_session(target_ref)
            if target is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="related Change not found")
            if target.id == session.id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="a Change cannot relate to itself",
                )
            target_review_id = target.id
        if kind == "change" and not (target_review_id or external_id or url):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="change relationship needs target_change_ref, external_id, or url",
            )
        if kind in {"scm", "work_item", "scm_requirement"} and not (external_id or url):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{kind} relationship needs external_id or url",
            )
        from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

        stored = store.upsert_relationship(
            ReviewRelationship(
                id="",
                review_id=session.id,
                kind=kind,
                relation=relation or ("requires" if kind == "scm_requirement" else "relates_to"),
                provider=str(body.get("provider") or ""),
                external_id=external_id,
                target_review_id=target_review_id,
                url=url,
                title=str(body.get("title") or ""),
                status=str(body.get("status") or ""),
                source=str(body.get("source") or "explicit"),
                metadata_json=json.dumps(body.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
            )
        )
        await _enforce_armed_landing(session)
        return {"relationship": _relationship_payload(stored), **_change_context_payload(store, session)}

    @router.get("/reviews/{review_id}/verification")
    async def _get_review_verification(review_id: str, revision_id: str = "") -> dict[str, Any]:
        session = _session(review_id)
        revision = _requested_revision(session, revision_id)
        return await asyncio.to_thread(_verification_projection, session, revision, discover_web_routes=True)

    @router.post("/reviews/{review_id}/browser-checks", status_code=status.HTTP_201_CREATED)
    async def _post_browser_check(review_id: str, request: Request) -> dict[str, Any]:
        """Record one browser/UI regression run against one exact Snapshot side.

        Execution happens in the reporter (Playwright/browser tests, CI, an
        external browser agent, or a local hook). Review validates the report,
        binds it to the named immutable Snapshot, and computes the verdict from
        inspectable checks; a reported PASS alone is never trusted.
        """

        from lemoncrow.pro.capabilities.review.browser_checks import (
            BROWSER_CHECK_MEDIA_TYPE,
            browser_source_ref,
            browser_summary,
            normalize_browser_run,
        )
        from lemoncrow.pro.capabilities.review.store import new_evidence_id

        session = _mutable_session(review_id)
        body = await _json_object(request)
        requested_revision_id = str(body.get("revision_id") or "").strip()
        if not requested_revision_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="browser checks must name the exact revision_id (Snapshot) they ran against",
            )
        revision = _requested_revision(session, requested_revision_id)
        source = str(body.get("source") or "ci").strip()
        if source not in REVIEW_EVIDENCE_SOURCES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown evidence source {source!r}",
            )

        def _record() -> tuple[dict[str, Any], ReviewEvidence]:
            policy = _browser_policy(revision)
            try:
                record = normalize_browser_run(
                    body,
                    policy=policy,
                    snapshot=revision,
                    resolve_artifact=store.get_evidence,
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
            payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            evidence_id = new_evidence_id()
            rel, digest, size = store.write_evidence_artifact(session.id, revision.id, evidence_id, payload)
            try:
                stored = store.add_evidence(
                    ReviewEvidence(
                        id=evidence_id,
                        review_id=session.id,
                        revision_id=revision.id,
                        kind="verification",
                        title=str(record["name"]),
                        artifact_path=rel,
                        content_hash=digest,
                        mime_type=BROWSER_CHECK_MEDIA_TYPE,
                        bytes=size,
                        source=source,  # type: ignore[arg-type]
                        source_ref=browser_source_ref(record),
                        # A baseline (old-side) run describes pre-change code. It is
                        # comparison evidence, never verification of this Snapshot.
                        verification_status=str(record["status"]) if record["side"] == "new" else "",
                        detail=browser_summary(record),
                    ),
                )
            except ValueError as exc:
                store.discard_artifact(rel)
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            return record, stored

        def _response(record: dict[str, Any], stored: ReviewEvidence) -> dict[str, Any]:
            return {
                "browser_check": record,
                "evidence": _review_evidence_payload(
                    stored,
                    current=revision.id == _latest(session).id,
                    revision_number=_display_revision_number(revision),
                ),
                "verification": _verification_projection(session, revision),
            }

        # The base-side policy read, artifact resolution, the artifact write,
        # the append (which can wait out another writer's lock) and the
        # projection all run off the event loop.
        record, stored = await asyncio.to_thread(_record)
        if stored.verification_status:
            await _enforce_armed_landing(session)
        return await asyncio.to_thread(_response, record, stored)

    def _runtime_validation_selection(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        provider: str,
        surface_id: str,
        side: str,
    ) -> tuple[Any, Any, Any, Any]:
        """Resolve one executable scenario exclusively from trusted-base policy."""

        from lemoncrow.pro.capabilities.review.runtime_validation import (
            build_surface_validation_request,
            surface_validation_scenario,
            trusted_policy_digest,
        )

        (
            _trusted_revision,
            trusted_registry,
            _trusted_runners,
            trusted_context,
        ) = _surface_runtime(session.id, revision.id, side="old", immutable_execution=True)
        trusted_surfaces, _trusted_issues = _discover_surfaces(
            trusted_registry,
            trusted_context,
        )
        surface = next(
            (item for item in trusted_surfaces if item.provider == provider and item.id == surface_id),
            None,
        )
        if surface is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "runtime validation scenario is not present in the trusted base; "
                    "land/configure it before repository execution is allowed"
                ),
            )
        runtime = trusted_context.config.runtime(surface.runtime) if surface.runtime else None
        if runtime is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="trusted-base runtime validation scenario has no configured runtime",
            )
        (
            _execution_revision,
            _execution_registry,
            execution_runners,
            execution_context,
        ) = _surface_runtime(session.id, revision.id, side=side, immutable_execution=True)
        authority = _snapshot_review_config(revision)
        policy_digest = trusted_policy_digest(
            authority.content,
            base_known=authority.base_known,
        )
        validation_request = build_surface_validation_request(
            revision,
            surface,
            runtime,
            side=side,  # type: ignore[arg-type]
            policy_digest=policy_digest,
        )
        scenario = surface_validation_scenario(surface, runtime)
        return (
            surface,
            scenario,
            validation_request,
            (
                trusted_context,
                execution_context,
                execution_runners,
            ),
        )

    def _runtime_validation_response(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        validation_request: Any,
        record: Mapping[str, Any] | None = None,
        evidence: ReviewEvidence | None = None,
        reused: bool = False,
        job: Mapping[str, Any] | None = None,
        execution_intent: str = "",
    ) -> dict[str, Any]:
        resolved_intent = execution_intent or (str(job.get("execution_intent") or "") if job is not None else "")
        payload: dict[str, Any] = {
            "request": validation_request.to_wire(),
            "reused": reused,
            "execution_intent": resolved_intent,
            "historical": resolved_intent == "explicit_historical",
            "verification": _verification_projection(session, revision),
        }
        if record is not None:
            payload["runtime_validation"] = dict(record)
        elif job is not None:
            payload["runtime_validation"] = {
                "status": "SUPERSEDED" if job.get("superseded") else str(job.get("state") or "unknown").upper(),
                "job": dict(job),
            }
        if evidence is not None:
            payload["evidence"] = _review_evidence_payload(
                evidence,
                current=revision.id == _latest(session).id,
                revision_number=_display_revision_number(revision),
            )
        return payload

    def _runtime_validation_superseded_response(
        session: ReviewSession,
        revision: ReviewRevision,
        validation_request: Any,
        *,
        detail: str,
        execution_intent: str,
        job: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        projected_job = dict(job or {})
        projected_job.setdefault("state", "superseded")
        projected_job["superseded"] = True
        projected_job["execution_intent"] = execution_intent
        projected_job["historical"] = False
        projected_job["publication_state"] = "superseded"
        projected_job["detail"] = detail
        projected_job.pop("observation", None)
        return _runtime_validation_response(
            session,
            revision,
            validation_request=validation_request,
            job=projected_job,
            execution_intent=execution_intent,
        )

    @router.post("/reviews/{review_id}/runtime-validation", status_code=status.HTTP_201_CREATED)
    async def _post_runtime_validation(review_id: str, request: Request) -> dict[str, Any]:
        """Explicitly validate one trusted runtime surface on one exact Snapshot."""

        from lemoncrow.pro.capabilities.review.runtime_validation import (
            execute_local_surface_validation,
            normalize_surface_run,
            persist_validation_record,
        )
        from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult

        session = _mutable_session(review_id)
        body = await _json_object(request)
        requested_revision_id = str(body.get("revision_id") or "").strip()
        if not requested_revision_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="runtime validation must name the exact revision_id (Snapshot) to execute",
            )
        revision = _requested_revision(session, requested_revision_id)
        execution_intent = "current" if revision.id == _latest(session).id else "explicit_historical"
        provider = str(body.get("provider") or "").strip()
        surface_id = str(body.get("surface_id") or "").strip()
        side = str(body.get("side") or "new").strip()
        approve_untrusted_execution = body.get("approve_untrusted_execution", False)
        if not isinstance(approve_untrusted_execution, bool):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="approve_untrusted_execution must be a boolean",
            )
        if not provider or not surface_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="runtime validation requires provider and surface_id",
            )
        if side not in {"old", "new"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="runtime validation side must be old or new",
            )

        planned: dict[str, str] = {}

        def _run_runtime_validation() -> dict[str, Any]:
            from lemoncrow.pro.capabilities.review.planned_execution import (
                PlannedExecutionRefused,
                admit_planned_execution,
                execution_wire,
                record_execution_compute,
                surface_entry_id,
            )
            from lemoncrow.pro.capabilities.review.run_plan import ManualPick

            surface, scenario, validation_request, execution = _runtime_validation_selection(
                session,
                revision,
                provider=provider,
                surface_id=surface_id,
                side=side,
            )
            trusted_context, execution_context, execution_runners = execution
            # An explicit validation is a per-Review pick of the trusted-base
            # surface; it runs only when the effective run plan schedules it.
            entry_id = surface_entry_id(surface)
            plan = run_plans.amend(
                session,
                revision,
                [ManualPick(entry_id)],
                ai_candidates=lambda: _ai_candidate_count(session, revision),
            )
            entry = plan.entry(entry_id)
            if entry is None or entry.status != "run":
                reason = entry.reason if entry is not None else "not addressable in the trusted base"
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"runtime validation of {entry_id} is not in the effective run plan: {reason}",
                )
            planned.update(digest=plan.digest, entry=entry_id)

            if runtime_validation_submitter is not None:
                if approve_untrusted_execution:
                    submission = dict(
                        runtime_validation_submitter(
                            session,
                            revision,
                            validation_request,
                            execution_intent,
                            True,
                        )
                    )
                else:
                    submission = dict(
                        runtime_validation_submitter(
                            session,
                            revision,
                            validation_request,
                            execution_intent,
                        )
                    )
                submission_state = str(submission.get("state") or "unavailable").lower()
                observation = submission.get("observation")
                if isinstance(observation, Mapping):
                    try:
                        record = normalize_surface_run(validation_request, scenario, observation)
                        evidence = persist_validation_record(
                            store,
                            revision,
                            validation_request,
                            scenario,
                            record,
                            path=surface.affected_paths[0] if surface.affected_paths else "",
                            source="external",
                            expected_current_revision_id=revision.id if execution_intent == "current" else "",
                            execution_intent=execution_intent,
                            # The server-side submitter (not the runner) states who executed it.
                            executor_class=str(submission.get("executor_class") or ""),
                        )
                    except ValueError as exc:
                        if execution_intent == "current" and "superseded" in str(exc).lower():
                            return _runtime_validation_superseded_response(
                                session,
                                revision,
                                validation_request,
                                detail=str(exc),
                                execution_intent=execution_intent,
                                job=submission,
                            )
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc),
                        ) from exc
                    return _runtime_validation_response(
                        session,
                        revision,
                        validation_request=validation_request,
                        record=record,
                        evidence=evidence,
                        execution_intent=execution_intent,
                    )
                if submission_state == "unavailable":
                    unavailable = SurfaceRunResult(
                        surface.id,
                        surface.provider,
                        side,
                        "unavailable",
                        str(submission.get("detail") or "managed runtime validation is unavailable"),
                        "hosted-job",
                        scenario.runtime_id,
                    )
                    record = normalize_surface_run(validation_request, scenario, unavailable)
                    try:
                        evidence = persist_validation_record(
                            store,
                            revision,
                            validation_request,
                            scenario,
                            record,
                            path=surface.affected_paths[0] if surface.affected_paths else "",
                            source="external",
                            expected_current_revision_id=revision.id if execution_intent == "current" else "",
                            execution_intent=execution_intent,
                        )
                    except ValueError as exc:
                        if execution_intent == "current" and "superseded" in str(exc).lower():
                            return _runtime_validation_superseded_response(
                                session,
                                revision,
                                validation_request,
                                detail=str(exc),
                                execution_intent=execution_intent,
                                job=submission,
                            )
                        raise
                    return _runtime_validation_response(
                        session,
                        revision,
                        validation_request=validation_request,
                        record=record,
                        evidence=evidence,
                        job=submission,
                        execution_intent=execution_intent,
                    )
                return _runtime_validation_response(
                    session,
                    revision,
                    validation_request=validation_request,
                    job=submission,
                    execution_intent=execution_intent,
                )

            # In-process hermetic execution: the plan must place the entry on
            # this registry's executor locus (a queued job is admitted by the
            # worker plane that executes it).
            run_on = str(getattr(execution_runners, "run_on", "") or "local")
            try:
                admit_planned_execution(plan, entry_id, run_on=run_on)
            except PlannedExecutionRefused as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"runtime validation of {entry_id} is not in the effective run plan: {exc}",
                ) from exc
            started = time.monotonic()
            reused = False
            outcome: Any = None
            try:
                outcome = execute_local_surface_validation(
                    store=store,
                    registry=execution_runners,
                    context=execution_context,
                    trusted_config=trusted_context.config,
                    surface=surface,
                    side=side,  # type: ignore[arg-type]
                    policy_digest=validation_request.trusted_policy_digest,
                    expected_current_revision_id=revision.id if execution_intent == "current" else "",
                    execution_intent=execution_intent,
                )
                reused = bool(outcome.reused)
            except ValueError as exc:
                if execution_intent == "current" and "superseded" in str(exc).lower():
                    return _runtime_validation_superseded_response(
                        session,
                        revision,
                        validation_request,
                        detail=str(exc),
                        execution_intent=execution_intent,
                    )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc
            finally:
                if not reused:
                    # In-process execution is metered per catalog entry, executor
                    # locus and stage (fetch/build/run of the execution contract).
                    record_execution_compute(
                        run_plans.compute.recorder(session.id, revision.id),
                        entry_id=entry_id,
                        run_on=run_on,
                        execution=execution_wire(outcome.record) if outcome is not None else None,
                        wall_seconds=time.monotonic() - started,
                        fallback_stage="runtime_validation",
                    )
            return _runtime_validation_response(
                session,
                revision,
                validation_request=outcome.request,
                record=outcome.record,
                evidence=outcome.evidence,
                reused=outcome.reused,
                execution_intent=execution_intent,
            )

        response = await asyncio.to_thread(_run_runtime_validation)
        response["run_plan"] = dict(planned)
        evidence_payload = response.get("evidence")
        if isinstance(evidence_payload, Mapping) and str(evidence_payload.get("verification_status") or ""):
            await _enforce_armed_landing(session)
        return response

    @router.get("/reviews/{review_id}/runtime-validation/jobs/{job_id}")
    async def _get_runtime_validation_job(
        review_id: str,
        job_id: str,
        revision_id: str,
        provider: str,
        surface_id: str,
        side: str = "new",
    ) -> dict[str, Any]:
        session = _session(review_id)
        revision = _requested_revision(session, revision_id)
        surface, scenario, validation_request, _execution = await asyncio.to_thread(
            _runtime_validation_selection,
            session,
            revision,
            provider=provider,
            surface_id=surface_id,
            side=side,
        )
        if runtime_validation_job_lookup is None:
            return {
                "request": validation_request.to_wire(),
                "runtime_validation": {
                    "status": "NOT_RUN",
                    "job": {
                        "state": "unavailable",
                        "detail": "hosted runtime validation job lookup is not configured",
                    },
                },
                "verification": _verification_projection(session, revision),
            }
        job = dict(
            await asyncio.to_thread(
                runtime_validation_job_lookup,
                session,
                revision,
                validation_request,
                job_id,
            )
        )
        job_execution_intent = str(job.get("execution_intent") or "")
        observation = job.get("observation")
        if isinstance(observation, Mapping):
            from lemoncrow.pro.capabilities.review.runtime_validation import (
                normalize_surface_run,
                persist_validation_record,
            )

            try:
                record = normalize_surface_run(validation_request, scenario, observation)
                evidence = await asyncio.to_thread(
                    persist_validation_record,
                    store,
                    revision,
                    validation_request,
                    scenario,
                    record,
                    path=surface.affected_paths[0] if surface.affected_paths else "",
                    source="external",
                    expected_current_revision_id=revision.id if job_execution_intent == "current" else "",
                    execution_intent=job_execution_intent,
                )
            except ValueError as exc:
                if job_execution_intent == "current" and "superseded" in str(exc).lower():
                    return _runtime_validation_superseded_response(
                        session,
                        revision,
                        validation_request,
                        detail=str(exc),
                        execution_intent=job_execution_intent,
                        job=job,
                    )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc
            response = _runtime_validation_response(
                session,
                revision,
                validation_request=validation_request,
                record=record,
                evidence=evidence,
                job=job,
                execution_intent=job_execution_intent,
            )
            if evidence.verification_status:
                await _enforce_armed_landing(session)
            return response
        return _runtime_validation_response(
            session,
            revision,
            validation_request=validation_request,
            job=job,
            execution_intent=job_execution_intent,
        )

    @router.post("/reviews/{review_id}/runtime-validation/jobs/{job_id}/cancel")
    async def _cancel_runtime_validation_job(review_id: str, job_id: str, request: Request) -> dict[str, Any]:
        session = _mutable_session(review_id)
        body = await _json_object(request)
        revision = _requested_revision(session, str(body.get("revision_id") or "").strip())
        provider = str(body.get("provider") or "").strip()
        surface_id = str(body.get("surface_id") or "").strip()
        side = str(body.get("side") or "new").strip()
        if not provider or not surface_id or side not in {"old", "new"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="runtime validation cancellation requires provider, surface_id, and a valid side",
            )
        _surface, _scenario, validation_request, _execution = await asyncio.to_thread(
            _runtime_validation_selection,
            session,
            revision,
            provider=provider,
            surface_id=surface_id,
            side=side,
        )
        if runtime_validation_job_cancel is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="hosted runtime validation cancellation is not configured",
            )
        job = await asyncio.to_thread(
            runtime_validation_job_cancel,
            session,
            revision,
            validation_request,
            job_id,
        )
        return _runtime_validation_response(
            session,
            revision,
            validation_request=validation_request,
            job=dict(job),
        )

    @router.get("/reviews/{review_id}/dependencies")
    def _get_review_dependencies(review_id: str, revision_id: str = "") -> dict[str, Any]:
        session = _session(review_id)
        revision = _requested_revision(session, revision_id)
        return _dependency_projection(session, revision)

    @router.get("/reviews/{review_id}/readiness")
    async def _get_review_readiness(review_id: str) -> dict[str, Any]:
        """Current readiness; an armed intent it no longer supports is revalidated first.

        Readiness can regress with no action on the Review (evidence expiry,
        a policy generation change), so reading it settles an armed landing
        the current answer no longer supports before reporting it. That side
        effect is idempotent (one compare-and-set claim per withdrawal) and
        audited; a read that cannot evaluate a condition reports ``unknown``
        and never withdraws the intent by itself.
        """

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.readiness import definite_blockers

        session = _session(review_id)
        current = await run_in_threadpool(_readiness_projection, session, trust=True)
        landing = current["landing"]
        approval = landing.get("auto_approval") or {}
        # A standing auto-approval this answer may no longer support (P10-LAND L2); review
        # progress is the viewer's own, so it alone never triggers the policy's revalidation.
        # lc-debt: a read revalidates on these triggers only; the hosted sweep catches the rest.
        approval_due = approval.get("state") in {"approving", "approved", "stale"} and (
            approval.get("state") == "stale"
            or "pending" in {approval.get("delivery"), approval.get("provider_cancel")}
            or approval.get("policy_generation") != current["policy_generation"]["digest"]
            or any(row["id"] != "latest-snapshot-reviewed" for row in definite_blockers(current["conditions"]))
        )
        if (landing["armed"] and (not current["armable"] or landing["policy_stale"])) or approval_due:
            if await _enforce_armed_landing(session, origin="read") not in {"none", "stale", "unchanged"}:
                current = await run_in_threadpool(_readiness_projection, session, trust=True)
        await run_in_threadpool(lambda: _note_policy_change(session, current, origin="read"))
        return current

    @router.post("/reviews/{review_id}/landing/arm")
    async def _post_landing_arm(review_id: str, request: Request) -> dict[str, Any]:
        """Explicitly arm provider-native landing for the exact visible conditions."""

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.landing import SATISFIED_CONDITION_STATES

        session = _session(review_id)
        _refuse_hosted_owned(session, "landing and policy satisfaction")
        if session.status == "archived":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="restore this archived Change before arming landing"
            )
        body = await _json_object(request)
        if body.get("armed") is not True:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="landing requires armed=true as an explicit human action",
            )
        current = await run_in_threadpool(_readiness_projection, session)
        revision = _latest(session)
        expected_revision_id = str(body.get("expected_revision_id") or "").strip()
        expected_tree_fingerprint = str(body.get("expected_tree_fingerprint") or "").strip()
        expected_conditions_hash = str(body.get("expected_conditions_hash") or "").strip()
        if not expected_revision_id or not expected_tree_fingerprint or not expected_conditions_hash:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="arming requires expected_revision_id, expected_tree_fingerprint, and expected_conditions_hash from readiness",
            )
        if expected_revision_id != revision.id or expected_tree_fingerprint != revision.tree_fingerprint:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the semantic Snapshot changed since readiness was inspected; reload the landing conditions before arming",
            )
        if expected_conditions_hash != current["conditions_hash"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="landing conditions changed since they were inspected; reload readiness before arming",
            )
        if not current["landing"]["supported"] or effective_landing_delegate is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this Change has no connected SCM landing delegate",
            )
        if not current["armable"]:
            local_blockers = [
                item["label"]
                for item in current["conditions"]
                if item["owner"] != "scm" and item["state"] not in SATISFIED_CONDITION_STATES
            ]
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("LemonCrow-owned landing conditions are not satisfied: " + "; ".join(local_blockers)),
            )

        return await _arm_landing(session, revision, current, {})

    async def _arm_landing(
        session: ReviewSession, revision: ReviewRevision, current: Mapping[str, Any], extra: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Record and delegate one Merge-when-ready intent at the exact reviewed head.

        Shared by the human arm and break-glass (*extra* records its basis and
        bypassed blockers). Landing is always the SCM's own mechanism (native
        auto-merge / merge queue); LemonCrow never merges or bypasses branch
        protection itself.
        """

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.landing import cancel_settlement
        from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

        delegate = effective_landing_delegate
        if delegate is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="this Change has no connected SCM landing delegate"
            )
        context = _change_context_payload(store, session)
        scm = (context.get("scm") or [{}])[0]
        provider = str(current["landing"].get("provider") or scm.get("provider") or "")
        external_id = str(scm.get("external_id") or session.source_ref or "landing")
        metadata: dict[str, Any] = {
            "revision_id": revision.id,
            "revision_number": _display_revision_number(revision),
            "tree_fingerprint": revision.tree_fingerprint,
            "source_fingerprint": revision.source_fingerprint,
            "conditions_hash": current["conditions_hash"],
            "armed_by": session.reviewer_id,
            "conditions": current["conditions"],
            # Durable record of the decision's policy and its time limit: the
            # intent is revalidated when the generation changes and the local
            # watch never lands it after the evidence it relied on expires.
            "policy_generation": current["policy_generation"]["digest"],
            "policy_layers": current["policy_generation"]["layers"],
            "readiness_valid_until": current["valid_until"],
            "evaluated_at": current["evaluated_at"],
            # Break-glass: its basis, record id and the exact bypassed blockers.
            **dict(extra),
        }
        record = await run_in_threadpool(
            store.upsert_relationship,
            ReviewRelationship(
                id="",
                review_id=session.id,
                kind="landing",
                relation="merge_when_ready",
                provider=provider,
                external_id=external_id,
                url=str(scm.get("url") or ""),
                title="Merge when ready",
                status="armed",
                source="human",
                metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        try:
            result = await run_in_threadpool(delegate, session, revision, action="arm")
        except Exception as exc:  # provider boundary: preserve the exact failure, never merge locally
            result = {
                "supported": True,
                "state": "failed",
                "provider": provider,
                "detail": str(exc),
                "url": record.url,
            }
        delegated_state = str(result.get("state") or "failed")
        latest_after_delegate = _latest(session)
        # A merge the provider performed (``merged``: the exact reviewed head;
        # ``blocked``: it reports what it did instead) is history, never a stale
        # intent -- even when a Snapshot was captured while it was being armed.
        if latest_after_delegate.tree_fingerprint != revision.tree_fingerprint and delegated_state not in {
            "merged",
            "blocked",
        }:
            try:
                withdrawal = await run_in_threadpool(delegate, session, revision, action="cancel")
                cancel_state = str(withdrawal.get("state") or "cancelled")
                cancel_detail = str(withdrawal.get("detail") or "")
            except Exception as exc:  # provider boundary: preserve the exact failure
                cancel_state, cancel_detail = "failed", str(exc)
            settled = cancel_settlement(cancel_state)
            if settled in {"merged", "blocked"}:
                # The provider landed before the withdrawal reached it.
                delegated_state = settled
                result = {**dict(result), "state": settled, "detail": cancel_detail}
            else:
                delegated_state = "stale"
                detail = "The semantic Snapshot changed while landing was being armed; "
                if settled == "failed":
                    # The provider may still land what it holds: the cancel
                    # stays owed, retried by the next Reader action (and, on a
                    # local server, the landing watch).
                    metadata["provider_cancel"] = "pending"
                    detail += (
                        f"withdrawing provider landing FAILED: {cancel_detail}. The provider may still merge until "
                        "the withdrawal succeeds; LemonCrow retries it on the next action on this Review."
                    )
                else:
                    detail += "provider landing was cancelled and must be re-armed after review."
                result = {**dict(result), "state": "stale", "detail": detail}
        metadata.update(
            {
                "detail": str(result.get("detail") or ""),
                "mechanism": str(result.get("mechanism") or ""),
                "provider_state": delegated_state,
            }
        )
        await run_in_threadpool(
            store.upsert_relationship,
            ReviewRelationship(
                id="",
                review_id=session.id,
                kind="landing",
                relation="merge_when_ready",
                provider=str(result.get("provider") or provider),
                external_id=external_id,
                url=str(result.get("url") or record.url),
                title="Merge when ready",
                status=delegated_state,
                source="human",
                metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        if delegated_state == "merged":
            await _after_provider_merge(session)
        return await run_in_threadpool(_readiness_projection, session)

    @router.post("/reviews/{review_id}/landing/cancel")
    async def _post_landing_cancel(review_id: str, request: Request) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.landing import (
            ARMED_LANDING_STATES,
            cancel_settlement,
            record_landing_state,
        )
        from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

        # Cancelling is always safe, including on a discarded Change: nothing
        # here can arm or land, only withdraw.
        session = _session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="cancelling merge when ready")
        landing = _landing_relationship(session)
        if landing is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="merge when ready is not armed")
        if landing.status in ARMED_LANDING_STATES:
            # Claim the intent before asking the provider: the landing watch
            # merges only while the intent it read is unchanged, so this write
            # stops a watch merge from racing the human cancel.
            claimed: ReviewRelationship = await run_in_threadpool(
                record_landing_state,
                store,
                landing,
                status=landing.status,
                metadata_updates={"cancel_requested": secrets.token_hex(8)},
            )
            landing = claimed
        result: Mapping[str, Any] = {"state": "cancelled", "detail": "Landing intent cancelled."}
        if effective_landing_delegate is not None:
            try:
                result = await run_in_threadpool(effective_landing_delegate, session, revision, action="cancel")
            except Exception as exc:
                result = {"state": "failed", "detail": str(exc)}
        cancel_state = str(result.get("state") or "cancelled")
        settled = cancel_settlement(cancel_state)
        current = _landing_relationship(session) or landing
        if settled not in {"merged", "blocked"} and (
            current.status == "merged" or (current.status == "blocked" and landing.status != "blocked")
        ):
            # The provider landed before this cancel reached it and that was
            # recorded meanwhile; a merge that happened is never rewritten.
            return await run_in_threadpool(_readiness_projection, session)
        metadata = _landing_metadata(current)
        metadata.pop("cancel_requested", None)
        if settled != "failed" and metadata.get("provider_cancel") == "pending":
            # The provider answered this cancel, so nothing is owed any more.
            metadata["provider_cancel"] = "done"
        metadata.update(
            {
                "detail": str(result.get("detail") or ""),
                "provider_state": cancel_state,
            }
        )
        await run_in_threadpool(
            store.upsert_relationship,
            ReviewRelationship(
                id="",
                review_id=session.id,
                kind="landing",
                relation="merge_when_ready",
                provider=current.provider,
                external_id=current.external_id,
                url=current.url,
                title=current.title or "Merge when ready",
                # A merge the provider already performed cannot be cancelled,
                # and a merge of an unreviewed head is never shown as one.
                status=settled,
                source="human",
                metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        if settled == "merged":
            await _after_provider_merge(session)
        return await run_in_threadpool(_readiness_projection, session)

    @router.post("/reviews/{review_id}/revisions/{revision_number}/publish")
    def _publish_review_snapshot(review_id: str, revision_number: int) -> dict[str, Any]:
        """Mark an explicit CLI capture as a saved Snapshot, including warm reuse."""
        session = _mutable_session(review_id)
        revision = _revision_number(session, revision_number)
        store.publish_snapshot(session.id, revision.id)
        return {"revision_id": revision.id, "revision_number": _display_revision_number(revision)}

    @router.get("/reviews/{review_id}/revisions")
    def _get_revisions(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        revisions = store.list_revisions(session.id)
        state = _revision_state_payload(session)
        reviewed = state["last_reviewed_revision_number"]
        commented = state["last_commented_revision_number"]
        latest = state["latest_revision_number"]
        published_ids = store.published_snapshot_ids(session.id)
        feedback_ids = {
            item.revision_id for item in store.list_annotations(session.id) if item.source in {"human", "ai_review"}
        }
        draft_number_by_revision: dict[str, int] = {}
        draft_number = 0
        for historical in revisions:
            if historical.capture_kind == "commit":
                draft_number = 0
            elif historical.capture_kind == "draft":
                draft_number += 1
                draft_number_by_revision[historical.id] = draft_number

        from lemoncrow.pro.capabilities.review.revisions import snapshot_history_numbers
        from lemoncrow.pro.capabilities.review.store import DerivedResult
        from lemoncrow.pro.capabilities.review.targets import DerivedRefresh, derivation_stamp, derive_review_targets

        # A history row's numbers are a pure function of two immutable Snapshots
        # (this one and the one before it) and the deriving code, so they are
        # derived once, stored with that stamp, and read back in one query.
        # Re-deriving every Snapshot on every call cost a unit read plus a
        # target derivation per Snapshot: 77s for a 335-Snapshot Review.
        stored_history = store.derived_results("history", (item.id for item in revisions))
        fresh_history: list[DerivedResult] = []
        refresh = DerivedRefresh()
        numbers_by_revision: dict[str, Mapping[str, Any]] = {}
        # Rows are derived newest first (the ones a reviewer reads), and a row
        # needs only its own and the previous Snapshot's inputs, so at most two
        # Snapshots' units are held at once.
        loaded: dict[str, tuple[tuple[ReviewUnit, ...], tuple[Any, ...]]] = {}

        def history_inputs(item: ReviewRevision) -> tuple[tuple[ReviewUnit, ...], tuple[Any, ...]]:
            cached = loaded.get(item.id)
            if cached is None:
                units = store.list_units(item.id)
                cached = (units, tuple(derive_review_targets(units, (), read_packet_json(store, item))))
                loaded[item.id] = cached
            return cached

        for index in range(len(revisions) - 1, -1, -1):
            item = revisions[index]
            previous = revisions[index - 1] if index > 0 else None
            scope_key = previous.id if previous is not None else ""
            stamp = derivation_stamp(previous, item)
            hit = stored_history.get((item.id, scope_key))
            if refresh.serve(hit[0] if hit is not None else None, stamp):
                assert hit is not None
                numbers_by_revision[item.id] = hit[1]
                continue
            with refresh.deriving():
                numbers = snapshot_history_numbers(
                    history_inputs(previous) if previous is not None else None, history_inputs(item)
                )
            numbers_by_revision[item.id] = numbers
            fresh_history.append(
                DerivedResult(
                    review_id=session.id,
                    revision_id=item.id,
                    kind="history",
                    scope_key=scope_key,
                    stamp=stamp,
                    payload=numbers,
                )
            )
            # The next (older) row needs only ``previous``.
            for key in [key for key in loaded if previous is None or key != previous.id]:
                del loaded[key]

        references = store.revision_references()
        commits = {commit.commit_sha: commit for commit in store.list_commits(session.id)}

        def history_row(index: int, item: ReviewRevision) -> dict[str, Any]:
            payload = _revision_payload(
                item, store=store, references=references, short_refs=short_references, commits=commits
            )
            number = payload["revision_number"]
            payload["is_latest"] = number == latest
            payload["is_last_reviewed"] = number == reviewed
            payload["is_last_commented"] = number == commented
            payload["has_review_feedback"] = item.id in feedback_ids
            payload["draft_number"] = draft_number_by_revision.get(item.id)
            payload["is_published_snapshot"] = item.id in published_ids
            previous = revisions[index - 1] if index > 0 else None
            payload["previous_revision_number"] = _display_revision_number(previous) if previous is not None else None
            payload["semantic_changed"] = previous is None or item.tree_fingerprint != previous.tree_fingerprint
            payload["source_identity_changed"] = (
                previous is None or item.source_fingerprint != previous.source_fingerprint
            )
            payload["base_moved"] = previous is not None and (
                item.base_sha != previous.base_sha or item.merge_base_sha != previous.merge_base_sha
            )

            # History is a reviewer surface. Keep both the denominator and the
            # delta aligned with the non-overlapping Review areas the Reader
            # shows, rather than the raw file/symbol/hunk units used internally.
            numbers = numbers_by_revision[item.id]
            payload["review_area_count"] = int(numbers["review_area_count"])
            payload["review_area_delta"] = dict(numbers["review_area_delta"])
            payload["delta"] = dict(numbers["delta"])
            return payload

        rows = [history_row(index, item) for index, item in enumerate(revisions)]
        if fresh_history:
            try:
                store.put_derived_results(fresh_history)
            except store.DRIVER_ERRORS:
                # Best effort: this response is unaffected; the next call
                # derives these rows again.
                logging.getLogger(__name__).warning(
                    "could not store Snapshot history for %s", session.id, exc_info=True
                )
        return {"revisions": rows, **state}

    @router.get("/reviews/{review_id}/compare")
    def _compare_revisions(
        review_id: str,
        from_revision: int | None = None,
        to_revision: int | None = None,
    ) -> dict[str, Any]:
        session = _session(review_id)
        return _revision_compare_payload(
            session,
            from_revision_number=from_revision,
            to_revision_number=to_revision,
        )

    @router.get("/reviews/{review_id}/compare/files/{path:path}/contents")
    def _compare_revision_file_contents(
        review_id: str,
        path: str,
        from_revision: int,
        to_revision: int,
    ) -> dict[str, Any]:
        session = _session(review_id)
        return _revision_compare_file_contents(
            session,
            _revision_number(session, from_revision),
            _revision_number(session, to_revision),
            path,
        )

    @router.get("/compare")
    def _compare_sources(from_ref: str, to_ref: str) -> dict[str, Any]:
        """Compare two explicitly named source snapshots without creating a Review.

        The URL-facing comparison surface is intentionally source-oriented, not
        Review-oriented. Review revisions are one source kind alongside Git
        commits/refs and the current index/worktree.
        """

        from lemoncrow.pro.capabilities.review.gitdiff import RevRange, collect_diff, resolve_rev_range

        def review_revision(spec: str) -> tuple[ReviewSession, ReviewRevision] | None:
            if spec.startswith("rr/"):
                if len(spec) <= 3:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty Review revision source"
                    )
                return _revision(spec)
            if spec.startswith("r/"):
                if len(spec) <= 2:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty Review source")
                session = _session(spec)
                return session, _latest(session)
            return None

        def git_spec(spec: str) -> str | None:
            if not spec.startswith("git/"):
                return None
            value = spec[4:]
            if not value:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty Git revision source"
                )
            return value

        def text_map_files(
            left_blobs: Mapping[str, str | None],
            right_blobs: Mapping[str, str | None],
        ) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for path in sorted(set(left_blobs) | set(right_blobs)):
                before = left_blobs.get(path)
                after = right_blobs.get(path)
                if before == after:
                    continue
                file_status = "added" if before is None else "deleted" if after is None else "modified"
                patch = "".join(
                    difflib.unified_diff(
                        (before or "").splitlines(keepends=True),
                        (after or "").splitlines(keepends=True),
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                        n=3,
                    )
                )
                rows.append(
                    {
                        "path": path,
                        "old_path": path,
                        "status": file_status,
                        "additions": sum(
                            1 for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")
                        ),
                        "deletions": sum(
                            1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")
                        ),
                        "patch": patch,
                        "renderable": True,
                        "refusal": "",
                        "detail": "",
                    }
                )
            return rows

        def revision_snapshot(
            revision: ReviewRevision,
            paths: set[str],
        ) -> dict[str, str | None]:
            return _persisted_revision_snapshot(revision, paths)

        left_revision = review_revision(from_ref)
        right_revision = review_revision(to_ref)
        files: list[dict[str, Any]]
        from_label = from_ref
        to_label = to_ref
        from_kind = "review_revision" if left_revision is not None else ""
        to_kind = "review_revision" if right_revision is not None else ""

        if left_revision is not None and right_revision is not None:
            left_session, left = left_revision
            right_session, right = right_revision
            if left_session.repo_root != right_session.repo_root:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Review revisions belong to different repositories"
                )
            left_tree = store.read_source_tree_artifact(left.review_id, left.id, side="new")
            right_tree = store.read_source_tree_artifact(right.review_id, right.id, side="new")
            if left_tree is not None and right_tree is not None:
                compare_paths = {
                    path for path in set(left_tree) | set(right_tree) if left_tree.get(path) != right_tree.get(path)
                }
            else:
                left_blob_artifact = store.read_blob_artifact(left.review_id, left.id)
                right_blob_artifact = store.read_blob_artifact(right.review_id, right.id)
                if left_blob_artifact is None and left_tree is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="the left Review revision predates persisted source snapshots",
                    )
                if right_blob_artifact is None and right_tree is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="the right Review revision predates persisted source snapshots",
                    )
                compare_paths = (
                    set(left_blob_artifact or {})
                    | set(right_blob_artifact or {})
                    | _revision_packet_paths(left)
                    | _revision_packet_paths(right)
                )
            files = text_map_files(
                revision_snapshot(left, compare_paths),
                revision_snapshot(right, compare_paths),
            )
            from_label = f"rev {_display_revision_number(left)}"
            to_label = f"rev {_display_revision_number(right)}"
        elif (left_revision is not None and git_spec(to_ref) is not None) or (
            right_revision is not None and git_spec(from_ref) is not None
        ):
            from lemoncrow.pro.capabilities.review.gitdiff import _blob_from_tree, _decode, _open_repo, _tree_for_sha

            snapshot_pair = left_revision if left_revision is not None else right_revision
            if snapshot_pair is None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing Review revision")
            snapshot_session, snapshot_revision = snapshot_pair
            if snapshot_session.repo_root != str(resolved_source_repo):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Snapshot belongs to another repository"
                )
            git_ref = git_spec(to_ref if left_revision is not None else from_ref)
            if git_ref is None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing Git source")
            try:
                git_range = resolve_rev_range(resolved_source_repo, base=git_ref, head=git_ref)
                repo = _open_repo(resolved_source_repo)
                git_tree = _tree_for_sha(repo, git_range.head_sha)
                base_diff = collect_diff(
                    resolved_source_repo,
                    RevRange(
                        mode="commit_range",
                        base_rev=git_ref,
                        head_rev=snapshot_revision.base_sha,
                        base_sha=git_range.head_sha,
                        head_sha=snapshot_revision.base_sha,
                    ),
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
            compare_paths = _revision_packet_paths(snapshot_revision)
            for changed in base_diff.files:
                compare_paths.add(changed.path)
                if changed.old_path:
                    compare_paths.add(changed.old_path)
            git_snapshot: dict[str, str | None] = {}
            for path in compare_paths:
                try:
                    git_tree / path
                except KeyError:
                    git_snapshot[path] = None
                    continue
                payload, reason = _blob_from_tree(repo, git_tree, path)
                if payload is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Git content unavailable for {path}: {reason}",
                    )
                git_snapshot[path] = _decode(payload)
            frozen_snapshot = revision_snapshot(snapshot_revision, compare_paths)
            if left_revision is not None:
                files = text_map_files(frozen_snapshot, git_snapshot)
                from_label, to_label = f"Snapshot {_display_revision_number(snapshot_revision)}", git_ref
                from_kind, to_kind = "review_revision", "git"
            else:
                files = text_map_files(git_snapshot, frozen_snapshot)
                from_label, to_label = git_ref, f"Snapshot {_display_revision_number(snapshot_revision)}"
                from_kind, to_kind = "git", "review_revision"
        elif left_revision is not None and to_ref in {"worktree", "index"}:
            left_session, left = left_revision
            left_blobs = store.read_blob_artifact(left.review_id, left.id)
            if left_blobs is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="the Review revision predates persisted source snapshots",
                )
            if not left.base_sha:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="the Review revision has no Git base for live-source comparison",
                )
            live_mode: Literal["working_tree", "staged"] = "working_tree" if to_ref == "worktree" else "staged"
            rng = RevRange(
                mode=live_mode,
                base_rev=left.base_sha,
                head_rev="WORKDIR" if live_mode == "working_tree" else "INDEX",
                base_sha=left.base_sha,
                head_sha="",
                dirty=True,
                title=f"rev {_display_revision_number(left)} → {to_ref}",
            )
            from lemoncrow.pro.capabilities.review.gitdiff import load_blobs

            diff = collect_diff(resolved_source_repo, rng, with_patch_text=True)
            current = load_blobs(resolved_source_repo, rng, diff.files)
            current_paths = {item.path for item in diff.files}
            compare_paths = set(left_blobs) | current_paths
            current_snapshot: dict[str, str | None] = {}
            current_new = dict(current.new)
            base_snapshot = revision_snapshot(left, compare_paths)
            deleted_current = {item.path for item in diff.files if item.status == "deleted"}
            for path in compare_paths:
                if path in deleted_current:
                    current_snapshot[path] = None
                elif path in current_new:
                    current_snapshot[path] = current_new[path]
                else:
                    current_snapshot[path] = base_snapshot.get(path)
            files = text_map_files(
                revision_snapshot(left, compare_paths),
                current_snapshot,
            )
            from_kind, to_kind = "review_revision", to_ref
            from_label = f"rev {_display_revision_number(left)}"
            to_label = to_ref
        else:
            left_git = git_spec(from_ref)
            right_git = git_spec(to_ref)
            if left_git is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="left source must be r/<review>, rr/<revision>, or git/<ref>",
                )
            if right_git is not None:
                try:
                    rng = resolve_rev_range(resolved_source_repo, base=left_git, head=right_git)
                except ValueError as exc:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
                from_kind = to_kind = "git"
                from_label, to_label = left_git, right_git
            elif to_ref in {"worktree", "index"}:
                try:
                    base_rng = resolve_rev_range(resolved_source_repo, base=left_git, head=left_git)
                except ValueError as exc:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
                target_mode: Literal["working_tree", "staged"] = "working_tree" if to_ref == "worktree" else "staged"
                rng = RevRange(
                    mode=target_mode,
                    base_rev=left_git,
                    head_rev="WORKDIR" if target_mode == "working_tree" else "INDEX",
                    base_sha=base_rng.base_sha,
                    head_sha="",
                    dirty=True,
                    title=f"{left_git} → {to_ref}",
                )
                from_kind, to_kind = "git", to_ref
                from_label, to_label = left_git, to_ref
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="right source must be r/<review>, rr/<revision>, git/<ref>, worktree, or index",
                )

            diff = collect_diff(resolved_source_repo, rng, with_patch_text=True)
            files = []
            for item in diff.files:
                entry = {
                    "path": item.path,
                    "old_path": item.old_path,
                    "status": item.status,
                    "is_binary": item.is_binary,
                    "submodule_pointer": item.submodule_pointer,
                    "diff_omitted_reason": item.diff_omitted_reason,
                    "hunks": [{"header": hunk.header, "patch": hunk.patch} for hunk in item.hunks],
                }
                patch, refusal, detail = synthesize_file_patch(entry)
                files.append(
                    {
                        "path": item.path,
                        "old_path": item.old_path or item.path,
                        "status": item.status,
                        "additions": item.additions,
                        "deletions": item.deletions,
                        "patch": patch,
                        "renderable": not refusal,
                        "refusal": refusal,
                        "detail": detail,
                    }
                )

        return {
            "from": {"spec": from_ref, "label": from_label, "kind": from_kind},
            "to": {"spec": to_ref, "label": to_label, "kind": to_kind},
            "summary": {
                "files": len(files),
                "additions": sum(int(item["additions"]) for item in files),
                "deletions": sum(int(item["deletions"]) for item in files),
            },
            "files": files,
        }

    @router.get("/reviews/{review_id}/activity")
    def _get_review_activity(review_id: str, limit: int = 500) -> dict[str, Any]:
        session = _session(review_id)
        events = store.list_activity_events(session.id, limit=limit)
        return {"review_id": session.id, "events": [_activity_payload(item) for item in events]}

    @router.get("/reviews/{review_id}/triage")
    def _get_review_triage(review_id: str) -> dict[str, Any]:
        """The lightweight per-Review triage projection; reads no Review body."""

        from lemoncrow.pro.capabilities.review.review_triage import PRIORITY_OVERRIDE_KINDS, load_review_triage

        session = _session(review_id)
        revision = _latest(session)
        events = store.list_activity_events(session.id, kinds=PRIORITY_OVERRIDE_KINDS, newest_first=True, limit=50)
        triage = load_review_triage(store, review_id=session.id, revision_id=revision.id, events=events)
        return {"triage": triage.to_wire()}

    async def _priority_override(review_id: str, request: Request, *, clear: bool) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.review_triage import (
            PRIORITY_OVERRIDE_KINDS,
            PriorityOverrideRefused,
            load_review_triage,
            priority_override_clear_event,
            priority_override_event,
        )

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="overriding priority")
        # The Reader route is a human action; a caller declaring itself an agent is refused.
        actor_type = str(body.get("actor_type") or "human")
        try:
            event = (
                priority_override_clear_event(
                    review_id=session.id,
                    revision_id=revision.id,
                    reason=str(body.get("reason") or ""),
                    actor_id=session.reviewer_id,
                    actor_type=cast(Any, actor_type),
                )
                if clear
                else priority_override_event(
                    review_id=session.id,
                    revision_id=revision.id,
                    level=str(body.get("level") or ""),
                    reason=str(body.get("reason") or ""),
                    scope=str(body.get("scope") or "review"),
                    actor_id=session.reviewer_id,
                    actor_type=cast(Any, actor_type),
                )
            )
        except PriorityOverrideRefused as exc:
            code = status.HTTP_403_FORBIDDEN if exc.code == "not_human" else status.HTTP_422_UNPROCESSABLE_ENTITY
            raise HTTPException(status_code=code, detail=exc.detail) from exc
        recorded = store.record_activity(event)
        events = store.list_activity_events(session.id, kinds=PRIORITY_OVERRIDE_KINDS, newest_first=True, limit=50)
        triage = load_review_triage(store, review_id=session.id, revision_id=revision.id, events=events)
        return {"event": _activity_payload(recorded), "triage": triage.to_wire()}

    @router.put("/reviews/{review_id}/priority-override")
    async def _put_priority_override(review_id: str, request: Request) -> dict[str, Any]:
        """Record an audited human priority override (level, reason, scope review|snapshot)."""

        return await _priority_override(review_id, request, clear=False)

    @router.post("/reviews/{review_id}/priority-override/clear")
    async def _clear_priority_override(review_id: str, request: Request) -> dict[str, Any]:
        """Close the active override with an audited event; computed priority resumes."""

        return await _priority_override(review_id, request, clear=True)

    @router.get("/reviews/{review_id}/targets/{unit_key:path}/history")
    def _get_target_history(review_id: str, unit_key: str) -> dict[str, Any]:
        session = _session(review_id)
        events = store.list_mark_events(session.id, reviewer_id=session.reviewer_id, unit_key=unit_key)
        known = any(
            unit.unit_key == unit_key
            for revision in store.list_revisions(session.id)
            for unit in store.list_units(revision.id)
        )
        if not events and not known:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such review target in this review")
        return {
            "review_id": session.id,
            "reviewer_id": session.reviewer_id,
            "unit_key": unit_key,
            "events": [_mark_event_payload(item) for item in events],
        }

    @router.get("/annotations/{annotation_id}/versions")
    def _get_annotation_versions(annotation_id: str) -> dict[str, Any]:
        annotation = store.get_annotation(annotation_id)
        if annotation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such comment")
        _session(annotation.review_id)
        versions = store.list_annotation_versions(annotation.id)
        return {
            "annotation_id": annotation.id,
            "versions": [_annotation_version_payload(item) for item in versions],
        }

    def _source_state_projection(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        if not source_refresh_enabled:
            return {
                "supported": False,
                "changed": False,
                "fingerprint": revision.source_fingerprint,
                "path_count": 0,
                "paths": [],
                "reason": "this Review has no trusted mutable source bound to the server; run lc review to publish a new revision",
            }
        if session.range_mode not in ("working_tree", "staged"):
            return {
                "supported": False,
                "changed": False,
                "fingerprint": revision.source_fingerprint,
                "path_count": 0,
                "paths": [],
                "reason": "source change detection currently applies to working-tree and staged reviews",
            }
        if not revision.source_fingerprint:
            return {
                "supported": False,
                "changed": False,
                "fingerprint": "",
                "path_count": 0,
                "paths": [],
                "reason": "this revision predates automatic change detection; refresh once to establish a baseline",
            }

        from lemoncrow.pro.capabilities.review.gitdiff import source_state
        from lemoncrow.pro.capabilities.review.sources.local import range_for_session, source_paths_since_revision

        try:
            rng = range_for_session(resolved_source_repo, session)
            current = source_state(resolved_source_repo, rng)
            changed = current.fingerprint != revision.source_fingerprint
            paths = (
                source_paths_since_revision(store, resolved_source_repo, revision, rng, current.paths)
                if changed
                else ()
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return {
                "supported": False,
                "changed": False,
                "fingerprint": "",
                "path_count": 0,
                "paths": [],
                "reason": f"could not inspect the current review source: {exc}",
            }
        return {
            "supported": True,
            "changed": changed,
            "fingerprint": current.fingerprint,
            "path_count": len(paths),
            "paths": list(paths),
            "reason": "",
        }

    # lc-debt: one lock serializes probes across reviews; per-review locks if that shows up.
    _source_probe_lock = threading.Lock()
    _source_probe_memo: dict[str, tuple[float, dict[str, Any]]] = {}

    @router.get("/reviews/{review_id}/source-state")
    def _get_source_state(review_id: str) -> dict[str, Any]:
        """Cheap, read-only detection that a local review has newer source.

        This route never snapshots or reconciles. A mismatch is only an offer
        to the human; ``POST /refresh`` remains the one door that advances the
        ReviewRevision and reopens marks/comments.
        """

        session = _session(review_id)
        revision = _latest(session)
        # Every open Reader tab probes this every couple of seconds and each probe
        # reads the working tree (seconds on a large repo). Probes are serialized
        # and one that arrived while another was running reuses that answer, so
        # polling cannot pile up and starve real actions. A probe that starts
        # after an edit still recomputes.
        arrived = time.monotonic()
        key = f"{session.id}:{revision.id}"
        with _source_probe_lock:
            hit = _source_probe_memo.get(key)
            if hit is not None and hit[0] >= arrived:
                return hit[1]
            started = time.monotonic()
            result = _source_state_projection(session, revision)
            _source_probe_memo[key] = (started, result)
            while len(_source_probe_memo) > 64:
                _source_probe_memo.pop(next(iter(_source_probe_memo)))
            return result

    def _review_live_state(review_id: str) -> dict[str, Any]:
        """One lightweight cursor spanning mutable Reader context.

        The cursor intentionally fingerprints metadata, never source/comment
        contents. Existing exact endpoints remain authoritative and are fetched
        only when their component cursor advances.
        """

        session = _session(review_id)
        revision = _latest(session)
        comment_ingestor.sync(session)
        annotations = _ordered_annotations(
            (*store.list_annotations(session.id), *store.list_discussion_comments(session.id))
        )
        annotation_rows = [
            (
                item.id,
                item.revision_id,
                item.updated_at,
                item.state,
                item.author_response,
                item.author_response_source_id,
                item.anchor_method,
            )
            for item in annotations
        ]
        annotations_cursor = hashlib.sha256(
            json.dumps(annotation_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]

        # Judgments made in another tab of this reviewer (a dedicated file tab)
        # reach every open Reader through this component.
        mark_rows = sorted(
            (mark.unit_key, mark.state, mark.updated_at)
            for mark in store.list_marks(session.id, reviewer_id=session.reviewer_id)
        )
        marks_cursor = hashlib.sha256(
            json.dumps(mark_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]

        journal = _journal_projection(session, revision, include_live=True)
        journal_rows = [
            (
                str(item.get("id") or ""),
                str(item.get("at") or ""),
                str(item.get("kind") or ""),
                str(item.get("status") or ""),
                str(item.get("summary") or ""),
            )
            for item in journal.get("activity", [])
            if isinstance(item, Mapping)
        ]
        journal_cursor = hashlib.sha256(
            json.dumps(
                {
                    "rows": journal_rows,
                    "updated_at": str(journal.get("updated_at") or ""),
                    "status": str(journal.get("status") or ""),
                    "task": str(journal.get("task") or ""),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]

        # Development advances only when an event is appended; ingestion is
        # throttled and incremental, so an idle poll costs a few stats.
        _development_sync(session)
        components = {
            "revision_id": revision.id,
            "packet_cursor": revision.packet_sha256,
            "annotations_cursor": annotations_cursor,
            "marks_cursor": marks_cursor,
            "journal_cursor": journal_cursor,
            "development_cursor": _development_cursor(session),
        }
        cursor = hashlib.sha256(
            json.dumps(components, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32]
        return {
            **components,
            "cursor": cursor,
        }

    def _event_roots(*paths: Path) -> list[Path]:
        roots: list[Path] = []
        for path in paths:
            path = path.resolve()
            if path.is_dir() and not any(path == root or root in path.parents for root in roots):
                roots = [root for root in roots if path not in root.parents]
                roots.append(path)
        return roots

    @router.get("/reviews/{review_id}/source-state/events")
    async def _source_events(review_id: str) -> StreamingResponse:
        from lemoncrow.pro.capabilities.review.gitdiff import _open_repo
        from lemoncrow.pro.capabilities.review.state_events import state_events

        session = _session(review_id)
        _latest(session)  # Validate scope and identity before starting the response.
        roots = [resolved_source_repo]
        if source_refresh_enabled:
            with contextlib.suppress(OSError, ValueError):
                git_dir = Path(_open_repo(resolved_source_repo).path)
                roots.append(git_dir)
                common = git_dir / "commondir"
                if common.is_file():
                    roots.append((git_dir / common.read_text().strip()).resolve())

        def relevant(_change: Any, raw: str) -> bool:
            path = Path(raw)
            if store.root == path or store.root in path.parents:
                return False
            if ".git" in path.parts:
                return path.name in {"index", "HEAD", "packed-refs"} or "refs" in path.parts
            return True

        return StreamingResponse(
            state_events(
                _event_roots(*roots),
                lambda _: _get_source_state(review_id),
                event="source-state",
                watch_filter=relevant,
                settle=True,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @router.get("/reviews/{review_id}/live-state/events")
    async def _live_events(review_id: str) -> StreamingResponse:
        from lemoncrow.pro.capabilities.review.state_events import state_events

        _latest(_session(review_id))

        def project(changes: set[tuple[Any, str]]) -> dict[str, Any]:
            ledgers = [Path(raw) for _, raw in changes if Path(raw).name == "run.json"]
            if ledgers:
                comment_ingestor.sync(_session(review_id), paths=ledgers)
            if development_ingestor is not None and any(Path(raw).name == "run.json" for _, raw in changes):
                with contextlib.suppress(Exception):
                    development_ingestor.sync(_session(review_id), force=True)
            return {**_review_live_state(review_id), "changed": True}

        return StreamingResponse(
            state_events(
                _event_roots(store.root, resolved_source_store),
                project,
                event="live-state",
                watch_filter=lambda _, raw: not raw.endswith((".db-shm", ".lock")),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @router.get("/reviews/{review_id}/live-state")
    async def _get_review_live_state(review_id: str, cursor: str = "", wait_ms: int = 10_000) -> dict[str, Any]:
        """Bounded authenticated long-poll for Reader invalidation cursors."""

        if len(cursor) > 128:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="live cursor is too long")
        bounded_wait_ms = max(0, min(int(wait_ms), 15_000))
        # Ledger stats and SQLite reads stay off the event loop.
        current = await asyncio.to_thread(_review_live_state, review_id)
        if not cursor or current["cursor"] != cursor or bounded_wait_ms == 0:
            return {**current, "changed": bool(not cursor or current["cursor"] != cursor)}

        deadline = asyncio.get_running_loop().time() + (bounded_wait_ms / 1000.0)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return {**current, "changed": False}
            await asyncio.sleep(min(0.75, remaining))
            current = await asyncio.to_thread(_review_live_state, review_id)
            if current["cursor"] != cursor:
                return {**current, "changed": True}

    def _targets_payload(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        order: str,
        historical: bool,
        startup: bool = False,
        path: str = "",
    ) -> dict[str, Any]:
        if order not in {"recommended", "file"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="order must be 'recommended' or 'file' in the R20 reader projection",
            )
        units = store.list_units(revision.id)
        marks = (
            _marks_visible_at(session, revision)
            if historical
            else store.list_marks(session.id, reviewer_id=session.reviewer_id)
        )
        annotations = _annotations_at(session, revision, historical=historical)
        packet = read_packet_json(store, revision)
        frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            revision,
            units,
            marks,
            annotations=annotations,
        )
        from lemoncrow.pro.capabilities.review.targets import (
            build_review_outline,
            order_review_targets,
            outline_payload,
            progress_payload,
            review_progress,
            target_payload,
        )

        targets = _derive_targets(session, revision, units, frontier.entries, packet, annotations)
        targets = order_review_targets(targets, order=order)  # type: ignore[arg-type]
        progress = review_progress(targets)
        startup_compact = bool(startup and not historical and progress.target_count > LARGE_REVIEW_TARGET_THRESHOLD)
        path_scope = path.strip()
        if path_scope:
            visible_targets = tuple(target for target in targets if target.path == path_scope)
        elif startup_compact:
            visible_targets = targets[:LARGE_REVIEW_STARTUP_TARGET_LIMIT]
        else:
            visible_targets = targets

        changed_lines_by_path: dict[str, int] = {}
        for target in targets:
            changed_lines_by_path[target.path] = (
                changed_lines_by_path.get(target.path, 0) + max(0, target.additions) + max(0, target.deletions)
            )

        if startup_compact and not path_scope:
            outline_rows = build_review_outline(targets)
            outline_payloads = [
                {
                    "path": item.path,
                    "target_count": item.target_count,
                    "reviewed": item.reviewed,
                    "changed_since_review": item.changed_since_review,
                    "needs_changes": item.needs_changes,
                    "unreviewed": item.unreviewed,
                    "unknown": item.unknown,
                    "mechanical": item.mechanical,
                    "attention_rank": item.attention_rank,
                    "reasons": [],
                    "section": item.section,
                    "position": item.position,
                    "changed_lines": changed_lines_by_path.get(item.path, 0),
                }
                for item in outline_rows
            ]
        else:
            outline_rows = build_review_outline(visible_targets)
            outline_payloads = [outline_payload(item) for item in outline_rows]

        payloads = [target_payload(target) for target in visible_targets]
        movement: dict[str, Any] | None = None
        if not historical and not startup_compact and not path_scope:
            payloads, movement = _movement_projection(session, revision, payloads)
        return {
            "revision_id": revision.id,
            "historical": historical,
            "order": order,
            "targets": payloads,
            "progress": progress_payload(progress),
            "outline": outline_payloads,
            "movement": movement,
            "complete": not startup_compact and not path_scope,
        }

    @router.get("/reviews/{review_id}/targets")
    def _get_targets(
        review_id: str,
        order: str = "recommended",
        startup: bool = False,
        path: str = "",
    ) -> dict[str, Any]:
        """Reader metadata: full, startup-bounded, or one-file target projection."""

        session = _session(review_id)
        return _targets_payload(
            session,
            _latest(session),
            order=order,
            historical=False,
            startup=startup,
            path=path,
        )

    @router.get("/reviews/{review_id}/revisions/{revision_number}/targets")
    def _get_historical_targets(review_id: str, revision_number: int, order: str = "recommended") -> dict[str, Any]:
        session = _session(review_id)
        return _targets_payload(
            session,
            _revision_number(session, revision_number),
            order=order,
            historical=True,
        )

    def _guidance_payload(session: ReviewSession, revision: ReviewRevision, *, historical: bool) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.guidance import guidance_view_payload

        units = store.list_units(revision.id)
        annotations = _annotations_at(session, revision, historical=historical)
        marks = (
            _marks_visible_at(session, revision)
            if historical
            else store.list_marks(session.id, reviewer_id=session.reviewer_id)
        )
        packet = read_packet_json(store, revision)
        frontier = compute_frontier(session.id, session.reviewer_id, revision, units, marks, annotations=annotations)
        targets = _derive_targets(session, revision, units, frontier.entries, packet, annotations)
        view = _guidance_view_for(session, revision, targets, units, packet)
        return {"revision_id": revision.id, "historical": historical, **guidance_view_payload(view)}

    @router.get("/reviews/{review_id}/guidance")
    def _get_guidance(review_id: str) -> dict[str, Any]:
        """Author guidance resolved against the current Snapshot; never a human state."""

        session = _session(review_id)
        return _guidance_payload(session, _latest(session), historical=False)

    @router.get("/reviews/{review_id}/revisions/{revision_number}/guidance")
    def _get_historical_guidance(review_id: str, revision_number: int) -> dict[str, Any]:
        session = _session(review_id)
        return _guidance_payload(session, _revision_number(session, revision_number), historical=True)

    def _brief_plan(session: ReviewSession, revision: ReviewRevision, *, historical: bool) -> tuple[Any, ...]:
        """``(plan, ordered targets, guidance view, decision states, plan checks)`` for one Snapshot."""

        from lemoncrow.pro.capabilities.review.chapters import build_chapter_lenses
        from lemoncrow.pro.capabilities.review.review_plan import apply_agent_context, build_review_plan
        from lemoncrow.pro.capabilities.review.session_comments import brief_agent_context
        from lemoncrow.pro.capabilities.review.targets import derive_review_targets, order_review_targets

        units = store.list_units(revision.id)
        annotations = _annotations_at(session, revision, historical=historical)
        marks = (
            _marks_visible_at(session, revision)
            if historical
            else store.list_marks(session.id, reviewer_id=session.reviewer_id)
        )
        packet = read_packet_json(store, revision)
        frontier = compute_frontier(session.id, session.reviewer_id, revision, units, marks, annotations=annotations)
        base = derive_review_targets(
            units, frontier.entries, packet, annotations=annotations, evidence=_current_evidence(session, revision)
        )
        targets, view, proofs, states = _with_plan_inputs(session, revision, units, packet, base, historical=historical)
        ordered = order_review_targets(targets)
        groups, _counts = build_groups(units, frontier.entries, packet, targets=ordered)
        chapters = build_chapter_lenses(
            [row for group in groups for row in group["rows"]],
            packet,
            repo_root=resolved_repo,
            base_sha=revision.base_sha,
            head_sha=revision.head_sha,
            targets=ordered,
        )
        plan_checks = _check_rows(session, revision, discover=True)
        plan = build_review_plan(
            targets=ordered,
            guidance=view,
            checks=plan_checks,
            chapters=chapters.get("intent", []),
            routine_proofs=proofs,
            snapshot_number=max(0, revision.revision_number - 1),
            handled_decisions={state.key for state in states},
        )
        plan = apply_agent_context(
            plan,
            brief_agent_context(store, session, revision, historical=historical),
        )
        return plan, ordered, view, states, plan_checks

    _AI_REVIEW_CAPABILITIES = frozenset(
        {"hypothesis_generation", "finding_investigation", "finding_dedupe", "rule_check"}
    )

    def _ai_review_runs(session: ReviewSession, revision: ReviewRevision) -> tuple[Any, ...]:
        return tuple(
            row
            for row in store.list_intelligence_runs(session.id, revision_id=revision.id)
            if row.capability in _AI_REVIEW_CAPABILITIES
        )

    def _ai_independence(
        session: ReviewSession,
        revision: ReviewRevision,
        runs: Sequence[Any] | None = None,
        queued_job: Mapping[str, Any] | None = None,
        *,
        lookup: bool = True,
    ) -> tuple[Any, Any]:
        """P11 item 14: the effective ``independent_ai_reviewer`` policy and the provenance-backed assessment."""

        from lemoncrow.pro.capabilities.review.independence import assess_revision

        if lookup and queued_job is None and ai_review_job_lookup is not None:
            queued_job = ai_review_job_lookup(session, revision)
        trace = queued_job.get("trace") if isinstance(queued_job, Mapping) else None
        rows = _ai_review_runs(session, revision) if runs is None else runs
        return run_plans.independence_policy(session, revision), assess_revision(
            session, revision, rows, trace if isinstance(trace, Mapping) else None
        )

    def _ai_assurance(
        session: ReviewSession,
        revision: ReviewRevision,
        runs: Sequence[Any],
        queued_job: Mapping[str, Any] | None,
        *,
        plan: Any,
        state: str,
        historical: bool,
        provider: str,
        model: str,
        route_trust: str,
        calls_used: int,
        max_calls: int,
    ) -> dict[str, Any]:
        """Observed identity, independence and fresh re-run preview for Run details (P11 items 14-15)."""

        from lemoncrow.pro.capabilities.review.fresh_rerun import execution_summary, fresh_rerun_preview
        from lemoncrow.pro.capabilities.review.independence import independence_block
        from lemoncrow.pro.capabilities.review.intelligence_runtime import run_identity
        from lemoncrow.pro.capabilities.review.model_identity import ModelIdentity

        policy, assessment = _ai_independence(session, revision, runs, queued_job, lookup=False)
        latest_ready = next((row for row in runs if row.status == "ready"), None)
        settled = state in {"ready", "failed", "cancelled"}
        reason = (
            "a fresh re-run applies to the current Snapshot only"
            if historical
            else "" if settled else "AI review has not settled on this Snapshot yet"
        )
        return {
            "identity": None if latest_ready is None else run_identity(latest_ready).to_wire(),
            "independence": independence_block(policy, assessment),
            "execution": execution_summary(runs),
            "fresh_rerun": fresh_rerun_preview(
                plan,
                calls_used=calls_used,
                max_calls=max_calls,
                provider=provider,
                model_label=ModelIdentity(provider, model).label if provider else "",
                model_data_locus=route_trust,
                available=not historical and settled,
                reason=reason,
            ),
        }

    def _current_ai_annotations(session: ReviewSession, revision: ReviewRevision) -> tuple[Any, ...]:
        anchored_here = store.annotations_anchored_at(revision.id)
        return tuple(
            annotation
            for annotation in store.list_annotations(session.id)
            if annotation.source == "ai_review"
            and annotation.state == "open"
            and (annotation.revision_id == revision.id or annotation.id in anchored_here)
        )

    def _ai_review_projection(
        session: ReviewSession,
        revision: ReviewRevision,
        targets: Sequence[Any],
        *,
        historical: bool,
    ) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.ai_review import generate_candidates
        from lemoncrow.pro.capabilities.review.data_governance import process_route_trust
        from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallBudget
        from lemoncrow.pro.capabilities.review.rules import content_access_for_path
        from lemoncrow.pro.capabilities.review.run_plan import ai_stage_plan

        policy = _effective_intelligence_policy(session, revision)
        governance = _review_governance(session, revision)
        runs = _ai_review_runs(session, revision)
        latest = runs[0] if runs else None
        annotations = _current_ai_annotations(session, revision)
        provider_error = ""
        resolved_driver = ""
        resolved_model = ""
        route_trust: ModelRouteTrust = "unknown"
        try:
            resolved = _resolve_route(session, revision, None, None)
            resolved_driver, resolved_model = resolved.driver, resolved.model
            route_trust = process_route_trust(resolved.driver)
        except Exception as exc:
            provider_error = f"{type(exc).__name__}: {exc}"
        access = {
            target.target_id: content_access_for_path(
                target.path,
                governance.content_access,
                route_trust=route_trust,
            )
            for target in targets
        }
        allowed_targets = tuple(target for target in targets if access[target.target_id].allowed)
        excluded = tuple(
            {
                "target_id": target.target_id,
                "path": target.path,
                "mode": access[target.target_id].mode,
                "reason": access[target.target_id].reason,
            }
            for target in targets
            if not access[target.target_id].allowed
        )
        candidates = generate_candidates(
            allowed_targets,
            rules=governance.rules,
            critical_rules=_critical_ai_rules(policy),
        )

        run_plan = run_plans.current(session, revision, ai_candidates=len(candidates), persist=not historical)
        stage = ai_stage_plan(run_plan)
        queued_job = ai_review_job_lookup(session, revision) if ai_review_job_lookup is not None else None
        queued_state = "" if queued_job is None else str(queued_job.get("status") or "")
        if any(row.status == "pending" for row in runs):
            state = "pending"
        elif not runs and queued_state in {"queued", "running", "cancel_requested"}:
            state = "pending"
        elif not runs and queued_state == "failed":
            state = "failed"
        elif not runs and queued_state == "cancelled":
            state = "cancelled"
        elif not runs and not candidates and not stage.rule_checks:
            state = "skipped_unnecessary"
        elif any(row.status == "ready" for row in runs):
            state = "ready"
        elif any(row.status == "failed" for row in runs) or provider_error:
            state = "failed"
        elif any(row.status == "cancelled" for row in runs):
            state = "cancelled"
        else:
            state = "none"
        effort = stage.effort
        budget = ModelCallBudget.from_env(max_calls=1)
        error = provider_error or (
            "" if latest is None or latest.status not in {"failed", "cancelled"} else latest.error
        )
        projection = {
            "status": state,
            "effort": effort,
            "candidates": len(candidates),
            "findings": len(annotations),
            "provider": "" if latest is None else latest.driver,
            "model": "" if latest is None else latest.model,
            "resolved_provider": resolved_driver,
            "resolved_model": resolved_model,
            "error": error,
            "auto_eligible": bool(
                not historical
                and state == "none"
                and not provider_error
                and ((candidates and stage.investigators) or stage.rule_checks)
            ),
            "run_plan": {"digest": run_plan.digest, "ai": stage.to_wire()},
            "budget": {
                "calls_used": store.intelligence_calls(session.id),
                "max_calls": budget.max_review_calls,
            },
            "skipped_by_budget": sum(
                1 for row in runs if row.status == "failed" and "budget exhausted" in row.error.lower()
            ),
            "coverage": {
                "human_deterministic_targets": len(targets),
                "ai_context_eligible_targets": len(allowed_targets),
                "excluded_by_policy": list(excluded[:64]),
                "excluded_count": len(excluded),
            },
            "rules": {
                "applicable_count": len(governance.rules_for_paths([target.path for target in allowed_targets])),
                "base_state": governance.base_state,
                "errors": list(governance.errors),
            },
        }
        if queued_job is not None:
            projection["execution"] = dict(queued_job)
        projection["assurance"] = _ai_assurance(
            session,
            revision,
            runs,
            queued_job,
            plan=run_plan,
            state=state,
            historical=historical,
            provider=resolved_driver,
            model=resolved_model,
            route_trust=route_trust,
            calls_used=store.intelligence_calls(session.id),
            max_calls=budget.max_review_calls,
        )
        return projection

    def _ai_candidate_count(
        session: ReviewSession, revision: ReviewRevision, targets: Sequence[Any] | None = None
    ) -> int:
        """Deterministic AI triage candidates on policy-allowed context (the Auto escalation signal)."""

        from lemoncrow.pro.capabilities.review.ai_review import generate_candidates
        from lemoncrow.pro.capabilities.review.data_governance import process_route_trust
        from lemoncrow.pro.capabilities.review.rules import content_access_for_path

        if targets is None:
            _plan, targets, _view, _states, _checks = _brief_plan(session, revision, historical=False)
        governance = _review_governance(session, revision)
        route_trust: ModelRouteTrust = "unknown"
        with contextlib.suppress(Exception):
            route_trust = process_route_trust(_resolve_route(session, revision, None, None).driver)
        allowed = tuple(
            target
            for target in targets
            if content_access_for_path(target.path, governance.content_access, route_trust=route_trust).allowed
        )
        policy = _effective_intelligence_policy(session, revision)
        return len(generate_candidates(allowed, rules=governance.rules, critical_rules=_critical_ai_rules(policy)))

    def _ai_plan_for(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        requested_effort: str | None,
        targets: Sequence[Any] | None = None,
    ) -> tuple[Any, Any]:
        """The persisted plan and AI stage plan; a manual request is an add-only pick.

        ``Run deeper`` picks the requested depth's AI selection. A request the
        plan cannot honor (AI turned off, limited by budget) is refused with 409.
        """

        from lemoncrow.pro.capabilities.review.capability_catalog import PRESET_SELECTIONS
        from lemoncrow.pro.capabilities.review.run_plan import ManualPick, ai_stage_plan

        def candidates() -> int:
            return _ai_candidate_count(session, revision, targets)

        if requested_effort is None:
            plan = run_plans.current(session, revision, ai_candidates=candidates)
            return plan, ai_stage_plan(plan)
        depth = cast(Any, requested_effort)
        picks = [ManualPick("ai.investigators", depth)] + [
            ManualPick(row.id, depth)
            for row in PRESET_SELECTIONS[depth]
            if row.id == "ai.hypotheses" and row.when != "manual" and not row.escalate
        ]
        plan = run_plans.amend(session, revision, picks, ai_candidates=candidates)
        stage = ai_stage_plan(plan)
        if not stage.investigators:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"AI review is not in the effective run plan for this Snapshot: {stage.reason}",
            )
        return plan, stage

    def _existing_ai_review_action(
        session: ReviewSession, revision: ReviewRevision, targets: Sequence[Any], effort: str
    ) -> dict[str, Any]:
        """Return an idempotent Auto response without replaying orchestration."""

        block = _ai_review_projection(session, revision, targets, historical=False)
        runs = _ai_review_runs(session, revision)
        annotations = _current_ai_annotations(session, revision)
        state = str(block["status"])
        action_state = (
            "cancelled"
            if state == "cancelled"
            else (
                "degraded"
                if state == "failed"
                else "skipped_unnecessary" if state == "skipped_unnecessary" else "completed"
            )
        )
        return {
            "revision_id": revision.id,
            "status": action_state,
            "effort": effort,
            "candidates": int(block["candidates"]),
            "hypotheses": sum(1 for row in runs if row.capability == "hypothesis_generation" and row.status == "ready"),
            "findings": {
                "confirmed": len(annotations),
                "not_confirmed": 0,
                "unknown": 0,
                "annotations_created": 0,
            },
            "annotation_ids": [annotation.id for annotation in annotations],
            "budget": dict(block["budget"]),
            "provider_calls": 0,
            "cache_reuse": sum(1 for row in runs if row.status == "ready"),
            "skipped_by_budget": int(block["skipped_by_budget"]),
            "provider_errors": [str(block["error"])] if block["error"] else [],
            "reviewer": {
                "driver": str(block.get("provider") or block.get("resolved_provider") or ""),
                "model": str(block.get("model") or block.get("resolved_model") or ""),
                "independent": False,
            },
            "provider_cost": None,
            "currency": None,
            "idempotent_reuse": True,
        }

    from lemoncrow.pro.capabilities.review.projection_progress import BRIEF_STAGES, ProjectionProgress

    _projection_progress = ProjectionProgress()

    def _projection(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        historical: bool,
        progress: Callable[[int, str], None] = lambda _completed, _detail: None,
    ) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.brief_summary import build_brief_summary
        from lemoncrow.pro.capabilities.review.review_plan import projection_payload
        from lemoncrow.pro.capabilities.review.review_roles import review_material_projection
        from lemoncrow.pro.capabilities.review.tour import build_change_tour

        progress(0, "Reading review plan")
        plan, ordered, view, states, plan_checks = _brief_plan(session, revision, historical=historical)
        progress(1, "Loading change summary")
        plan, synthesis = _with_synthesis(session, revision, plan, ordered, view, plan_checks)
        progress(2, "Comparing Snapshots")
        delta = _snapshot_delta(session, revision, ordered, plan_checks, states)
        progress(3, "Assembling Brief")
        payload = projection_payload(
            plan,
            ordered,
            review_id=session.id,
            revision_id=revision.id,
            snapshot_number=max(0, revision.revision_number - 1),
            historical=historical,
            decision_states=states,
            revision_numbers={item.id: item.revision_number for item in store.list_revisions(session.id)},
            synthesis=synthesis,
            delta=delta,
        )
        payload["summary"] = build_brief_summary(payload, plan_checks)
        progress(4, "Grouping review material")
        payload["material"] = review_material_projection(
            ordered,
            repo_root=session.repo_root,
            base_sha=revision.base_sha or session.review_base_sha,
        )
        progress(5, "Preparing guided review")
        payload["tour"] = _with_tour_synthesis(
            session,
            revision,
            build_change_tour(payload, ordered),
            ordered,
            historical=historical,
        )
        progress(6, "Loading AI review results")
        payload["ai_review"] = _ai_review_projection(session, revision, ordered, historical=historical)
        progress(7, "Loading review policy")
        payload["policy"] = _future_policy_projection(session, revision)
        ai_status = str(payload["ai_review"].get("status") or "")
        progress(8, "Prioritizing findings")
        payload["triage"] = _review_triage(session, revision, ordered, ai_status, plan_checks, historical=historical)
        progress(9, "Loading behavior evidence")
        behavior = _behavior_payload(session, revision, ordered, historical=historical)
        payload["behavior"] = behavior
        if behavior is not None and not historical:
            from lemoncrow.pro.capabilities.review.behavior import behavior_queue_actions, merge_queue_actions

            # Behavior targets join the one canonical review-level queue.
            payload["actions"] = merge_queue_actions(
                payload["actions"],
                behavior_queue_actions(_behavior_state(session, revision)[1], policy=_triage_policy(session, revision)),
            )
        return payload

    _vulnerability_scan_cache: dict[str, Any] = {}

    def _snapshot_severity_signals(
        session: ReviewSession,
        revision: ReviewRevision,
        evidence: Sequence[ReviewEvidence],
        targets: Sequence[Any] = (),
    ) -> tuple[tuple[Any, ...], frozenset[str]]:
        """``(behavior/vulnerability severity signals, producers that ran)`` on one exact Snapshot.

        A behavior producer ran when it recorded both sides here; the scan
        producer ran when an exact-Snapshot ``vulnerability_scan`` was readable.
        Advisories count only on dependencies this Snapshot changes (*targets*
        when given, else the Snapshot's dependency units).
        """

        from lemoncrow.pro.capabilities.review.review_triage import (
            VULNERABILITY_PRODUCER,
            behavior_signals,
            dependency_package,
            vulnerability_signals,
        )
        from lemoncrow.pro.capabilities.review.supply_chain import vulnerability_findings

        exact = [
            item
            for item in evidence
            if item.revision_id == revision.id and item.execution_intent != "explicit_historical"
        ]
        if not exact:
            return (), frozenset()
        collection = _behavior_collection(session, revision)
        sides: dict[str, set[str]] = {}
        for scope in collection.executed:
            sides.setdefault(str(scope.producer), set()).add(str(scope.side))
        ran = {producer for producer, seen in sides.items() if {"base", "head"} <= seen}
        signals: list[Any] = list(behavior_signals(collection.deltas, revision_id=revision.id))
        scans = [item for item in exact if item.kind == "vulnerability_scan"]
        if scans:
            changed: dict[str, list[str]] = {}
            if targets:
                for target in targets:
                    name = dependency_package(str(target.label)) if target.kind == "dependency" else ""
                    if name:
                        changed.setdefault(name, []).append(str(target.target_id))
            else:
                for unit in store.list_units(revision.id):
                    name = dependency_package(str(unit.symbol or "")) if unit.kind == "dependency" else ""
                    if name:
                        changed.setdefault(name, [])
            advisories: list[Any] = []
            for item in scans:
                key = f"{item.id}:{item.content_hash}"
                cached = _vulnerability_scan_cache.get(key)
                if cached is None:
                    payload = store.read_evidence_artifact(item)
                    cached = (False, ()) if payload is None else (True, vulnerability_findings(payload))
                    _bounded_cache_put(_vulnerability_scan_cache, key, cached)
                if cached[0]:
                    ran.add(VULNERABILITY_PRODUCER)
                    advisories.extend(cached[1])
            signals.extend(vulnerability_signals(advisories, revision_id=revision.id, changed_packages=changed))
        return tuple(signals), frozenset(ran)

    def _review_triage(
        session: ReviewSession,
        revision: ReviewRevision,
        targets: Sequence[Any],
        ai_status: str,
        checks: Sequence[Mapping[str, Any]],
        *,
        historical: bool,
    ) -> dict[str, Any]:
        """Review-level triage (``review_triage``); the current Snapshot's row is persisted for the Inbox."""

        from lemoncrow.pro.capabilities.review.behavior import registered_behavior_producers
        from lemoncrow.pro.capabilities.review.review_triage import (
            PRIORITY_OVERRIDE_KINDS,
            VULNERABILITY_PRODUCER,
            active_override,
            current_findings,
            derive_review_triage,
            finding_evidence_state,
            persist_review_triage,
            stale_severity_signals,
            with_override,
        )
        from lemoncrow.pro.capabilities.review.static_findings import static_severity_signals

        policy = _triage_policy(session, revision)
        findings, stale = current_findings(
            store,
            revision_id=revision.id,
            annotations=store.list_annotations(session.id),
            threshold=policy.finding_threshold,
        )
        evidence = store.list_evidence(session.id)
        revisions = store.list_revisions(session.id)
        observed, ran = _snapshot_severity_signals(session, revision, evidence, targets)
        # P11-FED: imported SARIF/check-run findings, with P9-PRIO severities.
        static = static_severity_signals(store, revision, evidence=evidence, revisions=revisions)
        with_evidence = {item.revision_id for item in evidence}
        earlier = sorted(
            (item for item in revisions if item.revision_number < revision.revision_number),
            key=lambda item: item.revision_number,
            reverse=True,
        )
        carried = stale_severity_signals(
            observed,
            ran,
            (
                _snapshot_severity_signals(session, item, evidence) if item.id in with_evidence else ((), ())
                for item in earlier
            ),
            producers=(*(str(item.producer_id) for item in registered_behavior_producers()), VULNERABILITY_PRODUCER),
        )
        evidence_state, evidence_detail = finding_evidence_state(
            ai_status, stale_findings=stale, stale_signals=len(carried) + sum(1 for item in static if item.stale)
        )
        reviewed = sum(target.state == "reviewed" for target in targets)
        touched = sum(target.state in {"reviewed", "needs_changes", "changed_since_review"} for target in targets)
        triage = derive_review_triage(
            review_id=session.id,
            revision_id=revision.id,
            policy=policy,
            changed_paths=tuple(dict.fromkeys([*_changed_paths(revision), *(t.path for t in targets if t.path)])),
            findings=findings,
            stale_findings=stale,
            signals=(*observed, *carried, *static),
            evidence_state=evidence_state,
            evidence_detail=evidence_detail,
            required_failures=sum(
                1
                for row in checks
                if row.get("required")
                and str(row.get("state") or "") == "current"
                and str(row.get("status") or "").upper() == "FAIL"
            ),
            dependency_changes=sum(target.kind == "dependency" for target in targets),
            review_state=(
                "unknown"
                if not targets
                else "reviewed" if reviewed == len(targets) else "in_progress" if touched else "not_reviewed"
            ),
        )
        if not historical:
            persist_review_triage(store, triage)
        events = store.list_activity_events(session.id, kinds=PRIORITY_OVERRIDE_KINDS, newest_first=True, limit=50)
        return with_override(triage, active_override(events, current_revision_id=revision.id)).to_wire()

    def _snapshot_delta(
        session: ReviewSession,
        revision: ReviewRevision,
        targets: Sequence[Any],
        checks: Sequence[Mapping[str, Any]],
        states: Sequence[Any],
    ) -> dict[str, Any] | None:
        """What changed since the previous Snapshot (spec §9 step 5), derived on read.

        ``changed`` is a unit whose content fingerprint moved; ``added`` and
        ``removed`` are target units new to, or gone from, this Snapshot;
        ``stale_evidence`` names checks recorded on the previous Snapshot that
        now read stale; ``reopened_decisions`` are decisions this Snapshot
        reopened. A reviewed, unchanged target is never named.
        """

        from lemoncrow.pro.capabilities.review.targets import derive_review_targets

        previous = next(
            (item for item in store.list_revisions(session.id) if item.revision_number == revision.revision_number - 1),
            None,
        )
        if previous is None:
            return None
        before_units = {unit.unit_key: unit.content_fingerprint for unit in store.list_units(previous.id)}
        now_units = {unit.unit_key: unit.content_fingerprint for unit in store.list_units(revision.id)}
        before_keys = {
            target.unit_key
            for target in derive_review_targets(store.list_units(previous.id), (), read_packet_json(store, previous))
        }
        added = [target.target_id for target in targets if target.unit_key not in before_keys]
        changed = [
            target.target_id
            for target in targets
            if target.unit_key in before_keys and before_units.get(target.unit_key) != now_units.get(target.unit_key)
        ]
        previous_number = max(0, previous.revision_number - 1)
        stale = sorted(
            {
                str(row.get("name") or row.get("source_ref") or "")
                for row in checks
                if str(row.get("state") or "") == "stale" and row.get("snapshot_number") == previous_number
            }
            - {""}
        )
        return {
            "since_snapshot": previous_number,
            "added": added,
            "changed": changed,
            "removed": len(before_keys - {target.unit_key for target in targets}),
            "stale_evidence": stale,
            "reopened_decisions": [
                state.key
                for state in states
                if state.reason_code in ("decision_scope_changed", "decision_implementation_changed")
            ],
        }

    def _with_synthesis(
        session: ReviewSession,
        revision: ReviewRevision,
        plan: Any,
        targets: Sequence[Any],
        view: Any,
        plan_checks: Sequence[Mapping[str, Any]],
    ) -> tuple[Any, dict[str, Any]]:
        """Overlay an exact cached Brief synthesis over policy-authorized targets only."""

        from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallBudget
        from lemoncrow.pro.capabilities.review.review_plan import apply_synthesis
        from lemoncrow.pro.capabilities.review.synthesis import (
            PROMPT_VERSION,
            SYNTHESIS_JSON_SCHEMA,
            SYNTHESIS_SCHEMA_VERSION,
            load_synthesis,
        )

        checks = _check_rows(session, revision, discover=False)
        initial_candidates = sum(1 for item in plan.items if item.kind in ("area", "other") and item.primary_target_ids)
        scope, context, _target_paths = _brief_intelligence_inputs(session, revision, view, targets, checks)
        if initial_candidates == 0:
            budget = ModelCallBudget.from_env(max_calls=1)
            return plan, {
                "status": "none",
                "mode": "deterministic_only",
                "model": "",
                "driver": "",
                "error": "",
                "candidates": 0,
                "rejected_count": 0,
                "unsynthesized": 0,
                "policy_digest": "",
                "cache_hit": False,
                "excluded_by_policy": [],
                "budget": {"calls_used": store.intelligence_calls(session.id), "max_calls": budget.max_review_calls},
            }
        spec = _intelligence_spec(
            session,
            revision,
            capability="brief_synthesis",
            scope=scope,
            context=context,
            output_schema=SYNTHESIS_JSON_SCHEMA,
            prompt_version=PROMPT_VERSION,
            schema_version=SYNTHESIS_SCHEMA_VERSION,
            target_paths={str(target.target_id): str(target.path) for target in targets},
        )
        allowed = frozenset(str(value) for value in spec.get("allowed_target_ids") or ())
        candidates = sum(
            1
            for item in plan.items
            if item.kind in ("area", "other") and item.primary_target_ids and set(item.primary_target_ids) <= allowed
        )
        block = {
            **_intelligence_status(session, spec, candidates=candidates),
            "rejected_count": 0,
            "unsynthesized": 0,
        }
        if candidates == 0:
            return plan, {**block, "mode": "deterministic_only"}
        row = spec.get("row")
        if row is None or str(row.status) != "ready":
            return plan, block
        try:
            payload = json.loads(row.result_json or "{}")
        except ValueError:
            payload = {}
        request = _synthesis_request(
            session,
            revision,
            plan,
            targets,
            checks,
            str(spec["context_digest"]),
            allowed_target_ids=tuple(allowed),
        )
        validated = load_synthesis(payload if isinstance(payload, Mapping) else {}, request)
        block["rejected_count"] = len(validated.rejected)
        block["unsynthesized"] = len(validated.unsynthesized_target_ids)
        return apply_synthesis(plan, validated, plan_checks), block

    def _intelligence_project_root(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        capability: str,
        driver: str,
        snapshot_identity: str,
    ) -> Path | None:
        if driver not in {"claude", "codex", "opencode"}:
            return None
        import hashlib

        from lemoncrow.pro.capabilities.review.data_governance import process_route_trust
        from lemoncrow.pro.capabilities.review.rules import content_access_for_path
        from lemoncrow.pro.capabilities.review.scratch_cache import touch_cache_entry
        from lemoncrow.pro.capabilities.review.snapshot import materialize_review_side

        governance = _review_governance(session, revision)
        route_trust = process_route_trust(driver)
        access_identity = hashlib.sha256(
            json.dumps(
                [row.to_wire() for row in governance.content_access],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        variant = hashlib.sha256(f"{snapshot_identity}:{route_trust}:{access_identity}".encode()).hexdigest()[:24]

        # Each access-policy variant gets its own immutable cache root. We never
        # broaden a previously filtered tree in place, and never need to delete
        # another session/capability's cache.
        cache = store.root / "cache" / "review-intelligence-snapshots" / session.id / revision.id / capability / variant
        snapshot_root = cache / "repo"
        marker = cache / "ready"
        revision_cache = cache.parent.parent
        touch_cache_entry(revision_cache)
        touch_cache_entry(revision_cache.parent)
        if not snapshot_root.is_dir() or not marker.is_file():
            materialize_review_side(
                resolved_source_store,
                resolved_source_repo,
                revision,
                side="new",
                target=snapshot_root,
                store=store,
            )
            for candidate in sorted(snapshot_root.rglob("*"), key=lambda path: len(path.parts), reverse=True):
                if not (candidate.is_file() or candidate.is_symlink()):
                    continue
                relative = candidate.relative_to(snapshot_root).as_posix()
                decision = content_access_for_path(
                    relative,
                    governance.content_access,
                    route_trust=route_trust,
                )
                if not decision.allowed:
                    candidate.unlink(missing_ok=True)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("filtered\n", encoding="utf-8")
            _prune_ask_snapshots(revision_cache)
        return snapshot_root

    def _synthesize_now(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.intelligence_runtime import execute_structured, run_intelligence
        from lemoncrow.pro.capabilities.review.synthesis import (
            PROMPT_VERSION,
            SYNTHESIS_JSON_SCHEMA,
            SYNTHESIS_SCHEMA_VERSION,
            synthesis_messages,
            validate_synthesis,
        )

        plan, ordered, view, _states, plan_checks = _brief_plan(session, revision, historical=False)
        checks = _check_rows(session, revision, discover=False)
        initial_candidates = sum(1 for item in plan.items if item.kind in ("area", "other") and item.primary_target_ids)
        scope, context, _target_paths = _brief_intelligence_inputs(session, revision, view, ordered, checks)
        if initial_candidates == 0:
            return {"synthesis": _with_synthesis(session, revision, plan, ordered, view, plan_checks)[1]}
        spec = _intelligence_spec(
            session,
            revision,
            capability="brief_synthesis",
            scope=scope,
            context=context,
            output_schema=SYNTHESIS_JSON_SCHEMA,
            prompt_version=PROMPT_VERSION,
            schema_version=SYNTHESIS_SCHEMA_VERSION,
            target_paths={str(target.target_id): str(target.path) for target in ordered},
        )
        allowed = frozenset(str(value) for value in spec.get("allowed_target_ids") or ())
        candidates = sum(
            1
            for item in plan.items
            if item.kind in ("area", "other") and item.primary_target_ids and set(item.primary_target_ids) <= allowed
        )
        request = spec.get("request")
        if request is None:
            return {"synthesis": _intelligence_status(session, spec, candidates=candidates)}
        synthesis_request = _synthesis_request(
            session,
            revision,
            plan,
            ordered,
            checks,
            str(spec["context_digest"]),
            allowed_target_ids=tuple(allowed),
        )
        if not synthesis_request.items:
            return {
                "synthesis": {
                    **_intelligence_status(session, spec, candidates=candidates),
                    "mode": "deterministic_only",
                }
            }
        messages = synthesis_messages(synthesis_request)

        def execute() -> Any:
            project_root = _intelligence_project_root(
                session,
                revision,
                capability="brief_synthesis",
                driver=str(spec["driver"]),
                snapshot_identity=str(spec["snapshot_identity"]),
            )
            return execute_structured(
                system=str(messages[0]["content"]),
                prompt=str(messages[1]["content"]),
                output_schema=SYNTHESIS_JSON_SCHEMA,
                driver=str(spec["driver"]),
                model=str(spec["model"]) or None,
                project_root=project_root,
                origin="machine",
                timeout_s=spec["budget"].timeout_s,
            )

        outcome = run_intelligence(store=store, request=request, executor=execute)
        if outcome.status == "ready":
            validate_synthesis(outcome.result, synthesis_request)
        return {"synthesis": _outcome_status(session, spec, outcome, candidates=candidates)}

    def _submit_durable_ai_review(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        requested_effort: str,
        trigger: str,
        action_id: str = "",
        fresh: bool = False,
    ) -> Mapping[str, Any] | None:
        if ai_review_submitter is None:
            return None
        plan, stage = _ai_plan_for(
            session,
            revision,
            # A fresh re-run executes the Snapshot's current effective plan again.
            requested_effort=None if trigger == "auto" or fresh else requested_effort,
        )
        if not stage.runs_ai:
            return {
                "status": "not_planned",
                "revision_id": revision.id,
                "detail": stage.reason,
                "run_plan": {"digest": plan.digest, "ai": stage.to_wire()},
            }
        policy = _effective_intelligence_policy(session, revision)
        governance = _review_governance(session, revision)
        queued = ai_review_submitter(
            session,
            revision,
            stage.effort,
            trigger,
            policy,
            governance,
            action_id,
            run_plan=plan.to_wire(),
            # Only a fresh re-run passes the flag, so existing submitters keep their signature.
            **({"fresh": True} if fresh else {}),
        )
        if trigger == "auto" or queued is None or fresh:
            return queued
        # P11-ESC item 11: a request below a floor is raised, and the response says why.
        return {**queued, "depth_request": run_plans.explain_request(session, revision, plan, requested_effort)}

    def _schedule_auto_ai_review(session: ReviewSession, revision: ReviewRevision) -> Mapping[str, Any]:
        if ai_review_submitter is None:
            return {"status": "unavailable", "detail": "durable AI Review scheduler is not configured"}
        _plan, ordered, _view, _states, _checks = _brief_plan(session, revision, historical=False)
        block = _ai_review_projection(session, revision, ordered, historical=False)
        policy_error = run_plans.peek(session, revision).policy_error
        if policy_error:
            # An invalid trusted-base config schedules nothing; the capture stands.
            return {
                "status": "not_planned",
                "revision_id": revision.id,
                "detail": f"Review policy invalid: {policy_error}",
            }
        if not block["auto_eligible"]:
            return {"status": str(block["status"]), "revision_id": revision.id}
        queued = _submit_durable_ai_review(
            session,
            revision,
            requested_effort="auto",
            trigger="auto",
        )
        return {"status": "unavailable"} if queued is None else dict(queued)

    def _worker_ai_publication_context(review_id: str, revision_id: str) -> Mapping[str, Any]:
        session = store.get_session(review_id)
        revision = store.get_revision(revision_id)
        if session is None or revision is None or revision.review_id != review_id:
            raise ValueError("worker AI result no longer has an exact Review Snapshot")
        latest = store.latest_revision(review_id)
        historical = latest is None or latest.id != revision.id
        policy = _effective_intelligence_policy(session, revision)
        governance = _review_governance(session, revision)
        _plan, ordered, _view, _states, _checks = _brief_plan(session, revision, historical=historical)
        return {
            "store": store,
            "session": session,
            "revision": revision,
            "latest_revision_id": "" if latest is None else latest.id,
            "policy": policy,
            "governance": governance,
            "targets": tuple(ordered),
        }

    def _worker_ai_publish(
        review_id: str,
        revision_id: str,
        publication_key: str,
        raw_findings: Sequence[Mapping[str, Any]],
        driver: str,
        model: str,
        cross_repo_context: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[str, ...]:
        from lemoncrow.pro.capabilities.review.ai_review import publish_machine_findings

        context = _worker_ai_publication_context(review_id, revision_id)
        revision = cast("ReviewRevision", context["revision"])
        governance = context["governance"]
        return publish_machine_findings(
            store,
            revision,
            cast(Sequence[Any], context["targets"]),
            raw_findings,
            publication_key=publication_key,
            driver=driver,
            model=model,
            rules=tuple(getattr(governance, "rules", ())),
            cross_repo_context=cross_repo_context,
        )

    def _worker_runtime_context(
        review_id: str,
        revision_id: str,
        *,
        provider: str,
        surface_id: str,
        side: str,
    ) -> Mapping[str, Any]:
        session = store.get_session(review_id)
        revision = store.get_revision(revision_id)
        if session is None or revision is None or revision.review_id != review_id:
            raise ValueError("worker runtime result no longer has an exact Review Snapshot")
        surface, scenario, validation_request, execution = _runtime_validation_selection(
            session,
            revision,
            provider=provider,
            surface_id=surface_id,
            side=side,
        )
        from lemoncrow.pro.capabilities.review.planned_execution import persisted_plan, surface_entry_id

        latest = store.latest_revision(review_id)
        trusted_context, execution_context, execution_runners = execution
        return {
            "store": store,
            "session": session,
            "revision": revision,
            "latest_revision_id": "" if latest is None else latest.id,
            "surface": surface,
            "scenario": scenario,
            "validation_request": validation_request,
            "trusted_context": trusted_context,
            "execution_context": execution_context,
            "execution_runners": execution_runners,
            # The worker plane admits the job against the persisted effective
            # run plan at claim and again at execution.
            "run_plan": persisted_plan(store, revision.id),
            "run_plan_entry": surface_entry_id(surface),
        }

    def _worker_runtime_publish(
        review_id: str,
        revision_id: str,
        *,
        provider: str,
        surface_id: str,
        side: str,
        observation: Mapping[str, Any],
        execution_intent: str,
        executor_class: str = "",
    ) -> Mapping[str, Any]:
        """Publish a worker observation; ``executor_class`` comes from the job's durable locus."""

        from lemoncrow.pro.capabilities.review.runtime_validation import (
            normalize_surface_run,
            persist_validation_record,
        )

        context = _worker_runtime_context(
            review_id,
            revision_id,
            provider=provider,
            surface_id=surface_id,
            side=side,
        )
        revision = cast("ReviewRevision", context["revision"])
        validation_request = context["validation_request"]
        scenario = context["scenario"]
        surface = context["surface"]
        record = normalize_surface_run(validation_request, scenario, observation)
        evidence = persist_validation_record(
            store,
            revision,
            validation_request,
            scenario,
            record,
            path=surface.affected_paths[0] if surface.affected_paths else "",
            source="external",
            expected_current_revision_id=revision.id if execution_intent == "current" else "",
            execution_intent=execution_intent,
            executor_class=executor_class,
        )
        return {
            "evidence_id": evidence.id,
            "executor_class": evidence.executor_class,
            "verification_status": evidence.verification_status,
            "record": record,
        }

    # ``review_auto_scheduler`` (AI review plus planned pre-merge checks) is
    # registered with the pre-merge routes below.
    app.state.review_landing_revalidator = _revalidate_landing_by_id
    app.state.review_policy_generation = _policy_generation_for
    app.state.review_trusted_base_moved = _trusted_base_moved_for
    app.state.review_worker_ai_publication_context = _worker_ai_publication_context
    app.state.review_worker_ai_publisher = _worker_ai_publish
    app.state.review_worker_runtime_context = _worker_runtime_context
    app.state.review_worker_runtime_publisher = _worker_runtime_publish

    @router.post("/reviews/{review_id}/ai-review")
    async def _post_ai_review(review_id: str, request: Request) -> dict[str, Any]:
        """Run locally or enqueue durable AI review for the exact Snapshot."""

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.ai_review import ReviewEffort, run_independent_review
        from lemoncrow.pro.capabilities.review.independence import configured_reviewer_routes
        from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallBudget

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="reviewing this Snapshot with AI")
        requested_effort = str(body.get("effort") or "auto").strip().lower()
        if requested_effort not in {"auto", "quick", "thorough", "deep"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="effort must be auto, quick, thorough, or deep",
            )
        trigger = str(body.get("trigger") or "manual").strip().lower()
        if trigger not in {"auto", "manual"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="trigger must be auto or manual",
            )
        action_id = str(body.get("action_id") or "").strip()
        if len(action_id) > 128:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="action_id is too long")

        if ai_review_submitter is not None:
            # Hosted Auto originates only from Snapshot/policy events. Reader/API
            # lifecycle is observational and cannot create automatic billable work.
            if trigger == "auto":
                existing = ai_review_job_lookup(session, revision) if ai_review_job_lookup is not None else None
                return {
                    "ai_review": (
                        dict(existing)
                        if existing is not None
                        else {"status": "none", "revision_id": revision.id, "auto_event_driven": True}
                    )
                }
            queued = _submit_durable_ai_review(
                session,
                revision,
                requested_effort=requested_effort,
                trigger="manual",
                action_id=action_id,
            )
            return {
                "ai_review": ({"status": "unavailable", "revision_id": revision.id} if queued is None else dict(queued))
            }

        policy = _effective_intelligence_policy(session, revision)
        governance = _review_governance(session, revision)

        def _local_background_budget() -> Any:
            from lemoncrow.pro.capabilities.review.background_budget_store import LocalBackgroundBudgetStore
            from lemoncrow.pro.capabilities.review.fix_agent import settings_root

            return background_budget if background_budget is not None else LocalBackgroundBudgetStore(settings_root())

        def _auto_superseded() -> bool:
            if trigger != "auto":
                return False
            current = store.latest_revision(session.id)
            return current is None or current.id != revision.id

        async def execute() -> dict[str, Any]:
            _plan, ordered, _view, _states, _checks = _brief_plan(session, revision, historical=False)
            if trigger == "auto":
                block = _ai_review_projection(session, revision, ordered, historical=False)
                if not block["auto_eligible"]:
                    return {"ai_review": _existing_ai_review_action(session, revision, ordered, str(block["effort"]))}
            # The persisted effective run plan is the authority for what runs.
            run_plan, stage = _ai_plan_for(
                session,
                revision,
                requested_effort=None if trigger == "auto" else requested_effort,
                targets=ordered,
            )

            def review(budget: ModelCallBudget | None) -> Any:
                return run_independent_review(
                    store=store,
                    revision=revision,
                    targets=ordered,
                    repo_root=repo_root,
                    effort=cast(ReviewEffort, stage.effort),
                    rules=governance.rules,
                    critical_rules=_critical_ai_rules(policy),
                    content_access=governance.content_access,
                    policy_context=policy,
                    budget=budget,
                    cancellation=_auto_superseded if trigger == "auto" else None,
                    stage_plan=stage,
                    reviewer_candidates=configured_reviewer_routes()[0],
                    project_knowledge_loader=_ai_review_knowledge_loader(session, revision, governance.content_access),
                )

            base_budget = ModelCallBudget.from_env(max_calls=1)
            ceiling = base_budget.max_review_calls
            if stage.max_review_calls is not None:
                ceiling = min(ceiling, stage.max_review_calls)
            if trigger != "auto" or ceiling <= 0:
                # Explicit work, or a Review that may make no model call: nothing to meter.
                result = await run_in_threadpool(review, None)
            else:
                # BI2: automatic AI Review is background work. It runs only under BI0's
                # background switch and run_on, and reserves/settles in the BI1 ledger.
                from lemoncrow.pro.capabilities.review.background_local import (
                    local_review_auto_decision,
                    run_local_review_auto,
                )
                from lemoncrow.pro.capabilities.review.background_scheduler import (
                    BackgroundOutcome,
                    reported_cost_micros,
                )
                from lemoncrow.pro.capabilities.review.fix_agent import settings_root
                from lemoncrow.pro.capabilities.review.intelligence_policy import process_intelligence_policy

                budget_store = _local_background_budget()
                bi0 = _route_policy(session, revision) or process_intelligence_policy(settings_root())
                decision = local_review_auto_decision(policy=bi0, budget=budget_store, source_digest=revision.id)
                if not decision.scheduled:
                    return {
                        "ai_review": {"status": "not_scheduled", "reason": decision.reason, "revision_id": revision.id}
                    }

                def metered(granted: int) -> BackgroundOutcome:
                    ran = review(replace(base_budget, max_review_calls=granted))
                    return BackgroundOutcome(
                        ran.provider_calls, reported_cost_micros(ran.provider_cost, ran.currency), ran
                    )

                admitted = await run_in_threadpool(
                    run_local_review_auto,
                    budget=budget_store,
                    revision_id=revision.id,
                    max_calls=ceiling,
                    current=lambda: not _auto_superseded(),
                    execute=metered,
                )
                if admitted.status == "superseded":
                    return {"ai_review": {"status": "cancelled", "reason": admitted.reason, "revision_id": revision.id}}
                if admitted.status != "completed" or admitted.result is None:
                    return {
                        "ai_review": {
                            "status": "paused" if admitted.paused else "not_scheduled",
                            "reason": admitted.reason,
                            "revision_id": revision.id,
                        }
                    }
                result = admitted.result
            wire = {**result.to_wire(), "revision_id": revision.id, "run_plan_digest": run_plan.digest}
            if trigger != "auto":
                # P11-ESC item 11: a request below a floor is raised, and the response says why.
                wire["depth_request"] = run_plans.explain_request(session, revision, run_plan, requested_effort)
            return {"ai_review": wire}

        if trigger != "auto":
            return await execute()
        lock = ai_review_auto_locks.setdefault(revision.id, asyncio.Lock())
        async with lock:
            return await execute()

    @router.post("/reviews/{review_id}/ai-review/cancel")
    async def _post_ai_review_cancel(review_id: str, request: Request) -> dict[str, Any]:
        """Human cancellation of one current-Snapshot durable AI job, never a model run."""
        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="cancelling AI review")
        job_id = body.get("job_id")
        if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="job_id is required")
        if ai_review_job_cancel is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="durable AI cancellation is unavailable")
        from starlette.concurrency import run_in_threadpool

        return {"ai_review": dict(await run_in_threadpool(ai_review_job_cancel, session, revision, job_id))}

    @router.post("/reviews/{review_id}/ai-review/fresh")
    async def _post_ai_review_fresh(review_id: str, request: Request) -> dict[str, Any]:
        """Reviewer/admin fresh re-run of AI review on the unchanged current Snapshot (P11 item 15b).

        Executes the Snapshot's effective plan again instead of reusing cached
        results, under the normal Review budget. ``action_id`` is required: a
        replay of the same action reuses what it produced. Ordinary requests
        and reloads keep reusing cache. Hosted RBAC maps this route to
        REVIEW_JUDGE (reviewer, developer, admin; never a service principal).
        """

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.ai_review import ReviewEffort, run_independent_review
        from lemoncrow.pro.capabilities.review.fresh_rerun import valid_action_id
        from lemoncrow.pro.capabilities.review.independence import configured_reviewer_routes

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="re-running AI review fresh on this Snapshot")
        action_id = valid_action_id(body.get("action_id"))
        if not action_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="action_id is required: an opaque token of 1-128 characters from [A-Za-z0-9._:-]",
            )
        if ai_review_submitter is not None:
            queued = _submit_durable_ai_review(
                session, revision, requested_effort="auto", trigger="manual", action_id=action_id, fresh=True
            )
            return {
                "ai_review": {"status": "unavailable", "revision_id": revision.id} if queued is None else dict(queued),
                "fresh_rerun": {"action_id": action_id},
            }
        _plan, ordered, _view, _states, _checks = _brief_plan(session, revision, historical=False)
        run_plan, stage = _ai_plan_for(session, revision, requested_effort=None, targets=ordered)
        if not stage.runs_ai:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"AI review is not in the effective run plan for this Snapshot: {stage.reason}",
            )
        policy = _effective_intelligence_policy(session, revision)
        governance = _review_governance(session, revision)
        result = await run_in_threadpool(
            run_independent_review,
            store=store,
            revision=revision,
            targets=ordered,
            repo_root=repo_root,
            effort=cast(ReviewEffort, stage.effort),
            rules=governance.rules,
            critical_rules=_critical_ai_rules(policy),
            content_access=governance.content_access,
            policy_context=policy,
            stage_plan=stage,
            reviewer_candidates=configured_reviewer_routes()[0],
            fresh_action_id=action_id,
            project_knowledge_loader=_ai_review_knowledge_loader(session, revision, governance.content_access),
        )
        return {
            "ai_review": {**result.to_wire(), "revision_id": revision.id, "run_plan_digest": run_plan.digest},
            "fresh_rerun": {"action_id": action_id},
        }

    @router.post("/reviews/{review_id}/rule-suggestions/{suggestion_id}/dismiss")
    async def _dismiss_rule_suggestion(review_id: str, suggestion_id: str, request: Request) -> dict[str, Any]:
        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="dismissing this Review rule suggestion")
        policy_projection = _future_policy_projection(session, revision)
        rows = policy_projection.get("suggestions", ())
        matching = next(
            (row for row in rows if isinstance(row, Mapping) and str(row.get("id") or "") == suggestion_id),
            None,
        )
        if matching is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such Review rule suggestion")
        acting = _as_acting_reviewer(session)
        store.dismiss_rule_suggestion(
            suggestion_id,
            repository_scope=expected_session_repo,
            actor_id=acting.reviewer_id,
        )
        return {"policy": _future_policy_projection(session, revision)}

    @router.post("/reviews/{review_id}/rule-suggestions/{suggestion_id}/adopt")
    async def _adopt_rule_suggestion(review_id: str, suggestion_id: str, request: Request) -> dict[str, Any]:
        """Explicit adoption: the patch a human lands on the trusted base. Writes nothing, changes no policy."""

        from lemoncrow.pro.capabilities.review.rule_suggestions import (
            RuleAdoptionError,
            propose_rule_adoption,
            repeated_human_feedback_suggestions,
        )

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="proposing this Review rule")
        found = next(
            (
                row
                for row in repeated_human_feedback_suggestions(
                    store.list_all_annotations(),
                    repository_scope=expected_session_repo,
                    dismissed_ids=store.dismissed_rule_suggestion_ids(repository_scope=expected_session_repo),
                )
                if row.id == suggestion_id
            ),
            None,
        )
        if found is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such Review rule suggestion")
        try:
            proposal = propose_rule_adoption(found, _snapshot_review_config(revision))
        except RuleAdoptionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return {"proposal": proposal.to_wire()}

    @router.post("/reviews/{review_id}/synthesis")
    async def _post_synthesis(review_id: str, request: Request) -> dict[str, Any]:
        """Explicit, exact-Snapshot Brief synthesis through the shared runtime."""

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.run_plan import ManualPick

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="explaining this Snapshot")
        # An explicit Brief synthesis is a per-Review pick of ai.brief_synthesis.
        plan = await run_in_threadpool(
            lambda: run_plans.amend(
                session,
                revision,
                [ManualPick("ai.brief_synthesis")],
                ai_candidates=lambda: _ai_candidate_count(session, revision),
            )
        )
        entry = plan.entry("ai.brief_synthesis")
        if entry is None or entry.status != "run":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Brief synthesis is not in the effective run plan for this Snapshot: "
                    f"{entry.reason if entry is not None else 'not selected'}"
                ),
            )
        result = await run_in_threadpool(_synthesize_now, session, revision)
        return {**result, "run_plan_digest": plan.digest}

    def _monthly_review_volume(session: ReviewSession, revision: ReviewRevision) -> int | None:
        """Recent Review volume for settings cost estimates (30 days)."""

        from lemoncrow.pro.capabilities.review.run_plan_service import created_since

        if run_plan_usage is not None:
            observed = run_plan_usage(session, revision) or {}
            value = observed.get("monthly_reviews")
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        since = time.time() - 30 * 86400
        # lc-debt: local volume counts at most the 500 most recent Reviews; a
        # larger store under-estimates. Upgrade: a store-side dated count.
        return sum(1 for item in store.list_sessions(status="", limit=500) if created_since(item, since))

    def _ai_availability() -> dict[str, Any]:
        """Whether AI entries can execute here; deterministic entries never depend on it."""

        if ai_review_submitter is not None:
            return {"available": True, "reason": "", "executor": "durable"}
        from lemoncrow.pro.capabilities.review.intelligence_runtime import resolve_driver_model

        try:
            resolved = resolve_driver_model(None, None)
        except Exception as exc:
            return {"available": False, "reason": f"no AI provider is configured: {exc}", "executor": "local"}
        return {"available": True, "reason": "", "executor": "local", "provider": resolved.driver}

    @router.get("/reviews/{review_id}/run-plan")
    async def _get_run_plan(review_id: str, revision_id: str = "") -> dict[str, Any]:
        """The Snapshot's effective run plan with setting origins and detected capabilities."""

        from starlette.concurrency import run_in_threadpool

        session = _session(review_id)
        latest = _latest(session)
        revision = _requested_revision(session, revision_id) if revision_id else latest

        def build() -> dict[str, Any]:
            from lemoncrow.pro.capabilities.review.trust_telemetry import RUN_PLAN, run_plan_observation

            plan = run_plans.current(
                session,
                revision,
                ai_candidates=lambda: _ai_candidate_count(session, revision),
                persist=revision.id == latest.id,
            )
            projection = run_plans.projection(
                session, revision, plan, monthly_reviews=_monthly_review_volume(session, revision)
            )
            _observe_trust(RUN_PLAN, lambda: run_plan_observation(projection["reverification"]))
            return {
                **projection,
                "revision_id": revision.id,
                "historical": revision.id != latest.id,
                "ai_availability": _ai_availability(),
            }

        return await run_in_threadpool(build)

    @router.post("/reviews/{review_id}/run-plan/run-specific")
    async def _post_run_specific(review_id: str, request: Request) -> dict[str, Any]:
        """``Run specific…``: add catalog entries for this Review, preview first, never remove."""

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.artifact_analyses import is_analysis_entry
        from lemoncrow.pro.capabilities.review.capability_catalog import PRESETS, valid_entry_id
        from lemoncrow.pro.capabilities.review.check_runs import is_check_entry
        from lemoncrow.pro.capabilities.review.run_plan import ManualPick, RunPlan

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="adding work to this Review")
        raw_entries = body.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries or len(raw_entries) > 64:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="entries must be a non-empty list of at most 64 catalog IDs",
            )
        picks: list[ManualPick] = []
        unknown: list[str] = []
        for item in raw_entries:
            entry_id = item if isinstance(item, str) else item.get("id") if isinstance(item, Mapping) else None
            depth = item.get("depth") if isinstance(item, Mapping) else None
            if not isinstance(entry_id, str) or not valid_entry_id(entry_id) or entry_id.endswith(":*"):
                unknown.append(str(entry_id))
                continue
            if depth is not None and depth not in PRESETS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="depth must be quick, auto, thorough or deep",
                )
            picks.append(ManualPick(entry_id, cast(Any, depth)))
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown capability id(s): {', '.join(unknown)}",
            )
        confirm = body.get("confirm") is True
        actions = {
            "ai.investigators": "ai_review",
            "ai.hypotheses": "ai_review",
            "ai.brief_synthesis": "synthesis",
            "ai.premerge_checks": "premerge_checks",
            "ai.rule_checks": "ai_review",
        }

        def depth_requests(plan: RunPlan) -> list[dict[str, Any]]:
            # P11-ESC item 11: a depth below a floor is raised, with the floor and its origin.
            return [
                run_plans.explain_request(session, revision, plan, pick.depth, entry_id=pick.id)
                for pick in picks
                if pick.depth is not None
            ]

        def run() -> dict[str, Any]:
            def candidates() -> int:
                return _ai_candidate_count(session, revision)

            picks[:] = _with_discovered_targets(session, revision, picks)
            preview = run_plans.preview(session, revision, picks, ai_candidates=candidates)
            if preview["rejected"]:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="; ".join(str(error) for error in preview["rejected"]),
                )
            if not confirm:
                after = RunPlan.from_wire(preview["plan"], verify=False)
                return {"confirmed": False, "preview": preview, "depth_requests": depth_requests(after)}
            plan = run_plans.amend(session, revision, picks, ai_candidates=candidates)
            follow_up = []
            for pick in picks:
                entry = plan.entry(pick.id)
                if entry is None or entry.status != "run":
                    continue
                action = actions.get(
                    pick.id,
                    (
                        # A discovered surface is not trusted-base configured: it
                        # runs as a reviewer's surface run, not a validation.
                        "surface_run"
                        if pick.target is not None
                        else (
                            "runtime_validation"
                            if pick.id.startswith("surface:")
                            else (
                                "check_recipes"
                                if is_check_entry(pick.id)
                                else (
                                    "artifact_analysis"
                                    if is_analysis_entry(pick.id)
                                    else "external_evidence" if entry.external else "executor"
                                )
                            )
                        )
                    ),
                )
                follow_up.append({"entry": pick.id, "action": action, "depth": entry.depth})
            return {
                "confirmed": True,
                "preview": preview,
                "plan": plan.to_wire(),
                "next": follow_up,
                "depth_requests": depth_requests(plan),
            }

        return await run_in_threadpool(run)

    # Trusted-base check recipes: planned sandboxed execution, differential and proof requests.
    from lemoncrow.pro.capabilities.review.behavior import register_behavior_producer
    from lemoncrow.pro.capabilities.review.check_api import CheckRecipeDeps, register_check_recipe_routes
    from lemoncrow.pro.capabilities.review.differential import CheckDifferentialProducer
    from lemoncrow.pro.capabilities.review.render_diff import RenderDiffProducer
    from lemoncrow.pro.capabilities.review.runtime_observation import RuntimeObservationProducer
    from lemoncrow.pro.capabilities.review.runtime_observation_api import register_runtime_observation_routes

    register_behavior_producer(CheckDifferentialProducer(), replace_existing=True)
    register_behavior_producer(RuntimeObservationProducer(), replace_existing=True)
    register_behavior_producer(RenderDiffProducer(), replace_existing=True)
    from lemoncrow.pro.capabilities.review.artifact_analyses import ArtifactAnalysisProducer
    from lemoncrow.pro.capabilities.review.artifact_analysis_api import register_artifact_analysis_routes

    register_behavior_producer(ArtifactAnalysisProducer(), replace_existing=True)

    def _check_runners() -> Any:
        from lemoncrow.pro.capabilities.review.runtimes import built_in_runner_registry

        return runner_registry if runner_registry is not None else built_in_runner_registry()

    def _annotate_check_feedback(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        path: str,
        body: str,
        title: str,
        evidence: tuple[str, ...],
    ) -> Annotation:
        """A reviewer's file-level comment: it rides the existing feedback handoff."""

        with store.atomic_review_transition():
            store.require_current_revision(session.id, revision.id)
            return annotate(
                store,
                session,
                revision,
                path=path,
                start_line=0,
                end_line=0,
                body=body,
                kind="comment",
                side="new",
                created_by=session.reviewer_id or "local",
                created_by_actor="human",
                source="human",
                source_id=session.reviewer_id or "local",
                title=title,
                evidence=evidence,
                new_text=revision_new_side_text(store, resolved_repo, session, revision, path),
                expected_current_revision_id=revision.id,
            )

    _check_deps = CheckRecipeDeps(
        store=store,
        run_plans=run_plans,
        development_store=development_store,
        session=_session,
        mutable_session=_mutable_session,
        latest=_latest,
        requested_revision=_requested_revision,
        runners=_check_runners,
        surface_runtime=lambda *args, **kwargs: _surface_runtime(*args, **kwargs),
        read_side=lambda revision, path, side: _snapshot_document_text(revision, path, side=cast(Any, side)),
        review_config=_snapshot_review_config,
        ai_candidates=lambda session, revision: _ai_candidate_count(session, revision),
        enforce_armed_landing=lambda session: _enforce_armed_landing(session),
        annotate_feedback=_annotate_check_feedback,
        annotation_payload=_annotation_payload,
        changed_paths=_changed_paths,
        json_object=_json_object,
        change_set_heads=_change_set_heads,
        qualification=lambda session, revision: _check_qualification(session, revision),
    )
    register_check_recipe_routes(router, _check_deps)
    # Runtime side effects and deep rendering diffs: planned, metered, sandboxed (R7 items 10, 13).
    register_runtime_observation_routes(router, _check_deps)
    # Artifact/infrastructure analyses share the check routes' seams (R9 items 15, 31, 32, 33).
    register_artifact_analysis_routes(router, _check_deps)

    def _with_tour_synthesis(
        session: ReviewSession,
        revision: ReviewRevision,
        tour: Mapping[str, Any],
        targets: Sequence[Any],
        *,
        historical: bool,
    ) -> dict[str, Any]:
        del historical
        from lemoncrow.pro.capabilities.review.data_governance import process_route_trust
        from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallBudget
        from lemoncrow.pro.capabilities.review.rules import content_access_for_path
        from lemoncrow.pro.capabilities.review.tour import (
            TOUR_JSON_SCHEMA,
            TOUR_PROMPT_VERSION,
            TOUR_SCHEMA_VERSION,
            apply_tour_synthesis,
            tour_synthesis_context,
        )

        raw_context = tour_synthesis_context(tour)
        target_paths = {str(target.target_id): str(target.path) for target in targets}
        governance = _review_governance(session, revision)
        route_trust: ModelRouteTrust = "unknown"
        with contextlib.suppress(Exception):
            route_trust = process_route_trust(_resolve_route(session, revision, None, None).driver)
        decisions = {
            target_id: content_access_for_path(path, governance.content_access, route_trust=route_trust)
            for target_id, path in target_paths.items()
        }
        allowed = frozenset(target_id for target_id, decision in decisions.items() if decision.allowed)
        context = {
            **raw_context,
            "steps": [
                row for row in raw_context["steps"] if set(str(value) for value in row.get("target_ids", ())) <= allowed
            ],
        }
        candidates = len(context["steps"])
        if not raw_context["steps"]:
            budget = ModelCallBudget.from_env(max_calls=1)
            return {
                **dict(tour),
                "synthesis": {
                    "status": "none",
                    "mode": "deterministic_only",
                    "model": "",
                    "driver": "",
                    "error": "",
                    "candidates": 0,
                    "policy_digest": "",
                    "cache_hit": False,
                    "excluded_by_policy": [],
                    "budget": {
                        "calls_used": store.intelligence_calls(session.id),
                        "max_calls": budget.max_review_calls,
                    },
                },
            }
        scope = {
            "step_ids": [str(row["step_id"]) for row in context["steps"]],
            "target_ids": sorted({str(value) for row in context["steps"] for value in row.get("target_ids", ())}),
        }
        spec = _intelligence_spec(
            session,
            revision,
            capability="tour_synthesis",
            scope=scope,
            context=context,
            output_schema=TOUR_JSON_SCHEMA,
            prompt_version=TOUR_PROMPT_VERSION,
            schema_version=TOUR_SCHEMA_VERSION,
            target_paths=target_paths,
        )
        block = _intelligence_status(session, spec, candidates=candidates)
        if candidates == 0:
            return {**dict(tour), "synthesis": {**block, "mode": "deterministic_only"}}
        row = spec.get("row")
        if row is None or str(row.status) != "ready":
            return {**dict(tour), "synthesis": block}
        try:
            payload = json.loads(row.result_json or "{}")
        except ValueError:
            payload = {}
        refined = apply_tour_synthesis(tour, payload if isinstance(payload, Mapping) else {})
        refined["synthesis"] = block
        return refined

    def _synthesize_tour_now(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.data_governance import process_route_trust
        from lemoncrow.pro.capabilities.review.intelligence_runtime import (
            execute_structured,
            run_intelligence,
        )
        from lemoncrow.pro.capabilities.review.review_plan import projection_payload
        from lemoncrow.pro.capabilities.review.rules import content_access_for_path
        from lemoncrow.pro.capabilities.review.tour import (
            TOUR_JSON_SCHEMA,
            TOUR_PROMPT_VERSION,
            TOUR_SCHEMA_VERSION,
            build_change_tour,
            tour_messages,
            tour_synthesis_context,
            validate_tour_synthesis,
        )

        plan, ordered, _view, states, plan_checks = _brief_plan(session, revision, historical=False)
        delta = _snapshot_delta(session, revision, ordered, plan_checks, states)
        projected = projection_payload(
            plan,
            ordered,
            review_id=session.id,
            revision_id=revision.id,
            snapshot_number=max(0, revision.revision_number - 1),
            historical=False,
            decision_states=states,
            revision_numbers={item.id: item.revision_number for item in store.list_revisions(session.id)},
            synthesis=None,
            delta=delta,
        )
        tour = build_change_tour(projected, ordered)
        raw_context = tour_synthesis_context(tour)
        if not raw_context["steps"]:
            return {"synthesis": {"status": "none", "mode": "deterministic_only", "candidates": 0}}

        target_paths = {str(target.target_id): str(target.path) for target in ordered}
        governance = _review_governance(session, revision)
        route_trust: ModelRouteTrust = "unknown"
        with contextlib.suppress(Exception):
            route_trust = process_route_trust(_resolve_route(session, revision, None, None).driver)
        decisions = {
            target_id: content_access_for_path(path, governance.content_access, route_trust=route_trust)
            for target_id, path in target_paths.items()
        }
        allowed = frozenset(target_id for target_id, decision in decisions.items() if decision.allowed)
        context = {
            **raw_context,
            "steps": [
                row for row in raw_context["steps"] if set(str(value) for value in row.get("target_ids", ())) <= allowed
            ],
        }
        candidates = len(context["steps"])
        scope = {
            "step_ids": [str(row["step_id"]) for row in context["steps"]],
            "target_ids": sorted({str(value) for row in context["steps"] for value in row.get("target_ids", ())}),
        }
        spec = _intelligence_spec(
            session,
            revision,
            capability="tour_synthesis",
            scope=scope,
            context=context,
            output_schema=TOUR_JSON_SCHEMA,
            prompt_version=TOUR_PROMPT_VERSION,
            schema_version=TOUR_SCHEMA_VERSION,
            target_paths=target_paths,
        )
        request = spec.get("request")
        if request is None or candidates == 0:
            return {
                "synthesis": {
                    **_intelligence_status(session, spec, candidates=candidates),
                    "mode": "deterministic_only" if candidates == 0 else "unavailable",
                }
            }
        system_prompt, user_prompt = tour_messages(context)

        def execute() -> Any:
            project_root = _intelligence_project_root(
                session,
                revision,
                capability="tour_synthesis",
                driver=str(spec["driver"]),
                snapshot_identity=str(spec["snapshot_identity"]),
            )
            return execute_structured(
                system=system_prompt,
                prompt=user_prompt,
                output_schema=TOUR_JSON_SCHEMA,
                driver=str(spec["driver"]),
                model=str(spec["model"]) or None,
                project_root=project_root,
                origin="machine",
                timeout_s=spec["budget"].timeout_s,
            )

        outcome = run_intelligence(store=store, request=request, executor=execute)
        if outcome.status == "ready":
            validate_tour_synthesis(outcome.result, tour)
        return {"synthesis": _outcome_status(session, spec, outcome, candidates=candidates)}

    @router.post("/reviews/{review_id}/tour/synthesis")
    async def _post_tour_synthesis(review_id: str, request: Request) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="explaining this Tour")
        return await run_in_threadpool(_synthesize_tour_now, session, revision)

    # ------------------------------------------------------------------ #
    # Behavior deltas (Phase 9R items 11, 12, 18, 38, 43). The contract and
    # every rule live in ``behavior.py``; these are its registration points.
    # ------------------------------------------------------------------ #

    _behavior_cache: dict[str, Any] = {}

    def _behavior_collection(session: ReviewSession, revision: ReviewRevision) -> Any:
        """Every registered producer over this exact Snapshot, cached per evidence set."""

        from lemoncrow.pro.capabilities.review.behavior import (
            BehaviorContext,
            collect_behavior,
            registered_behavior_producers,
        )
        from lemoncrow.pro.capabilities.review.side_effects import PRODUCER_ID as STATIC_PRODUCER_ID

        evidence = _current_evidence(session, revision)
        policy = _browser_policy(revision)
        scenario_paths = {str(scenario.id): tuple(scenario.paths) for scenario in policy.scenarios}
        producers = registered_behavior_producers()
        static_report = _static_report(session, revision)
        # Evidence is append-only, so a new run (or a new producer) is a new key.
        key = hashlib.sha256(
            json.dumps(
                [
                    revision.id,
                    sorted((item.id, item.content_hash) for item in evidence),
                    sorted((name, list(paths)) for name, paths in scenario_paths.items()),
                    [str(producer.producer_id) for producer in producers],
                    static_report.token(),
                ],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cached = _behavior_cache.get(key)
        if cached is not None:
            return cached
        collection = collect_behavior(
            BehaviorContext(
                review_id=session.id,
                revision=revision,
                evidence=evidence,
                read_artifact=store.read_evidence_artifact,
                changed_paths=_changed_paths(revision),
                scenario_paths=scenario_paths,
                prepared={STATIC_PRODUCER_ID: static_report},
            ),
            producers,
        )
        _bounded_cache_put(_behavior_cache, key, collection)
        return collection

    def _static_report(session: ReviewSession, revision: ReviewRevision) -> Any:
        """``side_effects.static`` for this Snapshot: computed once, only when planned."""

        latest = session.status != "archived" and _latest(session).id == revision.id
        return static_side_effects.report(session, revision, latest=latest)

    def _agent_actions(session: ReviewSession, revision: ReviewRevision) -> list[dict[str, Any]]:
        """Consequential authoring-agent actions outside the repository (item 17)."""

        from lemoncrow.pro.capabilities.review.development import DevelopmentUnavailable

        if not development_store.available():
            return []
        try:
            frozen = development_store.frozen_state(session.id, revision.id)
        except DevelopmentUnavailable:
            return []
        rows = list((frozen[0].get("outside") or []) if frozen is not None else [])
        annotated = _approval_annotated(
            [
                {
                    "category": str(row.get("status") or ""),
                    "summary": str(row.get("summary") or ""),
                    "detail": str(row.get("detail") or ""),
                    "host": str(row.get("host") or ""),
                    "session_id": str(row.get("session_id") or ""),
                    "at": str(row.get("at") or ""),
                    "action_digest": str(row.get("action_digest") or ""),
                    "source": str(row.get("source") or ""),
                }
                for row in rows
            ],
            session,
        )
        return [
            {key: row[key] for key in ("category", "summary", "detail", "host", "session_id", "at", "approval")}
            for row in reversed(annotated)
        ][:20]

    def _approval_policy() -> Any:
        """Classes this server requires approval for; ``None`` when unreadable (all count)."""

        from lemoncrow.pro.capabilities.review.action_approval import (
            ApprovalConfigError,
            ApprovalPolicy,
            read_policy,
        )
        from lemoncrow.pro.capabilities.review.fix_agent import settings_root

        if not source_refresh_enabled:
            # A server without the developer's checkout never reads its own
            # machine's settings on their behalf; the hook's requests say which
            # classes were configured where the agent ran.
            return ApprovalPolicy()
        try:
            return read_policy(settings_root())
        except ApprovalConfigError:
            return None

    def _approval_records(session: ReviewSession) -> list[tuple[str, dict[str, Any]]]:
        from lemoncrow.pro.capabilities.review.action_approval import approval_ledger

        ids = [item.id for item in store.list_revisions(session.id)]
        rows = [(revision_id, payload) for revision_id, _stamp, payload in approval_ledger(store).rows(ids)]
        rows.sort(key=lambda item: (str(item[1].get("requested_at") or ""), str(item[1].get("id") or "")))
        return rows

    def _approval_annotated(rows: list[dict[str, Any]], session: ReviewSession) -> list[dict[str, Any]]:
        """Observed side-effect rows with ``approval`` (P12-AGENT), never raising for the Brief."""

        from lemoncrow.pro.capabilities.review.action_approval import annotate_observed

        if not rows:
            return rows
        records = [payload for _revision, payload in _approval_records(session)]
        return annotate_observed(rows, records, _approval_policy())

    def _approval_decisions(session: ReviewSession, *, limit: int = 20) -> list[dict[str, Any]]:
        from lemoncrow.pro.capabilities.review.action_approval import approval_wire, utc_now

        now = utc_now()
        return [
            approval_wire(payload, now, revision_id=revision_id)
            for revision_id, payload in reversed(_approval_records(session))
        ][:limit]

    def _behavior_state(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        historical: bool = False,
    ) -> tuple[Any, tuple[Any, ...]]:
        """``(collection, targets)``: deltas joined with this reviewer's judgments."""

        from lemoncrow.pro.capabilities.review.behavior import behavior_targets

        collection = _behavior_collection(session, revision)
        marks = (
            _marks_visible_at(session, revision)
            if historical
            else store.list_marks(session.id, reviewer_id=session.reviewer_id)
        )
        return collection, behavior_targets(collection.deltas, marks)

    def _coverage_subjects(code_targets: Sequence[Any]) -> tuple[Any, ...]:
        from lemoncrow.pro.capabilities.review.behavior import CoverageSubject

        # Executable changes only: prose sections, dependency manifests and
        # generated/vendored files are not "never executed" news.
        return tuple(
            CoverageSubject(target_id=str(target.target_id), path=str(target.path), label=str(target.label))
            for target in code_targets
            if target.kind not in ("document_section", "dependency") and target.structural_level != "mechanical"
        )

    def _manual_checklist(
        session: ReviewSession,
        revision: ReviewRevision,
        targets: Sequence[Any],
    ) -> tuple[dict[str, Any] | None, tuple[Any, ...]]:
        from lemoncrow.pro.capabilities.review.behavior import (
            ManualVerificationPolicy,
            build_manual_checklist,
            manual_checklist_payload,
            parse_manual_verification_policy,
        )

        authority = _snapshot_review_config(revision)
        parsed = parse_manual_verification_policy(authority.content or "", source=authority.source)
        # Only a known trusted base can make the checklist required.
        policy = (
            parsed
            if authority.base_known
            else ManualVerificationPolicy(integrations=parsed.integrations, source=parsed.source, error=parsed.error)
        )
        items = build_manual_checklist(targets, policy, _changed_paths(revision))
        return manual_checklist_payload(items, policy, _current_evidence(session, revision), revision), items

    def _behavior_narrative(
        session: ReviewSession,
        revision: ReviewRevision,
        targets: Sequence[Any],
        *,
        run: bool,
    ) -> dict[str, Any]:
        """The before/after narrative: one synthesis call at most, deterministic otherwise."""

        from lemoncrow.pro.capabilities.review.behavior import (
            BEHAVIOR_NARRATIVE_JSON_SCHEMA,
            BEHAVIOR_NARRATIVE_PROMPT_VERSION,
            BEHAVIOR_NARRATIVE_SCHEMA_VERSION,
            behavior_narrative_context,
            behavior_narrative_messages,
            narrative_payload,
        )
        from lemoncrow.pro.capabilities.review.data_governance import process_route_trust
        from lemoncrow.pro.capabilities.review.intelligence_runtime import (
            ModelCallBudget,
            execute_structured,
            run_intelligence,
        )
        from lemoncrow.pro.capabilities.review.rules import content_access_for_path

        governance = _review_governance(session, revision)
        route_trust: ModelRouteTrust = "unknown"
        with contextlib.suppress(Exception):
            route_trust = process_route_trust(_resolve_route(session, revision, None, None).driver)
        target_paths = {
            f"{target.behavior_key}#{index}": path
            for target in targets
            for index, path in enumerate(target.delta.paths)
        }
        decisions = {
            key: content_access_for_path(path, governance.content_access, route_trust=route_trust)
            for key, path in target_paths.items()
        }

        def _shareable(target: Any) -> bool:
            keys = [f"{target.behavior_key}#{index}" for index in range(len(target.delta.paths))]
            # A path-less delta cannot be checked against content-access rules,
            # so it reaches a model only when no such rule exists at all.
            return all(decisions[key].allowed for key in keys) if keys else not governance.content_access

        eligible = tuple(target for target in targets if _shareable(target))
        context = behavior_narrative_context(eligible)
        candidates = len(context["deltas"])
        if candidates == 0:
            budget = ModelCallBudget.from_env(max_calls=1)
            return narrative_payload(
                targets,
                synthesis={
                    "status": "none",
                    "mode": "deterministic_only",
                    "model": "",
                    "driver": "",
                    "error": "",
                    "candidates": 0,
                    "policy_digest": "",
                    "cache_hit": False,
                    "excluded_by_policy": [],
                    "budget": {
                        "calls_used": store.intelligence_calls(session.id),
                        "max_calls": budget.max_review_calls,
                    },
                },
                model_result=None,
            )
        spec = _intelligence_spec(
            session,
            revision,
            capability="behavior_narrative",
            scope={"behavior_keys": [str(row["behavior_key"]) for row in context["deltas"]]},
            context=context,
            output_schema=BEHAVIOR_NARRATIVE_JSON_SCHEMA,
            prompt_version=BEHAVIOR_NARRATIVE_PROMPT_VERSION,
            schema_version=BEHAVIOR_NARRATIVE_SCHEMA_VERSION,
            target_paths=target_paths,
        )
        if not run:
            block = _intelligence_status(session, spec, candidates=candidates)
            row = spec.get("row")
            cached: Any = None
            if row is not None and str(row.status) == "ready":
                try:
                    cached = json.loads(row.result_json or "{}")
                except ValueError:
                    cached = None
            return narrative_payload(
                targets,
                synthesis=block,
                model_result=cached if isinstance(cached, Mapping) else None,
                eligible=eligible,
            )
        request = spec.get("request")
        if request is None:
            return narrative_payload(
                targets,
                synthesis={**_intelligence_status(session, spec, candidates=candidates), "mode": "unavailable"},
                model_result=None,
            )
        system_prompt, user_prompt = behavior_narrative_messages(context)

        def execute() -> Any:
            project_root = _intelligence_project_root(
                session,
                revision,
                capability="behavior_narrative",
                driver=str(spec["driver"]),
                snapshot_identity=str(spec["snapshot_identity"]),
            )
            return execute_structured(
                system=system_prompt,
                prompt=user_prompt,
                output_schema=BEHAVIOR_NARRATIVE_JSON_SCHEMA,
                driver=str(spec["driver"]),
                model=str(spec["model"]) or None,
                project_root=project_root,
                origin="machine",
                timeout_s=spec["budget"].timeout_s,
            )

        outcome = run_intelligence(store=store, request=request, executor=execute)
        result = outcome.result if outcome.status == "ready" and isinstance(outcome.result, Mapping) else None
        return narrative_payload(
            targets,
            synthesis=_outcome_status(session, spec, outcome, candidates=candidates),
            model_result=result,
            eligible=eligible,
        )

    def _behavior_payload(
        session: ReviewSession,
        revision: ReviewRevision,
        code_targets: Sequence[Any],
        *,
        historical: bool,
        limit: int = 50,
    ) -> dict[str, Any] | None:
        """The Brief's behavior block, or ``None`` when there is nothing to say."""

        from lemoncrow.pro.capabilities.review.behavior import (
            BEHAVIOR_CONTRACT_VERSION,
            behavior_progress,
            behavior_target_payload,
            coverage_payload,
            execution_coverage,
        )
        from lemoncrow.pro.capabilities.review.verification_delta import auto_approval_blockers

        collection, targets = _behavior_state(session, revision, historical=historical)
        coverage = execution_coverage(_coverage_subjects(code_targets), collection.executed)
        checklist, _items = _manual_checklist(session, revision, targets)
        agent_actions = _agent_actions(session, revision)
        agent_approvals = [] if historical else _approval_decisions(session)
        if (
            not targets
            and coverage is None
            and checklist is None
            and not collection.degraded
            and not agent_actions
            and not agent_approvals
        ):
            return None
        blockers = auto_approval_blockers(target.delta for target in targets)
        return {
            # Phase 9R items 9/26/35/17: the static run, the register delta, the
            # typed auto-approval blockers P10 consumes and agent actions.
            "static_inventory": _static_report(session, revision).summary_wire(),
            "auto_approval": {
                "blocked": bool(blockers),
                "blockers": [blocker.to_wire() for blocker in blockers],
            },
            "agent_actions": agent_actions,
            # P12-AGENT: human decisions on risky agent actions (pending ones can
            # be decided here; denied ones never ran, so they have no action row).
            "agent_approvals": agent_approvals,
            "contract_version": BEHAVIOR_CONTRACT_VERSION,
            "revision_id": revision.id,
            "total": len(targets),
            "progress": behavior_progress(targets),
            # Evidence (and the digest a judgment must echo) is served per target.
            "targets": [behavior_target_payload(target, include_evidence=False) for target in targets[:limit]],
            "unchanged": collection.unchanged,
            "execution_ran": collection.execution_ran,
            "coverage": coverage_payload(coverage),
            "checklist": checklist,
            "narrative": _behavior_narrative(session, revision, targets, run=False) if targets else None,
            "degraded": list(collection.degraded),
            "producers": list(collection.producers),
        }

    def _behavior_code_targets(session: ReviewSession, revision: ReviewRevision) -> tuple[Any, ...]:
        units = store.list_units(revision.id)
        annotations = store.list_annotations(session.id)
        frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            revision,
            units,
            store.list_marks(session.id, reviewer_id=session.reviewer_id),
            annotations=annotations,
        )
        return tuple(
            _derive_targets(session, revision, units, frontier.entries, read_packet_json(store, revision), annotations)
        )

    @router.get("/reviews/{review_id}/behavior")
    def _get_behavior(review_id: str) -> dict[str, Any]:
        """Behavior targets, diff execution coverage, narrative and manual checklist."""

        session = _session(review_id)
        revision = _latest(session)
        payload = _behavior_payload(
            session, revision, _behavior_code_targets(session, revision), historical=False, limit=500
        )
        return {"revision_id": revision.id, "behavior": payload}

    @router.get("/reviews/{review_id}/behavior/targets/{behavior_key}")
    def _get_behavior_target(review_id: str, behavior_key: str) -> dict[str, Any]:
        """One behavior target with the evidence (and digest) a judgment must echo."""

        from lemoncrow.pro.capabilities.review.behavior import behavior_target_payload

        session = _session(review_id)
        revision = _latest(session)
        _collection, targets = _behavior_state(session, revision)
        target = next((item for item in targets if item.behavior_key == behavior_key), None)
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no such behavior change on the latest Snapshot",
            )
        return {"revision_id": revision.id, "target": behavior_target_payload(target, include_evidence=True)}

    def _record_behavior_judgments(
        session: ReviewSession,
        revision: ReviewRevision,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.behavior import (
            BehaviorJudgmentRefused,
            behavior_mark,
            behavior_progress,
            behavior_target_payload,
            check_behavior_judgment,
        )

        raw = body.get("judgments")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="judgments must be a non-empty list")
        if len(raw) > 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="at most 200 behavior judgments at once"
            )
        requested: list[tuple[str, str, str]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="each judgment must be an object"
                )
            requested.append(
                (
                    str(item.get("behavior_key") or ""),
                    str(item.get("state") or "reviewed"),
                    str(item.get("evidence_digest") or ""),
                )
            )
        bulk = len(requested) > 1
        _collection, targets = _behavior_state(session, revision)
        by_key = {target.behavior_key: target for target in targets}
        accepted: list[tuple[Any, Any]] = []
        refused: list[dict[str, str]] = []
        for key, state_value, digest in dict.fromkeys(requested):
            try:
                judged = check_behavior_judgment(by_key.get(key), state=state_value, evidence_digest=digest, bulk=bulk)
            except BehaviorJudgmentRefused as exc:
                refused.append({"behavior_key": key, "code": exc.code, "reason": exc.detail})
                continue
            accepted.append((by_key[key], judged))
        if not bulk and refused:
            code = refused[0]["code"]
            raise HTTPException(
                status_code=(
                    status.HTTP_409_CONFLICT
                    if code in ("evidence_mismatch", "unknown_target")
                    else status.HTTP_422_UNPROCESSABLE_ENTITY
                ),
                detail=refused[0]["reason"],
            )
        recorded: list[dict[str, str]] = []
        try:
            with store.atomic_review_transition():
                store.require_current_revision(session.id, revision.id)
                for target, judged in accepted:
                    mark = store.set_mark(
                        behavior_mark(
                            target,
                            review_id=session.id,
                            revision_id=revision.id,
                            reviewer_id=session.reviewer_id,
                            state=judged,
                        ),
                        event_kind="judgment",
                        event_revision_id=revision.id,
                    )
                    recorded.append({"behavior_key": target.behavior_key, "recorded": mark.state})
                if accepted:
                    store.record_frontier(session.id, session.reviewer_id, revision.id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        _collection, after = _behavior_state(session, revision)
        return {
            "revision_id": revision.id,
            "recorded": recorded,
            "refused": refused,
            "progress": behavior_progress(after),
            "targets": [behavior_target_payload(target, include_evidence=False) for target in after],
        }

    @router.post("/reviews/{review_id}/behavior/marks")
    async def _post_behavior_marks(review_id: str, request: Request) -> dict[str, Any]:
        """Judge behavior targets; each judgment echoes the evidence digest it was shown.

        A single judgment that the action boundary refuses is an error. Several
        judgments are a bulk action: a critical target is never part of one and
        is returned under ``refused`` with the rest recorded.
        """

        from starlette.concurrency import run_in_threadpool

        session = _mutable_session(review_id)
        _refuse_hosted_owned(session, "judgments")
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="judging this behavior change")
        result = await run_in_threadpool(_record_behavior_judgments, session, revision, body)
        await _enforce_armed_landing(session)
        return result

    @router.post("/reviews/{review_id}/behavior/narrative")
    async def _post_behavior_narrative(review_id: str, request: Request) -> dict[str, Any]:
        """Run the one behavior-narrative synthesis call for the latest Snapshot."""

        from starlette.concurrency import run_in_threadpool

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="narrating these behavior changes")

        def _run() -> dict[str, Any]:
            _collection, targets = _behavior_state(session, revision)
            if not targets:
                return {"revision_id": revision.id, "narrative": None}
            return {"revision_id": revision.id, "narrative": _behavior_narrative(session, revision, targets, run=True)}

        return await run_in_threadpool(_run)

    @router.post("/reviews/{review_id}/behavior/checklist", status_code=status.HTTP_201_CREATED)
    async def _post_behavior_checklist(review_id: str, request: Request) -> dict[str, Any]:
        """Record what a human observed for one manual verification item.

        Stored as ``human_observation`` evidence on the latest Snapshot. Every
        authority field is fixed server-side; a body that claims another
        source or attestation class is ignored, never honored.
        """

        from lemoncrow.pro.capabilities.review.behavior import manual_check_evidence

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="recording this manual verification")
        item_id = str(body.get("item_id") or "").strip()

        def _append() -> dict[str, Any]:
            _collection, targets = _behavior_state(session, revision)
            _payload, items = _manual_checklist(session, revision, targets)
            item = next((row for row in items if row.item_id == item_id), None)
            if item is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="no such manual verification item on the latest Snapshot",
                )
            try:
                evidence = manual_check_evidence(
                    item,
                    review_id=session.id,
                    revision_id=revision.id,
                    reviewer_id=session.reviewer_id,
                    status=str(body.get("status") or ""),
                    note=str(body.get("note") or ""),
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
            try:
                record = store.add_evidence(evidence, expected_current_revision_id=revision.id)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            checklist, _items = _manual_checklist(session, revision, targets)
            return {
                "checklist": checklist,
                "evidence": _review_evidence_payload(
                    record, current=True, revision_number=_display_revision_number(revision)
                ),
            }

        result = await asyncio.to_thread(_append)
        await _enforce_armed_landing(session)
        return result

    @router.get("/reviews/{review_id}/measures")
    def _get_measures(review_id: str) -> dict[str, Any]:
        """Spec §13 measures for this Review, derived on read (no stored state)."""

        from lemoncrow.pro.capabilities.review.measures import review_measures

        session = _session(review_id)
        revision = _latest(session)
        plan, ordered, view, states, _checks = _brief_plan(session, revision, historical=False)
        return review_measures(
            target_count=len(ordered),
            author_explained=plan.coverage.author_explained,
            guidance=view,
            decision_states=states,
            decision_events=store.list_decision_events(session.id, reviewer_id=session.reviewer_id),
            syntheses=store.list_intelligence_runs(session.id, capability="brief_synthesis"),
        )

    @router.get("/reviews/{review_id}/projection")
    def _get_projection(review_id: str) -> dict[str, Any]:
        """Versioned review projection for the Brief: plan structure in canonical queue order."""

        session = _session(review_id)
        return _prepare_projection(session, _latest(session), historical=False)

    @router.get("/reviews/{review_id}/revisions/{revision_number}/projection")
    def _get_historical_projection(review_id: str, revision_number: int) -> dict[str, Any]:
        session = _session(review_id)
        return _prepare_projection(session, _revision_number(session, revision_number), historical=True)

    def _prepare_projection(session: ReviewSession, revision: ReviewRevision, *, historical: bool) -> dict[str, Any]:
        token = _projection_progress.start(revision.id)
        try:
            payload = _projection(
                session,
                revision,
                historical=historical,
                progress=lambda completed, detail: _projection_progress.update(revision.id, token, completed, detail),
            )
        except Exception:
            _projection_progress.update(revision.id, token, 0, "Brief preparation failed", state="failed")
            raise
        _projection_progress.update(revision.id, token, BRIEF_STAGES, "Brief ready", state="ready")
        return payload

    @router.get("/reviews/{review_id}/projection/preparation")
    async def _get_projection_preparation(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        revision = _latest(session)
        return {"revision_id": revision.id, **_projection_progress.get(revision.id)}

    @router.get("/reviews/{review_id}/revisions/{revision_number}/projection/preparation")
    async def _get_historical_projection_preparation(review_id: str, revision_number: int) -> dict[str, Any]:
        session = _session(review_id)
        revision = _revision_number(session, revision_number)
        return {"revision_id": revision.id, **_projection_progress.get(revision.id)}

    def _decision_targets(session: ReviewSession, revision: ReviewRevision, target_ids: Sequence[str]) -> list[Any]:
        from lemoncrow.pro.capabilities.review.targets import derive_review_targets

        units = store.list_units(revision.id)
        annotations = store.list_annotations(session.id)
        frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            revision,
            units,
            store.list_marks(session.id, reviewer_id=session.reviewer_id),
            annotations=annotations,
        )
        by_id = {
            target.target_id: target
            for target in derive_review_targets(units, frontier.entries, read_packet_json(store, revision))
        }
        return [by_id[target_id] for target_id in target_ids if target_id in by_id]

    def _decision_states_now(session: ReviewSession, revision: ReviewRevision) -> tuple[Any, ...]:
        from lemoncrow.pro.capabilities.review.targets import derive_review_targets

        units = store.list_units(revision.id)
        annotations = store.list_annotations(session.id)
        packet = read_packet_json(store, revision)
        frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            revision,
            units,
            store.list_marks(session.id, reviewer_id=session.reviewer_id),
            annotations=annotations,
        )
        base = derive_review_targets(
            units, frontier.entries, packet, annotations=annotations, evidence=_current_evidence(session, revision)
        )
        return _with_plan_inputs(session, revision, units, packet, base)[3]

    def _decision_event_payload(event: Any) -> dict[str, Any]:
        return {
            "id": event.id,
            "decision_key": event.decision_key,
            "reviewer_id": event.reviewer_id,
            "revision_id": event.revision_id,
            "action": event.action,
            "disposition": event.disposition,
            "option_key": event.option_key,
            "definition_ref": event.definition_ref,
            "semantic_fingerprint": event.semantic_fingerprint,
            "implementation_fingerprint": event.implementation_fingerprint,
            "reason": event.reason,
            "trigger_kind": event.trigger_kind,
            "trigger_check_key": event.trigger_check_key,
            "trigger_note": event.trigger_note,
            "feedback_annotation_id": event.feedback_annotation_id,
            "prior_event_id": event.prior_event_id,
            "actor_type": event.actor_type,
            "created_at": event.created_at,
        }

    @router.post("/reviews/{review_id}/decisions/{decision_key}/events", status_code=status.HTTP_201_CREATED)
    async def _post_decision_event(review_id: str, decision_key: str, request: Request) -> dict[str, Any]:
        """Record one human disposition of an author-keyed decision (spec §5).

        Human-only: no agent tool writes here. The write is bound to the
        displayed Snapshot and to the newest event the reviewer saw, and it
        never records a source review mark.
        """

        from lemoncrow.pro.capabilities.review.decisions import (
            check_keys,
            decision_event,
            observed_outcome,
            parse_trigger,
            semantic_fingerprint,
        )
        from lemoncrow.pro.capabilities.review.review_plan import decision_state_payload

        session = _mutable_session(review_id)
        _refuse_hosted_owned(session, "judgments")
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="recording this decision")
        action = str(body.get("action") or "").strip()
        if action not in ("accept", "reconfirm", "request_change", "defer", "reopen"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="action must be accept, reconfirm, request_change, defer or reopen",
            )
        expected_scope = str(body.get("expected_semantic_fingerprint") or "").strip()
        if not expected_scope:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="expected_semantic_fingerprint (the decision scope you saw) is required",
            )
        state = next((item for item in _decision_states_now(session, revision) if item.key == decision_key), None)
        if state is None or state.withdrawn or state.scope is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no current author decision {decision_key!r} on this Snapshot",
            )
        if semantic_fingerprint(state.scope) != expected_scope:
            # Same Snapshot, new scope (re-recorded guidance, a new check
            # outcome): a write must never accept a scope nobody saw.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the decision changed since you loaded it; reload before deciding",
            )
        if action not in state.available_actions:
            detail = (
                "the decision's semantic scope changed; accept an option again"
                if action == "reconfirm"
                else f"{action} is not available while this decision is {state.disposition}"
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

        option_key = ""
        if action == "accept":
            option_key = str(body.get("option_key") or "").strip()
            if option_key not in {key for key, _ in state.options}:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="option_key must be one of the decision's options",
                )
        elif action == "reconfirm":
            option_key = state.accepted_option

        feedback_id = ""
        feedback_body = str(body.get("feedback_body") or "").strip() if action == "request_change" else ""
        feedback_path = ""
        if feedback_body:
            # File the request and record the decision in one write, so a
            # refused decision never leaves a stray request-change behind.
            paths = [target.path for target in _decision_targets(session, revision, state.target_ids)]
            feedback_path = str(body.get("feedback_path") or (paths[0] if paths else ""))
            if feedback_path not in paths:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="a requested change must be filed on one of the decision's files",
                )
            if len(feedback_body) > 16_384:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="the requested change is too large"
                )
        elif action == "request_change":
            feedback_id = str(body.get("feedback_annotation_id") or "").strip()
            feedback = store.get_annotation(feedback_id) if feedback_id else None
            if (
                feedback is None
                or feedback.review_id != session.id
                or feedback.kind != "request_change"
                or feedback.parent_id
                or feedback.source != "human"
                or feedback.state in ("resolved", "obsolete")
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="request_change needs an open root human request-change comment on this Review",
                )

        reason = str(body.get("reason") or "").strip()
        trigger = str(body.get("trigger") or "")
        if action == "defer":
            if not reason:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="a deferral needs a reason"
                )
            kind, check_key, _ = parse_trigger(trigger)
            if kind == "check_available":
                configured = {f"browser:{item.id}" for item in _browser_policy(revision).scenarios}
                if check_key not in set(state.relied_checks) | configured:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"{check_key!r} is not a logical check this decision relies on",
                    )
                if any(
                    check_key in check_keys(row) and str(row.get("state") or "") == "current" and observed_outcome(row)
                    for row in _check_rows(session, revision, discover=False)
                ):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"{check_key} already has a result on this Snapshot",
                    )

        event = replace(
            decision_event(
                state,
                action=action,  # type: ignore[arg-type]
                reviewer_id=session.reviewer_id,
                revision_id=revision.id,
                option_key=option_key,
                reason=reason,
                trigger=trigger,
                feedback_annotation_id=feedback_id,
            ),
            review_id=session.id,
            actor_type="human",
        )
        expected_event_id = str(body.get("expected_event_id") or "")
        try:
            if feedback_body:
                with store.atomic_review_transition():
                    store.require_current_revision(session.id, revision.id)
                    latest = state.latest_event
                    if (latest.id if latest is not None else "") != expected_event_id:
                        raise ValueError("decision changed since you loaded it; reload before deciding")
                    filed = annotate(
                        store,
                        session,
                        revision,
                        path=feedback_path,
                        start_line=0,
                        end_line=0,
                        body=feedback_body,
                        kind="request_change",
                        side="new",
                        created_by=session.reviewer_id or "local",
                        created_by_actor="human",
                        source="human",
                        source_id=session.reviewer_id or "local",
                        new_text=revision_new_side_text(store, resolved_repo, session, revision, feedback_path),
                        expected_current_revision_id=revision.id,
                    )
                    recorded = store.append_decision_event(
                        replace(event, feedback_annotation_id=filed.id), expected_prior_event_id=expected_event_id
                    )
            else:
                recorded = store.append_decision_event(event, expected_prior_event_id=expected_event_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        refreshed = next((item for item in _decision_states_now(session, revision) if item.key == decision_key), state)
        numbers = {item.id: item.revision_number for item in store.list_revisions(session.id)}
        return {
            "event": _decision_event_payload(recorded),
            "decision": decision_state_payload(refreshed, numbers=numbers),
        }

    @router.get("/reviews/{review_id}/decisions/{decision_key}/history")
    def _get_decision_history(review_id: str, decision_key: str) -> dict[str, Any]:
        """Every recorded disposition of one decision, oldest first."""

        session = _session(review_id)
        events = store.list_decision_events(session.id, reviewer_id=session.reviewer_id, decision_key=decision_key)
        return {"decision_key": decision_key, "events": [_decision_event_payload(event) for event in events]}

    @router.get("/reviews/{review_id}/frontier")
    def _get_frontier(review_id: str) -> dict[str, Any]:
        """Current reviewer-owned "since my review" projection.

        The baseline is the durable reviewer frontier, never the latest captured
        revision. ``new`` is further filtered by attested content fingerprints,
        so an undo that restores code the reviewer already saw is not presented
        as brand-new work.
        """

        from lemoncrow.pro.capabilities.review.base_movement import base_movement_notes

        session = _session(review_id)
        revision = _latest(session)
        units = store.list_units(revision.id)
        marks = store.list_marks(session.id, reviewer_id=session.reviewer_id)
        annotations = store.list_annotations(session.id)
        frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            revision,
            units,
            marks,
            annotations=annotations,
        )
        baseline = baseline_revision(
            store,
            session.id,
            marks=marks,
            reviewer_id=session.reviewer_id,
        )
        baseline_units = store.list_units(baseline.id) if baseline is not None else ()
        baseline_keys = {unit.unit_key for unit in baseline_units}
        added = tuple(unit.unit_key for unit in units if unit.unit_key not in baseline_keys)
        unseen = unseen_units(
            store,
            session.id,
            units,
            added,
            reviewer_id=session.reviewer_id,
        )
        groups = group_frontier(frontier, unseen)
        ambiguous = ambiguous_symbols(units)

        def row(entry: FrontierEntry) -> dict[str, Any]:
            return {
                "unit_key": entry.unit_key,
                "kind": entry.kind,
                "path": entry.path,
                "symbol": entry.symbol,
                "state": entry.state,
                "label": unit_label(entry, ambiguous=(entry.path, entry.symbol) in ambiguous) or entry.unit_key,
                "attention_rank": entry.attention_rank,
                "changed_since_mark": entry.changed_since_mark,
                "reviewed_revision_id": entry.reviewed_revision_id,
            }

        current_keys = {unit.unit_key for unit in units}
        discarded = _discarded_payload(session, revision)
        discarded_by_key = {str(item.get("unit_key") or ""): item for item in discarded}
        removed_units = [
            {
                "unit_key": unit.unit_key,
                "kind": unit.kind,
                "path": unit.path,
                "symbol": unit.symbol,
                "label": unit_label(unit, ambiguous=(unit.path, unit.symbol) in ambiguous) or unit.unit_key,
                "discarded_state": str(discarded_by_key.get(unit.unit_key, {}).get("state") or ""),
            }
            for unit in baseline_units
            if unit.unit_key not in current_keys
        ]
        return {
            "previous_revision_number": _display_revision_number(baseline) if baseline is not None else None,
            "changed_since_review": [row(entry) for entry in groups.changed_since_review],
            "new": [row(entry) for entry in groups.new],
            "unresolved": [row(entry) for entry in groups.unresolved],
            "unchanged_reviewed": [row(entry) for entry in groups.unchanged_reviewed],
            "not_yet_reviewed": len(groups.not_yet_reviewed),
            "removed_units": removed_units,
            "discarded_verdicts": discarded,
            "notes": list(base_movement_notes(session.review_base_sha, revision, store.list_revisions(session.id))),
            "open_annotations": len(frontier.unresolved_annotation_ids),
            "orphaned_annotations": len(frontier.orphaned_annotation_ids),
            "annotation_moves": [
                {
                    "annotation_id": move.annotation_id,
                    "path": move.path,
                    "status": move.status,
                    "method": move.method,
                    "method_label": ANCHOR_METHOD_LABELS.get(move.method, move.method),
                    "detail": move.detail,
                    "from_line": move.from_line,
                    "to_line": move.to_line,
                }
                for move in store.anchor_moves_on(revision.id)
            ],
        }

    def _surface_runtime(
        review_id: str,
        revision_id: str = "",
        *,
        side: str = "new",
        immutable_execution: bool = False,
    ) -> tuple[Any, Any, Any, Any]:
        if side not in {"old", "new"}:
            raise ValueError("surface side must be old or new")
        session = _session(review_id)
        revision = _requested_revision(session, revision_id)
        packet = read_packet_json(store, revision)
        raw_files = packet.get("files", ()) if isinstance(packet, Mapping) else ()
        changed_paths = tuple(
            str(item.get("path") or "")
            for item in raw_files
            if isinstance(item, Mapping) and str(item.get("path") or "")
        )
        from lemoncrow.pro.capabilities.review.runtimes import built_in_runner_registry
        from lemoncrow.pro.capabilities.review.surfaces import (
            SurfaceContext,
            built_in_registry,
            load_review_surface_config,
        )

        # Exactness below is proven against the source checkout, so that is the
        # tree to read when it holds; repo_root can be a scratch root (the local
        # server's), where discovery finds no config and lists no surfaces.
        surface_repo = resolved_source_repo
        durable_source_tree = store.read_source_tree_artifact(session.id, revision.id, side=side)
        # Ordinary preview/discovery may reuse a checkout proven byte-identical.
        # Runtime Validation passes immutable_execution=True and therefore never
        # executes from that mutable path even when it currently matches.
        checkout_is_exact = False
        if (
            not immutable_execution
            and durable_source_tree is None
            and side == "new"
            and revision.id == _latest(session).id
        ):
            source_state_projection = _source_state_projection(session, revision)
            checkout_is_exact = bool(source_state_projection.get("supported")) and not bool(
                source_state_projection.get("changed")
            )
        if durable_source_tree is not None or not checkout_is_exact:
            from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable

            try:
                surface_repo = _snapshot_side_tree(revision, side, resolved_preview_repo)
            except ReviewSnapshotUnavailable as exc:
                raise RuntimeError(
                    f"exact Snapshot {side}-side surface source is unavailable for {_display_revision_number(revision)}: {exc}"
                ) from exc
        config = load_review_surface_config(surface_repo)
        registry = built_in_registry()
        runners = runner_registry if runner_registry is not None else built_in_runner_registry()
        context = SurfaceContext(
            repo_root=surface_repo,
            store_root=store.root,
            revision=revision,
            changed_paths=changed_paths,
            config=config,
            store=store,
            scratch_root=surface_workspace_root,
            dependency_root=resolved_dependency_repo,
            source_root=resolved_preview_repo,
            execution=_hermetic_execution(session, revision, side),
        )
        return revision, registry, runners, context

    def _discover_surfaces(registry: Any, context: Any) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        # Registries built from plugins isolate each provider's failure; minimal
        # registries (tests, embedders) only implement ``discover``.
        isolated = getattr(registry, "discover_isolated", None)
        if callable(isolated):
            surfaces, issues = isolated(context)
            return tuple(surfaces), tuple(issues)
        return tuple(registry.discover(context)), tuple(getattr(registry, "issues", ()) or ())

    def _discovered_side(
        review_id: str, revision_id: str, side: str
    ) -> tuple[Any, Any, Any, Any, tuple[Any, ...], tuple[Any, ...]]:
        # Materializing a Snapshot side and walking it takes seconds on a large
        # repository, so async routes run this in the threadpool: on the event
        # loop it stalled every other request of the server, health included.
        revision, registry, runners, context = _surface_runtime(review_id, revision_id, side=side)
        surfaces, issues = _discover_surfaces(registry, context)
        return revision, registry, runners, context, surfaces, issues

    @router.get("/reviews/{review_id}/surfaces")
    def _get_surfaces(review_id: str, revision_id: str = "") -> dict[str, Any]:
        """Discover product surfaces from one explicit immutable Snapshot."""

        from lemoncrow.pro.capabilities.review import comparisons as comparisons_module
        from lemoncrow.pro.capabilities.review import previews as previews_module

        try:
            revision, registry, _runners, context = _surface_runtime(review_id, revision_id)
            surfaces, issues = _discover_surfaces(registry, context)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"review surface discovery failed: {exc}"
            ) from exc
        preview_registry = previews_module.built_in_preview_registry()
        comparator_registry = comparisons_module.built_in_comparator_registry()
        rows: list[dict[str, Any]] = []
        for surface in surfaces:
            payload = surface.to_payload()
            if getattr(surface, "preview", None) is not None:
                reason = previews_module.preview_availability(surface, preview_registry, context)
                payload["preview"] = {
                    **payload["preview"],
                    "status": "unavailable" if reason else "available",
                    "reason": reason,
                }
            payload["comparison"] = comparisons_module.comparison_payload(surface, comparator_registry)
            rows.append(payload)
        return {
            "revision_id": revision.id,
            "surfaces": rows,
            "issues": [
                issue.to_payload() for issue in (*issues, *preview_registry.issues, *comparator_registry.issues)
            ],
        }

    @router.get("/reviews/{review_id}/surface-preview")
    async def _get_surface_preview(
        review_id: str,
        provider: str,
        surface_id: str,
        side: str = "new",
        revision_id: str = "",
    ) -> Response:
        """Serve one plugin-rendered preview artifact pinned to one Snapshot side.

        The surface is always resolved from the requested revision's new side
        (the identity the Reader listed); the artifact is rendered from exactly
        the requested side of that same revision, never from a live checkout.
        """

        if side not in {"old", "new"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="side must be old or new")
        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review import previews as previews_module

        try:
            revision, _registry, _runners, context, surfaces, _issues = await run_in_threadpool(
                _discovered_side, review_id, revision_id, "new"
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"review surface discovery failed: {exc}"
            ) from exc
        surface = next((item for item in surfaces if item.provider == provider and item.id == surface_id), None)
        if surface is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"review surface not found: {provider}:{surface_id}"
            )
        descriptor = getattr(surface, "preview", None)
        if descriptor is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"review surface declares no preview: {provider}:{surface_id}",
            )
        from lemoncrow.pro.capabilities.review.execution import RUN_ON_BY_EXECUTOR_CLASS

        preview_session = _session(review_id)
        hermetic = await run_in_threadpool(_hermetic_execution, preview_session, revision, side)
        # Rendering executes the Snapshot here; a reviewer's preview is a manual
        # amendment of the effective run plan.
        run_on = RUN_ON_BY_EXECUTOR_CLASS[hermetic.executor_class]
        planned = await run_in_threadpool(
            _planned_surface_request,
            preview_session,
            revision,
            surface,
            executable=True,
            run_on=run_on,
            action="surface preview",
        )
        started = time.monotonic()
        result = await run_in_threadpool(
            previews_module.render_surface_preview,
            context,
            surface,
            side=side,
            renderers=previews_module.built_in_preview_registry(),
            source_repo=resolved_repo,
            hermetic=hermetic,
        )
        if not result.cached:
            await run_in_threadpool(
                _meter_execution,
                revision,
                planned,
                run_on=run_on,
                execution=result.execution,
                wall_seconds=time.monotonic() - started,
                fallback_stage="surface_preview",
            )
        label = descriptor.label or f"{descriptor.renderer} preview"
        if result.status != "ready" or result.path is None:
            verb = "failed" if result.status == "failed" else "is unavailable"
            detail = f"{label} {verb} on the {side} side: {result.reason}"
            if result.output:
                detail = f"{detail}\n{result.output}"
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
        try:
            body = result.path.read_bytes()
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="preview artifact is unreadable"
            ) from exc
        disposition = "attachment" if result.display == "download" else "inline"
        return Response(
            content=body,
            media_type=result.media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                # Plugin output is untrusted: never let it script, navigate or
                # fetch, even when opened directly instead of in the Reader.
                "Content-Security-Policy": UNTRUSTED_CONTENT_CSP,
                "Content-Disposition": f'{disposition}; filename="{result.filename}"',
                "X-LemonCrow-Revision-Id": revision.id,
                "X-LemonCrow-Preview-Side": side,
                "X-LemonCrow-Preview-Renderer": result.renderer,
                "X-LemonCrow-Run-Plan-Entry": str(planned["entry"]),
                "X-LemonCrow-Run-Plan-Digest": str(planned["digest"]),
            },
        )

    @router.get("/reviews/{review_id}/surface-impact")
    async def _surface_impact(
        review_id: str,
        provider: str,
        surface_id: str,
        revision_id: str = "",
    ) -> dict[str, Any]:
        """Return one normalized old/new impact score for any comparable surface."""

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review import comparisons as comparisons_module
        from lemoncrow.pro.capabilities.review import previews as previews_module

        try:
            revision, _new_registry, new_runners, new_context, new_surfaces, _new_issues = await run_in_threadpool(
                _discovered_side, review_id, revision_id, "new"
            )
            surface = next(
                (item for item in new_surfaces if item.provider == provider and item.id == surface_id),
                None,
            )
            if surface is None:
                raise ValueError(f"review surface not found: {provider}:{surface_id}")
            if not surface.affected_paths:
                raise ValueError("automatic impact ranking requires an affected changed-file path")

            descriptor = comparisons_module.comparison_descriptor(surface)
            if descriptor is None:
                raise ValueError("surface declares no automatic comparison")
            problem = descriptor.problem()
            if problem:
                raise ValueError(problem)
            if not descriptor.auto:
                raise ValueError("surface comparison is not declared safe for automatic execution")

            comparators = comparisons_module.built_in_comparator_registry()
            comparator = comparators.get(descriptor.comparator)
            if comparator is None:
                raise RuntimeError(f"surface comparator {descriptor.comparator!r} is unavailable")
            cache_key = (review_id, revision.id, provider, surface_id, descriptor.comparator)
            cached = surface_impact_cache.get(cache_key)
            if cached is not None:
                return dict(cached)

            score = None
            # Executing either side is a manual amendment of the effective run plan.
            planned: dict[str, Any] | None = None
            impact_session = _session(review_id)
            # A side that failed to render may render next time: its one-sided
            # score answers this request but is not kept for the Snapshot.
            cacheable = True
            if descriptor.source == "preview":
                preview = surface.preview
                if preview is None:
                    raise ValueError("preview comparison requires a preview descriptor")
                if "old" not in preview.sides or "new" not in preview.sides:
                    added = "new" in preview.sides and "old" not in preview.sides
                    label = "Added preview · 100%" if added else "One-sided preview · 100%"
                    score = comparisons_module.ImpactScore(
                        comparator=descriptor.comparator,
                        changed=True,
                        magnitude=1.0,
                        label=label,
                        reasons=("preview exists on only one Snapshot side",),
                        metrics={"one_sided": True, "available_sides": list(preview.sides)},
                    )
                else:
                    from lemoncrow.pro.capabilities.review.execution import RUN_ON_BY_EXECUTOR_CLASS

                    renderers = previews_module.built_in_preview_registry()
                    old_hermetic, new_hermetic = await asyncio.gather(
                        run_in_threadpool(_hermetic_execution, impact_session, revision, "old"),
                        run_in_threadpool(_hermetic_execution, impact_session, revision, "new"),
                    )
                    preview_run_on = RUN_ON_BY_EXECUTOR_CLASS[new_hermetic.executor_class]
                    planned = await run_in_threadpool(
                        _planned_surface_request,
                        impact_session,
                        revision,
                        surface,
                        executable=True,
                        run_on=preview_run_on,
                        action="surface impact",
                    )
                    old_preview, new_preview = await asyncio.gather(
                        run_in_threadpool(
                            previews_module.render_surface_preview,
                            new_context,
                            surface,
                            side="old",
                            renderers=renderers,
                            source_repo=resolved_repo,
                            hermetic=old_hermetic,
                        ),
                        run_in_threadpool(
                            previews_module.render_surface_preview,
                            new_context,
                            surface,
                            side="new",
                            renderers=renderers,
                            source_repo=resolved_repo,
                            hermetic=new_hermetic,
                        ),
                    )
                    for rendered_side in (old_preview, new_preview):
                        if not rendered_side.cached:
                            # Sides render concurrently: each meters its own stages.
                            await run_in_threadpool(
                                _meter_execution,
                                revision,
                                planned,
                                run_on=preview_run_on,
                                execution=rendered_side.execution,
                                wall_seconds=0.0,
                                fallback_stage="surface_preview",
                            )
                    old_ready = old_preview.status == "ready" and old_preview.path is not None
                    new_ready = new_preview.status == "ready" and new_preview.path is not None
                    if old_ready and new_ready:
                        old_value = comparisons_module.ComparisonValue(
                            "old", path=old_preview.path, media_type=old_preview.media_type
                        )
                        new_value = comparisons_module.ComparisonValue(
                            "new", path=new_preview.path, media_type=new_preview.media_type
                        )
                    elif old_ready != new_ready and all(
                        value.status in {"ready", "unavailable"} for value in (old_preview, new_preview)
                    ):
                        label = "New-only preview · 100%" if new_ready else "Old-only preview · 100%"
                        cacheable = False
                        score = comparisons_module.ImpactScore(
                            comparator=descriptor.comparator,
                            changed=True,
                            magnitude=1.0,
                            label=label,
                            reasons=("preview renders on only one Snapshot side",),
                            metrics={
                                "one_sided": True,
                                "old_status": old_preview.status,
                                "new_status": new_preview.status,
                            },
                        )
                    else:
                        failed = next(
                            (
                                value
                                for value in (old_preview, new_preview)
                                if not (value.status == "ready" and value.path is not None)
                            ),
                            None,
                        )
                        assert failed is not None
                        raise RuntimeError(
                            f"{failed.side} preview is {failed.status}: {failed.reason or 'artifact not ready'}"
                        )
            elif descriptor.source == "web":
                if "auto_compare" not in surface.capabilities:
                    raise ValueError("web surface is not declared safe for automatic comparison")
                from lemoncrow.pro.capabilities.review.web_preview import render_web_preview

                route = surface.locator or surface.title or "/"
                document_path = surface.affected_paths[0]
                planned = await run_in_threadpool(
                    _planned_web_preview, impact_session, revision, action="surface impact"
                )
                rendered = await asyncio.gather(
                    run_in_threadpool(
                        render_web_preview,
                        store.root,
                        resolved_preview_repo,
                        revision,
                        path=document_path,
                        route=route,
                        side="old",
                        width=1440,
                        height=5000,
                        store=store,
                        dependency_root=resolved_dependency_repo,
                    ),
                    run_in_threadpool(
                        render_web_preview,
                        store.root,
                        resolved_preview_repo,
                        revision,
                        path=document_path,
                        route=route,
                        side="new",
                        width=1440,
                        height=5000,
                        store=store,
                        dependency_root=resolved_dependency_repo,
                    ),
                    return_exceptions=True,
                )
                old_render, new_render = rendered
                old_ok = not isinstance(old_render, BaseException)
                new_ok = not isinstance(new_render, BaseException)
                if old_ok and new_ok:
                    old_value = comparisons_module.ComparisonValue(
                        "old", path=cast(Path, old_render), media_type="image/png"
                    )
                    new_value = comparisons_module.ComparisonValue(
                        "new", path=cast(Path, new_render), media_type="image/png"
                    )
                elif old_ok != new_ok:
                    cacheable = False
                    score = comparisons_module.ImpactScore(
                        comparator=descriptor.comparator,
                        changed=True,
                        magnitude=1.0,
                        label="New-only render · 100%" if new_ok else "Old-only render · 100%",
                        reasons=("render exists on only one Snapshot side",),
                        metrics={"one_sided": True},
                    )
                else:
                    old_reason = str(old_render)
                    new_reason = str(new_render)
                    raise RuntimeError(f"both Snapshot renders failed: old={old_reason}; new={new_reason}")
            elif descriptor.source == "run":
                if "auto_compare" not in surface.capabilities:
                    raise ValueError("surface runner is not declared safe for automatic comparison")
                if not surface.runtime or "execute" not in surface.capabilities:
                    raise ValueError("surface has no executable runtime for automatic comparison")
                (
                    _old_revision,
                    _old_registry,
                    old_runners,
                    old_context,
                    old_surfaces,
                    _old_issues,
                ) = await run_in_threadpool(_discovered_side, review_id, revision.id, "old")
                old_surface = next(
                    (item for item in old_surfaces if item.provider == provider and item.id == surface_id),
                    None,
                )
                if old_surface is None:
                    score = comparisons_module.ImpactScore(
                        comparator=descriptor.comparator,
                        changed=True,
                        magnitude=1.0,
                        label="Added surface · 100%",
                        reasons=("surface exists only on the new Snapshot side",),
                        metrics={"one_sided": True},
                    )
                else:
                    if "auto_compare" not in old_surface.capabilities:
                        raise ValueError("old Snapshot surface is not declared safe for automatic comparison")
                    runner_run_on = str(getattr(new_runners, "run_on", "") or "local")
                    if str(getattr(old_runners, "run_on", "") or "local") != runner_run_on:
                        raise RuntimeError("Snapshot sides would execute on different executors")
                    planned = await run_in_threadpool(
                        _planned_surface_request,
                        impact_session,
                        revision,
                        surface,
                        executable=True,
                        run_on=runner_run_on,
                        action="surface impact",
                    )
                    old_result, new_result = await asyncio.gather(
                        run_in_threadpool(old_runners.run, old_context, old_surface, side="old"),
                        run_in_threadpool(new_runners.run, new_context, surface, side="new"),
                    )
                    for side_result in (old_result, new_result):
                        if side_result.duration_ms > 0:
                            await run_in_threadpool(
                                _meter_execution,
                                revision,
                                planned,
                                run_on=runner_run_on,
                                execution=(
                                    side_result.data.get("execution") if isinstance(side_result.data, Mapping) else None
                                ),
                                wall_seconds=side_result.duration_ms / 1000.0,
                                fallback_stage="surface_run",
                            )
                    old_value = comparisons_module.ComparisonValue("old", result=old_result)
                    new_value = comparisons_module.ComparisonValue("new", result=new_result)
            else:  # descriptor.problem() currently makes this unreachable.
                raise ValueError(f"unsupported comparison source: {descriptor.source}")

            if score is None:
                try:
                    score = await run_in_threadpool(
                        comparator.compare,
                        comparisons_module.ComparisonRequest(surface, descriptor, old_value, new_value),
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"surface comparator {descriptor.comparator!r} raised {type(exc).__name__}: {exc}"
                    ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except (OSError, RuntimeError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"surface impact unavailable: {exc}"
            ) from exc

        payload = {
            "revision_id": revision.id,
            "provider": provider,
            "surface_id": surface_id,
            "source": descriptor.source,
            **score.to_payload(),
            **({"run_plan": planned} if planned is not None else {}),
        }
        if cacheable:
            if len(surface_impact_cache) >= 1024:
                surface_impact_cache.pop(next(iter(surface_impact_cache)))
            surface_impact_cache[cache_key] = payload
        return dict(payload)

    @router.post("/reviews/{review_id}/surface-run")
    def _run_surface(
        review_id: str,
        provider: str,
        surface_id: str,
        side: str = "new",
        revision_id: str = "",
    ) -> dict[str, Any]:
        """Execute one surface side from one explicit immutable Snapshot."""

        try:
            revision, registry, runners, context = _surface_runtime(review_id, revision_id, side=side)
            surface = next(
                (item for item in registry.discover(context) if item.provider == provider and item.id == surface_id),
                None,
            )
            if surface is None:
                raise ValueError(f"review surface not found: {provider}:{surface_id}")
            # The runtime (the argv that executes) comes from the trusted base,
            # like Runtime Validation: a head-only or head-edited runtime is
            # proposed policy for later Reviews, never executed here (SL-5).
            trusted_config = (
                context.config
                if side == "old"
                else _surface_runtime(review_id, revision.id, side="old", immutable_execution=True)[3].config
            )
            if (
                surface.runtime
                and context.config.runtime(surface.runtime) is not None
                and trusted_config.runtime(surface.runtime) is None
            ):
                raise ValueError(
                    f"review runtime {surface.runtime!r} is not defined by the trusted base; "
                    "a runtime the Change adds runs only after it lands on the base branch"
                )
            context = replace(context, config=trusted_config)
            # A reviewer's run is a manual amendment of the effective run plan.
            run_on = str(getattr(runners, "run_on", "") or "local")
            planned = _planned_surface_request(
                _session(review_id),
                revision,
                surface,
                executable="execute" in surface.capabilities,
                run_on=run_on,
                action="surface run",
            )
            started = time.monotonic()
            result = runners.run(context, surface, side=side)
            if result.duration_ms > 0:
                _meter_execution(
                    revision,
                    planned,
                    run_on=run_on,
                    execution=result.data.get("execution") if isinstance(result.data, Mapping) else None,
                    wall_seconds=max(time.monotonic() - started, result.duration_ms / 1000.0),
                    fallback_stage="surface_run",
                )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except (OSError, RuntimeError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"review surface execution failed: {exc}"
            ) from exc
        return {
            "revision_id": revision.id,
            "result": {**result.to_payload(), "revision_id": revision.id},
            "run_plan": planned,
        }

    @router.get("/reviews/{review_id}/units")
    def _get_units(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        revision = _latest(session)
        units = store.list_units(revision.id)
        marks = {mark.unit_key: mark for mark in store.list_marks(session.id, reviewer_id=session.reviewer_id)}

        def _order(unit: ReviewUnit) -> tuple[int, int, str, int, str]:
            kinds = {"file": 0, "document_section": 1, "symbol": 2, "hunk": 3}
            return (
                1 if unit.attention_rank == 0 else 0,
                unit.attention_rank,
                unit.path,
                kinds.get(unit.kind, 9),
                unit.symbol,
            )

        ambiguous = ambiguous_symbols(units)
        rows: list[dict[str, Any]] = []
        for unit in sorted(units, key=_order):
            payload = _unit_payload(unit, ambiguous=(unit.path, unit.symbol) in ambiguous)
            mark = marks.get(unit.unit_key)
            payload["state"] = mark.state if mark is not None else "unreviewed"
            payload["changed_since_mark"] = mark is not None and mark.content_fingerprint != unit.content_fingerprint
            rows.append(payload)
        return {"revision_id": revision.id, "units": rows}

    def _special_change_for(
        session: ReviewSession,
        revision: ReviewRevision,
        packet: Mapping[str, Any] | None,
        entry: Mapping[str, Any],
        path: str,
    ) -> dict[str, Any] | None:
        """Semantic special-Git-change projection over the packet entry (8F)."""

        from lemoncrow.pro.capabilities.review.special_changes import special_git_change, submodule_repository_url

        url = ""
        if entry.get("submodule_pointer") is not None:
            # Linked repository context is optional: an unreadable .gitmodules
            # leaves the commit pair as the whole (still exact) answer.
            for side in ("new", "old"):
                try:
                    raw, _reason = _revision_asset_bytes(
                        store, session.id, revision, resolved_repo, packet, ".gitmodules", side=side
                    )
                except (OSError, ValueError, KeyError, RuntimeError):
                    raw = None
                if raw:
                    url = submodule_repository_url(raw.decode("utf-8", errors="replace"), path)
                if url:
                    break
        return special_git_change(entry, submodule_url=url)

    def _patch_payload(
        session: ReviewSession,
        revision: ReviewRevision,
        path: str,
        *,
        historical: bool,
        include_web_preview: bool = True,
    ) -> dict[str, Any]:
        units = store.list_units(revision.id)

        # Two independent gates, and the order matters. Confinement first, so a
        # traversal never reaches the membership lookup; membership second, so a
        # path that is genuinely inside the repository but is not part of this
        # change is still a 404. The payload itself always comes from the frozen
        # revision artifact, never from today's worktree.
        try:
            confine_to_root(resolved_repo / path, resolved_repo)
        except (ValueError, OSError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file in this review") from None
        if not any(unit.kind == "file" and unit.path == path for unit in units):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file in this review")

        packet = read_packet_json(store, revision)
        artifacts = tuple(
            item
            for item in _evidence_at(session, revision, historical=historical)
            if _evidence_is_current(item, revision)
        )
        entry = _files_by_path(packet).get(path)
        if entry is None:
            return {
                "path": path,
                "revision_id": revision.id,
                "patch": "",
                "renderable": False,
                "refusal": "packet_unavailable" if packet is None else "file_not_in_packet",
                "detail": (
                    "the packet that recorded this revision is not readable"
                    if packet is None
                    else "this revision's packet has no entry for that path"
                ),
                "historical": historical,
                "degraded": [{"name": name, "note": degraded_note(name)} for name in sorted(set(revision.degraded))],
            }
        text, refusal, detail = synthesize_file_patch(entry)
        special_change = _special_change_for(session, revision, packet, entry, path)
        if special_change is not None and refusal == "no_hunks":
            # A content-free rename or mode change is the packet's fact, not a
            # diff that failed to render.
            refusal, detail = "", ""
        preview = (
            None
            if refusal == "file_too_large"
            else (
                _markdown_preview_payload(store, session.id, revision, resolved_repo, entry)
                or _csv_preview_payload(store, session.id, revision, resolved_repo, entry)
                or _media_preview_payload(entry)
                or (
                    _web_preview_payload(store, revision, resolved_preview_repo, entry) if include_web_preview else None
                )
            )
        )
        media_renderable = isinstance(preview, Mapping) and preview.get("kind") == "media"
        return {
            "path": path,
            "revision_id": revision.id,
            "old_path": entry.get("old_path") or "",
            "status": str(entry.get("status") or "modified"),
            "language": entry.get("language") or "",
            "additions": int(entry.get("additions") or 0),
            "deletions": int(entry.get("deletions") or 0),
            "patch": text,
            "renderable": media_renderable or not refusal,
            "refusal": "" if media_renderable else refusal,
            "detail": "" if media_renderable else detail,
            "special_change": special_change,
            "preview": preview,
            "symbols": _symbols_for(packet, path),
            "provenance": _provenance_for(packet, path),
            "evidence": _evidence(packet, artifacts, path=path, revision_id=revision.id),
            "historical": historical,
            "degraded": [{"name": name, "note": degraded_note(name)} for name in sorted(set(revision.degraded))],
        }

    def _review_patch_response(session: ReviewSession, revision: ReviewRevision) -> Response:
        packet = read_packet_json(store, revision)
        raw_files = packet.get("files") if isinstance(packet, Mapping) else None
        if not isinstance(raw_files, list):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this Review revision has no recorded patch artifact",
            )

        chunks: list[str] = []
        refused: list[str] = []
        for raw_entry in raw_files:
            if not isinstance(raw_entry, Mapping):
                continue
            text, refusal, detail = synthesize_file_patch(raw_entry)
            if refusal:
                path = str(raw_entry.get("path") or "<unknown>")
                refused.append(f"{path}: {detail or refusal}")
                continue
            if text:
                chunks.append(text)

        if refused:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cannot export a complete git-apply patch: " + "; ".join(refused),
            )
        if not chunks:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this Review revision contains no textual patch hunks",
            )

        filename = f"lemoncrow-review-rev-{_display_revision_number(revision)}.patch"
        return Response(
            content="".join(chunks),
            media_type="text/x-diff",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/reviews/{review_id}/patch")
    def _get_review_patch(review_id: str) -> Response:
        session = _session(review_id)
        return _review_patch_response(session, _latest(session))

    @router.get("/reviews/{review_id}/revisions/{revision_number}/patch")
    def _get_historical_review_patch(review_id: str, revision_number: int) -> Response:
        session = _session(review_id)
        return _review_patch_response(session, _revision_number(session, revision_number))

    def _file_contents_payload(
        session: ReviewSession,
        revision: ReviewRevision,
        path: str,
    ) -> dict[str, Any]:
        """Return exact old/new text needed to expand collapsed diff context."""

        try:
            confine_to_root(resolved_repo / path, resolved_repo)
        except (ValueError, OSError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file in this review") from None
        units = store.list_units(revision.id)
        if not any(unit.kind == "file" and unit.path == path for unit in units):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file in this review")

        packet = read_packet_json(store, revision)
        entry = _files_by_path(packet).get(path)
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="this Review revision has no file artifact for that path"
            )
        if bool(entry.get("is_binary")):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="binary files do not have expandable text context"
            )

        file_status = str(entry.get("status") or "modified")
        old_path = str(entry.get("old_path") or "") or path
        old_content = (
            None
            if file_status == "added"
            else _snapshot_text(store, session.id, revision, resolved_repo, old_path, side="old")
        )
        new_content = (
            None
            if file_status == "deleted"
            else _snapshot_text(store, session.id, revision, resolved_repo, path, side="new")
        )
        if file_status != "added" and old_content is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the frozen old-side file contents are unavailable for this Review revision",
            )
        if file_status != "deleted" and new_content is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the frozen new-side file contents are unavailable for this Review revision",
            )
        return {
            "path": path,
            "revision_id": revision.id,
            "old_path": old_path,
            "status": file_status,
            "old_content": old_content,
            "new_content": new_content,
        }

    @router.get("/reviews/{review_id}/files/{path:path}/contents")
    def _get_file_contents(review_id: str, path: str, revision_id: str = "") -> dict[str, Any]:
        session = _session(review_id)
        return _file_contents_payload(session, _requested_revision(session, revision_id), path)

    @router.get("/reviews/{review_id}/files/{path:path}/patch")
    def _get_patch(review_id: str, path: str, include_web_preview: bool = True) -> dict[str, Any]:
        session = _session(review_id)
        return _patch_payload(
            session,
            _latest(session),
            path,
            historical=False,
            include_web_preview=include_web_preview,
        )

    @router.get("/reviews/{review_id}/revisions/{revision_number}/files/{path:path}/patch")
    def _get_historical_patch(
        review_id: str, revision_number: int, path: str, include_web_preview: bool = True
    ) -> dict[str, Any]:
        session = _session(review_id)
        return _patch_payload(
            session,
            _revision_number(session, revision_number),
            path,
            historical=True,
            include_web_preview=include_web_preview,
        )

    def _web_preview_revision(
        review_id: str, revision_id: str, document_path: str, route: str, *, missing: str
    ) -> ReviewRevision:
        """The revision whose frozen preview offers *route*: packet reads, run off the event loop."""

        session = _session(review_id)
        revision = _requested_revision(session, revision_id)
        packet = read_packet_json(store, revision)
        entry = _files_by_path(packet).get(document_path)
        preview = (
            _web_preview_payload(store, revision, resolved_preview_repo, entry)
            if entry is not None
            else _frozen_nested_web_preview_payload(store, revision, resolved_preview_repo, document_path)
        )
        if preview is None or route not in preview.get("routes", ()):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=missing)
        return revision

    @router.get("/reviews/{review_id}/web-preview")
    async def _get_web_preview(
        review_id: str,
        document_path: str,
        side: str,
        route: str,
        revision_id: str = "",
        width: int = 1440,
        height: int = 5000,
    ) -> Response:
        """Render one revision-pinned frontend route as a frozen PNG."""

        if side not in {"old", "new"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="side must be old or new")
        if width < 320 or width > 2560 or height < 480 or height > 8000:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unsupported preview viewport")

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.web_preview import WebPreviewUnavailable, render_web_preview

        revision = await run_in_threadpool(
            _web_preview_revision,
            review_id,
            revision_id,
            document_path,
            route,
            missing="web preview is unavailable for this route",
        )
        # Building and rendering the route executes the Snapshot: a reviewer's
        # preview is a manual amendment of the effective run plan.
        planned = await run_in_threadpool(_planned_web_preview, _session(review_id), revision, action="web preview")

        try:
            image_path = await run_in_threadpool(
                render_web_preview,
                store.root,
                resolved_preview_repo,
                revision,
                path=document_path,
                route=route,
                side=side,
                width=width,
                height=height,
                store=store,
                dependency_root=resolved_dependency_repo,
            )
            payload = image_path.read_bytes()
        except WebPreviewUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="preview artifact is unreadable"
            ) from exc
        return Response(
            content=payload,
            media_type="image/png",
            headers={
                "X-LemonCrow-Run-Plan-Entry": str(planned["entry"]),
                "X-LemonCrow-Run-Plan-Digest": str(planned["digest"]),
            },
        )

    @router.get("/reviews/{review_id}/web-live-preview")
    async def _get_web_live_preview(
        review_id: str,
        document_path: str,
        side: str,
        route: str,
        revision_id: str = "",
    ) -> dict[str, str]:
        """Return an isolated live URL for one exact revision-pinned static route."""

        if side not in {"old", "new"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="side must be old or new")

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.web_preview import WebPreviewUnavailable, ensure_live_web_preview

        revision = await run_in_threadpool(
            _web_preview_revision,
            review_id,
            revision_id,
            document_path,
            route,
            missing="web preview is unavailable for this route",
        )
        await run_in_threadpool(_planned_web_preview, _session(review_id), revision, action="web live preview")

        try:
            url = await run_in_threadpool(
                ensure_live_web_preview,
                store.root,
                resolved_preview_repo,
                revision,
                path=document_path,
                route=route,
                side=side,
                store=store,
                dependency_root=resolved_dependency_repo,
            )
        except WebPreviewUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return {"url": url}

    @router.get("/reviews/{review_id}/markdown-image")
    async def _get_markdown_image(
        review_id: str,
        document_path: str,
        side: str,
        src: str,
        revision_id: str = "",
    ) -> Response:
        """Serve one image explicitly referenced by a Markdown preview.

        Repository images are read from the reviewed revision, never by following
        a worktree path. Remote images are fetched server-side through the shared
        SSRF guard so the browser never receives arbitrary remote origins in its
        CSP and the workspace token never appears in an image URL.
        """

        if side not in {"old", "new"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="side must be old or new")

        from starlette.concurrency import run_in_threadpool

        # The packet, preview and revision bytes are store reads: off the loop.
        body, media_type = await run_in_threadpool(
            _markdown_repository_image, review_id, document_path, side, src, revision_id
        )
        if body is not None:
            return _untrusted_content_response(body, media_type)

        try:
            from lemoncrow.core.capabilities.web_fetch import async_fetch_image
        except ModuleNotFoundError as exc:
            # The guarded fetcher is Runtime's web tool; the Review-only engine serves
            # repository images only.
            if not module_absent(exc, REMOTE_IMAGES):
                raise
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="remote Markdown images are not available in this build",
            ) from exc
        try:
            remote_body, media_type = await async_fetch_image(src)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return _untrusted_content_response(remote_body, media_type)

    def _markdown_repository_image(
        review_id: str, document_path: str, side: str, src: str, revision_id: str
    ) -> tuple[bytes | None, str]:
        """A referenced repository image's bytes and type; ``(None, "")`` for a remote one to fetch."""

        session = _session(review_id)
        revision = _requested_revision(session, revision_id)
        packet = read_packet_json(store, revision)
        entry = _files_by_path(packet).get(document_path)
        if entry is None or not _is_markdown_path(document_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no such Markdown document in this review"
            )
        preview = _markdown_preview_payload(store, session.id, revision, resolved_repo, entry)
        if preview is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Markdown preview is unavailable")
        content = preview["old_content" if side == "old" else "new_content"]
        if src not in _markdown_image_sources(content):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="image is not referenced by this document"
            )

        parsed = urlsplit(src)
        if parsed.scheme:
            if parsed.scheme.lower() not in {"http", "https"}:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unsupported image source")
            return None, ""

        asset_path = _markdown_asset_repo_path(document_path, src)
        if asset_path is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid repository image path")
        try:
            confine_to_root(resolved_repo / asset_path, resolved_repo)
        except (ValueError, OSError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid repository image path") from None

        media_type = mimetypes.guess_type(asset_path)[0] or ""
        if not media_type.startswith("image/"):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="referenced asset is not an image"
            )

        body, reason = _revision_asset_bytes(
            store,
            session.id,
            revision,
            resolved_repo,
            packet,
            asset_path,
            side=side,
        )
        if body is None:
            code = (
                status.HTTP_409_CONFLICT
                if "did not preserve" in reason or "tree is unavailable" in reason
                else status.HTTP_404_NOT_FOUND
            )
            raise HTTPException(status_code=code, detail=reason or "image is unavailable")
        return body, media_type

    @router.get("/reviews/{review_id}/media-file")
    def _get_media_file(review_id: str, path: str, side: str, revision_id: str = "") -> Response:
        """Serve one changed media file from the immutable reviewed revision."""

        if side not in {"old", "new"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="side must be old or new")
        session = _session(review_id)
        revision = _requested_revision(session, revision_id)
        packet = read_packet_json(store, revision)
        entry = _files_by_path(packet).get(path)
        if entry is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such media file in this review")
        preview = _media_preview_payload(entry)
        if preview is None:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="file is not browser-previewable media"
            )
        try:
            confine_to_root(resolved_repo / path, resolved_repo)
        except (ValueError, OSError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid media path") from None
        body, reason = _revision_asset_bytes(
            store,
            session.id,
            revision,
            resolved_repo,
            packet,
            path,
            side=side,
        )
        if body is None:
            code = (
                status.HTTP_409_CONFLICT
                if "did not preserve" in reason or "tree is unavailable" in reason
                else status.HTTP_404_NOT_FOUND
            )
            raise HTTPException(status_code=code, detail=reason or "media is unavailable")
        return _untrusted_content_response(body, str(preview["media_type"]))

    def _related_payload(session: ReviewSession, revision: ReviewRevision, path: str, line: int) -> dict[str, Any]:
        try:
            confine_to_root(resolved_repo / path, resolved_repo)
        except (ValueError, OSError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such related source") from None

        packet = read_packet_json(store, revision)
        impact_paths = {
            str(item.get("path") or "").split(":L", 1)[0]
            for item in ((packet or {}).get("impact") or ())
            if isinstance(item, dict) and not bool(item.get("in_patch"))
        }
        if path not in impact_paths:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="path is not impact evidence for this review revision"
            )

        text, refusal = _related_source_text(revision, path)
        if refusal:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refusal)
        lines = text.splitlines()
        focus = min(max(1, int(line or 1)), max(1, len(lines)))
        start_line = max(1, focus - 35)
        end_line = min(len(lines), focus + 35)
        suffix = path.rsplit(".", 1)[-1] if "." in path else ""
        return {
            "path": path,
            "language": suffix,
            "start_line": start_line,
            "end_line": end_line,
            "focus_line": focus,
            "text": "\n".join(lines[start_line - 1 : end_line]),
        }

    @router.get("/reviews/{review_id}/related/{path:path}")
    def _get_related_source(review_id: str, path: str, line: int = 1) -> dict[str, Any]:
        session = _session(review_id)
        return _related_payload(session, _latest(session), path, line)

    @router.get("/reviews/{review_id}/revisions/{revision_number}/related/{path:path}")
    def _get_historical_related_source(
        review_id: str, revision_number: int, path: str, line: int = 1
    ) -> dict[str, Any]:
        session = _session(review_id)
        return _related_payload(session, _revision_number(session, revision_number), path, line)

    @router.post("/reviews/{review_id}/marks")
    async def _post_mark(review_id: str, request: Request, response: Response) -> dict[str, Any]:
        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="recording this mark", require_saved=False)

        from starlette.concurrency import run_in_threadpool

        queued = await run_in_threadpool(_queue_reader_write, session, "mark", body)
        if queued is not None:
            response.status_code = status.HTTP_202_ACCEPTED
            return queued
        _refuse_hosted_owned(session, "judgments")
        _require_saved_snapshot(revision)

        # Target derivation and the mark transaction, which can wait out another
        # writer's lock, run off the event loop the local server shares.
        payload = await run_in_threadpool(_record_mark, session, revision, body)
        await _enforce_armed_landing(session)
        return payload

    def _record_mark(session: ReviewSession, revision: ReviewRevision, body: dict[str, Any]) -> dict[str, Any]:
        target = str(body.get("unit_key") or body.get("path") or "")
        if not target:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unit_key is required")
        try:
            requested = coerce_mark_state(str(body.get("state") or "reviewed"))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        from lemoncrow.pro.capabilities.review.targets import priorities_payload, target_payload

        units = store.list_units(revision.id)
        by_key = {unit.unit_key: unit for unit in units}
        annotations = store.list_annotations(session.id)
        packet = read_packet_json(store, revision)

        def _targets_now() -> tuple[Any, ...]:
            frontier = compute_frontier(
                session.id,
                session.reviewer_id,
                revision,
                units,
                store.list_marks(session.id, reviewer_id=session.reviewer_id),
                annotations=annotations,
            )
            return _derive_targets(session, revision, units, frontier.entries, packet, annotations)

        # A bare `path` -- and a file's `fil:` key -- is the imprecise spelling,
        # and it has to widen onto that file's targets exactly as
        # `lc review --mark PATH` does. Progress and closure here are denominated
        # in reader targets, and no target is keyed to a file unit unless the
        # whole file *is* the one target, so the verdict this endpoint used to
        # park on the file unit moved none of the counters it then returned in
        # the same response. Only a spelling that can widen pays for the pre-mark
        # target derivation; a precise `hun:`/`sym:` key resolves against none.
        scope: tuple[Any, ...] = _targets_now() if target not in by_key or by_key[target].kind == "file" else ()
        resolution = resolve_mark_units(units, scope, target)
        if not resolution.units:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no review area matches {target!r} in Revision {_display_revision_number(revision)}",
            )

        marks: list[dict[str, Any]] = []
        try:
            with store.atomic_review_transition():
                store.require_current_revision(session.id, revision.id)
                for unit in resolution.units:
                    recorded = mark_unit(
                        store,
                        session,
                        revision,
                        unit,
                        state=requested,
                        reviewer_id=session.reviewer_id,
                        expected_current_revision_id=revision.id,
                    )
                    note = mark_downgrade_note(unit, requested, recorded.state)
                    marks.append(
                        {
                            "unit_key": unit.unit_key,
                            "label": unit.path,
                            "path": unit.path,
                            "recorded": recorded.state,
                            "downgraded": bool(note),
                            "note": note,
                        }
                    )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        overview = _overview(session)

        # R21: return the reader target that was actually judged plus the new
        # target denominator.  Legacy file groups remain during the R22
        # transition, but a target-aware client no longer has to refetch the
        # whole review after every `r`.
        targets = _targets_now()
        # Named the way every reader surface names them. Labels are derived from
        # the units and the packet, never from a verdict, so reading them off
        # the post-mark targets is the same word the pre-mark list would have
        # given -- and it is the one list that is always derived.
        label_by_key = {item.unit_key: item.label for item in targets}
        for row in marks:
            row["label"] = label_by_key.get(str(row["unit_key"]), row["path"])
        first = resolution.units[0]
        marked_paths = {item.path for item in resolution.units}
        changed_target = next((item for item in targets if item.unit_key == first.unit_key), None)
        path_updates = [item for item in overview["outline"] if item.get("path") in marked_paths]
        payload: dict[str, Any] = {
            # The first target the spelling landed on, so a client that sent a
            # precise unit key sees exactly the shape it always saw. `marks` is
            # the whole truth beside it: a path that widens onto N targets
            # records N verdicts, and every one of them is listed there with the
            # label the reader surfaces print for it.
            "unit_key": first.unit_key,
            "requested": requested,
            "recorded": marks[0]["recorded"],
            "downgraded": any(row["downgraded"] for row in marks),
            "note": next((row["note"] for row in marks if row["note"]), ""),
            "marks": marks,
            "target": target_payload(changed_target) if changed_target is not None else None,
            "progress": overview["progress"],
            "outline_updates": path_updates,
            # The whole queue: a verdict moves positions of targets it did not judge.
            "priorities": priorities_payload(targets),
            "outline": overview["outline"],
            "groups": overview["groups"],
            "group_counts": overview["group_counts"],
        }
        if resolution.shadowed:
            # A file the repository really contains outranked a target label
            # spelled the same way. Never silent: one word asked two questions
            # and this names the reading that lost, so a caller who meant the
            # other one can send its unit key instead.
            payload["shadowed"] = [{"unit_key": item.unit_key, "path": item.path} for item in resolution.shadowed]
        return payload

    @router.post("/reviews/{review_id}/marks/bulk")
    async def _post_bulk_reviewed(review_id: str, request: Request) -> dict[str, Any]:
        """Mark an explicitly supplied ordinary/mechanical target remainder reviewed.

        Every key must identify a target in the current derived reader
        projection. Stale ids and overlapping raw file/hunk/symbol units are
        refused rather than interpreted as broad review judgments. High-attention,
        changed, needs-changes, unknown-identity, and unresolved request-change
        targets are never swept.
        """

        session = _mutable_session(review_id)
        _refuse_hosted_owned(session, "judgments")
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="recording these marks")

        from starlette.concurrency import run_in_threadpool

        # Target derivation and the marks transaction run off the event loop.
        result = await run_in_threadpool(_record_bulk_reviewed, session, revision, body)
        await _enforce_armed_landing(session)
        return result

    def _record_bulk_reviewed(session: ReviewSession, revision: ReviewRevision, body: dict[str, Any]) -> dict[str, Any]:
        raw_keys = body.get("unit_keys")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unit_keys must be a non-empty list")
        keys = tuple(dict.fromkeys(str(item) for item in raw_keys if str(item)))
        if not keys:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unit_keys must contain a review unit")
        if len(keys) > 500:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="at most 500 units may be marked at once"
            )

        all_units = {unit.unit_key: unit for unit in store.list_units(revision.id)}
        annotations = store.list_annotations(session.id)
        marks = store.list_marks(session.id, reviewer_id=session.reviewer_id)
        packet = read_packet_json(store, revision)
        frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            revision,
            tuple(all_units.values()),
            marks,
            annotations=annotations,
        )
        from lemoncrow.pro.capabilities.review.targets import (
            paths_with_unattributed_request_change,
            priorities_payload,
            target_has_unresolved_request_change,
            target_keys_by_path,
        )

        current_targets = _derive_targets(
            session,
            revision,
            tuple(all_units.values()),
            frontier.entries,
            packet,
            annotations,
        )
        target_by_key = {target.unit_key: target for target in current_targets}
        # The same sibling-key set ``derive_review_targets`` counted threads
        # with, so the gate below and the counts in GET /targets cannot answer
        # "whose thread is this?" two different ways inside one request.
        keys_by_path = target_keys_by_path(current_targets)
        objected_paths = paths_with_unattributed_request_change(current_targets, annotations)
        marked: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        touched_paths: set[str] = set()
        with store.atomic_review_transition():
            try:
                store.require_current_revision(session.id, revision.id)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            for key in keys:
                target = target_by_key.get(key)
                unit = all_units.get(key)
                if target is None or unit is None:
                    skipped.append(
                        {
                            "unit_key": key,
                            "path": unit.path if unit is not None else "",
                            "reason": "not a current review target",
                        }
                    )
                    continue
                if target.state != "unreviewed":
                    skipped.append(
                        {
                            "unit_key": key,
                            "path": target.path,
                            "reason": {
                                "changed_since_review": "changed since your review",
                                "needs_changes": "still needs individual attention",
                                "reviewed": "already reviewed",
                                "unknown": "content identity is unavailable; review individually",
                            }.get(target.state, f"current state is {target.state}"),
                        }
                    )
                    continue
                if target_has_unresolved_request_change(
                    target,
                    annotations,
                    path_target_keys=keys_by_path.get(target.path, frozenset()),
                ):
                    skipped.append(
                        {
                            "unit_key": key,
                            "path": target.path,
                            "reason": "open request-change discussion requires individual attention",
                        }
                    )
                    continue
                if target.path in objected_paths:
                    skipped.append(
                        {
                            "unit_key": key,
                            "path": target.path,
                            "reason": "an open request-change on this file belongs to no target; review individually",
                        }
                    )
                    continue
                if target.verification.fail_count > 0 or target.verification.unknown_count > 0:
                    skipped.append(
                        {
                            "unit_key": key,
                            "path": target.path,
                            "reason": "verification is not settled",
                        }
                    )
                    continue
                if target.priority.tier <= TIER_REVIEW_FIRST:
                    # Queue tiers 1-3 are actions (spec §7); a bulk sweep is
                    # never allowed to close one of them.
                    first_reason = target.priority.reasons[0].detail if target.priority.reasons else ""
                    skipped.append(
                        {
                            "unit_key": key,
                            "path": target.path,
                            "reason": (
                                f"{first_reason}; review individually"
                                if first_reason
                                else "still needs individual attention"
                            ),
                        }
                    )
                    continue
                if unit.fingerprint_method == "unknown":
                    skipped.append(
                        {
                            "unit_key": key,
                            "path": unit.path,
                            "reason": "content identity is unavailable; review individually",
                        }
                    )
                    continue

                recorded = mark_unit(
                    store,
                    session,
                    revision,
                    unit,
                    state="reviewed",
                    reviewer_id=session.reviewer_id,
                    expected_current_revision_id=revision.id,
                )
                if recorded.state != "reviewed":
                    skipped.append(
                        {
                            "unit_key": key,
                            "path": unit.path,
                            "reason": mark_downgrade_note(unit, "reviewed", recorded.state)
                            or f"recorded as {recorded.state}",
                        }
                    )
                    continue
                events = store.list_mark_events(
                    session.id,
                    reviewer_id=session.reviewer_id,
                    unit_key=key,
                )
                event = events[-1] if events else None
                if event is None or event.to_state != "reviewed" or event.revision_id != revision.id:
                    raise RuntimeError("bulk mark was recorded without its authoritative judgment event")
                marked.append({"unit_key": key, "path": unit.path, "event_id": event.id})
                touched_paths.add(unit.path)

        after = _overview(session)
        after_frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            revision,
            tuple(all_units.values()),
            store.list_marks(session.id, reviewer_id=session.reviewer_id),
            annotations=annotations,
        )
        after_targets = _derive_targets(
            session, revision, tuple(all_units.values()), after_frontier.entries, packet, annotations
        )
        return {
            "marked": marked,
            "skipped": skipped,
            "progress": after["progress"],
            "outline_updates": [item for item in after["outline"] if item.get("path") in touched_paths],
            "priorities": priorities_payload(after_targets),
            "outline": after["outline"],
            "group_counts": after["group_counts"],
        }

    @router.post("/reviews/{review_id}/marks/bulk/undo")
    async def _undo_bulk_reviewed(review_id: str, request: Request) -> dict[str, Any]:
        """Undo exactly one server-returned bulk judgment set.

        Each item carries the mark-event id created by the original bulk write.
        If any newer human judgment exists for a unit, that unit is left alone.
        """

        session = _mutable_session(review_id)
        _refuse_hosted_owned(session, "judgments")
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="undoing these marks")
        raw = body.get("marks")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="marks must be a non-empty list")
        if len(raw) > 500:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="at most 500 marks may be undone at once"
            )

        requested: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="each undo mark must be an object"
                )
            key = str(item.get("unit_key") or "")
            event_id = str(item.get("event_id") or "")
            if not key or not event_id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="each undo mark requires unit_key and event_id",
                )
            if key not in seen:
                seen.add(key)
                requested.append((key, event_id))

        all_units = {unit.unit_key: unit for unit in store.list_units(revision.id)}
        undone: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        touched_paths: set[str] = set()
        with store.atomic_review_transition():
            try:
                store.require_current_revision(session.id, revision.id)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            marks = {mark.unit_key: mark for mark in store.list_marks(session.id, reviewer_id=session.reviewer_id)}
            for key, event_id in requested:
                unit = all_units.get(key)
                mark = marks.get(key)
                events = store.list_mark_events(session.id, reviewer_id=session.reviewer_id, unit_key=key)
                latest_event = events[-1] if events else None
                if (
                    unit is None
                    or mark is None
                    or mark.state != "reviewed"
                    or mark.reviewed_revision_id != revision.id
                    or latest_event is None
                    or latest_event.id != event_id
                    or latest_event.to_state != "reviewed"
                ):
                    skipped.append(
                        {
                            "unit_key": key,
                            "path": unit.path if unit is not None else "",
                            "reason": "a newer or different reviewer judgment exists",
                        }
                    )
                    continue
                reverted = mark_unit(
                    store,
                    session,
                    revision,
                    unit,
                    state="unreviewed",
                    reviewer_id=session.reviewer_id,
                    expected_current_revision_id=revision.id,
                )
                if reverted.state != "unreviewed":
                    skipped.append(
                        {
                            "unit_key": key,
                            "path": unit.path,
                            "reason": f"undo recorded as {reverted.state}",
                        }
                    )
                    continue
                undone.append({"unit_key": key, "path": unit.path})
                touched_paths.add(unit.path)

        after = _overview(session)
        annotations = store.list_annotations(session.id)
        units = tuple(all_units.values())
        frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            revision,
            units,
            store.list_marks(session.id, reviewer_id=session.reviewer_id),
            annotations=annotations,
        )
        packet = read_packet_json(store, revision)
        targets = _derive_targets(session, revision, units, frontier.entries, packet, annotations)
        from lemoncrow.pro.capabilities.review.targets import priorities_payload

        return {
            "undone": undone,
            "skipped": skipped,
            "progress": after["progress"],
            "outline_updates": [item for item in after["outline"] if item.get("path") in touched_paths],
            "priorities": priorities_payload(targets),
            "outline": after["outline"],
            "group_counts": after["group_counts"],
        }

    @router.post("/reviews/{review_id}/verification", status_code=status.HTTP_201_CREATED)
    async def _post_review_verification(review_id: str, request: Request) -> dict[str, Any]:
        """Append a local/CI result to the exact immutable Snapshot it ran against."""

        session = _mutable_session(review_id)
        body = await _json_object(request)
        requested_revision_id = str(body.get("revision_id") or body.get("expected_revision_id") or "").strip()
        revision = _requested_revision(session, requested_revision_id)
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="verification name is required")
        result_status = str(body.get("status") or "UNKNOWN").upper().strip()
        if result_status not in {"PASS", "FAIL", "NOT_RUN", "UNKNOWN"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="status must be PASS, FAIL, NOT_RUN, or UNKNOWN",
            )
        source = str(body.get("source") or "test").strip()
        if source not in REVIEW_EVIDENCE_SOURCES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown evidence source {source!r}",
            )
        path = str(body.get("path") or "").strip()

        def _append() -> ReviewEvidence:
            if path and not any(unit.kind == "file" and unit.path == path for unit in store.list_units(revision.id)):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="verification path is not in this Snapshot"
                )
            try:
                return store.add_evidence(
                    ReviewEvidence(
                        id="",
                        review_id=session.id,
                        revision_id=revision.id,
                        kind="verification",
                        title=name,
                        path=path,
                        source=source,  # type: ignore[arg-type]
                        source_ref=str(body.get("source_ref") or "").strip()[:500],
                        verification_status=result_status,
                        detail=str(body.get("detail") or "").strip()[:2000],
                        attestation_class="human_observation" if source == "human" else "",
                        stable_key=name if source == "human" else "",
                        attested_by=session.reviewer_id if source == "human" else "",
                        # A posted run result is caller-attested: whatever the
                        # body claims, it was executed on the author's machine.
                        executor_class="" if source == "human" else "author_machine",
                    ),
                    # If no explicit immutable Snapshot was supplied, preserve the
                    # old latest-Snapshot race guard. CI callers should always name
                    # the Snapshot they actually ran against and may finish later.
                    expected_current_revision_id=revision.id if not requested_revision_id else "",
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        def _response(record: ReviewEvidence) -> dict[str, Any]:
            return {
                "verification": _verification_projection(session, revision),
                "evidence": _review_evidence_payload(
                    record,
                    current=revision.id == _latest(session).id,
                    revision_number=_display_revision_number(revision),
                ),
            }

        # The append can wait out another writer's lock and the projection
        # re-derives every check: both off the event loop.
        record = await asyncio.to_thread(_append)
        await _enforce_armed_landing(session)
        return await asyncio.to_thread(_response, record)

    @router.post("/reviews/{review_id}/verification/waivers", status_code=status.HTTP_201_CREATED)
    async def _post_review_verification_waiver(review_id: str, request: Request) -> dict[str, Any]:
        """Record a human's reasoned override of one blocking check on the latest Snapshot.

        The waiver binds to that exact Snapshot, never changes the observed
        status and is shown as waived, not passed. Only a check that blocks
        without a current failure (a carried non-PASS, or a current NOT_RUN or
        UNKNOWN) can be waived. A typed (provider/LemonCrow/feed) check is
        named by its ``check_key``; its display title alone never identifies
        it, so a name shared by several blocking checks is refused.
        """

        from lemoncrow.pro.capabilities.review.readiness import SUPPLY_CHAIN_REQUIREMENT_KEY
        from lemoncrow.pro.capabilities.review.verification import TYPED_ROW_WAIVER_PREFIX

        session = _mutable_session(review_id)
        _refuse_hosted_owned(session, "policy satisfaction")
        body = await _json_object(request)
        revision_id = str(body.get("revision_id") or "").strip()
        name = str(body.get("name") or "").strip()
        reason = str(body.get("reason") or "").strip()
        requirement_key = str(body.get("requirement_key") or "").strip()
        row_check_key = str(body.get("check_key") or "").strip()
        if not revision_id or (not name and not requirement_key and not row_check_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="revision_id and either check name, check_key or typed requirement_key are required",
            )
        if not reason:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="a waiver needs a reason")
        if (
            requirement_key
            and requirement_key != SUPPLY_CHAIN_REQUIREMENT_KEY
            and re.fullmatch(r"check:[0-9a-f]{64}", requirement_key) is None
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="requirement_key must be a typed check requirement identity or supply-chain-policy",
            )
        revision = _requested_revision(session, revision_id)
        if revision.id != _latest(session).id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="a waiver applies only to the latest Snapshot; reload and review it first",
            )
        waived_key = requirement_key
        if requirement_key:
            # Typed requirements are core readiness conditions (operator policy
            # is the ``org_policy`` condition): find it where it is evaluated.
            readiness = await asyncio.to_thread(_readiness_projection, session)
            requirement = next(
                (
                    item
                    for item in readiness.get("conditions", ())
                    if isinstance(item, Mapping)
                    and str(item.get("requirement_key") or "") == requirement_key
                    and bool(item.get("waivable"))
                ),
                None,
            )
            if requirement is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="typed requirement is not waivable on this Snapshot",
                )
            name = str(requirement.get("stable_key") or requirement.get("label") or name or "Required check")
        else:
            projection = await asyncio.to_thread(_verification_projection, session, revision)
            candidates = [
                item
                for item in projection.get("checks", ())
                if item.get("waivable")
                and (str(item.get("check_key") or "") == row_check_key if row_check_key else item.get("name") == name)
            ]
            if not candidates:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"{name or row_check_key!r} is not a blocking check without a current failure on this Snapshot",
                )
            if len(candidates) > 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"several blocking checks are named {name!r}; name the one to waive with check_key",
                )
            row = candidates[0]
            name = str(row.get("name") or name)
            typed = str(row.get("check_key") or "")
            waived_key = f"{TYPED_ROW_WAIVER_PREFIX}{typed}" if typed else ""
        try:
            record = store.add_evidence(
                ReviewEvidence(
                    id="",
                    review_id=session.id,
                    revision_id=revision.id,
                    kind="waiver",
                    title=name,
                    source="human",
                    source_ref=session.reviewer_id[:500],
                    detail=reason[:2000],
                    attestation_class="human_waiver",
                    stable_key=name,
                    attested_by=session.reviewer_id,
                    waived_requirement_key=waived_key,
                ),
                expected_current_revision_id=revision.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        await _enforce_armed_landing(session)
        return {
            "verification": await asyncio.to_thread(_verification_projection, session, revision),
            "evidence": _review_evidence_payload(
                record,
                current=True,
                revision_number=_display_revision_number(revision),
            ),
        }

    @router.get("/reviews/{review_id}/evidence")
    def _get_review_evidence(review_id: str, revision_id: str = "") -> dict[str, Any]:
        session = _session(review_id)
        revision = _requested_revision(session, revision_id)
        revision_numbers = {item.id: _display_revision_number(item) for item in store.list_revisions(session.id)}
        return {
            "revision_id": revision.id,
            "evidence": [
                _review_evidence_payload(
                    item,
                    current=_evidence_is_current(item, revision),
                    revision_number=revision_numbers.get(item.revision_id),
                )
                for item in store.list_evidence(session.id)
            ],
        }

    @router.post("/reviews/{review_id}/evidence/upload", status_code=status.HTTP_201_CREATED)
    async def _post_review_evidence_upload(
        review_id: str,
        request: Request,
        kind: str = "screenshot",
        title: str = "",
        path: str = "",
        filename: str = "",
        mime_type: str = "",
        source: str = "human",
        verification_status: str = "",
        detail: str = "",
        expected_revision_id: str = "",
        revision_id: str = "",
        artifact_digest: str = "",
        evidence_format: str = "",
        observation_source: str = "",
        observed_at: str = "",
        source_revision: str = "",
        valid_until: str = "",
    ) -> dict[str, Any]:
        """Attach one artifact to the exact immutable Snapshot it describes.

        Supply-chain uploads may name when the observed world was in this state
        (``observed_at``), the source/database revision observed
        (``source_revision``) and an explicit ``valid_until``; an external
        observation (vulnerability scan) expires by these and by policy
        max-age, so it can never stay PASS on an unchanged Snapshot forever.
        """

        from lemoncrow.pro.capabilities.review.freshness import format_timestamp, parse_timestamp

        observed_instant = parse_timestamp(observed_at) if observed_at.strip() else None
        valid_instant = parse_timestamp(valid_until) if valid_until.strip() else None
        if (observed_at.strip() and observed_instant is None) or (valid_until.strip() and valid_instant is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="observed_at and valid_until must be ISO-8601 timestamps with a time zone",
            )

        session = _mutable_session(review_id)
        explicit_revision_id = revision_id.strip()
        requested_revision_id = explicit_revision_id or expected_revision_id.strip()
        revision = _requested_revision(session, requested_revision_id)
        if expected_revision_id and expected_revision_id != revision.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="expected_revision_id does not match the requested Snapshot",
            )
        if kind not in REVIEW_EVIDENCE_KINDS or kind in {
            "live_preview",
            "verification",
            "waiver",
            # Written only by their own paths (intent capture, pre-merge evaluation,
            # provider deployment facts -- never manual entry).
            "intent",
            "premerge_check",
            "deployment",
        }:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown upload evidence kind {kind!r}",
            )
        if source not in REVIEW_EVIDENCE_SOURCES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown evidence source {source!r}",
            )
        verification_status = verification_status.upper().strip()
        if verification_status not in {"", "PASS", "FAIL", "NOT_RUN", "UNKNOWN"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="verification_status must be PASS, FAIL, NOT_RUN, UNKNOWN, or empty",
            )
        if path:
            units = store.list_units(revision.id)
            if not any(unit.kind == "file" and unit.path == path for unit in units):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="evidence path is not in this revision Snapshot"
                )

        limit = 100 * 1024 * 1024
        raw_length = request.headers.get("content-length", "")
        if raw_length.isdigit() and int(raw_length) > limit:
            raise HTTPException(status_code=413, detail="review evidence is limited to 100 MiB per artifact")
        payload_buffer = bytearray()
        async for chunk in request.stream():
            if len(payload_buffer) + len(chunk) > limit:
                raise HTTPException(status_code=413, detail="review evidence is limited to 100 MiB per artifact")
            payload_buffer.extend(chunk)
        payload = bytes(payload_buffer)
        if not payload:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="evidence upload is empty")

        evidence_detail = detail.strip()[:1200]
        evidence_source_ref = filename.strip()
        if kind in {"sbom", "provenance", "vulnerability_scan"}:
            from lemoncrow.pro.capabilities.review.supply_chain import (
                inspect_json_evidence,
                inspect_sbom,
                validate_artifact_digest,
            )

            try:
                subject_digest = validate_artifact_digest(artifact_digest)
                metadata: dict[str, Any] = {
                    "artifact_digest": subject_digest,
                    "observation_source": observation_source.strip(),
                }
                if kind == "sbom":
                    inspection = inspect_sbom(payload)
                    metadata.update(
                        {
                            "standard": inspection.standard,
                            "format_version": inspection.version,
                            "component_count": inspection.component_count,
                        }
                    )
                else:
                    metadata.update(inspect_json_evidence(payload, kind=kind))
                    if evidence_format.strip():
                        metadata["format"] = evidence_format.strip()
                if evidence_detail:
                    metadata["note"] = evidence_detail
                if observed_instant is not None:
                    metadata["source_timestamp"] = format_timestamp(observed_instant)
                if source_revision.strip():
                    metadata["source_revision"] = source_revision.strip()[:200]
                if valid_instant is not None:
                    metadata["valid_until"] = format_timestamp(valid_instant)
                evidence_detail = json.dumps(metadata, sort_keys=True, separators=(",", ":"))[:2000]
                evidence_source_ref = observation_source.strip() or filename.strip()
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        from lemoncrow.pro.capabilities.review.store import new_evidence_id

        evidence_id = new_evidence_id()
        rel, digest, size = store.write_evidence_artifact(session.id, revision.id, evidence_id, payload)
        try:
            record = store.add_evidence(
                ReviewEvidence(
                    id=evidence_id,
                    review_id=session.id,
                    revision_id=revision.id,
                    kind=kind,  # type: ignore[arg-type]
                    title=(title or filename or kind).strip(),
                    path=path,
                    artifact_path=rel,
                    content_hash=digest,
                    mime_type=_safe_media_type(
                        mime_type or request.headers.get("content-type") or "application/octet-stream"
                    ),
                    bytes=size,
                    source=source,  # type: ignore[arg-type]
                    source_ref=evidence_source_ref,
                    verification_status=verification_status,
                    detail=evidence_detail,
                    attestation_class="human_observation" if source == "human" else "",
                    stable_key=(title or filename or kind).strip() if source == "human" else "",
                    attested_by=session.reviewer_id if source == "human" else "",
                    observed_at=format_timestamp(observed_instant) if observed_instant is not None else "",
                ),
                # Explicit immutable Snapshot uploads are append-only observations
                # and may finish after a newer Snapshot exists. The implicit/latest
                # path keeps the race guard used by interactive uploads.
                expected_current_revision_id=revision.id if not explicit_revision_id else "",
            )
        except ValueError as exc:
            store.discard_artifact(rel)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if record.verification_status:
            await _enforce_armed_landing(session)
        return {
            "evidence": _review_evidence_payload(
                record,
                current=revision.id == _latest(session).id,
                revision_number=_display_revision_number(revision),
            )
        }

    @router.post("/reviews/{review_id}/evidence/link", status_code=status.HTTP_201_CREATED)
    async def _post_review_evidence_link(review_id: str, request: Request) -> dict[str, Any]:
        """Attach a live preview URL without taking ownership of the environment."""

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="attaching this evidence")
        url = str(body.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="preview URL must be http(s)")
        path = str(body.get("path") or "").strip()
        if path and not any(unit.kind == "file" and unit.path == path for unit in store.list_units(revision.id)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="evidence path is not in this revision")
        try:
            record = store.add_evidence(
                ReviewEvidence(
                    id="",
                    review_id=session.id,
                    revision_id=revision.id,
                    kind="live_preview",
                    title=str(body.get("title") or "Live preview").strip() or "Live preview",
                    path=path,
                    url=url,
                    source="human",
                    attestation_class="human_observation",
                    stable_key=str(body.get("title") or "Live preview").strip() or "Live preview",
                    attested_by=session.reviewer_id,
                ),
                expected_current_revision_id=revision.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return {
            "evidence": _review_evidence_payload(
                record,
                current=True,
                revision_number=_display_revision_number(revision),
            )
        }

    @router.get("/evidence/{evidence_id}/content")
    def _get_review_evidence_content(evidence_id: str) -> Response:
        evidence = store.get_evidence(evidence_id)
        if evidence is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such review evidence")
        _session(evidence.review_id)
        payload = store.read_evidence_artifact(evidence)
        if payload is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="evidence artifact is unavailable")
        return _untrusted_content_response(payload, evidence.mime_type)

    @router.delete("/evidence/{evidence_id}")
    async def _delete_review_evidence(evidence_id: str) -> dict[str, Any]:
        evidence = store.get_evidence(evidence_id)
        if evidence is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such review evidence")
        session = _mutable_session(evidence.review_id)
        if evidence.verification_status or evidence.kind == "verification":
            # Verdicts are append-only observations of the Snapshot they ran
            # against. Deleting one would silently change readiness (a FAIL
            # vanishing makes a Change armable); a newer result supersedes it.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "verification results are append-only; record a newer result for this check "
                    "on the current Snapshot instead of deleting this one"
                ),
            )
        if evidence.kind == "waiver":
            # A waiver is the audit record of a human overriding a check; it may
            # already have let a Change land. It is append-only like a verdict.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="waivers are append-only audit records of a human override and cannot be deleted",
            )
        store.delete_evidence(evidence_id)
        # Any evidence change is a readiness input; an armed intent must not
        # outlive a condition it no longer meets.
        await _enforce_armed_landing(session)
        return {"deleted": evidence_id}

    @router.get("/reviews/{review_id}/proposal-selection")
    def _get_proposal_selection(
        review_id: str,
        path: str,
        start_line: int,
        end_line: int,
        side: str = "additions",
    ) -> dict[str, Any]:
        session = _session(review_id)
        revision = _latest(session)
        if side not in {"additions", "new"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="source proposals currently target only the current/new side",
            )
        try:
            confine_to_root(resolved_repo / path, resolved_repo)
        except (ValueError, OSError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file in this review") from None
        if not any(unit.kind == "file" and unit.path == path for unit in store.list_units(revision.id)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file in this review")
        base_text = revision_new_side_text(store, resolved_repo, session, revision, path)
        if base_text is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the exact reviewed source is unavailable for this proposal",
            )
        try:
            selected = selected_line_text(base_text, start_line, end_line)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        return {
            "revision_id": revision.id,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "side": "additions",
            "text": selected,
        }

    @router.get("/reviews/{review_id}/proposals")
    def _get_change_proposals(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        revision = _latest(session)
        return {
            "revision_id": revision.id,
            "proposals": [_proposal_payload(item) for item in store.list_change_proposals(session.id)],
            "source_mutation_supported": source_refresh_enabled,
        }

    @router.post("/reviews/{review_id}/proposals", status_code=status.HTTP_201_CREATED)
    async def _post_change_proposal(review_id: str, request: Request) -> dict[str, Any]:
        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="creating this change proposal")
        path = str(body.get("path") or "")
        if not path:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is required")
        side = str(body.get("side") or "additions")
        if side not in {"additions", "new"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="the first proposal flow only edits the current/new side of a text diff",
            )
        start_line = _int_field(body, ("start_line", "start"))
        end_line = _int_field(body, ("end_line", "end")) or start_line
        if start_line < 1 or end_line < start_line:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="a valid selected line range is required"
            )
        target_unit_key = str(body.get("target_unit_key") or "")
        if target_unit_key:
            unit = next((item for item in store.list_units(revision.id) if item.unit_key == target_unit_key), None)
            if unit is None or unit.path != path:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="proposal target is not a current ReviewTarget on this file",
                )
        base_text = revision_new_side_text(store, resolved_repo, session, revision, path)
        if base_text is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the exact reviewed source is unavailable for this proposal",
            )
        try:
            original_text = selected_line_text(base_text, start_line, end_line)
            replacement_text = str(body.get("replacement_text") if body.get("replacement_text") is not None else "")
            _, patch_text = unified_proposal_patch(path, base_text, start_line, end_line, replacement_text)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        if not patch_text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="proposal does not change the selected source"
            )
        try:
            proposal = store.add_change_proposal(
                ReviewChangeProposal(
                    id="",
                    review_id=session.id,
                    base_revision_id=revision.id,
                    path=path,
                    start_line=start_line,
                    end_line=end_line,
                    original_text=original_text,
                    replacement_text=replacement_text,
                    patch_text=patch_text,
                    base_file_sha256=text_sha256(base_text),
                    target_unit_key=target_unit_key,
                    annotation_id=str(body.get("annotation_id") or ""),
                    intent=str(body.get("intent") or "").strip()[:16_384],
                    created_by=session.reviewer_id,
                ),
                expected_current_revision_id=revision.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        store.record_activity(
            ReviewActivityEvent(
                id="",
                review_id=session.id,
                revision_id=revision.id,
                kind="proposal.created",
                actor_id=session.reviewer_id,
                actor_type="human",
                subject_type="change_proposal",
                subject_id=proposal.id,
                summary=f"Proposed source edit in {proposal.path}",
                detail_json=json.dumps(
                    {"path": proposal.path, "start_line": proposal.start_line, "end_line": proposal.end_line},
                    sort_keys=True,
                ),
            )
        )
        return {"proposal": _proposal_payload(proposal)}

    @router.post("/proposals/{proposal_id}/apply")
    async def _apply_change_proposal(proposal_id: str) -> dict[str, Any]:
        proposal = store.get_change_proposal(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such change proposal")
        session = _mutable_session(proposal.review_id)
        revision = _latest(session)
        if proposal.state != "proposed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"proposal is {proposal.state} and cannot be applied again",
            )
        if not source_refresh_enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this Review has no trusted mutable source bound to the server; apply the proposal in the source host instead",
            )

        def conflict(reason: str) -> None:
            updated = store.update_change_proposal(proposal.id, state="conflicted", conflict_reason=reason)
            store.record_activity(
                ReviewActivityEvent(
                    id="",
                    review_id=session.id,
                    revision_id=revision.id,
                    kind="proposal.conflicted",
                    actor_id=session.reviewer_id,
                    actor_type="human",
                    subject_type="change_proposal",
                    subject_id=proposal.id,
                    summary=f"Proposal conflicted in {proposal.path}",
                    detail_json=json.dumps({"reason": reason}, sort_keys=True),
                )
            )
            if updated is None:  # pragma: no cover - loaded immediately above
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such change proposal")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)

        if revision.id != proposal.base_revision_id:
            conflict("the Review advanced after this proposal was created; recreate it against the current revision")
        try:
            target = confine_to_root(resolved_source_repo / proposal.path, resolved_source_repo)
        except (ValueError, OSError):
            conflict("proposal path is no longer inside the trusted source checkout")
        if not target.is_file():
            conflict("proposal source file no longer exists in the trusted checkout")
        try:
            current_text = target.read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            conflict("proposal source file can no longer be read as text")
        if text_sha256(current_text) != proposal.base_file_sha256:
            conflict(
                "source changed since this proposal was prepared; inspect the current file and recreate or rebase the proposal"
            )
        try:
            if selected_line_text(current_text, proposal.start_line, proposal.end_line) != proposal.original_text:
                conflict("the selected source no longer matches the proposal base")
            next_text = replace_line_range(
                current_text,
                proposal.start_line,
                proposal.end_line,
                proposal.replacement_text,
            )
            _atomic_write_source(target, next_text, expected_sha256=proposal.base_file_sha256)
            from lemoncrow.pro.capabilities.review.gitdiff import source_state
            from lemoncrow.pro.capabilities.review.sources.local import range_for_session

            state = source_state(resolved_source_repo, range_for_session(resolved_source_repo, session))
        except HTTPException:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            conflict(f"could not apply proposal safely: {exc}")
        applied_at = utc_now()
        updated = store.update_change_proposal(
            proposal.id,
            state="applied",
            conflict_reason="",
            applied_at=applied_at,
            applied_source_fingerprint=state.fingerprint,
        )
        if updated is None:  # pragma: no cover - loaded immediately above
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such change proposal")
        store.record_activity(
            ReviewActivityEvent(
                id="",
                review_id=session.id,
                revision_id=revision.id,
                kind="proposal.applied",
                actor_id=session.reviewer_id,
                actor_type="human",
                subject_type="change_proposal",
                subject_id=proposal.id,
                summary=f"Applied source proposal in {proposal.path}",
                detail_json=json.dumps({"source_fingerprint": state.fingerprint}, sort_keys=True),
            )
        )
        return {
            "proposal": _proposal_payload(updated),
            "source_state": {
                "supported": True,
                "changed": state.fingerprint != revision.source_fingerprint,
                "fingerprint": state.fingerprint,
                "path_count": len(state.paths),
                "paths": list(state.paths),
                "reason": "",
            },
        }

    def _thread_summary_policy(session: ReviewSession, revision: ReviewRevision) -> EffectiveIntelligencePolicy:
        from lemoncrow.pro.capabilities.review.fix_agent import settings_root
        from lemoncrow.pro.capabilities.review.intelligence_policy import process_intelligence_policy

        return _route_policy(session, revision) or process_intelligence_policy(settings_root())

    def _refresh_thread_summary(session: ReviewSession, annotation_id: str) -> None:
        """Queue a BI2 refresh when a summarized thread changed; never affects the comment write."""

        if discussion_summaries is None or discussion_summaries.scheduler is None:
            return
        store_root = getattr(store, "root", None)
        if store_root is None:
            return
        from lemoncrow.pro.capabilities.review import discussion_summary as ds

        try:
            rows = ds.review_discussion(store, session.id)
            root_id = ds.thread_root_id(rows, annotation_id)
            thread = ds.find_thread(rows, root_id) if root_id else None
            # Only threads someone chose to summarize are kept current.
            if thread is None or not thread.eligible or not ds.has_summary_run(store, thread):
                return
            revision = store.latest_revision(session.id)
            if revision is None:
                return
            governance = _review_governance(session, revision)
            ds.originate_thread_summary(
                discussion_summaries.scheduler(),
                store_root=Path(store_root),
                thread=thread,
                revision_id=revision.id,
                content_access=[rule.to_wire() for rule in governance.content_access],
            )
        except Exception as exc:  # derived presentation: the comment already landed
            logging.getLogger(__name__).warning("thread summary refresh was not queued: %s", type(exc).__name__)

    @router.get("/reviews/{review_id}/thread-summaries")
    async def _get_thread_summaries(review_id: str) -> dict[str, Any]:
        """Stored thread summaries beside each thread's authoritative state. Never runs a model."""

        session = _session(review_id)
        if discussion_summaries is None:
            return {"available": False, "summaries": []}
        from lemoncrow.pro.capabilities.review.discussion_summary import (
            MIN_THREAD_MESSAGES,
            thread_summaries_view,
        )

        summaries = await asyncio.to_thread(thread_summaries_view, store, session.id)
        return {"available": True, "min_messages": MIN_THREAD_MESSAGES, "summaries": summaries}

    @router.get("/reviews/{review_id}/threads/{thread_id}/summary")
    async def _get_thread_summary(review_id: str, thread_id: str) -> dict[str, Any]:
        """One thread's stored summary, whether it is current, and its authoritative state. Never runs a model."""

        session = _session(review_id)
        if discussion_summaries is None:
            return {"available": False, "summary": None}
        from lemoncrow.pro.capabilities.review import discussion_summary as ds

        def _read() -> dict[str, Any]:
            thread = ds.find_thread(ds.review_discussion(store, session.id), thread_id)
            if thread is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such thread in this review")
            return {"available": True, "summary": ds.thread_summary_view(store, thread)}

        return await asyncio.to_thread(_read)

    @router.post("/reviews/{review_id}/threads/{thread_id}/summary")
    async def _post_thread_summary(review_id: str, thread_id: str) -> dict[str, Any]:
        """Summarize one thread now (explicit, like Ask): cached, content-access bound, Review-metered."""

        if discussion_summaries is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Discussion summaries are not available in this deployment",
            )
        from lemoncrow.pro.capabilities.review import discussion_summary as ds
        from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallBudget

        def _run() -> dict[str, Any]:
            session = _session(review_id)
            thread = ds.find_thread(ds.review_discussion(store, session.id), thread_id)
            if thread is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such thread in this review")
            revision = _latest(session)
            governance = _review_governance(session, revision)
            result = ds.summarize_thread(
                store=store,
                thread=thread,
                content_access=governance.content_access,
                content_paths=ds.thread_content_paths(store, thread, revision.id),
                budget=ModelCallBudget.from_env(max_calls=1),
                policy=_thread_summary_policy(session, revision),
            )
            return {"result": result.to_wire(), "summary": ds.thread_summary_view(store, thread)}

        return await asyncio.to_thread(_run)

    @router.get("/reviews/{review_id}/annotations")
    def _get_annotations(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        revision = _latest(session)
        comment_ingestor.sync(session)
        annotations = _ordered_annotations(
            (*store.list_annotations(session.id), *store.list_discussion_comments(session.id))
        )
        feedback_status, unpublished_ids, _ = _feedback_partition(
            revision.id,
            annotations,
            source_ref=session.source_ref,
            track_pull_request=store.get_destination(session.id).kind == "github_pr",
        )
        delivery = _feedback_capability(
            session,
            revision,
            tuple(item.id for item in annotations if item.id in unpublished_ids),
            {item.id: item for item in annotations},
        )
        return {
            "revision_id": revision.id,
            "revision_number": _display_revision_number(revision),
            "historical": False,
            "annotations": _annotation_context_payloads(session, revision, annotations),
            "counts": annotation_counts(annotations),
            "feedback": feedback_status,
            "delivery": delivery,
        }

    @router.get("/reviews/{review_id}/revisions/{revision_number}/annotations")
    def _get_historical_annotations(review_id: str, revision_number: int) -> dict[str, Any]:
        session = _session(review_id)
        revision = _revision_number(session, revision_number)
        annotations = _ordered_annotations(
            (
                *store.list_annotations_at_revision(session.id, revision.id),
                *store.list_discussion_comments_at_revision(session.id, revision.id),
            )
        )
        return {
            "revision_id": revision.id,
            "revision_number": _display_revision_number(revision),
            "historical": True,
            "annotations": _annotation_context_payloads(session, revision, annotations),
            "counts": annotation_counts(annotations),
        }

    @router.post("/reviews/{review_id}/annotations", status_code=status.HTTP_201_CREATED)
    async def _post_annotation(review_id: str, request: Request, response: Response) -> dict[str, Any]:
        """Create a durable annotation and optionally block its ReviewTarget.

        Anchor identity is always computed from the frozen revision, never from
        a browser line number. R24 additionally accepts an explicit current
        ``target_unit_key`` for a root human ``request_change``. When the client
        opts into ``mark_target``, that key is validated against the derived
        ReviewTarget projection *before* the comment is written; after the
        annotation lands, the same request records ``needs_changes`` on that
        target and returns the updated reader projection.
        """

        body = await _json_object(request)

        def _queue() -> dict[str, Any] | None:
            session = _mutable_session(review_id)
            revision = _latest(session)
            _require_expected_revision(body, revision, action="creating this comment", require_saved=False)
            return _queue_reader_write(session, "annotation", body)

        queued = await asyncio.to_thread(_queue)
        if queued is not None:
            response.status_code = status.HTTP_202_ACCEPTED
            return queued

        # Everything below is blocking SQLite/git/CPU work; on the event loop it
        # stalled every other request of this Reader (a comment took ~16s).
        def _create() -> tuple[Any, Any, Any, Any, list[dict[str, Any]], Any, Any]:
            session = _mutable_session(review_id)
            _refuse_hosted_owned(session, "comments")
            revision = _latest(session)
            _require_expected_revision(body, revision, action="creating this comment")
            nested = body.get("range")
            selection: dict[str, Any] = nested if isinstance(nested, dict) else body
            path = str(body.get("path") or "")
            if not path:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is required")
            text = str(body.get("body") or "")
            if not text.strip():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="a comment needs something to say")
            file_level = bool(body.get("file_level"))
            start = 0 if file_level else _int_field(selection, ("start_line", "start"))
            end = 0 if file_level else (_int_field(selection, ("end_line", "end")) or start)
            if not file_level and start < 1:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="start line is required")
            kind = str(body.get("kind") or "comment")
            parent_id = str(body.get("parent_id") or "")
            mark_target = bool(body.get("mark_target"))
            target_key = str(body.get("target_unit_key") or "")
            target_unit: ReviewUnit | None = None
            if mark_target and (kind != "request_change" or parent_id):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="only a root request_change may mark a review target",
                )
            if mark_target and not target_key:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="target_unit_key is required when mark_target is true",
                )
            if target_key:
                units = store.list_units(revision.id)
                existing_annotations = store.list_annotations(session.id)
                marks = store.list_marks(session.id, reviewer_id=session.reviewer_id)
                packet = read_packet_json(store, revision)
                frontier = compute_frontier(
                    session.id,
                    session.reviewer_id,
                    revision,
                    units,
                    marks,
                    annotations=existing_annotations,
                )
                current_targets = _derive_targets(
                    session,
                    revision,
                    units,
                    frontier.entries,
                    packet,
                    existing_annotations,
                )
                requested_target = next((item for item in current_targets if item.unit_key == target_key), None)
                if requested_target is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="target_unit_key is not a current ReviewTarget",
                    )
                if requested_target.path != path:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="annotation target belongs to a different file",
                    )
                target_unit = next((unit for unit in units if unit.unit_key == target_key), None)
                if target_unit is None:  # every ReviewTarget is backed by a ReviewUnit
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT, detail="review target unit is unavailable"
                    )

            try:
                with store.atomic_review_transition():
                    store.require_current_revision(session.id, revision.id)
                    annotation = annotate(
                        store,
                        session,
                        revision,
                        path=path,
                        start_line=start,
                        end_line=end,
                        body=text,
                        kind=kind,
                        side=str(selection.get("side") or body.get("side") or "new"),
                        parent_id=parent_id,
                        created_by=session.reviewer_id or "local",
                        created_by_actor="human",
                        source="human",
                        source_id=session.reviewer_id or "local",
                        new_text=revision_new_side_text(store, resolved_repo, session, revision, path),
                        expected_current_revision_id=revision.id,
                    )
                    if target_unit is not None and mark_target:
                        mark_unit(
                            store,
                            session,
                            revision,
                            target_unit,
                            state="needs_changes",
                            reviewer_id=session.reviewer_id,
                            expected_current_revision_id=revision.id,
                        )
            except ValueError as exc:
                message = str(exc)
                code = (
                    status.HTTP_409_CONFLICT if "Review advanced" in message else status.HTTP_422_UNPROCESSABLE_ENTITY
                )
                raise HTTPException(status_code=code, detail=message) from exc

            marked_target = None
            progress = None
            outline_updates: list[dict[str, Any]] = []
            priorities: dict[str, dict[str, Any]] | None = None
            outline: list[dict[str, Any]] | None = None
            if target_unit is not None:
                overview = _overview(session)
                progress = overview["progress"]
                outline_updates = [item for item in overview["outline"] if item.get("path") == target_unit.path]

                from lemoncrow.pro.capabilities.review.targets import priorities_payload, target_payload

                latest_annotations = store.list_annotations(session.id)
                latest_marks = store.list_marks(session.id, reviewer_id=session.reviewer_id)
                units = store.list_units(revision.id)
                packet = read_packet_json(store, revision)
                frontier = compute_frontier(
                    session.id,
                    session.reviewer_id,
                    revision,
                    units,
                    latest_marks,
                    annotations=latest_annotations,
                )
                targets = _derive_targets(session, revision, units, frontier.entries, packet, latest_annotations)
                changed_target = next((item for item in targets if item.unit_key == target_unit.unit_key), None)
                marked_target = target_payload(changed_target) if changed_target is not None else None
                priorities = priorities_payload(targets)
                outline = overview["outline"]

            return session, annotation, marked_target, progress, outline_updates, priorities, outline

        session, annotation, marked_target, progress, outline_updates, priorities, outline = await asyncio.to_thread(
            _create
        )
        if annotation.parent_id:
            await asyncio.to_thread(_refresh_thread_summary, session, annotation.id)
        await _enforce_armed_landing(session)
        return {
            "annotation": _annotation_payload(annotation),
            "target": marked_target,
            "progress": progress,
            "outline_updates": outline_updates,
            "priorities": priorities,
            "outline": outline,
        }

    @router.patch("/annotations/{annotation_id}")
    async def _patch_annotation(annotation_id: str, request: Request) -> dict[str, Any]:
        """Edit a comment's text, disposition or state -- never its anchor.

        The anchor is owned by the relocation ladder. A route that let a caller
        set line numbers directly would hand the browser back the authority spec
        SS5.4 takes away from it.
        """

        annotation = store.get_annotation(annotation_id)
        if annotation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such comment")
        # Load the session through the same gate every other route uses, so a
        # comment belonging to another checkout's review is a 404 here too.
        session = _mutable_session(annotation.review_id)
        _refuse_hosted_owned(session, "comments")
        revision = _latest(session)

        body = await _json_object(request)
        if "state" in body or "finding_disposition" in body:
            _require_expected_revision(body, revision, action="changing this comment state")
        _require_saved_snapshot(revision)
        fields: dict[str, object] = {}
        if "body" in body:
            text = str(body.get("body") or "")
            if not text.strip():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="a comment needs something to say")
            fields["body"] = text
        if "kind" in body:
            try:
                fields["kind"] = coerce_annotation_kind(str(body.get("kind") or ""))
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        if "state" in body:
            state = str(body.get("state") or "")
            if state not in ANNOTATION_STATES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"unknown comment state {state!r}; expected one of {', '.join(ANNOTATION_STATES)}",
                )
            fields["state"] = state
            if state == "resolved":
                fields["resolved_revision_id"] = revision.id
            elif state == "open":
                fields["resolved_revision_id"] = ""
        if "finding_disposition" in body:
            if annotation.source != "ai_review":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="finding disposition only applies to AI Review findings",
                )
            finding_disposition = str(body.get("finding_disposition") or "")
            if finding_disposition not in FINDING_DISPOSITIONS or not finding_disposition:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"unknown AI finding disposition {finding_disposition!r}; expected one of "
                        + ", ".join(value for value in FINDING_DISPOSITIONS if value)
                    ),
                )
            dismissal_reason = str(body.get("finding_disposition_reason") or "")
            if dismissal_reason not in FINDING_DISMISSAL_REASONS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="unknown AI finding dismissal reason",
                )
            if finding_disposition == "dismissed" and not dismissal_reason:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="dismissed AI findings require a dismissal reason",
                )
            if finding_disposition != "dismissed" and dismissal_reason:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="dismissal reason only applies to dismissed AI findings",
                )
            raw_evidence = body.get("finding_disposition_evidence") or []
            if not isinstance(raw_evidence, list) or any(not isinstance(item, str) for item in raw_evidence):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="finding disposition evidence must be a list of evidence ids",
                )
            evidence_ids = tuple(dict.fromkeys(item for item in raw_evidence if item))
            if finding_disposition == "fixed":
                current_evidence = {
                    item.id for item in store.list_evidence(session.id) if item.revision_id == revision.id
                }
                if not evidence_ids or any(item not in current_evidence for item in evidence_ids):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="fixed AI findings require evidence recorded on the current Snapshot",
                    )
            elif evidence_ids:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="revalidation evidence is only recorded for fixed AI findings",
                )
            fields["finding_disposition"] = finding_disposition
            fields["finding_disposition_reason"] = dismissal_reason
            fields["finding_disposition_revision_id"] = revision.id
            fields["finding_disposition_reviewer_id"] = session.reviewer_id
            fields["finding_disposition_evidence_json"] = json.dumps(list(evidence_ids), ensure_ascii=False)
        if not fields:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="nothing to change")

        request_changed = ("body" in fields and fields["body"] != annotation.body) or (
            "kind" in fields and fields["kind"] != annotation.kind
        )
        # P12-AUTO: a provider-published comment changes only through provider sync.
        automation_hooks.guard_published_edit(annotation, fields)
        if request_changed and annotation.author_response != "none":
            # An author response belongs to the request version the author saw.
            # Editing the request cannot carry that claim forward.
            fields["author_response"] = "none"
            fields["author_response_source_id"] = ""
            fields["author_response_at"] = ""

        from lemoncrow.pro.capabilities.review.store import AnnotationHistoryContext

        try:
            if "state" in fields or "finding_disposition" in fields:
                with store.atomic_review_transition():
                    store.require_current_revision(session.id, revision.id)
                    updated = store.update_annotation(
                        annotation_id,
                        AnnotationHistoryContext(
                            revision_id=revision.id,
                            changed_by=session.reviewer_id,
                            changed_by_actor="human",
                            change_kind=("finding_disposition" if "finding_disposition" in fields else None),
                        ),
                        **fields,
                    )
            else:
                updated = store.update_annotation(
                    annotation_id,
                    AnnotationHistoryContext(
                        revision_id=revision.id,
                        changed_by=session.reviewer_id,
                        changed_by_actor="human",
                    ),
                    **fields,
                )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if updated is None:  # pragma: no cover - the row was loaded a moment ago
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such comment")
        if request_changed:
            # What was said changed (state changes never do): a summarized thread refreshes.
            await asyncio.to_thread(_refresh_thread_summary, session, updated.id)
        await _enforce_armed_landing(session)
        return {"annotation": _annotation_payload(updated)}

    def _feedback_snapshot(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        packet = read_packet_json(store, revision)
        annotations = store.list_annotations(session.id)
        feedback_title = session.title or (str(packet.get("title") or "") if packet is not None else "")
        full_bundle = build_bundle(
            session,
            revision,
            annotations,
            store.list_units(revision.id),
            context=_feedback_context(packet),
            title=feedback_title,
        )
        # The reviewer's "what still needs to change" on this Snapshot is
        # written for whoever fixes it, so it travels with the comments.
        outcome = store.latest_outcome(session.id, session.reviewer_id)
        if (
            outcome is not None
            and outcome.revision_id == revision.id
            and outcome.outcome == "changes_requested"
            and outcome.summary.strip()
        ):
            full_bundle = replace(full_bundle, request_summary=outcome.summary.strip())
        feedback_status, unpublished_ids, versions = _feedback_partition(
            revision.id,
            annotations,
            source_ref=session.source_ref,
            track_pull_request=store.get_destination(session.id).kind == "github_pr",
        )
        bundle = replace(
            full_bundle,
            items=tuple(item for item in full_bundle.items if item.annotation_id in unpublished_ids),
            orphaned=tuple(item for item in full_bundle.orphaned if item.annotation_id in unpublished_ids),
            resolved=(),
        )
        markdown = render_markdown(bundle)
        annotation_ids = tuple(item.annotation_id for item in (*bundle.items, *bundle.orphaned))
        selected_versions = {annotation_id: versions.get(annotation_id, 0) for annotation_id in annotation_ids}
        digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        return {
            "bundle": bundle,
            "full_bundle": full_bundle,
            "markdown": markdown,
            "annotation_ids": annotation_ids,
            "annotations_by_id": {item.id: item for item in annotations},
            "annotation_versions": selected_versions,
            "feedback_hash": digest,
            "feedback_status": feedback_status,
            "resolved_total": len(full_bundle.resolved),
        }

    def _delivery_routes(
        revision: ReviewRevision,
        annotation_ids: Sequence[str],
        annotations_by_id: Mapping[str, Annotation],
    ) -> tuple[dict[str, dict[str, str]], tuple[str, ...]]:
        """Resolve each comment to one exact responsible session or fail honestly."""

        from lemoncrow.pro.capabilities.review.delivery import direct_delivery_supported

        attributions = store.list_revision_attributions(revision.id)
        exact_file_attributions = tuple(
            item
            for item in attributions
            if item.scope == "file" and item.certainty == "exact" and item.host and item.session_id
        )
        revision_candidates = {
            (item.host, item.session_id, item.model)
            for item in attributions
            if item.scope == "revision"
            and item.role == "primary"
            and item.certainty == "exact"
            and item.host
            and item.session_id
        }
        if not revision_candidates and (
            revision.provenance_certainty == "exact" and revision.provenance_host and revision.provenance_session_id
        ):
            revision_candidates.add(
                (revision.provenance_host, revision.provenance_session_id, revision.provenance_model)
            )

        routes: dict[str, dict[str, str]] = {}
        failures: list[str] = []
        for annotation_id in annotation_ids:
            annotation = annotations_by_id.get(annotation_id)
            if annotation is None:
                failures.append(f"{annotation_id}: annotation is unavailable")
                continue
            path = annotation.anchor.path
            scoped = {
                (item.host, item.session_id, item.model)
                for item in attributions
                if item.scope == "file"
                and item.scope_key == path
                and item.certainty == "exact"
                and item.host
                and item.session_id
            }
            if len(scoped) > 1:
                failures.append(f"{annotation_id}: {path} has mixed exact authorship")
                continue
            if exact_file_attributions:
                # Once this revision has file-scoped evidence, absence is
                # meaningful: the primary revision author is not proof of who
                # authored a different file. Never route that gap by fallback.
                candidates = scoped
            else:
                # Legacy revisions created before file attribution existed may
                # still use the exact revision-primary compatibility projection.
                candidates = revision_candidates
            if len(candidates) != 1:
                failures.append(f"{annotation_id}: exact responsible session is unavailable for {path}")
                continue
            host, session_id, model = next(iter(candidates))
            host = host.strip().lower()
            session_id = session_id.strip()
            if not direct_delivery_supported(host):
                failures.append(f"{annotation_id}: direct delivery is not configured for {host}")
                continue
            routes[annotation_id] = {
                "host": host,
                "session_id": session_id,
                "model": model,
                "target_ref": f"{host}:{session_id}",
                "scope": "file" if scoped else "revision",
                "path": path,
            }
        return routes, tuple(failures)

    def _delivery_capability(
        revision: ReviewRevision,
        *,
        path: str = "",
        annotation_ids: Sequence[str] = (),
        annotations_by_id: Mapping[str, Annotation] | None = None,
    ) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.delivery import direct_delivery_supported

        if annotation_ids and annotations_by_id is not None:
            routes, failures = _delivery_routes(revision, annotation_ids, annotations_by_id)
            targets = tuple(sorted({item["target_ref"] for item in routes.values()}))
            supported = len(routes) == len(annotation_ids) and not failures
            first = routes.get(annotation_ids[0]) if len(targets) == 1 and annotation_ids else None
            return {
                "supported": supported,
                "host": first["host"] if first is not None else "",
                "session_id": first["session_id"] if first is not None else "",
                "target_ref": targets[0] if len(targets) == 1 else "",
                "targets": list(targets),
                "routes": routes,
                "label": (first["host"].capitalize() if first is not None else f"{len(targets)} author sessions"),
                "reason": "; ".join(failures),
            }

        attributions = store.list_revision_attributions(revision.id)
        exact_file = tuple(
            item
            for item in attributions
            if item.scope == "file" and item.certainty == "exact" and item.host and item.session_id
        )
        revision_candidates = {
            (item.host.strip().lower(), item.session_id.strip(), item.model)
            for item in attributions
            if item.scope == "revision"
            and item.role == "primary"
            and item.certainty == "exact"
            and item.host
            and item.session_id
        }
        if (
            not revision_candidates
            and revision.provenance_certainty == "exact"
            and revision.provenance_host
            and revision.provenance_session_id
        ):
            revision_candidates.add(
                (
                    revision.provenance_host.strip().lower(),
                    revision.provenance_session_id.strip(),
                    revision.provenance_model,
                )
            )

        if path:
            scoped = {
                (item.host.strip().lower(), item.session_id.strip(), item.model)
                for item in exact_file
                if item.scope_key == path
            }
            if len(scoped) > 1:
                return {
                    "supported": False,
                    "host": "",
                    "session_id": "",
                    "target_ref": "",
                    "label": "",
                    "reason": f"{path} has mixed exact authorship",
                }
            candidates = scoped if exact_file else revision_candidates
        else:
            candidates = revision_candidates

        if len(candidates) != 1:
            reason = (
                f"exact responsible author session is unavailable for {path}"
                if path
                else "exact primary author session is unavailable"
            )
            return {
                "supported": False,
                "host": "",
                "session_id": "",
                "target_ref": "",
                "label": "",
                "reason": reason,
            }

        host, session_id, _model = next(iter(candidates))
        supported = direct_delivery_supported(host)
        return {
            "supported": supported,
            "host": host if supported else "",
            "session_id": session_id if supported else "",
            "target_ref": f"{host}:{session_id}" if supported else "",
            "label": host.capitalize() if supported else "",
            "reason": "" if supported else f"direct feedback delivery is not configured for {host}",
        }

    # Agent sessions live beside the author's checkout. A server without one
    # (hosted, a pull-request mirror) must never start, resume or look for them.
    no_local_checkout = "" if source_refresh_enabled else "agent sessions are reachable only from a local checkout"

    def _host_label(host: str) -> str:
        from lemoncrow.pro.capabilities.review.handoff import host_label

        return host_label(host)

    def _session_label(host: str, session_id: str) -> str:
        return f"{_host_label(host)} {session_id[:4]}".strip()

    def _destination_pull_request(session: ReviewSession, destination: ReviewDestination | None = None) -> Any:
        """The pull request feedback can be published to: the Review's own, else a pinned one."""

        from lemoncrow.pro.capabilities.review.sources.github import parse_pr_source_ref

        pr = parse_pr_source_ref(session.source_ref)
        if pr is not None:
            return pr
        pinned = destination or store.get_destination(session.id)
        return parse_pr_source_ref(pinned.external_ref) if pinned.kind == "github_pr" else None

    def _export_capability(
        annotation_ids: Sequence[str], *, reason: str = "", fallback: bool = False
    ) -> dict[str, Any]:
        target_ref = "clipboard:author"
        route = {"host": "clipboard", "session_id": "author", "target_ref": target_ref, "kind": "export"}
        return {
            "kind": "export",
            "destination": "auto" if fallback else "export",
            "fallback": fallback,
            "supported": True,
            "host": "",
            "session_id": "",
            "target_ref": target_ref,
            "targets": [target_ref],
            "routes": {annotation_id: dict(route) for annotation_id in annotation_ids},
            "label": "author",
            "reason": reason,
        }

    def _feedback_capability(
        session: ReviewSession,
        revision: ReviewRevision,
        annotation_ids: Sequence[str],
        annotations_by_id: Mapping[str, Annotation],
    ) -> dict[str, Any]:
        """Where this Review's feedback goes, honoring the reviewer's pinned destination.

        ``kind`` is how the Reader sends it: ``agent`` (resume or inbox of an
        exact session), ``new_session`` (start one), ``export`` (the reviewer
        copies it for a human; recorded only on explicit confirmation) or
        ``github`` (the preview-bound pull request publish). Exact-author
        routing with no provable author session falls back to ``export`` and
        says why; it never falls through to a different agent.
        """

        from lemoncrow.pro.capabilities.review.delivery import (
            direct_delivery_supported,
            new_session_id,
            new_session_supported,
        )

        destination = store.get_destination(session.id)
        ids = tuple(annotation_ids)
        common: dict[str, Any] = {
            "destination": destination.kind,
            "fallback": False,
            "host": "",
            "session_id": "",
            "target_ref": "",
            "targets": [],
            "routes": {},
            "reason": "",
        }

        def github(pr: Any, *, fallback: bool = False) -> dict[str, Any]:
            reason = (
                "the chosen pull request is not recognised"
                if pr is None
                else (
                    ""
                    if github_publish_enabled
                    else "publishing to GitHub runs only on your local LemonCrow server, where your `gh` login lives"
                )
            )
            return {
                **common,
                "kind": "github",
                "fallback": fallback,
                "supported": not reason,
                "target_ref": pr.source_ref if pr is not None else "",
                "label": f"PR #{pr.number}" if pr is not None else "pull request",
                "reason": reason,
            }

        if destination.kind == "auto":
            capability = (
                _delivery_capability(revision, annotation_ids=ids, annotations_by_id=annotations_by_id)
                if ids
                else _delivery_capability(revision)
            )
            if no_local_checkout and capability["supported"]:
                capability = {**capability, "supported": False, "reason": no_local_checkout}
            if not capability["supported"]:
                # A PR-opened Review with no reachable author session answers on
                # its pull request, as it always has; anything else is copied.
                own_pr = _destination_pull_request(session, destination)
                if own_pr is not None:
                    return github(own_pr, fallback=True)
                return _export_capability(ids, reason=str(capability.get("reason") or ""), fallback=True)
            routes = {key: {**value, "kind": "agent"} for key, value in dict(capability.get("routes") or {}).items()}
            return {**capability, "routes": routes, "kind": "agent", "destination": "auto", "fallback": False}
        if destination.kind == "export":
            return _export_capability(ids)
        if destination.kind == "github_pr":
            return github(_destination_pull_request(session, destination))
        host = destination.host
        if destination.kind == "new_session":
            supported, reason = (False, no_local_checkout) if no_local_checkout else new_session_supported(host)
            new_id = new_session_id(host, "\0".join((session.id, revision.id, *ids)))
            target_ref = f"{host}:{new_id}"
            route = {"host": host, "session_id": new_id, "target_ref": target_ref, "kind": "new_session"}
            return {
                **common,
                "kind": "new_session",
                "supported": supported,
                "host": host,
                "session_id": new_id,
                "target_ref": target_ref,
                "targets": [target_ref],
                "routes": {annotation_id: dict(route) for annotation_id in ids},
                "label": f"new {_host_label(host)} session",
                "reason": reason,
            }
        pinned_id = destination.session_id
        supported = direct_delivery_supported(host) and bool(pinned_id) and not no_local_checkout
        target_ref = f"{host}:{pinned_id}"
        route = {"host": host, "session_id": pinned_id, "target_ref": target_ref, "kind": "agent"}
        return {
            **common,
            "kind": "agent",
            "supported": supported,
            "host": host if supported else "",
            "session_id": pinned_id if supported else "",
            "target_ref": target_ref if supported else "",
            "targets": [target_ref] if supported else [],
            "routes": {annotation_id: dict(route) for annotation_id in ids} if supported else {},
            "label": _session_label(host, pinned_id),
            "reason": (
                "" if supported else no_local_checkout or f"direct feedback delivery is not configured for {host}"
            ),
        }

    def _destination_payload(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        destination = store.get_destination(session.id)
        effective = _feedback_capability(session, revision, (), {})
        return {
            "review_id": session.id,
            "destination": {
                "kind": destination.kind,
                "host": destination.host,
                "session_id": destination.session_id,
                "external_ref": destination.external_ref,
                "updated_at": destination.updated_at,
            },
            "effective": {
                "kind": effective["kind"],
                "label": effective.get("label") or "",
                "host": effective.get("host") or "",
                "session_id": effective.get("session_id") or "",
                "available": bool(effective["supported"]),
                "fallback": bool(effective.get("fallback")),
                "reason": effective.get("reason") or "",
            },
        }

    def _destination_options(session: ReviewSession, revision: ReviewRevision) -> list[dict[str, Any]]:
        """Everything the reviewer can send this Review's feedback to, and why not.

        Blocking (host CLIs, ``gh``): run off the event loop.
        """

        from lemoncrow.pro.capabilities.review.delivery import (
            NEW_SESSION_HOSTS,
            direct_delivery_supported,
            new_session_supported,
            running_claude_sessions,
            workspace_agent_session,
        )
        from lemoncrow.pro.capabilities.review.sources.github import (
            GitHubCliError,
            branch_pull_request,
            parse_pr_source_ref,
        )

        auto = _delivery_capability(revision)
        if no_local_checkout and auto["supported"]:
            auto = {**auto, "supported": False, "reason": no_local_checkout}
        own_pr = parse_pr_source_ref(session.source_ref)
        options: list[dict[str, Any]] = [
            {
                "kind": "auto",
                "label": "Author sessions",
                "detail": (
                    f"{_host_label(str(auto['host']))} · wrote this Snapshot"
                    if auto["supported"]
                    else (
                        f"No exact author session ({auto['reason']}); publishes to PR #{own_pr.number} instead"
                        if own_pr is not None
                        else f"No exact author session ({auto['reason']}); copies for the author instead"
                    )
                ),
                "available": True,
                "reason": "",
            }
        ]
        sessions: dict[tuple[str, str], str] = {}
        activity_times: dict[tuple[str, str], str] = {}
        # lc-debt: one attribution read per Snapshot of the Change; add a
        # per-Review attribution index if Changes grow to hundreds of Snapshots.
        for snapshot in sorted(store.list_revisions(session.id), key=_display_revision_number, reverse=True):
            for item in store.list_revision_attributions(snapshot.id):
                host = item.host.strip().lower()
                if item.certainty != "exact" or not host or not item.session_id.strip():
                    continue
                sessions.setdefault(
                    (host, item.session_id.strip()), f"wrote Snapshot {_display_revision_number(snapshot)}"
                )
                key = (host, item.session_id.strip())
                recorded_at = item.created_at or snapshot.created_at
                if recorded_at:
                    activity_times[key] = max(activity_times.get(key, ""), recorded_at)
        live = workspace_agent_session(resolved_source_repo) if not no_local_checkout else None
        if live is not None:
            host, live_id, activity = live
            relation = "live in this worktree" if activity in {"active", "working"} else "last active in this worktree"
            sessions[(host, live_id)] = (
                f"{sessions[(host, live_id)]} · {relation}" if (host, live_id) in sessions else relation
            )
            from lemoncrow.pro.capabilities.review.delivery import workspace_session_activity_at

            activity_at = workspace_session_activity_at(resolved_source_repo, host, live_id)
            if activity_at:
                activity_times[(host, live_id)] = activity_at
        for running_id, _state in running_claude_sessions(resolved_source_repo) if not no_local_checkout else ():
            key = ("claude", running_id)
            sessions[key] = f"{sessions[key]} · running here" if key in sessions else "running here"
        for (host, known_id), relation in sessions.items():
            supported = direct_delivery_supported(host) and not no_local_checkout
            options.append(
                {
                    "kind": "session",
                    "host": host,
                    "session_id": known_id,
                    "label": _session_label(host, known_id),
                    "detail": relation,
                    "activity_at": activity_times.get((host, known_id), ""),
                    "available": supported,
                    "reason": (
                        ""
                        if supported
                        else no_local_checkout or f"direct feedback delivery is not configured for {host}"
                    ),
                }
            )
        for host in NEW_SESSION_HOSTS:
            supported, reason = (False, no_local_checkout) if no_local_checkout else new_session_supported(host)
            options.append(
                {
                    "kind": "new_session",
                    "host": host,
                    "label": f"New {_host_label(host)} session",
                    "detail": "starts in this worktree with the comments",
                    "available": supported,
                    "reason": reason,
                }
            )
        pr = own_pr
        pr_reason = ""
        # `gh` runs only where the reviewer's own login lives.
        if pr is None and github_publish_enabled:
            try:
                pr = branch_pull_request(resolved_source_repo)
            except (GitHubCliError, OSError, ValueError) as exc:
                pr_reason = str(exc)
        if pr is not None:
            options.append(
                {
                    "kind": "github_pr",
                    "external_ref": pr.source_ref,
                    "label": f"PR #{pr.number}",
                    "detail": f"{pr.slug} · publish as a GitHub review",
                    "available": github_publish_enabled,
                    "reason": (
                        ""
                        if github_publish_enabled
                        else "publishing to GitHub runs only on your local LemonCrow server, where your `gh` login lives"
                    ),
                }
            )
        elif pr_reason:
            options.append(
                {
                    "kind": "github_pr",
                    "external_ref": "",
                    "label": "Pull request",
                    "detail": "",
                    "available": False,
                    "reason": pr_reason,
                }
            )
        options.append(
            {
                "kind": "export",
                "label": "Copy for the author",
                "detail": "Markdown for a human or any other tool",
                "available": True,
                "reason": "",
            }
        )
        return options

    @router.get("/reviews/{review_id}/destination")
    def _get_destination(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        return _destination_payload(session, _latest(session))

    @router.get("/reviews/{review_id}/destination/options")
    async def _get_destination_options(review_id: str) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        session = _session(review_id)
        revision = _latest(session)
        options = await run_in_threadpool(_destination_options, session, revision)
        return {**_destination_payload(session, revision), "options": options}

    @router.put("/reviews/{review_id}/destination")
    async def _put_destination(review_id: str, request: Request) -> dict[str, Any]:
        """Pin where this Review's feedback goes; ``auto`` returns to exact-author routing."""

        from lemoncrow.pro.capabilities.review.delivery import DIRECT_DELIVERY_HOSTS, NEW_SESSION_HOSTS
        from lemoncrow.pro.capabilities.review.session_models import REVIEW_DESTINATION_KINDS
        from lemoncrow.pro.capabilities.review.sources.github import parse_pr_source_ref

        session = _mutable_session(review_id)
        body = await _json_object(request)
        kind = str(body.get("kind") or "").strip()
        host = str(body.get("host") or "").strip().lower()
        pinned_id = str(body.get("session_id") or "").strip()
        external_ref = str(body.get("external_ref") or "").strip()
        problem = ""
        if kind not in REVIEW_DESTINATION_KINDS:
            problem = f"kind must be one of {', '.join(REVIEW_DESTINATION_KINDS)}"
        elif kind == "session" and (host not in DIRECT_DELIVERY_HOSTS or not pinned_id):
            problem = f"a session destination needs a host ({', '.join(DIRECT_DELIVERY_HOSTS)}) and a session_id"
        elif kind == "new_session" and host not in NEW_SESSION_HOSTS:
            problem = f"a new session can be started for {', '.join(NEW_SESSION_HOSTS)}"
        elif kind == "github_pr" and parse_pr_source_ref(external_ref) is None:
            problem = "a pull request destination needs external_ref github:OWNER/REPO#NUMBER"
        if problem:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problem)
        try:
            store.set_destination(
                ReviewDestination(
                    review_id=session.id,
                    kind=kind,  # type: ignore[arg-type]
                    host=host if kind in {"session", "new_session"} else "",
                    session_id=pinned_id if kind == "session" else "",
                    external_ref=external_ref if kind == "github_pr" else "",
                    set_by=session.reviewer_id,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return _destination_payload(session, _latest(session))

    def _feedback_operation_id(
        session: ReviewSession,
        revision: ReviewRevision,
        snapshot: Mapping[str, Any],
        delivery: Mapping[str, Any],
    ) -> str:
        route_identity = json.dumps(
            sorted(
                (annotation_id, str(delivery.get("routes", {}).get(annotation_id, {}).get("target_ref") or ""))
                for annotation_id in snapshot["annotation_ids"]
            ),
            separators=(",", ":"),
        )
        operation_identity = "\0".join(
            (
                session.id,
                revision.id,
                str(snapshot["feedback_hash"]),
                route_identity,
            )
        )
        return "fop-" + hashlib.sha256(operation_identity.encode("utf-8")).hexdigest()[:32]

    @router.post("/reviews/{review_id}/feedback/export")
    async def _post_feedback_export(review_id: str) -> dict[str, Any]:
        """Prepare an immutable feedback preview. Rendering never delivers."""

        session = _session(review_id)
        revision = _latest(session)
        snapshot = _feedback_snapshot(session, revision)
        bundle = snapshot["bundle"]
        delivery = _feedback_capability(
            session,
            revision,
            snapshot["annotation_ids"],
            snapshot["annotations_by_id"],
        )
        operation_id = _feedback_operation_id(session, revision, snapshot, delivery)
        return {
            "markdown": snapshot["markdown"],
            "open": len(bundle.items),
            "orphaned": len(bundle.orphaned),
            "resolved": snapshot["resolved_total"],
            "revision_id": revision.id,
            "feedback_hash": snapshot["feedback_hash"],
            "operation_id": operation_id,
            "annotation_versions": snapshot["annotation_versions"],
            "delivery": delivery,
            "status": snapshot["feedback_status"],
        }

    async def _deliver_feedback(
        review_id: str,
        request: Request,
        *,
        required_host: str = "",
        allow_implicit_preview: bool = False,
    ) -> dict[str, Any]:
        """Deliver exactly one immutable feedback snapshot, once per operation id.

        The general delivery route is preview-bound: the caller must send the
        operation/revision/hash it reviewed. The legacy Claude route may omit a
        body; in that compatibility case LemonCrow snapshots the current
        unpublished bundle itself and derives a deterministic operation id so a
        network retry still cannot dispatch the same correction twice.

        A copy-for-the-author handoff is recorded only when the caller confirms
        the copy (``copied: true``): nothing else can prove a human got it.
        """

        from lemoncrow.pro.capabilities.review.delivery import (
            PENDING_SESSION_ID,
            AgentDeliveryResult,
            deliver_to_agent_session,
            launch_agent_session,
        )
        from lemoncrow.pro.capabilities.review.session_models import DeliveryRecord

        session = _mutable_session(review_id)
        revision = _latest(session)
        raw_body = await request.body()
        body = await _json_object(request) if raw_body.strip() else {}
        operation_id = str(body.get("operation_id") or "").strip()
        expected_revision_id = str(body.get("expected_revision_id") or "").strip()
        expected_feedback_hash = str(body.get("expected_feedback_hash") or "").strip()
        copied = body.get("copied") is True
        implicit_operation = False
        if not operation_id or not expected_revision_id or not expected_feedback_hash:
            if allow_implicit_preview and not body:
                implicit_operation = True
                implicit = _feedback_snapshot(session, revision)
                expected_revision_id = revision.id
                expected_feedback_hash = str(implicit["feedback_hash"])
                operation_id = (
                    "fop-compat-"
                    + hashlib.sha256(
                        f"{session.id}\0{expected_revision_id}\0{expected_feedback_hash}".encode()
                    ).hexdigest()[:32]
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "delivery requires operation_id, expected_revision_id, and "
                        "expected_feedback_hash from a prepared preview"
                    ),
                )

        existing = store.list_delivery_operation(operation_id)
        if existing:
            existing_annotations = [store.get_annotation(item.annotation_id) for item in existing]
            if (
                any(item is None or item.review_id != session.id for item in existing_annotations)
                or any(item.revision_id != expected_revision_id for item in existing)
                or any(item.feedback_hash != expected_feedback_hash for item in existing)
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="delivery operation identity does not match this prepared feedback",
                )

        if revision.id != expected_revision_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="review revision changed since feedback preview; prepare the updated feedback before sending",
            )
        if required_host:
            legacy_capability = _delivery_capability(revision)
            if not legacy_capability["supported"]:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=legacy_capability["reason"])
            if legacy_capability["host"] != required_host:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"this compatibility route requires exact {required_host} provenance",
                )

        snapshot = _feedback_snapshot(session, revision)
        destination = store.get_destination(session.id)

        routes: dict[str, dict[str, str]] = {}
        ids_by_target: dict[str, list[str]] = {}
        if existing:
            operation = existing
            annotation_ids = tuple(dict.fromkeys(item.annotation_id for item in existing))
            for item in existing:
                host, separator, target_session = item.target_ref.partition(":")
                if not separator or not host or not target_session:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="stored delivery target is malformed",
                    )
                if item.target_type == "clipboard":
                    route_kind = "export"
                elif destination.kind == "new_session" and host == destination.host:
                    # A launch that provably never left keeps its operation;
                    # retrying it starts the session, it never resumes one.
                    route_kind = "new_session"
                else:
                    route_kind = "agent"
                routes[item.annotation_id] = {
                    "host": host,
                    "session_id": target_session,
                    "target_ref": item.target_ref,
                    "kind": route_kind,
                }
                ids_by_target.setdefault(item.target_ref, []).append(item.annotation_id)
            if required_host and any(route["host"] != required_host for route in routes.values()):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"this compatibility route requires exact {required_host} provenance",
                )
            bundle = snapshot["full_bundle"]
        else:
            if snapshot["feedback_hash"] != expected_feedback_hash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="feedback changed since preview; review the updated feedback before sending",
                )
            annotation_ids = snapshot["annotation_ids"]
            if not annotation_ids:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="there is no unpublished human feedback to send"
                )
            capability = _feedback_capability(
                session,
                revision,
                annotation_ids,
                snapshot["annotations_by_id"],
            )
            if capability["kind"] == "github":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="this Review's feedback goes to its pull request; publish it through the GitHub preview",
                )
            if not capability["supported"]:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=capability["reason"])
            routes = {key: dict(value) for key, value in capability["routes"].items()}
            if not implicit_operation:
                current_operation_id = _feedback_operation_id(session, revision, snapshot, capability)
                if operation_id != current_operation_id:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="feedback routing changed since preview; prepare the updated feedback before sending",
                    )
            if required_host and any(route.get("host") != required_host for route in routes.values()):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"this compatibility route requires exact {required_host} provenance",
                )
            if not copied and any(route.get("kind") == "export" for route in routes.values()):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="copy the feedback for the author, then confirm the handoff (copied: true)",
                )
            pending = tuple(
                DeliveryRecord(
                    id="",
                    annotation_id=annotation_id,
                    target_type="clipboard" if routes[annotation_id].get("kind") == "export" else "agent_session",
                    target_ref=str(routes[annotation_id]["target_ref"]),
                    state="prepared",
                    operation_id=operation_id,
                    revision_id=revision.id,
                    feedback_hash=snapshot["feedback_hash"],
                    annotation_version=int(snapshot["annotation_versions"].get(annotation_id) or 0),
                )
                for annotation_id in annotation_ids
            )
            try:
                operation, _created = store.begin_delivery_operation(pending)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            for annotation_id in annotation_ids:
                ids_by_target.setdefault(str(routes[annotation_id]["target_ref"]), []).append(annotation_id)
            bundle = snapshot["bundle"]

        if not copied and any(route.get("kind") == "export" for route in routes.values()):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="copy the feedback for the author, then confirm the handoff (copied: true)",
            )

        target_results: list[dict[str, Any]] = []
        for target_ref, target_ids in sorted(ids_by_target.items()):
            target_rows, claimed = store.claim_delivery_target(operation_id, target_ref)
            if not claimed:
                first = target_rows[0]
                state = "uncertain" if first.state == "dispatching" else first.state
                target_results.append(
                    {
                        "state": state,
                        "target_ref": target_ref,
                        "remote_ref": first.remote_ref,
                        "message": first.last_error
                        or (
                            "delivery may already have crossed the host boundary; refusing an automatic duplicate"
                            if state == "uncertain"
                            else "delivery already recorded"
                        ),
                        "annotation_count": len(target_rows),
                    }
                )
                continue

            stale_rows = [
                row for row in target_rows if _feedback_annotation_version(row.annotation_id) != row.annotation_version
            ]
            if stale_rows:
                message = "feedback changed after this delivery operation was prepared; prepare a new operation"
                target_rows = store.update_delivery_target(
                    operation_id,
                    target_ref,
                    state="failed",
                    remote_ref="",
                    last_error=message,
                )
                target_results.append(
                    {
                        "state": "failed",
                        "target_ref": target_ref,
                        "remote_ref": "",
                        "message": message,
                        "annotation_count": len(target_rows),
                    }
                )
                continue

            dispatch_rows, dispatch_started = store.begin_delivery_dispatch(operation_id, target_ref)
            if not dispatch_started:
                first = dispatch_rows[0] if dispatch_rows else target_rows[0]
                target_results.append(
                    {
                        "state": "uncertain" if first.state == "dispatching" else first.state,
                        "target_ref": target_ref,
                        "remote_ref": first.remote_ref,
                        "message": first.last_error or "delivery dispatch ownership changed before host handoff",
                        "annotation_count": len(dispatch_rows or target_rows),
                    }
                )
                continue

            target_set = frozenset(target_ids)
            target_bundle = replace(
                bundle,
                items=tuple(item for item in bundle.items if item.annotation_id in target_set),
                orphaned=tuple(item for item in bundle.orphaned if item.annotation_id in target_set),
                resolved=(),
            )
            route = routes[target_ids[0]]
            delivery_id = operation_id + ":" + hashlib.sha256(target_ref.encode("utf-8")).hexdigest()[:12]
            route_kind = route.get("kind") or "agent"
            if route_kind == "export":
                result = AgentDeliveryResult(
                    "sent",
                    target_ref,
                    message="copied for the author; the next Snapshot returns this Review for re-review",
                )
            elif route_kind == "new_session":
                result = await asyncio.to_thread(
                    launch_agent_session,
                    str(route["host"]),
                    resolved_source_repo,
                    render_markdown(target_bundle),
                    session_id="" if route["session_id"] == PENDING_SESSION_ID else str(route["session_id"]),
                    delivery_id=delivery_id,
                    log_dir=store.review_dir / "handoff-launch",
                )
                if result.accepted and result.session_id:
                    launched_ref = f"{route['host']}:{result.session_id}"
                    if launched_ref != target_ref:
                        store.retarget_delivery_target(operation_id, target_ref, launched_ref)
                        target_ref = launched_ref
                    route = {**route, "session_id": result.session_id, "target_ref": launched_ref}
                    # Later sends reach the session this launch created.
                    store.set_destination(
                        ReviewDestination(
                            review_id=session.id,
                            kind="session",
                            host=str(route["host"]),
                            session_id=result.session_id,
                            set_by=session.reviewer_id,
                        )
                    )
            else:
                result = deliver_to_agent_session(
                    str(route["host"]),
                    str(route["session_id"]),
                    # The author's checkout, not the local server's scratch mirror
                    # (``repo_root`` there): the resumed session edits, verifies and
                    # claims `review_feedback_addressed` from this directory.
                    resolved_source_repo,
                    render_markdown(target_bundle),
                    delivery_id=delivery_id,
                )
            target_rows = store.update_delivery_target(
                operation_id,
                target_ref,
                state=result.state,
                remote_ref=result.remote_ref,
                last_error="" if result.accepted else result.message,
            )
            if result.accepted and route_kind != "export":
                # Causal link only: the handoff is Development evidence that
                # later work of this session answers these comments.
                from lemoncrow.pro.capabilities.review.development import record_feedback_handoff

                by_id = snapshot.get("annotations_by_id") or {}

                def _handoff_title(annotation_id: str, by_id: Mapping[str, Any] = by_id) -> str:
                    annotation = by_id.get(annotation_id)
                    if annotation is None:
                        return ""
                    lines = (annotation.title or annotation.body or "").strip().splitlines()
                    return lines[0][:160] if lines else ""

                with contextlib.suppress(Exception):
                    record_feedback_handoff(
                        development_store,
                        session.id,
                        host=str(route["host"]),
                        session_id=str(route["session_id"]),
                        operation_id=operation_id,
                        annotations=[(annotation_id, _handoff_title(annotation_id)) for annotation_id in target_ids],
                        state=result.state,
                    )
            target_results.append(
                {
                    "state": result.state,
                    "target_ref": result.target_ref if route_kind == "agent" and result.target_ref else target_ref,
                    "remote_ref": result.remote_ref,
                    "message": result.message,
                    "annotation_count": len(target_rows),
                }
            )

        states = {str(item["state"]) for item in target_results}
        if len(target_results) == 1:
            payload_state = str(target_results[0]["state"])
        elif states == {"consumed"}:
            payload_state = "consumed"
        elif states <= {"consumed", "sent"}:
            # Consumed only when every session confirmed it; otherwise handed off.
            payload_state = "sent"
        elif states.intersection({"blocked", "failed"}):
            payload_state = "partial" if len(states) > 1 else next(iter(states))
        elif "uncertain" in states:
            payload_state = "uncertain"
        elif "working" in states:
            payload_state = "working"
        elif "queued" in states:
            payload_state = "queued"
        else:
            payload_state = sorted(states)[0] if states else "failed"

        payload = {
            "state": payload_state,
            "target_ref": target_results[0]["target_ref"] if len(target_results) == 1 else "",
            "remote_ref": target_results[0]["remote_ref"] if len(target_results) == 1 else "",
            "message": "; ".join(str(item["message"]) for item in target_results if item.get("message")),
            "annotation_count": len(operation),
            "operation_id": operation_id,
            "targets": target_results,
        }
        if len(target_results) == 1 and payload_state in {"blocked", "failed"}:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=payload["message"])
        return payload

    @router.post("/reviews/{review_id}/feedback/deliver")
    async def _post_feedback_delivery(review_id: str, request: Request) -> dict[str, Any]:
        return await _deliver_feedback(review_id, request)

    @router.post("/reviews/{review_id}/feedback/claude")
    async def _post_feedback_claude(review_id: str, request: Request) -> dict[str, Any]:
        """Backward-compatible Claude route; empty-body callers get a safe implicit preview."""
        return await _deliver_feedback(
            review_id,
            request,
            required_host="claude",
            allow_implicit_preview=True,
        )

    # ----- GitHub pull request publishing --------------------------------- #

    def _github_pull_request(session: ReviewSession) -> Any:
        pr = _destination_pull_request(session)
        if pr is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "this Review has no pull request: open it with `lc review --pr`, "
                    "or choose the branch's pull request as where fixes go"
                ),
            )
        if not github_publish_enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="publishing to GitHub runs only on your local LemonCrow server, where your `gh` login lives",
            )
        return pr

    def _github_blocked(annotation_id: str, version: int) -> bool:
        """This comment version is already on GitHub, or an attempt may have put it there."""

        return any(
            item.target_type == "github_review"
            and item.annotation_version == version
            and (
                item.state in {"sent", "dispatching", "queued", "uncertain"}
                or (item.state in {"blocked", "failed"} and bool(item.remote_ref))
            )
            for item in store.list_deliveries(annotation_id)
        )

    def _reconcile_github_deliveries(roots: Sequence[Annotation], state: Any) -> None:
        """GitHub is the truth for unfinished attempts.

        An attempt whose marker is on the PR reached GitHub (its local record was
        lost to a timeout or crash) and becomes ``sent``. A ``dispatching``
        attempt with no marker that has been idle for ten minutes never got
        there and becomes ``failed``, so its comments can be published again.
        """

        from datetime import UTC, datetime, timedelta

        unfinished: dict[str, DeliveryRecord] = {}
        for annotation in roots:
            for item in store.list_deliveries(annotation.id):
                if item.target_type == "github_review" and item.state != "sent" and item.operation_id:
                    unfinished.setdefault(item.operation_id, item)
        cutoff = datetime.now(UTC) - timedelta(minutes=10)
        for operation_id, record in unfinished.items():
            earlier = state.marked_review(operation_id)
            if earlier is not None:
                store.update_delivery_operation(operation_id, state="sent", remote_ref=f"github-review:{earlier.id}")
                continue
            if record.state != "dispatching":
                continue
            try:
                updated = datetime.fromisoformat(record.updated_at)
            except ValueError:
                continue
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if updated < cutoff:
                store.update_delivery_operation(
                    operation_id, state="failed", last_error="interrupted before it reached GitHub"
                )

    def _github_plan(session: ReviewSession, revision: ReviewRevision) -> tuple[Any, Any, dict[str, int]]:
        """Load GitHub's live view of the PR and plan the review. Blocking: run off-loop."""

        from lemoncrow.pro.capabilities.review.github_publish import (
            PublishCandidate,
            build_plan,
            load_pull_request_state,
        )
        from lemoncrow.pro.capabilities.review.sources import github as github_source

        pr = _github_pull_request(session)
        if not revision.head_sha:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this Review revision has no head commit to publish against",
            )
        state = load_pull_request_state(pr, lambda args, stdin: github_source.run_gh(args, stdin))
        roots = [
            annotation
            for annotation in store.list_annotations(session.id)
            if annotation.source == "human" and not annotation.parent_id and annotation.state in {"open", "orphaned"}
        ]
        _reconcile_github_deliveries(roots, state)
        candidates: list[PublishCandidate] = []
        versions: dict[str, int] = {}
        for annotation in roots:
            version = _feedback_annotation_version(annotation.id)
            if _github_blocked(annotation.id, version):
                continue
            versions[annotation.id] = version
            anchor = annotation.anchor
            candidates.append(
                PublishCandidate(
                    annotation_id=annotation.id,
                    path=anchor.path,
                    side=anchor.side,
                    start_line=anchor.start_line,
                    end_line=anchor.end_line,
                    kind=annotation.kind,
                    body=annotation.body,
                    state=annotation.state,
                    anchor_detail=annotation.anchor_detail,
                )
            )
        current = store.latest_outcome(session.id, session.reviewer_id)
        plan = build_plan(
            pr,
            candidates,
            commit_id=revision.head_sha,
            outcome=current.outcome if current is not None else "",
            outcome_summary=current.summary if current is not None else "",
            outcome_current=current is not None and current.revision_id == revision.id,
            state=state,
        )
        return plan, state, versions

    async def _github_call(function: Any, *args: Any) -> Any:
        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError

        try:
            return await run_in_threadpool(function, *args)
        except GitHubCliError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/reviews/{review_id}/github/preview")
    async def _post_github_preview(review_id: str) -> dict[str, Any]:
        """Plan the GitHub review for the latest revision. Nothing is posted."""

        session = _session(review_id)
        revision = _latest(session)
        plan, state, _versions = await _github_call(_github_plan, session, revision)
        return {
            **plan.preview(),
            "review_id": session.id,
            "revision_id": revision.id,
            "operation_id": f"gop-{secrets.token_hex(16)}",
            "head_sha": state.head_sha,
            "stale": bool(state.head_sha) and state.head_sha != plan.commit_id,
            "empty": plan.empty,
        }

    @router.post("/reviews/{review_id}/github/publish")
    async def _post_github_publish(review_id: str, request: Request) -> dict[str, Any]:
        """Post exactly the previewed plan as one GitHub review, once per operation id."""

        from lemoncrow.pro.capabilities.review.feedback_sync import durable_github_target
        from lemoncrow.pro.capabilities.review.github_publish import publish_plan
        from lemoncrow.pro.capabilities.review.session_models import DeliveryRecord
        from lemoncrow.pro.capabilities.review.sources import github as github_source

        session = _mutable_session(review_id)
        _refuse_hosted_owned(session, "comments")
        revision = _latest(session)
        body = await _json_object(request)
        operation_id = str(body.get("operation_id") or "").strip()
        expected_revision_id = str(body.get("expected_revision_id") or "").strip()
        expected_plan_hash = str(body.get("expected_plan_hash") or "").strip()
        if not operation_id or not expected_revision_id or not expected_plan_hash:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="publishing requires operation_id, expected_revision_id, and expected_plan_hash from a preview",
            )
        if revision.id != expected_revision_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="review revision changed since the preview; preview again before publishing",
            )
        plan, state, versions = await _github_call(_github_plan, session, revision)

        def _result(review_ref: str, html_url: str, *, adopted: bool) -> dict[str, Any]:
            return {
                "state": "sent",
                "operation_id": operation_id,
                "remote_ref": review_ref,
                "html_url": html_url,
                "adopted": adopted,
                "event": plan.event,
                "inline": len(plan.inline),
                "folded": len(plan.folded),
            }

        # A previous attempt of this operation already reached GitHub (a retry
        # after a timeout or crash): record it instead of posting a duplicate.
        earlier = state.marked_review(operation_id)
        if earlier is not None:
            review_ref = f"github-review:{earlier.id}"
            if store.list_delivery_operation(operation_id):
                store.update_delivery_operation(operation_id, state="sent", remote_ref=review_ref)
            return _result(review_ref, earlier.html_url or plan.pr.url, adopted=True)
        if plan.plan_hash != expected_plan_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the GitHub review changed since the preview; preview again before publishing",
            )
        if plan.empty:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="there is nothing new to publish")
        # Deliveries name the pull request by its repository's durable id and
        # the host that id belongs to, never its display name: a later edit or
        # dismissal follows a rename or transfer and never reaches a different
        # repository that took the name, or the same id on another host.
        target_ref = durable_github_target(state.repository_id, str(plan.pr.number), state.host) if state.host else ""
        if plan.annotation_ids and not target_ref:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"GitHub did not report the repository id and host of {plan.pr.url}; nothing was published",
            )

        pending = tuple(
            DeliveryRecord(
                id="",
                annotation_id=annotation_id,
                target_type="github_review",
                target_ref=target_ref,
                state="dispatching",
                operation_id=operation_id,
                revision_id=revision.id,
                feedback_hash=plan.plan_hash,
                annotation_version=versions.get(annotation_id, 0),
            )
            for annotation_id in plan.annotation_ids
        )
        if pending:
            operation, created = store.begin_delivery_operation(pending)
            if not created:
                first = operation[0]
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=first.last_error or f"this publish is already {first.state}",
                )
        try:
            published = await _github_call(
                publish_plan,
                plan,
                operation_id,
                state,
                lambda args, stdin: github_source.run_gh(args, stdin),
            )
        except HTTPException as exc:
            if pending:
                # GitHub has no review carrying this operation's marker unless the
                # POST itself timed out; the next plan finds that one by its marker
                # and records it as sent instead of posting it again.
                store.update_delivery_operation(operation_id, state="failed", last_error=str(exc.detail))
            raise
        review_ref = f"github-review:{published.review_id}"
        if pending:
            store.update_delivery_operation(
                operation_id,
                state="sent",
                remote_ref=review_ref,
                remote_refs=published.remote_refs(plan.annotation_ids),
            )
        return _result(review_ref, published.html_url, adopted=published.adopted)

    def _finish_payload(
        session: ReviewSession,
        chosen: ReviewSessionStatus,
        *,
        expected_revision_id: str = "",
    ) -> dict[str, Any]:
        """Target-based completion snapshot shared by preview and mutation."""

        # Build every fallible summary before persisting the human's status.
        # Previously a missing/corrupt packet could make `_overview` raise after
        # `set_review_status` had already committed FINISHED, so the client saw a
        # 404 while the durable review silently changed state.
        overview = _overview(session)
        evidence_gaps = _evidence_gaps(session)
        # Verification blocks Finish exactly as it blocks readiness: the Brief's
        # counts also hold the authoring session's placeholders for checks it
        # never ran, which readiness rightly never counts.
        gate = _verification_gate(_verification_projection(session, _latest(session)))
        closure = set_review_status(
            store,
            session,
            store.latest_revision(session.id),
            status=chosen,
            reviewer_id=session.reviewer_id,
            expected_revision_id=expected_revision_id,
        )
        brief_raw = overview.get("brief")
        brief: Mapping[str, Any] = brief_raw if isinstance(brief_raw, Mapping) else {}
        artifacts_raw = brief.get("artifacts")
        artifacts: Mapping[str, Any] = artifacts_raw if isinstance(artifacts_raw, Mapping) else {}
        return {
            "review_id": closure.review_id,
            "status": closure.status,
            # Finish is fetched immediately before the sheet opens. Carry
            # the same fresh limitations that _overview() just resolved so
            # background analysis cannot leave stale Reader degradation here.
            "degraded": list(overview.get("degraded") or ()),
            "target_count": closure.target_count,
            "reviewed_targets": closure.reviewed_targets,
            "unreviewed_targets": closure.unreviewed_targets,
            "changed_since_review": closure.changed_since_review,
            "needs_changes": closure.needs_changes,
            "unknown_targets": closure.unknown_targets,
            "open_comments": closure.open_comments,
            "orphaned_comments": closure.orphaned_comments,
            "failed_verification": len(gate["failed"]) + len(gate["failed_browser"]),
            "unresolved_verification": len(gate["unresolved"]) + len(gate["carried"]) + len(gate["open_browser"]),
            "previous_revision_evidence": int(artifacts.get("stale") or 0),
            "discarded_verdicts": len(overview.get("discarded") or ()),
            # Decision progress, separate from code inspection (spec §4.7). It
            # does not depend on the Brief capability: turning the Brief off
            # must not hide outstanding or deferred decisions at Finish.
            "decisions": _decision_summary(session),
            # Spec §4.7: claims the current evidence does not support, by name.
            "evidence_gaps": evidence_gaps,
        }

    def _evidence_gaps(session: ReviewSession) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.review_plan import SUMMARY_TEXT

        revision = store.latest_revision(session.id)
        if revision is None:
            return {"total": 0, "claims": []}
        plan, ordered, view, _states, plan_checks = _brief_plan(session, revision, historical=False)
        # The same overlay the Brief shows, so Finish never disagrees with it.
        plan, _block = _with_synthesis(session, revision, plan, ordered, view, plan_checks)
        owners = {claim_id: item for item in plan.items for claim_id in item.claim_ids}
        gaps = [
            {
                "id": claim.id,
                "item_id": owners[claim.id].id if claim.id in owners else "",
                "title": owners[claim.id].title if claim.id in owners else claim.statement,
                "summary": claim.summary,
                "summary_text": SUMMARY_TEXT[claim.summary],
            }
            for claim in plan.claims
            if claim.summary in _EVIDENCE_GAP_SUMMARIES
        ]
        return {"total": len(gaps), "claims": gaps}

    def _decision_summary(session: ReviewSession) -> dict[str, int]:
        revision = store.latest_revision(session.id)
        states = [] if revision is None else [s for s in _decision_states_now(session, revision) if not s.withdrawn]
        accepted = [state for state in states if state.disposition == "accepted"]
        return {
            "total": len(states),
            "open": sum(1 for state in states if state.disposition == "open"),
            "change_requested": sum(1 for state in states if state.disposition == "change_requested"),
            "deferred": sum(1 for state in states if state.disposition == "deferred"),
            "needs_reconsideration": sum(1 for state in accepted if state.validity == "needs_reconsideration"),
            "implementation_changed": sum(
                1 for state in accepted if state.validity == "implementation_changed_since_acceptance"
            ),
            "unverified": sum(1 for state in accepted if state.validity == "unverified"),
            "accepted": sum(1 for state in accepted if state.validity == "current"),
        }

    def _outcome_payload(session: ReviewSession) -> dict[str, Any]:
        revision = _latest(session)
        current = store.latest_outcome(session.id, session.reviewer_id)
        return {
            "review_id": session.id,
            "revision_id": revision.id,
            "reviewer_id": session.reviewer_id,
            "current": (
                None
                if current is None
                else {
                    "id": current.id,
                    "review_id": current.review_id,
                    "revision_id": current.revision_id,
                    "reviewer_id": current.reviewer_id,
                    "outcome": current.outcome,
                    "summary": current.summary,
                    "created_at": current.created_at,
                    "stale": current.revision_id != revision.id,
                }
            ),
            "history": [
                {
                    "id": item.id,
                    "review_id": item.review_id,
                    "revision_id": item.revision_id,
                    "reviewer_id": item.reviewer_id,
                    "outcome": item.outcome,
                    "summary": item.summary,
                    "created_at": item.created_at,
                    "stale": item.revision_id != revision.id,
                }
                for item in store.list_outcomes(session.id, reviewer_id=session.reviewer_id)
            ],
        }

    # ----- local <-> hosted attachment (G5-SYNC) ------------------------ #

    def _require_attachment_store() -> None:
        if not store.hosted_attachment_supported():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="hosted attachment is a local Review feature",
            )

    def _hosted_status(session: ReviewSession) -> dict[str, Any]:
        attachment = store.get_hosted_attachment(session.id)
        if attachment is None:
            return {"attached": False, "review_id": session.id}
        actions = store.list_hosted_actions(session.id)
        counts = dict.fromkeys(HOSTED_ACTION_STATES, 0)
        for action in actions:
            counts[action.state] += 1
        try:
            projection = json.loads(attachment.projection_json or "{}")
        except ValueError:
            projection = {}
        return {
            "attached": True,
            "review_id": session.id,
            "reference": hosted_reference(attachment.hosted_review_id),
            "attachment": {
                "origin": attachment.origin,
                "hosted_repo_id": attachment.hosted_repo_id,
                "hosted_review_id": attachment.hosted_review_id,
                "principal_id": attachment.principal_id,
                "attached_at": attachment.attached_at,
                "synced_at": attachment.synced_at,
                "url": f"{attachment.origin}/r/{attachment.hosted_review_id}",
            },
            "owns": list(_HOSTED_OWNED),
            "projection": projection,
            "actions": [action_wire(action) for action in actions],
            "counts": counts,
            "limits": {"unresolved": UNRESOLVED_ACTION_LIMIT, "resolved_kept": RESOLVED_ACTION_KEEP},
        }

    def _hosted_conflict(exc: HostedAttachmentConflict) -> HTTPException:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{exc.code}: {exc.message}")

    def _prune_hosted(review_id: str) -> None:
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=RESOLVED_ACTION_MAX_AGE_DAYS)).isoformat()
        store.prune_hosted_actions(review_id, keep_resolved=RESOLVED_ACTION_KEEP, resolved_before=cutoff)

    def _local_content(revision: ReviewRevision | None) -> tuple[dict[str, str], dict[str, str]]:
        """``(unit_key -> fingerprint, file path -> fingerprint)`` of the local Snapshot on screen."""

        if revision is None:
            return {}, {}
        units = store.list_units(revision.id)
        return (
            {unit.unit_key: unit.content_fingerprint for unit in units},
            {unit.path: unit.content_fingerprint for unit in units if unit.kind == "file"},
        )

    def _stored_projection(attachment: HostedAttachment) -> dict[str, Any]:
        try:
            return parse_projection(json.loads(attachment.projection_json or "{}"))
        except HostedActionRefused as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{exc.code}: {exc.message}") from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="invalid_projection: sync the attachment again"
            ) from exc

    def _prepare_hosted(
        session: ReviewSession, attachment: HostedAttachment, kind: str, body: Mapping[str, Any]
    ) -> Any:
        projection = _stored_projection(attachment)
        revision = store.latest_revision(session.id)
        if kind == "outcome" and revision is not None:
            # A verdict covers the whole Change: only from a saved Snapshot (RC5).
            _require_saved_snapshot(revision)
        local_units, local_files = _local_content(revision)

        def local_text(path: str) -> str | None:
            # The exact new side on screen: a queued comment anchors only on it (RC5).
            if revision is None:
                return None
            return revision_new_side_text(store, resolved_repo, session, revision, path)

        try:
            return prepare_action(
                kind, body, projection, local_units=local_units, local_files=local_files, local_text=local_text
            )
        except HostedActionRefused as exc:
            raise HTTPException(
                status_code=(
                    status.HTTP_422_UNPROCESSABLE_ENTITY if exc.code == "invalid" else status.HTTP_409_CONFLICT
                ),
                detail=f"{exc.code}: {exc.message}",
            ) from exc

    def _queue_reader_write(session: ReviewSession, route: str, body: Mapping[str, Any]) -> dict[str, Any] | None:
        """Queue one Reader write while its Review is attached (RC5); ``None`` when it is not.

        The same G5 queue and validators as ``POST /hosted/actions``: every
        action the write becomes is prepared first, then all are queued in one
        transaction, so a request_change that also marks its target never
        half-queues. Nothing is written to the local Review.
        """

        from lemoncrow.pro.capabilities.review.store import new_hosted_action_id

        if not store.hosted_attachment_supported():
            return None
        attachment = store.get_hosted_attachment(session.id)
        if attachment is None:
            return None
        try:
            planned = reader_write_actions(route, body)
        except HostedActionRefused as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{exc.code}: {exc.message}"
            ) from exc
        if route == "annotation" and body.get("mark_target"):
            # The online route's rule: a request_change marks a target in its own
            # file. Hosted replays the comment and the mark as separate actions,
            # so only here can the pair be checked.
            targets = _stored_projection(attachment).get("targets")
            row = targets.get(str(body.get("target_unit_key") or "")) if isinstance(targets, Mapping) else None
            if isinstance(row, Mapping) and row.get("path") != body.get("path"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="invalid: annotation target belongs to a different file",
                )
        prepared = [_prepare_hosted(session, attachment, kind, item) for kind, item in planned]
        stored: list[HostedAction] = []
        superseded: list[str] = []
        try:
            with store.atomic_review_transition():
                for item in prepared:
                    action, replaced = store.add_hosted_action(
                        HostedAction(
                            id=new_hosted_action_id(),
                            review_id=session.id,
                            kind=item.kind,
                            snapshot_id=item.snapshot_id,
                            payload_json=json.dumps(item.payload, sort_keys=True),
                            preconditions_json=json.dumps(item.preconditions, sort_keys=True),
                            supersede_key=item.supersede_key,
                        ),
                        unresolved_limit=UNRESOLVED_ACTION_LIMIT,
                        supersede=item.supersede,
                    )
                    stored.append(action)
                    superseded.extend(replaced)
        except HostedAttachmentConflict as exc:
            raise _hosted_conflict(exc) from exc
        _prune_hosted(session.id)
        return {
            "queued": True,
            "reference": hosted_reference(attachment.hosted_review_id),
            "actions": [action_wire(action) for action in stored],
            "superseded": superseded,
            "counts": _hosted_status(session)["counts"],
        }

    def _hosted_origin(raw: Any) -> str:
        parts = urlsplit(str(raw or "").strip())
        if (
            parts.scheme not in {"https", "http"}
            or not parts.hostname
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="origin must be a bare http(s) origin such as https://lemoncrow.example",
            )
        return f"{parts.scheme}://{parts.netloc.lower()}"

    @router.get("/reviews/{review_id}/hosted")
    def _get_hosted(review_id: str) -> dict[str, Any]:
        _require_attachment_store()
        return _hosted_status(_session(review_id))

    @router.put("/reviews/{review_id}/hosted")
    async def _put_hosted(review_id: str, request: Request) -> dict[str, Any]:
        """Explicitly attach to a hosted Review, or refresh the projection of the same attachment."""

        _require_attachment_store()
        session = _mutable_session(review_id)
        body = await _json_object(request)
        origin = _hosted_origin(body.get("origin"))
        repo_id, hosted_id, principal_id = (
            str(body.get(name) or "").strip() for name in ("hosted_repo_id", "hosted_review_id", "principal_id")
        )
        if not (repo_id and hosted_id and principal_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="hosted_repo_id, hosted_review_id and principal_id are required",
            )
        try:
            projection = parse_projection(body.get("projection"))
        except HostedActionRefused as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{exc.code}: {exc.message}"
            ) from exc
        if (projection["repo_id"], projection["review_id"], projection["principal_id"]) != (
            repo_id,
            hosted_id,
            principal_id,
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="projection_mismatch: the projection describes another hosted Review or principal",
            )
        if store.get_hosted_attachment(session.id) is None:
            local_units, local_files = _local_content(store.latest_revision(session.id))
            shared = sum(
                1
                for unit_key, target in projection["targets"].items()
                if isinstance(target, Mapping) and local_units.get(unit_key) == target.get("fingerprint")
            ) + sum(1 for path, fingerprint in projection["files"].items() if local_files.get(path) == fingerprint)
            if not shared:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="no_shared_content: the hosted Snapshot shares no unit with this local Review",
                )
        try:
            _stored, created = store.attach_hosted(
                HostedAttachment(
                    review_id=session.id,
                    origin=origin,
                    hosted_repo_id=repo_id,
                    hosted_review_id=hosted_id,
                    principal_id=principal_id,
                    projection_json=json.dumps(projection, sort_keys=True),
                )
            )
        except HostedAttachmentConflict as exc:
            raise _hosted_conflict(exc) from exc
        return {**_hosted_status(session), "created": created}

    @router.delete("/reviews/{review_id}/hosted")
    def _delete_hosted(review_id: str, discard_unresolved: bool = False) -> dict[str, Any]:
        """Explicit detach: drops the projection and queue; keeps the local Review and its own state."""

        _require_attachment_store()
        session = _session(review_id)
        try:
            discarded, removed = store.detach_hosted(session.id, discard_unresolved=discard_unresolved)
        except HostedAttachmentConflict as exc:
            raise _hosted_conflict(exc) from exc
        return {
            "detached": True,
            "review_id": session.id,
            "discarded_unresolved": discarded,
            "removed_resolved": removed,
            "kept": (
                "the local Review, its Snapshots and every local mark, comment and outcome; "
                "everything already applied stays on hosted"
            ),
        }

    @router.post("/reviews/{review_id}/hosted/actions", status_code=status.HTTP_201_CREATED)
    async def _post_hosted_action(review_id: str, request: Request) -> dict[str, Any]:
        """Explicitly queue one action for hosted, bound to the projected hosted Snapshot."""

        from lemoncrow.pro.capabilities.review.store import new_hosted_action_id

        _require_attachment_store()
        session = _mutable_session(review_id)
        body = await _json_object(request)
        attachment = store.get_hosted_attachment(session.id)
        if attachment is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="not_attached: this Review is not attached to a hosted Review",
            )
        prepared = _prepare_hosted(session, attachment, str(body.get("kind") or ""), body)
        try:
            stored, superseded = store.add_hosted_action(
                HostedAction(
                    id=new_hosted_action_id(),
                    review_id=session.id,
                    kind=prepared.kind,
                    snapshot_id=prepared.snapshot_id,
                    payload_json=json.dumps(prepared.payload, sort_keys=True),
                    preconditions_json=json.dumps(prepared.preconditions, sort_keys=True),
                    supersede_key=prepared.supersede_key,
                ),
                unresolved_limit=UNRESOLVED_ACTION_LIMIT,
                supersede=prepared.supersede,
            )
        except HostedAttachmentConflict as exc:
            raise _hosted_conflict(exc) from exc
        _prune_hosted(session.id)
        return {
            "action": action_wire(stored),
            "superseded": list(superseded),
            "reference": hosted_reference(attachment.hosted_review_id),
        }

    @router.post("/reviews/{review_id}/hosted/actions/{action_id}")
    async def _post_hosted_action_op(review_id: str, action_id: str, request: Request) -> dict[str, Any]:
        """``result`` records hosted's answer; ``discard`` and ``requeue`` are the reviewer's resolutions."""

        from lemoncrow.pro.capabilities.review.store import new_hosted_action_id

        _require_attachment_store()
        session = _session(review_id)
        body = await _json_object(request)
        op = str(body.get("op") or "")
        action = store.get_hosted_action(session.id, action_id)
        if action is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such queued action")
        current: HostedAction
        try:
            if op == "result":
                expected = str(body.get("expected_state") or "pending")
                state = str(body.get("state") or "")
                reason = str(body.get("reason") or "")
                result = body.get("result") or {}
                result_json = json.dumps(result, sort_keys=True) if isinstance(result, Mapping) else ""
                if (
                    expected not in {"pending", "discarded"}
                    or state not in REPLAY_STATES
                    or (state == "conflict") != (reason in CONFLICT_REASONS)
                    or (state == "applied" and reason)
                    or not result_json
                    or len(result_json) > _HOSTED_RESULT_LIMIT
                ):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="result needs expected_state pending|discarded, state applied|conflict, "
                        "a known conflict reason only for conflict, and a small result object",
                    )
                current = store.transition_hosted_action(
                    session.id, action.id, expected_state=expected, state=state, reason=reason, result_json=result_json
                )
            elif op == "discard":
                if action.state not in HOSTED_ACTION_UNRESOLVED:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"action_state_changed: the action is {action.state}; only pending or conflict discards",
                    )
                current = store.transition_hosted_action(
                    session.id,
                    action.id,
                    expected_state=action.state,
                    state="discarded",
                    reason="discarded_by_reviewer",
                    result_json=json.dumps({"resolution": "discarded", "was": action.state}, sort_keys=True),
                )
            elif op == "requeue":
                session = _mutable_session(review_id)
                if action.state != "conflict":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"action_state_changed: the action is {action.state}; only a conflict requeues",
                    )
                attachment = store.get_hosted_attachment(session.id)
                if attachment is None:  # pragma: no cover - detach deletes every action with it
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_attached")
                try:
                    original = json.loads(action.payload_json or "{}")
                except ValueError:
                    original = {}
                prepared = _prepare_hosted(session, attachment, action.kind, original)
                try:
                    bound = json.loads(action.preconditions_json or "{}")
                except ValueError:
                    bound = {}
                if not isinstance(bound, Mapping) or not requeue_keeps_content(
                    action.kind, bound, prepared.preconditions
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            "content_changed: this action was made on content the local Review no longer shows, "
                            "so it stays in conflict; discard it and act again on what you see now"
                        ),
                    )
                new_id = new_hosted_action_id()
                with store.atomic_review_transition():
                    store.transition_hosted_action(
                        session.id,
                        action.id,
                        expected_state="conflict",
                        state="discarded",
                        reason="requeued",
                        result_json=json.dumps(
                            {"resolution": "requeued", "requeued_as": new_id, "conflict": action.reason},
                            sort_keys=True,
                        ),
                    )
                    current, _superseded = store.add_hosted_action(
                        HostedAction(
                            id=new_id,
                            review_id=session.id,
                            kind=prepared.kind,
                            snapshot_id=prepared.snapshot_id,
                            payload_json=json.dumps(prepared.payload, sort_keys=True),
                            preconditions_json=json.dumps(prepared.preconditions, sort_keys=True),
                            supersede_key=prepared.supersede_key,
                        ),
                        unresolved_limit=UNRESOLVED_ACTION_LIMIT,
                        supersede=prepared.supersede,
                    )
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="op must be result, discard or requeue"
                )
        except HostedAttachmentConflict as exc:
            raise _hosted_conflict(exc) from exc
        _prune_hosted(session.id)
        return {"action": action_wire(current), **_hosted_status(session)}

    @router.get("/reviews/{review_id}/outcome")
    def _get_outcome(review_id: str) -> dict[str, Any]:
        return _outcome_payload(_session(review_id))

    @router.post("/reviews/{review_id}/outcome")
    async def _post_outcome(review_id: str, request: Request, response: Response) -> dict[str, Any]:
        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        # Saved-Snapshot rule: the queue's own (``_prepare_hosted``), else the local one below.
        _require_expected_revision(body, revision, action="recording this outcome", require_saved=False)

        from starlette.concurrency import run_in_threadpool

        queued = await run_in_threadpool(_queue_reader_write, session, "outcome", body)
        if queued is not None:
            response.status_code = status.HTTP_202_ACCEPTED
            return queued
        _refuse_hosted_owned(session, "outcomes")
        _require_saved_snapshot(revision)
        outcome = str(body.get("outcome") or "").strip().lower()
        if outcome not in REVIEW_OUTCOME_KINDS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="outcome must be comment, lgtm, or changes_requested",
            )
        # The verdict names the policy generation it was given under, so a later
        # policy change shows it as "approved under policy <short>".
        generation, _freshness = _readiness_policy(revision, _readiness_extension_payload(session, revision))
        try:
            store.record_outcome(
                session.id,
                revision.id,
                session.reviewer_id,
                outcome=outcome,  # type: ignore[arg-type]
                summary=str(body.get("summary") or ""),
                expected_current_revision_id=revision.id,
                activity_detail={"policy_generation": generation.digest},
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        await _enforce_armed_landing(session)
        return _outcome_payload(session)

    @router.get("/reviews/{review_id}/finish")
    def _get_finish_preview(review_id: str) -> dict[str, Any]:
        """Preview the exact finish sheet without changing review status."""

        session = _session(review_id)
        payload = _finish_payload(session, session.status)
        # What discarding would affect, for the confirmation dialog (nothing is deleted now).
        payload["discard"] = automation_hooks.discard_preview(session)
        return payload

    @router.post("/reviews/{review_id}/finish")
    async def _post_finish(review_id: str, request: Request) -> dict[str, Any]:
        """Record a human status choice; never infer or issue approval."""

        session = _session(review_id)
        revision = _latest(session)
        body: dict[str, Any] = {}
        if (await request.body()).strip():
            body = await _json_object(request)
        wanted = str(body.get("status") or "finished").strip().lower()
        if wanted == "finished":
            _refuse_hosted_owned(session, "outcomes")
        if wanted != "open":
            _require_expected_revision(body, revision, action="finishing this Review")
        chosen: ReviewSessionStatus
        if wanted == "open":
            chosen = "open"
        elif wanted == "finished":
            chosen = "finished"
        elif wanted == "archived":
            chosen = "archived"
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown review status {wanted!r}: expected 'open', 'finished' or 'archived'",
            )
        if session.status == "archived" and chosen not in ("open", "archived"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="discarded review can only be restored to open before another completion decision",
            )
        discard_hazards = None
        if chosen == "archived" and session.status != "archived":
            unsent = (
                store.list_hosted_actions(session.id, states=HOSTED_ACTION_UNRESOLVED)
                if store.hosted_attachment_supported()
                else ()
            )
            if unsent:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"unresolved_actions: {len(unsent)} queued action(s) were never applied on hosted; "
                    "sync or discard them, or detach with discard_unresolved=true, before archiving",
                )
            # Pending feedback / an armed landing: refused unless the request names each one.
            discard_hazards = automation_hooks.require_discard_confirmation(session, body)
        try:
            payload = _finish_payload(
                session,
                chosen,
                expected_revision_id=(str(body.get("expected_revision_id") or "") if chosen != "open" else ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if chosen == "archived":
            await _withdraw_landing_for_discard(session)
        if discard_hazards is not None and discard_hazards.hazards:
            payload["discard"] = automation_hooks.record_discard(session, discard_hazards)
        return payload

    @router.post("/reviews/{review_id}/refresh")
    async def _post_refresh(review_id: str, publish: bool = True) -> dict[str, Any]:
        """Re-snapshot the tree, reconcile marks, and name the human target delta."""

        session = _mutable_session(review_id)
        if not source_refresh_enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this Review has no trusted mutable source bound to the server; run lc review to publish a new revision",
            )
        # File-cache invalidation is relative to the revision the browser is
        # actually showing, which is the latest frozen revision before this
        # refresh. That is deliberately different from the reviewer's mark
        # baseline: a human can refresh several times without recording a new
        # verdict, while the browser still only needs N -> N+1 file changes.
        previous_latest = _latest(session)
        previous_latest_units = store.list_units(previous_latest.id)
        # Snapshot the reviewer's left-hand side before reconciliation mutates
        # mark rows and relocates annotations. R25 must describe the actual
        # transition, not reconstruct an approximation afterwards.
        before_marks = store.list_marks(session.id, reviewer_id=session.reviewer_id)
        before_annotations = store.list_annotations(session.id)

        from lemoncrow.pro.capabilities.review.sources.local import refresh

        try:
            result = refresh(
                store,
                session,
                resolved_source_repo,
                store_root=resolved_source_store,
                limit=5000,
                reviewer_id=session.reviewer_id,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        if publish:
            store.publish_snapshot(session.id, result.revision.id)
        await _invalidate_landing_after_semantic_change(session, previous_latest, result.revision)
        await _invalidate_landing_after_semantic_change(session, previous_latest, result.revision)
        if publish and result.revision.id != previous_latest.id:
            # A push: the planned pre-merge checks evaluate the new Snapshot.
            await asyncio.to_thread(_schedule_premerge_safely, session, result.revision)

        from lemoncrow.pro.capabilities.review.targets import (
            revision_delta_payload,
            revision_target_delta,
        )

        previous_targets: tuple[ReviewTarget, ...] = ()
        if result.previous_revision is not None:
            previous_units = store.list_units(result.previous_revision.id)
            previous_frontier = compute_frontier(
                session.id,
                session.reviewer_id,
                result.previous_revision,
                previous_units,
                before_marks,
                annotations=before_annotations,
            )
            previous_targets = _derive_targets(
                session,
                result.previous_revision,
                previous_units,
                previous_frontier.entries,
                read_packet_json(store, result.previous_revision),
                before_annotations,
            )

        current_units = store.list_units(result.revision.id)
        current_packet = read_packet_json(store, result.revision)
        current_annotations = store.list_annotations(session.id)
        current_frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            result.revision,
            current_units,
            store.list_marks(session.id, reviewer_id=session.reviewer_id),
            annotations=current_annotations,
        )
        current_targets = _derive_targets(
            session,
            result.revision,
            current_units,
            current_frontier.entries,
            current_packet,
            current_annotations,
        )
        target_delta = revision_target_delta(
            previous_targets,
            current_targets,
            aliases=result.reconciliation.aliases,
        )
        linked_proposals = store.link_applied_change_proposals(
            session.id,
            source_fingerprint=result.revision.source_fingerprint,
            result_revision_id=result.revision.id,
        )
        # Named out of the revision the reviewer last saw: it is the last one
        # that still contained the units this refresh took away.
        from lemoncrow.pro.capabilities.review.revisions import file_revision_delta

        rename_pairs: list[tuple[str, str]] = []
        if current_packet is not None:
            for row in current_packet.get("files") or ():
                if not isinstance(row, Mapping) or str(row.get("status") or "") != "renamed":
                    continue
                old_path = str(row.get("old_path") or "")
                new_path = str(row.get("path") or "")
                if old_path and new_path:
                    rename_pairs.append((old_path, new_path))
        same_diff_base = (
            previous_latest.range_mode == result.revision.range_mode
            and previous_latest.base_sha == result.revision.base_sha
            and previous_latest.merge_base_sha == result.revision.merge_base_sha
        )
        file_delta = file_revision_delta(
            previous_latest_units,
            current_units,
            renames=rename_pairs,
            same_diff_base=same_diff_base,
        )

        previous = result.previous_revision
        gone = {
            unit.unit_key: unit_label(unit) for unit in (store.list_units(previous.id) if previous is not None else ())
        }
        payload = _overview(session)
        payload["refreshed"] = {
            "created": result.created,
            "revision_number": _display_revision_number(result.revision),
            "previous_revision_number": (
                _display_revision_number(result.previous_revision) if result.previous_revision is not None else None
            ),
            "reopened": len(result.reconciliation.reopened),
            "rebased": len(result.reconciliation.rebased),
            "carried": len(result.reconciliation.carried),
            "added": len(result.reconciliation.added),
            "removed": [
                {"unit_key": key, "label": gone.get(key) or key}
                for key in sorted(result.reconciliation.removed, key=lambda key: (gone.get(key) or key, key))
            ],
            "discarded": _discarded_payload(session, result.revision),
            "notes": list(result.reconciliation.notes),
            "target_delta": revision_delta_payload(target_delta),
            "changed_paths": list(file_delta.changed),
            "added_paths": list(file_delta.added),
            "removed_paths": list(file_delta.removed),
            "renamed_paths": [{"old_path": old_path, "path": new_path} for old_path, new_path in file_delta.renamed],
            "preserved_paths": list(file_delta.preserved),
            "applied_proposals": [
                _proposal_payload(item, current_targets=current_targets) for item in linked_proposals
            ],
        }
        return payload

    def _review_queue(
        session: ReviewSession,
        revision: ReviewRevision,
        units: Sequence[ReviewUnit],
        packet: Mapping[str, Any] | None,
        base: Sequence[Any],
    ) -> tuple[Any, ...]:
        """The canonical queue for hosts that derive base targets themselves.

        Same path as every Reader surface (Routine proofs, plan questions,
        decisions, AI questions), so hosted APIs cannot disagree with the
        Reader about tier or order (spec §7, one queue).
        """

        return tuple(_with_plan_inputs(session, revision, units, packet, base)[0])

    app.state.review_queue = _review_queue

    # Controlled Review actions (Fix, Fix all, explicit corrections) reuse the
    # gates above: session scope, mutability, exact-author routing, the
    # local-checkout policy and proposal apply. No new authority is granted.
    from lemoncrow.pro.capabilities.review.fix_agent import local_fix_agent_preference
    from lemoncrow.pro.capabilities.review.review_actions_api import ReviewActionDeps, register_review_action_routes

    def _action_run_plan(session: ReviewSession, revision: ReviewRevision) -> Mapping[str, Any]:
        return run_plans.current(
            session,
            revision,
            ai_candidates=lambda: _ai_candidate_count(session, revision),
            persist=revision.id == _latest(session).id,
        ).to_wire()

    from lemoncrow.pro.capabilities.review.outcomes.outcomes_api import outcomes_configured

    def _local_outcome_settings(_session: ReviewSession) -> tuple[Any, Any]:
        from lemoncrow.pro.capabilities.review.outcomes.local_deployments import load_local_deployment_settings
        from lemoncrow.pro.capabilities.review.outcomes.local_observability import load_local_observability_settings

        return load_local_deployment_settings(), load_local_observability_settings()

    effective_outcome_settings = outcome_settings or _local_outcome_settings

    def _production_outcomes(session: ReviewSession) -> Sequence[Any] | None:
        """Revert / Fix forward subjects; ``None`` (no palette entries) without a connector."""

        from lemoncrow.pro.capabilities.review.outcomes.outcome_actions import production_outcomes

        if not outcomes_configured(effective_outcome_settings, session):
            return None
        return production_outcomes(store, session)

    action_deps = ReviewActionDeps(
        store=store,
        development_store=development_store,
        session=_session,
        mutable_session=_mutable_session,
        latest=_latest,
        require_expected_revision=lambda body, revision, action: _require_expected_revision(
            body, revision, action=action
        ),
        exact_routes=_delivery_routes,
        author_route=lambda revision, path: _delivery_capability(revision, path=path),
        behavior_targets=lambda session, revision: _behavior_state(session, revision)[1],
        run_plan=_action_run_plan,
        apply_proposal=_apply_change_proposal,
        display_number=_display_revision_number,
        host_label=_host_label,
        source_repo=resolved_source_repo,
        log_dir=store.review_dir / "handoff-launch",
        no_local_checkout=no_local_checkout,
        source_mutation_reason=(
            ""
            if source_refresh_enabled
            else "this Review has no trusted mutable source bound to the server; apply it in the source host"
        ),
        # The personal preference is the local reviewer's own; a server without
        # a local checkout never reads its machine's settings on their behalf.
        preferred_agent=(lambda _session: local_fix_agent_preference()) if source_refresh_enabled else None,
        production_outcomes=_production_outcomes,
    )
    register_review_action_routes(router, action_deps)

    # P12-AGENT split request: the same gates as Fix (``action_deps``); only
    # collecting the agent's branches is new, and it needs the trusted checkout.
    from lemoncrow.pro.capabilities.review.split_request_api import SplitDeps, register_split_request_routes

    def _split_cohorts(session: ReviewSession, revision: ReviewRevision) -> tuple[list[dict[str, Any]], list[str]]:
        """Phase 8 cohorts (Brief items, canonical order) with their paths, plus the changed files."""

        from lemoncrow.pro.capabilities.review.review_plan import projection_payload

        plan, ordered, _view, states, _checks = _brief_plan(session, revision, historical=False)
        payload = projection_payload(
            plan,
            ordered,
            review_id=session.id,
            revision_id=revision.id,
            snapshot_number=max(0, revision.revision_number - 1),
            historical=False,
            decision_states=states,
        )
        path_of = {target.target_id: target.path for target in ordered}

        def paths(ids: Sequence[str]) -> list[str]:
            return list(dict.fromkeys(path_of[item] for item in ids if path_of.get(item)))

        cohorts = [
            {
                "id": str(item.get("key") or item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "kind": str(item.get("kind") or ""),
                "primary_paths": paths(item.get("primary_target_ids") or ()),
                "related_paths": paths(item.get("related_target_ids") or ()),
            }
            for item in payload.get("items") or ()
        ]
        changed = sorted({unit.path for unit in store.list_units(revision.id) if unit.kind == "file" and unit.path})
        return cohorts, changed

    register_split_request_routes(
        router,
        SplitDeps(
            actions=action_deps,
            cohorts=_split_cohorts,
            source_store_root=resolved_source_store,
            collect_reason=(
                ""
                if source_refresh_enabled and not no_local_checkout
                else "collecting a split's branches needs the trusted local checkout; run it from lc review there"
            ),
        ),
    )

    # P12-AGENT: human approval before risky agent actions. Requests come from the
    # agent host's hook; only a human principal (``approval_authority``) decides.
    from lemoncrow.pro.capabilities.review.action_approval_api import ApprovalDeps, register_action_approval_routes

    register_action_approval_routes(
        router,
        ApprovalDeps(
            store=store,
            development_store=development_store,
            session=_session,
            mutable_session=_mutable_session,
            latest=_latest,
            authority=approval_authority,
            policy=_approval_policy,
            separate_requester=approval_separate_requester,
        ),
    )

    from lemoncrow.pro.capabilities.review.premerge_api import (
        PremergeDeps,
        premerge_background,
        premerge_lock,
        register_premerge_routes,
    )

    def _premerge_row(
        session: ReviewSession,
        revision: ReviewRevision,
        check: Any,
        *,
        input_digest: str,
        status_value: str,
        explanation: str,
        reason: str,
        **extra: Any,
    ) -> ReviewEvidence:
        from lemoncrow.pro.capabilities.review.premerge_checks import nl_result_evidence

        return nl_result_evidence(
            review_id=session.id,
            revision_id=revision.id,
            check=check,
            status=status_value,
            explanation=explanation,
            reason=reason,
            input_digest=input_digest,
            **extra,
        )

    def _premerge_context(
        session: ReviewSession,
        revision: ReviewRevision,
        check: Any,
        facts: Any,
        *,
        depth: str,
        route_trust: ModelRouteTrust,
    ) -> tuple[Any, list[str], list[str]]:
        """The bounded model context for one check, filtered by AI content access for *route_trust*.

        ``(context, in-scope paths, excluded paths)``; the context is ``None``
        when policy excludes every in-scope path (nothing may reach a model).
        The run-plan depth bounds what the model sees.
        """

        from lemoncrow.pro.capabilities.review.premerge_checks import NL_CONTEXT_BUDGET, build_nl_context
        from lemoncrow.pro.capabilities.review.rules import content_access_for_path, path_matches

        paths = [path for path in facts.changed_paths if not check.paths or path_matches(path, check.paths)]
        governance = _review_governance(session, revision)
        decisions = {
            path: content_access_for_path(path, governance.content_access, route_trust=route_trust) for path in paths
        }
        excluded = [path for path in paths if not decisions[path].allowed]
        if paths and len(excluded) == len(paths):
            return None, paths, excluded
        packet = read_packet_json(store, revision)
        entries = _files_by_path(packet if isinstance(packet, Mapping) else None)
        excerpts: dict[str, str] = {}
        omitted: list[str] = []
        for path in (path for path in paths if decisions[path].allowed):
            entry = entries.get(path)
            text, refusal, _detail = synthesize_file_patch(entry) if entry is not None else ("", "missing", "")
            if text and not refusal and len(excerpts) < 40:
                excerpts[path] = text
            else:
                omitted.append(path)
        per_excerpt, total = NL_CONTEXT_BUDGET.get(depth, NL_CONTEXT_BUDGET["auto"])
        context = build_nl_context(
            check,
            description=facts.description,
            intents=facts.intents,
            excerpts=excerpts,
            excluded_paths=excluded,
            omitted_paths=omitted,
            max_excerpt_chars=per_excerpt,
            max_total_chars=total,
        )
        return context, paths, excluded

    def _premerge_model_row(
        session: ReviewSession,
        revision: ReviewRevision,
        check: Any,
        facts: Any,
        *,
        depth: str,
        input_digest: str,
    ) -> tuple[ReviewEvidence, bool]:
        """Evaluate one model-evaluated pre-merge check in this process (local only).

        One budgeted call, validated, never a forged PASS. Returns the row to
        store and whether a model call was attempted.
        """

        from lemoncrow.pro.capabilities.review.data_governance import process_route_trust
        from lemoncrow.pro.capabilities.review.intelligence_runtime import (
            execute_structured,
            run_intelligence,
        )
        from lemoncrow.pro.capabilities.review.premerge_checks import (
            NL_OUTPUT_SCHEMA,
            NL_PROMPT_VERSION,
            NL_SCHEMA_VERSION,
            nl_messages,
            validate_nl_output,
        )

        def row(status_value: str, explanation: str, reason: str, **extra: Any) -> ReviewEvidence:
            return _premerge_row(
                session,
                revision,
                check,
                input_digest=input_digest,
                status_value=status_value,
                explanation=explanation,
                reason=reason,
                **extra,
            )

        route_trust: ModelRouteTrust = "unknown"
        with contextlib.suppress(Exception):
            route_trust = process_route_trust(_resolve_route(session, revision, None, None).driver)
        context, paths, _excluded = _premerge_context(
            session, revision, check, facts, depth=depth, route_trust=route_trust
        )
        if context is None:
            return (
                row(
                    "UNKNOWN",
                    f"AI content-access policy excludes all {len(paths)} in-scope path(s) from the model.",
                    "content_excluded",
                ),
                False,
            )
        spec = _intelligence_spec(
            session,
            revision,
            capability="premerge_check",
            scope={"check_id": check.id, "definition_digest": check.definition_digest},
            context={"document": context.document},
            output_schema=NL_OUTPUT_SCHEMA,
            prompt_version=NL_PROMPT_VERSION,
            schema_version=NL_SCHEMA_VERSION,
            target_paths={path: path for path in paths},
        )
        request = spec.get("request")
        if request is None:
            return (
                row(
                    "NOT_RUN",
                    f"Review Intelligence is unavailable: {str(spec.get('error') or 'not configured')[:200]}.",
                    "ai_unavailable",
                ),
                False,
            )
        system_prompt, user_prompt = nl_messages(context)

        def execute() -> Any:
            project_root = _intelligence_project_root(
                session,
                revision,
                capability="premerge_check",
                driver=str(spec["driver"]),
                snapshot_identity=str(spec["snapshot_identity"]),
            )
            return execute_structured(
                system=system_prompt,
                prompt=user_prompt,
                output_schema=NL_OUTPUT_SCHEMA,
                driver=str(spec["driver"]),
                model=str(spec["model"]) or None,
                project_root=project_root,
                origin="machine",
                timeout_s=spec["budget"].timeout_s,
            )

        outcome = run_intelligence(store=store, request=request, executor=execute)
        provenance = {"model": outcome.model, "driver": outcome.driver, "run_id": outcome.run_id}
        if outcome.status != "ready" or not isinstance(outcome.result, Mapping):
            over_budget = "budget exhausted" in outcome.error.lower()
            return (
                row(
                    "NOT_RUN" if over_budget else "UNKNOWN",
                    f"Model evaluation did not complete: {outcome.error[:200] or outcome.status}.",
                    "budget_exhausted" if over_budget else "model_error",
                    **provenance,
                ),
                not over_budget,
            )
        status_value, explanation, cited, reason = validate_nl_output(outcome.result, context)
        return row(status_value, explanation, reason, evidence=cited, **provenance), True

    def _run_premerge_checks(
        session: ReviewSession,
        revision: ReviewRevision,
        plan: Any,
        *,
        wanted: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """Execute ``ai.premerge_checks`` under *plan*: idempotent per (Snapshot, check, input digest).

        The same inputs return the stored answer and append nothing. A new
        answer replaces every other result of that check on this Snapshot, so
        a Snapshot holds at most one result row per check.
        """

        from lemoncrow.pro.capabilities.review.capability_catalog import PREMERGE_CHECKS_ENTRY
        from lemoncrow.pro.capabilities.review.premerge_checks import (
            model_needed,
            nl_input_digest,
            nl_results_from_evidence,
        )

        entry = plan.entry(PREMERGE_CHECKS_ENTRY)
        if premerge_submitter is not None or entry is None or entry.status != "run":
            # Hosted evaluations run only as durable jobs on the plan's locus.
            return []
        extension = _readiness_extension_payload(session, revision)
        facts = _premerge_facts(session, revision, extension, plan=plan)
        if facts is None:
            return []
        generation = _readiness_policy(revision, extension)[0].digest
        recorder = run_plans.compute.recorder(session.id, revision.id)
        recorded: list[dict[str, Any]] = []
        for check in facts.policy.checks:
            if (wanted and check.id not in wanted) or not model_needed(check, facts):
                continue
            digest = nl_input_digest(check, facts, generation=generation, depth=entry.depth)
            with premerge_lock(revision.id, check.id):
                rows = [
                    item
                    for item in nl_results_from_evidence(store.list_evidence(session.id))
                    if item.revision_id == revision.id and item.requirement_key == check.requirement_key
                ]
                settled = next(
                    (
                        item
                        for item in rows
                        if item.input_digest == digest
                        and item.definition_digest == check.definition_digest
                        and item.reusable
                    ),
                    None,
                )
                if settled is not None:
                    recorded.append(
                        {
                            "check": check.id,
                            "evidence_id": settled.evidence_id,
                            "status": settled.status,
                            "reused": True,
                        }
                    )
                    continue
                started = time.monotonic()
                row, attempted = _premerge_model_row(
                    session, revision, check, facts, depth=entry.depth, input_digest=digest
                )
                stored = store.add_evidence(row, expected_current_revision_id=revision.id)
                for previous in rows:
                    # Superseded: other inputs, or an unsettled answer for these.
                    store.delete_evidence(previous.evidence_id)
                if attempted:
                    recorder.record_compute(
                        PREMERGE_CHECKS_ENTRY, entry.executor, "model_call", max(0.0, time.monotonic() - started)
                    )
                recorded.append({"check": check.id, "evidence_id": stored.id, "reused": False})
        return recorded

    def _submit_premerge_jobs(
        session: ReviewSession,
        revision: ReviewRevision,
        plan: Any,
        *,
        trigger: str,
        wanted: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """Hosted: enqueue one durable job per (Snapshot, check, input digest) the plan runs.

        A settled answer for these exact inputs is reused and nothing is
        queued; the submitter is idempotent on the same key. Never a model call
        in this process.
        """

        from lemoncrow.pro.capabilities.review.capability_catalog import PREMERGE_CHECKS_ENTRY
        from lemoncrow.pro.capabilities.review.premerge_checks import (
            model_needed,
            nl_input_digest,
            nl_results_from_evidence,
        )

        entry = plan.entry(PREMERGE_CHECKS_ENTRY)
        if premerge_submitter is None or entry is None or entry.status != "run":
            return []
        extension = _readiness_extension_payload(session, revision)
        facts = _premerge_facts(session, revision, extension, plan=plan)
        if facts is None:
            return []
        generation = _readiness_policy(revision, extension)[0].digest
        stored = [
            item
            for item in nl_results_from_evidence(store.list_evidence(session.id))
            if item.revision_id == revision.id
        ]
        policy: Mapping[str, Any] | None = None
        governance: Any = None
        out: list[dict[str, Any]] = []
        for check in facts.policy.checks:
            if (wanted and check.id not in wanted) or not model_needed(check, facts):
                continue
            digest = nl_input_digest(check, facts, generation=generation, depth=entry.depth)
            same_inputs = [
                item
                for item in stored
                if item.requirement_key == check.requirement_key
                and item.input_digest == digest
                and item.definition_digest == check.definition_digest
            ]
            settled = next((item for item in same_inputs if item.reusable), None)
            if settled is not None:
                out.append(
                    {"check": check.id, "evidence_id": settled.evidence_id, "status": settled.status, "reused": True}
                )
                continue
            if policy is None:
                policy = _effective_intelligence_policy(session, revision)
                governance = _review_governance(session, revision)
            job = premerge_submitter(
                session,
                revision,
                {
                    "check_id": check.id,
                    "requirement_key": check.requirement_key,
                    "definition_digest": check.definition_digest,
                    "input_digest": digest,
                    "depth": entry.depth,
                    "executor": entry.executor,
                    "trigger": trigger,
                    # An explicit re-run of an unsettled answer (model error,
                    # no model route) may queue a new generation of the job.
                    "retry": trigger == "manual" and bool(same_inputs),
                    "run_plan": plan.to_wire(),
                },
                policy,
                governance,
            )
            out.append(
                {
                    "check": check.id,
                    "reused": False,
                    **{key: job[key] for key in ("status", "job_id", "execution_locus", "detail") if key in job},
                }
            )
        return out

    def _premerge_job_inputs(
        review_id: str, revision_id: str, *, check_id: str, input_digest: str, depth: str
    ) -> tuple[ReviewSession, ReviewRevision, Any, Any]:
        """The exact inputs a durable pre-merge job was queued for, or ``ValueError`` (superseded)."""

        from lemoncrow.pro.capabilities.review.premerge_checks import nl_input_digest

        session = store.get_session(review_id)
        revision = store.get_revision(revision_id)
        if session is None or revision is None or revision.review_id != review_id:
            raise ValueError("pre-merge job no longer has an exact Review Snapshot (superseded)")
        extension = _readiness_extension_payload(session, revision)
        facts = _premerge_facts(session, revision, extension)
        check = None if facts is None else next((item for item in facts.policy.checks if item.id == check_id), None)
        if facts is None or check is None or not check.model_evaluated:
            raise ValueError(f"pre-merge check {check_id!r} is no longer configured (superseded)")
        generation = _readiness_policy(revision, extension)[0].digest
        if nl_input_digest(check, facts, generation=generation, depth=depth) != input_digest:
            raise ValueError("pre-merge check inputs changed since the job was queued (superseded)")
        return session, revision, facts, check

    def _worker_premerge_prompt(
        review_id: str,
        revision_id: str,
        *,
        check_id: str,
        input_digest: str,
        depth: str,
        route_trust: ModelRouteTrust,
    ) -> Mapping[str, Any]:
        """The bounded prompt a durable executor sends to *its* model route (content-access filtered for it)."""

        from lemoncrow.pro.capabilities.review.premerge_checks import (
            NL_OUTPUT_SCHEMA,
            NL_PROMPT_VERSION,
            NL_SCHEMA_VERSION,
            nl_messages,
        )

        session, revision, facts, check = _premerge_job_inputs(
            review_id, revision_id, check_id=check_id, input_digest=input_digest, depth=depth
        )
        context, paths, _excluded = _premerge_context(
            session, revision, check, facts, depth=depth, route_trust=route_trust
        )
        system_prompt, user_prompt = ("", "") if context is None else nl_messages(context)
        return {
            "check_id": check.id,
            "input_digest": input_digest,
            # Every in-scope path is excluded for this route: send nothing.
            "excluded": context is None,
            "in_scope_paths": len(paths),
            "system": system_prompt,
            "prompt": user_prompt,
            "output_schema": dict(NL_OUTPUT_SCHEMA),
            "prompt_version": NL_PROMPT_VERSION,
            "schema_version": NL_SCHEMA_VERSION,
        }

    def _worker_premerge_publish(
        review_id: str,
        revision_id: str,
        publication_key: str,
        *,
        check_id: str,
        input_digest: str,
        depth: str,
        route_trust: ModelRouteTrust,
        answer: Mapping[str, Any] | None,
        error: str,
        driver: str,
        model: str,
        executor: str,
        seconds: float,
        unavailable: bool = False,
    ) -> Mapping[str, Any]:
        """Validate a durable executor's answer against the context it was sent and store one row.

        The worker reports only the model's answer: the context is rebuilt
        here, citations are checked against it, and an excluded or failed run
        is never a PASS. Idempotent per (Snapshot, check, input digest).
        """

        from lemoncrow.pro.capabilities.review.capability_catalog import PREMERGE_CHECKS_ENTRY
        from lemoncrow.pro.capabilities.review.premerge_checks import nl_results_from_evidence, validate_nl_output

        session, revision, facts, check = _premerge_job_inputs(
            review_id, revision_id, check_id=check_id, input_digest=input_digest, depth=depth
        )
        with premerge_lock(revision.id, check.id):
            rows = [
                item
                for item in nl_results_from_evidence(store.list_evidence(session.id))
                if item.revision_id == revision.id and item.requirement_key == check.requirement_key
            ]
            settled = next(
                (
                    item
                    for item in rows
                    if item.input_digest == input_digest
                    and item.definition_digest == check.definition_digest
                    and item.reusable
                ),
                None,
            )
            if settled is not None:
                return {"evidence_id": settled.evidence_id, "status": settled.status, "reused": True}
            context, paths, _excluded = _premerge_context(
                session, revision, check, facts, depth=depth, route_trust=route_trust
            )
            provenance = {"model": model[:200], "driver": driver[:64], "run_id": publication_key[:200]}
            attempted = not unavailable and (answer is not None or bool(error))
            cited: tuple[Any, ...] = ()
            if unavailable:
                # No model route where the plan placed the work: NOT_RUN, replaced on the next run.
                status_value, explanation, reason = (
                    "NOT_RUN",
                    f"Review Intelligence is unavailable: {error[:200] or 'no model route is configured'}.",
                    "ai_unavailable",
                )
            elif context is None:
                status_value, explanation, reason = (
                    "UNKNOWN",
                    f"AI content-access policy excludes all {len(paths)} in-scope path(s) from the model.",
                    "content_excluded",
                )
                attempted = False
            elif answer is None:
                status_value, explanation, reason = (
                    "UNKNOWN",
                    f"Model evaluation did not complete: {error[:200] or 'no answer was reported'}.",
                    "model_error",
                )
            else:
                status_value, explanation, cited, reason = validate_nl_output(answer, context)
            row = _premerge_row(
                session,
                revision,
                check,
                input_digest=input_digest,
                status_value=status_value,
                explanation=explanation,
                reason=reason,
                evidence=cited,
                **provenance,
            )
            try:
                stored_row = store.add_evidence(row, expected_current_revision_id=revision.id)
            except Exception as exc:
                raise ValueError(f"pre-merge result for a superseded Snapshot: {str(exc)[:200]}") from exc
            for previous in rows:
                store.delete_evidence(previous.evidence_id)
            if attempted:
                run_plans.compute.recorder(session.id, revision.id).record_compute(
                    PREMERGE_CHECKS_ENTRY, executor, "model_call", max(0.0, float(seconds))
                )
        _revalidate_armed_landing(session, origin="action")
        return {"evidence_id": stored_row.id, "status": status_value, "reused": False}

    app.state.review_worker_premerge_prompt = _worker_premerge_prompt
    app.state.review_worker_premerge_publisher = _worker_premerge_publish

    def _premerge_plan(session: ReviewSession, revision: ReviewRevision) -> Any:
        return run_plans.current(
            session,
            revision,
            ai_candidates=lambda: _ai_candidate_count(session, revision),
            persist=True,
        )

    # Snapshots with a scheduled evaluation in flight -> another event asked
    # for a re-run meanwhile. Entries leave when their job ends (bounded by
    # the jobs in flight).
    premerge_inflight: dict[str, bool] = {}
    premerge_inflight_lock = threading.Lock()

    def _schedule_premerge_checks(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        """Originate ``ai.premerge_checks`` on a durable event (capture, push, recorded intent).

        Never from a read and never from head-side policy: the plan entry is
        applicable only when the trusted base or operator policy configures a
        model-evaluated check, and runs only when the plan selects it (depth,
        family mode, trigger, budget).
        """

        from lemoncrow.pro.capabilities.review.capability_catalog import PREMERGE_CHECKS_ENTRY

        if _premerge_nl_count(session, revision) == 0:
            return {"status": "not_configured"}
        plan = _premerge_plan(session, revision)
        entry = plan.entry(PREMERGE_CHECKS_ENTRY)
        if plan.policy_error or entry is None or entry.status != "run":
            return {
                "status": "not_planned",
                "revision_id": revision.id,
                "detail": (
                    f"Review policy invalid: {plan.policy_error}"
                    if plan.policy_error
                    else entry.reason if entry is not None else "not selected by the run plan"
                ),
                "run_plan_digest": plan.digest,
            }
        if premerge_submitter is not None:
            # Hosted: durable jobs on the plan's executor locus, never this process.
            return {
                "status": "submitted",
                "revision_id": revision.id,
                "run_plan_digest": plan.digest,
                "jobs": _submit_premerge_jobs(session, revision, plan, trigger="auto"),
            }
        key = revision.id
        with premerge_inflight_lock:
            if key in premerge_inflight:
                premerge_inflight[key] = True
                return {"status": "running", "revision_id": revision.id, "run_plan_digest": plan.digest}
            premerge_inflight[key] = False

        def job() -> None:
            while True:
                try:
                    latest = store.latest_revision(session.id)
                    if latest is not None and latest.id == revision.id:
                        _run_premerge_checks(session, revision, _premerge_plan(session, revision))
                        _revalidate_armed_landing(session, origin="action")
                except Exception:
                    logging.getLogger(__name__).warning(
                        "scheduled pre-merge evaluation failed for Review %s", session.id, exc_info=True
                    )
                with premerge_inflight_lock:
                    if premerge_inflight.get(key):
                        premerge_inflight[key] = False
                        continue
                    premerge_inflight.pop(key, None)
                    return

        try:
            (premerge_executor or premerge_background)(job)
        except Exception as exc:
            with premerge_inflight_lock:
                premerge_inflight.pop(key, None)
            return {"status": "unavailable", "revision_id": revision.id, "detail": str(exc)[:200]}
        return {"status": "queued", "revision_id": revision.id, "run_plan_digest": plan.digest}

    def _schedule_premerge_safely(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        """Scheduling never fails the event that originated it."""

        try:
            return _schedule_premerge_checks(session, revision)
        except Exception as exc:
            logging.getLogger(__name__).warning("pre-merge scheduling failed for Review %s", session.id, exc_info=True)
            return {"status": "unavailable", "detail": str(exc)[:200]}

    def _schedule_premerge_by_id(review_id: str) -> Mapping[str, Any]:
        """Request-free entry (``app.state.review_premerge_scheduler``): the local capture warm-up."""

        session = store.get_session(review_id)
        if session is None or session.repo_root != expected_session_repo or session.status != "open":
            return {"status": "not_found"}
        revision = store.latest_revision(session.id)
        if revision is None:
            return {"status": "not_found"}
        return _schedule_premerge_safely(session, revision)

    def _schedule_auto_work(session: ReviewSession, revision: ReviewRevision) -> Mapping[str, Any]:
        """Hosted Auto origination: AI review, then the planned pre-merge checks."""

        premerge = _schedule_premerge_safely(session, revision)
        result = dict(_schedule_auto_ai_review(session, revision))
        if premerge.get("status") != "not_configured":
            result["premerge"] = premerge
        return result

    app.state.review_auto_scheduler = _schedule_auto_work
    app.state.review_premerge_scheduler = _schedule_premerge_by_id

    def _premerge_manual_run(
        session: ReviewSession, revision: ReviewRevision, wanted: frozenset[str]
    ) -> dict[str, Any]:
        """Explicit re-run: amends the plan with a manual pick of ``ai.premerge_checks``, then executes."""

        from lemoncrow.pro.capabilities.review.capability_catalog import PREMERGE_CHECKS_ENTRY
        from lemoncrow.pro.capabilities.review.run_plan import ManualPick

        plan = run_plans.amend(
            session,
            revision,
            [ManualPick(PREMERGE_CHECKS_ENTRY)],
            ai_candidates=lambda: _ai_candidate_count(session, revision),
        )
        entry = plan.entry(PREMERGE_CHECKS_ENTRY)
        if plan.policy_error or entry is None or entry.status != "run":
            reason = (
                f"Review policy invalid: {plan.policy_error}"
                if plan.policy_error
                else entry.reason if entry is not None else "not selected"
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"AI pre-merge checks are not in the effective run plan for this Snapshot: {reason}",
            )
        if premerge_submitter is not None:
            return {
                "evaluated": [],
                "jobs": _submit_premerge_jobs(session, revision, plan, trigger="manual", wanted=wanted),
                "run_plan_digest": plan.digest,
                "plan_entry": entry.to_wire(),
            }
        return {
            "evaluated": _run_premerge_checks(session, revision, plan, wanted=wanted),
            "run_plan_digest": plan.digest,
            "plan_entry": entry.to_wire(),
        }

    def _may_manage_review(session: ReviewSession) -> bool:
        """Owner/manager authority over what a Review is judged against (intent bindings)."""

        if review_manager is not None:
            return bool(review_manager(session))
        if acting_reviewer_id is None:
            return True  # the single local reviewer owns the Review
        durable = store.get_session(session.id)
        return durable is not None and durable.reviewer_id == session.reviewer_id

    def _premerge_repository(session: ReviewSession) -> str:
        from lemoncrow.pro.capabilities.review.sources.github import parse_pr_source_ref

        pull = parse_pr_source_ref(session.source_ref)
        return pull.slug if pull is not None else ""

    register_premerge_routes(
        router,
        PremergeDeps(
            store=store,
            session=_session,
            mutable_session=_mutable_session,
            latest=_latest,
            require_expected_revision=lambda body, revision, action: _require_expected_revision(
                body, revision, action=action
            ),
            facts=lambda session, revision: _premerge_facts(
                session, revision, _readiness_extension_payload(session, revision)
            ),
            generation=lambda session, revision: (
                _readiness_policy(revision, _readiness_extension_payload(session, revision))[0].digest
            ),
            run_checks=_premerge_manual_run,
            schedule_checks=_schedule_premerge_safely,
            may_manage=_may_manage_review,
            enforce_armed_landing=_enforce_armed_landing,
            repository=_premerge_repository,
            open_sessions=lambda limit: tuple(
                item
                for item in store.list_sessions(status="open", limit=limit)
                if item.repo_root == expected_session_repo
            ),
            now=utc_now,
        ),
    )

    # P10-SIGN: signed review records (export in Review Status; signing is the composition's).
    from lemoncrow.pro.capabilities.review.fix_agent import settings_root as _settings_root
    from lemoncrow.pro.capabilities.review.record_signing import LocalRecordSigning
    from lemoncrow.pro.capabilities.review.review_record_api import (
        RecordDeps,
        emit_on_provider_merge,
        export_record,
        register_review_record_routes,
    )

    def _record_run_plan(session: ReviewSession, revision: ReviewRevision) -> Mapping[str, Any] | None:
        try:
            return _action_run_plan(session, revision)
        except Exception as exc:  # recorded as unavailable, never as "no plan"
            logging.getLogger(__name__).warning("run plan unavailable for record of %s", session.id, exc_info=True)
            return {"error": str(exc) or exc.__class__.__name__}

    def _record_repository(session: ReviewSession) -> str:
        # A durable identity, never the local checkout path (records leave this machine).
        if session_repo_identity is not None:
            return session_repo_identity
        return next((row.repository_identity for row in store.list_bindings(session.id) if row.repository_identity), "")

    record_deps = RecordDeps(
        store=store,
        session=_session,
        latest=_latest,
        display_number=_display_revision_number,
        readiness_at=lambda session, instant: _readiness_projection(session, now=instant),
        policy_generation=lambda session, revision: _readiness_policy(
            revision, _readiness_extension_payload(session, revision)
        )[0].to_wire(),
        run_plan=_record_run_plan,
        authority=(
            record_signing
            if record_signing is not None
            else LocalRecordSigning(settings_root=_settings_root, repo_root=resolved_repo)
        ),
        repository=_record_repository,
        now=utc_now,
    )
    register_review_record_routes(router, record_deps)

    def _sign_record_for(review_id: str, *, signing: str = "auto", decision: Any = None) -> Any:
        """Request-free entry (``app.state.review_record_signer``): P10-LAND and provider merges."""

        session = store.get_session(review_id)
        if session is None or session.repo_root != expected_session_repo:
            raise LookupError("no such review")
        # No acting principal here: the composition's authority treats it as the
        # in-process control plane (hosted) or the local reviewer.
        return export_record(record_deps, session, signing=signing, decision=decision)

    app.state.review_record_signer = _sign_record_for

    def _record_on_merge(review_id: str) -> dict[str, Any]:
        """Request-free entry (``app.state.review_record_on_merge``): the local landing watch saw a merge."""

        session = store.get_session(review_id)
        if session is None or session.repo_root != expected_session_repo:
            raise LookupError("no such review")
        return emit_on_provider_merge(record_deps, session)

    app.state.review_record_on_merge = _record_on_merge

    # P10-LAND: auto-approval, break-glass, the merge-readiness SCM status and landing records.
    from lemoncrow.pro.capabilities.review import landing_api as _landing_api
    from lemoncrow.pro.capabilities.review.review_record_api import emit_landing_record

    def _landing_record_for(review_id: str, decision: Any) -> dict[str, Any]:
        """Request-free entry (``app.state.review_landing_record``): sign one landing decision's record.

        Hosted calls it only from the control plane (``sign_landing_decision``),
        never from a worker; locally it signs with the reviewer's opted-in key.
        """

        session = store.get_session(review_id)
        if session is None or session.repo_root != expected_session_repo:
            raise LookupError("no such review")
        return emit_landing_record(record_deps, session, decision)

    app.state.review_landing_record = _landing_record_for

    def _landing_findings(session: ReviewSession, revision: ReviewRevision) -> Any:
        from lemoncrow.pro.capabilities.review.auto_approval import FindingsFact
        from lemoncrow.pro.capabilities.review.review_triage import current_findings

        findings, stale = current_findings(
            store, revision_id=revision.id, annotations=store.list_annotations(session.id)
        )
        unresolved = [item for item in findings if item.unresolved]
        return FindingsFact(
            p0_p1=sum(1 for item in unresolved if item.severity in {"p0", "p1"}),
            unrated=sum(1 for item in unresolved if item.severity is None),
            other_unresolved=sum(1 for item in unresolved if item.severity not in {None, "p0", "p1"}),
            stale_unresolved=stale,
        )

    def _landing_behavior_blockers(session: ReviewSession, revision: ReviewRevision) -> list[dict[str, Any]]:
        from lemoncrow.pro.capabilities.review.verification_delta import auto_approval_blockers

        _collection, targets = _behavior_state(session, revision)
        return [blocker.to_wire() for blocker in auto_approval_blockers(target.delta for target in targets)]

    def _landing_activity(
        session: ReviewSession, item: Any, kind: str, summary: str, detail: Mapping[str, Any]
    ) -> None:
        human = kind == "landing.break_glass"
        store.record_activity(
            ReviewActivityEvent(
                id="",
                review_id=session.id,
                revision_id=str(_landing_metadata(item).get("revision_id") or _latest(session).id),
                kind=cast(Any, kind),
                actor_id=str(detail.get("actor") or "") if human else "lemoncrow",
                actor_type="human" if human else "unknown",
                subject_type="landing",
                subject_id=str(getattr(item, "id", "") or detail.get("break_glass_id") or ""),
                summary=summary[:500],
                detail_json=json.dumps(dict(detail), ensure_ascii=False, sort_keys=True, default=str),
            )
        )

    def _landing_scm_link(session: ReviewSession) -> Mapping[str, Any]:
        rows = _change_context_payload(store, session).get("scm") or [{}]
        return cast(Mapping[str, Any], rows[0])

    base_authority = (
        landing_authority
        if isinstance(landing_authority, _landing_api.LandingAuthority)
        else _landing_api.LandingAuthority()
    )
    landing_deps = _landing_api.LandingDeps(
        store=store,
        session=_session,
        latest=_latest,
        display_number=_display_revision_number,
        readiness=lambda session, instant: _readiness_projection(session, now=instant),
        extension=_readiness_extension_payload,
        review_config=_snapshot_review_config,
        changed_files=_changed_files,
        criticality=lambda session, revision, paths: _triage_policy(session, revision).review_criticality(paths)[0],
        behavior_blockers=_landing_behavior_blockers,
        verification_rows=lambda session, revision: [
            row for row in _verification_projection(session, revision).get("checks", ()) if isinstance(row, Mapping)
        ],
        findings=_landing_findings,
        arm=_arm_landing,
        activity=_landing_activity,
        approval_principal=approval_authority,
        may_manage=_may_manage_review,
        scm_link=_landing_scm_link,
        revalidate=lambda session, origin: _revalidate_armed_landing(session, origin=origin),
        # Control-plane signing is the composition's; local signs with the reviewer's own key.
        authority=(
            base_authority
            if base_authority.sign_decision is not None
            else replace(base_authority, sign_decision=_landing_record_for)
        ),
        now=utc_now,
    )
    _landing_api.register_landing_routes(router, landing_deps)

    def landing_revalidate(session: ReviewSession, origin: str) -> str:
        return _landing_api.revalidate(landing_deps, session, origin=origin)

    def _evaluate_landing_by_id(review_id: str, *, origin: str = "provider") -> dict[str, Any]:
        """Request-free entry (``app.state.review_landing_evaluator``): decide after a provider event.

        Runs with no viewer, so a change set reads as incomplete and never
        auto-approves; the decision, delivery and SCM status are the policy's.
        """

        session = store.get_session(review_id)
        if session is None or session.repo_root != expected_session_repo:
            return {"outcome": "none"}
        return _landing_api.decide(landing_deps, session, origin=origin)

    app.state.review_landing_evaluator = _evaluate_landing_by_id

    from lemoncrow.pro.capabilities.review.check_ingest_api import register_check_ingest_routes

    # P11-FED: check ingestion, static findings and rule usefulness. Caller-attested, except an
    # in-toto attestation a trusted-base key verifies (Trust T4).
    register_check_ingest_routes(
        router,
        store=store,
        session=_session,
        mutable_session=_mutable_session,
        requested_revision=_requested_revision,
        latest=_latest,
        evidence_payload=lambda record, session, revision: _review_evidence_payload(
            record,
            current=revision.id == _latest(session).id,
            revision_number=_display_revision_number(revision),
        ),
        verification_projection=_verification_projection,
        enforce_armed_landing=_enforce_armed_landing,
        trusted_review_config=_snapshot_review_config,
    )
    from lemoncrow.pro.capabilities.review.reviewer_suggestions_api import (
        ReviewerSuggestionDeps,
        register_reviewer_suggestion_routes,
    )

    # P9-ROUTE: advisory reviewer suggestions (read-only; hosted serves its own projection).
    register_reviewer_suggestion_routes(
        router,
        ReviewerSuggestionDeps(
            store=store,
            session=_session,
            requested_revision=_requested_revision,
            read_base_document=_snapshot_base_document,
            review_config=_snapshot_review_config,
            triage_policy=_triage_policy,
            effective_review=lambda session, revision: _effective_intelligence_policy(session, revision).get(
                "effective_review"
            ),
            packet=lambda revision: read_packet_json(store, revision),
            history_root=lambda: resolved_source_repo,
            same_repository=lambda other: other.repo_root == expected_session_repo,
            hosted=reviewer_suggestions,
        ),
    )

    from lemoncrow.pro.capabilities.review.interactive_preview_api import (
        InteractivePreviewBackend,
        InteractivePreviewDeps,
        close_backend,
        register_interactive_preview_routes,
    )
    from lemoncrow.pro.capabilities.review.snapshot import materialize_review_side

    def _materialize_preview_side(revision: ReviewRevision, side: str, target: Path) -> None:
        materialize_review_side(store.root, resolved_preview_repo, revision, side=side, target=target, store=store)

    def _build_static_preview_side(revision: ReviewRevision, side: str, framework: str, root: str) -> Path:
        from lemoncrow.pro.capabilities.review.web_preview import build_static_preview_side

        return build_static_preview_side(
            store.root,
            resolved_preview_repo,
            revision,
            side=side,
            framework=framework,
            project_root=root,
            store=store,
            dependency_root=resolved_dependency_repo,
        )

    # Phase 9R item 16: interactive side-by-side preview (author machine by default).
    preview_backend = interactive_preview or InteractivePreviewBackend.local(store.root, port=port)
    preview_manager = register_interactive_preview_routes(
        router,
        InteractivePreviewDeps(
            store=store,
            backend=preview_backend,
            compute=run_plans.compute.recorder,
            session=_session,
            mutable_session=_mutable_session,
            latest=_latest,
            require_expected_revision=lambda body, revision, action: _require_expected_revision(
                body, revision, action=action
            ),
            viewer_id=lambda session: str(_as_acting_reviewer(session).reviewer_id or ""),
            detected=run_plans.detected,
            plan=run_plans.peek,
            planned_request=_planned_request,
            preview_pick=lambda session, revision, pick: run_plans.preview(
                session, revision, [pick], ai_candidates=lambda: _ai_candidate_count(session, revision)
            ),
            usage=run_plans.usage,
            review_config=lambda revision: str(_snapshot_review_config(revision).content or ""),
            read_side=lambda revision, path, side: _snapshot_document_text(
                revision, path, side="old" if side == "old" else "new"
            ),
            hermetic=_hermetic_execution,
            materialize=_materialize_preview_side,
            build_static=_build_static_preview_side,
        ),
    )
    app.state.review_interactive_preview = preview_manager
    # Best effort: sandboxes also die with this process (bubblewrap --die-with-parent).
    with contextlib.suppress(Exception):
        app.router.add_event_handler("shutdown", lambda: close_backend(preview_backend, preview_manager))

    # P12-AUTO: automation rules, post-merge actions, published-feedback sync and
    # discard / hard-delete safety. Reuses the gates above; grants no authority.
    from lemoncrow.pro.capabilities.review.archive_safety import jsonl_deletion_audit
    from lemoncrow.pro.capabilities.review.automation_rules import (
        load_local_automation_rules,
        local_permission_check,
    )
    from lemoncrow.pro.capabilities.review.review_automation_api import (
        ReviewAutomationDeps,
        register_review_automation_routes,
    )

    def _local_feedback_sync(session: ReviewSession) -> Any:
        from lemoncrow.pro.capabilities.review.feedback_sync import GhFeedbackSyncProvider

        return GhFeedbackSyncProvider() if session.source_ref.startswith("github:") else None

    automation_hooks = register_review_automation_routes(
        router,
        ReviewAutomationDeps(
            store=store,
            session=_session,
            mutable_session=_mutable_session,
            latest=_latest,
            readiness=_readiness_projection,
            repository=automation_repository or (lambda session: session.repo_root),
            rules=automation_rules or (lambda _session: load_local_automation_rules()),
            permission_check=automation_permission_check or local_permission_check,
            feedback_version=_feedback_annotation_version,
            branch_protection=branch_protection,
            provider_fact=provider_merge_fact,
            executors=post_merge_executors,
            feedback_sync_provider=(
                feedback_sync_provider
                if feedback_sync_provider is not None
                else (_local_feedback_sync if github_publish_enabled else None)
            ),
            # The local server has one principal, its authenticated owner.
            hard_delete_allowed=hard_delete_allowed or (lambda _session: True),
            deletion_audit=deletion_audit or jsonl_deletion_audit(store.root / "audit" / "review-deletions.jsonl"),
            deletion_context=deletion_context,
            landing_delegate_available=effective_landing_delegate is not None,
            hosted_owned=_refuse_hosted_owned,
        ),
    )

    async def _after_provider_merge(session: ReviewSession) -> None:
        """Post-merge automation after the provider reported the merge; never breaks the caller."""

        from starlette.concurrency import run_in_threadpool

        try:
            await run_in_threadpool(automation_hooks.run_merge_automation, session)
        except Exception:  # automation is optional follow-up; the merge itself is already recorded
            logging.getLogger(__name__).warning("post-merge automation failed for Review %s", session.id, exc_info=True)

    # A composition that observes a provider merge outside a Reader request
    # (hosted webhook) runs the same idempotent automation through this seam.
    app.state.review_merge_automation = lambda review_id: automation_hooks.run_merge_automation(_session(review_id))

    # P15-DEPLOY: the merged change's deployments (read-only; nothing when unconfigured).
    from lemoncrow.pro.capabilities.review.outcomes.deployments_api import register_deployment_routes
    from lemoncrow.pro.capabilities.review.outcomes.local_deployments import local_deployment_projection

    register_deployment_routes(
        router,
        session=_session,
        projection=deployment_outcomes or (lambda session: local_deployment_projection(store, session)),
    )

    # P15-OBS: predicted vs observed, incident links, Revert / Fix forward links (nothing when unconfigured).
    from lemoncrow.pro.capabilities.review.outcomes.outcomes_api import register_production_routes

    register_production_routes(
        router,
        store=store,
        session=_session,
        mutable_session=_mutable_session,
        settings=effective_outcome_settings,
        rules=automation_rules or (lambda _session: load_local_automation_rules()),
        repository=automation_repository or (lambda session: session.repo_root),
        permission_check=automation_permission_check or local_permission_check,
    )

    app.include_router(router)


__all__ = [
    "API_PREFIX",
    "CONTENT_SECURITY_POLICY",
    "GROUP_KEYS",
    "GROUP_LABELS",
    "HEALTHZ_PATH",
    "PATCH_REFUSALS",
    "SECURITY_HEADERS",
    "UnitNaming",
    "ambiguous_symbols",
    "build_groups",
    "degraded_note",
    "group_for",
    "loopback_hosts",
    "loopback_origins",
    "make_token_dependency",
    "origin_refusal",
    "register_review_api",
    "status_column",
    "synthesize_file_patch",
    "unit_label",
]
