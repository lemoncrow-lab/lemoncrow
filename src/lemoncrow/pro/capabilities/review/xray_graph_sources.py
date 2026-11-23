"""Gather one revision's :class:`~lemoncrow.pro.capabilities.review.xray_graph.XRayInputs` from the Review store.

Read-only: the stored packet (changed files, hunks, symbols), the revision's own runtime-validation records (the
recorded before/after requests and their bodies), the ``api_response`` behavior targets built from them, the
newest runtime-observation diffs (call traces and side effects) and the stored data-path report. Nothing here runs
a model, a runner or the clock.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from lemoncrow.pro.capabilities.review.xray_graph import (
    ChangedFile,
    ChangedSymbol,
    ObservedEffect,
    ObservedTrace,
    RecordedCall,
    RecordedRun,
    ResponseChange,
    Side,
    XRayInputs,
    canonical_json,
    parse_request_file,
    url_path,
)

_SIDE: Final[Mapping[str, Side]] = {"old": "base", "new": "head", "base": "base", "head": "head"}
_STATUS: Final[Mapping[str, str]] = {"added": "added", "deleted": "removed"}
#: The HTTP-service runner's summary: ``GET /api/plans returned HTTP 200``.
_SUMMARY_REQUEST: Final = re.compile(r"^([A-Z]{3,7}) (\S+) returned HTTP (\d{3})\b")
_BODY_CHANGED: Final = re.compile(r"([\d.]+)\s*%")
#: At most this many recorded requests are read per revision (newest per scenario and side first).
MAX_RECORDED_CALLS: Final[int] = 64


def _int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _ranges(rows: Iterable[object]) -> tuple[tuple[int, int], ...]:
    out: set[tuple[int, int]] = set()
    for row in rows:
        if isinstance(row, list | tuple) and len(row) == 2:
            start, end = _int(row[0]), _int(row[1])
            if 1 <= start <= end:
                out.add((start, end))
    return tuple(sorted(out))


def _hunk_ranges(hunks: object, side: str) -> tuple[tuple[int, int], ...]:
    """The changed spans of one side; a hunk without them (patch unread) counts its whole span."""

    spans: list[object] = []
    for hunk in hunks if isinstance(hunks, list) else ():
        if not isinstance(hunk, Mapping):
            continue
        exact = hunk.get(f"{side}_ranges")
        if isinstance(exact, list) and exact:
            spans.extend(exact)
            continue
        start, count = _int(hunk.get(f"{side}_start")), _int(hunk.get(f"{side}_lines"))
        if start >= 1 and count >= 1:
            spans.append((start, start + count - 1))
    return _ranges(spans)


def packet_inputs(packet: Mapping[str, Any] | None) -> tuple[tuple[ChangedFile, ...], tuple[ChangedSymbol, ...]]:
    """Changed files (with their exact changed spans) and symbols of a stored packet."""

    files: list[ChangedFile] = []
    for raw in (packet or {}).get("files") or ():
        if not isinstance(raw, Mapping) or not str(raw.get("path") or ""):
            continue
        if raw.get("is_binary") is True or raw.get("submodule_pointer"):
            continue
        status = _STATUS.get(str(raw.get("status") or ""), "modified")
        files.append(
            ChangedFile(
                path=str(raw["path"]),
                status=status,
                old_path=str(raw.get("old_path") or "") if status != "removed" else "",
                additions=_int(raw.get("additions")),
                deletions=_int(raw.get("deletions")),
                category=str(raw.get("category") or ""),
                head_ranges=_hunk_ranges(raw.get("hunks"), "new"),
                base_ranges=_hunk_ranges(raw.get("hunks"), "old"),
            )
        )
    symbols: list[ChangedSymbol] = []
    for raw in (packet or {}).get("symbols") or ():
        if not isinstance(raw, Mapping):
            continue
        name, path = str(raw.get("symbol_name") or ""), str(raw.get("file_path") or "")
        start, end = _int(raw.get("start_line")), _int(raw.get("end_line"))
        if name and path and 1 <= start <= max(start, end):
            symbols.append(
                ChangedSymbol(
                    name, str(raw.get("kind") or ""), path, start, max(start, end), str(raw.get("change") or "")
                )
            )
    return tuple(sorted(files, key=lambda row: row.path)), tuple(
        sorted(symbols, key=lambda row: (row.path, row.start_line, row.name))
    )


def read_json_record(payload: bytes | None, content_hash: str) -> Mapping[str, Any] | None:
    """A stored evidence record, only when its bytes match the recorded digest."""

    if payload is None or not content_hash or hashlib.sha256(payload).hexdigest() != content_hash:
        return None
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def recorded_calls(
    evidence: Sequence[Any],
    read_artifact: Callable[[Any], bytes | None],
    read_text: Callable[[str, str], str | None],
    revision_id: str,
) -> tuple[tuple[RecordedRun, ...], tuple[RecordedCall, ...], list[str]]:
    """The newest runtime-validation request per scenario and side on this exact revision."""

    from lemoncrow.pro.capabilities.review.runtime_validation import RUNTIME_VALIDATION_MEDIA_TYPE

    rows = sorted(
        (
            item
            for item in evidence
            if item.kind == "verification"
            and item.mime_type == RUNTIME_VALIDATION_MEDIA_TYPE
            and item.revision_id == revision_id
            and item.execution_intent != "explicit_historical"
        ),
        key=lambda item: (str(item.created_at), str(item.id)),
        reverse=True,
    )
    seen: set[tuple[str, str]] = set()
    runs: list[RecordedRun] = []
    calls: list[RecordedCall] = []
    unreadable = 0
    for item in rows:
        if len(calls) >= MAX_RECORDED_CALLS:
            break
        record = read_json_record(read_artifact(item), str(item.content_hash))
        if record is None:
            unreadable += 1
            continue
        side = _SIDE.get(str(record.get("side") or ""))
        request = _map(record.get("request"))
        scenario = _map(record.get("scenario"))
        if side is None or request.get("scenario_kind") == "recipe":
            continue  # check recipes are differential runs of their own, not request surfaces
        recipe = str(request.get("scenario_id") or "")
        surface = str(scenario.get("surface_id") or item.path or "")
        if not recipe or (recipe, side) in seen:
            continue
        seen.add((recipe, side))
        data = _map(record.get("data"))
        method, route = "", ""
        matched = _SUMMARY_REQUEST.match(str(record.get("summary") or ""))
        if matched:
            method, route = matched.group(1), url_path(matched.group(2))
        elif surface.endswith(".bru"):
            _name, method, route, _line = parse_request_file(
                read_text(side, surface) or read_text("head", surface) or read_text("base", surface) or ""
            )
        status = data.get("status_code")
        at = str(item.created_at or "")
        runs.append(RecordedRun(str(item.id), recipe, side, at))
        calls.append(
            RecordedCall(
                ref=str(item.id),
                recipe=recipe,
                side=side,
                surface=surface,
                title=str(scenario.get("title") or item.title or ""),
                method=method,
                route=route,
                status=status if isinstance(status, int) and not isinstance(status, bool) and status > 0 else None,
                body=data.get("body"),
                has_body="body" in data,
            )
        )
    degraded = [f"{unreadable} runtime validation record(s) did not match their digest"] if unreadable else []
    return tuple(runs), tuple(calls), degraded


def _artifact_ref(value: object) -> str:
    text = str(value or "")
    return text.split(":", 1)[1] if text.startswith("artifact:") else text


def response_changes(collection: Any, calls: Sequence[RecordedCall]) -> tuple[ResponseChange, ...]:
    """The ``api_response`` behavior targets whose two sides are runs this graph holds."""

    from lemoncrow.pro.capabilities.review.behavior import behavior_key

    refs = {call.ref for call in calls}
    out: list[ResponseChange] = []
    for delta in getattr(collection, "deltas", ()) or ():
        if getattr(delta, "kind", "") != "api_response":
            continue
        base_ref = _artifact_ref(getattr(getattr(delta, "base", None), "evidence_ref", ""))
        head_ref = _artifact_ref(getattr(getattr(delta, "head", None), "evidence_ref", ""))
        if not {base_ref, head_ref} & refs:
            continue
        facts = dict(getattr(delta, "facts", ()) or ())
        matched = _BODY_CHANGED.search(str(facts.get("Body changed") or ""))
        out.append(
            ResponseChange(
                behavior_key=behavior_key(delta),
                recipe=str(delta.subject),
                title=str(delta.title or ""),
                summary=str(delta.summary or ""),
                base_ref=base_ref if base_ref in refs else "",
                head_ref=head_ref if head_ref in refs else "",
                body_change=min(1.0, float(matched.group(1)) / 100.0) if matched else 0.0,
            )
        )
    return tuple(sorted(out, key=lambda row: (row.recipe, row.behavior_key)))


def observed(
    observations: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[RecordedRun, ...], tuple[ObservedTrace, ...], tuple[ObservedEffect, ...]]:
    """Runs, call traces and side-effect differences of the newest observation diff per recipe."""

    runs: list[RecordedRun] = []
    traces: list[ObservedTrace] = []
    effects: list[ObservedEffect] = []
    for recipe_id in sorted(observations):
        record = observations[recipe_id]
        at = str(record.get("observed_at") or "")
        sides = _map(record.get("sides"))
        refs: dict[str, str] = {}
        for name in ("base", "head"):
            side_row = _map(sides.get(name))
            if not str(side_row.get("evidence_id") or ""):
                continue
            side: Side = "base" if name == "base" else "head"
            ref = str(side_row["evidence_id"])
            refs[name] = ref
            runs.append(RecordedRun(ref, recipe_id, side, at))
            observation = _map(side_row.get("observation"))
            for edge in observation.get("trace") or ():
                if isinstance(edge, Mapping):
                    traces.append(
                        ObservedTrace(
                            ref,
                            side,
                            str(edge.get("caller") or ""),
                            str(edge.get("caller_fn") or ""),
                            str(edge.get("callee") or ""),
                            str(edge.get("callee_fn") or ""),
                        )
                    )
        diff = _map(record.get("diff"))
        if not diff.get("comparable"):
            continue
        for change in diff.get("changes") or ():
            if not isinstance(change, Mapping):
                continue
            kind = str(change.get("change") or "")
            # The run that saw it: a removed effect was seen only on base.
            ref = refs.get("base" if kind == "removed" else "head", "")
            observed_row = change.get("base" if kind == "removed" else "head")
            sources = observed_row.get("sources") if isinstance(observed_row, Mapping) else ()
            if ref:
                effects.append(
                    ObservedEffect(
                        ref,
                        str(change.get("category") or ""),
                        str(change.get("identity") or ""),
                        kind,
                        tuple(str(item) for item in sources or () if str(item)),
                    )
                )
    return tuple(runs), tuple(traces), tuple(effects)


def data_path_flows(report: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    """The stored data-path report's flows, added first, then removed, then rerouted."""

    flows = (report or {}).get("flows")
    if not isinstance(flows, Mapping):
        return ()
    return tuple(
        dict(row)
        for kind in ("added", "removed", "rerouted")
        for row in flows.get(kind) or ()
        if isinstance(row, Mapping)
    )


