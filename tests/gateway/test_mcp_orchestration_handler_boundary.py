from __future__ import annotations

import inspect
from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_orchestration


def test_mcp_server_reexports_extracted_orchestration_handlers() -> None:
    assert mcp_server.tool_agent is tools_orchestration.tool_agent
    assert mcp_server.tool_workflow is tools_orchestration.tool_workflow
    source = inspect.getsource(mcp_server)
    assert "def tool_agent(" not in source
    assert "def tool_workflow(" not in source


def test_orchestration_hooks_late_bind_agent_runtime(monkeypatch) -> None:
    def fake_select(*_args, **_kwargs):
        return object()

    def fake_execute(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(mcp_server, "select_owned_route", fake_select)
    monkeypatch.setattr(mcp_server, "execute_owned_prompt", fake_execute)
    hooks = mcp_server._orchestration_handler_hooks()
    assert hooks.select_owned_route is fake_select
    assert hooks.execute_owned_prompt is fake_execute


def test_orchestration_hooks_late_bind_workflow_runtime(monkeypatch) -> None:
    def fake_run(args):
        return {"seen": args}

    monkeypatch.setattr(mcp_server, "_run_owned_workflow", fake_run)
    hooks = mcp_server._orchestration_handler_hooks()
    assert hooks.run_owned_workflow is fake_run
    assert hooks.run_owned_workflow({"x": 1}) == {"seen": {"x": 1}}


def test_orchestration_module_does_not_import_mcp_server() -> None:
    source = Path(tools_orchestration.__file__).read_text(encoding="utf-8")
    assert "mcp_server" not in source
