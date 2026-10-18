from __future__ import annotations

from pathlib import Path

import lemoncrow_client.credentials as credentials_mod
import pytest
from lemoncrow_client.config import load_config
from lemoncrow_client.credentials import (
    RefreshingHttpTransport,
    managed_credentials_path,
    read_managed_credentials,
    write_managed_credentials,
)
from lemoncrow_client.errors import ClientError
from lemoncrow_client.transport import HttpTransport, Response


def _hosted_env(state_dir: Path, **extra: str) -> dict[str, str]:
    return {
        "LEMONCROW_HOME": str(state_dir),
        "HOME": str(state_dir.parent),
        "LEMONCROW_INSTALL_MODE": "hosted",
        "LEMONCROW_URL": "https://api.lemoncrow.test",
        **extra,
    }


def test_managed_credentials_are_atomic_private_and_loaded_only_for_hosted(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    write_managed_credentials(
        state_dir, access_token="access-managed-0123456789", refresh_token="refresh-managed-0123456789"
    )

    path = managed_credentials_path(state_dir)
    assert path.exists()
    assert path.stat().st_mode & 0o077 == 0
    assert read_managed_credentials(state_dir) == (
        "access-managed-0123456789",
        "refresh-managed-0123456789",
    )

    hosted = load_config(_hosted_env(state_dir), cwd=tmp_path)
    assert hosted.token == "access-managed-0123456789"
    assert hosted.refresh_token == "refresh-managed-0123456789"
    assert hosted.token_source == str(path)
    assert hosted.refresh_token_source == str(path)

    local = load_config(
        {
            "LEMONCROW_HOME": str(state_dir),
            "HOME": str(tmp_path),
            "LEMONCROW_INSTALL_MODE": "local",
        },
        cwd=tmp_path,
    )
    assert local.token == ""
    assert local.refresh_token == ""


def test_operator_access_token_suppresses_stale_managed_family(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    write_managed_credentials(
        state_dir, access_token="access-managed-0123456789", refresh_token="refresh-managed-0123456789"
    )

    config = load_config(
        _hosted_env(state_dir, LEMONCROW_TOKEN="operator-token-0123456789"),
        cwd=tmp_path,
    )

    assert config.token == "operator-token-0123456789"
    assert config.token_source == "LEMONCROW_TOKEN"
    assert config.refresh_token == ""
    assert config.refresh_token_source == ""


def test_refreshing_transport_rotates_once_directly_with_authward_and_retries(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, str], object]] = []
    responses = [
        Response(status=401, payload={"error": {"code": "unauthenticated", "message": "expired"}}),
        Response(status=200, payload={"ok": True}),
        Response(status=200, payload={"ok": "next"}),
    ]
    refreshes: list[str] = []

    def fake_request(self, method, path, *, body=None, form=None, headers=None, timeout_s=None):
        del self, form, timeout_s
        calls.append((method, path, dict(headers or {}), body))
        return responses.pop(0)

    class FakeAuthward:
        def refresh(self, refresh_token: str):
            refreshes.append(refresh_token)
            return "access-new-0123456789", "refresh-new-0123456789"

    monkeypatch.setattr(HttpTransport, "request", fake_request)
    monkeypatch.setattr(credentials_mod, "discover_authward", lambda *_args, **_kwargs: FakeAuthward())
    write_managed_credentials(
        tmp_path,
        access_token="access-old-0123456789",
        refresh_token="refresh-old-0123456789",
    )
    transport = RefreshingHttpTransport(
        "https://api.lemoncrow.test",
        timeout_s=5,
        state_dir=tmp_path,
        access_token="access-old-0123456789",
        refresh_token="refresh-old-0123456789",
    )

    first = transport.get("/v1/sessions", headers={"Authorization": "Bearer access-old-0123456789"})
    assert first.payload == {"ok": True}
    second = transport.get("/v1/views/one", headers={"Authorization": "Bearer access-old-0123456789"})
    assert second.payload == {"ok": "next"}

    assert [(method, path) for method, path, _headers, _body in calls] == [
        ("GET", "/v1/sessions"),
        ("GET", "/v1/sessions"),
        ("GET", "/v1/views/one"),
    ]
    assert refreshes == ["refresh-old-0123456789"]
    assert calls[0][2]["Authorization"] == "Bearer access-old-0123456789"
    assert calls[1][2]["Authorization"] == "Bearer access-new-0123456789"
    assert calls[2][2]["Authorization"] == "Bearer access-new-0123456789"
    assert read_managed_credentials(tmp_path) == (
        "access-new-0123456789",
        "refresh-new-0123456789",
    )


