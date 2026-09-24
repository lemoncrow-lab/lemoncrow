from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import accounting


def test_mcp_accounting_wrapper_uses_canonical_runtime() -> None:
    import inspect

    source = inspect.getsource(mcp_server._process_tool_accounting)
    assert "_canonical_process_tool_accounting" in source
    assert accounting.ToolAccountingHooks is not None
