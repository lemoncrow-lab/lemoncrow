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
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, cast

from lemoncrow.core.service.code_relations import file_category, implausible_relation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lemoncrow.pro.capabilities.review.code_intelligence import CodeIntelligenceProvider

MapLevel = Literal["architecture", "file", "symbol"]

_MAX_SYMBOL_SEEDS = 8
_MAX_SYMBOL_GRAPH_NODES = 80
_MAX_MEMBER_SYMBOLS = 20
# Overview budgets. Changed files are the subject of the review and get a
# generous allowance; surrounding files are split per lane ("used by" vs "uses")
# so one busy direction cannot crowd the other out of the first frame.
_MAX_OVERVIEW_CHANGED = 300
_MAX_OVERVIEW_IMPACTED = 16
_MAX_OVERVIEW_CONTEXT_PER_LANE = 12
_MAX_OVERVIEW_EDGES = 320
# File focus: one file's complete bounded neighborhood.
_MAX_FILE_NEIGHBORS_PER_LANE = 24
_MAX_FILE_EDGES = 160
_MAX_FOCUS_SYMBOLS = 60
_MAX_EDGE_EVIDENCE = 6
_MAX_EDGE_SYMBOLS = 12
_RELATION_FACTOR = {"impacts": 10, "calls": 7, "imports": 5, "references": 1}
_ATTENTION_ORDER = {"high": 3, "normal": 2, "mechanical": 1}

# The plausibility filter is shared with `/code`, so both surfaces drop the
# same name collisions.
_file_category = file_category
_implausible_relation = implausible_relation
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


def _safe_index_path(path: str) -> bool:
    """Current-index paths must be repository-relative, never foreign/absolute."""
    return bool(
        path
        and not PurePosixPath(path).is_absolute()
        and ".." not in PurePosixPath(path).parts
        and "\\" not in path
        and not re.match(r"^[A-Za-z]:", path)
        and not any(char in path for char in ("\x00", "\r", "\n"))
    )


def _component_for_path(path: str) -> str:
    # A file's component is its folder: the Review Map groups by folder, and a
    # partition tuned to one repository's layout means nothing in another.
    parent = PurePosixPath(path).parent.as_posix()
    return "" if parent == "." else parent


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
        counts = getattr(target, "annotation_counts", None)
        priority = getattr(target, "priority", None)
        state = str(getattr(target, "state", "unknown"))
        # The canonical queue decides what is outstanding and where it sits
        # (review_priority); a target without one falls back on its state.
        outstanding = (
            bool(priority.outstanding)
            if priority is not None
            else state != "reviewed" or bool(getattr(target, "changed_since_mark", False))
        )
        rows.append(
            {
                "target_id": str(getattr(target, "target_id", "")),
                "path": str(getattr(target, "path", "")),
                "start_line": int(getattr(target, "start_line", 0) or 0),
                "end_line": int(getattr(target, "end_line", 0) or 0),
                "symbol": str(getattr(target, "symbol", "")),
                "state": str(getattr(target, "state", "unknown")),
                "changed_since_mark": bool(getattr(target, "changed_since_mark", False)),
                "attention_level": str(getattr(target, "attention_level", "") or ""),
                "open_threads": int(getattr(counts, "open", 0) or 0) if counts is not None else 0,
                "outstanding": outstanding,
                "queue_position": int(priority.position) if priority is not None and outstanding else -1,
            }
        )
    return rows


