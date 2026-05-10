"""First-run product telemetry disclosure banner."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

from atelier.core.foundation.identity import config_dir

FALSE_VALUES = {"0", "false", "off", "no"}

BANNER_TEXT = (
    "Atelier collects anonymous usage telemetry to improve the product.\n"
    "Disable any time:  atelier telemetry off  |  ATELIER_TELEMETRY=0\n"
    "What's collected:  atelier telemetry show  (or open the Insights tab)\n"
    "Privacy details:   https://atelier.dev/telemetry\n"
)


def ack_path() -> Path:
    return Path(os.environ.get("ATELIER_TELEMETRY_ACK", config_dir() / "telemetry_ack"))


def is_acknowledged() -> bool:
    return ack_path().exists()


def mark_acknowledged() -> None:
    path = ack_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("acknowledged\n", encoding="utf-8")


def _telemetry_disabled_via_env() -> bool:
    """Check whether ATELIER_TELEMETRY is explicitly set to a false value."""
    val = os.environ.get("ATELIER_TELEMETRY")
    return val is not None and val.strip().lower() in FALSE_VALUES


def maybe_show_banner(stream: TextIO | None = None) -> bool:
    stream = stream or sys.stderr
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    # If telemetry is explicitly disabled via env var, auto-acknowledge
    # so neither the CLI banner nor the frontend banner ever reappears.
    if _telemetry_disabled_via_env():
        mark_acknowledged()
        return False
    if is_acknowledged():
        return False
    if not stream.isatty():
        # Non-interactive context (e.g. MCP server subprocess): write the ack
        # silently so the banner doesn't reappear in the frontend or in future
        # interactive CLI sessions.
        mark_acknowledged()
        return False
    stream.write(BANNER_TEXT + "\n")
    stream.flush()
    mark_acknowledged()
    return True
