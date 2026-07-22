"""ChatGPT-connector OAuth 2.1 shim — resource protection + full handshake.

Exercises ``mcp_oauth.create_protected_mcp_app`` the way a ChatGPT Developer-Mode
custom connector drives it: discover protected-resource metadata, register via
DCR, run the PKCE authorization-code flow gated by the pairing code, exchange for
a bearer token, and call ``/mcp``. Follows the FastAPI ``TestClient`` conventions
of ``test_mcp_http_transport.py``.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import stat
import urllib.parse
from pathlib import Path

from fastapi.testclient import TestClient

from lemoncrow.gateway.adapters.mcp_oauth import create_protected_mcp_app

_PAIRING_CODE = "unit-test-pair"
_REDIRECT_URI = "https://chatgpt.example.com/connector/callback"
_TUNNEL_HEADERS = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "tunnel.example.com"}
_TUNNEL_BASE = "https://tunnel.example.com"


def _app(state_path: Path) -> TestClient:
    return TestClient(create_protected_mcp_app(pairing_code=_PAIRING_CODE, state_path=state_path))


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _register(client: TestClient) -> str:
    resp = client.post("/register", json={"redirect_uris": [_REDIRECT_URI], "client_name": "ChatGPT"})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["client_id"])


def _authorize(client: TestClient, client_id: str, challenge: str, *, pairing_code: str) -> str:
    """Run GET + POST /authorize and return the issued authorization code."""
    query = {
        "client_id": client_id,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "state": "state-xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "",
        "resource": _TUNNEL_BASE,
    }
    get_resp = client.get("/authorize", params=query)
    assert get_resp.status_code == 200
    assert "pairing" in get_resp.text.lower()

    post_resp = client.post("/authorize", data={**query, "pairing_code": pairing_code}, follow_redirects=False)
    assert post_resp.status_code == 302, post_resp.text
    location = post_resp.headers["location"]
    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    assert parsed["state"] == ["state-xyz"]
    return parsed["code"][0]


def _exchange_code(client: TestClient, client_id: str, code: str, verifier: str) -> dict[str, object]:
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


# ── Resource protection ────────────────────────────────────────────────────────
def test_mcp_without_token_returns_401_with_resource_metadata(tmp_path: Path) -> None:
    resp = _app(tmp_path / "s.json").post(
        "/mcp",
        headers=_TUNNEL_HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert resp.status_code == 401
    www_auth = resp.headers["www-authenticate"]
    assert "resource_metadata=" in www_auth
    assert f"{_TUNNEL_BASE}/.well-known/oauth-protected-resource" in www_auth


def test_protected_resource_metadata_reflects_forwarded_headers(tmp_path: Path) -> None:
    client = _app(tmp_path / "s.json")
    for url in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        body = client.get(url, headers=_TUNNEL_HEADERS).json()
        assert body["resource"] == _TUNNEL_BASE
        assert body["authorization_servers"] == [_TUNNEL_BASE]
        assert body["bearer_methods_supported"] == ["header"]


def test_authorization_server_metadata_reflects_forwarded_headers(tmp_path: Path) -> None:
    body = _app(tmp_path / "s.json").get("/.well-known/oauth-authorization-server", headers=_TUNNEL_HEADERS).json()
    assert body["issuer"] == _TUNNEL_BASE
    assert body["authorization_endpoint"] == f"{_TUNNEL_BASE}/authorize"
    assert body["token_endpoint"] == f"{_TUNNEL_BASE}/token"
    assert body["registration_endpoint"] == f"{_TUNNEL_BASE}/register"
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint_auth_methods_supported"] == ["none"]
    assert body["grant_types_supported"] == ["authorization_code", "refresh_token"]


def test_authorization_server_metadata_served_on_all_aliases(tmp_path: Path) -> None:
    """Observed live: ChatGPT probes /.well-known/openid-configuration after the
    OAuth metadata fetches; RFC 8414 also allows a path-component form. All
    aliases must serve the exact canonical document (forwarded headers included)."""
    client = _app(tmp_path / "s.json")
    canonical = client.get("/.well-known/oauth-authorization-server", headers=_TUNNEL_HEADERS).json()
    assert canonical["issuer"] == _TUNNEL_BASE
    for alias in (
        "/.well-known/oauth-authorization-server/mcp",
        "/.well-known/openid-configuration",
        "/.well-known/openid-configuration/mcp",
    ):
        resp = client.get(alias, headers=_TUNNEL_HEADERS)
        assert resp.status_code == 200, alias
        assert resp.json() == canonical, alias


# ── Dynamic client registration ────────────────────────────────────────────────
def test_dcr_happy_path(tmp_path: Path) -> None:
    resp = _app(tmp_path / "s.json").post(
        "/register", json={"redirect_uris": [_REDIRECT_URI], "client_name": "ChatGPT"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"]
    assert "client_secret" not in body
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"] == [_REDIRECT_URI]
    assert isinstance(body["client_id_issued_at"], int)


def test_dcr_rejects_non_https_redirect_uri(tmp_path: Path) -> None:
    resp = _app(tmp_path / "s.json").post("/register", json={"redirect_uris": ["http://evil.example.com/cb"]})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


def test_dcr_allows_loopback_http(tmp_path: Path) -> None:
    resp = _app(tmp_path / "s.json").post("/register", json={"redirect_uris": ["http://127.0.0.1:1234/cb"]})
    assert resp.status_code == 201


# ── Full happy-path handshake ──────────────────────────────────────────────────
def test_full_handshake_reaches_mcp(tmp_path: Path) -> None:
    client = _app(tmp_path / "s.json")
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    code = _authorize(client, client_id, challenge, pairing_code=_PAIRING_CODE)
    tokens = _exchange_code(client, client_id, code, verifier)
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 2_592_000
    access_token = str(tokens["access_token"])

    resp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["result"]["serverInfo"]["name"]


# ── Negative + edge cases ──────────────────────────────────────────────────────
def test_wrong_pairing_code_rerenders_form_without_redirect(tmp_path: Path) -> None:
    client = _app(tmp_path / "s.json")
    client_id = _register(client)
    _, challenge = _pkce_pair()
    resp = client.post(
        "/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "state": "state-xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "",
            "resource": _TUNNEL_BASE,
            "pairing_code": "wrong-code",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "location" not in resp.headers
    assert "incorrect pairing code" in resp.text.lower()


def test_authorization_code_is_single_use(tmp_path: Path) -> None:
    client = _app(tmp_path / "s.json")
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    code = _authorize(client, client_id, challenge, pairing_code=_PAIRING_CODE)
    _exchange_code(client, client_id, code, verifier)  # first use succeeds
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_bad_pkce_verifier_rejected(tmp_path: Path) -> None:
    client = _app(tmp_path / "s.json")
    client_id = _register(client)
    _, challenge = _pkce_pair()
    code = _authorize(client, client_id, challenge, pairing_code=_PAIRING_CODE)
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "code_verifier": "not-the-right-verifier",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_unsupported_grant_type(tmp_path: Path) -> None:
    resp = _app(tmp_path / "s.json").post("/token", data={"grant_type": "password"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


def test_refresh_token_rotation_kills_old_token(tmp_path: Path) -> None:
    client = _app(tmp_path / "s.json")
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    code = _authorize(client, client_id, challenge, pairing_code=_PAIRING_CODE)
    tokens = _exchange_code(client, client_id, code, verifier)
    old_refresh = str(tokens["refresh_token"])

    rotated = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": old_refresh})
    assert rotated.status_code == 200, rotated.text
    new_tokens = rotated.json()
    assert new_tokens["access_token"] != tokens["access_token"]
    assert new_tokens["refresh_token"] != old_refresh

    # Old refresh token is now dead (single-use rotation).
    replay = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": old_refresh})
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"

    # The freshly rotated access token still authorizes /mcp.
    mcp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
        json={"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}},
    )
    assert mcp.status_code == 200


def test_authorize_redirect_uri_mismatch_is_400_no_redirect(tmp_path: Path) -> None:
    client = _app(tmp_path / "s.json")
    client_id = _register(client)
    _, challenge = _pkce_pair()
    resp = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://attacker.example.com/steal",
            "response_type": "code",
            "state": "s",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "location" not in resp.headers


# ── Persistence ────────────────────────────────────────────────────────────────
def test_state_persists_tokens_and_is_0600(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    client = _app(state_path)
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    code = _authorize(client, client_id, challenge, pairing_code=_PAIRING_CODE)
    tokens = _exchange_code(client, client_id, code, verifier)
    access_token = str(tokens["access_token"])

    # State file exists and is owner-only.
    assert state_path.exists()
    assert stat.S_IMODE(os.stat(state_path).st_mode) == 0o600

    # A fresh app instance over the same state file honors the old bearer token.
    fresh = _app(state_path)
    resp = fresh.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["serverInfo"]["name"]
