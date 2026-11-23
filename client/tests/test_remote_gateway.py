"""Remote MCP gateway: pairing OAuth plus streamable HTTP, with no LemonCrow server.

The gateway is what ChatGPT/Claude.ai connectors reach through a tunnel. In Lite
mode it runs Runtime Lite inside its own process; in hosted mode it runs the thin
client, whose client-site tools execute here and whose server-site tools go to
the hosted URL. Every request below goes over a real loopback socket.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import urllib.parse
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from _stub import TOKEN, StubServer, StubState
from lemoncrow_client.remote.gateway import GatewayServer
from lemoncrow_client.remote.state import (
    MAX_PAIRING_FAILURES,
    MAX_STORE_PAIRING_FAILURES,
    PAIRING_LOCKOUT_SECONDS,
    ConnectorBinding,
    OAuthStore,
    connector_slug,
    load_or_create_pairing_code,
    pairing_path,
    save_connector,
    state_path,
)

HOSTNAME = "lc-test.example.com"
REDIRECT = "https://chat.example.com/oauth/callback"
VERIFIER = "v" * 64


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _closed_port_url() -> str:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


class _Client:
    def __init__(self, gateway: GatewayServer, hostname: str = HOSTNAME) -> None:
        self.gateway = gateway
        self.hostname = hostname
        self.token = ""

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "",
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.port, timeout=60)
        sent = {"X-Forwarded-Host": self.hostname, "X-Forwarded-Proto": "https", **(headers or {})}
        if content_type:
            sent["Content-Type"] = content_type
        if self.token:
            sent.setdefault("Authorization", f"Bearer {self.token}")
        try:
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def form(self, path: str, fields: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        return self.request(
            "POST",
            path,
            body=urllib.parse.urlencode(fields).encode(),
            content_type="application/x-www-form-urlencoded",
        )

    def rpc(self, method: str, params: Mapping[str, Any] | None = None, request_id: int = 1) -> dict[str, Any]:
        status, _headers, raw = self.request(
            "POST",
            "/mcp",
            body=json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})}
            ).encode(),
            content_type="application/json",
        )
        assert status == 200, raw
        payload = json.loads(raw)
        assert isinstance(payload, dict)
        return payload

    def pair(self, store_root: Path) -> str:
        status, _h, raw = self.request(
            "POST",
            "/register",
            body=json.dumps({"redirect_uris": [REDIRECT]}).encode(),
            content_type="application/json",
        )
        assert status == 201, raw
        client_id = json.loads(raw)["client_id"]
        query = {
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": _challenge(VERIFIER),
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": f"https://{self.hostname}",
        }
        status, _h, page = self.request("GET", "/authorize?" + urllib.parse.urlencode(query))
        assert status == 200 and b"pairing_code" in page
        paired = pairing_path(store_root, connector_slug(self.hostname))
        code = load_or_create_pairing_code(paired)  # what the terminal shows
        status, _h, page = self.form("/authorize", {**query, "pairing_code": "wrong"})
        assert b"Incorrect pairing code." in page
        status, headers, _page = self.form("/authorize", {**query, "pairing_code": code})
        assert status == 302
        location = urllib.parse.urlparse(headers["location"])
        returned = dict(urllib.parse.parse_qsl(location.query))
        assert returned["state"] == "xyz"
        token_fields = {
            "grant_type": "authorization_code",
            "code": returned["code"],
            "client_id": client_id,
            "redirect_uri": REDIRECT,
        }
        status, _h, raw = self.form("/token", {**token_fields, "code_verifier": "x" * 64})
        assert status == 400 and json.loads(raw)["error"] == "invalid_grant"
        # The failed exchange consumed the single-use auth code, and the
        # pairing code is single use too; authorize again with a fresh one.
        code = load_or_create_pairing_code(paired)
        status, headers, _page = self.form("/authorize", {**query, "pairing_code": code})
        returned = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(headers["location"]).query))
        token_fields["code"] = returned["code"]
        status, _h, raw = self.form("/token", {**token_fields, "code_verifier": VERIFIER})
        assert status == 200, raw
        issued = json.loads(raw)
        assert issued["token_type"] == "Bearer" and issued["refresh_token"]
        self.token = issued["access_token"]
        return self.token


def _git_repo(root: Path, files: Mapping[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "init"],
        check=True,
        env=env,
    )
    return root


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    root = tmp_path / "lemoncrow-home"
    root.mkdir()
    return root


def _lite_environment(store_root: Path) -> dict[str, str]:
    return {
        "LEMONCROW_HOME": str(store_root),
        "HOME": str(store_root.parent),
        "LEMONCROW_INSTALL_MODE": "lite",
        # Nothing listens here: a Lite gateway must never need a LemonCrow server.
        "LEMONCROW_URL": _closed_port_url(),
        "PATH": os.environ.get("PATH", ""),
    }


@pytest.fixture
def lite_gateway(store_root: Path, tmp_path: Path) -> Iterator[tuple[GatewayServer, Path]]:
    repo = _git_repo(tmp_path / "repo", {"pkg/alpha.py": "def alpha():\n    return 'widget label'\n"})
    save_connector(ConnectorBinding(hostname=HOSTNAME, workspace=str(repo)), store_root=store_root)
    with GatewayServer(environment=_lite_environment(store_root)) as gateway:
        yield gateway, repo


def test_a_strangers_wrong_pairing_codes_never_lock_the_owner_out(
    lite_gateway: tuple[GatewayServer, Path], store_root: Path
) -> None:
    gateway, _repo = lite_gateway
    attacker = _Client(gateway)
    status, _h, raw = attacker.request(
        "POST", "/register", body=json.dumps({"redirect_uris": [REDIRECT]}).encode(), content_type="application/json"
    )
    assert status == 201, raw
    query = {
        "client_id": json.loads(raw)["client_id"],
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": _challenge(VERIFIER),
        "code_challenge_method": "S256",
        "state": "xyz",
        "resource": f"https://{HOSTNAME}",
    }
    for _ in range(MAX_PAIRING_FAILURES):
        attacker.form("/authorize", {**query, "pairing_code": "wrong-guess"})
    status, _h, page = attacker.form("/authorize", {**query, "pairing_code": "wrong-guess"})
    assert status == 429 and b"Too many attempts" in page

    # The owner's own client pairs normally while the stranger is locked out.
    assert _Client(gateway).pair(store_root)


def test_repeat_pairing_lockouts_back_off_per_client(tmp_path: Path) -> None:
    store = OAuthStore(tmp_path / "oauth.json")
    client = store.register_client(
        redirect_uris=[REDIRECT], client_name=None, grant_types=["authorization_code"], response_types=["code"]
    )["client_id"]
    other = store.register_client(
        redirect_uris=[REDIRECT], client_name=None, grant_types=["authorization_code"], response_types=["code"]
    )["client_id"]
    for _ in range(MAX_PAIRING_FAILURES):
        store.record_pairing_failure(client)
    first = store.pairing_lockout_remaining(client)
    assert PAIRING_LOCKOUT_SECONDS - 5 < first <= PAIRING_LOCKOUT_SECONDS
    assert store.pairing_lockout_remaining(other) == 0.0
    for _ in range(MAX_PAIRING_FAILURES):
        store.record_pairing_failure(client)
    assert store.pairing_lockout_remaining(client) > first + PAIRING_LOCKOUT_SECONDS / 2
    store.reset_pairing_failures(client)
    assert store.pairing_lockout_remaining(client) == 0.0


def test_minting_fresh_client_ids_never_escapes_the_store_wide_pairing_budget(tmp_path: Path) -> None:
    store = OAuthStore(tmp_path / "oauth.json")

    def fresh() -> str:
        return str(
            store.register_client(
                redirect_uris=[REDIRECT], client_name=None, grant_types=["authorization_code"], response_types=["code"]
            )["client_id"]
        )

    # A guesser registers a new client before each per-client lockout would land.
    guesses = 0
    while guesses < MAX_STORE_PAIRING_FAILURES:
        client = fresh()
        for _ in range(MAX_PAIRING_FAILURES - 1):
            if guesses == MAX_STORE_PAIRING_FAILURES:
                break
            assert store.pairing_lockout_remaining(client) == 0.0
            store.record_pairing_failure(client)
            guesses += 1
    # Every client, including one registered afterwards, now waits.
    assert store.pairing_lockout_remaining(fresh()) > PAIRING_LOCKOUT_SECONDS - 5

    # A correct pairing clears the backstop; the next round backs off from scratch.
    store.reset_pairing_failures(fresh())
    assert store.pairing_lockout_remaining(fresh()) == 0.0


def test_unknown_connector_hostname_is_not_found(lite_gateway: tuple[GatewayServer, Path]) -> None:
    gateway, _repo = lite_gateway
    status, _h, _raw = _Client(gateway, hostname="unknown.example.com").request(
        "GET", "/.well-known/oauth-authorization-server"
    )
    assert status == 404


def test_mcp_refuses_requests_without_a_valid_bearer(lite_gateway: tuple[GatewayServer, Path]) -> None:
    gateway, _repo = lite_gateway
    client = _Client(gateway)
    client.token = "not-a-token"
    status, headers, _raw = client.request("POST", "/mcp", body=b"{}", content_type="application/json")
    assert status == 401
    assert headers["www-authenticate"] == (
        f'Bearer resource_metadata="https://{HOSTNAME}/.well-known/oauth-protected-resource"'
    )


def test_discovery_documents_advertise_the_public_tunnel_origin(lite_gateway: tuple[GatewayServer, Path]) -> None:
    gateway, _repo = lite_gateway
    status, _h, raw = _Client(gateway).request("GET", "/.well-known/oauth-authorization-server")
    assert status == 200
    document = json.loads(raw)
    assert document["issuer"] == f"https://{HOSTNAME}"
    assert document["token_endpoint"] == f"https://{HOSTNAME}/token"
    assert document["code_challenge_methods_supported"] == ["S256"]


def test_discovery_uses_https_on_public_hostname_without_forwarded_proto(
    lite_gateway: tuple[GatewayServer, Path],
) -> None:
    gateway, _repo = lite_gateway
    status, _h, raw = _Client(gateway).request(
        "GET", "/.well-known/oauth-authorization-server", headers={"X-Forwarded-Proto": ""}
    )
    assert status == 200
    document = json.loads(raw)
    assert document["issuer"] == f"https://{HOSTNAME}"
    assert document["authorization_endpoint"] == f"https://{HOSTNAME}/authorize"
    assert document["registration_endpoint"] == f"https://{HOSTNAME}/register"


def test_lite_mode_runs_runtime_lite_inside_the_gateway(
    lite_gateway: tuple[GatewayServer, Path], store_root: Path
) -> None:
    gateway, _repo = lite_gateway
    client = _Client(gateway)
    client.pair(store_root)
    initialized = client.rpc("initialize", {"protocolVersion": "2025-06-18"})
    assert initialized["result"]["serverInfo"]["name"] == "lemoncrow-runtime-lite"
    listed = client.rpc("tools/list", request_id=2)
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {"read", "code_search", "bash", "edit"} <= names
    read = client.rpc("tools/call", {"name": "read", "arguments": {"files": ["pkg/alpha.py"]}}, request_id=3)
    assert read["result"]["isError"] is False
    assert "widget label" in read["result"]["content"][0]["text"]


def test_streamable_http_answers_event_streams_when_asked(
    lite_gateway: tuple[GatewayServer, Path], store_root: Path
) -> None:
    gateway, _repo = lite_gateway
    client = _Client(gateway)
    client.pair(store_root)
    status, headers, raw = client.request(
        "POST",
        "/mcp",
        body=json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}).encode(),
        content_type="application/json",
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert status == 200 and headers["content-type"].startswith("text/event-stream")
    assert json.loads(raw.decode().removeprefix("data: ").strip()) == {"jsonrpc": "2.0", "id": 7, "result": {}}
    status, _h, _raw = client.request(
        "POST",
        "/mcp",
        body=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
        content_type="application/json",
    )
    assert status == 202


def test_project_id_selects_a_repository_inside_the_connector_workspace(store_root: Path, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _git_repo(projects / "one", {"one.py": "ONE = 'first project'\n"})
    _git_repo(projects / "two", {"two.py": "TWO = 'second project'\n"})
    outside = _git_repo(tmp_path / "outside", {"secret.py": "SECRET = 1\n"})
    save_connector(ConnectorBinding(hostname=HOSTNAME, workspace=str(projects)), store_root=store_root)
    with GatewayServer(environment=_lite_environment(store_root)) as gateway:
        client = _Client(gateway)
        client.pair(store_root)
        initialized = client.rpc("initialize", {"protocolVersion": "2025-06-18"})
        assert initialized["result"]["serverInfo"]["name"] == "lemoncrow-runtime-lite"
        listed = client.rpc("tools/list")
        assert {"read", "bash", "code_search"} <= {tool["name"] for tool in listed["result"]["tools"]}
        read = client.rpc(
            "tools/call",
            {"name": "read", "arguments": {"files": ["two.py"], "project_id": str(projects / "two")}},
        )
        assert "second project" in read["result"]["content"][0]["text"]
        refused = client.rpc(
            "tools/call",
            {"name": "read", "arguments": {"files": ["secret.py"], "project_id": str(outside)}},
            request_id=2,
        )
        assert refused["result"]["isError"] is True
        assert "outside this connector's workspace" in refused["result"]["content"][0]["text"]
        unbound = client.rpc("tools/call", {"name": "read", "arguments": {"files": ["one/one.py"]}}, request_id=3)
        assert unbound["result"]["isError"] is False
        assert "first project" in unbound["result"]["content"][0]["text"]


def test_existing_pairing_and_oauth_state_files_are_reused(store_root: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", {"a.py": "A = 1\n"})
    save_connector(ConnectorBinding(hostname=HOSTNAME, workspace=str(repo)), store_root=store_root)
    slug = connector_slug(HOSTNAME)
    paired = pairing_path(store_root, slug)
    paired.parent.mkdir(parents=True, exist_ok=True)
    paired.write_text(json.dumps({"pairing_code": "kept-from-before"}), encoding="utf-8")
    assert paired == store_root / "mcp" / f"pairing-{slug}.json"
    assert state_path(store_root, slug) == store_root / "mcp" / f"oauth-{slug}.json"
    # A code stored before codes expired is stale: the pairing file is reused
    # but holds a freshly minted code, while the OAuth state carries over.
    assert load_or_create_pairing_code(paired) != "kept-from-before"
    with GatewayServer(environment=_lite_environment(store_root)) as gateway:
        _Client(gateway).pair(store_root)
    assert json.loads(state_path(store_root, slug).read_text(encoding="utf-8"))["access_tokens"]


def _authorize_query(client: _Client, redirect: str, name: str | None = None) -> dict[str, str]:
    metadata: dict[str, Any] = {"redirect_uris": [redirect]}
    if name is not None:
        metadata["client_name"] = name
    status, _h, raw = client.request(
        "POST", "/register", body=json.dumps(metadata).encode(), content_type="application/json"
    )
    assert status == 201, raw
    return {
        "client_id": json.loads(raw)["client_id"],
        "redirect_uri": redirect,
        "response_type": "code",
        "code_challenge": _challenge(VERIFIER),
        "code_challenge_method": "S256",
        "state": "s",
        "resource": f"https://{client.hostname}",
    }


def test_authorize_page_names_the_client_and_redirect_host_and_cannot_be_framed(
    store_root: Path, tmp_path: Path
) -> None:
    repo = _git_repo(tmp_path / "repo", {"a.py": "A = 1\n"})
    save_connector(ConnectorBinding(hostname=HOSTNAME, workspace=str(repo)), store_root=store_root)
    with GatewayServer(environment=_lite_environment(store_root)) as gateway:
        client = _Client(gateway)
        query = _authorize_query(client, "https://evil.example/cb", "Totally <b>Claude</b>")
        status, headers, page = client.request("GET", "/authorize?" + urllib.parse.urlencode(query))
        assert status == 200
        assert b"evil.example" in page
        assert b"Totally &lt;b&gt;Claude&lt;/b&gt;" in page and b"<b>Claude" not in page
        assert headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        status, headers, page = client.form("/authorize", {**query, "pairing_code": "wrong"})
        assert b"evil.example" in page and headers["x-frame-options"] == "DENY"
        status, headers, page = client.request("GET", "/authorize?client_id=unknown")
        assert status == 400 and headers["x-frame-options"] == "DENY"


def test_pairing_code_is_single_use_and_short_lived(store_root: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", {"a.py": "A = 1\n"})
    save_connector(ConnectorBinding(hostname=HOSTNAME, workspace=str(repo)), store_root=store_root)
    paired = pairing_path(store_root, connector_slug(HOSTNAME))
    with GatewayServer(environment=_lite_environment(store_root)) as gateway:
        client = _Client(gateway)
        query = _authorize_query(client, REDIRECT)
        code = load_or_create_pairing_code(paired)
        assert load_or_create_pairing_code(paired) == code
        status, _h, _page = client.form("/authorize", {**query, "pairing_code": code})
        assert status == 302
        # A code that authorized one connector never authorizes another.
        status, _h, page = client.form("/authorize", {**query, "pairing_code": code})
        assert status != 302 and b"expired or was already used" in page
        fresh = load_or_create_pairing_code(paired)
        assert fresh != code
        # An expired code (or one stored before codes expired) is refused.
        paired.write_text(json.dumps({"pairing_code": fresh, "expires_at": 1.0}), encoding="utf-8")
        status, _h, page = client.form("/authorize", {**query, "pairing_code": fresh})
        assert status != 302 and b"expired or was already used" in page
        paired.write_text(json.dumps({"pairing_code": "kept-from-before"}), encoding="utf-8")
        status, _h, _page = client.form("/authorize", {**query, "pairing_code": "kept-from-before"})
        assert status != 302
        assert load_or_create_pairing_code(paired) not in {fresh, "kept-from-before"}


def test_hosted_mode_runs_the_thin_client_against_the_hosted_url(store_root: Path, worktree: Path) -> None:
    from lemoncrow_client.mcpserver import SERVER_NAME as THIN_SERVER_NAME

    save_connector(ConnectorBinding(hostname=HOSTNAME, workspace=str(worktree)), store_root=store_root)
    with StubServer(StubState()) as stub:
        environment = {
            "LEMONCROW_HOME": str(store_root),
            "HOME": str(store_root.parent),
            "LEMONCROW_INSTALL_MODE": "hosted",
            "LEMONCROW_URL": stub.url,
            "LEMONCROW_TOKEN": TOKEN,
            "PATH": os.environ.get("PATH", ""),
        }
        with GatewayServer(environment=environment) as gateway:
            client = _Client(gateway)
            client.pair(store_root)
            initialized = client.rpc("initialize", {"protocolVersion": "2025-06-18"})
            assert initialized["result"]["serverInfo"]["name"] == THIN_SERVER_NAME
            grep = client.rpc("tools/call", {"name": "grep", "arguments": {"pattern": "alpha"}}, request_id=2)
            assert grep["result"]["isError"] is False
            assert "alpha" in grep["result"]["content"][0]["text"]
            assert not any(call[0] == "grep" for call in stub.state.tool_calls), "grep is client-site"
            client.rpc("tools/call", {"name": "code_search", "arguments": {"query": "alpha"}}, request_id=3)
            assert stub.state.session_id, "the thin client opened a session on the hosted server"
            assert any(call[0] == "code_search" for call in stub.state.tool_calls), "code_search is server-site"


def test_the_gateway_imports_no_server_or_full_runtime() -> None:
    # Any LemonCrow distribution other than this client (full runtime, public or
    # private server) is a server-side dependency the gateway must not have.
    probe = (
        "import sys, lemoncrow_client.remote.cli\n"
        "tops = {m.split('.')[0] for m in sys.modules}\n"
        "bad = sorted(t for t in tops if (t.startswith('lemoncrow') and t != 'lemoncrow_client')"
        " or t in {'aiohttp', 'fastapi', 'uvicorn', 'click'})\n"
        "print(','.join(bad))\n"
    )
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert completed.stdout.strip() == ""


class _RecordingBackend:
    def __init__(self) -> None:
        self.closed = False

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": []}}

    def close(self) -> None:
        self.closed = True


def test_mcp_discovery_lists_tools_without_building_a_backend(store_root: Path, tmp_path: Path) -> None:
    from lemoncrow_client.remote.gateway import RemoteGateway, Request

    repo = _git_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    save_connector(ConnectorBinding(hostname=HOSTNAME, workspace=str(repo)), store_root=store_root)
    built: list[Path] = []

    def factory(root: Path) -> _RecordingBackend:
        built.append(root)
        return _RecordingBackend()

    gateway = RemoteGateway(environment=_lite_environment(store_root), factory=factory)
    request = Request("GET", "/.well-known/mcp.json", {}, {"x-forwarded-host": HOSTNAME}, b"", "127.0.0.1")
    for _ in range(3):
        response = gateway.dispatch(request)
        assert response.status == 200
        names = {tool["name"] for tool in json.loads(response.body)["tools"]}
        assert {"read", "code_search", "bash", "edit"} <= names
    # An unauthenticated caller never makes the gateway open a workspace.
    assert built == []


def test_a_session_handed_to_a_request_is_never_evicted_under_it(
    store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_client.remote import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "_MAX_SESSIONS", 1)
    backends: list[_RecordingBackend] = []

    def factory(root: Path) -> _RecordingBackend:
        backends.append(_RecordingBackend())
        return backends[-1]

    gateway = gateway_module.RemoteGateway(environment=_lite_environment(store_root), factory=factory)
    binding = ConnectorBinding(hostname=HOSTNAME, workspace=str(tmp_path))
    first = gateway._session(binding, "client-a", tmp_path)
    assert first is not None
    # The first request holds its session but has not started running yet.
    assert gateway._session(binding, "client-b", tmp_path) is None
    assert backends[0].closed is False
    gateway._release(first)
    second = gateway._session(binding, "client-b", tmp_path)
    assert second is not None and backends[0].closed is True
