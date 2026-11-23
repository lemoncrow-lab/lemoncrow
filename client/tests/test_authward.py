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


def _server_discovery(*, product_scopes: tuple[str, ...] = ()) -> Response:
    return Response(
        200,
        {
            "authentication": {
                "authorization_server": "https://auth.example",
                "resource": "https://api.lemoncrow.com",
                "client_id": "lemoncrow-cli",
                "scopes": ["openid", "email", "offline_access", "lemoncrow:access", *product_scopes],
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


def test_isolated_lemoncrow_issuer_discovers_nested_device_and_token_endpoints() -> None:
    issuer = "https://auth.olaryn.com/api/realms/lemoncrow/auth"
    expected_endpoint = issuer + "/device/code"
    server = FakeTransport(
        "https://api.lemoncrow.com",
        [
            Response(
                200,
                {
                    "authentication": {
                        "authorization_server": issuer,
                        "client_id": "isolated-lemoncrow-cli",
                        "resource": "https://api.lemoncrow.com",
                        "scopes": ["openid", "profile", "email", "offline_access", "lemoncrow:access"],
                    }
                },
            )
        ],
    )
    auth = FakeTransport(
        issuer,
        [
            Response(
                200,
                {
                    "issuer": issuer,
                    "device_authorization_endpoint": expected_endpoint,
                    "token_endpoint": issuer + "/oauth2/token",
                    "revocation_endpoint": issuer + "/oauth2/revoke",
                },
            ),
            Response(
                200,
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://auth.olaryn.com/realms/lemoncrow/device",
                },
            ),
        ],
    )

    def factory(url: str, *, timeout_s: float):
        assert url == issuer
        return auth

    client = discover_authward(server, timeout_s=5, transport_factory=factory)  # type: ignore[arg-type]
    assert client.start_device()["user_code"] == "ABCD-EFGH"
    assert auth.calls == [
        ("GET", "/.well-known/openid-configuration", {}),
        (
            "POST",
            expected_endpoint,
            {
                "client_id": "isolated-lemoncrow-cli",
                "scope": "openid profile email offline_access lemoncrow:access",
                "resource": "https://api.lemoncrow.com",
            },
        ),
    ]


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


def test_one_sign_in_names_every_resource_and_each_token_request_names_one() -> None:
    server = FakeTransport("https://api.lemoncrow.com", [_server_discovery()])
    started = {"device_code": "dev-1", "user_code": "ABCD-EFGH", "verification_uri": "https://auth.example/device"}
    auth = FakeTransport(
        "https://auth.example",
        [
            _oidc_discovery(),
            Response(200, started),
            Response(200, {"access_token": "api-access", "refresh_token": "refresh-2"}),
            Response(200, {"access_token": "server-access", "refresh_token": "refresh-3"}),
        ],
    )
    client = discover_authward(
        server,  # type: ignore[arg-type]
        timeout_s=5,
        transport_factory=lambda _url, *, timeout_s: auth,  # type: ignore[arg-type]
    )

    servers = ["https://acme.workspaces.lemoncrow.com", "https://api.lemoncrow.com", "https://beta.lemoncrow.com"]
    client.start_device(servers)
    assert client.refresh("refresh-1") == ("api-access", "refresh-2")
    resource = "https://acme.workspaces.lemoncrow.com"
    assert client.refresh("refresh-2", resource=resource) == ("server-access", "refresh-3")

    named = ["https://api.lemoncrow.com", "https://acme.workspaces.lemoncrow.com", "https://beta.lemoncrow.com"]
    assert auth.calls[1][2]["resource"] == named
    assert auth.calls[2][2]["resource"] == "https://api.lemoncrow.com"
    assert auth.calls[3][2]["resource"] == resource


@pytest.mark.parametrize(
    ("oauth_error", "http_status", "expected_code", "retryable"),
    [
        ("invalid_grant", 400, ErrorCode.UNAUTHENTICATED, False),
        ("invalid_client", 401, ErrorCode.NOT_CONFIGURED, False),
        ("authorization_pending", 400, ErrorCode.UNAUTHENTICATED, True),
        ("slow_down", 429, ErrorCode.UNAUTHENTICATED, True),
        ("expired_token", 400, ErrorCode.UNAUTHENTICATED, False),
        ("access_denied", 403, ErrorCode.FORBIDDEN, False),
    ],
)
def test_device_polling_oauth_error_field_not_http_status(
    oauth_error: str,
    http_status: int,
    expected_code: ErrorCode,
    retryable: bool,
) -> None:
    server = FakeTransport("https://api.lemoncrow.com", [_server_discovery()])
    auth = FakeTransport("https://auth.example", [_oidc_discovery(), Response(http_status, {"error": oauth_error})])
    client = discover_authward(
        server,
        timeout_s=5,
        transport_factory=lambda _url, *, timeout_s: auth,  # type: ignore[arg-type]
    )
    with pytest.raises(ClientError) as caught:
        client.poll_device("device-code")
    assert caught.value.code == expected_code
    assert caught.value.server_code == oauth_error
    assert caught.value.retryable is retryable
    assert len(auth.calls) == 2


def test_device_polling_rate_limit_without_an_oauth_error_is_retried() -> None:
    server = FakeTransport("https://api.lemoncrow.com", [_server_discovery()])
    limited = Response(429, {"message": "Too many requests. Please try again later."})
    auth = FakeTransport("https://auth.example", [_oidc_discovery(), limited, limited])
    client = discover_authward(
        server,
        timeout_s=5,
        transport_factory=lambda _url, *, timeout_s: auth,  # type: ignore[arg-type]
    )
    with pytest.raises(ClientError) as caught:
        client.poll_device("device-code")
    assert caught.value.server_code == "slow_down"
    assert caught.value.retryable is True
    # Only the device token poll is retried that way: any other refused request stays a refusal.
    with pytest.raises(ClientError) as other:
        client.refresh("refresh-1")
    assert other.value.retryable is False


@pytest.mark.parametrize(("oauth_error", "status"), [("invalid_grant", 400), ("invalid_client", 400)])
def test_cli_refresh_does_not_retry_terminal_oauth_errors(oauth_error: str, status: int) -> None:
    server = FakeTransport("https://api.lemoncrow.com", [_server_discovery()])
    auth = FakeTransport("https://auth.example", [_oidc_discovery(), Response(status, {"error": oauth_error})])
    client = discover_authward(
        server,
        timeout_s=5,
        transport_factory=lambda _url, *, timeout_s: auth,  # type: ignore[arg-type]
    )
    with pytest.raises(ClientError) as caught:
        client.refresh("refresh-token")
    assert caught.value.server_code == oauth_error
    assert caught.value.retryable is False
    assert len(auth.calls) == 2
    assert ("session has ended" in caught.value.message) == (oauth_error == "invalid_grant")


@pytest.mark.parametrize(
    ("product", "scope"),
    [("review", "lemoncrow:review"), ("runtime", "lemoncrow:runtime")],
)
def test_product_client_requests_only_its_advertised_authward_scope(product: str, scope: str) -> None:
    server = FakeTransport(
        "https://api.lemoncrow.com",
        [_server_discovery(product_scopes=("lemoncrow:review", "lemoncrow:runtime"))],
    )
    auth = FakeTransport(
        "https://auth.example",
        [
            _oidc_discovery(),
            Response(
                200,
                {
                    "device_code": "dev-product",
                    "user_code": "PRODUCT",
                    "verification_uri": "https://auth.example/device",
                },
            ),
        ],
    )
    client = discover_authward(
        server,  # type: ignore[arg-type]
        timeout_s=5,
        product=product,
        transport_factory=lambda _url, *, timeout_s: auth,  # type: ignore[arg-type]
    )

    client.start_device()

    requested = auth.calls[1][2]["scope"].split()
    assert "lemoncrow:access" in requested
    assert scope in requested
    other = "lemoncrow:runtime" if product == "review" else "lemoncrow:review"
    assert other not in requested


@pytest.mark.parametrize("product", ["review", "runtime"])
def test_product_client_falls_back_when_server_does_not_advertise_product_scopes(product: str) -> None:
    server = FakeTransport("https://api.lemoncrow.com", [_server_discovery()])
    auth = FakeTransport(
        "https://auth.example",
        [
            _oidc_discovery(),
            Response(
                200,
                {"device_code": "dev-legacy", "user_code": "LEGACY", "verification_uri": "https://auth.example/device"},
            ),
        ],
    )
    client = discover_authward(
        server,  # type: ignore[arg-type]
        timeout_s=5,
        product=product,
        transport_factory=lambda _url, *, timeout_s: auth,  # type: ignore[arg-type]
    )

    client.start_device()

    requested = auth.calls[1][2]["scope"].split()
    assert "lemoncrow:access" in requested
    assert "lemoncrow:review" not in requested
    assert "lemoncrow:runtime" not in requested
