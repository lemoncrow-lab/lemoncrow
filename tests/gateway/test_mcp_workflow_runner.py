from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.gateway.adapters import mcp_server
from tests.helpers import init_store_at


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    request: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    response = mcp_server._handle(request)
    assert isinstance(response, dict)
    return response


def _result(response: dict[str, Any]) -> dict[str, Any]:
    assert "result" in response, response
    payload = json.loads(response["result"]["content"][0]["text"])
    assert isinstance(payload, dict)
    return payload


@pytest.fixture()
def workflow_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-wf-session")
    mcp_server._ledger._current_ledger = None
    mcp_server._ledger._realtime_ctx = None
    mcp_server._remote_client = None
    # _resolve_live_session_id caches the session id by window-file mtime (0.0 in
    # test environments).  If a prior test in the same xdist worker cached
    # (0.0, "") the current test would never re-resolve from the env var below.
    # Reset every cache so the first _resolve_live_session_id() reads our pin.
    mcp_server._reset_runtime_cache_for_testing()
    # WorkflowRunner runs each step on a worker thread while _run_owned_workflow
    # holds the re-entrant _STATE_LOCK on the main thread. When the worker
    # resolves a session id and the module caches happen to be empty (e.g. a
    # preceding suite left them unset), _get_claude_session_id() falls through to
    # _get_product_session_id(), which acquires _STATE_LOCK unconditionally from
    # the wrong thread -> a cross-thread deadlock that only unblocks at the 600s
    # per-step deadline. Pin session resolution so the worker never contends for
    # the lock, keeping these tests fast and hermetic regardless of suite order.
    monkeypatch.setattr(mcp_server, "_get_product_session_id", lambda: "test-product-session")
    return root


def test_workflow_tool_run_delegates_to_owned_runner(workflow_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run_workflow(arguments: dict[str, Any]) -> dict[str, Any]:
        seen["arguments"] = arguments
        return {"run_id": "run-123", "status": "success", "step_count": 2, "artifact_ids": []}

    monkeypatch.setattr(mcp_server, "_run_owned_workflow", fake_run_workflow)

    payload = _result(
        _call(
            "workflow",
            {
                "op": "run",
                "workflow": {
                    "workflow_id": "owned-review-loop",
                    "steps": [
                        {
                            "step_id": "read_spec",
                            "kind": "tool",
                            "tool": "read",
                            "args": {"path": "README.md"},
                        }
                    ],
                },
            },
        )
    )

    assert payload == {
        "run_id": "run-123",
        "status": "success",
        "step_count": 2,
        "artifact_ids": [],
    }
    assert seen["arguments"]["workflow"]["workflow_id"] == "owned-review-loop"


def test_workflow_tool_run_returns_runner_receipt_shape(workflow_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_server,
        "_run_owned_workflow",
        lambda arguments: {
            "run_id": "run-456",
            "status": "failed",
            "step_count": 3,
            "failed_step_id": "review",
            "artifact_ids": ["trace-1"],
        },
    )

    payload = _result(
        _call(
            "workflow",
            {
                "op": "run",
                "workflow": {
                    "workflow_id": "owned-review-loop",
                    "steps": [
                        {
                            "step_id": "read_spec",
                            "kind": "tool",
                            "tool": "read",
                            "args": {"path": "README.md"},
                        }
                    ],
                },
            },
        )
    )

    assert payload["run_id"] == "run-456"
    assert payload["status"] == "failed"
    assert payload["step_count"] == 3
    assert payload["failed_step_id"] == "review"
    assert payload["artifact_ids"] == ["trace-1"]


def test_workflow_tool_status_pause_resume_and_stop(workflow_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        mcp_server,
        "resolve_swarm_runner_command",
        lambda **_kwargs: ["fake-runner", _kwargs["prompt_template"]],
    )

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        text: bool,
        capture_output: bool,
        check: bool,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        prompt = command[-1]
        calls.append(prompt)
        if "Draft the implementation plan." in prompt:
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="plan ready", stderr="")
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="applied", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    workflow = {
        "workflow_id": "review-gated",
        "steps": [
            {"step_id": "plan", "kind": "agent", "prompt": "Draft the implementation plan."},
            {
                "step_id": "execute",
                "kind": "agent",
                "prompt": "Apply the approved plan.",
                "requires_plan_review": True,
            },
        ],
    }

    started = _result(_call("workflow", {"op": "run", "workflow": workflow}))
    assert started["status"] == "awaiting_review"

    status = _result(_call("workflow", {"op": "status"}))
    assert status["status"] == "awaiting_review"
    assert status["paused_step_id"] == "execute"

    paused = _result(_call("workflow", {"op": "pause", "pause_reason": "waiting on approval"}))
    assert paused["status"] == "paused"
    assert paused["pause_reason"] == "waiting on approval"

    resumed = _result(_call("workflow", {"op": "resume", "plan_review": {"decision": "approve"}}))
    assert resumed["status"] == "success"

    stopped = _result(_call("workflow", {"op": "stop", "stop_reason": "user cancelled future follow-up"}))
    assert stopped["status"] == "stopped"
    assert stopped["stop_reason"] == "user cancelled future follow-up"
    assert calls == ["Draft the implementation plan.", "Apply the approved plan."]


