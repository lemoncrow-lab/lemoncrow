"""Cheap, read-only workspace search planning and per-member rank calibration.

A repository's search index remains its own source of truth.  This planner
uses directory-name metadata only: no file walking, persisted catalogue,
network requests, or user-specific location history.  Coverage is decided by
the caller, never inferred from a nonempty result.
"""

from __future__ import annotations

import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from ..workspace_members import WorkspaceRepo

_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
_DATA_NAMES: Final[frozenset[str]] = frozenset(
    {"data", "datasets", "dataset", "corpus", "catalog", "catalogue", "artifacts", "assets"}
)
_DATA_QUERIES: Final[frozenset[str]] = frozenset(
    {"data", "dataset", "datasets", "corpus", "catalog", "catalogue", "csv", "json", "sku", "products", "records"}
)
FAST_FIRST_WAVE: Final[int] = 8
_MAX_README_BYTES: Final[int] = 24 * 1024
_GENERIC: Final[frozenset[str]] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "when",
        "how",
        "what",
        "file",
        "code",
        "system",
        "service",
        "project",
        "repository",
        "platform",
        "using",
        "into",
        "user",
        "users",
    }
)


@lru_cache(maxsize=256)
def _description_terms(path: Path, modified_ns: int, size: int) -> tuple[frozenset[str], frozenset[str]]:
    """Bounded local README summary; keyed on metadata so edits invalidate it.

    Never follows symlinks, scans arbitrary source files, writes a new index,
    stores README text, or includes descriptions in tool results.  This is a
    cheap routing hint, not evidence that a repository contains source matches.
    """
    # A README can be atomically replaced between the caller's symlink check
    # and this read. Open the final component without following symlinks and
    # verify the same regular-file metadata on the opened descriptor. O_NONBLOCK
    # prevents a substituted FIFO from hanging a search request indefinitely.
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return frozenset(), frozenset()
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mtime_ns != modified_ns or info.st_size != size:
            return frozenset(), frozenset()
        with os.fdopen(fd, "rb") as stream:
            fd = -1  # Ownership transferred to the stream context manager.
            source = stream.read(min(size, _MAX_README_BYTES)).decode("utf-8", "replace")
    except OSError:
        return frozenset(), frozenset()
    finally:
        if fd >= 0:
            os.close(fd)
    title = source[:1200]
    return frozenset(_WORD.findall(title.lower())), frozenset(_WORD.findall(source.lower()))


def _repo_description(repo: WorkspaceRepo) -> tuple[frozenset[str], frozenset[str]]:
    path = repo.root / "README.md"
    try:
        if path.is_symlink():
            return frozenset(), frozenset()
        stat = path.stat()
        if not path.is_file() or stat.st_size > 2_000_000:
            return frozenset(), frozenset()
    except OSError:
        return frozenset(), frozenset()
    return _description_terms(path, stat.st_mtime_ns, stat.st_size)


@dataclass(frozen=True, slots=True)
class MemberHint:
    """A repository's stable, structural search hints (not retrieved source)."""

    repo: WorkspaceRepo
    role: str
    relevance: int


def hints(query: str, repos: Sequence[WorkspaceRepo]) -> list[MemberHint]:
    words = set(_WORD.findall(query.lower()))
    wants_data = bool(words & _DATA_QUERIES)
    ranked: list[MemberHint] = []
    for repo in repos:
        relpath = repo.relpath.lower()
        parts = [part for chunk in relpath.split("/") for part in _WORD.findall(chunk)]
        is_dataset = bool(set(parts) & _DATA_NAMES)
        matched = len(words.intersection(parts))
        relevance = matched * 6 + (8 if query.lower().strip() == relpath else 0)
        if wants_data and is_dataset:
            relevance += 3
        elif is_dataset and not wants_data:
            relevance -= 3
        ranked.append(MemberHint(repo, "dataset" if is_dataset else "code", relevance))
    return sorted(ranked, key=lambda hint: (-hint.relevance, hint.repo.relpath))


def choose_first_wave(query: str, repos: Sequence[WorkspaceRepo], *, cap: int = FAST_FIRST_WAVE) -> tuple[str, ...]:
    """Order a bounded first wave.  The caller expands to the rest on no hits."""

    words = set(_WORD.findall(query.lower())) - _GENERIC
    candidates = []
    for hint in hints(query, repos):
        lead, body = _repo_description(hint.repo)
        # Path tokens remain the strongest clue.  README descriptions break
        # unexplained alphabetic ties without requiring a root-wide code scan.
        score = hint.relevance + 3 * len(words & lead) + len(words & body)
        candidates.append((score, hint.repo.relpath))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return tuple(path for _score, path in candidates[:cap])


def normalize_member_ranks(
    results: Sequence[tuple[WorkspaceRepo, Sequence[Mapping[str, Any]]]], query: str
) -> list[dict[str, Any]]:
    """Merge fixed-weight file evidence from independent Runtime Lite indexes.

    LiteIndex.search already combines symbol, filename, import, and locally
    normalized lexical evidence into its score. Renormalizing every repository's
    top result to nearly the same value discards this evidence: incidental text
    matches can bury exact implementation hits. Preserve the local score order
    across repositories, while publishing a bounded [0, 1) workspace_score.
    Repository names are used only to break exact evidence ties, never to
    promote weak source hits over stronger ones.

    The query parameter remains for compatibility with existing callers.
    """
    combined: list[dict[str, Any]] = []
    for _repo, raw_hits in results:
        for raw_hit in raw_hits:
            if not isinstance(raw_hit, Mapping):
                continue
            hit = dict(raw_hit)
            evidence = _positive_score(hit.get("score"))
            hit["workspace_score"] = round(evidence / (evidence + 120.0), 6)
            combined.append(hit)
    return sorted(
        combined,
        key=lambda hit: (-_positive_score(hit.get("score")), str(hit.get("path") or "")),
    )


def _positive_score(value: object) -> float:
    try:
        score = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    return max(score, 0.0) if math.isfinite(score) else 0.0
