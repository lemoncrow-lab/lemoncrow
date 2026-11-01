"""Snapshot-exact, change-centric architecture projection for Review.

The authoritative delta is derived by comparing immutable old/new Review sides.
Current-index Code Intelligence is useful surrounding context, but it is never
promoted into a claim that an architectural relationship changed in the
reviewed Snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from lemoncrow.core.foundation.optional_modules import module_absent

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import ReviewRevision
    from lemoncrow.pro.capabilities.review.store import ReviewStore

ARCHITECTURE_DELTA_VERSION = 1
ARCHITECTURE_GROUPING_VERSION = 1
ARCHITECTURE_EXPLAIN_PROMPT_VERSION = 1
ARCHITECTURE_EXPLAIN_SCHEMA_VERSION = 1

_MAX_NODES = 300
_MAX_EXACT_EDGES = 320
_MAX_CONTEXT_EDGES = 80
_MAX_EDGE_EVIDENCE = 6
_MAX_FLOW_STEPS = 8
#: The file-reference graph both sides are built with: the repository map's, which the
#: Review-only engine does not ship. Without it the comparison reports itself unavailable.
REFERENCE_GRAPH = "lemoncrow.pro.capabilities.repo_map.graph"

ARCHITECTURE_EXPLAIN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "why_it_matters": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "grounded_edge_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "flow": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "edge_id": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["edge_id", "explanation"],
                "additionalProperties": False,
            },
        },
        "limitations": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
    },
    "required": ["summary", "why_it_matters", "grounded_edge_ids", "flow", "limitations"],
    "additionalProperties": False,
}


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "\0".join(str(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _component(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return "" if parent == "." else parent


def _mapping_rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _architecture_provenance(
    source: str,
    exactness: str,
    *,
    comparison: str,
    note: str,
) -> dict[str, str]:
    return {
        "source": source,
        "exactness": exactness,
        "comparison": comparison,
        "note": note,
    }


def _reference_architecture(root: Path) -> dict[str, Any]:
    """Build one deterministic file-reference architecture from an immutable tree.

    The repository map links a file to every file that defines a name it uses, by the bare name. The Review claims these
    relationships as the Snapshot's own, so five uses are not taken as one: a name the file defines itself (its own
    helper, a local), a name only an import brings in (the import uses someone else's definition), a name that is only
    a local or a member of the other file, a name a JavaScript or TypeScript module declares without exporting it, and
    a name defined in another language. On a 2026-11-01 pull request those tied 220 unchanged files to a new test file
    through ``actual``, ``body``, ``json``, ``afterEach`` and ``calls``.
    """

    from lemoncrow.infra.tree_sitter.tags import detect_language
    from lemoncrow.pro.capabilities.repo_map.graph import build_reference_graph
    from lemoncrow.pro.capabilities.review.impact import _is_definition_tag

    graph, tags_by_file = build_reference_graph(root)
    definitions: dict[str, set[str]] = defaultdict(set)
    own: dict[str, set[str]] = defaultdict(set)
    family: dict[str, str] = {}
    for path, tags in tags_by_file.items():
        scoped = path.endswith(_SCOPELESS_SUFFIXES)
        family[path] = "javascript" if scoped else str(detect_language(Path(path)) or Path(path).suffix)
        module: _Module | None = None
        for tag in tags:
            if tag.kind != "definition" or not _is_definition_tag(tag):
                continue
            own[path].add(tag.name)
            if scoped:
                if module is None:
                    module = _module(root / path)
                if not module.offers(tag.name, tag.line, tag.node_kind):
                    continue
            definitions[tag.name].add(path)

    edge_symbols: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    edge_evidence: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for source in sorted(tags_by_file):
        mine = own.get(source, set())
        for tag in tags_by_file[source]:
            if tag.kind != "reference" or tag.name in mine:
                continue
            for target in sorted(definitions.get(tag.name, ())):
                if target == source or family[target] != family[source]:
                    continue
                key = (source, target)
                edge_symbols[key][tag.name] += 1
                evidence = edge_evidence[key]
                if len(evidence) < _MAX_EDGE_EVIDENCE:
                    evidence.append(
                        {
                            "kind": "reference",
                            "path": source,
                            "line": max(0, int(tag.line)),
                            "symbol": tag.name,
                            "snippet": "",
                            "uncertainty": "",
                        }
                    )

    edges: list[dict[str, Any]] = []
    for key in sorted(edge_symbols):
        symbols = edge_symbols[key]
        edges.append(
            {
                "source": key[0],
                "target": key[1],
                "kind": "references",
                "weight": sum(symbols.values()),
                "symbols": [
                    {"name": name, "count": count}
                    for name, count in sorted(symbols.items(), key=lambda item: (-item[1], item[0]))[:8]
                ],
                "evidence": edge_evidence.get(key, []),
            }
        )
    return {"nodes": sorted(str(node) for node in graph.nodes), "edges": edges}


#: Sources whose tag extractor indexes definitions at every depth (function-body locals among them), so a definition's
#: indentation is what tells a file-level declaration from a local. Other languages either index file- and class-level
#: definitions only (Python) or skip locals by scope (C, Go, Java, Rust, Ruby).
# lc-debt: indentation and export lines stand in for scope and the module's export table; a scope-aware walk for
# JS/TS in infra.tree_sitter.tags, with export flags on its tags, would replace both.
_SCOPELESS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
#: The JavaScript and TypeScript declarations that can name something at file level; members (a method, a field, a
#: property of a type) never do, even on a line that starts at column 0.
_FILE_LEVEL_KINDS = frozenset(
    {
        "class_declaration",
        "enum_declaration",
        "function_declaration",
        "function_signature",
        "generator_function_declaration",
        "interface_declaration",
        "lexical_declaration",
        "type_alias_declaration",
        "variable_declaration",
    }
)
_MODULE_LINE = re.compile(r"^(?:import|export)\b")
_EXPORT_LIST = re.compile(r"\bexport\s+(?:type\s+)?\{([^}]*)\}")
_EXPORT_DEFAULT = re.compile(r"^export\s+default\s+([A-Za-z_$][\w$]*)\s*;?\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class _Module:
    """What one JavaScript or TypeScript file offers other files: its file-level names, only the exported ones if an ES
    module (a script or CommonJS file has no export lines to read, so all of them)."""

    lines: tuple[str, ...]
    esm: bool
    exported: frozenset[str]

    def offers(self, name: str, line: int, node_kind: str | None) -> bool:
        if node_kind is not None and node_kind not in _FILE_LEVEL_KINDS:
            return False
        if not 0 < line <= len(self.lines):
            return True  # an unknown line keeps its definition
        text = self.lines[line - 1]
        if text[:1].isspace():
            return False
        return not self.esm or text.startswith("export") or name in self.exported


def _module(path: Path) -> _Module:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    lines = tuple(text.splitlines())
    exported = {
        part.split(" as ")[0].strip().removeprefix("type ").strip()
        for clause in _EXPORT_LIST.findall(text)
        for part in clause.split(",")
    }
    exported.update(_EXPORT_DEFAULT.findall(text))
    return _Module(
        lines=lines,
        esm=any(_MODULE_LINE.match(row) for row in lines),
        exported=frozenset(name for name in exported if name),
    )


def _rename_map(packet: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in _mapping_rows(packet.get("files")):
        if str(row.get("status") or "") != "renamed":
            continue
        old_path = str(row.get("old_path") or "")
        path = str(row.get("path") or "")
        if old_path and path:
            out[old_path] = path
    return out


def _normalize_reference_graph(graph: Mapping[str, Any], renames: Mapping[str, str]) -> dict[str, Any]:
    def norm(path: str) -> str:
        return str(renames.get(path) or path)

    nodes = sorted({norm(str(path)) for path in list(graph.get("nodes") or []) if str(path)})
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in _mapping_rows(graph.get("edges")):
        source = norm(str(raw.get("source") or ""))
        target = norm(str(raw.get("target") or ""))
        kind = str(raw.get("kind") or "references")
        if not source or not target or source == target:
            continue
        key = (source, target, kind)
        row = edges.setdefault(
            key,
            {
                "source": source,
                "target": target,
                "kind": kind,
                "weight": 0,
                "symbols": Counter(),
                "evidence": [],
            },
        )
        row["weight"] += max(1, int(raw.get("weight") or 1))
        for symbol in _mapping_rows(raw.get("symbols")):
            name = str(symbol.get("name") or "")
            if name:
                row["symbols"][name] += max(1, int(symbol.get("count") or 1))
        for evidence in _mapping_rows(raw.get("evidence")):
            if len(row["evidence"]) < _MAX_EDGE_EVIDENCE:
                item = dict(evidence)
                if str(item.get("path") or "") in renames:
                    item["path"] = renames[str(item["path"])]
                row["evidence"].append(item)

    normalized_edges: list[dict[str, Any]] = []
    for key in sorted(edges):
        row = edges[key]
        symbols: Counter[str] = row.pop("symbols")
        row["symbols"] = [
            {"name": name, "count": count}
            for name, count in sorted(symbols.items(), key=lambda item: (-item[1], item[0]))[:8]
        ]
        normalized_edges.append(row)
    return {"nodes": nodes, "edges": normalized_edges}


def _edge_map(graph: Mapping[str, Any]) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    out: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for row in _mapping_rows(graph.get("edges")):
        source = str(row.get("source") or "")
        target = str(row.get("target") or "")
        kind = str(row.get("kind") or "references")
        if source and target:
            out[(source, target, kind)] = row
    return out


def _packet_rows(packet: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for row in _mapping_rows(packet.get("files")):
        path = str(row.get("path") or "")
        if path:
            out[path] = row
    return out


def _context_rows(context_graph: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], list[Mapping[str, Any]]]:
    nodes = {
        str(row.get("id") or ""): row for row in _mapping_rows(context_graph.get("nodes")) if str(row.get("id") or "")
    }
    return nodes, _mapping_rows(context_graph.get("edges"))


def _delta_edge_status(base_weight: int, snapshot_weight: int) -> str:
    if base_weight <= 0 and snapshot_weight > 0:
        return "added"
    if base_weight > 0 and snapshot_weight <= 0:
        return "removed"
    if base_weight != snapshot_weight:
        return "changed"
    return "unchanged"


def _node_change_status(
    path: str,
    *,
    base_nodes: set[str],
    snapshot_nodes: set[str],
    packet_rows: Mapping[str, Mapping[str, Any]],
) -> str:
    in_base = path in base_nodes
    in_snapshot = path in snapshot_nodes
    if in_snapshot and not in_base:
        return "added"
    if in_base and not in_snapshot:
        return "removed"
    row = packet_rows.get(path)
    if row is not None and str(row.get("status") or "") in {"modified", "renamed"}:
        return "changed"
    return "unchanged"


def _node_selection(
    *,
    review_id: str,
    revision_id: str,
    node_id: str,
    exactness: str,
    path: str,
    side: str,
    delta_status: str,
    provenance: Mapping[str, Any],
    target_ids: Sequence[str] = (),
    current_symbol_id: str = "",
) -> dict[str, Any]:
    return {
        "kind": "review_map_node",
        "review_id": review_id,
        "revision_id": revision_id,
        "level": "architecture",
        "projection": "architecture_delta",
        "node_id": node_id,
        "focus_node_id": "",
        "exactness": exactness,
        "side": side,
        "path": path,
        "line": 0,
        "end_line": 0,
        "symbol": "",
        "target_ids": list(target_ids),
        "current_symbol_id": current_symbol_id,
        "delta_status": delta_status,
        "architecture_source": str(provenance.get("source") or "unavailable"),
        "architecture_comparison": str(provenance.get("comparison") or "none"),
    }


def _edge_selection(
    *,
    review_id: str,
    revision_id: str,
    edge_id: str,
    source_id: str,
    target_id: str,
    relation: str,
    exactness: str,
    delta_status: str,
    provenance: Mapping[str, Any],
    side: str = "new",
    path: str = "",
    line: int = 0,
    end_line: int = 0,
) -> dict[str, Any]:
    return {
        "kind": "review_map_edge",
        "review_id": review_id,
        "revision_id": revision_id,
        "level": "architecture",
        "projection": "architecture_delta",
        "focus_node_id": "",
        "edge_id": edge_id,
        "source_id": source_id,
        "target_id": target_id,
        "relation": relation,
        "exactness": exactness,
        "side": side,
        "path": path,
        "line": line,
        "end_line": end_line or line,
        "delta_status": delta_status,
        "architecture_source": str(provenance.get("source") or "unavailable"),
        "architecture_comparison": str(provenance.get("comparison") or "none"),
    }


def _current_graph_degradation(context: Mapping[str, Any]) -> list[str]:
    degraded: list[str] = []
    source = str(context.get("source") or "unavailable")
    if source != "current_index":
        degraded.append("current_index_unavailable")
        return degraded
    readiness = context.get("readiness")
    graph = readiness.get("graph") if isinstance(readiness, Mapping) else None
    state = str(graph.get("state") or "") if isinstance(graph, Mapping) else ""
    if state and state != "ready":
        degraded.append(f"current_index_{state}")
    if not bool(context.get("snapshot_head_matches_checkout", True)):
        degraded.append("current_index_checkout_differs_from_snapshot")
    if context.get("generation_matches_snapshot_head") is False:
        degraded.append("current_index_generation_differs_from_snapshot")
    return degraded


def _component_summary(
    *,
    raw_base_nodes: set[str],
    raw_snapshot_nodes: set[str],
    selected_paths: set[str],
    changed_edges: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    base_components: dict[str, set[str]] = defaultdict(set)
    snapshot_components: dict[str, set[str]] = defaultdict(set)
    for path in raw_base_nodes:
        base_components[_component(path)].add(path)
    for path in raw_snapshot_nodes:
        snapshot_components[_component(path)].add(path)

    edge_components = {
        _component(str(edge.get(endpoint) or ""))
        for edge in changed_edges
        for endpoint in ("source_path", "target_path")
        if str(edge.get(endpoint) or "")
    }
    components = sorted(set(base_components) | set(snapshot_components) | edge_components)
    changed: set[str] = set()
    rows: list[dict[str, Any]] = []
    for name in components:
        before = base_components.get(name, set())
        after = snapshot_components.get(name, set())
        if not before and after:
            status = "added"
        elif before and not after:
            status = "removed"
        elif before != after or name in edge_components:
            status = "changed"
        else:
            status = "unchanged"
        if status != "unchanged":
            changed.add(name)
        visible = sorted(path for path in selected_paths if _component(path) == name)
        rows.append(
            {
                "id": _stable_id("architecture-component", name),
                "label": name or "(root)",
                "delta_status": status,
                "base_file_count": len(before),
                "snapshot_file_count": len(after),
                "paths": visible[:16],
                "paths_truncated": len(visible) > 16,
            }
        )
    return rows, changed


def _flow_projection(changed_edges: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    exact = [
        edge
        for edge in changed_edges
        if str(edge.get("exactness") or "") == "snapshot"
        and str(edge.get("delta_status") or "") in {"added", "removed", "changed"}
    ]
    components = {
        _component(str(edge.get(field) or ""))
        for edge in exact
        for field in ("source_path", "target_path")
        if str(edge.get(field) or "")
    }
    applicable = len(exact) >= 2 and len(components) >= 3
    reason = (
        "Multiple exact relationship changes cross at least three components."
        if applicable
        else "The exact relationship delta is too small for a useful sequence/flow explanation."
    )
    steps = [
        {
            "edge_id": str(edge.get("id") or ""),
            "from": str(edge.get("source_path") or ""),
            "to": str(edge.get("target_path") or ""),
            "relationship": str(edge.get("kind") or "relationship"),
            "delta_status": str(edge.get("delta_status") or "unknown"),
            "base_weight": int(edge.get("base_weight") or 0),
            "snapshot_weight": int(edge.get("snapshot_weight") or 0),
        }
        for edge in exact[:_MAX_FLOW_STEPS]
    ]
    return {
        "applicable": applicable,
        "kind": "changed_relationship_flow" if applicable else "none",
        "reason": reason,
        "steps": steps if applicable else [],
    }


def build_architecture_delta(
    *,
    review_id: str,
    revision_id: str,
    packet: Mapping[str, Any],
    base_architecture: Mapping[str, Any],
    snapshot_architecture: Mapping[str, Any],
    context_graph: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    base_identity: str = "",
    snapshot_identity: str = "",
) -> dict[str, Any]:
    """Compare immutable architectures and append clearly-labelled live context."""

    context_graph = context_graph or {}
    context = context or {}
    renames = _rename_map(packet)
    base = _normalize_reference_graph(base_architecture, renames)
    snapshot = _normalize_reference_graph(snapshot_architecture, {})
    base_nodes = {str(path) for path in list(base.get("nodes") or [])}
    snapshot_nodes = {str(path) for path in list(snapshot.get("nodes") or [])}
    raw_base_nodes = {str(path) for path in list(base_architecture.get("nodes") or [])}
    raw_snapshot_nodes = {str(path) for path in list(snapshot_architecture.get("nodes") or [])}
    packet_rows = _packet_rows(packet)

    changed_paths = set(packet_rows)
    base_edges = _edge_map(base)
    snapshot_edges = _edge_map(snapshot)
    edge_keys = sorted(set(base_edges) | set(snapshot_edges))

    exact_edges: list[dict[str, Any]] = []
    for key in edge_keys:
        source, target, kind = key
        before = base_edges.get(key)
        after = snapshot_edges.get(key)
        base_weight = int(before.get("weight") or 0) if before else 0
        snapshot_weight = int(after.get("weight") or 0) if after else 0
        delta_status = _delta_edge_status(base_weight, snapshot_weight)
        if delta_status == "unchanged" and source not in changed_paths and target not in changed_paths:
            continue
        provenance = _architecture_provenance(
            "snapshot_comparison",
            "snapshot",
            comparison="base_vs_snapshot",
            note="Derived by comparing immutable base and reviewed Snapshot source graphs.",
        )
        evidence_source = after or before or {}
        evidence_rows = [dict(item) for item in _mapping_rows(evidence_source.get("evidence"))[:_MAX_EDGE_EVIDENCE]]
        evidence_target = evidence_rows[0] if evidence_rows else {}
        evidence_path = str(evidence_target.get("path") or source)
        evidence_line = max(0, int(evidence_target.get("line") or 0))
        review_path = source if source in changed_paths else target if target in changed_paths else evidence_path
        review_line = evidence_line if review_path == evidence_path else 0
        edge_id = _stable_id("architecture-edge", source, target, kind)
        exact_edges.append(
            {
                "id": edge_id,
                "source_path": source,
                "target_path": target,
                "source": _stable_id("file", source),
                "target": _stable_id("file", target),
                "kind": kind,
                "exactness": "snapshot",
                "delta_status": delta_status,
                "base_weight": base_weight,
                "snapshot_weight": snapshot_weight,
                "weight": snapshot_weight or base_weight or 1,
                "uncertain": False,
                "evidence": evidence_rows,
                "symbols": [dict(item) for item in _mapping_rows(evidence_source.get("symbols"))[:8]],
                "architecture_provenance": provenance,
                "selection": _edge_selection(
                    review_id=review_id,
                    revision_id=revision_id,
                    edge_id=edge_id,
                    source_id=_stable_id("file", source),
                    target_id=_stable_id("file", target),
                    relation=kind,
                    exactness="snapshot",
                    delta_status=delta_status,
                    provenance=provenance,
                    side="old" if delta_status == "removed" else "new",
                    path=review_path,
                    line=review_line,
                ),
            }
        )

    priority = {"added": 0, "removed": 1, "changed": 2, "unchanged": 3}
    exact_edges.sort(
        key=lambda edge: (
            priority.get(str(edge["delta_status"]), 9),
            str(edge["source_path"]),
            str(edge["target_path"]),
            str(edge["kind"]),
        )
    )

    selected_paths = set(changed_paths)
    selected_exact_edges: list[dict[str, Any]] = []
    for edge in exact_edges:
        needed = {str(edge["source_path"]), str(edge["target_path"])} - selected_paths
        if selected_exact_edges and len(selected_exact_edges) >= _MAX_EXACT_EDGES:
            break
        if len(selected_paths) + len(needed) > _MAX_NODES and str(edge["delta_status"]) == "unchanged":
            continue
        selected_paths.update(needed)
        selected_exact_edges.append(edge)

    context_nodes_by_id, context_edges = _context_rows(context_graph)
    context_nodes_by_path = {
        str(row.get("path") or ""): row for row in context_nodes_by_id.values() if str(row.get("path") or "")
    }

    supplemental_edges: list[dict[str, Any]] = []
    exact_edge_shapes = {
        (str(edge["source_path"]), str(edge["target_path"]), str(edge["kind"])) for edge in selected_exact_edges
    }
    for raw in sorted(
        context_edges,
        key=lambda edge: (
            str(edge.get("exactness") or ""),
            str(edge.get("source") or ""),
            str(edge.get("target") or ""),
            str(edge.get("kind") or ""),
        ),
    ):
        source_node = context_nodes_by_id.get(str(raw.get("source") or ""))
        target_node = context_nodes_by_id.get(str(raw.get("target") or ""))
        if source_node is None or target_node is None:
            continue
        source_path = str(source_node.get("path") or "")
        target_path = str(target_node.get("path") or "")
        kind = str(raw.get("kind") or "relationship")
        if not source_path or not target_path or source_path == target_path:
            continue
        raw_exactness = str(raw.get("exactness") or "current_index")
        if raw_exactness == "current_index":
            if len([edge for edge in supplemental_edges if edge["exactness"] == "current_index"]) >= _MAX_CONTEXT_EDGES:
                continue
            if source_path not in selected_paths and target_path not in selected_paths:
                continue
            provenance = _architecture_provenance(
                "current_index",
                "current_index",
                comparison="none",
                note="Current-index relationship is context only; it is not evidence that this Change altered the relationship.",
            )
            delta_status = "context_only"
            exactness = "current_index"
            prefix = "architecture-context-edge"
        else:
            # Snapshot impact facts can explain behavior, but without a matching
            # base comparison they do not prove that the relationship itself was
            # added/removed/changed by this Change.
            if (source_path, target_path, kind) in exact_edge_shapes:
                continue
            provenance = _architecture_provenance(
                "snapshot_fact",
                "snapshot",
                comparison="none",
                note="Snapshot-exact relationship fact; base-vs-Snapshot relationship change is not established.",
            )
            delta_status = "unknown"
            exactness = "snapshot"
            prefix = "architecture-snapshot-edge"
        needed = {source_path, target_path} - selected_paths
        if len(selected_paths) + len(needed) > _MAX_NODES:
            continue
        selected_paths.update(needed)
        evidence_rows = [dict(item) for item in _mapping_rows(raw.get("evidence"))[:_MAX_EDGE_EVIDENCE]]
        evidence_target = evidence_rows[0] if evidence_rows else {}
        evidence_path = str(evidence_target.get("path") or source_path)
        evidence_line = max(0, int(evidence_target.get("line") or 0))
        review_path = (
            source_path
            if source_path in changed_paths
            else target_path if target_path in changed_paths else evidence_path
        )
        review_line = evidence_line if review_path == evidence_path else 0
        edge_id = _stable_id(prefix, source_path, target_path, kind)
        supplemental_edges.append(
            {
                "id": edge_id,
                "source_path": source_path,
                "target_path": target_path,
                "source": _stable_id("file", source_path),
                "target": _stable_id("file", target_path),
                "kind": kind,
                "exactness": exactness,
                "delta_status": delta_status,
                "base_weight": 0,
                "snapshot_weight": int(raw.get("weight") or 0) if exactness == "snapshot" else 0,
                "weight": max(1, int(raw.get("weight") or 1)),
                "uncertain": bool(raw.get("uncertain")),
                "evidence": evidence_rows,
                "symbols": [dict(item) for item in _mapping_rows(raw.get("symbols"))[:8]],
                "architecture_provenance": provenance,
                "selection": _edge_selection(
                    review_id=review_id,
                    revision_id=revision_id,
                    edge_id=edge_id,
                    source_id=_stable_id("file", source_path),
                    target_id=_stable_id("file", target_path),
                    relation=kind,
                    exactness=exactness,
                    delta_status=delta_status,
                    provenance=provenance,
                    path=review_path if exactness == "snapshot" else "",
                    line=review_line if exactness == "snapshot" else 0,
                ),
            }
        )

    all_edges = selected_exact_edges + supplemental_edges
    lanes: dict[str, str] = {}
    for edge in all_edges:
        source = str(edge["source_path"])
        target = str(edge["target_path"])
        source_changed = source in changed_paths
        target_changed = target in changed_paths
        if source_changed == target_changed:
            continue
        if target_changed and source not in changed_paths:
            lanes.setdefault(source, "used_by")
        elif source_changed and target not in changed_paths:
            lanes.setdefault(target, "uses")

    nodes: list[dict[str, Any]] = []
    for path in sorted(selected_paths):
        normal = context_nodes_by_path.get(path) or {}
        in_exact_architecture = path in base_nodes or path in snapshot_nodes
        snapshot_fact = str(normal.get("exactness") or "") == "snapshot"
        if in_exact_architecture:
            delta_status = _node_change_status(
                path,
                base_nodes=base_nodes,
                snapshot_nodes=snapshot_nodes,
                packet_rows=packet_rows,
            )
            exactness = "snapshot"
            provenance = _architecture_provenance(
                "snapshot_comparison",
                "snapshot",
                comparison="base_vs_snapshot",
                note="Node presence is compared between immutable base and reviewed Snapshot source graphs.",
            )
        elif snapshot_fact:
            delta_status = "unknown"
            exactness = "snapshot"
            provenance = _architecture_provenance(
                "snapshot_fact",
                "snapshot",
                comparison="none",
                note="Snapshot-exact context is known, but architectural node change was not established by the comparison graph.",
            )
        else:
            delta_status = "context_only"
            exactness = "current_index"
            provenance = _architecture_provenance(
                "current_index",
                "current_index",
                comparison="none",
                note="Current-index node is surrounding context only and is not a Snapshot delta fact.",
            )
        if delta_status == "added":
            status = "new"
        elif delta_status in {"changed", "removed"}:
            status = "changed"
        elif exactness == "snapshot":
            status = "impacted"
        else:
            status = "context"
        packet_row = packet_rows.get(path) or {}
        target_ids = [str(value) for value in list(normal.get("target_ids") or []) if str(value)]
        current_symbol_ids = [str(value) for value in list(normal.get("current_symbol_ids") or []) if str(value)]
        old_path = str(packet_row.get("old_path") or "")
        side = "old" if delta_status == "removed" else "new"
        node_id = _stable_id("file", path)
        nodes.append(
            {
                "id": node_id,
                "label": str(normal.get("label") or PurePosixPath(path).name or path),
                "kind": "file",
                "component": _component(path),
                "module": _component(path),
                "path": path,
                "line": int(normal.get("line") or 0),
                "end_line": int(normal.get("end_line") or normal.get("line") or 0),
                "status": status,
                "exactness": exactness,
                "delta_status": delta_status,
                "lane": lanes.get(path, ""),
                "change": str(packet_row.get("status") or normal.get("change") or ""),
                "old_path": old_path,
                "additions": int(packet_row.get("additions") or normal.get("additions") or 0),
                "deletions": int(packet_row.get("deletions") or normal.get("deletions") or 0),
                "category": str(packet_row.get("category") or normal.get("category") or ""),
                "language": str(packet_row.get("language") or normal.get("language") or ""),
                "rank": int(normal.get("rank") or 0),
                "reasons": list(normal.get("reasons") or []),
                "review": normal.get("review"),
                "changed_file_count": 1 if path in changed_paths else 0,
                "impacted_file_count": 1 if exactness == "snapshot" and path not in changed_paths else 0,
                "paths": [path],
                "paths_truncated": False,
                "symbols": list(normal.get("symbols") or []),
                "symbols_truncated": bool(normal.get("symbols_truncated")),
                "symbol_count": int(normal.get("symbol_count") or 0),
                "target_ids": target_ids,
                "current_symbol_ids": current_symbol_ids,
                "change_kinds": list(normal.get("change_kinds") or []),
                "important_public_contract": bool(normal.get("important_public_contract")),
                "boundaries": list(normal.get("boundaries") or []),
                "boundary_basis": str(normal.get("boundary_basis") or ""),
                "uncertain": bool(normal.get("uncertain")),
                "uncertainty": str(normal.get("uncertainty") or ""),
                "provenance": dict(normal.get("provenance") or {}),
                "architecture_provenance": provenance,
                "relationship_count": sum(
                    1 for edge in all_edges if edge["source_path"] == path or edge["target_path"] == path
                ),
                "selection": _node_selection(
                    review_id=review_id,
                    revision_id=revision_id,
                    node_id=node_id,
                    exactness=exactness,
                    path=path,
                    side=side,
                    delta_status=delta_status,
                    provenance=provenance,
                    target_ids=target_ids,
                    current_symbol_id=current_symbol_ids[0] if current_symbol_ids else "",
                ),
            }
        )

    changed_relationships = [
        edge for edge in selected_exact_edges if str(edge.get("delta_status") or "") in {"added", "removed", "changed"}
    ]
    components, changed_components = _component_summary(
        raw_base_nodes=raw_base_nodes,
        raw_snapshot_nodes=raw_snapshot_nodes,
        selected_paths=selected_paths,
        changed_edges=changed_relationships,
    )
    flow = _flow_projection(changed_relationships)
    current_degraded = _current_graph_degradation(context)
    structural_node_changes = [
        node for node in nodes if node["exactness"] == "snapshot" and node["delta_status"] in {"added", "removed"}
    ]
    renamed_nodes = [node for node in nodes if node.get("old_path") and node.get("old_path") != node["path"]]
    material = bool(changed_relationships or structural_node_changes or renamed_nodes)

    concerns: list[str] = []
    touched_boundaries = sorted(
        {
            str(boundary)
            for node in nodes
            if node["delta_status"] in {"added", "removed", "changed"}
            for boundary in list(node.get("boundaries") or [])
            if str(boundary)
        }
    )
    concerns.extend(f"{boundary.title()} boundary touched" for boundary in touched_boundaries)
    if any(edge["delta_status"] == "removed" for edge in changed_relationships):
        concerns.append("Snapshot-exact relationships were removed")
    if any(edge["delta_status"] == "added" for edge in changed_relationships):
        concerns.append("Snapshot-exact relationships were added")
    if renamed_nodes:
        concerns.append("Files moved across the base-to-Snapshot architecture")

    exact_document = {
        "version": ARCHITECTURE_DELTA_VERSION,
        "base_identity": base_identity,
        "snapshot_identity": snapshot_identity,
        "grouping": {"strategy": "directory", "version": ARCHITECTURE_GROUPING_VERSION},
        "nodes": [
            {
                "path": node["path"],
                "delta_status": node["delta_status"],
                "exactness": node["exactness"],
                "component": node["component"],
            }
            for node in nodes
            if node["exactness"] == "snapshot"
        ],
        "edges": [
            {
                "source": edge["source_path"],
                "target": edge["target_path"],
                "kind": edge["kind"],
                "delta_status": edge["delta_status"],
                "base_weight": edge["base_weight"],
                "snapshot_weight": edge["snapshot_weight"],
            }
            for edge in selected_exact_edges
        ],
    }
    delta_digest = _digest(exact_document)
    summary = {
        "material": material,
        "status": "degraded" if current_degraded else "ready",
        "changed_components": len(changed_components),
        "changed_nodes": len(structural_node_changes) + len(renamed_nodes),
        "changed_edges": len(changed_relationships),
        "added_edges": sum(edge["delta_status"] == "added" for edge in changed_relationships),
        "removed_edges": sum(edge["delta_status"] == "removed" for edge in changed_relationships),
        "modified_edges": sum(edge["delta_status"] == "changed" for edge in changed_relationships),
        "context_only_edges": sum(edge["exactness"] == "current_index" for edge in supplemental_edges),
        "unknown_edges": sum(edge["delta_status"] == "unknown" for edge in supplemental_edges),
        "important_concerns": concerns,
        "exactness": "snapshot",
        "degraded": current_degraded,
    }

    graph_stats = {
        "changed_files": len(changed_paths),
        "new_files": sum(node["delta_status"] == "added" for node in nodes),
        "deleted_files": sum(node["delta_status"] == "removed" for node in nodes),
        "impacted_files": sum(node["status"] == "impacted" for node in nodes),
        "context_files": sum(node["status"] == "context" for node in nodes),
        "shown_changed": sum(node["status"] in {"changed", "new"} for node in nodes),
        "shown_impacted": sum(node["status"] == "impacted" for node in nodes),
        "shown_context": sum(node["status"] == "context" for node in nodes),
        "relationships": len(all_edges),
        "shown_relationships": len(all_edges),
        "architecture_changed_edges": len(changed_relationships),
        "architecture_context_edges": summary["context_only_edges"],
    }
    return {
        "version": ARCHITECTURE_DELTA_VERSION,
        "digest": delta_digest,
        "status": summary["status"],
        "reason": "" if material else "No material base-to-Snapshot architecture relationship change was detected.",
        "base_identity": base_identity,
        "snapshot_identity": snapshot_identity,
        "grouping": {
            "strategy": "directory",
            "version": ARCHITECTURE_GROUPING_VERSION,
            "stable_for_identical_input": True,
            "components": components,
        },
        "summary": summary,
        "flow": flow,
        "graph": {
            "level": "architecture",
            "focus": next((node["id"] for node in nodes if node["status"] in {"new", "changed"}), None),
            "nodes": nodes,
            "edges": all_edges,
            "truncated": len(exact_edges) > len(selected_exact_edges),
            "omitted_node_count": max(0, len(changed_paths) - len(selected_paths)),
            "stats": graph_stats,
            "reason": "" if nodes else "No architecture facts are available for this Snapshot.",
        },
    }


def unavailable_architecture_delta(
    *,
    review_id: str,
    revision_id: str,
    context_graph: Mapping[str, Any],
    context: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Degrade visibly without pretending the ordinary Map is a comparison."""

    nodes: list[dict[str, Any]] = []
    for raw in _mapping_rows(context_graph.get("nodes")):
        row = dict(raw)
        exactness = str(row.get("exactness") or "unknown")
        if exactness == "snapshot":
            provenance = _architecture_provenance(
                "snapshot_fact",
                "snapshot",
                comparison="none",
                note="Snapshot fact is exact, but base-vs-Snapshot architecture comparison is unavailable.",
            )
        elif exactness == "current_index":
            provenance = _architecture_provenance(
                "current_index",
                "current_index",
                comparison="none",
                note="Current-index context only; architecture delta is unavailable.",
            )
        else:
            exactness = "unknown"
            provenance = _architecture_provenance(
                "unavailable",
                "unknown",
                comparison="none",
                note="Architecture exactness is unavailable.",
            )
        row["exactness"] = exactness
        row["delta_status"] = "unknown" if exactness == "snapshot" else "context_only"
        row["architecture_provenance"] = provenance
        selection = dict(row.get("selection") or {})
        selection.update(
            {
                "projection": "architecture_delta",
                "exactness": exactness,
                "delta_status": row["delta_status"],
                "architecture_source": provenance["source"],
                "architecture_comparison": "none",
            }
        )
        row["selection"] = selection
        nodes.append(row)

    id_to_path = {str(node.get("id") or ""): str(node.get("path") or "") for node in nodes}
    edges: list[dict[str, Any]] = []
    for raw in _mapping_rows(context_graph.get("edges")):
        row = dict(raw)
        exactness = str(row.get("exactness") or "unknown")
        delta_status = "unknown" if exactness == "snapshot" else "context_only"
        provenance = _architecture_provenance(
            (
                "snapshot_fact"
                if exactness == "snapshot"
                else "current_index" if exactness == "current_index" else "unavailable"
            ),
            exactness if exactness in {"snapshot", "current_index"} else "unknown",
            comparison="none",
            note=(
                "Relationship fact is available, but no base-vs-Snapshot comparison supports a delta claim."
                if exactness == "snapshot"
                else "Relationship is current-index context only; no Snapshot delta claim is made."
            ),
        )
        row["delta_status"] = delta_status
        row["architecture_provenance"] = provenance
        row["source_path"] = id_to_path.get(str(row.get("source") or ""), "")
        row["target_path"] = id_to_path.get(str(row.get("target") or ""), "")
        selection = dict(row.get("selection") or {})
        selection.update(
            {
                "projection": "architecture_delta",
                "exactness": exactness,
                "delta_status": delta_status,
                "architecture_source": provenance["source"],
                "architecture_comparison": "none",
            }
        )
        row["selection"] = selection
        edges.append(row)

    degraded = ["snapshot_architecture_comparison_unavailable", *_current_graph_degradation(context)]
    return {
        "version": ARCHITECTURE_DELTA_VERSION,
        "digest": "",
        "status": "unavailable",
        "reason": reason,
        "base_identity": "",
        "snapshot_identity": "",
        "grouping": {
            "strategy": "directory",
            "version": ARCHITECTURE_GROUPING_VERSION,
            "stable_for_identical_input": True,
            "components": [],
        },
        "summary": {
            "material": False,
            "status": "unavailable",
            "changed_components": 0,
            "changed_nodes": 0,
            "changed_edges": 0,
            "added_edges": 0,
            "removed_edges": 0,
            "modified_edges": 0,
            "context_only_edges": sum(edge.get("exactness") == "current_index" for edge in edges),
            "unknown_edges": sum(edge.get("delta_status") == "unknown" for edge in edges),
            "important_concerns": [],
            "exactness": "unknown",
            "degraded": degraded,
        },
        "flow": {
            "applicable": False,
            "kind": "none",
            "reason": "Exact architecture comparison is unavailable, so no sequence/flow delta is claimed.",
            "steps": [],
        },
        "graph": {
            "level": "architecture",
            "focus": next((node.get("id") for node in nodes if node.get("status") in {"new", "changed"}), None),
            "nodes": nodes,
            "edges": edges,
            "truncated": bool(context_graph.get("truncated")),
            "omitted_node_count": int(context_graph.get("omitted_node_count") or 0),
            "stats": dict(context_graph.get("stats") or {}),
            "reason": reason,
        },
    }


def build_architecture_delta_from_revision(
    *,
    store: ReviewStore,
    repo_root: Path,
    revision: ReviewRevision,
    packet: Mapping[str, Any],
    context_graph: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Materialize immutable sides and produce the exact comparison projection."""

    from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, materialize_review_side

    cache_root = store.root / "cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="lc-architecture-delta-", dir=cache_root) as raw:
            root = Path(raw)
            old_root = root / "old"
            new_root = root / "new"
            base_identity = materialize_review_side(
                store.root,
                repo_root,
                revision,
                side="old",
                target=old_root,
                store=store,
            )
            snapshot_identity = materialize_review_side(
                store.root,
                repo_root,
                revision,
                side="new",
                target=new_root,
                store=store,
            )
            base = _reference_architecture(old_root)
            snapshot = _reference_architecture(new_root)
    except (OSError, RuntimeError, ValueError, ReviewSnapshotUnavailable) as exc:
        return unavailable_architecture_delta(
            review_id=revision.review_id,
            revision_id=revision.id,
            context_graph=context_graph,
            context=context,
            reason=f"Snapshot architecture comparison is unavailable ({type(exc).__name__}).",
        )
    except ModuleNotFoundError as exc:
        if not module_absent(exc, REFERENCE_GRAPH):
            raise
        return unavailable_architecture_delta(
            review_id=revision.review_id,
            revision_id=revision.id,
            context_graph=context_graph,
            context=context,
            reason="Snapshot architecture comparison needs the repository map, which this build does not include.",
        )

    return build_architecture_delta(
        review_id=revision.review_id,
        revision_id=revision.id,
        packet=packet,
        base_architecture=base,
        snapshot_architecture=snapshot,
        context_graph=context_graph,
        context=context,
        base_identity=base_identity,
        snapshot_identity=snapshot_identity,
    )


def architecture_explain_context(
    delta: Mapping[str, Any],
    *,
    allowed_paths: Collection[str] | None = None,
) -> dict[str, Any]:
    """Bounded architecture narration context, filtered before model construction."""

    raw_graph = delta.get("graph")
    graph: Mapping[str, Any] = raw_graph if isinstance(raw_graph, Mapping) else {}
    raw_summary = delta.get("summary")
    summary: Mapping[str, Any] = raw_summary if isinstance(raw_summary, Mapping) else {}
    raw_grouping = delta.get("grouping")
    grouping: Mapping[str, Any] = raw_grouping if isinstance(raw_grouping, Mapping) else {}
    nodes = {str(row.get("id") or ""): row for row in _mapping_rows(graph.get("nodes")) if str(row.get("id") or "")}
    allowed = None if allowed_paths is None else frozenset(str(path) for path in allowed_paths)

    changed_edges: list[dict[str, Any]] = []
    for edge in _mapping_rows(graph.get("edges")):
        if str(edge.get("exactness") or "") != "snapshot":
            continue
        if str(edge.get("delta_status") or "") not in {"added", "removed", "changed"}:
            continue
        source = str((nodes.get(str(edge.get("source") or "")) or {}).get("path") or "")
        target = str((nodes.get(str(edge.get("target") or "")) or {}).get("path") or "")
        if allowed is not None and (not source or not target or source not in allowed or target not in allowed):
            continue
        changed_edges.append(
            {
                "id": str(edge.get("id") or ""),
                "source": source,
                "target": target,
                "relationship": str(edge.get("kind") or "relationship"),
                "delta_status": str(edge.get("delta_status") or "unknown"),
                "base_weight": int(edge.get("base_weight") or 0),
                "snapshot_weight": int(edge.get("snapshot_weight") or 0),
                "exactness": "snapshot",
            }
        )
        if len(changed_edges) >= 24:
            break

    if allowed is None:
        safe_summary = dict(summary)
        safe_flow = dict(delta.get("flow") or {}) if isinstance(delta.get("flow"), Mapping) else {}
    else:
        components = {
            _component(str(row[field]))
            for row in changed_edges
            for field in ("source", "target")
            if str(row.get(field) or "")
        }
        statuses = [str(row.get("delta_status") or "") for row in changed_edges]
        safe_summary = {
            "material": bool(changed_edges),
            "status": str(summary.get("status") or "ready"),
            "changed_components": len(components),
            "changed_nodes": len({str(row[field]) for row in changed_edges for field in ("source", "target")}),
            "changed_edges": len(changed_edges),
            "added_edges": statuses.count("added"),
            "removed_edges": statuses.count("removed"),
            "modified_edges": statuses.count("changed"),
            "exactness": "snapshot",
            "degraded": [str(value) for value in list(summary.get("degraded") or [])],
        }
        flow_applicable = len(changed_edges) >= 2 and len(components) >= 3
        safe_flow = {
            "applicable": flow_applicable,
            "kind": "changed_relationship_flow" if flow_applicable else "none",
            "reason": (
                "Multiple policy-authorized exact relationship changes cross at least three components."
                if flow_applicable
                else "The policy-authorized exact relationship delta is too small for a useful flow explanation."
            ),
            "steps": (
                [
                    {
                        "edge_id": str(row["id"]),
                        "from": str(row["source"]),
                        "to": str(row["target"]),
                        "relationship": str(row["relationship"]),
                        "delta_status": str(row["delta_status"]),
                        "base_weight": int(row["base_weight"]),
                        "snapshot_weight": int(row["snapshot_weight"]),
                    }
                    for row in changed_edges[:_MAX_FLOW_STEPS]
                ]
                if flow_applicable
                else []
            ),
        }

    semantic_digest = _digest(
        {
            "summary": safe_summary,
            "changed_edges": changed_edges,
            "flow": safe_flow,
            "grouping": str(grouping.get("strategy") or "directory"),
        }
    )
    return {
        "architecture_delta_digest": semantic_digest,
        "summary": safe_summary,
        "grouping": {"strategy": str(grouping.get("strategy") or "directory")},
        "changed_edges": changed_edges,
        "flow": safe_flow,
        "excluded_context_only_edges": sum(
            str(row.get("exactness") or "") == "current_index" for row in _mapping_rows(graph.get("edges"))
        ),
        "limitations": [str(value) for value in list(summary.get("degraded") or [])],
    }


def _sanitize_explanation(result: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        str(row.get("id") or "") for row in _mapping_rows(context.get("changed_edges")) if str(row.get("id") or "")
    }
    grounded = [str(value) for value in list(result.get("grounded_edge_ids") or []) if str(value) in allowed][:12]
    flow: list[dict[str, str]] = []
    for raw in _mapping_rows(result.get("flow")):
        edge_id = str(raw.get("edge_id") or "")
        if edge_id not in allowed:
            continue
        flow.append({"edge_id": edge_id, "explanation": str(raw.get("explanation") or "")[:1000]})
        if len(flow) >= 8:
            break
    return {
        "summary": str(result.get("summary") or "")[:4000],
        "why_it_matters": [str(value)[:1000] for value in list(result.get("why_it_matters") or [])[:4]],
        "grounded_edge_ids": grounded,
        "flow": flow,
        "limitations": [str(value)[:1000] for value in list(result.get("limitations") or [])[:4]],
        "provenance": "inferred_explanation",
        "exactness": "inferred",
    }


def run_architecture_explanation(
    *,
    store: Any,
    review_id: str,
    revision_id: str,
    snapshot_identity: str,
    delta: Mapping[str, Any],
    driver: str | None,
    model: str | None,
    budget: Any,
    policy_context: Mapping[str, Any] | None = None,
    allowed_paths: Collection[str] | None = None,
    project_root: Path | None = None,
    executor: Callable[[], Any] | None = None,
) -> Any:
    """Run one cached bounded narration over deterministic architecture facts."""

    from lemoncrow.pro.capabilities.review.intelligence_runtime import (
        IntelligenceRunRequest,
        StructuredExecution,
        execute_structured,
        run_intelligence,
    )

    context = architecture_explain_context(delta, allowed_paths=allowed_paths)
    scope = {
        "architecture_delta_digest": str(delta.get("digest") or ""),
        "changed_edge_ids": [str(row.get("id") or "") for row in _mapping_rows(context.get("changed_edges"))],
    }
    policy = {
        "review": dict(policy_context or {}),
        "read_only": True,
        "narration_only": True,
        "snapshot_delta_authority": "deterministic_comparison",
        "current_index_is_context_only": True,
        "may_not_create_graph_facts": True,
    }
    request = IntelligenceRunRequest(
        review_id=review_id,
        revision_id=revision_id,
        snapshot_identity=snapshot_identity,
        capability="architecture_explain",
        scope=scope,
        context=context,
        policy=policy,
        driver=driver,
        model=model,
        output_schema=ARCHITECTURE_EXPLAIN_OUTPUT_SCHEMA,
        prompt_version=ARCHITECTURE_EXPLAIN_PROMPT_VERSION,
        schema_version=ARCHITECTURE_EXPLAIN_SCHEMA_VERSION,
        budget=budget,
    )

    system = """You explain a LemonCrow Architecture Delta. The graph facts are already computed deterministically.
Do not add, remove, or promote graph nodes or relationships. Only relationships in changed_edges are Snapshot-exact delta evidence.
Current-index relationships are intentionally excluded from changed_edges and can never be described as introduced, removed, or changed by this Snapshot.
You may explain why exact relationship changes matter and, when the supplied flow says it is applicable, narrate a likely flow through those exact relationships.
For generic reference edges, use cautious dependency language; do not assert runtime execution order unless the supplied relationship kind establishes it.
Every flow statement must cite one supplied edge_id. State limitations when the evidence does not establish an execution path."""
    prompt = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def execute() -> StructuredExecution:
        if executor is not None:
            raw = executor()
        else:
            raw = execute_structured(
                system=system,
                prompt=prompt,
                output_schema=ARCHITECTURE_EXPLAIN_OUTPUT_SCHEMA,
                driver=driver,
                model=model,
                project_root=project_root,
                origin="machine",
                source_kind="review",
                timeout_s=budget.timeout_s,
            )
        return StructuredExecution(
            result=_sanitize_explanation(raw.result, context),
            driver=raw.driver,
            model=raw.model,
            usage=raw.usage,
        )

    return run_intelligence(store=store, request=request, executor=execute)


__all__ = [
    "ARCHITECTURE_DELTA_VERSION",
    "ARCHITECTURE_EXPLAIN_OUTPUT_SCHEMA",
    "ARCHITECTURE_EXPLAIN_PROMPT_VERSION",
    "ARCHITECTURE_EXPLAIN_SCHEMA_VERSION",
    "architecture_explain_context",
    "build_architecture_delta",
    "build_architecture_delta_from_revision",
    "run_architecture_explanation",
    "unavailable_architecture_delta",
]
