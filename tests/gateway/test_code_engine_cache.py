from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lemoncrow.gateway.adapters import mcp_server


def test_code_engine_cache_is_per_project_lru_and_closes_evicted_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import lemoncrow.pro.capabilities.code_context as code_context

    class FakeEngine:
        def __init__(self, repo_root: str | Path) -> None:
            self.repo_root = Path(repo_root).resolve()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(code_context, "CodeContextEngine", FakeEngine)
    monkeypatch.setattr(mcp_server, "_workspace_root", lambda: tmp_path)
    monkeypatch.setenv("LEMONCROW_CODE_ENGINE_CACHE_MAX", "2")
    with mcp_server._code_engine_cache_lock:
        mcp_server._code_engine_cache.clear()

    roots = [tmp_path / name for name in ("one", "two", "three")]
    for root in roots:
        root.mkdir()

    one = mcp_server._code_context_engine(str(roots[0]))
    two = mcp_server._code_context_engine(str(roots[1]))
    two_key = str(roots[1].resolve())
    with mcp_server._scoped_context_cache_lock:
        mcp_server._scoped_context_cache[two_key] = object()
    # Touch one so two becomes the least-recently-used engine.
    assert mcp_server._code_context_engine(str(roots[0])) is one
    three = mcp_server._code_context_engine(str(roots[2]))

    assert one.closed is False
    assert two.closed is True
    assert three.closed is False
    assert list(mcp_server._code_engine_cache) == [str(roots[0].resolve()), str(roots[2].resolve())]
    assert two_key not in mcp_server._scoped_context_cache

    with mcp_server._code_engine_cache_lock:
        survivors: list[Any] = list(mcp_server._code_engine_cache.values())
        mcp_server._code_engine_cache.clear()
    for engine in survivors:
        engine.close()


def test_code_engine_cache_limit_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_CODE_ENGINE_CACHE_MAX", "0")
    assert mcp_server._code_engine_cache_limit() == 1
    monkeypatch.setenv("LEMONCROW_CODE_ENGINE_CACHE_MAX", "999")
    assert mcp_server._code_engine_cache_limit() == 64
    monkeypatch.setenv("LEMONCROW_CODE_ENGINE_CACHE_MAX", "garbage")
    assert mcp_server._code_engine_cache_limit() == 8
