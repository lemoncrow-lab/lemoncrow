"""WS4 G6 -- symbol-level call-graph centrality (pure ranking function)."""

from __future__ import annotations

from lemoncrow.core.capabilities.code_context.call_graph_centrality import (
    compute_call_graph_centrality,
)


def test_hub_symbol_ranks_first() -> None:
    # a, b, c all call hub; hub calls d. hub is the most central node.
    edges = [("a", "hub"), ("b", "hub"), ("c", "hub"), ("hub", "d")]
    result = compute_call_graph_centrality(edges)
    assert result["node_count"] == 5
    assert result["edge_count"] == 4
    ranking = result["ranking"]
    assert ranking[0]["symbol"] == "hub"
    top = ranking[0]
    assert top["in_degree"] == 3
    assert top["out_degree"] == 1
    # hub has the highest total degree of any node.
    assert all(top["in_degree"] + top["out_degree"] >= r["in_degree"] + r["out_degree"] for r in ranking)


def test_empty_graph_is_safe() -> None:
    result = compute_call_graph_centrality([])
    assert result == {"node_count": 0, "edge_count": 0, "ranking": [], "truncated": False}


def test_self_loops_and_blanks_ignored() -> None:
    edges = [("x", "x"), ("", "y"), ("y", ""), ("p", "q")]
    result = compute_call_graph_centrality(edges)
    # Only p->q is a real edge.
    assert result["edge_count"] == 1
    assert {r["symbol"] for r in result["ranking"]} == {"p", "q"}


def test_ranking_is_truncated_to_limit() -> None:
    edges = [(f"caller{i}", "sink") for i in range(10)]
    result = compute_call_graph_centrality(edges, limit=3)
    assert len(result["ranking"]) == 3
    assert result["truncated"] is True
    assert result["ranking"][0]["symbol"] == "sink"


def test_out_degree_normalization_favors_focused_callers_over_promiscuous_ones() -> None:
    # "utility": called once each by 20 promiscuous callers that ALSO each call
    # 50 other unrelated things (so utility is a tiny fraction of their concern).
    # "real_hub": called by 3 focused callers whose ONLY job is calling real_hub.
    # Old undirected/un-normalized centrality ranked pure fan-in first, so
    # "utility" (in_degree=20) beat "real_hub" (in_degree=3) even though each of
    # real_hub's callers stakes its entire vote on it, while each of utility's
    # callers splits its vote 51 ways. Out-degree-normalized PageRank must rank
    # real_hub first: 3 undiluted votes outweigh 20 heavily-diluted ones.
    edges = []
    for i in range(20):
        caller = f"promiscuous{i}"
        edges.append((caller, "utility"))
        edges.extend((caller, f"other{i}_{j}") for j in range(50))
    for i in range(3):
        edges.append((f"focused{i}", "real_hub"))
    result = compute_call_graph_centrality(edges)
    ranking = {r["symbol"]: r for r in result["ranking"]}
    assert ranking["utility"]["in_degree"] == 20
    assert ranking["real_hub"]["in_degree"] == 3
    assert ranking["real_hub"]["eigenvector"] > ranking["utility"]["eigenvector"]


def test_pure_sink_does_not_outrank_its_multi_caller_source() -> None:
    # Regression test: a leaf with ZERO out-edges (a dangling/absorbing node in
    # the random-surfer model) must not out-rank a hub with several distinct
    # callers just by sitting at the end of the one path anyone follows.
    # Verified analytically (closed-form fixed point) that naive dangling-mass
    # redistribution -- crediting a sink with a slice of its OWN just-leaked
    # mass -- inverts this ordering (leaf 0.380 vs hub 0.336) on this exact
    # graph; excluding dangling nodes from the redistribution TARGET set fixes
    # it without reintroducing the old un-normalized-fan-in bug (see the test
    # above).
    edges = [("a", "hub"), ("b", "hub"), ("c", "hub"), ("hub", "leaf")]
    result = compute_call_graph_centrality(edges)
    ranking = {r["symbol"]: r for r in result["ranking"]}
    assert ranking["hub"]["eigenvector"] > ranking["leaf"]["eigenvector"]
