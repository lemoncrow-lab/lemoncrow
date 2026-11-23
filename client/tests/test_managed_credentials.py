from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path

import lemoncrow_client.credentials as credentials_mod
import pytest
from lemoncrow_client.authward import discover_authward
from lemoncrow_client.config import load_config
from lemoncrow_client.credentials import (
    NOT_COVERED,
    ManagedSignIn,
    RefreshingHttpTransport,
    managed_credentials_path,
    read_managed_credentials,
    read_managed_sign_in,
    refresh_managed_access,
    store_new_sign_in,
    workspaces_access,
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


def test_invalid_grant_refresh_removes_managed_session_without_retry(tmp_path: Path, monkeypatch) -> None:
    write_managed_credentials(
        tmp_path,
        access_token="access-old-0123456789",
        refresh_token="refresh-old-0123456789",
    )
    calls: list[str] = []

    def expired(self, method, path, *, body=None, form=None, headers=None, timeout_s=None):
        calls.append(path)
        return Response(status=401, payload={"error": {"code": "unauthenticated"}})

    class FakeAuthward:
        def refresh(self, refresh_token: str):
            calls.append("refresh")
            raise ClientError(
                credentials_mod.ErrorCode.UNAUTHENTICATED,
                "Your session has ended. Sign in again.",
                server_code="invalid_grant",
            )

    monkeypatch.setattr(HttpTransport, "request", expired)
    monkeypatch.setattr(credentials_mod, "discover_authward", lambda *_args, **_kwargs: FakeAuthward())
    transport = RefreshingHttpTransport(
        "https://api.lemoncrow.test",
        timeout_s=5,
        state_dir=tmp_path,
        access_token="access-old-0123456789",
        refresh_token="refresh-old-0123456789",
    )
    with pytest.raises(ClientError, match="session has ended"):
        transport.get("/v1/sessions", headers={"Authorization": "Bearer access-old-0123456789"})
    assert calls == ["/v1/sessions", "refresh"]
    assert read_managed_credentials(tmp_path) == ("", "")


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


WORKSPACES = "https://acme.workspaces.lemoncrow.com"


def test_a_sign_in_from_before_workspaces_servers_had_tokens_migrates(tmp_path: Path, monkeypatch) -> None:
    path = managed_credentials_path(tmp_path)
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(
        json.dumps({"access_token": "access-old-0123456789", "refresh_token": "refresh-old-0123456789"}),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    assert read_managed_sign_in(tmp_path) == ManagedSignIn("access-old-0123456789", "refresh-old-0123456789", {})
    assert load_config(_hosted_env(tmp_path), cwd=tmp_path).refresh_token == "refresh-old-0123456789"

    sent: list[tuple[str, str]] = []

    class FakeAuthward:
        def refresh(self, refresh_token: str, *, resource: str = ""):
            sent.append((refresh_token, resource))
            return "access-new-0123456789", "refresh-new-0123456789"

    monkeypatch.setattr(credentials_mod, "discover_authward", lambda *_args, **_kwargs: FakeAuthward())
    # It names no Workspaces server: asking for one says which command fixes it, and Authward is not asked.
    with pytest.raises(ClientError) as raised:
        workspaces_access(
            "https://api.lemoncrow.test", state_dir=tmp_path, origin=WORKSPACES, refresh_within_s=60, timeout_s=5
        )
    assert raised.value.server_code == NOT_COVERED
    assert f"run `lc auth login --server {WORKSPACES}`" in raised.value.message
    assert sent == []

    # The next API rotation keeps the sign-in and writes the new layout, whose API pair an older client still reads.
    fresh = refresh_managed_access(
        "https://api.lemoncrow.test", state_dir=tmp_path, stale_access="access-old-0123456789", timeout_s=5
    )
    assert fresh == "access-new-0123456789" and sent == [("refresh-old-0123456789", "")]
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 2,
        "access_token": "access-new-0123456789",
        "refresh_token": "refresh-new-0123456789",
        "servers": {},
    }
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 3, "access_token": "a", "refresh_token": "r"},
        {"version": True, "access_token": "a", "refresh_token": "r"},
        {"version": 2, "access_token": "a", "refresh_token": "r", "servers": ["https://a.lemoncrow.com"]},
        {"version": 2, "access_token": "a", "refresh_token": "r", "servers": {"https://a.lemoncrow.com/x": ""}},
        {"version": 2, "access_token": "a", "refresh_token": "r", "servers": {"HTTPS://A.lemoncrow.com": ""}},
        {"version": 2, "access_token": "a", "refresh_token": "r", "servers": {"https://a.lemoncrow.com": 7}},
    ],
)
def test_a_credential_file_this_client_cannot_read_asks_for_a_new_sign_in(tmp_path: Path, payload: object) -> None:
    path = managed_credentials_path(tmp_path)
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(ClientError):
        read_managed_sign_in(tmp_path)


