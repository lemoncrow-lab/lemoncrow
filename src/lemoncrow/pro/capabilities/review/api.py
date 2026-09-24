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
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import unquote, urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

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

    from lemoncrow.pro.capabilities.review.session_models import (
        Annotation,
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


def _markdown_preview_payload(
    store: ReviewStore,
    review_id: str,
    revision: ReviewRevision,
    repo_root: Path,
    entry: Mapping[str, Any],
) -> dict[str, str] | None:
    """Return exact old/new Markdown texts for the stored review revision.

    New-side text comes from LemonCrow's immutable blob artifact, never today's
    worktree. Old-side text is read from the revision's recorded base tree. If
    either side cannot be proved, the browser falls back to the source diff.
    """

    path = str(entry.get("path") or "")
    if not _is_markdown_path(path) or bool(entry.get("is_binary")):
        return None

    file_status = str(entry.get("status") or "modified")
    old_path = str(entry.get("old_path") or "") or path

    if file_status == "deleted":
        new_text = ""
    else:
        blobs = store.read_blob_artifact(review_id, revision.id)
        if blobs is None or path not in blobs:
            return None
        new_text = blobs[path]

    if file_status == "added":
        old_text = ""
    else:
        if not revision.base_sha:
            return None
        from lemoncrow.pro.capabilities.review.gitdiff import _blob_from_tree, _decode, _open_repo, _tree_for_sha

        try:
            repo = _open_repo(repo_root)
            tree = _tree_for_sha(repo, revision.base_sha)
            payload, reason = _blob_from_tree(repo, tree, old_path)
        except (OSError, ValueError):
            return None
        if reason:
            return None
        old_text = _decode(payload)

    return {"kind": "markdown", "old_content": old_text, "new_content": new_text}


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


def _session_payload(session: ReviewSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "ref": f"r/{session.id}",
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


def _revision_payload(revision: ReviewRevision) -> dict[str, Any]:
    return {
        "id": revision.id,
        "ref": f"rr/{revision.id}",
        "revision_number": revision.revision_number,
        "range_mode": revision.range_mode,
        "base_sha": revision.base_sha,
        "head_sha": revision.head_sha,
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


def _review_evidence_payload(evidence: ReviewEvidence, *, current: bool) -> dict[str, Any]:
    """Browser projection. Staleness follows reviewed code identity, not row identity."""

    return {
        "id": evidence.id,
        "review_id": evidence.review_id,
        "revision_id": evidence.revision_id,
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
    runner_registry: Any | None = None,
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
    ``source_store_root`` supplies the normal LemonCrow trace/session store so a
    UI-triggered refresh preserves the provenance/evidence semantics of the CLI
    capture path instead of rebuilding against the Review database itself.
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
    resolved_source_repo = source_repo_root.expanduser().resolve() if source_repo_root is not None else resolved_repo
    resolved_source_store = source_store_root.expanduser().resolve() if source_store_root is not None else store.root
    expected_session_repo = session_repo_identity if session_repo_identity is not None else str(resolved_repo)

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
            "base_revision_number": base_revision.revision_number if base_revision is not None else 0,
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

    def _atomic_write_source(path: Path, text: str) -> None:
        mode = path.stat().st_mode
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.lc-proposal-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, mode)
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
    ) -> tuple[dict[str, int], frozenset[str], dict[str, int]]:
        """Classify current root human feedback for one review revision.

        A comment version is publishable until that exact version is either in
        flight or sent for this revision. Reviewer edits create a newer version
        and become publishable again. An author ``addressed`` claim belongs to
        re-review instead of another automatic send.
        """

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
            if annotation.author_response == "addressed":
                addressed += 1
                continue
            matching = tuple(
                item
                for item in store.list_deliveries(annotation.id)
                if item.revision_id == revision_id and item.annotation_version == version
            )
            states = {item.state for item in matching}
            unsafe_to_resend = any(
                item.state in {"dispatching", "queued", "uncertain"}
                or (item.state in {"blocked", "failed"} and bool(item.remote_ref))
                for item in matching
            )
            if "sent" in states:
                published += 1
            elif unsafe_to_resend:
                # Keep active or ambiguous attempts out of the resendable bucket.
                # Only a proven not-dispatched failure becomes unpublished again.
                in_flight += 1
            else:
                unpublished.add(annotation.id)
        status_payload = {
            "open_total": open_total,
            "unpublished": len(unpublished),
            "published": published,
            "in_flight": in_flight,
            "addressed": addressed,
        }
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
        return session

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
        if revision_number < 1:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such review revision")
        revision = next(
            (item for item in store.list_revisions(session.id) if item.revision_number == revision_number),
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

    def _suggested_compare_from(session: ReviewSession, target: ReviewRevision) -> int:
        """Latest revision this reviewer actually judged before *target*.

        If the reviewer has no older judgment, adjacent snapshots are the least
        surprising fallback. A single-revision review compares to itself.
        """

        revisions = {item.id: item.revision_number for item in store.list_revisions(session.id)}
        judged = [
            revisions[event.revision_id]
            for event in store.list_mark_events(session.id, reviewer_id=session.reviewer_id)
            if event.event_kind == "judgment"
            and event.revision_id in revisions
            and revisions[event.revision_id] < target.revision_number
        ]
        if judged:
            return max(judged)
        return max(1, target.revision_number - 1)

    def _revision_compare_payload(
        session: ReviewSession,
        *,
        from_revision_number: int,
        to_revision_number: int,
    ) -> dict[str, Any]:
        target = _latest(session) if to_revision_number < 1 else _revision_number(session, to_revision_number)
        source_number = from_revision_number if from_revision_number > 0 else _suggested_compare_from(session, target)
        source = _revision_number(session, source_number)
        comparison = compare_revision_units(
            store.list_units(source.id),
            store.list_units(target.id),
            previous_marks=_marks_visible_at(session, source),
            next_marks=_marks_visible_at(session, target),
        )
        source_blob_artifact = store.read_blob_artifact(session.id, source.id)
        target_blob_artifact = store.read_blob_artifact(session.id, target.id)
        code_available = source_blob_artifact is not None and target_blob_artifact is not None
        source_blobs = source_blob_artifact or {}
        target_blobs = target_blob_artifact or {}
        code_files: list[dict[str, Any]] = []
        for path in sorted(set(source_blobs) | set(target_blobs)) if code_available else ():
            before = source_blobs.get(path)
            after = target_blobs.get(path)
            if before == after:
                continue
            status_name = "added" if before is None else "removed" if after is None else "changed"
            before_text = before or ""
            after_text = after or ""
            patch = "".join(
                difflib.unified_diff(
                    before_text.splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
                    fromfile=f"rev-{source.revision_number}/{path}",
                    tofile=f"rev-{target.revision_number}/{path}",
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
        return {
            "review_id": session.id,
            "reviewer_id": session.reviewer_id,
            "from_revision": _revision_payload(source),
            "to_revision": _revision_payload(target),
            "suggested_from_revision_number": _suggested_compare_from(session, target),
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
        return (
            store.list_annotations_at_revision(session.id, revision.id)
            if historical
            else store.list_annotations(session.id)
        )

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
        captured = store.get_revision(evidence.revision_id)
        return captured is not None and _same_reviewed_code(captured, revision)

    def _current_evidence(session: ReviewSession, revision: ReviewRevision) -> tuple[ReviewEvidence, ...]:
        return tuple(item for item in store.list_evidence(session.id) if _evidence_is_current(item, revision))

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
        file_rows = [row for group in groups for row in group["rows"]]
        chapters = build_chapter_lenses(
            file_rows,
            packet,
            repo_root=resolved_repo,
            base_sha=revision.base_sha,
            head_sha=revision.head_sha,
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
        return {
            "session": _session_payload(session),
            "revision": _revision_payload(revision),
            "revision_count": len(store.list_revisions(session.id)),
            "historical": historical,
            "latest_revision_number": _latest(session).revision_number,
            "latest_revision": _revision_payload(_latest(session)),
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
            "evidence": _evidence(packet, current_artifacts),
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
    async def _list_reviews(status_filter: str = "open") -> dict[str, Any]:
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
            payload = _session_payload(session)
            payload["revision_number"] = revision.revision_number if revision is not None else 0
            payload["revision_id"] = revision.id if revision is not None else ""
            payload["comment_count"] = len(annotations)
            payload["open_comment_count"] = sum(
                1 for item in annotations if item.state in {"open", "orphaned"} and not item.parent_id
            )
            rows.append(payload)
        return {"reviews": rows, "repo_root": str(resolved_repo), "status_filter": status_filter}

    @router.get("/reviews/{review_id}/preparation")
    async def _get_review_preparation(review_id: str, preparation_id: str) -> dict[str, Any]:
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
    async def _get_revision_locator(revision_id: str) -> dict[str, Any]:
        session, revision = _revision(revision_id)
        return {
            "review": _session_payload(session),
            "revision": _revision_payload(revision),
        }

    @router.get("/reviews/{review_id}")
    async def _get_review(review_id: str) -> dict[str, Any]:
        return _overview(_session(review_id))

    @router.get("/reviews/{review_id}/revisions/{revision_number}")
    async def _get_historical_review(review_id: str, revision_number: int) -> dict[str, Any]:
        session = _session(review_id)
        revision = _revision_number(session, revision_number)
        return _overview(session, revision, historical=True)

    @router.get("/reviews/{review_id}/revisions")
    async def _get_revisions(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        return {"revisions": [_revision_payload(item) for item in store.list_revisions(session.id)]}

    @router.get("/reviews/{review_id}/compare")
    async def _compare_revisions(
        review_id: str,
        from_revision: int = 0,
        to_revision: int = 0,
    ) -> dict[str, Any]:
        session = _session(review_id)
        return _revision_compare_payload(
            session,
            from_revision_number=from_revision,
            to_revision_number=to_revision,
        )

    @router.get("/compare")
    async def _compare_sources(from_ref: str, to_ref: str) -> dict[str, Any]:
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
            blobs = store.read_blob_artifact(revision.review_id, revision.id)
            if blobs is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="the Review revision predates persisted source snapshots",
                )
            packet = read_packet_json(store, revision) or {}
            raw_file_rows = packet.get("files")
            file_rows: list[Any] = raw_file_rows if isinstance(raw_file_rows, list) else []
            deleted = {
                str(item.get("path") or "")
                for item in file_rows
                if isinstance(item, dict) and str(item.get("status") or "") == "deleted"
            }
            from lemoncrow.pro.capabilities.review.gitdiff import _blob_from_tree, _decode, _open_repo, _tree_for_sha

            # Hosted Review persists a full immutable source-tree manifest whose
            # content digests are pinned by the artifact backend. Prefer that
            # durable snapshot over reopening a transient materialized checkout.
            source_tree = store.read_source_tree_artifact(revision.review_id, revision.id, side="new")
            repo = None
            base_tree = None
            base_tree_loaded = False
            snapshot: dict[str, str | None] = {}
            for path in paths:
                if path in deleted:
                    snapshot[path] = None
                    continue
                if path in blobs:
                    snapshot[path] = blobs[path]
                    continue
                if source_tree is not None:
                    entry = source_tree.get(path)
                    if entry is None:
                        snapshot[path] = None
                        continue
                    digest = str(entry.get("content_digest") or "")
                    payload = store.read_source_content(digest) if digest else None
                    if payload is None:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"frozen source content is unavailable for {path}",
                        )
                    snapshot[path] = _decode(payload)
                    continue
                if not base_tree_loaded:
                    base_tree_loaded = True
                    if revision.base_sha:
                        repo = _open_repo(resolved_source_repo)
                        base_tree = _tree_for_sha(repo, revision.base_sha)
                if base_tree is None or repo is None:
                    snapshot[path] = None
                    continue
                payload, _ = _blob_from_tree(repo, base_tree, path)
                snapshot[path] = None if payload is None else _decode(payload)
            return snapshot

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
            left_blob_artifact = store.read_blob_artifact(left.review_id, left.id)
            right_blob_artifact = store.read_blob_artifact(right.review_id, right.id)
            if left_blob_artifact is None or right_blob_artifact is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="one or both Review revisions predate persisted source snapshots",
                )
            compare_paths = set(left_blob_artifact) | set(right_blob_artifact)
            files = text_map_files(
                revision_snapshot(left, compare_paths),
                revision_snapshot(right, compare_paths),
            )
            from_label = f"rev {left.revision_number} · rr/{left.id}"
            to_label = f"rev {right.revision_number} · rr/{right.id}"
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
                title=f"rev {left.revision_number} → {to_ref}",
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
            from_label = f"rev {left.revision_number} · rr/{left.id}"
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
    async def _get_review_activity(review_id: str, limit: int = 500) -> dict[str, Any]:
        session = _session(review_id)
        events = store.list_activity_events(session.id, limit=limit)
        return {"review_id": session.id, "events": [_activity_payload(item) for item in events]}

    @router.get("/reviews/{review_id}/targets/{unit_key:path}/history")
    async def _get_target_history(review_id: str, unit_key: str) -> dict[str, Any]:
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
    async def _get_annotation_versions(annotation_id: str) -> dict[str, Any]:
        annotation = store.get_annotation(annotation_id)
        if annotation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such comment")
        _session(annotation.review_id)
        versions = store.list_annotation_versions(annotation.id)
        return {
            "annotation_id": annotation.id,
            "versions": [_annotation_version_payload(item) for item in versions],
        }

    @router.get("/reviews/{review_id}/source-state")
    async def _get_source_state(review_id: str) -> dict[str, Any]:
        """Cheap, read-only detection that a local review has newer source.

        This route never snapshots or reconciles. A mismatch is only an offer
        to the human; ``POST /refresh`` remains the one door that advances the
        ReviewRevision and reopens marks/comments.
        """

        session = _session(review_id)
        revision = _latest(session)
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
    async def _get_targets(review_id: str, order: str = "recommended") -> dict[str, Any]:
        """Reader metadata: one non-overlapping judgment target per changed span."""

        session = _session(review_id)
        return _targets_payload(session, _latest(session), order=order, historical=False)

    @router.get("/reviews/{review_id}/revisions/{revision_number}/targets")
    async def _get_historical_targets(
        review_id: str, revision_number: int, order: str = "recommended"
    ) -> dict[str, Any]:
        session = _session(review_id)
        return _targets_payload(
            session,
            _revision_number(session, revision_number),
            order=order,
            historical=True,
        )

    @router.get("/reviews/{review_id}/frontier")
    async def _get_frontier(review_id: str) -> dict[str, Any]:
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
            "previous_revision_number": baseline.revision_number if baseline is not None else 0,
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

    def _surface_runtime(review_id: str) -> tuple[Any, Any, Any, Any]:
        session = _session(review_id)
        revision = _latest(session)
        packet = read_packet_json(store, revision)
        raw_files = packet.get("files", ()) if isinstance(packet, Mapping) else ()
        changed_paths = tuple(
            str(item.get("path") or "")
            for item in raw_files
            if isinstance(item, Mapping) and str(item.get("path") or "")
        )
        from lemoncrow.pro.capabilities.review.runtimes import built_in_runner_registry
        from lemoncrow.pro.capabilities.review.snapshot import _read_source_tree_artifact, materialize_review_side
        from lemoncrow.pro.capabilities.review.surfaces import (
            SurfaceContext,
            built_in_registry,
            load_review_surface_config,
        )

        surface_repo = resolved_repo
        if _read_source_tree_artifact(store, session.id, revision.id, side="new") is not None:
            scratch_root = surface_workspace_root or (store.root / "review" / "surface-context")
            surface_repo = scratch_root / session.id / revision.id / "workspace"
            materialize_review_side(
                store.root,
                resolved_repo,
                revision,
                side="new",
                target=surface_repo,
                store=store,
            )
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

    @router.get("/reviews/{review_id}/surfaces")
    async def _get_surfaces(review_id: str) -> dict[str, Any]:
        """Discover revision-pinned product surfaces through provider plugins."""

        try:
            revision, registry, _runners, context = _surface_runtime(review_id)
            surfaces = registry.discover(context)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"review surface discovery failed: {exc}"
            ) from exc
        return {
            "revision_id": revision.id,
            "surfaces": [surface.to_payload() for surface in surfaces],
        }

    @router.post("/reviews/{review_id}/surface-run")
    def _run_surface(review_id: str, provider: str, surface_id: str, side: str = "new") -> dict[str, Any]:
        """Execute a surface through the runtime runner it is bound to."""

        try:
            revision, registry, runners, context = _surface_runtime(review_id)
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
        return {"revision_id": revision.id, "result": result.to_payload()}

    @router.get("/reviews/{review_id}/units")
    async def _get_units(review_id: str) -> dict[str, Any]:
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
            _markdown_preview_payload(store, session.id, revision, resolved_repo, entry)
            or _media_preview_payload(entry)
            or _web_preview_payload(store, revision, resolved_repo, entry)
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
            "evidence": _evidence(packet, artifacts, path=path),
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

        filename = f"lemoncrow-review-rev-{revision.revision_number}.patch"
        return Response(
            content="".join(chunks),
            media_type="text/x-diff",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/reviews/{review_id}/patch")
    async def _get_review_patch(review_id: str) -> Response:
        session = _session(review_id)
        return _review_patch_response(session, _latest(session))

    @router.get("/reviews/{review_id}/revisions/{revision_number}/patch")
    async def _get_historical_review_patch(review_id: str, revision_number: int) -> Response:
        session = _session(review_id)
        return _review_patch_response(session, _revision_number(session, revision_number))

    @router.get("/reviews/{review_id}/files/{path:path}/patch")
    async def _get_patch(review_id: str, path: str) -> dict[str, Any]:
        session = _session(review_id)
        return _patch_payload(session, _latest(session), path, historical=False)

    @router.get("/reviews/{review_id}/revisions/{revision_number}/files/{path:path}/patch")
    async def _get_historical_patch(review_id: str, revision_number: int, path: str) -> dict[str, Any]:
        session = _session(review_id)
        return _patch_payload(
            session,
            _revision_number(session, revision_number),
            path,
            historical=True,
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
            _web_preview_payload(store, revision, resolved_repo, entry)
            if entry is not None
            else _frozen_nested_web_preview_payload(store, revision, resolved_repo, document_path)
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
                resolved_repo,
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
            _web_preview_payload(store, revision, resolved_repo, entry)
            if entry is not None
            else _frozen_nested_web_preview_payload(store, revision, resolved_repo, document_path)
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
                resolved_repo,
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
    async def _get_media_file(review_id: str, path: str, side: str, revision_id: str = "") -> Response:
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
    async def _get_related_source(review_id: str, path: str, line: int = 1) -> dict[str, Any]:
        session = _session(review_id)
        return _related_payload(session, _latest(session), path, line)

    @router.get("/reviews/{review_id}/revisions/{revision_number}/related/{path:path}")
    async def _get_historical_related_source(
        review_id: str, revision_number: int, path: str, line: int = 1
    ) -> dict[str, Any]:
        session = _session(review_id)
        return _related_payload(session, _revision_number(session, revision_number), path, line)

    @router.post("/reviews/{review_id}/marks")
    async def _post_mark(review_id: str, request: Request) -> dict[str, Any]:
        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
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
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no reviewable unit matches {target!r}")

        marks: list[dict[str, Any]] = []
        for unit in resolution.units:
            recorded = mark_unit(store, session, revision, unit, state=requested, reviewer_id=session.reviewer_id)
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

            recorded = mark_unit(store, session, revision, unit, state="reviewed", reviewer_id=session.reviewer_id)
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
        return {
            "marked": marked,
            "skipped": skipped,
            "progress": after["progress"],
            "outline_updates": [item for item in after["outline"] if item.get("path") in touched_paths],
            "group_counts": after["group_counts"],
        }

    @router.get("/reviews/{review_id}/evidence")
    async def _get_review_evidence(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        revision = _latest(session)
        return {
            "revision_id": revision.id,
            "evidence": [
                _review_evidence_payload(item, current=_evidence_is_current(item, revision))
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
    ) -> dict[str, Any]:
        """Attach one local artifact to exactly the revision the reviewer sees."""

        session = _mutable_session(review_id)
        revision = _latest(session)
        if kind not in REVIEW_EVIDENCE_KINDS or kind == "live_preview":
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
                    status_code=status.HTTP_404_NOT_FOUND, detail="evidence path is not in this revision"
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

        from lemoncrow.pro.capabilities.review.store import new_evidence_id

        evidence_id = new_evidence_id()
        rel, digest, size = store.write_evidence_artifact(session.id, revision.id, evidence_id, payload)
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
                source_ref=filename.strip(),
                verification_status=verification_status,
                detail=detail.strip()[:500],
            )
        )
        return {"evidence": _review_evidence_payload(record, current=True)}

    @router.post("/reviews/{review_id}/evidence/link", status_code=status.HTTP_201_CREATED)
    async def _post_review_evidence_link(review_id: str, request: Request) -> dict[str, Any]:
        """Attach a live preview URL without taking ownership of the environment."""

        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        url = str(body.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="preview URL must be http(s)")
        path = str(body.get("path") or "").strip()
        if path and not any(unit.kind == "file" and unit.path == path for unit in store.list_units(revision.id)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="evidence path is not in this revision")
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
            )
        )
        return {"evidence": _review_evidence_payload(record, current=True)}

    @router.get("/evidence/{evidence_id}/content")
    async def _get_review_evidence_content(evidence_id: str) -> Response:
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
        store.delete_evidence(evidence_id)
        return {"deleted": evidence_id}

    @router.get("/reviews/{review_id}/proposal-selection")
    async def _get_proposal_selection(
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
    async def _get_change_proposals(review_id: str) -> dict[str, Any]:
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
        expected_revision_id = str(body.get("expected_revision_id") or "")
        if expected_revision_id and expected_revision_id != revision.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the Review advanced while this proposal was being prepared; select the current source again",
            )
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
            )
        )
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
            _atomic_write_source(target, next_text)
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
    async def _get_annotations(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        revision = _latest(session)
        annotations = store.list_annotations(session.id)
        feedback_status, _, _ = _feedback_partition(revision.id, annotations)
        return {
            "revision_id": revision.id,
            "historical": False,
            "annotations": [_annotation_payload(item) for item in annotations],
            "counts": annotation_counts(annotations),
            "feedback": feedback_status,
            "delivery": _delivery_capability(revision),
        }

    @router.get("/reviews/{review_id}/revisions/{revision_number}/annotations")
    async def _get_historical_annotations(review_id: str, revision_number: int) -> dict[str, Any]:
        session = _session(review_id)
        revision = _revision_number(session, revision_number)
        annotations = store.list_annotations_at_revision(session.id, revision.id)
        return {
            "revision_id": revision.id,
            "historical": True,
            "annotations": [_annotation_payload(item) for item in annotations],
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
            target_unit = next((unit for unit in units if unit.unit_key == target_key), None)
            if target_unit is None:  # every ReviewTarget is backed by a ReviewUnit
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="review target unit is unavailable")

        try:
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
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        marked_target = None
        progress = None
        outline_updates: list[dict[str, Any]] = []
        if target_unit is not None:
            if mark_target:
                mark_unit(
                    store,
                    session,
                    revision,
                    target_unit,
                    state="needs_changes",
                    reviewer_id=session.reviewer_id,
                )
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

        updated = store.update_annotation(
            annotation_id,
            AnnotationHistoryContext(
                revision_id=revision.id,
                changed_by=session.reviewer_id,
                changed_by_actor="human",
            ),
            **fields,
        )
        if updated is None:  # pragma: no cover - the row was loaded a moment ago
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such comment")
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
        feedback_status, unpublished_ids, versions = _feedback_partition(revision.id, annotations)
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
            "markdown": markdown,
            "annotation_ids": annotation_ids,
            "annotation_versions": selected_versions,
            "feedback_hash": digest,
            "feedback_status": feedback_status,
            "resolved_total": len(full_bundle.resolved),
        }

    def _delivery_capability(revision: ReviewRevision) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.delivery import direct_delivery_supported

        exact = (
            revision.provenance_certainty == "exact"
            and bool(revision.provenance_host)
            and bool(revision.provenance_session_id)
        )
        host = revision.provenance_host if exact else ""
        supported = exact and direct_delivery_supported(host)
        if not exact:
            reason = "exact author-session provenance is unavailable"
        elif not supported:
            reason = f"direct feedback delivery is not configured for {host}"
        else:
            reason = ""
        return {
            "supported": supported,
            "host": host,
            "session_id": revision.provenance_session_id if exact else "",
            "target_ref": f"{host}:{revision.provenance_session_id}" if exact else "",
            "label": host.capitalize() if host else "",
            "reason": reason,
        }

    @router.post("/reviews/{review_id}/feedback/export")
    async def _post_feedback_export(review_id: str) -> dict[str, Any]:
        """Prepare an immutable feedback preview. Rendering never delivers."""

        session = _session(review_id)
        revision = _latest(session)
        snapshot = _feedback_snapshot(session, revision)
        bundle = snapshot["bundle"]
        return {
            "markdown": snapshot["markdown"],
            "open": len(bundle.items),
            "orphaned": len(bundle.orphaned),
            "resolved": snapshot["resolved_total"],
            "revision_id": revision.id,
            "feedback_hash": snapshot["feedback_hash"],
            "operation_id": f"fop-{secrets.token_hex(16)}",
            "annotation_versions": snapshot["annotation_versions"],
            "delivery": _delivery_capability(revision),
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
        if not operation_id or not expected_revision_id or not expected_feedback_hash:
            if allow_implicit_preview and not body:
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
            safely_retryable = all(item.state in {"blocked", "failed"} and not item.remote_ref for item in existing)
            if not safely_retryable:
                first = existing[0]
                replay_state = "uncertain" if first.state == "dispatching" else first.state
                replay_message = first.last_error or (
                    "feedback already sent"
                    if first.state == "sent"
                    else (
                        "delivery may already be in progress; inspect its state before retrying"
                        if first.state == "dispatching"
                        else "delivery already recorded"
                    )
                )
                return {
                    "state": replay_state,
                    "target_ref": first.target_ref,
                    "remote_ref": first.remote_ref,
                    "message": replay_message,
                    "annotation_count": len(existing),
                    "operation_id": operation_id,
                }

        if revision.id != expected_revision_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="review revision changed since feedback preview; prepare the updated feedback before sending",
            )
        capability = _delivery_capability(revision)
        if not capability["supported"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=capability["reason"])
        if required_host and capability["host"] != required_host:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"this compatibility route requires exact {required_host} provenance",
            )

        snapshot = _feedback_snapshot(session, revision)
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

        from lemoncrow.pro.capabilities.review.delivery import deliver_to_agent_session
        from lemoncrow.pro.capabilities.review.session_models import DeliveryRecord

        target_ref = capability["target_ref"]
        pending = tuple(
            DeliveryRecord(
                id="",
                annotation_id=annotation_id,
                target_type="agent_session",
                target_ref=target_ref,
                state="dispatching",
                operation_id=operation_id,
                revision_id=revision.id,
                feedback_hash=snapshot["feedback_hash"],
                annotation_version=int(snapshot["annotation_versions"].get(annotation_id) or 0),
            )
            for annotation_id in annotation_ids
        )
        operation, created = store.begin_delivery_operation(pending)
        if not created:
            first = operation[0]
            return {
                "state": first.state,
                "target_ref": first.target_ref,
                "remote_ref": first.remote_ref,
                "message": first.last_error or "delivery already in progress",
                "annotation_count": len(operation),
                "operation_id": operation_id,
            }

        result = deliver_to_agent_session(
            capability["host"],
            capability["session_id"],
            resolved_repo,
            snapshot["markdown"],
        )
        operation = store.update_delivery_operation(
            operation_id,
            state=result.state,
            remote_ref=result.remote_ref,
            last_error="" if result.accepted else result.message,
        )
        payload = {
            "state": result.state,
            "target_ref": result.target_ref or target_ref,
            "remote_ref": result.remote_ref,
            "message": result.message,
            "annotation_count": len(operation),
            "operation_id": operation_id,
        }
        if result.state in {"blocked", "failed"}:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.message)
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

    def _finish_payload(session: ReviewSession, chosen: ReviewSessionStatus) -> dict[str, Any]:
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
    async def _get_outcome(review_id: str) -> dict[str, Any]:
        return _outcome_payload(_session(review_id))

    @router.post("/reviews/{review_id}/outcome")
    async def _post_outcome(review_id: str, request: Request) -> dict[str, Any]:
        session = _mutable_session(review_id)
        revision = _latest(session)
        body = await _json_object(request)
        outcome = str(body.get("outcome") or "").strip().lower()
        if outcome not in REVIEW_OUTCOME_KINDS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="outcome must be comment, lgtm, or changes_requested",
            )
        store.record_outcome(
            session.id,
            revision.id,
            session.reviewer_id,
            outcome=outcome,  # type: ignore[arg-type]
            summary=str(body.get("summary") or ""),
        )
        return _outcome_payload(session)

    @router.get("/reviews/{review_id}/finish")
    async def _get_finish_preview(review_id: str) -> dict[str, Any]:
        """Preview the exact finish sheet without changing review status."""

        session = _session(review_id)
        return _finish_payload(session, session.status)

    @router.post("/reviews/{review_id}/finish")
    async def _post_finish(review_id: str, request: Request) -> dict[str, Any]:
        """Record a human status choice; never infer or issue approval."""

        session = _session(review_id)
        body: dict[str, Any] = {}
        if (await request.body()).strip():
            body = await _json_object(request)
        wanted = str(body.get("status") or "finished").strip().lower()
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
        return _finish_payload(session, chosen)

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
            "revision_number": result.revision.revision_number,
            "previous_revision_number": (
                result.previous_revision.revision_number if result.previous_revision is not None else 0
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
