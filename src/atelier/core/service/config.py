"""Service configuration — read from environment variables.

All values have safe defaults suitable for local/SQLite mode.
"""

from __future__ import annotations

import os

from atelier.core.foundation.paths import default_store_root


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name, "").lower()
    if not val:
        return default
    return val in ("1", "true", "yes")


class ServiceConfig:
    """Live view of service configuration from environment variables."""

    @property
    def service_enabled(self) -> bool:
        return _bool_env("ATELIER_SERVICE_ENABLED", False)

    @property
    def require_auth(self) -> bool:
        return _bool_env("ATELIER_REQUIRE_AUTH", True)

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
    def knowledge_root(self) -> str | None:
        """Project-local knowledge root (usually ./.knowledge)."""
        return os.environ.get("ATELIER_KNOWLEDGE_ROOT")

    @property
    def dev_mode(self) -> bool:
        """Whether the runtime is in developer mode. Gated features (Lint, Reasoning, Verify)
        require this to be enabled. Tracking and analytics remain active in all modes.
        """
        return _bool_env("ATELIER_DEV_MODE", False)

    def as_dict(self) -> dict[str, object]:
        """Return config summary — never includes the api_key value."""
        return {
            "service_enabled": self.service_enabled,
            "require_auth": self.require_auth,
            "api_key_configured": bool(self.api_key),
            "dev_mode": self.dev_mode,
            "host": self.host,
            "port": self.port,
            "storage_backend": self.storage_backend,
            "database_url_configured": bool(self.database_url),
            "atelier_root": self.atelier_root,
            "knowledge_root": self.knowledge_root,
        }


# Module-level singleton
cfg = ServiceConfig()
