"""Tests for the M5 autopilot choreography capability."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from atelier.core.capabilities.autopilot import AutopilotCapability, AutopilotConfig, AutopilotEvent
from atelier.core.capabilities.autopilot import factory as autopilot_factory
from atelier.core.capabilities.autopilot.factory import run_autopilot_event
from atelier.core.capabilities.autopilot.workflow_config import (
    advance_workflow_state,
    default_workflow_config,
    workflow_state_from_mapping,
)
from atelier.core.capabilities.verification import Counterexample


def test_disabled_returns_noop() -> None:
    cap = AutopilotCapability(AutopilotConfig(enabled=False), lessons_fn=lambda: ["x"])
    action = cap.on_event(AutopilotEvent("session_start"))
    assert action.kind == "noop" and action.reason == "disabled"


def test_unknown_trigger_is_noop() -> None:
    cap = AutopilotCapability(AutopilotConfig())
    assert cap.on_event(AutopilotEvent("weird")).reason == "no_behavior"


def test_session_warm_injects_lessons() -> None:
    cap = AutopilotCapability(AutopilotConfig(), lessons_fn=lambda: ["prefer uv run", "hard-remove not deprecate"])
    action = cap.on_event(AutopilotEvent("session_start", {"cwd": "/repo"}))
    assert action.kind == "inject" and action.behavior == "session_warm"
    assert "prefer uv run" in action.content and action.injected_tokens > 0


def test_scoped_inject_uses_provider() -> None:
    seen: dict[str, Any] = {}

    def fake_pull(prompt: str, files: list[str]) -> Any:
        seen["prompt"] = prompt
        return SimpleNamespace(chunks=[SimpleNamespace(symbol="alpha", path="src/a.py")])

    cap = AutopilotCapability(AutopilotConfig(), scoped_pull_fn=fake_pull)
    action = cap.on_event(AutopilotEvent("user_prompt", {"prompt": "fix alpha"}))
    assert action.kind == "inject" and action.behavior == "scoped_inject"
    assert "alpha" in action.content and seen["prompt"] == "fix alpha"


def test_scoped_inject_noop_without_provider() -> None:
    cap = AutopilotCapability(AutopilotConfig())
    assert cap.on_event(AutopilotEvent("user_prompt", {"prompt": "x"})).reason == "no_provider"


def _inject_cap() -> AutopilotCapability:
    return AutopilotCapability(
        AutopilotConfig(),
        scoped_pull_fn=lambda prompt, files: SimpleNamespace(chunks=[SimpleNamespace(symbol="alpha", path="src/a.py")]),
    )


def test_gate_skips_meta_prompt() -> None:
    action = _inject_cap().on_event(AutopilotEvent("user_prompt", {"prompt": "what is prompt-gating?"}))
    assert action.kind == "noop" and action.reason == "not_coding_prompt"


def test_gate_skips_chat_prompt() -> None:
    for chat in ("yes", "thanks", "continue", "ok sounds good"):
        action = _inject_cap().on_event(AutopilotEvent("user_prompt", {"prompt": chat}))
        assert action.kind == "noop", f"{chat!r} should not inject"


def test_scoped_inject_caps_chunks() -> None:
    many = SimpleNamespace(chunks=[SimpleNamespace(symbol=f"s{i}", path=f"f{i}.py") for i in range(40)])
    cap = AutopilotCapability(AutopilotConfig(max_inject_chunks=8), scoped_pull_fn=lambda p, f: many)
    action = cap.on_event(AutopilotEvent("user_prompt", {"prompt": "refactor the parser module"}))
    assert action.kind == "inject"
    assert sum(1 for line in action.content.splitlines() if line.startswith("- ")) == 8


def test_gate_allows_coding_prompt() -> None:
    # coding verb
    a1 = _inject_cap().on_event(AutopilotEvent("user_prompt", {"prompt": "fix the failing auth flow"}))
    # code signal (filename / identifier)
    a2 = _inject_cap().on_event(AutopilotEvent("user_prompt", {"prompt": "what does tool_smart_read in a.py do"}))
    assert a1.kind == "inject" and a2.kind == "inject"


def test_counterexamples_injected() -> None:
    ce = Counterexample(check="typecheck", severity="error", file_path="a.py", line=1, diagnostic="bad")
    cap = AutopilotCapability(AutopilotConfig(), verify_fn=lambda files: [ce])
    action = cap.on_event(AutopilotEvent("post_edit", {"touched_files": ["a.py"]}))
    assert action.kind == "inject" and "<counterexample" in action.content
    assert "retry budget: 1/3" in action.content


def test_counterexamples_clean_is_noop() -> None:
    cap = AutopilotCapability(AutopilotConfig(), verify_fn=lambda files: [])
    assert cap.on_event(AutopilotEvent("post_edit", {"touched_files": ["a.py"]})).reason == "clean"


def test_counterexample_repeats_trigger_rescue_on_third_attempt() -> None:
    ce = Counterexample(
        check="typecheck",
        severity="error",
        file_path="a.py",
        line=1,
        diagnostic="bad",
        repro_command="uv run mypy a.py",
    )
    cap = AutopilotCapability(AutopilotConfig(), verify_fn=lambda files: [ce])

    first = cap.on_event(AutopilotEvent("post_edit", {"touched_files": ["a.py"]}))
    second = cap.on_event(AutopilotEvent("post_edit", {"touched_files": ["a.py"]}))
    third = cap.on_event(AutopilotEvent("post_edit", {"touched_files": ["a.py"]}))

    assert first.kind == "inject" and "retry budget: 1/3" in first.content
    assert second.kind == "inject" and "retry budget: 2/3" in second.content
    assert third.kind == "inject"
    assert "Switch to rescue-style debugging" in third.content
    assert "<counterexample" not in third.content


def test_counterexample_clean_run_resets_budget() -> None:
    ce = Counterexample(check="typecheck", severity="error", file_path="a.py", line=1, diagnostic="bad")
    calls = {"count": 0}

    def fake_verify(files: list[str]) -> list[Counterexample]:
        calls["count"] += 1
        if calls["count"] == 2:
            return []
        return [ce]

    cap = AutopilotCapability(AutopilotConfig(), verify_fn=fake_verify)

    first = cap.on_event(AutopilotEvent("post_edit", {"touched_files": ["a.py"]}))
    clean = cap.on_event(AutopilotEvent("post_edit", {"touched_files": ["a.py"]}))
    reset = cap.on_event(AutopilotEvent("post_edit", {"touched_files": ["a.py"]}))

    assert first.kind == "inject" and "retry budget: 1/3" in first.content
    assert clean.kind == "noop" and clean.reason == "clean"
    assert reset.kind == "inject" and "retry budget: 1/3" in reset.content


def test_counterexample_budgets_track_signatures_independently() -> None:
    ce_a = Counterexample(check="typecheck", severity="error", file_path="a.py", line=1, diagnostic="bad a")
    ce_b = Counterexample(check="typecheck", severity="error", file_path="b.py", line=2, diagnostic="bad b")
    calls = {"count": 0}

    def fake_verify(files: list[str]) -> list[Counterexample]:
        calls["count"] += 1
        return [ce_a] if calls["count"] != 2 else [ce_b]

    cap = AutopilotCapability(AutopilotConfig(), verify_fn=fake_verify)

    first = cap.on_event(AutopilotEvent("post_edit", {"touched_files": ["a.py"]}))
    different = cap.on_event(AutopilotEvent("post_edit", {"touched_files": ["b.py"]}))
    again = cap.on_event(AutopilotEvent("post_edit", {"touched_files": ["a.py"]}))

    assert "retry budget: 1/3" in first.content
    assert "retry budget: 1/3" in different.content
    assert "retry budget: 2/3" in again.content


def test_verify_provider_defaults_typecheck_for_python(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    def fake_run(self: Any, *, scope_files: list[str], checks: tuple[str, ...]) -> list[Counterexample]:
        seen["scope_files"] = scope_files
        seen["checks"] = checks
        return []

    monkeypatch.delenv("ATELIER_AUTOPILOT_TYPECHECK", raising=False)
    monkeypatch.delenv("ATELIER_AUTOPILOT_TESTS", raising=False)
    monkeypatch.setattr("atelier.core.capabilities.verification.VerifierCapability.run", fake_run)

    result = autopilot_factory._verify_provider("/repo")(["src/a.py", "README.md"])

    assert result == []
    assert seen["scope_files"] == ["src/a.py", "README.md"]
    assert seen["checks"] == ("lint", "typecheck")


def test_verify_provider_adds_semantic_when_task_intent_present(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    def fake_run(self: Any, *, scope_files: list[str], checks: tuple[str, ...]) -> list[Counterexample]:
        seen["scope_files"] = scope_files
        seen["checks"] = checks
        seen["task_intent"] = self._task_intent
        return []

    monkeypatch.delenv("ATELIER_AUTOPILOT_SEMANTIC", raising=False)
    monkeypatch.setattr("atelier.core.capabilities.verification.VerifierCapability.run", fake_run)

    result = autopilot_factory._verify_provider("/repo", task_intent="fix the auth flow")(["src/a.py"])

    assert result == []
    assert seen["scope_files"] == ["src/a.py"]
    assert seen["checks"] == ("lint", "typecheck", "semantic")
    assert seen["task_intent"] == "fix the auth flow"


def test_verify_provider_respects_typecheck_and_tests_env_overrides(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    def fake_run(self: Any, *, scope_files: list[str], checks: tuple[str, ...]) -> list[Counterexample]:
        seen["checks"] = checks
        return []

    monkeypatch.setenv("ATELIER_AUTOPILOT_TYPECHECK", "0")
    monkeypatch.setenv("ATELIER_AUTOPILOT_TESTS", "1")
    monkeypatch.setattr("atelier.core.capabilities.verification.VerifierCapability.run", fake_run)

    result = autopilot_factory._verify_provider("/repo")(["src/a.py", "tests/test_a.py"])

    assert result == []
    assert seen["checks"] == ("lint", "tests")


def test_build_autopilot_reads_last_user_prompt_from_session_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_state = {"last_user_prompt": "fix the auth flow"}

    cap = autopilot_factory.build_autopilot(
        store_root=str(tmp_path / ".atelier"),
        workspace=str(workspace),
        session_state=session_state,
    )

    verify_fn = cap._verify_fn
    assert verify_fn is not None
    closure_vars = inspect.getclosurevars(verify_fn)
    assert closure_vars.nonlocals["task_intent"] == "fix the auth flow"


def test_dedup_suppresses_repeat() -> None:
    cap = AutopilotCapability(AutopilotConfig(), lessons_fn=lambda: ["same"])
    first = cap.on_event(AutopilotEvent("session_start"))
    second = cap.on_event(AutopilotEvent("session_start"))
    assert first.kind == "inject" and second.reason == "deduped"


def test_budget_truncation() -> None:
    big = [f"lesson number {i} with some descriptive text" for i in range(500)]
    cap = AutopilotCapability(AutopilotConfig(max_inject_tokens=80), lessons_fn=lambda: big)
    action = cap.on_event(AutopilotEvent("session_start"))
    assert action.kind == "inject" and action.injected_tokens <= 80


def test_fail_open_on_provider_error() -> None:
    def boom() -> list[str]:
        raise RuntimeError("provider down")

    cap = AutopilotCapability(AutopilotConfig(), lessons_fn=boom)
    assert cap.on_event(AutopilotEvent("session_start")).reason == "error"


def test_workflow_progression_is_monotonic() -> None:
    config = default_workflow_config()
    state = workflow_state_from_mapping({}, config)

    planning, _, emit_advisory = advance_workflow_state(
        "user_prompt",
        {"prompt": "design the rollout plan"},
        state,
        config,
    )
    execution, _, _ = advance_workflow_state("post_edit", {"touched_files": ["src/a.py"]}, planning, config)
    steady, _, _ = advance_workflow_state("user_prompt", {"prompt": "read the docs"}, execution, config)

    assert planning.current_step == "planning"
    assert emit_advisory is True
    assert execution.current_step == "execution"
    assert steady.current_step == "execution"


def test_run_autopilot_event_persists_workflow_state_and_advisory_once(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path / ".atelier"))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))

    first = run_autopilot_event("user_prompt", {"prompt": "design the rollout plan"})
    second = run_autopilot_event("user_prompt", {"prompt": "design the rollout plan"})

    assert first.kind == "inject"
    assert "marked critical" in first.content
    assert second.kind == "noop"

    session_state_path = tmp_path / ".atelier" / "workspaces"
    files = list(session_state_path.glob("*/session_state.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["workflow"]["current_step"] == "planning"
    assert payload["workflow"]["session_phase"] == "transition"
    assert payload["workflow"]["advisory_emitted_steps"] == ["planning"]


def test_run_autopilot_event_persists_counterexample_budget(tmp_path: Path, monkeypatch: Any) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path / ".atelier"))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))

    ce = Counterexample(
        check="typecheck",
        severity="error",
        file_path="src/a.py",
        line=1,
        diagnostic="bad",
        repro_command="uv run mypy src/a.py",
    )
    monkeypatch.setattr(
        "atelier.core.capabilities.autopilot.factory._verify_provider",
        lambda workspace, task_intent=None: lambda files: [ce],
    )

    first = run_autopilot_event("post_edit", {"touched_files": ["src/a.py"]})
    second = run_autopilot_event("post_edit", {"touched_files": ["src/a.py"]})
    third = run_autopilot_event("post_edit", {"touched_files": ["src/a.py"]})

    assert "retry budget: 1/3" in first.content
    assert "retry budget: 2/3" in second.content
    assert "Switch to rescue-style debugging" in third.content

    session_state_path = tmp_path / ".atelier" / "workspaces"
    files = list(session_state_path.glob("*/session_state.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    attempts_by_key = payload["counterexample_budget"]["attempts_by_key"]
    assert len(attempts_by_key) == 1
    assert next(iter(attempts_by_key.values())) == 3
