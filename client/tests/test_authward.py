from __future__ import annotations

from typing import Any

import pytest
from lemoncrow_client.authward import DEVICE_CODE_GRANT, discover_authward
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.transport import Response


class FakeTransport:
    def __init__(self, url: str, responses: list[Response]) -> None:
        self.url = url
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def get(self, path: str, **_kwargs: Any) -> Response:
        self.calls.append(("GET", path, {}))
        return self.responses.pop(0)

    def post_form(self, path: str, *, form: dict[str, str], **_kwargs: Any) -> Response:
        self.calls.append(("POST", path, dict(form)))
        return self.responses.pop(0)


def _server_discovery() -> Response:
    return Response(
        200,
        {
            "authentication": {
                "authorization_server": "https://auth.example",
                "resource": "https://api.lemoncrow.com",
                "client_id": "lemoncrow-cli",
                "scopes": ["openid", "email", "offline_access", "lemoncrow:access"],
            }
        },
    )


def _oidc_discovery(*, token_endpoint: str = "https://auth.example/oauth/token") -> Response:
    return Response(
        200,
        {
            "issuer": "https://auth.example",
            "device_authorization_endpoint": "https://auth.example/oauth/device",
            "token_endpoint": token_endpoint,
            "revocation_endpoint": "https://auth.example/oauth/revoke",
        },
    )


def test_direct_authward_device_refresh_and_revoke_use_only_authward_origin() -> None:
    server = FakeTransport("https://api.lemoncrow.com", [_server_discovery()])
    auth = FakeTransport(
        "https://auth.example",
        [
            _oidc_discovery(),
            Response(
                200,
                {
                    "device_code": "dev-1",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://auth.example/device",
                    "expires_in": 600,
                    "interval": 5,
                },
            ),
            Response(400, {"error": "authorization_pending"}),
            Response(200, {"access_token": "authward-access", "refresh_token": "authward-refresh"}),
            Response(200, {"access_token": "authward-access-2", "refresh_token": "authward-refresh-2"}),
            Response(200, {}),
            Response(200, {}),
        ],
    )

    def factory(url: str, *, timeout_s: float):
        assert url == "https://auth.example"
        assert timeout_s == 7
        return auth

    client = discover_authward(server, timeout_s=7, transport_factory=factory)  # type: ignore[arg-type]

    started = client.start_device()
    assert started["device_code"] == "dev-1"

    with pytest.raises(ClientError) as pending:
        client.poll_device("dev-1")
    assert pending.value.server_code == "authorization_pending"
    assert pending.value.retryable is True

    issued = client.poll_device("dev-1")
    assert issued["access_token"] == "authward-access"

    access, refresh = client.refresh("authward-refresh")
    assert (access, refresh) == ("authward-access-2", "authward-refresh-2")
    assert client.revoke_access(access) is True
    assert client.revoke_refresh(refresh) is True

    assert server.calls == [("GET", "/.well-known/lemoncrow-server.json", {})]
    assert auth.calls[0] == ("GET", "/.well-known/openid-configuration", {})
    assert auth.calls[1] == (
        "POST",
        "https://auth.example/oauth/device",
        {
            "client_id": "lemoncrow-cli",
            "scope": "openid email offline_access lemoncrow:access",
            "resource": "https://api.lemoncrow.com",
        },
    )
    assert auth.calls[2] == (
        "POST",
        "https://auth.example/oauth/token",
        {
            "grant_type": DEVICE_CODE_GRANT,
            "device_code": "dev-1",
            "client_id": "lemoncrow-cli",
            "resource": "https://api.lemoncrow.com",
        },
    )
    assert auth.calls[4][2]["grant_type"] == "refresh_token"
    assert auth.calls[4][2]["refresh_token"] == "authward-refresh"
    assert auth.calls[5][1] == "https://auth.example/oauth/revoke"


def test_authward_discovery_refuses_foreign_token_endpoint_before_credentials_leave_origin() -> None:
    server = FakeTransport("https://api.lemoncrow.com", [_server_discovery()])
    auth = FakeTransport(
        "https://auth.example",
        [_oidc_discovery(token_endpoint="https://attacker.example/token")],
    )

    with pytest.raises(ClientError) as caught:
        discover_authward(
            server,  # type: ignore[arg-type]
            timeout_s=5,
            transport_factory=lambda _url, *, timeout_s: auth,  # type: ignore[arg-type]
        )

    assert caught.value.code is ErrorCode.NOT_CONFIGURED
    assert "another origin" in caught.value.message
    assert auth.calls == [("GET", "/.well-known/openid-configuration", {})]


def test_authward_discovery_requires_exact_issuer_match() -> None:
    server = FakeTransport("https://api.lemoncrow.com", [_server_discovery()])
    auth = FakeTransport(
        "https://auth.example",
        [
            Response(
                200,
                {
                    "issuer": "https://other.example",
                    "device_authorization_endpoint": "https://auth.example/device",
                    "token_endpoint": "https://auth.example/token",
                },
            )
        ],
    )

    with pytest.raises(ClientError, match="issuer does not match"):
        discover_authward(
            server,  # type: ignore[arg-type]
            timeout_s=5,
            transport_factory=lambda _url, *, timeout_s: auth,  # type: ignore[arg-type]
        )


def test_authward_refresh_keeps_refresh_token_when_provider_does_not_rotate_it() -> None:
    server = FakeTransport("https://api.lemoncrow.com", [_server_discovery()])
    auth = FakeTransport(
        "https://auth.example",
        [
            _oidc_discovery(),
            Response(200, {"access_token": "authward-access-2"}),
        ],
    )
    client = discover_authward(
        server,  # type: ignore[arg-type]
        timeout_s=5,
        transport_factory=lambda _url, *, timeout_s: auth,  # type: ignore[arg-type]
    )

    assert client.refresh("refresh-stable") == ("authward-access-2", "refresh-stable")
