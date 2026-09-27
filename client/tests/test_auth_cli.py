from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import lemoncrow_client.auth_cli as auth_cli
from lemoncrow_client.cli import main
from lemoncrow_client.errors import AgentAction, ClientError, ErrorCode


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

    def revoke_refresh(self, refresh_token: str) -> bool:
        self.calls.append(("revoke_refresh", refresh_token))
        return True


def test_auth_help_is_on_the_thin_client() -> None:
    sink = StringIO()
    assert main(["auth", "--help"], stdout=sink) == 0
    assert "login" in sink.getvalue()
    assert "logout" in sink.getvalue()
    assert "status" in sink.getvalue()


def test_auth_status_reports_local_mode(monkeypatch) -> None:
    monkeypatch.setattr(auth_cli, "_config", lambda: SimpleNamespace(hosted=False))
    sink = StringIO()
    assert main(["auth", "status"], stdout=sink) == 0
    assert sink.getvalue() == "local: no login required\n"


def test_device_login_persists_managed_tokens_without_full_runtime(tmp_path: Path, monkeypatch) -> None:
    config = SimpleNamespace(hosted=True, url="https://api.lemoncrow.com", state_dir=tmp_path)
    pending = ClientError(
        ErrorCode.UNAUTHENTICATED,
        "pending",
        details={"reason": "authorization_pending"},
        retryable=True,
        action=AgentAction.RETRY_LATER,
        server_code="authorization_pending",
    )
    authward = _Authward(
        authorization={
            "device_code": "device-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://auth.example/device",
            "expires_in": 60,
            "interval": 1,
        },
        polls=[pending, {"access_token": "access-1", "refresh_token": "refresh-1"}],
    )
    persisted: list[tuple[str, str]] = []
    monkeypatch.setattr(auth_cli, "_config", lambda: config)
    monkeypatch.setattr(auth_cli, "_authward", lambda _config: authward)
    monkeypatch.setattr(auth_cli.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        auth_cli,
        "write_managed_credentials",
        lambda _state_dir, *, access_token, refresh_token: persisted.append((access_token, refresh_token)),
    )

    sink = StringIO()
    assert main(["auth", "login", "--no-browser"], stdout=sink) == 0
    assert "Open https://auth.example/device" in sink.getvalue()
    assert "Code: ABCD-EFGH" in sink.getvalue()
    assert "Signed in to https://api.lemoncrow.com" in sink.getvalue()
    assert persisted == [("access-1", "refresh-1")]
    assert authward.calls == [
        ("start_device", None),
        ("poll_device", "device-1"),
        ("poll_device", "device-1"),
    ]


def test_logout_revokes_authward_then_clears_managed_credentials(tmp_path: Path, monkeypatch) -> None:
    config = SimpleNamespace(
        hosted=True,
        authenticated=True,
        url="https://api.lemoncrow.com",
        state_dir=tmp_path,
        token="access-current",
        refresh_token="refresh-current",
    )
    authward = _Authward()
    cleared: list[Path] = []
    monkeypatch.setattr(auth_cli, "_config", lambda: config)
    monkeypatch.setattr(auth_cli, "_managed", lambda _config: True)
    monkeypatch.setattr(auth_cli, "_authward", lambda _config: authward)
    monkeypatch.setattr(auth_cli, "clear_managed_credentials", lambda state_dir: cleared.append(state_dir))

    sink = StringIO()
    assert main(["auth", "logout"], stdout=sink) == 0
    assert sink.getvalue() == "Signed out.\n"
    assert cleared == [tmp_path]
    assert authward.calls == [("revoke_refresh", "refresh-current")]


def test_logout_never_deletes_operator_credentials(tmp_path: Path, monkeypatch) -> None:
    config = SimpleNamespace(
        hosted=True,
        authenticated=True,
        url="https://api.lemoncrow.com",
        state_dir=tmp_path,
        token="operator-token",
        refresh_token="",
    )
    monkeypatch.setattr(auth_cli, "_config", lambda: config)
    monkeypatch.setattr(auth_cli, "_managed", lambda _config: False)

    sink = StringIO()
    assert main(["auth", "logout"], stdout=sink) == 1
    assert "operator-provided" in sink.getvalue()
