from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_FULL_REF_PATTERN = re.compile(r"^\{\{\s*steps\.([A-Za-z0-9_\-]+)\.(output|output_json(?:\.[A-Za-z0-9_\-]+)*)\s*\}\}$")
_ANY_REF_PATTERN = re.compile(r"\{\{\s*steps\.[A-Za-z0-9_\-]+\.(?:output|output_json(?:\.[A-Za-z0-9_\-]+)*)\s*\}\}")
# G19: per-item binding references for map bodies, e.g. ``{{item}}`` or
# ``{{item.field}}``. The leading name must match an active binding var.
_FULL_ITEM_REF_PATTERN = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z0-9_\-]+)*)\s*\}\}$")


@dataclass(frozen=True)
class StepResult:
    step_id: str
    kind: str
    status: str
    output: Any = ""
    output_json: dict[str, Any] = field(default_factory=dict)
    execution_receipt: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    cost_usd: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "kind": self.kind,
            "status": self.status,
            "output": self.output,
            "output_json": copy.deepcopy(self.output_json),
            "execution_receipt": copy.deepcopy(self.execution_receipt),
            "duration_seconds": self.duration_seconds,
            "cost_usd": self.cost_usd,
            "error": self.error,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> StepResult:
        raw_output_json = raw.get("output_json")
        output_json: dict[str, Any] = dict(raw_output_json) if isinstance(raw_output_json, dict) else {}
        raw_execution_receipt = raw.get("execution_receipt")
        execution_receipt = dict(raw_execution_receipt) if isinstance(raw_execution_receipt, dict) else {}
        return cls(
            step_id=str(raw.get("step_id") or "").strip(),
            kind=str(raw.get("kind") or "").strip(),
            status=str(raw.get("status") or "").strip() or "pending",
            output=copy.deepcopy(raw.get("output")),
            output_json=copy.deepcopy(output_json),
            execution_receipt=copy.deepcopy(execution_receipt),
            duration_seconds=float(raw.get("duration_seconds") or 0.0),
            cost_usd=float(raw.get("cost_usd") or 0.0),
            error=str(raw.get("error") or "").strip(),
        )


