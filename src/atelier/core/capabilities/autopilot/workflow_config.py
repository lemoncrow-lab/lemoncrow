"""Workflow-step defaults and state transitions for autopilot/STEM."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_PLANNING_SIGNAL = re.compile(
    r"\b(plan|planning|spec|design|architecture|roadmap|milestone|phase|approach|strategy)\b",
    re.IGNORECASE,
)
_EXECUTION_SIGNAL = re.compile(
    r"\b(fix|implement|code|edit|patch|write|test|run|debug|refactor|build|apply|change)\b",
    re.IGNORECASE,
)
_STEP_RANK = {"exploration": 0, "planning": 1, "execution": 2}
_STEP_SESSION_PHASE = {
    "exploration": "explore",
    "planning": "transition",
    "execution": "execute",
}
_PHASE_STEP = {
    "explore": "exploration",
    "exploration": "exploration",
    "transition": "planning",
    "planning": "planning",
    "execute": "execution",
    "execution": "execution",
}


@dataclass(frozen=True)
class WorkflowStepConfig:
    id: str
    share_context: bool = True
    sticky_window: int = 0
    advisory_vote: bool = False
    critical: bool = False


@dataclass(frozen=True)
class WorkflowConfig:
    steps: tuple[WorkflowStepConfig, ...] = field(default_factory=tuple)

    def step(self, step_id: str) -> WorkflowStepConfig:
        normalized = normalize_workflow_step(step_id)
        for step in self.steps:
            if step.id == normalized:
                return step
        return self.steps[0]


@dataclass(frozen=True)
class WorkflowState:
    current_step: str = "exploration"
    last_step: str = ""
    session_phase: str = "explore"
    sticky_window: int = 0
    advisory_emitted_steps: tuple[str, ...] = ()
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_step": self.current_step,
            "last_step": self.last_step,
            "session_phase": self.session_phase,
            "sticky_window": self.sticky_window,
            "advisory_emitted_steps": list(self.advisory_emitted_steps),
            "updated_at": self.updated_at,
        }


def default_workflow_config() -> WorkflowConfig:
    return WorkflowConfig(
        steps=(
            WorkflowStepConfig(id="exploration", share_context=False, sticky_window=0),
            WorkflowStepConfig(id="planning", share_context=True, sticky_window=1, advisory_vote=True, critical=True),
            WorkflowStepConfig(id="execution", share_context=True, sticky_window=3),
        )
    )


def normalize_workflow_step(value: str | None) -> str:
    normalized = (value or "").strip().lower().replace("-", "_")
    return _PHASE_STEP.get(normalized, normalized if normalized in _STEP_RANK else "exploration")


def workflow_state_from_mapping(
    raw: Mapping[str, Any] | None,
    config: WorkflowConfig | None = None,
) -> WorkflowState:
    cfg = config or default_workflow_config()
    data = raw if isinstance(raw, Mapping) else {}
    current = normalize_workflow_step(str(data.get("current_step") or data.get("workflow_step") or ""))
    last = normalize_workflow_step(str(data.get("last_step") or ""))
    sticky = max(0, int(data.get("sticky_window") or cfg.step(current).sticky_window))
    emitted_raw = data.get("advisory_emitted_steps") or ()
    emitted = tuple(
        normalize_workflow_step(str(step)) for step in emitted_raw if normalize_workflow_step(str(step)) in _STEP_RANK
    )
    return WorkflowState(
        current_step=current,
        last_step=last,
        session_phase=session_phase_for_step(current),
        sticky_window=sticky,
        advisory_emitted_steps=emitted,
        updated_at=str(data.get("updated_at") or ""),
    )


def session_phase_for_step(step_id: str) -> str:
    return _STEP_SESSION_PHASE.get(normalize_workflow_step(step_id), "explore")


def infer_workflow_step(
    trigger: str,
    payload: Mapping[str, Any],
    prior_state: WorkflowState,
    config: WorkflowConfig | None = None,
) -> str:
    cfg = config or default_workflow_config()
    explicit_raw = str(payload.get("workflow_step") or payload.get("session_phase") or "").strip()
    explicit = normalize_workflow_step(explicit_raw) if explicit_raw else ""
    if explicit and explicit in {step.id for step in cfg.steps}:
        candidate = explicit
    elif trigger == "session_start":
        candidate = "exploration"
    elif trigger == "post_edit" or payload.get("touched_files"):
        candidate = "execution"
    elif trigger == "user_prompt":
        prompt = str(payload.get("prompt") or "")
        if _PLANNING_SIGNAL.search(prompt):
            candidate = "planning"
        elif payload.get("files") or _EXECUTION_SIGNAL.search(prompt):
            candidate = "execution"
        else:
            candidate = prior_state.current_step or "exploration"
    else:
        candidate = prior_state.current_step or "exploration"

    if _STEP_RANK.get(candidate, 0) < _STEP_RANK.get(prior_state.current_step, 0):
        return prior_state.current_step
    return candidate


def advance_workflow_state(
    trigger: str,
    payload: Mapping[str, Any],
    prior_state: WorkflowState,
    config: WorkflowConfig | None = None,
) -> tuple[WorkflowState, WorkflowStepConfig, bool]:
    cfg = config or default_workflow_config()
    current = infer_workflow_step(trigger, payload, prior_state, cfg)
    step_cfg = cfg.step(current)
    changed = current != prior_state.current_step
    emitted = set(prior_state.advisory_emitted_steps)
    emit_advisory = changed and step_cfg.critical and step_cfg.advisory_vote and current not in emitted
    if emit_advisory:
        emitted.add(current)
    return (
        WorkflowState(
            current_step=current,
            last_step=prior_state.current_step if changed else prior_state.last_step,
            session_phase=session_phase_for_step(current),
            sticky_window=step_cfg.sticky_window,
            advisory_emitted_steps=tuple(sorted(emitted)),
            updated_at=datetime.now(UTC).isoformat(),
        ),
        step_cfg,
        emit_advisory,
    )
