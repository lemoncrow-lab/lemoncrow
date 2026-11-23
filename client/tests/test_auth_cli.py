from __future__ import annotations

import sys
from collections.abc import Sequence
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import lemoncrow_client.auth_cli as auth_cli
import pytest
from lemoncrow_client.cli import main
from lemoncrow_client.errors import AgentAction, ClientError, ErrorCode


class _Authward:
    contract = SimpleNamespace(resource="https://api.lemoncrow.com")

    def __init__(self, *, authorization: dict[str, Any] | None = None, polls: list[object] | None = None) -> None:
        self.authorization = authorization or {}
        self.polls = list(polls or [])
        self.calls: list[tuple[str, object]] = []

    def start_device(self, resources: Sequence[str] = ()) -> dict[str, Any]:
        self.calls.append(("start_device", tuple(resources)))
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


@pytest.mark.parametrize("verb", ["login", "status", "logout"])
def test_auth_verb_help_prints_usage_without_loading_config(verb: str, monkeypatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("help must not load configuration")

    monkeypatch.setattr(auth_cli, "_config", refuse)
    sink = StringIO()
    assert main(["auth", verb, "--help"], stdout=sink) == 0
    assert sink.getvalue().startswith(f"usage: lemoncrow-client auth {verb}")


def test_auth_status_reports_local_mode(monkeypatch) -> None:
    monkeypatch.setattr(auth_cli, "_config", lambda *_args, **_kwargs: SimpleNamespace(hosted=False))
    sink = StringIO()
    assert main(["auth", "status"], stdout=sink) == 0
    assert sink.getvalue() == "local: no login required\n"


_AUTHORIZATION: dict[str, Any] = {
    "device_code": "device-1",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://auth.example/device",
    "verification_uri_complete": "https://auth.example/device?user_code=ABCD-EFGH",
    "expires_in": 60,
    "interval": 1,
}


def _login_setup(tmp_path: Path, monkeypatch, authward: _Authward) -> tuple[SimpleNamespace, list[object]]:
    config = SimpleNamespace(
        hosted=True,
        url="https://api.lemoncrow.com",
        state_dir=tmp_path,
        account_state_dir=tmp_path / "account",
    )
    persisted: list[object] = []
    monkeypatch.delenv("LEMONCROW_WORKSPACES_SERVER", raising=False)
    monkeypatch.setattr(auth_cli, "launch_browser_url", lambda _url: True)
    monkeypatch.setattr(auth_cli, "_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(auth_cli, "_authward", lambda _config: authward)
    monkeypatch.setattr(auth_cli.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        auth_cli,
        "store_new_sign_in",
        lambda state_dir, *, access_token, refresh_token, servers: persisted.append(
            (state_dir, access_token, refresh_token, list(servers))
        ),
    )
    return config, persisted


def test_device_login_persists_managed_tokens_without_full_runtime(tmp_path: Path, monkeypatch) -> None:
    pending = ClientError(
        ErrorCode.UNAUTHENTICATED,
        "pending",
        details={"reason": "authorization_pending"},
        retryable=True,
        action=AgentAction.RETRY_LATER,
        server_code="authorization_pending",
    )
    authward = _Authward(
        authorization=_AUTHORIZATION,
        polls=[pending, {"access_token": "access-1", "refresh_token": "refresh-1"}],
    )
    config, persisted = _login_setup(tmp_path, monkeypatch, authward)
    opened: list[str] = []
    monkeypatch.setattr(auth_cli, "launch_browser_url", lambda url: opened.append(url) or True)

    sink = StringIO()
    assert main(["auth", "login"], stdout=sink) == 0
    assert "Open https://auth.example/device?user_code=ABCD-EFGH" in sink.getvalue()
    assert opened == ["https://auth.example/device?user_code=ABCD-EFGH"]
    assert "Code: ABCD-EFGH" in sink.getvalue()
    assert "Signed in to https://api.lemoncrow.com" in sink.getvalue()
    assert persisted == [(config.account_state_dir, "access-1", "refresh-1", [])]
    assert authward.calls == [
        ("start_device", ()),
        ("poll_device", "device-1"),
        ("poll_device", "device-1"),
    ]


def test_login_names_each_workspaces_server_by_its_origin(tmp_path: Path, monkeypatch) -> None:
    authward = _Authward(authorization=_AUTHORIZATION, polls=[{"access_token": "a", "refresh_token": "r"}])
    config, persisted = _login_setup(tmp_path, monkeypatch, authward)

    sink = StringIO()
    arguments = ["auth", "login", "--no-browser", "--server", "HTTPS://Acme.Workspaces.LemonCrow.com:443/api/"]
    assert main([*arguments, "--server=https://beta.lemoncrow.com"], stdout=sink) == 0
    servers = ["https://acme.workspaces.lemoncrow.com", "https://beta.lemoncrow.com"]
    assert authward.calls[0] == ("start_device", tuple(servers))
    assert persisted == [(config.account_state_dir, "a", "r", servers)]
    assert "Signed in to the Workspaces server https://acme.workspaces.lemoncrow.com\n" in sink.getvalue()


def test_login_names_the_servers_the_current_sign_in_covers_again(tmp_path: Path, monkeypatch) -> None:
    from lemoncrow_client.credentials import write_managed_credentials

    authward = _Authward(authorization=_AUTHORIZATION, polls=[{"access_token": "a", "refresh_token": "r"}])
    config, persisted = _login_setup(tmp_path, monkeypatch, authward)
    write_managed_credentials(
        config.account_state_dir, access_token="old", refresh_token="old-r", servers=["https://acme.lemoncrow.com"]
    )
    monkeypatch.setenv("LEMONCROW_WORKSPACES_SERVER", "https://beta.workspaces.lemoncrow.com")

    assert main(["auth", "login", "--no-browser"], stdout=StringIO()) == 0
    servers = ("https://acme.lemoncrow.com", "https://beta.workspaces.lemoncrow.com")
    assert authward.calls[0] == ("start_device", servers)
    assert persisted == [(config.account_state_dir, "a", "r", list(servers))]


def test_the_review_client_signs_in_without_workspaces_and_keeps_the_servers(tmp_path: Path, monkeypatch) -> None:
    """The Review client ships without task_server: its sign-in names the current sign-in's servers again, and only
    ``--server`` is refused, by the client that has Workspaces."""
    from lemoncrow_client import task_server
    from lemoncrow_client.credentials import write_managed_credentials

    assert auth_cli._SERVER_ENV == task_server.SERVER_ENV
    authward = _Authward(authorization=_AUTHORIZATION, polls=[{"access_token": "a", "refresh_token": "r"}])
    config, persisted = _login_setup(tmp_path, monkeypatch, authward)
    write_managed_credentials(
        config.account_state_dir, access_token="old", refresh_token="old-r", servers=["https://acme.lemoncrow.com"]
    )
    monkeypatch.setenv("LEMONCROW_WORKSPACES_SERVER", "https://beta.workspaces.lemoncrow.com")
    monkeypatch.setitem(sys.modules, "lemoncrow_client.task_server", None)

    assert main(["auth", "login", "--no-browser"], stdout=StringIO()) == 0
    assert authward.calls[0] == ("start_device", ("https://acme.lemoncrow.com",))
    assert persisted == [(config.account_state_dir, "a", "r", ["https://acme.lemoncrow.com"])]

    sink = StringIO()
    assert main(["auth", "login", "--server", "https://acme.workspaces.lemoncrow.com"], stdout=sink) == 2
    assert "lemoncrow-runtime-client auth login --server URL" in sink.getvalue()
    assert len(persisted) == 1


def test_a_configured_server_that_is_not_lemoncrow_hosted_is_not_named(tmp_path: Path, monkeypatch) -> None:
    authward = _Authward(authorization=_AUTHORIZATION, polls=[{"access_token": "a", "refresh_token": "r"}])
    _login_setup(tmp_path, monkeypatch, authward)
    monkeypatch.setenv("LEMONCROW_WORKSPACES_SERVER", "https://ws.acme.example")

    assert main(["auth", "login", "--no-browser"], stdout=StringIO()) == 0
    assert authward.calls[0] == ("start_device", ())


@pytest.mark.parametrize(
    ("server", "refusal"),
    [
        ("https://ws.acme.example", "is not LemonCrow-hosted"),
        ("http://acme.workspaces.lemoncrow.com", "is not LemonCrow-hosted"),
        ("https://lemoncrow.com.evil.example", "is not LemonCrow-hosted"),
        ("https://api.lemoncrow.com", "is the LemonCrow API, not a Workspaces server"),
    ],
)
def test_login_refuses_a_server_a_sign_in_is_never_for(tmp_path: Path, monkeypatch, server: str, refusal: str) -> None:
    authward = _Authward(authorization=_AUTHORIZATION, polls=[])
    _, persisted = _login_setup(tmp_path, monkeypatch, authward)

    sink = StringIO()
    assert main(["auth", "login", "--server", server], stdout=sink) == 2
    assert refusal in sink.getvalue()
    assert authward.calls == []
    assert persisted == []


@pytest.mark.parametrize(
    "complete",
    [
        "https://evil.example/device?user_code=ABCD-EFGH",
        "https://auth.example/other?user_code=ABCD-EFGH",
        "https://user:password@auth.example/device?user_code=ABCD-EFGH",
        "javascript:alert(1)",
        "https://auth.example/device?user_code=WRONG",
        "https://auth.example/device?user_code=ABCD-EFGH&user_code=OTHER",
        "https://auth.example/device?user_code=ABCD-EFGH#fragment",
    ],
)
def test_device_link_does_not_change_origin_or_terminal_code(complete: str) -> None:
    base = "https://auth.example/device"
    assert auth_cli._device_browser_uri({"verification_uri_complete": complete}, base, "ABCD-EFGH") == base


def test_logout_revokes_authward_then_clears_managed_credentials(tmp_path: Path, monkeypatch) -> None:
    config = SimpleNamespace(
        hosted=True,
        authenticated=True,
        url="https://api.lemoncrow.com",
        state_dir=tmp_path,
        account_state_dir=tmp_path / "account",
        token="access-current",
        refresh_token="refresh-current",
    )
    authward = _Authward()
    cleared: list[Path] = []
    monkeypatch.setattr(auth_cli, "_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(auth_cli, "_managed", lambda _config: True)
    monkeypatch.setattr(auth_cli, "_authward", lambda _config: authward)
    monkeypatch.setattr(auth_cli, "clear_managed_credentials", lambda state_dir: cleared.append(state_dir))

    sink = StringIO()
    assert main(["auth", "logout"], stdout=sink) == 0
    assert sink.getvalue() == "Signed out.\n"
    assert cleared == [config.account_state_dir, tmp_path]
    assert authward.calls == [("revoke_refresh", "refresh-current")]


def test_logout_never_deletes_operator_credentials(tmp_path: Path, monkeypatch) -> None:
    config = SimpleNamespace(
        hosted=True,
        authenticated=True,
        url="https://api.lemoncrow.com",
        state_dir=tmp_path,
        account_state_dir=tmp_path / "account",
        token="operator-token",
        refresh_token="",
    )
    monkeypatch.setattr(auth_cli, "_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(auth_cli, "_managed", lambda _config: False)

    sink = StringIO()
    assert main(["auth", "logout"], stdout=sink) == 1
    assert "operator-provided" in sink.getvalue()


def test_a_sign_in_naming_a_server_the_issuer_does_not_know_says_what_to_do(tmp_path: Path, monkeypatch) -> None:
    class Refusing(_Authward):
        def start_device(self, resources: Sequence[str] = ()) -> dict[str, Any]:
            super().start_device(resources)
            raise ClientError(ErrorCode.UNAUTHENTICATED, "refused", server_code="invalid_target")

    authward = Refusing()
    _, persisted = _login_setup(tmp_path, monkeypatch, authward)
    sink = StringIO()
    assert main(["auth", "login", "--server", "https://acme.workspaces.lemoncrow.com"], stdout=sink) == 1
    assert "also named https://acme.workspaces.lemoncrow.com" in sink.getvalue()
    assert "run `lc auth logout`, then `lc auth login --server URL`" in sink.getvalue()
    assert persisted == []


def test_status_names_the_workspaces_servers_the_sign_in_covers(tmp_path: Path, monkeypatch) -> None:
    from lemoncrow_client.credentials import managed_credentials_path, write_managed_credentials

    account = tmp_path / "account"
    write_managed_credentials(account, access_token="a", refresh_token="r", servers=["https://acme.lemoncrow.com"])
    config = SimpleNamespace(
        hosted=True,
        authenticated=True,
        url="https://api.lemoncrow.com",
        state_dir=tmp_path,
        account_state_dir=account,
        token="a",
        token_source=str(managed_credentials_path(account)),
        refresh_token="r",
        refresh_token_source=str(managed_credentials_path(account)),
    )
    monkeypatch.setattr(auth_cli, "_config", lambda *_args, **_kwargs: config)
    sink = StringIO()
    assert main(["auth", "status"], stdout=sink) == 0
    assert sink.getvalue() == (
        "hosted: signed in to https://api.lemoncrow.com with Authward\n"
        "hosted: signed in to the Workspaces server https://acme.lemoncrow.com\n"
    )
