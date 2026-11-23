"""Reads one request repeats, answered once (P9-6).

A single Reader request (the Brief's projection, readiness) asks the same questions many times over: which paths a
Snapshot changed, what the trusted base says, which digest an artifact key maps to. On a database a round trip away
that is hundreds of statements. This module holds the answers for the length of one request and no longer.

Scope. The memo lives in a :class:`contextvars.ContextVar` that :func:`reads_once` sets on entry and resets on exit. It
is never attached to a store, an app or a principal, so no request is answered from another request's reads and no
viewer can see another viewer's answer. Outside a :func:`reads_once` block every function here just reads.

Two kinds of entry:

* :func:`read_once` -- an immutable Snapshot artifact (never changes, kept for the whole block);
* :func:`facts_once` -- a fact that other writes can change (the revisions of a Review, the action ledger, the trusted
  base). It records the store's ``write_epoch`` before it reads and is served only while the epoch is unchanged, so a
  request that writes between two questions gets the second one fresh. A store with no ``write_epoch`` is never memoized.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Iterator
from typing import Any, cast

_READS: contextvars.ContextVar[dict[tuple[Any, ...], Any] | None] = contextvars.ContextVar(
    "lemoncrow_review_request_reads", default=None
)


@contextlib.contextmanager
def reads_once() -> Iterator[None]:
    """Inside this block each :func:`read_once` and :func:`facts_once` question is answered once."""

    if _READS.get() is not None:
        yield
        return
    token = _READS.set({})
    try:
        yield
    finally:
        _READS.reset(token)


def active() -> bool:
    return _READS.get() is not None


def read_once[T](key: tuple[Any, ...], read: Callable[[], T]) -> T:
    reads = _READS.get()
    if reads is None:
        return read()
    slot = ("immutable", *key)
    if slot not in reads:
        reads[slot] = read()
    return cast(T, reads[slot])


def facts_once[T](
    store: Any, key: tuple[str, ...], read: Callable[[], T], *, copy: Callable[[T], T] | None = None
) -> T:
    """``read()`` once while ``store.write_epoch`` stays put; ``copy`` hands each caller its own value."""

    reads = _READS.get()
    epoch = getattr(store, "write_epoch", None)
    if reads is None or epoch is None:
        return read()
    slot = ("facts", id(store), *key)
    held = reads.get(slot)
    if held is not None and held[0] == epoch:
        value = cast(T, held[1])
    else:
        # The epoch is taken before the read: a write that lands during it leaves the entry already out of date.
        value = read()
        reads[slot] = (epoch, value)
    return value if copy is None else copy(value)


def source_tree_once(
    store: Any, review_id: str, revision_id: str, side: str, read: Callable[[], Any] | None = None
) -> Any:
    """One side's immutable source-tree manifest of a revision, read once per request (callers only read it).

    One key for every reader of the manifest, so the Reader's projection, the surface materializer and the batch guard
    share the answer instead of each loading and parsing the whole manifest again.
    """

    return read_once(
        ("source_tree", review_id, revision_id, side),
        read or (lambda: store.read_source_tree_artifact(review_id, revision_id, side=side)),
    )


#: Most characters of document text one request holds. Past it a document is read again on demand, never dropped.
# lc-debt: a fixed 32 MiB heuristic bound; upgrade to a byte-weighted LRU if a request ever re-reads hot documents.
DOCUMENT_CHARS_CAP = 32 * 1024 * 1024

_HELD_CHARS = ("document_chars",)


def document_once(key: tuple[Any, ...], read: Callable[[], str | None]) -> str | None:
    """One Snapshot document's text, read once per request while the request holds under :data:`DOCUMENT_CHARS_CAP`.

    The memo changes speed only: once the held text passes the cap, further documents are read each time they are asked
    for, with the same result. Outside a request this just reads.
    """

    reads = _READS.get()
    if reads is None:
        return read()
    slot = ("document", *key)
    if slot in reads:
        return cast("str | None", reads[slot])
    text = read()
    held = cast(int, reads.get(_HELD_CHARS, 0))
    size = len(text) if text else 0
    if held + size <= DOCUMENT_CHARS_CAP:
        reads[slot] = text
        reads[_HELD_CHARS] = held + size
    return text


# -- artifact digests ---------------------------------------------------------------------------------------------


class ArtifactDigests:
    """Key-to-digest mappings loaded by prefix, each good only for the write epoch it was read at."""

    __slots__ = ("_listed",)

    def __init__(self) -> None:
        self._listed: list[tuple[str, int, dict[str, str]]] = []

    def add(self, prefix: str, epoch: int, digests: dict[str, str]) -> None:
        self._listed.append((prefix, epoch, digests))

    def lookup(self, key: str, epoch: int) -> tuple[bool, str | None]:
        """``(True, digest or None)`` when a listing of this epoch covers ``key`` (absent from it means absent)."""

        for prefix, listed_at, digests in self._listed:
            if listed_at == epoch and key.startswith(prefix):
                return True, digests.get(key)
        return False, None


def digest_scope(scope: tuple[str, ...]) -> ArtifactDigests | None:
    """This request's known digests for one (organization, repository), or ``None`` outside a request."""

    reads = _READS.get()
    if reads is None:
        return None
    held = reads.setdefault(("digests", *scope), ArtifactDigests())
    return cast(ArtifactDigests, held)
