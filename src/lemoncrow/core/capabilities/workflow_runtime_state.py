from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from lemoncrow.core.capabilities.workflow_context import WorkflowContextState

WORKFLOW_RUNTIME_STATE_KEY = "workflow_runtime"


def workflow_runtime_state(session_state: dict[str, Any]) -> dict[str, Any]:
    raw = session_state.get(WORKFLOW_RUNTIME_STATE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def write_workflow_runtime_state(session_state: dict[str, Any], runtime_state: dict[str, Any]) -> None:
    session_state[WORKFLOW_RUNTIME_STATE_KEY] = runtime_state


def coerce_workflow_review_decision(raw: Any) -> str:
    if not isinstance(raw, Mapping):
        return ""
    return str(raw.get("decision") or raw.get("review_decision") or "").strip().lower()


def workflow_runtime_status(session_state: dict[str, Any]) -> dict[str, Any]:
    workflow_state = (
        dict(session_state.get("workflow") or {}) if isinstance(session_state.get("workflow"), dict) else {}
    )
    runtime_state = workflow_runtime_state(session_state)
    runner_state = WorkflowContextState.from_mapping(runtime_state.get("runner"))
    current_step = (
        str(runtime_state.get("current_step") or "").strip()
        or str(workflow_state.get("current_step") or "").strip()
        or (runner_state.step_order[-1] if runner_state.step_order else "")
    )
    step_count = len(runtime_state.get("step_order") or []) or len(runner_state.step_order)
    completed_steps = sum(1 for result in runner_state.step_results.values() if result.status == "done")
    spawn_summary = (
        dict(runtime_state.get("spawn_summary") or {})
        if isinstance(runtime_state.get("spawn_summary"), dict)
        else (
            dict(workflow_state.get("spawn_summary") or {})
            if isinstance(workflow_state.get("spawn_summary"), dict)
            else {}
        )
    )
    return {
        "run_id": str(runtime_state.get("run_id") or runner_state.run_id or "").strip(),
        "workflow_id": str(runtime_state.get("workflow_id") or "").strip(),
        "status": str(runtime_state.get("status") or runner_state.status or "idle").strip() or "idle",
        "current_step": current_step,
        "session_phase": str(workflow_state.get("session_phase") or "").strip(),
        "step_count": step_count,
        "completed_steps": completed_steps,
        "paused_step_id": str(runtime_state.get("paused_step_id") or "").strip(),
        "failed_step_id": str(runtime_state.get("failed_step_id") or "").strip(),
        "pause_reason": str(runtime_state.get("pause_reason") or "").strip(),
        "stop_reason": str(runtime_state.get("stop_reason") or "").strip(),
        "review_decision": (
            str((runtime_state.get("plan_review") or {}).get("decision") or "").strip()
            if isinstance(runtime_state.get("plan_review"), dict)
            else ""
        ),
        "updated_at": str(runtime_state.get("updated_at") or "").strip(),
        "created_at": str(runtime_state.get("created_at") or "").strip(),
        "spawn_summary": spawn_summary,
    }


def require_active_workflow_runtime(session_state: dict[str, Any], run_id: str = "") -> dict[str, Any]:
    runtime_state = workflow_runtime_state(session_state)
    stored_run_id = str(runtime_state.get("run_id") or "").strip()
    if not runtime_state or not stored_run_id:
        raise ValueError("workflow runtime has no persisted run")
    if run_id and run_id.strip() and run_id.strip() != stored_run_id:
        raise ValueError(f"workflow runtime run_id mismatch: expected {stored_run_id}")
    return runtime_state


def workflow_runtime_detail(session_state: dict[str, Any]) -> dict[str, Any]:
    workflow_state = (
        dict(session_state.get("workflow") or {}) if isinstance(session_state.get("workflow"), dict) else {}
    )
    runtime_state = workflow_runtime_state(session_state)
    runner_state = WorkflowContextState.from_mapping(runtime_state.get("runner"))
    summary = workflow_runtime_status(session_state)
    task_outputs = (
        dict(workflow_state.get("task_outputs") or {})
        if isinstance(workflow_state.get("task_outputs"), dict)
        else {step_id: result.to_dict() for step_id, result in runner_state.step_results.items()}
    )
    can_resume = summary["status"] in {"awaiting_review", "review_rejected", "paused"}
    can_pause = summary["status"] in {"awaiting_review", "review_rejected", "paused"}
    can_stop = summary["status"] not in {"idle", "stopped"}
    control_payloads = {
        "status": {"op": "status", "run_id": summary["run_id"]},
        "pause": {"op": "pause", "run_id": summary["run_id"]},
        "stop": {"op": "stop", "run_id": summary["run_id"]},
    }
    if can_resume:
        control_payloads["resume_approve"] = {
            "op": "resume",
            "run_id": summary["run_id"],
            "plan_review": {"decision": "approve"},
        }
        control_payloads["resume_revise"] = {
            "op": "resume",
            "run_id": summary["run_id"],
            "plan_review": {"decision": "revise"},
        }
        control_payloads["resume_rerun"] = {
            "op": "resume",
            "run_id": summary["run_id"],
            "plan_review": {"decision": "rerun"},
        }
    return {
        "summary": summary,
        "workflow": (
            dict(runtime_state.get("workflow") or {}) if isinstance(runtime_state.get("workflow"), dict) else {}
        ),
        "route": dict(runtime_state.get("route") or {}) if isinstance(runtime_state.get("route"), dict) else {},
        "current_task": (
            dict(workflow_state.get("current_task") or {})
            if isinstance(workflow_state.get("current_task"), dict)
            else {}
        ),
        "plan_review": (
            dict(workflow_state.get("plan_review") or runtime_state.get("plan_review") or {})
            if isinstance(workflow_state.get("plan_review"), dict) or isinstance(runtime_state.get("plan_review"), dict)
            else {}
        ),
        "task_outputs": task_outputs,
        "spawn_summary": dict(summary.get("spawn_summary") or {}),
        "step_order": list(runtime_state.get("step_order") or runner_state.step_order),
        "available_actions": {
            "can_pause": can_pause,
            "can_resume": can_resume,
            "can_stop": can_stop,
            "resume_requires_host_call": True,
            "pause_is_snapshot_only": True,
            "stop_is_snapshot_only": True,
        },
        "control_payloads": control_payloads,
        "notes": {
            "snapshot_kind": "workspace-current",
            "live_control": False,
            "summary": "Workflow state is a workspace-local persisted snapshot, not a historical run ledger.",
        },
    }


def pause_workflow_runtime(
    session_state: dict[str, Any], *, run_id: str = "", pause_reason: str = ""
) -> dict[str, Any]:
    runtime_state = require_active_workflow_runtime(session_state, run_id)
    current_status = str(runtime_state.get("status") or "").strip()
    if current_status in {"success", "failed", "stopped", "idle"}:
        raise ValueError(f"workflow cannot pause from status: {current_status}")
    runtime_state["status"] = "paused"
    runtime_state["pause_reason"] = pause_reason.strip()
    runtime_state["updated_at"] = datetime.now(UTC).isoformat()
    write_workflow_runtime_state(session_state, runtime_state)
    return workflow_runtime_detail(session_state)


def stop_workflow_runtime(session_state: dict[str, Any], *, run_id: str = "", stop_reason: str = "") -> dict[str, Any]:
    runtime_state = require_active_workflow_runtime(session_state, run_id)
    runtime_state["status"] = "stopped"
    runtime_state["stop_reason"] = stop_reason.strip()
    runtime_state["updated_at"] = datetime.now(UTC).isoformat()
    write_workflow_runtime_state(session_state, runtime_state)
    return workflow_runtime_detail(session_state)


__all__ = [
    "WORKFLOW_RUNTIME_STATE_KEY",
    "coerce_workflow_review_decision",
    "pause_workflow_runtime",
    "require_active_workflow_runtime",
    "stop_workflow_runtime",
    "workflow_runtime_detail",
    "workflow_runtime_state",
    "workflow_runtime_status",
    "write_workflow_runtime_state",
]
