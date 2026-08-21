"""``lc mcp serve`` CLI — tunnel auto-launch, cloudflared auto-install, no-auth,
user-defined client, and the deprecated ``lc chatgpt`` alias.

None of these tests require cloudflared or the network: the spawn/download
points (``shutil.which`` / ``subprocess.Popen`` / ``_download_cloudflared``) and
``uvicorn.run`` are monkeypatched, and the pure helpers (URL extraction, asset
naming) are fed canned inputs.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from click.testing import CliRunner, Result

from lemoncrow.gateway.cli.commands import mcp_serve as mcp_serve_mod
from lemoncrow.gateway.cli.commands.mcp_serve import (
    _cloudflared_asset_name,
    _extract_tunnel_url,
    _watch_tunnel_process,
    mcp_client_cmd,
    mcp_serve_cmd,
)

# Real cloudflared quick-tunnel stderr shape: the URL sits inside an ASCII box.
_REAL_BANNER_LINES = [
    "2026-07-22T10:00:00Z INF +--------------------------------------------------------------------------------------------+",
    "2026-07-22T10:00:00Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |",
    "2026-07-22T10:00:00Z INF |  https://liquid-marmot-example-words.trycloudflare.com                                     |",
    "2026-07-22T10:00:00Z INF +--------------------------------------------------------------------------------------------+",
]


# ── URL extraction (pure function) ─────────────────────────────────────────────
def test_extract_url_from_real_banner_line() -> None:
    urls = [_extract_tunnel_url(line) for line in _REAL_BANNER_LINES]
    assert urls == [None, None, "https://liquid-marmot-example-words.trycloudflare.com", None]


def test_extract_url_ignores_control_plane_host() -> None:
    line = (
        "2026-07-22T10:00:00Z ERR failed to request quick Tunnel: "
        "POST https://api.trycloudflare.com/tunnel: 429 Too Many Requests"
    )
    assert _extract_tunnel_url(line) is None


def test_extract_url_ignores_unrelated_urls_and_noise() -> None:
    assert (
        _extract_tunnel_url(
            "2026-07-22T10:00:00Z INF Thank you for trying Cloudflare Tunnel. ... "
            "https://developers.cloudflare.com/cloudflare-one/connections/connect-apps"
        )
        is None
    )
    assert _extract_tunnel_url("2026-07-22T10:00:00Z INF Registered tunnel connection connIndex=0") is None
    assert _extract_tunnel_url("") is None


# ── Release asset naming (pure function) ───────────────────────────────────────
def test_cloudflared_asset_name_mapping() -> None:
    assert _cloudflared_asset_name("Linux", "x86_64") == "cloudflared-linux-amd64"
    assert _cloudflared_asset_name("Linux", "aarch64") == "cloudflared-linux-arm64"
    assert _cloudflared_asset_name("Linux", "arm64") == "cloudflared-linux-arm64"
    # darwin ships only as .tgz archives on GitHub releases.
    assert _cloudflared_asset_name("Darwin", "arm64") == "cloudflared-darwin-arm64.tgz"
    assert _cloudflared_asset_name("Darwin", "x86_64") == "cloudflared-darwin-amd64.tgz"
    assert _cloudflared_asset_name("Windows", "AMD64") is None
    assert _cloudflared_asset_name("Linux", "riscv64") is None


# ── Binary resolution ──────────────────────────────────────────────────────────
def test_resolve_cloudflared_prefers_path_then_managed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    managed = tmp_path / ".lemoncrow" / "chatgpt" / "bin" / "cloudflared"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"#!/bin/sh\n")
    managed.chmod(0o755)

    monkeypatch.setattr(mcp_serve_mod.shutil, "which", lambda name: "/usr/local/bin/cloudflared")
    assert mcp_serve_mod._resolve_cloudflared() == "/usr/local/bin/cloudflared"

    monkeypatch.setattr(mcp_serve_mod.shutil, "which", lambda name: None)
    assert mcp_serve_mod._resolve_cloudflared() == str(managed)

    managed.unlink()
    assert mcp_serve_mod._resolve_cloudflared() is None


# ── CLI behavior (no cloudflared, no network) ──────────────────────────────────
def _invoke_serve(args: list[str], input: str | None = None) -> Result:
    # click >= 8.2: result.output interleaves stdout and stderr by default.
    return CliRunner().invoke(mcp_serve_cmd, ["--pairing-code", "x", *args], input=input)


class _FakeTunnelProc:
    """Stand-in for the cloudflared Popen handle; records cleanup calls.

    Mirrors real ``subprocess.Popen`` lifecycle, not just its method names:
    ``wait()`` blocks until ``terminate()``/``kill()`` actually ends the
    "process" (an ``Event``, not an immediate return of 0), and
    ``returncode`` stays ``None`` until then. A fake that returns from
    ``wait()`` before anything told it to exit made the tunnel-watchdog
    thread in mcp_serve.py race the test's own shutdown path.
    """

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.returncode: int | None = None
        self._exited = threading.Event()

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0
        self._exited.set()

    def wait(self, timeout: float | None = None) -> int:
        if not self._exited.wait(timeout):
            assert timeout is not None  # Event.wait(None) never times out
            raise subprocess.TimeoutExpired(cmd="cloudflared", timeout=timeout)
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
        self._exited.set()


class _FakeServer:
    """Stand-in for ``uvicorn.Server``: only ``should_exit`` matters here."""

    def __init__(self) -> None:
        self.should_exit = False


# ── tunnel watchdog: this is the actual fix (mcp_serve.py:_watch_tunnel_process) ──
# cloudflared used to be able to die silently while the local server (and the
# systemd unit wrapping it) kept reporting healthy, leaving the public URL
# 502ing until someone noticed and restarted by hand. These two tests pin the
# watchdog's whole job: stop the server on an *unannounced* tunnel death, but
# stay out of the way of a *deliberate* one (our own shutdown terminating it).
def test_watch_tunnel_process_stops_server_on_unexpected_death(capsys: pytest.CaptureFixture[str]) -> None:
    proc = _FakeTunnelProc()
    proc.kill()  # cloudflared died on its own — edge disconnect, OOM, whatever
    server = _FakeServer()
    shutting_down = threading.Event()

    _watch_tunnel_process(proc, server, shutting_down)  # type: ignore[arg-type]

    assert server.should_exit is True
    assert "exited unexpectedly" in capsys.readouterr().err


def test_watch_tunnel_process_stays_quiet_on_deliberate_shutdown(capsys: pytest.CaptureFixture[str]) -> None:
    proc = _FakeTunnelProc()
    server = _FakeServer()
    shutting_down = threading.Event()
    shutting_down.set()  # our own `finally:` block is already tearing things down
    proc.terminate()  # ... which is what calls this

    _watch_tunnel_process(proc, server, shutting_down)  # type: ignore[arg-type]

    assert server.should_exit is False
    assert capsys.readouterr().err == ""


def test_no_tunnel_never_probes_cloudflared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    probes: list[str] = []
    monkeypatch.setattr(mcp_serve_mod.shutil, "which", lambda *a, **k: probes.append("which"))
    monkeypatch.setattr(mcp_serve_mod.subprocess, "Popen", lambda *a, **k: probes.append("popen"))
    monkeypatch.setattr(mcp_serve_mod, "_download_cloudflared", lambda dest: probes.append("download"))
    served: list[bool] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: served.append(True))

    result = _invoke_serve(["--no-tunnel"])
    assert result.exit_code == 0, result.output
    assert probes == []
    assert served == [True]


def test_missing_cloudflared_noninteractive_aborts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No binary + no TTY to answer the download prompt → exit 1 with the link."""
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr(mcp_serve_mod, "_resolve_cloudflared", lambda: None)
    downloads: list[Path] = []
    monkeypatch.setattr(mcp_serve_mod, "_download_cloudflared", lambda dest: downloads.append(dest))
    served: list[bool] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: served.append(True))

    result = _invoke_serve([])  # no input → click.confirm hits EOF → Abort
    assert result.exit_code == 1
    assert "developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads" in result.output
    assert "run `uv run lemoncrow mcp serve` again" in result.output
    assert downloads == []
    assert served == []