@dataclass
class WorkflowContextState:
    run_id: str = ""
    status: str = "idle"
    definition_hash: str = ""
    step_results: dict[str, StepResult] = field(default_factory=dict)
    step_order: list[str] = field(default_factory=list)
    wave_spawn_plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    host_lane_observations: dict[str, dict[str, str]] = field(default_factory=dict)
    # Transient per-item bindings for an active `map` body scope (G19). Not
    # serialized — only meaningful while a map iteration is executing.
    item_bindings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "definition_hash": self.definition_hash,
            "step_results": {step_id: result.to_dict() for step_id, result in self.step_results.items()},
            "step_order": list(self.step_order),
            "wave_spawn_plans": copy.deepcopy(self.wave_spawn_plans),
            "host_lane_observations": copy.deepcopy(self.host_lane_observations),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> WorkflowContextState:
        data = raw if isinstance(raw, Mapping) else {}
        raw_results = data.get("step_results")
        step_results = (
            {
                str(step_id): StepResult.from_mapping(result)
                for step_id, result in raw_results.items()
                if isinstance(step_id, str) and isinstance(result, Mapping)
            }
            if isinstance(raw_results, Mapping)
            else {}
        )
        step_order = (
            [str(step_id) for step_id in data.get("step_order", []) if str(step_id).strip()]
            if isinstance(data.get("step_order"), list)
            else []
        )
        raw_wave_spawn_plans = data.get("wave_spawn_plans")
        wave_spawn_plans = (
            {
                str(step_id): dict(copy.deepcopy(plan))
                for step_id, plan in raw_wave_spawn_plans.items()
                if isinstance(step_id, str) and isinstance(plan, Mapping)
            }
            if isinstance(raw_wave_spawn_plans, Mapping)
            else {}
        )
        raw_host_lane_observations = data.get("host_lane_observations")
        host_lane_observations = (
            {
                str(lane_key): {str(key): str(value) for key, value in lane.items()}
                for lane_key, lane in raw_host_lane_observations.items()
                if isinstance(lane_key, str) and isinstance(lane, Mapping)
            }
            if isinstance(raw_host_lane_observations, Mapping)
            else {}
        )
        return cls(
            run_id=str(data.get("run_id") or "").strip(),
            status=str(data.get("status") or "").strip() or "idle",
            definition_hash=str(data.get("definition_hash") or "").strip(),
            step_results=step_results,
            step_order=step_order,
            wave_spawn_plans=wave_spawn_plans,
            host_lane_observations=host_lane_observations,
        )

    def record_step_result(self, result: StepResult) -> None:
        self.step_results[result.step_id] = result
        if result.step_id not in self.step_order:
            self.step_order.append(result.step_id)

    def fork_step_context(self, step_id: str) -> dict[str, Any]:
        result = self.step_results.get(step_id)
        if result is None:
            raise ValueError(f"unknown fork source: {step_id}")
        return copy.deepcopy(result.to_dict())

    def set_wave_spawn_plan(self, step_id: str, plan: Mapping[str, Any]) -> None:
        self.wave_spawn_plans[step_id] = copy.deepcopy(dict(plan))

    def spawn_plan_for_step(self, step_id: str) -> dict[str, Any]:
        plan = self.wave_spawn_plans.get(step_id)
        return copy.deepcopy(plan) if isinstance(plan, dict) else {}

    def observed_host_lane(self, lane_key: str) -> dict[str, str]:
        lane = self.host_lane_observations.get(lane_key)
        return copy.deepcopy(lane) if isinstance(lane, dict) else {}

    def record_host_lane(self, lane_key: str, lane: Mapping[str, str]) -> None:
        self.host_lane_observations[lane_key] = {str(key): str(value) for key, value in lane.items()}

    def set_item_bindings(self, bindings: Mapping[str, Any]) -> None:
        self.item_bindings = {str(key): copy.deepcopy(value) for key, value in bindings.items()}

    def clear_item_bindings(self) -> None:
        self.item_bindings = {}

    def _resolve_item_reference(self, reference: str) -> Any:
        match = _FULL_ITEM_REF_PATTERN.fullmatch(reference.strip())
        if match is None:
            raise ValueError(f"unsupported step reference: {reference}")
        name, path = match.groups()
        if name not in self.item_bindings:
            raise ValueError(f"unsupported step reference: {reference}")
        current: Any = copy.deepcopy(self.item_bindings[name])
        for part in path.split(".")[1:]:
            if part == "":
                continue
            if not isinstance(current, Mapping) or part not in current:
                raise ValueError(f"missing item path: {reference}")
            current = current[part]
        return copy.deepcopy(current)

    def resolve_reference(self, reference: str) -> Any:
        match = _FULL_REF_PATTERN.fullmatch(reference.strip())
        if match is None:
            return self._resolve_item_reference(reference)
        step_id, path = match.groups()
        result = self.step_results.get(step_id)
        if result is None or result.status != "done":
            raise ValueError(f"step output not available: {step_id}")
        if path == "output":
            return copy.deepcopy(result.output)
        current: Any = copy.deepcopy(result.output_json)
        for part in path.split(".")[1:]:
            if not isinstance(current, Mapping) or part not in current:
                raise ValueError(f"missing step output path: {reference}")
            current = current[part]
        return copy.deepcopy(current)

    def _is_active_item_ref(self, stripped: str) -> bool:
        match = _FULL_ITEM_REF_PATTERN.fullmatch(stripped)
        return match is not None and match.group(1) in self.item_bindings

    def render_value(self, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if _FULL_REF_PATTERN.fullmatch(stripped) or self._is_active_item_ref(stripped):
                return self.resolve_reference(stripped)
            if _ANY_REF_PATTERN.search(value):
                raise ValueError("workflow templates only support full-value substitutions")
            return value
        if isinstance(value, list):
            return [self.render_value(item) for item in value]
        if isinstance(value, tuple):
            return [self.render_value(item) for item in value]
        if isinstance(value, Mapping):
            return {str(key): self.render_value(item) for key, item in value.items()}
        return value


__all__ = ["StepResult", "WorkflowContextState"]
