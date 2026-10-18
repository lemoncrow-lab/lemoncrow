"""Product telemetry configuration: opt-out state and where remote telemetry goes.

The local store (``telemetry.db``) and the local UI are always on. Only the
daily anonymous rollups (savings and event counts) leave the machine, and
:func:`resolve_destination` decides where they go:

- ``off``        local only; nothing is sent.
- ``lemoncrow``  lemoncrow.com (the LemonCrow hosted service).
- ``server``     the LemonCrow server this install is configured for
                 (``LEMONCROW_URL``): an enterprise's own server or our hosted one.
- ``custom``     any base URL, plus an optional bearer token, that serves
                 ``/api/telemetry/rollup`` and ``/api/telemetry/events``.

With no choice saved ("auto"), telemetry follows the configured server when it
is hosted and goes to lemoncrow.com otherwise. An administrator can fix the
destination with a policy file (:func:`policy_path`). Users cannot change a
managed destination, but ``DO_NOT_TRACK=1`` and ``LEMONCROW_TELEMETRY=off``
still turn remote telemetry off.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lemoncrow.core.foundation.identity import config_dir

logger = logging.getLogger("lemoncrow.product.telemetry.config")

FALSE_VALUES = {"0", "false", "off", "no"}
TRUE_VALUES = {"1", "true", "on", "yes"}

DESTINATIONS = ("off", "lemoncrow", "server", "custom")
LEMONCROW_BASE_URL = "https://lemoncrow.com"
ROLLUP_PATH = "/api/telemetry/rollup"
EVENTS_PATH = "/api/telemetry/events"
POLICY_ENV = "LEMONCROW_TELEMETRY_POLICY"
# Mirrors lemoncrow_client.config.DEFAULT_URL (the loopback server).
_DEFAULT_SERVER_URL = "http://127.0.0.1:7420"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,4096}$")
_MAX_URL_LENGTH = 2048


class TelemetryConfigError(ValueError):
    """A requested telemetry setting is invalid."""


class TelemetryPolicyLocked(TelemetryConfigError):
    """An administrator policy fixes the telemetry destination."""


@dataclass(frozen=True)
class TelemetryConfig:
    # On by default: anonymous counts/durations only -- no code, prompts, paths,
    # or symbol names (see telemetry/public_rollup.py `_payload`). Opt out with
    # `lc rt telemetry remote off`, DO_NOT_TRACK=1, or LEMONCROW_TELEMETRY=off.
    # Every other part of the runtime stays local.
    remote_enabled: bool = True
    lexical_frustration_enabled: bool = False
    # The user's saved choice: "" (auto) or one of DESTINATIONS.
    destination: str = ""
    endpoint: str = ""
    token: str = field(default="", repr=False)


@dataclass(frozen=True)
class TelemetryPolicy:
    """An administrator's fixed destination. ``error`` set means the policy
    file exists but is unusable, which locks telemetry off (fail closed)."""

    path: Path
    destination: str
    endpoint: str = ""
    token: str = field(default="", repr=False)
    error: str = ""


@dataclass(frozen=True)
class TelemetryDestination:
    """Where remote telemetry goes right now. A non-``off`` kind always has a
    valid ``base_url``; anything unresolvable collapses to ``off``."""

    kind: str
    base_url: str = ""
    token: str = field(default="", repr=False)
    managed: bool = False
    # default | user | policy | env | test
    source: str = "default"
    error: str = ""

    @property
    def enabled(self) -> bool:
        return self.kind != "off"


def config_path() -> Path:
    return Path(os.environ.get("LEMONCROW_TELEMETRY_CONFIG", config_dir() / "telemetry.toml"))


def _system_policy_path() -> Path:
    if sys.platform.startswith("win"):
        return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "LemonCrow" / "telemetry.toml"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/LemonCrow/telemetry.toml")
    return Path("/etc/lemoncrow/telemetry.toml")


def policy_path() -> Path:
    """The administrator policy file.

    The system path always wins. ``LEMONCROW_TELEMETRY_POLICY`` applies only
    where no system policy is installed (tests, staged MDM rollouts), so a user
    cannot point around an installed policy.
    """
    system = _system_policy_path()
    override = os.environ.get(POLICY_ENV, "").strip()
    if override:
        try:
            installed = system.exists()
        except OSError:
            installed = True
        if not installed:
            return Path(override)
    return system


def load_policy() -> TelemetryPolicy | None:
    path = policy_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError) as exc:
        # Present but unreadable: the administrator meant to lock something.
        return TelemetryPolicy(path, "off", error=f"policy file is unreadable ({type(exc).__name__})")
    try:
        section = tomllib.loads(text).get("telemetry")
    except tomllib.TOMLDecodeError:
        return TelemetryPolicy(path, "off", error="policy file is not valid TOML")
    if not isinstance(section, dict) or "destination" not in section:
        return None
    raw = section.get("destination")
    destination = raw.strip().lower() if isinstance(raw, str) else ""
    if destination not in DESTINATIONS:
        return TelemetryPolicy(path, "off", error=f"policy destination must be one of {', '.join(DESTINATIONS)}")
    endpoint = section.get("endpoint", "")
    token = section.get("token", "")
    return TelemetryPolicy(
        path,
        destination,
        endpoint=endpoint if isinstance(endpoint, str) else "",
        token=token if isinstance(token, str) else "",
    )


def _load_file_config() -> TelemetryConfig:
    """Configuration exactly as persisted in telemetry.toml (no test overrides)."""
    data: dict[str, Any] = {}
    path = config_path()
    if path.exists():
        try:
            loaded = tomllib.loads(path.read_text(encoding="utf-8"))
            section = loaded.get("telemetry", {})
            data = section if isinstance(section, dict) else {}
        except Exception:
            # Unreadable/invalid telemetry.toml must not print to the user;
            # fall back to defaults and keep the traceback under debug.
            logger.debug("telemetry.config_read_failed", exc_info=True, extra={"path": str(path)})
            data = {}

    raw_destination = data.get("destination")
    destination = raw_destination.strip().lower() if isinstance(raw_destination, str) else ""
    if destination not in DESTINATIONS:
        destination = ""
    # `remote_enabled = false` (the pre-destination opt-out) always means off.
    if not _bool(data.get("remote_enabled"), True):
        destination = "off"
    endpoint = data.get("endpoint")
    token = data.get("token")
    return TelemetryConfig(
        remote_enabled=destination != "off",
        lexical_frustration_enabled=_bool(data.get("lexical_frustration_enabled"), False),
        destination=destination,
        endpoint=endpoint if isinstance(endpoint, str) else "",
        token=token if isinstance(token, str) else "",
    )


def _env_master_disabled() -> bool:
    """Global opt-out kill switch, independent of telemetry.toml.

    Honors the cross-tool ``DO_NOT_TRACK=1`` convention and
    ``LEMONCROW_TELEMETRY=off`` (any false-y value). When set, no remote
    telemetry is ever emitted regardless of the on-disk config or policy.
    """
    if os.environ.get("DO_NOT_TRACK", "").strip().lower() in TRUE_VALUES:
        return True
    if os.environ.get("LEMONCROW_TELEMETRY", "").strip().lower() in FALSE_VALUES:
        return True
    return False


def _test_guard_active() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and os.environ.get("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS") != "1"


def normalize_endpoint(raw: str) -> str:
    """Validate a telemetry base URL. HTTPS is required off loopback, and
    credentials go in the token, never the URL (it is shown in the UI)."""
    text = str(raw or "").strip()
    if not text:
        raise TelemetryConfigError("a custom destination needs an endpoint URL")
    if len(text) > _MAX_URL_LENGTH or not text.isascii() or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in text):
        raise TelemetryConfigError("endpoint must be a plain ASCII URL")
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").strip("[]").lower()
        has_credentials = bool(parts.username or parts.password)
    except ValueError as exc:
        raise TelemetryConfigError("endpoint is not a valid URL") from exc
    if parts.scheme not in {"http", "https"} or not host:
        raise TelemetryConfigError("endpoint must be an http(s) URL with a host")
    if has_credentials:
        raise TelemetryConfigError("endpoint must not embed credentials; use a token")
    if parts.query or parts.fragment:
        raise TelemetryConfigError("endpoint must be a base URL without a query or fragment")
    if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise TelemetryConfigError("endpoint must use HTTPS outside loopback")
    return text.rstrip("/")


def _normalize_token(raw: str) -> str:
    text = str(raw or "").strip()
    if text and not _TOKEN_RE.match(text):
        raise TelemetryConfigError("token must be printable ASCII without spaces")
    return text


def _client_environment() -> tuple[dict[str, str], Path]:
    """The ``LEMONCROW_*`` settings the thin client resolves its server from:
    legacy ``~/.lemoncrow/env`` compatibility settings (read-only after product isolation) under the
    real environment."""
    home = os.environ.get("LEMONCROW_HOME", "").strip()
    state_dir = Path(home) if home else Path(os.environ.get("HOME") or str(Path.home())) / ".lemoncrow"
    persisted: dict[str, str] = {}
    try:
        from lemoncrow_client.config import read_settings_file

        persisted = read_settings_file(state_dir)
    except Exception:
        logger.debug("telemetry.client_settings_unreadable", exc_info=True)
    return {**persisted, **os.environ}, state_dir


def configured_server_url() -> tuple[str, bool]:
    """``(url, hosted)`` for the LemonCrow server this install is configured
    for; ``("", False)`` when the configured URL is invalid."""
    environment, _state_dir = _client_environment()
    try:
        url = normalize_endpoint(environment.get("LEMONCROW_URL", "").strip() or _DEFAULT_SERVER_URL)
    except TelemetryConfigError:
        return "", False
    mode = environment.get("LEMONCROW_INSTALL_MODE", "").strip().lower()
    host = (urlsplit(url).hostname or "").strip("[]").lower()
    hosted = mode == "hosted" if mode in {"local", "hosted"} else host not in _LOOPBACK_HOSTS
    return url, hosted


def _server_token() -> str:
    """The credential this install already presents to its LemonCrow server."""
    environment, state_dir = _client_environment()
    try:
        from lemoncrow_client.config import resolve_token

        token, _source = resolve_token(environment, state_dir)
    except Exception:
        logger.debug("telemetry.server_token_unavailable", exc_info=True)
        return ""
    return str(token or "")


def _build_destination(
    kind: str,
    endpoint: str,
    token: str,
    *,
    managed: bool,
    source: str,
    with_token: bool,
) -> TelemetryDestination:
    if kind == "lemoncrow":
        return TelemetryDestination("lemoncrow", LEMONCROW_BASE_URL, managed=managed, source=source)
    if kind == "server":
        url, _hosted = configured_server_url()
        if not url:
            return TelemetryDestination(
                "off", managed=managed, source=source, error="no valid LemonCrow server URL is configured"
            )
        return TelemetryDestination(
            "server", url, _server_token() if with_token else "", managed=managed, source=source
        )
    if kind == "custom":
        try:
            base = normalize_endpoint(endpoint)
            bearer = _normalize_token(token)
        except TelemetryConfigError as exc:
            return TelemetryDestination("off", managed=managed, source=source, error=str(exc))
        return TelemetryDestination("custom", base, bearer if with_token else "", managed=managed, source=source)
    return TelemetryDestination("off", managed=managed, source=source)


def resolve_destination(*, with_token: bool = False) -> TelemetryDestination:
    """Where remote telemetry goes. Precedence: test guard, env opt-out,
    administrator policy, the user's saved choice, then the default.

    ``with_token`` also resolves the bearer credential (it reads the server
    token file); only the uploaders need it.
    """
    if _test_guard_active():
        return TelemetryDestination("off", source="test")
    if _env_master_disabled():
        return TelemetryDestination("off", source="env")
    policy = load_policy()
    if policy is not None:
        if policy.error:
            return TelemetryDestination("off", managed=True, source="policy", error=policy.error)
        return _build_destination(
            policy.destination, policy.endpoint, policy.token, managed=True, source="policy", with_token=with_token
        )
    cfg = _load_file_config()
    if cfg.destination:
        return _build_destination(
            cfg.destination, cfg.endpoint, cfg.token, managed=False, source="user", with_token=with_token
        )
    url, hosted = configured_server_url()
    return _build_destination(
        "server" if url and hosted else "lemoncrow", "", "", managed=False, source="default", with_token=with_token
    )


def destination_url(destination: TelemetryDestination, path: str, override_env: str) -> str:
    """The URL one upload channel posts to; "" when remote telemetry is off.

    ``override_env`` (a full URL, for local development) applies unless an
    administrator policy manages the destination.
    """
    if not destination.enabled:
        return ""
    if not destination.managed:
        override = os.environ.get(override_env, "").strip()
        if override:
            return override
    return destination.base_url + path


def auth_headers(destination: TelemetryDestination, url: str) -> dict[str, str]:
    """The bearer header for ``url``, only on the destination's own origin, so a
    development URL override never receives a server credential."""
    if not destination.token or not _same_origin(url, destination.base_url):
        return {}
    return {"Authorization": f"Bearer {destination.token}"}


def _same_origin(left: str, right: str) -> bool:
    try:
        a, b = urlsplit(left), urlsplit(right)
        return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)
    except ValueError:
        return False


def load_telemetry_config() -> TelemetryConfig:
    """The saved configuration with ``remote_enabled`` reflecting the effective
    state (test guard, env kill switch, administrator policy)."""
    return replace(_load_file_config(), remote_enabled=resolve_destination().enabled)


def save_telemetry_config(
    *,
    remote_enabled: bool | None = None,
    lexical_frustration_enabled: bool | None = None,
    destination: str | None = None,
    endpoint: str | None = None,
    token: str | None = None,
) -> TelemetryConfig:
    """Persist user settings. Destination changes (including ``remote_enabled``)
    raise :class:`TelemetryPolicyLocked` under an administrator policy."""
    if any(value is not None for value in (remote_enabled, destination, endpoint, token)):
        policy = load_policy()
        if policy is not None:
            raise TelemetryPolicyLocked(f"the telemetry destination is managed by your organization ({policy.path})")
    current = _load_file_config()
    next_destination = current.destination
    if destination is not None:
        choice = str(destination).strip().lower()
        if choice == "auto":
            choice = ""
        if choice and choice not in DESTINATIONS:
            raise TelemetryConfigError(f"destination must be auto or one of {', '.join(DESTINATIONS)}")
        next_destination = choice
    elif remote_enabled is not None:
        if not remote_enabled:
            next_destination = "off"
        elif next_destination == "off":
            next_destination = ""
    next_endpoint = current.endpoint
    if endpoint is not None:
        next_endpoint = normalize_endpoint(endpoint) if str(endpoint).strip() else ""
    next_token = current.token if token is None else _normalize_token(token)
    if next_destination == "custom":
        next_endpoint = normalize_endpoint(next_endpoint)
    next_cfg = TelemetryConfig(
        remote_enabled=next_destination != "off",
        lexical_frustration_enabled=(
            current.lexical_frustration_enabled if lexical_frustration_enabled is None else lexical_frustration_enabled
        ),
        destination=next_destination,
        endpoint=next_endpoint,
        token=next_token,
    )
    lines = [
        "[telemetry]",
        f"remote_enabled = {_toml_bool(next_cfg.remote_enabled)}",
        f"lexical_frustration_enabled = {_toml_bool(next_cfg.lexical_frustration_enabled)}",
    ]
    # Values are validated printable ASCII, for which a JSON string is a valid
    # TOML basic string.
    if next_cfg.destination:
        lines.append(f"destination = {json.dumps(next_cfg.destination)}")
    if next_cfg.endpoint:
        lines.append(f"endpoint = {json.dumps(next_cfg.endpoint)}")
    if next_cfg.token:
        lines.append(f"token = {json.dumps(next_cfg.token)}")
    _write_private(config_path(), "\n".join(lines) + "\n")
    return next_cfg


def _write_private(path: Path, text: str) -> None:
    """Atomic, owner-only write: the file may hold a telemetry token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def telemetry_status() -> dict[str, Any]:
    """Settings and the effective destination, for the CLI, API and UI.
    Never includes a token."""
    cfg = _load_file_config()
    destination = resolve_destination()
    policy = load_policy()
    server_url, server_hosted = configured_server_url()
    return {
        "remote_enabled": destination.enabled,
        "lexical_frustration_enabled": cfg.lexical_frustration_enabled,
        "destination": destination.kind,
        "destination_setting": cfg.destination or "auto",
        "destination_source": destination.source,
        "destination_url": destination.base_url,
        "destination_error": destination.error,
        "endpoint": cfg.endpoint,
        "token_set": bool(cfg.token),
        "server_url": server_url,
        "server_hosted": server_hosted,
        "managed": policy is not None,
        "policy_path": str(policy.path) if policy is not None else "",
    }


def remote_enabled() -> bool:
    return resolve_destination().enabled


def lemoncrow_cloud_enabled() -> bool:
    """Whether telemetry may go to LemonCrow's own services (PostHog, usage
    sync). Never true for an enterprise or custom destination."""
    return resolve_destination().kind == "lemoncrow"


def lexical_frustration_enabled() -> bool:
    return load_telemetry_config().lexical_frustration_enabled


def otel_endpoint() -> str:
    return os.environ.get("LEMONCROW_OTEL_ENDPOINT", "http://localhost:4318")


def posthog_key() -> str:
    return os.environ.get("LEMONCROW_POSTHOG_KEY", "")


def posthog_host() -> str:
    return os.environ.get("LEMONCROW_POSTHOG_HOST", "https://us.i.posthog.com")


def posthog_otlp_url() -> str:
    """Direct OTLP ingest endpoint on PostHog - no local collector required."""
    return posthog_host().rstrip("/") + "/i/v0/otlp"


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in TRUE_VALUES:
            return True
        if lowered in FALSE_VALUES:
            return False
    return default


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
