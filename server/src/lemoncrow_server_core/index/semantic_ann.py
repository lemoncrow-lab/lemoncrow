"""Deterministic generation-scoped ANN over semantic symbol/chunk units.

The index uses multi-table random-hyperplane LSH.  Bucket lookup is sublinear
in repository unit count; only a bounded collision-ranked candidate set is
cosine-scored exactly.  Small repositories intentionally use exact scoring --
there is no value in paying ANN overhead to avoid scanning a few dozen units.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .analysis import EMBEDDING_DIM, SEMANTIC_EMBEDDING_VERSION, cosine
from .contracts import SemanticMatch, SemanticSearchResult, SemanticUnit

__all__ = ["SEMANTIC_ANN_FORMAT", "SemanticAnnIndex", "SemanticAnnItem"]

SEMANTIC_ANN_FORMAT: Final[str] = "lemoncrow-semantic-ann/1"
_TABLES: Final[int] = 12
_BITS: Final[int] = 12
_EXACT_THRESHOLD: Final[int] = 256
_MIN_CANDIDATES: Final[int] = 256
_CANDIDATES_PER_HIT: Final[int] = 64


@dataclass(frozen=True, slots=True, order=True)
class SemanticAnnItem:
    path: str
    digest: str
    unit: SemanticUnit


def _sign(table: int, bit: int, dim: int) -> float:
    seed = f"{SEMANTIC_ANN_FORMAT}:{table}:{bit}:{dim}".encode("ascii")
    return 1.0 if hashlib.blake2b(seed, digest_size=1).digest()[0] & 1 else -1.0


_PLANES: Final[tuple[tuple[tuple[float, ...], ...], ...]] = tuple(
    tuple(tuple(_sign(table, bit, dim) for dim in range(EMBEDDING_DIM)) for bit in range(_BITS))
    for table in range(_TABLES)
)


def signatures(vector: Sequence[float]) -> tuple[int, ...]:
    """*vector*'s bucket signature in every table."""
    return tuple(_signature(vector, table) for table in range(_TABLES))


def _signature(vector: Sequence[float], table: int) -> int:
    signature = 0
    for bit, plane in enumerate(_PLANES[table]):
        dot = sum(value * plane[dim] for dim, value in enumerate(vector[:EMBEDDING_DIM]))
        if dot >= 0.0:
            signature |= 1 << bit
    return signature


def _neighbor_signatures(signature: int, *, distance: int) -> Iterable[int]:
    if distance == 0:
        yield signature
        return
    if distance == 1:
        for bit in range(_BITS):
            yield signature ^ (1 << bit)
        return
    if distance == 2:
        for left in range(_BITS):
            for right in range(left + 1, _BITS):
                yield signature ^ (1 << left) ^ (1 << right)


