"""Local <-> hosted Review attachment (G5-SYNC): queue guards, conflict rules, reconnect.

The contract is "G5-SYNC contract" in
``docs/planning/2026-10-05-production-guarantees-plan.md``. In short:

* A local Review is fully usable alone. Attaching it to a hosted Review is
  explicit; from then on hosted owns comments, judgments, requests, outcomes
  and policy satisfaction.
* The local Review keeps a *projection* (the hosted state last read) and a
  queue of explicitly queued actions, each bound to the hosted Snapshot it was
  made on plus compare-and-set preconditions read from that projection.
* Reconnect replays the queue in order. Hosted evaluates every precondition
  inside one Review transition and answers ``applied`` or ``conflict`` with a
  reason; nothing is merged, rebased or carried forward silently.

Everything here is pure or talks through the two small ports below, so the
same rules run in the local Reader API (queue guards), the hosted server
(:func:`conflict_reason`) and the client (:func:`reconnect`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from lemoncrow.pro.capabilities.review.errors import (
    HostedActionRefused,
    HostedAttachmentConflict,
    HostedReplayRefused,
    HostedUnavailable,
)
from lemoncrow.pro.capabilities.review.session_models import (
    ANNOTATION_KINDS,
    MARK_STATES,
    REVIEW_OUTCOME_KINDS,
    HostedAction,
)

#: Pending + conflicted actions one attached Review may hold. A reviewer with a
#: full queue syncs, discards or resolves before queueing more.
UNRESOLVED_ACTION_LIMIT: Final[int] = 100
#: Applied/discarded actions kept per Review as the visible replay log.
RESOLVED_ACTION_KEEP: Final[int] = 200
#: Applied/discarded actions older than this are pruned on the next queue write.
RESOLVED_ACTION_MAX_AGE_DAYS: Final[int] = 30
# lc-debt: a projection carries at most this many targets/files and threads; a larger hosted
# Review projects truncated and anything past the cut is refused as unknown_target/unknown_thread
# (fail-closed). Upgrade path: page the projection by path when a real Change needs it.
PROJECTION_TARGET_LIMIT: Final[int] = 5000
PROJECTION_THREAD_LIMIT: Final[int] = 2000
COMMENT_BODY_LIMIT: Final[int] = 64 * 1024
OUTCOME_SUMMARY_LIMIT: Final[int] = 16 * 1024
#: ``review_derived`` kind of the hosted idempotency receipt for one replayed action.
RECEIPT_KIND: Final[str] = "attachment_receipt"

#: Every reason a replayed action can end in ``conflict``, in evaluation order
#: for the reasons :func:`conflict_reason` decides. ``scope_unresolved`` is
#: decided by the hosted request rule and ``rejected`` by hosted payload
#: validation (the action can never apply as written). ``target_changed``
#: (RC5): hosted's own unit or file on the action's Snapshot is not the content
#: the reviewer judged or commented on, so the anchor is never carried by guess.
CONFLICT_REASONS: Final[tuple[str, ...]] = (
    "review_closed",
    "thread_deleted",
    "thread_changed",
    "request_changed",
    "snapshot_advanced",
    "target_changed",
    "judgment_changed",
    "scope_unresolved",
    "rejected",
)
REPLAY_STATES: Final[tuple[str, ...]] = ("applied", "conflict")


def hosted_reference(hosted_review_id: str) -> str:
    """The one public reference an attached Review shows: hosted's own ``r/<id>``."""

    return f"r/{hosted_review_id}"


def attached_identity(*, local_review_id: str, origin: str, hosted_review_id: str) -> dict[str, Any]:
    """The identity fields every projection of an attached Review carries (RC5).

    The canonical Change is hosted's: ``id``, ``short_id``, ``ref``,
    ``change_ref`` and ``review_path`` all name the hosted Review, and the
    local store resolves that id back to this Review for routing. The local
    id survives only as diagnostic provenance (``hosted.local_review_id``).
    """

    reference = hosted_reference(hosted_review_id)
    return {
        "id": hosted_review_id,
        "short_id": hosted_review_id,
        "ref": reference,
        "change_ref": reference,
        "review_path": f"/{reference}",
        "hosted": {
            "reference": reference,
            "origin": origin,
            "url": f"{origin}/{reference}",
            "local_review_id": local_review_id,
        },
    }


