"""Anonymous product telemetry identity helpers."""

from __future__ import annotations

import logging
import os
import platform
import sys
import uuid
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)


def config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "lemoncrow"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lemoncrow"


def telemetry_id_path() -> Path:
    return Path(os.environ.get("LEMONCROW_TELEMETRY_ID_PATH", config_dir() / "telemetry_id"))


def get_anon_id() -> str:
    path = telemetry_id_path()
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            uuid.UUID(value)
            return value
    except Exception:
        logging.exception("Recovered from broad exception handler")
        logger.warning(
            "Suppressed exception at identity.py:32",
            exc_info=True,
        )
    anon_id = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(anon_id + "\n", encoding="utf-8")
    with suppress(OSError):
        os.chmod(path, 0o600)
    return anon_id


def reset_anon_id() -> str:
    path = telemetry_id_path()
    with suppress(FileNotFoundError):
        path.unlink()
    return get_anon_id()


def new_session_id() -> str:
    return str(uuid.uuid4())


def platform_payload() -> dict[str, str]:
    return {
        "os": platform.system().lower() or "unknown",
        "py_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    }
