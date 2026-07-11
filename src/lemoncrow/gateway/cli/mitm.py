"""Optional mitmproxy integration for LLM conversation capture."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path


def start_mitmdump(flow_path: Path) -> subprocess.Popen[bytes] | None:
    """Start mitmdump in the background; return the process or None if unavailable."""
    if not _mitmdump_available():
        return None
    flow_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            "mitmdump",
            "--save-stream-file",
            str(flow_path),
            "--quiet",
            "--listen-port",
            "8899",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)  # give it a moment to start
    if proc.poll() is not None:
        return None
    # Route litellm/httpx through this proxy.
    os.environ["HTTPS_PROXY"] = "http://localhost:8899"
    os.environ["HTTP_PROXY"] = "http://localhost:8899"
    os.environ["LEMONCROW_MITM_ACTIVE"] = "1"
    return proc


def stop_mitmdump(proc: subprocess.Popen[bytes] | None) -> None:
    """Terminate the mitmdump process and clear proxy environment variables."""
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("LEMONCROW_MITM_ACTIVE", None)


def _mitmdump_available() -> bool:
    return shutil.which("mitmdump") is not None


def flow_path_for_session(session_id: str) -> Path:
    from lemoncrow.core.foundation.paths import default_store_root

    return default_store_root() / "mitm" / f"{session_id}.flow"
