"""Symbol-level call-graph centrality (G6).

File-level PageRank already exists in ``repo_map/pagerank.py``; this module is
the genuinely-missing *symbol* / call-graph level. It answers "which symbols
are most important here?" by ranking nodes of the call graph rather than files.

Input is the set of directed call edges already persisted in the engine's
``call_edges`` table -- ``(caller, callee)`` name pairs. We do NOT reinvent
graph storage; the caller passes edges read straight from that table.

Metrics (cheap, deterministic, dependency-free):

* ``in_degree`` / ``out_degree`` / ``degree`` centrality (normalised by N-1).
* ``eigenvector`` -- a bounded power-iteration approximation over the
  undirected adjacency, so highly-called symbols that are themselves called by
  important symbols rank above merely high-degree leaves.

The whole computation is O(iterations x edges); ``max_iterations`` is small and
fixed, so it stays cheap even on large graphs.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Edge = (caller_name, callee_name). Names are qualified-or-short identifiers as
# stored in call_edges; identity is by name (the call graph is name-resolved).
CallEdge = tuple[str, str]


def compute_call_graph_centrality(
    edges: Iterable[CallEdge],
    *,
    limit: int = 50,
    max_iterations: int = 50,
    tolerance: float = 1.0e-6,
) -> dict[str, Any]:
    """Rank call-graph symbols by importance.

    Returns a dict with ``node_count``, ``edge_count`` and a ``ranking`` list
    (most central first) of ``{symbol, in_degree, out_degree, degree,
    eigenvector}`` entries, truncated to ``limit``. Ordering is total and
    deterministic: by descending eigenvector, then descending total degree,
    then symbol name -- so equal-score graphs always rank identically.
    """
    in_deg: dict[str, int] = {}
    out_deg: dict[str, int] = {}
    adjacency: dict[str, set[str]] = {}
    edge_count = 0
    for caller, callee in edges:
        if not caller or not callee or caller == callee:
            continue
        edge_count += 1
        out_deg[caller] = out_deg.get(caller, 0) + 1
        in_deg[callee] = in_deg.get(callee, 0) + 1
        in_deg.setdefault(caller, in_deg.get(caller, 0))
        out_deg.setdefault(callee, out_deg.get(callee, 0))
        # Undirected adjacency for the eigenvector approximation.
        adjacency.setdefault(caller, set()).add(callee)
        adjacency.setdefault(callee, set()).add(caller)

    nodes = sorted(adjacency)
    node_count = len(nodes)
    if node_count == 0:
        return {"node_count": 0, "edge_count": 0, "ranking": [], "truncated": False}

    eigen = _power_iteration(nodes, adjacency, max_iterations=max_iterations, tolerance=tolerance)
    norm = float(node_count - 1) if node_count > 1 else 1.0

    ranking: list[dict[str, Any]] = []
    for symbol in nodes:
        ind = in_deg.get(symbol, 0)
        outd = out_deg.get(symbol, 0)
        ranking.append(
            {
                "symbol": symbol,
                "in_degree": ind,
                "out_degree": outd,
                "degree": round((ind + outd) / norm, 4),
                "in_degree_centrality": round(ind / norm, 4),
                "out_degree_centrality": round(outd / norm, 4),
                "eigenvector": round(eigen.get(symbol, 0.0), 6),
            }
        )
    ranking.sort(
        key=lambda r: (
            -float(r["eigenvector"]),
            -(int(r["in_degree"]) + int(r["out_degree"])),
            str(r["symbol"]),
        )
    )
    return {
        "node_count": node_count,
        "edge_count": edge_count,
        "ranking": ranking[:limit],
        "truncated": node_count > limit,
    }


def _power_iteration(
    nodes: list[str],
    adjacency: dict[str, set[str]],
    *,
    max_iterations: int,
    tolerance: float,
) -> dict[str, float]:
    """Bounded power iteration on the undirected adjacency -> eigenvector scores.

    L1-normalised each step; stops early once the update is below ``tolerance``.
    Disconnected graphs converge fine because the start vector is uniform.
    """
    n = len(nodes)
    score: dict[str, float] = {node: 1.0 / n for node in nodes}
    for _ in range(max(1, max_iterations)):
        nxt: dict[str, float] = {node: 0.0 for node in nodes}
        for node in nodes:
            s = score[node]
            if s == 0.0:
                continue
            for neighbour in adjacency.get(node, ()):  # spread to neighbours
                nxt[neighbour] += s
        total = sum(nxt.values())
        if total <= 0.0:
            return score
        delta = 0.0
        for node in nodes:
            normalised = nxt[node] / total
            delta += abs(normalised - score[node])
            nxt[node] = normalised
        score = nxt
        if delta < tolerance:
            break
    return score