def test_workflow_run_executes_agent_steps_by_default(workflow_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_resolve_swarm_runner_command(
        *,
        runner: str | None,
        runner_model: str | None,
        runner_args: list[str] | tuple[str, ...],
        child_command: list[str] | tuple[str, ...],
        prompt_template: str,
    ) -> list[str]:
        seen["runner"] = runner
        seen["runner_model"] = runner_model
        seen["prompt"] = prompt_template
        return ["fake-runner", prompt_template]

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        text: bool,
        capture_output: bool,
        check: bool,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["cwd"] = cwd
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='{"verdict":"PASS","checklist":["done"],"missing":[]}',
            stderr="",
        )

    monkeypatch.setattr(mcp_server, "resolve_swarm_runner_command", fake_resolve_swarm_runner_command)
    monkeypatch.setattr("subprocess.run", fake_run)

    payload = mcp_server._run_owned_workflow(
        {
            "workflow": {
                "workflow_id": "owned-review-loop",
                "steps": [
                    {
                        "step_id": "agent_step",
                        "kind": "agent",
                        "prompt": "Inspect README.md and return a JSON verdict.",
                    }
                ],
            }
        }
    )

    assert payload["status"] == "success"
    assert payload["step_count"] == 1
    assert seen["runner"] == mcp_server._workflow_runner_profile()
    assert seen["runner_model"] == mcp_server._workflow_runner_model(
        mcp_server.build_default_registry(Path(__file__).resolve().parents[2]),
        role_id="general",
        workspace=Path(os.environ["CLAUDE_WORKSPACE_ROOT"]),
        runner=seen["runner"],
    )
    assert seen["cwd"].name == "workspace"
    assert "Inspect README.md" in seen["prompt"]


def test_workflow_run_uses_workspace_host_model_when_configured(
    workflow_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Path(os.environ["CLAUDE_WORKSPACE_ROOT"])
    settings_dir = workspace / ".lemoncrow"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(
        json.dumps({"models": {"hosts": {"claude": {"roles": {"*": "claude-opus-4.8"}}}}}),
        encoding="utf-8",
    )
    seen: dict[str, Any] = {}

    def fake_resolve_swarm_runner_command(
        *,
        runner: str | None,
        runner_model: str | None,
        runner_args: list[str] | tuple[str, ...],
        child_command: list[str] | tuple[str, ...],
        prompt_template: str,
    ) -> list[str]:
        seen["runner_model"] = runner_model
        return ["fake-runner", prompt_template]

    monkeypatch.setattr(mcp_server, "resolve_swarm_runner_command", fake_resolve_swarm_runner_command)
    monkeypatch.setattr(mcp_server, "_workflow_runner_profile", lambda: "claude")
    monkeypatch.setattr(
        "subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='{"verdict":"PASS","checklist":["done"],"missing":[]}',
            stderr="",
        ),
    )

    payload = mcp_server._run_owned_workflow(
        {
            "workflow": {
                "workflow_id": "owned-review-loop",
                "steps": [
                    {"step_id": "agent_step", "kind": "agent", "role_id": "code", "prompt": "Inspect README.md."}
                ],
            }
        }
    )

    assert payload["status"] == "success"
    assert seen["runner_model"] == "claude-opus-4-8"


