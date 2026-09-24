"""The review workspace process: bind, registration, adoption, bundle.

Everything here is a security control, and each one is asserted with the case
it exists to prevent:

* the bind **refuses** a non-loopback host with a non-zero exit -- it does not
  warn and carry on, because a warning nobody reads is how a private repository
  ends up on a LAN;
* the registration holds a bearer token, so it is ``0600`` and written
  tmp-then-replace -- a torn write hands the next reader an unreachable
  workspace;
* a registration naming a dead pid reads as **absent**, so a crash without
  cleanup respawns instead of dialling a port nobody is listening on;
* adoption requires the recorded token to still **work**. Liveness alone is the
  bug that made every authenticated call 403 forever in the MCP daemon.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import signal
import socket
import stat
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review import workspace as ws


def _handle(
    store_root: Path,
    repo_root: Path,
    port: int,
    token: str = "tok",
    *,
    generation: str | None = None,
) -> ws.WorkspaceHandle:
    return ws.WorkspaceHandle(
        pid=os.getpid(),
        url=f"http://127.0.0.1:{port}",
        token=token,
        started_at=1.0,
        repo_root=str(repo_root),
        generation=ws.workspace_generation() if generation is None else generation,
    )


# --------------------------------------------------------------------------- #
# 1. bind
# --------------------------------------------------------------------------- #


def test_the_bind_refuses_any_host_but_loopback() -> None:
    """Not a warning. A refusal, with the host named."""

    for host in ("0.0.0.0", "::", "", "192.168.1.10", "localhost"):
        with pytest.raises(ws.WorkspaceBindRefused) as excinfo:
            ws.bind_loopback(host)
        assert "127.0.0.1" in str(excinfo.value)


def test_the_loopback_bind_keeps_the_socket_it_bound() -> None:
    """Port 0 plus a kept socket: no discover-close-rebind window to lose."""

    sock = ws.bind_loopback()
    try:
        host, port = sock.getsockname()[:2]
        assert host == "127.0.0.1"
        assert port > 0
        # Still bound: a second bind of the same port must fail.
        rival = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError):
                rival.bind(("127.0.0.1", port))
                rival.listen(1)
        finally:
            rival.close()
    finally:
        sock.close()


def test_local_workspace_idle_timeout_is_bounded_and_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ws.IDLE_TIMEOUT_ENV, raising=False)
    assert ws._workspace_idle_seconds() == 4 * 60 * 60

    monkeypatch.setenv(ws.IDLE_TIMEOUT_ENV, "90")
    assert ws._workspace_idle_seconds() == 90

    monkeypatch.setenv(ws.IDLE_TIMEOUT_ENV, "1")
    assert ws._workspace_idle_seconds() == 30

    monkeypatch.setenv(ws.IDLE_TIMEOUT_ENV, "not-a-number")
    assert ws._workspace_idle_seconds() == 4 * 60 * 60


def test_local_workspace_idle_reaper_requests_server_shutdown() -> None:
    class _Server:
        should_exit = False

    server = _Server()
    activity = ws._WorkspaceActivity(last_seen=0.0)
    stop = threading.Event()

    ws._idle_reaper(server, activity, stop, idle_seconds=0.001, check_interval=0.001)

    assert server.should_exit is True


def test_retired_workspace_entrypoint_exits_non_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real CLI refuses the retired per-repository workspace server."""

    monkeypatch.setenv(ws.HOST_ENV, "0.0.0.0")
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path / "store"), "review", "--repo-root", str(Path.cwd()), "--serve-workspace"],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "per-repository Review workspace server is retired" in result.output
    assert "LEMONCROW_URL" in result.output


# --------------------------------------------------------------------------- #
# 2 + 3. registration
# --------------------------------------------------------------------------- #


def test_the_registration_is_owner_only_and_written_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """0600 because it holds a bearer token; replace() because a torn write lies."""

    seen: list[tuple[str, str]] = []
    original = Path.replace

    def _spy(self: Path, target: Any) -> Path:
        seen.append((self.name, Path(target).name))
        return original(self, target)

    monkeypatch.setattr(Path, "replace", _spy)
    store_root = tmp_path / "store"
    repo_root = tmp_path / "repo"
    path = ws.write_registration(store_root, repo_root, _handle(store_root, repo_root, 4100))

    assert path.is_file()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert seen and seen[-1][1] == path.name
    assert seen[-1][0] != path.name, "the payload must land on a temp name first"
    assert [item.name for item in path.parent.iterdir()] == [path.name], "a temp file was left behind"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["token"] == "tok"
    assert payload["generation"] == ws.workspace_generation()


