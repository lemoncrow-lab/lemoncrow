"""Feature counts as the ``lc`` command uses them: :mod:`.usage` plus this client's settings and upload."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import TextIO
from urllib.parse import urlsplit

from . import usage
from .config import read_settings_file
from .usage import ENDPOINT, classify

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

__all__ = ["ENDPOINT", "announce", "classify", "flush", "hosted_install", "off_reason", "record", "telemetry_command"]


def hosted_install(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this client is pointed at a server other than the local one; unknown counts as hosted."""

    env = os.environ if environ is None else environ
    try:
        settings = {**read_settings_file(usage.usage_dir(env).parent), **env}
        mode = settings.get("LEMONCROW_INSTALL_MODE", "").strip().lower()
        if mode in {"local", "hosted"}:
            return mode == "hosted"
        url = settings.get("LEMONCROW_URL", "").strip()
        return bool(url) and (urlsplit(url).hostname or "").lower() not in _LOOPBACK_HOSTS
    except Exception:
        return True


def off_reason(environ: Mapping[str, str] | None = None) -> str:
    return usage.off_reason(environ, hosted=hosted_install(environ))


def announce() -> None:
    usage.announce(hosted=hosted_install())


def record(feature: str, *, surface: str, ok: bool, seconds: float) -> None:
    usage.record(feature, surface=surface, ok=ok, seconds=seconds, hosted=hosted_install())


def flush(version: str) -> int:
    def post(url: str, payload: dict[str, object], timeout_s: float) -> int:
        from .usage_upload import post_json

        return post_json(url, payload, timeout_s)

    return usage.flush(version, post, hosted=hosted_install())


def telemetry_command(arguments: Sequence[str], sink: TextIO) -> int:
    """``lc rt telemetry [status|show|remote on|remote off] [--json]`` for an install without the full engine."""

    words = [argument for argument in arguments if argument != "--json"]
    as_json = "--json" in arguments
    if words == ["show"]:
        sink.write(json.dumps(usage.pending(), indent=2, sort_keys=True) + "\n")
        return 0
    if words in (["remote", "on"], ["remote", "off"]):
        if usage.managed():
            sink.write("Telemetry is managed by your organization's policy file; this setting was not changed.\n")
            return 2
        usage.set_enabled(words[1] == "on")
    elif words not in ([], ["status"]):
        sink.write("usage: lc rt telemetry [status|show|remote on|remote off] [--json]\n")
        return 2
    reason = off_reason()
    if as_json:
        payload = {"remote_enabled": not reason, "off_reason": reason, "endpoint": "" if reason else ENDPOINT}
        sink.write(json.dumps(payload, sort_keys=True) + "\n")
    else:
        sink.write(f"remote telemetry: {'on' if not reason else f'off ({reason})'}\n")
        sink.write("See what is waiting to be sent: lc rt telemetry show\n")
    return 0
