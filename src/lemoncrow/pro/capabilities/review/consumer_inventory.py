"""Known callers/consumers per changed target, and the readiness of the pass that found them.

A read-only projection over the Snapshot's own packet: the impact sites and
changed symbols the capture already recorded (:mod:`impact`), joined to the
Review targets the Brief holds. Nothing here queries an index, writes a store or
calls a model. A site joins one target only when the changed symbol it came from
resolves to exactly one target; otherwise it stays at file level (every target
in that file), and a site no changed file owns stays at Review level. An empty
list with an incomplete pass is "no known consumers", never "no consumers" --
the readiness block says which.

When the capture ran with no code index it never asked who calls the changed
symbols. The caller may then pass the current index's file relationships (the
same provider answer the Review Map draws "Used by" from); their callers join
with ``exactness: "current_index"``. The GitHub Action passes the same shape
from its search of the head commit (``pr_bot.callers``, ``source:
"checkout_search"``), whose ``unmeasured`` symbols readiness carries on. With no
such answer readiness says ``impact_pass: "no_index"``: callers were not
measured, which is not zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .impact import _CALLERS_PER_SYMBOL, _MAX_CALLER_QUERIES

# lc-debt: fixed group/site caps for one Brief payload; page consumers per target if a Reader needs more.
MAX_GROUPS = 400
MAX_SITES_PER_GROUP = 50

#: Current-index relation kinds that are a use of the changed symbol, and the site kind each becomes.
_CURRENT_USES = {"calls": "untouched_caller", "references": "reference"}
#: Impact kinds that say an outside file still uses a changed symbol by name.
_REACH_KINDS = frozenset({"untouched_caller", "removed_symbol", "reference"})


class _Target(Protocol):
    target_id: str
    path: str
    symbol: str
    start_line: int
    end_line: int


def _split_location(location: str) -> tuple[str, int]:
    path, sep, tail = location.rpartition(":L")
    if sep and tail.isdigit():
        return path, int(tail)
    return location, 0


def _names_symbol(target: _Target, name: str) -> bool:
    return bool(name) and (target.symbol == name or target.symbol.endswith(f".{name}"))


def _exact_target(targets: Sequence[_Target], symbol: Mapping[str, Any] | None, name: str) -> _Target | None:
    """The one target this changed symbol is, or ``None`` when that is not provable."""

    named = [target for target in targets if _names_symbol(target, name)]
    if symbol is not None and len(named) > 1:
        line = int(symbol.get("start_line") or 0)
        named = [target for target in named if target.start_line <= line <= target.end_line]
    return named[0] if len(named) == 1 else None


def callers_unmeasured(packet: Mapping[str, Any] | None) -> bool:
    """The impact pass ran, but with no code index, so it never asked who calls the changed symbols."""

    if packet is None:
        return False
    degraded = {str(name) for name in packet.get("degraded") or () if isinstance(name, str)}
    return "impact_disabled" not in degraded and str(packet.get("index_status") or "") not in ("fresh", "stale")


def current_index_answered(relationships: Mapping[str, Any] | None) -> bool:
    """The current index answered: edges came back, or an empty answer it did not mark degraded."""

    if not isinstance(relationships, Mapping):
        return False
    edges = relationships.get("edges")
    if not isinstance(edges, Sequence):
        return False
    return bool(edges) or not relationships.get("degraded")


def readiness(packet: Mapping[str, Any] | None, *, current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Index status and the degradation names the impact pass recorded for this Snapshot.

    ``impact_pass`` is ``ran`` only when callers were measured: by the capture's
    index, or by the current index (*current*) when the capture had none.
    """

    expansion = {"max_symbols": _MAX_CALLER_QUERIES, "callers_per_symbol": _CALLERS_PER_SYMBOL}
    if packet is None:
        return {
            "source": "unavailable",
            "impact_pass": "unavailable",
            "index_status": "unknown",
            "degraded": [],
            "caller_expansion": expansion,
            "sites_recorded": 0,
        }
    degraded = sorted({str(name) for name in packet.get("degraded") or () if isinstance(name, str)})
    status = str(packet.get("index_status") or "unknown")
    out: dict[str, Any] = {
        "source": "snapshot_packet",
        "impact_pass": "not_run" if "impact_disabled" in degraded else "ran",
        "index_status": status if status in ("fresh", "stale", "absent") else "unknown",
        "degraded": degraded,
        "caller_expansion": expansion,
        "sites_recorded": sum(1 for site in packet.get("impact") or () if isinstance(site, Mapping)),
    }
    if not callers_unmeasured(packet):
        return out
    if current is None or not current_index_answered(current):
        out["impact_pass"] = "no_index"
        return out
    # The current index (or the Action's search of the head commit) answered the question the capture could not ask.
    gaps = set(degraded) - {"symbol_relations"}
    if current.get("truncated") and not current.get("unmeasured"):
        # An answer that names the symbols it could not finish (the checkout search) says exactly what is missing.
        gaps.add("caller_queries_truncated")
    source = str(current.get("source") or "current_index")
    out.update({"source": source, "index_status": "current", "degraded": sorted(gaps)})
    # Symbols whose callers the answer could not finish (a bound, an unsupported language): not measured, never none.
    unmeasured = [dict(item) for item in current.get("unmeasured") or () if isinstance(item, Mapping)]
    if unmeasured:
        out["unmeasured"] = unmeasured
    return out


