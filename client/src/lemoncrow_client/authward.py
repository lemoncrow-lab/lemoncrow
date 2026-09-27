"""Direct Authward OAuth client for hosted LemonCrow credentials.

The LemonCrow data-plane origin is still the only product endpoint the client
accepts. Hosted discovery may name one authorization server; this module then
binds a second :class:`HttpTransport` to that exact issuer origin. Authward's
own OIDC discovery must repeat the issuer exactly and every advertised OAuth
endpoint must stay on that origin, so neither LemonCrow nor Authward discovery
can turn authentication into arbitrary egress.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

from .errors import AgentAction, ClientError, ErrorCode
from .transport import HttpTransport, Response

DEVICE_CODE_GRANT: Final[str] = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_TOKEN_GRANT: Final[str] = "refresh_token"
_DEFAULT_SCOPES: Final[tuple[str, ...]] = ("openid", "profile", "email", "offline_access", "lemoncrow:access")
_MAX_ENDPOINT_LEN: Final[int] = 2048

TransportFactory = Callable[[str], HttpTransport]


@dataclass(frozen=True, slots=True)
class AuthwardContract:
    issuer: str
    client_id: str
    resource: str
    scopes: tuple[str, ...]
    device_authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str = ""


@dataclass(slots=True)
class AuthwardClient:
    contract: AuthwardContract
    transport: HttpTransport

    def start_device(self) -> Mapping[str, Any]:
        form = {
            "client_id": self.contract.client_id,
            "scope": " ".join(self.contract.scopes),
        }
        if self.contract.resource:
            form["resource"] = self.contract.resource
        response = self.transport.post_form(self.contract.device_authorization_endpoint, form=form)
        return _oauth_payload(response, operation="device authorization")

    def poll_device(self, device_code: str) -> Mapping[str, Any]:
        form = {
            "grant_type": DEVICE_CODE_GRANT,
            "device_code": device_code,
            "client_id": self.contract.client_id,
        }
        if self.contract.resource:
            form["resource"] = self.contract.resource
        response = self.transport.post_form(self.contract.token_endpoint, form=form)
        return _oauth_payload(response, operation="device token")

    def refresh(self, refresh_token: str) -> tuple[str, str]:
        form = {
            "grant_type": REFRESH_TOKEN_GRANT,
            "refresh_token": refresh_token,
            "client_id": self.contract.client_id,
        }
        if self.contract.resource:
            form["resource"] = self.contract.resource
        payload = _oauth_payload(
            self.transport.post_form(self.contract.token_endpoint, form=form),
            operation="token refresh",
        )
        access = _required_string(payload, "access_token")
        rotated = payload.get("refresh_token")
        refresh = rotated if isinstance(rotated, str) and rotated else refresh_token
        return access, refresh

    def revoke(self, token: str, *, token_type_hint: str) -> bool:
        endpoint = self.contract.revocation_endpoint
        if not endpoint or not token:
            return False
        response = self.transport.post_form(
            endpoint,
            form={
                "token": token,
                "token_type_hint": token_type_hint,
                "client_id": self.contract.client_id,
            },
        )
        _oauth_payload(response, operation="token revocation")
        return True

    def revoke_access(self, access_token: str) -> bool:
        """Revoke a currently issued access token at the authorization server."""
        return self.revoke(access_token, token_type_hint="access_token")

    def revoke_refresh(self, refresh_token: str) -> bool:
        return self.revoke(refresh_token, token_type_hint="refresh_token")


def discover_authward(
    server_transport: HttpTransport,
    *,
    timeout_s: float,
    transport_factory: Callable[..., HttpTransport] = HttpTransport,
) -> AuthwardClient:
    """Resolve and verify the Authward contract advertised by LemonCrow."""

    server = server_transport.get("/.well-known/lemoncrow-server.json", timeout_s=timeout_s).require()
    raw_auth = server.get("authentication")
    if not isinstance(raw_auth, Mapping):
        raise _not_configured(
            "the hosted LemonCrow server does not advertise a compatible authorization server; "
            "upgrade the server or check its hosted sign-in configuration"
        )

    issuer = _required_string(raw_auth, "authorization_server")
    client_id = _required_string(raw_auth, "client_id")
    resource = _required_string(raw_auth, "resource")
    scopes = _scopes(raw_auth.get("scopes"))
    _validate_issuer(issuer)

    auth_transport = transport_factory(issuer, timeout_s=timeout_s)
    discovery = auth_transport.get("/.well-known/openid-configuration", timeout_s=timeout_s).require()
    discovered_issuer = _required_string(discovery, "issuer")
    if discovered_issuer != issuer:
        raise _not_configured("Hosted sign-in discovery issuer does not match the issuer advertised by LemonCrow")

    device_endpoint = _required_string(discovery, "device_authorization_endpoint")
    token_endpoint = _required_string(discovery, "token_endpoint")
    revocation = discovery.get("revocation_endpoint")
    revocation_endpoint = revocation if isinstance(revocation, str) and revocation else ""
    for endpoint in (device_endpoint, token_endpoint, revocation_endpoint):
        if endpoint:
            _require_same_origin(issuer, endpoint)

    return AuthwardClient(
        contract=AuthwardContract(
            issuer=issuer,
            client_id=client_id,
            resource=resource,
            scopes=scopes,
            device_authorization_endpoint=device_endpoint,
            token_endpoint=token_endpoint,
            revocation_endpoint=revocation_endpoint,
        ),
        transport=auth_transport,
    )


def _scopes(value: object) -> tuple[str, ...]:
    if value is None:
        return _DEFAULT_SCOPES
    if not isinstance(value, list):
        raise _not_configured("LemonCrow sign-in discovery scopes are not a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 128 or any(ch.isspace() for ch in item):
            raise _not_configured("LemonCrow sign-in discovery contains an invalid OAuth scope")
        out.append(item)
    return tuple(out) if out else _DEFAULT_SCOPES


def _validate_issuer(issuer: str) -> None:
    parts = urlsplit(issuer)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise _not_configured("Hosted sign-in issuer must not contain credentials, query parameters, or a fragment")
    host = (parts.hostname or "").strip("[]").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise _not_configured("Hosted sign-in issuer must use HTTPS outside loopback development")
    if not host:
        raise _not_configured("Hosted sign-in issuer has no host")


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host.lower(), int(port)


def _require_same_origin(issuer: str, endpoint: str) -> None:
    if len(endpoint) > _MAX_ENDPOINT_LEN or _origin(endpoint) != _origin(issuer):
        raise _not_configured("Hosted sign-in discovery advertised an OAuth endpoint on another origin")


def _required_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > _MAX_ENDPOINT_LEN:
        raise _not_configured(f"Hosted sign-in discovery is missing {name}")
    return value


def _oauth_payload(response: Response, *, operation: str) -> Mapping[str, Any]:
    payload = response.payload
    raw_error = payload.get("error")
    if response.status < 400 and not raw_error:
        return payload

    error = raw_error if isinstance(raw_error, str) and raw_error else "oauth_error"
    description = payload.get("error_description")
    message = (
        description
        if isinstance(description, str) and description
        else f"LemonCrow sign-in refused the {operation} request ({error})"
    )
    retryable = error in {"authorization_pending", "slow_down"}
    action = AgentAction.RETRY_LATER if retryable else AgentAction.REAUTHENTICATE
    code = ErrorCode.UNAUTHENTICATED
    if error == "access_denied":
        code = ErrorCode.FORBIDDEN
        action = AgentAction.REQUEST_ACCESS
    raise ClientError(
        code,
        message,
        details={"reason": error},
        retryable=retryable,
        action=action,
        server_code=error,
    )


def _not_configured(message: str) -> ClientError:
    return ClientError(ErrorCode.NOT_CONFIGURED, message, action=AgentAction.REAUTHENTICATE)


__all__ = [
    "DEVICE_CODE_GRANT",
    "AuthwardClient",
    "AuthwardContract",
    "discover_authward",
]
