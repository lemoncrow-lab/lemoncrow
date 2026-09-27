"""N12: tunable tri-signal fusion weights + P/R/F1 evaluation harness tests."""

from __future__ import annotations

import math

from lemoncrow.pro.capabilities.code_context.embedding import (
    FusionWeights,
    SemanticSearchRanker,
)
from lemoncrow.pro.capabilities.code_context.eval_harness import (
    EvalCase,
    evaluate_cases,
    score_ranking,
)
from lemoncrow.pro.capabilities.code_context.models import SymbolRecord


def _symbol(sym_id: str, *, file_path: str = "src/m.py", start_line: int = 1) -> SymbolRecord:
    return SymbolRecord(
        symbol_id=sym_id,
        repo_id="repo",
        file_path=file_path,
        language="python",
        symbol_name=sym_id,
        qualified_name=f"m.{sym_id}",
        kind="function",
        signature=f"def {sym_id}()",
        start_byte=0,
        end_byte=10,
        start_line=start_line,
        end_line=start_line + 1,
        content_hash=f"hash-{sym_id}",
    )


class _NullEmbedder:
    """Disabled embedder (dim=0) matching the Embedder protocol structurally."""

    name = "null"
    dim = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        return []


def _ranker(weights: FusionWeights | None = None) -> SemanticSearchRanker:
    # store_root/embedder are unused by reciprocal_rank_fuse; pass a disabled
    # config so construction never reaches the real code embedder.
    return SemanticSearchRanker(
        ".",
        store_root=".",
        embedder=_NullEmbedder(),
        fusion_weights=weights,
    )


def _score(record: SymbolRecord) -> float:
    # reciprocal_rank_fuse always attaches a fused score; narrow the optional.
    assert record.score is not None
    return record.score


# --------------------------------------------------------------------------
# Default weights reproduce baseline ranking
# --------------------------------------------------------------------------
def test_default_weights_reproduce_baseline_unweighted_rrf() -> None:
    ranker = _ranker()
    lexical = [_symbol("a"), _symbol("b"), _symbol("c")]
    semantic = [_symbol("c"), _symbol("d"), _symbol("a")]

    # No weights arg == default FusionWeights() == prior unweighted blend.
    default = ranker.reciprocal_rank_fuse(lexical, semantic, limit=5)
    explicit = ranker.reciprocal_rank_fuse(lexical, semantic, limit=5, weights=FusionWeights())
    assert [s.symbol_id for s in default] == [s.symbol_id for s in explicit]
    assert [s.score for s in default] == [s.score for s in explicit]

    # The default weights object is lexical=1.0, semantic=1.0, graph=0.0.
    assert FusionWeights() == FusionWeights(lexical=1.0, semantic=1.0, graph=0.0)
    # A ranker built with no env override resolves to the baseline weights.
    assert ranker.fusion_weights == FusionWeights()

    # Numeric: with k=60, 'a' scores 1/61 (lex r1) + 1/63 (sem r3); 'c' scores
    # 1/63 (lex r3) + 1/61 (sem r1). Equal scores -> semantic rank breaks ties.
    scores = {s.symbol_id: _score(s) for s in default}
    assert math.isclose(scores["a"], 1.0 / 61 + 1.0 / 63)
    assert math.isclose(scores["c"], 1.0 / 63 + 1.0 / 61)


def test_default_graph_weight_is_noop_even_with_graph_hits() -> None:
    ranker = _ranker()
    lexical = [_symbol("a"), _symbol("b")]
    semantic = [_symbol("a"), _symbol("b")]
    graph = [_symbol("z"), _symbol("a")]

    without_graph = ranker.reciprocal_rank_fuse(lexical, semantic, limit=5)
    with_graph = ranker.reciprocal_rank_fuse(lexical, semantic, limit=5, graph_hits=graph)
    # graph weight defaults to 0.0 -> a graph-only symbol ('z') gets score 0 and
    # the existing ranking/scores are unchanged.
    assert [s.symbol_id for s in with_graph if _score(s) > 0] == [s.symbol_id for s in without_graph]
    z = next(s for s in with_graph if s.symbol_id == "z")
    assert _score(z) == 0.0


