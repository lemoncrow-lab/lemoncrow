"""Known callers/consumers per changed target, and the readiness of the pass that found them.

A read-only projection over the Snapshot's own packet: the impact sites and
changed symbols the capture already recorded (:mod:`impact`), joined to the
Review targets the Brief holds. Nothing here queries an index, writes a store or
calls a model. A site joins one target only when the changed symbol it came from
resolves to exactly one target; otherwise it stays at file level (every target
in that file), and a site no changed file owns stays at Review level. An empty
list with an incomplete pass is "no known consumers", never "no consumers" --
the readiness block says which.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .impact import _CALLERS_PER_SYMBOL, _MAX_CALLER_QUERIES

# lc-debt: fixed group/site caps for one Brief payload; page consumers per target if a Reader needs more.
MAX_GROUPS = 400
MAX_SITES_PER_GROUP = 50


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


def readiness(packet: Mapping[str, Any] | None) -> dict[str, Any]:
    """Index status and the degradation names the impact pass recorded for this Snapshot."""

    if packet is None:
        return {
            "source": "unavailable",
            "impact_pass": "unavailable",
            "index_status": "unknown",
            "degraded": [],
            "caller_expansion": {"max_symbols": _MAX_CALLER_QUERIES, "callers_per_symbol": _CALLERS_PER_SYMBOL},
            "sites_recorded": 0,
        }
    degraded = sorted({str(name) for name in packet.get("degraded") or () if isinstance(name, str)})
    status = str(packet.get("index_status") or "unknown")
    return {
        "source": "snapshot_packet",
        "impact_pass": "not_run" if "impact_disabled" in degraded else "ran",
        "index_status": status if status in ("fresh", "stale", "absent") else "unknown",
        "degraded": degraded,
        "caller_expansion": {"max_symbols": _MAX_CALLER_QUERIES, "callers_per_symbol": _CALLERS_PER_SYMBOL},
        "sites_recorded": sum(1 for site in packet.get("impact") or () if isinstance(site, Mapping)),
    }


def consumer_inventory(packet: Mapping[str, Any] | None, targets: Sequence[_Target]) -> dict[str, Any]:
    """``{"readiness", "consumers", "consumers_omitted"}`` for one Snapshot; deterministic for one packet."""

    ready = readiness(packet)
    if packet is None:
        return {"readiness": ready, "consumers": [], "consumers_omitted": 0}
    by_path: dict[str, list[_Target]] = {}
    for target in targets:
        by_path.setdefault(target.path, []).append(target)
    symbols: dict[tuple[str, str], Mapping[str, Any]] = {}
    for symbol in packet.get("symbols") or ():
        if isinstance(symbol, Mapping):
            key = (str(symbol.get("file_path") or ""), str(symbol.get("symbol_name") or ""))
            symbols.setdefault(key, symbol)

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in packet.get("impact") or ():
        if not isinstance(raw, Mapping):
            continue
        impact_kind = str(raw.get("kind") or "")
        source = str(raw.get("source_path") or "")
        name = str(raw.get("old") or "")
        caller = impact_kind == "untouched_caller"
        key = (source, name if caller or impact_kind == "removed_symbol" else "")
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
                # Recorded in this Snapshot's packet; Reader-time index rows would be ``current_index``.
                "exactness": "snapshot",
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
