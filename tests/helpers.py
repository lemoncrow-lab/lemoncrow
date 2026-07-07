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
    """Simulate a signed-in OAuth session on a paid plan (no disk, no network)."""
    from atelier.core.capabilities.licensing import entitlements, store

    monkeypatch.setattr(store, "load_auth_token", lambda: "test-session-token")  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        store,
        "load_auth_user",
        lambda: {"user_id": "u_test", "email": email, "plan": plan},
    )
    entitlements.reload()


def deny_oauth(monkeypatch: object) -> None:
    """Force the signed-out state regardless of the developer's real ~/.atelier."""
    from atelier.core.capabilities.licensing import entitlements, store

    monkeypatch.setattr(store, "load_auth_token", lambda: None)  # type: ignore[attr-defined]
    entitlements.reload()


@functools.cache
def init_store_at(root_str: str) -> None:
    """Initialize atelier at *root_str*. Cached so repeated inits for the
    same path are no-ops (saves ~1-2 s per redundant call).

    Caller must pass a **string** (not a Path) so lru_cache can hash it.
    """
    from atelier.infra.storage.factory import create_store

    create_store(Path(root_str)).init()
