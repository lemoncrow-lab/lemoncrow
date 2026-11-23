"""Interactive preview session records (Phase 9R item 16).

One session is one reviewer exploring the base and head of one exact Snapshot
side by side. The durable record lives in the Review store's
``review_derived`` rows (kind :data:`SESSION_KIND`, one row per session on its
Snapshot), so Snapshot deletion is its closer and no schema is added.

The session token is a bearer capability minted once and returned once to the
viewer that started the session. Only its digest is stored, bound to the
session id; :func:`token_matches` compares in constant time. A token is never
logged, never placed in a URL, and never written to the recording.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, cast

from lemoncrow.pro.environment.preview_state import SIDES, Sides

INTERACTIVE_PREVIEW_ENTRY: Final[str] = "visual.interactive_preview"
SESSION_KIND: Final[str] = "interactive_preview_session"
SESSION_SCHEMA_VERSION: Final[int] = 1

#: Default and ceiling of one session's lifetime, and the shortest session worth
#: starting (below it a budget-capped session is refused as Limited by budget).
DEFAULT_TTL_SECONDS: Final[int] = 300
MAX_TTL_SECONDS: Final[int] = 900
MIN_SESSION_SECONDS: Final[int] = 60

SessionState = Literal["starting", "running", "expired", "stopped", "superseded", "closed", "failed"]
SESSION_STATES: Final[tuple[str, ...]] = ("starting", "running", "expired", "stopped", "superseded", "closed", "failed")
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"expired", "stopped", "superseded", "closed", "failed"})

_TOKEN_CONTEXT: Final[bytes] = b"lemoncrow-interactive-preview-token\0"


def new_session_id() -> str:
    return "ips_" + secrets.token_hex(12)


def new_token() -> str:
    """An unguessable bearer capability (256 bits)."""

    return secrets.token_urlsafe(32)


def token_digest(session_id: str, token: str) -> str:
    """Digest bound to the session id: a token for one session never matches another."""

    return hashlib.sha256(_TOKEN_CONTEXT + session_id.encode() + b"\0" + token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PreviewSide:
    """One running side of a session (``old`` = base, ``new`` = head)."""

    side: Sides
    status: Literal["running", "failed", "stopped"] = "running"
    fingerprint: Mapping[str, Any] = field(default_factory=dict)
    fingerprint_digest: str = ""
    detail: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "status": self.status,
            "fingerprint": dict(self.fingerprint),
            "fingerprint_digest": self.fingerprint_digest,
            "detail": self.detail,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> PreviewSide:
        side = str(raw.get("side") or "")
        if side not in SIDES:
            raise ValueError(f"unknown preview side {side!r}")
        status = str(raw.get("status") or "stopped")
        fingerprint = raw.get("fingerprint")
        return cls(
            side=side,
            status=cast(Any, status if status in {"running", "failed", "stopped"} else "stopped"),
            fingerprint=dict(fingerprint) if isinstance(fingerprint, Mapping) else {},
            fingerprint_digest=str(raw.get("fingerprint_digest") or ""),
            detail=str(raw.get("detail") or "")[:500],
        )


@dataclass(frozen=True, slots=True)
class PreviewSession:
    """The durable record of one interactive preview session."""

    id: str
    review_id: str
    revision_id: str
    viewer_id: str
    token_digest: str
    created_at: float
    expires_at: float
    ttl_seconds: int
    state: SessionState
    run_on: str
    executor_class: str
    plan_digest: str = ""
    entry_id: str = INTERACTIVE_PREVIEW_ENTRY
    mode: str = ""
    #: The environment.toml preview this session serves (``""``: review.yaml or a static build).
    preview: str = ""
    sides: tuple[PreviewSide, ...] = ()
    comparable: bool = False
    comparability: str = ""
    stop_reason: str = ""
    stopped_at: float | None = None
    #: The TTL was shortened to fit the remaining per-Review budget.
    limited_by_budget: bool = False
    compute_seconds: float = 0.0
    events: int = 0
    recording_evidence_id: str = ""
    version: int = SESSION_SCHEMA_VERSION

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def side(self, side: str) -> PreviewSide | None:
        return next((row for row in self.sides if row.side == side), None)

    def with_state(self, state: SessionState, *, reason: str = "", at: float | None = None) -> PreviewSession:
        return replace(self, state=state, stop_reason=reason or self.stop_reason, stopped_at=at)

    def to_wire(self, *, durable: bool = False) -> dict[str, Any]:
        """Public shape; ``durable`` adds the token digest for the store row only."""

        wire: dict[str, Any] = {
            "version": self.version,
            "id": self.id,
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "viewer_id": self.viewer_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "ttl_seconds": self.ttl_seconds,
            "state": self.state,
            "run_on": self.run_on,
            "executor_class": self.executor_class,
            "plan_digest": self.plan_digest,
            "entry_id": self.entry_id,
            "mode": self.mode,
            "preview": self.preview,
            "sides": [row.to_wire() for row in self.sides],
            "comparable": self.comparable,
            "comparability": self.comparability,
            "stop_reason": self.stop_reason,
            "stopped_at": self.stopped_at,
            "limited_by_budget": self.limited_by_budget,
            "compute_seconds": round(self.compute_seconds, 3),
            "events": self.events,
            "recording_evidence_id": self.recording_evidence_id,
        }
        if durable:
            wire["token_digest"] = self.token_digest
        return wire

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> PreviewSession:
        version = int(raw.get("version") or 0)
        if version != SESSION_SCHEMA_VERSION:
            raise ValueError(f"interactive preview session version {version} is not supported")
        state = str(raw.get("state") or "")
        if state not in SESSION_STATES:
            raise ValueError(f"unknown interactive preview session state {state!r}")
        stopped = raw.get("stopped_at")
        sides = raw.get("sides")
        return cls(
            id=str(raw["id"]),
            review_id=str(raw["review_id"]),
            revision_id=str(raw["revision_id"]),
            viewer_id=str(raw.get("viewer_id") or ""),
            token_digest=str(raw.get("token_digest") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            expires_at=float(raw.get("expires_at") or 0.0),
            ttl_seconds=int(raw.get("ttl_seconds") or 0),
            state=cast(SessionState, state),
            run_on=str(raw.get("run_on") or ""),
            executor_class=str(raw.get("executor_class") or ""),
            plan_digest=str(raw.get("plan_digest") or ""),
            entry_id=str(raw.get("entry_id") or INTERACTIVE_PREVIEW_ENTRY),
            mode=str(raw.get("mode") or ""),
            preview=str(raw.get("preview") or "")[:64],
            sides=tuple(
                PreviewSide.from_wire(row)
                for row in (sides if isinstance(sides, Sequence) else ())
                if isinstance(row, Mapping)
            ),
            comparable=raw.get("comparable") is True,
            comparability=str(raw.get("comparability") or ""),
            stop_reason=str(raw.get("stop_reason") or ""),
            stopped_at=None if stopped is None else float(stopped),
            limited_by_budget=raw.get("limited_by_budget") is True,
            compute_seconds=float(raw.get("compute_seconds") or 0.0),
            events=int(raw.get("events") or 0),
            recording_evidence_id=str(raw.get("recording_evidence_id") or ""),
            version=version,
        )


def token_matches(session: PreviewSession, token: str) -> bool:
    """Constant-time check of a presented token against the stored digest."""

    if not token or not session.token_digest:
        return False
    return hmac.compare_digest(token_digest(session.id, token), session.token_digest)


def comparability(fingerprints: Mapping[str, Mapping[str, Any]]) -> tuple[bool, str]:
    """Base and head are comparable only under the same environment fingerprint.

    The fingerprint is the R4 :class:`~lemoncrow.pro.environment.execution.EnvironmentFingerprint`
    wire shape. Differences are named so the pair can say *why* it is not comparable.
    """

    from lemoncrow.pro.environment.execution import EnvironmentFingerprint

    old, new = fingerprints.get("old"), fingerprints.get("new")
    if not old or not new:
        return False, "not comparable: a side has no environment fingerprint"
    try:
        base, head = EnvironmentFingerprint.from_wire(old), EnvironmentFingerprint.from_wire(new)
    except (TypeError, ValueError) as exc:
        return False, f"not comparable: {exc}"
    differences = base.differences(head)
    if differences:
        return False, "not comparable: base and head ran under different environments (" + ", ".join(differences) + ")"
    return True, "comparable: base and head ran under the same environment fingerprint"


class PreviewSessionStore:
    """Persist session records as ``review_derived`` rows of their Snapshot."""

    def __init__(self, store: Any) -> None:
        self._store = store

    @staticmethod
    def _scope(session_id: str) -> str:
        return f"session:{session_id}"

    def put(self, session: PreviewSession) -> PreviewSession:
        from lemoncrow.pro.capabilities.review.store import DerivedResult

        payload = session.to_wire(durable=True)
        stamp = hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest()[:32]
        self._store.put_derived_results(
            [
                DerivedResult(
                    review_id=session.review_id,
                    revision_id=session.revision_id,
                    kind=SESSION_KIND,
                    scope_key=self._scope(session.id),
                    stamp=stamp,
                    payload=payload,
                )
            ]
        )
        return session

    def list_for(self, revision_ids: Iterable[str]) -> list[PreviewSession]:
        out: list[PreviewSession] = []
        for (_revision_id, scope), (_stamp, payload) in self._store.derived_results(
            SESSION_KIND, list(revision_ids)
        ).items():
            if not scope.startswith("session:"):
                continue
            try:
                out.append(PreviewSession.from_wire(payload))
            except (KeyError, TypeError, ValueError):
                # An unreadable row is never served as a live session.
                continue
        return sorted(out, key=lambda row: (row.created_at, row.id))

    def for_review(self, review_id: str) -> list[PreviewSession]:
        return self.list_for(item.id for item in self._store.list_revisions(review_id))

    def get(self, review_id: str, session_id: str) -> PreviewSession | None:
        return next((row for row in self.for_review(review_id) if row.id == session_id), None)


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "INTERACTIVE_PREVIEW_ENTRY",
    "MAX_TTL_SECONDS",
    "MIN_SESSION_SECONDS",
    "SESSION_KIND",
    "SIDES",
    "TERMINAL_STATES",
    "PreviewSession",
    "PreviewSessionStore",
    "PreviewSide",
    "SessionState",
    "comparability",
    "new_session_id",
    "new_token",
    "token_digest",
    "token_matches",
]
