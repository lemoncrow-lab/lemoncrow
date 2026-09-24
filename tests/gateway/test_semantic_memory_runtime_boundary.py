from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import semantic_memory


def test_mcp_reexports_canonical_semantic_memory_cache() -> None:
    assert mcp_server._semantic_file_memory is semantic_memory.semantic_file_memory
    assert mcp_server._semantic_file_memory_cache is semantic_memory.semantic_file_memory_cache
    assert mcp_server._semantic_file_memory_lock is semantic_memory.semantic_file_memory_lock


def test_semantic_memory_runtime_has_no_mcp_adapter_dependency() -> None:
    source = inspect.getsource(semantic_memory)
    assert "gateway.adapters" not in source
    assert "mcp_server" not in source


def test_semantic_memory_ownership_no_longer_lives_in_mcp_server() -> None:
    source = inspect.getsource(mcp_server)
    assert "def _semantic_file_memory(" not in source
    assert "_semantic_file_memory_cache: dict" not in source
