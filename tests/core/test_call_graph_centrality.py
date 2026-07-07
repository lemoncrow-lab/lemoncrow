"""WS4 G6 -- symbol-level call-graph centrality (pure ranking function)."""

from __future__ import annotations

from atelier.core.capabilities.code_context.call_graph_centrality import (
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
