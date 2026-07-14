from __future__ import annotations

from lemoncrow.pro.capabilities.tool_supervision import relevance_ranking as rr


def test_score_lexical_rewards_term_coverage_over_repetition() -> None:
    scores = rr.score_lexical(
        "claude code",
        [
            "claude claude claude claude",  # 1 distinct term, repeated
            "claude code opus",  # both distinct terms present
            "unrelated text entirely",
        ],
    )
    assert scores[1] > scores[0] > scores[2] == 0.0


def test_score_lexical_empty_query_is_all_zero() -> None:
    assert rr.score_lexical("", ["anything", "else"]) == [0.0, 0.0]


def test_try_score_semantic_returns_none_without_a_configured_embedder(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("LEMONCROW_CODE_EMBEDDER", raising=False)
    monkeypatch.delenv("LEMONCROW_EMBEDDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert rr.try_score_semantic("query", ["a", "b"]) is None


def test_rank_and_select_pulls_a_distant_match_forward() -> None:
    # 100 irrelevant chunks, then one matching chunk pinned to a header, far
    # past what a small char budget would reach in original order.
    chunks = [(f"row {i}", "HEADER") for i in range(100)]
    chunks.append(("row 100 mentions claude code", "HEADER"))
    assembled, meta = rr.rank_and_select(chunks, query="claude code", char_budget=200)
    assert "claude code" in assembled
    assert meta["tier"] == "lexical"
    assert meta["chunks_kept"] >= 1
    # The header is pinned once, not once per selected row.
    assert assembled.count("HEADER") <= 1


def test_rank_and_select_marks_the_gap_between_non_adjacent_picks() -> None:
    chunks = [("alpha match", None)] + [(f"filler {i}", None) for i in range(5)] + [("beta match", None)]
    # Budget fits the two matches but not the filler chunks between them, so
    # the reassembled text must show a gap marker rather than silently
    # concatenating two chunks that weren't adjacent in the source.
    assembled, _meta = rr.rank_and_select(chunks, query="alpha beta", char_budget=25)
    assert "alpha match" in assembled
    assert "beta match" in assembled
    assert "..." in assembled


def test_rank_and_select_empty_chunks() -> None:
    assembled, meta = rr.rank_and_select([], query="x", char_budget=100)
    assert assembled == ""
    assert meta["chunks_kept"] == 0


def test_rank_and_select_tiny_budget_still_returns_the_best_match() -> None:
    chunks = [("irrelevant", None), ("the target phrase", None)]
    assembled, meta = rr.rank_and_select(chunks, query="target", char_budget=5)
    assert assembled  # never empty, even when nothing fully fits
    assert meta["chunks_kept"] == 1