def test_workflow_run_applies_explicit_owned_route(workflow_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    monkeypatch.setattr(
        mcp_server,
        "_select_owned_execution_route",
        lambda **_kwargs: SimpleNamespace(
            mode="explicit",
            provider="openai",
            model="gpt-4o",
            runner="codex",
            transport="openai",
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "execute_owned_prompt",
        lambda prompt, **kwargs: (
            seen.update({"prompt": prompt, "decision": kwargs["decision"]})
            or SimpleNamespace(
                output='{"verdict":"PASS","checklist":["done"],"missing":[]}',
                receipt=SimpleNamespace(
                    to_dict=lambda: {
                        "status": "done",
                        "mode": "explicit",
                        "selected_provider": "openai",
                        "selected_model": "gpt-4o",
                        "selected_runner": "codex",
                        "selected_transport": "openai",
                        "executed_provider": "openai",
                        "executed_model": "gpt-4o",
                        "executed_runner": "codex",
                        "executed_transport": "openai",
                        "request_id": "req-1",
                        "input_tokens": 21,
                        "output_tokens": 7,
                        "cache_read_input_tokens": 5,
                        "cache_write_input_tokens": 0,
                        "duration_seconds": 1.2,
                        "cost_usd": 0.0,
                        "rerouted": False,
                        "attempts": [],
                        "error": "",
                    },
                    executed_model="gpt-4o",
                    input_tokens=21,
                    output_tokens=7,
                    cache_read_input_tokens=5,
                    cache_write_input_tokens=0,
                    modeled_cache_read_input_tokens=0,
                    stable_prefix_hash="",
                    prefix_invalidated_reason="",
                    cache_evidence="none",
                    duration_seconds=1.2,
                    cost_usd=0.0,
                ),
            )
        ),
    )

    payload = mcp_server._run_owned_workflow(
        {
            "workflow": {
                "workflow_id": "owned-review-loop",
                "steps": [
                    {
                        "step_id": "agent_step",
                        "kind": "agent",
                        "prompt": "Inspect README.md and return a JSON verdict.",
                    }
                ],
            },
            "route": {
                "mode": "explicit",
                "provider": "openai",
                "model": "gpt-4o",
                "runner": "codex",
            },
        }
    )

    assert payload["status"] == "success"
    state = mcp_server._read_workspace_session_state()
    step_output = state["workflow"]["task_outputs"]["agent_step"]
    assert seen["decision"].runner == "codex"
    assert seen["decision"].transport == "openai"
    assert step_output["execution_receipt"]["executed_transport"] == "openai"


def test_workflow_run_auto_route_failure_does_not_fallback_to_native_subprocess(
    workflow_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.cross_vendor_routing.router import NoFeasibleRouteError

    monkeypatch.setattr(
        mcp_server,
        "_select_owned_execution_route",
        lambda **_kwargs: (_ for _ in ()).throw(NoFeasibleRouteError("no owned route")),
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("native subprocess should not run")),
    )

    payload = mcp_server._run_owned_workflow(
        {
            "workflow": {
                "workflow_id": "owned-review-loop",
                "steps": [
                    {
                        "step_id": "agent_step",
                        "kind": "agent",
                        "prompt": "Inspect README.md and return a JSON verdict.",
                    }
                ],
            },
            "route": {"mode": "auto"},
        }
    )

    assert payload["status"] == "failed"
    state = mcp_server._read_workspace_session_state()
    step_output = state["workflow"]["task_outputs"]["agent_step"]
    assert "owned route selection failed" in step_output["error"]
    assert step_output["execution_receipt"]["mode"] == "auto"
    assert step_output["execution_receipt"]["status"] == "failed"


def test_workflow_run_pauses_for_plan_review_and_resumes_on_approval(
    workflow_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_resolve_swarm_runner_command(
        *,
        runner: str | None,
        runner_model: str | None,
        runner_args: list[str] | tuple[str, ...],
        child_command: list[str] | tuple[str, ...],
        prompt_template: str,
    ) -> list[str]:
        return ["fake-runner", prompt_template]

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        text: bool,
        capture_output: bool,
        check: bool,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        prompt = command[-1]
        calls.append(prompt)
        if "Draft the implementation plan." in prompt:
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="plan ready", stderr="")
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="applied", stderr="")

    monkeypatch.setattr(mcp_server, "resolve_swarm_runner_command", fake_resolve_swarm_runner_command)
    monkeypatch.setattr("subprocess.run", fake_run)

    workflow = {
        "workflow_id": "review-gated",
        "steps": [
            {"step_id": "plan", "kind": "agent", "prompt": "Draft the implementation plan."},
            {
                "step_id": "execute",
                "kind": "agent",
                "prompt": "Apply the approved plan.",
                "requires_plan_review": True,
            },
        ],
    }

    paused = mcp_server._run_owned_workflow({"workflow": workflow})
    assert paused["status"] == "awaiting_review"
    assert paused["paused_step_id"] == "execute"

    resumed = mcp_server._run_owned_workflow({"resume": True, "plan_review": {"decision": "approve"}})
    assert resumed["status"] == "success"
    assert calls == ["Draft the implementation plan.", "Apply the approved plan."]
