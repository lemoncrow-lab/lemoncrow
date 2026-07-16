"""Find the already-running local LemonCrow dashboard."""

from __future__ import annotations

import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from lemoncrow.infra.runtime.daemon_units import DEFAULT_STACK_FRONTEND_PORT


def _dashboard_responds(url: str) -> bool:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "lemoncrow-cli"})
        with urllib.request.urlopen(request, timeout=0.35) as response:
            body = response.read(32_768).lower()
    except (OSError, urllib.error.URLError):
        return False
    return b"<title>lemoncrow dashboard</title>" in body


def _linux_listening_ports() -> set[int]:
    ports: set[int] = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = table.read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            try:
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
            except (IndexError, ValueError):
                continue
    return ports


def _lsof_listening_ports() -> set[int]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "n"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    ports: set[int] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("n") or ":" not in line:
            continue
        try:
            ports.add(int(line.rsplit(":", 1)[1]))
        except ValueError:
            continue
    return ports


def _listening_loopback_ports() -> set[int]:
    ports = _linux_listening_ports()
    return ports or _lsof_listening_ports()


def discover_dashboard_url(root: Path, *, requested_port: int | None = None) -> str | None:
    """Return the verified dashboard URL, including manually selected Vite ports."""

    if requested_port is not None:
        candidates = [f"http://127.0.0.1:{requested_port}"]
    else:
        from lemoncrow.infra.runtime.stack_lifecycle import _stack_status_payload

        status = _stack_status_payload(root)
        configured = os.environ.get("LEMONCROW_FRONTEND_URL", "").strip().rstrip("/")
        candidates = [
            configured,
            str(status.get("frontend_url") or "").rstrip("/"),
            f"http://127.0.0.1:{DEFAULT_STACK_FRONTEND_PORT}",
        ]
        listening = sorted(
            (port for port in _listening_loopback_ports() if 3_000 <= port < 4_000),
            key=lambda port: (abs(port - DEFAULT_STACK_FRONTEND_PORT), port),
        )
        candidates.extend(f"http://127.0.0.1:{port}" for port in listening)

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if _dashboard_responds(candidate):
            return candidate
    return None


__all__ = ["discover_dashboard_url"]