def test_the_registration_is_never_world_readable_even_for_an_instant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp file holds the bearer token from its first byte.

    Created at the process umask (0644 under the usual 022) and only chmod'ed
    to 0600 afterwards, it published the workspace token -- which authenticates
    every ``/api/*`` route of a private repository -- to any local user looping
    ``open()`` on the traversable workspaces directory.
    """

    # Every mode the token file is ever observed at: as created (os.open) and as
    # found by any later chmod, which is where the old 0644 window showed up.
    modes: list[tuple[str, int]] = []
    real_open = os.open
    real_chmod = os.chmod

    def _spy_open(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        fd = real_open(path, flags, mode, **kwargs)
        with contextlib.suppress(OSError):
            modes.append((Path(path).name, stat.S_IMODE(os.fstat(fd).st_mode)))
        return fd

    def _spy_chmod(path: Any, mode: int, **kwargs: Any) -> None:
        with contextlib.suppress(OSError, TypeError):
            modes.append((Path(path).name, stat.S_IMODE(os.stat(path).st_mode)))
        real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "open", _spy_open)
    monkeypatch.setattr(os, "chmod", _spy_chmod)
    store_root = tmp_path / "store"
    repo_root = tmp_path / "repo"
    path = ws.write_registration(store_root, repo_root, _handle(store_root, repo_root, 4100))
    monkeypatch.undo()

    tmp_modes = [(name, mode) for name, mode in modes if name != path.name]
    assert tmp_modes, "the payload must land on a temp file first"
    exposed = [(name, oct(mode)) for name, mode in tmp_modes if mode & 0o077]
    assert exposed == [], f"the workspace token was group/world readable: {exposed}"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "tok"


def test_a_registration_naming_a_dead_pid_reads_as_absent(tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    repo_root = tmp_path / "repo"
    path = ws.registration_path(store_root, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": 2**22 - 1, "url": "http://127.0.0.1:4100", "token": "tok", "started_at": 1.0}),
        encoding="utf-8",
    )
    assert ws.read_registration(store_root, repo_root) is None
    assert not path.exists()


def test_a_corrupt_or_missing_registration_reads_as_absent(tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    repo_root = tmp_path / "repo"
    assert ws.read_registration(store_root, repo_root) is None
    path = ws.registration_path(store_root, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert ws.read_registration(store_root, repo_root) is None
    assert not path.exists()


def test_stale_registration_cleanup_never_deletes_a_concurrent_replacement(tmp_path: Path) -> None:
    path = tmp_path / "workspace.json"
    observed = "old"
    path.write_text("new", encoding="utf-8")

    ws._remove_stale_registration(path, observed)

    assert path.read_text(encoding="utf-8") == "new"


def test_retire_legacy_workspaces_only_signals_a_process_that_still_authenticates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    registrations = store_root / "review" / "workspaces"
    registrations.mkdir(parents=True)
    healthy = registrations / "workspace-healthy.json"
    stale = registrations / "workspace-stale.json"
    healthy.write_text(
        json.dumps(
            {
                "pid": 41001,
                "url": "http://127.0.0.1:4101",
                "token": "healthy-token",
                "started_at": 1.0,
                "repo_root": str(tmp_path / "healthy"),
            }
        ),
        encoding="utf-8",
    )
    stale.write_text(
        json.dumps(
            {
                "pid": 41002,
                "url": "http://127.0.0.1:4102",
                "token": "stale-token",
                "started_at": 1.0,
                "repo_root": str(tmp_path / "stale"),
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(ws, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(ws, "probe_healthy", lambda handle, timeout=2.0: handle.pid == 41001)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert ws.retire_legacy_workspaces(store_root) == 1
    assert killed == [(41001, signal.SIGTERM)]
    assert not healthy.exists()
    assert not stale.exists()


def test_retire_legacy_workspaces_removes_corrupt_registration_without_signalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    registrations = store_root / "review" / "workspaces"
    registrations.mkdir(parents=True)
    corrupt = registrations / "workspace-corrupt.json"
    corrupt.write_text("{bad json", encoding="utf-8")

    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("corrupt registration must never signal a pid"))

    assert ws.retire_legacy_workspaces(store_root) == 0
    assert not corrupt.exists()


def test_two_repositories_get_two_registrations(tmp_path: Path) -> None:
    """One workspace serves one repo_root; adopting another's would serve the wrong repo."""

    store_root = tmp_path / "store"
    first = ws.registration_path(store_root, tmp_path / "a")
    second = ws.registration_path(store_root, tmp_path / "b")
    assert first != second
    assert first.parent == second.parent


def test_removing_a_registration_never_deletes_another_processes(tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    repo_root = tmp_path / "repo"
    path = ws.write_registration(
        store_root,
        repo_root,
        ws.WorkspaceHandle(pid=os.getpid() + 1, url="http://127.0.0.1:1", token="t", started_at=1.0),
    )
    ws.remove_registration(store_root, repo_root)
    assert path.is_file()


# --------------------------------------------------------------------------- #
# 4. adoption, and what "healthy" means
# --------------------------------------------------------------------------- #


class _Stub(http.server.BaseHTTPRequestHandler):
    """A server that is alive but rejects the token -- the exact stale case."""

    accept_token = ""

    def do_GET(self) -> None:  # stdlib handler contract dictates the name
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            return
        presented = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        self.send_response(200 if presented and presented == self.accept_token else 403)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:  # keep the test output clean
        return


def _stub_server(accept_token: str) -> tuple[Any, int]:
    handler = type("_Bound", (_Stub,), {"accept_token": accept_token})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def test_health_means_the_token_still_works_not_merely_that_the_pid_lives(tmp_path: Path) -> None:
    """``/healthz`` answering 200 is not permission to adopt."""

    server, port = _stub_server(accept_token="the-real-token")
    try:
        good = _handle(tmp_path, tmp_path, port, token="the-real-token")
        stale = _handle(tmp_path, tmp_path, port, token="a-token-from-a-dead-process")
        assert ws.probe_healthy(good) is True
        assert ws.probe_healthy(stale) is False
    finally:
        server.shutdown()
        server.server_close()


def test_ensure_workspace_is_a_hard_retirement_guard(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="per-repository Review workspace server is retired"):
        ws.ensure_workspace(tmp_path / "store", repo_root)


def test_serve_workspace_is_a_hard_retirement_guard(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="per-repository Review workspace server is retired"):
        ws.serve_workspace(tmp_path / "store", repo_root)


def test_retirement_guard_never_touches_a_live_legacy_registration(tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    server, port = _stub_server(accept_token="live")
    try:
        handle = _handle(store_root, repo_root, port, token="live")
        ws.write_registration(store_root, repo_root, handle)
        with pytest.raises(RuntimeError, match="per-repository Review workspace server is retired"):
            ws.ensure_workspace(store_root, repo_root)
        adopted = ws.read_registration(store_root, repo_root)
        assert adopted is not None
        assert adopted.pid == handle.pid
        assert adopted.url == handle.url
        assert ws.probe_healthy(adopted) is True
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# bundle
# --------------------------------------------------------------------------- #


def test_a_missing_bundle_reads_as_none_rather_than_raising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pip install ships no frontend; the CLI must fall back, not crash."""

    empty = tmp_path / "nothing"
    empty.mkdir()
    monkeypatch.setenv(ws.BUNDLE_ENV, str(empty))
    monkeypatch.setattr(ws, "_checkout_bundle", lambda: empty)
    monkeypatch.setattr(
        "lemoncrow.infra.runtime.frontend_bundle.frontend_dir",
        lambda: empty,
    )
    assert ws.bundle_dir() is None


def test_a_bundle_is_found_through_the_environment_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "dist"
    bundle.mkdir()
    (bundle / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setenv(ws.BUNDLE_ENV, str(bundle))
    assert ws.bundle_dir() == bundle.resolve()


def test_workspace_generation_changes_when_the_served_review_bundle_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted against the chunk names ``vite build`` really emits.

    The digest globbed ``ReviewWorkspace-*``, the chunk that existed before the
    reader was split into ``ReviewReader``/``ReviewStream``. The glob then
    matched nothing in a real build while this test kept passing on a fixture
    chunk wearing the dead name, so the digest stopped moving when the shipped
    reader changed and an upgraded CLI adopted a workspace still serving the
    previous bundle. The fixture therefore mirrors an actual build layout, and
    the chunk it edits is one the old pattern would have ignored.
    """

    bundle = tmp_path / "dist"
    assets = bundle / "assets"
    assets.mkdir(parents=True)
    (bundle / "index.html").write_text("<html>review</html>", encoding="utf-8")
    stylesheet = assets / "index-Bi751VJl.css"
    stylesheet.write_text(".shell{color:red}", encoding="utf-8")
    (assets / "index-CJi9FGA7.js").write_text("import('./ReviewReader-Cv66EACy.js')", encoding="utf-8")
    reader = assets / "ReviewReader-Cv66EACy.js"
    reader.write_text("const version = 1", encoding="utf-8")
    monkeypatch.setenv(ws.BUNDLE_ENV, str(bundle))

    first = ws.workspace_generation()
    reader.write_text("const version = 2", encoding="utf-8")
    second = ws.workspace_generation()

    assert first != second

    # Rot insurance: the digest has to cover whatever the build emits, not a
    # list of chunk names someone has to remember to update. A chunk named
    # after a component nobody has written yet counts the same as this one.
    (assets / "SomeFutureChunk-Xk3f9aQ2.js").write_text("const later = 1", encoding="utf-8")
    third = ws.workspace_generation()
    assert third != second

    # The stylesheet too, so that a pattern narrowed back to script chunks fails
    # here rather than passing on the two assertions above. Between them the
    # three name a stylesheet, a script chunk that exists today and a chunk
    # nobody has written yet: no pattern narrower than "everything the build
    # emitted" survives all three.
    stylesheet.write_text(".shell{color:blue}", encoding="utf-8")
    assert ws.workspace_generation() != third


def test_workspace_generation_sees_a_compiled_review_module_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most of this package ships as mypyc ``.so`` only in a release wheel.

    Fingerprinting ``*.py`` alone left the staleness check blind to 21 of the
    24 review modules, so a patch release confined to compiled code produced
    the same generation stamp and the upgraded CLI adopted the still-running
    pre-upgrade workspace process.
    """

    bundle = tmp_path / "dist"
    bundle.mkdir()
    (bundle / "index.html").write_text("<html>review</html>", encoding="utf-8")
    monkeypatch.setenv(ws.BUNDLE_ENV, str(bundle))

    compiled = Path(ws.__file__).resolve().parent / "store.cpython-000-regression.so"
    assert not compiled.exists()
    compiled.write_bytes(b"compiled-v1")
    try:
        first = ws.workspace_generation()
        compiled.write_bytes(b"compiled-v2")
        second = ws.workspace_generation()
    finally:
        compiled.unlink()

    assert first != second


def test_the_checkout_bundle_beats_the_installed_stack_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dev checkout must serve its own build, not the installed release.

    ``frontend_dir`` points at ``~/.lemoncrow/install/frontend`` -- a
    released dashboard bundle that predates this checkout's `/review` route.
    Serving it renders dashboard chrome around an empty pane, which reads as a
    broken workspace rather than a stale bundle. Observed live before the fix.
    """

    installed = tmp_path / "installed-frontend"
    installed.mkdir()
    (installed / "index.html").write_text("<html>old dashboard</html>", encoding="utf-8")
    monkeypatch.delenv(ws.BUNDLE_ENV, raising=False)
    monkeypatch.setattr(
        "lemoncrow.infra.runtime.frontend_bundle.frontend_dir",
        lambda: installed,
    )

    checkout = ws._checkout_bundle()
    if not (checkout / "index.html").is_file():
        pytest.skip("no frontend/dist in this checkout; nothing to prefer")
    assert ws.bundle_dir() == checkout.resolve()

    # ... and with no checkout build, the installed bundle is still better than
    # falling all the way back to the static report.
    monkeypatch.setattr(ws, "_checkout_bundle", lambda: tmp_path / "absent")
    assert ws.bundle_dir() == installed.resolve()


def test_the_bundle_mount_serves_the_bundle_and_nothing_above_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A static server pointed at a repository is a file-read primitive."""

    from fastapi.testclient import TestClient

    bundle = tmp_path / "dist"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text("<html>workspace</html>", encoding="utf-8")
    (bundle / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("NOT-FOR-THE-BROWSER", encoding="utf-8")
    monkeypatch.setenv(ws.BUNDLE_ENV, str(bundle))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    port = 45111
    app = ws.build_app(tmp_path / "store", repo_root, token="tok", port=port)
    client = TestClient(app, base_url=f"http://127.0.0.1:{port}")

    page = client.get(ws.WORKSPACE_ROUTE)
    assert page.status_code == 200
    assert "workspace" in page.text
    assert client.get(ws.REVIEWS_ROUTE).status_code == 200
    assert client.get(f"{ws.REVIEWS_ROUTE}/rev-1").status_code == 200
    assert client.get(f"{ws.REVIEWS_ROUTE}/rev-1/revisions/2").status_code == 200
    assert client.get("/assets/app.js").status_code == 200
    for escape in ("/../secret.txt", "/..%2fsecret.txt", "/assets/../../secret.txt"):
        response = client.get(escape)
        assert response.status_code >= 400 or "NOT-FOR-THE-BROWSER" not in response.text


def test_the_bootstrap_url_carries_the_token_in_the_fragment_only(tmp_path: Path) -> None:
    """A fragment never reaches a server, a log, a proxy or ``Referer``."""

    handle = _handle(tmp_path, tmp_path, 4100, token="s3cret")
    url = ws.bootstrap_url(handle, "rev-1")
    before_fragment, _, fragment = url.partition("#")
    assert "s3cret" not in before_fragment
    assert "t=s3cret" in fragment
    assert before_fragment.endswith("/reviews/rev-1")
