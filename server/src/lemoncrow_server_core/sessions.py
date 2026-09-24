"""Session lifecycle and the acknowledged-revision contract.

A session binds an authenticated principal to a negotiated protocol, a set of
capabilities and -- once the client opens one -- a view. Every server tool call
is evaluated against a named ``view_revision``, which is what turns
"edit then read" from a timing assumption into a protocol guarantee:

* the client sends the revision it last had ACKed;
* the server compares it with the committed revision;
* **lower** means the client has not seen a commit that already happened, so it
  is refused as stale rather than served an answer from a future it has not
  observed;
* **higher** means the client is claiming a revision this server never
  committed -- a wrong view, a replayed session or a bug -- and is refused as
  unacknowledged.

Sessions are scoped on lookup to the organization *and the subject* that
opened them, so a valid session id is indistinguishable from a nonexistent one
to everybody else -- including the colleague at the next desk, who holds the
same roles and works for the same company.

That second half is the part this module used to store and then ignore. The
subject has always been recorded on :class:`Session`; every lookup compared
``org_id`` alone. A session id is not a secret -- it travels in a header, it is
logged, it is pasted into bug reports -- so "same tenant" was the entire gate
on reading another engineer's session, cancelling their in-flight work and
deleting the session out from under them. Lookup now takes a
:class:`SessionAccess`: who is asking, and whether they hold the grant that
lets them ask about somebody else.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from .context import Principal, new_opaque_id
from .errors import AgentAction, ErrorCode, ServerError
from .protocol import Negotiation

__all__ = ["Session", "SessionAccess", "SessionRegistry", "SessionStore"]

#: Bytes of entropy in the ``local_fs`` proof token. The token is what a client
#: has to get *into* a worktree before the server will read that worktree, so
#: its only requirement is that nothing already on the filesystem can equal it
#: by accident or by prediction.
_PROOF_TOKEN_BYTES: Final[int] = 32


@dataclass(frozen=True, slots=True)
class SessionAccess:
    """Who is reaching for a session, and whether they may cross subjects.

    Passed instead of a bare ``org_id`` so that every lookup states the subject
    it is made on behalf of. A caller cannot forget to check the owner, because
    there is no signature left that would let it: the registry has no method
    that takes an organization alone.

    ``cross_subject`` is never inferred. It is set by the one caller that has
    already resolved :attr:`~.identity.Permission.ACT_AS_SUBJECT` for the
    principal and is about to audit the crossing.
    """

    org_id: str
    subject: str
    cross_subject: bool = False

    def reaches(self, session: Session) -> bool:
        """Whether this access may see ``session`` at all."""
        if session.org_id != self.org_id:
            return False
        return self.cross_subject or session.subject == self.subject


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    org_id: str
    subject: str
    token_id: str
    protocol_version: str
    capabilities: frozenset[str]
    client_name: str
    client_version: str
    local_fs: bool
    created_at: float
    expires_at: float
    #: The nonce a ``local_fs`` offer has to be carrying. Minted here rather
    #: than chosen by the client because a client-chosen token proves only that
    #: the caller can *predict* the contents of a file under the root -- which
    #: every repository's ``.git/HEAD`` satisfies -- while a token the server
    #: invented can only get into the tree if the caller can write into it.
    #: Empty when the capability was not granted, and there is then nothing to
    #: prove against.
    local_fs_proof_token: str = ""
    view_id: str | None = None
    acknowledged_revision: int = 0

    def to_wire(self, *, caller: str) -> dict[str, Any]:
        """The session as ``caller`` may see it.

        ``caller`` is the subject the answer is being written for, and it is a
        required argument because the difference it makes is a secret: the
        proof nonce is a bearer credential for the ``local_fs`` capability, so
        it belongs to this session's owner and to nobody else. An administrator
        holding ``subject:act_as`` can resolve another engineer's session --
        that is a legitimate, audited act -- and gets the same payload with the
        nonce withheld, which is what a session that never negotiated the
        capability looks like anyway. Defaulting the argument would have made
        forgetting it leak; leaving it out is a ``TypeError``.
        """
        return {
            "session_id": self.session_id,
            "protocol_version": self.protocol_version,
            "capabilities": sorted(self.capabilities),
            "local_fs": self.local_fs,
            "local_fs_proof_token": self.local_fs_proof_token if caller == self.subject else "",
            "expires_at": round(self.expires_at, 3),
            "view_id": self.view_id,
            "acknowledged_view_revision": self.acknowledged_revision,
        }


class SessionStore(Protocol):
    """Session authority consumed by the shared route layer.

    The local implementation is process memory. Hosted deployments may provide
    a shared I/O-backed store; route code uses ``io_bound`` to keep that work off
    aiohttp's event loop without changing the wire contract.
    """

    @property
    def io_bound(self) -> bool: ...

    @property
    def live(self) -> int: ...

    def open(self, principal: Principal, negotiation: Negotiation) -> Session: ...

    def get(self, access: SessionAccess, session_id: str) -> Session: ...

    def touch(self, access: SessionAccess, session_id: str) -> Session: ...

    def bind_view(self, access: SessionAccess, session_id: str, view_id: str, view_revision: int) -> Session: ...

    def acknowledge(self, access: SessionAccess, session_id: str, view_revision: int) -> Session: ...

    def close(self, access: SessionAccess, session_id: str) -> bool: ...

    def sweep(self) -> int: ...

    def check_ready(self) -> None: ...

    def close_store(self) -> None: ...


class SessionRegistry:
    """Bounded, subject-scoped session table."""

    io_bound = False
    __slots__ = ("_by_id", "_clock", "_lock", "_max_sessions", "_ttl_s")

    def __init__(
        self,
        *,
        ttl_s: float,
        max_sessions: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._by_id: dict[str, Session] = {}
        self._ttl_s = ttl_s
        self._max_sessions = max_sessions
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def live(self) -> int:
        with self._lock:
            return len(self._by_id)

    def open(self, principal: Principal, negotiation: Negotiation) -> Session:
        now = self._clock()
        session = Session(
            session_id=new_opaque_id("sess"),
            org_id=principal.org_id,
            subject=principal.subject,
            token_id=principal.token_id,
            protocol_version=negotiation.protocol_version,
            capabilities=negotiation.capabilities,
            client_name=negotiation.client_name,
            client_version=negotiation.client_version,
            local_fs=negotiation.local_fs,
            local_fs_proof_token=secrets.token_hex(_PROOF_TOKEN_BYTES) if negotiation.local_fs else "",
            created_at=now,
            expires_at=now + self._ttl_s,
        )
        with self._lock:
            self._sweep_locked(now)
            if len(self._by_id) >= self._max_sessions:
                raise ServerError(
                    ErrorCode.CONCURRENCY_LIMIT,
                    "server is at its session limit",
                    details={"limit": self._max_sessions, "scope": "sessions"},
                )
            self._by_id[session.session_id] = session
        return session

    def get(self, access: SessionAccess, session_id: str) -> Session:
        """The session, if this access reaches it. Otherwise: it does not exist.

        One refusal for "no such session", "another tenant's session" and
        "another engineer's session", constructed in one place so the three
        cannot drift apart into an oracle.
        """
        now = self._clock()
        with self._lock:
            session = self._by_id.get(session_id)
            if session is None or not access.reaches(session):
                raise ServerError(
                    ErrorCode.SESSION_UNKNOWN,
                    "session is not available",
                    details={"session_id": session_id},
                    action=AgentAction.REOPEN_SESSION,
                )
            if session.expires_at <= now:
                del self._by_id[session_id]
                raise ServerError(
                    ErrorCode.SESSION_EXPIRED,
                    "session has expired",
                    details={"session_id": session_id},
                    action=AgentAction.REOPEN_SESSION,
                )
            return session

    def touch(self, access: SessionAccess, session_id: str) -> Session:
        session = self.get(access, session_id)
        renewed = replace(session, expires_at=self._clock() + self._ttl_s)
        with self._lock:
            self._by_id[session_id] = renewed
        return renewed

    def bind_view(self, access: SessionAccess, session_id: str, view_id: str, view_revision: int) -> Session:
        session = self.get(access, session_id)
        bound = replace(session, view_id=view_id, acknowledged_revision=view_revision)
        with self._lock:
            self._by_id[session_id] = bound
        return bound

    def acknowledge(self, access: SessionAccess, session_id: str, view_revision: int) -> Session:
        """Record the newest revision this session has been shown. A high-water mark.

        Monotonic by construction rather than by refusal, and the difference
        matters: an overlay write that is a *replay* legitimately reports an
        older revision -- the one it committed before its ACK was lost -- while
        the view, and this session, have already moved past it. Refusing that
        turned a correct idempotent retry into a 409 the client could never
        recover from, because the answer it needed was in the response being
        refused.

        Taking the maximum keeps the invariant the refusal was there to state
        ("what a session has observed never goes backwards") and drops only the
        refusal. Nothing downstream is weakened:
        :func:`require_acknowledged_revision` binds a call to the revision the
        *request* names against what the store has *committed*, and never reads
        this field -- so a session holding a high-water mark above a view that
        somehow regressed still gets every call refused, as
        ``view_revision_unacknowledged``.
        """
        session = self.get(access, session_id)
        if view_revision <= session.acknowledged_revision:
            return session
        acked = replace(session, acknowledged_revision=view_revision)
        with self._lock:
            self._by_id[session_id] = acked
        return acked

    def close(self, access: SessionAccess, session_id: str) -> bool:
        """Delete the session if this access reaches it. ``False`` otherwise.

        ``False`` is also the answer for a session id that never existed, which
        is what makes closing idempotent *and* keeps it from reporting whether
        a colleague is logged in.
        """
        with self._lock:
            session = self._by_id.get(session_id)
            if session is None or not access.reaches(session):
                return False
            del self._by_id[session_id]
            return True

    def sweep(self) -> int:
        with self._lock:
            return self._sweep_locked(self._clock())

    def _sweep_locked(self, now: float) -> int:
        expired = [sid for sid, session in self._by_id.items() if session.expires_at <= now]
        for sid in expired:
            del self._by_id[sid]
        return len(expired)

    def check_ready(self) -> None:
        return None

    def close_store(self) -> None:
        return None


def require_acknowledged_revision(
    *,
    session: Session,
    committed_revision: int,
    claimed_revision: int | None,
) -> int:
    """Bind one tool call to a revision, or refuse with the reason.

    ``claimed_revision`` is ``None`` only when the session has no view bound,
    in which case there is nothing to bind to and the call proceeds at 0.
    """
    if session.view_id is None:
        return 0
    if claimed_revision is None:
        raise ServerError(
            ErrorCode.VIEW_REVISION_UNACKNOWLEDGED,
            "a tool call against a bound view must name the view_revision it observed",
            details={"committed_view_revision": committed_revision},
            action=AgentAction.REFRESH_VIEW_REVISION,
        )
    if claimed_revision < committed_revision:
        raise ServerError(
            ErrorCode.VIEW_REVISION_STALE,
            "the client has not observed the current committed view revision",
            details={
                "claimed_view_revision": claimed_revision,
                "committed_view_revision": committed_revision,
            },
            action=AgentAction.REFRESH_VIEW_REVISION,
        )
    if claimed_revision > committed_revision:
        raise ServerError(
            ErrorCode.VIEW_REVISION_UNACKNOWLEDGED,
            "the client named a view revision this server has not committed",
            details={
                "claimed_view_revision": claimed_revision,
                "committed_view_revision": committed_revision,
            },
            action=AgentAction.REFRESH_VIEW_REVISION,
        )
    return committed_revision
