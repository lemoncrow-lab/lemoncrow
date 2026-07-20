"""Per-workspace singleton MCP daemon + stdio bridge (Q1).

Fast in-process unit tests run in the default gate; the daemon/bridge end-to-end
tests spawn real subprocesses and timing-driven reapers, so they are marked
``slow``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.gateway.adapters import mcp_bridge as mb
from lemoncrow.gateway.adapters import mcp_daemon as md

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
_CORE_TOOLS = {"read", "edit", "code_search", "bash"}


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        cwd=path,
        check=True,
    )
    return path


def _exited(pid: int) -> bool:
    """True if *pid* has terminated, tolerating the zombie a not-yet-reaped child
    leaves behind (``os.kill(pid, 0)`` would still report it alive)."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return True
    except ChildProcessError:
        return True
    except OSError:
        pass
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] == "Z"
    except OSError:
        return True


def _killpg(pid: int) -> None:
    for target in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
        try:
            target()
            return
        except OSError:
            continue


def _cli(env: dict[str, str], *args: str, **kw: Any) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "lemoncrow.gateway.cli", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        **kw,
    )


@pytest.fixture()
def repo_and_root(tmp_path: Path):
    work = _make_repo(tmp_path / "ws")
    root = tmp_path / "root"
    root.mkdir()
    yield work, root
    for reg in md.list_daemons(root):
        _killpg(int(reg["pid"]))


def _bridge_env(work: Path, root: Path, **extra: str) -> dict[str, str]:
    return {
        **os.environ,
        "LEMONCROW_MCP_SINGLETON": "1",
        "LEMONCROW_ROOT": str(root),
        "CLAUDE_WORKSPACE_ROOT": str(work),
        **extra,
    }


# ------------------------------------------------------------------ unit tests


def test_live_sessions_touch_drop_ttl() -> None:
    live = md._LiveSessions()
    assert live.count(ttl=100) == 0
    live.touch("a")
    live.touch("b")
    assert live.count(ttl=100) == 2
    live.drop("a")
    assert live.count(ttl=100) == 1
    live.touch("")  # empty id is ignored
    assert live.count(ttl=100) == 1
    assert live.count(ttl=0) == 0  # zero ttl -> everything stale


def test_registration_roundtrip_perms_and_prune(tmp_path: Path) -> None:
    root = tmp_path
    sock_path = str(md._daemon_socket_path(root, "ws-x"))
    md._write_registration(root, "ws-x", socket_path=sock_path, token="tok", workspace=str(tmp_path))
    reg = md.read_daemon_registration(root, "ws-x")
    assert reg is not None
    assert reg["pid"] == os.getpid()
    assert reg["socket"] == sock_path
    assert reg["token"] == "tok"
    # bearer token file must be owner-only
    assert md.daemon_registration_path(root, "ws-x").stat().st_mode & 0o777 == 0o600

    dead = md.daemon_registration_path(root, "ws-dead")
    dead.write_text(json.dumps({"pid": 2**31 - 1, "socket": "/nonexistent.sock", "token": "t"}))
    assert md.read_daemon_registration(root, "ws-dead") is None  # dead pid -> absent

    assert md.prune_stale_daemons(root) >= 1
    assert not dead.exists()
    assert md.daemon_registration_path(root, "ws-x").exists()  # live pid survives


def test_started_at_preserved_across_rewrites(tmp_path: Path) -> None:
    md._write_registration(tmp_path, "ws", socket_path="/x.sock", token="t", workspace=str(tmp_path))
    first = md.read_daemon_registration(tmp_path, "ws")
    assert first is not None
    time.sleep(0.01)
    md._write_registration(tmp_path, "ws", socket_path="/x.sock", token="t", workspace=str(tmp_path))
    second = md.read_daemon_registration(tmp_path, "ws")
    assert second is not None
    assert second["started_at"] == first["started_at"]  # heartbeat keeps origin
    assert second["last_heartbeat"] >= first["last_heartbeat"]


