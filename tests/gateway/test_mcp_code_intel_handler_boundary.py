from __future__ import annotations

import inspect
from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_code_intel


def test_mcp_server_reexports_extracted_code_intel_handlers() -> None:
    for name in ("tool_graph", "tool_pattern", "tool_index", "tool_blame", "tool_cache", "tool_relations"):
        assert getattr(mcp_server, name) is getattr(tools_code_intel, name)
        assert f"def {name}(" not in inspect.getsource(mcp_server)


def test_code_intel_handler_hooks_are_late_bound(monkeypatch) -> None:
    marker = object()

    def fake_graph(**_kwargs):
        return {"marker": marker}

    monkeypatch.setattr(mcp_server, "_op_graph", fake_graph)
    hooks = mcp_server._code_intel_handler_hooks()
    assert hooks.graph is fake_graph
    assert hooks.graph()["marker"] is marker


def test_relations_wrapper_preserves_symbol_routing(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_callers(**kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_op_callers", fake_callers)
    out = tools_code_intel.tool_relations({"symbol": "pkg.Type.fn", "kind": "callers", "depth": 2, "limit": 7})
    assert out == {"ok": True}
    assert seen == {"qualified_name": "pkg.Type.fn", "depth": 2, "limit": 7}


def test_code_intel_module_does_not_import_mcp_server() -> None:
    source = Path(tools_code_intel.__file__).read_text(encoding="utf-8")
    assert "mcp_server" not in source