def _order_rows(packet: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in _mapping_rows(packet.get("order")):
        path = str(row.get("path") or "")
        if not path or path in out:
            continue
        out[path] = {
            "rank": _integer(row.get("rank"), 0),
            "reasons": [str(reason) for reason in (row.get("reasons") or ()) if str(reason)][:6],
        }
    return out


def _review_by_path(targets: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate the reviewer's own progress per file for the map's triage cues."""

    out: dict[str, dict[str, Any]] = {}
    for target in targets:
        path = str(target.get("path") or "")
        if not path:
            continue
        row = out.setdefault(
            path,
            {
                "targets": 0,
                "reviewed": 0,
                "needs_changes": 0,
                "changed_since_review": 0,
                "open_threads": 0,
                "attention": "",
                "outstanding": 0,
                "next_position": -1,
            },
        )
        state = str(target.get("state") or "")
        changed = bool(target.get("changed_since_mark"))
        row["targets"] += 1
        if state == "reviewed" and not changed:
            row["reviewed"] += 1
        if state == "needs_changes":
            row["needs_changes"] += 1
        if changed:
            row["changed_since_review"] += 1
        row["open_threads"] += int(target.get("open_threads") or 0)
        attention = str(target.get("attention_level") or "")
        if _ATTENTION_ORDER.get(attention, 0) > _ATTENTION_ORDER.get(str(row["attention"]), 0):
            row["attention"] = attention
        if target.get("outstanding"):
            row["outstanding"] += 1
            position = int(target.get("queue_position", -1))
            if position >= 0 and (row["next_position"] < 0 or position < row["next_position"]):
                row["next_position"] = position
    return out


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


def _overview_seed_paths(packet: Mapping[str, Any]) -> tuple[str, ...]:
    """The Snapshot files the overview shows, in the review rank it shows them by.

    The overview keeps the top ``_MAX_OVERVIEW_CHANGED`` changed files by review
    rank; links fetched for any other slice leave shown files without links.
    """

    order = _order_rows(packet)
    public_paths = _public_contract_paths(packet)
    paths = dict.fromkeys(str(item.get("path") or "") for item in _snapshot_files(packet))
    paths.pop("", None)

    def rank_key(path: str) -> tuple[int, int, str]:
        rank = int((order.get(path) or {}).get("rank") or 0)
        return (rank if rank > 0 else 10**6, 0 if path in public_paths else 1, path)

    return tuple(sorted(paths, key=rank_key))[:_MAX_OVERVIEW_CHANGED]


def _current_context(
    *,
    provider: CodeIntelligenceProvider | None,
    packet: Mapping[str, Any],
    snapshot_head_sha: str,
    selected_node_id: str = "",
    focus_only: bool = False,
    include_references: bool = False,
    relationship_paths: tuple[str, ...] | None = None,
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
        "file_relationships": {},
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

        file_relationships: dict[str, Any] = {}
        file_relationships_error = ""
        file_relationships_attempted = False
        if not focus_only:
            changed_paths = relationship_paths if relationship_paths is not None else _overview_seed_paths(packet)
            file_relationships_fn = getattr(provider, "file_relationships", None)
            if changed_paths and callable(file_relationships_fn):
                file_relationships_attempted = True
                try:
                    raw_relationships = file_relationships_fn(
                        changed_paths, limit=240 if relationship_paths is not None else 320
                    )
                except Exception as exc:
                    raw_relationships = None
                    file_relationships_error = type(exc).__name__
                if isinstance(raw_relationships, Mapping):
                    file_relationships = dict(raw_relationships)
                    if raw_relationships.get("degraded"):
                        file_relationships_error = str(raw_relationships.get("degraded_reason") or "degraded")

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
        # Architecture/file overview already has a direct bounded graph query.
        # Do not immediately follow it with N symbol-neighborhood queries: the
        # native relation query is symbol-oriented and may materialize the full
        # graph for each seed. Symbol identity/context is fetched lazily when the
        # reviewer drills into one changed symbol.
        if not focus_only and (file_relationships_attempted or relationship_paths is not None):
            symbols = []
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
                # The Snapshot's body span lets the provider attribute outgoing
                # calls to this symbol; it decides whether its View still
                # matches the revision that span came from.
                neighborhood = provider.neighborhood(
                    {
                        **match_row,
                        "span_start": int(symbol.get("start_line") or 0),
                        "span_end": int(symbol.get("end_line") or 0),
                    },
                    depth=1,
                    limit=40,
                )
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
        relationship_note = (
            f" File relationships are degraded ({file_relationships_error})." if file_relationships_error else ""
        )
        base.update(
            {
                "source": "current_index",
                "exact_for_snapshot": False,
                "note": (
                    "Surrounding calls/references come from the current code-intelligence provider, "
                    "not the immutable Review Snapshot." + graph_note + relationship_note
                ),
                "matches": matches,
                "neighborhoods": neighborhoods,
                "file_relationships": file_relationships,
                "references": references,
            }
        )
        return base
    except Exception as exc:
        base["note"] = (
            f"Current code intelligence is unavailable ({type(exc).__name__}); " "Snapshot-exact facts are still shown."
        )
        return base


def _symbol_node_id(symbol: Mapping[str, Any]) -> str:
    return _stable_id(
        "symbol",
        symbol.get("file_path") or "",
        symbol.get("qualified_name") or symbol.get("symbol_name") or "",
        symbol.get("start_line") or 0,
        symbol.get("change") or "unknown",
    )


def _normalized_relation_kind(value: object) -> str:
    raw = str(value or "references")
    return {"call": "calls", "reference": "references", "import": "imports"}.get(raw, raw)


def _relation_score(edge: Mapping[str, Any]) -> tuple[int, int]:
    """``(strong, weak)`` contribution of one aggregated edge.

    Impact/call/import edges decide what earns scarce map space. Name-resolved
    references are kept, but only as a tie-breaker: counting them equally lets
    broad lexical matches swamp the architecture.
    """

    kind = str(edge.get("kind") or "")
    weight = min(6, max(1, int(edge.get("weight") or 1)))
    if kind == "references":
        return 0, weight
    return _RELATION_FACTOR.get(kind, 1) * weight, 0


class _FileGraph:
    """Snapshot file facts plus bounded current-index file relationships.

    Shared by the change overview and the single-file focus so both levels agree
    on identity, status, exactness, and which relationships are plausible.
    Snapshot facts are loaded first and are never downgraded by current-index
    context.
    """

    def __init__(
        self,
        *,
        review_id: str,
        revision_id: str,
        packet: Mapping[str, Any],
        targets: Sequence[Mapping[str, Any]],
    ) -> None:
        self.review_id = review_id
        self.revision_id = revision_id
        self.packet = packet
        self.targets = targets
        self.public_paths = _public_contract_paths(packet)
        self.attributions = _file_attributions(packet)
        self.fallback_provenance = _global_provenance(packet)
        self.order = _order_rows(packet)
        self.review = _review_by_path(targets)
        self.files: dict[str, dict[str, Any]] = {}
        self.edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.file_status: dict[str, str] = {}
        self.categories: dict[str, str] = {}
        self.filtered: Counter[str] = Counter()
        self._load_snapshot()

    @staticmethod
    def _label(path: str, component: str) -> str:
        prefix = component.rstrip("/") + "/"
        relative = path[len(prefix) :] if path.startswith(prefix) else Path(path).name
        return relative or Path(path).name or path

    def ensure_file(self, path: str) -> dict[str, Any]:
        row = self.files.get(path)
        if row is None:
            component = _component_for_path(path)
            category = self.categories.get(path) or _file_category(path)
            self.categories[path] = category
            row = {
                "id": _stable_id("file", path),
                "label": self._label(path, component),
                "kind": "file",
                "component": component,
                "path": path,
                "line": 0,
                "end_line": 0,
                "exactness": "current_index",
                "status": "context",
                "change_kinds": set(),
                "change": "",
                "old_path": "",
                "additions": 0,
                "deletions": 0,
                "category": category,
                "language": "",
                "important_public_contract": False,
                "boundaries": set(),
                "uncertain": False,
                "uncertainty": "",
                "symbols": [],
                "target_ids": set(),
                "current_symbol_ids": set(),
                "provenance": {},
            }
            self.files[path] = row
        return row

    def _load_snapshot(self) -> None:
        targets = self.targets
        for item in _snapshot_files(self.packet):
            path = str(item.get("path") or "")
            if not path:
                continue
            category = str(item.get("category") or "")
            if category:
                self.categories[path] = category
            change = str(item.get("status") or "modified")
            self.file_status[path] = change
            row = self.ensure_file(path)
            row["exactness"] = "snapshot"
            row["status"] = "new" if change == "added" else "changed"
            row["change_kinds"].add(change)
            row["change"] = change
            row["old_path"] = str(item.get("old_path") or "")
            row["additions"] = max(0, _integer(item.get("additions"), 0))
            row["deletions"] = max(0, _integer(item.get("deletions"), 0))
            row["language"] = str(item.get("language") or "")
            row["important_public_contract"] = path in self.public_paths
            row["boundaries"].update(_boundary_for_path(path))
            row["provenance"] = self.attributions.get(path) or self.fallback_provenance
            row["target_ids"].update(_targets_for_span(targets, path, 0, 0))

        changed_symbols = sorted(
            _snapshot_symbols(self.packet),
            key=lambda item: (
                0 if str(item.get("file_path") or "") in self.public_paths else 1,
                int(item.get("centrality_rank") or 10**9),
                -_integer(item.get("caller_count")),
                str(item.get("file_path") or ""),
                int(item.get("start_line") or 0),
            ),
        )
        for symbol in changed_symbols:
            path = str(symbol.get("file_path") or "")
            if not path:
                continue
            row = self.ensure_file(path)
            row["exactness"] = "snapshot"
            if row["status"] == "context":
                change = str(symbol.get("change") or "modified")
                row["status"] = "new" if change == "added" else "changed"
                row["change_kinds"].add(change)
                row["change"] = row["change"] or change
            start = int(symbol.get("start_line") or 0)
            end = int(symbol.get("end_line") or start)
            row["symbols"].append(
                {
                    "id": _symbol_node_id(symbol),
                    "label": str(symbol.get("symbol_name") or symbol.get("qualified_name") or path),
                    "qualified_name": str(symbol.get("qualified_name") or ""),
                    "kind": str(symbol.get("kind") or ""),
                    "path": path,
                    "line": start,
                    "end_line": end,
                    "change": str(symbol.get("change") or "unknown"),
                    "caller_count": _integer(symbol.get("caller_count")),
                    "usage_count": _integer(symbol.get("usage_count")),
                    "centrality_rank": symbol.get("centrality_rank"),
                    "centrality_percentile": symbol.get("centrality_percentile"),
                    "target_ids": _targets_for_span(targets, path, start, end),
                }
            )
            row["important_public_contract"] = bool(row["important_public_contract"] or path in self.public_paths)
            row["boundaries"].update(_boundary_for_path(path))
            row["target_ids"].update(_targets_for_span(targets, path, start, end))

        # Frozen impact facts are the strongest relationships on the map.
        for impact in _snapshot_impacts(self.packet):
            source_path = str(impact.get("source_path") or "")
            site_path, line, impact_end = _site_location(str(impact.get("path") or ""))
            if not site_path:
                continue
            uncertainty = str(impact.get("uncertainty") or "")
            target = self.ensure_file(site_path)
            if site_path not in self.file_status:
                target["exactness"] = "snapshot"
                target["status"] = "impacted"
                target["line"] = target["line"] or line
                target["end_line"] = target["end_line"] or impact_end
                target["boundaries"].update(_boundary_for_path(site_path))
                target["uncertain"] = bool(target["uncertain"] or uncertainty)
                target["uncertainty"] = target["uncertainty"] or uncertainty
            if not source_path:
                continue
            edge = self.ensure_edge(source_path, site_path, "impacts", exactness="snapshot")
            if edge is None:
                continue
            edge["weight"] += 1
            edge["uncertain"] = bool(edge["uncertain"] or uncertainty)
            if len(edge["evidence"]) < _MAX_EDGE_EVIDENCE:
                edge["evidence"].append(
                    {
                        "kind": str(impact.get("kind") or "impact"),
                        "path": site_path,
                        "line": line,
                        "symbol": str(impact.get("old") or ""),
                        "snippet": str(impact.get("snippet") or "")[:120],
                        "uncertainty": uncertainty,
                    }
                )
            symbol_name = str(impact.get("old") or "")
            if symbol_name:
                edge["_symbols"][symbol_name] += 1

    def ensure_edge(
        self,
        source_path: str,
        target_path: str,
        kind: str,
        *,
        exactness: str,
    ) -> dict[str, Any] | None:
        if not source_path or not target_path or source_path == target_path:
            return None
        source = self.ensure_file(source_path)
        target = self.ensure_file(target_path)
        key = (source_path, target_path, kind)
        edge = self.edges.get(key)
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
                "_symbols": Counter(),
            }
            self.edges[key] = edge
        elif edge["exactness"] != "snapshot" and exactness == "snapshot":
            edge["exactness"] = "snapshot"
        return edge

    def add_relationships(self, rows: Sequence[Mapping[str, Any]]) -> None:
        for relation in rows:
            source_path = str(relation.get("source_path") or "")
            target_path = str(relation.get("target_path") or "")
            kind = _normalized_relation_kind(relation.get("kind"))
            # Only a frozen ReviewPacket impact finding may assert an `impacts`
            # edge. The mutable index supplies navigation relationships, never
            # Snapshot evidence; accepting its arbitrary relation names would
            # both invent impact edges and inflate an existing exact edge.
            if kind not in {"calls", "imports", "references"}:
                self.filtered["untrusted_relation_kind"] += 1
                continue
            if not source_path or not target_path or source_path == target_path:
                continue
            if not _safe_index_path(source_path) or not _safe_index_path(target_path):
                self.filtered["unsafe_index_path"] += 1
                continue
            reason = _implausible_relation(source_path, target_path, kind, self.categories)
            if reason:
                self.filtered[reason] += 1
                continue
            edge = self.ensure_edge(source_path, target_path, kind, exactness="current_index")
            if edge is None:
                continue
            edge["weight"] += max(1, _integer(relation.get("weight"), 1))
            for evidence in _mapping_rows(relation.get("evidence")):
                evidence_path = str(evidence.get("path") or source_path)
                if not _safe_index_path(evidence_path):
                    continue
                symbol = str(evidence.get("symbol") or "")
                row = {
                    "kind": kind,
                    "path": evidence_path,
                    "line": _integer(evidence.get("line"), 0),
                    "symbol": symbol,
                    "snippet": "",
                    "uncertainty": "",
                }
                if len(edge["evidence"]) < _MAX_EDGE_EVIDENCE and row not in edge["evidence"]:
                    edge["evidence"].append(row)
            named = _mapping_rows(relation.get("symbols"))
            if named:
                for item in named:
                    name = str(item.get("name") or "")
                    if name:
                        edge["_symbols"][name] += max(1, _integer(item.get("count"), 1))
            else:
                for evidence in _mapping_rows(relation.get("evidence")):
                    name = str(evidence.get("symbol") or "")
                    if name:
                        edge["_symbols"][name] += 1

    def add_neighborhoods(self, context: Mapping[str, Any], *, relation_fallback: bool) -> None:
        """Bind changed symbols to native ids; optionally derive file edges.

        Symbol neighborhoods are the relation source only for providers that do
        not expose the file-level seam.
        """

        matches_raw = context.get("matches")
        matches: Mapping[str, Any] = matches_raw if isinstance(matches_raw, Mapping) else {}
        neighborhoods_raw = context.get("neighborhoods")
        neighborhoods: Mapping[str, Any] = neighborhoods_raw if isinstance(neighborhoods_raw, Mapping) else {}
        for snapshot_id, match in matches.items():
            if not isinstance(match, Mapping):
                continue
            matched_path = str(match.get("path") or "")
            current_id = str(match.get("id") or "")
            if matched_path and current_id and _safe_index_path(matched_path):
                self.ensure_file(matched_path)["current_symbol_ids"].add(current_id)
            neighborhood = neighborhoods.get(snapshot_id)
            if not isinstance(neighborhood, Mapping):
                continue
            current_paths: dict[str, str] = {}
            for node in _mapping_rows(neighborhood.get("nodes")):
                node_id = str(node.get("id") or "")
                node_path = str(node.get("path") or "")
                if not node_id or not _safe_index_path(node_path):
                    continue
                current_paths[node_id] = node_path
                if node_path in self.files or relation_fallback:
                    row = self.ensure_file(node_path)
                    row["current_symbol_ids"].add(node_id)
                    if row["status"] == "context":
                        row["line"] = row["line"] or int(node.get("line") or 0)
                        row["end_line"] = row["end_line"] or int(node.get("end_line") or node.get("line") or 0)
            if not relation_fallback:
                continue
            for current_edge in _mapping_rows(neighborhood.get("edges")):
                self.add_relationships(
                    [
                        {
                            "source_path": current_paths.get(str(current_edge.get("source") or ""), ""),
                            "target_path": current_paths.get(str(current_edge.get("target") or ""), ""),
                            "kind": current_edge.get("kind"),
                            "weight": current_edge.get("weight") or 1,
                        }
                    ]
                )

    def lane_scores(self, anchors: set[str]) -> dict[str, dict[str, list[int]]]:
        """Score every non-anchor file by direction relative to *anchors*.

        ``used_by``: the file depends on (or is impacted by) an anchor.
        ``uses``: an anchor depends on the file. Scores are ``[strong, weak]``.
        """

        scores: dict[str, dict[str, list[int]]] = {}

        def bump(path: str, lane: str, edge: Mapping[str, Any]) -> None:
            strong, weak = _relation_score(edge)
            row = scores.setdefault(path, {"used_by": [0, 0], "uses": [0, 0]})
            row[lane][0] += strong
            row[lane][1] += weak

        for (source_path, target_path, kind), edge in self.edges.items():
            source_in = source_path in anchors
            target_in = target_path in anchors
            if source_in == target_in:
                continue
            if kind == "impacts":
                # source changed; target is a site that uses the changed code.
                if source_in:
                    bump(target_path, "used_by", edge)
                else:
                    bump(source_path, "uses", edge)
            elif target_in:
                bump(source_path, "used_by", edge)
            else:
                bump(target_path, "uses", edge)
        return scores

    def degree(self) -> Counter[str]:
        degree: Counter[str] = Counter()
        for edge in self.edges.values():
            strong, weak = _relation_score(edge)
            degree[str(edge["source"])] += strong + weak
            degree[str(edge["target"])] += strong + weak
        return degree

    def node_payload(
        self,
        row: Mapping[str, Any],
        *,
        level: MapLevel,
        focus_node_id: str = "",
        lane: str = "",
        relationship_count: int = 0,
        symbol_limit: int = _MAX_MEMBER_SYMBOLS,
    ) -> dict[str, Any]:
        path = str(row["path"] or "")
        symbols = list(row["symbols"])
        target_ids = sorted(row["target_ids"])
        current_symbol_ids = sorted(item for item in row["current_symbol_ids"] if item)
        order = self.order.get(path) or {}
        review = self.review.get(path)
        change_kinds = sorted(row["change_kinds"])
        return {
            "id": row["id"],
            "label": row["label"],
            "kind": "file",
            "component": row["component"],
            "module": str(PurePosixPath(path).parent) if "/" in path else "",
            "path": path,
            "line": int(row["line"] or 0),
            "end_line": int(row["end_line"] or row["line"] or 0),
            "status": row["status"],
            "exactness": row["exactness"],
            "lane": lane,
            "change": row["change"],
            "old_path": row["old_path"],
            "additions": int(row["additions"] or 0),
            "deletions": int(row["deletions"] or 0),
            "category": row["category"],
            "language": row["language"],
            "rank": int(order.get("rank") or 0),
            "reasons": list(order.get("reasons") or []),
            "review": dict(review) if review else None,
            # Retain the old aggregate fields for compatible inspectors/clients.
            "changed_file_count": 1 if row["status"] in {"new", "changed"} else 0,
            "impacted_file_count": 1 if row["status"] == "impacted" else 0,
            "paths": [path],
            "paths_truncated": False,
            "symbols": symbols[:symbol_limit],
            "symbols_truncated": len(symbols) > symbol_limit,
            "symbol_count": len(symbols),
            "target_ids": target_ids,
            "current_symbol_ids": current_symbol_ids[:_MAX_SYMBOL_SEEDS],
            "change_kinds": change_kinds,
            "important_public_contract": bool(row["important_public_contract"]),
            "boundaries": sorted(row["boundaries"]),
            "boundary_basis": "path" if row["boundaries"] else "",
            "uncertain": bool(row["uncertain"]),
            "uncertainty": str(row["uncertainty"] or ""),
            "provenance": row["provenance"],
            "relationship_count": relationship_count,
            "selection": _selection(
                review_id=self.review_id,
                revision_id=self.revision_id,
                level=level,
                node_id=str(row["id"]),
                exactness=str(row["exactness"]),
                focus_node_id=focus_node_id,
                side="old" if change_kinds and set(change_kinds).issubset({"deleted", "removed"}) else "new",
                path=path,
                line=int(row["line"] or 0),
                end_line=int(row["end_line"] or row["line"] or 0),
                target_ids=target_ids,
                current_symbol_id=current_symbol_ids[0] if current_symbol_ids else "",
            ),
        }

    def edge_payload(self, edge: Mapping[str, Any], *, level: MapLevel, focus_node_id: str = "") -> dict[str, Any]:
        symbols: Counter[str] = edge["_symbols"]
        payload = {key: value for key, value in edge.items() if not key.startswith("_")}
        payload["evidence"] = list(edge["evidence"])
        payload["symbols"] = [
            {"name": name, "count": count}
            for name, count in sorted(symbols.items(), key=lambda item: (-item[1], item[0]))[:_MAX_EDGE_SYMBOLS]
        ]
        payload["selection"] = _edge_selection(
            self.review_id, self.revision_id, payload, level=level, focus_node_id=focus_node_id
        )
        return payload


def _edge_sort_key(edge: Mapping[str, Any]) -> tuple[int, int, int, str, str]:
    order = {"impacts": 0, "calls": 1, "imports": 2, "references": 3}
    return (
        0 if edge.get("exactness") == "snapshot" else 1,
        order.get(str(edge.get("kind") or ""), 4),
        -int(edge.get("weight") or 0),
        str(edge.get("source") or ""),
        str(edge.get("target") or ""),
    )


def _pick_lanes(
    candidates: Sequence[Mapping[str, Any]],
    scores: Mapping[str, Mapping[str, list[int]]],
    per_lane: int,
    *,
    changed_first: bool = False,
) -> tuple[list[tuple[Mapping[str, Any], str]], int]:
    """Choose surrounding files for each lane; return picks and candidate count.

    A file with no relationship to the anchors is not a candidate at all -- the
    old overview counted every file the index touched as \"omitted\", which made
    a focused change look like a tiny slice of a huge graph.
    """

    pools: dict[str, list[tuple[tuple[Any, ...], Mapping[str, Any]]]] = {"used_by": [], "uses": []}
    for row in candidates:
        path = str(row.get("path") or "")
        score = scores.get(path)
        if score is None:
            continue
        used_by = score["used_by"]
        uses = score["uses"]
        if not any(used_by) and not any(uses):
            continue
        if str(row.get("status") or "") == "impacted":
            lane = "used_by"
        else:
            lane = "used_by" if (used_by[0], used_by[1]) >= (uses[0], uses[1]) else "uses"
        strong, weak = score[lane]
        priority = 0 if changed_first and str(row.get("status") or "") in {"new", "changed"} else 1
        pools[lane].append(((priority, -strong, -weak, path), row))
    for pool in pools.values():
        pool.sort(key=lambda item: item[0])
    candidate_count = len(pools["used_by"]) + len(pools["uses"])
    take = {lane: min(per_lane, len(pool)) for lane, pool in pools.items()}
    # Lend unused capacity from a sparse lane to a busy one.
    spare = sum(per_lane - value for value in take.values())
    for lane, pool in sorted(pools.items(), key=lambda item: -len(item[1])):
        extra = min(spare, len(pool) - take[lane])
        take[lane] += extra
        spare -= extra
    picks: list[tuple[Mapping[str, Any], str]] = []
    for lane, pool in pools.items():
        picks.extend((row, lane) for _key, row in pool[: take[lane]])
    return picks, candidate_count


def _architecture_projection(
    *,
    review_id: str,
    revision_id: str,
    packet: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a review as changed files plus their meaningful surroundings.

    Changed files are the subject and always come first. Surrounding files are
    chosen by relationship strength per direction (who uses the change, what the
    change uses), never by directory proximity. Edges that do not touch a
    changed file are dropped: they do not explain the change.
    """

    graph = _FileGraph(review_id=review_id, revision_id=revision_id, packet=packet, targets=targets)
    file_relationships_raw = context.get("file_relationships")
    file_relationships: Mapping[str, Any] = (
        file_relationships_raw if isinstance(file_relationships_raw, Mapping) else {}
    )
    relationship_rows = _mapping_rows(file_relationships.get("edges"))
    graph.add_relationships(relationship_rows)
    graph.add_neighborhoods(context, relation_fallback=not relationship_rows)

    degree = graph.degree()
    changed_rows = [row for row in graph.files.values() if row["status"] in {"new", "changed"}]
    anchors = {str(row["path"]) for row in changed_rows}

    def changed_key(row: Mapping[str, Any]) -> tuple[int, int, int, str]:
        rank = int((graph.order.get(str(row["path"])) or {}).get("rank") or 0)
        return (
            rank if rank > 0 else 10**6,
            0 if row.get("important_public_contract") else 1,
            -degree[str(row["id"])],
            str(row["path"]),
        )

    changed_rows.sort(key=changed_key)
    selected: list[tuple[Mapping[str, Any], str]] = [(row, "") for row in changed_rows[:_MAX_OVERVIEW_CHANGED]]

    scores = graph.lane_scores(anchors)
    impacted_rows = sorted(
        (row for row in graph.files.values() if row["status"] == "impacted"),
        key=lambda row: (
            0 if row.get("uncertain") is False else 1,
            -sum((scores.get(str(row["path"])) or {"used_by": [0, 0]})["used_by"]),
            str(row["path"]),
        ),
    )
    selected.extend((row, "used_by") for row in impacted_rows[:_MAX_OVERVIEW_IMPACTED])
    context_rows = [row for row in graph.files.values() if row["status"] == "context"]
    context_picks, context_candidates = _pick_lanes(context_rows, scores, _MAX_OVERVIEW_CONTEXT_PER_LANE)
    selected.extend(context_picks)

    selected_paths = {str(row["path"]) for row, _lane in selected}
    anchor_edges = [
        edge
        for (source_path, target_path, _kind), edge in graph.edges.items()
        if (source_path in anchors or target_path in anchors)
    ]
    internal_edges: list[dict[str, Any]] = []
    crossing_edges: list[dict[str, Any]] = []
    for (source_path, target_path, _kind), edge in graph.edges.items():
        if source_path not in selected_paths or target_path not in selected_paths:
            continue
        if source_path in anchors and target_path in anchors:
            internal_edges.append(edge)
        elif source_path in anchors or target_path in anchors:
            crossing_edges.append(edge)
    internal_edges.sort(key=_edge_sort_key)
    crossing_edges.sort(key=_edge_sort_key)
    # Links among changed files and links out to surrounding code answer
    # different questions; a large change must not spend the whole budget on
    # the first, so each side gets half and lends what it does not use.
    internal_share = min(len(internal_edges), max(_MAX_OVERVIEW_EDGES // 2, _MAX_OVERVIEW_EDGES - len(crossing_edges)))
    shown_edges = internal_edges[:internal_share] + crossing_edges[: _MAX_OVERVIEW_EDGES - internal_share]
    shown_edges.sort(key=_edge_sort_key)

    nodes = [
        graph.node_payload(row, level="architecture", lane=lane, relationship_count=degree[str(row["id"])])
        for row, lane in selected
    ]
    edges = [graph.edge_payload(edge, level="architecture") for edge in shown_edges]
    changed_total = len(changed_rows)
    omitted = (
        max(0, changed_total - _MAX_OVERVIEW_CHANGED)
        + max(0, len(impacted_rows) - _MAX_OVERVIEW_IMPACTED)
        + max(0, context_candidates - len(context_picks))
    )
    return {
        "level": "architecture",
        "focus": next((node["id"] for node in nodes if node["status"] in {"new", "changed"}), None),
        "nodes": nodes,
        "edges": edges,
        "truncated": omitted > 0 or len(anchor_edges) > len(shown_edges),
        "omitted_node_count": omitted,
        "stats": {
            "changed_files": changed_total,
            "new_files": sum(1 for row in changed_rows if row["status"] == "new"),
            "deleted_files": sum(1 for row in changed_rows if row["change"] in {"deleted", "removed"}),
            "impacted_files": len(impacted_rows),
            "context_files": context_candidates,
            "shown_changed": min(changed_total, _MAX_OVERVIEW_CHANGED),
            "shown_impacted": min(len(impacted_rows), _MAX_OVERVIEW_IMPACTED),
            "shown_context": len(context_picks),
            "relationships": len(anchor_edges),
            "shown_relationships": len(shown_edges),
            "filtered_relationships": sum(graph.filtered.values()),
            "filtered_reasons": dict(sorted(graph.filtered.items())),
        },
    }


def _snapshot_file_paths(packet: Mapping[str, Any]) -> list[str]:
    paths: dict[str, None] = {}
    for item in _snapshot_files(packet):
        path = str(item.get("path") or "")
        if path:
            paths[path] = None
    for symbol in _snapshot_symbols(packet):
        path = str(symbol.get("file_path") or "")
        if path:
            paths[path] = None
    for impact in _snapshot_impacts(packet):
        path, _line, _end = _site_location(str(impact.get("path") or ""))
        if path:
            paths[path] = None
    return list(paths)


def _resolve_snapshot_file(packet: Mapping[str, Any], node_id: str) -> str:
    return next((path for path in _snapshot_file_paths(packet) if _stable_id("file", path) == node_id), "")


def _file_projection(
    *,
    review_id: str,
    revision_id: str,
    packet: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
    focus_path: str,
) -> dict[str, Any]:
    """One Snapshot file with its complete bounded neighborhood.

    The overview spends its budget across the whole change; the file focus
    spends it on one file so a reviewer can see *every* file that uses it or
    that it uses, and which of its symbols each relationship lands on.
    """

    if not focus_path:
        return {
            "level": "file",
            "focus": None,
            "nodes": [],
            "edges": [],
            "truncated": False,
            "reason": "The selected map file is not part of this immutable Snapshot.",
        }
    graph = _FileGraph(review_id=review_id, revision_id=revision_id, packet=packet, targets=targets)
    file_relationships_raw = context.get("file_relationships")
    file_relationships: Mapping[str, Any] = (
        file_relationships_raw if isinstance(file_relationships_raw, Mapping) else {}
    )
    graph.add_relationships(
        [
            row
            for row in _mapping_rows(file_relationships.get("edges"))
            if focus_path in {str(row.get("source_path") or ""), str(row.get("target_path") or "")}
        ]
    )
    focus_row = graph.ensure_file(focus_path)
    focus_id = str(focus_row["id"])
    anchors = {focus_path}
    scores = graph.lane_scores(anchors)
    neighbors = [row for path, row in graph.files.items() if path != focus_path]
    picks, candidate_count = _pick_lanes(neighbors, scores, _MAX_FILE_NEIGHBORS_PER_LANE, changed_first=True)
    selected_paths = {focus_path, *(str(row["path"]) for row, _lane in picks)}
    focus_edges = [
        edge
        for (source_path, target_path, _kind), edge in graph.edges.items()
        if focus_path in {source_path, target_path}
    ]
    edge_rows = [
        edge
        for (source_path, target_path, _kind), edge in graph.edges.items()
        if focus_path in {source_path, target_path} and source_path in selected_paths and target_path in selected_paths
    ]
    edge_rows.sort(key=_edge_sort_key)
    shown_edges = edge_rows[:_MAX_FILE_EDGES]
    degree = graph.degree()
    nodes = [
        graph.node_payload(
            focus_row,
            level="file",
            focus_node_id=focus_id,
            relationship_count=degree[focus_id],
            symbol_limit=_MAX_FOCUS_SYMBOLS,
        )
    ]
    nodes.extend(
        graph.node_payload(
            row,
            level="file",
            focus_node_id=focus_id,
            lane=lane,
            relationship_count=degree[str(row["id"])],
        )
        for row, lane in picks
    )
    edges = [graph.edge_payload(edge, level="file", focus_node_id=focus_id) for edge in shown_edges]
    omitted = max(0, candidate_count - len(picks))
    return {
        "level": "file",
        "focus": focus_id,
        "nodes": nodes,
        "edges": edges,
        "truncated": omitted > 0 or len(focus_edges) > len(shown_edges) or bool(file_relationships.get("truncated")),
        "omitted_node_count": omitted,
        "stats": {
            "relationships": len(focus_edges),
            "shown_relationships": len(shown_edges),
            "context_files": candidate_count,
            "shown_context": len(picks),
            "filtered_relationships": sum(graph.filtered.values()),
            "filtered_reasons": dict(sorted(graph.filtered.items())),
        },
        "reason": "",
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
    filtered_relationships = 0
    focus_label = str(selected.get("symbol_name") or "")
    categories = {
        str(item.get("path") or ""): str(item.get("category") or "")
        for item in _snapshot_files(packet)
        if str(item.get("path") or "") and str(item.get("category") or "")
    }
    neighborhood = neighborhoods.get(selected_node_id) if isinstance(neighborhoods, Mapping) else None
    if isinstance(neighborhood, Mapping):
        # A native callee says so in its kind; the legacy index gives every node
        # its symbol kind, so an edge out of the focus is what marks a callee.
        callee_ids = {
            str(edge.get("target") or "")
            for edge in _mapping_rows(neighborhood.get("edges"))
            if current_symbol_id and str(edge.get("source") or "") == current_symbol_id
        }
        remap: dict[str, str] = {}
        for node in _mapping_rows(neighborhood.get("nodes")):
            raw_id = str(node.get("id") or "")
            if not raw_id:
                continue
            if raw_id == current_symbol_id:
                remap[raw_id] = selected_node_id
                continue
            node_path = str(node.get("path") or "")
            node_kind = str(node.get("kind") or "symbol")
            outgoing = node_kind == "callee" or raw_id in callee_ids
            if node_path and (
                _implausible_relation(path, node_path, "calls", categories)
                if outgoing
                else _implausible_relation(node_path, path, "calls", categories)
            ):
                filtered_relationships += 1
                continue
            mapped_id = f"current:{raw_id}"
            remap[raw_id] = mapped_id
            if len(nodes) >= _MAX_SYMBOL_GRAPH_NODES:
                context_truncated = True
                continue
            raw_label = str(node.get("label") or node.get("qualified_name") or "symbol")
            node_line = int(node.get("line") or 0)
            if node_kind in {"caller", "reference"} or (raw_label == focus_label and not outgoing):
                # The native edge names the symbol it lands on, not the caller's
                # own span; label the site by location instead of repeating the
                # focused symbol's name on every caller.
                raw_label = f"{Path(node_path).name}:{node_line}" if node_line else Path(node_path).name
            nodes[mapped_id] = {
                "id": mapped_id,
                "label": raw_label,
                "qualified_name": str(node.get("qualified_name") or ""),
                "kind": node_kind,
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
            if _implausible_relation(reference_path, path, "references", categories):
                filtered_relationships += 1
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
        "stats": {
            "filtered_relationships": filtered_relationships,
            "outgoing_attributed": bool(
                isinstance(neighborhood, Mapping) and neighborhood.get("outgoing_attributed") is True
            ),
        },
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
    focus_path = _resolve_snapshot_file(packet_data, selected_node_id) if level == "file" else ""
    context = _current_context(
        provider=provider,
        packet=packet_data,
        snapshot_head_sha=str(getattr(revision, "head_sha", "")),
        selected_node_id=selected_node_id if level == "symbol" else "",
        focus_only=level == "symbol",
        include_references=level == "symbol",
        relationship_paths=((focus_path,) if focus_path else ()) if level == "file" else None,
    )
    if level == "file":
        graph = _file_projection(
            review_id=review_id,
            revision_id=revision_id,
            packet=packet_data,
            targets=target_rows,
            context=context,
            focus_path=focus_path,
        )
    elif level == "symbol":
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