@pytest.mark.parametrize(
    "val,expected",
    [
        ("1", True),
        ("true", True),
        ("on", True),
        ("", True),  # default ON: only explicit off-values disable it
        ("anything", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
    ],
)
def test_singleton_enabled_defaults_on(monkeypatch: pytest.MonkeyPatch, val: str, expected: bool) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SINGLETON", val)
    assert mb.singleton_enabled() is expected


def test_singleton_enabled_unset_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_MCP_SINGLETON", raising=False)
    assert mb.singleton_enabled() is True


def test_cleanup_commands_for_owner_scopes_and_preserves_bg() -> None:
    """A session disconnecting reaps only its own foreground shells; other owners
    and explicit bg=true jobs survive."""
    from lemoncrow.pro.capabilities.tool_supervision import bash_exec as be

    started: list[dict[str, Any]] = []
    try:
        fg_a = be.start_managed_command("sleep 60", timeout=1, owner="bridge-A")
        fg_b = be.start_managed_command("sleep 60", timeout=1, owner="bridge-B")
        bg_a = be.start_managed_command("sleep 60", timeout=1, owner="bridge-A", explicit_background=True)
        started = [fg_a, fg_b, bg_a]
        assert all(s.get("status") == "running" for s in started)

        summary = be.cleanup_commands_for_owner("bridge-A")
        terminated = {d["pid"] for d in summary["terminated"]}
        preserved = {d["pid"] for d in summary["preserved"]}
        assert fg_a["pid"] in terminated  # this session's foreground shell reaped
        assert bg_a["pid"] in preserved  # explicit bg survives its launching session
        assert fg_b["pid"] not in terminated  # a different session is untouched

        assert be.cleanup_commands_for_owner("") == {"terminated": [], "preserved": []}  # no-op
    finally:
        for s in started:
            try:
                os.killpg(int(s["pid"]), signal.SIGKILL)
            except (OSError, KeyError, ValueError):
                pass


def test_resolve_workspace_prefers_host_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    assert mb._resolve_workspace() == str(tmp_path.resolve())


def test_request_session_context_overrides_resolvers() -> None:
    """The header session-id/host stamped by the HTTP dispatcher must win over the
    daemon's own (wrong) window/env resolution, for every _handle consumer."""
    from lemoncrow.gateway.adapters import mcp_server as m

    base_host = m._detect_agent()
    prior = m._set_request_session("sid-XYZ", "codex-cli")
    try:
        assert m._get_claude_session_id() == "sid-XYZ"
        assert m._detect_agent() == "codex-cli"
    finally:
        m._clear_request_session(prior)
    assert m._get_claude_session_id() != "sid-XYZ"  # not leaked to the global cache
    assert m._detect_agent() == base_host


# ----------------------------------------------------------- integration (slow)


@pytest.mark.slow
def test_daemon_handshake_auth_and_idempotent(repo_and_root: tuple[Path, Path]) -> None:
    work, root = repo_and_root
    reg = md.ensure_daemon(str(work), root, idle_grace_seconds=300.0)
    url = md._UDS_BASE_URL + "/mcp"
    auth = {"Authorization": f"Bearer {reg['token']}", "Content-Type": "application/json"}
    client = md.daemon_client(reg, timeout=20.0)

    # token required
    assert client.post(url, json=_TOOLS).status_code == 403

    init = client.post(url, headers=auth, json=_INIT)
    assert init.json()["result"]["serverInfo"]["name"] == "lemoncrow"
    tools = {t["name"] for t in client.post(url, headers=auth, json=_TOOLS).json()["result"]["tools"]}
    assert _CORE_TOOLS <= tools
    client.close()

    # find-or-spawn is idempotent: same daemon, never a second one
    reg2 = md.ensure_daemon(str(work), root, idle_grace_seconds=300.0)
    assert reg2["pid"] == reg["pid"]
    assert len(md.list_daemons(root)) == 1


@pytest.mark.slow
def test_daemon_idle_reaps_without_sessions(repo_and_root: tuple[Path, Path]) -> None:
    work, root = repo_and_root
    reg = md.ensure_daemon(str(work), root, idle_grace_seconds=5.0)
    deadline = time.time() + 30
    while time.time() < deadline and not _exited(int(reg["pid"])):
        time.sleep(0.5)
    assert _exited(int(reg["pid"]))
    assert md.read_daemon_registration(root, reg["ws_hash"]) is None


@pytest.mark.slow
def test_two_bridges_share_one_daemon(repo_and_root: tuple[Path, Path]) -> None:
    work, root = repo_and_root
    env = _bridge_env(work, root, CLAUDE_CODE_SESSION_ID="sess-share")
    reqs = "\n".join(json.dumps(m) for m in (_INIT, _INITIALIZED, _TOOLS)) + "\n"

    p1 = _cli(env, "mcp")
    try:
        p2 = _cli(env, "mcp")
        out2, _ = p2.communicate(input=reqs, timeout=120)
        p1.communicate(input=reqs, timeout=120)
    finally:
        for p in (p1,):
            if p.poll() is None:
                p.kill()

    by_id = {r.get("id"): r for r in (json.loads(x) for x in out2.splitlines() if x.strip())}
    assert by_id[1]["result"]["serverInfo"]["name"] == "lemoncrow"
    assert _CORE_TOOLS <= {t["name"] for t in by_id[2]["result"]["tools"]}
    assert len(md.list_daemons(root)) == 1  # both bridges collapsed to one daemon


@pytest.mark.slow
def test_attached_idle_session_survives_grace_then_reaps_on_close(repo_and_root: tuple[Path, Path]) -> None:
    work, root = repo_and_root
    reg = md.ensure_daemon(str(work), root, idle_grace_seconds=5.0)
    proc = _cli(_bridge_env(work, root, CLAUDE_CODE_SESSION_ID="sess-live"), "mcp")
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(_INIT) + "\n")
        proc.stdin.write(json.dumps(_INITIALIZED) + "\n")
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["result"]["serverInfo"]["name"] == "lemoncrow"

        # idle far past the 5s grace while still attached -> must NOT reap
        time.sleep(12)
        assert not _exited(int(reg["pid"]))

        # host disconnects -> /session/close -> reap within grace
        proc.stdin.close()
        deadline = time.time() + 25
        reaped = False
        while time.time() < deadline:
            if _exited(int(reg["pid"])):
                reaped = True
                break
            time.sleep(0.5)
        assert reaped
    finally:
        if proc.poll() is None:
            proc.kill()
