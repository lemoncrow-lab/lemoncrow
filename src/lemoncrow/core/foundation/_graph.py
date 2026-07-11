"""Pure-Python directed/undirected graph + graph algorithms — replaces networkx.

Implements the exact API surface used in LemonCrow:

* ``DiGraph`` / ``Graph``  — adjacency-list graphs with node/edge attributes
* ``pagerank()``           — power iteration; results identical to nx.pagerank
* ``ancestors()``          — BFS on reversed edges
* ``topological_sort()``   — Kahn's algorithm
* ``NetworkXUnfeasible``   — raised on cycles in topological_sort

No external dependencies.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from typing import Any


class NetworkXUnfeasible(Exception):
    """Raised when topological_sort detects a cycle."""


class DiGraph:
    """Directed weighted graph with node and edge attribute dicts."""

    def __init__(self) -> None:
        self._nodes: dict[str, dict[str, Any]] = {}
        self._out: dict[str, dict[str, dict[str, Any]]] = {}  # src → dst → attrs
        self._in: dict[str, dict[str, dict[str, Any]]] = {}  # dst → src → attrs

    def _ensure(self, name: str) -> None:
        if name not in self._nodes:
            self._nodes[name] = {}
            self._out[name] = {}
            self._in[name] = {}

    def add_node(self, name: str, **attrs: Any) -> None:
        self._ensure(name)
        self._nodes[name].update(attrs)

    def add_edge(self, src: str, dst: str, **attrs: Any) -> None:
        self._ensure(src)
        self._ensure(dst)
        self._out[src][dst] = attrs
        self._in[dst][src] = attrs

    def get_edge_data(self, src: str, dst: str, default: Any = None) -> Any:
        return self._out.get(src, {}).get(dst, default)

    def has_edge(self, src: str, dst: str) -> bool:
        return dst in self._out.get(src, {})

    @property
    def nodes(self) -> Any:  # KeysView[str]
        return self._nodes.keys()

    def __contains__(self, name: object) -> bool:
        return name in self._nodes

    def number_of_nodes(self) -> int:
        return len(self._nodes)

    def number_of_edges(self) -> int:
        return sum(len(v) for v in self._out.values())

    def edges(self, data: bool = False) -> Iterator[Any]:
        for src, targets in self._out.items():
            for dst, attrs in targets.items():
                yield (src, dst, attrs) if data else (src, dst)

    def subgraph(self, nodes: list[str]) -> DiGraph:
        node_set = set(nodes)
        g = DiGraph()
        for n in node_set:
            if n in self._nodes:
                g.add_node(n, **self._nodes[n])
        for src in node_set:
            for dst, attrs in self._out.get(src, {}).items():
                if dst in node_set:
                    g.add_edge(src, dst, **attrs)
        return g


class Graph(DiGraph):
    """Undirected graph — edges stored symmetrically."""

    def add_edge(self, src: str, dst: str, **attrs: Any) -> None:
        self._ensure(src)
        self._ensure(dst)
        self._out[src][dst] = attrs
        self._out[dst][src] = attrs
        self._in[dst][src] = attrs
        self._in[src][dst] = attrs

    def number_of_edges(self) -> int:
        return sum(len(v) for v in self._out.values()) // 2

    def edges(self, data: bool = False) -> Iterator[Any]:
        seen: set[tuple[str, str]] = set()
        for src, targets in self._out.items():
            for dst, attrs in targets.items():
                key: tuple[str, str] = (src, dst) if src <= dst else (dst, src)
                if key not in seen:
                    seen.add(key)
                    yield (src, dst, attrs) if data else (src, dst)


def pagerank(
    graph: DiGraph,
    *,
    alpha: float = 0.85,
    personalization: dict[str, float] | None = None,
    weight: str = "weight",
    max_iter: int = 100,
    tol: float = 1.0e-6,
) -> dict[str, float]:
    """Power-iteration PageRank — identical results to ``nx.pagerank``."""
    nodes = list(graph.nodes)
    n = len(nodes)
    if n == 0:
        return {}

    # Personalisation vector normalised to sum=1
    if personalization is None:
        p = {v: 1.0 / n for v in nodes}
    else:
        s = sum(personalization.values()) or 1.0
        p = {v: personalization.get(v, 0.0) / s for v in nodes}

    # Build reverse stochastic matrix: in_w[dst][src] = normalised weight
    in_w: dict[str, dict[str, float]] = {v: {} for v in nodes}
    dangling: list[str] = []
    for src in nodes:
        out = graph._out.get(src, {})
        total = sum(float(a.get(weight, 1.0)) for a in out.values())
        if not out or total == 0.0:
            dangling.append(src)
            continue
        for dst, attrs in out.items():
            in_w[dst][src] = float(attrs.get(weight, 1.0)) / total

    pr: dict[str, float] = {v: 1.0 / n for v in nodes}
    for _ in range(max_iter):
        prev = pr.copy()
        d_sum = alpha * sum(prev[v] for v in dangling) / n
        new_pr = {
            v: alpha * sum(prev[s] * w for s, w in in_w.get(v, {}).items()) + d_sum + (1.0 - alpha) * p[v]
            for v in nodes
        }
        if sum(abs(new_pr[v] - prev[v]) for v in nodes) < n * tol:
            return new_pr
        pr = new_pr
    return pr


def ancestors(graph: DiGraph, source: str) -> set[str]:
    """All nodes with a directed path leading *to* ``source`` (reversed BFS)."""
    visited: set[str] = set()
    queue: deque[str] = deque([source])
    while queue:
        node = queue.popleft()
        for pred in graph._in.get(node, {}):
            if pred not in visited:
                visited.add(pred)
                queue.append(pred)
    return visited


def topological_sort(graph: DiGraph) -> list[str]:
    """Kahn's algorithm — raises ``NetworkXUnfeasible`` on cycles."""
    in_deg: dict[str, int] = {v: len(graph._in.get(v, {})) for v in graph.nodes}
    queue: deque[str] = deque(v for v, d in in_deg.items() if d == 0)
    result: list[str] = []
    while queue:
        node = queue.popleft()
        result.append(node)
        for succ in graph._out.get(node, {}):
            in_deg[succ] -= 1
            if in_deg[succ] == 0:
                queue.append(succ)
    if len(result) != graph.number_of_nodes():
        raise NetworkXUnfeasible("Graph contains a cycle")
    return result


__all__ = [
    "DiGraph",
    "Graph",
    "NetworkXUnfeasible",
    "ancestors",
    "pagerank",
    "topological_sort",
]
