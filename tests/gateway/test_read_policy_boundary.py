from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import read_policy


def test_mcp_reexports_canonical_read_policy_helpers() -> None:
    assert mcp_server._binary_read_message is read_policy.binary_read_message
    assert mcp_server._suggest_paths_for_missing is read_policy.suggest_paths_for_missing
    assert mcp_server._read_inline_budget_bytes is read_policy.read_inline_budget_bytes
    assert mcp_server._read_batch_budget_bytes is read_policy.read_batch_budget_bytes
    assert mcp_server._batch_entry_bytes is read_policy.batch_entry_bytes


def test_summary_wrapper_supplies_live_mcp_dependencies() -> None:
    source = inspect.getsource(mcp_server._read_summary_response)
    assert "_canonical_read_summary_response" in source
    assert "semantic_memory_root=_lemoncrow_root()" in source
    assert "render_outline=_render_read_outline_md" in source


def test_batch_budget_wrapper_uses_live_smart_read() -> None:
    source = inspect.getsource(mcp_server._apply_batch_read_budget)
    assert "_canonical_apply_batch_read_budget" in source
    assert "outline_reader=_smart_read_single" in source


def test_read_policy_has_no_mcp_adapter_dependency() -> None:
    source = inspect.getsource(read_policy)
    assert "gateway.adapters" not in source
    assert "mcp_server" not in source