# --------------------------------------------------------------------------- #
# projection (hosted state as last read)
# --------------------------------------------------------------------------- #


def _mapping(raw: Any, name: str) -> dict[str, Any]:
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise HostedActionRefused("invalid_projection", f"hosted projection field {name!r} must be an object")
    return dict(value)


def parse_projection(raw: Any) -> dict[str, Any]:
    """Validate a hosted attachment projection before it is stored locally."""

    if not isinstance(raw, Mapping):
        raise HostedActionRefused("invalid_projection", "hosted projection must be an object")
    for name in ("review_id", "repo_id", "principal_id", "status"):
        if not isinstance(raw.get(name), str) or not str(raw.get(name)).strip():
            raise HostedActionRefused("invalid_projection", f"hosted projection needs {name}")
    snapshot = _mapping(raw, "snapshot")
    if not isinstance(snapshot.get("id"), str) or not snapshot["id"]:
        raise HostedActionRefused("invalid_projection", "hosted projection needs snapshot.id")
    targets = _mapping(raw, "targets")
    files = _mapping(raw, "files")
    threads = _mapping(raw, "threads")
    if len(targets) > PROJECTION_TARGET_LIMIT or len(files) > PROJECTION_TARGET_LIMIT:
        raise HostedActionRefused("invalid_projection", "hosted projection carries too many targets")
    if len(threads) > PROJECTION_THREAD_LIMIT:
        raise HostedActionRefused("invalid_projection", "hosted projection carries too many threads")
    return {
        **dict(raw),
        "snapshot": snapshot,
        "targets": targets,
        "files": files,
        "threads": threads,
        "my_marks": _mapping(raw, "my_marks"),
        "my_requests": _mapping(raw, "my_requests"),
        "policy": _mapping(raw, "policy"),
    }


# --------------------------------------------------------------------------- #
# queue guards (local side)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PreparedAction:
    """A validated local action ready for :meth:`ReviewStore.add_hosted_action`."""

    kind: str
    snapshot_id: str
    payload: dict[str, Any]
    preconditions: dict[str, Any]
    supersede_key: str = ""
    supersede: bool = False


def _text(body: Mapping[str, Any], name: str, *, limit: int, required: bool = True) -> str:
    value = body.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise HostedActionRefused("invalid", f"{name} is required")
    if len(value.encode("utf-8")) > limit:
        raise HostedActionRefused("invalid", f"{name} is longer than {limit} bytes")
    return value


def _line(body: Mapping[str, Any], name: str, default: int | None) -> int:
    """A non-negative line number; absent is *default*, or refused when there is none."""

    if name not in body:
        if default is None:
            raise HostedActionRefused("invalid", f"{name} is required unless file_level is true")
        return default
    value = body[name]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HostedActionRefused("invalid", f"{name} must be a non-negative integer")
    return value


def _choice(body: Mapping[str, Any], name: str, default: str) -> str:
    """A string field; absent, null or empty is *default*; any other type is refused, never cast."""

    value = body.get(name)
    if value is None:
        return default
    if not isinstance(value, str):
        raise HostedActionRefused("invalid", f"{name} must be a string")
    return value or default


def _flag(body: Mapping[str, Any], name: str) -> bool:
    value = body.get(name)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise HostedActionRefused("invalid", f"{name} must be true or false")
    return value


def _mark_fact(raw: Any) -> list[str] | None:
    if not isinstance(raw, Mapping):
        return None
    return [str(raw.get("state") or ""), str(raw.get("reviewed_revision_id") or "")]


def anchor_fingerprint(path: str, text: str) -> str:
    """Exact identity of the new-side text a queued comment's lines name (RC5).

    Unit and file fingerprints are normalized on purpose (trailing whitespace,
    line endings and outer blank lines never reopen a review), so two files
    with equal fingerprints can still number their lines differently. A line
    anchor carries only onto byte-identical text, so this hashes the raw text
    and path; client and hosted compute it from their own frozen new side.
    """

    digest = hashlib.sha256(b"lc-anchor-v1\x00")
    digest.update(path.encode("utf-8", "surrogatepass"))
    digest.update(b"\x00")
    digest.update(text.encode("utf-8", "surrogatepass"))
    return digest.hexdigest()


