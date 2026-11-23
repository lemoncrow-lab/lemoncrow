"""Workload Identity Federation on the SDK-free Anthropic route: a fresh identity token per exchange, a cached access
token refreshed before it expires, an API key that wins, and a refusal that says where to look. No real network."""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.infra.internal_llm import direct_http, litellm_client
from lemoncrow.pro.capabilities.review.pr_bot import ci_env, model_failure

Reply = tuple[int, dict[str, Any]]
MESSAGES = [{"role": "user", "content": "Is this safe?"}]
ACCESS = "sk-ant-oat01-access-token-0001"


class Fake:
    """GitHub's OIDC endpoint (GET) and Anthropic's token exchange and Messages API (POST), on one local port."""

    def __init__(self) -> None:
        self.url = ""
        self.oidc: list[tuple[str, dict[str, str]]] = []
        self.exchanges: list[dict[str, Any]] = []
        self.messages: list[dict[str, str]] = []
        self.issued = 0
        self.exchange: Callable[[dict[str, Any]], Reply] = lambda body: (
            200,
            {"access_token": f"{ACCESS}-{len(self.exchanges)}", "expires_in": 600, "token_type": "Bearer"},
        )

    def jwt(self) -> str:
        self.issued += 1
        return f"eyJhbGciOiJSUzI1NiJ9.eyJqdGkiOiJ{self.issued:04d}fake.signature{self.issued:04d}"


@contextmanager
def fake() -> Iterator[Fake]:
    state = Fake()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            state.oidc.append((self.path, {key.lower(): value for key, value in self.headers.items()}))
            self._send(200, {"count": 1, "value": state.jwt()})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/v1/oauth/token":
                state.exchanges.append(body)
                self._send(*state.exchange(body))
                return
            state.messages.append({key.lower(): value for key, value in self.headers.items()})
            self._send(
                200,
                {"id": "msg_1", "model": "m", "content": [{"type": "text", "text": "ok"}], "usage": {}},
            )

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID",
        "ANTHROPIC_WORKSPACE_ID",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "GITHUB_ACTIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(direct_http, "_ACCESS", direct_http._AccessToken())


def federate(monkeypatch: pytest.MonkeyPatch, server: Fake, *, workspace: bool = True) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", server.url)
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "fdrl_rule")
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org-uuid")
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_account")
    if workspace:
        monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_one")
    # GitHub's request URL already carries a query string; the audience is appended with `&`.
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", f"{server.url}/oidc?api-version=2.0")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runner-request-token")


def call() -> Any:
    return direct_http.anthropic_chat_with_result(MESSAGES, model="anthropic/m", json_schema=None)


def test_a_federation_exchanges_a_fresh_github_token_and_sends_the_access_token_as_a_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with fake() as server:
        federate(monkeypatch, server)
        assert direct_http.handles_anthropic("anthropic/m") and direct_http.uses_federation("anthropic/m")
        call()
        call()
    [(path, headers)] = server.oidc
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    assert query == {"api-version": ["2.0"], "audience": ["https://api.anthropic.com"]}
    assert headers["authorization"] == "Bearer runner-request-token"
    [exchange] = server.exchanges  # the second call reuses the cached access token
    assert exchange == {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": "eyJhbGciOiJSUzI1NiJ9.eyJqdGkiOiJ0001fake.signature0001",
        "federation_rule_id": "fdrl_rule",
        "organization_id": "org-uuid",
        "service_account_id": "svac_account",
        "workspace_id": "wrkspc_one",
    }
    assert [m["authorization"] for m in server.messages] == [f"Bearer {ACCESS}-1"] * 2
    assert all("x-api-key" not in m for m in server.messages)


def test_optional_ids_are_left_out_of_the_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    with fake() as server:
        federate(monkeypatch, server, workspace=False)
        monkeypatch.delenv("ANTHROPIC_SERVICE_ACCOUNT_ID")
        call()
    assert set(server.exchanges[0]) == {"grant_type", "assertion", "federation_rule_id", "organization_id"}


