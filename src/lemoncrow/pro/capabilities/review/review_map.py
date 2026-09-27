"""Review-specific system-impact projection over the generic Code Map graph.

The immutable Review Snapshot owns every claim about what changed, what was
impacted, and what the reviewer is judging. Generic Code Map data is optional
surrounding context from the *current* repository index. The two sources are
kept explicit so a historical Review never presents today's call graph as a
revision-exact fact.

No Review state is stored here. Nodes and edges are bounded, deterministic
projections of ReviewPacket / ReviewTarget data plus optional read-only Code Map
queries.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lemoncrow.pro.capabilities.review.code_intelligence import CodeIntelligenceProvider

MapLevel = Literal["architecture", "symbol"]

_MAX_ARCHITECTURE_NODES = 64
_MAX_ARCHITECTURE_EDGES = 160
_MAX_SYMBOL_SEEDS = 8
_MAX_SYMBOL_GRAPH_NODES = 80
_MAX_MEMBER_PATHS = 24
_MAX_MEMBER_SYMBOLS = 20

_SECURITY_PARTS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "identity",
        "oauth",
        "oidc",
        "permission",
        "permissions",
        "rbac",
        "security",
    }
)
_PERSISTENCE_PARTS = frozenset(
    {
        "database",
        "db",
        "migration",
        "migrations",
        "persistence",
        "postgres",
        "repository",
        "sql",
        "storage",
        "store",
    }
)
_SITE_LINE = re.compile(r"^(?P<path>.+?):L(?P<line>\d+)(?:-L?(?P<end>\d+))?$")


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "\0".join(str(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _component_for_path(path: str) -> str:
    # Reuse the generic Code Map community partitioning without putting Review
    # semantics into the generic Code Map HTTP API.
    from lemoncrow.core.service.code_map import community_for_path

    return community_for_path(path)


def _boundary_for_path(path: str) -> tuple[str, ...]:
    words = {part.lower() for part in re.split(r"[/_.-]+", path) if part}
    out: list[str] = []
    if words & _SECURITY_PARTS:
        out.append("security")
    if words & _PERSISTENCE_PARTS:
        out.append("persistence")
    return tuple(out)


def _site_location(value: str) -> tuple[str, int, int]:
    match = _SITE_LINE.match(value.strip())
    if not match:
        return value.strip(), 0, 0
    line = int(match.group("line"))
    return match.group("path"), line, int(match.group("end") or line)


def _mapping_rows(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, (list, tuple)) else []


def _integer(value: object, default: int = -1) -> int:
    if value is None:
        return default
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return default


def _snapshot_symbols(packet: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return _mapping_rows(packet.get("symbols"))


def _snapshot_files(packet: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return _mapping_rows(packet.get("files"))


def _snapshot_impacts(packet: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return _mapping_rows(packet.get("impact"))


def _public_contract_paths(packet: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    for row in _mapping_rows(packet.get("order")):
        reasons = {str(reason).casefold() for reason in (row.get("reasons") or ())}
        if "public contract changed" in reasons:
            path = str(row.get("path") or "")
            if path:
                out.add(path)
    return out


def _target_rows(targets: Sequence[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target in targets:
        rows.append(
            {
                "target_id": str(getattr(target, "target_id", "")),
                "path": str(getattr(target, "path", "")),
                "start_line": int(getattr(target, "start_line", 0) or 0),
                "end_line": int(getattr(target, "end_line", 0) or 0),
                "symbol": str(getattr(target, "symbol", "")),
                "state": str(getattr(target, "state", "unknown")),
            }
        )
    return rows


def _targets_for_span(targets: Sequence[Mapping[str, Any]], path: str, start: int, end: int) -> list[str]:
    out: list[str] = []
    for target in targets:
        if str(target.get("path") or "") != path:
            continue
        target_start = int(target.get("start_line") or 0)
        target_end = int(target.get("end_line") or target_start)
        overlaps = not start or not target_start or (target_start <= end and target_end >= start)
        if overlaps:
            target_id = str(target.get("target_id") or "")
            if target_id:
                out.append(target_id)
    return out


def _file_attributions(packet: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for item in _mapping_rows(packet.get("attributions")):
        if str(item.get("scope") or "") != "file" or str(item.get("certainty") or "") != "exact":
            continue
        path = str(item.get("scope_key") or "")
        if not path:
            continue
        rows[path] = {
            "host": str(item.get("host") or ""),
            "model": str(item.get("model") or ""),
            "certainty": "exact",
            "method": str(item.get("method") or ""),
        }
    return rows


def _global_provenance(packet: Mapping[str, Any]) -> dict[str, str]:
    raw = packet.get("provenance")
    item: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return {
        "host": str(item.get("host") or ""),
        "model": str(item.get("model") or ""),
        "certainty": str(item.get("certainty") or "none"),
        "method": str(item.get("match_reason") or ""),
    }


def _verification_payload(evidence: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(item.get("status") or "UNKNOWN").upper() for item in evidence)
    return {
        "pass": statuses["PASS"],
        "fail": statuses["FAIL"],
        "not_run": statuses["NOT_RUN"],
        "unknown": statuses["UNKNOWN"],
        "checks": [
            {
                "name": str(item.get("name") or "Verification"),
                "status": str(item.get("status") or "UNKNOWN").upper(),
                "detail": str(item.get("detail") or ""),
                "source": str(item.get("source") or ""),
            }
            for item in evidence[:20]
        ],
    }


def _selection(
    *,
    review_id: str,
    revision_id: str,
    level: MapLevel,
    node_id: str,
    exactness: str,
    focus_node_id: str = "",
    side: str = "new",
    path: str = "",
    line: int = 0,
    end_line: int = 0,
    symbol: str = "",
    target_ids: Sequence[str] = (),
    current_symbol_id: str = "",
) -> dict[str, Any]:
    """Stable, explicit surface context reserved for Phase 7 Ask integration."""

    return {
        "kind": "review_map_node",
        "review_id": review_id,
        "revision_id": revision_id,
        "level": level,
        "node_id": node_id,
        "focus_node_id": focus_node_id,
        "exactness": exactness,
        "side": side,
        "path": path,
        "line": line,
        "end_line": end_line,
        "symbol": symbol,
        "target_ids": list(target_ids),
        "current_symbol_id": current_symbol_id,
    }


def _edge_selection(
    review_id: str,
    revision_id: str,
    edge: Mapping[str, Any],
    *,
    level: MapLevel,
    focus_node_id: str = "",
) -> dict[str, str]:
    return {
        "kind": "review_map_edge",
        "review_id": review_id,
        "revision_id": revision_id,
        "level": level,
        "focus_node_id": focus_node_id,
        "edge_id": str(edge.get("id") or ""),
        "source_id": str(edge.get("source") or ""),
        "target_id": str(edge.get("target") or ""),
        "relation": str(edge.get("kind") or ""),
        "exactness": str(edge.get("exactness") or ""),
    }


def _current_context(
    *,
    provider: CodeIntelligenceProvider | None,
    packet: Mapping[str, Any],
    snapshot_head_sha: str,
    selected_node_id: str = "",
    focus_only: bool = False,
    include_references: bool = False,
) -> dict[str, Any]:
    base = {
        "source": "unavailable",
        "exact_for_snapshot": False,
        "snapshot_head_matches_checkout": False,
        "generation_matches_snapshot_head": False,
        "index": {},
        "readiness": {},
        "note": "Surrounding Code Map context is unavailable; Snapshot-exact changed/impact facts are still shown.",
        "matches": {},
        "neighborhoods": {},
        "references": {},
    }
    if provider is None:
        return base
    try:
        readiness = provider.readiness()
        base["readiness"] = readiness.to_wire()
        base["index"] = {
            "backend": readiness.backend,
            "view_id": readiness.view_id,
            "view_revision": readiness.view_revision,
            "generation_id": readiness.generation_id,
            "base_revision": readiness.base_revision,
            "manifest_root": readiness.manifest_root,
        }
        same_generation = bool(snapshot_head_sha and readiness.base_revision == snapshot_head_sha)
        base["snapshot_head_matches_checkout"] = bool(readiness.backend == "legacy" and same_generation)
        base["generation_matches_snapshot_head"] = same_generation
        if not readiness.available:
            base["note"] = (
                f"Current code intelligence is unavailable ({readiness.reason or 'no native View'}); "
                "Snapshot-exact changed/impact facts are still shown."
            )
            return base

        public_paths = _public_contract_paths(packet)
        symbols = sorted(
            _snapshot_symbols(packet),
            key=lambda item: (
                0 if str(item.get("file_path") or "") in public_paths else 1,
                int(item.get("centrality_rank") or 10**9),
                -_integer(item.get("caller_count")),
                str(item.get("file_path") or ""),
                int(item.get("start_line") or 0),
            ),
        )
        if selected_node_id:
            selected_symbol = next(
                (
                    item
                    for item in symbols
                    if _stable_id(
                        "symbol",
                        item.get("file_path") or "",
                        item.get("qualified_name") or item.get("symbol_name") or "",
                        item.get("start_line") or 0,
                        item.get("change") or "unknown",
                    )
                    == selected_node_id
                ),
                None,
            )
            if selected_symbol is not None:
                symbols = [selected_symbol, *(item for item in symbols if item is not selected_symbol)]
            elif focus_only:
                symbols = []
        symbols = symbols[:1] if focus_only and symbols else symbols[:_MAX_SYMBOL_SEEDS]

        matches: dict[str, dict[str, Any]] = {}
        neighborhoods: dict[str, dict[str, Any]] = {}
        references: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            snapshot_id = _stable_id(
                "symbol",
                symbol.get("file_path") or "",
                symbol.get("qualified_name") or symbol.get("symbol_name") or "",
                symbol.get("start_line") or 0,
                symbol.get("change") or "unknown",
            )
            try:
                match = provider.find_symbol(
                    str(symbol.get("symbol_name") or ""),
                    path=str(symbol.get("file_path") or ""),
                    qualified_name=str(symbol.get("qualified_name") or ""),
                    start_line=int(symbol.get("start_line") or 0),
                    limit=12,
                )
            except Exception:
                match = None
            if not isinstance(match, Mapping):
                continue
            match_row = dict(match)
            matches[snapshot_id] = match_row
            try:
                neighborhood = provider.neighborhood(match_row, depth=1, limit=40)
            except Exception:
                neighborhood = None
            if isinstance(neighborhood, Mapping):
                neighborhoods[snapshot_id] = dict(neighborhood)
            if include_references and (not selected_node_id or snapshot_id == selected_node_id):
                try:
                    reference_rows = provider.references(match_row, limit=20)
                except Exception:
                    reference_rows = None
                if isinstance(reference_rows, Mapping):
                    references[snapshot_id] = dict(reference_rows)

        try:
            readiness = provider.readiness()
            base["readiness"] = readiness.to_wire()
        except Exception:
            pass
        graph = base["readiness"].get("graph") if isinstance(base["readiness"], Mapping) else None
        graph_state = str(graph.get("state") or "") if isinstance(graph, Mapping) else ""
        graph_reason = str(graph.get("reason") or "") if isinstance(graph, Mapping) else ""
        graph_note = (
            f" Graph intelligence is {graph_state}" + (f" ({graph_reason})" if graph_reason else "") + "."
            if graph_state and graph_state != "ready"
            else ""
        )
        base.update(
            {
                "source": "current_index",
                "exact_for_snapshot": False,
                "note": (
                    "Surrounding calls/references come from the current code-intelligence provider, "
                    "not the immutable Review Snapshot." + graph_note
                ),
                "matches": matches,
                "neighborhoods": neighborhoods,
                "references": references,
            }
        )
        return base
    except Exception as exc:
        base["note"] = (
            f"Current code intelligence is unavailable ({type(exc).__name__}); " "Snapshot-exact facts are still shown."
        )
        return base


def _architecture_projection(
    *,
    review_id: str,
    revision_id: str,
    packet: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a review as a bounded file relationship graph.

    Components are grouping metadata, not the primary visible nodes. Snapshot
    changed/impacted files are first-class nodes; current-index callers and
    references are bounded surrounding context. File-to-file relationships are
    preserved even when both files live in the same component.
    """

    public_paths = _public_contract_paths(packet)
    attributions = _file_attributions(packet)
    fallback_provenance = _global_provenance(packet)
    changed_files = _snapshot_files(packet)
    changed_symbols = sorted(
        _snapshot_symbols(packet),
        key=lambda item: (
            0 if str(item.get("file_path") or "") in public_paths else 1,
            int(item.get("centrality_rank") or 10**9),
            -_integer(item.get("caller_count")),
            str(item.get("file_path") or ""),
            int(item.get("start_line") or 0),
        ),
    )
    impacts = _snapshot_impacts(packet)

    files: dict[str, dict[str, Any]] = {}

    def file_label(path: str, component: str) -> str:
        prefix = component.rstrip("/") + "/"
        relative = path[len(prefix) :] if path.startswith(prefix) else Path(path).name
        return relative or Path(path).name or path

    def ensure_file(path: str) -> dict[str, Any]:
        row = files.get(path)
        if row is None:
            component = _component_for_path(path)
            row = {
                "id": _stable_id("file", path),
                "label": file_label(path, component),
                "kind": "file",
                "component": component,
                "path": path,
                "line": 0,
                "end_line": 0,
                "exactness": "current_index",
                "status": "context",
                "change_kinds": set(),
                "important_public_contract": False,
                "boundaries": set(),
                "uncertain": False,
                "symbols": [],
                "target_ids": set(),
                "current_symbol_ids": set(),
                "provenance": {},
            }
            files[path] = row
        return row

    file_status: dict[str, str] = {}
    for item in changed_files:
        path = str(item.get("path") or "")
        if not path:
            continue
        change = str(item.get("status") or "modified")
        file_status[path] = change
        row = ensure_file(path)
        row["exactness"] = "snapshot"
        row["status"] = "new" if change == "added" else "changed"
        row["change_kinds"].add(change)
        row["important_public_contract"] = path in public_paths
        row["boundaries"].update(_boundary_for_path(path))
        row["provenance"] = attributions.get(path) or fallback_provenance
        row["target_ids"].update(_targets_for_span(targets, path, 0, 0))

    for symbol in changed_symbols:
        path = str(symbol.get("file_path") or "")
        if not path:
            continue
        row = ensure_file(path)
        row["exactness"] = "snapshot"
        if row["status"] == "context":
            change = str(symbol.get("change") or "modified")
            row["status"] = "new" if change == "added" else "changed"
            row["change_kinds"].add(change)
        row["symbols"].append(
            {
                "id": _stable_id(
                    "symbol",
                    path,
                    symbol.get("qualified_name") or symbol.get("symbol_name") or "",
                    symbol.get("start_line") or 0,
                    symbol.get("change") or "unknown",
                ),
                "label": str(symbol.get("symbol_name") or symbol.get("qualified_name") or path),
                "qualified_name": str(symbol.get("qualified_name") or ""),
                "path": path,
                "line": int(symbol.get("start_line") or 0),
                "end_line": int(symbol.get("end_line") or symbol.get("start_line") or 0),
                "change": str(symbol.get("change") or "unknown"),
                "caller_count": _integer(symbol.get("caller_count")),
                "usage_count": _integer(symbol.get("usage_count")),
                "centrality_rank": symbol.get("centrality_rank"),
                "centrality_percentile": symbol.get("centrality_percentile"),
            }
        )
        row["important_public_contract"] = bool(row["important_public_contract"] or path in public_paths)
        row["boundaries"].update(_boundary_for_path(path))
        row["target_ids"].update(
            _targets_for_span(
                targets,
                path,
                int(symbol.get("start_line") or 0),
                int(symbol.get("end_line") or symbol.get("start_line") or 0),
            )
        )

    file_edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def ensure_edge(
        source_path: str,
        target_path: str,
        kind: str,
        *,
        exactness: str,
    ) -> dict[str, Any] | None:
        if not source_path or not target_path or source_path == target_path:
            return None
        source = ensure_file(source_path)
        target = ensure_file(target_path)
        key = (source_path, target_path, kind)
        edge = file_edges.get(key)
        if edge is None:
            edge = {
                "id": _stable_id("edge", source_path, target_path, kind),
                "source": source["id"],
                "target": target["id"],
                "kind": kind,
                "exactness": exactness,
                "weight": 0,
                "uncertain": False,
                "evidence": [],
            }
            file_edges[key] = edge
        elif edge["exactness"] != "snapshot" and exactness == "snapshot":
            edge["exactness"] = "snapshot"
        return edge

    # Frozen impact facts are the strongest relationships in the overview.
    for impact in impacts:
        source_path = str(impact.get("source_path") or "")
        site_path, line, impact_end = _site_location(str(impact.get("path") or ""))
        if not site_path:
            continue

        target = ensure_file(site_path)
        if site_path not in file_status:
            target["exactness"] = "snapshot"
            target["status"] = "impacted"
            target["line"] = target["line"] or line
            target["end_line"] = target["end_line"] or impact_end
            target["boundaries"].update(_boundary_for_path(site_path))
            target["uncertain"] = bool(target["uncertain"] or str(impact.get("uncertainty") or ""))

        if source_path:
            source = ensure_file(source_path)
            if source_path in file_status:
                source["exactness"] = "snapshot"
            edge = ensure_edge(source_path, site_path, "impacts", exactness="snapshot")
            if edge is not None:
                edge["weight"] += 1
                edge["uncertain"] = bool(edge["uncertain"] or str(impact.get("uncertainty") or ""))
                if len(edge["evidence"]) < 4:
                    edge["evidence"].append(
                        {
                            "kind": str(impact.get("kind") or "impact"),
                            "path": site_path,
                            "line": line,
                            "uncertainty": str(impact.get("uncertainty") or ""),
                        }
                    )

    matches_raw = context.get("matches")
    matches: Mapping[str, Any] = matches_raw if isinstance(matches_raw, Mapping) else {}
    neighborhoods_raw = context.get("neighborhoods")
    neighborhoods: Mapping[str, Any] = neighborhoods_raw if isinstance(neighborhoods_raw, Mapping) else {}

    # Aggregate current symbol neighborhoods into file-to-file relationships.
    # Unlike the old component projection, same-component edges are preserved.
    for snapshot_id, match in matches.items():
        if not isinstance(match, Mapping):
            continue
        matched_path = str(match.get("path") or "")
        if matched_path:
            matched_file = ensure_file(matched_path)
            current_id = str(match.get("id") or "")
            if current_id:
                matched_file["current_symbol_ids"].add(current_id)

        neighborhood = neighborhoods.get(snapshot_id)
        if not isinstance(neighborhood, Mapping):
            continue

        current_nodes = {
            str(node.get("id") or ""): node
            for node in _mapping_rows(neighborhood.get("nodes"))
            if str(node.get("id") or "") and str(node.get("path") or "")
        }
        current_paths: dict[str, str] = {}
        for node_id, node in current_nodes.items():
            node_path = str(node.get("path") or "")
            current_paths[node_id] = node_path
            row = ensure_file(node_path)
            row["current_symbol_ids"].add(node_id)
            if row["status"] == "context":
                row["line"] = row["line"] or int(node.get("line") or 0)
                row["end_line"] = row["end_line"] or int(node.get("end_line") or node.get("line") or 0)

        for current_edge in _mapping_rows(neighborhood.get("edges")):
            source_path = current_paths.get(str(current_edge.get("source") or ""), "")
            target_path = current_paths.get(str(current_edge.get("target") or ""), "")
            relation_kind = str(current_edge.get("kind") or "references")
            edge = ensure_edge(source_path, target_path, relation_kind, exactness="current_index")
            if edge is not None:
                edge["weight"] += int(current_edge.get("weight") or 1)

    degree: Counter[str] = Counter()
    for edge in file_edges.values():
        weight = max(1, int(edge.get("weight") or 1))
        degree[str(edge["source"])] += weight
        degree[str(edge["target"])] += weight

    def file_priority(row: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
        status = str(row.get("status") or "context")
        order = {"new": 0, "changed": 0, "impacted": 1, "context": 2}
        return (
            order.get(status, 2),
            0 if row.get("important_public_contract") else 1,
            -len(row.get("symbols") or ()),
            -degree[str(row.get("id") or "")],
            str(row.get("path") or ""),
        )

    ordered = sorted(files.values(), key=file_priority)
    selected = ordered[:_MAX_ARCHITECTURE_NODES]
    selected_ids = {str(row["id"]) for row in selected}

    edge_rows = [
        edge
        for edge in file_edges.values()
        if str(edge.get("source") or "") in selected_ids and str(edge.get("target") or "") in selected_ids
    ]
    edge_rows.sort(
        key=lambda edge: (
            0 if edge.get("exactness") == "snapshot" else 1,
            -int(edge.get("weight") or 0),
            str(edge.get("source") or ""),
            str(edge.get("target") or ""),
            str(edge.get("kind") or ""),
        )
    )
    edge_rows = edge_rows[:_MAX_ARCHITECTURE_EDGES]

    payload_nodes: list[dict[str, Any]] = []
    for row in selected:
        symbols = list(row["symbols"])
        target_ids = sorted(row["target_ids"])
        current_symbol_ids = sorted(item for item in row["current_symbol_ids"] if item)
        node = {
            "id": row["id"],
            "label": row["label"],
            "kind": "file",
            "component": row["component"],
            "path": row["path"],
            "line": int(row["line"] or 0),
            "end_line": int(row["end_line"] or row["line"] or 0),
            "status": row["status"],
            "exactness": row["exactness"],
            # Retain the old aggregate fields for compatible inspectors/clients.
            "changed_file_count": 1 if row["status"] in {"new", "changed"} else 0,
            "impacted_file_count": 1 if row["status"] == "impacted" else 0,
            "paths": [row["path"]],
            "paths_truncated": False,
            "symbols": symbols[:_MAX_MEMBER_SYMBOLS],
            "symbols_truncated": len(symbols) > _MAX_MEMBER_SYMBOLS,
            "target_ids": target_ids,
            "current_symbol_ids": current_symbol_ids[:_MAX_SYMBOL_SEEDS],
            "change_kinds": sorted(row["change_kinds"]),
            "important_public_contract": bool(row["important_public_contract"]),
            "boundaries": sorted(row["boundaries"]),
            "boundary_basis": "path" if row["boundaries"] else "",
            "uncertain": bool(row["uncertain"]),
            "provenance": row["provenance"],
            "relationship_count": degree[str(row["id"])],
            "selection": _selection(
                review_id=review_id,
                revision_id=revision_id,
                level="architecture",
                node_id=str(row["id"]),
                exactness=str(row["exactness"]),
                side=(
                    "old"
                    if row["change_kinds"] and set(row["change_kinds"]).issubset({"deleted", "removed"})
                    else "new"
                ),
                path=str(row["path"] or ""),
                line=int(row["line"] or 0),
                end_line=int(row["end_line"] or row["line"] or 0),
                target_ids=target_ids,
                current_symbol_id=current_symbol_ids[0] if current_symbol_ids else "",
            ),
        }
        payload_nodes.append(node)

    for edge in edge_rows:
        edge["selection"] = _edge_selection(review_id, revision_id, edge, level="architecture")

    return {
        "level": "architecture",
        "focus": next((node["id"] for node in payload_nodes if node["status"] in {"new", "changed"}), None),
        "nodes": payload_nodes,
        "edges": edge_rows,
        "truncated": len(ordered) > len(selected) or len(file_edges) > len(edge_rows),
        "omitted_node_count": max(0, len(ordered) - len(selected)),
    }


def _symbol_projection(
    *,
    review_id: str,
    revision_id: str,
    packet: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
    selected_node_id: str,
) -> dict[str, Any]:
    public_paths = _public_contract_paths(packet)
    attributions = _file_attributions(packet)
    fallback_provenance = _global_provenance(packet)
    symbols = _snapshot_symbols(packet)
    impacts = _snapshot_impacts(packet)
    snapshot_by_id: dict[str, Mapping[str, Any]] = {}
    for symbol in symbols:
        node_id = _stable_id(
            "symbol",
            symbol.get("file_path") or "",
            symbol.get("qualified_name") or symbol.get("symbol_name") or "",
            symbol.get("start_line") or 0,
            symbol.get("change") or "unknown",
        )
        snapshot_by_id[node_id] = symbol
    selected = snapshot_by_id.get(selected_node_id)
    if selected_node_id and selected is None:
        return {
            "level": "symbol",
            "focus": None,
            "nodes": [],
            "edges": [],
            "truncated": False,
            "reason": "The selected map symbol is not part of this immutable Snapshot.",
        }
    if selected is None:
        selected = next(iter(snapshot_by_id.values()), None)
        selected_node_id = next(iter(snapshot_by_id), "")
    if selected is None:
        return {
            "level": "symbol",
            "focus": None,
            "nodes": [],
            "edges": [],
            "truncated": False,
            "reason": "This Snapshot contains no changed symbols to drill into.",
        }

    path = str(selected.get("file_path") or "")
    start = int(selected.get("start_line") or 0)
    end = int(selected.get("end_line") or start)
    target_ids = _targets_for_span(targets, path, start, end)
    matches = context.get("matches") if isinstance(context.get("matches"), Mapping) else {}
    neighborhoods = context.get("neighborhoods") if isinstance(context.get("neighborhoods"), Mapping) else {}
    references = context.get("references") if isinstance(context.get("references"), Mapping) else {}
    current_match = matches.get(selected_node_id) if isinstance(matches, Mapping) else None
    current_symbol_id = str(current_match.get("id") or "") if isinstance(current_match, Mapping) else ""

    nodes: dict[str, dict[str, Any]] = {}
    nodes[selected_node_id] = {
        "id": selected_node_id,
        "label": str(selected.get("symbol_name") or selected.get("qualified_name") or path),
        "qualified_name": str(selected.get("qualified_name") or ""),
        "kind": str(selected.get("kind") or "symbol"),
        "path": path,
        "line": start,
        "end_line": end,
        "status": "new" if str(selected.get("change") or "") == "added" else "changed",
        "change": str(selected.get("change") or "unknown"),
        "exactness": "snapshot",
        "important_public_contract": path in public_paths,
        "boundaries": list(_boundary_for_path(path)),
        "boundary_basis": "path" if _boundary_for_path(path) else "",
        "caller_count": _integer(selected.get("caller_count")),
        "usage_count": _integer(selected.get("usage_count")),
        "centrality_rank": selected.get("centrality_rank"),
        "centrality_percentile": selected.get("centrality_percentile"),
        "target_ids": target_ids,
        "current_symbol_id": current_symbol_id,
        "provenance": attributions.get(path) or fallback_provenance,
        "selection": _selection(
            review_id=review_id,
            revision_id=revision_id,
            level="symbol",
            node_id=selected_node_id,
            exactness="snapshot",
            side="old" if str(selected.get("change") or "") in {"deleted", "removed"} else "new",
            path=path,
            line=start,
            end_line=end,
            symbol=str(selected.get("qualified_name") or selected.get("symbol_name") or ""),
            target_ids=target_ids,
            current_symbol_id=current_symbol_id,
        ),
    }

    edges: dict[str, dict[str, Any]] = {}
    for index, impact in enumerate(impacts):
        if str(impact.get("source_path") or "") != path:
            continue
        impact_path, line, impact_end = _site_location(str(impact.get("path") or ""))
        if not impact_path or impact_path in {str(item.get("path") or "") for item in _snapshot_files(packet)}:
            continue
        impact_id = _stable_id("impact", selected_node_id, impact_path, line, index)
        nodes[impact_id] = {
            "id": impact_id,
            "label": f"{Path(impact_path).name}:{line}" if line else Path(impact_path).name,
            "qualified_name": "",
            "kind": str(impact.get("kind") or "impact"),
            "path": impact_path,
            "line": line,
            "end_line": impact_end,
            "status": "impacted",
            "change": "unchanged",
            "exactness": "snapshot",
            "important_public_contract": False,
            "boundaries": list(_boundary_for_path(impact_path)),
            "boundary_basis": "path" if _boundary_for_path(impact_path) else "",
            "uncertain": bool(str(impact.get("uncertainty") or "")),
            "uncertainty": str(impact.get("uncertainty") or ""),
            "target_ids": [],
            "current_symbol_id": "",
            "selection": _selection(
                review_id=review_id,
                revision_id=revision_id,
                level="symbol",
                node_id=impact_id,
                exactness="snapshot",
                focus_node_id=selected_node_id,
                path=impact_path,
                line=line,
                end_line=impact_end,
            ),
        }
        edge_id = _stable_id("edge", selected_node_id, impact_id, "impacts")
        edges[edge_id] = {
            "id": edge_id,
            "source": selected_node_id,
            "target": impact_id,
            "kind": "impacts",
            "exactness": "snapshot",
            "weight": 1,
            "uncertain": bool(str(impact.get("uncertainty") or "")),
        }

    context_truncated = False
    neighborhood = neighborhoods.get(selected_node_id) if isinstance(neighborhoods, Mapping) else None
    if isinstance(neighborhood, Mapping):
        remap: dict[str, str] = {}
        for node in _mapping_rows(neighborhood.get("nodes")):
            raw_id = str(node.get("id") or "")
            if not raw_id:
                continue
            if raw_id == current_symbol_id:
                remap[raw_id] = selected_node_id
                continue
            mapped_id = f"current:{raw_id}"
            remap[raw_id] = mapped_id
            if len(nodes) >= _MAX_SYMBOL_GRAPH_NODES:
                context_truncated = True
                continue
            node_path = str(node.get("path") or "")
            nodes[mapped_id] = {
                "id": mapped_id,
                "label": str(node.get("label") or node.get("qualified_name") or "symbol"),
                "qualified_name": str(node.get("qualified_name") or ""),
                "kind": str(node.get("kind") or "symbol"),
                "path": node_path,
                "line": int(node.get("line") or 0),
                "end_line": int(node.get("end_line") or node.get("line") or 0),
                "status": "context",
                "change": "unknown",
                "exactness": "current_index",
                "important_public_contract": False,
                "boundaries": list(_boundary_for_path(node_path)),
                "boundary_basis": "path" if _boundary_for_path(node_path) else "",
                "target_ids": [],
                "current_symbol_id": raw_id,
                "selection": _selection(
                    review_id=review_id,
                    revision_id=revision_id,
                    level="symbol",
                    node_id=mapped_id,
                    exactness="current_index",
                    focus_node_id=selected_node_id,
                    path=node_path,
                    line=int(node.get("line") or 0),
                    end_line=int(node.get("end_line") or node.get("line") or 0),
                    symbol=str(node.get("qualified_name") or node.get("label") or ""),
                    current_symbol_id=raw_id,
                ),
            }
        for edge in _mapping_rows(neighborhood.get("edges")):
            source = remap.get(str(edge.get("source") or ""))
            target = remap.get(str(edge.get("target") or ""))
            if not source or not target or source not in nodes or target not in nodes or source == target:
                continue
            relation_kind = str(edge.get("kind") or "reference")
            edge_id = _stable_id("edge", source, target, relation_kind)
            edges[edge_id] = {
                "id": edge_id,
                "source": source,
                "target": target,
                "kind": relation_kind,
                "exactness": "current_index",
                "weight": int(edge.get("weight") or 1),
                "uncertain": False,
            }

    reference_payload = references.get(selected_node_id) if isinstance(references, Mapping) else None
    if isinstance(reference_payload, Mapping):
        for index, reference in enumerate(_mapping_rows(reference_payload.get("references"))):
            if len(nodes) >= _MAX_SYMBOL_GRAPH_NODES:
                context_truncated = True
                break
            reference_path = str(reference.get("path") or "")
            reference_line = int(reference.get("line") or 0)
            if not reference_path or reference_line <= 0:
                continue
            reference_id = _stable_id("reference", selected_node_id, reference_path, reference_line, index)
            nodes[reference_id] = {
                "id": reference_id,
                "label": f"{Path(reference_path).name}:{reference_line}",
                "qualified_name": str(reference.get("caller") or ""),
                "kind": str(reference.get("edge_kind") or "reference"),
                "path": reference_path,
                "line": reference_line,
                "end_line": int(reference.get("end_line") or reference_line),
                "status": "context",
                "change": "unknown",
                "exactness": "current_index",
                "important_public_contract": False,
                "boundaries": list(_boundary_for_path(reference_path)),
                "boundary_basis": "path" if _boundary_for_path(reference_path) else "",
                "target_ids": [],
                "current_symbol_id": "",
                "selection": _selection(
                    review_id=review_id,
                    revision_id=revision_id,
                    level="symbol",
                    node_id=reference_id,
                    exactness="current_index",
                    focus_node_id=selected_node_id,
                    path=reference_path,
                    line=reference_line,
                    end_line=int(reference.get("end_line") or reference_line),
                    symbol=str(reference.get("caller") or ""),
                ),
            }
            edge_id = _stable_id("edge", reference_id, selected_node_id, "references")
            edges[edge_id] = {
                "id": edge_id,
                "source": reference_id,
                "target": selected_node_id,
                "kind": "references",
                "exactness": "current_index",
                "weight": 1,
                "uncertain": False,
            }

    visible_nodes = list(nodes.values())[:_MAX_SYMBOL_GRAPH_NODES]
    visible_ids = {str(node["id"]) for node in visible_nodes}
    edge_rows = [
        edge
        for edge in edges.values()
        if str(edge.get("source") or "") in visible_ids and str(edge.get("target") or "") in visible_ids
    ]
    edge_rows.sort(key=lambda item: (0 if item["exactness"] == "snapshot" else 1, str(item["id"])))
    for edge in edge_rows:
        edge["selection"] = _edge_selection(
            review_id, revision_id, edge, level="symbol", focus_node_id=selected_node_id
        )
    return {
        "level": "symbol",
        "focus": selected_node_id,
        "nodes": visible_nodes,
        "edges": edge_rows,
        "truncated": bool(
            len(nodes) > _MAX_SYMBOL_GRAPH_NODES
            or context_truncated
            or (isinstance(neighborhood, Mapping) and bool(neighborhood.get("truncated")))
            or (isinstance(reference_payload, Mapping) and bool(reference_payload.get("truncated")))
        ),
        "reason": "",
    }


def build_review_map(
    *,
    review_id: str,
    revision: Any,
    packet: Mapping[str, Any] | None,
    targets: Sequence[Any],
    evidence: Sequence[Mapping[str, Any]],
    context_repo_root: Path | None,
    code_intelligence: CodeIntelligenceProvider | None = None,
    level: MapLevel = "architecture",
    selected_node_id: str = "",
) -> dict[str, Any]:
    """Build one bounded map for an immutable Review Snapshot.

    Frozen ReviewPacket/ReviewTarget facts are always available when captured.
    Current code intelligence is best-effort and is *never* upgraded to
    Snapshot-exact, even when its generation points at the same commit.
    """

    packet_data: Mapping[str, Any] = packet if isinstance(packet, Mapping) else {}
    revision_id = str(getattr(revision, "id", ""))
    target_rows = _target_rows(targets)
    provider = code_intelligence
    if provider is None and context_repo_root is not None and context_repo_root.is_dir():
        try:
            from lemoncrow.core.service.review_code_intelligence import LegacyReviewCodeIntelligence

            provider = LegacyReviewCodeIntelligence(context_repo_root)
        except Exception:
            provider = None
    context = _current_context(
        provider=provider,
        packet=packet_data,
        snapshot_head_sha=str(getattr(revision, "head_sha", "")),
        selected_node_id=selected_node_id,
        focus_only=level == "symbol",
        include_references=level == "symbol",
    )
    if level == "symbol":
        graph = _symbol_projection(
            review_id=review_id,
            revision_id=revision_id,
            packet=packet_data,
            targets=target_rows,
            context=context,
            selected_node_id=selected_node_id,
        )
    else:
        graph = _architecture_projection(
            review_id=review_id,
            revision_id=revision_id,
            packet=packet_data,
            targets=target_rows,
            context=context,
        )

    degraded = sorted(
        set(str(item) for item in (getattr(revision, "degraded", ()) or ()))
        | set(str(item) for item in (packet_data.get("degraded") or ()))
    )
    return {
        "review_id": review_id,
        "revision_id": revision_id,
        "revision_number": max(0, int(getattr(revision, "revision_number", 1) or 1) - 1),
        "level": level,
        "snapshot": {
            "exactness": "snapshot",
            "tree_fingerprint": str(getattr(revision, "tree_fingerprint", "")),
            "source_fingerprint": str(getattr(revision, "source_fingerprint", "")),
            "base_sha": str(getattr(revision, "base_sha", "")),
            "head_sha": str(getattr(revision, "head_sha", "")),
            "degraded": degraded,
            "provenance": _global_provenance(packet_data),
            "verification": _verification_payload(evidence),
        },
        "context": {
            "source": str(context.get("source") or "unavailable"),
            "exact_for_snapshot": False,
            "snapshot_head_matches_checkout": bool(context.get("snapshot_head_matches_checkout")),
            "generation_matches_snapshot_head": bool(context.get("generation_matches_snapshot_head")),
            "index": dict(context.get("index") or {}),
            "readiness": dict(context.get("readiness") or {}),
            "note": str(context.get("note") or ""),
        },
        "graph": graph,
    }
