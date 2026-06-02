"""Provider wiring + hook entrypoint for autopilot choreography (M5).

Builds an :class:`AutopilotCapability` with best-effort providers resolved from
the store root + workspace. Every provider is fail-open: if a dependency cannot
be constructed or errors, it yields empty results and the behavior degrades to
a noop rather than raising. Hooks call :func:`run_autopilot_event` and deliver
the result with :func:`emit_action`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .capability import AutopilotCapability
from .models import AutopilotAction, AutopilotConfig, AutopilotEvent
from .workflow_config import (
    advance_workflow_state,
    default_workflow_config,
    workflow_state_from_mapping,
)

_HOOK_EVENT_NAMES = {
    "session_start": "SessionStart",
    "user_prompt": "UserPromptSubmit",
    "post_edit": "PostToolUse",
}


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "off", "no", ""}


def config_from_env() -> AutopilotConfig:
    return AutopilotConfig(
        enabled=_flag("ATELIER_AUTOPILOT", True),
        session_warm=_flag("ATELIER_AUTOPILOT_SESSION_WARM", True),
        scoped_inject=_flag("ATELIER_AUTOPILOT_SCOPED_INJECT", True),
        counterexamples=_flag("ATELIER_AUTOPILOT_COUNTEREXAMPLES", True),
    )


def _lessons_provider(store_root: str) -> Any:
    def fn() -> list[str]:
        try:
            from atelier.core.capabilities.lesson_promotion import LessonPromoterCapability
            from atelier.infra.storage.factory import create_store

            cap = LessonPromoterCapability(create_store(Path(store_root)))
            items = cap.inbox(limit=5)
        except Exception:
            logging.exception("Recovered from broad exception handler")
            return []
        out: list[str] = []
        for it in items:
            text = (
                getattr(it, "summary", None)
                or getattr(it, "title", None)
                or getattr(it, "lesson", None)
                or getattr(it, "text", None)
            )
            if text:
                out.append(str(text))
        return out

    return fn


def _scoped_pull_provider(workspace: str) -> Any:
    def fn(prompt: str, files: list[str]) -> Any:
        try:
            from atelier.core.capabilities.code_context import CodeContextEngine
            from atelier.core.capabilities.scoped_context import ScopedContextCapability, Subtask

            engine = CodeContextEngine(Path(workspace))
            cap = ScopedContextCapability(engine)
            return cap.pull(Subtask(description=prompt, affected_paths=list(files), budget_tokens=1200))
        except Exception:
            logging.exception("Recovered from broad exception handler")
            return None

    return fn


def _verify_provider(workspace: str, *, task_intent: str | None = None) -> Any:
    def fn(files: list[str]) -> list[Any]:
        try:
            from atelier.core.capabilities.verification import VerifierCapability

            checks: list[str] = ["lint"]
            normalized_intent = str(task_intent or "").strip()
            if any(Path(file_path).suffix == ".py" for file_path in files) and _flag(
                "ATELIER_AUTOPILOT_TYPECHECK", True
            ):
                checks.append("typecheck")
            if _flag("ATELIER_AUTOPILOT_TESTS", False):
                checks.append("tests")
            if normalized_intent and _flag("ATELIER_AUTOPILOT_SEMANTIC", True):
                checks.append("semantic")
            return VerifierCapability(cwd=workspace, task_intent=normalized_intent).run(
                scope_files=list(files), checks=tuple(checks)
            )
        except Exception:
            logging.exception("Recovered from broad exception handler")
            return []

    return fn


def build_autopilot(
    *, store_root: str, workspace: str, session_state: dict[str, Any] | None = None
) -> AutopilotCapability:
    session_state = session_state if session_state is not None else _read_session_state(store_root, workspace)
    retry_budget = _retry_budget_from_state(session_state)
    last_user_prompt = str(session_state.get("last_user_prompt") or "").strip()
    return AutopilotCapability(
        config_from_env(),
        lessons_fn=_lessons_provider(store_root),
        scoped_pull_fn=_scoped_pull_provider(workspace),
        verify_fn=_verify_provider(workspace, task_intent=last_user_prompt),
        retry_budget=retry_budget,
    )


def _session_state_path(store_root: str, workspace: str) -> Path:
    import hashlib

    ws_hash = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]
    return Path(store_root) / "workspaces" / ws_hash / "session_state.json"


def _read_session_state(store_root: str, workspace: str) -> dict[str, Any]:
    path = _session_state_path(store_root, workspace)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_session_state(store_root: str, workspace: str, state: dict[str, Any]) -> None:
    path = _session_state_path(store_root, workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(state, tmp, indent=2)
            tmp_path = tmp.name
        Path(tmp_path).replace(path)
    except OSError:
        if tmp_path:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)


def _retry_budget_from_state(state: dict[str, Any]) -> Any:
    from atelier.core.capabilities.verification import RetryBudget

    return RetryBudget.from_mapping(state.get("counterexample_budget"))


def _persist_retry_budget(state: dict[str, Any], cap: AutopilotCapability) -> None:
    budget = getattr(cap, "_retry_budget", None)
    if budget is None:
        state.pop("counterexample_budget", None)
        return
    payload = budget.to_dict()
    if payload.get("attempts_by_key"):
        state["counterexample_budget"] = payload
    else:
        state.pop("counterexample_budget", None)


def run_autopilot_event(trigger: str, payload: dict[str, Any]) -> AutopilotAction:
    """Resolve roots from env, build the capability, and evaluate one event."""
    try:
        store_root = (
            os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or str(Path.home() / ".atelier")
        )
        workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd()
        session_state = _read_session_state(store_root, workspace)
        workflow_config = default_workflow_config()
        prior_workflow = workflow_state_from_mapping(session_state.get("workflow"), workflow_config)
        workflow_state, step_cfg, emit_advisory = advance_workflow_state(
            trigger,
            payload,
            prior_workflow,
            workflow_config,
        )
        session_state["workflow"] = workflow_state.to_dict()
        cap = build_autopilot(store_root=store_root, workspace=workspace, session_state=session_state)
        enriched_payload = dict(payload)
        enriched_payload.update(
            {
                "workflow_step": workflow_state.current_step,
                "session_phase": workflow_state.session_phase,
                "workflow_share_context": step_cfg.share_context,
                "workflow_sticky_window": step_cfg.sticky_window,
                "workflow_vote_advisory": emit_advisory,
            }
        )
        action = cap.on_event(AutopilotEvent(trigger, enriched_payload))
        _persist_retry_budget(session_state, cap)
        _write_session_state(store_root, workspace, session_state)
        return action
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return AutopilotAction.noop("error")


def emit_action(trigger: str, action: AutopilotAction) -> None:
    """Write the host's additionalContext payload to stdout (no-op for noop)."""
    if action.kind != "inject" or not action.content:
        return
    hook_event = _HOOK_EVENT_NAMES.get(trigger, "")
    if not hook_event:
        return
    payload = {
        "hookSpecificOutput": {
            "hookEventName": hook_event,
            "additionalContext": action.content,
        }
    }
    sys.stdout.write(json.dumps(payload))


def run_and_emit(trigger: str, payload: dict[str, Any]) -> None:
    """Convenience for hooks: evaluate an event and emit any injection."""
    # fail-open: never block the agent
    with contextlib.suppress(Exception):
        emit_action(trigger, run_autopilot_event(trigger, payload))