def test_a_server_token_is_asked_for_by_its_origin_alone_and_the_api_pair_is_kept(tmp_path: Path, monkeypatch) -> None:
    write_managed_credentials(
        tmp_path, access_token="access-api-0123456789", refresh_token="refresh-1", servers=[WORKSPACES]
    )
    sent: list[tuple[str, str]] = []

    class FakeAuthward:
        def refresh(self, refresh_token: str, *, resource: str = ""):
            sent.append((refresh_token, resource))
            if resource == "https://other.lemoncrow.com":
                raise ClientError(credentials_mod.ErrorCode.UNAUTHENTICATED, "no", server_code="invalid_target")
            return f"access-for-{resource}", "refresh-2"

    monkeypatch.setattr(credentials_mod, "discover_authward", lambda *_args, **_kwargs: FakeAuthward())
    access = workspaces_access(
        "https://api.lemoncrow.test", state_dir=tmp_path, origin=WORKSPACES, refresh_within_s=60, timeout_s=5
    )
    assert access == f"access-for-{WORKSPACES}" and sent == [("refresh-1", WORKSPACES)]
    assert read_managed_sign_in(tmp_path) == ManagedSignIn("access-api-0123456789", "refresh-2", {WORKSPACES: access})
    # A token that is not a JWT has no expiry to run out: it is reused until the server refuses it.
    assert (
        workspaces_access(
            "https://api.lemoncrow.test", state_dir=tmp_path, origin=WORKSPACES, refresh_within_s=60, timeout_s=5
        )
        == access
    )
    assert len(sent) == 1

    # Authward answering invalid_target (a resource the sign-in did not name) uses nothing up and names the fix.
    path = managed_credentials_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["servers"]["https://other.lemoncrow.com"] = ""
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ClientError) as raised:
        workspaces_access(
            "https://api.lemoncrow.test",
            state_dir=tmp_path,
            origin="https://other.lemoncrow.com",
            refresh_within_s=60,
            timeout_s=5,
        )
    assert raised.value.server_code == NOT_COVERED
    assert read_managed_sign_in(tmp_path).refresh_token == "refresh-2"


def test_concurrent_refreshes_never_send_a_spent_refresh_token(tmp_path: Path) -> None:
    from _fake_authward import serve_authward

    state_dir = tmp_path / "account"
    with serve_authward() as authward:
        authward.registered.add(WORKSPACES)
        client = discover_authward(HttpTransport(authward.base, timeout_s=5), timeout_s=5)
        started = client.start_device([WORKSPACES])
        issued = client.poll_device(started["device_code"])
        store_new_sign_in(
            state_dir, access_token=issued["access_token"], refresh_token=issued["refresh_token"], servers=[WORKSPACES]
        )
        barrier = threading.Barrier(12)
        failures: list[BaseException] = []

        def api() -> None:
            barrier.wait()
            stale = read_managed_sign_in(state_dir).access_token
            refresh_managed_access(authward.base, state_dir=state_dir, stale_access=stale, timeout_s=5)

        def server() -> None:
            barrier.wait()
            # Every call finds the held token due, so every call rotates the refresh token.
            workspaces_access(
                authward.base, state_dir=state_dir, origin=WORKSPACES, refresh_within_s=10**6, timeout_s=5
            )

        def guarded(work: Callable[[], None]) -> Callable[[], None]:
            def run() -> None:
                try:
                    work()
                except BaseException as exc:  # reported below, with the thread's work
                    failures.append(exc)

            return run

        threads = [threading.Thread(target=guarded(api if n % 2 else server)) for n in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        assert failures == []
        assert not authward.ended
        assert len(authward.refreshes) == len(set(authward.refreshes)) >= 6
        final = read_managed_sign_in(state_dir)
        assert final is not None and final.refresh_token == next(iter(authward.newest.values()))
        assert final.servers[WORKSPACES] == [token for resource, token in authward.issued if resource == WORKSPACES][-1]