def test_refreshing_transport_adopts_rotation_won_by_another_process(tmp_path: Path, monkeypatch) -> None:
    write_managed_credentials(
        tmp_path,
        access_token="access-old-0123456789",
        refresh_token="refresh-old-0123456789",
    )
    calls: list[tuple[str, str, dict[str, str], object]] = []

    def fake_request(self, method, path, *, body=None, form=None, headers=None, timeout_s=None):
        del self, form, timeout_s
        calls.append((method, path, dict(headers or {}), body))
        if len(calls) == 1:
            assert path == "/v1/sessions"
            write_managed_credentials(
                tmp_path,
                access_token="access-winner-0123456789",
                refresh_token="refresh-winner-0123456789",
            )
            return Response(status=401, payload={"error": {"code": "unauthenticated", "message": "expired"}})
        assert path == "/v1/sessions"
        assert dict(headers or {})["Authorization"] == "Bearer access-winner-0123456789"
        return Response(status=200, payload={"ok": True})

    monkeypatch.setattr(HttpTransport, "request", fake_request)
    transport = RefreshingHttpTransport(
        "https://api.lemoncrow.test",
        timeout_s=5,
        state_dir=tmp_path,
        access_token="access-old-0123456789",
        refresh_token="refresh-old-0123456789",
    )

    response = transport.get("/v1/sessions", headers={"Authorization": "Bearer access-old-0123456789"})
    assert response.payload == {"ok": True}
    assert [(method, path) for method, path, _headers, _body in calls] == [
        ("GET", "/v1/sessions"),
        ("GET", "/v1/sessions"),
    ]


def test_legacy_managed_session_refuses_refresh_with_migration_instruction(tmp_path: Path, monkeypatch) -> None:
    write_managed_credentials(
        tmp_path,
        access_token="lcs_legacy_access_0123456789",
        refresh_token="lcr_legacy_refresh_0123456789",
    )

    def expired(self, method, path, *, body=None, form=None, headers=None, timeout_s=None):
        del self, method, path, body, form, headers, timeout_s
        return Response(status=401, payload={"error": {"code": "unauthenticated", "message": "expired"}})

    monkeypatch.setattr(HttpTransport, "request", expired)
    monkeypatch.setattr(
        credentials_mod,
        "discover_authward",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy credentials must not reach Authward refresh")
        ),
    )
    transport = RefreshingHttpTransport(
        "https://api.lemoncrow.test",
        timeout_s=5,
        state_dir=tmp_path,
        access_token="lcs_legacy_access_0123456789",
        refresh_token="lcr_legacy_refresh_0123456789",
    )

    with pytest.raises(ClientError, match="run `lc auth login` to refresh your hosted sign-in"):
        transport.get("/v1/sessions", headers={"Authorization": "Bearer lcs_legacy_access_0123456789"})


def test_account_scoped_managed_credentials_win_over_legacy_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    account_state_dir = state_dir / "account"
    write_managed_credentials(
        state_dir,
        access_token="legacy-access-0123456789",
        refresh_token="legacy-refresh-0123456789",
    )
    write_managed_credentials(
        account_state_dir,
        access_token="account-access-0123456789",
        refresh_token="account-refresh-0123456789",
    )

    config = load_config(_hosted_env(state_dir), cwd=tmp_path)

    account_path = managed_credentials_path(account_state_dir)
    assert config.token == "account-access-0123456789"
    assert config.refresh_token == "account-refresh-0123456789"
    assert config.token_source == str(account_path)
    assert config.refresh_token_source == str(account_path)


def test_legacy_managed_credentials_remain_readable_during_account_migration(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    write_managed_credentials(
        state_dir,
        access_token="legacy-access-0123456789",
        refresh_token="legacy-refresh-0123456789",
    )

    config = load_config(_hosted_env(state_dir), cwd=tmp_path)

    legacy_path = managed_credentials_path(state_dir)
    assert config.token == "legacy-access-0123456789"
    assert config.refresh_token == "legacy-refresh-0123456789"
    assert config.token_source == str(legacy_path)
    assert config.refresh_token_source == str(legacy_path)
    assert not managed_credentials_path(config.account_state_dir).exists()


def test_refresh_migrates_legacy_pair_into_account_state_before_rotation(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    account_state_dir = state_dir / "account"
    write_managed_credentials(
        state_dir,
        access_token="legacy-access-0123456789",
        refresh_token="legacy-refresh-0123456789",
    )
    responses = [
        Response(status=401, payload={"error": {"code": "unauthenticated", "message": "expired"}}),
        Response(status=200, payload={"ok": True}),
    ]

    def fake_request(self, method, path, *, body=None, form=None, headers=None, timeout_s=None):
        del self, method, path, body, form, headers, timeout_s
        return responses.pop(0)

    class FakeAuthward:
        def refresh(self, refresh_token: str):
            assert refresh_token == "legacy-refresh-0123456789"
            # The legacy pair has already been imported under the new account
            # lock before the one-time refresh credential is consumed.
            assert read_managed_credentials(account_state_dir) == (
                "legacy-access-0123456789",
                "legacy-refresh-0123456789",
            )
            return "new-access-0123456789", "new-refresh-0123456789"

    monkeypatch.setattr(HttpTransport, "request", fake_request)
    monkeypatch.setattr(credentials_mod, "discover_authward", lambda *_args, **_kwargs: FakeAuthward())
    transport = RefreshingHttpTransport(
        "https://api.lemoncrow.test",
        timeout_s=5,
        state_dir=account_state_dir,
        legacy_state_dir=state_dir,
        access_token="legacy-access-0123456789",
        refresh_token="legacy-refresh-0123456789",
    )

    response = transport.get("/v1/sessions", headers={"Authorization": "Bearer legacy-access-0123456789"})

    assert response.payload == {"ok": True}
    assert read_managed_credentials(account_state_dir) == (
        "new-access-0123456789",
        "new-refresh-0123456789",
    )
    # Compatibility file is deliberately left in place for older clients; new
    # clients prefer account/ and logout clears both locations.
    assert read_managed_credentials(state_dir) == (
        "legacy-access-0123456789",
        "legacy-refresh-0123456789",
    )
