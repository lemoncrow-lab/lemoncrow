from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import code_context


def test_mcp_reexports_canonical_code_context_cache_state() -> None:
    assert mcp_server._code_engine_cache is code_context.code_engine_cache
    assert mcp_server._code_engine_cache_lock is code_context.code_engine_cache_lock
    assert mcp_server._scoped_context_cache is code_context.scoped_context_cache
    assert mcp_server._scoped_context_cache_lock is code_context.scoped_context_cache_lock
    assert mcp_server._code_engine_for_current_call is code_context.code_engine_for_current_call


def test_mcp_keeps_only_live_workspace_wrappers() -> None:
    engine_source = inspect.getsource(mcp_server._code_context_engine)
    scoped_source = inspect.getsource(mcp_server._scoped_context_capability)
    router_source = inspect.getsource(mcp_server._workspace_code_router)
    assert "_canonical_code_context_engine" in engine_source
    assert "_workspace_root()" in engine_source
    assert "engine_factory=lambda target: _code_context_engine(target)" in scoped_source
    assert "engine_factory=lambda target: _code_context_engine(target)" in router_source


def test_canonical_code_context_runtime_has_no_mcp_adapter_dependency() -> None:
    source = inspect.getsource(code_context)
    assert "gateway.adapters" not in source
    assert "mcp_server" not in source


def test_mcp_cache_reset_delegates_to_canonical_runtime() -> None:
    source = inspect.getsource(mcp_server._reset_runtime_cache_for_testing)
    assert "_reset_code_context_cache()" in source
