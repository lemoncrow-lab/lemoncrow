from __future__ import annotations

import inspect
from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.pro.capabilities.memory import runtime as memory_runtime
from lemoncrow.pro.capabilities.memory.redaction import redact_memory_input
from lemoncrow.pro.capabilities.tool_supervision import symbol_edit


def test_memory_runtime_reuses_store_per_thread_and_root(tmp_path: Path) -> None:
    root = tmp_path / "store"
    first = memory_runtime.memory_store(root)
    second = memory_runtime.memory_store(root)
    assert first is second


def test_mcp_memory_store_uses_canonical_runtime(monkeypatch, tmp_path: Path) -> None:
    seen: list[Path] = []
    sentinel = object()

    def _memory_store(root: Path):
        seen.append(root)
        return sentinel

    monkeypatch.setattr(mcp_server, "memory_store", _memory_store)
    monkeypatch.setattr(mcp_server, "_lemoncrow_root", lambda: tmp_path)
    assert mcp_server._memory_store() is sentinel
    assert seen == [tmp_path]


def test_mcp_editable_block_helper_delegates_to_memory_service(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class _Service:
        def upsert_editable_block(self, **kwargs):
            seen.update(kwargs)
            return {"id": "mem-1", "version": 1, "arbitration": {"op": "ADD", "reason": "test"}}

    monkeypatch.setattr(mcp_server, "_memory_service", lambda: _Service())
    result = mcp_server._memory_upsert_block(
        agent_id="shared",
        label="edits/sym",
        value="trace",
        metadata={"symbol_id": "sym"},
    )
    assert result["id"] == "mem-1"
    assert seen["agent_id"] == "shared"
    assert seen["label"] == "edits/sym"
    assert seen["metadata"] == {"symbol_id": "sym"}
    assert seen["field_redactor"] is mcp_server._redact_memory_input


def test_symbol_edit_no_longer_depends_on_mcp_server() -> None:
    source = inspect.getsource(symbol_edit.record_symbol_edit_memory)
    module_source = inspect.getsource(symbol_edit)
    assert "mcp_server" not in module_source
    assert "memory_service().upsert_editable_block" in source
    assert symbol_edit.redact_memory_input is redact_memory_input