# --------------------------------------------------------------------------
# Reweighting changes the ranking
# --------------------------------------------------------------------------
def test_reweighting_changes_ranking() -> None:
    ranker = _ranker()
    # 'sem_top' is lexical rank 2 but semantic rank 1; 'lex_top' is the inverse.
    # Baseline (equal weights) -> equal scores, broken toward the semantic-ranked
    # 'sem_top'. A lexical-heavy reweight must flip 'lex_top' ahead.
    lexical = [_symbol("lex_top"), _symbol("sem_top")]
    semantic = [_symbol("sem_top"), _symbol("lex_top")]

    baseline = ranker.reciprocal_rank_fuse(lexical, semantic, limit=5, weights=FusionWeights())
    reweighted = ranker.reciprocal_rank_fuse(
        lexical,
        semantic,
        limit=5,
        weights=FusionWeights(lexical=10.0, semantic=0.1, graph=0.0),
    )

    assert [s.symbol_id for s in baseline] != [s.symbol_id for s in reweighted]
    assert baseline[0].symbol_id == "sem_top"
    assert reweighted[0].symbol_id == "lex_top"


def test_graph_weight_promotes_graph_signal() -> None:
    ranker = _ranker()
    lexical = [_symbol("a")]
    semantic = [_symbol("a")]
    graph = [_symbol("graph_only")]
    fused = ranker.reciprocal_rank_fuse(
        lexical,
        semantic,
        limit=5,
        graph_hits=graph,
        weights=FusionWeights(lexical=1.0, semantic=1.0, graph=5.0),
    )
    # With a strong graph weight a graph-only hit earns a real (non-zero) score.
    g = next(s for s in fused if s.symbol_id == "graph_only")
    assert _score(g) > 0.0


# --------------------------------------------------------------------------
# FusionWeights.from_env
# --------------------------------------------------------------------------
def test_from_env_defaults_reproduce_baseline() -> None:
    assert FusionWeights.from_env(env={}) == FusionWeights()


def test_from_env_overrides_apply() -> None:
    weights = FusionWeights.from_env(
        env={
            "LEMONCROW_FUSION_WEIGHT_LEXICAL": "2.0",
            "LEMONCROW_FUSION_WEIGHT_GRAPH": "0.5",
        }
    )
    assert weights.lexical == 2.0
    assert weights.semantic == 1.0
    assert weights.graph == 0.5


def test_from_env_garbage_falls_back_to_baseline() -> None:
    weights = FusionWeights.from_env(env={"LEMONCROW_FUSION_WEIGHT_SEMANTIC": "not-a-float"})
    assert weights.semantic == 1.0


# --------------------------------------------------------------------------
# P/R/F1 evaluation harness
# --------------------------------------------------------------------------
def test_score_ranking_computes_precision_recall_f1() -> None:
    # 4 retrieved, top-4; relevant = {a, c, x}. TP among top-4 = {a, c} = 2.
    result = score_ranking(["a", "b", "c", "d"], {"a", "c", "x"}, k=4)
    assert result.true_positives == 2
    assert result.retrieved == 4
    assert result.relevant == 3
    assert math.isclose(result.precision, 2 / 4)
    assert math.isclose(result.recall, 2 / 3)
    expected_f1 = 2 * (0.5 * (2 / 3)) / (0.5 + 2 / 3)
    assert math.isclose(result.f1, expected_f1)


def test_score_ranking_perfect_and_empty() -> None:
    perfect = score_ranking(["a", "b"], {"a", "b"}, k=2)
    assert perfect.precision == 1.0
    assert perfect.recall == 1.0
    assert perfect.f1 == 1.0

    miss = score_ranking(["x", "y"], {"a"}, k=2)
    assert miss.precision == 0.0
    assert miss.recall == 0.0
    assert miss.f1 == 0.0


def test_score_ranking_cutoff_and_dedup() -> None:
    # k=2 truncates to top-2; duplicates collapse to first occurrence.
    result = score_ranking(["a", "a", "c"], {"a", "c"}, k=2)
    assert result.retrieved == 2  # a (deduped), c
    assert result.true_positives == 2
    assert math.isclose(result.precision, 1.0)
    assert math.isclose(result.recall, 1.0)


