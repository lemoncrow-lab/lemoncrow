"""Ground the changed-execution-flow view in observed traces (Phase 9R item 14).

Phase 3's architecture delta infers a changed-relationship flow from the
exact import/reference graph. The observed runs of ``side_effects.runtime``
record the call edges into changed Python files on each side (same runs,
same Snapshot). This module joins the two:

* a flow step whose ``from -> to`` relationship was observed as a call on
  the side it describes (head for added/changed, base for removed) is
  ``grounding: "observed"`` and cites the trace evidence (attempt record
  ref, the diff record, caller/callee functions);
* every other step stays ``grounding: "inferred"`` -- never upgraded;
* the base and head call sequences (first-seen order) are returned as
  sequence-diagram messages; added/removed messages are claimed only when
  both sides ran under one environment fingerprint.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from lemoncrow.pro.capabilities.review.runtime_observation import TraceEdge
from lemoncrow.pro.capabilities.review.runtime_observation_runs import latest_observations

MAX_MESSAGES: Final[int] = 60


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def observed_traces(records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Per-side call edges (with their evidence) from the newest observation diff records."""

    sides: dict[str, list[dict[str, Any]]] = {"base": [], "head": []}
    comparable = bool(records)
    seen: dict[str, set[TraceEdge]] = {"base": set(), "head": set()}
    for recipe_id in sorted(records):
        record = records[recipe_id]
        comparable = comparable and _mapping(record.get("diff")).get("comparable") is True
        for name in ("base", "head"):
            side = _mapping(_mapping(record.get("sides")).get(name))
            observation = _mapping(side.get("observation"))
            for raw in observation.get("trace") or ():
                if not isinstance(raw, Mapping):
                    continue
                edge = TraceEdge.from_wire(raw)
                if not edge.callee or edge in seen[name]:
                    continue
                seen[name].add(edge)
                sides[name].append(
                    {
                        **edge.to_wire(),
                        "recipe": recipe_id,
                        "evidence_ref": str(side.get("evidence_ref") or ""),
                        "record_id": str(record.get("evidence_id") or ""),
                    }
                )
    return {"comparable": comparable, "sides": sides}


def _message(edge: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "from": edge["caller"],
        "from_fn": edge["caller_fn"],
        "to": edge["callee"],
        "to_fn": edge["callee_fn"],
        "evidence_ref": edge["evidence_ref"],
    }


def _key(edge: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (str(edge["caller"]), str(edge["caller_fn"]), str(edge["callee"]), str(edge["callee_fn"]))


def ground_flow(flow: Mapping[str, Any] | None, traces: Mapping[str, Any]) -> dict[str, Any]:
    """The flow projection with per-step grounding and observed base/head sequences."""

    out = dict(flow or {})
    sides = _mapping(traces.get("sides"))
    base_edges = [edge for edge in sides.get("base") or () if isinstance(edge, Mapping)]
    head_edges = [edge for edge in sides.get("head") or () if isinstance(edge, Mapping)]
    steps: list[dict[str, Any]] = []
    observed = 0
    for raw in out.get("steps") or ():
        if not isinstance(raw, Mapping):
            continue
        step = dict(raw)
        status = str(step.get("delta_status") or "")
        side, candidates = ("base", base_edges) if status == "removed" else ("head", head_edges)
        match = next(
            (
                edge
                for edge in candidates
                if edge.get("caller") == step.get("from") and edge.get("callee") == step.get("to")
            ),
            None,
        )
        if match is None:
            step["grounding"] = "inferred"
            step["trace"] = None
        else:
            observed += 1
            step["grounding"] = "observed"
            step["trace"] = {
                "side": side,
                "caller_fn": match.get("caller_fn"),
                "callee_fn": match.get("callee_fn"),
                "recipe": match.get("recipe"),
                "evidence_ref": match.get("evidence_ref"),
                "record_id": match.get("record_id"),
            }
        steps.append(step)
    out["steps"] = steps
    comparable = traces.get("comparable") is True
    base_keys, head_keys = {_key(edge) for edge in base_edges}, {_key(edge) for edge in head_edges}
    out["observed"] = {
        "available": bool(base_edges or head_edges),
        "comparable": comparable,
        "grounded_steps": observed,
        "inferred_steps": len(steps) - observed,
        "base": [_message(edge) for edge in base_edges[:MAX_MESSAGES]],
        "head": [_message(edge) for edge in head_edges[:MAX_MESSAGES]],
        "added": (
            [_message(edge) for edge in head_edges if _key(edge) not in base_keys][:MAX_MESSAGES] if comparable else []
        ),
        "removed": (
            [_message(edge) for edge in base_edges if _key(edge) not in head_keys][:MAX_MESSAGES] if comparable else []
        ),
        "note": (
            "Observed steps cite call traces from the planned runtime-observation runs of this revision; "
            "other steps are inferred from the import graph."
            if base_edges or head_edges
            else "No observed trace for this revision; every step is inferred from the import graph."
        ),
    }
    return out


def ground_architecture_flow(delta: Mapping[str, Any], store: Any, revision: Any) -> dict[str, Any]:
    """A copy of the architecture delta whose flow is grounded in this Snapshot's traces."""

    try:
        traces = observed_traces(latest_observations(store, revision))
    except (OSError, ValueError, TypeError):
        traces = {"comparable": False, "sides": {}}
    flow = delta.get("flow")
    return {**dict(delta), "flow": ground_flow(flow if isinstance(flow, Mapping) else None, traces)}


def sequence_steps(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Plain-text sequence lines (for model context and tests)."""

    return [f"{row['from']}:{row['from_fn']} -> {row['to']}:{row['to_fn']}" for row in messages]


__all__ = ["ground_architecture_flow", "ground_flow", "observed_traces", "sequence_steps"]