def test_declined_download_exits_with_link(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr(mcp_serve_mod, "_resolve_cloudflared", lambda: None)
    downloads: list[Path] = []
    monkeypatch.setattr(mcp_serve_mod, "_download_cloudflared", lambda dest: downloads.append(dest))
    served: list[bool] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: served.append(True))

    result = _invoke_serve([], input="n\n")
    assert result.exit_code == 1
    assert "Download it now" in result.output
    assert "developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads" in result.output
    assert downloads == []
    assert served == []


def test_accepted_download_installs_then_serves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr(mcp_serve_mod, "_resolve_cloudflared", lambda: None)
    managed = tmp_path / ".lemoncrow" / "chatgpt" / "bin" / "cloudflared"
    downloads: list[Path] = []

    def _fake_download(dest: Path) -> str:
        downloads.append(dest)
        return str(dest)

    monkeypatch.setattr(mcp_serve_mod, "_download_cloudflared", _fake_download)
    proc = _FakeTunnelProc()
    tunnel_binaries: list[str] = []

    def _fake_start(binary: str, port: int, timeout: float = 30.0) -> tuple[Any, str]:
        tunnel_binaries.append(binary)
        return proc, "https://foo.trycloudflare.com"

    monkeypatch.setattr(mcp_serve_mod, "_start_tunnel", _fake_start)
    served: list[bool] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: served.append(True))

    result = _invoke_serve([], input="y\n")
    assert result.exit_code == 0, result.output
    assert downloads == [managed]
    assert tunnel_binaries == [str(managed)]  # serve proceeds with the managed path
    assert served == [True]
    assert "✓ tunnel up" in result.output