def _snapshot_content(
    projection: Mapping[str, Any], local_units: Mapping[str, str], local_files: Mapping[str, str]
) -> str:
    """Digest of the hosted Snapshot's content, when the local revision is exactly it (RC5).

    A verdict covers the whole Change, so it queues only where every file and
    every projected target the reviewer judged has hosted's fingerprint, and
    the projection was not truncated (content past the cut is unseen).
    """

    truncated = projection.get("truncated")
    if isinstance(truncated, Mapping) and truncated.get("targets"):
        raise HostedActionRefused(
            "content_mismatch", "the hosted revision is too large to confirm here; give the verdict on hosted"
        )
    files = dict(projection.get("files") or {})
    targets = {
        unit_key: target.get("fingerprint") if isinstance(target, Mapping) else None
        for unit_key, target in dict(projection.get("targets") or {}).items()
    }
    if dict(local_files) != files or any(
        not isinstance(fingerprint, str) or local_units.get(unit_key) != fingerprint
        for unit_key, fingerprint in targets.items()
    ):
        raise HostedActionRefused(
            "content_mismatch",
            "the local Review is not the hosted revision's content; refresh it or give the verdict on hosted",
        )
    canonical = json.dumps({"files": files, "targets": targets}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


def prepare_action(
    kind: str,
    body: Mapping[str, Any],
    projection: Mapping[str, Any],
    *,
    local_units: Mapping[str, str],
    local_files: Mapping[str, str],
    local_text: Callable[[str], str | None] | None = None,
) -> PreparedAction:
    """Validate one local action against the projection it was made on.

    *local_units* (unit_key -> fingerprint) and *local_files* (path -> file
    fingerprint) describe the local Snapshot the reviewer is looking at, and
    *local_text* returns its exact new-side text of one path (``None`` when it
    has none). A judgment, comment or verdict is queued only on content
    identical to the hosted Snapshot's, so a queued action can never claim the
    reviewer saw something hosted does not have.
    """

    if str(projection.get("status") or "") != "open":
        raise HostedActionRefused("review_closed", "the hosted Review is not open")
    snapshot_id = str(dict(projection.get("snapshot") or {}).get("id") or "")
    if not snapshot_id:
        raise HostedActionRefused("invalid_projection", "the hosted projection has no revision")
    if kind == "mark":
        unit_key = _text(body, "unit_key", limit=2048)
        state = _choice(body, "state", "reviewed")
        if state not in MARK_STATES:
            raise HostedActionRefused("invalid", f"unknown mark state {state!r}")
        if state == "later" and projection.get("later_marks") is not True:
            raise HostedActionRefused(
                "invalid",
                "Decide later is not available on hosted reviews yet; mark the part reviewed or needs_changes",
            )
        target = dict(projection.get("targets") or {}).get(unit_key)
        if not isinstance(target, Mapping):
            raise HostedActionRefused("unknown_target", "this part is not in the hosted revision")
        local = local_units.get(unit_key)
        if local is None:
            raise HostedActionRefused("unknown_target", "this part is not in the local revision")
        if local != str(target.get("fingerprint") or ""):
            raise HostedActionRefused(
                "content_mismatch", "the local part differs from the hosted revision; refresh or open hosted"
            )
        mine = dict(projection.get("my_marks") or {}).get(unit_key)
        return PreparedAction(
            kind=kind,
            snapshot_id=snapshot_id,
            payload={"unit_key": unit_key, "state": state, "fingerprint": local},
            # Hosted confirms the judged unit is this exact content on its Snapshot.
            preconditions={"mark": _mark_fact(mine), "fingerprint": local},
            supersede_key=f"mark:{unit_key}",
            supersede=True,
        )
    if kind == "comment":
        path = _text(body, "path", limit=1024)
        text = _text(body, "body", limit=COMMENT_BODY_LIMIT)
        comment_kind = _choice(body, "kind", "comment")
        if comment_kind not in ANNOTATION_KINDS:
            raise HostedActionRefused("invalid", f"unknown comment kind {comment_kind!r}")
        side = _choice(body, "side", "new")
        # The hosted comment endpoint's rules: a line comment names its line.
        file_level = _flag(body, "file_level")
        start = 0 if file_level else _line(body, "start_line", None)
        end = 0 if file_level else _line(body, "end_line", start)
        hosted_fingerprint = dict(projection.get("files") or {}).get(path)
        if not isinstance(hosted_fingerprint, str):
            raise HostedActionRefused("unknown_target", "this file is not in the hosted revision")
        local = local_files.get(path)
        if local is None:
            raise HostedActionRefused("unknown_target", "this file is not in the local revision")
        if local != hosted_fingerprint:
            raise HostedActionRefused(
                "content_mismatch", "the local file differs from the hosted revision; refresh or open hosted"
            )
        # Hosted re-checks the anchor against its own frozen new side. Neither side
        # keeps old-side text, so a comment on a deletion cannot be confirmed later.
        if side != "new":
            raise HostedActionRefused(
                "online_only", "a comment on the old side cannot be confirmed later; comment on the hosted Review"
            )
        new_text = local_text(path) if local_text is not None else None
        if new_text is None:
            raise HostedActionRefused(
                "online_only",
                "this file has no exact new-side text here (deleted, binary or not captured); "
                "comment on the hosted Review",
            )
        return PreparedAction(
            kind=kind,
            snapshot_id=snapshot_id,
            payload={
                "path": path,
                "body": text,
                "kind": comment_kind,
                "side": side,
                "file_level": file_level,
                "start_line": start,
                "end_line": end,
            },
            # The anchor carries only onto the exact file text: hosted confirms both.
            preconditions={"fingerprint": local, "anchor": anchor_fingerprint(path, new_text)},
        )
    if kind == "reply":
        parent_id = _text(body, "parent_id", limit=256)
        text = _text(body, "body", limit=COMMENT_BODY_LIMIT)
        thread = dict(projection.get("threads") or {}).get(parent_id)
        if not isinstance(thread, Mapping):
            raise HostedActionRefused("unknown_thread", "this thread is not on the hosted Review")
        return PreparedAction(
            kind=kind,
            snapshot_id=snapshot_id,
            payload={"parent_id": parent_id, "body": text},
            preconditions={"thread_version": int(thread.get("version") or 0)},
        )
    if kind == "outcome":
        outcome = _choice(body, "outcome", "").strip().lower()
        if outcome not in REVIEW_OUTCOME_KINDS:
            raise HostedActionRefused("invalid", "outcome must be comment, lgtm or changes_requested")
        summary = _text(body, "summary", limit=OUTCOME_SUMMARY_LIMIT, required=False)
        content = _snapshot_content(projection, local_units, local_files)
        mine = projection.get("my_outcome")
        return PreparedAction(
            kind=kind,
            snapshot_id=snapshot_id,
            payload={"outcome": outcome, "summary": summary},
            # ``content`` names what the verdict covered, for an explicit requeue;
            # hosted's own binding is the immutable Snapshot id.
            preconditions={
                "outcome_id": str(mine.get("id") or "") if isinstance(mine, Mapping) else "",
                "content": content,
            },
            supersede_key="outcome",
            supersede=True,
        )
    if kind == "request_complete":
        request_id = _text(body, "request_id", limit=256)
        request = dict(projection.get("my_requests") or {}).get(request_id)
        if not isinstance(request, Mapping) or request.get("state") != "active":
            raise HostedActionRefused("unknown_request", "you have no active hosted request with this id")
        return PreparedAction(
            kind=kind,
            snapshot_id=snapshot_id,
            payload={"request_id": request_id},
            preconditions={"request_version": int(request.get("version") or 0)},
            supersede_key=f"request:{request_id}",
        )
    raise HostedActionRefused("invalid", f"unknown hosted action kind {kind!r}")


#: The preconditions naming the content an action was made on, by kind (RC5).
CONTENT_BINDINGS: Final[dict[str, tuple[str, ...]]] = {
    "mark": ("fingerprint",),
    "comment": ("fingerprint", "anchor"),
    "outcome": ("content",),
}


def requeue_keeps_content(kind: str, original: Mapping[str, Any], renewed: Mapping[str, Any]) -> bool:
    """Whether an explicit requeue re-binds *original* preconditions to the same content.

    A judgment, anchor or verdict is never carried onto content the reviewer
    did not see: each binding must be present in the conflicted action and
    equal in the fresh preparation. Otherwise the conflict stays and the
    reviewer acts again on what is on screen.
    """

    return all(name in original and original[name] == renewed.get(name) for name in CONTENT_BINDINGS.get(kind, ()))


#: Reader writes that queue while attached (RC5), by route. Every other
#: hosted-owned write (bulk and behavior judgments, decision events, waivers,
#: landing, finishing, comment edits and provider publishing) stays online-only.
READER_QUEUED_WRITES: Final[tuple[str, ...]] = ("mark", "annotation", "outcome")


_READER_SIDES: Final[dict[str, str]] = {"additions": "new", "deletions": "old"}


def _reader_line(selection: Mapping[str, Any], names: tuple[str, str]) -> Any:
    """The Reader's spelling of a line (``start_line`` or ``start``), untouched; ``None`` when absent."""

    for name in names:
        if selection.get(name) is not None:
            return selection[name]
    return None


def reader_write_actions(route: str, body: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The queued actions one local Reader write becomes while its Review is attached.

    Only the Reader's own spellings are translated (a nested ``range``, the
    ``start``/``end`` aliases); the values are passed through untouched so
    :func:`prepare_action` -- the queue twin of the hosted v1 validators --
    accepts or refuses them exactly as the hosted endpoint would. A spelling
    that would widen locally (a bare ``path`` mark) is refused, never guessed.
    """

    if route == "mark":
        if body.get("unit_key") is None and body.get("path") is not None:
            raise HostedActionRefused("invalid", "a queued judgment names its exact unit_key, not a path")
        return [("mark", {"unit_key": body.get("unit_key"), "state": body.get("state")})]
    if route == "outcome":
        return [("outcome", {"outcome": body.get("outcome"), "summary": body.get("summary")})]
    if route != "annotation":
        raise HostedActionRefused("invalid", f"the Reader does not queue {route!r} writes")
    parent_id = body.get("parent_id")
    if parent_id:
        if body.get("mark_target"):
            raise HostedActionRefused("invalid", "only a root request_change may mark a part")
        return [("reply", {"parent_id": parent_id, "body": body.get("body")})]
    nested = body.get("range")
    selection: Mapping[str, Any] = nested if isinstance(nested, Mapping) else body
    side = selection.get("side") if selection.get("side") is not None else body.get("side")
    comment: dict[str, Any] = {
        "path": body.get("path"),
        "body": body.get("body"),
        "kind": body.get("kind"),
        # The diff viewer's side names; hosted anchors on the canonical ones.
        "side": _READER_SIDES.get(side, side) if isinstance(side, str) else side,
        "file_level": body.get("file_level"),
    }
    if body.get("file_level") is not True:
        start = _reader_line(selection, ("start_line", "start"))
        end = _reader_line(selection, ("end_line", "end"))
        if start is not None:
            comment["start_line"] = start
        # The Reader sends 0 (or nothing) for a one-line selection: the end is the start.
        if end is not None and end != 0:
            comment["end_line"] = end
    actions: list[tuple[str, dict[str, Any]]] = [("comment", comment)]
    if body.get("mark_target"):
        if body.get("kind") != "request_change":
            raise HostedActionRefused("invalid", "only a root request_change may mark a part")
        target = body.get("target_unit_key")
        if not target:
            raise HostedActionRefused("invalid", "target_unit_key is required when mark_target is true")
        actions.append(("mark", {"unit_key": target, "state": "needs_changes"}))
    return actions


# --------------------------------------------------------------------------- #
# conflict rules (hosted side)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HostedFacts:
    """What hosted holds *now*, read inside the replay transaction.

    ``thread_version`` is ``None`` when the replied-to thread is gone;
    ``request_version`` is ``None`` when the request is gone or is not the
    replaying principal's. ``mark`` is the principal's ``[state,
    reviewed_revision_id]`` on the unit, ``outcome_id`` their newest outcome.
    ``target_fingerprint`` is hosted's content fingerprint of the judged unit
    or commented file on the current Snapshot (``None`` when it has none), and
    ``anchor`` the :func:`anchor_fingerprint` of the commented file's frozen
    new-side text (``None`` for the old side or a file without that text).
    """

    review_status: str
    snapshot_id: str
    thread_version: int | None = None
    request_version: int | None = None
    mark: list[str] | None = None
    outcome_id: str = ""
    target_fingerprint: str | None = None
    anchor: str | None = None


def conflict_reason(kind: str, snapshot_id: str, preconditions: Mapping[str, Any], facts: HostedFacts) -> str:
    """``""`` when the action may apply, else the first conflict in :data:`CONFLICT_REASONS` order.

    Every kind is strictly Snapshot-bound: an action made on Snapshot N never
    applies to N+1, even where the unit did not change. Carry-forward is the
    reviewer's explicit ``requeue``, never a silent merge.
    """

    if facts.review_status != "open":
        return "review_closed"
    if kind == "reply":
        if facts.thread_version is None:
            return "thread_deleted"
        if facts.thread_version != preconditions.get("thread_version"):
            return "thread_changed"
    if kind == "request_complete" and (
        facts.request_version is None or facts.request_version != preconditions.get("request_version")
    ):
        return "request_changed"
    if snapshot_id != facts.snapshot_id:
        return "snapshot_advanced"
    # An action queued with its anchor's content (RC5) applies only where hosted
    # confirms that exact content; one queued before RC5 carries none.
    if kind in {"mark", "comment"} and "fingerprint" in preconditions:
        if facts.target_fingerprint is None or facts.target_fingerprint != preconditions.get("fingerprint"):
            return "target_changed"
        # Line numbers name the same lines only on byte-identical new-side text.
        if kind == "comment" and (facts.anchor is None or facts.anchor != preconditions.get("anchor")):
            return "target_changed"
    if kind == "mark" and facts.mark != preconditions.get("mark"):
        return "judgment_changed"
    if kind == "outcome" and facts.outcome_id != preconditions.get("outcome_id"):
        return "judgment_changed"
    return ""


# --------------------------------------------------------------------------- #
# wire
# --------------------------------------------------------------------------- #


def _loads(text: str) -> Any:
    try:
        return json.loads(text or "{}")
    except ValueError:
        return {}


def action_wire(action: HostedAction) -> dict[str, Any]:
    return {
        "id": action.id,
        "sequence": action.sequence,
        "kind": action.kind,
        "snapshot_id": action.snapshot_id,
        "supersede_key": action.supersede_key,
        "payload": _loads(action.payload_json),
        "preconditions": _loads(action.preconditions_json),
        "state": action.state,
        "reason": action.reason,
        "result": _loads(action.result_json),
        "created_at": action.created_at,
        "resolved_at": action.resolved_at,
    }


def replay_request(action: Mapping[str, Any], principal_id: str) -> dict[str, Any]:
    """The hosted ``attachment/actions`` body for one queued action (wire form)."""

    return {
        "action_id": str(action["id"]),
        "kind": str(action["kind"]),
        "snapshot_id": str(action["snapshot_id"]),
        "payload": dict(action.get("payload") or {}),
        "preconditions": dict(action.get("preconditions") or {}),
        "actor_id": principal_id,
    }


# --------------------------------------------------------------------------- #
# reconnect (client side)
# --------------------------------------------------------------------------- #


class LocalPort(Protocol):
    """The local Reader API of one attached Review.

    ``record_result`` raises :class:`HostedAttachmentConflict` when the action
    is no longer in *expected_state*.
    """

    def status(self) -> Mapping[str, Any]: ...

    def record_projection(self, projection: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def record_result(
        self,
        action_id: str,
        *,
        expected_state: str,
        state: str,
        reason: str,
        result: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class HostedPort(Protocol):
    """The hosted Review the local one is attached to, as the attached principal."""

    def projection(self) -> Mapping[str, Any]: ...

    def replay(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass
class ReconnectReport:
    """What one reconnect did. ``stopped`` names why replay ended early, if it did."""

    identity: str = "ok"
    stopped: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)
    remaining: int = 0
    projection_refreshed: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "stopped": self.stopped,
            "results": list(self.results),
            "remaining": self.remaining,
            "projection_refreshed": self.projection_refreshed,
        }


def _valid_response(response: Mapping[str, Any], action_id: str) -> bool:
    state = response.get("state")
    if response.get("action_id") != action_id or state not in REPLAY_STATES:
        return False
    return state == "applied" or response.get("reason") in CONFLICT_REASONS


def reconnect(local: LocalPort, hosted: HostedPort) -> ReconnectReport:
    """Replay every pending action in queue order; record each exact result locally.

    The hosted principal must be the one the Review was attached as, or
    nothing replays. An unreachable hosted stops the loop and leaves the
    rest pending; a refused credential stops it too. Each answered action is
    recorded as ``applied`` or ``conflict`` with hosted's reason -- the
    reviewer resolves conflicts explicitly (discard or requeue).
    """

    report = ReconnectReport()
    status = local.status()
    attachment = dict(status.get("attachment") or {})
    if not attachment:
        report.identity = "not_attached"
        return report
    pending = [dict(row) for row in status.get("actions") or () if dict(row).get("state") == "pending"]
    pending.sort(key=lambda row: int(row.get("sequence") or 0))
    report.remaining = len(pending)
    try:
        current = hosted.projection()
    except HostedUnavailable:
        report.stopped = "offline"
        return report
    except HostedReplayRefused as exc:
        report.stopped = exc.code
        return report
    if current.get("principal_id") != attachment.get("principal_id") or current.get("review_id") != attachment.get(
        "hosted_review_id"
    ):
        report.identity = "mismatch"
        return report
    principal_id = str(attachment["principal_id"])
    for action in pending:
        action_id = str(action["id"])
        try:
            response = hosted.replay(replay_request(action, principal_id))
        except HostedUnavailable:
            report.stopped = "offline"
            break
        except HostedReplayRefused as exc:
            if exc.code != "rejected":
                report.stopped = exc.code
                break
            response = {"action_id": action_id, "state": "conflict", "reason": "rejected", "message": exc.message}
        if not _valid_response(response, action_id):
            report.stopped = "invalid_response"
            break
        state = str(response["state"])
        reason = "" if state == "applied" else str(response["reason"])
        overrode = ""
        try:
            local.record_result(action_id, expected_state="pending", state=state, reason=reason, result=response)
        except HostedAttachmentConflict:
            # The reviewer discarded (or superseded) it while it was in flight.
            # Hosted already answered, and hosted's answer is the truth: record
            # it over the discard and say so, never leave the two disagreeing.
            try:
                local.record_result(
                    action_id,
                    expected_state="discarded",
                    state=state,
                    reason=reason,
                    result={**response, "overrode": "discarded"},
                )
            except HostedAttachmentConflict:
                # Gone or moved again: hosted keeps its receipt, so the next
                # reconnect is not a second application; stop and say why.
                report.stopped = "local_changed"
                break
            overrode = "discarded"
        report.results.append(
            {"action_id": action_id, "kind": action.get("kind"), "state": state, "reason": reason, "overrode": overrode}
        )
        report.remaining -= 1
    try:
        refreshed = hosted.projection()
    except (HostedUnavailable, HostedReplayRefused):
        return report
    if refreshed.get("principal_id") == attachment.get("principal_id") and refreshed.get("review_id") == attachment.get(
        "hosted_review_id"
    ):
        local.record_projection(refreshed)
        report.projection_refreshed = True
    return report


__all__ = [
    "COMMENT_BODY_LIMIT",
    "CONFLICT_REASONS",
    "CONTENT_BINDINGS",
    "OUTCOME_SUMMARY_LIMIT",
    "PROJECTION_TARGET_LIMIT",
    "PROJECTION_THREAD_LIMIT",
    "READER_QUEUED_WRITES",
    "RECEIPT_KIND",
    "REPLAY_STATES",
    "RESOLVED_ACTION_KEEP",
    "RESOLVED_ACTION_MAX_AGE_DAYS",
    "UNRESOLVED_ACTION_LIMIT",
    "HostedFacts",
    "HostedPort",
    "LocalPort",
    "PreparedAction",
    "ReconnectReport",
    "action_wire",
    "anchor_fingerprint",
    "attached_identity",
    "conflict_reason",
    "hosted_reference",
    "parse_projection",
    "prepare_action",
    "reader_write_actions",
    "reconnect",
    "replay_request",
    "requeue_keeps_content",
]
