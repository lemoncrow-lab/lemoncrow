from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from lemoncrow.gateway.cli.commands import hosted_auth


class _Authward:
    def __init__(self, *, authorization: dict[str, Any] | None = None, polls: list[object] | None = None) -> None:
        self.authorization = authorization or {}
        self.polls = list(polls or [])
        self.calls: list[tuple[str, object]] = []

    def start_device(self) -> dict[str, Any]:
        self.calls.append(("start_device", None))
        return self.authorization

    def poll_device(self, device_code: str) -> dict[str, Any]:
        self.calls.append(("poll_device", device_code))
        answer = self.polls.pop(0)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, dict)
        return answer

    def revoke_access(self, access_token: str) -> bool:
        self.calls.append(("revoke_access", access_token))
        return True

    def revoke_refresh(self, refresh_token: str) -> bool:
        self.calls.append(("revoke_refresh", refresh_token))
        return True


def test_auth_status_reports_local_mode_without_credentials(monkeypatch) -> None:
    monkeypatch.setattr(hosted_auth, "_config", lambda: SimpleNamespace(hosted=False))

    result = CliRunner().invoke(hosted_auth.auth_group, ["status"])

    assert result.exit_code == 0
    assert result.output == "local: no login required\n"


def test_auth_login_uses_authward_device_flow_and_persists_managed_tokens(tmp_path, monkeypatch) -> None:
    config = SimpleNamespace(hosted=True, url="https://api.lemoncrow.com", state_dir=tmp_path)
    authward = _Authward(
        authorization={
            "device_code": "device-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://lemoncrow.com/device",
            "verification_uri_complete": "https://lemoncrow.com/device?user_code=ABCD-EFGH",
            "expires_in": 60,
            "interval": 1,
        },
        polls=[{"access_token": "access-1", "refresh_token": "refresh-1"}],
    )
    persisted: list[tuple[str, str]] = []
    opened: list[str] = []
    monkeypatch.setattr(hosted_auth, "_config", lambda: config)
    monkeypatch.setattr(hosted_auth, "_authward", lambda _config: authward)
    monkeypatch.setattr(hosted_auth.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(hosted_auth.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        hosted_auth,
        "write_managed_credentials",
        lambda _state_dir, *, access_token, refresh_token: persisted.append((access_token, refresh_token)),
    )

    result = CliRunner().invoke(hosted_auth.auth_group, ["login"])

    assert result.exit_code == 0, result.output
    assert "Open https://lemoncrow.com/device" in result.output
    assert "Code: ABCD-EFGH" in result.output
    assert "auth.olaryn.com" not in result.output
    assert "Authward" not in result.output
    assert "Signed in to https://api.lemoncrow.com" in result.output
    assert opened == ["https://lemoncrow.com/device?user_code=ABCD-EFGH"]
    assert persisted == [("access-1", "refresh-1")]
    assert authward.calls == [("start_device", None), ("poll_device", "device-1")]


def test_auth_logout_revokes_directly_with_authward(tmp_path, monkeypatch) -> None:
    config = SimpleNamespace(
        hosted=True,
        authenticated=True,
        url="https://api.lemoncrow.com",
        state_dir=tmp_path,
        token="access-current",
        refresh_token="refresh-current",
    )
    authward = _Authward()
    cleared: list[object] = []
    monkeypatch.setattr(hosted_auth, "_config", lambda: config)
    monkeypatch.setattr(hosted_auth, "_managed", lambda _config: True)
    monkeypatch.setattr(hosted_auth, "_authward", lambda _config: authward)
    monkeypatch.setattr(hosted_auth, "clear_managed_credentials", lambda state_dir: cleared.append(state_dir))

    result = CliRunner().invoke(hosted_auth.auth_group, ["logout"])

    assert result.exit_code == 0, result.output
    assert result.output == "Signed out.\n"
    assert cleared == [tmp_path]
    assert authward.calls == [
        ("revoke_access", "access-current"),
        ("revoke_refresh", "refresh-current"),
    ]


def test_auth_status_names_legacy_managed_session(monkeypatch) -> None:
    config = SimpleNamespace(
        hosted=True,
        authenticated=True,
        url="https://api.lemoncrow.com",
        token="lcs_legacy",
    )
    monkeypatch.setattr(hosted_auth, "_config", lambda: config)
    monkeypatch.setattr(hosted_auth, "_managed", lambda _config: True)

    result = CliRunner().invoke(hosted_auth.auth_group, ["status"])

    assert result.exit_code == 0
    assert "legacy LemonCrow session" in result.output
    assert "lc auth login" in result.output
