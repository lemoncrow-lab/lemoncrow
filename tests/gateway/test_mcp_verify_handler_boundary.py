from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_verify
from lemoncrow.gateway.tools.registry import tool_spec


def test_mcp_server_reexports_extracted_verify_handler() -> None:
    assert mcp_server.tool_run_rubric_gate is tools_verify.tool_run_rubric_gate


def test_registry_uses_extracted_verify_handler_object() -> None:
    spec = tool_spec("verify")
    assert spec is not None
    assert spec["handler"] is tools_verify.tool_run_rubric_gate


def test_verify_handler_module_does_not_import_mcp_server() -> None:
    source = inspect.getsource(tools_verify)
    assert "from lemoncrow.gateway.adapters import mcp_server" not in source
    assert "from lemoncrow.gateway.adapters.mcp_server import" not in source


def test_verify_hook_factory_late_binds_runtime(monkeypatch) -> None:
    marker = object()

    def runtime():
        return marker

    monkeypatch.setattr(mcp_server, "_runtime", runtime)
    hooks = mcp_server._verify_handler_hooks()
    assert hooks.runtime is runtime
    assert hooks.runtime() is marker
