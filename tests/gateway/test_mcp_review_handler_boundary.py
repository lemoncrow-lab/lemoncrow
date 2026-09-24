from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_review
from lemoncrow.gateway.tools.registry import tool_spec


def test_mcp_server_reexports_extracted_review_handlers() -> None:
    assert mcp_server.tool_review_rationale is tools_review.tool_review_rationale
    assert mcp_server.tool_review_evidence is tools_review.tool_review_evidence
    assert mcp_server.tool_review_feedback_addressed is tools_review.tool_review_feedback_addressed


def test_registry_uses_extracted_review_handler_objects() -> None:
    for name, handler in (
        ("review_rationale", tools_review.tool_review_rationale),
        ("review_evidence", tools_review.tool_review_evidence),
        ("review_feedback_addressed", tools_review.tool_review_feedback_addressed),
    ):
        spec = tool_spec(name)
        assert spec is not None
        assert spec["handler"] is handler


def test_review_handler_module_does_not_import_mcp_server() -> None:
    source = inspect.getsource(tools_review)
    assert "from lemoncrow.gateway.adapters import mcp_server" not in source
    assert "from lemoncrow.gateway.adapters.mcp_server import" not in source


def test_review_hook_factory_late_binds_legacy_monkeypatches(monkeypatch) -> None:
    def session() -> tuple[str, str]:
        return "sid-x", "claude"

    def model() -> str:
        return "model-x"

    monkeypatch.setattr(mcp_server, "_resolved_host_session", session)
    monkeypatch.setattr(mcp_server, "_get_mcp_model", model)
    hooks = mcp_server._review_handler_hooks()
    assert hooks.resolved_host_session is session
    assert hooks.get_mcp_model is model
