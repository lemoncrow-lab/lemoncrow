"""The unauthenticated OAuth surface must not grow without bound."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_dynamic_client_registration_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.adapters import mcp_oauth

    monkeypatch.setattr(mcp_oauth, "MAX_DCR_CLIENTS", 3)
    store = mcp_oauth._OAuthStore(tmp_path / "oauth.json")
    operator = store.ensure_user_client(["https://operator.example/callback"])
    ids = [
        store.register_client(
            redirect_uris=["https://chat.example/callback"], client_name=None, grant_types=[], response_types=[]
        )["client_id"]
        for _ in range(6)
    ]
    assert store.get_client(operator["client_id"]) is not None, "an operator-minted client must never be evicted"
    assert [store.get_client(client_id) is not None for client_id in ids] == [False] * 3 + [True] * 3


def test_expired_access_tokens_are_pruned_on_issue(tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_oauth

    store = mcp_oauth._OAuthStore(tmp_path / "oauth.json")
    stale, _ = store.issue_tokens("client")
    key = mcp_oauth._sha256_hex(stale)
    store._access_tokens[key]["expires_at"] = 0.0
    store.issue_tokens("client")
    assert key not in store._access_tokens


def test_access_token_resolves_to_durable_client_identity(tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_oauth

    store = mcp_oauth._OAuthStore(tmp_path / "oauth.json")
    access, _refresh = store.issue_tokens("client-a")
    assert store.access_token_client_id(access) == "client-a"
    assert store.verify_access_token(access) is True
    assert store.access_token_client_id("not-a-token") is None
