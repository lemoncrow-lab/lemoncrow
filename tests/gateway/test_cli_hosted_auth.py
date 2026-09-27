from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from lemoncrow_client.config import load_config
from lemoncrow_client.credentials import (
    managed_credentials_path,
    read_managed_credentials,
    write_managed_credentials,
)
from lemoncrow_client.errors import AgentAction, ClientError, ErrorCode

from lemoncrow.gateway.cli.app import cli
from lemoncrow.gateway.cli.commands import hosted_auth


class FakeAuthward:
    def __init__(self, *, authorization=None, polls=None, revoked=False):  # type: ignore[no-untyped-def]
        self.authorization = authorization or {}
        self.polls = list(polls or [])
        self.revoked = revoked
        self.calls: list[tuple[str, object]] = []

    def start_device(self):  # type: ignore[no-untyped-def]
        self.calls.append(("start_device", None))
        return self.authorization

    def poll_device(self, device_code: str):  # type: ignore[no-untyped-def]
        self.calls.append(("poll_device", device_code))
        answer = self.polls.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def revoke_access(self, access_token: str) -> bool:
        self.calls.append(("revoke_access", access_token))
        return self.revoked

    def revoke_refresh(self, refresh_token: str) -> bool:
        self.calls.append(("revoke_refresh", refresh_token))
        return self.revoked


def _config(tmp_path: Path, *, managed: bool = False):  # type: ignore[no-untyped-def]
    state = tmp_path / "home"
    state.mkdir()
    if managed:
        write_managed_credentials(
            state,
            access_token="authward_access_0123456789abcdef",
            refresh_token="authward_refresh_0123456789abcdef",
        )
    return load_config(
        {
            "LEMONCROW_URL": "https://api.lemoncrow.com",
            "LEMONCROW_INSTALL_MODE": "hosted",
            "LEMONCROW_HOME": str(state),
            "HOME": str(tmp_path),
        },
        cwd=tmp_path,
    )


def test_auth_command_is_registered() -> None:
    result = CliRunner().invoke(cli, ["auth", "--help"])
    assert result.exit_code == 0, result.output
    assert "login" in result.output
    assert "logout" in result.output
    assert "status" in result.output


def test_device_login_persists_authward_credentials_without_using_lemoncrow_auth_routes(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    config = _config(tmp_path)
    pending = ClientError(
        ErrorCode.UNAUTHENTICATED,
        "pending",
        details={"reason": "authorization_pending"},
        retryable=True,
        action=AgentAction.RETRY_LATER,
        server_code="authorization_pending",
    )
    authward = FakeAuthward(
        authorization={
            "device_code": "dev_abc",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://auth.olaryn.com/device",
            "expires_in": 600,
            "interval": 1,
        },
        polls=[
            pending,
            {
                "access_token": "authward_access_new_0123456789",
                "refresh_token": "authward_refresh_new_0123456789",
                "token_type": "Bearer",
                "expires_in": 900,
            },
        ],
    )
    monkeypatch.setattr(hosted_auth, "_config", lambda: config)
    monkeypatch.setattr(hosted_auth, "_authward", lambda _config: authward)
    monkeypatch.setattr(hosted_auth.time, "sleep", lambda _seconds: None)

    result = CliRunner().invoke(hosted_auth.auth_group, ["login", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert "https://auth.olaryn.com/device" in result.output
    assert "ABCD-EFGH" in result.output
    assert read_managed_credentials(config.state_dir) == (
        "authward_access_new_0123456789",
        "authward_refresh_new_0123456789",
    )
    assert authward.calls == [("start_device", None), ("poll_device", "dev_abc"), ("poll_device", "dev_abc")]


def test_logout_revokes_authward_then_removes_managed_credentials(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    config = _config(tmp_path, managed=True)
    authward = FakeAuthward(revoked=True)
    monkeypatch.setattr(hosted_auth, "_config", lambda: config)
    monkeypatch.setattr(hosted_auth, "_authward", lambda _config: authward)

    result = CliRunner().invoke(hosted_auth.auth_group, ["logout"])
    assert result.exit_code == 0, result.output
    assert "Signed out" in result.output
    assert not managed_credentials_path(config.state_dir).exists()
    assert authward.calls == [
        ("revoke_access", "authward_access_0123456789abcdef"),
        ("revoke_refresh", "authward_refresh_0123456789abcdef"),
    ]


def test_logout_never_deletes_a_default_path_operator_token(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    state = tmp_path / "home"
    state.mkdir()
    token_file = state / "token"
    token_file.write_text("operator_access_token_0123456789\n", encoding="utf-8")
    token_file.chmod(0o600)
    config = load_config(
        {
            "LEMONCROW_URL": "https://api.lemoncrow.com",
            "LEMONCROW_INSTALL_MODE": "hosted",
            "LEMONCROW_HOME": str(state),
            "HOME": str(tmp_path),
        },
        cwd=tmp_path,
    )
    monkeypatch.setattr(hosted_auth, "_config", lambda: config)

    result = CliRunner().invoke(hosted_auth.auth_group, ["logout"])
    assert result.exit_code != 0
    assert "operator-provided" in result.output
    assert token_file.read_text().strip() == "operator_access_token_0123456789"
