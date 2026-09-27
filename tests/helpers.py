"""Shared test helpers - reusable across test files without conftest import hacks."""

from __future__ import annotations

import functools
from pathlib import Path


def grant_oauth_pro(
    monkeypatch: object,
    *,
    plan: str = "pro",
    email: str = "dev@example.com",
) -> None:
    """Legacy test compatibility: local capabilities are always enabled."""
    del monkeypatch, plan, email


def python_script_with_development_cap(script: Path) -> list[str]:
    """Legacy helper name: run the hook script directly in the local runtime."""
    import sys

    return [sys.executable, str(script)]


def deny_oauth(monkeypatch: object) -> None:
    """Legacy test compatibility: local capability access has no account state."""
    del monkeypatch


@functools.cache
def init_store_at(root_str: str) -> None:
    """Initialize lemoncrow at *root_str*. Cached so repeated inits for the
    same path are no-ops (saves ~1-2 s per redundant call).

    Caller must pass a **string** (not a Path) so lru_cache can hash it.
    """
    from lemoncrow.infra.storage.factory import create_store

    create_store(Path(root_str)).init()