def _relation_names(row: Mapping[str, Any]) -> list[str]:
    """Names a relationship row lands on: a list of ``{name, count}`` (native) or ``{name: count}`` (legacy)."""

    raw = row.get("symbols")
    if isinstance(raw, Mapping):
        return [str(name) for name in raw if name]
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        return [str(item.get("name") or "") for item in raw if isinstance(item, Mapping) and item.get("name")]
    return []


def _current_sites(packet: Mapping[str, Any], relationships: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Current-index uses of changed symbols from files outside the change, as packet-shaped impact rows."""

    changed = {str(item.get("path") or "") for item in packet.get("files") or () if isinstance(item, Mapping)}
    changed.discard("")
    named = {
        (str(symbol.get("file_path") or ""), str(symbol.get("symbol_name") or ""))
        for symbol in packet.get("symbols") or ()
        if isinstance(symbol, Mapping)
    }
    exactness = str(relationships.get("exactness") or "current_index")
    rows: list[dict[str, Any]] = []
    for row in relationships.get("edges") or ():
        if not isinstance(row, Mapping):
            continue
        kind = _CURRENT_USES.get(str(row.get("kind") or ""))
        source = str(row.get("source_path") or "")
        target = str(row.get("target_path") or "")
        if kind is None or not source or source in changed or target not in changed:
            continue
        evidence = [item for item in row.get("evidence") or () if isinstance(item, Mapping)]
        for name in _relation_names(row):
            if (target, name) not in named:
                continue
            lines = sorted(
                {int(item.get("line") or 0) for item in evidence if str(item.get("symbol") or "") == name} or {0}
            )
            rows.extend(
                {
                    "kind": kind,
                    "path": f"{source}:L{line}" if line > 0 else source,
                    "old": name,
                    "snippet": "",
                    "in_patch": False,
                    "source_path": target,
                    "exactness": exactness,
                }
                for line in lines
            )
    return rows


def consumer_inventory(
    packet: Mapping[str, Any] | None,
    targets: Sequence[_Target],
    *,
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``{"readiness", "consumers", "consumers_omitted"}`` for one Snapshot; deterministic for one input.

    *current* is the current index's ``file_relationships`` answer for the changed
    paths; it is read only when the capture had no index (:func:`callers_unmeasured`).
    """

    ready = readiness(packet, current=current)
    if packet is None:
        return {"readiness": ready, "consumers": [], "consumers_omitted": 0}
    answered = ready["source"] not in ("snapshot_packet", "unavailable")
    by_path: dict[str, list[_Target]] = {}
    for target in targets:
        by_path.setdefault(target.path, []).append(target)
    symbols: dict[tuple[str, str], Mapping[str, Any]] = {}
    for symbol in packet.get("symbols") or ():
        if isinstance(symbol, Mapping):
            key = (str(symbol.get("file_path") or ""), str(symbol.get("symbol_name") or ""))
            symbols.setdefault(key, symbol)

    impact: list[Mapping[str, Any]] = [raw for raw in packet.get("impact") or () if isinstance(raw, Mapping)]
    if answered and current is not None:
        impact.extend(_current_sites(packet, current))

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in impact:
        impact_kind = str(raw.get("kind") or "")
        source = str(raw.get("source_path") or "")
        name = str(raw.get("old") or "")
        caller = impact_kind == "untouched_caller"
        key = (source, name if impact_kind in _REACH_KINDS else "")
        group = groups.get(key)
        if group is None:
            symbol = symbols.get((source, name)) if key[1] else None
            in_file = by_path.get(source, []) if source else []
            exact = _exact_target(in_file, symbol, name) if key[1] else None
            raw_count = symbol.get("caller_count") if symbol is not None else None
            caller_count = raw_count if isinstance(raw_count, int) else -1
            group = {
                "key": f"{source}::{key[1]}",
                "path": source,
                "symbol": key[1],
                # How the changed symbol changed (added/modified/deleted); empty when no symbol owns the group.
                "change": str(symbol.get("change") or "") if symbol is not None else "",
                "target_ids": [exact.target_id] if exact else [target.target_id for target in in_file],
                "join": "exact" if exact else "file" if in_file else "review",
                "known_callers": caller_count if caller_count >= 0 else None,
                "sites": [],
                "omitted": 0,
                "unlisted_callers": 0,
            }
            groups[key] = group
        path, line = _split_location(str(raw.get("path") or ""))
        snippet = str(raw.get("snippet") or "")
        group["sites"].append(
            {
                "path": path,
                "line": line,
                "symbol": snippet if caller else "",
                "snippet": "" if caller else snippet,
                "kind": "caller" if caller else "consumer",
                "impact_kind": impact_kind,
                "in_patch": bool(raw.get("in_patch")),
                # Recorded in this Snapshot's packet, or read from the current index when the capture had none.
                "exactness": str(raw.get("exactness") or "snapshot"),
                "uncertainty": str(raw.get("uncertainty") or ""),
                # Whether the authoring agent opened this file; ``None`` when provenance is unknown.
                "inspected_by_agent": (
                    raw.get("inspected_by_agent") if isinstance(raw.get("inspected_by_agent"), bool) else None
                ),
            }
        )

    out: list[dict[str, Any]] = []
    for group in sorted(groups.values(), key=lambda item: (item["path"], item["symbol"])):
        sites = sorted(group["sites"], key=lambda site: (site["in_patch"], site["path"], site["line"], site["symbol"]))
        callers = sum(1 for site in sites if site["kind"] == "caller")
        group["sites"] = sites[:MAX_SITES_PER_GROUP]
        group["omitted"] = max(0, len(sites) - MAX_SITES_PER_GROUP)
        # The index counted more callers than the packet lists: in changed files, dropped as
        # namesakes, or past the expansion caps. Counted, never presented as the whole set.
        group["unlisted_callers"] = max(0, (group["known_callers"] or 0) - callers) if callers else 0
        out.append(group)
    return {"readiness": ready, "consumers": out[:MAX_GROUPS], "consumers_omitted": max(0, len(out) - MAX_GROUPS)}


@dataclass(frozen=True, slots=True)
class OutsideReach:
    """Files outside the change that still use one changed symbol, from :func:`consumer_inventory`."""

    target_id: str
    path: str
    symbol: str
    change: str
    #: Non-test files, most sites first; ``tests`` holds the test files apart.
    files: tuple[str, ...]
    tests: tuple[str, ...]
    #: Every listed use is a call (otherwise some are references or removed-symbol uses).
    calls_only: bool
    #: A group past the per-group bound: ``files`` is a lower bound.
    at_least: bool


def outside_reach(context: Mapping[str, Any] | None) -> dict[str, OutsideReach]:
    """Per exactly-joined target, the outside files that use its changed symbol."""

    from .differential import is_test_path

    out: dict[str, OutsideReach] = {}
    for group in (context or {}).get("consumers") or ():
        if not isinstance(group, Mapping) or group.get("join") != "exact" or not group.get("target_ids"):
            continue
        counts: dict[str, int] = {}
        calls_only = True
        for site in group.get("sites") or ():
            if site.get("in_patch") or site.get("impact_kind") not in _REACH_KINDS or not site.get("path"):
                continue
            counts[str(site["path"])] = counts.get(str(site["path"]), 0) + 1
            calls_only = calls_only and site.get("impact_kind") == "untouched_caller"
        if not counts:
            continue
        ordered = sorted(counts, key=lambda path: (-counts[path], path))
        target_id = str(group["target_ids"][0])
        out[target_id] = OutsideReach(
            target_id=target_id,
            path=str(group.get("path") or ""),
            symbol=str(group.get("symbol") or ""),
            change=str(group.get("change") or ""),
            files=tuple(path for path in ordered if not is_test_path(path)),
            tests=tuple(path for path in ordered if is_test_path(path)),
            calls_only=calls_only,
            at_least=bool(group.get("omitted")),
        )
    return out