class SemanticAnnIndex:
    """Immutable ANN index for one generation or one View overlay."""

    __slots__ = ("_buckets", "_items")

    def __init__(
        self,
        items: Sequence[SemanticAnnItem],
        buckets: Mapping[tuple[int, int], Sequence[int]] | None = None,
        *,
        signatures_of: Callable[[Sequence[float]], tuple[int, ...]] = signatures,
    ) -> None:
        self._items = tuple(items)
        if buckets is None:
            built: dict[tuple[int, int], list[int]] = {}
            for index, item in enumerate(self._items):
                vector = item.unit.embedding
                if len(vector) != EMBEDDING_DIM:
                    continue
                for table, signature in enumerate(signatures_of(vector)):
                    built.setdefault((table, signature), []).append(index)
            self._buckets = {key: tuple(values) for key, values in built.items()}
        else:
            normalized: dict[tuple[int, int], tuple[int, ...]] = {}
            for key, values in buckets.items():
                table, signature = key
                if table < 0 or table >= _TABLES or signature < 0 or signature >= (1 << _BITS):
                    raise ValueError("invalid semantic ANN bucket key")
                indices = tuple(int(value) for value in values)
                if any(index < 0 or index >= len(self._items) for index in indices):
                    raise ValueError("semantic ANN bucket references an unknown item")
                normalized[(table, signature)] = indices
            self._buckets = normalized

    @classmethod
    def build(
        cls,
        items: Iterable[SemanticAnnItem],
        *,
        signatures_of: Callable[[Sequence[float]], tuple[int, ...]] = signatures,
    ) -> SemanticAnnIndex:
        """*signatures_of* may memoize :func:`signatures`; it must return the same."""
        return cls(tuple(sorted(items)), signatures_of=signatures_of)

    @property
    def items(self) -> tuple[SemanticAnnItem, ...]:
        return self._items

    @property
    def size(self) -> int:
        return len(self._items)

    @property
    def paths(self) -> frozenset[str]:
        return frozenset(item.path for item in self._items)

    @property
    def path_digests(self) -> frozenset[tuple[str, str]]:
        return frozenset((item.path, item.digest) for item in self._items)

    def query(
        self,
        vector: Sequence[float],
        limit: int,
        *,
        excluded_paths: frozenset[str] = frozenset(),
    ) -> SemanticSearchResult:
        if limit < 1 or len(vector) != EMBEDDING_DIM or not self._items:
            return SemanticSearchResult((), 0, len(self._items))
        if len(self._items) <= _EXACT_THRESHOLD:
            candidates = [index for index, item in enumerate(self._items) if item.path not in excluded_paths]
        else:
            votes: dict[int, int] = {}
            target = max(_MIN_CANDIDATES, limit * _CANDIDATES_PER_HIT)
            for distance in (0, 1, 2):
                for table in range(_TABLES):
                    signature = _signature(vector, table)
                    for neighbor in _neighbor_signatures(signature, distance=distance):
                        for index in self._buckets.get((table, neighbor), ()):
                            if self._items[index].path in excluded_paths:
                                continue
                            votes[index] = votes.get(index, 0) + 1
                if len(votes) >= target:
                    break
            if not votes:
                return SemanticSearchResult((), 0, len(self._items))
            candidates = [
                index
                for index, _votes in sorted(
                    votes.items(),
                    key=lambda pair: (-pair[1], self._items[pair[0]].path, self._items[pair[0]].unit.unit_id),
                )[:target]
            ]

        scored = [
            (
                cosine(vector, self._items[index].unit.embedding),
                self._items[index],
            )
            for index in candidates
        ]
        scored = [pair for pair in scored if pair[0] > 0.0]
        scored.sort(key=lambda pair: (-pair[0], pair[1].path, pair[1].unit.unit_id))
        matches = tuple(
            SemanticMatch(
                path=item.path,
                digest=item.digest,
                unit_id=item.unit.unit_id,
                start_line=item.unit.start_line,
                end_line=item.unit.end_line,
                symbol=item.unit.symbol,
                score=round(score, 6),
            )
            for score, item in scored[:limit]
        )
        return SemanticSearchResult(matches, len(candidates), len(self._items))

    def exact(self, vector: Sequence[float], limit: int) -> SemanticSearchResult:
        """Benchmark oracle for ANN quality; not used by production queries."""
        candidates = tuple(range(len(self._items)))
        scored = [(cosine(vector, item.unit.embedding), item) for item in self._items]
        scored = [pair for pair in scored if pair[0] > 0.0]
        scored.sort(key=lambda pair: (-pair[0], pair[1].path, pair[1].unit.unit_id))
        return SemanticSearchResult(
            tuple(
                SemanticMatch(
                    path=item.path,
                    digest=item.digest,
                    unit_id=item.unit.unit_id,
                    start_line=item.unit.start_line,
                    end_line=item.unit.end_line,
                    symbol=item.unit.symbol,
                    score=round(score, 6),
                )
                for score, item in scored[:limit]
            ),
            len(candidates),
            len(self._items),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "format": SEMANTIC_ANN_FORMAT,
            "embedding_version": SEMANTIC_EMBEDDING_VERSION,
            "items": [[item.path, item.digest, item.unit.to_json()] for item in self._items],
            "buckets": [
                [table, signature, list(indices)] for (table, signature), indices in sorted(self._buckets.items())
            ],
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> SemanticAnnIndex:
        if raw.get("format") != SEMANTIC_ANN_FORMAT or raw.get("embedding_version") != SEMANTIC_EMBEDDING_VERSION:
            raise ValueError("stale semantic ANN format")
        raw_items = raw.get("items")
        raw_buckets = raw.get("buckets")
        if not isinstance(raw_items, list) or not isinstance(raw_buckets, list):
            raise ValueError("invalid semantic ANN payload")
        items = tuple(
            SemanticAnnItem(path=str(item[0]), digest=str(item[1]), unit=SemanticUnit.from_json(item[2]))
            for item in raw_items
        )
        if any(
            item.unit.embedding_version != SEMANTIC_EMBEDDING_VERSION or len(item.unit.embedding) != EMBEDDING_DIM
            for item in items
        ):
            raise ValueError("semantic ANN item uses an unsupported embedding version")
        buckets: dict[tuple[int, int], tuple[int, ...]] = {}
        for row in raw_buckets:
            if not isinstance(row, list) or len(row) != 3 or not isinstance(row[2], list):
                raise ValueError("invalid semantic ANN bucket")
            buckets[(int(row[0]), int(row[1]))] = tuple(int(index) for index in row[2])
        return cls(items, buckets)
