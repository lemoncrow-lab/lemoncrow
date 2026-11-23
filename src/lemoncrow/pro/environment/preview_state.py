"""Live state of interactive preview sessions shared by every serving process.

A hosted control plane runs several replicas behind one load balancer, so the
replica that started a preview is not necessarily the one a browser request
for its preview host reaches. Everything such a request needs beyond the
durable session row lives here, behind one protocol:

* which session side a preview host serves (host -> session -> worker);
* the one-time entry tickets, consumed atomically, so a ticket redeemed on one
  replica is refused on every other;
* the digests of the capabilities redeemed tickets minted (never the values);
* the end marker that refuses a stopped session everywhere, and the teardown
  claim that lets exactly one process stop the sides and write the recording;
* the recorded requests and the metering mark, so a session is metered and
  recorded once whichever replicas served it.

:class:`InMemoryPreviewState` serves one process (the author machine, a
single-replica server). The hosted server backs the same protocol with its
coordination Redis (``lemoncrow_server.review_preview.RedisPreviewState``).

Closer: every key carries a TTL of the session's remaining lifetime plus
:data:`STATE_RETENTION_SECONDS`, and teardown removes a session's keys at once
(:meth:`PreviewState.close`); only the end marker and the claim outlive it,
until their own TTL. A start's slot reservation (:meth:`PreviewState.reserve`)
lives a short TTL the start in flight keeps renewing: ``open`` turns it into the
session, a failed start gives it back (:meth:`PreviewState.unreserve`), and a
process that dies mid-start loses it at that TTL.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Literal, Protocol

Sides = Literal["old", "new"]
#: The two sides of a change a preview serves: ``old`` (base) and ``new`` (head).
SIDES: Final[tuple[Sides, Sides]] = ("old", "new")

#: How long shared state outlives a session's deadline (orphan reaping window).
STATE_RETENTION_SECONDS: Final[float] = 900.0


def side_key(session_id: str, side: str) -> str:
    """The shared-state key of one session side (tickets and capabilities)."""

    return f"{session_id}:{side}"


class PreviewState(Protocol):
    """Shared live state of preview sessions; every method is safe across processes."""

    def open(
        self,
        session_id: str,
        record: Mapping[str, Any],
        *,
        scope: str,
        hosts: Mapping[str, Mapping[str, Any]],
        started_at: float,
        ttl: float,
    ) -> None:
        """Publish a running session: its record, its host bindings and its metering mark."""
        ...

    def session(self, session_id: str) -> dict[str, Any] | None: ...

    def live(self, scope: str) -> list[str]:
        """Session ids opened under *scope* whose shared state has not been closed."""
        ...

    def reserve(
        self,
        scope: str,
        token: str,
        *,
        limit: int,
        ttl: float,
        exclude: Sequence[str] = (),
        release: Sequence[str] = (),
    ) -> int:
        """Atomically hold one of *limit* live-preview slots of *scope* for *token*; ``-1`` when held.

        *token* is the id the session will be opened under. Counted: every member of *scope* -- opened
        sessions and unexpired reservations alike -- except *token* itself, the *exclude* ids (sessions
        the caller saw ended) and those *release* ids (the caller's own sessions it tears down next) no
        other reservation has already released. With room, *token* joins *scope* until ``now + ttl``
        (``open`` under the same id replaces that deadline: the reservation becomes the session) and
        the released ids are marked released by *token* for as long; otherwise nothing changes and the
        occupied count is returned. Holding an already-held *token* again only extends it, so a start
        in flight renews its slot with the same call.
        """
        ...

    def unreserve(self, scope: str, token: str, *, release: Sequence[str] = ()) -> None:
        """Give back *token*'s reservation (never an opened session) and its release marks."""
        ...

    def binding(self, host: str) -> dict[str, Any] | None: ...

    def put_ticket(self, key: str, digest: str, target: str, *, expires_at: float, limit: int) -> None:
        """Keep one ticket digest for *key*; beyond *limit* outstanding, the oldest is dropped."""
        ...

    def take_ticket(self, key: str, digest: str) -> tuple[float, str] | None:
        """Atomically remove and return ``(expires_at, target)``: a ticket is taken at most once."""
        ...

    def add_capability(self, key: str, digest: str, *, ttl: float, limit: int) -> None: ...

    def has_capability(self, key: str, digest: str) -> bool: ...

    def end(self, session_id: str, state: str, reason: str, *, ttl: float) -> tuple[str, str]:
        """Mark the session ended; the first marker wins and is returned."""
        ...

    def ended(self, session_id: str) -> tuple[str, str] | None: ...

    def claim(self, session_id: str, *, ttl: float) -> bool:
        """True for exactly one caller: the one that stops the sides and records."""
        ...

    def append_event(self, session_id: str, event: Mapping[str, Any], *, limit: int) -> None:
        """Keep one served-request event; beyond *limit* only a dropped count grows."""
        ...

    def events(self, session_id: str) -> tuple[list[dict[str, Any]], int]: ...

    def meter(self, session_id: str, until: float, *, sides: int) -> tuple[float, int]:
        """Advance the metering mark to *until*; returns ``(compute seconds, tick)``.

        Concurrent callers never meter the same interval twice; ``tick`` is a
        per-session sequence number usable as an idempotency key.
        """
        ...

    def metered(self, session_id: str) -> tuple[float, float]:
        """``(mark, total compute seconds)`` metered so far."""
        ...

    def close(self, session_id: str) -> None:
        """Remove the session's record, bindings, tickets, capabilities, events and mark."""
        ...

    def put_note(self, key: str, value: Mapping[str, Any], *, ttl: float) -> None: ...

    def note(self, key: str) -> dict[str, Any] | None: ...


def _copy(value: Mapping[str, Any]) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(json.dumps(dict(value), sort_keys=True))
    return decoded


class InMemoryPreviewState:
    """One process's shared preview state (expiry on an injected clock)."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, tuple[float, dict[str, Any]]] = {}
        self._scopes: dict[str, dict[str, float]] = {}
        self._bindings: dict[str, tuple[float, dict[str, Any]]] = {}
        self._tickets: dict[str, OrderedDict[str, tuple[float, str]]] = {}
        self._capabilities: dict[str, tuple[float, OrderedDict[str, None]]] = {}
        self._ends: dict[str, tuple[float, tuple[str, str]]] = {}
        self._claims: dict[str, float] = {}
        self._events: dict[str, tuple[list[dict[str, Any]], int]] = {}
        self._meters: dict[str, list[float]] = {}
        self._notes: dict[str, tuple[float, dict[str, Any]]] = {}
        #: released session id -> (until, reservation token that released it)
        self._releases: dict[str, tuple[float, str]] = {}

    def _purge(self, now: float) -> None:
        """Drop expired entries nothing read again (the TTL a store like Redis enforces itself)."""

        for session_id in [name for name, entry in self._records.items() if entry[0] <= now]:
            del self._records[session_id]
            self._events.pop(session_id, None)
            self._meters.pop(session_id, None)
            for side in SIDES:
                self._tickets.pop(side_key(session_id, side), None)
                self._capabilities.pop(side_key(session_id, side), None)
        for table in (self._bindings, self._ends, self._notes, self._releases):
            for key in [name for name, entry in table.items() if entry[0] <= now]:
                del table[key]
        for key in [name for name, until in self._claims.items() if until <= now]:
            del self._claims[key]

    def _live_entry(self, table: dict[str, Any], key: str) -> Any:
        entry = table.get(key)
        if entry is None:
            return None
        if entry[0] <= self._clock():
            del table[key]
            return None
        return entry[1]

    def open(
        self,
        session_id: str,
        record: Mapping[str, Any],
        *,
        scope: str,
        hosts: Mapping[str, Mapping[str, Any]],
        started_at: float,
        ttl: float,
    ) -> None:
        now = self._clock()
        until = now + max(1.0, float(ttl))
        with self._lock:
            self._purge(now)
            self._records[session_id] = (until, {**_copy(record), "scope": scope, "hosts": sorted(hosts)})
            self._scopes.setdefault(scope, {})[session_id] = until
            for host, binding in hosts.items():
                self._bindings[host] = (until, {**_copy(binding), "session_id": session_id})
            self._meters[session_id] = [float(started_at), 0.0, 0.0]
            self._events.setdefault(session_id, ([], 0))

    def session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._live_entry(self._records, session_id)
            return _copy(found) if found is not None else None

    def live(self, scope: str) -> list[str]:
        now = self._clock()
        with self._lock:
            members = self._scopes.get(scope, {})
            for session_id in [sid for sid, until in members.items() if until <= now]:
                del members[session_id]
            return sorted(members)

    def reserve(
        self,
        scope: str,
        token: str,
        *,
        limit: int,
        ttl: float,
        exclude: Sequence[str] = (),
        release: Sequence[str] = (),
    ) -> int:
        now = self._clock()
        until = now + max(1.0, float(ttl))
        with self._lock:
            self._purge(now)
            members = self._scopes.setdefault(scope, {})
            for session_id in [sid for sid, deadline in members.items() if deadline <= now]:
                del members[session_id]
            skip = set(exclude)
            claimed = []
            for session_id in release:
                holder = self._live_entry(self._releases, session_id)
                if holder is None or holder == token:
                    skip.add(session_id)
                    claimed.append(session_id)
            held = members.get(token)
            occupied = sum(1 for sid in members if sid != token and sid not in skip)
            if held is None and occupied >= max(1, int(limit)):
                return occupied
            if held is None or held < until:
                members[token] = until
            for session_id in claimed:
                self._releases[session_id] = (until, token)
            return -1

    def unreserve(self, scope: str, token: str, *, release: Sequence[str] = ()) -> None:
        with self._lock:
            members = self._scopes.get(scope)
            if members is not None and token not in self._records:
                members.pop(token, None)
            for session_id in release:
                entry = self._releases.get(session_id)
                if entry is not None and entry[1] == token:
                    del self._releases[session_id]

    def binding(self, host: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._live_entry(self._bindings, host)
            return _copy(found) if found is not None else None

    def put_ticket(self, key: str, digest: str, target: str, *, expires_at: float, limit: int) -> None:
        now = self._clock()
        with self._lock:
            tickets = self._tickets.setdefault(key, OrderedDict())
            for stale in [name for name, row in tickets.items() if row[0] <= now]:
                del tickets[stale]
            tickets[digest] = (float(expires_at), target)
            while len(tickets) > max(1, limit):
                tickets.popitem(last=False)

    def take_ticket(self, key: str, digest: str) -> tuple[float, str] | None:
        with self._lock:
            tickets = self._tickets.get(key)
            return tickets.pop(digest, None) if tickets is not None else None

    def add_capability(self, key: str, digest: str, *, ttl: float, limit: int) -> None:
        until = self._clock() + max(1.0, float(ttl))
        with self._lock:
            current = self._live_entry(self._capabilities, key)
            digests: OrderedDict[str, None] = current if current is not None else OrderedDict()
            digests[digest] = None
            digests.move_to_end(digest)
            while len(digests) > max(1, limit):
                digests.popitem(last=False)
            self._capabilities[key] = (until, digests)

    def has_capability(self, key: str, digest: str) -> bool:
        with self._lock:
            current = self._live_entry(self._capabilities, key)
            return current is not None and digest in current

    def end(self, session_id: str, state: str, reason: str, *, ttl: float) -> tuple[str, str]:
        with self._lock:
            current = self._live_entry(self._ends, session_id)
            if current is not None:
                return tuple(current)  # type: ignore[return-value]
            self._ends[session_id] = (self._clock() + max(1.0, float(ttl)), (state, reason))
            return state, reason

    def ended(self, session_id: str) -> tuple[str, str] | None:
        with self._lock:
            current = self._live_entry(self._ends, session_id)
            return tuple(current) if current is not None else None  # type: ignore[return-value]

    def claim(self, session_id: str, *, ttl: float) -> bool:
        now = self._clock()
        with self._lock:
            until = self._claims.get(session_id)
            if until is not None and until > now:
                return False
            self._claims[session_id] = now + max(1.0, float(ttl))
            return True

    def append_event(self, session_id: str, event: Mapping[str, Any], *, limit: int) -> None:
        with self._lock:
            if session_id not in self._meters:
                return  # closed: nothing is recorded after teardown
            rows, dropped = self._events.get(session_id, ([], 0))
            if len(rows) < max(0, limit):
                rows.append(_copy(event))
            else:
                dropped += 1
            self._events[session_id] = (rows, dropped)

    def events(self, session_id: str) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows, dropped = self._events.get(session_id, ([], 0))
            return [dict(row) for row in rows], dropped

    def meter(self, session_id: str, until: float, *, sides: int) -> tuple[float, int]:
        with self._lock:
            mark = self._meters.get(session_id)
            if mark is None:
                return 0.0, 0
            if until <= mark[0]:
                return 0.0, int(mark[2])
            seconds = (float(until) - mark[0]) * max(0, sides)
            mark[0] = float(until)
            mark[1] += seconds
            mark[2] += 1
            return seconds, int(mark[2])

    def metered(self, session_id: str) -> tuple[float, float]:
        with self._lock:
            mark = self._meters.get(session_id)
            return (mark[0], mark[1]) if mark is not None else (0.0, 0.0)

    def close(self, session_id: str) -> None:
        with self._lock:
            entry = self._records.pop(session_id, None)
            record = entry[1] if entry is not None else {}
            members = self._scopes.get(str(record.get("scope") or ""))
            if members is not None:
                members.pop(session_id, None)
            for host in record.get("hosts") or ():
                self._bindings.pop(str(host), None)
            for side in SIDES:
                self._tickets.pop(side_key(session_id, side), None)
                self._capabilities.pop(side_key(session_id, side), None)
            self._events.pop(session_id, None)
            self._meters.pop(session_id, None)

    def put_note(self, key: str, value: Mapping[str, Any], *, ttl: float) -> None:
        with self._lock:
            self._notes[key] = (self._clock() + max(1.0, float(ttl)), _copy(value))

    def note(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._live_entry(self._notes, key)
            return _copy(found) if found is not None else None


__all__ = ["SIDES", "STATE_RETENTION_SECONDS", "InMemoryPreviewState", "PreviewState", "Sides", "side_key"]
