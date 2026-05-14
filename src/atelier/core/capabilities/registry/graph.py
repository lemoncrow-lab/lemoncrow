"""Capability registry backed by an explicit networkx dependency graph.

Design
------
Capabilities are nodes in a directed acyclic graph (DAG).  An edge
``A → B`` means "B depends on A" — A's output influences B's behaviour.
Weights on edges express the strength of that influence.

The registry provides:

* ``register()``  — add a capability (with optional dependencies/fallback).
* ``activation_path()`` — topological activation order for a given target.
* ``fallback_for()`` — which capability to fall back to if one fails.
* ``dependency_report()`` — full graph dump as a serialisable dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:
    import networkx as nx

    _HAS_NX = True
except Exception:  # pragma: no cover
    nx = None
    _HAS_NX = False


@dataclass
class CapabilityNode:
    """Metadata for a registered capability."""

    name: str
    instance: Any
    fallback: str | None = None
    tags: list[str] = field(default_factory=list)


class CapabilityRegistry:
    """Registry of all capabilities with an explicit dependency graph.

    Typical inter-capability influences::

        context_reuse    ──►  context_compression
        semantic_memory  ──►  context_reuse
        tool_supervision ──►  loop_detection
        loop_detection   ──►  context_compression

    Usage::

        from atelier.core.capabilities import (
            ContextCompressionCapability, ContextReuseCapability,
        )
        reg = CapabilityRegistry()
        reg.register("context_reuse", ContextReuseCapability())
        reg.register(
            "context_compression",
            ContextCompressionCapability(),
            depends_on=[("context_reuse", 0.9)],
        )
        path = reg.activation_path("context_compression")
        # → ["context_reuse", "context_compression"]
    """

    def __init__(self) -> None:
        self._nodes: dict[str, CapabilityNode] = {}
        self._adj: dict[str, list[tuple[str, float]]] = {}
        if _HAS_NX:
            self._graph: Any = nx.DiGraph()
        else:
            self._graph = None  # pragma: no cover

    # ------------------------------------------------------------------
    # Registration

    def register(
        self,
        name: str,
        instance: Any,
        *,
        depends_on: list[tuple[str, float]] | None = None,
        fallback: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Register a capability.

        Args:
            name:        Unique capability identifier.
            instance:    Capability object.
            depends_on:  List of ``(name, weight)`` pairs — upstream deps.
            fallback:    Capability to use when this one fails.
            tags:        Free-form labels (e.g. ``["compression", "core"]``).
        """
        node = CapabilityNode(
            name=name,
            instance=instance,
            fallback=fallback,
            tags=tags or [],
        )
        self._nodes[name] = node
        self._adj[name] = depends_on or []

        if self._graph is not None:
            self._graph.add_node(name, tags=tags or [])
            for dep, weight in depends_on or []:
                if dep not in self._graph:
                    self._graph.add_node(dep)
                self._graph.add_edge(dep, name, weight=weight)

    # ------------------------------------------------------------------
    # Activation / fallback

    def activation_path(self, target: str) -> list[str]:
        """Return the ordered activation sequence leading up to ``target``.

        The list is in topological order: all dependencies first, ``target``
        last.  If networkx is unavailable, returns ``[target]``.
        """
        if not _HAS_NX or self._graph is None:  # pragma: no cover
            return [target]
        if target not in self._graph:
            return [target]
        ancestors = list(nx.ancestors(self._graph, target))
        sub = self._graph.subgraph([*ancestors, target])
        try:
            return list(nx.topological_sort(sub))
        except nx.NetworkXUnfeasible:  # cycle (shouldn't happen in a well-formed DAG)
            return [*ancestors, target]

    def fallback_for(self, name: str) -> str | None:
        """Return the registered fallback capability name, or ``None``."""
        node = self._nodes.get(name)
        return node.fallback if node else None

    def get(self, name: str) -> Any | None:
        """Return the capability instance registered under ``name``."""
        node = self._nodes.get(name)
        return node.instance if node else None

    # ------------------------------------------------------------------
    # Reporting

    def dependency_report(self) -> dict[str, Any]:
        """Return a serialisable snapshot of the full dependency graph."""
        report: dict[str, Any] = {"capabilities": {}, "edges": []}
        for name, node in self._nodes.items():
            report["capabilities"][name] = {
                "fallback": node.fallback,
                "tags": node.tags,
                "depends_on": [dep for dep, _ in self._adj.get(name, [])],
            }
        if self._graph is not None:
            for src, dst, data in self._graph.edges(data=True):
                report["edges"].append({"from": src, "to": dst, "weight": data.get("weight", 1.0)})
        return report

    # ------------------------------------------------------------------
    # Dunder helpers

    def __contains__(self, name: str) -> bool:
        return name in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)
