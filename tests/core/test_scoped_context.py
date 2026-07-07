"""Tests for the M4 scoped pull-context capability."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from atelier.core.capabilities.scoped_context import ScopedContextCapability, Subtask


@dataclass
class _FakeRecord:
    file_path: str
    symbol_name: str
    kind: str = "function"
    language: str = "python"
    qualified_name: str = ""
    signature: str = ""
    snippet: str = ""
    score: float | None = None
    provenance: str = "local"
    commit_sha: str = ""


class _FakeEngine:
    """Minimal engine exposing the search_symbols subset pull() calls."""

    def __init__(self, records: list[_FakeRecord]) -> None:
        self._records = records
        self.index_version = 0

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        mode: str = "auto",
        snippet: str = "none",
        snippet_lines: int = 8,
        file_glob: str | None = None,
        provenance_filter: str | None = None,
        **_: object,
    ) -> list[_FakeRecord]:
        recs = self._records
        if file_glob is not None:
            recs = [r for r in recs if r.file_path == file_glob or fnmatch.fnmatch(r.file_path, file_glob)]
        if provenance_filter is not None:
            recs = [r for r in recs if r.provenance == provenance_filter]
        return recs[:limit]

    def _current_index_version(self) -> int:
        return self.index_version


def _records() -> list[_FakeRecord]:
    return [
        _FakeRecord("src/a.py", "alpha", score=0.9, snippet="x" * 200, signature="def alpha(): ..."),
        _FakeRecord("src/b.py", "beta", score=0.8, snippet="y" * 200, signature="def beta(): ..."),
        _FakeRecord("src/c.py", "gamma", score=0.7, snippet="z" * 200, signature="def gamma(): ..."),
        _FakeRecord("src/legacy/old.py", "delta", score=0.6, snippet="w" * 200),
    ]


def test_pull_respects_budget() -> None:
    cap = ScopedContextCapability(_FakeEngine(_records()))
    result = cap.pull(Subtask(description="work on alpha", budget_tokens=200))
    assert result.total_tokens <= 200
    assert result.dropped_for_budget > 0  # heavy snippet fields were dropped
    assert result.chunks  # something survived


def test_excluded_paths_honoured() -> None:
    cap = ScopedContextCapability(_FakeEngine(_records()))
    result = cap.pull(Subtask(description="work", excluded_paths=["src/legacy"], budget_tokens=4000))
    assert all("legacy" not in c.path for c in result.chunks)
    assert any(e.reason.startswith("excluded_path") for e in result.excluded)


def test_rationale_cites_scores() -> None:
    cap = ScopedContextCapability(_FakeEngine(_records()))
    result = cap.pull(Subtask(description="work on alpha", budget_tokens=4000))
    assert "score=" in result.rationale


def test_cache_hit() -> None:
    cap = ScopedContextCapability(_FakeEngine(_records()))
    subtask = Subtask(description="work on alpha", budget_tokens=4000)
    first = cap.pull(subtask)
    second = cap.pull(subtask)
    assert first.provenance == "fresh"
    assert second.provenance == "cached"
    assert [c.path for c in first.chunks] == [c.path for c in second.chunks]


def test_cache_misses_when_index_version_changes() -> None:
    engine = _FakeEngine(_records())
    cap = ScopedContextCapability(engine)
    subtask = Subtask(description="work on alpha", budget_tokens=4000)

    first = cap.pull(subtask)
    engine.index_version = 1
    second = cap.pull(subtask)

    assert first.provenance == "fresh"
    assert second.provenance == "fresh"


def test_dead_end_filtered() -> None:
    cap = ScopedContextCapability(_FakeEngine(_records()))
    cap.mark_dead_end("beta src/b.py")
    result = cap.pull(Subtask(description="work", budget_tokens=4000))
    assert all(c.path != "src/b.py" for c in result.chunks)
    assert any(e.reason == "dead_end" for e in result.excluded)


def test_path_token_overlap_can_outrank_higher_raw_score() -> None:
    records = [
        _FakeRecord("src/misc/helpers.py", "helper", score=0.95),
        _FakeRecord("src/search/ranking.py", "rank_candidates", score=0.45),
    ]

    cap = ScopedContextCapability(_FakeEngine(records))
    result = cap.pull(
        Subtask(
            description="improve search ranking relevance",
            keywords=["search", "ranking"],
            budget_tokens=4000,
        )
    )

    assert result.chunks[0].path == "src/search/ranking.py"


def test_pull_seeds_local_results_only() -> None:
    records = [
        _FakeRecord("src/current.py", "current", score=0.5, provenance="local"),
        _FakeRecord(
            "history/fix.py",
            "historical_fix",
            kind="commit",
            score=1.0,
            provenance="commit",
            commit_sha="abc12345",
        ),
    ]

    cap = ScopedContextCapability(_FakeEngine(records))
    result = cap.pull(Subtask(description="current fix", budget_tokens=4000))

    assert [chunk.path for chunk in result.chunks] == ["src/current.py"]


def test_pull_surfaces_commit_provenance_for_history_queries() -> None:
    records = [
        _FakeRecord("src/current.py", "current", score=0.6, provenance="local"),
        _FakeRecord(
            "src/auth.py",
            "abc12345",
            kind="commit",
            qualified_name="Fixed auth session token leak on logout",
            signature="Fixed auth session token leak on logout",
            score=0.8,
            provenance="commit",
            commit_sha="abc12345deadbeef",
        ),
    ]

    cap = ScopedContextCapability(_FakeEngine(records))
    result = cap.pull(
        Subtask(
            description="which prior commit introduced the auth session regression",
            affected_paths=["src/auth.py"],
            budget_tokens=4000,
        )
    )

    commit_chunks = [chunk for chunk in result.chunks if chunk.provenance == "commit"]
    assert commit_chunks
    assert commit_chunks[0].commit_sha == "abc12345deadbeef"
    assert commit_chunks[0].path == "src/auth.py"