def test_evaluate_cases_macro_averages() -> None:
    cases = [
        EvalCase(case_id="q1", ranking=["a", "b"], relevant=frozenset({"a"})),
        EvalCase(case_id="q2", ranking=["x", "y"], relevant=frozenset({"x", "y"})),
    ]
    agg = evaluate_cases(cases, k=2)
    assert agg.case_count == 2
    # q1 precision 0.5, q2 precision 1.0 -> macro 0.75.
    assert math.isclose(agg.precision, (0.5 + 1.0) / 2)
    # q1 recall 1.0, q2 recall 1.0 -> macro 1.0.
    assert math.isclose(agg.recall, 1.0)
    assert len(agg.per_case) == 2


def test_evaluate_cases_empty_is_zero() -> None:
    agg = evaluate_cases([], k=5)
    assert agg.case_count == 0
    assert agg.precision == 0.0 and agg.recall == 0.0 and agg.f1 == 0.0


def test_harness_measures_a_reweight_against_ground_truth() -> None:
    # Tiny labelled set: the relevant symbol is the lexical-ranked top hit but a
    # noise symbol is the semantic-ranked top hit. With balanced weights the
    # noise wins position 1 (P@1=0). A lexical-heavy reweight lifts the relevant
    # symbol to position 1 (P@1=1), and the harness MEASURES that F1 gain.
    ranker = _ranker()
    lexical = [_symbol("relevant"), _symbol("noise")]
    semantic = [_symbol("noise"), _symbol("relevant")]
    relevant = frozenset({"relevant"})

    default_rank = [
        s.symbol_id for s in ranker.reciprocal_rank_fuse(lexical, semantic, limit=5, weights=FusionWeights())
    ]
    lex_rank = [
        s.symbol_id
        for s in ranker.reciprocal_rank_fuse(
            lexical,
            semantic,
            limit=5,
            weights=FusionWeights(lexical=10.0, semantic=0.1, graph=0.0),
        )
    ]

    default_f1 = score_ranking(default_rank, relevant, k=1).f1
    lex_f1 = score_ranking(lex_rank, relevant, k=1).f1
    assert default_f1 == 0.0
    assert lex_f1 == 1.0
    assert lex_f1 > default_f1


# --------------------------------------------------------------------------
# "Semantic additive only" gate (semantic_additive_k)
# --------------------------------------------------------------------------
def test_semantic_additive_k_zero_matches_default_rrf() -> None:
    ranker = _ranker()
    lexical = [_symbol("a"), _symbol("b"), _symbol("c")]
    semantic = [_symbol("c"), _symbol("d"), _symbol("a")]

    default = ranker.reciprocal_rank_fuse(lexical, semantic, limit=5)
    explicit_zero = ranker.reciprocal_rank_fuse(lexical, semantic, limit=5, semantic_additive_k=0)

    # k=0 is the opt-out: identical order AND identical fused scores.
    assert [s.symbol_id for s in default] == [s.symbol_id for s in explicit_zero]
    assert [_score(s) for s in default] == [_score(s) for s in explicit_zero]


def test_semantic_additive_gate_freezes_lexical_top_and_lifts_semantic_only() -> None:
    ranker = _ranker()
    # 'lex1' is lexical rank 1 but semantic rank 3; 'boosted' is lexical rank 3
    # yet semantic rank 1, so ungated RRF lets the semantic channel demote 'lex1'
    # below 'boosted'. 'sem_only' lives solely in the semantic channel.
    lexical = [_symbol("lex1"), _symbol("lex2"), _symbol("boosted")]
    semantic = [_symbol("boosted"), _symbol("sem_only"), _symbol("lex1")]

    ungated = [s.symbol_id for s in ranker.reciprocal_rank_fuse(lexical, semantic, limit=10)]
    gated = [s.symbol_id for s in ranker.reciprocal_rank_fuse(lexical, semantic, limit=10, semantic_additive_k=5)]

    # Ungated: semantic pushes the lexical rank-1 hit off the top spot.
    assert ungated[0] == "boosted"
    assert ungated.index("lex1") == 1

    # Gated: the lexical rank-1 hit is frozen at rank 1 despite its semantic
    # rank 3, and the semantic-only candidate rises above the now-unboosted
    # lexical hits it previously sat below.
    assert gated[0] == "lex1"
    assert gated.index("sem_only") < ungated.index("sem_only")
    assert gated == ["lex1", "sem_only", "lex2", "boosted"]
