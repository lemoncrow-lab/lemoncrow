"""Open a URL in the person's graphical browser, never in the terminal.

Python's ``webbrowser`` falls back to console browsers (lynx, w3m, links) when
no desktop session exists, which takes over the terminal of an SSH session or
writes screen-control codes into redirected output. Every LemonCrow command
that opens a page goes through :func:`launch_browser_url` instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping

_LINUX_GUI_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "firefox",
    "brave-browser",
    "brave-browser-stable",
    "microsoft-edge",
    "vivaldi",
)


def _spawn(argv: list[str]) -> bool:
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def launch_browser_url(url: str) -> bool:
    """Hand *url* to a graphical browser; False when none can take it.

    A successful launch, not the opener's exit status, is the contract: desktop
    helpers exit as soon as they pass the URL to an already-running browser.
    ``LEMONCROW_NO_BROWSER=1`` never launches anything (tests, CI, headless use).
    """

    if os.environ.get("LEMONCROW_NO_BROWSER", "").strip() not in {"", "0"}:
        return False
    if sys.platform == "darwin":
        opener = shutil.which("open")
        return bool(opener and _spawn([opener, url]))
    if os.name == "nt":
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    # Linux and the other Unixes: only a desktop session has a graphical browser.
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    opener = shutil.which("xdg-open")
    if opener and _spawn([opener, url]):
        return True
    for name in _LINUX_GUI_BROWSERS:
        executable = shutil.which(name)
        if executable and _spawn([executable, url]):
            return True
    return False


def open_by_hand(url: str, armed: object = None) -> str:
    """The line to print when no browser opened *url*.

    *armed* is the local server's pairing reply. A local Review link connects only a
    browser that opens it within ``expires_in`` seconds, so the line says how long.
    """

    seconds = armed.get("expires_in") if isinstance(armed, Mapping) else None
    if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0:
        return (
            f"(could not open a browser. Open {url} within {seconds} seconds, "
            "or run `lc review --open` again when you are ready.)"
        )
    return f"(could not open a browser; open {url} manually.)"