def collect_inputs(
    *,
    store: Any,
    revision: Any,
    evidence: Sequence[Any],
    read_text: Callable[[str, str], str | None],
    behavior: Callable[[], Any] | None = None,
    title: str = "",
) -> XRayInputs:
    """Everything the index graph of ``revision`` is built from (``read_text`` is ``(base|head, path)``)."""

    from lemoncrow.pro.capabilities.review import data_path
    from lemoncrow.pro.capabilities.review.runtime_observation_runs import latest_observations
    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    degraded: list[str] = []
    packet = read_packet_json(store, revision)
    if packet is None:
        degraded.append("The revision's packet is not readable")
    files, symbols = packet_inputs(packet)
    runs, calls, notes = recorded_calls(evidence, store.read_evidence_artifact, read_text, str(revision.id))
    degraded.extend(notes)
    responses: tuple[ResponseChange, ...] = ()
    if calls and behavior is not None:
        try:
            responses = response_changes(behavior(), calls)
        except (OSError, RuntimeError, ValueError, LookupError):
            degraded.append("API response comparisons could not be read")
    try:
        observation_runs, traces, effects = observed(latest_observations(store, revision))
    except (OSError, RuntimeError, ValueError, LookupError):
        observation_runs, traces, effects = (), (), ()
        degraded.append("Runtime observations could not be read")
    flows = data_path_flows(data_path.stored_report(store, revision))
    all_runs = tuple(
        sorted(
            {(run.ref, run.side): run for run in (*runs, *observation_runs)}.values(), key=lambda run: (run.at, run.ref)
        )
    )
    at = max([str(getattr(revision, "created_at", "") or ""), *(run.at for run in all_runs)])
    return XRayInputs(
        review_id=str(revision.review_id),
        revision_id=str(revision.id),
        title=title,
        generated_at=at,
        files=files,
        symbols=symbols,
        runs=all_runs,
        calls=calls,
        responses=responses,
        traces=traces,
        effects=effects,
        data_flows=flows,
        degraded=tuple(degraded),
    )


