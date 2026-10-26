"""First-run product telemetry disclosure banner."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

from lemoncrow.core.foundation.identity import config_dir
from lemoncrow.core.service.telemetry.config import resolve_destination

#: One line, once: that it is on, what it sends, and the command that turns it off. Only the full engine (a source
#: checkout's ``lc``) has telemetry; the installed client sends none (``lc audit`` says so).
BANNER_TEXT = (
    "Telemetry is on: LemonCrow sends anonymous counts and timings, never code, prompts or file paths. "
    "Turn it off: lc rt telemetry remote off (or DO_NOT_TRACK=1)."
)


def ack_path() -> Path:
    return Path(os.environ.get("LEMONCROW_TELEMETRY_ACK", config_dir() / "telemetry_ack"))


def is_acknowledged() -> bool:
    return ack_path().exists()


def mark_acknowledged() -> None:
    path = ack_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("acknowledged\n", encoding="utf-8")


def maybe_show_banner(stream: TextIO | None = None, *, announce: bool = False) -> bool:
    """Print :data:`BANNER_TEXT` to *stream* (stderr) the first time, and remember that it was shown.

    A terminal always gets it. Anything else (a pipe, a log, a host-spawned subprocess) is acknowledged silently so a
    machine-run command never prints it later, unless *announce* says a person ran this command: then it is printed
    wherever stderr goes, because the first run of the full engine is the one place the person can be told.

    Nothing is printed, and nothing is acknowledged, while telemetry is off (``DO_NOT_TRACK=1``,
    ``LEMONCROW_TELEMETRY=off``, ``lc rt telemetry remote off`` or an administrator policy): "Telemetry is on" would be
    untrue, and it is the Action's job log that would carry it.
    """

    stream = stream or sys.stderr
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if is_acknowledged():
        return False
    if not resolve_destination().enabled:
        return False
    if not announce and not stream.isatty():
        # Non-interactive context (e.g. MCP server subprocess): write the ack
        # silently so the banner doesn't reappear in the frontend or in future
        # interactive CLI sessions.
        mark_acknowledged()
        return False
    stream.write(BANNER_TEXT + "\n")
    stream.flush()
    try:
        mark_acknowledged()
    except OSError:
        pass  # a read-only home: the line shows again next time, which is honest
    return True
