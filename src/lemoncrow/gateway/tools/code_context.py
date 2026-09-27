"""Shared code-context engine cache and request-scoped runtime helpers."""

from __future__ import annotations

import contextlib
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lemoncrow.gateway.tools.state import NO_CODE_ENGINE_OVERRIDE, request_code_engine_override
from lemoncrow.gateway.tools.workspace import workspace_root

# Thread-local used by renderers to inspect the engine active for the current
# tool call without threading it through every response branch.
code_engine_for_current_call: threading.local = threading.local()

# Process-level engine cache keyed by resolved repo path. A daemon can serve an
# arbitrary number of projects while keeping only a bounded hot set in memory.
code_engine_cache: OrderedDict[str, Any] = OrderedDict()
code_engine_cache_lock: threading.Lock = threading.Lock()
scoped_context_cache: OrderedDict[str, Any] = OrderedDict()
scoped_context_cache_lock: threading.RLock = threading.RLock()


def code_engine_cache_limit() -> int:
    raw = os.environ.get("LEMONCROW_CODE_ENGINE_CACHE_MAX", "8").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 8
    return max(1, min(value, 64))


def _resolved_repo_root(repo_root: str, workspace: Path | None) -> Path:
    root = Path(repo_root)
    base = workspace if workspace is not None else workspace_root()
    return (root if root.is_absolute() else base / root).resolve()


def code_context_engine(repo_root: str = ".", *, workspace: Path | None = None) -> Any:
    """Return the request override or cached CodeContextEngine for *repo_root*."""
    override = getattr(request_code_engine_override, "value", NO_CODE_ENGINE_OVERRIDE)
    if override is not NO_CODE_ENGINE_OVERRIDE:
        return override

    from lemoncrow.pro.capabilities.code_context import CodeContextEngine

    resolved = _resolved_repo_root(repo_root, workspace)
    cache_key = str(resolved)
    evicted: Any | None = None
    evicted_key: str | None = None
    with code_engine_cache_lock:
        engine = code_engine_cache.get(cache_key)
        if engine is not None:
            code_engine_cache.move_to_end(cache_key)
            return engine
        engine = CodeContextEngine(resolved)
        code_engine_cache[cache_key] = engine
        while len(code_engine_cache) > code_engine_cache_limit():
            evicted_key, evicted = code_engine_cache.popitem(last=False)
            break
    if evicted_key is not None:
        # ScopedContextCapability owns a strong engine reference. Drop the
        # matching wrapper too or it would defeat the engine LRU.
        with scoped_context_cache_lock:
            scoped_context_cache.pop(evicted_key, None)
    if evicted is not None:
        with contextlib.suppress(Exception):
            evicted.close()
    return engine


def scoped_context_capability(
    repo_root: str = ".",
    *,
    workspace: Path | None = None,
    engine_factory: Callable[[str], Any] | None = None,
) -> Any:
    """Return a cached ScopedContextCapability unless a request override is active."""
    from lemoncrow.pro.capabilities.scoped_context import ScopedContextCapability

    override = getattr(request_code_engine_override, "value", NO_CODE_ENGINE_OVERRIDE)
    if override is not NO_CODE_ENGINE_OVERRIDE:
        # A request-scoped hosted engine must never enter the process-global
        # standalone cache where a later tenant could reuse it.
        return ScopedContextCapability(override)

    resolved = _resolved_repo_root(repo_root, workspace)
    cache_key = str(resolved)
    with scoped_context_cache_lock:
        capability = scoped_context_cache.get(cache_key)
        if capability is not None:
            scoped_context_cache.move_to_end(cache_key)
            return capability
        factory = engine_factory or (lambda target: code_context_engine(target, workspace=resolved))
        capability = ScopedContextCapability(factory(str(resolved)))
        scoped_context_cache[cache_key] = capability
        while len(scoped_context_cache) > code_engine_cache_limit():
            scoped_context_cache.popitem(last=False)
        return capability


def workspace_code_router(
    repo_root: str = ".",
    *,
    workspace: Path | None = None,
    engine_factory: Callable[[str], Any] | None = None,
) -> Any:
    """Build a multi-workspace code router using the canonical engine cache."""
    from lemoncrow.pro.capabilities.code_context.workspace_router import WorkspaceCodeRouter

    root = Path(repo_root)
    base = workspace if workspace is not None else workspace_root()
    resolved = root if root.is_absolute() else base / root
    factory = engine_factory or (lambda target: code_context_engine(target, workspace=base))
    return WorkspaceCodeRouter(
        repo_root=resolved,
        engine_factory=lambda target_root: factory(str(target_root)),
    )


def reset_code_context_cache() -> None:
    """Close and clear all cached standalone code engines/capabilities."""
    with code_engine_cache_lock:
        stale_engines = list(code_engine_cache.values())
        code_engine_cache.clear()
    for engine in stale_engines:
        with contextlib.suppress(Exception):
            engine.close()
    with scoped_context_cache_lock:
        scoped_context_cache.clear()


__all__ = [
    "code_context_engine",
    "code_engine_cache",
    "code_engine_cache_limit",
    "code_engine_cache_lock",
    "code_engine_for_current_call",
    "reset_code_context_cache",
    "scoped_context_cache",
    "scoped_context_cache_lock",
    "scoped_context_capability",
    "workspace_code_router",
]
