"""Service configuration — read from environment variables.

All values have safe defaults suitable for local/SQLite mode.
"""

from __future__ import annotations

import os

from atelier.core.environment import bool_env
from atelier.core.foundation.paths import default_store_root


def _bool_env(name: str, default: bool) -> bool:
    return bool_env(name, default)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback_host(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_HOSTS


class ServiceConfig:
    """Live view of service configuration from environment variables."""

    @property
    def service_enabled(self) -> bool:
        return _bool_env("ATELIER_SERVICE_ENABLED", False)

    @property
    def require_auth(self) -> bool:
        """Whether Bearer auth is enforced on protected routes.

        Secure default: auth is REQUIRED unless the service binds to a
        loopback host (127.0.0.1/localhost/::1) for local dev. Binding a
        non-loopback host exposes memory, traces, and config to the network,
        so it fails closed (auth on) by default.

        An explicit ``ATELIER_REQUIRE_AUTH`` always wins, in either direction.
        """
        if "ATELIER_REQUIRE_AUTH" in os.environ:
            return _bool_env("ATELIER_REQUIRE_AUTH", False)
        return not _is_loopback_host(self.host)

    @property
    def api_key(self) -> str:
        """The expected API key value.  Empty string means no key configured."""
        return os.environ.get("ATELIER_API_KEY", "")

    @property
    def host(self) -> str:
        return os.environ.get("ATELIER_SERVICE_HOST", "127.0.0.1")

    @property
    def port(self) -> int:
        return int(os.environ.get("ATELIER_SERVICE_PORT", "8787"))

    @property
    def storage_backend(self) -> str:
        return os.environ.get("ATELIER_STORAGE_BACKEND", "sqlite")

    @property
    def database_url(self) -> str:
        return os.environ.get("ATELIER_DATABASE_URL", "")

    @property
    def atelier_root(self) -> str:
        return os.environ.get("ATELIER_ROOT", str(default_store_root()))

    @property
    def lessons_root(self) -> str | None:
        """Project-local lessons root (usually ./.atelier/lessons)."""
        return os.environ.get("ATELIER_LESSONS_ROOT")

    def as_dict(self) -> dict[str, object]:
        """Return config summary — never includes the api_key value."""
        return {
            "service_enabled": self.service_enabled,
            "require_auth": self.require_auth,
            "api_key_configured": bool(self.api_key),
            "host": self.host,
            "port": self.port,
            "storage_backend": self.storage_backend,
            "database_url_configured": bool(self.database_url),
            "atelier_root": self.atelier_root,
            "lessons_root": self.lessons_root,
        }


# Module-level singleton
cfg = ServiceConfig()