def test_the_access_token_is_refreshed_120_seconds_before_it_expires_with_a_new_identity_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(direct_http.time, "monotonic", lambda: clock[0])
    with fake() as server:
        federate(monkeypatch, server)
        call()
        clock[0] += 600 - 121  # still 121 s left: the cached token is used
        call()
        assert len(server.exchanges) == 1
        clock[0] += 2  # 119 s left: exchange again
        call()
    assert len(server.exchanges) == 2 and len(server.oidc) == 2
    first, second = (exchange["assertion"] for exchange in server.exchanges)
    assert first != second, "a GitHub token's jti is single-use: every exchange presents a new one"
    assert [m["authorization"] for m in server.messages] == [
        f"Bearer {ACCESS}-1",
        f"Bearer {ACCESS}-1",
        f"Bearer {ACCESS}-2",
    ]


def test_a_short_lived_token_is_refreshed_at_half_its_life(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(direct_http.time, "monotonic", lambda: clock[0])
    with fake() as server:
        server.exchange = lambda body: (200, {"access_token": ACCESS, "expires_in": 60})
        federate(monkeypatch, server)
        call()
        clock[0] = 29.0
        call()
        clock[0] = 31.0
        call()
    assert len(server.exchanges) == 2


def test_a_refused_exchange_says_where_the_reason_is_and_never_repeats_a_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with fake() as server:
        server.exchange = lambda body: (
            401,
            {"type": "error", "error": {"type": "authentication_error", "message": "Authentication failed"}},
        )
        federate(monkeypatch, server)
        with pytest.raises(direct_http.AnthropicRequestFailed) as raised:
            call()
    text = str(raised.value)
    assert text == direct_http.FEDERATION_DENIED
    assert "Authentication history" in text and "repo:<owner>/<repo>:pull_request" in text and "`sub`" in text
    assert raised.value.__cause__ is None and raised.value.__suppress_context__  # no HTTPError carrying the request
    assert "eyJ" not in text and "eyJ" not in caplog.text
    assert not server.messages, "no model call is made without an access token"
    # The review turns it into the Console's history, not "check the secret passed as anthropic-api-key".
    failure = model_failure.classify([text], provider="anthropic")
    assert failure is not None and failure.kind == "federation" and "anthropic-api-key" not in failure.annotation
    assert "Authentication history" in failure.annotation and "repo:<owner>/<repo>:pull_request" in failure.fix
    assert len(f"Review incomplete: {failure.headline}") <= 140


def test_an_api_key_wins_over_a_federation(monkeypatch: pytest.MonkeyPatch) -> None:
    with fake() as server:
        federate(monkeypatch, server)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-key")
        assert not direct_http.uses_federation("anthropic/m")
        call()
    assert not server.oidc and not server.exchanges
    assert server.messages[0]["x-api-key"] == "sk-ant-api-key" and "authorization" not in server.messages[0]
    env = {
        "ANTHROPIC_API_KEY": "k",
        "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_rule",
        "ANTHROPIC_ORGANIZATION_ID": "org",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://x",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t",
    }
    route = ci_env.resolve_model_route(env)
    assert route is not None and route.credential == "ANTHROPIC_API_KEY"


def test_no_token_reaches_a_log_only_the_runner_mask_command(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with fake() as server:
        federate(monkeypatch, server)
        call()
    out, err = capfd.readouterr()
    jwt = server.exchanges[0]["assertion"]
    access = f"{ACCESS}-1"
    assert f"::add-mask::{jwt}\n" in out and f"::add-mask::{access}\n" in out
    for line in (out + err + caplog.text).splitlines():
        if jwt in line or access in line:
            assert line.startswith("::add-mask::"), line
    assert not list(Path.cwd().glob("*jwt*")), "nothing is written to disk"


def test_an_identity_token_file_is_read_again_for_every_exchange(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "token"
    clock = [0.0]
    monkeypatch.setattr(direct_http.time, "monotonic", lambda: clock[0])
    with fake() as server:
        federate(monkeypatch, server)
        monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN_FILE", str(token_file))
        token_file.write_text("jwt-one\n", encoding="utf-8")
        call()
        token_file.write_text("jwt-two\n", encoding="utf-8")
        clock[0] = 10_000.0
        call()
    assert [exchange["assertion"] for exchange in server.exchanges] == ["jwt-one", "jwt-two"]
    assert not server.oidc, "the file wins over GitHub's endpoint"


def test_github_refusing_the_oidc_request_names_the_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    federate(monkeypatch, Fake())
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

    def refuse(*_: Any, **__: Any) -> Any:
        raise direct_http.urllib.error.HTTPError("u", 403, "Forbidden", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(direct_http.urllib.request, "urlopen", refuse)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "http://127.0.0.1:9/oidc?x=1")
    with pytest.raises(direct_http.AnthropicRequestFailed, match="id-token: write"):
        call()


def test_the_litellm_route_hands_a_federated_model_to_the_direct_call_even_with_litellm_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_litellm() -> Any:
        raise AssertionError("LiteLLM cannot send a federated token")

    monkeypatch.setattr(litellm_client, "_litellm_module", no_litellm)
    with fake() as server:
        federate(monkeypatch, server)
        result = litellm_client.chat_with_result(MESSAGES, model="anthropic/m")
        deltas: list[str] = []
        streamed = litellm_client.chat_stream(MESSAGES, on_delta=deltas.append, model="anthropic/m")
    assert result.content == "ok" and streamed == "ok" and deltas == ["ok"]
    assert len(server.exchanges) == 1 and len(server.messages) == 2


def test_the_gate_treats_a_federation_as_anthropic_and_names_id_token_write_when_it_is_missing() -> None:
    ids = {
        "INPUT_ANTHROPIC_FEDERATION_RULE_ID": "fdrl_rule",
        "INPUT_ANTHROPIC_ORGANIZATION_ID": "org",
    }
    env = ci_env.key_environment(
        {**ids, "ACTIONS_ID_TOKEN_REQUEST_URL": "https://x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t"}
    )
    assert env["ANTHROPIC_FEDERATION_RULE_ID"] == "fdrl_rule" and env["ANTHROPIC_ORGANIZATION_ID"] == "org"
    assert ci_env.mask_commands(env) == [], "IDs are not secrets"
    route = ci_env.resolve_model_route(env)
    assert route is not None and (route.provider, route.backend) == ("anthropic", "litellm")
    assert route.model.startswith("anthropic/") and ci_env._install_extra(route) == ""
    # The stdlib-only gate and the call agree on what a federation is.
    assert direct_http.federation_config(env) is not None
    without_oidc = ci_env.key_environment(ids)
    assert ci_env.resolve_model_route(without_oidc) is None and direct_http.federation_config(without_oidc) is None
    gate = ci_env.decide(
        {**without_oidc, "GITHUB_EVENT_NAME": "workflow_dispatch"}, pr_number=7, require_credentials=True
    )
    assert not gate.run and gate.exit_code == 1 and "id-token: write" in gate.reason
    assert gate.error_status == ci_env.NO_ID_TOKEN_STATUS and len(gate.error_status) <= 140


def test_a_claude_subscription_token_selects_the_claude_cli_route_and_is_masked() -> None:
    token = "sk-ant-oat01-subscription-token"
    env = ci_env.key_environment({"INPUT_CLAUDE_CODE_OAUTH_TOKEN": token, "OPENAI_API_KEY": "sk-openai-123456"})
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == token
    assert f"::add-mask::{token}" in ci_env.mask_commands(env)
    route = ci_env.resolve_model_route(env)
    assert route is not None and (route.provider, route.backend, route.model) == ("claude", "claude", "")
    assert ci_env._install_extra(route) == ""
    assert ci_env.resolve_model_route(env, "openai") is not None
    assert ci_env.resolve_model_route(env, "openai").backend == "openai"  # type: ignore[union-attr]
    keyed = ci_env.resolve_model_route({**env, "ANTHROPIC_API_KEY": "k"})
    assert keyed is not None and keyed.backend == "litellm"
    scratch: dict[str, str] = {}
    ci_env.apply_model_route(route, scratch)
    assert scratch["LEMONCROW_LLM_BACKEND"] == "claude"
