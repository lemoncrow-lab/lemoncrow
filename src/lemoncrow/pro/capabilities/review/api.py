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
* Diff *rendering* is the browser's job (``@pierre/diffs``). What crosses the
  wire is the unified patch text git already produced, re-serialised from the
  stored packet -- header lines we already know plus each hunk's recorded body.
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

import secrets
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from lemoncrow.pro.capabilities.review.session_models import (
    ANCHOR_METHOD_LABELS,
    EXACT_ANCHOR_METHODS,
    REVIEW_EVIDENCE_KINDS,
    REVIEW_EVIDENCE_SOURCES,
    ReviewEvidence,
    anchor_symbol_claim,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from lemoncrow.pro.capabilities.review.session_models import (
        Annotation,
        FrontierEntry,
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
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
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
        "revision_number": revision.revision_number,
        "range_mode": revision.range_mode,
        "base_sha": revision.base_sha,
        "head_sha": revision.head_sha,
        "dirty": revision.dirty,
        # No `tree_fingerprint`: it is the store's idempotency key, it was
        # rendered by no pane, and a field the browser carries but never shows
        # is a contract nobody is keeping.
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
                and not item.get("is_binary")
                and bool(item.get("hunks") or item.get("submodule_pointer")),
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
) -> None:
    """Add the review routes and the browser-facing guards to *app*.

    *port* is required rather than inferred: the ``Origin``/``Host`` check has
    to know which origin is *this* workspace, and a guard that accepts any
    loopback port accepts every other local page as well.

    Route registration only. This function never binds a socket, never mints a
    token and never starts a server.
    """

    from fastapi import APIRouter

    from lemoncrow.core.foundation.paths import confine_to_root
    from lemoncrow.pro.capabilities.review.feedback import build_bundle, render_markdown
    from lemoncrow.pro.capabilities.review.revisions import compute_frontier
    from lemoncrow.pro.capabilities.review.session_models import ANNOTATION_STATES
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
    )

    resolved_repo = repo_root.expanduser().resolve()

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
        session = store.get_session(review_id)
        if session is None or session.repo_root != str(resolved_repo):
            # A session for another repository is *not found* here rather than
            # forbidden: this workspace serves exactly one repo_root, and saying
            # "exists elsewhere" would leak that another checkout is under
            # review on this machine.
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

    def _overview(session: ReviewSession) -> dict[str, Any]:
        revision = _latest(session)
        units = store.list_units(revision.id)
        marks = store.list_marks(session.id)
        packet = read_packet_json(store, revision)
        annotations = store.list_annotations(session.id)
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
        artifacts = store.list_evidence(session.id)
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
            "discarded": _discarded_payload(session, revision),
        }

    @router.get("/reviews")
    async def _list_reviews() -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for session in store.list_sessions(status="open"):
            if session.repo_root != str(resolved_repo):
                continue
            revision = store.latest_revision(session.id)
            payload = _session_payload(session)
            payload["revision_number"] = revision.revision_number if revision is not None else 0
            rows.append(payload)
        return {"reviews": rows, "repo_root": str(resolved_repo)}

    @router.get("/reviews/{review_id}")
    async def _get_review(review_id: str) -> dict[str, Any]:
        return _overview(_session(review_id))

    @router.get("/reviews/{review_id}/revisions")
    async def _get_revisions(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        return {"revisions": [_revision_payload(item) for item in store.list_revisions(session.id)]}

    @router.get("/reviews/{review_id}/source-state")
    async def _get_source_state(review_id: str) -> dict[str, Any]:
        """Cheap, read-only detection that a local review has newer source.

        This route never snapshots or reconciles. A mismatch is only an offer
        to the human; ``POST /refresh`` remains the one door that advances the
        ReviewRevision and reopens marks/comments.
        """

        session = _session(review_id)
        revision = _latest(session)
        if session.range_mode not in ("working_tree", "staged"):
            return {
                "supported": False,
                "changed": False,
                "fingerprint": revision.source_fingerprint,
                "path_count": 0,
                "paths": [],
                "reason": "automatic change detection currently applies to working-tree and staged reviews",
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
            current = source_state(resolved_repo, range_for_session(resolved_repo, session))
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

    @router.get("/reviews/{review_id}/targets")
    async def _get_targets(review_id: str, order: str = "recommended") -> dict[str, Any]:
        """Reader metadata: one non-overlapping judgment target per changed span."""

        if order not in {"recommended", "file"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="order must be 'recommended' or 'file' in the R20 reader projection",
            )
        session = _session(review_id)
        revision = _latest(session)
        units = store.list_units(revision.id)
        marks = store.list_marks(session.id)
        annotations = store.list_annotations(session.id)
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
            "order": order,
            "targets": [target_payload(target) for target in targets],
            "progress": progress_payload(progress),
            "outline": [outline_payload(item) for item in outline],
        }

    @router.get("/reviews/{review_id}/units")
    async def _get_units(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        revision = _latest(session)
        units = store.list_units(revision.id)
        marks = {mark.unit_key: mark for mark in store.list_marks(session.id)}

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

    @router.get("/reviews/{review_id}/files/{path:path}/patch")
    async def _get_patch(review_id: str, path: str) -> dict[str, Any]:
        session = _session(review_id)
        revision = _latest(session)
        units = store.list_units(revision.id)

        # Two independent gates, and the order matters. Confinement first, so a
        # traversal never reaches the membership lookup; membership second, so a
        # path that is genuinely inside the repository but is not part of this
        # change is still a 404. Neither gate opens the file: the patch comes
        # from the stored packet artifact, so there is no filesystem read to
        # confine in the first place -- the check is belt and braces.
        try:
            confine_to_root(resolved_repo / path, resolved_repo)
        except (ValueError, OSError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file in this review") from None
        if not any(unit.kind == "file" and unit.path == path for unit in units):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file in this review")

        packet = read_packet_json(store, revision)
        artifacts = tuple(item for item in store.list_evidence(session.id) if _evidence_is_current(item, revision))
        entry = _files_by_path(packet).get(path)
        if entry is None:
            return {
                "path": path,
                "patch": "",
                "renderable": False,
                "refusal": "packet_unavailable" if packet is None else "file_not_in_packet",
                "detail": (
                    "the packet that recorded this revision is not readable"
                    if packet is None
                    else "this revision's packet has no entry for that path"
                ),
                "degraded": [{"name": name, "note": degraded_note(name)} for name in sorted(set(revision.degraded))],
            }
        text, refusal, detail = synthesize_file_patch(entry)
        return {
            "path": path,
            "old_path": entry.get("old_path") or "",
            "status": str(entry.get("status") or "modified"),
            "language": entry.get("language") or "",
            "additions": int(entry.get("additions") or 0),
            "deletions": int(entry.get("deletions") or 0),
            "patch": text,
            "renderable": not refusal,
            "refusal": refusal,
            "detail": detail,
            "impact": _impact_for(packet, path),
            "symbols": _symbols_for(packet, path),
            "provenance": _provenance_for(packet, path),
            "evidence": _evidence(packet, artifacts, path=path),
            "degraded": [{"name": name, "note": degraded_note(name)} for name in sorted(set(revision.degraded))],
        }

    @router.get("/reviews/{review_id}/related/{path:path}")
    async def _get_related_source(review_id: str, path: str, line: int = 1) -> dict[str, Any]:
        """Read an impact target at the reviewed revision without leaving LemonCrow.

        This is deliberately *not* a repository browser. The path must already
        appear in this revision's impact evidence, and it is read from the
        reviewed git tree rather than from the current worktree.
        """

        session = _session(review_id)
        revision = _latest(session)
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
                status_code=status.HTTP_404_NOT_FOUND, detail="path is not impact evidence for this review"
            )

        text, refusal = _related_source_text(revision, path)
        if refusal:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refusal)
        lines = text.splitlines()
        focus = min(max(1, int(line or 1)), max(1, len(lines)))
        start = max(1, focus - 35)
        end = min(len(lines), focus + 35)
        suffix = path.rsplit(".", 1)[-1] if "." in path else ""
        return {
            "path": path,
            "language": suffix,
            "start_line": start,
            "end_line": end,
            "focus_line": focus,
            "text": "\n".join(lines[start - 1 : end]),
        }

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
                store.list_marks(session.id),
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
            recorded = mark_unit(store, session, revision, unit, state=requested)
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
        marks = store.list_marks(session.id)
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

            recorded = mark_unit(store, session, revision, unit, state="reviewed")
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

    @router.get("/reviews/{review_id}/annotations")
    async def _get_annotations(review_id: str) -> dict[str, Any]:
        session = _session(review_id)
        revision = _latest(session)
        annotations = store.list_annotations(session.id)
        return {
            "revision_id": revision.id,
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
            marks = store.list_marks(session.id)
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
                mark_unit(store, session, revision, target_unit, state="needs_changes")
            overview = _overview(session)
            progress = overview["progress"]
            outline_updates = [item for item in overview["outline"] if item.get("path") == target_unit.path]

            from lemoncrow.pro.capabilities.review.targets import target_payload

            latest_annotations = store.list_annotations(session.id)
            latest_marks = store.list_marks(session.id)
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
        _mutable_session(annotation.review_id)

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

        updated = store.update_annotation(annotation_id, **fields)
        if updated is None:  # pragma: no cover - the row was loaded a moment ago
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such comment")
        return {"annotation": _annotation_payload(updated)}

    @router.post("/reviews/{review_id}/feedback/export")
    async def _post_feedback_export(review_id: str) -> dict[str, Any]:
        """Render the review's comments as Markdown. Renders; never delivers.

        Delivery is PR-R6's job and a separate decision: handing an agent a
        bundle is an action with consequences, and an export route that also
        sent it would make the two indistinguishable.
        """

        session = _session(review_id)
        revision = _latest(session)
        packet = read_packet_json(store, revision)
        bundle = build_bundle(
            session,
            revision,
            store.list_annotations(session.id),
            store.list_units(revision.id),
            context=_feedback_context(packet),
            title=(packet.get("title") if packet is not None else "") or session.title,
        )
        return {
            "markdown": render_markdown(bundle),
            "open": len(bundle.items),
            "orphaned": len(bundle.orphaned),
            "resolved": len(bundle.resolved),
        }

    @router.post("/reviews/{review_id}/feedback/claude")
    async def _post_feedback_claude(review_id: str) -> dict[str, Any]:
        """Deliver human feedback to one exact inactive Claude session.

        Exact provenance is a hard precondition. The adapter also refuses an
        already-running target because Claude may otherwise fork a copy; a
        review tool must not call that "sent to the author".
        """

        session = _mutable_session(review_id)
        revision = _latest(session)
        if (
            revision.provenance_host != "claude"
            or revision.provenance_certainty != "exact"
            or not revision.provenance_session_id
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="direct Claude delivery requires exact Claude-session provenance for this revision",
            )

        packet = read_packet_json(store, revision)
        bundle = build_bundle(
            session,
            revision,
            store.list_annotations(session.id),
            store.list_units(revision.id),
            context=_feedback_context(packet),
            title=(packet.get("title") if packet is not None else "") or session.title,
        )
        annotation_ids = tuple(item.annotation_id for item in (*bundle.items, *bundle.orphaned))
        if not annotation_ids:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="there is no open human feedback to send")

        from lemoncrow.pro.capabilities.review.delivery import deliver_to_claude_session
        from lemoncrow.pro.capabilities.review.session_models import DeliveryRecord

        result = deliver_to_claude_session(
            revision.provenance_session_id,
            resolved_repo,
            render_markdown(bundle),
        )
        for annotation_id in annotation_ids:
            store.record_delivery(
                DeliveryRecord(
                    id="",
                    annotation_id=annotation_id,
                    target_type="agent_session",
                    target_ref=result.target_ref,
                    state=result.state,
                    remote_ref=result.remote_ref,
                    last_error="" if result.sent else result.message,
                )
            )
        if not result.sent:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.message)
        return {
            "state": result.state,
            "target_ref": result.target_ref,
            "remote_ref": result.remote_ref,
            "message": result.message,
            "annotation_count": len(annotation_ids),
        }

    def _finish_payload(session: ReviewSession, chosen: ReviewSessionStatus) -> dict[str, Any]:
        """Target-based completion snapshot shared by preview and mutation."""

        # Build every fallible summary before persisting the human's status.
        # Previously a missing/corrupt packet could make `_overview` raise after
        # `set_review_status` had already committed FINISHED, so the client saw a
        # 404 while the durable review silently changed state.
        overview = _overview(session)
        closure = set_review_status(store, session, store.latest_revision(session.id), status=chosen)
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
        # Snapshot the reviewer's left-hand side before reconciliation mutates
        # mark rows and relocates annotations. R25 must describe the actual
        # transition, not reconstruct an approximation afterwards.
        before_marks = store.list_marks(session.id)
        before_annotations = store.list_annotations(session.id)

        from lemoncrow.pro.capabilities.review.sources.local import refresh

        try:
            result = refresh(store, session, resolved_repo, store_root=store.root, limit=5000)
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
        current_annotations = store.list_annotations(session.id)
        current_frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            result.revision,
            current_units,
            store.list_marks(session.id),
            annotations=current_annotations,
        )
        current_targets = _derive_targets(
            session,
            result.revision,
            current_units,
            current_frontier.entries,
            read_packet_json(store, result.revision),
            current_annotations,
        )
        target_delta = revision_target_delta(
            previous_targets,
            current_targets,
            aliases=result.reconciliation.aliases,
        )
        # Named out of the revision the reviewer last saw: it is the last one
        # that still contained the units this refresh took away.
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
