"""Find the web UI served by the configured LemonCrow loopback server."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_LOOPBACK_URL = "http://127.0.0.1:7420"


def _request_ok(url: str) -> bool:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "lemoncrow-cli"})
        with urllib.request.urlopen(request, timeout=0.5) as response:
            return 200 <= response.status < 400
    except (OSError, urllib.error.URLError):
        return False


def _dashboard_responds(url: str) -> bool:
    return _request_ok(url.rstrip("/") + "/")


def _server_healthy(url: str) -> bool:
    return _request_ok(url.rstrip("/") + "/healthz")


def discover_dashboard_url(root: Path, *, requested_port: int | None = None) -> str | None:
    """Return the verified UI base URL for the one local loopback server."""
    del root  # retained for CLI call-site compatibility
    if requested_port is not None:
        candidate = f"http://127.0.0.1:{requested_port}"
        return candidate if _server_healthy(candidate) or _dashboard_responds(candidate) else None

    explicit_frontend = os.environ.get("LEMONCROW_FRONTEND_URL", "").strip().rstrip("/")
    if explicit_frontend and _dashboard_responds(explicit_frontend):
        return explicit_frontend

    endpoint = (os.environ.get("LEMONCROW_URL") or DEFAULT_LOOPBACK_URL).strip().rstrip("/")
    return endpoint if _server_healthy(endpoint) else None


__all__ = ["DEFAULT_LOOPBACK_URL", "discover_dashboard_url"]
