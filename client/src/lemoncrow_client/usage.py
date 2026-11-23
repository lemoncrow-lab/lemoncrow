"""Anonymous feature counts: which LemonCrow features are used, how often, and whether they worked.

On by default, announced once on standard error before anything is counted, and off with ``lc rt telemetry remote off``,
``DO_NOT_TRACK=1`` or ``LEMONCROW_TELEMETRY=off``. A count is a capability id from a fixed list, where it ran, ok or not,
and a duration bucket. Arguments, paths, repository names, code, prompts and findings are never read into one.

Counts wait in ``<state>/usage/<UTC day>.jsonl`` until the day is over. The first counted command of a later day posts
them (:mod:`.usage_cli`, a short blocking request: this client runs nothing in the background) and deletes the file.
Turning telemetry off deletes what is waiting. Python standard library only; the Review engine imports this module for
the Reader's features, so it imports nothing else from the client.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import tomllib
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, TextIO

NOTICE: Final[str] = (
    "Telemetry is on: LemonCrow sends anonymous counts and timings, never code, prompts or file paths. "
    "Turn it off: lc rt telemetry remote off (or DO_NOT_TRACK=1)."
)
EVENT: Final[str] = "feature_used"
ENDPOINT: Final[str] = "https://lemoncrow.com/api/telemetry/events"
ENDPOINT_ENV: Final[str] = "LEMONCROW_EVENT_TELEMETRY_ENDPOINT"
SURFACES: Final[frozenset[str]] = frozenset({"cli", "pr", "reader"})
#: Every id is a capability in ``docs/status/capabilities.toml`` (a test holds this list to it).
FEATURES: Final[frozenset[str]] = frozenset(
    {
        "data-flow",
        "review-action-init",
        "review-analytics",
        "review-ask",
        "review-browser-checks",
        "review-check-runs",
        "review-config-import",
        "review-data-path",
        "review-discussion-summaries",
        "review-interactive-preview",
        "review-local",
        "review-local-ai",
        "review-map",
        "review-part-proof",
        "review-premerge-checks",
        "review-proof",
        "review-record-signing",
        "review-reproduce",
        "review-rule-suggestions",
        "review-runtime-validation",
        "runtime-lite",
        "runtime-lite-member-repositories",
    }
)
#: Reader friction counts (``review-friction-metrics-deferred``): how often, never about what. The same row, the same
#: off switch and the same waiting file as a feature; a wait's length is only its duration bucket.
FRICTION_METRICS: Final[frozenset[str]] = frozenset(
    {
        "review-friction-blocked-reason",
        "review-friction-map-open-with-delta",
        "review-friction-map-open-without-delta",
        "review-friction-settings-before-first-review",
        "review-friction-wait-author",
        "review-friction-wait-checks",
        "review-friction-wait-reviewer",
    }
)
_COUNTED: Final[frozenset[str]] = FEATURES | FRICTION_METRICS
_REVIEW_COMMANDS: Final[Mapping[str, str]] = {
    "init-action": "review-action-init",
    "proof": "review-proof",
    "qa": "review-browser-checks",
    "data-flow": "data-flow",
    "record-key": "review-record-signing",
}
#: Polled by agents and hooks, or plumbing: counting them would measure the polling.
_UNCOUNTED_REVIEW_COMMANDS: Final[frozenset[str]] = frozenset({"status", "service", "hook", "config"})
_HELP: Final[frozenset[str]] = frozenset({"-h", "--help", "help", "--version"})
_READER_ROUTES: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = tuple(
    (method, re.compile(rf"^/(?:api|v1)/reviews/[^/]+/{tail}$"), feature)
    for method, tail, feature in (
        ("POST", r"ask(?:/stream)?", "review-ask"),
        ("GET", r"(?:revisions/[^/]+/)?map", "review-map"),
        ("POST", r"ai-review(?:/fresh)?", "review-local-ai"),
        ("POST", r"check-recipes/run", "review-check-runs"),
        ("POST", r"data-path/run", "review-data-path"),
        ("POST", r"part-proof/run", "review-part-proof"),
        ("POST", r"premerge/evaluate", "review-premerge-checks"),
        ("POST", r"runtime-validation", "review-runtime-validation"),
        ("POST", r"interactive-preview/sessions", "review-interactive-preview"),
        ("POST", r"threads/[^/]+/summary", "review-discussion-summaries"),
        ("POST", r"rule-suggestions/[^/]+/adopt", "review-rule-suggestions"),
    )
)
_DURATION_BUCKETS: Final[tuple[tuple[float, str], ...]] = (
    (1, "<1"),
    (10, "1-10"),
    (60, "10-60"),
    (300, "60-300"),
    (1800, "300-1800"),
)
_DAY_FILE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")
_RETENTION_DAYS: Final[int] = 30
_MAX_ROWS: Final[int] = 500
_MAX_DAYS_PER_FLUSH: Final[int] = 3
#: lc-debt: a day past this size stops counting (about 12,000 uses); upgrade path: fold lines into a counter file.
_MAX_DAY_BYTES: Final[int] = 1024 * 1024
_POST_TIMEOUT_S: Final[float] = 1.5
_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "on", "yes"})
_FALSE: Final[frozenset[str]] = frozenset({"0", "false", "off", "no"})


def classify(arguments: Sequence[str]) -> tuple[str, str] | None:
    """``(feature, surface)`` for a command a person runs, from the verb and flag names only; ``None`` is not counted."""

    if not arguments or any(argument in _HELP for argument in arguments):
        return None
    verb, rest = arguments[0], list(arguments[1:])
    if verb == "with":
        return "runtime-lite", "cli"
    if verb == "workspace":
        return "runtime-lite-member-repositories", "cli"
    if verb in {"rt", "runtime"}:
        return ("runtime-lite", "cli") if rest[:1] == ["index"] else None
    if verb != "review":
        return None
    first = rest[0] if rest else ""
    if first == "config" and rest[1:2] == ["import"]:
        return "review-config-import", "cli"
    if first == "hook" and rest[1:2] == ["install"]:
        return "review-local-ai", "cli"
    if first in _UNCOUNTED_REVIEW_COMMANDS:
        return None
    if first in _REVIEW_COMMANDS:
        return _REVIEW_COMMANDS[first], "cli"
    flags = {argument.split("=", 1)[0] for argument in rest if argument.startswith("--")}
    if "--reproduce" in flags:
        return "review-reproduce", "cli"
    surface = "pr" if "--pr" in flags else "cli"
    return ("review-local-ai" if "--ai" in flags else "review-local"), surface


def classify_reader_request(method: str, path: str) -> str | None:
    """The feature one local server request uses; ``None`` for everything else, polling and page loads included."""

    if method == "GET" and path == "/api/review-analytics":
        return "review-analytics"
    for wanted, pattern, feature in _READER_ROUTES:
        if method == wanted and pattern.match(path):
            return feature
    return None


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _state_dir(environ: Mapping[str, str]) -> Path:
    home = environ.get("LEMONCROW_HOME", "").strip()
    return Path(home) if home else Path(environ.get("HOME") or str(Path.home())) / ".lemoncrow"


def usage_dir(environ: Mapping[str, str] | None = None) -> Path:
    return _state_dir(_environment(environ)) / "usage"


def _config_dir(environ: Mapping[str, str]) -> Path:
    if sys.platform.startswith("win"):
        return Path(environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")) / "lemoncrow"
    return Path(environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "lemoncrow"


def _config_path(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("LEMONCROW_TELEMETRY_CONFIG") or _config_dir(environ) / "telemetry.toml")


def _ack_path(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("LEMONCROW_TELEMETRY_ACK") or _config_dir(environ) / "telemetry_ack")


def _id_path(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("LEMONCROW_TELEMETRY_ID_PATH") or _config_dir(environ) / "telemetry_id")


def _policy_path(environ: Mapping[str, str]) -> Path:
    if sys.platform.startswith("win"):
        system = Path(environ.get("ProgramData") or r"C:\ProgramData") / "LemonCrow" / "telemetry.toml"
    elif sys.platform == "darwin":
        system = Path("/Library/Application Support/LemonCrow/telemetry.toml")
    else:
        system = Path("/etc/lemoncrow/telemetry.toml")
    override = environ.get("LEMONCROW_TELEMETRY_POLICY", "").strip()
    return Path(override) if override and not system.exists() else system


def _section(path: Path) -> dict[str, Any] | None:
    """The ``[telemetry]`` table; ``{}`` for no file, ``None`` for a file that cannot be read."""

    try:
        section = tomllib.loads(path.read_text(encoding="utf-8")).get("telemetry", {})
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    return section if isinstance(section, dict) else {}


def off_reason(environ: Mapping[str, str] | None = None, *, hosted: bool = False) -> str:
    """Why nothing is counted, ``""`` when counting is allowed. Anything unreadable turns it off.

    *hosted* comes from the caller: the client reads its settings (:mod:`.usage_cli`), and the loopback Review server,
    which is local by definition, leaves it false.
    """

    env = _environment(environ)
    if env.get("PYTEST_CURRENT_TEST") and env.get("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS") != "1":
        return "test run"
    if env.get("DO_NOT_TRACK", "").strip().lower() in _TRUE:
        return "DO_NOT_TRACK"
    if env.get("LEMONCROW_TELEMETRY", "").strip().lower() in _FALSE:
        return "LEMONCROW_TELEMETRY"
    if env.get("CI", "").strip().lower() not in {"", *_FALSE}:
        return "CI"  # nobody reads a first-run notice in a job log
    policy_path = _policy_path(env)
    policy = _section(policy_path)
    if policy is None:
        return "administrator policy"
    if policy_path.exists() and "destination" in policy and policy.get("destination") != "lemoncrow":
        return "administrator policy"
    saved = _section(_config_path(env))
    if saved is None:
        return "unreadable telemetry.toml"
    if saved.get("remote_enabled") is False or saved.get("destination") == "off":
        return "turned off"
    if saved.get("destination") not in (None, "", "lemoncrow"):
        return "another destination is configured"
    if hosted:
        return "hosted install"
    return ""


def acknowledged(environ: Mapping[str, str] | None = None) -> bool:
    return _ack_path(_environment(environ)).exists()


def announce(stream: TextIO | None = None, environ: Mapping[str, str] | None = None, *, hosted: bool = False) -> None:
    """Say once that telemetry is on. Nothing is counted until this has been said and remembered."""

    env = _environment(environ)
    if off_reason(env, hosted=hosted) or acknowledged(env):
        return
    sink = stream if stream is not None else sys.stderr
    sink.write(NOTICE + "\n")
    sink.flush()
    path = _ack_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("acknowledged\n", encoding="utf-8")


def _bucket(seconds: float) -> str:
    for limit, label in _DURATION_BUCKETS:
        if seconds < limit:
            return label
    return ">1800"


def _today(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")


def record(
    feature: str,
    *,
    surface: str,
    ok: bool,
    seconds: float,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
    hosted: bool = False,
) -> None:
    """Count one use of *feature*. Never raises: counting must not change what the command did."""

    env = _environment(environ)
    with contextlib.suppress(Exception):
        if feature not in _COUNTED or surface not in SURFACES:
            return
        if off_reason(env, hosted=hosted) or not acknowledged(env):
            return
        directory = usage_dir(env)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{_today(now)}.jsonl"
        if path.exists() and path.stat().st_size >= _MAX_DAY_BYTES:
            return
        line = json.dumps(
            {"f": feature, "s": surface, "o": "ok" if ok else "error", "b": _bucket(seconds)}, separators=(",", ":")
        )
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, (line + "\n").encode("ascii"))
        finally:
            os.close(descriptor)


def _day_files(environ: Mapping[str, str]) -> list[Path]:
    try:
        return sorted(path for path in usage_dir(environ).iterdir() if _DAY_FILE.match(path.name))
    except OSError:
        return []


def rows_for(path: Path) -> list[dict[str, Any]]:
    """The rows one day's file becomes. Only known features and labels survive, whatever the file holds."""

    counts: Counter[tuple[str, str, str, str]] = Counter()
    buckets = {label for _limit, label in _DURATION_BUCKETS} | {">1800"}
    for line in path.read_text(encoding="ascii", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        key = (item.get("f"), item.get("s"), item.get("o"), item.get("b"))
        if key[0] in _COUNTED and key[1] in SURFACES and key[2] in {"ok", "error"} and key[3] in buckets:
            counts[key] += 1  # type: ignore[index]
    return [
        {"event": EVENT, "dim": feature, "context": surface, "outcome": outcome, "bucket": bucket, "n": n}
        for (feature, surface, outcome, bucket), n in counts.most_common(_MAX_ROWS)
    ]


def pending(environ: Mapping[str, str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """What is waiting to be sent, by day: exactly the rows a flush would post."""

    env = _environment(environ)
    return {path.stem: rows_for(path) for path in _day_files(env)}


def discard_pending(environ: Mapping[str, str] | None = None) -> None:
    for path in _day_files(_environment(environ)):
        path.unlink(missing_ok=True)


def _install_key(environ: Mapping[str, str]) -> str:
    """One-way, and different from the full engine's key: the server replaces an install's rows for a day, so the two
    senders must not share one."""

    path = _id_path(environ)
    try:
        value = str(uuid.UUID(path.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        value = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
    return hashlib.sha256(f"lemoncrow-usage:{value}".encode()).hexdigest()


def flush(
    version: str,
    post: Callable[[str, dict[str, Any], float], int],
    *,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
    hosted: bool = False,
) -> int:
    """Send finished days, at most one attempt a day; the number of days sent. Never raises.

    The server replaces an install's rows for a day, so a day sent twice is not counted twice.
    """

    env = _environment(environ)
    sent = 0
    with contextlib.suppress(Exception):
        if off_reason(env, hosted=hosted):
            discard_pending(env)
            return 0
        if not acknowledged(env):
            return 0
        today = _today(now)
        directory = usage_dir(env)
        files = _day_files(env)
        if not files:
            return 0
        marker = directory / "last_flush"
        if marker.exists() and marker.read_text(encoding="ascii").strip() == today:
            return 0
        oldest = ((now or datetime.now(UTC)).astimezone(UTC) - timedelta(days=_RETENTION_DAYS)).strftime("%Y-%m-%d")
        due: list[Path] = []
        for path in files:
            if path.stem < oldest:
                path.unlink(missing_ok=True)
            elif path.stem < today:
                due.append(path)
        if not due:
            return 0
        marker.write_text(today + "\n", encoding="ascii")
        url = env.get(ENDPOINT_ENV, "").strip() or ENDPOINT
        key = _install_key(env)
        for path in due[:_MAX_DAYS_PER_FLUSH]:
            rows = rows_for(path)
            if rows:
                payload = {"install_key": key, "lemoncrow_version": version, "day": path.stem, "rows": rows}
                status = post(url, payload, _POST_TIMEOUT_S)
                if not (200 <= status < 300 or status in {400, 413}):
                    break  # kept for the next day's attempt; 400 and 413 would fail the same way again
                sent += 1 if 200 <= status < 300 else 0
            path.unlink(missing_ok=True)
    return sent


def set_enabled(enabled: bool, environ: Mapping[str, str] | None = None) -> None:
    """Save the choice in the file the full engine reads, keeping its other settings. Off deletes what is waiting."""

    env = _environment(environ)
    path = _config_path(env)
    saved = _section(path) or {}
    destination = saved.get("destination")
    destination = destination if isinstance(destination, str) else ""
    if not enabled:
        destination = "off"
    elif destination == "off":
        destination = ""
    lines = [
        "[telemetry]",
        f"remote_enabled = {'true' if enabled else 'false'}",
        f"lexical_frustration_enabled = {'true' if saved.get('lexical_frustration_enabled') is True else 'false'}",
    ]
    values = ("destination", destination), ("endpoint", saved.get("endpoint")), ("token", saved.get("token"))
    lines += [f"{name} = {json.dumps(value)}" for name, value in values if isinstance(value, str) and value]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(temporary, path)
    if not enabled:
        discard_pending(env)


def managed(environ: Mapping[str, str] | None = None) -> bool:
    """Whether an administrator's policy file decides, so a person's switch would not."""

    return _policy_path(_environment(environ)).exists()


__all__ = [
    "ENDPOINT",
    "EVENT",
    "FEATURES",
    "FRICTION_METRICS",
    "NOTICE",
    "announce",
    "classify",
    "classify_reader_request",
    "discard_pending",
    "flush",
    "managed",
    "off_reason",
    "pending",
    "record",
    "rows_for",
    "set_enabled",
    "usage_dir",
]