def inputs_digest(inputs: XRayInputs) -> str:
    """The identity of everything but the (immutable) revision texts: half of the index graph's cache stamp."""

    document = {
        "review_id": inputs.review_id,
        "revision_id": inputs.revision_id,
        "generated_at": inputs.generated_at,
        "files": [
            [
                row.path,
                row.status,
                row.old_path,
                row.additions,
                row.deletions,
                row.category,
                row.head_ranges,
                row.base_ranges,
            ]
            for row in inputs.files
        ],
        "symbols": [[row.name, row.kind, row.path, row.start_line, row.end_line, row.change] for row in inputs.symbols],
        "runs": [[row.ref, row.recipe, row.side, row.at] for row in inputs.runs],
        "calls": [
            [
                row.ref,
                row.recipe,
                row.side,
                row.surface,
                row.title,
                row.method,
                row.route,
                row.status,
                row.body,
                row.has_body,
            ]
            for row in inputs.calls
        ],
        "responses": [
            [row.behavior_key, row.recipe, row.title, row.summary, row.base_ref, row.head_ref, row.body_change]
            for row in inputs.responses
        ],
        "traces": [[row.ref, row.side, row.caller, row.caller_fn, row.callee, row.callee_fn] for row in inputs.traces],
        "effects": [[row.ref, row.category, row.identity, row.change, row.sources] for row in inputs.effects],
        "data_flows": list(inputs.data_flows),
        "degraded": list(inputs.degraded),
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


__all__ = [
    "MAX_RECORDED_CALLS",
    "collect_inputs",
    "data_path_flows",
    "inputs_digest",
    "observed",
    "packet_inputs",
    "read_json_record",
    "recorded_calls",
    "response_changes",
]