def test_tunnel_url_printed_and_proc_cleaned_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr(mcp_serve_mod, "_resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    proc = _FakeTunnelProc()
    monkeypatch.setattr(
        mcp_serve_mod,
        "_start_tunnel",
        lambda binary, port, timeout=30.0: (proc, "https://foo.trycloudflare.com"),
    )
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)

    result = _invoke_serve([])
    assert result.exit_code == 0, result.output
    assert "MCP server URL:  https://foo.trycloudflare.com/mcp" in result.output
    assert "quick-tunnel URL rotates" in result.output
    # The manual "expose it through a tunnel" step is dropped when the URL is known.
    assert "Expose it through a tunnel" not in result.output
    # try/finally must take the tunnel down even on a clean exit.
    assert proc.terminated


def test_tunnel_timeout_warns_and_still_serves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr(mcp_serve_mod, "_resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    proc = _FakeTunnelProc()
    monkeypatch.setattr(mcp_serve_mod, "_start_tunnel", lambda binary, port, timeout=30.0: (proc, None))
    served: list[bool] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: served.append(True))

    result = _invoke_serve([])
    assert result.exit_code == 0, result.output
    assert "no tunnel URL" in result.output
    assert "Expose it through a tunnel" in result.output  # manual fallback steps
    assert served == [True]
    assert proc.terminated


# ── --no-auth mode ─────────────────────────────────────────────────────────────
def test_no_auth_serves_open_mcp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    captured_apps: list[Any] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: captured_apps.append(self.config.app))

    result = CliRunner().invoke(mcp_serve_cmd, ["--no-auth", "--no-tunnel"])
    assert result.exit_code == 0, result.output
    assert "Authentication:  None (no auth)" in result.output
    assert "NO AUTHENTICATION" in result.output
    assert "Pairing code" not in result.output

    resp = TestClient(captured_apps[0]).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["jsonrpc"] == "2.0"
    assert resp.json()["result"]["serverInfo"]["name"]


def test_no_auth_conflicts_with_pairing_code_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)
    for extra in (["--pairing-code", "x"], ["--reset"]):
        result = CliRunner().invoke(mcp_serve_cmd, ["--no-auth", "--no-tunnel", *extra])
        assert result.exit_code != 0
        assert "cannot be combined" in result.output


# ── user-defined client (`lc chatgpt client`) ──────────────────────────────────
def _state_clients(tmp_path: Path) -> dict[str, Any]:
    state = json.loads((tmp_path / ".lemoncrow" / "chatgpt" / "oauth.json").read_text(encoding="utf-8"))
    clients: dict[str, Any] = state["clients"]
    return clients


def test_client_command_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    first = CliRunner().invoke(mcp_client_cmd, [])
    assert first.exit_code == 0, first.output

    clients = _state_clients(tmp_path)
    assert len(clients) == 1
    client_id, record = next(iter(clients.items()))
    assert record["user_defined"] is True
    assert set(record["redirect_uris"]) == {
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://chat.openai.com/connector_platform_oauth_redirect",
    }
    assert client_id in first.output
    assert "leave empty" in first.output

    second = CliRunner().invoke(mcp_client_cmd, [])
    assert second.exit_code == 0, second.output
    assert client_id in second.output  # same ID, not a new registration
    assert len(_state_clients(tmp_path)) == 1


def test_client_command_rejects_non_https_redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    result = CliRunner().invoke(mcp_client_cmd, ["--redirect-uri", "http://evil.example.com/cb"])
    assert result.exit_code != 0
    assert "must be https" in result.output


# ── command wiring (`lc mcp serve` is the name; `lc chatgpt` still resolves) ──
def test_remote_transport_hangs_off_the_mcp_group() -> None:
    """The remote transport must be reachable as ``lc mcp serve``/``lc mcp client``.

    Registration lives in ``mcp.py`` while the implementation lives here, so a
    missing ``add_command`` would silently drop the whole feature from the CLI
    with every unit test above still green.
    """
    from lemoncrow.gateway.cli.commands.mcp import mcp_group

    assert mcp_group.commands["serve"] is mcp_serve_cmd
    assert mcp_group.commands["client"] is mcp_client_cmd


def test_chatgpt_alias_still_runs_and_says_it_is_deprecated() -> None:
    result = CliRunner().invoke(mcp_serve_mod.chatgpt_group, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "client" in result.output

    from lemoncrow.gateway.cli.app import cli

    assert cli.commands["chatgpt"].hidden is True
