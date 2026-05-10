"""Anonymous product telemetry identity helpers."""

from __future__ import annotations

import os
import platform
import sys
import uuid
from contextlib import suppress
from pathlib import Path


def config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "atelier"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "atelier"


def telemetry_id_path() -> Path:
    return Path(os.environ.get("ATELIER_TELEMETRY_ID_PATH", config_dir() / "telemetry_id"))


def get_anon_id() -> str:
    path = telemetry_id_path()
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            uuid.UUID(value)
            return value
    except Exception:
        pass
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
