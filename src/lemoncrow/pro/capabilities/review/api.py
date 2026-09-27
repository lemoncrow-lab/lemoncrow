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
import mimetypes
import os
import posixpath
import re
import secrets
import tempfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from urllib.parse import unquote, urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from lemoncrow.pro.capabilities.review.relationships import (
    change_context_payload as _change_context_payload,
)
from lemoncrow.pro.capabilities.review.relationships import (
    relationship_payload as _relationship_payload,
)
from lemoncrow.pro.capabilities.review.session_models import (
    ANCHOR_METHOD_LABELS,
    EXACT_ANCHOR_METHODS,
    REVIEW_EVIDENCE_KINDS,
    REVIEW_EVIDENCE_SOURCES,
    ReviewChangeProposal,
    ReviewEvidence,
    anchor_symbol_claim,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from lemoncrow.pro.capabilities.review.code_intelligence import CodeIntelligenceProvider
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
    from lemoncrow.pro.capabilities.review.store import ReviewStore
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget

HEALTHZ_PATH = "/healthz"
API_PREFIX = "/api"

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


def _is_markdown_path(path: str) -> bool:
    """True for Markdown files the browser can render as documents."""

    return path.lower().endswith((".md", ".markdown", ".mdc"))


def _is_csv_path(path: str) -> bool:
    """True for CSV files the browser can render as bounded tables."""

    return path.lower().endswith(".csv")


_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))")
_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*(['\"])(.*?)\1", flags=re.IGNORECASE | re.DOTALL)


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

    if path not in set(frozen_submodule_changed_paths(store.root, revision)):
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


def _is_promoting(reason: str) -> bool:
    """Whether *reason* is a finding: the reader's one answer, asked once.

    Delegates to ``targets.reason_is_promoting`` so the two panes cannot drift
    apart. This is a refactor, not a behaviour change: :func:`group_for` is the
    only caller and its own only call site skips every entry whose ``kind`` is
    not ``"file"``, which is the predicate's default subject. The descriptors
    that moved into the shared predicate -- the symbol change verb, and a caller
    count read off a symbol -- are stamped by ``units._symbol_reasons`` only
    when a symbol is in hand, so no file entry ever carried one. What the
    sharing buys is that a later edit to either pane's rule cannot silently
    disagree with the other's.
    """

    from lemoncrow.pro.capabilities.review.targets import reason_is_promoting

    return reason_is_promoting(reason)


def group_for(entry: FrontierEntry, category: str) -> str:
    """Which of the five left-pane groups *entry* belongs in.

    Precedence is fixed and stated here once, because a row that could plausibly
    sit in two groups must always sit in the same one:

    1. content that moved under a mark outranks everything -- it is the only
       group whose members were once believed done;
    2. an explicit ``needs_changes`` verdict is an open request, not a
       to-do item;
    3. a live ``reviewed`` mark means done;
    4. generated/vendor files are mechanical;
    5. an unreviewed file with at least one *finding* against it needs
       attention; without one it is merely unreviewed.
    """

    if entry.state == "changed_since_review" or entry.changed_since_mark:
        return "changed_since_my_review"
    if entry.state == "needs_changes":
        return "needs_attention"
    if entry.state == "reviewed":
        return "reviewed"
    if category in _MECHANICAL_GROUPS:
        return "mechanical"
    if any(_is_promoting(reason) for reason in entry.reasons):
        return "needs_attention"
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


def _session_payload(session: ReviewSession, *, store: ReviewStore | None = None) -> dict[str, Any]:
    short_id = store.short_session_reference(session.id) if store is not None else session.id
    ref = f"r/{short_id}"
    return {
        "id": session.id,
        "short_id": short_id,
        "ref": ref,
        "object": "change",
        "change_ref": ref,
        "title": session.title,
        "subject_type": session.subject_type,
        "range_mode": session.range_mode,
        "source_ref": session.source_ref,
        "status": session.status,
        "actor_type": session.actor_type,
        "reviewer_id": session.reviewer_id,
        "repo_root": session.repo_root,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _display_revision_number(revision: ReviewRevision) -> int:
    """Stable zero-based Review-local number shown outside persistence internals."""

    return revision.revision_number - 1


def _revision_payload(revision: ReviewRevision, *, store: ReviewStore | None = None) -> dict[str, Any]:
    short_id = store.short_revision_reference(revision.id) if store is not None else revision.id
    ref = f"rr/{short_id}"
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
    for target in targets:
        targets_by_path.setdefault(target.path, []).append(target)

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

    def _order(row: dict[str, Any]) -> tuple[int, int, str]:
        rank = int(row["attention_rank"])
        return (1 if rank == 0 else 0, rank, str(row["path"]))

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
        "title": annotation.title,
        "evidence": list(annotation.evidence),
        "confidence": annotation.confidence,
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
        "status": "current" if current else "stale",
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
    rows.update(artifact_rows)
    return list(rows.values())


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #


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
    acting_reviewer_id: Callable[[], str] | None = None,
    github_publish_enabled: bool = False,
    ask_answerer: Any | None = None,
    code_intelligence: (
        CodeIntelligenceProvider | Callable[[ReviewSession, ReviewRevision], CodeIntelligenceProvider | None] | None
    ) = None,
    readiness_extension: Callable[[Any, Any], Mapping[str, Any]] | None = None,
    landing_delegate: Callable[..., Mapping[str, Any]] | None = None,
) -> None:
    """Add the review routes and browser-facing guards to *app*.

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

    ``code_intelligence`` is the narrow current-repository intelligence seam.
    It may be one provider or a per-Snapshot factory; the latter receives both
    the Review and exact revision so native current context can stay bound to
    the View/worktree that supplied that Snapshot. All returned evidence stays
    explicitly non-Snapshot-exact.

    ``readiness_extension`` lets an outer composition contribute organization
    policy conditions to the same readiness result. ``landing_delegate`` is the
    provider-owned final handoff; it receives ``action=arm|cancel`` and must not
    perform Git merging/rebasing inside LemonCrow.

    ``acting_reviewer_id`` resolves the authenticated reviewer for the current
    request. Multi-principal compositions (hosted) must pass it: every Reader
    judgment, outcome, comment and activity is then read and written as that
    principal instead of the session's stored single-reviewer identity. It
    must fail closed (raise) when no principal is bound to the request.
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

    resolved_repo = repo_root.expanduser().resolve()
    resolved_dependency_repo = dependency_repo_root.expanduser().resolve() if dependency_repo_root is not None else None
    resolved_preview_repo = resolved_dependency_repo or resolved_repo
    resolved_source_repo = source_repo_root.expanduser().resolve() if source_repo_root is not None else resolved_repo
    resolved_source_store = source_store_root.expanduser().resolve() if source_store_root is not None else store.root
    from lemoncrow.pro.capabilities.review.development import DevelopmentStore

    # Change-level Development: an append-only stream on the local store, fed
    # incrementally from this machine's agent ledgers when live context is on.
    development_store = DevelopmentStore(store)
    development_ingestor: Any | None = None
    if live_journal_enabled:
        from lemoncrow.pro.capabilities.review.development_ingest import ingestor_for

        development_ingestor = ingestor_for(
            store,
            source_store_root=resolved_source_store,
            repo_root=resolved_source_repo,
        )
    expected_session_repo = session_repo_identity if session_repo_identity is not None else str(resolved_repo)
    effective_landing_delegate = landing_delegate
    # Immutable Snapshot comparisons can be expensive (rendering two artifacts
    # or starting two isolated services) but are stable for the life of the
    # server. Keep bounded in-process caches so reopening/reordering the Reader
    # does not execute reviewed code merely to rebuild surface ranking.
    surface_impact_cache: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    surface_response_diff_cache: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    ask_snapshot_locks: dict[str, asyncio.Lock] = {}

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

        versions = store.list_annotation_versions(annotation_id)
        for version in reversed(versions):
            if version.change_kind != "author_response":
                return int(version.version_number)
        return 0

    def _feedback_partition(
        revision_id: str,
        annotations: Sequence[Annotation],
        *,
        source_ref: str = "",
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

        from lemoncrow.pro.capabilities.review.sources.github import GITHUB_SOURCE_PREFIX

        track_github = source_ref.startswith(GITHUB_SOURCE_PREFIX)
        github = {"unpublished": 0, "published": 0, "in_flight": 0}
        root_human_ids = tuple(
            annotation.id
            for annotation in annotations
            if annotation.source == "human"
            and not annotation.parent_id
            and annotation.state not in {"resolved", "obsolete"}
        )
        store.recover_stale_delivery_attempts(revision_id, root_human_ids)
        unpublished: set[str] = set()
        versions: dict[str, int] = {}
        published = 0
        in_flight = 0
        addressed = 0
        open_total = 0
        for annotation in annotations:
            if annotation.source != "human" or annotation.parent_id:
                continue
            if annotation.state in {"resolved", "obsolete"}:
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
            if annotation.author_response == "addressed":
                addressed += 1
                continue
            # A GitHub review is a separate audience: publishing a comment to the
            # PR does not hand it to the authoring agent, and vice versa.
            matching = tuple(
                item
                for item in deliveries
                if item.revision_id == revision_id
                and item.annotation_version == version
                and item.target_type != "github_review"
            )
            states = {item.state for item in matching}
            unsafe_to_resend = any(
                item.state in {"prepared", "leased", "dispatching", "working", "queued", "uncertain"}
                or (item.state in {"blocked", "failed"} and bool(item.remote_ref))
                for item in matching
            )
            if states.intersection({"sent", "consumed"}):
                published += 1
            elif unsafe_to_resend:
                # Keep active or ambiguous attempts out of the resendable bucket.
                # Only a proven not-dispatched failure becomes unpublished again.
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

    router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(auth_dependency)])

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

    def _latest(session: ReviewSession) -> ReviewRevision:
        revision = store.latest_revision(session.id)
        if revision is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="this review has no revision yet")
        return revision

    def _require_expected_revision(body: Mapping[str, Any], revision: ReviewRevision, *, action: str) -> None:
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

    def _last_commented_revision(session: ReviewSession) -> ReviewRevision | None:
        """Newest immutable revision on which this reviewer originated human feedback."""

        revisions = {item.id: item for item in store.list_revisions(session.id)}
        candidates = [
            revisions[item.revision_id]
            for item in store.list_annotations(session.id)
            if item.source == "human" and item.created_by == session.reviewer_id and item.revision_id in revisions
        ]
        return max(candidates, key=lambda item: item.revision_number, default=None)

    def _reviewed_revision(session: ReviewSession) -> ReviewRevision | None:
        """Durable intentional human-review cursor; opening a revision never moves it."""

        return baseline_revision(store, session.id, reviewer_id=session.reviewer_id)

    def _default_compare_source(session: ReviewSession, target: ReviewRevision) -> ReviewRevision:
        """Canonical default baseline: last reviewed, otherwise full target review."""

        reviewed = _reviewed_revision(session)
        if reviewed is not None and reviewed.revision_number <= target.revision_number:
            return reviewed
        # With no intentional human frontier, a delta from the first captured
        # checkpoint would hide unchanged portions of the current full review.
        # A self-view keeps normal target-vs-source-base review semantics; on a
        # brand-new Review this is naturally rev 0 -> rev 0.
        return target

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
        latest = revisions[-1]
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

    def _annotation_context_payload(
        session: ReviewSession,
        target: ReviewRevision,
        annotation: Annotation,
    ) -> dict[str, Any]:
        """Comment state at *target* without erasing the immutable origin revision."""

        revisions = store.list_revisions(session.id)
        by_id = {item.id: item for item in revisions}
        origin = by_id.get(annotation.revision_id)
        resolved = by_id.get(annotation.resolved_revision_id) if annotation.resolved_revision_id else None

        addressed_revision: ReviewRevision | None = None
        for version in store.list_annotation_versions(annotation.id):
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
                    "originating"
                    if origin is not None and origin.id == target.id
                    else (
                        "inherited"
                        if origin is not None and origin.revision_number < target.revision_number
                        else "future"
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
        source_tree = store.read_source_tree_artifact(session.id, source.id, side="new")
        target_tree = store.read_source_tree_artifact(session.id, target.id, side="new")
        if source_tree is not None and target_tree is not None:
            compare_paths = {
                path for path in set(source_tree) | set(target_tree) if source_tree.get(path) != target_tree.get(path)
            }
        else:
            source_blobs = store.read_blob_artifact(session.id, source.id)
            target_blobs = store.read_blob_artifact(session.id, target.id)
            compare_paths = (
                set(source_blobs or {})
                | set(target_blobs or {})
                | _revision_packet_paths(source)
                | _revision_packet_paths(target)
            )
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
        annotation_context = [
            _annotation_context_payload(session, target, item)
            for item in _ordered_annotations(store.list_annotations_at_revision(session.id, target.id))
        ]
        return {
            "review_id": session.id,
            "reviewer_id": session.reviewer_id,
            "from_revision": _revision_payload(source, store=store),
            "to_revision": _revision_payload(target, store=store),
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
        return tuple(item for item in store.list_evidence(session.id) if item.revision_id == revision.id)

    _browser_policy_cache: dict[str, Any] = {}
    _web_routes_cache: dict[str, tuple[str, ...] | None] = {}

    def _bounded_cache_put(cache: dict[str, Any], key: str, value: Any) -> None:
        # lc-debt: per-process FIFO of 128 immutable Snapshot projections; upgrade to a shared LRU if hosted fan-out needs it
        cache.pop(key, None)
        cache[key] = value
        while len(cache) > 128:
            cache.pop(next(iter(cache)))

    def _snapshot_review_config(revision: ReviewRevision) -> tuple[str, str]:
        """``.lemoncrow/review.yaml`` for browser policy, base side first.

        The base governs so a Change cannot weaken the browser checks gating
        its own landing; a configuration the Change itself introduces applies
        only when the base has none.
        """

        from lemoncrow.pro.capabilities.review.gitdiff import _blob_from_tree, _decode, _open_repo, _tree_for_sha

        config_path = ".lemoncrow/review.yaml"

        def from_tree(side: Literal["old", "new"]) -> str | None:
            tree = store.read_source_tree_artifact(revision.review_id, revision.id, side=side)
            if tree is None:
                return None
            entry = tree.get(config_path)
            if entry is None:
                return ""
            payload = store.read_source_content(str(entry.get("content_digest") or ""))
            return payload.decode("utf-8", errors="replace") if payload is not None else ""

        def from_git(sha: str) -> str | None:
            if not sha:
                return None
            for root in (resolved_dependency_repo, resolved_source_repo, resolved_repo):
                if root is None:
                    continue
                try:
                    repo = _open_repo(root)
                    tree = _tree_for_sha(repo, sha)
                    data, _reason = _blob_from_tree(repo, tree, config_path)
                except (KeyError, OSError, RuntimeError, ValueError):
                    continue
                return _decode(data) if data is not None else ""
            return None

        base = from_tree("old")
        if base is None:
            base = from_git(revision.base_sha)
        if base:
            return base, f"base Snapshot {config_path}"
        head = from_tree("new")
        if head is None:
            blobs = store.read_blob_artifact(revision.review_id, revision.id) or {}
            head = blobs.get(config_path)
        if head is None and revision.range_mode == "commit_range":
            head = from_git(revision.head_sha)
        if head:
            return head, f"Change Snapshot {config_path} (the base has none)"
        return "", ""

    def _browser_policy(revision: ReviewRevision) -> Any:
        from lemoncrow.pro.capabilities.review.browser_checks import BrowserPolicy, parse_browser_policy

        cached = _browser_policy_cache.get(revision.id)
        if cached is not None:
            return cached
        text, source = _snapshot_review_config(revision)
        policy = parse_browser_policy(text, source=source) if text else BrowserPolicy()
        _bounded_cache_put(_browser_policy_cache, revision.id, policy)
        return policy

    def _changed_paths(revision: ReviewRevision) -> tuple[str, ...]:
        packet = read_packet_json(store, revision)
        raw_files = packet.get("files", ()) if isinstance(packet, Mapping) else ()
        return tuple(
            str(item.get("path") or "")
            for item in raw_files
            if isinstance(item, Mapping) and str(item.get("path") or "")
        )

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
    ) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.browser_checks import scenario_applicability
        from lemoncrow.pro.capabilities.review.verification import build_verification_projection

        policy = _browser_policy(revision)
        changed_paths = _changed_paths(revision)
        web_routes: tuple[str, ...] | None = None
        # Route discovery can materialize the Snapshot. Readiness pays for it
        # only when a configured scenario cannot be decided from changed paths.
        needs_routes = discover_web_routes or any(
            scenario_applicability(scenario, changed_paths, None)[0] == "unknown" for scenario in policy.scenarios
        )
        if needs_routes:
            web_routes = _snapshot_web_routes(session, revision)
        return build_verification_projection(
            packet=read_packet_json(store, revision),
            evidence=store.list_evidence(session.id),
            revision=revision,
            revisions=store.list_revisions(session.id),
            browser_policy=policy,
            changed_paths=changed_paths,
            web_routes=web_routes,
            read_artifact=store.read_evidence_artifact,
        )

    def _dependency_projection(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.dependencies import build_dependency_review, supported_path
        from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, materialize_review_side
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

        scratch_root = surface_workspace_root or (store.root / "review" / "dependency-context")
        old_root = scratch_root / session.id / revision.id / "old"
        new_root = scratch_root / session.id / revision.id / "new"
        try:
            materialize_review_side(
                store.root,
                resolved_repo,
                revision,
                side="old",
                target=old_root,
                store=store,
            )
            materialize_review_side(
                store.root,
                resolved_repo,
                revision,
                side="new",
                target=new_root,
                store=store,
            )
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

    def _readiness_projection(session: ReviewSession) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.landing import (
            condition,
            normalized_requirement_state,
            readiness_summary,
        )

        revision = _latest(session)
        overview = _overview(session)
        progress = overview.get("progress") or {}
        context = _change_context_payload(store, session)
        conditions: list[dict[str, Any]] = []

        unreviewed = int(progress.get("unreviewed") or 0)
        unknown = int(progress.get("unknown") or 0)
        changed = int(progress.get("changed_since_review") or 0)
        needs_changes = int(progress.get("needs_changes") or 0)
        pending_targets = unreviewed + unknown + changed + needs_changes
        conditions.append(
            condition(
                "latest-snapshot-reviewed",
                "Latest Snapshot reviewed",
                state="pass" if pending_targets == 0 else "pending",
                detail=(
                    "All review areas on the latest Snapshot have current human judgment."
                    if pending_targets == 0
                    else f"{pending_targets} review area(s) still need human judgment or re-review."
                ),
                category="human_review",
            )
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

        def _removed_code_note(rows: tuple[Annotation, ...]) -> str:
            removed = sum(1 for item in rows if item.state == "obsolete")
            return (
                f" {removed} refer(s) to code that was since removed; a human must still resolve them."
                if removed
                else ""
            )

        conditions.append(
            condition(
                "human-change-requests",
                "No unresolved request-change comments",
                state="pass" if not request_changes else "fail",
                detail=(
                    "No unresolved human request-change comment remains."
                    if not request_changes
                    else f"{len(request_changes)} unresolved human request-change comment(s) remain."
                    + _removed_code_note(request_changes)
                ),
                category="human_review",
            )
        )
        conditions.append(
            condition(
                "human-comments",
                "No unresolved human comments",
                state="pass" if not other_comments else "pending",
                detail=(
                    "No unresolved human review discussion remains."
                    if not other_comments
                    else f"{len(other_comments)} unresolved human review discussion(s) remain."
                    + _removed_code_note(other_comments)
                ),
                category="human_review",
            )
        )

        verification = _verification_projection(session, revision)
        supply_kinds = {"sbom", "provenance", "vulnerability_scan"}
        generic_checks = [
            row
            for row in verification.get("checks", ())
            if row.get("kind") not in supply_kinds and not row.get("browser")
        ]
        verification_checks = [row for row in generic_checks if row.get("state") == "current"]
        # A check whose latest result on this Change was not PASS keeps blocking
        # as NOT_RUN on later Snapshots until that same check reports again: a
        # new Snapshot must never launder a failure into "no verification". A
        # stale PASS is named but never counted as passing.
        # A human waiver on this exact Snapshot unblocks such a row (and a
        # current NOT_RUN/UNKNOWN) without ever counting it as a pass.
        waived_checks = [row for row in generic_checks if row.get("waiver")]
        carried_checks = [
            row
            for row in generic_checks
            if row.get("state") == "stale" and row.get("status") != "PASS" and not row.get("waiver")
        ]
        stale_passes = [row for row in generic_checks if row.get("state") == "stale" and row.get("status") == "PASS"]

        def _check_names(rows: Sequence[Mapping[str, Any]]) -> str:
            names = [str(row.get("name") or "Verification") for row in rows]
            return ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")

        failed_checks = sum(1 for row in verification_checks if row.get("status") == "FAIL")
        current_unresolved = sum(
            1 for row in verification_checks if row.get("status") in {"NOT_RUN", "UNKNOWN"} and not row.get("waiver")
        )
        unresolved_checks = current_unresolved + len(carried_checks)
        passed_checks = sum(1 for row in verification_checks if row.get("status") == "PASS")
        verification_state = (
            "fail"
            if failed_checks
            else (
                "pending"
                if unresolved_checks
                else "waived" if waived_checks else "pass" if verification_checks else "not_applicable"
            )
        )
        waived_detail = (
            f"{len(waived_checks)} check(s) waived by a human on this Snapshot, not passed: "
            + "; ".join(
                f"{row.get('name') or 'Verification'} ({str((row.get('waiver') or {}).get('reason') or '').strip()})"
                for row in waived_checks[:4]
            )
            + (f" and {len(waived_checks) - 4} more" if len(waived_checks) > 4 else "")
            + "."
        )
        unresolved_detail = " ".join(
            part
            for part in (
                f"{current_unresolved} current check(s) are NOT_RUN or UNKNOWN." if current_unresolved else "",
                (
                    f"{len(carried_checks)} check(s) whose last result was not PASS have not reported on this "
                    f"Snapshot: {_check_names(carried_checks)}."
                    if carried_checks
                    else ""
                ),
            )
            if part
        )
        conditions.append(
            condition(
                "verification",
                (
                    "Verification waived"
                    if verification_state == "waived"
                    else (
                        "Verification passed"
                        if verification_checks or carried_checks or waived_checks
                        else "Verification requirements"
                    )
                ),
                state=verification_state,
                detail=(
                    f"{failed_checks} current check(s) failed."
                    if failed_checks
                    else (
                        unresolved_detail
                        if unresolved_checks
                        else (
                            (f"{passed_checks} current check(s) passed. " if passed_checks else "") + waived_detail
                            if waived_checks
                            else (
                                f"{passed_checks} current check(s) passed with no current failure or unresolved result."
                                if verification_checks
                                else (
                                    "No verification result is recorded for this Snapshot; "
                                    f"{len(stale_passes)} earlier passing check(s) are stale and not counted: "
                                    f"{_check_names(stale_passes)}."
                                    if stale_passes
                                    else "No required verification result is configured or recorded for this Snapshot."
                                )
                            )
                        )
                    )
                ),
                category="verification",
            )
        )

        # Browser regression: only repository-required, applicable scenarios
        # gate landing. Optional scenarios stay informative in verification.
        browser_summary = verification.get("browser") or {}
        required_browser = [
            row
            for row in verification.get("checks", ())
            if row.get("browser") and row.get("state") == "current" and row.get("required")
        ]
        if browser_summary.get("policy_error"):
            conditions.append(
                condition(
                    "browser-regression-policy",
                    "Browser regression policy",
                    state="pending",
                    detail=(
                        f"{browser_summary.get('policy_source') or 'Review configuration'} could not be read: "
                        f"{browser_summary.get('policy_error')}. Required browser checks cannot be determined."
                    ),
                    category="verification",
                )
            )
        if required_browser:
            failed_browser = [row for row in required_browser if row.get("status") == "FAIL"]
            open_browser = [row for row in required_browser if row.get("status") in {"NOT_RUN", "UNKNOWN"}]

            def _names(rows: list[Mapping[str, Any]]) -> str:
                return ", ".join(f"{row.get('name')} ({row.get('status')})" for row in rows[:4]) + (
                    "…" if len(rows) > 4 else ""
                )

            conditions.append(
                condition(
                    "browser-regression",
                    (
                        "Required browser regression passed"
                        if not failed_browser and not open_browser
                        else "Required browser regression"
                    ),
                    state="fail" if failed_browser else "pending" if open_browser else "pass",
                    detail=(
                        f"Failed on Snapshot {_display_revision_number(revision)}: {_names(failed_browser)}."
                        if failed_browser
                        else (
                            f"No current PASS for Snapshot {_display_revision_number(revision)}: {_names(open_browser)}."
                            if open_browser
                            else f"{len(required_browser)} required browser scenario(s) passed on this exact Snapshot."
                        )
                    ),
                    category="verification",
                )
            )

        supply_rows = [
            row
            for row in verification.get("artifacts", ())
            if row.get("state") == "current" and row.get("kind") in supply_kinds
        ]
        # Supply-chain artifacts are evidence, not a policy verdict by
        # themselves. Only an explicit verification status may satisfy or
        # block this landing condition.
        supply_policy_rows = [row for row in supply_rows if row.get("verification_status")]
        supply_failed = [row for row in supply_policy_rows if row.get("verification_status") == "FAIL"]
        supply_unresolved = [
            row for row in supply_policy_rows if row.get("verification_status") in {"NOT_RUN", "UNKNOWN"}
        ]
        # Same carry-forward rule as checks: the latest earlier verdict for a
        # policy that has not been re-evaluated on this Snapshot keeps blocking
        # unless it passed.
        current_policy_keys = {(row.get("kind"), row.get("title")) for row in supply_policy_rows}
        latest_prior_policy: dict[tuple[Any, Any], Mapping[str, Any]] = {}
        for row in verification.get("artifacts", ()):
            if row.get("state") != "stale" or row.get("kind") not in supply_kinds or not row.get("verification_status"):
                continue
            key = (row.get("kind"), row.get("title"))
            best = latest_prior_policy.get(key)
            rank = (int(row.get("snapshot_number") or 0), str(row.get("observed_at") or ""))
            if best is None or rank > (int(best.get("snapshot_number") or 0), str(best.get("observed_at") or "")):
                latest_prior_policy[key] = row
        supply_carried = [
            row
            for key, row in sorted(latest_prior_policy.items(), key=lambda item: str(item[0]))
            if key not in current_policy_keys and row.get("verification_status") != "PASS"
        ]
        supply_state = (
            "fail"
            if supply_failed
            else (
                "pending" if supply_unresolved or supply_carried else "pass" if supply_policy_rows else "not_applicable"
            )
        )
        supply_detail = (
            f"{len(supply_failed)} current supply-chain policy result(s) failed."
            if supply_failed
            else (
                " ".join(
                    part
                    for part in (
                        (
                            f"{len(supply_unresolved)} current supply-chain policy result(s) remain unresolved."
                            if supply_unresolved
                            else ""
                        ),
                        (
                            f"{len(supply_carried)} earlier supply-chain verdict(s) that did not pass have not been "
                            "re-evaluated on this Snapshot: "
                            + ", ".join(str(row.get("title") or row.get("kind") or "") for row in supply_carried[:4])
                            + "."
                            if supply_carried
                            else ""
                        ),
                    )
                    if part
                )
                if supply_unresolved or supply_carried
                else (
                    "Current dependency / supply-chain policy results passed."
                    if supply_policy_rows
                    else (
                        f"{len(supply_rows)} current supply-chain artifact(s) are available, but no policy verdict is recorded."
                        if supply_rows
                        else "No dependency / supply-chain policy evidence is required or recorded for this Snapshot."
                    )
                )
            )
        )
        conditions.append(
            condition(
                "supply-chain-policy",
                "Dependency / supply-chain policy passed" if supply_policy_rows else "Dependency / supply-chain policy",
                state=supply_state,
                detail=supply_detail,
                category="supply_chain",
            )
        )

        latest_outcomes = tuple(store.latest_outcomes(session.id))
        current_outcomes = tuple(item for item in latest_outcomes if item.revision_id == revision.id)
        requested = tuple(item for item in current_outcomes if item.outcome == "changes_requested")
        approvals = tuple(item for item in current_outcomes if item.outcome == "lgtm")
        if requested:
            conditions.append(
                condition(
                    "human-approval-state",
                    "Required human approval state",
                    state="fail",
                    detail=f"{len(requested)} current human reviewer outcome(s) request changes.",
                    category="approval",
                )
            )
        elif approvals:
            conditions.append(
                condition(
                    "human-approval-state",
                    "Human approval state",
                    state="pass",
                    detail=f"{len(approvals)} current human LGTM outcome(s); no current changes-requested outcome.",
                    category="approval",
                )
            )
        else:
            conditions.append(
                condition(
                    "human-approval-state",
                    "Additional LemonCrow approval",
                    state="not_applicable",
                    detail="No additional LemonCrow approval policy is configured for this Change.",
                    category="approval",
                )
            )
        for item in context.get("scm_requirements", ()):
            state = normalized_requirement_state(item.get("status"))
            identity = str(item.get("title") or item.get("external_id") or item.get("relation") or "SCM requirement")
            conditions.append(
                condition(
                    f"scm-requirement:{item.get('id')}",
                    identity,
                    state=state,
                    detail=(
                        str(item.get("provider") or "Connected SCM")
                        + " remains authoritative for this landing requirement."
                    ),
                    owner="scm",
                    category="scm",
                    url=str(item.get("url") or ""),
                )
            )

        blocking_relations = {"depends_on", "requires", "parent", "previous"}
        for item in context.get("relations", ()):
            if str(item.get("relation") or "") not in blocking_relations:
                continue
            target_ref = str(item.get("target_change_ref") or "")
            label = str(item.get("title") or target_ref or item.get("external_id") or "Dependent Change")
            state = "pending"
            detail = "Dependent Change state is not yet known."
            owner = "scm" if not target_ref else "lemoncrow"
            if target_ref:
                target = store.resolve_session(target_ref)
                if target is None:
                    state, detail = "fail", "The dependent Change cannot be resolved."
                elif target.status == "archived":
                    state, detail = "fail", f"{target_ref} is archived."
                else:
                    target_landing = next(
                        (
                            row
                            for row in store.list_relationships(target.id, kind="landing")
                            if row.relation == "merge_when_ready"
                        ),
                        None,
                    )
                    target_landing_state = normalized_requirement_state(
                        "" if target_landing is None else target_landing.status
                    )
                    if target_landing_state == "pass":
                        state, detail = "pass", f"{target_ref} has a confirmed landed state."
                    elif target.status == "finished":
                        detail = f"{target_ref} review is finished, but landing is not confirmed."
                    else:
                        detail = f"{target_ref} is still open or not yet confirmed landed."
            else:
                raw_status = str(item.get("status") or "")
                state = normalized_requirement_state(raw_status)
                detail = "Connected SCM remains authoritative for this related Change."
            conditions.append(
                condition(
                    f"stack:{item.get('id')}",
                    f"Dependency {label}",
                    state=state,
                    detail=detail,
                    owner=owner,
                    category="stack",
                    url=str(item.get("url") or ""),
                )
            )

        extension_landing: Mapping[str, Any] = {}
        if readiness_extension is not None:
            extra = readiness_extension(session, revision)
            if isinstance(extra, Mapping):
                raw_landing = extra.get("landing")
                if isinstance(raw_landing, Mapping):
                    extension_landing = raw_landing
                for raw in extra.get("conditions", ()):
                    if not isinstance(raw, Mapping):
                        continue
                    conditions.append(
                        condition(
                            str(raw.get("id") or f"extension:{len(conditions)}"),
                            str(raw.get("label") or "Additional landing requirement"),
                            state=str(raw.get("state") or "pending"),
                            detail=str(raw.get("detail") or "Outer composition requirement."),
                            owner=str(raw.get("owner") or "lemoncrow"),
                            category=str(raw.get("category") or "policy"),
                            url=str(raw.get("url") or ""),
                        )
                    )

        readiness = readiness_summary(conditions)
        landing = _landing_relationship(session)
        landing_metadata = _landing_metadata(landing)
        landing_state = str(getattr(landing, "status", "") or "") if landing is not None else ""
        semantic_stale = (
            bool(landing)
            and bool(landing_metadata.get("tree_fingerprint"))
            and str(landing_metadata.get("tree_fingerprint")) != revision.tree_fingerprint
        )
        if semantic_stale:
            landing_state = "stale"
        provider = str(getattr(landing, "provider", "") or "") if landing is not None else ""
        scm_rows = context.get("scm", ())
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
        return {
            "review_id": session.id,
            "snapshot_id": revision.id,
            "snapshot_number": _display_revision_number(revision),
            "tree_fingerprint": revision.tree_fingerprint,
            "source_fingerprint": revision.source_fingerprint,
            "conditions": conditions,
            **readiness,
            "work_items": context.get("work_items", []),
            "landing": {
                "supported": supported,
                "provider": provider,
                "state": landing_state or "not_armed",
                "armed": landing_state in {"armed", "delegated", "queued"} and not semantic_stale,
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
            },
        }

    def _armed_landing_revision(session: ReviewSession, landing: Any) -> ReviewRevision:
        armed_revision_id = str(_landing_metadata(landing).get("revision_id") or "")
        armed = store.get_revision(armed_revision_id) if armed_revision_id else None
        return armed if armed is not None and armed.review_id == session.id else _latest(session)

    async def _complete_pending_landing_cancel(session: ReviewSession) -> None:
        """Cancel provider landing still owed after a semantic Snapshot change."""

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.landing import complete_pending_landing_cancel, record_landing_state

        landing = _landing_relationship(session)
        if landing is None or _landing_metadata(landing).get("provider_cancel") != "pending":
            return
        if effective_landing_delegate is None:
            record_landing_state(
                store,
                landing,
                status=landing.status,
                metadata_updates={
                    "provider_cancel": "unavailable",
                    "detail": "Semantic Snapshot changed; no connected landing delegate is available to cancel provider landing.",
                },
            )
            return
        armed_revision = _armed_landing_revision(session, landing)
        delegate = effective_landing_delegate
        await run_in_threadpool(
            complete_pending_landing_cancel,
            store,
            session.id,
            lambda: delegate(session, armed_revision, action="cancel"),
        )

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
        """A discarded Change never lands: withdraw its Merge-when-ready intent."""

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.landing import (
            ARMED_LANDING_STATES,
            cancel_settlement,
            record_landing_state,
        )

        await _complete_pending_landing_cancel(session)
        landing = _landing_relationship(session)
        if landing is None or landing.status not in ARMED_LANDING_STATES:
            return
        state, provider_detail = "cancelled", ""
        if effective_landing_delegate is not None:
            armed_revision = _armed_landing_revision(session, landing)
            try:
                result = await run_in_threadpool(effective_landing_delegate, session, armed_revision, action="cancel")
                state = str(result.get("state") or "cancelled")
                provider_detail = str(result.get("detail") or "")
            except Exception as exc:  # provider boundary: preserve the exact failure
                state, provider_detail = "failed", str(exc)
        settled = cancel_settlement(state)
        if settled in {"merged", "blocked"}:
            detail = provider_detail
        elif settled == "failed":
            detail = f"The Change was discarded, but withdrawing provider landing FAILED: {provider_detail}"
        else:
            detail = "The Change was discarded; Merge when ready was withdrawn." + (
                f" {provider_detail}" if provider_detail else ""
            )
        record_landing_state(
            store,
            landing,
            status=settled,
            metadata_updates={"cancel_state": state, "provider_state": state, "detail": detail},
        )

    async def _enforce_armed_landing(session: ReviewSession) -> None:
        """Disarm provider landing when a LemonCrow-owned condition regresses.

        Arming requires every LemonCrow-owned condition to hold for the exact
        Snapshot. A later human request-change/comment, reviewer outcome, or
        failed required verification on that same Snapshot must not be left to
        the provider's auto-merge, which cannot see LemonCrow-owned state.
        """

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.landing import (
            ARMED_LANDING_STATES,
            SATISFIED_CONDITION_STATES,
            record_landing_state,
        )

        # A capture path without the delegate may have left a provider
        # cancellation owed; any later human action on the Change settles it.
        await _complete_pending_landing_cancel(session)
        landing = _landing_relationship(session)
        if landing is None or landing.status not in ARMED_LANDING_STATES:
            return
        readiness = await run_in_threadpool(_readiness_projection, session)
        if readiness["armable"] or readiness["landing"]["semantic_stale"]:
            return
        blockers = [
            str(item["label"])
            for item in readiness["conditions"]
            if item["owner"] != "scm" and item["state"] not in SATISFIED_CONDITION_STATES
        ]
        state, provider_detail = "cancelled", ""
        if effective_landing_delegate is not None:
            armed_revision = _armed_landing_revision(session, landing)
            try:
                result = await run_in_threadpool(effective_landing_delegate, session, armed_revision, action="cancel")
                state = str(result.get("state") or "cancelled")
                provider_detail = str(result.get("detail") or "")
            except Exception as exc:  # provider boundary: preserve the exact failure
                state, provider_detail = "failed", str(exc)
        if state == "merged":
            # The provider merged the armed Snapshot before the regression; that
            # is history now, not a block.
            record_landing_state(
                store,
                landing,
                status="merged",
                metadata_updates={"cancel_state": state, "provider_state": state, "detail": provider_detail},
            )
            return
        if state == "blocked":
            # The provider had already landed something other than the armed
            # Snapshot; say exactly that instead of claiming a cancellation.
            record_landing_state(
                store,
                landing,
                status="blocked",
                metadata_updates={
                    "cancel_state": state,
                    "provider_state": state,
                    "blocked_by": blockers,
                    "detail": provider_detail or "The provider could not withdraw landing.",
                },
            )
            return
        cancelled = state != "failed"
        record_landing_state(
            store,
            landing,
            status="blocked",
            metadata_updates={
                "cancel_state": state,
                "blocked_by": blockers,
                "detail": (
                    "A LemonCrow-owned landing condition regressed after arming ("
                    + "; ".join(blockers)
                    + (
                        "). Provider landing was cancelled; resolve it and re-arm."
                        if cancelled
                        else f"). Provider landing cancellation FAILED: {provider_detail}"
                    )
                ),
            },
        )

    def _derive_targets(
        session: ReviewSession,
        revision: ReviewRevision,
        units: Sequence[ReviewUnit],
        frontier_entries: Sequence[FrontierEntry],
        packet: Mapping[str, Any] | None,
        annotations: Sequence[Annotation],
    ) -> tuple[Any, ...]:
        from lemoncrow.pro.capabilities.review.targets import derive_review_targets

        return derive_review_targets(
            units,
            frontier_entries,
            packet,
            annotations=annotations,
            evidence=_current_evidence(session, revision),
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
        groups, counts = build_groups(units, frontier.entries, packet, targets=targets)
        target_progress = review_progress(targets)
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
        degraded = sorted(set(revision.degraded) | ({"packet_unavailable"} if packet is None else set()))
        stats = packet.get("stats") if packet is not None and isinstance(packet.get("stats"), dict) else {}
        artifacts = _evidence_at(session, revision, historical=historical)
        current_artifacts = tuple(item for item in artifacts if _evidence_is_current(item, revision))
        stale_artifact_count = len(artifacts) - len(current_artifacts)
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
        intent_chapters = chapters.get("intent", [])
        story_labels, story_more = change_story_labels(intent_chapters) if len(file_rows) >= 8 else ((), 0)
        if len(intent_chapters) <= 1:
            story_labels, story_more = (), 0
        revision_state = _revision_state_payload(session)
        return {
            "session": _session_payload(session, store=store),
            "change": _change_context_payload(store, session),
            # `session.repo_root` is an opaque durable identity on server-backed
            # Review. Only a trusted local checkout binding may name a real
            # filesystem path for workspace actions (copy path, open in editor).
            "workspace_root": str(resolved_source_repo) if source_refresh_enabled else "",
            "revision": _revision_payload(revision, store=store),
            "revision_count": len(store.list_revisions(session.id)),
            "historical": historical,
            "latest_revision_number": _display_revision_number(_latest(session)),
            "latest_revision": _revision_payload(_latest(session), store=store),
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
            "title": (packet.get("title") if packet is not None else "") or session.title,
            "change_story": list(story_labels),
            "change_story_more": story_more,
            "brief": brief,
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
            "outline": [outline_payload(item) for item in target_outline],
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
        for session in sessions:
            if session.repo_root != str(resolved_repo):
                continue
            revision = store.latest_revision(session.id)
            annotations = store.list_annotations(session.id)
            payload = _session_payload(_as_acting_reviewer(session), store=store)
            payload["revision_number"] = _display_revision_number(revision) if revision is not None else None
            payload["revision_id"] = revision.id if revision is not None else ""
            payload["comment_count"] = len(annotations)
            payload["open_comment_count"] = sum(
                1 for item in annotations if item.state in {"open", "orphaned"} and not item.parent_id
            )
            rows.append(payload)
        return {"reviews": rows, "repo_root": str(resolved_repo), "status_filter": status_filter}

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
            "review": _session_payload(session, store=store),
            "revision": _revision_payload(revision, store=store),
        }

    def _journal_projection(
        session: ReviewSession,
        revision: ReviewRevision,
        *,
        include_live: bool,
    ) -> dict[str, Any]:
        """Structured observable author context, pinned to one Review revision."""

        packet = read_packet_json(store, revision)
        if not isinstance(packet, dict):
            return {
                "available": False,
                "reason": "review packet is unavailable",
                "review_id": session.id,
                "revision_id": revision.id,
                "revision_number": _display_revision_number(revision),
                "activity": [],
                "entries": [],
            }
        captured = [dict(item) for item in packet.get("journal", []) if isinstance(item, dict)]
        try:
            from lemoncrow.pro.capabilities.review.models import review_packet_from_dict

            parsed = review_packet_from_dict(packet)
            provenance = parsed.provenance
        except (TypeError, ValueError):
            parsed = None
            provenance = None
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
        if include_live and live_journal_enabled and exact and parsed is not None and provenance is not None:
            try:
                from lemoncrow.pro.capabilities.review.journal import load_review_journal

                state = load_review_journal(
                    resolved_source_store,
                    session_id=provenance.session_id or "",
                    host=provenance.host or "",
                    changed_paths=[item.path for item in parsed.files],
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
        if str(raw_context.get("review_id") or "") != session.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ask context belongs to another Change")
        if str(raw_context.get("revision_id") or "") != revision.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ask context belongs to another Snapshot")

        level = str(raw_context.get("level") or "architecture")
        if level not in {"architecture", "symbol"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid Review Map context level")
        focus_node_id = str(raw_context.get("focus_node_id") or "").strip()
        identity_key = "node_id" if kind == "review_map_node" else "edge_id"
        identity = str(raw_context.get(identity_key) or "").strip()
        if not identity:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Ask context requires {identity_key}")
        if level == "symbol" and not focus_node_id:
            if kind == "review_map_node" and str(raw_context.get("exactness") or "") == "snapshot":
                focus_node_id = identity
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="symbol-level Review Map context is missing its Snapshot focus",
                )

        latest = _latest(session)
        map_payload = await _review_map_payload(
            session,
            revision,
            historical=revision.id != latest.id,
            level=level,
            node_id=focus_node_id if level == "symbol" else "",
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
        canonical_focus = str(canonical.get("focus_node_id") or "")
        supplied_focus = str(raw_context.get("focus_node_id") or "")
        if supplied_focus and supplied_focus != canonical_focus:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Review Map selection context changed")
        for key in ("exactness", "source_id", "target_id", "relation"):
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
        answer_context = {
            "kind": kind,
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

    def _ask_messages_payload(session: ReviewSession) -> list[dict[str, Any]]:
        revision_numbers = {item.id: _display_revision_number(item) for item in store.list_revisions(session.id)}
        return [_ask_message_payload(item, revision_numbers) for item in store.list_ask_messages(session.id)]

    @router.get("/reviews/{review_id}/ask")
    async def _get_review_ask(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        latest = _latest(session)
        from lemoncrow.core.capabilities.providers.config import load_providers_config
        from lemoncrow.core.capabilities.providers.discovery import _fetch_ollama, discover_models
        from lemoncrow.pro.capabilities.review.ask import discover_opencode_models

        configured_backend = os.environ.get("LEMONCROW_LLM_BACKEND", "none").strip().lower()
        if configured_backend == "openai_compatible":
            configured_backend = "openai"
        driver = configured_backend if configured_backend in {"ollama", "openai", "litellm"} else "configured"
        if driver == "ollama":
            configured_model = os.environ.get("LEMONCROW_OLLAMA_MODEL", "").strip()
        elif driver == "openai":
            configured_model = os.environ.get("LEMONCROW_OPENAI_MODEL", "").strip()
        elif driver == "litellm":
            configured_model = os.environ.get("LEMONCROW_LITELLM_MODEL", "").strip()
        else:
            configured_model = ""
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
        try:
            opencode_models = await asyncio.to_thread(discover_opencode_models)
        except Exception:
            opencode_models = []
        for item in local_ollama_models:
            if item not in opencode_models:
                opencode_models.append(item)
        if configured_model and configured_model not in discovered_models:
            discovered_models.insert(0, configured_model)
        return {
            "review_id": session.id,
            "latest_revision_number": _display_revision_number(latest),
            "messages": _ask_messages_payload(session),
            "ai": {
                "driver": driver,
                "model": configured_model,
                "models": discovered_models,
                "models_by_driver": {
                    "claude": ["sonnet", "opus", "haiku"],
                    "codex": [],
                    "opencode": opencode_models,
                    "ollama": [item for item in discovered_models if item.startswith("ollama/")],
                    "openai": [configured_model] if driver == "openai" and configured_model else [],
                    "litellm": discovered_models,
                },
            },
        }

    @router.post("/reviews/{review_id}/ask", status_code=status.HTTP_201_CREATED)
    async def _post_review_ask(review_id: str, request: Request) -> dict[str, Any]:
        """Answer one read-only Review question against an exact immutable revision."""

        from lemoncrow.pro.capabilities.review.ask import (
            AskContextSelection,
            answer_ask,
            build_ask_context,
            wants_previous_snapshot_comparison,
        )
        from lemoncrow.pro.capabilities.review.session_models import ReviewAskMessage

        # A discarded Review is read-only: Ask appends durable messages.
        session = _mutable_session(review_id)
        body = await _json_object(request)
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
        try:
            revision_number = int(cast(Any, body.get("revision_number")))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="revision_number is required",
            ) from None
        revision = _revision_number(session, revision_number)
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
        if path and not persisted_context and path not in _revision_packet_paths(revision):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ask source must belong to this Review revision",
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
            if not symbol:
                symbol = unit.symbol
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
        previous = store.list_ask_messages(session.id)
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
            if ask_code_intelligence is not None and hasattr(ask_code_intelligence, "semantic_search"):
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
        context = build_ask_context(
            store=store,
            revision=revision,
            packet=packet,
            journal=journal,
            evidence=evidence_rows,
            previous_messages=previous,
            question=question,
            comparison=comparison_context,
            selection=selection,
            code_intelligence_readiness=code_intelligence_readiness,
            semantic_retrieval=semantic_retrieval,
        )

        call = ask_answerer or answer_ask
        try:
            if ask_answerer is None and backend in {"claude", "codex", "opencode"}:
                from lemoncrow.pro.capabilities.review.snapshot import materialize_review_side

                snapshot_cache = store.root / "cache" / "review-ask-snapshots" / session.id / revision.id
                snapshot_root = snapshot_cache / "repo"
                marker = snapshot_cache / "identity"
                snapshot_identity = revision.source_fingerprint or revision.tree_fingerprint or revision.id
                lock = ask_snapshot_locks.setdefault(revision.id, asyncio.Lock())
                async with lock:
                    marker_identity = ""
                    with contextlib.suppress(OSError):
                        marker_identity = marker.read_text(encoding="utf-8").strip()
                    if not snapshot_root.is_dir() or marker_identity != snapshot_identity:
                        await asyncio.to_thread(
                            materialize_review_side,
                            resolved_source_store,
                            resolved_source_repo,
                            revision,
                            side="new",
                            target=snapshot_root,
                            store=store,
                        )
                        marker.parent.mkdir(parents=True, exist_ok=True)
                        marker.write_text(snapshot_identity + "\n", encoding="utf-8")
                    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

                    snapshot_engine = CodeContextEngine(snapshot_root, autosync_enabled=False)
                    if not snapshot_engine.index_ready():
                        await asyncio.to_thread(
                            snapshot_engine.index_repo,
                            force=False,
                            require_lock=True,
                        )
                    result = await asyncio.to_thread(
                        answer_ask,
                        context,
                        backend=backend,
                        model=model or None,
                        project_root=snapshot_root,
                    )
            elif ask_answerer is None:
                result = await asyncio.to_thread(
                    answer_ask,
                    context,
                    backend=backend or None,
                    model=model or None,
                )
            else:
                result = await asyncio.to_thread(call, context)
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
        return {
            "review_id": session.id,
            "latest_revision_number": _display_revision_number(_latest(session)),
            "human": _ask_message_payload(human, revision_numbers),
            "assistant": _ask_message_payload(assistant, revision_numbers),
        }

    @router.post("/reviews/{review_id}/ask/stream")
    async def _post_review_ask_stream(review_id: str, request: Request) -> StreamingResponse:
        """Stream Review Ask lifecycle events while preserving the existing durable POST semantics."""

        raw_body = await request.body()
        try:
            decoded = json.loads(raw_body or b"{}")
        except Exception:
            decoded = {}
        driver = str(decoded.get("driver") or "configured") if isinstance(decoded, Mapping) else "configured"
        model = str(decoded.get("model") or "") if isinstance(decoded, Mapping) else ""
        scope = dict(request.scope)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": raw_body, "more_body": False}

        replayed = Request(scope, receive)

        async def events() -> AsyncIterator[bytes]:
            def line(payload: Mapping[str, Any]) -> bytes:
                return (json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

            started = asyncio.get_running_loop().time()
            yield line({"type": "accepted", "driver": driver, "model": model})
            task = asyncio.create_task(_post_review_ask(review_id, replayed))
            while not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                except TimeoutError:
                    elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
                    yield line({"type": "working", "elapsed_ms": elapsed_ms, "driver": driver, "model": model})
            try:
                result = task.result()
            except HTTPException as exc:
                yield line({"type": "error", "status": exc.status_code, "detail": str(exc.detail)})
                return
            except Exception as exc:
                yield line({"type": "error", "status": 500, "detail": f"Review Ask failed: {exc}"})
                return
            yield line({"type": "result", "result": result})

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @router.post("/reviews/{review_id}/ask/{message_id}/handoff")
    async def _post_review_ask_handoff(review_id: str, message_id: str, request: Request) -> dict[str, Any]:
        """Send one explicit chat-requested code change to the exact author session."""

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
        return development_view(
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
    ) -> dict[str, Any]:
        if level not in {"architecture", "symbol"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="level must be 'architecture' or 'symbol'",
            )
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

        return await asyncio.to_thread(
            build_review_map,
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

    @router.get("/reviews/{review_id}/map")
    async def _get_review_map(
        review_id: str,
        level: str = "architecture",
        node_id: str = "",
    ) -> dict[str, Any]:
        session = _session(review_id)
        return await _review_map_payload(
            session,
            _latest(session),
            historical=False,
            level=level,
            node_id=node_id,
        )

    @router.get("/reviews/{review_id}/revisions/{revision_number}/map")
    async def _get_historical_review_map(
        review_id: str,
        revision_number: int,
        level: str = "architecture",
        node_id: str = "",
    ) -> dict[str, Any]:
        session = _session(review_id)
        return await _review_map_payload(
            session,
            _revision_number(session, revision_number),
            historical=True,
            level=level,
            node_id=node_id,
        )

    @router.get("/reviews/{review_id}")
    def _get_review(review_id: str) -> dict[str, Any]:
        return _overview(_session(review_id))

    @router.get("/reviews/{review_id}/revisions/{revision_number}")
    def _get_historical_review(review_id: str, revision_number: int) -> dict[str, Any]:
        session = _session(review_id)
        revision = _revision_number(session, revision_number)
        return _overview(session, revision, historical=True)

    @router.get("/reviews/{review_id}/relationships")
    def _get_relationships(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        return _change_context_payload(store, session)

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
        if stored.verification_status:
            await _enforce_armed_landing(session)
        return {
            "browser_check": record,
            "evidence": _review_evidence_payload(
                stored,
                current=revision.id == _latest(session).id,
                revision_number=_display_revision_number(revision),
            ),
            "verification": await asyncio.to_thread(_verification_projection, session, revision),
        }

    @router.get("/reviews/{review_id}/dependencies")
    def _get_review_dependencies(review_id: str, revision_id: str = "") -> dict[str, Any]:
        session = _session(review_id)
        revision = _requested_revision(session, revision_id)
        return _dependency_projection(session, revision)

    @router.get("/reviews/{review_id}/readiness")
    def _get_review_readiness(review_id: str) -> dict[str, Any]:
        return _readiness_projection(_session(review_id))

    @router.post("/reviews/{review_id}/landing/arm")
    async def _post_landing_arm(review_id: str, request: Request) -> dict[str, Any]:
        """Explicitly arm provider-native landing for the exact visible conditions."""

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.landing import SATISFIED_CONDITION_STATES
        from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

        session = _session(review_id)
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
        current = _readiness_projection(session)
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
        }
        record = store.upsert_relationship(
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
            )
        )
        try:
            result = await run_in_threadpool(effective_landing_delegate, session, revision, action="arm")
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
        if latest_after_delegate.tree_fingerprint != revision.tree_fingerprint:
            try:
                await run_in_threadpool(effective_landing_delegate, session, revision, action="cancel")
            except Exception:
                pass
            delegated_state = "stale"
            result = {
                **dict(result),
                "state": "stale",
                "detail": "The semantic Snapshot changed while landing was being armed; provider landing was cancelled and must be re-armed after review.",
            }
        metadata.update(
            {
                "detail": str(result.get("detail") or ""),
                "mechanism": str(result.get("mechanism") or ""),
                "provider_state": delegated_state,
            }
        )
        store.upsert_relationship(
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
            )
        )
        return _readiness_projection(session)

    @router.post("/reviews/{review_id}/landing/cancel")
    async def _post_landing_cancel(review_id: str, request: Request) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.landing import cancel_settlement
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
        metadata = _landing_metadata(landing)
        result: Mapping[str, Any] = {"state": "cancelled", "detail": "Landing intent cancelled."}
        if effective_landing_delegate is not None:
            try:
                result = await run_in_threadpool(effective_landing_delegate, session, revision, action="cancel")
            except Exception as exc:
                result = {"state": "failed", "detail": str(exc)}
        cancel_state = str(result.get("state") or "cancelled")
        metadata.update(
            {
                "detail": str(result.get("detail") or ""),
                "provider_state": cancel_state,
            }
        )
        store.upsert_relationship(
            ReviewRelationship(
                id="",
                review_id=session.id,
                kind="landing",
                relation="merge_when_ready",
                provider=landing.provider,
                external_id=landing.external_id,
                url=landing.url,
                title=landing.title or "Merge when ready",
                # A merge the provider already performed cannot be cancelled,
                # and a merge of an unreviewed head is never shown as one.
                status=cancel_settlement(cancel_state),
                source="human",
                metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            )
        )
        return _readiness_projection(session)

    @router.get("/reviews/{review_id}/revisions")
    def _get_revisions(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        revisions = store.list_revisions(session.id)
        state = _revision_state_payload(session)
        reviewed = state["last_reviewed_revision_number"]
        commented = state["last_commented_revision_number"]
        latest = state["latest_revision_number"]

        from lemoncrow.pro.capabilities.review.targets import derive_review_targets

        target_cache: dict[str, tuple[Any, ...]] = {}
        unit_cache: dict[str, dict[str, ReviewUnit]] = {}

        def history_targets(item: ReviewRevision) -> tuple[Any, ...]:
            cached = target_cache.get(item.id)
            if cached is not None:
                return cached
            units = store.list_units(item.id)
            unit_cache[item.id] = {unit.unit_key: unit for unit in units}
            targets = tuple(derive_review_targets(units, (), read_packet_json(store, item)))
            target_cache[item.id] = targets
            return targets

        def history_target_delta(previous: ReviewRevision | None, current: ReviewRevision) -> dict[str, int]:
            current_targets = history_targets(current)
            if previous is None:
                return {"added": len(current_targets), "changed": 0, "removed": 0}

            previous_targets = history_targets(previous)
            previous_by_key = {target.unit_key: target for target in previous_targets}
            current_by_key = {target.unit_key: target for target in current_targets}
            previous_units = unit_cache[previous.id]
            current_units = unit_cache[current.id]
            added = len(current_by_key.keys() - previous_by_key.keys())
            removed = len(previous_by_key.keys() - current_by_key.keys())
            changed = 0
            for key in previous_by_key.keys() & current_by_key.keys():
                before = previous_units.get(key)
                after = current_units.get(key)
                if before is None or after is None:
                    changed += 1
                    continue
                known_identity = before.fingerprint_method != "unknown" and after.fingerprint_method != "unknown"
                if not known_identity or before.content_fingerprint != after.content_fingerprint:
                    changed += 1
            return {"added": added, "changed": changed, "removed": removed}

        def history_row(index: int, item: ReviewRevision) -> dict[str, Any]:
            payload = _revision_payload(item, store=store)
            number = payload["revision_number"]
            payload["is_latest"] = number == latest
            payload["is_last_reviewed"] = number == reviewed
            payload["is_last_commented"] = number == commented
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
            payload["review_area_count"] = len(history_targets(item))
            payload["review_area_delta"] = history_target_delta(previous, item)

            if previous is None:
                payload["delta"] = {
                    "added": len(store.list_units(item.id)),
                    "changed": 0,
                    "removed": 0,
                }
            else:
                delta = compare_revision_units(
                    store.list_units(previous.id),
                    store.list_units(item.id),
                )
                payload["delta"] = {
                    "added": delta.added,
                    "changed": delta.changed,
                    "removed": delta.removed,
                }
            return payload

        return {
            "revisions": [history_row(index, item) for index, item in enumerate(revisions)],
            **state,
        }

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
        from lemoncrow.pro.capabilities.review.sources.local import range_for_session

        try:
            current = source_state(resolved_source_repo, range_for_session(resolved_source_repo, session))
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
            "changed": current.fingerprint != revision.source_fingerprint,
            "fingerprint": current.fingerprint,
            "path_count": len(current.paths),
            "paths": list(current.paths),
            "reason": "",
        }

    @router.get("/reviews/{review_id}/source-state")
    def _get_source_state(review_id: str) -> dict[str, Any]:
        """Cheap, read-only detection that a local review has newer source.

        This route never snapshots or reconciles. A mismatch is only an offer
        to the human; ``POST /refresh`` remains the one door that advances the
        ReviewRevision and reopens marks/comments.
        """

        session = _session(review_id)
        return _source_state_projection(session, _latest(session))

    def _review_live_state(review_id: str) -> dict[str, Any]:
        """One lightweight cursor spanning mutable Reader context.

        The cursor intentionally fingerprints metadata, never source/comment
        contents. Existing exact endpoints remain authoritative and are fetched
        only when their component cursor advances.
        """

        session = _session(review_id)
        revision = _latest(session)
        annotations = _ordered_annotations(store.list_annotations(session.id))
        annotation_rows = [
            (
                item.id,
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
        outline = build_review_outline(targets)
        return {
            "revision_id": revision.id,
            "historical": historical,
            "order": order,
            "targets": [target_payload(target) for target in targets],
            "progress": progress_payload(progress),
            "outline": [outline_payload(item) for item in outline],
        }

    @router.get("/reviews/{review_id}/targets")
    def _get_targets(review_id: str, order: str = "recommended") -> dict[str, Any]:
        """Reader metadata: one non-overlapping judgment target per changed span."""

        session = _session(review_id)
        return _targets_payload(session, _latest(session), order=order, historical=False)

    @router.get("/reviews/{review_id}/revisions/{revision_number}/targets")
    def _get_historical_targets(review_id: str, revision_number: int, order: str = "recommended") -> dict[str, Any]:
        session = _session(review_id)
        return _targets_payload(
            session,
            _revision_number(session, revision_number),
            order=order,
            historical=True,
        )

    @router.get("/reviews/{review_id}/frontier")
    def _get_frontier(review_id: str) -> dict[str, Any]:
        """Current reviewer-owned "since my review" projection.

        The baseline is the durable reviewer frontier, never the latest captured
        revision. ``new`` is further filtered by attested content fingerprints,
        so an undo that restores code the reviewer already saw is not presented
        as brand-new work.
        """

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
            "notes": [],
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
        from lemoncrow.pro.capabilities.review.snapshot import materialize_review_side
        from lemoncrow.pro.capabilities.review.surfaces import (
            SurfaceContext,
            built_in_registry,
            load_review_surface_config,
        )

        surface_repo = resolved_repo
        source_state_projection = _source_state_projection(session, revision)
        latest_revision = _latest(session)
        durable_source_tree = store.read_source_tree_artifact(session.id, revision.id, side=side)
        checkout_is_exact = (
            side == "new"
            and revision.id == latest_revision.id
            and bool(source_state_projection.get("supported"))
            and not bool(source_state_projection.get("changed"))
        )
        if durable_source_tree is not None or not checkout_is_exact:
            from lemoncrow.core.foundation.run_file_io import RunFileLock
            from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable

            scratch_root = surface_workspace_root or (store.root / "review" / "surface-context")
            side_root = scratch_root / session.id / revision.id / side
            surface_repo = side_root / "workspace"
            materialized = side_root / "materialized"
            # A Snapshot side is immutable: materialize it once and share it.
            # Re-materializing (rmtree + write) per request deleted the tree a
            # concurrent surface run was executing from, and one directory for
            # both sides let the old side overwrite the new one.
            # lc-debt: Snapshot surface sources are never pruned; add an idle-entry closer like web-preview builds.
            with RunFileLock(side_root / "materialize"):
                if not materialized.is_file():
                    try:
                        materialize_review_side(
                            store.root,
                            resolved_preview_repo,
                            revision,
                            side=side,
                            target=surface_repo,
                            store=store,
                        )
                    except ReviewSnapshotUnavailable as exc:
                        raise RuntimeError(
                            f"exact Snapshot {side}-side surface source is unavailable for {_display_revision_number(revision)}: {exc}"
                        ) from exc
                    materialized.touch()
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
        result = await run_in_threadpool(
            previews_module.render_surface_preview,
            context,
            surface,
            side=side,
            renderers=previews_module.built_in_preview_registry(),
            source_repo=resolved_repo,
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
                "Content-Security-Policy": (
                    "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                ),
                "Content-Disposition": f'{disposition}; filename="{result.filename}"',
                "X-LemonCrow-Revision-Id": revision.id,
                "X-LemonCrow-Preview-Side": side,
                "X-LemonCrow-Preview-Renderer": result.renderer,
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
                    renderers = previews_module.built_in_preview_registry()
                    old_preview, new_preview = await asyncio.gather(
                        run_in_threadpool(
                            previews_module.render_surface_preview,
                            new_context,
                            surface,
                            side="old",
                            renderers=renderers,
                            source_repo=resolved_repo,
                        ),
                        run_in_threadpool(
                            previews_module.render_surface_preview,
                            new_context,
                            surface,
                            side="new",
                            renderers=renderers,
                            source_repo=resolved_repo,
                        ),
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
                    old_result, new_result = await asyncio.gather(
                        run_in_threadpool(old_runners.run, old_context, old_surface, side="old"),
                        run_in_threadpool(new_runners.run, new_context, surface, side="new"),
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
        }
        if len(surface_impact_cache) >= 1024:
            surface_impact_cache.pop(next(iter(surface_impact_cache)))
        surface_impact_cache[cache_key] = payload
        return dict(payload)

    @router.get("/reviews/{review_id}/surface-response-diff")
    async def _surface_response_diff(
        review_id: str,
        provider: str,
        surface_id: str,
        revision_id: str = "",
    ) -> dict[str, Any]:
        """Compare a safe affected API request across the immutable old/new sides.

        This powers ordering in the API picker. It deliberately refuses mutation
        methods and unresolved request parameters: ranking must never create
        application state or guess fixture values merely because the Reader was
        opened.
        """

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.runtimes import api_response_diff_score

        try:
            revision, _new_registry, new_runners, new_context, new_surfaces, _new_issues = await run_in_threadpool(
                _discovered_side, review_id, revision_id, "new"
            )
            new_surface = next(
                (item for item in new_surfaces if item.provider == provider and item.id == surface_id),
                None,
            )
            if new_surface is None:
                raise ValueError(f"review surface not found: {provider}:{surface_id}")
            if new_surface.kind != "api.request":
                raise ValueError("response diff ranking currently supports API request surfaces only")
            cache_key = (review_id, revision.id, provider, surface_id)
            cached = surface_response_diff_cache.get(cache_key)
            if cached is not None:
                return dict(cached)

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
                raise RuntimeError("old Snapshot side has no matching API request to compare")

            for side_name, surface in (("old", old_surface), ("new", new_surface)):
                method = str(surface.metadata.get("method") or "").upper()
                if method not in {"GET", "HEAD", "OPTIONS"}:
                    raise ValueError(
                        f"automatic response diff refuses mutation method {method or 'unknown'} on the {side_name} side"
                    )
                raw_url = str(surface.metadata.get("url") or surface.locator or "").replace("{{baseUrl}}", "")
                if "{{" in raw_url or re.search(r"/:([^/?]+)", raw_url):
                    raise ValueError(f"automatic response diff requires a concrete request URL on the {side_name} side")

            old_result, new_result = await asyncio.gather(
                run_in_threadpool(old_runners.run, old_context, old_surface, side="old"),
                run_in_threadpool(new_runners.run, new_context, new_surface, side="new"),
            )
            score = api_response_diff_score(old_result, new_result)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except (OSError, RuntimeError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"API response diff unavailable: {exc}"
            ) from exc

        payload = {
            "revision_id": revision.id,
            "provider": provider,
            "surface_id": surface_id,
            **score.to_payload(),
        }
        if len(surface_response_diff_cache) >= 512:
            surface_response_diff_cache.pop(next(iter(surface_response_diff_cache)))
        surface_response_diff_cache[cache_key] = payload
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
            result = runners.run(context, surface, side=side)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except (OSError, RuntimeError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"review surface execution failed: {exc}"
            ) from exc
        return {
            "revision_id": revision.id,
            "result": {**result.to_payload(), "revision_id": revision.id},
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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="web preview is unavailable for this route"
            )

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.web_preview import WebPreviewUnavailable, render_web_preview

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
        return Response(content=payload, media_type="image/png")

    @router.get("/reviews/{review_id}/web-preview-diff")
    async def _get_web_preview_diff(
        review_id: str,
        document_path: str,
        route: str,
        revision_id: str = "",
        width: int = 1440,
        height: int = 5000,
    ) -> dict[str, Any]:
        """Rank one affected route by deterministic old/new screenshot delta."""

        if width < 320 or width > 2560 or height < 480 or height > 8000:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unsupported preview viewport")
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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="web preview is unavailable for this affected route"
            )

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.web_preview import WebPreviewUnavailable, rank_web_preview_difference

        try:
            score = await run_in_threadpool(
                rank_web_preview_difference,
                store.root,
                resolved_preview_repo,
                revision,
                path=document_path,
                route=route,
                width=width,
                height=height,
                store=store,
                dependency_root=resolved_dependency_repo,
            )
        except WebPreviewUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return {
            "revision_id": revision.id,
            "route": route,
            **score.to_payload(),
        }

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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="web preview is unavailable for this route"
            )

        from starlette.concurrency import run_in_threadpool

        from lemoncrow.pro.capabilities.review.web_preview import WebPreviewUnavailable, ensure_live_web_preview

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
            from lemoncrow.core.capabilities.web_fetch import async_fetch_image

            try:
                remote_body, media_type = await async_fetch_image(src)
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
            return Response(content=remote_body, media_type=media_type)

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
        return Response(content=body, media_type=media_type)

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
        return Response(content=body, media_type=preview["media_type"])

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
    async def _post_mark(review_id: str, request: Request) -> dict[str, Any]:
        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="recording this mark")
        target = str(body.get("unit_key") or body.get("path") or "")
        if not target:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unit_key is required")
        try:
            requested = coerce_mark_state(str(body.get("state") or "reviewed"))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        from lemoncrow.pro.capabilities.review.targets import target_payload

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
                detail=f"no reviewable unit matches {target!r} in Snapshot {_display_revision_number(revision)}",
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
            "groups": overview["groups"],
            "group_counts": overview["group_counts"],
        }
        if resolution.shadowed:
            # A file the repository really contains outranked a target label
            # spelled the same way. Never silent: one word asked two questions
            # and this names the reading that lost, so a caller who meant the
            # other one can send its unit key instead.
            payload["shadowed"] = [{"unit_key": item.unit_key, "path": item.path} for item in resolution.shadowed]
        await _enforce_armed_landing(session)
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
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="recording these marks")
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
                if target.attention_level == "high":
                    skipped.append(
                        {
                            "unit_key": key,
                            "path": target.path,
                            "reason": "still needs individual attention",
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
                marked.append({"unit_key": key, "path": unit.path})
                touched_paths.add(unit.path)

        after = _overview(session)
        await _enforce_armed_landing(session)
        return {
            "marked": marked,
            "skipped": skipped,
            "progress": after["progress"],
            "outline_updates": [item for item in after["outline"] if item.get("path") in touched_paths],
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
        if path and not any(unit.kind == "file" and unit.path == path for unit in store.list_units(revision.id)):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="verification path is not in this Snapshot"
            )
        try:
            record = store.add_evidence(
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
                ),
                # If no explicit immutable Snapshot was supplied, preserve the
                # old latest-Snapshot race guard. CI callers should always name
                # the Snapshot they actually ran against and may finish later.
                expected_current_revision_id=revision.id if not requested_revision_id else "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        await _enforce_armed_landing(session)
        return {
            "verification": _verification_projection(session, revision),
            "evidence": _review_evidence_payload(
                record,
                current=revision.id == _latest(session).id,
                revision_number=_display_revision_number(revision),
            ),
        }

    @router.post("/reviews/{review_id}/verification/waivers", status_code=status.HTTP_201_CREATED)
    async def _post_review_verification_waiver(review_id: str, request: Request) -> dict[str, Any]:
        """Record a human's reasoned override of one blocking check on the latest Snapshot.

        The waiver binds to that exact Snapshot, never changes the observed
        status and is shown as waived, not passed. Only a check that blocks
        without a current failure (a carried non-PASS, or a current NOT_RUN or
        UNKNOWN) can be waived.
        """

        session = _mutable_session(review_id)
        body = await _json_object(request)
        revision_id = str(body.get("revision_id") or "").strip()
        name = str(body.get("name") or "").strip()
        reason = str(body.get("reason") or "").strip()
        if not revision_id or not name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="revision_id and check name are required"
            )
        if not reason:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="a waiver needs a reason")
        revision = _requested_revision(session, revision_id)
        if revision.id != _latest(session).id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="a waiver applies only to the latest Snapshot; reload and review it first",
            )
        projection = await asyncio.to_thread(_verification_projection, session, revision)
        row = next(
            (item for item in projection.get("checks", ()) if item.get("name") == name and item.get("waivable")),
            None,
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{name!r} is not a blocking check without a current failure on this Snapshot",
            )
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
    ) -> dict[str, Any]:
        """Attach one artifact to the exact immutable Snapshot it describes."""

        session = _mutable_session(review_id)
        explicit_revision_id = revision_id.strip()
        requested_revision_id = explicit_revision_id or expected_revision_id.strip()
        revision = _requested_revision(session, requested_revision_id)
        if expected_revision_id and expected_revision_id != revision.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="expected_revision_id does not match the requested Snapshot",
            )
        if kind not in REVIEW_EVIDENCE_KINDS or kind in {"live_preview", "verification", "waiver"}:
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
        return Response(content=payload, media_type=_safe_media_type(evidence.mime_type))

    @router.delete("/evidence/{evidence_id}")
    async def _delete_review_evidence(evidence_id: str) -> dict[str, Any]:
        evidence = store.get_evidence(evidence_id)
        if evidence is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such review evidence")
        _mutable_session(evidence.review_id)
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
        store.delete_evidence(evidence_id)
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

    @router.get("/reviews/{review_id}/annotations")
    def _get_annotations(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        revision = _latest(session)
        annotations = _ordered_annotations(store.list_annotations(session.id))
        feedback_status, unpublished_ids, _ = _feedback_partition(
            revision.id,
            annotations,
            source_ref=session.source_ref,
        )
        delivery = _delivery_capability(
            revision,
            annotation_ids=tuple(item.id for item in annotations if item.id in unpublished_ids),
            annotations_by_id={item.id: item for item in annotations},
        )
        return {
            "revision_id": revision.id,
            "revision_number": _display_revision_number(revision),
            "historical": False,
            "annotations": [_annotation_context_payload(session, revision, item) for item in annotations],
            "counts": annotation_counts(annotations),
            "feedback": feedback_status,
            "delivery": delivery,
        }

    @router.get("/reviews/{review_id}/revisions/{revision_number}/annotations")
    def _get_historical_annotations(review_id: str, revision_number: int) -> dict[str, Any]:
        session = _session(review_id)
        revision = _revision_number(session, revision_number)
        annotations = _ordered_annotations(store.list_annotations_at_revision(session.id, revision.id))
        return {
            "revision_id": revision.id,
            "revision_number": _display_revision_number(revision),
            "historical": True,
            "annotations": [_annotation_context_payload(session, revision, item) for item in annotations],
            "counts": annotation_counts(annotations),
        }

    @router.post("/reviews/{review_id}/annotations", status_code=status.HTTP_201_CREATED)
    async def _post_annotation(review_id: str, request: Request) -> dict[str, Any]:
        """Create a durable annotation and optionally block its ReviewTarget.

        Anchor identity is always computed from the frozen revision, never from
        a browser line number. R24 additionally accepts an explicit current
        ``target_unit_key`` for a root human ``request_change``. When the client
        opts into ``mark_target``, that key is validated against the derived
        ReviewTarget projection *before* the comment is written; after the
        annotation lands, the same request records ``needs_changes`` on that
        target and returns the updated reader projection.
        """

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
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
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="review target unit is unavailable")

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
            code = status.HTTP_409_CONFLICT if "Review advanced" in message else status.HTTP_422_UNPROCESSABLE_ENTITY
            raise HTTPException(status_code=code, detail=message) from exc

        marked_target = None
        progress = None
        outline_updates: list[dict[str, Any]] = []
        if target_unit is not None:
            overview = _overview(session)
            progress = overview["progress"]
            outline_updates = [item for item in overview["outline"] if item.get("path") == target_unit.path]

            from lemoncrow.pro.capabilities.review.targets import target_payload

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

        await _enforce_armed_landing(session)
        return {
            "annotation": _annotation_payload(annotation),
            "target": marked_target,
            "progress": progress,
            "outline_updates": outline_updates,
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
        revision = _latest(session)

        body = await _json_object(request)
        if "state" in body:
            _require_expected_revision(body, revision, action="changing this comment state")
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
        if not fields:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="nothing to change")

        request_changed = ("body" in fields and fields["body"] != annotation.body) or (
            "kind" in fields and fields["kind"] != annotation.kind
        )
        if request_changed and annotation.author_response != "none":
            # An author response belongs to the request version the author saw.
            # Editing the request cannot carry that claim forward.
            fields["author_response"] = "none"
            fields["author_response_source_id"] = ""
            fields["author_response_at"] = ""

        from lemoncrow.pro.capabilities.review.store import AnnotationHistoryContext

        try:
            if "state" in fields:
                with store.atomic_review_transition():
                    store.require_current_revision(session.id, revision.id)
                    updated = store.update_annotation(
                        annotation_id,
                        AnnotationHistoryContext(
                            revision_id=revision.id,
                            changed_by=session.reviewer_id,
                            changed_by_actor="human",
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
        await _enforce_armed_landing(session)
        return {"annotation": _annotation_payload(updated)}

    def _feedback_snapshot(session: ReviewSession, revision: ReviewRevision) -> dict[str, Any]:
        packet = read_packet_json(store, revision)
        annotations = store.list_annotations(session.id)
        full_bundle = build_bundle(
            session,
            revision,
            annotations,
            store.list_units(revision.id),
            context=_feedback_context(packet),
            title=(packet.get("title") if packet is not None else "") or session.title,
        )
        feedback_status, unpublished_ids, versions = _feedback_partition(
            revision.id, annotations, source_ref=session.source_ref
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
        delivery = _delivery_capability(
            revision,
            annotation_ids=snapshot["annotation_ids"],
            annotations_by_id=snapshot["annotations_by_id"],
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
        """

        session = _mutable_session(review_id)
        revision = _latest(session)
        raw_body = await request.body()
        body = await _json_object(request) if raw_body.strip() else {}
        operation_id = str(body.get("operation_id") or "").strip()
        expected_revision_id = str(body.get("expected_revision_id") or "").strip()
        expected_feedback_hash = str(body.get("expected_feedback_hash") or "").strip()
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

        from lemoncrow.pro.capabilities.review.delivery import deliver_to_agent_session
        from lemoncrow.pro.capabilities.review.session_models import DeliveryRecord

        routes: dict[str, dict[str, str]] = {}
        ids_by_target: dict[str, list[str]] = {}
        if existing:
            operation = existing
            annotation_ids = tuple(dict.fromkeys(item.annotation_id for item in existing))
            for item in existing:
                host, separator, session_id = item.target_ref.partition(":")
                if not separator or not host or not session_id:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="stored delivery target is malformed",
                    )
                routes[item.annotation_id] = {
                    "host": host,
                    "session_id": session_id,
                    "target_ref": item.target_ref,
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
            capability = _delivery_capability(
                revision,
                annotation_ids=annotation_ids,
                annotations_by_id=snapshot["annotations_by_id"],
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
            pending = tuple(
                DeliveryRecord(
                    id="",
                    annotation_id=annotation_id,
                    target_type="agent_session",
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
            result = deliver_to_agent_session(
                str(route["host"]),
                str(route["session_id"]),
                resolved_repo,
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
            if result.accepted:
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
                    "target_ref": result.target_ref or target_ref,
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
        from lemoncrow.pro.capabilities.review.sources.github import parse_pr_source_ref

        pr = parse_pr_source_ref(session.source_ref)
        if pr is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this Review was not opened from a GitHub pull request; open it with `lc review --pr`",
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

        from lemoncrow.pro.capabilities.review.github_publish import publish_plan
        from lemoncrow.pro.capabilities.review.session_models import DeliveryRecord
        from lemoncrow.pro.capabilities.review.sources import github as github_source

        session = _mutable_session(review_id)
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

        pending = tuple(
            DeliveryRecord(
                id="",
                annotation_id=annotation_id,
                target_type="github_review",
                target_ref=session.source_ref,
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
        verification_raw = brief.get("verification")
        verification: Mapping[str, Any] = verification_raw if isinstance(verification_raw, Mapping) else {}
        artifacts_raw = brief.get("artifacts")
        artifacts: Mapping[str, Any] = artifacts_raw if isinstance(artifacts_raw, Mapping) else {}
        return {
            "review_id": closure.review_id,
            "status": closure.status,
            "target_count": closure.target_count,
            "reviewed_targets": closure.reviewed_targets,
            "unreviewed_targets": closure.unreviewed_targets,
            "changed_since_review": closure.changed_since_review,
            "needs_changes": closure.needs_changes,
            "unknown_targets": closure.unknown_targets,
            "open_comments": closure.open_comments,
            "orphaned_comments": closure.orphaned_comments,
            "failed_verification": int(verification.get("fail") or 0),
            "unresolved_verification": int(verification.get("not_run") or 0) + int(verification.get("unknown") or 0),
            "previous_revision_evidence": int(artifacts.get("stale") or 0),
            "discarded_verdicts": len(overview.get("discarded") or ()),
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

    @router.get("/reviews/{review_id}/outcome")
    def _get_outcome(review_id: str) -> dict[str, Any]:
        return _outcome_payload(_session(review_id))

    @router.post("/reviews/{review_id}/outcome")
    async def _post_outcome(review_id: str, request: Request) -> dict[str, Any]:
        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        _require_expected_revision(body, revision, action="recording this outcome")
        outcome = str(body.get("outcome") or "").strip().lower()
        if outcome not in REVIEW_OUTCOME_KINDS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="outcome must be comment, lgtm, or changes_requested",
            )
        try:
            store.record_outcome(
                session.id,
                revision.id,
                session.reviewer_id,
                outcome=outcome,  # type: ignore[arg-type]
                summary=str(body.get("summary") or ""),
                expected_current_revision_id=revision.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        await _enforce_armed_landing(session)
        return _outcome_payload(session)

    @router.get("/reviews/{review_id}/finish")
    def _get_finish_preview(review_id: str) -> dict[str, Any]:
        """Preview the exact finish sheet without changing review status."""

        session = _session(review_id)
        return _finish_payload(session, session.status)

    @router.post("/reviews/{review_id}/finish")
    async def _post_finish(review_id: str, request: Request) -> dict[str, Any]:
        """Record a human status choice; never infer or issue approval."""

        session = _session(review_id)
        revision = _latest(session)
        body: dict[str, Any] = {}
        if (await request.body()).strip():
            body = await _json_object(request)
        wanted = str(body.get("status") or "finished").strip().lower()
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
        return payload

    @router.post("/reviews/{review_id}/refresh")
    async def _post_refresh(review_id: str) -> dict[str, Any]:
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

        await _invalidate_landing_after_semantic_change(session, previous_latest, result.revision)

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
